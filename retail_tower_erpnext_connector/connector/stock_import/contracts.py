# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Frozen read-models for Backend-Core's ``stock-import.yaml`` 1.0.0-draft (RT-1286).

The contract is Backend-Core-owned and read-only (Principle I/IV): these DTOs CITE it, never
re-derive it. Wire shapes consumed and produced here:

  - ``StockImportRequest`` (pulled): one wanted page of Stock Ledger facts at one mapped bucket
    warehouse within one import run, with the keyset lower bound ``after`` (a ``LedgerKey`` or
    null), ``settleSeconds``, ``overlapSeconds`` (informative) and ``maxRows`` (≤100);
  - ``StockImportRow`` (produced): one Stock Ledger Entry, or one Batch entry of its Serial and
    Batch Bundle. Facts only (G8-7), exact-decimal quantity, NO valuation field;
  - ``StockImportReport`` (produced): the rows in read order with ``readThrough``, ``exhausted``,
    ``readAt`` and an optional ``StockImportRefusal`` (``unknown_warehouse``);
  - ``RecordedStockImport`` (consumed): Backend-Core's acknowledgement.

Every ``from_wire`` reads only the keys it knows, so additive Backend-Core fields are ignored.
This module imports NO frappe: pure-Python core, unit-tested locally.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


def _opt_str(value: object) -> str | None:
	return None if value is None else str(value)


def _opt_int(value: object) -> int | None:
	if value is None:
		return None
	if isinstance(value, bool) or not isinstance(value, int):
		raise ValueError(f"expected an integer, got {value!r}")
	return value


@dataclass(frozen=True)
class LedgerKey:
	"""``LedgerKey`` — a position in the connector's read order at one warehouse.

	``creation`` is an RFC 3339 date-time with an offset. ``ledger_entry_ref`` is null only on
	page 0 of a run ("every row at ``creation`` or later"), and then ``entry_index`` is null too.
	"""

	creation: str
	ledger_entry_ref: str | None
	entry_index: int | None

	@classmethod
	def from_wire(cls, wire: Mapping[str, object]) -> LedgerKey:
		ref = _opt_str(wire.get("ledgerEntryRef"))
		index = _opt_int(wire.get("entryIndex"))
		if (ref is None) != (index is None):
			raise ValueError("LedgerKey: ledgerEntryRef and entryIndex must be null together")
		return cls(creation=str(wire["creation"]), ledger_entry_ref=ref, entry_index=index)

	def to_wire(self) -> dict[str, object]:
		return {
			"creation": self.creation,
			"ledgerEntryRef": self.ledger_entry_ref,
			"entryIndex": self.entry_index,
		}


@dataclass(frozen=True)
class StockImportRequest:
	"""``StockImportRequest`` — one wanted page of ledger facts (Backend-Core → connector)."""

	request_ref: str
	run_ref: str
	page_seq: int
	store_id: str
	erpnext_warehouse_ref: str
	after: LedgerKey | None
	settle_seconds: int
	overlap_seconds: int
	max_rows: int
	item_cursor: str

	@classmethod
	def from_wire(cls, wire: Mapping[str, object]) -> StockImportRequest:
		after = wire["after"]
		max_rows = int(wire["maxRows"])  # type: ignore[arg-type]
		settle = int(wire["settleSeconds"])  # type: ignore[arg-type]
		if not 1 <= max_rows <= 100:
			raise ValueError(f"StockImportRequest.maxRows out of range: {max_rows}")
		if not 0 <= settle <= 3600:
			raise ValueError(f"StockImportRequest.settleSeconds out of range: {settle}")
		return cls(
			request_ref=str(wire["requestRef"]),
			run_ref=str(wire["runRef"]),
			page_seq=int(wire["pageSeq"]),  # type: ignore[arg-type]
			store_id=str(wire["storeId"]),
			erpnext_warehouse_ref=str(wire["erpnextWarehouseRef"]),
			after=None if after is None else LedgerKey.from_wire(after),  # type: ignore[arg-type]
			settle_seconds=settle,
			overlap_seconds=int(wire["overlapSeconds"]),  # type: ignore[arg-type]
			max_rows=max_rows,
			item_cursor=str(wire["itemCursor"]),
		)

	def to_wire(self) -> dict[str, object]:
		"""The pulled wire shape (round-trips through :meth:`from_wire`)."""
		return {
			"requestRef": self.request_ref,
			"runRef": self.run_ref,
			"pageSeq": self.page_seq,
			"storeId": self.store_id,
			"erpnextWarehouseRef": self.erpnext_warehouse_ref,
			"after": None if self.after is None else self.after.to_wire(),
			"settleSeconds": self.settle_seconds,
			"overlapSeconds": self.overlap_seconds,
			"maxRows": self.max_rows,
			"itemCursor": self.item_cursor,
		}


@dataclass(frozen=True)
class StockImportRow:
	"""``StockImportRow`` — one Stock Ledger fact, every field present (null/false when n/a)."""

	ledger_entry_ref: str
	entry_index: int
	voucher_type: str
	voucher_no: str
	voucher_detail_no: str | None
	purpose: str | None
	is_return: bool
	return_against: str | None
	posting_date: str
	posting_time: str
	creation: str
	is_cancelled: bool
	actual_qty: str
	stock_uom: str
	erpnext_item_ref: str
	erpnext_batch_ref: str | None
	has_retail_provenance: bool
	correction_ref: str | None

	@property
	def key(self) -> LedgerKey:
		"""This row's position in the read order (its ``readThrough`` when it is the last row)."""
		return LedgerKey(self.creation, self.ledger_entry_ref, self.entry_index)

	def to_wire(self) -> dict[str, object]:
		return {
			"ledgerEntryRef": self.ledger_entry_ref,
			"entryIndex": self.entry_index,
			"voucherType": self.voucher_type,
			"voucherNo": self.voucher_no,
			"voucherDetailNo": self.voucher_detail_no,
			"purpose": self.purpose,
			"isReturn": self.is_return,
			"returnAgainst": self.return_against,
			"postingDate": self.posting_date,
			"postingTime": self.posting_time,
			"creation": self.creation,
			"isCancelled": self.is_cancelled,
			"actualQty": self.actual_qty,
			"stockUom": self.stock_uom,
			"erpnextItemRef": {"doctype": "Item", "name": self.erpnext_item_ref},
			"erpnextBatchRef": (
				None
				if self.erpnext_batch_ref is None
				else {"doctype": "Batch", "name": self.erpnext_batch_ref}
			),
			"hasRetailProvenance": self.has_retail_provenance,
			"correctionRef": self.correction_ref,
		}


@dataclass(frozen=True)
class StockImportRefusal:
	"""``StockImportRefusal`` — why the connector read nothing (``unknown_warehouse`` only)."""

	reason: str

	def to_wire(self) -> dict[str, object]:
		return {"reason": self.reason}


@dataclass(frozen=True)
class StockImportReport:
	"""``StockImportReport`` — the rows read for one request, in read order, or a refusal."""

	rows: tuple[StockImportRow, ...]
	read_through: LedgerKey | None
	exhausted: bool
	read_at: str
	refusal: StockImportRefusal | None = None

	def to_wire(self) -> dict[str, object]:
		return {
			"rows": [row.to_wire() for row in self.rows],
			"readThrough": None if self.read_through is None else self.read_through.to_wire(),
			"exhausted": self.exhausted,
			"readAt": self.read_at,
			"refusal": None if self.refusal is None else self.refusal.to_wire(),
		}


@dataclass(frozen=True)
class RecordedStockImport:
	"""``RecordedStockImport`` — Backend-Core's acknowledgement. Tolerant read (all optional)."""

	request_ref: str | None
	recorded_row_count: int | None
	exhausted: bool | None
	refused: bool | None

	@classmethod
	def from_wire(cls, wire: Mapping[str, object] | None) -> RecordedStockImport:
		wire = wire if isinstance(wire, Mapping) else {}
		count = wire.get("recordedRowCount")
		exhausted = wire.get("exhausted")
		refused = wire.get("refused")
		return cls(
			request_ref=_opt_str(wire.get("requestRef")),
			recorded_row_count=count if isinstance(count, int) and not isinstance(count, bool) else None,
			exhausted=exhausted if isinstance(exhausted, bool) else None,
			refused=refused if isinstance(refused, bool) else None,
		)
