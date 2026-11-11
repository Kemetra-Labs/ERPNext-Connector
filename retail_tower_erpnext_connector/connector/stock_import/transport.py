# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Pull/report transport for Backend-Core's ERP-origin stock import feed (``stock-import.yaml``).

The connector authenticates *to* Backend-Core (it is the client; Backend-Core makes no outbound
call) over the two-operation surface:
  - ``stockImportPullRequests`` (GET, cursor-paged) — pull wanted Stock Ledger reads;
  - ``stockImportReportRows`` (POST) — report the facts read for one pulled ``requestRef``
    (``Idempotency-Key`` REQUIRED, ``stockimport-{requestRef}``).

The HTTP mechanism is the shared :class:`~..http_transport.HttpTransport`. NO frappe.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..http_transport import HttpTransport
from ..text import error_code
from .contracts import RecordedStockImport, StockImportRequest

CORRELATION_HEADER = "X-Request-Id"
IDEMPOTENCY_HEADER = "Idempotency-Key"

_PULL_PATH = "/api/connector/v1/erpnext/stock-import-requests"
_REPORT_PATH_TEMPLATE = "/api/connector/v1/erpnext/stock-import-requests/{request_ref}/report"

_DEFAULT_LIMIT = 100
_MAX_LIMIT = 500

_SNAPSHOT_REQUIRED = "snapshot_required"


def idempotency_key(request_ref: str) -> str:
	"""The contract's connector key for one request's report (48 characters for a UUID ref)."""
	return f"stockimport-{request_ref}"


def _is_snapshot_required(response: object) -> bool:
	"""True for a 409 HTTP response whose error envelope code is ``snapshot_required``."""
	if getattr(response, "status_code", None) != 409:
		return False
	try:
		body = response.json()  # type: ignore[union-attr]
	except Exception:
		return False
	return error_code(body, "", flat=False, limit=None) == _SNAPSHOT_REQUIRED


class SnapshotRequired(Exception):
	"""409 ``snapshot_required`` on the pull: the ``since`` cursor is stale; re-baseline."""


class ReportConflict(Exception):
	"""409 on the report: ``idempotency_key_conflict`` or ``import_report_conflict``.

	Deterministic; the request already has a recorded report (or the key was reused with a
	different body). Never blind-retried. ``code`` is the served error code.
	"""

	def __init__(self, code: str) -> None:
		super().__init__(code)
		self.code = code


class ReportNotFound(Exception):
	"""404 on the report: a stale, closed or out-of-scope ``requestRef`` (non-disclosing)."""


class ReportRejected(Exception):
	"""400 ``validation_error`` (or 413) on the report: Backend-Core refused this body.

	Deterministic for this body, so resending it is pointless; the request stays on the feed.
	"""


@dataclass(frozen=True)
class StockImportPage:
	"""A parsed page of the feed (mirrors ``StockImportPage``)."""

	items: tuple[StockImportRequest, ...]
	cursor: str
	next_page_token: str | None
	# Feed items that did not parse, as (requestRef or None, error): one bad item never fails the page.
	invalid_items: tuple[tuple[str | None, str], ...] = ()


@dataclass(frozen=True)
class ReportResult:
	"""A recorded report: ``replayed`` on a 200 ``Idempotent-Replayed: true``."""

	replayed: bool
	recorded: RecordedStockImport


class StockImportClient:
	"""Consumes the ``stock-import.yaml`` pull/report surface over an injected transport."""

	def __init__(self, transport: HttpTransport, *, correlation_id: str) -> None:
		self._transport = transport
		self._correlation_id = correlation_id

	def _headers(self, extra: dict | None = None) -> dict:
		headers = {CORRELATION_HEADER: self._correlation_id}
		if extra:
			headers.update(extra)
		return headers

	def pull_requests(self, *, since: str | None, limit: int = _DEFAULT_LIMIT) -> StockImportPage:
		"""``stockImportPullRequests``. An empty page is a valid result (no open run)."""
		params: dict = {"limit": min(max(int(limit), 1), _MAX_LIMIT)}
		if since is not None:
			params["since"] = since  # opaque cursor — sent verbatim, never decoded
		try:
			raw = self._transport.get(_PULL_PATH, params=params, headers=self._headers())
		except Exception as exc:
			# The shared transport raises on an error status (``raise_for_status``).
			if _is_snapshot_required(getattr(exc, "response", None)):
				raise SnapshotRequired(_SNAPSHOT_REQUIRED) from None
			raise
		cursor = raw.get("cursor")
		if not cursor:
			raise ValueError("stock-import feed page missing required non-empty cursor")
		items, invalid = [], []
		for item in raw.get("items", []):
			try:
				items.append(StockImportRequest.from_wire(item))
			except Exception as exc:
				ref = item.get("requestRef") if isinstance(item, dict) else None
				invalid.append((None if ref is None else str(ref), f"{type(exc).__name__}: {exc}"))
		return StockImportPage(
			items=tuple(items),
			cursor=str(cursor),
			next_page_token=None if raw.get("next_page_token") is None else str(raw["next_page_token"]),
			invalid_items=tuple(invalid),
		)

	def report_rows(self, request_ref: str, body: dict) -> ReportResult:
		"""``stockImportReportRows`` — POST one report body (already rendered to wire).

		The caller passes the stored wire body so a retry resends it byte-for-byte (the
		contract's retry rule). 201 and 200 succeed; 409 → :class:`ReportConflict`;
		404 → :class:`ReportNotFound`; 400/413 → :class:`ReportRejected`; anything else raises
		``RuntimeError`` (retryable).
		"""
		resp = self._transport.post(
			_REPORT_PATH_TEMPLATE.format(request_ref=request_ref),
			json=body,
			headers=self._headers({IDEMPOTENCY_HEADER: idempotency_key(request_ref)}),
		)
		if not hasattr(resp, "status"):
			return ReportResult(replayed=False, recorded=RecordedStockImport.from_wire(resp))  # type: ignore[arg-type]
		status = resp.status  # type: ignore[attr-defined]
		headers = getattr(resp, "headers", {}) or {}
		rbody = getattr(resp, "body", {}) or {}
		if status in (200, 201):
			replayed = str(headers.get("Idempotent-Replayed", "")).lower() == "true"
			return ReportResult(replayed=replayed, recorded=RecordedStockImport.from_wire(rbody))
		if status == 409:
			raise ReportConflict(error_code(rbody, "idempotency_key_conflict", flat=True, limit=None))
		if status == 404:
			raise ReportNotFound(error_code(rbody, "not_found", flat=True, limit=None))
		if status in (400, 413):
			raise ReportRejected(error_code(rbody, "validation_error", flat=True, limit=None))
		raise RuntimeError(f"unexpected stock-import report status {status}")
