# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Pure batch-posting policy for stock-moving Sales Invoices (RT-452, RT-50 Gate 7 G7-10 / G7-11).

It replaces the blanket RT-47 D4 rule (every batch/serial Item fails closed) for **batch** Items.
Serial Items stay fail-closed (RT-50 Q11). Owner decision: RT-50 comment 11765.

  - **Before insert** (:func:`assert_tracking_postable`), per invoice row, never per Item (two lines of
    one Item may carry different Batches):

    * ``has_serial_no`` → :class:`stock_policy.UnsupportedTrackedItem` (unchanged);
    * a ``has_batch_no`` Item posts only as ``trackingMode: batch`` with an ``erpnextBatchRef``; an
      omitted, null or ``none`` mode, or no ref, is refused (the RT-47 D4 fail-closed rule, so an older
      feed that omits the fields behaves exactly as before);
    * a ref, or ``trackingMode: batch``, on an Item that is not batch-tracked is refused;
    * ``trackingMode: serial`` is refused (Backend-Core never emits it in this version).

    The Batch must exist and belong to the row's Item (:func:`assert_batches_belong`). ERPNext itself
    refuses an expired or disabled Batch and a batch shortfall (``BatchExpiredError``,
    ``BatchNegativeStockError``, ``NegativeStockError``: all ``frappe.ValidationError``), which the glue
    maps to ``validation`` with the insert rolled back. ERPNext auto-pick is never relied on: a sale row
    carries its Batch (``builder``), and a row without one never reaches insert.
  - **Returns and voids** carry no Batch of their own: ERPNext rebuilds the original row's Batch through
    the mandatory ``sales_invoice_item`` link. The original batch row must have a Serial and Batch
    Bundle holding exactly one Batch, or the credit note fails closed (:func:`return_batch_intents`).
  - **After submit** (:func:`verify_batch_postings`, inside the glue's savepoint) each batch row must have
    exactly one Stock Ledger Entry for exactly its stock quantity, and a bundle with exactly one entry
    for the intended Batch and quantity. A mismatch is never reported as success: it rolls back.

All rejections are ``permanently_rejected`` / ``validation``; the operator repairs ERPNext and uses
``re_post``. This module imports NO frappe and never mutates its inputs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from .contracts import PostingWorkItem
from .stock_policy import ReturnLineMismatch, UnsupportedTrackedItem


class TrackedLineRejected(Exception):
	"""A line's tracking intent and its ERPNext Item disagree, or its Batch is unusable (``validation``)."""


class BatchPostingMismatch(Exception):
	"""ERPNext wrote a batch posting other than the intent (RT-452 post-write check, ``validation``)."""


@dataclass(frozen=True)
class LineTracking:
	"""What the feed says about one invoice row's tracking (``SaleLine.trackingMode`` / ``erpnextBatchRef``)."""

	tracking_mode: str | None
	batch_ref: str | None


def line_tracking(work_item: PostingWorkItem) -> tuple[LineTracking, ...]:
	"""The tracking intent per invoice row, in the row order the builders emit.

	A sale, a void or a refund has one row per sale line; a ``return`` has one row per returned line,
	which points at its sale line by ``lineRef`` (checked when the work item was parsed).
	"""
	sale = work_item.sale
	ref = work_item.reversal_of
	lines = sale.lines
	if ref is not None and ref.reversal_kind == "return":
		by_ref = {line.line_ref: line for line in sale.lines}
		lines = tuple(by_ref[returned.line_ref] for returned in ref.return_lines)
	return tuple(LineTracking(line.tracking_mode, line.erpnext_batch_ref) for line in lines)


def is_batch_item(flags: Mapping[str, object] | None) -> bool:
	"""True if the Item master marks it batch-tracked (an Item absent from the read → False)."""
	return bool(flags) and bool(int(flags.get("has_batch_no") or 0))  # type: ignore[union-attr]


def _is_serial_item(flags: Mapping[str, object] | None) -> bool:
	return bool(flags) and bool(int(flags.get("has_serial_no") or 0))  # type: ignore[union-attr]


def assert_tracking_postable(
	doc: Mapping[str, object],
	lines: Sequence[LineTracking],
	tracking: Mapping[str, Mapping[str, object]],
) -> None:
	"""Refuse a stock-moving ``doc`` whose rows cannot post as their feed lines say (fail closed).

	``tracking`` maps ``item_code`` → ``{has_batch_no, has_serial_no}`` read from the Item master. A
	document that does not move stock (``update_stock`` falsy, a refund) involves no bundle and is not
	checked. An Item absent from ``tracking`` is left to ERPNext's own link validation.
	"""
	if not doc.get("update_stock"):
		return
	items = list(doc.get("items") or [])  # type: ignore[call-overload]
	serial = [item["item_code"] for item in items if _is_serial_item(tracking.get(item["item_code"]))]
	if serial:
		raise UnsupportedTrackedItem(list(dict.fromkeys(serial)))
	if len(items) != len(lines):
		raise TrackedLineRejected(
			f"the invoice has {len(items)} row(s) but the feed has {len(lines)} line(s)"
		)
	for pos, (item, line) in enumerate(zip(items, lines, strict=True), start=1):
		problem = _line_problem(item["item_code"], line, is_batch_item(tracking.get(item["item_code"])))
		if problem:
			raise TrackedLineRejected(f"row {pos} Item {item['item_code']!r}: {problem}")


def _line_problem(item_code: str, line: LineTracking, batch_item: bool) -> str | None:
	"""Why one row cannot post, or ``None`` (the RT-452 stock-policy matrix)."""
	if line.tracking_mode == "serial":
		return "trackingMode 'serial' is not postable (serial selling is fail-closed, RT-50 Q11)"
	if not batch_item:
		if line.tracking_mode == "batch":
			return "trackingMode 'batch' but the ERPNext Item is not batch-tracked (has_batch_no = 0)"
		if line.batch_ref is not None:
			return f"erpnextBatchRef {line.batch_ref!r} on an Item that is not batch-tracked"
		return None
	if line.tracking_mode != "batch":
		return (
			f"the ERPNext Item is batch-tracked but trackingMode is {line.tracking_mode!r} "
			"(the Connector never lets ERPNext pick a Batch)"
		)
	if line.batch_ref is None:
		return "batch line without an erpnextBatchRef (the Connector never lets ERPNext pick a Batch)"
	return None


def assert_batches_belong(doc: Mapping[str, object], batches: Mapping[str, Mapping[str, object]]) -> None:
	"""Every row ``batch_no`` must name an existing Batch of that row's Item (fail closed).

	``batches`` maps Batch name → ``{item}`` read from the Batch master.
	"""
	for pos, item in enumerate(doc.get("items") or [], start=1):  # type: ignore[arg-type]
		batch = item.get("batch_no")
		if not batch:
			continue
		master = batches.get(batch)
		if master is None:
			raise TrackedLineRejected(f"row {pos}: Batch {batch!r} does not exist in ERPNext")
		if master.get("item") != item["item_code"]:
			raise TrackedLineRejected(
				f"row {pos}: Batch {batch!r} belongs to Item {master.get('item')!r}, not {item['item_code']!r}"
			)


def sale_batch_intents(
	doc: Mapping[str, object], tracking: Mapping[str, Mapping[str, object]]
) -> dict[int, str]:
	"""``{row position: Batch}`` for a stock-moving sale: each batch Item row's own ``batch_no``."""
	if not doc.get("update_stock"):
		return {}
	return {
		pos: item["batch_no"]
		for pos, item in enumerate(doc.get("items") or [], start=1)  # type: ignore[arg-type]
		if is_batch_item(tracking.get(item["item_code"]))
	}


def return_batch_intents(
	doc: Mapping[str, object],
	lines: Sequence[LineTracking],
	tracking: Mapping[str, Mapping[str, object]],
	original_batches: Mapping[str, Sequence[str] | None],
) -> dict[int, str]:
	"""``{row position: Batch}`` for a stock-moving credit note: each batch row's ORIGINAL Batch (G7-10).

	``original_batches`` maps an original row name → the Batches of its bundle (``None`` = no bundle).
	The original must hold exactly one Batch; the feed's own ref, when present, must be that Batch.
	"""
	if not doc.get("update_stock"):
		return {}
	intents: dict[int, str] = {}
	items = doc.get("items") or []
	for pos, (item, line) in enumerate(zip(items, lines, strict=True), start=1):  # type: ignore[arg-type]
		if not is_batch_item(tracking.get(item["item_code"])):
			continue
		if item.get("batch_no") or item.get("use_serial_batch_fields"):
			raise ReturnLineMismatch(f"credit note row {pos} carries a Batch of its own (G7-10)")
		batch = _original_batch(pos, item.get("sales_invoice_item"), original_batches)
		if line.batch_ref is not None and line.batch_ref != batch:
			raise ReturnLineMismatch(
				f"credit note row {pos}: the feed Batch {line.batch_ref!r} != the original row's Batch {batch!r}"
			)
		intents[pos] = batch
	return intents


def _original_batch(
	pos: int, original_row: object, original_batches: Mapping[str, Sequence[str] | None]
) -> str:
	if not original_row:
		raise ReturnLineMismatch(
			f"credit note row {pos} is not linked to an original row (sales_invoice_item)"
		)
	batches = original_batches.get(original_row)  # type: ignore[call-overload]
	if not batches:
		raise ReturnLineMismatch(
			f"credit note row {pos}: the original row has no Serial and Batch Bundle (its Batch is unknown)"
		)
	if len(set(batches)) != 1:
		raise ReturnLineMismatch(
			f"credit note row {pos}: the original row holds several Batches {sorted(set(batches))}"
		)
	return batches[0]


def verify_batch_postings(
	intents: Mapping[int, str],
	*,
	inward: bool,
	rows: Sequence[Mapping[str, object]],
	ledger: Sequence[Mapping[str, object]],
	entries: Sequence[Mapping[str, object]],
) -> None:
	"""Raise :class:`BatchPostingMismatch` unless ERPNext posted each intended Batch exactly (891/892).

	``rows`` are the submitted invoice's item rows ``(name, idx, stock_qty, serial_and_batch_bundle)``,
	``ledger`` its live Stock Ledger Entries ``(voucher_detail_no, actual_qty, serial_and_batch_bundle)``
	and ``entries`` the Serial and Batch Entries of its bundles ``(parent, batch_no, qty)``. A sale moves
	stock out (``inward`` False); a credit note moves it back in. Quantities are compared as
	:class:`Decimal` from their string form, never as floats.
	"""
	for pos, batch in sorted(intents.items()):
		row = _row_at(pos, rows)
		stock_qty = abs(Decimal(str(row.get("stock_qty") or 0)))
		bundle = row.get("serial_and_batch_bundle")
		if not bundle:
			raise BatchPostingMismatch(
				f"row {pos}: no Serial and Batch Bundle was posted for Batch {batch!r}"
			)
		_check_ledger(pos, row["name"], bundle, stock_qty if inward else -stock_qty, ledger)
		_check_bundle(pos, bundle, batch, stock_qty, entries)


def _row_at(pos: int, rows: Sequence[Mapping[str, object]]) -> Mapping[str, object]:
	found = [row for row in rows if int(row.get("idx") or 0) == pos]  # type: ignore[call-overload]
	if len(found) != 1:
		raise BatchPostingMismatch(f"row {pos}: {len(found)} submitted invoice row(s) at that position")
	return found[0]


def _check_ledger(
	pos: int, row_name: object, bundle: object, expected: Decimal, ledger: Sequence[Mapping[str, object]]
) -> None:
	"""Exactly one live SLE for the row, for exactly ``expected`` stock units, through its bundle."""
	sles = [sle for sle in ledger if sle.get("voucher_detail_no") == row_name]
	if len(sles) != 1:
		raise BatchPostingMismatch(f"row {pos}: {len(sles)} Stock Ledger Entries, expected exactly 1")
	actual = Decimal(str(sles[0].get("actual_qty")))
	if actual != expected:
		raise BatchPostingMismatch(f"row {pos}: the Stock Ledger Entry moved {actual}, expected {expected}")
	if sles[0].get("serial_and_batch_bundle") != bundle:
		raise BatchPostingMismatch(f"row {pos}: the Stock Ledger Entry does not use the row's bundle")


def _check_bundle(
	pos: int, bundle: object, batch: str, stock_qty: Decimal, entries: Sequence[Mapping[str, object]]
) -> None:
	"""The row's bundle holds exactly one entry: the intended Batch for the row's stock quantity."""
	found = [entry for entry in entries if entry.get("parent") == bundle]
	if len(found) != 1:
		raise BatchPostingMismatch(
			f"row {pos}: the bundle holds {len(found)} Batch entries, expected exactly 1"
		)
	if found[0].get("batch_no") != batch:
		raise BatchPostingMismatch(
			f"row {pos}: ERPNext posted Batch {found[0].get('batch_no')!r}, expected {batch!r}"
		)
	qty = abs(Decimal(str(found[0].get("qty"))))
	if qty != stock_qty:
		raise BatchPostingMismatch(
			f"row {pos}: the bundle moved {qty} of Batch {batch!r}, expected {stock_qty}"
		)
