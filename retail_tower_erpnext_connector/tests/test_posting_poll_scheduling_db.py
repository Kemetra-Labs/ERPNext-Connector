# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1354 — the posting poll's Frappe scheduling, proven on a real bench (real ``ScheduledJobType``).

``test_posting_poll_cadence.py`` models Frappe's two scheduling rules over a fake frappe. This module
runs the REAL ones: after the Connector's setter (the poller calls it at the start of every run) the stored job has ``create_log = 1``; with it a run
that STARTS at :41 and ends past the next minute boundary leaves ``last_execution`` at the start, so
the next tick is due; with ``create_log = 0`` (what Frappe creates) the same run leaves it at the end
and the next tick is not due; and ``enqueue()`` never queues a job that is still queued or running.

Runs only on a bench (``bench --site <site> run-tests --app retail_tower_erpnext_connector``; the
``connector/bench`` job). Without a connected site it skips, so the frappe-free pytest run is
unaffected. Everything is written inside the test transaction and rolled back; ``frappe.db.commit`` is
patched out while Frappe's own ``update_scheduler_log`` runs.
"""

import unittest
from datetime import datetime, timedelta
from unittest import mock

try:
	import frappe
except ImportError:  # the frappe-free unit-test environment
	frappe = None

JOB_TYPE = "Scheduled Job Type"
# A wall-clock the scheduler could tick at: :41 past the minute, as on lab-03 after the RT-317 deploy.
START = datetime(2030, 1, 1, 12, 0, 41)
POLL_SECONDS = 25  # ends at 12:01:06, past the 12:01:00 boundary


def _connected_site() -> bool:
	# Checked at run time, not import time: a fake ``frappe`` left in sys.modules by a unit test has
	# no connected site, and a real bench connects before it runs the tests.
	return frappe is not None and bool(getattr(getattr(frappe, "local", None), "site", None))


class TestPostingPollScheduling(unittest.TestCase):
	def setUp(self):
		if not _connected_site():
			self.skipTest("needs a bench site with the app installed (bench run-tests)")
		from frappe.core.doctype.scheduled_job_type import scheduled_job_type as sjt

		from retail_tower_erpnext_connector.connector import scheduling

		self.sjt = sjt
		self.scheduling = scheduling
		self.job_name = frappe.db.get_value(JOB_TYPE, {"method": scheduling.POSTING_POLL_METHOD})
		self.assertTrue(self.job_name, "Scheduled Job Type row for run_posting_poll is missing")

	def tearDown(self):
		if _connected_site():
			frappe.db.rollback()

	def _job(self, create_log):
		frappe.db.set_value(JOB_TYPE, self.job_name, "create_log", create_log, update_modified=False)
		frappe.db.set_value(JOB_TYPE, self.job_name, "last_execution", None, update_modified=False)
		return frappe.get_doc(JOB_TYPE, self.job_name)

	def _ensure(self):
		"""The setter commits at once; keep that inside the test transaction (rolled back in tearDown)."""
		with mock.patch.object(frappe.local.db, "commit") as commit:
			changed = self.scheduling.ensure_posting_poll_logged()
		self.assertEqual(commit.call_count, int(changed))
		return changed

	def _run_once(self, job):
		"""Drive Frappe's own status writes for one run: Start at START, Complete POLL_SECONDS later."""
		job.scheduler_log = None  # what ScheduledJobType.execute() sets before the first status
		with mock.patch.object(frappe.local.db, "commit"):
			for status, at in (("Start", START), ("Complete", START + timedelta(seconds=POLL_SECONDS))):
				with mock.patch.object(self.sjt, "now_datetime", return_value=at):
					job.update_scheduler_log(status)
		job.reload()

	def test_the_setter_turns_create_log_on_and_is_idempotent(self):
		self._job(create_log=0)
		self.assertTrue(self._ensure())
		self.assertEqual(frappe.db.get_value(JOB_TYPE, self.job_name, "create_log"), 1)
		self.assertFalse(self._ensure())

	def test_with_create_log_a_poll_ending_past_the_boundary_leaves_the_next_tick_due(self):
		self._job(create_log=0)
		self._ensure()
		job = frappe.get_doc(JOB_TYPE, self.job_name)
		self.assertEqual(job.create_log, 1)
		self._run_once(job)
		self.assertEqual(frappe.utils.get_datetime(job.last_execution), START)  # stamped at Start
		self.assertTrue(job.is_event_due(current_time=START + timedelta(seconds=60)))

	def test_without_create_log_the_same_poll_skips_the_next_tick(self):
		# The lab bug, reproduced on the real class: last_execution is the END, so the next tick at :41
		# finds the 12:02:00 boundary still ahead.
		job = self._job(create_log=0)
		self._run_once(job)
		self.assertEqual(
			frappe.utils.get_datetime(job.last_execution), START + timedelta(seconds=POLL_SECONDS)
		)
		self.assertFalse(job.is_event_due(current_time=START + timedelta(seconds=60)))

	def test_enqueue_never_queues_a_job_that_is_still_queued_or_running(self):
		# Under create_log the job is due again while it may still be running, so this guard is the only
		# overlap protection: two runs of the poll never execute at the same time. force=True skips the
		# due check, so the queue check is the only thing that can refuse.
		job = self._job(create_log=1)
		with (
			mock.patch.object(self.sjt, "enqueue") as enqueue,
			mock.patch.object(job, "is_job_in_queue", return_value=True),
		):
			self.assertFalse(job.enqueue(force=True))
		enqueue.assert_not_called()

	def test_enqueue_queues_a_job_that_is_idle(self):
		job = self._job(create_log=1)
		with (
			mock.patch.object(self.sjt, "enqueue") as enqueue,
			mock.patch.object(job, "is_job_in_queue", return_value=False),
		):
			self.assertTrue(job.enqueue(force=True))
		enqueue.assert_called_once()
