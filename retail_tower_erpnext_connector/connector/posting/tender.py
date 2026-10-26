# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Tender → ERPNext Mode of Payment resolution (RT-78, RT-10 D5).

The operator maps each posting-feed tender method to an ERPNext Mode of Payment in Connector
Settings (config, injected — never hardcoded). An unmapped method fails closed
(:class:`UnmappedTender`) → ``permanently_rejected`` / ``validation``: there is no default Mode of
Payment and the connector never guesses one or its account (Principle VI).

Settlement (RT-10 D3(b), amendment 011-DR-POSTING-A1): a tender-bearing sale posts as ONE Sales
Invoice carrying its own payments — one row per tender, amount verbatim. :func:`build_payments`
builds those rows. It fails closed when the tender total differs from the invoice total the
connector builds from the lines (amendment §4a: never adjust a line, invent change or leave a
partial balance to force a match) and never derives a tender from ``posTotal`` (R1).

This module imports NO frappe.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal

from .contracts import SaleTender


class UnmappedTender(Exception):
    """A tender method has no configured ERPNext Mode of Payment (RT-10 D5 fail-closed)."""

    def __init__(self, method: str) -> None:
        super().__init__(
            f"tender method {method!r} has no ERPNext Mode of Payment mapping — fail-closed → "
            "validation (RT-10 D5: configure the Connector Settings tender map; never guessed)"
        )
        self.method = method


class TenderMismatch(Exception):
    """The tenders do not sum to the invoice total built from the lines (amendment §4a) → validation.

    A reconciliation case: ``posTotal`` is kept verbatim at capture and may differ from the line sum
    (008 advisory mismatch flag); the connector never forces a match.
    """

    def __init__(self, tender_total: Decimal, line_total: Decimal) -> None:
        super().__init__(
            f"tender total {tender_total} != invoice line total {line_total} — rejected as validation "
            "(amendment 4a: no line adjustment, invented change or partial settlement)"
        )
        self.tender_total = tender_total
        self.line_total = line_total


class TenderModeMap:
    """Applies the operator-configured tender-method → Mode of Payment map (fail-closed on a miss)."""

    def __init__(self, mapping: Mapping[str, str]) -> None:
        self._map = dict(mapping)

    def resolve(self, method: str) -> str:
        """Return the ERPNext Mode of Payment for ``method``; raise :class:`UnmappedTender` if absent."""
        try:
            return self._map[method]
        except KeyError:
            raise UnmappedTender(method) from None


def build_payments(
    tenders: Sequence[SaleTender],
    *,
    line_amounts: Sequence[str],
    mode_of_payment_for: Callable[[str], str] | None,
) -> list[dict]:
    """The Sales Invoice ``payments`` rows for ``tenders``; ``[]`` for a tender-unknown sale (D8).

    Raises :class:`UnmappedTender` for an unmapped method (or when no resolver was wired — a
    tender-bearing sale never posts unpaid by omission) and :class:`TenderMismatch` when the tender
    total differs from ``Σ line_amounts``. Both compare as exact :class:`Decimal` (FR-009).
    """
    if not tenders:
        return []
    _assert_tenders_cover_lines(tenders, line_amounts)
    resolve = mode_of_payment_for or _unwired
    # RT-361 / RT-264 D4: one row per tender, same-method tenders included (never merged), in
    # sequence order. tenderRef and sequence stay off the document.
    return [{"mode_of_payment": resolve(t.method), "amount": t.amount} for t in ordered_tenders(tenders)]


def ordered_tenders(tenders: Sequence[SaleTender]) -> list[SaleTender]:
    """``tenders`` in payment-row order (RT-361): by ``sequence``, then those without one, in wire order.

    The feed contract builds the rows "in ``sequence`` order (tenders without it keep the order
    given)"; Backend-Core projects them ``ORDER BY sequence NULLS LAST``, so a tender without a
    sequence follows the sequenced ones. The sort is stable, so legacy tenders keep wire order.
    """
    return sorted(tenders, key=lambda t: (t.sequence is None, t.sequence or 0))


def tender_refs_in_row_order(tenders: Sequence[SaleTender]) -> list[str | None]:
    """The ``tenderRef`` of each payment row, by row position (RT-361 evidence; ``None`` = legacy)."""
    return [t.tender_ref for t in ordered_tenders(tenders)]


def _decimal_sum(amounts: Sequence[str]) -> Decimal:
    return sum((Decimal(a) for a in amounts), Decimal(0))


def _assert_tenders_cover_lines(tenders: Sequence[SaleTender], line_amounts: Sequence[str]) -> None:
    """Raise :class:`TenderMismatch` unless ``Σ tenders == Σ line_amounts`` exactly (amendment 4a)."""
    tender_total = _decimal_sum([t.amount for t in tenders])
    line_total = _decimal_sum(line_amounts)
    if tender_total != line_total:
        raise TenderMismatch(tender_total, line_total)


def _unwired(method: str) -> str:
    """No tender resolver was wired: a tender-bearing sale fails closed, never posts unpaid."""
    raise UnmappedTender(method)


class SettlementDrift(Exception):
    """ERPNext's computed totals do not settle the invoice exactly (amendment 4a) → validation.

    Raised between insert and submit: ERPNext recomputes totals on insert (currency precision,
    taxes, pricing rules), so the pre-build tender check alone cannot prove the posted invoice has
    no change, outstanding balance or write-off.
    """


SETTLED_FIELDS = ("grand_total", "paid_amount", "change_amount", "write_off_amount", "outstanding_amount")


def payments_total(payments: Sequence[Mapping[str, object]]) -> str:
    """The exact total of the requested payment rows (exact-decimal strings), as a string."""
    return str(sum((Decimal(str(p["amount"])) for p in payments), Decimal(0)))


def assert_settled(totals: Mapping[str, object], *, expected_total: str | None = None) -> None:
    """Raise :class:`SettlementDrift` unless ``paid_amount == grand_total`` with no residual.

    ``totals`` carries ERPNext's computed Sales Invoice values (floats from the document); each is
    compared as a :class:`Decimal` built from its string form. A missing value counts as zero.
    ``expected_total`` is the EXACT total the connector requested (:func:`payments_total`): when
    given, ``grand_total`` must equal it too, so ERPNext rounding both the rows and the payments to
    its currency precision (3.3333 -> 3.33) cannot pass as settled (Codex P2, PR #50).
    """
    values = {f: Decimal(str(totals.get(f) or 0)) for f in SETTLED_FIELDS}
    _assert_requested_total(values["grand_total"], expected_total)
    residual = {f: values[f] for f in ("change_amount", "write_off_amount", "outstanding_amount") if values[f]}
    if values["paid_amount"] != values["grand_total"] or residual:
        raise SettlementDrift(
            f"invoice does not settle exactly after ERPNext computed its totals: grand_total "
            f"{values['grand_total']}, paid_amount {values['paid_amount']}, residual {residual or 'none'} "
            "— rolled back, rejected as validation (amendment 4a)"
        )


def _assert_requested_total(grand_total: Decimal, expected_total: str | None) -> None:
    """ERPNext's computed total must equal the exact total the connector requested (no rounding)."""
    if expected_total is not None and grand_total != Decimal(expected_total):
        raise SettlementDrift(
            f"ERPNext computed grand_total {grand_total} != requested total {expected_total} "
            "(currency precision rounding) — rolled back, rejected as validation (amendment 4a)"
        )
