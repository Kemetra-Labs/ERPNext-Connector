# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt
"""RT-1351 — a Frappe ``TimestampMismatchError`` is retried, never ``permanently_rejected``.

RT-317 A1c (lab, 3000 sales/h) left one accepted sale ``permanently_rejected / validation`` with
``TimestampMismatchError: ... has been modified after you have opened it``. Frappe v16.52.0
declares ``class TimestampMismatchError(ValidationError)`` (``frappe/exceptions.py``) and raises it
from ``Document.check_if_latest`` when the row's ``modified`` differs from the in-memory copy. It is a
failure of Frappe's optimistic-lock check, not a business rule (the lab showed no second posting: the
postings of a tick run one after another), but the generic ``except frappe.ValidationError`` branch
rejected it. The fix lists it in ``_transient_exceptions()``, which every stage tries FIRST.

Exactly-once is unchanged, and these tests pin it:

* the conflict is raised inside ``_submit_atomically``'s savepoint, so the insert is rolled back
  and nothing is committed (RT-48 / RT-458): "failed_transient" still means "nothing posted";
* a re-offer after the conflict posts once (one submit, one Posting Log write);
* a re-offer after a PARTIAL commit (an invoice with this provenance already exists) is stopped by
  the ``unique_rt_si_provenance`` index and resolves to that invoice through the G5 recovery: one
  ``posted`` ack on the existing document, no second submit;
* a business ``ValidationError`` is still ``permanently_rejected / validation``.

The retry bound is Backend-Core's existing per-item budget: each ``failed_transient`` ack spends one
unit and, when it runs out, ends the item as ``retry_budget_exhausted`` (a repairable state), so a
conflict that never clears is surfaced, not lost.
"""

import dataclasses
import importlib
import sys
import types
from decimal import Decimal

import pytest

from retail_tower_erpnext_connector.connector.posting import contracts as c
from retail_tower_erpnext_connector.connector.posting.tender import TenderModeMap
from retail_tower_erpnext_connector.connector.posting.uom import (
	PreResolvedWarehouse,
	StoreCustomerMap,
	UomMap,
)

_GLUE = "retail_tower_erpnext_connector.connector.posting.frappe_glue"
_PKG = "retail_tower_erpnext_connector.connector.posting"
_MISSING = object()
_STORE = "33333333-3333-4333-8333-333333333333"
_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_REF = "13511351-1351-4351-8351-135113511351"
_FRESH = "ACC-SINV-2026-01139"
_EXISTING = c.ErpnextDocumentRef(doctype="Sales Invoice", name="ACC-SINV-2026-01138")

# The existing Sales Invoice as the database holds it: exactly what Retail Tower sent (RT-378).
_EXISTING_DOC = {
	"docstatus": 1,
	"currency": "EGP",
	"customer": "Walk-in",
	"company": "RT Company",
	"ignore_pricing_rule": 1,
	"grand_total": 100.0,
	"taxes": [],
	"items": [
		{
			"item_code": "ITEM-A",
			"warehouse": "Map WH",
			"rt_line_ref": _A,
			"qty": 1.0,
			"uom": "Nos",
			"rate": 100.0,
			"amount": 100.0,
			"price_list_rate": 100.0,
			"discount_percentage": 0.0,
			"discount_amount": 0.0,
			"margin_type": "",
			"margin_rate_or_amount": 0.0,
		}
	],
}


@dataclasses.dataclass
class _World:
	"""What ERPNext does to the next post; the test flips it between two offers of one sale."""

	timeline: list = dataclasses.field(default_factory=list)
	insert_error: object = None  # "duplicate" (the G5 unique index) or an exception instance
	submit_error: BaseException | None = None
	provenance_hit: bool = False  # an invoice already exists under the sale's provenance
	acks: list = dataclasses.field(default_factory=list)
	records: list = dataclasses.field(default_factory=list)


class _Logger:
	def info(self, record):
		pass

	warning = error = info


class _Invoice:
	def __init__(self, payload, world, fake):
		self.payload, self._world, self._fake, self.name, self.values = payload, world, fake, _FRESH, {}

	def insert(self):
		if self._world.insert_error == "duplicate":
			raise self._fake.exceptions.UniqueValidationError("Duplicate entry for unique_rt_si_provenance")
		if self._world.insert_error is not None:
			raise self._world.insert_error
		grand = float(sum(Decimal(i["amount"]) for i in self.payload["items"]))
		self.values = {
			"grand_total": grand,
			"paid_amount": 0.0,
			"change_amount": 0.0,
			"write_off_amount": 0.0,
			"outstanding_amount": grand,
		}
		self._world.timeline.append("insert")

	def get(self, field):
		return {"company": "RT Company", **self.payload, **self.values}.get(field)

	def submit(self):
		if self._world.submit_error is not None:
			raise self._world.submit_error
		self._world.timeline.append("submit")

	def reload(self):
		self._world.timeline.append("reload")


class _Frappe(types.ModuleType):
	"""frappe v16.52.0 shape: ``TimestampMismatchError`` is a ``ValidationError`` exported at top level."""

	def __init__(self, world):
		super().__init__("frappe")
		self.ValidationError = type("ValidationError", (Exception,), {})
		self.TimestampMismatchError = type("TimestampMismatchError", (self.ValidationError,), {})
		self.exceptions = types.SimpleNamespace(
			UniqueValidationError=type("UniqueValidationError", (self.ValidationError,), {})
		)
		self.logger = lambda name=None: _Logger()
		self._world = world
		timeline = world.timeline
		self.db = types.SimpleNamespace(
			get_value=self._get_value,
			get_single_value=lambda doctype, field: "RT Company",
			savepoint=lambda name: timeline.append("savepoint"),
			rollback=lambda save_point=None: timeline.append("rollback"),
			commit=lambda: timeline.append("commit"),
		)
		self.utils = types.SimpleNamespace(get_system_timezone=lambda: "UTC")

	def _get_value(self, doctype, filters, fields=None, **_):
		assert isinstance(filters, dict) and filters.get("rt_external_id") == "POS-13511"
		return _EXISTING.name if self._world.provenance_hit else None

	def get_all(self, doctype, **kwargs):
		return []  # Items: no batch/serial tracking

	def get_doc(self, payload, name=None):
		if name is not None:  # an existing invoice read back from the database
			assert (payload, name) == ("Sales Invoice", _EXISTING.name)
			self._world.timeline.append("read")
			return dict(_EXISTING_DOC)
		return _Invoice(payload, self._world, self)


class _Client:
	def __init__(self, world):
		self.world = world

	def ack_outcome(self, ref, request, idempotency_key):
		wire = request.to_wire()
		self.world.timeline.append(f"ack:{wire['outcome']}")
		self.world.acks.append({"wire": wire, "key": idempotency_key})


class _Store:
	"""The Posting Log: one row per key, a second record of another ref is a conflict."""

	def __init__(self, world):
		self.world = world

	def get_document_ref(self, key):
		return None

	def record_posted(self, key, ref):
		self.world.timeline.append("record_posted")
		self.world.records.append(ref)


@pytest.fixture
def load_glue():
	pkg = importlib.import_module(_PKG)
	saved = [
		(sys.modules, "frappe", sys.modules.get("frappe", _MISSING)),
		(sys.modules, _GLUE, sys.modules.get(_GLUE, _MISSING)),
		(vars(pkg), "frappe_glue", vars(pkg).get("frappe_glue", _MISSING)),
	]

	def _load(fake):
		sys.modules["frappe"] = fake
		sys.modules.pop(_GLUE, None)
		return importlib.import_module(_GLUE)

	yield _load
	for namespace, name, value in saved:
		namespace.pop(name, None)
		if value is not _MISSING:
			namespace[name] = value


def _work_item(cursor):
	return c.PostingWorkItem.from_wire(
		{
			"workItemRef": _REF,
			"kind": "sale_post",
			"sourceSystem": "pos-pulse",
			"externalId": "POS-13511",
			"payloadHash": "d" * 64,
			"businessDate": "2026-06-01",
			"itemCursor": str(cursor),
			"sale": {
				"saleRef": "22222222-2222-4222-8222-222222222222",
				"storeId": _STORE,
				"currencyCode": "EGP",
				"posTotal": "100.00",
				"occurredAt": "2026-06-01T10:00:00Z",
				"businessDate": "2026-06-01",
				"sourceSystem": "pos-pulse",
				"externalId": "POS-13511",
				"lines": [
					{
						"lineRef": _A,
						"lineName": "Item A",
						"unitPrice": "100.00",
						"currencyCode": "EGP",
						"quantity": "1",
						"lineAmount": "100.00",
						"unit": "each",
						"erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"},
					}
				],
			},
		}
	)


def _offer(glue, world, cursor):
	return glue.post_work_item(
		_work_item(cursor),
		client=_Client(world),
		store=_Store(world),
		uom_map=UomMap({"each": "Nos"}),
		warehouses=PreResolvedWarehouse({_STORE: {"doctype": "Warehouse", "name": "Map WH"}}),
		customers=StoreCustomerMap({_STORE: "Walk-in"}),
		tenders=TenderModeMap({"cash": "Cash"}),
		correlation_id="rt1351-test",
	)


def _conflict(fake):
	return fake.TimestampMismatchError(
		"Error: ACC-SINV-2026-01138 (Sales Invoice) has been modified after you have opened it "
		"(2026-11-12 12:27:25, 2026-11-12 12:27:25.070314). Please refresh to get the latest document."
	)


class TestTimestampMismatchIsTransient:
	@pytest.mark.parametrize("where", ["insert", "submit"])
	def test_conflict_acks_failed_transient_and_commits_nothing(self, load_glue, where):
		world = _World()
		fake = _Frappe(world)
		if where == "insert":
			world.insert_error = _conflict(fake)
		else:
			world.submit_error = _conflict(fake)

		outcome = _offer(load_glue(fake), world, 7)

		assert outcome == "failed_transient"
		assert "rollback" in world.timeline  # RT-48: the savepoint undid the insert
		assert "commit" not in world.timeline and "record_posted" not in world.timeline
		assert world.timeline[-1] == "ack:failed_transient"
		assert [a["wire"]["outcome"] for a in world.acks] == ["failed_transient"]
		assert (
			world.acks[0]["key"] == f"{_REF}:failed_transient:7"
		)  # RT-171: per offer, so a re-offer is heard

	def test_a_plain_validation_error_is_still_a_permanent_rejection(self, load_glue):
		world = _World()
		fake = _Frappe(world)
		world.submit_error = fake.ValidationError("Item ITEM-A is disabled")

		outcome = _offer(load_glue(fake), world, 7)

		assert outcome == "permanently_rejected"
		assert world.acks[0]["wire"]["reason"]["category"] == "validation"
		assert world.timeline == ["savepoint", "insert", "rollback", "ack:permanently_rejected"]
		assert "commit" not in world.timeline

	def test_an_older_frappe_without_the_class_still_rejects_validation_errors(self, load_glue):
		# getattr-guarded like the other names: a frappe that lacks the class degrades, not errors.
		world = _World()
		fake = _Frappe(world)
		del fake.TimestampMismatchError
		world.submit_error = fake.ValidationError("Item ITEM-A is disabled")

		assert _offer(load_glue(fake), world, 7) == "permanently_rejected"


class TestExactlyOnceAfterAConflict:
	def test_a_re_offer_after_a_conflict_posts_one_invoice(self, load_glue):
		world = _World()
		fake = _Frappe(world)
		glue = load_glue(fake)

		world.submit_error = _conflict(fake)
		assert _offer(glue, world, 7) == "failed_transient"
		world.submit_error = None
		assert _offer(glue, world, 9) == "posted"

		assert world.timeline.count("submit") == 1
		assert world.records == [c.ErpnextDocumentRef(doctype="Sales Invoice", name=_FRESH)]
		assert [a["wire"]["outcome"] for a in world.acks] == ["failed_transient", "posted"]
		assert [a["key"] for a in world.acks] == [f"{_REF}:failed_transient:7", f"{_REF}:posted"]

	def test_a_re_offer_after_a_partial_commit_resolves_to_the_existing_invoice(self, load_glue):
		# Offer 1 conflicted. Meanwhile an invoice with this provenance was committed (a concurrent
		# worker, or a crash between submit and record). Offer 2's insert is refused by the unique
		# provenance index, and the G5 recovery echoes the existing invoice: never a second document.
		world = _World()
		fake = _Frappe(world)
		glue = load_glue(fake)

		world.submit_error = _conflict(fake)
		assert _offer(glue, world, 7) == "failed_transient"
		world.submit_error = None
		world.insert_error, world.provenance_hit = "duplicate", True
		before = len(world.timeline)
		assert _offer(glue, world, 9) == "posted"

		second_offer = world.timeline[before:]
		# No insert and no submit on the second offer: the only invoice is the one that already existed.
		assert second_offer == ["savepoint", "rollback", "record_posted", "read", "commit", "ack:posted"]
		assert world.timeline.count("submit") == 0
		assert world.records == [_EXISTING]  # the Posting Log is back-filled with the EXISTING invoice
		assert world.acks[-1]["wire"]["documentRef"]["name"] == _EXISTING.name
