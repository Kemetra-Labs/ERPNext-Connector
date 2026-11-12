# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1354 — keep the posting poll's cadence independent of the scheduler's tick phase. ⏳ BENCH-VALIDATION.

Frappe v16 decides whether a ``Scheduled Job Type`` is due from its ``last_execution``: the next cron
boundary AFTER it. With ``create_log = 0`` (the default for a ``Cron`` job) ``update_scheduler_log()``
writes ``last_execution`` on every status, including Complete, so a poll that ends after the next
minute boundary makes the following tick find the job "not due" and the poll runs every other
minute (RT-317 comment 16756: 7 polls in 15 minutes at 3000 sales/h, ERP posting lag p95 61 s -> 116 s).
With ``create_log = 1`` Frappe writes ``last_execution`` only at Start, so the next run is due one
minute after the previous run STARTED, whatever the scheduler's tick phase.

``hooks.py`` cannot set it: Frappe's ``insert_single_event`` keeps the stored value of every field
except ``frequency`` and ``cron_format`` once the row exists. So the poller itself makes sure of it at
the start of every run (:func:`ensure_posting_poll_logged`: one indexed read, a write only when the
flag was off, committed at once). That also corrects an operator who unticks "Create Log" in the desk,
needs no migrate or install ordering (Frappe's ``install_app`` runs ``sync_jobs`` after ``after_install``), and changes no
``hooks.py`` statement, so the release stays ``image-only`` for the lab's rollback class (RT-927 treats
any ``hooks.py`` change outside ``scheduler_events`` as ``restore``). The first run after a deploy still
ends with the old in-memory flag (Frappe loaded the job before the write), so that one run is stamped at
its end as before; every later run is stamped at its start. No Frappe or ERPNext core is changed.

Cost: one ``Scheduled Job Log`` row per run (about 1440 a day). Frappe's Log Settings clears that
doctype after 7 days by default (``default_log_clearing_doctypes``; a site whose Log Settings already
lists it keeps its own value), about 10 000 small rows. A failed run now also keeps its traceback in
``Scheduled Job Log.details``: plain text, with local variables only when ``developer_mode`` is on
(and then redacted by Frappe). Single-flight is unchanged: ``enqueue()`` still refuses a job that is
queued or running.
"""

from __future__ import annotations

import frappe

_LOGGER = "retail_tower_posting"
POSTING_POLL_METHOD = "retail_tower_erpnext_connector.connector.posting.poller.run_posting_poll"


def ensure_posting_poll_logged() -> bool:
	"""Idempotently turn ``create_log`` on for the posting poll; return whether this call changed it.

	A missing row is a logged no-op. The write is committed at once: it is the first write of the run
	(Frappe already committed the Start status), so the commit takes nothing else with it, and a tick
	that later raises (Frappe rolls the run back) or hits the glue's deadlock rollback cannot discard it.
	"""
	row = frappe.db.get_value(
		"Scheduled Job Type", {"method": POSTING_POLL_METHOD}, ["name", "create_log"], as_dict=True
	)
	if row is None:
		frappe.logger(_LOGGER).warning(
			{"event": "posting.poll.scheduling.row_missing", "method": POSTING_POLL_METHOD}
		)
		return False
	if row.create_log:
		return False
	frappe.db.set_value("Scheduled Job Type", row.name, "create_log", 1, update_modified=False)
	frappe.db.commit()
	frappe.logger(_LOGGER).info({"event": "posting.poll.scheduling.create_log_enabled", "job": row.name})
	return True
