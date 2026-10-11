# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt
"""RT-378 (RT-283 F3, design B) — did ERPNext persist exactly what Retail Tower sent? (pure)

Retail Tower is the pricing authority (Pricing 656-659). ``ignore_pricing_rule`` and
``price_list_rate = rate`` (RT-377) ask ERPNext not to reprice; this module proves the outcome
(675-687). It compares the payload the Connector built with the Sales Invoice ERPNext holds:

* **per line**, paired by position (ERPNext keeps the row order it was given): Item, qty, UOM,
  rate, amount and warehouse, exactly; plus ``price_list_rate`` = rate, no discount
  (``discount_percentage`` / ``discount_amount`` = 0) and no margin (empty ``margin_type``,
  ``margin_rate_or_amount`` = 0). An extra row (e.g. a free item) or a missing one is a mismatch.
* **per document**: currency (ERPNext v15 holds it on the parent, not on Sales Invoice Item),
  customer and company (the expected company for the store; today the site default); no tax row
  (D4: Retail Tower sends none while Egyptian VAT is deferred); and ``grand_total`` equal to the sum
  of the line amounts.

A return or void keeps ERPNext's sign convention (negative qty and amount, positive rate), so the
payload and the document are compared as they are. Numbers compare as exact :class:`Decimal` built
from their string form (``"9.50"`` equals the float ``9.5``), never float arithmetic.

The glue decides what a mismatch means (D1): before submit it rolls back and rejects as
``validation``; after submit or on replay it acks ``reconciliation_required``. The Connector never
cancels or amends an ERP document (684). Every reason starts with ``representation:``.
This module imports NO frappe.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .contracts import PostingWorkItem

PREFIX = "representation:"

# Expected fields a caller may exempt (they are then not compared). Commercial values never are.
EXEMPTABLE = frozenset({"warehouse", "uom", "customer"})

# Line fields that must be present and equal to what was sent.
_LINE_TEXT_FIELDS = ("item_code", "uom", "warehouse")
_LINE_NUMBER_FIELDS = ("qty", "rate", "amount")
# ERP-derived adjustments that must be zero (an unset value counts as zero).
_ZERO_FIELDS = ("discount_percentage", "discount_amount", "margin_rate_or_amount")


class RepresentationMismatch(Exception):
	"""ERPNext holds a commercial value Retail Tower did not send (message starts with ``representation:``)."""


@dataclass(frozen=True)
class ExpectedLine:
	"""One item line as Retail Tower sent it. ``None`` = exempt from the comparison."""

	item_code: str
	qty: str
	uom: str | None
	rate: str
	amount: str
	warehouse: str | None


@dataclass(frozen=True)
class ExpectedInvoice:
	"""The commercial representation Retail Tower sent. ``customer=None`` = exempt."""

	currency: str
	customer: str | None
	company: str
	lines: tuple[ExpectedLine, ...]


@dataclass(frozen=True)
class PersistedInvoice:
	"""The commercial representation ERPNext holds: the document fields and one mapping per item row."""

	currency: object
	customer: object
	company: object
	grand_total: object
	tax_rows: int
	lines: tuple[Mapping[str, object], ...]


def expected_from_payload(
	payload: Mapping[str, object], *, company: str | None, exempt: frozenset[str] = frozenset()
) -> ExpectedInvoice:
	"""The representation in a built Sales Invoice payload; ``exempt`` fields are not compared.

	Raises :class:`RepresentationMismatch` when no expected company is known (fail closed) and
	:class:`ValueError` for an exemption outside :data:`EXEMPTABLE`.
	"""
	unknown = exempt - EXEMPTABLE
	if unknown:
		raise ValueError(f"cannot exempt {sorted(unknown)} from the representation check")
	if not company:
		raise RepresentationMismatch(f"{PREFIX} the expected company (site default) is not configured")
	return ExpectedInvoice(
		currency=str(payload["currency"]),
		customer=None if "customer" in exempt else str(payload["customer"]),
		company=company,
		lines=tuple(_expected_line(item, exempt) for item in payload["items"]),
	)


def _expected_line(item: Mapping[str, object], exempt: frozenset[str]) -> ExpectedLine:
	return ExpectedLine(
		item_code=str(item["item_code"]),
		qty=str(item["qty"]),
		uom=None if "uom" in exempt else str(item["uom"]),
		rate=str(item["rate"]),
		amount=str(item["amount"]),
		warehouse=None if "warehouse" in exempt else str(item["warehouse"]),
	)


def persisted_from_doc(doc) -> PersistedInvoice:
	"""Read the representation from a Sales Invoice (a Frappe document or a mapping, both with ``get``)."""
	return PersistedInvoice(
		currency=doc.get("currency"),
		customer=doc.get("customer"),
		company=doc.get("company"),
		grand_total=doc.get("grand_total"),
		tax_rows=len(doc.get("taxes") or ()),
		lines=tuple(_row_values(row) for row in doc.get("items") or ()),
	)


def _row_values(row) -> dict[str, object]:
	fields = (*_LINE_TEXT_FIELDS, *_LINE_NUMBER_FIELDS, "price_list_rate", "margin_type", *_ZERO_FIELDS)
	return {field: row.get(field) for field in fields}


def replay_exemptions(work_item: PostingWorkItem) -> frozenset[str]:
	"""The fields a replay does not compare against the rebuilt payload.

	The warehouse is RT-331's frozen-warehouse check (a void or return takes the original rows'
	warehouses). A partial return also takes the original rows' UOM and the original invoice's
	customer (``stock_policy.link_return_to_original``), which a rebuild cannot know.
	"""
	reversal = work_item.reversal_of
	if reversal is not None and reversal.reversal_kind == "return":
		return frozenset({"warehouse", "uom", "customer"})
	return frozenset({"warehouse"})


def check_representation(expected: ExpectedInvoice, persisted: PersistedInvoice) -> str | None:
	"""``None`` when ERPNext holds exactly ``expected``, else the first mismatch reason."""
	reason = _document_mismatch(expected, persisted) or _lines_mismatch(expected.lines, persisted.lines)
	return f"{PREFIX} {reason}" if reason else None


def _document_mismatch(expected: ExpectedInvoice, persisted: PersistedInvoice) -> str | None:
	for field in ("currency", "customer", "company"):
		want = getattr(expected, field)
		if want is not None and _text(getattr(persisted, field)) != want:
			return f"document {field} {getattr(persisted, field)!r} is not the sent {want!r}"
	if persisted.tax_rows:
		return f"document carries {persisted.tax_rows} tax row(s); Retail Tower sent none (tax deferred, D4)"
	line_total = sum((_number(line.amount) for line in expected.lines), Decimal(0))
	if _number(persisted.grand_total) != line_total:
		return f"document grand_total {persisted.grand_total!r} is not the line total {line_total}"
	return None


def _lines_mismatch(expected: Sequence[ExpectedLine], rows: Sequence[Mapping[str, object]]) -> str | None:
	if len(rows) != len(expected):
		return f"ERPNext holds {len(rows)} item row(s); Retail Tower sent {len(expected)} line(s)"
	for pos, (line, row) in enumerate(zip(expected, rows, strict=True), start=1):
		reason = _line_mismatch(line, row)
		if reason:
			return f"line {pos} {reason}"
	return None


def _line_mismatch(line: ExpectedLine, row: Mapping[str, object]) -> str | None:
	return _sent_values_mismatch(line, row) or _adjustment_mismatch(line, row)


def _sent_values_mismatch(line: ExpectedLine, row: Mapping[str, object]) -> str | None:
	for field in _LINE_TEXT_FIELDS:
		want = getattr(line, field)
		if want is not None and _text(row.get(field)) != want:
			return f"{field} {row.get(field)!r} is not the sent {want!r}"
	for field in _LINE_NUMBER_FIELDS:
		want = getattr(line, field)
		if _number(row.get(field)) != _number(want):
			return f"{field} {row.get(field)!r} is not the sent {want}"
	return None


def _adjustment_mismatch(line: ExpectedLine, row: Mapping[str, object]) -> str | None:
	"""ERPNext derived no list price, discount or margin Retail Tower did not send (D6, decision 658)."""
	if _number(row.get("price_list_rate")) != _number(line.rate):
		return f"price_list_rate {row.get('price_list_rate')!r} is not the sent rate {line.rate}"
	for field in _ZERO_FIELDS:
		if _number(row.get(field) or 0) != 0:
			return f"{field} {row.get(field)!r} is not zero (ERP-derived adjustment)"
	if row.get("margin_type"):
		return f"margin_type {row.get('margin_type')!r} is set (ERP-derived margin)"
	return None


def _text(value: object) -> str | None:
	"""A persisted text value as a string (``None`` stays ``None``)."""
	return None if value is None else str(value)


def _number(value: object) -> Decimal | None:
	"""Exact decimal of a persisted or sent number; ``None`` (or unparseable) never equals a number."""
	if value is None or isinstance(value, bool):
		return None
	try:
		return Decimal(str(value))
	except InvalidOperation:
		return None
