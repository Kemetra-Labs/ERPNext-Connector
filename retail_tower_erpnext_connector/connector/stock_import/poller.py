# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Stock import poller — the scheduled entrypoint (RT-1287): BENCH-VALIDATION.

``run_stock_import_poll`` is meant for a ``scheduler_events`` cron, like the bin-view poller. It
is NOT registered in ``hooks.py`` by RT-1287: ``hooks.py`` is a gated surface (standing-rules §3)
and the contract's rollout order puts Backend-Core first (RT-1288 serves the routes). Until both
happen the entrypoint is inert, and even when registered it does nothing unless the site config
switch :data:`.frappe_glue.ENABLE_SWITCH` is on.

One tick:

  1. resends a report left pending by an earlier tick (a response never arrived), unchanged:
     the contract's retry rule is "resend the stored body", never re-read ERPNext for a retry;
  2. pulls wanted reads (``stockImportPullRequests``) from the saved feed cursor, page by page;
  3. for each request reads the ledger facts (:func:`.reader.read_report`), stores the report body
     as pending (durable) BEFORE it is sent, sends it under ``stockimport-{requestRef}``, and on a
     definite answer clears the pending body AND moves the cursor past the request in ONE commit.

Definite answers: 201/200 recorded (200 = replay), 409 (``idempotency_key_conflict`` or
``import_report_conflict``: the request already has its report; logged, never blind-retried),
404 (stale or closed run) and 400/413 (Backend-Core refused this body; logged as an error, and the
request is passed: whether Backend-Core offers it again is RT-1288's). A transport failure or an
unexpected status leaves the body pending and ends the tick; the next tick resends it first.

A failed ERPNext read ends the tick with the cursor before that request, so it is read again next
tick. The failures are counted per request; after :data:`MAX_READ_FAILURES` the request is passed
with an error log (``stock_import.read.abandoned``) and reported to Sentry, so one deterministic
failure cannot stop the import of every other warehouse. A feed item that does not parse is passed
by the page cursor (``stock_import.feed.invalid_item``).

Bounded: at most :data:`MAX_REPORTS_PER_TICK` reports, and no Backend-Core call starts once the
elapsed time plus the 30 s transport timeout would pass :data:`_TICK_BUDGET_S` (240 s), under
Frappe's 300 s default-queue job timeout.

State (cursor, pending body, read-failure counts) is ONE compressed JSON record in its own
``tabDefaultValue`` row (``parent`` :data:`_STATE_PARENT`), read and written with the query builder
and committed at each checkpoint. Not ``frappe.db.set_global``: that clears the whole site cache
on every write (``__global`` is a common default key). Not a Connector Settings field: that is a
gated DocType change. Durable across a Redis flush; compressed because a full page of 100 rows can
exceed a ``Text`` column.

Known limits (documented for RT-1288 and the bench):

* ``creation`` is naive site wall time. In a zone with daylight saving (Africa/Cairo since 2023),
  rows stamped during the second pass of a repeated hour sort before a ``readThrough`` taken in the
  first pass; only a run-start ``overlapSeconds`` of at least 3600 + ``settleSeconds`` re-reads them.
* A 413 is reachable with 100 rows at maximum field lengths while Backend-Core keeps the 100 kB
  default body limit (the contract leaves the route limit to RT-1288).
"""

from __future__ import annotations

import base64
import json
import time
import zlib

import frappe

from ..observability.sentry_client import capture_tick_error, report_unhandled
from ..request_id import new_request_id
from ..text import scrub
from .reader import read_report
from .transport import ReportConflict, ReportNotFound, ReportRejected, SnapshotRequired, StockImportClient

_LOGGER = "retail_tower_stock_import"
_STATE_PARENT = "__rt_connector_stock_import"
_STATE_KEY = "state"
MAX_READ_FAILURES = 5
_MAX_PAGES_PER_TICK = 20
MAX_REPORTS_PER_TICK = 10
_CALL_TIMEOUT_S = 30
_TICK_BUDGET_S = 240
_monotonic = time.monotonic


class _Budget:
	def __init__(self) -> None:
		self._start = _monotonic()
		self._reports = MAX_REPORTS_PER_TICK
		self.exhausted = False

	def can_call(self) -> bool:
		ok = _monotonic() - self._start + _CALL_TIMEOUT_S <= _TICK_BUDGET_S
		self.exhausted = self.exhausted or not ok
		return ok

	def take_report(self) -> bool:
		if self._reports <= 0:
			self.exhausted = True
			return False
		if not self.can_call():
			return False
		self._reports -= 1
		return True


def run_stock_import_poll() -> None:
	"""Scheduler entrypoint: resend a pending report, then pull, read and report."""
	from .frappe_glue import enabled

	if not enabled():
		return
	request_id = new_request_id()
	with report_unhandled(job_name="stock_import_poll", request_id=request_id):
		_run_tick(request_id)


def _run_tick(request_id: str) -> None:
	_STATE.request_id = request_id
	log = frappe.logger(_LOGGER)
	try:
		client, source, clock = _build(request_id)
	except Exception as exc:  # config/auth not ready: skip the tick, no partial work
		log.warning({"event": "stock_import.poll.skipped", "reason": scrub(str(exc))})
		return
	budget = _Budget()
	if _flush_pending(client, budget):
		_drain_feed(client, source, clock, budget)
	if budget.exhausted:
		log.info({"event": "stock_import.poll.budget_exhausted"})


def _drain_feed(client, source, clock, budget: _Budget) -> None:
	"""Pull pages while the budget allows; the cursor only passes requests with a definite answer."""
	handled: set[str] = set()
	for _ in range(_MAX_PAGES_PER_TICK):
		if not budget.can_call():
			return
		try:
			page = client.pull_requests(since=_load_state().get("cursor"))
		except SnapshotRequired:
			frappe.logger(_LOGGER).warning({"event": "stock_import.feed.snapshot_required"})
			_update_state(cursor=None)
			return  # re-baseline from the start next tick
		if not _process_page(client, source, clock, budget, page, handled):
			return  # out of budget or a failure mid-page: the rest waits for the next tick
		_update_state(cursor=page.cursor)
		if page.next_page_token is None:
			return


def _process_page(client, source, clock, budget: _Budget, page, handled: set[str]) -> bool:
	"""Handle the page's requests in order; False when one of them stops the tick."""
	for ref, detail in page.invalid_items:
		# Not parseable, so it cannot be read; the page cursor passes it.
		frappe.logger(_LOGGER).error(
			{
				"event": "stock_import.feed.invalid_item",
				"request_ref": scrub(str(ref)),
				"detail": scrub(detail),
			}
		)
	for request in page.items:
		if request.request_ref in handled:
			continue
		if not budget.take_report():
			return False
		handled.add(request.request_ref)
		if not _read_and_report(client, source, clock, request):
			return False
	return True


def _read_and_report(client, source, clock, request) -> bool:
	"""Read, store pending, send; True when the request has a definite outcome."""
	try:
		report = read_report(source, request, read_at=clock.now_iso())
	except Exception as exc:  # ERPNext read failure: retry the read next tick, up to a bound
		return _read_failed(request, exc)
	pending = {"requestRef": request.request_ref, "itemCursor": request.item_cursor, "body": report.to_wire()}
	_update_state(pending=pending)
	return _send(client, pending)


def _read_failed(request, exc: Exception) -> bool:
	"""Count a failed read; True (pass the request) once :data:`MAX_READ_FAILURES` is reached."""
	ref = request.request_ref
	failures = dict(_load_state().get("readFailures") or {})
	count = int(failures.get(ref, 0)) + 1
	record = {
		"request_ref": ref,
		"warehouse": request.erpnext_warehouse_ref,
		"failures": count,
		"detail": scrub(str(exc)),
	}
	if count < MAX_READ_FAILURES:
		failures[ref] = count
		_update_state(readFailures=failures)
		frappe.logger(_LOGGER).warning({"event": "stock_import.read.failed", **record})
		return False
	failures.pop(ref, None)
	_update_state(readFailures=failures, cursor=request.item_cursor)
	frappe.logger(_LOGGER).error({"event": "stock_import.read.abandoned", **record})
	capture_tick_error(exc, request_id=_STATE.request_id, job_name="stock_import_poll")
	return True


def _flush_pending(client, budget) -> bool:
	"""Resend a report left pending by an earlier tick; True when none is left."""
	pending = _load_state().get("pending")
	if not pending:
		return True
	if not budget.take_report():
		return False
	return _send(client, pending)


def _send(client, pending: dict) -> bool:
	"""POST the stored body. On a definite answer clear it and pass the request in one commit."""
	ref = pending["requestRef"]
	body = pending["body"]
	log = frappe.logger(_LOGGER)
	record = {
		"request_ref": ref,
		"rows": len(body.get("rows") or []),
		"refused": body.get("refusal") is not None,
	}
	try:
		result = client.report_rows(ref, body)
	except ReportConflict as exc:
		log.error({"event": "stock_import.report.conflict", **record, "code": exc.code})
	except ReportNotFound as exc:
		log.info({"event": "stock_import.report.stale", **record, "detail": scrub(str(exc))})
	except ReportRejected as exc:
		log.error({"event": "stock_import.report.rejected", **record, "code": scrub(str(exc))})
	except Exception as exc:  # transport / unexpected status: keep the body, resend next tick
		log.warning({"event": "stock_import.report.retry_scheduled", **record, "detail": scrub(str(exc))})
		return False
	else:
		event = "stock_import.report.replayed" if result.replayed else "stock_import.report.recorded"
		log.info({"event": event, **record, "exhausted": body.get("exhausted")})
	failures = dict(_load_state().get("readFailures") or {})
	failures.pop(ref, None)
	_update_state(pending=None, cursor=pending.get("itemCursor"), readFailures=failures)
	return True


def _build(correlation_id: str):
	from .. import http_transport
	from ..bin_view.frappe_glue import UtcClock
	from .frappe_glue import FrappeLedgerSource

	settings = frappe.get_doc("Connector Settings")
	transport = http_transport.build_http_transport(settings)
	return StockImportClient(transport, correlation_id=correlation_id), FrappeLedgerSource(), UtcClock()


class _TickContext:
	request_id = ""


_STATE = _TickContext()


def _load_state() -> dict:
	"""The poller's state record (``{}`` when none): ``cursor``, ``pending``, ``readFailures``."""
	dv = frappe.qb.DocType("DefaultValue")
	rows = (
		frappe.qb.from_(dv)
		.select(dv.defvalue)
		.where(dv.parent == _STATE_PARENT)
		.where(dv.defkey == _STATE_KEY)
		.run()
	)
	text = rows[0][0] if rows else None
	if not text:
		return {}
	return json.loads(zlib.decompress(base64.b64decode(text)).decode("utf-8"))


def _update_state(**changes) -> None:
	"""Apply ``changes`` to the state record and commit it as one write (delete + insert).

	Key order is kept, so a resent pending body serialises exactly as the first send did.
	"""
	state = _load_state()
	state.update(changes)
	raw = json.dumps(state, separators=(",", ":")).encode("utf-8")
	text = base64.b64encode(zlib.compress(raw, 9)).decode("ascii")
	dv = frappe.qb.DocType("DefaultValue")
	frappe.qb.from_(dv).delete().where(dv.parent == _STATE_PARENT).where(dv.defkey == _STATE_KEY).run()
	frappe.qb.into(dv).columns(dv.name, dv.parent, dv.parenttype, dv.defkey, dv.defvalue).insert(
		frappe.generate_hash(length=10), _STATE_PARENT, "__default", _STATE_KEY, text
	).run()
	frappe.db.commit()
