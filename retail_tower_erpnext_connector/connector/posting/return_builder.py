# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Partial-return work-item → return Sales Invoice (credit note) builder (RT-16).

A 012 ``reversalKind: return`` (RT-14 D1) carries only the returned lines (``returnLines``, priced by
Backend-Core by the cumulative-difference rule) and how the cash was paid out (``refundTenders``,
RT-14 D3, cash only). It posts a return Sales Invoice that:

  - carries ONLY the returned lines, negated (qty and amount; the rate stays positive), each tagged
	with its ``rt_line_ref`` so the glue links it to the ORIGINAL invoice row by identity (RT-14 D6
	custom field) and takes that row's warehouse;
  - pays the refund out from ``refundTenders`` through the CURRENT tender map (RT-10 D6): it is a new
	cash payout, not a mirror of the sale's tenders, so it applies even to a tender-unknown sale;
  - is unrounded (RT-80) and stock-moving (the glue mirrors the original's ``update_stock``);
  - posts on the return's own business date at its recorded time (RT-63, :func:`reversal_stamp_source`).

It COMPOSES on :func:`builder.build_sales_invoice` — the returned lines become the lines of a
synthetic sale snapshot and the refund tenders its tenders — so money validation (FR-009), the
tender == line total check (amendment 4a) and the payments rows come from the single forward money
path. The result is then negated. Pure: no frappe, no ERPNext call.

A returned line whose ``lineAmount`` is below ``unitPrice x quantity`` (an Offer Engine reward unit,
RT-286, or an RT-857 manual discount) posts the same visible-discount shape the sale does
(``price_list_rate`` = unitPrice, a per-unit ``discount_amount``, the net ``rate``; RT-1340 F3, owner
OD-11). ERPNext recomputes ``amount = rate x qty``, so the net per-unit rate must be exact at the
currency's minor unit. When ``lineAmount / quantity`` is not, a whole-number quantity is split into
quantity-1 rows whose rates differ by one minor unit (all tagged with the same ``rt_line_ref``), so
the credit note still totals exactly ``lineAmount``. Anything else not exactly representable is
refused by the sale builder (:class:`builder.LineDiscountNotPostable`).

OD-11 (feed 1.13.0-draft, RT-1367; Backend-Core allocates both amounts, the connector never works them
out):

  - option A, ``promotionClawbackAmount``: the line posts at ``lineAmount - promotionClawbackAmount``
	through the same visible-discount and split path, so the per-unit discount grows by the clawed-back
	part (bench A1: 300.00 with 80.00 clawed back posts at 220.00; A2: 3 x 100.00 with 10.01 posts
	96.66 + 96.66 + 96.67);
  - Hybrid, ``promotionGiveBackAmount``: the reward line posts at its net and the give-back posts as an
	extra quantity-1 row of the configured non-stock give-back Item (:class:`GiveBackItem`), stamped
	``<lineRef>#give-back`` so the glue leaves it unlinked and the replay check counts it (bench S3
	hybrid: 120.00 + 80.00 = 200.00). ERPNext refuses a credit-note rate above the original row's, so
	the give-back cannot be folded into the reward row.

Either amount must be a whole number of the currency's minor units; the clawback may not exceed
``lineAmount`` and the give-back may not exceed the discount of the units returned now
(``unitPrice x quantity - lineAmount``), else :class:`PromotionAmountNotPostable`. A give-back with no
configured Item raises :class:`GiveBackItemNotConfigured` (the glue decides the outcome).

Fail closed (all ``validation``): a return with no ``refundTenders`` (the feed INVARIANT says every
return carries them — an outstanding credit note would hide cash that left the drawer), a refund
total that differs from the returned total, an unmapped refund method, and a returned line whose
net cannot be posted exactly as a visible discount (above gross, negative, off the minor unit, or
with no exact per-unit rate even after a split).

Tax: like the forward builder, no tax rows are posted (Egyptian VAT is 0 today), so a return line
with a non-zero ``taxAmount`` is rejected outright (:class:`ReturnTaxNotPosted`) — never posted with
its tax silently dropped, whatever the refund covers (Codex P2, PR #50).
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, localcontext

from .builder import _EXACT_DIGITS, _minor_unit, build_sales_invoice
from .contracts import (
	GIVE_BACK_REF_SUFFIX,
	ErpnextItemRef,
	PostingWorkItem,
	ReturnLine,
	SaleLine,
	SaleTender,
	is_give_back_ref,
)
from .idempotency import provenance_id
from .posting_time import UTC_CLOCK, PostingStamp, apply_stamp, stamp_for
from .reversal_builder import _negate, reversal_stamp_source


class MissingRefundTenders(Exception):
	"""A ``return`` without ``refundTenders`` (feed INVARIANT breach) — rejected as validation."""

	def __init__(self) -> None:
		super().__init__(
			"return carries no refundTenders — rejected as validation (every return records how the "
			"cash was paid out; posting an outstanding credit note would hide it, RT-10 D6 / RT-14 D3)"
		)


class ReturnTaxNotPosted(Exception):
	"""A returned line carries a non-zero ``taxAmount`` while no tax rows are posted — validation."""

	def __init__(self, line_ref: str, tax_amount: str) -> None:
		super().__init__(
			f"return line {line_ref}: taxAmount {tax_amount} cannot be posted (no tax rows while VAT is 0) "
			"— rejected as validation rather than posted with the tax silently dropped"
		)


class PromotionAmountNotPostable(Exception):
	"""An OD-11 promotion amount cannot be posted exactly (above its bound or off the minor unit) — validation."""

	def __init__(self, line_ref: str, field: str, detail: str) -> None:
		super().__init__(f"return line {line_ref}: {field} {detail} — rejected as validation")


class GiveBackItemNotConfigured(Exception):
	"""A return carries a promotion give-back but no give-back Item is configured (OD-11 Hybrid)."""

	def __init__(self, reason: str = "is not configured") -> None:
		super().__init__(
			f"return carries a promotionGiveBackAmount but the promotion give-back Item {reason}"
		)


@dataclass(frozen=True)
class GiveBackItem:
	"""The non-stock ERPNext Item a promotion give-back row posts on, with its stock UOM (OD-11 Hybrid)."""

	item_code: str
	uom: str


@dataclass(frozen=True)
class ReturnResolvers:
	"""The config resolvers a return is built with (mirroring :func:`builder.build_sales_invoice`).

	``mode_of_payment_for`` is the RT-10 D5 tender map, applied to the refund tenders.
	``give_back_item`` returns the configured :class:`GiveBackItem`; it is consulted only when a returned
	line carries a give-back, and ``None`` means none is configured.
	"""

	uom_for: Callable[[str], str]
	warehouse_for: Callable[[str], dict]
	customer_for: Callable[[str], str]
	mode_of_payment_for: Callable[[str], str]
	give_back_item: Callable[[], GiveBackItem] | None = None


def build_return_invoice(
	work_item: PostingWorkItem,
	resolvers: ReturnResolvers,
	*,
	posting_stamp: PostingStamp | None = None,
	company: str | None = None,
) -> dict:
	"""Build the return Sales-Invoice payload for one ``reversalKind: return`` work item.

	``posting_stamp`` is the glue's site-clock stamp; without one the return's own ``recordedAt`` /
	``businessDate`` are used in UTC.
	"""
	ref = _return_ref(work_item)
	give_back = _give_back_item(ref, resolvers)
	returned_sale = dataclasses.replace(
		work_item.sale,
		lines=tuple(
			piece for rl in ref.return_lines for piece in _credit_note_lines(work_item, rl, give_back)
		),
		tenders=tuple(SaleTender(method=t.method, amount=t.amount) for t in ref.refund_tenders),
	)
	doc = build_sales_invoice(
		dataclasses.replace(work_item, sale=returned_sale),
		uom_for=resolvers.uom_for,
		warehouse_for=resolvers.warehouse_for,
		customer_for=resolvers.customer_for,
		mode_of_payment_for=resolvers.mode_of_payment_for,
		company=company,  # the glue replaces it with the original invoice's company (RT-378)
	)

	doc["is_return"] = 1
	doc["update_stock"] = 1  # the glue mirrors the original invoice's update_stock
	_negate_rows(doc)
	if give_back is not None:
		for item in doc["items"]:
			if is_give_back_ref(item.get("rt_line_ref")):
				item["uom"] = give_back.uom  # the give-back Item's own stock UOM, not the reward line's
				# ERPNext's duplicate check for non-stock rows compares item_code + description: two give-backs
				# on one credit note must not look alike (CodeRabbit, PR #584).
				item["description"] = _give_back_description(item["rt_line_ref"])

	# Provenance is the return's own work item (Connector #28), never the original sale's id.
	doc["rt_source_system"] = work_item.source_system
	doc["rt_external_id"] = provenance_id(work_item)

	stamp = posting_stamp or stamp_for(*reversal_stamp_source(work_item), UTC_CLOCK)
	return apply_stamp(doc, stamp)


def _return_ref(work_item: PostingWorkItem):
	"""The work item's ``return`` reference; fail closed on another kind or no refund tenders."""
	ref = work_item.reversal_of
	if ref is None or ref.reversal_kind != "return":
		raise ValueError("build_return_invoice expects a reversal work-item of reversalKind 'return'")
	if not ref.refund_tenders:
		raise MissingRefundTenders()
	return ref


def _negate_rows(doc: dict) -> None:
	"""Return semantics: negate every item qty/amount and payment amount (string-only, no float)."""
	for item in doc["items"]:
		item["qty"] = _negate(item["qty"])
		item["amount"] = _negate(item["amount"])
	for payment in doc["payments"]:
		payment["amount"] = _negate(payment["amount"])


def expected_row_counts(work_item: PostingWorkItem) -> Counter[str]:
	"""How many credit-note rows each returned ``lineRef`` is posted as (1, or the split count; RT-1340 F3).

	A give-back adds one row stamped ``<lineRef>#give-back``. The replay identity check
	(:mod:`resolution_check`) holds an existing credit note to exactly these counts, so a line posted
	twice, or a split with the wrong number of rows, is still a mismatch.
	"""
	ref = _return_ref(work_item)
	counts = Counter(piece.line_ref for rl in ref.return_lines for piece in _returned_lines(work_item, rl))
	counts.update(rl.line_ref + GIVE_BACK_REF_SUFFIX for rl in ref.return_lines if _give_back(rl) is not None)
	return counts


def _give_back_item(ref, resolvers: ReturnResolvers) -> GiveBackItem | None:
	"""The configured give-back Item when any returned line carries a give-back; fail closed when none is."""
	if not any(_give_back(rl) is not None for rl in ref.return_lines):
		return None
	item = resolvers.give_back_item() if resolvers.give_back_item is not None else None
	if item is None:
		raise GiveBackItemNotConfigured()
	return item


def _give_back_description(stamp: str) -> str:
	return f"Promotion give-back for sale line {stamp[: -len(GIVE_BACK_REF_SUFFIX)]}"


def _give_back(returned: ReturnLine) -> Decimal | None:
	"""The line's give-back amount, ``None`` when absent or 0."""
	amount = returned.promotion_give_back_amount
	return None if amount is None or Decimal(amount) == 0 else Decimal(amount)


def _credit_note_lines(
	work_item: PostingWorkItem, returned: ReturnLine, give_back: GiveBackItem | None
) -> list[SaleLine]:
	"""The returned line's row(s) and, for a give-back (OD-11 Hybrid), its quantity-1 give-back row."""
	pieces = _returned_lines(work_item, returned)
	amount = _give_back(returned)
	if amount is None:
		return pieces
	sale_line = _sale_line(work_item, returned)
	stamp = returned.line_ref + GIVE_BACK_REF_SUFFIX
	if len(stamp) > _RT_LINE_REF_LENGTH:
		raise PromotionAmountNotPostable(
			returned.line_ref,
			"promotionGiveBackAmount",
			f"cannot be stamped: {stamp!r} is over {_RT_LINE_REF_LENGTH} characters",
		)
	_check_promotion_amount(
		sale_line, returned, "promotionGiveBackAmount", amount, _returned_discount(sale_line, returned)
	)
	text = f"{amount:f}"
	row = dataclasses.replace(
		sale_line,
		line_name="Promotion give-back",
		unit_price=text,
		quantity="1",
		line_amount=text,
		tax_amount=None,
		erpnext_item_ref=ErpnextItemRef(doctype="Item", name=give_back.item_code),
		line_ref=stamp,
		erpnext_batch_ref=None,
		tracking_mode=None,
	)
	return [*pieces, row]


def _sale_line(work_item: PostingWorkItem, returned: ReturnLine) -> SaleLine:
	return next(line for line in work_item.sale.lines if line.line_ref == returned.line_ref)


def _returned_discount(sale_line: SaleLine, returned: ReturnLine) -> Decimal:
	"""The discount of the units returned now: ``unitPrice x quantity - lineAmount`` (never below 0)."""
	with localcontext() as ctx:
		ctx.prec = _EXACT_DIGITS
		gross = Decimal(sale_line.unit_price) * Decimal(returned.quantity)
		return max(Decimal(0), gross - Decimal(returned.line_amount))


def _check_promotion_amount(
	sale_line: SaleLine, returned: ReturnLine, field: str, amount: Decimal, bound: Decimal
) -> None:
	"""Fail closed unless ``amount`` is whole minor units of the line currency and at most ``bound``."""
	quantum = _minor_unit(sale_line)
	if amount != amount.quantize(quantum, rounding="ROUND_DOWN"):
		raise PromotionAmountNotPostable(
			returned.line_ref, field, f"{amount} is finer than the currency's minor unit"
		)
	if amount > bound:
		raise PromotionAmountNotPostable(returned.line_ref, field, f"{amount} is above its bound {bound}")


# A whole-number quantity above this is not split into quantity-1 rows (a bound on the credit note's size);
# such a line is refused by the sale builder when its net has no exact per-unit rate.
_MAX_SPLIT_ROWS = 100

# The length of the Sales Invoice Item ``rt_line_ref`` custom field (fixtures/custom_field.json).
_RT_LINE_REF_LENGTH = 64


def _returned_lines(work_item: PostingWorkItem, returned: ReturnLine) -> list[SaleLine]:
	"""The sale line(s) ``returned`` points at, re-quantified to the returned quantity and net amount.

	One row normally. A discounted line whose net has no exact per-unit rate is split into quantity-1
	rows (:func:`_split_amounts`), each keeping the original ``line_ref`` so the glue links every row to
	the same original invoice row (and ERPNext's cumulative over-return cap applies to the sum).
	"""
	if returned.tax_amount is not None and Decimal(returned.tax_amount) != 0:
		raise ReturnTaxNotPosted(returned.line_ref, returned.tax_amount)
	sale_line = _sale_line(work_item, returned)
	whole = dataclasses.replace(
		sale_line,
		quantity=returned.quantity,
		line_amount=_net_after_clawback(sale_line, returned),
		tax_amount=returned.tax_amount,
	)
	amounts = _split_amounts(whole)
	if amounts is None:
		return [whole]
	return [dataclasses.replace(whole, quantity="1", line_amount=f"{amount:f}") for amount in amounts]


def _net_after_clawback(sale_line: SaleLine, returned: ReturnLine) -> str:
	"""``lineAmount - promotionClawbackAmount`` (OD-11 option A); ``lineAmount`` unchanged without a clawback."""
	raw = returned.promotion_clawback_amount
	if raw is None or Decimal(raw) == 0:
		return returned.line_amount
	clawback, line_amount = Decimal(raw), Decimal(returned.line_amount)
	_check_promotion_amount(sale_line, returned, "promotionClawbackAmount", clawback, line_amount)
	with localcontext() as ctx:
		ctx.prec = _EXACT_DIGITS
		return f"{line_amount - clawback:f}"


def _split_amounts(line: SaleLine) -> list[Decimal] | None:
	"""Per-unit net amounts (summing exactly to ``line.line_amount``) when the line must be split, else ``None``.

	Split only a discounted line with a whole-number quantity whose net is a whole number of minor units
	but does not divide by the quantity. Everything else (undiscounted, exactly divisible, fractional
	quantity, surcharge, off-minor-unit) is left whole for :func:`builder._line_discount` to post or refuse.
	"""
	with localcontext() as ctx:
		ctx.prec = _EXACT_DIGITS
		quantity, net = Decimal(line.quantity), Decimal(line.line_amount)
		gross = Decimal(line.unit_price) * quantity
		if not 0 <= net < gross or quantity != quantity.to_integral_value():
			return None
		count = int(quantity)
		if count < 2 or count > _MAX_SPLIT_ROWS:
			return None
		quantum = _minor_unit(line)
		if net != net.quantize(quantum):
			return None
		base = (net / count).quantize(quantum, rounding="ROUND_FLOOR")
		extra = int((net - base * count) / quantum)  # rows that take one more minor unit
		if extra == 0:
			return None  # exactly divisible: one row
		return [base] * (count - extra) + [base + quantum] * extra
