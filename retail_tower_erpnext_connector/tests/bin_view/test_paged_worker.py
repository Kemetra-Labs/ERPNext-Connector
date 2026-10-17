# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-176 — connector-paged bin-view reports (stock-view 1.2), pure worker over fakes.

A request advertising ``itemWindow.maxWindows > 1`` is read in ONE attempt and reported as
windows 0..N of at most ``maxItems`` entries, sharing one ``attemptRef`` and one ``readAt``,
``isFinal`` on window N only, each keyed ``binview-{requestRef}-{attemptRef}-w{seq}``. Above
``maxWindows x maxItems`` nothing is reported. Without ``maxWindows`` (or with 1) the v1 path
is unchanged. No frappe, no live DP2/ERPNext.
"""

import uuid
from decimal import Decimal

import pytest

from retail_tower_erpnext_connector.connector.bin_view import contracts as c
from retail_tower_erpnext_connector.connector.bin_view import transport as t
from retail_tower_erpnext_connector.connector.bin_view import worker as w

_REF = "11111111-1111-4111-8111-111111111111"
_READ_AT = "2026-10-04T08:00:00.000Z"


class _Reader:
	"""Fake BinReader over a fixed, item_code-ordered warehouse; honors the paged-read bound."""

	def __init__(self, bins):
		self._bins = list(bins)
		self.v1_calls = []
		self.paged_calls = []

	def read_bins(self, *, erpnext_warehouse_ref, item_window):
		self.v1_calls.append((erpnext_warehouse_ref, item_window))
		return self._bins[: int(item_window.max_items) + 1]

	def read_bins_paged(self, *, erpnext_warehouse_ref, page_size, max_rows):
		self.paged_calls.append((erpnext_warehouse_ref, page_size, max_rows))
		return self._bins[:max_rows]


class _Clock:
	def __init__(self):
		self.calls = 0

	def now_iso(self):
		self.calls += 1
		return _READ_AT if self.calls == 1 else f"2026-10-04T08:00:0{self.calls}.000Z"


class _Transport:
	def __init__(self, responses=None):
		self.posted = []
		self._responses = list(responses or [])

	def get(self, path, *, params, headers):  # pragma: no cover - unused
		return {}

	def post(self, path, *, json, headers):
		self.posted.append({"path": path, "body": json, "key": headers[t.IDEMPOTENCY_HEADER]})
		if self._responses:
			item = self._responses.pop(0)
			if isinstance(item, Exception):
				raise item
			return item
		return _resp()


def _resp(status=201, body=None):
	return type("_R", (), {"status": status, "headers": {}, "body": body or {}})()


def _bins(n, qty=lambda i: i + 0.25):
	return [w.RawBin(item_code=f"ITEM-{i:05d}", actual_qty=qty(i), stock_uom="Nos") for i in range(n)]


def _request(max_windows=20, max_items=500):
	return c.BinViewRequest(
		request_ref=_REF,
		store_id="33333333-3333-4333-8333-333333333333",
		erpnext_warehouse_ref="ERP-WH-1",
		run_ref="44444444-4444-4444-8444-444444444444",
		item_window=c.BinViewItemWindow(
			window_seq=0, max_items=max_items, from_item_ref=None, to_item_ref=None, max_windows=max_windows
		),
		item_cursor=_REF,
	)


def _run(bins, request, transport=None):
	transport = transport or _Transport()
	reader = _Reader(bins)
	result = w.process_request(
		request, client=t.BinViewClient(transport, correlation_id="corr"), reader=reader, clock=_Clock()
	)
	return result, transport, reader


# --- AC1: 1,037 rows, maxWindows 20 → 3 windows -----------------------------------------


def _windows(transport):
	return [p["body"]["window"] for p in transport.posted]


def _entries(transport):
	return [e for p in transport.posted for e in p["body"]["entries"]]


def _assert_one_attempt_in_order(transport, *, sizes):
	"""Windows 0..N in order, isFinal on N only, one uuid4 attemptRef + one readAt; returns it."""
	windows = _windows(transport)
	last = len(sizes) - 1
	assert [win["windowSeq"] for win in windows] == list(range(len(sizes)))
	assert [win["isFinal"] for win in windows] == [seq == last for seq in range(len(sizes))]
	assert [len(p["body"]["entries"]) for p in transport.posted] == sizes
	attempt_refs = {win["attemptRef"] for win in windows}
	assert len(attempt_refs) == 1
	attempt_ref = attempt_refs.pop()
	assert uuid.UUID(attempt_ref).version == 4
	assert {p["body"]["readAt"] for p in transport.posted} == {_READ_AT}
	return attempt_ref


def _assert_disjoint_and_complete(transport, n):
	names = [e["erpnextItemRef"]["name"] for e in _entries(transport)]
	assert len(names) == len(set(names)) == n
	assert names == [f"ITEM-{i:05d}" for i in range(n)]


def _assert_exact_decimal_entries(transport):
	"""Exact-decimal STRING quantities and no valuation keys on any entry."""
	entries = _entries(transport)
	assert all(isinstance(e["quantity"], str) for e in entries)
	assert all(set(e) == {"erpnextItemRef", "quantity", "stockUom"} for e in entries)


def _assert_window_keys_and_path(transport, attempt_ref):
	keys = [p["key"] for p in transport.posted]
	assert keys == [f"binview-{_REF}-{attempt_ref}-w{seq}" for seq in range(len(keys))]
	assert {p["path"] for p in transport.posted} == {
		f"/api/connector/v1/erpnext/bin-view-requests/{_REF}/snapshot"
	}


def test_1037_rows_report_three_windows_in_order_one_attempt_one_read_at():
	result, transport, reader = _run(_bins(1037), _request())

	assert result.recorded is True
	attempt_ref = _assert_one_attempt_in_order(transport, sizes=[500, 500, 37])
	_assert_disjoint_and_complete(transport, 1037)
	_assert_window_keys_and_path(transport, attempt_ref)
	# ONE paged read attempt, asking for limit + 1 rows in maxItems pages; v1 read not used.
	assert reader.paged_calls == [("ERP-WH-1", 500, 20 * 500 + 1)]
	assert reader.v1_calls == []


def test_1037_rows_quantities_are_exact_decimal_strings():
	_, transport, _ = _run(_bins(1037), _request())

	_assert_exact_decimal_entries(transport)
	entries = _entries(transport)
	assert entries[0]["quantity"] == "0.250000"
	assert Decimal(entries[-1]["quantity"]) == Decimal("1036.25")


def test_each_attempt_gets_a_fresh_attempt_ref():
	_, first, _ = _run(_bins(3), _request())
	_, second, _ = _run(_bins(3), _request())
	assert first.posted[0]["body"]["window"]["attemptRef"] != second.posted[0]["body"]["window"]["attemptRef"]


# --- AC2: ≤ maxItems rows → one final window; empty warehouse → one empty final window ---


@pytest.mark.parametrize("n", [1, 499, 500])
def test_at_most_max_items_rows_is_a_single_final_window(n):
	_, transport, _ = _run(_bins(n), _request())
	assert len(transport.posted) == 1
	body = transport.posted[0]["body"]
	assert body["window"]["windowSeq"] == 0
	assert body["window"]["isFinal"] is True
	assert len(body["entries"]) == n


def test_501_rows_is_two_windows_the_second_non_empty():
	_, transport, _ = _run(_bins(501), _request())
	assert [len(p["body"]["entries"]) for p in transport.posted] == [500, 1]
	assert [p["body"]["window"]["isFinal"] for p in transport.posted] == [False, True]


def test_empty_warehouse_is_one_empty_final_window():
	_, transport, _ = _run([], _request())
	assert len(transport.posted) == 1
	body = transport.posted[0]["body"]
	assert body["entries"] == []
	assert body["window"]["windowSeq"] == 0
	assert body["window"]["isFinal"] is True


def test_exactly_the_limit_is_reported_in_max_windows_windows():
	_, transport, _ = _run(_bins(3 * 4), _request(max_windows=3, max_items=4))
	assert [len(p["body"]["entries"]) for p in transport.posted] == [4, 4, 4]
	assert [p["body"]["window"]["windowSeq"] for p in transport.posted] == [0, 1, 2]


def test_split_windows_never_yields_an_empty_non_final_window():
	for n in range(0, 13):
		chunks = w.split_windows(_bins(n), 4)
		assert all(chunks[:-1]) and (n == 0 or chunks[-1])
		assert sum(len(ch) for ch in chunks) == n
		assert all(len(ch) <= 4 for ch in chunks)


# --- AC3: above maxWindows x maxItems → nothing reported ----------------------------------


def test_above_the_limit_raises_and_reports_nothing():
	transport = _Transport()
	reader = _Reader(_bins(20 * 500 + 1))
	with pytest.raises(w.WindowLimitExceeded) as info:
		w.process_request(
			_request(), client=t.BinViewClient(transport, correlation_id="c"), reader=reader, clock=_Clock()
		)
	assert info.value.limit == 10000
	assert transport.posted == []


def test_small_limit_is_max_windows_times_max_items_not_500():
	transport = _Transport()
	with pytest.raises(w.WindowLimitExceeded):
		w.process_request(
			_request(max_windows=2, max_items=3),
			client=t.BinViewClient(transport, correlation_id="c"),
			reader=_Reader(_bins(7)),
			clock=_Clock(),
		)
	assert transport.posted == []


# --- AC4: no maxWindows (or 1) → v1 exactly -----------------------------------------------


@pytest.mark.parametrize("max_windows", [None, 1])
def test_v1_request_sends_v1_body_and_v1_key(max_windows):
	_, transport, reader = _run(_bins(3), _request(max_windows=max_windows))
	assert len(transport.posted) == 1
	body = transport.posted[0]["body"]
	assert "window" not in body
	assert set(body) == {"entries", "readAt"}
	assert transport.posted[0]["key"] == f"binview-{_REF}"
	assert reader.paged_calls == []
	assert len(reader.v1_calls) == 1


@pytest.mark.parametrize("max_windows", [None, 1])
def test_v1_refusal_above_max_items_is_preserved(max_windows):
	transport = _Transport()
	with pytest.raises(w.WindowOverflowError):
		w.process_request(
			_request(max_windows=max_windows),
			client=t.BinViewClient(transport, correlation_id="c"),
			reader=_Reader(_bins(501)),
			clock=_Clock(),
		)
	assert transport.posted == []


# --- AC5 (worker side): a mid-attempt failure propagates; a stale attempt raises -----------


def test_transport_failure_on_window_1_propagates_after_window_0():
	transport = _Transport(responses=[_resp(), ConnectionError("reset")])
	with pytest.raises(ConnectionError):
		_run(_bins(1037), _request(), transport)
	assert [p["body"]["window"]["windowSeq"] for p in transport.posted] == [0, 1]


def test_window_sequence_conflict_carries_the_refused_window_seq():
	conflict = _resp(409, {"error": {"code": "window_sequence_conflict"}})
	transport = _Transport(responses=[_resp(), conflict])
	with pytest.raises(t.WindowSequenceConflict) as info:
		_run(_bins(1037), _request(), transport)
	assert info.value.window_seq == 1
	assert len(transport.posted) == 2  # nothing sent after the refusal


def test_final_window_acknowledged_as_not_complete_raises_report_incomplete():
	transport = _Transport(responses=[_resp(body={"complete": False})])
	with pytest.raises(w.ReportIncomplete):
		_run(_bins(3), _request(), transport)


def test_final_window_without_complete_field_is_accepted():
	# A Backend-Core that omits `complete` (pre-1.2 runtime) is never read as incomplete.
	result, _, _ = _run(_bins(3), _request(), _Transport(responses=[_resp(body={"windowSeq": 0})]))
	assert result.recorded is True


# --- attempt deadline (Codex P1 on #56, worker.py:278) ---------------------------------------------


class _SlowTransport(_Transport):
	"""Each report call advances the injected monotonic clock by ``seconds``."""

	def __init__(self, clock, seconds):
		super().__init__()
		self._clock, self._seconds = clock, seconds

	def post(self, path, *, json, headers):
		self._clock["t"] += self._seconds
		return super().post(path, json=json, headers=headers)


def _run_with_deadline(n, seconds_per_call, budget_s):
	clock = {"t": 0.0}
	transport = _SlowTransport(clock, seconds_per_call)
	deadline = w.AttemptDeadline(budget_s, monotonic=lambda: clock["t"])
	w.process_request(
		_request(),
		client=t.BinViewClient(transport, correlation_id="c"),
		reader=_Reader(_bins(n)),
		clock=_Clock(),
		deadline=deadline,
	)
	return transport


def test_deadline_stops_before_a_window_that_could_overrun_the_budget():
	with pytest.raises(w.AttemptDeadlineExceeded) as info:
		_run_with_deadline(1037, seconds_per_call=40, budget_s=100)
	# Before w0: 0+30 <= 100; before w1: 40+30 <= 100; before w2: 80+30 > 100 -> stop.
	assert info.value.window_seq == 2


def test_deadline_sends_no_final_window_when_it_stops():
	clock = {"t": 0.0}
	transport = _SlowTransport(clock, 40)
	with pytest.raises(w.AttemptDeadlineExceeded):
		w.process_request(
			_request(),
			client=t.BinViewClient(transport, correlation_id="c"),
			reader=_Reader(_bins(1037)),
			clock=_Clock(),
			deadline=w.AttemptDeadline(100, monotonic=lambda: clock["t"]),
		)
	assert [p["body"]["window"]["windowSeq"] for p in transport.posted] == [0, 1]
	assert not any(p["body"]["window"]["isFinal"] for p in transport.posted)


def test_fast_attempt_under_a_deadline_is_unchanged():
	transport = _run_with_deadline(1037, seconds_per_call=1, budget_s=w.attempt_budget_s(20))
	assert [p["body"]["window"]["windowSeq"] for p in transport.posted] == [0, 1, 2]
	assert [p["body"]["window"]["isFinal"] for p in transport.posted] == [False, False, True]


def test_attempt_budget_covers_every_window_at_the_transport_timeout_and_is_capped():
	assert w.attempt_budget_s(1) == 1 * 30 + 60
	assert w.attempt_budget_s(20) == 20 * 30 + 60
	assert w.attempt_budget_s(100) == w.MAX_ATTEMPT_BUDGET_S == 1200
	# Every window at the full 30 s transport timeout still fits the (uncapped) budget.
	transport = _run_with_deadline(20 * 500, seconds_per_call=30, budget_s=w.attempt_budget_s(20))
	assert len(transport.posted) == 20


def test_v1_request_ignores_the_deadline():
	clock = {"t": 0.0}
	transport = _SlowTransport(clock, 10_000)
	w.process_request(
		_request(max_windows=None),
		client=t.BinViewClient(transport, correlation_id="c"),
		reader=_Reader(_bins(3)),
		clock=_Clock(),
		deadline=w.AttemptDeadline(1, monotonic=lambda: clock["t"]),
	)
	assert len(transport.posted) == 1
