# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Heartbeat request body + client for Backend-Core ``connectorReportHeartbeat`` (spec 020).

Contract: ``services/backend-core/packages/contracts/openapi/erpnext-connector/connector-health.yaml``
(``POST /api/connector/v1/erpnext/health/heartbeat``, ``connectorBearer``). The body is
``HeartbeatReport`` with ``additionalProperties: false`` and ALL fields optional:

  - ``connectorVersion``  string, 1..64 chars;
  - ``backlogIndicator``  integer, 0..2147483647 (int4);
  - ``erpnextReachable``  boolean — the connector's own self-report;
  - ``sourceClockAt``     date-time — provenance only; Backend-Core's own clock decides liveness.

The connector identity and tenant come from the bearer on Backend-Core's side, never the body, so
this module sends no identity field and nothing outside that list. A 200 returns ``HeartbeatAck``
(``acknowledgedAt``); 400 is ``validation_error``, 401 the non-disclosing auth refusal, 500
``system_failure``.

The HTTP mechanism is the injected :class:`HttpTransport` (the auth-backed transport the pollers
use, or a fake in unit tests). Pure Python, NO frappe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

# spec-003 substrate: the request_id correlation travels on every call (RT-39).
CORRELATION_HEADER = "X-Request-Id"
HEARTBEAT_PATH = "/api/connector/v1/erpnext/health/heartbeat"

_VERSION_MAX_LEN = 64
_INT4_MAX = 2147483647


class HeartbeatError(Exception):
	"""Backend-Core did not accept the heartbeat. Carries only the HTTP status and error code."""

	def __init__(self, status: int, code: str) -> None:
		super().__init__(f"heartbeat refused: status {status}, code {code}")
		self.status = status
		self.code = code


class HeartbeatUnauthorized(HeartbeatError):
	"""401 — the connector credential is missing, invalid, expired, revoked or unlinked."""


class HeartbeatRejected(HeartbeatError):
	"""400 ``validation_error`` — the body broke the strict ``HeartbeatReport`` schema."""


class HttpTransport(Protocol):
	"""The minimal HTTP surface the client needs. ``post`` returns a status-aware response
	(``.status`` / ``.headers`` / ``.body``) or, for simple fakes, a plain body ``dict``."""

	def post(self, path: str, *, json: dict, headers: dict) -> object: ...


@dataclass(frozen=True)
class HeartbeatReport:
	"""The self-reported fields of one heartbeat. ``None`` fields are left out of the body."""

	connector_version: str | None = None
	backlog_indicator: int | None = None
	erpnext_reachable: bool | None = None
	source_clock_at: str | None = None

	def to_wire(self) -> dict:
		"""The ``HeartbeatReport`` body: only contract keys, only set fields, within bounds.

		Raises ``ValueError`` on a value Backend-Core would reject, so a bad report fails here
		instead of as a 400.
		"""
		body: dict = {}
		if self.connector_version is not None:
			version = str(self.connector_version).strip()
			if not 1 <= len(version) <= _VERSION_MAX_LEN:
				raise ValueError(f"connectorVersion must be 1..{_VERSION_MAX_LEN} characters")
			body["connectorVersion"] = version
		if self.backlog_indicator is not None:
			backlog = self.backlog_indicator
			if isinstance(backlog, bool) or not isinstance(backlog, int) or not 0 <= backlog <= _INT4_MAX:
				raise ValueError(f"backlogIndicator must be an integer in 0..{_INT4_MAX}")
			body["backlogIndicator"] = backlog
		if self.erpnext_reachable is not None:
			if not isinstance(self.erpnext_reachable, bool):
				raise ValueError("erpnextReachable must be a boolean")
			body["erpnextReachable"] = self.erpnext_reachable
		if self.source_clock_at is not None:
			body["sourceClockAt"] = self.source_clock_at
		return body


@dataclass(frozen=True)
class HeartbeatAck:
	"""Backend-Core's acknowledgement: its own server-clock time for the recorded heartbeat."""

	acknowledged_at: str | None


def format_source_clock(moment: datetime) -> str:
	"""An aware datetime as ISO-8601 UTC with milliseconds and a trailing ``Z``.

	Backend-Core validates ``sourceClockAt`` with Zod ``string().datetime()``, which accepts the
	``Z`` form and rejects a ``+00:00`` offset.
	"""
	if moment.tzinfo is None:
		raise ValueError("source clock must be timezone-aware")
	utc = moment.astimezone(timezone.utc)
	return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


class HeartbeatClient:
	"""Sends ``connectorReportHeartbeat`` over an injected :class:`HttpTransport`."""

	def __init__(self, transport: HttpTransport, *, correlation_id: str) -> None:
		self._transport = transport
		self._correlation_id = correlation_id

	def report(self, report: HeartbeatReport) -> HeartbeatAck:
		"""POST one heartbeat. 200 → :class:`HeartbeatAck`; anything else raises."""
		resp = self._transport.post(
			HEARTBEAT_PATH,
			json=report.to_wire(),
			headers={CORRELATION_HEADER: self._correlation_id},
		)
		return _interpret(resp)


def _interpret(resp: object) -> HeartbeatAck:
	if not hasattr(resp, "status"):
		# A simple transport returns a plain body dict — treat it as a 200.
		return _ack(resp)
	status = resp.status  # type: ignore[attr-defined]
	body = getattr(resp, "body", {}) or {}
	if status == 200:
		return _ack(body)
	if status == 401:
		raise HeartbeatUnauthorized(status, _error_code(body, "unauthorized"))
	if status == 400:
		raise HeartbeatRejected(status, _error_code(body, "validation_error"))
	raise HeartbeatError(status, _error_code(body, "unexpected_status"))


def _ack(body: object) -> HeartbeatAck:
	value = body.get("acknowledgedAt") if isinstance(body, dict) else None
	return HeartbeatAck(acknowledged_at=None if value is None else str(value))


def _error_code(body: object, default: str) -> str:
	"""The ``code`` of Backend-Core's ``{"error": {"code": ...}}`` envelope, else ``default``.

	Only the code is kept — never the message or the rest of the body.
	"""
	if isinstance(body, dict):
		envelope = body.get("error")
		if isinstance(envelope, dict) and envelope.get("code"):
			return str(envelope["code"])[:64]
	return default
