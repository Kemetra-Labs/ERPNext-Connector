# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-918 — stock-view 1.4 ``BinEntry.tracking`` evidence, pure worker + read-model tests.

No frappe: the tracking read is behind a fake :class:`TrackingReader`, the DP2 report behind a
recording transport. Covered: the 1.4 wire shapes, the per-Item evidence (batch Item with Batches,
untracked Item, serial Item, batch Item with no Batch here), the refusals that report nothing
(more than 200 Batches, missing Item flags), and that a report without a tracking reader is the
pre-1.4 shape byte for byte.
"""

import pytest

from retail_tower_erpnext_connector.connector.bin_view import contracts as c
from retail_tower_erpnext_connector.connector.bin_view import transport as t
from retail_tower_erpnext_connector.connector.bin_view import worker as w

_READ_AT = "2026-11-08T12:00:00.000Z"


def _flags(code, batch=False, serial=False, expiry=False):
	return w.RawItemTracking(item_code=code, has_batch_no=batch, has_serial_no=serial, has_expiry_date=expiry)


def _batch(code, name, qty, expiry=None, disabled=False):
	return w.RawBatch(item_code=code, batch_no=name, qty=qty, expiry_date=expiry, disabled=disabled)


# --- 1.4 wire shapes ------------------------------------------------------------------------------


def test_batch_wire_shape_matches_bin_entry_batch():
	wire = c.BinEntryBatch(
		batch_ref="B1", quantity="4.000000", expiry_date="2027-03-31", disabled=False
	).to_wire()
	assert wire == {
		"batchRef": {"doctype": "Batch", "name": "B1"},
		"quantity": "4.000000",
		"expiryDate": "2027-03-31",
		"disabled": False,
	}


def test_batch_with_no_expiry_sends_an_explicit_null():
	wire = c.BinEntryBatch(batch_ref="B2", quantity="0.000000", expiry_date=None, disabled=True).to_wire()
	assert "expiryDate" in wire and wire["expiryDate"] is None


def test_tracking_without_batches_omits_the_key():
	wire = c.BinEntryTracking(has_batch_no=False, has_serial_no=False, has_expiry_date=False).to_wire()
	assert wire == {"hasBatchNo": False, "hasSerialNo": False, "hasExpiryDate": False}


def test_entry_without_tracking_is_the_pre_1_4_shape():
	wire = c.BinEntry(erpnext_item_ref="ITEM-A", quantity="1.000000", stock_uom="Nos").to_wire()
	assert set(wire) == {"erpnextItemRef", "quantity", "stockUom"}


def test_entry_with_tracking_carries_it():
	tracking = c.BinEntryTracking(has_batch_no=True, has_serial_no=False, has_expiry_date=True)
	wire = c.BinEntry(
		erpnext_item_ref="ITEM-A", quantity="1.000000", stock_uom="Nos", tracking=tracking
	).to_wire()
	assert wire["tracking"] == {"hasBatchNo": True, "hasSerialNo": False, "hasExpiryDate": True}


# --- build_tracking -------------------------------------------------------------------------------


def test_batch_item_lists_its_batches_by_name_with_book_qty_expiry_and_disabled():
	read = w.RawTrackingRead(
		items=(_flags("MED", batch=True, expiry=True),),
		batches=(
			_batch("MED", "B2", -1.0, expiry=None, disabled=True),
			_batch("MED", "B1", 12.5, expiry="2027-03-31"),
			_batch("MED", "B0", 0.0, expiry="2026-01-01"),  # used up: omitted
			_batch("MED", "B9", 1e-9, expiry="2026-01-01"),  # zero at 6 dp: omitted
			_batch("MED", "BX", 1.0, expiry="2020-01-01", disabled=True),  # expired + disabled: listed
		),
	)
	(tracking,) = w.build_tracking(["MED"], read).values()
	assert tracking.to_wire() == {
		"hasBatchNo": True,
		"hasSerialNo": False,
		"hasExpiryDate": True,
		"batches": [
			{
				"batchRef": {"doctype": "Batch", "name": "B1"},
				"quantity": "12.500000",
				"expiryDate": "2027-03-31",
				"disabled": False,
			},
			{
				"batchRef": {"doctype": "Batch", "name": "B2"},
				"quantity": "-1.000000",
				"expiryDate": None,
				"disabled": True,
			},
			{
				"batchRef": {"doctype": "Batch", "name": "BX"},
				"quantity": "1.000000",
				"expiryDate": "2020-01-01",
				"disabled": True,
			},
		],
	}


def test_zero_batches_do_not_count_toward_the_200_cap():
	read = w.RawTrackingRead(
		items=(_flags("BREAD", batch=True),),
		batches=(
			*(_batch("BREAD", f"Z{i:03d}", 0.0) for i in range(250)),
			_batch("BREAD", "N1", 3.0),
			_batch("BREAD", "N2", -1.0),
			_batch("BREAD", "N3", 0.5),
		),
	)
	assert [b.batch_ref for b in w.build_tracking(["BREAD"], read)["BREAD"].batches] == ["N1", "N2", "N3"]


def test_a_batch_item_whose_batches_are_all_used_up_omits_batches():
	read = w.RawTrackingRead(items=(_flags("MED", batch=True),), batches=(_batch("MED", "B1", 0.0),))
	assert "batches" not in w.build_tracking(["MED"], read)["MED"].to_wire()


def test_untracked_item_carries_three_false_flags_and_no_batches():
	read = w.RawTrackingRead(items=(_flags("SOAP"),), batches=())
	assert w.build_tracking(["SOAP"], read)["SOAP"].to_wire() == {
		"hasBatchNo": False,
		"hasSerialNo": False,
		"hasExpiryDate": False,
	}


def test_serial_item_is_reported_with_its_flag_only():
	read = w.RawTrackingRead(items=(_flags("PHONE", serial=True),), batches=())
	assert w.build_tracking(["PHONE"], read)["PHONE"].to_wire() == {
		"hasBatchNo": False,
		"hasSerialNo": True,
		"hasExpiryDate": False,
	}


def test_batch_item_with_no_batch_in_the_warehouse_omits_batches():
	read = w.RawTrackingRead(items=(_flags("MED", batch=True),), batches=())
	assert "batches" not in w.build_tracking(["MED"], read)["MED"].to_wire()


def test_batch_rows_of_a_non_batch_item_are_ignored():
	read = w.RawTrackingRead(items=(_flags("SOAP"),), batches=(_batch("SOAP", "OLD-B", 3.0),))
	assert "batches" not in w.build_tracking(["SOAP"], read)["SOAP"].to_wire()


def test_batch_qty_is_an_exact_decimal_string():
	read = w.RawTrackingRead(items=(_flags("MED", batch=True),), batches=(_batch("MED", "B1", 1.2345678),))
	(batch,) = w.build_tracking(["MED"], read)["MED"].batches
	assert batch.quantity == "1.234568"


def test_missing_item_flags_is_a_read_error_never_untracked():
	read = w.RawTrackingRead(items=(_flags("A"),), batches=())
	with pytest.raises(w.TrackingReadError):
		w.build_tracking(["A", "B"], read)


def test_exactly_200_batches_is_accepted():
	read = w.RawTrackingRead(
		items=(_flags("MED", batch=True),),
		batches=tuple(_batch("MED", f"B{i:03d}", 1.0) for i in range(200)),
	)
	assert len(w.build_tracking(["MED"], read)["MED"].batches) == 200


def test_more_than_200_batches_is_refused():
	read = w.RawTrackingRead(
		items=(_flags("MED", batch=True),),
		batches=tuple(_batch("MED", f"B{i:03d}", 1.0) for i in range(201)),
	)
	with pytest.raises(w.BatchLimitExceeded) as raised:
		w.build_tracking(["MED"], read)
	assert raised.value.item_code == "MED" and raised.value.batch_count == 201


# --- process_request over fakes -------------------------------------------------------------------


class _Reader:
	def __init__(self, codes):
		self.bins = [w.RawBin(item_code=code, actual_qty=2.0, stock_uom="Nos") for code in codes]

	def read_bins(self, *, erpnext_warehouse_ref, item_window):
		return self.bins[: item_window.max_items + 1]

	def read_bins_paged(self, *, erpnext_warehouse_ref, page_size, max_rows):
		return self.bins[:max_rows]


class _TrackingReader:
	"""Every Item untracked except those named in ``batch_items`` (each with ``per_item`` Batches)."""

	def __init__(self, batch_items=(), per_item=1):
		self.batch_items = set(batch_items)
		self.per_item = per_item
		self.calls = []

	def read_tracking(self, *, erpnext_warehouse_ref, item_codes):
		self.calls.append((erpnext_warehouse_ref, list(item_codes)))
		items = tuple(_flags(code, batch=code in self.batch_items) for code in item_codes)
		batches = tuple(
			_batch(code, f"{code}-B{i}", 1.0, expiry="2027-01-31")
			for code in item_codes
			if code in self.batch_items
			for i in range(self.per_item)
		)
		return w.RawTrackingRead(items=items, batches=batches)


class _Clock:
	def now_iso(self):
		return _READ_AT


class _Transport:
	def __init__(self):
		self.posted = []

	def get(self, path, *, params, headers):  # pragma: no cover - unused here
		return {}

	def post(self, path, *, json, headers):
		self.posted.append(json)

		class _R:
			status = 201
			headers = {}  # noqa: RUF012 — read-only fake response
			body = {}  # noqa: RUF012 — read-only fake response

		return _R()


def _request(max_items=500, max_windows=None):
	return c.BinViewRequest(
		request_ref="req-1",
		store_id="33333333-3333-4333-8333-333333333333",
		erpnext_warehouse_ref="ERP-WH-1",
		run_ref="44444444-4444-4444-8444-444444444444",
		item_window=c.BinViewItemWindow(
			window_seq=0, max_items=max_items, from_item_ref=None, to_item_ref=None, max_windows=max_windows
		),
		item_cursor="req-1",
	)


def _run(request, reader, tracking_reader=None):
	transport = _Transport()
	w.process_request(
		request,
		client=t.BinViewClient(transport, correlation_id="corr"),
		reader=reader,
		clock=_Clock(),
		attempt_ref_factory=lambda: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
		tracking_reader=tracking_reader,
	)
	return transport.posted


def test_v1_report_without_a_tracking_reader_is_unchanged():
	(body,) = _run(_request(), _Reader(["A", "B"]))
	assert body == {
		"entries": [
			{"erpnextItemRef": {"doctype": "Item", "name": "A"}, "quantity": "2.000000", "stockUom": "Nos"},
			{"erpnextItemRef": {"doctype": "Item", "name": "B"}, "quantity": "2.000000", "stockUom": "Nos"},
		],
		"readAt": _READ_AT,
	}


def test_v1_report_with_a_tracking_reader_puts_tracking_on_every_entry():
	tracking_reader = _TrackingReader(batch_items={"MED"})
	(body,) = _run(_request(), _Reader(["MED", "SOAP"]), tracking_reader)
	med, soap = body["entries"]
	assert med["tracking"]["batches"] == [
		{
			"batchRef": {"doctype": "Batch", "name": "MED-B0"},
			"quantity": "1.000000",
			"expiryDate": "2027-01-31",
			"disabled": False,
		}
	]
	assert soap["tracking"] == {"hasBatchNo": False, "hasSerialNo": False, "hasExpiryDate": False}
	assert tracking_reader.calls == [("ERP-WH-1", ["MED", "SOAP"])]


def test_paged_report_puts_tracking_on_every_window_and_reads_one_window_of_items_at_a_time():
	codes = [f"I-{i}" for i in range(5)]
	tracking_reader = _TrackingReader(batch_items={"I-3"})
	posts = _run(_request(max_items=2, max_windows=3), _Reader(codes), tracking_reader)
	assert [len(p["entries"]) for p in posts] == [2, 2, 1]
	assert all("tracking" in e for p in posts for e in p["entries"])
	assert [call[1] for call in tracking_reader.calls] == [["I-0", "I-1"], ["I-2", "I-3"], ["I-4"]]


def test_paged_batch_limit_refuses_before_window_0_so_nothing_is_reported():
	codes = [f"I-{i}" for i in range(5)]
	# The over-limit Item sits in the LAST window: the refusal must still come before window 0.
	tracking_reader = _TrackingReader(batch_items={"I-4"}, per_item=201)
	transport = _Transport()
	with pytest.raises(w.BatchLimitExceeded):
		w.process_request(
			_request(max_items=2, max_windows=3),
			client=t.BinViewClient(transport, correlation_id="corr"),
			reader=_Reader(codes),
			clock=_Clock(),
			tracking_reader=tracking_reader,
		)
	assert transport.posted == []


def test_v1_tracking_read_error_reports_nothing():
	class _Broken(_TrackingReader):
		def read_tracking(self, *, erpnext_warehouse_ref, item_codes):
			return w.RawTrackingRead(items=(), batches=())

	transport = _Transport()
	with pytest.raises(w.TrackingReadError):
		w.process_request(
			_request(),
			client=t.BinViewClient(transport, correlation_id="corr"),
			reader=_Reader(["A"]),
			clock=_Clock(),
			tracking_reader=_Broken(),
		)
	assert transport.posted == []


def test_build_report_refuses_an_item_missing_from_a_given_tracking_map():
	with pytest.raises(w.TrackingReadError):
		w.build_report([w.RawBin("A", 1.0, "Nos")], read_at=_READ_AT, tracking={})
