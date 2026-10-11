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
  - sets ``disable_rounded_total = 1`` (RT-80): the site default rounds a fractional total to the
    whole unit (10.49 EGP -> AR 10.00 + 0.49 Round Off), so AR would no longer equal ``posTotal``.
    The reversal builder composes on this one, so credit notes inherit it.
  - settles a tender-bearing sale on the invoice itself (RT-78, RT-10 D3(b), amendment
    011-DR-POSTING-A1): ``is_pos = 1`` plus one ``payments`` row per tender (Mode of Payment from
    the injected tender map), so the invoice is Paid in the same exactly-once submit. A
    tender-unknown sale builds exactly as before: an unpaid invoice (R1 interim mode, RT-10 D8).

This module imports NO frappe — it builds a plain dict the bench glue submits. There is never a
Payment Entry here: a paid sale carries its own payments; a tender-unknown one stays outstanding AR.
"""

from __future__ import annotations

from collections.abc import Callable

from .contracts import PostingWorkItem
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
            # RT-377 / D6: the list price IS the Retail Tower price, so ERPNext derives no discount.
            "price_list_rate": line.unit_price,
            "amount": line.line_amount,
            "warehouse": warehouse["name"],
            "currency": line.currency_code,
        }
        if line.line_ref is not None:
            # RT-16 / RT-14 D6: the sale-line identity a later partial return is matched on.
            item["rt_line_ref"] = line.line_ref
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
    # businessDate drives the fiscal period — never the connector's post-time (T033). RT-49:
    # set_posting_time=1, or ERPNext overwrites posting_date/posting_time with "now".
    stamp = posting_stamp or stamp_for(sale.occurred_at, sale.business_date, UTC_CLOCK)
    return apply_stamp(doc, stamp)
