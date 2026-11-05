# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Bounded retry set for bin-view requests whose report did not reach completion (RT-176).

DP2's bin-view feed is a keyset on the run, and the poller saves the feed cursor past every
page it has handled. Before RT-176 a request whose report failed (a transport error, a
mid-attempt failure) was therefore never offered again and its 017 run stayed ``running``
(the stranded-run defect at ``poller.py`` ~71). The poller now keeps such requests here —
persisted BEFORE the cursor moves — and re-reads + re-reports them on later ticks. A retry
of a connector-paged request is always a FRESH read attempt (new ``attemptRef`` from window
0), which DP2 treats as a supersede of the incomplete attempt.

The set is bounded twice: an entry is abandoned after :data:`MAX_RETRY_TICKS` failed retries
(the poller logs ``bin_view.report.incomplete_abandoned``), and at most
:data:`MAX_PENDING` requests are held at once (a failure that does not fit is abandoned at
once, with the same log).

The poller bounds the inline (v1) work of one tick — new feed requests and retries combined —
by ``poller.MAX_INLINE_ATTEMPTS_PER_TICK`` and a tick time budget (Codex P1s on #56), so a run
of slow transports cannot push the cron job past its queue timeout before the state is saved;
paged requests are handed off to a long-queue job instead (see ``poller``). A
retry is CHARGED before it runs (:meth:`RetrySet.charge_retry`) and the poller checkpoints the
set before and after every attempt, so an attempt cut short by a killed worker still counts
toward the abandon bound. Charging moves the request to the back of the order, so requests
not reached this tick go first next tick (FIFO rotation — no starvation).

Pure Python (NO frappe); the poller persists :meth:`RetrySet.to_state` in the frappe cache.
The state stores each request in its pulled WIRE shape, so a stored entry survives a
connector upgrade; an entry that no longer parses is dropped.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .contracts import BinViewRequest

# Retry ticks after the original failure before a request is abandoned. The bin-view cron runs
# every 5 minutes (hooks.py), so a request is retried for ~25 minutes.
MAX_RETRY_TICKS = 5
# Most requests held for retry at once (one per reconciliation run in flight).
MAX_PENDING = 200

QUEUED = "queued"
ABANDONED = "abandoned"


@dataclass(frozen=True)
class FailureOutcome:
	"""What :meth:`RetrySet.record_failure` did with a failed request."""

	status: str  # QUEUED | ABANDONED
	failures: int


class RetrySet:
	"""Requests awaiting a fresh report attempt, oldest-charged first (FIFO rotation)."""

	def __init__(self, state: object = None) -> None:
		self._entries: dict[str, tuple[BinViewRequest, int]] = {}
		if not isinstance(state, Mapping):
			return
		for ref, raw in state.items():
			try:
				request = BinViewRequest.from_wire(raw["request"])
				failures = int(raw["failures"])
			except (KeyError, TypeError, ValueError):
				continue  # unreadable entry (e.g. a pre-upgrade shape) — drop it
			if request.request_ref == ref and failures >= 1:
				self._entries[ref] = (request, failures)

	def __contains__(self, request_ref: object) -> bool:
		return request_ref in self._entries

	def __len__(self) -> int:
		return len(self._entries)

	def pending(self) -> list[BinViewRequest]:
		"""The requests to re-attempt this tick (a snapshot; safe to mutate the set while iterating)."""
		return [request for request, _ in self._entries.values()]

	def has_room_for(self, request_ref: str) -> bool:
		"""True when ``request_ref`` is held already or the set is below ``MAX_PENDING``."""
		return not self._is_full_for(request_ref)

	def failures(self, request_ref: str) -> int:
		entry = self._entries.get(request_ref)
		return 0 if entry is None else entry[1]

	def resolve(self, request_ref: str) -> None:
		"""Forget a request — its report completed, or it ended in a terminal (non-retryable) state."""
		self._entries.pop(request_ref, None)

	def record_failure(self, request: BinViewRequest) -> FailureOutcome:
		"""Count one more failed attempt; keep the request for the next tick, or abandon it.

		The first failure (from the feed) queues the request; each failed retry adds one. After
		the ``MAX_RETRY_TICKS``-th failed retry the request is abandoned and removed.
		"""
		ref = request.request_ref
		failures = self.failures(ref) + 1
		if self._should_abandon(ref, failures):
			return self._abandon(ref, failures)
		return self._requeue(request, failures)

	def charge_retry(self, request: BinViewRequest) -> FailureOutcome:
		"""Count a retry attempt BEFORE it runs and move the request to the back of the order.

		Charging up front means an attempt that never returns (the worker is killed mid-attempt)
		still uses up one retry once the poller has checkpointed the set. ``ABANDONED`` here means
		the previous charged attempt was already the last one allowed, so it is not attempted again.
		"""
		ref = request.request_ref
		failures = self.failures(ref) + 1
		if failures > MAX_RETRY_TICKS + 1:
			return self._abandon(ref, failures)
		return self._requeue(request, failures)

	def settle_failed_retry(self, request: BinViewRequest) -> FailureOutcome:
		"""A charged retry failed: keep it (already counted and rotated) or abandon it at the bound."""
		ref = request.request_ref
		failures = self.failures(ref)
		if failures > MAX_RETRY_TICKS:
			return self._abandon(ref, failures)
		return FailureOutcome(status=QUEUED, failures=failures)

	def _abandon(self, request_ref: str, failures: int) -> FailureOutcome:
		self._entries.pop(request_ref, None)
		return FailureOutcome(status=ABANDONED, failures=failures)

	def _requeue(self, request: BinViewRequest, failures: int) -> FailureOutcome:
		"""Store ``request`` with ``failures`` at the BACK of the order (pop + re-insert)."""
		self._entries.pop(request.request_ref, None)
		self._entries[request.request_ref] = (request, failures)
		return FailureOutcome(status=QUEUED, failures=failures)

	def _should_abandon(self, request_ref: str, failures: int) -> bool:
		"""Abandon once the retry budget is spent, or when a NEW request finds the set full."""
		return failures > MAX_RETRY_TICKS or self._is_full_for(request_ref)

	def _is_full_for(self, request_ref: str) -> bool:
		"""True when ``request_ref`` is not held yet and the set is already at ``MAX_PENDING``."""
		return request_ref not in self._entries and len(self._entries) >= MAX_PENDING

	def to_state(self) -> dict[str, dict[str, object]]:
		"""A plain, cache-storable form (round-trips through the constructor)."""
		return {
			ref: {"request": request.to_wire(), "failures": failures}
			for ref, (request, failures) in self._entries.items()
		}
