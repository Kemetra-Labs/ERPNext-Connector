# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-176 — the bounded bin-view retry set (pure; no frappe)."""

from retail_tower_erpnext_connector.connector.bin_view import contracts as c
from retail_tower_erpnext_connector.connector.bin_view import retry as r


def _request(n=1, max_windows=20):
	ref = f"{n:08d}-1111-4111-8111-111111111111"
	return c.BinViewRequest(
		request_ref=ref,
		store_id="33333333-3333-4333-8333-333333333333",
		erpnext_warehouse_ref="ERP-WH-1",
		run_ref="44444444-4444-4444-8444-444444444444",
		item_window=c.BinViewItemWindow(
			window_seq=0, max_items=500, from_item_ref=None, to_item_ref=None, max_windows=max_windows
		),
		item_cursor=ref,
	)


def test_first_failure_queues_the_request():
	rs = r.RetrySet()
	outcome = rs.record_failure(_request())
	assert outcome == r.FailureOutcome(status=r.QUEUED, failures=1)
	assert _request().request_ref in rs
	assert rs.pending() == [_request()]


def test_abandoned_after_max_retry_ticks_failed_retries():
	rs = r.RetrySet()
	req = _request()
	outcomes = [rs.record_failure(req) for _ in range(r.MAX_RETRY_TICKS + 1)]
	# The original failure + MAX_RETRY_TICKS failed retries are queued; the next one abandons.
	assert [o.status for o in outcomes[:-1]] == [r.QUEUED] * r.MAX_RETRY_TICKS
	assert outcomes[-1].status == r.ABANDONED
	assert req.request_ref not in rs


def test_resolve_forgets_the_request():
	rs = r.RetrySet()
	rs.record_failure(_request())
	rs.resolve(_request().request_ref)
	assert len(rs) == 0
	rs.resolve("unknown")  # no-op


def test_bounded_size_abandons_new_failures_but_keeps_counting_held_ones():
	rs = r.RetrySet()
	for i in range(r.MAX_PENDING):
		rs.record_failure(_request(i))
	assert rs.record_failure(_request(r.MAX_PENDING)).status == r.ABANDONED
	assert len(rs) == r.MAX_PENDING
	assert rs.record_failure(_request(0)).status == r.QUEUED  # already held


def test_state_round_trips_and_tolerates_garbage():
	rs = r.RetrySet()
	rs.record_failure(_request(1))
	rs.record_failure(_request(2, max_windows=None))
	rs.record_failure(_request(2, max_windows=None))
	state = rs.to_state()
	again = r.RetrySet(state)
	assert again.pending() == rs.pending()
	assert again.failures(_request(2).request_ref) == 2

	state["broken"] = {"request": {"nope": 1}, "failures": 1}
	state["mismatch"] = {"request": _request(3).to_wire(), "failures": 1}
	state[_request(4).request_ref] = {"request": _request(4).to_wire(), "failures": 0}
	assert len(r.RetrySet(state)) == 2
	assert len(r.RetrySet(None)) == 0
	assert len(r.RetrySet("not a mapping")) == 0


# --- per-tick bound, pre-charge, FIFO rotation (Codex P1 on #56) ------------------------------


def _held(n):
	rs = r.RetrySet()
	for i in range(n):
		rs.record_failure(_request(i))
	return rs


def test_charge_counts_up_front_and_rotates_to_the_back():
	rs = _held(3)
	outcome = rs.charge_retry(_request(0))
	assert outcome == r.FailureOutcome(status=r.QUEUED, failures=2)
	assert [q.request_ref for q in rs.pending()] == [_request(i).request_ref for i in (1, 2, 0)]


def test_charged_retries_abandon_at_the_same_bound_as_before():
	rs = _held(1)
	req = _request(0)
	for retry in range(1, r.MAX_RETRY_TICKS + 1):
		assert rs.charge_retry(req).status == r.QUEUED
		outcome = rs.settle_failed_retry(req)
		if retry < r.MAX_RETRY_TICKS:
			assert outcome == r.FailureOutcome(status=r.QUEUED, failures=retry + 1)
	# The MAX_RETRY_TICKS-th failed retry abandons — same as record_failure counting.
	assert outcome == r.FailureOutcome(status=r.ABANDONED, failures=r.MAX_RETRY_TICKS + 1)
	assert req.request_ref not in rs


def test_an_interrupted_last_retry_is_abandoned_on_the_next_charge():
	rs = _held(1)
	req = _request(0)
	for _ in range(r.MAX_RETRY_TICKS):
		rs.charge_retry(req)  # charged, never settled (worker killed mid-attempt)
	assert rs.failures(req.request_ref) == r.MAX_RETRY_TICKS + 1
	assert rs.charge_retry(req) == r.FailureOutcome(status=r.ABANDONED, failures=r.MAX_RETRY_TICKS + 2)
	assert req.request_ref not in rs
