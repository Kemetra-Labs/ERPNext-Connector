# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1287 — the stock import poller tick and the frappe glue switch, against a frappe stand-in.

``poller`` and ``frappe_glue`` import frappe, so they load against a minimal stand-in (the
RT-39 / RT-71 pattern); the real bench run stays a deferred validation step (standing-rules §6).

Covered: off by default (no call at all); the report body is stored pending before it is sent and
the cursor passes a request only after a definite answer; a lost response is resent unchanged on
the next tick (the contract's retry rule) and the cursor then passes that request; 409 / 404 / 400
are definite and not retried; a failed ERPNext read leaves the cursor before the request;
``snapshot_required`` re-baselines; the tick budget; and the voucher-fact mapping of the glue.
"""

import importlib
import sys
import types
from datetime import datetime

import pytest

_ROOT = "retail_tower_erpnext_connector"
_POLLER = f"{_ROOT}.connector.stock_import.poller"
_GLUE = f"{_ROOT}.connector.stock_import.frappe_glue"
_MISSING = object()
_REF = "11111111-1111-4111-8111-111111111111"
_REF_2 = "22222222-2222-4222-8222-222222222222"


class _Logger:
	def __init__(self):
		self.records = []

	def __getattr__(self, level):
		return lambda record: self.records.append((level, record))


class _Col:
	def __init__(self, name):
		self.name = name

	def __eq__(self, value):
		return (self.name, value)

	__hash__ = object.__hash__


class _Table:
	def __getattr__(self, name):
		return _Col(name)


class _Query:
	"""Just enough of frappe.qb for the poller's ``tabDefaultValue`` state row."""

	def __init__(self, db, kind):
		self.db, self.kind, self.conds, self.values = db, kind, [], None

	def select(self, *_cols):
		return self

	def delete(self):
		self.kind = "delete"
		return self

	def where(self, cond):
		self.conds.append(cond)
		return self

	def columns(self, *cols):
		self.cols = [c.name for c in cols]
		return self

	def insert(self, *values):
		self.values = dict(zip(self.cols, values, strict=True))
		return self

	def _match(self, row):
		return all(row.get(k) == v for k, v in self.conds)

	def run(self):
		if self.kind == "select":
			return [(r["defvalue"],) for r in self.db.rows if self._match(r)]
		if self.kind == "delete":
			self.db.rows = [r for r in self.db.rows if not self._match(r)]
			return None
		self.db.rows.append(self.values)
		return None


class _Db:
	def __init__(self):
		self.rows = []
		self.commits = 0

	def commit(self):
		self.commits += 1
		self.committed = [dict(r) for r in self.rows]


@pytest.fixture
def frappe_stub():
	before = set(sys.modules)
	saved = sys.modules.get("frappe", _MISSING)
	fake = types.ModuleType("frappe")
	fake.log = _Logger()
	fake.logger = lambda *_a, **_k: fake.log
	fake.db = _Db()
	fake.qb = types.SimpleNamespace(
		DocType=lambda _name: _Table(),
		from_=lambda _t: _Query(fake.db, "select"),
		into=lambda _t: _Query(fake.db, "insert"),
	)
	fake.generate_hash = lambda length=10: "h" * length
	fake.captured = []
	fake.conf = {}
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
	return [(lvl, rec) for lvl, rec in fake.log.records if name is None or rec.get("event") == name]


def _wire(ref=_REF, cursor=None):
	return {
		"requestRef": ref,
		"runRef": "44444444-4444-4444-8444-444444444444",
		"pageSeq": 0,
		"storeId": "33333333-3333-4333-8333-333333333333",
		"erpnextWarehouseRef": "Stores - RT",
		"after": None,
		"settleSeconds": 300,
		"overlapSeconds": 0,
		"maxRows": 100,
		"itemCursor": cursor or f"ic-{ref[:2]}",
	}


class _Resp:
	def __init__(self, status=201, body=None):
		self.status, self.body, self.headers = status, body or {}, {}


class _Backend:
	"""One scripted feed page per pull and scripted POST outcomes (default 201)."""

	def __init__(self, pages=(), posts=()):
		self.pages = list(pages)
		self.post_outcomes = list(posts)
		self.gets, self.posts = [], []

	def get(self, path, *, params, headers):
		self.gets.append(params.get("since"))
		if self.pages:
			page = self.pages.pop(0)
			if isinstance(page, BaseException):
				raise page
			return page
		return {"items": [], "cursor": params.get("since") or "c-0", "next_page_token": None}

	def post(self, path, *, json, headers):
		self.posts.append({"path": path, "body": json, "key": headers["Idempotency-Key"]})
		if self.post_outcomes:
			outcome = self.post_outcomes.pop(0)
			if isinstance(outcome, BaseException):
				raise outcome
			return outcome
		return _Resp(201)


class _Source:
	def __init__(self, fail=False):
		self.fail = fail
		self.reads = 0

	def warehouse_exists(self, warehouse):
		if self.fail:
			raise RuntimeError("db gone")
		return True

	def site_now(self):
		return datetime(2026, 11, 11, 12, 0)

	def site_timezone(self):
		from zoneinfo import ZoneInfo

		return ZoneInfo("UTC")

	def read_ledger(self, warehouse, *, start, cutoff, limit):
		self.reads += 1
		return []

	def bundle_entries(self, bundles):
		return {}

	def voucher_facts(self, vouchers):
		return {}


class _FailFor(_Source):
	"""Reads fail for one warehouse only."""

	def __init__(self, warehouse):
		super().__init__()
		self.bad = warehouse

	def warehouse_exists(self, warehouse):
		if warehouse == self.bad:
			raise RuntimeError("deterministic read failure")
		return True


class _Clock:
	def now_iso(self):
		return "2026-11-11T12:00:00.000Z"


def _cursor(poller):
	return poller._load_state().get("cursor")


def _pending(poller):
	return poller._load_state().get("pending")


def _poller(fake, backend, source=None, enabled=True):
	fake.conf = {"rt_connector_stock_import_enabled": 1} if enabled else {}
	poller = importlib.import_module(_POLLER)
	from retail_tower_erpnext_connector.connector.stock_import.transport import StockImportClient

	poller.capture_tick_error = lambda exc, **kw: fake.captured.append((exc, kw))
	poller._build = lambda rid: (
		StockImportClient(backend, correlation_id=rid),
		source or _Source(),
		_Clock(),
	)
	return poller


def _page(*refs, cursor="c-1", nxt=None):
	return {"items": [_wire(ref) for ref in refs], "cursor": cursor, "next_page_token": nxt}


def test_off_by_default_makes_no_call(frappe_stub):
	backend = _Backend([_page(_REF)])
	_poller(frappe_stub, backend, enabled=False).run_stock_import_poll()
	assert backend.gets == [] and backend.posts == []


def test_reports_each_request_and_advances_the_cursor(frappe_stub):
	backend = _Backend([_page(_REF, _REF_2, cursor="c-9")])
	poller = _poller(frappe_stub, backend)
	poller.run_stock_import_poll()
	assert [p["key"] for p in backend.posts] == [f"stockimport-{_REF}", f"stockimport-{_REF_2}"]
	body = backend.posts[0]["body"]
	assert body == {
		"rows": [],
		"readThrough": None,
		"exhausted": True,
		"readAt": "2026-11-11T12:00:00.000Z",
		"refusal": None,
	}
	assert _cursor(poller) == "c-9"
	assert _pending(poller) is None
	assert len(_events(frappe_stub, "stock_import.report.recorded")) == 2


def test_cursor_is_saved_only_after_the_pending_body_is_cleared(frappe_stub):
	backend = _Backend([_page(_REF)], posts=[ConnectionError("down")])
	poller = _poller(frappe_stub, backend)
	poller.run_stock_import_poll()
	pending = _pending(poller)
	assert pending["requestRef"] == _REF and pending["body"] == backend.posts[0]["body"]
	assert _cursor(poller) is None  # not past the unanswered request
	assert _events(frappe_stub, "stock_import.report.retry_scheduled")


def test_lost_response_is_resent_unchanged_then_the_cursor_passes_it(frappe_stub):
	backend = _Backend([_page(_REF)], posts=[ConnectionError("down")])
	source = _Source()
	poller = _poller(frappe_stub, backend, source)
	poller.run_stock_import_poll()
	reads_after_first = source.reads
	poller.run_stock_import_poll()
	assert backend.posts[1]["body"] == backend.posts[0]["body"]
	assert backend.posts[1]["key"] == backend.posts[0]["key"]
	assert source.reads == reads_after_first  # the retry never re-reads ERPNext
	assert _pending(poller) is None
	# The cursor moved past the resent request before the next pull, so it is not handed out again.
	assert backend.gets[-1] == _wire(_REF)["itemCursor"]
	assert _cursor(poller) == _wire(_REF)["itemCursor"]


def test_pending_resend_failing_again_stops_the_tick(frappe_stub):
	backend = _Backend([_page(_REF)], posts=[ConnectionError("down"), ConnectionError("still down")])
	poller = _poller(frappe_stub, backend)
	poller.run_stock_import_poll()
	gets = len(backend.gets)
	poller.run_stock_import_poll()
	assert len(backend.gets) == gets  # no pull while a body is pending
	assert _pending(poller) is not None


@pytest.mark.parametrize(
	("resp", "event", "level"),
	[
		(_Resp(409, {"error": {"code": "import_report_conflict"}}), "stock_import.report.conflict", "error"),
		(_Resp(404, {"error": {"code": "not_found"}}), "stock_import.report.stale", "info"),
		(_Resp(400, {"error": {"code": "validation_error"}}), "stock_import.report.rejected", "error"),
		(_Resp(200, {}), "stock_import.report.recorded", "info"),
	],
)
def test_definite_answers_clear_pending_and_advance(frappe_stub, resp, event, level):
	backend = _Backend([_page(_REF, cursor="c-5")], posts=[resp])
	poller = _poller(frappe_stub, backend)
	poller.run_stock_import_poll()
	assert [lvl for lvl, _ in _events(frappe_stub, event)] == [level]
	assert _pending(poller) is None
	assert _cursor(poller) == "c-5"
	assert len(backend.posts) == 1


def test_failed_read_leaves_the_cursor_before_the_request(frappe_stub):
	backend = _Backend([_page(_REF, cursor="c-5")])
	poller = _poller(frappe_stub, backend, _Source(fail=True))
	poller.run_stock_import_poll()
	assert backend.posts == []
	assert _cursor(poller) is None and _pending(poller) is None
	assert _events(frappe_stub, "stock_import.read.failed")


def test_snapshot_required_rebaselines(frappe_stub):
	class _Http(Exception):
		response = type(
			"R", (), {"status_code": 409, "json": lambda _s: {"error": {"code": "snapshot_required"}}}
		)()

	backend = _Backend([_Http("409")])
	poller = _poller(frappe_stub, backend)
	poller._update_state(cursor="stale")
	poller.run_stock_import_poll()
	assert _cursor(poller) is None
	assert _events(frappe_stub, "stock_import.feed.snapshot_required")


def test_follows_next_page_token(frappe_stub):
	backend = _Backend([_page(_REF, cursor="c-1", nxt="t"), _page(_REF_2, cursor="c-2")])
	poller = _poller(frappe_stub, backend)
	poller.run_stock_import_poll()
	assert backend.gets == [None, "c-1"]
	assert _cursor(poller) == "c-2"


def test_duplicate_request_in_one_tick_is_reported_once(frappe_stub):
	backend = _Backend([_page(_REF, _REF)])
	_poller(frappe_stub, backend).run_stock_import_poll()
	assert len(backend.posts) == 1


def test_report_count_budget_leaves_the_rest_for_the_next_tick(frappe_stub):
	refs = [f"{i:02d}111111-1111-4111-8111-111111111111" for i in range(12)]
	backend = _Backend([_page(*refs, cursor="c-end")])
	poller = _poller(frappe_stub, backend)
	poller.run_stock_import_poll()
	assert len(backend.posts) == poller.MAX_REPORTS_PER_TICK
	assert _cursor(poller) == _wire(refs[poller.MAX_REPORTS_PER_TICK - 1])["itemCursor"]
	assert _events(frappe_stub, "stock_import.poll.budget_exhausted")


def test_time_budget_stops_before_a_call(frappe_stub):
	backend = _Backend([_page(_REF)])
	poller = _poller(frappe_stub, backend)
	ticks = iter([0, 0, 1000, 1000, 1000])
	poller._monotonic = lambda: next(ticks)
	poller.run_stock_import_poll()
	assert backend.posts == []


def test_skips_when_settings_are_not_ready(frappe_stub):
	poller = _poller(frappe_stub, _Backend())

	def _boom(_rid):
		raise ValueError("dp2_base_url missing")

	poller._build = _boom
	poller.run_stock_import_poll()
	assert _events(frappe_stub, "stock_import.poll.skipped")


def test_state_is_one_compressed_row_written_in_one_commit(frappe_stub):
	poller = _poller(frappe_stub, _Backend())
	pending = {"requestRef": _REF, "itemCursor": "ic", "body": {"rows": [{"a": "x" * 2000}] * 100}}
	poller._update_state(pending=pending, cursor="c-1")
	[row] = frappe_stub.db.rows
	assert row["parent"] == "__rt_connector_stock_import" and row["defkey"] == "state"
	assert len(row["defvalue"]) < 10_000
	assert poller._load_state() == {"pending": pending, "cursor": "c-1"}
	commits = frappe_stub.db.commits
	poller._update_state(pending=None, cursor="c-2")
	assert frappe_stub.db.commits == commits + 1
	assert poller._load_state() == {"pending": None, "cursor": "c-2"}
	assert len(frappe_stub.db.rows) == 1


def test_definite_answer_clears_pending_and_moves_cursor_in_the_same_commit(frappe_stub):
	backend = _Backend([_page(_REF, cursor="c-5")])
	poller = _poller(frappe_stub, backend)
	seen = []
	real = poller._update_state

	def _spy(**changes):
		seen.append(set(changes))
		real(**changes)

	poller._update_state = _spy
	poller.run_stock_import_poll()
	assert {"pending", "cursor", "readFailures"} in seen


def test_read_failures_are_bounded_then_the_request_is_passed(frappe_stub):
	page = {"items": [{**_wire(_REF), "erpnextWarehouseRef": "Bad - RT"}, _wire(_REF_2)], "cursor": "c-5"}
	backend = _Backend([{**page, "next_page_token": None} for _ in range(10)])
	poller = _poller(frappe_stub, backend, _FailFor("Bad - RT"))
	for _ in range(poller.MAX_READ_FAILURES - 1):
		poller.run_stock_import_poll()
		assert backend.posts == [] and _cursor(poller) is None
	poller.run_stock_import_poll()
	assert [p["key"] for p in backend.posts] == [f"stockimport-{_REF_2}"]
	assert _events(frappe_stub, "stock_import.read.abandoned")[0][0] == "error"
	assert len(frappe_stub.captured) == 1
	assert poller._load_state()["readFailures"] == {}
	assert _cursor(poller) == "c-5"


def test_invalid_feed_item_is_logged_and_does_not_block_the_page(frappe_stub):
	bad = {**_wire(_REF), "maxRows": 0}
	backend = _Backend([{"items": [bad, _wire(_REF_2)], "cursor": "c-7", "next_page_token": None}])
	poller = _poller(frappe_stub, backend)
	poller.run_stock_import_poll()
	assert [p["key"] for p in backend.posts] == [f"stockimport-{_REF_2}"]
	[(level, rec)] = _events(frappe_stub, "stock_import.feed.invalid_item")
	assert level == "error" and rec["request_ref"] == _REF
	assert _cursor(poller) == "c-7"


# --- glue (switch and voucher facts) --------------------------------------------------------------


@pytest.mark.parametrize(
	("value", "on"),
	[
		(None, False),
		(0, False),
		("0", False),
		(1, True),
		("1", True),
		(True, True),
		("true", True),
		("yes", False),
	],
)
def test_enable_switch(frappe_stub, value, on):
	glue = importlib.import_module(_GLUE)
	frappe_stub.conf = {} if value is None else {glue.ENABLE_SWITCH: value}
	assert glue.enabled() is on


def test_voucher_facts_map_provenance_and_missing_columns(frappe_stub):
	glue = importlib.import_module(_GLUE)
	columns = {
		"Sales Invoice": {"is_return", "return_against", "rt_source_system", "rt_external_id"},
		"Stock Entry": {"purpose"},
	}
	rows = {
		"Sales Invoice": [
			{"name": "SINV-1", "is_return": 0, "rt_source_system": "retail_tower", "rt_external_id": "x"}
		],
		"Stock Entry": [{"name": "STE-1", "purpose": "Repack"}],
	}
	frappe_stub.db.has_column = lambda doctype, column: column in columns[doctype]
	asked = {}

	def _get_all(doctype, filters, fields):
		asked[doctype] = fields
		return rows[doctype]

	frappe_stub.get_all = _get_all
	facts = glue.FrappeLedgerSource().voucher_facts([("Sales Invoice", "SINV-1"), ("Stock Entry", "STE-1")])
	assert facts[("Sales Invoice", "SINV-1")].has_retail_provenance is True
	assert facts[("Stock Entry", "STE-1")] == glue.VoucherFacts(purpose="Repack")
	assert asked["Stock Entry"] == ["name", "purpose"]  # no column the DocType lacks is selected


def test_bundle_entries_keep_bundle_order(frappe_stub):
	glue = importlib.import_module(_GLUE)
	frappe_stub.get_all = lambda *a, **k: [
		{"parent": "SBB-1", "idx": 1, "batch_no": "B1", "qty": -2},
		{"parent": "SBB-1", "idx": 2, "batch_no": "B2", "qty": -1},
	]
	out = glue.FrappeLedgerSource().bundle_entries(["SBB-1"])
	assert [(e.batch_no, e.qty) for e in out["SBB-1"]] == [("B1", -2.0), ("B2", -1.0)]
