# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Heartbeat sender — the scheduled entrypoint (RT-564) — BENCH-VALIDATION.

Registered as its own ``scheduler_events`` cron job in ``hooks.py``, every minute. Backend-Core
marks an instance ``stale`` 5 minutes after its last heartbeat (spec 020), so a one-minute beat
leaves room for several missed or slow ticks before the verdict flips.

On each tick it:

  1. builds the heartbeat client over the auth-backed transport the pollers already use
     (Connector Settings ``dp2_base_url`` + ``dp2_token`` via ``get_password``, sent as
     ``Authorization: Bearer``); an unconfigured connector skips the tick;
  2. sends ``connectorReportHeartbeat`` with the contract's self-reported fields the connector can
     state truthfully: ``connectorVersion`` (the app version), ``erpnextReachable`` (whether this
     ERPNext site's database answers a trivial query; the connector runs inside the site, so that
     is what reaching ERPNext means for it) and ``sourceClockAt`` (provenance only).
     ``backlogIndicator`` is left out: the posting backlog lives in Backend-Core and the connector
     has no count of it, and the contract makes every field optional.

FAILS QUIETLY AND OBSERVABLY. Every failure — configuration, transport, a refused or rejected
heartbeat — ends in one structured log line and the job returns normally; nothing is re-raised and
nothing is retried within the tick (the next minute's beat is the retry). The heartbeat is a
separate scheduled job that shares no state with the posting or bin-view pollers, so a failing
heartbeat never blocks, delays or alters posting. Log lines carry the error class, the HTTP status
and error code, and a scrubbed message only — never the token or a request header.

This is the composition root; it imports ``frappe``, so it is validated on a bench, not locally
(standing-rules §6). The request body and status handling live in :mod:`.heartbeat` (pure,
unit-tested); this shell is exercised locally against a frappe stand-in.
"""

from __future__ import annotations

from datetime import datetime, timezone

import frappe

from ..request_id import new_request_id
from .heartbeat import (
	HeartbeatClient,
	HeartbeatError,
	HeartbeatRejected,
	HeartbeatReport,
	HeartbeatUnauthorized,
	format_source_clock,
)

_LOGGER = "retail_tower_heartbeat"


def run_heartbeat() -> None:
	"""Scheduler entrypoint — send one heartbeat. Never raises an ``Exception``."""
	# RT-39 — one UUID per tick, sent as the X-Request-Id and logged, so a failure line matches
	# Backend-Core's log line for the same call.
	request_id = new_request_id()
	try:
		_send_heartbeat(request_id)
	except HeartbeatUnauthorized as exc:
		# The credential is refused: posting with it fails too, so this needs an operator.
		_log_refusal("error", "connector.heartbeat.unauthorized", request_id, exc)
	except HeartbeatRejected as exc:
		# A 400 means the body broke the contract — a connector defect, not a transient fault.
		_log_refusal("error", "connector.heartbeat.rejected", request_id, exc)
	except HeartbeatError as exc:
		_log_refusal("warning", "connector.heartbeat.failed", request_id, exc)
	except Exception as exc:  # transport / unexpected — the next tick is the retry.
		frappe.logger(_LOGGER).warning(
			{
				"event": "connector.heartbeat.failed",
				"request_id": request_id,
				"error_class": type(exc).__name__,
				"detail": _safe(str(exc)),
			}
		)


def _send_heartbeat(request_id: str) -> None:
	try:
		client = _build_client(request_id)
	except Exception as exc:  # config/auth not ready — skip the beat, like the pollers do.
		frappe.logger(_LOGGER).warning(
			{"event": "connector.heartbeat.skipped", "request_id": request_id, "reason": _safe(str(exc))}
		)
		return
	report = HeartbeatReport(
		connector_version=_connector_version(),
		erpnext_reachable=_erpnext_reachable(),
		source_clock_at=format_source_clock(datetime.now(timezone.utc)),
	)
	ack = client.report(report)
	frappe.logger(_LOGGER).debug(
		{
			"event": "connector.heartbeat.sent",
			"request_id": request_id,
			"acknowledged_at": ack.acknowledged_at,
		}
	)


def _build_client(correlation_id: str) -> HeartbeatClient:
	"""The heartbeat client over the pollers' auth-backed transport (Gate G4 — token via get_password).

	Raises when ``dp2_base_url`` or ``dp2_token`` is unset, so the tick skips cleanly.
	"""
	from ..posting.poller import _build_http_transport  # reuse the auth-backed transport

	settings = frappe.get_doc("Connector Settings")
	return HeartbeatClient(_build_http_transport(settings), correlation_id=correlation_id)


def _connector_version() -> str:
	from ... import __version__

	return __version__


def _erpnext_reachable() -> bool:
	"""Self-report: does this ERPNext site's database answer? Never raises."""
	try:
		frappe.db.sql("SELECT 1")
	except Exception:
		return False
	return True


def _log_refusal(level: str, event: str, request_id: str, exc: HeartbeatError) -> None:
	getattr(frappe.logger(_LOGGER), level)(
		{"event": event, "request_id": request_id, "status": exc.status, "code": exc.code}
	)


def _safe(message: str) -> str:
	from ..posting.reasons import scrub_message

	return scrub_message(message)
