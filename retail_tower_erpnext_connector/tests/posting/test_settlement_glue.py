# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-78 — executable coverage of POS settlement in the REAL glue (no bench).

``frappe_glue`` imports frappe, so it is loaded here against a minimal stand-in module (the RT-71
pattern). The stand-in's Sales Invoice computes ``grand_total`` / ``paid_amount`` from the payload
the way ERPNext would, and a test can make ERPNext's computed totals DRIFT after insert.

Cases:
  - an unmapped tender method and a tender total that differs from the lines are rejected as
    ``validation`` and build/submit nothing (RT-10 D5, amendment 4a);
  - a cash sale is inserted as ``is_pos`` with the mapped Mode of Payment, submitted and acked
    ``posted``;
  - if ERPNext's computed totals do not settle the invoice exactly (change, outstanding or write-off
    left over), the insert is rolled back, never submitted, and rejected as ``validation``.
"""

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
	"""A Sales Invoice stand-in: totals computed on insert like ERPNext (plus an optional drift)."""

	def __init__(self, payload, frappe_fake):
		self.payload = payload
		self._fake = frappe_fake
		self.name = "ACC-SINV-2026-09999"
		self.values = {}

	def insert(self):
		grand = sum(Decimal(i["amount"]) for i in self.payload["items"])
		paid = sum(Decimal(p["amount"]) for p in self.payload.get("payments") or [])
		self.values = {
			"grand_total": float(grand),
			"paid_amount": float(paid),
			"change_amount": 0.0,
			"write_off_amount": 0.0,
			"outstanding_amount": float(grand - paid),
		}
		self.values.update(self._fake.drift)
		self._fake.events.append("insert")

	def get(self, field):
		# RT-378: ERPNext holds the payload as sent, on the site default company, plus computed totals.
		return {"company": "RT Company", **self.payload, **self.values}.get(field)

	def reload(self):
		pass  # RT-378 read-back after submit: the database holds what insert computed

	def submit(self):
		self._fake.events.append("submit")


class _Frappe(types.ModuleType):
	def __init__(self, drift=None):
		super().__init__("frappe")
		self.ValidationError = type("ValidationError", (Exception,), {})
		unique = type("UniqueValidationError", (self.ValidationError,), {})
		self.exceptions = types.SimpleNamespace(UniqueValidationError=unique)
		self._logger = _Logger()
		self.logger = lambda name=None: self._logger
		self.events = []
		self.payloads = []
		self.drift = drift or {}
		self.db = types.SimpleNamespace(
			get_value=lambda *a, **k: None,
			get_single_value=lambda doctype, field: "RT Company",  # RT-378 site default company
			savepoint=lambda name: self.events.append("savepoint"),
			rollback=lambda save_point=None: self.events.append("rollback"),
		)
		self.utils = types.SimpleNamespace(get_system_timezone=lambda: "UTC")

	def get_all(self, *args, **kwargs):
		return []  # no batch/serial-tracked Items

	def get_doc(self, payload):
		self.payloads.append(payload)
		return _Invoice(payload, self)


class _Client:
	def __init__(self):
		self.acks = []

	def ack_outcome(self, ref, request, idempotency_key):
		self.acks.append(request.to_wire())


class _Store:
	def __init__(self):
		self.writes = []

	def get_document_ref(self, key):
		return None

	def record_posted(self, key, ref):
		self.writes.append((key, ref))


def _restore(namespace: dict, name: str, value) -> None:
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


_STORE = "33333333-3333-4333-8333-333333333333"


def _sale(tenders) -> c.PostingWorkItem:
	return c.PostingWorkItem.from_wire(
		{
			"workItemRef": "66666666-6666-4666-8666-666666666666",
			"kind": "sale_post",
			"sourceSystem": "pos-pulse",
			"externalId": "POS-7801",
			"payloadHash": "c" * 64,
			"businessDate": "2026-06-01",
			"itemCursor": "cursor-78",
			"sale": {
				"saleRef": "22222222-2222-4222-8222-222222222222",
				"storeId": _STORE,
				"currencyCode": "EGP",
				"posTotal": "10.49",
				"occurredAt": "2026-06-01T10:00:00Z",
				"businessDate": "2026-06-01",
				"sourceSystem": "pos-pulse",
				"externalId": "POS-7801",
				"lines": [
					{
						"lineName": "Item A",
						"unitPrice": "10.49",
						"currencyCode": "EGP",
						"quantity": "1",
						"lineAmount": "10.49",
						"unit": "each",
						"erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"},
					}
				],
				"tenders": tenders,
			},
		}
	)


def _post(glue, work_item, *, tender_map):
	client, store = _Client(), _Store()
	outcome = glue.post_work_item(
		work_item,
		client=client,
		store=store,
		uom_map=UomMap({"each": "Nos"}),
		warehouses=PreResolvedWarehouse({_STORE: {"doctype": "Warehouse", "name": "Stores - RT"}}),
		customers=StoreCustomerMap({_STORE: "Walk-in"}),
		tenders=TenderModeMap(tender_map),
		correlation_id="rt78-test",
	)
	return outcome, client, store


_CASH = [{"method": "cash", "amount": "10.49"}]


class TestSettlementInGlue:
	def test_unmapped_tender_is_rejected_and_builds_nothing(self, load_glue):
		fake = _Frappe()
		outcome, client, store = _post(load_glue(fake), _sale(_CASH), tender_map={})
		assert outcome == "permanently_rejected"
		assert client.acks[0]["reason"]["category"] == "validation"
		assert fake.payloads == [] and store.writes == []

	def test_tender_total_mismatch_is_rejected_and_builds_nothing(self, load_glue):
		fake = _Frappe()
		work_item = _sale([{"method": "cash", "amount": "10.00"}])
		outcome, client, _store = _post(load_glue(fake), work_item, tender_map={"cash": "Cash"})
		assert outcome == "permanently_rejected"
		assert client.acks[0]["reason"]["category"] == "validation"
		assert fake.payloads == []

	def test_cash_sale_posts_as_a_paid_pos_invoice(self, load_glue):
		fake = _Frappe()
		outcome, client, store = _post(load_glue(fake), _sale(_CASH), tender_map={"cash": "Cash"})
		assert outcome == "posted"
		(payload,) = fake.payloads
		assert payload["is_pos"] == 1
		assert payload["payments"] == [{"mode_of_payment": "Cash", "amount": "10.49"}]
		assert payload["disable_rounded_total"] == 1
		assert fake.events == ["savepoint", "insert", "submit"]
		assert client.acks[0]["outcome"] == "posted" and len(store.writes) == 1

	@pytest.mark.parametrize(
		"drift",
		[{"change_amount": 0.49}, {"outstanding_amount": 0.49}, {"write_off_amount": 0.49}, {"paid_amount": 10.0}],
	)
	def test_totals_that_do_not_settle_exactly_are_rolled_back_and_rejected(self, load_glue, drift):
		fake = _Frappe(drift=drift)
		outcome, client, store = _post(load_glue(fake), _sale(_CASH), tender_map={"cash": "Cash"})
		assert outcome == "permanently_rejected"
		assert client.acks[0]["reason"]["category"] == "validation"
		assert fake.events == ["savepoint", "insert", "rollback"]  # never submitted
		assert store.writes == []

	def test_erpnext_rounding_away_from_the_requested_total_is_rejected(self, load_glue):
		# Codex P2 PR #50 round 3: grand_total and paid_amount both rounded (10.49 -> 10.00) still
		# balance; the invoice must equal the EXACT total the connector asked it to post.
		fake = _Frappe(drift={"grand_total": 10.0, "paid_amount": 10.0, "outstanding_amount": 0.0})
		outcome, client, store = _post(load_glue(fake), _sale(_CASH), tender_map={"cash": "Cash"})
		assert outcome == "permanently_rejected"
		assert client.acks[0]["reason"]["category"] == "validation"
		assert fake.events == ["savepoint", "insert", "rollback"] and store.writes == []

	def test_tender_unknown_sale_skips_the_settlement_check(self, load_glue):
		# An unpaid invoice legitimately keeps its outstanding amount (RT-10 D8).
		fake = _Frappe()
		outcome, _client, _store = _post(load_glue(fake), _sale(None), tender_map={})
		assert outcome == "posted"
		assert "payments" not in fake.payloads[0]
		assert fake.events == ["savepoint", "insert", "submit"]
