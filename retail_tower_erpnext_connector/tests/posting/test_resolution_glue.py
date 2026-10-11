# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt
"""RT-331 — the real glue consumes the frozen resolution (Backend-Core RT-332), no bench.

* A fresh post uses the feed's ``sale.warehouseRef`` (the frozen warehouse) instead of the local
  warehouse map, and echoes ``resolutionVersion`` on the ``posted`` ack.
* A replay-guard hit verifies the EXISTING invoice against the frozen resolution before acking:
  a match acks ``posted`` (echoing the version); a mismatch acks ``reconciliation_required`` with
  the existing ``documentRef``; an unreadable invoice acks ``failed_transient`` (never a blind
  ``posted``).
* A work item without ``resolutionVersion`` (older Backend-Core) behaves exactly as before: the
  replay echoes ``posted`` without reading the invoice.

``frappe_glue`` imports frappe, so it is loaded against a minimal stand-in (the RT-71 / RT-78 /
RT-171 pattern).
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
_STORE = "33333333-3333-4333-8333-333333333333"
_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_REF = "18181818-1818-4181-8181-181818181818"
_EXISTING = c.ErpnextDocumentRef(doctype="Sales Invoice", name="ACC-SINV-2026-18000")


class _Logger:
	def __init__(self):
		self.records = []

	def info(self, record):
		self.records.append(record)

	warning = error = info


class _Invoice:
	def __init__(self, payload, fake):
		self.payload, self._fake, self.name = payload, fake, "ACC-SINV-2026-18999"

	def insert(self):
		grand = sum(Decimal(i["amount"]) for i in self.payload["items"])
		self.values = {
			"grand_total": float(grand),
			"paid_amount": 0.0,
			"change_amount": 0.0,
			"write_off_amount": 0.0,
			"outstanding_amount": float(grand),
		}

	def get(self, field):
		# RT-378: ERPNext holds the payload as sent, on the site default company, plus computed totals.
		return {"company": "RT Company", **self.payload, **self.values}.get(field)

	def reload(self):
		pass  # RT-378 read-back after submit: the database holds what insert computed

	def submit(self):
		self._fake.events.append("submit")


# RT-378: an existing row that holds exactly what Retail Tower sent; a test overrides fields.
_HONOURED_ROW = {
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


class _Existing:
	"""The EXISTING Sales Invoice as ``frappe.get_doc`` reads it back from the database."""

	def __init__(self, docstatus, items, flag=1):
		grand = sum(row["amount"] for row in items)
		self._fields = {
			"docstatus": docstatus,
			"currency": "EGP",
			"customer": "Walk-in",
			"company": "RT Company",
			"ignore_pricing_rule": flag,  # RT-378: 0 = posted before RT-377
			"grand_total": grand,
			"taxes": [],
			"items": items,
		}

	def get(self, field):
		return self._fields.get(field)


class _Frappe(types.ModuleType):
	"""``invoice`` is what the EXISTING Sales Invoice holds: docstatus + item rows."""

	def __init__(self, *, docstatus=1, items=(), unreadable=False, missing=False, company="RT Company", flag=1):
		super().__init__("frappe")
		self.ValidationError = type("ValidationError", (Exception,), {})
		self.DoesNotExistError = type("DoesNotExistError", (self.ValidationError,), {})
		self.missing, self.flag = missing, flag
		self.exceptions = types.SimpleNamespace(
			UniqueValidationError=type("UniqueValidationError", (self.ValidationError,), {})
		)
		self._logger = _Logger()
		self.logger = lambda name=None: self._logger
		self.events, self.payloads, self.reads = [], [], []
		self.docstatus, self.unreadable = docstatus, unreadable
		self.items = [{**_HONOURED_ROW, **row} for row in items]
		self.db = types.SimpleNamespace(
			get_value=self._get_value,
			get_single_value=lambda doctype, field: company,  # RT-378 site default company
			savepoint=lambda name: None,
			rollback=lambda save_point=None: None,
		)
		self.utils = types.SimpleNamespace(get_system_timezone=lambda: "UTC")

	def _get_value(self, doctype, filters, fields=None, **_):
		assert isinstance(filters, dict), "the existing invoice is read with get_doc (RT-378)"
		return None  # no provenance hit for a fresh post

	def _read(self, name):
		self.reads.append(name)
		if self.unreadable:
			raise TimeoutError("Lock wait timeout exceeded")

	def get_all(self, doctype, **kwargs):
		return []  # Items: no batch/serial tracking

	def get_doc(self, payload, name=None):
		if name is not None:  # read back an existing invoice from the database
			assert payload == "Sales Invoice"
			self._read(name)
			if self.missing:
				raise self.DoesNotExistError(f"Sales Invoice {name} not found")
			return _Existing(self.docstatus, [dict(row) for row in self.items], self.flag)
		self.payloads.append(payload)
		return _Invoice(payload, self)


class _Client:
	def __init__(self):
		self.acks = []

	def ack_outcome(self, ref, request, idempotency_key):
		self.acks.append({"ack": request.to_wire(), "key": idempotency_key})


class _Store:
	def __init__(self, existing=None):
		self.existing, self.writes = existing, []

	def get_document_ref(self, key):
		return self.existing

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


def _work_item(*, version=2, warehouse="Frozen WH"):
	sale = {
		"saleRef": "22222222-2222-4222-8222-222222222222",
		"storeId": _STORE,
		"currencyCode": "EGP",
		"posTotal": "100.00",
		"occurredAt": "2026-06-01T10:00:00Z",
		"businessDate": "2026-06-01",
		"sourceSystem": "pos-pulse",
		"externalId": "POS-18101",
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
	}
	if warehouse is not None:
		sale["warehouseRef"] = {"doctype": "Warehouse", "name": warehouse}
	wire = {
		"workItemRef": _REF,
		"kind": "sale_post",
		"sourceSystem": "pos-pulse",
		"externalId": "POS-18101",
		"payloadHash": "a" * 64,
		"businessDate": "2026-06-01",
		"itemCursor": "9",
		"sale": sale,
	}
	if version is not None:
		wire["resolutionVersion"] = version
	return c.PostingWorkItem.from_wire(wire)


def _post(glue, work_item, store):
	client = _Client()
	outcome = glue.post_work_item(
		work_item,
		client=client,
		store=store,
		uom_map=UomMap({"each": "Nos"}),
		warehouses=PreResolvedWarehouse({_STORE: {"doctype": "Warehouse", "name": "Map WH"}}),
		customers=StoreCustomerMap({_STORE: "Walk-in"}),
		tenders=TenderModeMap({"cash": "Cash"}),
		correlation_id="rt331-test",
	)
	return outcome, client


class TestFreshPost:
	def test_uses_the_frozen_warehouse_and_echoes_the_version(self, load_glue):
		fake = _Frappe()
		glue = load_glue(fake)
		outcome, client = _post(glue, _work_item(), _Store())
		assert outcome == "posted"
		assert {item["warehouse"] for item in fake.payloads[0]["items"]} == {"Frozen WH"}
		assert client.acks[0]["ack"]["outcome"] == "posted"
		assert client.acks[0]["ack"]["resolutionVersion"] == 2

	def test_without_a_frozen_warehouse_the_local_map_applies(self, load_glue):
		fake = _Frappe()
		glue = load_glue(fake)
		_post(glue, _work_item(warehouse=None), _Store())
		assert {item["warehouse"] for item in fake.payloads[0]["items"]} == {"Map WH"}


class TestReplayVerification:
	def test_a_matching_existing_invoice_acks_posted_with_the_version(self, load_glue):
		fake = _Frappe(items=[{"item_code": "ITEM-A", "warehouse": "Frozen WH", "rt_line_ref": _A}])
		glue = load_glue(fake)
		outcome, client = _post(glue, _work_item(), _Store(existing=_EXISTING))
		assert outcome == "posted"
		assert client.acks == [
			{
				"ack": {
					"outcome": "posted",
					"documentRef": {"doctype": "Sales Invoice", "name": "ACC-SINV-2026-18000"},
					"resolutionVersion": 2,
				},
				"key": f"{_REF}:posted:v2",
			}
		]

	def test_a_mismatching_existing_invoice_acks_reconciliation_required(self, load_glue):
		fake = _Frappe(items=[{"item_code": "ITEM-OTHER", "warehouse": "Frozen WH", "rt_line_ref": _A}])
		glue = load_glue(fake)
		outcome, client = _post(glue, _work_item(), _Store(existing=_EXISTING))
		assert outcome == "reconciliation_required"
		ack = client.acks[0]
		assert ack["key"] == f"{_REF}:reconciliation_required:v2"
		assert ack["ack"]["outcome"] == "reconciliation_required"
		assert ack["ack"]["documentRef"] == {"doctype": "Sales Invoice", "name": "ACC-SINV-2026-18000"}
		assert ack["ack"]["reason"]["category"] == "validation"
		assert fake.events.count("submit") == 0  # never a second document

	def test_an_unreadable_existing_invoice_acks_failed_transient(self, load_glue):
		fake = _Frappe(unreadable=True)
		glue = load_glue(fake)
		outcome, client = _post(glue, _work_item(), _Store(existing=_EXISTING))
		assert outcome == "failed_transient"
		# Echoed on every outcome, so Backend-Core can fence a superseded attempt (RT-333).
		assert client.acks[0]["ack"] == {"outcome": "failed_transient", "resolutionVersion": 2}
		assert client.acks[0]["key"] == f"{_REF}:failed_transient:9"

	def test_a_legacy_work_item_skips_the_identity_checks_but_not_the_representation(self, load_glue):
		# RT-378 finding 3: an unversioned replay has no frozen resolution, so the RT-331 identity
		# checks stay skipped (a warehouse other than any frozen one is fine) -- but it is read back.
		fake = _Frappe(items=[{"item_code": "ITEM-A", "warehouse": "Some Old WH", "rt_line_ref": None}])
		glue = load_glue(fake)
		outcome, client = _post(glue, _work_item(version=None), _Store(existing=_EXISTING))
		assert outcome == "posted"
		assert fake.reads == [_EXISTING.name]
		assert "resolutionVersion" not in client.acks[0]["ack"]

	def test_a_legacy_work_item_whose_invoice_was_repriced_acks_reconciliation_required(self, load_glue):
		row = {"item_code": "ITEM-A", "warehouse": "X", "rt_line_ref": _A, "rate": 90.0, "amount": 90.0}
		fake = _Frappe(items=[row], flag=0)
		outcome, client = _post(load_glue(fake), _work_item(version=None), _Store(existing=_EXISTING))
		assert outcome == "reconciliation_required"
		assert "resolutionVersion" not in client.acks[0]["ack"]
		assert client.acks[0]["key"] == f"{_REF}:reconciliation_required"

	def test_a_correct_pre_rt377_legacy_invoice_acks_posted(self, load_glue):
		# ERPNext's own list price and an invented discount, but the rate Retail Tower sent (flag 0).
		row = {"item_code": "ITEM-A", "warehouse": "X", "rt_line_ref": _A, "price_list_rate": 110.0,
			"discount_amount": 10.0}
		fake = _Frappe(items=[row], flag=0, company="ERPNext Default Co")
		outcome, _client = _post(load_glue(fake), _work_item(version=None), _Store(existing=_EXISTING))
		assert outcome == "posted"

	def test_a_legacy_work_item_whose_invoice_is_unreadable_acks_failed_transient(self, load_glue):
		fake = _Frappe(unreadable=True)
		outcome, client = _post(load_glue(fake), _work_item(version=None), _Store(existing=_EXISTING))
		assert outcome == "failed_transient"
		assert client.acks[0]["ack"] == {"outcome": "failed_transient"}


class TestReconciliationReasonIsBounded:
	def test_a_long_mismatch_reason_fits_the_ack_message_limit(self, load_glue):
		rows = [
			{"item_code": f"ITEM-LONG-CODE-{i:05d}", "warehouse": "Frozen WH", "rt_line_ref": None}
			for i in range(300)
		]
		fake = _Frappe(items=rows)
		glue = load_glue(fake)
		outcome, client = _post(glue, _work_item(), _Store(existing=_EXISTING))
		assert outcome == "reconciliation_required"
		message = client.acks[0]["ack"]["reason"]["message"]
		assert 1 <= len(message) <= 1000


class _ConflictStore(_Store):
	"""``record_posted`` loses a race: another worker recorded ``existing`` for this key first."""

	def record_posted(self, key, ref):
		from retail_tower_erpnext_connector.connector.posting.idempotency import IdempotencyConflict

		raise IdempotencyConflict(key, _EXISTING, ref)


class TestIdempotencyConflictIsVerified:
	def test_a_conflict_selected_document_is_verified_before_acking(self, load_glue):
		fake = _Frappe(items=[{"item_code": "ITEM-OTHER", "warehouse": "Frozen WH", "rt_line_ref": _A}])
		glue = load_glue(fake)
		outcome, client = _post(glue, _work_item(), _ConflictStore())
		assert outcome == "reconciliation_required"
		assert client.acks[0]["ack"]["documentRef"] == {
			"doctype": "Sales Invoice",
			"name": "ACC-SINV-2026-18000",
		}


class TestEveryAckEchoesTheVersion:
	def test_a_rejection_echoes_the_resolution_version(self, load_glue):
		fake = _Frappe()
		glue = load_glue(fake)
		item = _work_item()
		client = _Client()
		outcome = glue.post_work_item(
			item,
			client=client,
			store=_Store(),
			uom_map=UomMap({}),  # the line's unit is unmapped -> rejected
			warehouses=PreResolvedWarehouse({_STORE: {"doctype": "Warehouse", "name": "Map WH"}}),
			customers=StoreCustomerMap({_STORE: "Walk-in"}),
			tenders=TenderModeMap({"cash": "Cash"}),
			correlation_id="rt331-test",
		)
		assert outcome == "permanently_rejected"
		assert client.acks[0]["ack"]["resolutionVersion"] == 2

	def test_a_legacy_rejection_carries_no_version(self, load_glue):
		fake = _Frappe()
		glue = load_glue(fake)
		client = _Client()
		glue.post_work_item(
			_work_item(version=None),
			client=client,
			store=_Store(),
			uom_map=UomMap({}),
			warehouses=PreResolvedWarehouse({_STORE: {"doctype": "Warehouse", "name": "Map WH"}}),
			customers=StoreCustomerMap({_STORE: "Walk-in"}),
			tenders=TenderModeMap({"cash": "Cash"}),
			correlation_id="rt331-test",
		)
		assert "resolutionVersion" not in client.acks[0]["ack"]


class TestTerminalAckKeysAreVersioned:
	"""A rejection of v1 and one of v2 are different logical acks: one key would 409 the second."""

	def _reject(self, glue, version):
		client = _Client()
		glue.post_work_item(
			_work_item(version=version),
			client=client,
			store=_Store(),
			uom_map=UomMap({}),
			warehouses=PreResolvedWarehouse({_STORE: {"doctype": "Warehouse", "name": "Map WH"}}),
			customers=StoreCustomerMap({_STORE: "Walk-in"}),
			tenders=TenderModeMap({"cash": "Cash"}),
			correlation_id="rt331-test",
		)
		return client.acks[0]["key"]

	def test_each_resolution_version_rejects_under_its_own_key(self, load_glue):
		glue = load_glue(_Frappe())
		assert self._reject(glue, 1) == f"{_REF}:permanently_rejected:v1"
		assert self._reject(glue, 2) == f"{_REF}:permanently_rejected:v2"

	def test_an_unversioned_work_item_keeps_the_legacy_key(self, load_glue):
		glue = load_glue(_Frappe())
		assert self._reject(glue, None) == f"{_REF}:permanently_rejected"


class TestReplayRepresentation:
	"""RT-378 (RT-283 B.3): a replay also verifies the persisted representation."""

	def test_a_repriced_existing_invoice_acks_reconciliation_required(self, load_glue):
		row = {"item_code": "ITEM-A", "warehouse": "Frozen WH", "rt_line_ref": _A, "rate": 90.0, "amount": 90.0}
		fake = _Frappe(items=[row])
		glue = load_glue(fake)
		outcome, client = _post(glue, _work_item(), _Store(existing=_EXISTING))
		assert outcome == "reconciliation_required"
		assert client.acks[0]["ack"]["reason"]["message"].startswith("representation:")
		assert fake.events.count("submit") == 0

	def test_an_erp_derived_discount_on_the_existing_invoice_is_a_mismatch(self, load_glue):
		row = {"item_code": "ITEM-A", "warehouse": "Frozen WH", "rt_line_ref": _A, "discount_amount": 5.0}
		glue = load_glue(_Frappe(items=[row]))
		outcome, _client = _post(glue, _work_item(), _Store(existing=_EXISTING))
		assert outcome == "reconciliation_required"

	def _replay_without_unit_map(self, glue):
		client = _Client()
		outcome = glue.post_work_item(
			_work_item(),
			client=client,
			store=_Store(existing=_EXISTING),
			uom_map=UomMap({}),  # the unit map lost "each" since the sale posted
			warehouses=PreResolvedWarehouse({_STORE: {"doctype": "Warehouse", "name": "Map WH"}}),
			customers=StoreCustomerMap({_STORE: "Walk-in"}),
			tenders=TenderModeMap({"cash": "Cash"}),
			correlation_id="rt378-test",
		)
		return outcome, client

	def test_a_unit_map_change_since_the_sale_does_not_affect_the_replay(self, load_glue):
		# Review trivial: the frozen resolution carries no UOM, so a replay never compares it against
		# today's map (the insert-time check did). A matching invoice still acks posted.
		row = {"item_code": "ITEM-A", "warehouse": "Frozen WH", "rt_line_ref": _A}
		outcome, _client = self._replay_without_unit_map(load_glue(_Frappe(items=[row])))
		assert outcome == "posted"

	def test_an_identity_mismatch_is_reconciliation_required(self, load_glue):
		# RT-331 identity is checked from the work item alone, before any rebuild.
		row = {"item_code": "ITEM-OTHER", "warehouse": "Frozen WH", "rt_line_ref": _A}
		outcome, _client = _post(load_glue(_Frappe(items=[row])), _work_item(), _Store(existing=_EXISTING))
		assert outcome == "reconciliation_required"

	def test_a_persisted_uom_other_than_todays_map_still_posts(self, load_glue):
		row = {"item_code": "ITEM-A", "warehouse": "Frozen WH", "rt_line_ref": _A, "uom": "Box"}
		outcome, _client = _post(load_glue(_Frappe(items=[row])), _work_item(), _Store(existing=_EXISTING))
		assert outcome == "posted"

	@pytest.mark.parametrize("company", [None, "", "New Default Co"])
	def test_a_sale_replay_does_not_depend_on_todays_site_default_company(self, load_glue, company):
		# Codex PR #66: the sale's company was checked at insert; a site default that is blank or has
		# changed since must not read as document drift on replay (the existing invoice is on RT Company).
		fake = _Frappe(items=[{"item_code": "ITEM-A", "warehouse": "Frozen WH", "rt_line_ref": _A}], company=company)
		outcome, _client = _post(load_glue(fake), _work_item(), _Store(existing=_EXISTING))
		assert outcome == "posted"

	def test_a_tender_map_change_does_not_affect_the_replay(self, load_glue):
		fake = _Frappe(items=[{"item_code": "ITEM-A", "warehouse": "Frozen WH", "rt_line_ref": _A}])
		glue = load_glue(fake)
		client = _Client()
		outcome = glue.post_work_item(
			_tendered_work_item(),
			client=client,
			store=_Store(existing=_EXISTING),
			uom_map=UomMap({"each": "Nos"}),
			warehouses=PreResolvedWarehouse({_STORE: {"doctype": "Warehouse", "name": "Map WH"}}),
			customers=StoreCustomerMap({_STORE: "Walk-in"}),
			tenders=TenderModeMap({}),  # "cash" no longer mapped: payments are not compared
			correlation_id="rt378-test",
		)
		assert outcome == "posted"


class TestReplayIgnoresPayments:
	def test_a_pre_rt78_sale_whose_tenders_differ_from_its_lines_is_still_verified(self, load_glue):
		# Codex PR #66: before RT-78 tenders were ignored (unpaid invoice), so a tender total that differs
		# from the lines must not make the replay rebuild fail: payments are never compared.
		fake = _Frappe(items=[{"item_code": "ITEM-A", "warehouse": "Frozen WH", "rt_line_ref": _A}])
		outcome, client = _post(load_glue(fake), _tendered_work_item("90.00"), _Store(existing=_EXISTING))
		assert outcome == "posted", client.acks


def _tendered_work_item(tender_amount="100.00"):
	item = _work_item()
	wire_sale = {
		"saleRef": item.sale.sale_ref,
		"storeId": _STORE,
		"currencyCode": "EGP",
		"posTotal": "100.00",
		"occurredAt": "2026-06-01T10:00:00Z",
		"businessDate": "2026-06-01",
		"sourceSystem": "pos-pulse",
		"externalId": "POS-18101",
		"warehouseRef": {"doctype": "Warehouse", "name": "Frozen WH"},
		"tenders": [{"method": "cash", "amount": tender_amount}],
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
	}
	return c.PostingWorkItem.from_wire(
		{
			"workItemRef": _REF,
			"kind": "sale_post",
			"sourceSystem": "pos-pulse",
			"externalId": "POS-18101",
			"payloadHash": "a" * 64,
			"businessDate": "2026-06-01",
			"itemCursor": "9",
			"resolutionVersion": 2,
			"sale": wire_sale,
		}
	)


class TestMissingExistingInvoice:
	def test_a_missing_existing_invoice_acks_reconciliation_required_as_before(self, load_glue):
		# RT-331 read docstatus 0 for a missing document; get_doc raises instead (RT-378). Same outcome.
		glue = load_glue(_Frappe(missing=True))
		outcome, client = _post(glue, _work_item(), _Store(existing=_EXISTING))
		assert outcome == "reconciliation_required"
		assert "not submitted" in client.acks[0]["ack"]["reason"]["message"]
