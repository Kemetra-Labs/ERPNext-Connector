# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-452 — executable coverage of explicit-batch posting in the REAL glue (no bench).

``frappe_glue`` imports frappe, so it is loaded against a minimal stand-in (the RT-16 / RT-78
pattern). The stand-in holds the Item flags, the Batch master, one ORIGINAL batch sale (its row
``row-a`` posted Batch ``B1`` through bundle ``SABB-0``) and what ERPNext posts on submit: the new
invoice's rows, its Stock Ledger Entries and its bundle entries.

Cases (RT-452 acceptance, unit level; the bench evidence is separate):
  - a batch sale posts its row with ``batch_no`` + ``use_serial_batch_fields``, is verified after submit
    and acked ``posted``; another Batch or quantity after submit rolls back and is ``validation``;
  - ERPNext's batch refusals (expired, negative batch stock) roll back and are ``validation``;
  - a missing ref, a flag mismatch, a Batch of another Item and a serial Item are ``validation``
    before anything is inserted; a replay inserts nothing;
  - a return row carries no Batch, takes the original's and is verified inward; an original row
    without a bundle fails closed; a rebuilt bundle on another Batch rolls back.
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
_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_STORE = "33333333-3333-4333-8333-333333333333"
_ORIGINAL = "ACC-SINV-2026-00100"
_NEW = "ACC-SINV-2026-00101"
_BATCH = {"name": "ITEM-A", "has_batch_no": 1, "has_serial_no": 0}


class _Logger:
	def __init__(self):
		self.records = []

	def info(self, record):
		self.records.append(("info", record))

	def warning(self, record):
		self.records.append(("warning", record))

	def error(self, record):
		self.records.append(("error", record))


class _Invoice:
	def __init__(self, payload, fake):
		self.payload, self._fake, self.name, self.values = payload, fake, _NEW, {}

	def insert(self):
		grand = sum(Decimal(i["amount"]) for i in self.payload["items"])
		paid = sum(Decimal(p["amount"]) for p in self.payload.get("payments") or [])
		self.values = {"grand_total": float(grand), "paid_amount": float(paid), "change_amount": 0.0,
			"write_off_amount": 0.0, "outstanding_amount": float(grand - paid)}
		self._fake.events.append("insert")

	def get(self, field):
		return {"company": "RT Company", **self.payload, **self.values}.get(field)

	def reload(self):
		self._fake.events.append("reload")

	def submit(self):
		self._fake.events.append("submit")
		if self._fake.submit_error is not None:
			raise self._fake.submit_error


class _Frappe(types.ModuleType):
	"""ERPNext as the glue reads it. ``posted`` is what submit wrote for row 1 (Batch, stock qty)."""

	def __init__(self, *, flags=_BATCH, batch_item="ITEM-A", original_bundle="SABB-0", posted=("B1", None)):
		super().__init__("frappe")
		self.ValidationError = type("ValidationError", (Exception,), {})
		self.exceptions = types.SimpleNamespace(
			UniqueValidationError=type("UniqueValidationError", (self.ValidationError,), {})
		)
		# ERPNext v16: erpnext.exceptions.BatchExpiredError and the SABB BatchNegativeStockError are
		# both frappe.ValidationError subclasses.
		self.BatchExpiredError = type("BatchExpiredError", (self.ValidationError,), {})
		self.BatchNegativeStockError = type("BatchNegativeStockError", (self.ValidationError,), {})
		self._logger = _Logger()
		self.logger = lambda name=None: self._logger
		self.events, self.payloads, self.reads = [], [], []
		self.flags, self.batch_item, self.original_bundle, self.posted = flags, batch_item, original_bundle, posted
		self.submit_error = None
		self.db = types.SimpleNamespace(
			get_value=self._get_value,
			get_single_value=lambda doctype, field: "RT Company",
			savepoint=lambda name: self.events.append("savepoint"),
			rollback=lambda save_point=None: self.events.append("rollback"),
			commit=lambda: self.events.append("commit"),
		)
		self.utils = types.SimpleNamespace(get_system_timezone=lambda: "UTC")

	def _get_value(self, doctype, filters, fields, **_):
		if isinstance(filters, dict):  # provenance lookups: only the original sale is posted
			if filters.get("rt_external_id") == "POS-9001" and filters.get("docstatus") == 1:
				return _ORIGINAL
			return None
		assert filters == _ORIGINAL
		if fields == ["posting_date", "posting_time"]:
			return {"posting_date": datetime.date(2026, 6, 1), "posting_time": datetime.timedelta(hours=10)}
		return {"update_stock": 1, "disable_rounded_total": 1, "is_pos": 0, "customer": "Original Customer",
			"company": "RT Company"}

	def get_all(self, doctype, filters=None, fields=None, **_):
		self.reads.append(doctype)
		filters = filters or {}
		if doctype == "Item":
			return [dict(self.flags)]
		if doctype == "Batch":
			return [{"name": "B1", "item": self.batch_item}]
		if doctype == "Sales Invoice Item":
			return self._invoice_rows(filters)
		if doctype == "Stock Ledger Entry":
			return [{"voucher_detail_no": "new-row-1", "actual_qty": self._moved(), "serial_and_batch_bundle": "SABB-1"}]
		if doctype == "Serial and Batch Entry":
			wanted = filters["parent"][1]
			entries = [{"parent": "SABB-0", "batch_no": "B1", "qty": -2.0}] if "SABB-0" in wanted else []
			if "SABB-1" in wanted:  # the bundle submit wrote (read only after submit)
				entries.append({"parent": "SABB-1", "batch_no": self.posted[0], "qty": self._moved()})
			return entries
		return []  # the original's payments (a tender-unknown sale)

	def _invoice_rows(self, filters):
		if "name" in filters:  # the original batch rows' bundles (G7-10)
			return [{"name": "row-a", "serial_and_batch_bundle": self.original_bundle}]
		if filters.get("parent") == _ORIGINAL:
			return [{"name": "row-a", "item_code": "ITEM-A", "qty": 2.0, "idx": 1, "warehouse": "Stores - A",
				"rt_line_ref": _A, "uom": "Nos", "conversion_factor": 1.0}]
		assert filters.get("parent") == _NEW  # the submitted invoice, read back for verification
		stock_qty = float(self.payloads[-1]["items"][0]["qty"])  # the row as sent: + for a sale, - for a credit note
		return [{"name": "new-row-1", "idx": 1, "stock_qty": stock_qty, "serial_and_batch_bundle": "SABB-1"}]

	def _moved(self):
		"""The signed stock quantity submit moved for row 1 (out for a sale, in for a credit note)."""
		payload = self.payloads[-1]
		qty = float(payload["items"][0]["qty"])
		return self.posted[1] if self.posted[1] is not None else -qty

	def get_doc(self, payload, name=None):
		assert name is None
		self.payloads.append(payload)
		return _Invoice(payload, self)


class _Client:
	def __init__(self):
		self.acks = []

	def ack_outcome(self, ref, request, idempotency_key):
		self.acks.append(request.to_wire())


class _Store:
	def __init__(self, existing=None):
		self.writes, self.existing = [], existing

	def get_document_ref(self, key):
		return self.existing

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


_B1 = {"doctype": "Batch", "name": "B1"}


def _line(**tracking):
	return {"lineRef": _A, "lineName": "ITEM-A", "unitPrice": "100.00", "currencyCode": "EGP", "quantity": "2",
		"lineAmount": "200.00", "unit": "each", "erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"}, **tracking}


def _sale_wire(**tracking):
	return {"saleRef": "22222222-2222-4222-8222-222222222222", "storeId": _STORE, "currencyCode": "EGP",
		"posTotal": "200.00", "occurredAt": "2026-06-01T10:00:00Z", "businessDate": "2026-06-01",
		"sourceSystem": "pos-pulse", "externalId": "POS-9001", "lines": [_line(**tracking)]}


def _sale(**tracking):
	tracking = tracking or {"trackingMode": "batch", "erpnextBatchRef": _B1}
	return c.PostingWorkItem.from_wire({
		"workItemRef": "11111111-1111-4111-8111-111111111111", "kind": "sale_post", "sourceSystem": "pos-pulse",
		"externalId": "POS-9001", "payloadHash": "a" * 64, "businessDate": "2026-06-01", "itemCursor": "c-1",
		"sale": _sale_wire(**tracking),
	})


def _return(**tracking):
	tracking = tracking or {"trackingMode": "batch", "erpnextBatchRef": _B1}
	return c.PostingWorkItem.from_wire({
		"workItemRef": "88888888-8888-4888-8888-888888888888", "kind": "reversal", "sourceSystem": "pos-pulse",
		"externalId": "POS-9001", "payloadHash": "e" * 64, "businessDate": "2026-06-01", "itemCursor": "c-2",
		"reversalOf": {"sourceSystem": "pos-pulse", "externalId": "POS-9001", "reversalKind": "return",
			"recordedAt": "2026-06-05T09:30:00Z", "businessDate": "2026-06-05",
			"returnLines": [{"lineRef": _A, "quantity": "1", "lineAmount": "100.00"}],
			"refundTenders": [{"method": "cash", "amount": "100.00"}]},
		"sale": _sale_wire(**tracking),
	})


def _post(glue, work_item, *, store=None):
	client, store = _Client(), store or _Store()
	outcome = glue.post_work_item(
		work_item,
		client=client,
		store=store,
		uom_map=UomMap({"each": "Nos"}),
		warehouses=PreResolvedWarehouse({_STORE: {"doctype": "Warehouse", "name": "Map WH"}}),
		customers=StoreCustomerMap({_STORE: "Walk-in"}),
		tenders=TenderModeMap({"cash": "Cash"}),
		correlation_id="rt452-test",
	)
	return outcome, client, store


def _rejected(client, outcome, prefix):
	assert outcome == "permanently_rejected"
	(ack,) = client.acks
	assert ack["reason"]["category"] == "validation"
	assert ack["reason"]["message"].startswith(prefix)


class TestBatchSale:
	def test_a_batch_sale_posts_its_batch_explicitly_and_is_verified(self, load_glue):
		fake = _Frappe()
		outcome, client, store = _post(load_glue(fake), _sale())
		assert outcome == "posted" and client.acks[0]["outcome"] == "posted"
		(payload,) = fake.payloads
		(item,) = payload["items"]
		assert (item["batch_no"], item["use_serial_batch_fields"]) == ("B1", 1)
		assert fake.events == ["savepoint", "insert", "submit", "reload", "commit"]
		assert {"Batch", "Stock Ledger Entry", "Serial and Batch Entry"} <= set(fake.reads)
		assert len(store.writes) == 1

	def test_another_batch_after_submit_rolls_back_and_is_validation(self, load_glue):
		fake = _Frappe(posted=("B2", None))
		outcome, client, store = _post(load_glue(fake), _sale())
		_rejected(client, outcome, "BatchPostingMismatch: row 1: ERPNext posted Batch 'B2'")
		assert fake.events == ["savepoint", "insert", "submit", "rollback"] and store.writes == []

	def test_another_quantity_after_submit_rolls_back_and_is_validation(self, load_glue):
		fake = _Frappe(posted=("B1", -1.0))
		outcome, client, _store = _post(load_glue(fake), _sale())
		_rejected(client, outcome, "BatchPostingMismatch:")
		assert fake.events[-1] == "rollback"

	@pytest.mark.parametrize("error", ["BatchExpiredError", "BatchNegativeStockError"])
	def test_an_erpnext_batch_refusal_rolls_back_and_is_validation(self, load_glue, error):
		fake = _Frappe()
		fake.submit_error = getattr(fake, error)("Row #1: The batch B1 has already expired.")
		outcome, client, store = _post(load_glue(fake), _sale())
		_rejected(client, outcome, f"{error}: Row #1")
		assert fake.events == ["savepoint", "insert", "submit", "rollback"] and store.writes == []

	@pytest.mark.parametrize(
		"tracking",
		[
			{"trackingMode": "batch", "erpnextBatchRef": None},  # (f) a missing ref
			{"trackingMode": "none", "erpnextBatchRef": None},  # the Item flag disagrees
			{"lineRef": _A},  # an older feed without the fields: fail closed as before
		],
	)
	def test_a_batch_item_without_its_ref_is_rejected_before_insert(self, load_glue, tracking):
		fake = _Frappe()
		outcome, client, _store = _post(load_glue(fake), _sale(**tracking))
		_rejected(client, outcome, "TrackedLineRejected: row 1 Item 'ITEM-A'")
		assert fake.payloads == [] and fake.events == []

	def test_a_ref_on_an_untracked_item_is_rejected_before_insert(self, load_glue):
		fake = _Frappe(flags={"name": "ITEM-A", "has_batch_no": 0, "has_serial_no": 0})
		outcome, client, _store = _post(load_glue(fake), _sale())
		_rejected(client, outcome, "TrackedLineRejected:")
		assert fake.payloads == []

	def test_a_batch_of_another_item_is_rejected_before_insert(self, load_glue):
		fake = _Frappe(batch_item="ITEM-Z")
		outcome, client, _store = _post(load_glue(fake), _sale())
		_rejected(client, outcome, "TrackedLineRejected: row 1: Batch 'B1' belongs to Item 'ITEM-Z'")
		assert fake.payloads == []

	def test_a_serial_item_stays_fail_closed(self, load_glue):
		fake = _Frappe(flags={"name": "ITEM-A", "has_batch_no": 0, "has_serial_no": 1})
		outcome, client, _store = _post(load_glue(fake), _sale())
		_rejected(client, outcome, "UnsupportedTrackedItem:")
		assert fake.payloads == []

	def test_an_untracked_sale_reads_no_batch_evidence(self, load_glue):
		fake = _Frappe(flags={"name": "ITEM-A", "has_batch_no": 0, "has_serial_no": 0})
		outcome, _client, _store = _post(load_glue(fake), _sale(trackingMode="none", erpnextBatchRef=None))
		assert outcome == "posted"
		assert "batch_no" not in fake.payloads[0]["items"][0]
		assert set(fake.reads) == {"Item"}

	def test_a_replay_inserts_nothing(self, load_glue):
		# (b) a replay adds no Stock Ledger Entry: the Posting Log replay guard answers before any build.
		fake = _Frappe()
		existing = c.ErpnextDocumentRef(doctype="Sales Invoice", name=_NEW)
		glue = load_glue(fake)
		glue._existing_mismatch = lambda work_item, document_ref: None
		outcome, _client, _store = _post(glue, _sale(), store=_Store(existing=existing))
		assert outcome == "posted" and fake.payloads == [] and "insert" not in fake.events


class TestBatchReturn:
	def test_a_return_takes_the_original_batch_and_is_verified_inward(self, load_glue):
		fake = _Frappe()
		outcome, client, _store = _post(load_glue(fake), _return())
		assert outcome == "posted" and client.acks[0]["outcome"] == "posted"
		(payload,) = fake.payloads
		(item,) = payload["items"]
		assert item["sales_invoice_item"] == "row-a" and item["qty"] == "-1"
		assert "batch_no" not in item and "use_serial_batch_fields" not in item
		assert fake.events == ["savepoint", "insert", "submit", "reload", "commit"]
		assert "Stock Ledger Entry" in fake.reads

	def test_an_original_row_without_a_bundle_fails_closed(self, load_glue):
		fake = _Frappe(original_bundle=None)
		outcome, client, _store = _post(load_glue(fake), _return())
		_rejected(client, outcome, "ReturnLineMismatch: credit note row 1: the original row has no Serial")
		assert fake.payloads == []

	def test_a_rebuilt_bundle_on_another_batch_rolls_back(self, load_glue):
		fake = _Frappe(posted=("B2", None))
		outcome, client, _store = _post(load_glue(fake), _return())
		_rejected(client, outcome, "BatchPostingMismatch:")
		assert fake.events[-1] == "rollback"

	def test_a_return_whose_feed_batch_is_not_the_original_fails_closed(self, load_glue):
		fake = _Frappe()
		work_item = _return(trackingMode="batch", erpnextBatchRef={"doctype": "Batch", "name": "B9"})
		outcome, client, _store = _post(load_glue(fake), work_item)
		_rejected(client, outcome, "ReturnLineMismatch:")
		assert fake.payloads == []
