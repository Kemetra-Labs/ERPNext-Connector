# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1422 — the frappe-coupled count glue, loaded against a stand-in frappe (no bench).

Proves what the fake-port tests cannot: ``frappe_glue.post_work_item`` routes an ``inventory_count``
item to the count leg BEFORE the sale Posting Log replay guard, ``frappe_count_port`` imports and its
submit-failure mapper classifies a freeze, a back-dated permission refusal and a valuation refusal.
The port's ERPNext calls (balance, Draft lock, ledger read-back, unique index DDL) stay NOT_VERIFIED.
"""

import importlib
import sys
import types

import pytest

from retail_tower_erpnext_connector.connector.posting import contracts as c

_PKG = "retail_tower_erpnext_connector.connector.posting"
_MODULES = (
	f"{_PKG}.frappe_glue",
	f"{_PKG}.frappe_count_port",
	"retail_tower_erpnext_connector.connector.schema",
)
_MISSING = object()
_SESSION = "00000000-0000-7000-8000-00000000c0e1"


class _Logger:
	def __init__(self):
		self.records = []

	def info(self, record):
		self.records.append(record)

	warning = error = info


def _frappe():
	fake = types.ModuleType("frappe")
	fake.ValidationError = type("ValidationError", (Exception,), {})
	fake.PermissionError = type("PermissionError", (Exception,), {})
	fake.exceptions = types.SimpleNamespace(
		UniqueValidationError=type("UniqueValidationError", (fake.ValidationError,), {})
	)
	fake._logger = _Logger()
	fake.logger = lambda name=None: fake._logger
	fake.db = types.SimpleNamespace(commit=lambda: None)
	fake.utils = types.SimpleNamespace(get_system_timezone=lambda: "Africa/Cairo")
	return fake


@pytest.fixture
def glue():
	pkg = importlib.import_module(_PKG)
	saved_modules = {name: sys.modules.get(name, _MISSING) for name in _MODULES}
	saved_frappe = sys.modules.get("frappe", _MISSING)
	saved_attrs = {n: vars(pkg).get(n, _MISSING) for n in ("frappe_glue", "frappe_count_port")}
	fake = _frappe()
	sys.modules["frappe"] = fake
	for name in _MODULES:
		sys.modules.pop(name, None)
	module = importlib.import_module(_MODULES[0])
	module.fake_frappe = fake
	yield module
	for name, value in saved_modules.items():
		sys.modules.pop(name, None)
		if value is not _MISSING:
			sys.modules[name] = value
	sys.modules.pop("frappe", None)
	if saved_frappe is not _MISSING:
		sys.modules["frappe"] = saved_frappe
	for attr, value in saved_attrs.items():
		vars(pkg).pop(attr, None)
		if value is not _MISSING:
			setattr(pkg, attr, value)


def _count_item() -> c.PostingWorkItem:
	return c.PostingWorkItem.from_wire(
		{
			"workItemRef": "00000000-0000-7000-8000-00000000c001",
			"kind": "inventory_count",
			"sourceSystem": "retail-tower",
			"externalId": f"count:{_SESSION}",
			"payloadHash": "a" * 64,
			"businessDate": "2026-11-16",
			"itemCursor": "c1",
			"count": {
				"countSessionRef": _SESSION,
				"chunk": 1,
				"chunkCount": 1,
				"storeId": "00000000-0000-7000-8000-00000000570e",
				"mappingBucketRef": "bucket:store:stock",
				"postingDatetime": "2026-11-16T09:30:00.000001Z",
				"lines": [
					{
						"lineSeq": 1,
						"erpnextItemRef": {"doctype": "Item", "name": "PANADOL-24"},
						"warehouseRef": {"doctype": "Warehouse", "name": "Stores - RT"},
						"countedQuantity": "7",
						"unit": "Nos",
					}
				],
			},
		}
	)


class _Store:
	def get_document_ref(self, key):
		raise AssertionError("the sale replay guard must not run for an inventory_count item")

	def record_posted(self, key, ref):
		raise AssertionError("the glue must not record a count outside the count leg")


def test_post_work_item_routes_inventory_count_before_the_sale_replay_guard(glue, monkeypatch):
	seen = {}

	def _fake_leg(work_item, *, client, store, port, site_tz):
		seen.update(kind=work_item.kind, tz=site_tz, port=type(port).__name__)
		return "verified_noop"

	count_flow = importlib.import_module(f"{_PKG}.count_flow")
	monkeypatch.setattr(count_flow, "post_count_work_item", _fake_leg)
	outcome = glue.post_work_item(
		_count_item(),
		client=object(),
		store=_Store(),
		uom_map=None,
		warehouses=None,
		customers=None,
		tenders=None,
		correlation_id="req-1",
	)
	assert outcome == "verified_noop"
	assert seen == {"kind": "inventory_count", "tz": "Africa/Cairo", "port": "FrappeCountPort"}
	(record,) = glue.fake_frappe._logger.records
	assert record["event"] == "posting.count" and record["outcome"] == "verified_noop"
	assert record["external_id"] == f"count:{_SESSION}"


def test_the_count_port_imports_and_exposes_the_leg_protocol(glue):
	port_module = importlib.import_module(f"{_PKG}.frappe_count_port")
	port = port_module.FrappeCountPort()
	for name in (
		"indexes_ready",
		"company",
		"item_facts",
		"warehouse_problem",
		"existing_documents",
		"balance_at",
		"submit",
		"read_back",
		"commit",
	):
		assert callable(getattr(port, name))


def test_a_transient_site_error_in_a_read_becomes_count_transient(glue):
	port_module = importlib.import_module(f"{_PKG}.frappe_count_port")
	count_flow = importlib.import_module(f"{_PKG}.count_flow")
	glue.fake_frappe.QueryDeadlockError = type("QueryDeadlockError", (Exception,), {})
	glue.fake_frappe.get_all = lambda *a, **k: (_ for _ in ()).throw(
		glue.fake_frappe.QueryDeadlockError("deadlock")
	)
	with pytest.raises(count_flow.CountTransient):
		port_module.FrappeCountPort().item_facts(["X"])


@pytest.mark.parametrize(
	("make", "code", "category_code"),
	[
		(lambda f: f.PermissionError("not allowed"), "back_dated_permission", "other"),
		(
			lambda f: f.ValidationError("Role Stock Manager may create back dated stock transaction"),
			"back_dated_permission",
			"other",
		),
		(
			lambda f: f.ValidationError("Stock transactions before 2026-11-01 are frozen"),
			"closed_period",
			"closed_period",
		),
		(
			lambda f: f.ValidationError("Valuation Rate is mandatory if Opening Stock entered"),
			"no_valuation_history",
			"validation",
		),
		(lambda f: f.ValidationError("something else entirely"), "invalid_shape", "validation"),
	],
)
def test_a_submit_failure_is_classified_into_a_typed_refusal(glue, make, code, category_code):
	port_module = importlib.import_module(f"{_PKG}.frappe_count_port")
	count_flow = importlib.import_module(f"{_PKG}.count_flow")
	refusal = port_module.classify_submit_failure(make(glue.fake_frappe))
	assert isinstance(refusal, count_flow.CountRefused) and refusal.code == code
	assert count_flow.refusal(refusal.code, refusal.message).ack_category == category_code
