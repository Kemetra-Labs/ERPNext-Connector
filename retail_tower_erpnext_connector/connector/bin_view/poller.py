# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Bin-view poller — the scheduled entrypoint — BENCH-VALIDATION.

Registered as a ``scheduler_events`` cron in ``hooks.py`` (Frappe v15 and v16 run it on the DEFAULT
queue, 300 s job timeout). On each tick it:

  1. builds the 019 client over the spec-003 auth (reuses Connector Settings
     ``dp2_base_url`` + ``dp2_token``) + the frappe-backed Bin reader + a UTC clock;
  2. folds in the outcomes that finished paged-report jobs left behind (see below);
  3. drains the bounded retry set (RT-176, :mod:`.retry`), oldest first: a v1 request is
     re-attempted inline (charged and checkpointed before it runs, checkpointed again after);
     a paged request is handed off to a long-queue job (cheap — no network);
  4. pulls wanted Bin-view reads (``binViewPullRequests``) page by page; a new v1 request is
     charged into the retry set, checkpointed, then read and reported inline (one report, v1
     key); a paged request (``maxWindows > 1``) is handed off. The cursor advances request by
     request (to its ``itemCursor``) and only past a request whose outcome is durable.

INLINE WORK IS BOUNDED (Codex P1s on #56). All inline v1 attempts of a tick — retries and new
feed requests COMBINED — share :data:`MAX_INLINE_ATTEMPTS_PER_TICK` (5 x 30 s transport
timeout), and no DP2 call (pull or attempt) starts once ``elapsed + 30 s`` would pass
:data:`_TICK_BUDGET_S` (240 s), so the tick stays under Frappe v15/v16's 300 s default-queue
timeout. When the budget runs out mid-page, the rest of the page is left for the next tick: the
cursor stays before the first unprocessed request, so nothing is skipped or processed twice.

PAGED REPORTS RUN OFF THE CRON QUEUE (Codex P1s on #56). One paged attempt makes one DP2 call
per window (up to ``maxWindows``, 30 s transport timeout each), which can outlast the cron's
300 s default-queue timeout; a killed worker would restart the attempt at window 0 forever.
So the tick hands each paged request to :func:`run_paged_report_job` via
``frappe.enqueue(queue="long", timeout=<budget + margin>, job_id="rt_bin_view_paged::<ref>",
deduplicate=True)``: one job per request at a time, with an explicit timeout derived from
``worker.attempt_budget_s(maxWindows)`` (``maxWindows x 30 s + 60 s``, capped at 1200 s) plus
:data:`_JOB_TIMEOUT_MARGIN_S`, so always under the long queue's 1500 s. Inside the job an
:class:`~.worker.AttemptDeadline` (same budget) ends the attempt cleanly BEFORE any window call
that could overrun it — no ``isFinal`` is sent and the job is never killed mid-write. The
handed-off request is charged in the retry set BEFORE the enqueue; the job writes its outcome
to ``rt_bin_view_outcome::<ref>`` and the next tick folds it in (resolve, or settle the
failure). The retry set therefore has ONE writer — the cron tick, which Frappe never runs
twice at once — and a job that dies without an outcome simply counts as a failed attempt.
An attempt is never resumed across jobs/ticks: stock-view 1.2 defines no attempt lifetime or
continuation, only that window 0 under a new ``attemptRef`` supersedes, so every retry is a
fresh attempt.

A request whose report did not reach completion for a RETRYABLE reason (a transport error,
an unexpected DP2 status, a mid-attempt failure, a superseded attempt, an explicit
``complete: false``, the attempt deadline) stays in the retry set, which is saved BEFORE the
cursor advances past its page — so a failed run is never stranded behind the saved cursor (the
pre-RT-176 defect). After ``retry.MAX_RETRY_TICKS`` failed retries it is abandoned with
``bin_view.report.incomplete_abandoned``. Deterministic refusals are terminal and logged, not
retried: the v1 overflow, the paged ``bin_view.window.limit_exceeded``, a 409 idempotency-key
conflict, and a 404 stale run.

This is the composition root; it imports ``frappe`` and the glue, so it is validated on
a bench, not locally (standing-rules §6). The pure pull/report/quantize/retry logic lives in
``transport.py`` + ``worker.py`` + ``retry.py`` (unit-tested locally); this is the thin frappe
shell (its tick and job logic is also exercised locally against a frappe stand-in).
"""

from __future__ import annotations

import pickle
import time
from dataclasses import dataclass

import frappe

from ..observability.sentry_client import report_unhandled
from ..request_id import new_request_id
from . import worker
from .contracts import BinViewRequest
from .retry import ABANDONED, RetrySet
from .transport import BinViewClient, ReportConflict, ReportNotFound, WindowSequenceConflict
from .worker import (
    AttemptDeadline,
    AttemptDeadlineExceeded,
    ReportIncomplete,
    WindowLimitExceeded,
    WindowOverflowError,
)

_LOGGER = "retail_tower_bin_view"
# Bounded pages per tick — the scheduler re-enters next cron; the report is idempotent
# (a re-report of the same request replays), so a mid-loop interruption is safe.
_MAX_PAGES_PER_TICK = 20
_CURSOR_KEY = "rt_bin_view_cursor"
_RETRY_KEY = "rt_bin_view_retry"
_OUTCOME_KEY_PREFIX = "rt_bin_view_outcome::"
_OUTCOME_TTL_S = 24 * 3600
_JOB_ID_PREFIX = "rt_bin_view_paged::"
_JOB_METHOD = "retail_tower_erpnext_connector.connector.bin_view.poller.run_paged_report_job"
_JOB_QUEUE = "long"
# Head-room between the in-job attempt deadline and the RQ job timeout (Bin read + job setup).
_JOB_TIMEOUT_MARGIN_S = 120
# The attempt deadline's clock (a module attribute so tests can drive it deterministically).
_monotonic = time.monotonic
# Inline v1 attempts (new feed requests + retries, combined) one cron tick may start: one DP2
# report call each, 30 s transport timeout -> at most ~150 s.
MAX_INLINE_ATTEMPTS_PER_TICK = 5
# No DP2 call (pull or inline attempt) starts once elapsed + 30 s would pass this; it keeps
# the whole tick under Frappe v15/v16's 300 s default-queue timeout.
_TICK_BUDGET_S = 240


@dataclass(frozen=True)
class _Outcome:
    """How one read-and-report attempt ended: ``resolved`` (complete or terminal) or not."""

    resolved: bool
    reason: str = ""
    detail: str = ""


class _TickBudget:
    """The cron tick's bound on INLINE network work (Codex P1s on #56).

    Frappe v15/v16 run the cron on the default queue (300 s job timeout) and every DP2 call has a
    30 s transport timeout. A tick therefore (a) starts at most :data:`MAX_INLINE_ATTEMPTS_PER_TICK`
    inline v1 attempts (new feed requests and retries COMBINED; one report call each) and (b)
    starts no DP2 call — pull or inline attempt — once ``elapsed + 30 s`` would pass
    :data:`_TICK_BUDGET_S`. Paged attempts never run inline (long-queue jobs).
    """

    def __init__(self) -> None:
        self._attempts = MAX_INLINE_ATTEMPTS_PER_TICK
        self._clock = AttemptDeadline(_TICK_BUDGET_S, monotonic=_monotonic)
        self.exhausted = False

    def can_call(self) -> bool:
        """Time check only: pulls (and paged hand-offs) continue after the attempt count is spent."""
        allowed = self._clock.allows_call()
        self.exhausted = self.exhausted or not allowed
        return allowed

    def take_attempt(self) -> bool:
        if self._attempts <= 0:
            self.exhausted = True
            return False
        if not self.can_call():
            return False
        self._attempts -= 1
        return True


@dataclass(frozen=True)
class _Io:
    """The DP2 client, the ERPNext Bin reader and the ``readAt`` clock of one tick or job."""

    client: object
    reader: object
    clock: object


@dataclass
class _Tick:
    """One cron tick's working set."""

    io: _Io
    retries: RetrySet
    budget: _TickBudget
    attempted: set
    blocked_on_full_set: bool = False


def run_bin_view_poll() -> None:
    """Scheduler entrypoint — fold job outcomes, drain retries, pull + report one batch."""
    # RT-39 / RT-635 — one UUID per tick: the X-Request-Id of every call and the Sentry tag of an
    # unhandled tick error (re-raised unchanged), so the event matches Backend-Core's log line.
    request_id = new_request_id()
    with report_unhandled(job_name="bin_view_poll", request_id=request_id):
        _run_bin_view_tick(request_id)


def _run_bin_view_tick(request_id: str) -> None:
    try:
        client, reader, clock = _build_bin_view_path(request_id)
    except Exception as exc:  # config/auth not ready — log + skip the tick (no partial work).
        frappe.logger(_LOGGER).warning(
            {"event": "bin_view.poll.skipped", "reason": _safe(str(exc))}
        )
        return

    retries = RetrySet(_load_retry_state())
    _fold_job_outcomes(retries)
    _save_retry_state(retries)
    tick = _Tick(_Io(client, reader, clock), retries, _TickBudget(), set())
    _drain_retries(tick)
    _drain_feed(tick)
    if tick.blocked_on_full_set:
        # The feed stopped before a new request: no slot to track it. Held requests free slots
        # as they resolve or reach the abandon bound, so this always clears.
        frappe.logger(_LOGGER).warning(
            {"event": "bin_view.poll.retry_set_full", "retries_held": len(retries)}
        )
    if tick.budget.exhausted:
        frappe.logger(_LOGGER).info(
            {"event": "bin_view.poll.budget_exhausted", "retries_held": len(retries)}
        )


def _drain_retries(tick: _Tick) -> None:
    """Hand off every paged retry; re-attempt v1 retries inline while the tick budget allows."""
    for request in tick.retries.pending():
        if request.item_window.is_paged:
            tick.attempted.add(request.request_ref)
            _hand_off(request, tick.retries)
        elif tick.budget.take_attempt():
            tick.attempted.add(request.request_ref)
            _retry_inline(tick, request)


def _drain_feed(tick: _Tick) -> None:
    """Pull pages while the tick budget allows; the cursor only ever passes durable outcomes.

    After each request the cursor moves to that request's ``itemCursor`` (the contract's
    "advanced cursor after this request item, <= the page cursor"); after a whole page, to the
    page ``cursor``. A request is only passed once its outcome is durable: resolved, charged into
    the (already saved) retry set, handed off, or held by the retry set already.
    """
    since: str | None = _load_cursor()
    for _ in range(_MAX_PAGES_PER_TICK):
        if not tick.budget.can_call():
            return
        page = tick.io.client.pull_requests(since=since)
        if not _process_page(tick, page):
            return  # out of budget mid-page — the rest of the page waits for the next tick
        _save_retry_state(tick.retries)
        _save_cursor(page.cursor)
        since = page.cursor
        if page.next_page_token is None:
            return  # caught up — no more wanted reads


def _process_page(tick: _Tick, page) -> bool:
    """Handle the page's requests in order; False when the budget stops it before one of them."""
    for request in page.items:
        if not _handle_feed_request(tick, request):
            return False
        # Retry state is saved by every path that changes it, so this request's outcome is
        # durable before the cursor passes it (no stranded run, no double count).
        _save_cursor(request.item_cursor)
    return True


def _handle_feed_request(tick: _Tick, request) -> bool:
    """One new feed request; False = left unprocessed for the next tick (cursor stays before it).

    That happens when the retry set has no slot to track it (it is never abandoned unattempted)
    or when no inline budget is left for a v1 attempt.
    """
    ref = request.request_ref
    if ref in tick.attempted or ref in tick.retries:
        return True  # handled this tick, or owned by the retry drain — never processed twice
    if not tick.retries.has_room_for(ref):
        tick.blocked_on_full_set = True
        return False
    if request.item_window.is_paged:
        tick.attempted.add(ref)
        _hand_off(request, tick.retries)
        return True
    if not tick.budget.take_attempt():
        return False
    tick.attempted.add(ref)
    _attempt_new_inline(tick, request)
    return True


def _attempt_new_inline(tick: _Tick, request) -> None:
    """A new v1 request: charge it into the retry set (checkpointed) BEFORE the network call.

    A worker killed mid-attempt then leaves it durably held for a retry. The caller has checked
    that the set has a slot for it.
    """
    _charge(request, tick.retries)
    _settle(request, tick.retries, _attempt_inline(tick, request))
    _save_retry_state(tick.retries)


def _attempt_inline(tick: _Tick, request) -> _Outcome:
    """Attempt a just-charged request; ``retried`` when an earlier attempt was charged too."""
    retried = tick.retries.failures(request.request_ref) >= 2
    return _attempt(tick.io, request, retried=retried)


def run_paged_report_job(request_wire: dict, retried: bool = False) -> None:
    """Long-queue job: ONE paged read-and-report attempt under an explicit time budget.

    Never touches the retry set (the cron tick is its only writer); it leaves its outcome under
    ``rt_bin_view_outcome::<ref>`` for the next tick to fold in. If the job dies before writing
    one, the next tick sees no outcome and no job in flight, and counts the charged attempt as
    failed.
    """
    # RT-635 — its own request id, like a tick: the X-Request-Id of the job's calls and the Sentry
    # tag of an unhandled job error (re-raised unchanged).
    request_id = new_request_id()
    with report_unhandled(job_name="bin_view_paged_report", request_id=request_id):
        _run_paged_report(request_wire, retried, request_id)


def _run_paged_report(request_wire: dict, retried: bool, request_id: str) -> None:
    request = BinViewRequest.from_wire(request_wire)
    try:
        client, reader, clock = _build_bin_view_path(request_id)
    except Exception as exc:
        frappe.logger(_LOGGER).warning(
            {"event": "bin_view.poll.skipped", "reason": _safe(str(exc)),
             "request_ref": request.request_ref}
        )
        return
    deadline = AttemptDeadline(
        worker.attempt_budget_s(request.item_window.max_windows or 1), monotonic=_monotonic
    )
    outcome = _attempt(_Io(client, reader, clock), request, deadline=deadline, retried=retried)
    frappe.cache().set_value(
        _OUTCOME_KEY_PREFIX + request.request_ref,
        {"resolved": outcome.resolved, "reason": outcome.reason, "detail": outcome.detail},
        expires_in_sec=_OUTCOME_TTL_S,
    )


def _retry_inline(tick: _Tick, request) -> None:
    """Re-attempt one v1 retry-set request: charge it, checkpoint, attempt, checkpoint again.

    The checkpoint BEFORE the attempt persists the charge (and the rotation to the back), so a
    worker killed mid-attempt still counts the attempt and the next tick starts with the
    requests behind it. The checkpoint AFTER persists the outcome (resolved or still queued).
    """
    if not _charge(request, tick.retries):
        return
    _settle(request, tick.retries, _attempt_inline(tick, request))
    _save_retry_state(tick.retries)


def _hand_off(request, retries: RetrySet) -> None:
    """Charge a paged request, checkpoint, then enqueue its attempt on the long queue.

    Skipped (not charged) while that request's job is still queued or running.
    """
    job_id = _JOB_ID_PREFIX + request.request_ref
    if _job_in_flight(job_id):
        return
    if request.request_ref in retries and _fold_outcome(request, retries):
        # The previous job finished after this tick's fold read its key: apply that outcome now
        # instead of charging and running the request again.
        _save_retry_state(retries)
        return
    if not _charge(request, retries):
        return
    budget = worker.attempt_budget_s(request.item_window.max_windows or 1)
    try:
        frappe.enqueue(
            _JOB_METHOD,
            queue=_JOB_QUEUE,
            timeout=budget + _JOB_TIMEOUT_MARGIN_S,
            job_id=job_id,
            deduplicate=True,
            request_wire=request.to_wire(),
            retried=retries.failures(request.request_ref) >= 2,
        )
    except Exception as exc:  # Redis/queue unavailable — already charged; the next tick retries.
        frappe.logger(_LOGGER).warning(
            {"event": "bin_view.report.enqueue_failed", "request_ref": request.request_ref,
             "detail": _safe(str(exc))}
        )


def _charge(request, retries: RetrySet) -> bool:
    """Count one attempt up front (rotating it to the back) and checkpoint; False = abandoned.

    A request already held is charged as a retry; a new one (from the feed) is recorded, which
    also applies the ``MAX_PENDING`` bound.
    """
    held = request.request_ref in retries
    charged = retries.charge_retry(request) if held else retries.record_failure(request)
    _save_retry_state(retries)
    if charged.status == ABANDONED:
        # A held request: its previous charged attempt was the last one allowed (it never
        # settled). A new one: the retry set is full.
        reason = "retry_budget_spent" if held else "retry_set_full"
        _log_failure(request, charged, _Outcome(False, reason))
        return False
    return True


def _settle(request, retries: RetrySet, outcome: _Outcome) -> None:
    if outcome.resolved:
        retries.resolve(request.request_ref)
    else:
        _log_failure(request, retries.settle_failed_retry(request), outcome)


def _fold_job_outcomes(retries: RetrySet) -> None:
    """Apply the outcomes finished paged-report jobs left for requests still in the retry set."""
    for request in retries.pending():
        _fold_outcome(request, retries)


def _fold_outcome(request, retries: RetrySet) -> bool:
    """Take (atomically) and apply the outcome a finished job left for ``request``; True if any."""
    raw = _take_outcome(request.request_ref)
    if not isinstance(raw, dict):
        return False
    outcome = _Outcome(bool(raw.get("resolved")), str(raw.get("reason") or ""),
                       str(raw.get("detail") or ""))
    _settle(request, retries, outcome)
    return True


def _take_outcome(request_ref: str):
    """GET + DEL of the job outcome key in ONE Redis transaction (MULTI/EXEC).

    Atomic, so an outcome can never be read twice or dropped between the read and the delete;
    works on every Redis version (unlike GETDEL, 6.2+). Bypasses ``get_value``'s per-process
    memo, which could otherwise return a stale miss within the same tick. The key and the
    pickled value use the same scheme as frappe's ``RedisWrapper.set_value`` (v15, unchanged in v16).
    """
    cache = frappe.cache()
    key = cache.make_key(_OUTCOME_KEY_PREFIX + request_ref)
    pipe = cache.pipeline(transaction=True)
    pipe.get(key)
    pipe.delete(key)
    raw, _deleted = pipe.execute()
    return None if raw is None else pickle.loads(raw)


def _attempt(io: _Io, request, *, deadline=None, retried: bool = False) -> _Outcome:
    """One read-and-report attempt for ``request``; classifies how it ended.

    Never raises an ``Exception``: completion and the terminal refusals are ``resolved`` (and
    logged); retryable failures come back unresolved with a reason for the caller to record.
    ``retried`` marks an attempt whose request was already charged for an earlier attempt.
    """
    ref = request.request_ref
    log = frappe.logger(_LOGGER)
    try:
        worker.process_request(request, client=io.client, reader=io.reader, clock=io.clock,
                               deadline=deadline)
    except WindowSequenceConflict as exc:
        if exc.window_seq != 0:
            # This attempt was superseded / went stale mid-sequence — retry with a fresh one.
            return _Outcome(False, "window_sequence_conflict", _safe(str(exc)))
        # Window 0 of a NEW attempt is refused only once another attempt of this request is
        # already complete (stock-view 1.2) — the run has its report; nothing to retry.
        log.info({"event": "bin_view.report.already_complete", "request_ref": ref,
                  "detail": _safe(str(exc))})
    except ReportConflict as exc:
        _log_key_conflict(ref, exc, retried=retried)
    except ReportNotFound as exc:
        # The run completed / went away between pull and report (non-disclosing).
        log.info({"event": "bin_view.report.stale", "request_ref": ref, "detail": _safe(str(exc))})
    except WindowOverflowError as exc:
        # v1 request: the warehouse exceeds the single window — REFUSE to report a
        # known-incomplete snapshot (no silent truncation). Deterministic: not retried.
        log.error({"event": "bin_view.window.overflow", "request_ref": ref,
                   "warehouse": request.erpnext_warehouse_ref, "detail": _safe(str(exc))})
    except WindowLimitExceeded as exc:
        # Paged request: more than maxWindows x maxItems items — nothing was reported.
        log.error({"event": "bin_view.window.limit_exceeded", "request_ref": ref,
                   "warehouse": request.erpnext_warehouse_ref, "limit": exc.limit,
                   "detail": _safe(str(exc))})
    except (AttemptDeadlineExceeded, ReportIncomplete) as exc:
        return _incomplete_attempt(ref, exc)
    except Exception as exc:  # transport / unexpected DP2 status / read failure — retryable.
        return _Outcome(False, type(exc).__name__, _safe(str(exc)))
    return _Outcome(True)


def _log_key_conflict(ref: str, exc: Exception, *, retried: bool) -> None:
    """409 ``idempotency_key_conflict``: resolved either way, never blind-retried.

    On a RETRY it is expected: an earlier attempt's report was committed but its response was
    lost, and this attempt re-read a different snapshot under the same v1 key — the run already
    has its report (info). On a first attempt it needs operator attention (error).
    """
    if retried:
        frappe.logger(_LOGGER).info(
            {"event": "bin_view.report.already_reported", "request_ref": ref,
             "detail": _safe(str(exc))}
        )
        return
    frappe.logger(_LOGGER).error(
        {"event": "bin_view.report.conflict", "request_ref": ref, "detail": _safe(str(exc))}
    )


def _incomplete_attempt(ref: str, exc: Exception) -> _Outcome:
    """A paged attempt that ended without completing the report — retryable."""
    if isinstance(exc, AttemptDeadlineExceeded):
        # Ended cleanly before a window that could overrun the budget (no isFinal sent).
        frappe.logger(_LOGGER).warning(
            {"event": "bin_view.report.deadline_reached", "request_ref": ref,
             "window_seq": exc.window_seq, "detail": _safe(str(exc))}
        )
        return _Outcome(False, "attempt_deadline", _safe(str(exc)))
    return _Outcome(False, "report_incomplete", _safe(str(exc)))


def _log_failure(request, failure, outcome: _Outcome) -> None:
    record = {
        "request_ref": request.request_ref,
        "warehouse": request.erpnext_warehouse_ref,
        "failures": failure.failures,
        "reason": outcome.reason,
        "detail": outcome.detail,
    }
    if failure.status == ABANDONED:
        frappe.logger(_LOGGER).error({"event": "bin_view.report.incomplete_abandoned", **record})
    else:
        frappe.logger(_LOGGER).warning({"event": "bin_view.report.retry_scheduled", **record})


def _job_in_flight(job_id: str) -> bool:
    """True while the paged-report job ``job_id`` is queued or started (Frappe v15/v16 RQ)."""
    from frappe.utils.background_jobs import is_job_enqueued

    return bool(is_job_enqueued(job_id))


def _build_bin_view_path(correlation_id: str):
    """Construct the 019 client + the frappe-backed Bin reader + a UTC clock.

    Reuses the Connector Settings ``dp2_base_url`` + ``dp2_token`` (the posting poller's
    transport pattern). Raises if base-URL/token unset → the tick skips cleanly.
    """
    from ..posting.poller import _build_http_transport  # reuse the auth-backed transport
    from .frappe_glue import FrappeBinReader, UtcClock

    settings = frappe.get_doc("Connector Settings")
    transport = _build_http_transport(settings)
    client = BinViewClient(transport, correlation_id=correlation_id)
    return client, FrappeBinReader(), UtcClock()


def _load_cursor() -> str | None:
    return frappe.cache().get_value(_CURSOR_KEY)


def _save_cursor(cursor: str) -> None:
    frappe.cache().set_value(_CURSOR_KEY, cursor)


def _load_retry_state():
    return frappe.cache().get_value(_RETRY_KEY)


def _save_retry_state(retries: RetrySet) -> None:
    frappe.cache().set_value(_RETRY_KEY, retries.to_state())


def _safe(message: str) -> str:
    from ..posting.reasons import scrub_message

    return scrub_message(message)
