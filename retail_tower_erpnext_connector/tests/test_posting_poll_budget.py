# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1383 — the posting poll must end before the next scheduler tick.

RT-1354 fixed the every-other-minute phase skip. What is left (RT-317 run a1c4, lab-03, 2026-11-13): the
poll was enqueued on time at the :37 tick but started 13 s later behind ~11 hourly jobs, then ran 57 s
(a drain of the backlog). The next tick, at :36, found the job still running and Frappe refused to
enqueue it (single-flight), so nothing posted for 51 s after the run ended (17:01:46.7 to 17:02:37.9).

The poll loop had no time bound, only ``_MAX_PAGES_PER_TICK``. The fix: do not start another page when it
could not finish before the next tick (measured from the RQ enqueue time, with a margin). The poll then
ends before the tick, Frappe enqueues the next one on time, and nothing runs twice.

Frappe-free: the budget is a pure helper, and the loop is driven over a fake clock and a fake page.
"""

import importlib
import sys
import types
from datetime import UTC, timedelta, timezone
from itertools import pairwise

import pytest

from retail_tower_erpnext_connector.connector.posting.tick_budget import TickBudget

_POLLER = "retail_tower_erpnext_connector.connector.posting.poller"
_PKG = "retail_tower_erpnext_connector.connector.posting"
_SCHEMA = "retail_tower_erpnext_connector.connector.schema"
_GUARD = f"{_PKG}.index_guard"
_SCHEDULING = "retail_tower_erpnext_connector.connector.scheduling"
_MISSING = object()


class TestTickBudget:
	def test_deadline_is_the_enqueue_time_plus_the_interval_minus_the_margin(self):
		b = TickBudget(enqueued_at=1000.0, started_at=1013.0, interval=60.0, margin=5.0)
		assert b.deadline == 1055.0

	def test_the_interval_is_pinned_to_the_cron_it_is_measured_against(self):
		# The deadline assumes one tick per minute; a hooks.py cron change must change this too.
		from retail_tower_erpnext_connector import hooks
		from retail_tower_erpnext_connector.connector.posting.tick_budget import TICK_INTERVAL_SECONDS

		poll = "retail_tower_erpnext_connector.connector.posting.poller.run_posting_poll"
		assert hooks.scheduler_events["cron"]["* * * * *"][0] == poll
		assert TICK_INTERVAL_SECONDS == 60.0

	def test_another_page_is_allowed_only_if_it_is_expected_to_finish_before_the_deadline(self):
		b = TickBudget(enqueued_at=0.0, started_at=13.0)  # deadline 55
		assert b.allows_another_page(now=35.0, last_page_seconds=20.0) is True  # 35 + 20 = 55
		assert b.allows_another_page(now=35.0, last_page_seconds=20.1) is False

	def test_a_missing_enqueue_time_falls_back_to_the_start_time(self):
		b = TickBudget(enqueued_at=None, started_at=100.0)
		assert b.deadline == 155.0

	@pytest.mark.parametrize("enqueued_at", [-10_000.0, 5000.0, float("nan")])
	def test_an_unusable_enqueue_time_falls_back_to_the_start_time(self, enqueued_at):
		# Clock skew or a stale RQ value must neither stop every poll after one page nor lift the bound.
		b = TickBudget(enqueued_at=enqueued_at, started_at=100.0)
		assert b.deadline == 155.0

	def test_a_queue_wait_longer_than_the_interval_still_runs_one_page(self):
		b = TickBudget(enqueued_at=0.0, started_at=70.0)  # waited 70 s in the queue
		assert b.allows_another_page(now=71.0, last_page_seconds=1.0) is False  # but the caller ran page 1


class _Clock:
	def __init__(self, now):
		self.now = now

	def __call__(self):
		return self.now


class _Logger:
	def __init__(self):
		self.records = []

	def info(self, record):
		self.records.append(("info", record))

	def warning(self, record):
		self.records.append(("warning", record))

	def error(self, record):
		self.records.append(("error", record))


@pytest.fixture
def poller_env(monkeypatch):
	"""The poller over a fake ``frappe`` with a fake clock, cursor store and ``process_page``."""
	pkg = importlib.import_module(_PKG)
	# `connector.scheduling` is imported by the poller and binds the fake frappe: restore it too, or a
	# later test (the bench DB tests) would get a module bound to this file's fake.
	names = ("frappe", _POLLER, f"{_PKG}.frappe_store", f"{_PKG}.frappe_glue", _SCHEDULING)
	saved = [(sys.modules, n, sys.modules.get(n, _MISSING)) for n in names]
	saved += [(vars(pkg), n, vars(pkg).get(n, _MISSING)) for n in ("poller", "frappe_store", "frappe_glue")]
	saved_stubs = {n: sys.modules.get(n, _MISSING) for n in (_SCHEMA, _GUARD)}

	def _load(*, enqueued_at, start, page_seconds, pages_available):
		fake = types.ModuleType("frappe")
		fake._logger = _Logger()
		fake.logger = lambda name=None: fake._logger
		fake.exceptions = types.SimpleNamespace()  # the glue resolves its duplicate-key classes lazily
		sys.modules["frappe"] = fake
		for name in names[1:]:
			sys.modules.pop(name, None)
		poller = importlib.import_module(_POLLER)
		clock = _Clock(start)
		env = types.SimpleNamespace(poller=poller, clock=clock, log=fake._logger, saved_cursors=[], pulls=[])

		def process_page(client, ack_client, *, since, post_valid):
			env.pulls.append(since)
			clock.now += page_seconds
			n = len(env.pulls)
			more = n < pages_available
			return types.SimpleNamespace(
				cursor=f"c{n}",
				next_page_token="t" if more else None,
				degraded=False,
				degraded_message=None,
			)

		monkeypatch.setattr(poller.worker, "process_page", process_page)  # worker is shared: restored
		poller._now = clock
		poller._monotonic = clock  # one fake clock drives both; a wall-clock step is tested separately
		env.real_job_enqueued_at = poller._job_enqueued_at
		poller._job_enqueued_at = lambda: enqueued_at
		poller._ready_posting_path = lambda rid: (object(), lambda item: None)
		poller._load_cursor = lambda: None
		poller._save_cursor = env.saved_cursors.append
		# The tick's G5 guard (its own tests cover it) is stubbed green: this file is about the loop.
		sys.modules[_SCHEMA] = types.SimpleNamespace(index_present=lambda *a, **k: True)
		sys.modules[_GUARD] = types.SimpleNamespace(evaluate=lambda f: types.SimpleNamespace(ok=True))
		return env

	yield _load
	for stub in (_SCHEMA, _GUARD):
		sys.modules.pop(stub, None)
		if saved_stubs[stub] is not _MISSING:
			sys.modules[stub] = saved_stubs[stub]
	for namespace, name, value in saved:
		namespace.pop(name, None)
		if value is not _MISSING:
			namespace[name] = value


class TestJobEnqueuedAt:
	"""The real ``_job_enqueued_at`` over a fake ``rq`` (the parts most likely to break on an RQ upgrade)."""

	@pytest.fixture
	def enqueued_at(self, poller_env, monkeypatch):
		env = poller_env(enqueued_at=None, start=0.0, page_seconds=1.0, pages_available=1)

		def _with_rq(job):
			rq = types.ModuleType("rq")
			if isinstance(job, Exception):

				def _raise():
					raise job

				rq.get_current_job = _raise
			else:
				rq.get_current_job = lambda: job
			monkeypatch.setitem(sys.modules, "rq", rq)
			return env.real_job_enqueued_at()

		return _with_rq

	def test_naive_utc_enqueued_at_becomes_epoch_seconds(self, enqueued_at):
		from datetime import datetime

		job = types.SimpleNamespace(enqueued_at=datetime(2026, 11, 13, 15, 0, 37))  # RQ stores naive UTC
		assert enqueued_at(job) == datetime(2026, 11, 13, 15, 0, 37, tzinfo=UTC).timestamp()

	def test_an_aware_enqueued_at_is_not_shifted(self, enqueued_at):
		from datetime import datetime

		moment = datetime(2026, 11, 13, 17, 0, 37, tzinfo=timezone(timedelta(hours=2)))
		assert enqueued_at(types.SimpleNamespace(enqueued_at=moment)) == moment.timestamp()

	@pytest.mark.parametrize("job", [None, types.SimpleNamespace(enqueued_at=None), RuntimeError("no rq")])
	def test_no_job_or_a_failure_gives_none_and_never_raises(self, enqueued_at, job):
		assert enqueued_at(job) is None


class TestPollerLoop:
	def test_a1c4_the_poll_stops_before_the_next_tick(self, poller_env):
		# a1c4: enqueued at 0, started at 13 (queue wait), pages of ~22 s, a deep backlog (many pages).
		env = poller_env(enqueued_at=0.0, start=13.0, page_seconds=22.0, pages_available=20)
		env.poller._run_posting_tick("rid")
		# page 1 ends at 35; another 22 s page would end at 57 > 55 -> stop. Before the fix: all 20 pages.
		assert len(env.pulls) == 1
		assert env.clock.now == 35.0
		assert env.clock.now < 60.0  # ends before the next tick, so Frappe can enqueue the next poll

	def test_the_cursor_is_saved_after_each_page_and_resumes_where_the_stop_left_it(self, poller_env):
		env = poller_env(enqueued_at=0.0, start=0.0, page_seconds=20.0, pages_available=20)
		env.poller._run_posting_tick("rid")
		# 0 -> 20 -> 40, a third page would end at 60 > 55: two pages, both cursors saved, none re-pulled.
		assert env.pulls == [None, "c1"]
		assert env.saved_cursors == ["c1", "c2"]

	def test_the_first_page_always_runs_even_when_the_queue_wait_used_the_whole_budget(self, poller_env):
		env = poller_env(enqueued_at=0.0, start=80.0, page_seconds=5.0, pages_available=20)
		env.poller._run_posting_tick("rid")
		assert env.pulls == [None]
		assert env.saved_cursors == ["c1"]

	def test_a_short_backlog_is_unchanged_it_drains_and_stops_on_the_empty_token(self, poller_env):
		env = poller_env(enqueued_at=0.0, start=1.0, page_seconds=3.0, pages_available=3)
		env.poller._run_posting_tick("rid")
		assert len(env.pulls) == 3
		assert not [r for _, r in env.log.records if r.get("event") == "posting.poll.budget_stop"]

	def test_a_budget_stop_is_logged_once_with_its_numbers(self, poller_env):
		env = poller_env(enqueued_at=0.0, start=13.0, page_seconds=22.0, pages_available=20)
		env.poller._run_posting_tick("rid")
		stops = [r for _, r in env.log.records if r.get("event") == "posting.poll.budget_stop"]
		assert len(stops) == 1
		assert stops[0]["pages"] == 1

	def test_a_wall_clock_step_back_does_not_shorten_the_measured_page(self, poller_env):
		# The page duration comes from the monotonic clock: an NTP step back of 30 s during a 40 s page
		# must not make it look like 10 s (which would let a second page start and overrun the tick).
		env = poller_env(enqueued_at=0.0, start=13.0, page_seconds=0.0, pages_available=20)
		wall, mono = _Clock(13.0), _Clock(0.0)
		env.poller._now, env.poller._monotonic = wall, mono

		def process_page(client, ack_client, *, since, post_valid):
			env.pulls.append(since)
			mono.now += 40.0
			wall.now += 40.0 - 30.0  # the wall clock was stepped back 30 s meanwhile
			return types.SimpleNamespace(
				cursor="c", next_page_token="t", degraded=False, degraded_message=None
			)

		env.poller.worker.process_page = (
			process_page  # restored by the fixture's monkeypatch of the attribute
		)
		env.poller._run_posting_tick("rid")
		assert len(env.pulls) == 1  # 23 + 40 > 55: stop (a wall-clock duration of 10 s would have continued)

	def test_the_page_cap_still_applies(self, poller_env):
		env = poller_env(enqueued_at=0.0, start=0.0, page_seconds=0.01, pages_available=1000)
		env.poller._run_posting_tick("rid")
		assert len(env.pulls) == env.poller._MAX_PAGES_PER_TICK


def _simulate(*, queue_wait, backlog_seconds, page_seconds, budgeted, ticks=10):
	"""The scheduler single-flight model with the poll's own stop rule. Returns [(start, end), ...].

	A tick every 60 s (at :37 of each minute). The poll starts ``queue_wait`` s after its tick and, per
	page, takes ``page_seconds`` while ``backlog_seconds`` of work remains (the first run has a backlog,
	later runs a steady ~one page). Without the budget the run drains everything.
	"""
	runs = []
	running_until = None
	backlog = backlog_seconds
	for n in range(ticks):
		tick = 37.0 + 60.0 * n
		if running_until is not None and tick < running_until:
			continue  # "Skipped queueing ... found in queue"
		start = tick + queue_wait
		t, pages = start, 0
		while backlog > 0:
			t += page_seconds
			backlog -= page_seconds
			pages += 1
			if budgeted:
				b = TickBudget(enqueued_at=tick, started_at=start)
				if not b.allows_another_page(now=t, last_page_seconds=page_seconds):
					break
		runs.append((start, t))
		running_until = t
		backlog = max(backlog, 0) + page_seconds  # about one page of new sales arrives per minute
	return runs


class TestScheduleModel:
	def test_without_the_budget_the_a1c4_shape_loses_a_tick(self):
		runs = _simulate(queue_wait=13.0, backlog_seconds=57.0, page_seconds=19.0, budgeted=False)
		assert runs[0][1] > 97.0  # runs past the 97 s tick (37 + 60): that tick is skipped
		assert runs[1][0] - runs[0][0] > 60.0

	def test_with_the_budget_no_tick_is_lost_and_no_two_polls_overlap(self):
		runs = _simulate(queue_wait=13.0, backlog_seconds=57.0, page_seconds=19.0, budgeted=True)
		starts = [s for s, _ in runs]
		assert all(b - a == 60.0 for a, b in pairwise(starts)), runs
		assert all(nxt_start >= end for (_, end), (nxt_start, _) in pairwise(runs))
