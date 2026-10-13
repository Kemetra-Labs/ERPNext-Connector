# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt
"""RT-458 (RT-382 F-1) — an item's ERPNext effects are committed before its outcome ack leaves.

RT-382 reproduced the defect on the rt9 bench: the ``posted`` ack reached Backend-Core while the
scheduler tick's transaction was still open, so a later failure in the same tick rolled back the
Sales Invoice, its ledgers and the Posting Log, and the naming series handed the lost sale's name to
the next invoice. The fix commits the item's effects after submit, ``record_posted`` and the RT-378
read-back, and before the ack.

Every collaborator writes to ONE timeline (the frappe stand-in, the Posting Log store and the
Backend-Core client), so the order of ``commit`` against the ack is asserted directly:

* every ``posted`` / ``reconciliation_required`` path commits first: the fresh sale, the
  duplicate-provenance back-fill, the ``IdempotencyConflict`` echo and the replay guard (the same
  paths on the reversal leg are in ``test_return_glue``; the legacy-refund recovery in
  ``test_refund_containment_glue``);
* a commit that fails sends no ack;
* a read-back that fails after submit commits before its ``failed_transient`` ack (the document
  exists, so a re-offer goes through the replay guard);
* a rejection never commits: whatever was inserted was rolled back to the RT-48 savepoint, so
  "rejected" still means "nothing posted";
* a failing ack (RT-382 Run B's 500) leaves the commit already done.
"""

import dataclasses
import importlib
import sys
import types
from decimal import Decimal

import pytest

from retail_tower_erpnext_connector.connector.posting import contracts as c
from retail_tower_erpnext_connector.connector.posting.idempotency import IdempotencyConflict
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
_REF = "45845845-8458-4458-8458-458458458458"
_FRESH = "ACC-SINV-2026-45801"
_EXISTING = c.ErpnextDocumentRef(doctype="Sales Invoice", name="ACC-SINV-2026-45800")

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
			"warehouse": "Frozen WH",
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


@dataclasses.dataclass(frozen=True)
class _Scenario:
	"""What ERPNext, the Posting Log and Backend-Core do for one post (defaults: a clean fresh post).

	``insert_error`` is ``"duplicate"`` (the G5 unique provenance index), ``"validation"`` or an
	exception instance; ``provenance_hit`` means an invoice already exists under the sale's provenance.
	"""

	provenance_hit: bool = False
	insert_error: object = None
	after_insert: dict = dataclasses.field(default_factory=dict)
	after_submit: dict = dataclasses.field(default_factory=dict)
	unreadable: bool = False
	commit_error: BaseException | None = None
	existing: c.ErpnextDocumentRef | None = None  # the Posting Log replay guard's hit
	conflict: c.ErpnextDocumentRef | None = None  # another worker recorded this first
	ack_fails: bool = False  # Backend-Core answers like RT-382 Run B's injected 500
	uom: dict = dataclasses.field(default_factory=lambda: {"each": "Nos"})


class _Logger:
	def info(self, record):
		pass

	warning = error = info


class _Invoice:
	"""The Sales Invoice this tick inserts: the payload as sent plus ERPNext's computed totals."""

	def __init__(self, payload, fake):
		self.payload, self._fake, self.name, self.values = payload, fake, _FRESH, {}

	def insert(self):
		error = self._fake.scenario.insert_error
		if error == "duplicate":  # the G5 unique provenance index (bench frappe v15 raises this)
			raise self._fake.exceptions.UniqueValidationError("Duplicate entry for unique_rt_si_provenance")
		if error == "validation":
			raise self._fake.ValidationError("Server Scripts are disabled")
		if error is not None:
			raise error
		grand = float(sum(Decimal(i["amount"]) for i in self.payload["items"]))
		self.values = {"grand_total": grand, "paid_amount": 0.0, "change_amount": 0.0,
			"write_off_amount": 0.0, "outstanding_amount": grand}
		self.values.update(self._fake.scenario.after_insert)
		self._fake.timeline.append("insert")

	def get(self, field):
		return {"company": "RT Company", **self.payload, **self.values}.get(field)

	def submit(self):
		self._fake.timeline.append("submit")

	def reload(self):
		self._fake.timeline.append("reload")
		if self._fake.scenario.unreadable:
			raise TimeoutError("Lock wait timeout exceeded")
		self.values.update(self._fake.scenario.after_submit)


class _Frappe(types.ModuleType):
	def __init__(self, timeline, scenario):
		super().__init__("frappe")
		self.ValidationError = type("ValidationError", (Exception,), {})
		self.exceptions = types.SimpleNamespace(
			UniqueValidationError=type("UniqueValidationError", (self.ValidationError,), {})
		)
		self.logger = lambda name=None: _Logger()
		self.timeline, self.scenario = timeline, scenario
		self.db = types.SimpleNamespace(
			get_value=self._get_value,
			get_single_value=lambda doctype, field: "RT Company",  # RT-378 site default company
			savepoint=lambda name: timeline.append("savepoint"),
			rollback=lambda save_point=None: timeline.append("rollback"),
			commit=self._commit,
		)
		self.utils = types.SimpleNamespace(get_system_timezone=lambda: "UTC")

	def _commit(self):
		self.timeline.append("commit")
		if self.scenario.commit_error is not None:
			raise self.scenario.commit_error

	def _get_value(self, doctype, filters, fields=None, **_):
		assert isinstance(filters, dict) and filters.get("rt_external_id") == "POS-45801"
		return _EXISTING.name if self.scenario.provenance_hit else None

	def get_all(self, doctype, **kwargs):
		return []  # Items: no batch/serial tracking

	def get_doc(self, payload, name=None):
		if name is not None:  # an existing invoice read back from the database
			assert (payload, name) == ("Sales Invoice", _EXISTING.name)
			self.timeline.append("read")
			return dict(_EXISTING_DOC)
		return _Invoice(payload, self)


class _Client:
	"""Backend-Core's ack endpoint."""

	def __init__(self, timeline, scenario):
		self.timeline, self.scenario = timeline, scenario

	def ack_outcome(self, ref, request, idempotency_key):
		self.timeline.append(f"ack:{request.to_wire()['outcome']}")
		if self.scenario.ack_fails:
			raise RuntimeError("unexpected ack status 500: {'code': 'rt382_injected_fault'}")


class _Store:
	"""The Posting Log."""

	def __init__(self, timeline, scenario):
		self.timeline, self.scenario = timeline, scenario

	def get_document_ref(self, key):
		return self.scenario.existing

	def record_posted(self, key, ref):
		self.timeline.append("record_posted")
		if self.scenario.conflict is not None:
			raise IdempotencyConflict(key, self.scenario.conflict, ref)


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


def _work_item():
	return c.PostingWorkItem.from_wire(
		{
			"workItemRef": _REF,
			"kind": "sale_post",
			"sourceSystem": "pos-pulse",
			"externalId": "POS-45801",
			"payloadHash": "c" * 64,
			"businessDate": "2026-06-01",
			"itemCursor": "45",
			"resolutionVersion": 1,
			"sale": {
				"saleRef": "22222222-2222-4222-8222-222222222222",
				"storeId": _STORE,
				"currencyCode": "EGP",
				"posTotal": "100.00",
				"occurredAt": "2026-06-01T10:00:00Z",
				"businessDate": "2026-06-01",
				"sourceSystem": "pos-pulse",
				"externalId": "POS-45801",
				"warehouseRef": {"doctype": "Warehouse", "name": "Frozen WH"},
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


_CLEAN = _Scenario()  # a fresh post that nothing interrupts


def _post(load_glue, scenario=_CLEAN, timeline=None):
	"""Post the sale under ``scenario``; every collaborator writes to the one returned ``timeline``."""
	timeline = [] if timeline is None else timeline
	glue = load_glue(_Frappe(timeline, scenario))
	outcome = glue.post_work_item(
		_work_item(),
		client=_Client(timeline, scenario),
		store=_Store(timeline, scenario),
		uom_map=UomMap(scenario.uom),
		warehouses=PreResolvedWarehouse({_STORE: {"doctype": "Warehouse", "name": "Map WH"}}),
		customers=StoreCustomerMap({_STORE: "Walk-in"}),
		tenders=TenderModeMap({"cash": "Cash"}),
		correlation_id="rt458-test",
	)
	return outcome, timeline


class TestFreshSaleCommitsBeforeItsAck:
	def test_posted_commits_after_record_and_read_back_and_before_the_ack(self, load_glue):
		outcome, timeline = _post(load_glue)
		assert outcome == "posted"
		assert timeline == ["savepoint", "insert", "submit", "record_posted", "reload", "commit", "ack:posted"]

	def test_a_reprice_on_submit_commits_before_reconciliation_required(self, load_glue):
		# RT-378: the document stays as ERPNext holds it, so it is durable before Backend-Core hears of it.
		outcome, timeline = _post(load_glue, _Scenario(after_submit={"grand_total": 90.0}))
		assert outcome == "reconciliation_required"
		assert timeline[-3:] == ["reload", "commit", "ack:reconciliation_required"]

	def test_an_unreadable_read_back_commits_before_failed_transient(self, load_glue):
		# The invoice and its Posting Log row exist: a re-offer must find them through the replay guard.
		outcome, timeline = _post(load_glue, _Scenario(unreadable=True))
		assert outcome == "failed_transient"
		assert timeline[-4:] == ["record_posted", "reload", "commit", "ack:failed_transient"]

	def test_a_failing_ack_leaves_the_invoice_committed(self, load_glue):
		# RT-382 Run B: the ack answered 500 and the tick rolled back. The commit has already happened, so
		# the rollback cannot take the invoice; Backend-Core re-offers and the replay guard acks it.
		timeline = []
		with pytest.raises(RuntimeError, match="unexpected ack status 500"):
			_post(load_glue, _Scenario(ack_fails=True), timeline)
		assert timeline[-2:] == ["commit", "ack:posted"]

	def test_a_failing_commit_sends_no_ack(self, load_glue):
		# Nothing durable, nothing acked: the item stays pending in Backend-Core and is re-offered.
		timeline = []
		with pytest.raises(TimeoutError):
			_post(load_glue, _Scenario(commit_error=TimeoutError("Lock wait timeout exceeded")), timeline)
		assert timeline[-1] == "commit" and not any(event.startswith("ack:") for event in timeline)


class TestEveryExistingDocumentPathCommitsBeforeItsAck:
	def test_the_duplicate_provenance_back_fill_commits_before_posted(self, load_glue):
		# G5 crash window: the unique index refused a second invoice; the Posting Log row is back-filled.
		outcome, timeline = _post(load_glue, _Scenario(provenance_hit=True, insert_error="duplicate"))
		assert outcome == "posted"
		assert timeline == ["savepoint", "rollback", "record_posted", "read", "commit", "ack:posted"]

	def test_the_idempotency_conflict_echo_commits_before_posted(self, load_glue):
		outcome, timeline = _post(load_glue, _Scenario(conflict=_EXISTING))
		assert outcome == "posted"
		assert timeline[-4:] == ["record_posted", "read", "commit", "ack:posted"]

	def test_the_replay_guard_commits_before_posted(self, load_glue):
		# RT-458 AC 3 (lost-ack replay): BC re-offers a committed sale whose ack never arrived.
		outcome, timeline = _post(load_glue, _Scenario(existing=_EXISTING))
		assert outcome == "posted"
		assert timeline == ["read", "commit", "ack:posted"]  # no second document


class TestARejectionNeverCommits:
	"""RT-48: a rejected item rolled back to its savepoint, so nothing of it may be committed."""

	@pytest.mark.parametrize(
		"scenario",
		[
			pytest.param(_Scenario(insert_error="validation"), id="validation-error-on-insert"),
			pytest.param(_Scenario(after_insert={"grand_total": 90.0}), id="representation-mismatch-on-insert"),
			pytest.param(_Scenario(insert_error=NameError("name 'flt' is not defined")), id="unclassified-error"),
		],
	)
	def test_a_rolled_back_insert_is_rejected_without_a_commit(self, load_glue, scenario):
		outcome, timeline = _post(load_glue, scenario)
		assert outcome == "permanently_rejected"
		assert "rollback" in timeline and "commit" not in timeline
		assert timeline[-1] == "ack:permanently_rejected"

	def test_a_build_rejection_never_commits(self, load_glue):
		outcome, timeline = _post(load_glue, _Scenario(uom={}))  # the line's unit is unmapped
		assert outcome == "permanently_rejected"
		assert timeline == ["ack:permanently_rejected"]

	def test_a_duplicate_without_a_matching_invoice_is_rejected_without_a_commit(self, load_glue):
		outcome, timeline = _post(load_glue, _Scenario(insert_error="duplicate"))
		assert outcome == "permanently_rejected"
		assert timeline == ["savepoint", "rollback", "ack:permanently_rejected"]

	def test_a_transient_insert_error_is_retried_without_a_commit(self, load_glue):
		outcome, timeline = _post(load_glue, _Scenario(insert_error=TimeoutError("Lock wait timeout")))
		assert outcome == "failed_transient"
		assert timeline == ["savepoint", "rollback", "ack:failed_transient"]
