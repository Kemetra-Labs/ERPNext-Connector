# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Local unit tests for the bin-view worker — pure read-and-report logic.

The ERPNext Bin read is behind a fake :class:`BinReader`; the DP2 report is behind a
fake :class:`BinViewClient` transport. No frappe, no live ERPNext/DP2. The critical
property under test is §III: a float ``actual_qty`` becomes an exact-decimal STRING.
"""

import pytest

from retail_tower_erpnext_connector.connector.bin_view import contracts as c
from retail_tower_erpnext_connector.connector.bin_view import transport as t
from retail_tower_erpnext_connector.connector.bin_view import worker as w

# --- quantize_qty (§III) --------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (10.0, "10.000000"),
        (12.5, "12.500000"),
        (0.0, "0.000000"),
        (-3.0, "-3.000000"),
        (1.2345678, "1.234568"),  # round half-even to 6 dp
        (2.0000005, "2.000000"),  # half-even: rounds to even
    ],
)
def test_quantize_qty_float_to_exact_decimal_string(raw, expected):
    out = w.quantize_qty(raw)
    assert isinstance(out, str)
    assert out == expected


def test_quantize_qty_negative_zero_normalized():
    assert w.quantize_qty(-0.0) == "0.000000"


# --- build_report ---------------------------------------------------------------------


def test_build_report_maps_bins_to_entries_no_valuation():
    bins = [
        w.RawBin(item_code="ITEM-A", actual_qty=7.0, stock_uom="Nos"),
        w.RawBin(item_code="ITEM-B", actual_qty=2.5, stock_uom="kg"),
    ]
    report = w.build_report(bins, read_at="2026-06-08T10:00:00.000Z")
    assert report.read_at == "2026-06-08T10:00:00.000Z"
    assert [e.erpnext_item_ref for e in report.entries] == ["ITEM-A", "ITEM-B"]
    assert [e.quantity for e in report.entries] == ["7.000000", "2.500000"]
    assert [e.stock_uom for e in report.entries] == ["Nos", "kg"]
    # No valuation field anywhere on the wire shape.
    wire = report.to_wire()
    for entry in wire["entries"]:
        assert set(entry.keys()) == {"erpnextItemRef", "quantity", "stockUom"}


def test_build_report_empty_is_valid():
    report = w.build_report([], read_at="2026-06-08T10:00:00.000Z")
    assert report.entries == ()


# --- derive_idempotency_key -----------------------------------------------------------


def test_idempotency_key_deterministic_per_request():
    # Independent expected value: the key is the request ref under the "binview-" prefix, so a
    # re-report of the same request reuses it and a different request gets a different one.
    assert w.derive_idempotency_key("req-1") == "binview-req-1"
    assert w.derive_idempotency_key("req-1") != w.derive_idempotency_key("req-2")


# --- process_request (end-to-end over fakes) ------------------------------------------


class _FakeReader:
    def __init__(self, bins):
        self._bins = bins
        self.calls = []

    def read_bins(self, *, erpnext_warehouse_ref, item_window):
        self.calls.append((erpnext_warehouse_ref, item_window))
        return self._bins


class _FakeClock:
    def now_iso(self):
        return "2026-06-08T12:00:00.000Z"


class _RecordingTransport:
    def __init__(self):
        self.posted = []

    def get(self, path, *, params, headers):  # pragma: no cover - unused here
        return {}

    def post(self, path, *, json, headers):
        self.posted.append((path, json, headers))

        class _R:
            status = 201
            headers = {}  # noqa: RUF012 — read-only fake response, never mutated
            body = {"requestRef": "req-1"}  # noqa: RUF012 — read-only fake response

        return _R()


def _request(ref="req-1", wh="ERP-WH-1"):
    return c.BinViewRequest(
        request_ref=ref,
        store_id="33333333-3333-4333-8333-333333333333",
        erpnext_warehouse_ref=wh,
        run_ref="44444444-4444-4444-8444-444444444444",
        item_window=c.BinViewItemWindow(window_seq=0, max_items=500, from_item_ref=None, to_item_ref=None),
        item_cursor=ref,
    )


def test_process_request_raises_on_window_overflow_no_silent_truncation():
    # Reader returns max_items + 1 rows → the warehouse exceeds the single v1 window.
    # The worker must REFUSE (loud), not report a truncated snapshot as complete.
    overflow = [
        w.RawBin(item_code=f"ITEM-{i}", actual_qty=1.0, stock_uom="Nos") for i in range(501)
    ]
    reader = _FakeReader(overflow)
    transport = _RecordingTransport()
    client = t.BinViewClient(transport, correlation_id="corr-1")
    with pytest.raises(w.WindowOverflowError):
        w.process_request(_request(), client=client, reader=reader, clock=_FakeClock())
    # Nothing was reported (no known-incomplete snapshot posted).
    assert transport.posted == []


def test_process_request_reads_warehouse_and_reports_with_idempotency_key():
    reader = _FakeReader([w.RawBin(item_code="ITEM-A", actual_qty=9.0, stock_uom="Nos")])
    transport = _RecordingTransport()
    client = t.BinViewClient(transport, correlation_id="corr-1")

    result = w.process_request(_request(), client=client, reader=reader, clock=_FakeClock())

    assert result.recorded is True
    # Read the request's warehouse.
    assert reader.calls[0][0] == "ERP-WH-1"
    # Posted to the request's snapshot path with the derived idempotency key + read clock.
    path, body, headers = transport.posted[0]
    assert path == "/api/connector/v1/erpnext/bin-view-requests/req-1/snapshot"
    assert headers[t.IDEMPOTENCY_HEADER] == "binview-req-1"
    assert body["readAt"] == "2026-06-08T12:00:00.000Z"
    assert body["entries"][0]["quantity"] == "9.000000"
