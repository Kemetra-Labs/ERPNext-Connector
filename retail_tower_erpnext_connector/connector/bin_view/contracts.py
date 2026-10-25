# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Frozen read-models for the fixed 019 stock-view (Bin) contract.

These mirror Data-Pulse-2's ``stock-view.yaml`` (pinned to 1.4.0-draft, RT-875) 1:1. The
contract is DP2-owned and read-only (Principle I/IV) — these DTOs CITE it, never re-derive it.

Wire shapes consumed/produced here:
  - ``BinViewRequest`` (pulled): one wanted Bin read for a store's mapped warehouse,
    correlated to a 017 run. ``itemWindow.maxItems`` (≤500) bounds ONE report window;
    the optional v1.2 ``itemWindow.maxWindows`` (absent/1 = v1 single window) lets the
    connector page its read into up to ``maxWindows`` windows (RT-176);
  - ``BinEntry`` (produced): one ERPNext Item's on-hand QUANTITY (exact-decimal
    string, NEVER a float — Principle III) + its ``stockUom``; NO valuation;
  - ``BinViewReportWindow`` (produced, v1.2): ``{attemptRef, windowSeq, isFinal}`` —
    sent ONLY when the request advertised ``maxWindows > 1``;
  - ``BinViewSnapshotReport`` (produced): the per-item snapshot (or one window of it) +
    the connector ``readAt`` clock;
  - ``RecordedBinView`` (consumed): DP2's acknowledgement, incl. the optional v1.2
    ``windowSeq`` / ``windowsRecorded`` / ``complete`` progress fields.

Every ``from_wire`` reads only the keys it knows, so additive DP2 fields are ignored.

This module imports NO frappe — pure-Python core, unit-tested locally.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class BinViewItemWindow:
    """019 ``BinViewItemWindow`` — the item scope of one request.

    v1 (``max_windows`` absent or 1): one window of at most ``max_items`` (≤500) items;
    the connector treats the bounds as opaque scoping. v1.2 (``max_windows > 1``): the
    connector pages its read into up to ``max_windows`` report windows of at most
    ``max_items`` entries each and owns their boundaries (``window_seq`` is 0 and the
    bounds are null on such a request — schema-enforced by DP2).
    """

    window_seq: int
    max_items: int
    from_item_ref: str | None
    to_item_ref: str | None
    max_windows: int | None = None

    @property
    def is_paged(self) -> bool:
        """True when the request lets the connector page its read (``maxWindows > 1``)."""
        return self.max_windows is not None and self.max_windows > 1

    @classmethod
    def from_wire(cls, wire: Mapping[str, object]) -> BinViewItemWindow:
        return cls(
            window_seq=int(wire["windowSeq"]),  # type: ignore[arg-type]
            max_items=int(wire["maxItems"]),  # type: ignore[arg-type]
            from_item_ref=(None if wire.get("fromItemRef") is None else str(wire["fromItemRef"])),
            to_item_ref=(None if wire.get("toItemRef") is None else str(wire["toItemRef"])),
            max_windows=(None if wire.get("maxWindows") is None else int(wire["maxWindows"])),  # type: ignore[arg-type]
        )

    def to_wire(self) -> dict[str, object]:
        """The pulled wire shape (used to persist a request in the poller's retry set)."""
        wire: dict[str, object] = {
            "windowSeq": self.window_seq,
            "maxItems": self.max_items,
            "fromItemRef": self.from_item_ref,
            "toItemRef": self.to_item_ref,
        }
        if self.max_windows is not None:
            wire["maxWindows"] = self.max_windows
        return wire


@dataclass(frozen=True)
class BinViewRequest:
    """019 ``BinViewRequest`` — one wanted ERPNext-Bin read (DP2 → connector)."""

    request_ref: str
    store_id: str
    erpnext_warehouse_ref: str
    run_ref: str
    item_window: BinViewItemWindow
    item_cursor: str

    @classmethod
    def from_wire(cls, wire: Mapping[str, object]) -> BinViewRequest:
        return cls(
            request_ref=str(wire["requestRef"]),
            store_id=str(wire["storeId"]),
            erpnext_warehouse_ref=str(wire["erpnextWarehouseRef"]),
            run_ref=str(wire["runRef"]),
            item_window=BinViewItemWindow.from_wire(wire["itemWindow"]),  # type: ignore[arg-type]
            item_cursor=str(wire["itemCursor"]),
        )

    def to_wire(self) -> dict[str, object]:
        """The pulled wire shape (round-trips through :meth:`from_wire`)."""
        return {
            "requestRef": self.request_ref,
            "storeId": self.store_id,
            "erpnextWarehouseRef": self.erpnext_warehouse_ref,
            "runRef": self.run_ref,
            "itemWindow": self.item_window.to_wire(),
            "itemCursor": self.item_cursor,
        }


@dataclass(frozen=True)
class BinEntry:
    """019 ``BinEntry`` — one Item's on-hand quantity, exact-decimal string, NO valuation."""

    erpnext_item_ref: str
    quantity: str
    stock_uom: str

    def to_wire(self) -> dict[str, object]:
        return {
            "erpnextItemRef": {"doctype": "Item", "name": self.erpnext_item_ref},
            "quantity": self.quantity,
            "stockUom": self.stock_uom,
        }


@dataclass(frozen=True)
class BinViewReportWindow:
    """019 v1.2 ``BinViewReportWindow`` — one window of a connector-paged read.

    All windows of one read attempt share ``attempt_ref`` (and the report ``readAt``);
    ``window_seq`` runs 0..N in order and ``is_final`` is true on window N only.
    """

    attempt_ref: str
    window_seq: int
    is_final: bool

    def to_wire(self) -> dict[str, object]:
        return {
            "attemptRef": self.attempt_ref,
            "windowSeq": self.window_seq,
            "isFinal": self.is_final,
        }


@dataclass(frozen=True)
class BinViewSnapshotReport:
    """019 ``BinViewSnapshotReport`` — the connector's point-in-time Bin snapshot.

    ``window`` is set only for a v1.2 connector-paged report; a v1 report carries no
    ``window`` key on the wire at all (v1 compatibility (iii)).
    """

    entries: tuple[BinEntry, ...]
    read_at: str
    window: BinViewReportWindow | None = None

    def to_wire(self) -> dict[str, object]:
        wire: dict[str, object] = {
            "entries": [e.to_wire() for e in self.entries],
            "readAt": self.read_at,
        }
        if self.window is not None:
            wire["window"] = self.window.to_wire()
        return wire


def _opt_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class RecordedBinView:
    """019 ``RecordedBinView`` — DP2's acknowledgement of one report (or report window).

    Tolerant read: every field is optional here (a legacy/simple transport may return an
    empty body) and unknown keys are ignored. The v1.2 progress fields are absent on a
    response to a v1 report and from a Backend-Core that predates multi-window support;
    ``complete`` absent means a v1 single-window report, which is complete.
    """

    request_ref: str | None
    accepted_entry_count: int | None
    window_seq: int | None
    windows_recorded: int | None
    complete: bool | None

    @classmethod
    def from_wire(cls, wire: Mapping[str, object] | None) -> RecordedBinView:
        wire = wire if isinstance(wire, Mapping) else {}
        complete = wire.get("complete")
        return cls(
            request_ref=(None if wire.get("requestRef") is None else str(wire["requestRef"])),
            accepted_entry_count=_opt_int(wire.get("acceptedEntryCount")),
            window_seq=_opt_int(wire.get("windowSeq")),
            windows_recorded=_opt_int(wire.get("windowsRecorded")),
            complete=complete if isinstance(complete, bool) else None,
        )
