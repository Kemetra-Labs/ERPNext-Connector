# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1383 — the posting poll's time budget: end the run before the next scheduler tick.

Frappe enqueues the poll every minute and refuses to enqueue it while the previous run is still queued
or running (the RQ job id ``scheduled_job||<name>``). A poll that ends after the next tick therefore
costs a whole tick: in RT-317 run a1c4 nothing posted for 51 s after a 57 s poll ended (it had started
13 s late behind the hourly jobs). The poll loop only had a page cap, no time bound.

:class:`TickBudget` answers one question for the poll loop: may another page start, i.e. is it expected
to finish before ``enqueued_at + interval - margin``? It is measured from the RQ enqueue time, not the
job start, because the queue wait is part of the problem. Frappe-free, so it is unit-tested locally.

Limits (stated, not hidden): the first page always runs (progress on every poll), and a page itself has
no time bound (the pull asks for up to 100 items); a stop only prevents starting the NEXT page.
"""

from __future__ import annotations

import math

# The cron is ``* * * * *``: one tick per minute. The margin covers tick jitter and the page-time
# estimate being the last page's duration, not a guarantee.
TICK_INTERVAL_SECONDS = 60.0
TICK_MARGIN_SECONDS = 5.0


class TickBudget:
	"""The deadline of one poll run, and the rule for starting another page."""

	def __init__(
		self,
		*,
		enqueued_at: float | None,
		started_at: float,
		interval: float = TICK_INTERVAL_SECONDS,
		margin: float = TICK_MARGIN_SECONDS,
	) -> None:
		anchor = _usable_enqueue_time(enqueued_at, started_at, interval)
		self.deadline = anchor + interval - margin

	def allows_another_page(self, *, now: float, last_page_seconds: float) -> bool:
		"""True when a page as long as the last one would still end by the deadline."""
		return now + last_page_seconds <= self.deadline


def _usable_enqueue_time(enqueued_at: float | None, started_at: float, interval: float) -> float:
	"""The enqueue time when it is plausible, else the job start (so the bound is never lifted).

	Unusable: missing or not finite, later than the start (clock skew), or more than ten intervals
	earlier (a stale RQ field). A genuine queue wait of up to ten intervals is kept: the deadline is then
	already past and only the first page (which the caller always runs) happens.
	"""
	if enqueued_at is None or not math.isfinite(enqueued_at):
		return started_at
	if enqueued_at > started_at or started_at - enqueued_at > 10 * interval:
		return started_at
	return enqueued_at
