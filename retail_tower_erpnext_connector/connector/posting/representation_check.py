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
  customer and company (set explicitly in the payload: the site default for a sale, the original
  invoice's for a credit note); ``ignore_pricing_rule`` = 1 (RT-377); no tax row (D4: Retail Tower
  sends none while Egyptian VAT is deferred); and ``grand_total`` equal to the sum of the line
  amounts.

**Old invoices on replay** (review finding 3): an invoice posted before RT-377 has
``ignore_pricing_rule`` = 0 (the builder never set it and ERPNext's default is 0). With
``legacy_allowed`` (replays only) such an invoice is held to the fields valid at any age (Item, qty,
UOM, rate, amount, warehouse, currency, customer, no tax row, grand total) and NOT to list price,
discount, margin or company, which Retail Tower did not send then. A fresh document must carry the flag.

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
from itertools import chain

from .contracts import PostingWorkItem

PREFIX = "representation:"

# Expected fields a caller may exempt (they are then not compared). Commercial values never are.
EXEMPTABLE = frozenset({"warehouse", "uom", "customer", "company"})

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
	"""The commercial representation Retail Tower sent. ``customer`` / ``company`` ``None`` = exempt."""

	currency: str
	customer: str | None
	company: str | None
	lines: tuple[ExpectedLine, ...]


@dataclass(frozen=True)
class PersistedInvoice:
	"""The commercial representation ERPNext holds: the document fields and one mapping per item row."""

	currency: object
	customer: object
	company: object
	ignore_pricing_rule: object
	grand_total: object
	tax_rows: int
	lines: tuple[Mapping[str, object], ...]


def expected_from_payload(payload: Mapping[str, object], *, exempt: frozenset[str] = frozenset()) -> ExpectedInvoice:
	"""The representation in a built Sales Invoice payload; ``exempt`` fields are not compared.

	Raises :class:`RepresentationMismatch` when the payload carries no company (the site default is
	not configured, or the original invoice has none: fail closed) and :class:`ValueError` for an
	exemption outside :data:`EXEMPTABLE`.
	"""
	unknown = exempt - EXEMPTABLE
	if unknown:
		raise ValueError(f"cannot exempt {sorted(unknown)} from the representation check")
	company = None if "company" in exempt else payload.get("company")
	if not company and "company" not in exempt:
		raise RepresentationMismatch(f"{PREFIX} the expected company (site default) is not configured")
	return ExpectedInvoice(
		currency=str(payload["currency"]),
		customer=None if "customer" in exempt else str(payload["customer"]),
		company=str(company) if company else None,
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
		ignore_pricing_rule=doc.get("ignore_pricing_rule"),
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
	warehouses). The frozen resolution carries no UOM, customer or company, and today's maps and site
	default may differ from the ones the invoice was posted with, so a replay relies on the
	insert-time check for them. A credit note is still held to its ORIGINAL invoice's company, which
	cannot change after submit.
	"""
	if work_item.kind == "reversal":
		return frozenset({"warehouse", "uom", "customer"})
	return frozenset({"warehouse", "uom", "customer", "company"})


def check_representation(
	expected: ExpectedInvoice, persisted: PersistedInvoice, *, legacy_allowed: bool = False
) -> str | None:
	"""``None`` when ERPNext holds exactly ``expected``, else the first mismatch reason.

	``legacy_allowed`` (replays only): an invoice without ``ignore_pricing_rule`` predates RT-377 and is
	held to the reduced field set. Otherwise a document without the flag is a mismatch.
	"""
	full = _number(persisted.ignore_pricing_rule or 0) == 1
	if not full and not legacy_allowed:
		return f"{PREFIX} document ignore_pricing_rule {persisted.ignore_pricing_rule!r} is not set (RT-377)"
	reason = _document_mismatch(expected, persisted, full) or _lines_mismatch(
		expected.lines, persisted.lines, full
	)
	return f"{PREFIX} {reason}" if reason else None


def _document_mismatch(expected: ExpectedInvoice, persisted: PersistedInvoice, full: bool) -> str | None:
	for field in ("currency", "customer", "company") if full else ("currency", "customer"):
		want = getattr(expected, field)
		if want is not None and _text(getattr(persisted, field)) != want:
			return f"document {field} {getattr(persisted, field)!r} is not the sent {want!r}"
	if persisted.tax_rows:
		return f"document carries {persisted.tax_rows} tax row(s); Retail Tower sent none (tax deferred, D4)"
	line_total = sum((_number(line.amount) for line in expected.lines), Decimal(0))
	if _number(persisted.grand_total) != line_total:
		return f"document grand_total {persisted.grand_total!r} is not the line total {line_total}"
	return None


def _lines_mismatch(
	expected: Sequence[ExpectedLine], rows: Sequence[Mapping[str, object]], full: bool
) -> str | None:
	if len(rows) != len(expected):
		return f"ERPNext holds {len(rows)} item row(s); Retail Tower sent {len(expected)} line(s)"
	for pos, (line, row) in enumerate(zip(expected, rows, strict=True), start=1):
		reason = _line_mismatch(line, row, full)
		if reason:
			return f"line {pos} {reason}"
	return None


def _line_mismatch(line: ExpectedLine, row: Mapping[str, object], full: bool) -> str | None:
	"""``full`` = RT-377+ invoice: also no ERP-derived list price, discount or margin."""
	sent = _sent_values_mismatch(line, row)
	if sent or not full:
		return sent
	return _adjustment_mismatch(line, row)


def _sent_values_mismatch(line: ExpectedLine, row: Mapping[str, object]) -> str | None:
	"""The first sent text or number ERPNext does not hold (lazy: stops at the first mismatch)."""
	text = (_text_mismatch(field, getattr(line, field), row) for field in _LINE_TEXT_FIELDS)
	numbers = (_number_mismatch(field, getattr(line, field), row) for field in _LINE_NUMBER_FIELDS)
	return next(filter(None, chain(text, numbers)), None)


def _text_mismatch(field: str, want: str | None, row: Mapping[str, object]) -> str | None:
	"""``want=None`` is an exempt field."""
	if want is None or _text(row.get(field)) == want:
		return None
	return f"{field} {row.get(field)!r} is not the sent {want!r}"


def _number_mismatch(field: str, want: str, row: Mapping[str, object]) -> str | None:
	if _number(row.get(field)) == _number(want):
		return None
	return f"{field} {row.get(field)!r} is not the sent {want}"


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
