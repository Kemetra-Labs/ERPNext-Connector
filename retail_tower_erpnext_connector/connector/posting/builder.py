# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Work-item → ERPNext Sales-Invoice payload builder (T030/T033/T034).

A pure dict-transform: given a :class:`PostingWorkItem`, produce the ERPNext Sales-Invoice
document payload the connector will submit. It:

  - **applies** each line's pre-resolved ``erpnextItemRef`` as the ``item_code`` — there is
    deliberately NO item-resolution hook (rider R2 / FR-001); the only injected callables are
    a UOM resolver (FR-008) and a warehouse resolver (FR-010, applying the DP2-pre-resolved id);
  - keeps every monetary value an **exact-decimal string** (never float — FR-009);
  - carries ``businessDate`` → ``posting_date`` (never the connector's post-time — T033) with
    ``set_posting_time = 1`` so ERPNext keeps it, and ``posting_time`` from ``occurredAt``
    (RT-49 rule, ``posting_time.stamp_for``; ERPNext would otherwise overwrite both with "now");
  - addresses everything generically (``item_code`` / ``Warehouse`` name — FR-002, Principle II);
  - posts with ``update_stock=1`` (RT-48, owner decision RT-47 D1): the Sales Invoice IS the
    stock-moving document. ERPNext writes the Stock Ledger Entries inside the SAME submit, so the
    existing SI idempotency (Posting Log + ``unique_rt_si_provenance``) is also the stock
    exactly-once guarantee. No Delivery Note.
  - suppresses ERPNext repricing (RT-377, Pricing 657/658, RT-283 D6): ``ignore_pricing_rule = 1`` on
    the document, and each line's ``price_list_rate`` equal to its ``rate``. RT-376 proved an enabled
    selling Pricing Rule otherwise rewrote 9.50 to 9.00 on submit, and that without ``price_list_rate``
    ERPNext fills it from its own price list and records an invented discount or margin. Per document
    only: the connector never changes Pricing Rules or Selling Settings (the site may serve ERP sales).
  - posts a per-line manual discount as a VISIBLE discount (RT-857, owner decision OD-28-13): the feed
    carries only ``unitPrice``, ``quantity`` and the NET ``lineAmount``, so the discount is derived as
    ``unitPrice x quantity - lineAmount`` (no kind or reason reaches ERPNext). A discounted line carries
    ``price_list_rate = unitPrice``, a per-unit ``discount_amount`` and ``rate`` = the net per-unit
    price; ``amount`` stays ``lineAmount``. ERPNext v16 keeps that rate (``calculate_item_rate``: a
    rate equal to ``price_list_rate - discount_amount`` exits early) and recomputes
    ``amount = flt(rate x qty, precision)``, so the net per-unit price must be exact at the currency's
    minor unit with ``rate x qty == lineAmount``. When it is not (e.g. 10.00 net over qty 3), the line
    is refused (:class:`LineDiscountNotPostable`, ``validation``): never a total off by one minor unit.
    An undiscounted line (``lineAmount == unitPrice x quantity``) builds exactly as before.
  - sets ``disable_rounded_total = 1`` (RT-80): the site default rounds a fractional total to the
    whole unit (10.49 EGP -> AR 10.00 + 0.49 Round Off), so AR would no longer equal ``posTotal``.
    The reversal builder composes on this one, so credit notes inherit it.
  - settles a tender-bearing sale on the invoice itself (RT-78, RT-10 D3(b), amendment
    011-DR-POSTING-A1): ``is_pos = 1`` plus one ``payments`` row per tender (Mode of Payment from
    the injected tender map), so the invoice is Paid in the same exactly-once submit. A
    tender-unknown sale builds exactly as before: an unpaid invoice (R1 interim mode, RT-10 D8).
  - posts a batch line with its frozen ``erpnextBatchRef`` (RT-452, G7-11): row ``batch_no`` plus
    ``use_serial_batch_fields = 1``, so ERPNext creates the Serial and Batch Bundle for exactly that
    Batch. Only on a ``sale_post``: the reversal builders compose on this one and a credit note row
    carries no Batch. Whether the Item may carry it is ``batch_policy``'s decision, before insert.

This module imports NO frappe — it builds a plain dict the bench glue submits. There is never a
Payment Entry here: a paid sale carries its own payments; a tender-unknown one stays outstanding AR.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal, localcontext

from .compatibility_profile import ISO_MINOR_UNITS
from .contracts import PostingWorkItem, SaleLine
from .posting_time import UTC_CLOCK, PostingStamp, apply_stamp, stamp_for
from .tender import build_payments


class UnmappedUnit(Exception):
    """A DP2 free-text ``unit`` has no entry in the signed unit→UOM map (FR-008).

    The caller maps this to a ``permanently_rejected`` / ``validation`` outcome — never a
    silent default (Principle VI). Raised by the injected UOM resolver.
    """

    def __init__(self, unit: str) -> None:
        super().__init__(f"unit {unit!r} has no ERPNext UOM mapping (fail-closed → validation)")
        self.unit = unit


class LineDiscountNotPostable(Exception):
    """A discounted line cannot be posted exactly as a visible per-unit discount (RT-857, OD-28-13).

    The caller maps this to a ``permanently_rejected`` / ``validation`` outcome (fail closed: the sale
    goes to Backend-Core's review path, never posted at gross or at a rounded total). ``code`` says why:

    * ``discount_currency_unknown``: the line currency has no known ISO 4217 minor unit;
    * ``discount_off_minor_unit``: ``unitPrice`` or ``lineAmount`` is finer than that minor unit;
    * ``discount_above_gross``: ``lineAmount`` is negative (a discount above 100%);
    * ``discount_negative``: ``lineAmount`` is above ``unitPrice x quantity`` (a surcharge);
    * ``discount_not_divisible``: no net per-unit price at the minor unit gives ``rate x qty ==
      lineAmount``, so ERPNext would post a different line amount.
    """

    def __init__(self, code: str, line: SaleLine, detail: str) -> None:
        super().__init__(
            f"line discount not postable [{code}] on line {line.line_ref or line.line_name!r}: {detail} "
            "— rejected as validation (OD-28-13: fail closed, never a total off by a minor unit)"
        )
        self.code = code


# Digits for the exact discount arithmetic: a 012 amount (<= 15 + 4 digits) times a quantity
# (<= 15 + 6 digits) never needs more, so no product below is ever rounded by the context.
_EXACT_DIGITS = 60


def _line_discount(line: SaleLine) -> tuple[str, str] | None:
    """``(rate, discount_amount)`` per unit for a discounted line; ``None`` for an undiscounted one.

    The minor unit is the line currency's ISO 4217 exponent (``compatibility_profile.ISO_MINOR_UNITS``,
    the table the RT-379 profile holds ERPNext's currency precision to: posting is paused while the
    site precision is below it). A value exact at the minor unit is therefore held exactly by ERPNext.
    Exact :class:`Decimal` only, never float (FR-009).
    """
    with localcontext() as ctx:
        ctx.prec = _EXACT_DIGITS
        unit_price, quantity, line_amount = (
            Decimal(line.unit_price),
            Decimal(line.quantity),
            Decimal(line.line_amount),
        )
        gross = unit_price * quantity
        if line_amount == gross:
            return None  # undiscounted: posted exactly as before RT-857
        quantum = _minor_unit(line)
        for field, value in (("unitPrice", unit_price), ("lineAmount", line_amount)):
            if value != value.quantize(quantum):
                raise LineDiscountNotPostable(
                    "discount_off_minor_unit", line, f"{field} {value} is finer than the {line.currency_code} minor unit"
                )
        if line_amount < 0:
            raise LineDiscountNotPostable(
                "discount_above_gross", line, f"lineAmount {line.line_amount} is negative (discount above 100%)"
            )
        if line_amount > gross:
            raise LineDiscountNotPostable(
                "discount_negative",
                line,
                f"lineAmount {line.line_amount} is above unitPrice {line.unit_price} x quantity {line.quantity}",
            )
        # Here gross > lineAmount >= 0, so quantity > 0. A rate is exact iff rate x qty == lineAmount.
        rate = (line_amount / quantity).quantize(quantum)
        if rate * quantity != line_amount:
            raise LineDiscountNotPostable(
                "discount_not_divisible",
                line,
                f"net lineAmount {line.line_amount} over quantity {line.quantity} has no exact per-unit price "
                f"at the {line.currency_code} minor unit (ERPNext would post {rate} x {line.quantity} = "
                f"{rate * quantity})",
            )
        return f"{rate:f}", f"{(unit_price - rate).quantize(quantum):f}"


def _minor_unit(line: SaleLine) -> Decimal:
    """``10 ** -minor`` for the line currency; fail closed when its minor unit is unknown."""
    minor = ISO_MINOR_UNITS.get(line.currency_code)
    if minor is None:
        raise LineDiscountNotPostable(
            "discount_currency_unknown", line, f"currency {line.currency_code!r} has no known ISO 4217 minor unit"
        )
    return Decimal(1).scaleb(-minor)


def _represent_line_discounts(items: list[dict], lines: Sequence[SaleLine]) -> None:
    """RT-857 / OD-28-13 (i): put each discounted line's visible per-unit discount on its item row.

    ``price_list_rate`` stays ``unitPrice`` and ``amount`` stays ``lineAmount``; ``rate`` becomes the net
    per-unit price and ``discount_amount`` the per-unit discount. Undiscounted rows are not touched.
    """
    for item, line in zip(items, lines, strict=True):
        discount = _line_discount(line)
        if discount is not None:
            item["rate"], item["discount_amount"] = discount


def build_sales_invoice(
    work_item: PostingWorkItem,
    *,
    uom_for: Callable[[str], str],
    warehouse_for: Callable[[str], dict],
    customer_for: Callable[[str], str],
    posting_stamp: PostingStamp | None = None,
    mode_of_payment_for: Callable[[str], str] | None = None,
    company: str | None = None,
) -> dict:
    """Build the ERPNext Sales-Invoice payload for one ``sale_post`` work-item.

    ``uom_for(unit) -> erpnext_uom`` resolves the signed unit→UOM map (raises
    :class:`UnmappedUnit` on a miss). ``warehouse_for(store_id) -> {doctype, name}`` returns
    the DP2-pre-resolved warehouse identity (rider R5; never guessed by this builder).
    ``customer_for(store_id) -> customer`` resolves the operator-configured store→Customer map
    (F-009; raises :class:`uom.UnmappedStore` on a miss — the builder NEVER fabricates a
    customer, which would hide a config gap; Principle VI).
    ``posting_stamp`` is the RT-49 posting date/time; the glue computes it with the ERPNext site
    clock. Without one, it is derived from ``occurredAt`` in UTC (``posting_time.UTC_CLOCK``).
    ``mode_of_payment_for(method) -> Mode of Payment`` resolves the RT-10 D5 tender map (raises
    :class:`tender.UnmappedTender` on a miss). It is only consulted when the sale carries tenders;
    a tender-bearing sale without it fails closed rather than posting unpaid.
    ``company`` (RT-378) is set explicitly: the glue passes the site default, the value the
    representation check expects. Without one the document carries no ``company`` key.
    """
    sale = work_item.sale
    warehouse = warehouse_for(sale.store_id)
    # F-009: the 012 work-item carries no customer identity — only Sale.store_id. The operator
    # maps each store to its ERPNext Customer (config, injected) exactly as for the warehouse.
    customer = customer_for(sale.store_id)

    items = []
    for line in sale.lines:
        item = {
            # Apply the pre-resolved Item identity — no lookup (rider R2).
            "item_code": line.erpnext_item_ref.name,
            "qty": line.quantity,
            "uom": uom_for(line.unit),
            "rate": line.unit_price,
            # RT-377 / D6: the list price IS the Retail Tower unit price, so ERPNext derives no discount
            # of its own. A discounted line gets its net rate and discount_amount below (RT-857).
            "price_list_rate": line.unit_price,
            "amount": line.line_amount,
            "warehouse": warehouse["name"],
            "currency": line.currency_code,
        }
        if line.line_ref is not None:
            # RT-16 / RT-14 D6: the sale-line identity a later partial return is matched on.
            item["rt_line_ref"] = line.line_ref
        if line.erpnext_batch_ref is not None and work_item.kind == "sale_post":
            # RT-452 / G7-11: the supplied Batch, set explicitly; ERPNext builds the row's Serial and
            # Batch Bundle from it and never auto-picks. A credit note carries no Batch of its own:
            # ERPNext rebuilds it from the original row (``sales_invoice_item``, G7-10).
            item["batch_no"] = line.erpnext_batch_ref
            item["use_serial_batch_fields"] = 1
        items.append(item)

    # F-009 CLOSED: the customer is resolved from the operator-configured store→Customer map
    # (above), never fabricated. The 012 work-item carries no customer; the operator owns the
    # mapping, identical to the UOM and warehouse maps. `company` is the explicit site default the
    # glue passes (RT-378); a multi-company store→company mapping is a future extension if needed.
    doc = {
        "doctype": "Sales Invoice",
        "customer": customer,
        "currency": sale.currency_code,
        # Provenance for idempotency + audit (O-3); carried on custom fields.
        "rt_source_system": sale.source_system,
        "rt_external_id": sale.external_id,
        "rt_sale_ref": sale.sale_ref,
        # RT-48 / RT-47 D1: the sale moves stock out of each line's mapped store warehouse.
        "update_stock": 1,
        # RT-80: post the exact POS total. ERPNext otherwise applies the site default
        # (Global Defaults disable_rounded_total=0) and rounds 10.49 to 10.00 + Round Off.
        "disable_rounded_total": 1,
        # RT-377 / Pricing 657: ERPNext Pricing Rules must not alter a finalized Retail Tower sale.
        "ignore_pricing_rule": 1,
        "items": items,
        **({"company": company} if company else {}),  # RT-378: explicit, never ERPNext's user default
    }

    # RT-78 / RT-10 D3(b): a tender-bearing sale is settled on this invoice (no Payment Entry).
    payments = build_payments(
        sale.tenders,
        line_amounts=[item["amount"] for item in items],
        mode_of_payment_for=mode_of_payment_for,
    )
    if payments:
        doc["is_pos"] = 1
        doc["payments"] = payments

    # FR-009 enforced in the path (not only by a separately-callable check): every monetary
    # field is an exact-decimal string + ISO-4217, never a float. Local import avoids the
    # builder↔uom import cycle (uom imports UnmappedUnit from this module).
    from .uom import assert_money_conformance

    assert_money_conformance(doc)
    # RT-857: derived from the conformant strings checked above, then the final payload is held to
    # FR-009 again (a discounted row gains a net rate and a discount_amount).
    _represent_line_discounts(items, sale.lines)
    assert_money_conformance(doc)
    # businessDate drives the fiscal period — never the connector's post-time (T033). RT-49:
    # set_posting_time=1, or ERPNext overwrites posting_date/posting_time with "now".
    stamp = posting_stamp or stamp_for(sale.occurred_at, sale.business_date, UTC_CLOCK)
    return apply_stamp(doc, stamp)
