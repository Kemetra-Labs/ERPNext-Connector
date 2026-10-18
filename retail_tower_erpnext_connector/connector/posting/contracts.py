# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Frozen read-models for the fixed 012 posting-feed contract (T010).

These mirror Data-Pulse-2's ``posting-feed.yaml`` 1:1 (1.1.0-draft, plus the RT-76 feed-1.2
``Sale.tenders`` settlement field). The contract is
DP2-owned and read-only (FR-015, Principle I) — these DTOs CITE it, never re-derive it.

Apply-only invariant (rider R2 / Q-CON-004): every offered ``SaleLine`` carries a
DP2-resolved ``erpnextItemRef``. A line missing it is an upstream contract violation —
parsing raises :class:`MissingErpnextItemRef` (the connector never resolves/guesses; FR-001).

This module imports NO frappe — it is the pure-Python core and is unit-tested locally.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

# 012 RejectionReason.category — the closed set. No new wire reason is ever invented
# (FR-007). An unmapped unit is a `validation` failure (there is no `unmapped_uom`).
REJECTION_CATEGORIES: frozenset[str] = frozenset(
    {"validation", "closed_period", "unmapped_item", "unmapped_account", "other"}
)

# 012 OutcomeAckRequest.outcome enum.
OUTCOMES: frozenset[str] = frozenset(
    {"posted", "failed_transient", "permanently_rejected", "reconciliation_required"}
)

# 012 PostingWorkItem.kind enum.
WORK_ITEM_KINDS: frozenset[str] = frozenset({"sale_post", "reversal"})

# 012 ReversalRef.reversalKind enum.
REVERSAL_KINDS: frozenset[str] = frozenset({"void", "refund", "return"})

# 012 SaleTender.method enum (RT-10 D2 pilot methods; vouchers are excluded).
TENDER_METHODS: frozenset[str] = frozenset({"cash", "card_external"})

# 012 NonNegativeDecimalAmount and SaleTender.reference patterns (posting-feed.yaml, verbatim).
_NON_NEGATIVE_DECIMAL_RE = re.compile(r"^\d{1,15}(\.\d{1,4})?$", re.ASCII)
_TENDER_REFERENCE_RE = re.compile(r"^[A-Z0-9]{1,6}$")
# RFC 3339 ``date-time`` (full-date "T" full-time with seconds and an offset); fromisoformat alone
# also accepts forms without seconds or in basic format, and some Pythons normalize 24:00 to the next
# midnight — so hour/minute/second/offset ranges are part of the grammar (Codex P2, PR #49).
# RFC 3339 ``full-date`` (a calendar date): fromisoformat on 3.11+ also takes ISO week dates.
_CALENDAR_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RFC3339_DATE_TIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]([01]\d|2[0-3]):[0-5]\d:[0-5]\d(\.\d+)?([Zz]|[+-]([01]\d|2[0-3]):[0-5]\d)$"
)
# 012 ReturnLine.quantity pattern (verbatim); the connector additionally requires > 0.
_RETURN_QUANTITY_RE = re.compile(r"^\d{1,13}(\.\d{1,6})?$", re.ASCII)


class MissingErpnextItemRef(Exception):
    """An offered SaleLine lacks ``erpnextItemRef`` — an upstream 012 contract violation.

    Per rider R2 every offered line is DP2-pre-resolved; observing one without the ref is a
    STOP-and-raise (the connector never substitutes a "Misc" item — R3). Mapped to a
    ``permanently_rejected`` / ``validation`` outcome by the caller (FR-006/008).
    """


@dataclass(frozen=True)
class ErpnextItemRef:
    """012 ``ErpnextItemRef`` — generic ``{doctype:"Item", name}`` addressing (O-6)."""

    doctype: str
    name: str

    @classmethod
    def from_wire(cls, wire: Mapping[str, object]) -> ErpnextItemRef:
        doctype = wire.get("doctype")
        if doctype != "Item":
            raise ValueError(f"ErpnextItemRef.doctype must be 'Item', got {doctype!r}")
        return cls(doctype="Item", name=str(wire["name"]))


@dataclass(frozen=True)
class ErpnextDocumentRef:
    """012 ``ErpnextDocumentRef`` — generic addressing for a submitted ERPNext document."""

    doctype: str
    name: str

    def to_wire(self) -> dict[str, str]:
        return {"doctype": self.doctype, "name": self.name}


# Reversal kinds that carry their own persisted business date (RT-63 P2; a legacy refund has none).
_DATED_REVERSAL_KINDS: frozenset[str] = frozenset({"void", "return"})


def _omitted_or_value(wire: Mapping[str, object], field: str) -> object:
    """``None`` only when ``field`` is OMITTED; an explicit JSON null is malformed (PR #49 review).

    The schema types these fields as strings: only an older Backend-Core that omits them gets the
    RT-49 fallback, never a modern payload that sends null (it would silently post on the sale's day).
    """
    if field not in wire:
        return None
    value = wire[field]
    if value is None:
        raise ValueError(f"ReversalRef.{field} must be omitted or a string, not null (RT-63)")
    return value


def _optional_instant(wire: Mapping[str, object], field: str) -> str | None:
    """An optional RFC 3339 ``date-time`` string, validated (never re-formatted)."""
    value = _omitted_or_value(wire, field)
    if value is None:
        return None
    if not isinstance(value, str) or not _RFC3339_DATE_TIME_RE.match(value):
        raise ValueError(
            f"ReversalRef.{field} must be an RFC 3339 date-time string (seconds and a timezone offset, "
            f"Z or ±hh:mm), got {value!r}"
        )
    try:
        # The grammar guarantees an offset; this catches out-of-range values (month 13, hour 25).
        datetime.fromisoformat(value.upper().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"ReversalRef.{field} is not a valid date-time: {value!r}") from None
    return value


def _optional_date(wire: Mapping[str, object], field: str) -> str | None:
    """An optional ISO ``date`` (YYYY-MM-DD) string, validated."""
    value = _omitted_or_value(wire, field)
    if value is None:
        return None
    if not isinstance(value, str) or not _CALENDAR_DATE_RE.match(value):
        raise ValueError(f"ReversalRef.{field} must be a YYYY-MM-DD date string, got {value!r}")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"ReversalRef.{field} is not a valid date: {value!r}") from None
    return value


def _require_object(wire: object, name: str) -> Mapping[str, object]:
    """A wire element must be a JSON object; anything else is a malformed item (ValueError)."""
    if not isinstance(wire, Mapping):
        raise ValueError(f"{name} must be an object, got {type(wire).__name__}")
    return wire


def _non_negative_amount(wire: Mapping[str, object], field: str, name: str) -> str:
    value = wire.get(field)
    if not isinstance(value, str) or not _NON_NEGATIVE_DECIMAL_RE.match(value):
        raise ValueError(f"{name}.{field} must be a non-negative exact-decimal string, got {value!r}")
    return value


@dataclass(frozen=True)
class ReturnLine:
    """012 ``ReturnLine`` (RT-14 D1): one returned sale line, priced by Backend-Core.

    Non-negative MAGNITUDES (the connector negates them on the credit note); ``line_ref`` points at
    ``sale.lines[].lineRef``. Backend-Core prices it by the cumulative-difference rule, so a line's
    returns sum to its amount exactly.
    """

    line_ref: str
    quantity: str
    line_amount: str
    tax_amount: str | None = None

    @classmethod
    def from_wire(cls, wire: object) -> ReturnLine:
        w = _require_object(wire, "ReturnLine")
        line_ref = w.get("lineRef")
        if not isinstance(line_ref, str) or not line_ref:
            raise ValueError(f"ReturnLine.lineRef must be a non-empty string, got {line_ref!r}")
        quantity = w.get("quantity")
        if (
            not isinstance(quantity, str)
            or not _RETURN_QUANTITY_RE.match(quantity)
            or Decimal(quantity) <= 0
        ):
            raise ValueError(f"ReturnLine.quantity must be a positive exact-decimal string, got {quantity!r}")
        line_amount = _non_negative_amount(w, "lineAmount", "ReturnLine")
        tax_amount = None if w.get("taxAmount") is None else _non_negative_amount(w, "taxAmount", "ReturnLine")
        return cls(line_ref=line_ref, quantity=quantity, line_amount=line_amount, tax_amount=tax_amount)


@dataclass(frozen=True)
class RefundTender:
    """012 ``RefundTender`` (RT-14 D3): one cash payout of a ``return``. Cash only; no reference."""

    method: str
    amount: str

    @classmethod
    def from_wire(cls, wire: object) -> RefundTender:
        w = _require_object(wire, "RefundTender")
        if w.get("method") != "cash":
            raise ValueError(f"RefundTender.method must be 'cash' (RT-14 D3), got {w.get('method')!r}")
        if "reference" in w:
            raise ValueError("RefundTender carries no reference (012 additionalProperties: false)")
        return cls(method="cash", amount=_non_negative_amount(w, "amount", "RefundTender"))


def _parse_list(raw: object, field: str, parse) -> tuple:
    if not isinstance(raw, list):
        raise ValueError(f"ReversalRef.{field} must be a list, got {type(raw).__name__}")
    return tuple(parse(item) for item in raw)


def _parse_return_parts(kind: str, wire: Mapping[str, object]) -> tuple[tuple, tuple]:
    """``(returnLines, refundTenders)``: required lines on a ``return``; both forbidden elsewhere."""
    raw_lines, raw_tenders = wire.get("returnLines"), wire.get("refundTenders")
    if kind != "return":
        for field, raw in (("returnLines", raw_lines), ("refundTenders", raw_tenders)):
            if raw is not None:
                raise ValueError(f"ReversalRef.{field} is only allowed on a return, not {kind!r}")
        return (), ()
    if not raw_lines:
        raise ValueError("a return must carry at least one ReversalRef.returnLines entry (012 minItems: 1)")
    lines = _parse_list(raw_lines, "returnLines", ReturnLine.from_wire)
    refs = [line.line_ref for line in lines]
    if len(set(refs)) != len(refs):
        raise ValueError(f"ReversalRef.returnLines has a duplicate lineRef: {refs}")
    tenders = () if raw_tenders is None else _parse_list(raw_tenders, "refundTenders", RefundTender.from_wire)
    return lines, tenders


@dataclass(frozen=True)
class ReversalRef:
    """012 ``ReversalRef`` — provenance of the original sale a reversal targets (O-4).

    RT-16 / RT-63 (decision 10348): ``recorded_at`` is the reversal's server event time and
    ``business_date`` its own business day. Both are optional on parse — an older Backend-Core sends
    neither and the connector falls back to the RT-49 sale-derived stamp.
    """

    source_system: str
    external_id: str
    reversal_kind: str
    recorded_at: str | None = None
    business_date: str | None = None
    # RT-16 / RT-14 D1+D3: only on a `return` — the returned lines and how the cash was paid out.
    return_lines: tuple[ReturnLine, ...] = ()
    refund_tenders: tuple[RefundTender, ...] = ()

    @classmethod
    def from_wire(cls, wire: Mapping[str, object]) -> ReversalRef:
        kind = str(wire["reversalKind"])
        if kind not in REVERSAL_KINDS:
            raise ValueError(f"reversalKind must be one of {sorted(REVERSAL_KINDS)}, got {kind!r}")
        recorded_at = _optional_instant(wire, "recordedAt")
        return_lines, refund_tenders = _parse_return_parts(kind, wire)
        business_date = _optional_date(wire, "businessDate")
        if business_date is not None and recorded_at is None:
            # Only BOTH-absent is the older-Backend-Core fallback: a lone day would be ignored by the
            # stamp selector and the reversal silently posted on the sale's day (PR #49 review).
            raise ValueError(f"ReversalRef for {kind!r} carries businessDate but no recordedAt (RT-63)")
        if kind == "return" and recorded_at is None:
            # A return exists only on feed 1.3, which always carries the reversal's own time + day.
            raise ValueError("a return must carry reversalOf.recordedAt and businessDate (RT-63)")
        if kind in _DATED_REVERSAL_KINDS and recorded_at is not None and business_date is None:
            # posting-feed 1.3: a void/return carries its own businessDate. A time without its day
            # cannot be stamped correctly (and must never silently fall back to the sale's day).
            raise ValueError(f"ReversalRef for {kind!r} carries recordedAt but no businessDate (RT-63)")
        return cls(
            source_system=str(wire["sourceSystem"]),
            external_id=str(wire["externalId"]),
            reversal_kind=kind,
            recorded_at=recorded_at,
            business_date=business_date,
            return_lines=return_lines,
            refund_tenders=refund_tenders,
        )


@dataclass(frozen=True)
class SaleTender:
    """012 ``SaleTender`` — one way the sale was paid (RT-10 D1/D2), amount NET of change.

    Parsed strictly: an unknown method, a non-string / negative / over-precise amount, a reference
    on ``cash`` or a reference outside the short card-terminal pattern raises ``ValueError``, which
    the transport isolates as ``malformed_work_item`` → ``validation``. The connector never guesses
    or repairs a tender (Principle VI). ``reference`` is the terminal's short reference, never card data.
    """

    method: str
    amount: str
    reference: str | None = None

    @classmethod
    def from_wire(cls, wire: Mapping[str, object]) -> SaleTender:
        if not isinstance(wire, Mapping):
            # Greptile PR #48: `.get()` on a scalar raised AttributeError, which the transport does
            # not isolate, so one bad item aborted the page. ValueError is isolated per item.
            raise ValueError(f"SaleTender must be an object, got {type(wire).__name__}")
        method = wire.get("method")
        if method not in TENDER_METHODS:
            raise ValueError(f"SaleTender.method must be one of {sorted(TENDER_METHODS)}, got {method!r}")
        amount = wire.get("amount")
        if not isinstance(amount, str) or not _NON_NEGATIVE_DECIMAL_RE.match(amount):
            raise ValueError(
                f"SaleTender.amount must be a non-negative exact-decimal string, got {amount!r}"
            )
        reference = wire.get("reference")
        if reference is not None:
            if method == "cash":
                raise ValueError("SaleTender.reference is only allowed on card_external (012)")
            if not isinstance(reference, str) or not _TENDER_REFERENCE_RE.match(reference):
                raise ValueError(
                    f"SaleTender.reference must match ^[A-Z0-9]{{1,6}}$, got {reference!r}"
                )
        return cls(method=str(method), amount=amount, reference=reference)


def _parse_tenders(raw: object) -> tuple[SaleTender, ...]:
    """``Sale.tenders``: absent / null / empty → ``()`` (tender-unknown, RT-10 D8); else each parsed."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"Sale.tenders must be a list, got {type(raw).__name__}")
    return tuple(SaleTender.from_wire(t) for t in raw)


@dataclass(frozen=True)
class SaleLine:
    """012 ``SaleLine`` — a frozen 008 sale-line snapshot carrying the resolved Item identity.

    Money fields are exact-decimal strings (never float — FR-009 / `DecimalAmount`).
    """

    line_name: str
    unit_price: str
    currency_code: str
    quantity: str
    line_amount: str
    unit: str
    erpnext_item_ref: ErpnextItemRef
    tax_amount: str | None = None
    tenant_product_ref: str | None = None
    # RT-16 / RT-14 D6: stable line identity (= sale_lines.id) from feed 1.3; a return points at it.
    line_ref: str | None = None

    @classmethod
    def from_wire(cls, wire: Mapping[str, object]) -> SaleLine:
        if "erpnextItemRef" not in wire or wire.get("erpnextItemRef") is None:
            raise MissingErpnextItemRef(
                f"offered SaleLine {wire.get('lineName')!r} carries no erpnextItemRef "
                "(rider R2 — every offered line is DP2-pre-resolved)"
            )
        return cls(
            line_name=str(wire["lineName"]),
            unit_price=str(wire["unitPrice"]),
            currency_code=str(wire["currencyCode"]),
            quantity=str(wire["quantity"]),
            line_amount=str(wire["lineAmount"]),
            unit=str(wire["unit"]),
            erpnext_item_ref=ErpnextItemRef.from_wire(wire["erpnextItemRef"]),  # type: ignore[arg-type]
            tax_amount=None if wire.get("taxAmount") is None else str(wire["taxAmount"]),
            tenant_product_ref=(
                None if wire.get("tenantProductRef") is None else str(wire["tenantProductRef"])
            ),
            line_ref=None if wire.get("lineRef") is None else str(wire["lineRef"]),
        )


@dataclass(frozen=True)
class Sale:
    """012 ``Sale`` — wire projection of a captured 008 sale (header + lines)."""

    sale_ref: str
    store_id: str
    currency_code: str
    pos_total: str
    occurred_at: str
    business_date: str
    source_system: str
    external_id: str
    lines: tuple[SaleLine, ...]
    # RT-78: how the sale was paid. Empty = tender-unknown (posted unpaid, as before — RT-10 D8).
    tenders: tuple[SaleTender, ...] = ()
    # RT-331: the frozen ERP warehouse ({doctype: "Warehouse", name}) from Backend-Core's resolution
    # (012 1.6.0-draft). None for an intent created before the freeze: the local warehouse map applies.
    warehouse_ref: dict | None = None

    @classmethod
    def from_wire(cls, wire: Mapping[str, object]) -> Sale:
        # 012 Sale.lines is minItems: 1 — an empty lines array is an upstream contract
        # violation; raise rather than build an invoice with no items (F-012).
        raw_lines = wire["lines"]
        if not raw_lines:
            raise ValueError("Sale.lines must carry at least one line (012 minItems: 1)")
        lines = tuple(SaleLine.from_wire(line) for line in raw_lines)  # type: ignore[union-attr]
        refs = [line.line_ref for line in lines if line.line_ref is not None]
        if len(set(refs)) != len(refs):
            # lineRef = sale_lines.id (unique per sale). A duplicate would post two invoice rows with
            # one rt_line_ref and make every later return of that line unmatchable (PR #50 review).
            raise ValueError(f"Sale.lines carries a duplicate lineRef: {refs}")
        return cls(
            sale_ref=str(wire["saleRef"]),
            store_id=str(wire["storeId"]),
            currency_code=str(wire["currencyCode"]),
            pos_total=str(wire["posTotal"]),
            occurred_at=str(wire["occurredAt"]),
            business_date=str(wire["businessDate"]),
            source_system=str(wire["sourceSystem"]),
            external_id=str(wire["externalId"]),
            lines=lines,
            tenders=_parse_tenders(wire.get("tenders")),
            warehouse_ref=_parse_warehouse_ref(wire.get("warehouseRef", _ABSENT)),
        )


# An omitted optional field (the legacy, pre-freeze shape) — distinct from an explicit ``null``,
# which the 012 contract does not allow for ``resolutionVersion`` / ``warehouseRef`` (PR #60 review).
_ABSENT = object()


def _parse_warehouse_ref(raw: object) -> dict | None:
    """012 ``ErpnextWarehouseRef`` (RT-331): a strict ``{doctype: "Warehouse", name}`` or absent (never null)."""
    if raw is _ABSENT:
        return None
    if not isinstance(raw, Mapping) or raw.get("doctype") != "Warehouse":
        raise ValueError(f"Sale.warehouseRef must be a Warehouse reference, got {raw!r}")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"Sale.warehouseRef.name must be a non-empty string, got {name!r}")
    return {"doctype": "Warehouse", "name": name}


def _parse_resolution_version(raw: object) -> int | None:
    """012 ``resolutionVersion`` (RT-331): a positive integer or absent, never null (a bool is not an integer)."""
    if raw is _ABSENT:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ValueError(f"resolutionVersion must be a positive integer, got {raw!r}")
    return raw


def _assert_return_lines_point_at_sale_lines(reversal_of: ReversalRef, sale: Sale) -> None:
    """Every returned ``lineRef`` must name a line of the sale snapshot (RT-14 D6) — else malformed."""
    known = {line.line_ref for line in sale.lines if line.line_ref is not None}
    stray = [r.line_ref for r in reversal_of.return_lines if r.line_ref not in known]
    if stray:
        raise ValueError(f"ReturnLine.lineRef {stray} does not name a line of the sale snapshot")


@dataclass(frozen=True)
class PostingWorkItem:
    """012 ``PostingWorkItem`` — one pending posting the connector must apply (O-1)."""

    work_item_ref: str
    kind: str
    source_system: str
    external_id: str
    payload_hash: str
    business_date: str
    sale: Sale
    item_cursor: str
    reversal_of: ReversalRef | None = None
    # RT-331: the frozen resolution version (012 1.6.0-draft); echoed on the ack. None before the freeze.
    resolution_version: int | None = None

    @classmethod
    def from_wire(cls, wire: Mapping[str, object]) -> PostingWorkItem:
        kind = str(wire["kind"])
        if kind not in WORK_ITEM_KINDS:
            raise ValueError(f"kind must be one of {sorted(WORK_ITEM_KINDS)}, got {kind!r}")
        raw_reversal = wire.get("reversalOf")
        sale = Sale.from_wire(wire["sale"])  # type: ignore[arg-type]
        reversal_of = None if raw_reversal is None else ReversalRef.from_wire(raw_reversal)  # type: ignore[arg-type]
        if reversal_of is not None:
            _assert_return_lines_point_at_sale_lines(reversal_of, sale)
        return cls(
            work_item_ref=str(wire["workItemRef"]),
            kind=kind,
            source_system=str(wire["sourceSystem"]),
            external_id=str(wire["externalId"]),
            payload_hash=str(wire["payloadHash"]),
            business_date=str(wire["businessDate"]),
            sale=sale,
            item_cursor=str(wire["itemCursor"]),
            reversal_of=reversal_of,
            resolution_version=_parse_resolution_version(wire.get("resolutionVersion", _ABSENT)),
        )

    @property
    def idempotency_key(self) -> tuple[str, str]:
        """The 012 O-3 wire idempotency anchor for a FORWARD ``sale_post`` — ``(sourceSystem, externalId)``.

        FAIL-CLOSED for reversals (Connector #28): a reversal's top-level ``externalId`` is the
        ORIGINAL sale's id, so ``(sourceSystem, externalId)`` is the PRE-FIX buggy anchor — keying a
        reversal on it collides with the original sale's replay slot and silently echoes the original
        invoice (silent mis-success). Reversals MUST key via ``idempotency.key_for`` /
        ``idempotency.provenance_id`` (which re-key on ``workItemRef``). This property therefore raises
        for any non-``sale_post`` kind rather than hand back the buggy anchor.
        """
        if self.kind != "sale_post":
            raise ValueError(
                "idempotency_key is the forward sale_post anchor only; reversals must key via "
                "idempotency.key_for/provenance_id (see #28)"
            )
        return (self.source_system, self.external_id)


@dataclass(frozen=True)
class RejectionReason:
    """012 ``RejectionReason`` — structured permanent-rejection reason (closed category set)."""

    category: str
    message: str

    def __post_init__(self) -> None:
        if self.category not in REJECTION_CATEGORIES:
            raise ValueError(
                f"category must be one of {sorted(REJECTION_CATEGORIES)} "
                f"(no new wire reason — FR-007), got {self.category!r}"
            )

    def to_wire(self) -> dict[str, str]:
        return {"category": self.category, "message": self.message}


@dataclass(frozen=True)
class OutcomeAckRequest:
    """012 ``OutcomeAckRequest`` — the typed terminal outcome over ``connectorAckOutcome``."""

    outcome: str
    document_ref: ErpnextDocumentRef | None = None
    reason: RejectionReason | None = None
    # RT-331: echo of the work item's frozen resolution version (012 1.6.0-draft); None = omitted.
    resolution_version: int | None = None

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {sorted(OUTCOMES)}, got {self.outcome!r}")
        if self.outcome in ("posted", "reconciliation_required") and self.document_ref is None:
            raise ValueError(f"{self.outcome} outcome requires a documentRef (012 OutcomeAckRequest)")
        if self.outcome in ("permanently_rejected", "reconciliation_required") and self.reason is None:
            raise ValueError(f"{self.outcome} outcome requires a reason (012)")

    @classmethod
    def posted(
        cls, document_ref: ErpnextDocumentRef, *, resolution_version: int | None = None
    ) -> OutcomeAckRequest:
        return cls(outcome="posted", document_ref=document_ref, resolution_version=resolution_version)

    @classmethod
    def reconciliation_required(
        cls,
        document_ref: ErpnextDocumentRef,
        reason: RejectionReason,
        *,
        resolution_version: int | None = None,
    ) -> OutcomeAckRequest:
        """RT-331: an EXISTING ERP document does not match the work item's frozen resolution."""
        return cls(
            outcome="reconciliation_required",
            document_ref=document_ref,
            reason=reason,
            resolution_version=resolution_version,
        )

    # The version is echoed on EVERY outcome so Backend-Core can fence an attempt from a
    # superseded resolution (an operator re-resolved the intent meanwhile, RT-333).
    @classmethod
    def failed_transient(cls, *, resolution_version: int | None = None) -> OutcomeAckRequest:
        return cls(outcome="failed_transient", resolution_version=resolution_version)

    @classmethod
    def permanently_rejected(
        cls, reason: RejectionReason, *, resolution_version: int | None = None
    ) -> OutcomeAckRequest:
        return cls(outcome="permanently_rejected", reason=reason, resolution_version=resolution_version)

    def to_wire(self) -> dict[str, object]:
        wire: dict[str, object] = {"outcome": self.outcome}
        if self.document_ref is not None:
            wire["documentRef"] = self.document_ref.to_wire()
        if self.reason is not None:
            wire["reason"] = self.reason.to_wire()
        if self.resolution_version is not None:
            wire["resolutionVersion"] = self.resolution_version
        return wire
