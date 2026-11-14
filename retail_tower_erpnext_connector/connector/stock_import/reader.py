# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Pure read logic of the ERP-origin stock import (RT-1287): ledger rows → report rows.

The frappe-touching seam is :class:`LedgerSource` (implemented in :mod:`.frappe_glue`); this
module turns what it returns into one ``StockImportReport`` per pulled request, following the
contract's read rule (``stock-import.yaml`` ``StockImportRequest``):

* the submitted and cancelled Stock Ledger Entries at ``erpnextWarehouseRef`` whose ``creation``
  is at or before the site clock minus ``settleSeconds``;
* each expanded into report rows, one per entry of its Serial and Batch Bundle when the bundle
  carries a Batch (``entryIndex`` = the entry's 0-based position in the bundle), else one row
  (``entryIndex`` 0; the Batch from the row's own ``batch_no`` for a legacy ledger row). A
  serial-only bundle (no Batch on any entry) is reported as ONE row with the ledger quantity and no
  Batch: serial Items are outside this contract version ("reported with ``erpnextBatchRef`` null
  and no serial number"), and per-serial rows would carry no extra fact. Open for the contract
  owner: a serial-and-batch Item's bundle gives one row per serial entry, so several rows can share
  one DC-4 key (voucher, detail, warehouse, Batch);
* ordered by (``creation``, ``ledgerEntryRef``, ``entryIndex``), ``creation`` and the SLE name in
  ERPNext's stored order (the source orders them in SQL, so the database collation decides);
* at most ``maxRows`` rows strictly after ``after``; a page may end inside a ledger row.

``actualQty`` is the row's signed stock movement (``stock-import.yaml`` 1.1.0-draft, RT-1396, RT-1398):
the ledger ``actual_qty``, except for an untracked Stock Reconciliation row (no Serial and Batch
Bundle, no ``batch_no``), which reads the Stock Reconciliation Item its ``voucher_detail_no`` names:
a row stored with ``actual_qty`` 0 reports ``qty - current_qty`` (ERPNext v16 records that change only
in ``qty_after_transaction``), and a cancelled row stored with a non-zero ``actual_qty`` (the reverse
row of a cancellation, which v16 fills with the balance before the reconciliation) reports the
negation, ``-(qty - current_qty)``, so it pairs with its original (RT-1401). Only quantity fields
are read, never a valuation field.

FACTS ONLY (G8-7): every voucher type is reported, Retail Tower's own Sales Invoices included;
nothing is classified, filtered or deduplicated by business rule. No valuation field is read.

``creation`` is ERPNext's stored value (naive, in the site's system time zone). It is rendered
with that zone's offset; in a repeated daylight-saving hour the earlier offset (``fold=0``) is
used, so the same stored value always renders the same way. A received ``after.creation`` (any
offset) is converted back to naive site time before it is compared, so comparisons and the read
order are always on stored values.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from decimal import Decimal
from typing import Protocol

from ..bin_view.worker import quantize_qty
from .contracts import LedgerKey, StockImportRefusal, StockImportReport, StockImportRequest, StockImportRow

UNKNOWN_WAREHOUSE = "unknown_warehouse"
STOCK_RECONCILIATION = "Stock Reconciliation"


class StockImportReadError(Exception):
	"""A fact the contract's read rule needs is missing in ERPNext: fail closed, never report a guess.

	The poller counts it like any read failure (retry next tick, then abandon the request and log it).
	"""


@dataclass(frozen=True)
class RawLedgerEntry:
	"""One Stock Ledger Entry as read (no valuation field)."""

	name: str
	creation: datetime  # naive, site time zone (as stored)
	voucher_type: str
	voucher_no: str
	voucher_detail_no: str | None
	posting_date: object  # date or ISO string
	posting_time: object  # timedelta (Frappe Time), time or string
	is_cancelled: bool
	actual_qty: float
	stock_uom: str
	item_code: str
	batch_no: str | None
	serial_and_batch_bundle: str | None


@dataclass(frozen=True)
class RawBundleEntry:
	"""One Serial and Batch Entry of a bundle, in bundle order (``idx``)."""

	batch_no: str | None
	qty: float


@dataclass(frozen=True)
class RawReconciliationItem:
	"""The quantity fields of one Stock Reconciliation Item (no valuation field).

	``qty`` is None when the row leaves the counted quantity blank; the reader never guesses it.
	"""

	qty: float | None
	current_qty: float


@dataclass(frozen=True)
class VoucherFacts:
	"""The voucher-level facts of one (voucher type, voucher no)."""

	purpose: str | None = None
	is_return: bool = False
	return_against: str | None = None
	has_retail_provenance: bool = False
	correction_ref: str | None = None


@dataclass(frozen=True)
class LedgerStart:
	"""Where a ledger read starts: rows with (``creation``, ``name``) at or after this position.

	``name`` None: every row with ``creation`` at or after ``creation``. ``inclusive`` False
	excludes the row (``creation``, ``name``) itself (used to continue after a fetched chunk).
	"""

	creation: datetime
	name: str | None
	inclusive: bool = True


class LedgerSource(Protocol):
	"""The ERPNext reads the reader needs (implemented by :mod:`.frappe_glue`)."""

	def warehouse_exists(self, warehouse: str) -> bool: ...

	def site_now(self) -> datetime:
		"""The site clock that stamps ``creation`` (naive, site time zone)."""
		...

	def site_timezone(self) -> tzinfo: ...

	def read_ledger(
		self, warehouse: str, *, start: LedgerStart | None, cutoff: datetime, limit: int
	) -> list[RawLedgerEntry]:
		"""At most ``limit`` Stock Ledger Entries at ``warehouse`` from ``start`` (or the
		beginning) with ``creation`` ≤ ``cutoff``, ordered by (``creation``, ``name``)."""
		...

	def bundle_entries(self, bundles: list[str]) -> dict[str, list[RawBundleEntry]]:
		"""The entries of each bundle, in bundle order."""
		...

	def voucher_facts(self, vouchers: list[tuple[str, str]]) -> dict[tuple[str, str], VoucherFacts]: ...

	def reconciliation_items(self, details: list[str]) -> dict[str, RawReconciliationItem]:
		"""The Stock Reconciliation Items by name (cancelled vouchers' rows included)."""
		...


def render_creation(value: datetime, zone: tzinfo) -> str:
	"""A stored (naive, site-time) ``creation`` as RFC 3339 with the site offset (earlier on fold)."""
	return value.replace(tzinfo=zone, fold=0).isoformat(timespec="microseconds")


def to_site_time(value: str, zone: tzinfo) -> datetime:
	"""A received RFC 3339 date-time (any offset) as naive site time, for comparison."""
	parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
	if parsed.tzinfo is None:
		raise ValueError(f"date-time without an offset: {value!r}")
	return parsed.astimezone(zone).replace(tzinfo=None)


def render_posting_date(value: object) -> str:
	if isinstance(value, datetime):
		return value.date().isoformat()
	if isinstance(value, date):
		return value.isoformat()
	return date.fromisoformat(str(value)).isoformat()


def render_posting_time(value: object) -> str:
	"""Frappe returns a Time field as ``timedelta``; render ``HH:MM:SS[.ffffff]`` (24-hour)."""
	if isinstance(value, timedelta):
		micros = value // timedelta(microseconds=1)
		if not 0 <= micros < 24 * 3600 * 10**6:
			raise ValueError(f"posting_time out of range: {value!r}")
		seconds, micro = divmod(micros, 10**6)
		value = time(seconds // 3600, seconds // 60 % 60, seconds % 60, micro)
	elif not isinstance(value, time):
		value = time.fromisoformat(str(value))
	text = value.strftime("%H:%M:%S")
	return f"{text}.{value.microsecond:06d}" if value.microsecond else text


def _entry_qty(entry: RawBundleEntry, ledger_qty: float) -> str:
	"""A bundle entry's quantity with the ledger row's sign (the entry's own when the row is 0)."""
	qty = Decimal(quantize_qty(entry.qty))
	if ledger_qty > 0:
		qty = abs(qty)
	elif ledger_qty < 0:
		qty = -abs(qty)
	return f"{qty:f}" if qty != 0 else quantize_qty(0)


def needs_reconciliation_item(entry: RawLedgerEntry) -> bool:
	"""True for an untracked Stock Reconciliation ledger row whose quantity comes from its item.

	Such a row has no Serial and Batch Bundle and no ``batch_no`` (a tracked row, a serial-only
	bundle included, is read from its bundle or its own ``actual_qty``), whatever the voucher
	purpose, and is either stored with ``actual_qty`` 0, or cancelled with a non-zero ``actual_qty``
	(the reverse row of a cancellation). Any other such row is not expected on v16 and passes
	through with its own ``actual_qty``.
	"""
	return (
		entry.voucher_type == STOCK_RECONCILIATION
		and not entry.serial_and_batch_bundle
		and not entry.batch_no
		# the stored value, as `_entry_qty` reads the ledger sign
		and (entry.actual_qty == 0 or entry.is_cancelled)
	)


def _reconciliation_qty(entry: RawLedgerEntry, item: RawReconciliationItem | None) -> str:
	"""``qty - current_qty`` of the reconciliation row (negated for a reverse row), each value at 6 dp
	half-even; fail closed if unknown.

	A row stored with ``actual_qty`` 0 is the reconciliation itself (an original, cancelled or not):
	its movement is ``qty - current_qty``. A cancelled row with a non-zero ``actual_qty`` is the reverse
	row ERPNext writes on cancellation, whose stored value is the balance before the reconciliation, not
	the movement: it reports the negation, so the pair nets to 0 (bench, RT-1401: 10 -> 12 cancelled
	stores +10 on the reverse row and reports -2).

	On the v16 bench both columns are NOT NULL (default 0) and a valuation-only row is stored with
	``qty`` = ``current_qty`` (delta 0), so ``qty`` None is a defence against a source that returns it,
	never a guess of 0.
	"""
	if item is None or item.qty is None:
		raise StockImportReadError(
			f"{entry.name}: no counted quantity for Stock Reconciliation Item {entry.voucher_detail_no!r}"
		)
	delta = Decimal(quantize_qty(item.qty)) - Decimal(quantize_qty(item.current_qty))
	if entry.actual_qty != 0:
		delta = -delta
	return f"{delta:f}" if delta != 0 else quantize_qty(0)


def expand_entry(
	entry: RawLedgerEntry,
	bundle: list[RawBundleEntry],
	facts: VoucherFacts,
	zone: tzinfo,
	reconciliation: RawReconciliationItem | None = None,
) -> list[StockImportRow]:
	"""One ledger row → its report rows (one per bundle entry when the bundle carries a Batch).

	``reconciliation`` is the Stock Reconciliation Item named by ``voucher_detail_no``; it is used
	only for a row :func:`needs_reconciliation_item` selects.
	"""
	base = {
		"ledger_entry_ref": entry.name,
		"voucher_type": entry.voucher_type,
		"voucher_no": entry.voucher_no,
		"voucher_detail_no": entry.voucher_detail_no or None,
		"purpose": facts.purpose or None,
		"is_return": bool(facts.is_return),
		"return_against": facts.return_against or None,
		"posting_date": render_posting_date(entry.posting_date),
		"posting_time": render_posting_time(entry.posting_time),
		"creation": render_creation(entry.creation, zone),
		"is_cancelled": bool(entry.is_cancelled),
		"stock_uom": entry.stock_uom,
		"erpnext_item_ref": entry.item_code,
		"has_retail_provenance": bool(facts.has_retail_provenance),
		"correction_ref": facts.correction_ref or None,
	}
	if bundle and any(b.batch_no for b in bundle):
		return [
			StockImportRow(
				entry_index=index,
				actual_qty=_entry_qty(b, entry.actual_qty),
				erpnext_batch_ref=b.batch_no or None,
				**base,
			)
			for index, b in enumerate(bundle)
		]
	actual_qty = (
		_reconciliation_qty(entry, reconciliation)
		if needs_reconciliation_item(entry)
		else quantize_qty(entry.actual_qty)
	)
	return [
		StockImportRow(
			entry_index=0,
			actual_qty=actual_qty,
			erpnext_batch_ref=entry.batch_no or None,
			**base,
		)
	]


def _iter_rows(
	source: LedgerSource,
	request: StockImportRequest,
	*,
	cutoff: datetime,
	zone: tzinfo,
	chunk: int,
) -> Iterator[StockImportRow]:
	"""Report rows strictly after ``request.after``, in read order, until the ledger runs out."""
	after = request.after
	start = None
	skip_ref, skip_through = None, -1
	if after is not None:
		start = LedgerStart(to_site_time(after.creation, zone), after.ledger_entry_ref)
		if after.ledger_entry_ref is not None:
			skip_ref, skip_through = after.ledger_entry_ref, int(after.entry_index or 0)
	warehouse = request.erpnext_warehouse_ref
	while True:
		entries = source.read_ledger(warehouse, start=start, cutoff=cutoff, limit=chunk)
		if not entries:
			return
		bundles = source.bundle_entries(
			sorted({e.serial_and_batch_bundle for e in entries if e.serial_and_batch_bundle})
		)
		facts = source.voucher_facts(sorted({(e.voucher_type, e.voucher_no) for e in entries}))
		details = sorted(
			{e.voucher_detail_no for e in entries if needs_reconciliation_item(e) and e.voucher_detail_no}
		)
		items = source.reconciliation_items(details) if details else {}
		for entry in entries:
			rows = expand_entry(
				entry,
				bundles.get(entry.serial_and_batch_bundle or "", []),
				facts.get((entry.voucher_type, entry.voucher_no), VoucherFacts()),
				zone,
				items.get(entry.voucher_detail_no or ""),
			)
			for row in rows:
				if row.ledger_entry_ref == skip_ref and row.entry_index <= skip_through:
					continue  # at or before ``after`` inside the boundary ledger row
				yield row
		if len(entries) < chunk:
			return
		last = entries[-1]
		start = LedgerStart(last.creation, last.name, inclusive=False)


def read_report(source: LedgerSource, request: StockImportRequest, *, read_at: str) -> StockImportReport:
	"""Read one request's page of facts, or refuse it when ERPNext has no such warehouse."""
	if not source.warehouse_exists(request.erpnext_warehouse_ref):
		return StockImportReport(
			rows=(),
			read_through=None,
			exhausted=False,
			read_at=read_at,
			refusal=StockImportRefusal(UNKNOWN_WAREHOUSE),
		)
	zone = source.site_timezone()
	cutoff = source.site_now() - timedelta(seconds=request.settle_seconds)
	rows: list[StockImportRow] = []
	for row in _iter_rows(source, request, cutoff=cutoff, zone=zone, chunk=request.max_rows + 1):
		rows.append(row)
		if len(rows) > request.max_rows:
			break
	page = tuple(rows[: request.max_rows])
	read_through: LedgerKey | None = page[-1].key if page else None
	return StockImportReport(
		rows=page,
		read_through=read_through,
		exhausted=len(rows) <= request.max_rows,
		read_at=read_at,
	)
