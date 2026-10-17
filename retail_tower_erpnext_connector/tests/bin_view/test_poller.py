# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-176 — the bin-view poller tick and the paged frappe Bin read, against a frappe stand-in.

``poller`` and ``frappe_glue`` import frappe, so they are loaded against a minimal stand-in (the
RT-39 / RT-71 pattern); the real bench run stays a deferred validation step (standing-rules §6).

Covered here: a mid-attempt transport failure is retried on a LATER tick as a fresh attempt
(new ``attemptRef`` from window 0) even though the cursor has moved on (the stranded-run fix);
a 409 ``window_sequence_conflict`` on a stale attempt is handled without crashing; the bounded
give-up (``bin_view.report.incomplete_abandoned``); the paged limit refusal
(``bin_view.window.limit_exceeded``); paged attempts handed off to a deduplicated long-queue job
with an explicit timeout and an in-job deadline; and the keyset page read.

Paged requests run in ``run_paged_report_job`` (long queue). ``_tick`` simulates the long-queue
worker by running the jobs the tick enqueued right after it; their outcomes are folded into the
retry set by the NEXT tick.
"""

import importlib
import pickle
import sys
import types

import pytest

from retail_tower_erpnext_connector.connector.bin_view import retry as r

_ROOT = "retail_tower_erpnext_connector"
_POLLER = f"{_ROOT}.connector.bin_view.poller"
_GLUE = f"{_ROOT}.connector.bin_view.frappe_glue"
_MISSING = object()
_REF = "11111111-1111-4111-8111-111111111111"
_REF_2 = "22222222-2222-4222-8222-222222222222"


class _Logger:
    def __init__(self):
        self.records = []

    def _log(self, level):
        return lambda record: self.records.append((level, record))

    def __getattr__(self, level):
        return self._log(level)


class _Pipe:
    """MULTI/EXEC stand-in: queued commands run together on ``execute``."""

    def __init__(self, redis):
        self._redis, self._ops = redis, []

    def get(self, key):
        self._ops.append(lambda: self._redis.get(key))

    def delete(self, key):
        self._ops.append(lambda: int(self._redis.pop(key, None) is not None))

    def execute(self):
        return [op() for op in self._ops]


class _Cache:
    """Mirrors frappe v15 ``RedisWrapper``: pickled values under ``make_key`` + a pipeline."""

    def __init__(self):
        self.values = {}
        self.redis = {}  # the raw store: make_key(key) -> pickled value
        self.writes = []  # (key, retry state as stored at the moment of this write)

    def make_key(self, key):
        return f"site_db|{key}".encode()

    def get_value(self, key):
        return self.values.get(key)

    def set_value(self, key, value, **_kwargs):
        self.values[key] = value
        self.redis[self.make_key(key)] = pickle.dumps(value)
        self.writes.append((key, self.values.get("rt_bin_view_retry")))

    def delete_value(self, key):
        self.values.pop(key, None)
        self.redis.pop(self.make_key(key), None)

    def pipeline(self, transaction=True):
        assert transaction is True
        return _Pipe(self.redis)


@pytest.fixture
def frappe_stub():
    before = set(sys.modules)
    saved = sys.modules.get("frappe", _MISSING)
    fake = types.ModuleType("frappe")
    fake.exceptions = types.SimpleNamespace()
    fake.log = _Logger()
    fake.logger = lambda *_a, **_k: fake.log
    fake.cache_obj = _Cache()
    fake.cache = lambda: fake.cache_obj
    fake.jobs = []  # enqueued, not yet run (i.e. "in flight")

    def _enqueue(method, **kwargs):
        fake.jobs.append({"method": method, **kwargs})

    fake.enqueue = _enqueue
    sys.modules["frappe"] = fake
    yield fake
    for name in set(sys.modules) - before:
        if name.startswith(_ROOT):
            sys.modules.pop(name, None)
            parent, _, child = name.rpartition(".")
            if parent in sys.modules:
                vars(sys.modules[parent]).pop(child, None)
    if saved is _MISSING:
        sys.modules.pop("frappe", None)
    else:
        sys.modules["frappe"] = saved


def _events(fake, name=None):
    events = [(level, rec) for level, rec in fake.log.records if isinstance(rec, dict)]
    return [(level, rec) for level, rec in events if name is None or rec.get("event") == name]


# --- scripted DP2 + fakes -------------------------------------------------------------------------


class _Resp:
    def __init__(self, status=201, body=None):
        self.status = status
        self.headers = {}
        self.body = body or {}


def _wire(ref=_REF, max_windows=20, max_items=500):
    window = {"windowSeq": 0, "maxItems": max_items, "fromItemRef": None, "toItemRef": None}
    if max_windows is not None:
        window["maxWindows"] = max_windows
    return {
        "requestRef": ref,
        "storeId": "33333333-3333-4333-8333-333333333333",
        "erpnextWarehouseRef": "ERP-WH-1",
        "runRef": "44444444-4444-4444-8444-444444444444",
        "itemWindow": window,
        "itemCursor": ref,
    }


class _Dp2:
    """Serves one feed page per tick (``pages``) and scripted POST outcomes (default 201)."""

    def __init__(self, pages, post_outcomes=None):
        self.pages = list(pages)
        self.post_outcomes = list(post_outcomes or [])
        self.gets, self.posts = [], []

    def get(self, path, *, params, headers):
        self.gets.append(params.get("since"))
        if self.pages:
            return self.pages.pop(0)
        return {"items": [], "cursor": params.get("since") or "c-0", "next_page_token": None}

    def post(self, path, *, json, headers):
        self.posts.append({"path": path, "body": json, "key": headers["Idempotency-Key"]})
        if self.post_outcomes:
            outcome = self.post_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return _Resp(201)


class _Reader:
    def __init__(self, n):
        from retail_tower_erpnext_connector.connector.bin_view.worker import RawBin

        self.bins = [RawBin(item_code=f"ITEM-{i:05d}", actual_qty=1.5, stock_uom="Nos") for i in range(n)]

    def read_bins(self, *, erpnext_warehouse_ref, item_window):
        return self.bins[: item_window.max_items + 1]

    def read_bins_paged(self, *, erpnext_warehouse_ref, page_size, max_rows):
        return self.bins[:max_rows]


class _Clock:
    def now_iso(self):
        return "2026-10-04T08:00:00.000Z"


def _poller(fake, dp2, reader):
    poller = importlib.import_module(_POLLER)
    from retail_tower_erpnext_connector.connector.bin_view.transport import BinViewClient

    poller._build_bin_view_path = lambda _correlation_id: (BinViewClient(dp2, correlation_id="corr"), reader, _Clock())
    poller._job_in_flight = lambda job_id: any(job["job_id"] == job_id for job in fake.jobs)
    return poller


def _run_jobs(fake, poller):
    """Simulate the long-queue worker: run every enqueued paged-report job, in order."""
    while fake.jobs:
        job = fake.jobs.pop(0)
        assert job["method"] == "retail_tower_erpnext_connector.connector.bin_view.poller.run_paged_report_job"
        poller.run_paged_report_job(request_wire=job["request_wire"], retried=job["retried"])


def _tick(fake, dp2, reader, *, run_jobs=True):
    poller = _poller(fake, dp2, reader)
    before = len(dp2.posts)
    poller.run_bin_view_poll()
    if run_jobs:
        _run_jobs(fake, poller)
    return dp2.posts[before:]


def _page(*items, cursor="c-1"):
    return {"items": list(items), "cursor": cursor, "next_page_token": None}


def _retry_state(fake):
    return r.RetrySet(fake.cache_obj.values.get("rt_bin_view_retry"))


def _seqs(posts):
    return [p["body"]["window"]["windowSeq"] for p in posts]


def _attempts(posts):
    return {p["body"]["window"]["attemptRef"] for p in posts}


# --- AC5: transport failure on window 1 → next tick, fresh attempt from window 0 ------------------


def test_transport_failure_on_window_1_is_retried_next_tick_as_a_fresh_attempt(frappe_stub):
    dp2 = _Dp2([_page(_wire())], post_outcomes=[_Resp(201), ConnectionError("connection reset")])
    reader = _Reader(1037)

    first = _tick(frappe_stub, dp2, reader)
    assert _seqs(first) == [0, 1]
    # The cursor moved past the page — but the request is durable in the retry set.
    assert frappe_stub.cache_obj.values["rt_bin_view_cursor"] == "c-1"
    assert _REF in _retry_state(frappe_stub)

    second = _tick(frappe_stub, dp2, reader)
    # The next tick folds the job's failure in, then hands off a fresh attempt.
    (level, rec), = _events(frappe_stub, "bin_view.report.retry_scheduled")
    assert level == "warning" and rec["failures"] == 1 and rec["request_ref"] == _REF
    assert rec["reason"] == "ConnectionError"
    assert _seqs(second) == [0, 1, 2]
    assert [p["body"]["window"]["isFinal"] for p in second] == [False, False, True]
    assert len(_attempts(second)) == 1
    assert _attempts(second).isdisjoint(_attempts(first))  # a NEW attemptRef (supersede)
    assert second[0]["key"] != first[0]["key"]

    assert _tick(frappe_stub, dp2, reader) == []  # the success is folded in; nothing retried
    assert len(_retry_state(frappe_stub)) == 0
    assert dp2.gets == [None, "c-1", "c-1"]  # the feed cursor is not rewound


def test_retry_state_is_saved_before_the_cursor_moves(frappe_stub):
    # The stranded-run fix: when the cursor moves past the page, the failed request is ALREADY
    # durable in the retry set (a crash between the two writes cannot lose it).
    dp2 = _Dp2([_page(_wire())], post_outcomes=[ConnectionError("down")])
    _tick(frappe_stub, dp2, _Reader(3))
    cursor_writes = [state for key, state in frappe_stub.cache_obj.writes if key == "rt_bin_view_cursor"]
    assert len(cursor_writes) == 2  # past the request (its itemCursor), then past the page
    assert all(_REF in r.RetrySet(state) for state in cursor_writes)


def test_stale_attempt_409_mid_sequence_is_handled_and_retried(frappe_stub):
    conflict = _Resp(409, {"error": {"code": "window_sequence_conflict", "message": "m"}})
    dp2 = _Dp2([_page(_wire())], post_outcomes=[_Resp(201), conflict])
    reader = _Reader(1037)

    first = _tick(frappe_stub, dp2, reader)  # must not raise
    assert _seqs(first) == [0, 1]

    second = _tick(frappe_stub, dp2, reader)
    (_, rec), = _events(frappe_stub, "bin_view.report.retry_scheduled")
    assert rec["reason"] == "window_sequence_conflict"
    assert _seqs(second) == [0, 1, 2]
    _tick(frappe_stub, dp2, reader)
    assert len(_retry_state(frappe_stub)) == 0


def test_409_on_window_0_of_a_new_attempt_means_already_complete(frappe_stub):
    conflict = _Resp(409, {"error": {"code": "window_sequence_conflict"}})
    dp2 = _Dp2([_page(_wire())], post_outcomes=[conflict])
    posts = _tick(frappe_stub, dp2, _Reader(1037))
    assert _seqs(posts) == [0]
    assert len(_events(frappe_stub, "bin_view.report.already_complete")) == 1
    assert _tick(frappe_stub, dp2, _Reader(1037)) == []  # resolved on fold; nothing retried
    assert len(_retry_state(frappe_stub)) == 0


def test_gives_up_after_max_retry_ticks(frappe_stub):
    dp2 = _Dp2([_page(_wire())], post_outcomes=[ConnectionError("down")] * 50)
    reader = _Reader(3)
    # The feed attempt + MAX_RETRY_TICKS retries each run in a job; the next tick folds the last
    # failure in and abandons it.
    for _ in range(2 + r.MAX_RETRY_TICKS):
        _tick(frappe_stub, dp2, reader)
    assert len(dp2.posts) == 1 + r.MAX_RETRY_TICKS
    abandoned = _events(frappe_stub, "bin_view.report.incomplete_abandoned")
    assert len(abandoned) == 1
    level, rec = abandoned[0]
    assert level == "error" and rec["request_ref"] == _REF and rec["failures"] == r.MAX_RETRY_TICKS + 1
    assert len(_retry_state(frappe_stub)) == 0
    assert _tick(frappe_stub, dp2, reader) == []  # bounded: no further attempts


def test_report_incomplete_is_retried(frappe_stub):
    dp2 = _Dp2([_page(_wire())], post_outcomes=[_Resp(201, {"complete": False})])
    _tick(frappe_stub, dp2, _Reader(3))
    _tick(frappe_stub, dp2, _Reader(3), run_jobs=False)
    (_, rec), = _events(frappe_stub, "bin_view.report.retry_scheduled")
    assert rec["reason"] == "report_incomplete"
    assert _REF in _retry_state(frappe_stub)


def test_one_failing_request_does_not_block_the_rest_of_the_page(frappe_stub):
    dp2 = _Dp2(
        [_page(_wire(_REF, max_windows=None), _wire(_REF_2, max_windows=None))],
        post_outcomes=[ConnectionError("down")],
    )
    posts = _tick(frappe_stub, dp2, _Reader(3))
    assert [p["path"].split("/")[-2] for p in posts] == [_REF, _REF_2]
    state = _retry_state(frappe_stub)
    assert _REF in state and _REF_2 not in state


def test_a_retried_request_re_offered_by_the_feed_is_attempted_once_per_tick(frappe_stub):
    dp2 = _Dp2([_page(_wire()), _page(_wire(), cursor="c-0")], post_outcomes=[ConnectionError("down")])
    reader = _Reader(3)
    _tick(frappe_stub, dp2, reader)
    second = _tick(frappe_stub, dp2, reader)  # retry + a re-baselined feed offering the same ref
    assert len(second) == 1


# --- AC3: above maxWindows x maxItems → nothing reported, limit_exceeded logged -----------------


def test_limit_exceeded_reports_nothing_and_logs(frappe_stub):
    dp2 = _Dp2([_page(_wire(max_windows=2, max_items=500))])
    posts = _tick(frappe_stub, dp2, _Reader(1001))
    assert posts == []
    (level, rec), = _events(frappe_stub, "bin_view.window.limit_exceeded")
    assert level == "error"
    assert rec["request_ref"] == _REF and rec["limit"] == 1000 and rec["warehouse"] == "ERP-WH-1"
    # Deterministic refusal — logged loudly, resolved on the next fold, not retried.
    assert _tick(frappe_stub, dp2, _Reader(1001)) == []
    assert len(_retry_state(frappe_stub)) == 0
    assert len(_events(frappe_stub, "bin_view.window.limit_exceeded")) == 1


# --- AC4 (tick level): v1 unchanged ---------------------------------------------------------------


def test_v1_request_tick_uses_v1_body_and_key(frappe_stub):
    dp2 = _Dp2([_page(_wire(max_windows=None))])
    (post,) = _tick(frappe_stub, dp2, _Reader(3))
    assert "window" not in post["body"]
    assert post["key"] == f"binview-{_REF}"


def test_v1_overflow_still_refuses_and_logs_overflow(frappe_stub):
    dp2 = _Dp2([_page(_wire(max_windows=None))])
    assert _tick(frappe_stub, dp2, _Reader(501)) == []
    assert len(_events(frappe_stub, "bin_view.window.overflow")) == 1
    assert len(_retry_state(frappe_stub)) == 0


def test_v1_transport_failure_is_retried_with_the_same_v1_key(frappe_stub):
    dp2 = _Dp2([_page(_wire(max_windows=None))], post_outcomes=[ConnectionError("down")])
    reader = _Reader(3)
    first = _tick(frappe_stub, dp2, reader)
    second = _tick(frappe_stub, dp2, reader)
    assert [p["key"] for p in first + second] == [f"binview-{_REF}"] * 2
    assert len(_retry_state(frappe_stub)) == 0


# --- frappe_glue: keyset page read --------------------------------------------------------------


class _BinTable:
    def __init__(self, item_codes):
        self.rows = [
            {"item_code": code, "warehouse": "ERP-WH-1", "actual_qty": 2.0, "stock_uom": "Nos"} for code in item_codes
        ] + [{"item_code": "OTHER", "warehouse": "ERP-WH-2", "actual_qty": 1.0, "stock_uom": "Nos"}]
        self.calls = []

    def get_all(self, doctype, **query):
        """``frappe.get_all("Bin", filters=..., fields=..., order_by=..., limit=...)`` stand-in."""
        assert doctype == "Bin" and query["order_by"] == "item_code asc"
        fields = query["fields"]
        assert "valuation_rate" not in fields and "stock_value" not in fields
        self.calls.append({"filters": dict(query["filters"]), "limit": query["limit"]})
        rows = sorted(self._matching(query["filters"]), key=lambda row: row["item_code"])
        return [{f: row[f] for f in fields} for row in rows[: query["limit"]]]

    def _matching(self, filters):
        rows = [row for row in self.rows if row["warehouse"] == filters["warehouse"]]
        if "item_code" not in filters:
            return rows
        op, bound = filters["item_code"]
        assert op == ">"
        return [row for row in rows if row["item_code"] > bound]


def _glue(frappe_stub, table):
    frappe_stub.get_all = table.get_all
    return importlib.import_module(_GLUE)


def test_paged_read_uses_keyset_pages_and_stops_at_a_short_page(frappe_stub):
    table = _BinTable([f"I-{i:04d}" for i in range(1037)])
    glue = _glue(frappe_stub, table)
    rows = glue.FrappeBinReader().read_bins_paged(erpnext_warehouse_ref="ERP-WH-1", page_size=500, max_rows=10001)
    assert [row.item_code for row in rows] == [f"I-{i:04d}" for i in range(1037)]
    assert [c["limit"] for c in table.calls] == [500, 500, 500]
    assert "item_code" not in table.calls[0]["filters"]
    assert table.calls[1]["filters"]["item_code"] == [">", "I-0499"]
    assert table.calls[2]["filters"]["item_code"] == [">", "I-0999"]
    assert all(c["filters"]["warehouse"] == "ERP-WH-1" for c in table.calls)


def test_paged_read_stops_at_max_rows(frappe_stub):
    table = _BinTable([f"I-{i:04d}" for i in range(20)])
    glue = _glue(frappe_stub, table)
    rows = glue.FrappeBinReader().read_bins_paged(erpnext_warehouse_ref="ERP-WH-1", page_size=4, max_rows=9)
    assert len(rows) == 9
    assert [c["limit"] for c in table.calls] == [4, 4, 1]


def test_paged_read_of_an_exact_multiple_ends_with_an_empty_page(frappe_stub):
    table = _BinTable([f"I-{i:04d}" for i in range(8)])
    glue = _glue(frappe_stub, table)
    rows = glue.FrappeBinReader().read_bins_paged(erpnext_warehouse_ref="ERP-WH-1", page_size=4, max_rows=100)
    assert len(rows) == 8
    assert [c["limit"] for c in table.calls] == [4, 4, 4]


def test_paged_read_of_an_empty_warehouse(frappe_stub):
    table = _BinTable([])
    glue = _glue(frappe_stub, table)
    assert glue.FrappeBinReader().read_bins_paged(erpnext_warehouse_ref="ERP-WH-1", page_size=500, max_rows=10001) == []
    assert len(table.calls) == 1


def test_v1_read_is_unchanged_single_query(frappe_stub):
    table = _BinTable([f"I-{i:04d}" for i in range(600)])
    glue = _glue(frappe_stub, table)
    window = types.SimpleNamespace(max_items=500)
    rows = glue.FrappeBinReader().read_bins(erpnext_warehouse_ref="ERP-WH-1", item_window=window)
    assert len(rows) == 501
    assert table.calls == [{"filters": {"warehouse": "ERP-WH-1"}, "limit": 501}]


# --- Codex P1 on #56: bounded, checkpointed, non-starving retry drain ----------------------------


class _WorkerKilled(BaseException):
    """Stands in for the scheduler job being killed mid-attempt (not an ``Exception``)."""


def _ref(i):
    return f"{i:08d}-1111-4111-8111-111111111111"


def _seed_retry_set(fake, n):
    rs = r.RetrySet()
    for i in range(n):
        rs.record_failure(importlib.import_module(f"{_ROOT}.connector.bin_view.contracts").BinViewRequest.from_wire(
            _wire(_ref(i), max_windows=None)
        ))
    fake.cache_obj.values["rt_bin_view_retry"] = rs.to_state()


def _refs(posts):
    return [p["path"].split("/")[-2] for p in posts]


def test_retry_drain_is_bounded_per_tick(frappe_stub):
    _seed_retry_set(frappe_stub, 12)
    dp2 = _Dp2([], post_outcomes=[ConnectionError("down")] * 100)
    posts = _tick(frappe_stub, dp2, _Reader(3))
    assert _refs(posts) == [_ref(i) for i in range(5)]  # poller.MAX_INLINE_ATTEMPTS_PER_TICK
    assert len(dp2.gets) == 1  # the feed is still pulled after the bounded drain
    state = _retry_state(frappe_stub)
    assert len(state) == 12  # nothing lost; the rest wait for later ticks
    assert [state.failures(_ref(i)) for i in range(12)] == [2] * 5 + [1] * 7


def test_counters_persist_when_the_worker_dies_mid_drain(frappe_stub):
    _seed_retry_set(frappe_stub, 8)
    dp2 = _Dp2([], post_outcomes=[ConnectionError("slow"), ConnectionError("slow"), _WorkerKilled()])
    with pytest.raises(_WorkerKilled):
        _tick(frappe_stub, dp2, _Reader(3))
    assert _refs(dp2.posts) == [_ref(0), _ref(1), _ref(2)]
    state = _retry_state(frappe_stub)
    # The two finished attempts AND the interrupted one are counted (charged before running)...
    assert [state.failures(_ref(i)) for i in range(8)] == [2, 2, 2, 1, 1, 1, 1, 1]
    # ...and rotated to the back, so the next tick starts with the requests not yet reached.
    assert [q.request_ref for q in state.pending()] == [_ref(i) for i in (3, 4, 5, 6, 7, 0, 1, 2)]
    nxt = _tick(frappe_stub, _Dp2([], post_outcomes=[ConnectionError("down")] * 10), _Reader(3))
    assert _refs(nxt) == [_ref(i) for i in (3, 4, 5, 6, 7)]


def test_no_retry_is_starved_across_ticks(frappe_stub):
    _seed_retry_set(frappe_stub, 12)
    dp2 = _Dp2([], post_outcomes=[ConnectionError("down")] * 100)
    reader = _Reader(3)
    attempted = []
    for _ in range(3):
        attempted += _refs(_tick(frappe_stub, dp2, reader))
    # Every request is retried once before any is retried twice (FIFO rotation).
    assert attempted[:12] == [_ref(i) for i in range(12)]
    assert attempted[12:] == [_ref(i) for i in range(3)]


def test_an_interrupted_final_retry_is_abandoned_without_another_attempt(frappe_stub):
    rs = r.RetrySet()
    contracts = importlib.import_module(f"{_ROOT}.connector.bin_view.contracts")
    req = contracts.BinViewRequest.from_wire(_wire(_ref(0), max_windows=None))
    rs.record_failure(req)
    for _ in range(r.MAX_RETRY_TICKS):
        rs.charge_retry(req)  # the last allowed retry was charged but the worker died
    frappe_stub.cache_obj.values["rt_bin_view_retry"] = rs.to_state()
    assert _tick(frappe_stub, _Dp2([]), _Reader(3)) == []
    (level, rec), = _events(frappe_stub, "bin_view.report.incomplete_abandoned")
    assert level == "error" and rec["reason"] == "retry_budget_spent" and rec["request_ref"] == _ref(0)
    assert len(_retry_state(frappe_stub)) == 0


# --- Codex P1 on #56 (worker.py:278): paged attempts on the long queue, under a deadline ---------


def test_paged_request_is_enqueued_on_the_long_queue_with_dedup_and_explicit_timeout(frappe_stub):
    dp2 = _Dp2([_page(_wire(max_windows=20))])
    assert _tick(frappe_stub, dp2, _Reader(3), run_jobs=False) == []  # nothing posted inline
    (job,) = frappe_stub.jobs
    assert job["method"] == "retail_tower_erpnext_connector.connector.bin_view.poller.run_paged_report_job"
    assert job["queue"] == "long"
    assert job["job_id"] == f"rt_bin_view_paged::{_REF}"
    assert job["deduplicate"] is True
    # maxWindows 20 x 30 s + 60 s read allowance = 660 s attempt budget, + 120 s job margin.
    assert job["timeout"] == 780 < 1500
    assert job["request_wire"] == _wire(max_windows=20)
    # Charged and checkpointed BEFORE the hand-off.
    assert _retry_state(frappe_stub).failures(_REF) == 1


def test_job_timeout_is_capped_under_the_long_queue_timeout(frappe_stub):
    dp2 = _Dp2([_page(_wire(max_windows=100))])
    _tick(frappe_stub, dp2, _Reader(3), run_jobs=False)
    (job,) = frappe_stub.jobs
    assert job["timeout"] == 1200 + 120 < 1500


def test_an_in_flight_job_is_neither_re_enqueued_nor_charged(frappe_stub):
    dp2 = _Dp2([_page(_wire())])
    _tick(frappe_stub, dp2, _Reader(3), run_jobs=False)
    for _ in range(3):  # the job is still queued/running across these ticks
        _tick(frappe_stub, dp2, _Reader(3), run_jobs=False)
    assert len(frappe_stub.jobs) == 1
    assert _retry_state(frappe_stub).failures(_REF) == 1


def test_a_job_that_dies_without_an_outcome_counts_as_a_failed_attempt(frappe_stub):
    dp2 = _Dp2([_page(_wire())])
    _tick(frappe_stub, dp2, _Reader(3), run_jobs=False)
    frappe_stub.jobs.clear()  # the long-queue worker was killed: no outcome was written
    posts = _tick(frappe_stub, dp2, _Reader(3))
    assert _retry_state(frappe_stub).failures(_REF) == 2  # charged again for the new attempt
    assert len(posts) == 1


class _SlowDp2(_Dp2):
    """Every report call takes ``seconds`` on the injected monotonic clock."""

    def __init__(self, pages, clock, seconds):
        super().__init__(pages)
        self._clock, self._seconds = clock, seconds

    def post(self, path, *, json, headers):
        self._clock["t"] += self._seconds
        return super().post(path, json=json, headers=headers)


def test_slow_windows_stop_at_the_deadline_without_is_final_and_counters_persist(frappe_stub):
    clock = {"t": 0.0}
    dp2 = _SlowDp2([_page(_wire(max_windows=20))], clock, seconds=320)
    reader = _Reader(1037)
    poller = _poller(frappe_stub, dp2, reader)
    poller._monotonic = lambda: clock["t"]

    poller.run_bin_view_poll()
    _run_jobs(frappe_stub, poller)
    # Budget 660 s: w0 (0+30), w1 (320+30) pass; w2 (640+30 > 660) is NOT sent.
    assert _seqs(dp2.posts) == [0, 1]
    assert not any(p["body"]["window"]["isFinal"] for p in dp2.posts)
    (level, rec), = _events(frappe_stub, "bin_view.report.deadline_reached")
    assert level == "warning" and rec["window_seq"] == 2

    poller.run_bin_view_poll()  # next tick folds the outcome in; the counter persists
    (_, sched), = _events(frappe_stub, "bin_view.report.retry_scheduled")
    assert sched["reason"] == "attempt_deadline" and sched["failures"] == 1
    assert _retry_state(frappe_stub).failures(_REF) == 2  # + the fresh attempt it just handed off


def test_a_fast_paged_attempt_is_unchanged_under_the_deadline(frappe_stub):
    clock = {"t": 0.0}
    dp2 = _SlowDp2([_page(_wire(max_windows=20))], clock, seconds=1)
    poller = _poller(frappe_stub, dp2, _Reader(1037))
    poller._monotonic = lambda: clock["t"]
    poller.run_bin_view_poll()
    _run_jobs(frappe_stub, poller)
    assert _seqs(dp2.posts) == [0, 1, 2]
    assert [p["body"]["window"]["isFinal"] for p in dp2.posts] == [False, False, True]
    assert _events(frappe_stub, "bin_view.report.deadline_reached") == []


# --- Codex P1 on #56 (poller.py:123): new v1 feed attempts share the bounded, checkpointed budget --


class _FeedDp2(_Dp2):
    """A keyset feed over ``wires`` (ordered by itemCursor) that honors ``since`` like Backend-Core."""

    def __init__(self, wires, post_outcomes=None, clock=None, seconds_per_call=0):
        super().__init__([], post_outcomes)
        self.wires = sorted(wires, key=lambda w: w["itemCursor"])
        self._clock, self._seconds = clock, seconds_per_call
        self.call_starts = []

    def _spend(self):
        if self._clock is not None:
            self.call_starts.append(self._clock["t"])
            self._clock["t"] += self._seconds

    def get(self, path, *, params, headers):
        self._spend()
        since = params.get("since")
        self.gets.append(since)
        items = [w for w in self.wires if since is None or w["itemCursor"] > since][: params["limit"]]
        cursor = items[-1]["itemCursor"] if items else (since or "c-0")
        token = cursor if len(items) == params["limit"] else None
        return {"items": items, "cursor": cursor, "next_page_token": token}

    def post(self, path, *, json, headers):
        self._spend()
        return super().post(path, json=json, headers=headers)


def _v1_wires(n):
    return [_wire(_ref(i), max_windows=None) for i in range(n)]


def _post_counts(dp2):
    counts = {}
    for ref in _refs(dp2.posts):
        counts[ref] = counts.get(ref, 0) + 1
    return counts


def test_a_page_of_more_v1_requests_than_the_budget_is_processed_across_ticks(frappe_stub):
    dp2 = _FeedDp2(_v1_wires(12))
    reader = _Reader(3)
    per_tick = [_refs(_tick(frappe_stub, dp2, reader)) for _ in range(3)]
    assert per_tick == [[_ref(i) for i in range(0, 5)], [_ref(i) for i in range(5, 10)], [_ref(10), _ref(11)]]
    # The cursor stopped right after the last processed request each time.
    assert dp2.gets == [None, _ref(4), _ref(9)]
    assert frappe_stub.cache_obj.values["rt_bin_view_cursor"] == _ref(11)
    assert _post_counts(dp2) == {_ref(i): 1 for i in range(12)}  # none lost, none twice
    assert len(_retry_state(frappe_stub)) == 0


def test_inline_budget_is_shared_between_retries_and_new_feed_requests(frappe_stub):
    _seed_retry_set(frappe_stub, 3)  # refs 0..2 held for retry
    dp2 = _FeedDp2([_wire(_ref(i), max_windows=None) for i in range(10, 16)])
    posts = _tick(frappe_stub, dp2, _Reader(3))
    # 3 retries + 2 new feed requests = the 5 inline attempts of one tick.
    assert _refs(posts) == [_ref(0), _ref(1), _ref(2), _ref(10), _ref(11)]
    assert frappe_stub.cache_obj.values["rt_bin_view_cursor"] == _ref(11)


def test_tick_time_budget_stops_slow_calls_before_the_cron_timeout(frappe_stub):
    clock = {"t": 0.0}
    dp2 = _FeedDp2(_v1_wires(12), clock=clock, seconds_per_call=50)
    poller = _poller(frappe_stub, dp2, _Reader(3))
    poller._monotonic = lambda: clock["t"]
    poller.run_bin_view_poll()
    # GET at 0, POSTs at 50/100/150/200; the next would start at 250 (250 + 30 > 240): not started,
    # although the attempt count (5) is not yet spent.
    assert dp2.call_starts == [0, 50, 100, 150, 200]
    assert all(start + 30 <= 240 for start in dp2.call_starts)
    assert _refs(dp2.posts) == [_ref(i) for i in range(4)]
    assert frappe_stub.cache_obj.values["rt_bin_view_cursor"] == _ref(3)
    assert len(_events(frappe_stub, "bin_view.poll.budget_exhausted")) == 1


def test_a_crash_mid_page_keeps_completed_outcomes_and_never_double_counts(frappe_stub):
    dp2 = _FeedDp2(_v1_wires(8), post_outcomes=[_Resp(201), _Resp(201), _WorkerKilled()])
    reader = _Reader(3)
    with pytest.raises(_WorkerKilled):
        _tick(frappe_stub, dp2, reader)
    # ref 0 and 1 completed; the cursor passed exactly them. ref 2 was charged BEFORE its call.
    assert frappe_stub.cache_obj.values["rt_bin_view_cursor"] == _ref(1)
    state = _retry_state(frappe_stub)
    assert [q.request_ref for q in state.pending()] == [_ref(2)]
    assert state.failures(_ref(2)) == 1

    nxt = _tick(frappe_stub, dp2, reader)
    # ref 2 is retried by the drain (not again as a new feed request); the feed resumes after ref 1.
    assert _refs(nxt) == [_ref(2), _ref(3), _ref(4), _ref(5), _ref(6)]
    _tick(frappe_stub, dp2, reader)
    counts = _post_counts(dp2)
    assert counts == {**{_ref(i): 1 for i in range(8)}, _ref(2): 2}  # the killed call + one retry
    assert len(_retry_state(frappe_stub)) == 0


# --- substitute review of #56 at 17891fc ---------------------------------------------------------


def _paged_request(i):
    contracts = importlib.import_module(f"{_ROOT}.connector.bin_view.contracts")
    return contracts.BinViewRequest.from_wire(_wire(_ref(i), max_windows=20))


def _fill_retry_set_with_in_flight_paged(fake):
    rs = r.RetrySet()
    for i in range(r.MAX_PENDING):
        rs.record_failure(_paged_request(i))
        fake.jobs.append({"job_id": f"rt_bin_view_paged::{_ref(i)}"})  # queued/running
    fake.cache_obj.values["rt_bin_view_retry"] = rs.to_state()


@pytest.mark.parametrize("max_windows", [20, None])
def test_a_full_retry_set_holds_new_requests_back_instead_of_abandoning_them(frappe_stub, max_windows):
    _fill_retry_set_with_in_flight_paged(frappe_stub)
    new = _wire(_ref(500), max_windows=max_windows)
    dp2 = _FeedDp2([new])
    reader = _Reader(3)

    assert _tick(frappe_stub, dp2, reader, run_jobs=False) == []
    # Not abandoned, not attempted, and the cursor did NOT pass it.
    assert _events(frappe_stub, "bin_view.report.incomplete_abandoned") == []
    assert len(_events(frappe_stub, "bin_view.poll.retry_set_full")) == 1
    assert frappe_stub.cache_obj.values.get("rt_bin_view_cursor") is None
    assert _ref(500) not in _retry_state(frappe_stub)
    assert not any(job["job_id"] == f"rt_bin_view_paged::{_ref(500)}" for job in frappe_stub.jobs)

    # Room frees: one held request's job finishes and its outcome is folded in on the next tick.
    frappe_stub.jobs.pop(0)
    frappe_stub.cache_obj.set_value(
        f"rt_bin_view_outcome::{_ref(0)}", {"resolved": True, "reason": "", "detail": ""}, expires_in_sec=60
    )
    posts = _tick(frappe_stub, dp2, reader, run_jobs=False)
    assert _ref(0) not in _retry_state(frappe_stub)
    assert frappe_stub.cache_obj.values["rt_bin_view_cursor"] == _ref(500)  # picked up, then passed
    if max_windows is None:
        assert _refs(posts) == [_ref(500)]  # attempted inline
    else:
        assert frappe_stub.jobs[-1]["job_id"] == f"rt_bin_view_paged::{_ref(500)}"  # handed off


def _seed_paged(fake, i, failures=1):
    rs = r.RetrySet()
    for _ in range(failures):
        if _ref(i) in rs:
            rs.charge_retry(_paged_request(i))
        else:
            rs.record_failure(_paged_request(i))
    fake.cache_obj.values["rt_bin_view_retry"] = rs.to_state()


@pytest.mark.parametrize("resolved", [True, False])
def test_a_job_finishing_after_the_fold_is_folded_at_hand_off_not_re_run(frappe_stub, resolved):
    _seed_paged(frappe_stub, 0)
    dp2 = _Dp2([])
    poller = _poller(frappe_stub, dp2, _Reader(3))
    real_fold = poller._fold_job_outcomes

    def fold_then_job_finishes(retries):
        real_fold(retries)  # reads no outcome ...
        frappe_stub.cache_obj.set_value(  # ... then the job writes its outcome and exits
            f"rt_bin_view_outcome::{_ref(0)}",
            {"resolved": resolved, "reason": "" if resolved else "ConnectionError", "detail": ""},
            expires_in_sec=60,
        )

    poller._fold_job_outcomes = fold_then_job_finishes
    poller.run_bin_view_poll()

    assert frappe_stub.jobs == []  # not charged and re-enqueued
    assert frappe_stub.cache_obj.make_key(f"rt_bin_view_outcome::{_ref(0)}") not in frappe_stub.cache_obj.redis
    state = _retry_state(frappe_stub)
    if resolved:
        assert _ref(0) not in state
    else:
        assert state.failures(_ref(0)) == 1  # settled, not charged again this tick
        (_, rec), = _events(frappe_stub, "bin_view.report.retry_scheduled")
        assert rec["reason"] == "ConnectionError"


def test_taking_an_outcome_is_atomic_and_one_shot(frappe_stub):
    poller = _poller(frappe_stub, _Dp2([]), _Reader(3))
    frappe_stub.cache_obj.set_value(f"rt_bin_view_outcome::{_ref(0)}", {"resolved": True}, expires_in_sec=60)
    assert poller._take_outcome(_ref(0)) == {"resolved": True}
    assert poller._take_outcome(_ref(0)) is None


_KEY_CONFLICT = {"error": {"code": "idempotency_key_conflict", "message": "m"}}


def test_a_v1_retry_hitting_a_key_conflict_is_already_reported_not_an_operator_conflict(frappe_stub):
    _seed_retry_set(frappe_stub, 1)  # held v1 request (its first attempt's response was lost)
    dp2 = _Dp2([], post_outcomes=[_Resp(409, _KEY_CONFLICT)])
    assert _refs(_tick(frappe_stub, dp2, _Reader(3))) == [_ref(0)]
    (level, rec), = _events(frappe_stub, "bin_view.report.already_reported")
    assert level == "info" and rec["request_ref"] == _ref(0)
    assert _events(frappe_stub, "bin_view.report.conflict") == []
    assert len(_retry_state(frappe_stub)) == 0  # resolved, never blind-retried


def test_a_first_attempt_key_conflict_still_needs_operator_attention(frappe_stub):
    dp2 = _Dp2([_page(_wire(max_windows=None))], post_outcomes=[_Resp(409, _KEY_CONFLICT)])
    _tick(frappe_stub, dp2, _Reader(3))
    (level, _rec), = _events(frappe_stub, "bin_view.report.conflict")
    assert level == "error"
    assert _events(frappe_stub, "bin_view.report.already_reported") == []
    assert len(_retry_state(frappe_stub)) == 0


def test_a_paged_retry_job_is_told_it_is_a_retry(frappe_stub):
    _seed_paged(frappe_stub, 0)
    _tick(frappe_stub, _Dp2([]), _Reader(3), run_jobs=False)
    (job,) = frappe_stub.jobs
    assert job["retried"] is True
    frappe_stub.cache_obj.values.pop("rt_bin_view_cursor")  # the next feed starts from scratch
    _tick(frappe_stub, _FeedDp2([_wire(_ref(7))]), _Reader(3), run_jobs=False)
    (first,) = [job for job in frappe_stub.jobs if job["job_id"] == f"rt_bin_view_paged::{_ref(7)}"]
    assert first["retried"] is False
