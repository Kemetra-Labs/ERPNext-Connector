# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-16 — executable coverage of the partial-return leg in the REAL glue (no bench).

``frappe_glue`` imports frappe, so it is loaded against a minimal stand-in (the RT-71 / RT-78
pattern). The stand-in holds one ORIGINAL Sales Invoice (two rows carrying ``rt_line_ref``) and a
Sales Invoice that computes its totals from the payload on insert.

Cases:
  - a ``return`` is routed to the partial-return builder: only the returned row, linked to the
    original row by ``rt_line_ref`` with that row's warehouse, ``return_against`` set, refund paid
    out through the tender map, inserted, verified settled, submitted and acked ``posted``;
  - a return without refundTenders, or with an unmapped refund method, is rejected as
    ``validation`` before anything is built;
  - a return against an original posted before RT-16 (no line refs) is rejected as ``validation``
    and nothing is inserted.
"""

import dataclasses
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
_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
_STORE = "33333333-3333-4333-8333-333333333333"
_ORIGINAL = "ACC-SINV-2026-00100"


class _Logger:
	"""Records ``(level, record)``: RT-409 asserts the level, so the levels are not aliased."""

	def __init__(self):
		self.records = []

	def info(self, record):
		self.records.append(("info", record))

	def warning(self, record):
		self.records.append(("warning", record))

	def error(self, record):
		self.records.append(("error", record))


def _signal(fake, event):
	"""The one ``(level, record)`` logged for ``event``."""
	(found,) = [(level, record) for level, record in fake._logger.records if record["event"] == event]
	return found


class _Invoice:
	def __init__(self, payload, fake):
		self.payload, self._fake, self.name, self.values = payload, fake, "ACC-SINV-2026-00101", {}

	def insert(self):
		grand = sum(Decimal(i["amount"]) for i in self.payload["items"])
		paid = sum(Decimal(p["amount"]) for p in self.payload.get("payments") or [])
		self.values = {"grand_total": float(grand), "paid_amount": float(paid), "change_amount": 0.0,
			"write_off_amount": 0.0, "outstanding_amount": float(grand - paid)}
		self.values.update(self._fake.after_insert)  # RT-378: what ERPNext computed differently
		self._fake.events.append("insert")

	def get(self, field):
		# RT-378: ERPNext holds the payload as sent, on the site default company, plus computed totals.
		return {"company": "RT Company", **self.payload, **self.values}.get(field)

	def reload(self):
		self._fake.events.append("reload")
		self.values.update(self._fake.after_submit)  # RT-378: what submit persisted differently

	def submit(self):
		self._fake.events.append("submit")


class _Frappe(types.ModuleType):
	"""One original invoice: rows A (3 sold) and B (1 sold); ``line_refs=False`` models pre-RT-16."""

	def __init__(self, *, line_refs=True, after_insert=None, after_submit=None):
		super().__init__("frappe")
		self.ValidationError = type("ValidationError", (Exception,), {})
		self.exceptions = types.SimpleNamespace(
			UniqueValidationError=type("UniqueValidationError", (self.ValidationError,), {})
		)
		self._logger = _Logger()
		self.logger = lambda name=None: self._logger
		self.events, self.payloads = [], []
		self.after_insert, self.after_submit = after_insert or {}, after_submit or {}
		self.posted_row = {}  # RT-378 replay: overrides on the already-posted return's row
		self.original_company = "Original Co"
		self.posted_company = "Original Co"  # RT-378 replay: the already-posted return's company
		self._rows = [
			{"name": "row-a", "item_code": "ITEM-A", "qty": 3.0, "idx": 1, "warehouse": "Stores - A",
				"rt_line_ref": _A if line_refs else None, "uom": "Box", "conversion_factor": 12.0},
			{"name": "row-b", "item_code": "ITEM-B", "qty": 1.0, "idx": 2, "warehouse": "Stores - B",
				"rt_line_ref": _B if line_refs else None, "uom": "Nos", "conversion_factor": 1.0},
		]
		self.db = types.SimpleNamespace(
			get_value=self._get_value,
			get_single_value=lambda doctype, field: "RT Company",  # RT-378 site default company
			savepoint=lambda name: self.events.append("savepoint"),
			rollback=lambda save_point=None: self.events.append("rollback"),
		)
		self.utils = types.SimpleNamespace(get_system_timezone=lambda: "UTC")

	def _get_value(self, doctype, filters, fields, **_):
		if isinstance(filters, dict):  # provenance lookups
			if filters.get("rt_external_id") == "POS-9001" and filters.get("docstatus") == 1:
				return _ORIGINAL  # the original sale's invoice
			return None  # no already-posted return (no crash-window recovery)
		assert filters == _ORIGINAL
		if fields == ["posting_date", "posting_time"]:
			return {"posting_date": datetime.date(2026, 6, 1), "posting_time": datetime.timedelta(hours=10)}
		if fields == "company":
			return self.original_company  # RT-378: a return replay is held to the original's company
		return {"update_stock": 1, "disable_rounded_total": 1, "is_pos": 0, "customer": "Original Customer",
			"company": self.original_company}

	def get_all(self, doctype, **kwargs):
		if doctype == "Sales Invoice Item":
			# Return ONLY the requested fields, like frappe: a field the glue stops reading is dropped.
			return [{f: r[f] for f in kwargs["fields"] if f in r} for r in self._rows]
		return []  # Items (no batch/serial) and the original's payments (tender-unknown sale)

	def get_doc(self, payload, name=None):
		if name is not None:  # RT-378 replay: the already-posted return read back from the database
			assert (payload, name) == ("Sales Invoice", _POSTED_RETURN.name)
			return {**_posted_return_doc(**self.posted_row), "company": self.posted_company}
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


def _line(ref, item, qty, amount):
	return {"lineRef": ref, "lineName": item, "unitPrice": "100.00", "currencyCode": "EGP", "quantity": qty,
		"lineAmount": amount, "unit": "each", "erpnextItemRef": {"doctype": "Item", "name": item}}


def _return(refund=(("cash", "100.00"),), tax=None, version=None):
	ref = {"sourceSystem": "pos-pulse", "externalId": "POS-9001", "reversalKind": "return",
		"recordedAt": "2026-06-05T09:30:00Z", "businessDate": "2026-06-05",
		"returnLines": [{"lineRef": _A, "quantity": "1", "lineAmount": "100.00", "taxAmount": tax}]}
	if refund is not None:
		ref["refundTenders"] = [{"method": m, "amount": a} for m, a in refund]
	return c.PostingWorkItem.from_wire({
		"workItemRef": "88888888-8888-4888-8888-888888888888", "kind": "reversal", "sourceSystem": "pos-pulse",
		"externalId": "POS-9001", "payloadHash": "e" * 64, "businessDate": "2026-06-01", "itemCursor": "c-16",
		"reversalOf": ref,
		**({"resolutionVersion": version} if version is not None else {}),
		"sale": {"saleRef": "22222222-2222-4222-8222-222222222222", "storeId": _STORE, "currencyCode": "EGP",
			"posTotal": "400.00", "occurredAt": "2026-06-01T10:00:00Z", "businessDate": "2026-06-01",
			"sourceSystem": "pos-pulse", "externalId": "POS-9001",
			"lines": [_line(_A, "ITEM-A", "3", "300.00"), _line(_B, "ITEM-B", "1", "100.00")]},
	})


def _post(glue, work_item, *, tender_map=None, store=None, uom_map=None, customer_map=None):
	client, store = _Client(), store or _Store()
	outcome = glue.post_work_item(
		work_item,
		client=client,
		store=store,
		uom_map=UomMap({"each": "Nos"} if uom_map is None else uom_map),
		warehouses=PreResolvedWarehouse({_STORE: {"doctype": "Warehouse", "name": "Map WH"}}),
		customers=StoreCustomerMap({_STORE: "Walk-in"} if customer_map is None else customer_map),
		tenders=TenderModeMap({"cash": "Cash"} if tender_map is None else tender_map),
		correlation_id="rt16-test",
	)
	return outcome, client, store


class TestReturnInGlue:
	def test_return_posts_only_the_returned_row_linked_to_the_original(self, load_glue):
		fake = _Frappe()
		outcome, client, store = _post(load_glue(fake), _return())
		assert outcome == "posted" and client.acks[0]["outcome"] == "posted"
		(payload,) = fake.payloads
		assert payload["return_against"] == _ORIGINAL and payload["is_return"] == 1
		assert [(i["item_code"], i["qty"], i["amount"], i["sales_invoice_item"], i["warehouse"], i["uom"]) for i in payload["items"]] == [
			("ITEM-A", "-1", "-100.00", "row-a", "Stores - A", "Box")
		]
		assert payload["customer"] == "Original Customer"  # not the current store map's "Walk-in"
		assert payload["company"] == "Original Co"  # RT-378: the original's, not the site default
		# Greptile PR #51: the glue must READ the original row's conversion factor, not only copy it.
		assert payload["items"][0]["conversion_factor"] == 12.0
		assert payload["payments"] == [{"mode_of_payment": "Cash", "amount": "-100.00"}]
		assert (payload["posting_date"], payload["posting_time"]) == ("2026-06-05", "09:30:00.000000")
		assert fake.events == ["savepoint", "insert", "submit", "reload"] and len(store.writes) == 1

	def test_return_without_refund_tenders_is_rejected_before_building(self, load_glue):
		fake = _Frappe()
		outcome, client, _store = _post(load_glue(fake), _return(refund=None))
		assert outcome == "permanently_rejected" and client.acks[0]["reason"]["category"] == "validation"
		assert fake.payloads == []

	def test_a_taxed_return_line_is_a_validation_rejection(self, load_glue):
		# Greptile PR #50: ReturnTaxNotPosted must map to `validation`, not the generic `other`.
		fake = _Frappe()
		outcome, client, _store = _post(load_glue(fake), _return(tax="14.00"))
		assert outcome == "permanently_rejected" and client.acks[0]["reason"]["category"] == "validation"
		assert fake.payloads == []

	def test_unmapped_refund_method_is_rejected_before_building(self, load_glue):
		fake = _Frappe()
		outcome, client, _store = _post(load_glue(fake), _return(), tender_map={})
		assert outcome == "permanently_rejected" and client.acks[0]["reason"]["category"] == "validation"
		assert fake.payloads == []

	def test_return_against_an_original_without_line_refs_is_rejected(self, load_glue):
		fake = _Frappe(line_refs=False)
		outcome, client, _store = _post(load_glue(fake), _return())
		assert outcome == "permanently_rejected" and client.acks[0]["reason"]["category"] == "validation"
		assert "rt_line_ref" in client.acks[0]["reason"]["message"]
		assert fake.payloads == []


def _repriced_row():
	"""The returned row as ERPNext would hold it after a Pricing Rule cut the rate 100 -> 90."""
	return [{"item_code": "ITEM-A", "qty": -1.0, "uom": "Box", "rate": 90.0, "amount": -90.0,
		"warehouse": "Stores - A", "price_list_rate": 100.0, "discount_amount": 10.0}]


class TestReturnRepresentation:
	"""RT-378: the reversal leg is verified like the sale (negative qty/amount, positive rate)."""

	def test_an_honoured_return_is_read_back_and_posted(self, load_glue):
		fake = _Frappe()
		outcome, _client, _store = _post(load_glue(fake), _return())
		assert outcome == "posted" and "reload" in fake.events

	def test_a_tax_row_after_insert_rolls_back_and_rejects_as_validation(self, load_glue):
		fake = _Frappe(after_insert={"taxes": [{"charge_type": "On Net Total", "tax_amount": -14.0}]})
		outcome, client, store = _post(load_glue(fake), _return())
		assert outcome == "permanently_rejected"
		assert fake.events == ["savepoint", "insert", "rollback"]
		assert client.acks[0]["reason"]["category"] == "validation"
		assert client.acks[0]["reason"]["message"].startswith("RepresentationMismatch: representation:")
		assert store.writes == []

	def test_an_erp_derived_discount_after_insert_rolls_back(self, load_glue):
		# Same rate and total, but ERPNext filled its own list price 110 and recorded a discount (D6).
		row = [{"item_code": "ITEM-A", "qty": -1.0, "uom": "Box", "rate": 100.0, "amount": -100.0,
			"warehouse": "Stores - A", "price_list_rate": 110.0, "discount_amount": 10.0}]
		fake = _Frappe(after_insert={"items": row})
		outcome, client, _store = _post(load_glue(fake), _return())
		assert outcome == "permanently_rejected" and "submit" not in fake.events
		assert "price_list_rate" in client.acks[0]["reason"]["message"]

	def test_a_reprice_on_submit_acks_reconciliation_required(self, load_glue):
		fake = _Frappe(after_submit={"items": _repriced_row(), "grand_total": -90.0})
		outcome, client, store = _post(load_glue(fake), _return())
		assert outcome == "reconciliation_required"
		assert client.acks[0]["documentRef"] == {"doctype": "Sales Invoice", "name": "ACC-SINV-2026-00101"}
		assert client.acks[0]["reason"]["message"].startswith("representation:")
		assert len(store.writes) == 1 and "rollback" not in fake.events


_POSTED_RETURN = c.ErpnextDocumentRef(doctype="Sales Invoice", name="ACC-SINV-2026-00101")


def _posted_return_doc(**row_overrides):
	"""The return as ERPNext holds it: the ORIGINAL row's UOM (Box) and warehouse, and the ORIGINAL
	invoice's customer, none of which the current maps ("Nos", "Map WH", "Walk-in") would give."""
	row = {"item_code": "ITEM-A", "qty": -1.0, "uom": "Box", "rate": 100.0, "amount": -100.0,
		"warehouse": "Stores - A", "rt_line_ref": _A, "price_list_rate": 100.0, "discount_percentage": 0.0,
		"discount_amount": 0.0, "margin_type": "", "margin_rate_or_amount": 0.0, **row_overrides}
	return {"docstatus": 1, "currency": "EGP", "customer": "Original Customer", "company": "Original Co",
		"ignore_pricing_rule": 1, "grand_total": row["amount"], "taxes": [], "items": [row]}


class TestReturnReplay:
	"""RT-378: a return replay rebuilds the expected representation through the reversal builders."""

	def test_an_honoured_return_replay_acks_posted(self, load_glue):
		fake = _Frappe()
		outcome, client, _store = _post(
			load_glue(fake), _return(version=3), store=_Store(existing=_POSTED_RETURN)
		)
		assert outcome == "posted", client.acks
		assert fake.payloads == []  # never a second document

	def test_a_repriced_return_replay_acks_reconciliation_required(self, load_glue):
		fake = _Frappe()
		fake.posted_row = {"rate": 90.0, "amount": -90.0, "price_list_rate": 90.0}
		outcome, client, _store = _post(
			load_glue(fake), _return(version=3), store=_Store(existing=_POSTED_RETURN)
		)
		assert outcome == "reconciliation_required"
		assert client.acks[0]["reason"]["message"].startswith("representation:")

	def test_a_return_replay_does_not_resolve_its_exempt_uom_and_customer(self, load_glue):
		# CodeRabbit PR #66: the return takes UOM and customer from the original invoice, so a unit or
		# store mapping removed since then must not turn a matching replay into reconciliation_required.
		fake = _Frappe()
		outcome, client, _store = _post(
			load_glue(fake),
			_return(version=3),
			store=_Store(existing=_POSTED_RETURN),
			uom_map={},
			customer_map={},
		)
		assert outcome == "posted", client.acks


class TestReturnCompany:
	"""RT-378 review finding 2: a credit note is held to the ORIGINAL invoice's company."""

	def test_an_original_without_a_company_is_rejected_before_insert(self, load_glue):
		fake = _Frappe()
		fake.original_company = None
		outcome, client, _store = _post(load_glue(fake), _return())
		assert outcome == "permanently_rejected" and fake.payloads == []
		assert client.acks[0]["reason"]["category"] == "validation"

	def test_a_replayed_return_on_another_company_acks_reconciliation_required(self, load_glue):
		fake = _Frappe()
		fake.posted_company = "RT Company"  # the site default, not the original's company
		outcome, client, _store = _post(
			load_glue(fake), _return(version=3), store=_Store(existing=_POSTED_RETURN)
		)
		assert outcome == "reconciliation_required"
		assert "company" in client.acks[0]["reason"]["message"]

	def test_a_replayed_return_whose_original_has_no_company_cannot_be_verified(self, load_glue):
		fake = _Frappe()
		fake.original_company = None
		outcome, _client, _store = _post(
			load_glue(fake), _return(version=3), store=_Store(existing=_POSTED_RETURN)
		)
		assert outcome == "failed_transient"


class TestReversalRejectionReasonAttribution:
	"""RT-409 (AD-POSTING-001 D-7): the reversal leg names the failing check and logs it at ERROR."""

	def test_an_unclassified_error_is_prefixed_with_its_class(self, load_glue, monkeypatch):
		def _raise(self):
			raise NameError("name 'flt' is not defined")

		monkeypatch.setattr(_Invoice, "insert", _raise)
		fake = _Frappe()
		outcome, client, store = _post(load_glue(fake), _return())
		assert outcome == "permanently_rejected"
		assert client.acks[0]["reason"] == {"category": "other", "message": "NameError: name 'flt' is not defined"}
		assert "rollback" in fake.events and store.writes == []

	def test_a_build_rejection_is_prefixed_and_logged_at_error(self, load_glue):
		fake = _Frappe()
		_outcome, client, _store = _post(load_glue(fake), _return(), tender_map={})
		message = client.acks[0]["reason"]["message"]
		assert message.startswith("UnmappedTender: ")
		level, record = _signal(fake, "posting.rejected")
		assert level == "error"
		assert record["category"] == "validation" and record["reason_message"] == message

	def test_a_literal_reason_is_not_prefixed(self, load_glue):
		fake = _Frappe()
		_outcome, client, _store = _post(load_glue(fake), dataclasses.replace(_return(), reversal_of=None))
		assert client.acks[0]["reason"]["message"] == "reversal work-item carries no reversalOf (return_against unresolvable)"

	def test_reconciliation_required_is_logged_at_error_with_the_wire_reason(self, load_glue):
		fake = _Frappe(after_submit={"items": _repriced_row(), "grand_total": -90.0})
		_outcome, client, _store = _post(load_glue(fake), _return())
		level, record = _signal(fake, "posting.reconciliation_required")
		assert level == "error"
		assert record["category"] == "validation"
		assert record["reason_message"] == client.acks[0]["reason"]["message"]
