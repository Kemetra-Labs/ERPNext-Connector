# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1287 — the pure stock import read rule (``stock-import.yaml`` 1.0.0-draft), frappe-free.

A fake :class:`LedgerSource` stands in for ERPNext: it orders and filters ledger rows as the
frappe glue's SQL does (keyset on (``creation``, ``name``), ``creation`` ≤ cutoff). Covered: the
acceptance-criteria voucher families (Purchase Receipt and its return, Stock Entry Manufacture and
Repack, opening Stock Reconciliation, a cancelled voucher), batch rows, the provenance flag,
facts-only reporting of every voucher type, paging inside a ledger row, the settle cutoff, the
overlap re-read (same request → identical report), the refusal, and the time rendering.
"""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from retail_tower_erpnext_connector.connector.stock_import import reader as r
from retail_tower_erpnext_connector.connector.stock_import.contracts import LedgerKey, StockImportRequest

_WH = "Stores - RT"
_NOW = datetime(2026, 11, 11, 12, 0, 0)
_ZONE = ZoneInfo("Africa/Cairo")
_QTY_RE = re.compile(r"^-?[0-9]{1,15}(\.[0-9]{1,6})?$", re.ASCII)
_TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.[0-9]{1,6})?$", re.ASCII)
_ROW_KEYS = {
	"ledgerEntryRef",
	"entryIndex",
	"voucherType",
	"voucherNo",
	"voucherDetailNo",
	"purpose",
	"isReturn",
	"returnAgainst",
	"postingDate",
	"postingTime",
	"creation",
	"isCancelled",
	"actualQty",
	"stockUom",
	"erpnextItemRef",
	"erpnextBatchRef",
	"hasRetailProvenance",
	"correctionRef",
}


class FakeSource:
	def __init__(self, entries=(), bundles=None, facts=None, warehouses=(_WH,), now=_NOW, zone=_ZONE):
		self.entries = list(entries)
		self.bundles = bundles or {}
		self.facts = facts or {}
		self.warehouses = set(warehouses)
		self.now = now
		self.zone = zone
		self.reads = []

	def warehouse_exists(self, warehouse):
		return warehouse in self.warehouses

	def site_now(self):
		return self.now

	def site_timezone(self):
		return self.zone

	def read_ledger(self, warehouse, *, start, cutoff, limit):
		self.reads.append((start, cutoff, limit))
		rows = sorted(
			(e for e in self.entries if e[0] == warehouse), key=lambda e: (e[1].creation, e[1].name)
		)
		out = []
		for _, e in rows:
			if e.creation > cutoff:
				continue
			if start is not None:
				if start.name is None:
					if e.creation < start.creation:
						continue
				else:
					pos, s = (e.creation, e.name), (start.creation, start.name)
					if pos < s or (pos == s and not start.inclusive):
						continue
			out.append(e)
		return out[:limit]

	def bundle_entries(self, bundles):
		return {b: self.bundles[b] for b in bundles if b in self.bundles}

	def voucher_facts(self, vouchers):
		return {v: self.facts[v] for v in vouchers if v in self.facts}


def sle(name, creation, *, vt="Purchase Receipt", vno="PR-1", detail="d1", qty=10.0, item="ITEM-1", **kw):
	return (
		kw.pop("warehouse", _WH),
		r.RawLedgerEntry(
			name=name,
			creation=creation,
			voucher_type=vt,
			voucher_no=vno,
			voucher_detail_no=detail,
			posting_date=kw.pop("posting_date", date(2026, 11, 11)),
			posting_time=kw.pop("posting_time", timedelta(hours=9, minutes=5, seconds=7)),
			is_cancelled=kw.pop("is_cancelled", False),
			actual_qty=qty,
			stock_uom=kw.pop("uom", "Nos"),
			item_code=item,
			batch_no=kw.pop("batch_no", None),
			serial_and_batch_bundle=kw.pop("bundle", None),
		),
	)


def request(after=None, max_rows=100, settle=300, warehouse=_WH):
	return StockImportRequest(
		request_ref="11111111-1111-4111-8111-111111111111",
		run_ref="22222222-2222-4222-8222-222222222222",
		page_seq=0,
		store_id="33333333-3333-4333-8333-333333333333",
		erpnext_warehouse_ref=warehouse,
		after=after,
		settle_seconds=settle,
		overlap_seconds=0,
		max_rows=max_rows,
		item_cursor="c-1",
	)


def at(minute, second=0, micro=0):
	return datetime(2026, 11, 11, 10, minute, second, micro)


def report(source, req=None):
	return r.read_report(source, req or request(), read_at="2026-11-11T10:00:00.000Z")


def wire_rows(rep):
	return [row.to_wire() for row in rep.rows]


# --- voucher families (facts only) ----------------------------------------------------------------


def test_purchase_receipt_with_new_batch_is_reported_with_its_batch():
	src = FakeSource(
		[sle("SLE-1", at(1), qty=12, bundle="SBB-1")],
		bundles={"SBB-1": [r.RawBundleEntry("BATCH-NEW", 12.0)]},
		facts={("Purchase Receipt", "PR-1"): r.VoucherFacts()},
	)
	[row] = wire_rows(report(src))
	assert row["voucherType"] == "Purchase Receipt"
	assert row["erpnextBatchRef"] == {"doctype": "Batch", "name": "BATCH-NEW"}
	assert row["actualQty"] == "12.000000"
	assert row["entryIndex"] == 0
	assert row["isReturn"] is False and row["returnAgainst"] is None
	assert row["hasRetailProvenance"] is False


def test_purchase_return_is_reported_negative_with_its_return_link():
	src = FakeSource(
		[sle("SLE-1", at(1), vno="PR-RET-1", qty=-3, bundle="SBB-R")],
		bundles={"SBB-R": [r.RawBundleEntry("BATCH-A", -3.0)]},
		facts={("Purchase Receipt", "PR-RET-1"): r.VoucherFacts(is_return=True, return_against="PR-1")},
	)
	[row] = wire_rows(report(src))
	assert row["isReturn"] is True
	assert row["returnAgainst"] == "PR-1"
	assert row["actualQty"] == "-3.000000"


@pytest.mark.parametrize("purpose", ["Manufacture", "Repack"])
def test_stock_entry_purpose_is_reported(purpose):
	src = FakeSource(
		[sle("SLE-1", at(1), vt="Stock Entry", vno="STE-1", qty=5)],
		facts={("Stock Entry", "STE-1"): r.VoucherFacts(purpose=purpose)},
	)
	[row] = wire_rows(report(src))
	assert (row["voucherType"], row["purpose"]) == ("Stock Entry", purpose)


def test_opening_stock_reconciliation_reports_the_ledger_actual_qty():
	src = FakeSource(
		[sle("SLE-1", at(1), vt="Stock Reconciliation", vno="MAT-RECO-1", qty=7.5)],
		facts={("Stock Reconciliation", "MAT-RECO-1"): r.VoucherFacts(purpose="Opening Stock")},
	)
	[row] = wire_rows(report(src))
	assert row["purpose"] == "Opening Stock"
	assert row["actualQty"] == "7.500000"  # the SLE's actual_qty as read (the delta: BENCH item)


def test_cancelled_voucher_reports_original_and_reverse_rows_in_read_order():
	src = FakeSource(
		[
			sle("SLE-REV", at(5), qty=-10, is_cancelled=True),
			sle("SLE-ORIG", at(1), qty=10, is_cancelled=True),
		],
	)
	rows = wire_rows(report(src))
	assert [(x["ledgerEntryRef"], x["isCancelled"], x["actualQty"]) for x in rows] == [
		("SLE-ORIG", True, "10.000000"),
		("SLE-REV", True, "-10.000000"),
	]
	assert rows[0]["voucherDetailNo"] == rows[1]["voucherDetailNo"] == "d1"


def test_every_voucher_type_is_reported_and_provenance_is_a_flag_only():
	src = FakeSource(
		[
			sle("SLE-1", at(1), vt="Sales Invoice", vno="SINV-1", qty=-1),
			sle("SLE-2", at(2), vt="Stock Entry", vno="STE-9", qty=-2),
		],
		facts={
			("Sales Invoice", "SINV-1"): r.VoucherFacts(is_return=False, has_retail_provenance=True),
			("Stock Entry", "STE-9"): r.VoucherFacts(purpose="Material Issue"),
		},
	)
	rows = wire_rows(report(src))
	assert [(x["voucherType"], x["hasRetailProvenance"]) for x in rows] == [
		("Sales Invoice", True),
		("Stock Entry", False),
	]


def test_correction_reference_is_reported_as_read():
	src = FakeSource(
		[sle("SLE-1", at(1), vt="Stock Reconciliation", vno="RECO-2", qty=1)],
		facts={
			("Stock Reconciliation", "RECO-2"): r.VoucherFacts(
				purpose="Stock Reconciliation", correction_ref="BG-7"
			)
		},
	)
	[row] = wire_rows(report(src))
	assert row["correctionRef"] == "BG-7"


def test_unknown_voucher_facts_report_the_does_not_apply_values():
	[row] = wire_rows(report(FakeSource([sle("SLE-1", at(1), vt="Delivery Note", vno="DN-1", detail="")])))
	assert row["purpose"] is None and row["isReturn"] is False and row["correctionRef"] is None
	assert row["voucherDetailNo"] is None  # ERPNext left it empty: reported as null


# --- batches --------------------------------------------------------------------------------------


def test_bundle_with_several_batches_gives_one_row_per_entry_with_the_ledger_sign():
	src = FakeSource(
		[sle("SLE-1", at(1), vt="Sales Invoice", vno="SINV-1", qty=-5, bundle="SBB-1")],
		bundles={"SBB-1": [r.RawBundleEntry("B1", -2.0), r.RawBundleEntry("B2", 3.0)]},
	)
	rows = wire_rows(report(src))
	assert [(x["entryIndex"], x["erpnextBatchRef"]["name"], x["actualQty"]) for x in rows] == [
		(0, "B1", "-2.000000"),
		(1, "B2", "-3.000000"),
	]
	assert {x["ledgerEntryRef"] for x in rows} == {"SLE-1"}


def test_legacy_batch_no_without_bundle_is_used():
	[row] = wire_rows(report(FakeSource([sle("SLE-1", at(1), batch_no="OLD-B")])))
	assert row["erpnextBatchRef"] == {"doctype": "Batch", "name": "OLD-B"}


def test_serial_only_bundle_gives_one_row_with_no_batch():
	src = FakeSource(
		[sle("SLE-1", at(1), qty=2, bundle="SBB-S")],
		bundles={"SBB-S": [r.RawBundleEntry(None, 1.0), r.RawBundleEntry(None, 1.0)]},
	)
	[row] = wire_rows(report(src))
	assert (row["erpnextBatchRef"], row["entryIndex"], row["actualQty"]) == (None, 0, "2.000000")


# --- paging, settle and overlap -------------------------------------------------------------------


def _many():
	return FakeSource(
		[
			sle("SLE-A", at(1), qty=1),
			sle("SLE-B", at(2), qty=6, bundle="SBB-B"),
			sle("SLE-C", at(3), qty=1),
		],
		bundles={"SBB-B": [r.RawBundleEntry(f"B{i}", 2.0) for i in range(3)]},
	)


def _positions(rep):
	return [(x.ledger_entry_ref, x.entry_index) for x in rep.rows]


def test_pages_may_end_inside_a_ledger_row_and_continue_after_read_through():
	src = _many()
	first = report(src, request(max_rows=2))
	assert _positions(first) == [("SLE-A", 0), ("SLE-B", 0)]
	assert first.exhausted is False
	assert first.read_through == LedgerKey(r.render_creation(at(2), _ZONE), "SLE-B", 0)
	second = report(src, request(after=first.read_through, max_rows=2))
	assert _positions(second) == [("SLE-B", 1), ("SLE-B", 2)]
	assert second.exhausted is False
	third = report(src, request(after=second.read_through, max_rows=2))
	assert _positions(third) == [("SLE-C", 0)]
	assert third.exhausted is True


def test_exactly_max_rows_with_nothing_after_is_exhausted():
	rep = report(FakeSource([sle("SLE-A", at(1)), sle("SLE-B", at(2))]), request(max_rows=2))
	assert len(rep.rows) == 2 and rep.exhausted is True


def test_empty_page_is_exhausted_with_null_read_through():
	rep = report(FakeSource([]))
	assert rep.rows == () and rep.read_through is None and rep.exhausted is True
	assert rep.to_wire()["refusal"] is None


def test_rows_newer_than_the_settle_cutoff_are_not_read():
	src = FakeSource(
		[sle("SLE-OLD", _NOW - timedelta(seconds=301)), sle("SLE-NEW", _NOW - timedelta(seconds=299))]
	)
	rep = report(src, request(settle=300))
	assert _positions(rep) == [("SLE-OLD", 0)]
	assert src.reads[0][1] == _NOW - timedelta(seconds=300)


def test_overlap_reread_with_null_ledger_ref_starts_at_creation_inclusive():
	src = _many()
	after = LedgerKey(r.render_creation(at(2), _ZONE), None, None)
	assert _positions(report(src, request(after=after))) == [
		("SLE-B", 0),
		("SLE-B", 1),
		("SLE-B", 2),
		("SLE-C", 0),
	]


def test_overlap_reread_is_idempotent_same_request_same_body():
	src = _many()
	req = request(after=LedgerKey(r.render_creation(at(1), _ZONE), None, None))
	assert report(src, req).to_wire() == report(src, req).to_wire()


def test_after_in_another_offset_is_converted_to_site_time():
	src = _many()
	utc = datetime(2026, 11, 11, 8, 2, tzinfo=ZoneInfo("UTC")).isoformat()  # = 10:02 Cairo (UTC+2)
	rep = report(src, request(after=LedgerKey(utc, "SLE-B", 1)))
	assert _positions(rep) == [("SLE-B", 2), ("SLE-C", 0)]


def test_long_ledger_is_read_in_chunks_without_skipping():
	entries = [sle(f"SLE-{i:03d}", at(1, i % 60, i), qty=1) for i in range(30)]
	src = FakeSource(entries)
	rep = report(src, request(max_rows=25))
	assert [x.ledger_entry_ref for x in rep.rows] == sorted(e[1].name for e in entries)[:25]
	assert rep.exhausted is False


def test_other_warehouses_are_not_read():
	src = FakeSource([sle("SLE-X", at(1), warehouse="Other - RT"), sle("SLE-1", at(2))])
	assert _positions(report(src)) == [("SLE-1", 0)]


# --- refusal --------------------------------------------------------------------------------------


def test_unknown_warehouse_is_refused_and_nothing_is_read():
	src = FakeSource([sle("SLE-1", at(1))], warehouses=())
	rep = report(src)
	assert rep.to_wire() == {
		"rows": [],
		"readThrough": None,
		"exhausted": False,
		"readAt": "2026-11-11T10:00:00.000Z",
		"refusal": {"reason": "unknown_warehouse"},
	}
	assert src.reads == []


# --- wire shape and rendering ---------------------------------------------------------------------


def test_row_wire_has_every_field_and_valid_formats():
	src = FakeSource(
		[
			sle(
				"SLE-1",
				at(1, 2, 345),
				qty=1.23456789,
				posting_time=timedelta(hours=23, seconds=59, microseconds=5),
			)
		]
	)
	[row] = wire_rows(report(src))
	assert set(row) == _ROW_KEYS
	assert _QTY_RE.match(row["actualQty"]) and row["actualQty"] == "1.234568"
	assert _TIME_RE.match(row["postingTime"]) and row["postingTime"] == "23:00:59.000005"
	assert row["postingDate"] == "2026-11-11"
	assert row["creation"] == "2026-11-11T10:01:02.000345+02:00"
	assert row["erpnextItemRef"] == {"doctype": "Item", "name": "ITEM-1"}


def test_creation_in_a_repeated_dst_hour_uses_the_earlier_offset():
	zone = ZoneInfo("Africa/Cairo")  # 2026-10-30 00:00 local falls back to 2026-10-29 23:00
	text = r.render_creation(datetime(2026, 10, 29, 23, 30), zone)
	assert text == "2026-10-29T23:30:00.000000+03:00"
	assert r.to_site_time(text, zone) == datetime(2026, 10, 29, 23, 30)


def test_to_site_time_refuses_a_naive_value():
	with pytest.raises(ValueError):
		r.to_site_time("2026-11-11T10:00:00", _ZONE)


@pytest.mark.parametrize(
	("value", "expected"),
	[("09:05:07", "09:05:07"), ("09:05:07.250000", "09:05:07.250000"), (timedelta(seconds=5), "00:00:05")],
)
def test_posting_time_rendering(value, expected):
	assert r.render_posting_time(value) == expected


def test_posting_time_out_of_range_is_refused():
	with pytest.raises(ValueError):
		r.render_posting_time(timedelta(hours=24))


def test_posting_date_accepts_a_string_or_datetime():
	assert r.render_posting_date("2026-11-11") == "2026-11-11"
	assert r.render_posting_date(datetime(2026, 11, 11, 3)) == "2026-11-11"


def test_zero_quantity_is_not_negative():
	src = FakeSource(
		[sle("SLE-1", at(1), qty=-0.0000001, bundle="SBB")], bundles={"SBB": [r.RawBundleEntry("B", 0.0)]}
	)
	[row] = wire_rows(report(src))
	assert row["actualQty"] == "0.000000"
