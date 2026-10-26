# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""UOM mapping, warehouse application, and money conformance (T070/T071/T072).

UOM (FR-008, signed decision ``docs/decisions/mapping-uom.md`` Option A): the connector maps
each DP2 free-text ``unit`` to an ERPNext UOM via a configured map. An unmapped unit fails
closed (:class:`builder.UnmappedUnit`) → ``permanently_rejected`` / ``validation`` — never a
silent default (Principle VI). The map is **config**, injected — not hardcoded.

Warehouse (FR-010, rider R5): apply the DP2-pre-resolved warehouse identity generically
``{doctype, name}``; never derive/guess. A store with no pre-resolved warehouse should have
failed-to-DLQ in DP2 — if it reaches here, fail closed.

Money (FR-009): assert every monetary field on a built doc is an exact-decimal string paired
with an ISO-4217 currency code (never a float). This ASSERTS the builder's output; it does
not re-build (no second money path — T072). This module imports NO frappe.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from .builder import UnmappedUnit

# Money fields mirror the 012 DecimalAmount pattern — at most 4 fractional digits (F-006).
_DECIMAL_RE = re.compile(r"^-?\d{1,15}(\.\d{1,4})?$", re.ASCII)
# Quantity mirrors the 012 SaleLine.quantity pattern — up to SIX fractional digits (weighed
# goods, e.g. "1.234567" kg) and unsigned. It must NOT be validated under the 4-digit money
# regex, which would false-reject a valid sale (a Principle VI inversion).
_QUANTITY_RE = re.compile(r"^\d{1,15}(\.\d{1,6})?$", re.ASCII)
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class UomMap:
    """Signed Option-A connector-side unit→ERPNext-UOM map (fail-closed on a miss)."""

    def __init__(self, mapping: Mapping[str, str]) -> None:
        self._map = dict(mapping)

    def resolve(self, unit: str) -> str:
        """Return the ERPNext UOM for ``unit``; raise :class:`UnmappedUnit` if absent.

        No normalization/casefolding — an unmapped or differently-cased unit fails closed
        rather than being silently guessed (Principle VI).
        """
        try:
            return self._map[unit]
        except KeyError:
            raise UnmappedUnit(unit) from None


class UnresolvedWarehouse(Exception):
    """A store has no DP2-pre-resolved warehouse (should have DLQ'd upstream — rider R5)."""

    def __init__(self, store_id: str) -> None:
        super().__init__(
            f"store {store_id!r} has no pre-resolved warehouse — upstream contract violation "
            "(R5: a missing warehouse fails-to-DLQ in DP2; the connector never guesses)"
        )
        self.store_id = store_id


class PreResolvedWarehouse:
    """Applies the DP2-pre-resolved store→warehouse identity (never derives one)."""

    def __init__(self, by_store: Mapping[str, dict]) -> None:
        self._by_store = dict(by_store)

    def for_store(self, store_id: str) -> dict:
        try:
            return self._by_store[store_id]
        except KeyError:
            raise UnresolvedWarehouse(store_id) from None


class UnmappedStore(Exception):
    """A store has no operator-configured ERPNext Customer (F-009 fail-closed).

    The 012 work-item carries no customer identity, only ``Sale.store_id``. The operator maps
    each store to its ERPNext Customer (config, injected) — exactly parallel to the warehouse
    map. A store with no mapping fails closed → ``permanently_rejected`` / ``validation`` (the
    connector NEVER fabricates a default customer; that would hide the config gap — Principle VI).
    """

    def __init__(self, store_id: str) -> None:
        super().__init__(
            f"store {store_id!r} has no configured ERPNext Customer — fail-closed → validation "
            "(F-009: the connector never invents a customer; configure the store→customer map)"
        )
        self.store_id = store_id


class StoreCustomerMap:
    """Applies the operator-configured store→ERPNext-Customer map (fail-closed on a miss)."""

    def __init__(self, by_store: Mapping[str, str]) -> None:
        self._by_store = dict(by_store)

    def for_store(self, store_id: str) -> str:
        """Return the ERPNext Customer for ``store_id``; raise :class:`UnmappedStore` if absent.

        No normalization — an unmapped or differently-cased store id fails closed rather than
        being silently posted to a fallback customer (Principle VI).
        """
        try:
            return self._by_store[store_id]
        except KeyError:
            raise UnmappedStore(store_id) from None


class MoneyConformanceError(Exception):
    """A built doc carries a non-conformant monetary value (float, missing/invalid currency)."""


def assert_money_conformance(doc: Mapping[str, object]) -> None:
    """Assert every monetary field on ``doc`` is exact-decimal string + ISO-4217 (FR-009).

    Raises :class:`MoneyConformanceError` on a float, a non-decimal string, a missing
    top-level ``currency``, or a non-ISO-4217 currency code.
    """
    currency = doc.get("currency")
    if not isinstance(currency, str) or not _CURRENCY_RE.match(currency):
        raise MoneyConformanceError(f"doc currency must be ISO-4217, got {currency!r}")

    items = doc.get("items") or []
    for idx, item in enumerate(items):  # type: ignore[assignment]
        _assert_item_money(idx, item)

    # RT-78: settlement payment amounts are money too — exact-decimal strings, never float.
    for idx, payment in enumerate(doc.get("payments") or []):  # type: ignore[assignment]
        _assert_payment_money(idx, payment)


def _assert_item_money(idx: int, item: Mapping[str, object]) -> None:
    """One item's money fields (``rate``/``price_list_rate``/``discount_amount``/``amount``) and ``qty`` conform."""
    # rate/price_list_rate/discount_amount (RT-857)/amount → DecimalAmount (≤4 fractional); qty →
    # quantity (≤6 fractional). All must be exact-decimal STRINGS, never float (FR-009).
    for field, pattern in (
        ("rate", _DECIMAL_RE),
        ("price_list_rate", _DECIMAL_RE),
        ("discount_amount", _DECIMAL_RE),
        ("amount", _DECIMAL_RE),
        ("qty", _QUANTITY_RE),
    ):
        if field not in item:
            continue
        value = item[field]
        if isinstance(value, float):
            raise MoneyConformanceError(f"items[{idx}].{field} is a float ({value!r}) — FR-009")
        if not isinstance(value, str) or not pattern.match(value):
            raise MoneyConformanceError(f"items[{idx}].{field} must be an exact-decimal string, got {value!r}")


def _assert_payment_money(idx: int, payment: Mapping[str, object]) -> None:
    """One settlement payment's ``amount`` is an exact-decimal string."""
    value = payment.get("amount")
    if isinstance(value, float):
        raise MoneyConformanceError(f"payments[{idx}].amount is a float ({value!r}) — FR-009")
    if not isinstance(value, str) or not _DECIMAL_RE.match(value):
        raise MoneyConformanceError(f"payments[{idx}].amount must be an exact-decimal string, got {value!r}")
