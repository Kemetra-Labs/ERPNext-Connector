# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-171 — the ``failed_transient`` ack Idempotency-Key is per OFFER, in the REAL glue (no bench).

Backend-Core's ack route is ``@Idempotent``: a stored key is REPLAYED without running the handler.
With the old ``{workItemRef}:failed_transient`` key, the SECOND transient of one work-item was
replayed, the row was never re-headed, and it stayed pending forever. The transient key now carries
the offer's ``itemCursor`` (DP2 issues a new one per re-offer), so each re-offer's ack reaches the
handler while a resend of one offer still dedupes. ``posted`` / ``permanently_rejected`` keys are
byte-identical to before.

``frappe_glue`` imports frappe, so it is loaded against a minimal stand-in (the RT-71 / RT-78
pattern). The stand-in's Sales Invoice raises ``TimeoutError`` (always a transient) on insert while
``fake.transient`` is set, and the stand-in can hold an ORIGINAL invoice for the reversal leg.

RT-172 — a reversal whose original sale has no submitted invoice YET acks ``failed_transient`` with
that per-offer key (signal ``posting.reversal.awaiting_original``), inserts nothing, and posts with
``return_against`` once the original exists. It never acks ``validation`` for that case: DP2's
retry budget ends it as ``retry_budget_exhausted``. A reversal with no ``reversalOf`` at all is still
``permanently_rejected / validation``.
"""

import datetime
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
_SALE_REF = "17171717-1717-4171-8171-171717171717"
_REVERSAL_REF = "17272727-2727-4272-8272-272727272727"
_ORIGINAL_EXTERNAL_ID = "POS-17100"
_ORIGINAL = "ACC-SINV-2026-17100"


class _Logger:
	def __init__(self):
		self.records = []

	def info(self, record):
		self.records.append(record)

	warning = error = info


class _Invoice:
	def __init__(self, payload, fake):
		self.payload, self._fake, self.name, self.values = payload, fake, "ACC-SINV-2026-17999", {}

	def insert(self):
		if self._fake.transient:
			raise TimeoutError("Lock wait timeout exceeded")
		grand = sum(Decimal(i["amount"]) for i in self.payload["items"])
		paid = sum(Decimal(p["amount"]) for p in self.payload.get("payments") or [])
		self.values = {"grand_total": float(grand), "paid_amount": float(paid), "change_amount": 0.0,
			"write_off_amount": 0.0, "outstanding_amount": float(grand - paid)}
		self._fake.events.append("insert")

	def get(self, field):
		# RT-378: ERPNext holds the payload as sent, on the site default company, plus computed totals.
		return {"company": "RT Company", **self.payload, **self.values}.get(field)

	def reload(self):
		pass  # RT-378 read-back after submit: the database holds what insert computed

	def submit(self):
		self._fake.events.append("submit")


class _Frappe(types.ModuleType):
	"""``originals`` maps a sale's ``externalId`` to its submitted forward invoice (mutable)."""

	def __init__(self, *, transient=False, originals=None):
		super().__init__("frappe")
		self.ValidationError = type("ValidationError", (Exception,), {})
		self.exceptions = types.SimpleNamespace(
			UniqueValidationError=type("UniqueValidationError", (self.ValidationError,), {})
		)
		self._logger = _Logger()
		self.logger = lambda name=None: self._logger
		self.events, self.payloads = [], []
		self.transient = transient
		self.originals = dict(originals or {})
		self.db = types.SimpleNamespace(
			get_value=self._get_value,
			get_single_value=lambda doctype, field: "RT Company",  # RT-378 site default company
			savepoint=lambda name: self.events.append("savepoint"),
			rollback=lambda save_point=None: self.events.append("rollback"),
		)
		self.utils = types.SimpleNamespace(get_system_timezone=lambda: "UTC")

	def _get_value(self, doctype, filters, fields, **_):
		if isinstance(filters, dict):  # provenance lookups (original / crash-window recovery)
			if filters.get("docstatus") == 1:
				return self.originals.get(filters.get("rt_external_id"))
			return None
		assert filters == _ORIGINAL
		if fields == ["posting_date", "posting_time"]:
			return {"posting_date": datetime.date(2026, 6, 1), "posting_time": datetime.timedelta(hours=10)}
		if fields == "company":
			return "Original Co"  # RT-378: a reversal replay is held to the original's company
		return {"update_stock": 1, "disable_rounded_total": 1, "is_pos": 0, "customer": "Original Customer",
			"company": "Original Co"}

	def get_all(self, doctype, **kwargs):
		if doctype == "Sales Invoice Item":
			row = {"name": "row-a", "item_code": "ITEM-A", "qty": 1.0, "idx": 1, "warehouse": "Stores - A",
				"rt_line_ref": _A, "uom": "Nos", "conversion_factor": 1.0}
			return [{f: row[f] for f in kwargs["fields"] if f in row}]
		return []  # Items (no batch/serial) and the original's payments (unpaid original)

	def get_doc(self, payload):
		self.payloads.append(payload)
		return _Invoice(payload, self)


class _Client:
	def __init__(self):
		self.acks = []

	def ack_outcome(self, ref, request, idempotency_key):
		self.acks.append({"ref": ref, "ack": request.to_wire(), "key": idempotency_key})


class _Store:
	def __init__(self):
		self.writes = []

	def get_document_ref(self, key):
		return None

	def record_posted(self, key, ref):
		self.writes.append((key, ref))


def _restore(namespace, name, value):
	namespace.pop(name, None)
	if value is not _MISSING:
		namespace[name] = value


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
		_restore(namespace, name, value)


def _sale(external_id):
	return {"saleRef": "22222222-2222-4222-8222-222222222222", "storeId": _STORE, "currencyCode": "EGP",
		"posTotal": "100.00", "occurredAt": "2026-06-01T10:00:00Z", "businessDate": "2026-06-01",
		"sourceSystem": "pos-pulse", "externalId": external_id,
		"lines": [{"lineRef": _A, "lineName": "Item A", "unitPrice": "100.00", "currencyCode": "EGP",
			"quantity": "1", "lineAmount": "100.00", "unit": "each",
			"erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"}}]}


def _sale_post(cursor, *, unit="each"):
	sale = _sale("POS-17101")
	sale["lines"][0]["unit"] = unit
	return c.PostingWorkItem.from_wire({
		"workItemRef": _SALE_REF, "kind": "sale_post", "sourceSystem": "pos-pulse", "externalId": "POS-17101",
		"payloadHash": "a" * 64, "businessDate": "2026-06-01", "itemCursor": str(cursor), "sale": sale,
	})


def _reversal(kind, cursor):
	ref = {"sourceSystem": "pos-pulse", "externalId": _ORIGINAL_EXTERNAL_ID, "reversalKind": kind,
		"recordedAt": "2026-06-05T09:30:00Z", "businessDate": "2026-06-05"}
	if kind == "return":
		ref["returnLines"] = [{"lineRef": _A, "quantity": "1", "lineAmount": "100.00"}]
		ref["refundTenders"] = [{"method": "cash", "amount": "100.00"}]
	return c.PostingWorkItem.from_wire({
		"workItemRef": _REVERSAL_REF, "kind": "reversal", "sourceSystem": "pos-pulse",
		"externalId": _ORIGINAL_EXTERNAL_ID, "payloadHash": "b" * 64, "businessDate": "2026-06-01",
		"itemCursor": str(cursor), "reversalOf": ref, "sale": _sale(_ORIGINAL_EXTERNAL_ID),
	})


def _post(glue, work_item, client=None, store=None):
	client, store = client or _Client(), store or _Store()
	outcome = glue.post_work_item(
		work_item,
		client=client,
		store=store,
		uom_map=UomMap({"each": "Nos"}),
		warehouses=PreResolvedWarehouse({_STORE: {"doctype": "Warehouse", "name": "Map WH"}}),
		customers=StoreCustomerMap({_STORE: "Walk-in"}),
		tenders=TenderModeMap({"cash": "Cash"}),
		correlation_id="rt171-test",
	)
	return outcome, client, store


class TestTransientAckKeyPerOffer:
	def test_sale_post_transients_at_different_cursors_get_different_keys_then_posts(self, load_glue):
		fake = _Frappe(transient=True)
		glue = load_glue(fake)
		client = _Client()

		assert _post(glue, _sale_post(10), client)[0] == "failed_transient"
		assert _post(glue, _sale_post(12), client)[0] == "failed_transient"
		fake.transient = False
		assert _post(glue, _sale_post(14), client)[0] == "posted"

		assert [a["key"] for a in client.acks] == [
			f"{_SALE_REF}:failed_transient:10",
			f"{_SALE_REF}:failed_transient:12",
			f"{_SALE_REF}:posted",
		]
		assert [a["ack"]["outcome"] for a in client.acks] == ["failed_transient", "failed_transient", "posted"]
		assert fake.events.count("submit") == 1  # nothing half-posted by the transients
		assert fake.events.count("rollback") == 2

	def test_a_resend_of_one_offer_reuses_its_key(self, load_glue):
		glue = load_glue(_Frappe(transient=True))
		client = _Client()
		_post(glue, _sale_post(10), client)
		_post(glue, _sale_post(10), client)  # the same offer, re-processed (e.g. a dropped response)
		assert client.acks[0]["key"] == client.acks[1]["key"] == f"{_SALE_REF}:failed_transient:10"

	def test_reversal_transient_key_carries_the_offer_cursor(self, load_glue):
		fake = _Frappe(transient=True, originals={_ORIGINAL_EXTERNAL_ID: _ORIGINAL})
		glue = load_glue(fake)
		client = _Client()

		assert _post(glue, _reversal("void", 20), client)[0] == "failed_transient"
		assert _post(glue, _reversal("void", 22), client)[0] == "failed_transient"
		fake.transient = False
		assert _post(glue, _reversal("void", 24), client)[0] == "posted"

		assert [a["key"] for a in client.acks] == [
			f"{_REVERSAL_REF}:failed_transient:20",
			f"{_REVERSAL_REF}:failed_transient:22",
			f"{_REVERSAL_REF}:posted",
		]
		assert fake.payloads[-1]["return_against"] == _ORIGINAL


class TestTerminalAckKeysUnchanged:
	"""``posted`` and ``permanently_rejected`` happen at most once — their keys stay byte-identical."""

	def test_posted_key(self, load_glue):
		_outcome, client, _store = _post(load_glue(_Frappe()), _sale_post(10))
		assert client.acks[0]["key"] == f"{_SALE_REF}:posted"

	def test_permanently_rejected_key(self, load_glue):
		_outcome, client, _store = _post(load_glue(_Frappe()), _sale_post(10, unit="crate"))
		assert client.acks[0]["ack"]["outcome"] == "permanently_rejected"
		assert client.acks[0]["key"] == f"{_SALE_REF}:permanently_rejected"


class TestReversalAwaitsOriginal:
	@pytest.mark.parametrize("kind", ["void", "return"])
	def test_reversal_before_its_original_waits_then_posts_against_it(self, load_glue, kind):
		fake = _Frappe()  # the original sale has no submitted invoice yet
		glue = load_glue(fake)
		client = _Client()

		assert _post(glue, _reversal(kind, 11), client)[0] == "failed_transient"
		assert client.acks == [{"ref": _REVERSAL_REF, "ack": {"outcome": "failed_transient"},
			"key": f"{_REVERSAL_REF}:failed_transient:11"}]
		assert fake.payloads == [] and fake.events == []  # nothing built into ERPNext, no insert
		(signal,) = [r for r in fake._logger.records if r["event"] == "posting.reversal.awaiting_original"]
		assert signal["work_item_ref"] == _REVERSAL_REF and signal["reversal_kind"] == kind

		fake.originals[_ORIGINAL_EXTERNAL_ID] = _ORIGINAL  # the original sale posts
		assert _post(glue, _reversal(kind, 13), client)[0] == "posted"
		assert client.acks[1]["key"] == f"{_REVERSAL_REF}:posted"
		(payload,) = fake.payloads
		assert payload["return_against"] == _ORIGINAL and payload["is_return"] == 1
		assert fake.events == ["savepoint", "insert", "submit"]

	def test_never_acks_validation_while_waiting(self, load_glue):
		# DP2 owns the budget (POSTING_RETRY_BUDGET): every offer the connector sees is a transient,
		# each under its own key, so the end state is retry_budget_exhausted — never validation.
		fake = _Frappe()
		glue = load_glue(fake)
		client = _Client()
		cursors = [11, 13, 15, 17, 19]
		for cursor in cursors:
			assert _post(glue, _reversal("void", cursor), client)[0] == "failed_transient"
		assert {a["ack"]["outcome"] for a in client.acks} == {"failed_transient"}
		assert [a["key"] for a in client.acks] == [f"{_REVERSAL_REF}:failed_transient:{n}" for n in cursors]
		assert fake.payloads == []

	def test_reversal_without_reversal_of_is_still_a_validation_rejection(self, load_glue):
		wi = _reversal("void", 11)
		malformed = c.PostingWorkItem(
			work_item_ref=wi.work_item_ref,
			kind=wi.kind,
			source_system=wi.source_system,
			external_id=wi.external_id,
			payload_hash=wi.payload_hash,
			business_date=wi.business_date,
			sale=wi.sale,
			item_cursor=wi.item_cursor,
			reversal_of=None,
		)
		fake = _Frappe(originals={_ORIGINAL_EXTERNAL_ID: _ORIGINAL})
		outcome, client, _store = _post(load_glue(fake), malformed)
		assert outcome == "permanently_rejected"
		assert client.acks[0]["ack"]["reason"]["category"] == "validation"
		assert client.acks[0]["key"] == f"{_REVERSAL_REF}:permanently_rejected"
		assert fake.payloads == []
