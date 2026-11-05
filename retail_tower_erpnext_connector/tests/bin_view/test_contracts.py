# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-176 — the bin-view DTOs pinned to stock-view 1.2.0-draft (Backend-Core, RT-174).

Additive only: ``itemWindow.maxWindows`` (optional), the report ``window`` object (sent only on
a paged request), and the optional ``RecordedBinView`` progress fields. Every ``from_wire``
stays tolerant of keys it does not know. No frappe.
"""

from retail_tower_erpnext_connector.connector.bin_view import contracts as c

_REF = "11111111-1111-4111-8111-111111111111"


def _wire(item_window: dict) -> dict:
	return {
		"requestRef": _REF,
		"storeId": "33333333-3333-4333-8333-333333333333",
		"erpnextWarehouseRef": "ERP-WH-1",
		"runRef": "44444444-4444-4444-8444-444444444444",
		"itemWindow": item_window,
		"itemCursor": _REF,
	}


def test_v1_item_window_without_max_windows_is_not_paged():
	req = c.BinViewRequest.from_wire(
		_wire({"windowSeq": 0, "maxItems": 500, "fromItemRef": None, "toItemRef": None})
	)
	assert req.item_window.max_windows is None
	assert req.item_window.is_paged is False


def test_max_windows_one_is_the_v1_single_window():
	req = c.BinViewRequest.from_wire(_wire({"windowSeq": 0, "maxItems": 500, "maxWindows": 1}))
	assert req.item_window.max_windows == 1
	assert req.item_window.is_paged is False


def test_max_windows_above_one_is_paged():
	req = c.BinViewRequest.from_wire(
		_wire({"windowSeq": 0, "maxItems": 500, "maxWindows": 20, "fromItemRef": None, "toItemRef": None})
	)
	assert req.item_window.max_windows == 20
	assert req.item_window.is_paged is True


def test_from_wire_ignores_unknown_keys():
	wire = _wire({"windowSeq": 0, "maxItems": 500, "maxWindows": 3, "futureKnob": "x"})
	wire["futureField"] = {"a": 1}
	req = c.BinViewRequest.from_wire(wire)
	assert req.request_ref == _REF
	assert req.item_window.max_windows == 3


def test_request_round_trips_through_its_wire_shape():
	for window in (
		{"windowSeq": 0, "maxItems": 500, "fromItemRef": None, "toItemRef": None},
		{"windowSeq": 0, "maxItems": 500, "fromItemRef": None, "toItemRef": None, "maxWindows": 20},
	):
		req = c.BinViewRequest.from_wire(_wire(window))
		assert req.to_wire() == _wire(window)
		assert c.BinViewRequest.from_wire(req.to_wire()) == req


def test_v1_report_wire_has_no_window_key():
	report = c.BinViewSnapshotReport(entries=(), read_at="2026-06-08T10:00:00.000Z")
	assert report.to_wire() == {"entries": [], "readAt": "2026-06-08T10:00:00.000Z"}


def test_paged_report_wire_carries_the_window_object():
	report = c.BinViewSnapshotReport(
		entries=(c.BinEntry(erpnext_item_ref="ITEM-A", quantity="1.000000", stock_uom="Nos"),),
		read_at="2026-06-08T10:00:00.000Z",
		window=c.BinViewReportWindow(attempt_ref="aaaa", window_seq=2, is_final=True),
	)
	wire = report.to_wire()
	assert wire["window"] == {"attemptRef": "aaaa", "windowSeq": 2, "isFinal": True}
	assert set(wire) == {"entries", "readAt", "window"}


def test_recorded_view_reads_v12_progress_fields():
	view = c.RecordedBinView.from_wire(
		{
			"requestRef": _REF,
			"runRef": "r",
			"erpnextWarehouseRef": "ERP-WH-1",
			"acceptedEntryCount": 500,
			"readAt": "t",
			"recordedAt": "t2",
			"windowSeq": 1,
			"windowsRecorded": 2,
			"complete": False,
			"somethingNew": True,
		}
	)
	assert (view.request_ref, view.accepted_entry_count) == (_REF, 500)
	assert (view.window_seq, view.windows_recorded, view.complete) == (1, 2, False)


def test_recorded_view_is_tolerant_of_v1_and_empty_bodies():
	v1 = c.RecordedBinView.from_wire({"requestRef": _REF, "acceptedEntryCount": 3})
	assert (v1.window_seq, v1.windows_recorded, v1.complete) == (None, None, None)
	empty = c.RecordedBinView.from_wire({})
	assert empty.complete is None
	assert c.RecordedBinView.from_wire(None).request_ref is None
	# Wrong-typed values are ignored rather than coerced.
	odd = c.RecordedBinView.from_wire({"complete": "true", "windowSeq": True})
	assert (odd.complete, odd.window_seq) == (None, None)
