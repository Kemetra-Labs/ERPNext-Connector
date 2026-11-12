# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1354 — the posting poll's cadence must not depend on the Frappe scheduler's tick phase.

Frappe v16 (``scheduled_job_type.py``, v16.52.0) decides whether a job is due from ``last_execution``:

  * ``get_next_execution()`` is the next cron boundary AFTER ``last_execution`` (for ``* * * * *``:
    the next whole minute);
  * with ``create_log = 0`` ``update_scheduler_log()`` writes ``last_execution`` on EVERY status,
    including Complete, so it is the END of the run; with ``create_log = 1`` it writes it only at
    Start;
  * ``enqueue()`` refuses while the job is still queued or running (the RQ job id
    ``scheduled_job||<name>``), so two runs of one job never overlap.

At the lab's 3000 sales/h a poll takes about 20 s. With the scheduler ticking at :41, that poll ended
after the next minute boundary, the next tick found the job "not due", and the poll ran every other
minute (7 polls in 15 minutes) and doubled the ERP posting lag (RT-317 comment 16756).

This module has two halves, both frappe-free (no bench):

  * the setter that turns ``create_log`` on (``connector/scheduling.py``), driven over a fake frappe;
  * a small MODEL of the two Frappe rules above, fed by what the setter wrote. It is a model, not
    Frappe: ``test_posting_poll_scheduling_db.py`` runs the same assertions on a real bench, and the
    RT-317 A1c rerun is the runtime proof.
"""

import importlib
import sys
import types
from datetime import datetime, timedelta
from itertools import pairwise

import pytest

from retail_tower_erpnext_connector import hooks

_MODULE = "retail_tower_erpnext_connector.connector.scheduling"
_POLL = "retail_tower_erpnext_connector.connector.posting.poller.run_posting_poll"
_OTHER = "retail_tower_erpnext_connector.connector.health.sender.run_heartbeat"
_MISSING = object()


class _Logger:
	def __init__(self):
		self.records = []

	def info(self, record):
		self.records.append(("info", record))

	def warning(self, record):
		self.records.append(("warning", record))


class _Db:
	"""The slice of ``frappe.db`` the setter uses, over an in-memory Scheduled Job Type table."""

	def __init__(self, rows):
		self.rows = rows  # name -> {"method": ..., "create_log": 0/1}
		self.writes = []
		self.commits = 0

	def get_value(self, doctype, filters, fieldname, as_dict=False):
		assert doctype == "Scheduled Job Type"
		for name, row in self.rows.items():
			if row["method"] == filters["method"]:
				return types.SimpleNamespace(**{f: (name if f == "name" else row[f]) for f in fieldname})
		return None

	def set_value(self, doctype, name, fieldname, value, update_modified=True):
		assert doctype == "Scheduled Job Type"
		self.writes.append((name, fieldname, value, update_modified))
		self.rows[name][fieldname] = value

	def commit(self):
		self.commits += 1


class _Frappe(types.ModuleType):
	def __init__(self, rows):
		super().__init__("frappe")
		self.db = _Db(rows)
		self._logger = _Logger()
		self.logger = lambda name=None: self._logger
		self.exceptions = types.SimpleNamespace()  # the glue resolves its duplicate-key classes lazily


@pytest.fixture
def scheduling(monkeypatch):
	"""Load ``connector.scheduling`` over a fresh fake ``frappe``; restore sys.modules afterwards."""
	saved = {n: sys.modules.get(n, _MISSING) for n in ("frappe", _MODULE)}

	def _load(rows):
		fake = _Frappe(rows)
		sys.modules["frappe"] = fake
		sys.modules.pop(_MODULE, None)
		return importlib.import_module(_MODULE), fake

	yield _load
	for name, value in saved.items():
		sys.modules.pop(name, None)
		if value is not _MISSING:
			sys.modules[name] = value


def _row(method, create_log):
	return {"method": method, "create_log": create_log}


class TestSetter:
	def test_turns_create_log_on_for_the_posting_poll_row(self, scheduling):
		mod, fake = scheduling({"job-1": _row(_POLL, 0)})
		assert mod.ensure_posting_poll_logged() is True
		assert fake.db.rows["job-1"]["create_log"] == 1
		# `modified` is left alone, and the write is committed at once so a later rollback of the tick
		# (an exception, the glue's deadlock handler) cannot discard it.
		assert fake.db.writes == [("job-1", "create_log", 1, False)]
		assert fake.db.commits == 1

	def test_is_idempotent(self, scheduling):
		mod, fake = scheduling({"job-1": _row(_POLL, 0)})
		mod.ensure_posting_poll_logged()
		fake.db.writes.clear()
		assert mod.ensure_posting_poll_logged() is False
		assert fake.db.writes == []

	def test_an_operator_who_turned_it_off_is_corrected_on_the_next_run(self, scheduling):
		mod, fake = scheduling({"job-1": _row(_POLL, 1)})
		fake.db.rows["job-1"]["create_log"] = 0  # someone unticked "Create Log" in the desk
		assert mod.ensure_posting_poll_logged() is True
		assert fake.db.rows["job-1"]["create_log"] == 1

	def test_touches_no_other_scheduled_job(self, scheduling):
		mod, fake = scheduling({"job-1": _row(_POLL, 0), "job-2": _row(_OTHER, 0)})
		mod.ensure_posting_poll_logged()
		assert fake.db.rows["job-2"]["create_log"] == 0
		assert [w[0] for w in fake.db.writes] == ["job-1"]

	def test_a_missing_row_is_a_logged_no_op_and_commits_nothing(self, scheduling):
		# Defensive: the scheduler only runs a job that has a row, but a manual call must not raise.
		mod, fake = scheduling({"job-2": _row(_OTHER, 0)})
		assert mod.ensure_posting_poll_logged() is False
		assert fake.db.writes == []
		assert fake.db.commits == 0
		assert [level for level, _ in fake._logger.records] == ["warning"]
		assert fake._logger.records[0][1]["event"] == "posting.poll.scheduling.row_missing"


class TestHooks:
	def test_no_migrate_or_install_hook_is_added(self):
		# RT-927 classes any hooks.py change outside scheduler_events as `restore`; this fix must stay
		# `image-only`, so the flag is kept by the poller and no after_* hook is touched.
		assert hooks.after_migrate == "retail_tower_erpnext_connector.connector.schema.after_migrate"
		assert hooks.after_install == "retail_tower_erpnext_connector.connector.schema.after_install"
		assert not hasattr(hooks, "after_sync")

	def test_the_cron_is_still_one_run_every_minute(self):
		assert hooks.scheduler_events["cron"]["* * * * *"][0] == _POLL


_POLLER = "retail_tower_erpnext_connector.connector.posting.poller"
_PKG = "retail_tower_erpnext_connector.connector.posting"


@pytest.fixture
def load_poller():
	"""Import the poller over a fake ``frappe`` (the poller is the scheduled entrypoint)."""
	pkg = importlib.import_module(_PKG)
	names = ("frappe", _POLLER, _MODULE, f"{_PKG}.frappe_store", f"{_PKG}.frappe_glue")
	saved = [(sys.modules, n, sys.modules.get(n, _MISSING)) for n in names]
	saved += [(vars(pkg), n, vars(pkg).get(n, _MISSING)) for n in ("poller", "frappe_store", "frappe_glue")]

	def _load(fake):
		sys.modules["frappe"] = fake
		for name in names[1:]:
			sys.modules.pop(name, None)
		return importlib.import_module(_POLLER)

	yield _load
	for namespace, name, value in saved:
		namespace.pop(name, None)
		if value is not _MISSING:
			namespace[name] = value


class TestPollerKeepsItLogged:
	def test_every_run_ensures_the_flag_before_it_pulls(self, load_poller, monkeypatch):
		fake = _Frappe({"job-1": _row(_POLL, 0)})
		poller = load_poller(fake)
		seen = []
		monkeypatch.setattr(
			poller, "_run_posting_tick", lambda rid: seen.append(fake.db.rows["job-1"]["create_log"])
		)
		poller.run_posting_poll()
		assert seen == [1]  # already on when the tick starts pulling

	def test_an_ensure_failure_is_a_warning_and_never_stops_the_tick(self, load_poller, monkeypatch):
		fake = _Frappe({})

		def _boom(*args, **kwargs):
			raise RuntimeError("db gone token=abc123")

		fake.db.get_value = _boom
		poller = load_poller(fake)
		ticks = []
		monkeypatch.setattr(poller, "_run_posting_tick", lambda rid: ticks.append(rid))
		poller.run_posting_poll()
		assert len(ticks) == 1
		level, record = fake._logger.records[0]
		assert level == "warning" and record["event"] == "posting.poll.scheduling.ensure_failed"
		assert "abc123" not in record["reason"]  # scrubbed like every other poller log detail


# --- a model of the two Frappe v16 rules (see the module docstring) ----------------------------


def _next_minute_after(moment: datetime) -> datetime:
	"""``croniter('* * * * *').get_next(start_time=moment)``: the next whole minute strictly after."""
	return moment.replace(second=0, microsecond=0) + timedelta(minutes=1)


def _simulate(*, create_log: int, poll_seconds: int, tick_phase: int = 41, minutes: int = 15):
	"""Run the scheduler model; return the list of (start, end) second offsets of each run.

	Frappe ticks every 60 s at a fixed phase. A tick starts the job only if it is due
	(``next_execution <= now``) AND it is not already queued/running (RQ job-id dedup).
	``last_execution`` is written at Start when ``create_log`` is on, else at every status (so at the
	end of the run).
	"""
	epoch = datetime(2030, 1, 1, 12, 0, 0)
	last_execution = epoch - timedelta(hours=1)  # an established job: its last run was long ago
	running_until = None
	runs = []
	for tick in range(minutes):
		now = epoch + timedelta(seconds=tick_phase + 60 * tick)
		if running_until is not None and now < running_until:
			continue  # "Skipped queueing ... found in queue": the single-flight guard
		if _next_minute_after(last_execution) > now:
			continue  # not due: the start of the bug
		end = now + timedelta(seconds=poll_seconds)
		running_until = end
		last_execution = now if create_log else end
		runs.append((int((now - epoch).total_seconds()), int((end - epoch).total_seconds())))
	return runs


def _applied(scheduling, create_log=0):
	"""The ``create_log`` Frappe holds for the poll row after the Connector's setter has run."""
	mod, fake = scheduling({"job-1": _row(_POLL, create_log)})
	mod.ensure_posting_poll_logged()
	return fake.db.rows["job-1"]["create_log"]


class TestCadenceModel:
	def test_the_model_reproduces_the_lab_bug_without_the_setter(self):
		# RT-317 A1c rerun: tick at :41, a 25 s poll ends past the next boundary -> every other tick lost.
		runs = _simulate(create_log=0, poll_seconds=25)
		assert 7 <= len(runs) <= 8, runs

	def test_a_poll_ending_past_the_minute_boundary_does_not_skip_the_next_run(self, scheduling):
		create_log = _applied(scheduling)
		runs = _simulate(create_log=create_log, poll_seconds=25)
		assert len(runs) == 15, runs  # one poll per minute for the whole 15 minutes
		assert all(end - start == 25 for start, end in runs)
		assert all(runs[i + 1][0] - runs[i][0] == 60 for i in range(len(runs) - 1))

	@pytest.mark.parametrize("tick_phase", [0, 1, 24, 41, 59])
	def test_the_cadence_does_not_depend_on_the_tick_phase(self, scheduling, tick_phase):
		# AC 3: whatever phase a scheduler restart lands on, one run per minute.
		create_log = _applied(scheduling)
		assert len(_simulate(create_log=create_log, poll_seconds=40, tick_phase=tick_phase)) == 15

	@pytest.mark.parametrize("poll_seconds", [20, 59, 70, 130, 400])
	def test_two_polls_never_overlap(self, scheduling, poll_seconds):
		create_log = _applied(scheduling)
		runs = _simulate(create_log=create_log, poll_seconds=poll_seconds, minutes=60)
		assert runs
		for (_, end), (next_start, _) in pairwise(runs):
			assert next_start >= end, (poll_seconds, runs)

	@pytest.mark.parametrize("poll_seconds", [70, 130, 400])
	def test_a_long_poll_loses_at_most_one_tick_between_runs(self, scheduling, poll_seconds):
		# Documented limit: a poll longer than ~60 s still skips the tick it overlaps (the guard), but
		# the next run starts at the first tick after it ends, never later.
		create_log = _applied(scheduling)
		runs = _simulate(create_log=create_log, poll_seconds=poll_seconds, minutes=60)
		for (_, end), (next_start, _) in pairwise(runs):
			assert next_start - end <= 60, (poll_seconds, runs)
