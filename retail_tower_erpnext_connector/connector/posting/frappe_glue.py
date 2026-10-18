# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Frappe-coupled posting glue — ⏳ BENCH-VALIDATION (T031/T032/T041/T051/T090).

This is the ONLY posting module that imports ``frappe``. It composes the pure-Python core
(contracts / transport / builder / idempotency / reasons / uom) into the live posting flow
against an ERPNext site:

  - T031 — submit the built Sales Invoice on ERPNext (interim SI-only; rider R1).
  - T032 — ack ``posted`` + ``documentRef`` over ``connectorAckOutcome``.
  - T041 — duplicate ``posted`` echoes the existing ``documentRef`` (no second document).
  - T051 — transient → ``failed_transient``; non-retryable → ``permanently_rejected`` + reason.
  - T090 — structured logs + signals carrying the DP2 ``request_id`` correlation; no secrets.

⚠️ NOT RUN AND NOT CLAIMED PASSING IN THIS LANE. This machine has no Frappe bench
(standing-rules §6); these paths are validated on a staging ERPNext v15 site. The frappe
import below intentionally fails at local import time — that is expected and is why this
module is isolated from the locally-tested core.

This module does NOT register any hook (FR-014 — ``hooks.py`` stays empty; the poller
``scheduler_event`` is the deferred [GATED] T093) and does NOT define a DocType (the
idempotency-store DocType is the deferred [GATED] T020). It RECEIVES an
:class:`IdempotencyStore` adapter; it does not create one.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import frappe

from .builder import UnmappedUnit, build_sales_invoice
from .contracts import ErpnextDocumentRef, OutcomeAckRequest, PostingWorkItem
from .idempotency import IdempotencyConflict, IdempotencyStore, key_for, provenance_id
from .posting_time import PostingClock, PostingStamp, apply_stamp, raise_to_original, stamp_for
from .reasons import FailureKind, scrub_message, to_rejection_reason
from .representation_check import (
    ExpectedInvoice,
    RepresentationMismatch,
    check_representation,
    expected_from_payload,
    persisted_from_doc,
    replay_exemptions,
)
from .resolution_check import InvoiceLine, InvoiceSnapshot, check_existing, check_replayed_representation
from .return_builder import (
    MissingRefundTenders,
    ReturnPricingMismatch,
    ReturnResolvers,
    ReturnTaxNotPosted,
    build_return_invoice,
)
from .reversal_builder import build_reversing_invoice, reversal_stamp_source
from .reversal_policy import UnsupportedReversal, assert_reversal_supported
from .stock_policy import (
    ReturnLineMismatch,
    UnsupportedTrackedItem,
    assert_no_tracked_items,
    link_return_to_original,
    link_void_to_original,
    mirror_company,
    tracking_item_codes,
)
from .tender import (
    SETTLED_FIELDS,
    SettlementDrift,
    TenderMismatch,
    TenderModeMap,
    UnmappedTender,
    assert_settled,
    payments_total,
)
from .transport import PostingFeedClient
from .uom import (
    MoneyConformanceError,
    PreResolvedWarehouse,
    StoreCustomerMap,
    UnmappedStore,
    UnresolvedWarehouse,
    UomMap,
)
from .warehouse_policy import FrozenWarehouseRejected, assert_postable_warehouse

_SALES_INVOICE = "Sales Invoice"
_SIGNAL_RECOVERED = "posting.recovered"


def _ack_key(work_item: PostingWorkItem, outcome: str) -> str:
    """The connectorAckOutcome Idempotency-Key — required on EVERY ack (012, F-004).

    Codex-P1: the key must be **per-outcome**, not per-sale. One sale legitimately yields
    `failed_transient` then `posted` on a re-offer; reusing one key across DIFFERENT outcomes
    returns `409 idempotency_key_conflict` (resolution-concepts.md §4), which would make the
    recovery `posted` ack unreachable after any transient. Keying on `(workItemRef, outcome)`
    keeps each logical outcome's key stable (so a dropped-response resend of the SAME ack still
    dedupes) while letting a later different outcome use its own key. NOT per-attempt — a
    per-attempt nonce would break resend dedup.

    RT-171: ``failed_transient`` is the one outcome that legitimately repeats for one work-item —
    DP2 re-offers it after each transient, and each re-offer is a NEW logical ack that must reach
    the handler (it re-heads the row and spends one unit of DP2's retry budget). Backend-Core's
    ack route is ``@Idempotent``: a second ``{workItemRef}:failed_transient`` would be REPLAYED
    from the stored response without running the handler, leaving the row pending forever. So the
    transient key also carries the offer's ``itemCursor``: stable across resends of ONE offer (resend
    dedup still holds) and different across re-offers (DP2 issues a new cursor per offer). The
    terminal ``posted`` / ``permanently_rejected`` keys are unchanged — they happen at most once.

    RT-333: "at most once" holds per frozen resolution version. An operator re-resolution makes
    the v2 attempt a NEW logical ack whose body echoes v2: under the v1 key it would be answered
    ``409 idempotency_key_conflict`` and never reach the handler. So a versioned work item's
    terminal keys carry ``:v{n}`` (stable across resends of one version); an unversioned (legacy)
    work item keeps the plain key. The transient key already changes per offer.
    """
    if outcome == "failed_transient":
        return f"{work_item.work_item_ref}:failed_transient:{work_item.item_cursor}"
    if work_item.resolution_version is not None:
        return f"{work_item.work_item_ref}:{outcome}:v{work_item.resolution_version}"
    return f"{work_item.work_item_ref}:{outcome}"


def _transient_exceptions() -> tuple[type[BaseException], ...]:
    """The ERPNext/Frappe exceptions that are genuinely retryable (F-005).

    Everything else (ValidationError, PermissionError, KeyError, …) is non-retryable and
    maps to ``permanently_rejected`` — never silently re-offered forever. Resolved lazily so
    the set tracks the installed frappe; unknown names degrade to the base where absent.
    """
    names = ("TimedOutError", "RetryBackoffError", "QueryTimeoutError")
    found = tuple(getattr(frappe, n) for n in names if hasattr(frappe, n))
    # TimeoutError is always a sane transient signal even if frappe names none of the above.
    return (*found, TimeoutError)


def _dup_provenance_exceptions() -> tuple[type[BaseException], ...]:
    """Exceptions a duplicate Sales Invoice `(rt_source_system, rt_external_id)` insert raises (G5/F-002).

    The `unique_rt_si_provenance` index on Sales Invoice (patch sales_invoice_unique_provenance)
    makes a SECOND submit of an already-posted sale fail at the DB. On bench frappe v15 this surfaces
    as **UniqueValidationError** (a ValidationError subclass — verified on the bench, NOT guessed);
    DuplicateEntryError is included defensively (the Posting Log path raises that one). getattr-guarded
    so a frappe missing a name degrades rather than errors. This MUST be caught BEFORE the generic
    `frappe.ValidationError` handler, or a legitimate crash-recovery would be FALSELY rejected.
    """
    names = ("UniqueValidationError", "DuplicateEntryError")
    return tuple(
        exc for exc in (getattr(frappe.exceptions, n, None) for n in names) if exc is not None
    ) or (Exception,)


def _warehouse_for(work_item: PostingWorkItem, warehouses: PreResolvedWarehouse):
    """RT-331: the frozen warehouse from the feed (012 1.6.0-draft), else the local store map."""
    frozen = work_item.sale.warehouse_ref
    if frozen is None:
        return warehouses.for_store
    return lambda _store_id: dict(frozen)


def _assert_frozen_warehouse(work_item: PostingWorkItem, company: str) -> None:
    """⏳ BENCH-VALIDATION. RT-434: a feed ``warehouseRef`` must be a leaf Warehouse of ``company``.

    The preflight checks only the local map's Warehouses; a frozen one arrives per item, so it is
    checked here, before insert (:mod:`warehouse_policy`). A work item without one (pre-freeze, local
    map) reads nothing. A transient read error propagates and acks ``failed_transient``.
    """
    frozen = work_item.sale.warehouse_ref
    if frozen is None:
        return
    name = frozen["name"]
    row = frappe.db.get_value("Warehouse", name, ["company", "is_group"], as_dict=True)
    assert_postable_warehouse(name, row, company)


class _NotCompared:
    """A unit / customer / tender map stand-in for a replay rebuild (RT-378): what it feeds is never
    compared there.

    Payments are not part of the representation check; UOM and customer are exempt on replay
    (``replay_exemptions``: the frozen resolution carries neither). So a replay needs no current map,
    and a map changed since the sale cannot turn a matching replay into a failure. It echoes its input
    (a string) so the pure builders still run.
    """

    @staticmethod
    def resolve(value: str) -> str:
        return value

    @staticmethod
    def for_store(value: str) -> str:
        return value


class _WarehouseNotCompared:
    """The warehouse map stand-in for a replay rebuild: the warehouse is exempt (RT-331 owns it), but
    the builders need a ``{doctype, name}`` identity."""

    @staticmethod
    def for_store(value: str) -> dict:
        return {"doctype": "Warehouse", "name": value}


class SiteCompanyNotConfigured(Exception):
    """Global Defaults has no ``default_company``: a sale cannot be posted on an explicit company.

    A plain configuration rejection (``validation``) before insert: nothing was persisted, so it is
    not a D1 representation mismatch.
    """


def _invoice_snapshot(document_ref: ErpnextDocumentRef) -> InvoiceSnapshot:
    """⏳ BENCH-VALIDATION. Read an existing Sales Invoice back from the database (RT-331 + RT-378).

    A document that no longer exists reads as not submitted (docstatus 0), as the RT-331 read did,
    so it acks ``reconciliation_required`` rather than being re-offered forever.
    """
    missing = getattr(frappe, "DoesNotExistError", None)
    try:
        doc = frappe.get_doc(_SALES_INVOICE, document_ref.name)
    except Exception as exc:
        if missing is not None and isinstance(exc, missing):
            return InvoiceSnapshot(docstatus=0, items=())
        raise
    return InvoiceSnapshot(
        docstatus=int(doc.get("docstatus") or 0),
        items=tuple(
            InvoiceLine(item_code=r.get("item_code"), warehouse=r.get("warehouse"), line_ref=r.get("rt_line_ref"))
            for r in doc.get("items") or ()
        ),
        representation=persisted_from_doc(doc),
    )


def _site_company() -> str | None:
    """⏳ BENCH-VALIDATION. The site default company (Global Defaults ``default_company``)."""
    return frappe.db.get_single_value("Global Defaults", "default_company")


def _sale_company() -> str:
    """The company a Retail Tower SALE is posted on, set explicitly in the payload (RT-378 finding 2).

    One source of truth with the representation check. A blank site default fails closed
    (:class:`SiteCompanyNotConfigured` → ``validation``, before insert); a transient read error is
    ``failed_transient`` (read inside the submit stage).
    A store→company map is a future extension.
    """
    company = _site_company()
    if not company:
        raise SiteCompanyNotConfigured(
            "the site default company (Global Defaults default_company) is not configured — rejected as "
            "validation before insert (RT-378: every sale is posted on an explicit company)"
        )
    return company


def _replay_company(work_item: PostingWorkItem) -> str | None:
    """⏳ BENCH-VALIDATION. The company an EXISTING credit note must be on: its original invoice's
    (``None`` when unknown: the replay cannot be verified). A sale's company is exempt on replay
    (``replay_exemptions``): today's site default is not a frozen value."""
    if work_item.kind != "reversal":
        return None
    original = _resolve_original_invoice(work_item)
    return frappe.db.get_value(_SALES_INVOICE, original, "company") if original else None


def _replay_expected(work_item: PostingWorkItem, company: str | None) -> ExpectedInvoice:
    """Rebuild the payload with the pure builders and take its representation (RT-378, B.3).

    No current map is consulted (:class:`_NotCompared`): every field it would feed is exempt. The
    sale's tenders are dropped too: payments are never compared, and a sale posted before RT-78 may
    carry tenders that do not sum to its lines (``TenderMismatch`` would make it unverifiable). A
    return keeps its refund tenders, which its builder requires.
    """
    work_item = dataclasses.replace(work_item, sale=dataclasses.replace(work_item.sale, tenders=()))
    if work_item.kind == "reversal":
        stand_in = _NotCompared()
        payload = _build_reversal(
            work_item,
            None,
            uom_map=stand_in,
            warehouses=_WarehouseNotCompared(),
            customers=stand_in,
            tenders=stand_in,
        )
        payload = {**payload, "company": company}
    else:
        payload = build_sales_invoice(
            work_item,
            uom_for=_NotCompared.resolve,
            warehouse_for=_WarehouseNotCompared.for_store,
            customer_for=_NotCompared.for_store,
            mode_of_payment_for=_NotCompared.resolve,
            company=company,
        )
    return expected_from_payload(payload, exempt=replay_exemptions(work_item))


class _CannotVerify(Exception):
    """The existing invoice cannot be verified now: unreadable, or its expected representation cannot
    be rebuilt (e.g. no company known). Not a mismatch: retried (RT-378 review finding 1)."""


def _existing_mismatch(work_item: PostingWorkItem, document_ref: ErpnextDocumentRef) -> str | None:
    """RT-331 identity first (versioned work items only), then the RT-378 representation.

    Returns the mismatch reason, or ``None`` when the document matches. Raises :class:`_CannotVerify`
    when the invoice cannot be read or the expected representation cannot be rebuilt.
    """
    try:
        snapshot = _invoice_snapshot(document_ref)
        company = _replay_company(work_item)
    except Exception as exc:
        raise _CannotVerify(str(exc)) from exc
    if work_item.resolution_version is not None:
        identity = check_existing(work_item, snapshot)
        if identity is not None:
            return identity
    try:
        expected = _replay_expected(work_item, company)
    except Exception as exc:  # incl. RepresentationMismatch for an unknown company
        raise _CannotVerify(f"expected representation cannot be rebuilt: {exc}") from exc
    return check_replayed_representation(snapshot, expected)


def _ack_existing(
    client: PostingFeedClient,
    work_item: PostingWorkItem,
    document_ref: ErpnextDocumentRef,
    correlation_id: str,
    signal: str,
) -> str:
    """Ack a work item whose invoice ALREADY exists (replay guard or duplicate-provenance recovery).

    RT-331: "matching provenance alone proves identity candidate, not correctness". When Backend-Core
    sent a frozen resolution (``resolution_version``), the existing invoice is verified against it
    first: a mismatch (or an unsubmitted document) acks ``reconciliation_required`` with the existing
    ``documentRef`` — never a second document; an unreadable invoice acks ``failed_transient`` so
    Backend-Core re-offers it — never a blind ``posted``.
    RT-378: every replay, versioned or not, is also held to the persisted representation (Item, qty,
    rate, amount, list price, no discount or margin, currency, company, no tax row, grand total). An
    unversioned work item (no frozen resolution) skips only the RT-331 identity checks. An invoice
    posted before RT-377 (``ignore_pricing_rule`` 0) gets the reduced field set. When the expected
    representation cannot be rebuilt, the document cannot be verified: ``failed_transient`` like an
    unreadable invoice, so DP2's retry budget ends it as repairable. ``reconciliation_required`` is
    only for a real mismatch.
    """
    try:
        mismatch = _existing_mismatch(work_item, document_ref)
    except _CannotVerify as exc:  # retry later, never assume the document matches.
        return _ack_verify_transient(client, work_item, correlation_id, exc)
    if mismatch is not None:
        return _ack_reconciliation(client, work_item, document_ref, correlation_id, mismatch)
    return _ack_posted(client, work_item, document_ref, correlation_id, signal)


def _commit_before_ack() -> None:
    """RT-458 (RT-382 F-1): make the item's ERPNext effects durable before Backend-Core hears of them.

    ⏳ BENCH-VALIDATION. The scheduler tick is one transaction: ``ScheduledJobType.execute`` commits on
    success and rolls back on ANY exception. Acking while it was still open let a later failure in the
    tick (the next ack, the next page pull) roll back an invoice Backend-Core already held ``posted`` —
    with its Stock Ledger/GL, its Posting Log row and its naming-series value, so the next invoice took
    the lost sale's name. Committing here, after submit, ``record_posted`` and the RT-378 read-back,
    means a later failure can only lose un-acked work, which Backend-Core still re-offers; a failure
    between this commit and the ack is the designed lost-ack replay (the Posting Log replay guard).

    Called only by the acks that report an existing document (``posted``, ``reconciliation_required``,
    and the read-back ``failed_transient``). A rejection never commits: its insert was already rolled
    back to the RT-48 savepoint, so "rejected" still means "nothing posted". A commit failure is NOT
    caught: it propagates with no ack sent, and the item stays pending in Backend-Core.
    """
    frappe.db.commit()


def _ack_posted(
    client: PostingFeedClient,
    work_item: PostingWorkItem,
    document_ref: ErpnextDocumentRef,
    correlation_id: str,
    signal: str,
) -> str:
    _commit_before_ack()  # RT-458
    client.ack_outcome(
        work_item.work_item_ref,
        OutcomeAckRequest.posted(document_ref, resolution_version=work_item.resolution_version),
        idempotency_key=_ack_key(work_item, "posted"),
    )
    _log_signal(signal, work_item, correlation_id, document_ref=document_ref.name)
    return "posted"


def _ack_verify_transient(
    client: PostingFeedClient, work_item: PostingWorkItem, correlation_id: str, exc: BaseException
) -> str:
    """The invoice could not be read back: ``failed_transient`` so Backend-Core re-offers it.

    RT-458: after a fresh submit the invoice and its Posting Log row exist, so they are committed first
    and the re-offer resolves through the replay guard (on a replay the commit has nothing to write).
    """
    _commit_before_ack()
    client.ack_outcome(
        work_item.work_item_ref,
        OutcomeAckRequest.failed_transient(resolution_version=work_item.resolution_version),
        idempotency_key=_ack_key(work_item, "failed_transient"),
    )
    _log_signal("posting.verify.transient", work_item, correlation_id, detail=scrub_message(str(exc)))
    return "failed_transient"


def _ack_reconciliation(
    client: PostingFeedClient,
    work_item: PostingWorkItem,
    document_ref: ErpnextDocumentRef,
    correlation_id: str,
    mismatch: str,
) -> str:
    """An ERP document exists but does not match: ``reconciliation_required``, never cancelled or amended."""
    # Bounded + scrubbed like every other reason (RT-48: 1..1000 chars).
    reason = to_rejection_reason(FailureKind.VALIDATION, message=mismatch)
    _commit_before_ack()  # RT-458: the document stays as ERPNext holds it — durable before the ack
    client.ack_outcome(
        work_item.work_item_ref,
        OutcomeAckRequest.reconciliation_required(
            document_ref, reason, resolution_version=work_item.resolution_version
        ),
        idempotency_key=_ack_key(work_item, "reconciliation_required"),
    )
    _log_signal(
        "posting.reconciliation_required",
        work_item,
        correlation_id,
        level="error",  # Frappe v15 drops INFO outside the dev server (RT-409)
        document_ref=document_ref.name,
        category=reason.category,
        reason_message=reason.message,  # the same scrubbed, bounded text sent on the wire (Gate G4)
    )
    return "reconciliation_required"


def _ack_submitted(
    client: PostingFeedClient,
    work_item: PostingWorkItem,
    sinv,
    expected: ExpectedInvoice,
    correlation_id: str,
) -> str:
    """RT-378 after submit: read the invoice back from the database before acking ``posted``.

    ⏳ BENCH-VALIDATION. ERPNext validates again on submit (RT-376 saw a Pricing Rule reprice there),
    so the in-memory check after insert is not proof. A mismatch acks ``reconciliation_required`` and
    the document stays as ERPNext holds it (684). An unreadable invoice acks ``failed_transient``.
    """
    document_ref = ErpnextDocumentRef(doctype=_SALES_INVOICE, name=sinv.name)
    try:
        sinv.reload()
        mismatch = check_representation(expected, persisted_from_doc(sinv))
    except Exception as exc:
        return _ack_verify_transient(client, work_item, correlation_id, exc)
    if mismatch is not None:
        return _ack_reconciliation(client, work_item, document_ref, correlation_id, mismatch)
    return _ack_posted(client, work_item, document_ref, correlation_id, "posting.posted")


def _find_posted_invoice(work_item: PostingWorkItem) -> ErpnextDocumentRef | None:
    """Resolve the already-submitted Sales Invoice for this work-item's provenance (G5/F-002 recovery).

    Looks up by the `(rt_source_system, rt_external_id)` idempotency anchor — a hit IS the same
    logical posting. The `rt_external_id` value is the #28 discriminator `provenance_id(work_item)`:
    `external_id` for a `sale_post` (UNCHANGED forward recovery), `work_item_ref` for a `reversal`.
    This MUST mirror what the builder wrote (`reversal_builder` writes the same `provenance_id`),
    else a reversal crash-recovery would match the ORIGINAL forward SI by `external_id` and echo it
    (re-introducing the #28 silent mis-success on the dup path). Returns the document_ref to echo,
    or None if no submitted SI exists.
    """
    name = frappe.db.get_value(
        _SALES_INVOICE,
        {
            "rt_source_system": work_item.source_system,
            "rt_external_id": provenance_id(work_item),
            "docstatus": 1,
        },
        "name",
    )
    if not name:
        return None
    return ErpnextDocumentRef(doctype=_SALES_INVOICE, name=name)


# Sale build errors that are a non-retryable ``validation`` rejection (never a guessed value).
_SALE_BUILD_VALIDATION_ERRORS = (
    MoneyConformanceError,
    UnresolvedWarehouse,
    UnmappedStore,
    UnmappedTender,
    TenderMismatch,
)


# Reversal build errors that are a non-retryable ``validation`` rejection (never a guessed value).
_REVERSAL_BUILD_VALIDATION_ERRORS = (
    MoneyConformanceError,
    UnresolvedWarehouse,
    UnmappedStore,
    UnmappedTender,
    TenderMismatch,
    MissingRefundTenders,
    ReturnPricingMismatch,
    ReturnTaxNotPosted,
)


def post_work_item(
    work_item: PostingWorkItem,
    *,
    client: PostingFeedClient,
    store: IdempotencyStore,
    uom_map: UomMap,
    warehouses: PreResolvedWarehouse,
    customers: StoreCustomerMap,
    tenders: TenderModeMap,
    correlation_id: str,
) -> str:
    """Post one work-item to ERPNext and ack the outcome. Returns the resulting outcome string.

    ⏳ BENCH-VALIDATION — exercises live ``frappe`` APIs; validated on staging, not locally.
    ``tenders`` is the RT-10 D5 tender → Mode of Payment map (RT-78): a sale carrying tenders is
    settled on the invoice and an unmapped method fails closed. A void does not use it — it pays
    back the original invoice's own payment rows.
    """
    key = key_for(work_item)

    # T041 — idempotent replay: an already-posted key echoes the existing documentRef.
    existing = store.get_document_ref(key)
    if existing is not None:
        return _ack_existing(client, work_item, existing, correlation_id, "posting.replay")

    # F-001 — a `reversal` work-item posts a REVERSING document (a return Sales Invoice,
    # `is_return=1`), NOT a fresh positive Sales Invoice (012: kind=reversal). Arc A S1 routes it
    # to the dedicated reversal leg below. Any unknown kind still fails closed.
    if work_item.kind == "reversal":
        return _post_reversal(
            work_item,
            client=client,
            store=store,
            uom_map=uom_map,
            warehouses=warehouses,
            customers=customers,
            tenders=tenders,
            correlation_id=correlation_id,
        )
    if work_item.kind != "sale_post":
        return _reject(
            client,
            work_item,
            correlation_id,
            FailureKind.OTHER,
            f"work-item kind {work_item.kind!r} not supported (012 kinds: sale_post, reversal)",
        )

    # Build the Sales-Invoice payload from the pure-Python core (no frappe in the build).
    # build_sales_invoice self-validates money (FR-009), raises on an unmapped unit (FR-008) or a
    # non-conformant amount, and warehouse_for raises UnresolvedWarehouse on a store with no
    # pre-resolved warehouse (rider R5 — should have DLQ'd in DP2). All are non-retryable; a final
    # `except Exception` guarantees no build error escapes the terminal-outcome invariant
    # (Codex-P2 / SC-001 / Principle VI).
    try:
        stamp = _posting_stamp(work_item, work_item.sale.business_date)
        doc_payload = build_sales_invoice(
            work_item,
            uom_for=uom_map.resolve,
            warehouse_for=_warehouse_for(work_item, warehouses),
            customer_for=customers.for_store,
            posting_stamp=stamp,
            mode_of_payment_for=tenders.resolve,
        )
    except UnmappedUnit as exc:
        return _reject(client, work_item, correlation_id, FailureKind.UNMAPPED_UNIT, _exception_message(exc))
    except _SALE_BUILD_VALIDATION_ERRORS as exc:
        # UnmappedStore (F-009): a store with no configured Customer fails closed → validation,
        # exactly like an unresolved warehouse — never fabricate a customer (Principle VI).
        # RT-78: an unmapped tender method (D5) or a tender total that differs from the lines
        # (amendment 4a) is the same — the connector never guesses a mode or forces a match.
        return _reject(client, work_item, correlation_id, FailureKind.VALIDATION, _exception_message(exc))
    except Exception as exc:  # any other build error is non-retryable — never let it escape.
        return _reject(client, work_item, correlation_id, FailureKind.OTHER, _exception_message(exc))

    _log_stamp_adjustments(stamp, work_item, correlation_id)

    # RT-48 / RT-47 D4 — the sale moves stock, so a batch/serial Item fails closed (ERPNext would
    # otherwise auto-pick a batch, i.e. guess one). After the replay guard, before insert.
    rejected = _guard_tracked_items(doc_payload, work_item, client, correlation_id)
    if rejected is not None:
        return rejected

    # T031 — submit on ERPNext (interim SI-only; rider R1 — no Payment Entry here).
    try:
        # RT-378 finding 2: the explicit company (site default). Read here, so a transient DB error is
        # failed_transient and a blank default is validation (RepresentationMismatch), before insert.
        company = _sale_company()
        _assert_frozen_warehouse(work_item, company)  # RT-434: a feed warehouseRef, before insert
        doc_payload = {**doc_payload, "company": company}
        expected = expected_from_payload(doc_payload)  # what ERPNext must persist
        sinv = _submit_atomically(doc_payload, expected)
        document_ref = ErpnextDocumentRef(doctype=_SALES_INVOICE, name=sinv.name)
    except _transient_exceptions() as exc:
        # T051 — transient (timeout/lock) → failed_transient; DP2 re-offers (no self-retry).
        client.ack_outcome(
            work_item.work_item_ref,
            OutcomeAckRequest.failed_transient(resolution_version=work_item.resolution_version),
            idempotency_key=_ack_key(work_item, "failed_transient"),
        )
        _log_signal("posting.transient", work_item, correlation_id, detail=scrub_message(str(exc)))
        return "failed_transient"
    except FrozenWarehouseRejected as exc:
        # RT-434 — the feed warehouseRef is a group / other-company / missing Warehouse: nothing was
        # inserted. An invoice already submitted under this provenance (a crash before record_posted,
        # then the Warehouse changed) is recovered, never rejected; otherwise reject as validation.
        return _recover_or_reject_unsupported(
            work_item, key, exc, client=client, store=store, correlation_id=correlation_id
        )
    except (SettlementDrift, RepresentationMismatch, SiteCompanyNotConfigured) as exc:
        # RT-78 — ERPNext's computed totals left change/outstanding/write-off on a settled invoice.
        # RT-378 / D1 — ERPNext holds a value Retail Tower did not send ("representation: ...");
        # SiteCompanyNotConfigured is a plain config rejection (no default company), before insert.
        # _submit_atomically already rolled the insert back; nothing was submitted. Caught BEFORE the
        # dup-provenance clause, whose fallback catches Exception on a frappe missing those names.
        return _reject(client, work_item, correlation_id, FailureKind.VALIDATION, _exception_message(exc))
    except _dup_provenance_exceptions() as exc:
        # GATE G5 / F-002 — the crash-window recovery. The `unique_rt_si_provenance` index rejected
        # a SECOND submit of an already-posted sale (a crash between the FIRST submit and
        # record_posted left no Posting Log row, so the replay guard above found nothing). This is
        # NOT a rejection: a matching SI already exists. Resolve to it (echo its documentRef), back-
        # fill the Posting Log so future re-offers fast-path through the replay guard, and fall
        # through to the normal `posted` ack. Catching this BEFORE frappe.ValidationError is load-
        # bearing — UniqueValidationError ⊂ ValidationError, so the generic handler would otherwise
        # FALSELY reject a legitimately-posted sale (Principle VI inversion).
        return _recover_duplicate_provenance(
            work_item, key, exc, client=client, store=store, correlation_id=correlation_id
        )
    except frappe.ValidationError as exc:  # type: ignore[attr-defined]
        # T051 — validation failure → permanently_rejected / validation.
        return _reject(client, work_item, correlation_id, FailureKind.VALIDATION, _exception_message(exc))
    except Exception as exc:  # any OTHER error is non-retryable (F-005), not transient.
        # Decision table row 9: an unclassified non-retryable error → permanently_rejected / other.
        return _reject(client, work_item, correlation_id, FailureKind.OTHER, _exception_message(exc))

    # Record the posting, then ack. A concurrent double-record (another worker recorded a
    # different ref for this key first) is resolved by echoing the recorded ref.
    #
    # F-002 (the crash-BETWEEN-submit-and-record window) is now CLOSED by the
    # `unique_rt_si_provenance` index on Sales Invoice + the `_dup_provenance_exceptions` recovery
    # above: if a crash here leaves no Posting Log row, a DP2 re-offer's second submit fails on the
    # unique index, is caught, and resolves to the existing SI (no duplicate). Gate G5 holds against
    # both concurrent double-record (this IdempotencyConflict echo) and the crash window.
    try:
        store.record_posted(key, document_ref)
    except IdempotencyConflict as conflict:
        # Another worker recorded a DIFFERENT document for this key first: that document is an
        # EXISTING one, so it is verified against the frozen resolution like any other (RT-331).
        return _ack_existing(client, work_item, conflict.existing, correlation_id, "posting.conflict")

    # T032 — ack posted + documentRef once the persisted invoice is verified (RT-378); the DP2 sale
    # fact is never mutated.
    return _ack_submitted(client, work_item, sinv, expected, correlation_id)


def _resolve_original_invoice(work_item: PostingWorkItem) -> str | None:
    """Resolve the ORIGINAL submitted Sales Invoice this reversal targets (Arc A S1).

    ⏳ BENCH-VALIDATION. Looks up the forward SI by the reversed sale's provenance
    ``(reversal_of.source_system, reversal_of.external_id)`` — the 012 ``reversalOf`` anchor, which
    is the original sale's ``(sourceSystem, externalId)``, NOT this reversal work-item's own id.
    Returns the original SI's ERPNext docname for ``return_against``, or None if none is found
    (the caller never posts a reversal against a sale that was never posted — RT-172: it acks
    ``failed_transient`` so DP2 re-offers it once the original posts).
    """
    if work_item.reversal_of is None:  # an upstream contract violation for kind=reversal.
        return None
    return frappe.db.get_value(
        _SALES_INVOICE,
        {
            "rt_source_system": work_item.reversal_of.source_system,
            "rt_external_id": work_item.reversal_of.external_id,
            "docstatus": 1,
        },
        "name",
    )


def _post_reversal(
    work_item: PostingWorkItem,
    *,
    client: PostingFeedClient,
    store: IdempotencyStore,
    uom_map: UomMap,
    warehouses: PreResolvedWarehouse,
    customers: StoreCustomerMap,
    tenders: TenderModeMap,
    correlation_id: str,
) -> str:
    """Post one ``reversal`` work-item as a return Sales Invoice (credit note). Returns the outcome.

    ⏳ BENCH-VALIDATION — this leg exercises live ``frappe`` APIs and CANNOT run in this
    environment (no Frappe bench; standing-rules §6). It mirrors the sale_post path exactly:
    replay-guard → build (pure ``build_reversing_invoice``) → resolve original for return_against →
    ``insert().submit()`` → record_posted → ack ``posted``; transient/validation/dup/other handling
    mirrors the forward path; a reversal whose original sale has no submitted invoice YET is never
    posted (Principle VI) — RT-172 acks it ``failed_transient`` so DP2 re-offers it within its retry
    budget, instead of rejecting a reversal that only arrived before its original. Idempotency reuses the SAME ``store`` replay
    primitive but keyed on the reversal work-item's ``(source_system, work_item_ref)`` (Connector
    #28 re-key via :func:`key_for` — the top-level ``external_id`` on a reversal is the ORIGINAL
    sale's id and would collide with its replay slot). No new primitive (Arc A §4); a
    reversal→original is N:1 but each reversal request is 1:1 with its key.
    """
    key = key_for(work_item)

    # Replay guard: an already-posted reversal key echoes the existing reversing-doc ref (T041).
    existing = store.get_document_ref(key)
    if existing is not None:
        return _ack_existing(client, work_item, existing, correlation_id, "posting.replay")

    # RT-71 (RT-14 F1 / D8) — an amount-only refund would credit the WHOLE sale (the feed carries
    # every sale line and no refund amount). Reject it before building anything. AFTER the replay
    # guard, so an already-posted legacy refund still echoes its document.
    try:
        assert_reversal_supported(work_item)
    except UnsupportedReversal as exc:
        return _recover_or_reject_unsupported(
            work_item, key, exc, client=client, store=store, correlation_id=correlation_id
        )

    # Build the reversing payload from the pure core (no frappe in the build). Same fail-closed
    # mapping as the forward path: unmapped unit → unmapped_unit; money/warehouse/store → validation;
    # anything else → other. A final `except Exception` guarantees the terminal-outcome invariant.
    built = _build_reversal_or_reject(
        work_item,
        client=client,
        uom_map=uom_map,
        warehouses=warehouses,
        customers=customers,
        tenders=tenders,
        correlation_id=correlation_id,
    )
    if isinstance(built, str):
        return built  # the rejection outcome
    stamp, doc_payload = built

    # Resolve the original SI for `return_against`. The pure builder cannot do this lookup (no DB
    # hit there), so it is applied here, on the bench leg, before insert. A reversal work-item with
    # no `reversalOf` is an upstream contract violation — rejected as validation, never re-offered.
    if work_item.reversal_of is None:
        return _reject(
            client,
            work_item,
            correlation_id,
            FailureKind.VALIDATION,
            "reversal work-item carries no reversalOf (return_against unresolvable)",
        )
    original_name = _resolve_original_invoice(work_item)
    if original_name is None:
        # RT-172 — the reversed sale has no submitted Sales Invoice YET (e.g. its own posting is
        # still pending or retrying). Posting against it would be a silent inconsistency (Principle
        # VI), but rejecting it would strand a reversal that merely arrived first. Nothing is built
        # into ERPNext: ack `failed_transient` (per-offer key, RT-171) so DP2 re-offers it; DP2's
        # POSTING_RETRY_BUDGET bounds the wait and ends it as retry_budget_exhausted, not validation.
        return _ack_awaiting_original(client, work_item, correlation_id)
    doc_payload["return_against"] = original_name

    # RT-49 (decision 10312) — ERPNext rejects a return timestamped EARLIER than its original. An
    # original posted before RT-49 carries ERPNext's post-day, which can be after the businessDate
    # stamp; raise to it (equal is allowed). Applies to every reversal kind.
    try:
        original_date, original_time = _read_original_posting(original_name)
        stamp = raise_to_original(stamp, original_date, original_time)
        doc_payload = apply_stamp(doc_payload, stamp)
    except Exception as exc:  # a read failure must not escape the terminal-outcome invariant.
        return _reject(client, work_item, correlation_id, FailureKind.OTHER, _exception_message(exc))
    _log_stamp_adjustments(stamp, work_item, correlation_id)

    # RT-48 / RT-47 D5 — a full void mirrors the original's update_stock (a legacy update_stock=0
    # sale never fabricates a stock restoration) and, RT-80, its disable_rounded_total (a pre-RT-80
    # rounded sale is reversed with its own Round Off), and links each line to its original row so
    # ERPNext's per-line over-return cap applies. A refund stays update_stock=0 and unlinked (its
    # line semantics belong to RT-14/RT-16). Any line mismatch fails closed.
    # RT-16 / RT-14 D6 — a partial return links each row to the original row with the same
    # rt_line_ref (identity), restores to that row's warehouse and mirrors update_stock.
    linked = _link_original_or_reject(
        work_item, doc_payload, original_name, client=client, correlation_id=correlation_id
    )
    if isinstance(linked, str):
        return linked  # the rejection outcome
    doc_payload = linked

    rejected = _guard_tracked_items(doc_payload, work_item, client, correlation_id)
    if rejected is not None:
        return rejected

    # Submit on ERPNext. The `unique_rt_si_provenance` index spans ALL Sales Invoices (incl.
    # is_return=1), so a second submit of the SAME reversal key (its work_item_ref) collides and is
    # recovered exactly like the forward path — and because the builder wrote the reversal's
    # work_item_ref into rt_external_id (#28 re-key), it does NOT collide with the original SI's slot.
    try:
        expected = expected_from_payload(doc_payload)  # RT-378: company = the original's (linked)
        sinv = _submit_atomically(doc_payload, expected)
        document_ref = ErpnextDocumentRef(doctype=_SALES_INVOICE, name=sinv.name)
    except _transient_exceptions() as exc:
        client.ack_outcome(
            work_item.work_item_ref,
            OutcomeAckRequest.failed_transient(resolution_version=work_item.resolution_version),
            idempotency_key=_ack_key(work_item, "failed_transient"),
        )
        _log_signal("posting.transient", work_item, correlation_id, detail=scrub_message(str(exc)))
        return "failed_transient"
    except (SettlementDrift, RepresentationMismatch) as exc:
        # RT-78 — ERPNext's computed totals left change/outstanding/write-off on a settled invoice.
        # RT-378 / D1 — ERPNext holds a value Retail Tower did not send ("representation: ...").
        # _submit_atomically already rolled the insert back; nothing was submitted. Caught BEFORE the
        # dup-provenance clause, whose fallback catches Exception on a frappe missing those names.
        return _reject(client, work_item, correlation_id, FailureKind.VALIDATION, _exception_message(exc))
    except _dup_provenance_exceptions() as exc:
        # Crash-window recovery, keyed on the reversal's work_item_ref provenance (#28 re-key): a
        # re-offer whose second submit hits the unique index resolves to the already-posted
        # reversing doc (via _find_posted_invoice, which reads the same provenance_id discriminator).
        return _recover_duplicate_provenance(
            work_item, key, exc, client=client, store=store, correlation_id=correlation_id
        )
    except frappe.ValidationError as exc:  # type: ignore[attr-defined]
        return _reject(client, work_item, correlation_id, FailureKind.VALIDATION, _exception_message(exc))
    except Exception as exc:  # any OTHER error is non-retryable (F-005), not transient.
        return _reject(client, work_item, correlation_id, FailureKind.OTHER, _exception_message(exc))

    try:
        store.record_posted(key, document_ref)
    except IdempotencyConflict as conflict:
        # Same as the forward sale: the concurrently recorded document is verified first (RT-331).
        return _ack_existing(client, work_item, conflict.existing, correlation_id, "posting.conflict")

    return _ack_submitted(client, work_item, sinv, expected, correlation_id)  # RT-378 read-back


def _build_reversal_or_reject(
    work_item: PostingWorkItem,
    *,
    client: PostingFeedClient,
    uom_map: UomMap,
    warehouses: PreResolvedWarehouse,
    customers: StoreCustomerMap,
    tenders: TenderModeMap,
    correlation_id: str,
) -> tuple[PostingStamp, dict] | str:
    """Build the reversing payload; a build error returns its rejection outcome string instead."""
    try:
        # RT-16 / RT-63: the reversal's own recordedAt + businessDate (RT-49 fallback when absent).
        stamp = stamp_for(*reversal_stamp_source(work_item), _posting_clock())
        doc_payload = _build_reversal(
            work_item, stamp, uom_map=uom_map, warehouses=warehouses, customers=customers, tenders=tenders
        )
    except UnmappedUnit as exc:
        return _reject(client, work_item, correlation_id, FailureKind.UNMAPPED_UNIT, _exception_message(exc))
    except _REVERSAL_BUILD_VALIDATION_ERRORS as exc:
        # RT-16: a return without refundTenders, or priced other than unitPrice x qty, is rejected
        # like an unmapped store — never posted as an outstanding credit note or at a guessed amount.
        return _reject(client, work_item, correlation_id, FailureKind.VALIDATION, _exception_message(exc))
    except Exception as exc:  # any other build error is non-retryable — never let it escape.
        return _reject(client, work_item, correlation_id, FailureKind.OTHER, _exception_message(exc))
    return stamp, doc_payload


def _link_original_or_reject(
    work_item: PostingWorkItem,
    doc_payload: dict,
    original_name: str,
    *,
    client: PostingFeedClient,
    correlation_id: str,
) -> dict | str:
    """Link a void/return to its original invoice rows; a failure returns its rejection outcome."""
    kind = work_item.reversal_of.reversal_kind if work_item.reversal_of is not None else None
    if kind in ("void", "return"):
        try:
            doc_payload = _link_to_original(kind, doc_payload, _read_original_invoice(original_name))
        except ReturnLineMismatch as exc:
            return _reject(client, work_item, correlation_id, FailureKind.VALIDATION, _exception_message(exc))
        except Exception as exc:  # a read failure must not escape the terminal-outcome invariant.
            return _reject(client, work_item, correlation_id, FailureKind.OTHER, _exception_message(exc))

    return doc_payload


def _recover_duplicate_provenance(
    work_item: PostingWorkItem,
    key,
    exc: Exception,
    *,
    client: PostingFeedClient,
    store: IdempotencyStore,
    correlation_id: str,
) -> str:
    """Crash-window recovery after the ``unique_rt_si_provenance`` index rejected a second submit.

    Resolves to the already-submitted invoice (via :func:`_find_posted_invoice`), back-fills the
    Posting Log row, and acks it. No matching invoice is a non-retryable ``other`` rejection —
    never a fabricated success.
    """
    existing = _find_posted_invoice(work_item)
    if existing is None:
        return _reject(client, work_item, correlation_id, FailureKind.OTHER, _exception_message(exc))
    document_ref = existing
    try:
        store.record_posted(key, document_ref)  # back-fill the missing Posting Log row
    except IdempotencyConflict as conflict:
        document_ref = conflict.existing
    return _ack_existing(client, work_item, document_ref, correlation_id, _SIGNAL_RECOVERED)


def _ack_awaiting_original(client: PostingFeedClient, work_item: PostingWorkItem, correlation_id: str) -> str:
    """RT-172: the reversed sale has no submitted invoice yet — ack ``failed_transient``."""
    client.ack_outcome(
        work_item.work_item_ref,
        OutcomeAckRequest.failed_transient(resolution_version=work_item.resolution_version),
        idempotency_key=_ack_key(work_item, "failed_transient"),
    )
    _log_signal(
        "posting.reversal.awaiting_original",
        work_item,
        correlation_id,
        reversal_kind=work_item.reversal_of.reversal_kind,
    )
    return "failed_transient"


def _recover_or_reject_unsupported(
    work_item: PostingWorkItem,
    key,
    exc: Exception,
    *,
    client: PostingFeedClient,
    store: IdempotencyStore,
    correlation_id: str,
) -> str:
    """Reject an unsupported reversal, unless its invoice already exists (RT-71; Codex P1, PR #46).

    A legacy refund may have been submitted before RT-71 and then crashed before ``record_posted``:
    the Posting Log has no row, so the replay guard missed it, but the Sales Invoice exists under
    this work-item's provenance. Rejecting it would make DP2 disagree with ERPNext, and a later
    ``re_post`` could credit the sale twice. So resolve that invoice first and ack it ``posted``
    (back-filling the Posting Log), exactly like the duplicate-provenance recovery. Otherwise nothing
    was posted, and the reversal is rejected as ``validation``.

    RT-434 reuses it for a sale whose feed warehouseRef fails :func:`_assert_frozen_warehouse`.
    """
    existing = _find_posted_invoice(work_item)
    if existing is None:
        return _reject(client, work_item, correlation_id, FailureKind.VALIDATION, _exception_message(exc))
    document_ref = existing
    try:
        store.record_posted(key, document_ref)
    except IdempotencyConflict as conflict:
        document_ref = conflict.existing
    return _ack_existing(client, work_item, document_ref, correlation_id, _SIGNAL_RECOVERED)


_SUBMIT_SAVEPOINT = "rt_posting_submit"


def _submit_atomically(doc_payload: dict, expected: ExpectedInvoice):
    """Insert + submit one Sales Invoice as a unit: on ANY failure, undo it before re-raising (RT-48).

    ⏳ BENCH-VALIDATION. A submit can fail PART-WAY. The RT-48 bench run proved it: a missing
    valuation rate raised after the invoice row and its Stock Ledger Entry were written, and the
    scheduler job then committed that half-posted invoice while the connector acked
    ``permanently_rejected``. Stock and ERP truth would diverge from the DP2 outcome (Principle VI).
    Rolling back to a savepoint keeps "rejected" meaning "nothing posted", so the later ``re_post``
    repair starts clean. The caller's exception mapping is unchanged.

    RT-378 (D1): after ``insert()`` the document ERPNext computed must hold exactly ``expected``;
    otherwise :class:`RepresentationMismatch` rolls it back like any other failure.
    """
    frappe.db.savepoint(_SUBMIT_SAVEPOINT)
    try:
        sinv = frappe.get_doc(doc_payload)
        sinv.insert()
        # RT-378 first: a repriced paid sale also fails to settle, and D1 requires the
        # "representation:" reason for it, not the settlement one.
        mismatch = check_representation(expected, persisted_from_doc(sinv))
        if mismatch is not None:
            raise RepresentationMismatch(mismatch)
        if doc_payload.get("payments"):
            # RT-78: verify ERPNext's computed totals settle the invoice exactly before submitting.
            assert_settled(
                {field: sinv.get(field) for field in SETTLED_FIELDS},
                expected_total=payments_total(doc_payload["payments"]),
            )
        sinv.submit()
    except BaseException:
        frappe.db.rollback(save_point=_SUBMIT_SAVEPOINT)
        raise
    return sinv


def _read_item_tracking(doc_payload: dict) -> dict[str, dict]:
    """Read ``{item_code: {has_batch_no, has_serial_no}}`` for a stock-moving doc's Items (RT-48).

    ⏳ BENCH-VALIDATION. Returns ``{}`` for a doc that does not move stock (nothing to check).
    """
    codes = tracking_item_codes(doc_payload)
    if not codes or not doc_payload.get("update_stock"):
        return {}
    rows = frappe.get_all(
        "Item",
        filters={"name": ["in", codes]},
        fields=["name", "has_batch_no", "has_serial_no"],
    )
    return {row["name"]: row for row in rows}


def _guard_tracked_items(
    doc_payload: dict,
    work_item: PostingWorkItem,
    client: PostingFeedClient,
    correlation_id: str,
) -> str | None:
    """Reject a stock-moving doc carrying a batch/serial Item (RT-47 D4). Returns the outcome if rejected.

    ⏳ BENCH-VALIDATION. The decision is the pure ``stock_policy.assert_no_tracked_items``; this
    only reads the Item flags. A read failure is non-retryable ``other`` (never escapes the page).
    """
    try:
        assert_no_tracked_items(doc_payload, _read_item_tracking(doc_payload))
    except UnsupportedTrackedItem as exc:
        return _reject(client, work_item, correlation_id, FailureKind.VALIDATION, _exception_message(exc))
    except Exception as exc:
        return _reject(client, work_item, correlation_id, FailureKind.OTHER, _exception_message(exc))
    return None


def _posting_clock() -> PostingClock:
    """The ERPNext site timezone (ERPNext reads ``posting_time`` as site-local) and now (RT-49).

    ⏳ BENCH-VALIDATION.
    """
    return PostingClock(site_tz=frappe.utils.get_system_timezone(), now=datetime.now(timezone.utc))


def _posting_stamp(work_item: PostingWorkItem, business_date: str) -> PostingStamp:
    """RT-49 rule (``posting_time.stamp_for``) for this work-item on the live site clock."""
    return stamp_for(work_item.sale.occurred_at, business_date, _posting_clock())


def _read_original_posting(name: str) -> tuple[object, object]:
    """Read the original SI's ``(posting_date, posting_time)`` as stored (RT-49).

    ⏳ BENCH-VALIDATION. ``posting_time`` comes back from MariaDB as a ``timedelta``;
    ``posting_time.raise_to_original`` normalises it.
    """
    row = frappe.db.get_value(_SALES_INVOICE, name, ["posting_date", "posting_time"], as_dict=True)
    if not row:
        raise ValueError(f"original Sales Invoice {name!r} not readable for its posting time")
    return row["posting_date"], row["posting_time"]


def _log_stamp_adjustments(stamp: PostingStamp, work_item: PostingWorkItem, correlation_id: str) -> None:
    """Log a clamped/capped/raised posting stamp at ERROR (Frappe drops lower levels; RT-49 10312).

    A clamp means the store and site timezones disagree (a precondition breach) or the POS clock
    ran ahead; a raise means the original invoice was posted before RT-49 on a later day.
    """
    if not stamp.adjustments:
        return
    frappe.logger("retail_tower_posting").error(
        {
            "event": "posting.time_adjusted",
            "work_item_ref": work_item.work_item_ref,
            "source_system": work_item.source_system,
            "external_id": work_item.external_id,
            "request_id": correlation_id,
            "adjustments": list(stamp.adjustments),
            "posting_date": stamp.posting_date,
            "posting_time": stamp.posting_time,
        }
    )


def _build_reversal(
    work_item: PostingWorkItem,
    stamp: PostingStamp | None,
    *,
    uom_map: UomMap,
    warehouses: PreResolvedWarehouse,
    customers: StoreCustomerMap,
    tenders: TenderModeMap,
) -> dict:
    """The pure reversing payload: a partial return (RT-16) or a void/refund credit note.

    A void takes no tender map (its Modes of Payment come from the original invoice, RT-78); a
    return pays its refund out through the CURRENT map (a new cash payout, RT-10 D6).
    """
    if work_item.reversal_of is not None and work_item.reversal_of.reversal_kind == "return":
        resolvers = ReturnResolvers(
            uom_for=uom_map.resolve,
            warehouse_for=_warehouse_for(work_item, warehouses),
            customer_for=customers.for_store,
            mode_of_payment_for=tenders.resolve,
        )
        return build_return_invoice(work_item, resolvers, posting_stamp=stamp)
    return build_reversing_invoice(
        work_item,
        uom_for=uom_map.resolve,
        warehouse_for=_warehouse_for(work_item, warehouses),
        customer_for=customers.for_store,
        posting_stamp=stamp,
    )


def _link_to_original(kind: str, doc_payload: dict, original: dict) -> dict:
    """Mirror/link a void (whole sale) or a partial return (by rt_line_ref) onto its original invoice.

    RT-378 finding 2: either way the credit note lands on the original invoice's company.
    """
    if kind == "return":
        linked = link_return_to_original(
            doc_payload,
            original_update_stock=original["update_stock"],
            original_items=original["items"],
            original_customer=original["customer"],
        )
    else:
        linked = link_void_to_original(
            doc_payload,
            original_update_stock=original["update_stock"],
            original_disable_rounded_total=original["disable_rounded_total"],
            original_items=original["items"],
            original_is_pos=original["is_pos"],
            original_payments=original["payments"],
        )
    return mirror_company(linked, original["company"])


def _read_original_invoice(name: str) -> dict:
    """Read what a full void mirrors from the original SI (``stock_policy.link_void_to_original``).

    ``update_stock`` (RT-48), ``disable_rounded_total`` (RT-80), ``is_pos`` and its payment rows
    ``(mode_of_payment, amount, idx)`` (RT-78), and its item rows
    ``(name, item_code, qty, idx, warehouse, rt_line_ref, uom)`` (RT-48; ``rt_line_ref``/``uom`` RT-16)
    and the ``customer`` a partial return credits. The
    rows are filtered by ``parent`` — the original only, never an earlier credit note's rows.

    ⏳ BENCH-VALIDATION.
    """
    flags = frappe.db.get_value(
        _SALES_INVOICE,
        name,
        ["update_stock", "disable_rounded_total", "is_pos", "customer", "company"],
        as_dict=True,
    ) or {}
    rows = frappe.get_all(
        "Sales Invoice Item",
        filters={"parent": name, "parenttype": _SALES_INVOICE},
        fields=["name", "item_code", "qty", "idx", "warehouse", "rt_line_ref", "uom", "conversion_factor"],
        order_by="idx asc",
    )
    payments = frappe.get_all(
        "Sales Invoice Payment",
        filters={"parent": name, "parenttype": _SALES_INVOICE},
        fields=["mode_of_payment", "amount", "idx"],
        order_by="idx asc",
    )
    return {
        "update_stock": int(flags.get("update_stock") or 0),
        "disable_rounded_total": int(flags.get("disable_rounded_total") or 0),
        "is_pos": int(flags.get("is_pos") or 0),
        "customer": flags.get("customer"),
        "company": flags.get("company"),  # RT-378: the credit note lands on the original's company
        "items": [dict(row) for row in rows],
        "payments": [dict(row) for row in payments],
    }


def _reject(
    client: PostingFeedClient,
    work_item: PostingWorkItem,
    correlation_id: str,
    kind: FailureKind,
    message: str,
) -> str:
    """Ack a non-retryable failure with a closed-set reason (scrubbed). F-003/F-004.

    Uses the per-outcome ack key for ``permanently_rejected`` (Codex-P1).
    """
    reason = to_rejection_reason(kind, message=message)  # scrubs the message (Gate G4)
    client.ack_outcome(
        work_item.work_item_ref,
        OutcomeAckRequest.permanently_rejected(reason, resolution_version=work_item.resolution_version),
        idempotency_key=_ack_key(work_item, "permanently_rejected"),
    )
    _log_signal(
        "posting.rejected",
        work_item,
        correlation_id,
        level="error",  # Frappe v15 drops INFO outside the dev server (RT-409 / AD-POSTING-001 D-7)
        category=reason.category,
        reason_message=reason.message,  # the same scrubbed, bounded text sent on the wire
    )
    return "permanently_rejected"


def _exception_message(exc: BaseException) -> str:
    """A rejection message that names the check that failed: ``"<ExceptionClass>: <text>"``.

    RT-409 / AD-POSTING-001 D-7: a bare ``str(exc)`` hid which check raised (RT-380 run 3:
    ``ServerScriptNotEnabled`` was unattributable). Literal messages are not passed through this.
    """
    return f"{type(exc).__name__}: {exc}"


def _log_signal(
    event: str, work_item: PostingWorkItem, correlation_id: str, *, level: str = "info", **fields
) -> None:
    """T090 — structured log carrying the DP2 request_id correlation; never logs secrets/payload.

    ``level="error"`` is for the signals an operator must see on a production bench, where Frappe
    v15 sets ``frappe.logger()`` to ERROR (the RT-49 convention).
    """
    getattr(frappe.logger("retail_tower_posting"), level)(
        {
            "event": event,
            "work_item_ref": work_item.work_item_ref,
            "source_system": work_item.source_system,
            "external_id": work_item.external_id,
            "request_id": correlation_id,
            **fields,
        }
    )
