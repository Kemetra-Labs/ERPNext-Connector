# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-918 — the tracking release switch, the frappe tracking reader and the poller wiring.

Loaded against the frappe stand-in of :mod:`.test_poller` (the RT-39 / RT-71 pattern). The two
Stock Ledger aggregate queries (``frappe.qb``) are replaced by fakes here; their SQL is a bench
validation step (standing-rules §6). Covered: the switch is off by default and parsed like
``cint``; the reader merges bundle-backed and legacy ledger sums per (Item, Batch), lists a Batch
at zero, reads ledger rows for batch Items only, and refuses a Batch with no master; with the
switch off the tick reports the pre-1.4 shape, with it on every entry carries ``tracking``, and
an Item with more than 200 Batches is a logged, non-retried refusal that reports nothing.
"""

import datetime
import importlib

import pytest

from .test_poller import _GLUE, _Dp2, _events, _page, _Reader, _retry_state, _tick, _wire, frappe_stub

__all__ = ["frappe_stub"]  # the fixture is used by name


class _Masters:
	"""``frappe.get_all`` stand-in for the ``Item`` and ``Batch`` master reads."""

	def __init__(self, items, batches):
		self.tables = {"Item": items, "Batch": batches}
		self.calls = []

	def get_all(self, doctype, *, filters, fields):
		op, names = filters["name"]
		assert op == "in"
		assert not {"valuation_rate", "stock_value", "incoming_rate"} & set(fields)
		self.calls.append((doctype, list(names)))
		return [{f: row.get(f) for f in fields} for row in self.tables[doctype] if row["name"] in names]


def _reader(frappe_stub, masters, *, bundle=(), legacy=()):
	frappe_stub.get_all = masters.get_all
	glue = importlib.import_module(_GLUE)
	ledger_calls = []

	def _fake(rows):
		def run(warehouse, item_codes):
			ledger_calls.append((warehouse, list(item_codes)))
			return [dict(r) for r in rows if r["item_code"] in item_codes]

		return run

	glue._bundle_batch_qty = _fake(bundle)
	glue._legacy_batch_qty = _fake(legacy)
	return glue, ledger_calls


# --- release switch -------------------------------------------------------------------------------


@pytest.mark.parametrize(
	"conf,expected",
	[
		({}, False),
		({"rt_connector_bin_view_send_tracking": 0}, False),
		({"rt_connector_bin_view_send_tracking": "0"}, False),
		({"rt_connector_bin_view_send_tracking": "yes"}, False),
		({"rt_connector_bin_view_send_tracking": 1}, True),
		({"rt_connector_bin_view_send_tracking": "1"}, True),
		({"rt_connector_bin_view_send_tracking": True}, True),
		({"rt_connector_bin_view_send_tracking": "true"}, True),
	],
)
def test_tracking_switch_is_off_unless_set(frappe_stub, conf, expected):
	frappe_stub.conf = conf
	glue = importlib.import_module(_GLUE)
	assert glue.tracking_enabled() is expected


def test_tracking_switch_is_off_without_a_site_config(frappe_stub):
	glue = importlib.import_module(_GLUE)
	assert glue.tracking_enabled() is False


# --- FrappeTrackingReader -------------------------------------------------------------------------


def test_reader_merges_bundle_and_legacy_sums_and_reads_the_batch_master(frappe_stub):
	masters = _Masters(
		items=[
			{"name": "MED", "has_batch_no": 1, "has_serial_no": 0, "has_expiry_date": 1},
			{"name": "SOAP", "has_batch_no": 0, "has_serial_no": 0, "has_expiry_date": 0},
		],
		batches=[
			{"name": "B1", "item": "MED", "expiry_date": datetime.date(2027, 3, 31), "disabled": 0},
			{"name": "B2", "item": "MED", "expiry_date": None, "disabled": 1},
		],
	)
	glue, ledger_calls = _reader(
		frappe_stub,
		masters,
		bundle=[
			{"item_code": "MED", "batch_no": "B1", "qty": 10.0},
			{"item_code": "MED", "batch_no": "B2", "qty": 0.0},
		],
		legacy=[{"item_code": "MED", "batch_no": "B1", "qty": 2.5}],
	)
	read = glue.FrappeTrackingReader().read_tracking(
		erpnext_warehouse_ref="ERP-WH-1", item_codes=["SOAP", "MED"]
	)
	assert {(i.item_code, i.has_batch_no, i.has_expiry_date) for i in read.items} == {
		("MED", True, True),
		("SOAP", False, False),
	}
	assert [(b.batch_no, b.qty, b.expiry_date, b.disabled) for b in read.batches] == [
		("B1", 12.5, "2027-03-31", False),  # B2 sums to zero: omitted
	]
	# The zero Batch's master is never read.
	assert [n for d, n in masters.calls if d == "Batch"] == [["B1"]]
	# Ledger rows are read for the batch Items only, in the request's warehouse.
	assert ledger_calls == [("ERP-WH-1", ["MED"]), ("ERP-WH-1", ["MED"])]


def test_batch_master_read_is_chunked(frappe_stub):
	names = [f"B{i:05d}" for i in range(2500)]
	masters = _Masters(
		items=[{"name": "MED", "has_batch_no": 1}],
		batches=[{"name": n, "item": "MED", "expiry_date": None, "disabled": 0} for n in names],
	)
	glue, _ = _reader(
		frappe_stub, masters, bundle=[{"item_code": "MED", "batch_no": n, "qty": 1.0} for n in names]
	)
	read = glue.FrappeTrackingReader().read_tracking(erpnext_warehouse_ref="ERP-WH-1", item_codes=["MED"])
	assert len(read.batches) == 2500
	assert [len(n) for d, n in masters.calls if d == "Batch"] == [1000, 1000, 500]


def test_reader_skips_the_ledger_when_no_item_is_batch_tracked(frappe_stub):
	masters = _Masters(items=[{"name": "SOAP", "has_batch_no": 0, "has_serial_no": 1}], batches=[])
	glue, ledger_calls = _reader(frappe_stub, masters)
	read = glue.FrappeTrackingReader().read_tracking(erpnext_warehouse_ref="ERP-WH-1", item_codes=["SOAP"])
	assert read.batches == () and read.items[0].has_serial_no is True
	assert ledger_calls == [] and [d for d, _ in masters.calls] == ["Item"]


def test_reader_of_no_items_reads_nothing(frappe_stub):
	masters = _Masters(items=[], batches=[])
	glue, _ = _reader(frappe_stub, masters)
	read = glue.FrappeTrackingReader().read_tracking(erpnext_warehouse_ref="ERP-WH-1", item_codes=[])
	assert read.items == () and read.batches == () and masters.calls == []


def test_reader_refuses_a_batch_whose_master_names_another_item(frappe_stub):
	masters = _Masters(
		items=[{"name": "MED", "has_batch_no": 1}],
		batches=[{"name": "B1", "item": "OTHER", "expiry_date": None, "disabled": 0}],
	)
	glue, _ = _reader(frappe_stub, masters, bundle=[{"item_code": "MED", "batch_no": "B1", "qty": 1.0}])
	from retail_tower_erpnext_connector.connector.bin_view.worker import TrackingReadError

	with pytest.raises(TrackingReadError):
		glue.FrappeTrackingReader().read_tracking(erpnext_warehouse_ref="ERP-WH-1", item_codes=["MED"])


def test_iso_date_refuses_a_malformed_string(frappe_stub):
	glue = importlib.import_module(_GLUE)
	with pytest.raises(ValueError):
		glue._iso_date("2027-13-45")


def test_reader_refuses_a_batch_without_a_master_row(frappe_stub):
	masters = _Masters(items=[{"name": "MED", "has_batch_no": 1}], batches=[])
	glue, _ = _reader(frappe_stub, masters, bundle=[{"item_code": "MED", "batch_no": "GHOST", "qty": 1.0}])
	from retail_tower_erpnext_connector.connector.bin_view.worker import TrackingReadError

	with pytest.raises(TrackingReadError):
		glue.FrappeTrackingReader().read_tracking(erpnext_warehouse_ref="ERP-WH-1", item_codes=["MED"])


@pytest.mark.parametrize(
	"value,expected",
	[
		(None, None),
		("", None),
		(datetime.date(2027, 1, 5), "2027-01-05"),
		(datetime.datetime(2027, 1, 5, 9, 30), "2027-01-05"),
		("2027-01-05", "2027-01-05"),
	],
)
def test_iso_date(frappe_stub, value, expected):
	glue = importlib.import_module(_GLUE)
	assert glue._iso_date(value) == expected


# --- poller wiring --------------------------------------------------------------------------------


def _switch_on(frappe_stub, masters, **ledger):
	frappe_stub.conf = {"rt_connector_bin_view_send_tracking": 1}
	_reader(frappe_stub, masters, **ledger)


def test_tick_with_the_switch_off_reports_the_pre_1_4_shape(frappe_stub):
	dp2 = _Dp2([_page(_wire(max_windows=None))])
	(post,) = _tick(frappe_stub, dp2, _Reader(2))
	assert all("tracking" not in e for e in post["body"]["entries"])


def test_tick_with_the_switch_on_puts_tracking_on_every_entry(frappe_stub):
	masters = _Masters(
		items=[
			{"name": f"ITEM-{i:05d}", "has_batch_no": int(i == 0), "has_expiry_date": int(i == 0)}
			for i in range(2)
		],
		batches=[{"name": "LOT-B1", "item": "ITEM-00000", "expiry_date": "2027-03-31", "disabled": 0}],
	)
	_switch_on(frappe_stub, masters, bundle=[{"item_code": "ITEM-00000", "batch_no": "LOT-B1", "qty": 4.0}])
	dp2 = _Dp2([_page(_wire(max_windows=None))])
	(post,) = _tick(frappe_stub, dp2, _Reader(2))
	first, second = post["body"]["entries"]
	assert first["tracking"]["batches"] == [
		{
			"batchRef": {"doctype": "Batch", "name": "LOT-B1"},
			"quantity": "4.000000",
			"expiryDate": "2027-03-31",
			"disabled": False,
		}
	]
	assert second["tracking"] == {"hasBatchNo": False, "hasSerialNo": False, "hasExpiryDate": False}


def test_tick_batch_limit_reports_nothing_logs_and_is_not_retried(frappe_stub):
	masters = _Masters(
		items=[{"name": "ITEM-00000", "has_batch_no": 1}],
		batches=[
			{"name": f"B{i:03d}", "item": "ITEM-00000", "expiry_date": None, "disabled": 0}
			for i in range(201)
		],
	)
	_switch_on(
		frappe_stub,
		masters,
		bundle=[{"item_code": "ITEM-00000", "batch_no": f"B{i:03d}", "qty": 1.0} for i in range(201)],
	)
	dp2 = _Dp2([_page(_wire(max_windows=None))])
	assert _tick(frappe_stub, dp2, _Reader(1)) == []
	((level, rec),) = _events(frappe_stub, "bin_view.tracking.batch_limit_exceeded")
	assert level == "error"
	assert rec["item_code"] == "ITEM-00000" and rec["batch_count"] == 201 and rec["warehouse"] == "ERP-WH-1"
	assert len(_retry_state(frappe_stub)) == 0
