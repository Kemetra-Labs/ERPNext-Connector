# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt
"""RT-378 (RT-283 F3) — the real glue verifies the persisted representation, no bench.

* **Before submit** (after ``insert()``, inside the savepoint): a mismatch rolls back, nothing is
  submitted, and the sale is acked ``permanently_rejected`` / ``validation`` with a
  ``representation:`` reason (D1).
* **After submit**: the invoice is read back from the database (``reload``) before acking. A
  mismatch acks ``reconciliation_required`` with the documentRef; the Posting Log keeps the
  document and the Connector never cancels or amends it (684). An unreadable invoice acks
  ``failed_transient`` (never a blind ``posted``).

The stand-in Sales Invoice keeps two views: the in-memory document (what ``insert`` computed) and
the database row (what ``submit`` persisted). Only ``reload`` copies the database view into the
document, so a glue that skipped the read-back would miss a reprice-on-submit.
"""

import copy
import importlib
import sys
import types

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
_REF = "37837837-8378-4378-8378-378378378378"
_COMPANY = "RT Company"
_NAME = "ACC-SINV-2026-37800"


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


def _erp_view(payload):
	"""What ERPNext would hold for ``payload`` when it honours it exactly (floats, like Frappe)."""
	items = [
		{
			"item_code": i["item_code"],
			"qty": float(i["qty"]),
			"uom": i["uom"],
			"rate": float(i["rate"]),
			"amount": float(i["amount"]),
			"warehouse": i["warehouse"],
			"price_list_rate": float(i["price_list_rate"]),
			"discount_percentage": 0.0,
			"discount_amount": 0.0,
			"margin_type": "",
			"margin_rate_or_amount": 0.0,
		}
		for i in payload["items"]
	]
	grand = sum(i["amount"] for i in items)
	paid = sum(float(p["amount"]) for p in payload.get("payments") or [])
	return {
		"docstatus": 0,
		"currency": payload["currency"],
		"customer": payload["customer"],
		"company": payload.get("company"),
		"ignore_pricing_rule": payload.get("ignore_pricing_rule"),
		"grand_total": grand,
		"paid_amount": paid,
		"change_amount": 0.0,
		"write_off_amount": 0.0,
		"outstanding_amount": grand - paid,
		"taxes": [],
		"items": items,
	}


class _Invoice:
	def __init__(self, payload, fake):
		self.payload, self._fake, self.name = payload, fake, _NAME
		self.doc, self.db_row = {}, {}

	def insert(self):
		self.doc = _erp_view(self.payload)
		self._fake.on_insert(self.doc)
		self.db_row = copy.deepcopy(self.doc)
		self._fake.events.append("insert")

	def get(self, field):
		return self.doc.get(field)

	def submit(self):
		self.doc["docstatus"] = 1
		self.db_row = copy.deepcopy(self.doc)
		self._fake.on_submit(self.db_row)  # persisted only: visible after reload
		self._fake.events.append("submit")

	def reload(self):
		self._fake.events.append("reload")
		if self._fake.unreadable:
			raise TimeoutError("Lock wait timeout exceeded")
		self.doc = copy.deepcopy(self.db_row)

	def cancel(self):
		self._fake.events.append("cancel")


class _Frappe(types.ModuleType):
	def __init__(self, *, on_insert=None, on_submit=None, company=_COMPANY, unreadable=False):
		super().__init__("frappe")
		self.ValidationError = type("ValidationError", (Exception,), {})
		self.exceptions = types.SimpleNamespace(
			UniqueValidationError=type("UniqueValidationError", (self.ValidationError,), {})
		)
		self._logger = _Logger()
		self.logger = lambda name=None: self._logger
		self.events, self.payloads = [], []
		self.on_insert = on_insert or (lambda doc: None)
		self.on_submit = on_submit or (lambda doc: None)
		self.company, self.unreadable = company, unreadable
		self.db = types.SimpleNamespace(
			get_value=lambda doctype, filters, fields=None, **_: None,  # no provenance hit
			get_single_value=self._get_single_value,
			savepoint=lambda name: self.events.append("savepoint"),
			rollback=lambda save_point=None: self.events.append("rollback"),
		)
		self.utils = types.SimpleNamespace(get_system_timezone=lambda: "UTC")

	def _get_single_value(self, doctype, field):
		assert (doctype, field) == ("Global Defaults", "default_company")
		if isinstance(self.company, BaseException):
			raise self.company
		return self.company

	def get_all(self, doctype, **kwargs):
		return []  # Items: no batch/serial tracking

	def get_doc(self, payload):
		self.payloads.append(payload)
		return _Invoice(payload, self)


class _Client:
	def __init__(self):
		self.acks = []

	def ack_outcome(self, ref, request, idempotency_key):
		self.acks.append({"ack": request.to_wire(), "key": idempotency_key})


class _Store:
	def __init__(self):
		self.writes = []

	def get_document_ref(self, key):
		return None

	def record_posted(self, key, ref):
		self.writes.append((key, ref))


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


def _work_item(*, version=2, tenders=None):
	wire = {
		"workItemRef": _REF,
		"kind": "sale_post",
		"sourceSystem": "pos-pulse",
		"externalId": "POS-37801",
		"payloadHash": "b" * 64,
		"businessDate": "2026-06-01",
		"itemCursor": "11",
		"sale": {
			"saleRef": "22222222-2222-4222-8222-222222222222",
			"storeId": _STORE,
			"currencyCode": "EGP",
			"posTotal": "19.00",
			"occurredAt": "2026-06-01T10:00:00Z",
			"businessDate": "2026-06-01",
			"sourceSystem": "pos-pulse",
			"externalId": "POS-37801",
			"lines": [
				{
					"lineRef": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
					"lineName": "Item A",
					"unitPrice": "9.50",
					"currencyCode": "EGP",
					"quantity": "2",
					"lineAmount": "19.00",
					"unit": "each",
					"erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"},
				}
			],
		},
	}
	if version is not None:
		wire["resolutionVersion"] = version
	if tenders is not None:
		wire["sale"]["tenders"] = tenders
	return c.PostingWorkItem.from_wire(wire)


def _post(glue, work_item=None):
	client, store = _Client(), _Store()
	outcome = glue.post_work_item(
		work_item or _work_item(),
		client=client,
		store=store,
		uom_map=UomMap({"each": "Nos"}),
		warehouses=PreResolvedWarehouse({_STORE: {"doctype": "Warehouse", "name": "Store WH"}}),
		customers=StoreCustomerMap({_STORE: "Walk-in"}),
		tenders=TenderModeMap({"cash": "Cash"}),
		correlation_id="rt378-test",
	)
	return outcome, client, store


def _reprice(doc):
	"""An enabled selling Pricing Rule: 9.50 -> 9.00 (RT-376), with the difference as a discount."""
	row = doc["items"][0]
	row.update({"rate": 9.0, "amount": 18.0, "discount_amount": 0.5, "price_list_rate": 9.5})
	doc["grand_total"] = 18.0


def _add_tax(doc):
	doc["taxes"] = [{"charge_type": "On Net Total", "rate": 14.0, "tax_amount": 2.66}]
	doc["grand_total"] = 21.66


def _add_discount(doc):
	doc["items"][0]["discount_percentage"] = 5.0


class TestHonouredInvoice:
	def test_an_exact_invoice_is_read_back_and_acked_posted(self, load_glue):
		fake = _Frappe()
		outcome, client, store = _post(load_glue(fake))
		assert outcome == "posted"
		assert fake.events == ["savepoint", "insert", "submit", "reload"]
		assert client.acks[0]["ack"]["outcome"] == "posted"
		assert len(store.writes) == 1


class TestBeforeSubmit:
	@pytest.mark.parametrize("mutate", [_reprice, _add_tax, _add_discount])
	def test_a_mismatch_after_insert_rolls_back_and_rejects_as_validation(self, load_glue, mutate):
		fake = _Frappe(on_insert=mutate)
		outcome, client, store = _post(load_glue(fake))
		assert outcome == "permanently_rejected"
		assert fake.events == ["savepoint", "insert", "rollback"]  # nothing submitted, nothing kept
		ack = client.acks[0]
		assert ack["key"] == f"{_REF}:permanently_rejected:v2"
		assert ack["ack"]["reason"]["category"] == "validation"
		assert ack["ack"]["reason"]["message"].startswith("RepresentationMismatch: representation:")
		assert store.writes == []

	def test_a_repriced_cash_sale_is_a_representation_mismatch_not_settlement_drift(self, load_glue):
		# The pilot's main path: a paid (is_pos) sale repriced 9.50 -> 9.00 on insert (RT-376).
		fake = _Frappe(on_insert=_reprice)
		paid = _work_item(tenders=[{"method": "cash", "amount": "19.00"}])
		outcome, client, store = _post(load_glue(fake), paid)
		assert outcome == "permanently_rejected"
		assert fake.payloads[0]["is_pos"] == 1
		assert fake.events == ["savepoint", "insert", "rollback"]
		assert client.acks[0]["ack"]["reason"]["category"] == "validation"
		assert client.acks[0]["ack"]["reason"]["message"].startswith("RepresentationMismatch: representation:")
		assert store.writes == []

	def test_the_sale_carries_the_site_default_company_explicitly(self, load_glue):
		# Review finding 2: one source of truth with the check, never ERPNext's user-scoped default.
		fake = _Frappe()
		_post(load_glue(fake))
		assert fake.payloads[0]["company"] == _COMPANY

	def test_an_insert_that_drops_ignore_pricing_rule_is_rejected(self, load_glue):
		fake = _Frappe(on_insert=lambda doc: doc.update(ignore_pricing_rule=0))
		outcome, client, _store = _post(load_glue(fake))
		assert outcome == "permanently_rejected"
		assert "ignore_pricing_rule" in client.acks[0]["ack"]["reason"]["message"]

	def test_an_honoured_cash_sale_posts(self, load_glue):
		fake = _Frappe()
		paid = _work_item(tenders=[{"method": "cash", "amount": "19.00"}])
		assert _post(load_glue(fake), paid)[0] == "posted"

	def test_no_site_default_company_fails_closed_before_insert(self, load_glue):
		fake = _Frappe(company=None)
		outcome, client, _store = _post(load_glue(fake))
		assert outcome == "permanently_rejected"
		assert fake.payloads == [] and "insert" not in fake.events
		assert client.acks[0]["ack"]["reason"]["category"] == "validation"
		# Nothing was persisted, so this is a plain config rejection, not a D1 representation mismatch.
		message = client.acks[0]["ack"]["reason"]["message"]
		assert not message.startswith("representation:") and "default_company" in message

	def test_a_transient_error_reading_the_default_company_is_retried(self, load_glue):
		# Codex PR #66: a lock / timeout on the company read is retryable, never a terminal rejection.
		fake = _Frappe(company=TimeoutError("Lock wait timeout exceeded"))
		outcome, client, store = _post(load_glue(fake))
		assert outcome == "failed_transient"
		assert client.acks[0]["ack"] == {"outcome": "failed_transient", "resolutionVersion": 2}
		assert "insert" not in fake.events and store.writes == []

	def test_an_invoice_on_another_company_is_rejected(self, load_glue):
		fake = _Frappe(on_insert=lambda doc: doc.update(company="Other Co"))
		outcome, client, _store = _post(load_glue(fake))
		assert outcome == "permanently_rejected"
		assert "company" in client.acks[0]["ack"]["reason"]["message"]


class TestAfterSubmit:
	def test_a_reprice_on_submit_acks_reconciliation_required(self, load_glue):
		fake = _Frappe(on_submit=_reprice)
		outcome, client, store = _post(load_glue(fake))
		assert outcome == "reconciliation_required"
		ack = client.acks[0]
		assert ack["key"] == f"{_REF}:reconciliation_required:v2"
		assert ack["ack"]["outcome"] == "reconciliation_required"
		assert ack["ack"]["documentRef"] == {"doctype": "Sales Invoice", "name": _NAME}
		assert ack["ack"]["reason"]["category"] == "validation"
		assert ack["ack"]["reason"]["message"].startswith("representation:")
		assert ack["ack"]["resolutionVersion"] == 2
		assert len(store.writes) == 1  # the Posting Log keeps the document
		assert "cancel" not in fake.events and "rollback" not in fake.events  # never undone (684)

	def test_an_unversioned_mismatch_still_acks_reconciliation_required(self, load_glue):
		fake = _Frappe(on_submit=_reprice)
		outcome, client, _store = _post(load_glue(fake), _work_item(version=None))
		assert outcome == "reconciliation_required"
		assert client.acks[0]["key"] == f"{_REF}:reconciliation_required"
		assert "resolutionVersion" not in client.acks[0]["ack"]

	def test_an_unreadable_invoice_acks_failed_transient(self, load_glue):
		fake = _Frappe(unreadable=True)
		outcome, client, store = _post(load_glue(fake))
		assert outcome == "failed_transient"
		assert client.acks[0]["ack"] == {"outcome": "failed_transient", "resolutionVersion": 2}
		assert len(store.writes) == 1  # a re-offer goes through the replay guard and verifies again


def _raise_on_insert(exc):
	def _raise(doc):
		raise exc

	return _raise


class TestRejectionReasonAttribution:
	"""RT-409 (AD-POSTING-001 D-7): every rejection reason names the check that failed and is logged at
	ERROR, the level Frappe v15 keeps outside the dev server, with the text sent on the wire."""

	def test_an_unclassified_error_is_prefixed_with_its_class(self, load_glue):
		fake = _Frappe(on_insert=_raise_on_insert(NameError("name 'flt' is not defined")))
		outcome, client, store = _post(load_glue(fake))
		assert outcome == "permanently_rejected"
		assert client.acks[0]["ack"]["reason"] == {"category": "other", "message": "NameError: name 'flt' is not defined"}
		assert "rollback" in fake.events and store.writes == []

	def test_a_frappe_validation_error_is_prefixed_with_its_class(self, load_glue):
		fake = _Frappe()
		fake.on_insert = _raise_on_insert(fake.ValidationError("Server Scripts are disabled"))
		outcome, client, _store = _post(load_glue(fake))
		assert outcome == "permanently_rejected"
		assert client.acks[0]["ack"]["reason"] == {
			"category": "validation",
			"message": "ValidationError: Server Scripts are disabled",
		}

	def test_a_config_rejection_before_insert_is_prefixed_too(self, load_glue):
		fake = _Frappe(company=None)
		_outcome, client, _store = _post(load_glue(fake))
		assert client.acks[0]["ack"]["reason"]["message"].startswith("SiteCompanyNotConfigured: ")

	def test_rejected_is_logged_at_error_with_the_wire_reason(self, load_glue):
		fake = _Frappe(on_insert=_reprice)
		_outcome, client, _store = _post(load_glue(fake))
		level, record = _signal(fake, "posting.rejected")
		assert level == "error"
		assert record["category"] == "validation"
		assert record["reason_message"] == client.acks[0]["ack"]["reason"]["message"]
		assert record["reason_message"].startswith("RepresentationMismatch: representation:")

	def test_reconciliation_required_is_logged_at_error_with_the_wire_reason(self, load_glue):
		fake = _Frappe(on_submit=_reprice)
		_outcome, client, _store = _post(load_glue(fake))
		level, record = _signal(fake, "posting.reconciliation_required")
		assert level == "error"
		assert record["category"] == "validation"
		assert record["reason_message"] == client.acks[0]["ack"]["reason"]["message"]
		assert record["document_ref"] == _NAME

	def test_the_posted_signal_keeps_its_level(self, load_glue):
		fake = _Frappe()
		_post(load_glue(fake))
		assert _signal(fake, "posting.posted")[0] == "info"

	def test_a_credential_in_the_error_never_reaches_the_wire_or_the_log(self, load_glue):
		fake = _Frappe()
		fake.on_insert = _raise_on_insert(fake.ValidationError("bad settings dp2_token=abc123 pwd=hunter2"))
		_outcome, client, _store = _post(load_glue(fake))
		_level, record = _signal(fake, "posting.rejected")
		for text in (client.acks[0]["ack"]["reason"]["message"], record["reason_message"]):
			assert "abc123" not in text and "hunter2" not in text
			assert text.startswith("ValidationError: bad settings")
