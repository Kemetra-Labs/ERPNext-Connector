# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1514 — the frappe-coupled transfer glue, loaded against a stand-in frappe (no bench).

Proves what the fake-port tests cannot: ``frappe_glue.post_work_item`` routes both transfer kinds to the
transfer leg BEFORE the sale Posting Log replay guard, ``frappe_transfer_port`` imports and exposes the leg's
protocol, a transient site read becomes ``TransferTransient`` and the unique-index probe also demands the row
field. The port's ERPNext calls (Draft lock, ledger and batch read-back, the add-to-transit submit, the
unique index DDL) stay NOT_VERIFIED until the v16 bench.
"""

import importlib
import sys
import types

import pytest

from retail_tower_erpnext_connector.connector.posting import contracts as c

from .test_transfer_contracts import dispatch_wire, receive_wire

_PKG = "retail_tower_erpnext_connector.connector.posting"
_MODULES = (
	f"{_PKG}.frappe_glue",
	f"{_PKG}.frappe_count_port",
	f"{_PKG}.frappe_transfer_port",
	"retail_tower_erpnext_connector.connector.schema",
)
_MISSING = object()


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
	fake.db = types.SimpleNamespace(commit=lambda: None, has_column=lambda dt, col: True)
	fake.utils = types.SimpleNamespace(get_system_timezone=lambda: "Africa/Cairo")
	return fake


@pytest.fixture
def glue():
	pkg = importlib.import_module(_PKG)
	saved_modules = {name: sys.modules.get(name, _MISSING) for name in _MODULES}
	saved_frappe = sys.modules.get("frappe", _MISSING)
	saved_attrs = {
		n: vars(pkg).get(n, _MISSING) for n in ("frappe_glue", "frappe_count_port", "frappe_transfer_port")
	}
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


class _Store:
	def get_document_ref(self, key):
		raise AssertionError("the sale replay guard must not run for a transfer item")

	def record_posted(self, key, ref):
		raise AssertionError("the glue must not record a transfer outside the transfer leg")


@pytest.mark.parametrize(
	("wire", "external_prefix"),
	[(dispatch_wire(), "transfer_dispatch:"), (receive_wire(), "transfer_receipt:")],
)
def test_post_work_item_routes_the_transfer_kinds_before_the_sale_replay_guard(
	glue, monkeypatch, wire, external_prefix
):
	seen = {}

	def _fake_leg(work_item, *, client, store, port, site_tz, uom_map):
		seen.update(kind=work_item.kind, tz=site_tz, port=type(port).__name__)
		return "posted"

	transfer_flow = importlib.import_module(f"{_PKG}.transfer_flow")
	monkeypatch.setattr(transfer_flow, "post_transfer_work_item", _fake_leg)
	item = c.PostingWorkItem.from_wire(wire)
	outcome = glue.post_work_item(
		item,
		client=object(),
		store=_Store(),
		uom_map=None,
		warehouses=None,
		customers=None,
		tenders=None,
		correlation_id="req-1",
	)
	assert outcome == "posted"
	assert seen == {"kind": item.kind, "tz": "Africa/Cairo", "port": "FrappeTransferPort"}
	(record,) = glue.fake_frappe._logger.records
	assert record["event"] == "posting.transfer" and record["outcome"] == "posted"
	assert record["external_id"].startswith(external_prefix)


def test_the_transfer_port_imports_and_exposes_the_leg_protocol(glue):
	port_module = importlib.import_module(f"{_PKG}.frappe_transfer_port")
	port = port_module.FrappeTransferPort()
	for name in (
		"indexes_ready",
		"company",
		"item_facts",
		"batch_problem",
		"warehouse_problem",
		"transit_warehouse_problem",
		"existing_documents",
		"dispatch_document",
		"submit",
		"read_back",
		"commit",
	):
		assert callable(getattr(port, name)), name


def test_a_transient_site_error_in_a_read_becomes_transfer_transient(glue):
	port_module = importlib.import_module(f"{_PKG}.frappe_transfer_port")
	transfer_flow = importlib.import_module(f"{_PKG}.transfer_flow")
	glue.fake_frappe.QueryDeadlockError = type("QueryDeadlockError", (Exception,), {})
	glue.fake_frappe.get_all = lambda *a, **k: (_ for _ in ()).throw(
		glue.fake_frappe.QueryDeadlockError("deadlock")
	)
	with pytest.raises(transfer_flow.TransferTransient):
		port_module.FrappeTransferPort().item_facts(["X"])


def test_the_probe_demands_the_unique_index_and_the_row_field(glue, monkeypatch):
	port_module = importlib.import_module(f"{_PKG}.frappe_transfer_port")
	monkeypatch.setattr(port_module, "index_present", lambda dt, name, cols: True)
	assert port_module.FrappeTransferPort().indexes_ready() is True
	glue.fake_frappe.db.has_column = lambda dt, col: False
	assert port_module.FrappeTransferPort().indexes_ready() is False
	glue.fake_frappe.db.has_column = lambda dt, col: True
	monkeypatch.setattr(port_module, "index_present", lambda dt, name, cols: False)
	assert port_module.FrappeTransferPort().indexes_ready() is False


def test_the_submit_failure_mapper_is_the_shared_one(glue):
	port_module = importlib.import_module(f"{_PKG}.frappe_transfer_port")
	count_port = importlib.import_module(f"{_PKG}.frappe_count_port")
	assert port_module.classify_submit_failure is count_port.classify_submit_failure


def test_the_batch_read_back_sums_exactly_and_takes_the_sign_from_the_ledger_entry(glue):
	"""RT-1514 review: a float sum of 0.1 and 0.2 is 0.30000000000000004, a false mismatch on a posted leg."""
	port_module = importlib.import_module(f"{_PKG}.frappe_transfer_port")
	entries = [
		{
			"item_code": "AMOXIL-12",
			"warehouse": "Store A - RT",
			"actual_qty": "-0.3",
			"batch_no": None,
			"serial_and_batch_bundle": "SBB-1",
		}
	]
	bundle = [{"batch_no": "AMX-2611", "qty": "0.1"}, {"batch_no": "AMX-2611", "qty": "0.2"}]

	def get_all(doctype, **kwargs):
		return entries if doctype == "Stock Ledger Entry" else bundle

	glue.fake_frappe.get_all = get_all
	(row,) = port_module.FrappeTransferPort().read_back("MAT-STE-1")
	assert (row.item_code, row.warehouse, row.batch) == ("AMOXIL-12", "Store A - RT", "AMX-2611")
	assert row.actual_qty == "-0.3"  # exact, and negative because the ledger entry is an outward movement
