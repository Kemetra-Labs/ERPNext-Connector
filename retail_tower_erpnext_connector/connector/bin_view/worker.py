# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Bin-view worker — pure read-and-report logic for one pulled request.

For each :class:`~.transport.BinViewRequest`, the worker reads the live ERPNext ``Bin``
on-hand per Item for the request's warehouse (via an injected :class:`BinReader` — the
frappe-touching part is the bench glue), builds an exact-decimal
:class:`~.contracts.BinViewSnapshotReport`, and reports it back to DP2 with a
deterministic ``Idempotency-Key``.

Two paths, chosen by the pulled request (stock-view 1.2, RT-176):

* v1 — ``itemWindow.maxWindows`` absent or 1: one report with no ``window`` object, key
  ``binview-{requestRef}``, and a loud refusal (:class:`WindowOverflowError`) above
  ``maxItems`` items. Unchanged from 019 v1.
* paged — ``maxWindows > 1``: ONE read attempt (keyset pages, see
  :meth:`BinReader.read_bins_paged`) split into windows 0..N of at most ``maxItems``
  entries, all sharing one fresh ``attemptRef`` and one ``readAt``; reported in order
  with ``isFinal`` on window N only, each keyed
  ``binview-{requestRef}-{attemptRef}-w{seq}``. Above ``maxWindows x maxItems`` items
  :class:`WindowLimitExceeded` is raised and NOTHING is reported. A failure mid-attempt
  propagates; the poller retries later with a FRESH attempt from window 0 (DP2
  supersedes the incomplete one). An optional :class:`AttemptDeadline` ends the attempt
  cleanly BEFORE a window call that could overrun the attempt's time budget
  (:class:`AttemptDeadlineExceeded`, no ``isFinal`` sent), so the worker running it is
  never killed mid-write.

§III: ERPNext ``Bin.actual_qty`` is a FLOAT; the connector converts it to an
exact-decimal STRING deterministically (round HALF-EVEN to 6 fractional digits — the
019 contract precision cap) so DP2 never receives a float. NO valuation is read.

RT-918 (stock-view 1.4): when the poller passes a ``tracking_reader`` (the Connector's
tracking release switch is on), EVERY entry carries ``tracking``: the ERPNext Item flags and,
for a batch Item, its Batches in the warehouse with their book quantity, expiry and disabled
flag. The tracking of the whole read is read BEFORE any window is reported, so a refusal
(:class:`BatchLimitExceeded`, :class:`TrackingReadError`) reports nothing. Without a
``tracking_reader`` the report is byte-for-byte the pre-1.4 shape.

This module imports NO frappe — the Bin read is behind :class:`BinReader`; tested
locally against a fake. The live composition root is :mod:`.poller` (bench-validated).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Protocol

from .contracts import (
	MAX_BATCHES_PER_ENTRY,
	BinEntry,
	BinEntryBatch,
	BinEntryTracking,
	BinViewReportWindow,
	BinViewRequest,
	BinViewSnapshotReport,
)
from .transport import BinViewClient, ReportResult

# 019 BinEntry.quantity precision cap — at most 6 fractional digits.
_QTY_QUANTUM = Decimal("0.000001")

# Paged-attempt time budget (Codex P1 on #56). One report call is bounded by the DP2 transport
# timeout (``http_transport.RequestsTransport``: 30 s); the keyset Bin read gets a fixed
# allowance. A paged attempt's budget is therefore ``maxWindows x 30 s + 60 s`` — enough for
# every window even at the worst-case call time — capped so the long-queue job that runs it
# (budget + margin, see ``poller``) stays under Frappe v15/v16's 1500 s long-queue timeout.
TRANSPORT_TIMEOUT_S = 30
READ_ALLOWANCE_S = 60
MAX_ATTEMPT_BUDGET_S = 1200


class WindowOverflowError(Exception):
	"""The warehouse holds MORE Bin items than this request's window can carry.

	v1 issues a single ≤500-item window per warehouse (DP2 does not yet split a
	>500-item warehouse into multiple windows — that needs DP2 to enumerate the
	ERPNext item space it cannot see; tracked as a follow-up). Rather than report a
	KNOWN-INCOMPLETE snapshot as if complete (which would corrupt the 017 run —
	items beyond the window read as `dp2_only`/absent), the connector REFUSES and
	surfaces this loudly. No silent truncation (CodeRabbit #528 P1).

	RT-176: still raised on the v1 path only (a request without ``maxWindows > 1``)."""


class WindowLimitExceeded(Exception):
	"""A connector-paged request's warehouse holds MORE than ``maxWindows x maxItems`` Bin
	items (stock-view 1.2). The paged equivalent of :class:`WindowOverflowError`: nothing is
	reported (no known-incomplete snapshot), the poller logs
	``bin_view.window.limit_exceeded``."""

	def __init__(self, message: str, *, limit: int) -> None:
		super().__init__(message)
		self.limit = limit


class AttemptDeadlineExceeded(Exception):
	"""A paged attempt stopped BEFORE window ``window_seq`` because that call could overrun the
	attempt's time budget. Windows ``0..window_seq-1`` were sent; no ``isFinal`` was. The attempt
	is incomplete (DP2 supersedes it on the next attempt) and is retried within the bound."""

	def __init__(self, message: str, *, window_seq: int) -> None:
		super().__init__(message)
		self.window_seq = window_seq


class AttemptDeadline:
	"""Deterministic elapsed-time budget for one paged attempt (clock injectable for tests).

	Started when constructed. :meth:`check` is called before each window call and raises
	:class:`AttemptDeadlineExceeded` when ``elapsed + per_call_s`` would pass ``budget_s``.
	"""

	def __init__(
		self,
		budget_s: float,
		*,
		per_call_s: float = TRANSPORT_TIMEOUT_S,
		monotonic: Callable[[], float] = time.monotonic,
	) -> None:
		self._budget_s = float(budget_s)
		self._per_call_s = float(per_call_s)
		self._monotonic = monotonic
		self._started = monotonic()

	def allows_call(self) -> bool:
		"""True while one more call (``per_call_s``) still fits the budget."""
		return self._monotonic() - self._started + self._per_call_s <= self._budget_s

	def check(self, window_seq: int) -> None:
		if not self.allows_call():
			elapsed = self._monotonic() - self._started
			raise AttemptDeadlineExceeded(
				f"stopping before window {window_seq}: {elapsed:.0f}s elapsed + up to "
				f"{self._per_call_s:.0f}s per call exceeds the {self._budget_s:.0f}s attempt budget",
				window_seq=window_seq,
			)


def attempt_budget_s(max_windows: int) -> int:
	"""The time budget for one paged attempt of a ``maxWindows`` request (see constants above)."""
	return min(int(max_windows) * TRANSPORT_TIMEOUT_S + READ_ALLOWANCE_S, MAX_ATTEMPT_BUDGET_S)


class BatchLimitExceeded(Exception):
	"""An Item holds MORE than 200 Batches in the warehouse (stock-view 1.4
	``BinEntryTracking.batches`` maxItems). Reporting a truncated list would be known-incomplete
	evidence and more is a 400 at Backend-Core, so the read is refused and NOTHING is reported
	(the poller logs ``bin_view.tracking.batch_limit_exceeded``; deterministic, not retried)."""

	def __init__(self, message: str, *, item_code: str, batch_count: int) -> None:
		super().__init__(message)
		self.item_code = item_code
		self.batch_count = batch_count


class TrackingReadError(Exception):
	"""The tracking read could not produce evidence for an Item (its Item flags or a Batch master
	row is missing). The Connector never guesses "untracked": nothing is reported and the attempt
	is retried like any read failure."""


class ReportIncomplete(Exception):
	"""The final window of an attempt was acknowledged, but DP2 explicitly said the report is
	NOT complete (``complete: false``). The run has not reached completion, so the poller
	retries it with a fresh attempt (bounded). An absent ``complete`` (v1 / pre-1.2 DP2) is
	never treated as incomplete."""


@dataclass(frozen=True)
class RawBin:
	"""One ERPNext ``Bin`` row as read from the warehouse (item_code + on-hand + uom)."""

	item_code: str
	actual_qty: float
	stock_uom: str


@dataclass(frozen=True)
class RawItemTracking:
	"""One ERPNext Item's tracking flags (``has_batch_no``, ``has_serial_no``, ``has_expiry_date``)."""

	item_code: str
	has_batch_no: bool
	has_serial_no: bool
	has_expiry_date: bool


@dataclass(frozen=True)
class RawBatch:
	"""One Batch of a batch Item with its summed book quantity in the warehouse + its master row.

	``qty`` is the ERPNext float sum (quantized by :func:`quantize_qty`, never sent as a float);
	``expiry_date`` is an ISO calendar date or ``None``.
	"""

	item_code: str
	batch_no: str
	qty: float
	expiry_date: str | None
	disabled: bool


@dataclass(frozen=True)
class RawTrackingRead:
	"""What one tracking read returned for a set of Items in one warehouse."""

	items: tuple[RawItemTracking, ...]
	batches: tuple[RawBatch, ...]


class TrackingReader(Protocol):
	"""Reads the ERPNext tracking master + batch-wise book quantity (the frappe-touching seam)."""

	def read_tracking(self, *, erpnext_warehouse_ref: str, item_codes: list[str]) -> RawTrackingRead:
		"""The Item flags of ``item_codes`` and, for the batch Items among them, every Batch that has
		stock-ledger presence in the warehouse with its summed book quantity and master row."""
		...


class BinReader(Protocol):
	"""Reads the live ERPNext ``Bin`` rows for a warehouse (the frappe-touching seam)."""

	def read_bins(self, *, erpnext_warehouse_ref: str, item_window) -> list[RawBin]: ...

	def read_bins_paged(self, *, erpnext_warehouse_ref: str, page_size: int, max_rows: int) -> list[RawBin]:
		"""ONE read attempt over the warehouse's Bin rows in ``item_code`` order, fetched as
		keyset pages of ``page_size`` rows, returning at most ``max_rows`` rows (the caller
		asks for its limit + 1 so an over-limit warehouse is detectable)."""
		...


class Clock(Protocol):
	"""Supplies the connector ``readAt`` timestamp (ISO-8601 UTC)."""

	def now_iso(self) -> str: ...


def quantize_qty(actual_qty: float) -> str:
	"""Convert an ERPNext float ``actual_qty`` to an exact-decimal STRING (§III).

	Round HALF-EVEN to 6 fractional digits (the 019 quantity precision cap). The
	Decimal is built from ``str(actual_qty)`` (not the float directly) so the input's
	decimal repr is honored rather than binary-float drift. Negative is preserved
	(ERPNext may carry negative on-hand).
	"""
	d = Decimal(str(actual_qty)).quantize(_QTY_QUANTUM, rounding=ROUND_HALF_EVEN)
	# Normalize "-0.000000" → "0.000000" (sign of zero is not meaningful).
	if d == 0:
		d = abs(d)
	return f"{d:f}"


def is_zero_qty(qty: float) -> bool:
	"""True when ``qty`` is zero at the contract precision (6 fractional digits, half-even)."""
	return Decimal(quantize_qty(qty)) == 0


def build_report(
	bins: list[RawBin],
	*,
	read_at: str,
	window: BinViewReportWindow | None = None,
	tracking: dict[str, BinEntryTracking] | None = None,
) -> BinViewSnapshotReport:
	"""Build the snapshot report (or one window of it) from raw Bin rows — pure, no valuation.

	``tracking`` (RT-918) maps every ``item_code`` of ``bins`` to its evidence; ``None`` builds the
	pre-1.4 shape (no ``tracking`` on any entry). A missing Item in a given map is a bug, not
	"untracked" — :class:`TrackingReadError`.
	"""

	def _tracking_of(item_code: str) -> BinEntryTracking | None:
		if tracking is None:
			return None
		if item_code not in tracking:
			raise TrackingReadError(f"no tracking evidence was built for Item {item_code!r}")
		return tracking[item_code]

	entries = tuple(
		BinEntry(
			erpnext_item_ref=b.item_code,
			quantity=quantize_qty(b.actual_qty),
			stock_uom=b.stock_uom,
			tracking=_tracking_of(b.item_code),
		)
		for b in bins
	)
	return BinViewSnapshotReport(entries=entries, read_at=read_at, window=window)


def build_tracking(item_codes: list[str], read: RawTrackingRead) -> dict[str, BinEntryTracking]:
	"""Turn one tracking read into per-Item ``BinEntryTracking`` evidence — pure, no valuation.

	* Every Item of ``item_codes`` must have its flags in ``read`` (else :class:`TrackingReadError`).
	* Only a ``has_batch_no`` Item lists Batches; a Batch row of another Item is ignored.
	* A Batch whose book quantity quantizes to zero is omitted (captain decision, RT-918; RT-1283
	  comment 16225): a used-up Batch has no presence in the warehouse in the 1.4 sense, and keeping
	  every lifetime Batch would push a fast-batched Item over the 200 cap. Negative stays listed.
	* Batches are listed once each, ordered by Batch name, so a re-read of the same ledger gives the
	  same body (the report's idempotency fingerprint).
	* More than :data:`MAX_BATCHES_PER_ENTRY` Batches for one Item → :class:`BatchLimitExceeded`.
	"""
	flags = {i.item_code: i for i in read.items}
	missing = [code for code in item_codes if code not in flags]
	if missing:
		raise TrackingReadError(
			f"ERPNext returned no Item tracking flags for {len(missing)} Item(s), e.g. {missing[0]!r}"
		)
	batches_by_item: dict[str, dict[str, RawBatch]] = {}
	for batch in read.batches:
		if is_zero_qty(batch.qty):
			continue
		if batch.item_code in flags and flags[batch.item_code].has_batch_no:
			batches_by_item.setdefault(batch.item_code, {})[batch.batch_no] = batch
	return {code: _entry_tracking(flags[code], batches_by_item.get(code, {})) for code in item_codes}


def _entry_tracking(item: RawItemTracking, batches: dict[str, RawBatch]) -> BinEntryTracking:
	if len(batches) > MAX_BATCHES_PER_ENTRY:
		raise BatchLimitExceeded(
			f"Item {item.item_code!r} has {len(batches)} Batches in the warehouse; stock-view 1.4 "
			f"carries at most {MAX_BATCHES_PER_ENTRY} per entry, so the read is refused",
			item_code=item.item_code,
			batch_count=len(batches),
		)
	return BinEntryTracking(
		has_batch_no=item.has_batch_no,
		has_serial_no=item.has_serial_no,
		has_expiry_date=item.has_expiry_date,
		batches=tuple(
			BinEntryBatch(
				batch_ref=b.batch_no,
				quantity=quantize_qty(b.qty),
				expiry_date=b.expiry_date,
				disabled=b.disabled,
			)
			for _name, b in sorted(batches.items())
		),
	)


def read_tracking(
	reader: TrackingReader, *, erpnext_warehouse_ref: str, bins: list[RawBin], chunk_size: int
) -> dict[str, BinEntryTracking]:
	"""Read and build the tracking evidence for every Item of ``bins``, ``chunk_size`` Items per read
	(one report window's worth, so each ERPNext query stays bounded)."""
	codes = [b.item_code for b in bins]
	tracking: dict[str, BinEntryTracking] = {}
	step = max(int(chunk_size), 1)
	for i in range(0, len(codes), step):
		chunk = codes[i : i + step]
		read = reader.read_tracking(erpnext_warehouse_ref=erpnext_warehouse_ref, item_codes=chunk)
		tracking.update(build_tracking(chunk, read))
	return tracking


def derive_idempotency_key(request_ref: str) -> str:
	"""Deterministic Idempotency-Key for a request's report.

	Keyed on the ``requestRef`` so a re-report of the SAME request reuses the SAME key
	(DP2 replays idempotently). A different request → a different key.
	"""
	return f"binview-{request_ref}"


def derive_window_idempotency_key(request_ref: str, attempt_ref: str, window_seq: int) -> str:
	"""Idempotency-Key for ONE window of a connector-paged report (stock-view 1.2).

	DP2 fingerprints the whole body, so a resend of the same window replays and anything
	different under the same key conflicts; a fresh attempt therefore needs fresh keys,
	which the ``attemptRef`` component gives it.
	"""
	return f"binview-{request_ref}-{attempt_ref}-w{window_seq}"


def split_windows(bins: list[RawBin], max_items: int) -> list[list[RawBin]]:
	"""Split one attempt's rows into consecutive windows of at most ``max_items`` rows.

	Every window is non-empty except the single window of an empty warehouse, which is the
	one shape the contract allows with zero entries (``{windowSeq 0, isFinal true}``).
	"""
	if max_items < 1:
		raise ValueError(f"maxItems must be >= 1, got {max_items}")
	if not bins:
		return [[]]
	return [bins[i : i + max_items] for i in range(0, len(bins), max_items)]


def new_attempt_ref() -> str:
	"""A fresh ``attemptRef`` (UUIDv4, canonical lower-case form) for one read attempt."""
	return str(uuid.uuid4())


def process_request(
	request: BinViewRequest,
	*,
	client: BinViewClient,
	reader: BinReader,
	clock: Clock,
	attempt_ref_factory: Callable[[], str] = new_attempt_ref,
	deadline: AttemptDeadline | None = None,
	tracking_reader: TrackingReader | None = None,
) -> ReportResult:
	"""Read the warehouse's Bin for one request and report it to DP2 (v1 or paged).

	Dispatches on ``itemWindow.maxWindows``: ``> 1`` → :func:`process_paged_request` (with the
	optional ``deadline``); absent or 1 → the unchanged v1 single-window path
	(:func:`process_v1_request`). ``tracking_reader`` set → every entry carries 1.4 ``tracking``.
	"""
	if request.item_window.is_paged:
		return process_paged_request(
			request,
			client=client,
			reader=reader,
			clock=clock,
			attempt_ref_factory=attempt_ref_factory,
			deadline=deadline,
			tracking_reader=tracking_reader,
		)
	return process_v1_request(
		request, client=client, reader=reader, clock=clock, tracking_reader=tracking_reader
	)


def process_v1_request(
	request: BinViewRequest,
	*,
	client: BinViewClient,
	reader: BinReader,
	clock: Clock,
	tracking_reader: TrackingReader | None = None,
) -> ReportResult:
	"""v1: read the warehouse's Bin for one request and report the snapshot to DP2.

	Reads on-hand QUANTITY only (no valuation, 019 / Principle IX). The
	``Idempotency-Key`` is derived from the requestRef so a retry is a safe replay.

	Raises :class:`WindowOverflowError` if the warehouse holds MORE items than the
	request's window can carry — refusing to report a known-incomplete snapshot as
	complete (no silent truncation; the poller surfaces this as an alert).
	"""
	read_at = clock.now_iso()
	bins = reader.read_bins(
		erpnext_warehouse_ref=request.erpnext_warehouse_ref,
		item_window=request.item_window,
	)
	# LOUD overflow guard: the reader returns up to max_items+1 so >max_items is
	# detectable. A full-or-over read means the single v1 window did not cover the
	# whole warehouse → refuse rather than silently drop the tail.
	max_items = int(request.item_window.max_items)
	if len(bins) > max_items:
		raise WindowOverflowError(
			f"warehouse {request.erpnext_warehouse_ref!r} has more than {max_items} "
			f"Bin items; v1 single-window cannot report it completely (request "
			f"{request.request_ref})"
		)
	tracking = None
	if tracking_reader is not None:
		tracking = read_tracking(
			tracking_reader,
			erpnext_warehouse_ref=request.erpnext_warehouse_ref,
			bins=bins,
			chunk_size=max_items,
		)
	report = build_report(bins, read_at=read_at, tracking=tracking)
	return client.report_snapshot(
		request.request_ref,
		report,
		idempotency_key=derive_idempotency_key(request.request_ref),
	)


def process_paged_request(
	request: BinViewRequest,
	*,
	client: BinViewClient,
	reader: BinReader,
	clock: Clock,
	attempt_ref_factory: Callable[[], str] = new_attempt_ref,
	deadline: AttemptDeadline | None = None,
	tracking_reader: TrackingReader | None = None,
) -> ReportResult:
	"""v1.2: one read attempt, reported as windows 0..N (``maxWindows > 1``).

	Reads up to ``maxWindows x maxItems + 1`` rows in ONE read attempt (one ``readAt``),
	refuses (:class:`WindowLimitExceeded`, nothing reported) above ``maxWindows x maxItems``,
	else reports each window in order under one fresh ``attemptRef`` with ``isFinal`` on the
	last. Returns the FINAL window's result. Any transport/DP2 error propagates mid-attempt
	(the poller's retry starts a NEW attempt at window 0, which DP2 treats as a supersede).
	Raises :class:`ReportIncomplete` if DP2 acknowledged the final window with an explicit
	``complete: false``, and :class:`AttemptDeadlineExceeded` (before sending a window) when the
	``deadline`` says that window's call could overrun the attempt budget.
	"""
	window = request.item_window
	max_items = int(window.max_items)
	max_windows = int(window.max_windows or 1)
	limit = max_windows * max_items
	read_at = clock.now_iso()
	bins = reader.read_bins_paged(
		erpnext_warehouse_ref=request.erpnext_warehouse_ref,
		page_size=max_items,
		max_rows=limit + 1,
	)
	if len(bins) > limit:
		raise WindowLimitExceeded(
			f"warehouse {request.erpnext_warehouse_ref!r} has more than {limit} Bin items "
			f"({max_windows} windows x {max_items}); refusing to report an incomplete "
			f"snapshot (request {request.request_ref})",
			limit=limit,
		)
	# RT-918: all of the attempt's tracking is read before window 0, so a refusal reports nothing.
	tracking = None
	if tracking_reader is not None:
		tracking = read_tracking(
			tracking_reader,
			erpnext_warehouse_ref=request.erpnext_warehouse_ref,
			bins=bins,
			chunk_size=max_items,
		)
	chunks = split_windows(bins, max_items)
	attempt_ref = attempt_ref_factory()
	last_seq = len(chunks) - 1
	result: ReportResult | None = None
	for seq, chunk in enumerate(chunks):
		if deadline is not None:
			deadline.check(seq)
		report = build_report(
			chunk,
			read_at=read_at,
			window=BinViewReportWindow(attempt_ref=attempt_ref, window_seq=seq, is_final=(seq == last_seq)),
			tracking=tracking,
		)
		result = client.report_snapshot(
			request.request_ref,
			report,
			idempotency_key=derive_window_idempotency_key(request.request_ref, attempt_ref, seq),
		)
	assert result is not None  # split_windows always yields at least one window
	if result.recorded_view.complete is False:
		raise ReportIncomplete(
			f"DP2 acknowledged final window {last_seq} of attempt {attempt_ref} as not "
			f"complete (request {request.request_ref})"
		)
	return result
