# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Reversal work-item → reversing-document (return Sales Invoice) builder (Arc A S1).

A 012 ``kind=reversal`` work-item posts a **negative-qty return Sales Invoice** — in ERPNext a
credit note IS a Sales Invoice with ``is_return=1`` (CHECKPOINT-1 decision, chosen for symmetric
DP-017 reconciliation against the 1:1 forward SI). This is a pure dict-transform: no frappe, no
ERPNext call — the bench glue submits the dict.

It COMPOSES on :func:`builder.build_sales_invoice`: that builder validates money on the positive
magnitudes once (FR-009; ``uom.assert_money_conformance`` runs there), so there is a single money
path and no duplicated line loop. This builder then:

  - sets ``is_return = 1`` (credit-note discriminator);
  - **negates** each line's ``qty`` and ``amount`` (return semantics); leaves ``rate`` positive
    (ERPNext convention — the sign lives on qty);
  - writes the provenance fields from the #28 discriminator: ``rt_external_id`` =
    ``idempotency.provenance_id(work_item)`` (= ``work_item.work_item_ref`` for a reversal),
    ``rt_source_system`` = ``work_item.source_system`` (THE F-002 RULE — see below);
  - posts ON the reversal's own business day AT its server event time (RT-16 / RT-63, decision
    10348): ``reversalOf.businessDate`` + ``reversalOf.recordedAt`` with ``set_posting_time = 1``, so
    a late void is NOT back-dated into the sale's day (:func:`reversal_stamp_source`). An older
    Backend-Core that sends neither falls back to the RT-49 rule (the sale's ``occurredAt`` on the
    work item's ``businessDate``). The glue then raises the stamp to the original invoice's posting
    timestamp if that is later (``posting_time.raise_to_original``).

THE CRITICAL CORRECTNESS CONSTRAINT (F-002 + Connector #28): the ``unique_rt_si_provenance`` index
spans ALL Sales Invoices via ``rt_external_id``. DP2 emits the ORIGINAL sale's ``externalId`` as the
top-level anchor on a reversal work-item (``work_item.external_id == reversal_of.external_id ==
sale.external_id`` — the original sale's id, NOT per-reversal-distinct). Writing that into
``rt_external_id`` would collide with the original SI's unique key and be FALSELY treated as a
dup-recovery — silently echoing the original invoice with no credit note (#28). The
per-reversal-distinct value that IS on the wire is ``work_item_ref`` (the ack identity), so the
reversing doc carries ``idempotency.provenance_id(work_item)`` (= ``work_item_ref``). This is the
SAME value the replay key uses (``idempotency.key_for``) and the value
``frappe_glue._find_posted_invoice`` queries by — one discriminator, three consumers.

Cardinality: forward sale→SI is 1:1; reversal→original is **N:1** (successive partial returns).
Each reversal work-item has its OWN ``work_item_ref`` / reversal key, so each yields a distinct
``rt_external_id`` even when several share one ``external_id`` (the original sale). Idempotency is
per reversal-request key, reusing the same ``store`` replay primitive the sale_post path uses (in
the glue leg).

``return_against`` is NOT resolved here: mapping ``reversal_of`` → the original SI's ERPNext docname
needs a DB hit, and this builder's injected signature carries no SI-resolver. The pure builder emits
a standalone ``is_return=1`` credit note (ERPNext accepts that without ``return_against``); the
bench-pending ``frappe_glue`` reversal leg resolves the original SI by
``(reversal_of.source_system, reversal_of.external_id)`` and sets ``return_against`` before insert.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

from .builder import build_sales_invoice
from .contracts import PostingWorkItem
from .idempotency import provenance_id
from .posting_time import UTC_CLOCK, PostingStamp, apply_stamp, stamp_for


def _negate(amount: str) -> str:
	"""Flip the sign of an exact-decimal string WITHOUT going through float (FR-009).

	``"2" -> "-2"``, ``"200.00" -> "-200.00"``. Zero (any fractional width) passes through
	unchanged. An ALREADY-NEGATIVE magnitude is a contract violation (reversal lines must carry
	positive magnitudes — the forward builder validated them as such); per the fail-closed
	principle this RAISES rather than silently double-negating into a spurious positive.
	"""
	if amount.startswith("-"):
		raise ValueError(f"cannot negate already-negative magnitude {amount!r}")
	if amount in ("0", "0.0", "0.00", "0.000", "0.0000"):
		return amount
	return f"-{amount}"


def _mode_from_original(method: str) -> None:
	"""A void's Mode of Payment is filled from the ORIGINAL invoice's payment rows in the glue.

	Left ``None`` here on purpose: if that mirror step were ever skipped, ERPNext rejects the row
	(Mode of Payment is mandatory) instead of refunding through a guessed or current mode.
	"""
	return None


def reversal_stamp_source(work_item: PostingWorkItem) -> tuple[str, str]:
	"""``(event time, business date)`` a reversal is stamped from (RT-16 / RT-63, decision 10348).

	The reversal's own ``recordedAt`` + ``businessDate`` when Backend-Core sends both; otherwise the
	RT-49 fallback — the sale's ``occurredAt`` on the work item's ``businessDate``. Shared by the
	builder's default stamp and the glue's site-clock stamp, so both always agree.
	"""
	ref = work_item.reversal_of
	if ref is not None and ref.recorded_at is not None and ref.business_date is not None:
		return ref.recorded_at, ref.business_date
	return work_item.sale.occurred_at, work_item.business_date


def build_reversing_invoice(
	work_item: PostingWorkItem,
	*,
	uom_for: Callable[[str], str],
	warehouse_for: Callable[[str], dict],
	customer_for: Callable[[str], str],
	posting_stamp: PostingStamp | None = None,
	company: str | None = None,
) -> dict:
	"""Build the ERPNext return Sales-Invoice (credit note) payload for one ``reversal`` work-item.

	Resolvers mirror :func:`builder.build_sales_invoice` exactly (UOM / warehouse / customer). The
	money path is the forward builder's (validated once on positive magnitudes); qty/amount are then
	negated. Raises :class:`ValueError` if handed a non-reversal work-item (it would mis-post).
	``posting_stamp`` is the RT-49 posting date/time (glue: site clock); without one it is derived
	from ``sale.occurredAt`` and the reversal's ``business_date`` in UTC.
	RT-78 / RT-10 D6: a ``void`` refunds by mirroring ``sale.tenders`` as NEGATIVE payment amounts;
	a void of a tender-unknown sale stays an outstanding credit note. The rows carry NO Mode of
	Payment here: the glue fills each one from the ORIGINAL invoice's own payment rows
	(``stock_policy.link_void_to_original``), so a tender map changed since the sale can neither
	block nor redirect the refund (Codex P2, PR #48). Any other kind never mirrors the sale's
	tenders (a return pays out its own ``refundTenders`` — RT-16).
	"""
	if work_item.kind != "reversal":
		raise ValueError(f"build_reversing_invoice expects a reversal work-item, got kind {work_item.kind!r}")

	reversal_kind = work_item.reversal_of.reversal_kind if work_item.reversal_of else None
	# RT-78 / RT-10 D6: only a void mirrors the sale's tenders. Strip them for any other kind, so the
	# forward builder never settles (or tender-validates) a refund against the whole sale's payments.
	source = work_item
	if reversal_kind != "void" and work_item.sale.tenders:
		source = dataclasses.replace(work_item, sale=dataclasses.replace(work_item.sale, tenders=()))

	# Compose on the forward builder: this validates money (FR-009) on the POSITIVE magnitudes and
	# resolves customer/currency/warehouse/UOM (and a void's tender modes) identically — one money
	# path, no duplicated loop.
	doc = build_sales_invoice(
		source,
		uom_for=uom_for,
		warehouse_for=warehouse_for,
		customer_for=customer_for,
		mode_of_payment_for=_mode_from_original,
		company=company,  # the glue replaces it with the original invoice's company (RT-378)
	)

	# A credit note IS a Sales Invoice with is_return=1.
	doc["is_return"] = 1

	# RT-48 / RT-47 D5: only a FULL VOID restores stock. A refund is amount-only today while this
	# builder negates EVERY sale line, so a refund with update_stock=1 would restock the full sold
	# quantity — refund stock semantics wait for RT-14/RT-16 (update_stock=0). A void keeps the
	# forward builder's update_stock=1; the glue then MIRRORS the original invoice's update_stock
	# (stock_policy.link_void_to_original) so a legacy update_stock=0 sale never fabricates stock.
	doc["update_stock"] = 1 if reversal_kind == "void" else 0

	# Return semantics: negate qty + amount (the credit note totals are negative); rate stays
	# positive (ERPNext convention — the sign lives on qty). Negation is string-only (no float).
	for item in doc["items"]:
		item["qty"] = _negate(item["qty"])
		item["amount"] = _negate(item["amount"])
	# A void pays the tenders back out: the payments carry the same sign as the credit note.
	for payment in doc.get("payments") or []:
		payment["amount"] = _negate(payment["amount"])

	# THE F-002 + #28 RULE: the reversing doc's provenance is the per-reversal-distinct discriminator
	# (work_item_ref), NOT the original sale's id (which is what reversal_of.* / sale.* / and the
	# top-level work_item.external_id ALL carry on a reversal). Writing the original's id would
	# collide with the original SI's unique slot and silently echo the original invoice (#28).
	# `provenance_id` is the single source of truth shared with the replay key and the dup-recovery
	# lookup (one discriminator, three consumers).
	doc["rt_source_system"] = work_item.source_system
	doc["rt_external_id"] = provenance_id(work_item)

	# RT-16 / RT-63: the credit note posts on the reversal's own day at its own time (RT-49 fallback
	# when absent). The forward builder stamped the sale's day — replace it with the reversal's stamp.
	stamp = posting_stamp or stamp_for(*reversal_stamp_source(work_item), UTC_CLOCK)
	return apply_stamp(doc, stamp)
