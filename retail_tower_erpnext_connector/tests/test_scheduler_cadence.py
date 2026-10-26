# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-45/RT-46 — pin the scheduler cadence of the Connector's pollers.

Owner decision (RT-45): a captured sale must reach ERPNext in <= ~1 minute via the existing
pull model, so the posting poller runs every minute (Frappe's finest cron granularity). The
019 bin-view poller is NOT part of that decision and stays on */5. ``hooks.py`` is plain data
(re-applied on every ``bench migrate``), so this is a pure unit test — no frappe, no bench.
"""

from retail_tower_erpnext_connector import hooks

POSTING_POLLER = "retail_tower_erpnext_connector.connector.posting.poller.run_posting_poll"
BIN_VIEW_POLLER = "retail_tower_erpnext_connector.connector.bin_view.poller.run_bin_view_poll"
HEARTBEAT = "retail_tower_erpnext_connector.connector.health.sender.run_heartbeat"


def _cron_expressions_for(job: str) -> list[str]:
	cron = hooks.scheduler_events.get("cron", {})
	return [expr for expr, jobs in cron.items() if job in jobs]


def test_posting_poller_runs_every_minute():
	assert _cron_expressions_for(POSTING_POLLER) == ["* * * * *"]


def test_bin_view_poller_stays_every_five_minutes():
	assert _cron_expressions_for(BIN_VIEW_POLLER) == ["*/5 * * * *"]


def test_heartbeat_runs_every_minute():
	# RT-564: Backend-Core marks an instance stale 5 minutes after its last heartbeat (spec 020),
	# so the beat runs every minute, its own job beside the posting poller.
	assert _cron_expressions_for(HEARTBEAT) == ["* * * * *"]
