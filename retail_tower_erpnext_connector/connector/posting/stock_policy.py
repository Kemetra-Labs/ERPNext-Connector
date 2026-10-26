# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Pure stock-posting policy for stock-moving Sales Invoices (RT-48).

Owner decisions (RT-47 comment 10287; RT-48 comment 10291):

  - **D4 — no guessed batches.** ERPNext would otherwise AUTO-PICK a batch/serial on an outward line
    (``auto_create_serial_and_batch_bundle_for_outward`` defaults on) — a guess. A serial Item on a
    stock-moving document fails CLOSED (:class:`UnsupportedTrackedItem` → ``validation``). RT-452
    (RT-50 G7-11) replaces the blanket rule for batch Items: they post only with the Batch the feed
    supplies (``batch_policy``).
  - **D5 — a full void restores stock exactly once, and only if the sale moved it.** The return
    invoice MIRRORS the original invoice's ``update_stock`` (a legacy ``update_stock=0`` invoice
    gets an accounting-only credit note — never a fabricated stock restoration; ERPNext also refuses
    ``update_stock=1`` against a non-stock original) and references each original line via
    ``sales_invoice_item`` so ERPNext's per-line over-return cap (``StockOverReturnError``) applies.
  - **RT-80 — a full void reverses the original's rounding too.** The return also MIRRORS the
    original's ``disable_rounded_total``. A sale posted before RT-80 was rounded (10.49 -> AR 10.00
    + Round Off 0.49); an unrounded -10.49 void of it would leave a -0.49 customer credit and never
    reverse the Round Off. Mirroring makes the void net to exactly zero in both cases.
  - **RT-78 / RT-10 D6 — a full void pays back exactly what the original was paid with.** The
    void's payments are rebuilt from the ORIGINAL invoice's own payment rows (Mode of Payment and
    amount as booked), so a tender map remapped since the sale cannot refund through a different
    mode. The void's ``sale.tenders`` (already negated onto ``doc`` by the reversal builder) only
    cross-check it: a paid void of an unpaid original, an unpaid void of a paid original, or a
    different total fails closed (:class:`VoidSettlementMismatch`).

  - **RT-16 / RT-14 D6 — a partial return links by line identity.** Each return row is matched to
    the original row carrying the same ``rt_line_ref`` (never by position), restores to that row's
    warehouse and mirrors ``update_stock``; an original posted before RT-16 has no line refs and
    fails closed (:func:`link_return_to_original`).

The frappe glue only READS the Item flags / original invoice rows and calls these functions.
This module imports NO frappe and never mutates its inputs.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from decimal import Decimal


class UnsupportedTrackedItem(Exception):
	"""A serial-tracked Item on a stock-moving document (RT-47 D4 fail-closed; batch Items: RT-452)."""

	def __init__(self, item_codes: Sequence[str]) -> None:
		super().__init__(
			f"stock posting does not support serial-tracked Item(s) {', '.join(item_codes)} "
			"(serial selling is fail-closed, RT-50 Q11; the connector never guesses a serial or batch)"
		)
		self.item_codes = tuple(item_codes)


class ReturnLineMismatch(Exception):
	"""A void's lines do not line up 1:1 with the original invoice rows (fail-closed)."""


class VoidSettlementMismatch(ReturnLineMismatch):
	"""A void's tenders disagree with how the original invoice was settled (RT-78 fail-closed).

	A subclass of :class:`ReturnLineMismatch` so the glue's existing mapping (→ ``validation``)
	covers it: the void is rejected and nothing is posted.
	"""


def tracking_item_codes(doc: Mapping[str, object]) -> list[str]:
	"""The distinct ``item_code``s on ``doc`` in first-seen order (the Item-flag lookup set)."""
	seen: list[str] = []
	for item in doc.get("items") or []:  # type: ignore[union-attr]
		code = item["item_code"]
		if code not in seen:
			seen.append(code)
	return seen


def link_void_to_original(
	doc: Mapping[str, object],
	*,
	original_update_stock: int,
	original_disable_rounded_total: int,
	original_items: Sequence[Mapping[str, object]],
	original_is_pos: int,
	original_payments: Sequence[Mapping[str, object]],
) -> dict:
	"""Return a copy of the void return ``doc`` mirrored onto and linked to its original invoice.

	- ``update_stock`` := the original's (never restore stock the sale did not move).
	- ``disable_rounded_total`` := the original's (RT-80: reverse exactly what the sale posted).
	- ``payments`` := the original's payment rows, negated (RT-78), after the cross-check in
	  :func:`_mirror_settlement`; a void of an unpaid original carries none.
	- each return line is paired with the original row at the same position (rows ordered by
	  ``idx``; DP2 emits sale and reversal lines in the same ``ORDER BY sale_lines.id``), checked
	  for ``item_code`` and exact quantity (:func:`_check_pair`), then gets ``sales_invoice_item``
	  and the ORIGINAL row's warehouse, so stock goes back where the sale took it from even if the
	  store→warehouse map changed since (Codex P1, PR #42). Any mismatch raises
	  :class:`ReturnLineMismatch`.
	"""
	if not doc.get("is_return"):
		raise ValueError("link_void_to_original expects a return (is_return=1) document")

	out = copy.deepcopy(dict(doc))
	lines = out.get("items") or []
	rows = _rows_in_line_order(lines, original_items)
	for pos, (line, row) in enumerate(zip(lines, rows, strict=True), start=1):
		_link_line(pos, line, row)
	out["update_stock"] = 1 if int(original_update_stock or 0) else 0
	out["disable_rounded_total"] = 1 if int(original_disable_rounded_total or 0) else 0
	_mirror_settlement(out, original_is_pos=original_is_pos, original_payments=original_payments)
	return out


def _mirror_settlement(
	out: dict, *, original_is_pos: int, original_payments: Sequence[Mapping[str, object]]
) -> None:
	"""Replace the void's tender-built payments with the original's rows, negated (fail closed).

	All amounts are compared and copied as :class:`Decimal` built from their string form (the DB
	amount is a float), never float arithmetic.
	"""
	void_payments = out.get("payments") or []
	if not int(original_is_pos or 0):
		if void_payments:
			raise VoidSettlementMismatch(
				"void carries tenders but the original invoice is unpaid (not is_pos) — refusing to pay "
				"cash out against a sale ERPNext never recorded as paid"
			)
		return
	if not void_payments:
		raise VoidSettlementMismatch(
			"the original invoice is paid (is_pos) but the void carries no tenders — ERPNext would "
			"leave the refund as a customer credit instead of paying it back"
		)
	rows = sorted(original_payments, key=lambda r: int(r.get("idx") or 0))  # type: ignore[arg-type]
	paid = sum((Decimal(str(r["amount"])) for r in rows), Decimal(0))
	refunded = -sum((Decimal(str(p["amount"])) for p in void_payments), Decimal(0))
	if paid != refunded:
		raise VoidSettlementMismatch(
			f"void tender total {refunded} != original invoice payment total {paid}"
		)
	out["is_pos"] = 1
	out["payments"] = [
		{"mode_of_payment": r["mode_of_payment"], "amount": str(-Decimal(str(r["amount"])))} for r in rows
	]


def _rows_in_line_order(lines: Sequence[object], original_items: Sequence[Mapping[str, object]]) -> list:
	"""Original rows ordered by ``idx``; fail closed unless there is exactly one per void line."""
	rows = sorted(original_items, key=lambda r: int(r["idx"]))  # type: ignore[arg-type]
	if len(lines) != len(rows):
		raise ReturnLineMismatch(
			f"void carries {len(lines)} line(s) but the original invoice has {len(rows)} row(s)"
		)
	return rows


def _link_line(pos: int, line: dict, row: Mapping[str, object]) -> None:
	"""Check one void line against its original row, then link it and restore to the row's warehouse."""
	_check_pair(pos, line, row)
	line["sales_invoice_item"] = row["name"]
	if row.get("warehouse"):
		line["warehouse"] = row["warehouse"]


def _check_pair(pos: int, line: Mapping[str, object], row: Mapping[str, object]) -> None:
	"""Fail closed unless a void line matches its original row's Item and exact sold quantity.

	A full void restores EXACTLY the sold quantity; both sides are compared as :class:`Decimal`
	built from their string form (the DB row's qty is a float), never float-vs-string.
	"""
	if line["item_code"] != row["item_code"]:
		raise ReturnLineMismatch(
			f"void line {pos} item {line['item_code']!r} != original row item {row['item_code']!r}"
		)
	returned = abs(Decimal(str(line["qty"])))
	sold = abs(Decimal(str(row["qty"])))
	if returned != sold:
		raise ReturnLineMismatch(
			f"void line {pos} quantity {returned} != original sold quantity {sold} (full void only)"
		)


def link_return_to_original(
	doc: Mapping[str, object],
	*,
	original_update_stock: int,
	original_items: Sequence[Mapping[str, object]],
	original_customer: str,
) -> dict:
	"""Return a copy of the partial-return ``doc`` linked to its ORIGINAL invoice rows (RT-16).

	RT-14 D6: each return row is matched by ``rt_line_ref`` to the one original row carrying the same
	sale-line identity — never by position, since a return carries any subset of the lines. The
	match is checked for ``item_code`` and for a quantity no larger than that row sold, then gets
	``sales_invoice_item`` (so ERPNext's cumulative over-return cap applies) and the ORIGINAL row's
	warehouse, UOM and conversion factor (a unit map or Item UOM factor changed since the sale must not
	change how many stock units the return restores).
	``update_stock`` and the ``customer`` mirror the original invoice (a store map changed since the
	sale must not credit another customer — Codex P2, PR #50). ``original_items`` must be the original
	invoice's own rows (the glue filters by parent), each with ``rt_line_ref``. An original posted
	before RT-16 carries no line refs and fails closed (:class:`ReturnLineMismatch`): the owner chose
	identity over positional pairing (D6).
	"""
	if not doc.get("is_return"):
		raise ValueError("link_return_to_original expects a return (is_return=1) document")
	out = copy.deepcopy(dict(doc))
	for pos, line in enumerate(out.get("items") or [], start=1):
		row = _row_for_line_ref(pos, line, original_items)
		_check_return_row(pos, line, row)
		line["sales_invoice_item"] = row["name"]
		for field in ("warehouse", "uom", "conversion_factor"):
			if row.get(field):
				line[field] = row[field]
	out["update_stock"] = 1 if int(original_update_stock or 0) else 0
	if original_customer:
		out["customer"] = original_customer
	return out


def mirror_company(doc: Mapping[str, object], original_company: str | None) -> dict:
	"""Return a copy of the credit note ``doc`` on the ORIGINAL invoice's company (RT-378 finding 2).

	Applied after :func:`link_void_to_original` / :func:`link_return_to_original`: a void or return
	lands where the sale did, never on the site default (which may have changed since the sale). An
	original without a company fails closed (:class:`ReturnLineMismatch` → ``validation``).
	"""
	if not original_company:
		raise ReturnLineMismatch("the original invoice carries no company — the credit note's company is unknown")
	return {**doc, "company": original_company}


def _row_for_line_ref(pos: int, line: Mapping[str, object], rows: Sequence[Mapping[str, object]]):
	"""The single original row carrying ``line``'s ``rt_line_ref``; fail closed otherwise."""
	ref = line.get("rt_line_ref")
	matches = [r for r in rows if ref and r.get("rt_line_ref") == ref]
	if not matches:
		raise ReturnLineMismatch(
			f"return line {pos}: the original invoice has no row with rt_line_ref {ref!r} "
			"(posted before RT-16, or a line of another sale)"
		)
	if len(matches) > 1:
		raise ReturnLineMismatch(f"return line {pos}: rt_line_ref {ref!r} is ambiguous on the original invoice")
	return matches[0]


def _check_return_row(pos: int, line: Mapping[str, object], row: Mapping[str, object]) -> None:
	"""Fail closed unless the return row's Item matches and its quantity is within what was sold."""
	if line["item_code"] != row["item_code"]:
		raise ReturnLineMismatch(
			f"return line {pos} item {line['item_code']!r} != original row item {row['item_code']!r}"
		)
	returned = abs(Decimal(str(line["qty"])))
	sold = abs(Decimal(str(row["qty"])))
	if returned > sold:
		raise ReturnLineMismatch(f"return line {pos} quantity {returned} > original sold quantity {sold}")
