# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Pull/report transport for the 019 stock-view (Bin) feed.

The connector authenticates *to* DP2 (it is the client; DP2 makes no outbound calls —
Principle IX) over the fixed two-operation surface:
  - ``binViewPullRequests``  (GET, cursor-paged) — pull wanted Bin-view reads;
  - ``binViewReportSnapshot`` (POST) — report the point-in-time Bin snapshot for one
    pulled ``requestRef`` (``Idempotency-Key`` REQUIRED).

The live HTTP mechanism is abstracted behind :class:`HttpTransport` (the repo's
Protocol/Repository pattern). The client logic is unit-tested here against a fake; the
concrete HTTP/auth-backed transport + the live round-trip are bench-validated. NO frappe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .contracts import BinViewRequest, BinViewSnapshotReport, RecordedBinView

# spec-003 substrate: DP2 request_id correlation on every call (Principle V).
CORRELATION_HEADER = "X-Request-Id"
# 019 binViewReportSnapshot is x-idempotency: required.
IDEMPOTENCY_HEADER = "Idempotency-Key"

# Paths DP2 serves (019 T040), verified from DP2 source
# (erpnext-bin-view.controller.ts @Controller("api/connector/v1/erpnext")).
_PULL_PATH = "/api/connector/v1/erpnext/bin-view-requests"
_REPORT_PATH_TEMPLATE = "/api/connector/v1/erpnext/bin-view-requests/{request_ref}/snapshot"

# DP2 pull `limit`: 1..500, default 100. The client never requests more.
_DEFAULT_LIMIT = 100
_MAX_LIMIT = 500


class ReportConflict(Exception):
	"""DP2 returned 409 idempotency_key_conflict — same key + a different snapshot.
	Requires operator attention; the connector MUST NOT blind-retry."""


class WindowSequenceConflict(ReportConflict):
	"""DP2 returned 409 ``window_sequence_conflict`` (stock-view 1.2) — a report window that
	does not fit the recorded attempt: a gap / out-of-order ``windowSeq``, a window after the
	final one, a stale or superseded ``attemptRef`` (incl. ANY other attempt once one is
	complete), a duplicate item across windows, or a ``readAt`` mismatch. Deterministic, no
	side effects. ``window_seq`` is the refused window's sequence (0 for a v1 report).

	A subclass of :class:`ReportConflict` so a catch-all conflict handler still sees it; the
	bin-view poller catches it FIRST because, unlike a key conflict, it is recoverable."""

	def __init__(self, code: str, *, window_seq: int) -> None:
		super().__init__(code)
		self.window_seq = window_seq


_WINDOW_SEQUENCE_CONFLICT = "window_sequence_conflict"


class ReportNotFound(Exception):
	"""DP2 returned 404 — a cross-tenant / foreign / stale-run requestRef (non-disclosing)."""


class SnapshotRequired(Exception):
	"""DP2 returned 409 snapshot_required on the pull — the `since` cursor is stale.
	The connector MUST re-baseline (pull from the start)."""


class HttpTransport(Protocol):
	"""The minimal HTTP surface the client needs (real auth-backed impl in the bench glue,
	a fake in unit tests). ``get`` returns a body dict or raises on stale-cursor 409;
	``post`` returns a status-aware response (``.status``/``.headers``/``.body``)."""

	def get(self, path: str, *, params: dict, headers: dict) -> dict: ...

	def post(self, path: str, *, json: dict, headers: dict) -> object: ...


@dataclass(frozen=True)
class BinViewPage:
	"""A parsed page of the 019 feed (mirrors ``BinViewPage``)."""

	items: tuple[BinViewRequest, ...]
	cursor: str
	next_page_token: str | None


@dataclass(frozen=True)
class ReportResult:
	"""Outcome of a report POST: ``recorded`` (201/200 both succeed), ``replayed`` (200 +
	``Idempotent-Replayed: true``), and the response ``body``."""

	recorded: bool
	replayed: bool
	body: dict

	@property
	def recorded_view(self) -> RecordedBinView:
		"""The tolerant parse of the response body (incl. the v1.2 progress fields)."""
		return RecordedBinView.from_wire(self.body)


class BinViewClient:
	"""Consumes the fixed 019 pull/report surface over an injected :class:`HttpTransport`."""

	def __init__(self, transport: HttpTransport, *, correlation_id: str) -> None:
		self._transport = transport
		self._correlation_id = correlation_id

	def _headers(self, extra: dict | None = None) -> dict:
		headers = {CORRELATION_HEADER: self._correlation_id}
		if extra:
			headers.update(extra)
		return headers

	def _pull_params(self, since: str | None, limit: int) -> dict:
		params: dict = {"limit": min(max(int(limit), 1), _MAX_LIMIT)}
		if since is not None:
			params["since"] = since  # opaque cursor — send verbatim, never decode
		return params

	def pull_requests(self, *, since: str | None, limit: int = _DEFAULT_LIMIT) -> BinViewPage:
		"""``binViewPullRequests`` — pull a cursor-ordered page of wanted Bin-view reads.

		An empty page is a valid, non-error result (no running runs awaiting a Bin read).
		"""
		raw = self._transport.get(_PULL_PATH, params=self._pull_params(since, limit), headers=self._headers())
		items = tuple(BinViewRequest.from_wire(item) for item in raw.get("items", []))
		# 019 BinViewPage.cursor is required, minLength 1. An absent/empty cursor is an
		# upstream contract violation — raise rather than substitute "" (which would
		# silently re-baseline the next pull instead of advancing).
		cursor = raw.get("cursor")
		if not cursor:
			raise ValueError("bin-view feed page missing required non-empty cursor (019)")
		return BinViewPage(
			items=items,
			cursor=str(cursor),
			next_page_token=(None if raw.get("next_page_token") is None else str(raw["next_page_token"])),
		)

	def report_snapshot(
		self,
		request_ref: str,
		report: BinViewSnapshotReport,
		*,
		idempotency_key: str,
	) -> ReportResult:
		"""``binViewReportSnapshot`` — POST the point-in-time Bin snapshot for one request.

		``Idempotency-Key`` is REQUIRED (019 x-idempotency: required). Interprets the served
		status: 201 (first record) + 200 (+ ``Idempotent-Replayed: true``) are both success;
		409 ``window_sequence_conflict`` → :class:`WindowSequenceConflict` (v1.2 multi-window);
		any other 409 → :class:`ReportConflict` (operator attention, no blind retry);
		404 → :class:`ReportNotFound` (non-disclosing — stale run / cross-tenant).
		"""
		resp = self._transport.post(
			_REPORT_PATH_TEMPLATE.format(request_ref=request_ref),
			json=report.to_wire(),
			headers=self._headers({IDEMPOTENCY_HEADER: idempotency_key}),
		)
		window_seq = 0 if report.window is None else report.window.window_seq
		return self._interpret_report(resp, window_seq=window_seq)

	@staticmethod
	def _interpret_report(resp: object, *, window_seq: int = 0) -> ReportResult:
		# A legacy/simple transport returns a plain body dict — treat as a 201 success.
		if not hasattr(resp, "status"):
			return ReportResult(recorded=True, replayed=False, body=dict(resp or {}))  # type: ignore[arg-type]
		status = resp.status  # type: ignore[attr-defined]
		headers = getattr(resp, "headers", {}) or {}
		body = getattr(resp, "body", {}) or {}
		if status == 409:
			code = _error_code(body, "idempotency_key_conflict")
			if code == _WINDOW_SEQUENCE_CONFLICT:
				raise WindowSequenceConflict(code, window_seq=window_seq)
			raise ReportConflict(code)
		if status == 404:
			raise ReportNotFound(_error_code(body, "not_found"))
		if status in (200, 201):
			replayed = str(headers.get("Idempotent-Replayed", "")).lower() == "true"
			return ReportResult(recorded=True, replayed=replayed, body=body)
		raise RuntimeError(f"unexpected report status {status}: {body!r}")


def _error_code(body: object, default: str) -> str:
	"""The error ``code`` from DP2's canonical envelope ``{"error": {"code": ...}}`` (or a flat
	``{"code": ...}`` body, as older fakes/transports return); ``default`` when neither is set."""
	if isinstance(body, dict):
		envelope = body.get("error")
		if isinstance(envelope, dict) and envelope.get("code"):
			return str(envelope["code"])
		if body.get("code"):
			return str(body["code"])
	return default
