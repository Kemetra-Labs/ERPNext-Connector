# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Pure Connector Settings parsers (frappe-free).

The poller's ``_load_*`` stubs read Connector Settings child tables (a frappe-coupled, bench-only
act via ``frappe.get_doc``) and then transform the raw DocType rows into the dict shapes
:class:`uom.UomMap` / :class:`uom.PreResolvedWarehouse` / :class:`uom.StoreCustomerMap` consume.

That TRANSFORM is split out here as pure functions so the branching logic (empty rows, blank
values, duplicate keys) is unit-testable WITHOUT frappe — leaving only a three-line frappe read in
the poller shell. Each row is a plain mapping (a Frappe child-table row exposes ``.get(field)`` and
is dict-like) or a Document with field attributes; we read by field name either way.

Fail-loud philosophy (Principle VI): a half-filled or duplicate config row is a CONFIG error
surfaced at load time, never a silent skip — a silently-dropped mapping would later masquerade as a
data problem (an "unmapped unit/store" rejection on a sale that was actually mis-configured).

This module imports NO frappe.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping


class ConfigError(Exception):
    """A Connector Settings row is malformed (blank field or duplicate key) — fail loud."""


def _row_get(row: object, field: str) -> str:
    """Read ``field`` from a child-table row (dict-like or attr-like), as a stripped string."""
    if isinstance(row, Mapping):
        value = row.get(field)
    else:  # a Frappe Document row exposes fields as attributes
        value = getattr(row, field, None)
    return "" if value is None else str(value).strip()


def _parse_pairs(
    rows: Iterable | None, key_field: str, value_field: str, label: str
) -> dict[str, str]:
    """Parse child rows into a ``{key: value}`` dict; raise on a blank field or duplicate key."""
    out: dict[str, str] = {}
    for row in rows or []:
        key = _row_get(row, key_field)
        value = _row_get(row, value_field)
        if not key or not value:
            raise ConfigError(
                f"{label} row is incomplete: {key_field}={key!r}, {value_field}={value!r} "
                "(both are required — fix the Connector Settings row, do not leave it half-filled)"
            )
        if key in out:
            raise ConfigError(
                f"{label} has a duplicate {key_field}={key!r} — ambiguous mapping "
                "(remove the duplicate row; the connector never silently picks last-wins)"
            )
        out[key] = value
    return out


def parse_uom_map(rows: Iterable | None) -> dict[str, str]:
    """Child rows ``{dp2_unit, erpnext_uom}`` → ``{unit: uom}`` for :class:`uom.UomMap`."""
    return _parse_pairs(rows, "dp2_unit", "erpnext_uom", "UOM map")


def parse_warehouse_map(rows: Iterable | None) -> dict[str, dict]:
    """Child rows ``{store_id, warehouse}`` → ``{store_id: {doctype, name}}`` for the applier.

    Addresses the warehouse generically as ``{doctype:"Warehouse", name}`` (Principle II, FR-002).

    RT-434 (RT-290 M-2/M-3): a Warehouse belongs to at most one store. One Warehouse mapped for two
    stores would post both stores' stock into one Bin, so it is a config error naming the stores.
    Names compare case-insensitively: Frappe document names are case-insensitive on MariaDB.
    """
    pairs = _parse_pairs(rows, "store_id", "warehouse", "warehouse map")
    stores_by_warehouse: dict[str, list[str]] = {}
    first_spelling: dict[str, str] = {}
    for store, name in pairs.items():
        stores_by_warehouse.setdefault(name.casefold(), []).append(store)
        first_spelling.setdefault(name.casefold(), name)
    for key, stores in stores_by_warehouse.items():
        if len(stores) > 1:
            name = first_spelling[key]
            raise ConfigError(
                f"warehouse map has warehouse {name!r} for more than one store: {sorted(stores)} — a "
                "Warehouse belongs to at most one store (RT-290 M-2/M-3); map each store to its own "
                "Warehouse"
            )
    return {store: {"doctype": "Warehouse", "name": name} for store, name in pairs.items()}


def parse_store_customer_map(rows: Iterable | None) -> dict[str, str]:
    """Child rows ``{store_id, customer}`` → ``{store_id: customer}`` for the customer map (F-009)."""
    return _parse_pairs(rows, "store_id", "customer", "store→customer map")


def parse_tender_mode_map(rows: Iterable | None) -> dict[str, str]:
    """Child rows ``{tender_method, mode_of_payment}`` → ``{method: Mode of Payment}`` (RT-78, RT-10 D5).

    Not a REQUIRED map (see :func:`require_configured_maps`): while sales are tender-unknown an
    empty map is legitimate, and a tender-bearing sale then fails closed per item. A method outside
    the posting-feed enum is a config error (the Select field should have prevented it) — fail loud.
    """
    from .contracts import TENDER_METHODS

    pairs = _parse_pairs(rows, "tender_method", "mode_of_payment", "tender map")
    unknown = sorted(set(pairs) - TENDER_METHODS)
    if unknown:
        raise ConfigError(
            f"tender map has unknown tender_method(s) {unknown} — allowed: {sorted(TENDER_METHODS)}"
        )
    return pairs


def require_configured_maps(
    *, uom: Mapping, warehouse: Mapping, customer: Mapping
) -> None:
    """Raise :class:`ConfigError` naming any REQUIRED map that is empty (Codex PR #23 P1).

    Every posting work-item carries a store and ≥1 line (012 ``minItems: 1``), and the builder
    resolves all three maps unconditionally — so an EMPTY map guarantees 100% rejection, which is
    indistinguishable from "not yet configured". If the operator saved credentials before finishing
    the maps, activating the poll would PERMANENTLY reject every pending sale (terminal
    ``permanently_rejected``/``validation`` → DP2 DLQ, recoverable only via 017) — a Principle VI
    inversion (a config gap masquerading as a data rejection).

    So a fully-empty required map is treated as "not configured": this raises, the poller's
    ``_build_posting_path`` lets it propagate, and ``run_posting_poll`` logs ``posting.poll.skipped``
    and skips the tick — sales stay ``pending`` until the connector is fully configured.

    IMPORTANT — this gates ONLY the EMPTY (unconfigured) state. A NON-empty map with a specific key
    absent (store B unmapped while A is live) is a GENUINE terminal rejection (decision-table rows
    5/8) and is left to the per-item ``UnmappedUnit``/``UnmappedStore`` path in ``frappe_glue``,
    UNCHANGED — this helper never inspects individual keys.
    """
    empty = [name for name, m in (("uom", uom), ("warehouse", warehouse), ("customer", customer)) if not m]
    if empty:
        raise ConfigError(
            "Connector Settings: required map(s) not configured: "
            + ", ".join(empty)
            + " — refusing to activate the poller (would mass-reject pending sales). "
            "Finish the mapping table(s); the poll skips until then."
        )
