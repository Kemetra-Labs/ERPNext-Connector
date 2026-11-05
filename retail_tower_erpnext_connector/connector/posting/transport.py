# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Pull/ack transport for the 012 posting feed (T011).

The connector authenticates *to* DP2 (it is the client; DP2 makes no outbound calls —
spec 003 / Gate G4) and uses the fixed two-operation surface:
  - ``connectorPullPostings`` (GET, cursor-paged) — pull pending work-items;
  - ``connectorAckOutcome``   (POST) — ack a typed outcome for one work-item.

The live HTTP mechanism is abstracted behind :class:`HttpTransport` (a Protocol — the
repo's Repository pattern). The client logic is unit-tested here against a fake; the
concrete HTTP/auth-backed transport and the live round-trip are ⏳ BENCH-VALIDATION
(standing-rules §6). This module imports NO frappe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .contracts import MissingErpnextItemRef, OutcomeAckRequest, PostingWorkItem

# spec-003 substrate: the DP2 request_id correlation travels on every call (Principle V).
CORRELATION_HEADER = "X-Request-Id"
# 012 connectorAckOutcome is x-idempotency: required.
IDEMPOTENCY_HEADER = "Idempotency-Key"

# The paths DP2 actually serves (015 US1 #502 / US2 #503), verified from DP2 source
# (erpnext-posting.controller.ts @Controller("api/connector/v1/erpnext")).
_PULL_PATH = "/api/connector/v1/erpnext/postings"
_ACK_PATH_TEMPLATE = "/api/connector/v1/erpnext/postings/{work_item_ref}/outcome"

# DP2 pull `limit`: 1..500, default 100 (POSTING_FEED_MAX_PAGE). The client never requests more.
_DEFAULT_LIMIT = 100
_MAX_LIMIT = 500


class AckConflict(Exception):
	"""DP2 returned 409 idempotency_key_conflict — same key + different body, or a contradicting
	terminal outcome. Requires operator attention; the connector MUST NOT blind-retry."""


class AckNotFound(Exception):
	"""DP2 returned 404 — a cross-tenant / foreign / absent workItemRef (non-disclosing)."""


@dataclass(frozen=True)
class AckResult:
	"""Outcome of an ack POST: ``recorded`` (201/200 both succeed), ``replayed`` (200 +
	``Idempotent-Replayed: true`` — a safe duplicate), and the response ``body``."""

	recorded: bool
	replayed: bool
	body: dict


class HttpTransport(Protocol):
	"""The minimal HTTP surface the client needs. Implemented by a real auth-backed
	client in the deferred bench glue, and by a fake in unit tests.

	``post`` may return either a plain body ``dict`` (legacy/simple transports — treated as a
	201 success) or a status-aware response object exposing ``.status``, ``.headers``, ``.body``
	(so the client can distinguish 201/200-replay/409/404, per the served DP2 contract)."""

	def get(self, path: str, *, params: dict, headers: dict) -> dict: ...

	def post(self, path: str, *, json: dict, headers: dict) -> dict: ...


@dataclass(frozen=True)
class PostingFeedPage:
	"""A parsed page of the 012 posting feed (mirrors ``PostingFeedPage``)."""

	items: tuple[PostingWorkItem, ...]
	cursor: str
	next_page_token: str | None


@dataclass(frozen=True)
class InvalidItem:
	"""A raw feed item that failed to parse — isolated, not aborted (re-P2b).

	``work_item_ref`` is read directly off the raw item (so the bad item can still be acked
	by ref even though it never became a :class:`PostingWorkItem`). ``error_kind`` classifies
	the violation for the rejection reason.
	"""

	work_item_ref: str
	error_kind: str
	message: str
	raw: dict


@dataclass(frozen=True)
class RawFeedPage:
	"""A page split into successfully-parsed items and isolated invalid ones (re-P2b).

	The page is NEVER aborted on a bad line: valid items parse and post, invalid items are
	surfaced for per-item rejection, and the caller raises a page-degraded alert AFTER
	recording every per-item outcome.
	"""

	items: tuple[PostingWorkItem, ...]
	invalid: tuple[InvalidItem, ...]
	cursor: str
	next_page_token: str | None


class PostingFeedClient:
	"""Consumes the fixed 012 pull/ack surface over an injected :class:`HttpTransport`."""

	def __init__(self, transport: HttpTransport, *, correlation_id: str) -> None:
		self._transport = transport
		self._correlation_id = correlation_id

	def _headers(self, extra: dict | None = None) -> dict:
		headers = {CORRELATION_HEADER: self._correlation_id}
		if extra:
			headers.update(extra)
		return headers

	def _pull_params(self, since: str | None, limit: int) -> dict:
		"""Build the pull query: opaque `since` cursor (verbatim) + capped `limit` (≤500)."""
		params: dict = {"limit": min(max(int(limit), 1), _MAX_LIMIT)}
		if since is not None:
			params["since"] = since  # opaque numeric string — send verbatim, never int-parse
		return params

	def pull_postings(self, *, since: str | None, limit: int = _DEFAULT_LIMIT) -> PostingFeedPage:
		"""``connectorPullPostings`` — pull a cursor-ordered page of pending work-items.

		An empty page is a valid, non-error result (no pending postings).
		"""
		raw = self._transport.get(_PULL_PATH, params=self._pull_params(since, limit), headers=self._headers())
		items = tuple(PostingWorkItem.from_wire(item) for item in raw.get("items", []))
		# 012 PostingFeedPage.cursor is required, minLength 1. An absent/empty cursor is an
		# upstream contract violation — raise rather than substitute "" (which would silently
		# re-baseline the next pull instead of advancing). F-011.
		cursor = raw.get("cursor")
		if not cursor:
			raise ValueError("posting feed page missing required non-empty cursor (012)")
		return PostingFeedPage(
			items=items,
			cursor=str(cursor),
			next_page_token=(None if raw.get("next_page_token") is None else str(raw["next_page_token"])),
		)

	def pull_postings_raw(self, *, since: str | None, limit: int = _DEFAULT_LIMIT) -> RawFeedPage:
		"""``connectorPullPostings`` — pull a page, isolating unparseable items (re-P2b).

		Unlike :meth:`pull_postings` (which aborts the whole page on a bad line), this parses
		each item independently: a line missing ``erpnextItemRef`` (or any parse failure) becomes
		an :class:`InvalidItem` rather than aborting valid siblings. The caller acks each invalid
		item ``permanently_rejected``/``validation`` and raises a page-degraded alert AFTER.
		"""
		raw = self._transport.get(_PULL_PATH, params=self._pull_params(since, limit), headers=self._headers())

		items: list[PostingWorkItem] = []
		invalid: list[InvalidItem] = []
		for entry in raw.get("items", []):
			try:
				items.append(PostingWorkItem.from_wire(entry))
			except MissingErpnextItemRef as exc:
				invalid.append(
					InvalidItem(
						work_item_ref=str(entry.get("workItemRef", "")),
						error_kind="missing_erpnext_item_ref",
						message=str(exc),
						raw=entry,
					)
				)
			except (ValueError, KeyError, TypeError) as exc:
				# Any other malformed item is also isolated, not page-aborting (re-P2b).
				invalid.append(
					InvalidItem(
						work_item_ref=str(entry.get("workItemRef", "")),
						error_kind="malformed_work_item",
						message=str(exc),
						raw=entry,
					)
				)

		cursor = raw.get("cursor")
		if not cursor:
			raise ValueError("posting feed page missing required non-empty cursor (012)")
		return RawFeedPage(
			items=tuple(items),
			invalid=tuple(invalid),
			cursor=str(cursor),
			next_page_token=(None if raw.get("next_page_token") is None else str(raw["next_page_token"])),
		)

	def ack_outcome(
		self,
		work_item_ref: str,
		ack: OutcomeAckRequest,
		*,
		idempotency_key: str | None = None,
	) -> AckResult:
		"""``connectorAckOutcome`` — POST the typed outcome for one work-item.

		Posts ONLY the outcome; never mutates the DP2 sale fact (012 O-3 / Principle IV).
		Interprets the served status: 201 (first record) and 200 (+ ``Idempotent-Replayed: true``)
		are both success; 409 → :class:`AckConflict` (operator attention, no blind retry);
		404 → :class:`AckNotFound` (non-disclosing).
		"""
		extra = {IDEMPOTENCY_HEADER: idempotency_key} if idempotency_key else None
		resp = self._transport.post(
			_ACK_PATH_TEMPLATE.format(work_item_ref=work_item_ref),
			json=ack.to_wire(),
			headers=self._headers(extra),
		)
		return self._interpret_ack(resp)

	@staticmethod
	def _interpret_ack(resp: object) -> AckResult:
		# A legacy/simple transport returns a plain body dict — treat as a 201 success.
		if not hasattr(resp, "status"):
			return AckResult(recorded=True, replayed=False, body=dict(resp or {}))  # type: ignore[arg-type]
		status = resp.status  # type: ignore[attr-defined]
		headers = getattr(resp, "headers", {}) or {}
		body = getattr(resp, "body", {}) or {}
		if status == 409:
			raise AckConflict(str(body.get("code", "idempotency_key_conflict")))
		if status == 404:
			raise AckNotFound(str(body.get("code", "not_found")))
		if status in (200, 201):
			replayed = str(headers.get("Idempotent-Replayed", "")).lower() == "true"
			return AckResult(recorded=True, replayed=replayed, body=body)
		raise RuntimeError(f"unexpected ack status {status}: {body!r}")
