# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Frappe-backed :class:`~.transfer_flow.TransferErpPort` (RT-1514) — ⏳ BENCH-VALIDATION.

The ONLY transfer module that imports ``frappe``. It is a thin adapter: every decision (what is a refusal,
an echo, a duplicate, a failed Draft, a verification failure) lives in the pure ``transfer_flow``. It reads and
writes the NATIVE ERPNext v16 Stock Entry (Material Transfer, add to transit, and the receiving entry against
it) through the ORM; it never writes the ledger, the Bin or the GL, never edits or cancels a document, and
never sets a valuation rate.

Not run in this lane: there is no Frappe bench on the development machine (standing rules, no local bench
validation). The behaviours it relies on are read from the ERPNext v16.51.0 source (RT-291 planning record,
comment 17800) and are NOT yet proven on a bench:

  - ``add_to_transit`` with the target a Warehouse of type Transit in the same Company, and a receiving entry
    that names the dispatch through ``outgoing_stock_entry`` and each row through ``ste_detail`` and
    ``against_stock_entry``, so several receipts post against one dispatch;
  - the batch of a row posted with ``batch_no`` and ``use_serial_batch_fields`` living in the Serial and Batch
    Bundle that submit creates, and the sign of its entries (the read-back therefore takes the sign from the
    ledger entry, never from the bundle entry);
  - a Draft being locked while it is submitted and an ``Action Failed`` comment on one that failed.
"""

from __future__ import annotations

from decimal import Decimal

import frappe

from ..schema import index_present
from .count_flow import SiteStamp
from .frappe_count_port import _reads, classify_submit_failure
from .frappe_exceptions import _dup_provenance_exceptions, _transient_exceptions
from .index_guard import TRANSFER_INDEXES
from .transfer_flow import (
	STOCK_ENTRY,
	DispatchFacts,
	DispatchRow,
	ExistingTransfer,
	LedgerRow,
	TransferDuplicateProvenance,
	TransferItemFacts,
	TransferRow,
	TransferTransient,
)
from .warehouse_policy import FrozenWarehouseRejected, assert_postable_warehouse

_SAVEPOINT = "rt_transfer_submit"
_ROW_FIELD = "rt_transfer_line_ref"

__all__ = ["FrappeTransferPort", "classify_submit_failure"]


def _warehouse_row(warehouse: str) -> dict | None:
	return frappe.db.get_value(
		"Warehouse", warehouse, ["company", "is_group", "disabled", "warehouse_type"], as_dict=True
	)


class FrappeTransferPort:
	"""``TransferErpPort`` over the live site."""

	@_reads
	def indexes_ready(self) -> bool:
		"""The unique provenance index AND the row field a receipt is checked against must both exist."""
		for doctype, name, columns in TRANSFER_INDEXES:
			if not index_present(doctype, name, columns):
				return False
		return bool(frappe.db.has_column("Stock Entry Detail", _ROW_FIELD))

	@_reads
	def company(self) -> str:
		return frappe.db.get_single_value("Global Defaults", "default_company")

	@_reads
	def item_facts(self, item_codes: list[str]) -> dict[str, TransferItemFacts]:
		rows = frappe.get_all(
			"Item",
			filters={"name": ["in", item_codes]},
			fields=["name", "stock_uom", "has_batch_no", "has_serial_no"],
		)
		return {
			r["name"]: TransferItemFacts(
				exists=True,
				stock_uom=r["stock_uom"],
				has_batch_no=bool(int(r["has_batch_no"] or 0)),
				has_serial_no=bool(int(r["has_serial_no"] or 0)),
			)
			for r in rows
		}

	@_reads
	def batch_problem(self, item_code: str, batch: str, stamp: SiteStamp) -> str | None:
		row = frappe.db.get_value("Batch", batch, ["item", "disabled", "expiry_date"], as_dict=True)
		if row is None:
			return "the Batch does not exist on this site"
		if row["item"] != item_code:
			return f"the Batch belongs to Item {row['item']!r}"
		if int(row["disabled"] or 0):
			return "the Batch is disabled"
		if row["expiry_date"] and str(row["expiry_date"]) < stamp.posting_date:
			return f"the Batch expired on {row['expiry_date']}"
		return None

	@_reads
	def warehouse_problem(self, warehouse: str, company: str) -> str | None:
		row = _warehouse_row(warehouse)
		try:
			assert_postable_warehouse(warehouse, row, company)
		except FrozenWarehouseRejected as exc:
			return str(exc)
		if row is not None and int(row.get("disabled") or 0):
			return f"warehouse {warehouse!r} is disabled"
		return None

	@_reads
	def transit_warehouse_problem(self, warehouse: str, company: str) -> str | None:
		problem = self.warehouse_problem(warehouse, company)
		if problem:
			return problem
		row = _warehouse_row(warehouse)
		if row is None or row.get("warehouse_type") != "Transit":
			return f"warehouse {warehouse!r} is not a Transit warehouse"
		return None

	@_reads
	def existing_documents(self, source_system: str, external_id: str) -> list[ExistingTransfer]:
		"""Draft, submitted AND cancelled documents carrying the provenance key (never submitted only)."""
		heads = frappe.get_all(
			STOCK_ENTRY,
			filters={"rt_source_system": source_system, "rt_external_id": external_id},
			fields=[
				"name",
				"docstatus",
				"purpose",
				"add_to_transit",
				"outgoing_stock_entry",
				"posting_date",
				"posting_time",
			],
		)
		return [self._existing(head) for head in heads]

	def _existing(self, head: dict) -> ExistingTransfer:
		draft = int(head["docstatus"]) == 0
		return ExistingTransfer(
			name=head["name"],
			docstatus=int(head["docstatus"]),
			purpose=head["purpose"],
			add_to_transit=bool(int(head["add_to_transit"] or 0)),
			outgoing_stock_entry=head["outgoing_stock_entry"] or None,
			posting_date=str(head["posting_date"]),
			posting_time=str(head["posting_time"]),
			rows=tuple(
				TransferRow(
					r["item_code"],
					self._batch_of(r),
					str(r["qty"]),
					r["s_warehouse"] or "",
					r["t_warehouse"] or "",
				)
				for r in self._rows(head["name"])
			),
			in_flight=draft and self._locked(head["name"]),
			failure=self._failure_comment(head["name"]) if draft else None,
		)

	def _rows(self, name: str) -> list[dict]:
		return frappe.get_all(
			"Stock Entry Detail",
			filters={"parent": name},
			fields=[
				"name",
				"idx",
				"item_code",
				"qty",
				"batch_no",
				"serial_and_batch_bundle",
				"s_warehouse",
				"t_warehouse",
				"transferred_qty",
				_ROW_FIELD,
			],
			order_by="idx asc",
		)

	def _batch_of(self, row: dict) -> str | None:
		"""The row's Batch: its own field, else the single Batch of its Serial and Batch Bundle."""
		if row.get("batch_no"):
			return row["batch_no"]
		bundle = row.get("serial_and_batch_bundle")
		if not bundle:
			return None
		batches = {
			e["batch_no"]
			for e in frappe.get_all("Serial and Batch Entry", filters={"parent": bundle}, fields=["batch_no"])
			if e["batch_no"]
		}
		return next(iter(batches)) if len(batches) == 1 else None

	def _locked(self, name: str) -> bool:
		"""A Draft being submitted is locked (``queue_action`` locks it first); a failed one is not."""
		return bool(frappe.get_doc(STOCK_ENTRY, name).is_locked)

	def _failure_comment(self, name: str) -> str | None:
		rows = frappe.get_all(
			"Comment",
			filters={
				"reference_doctype": STOCK_ENTRY,
				"reference_name": name,
				"content": ["like", "%Action Failed%"],
			},
			fields=["content"],
			order_by="creation desc",
			limit=1,
		)
		return rows[0]["content"] if rows else None

	@_reads
	def dispatch_document(self, name: str) -> DispatchFacts | None:
		head = frappe.db.get_value(
			STOCK_ENTRY,
			name,
			["name", "docstatus", "purpose", "company", "add_to_transit"],
			as_dict=True,
		)
		if head is None:
			return None
		return DispatchFacts(
			name=head["name"],
			docstatus=int(head["docstatus"]),
			purpose=head["purpose"],
			company=head["company"],
			add_to_transit=bool(int(head["add_to_transit"] or 0)),
			rows=tuple(
				DispatchRow(
					idx=int(r["idx"]),
					row_name=r["name"],
					item_code=r["item_code"],
					batch=self._batch_of(r),
					qty=str(r["qty"]),
					transferred_qty=str(r["transferred_qty"] or 0),
					t_warehouse=r["t_warehouse"] or "",
					line_ref=r.get(_ROW_FIELD) or None,
				)
				for r in self._rows(name)
			),
		)

	def submit(self, payload: dict) -> str:
		"""Insert and submit one Stock Entry as a unit; any failure rolls it back (RT-48 style)."""
		frappe.db.savepoint(_SAVEPOINT)
		try:
			doc = frappe.get_doc(payload)
			doc.insert()
			doc.submit()
			return doc.name
		except BaseException as exc:
			self._rollback()
			raise self._typed(exc) from exc

	def _rollback(self) -> None:
		try:
			frappe.db.rollback(save_point=_SAVEPOINT)
		except Exception:
			frappe.db.rollback()  # a deadlock victim loses the savepoint with its transaction

	def _typed(self, exc: BaseException) -> BaseException:
		if isinstance(exc, _transient_exceptions()):
			return TransferTransient(str(exc))
		if isinstance(exc, _dup_provenance_exceptions()):
			return TransferDuplicateProvenance(str(exc))
		if isinstance(exc, Exception):
			return classify_submit_failure(exc)
		return exc

	@_reads
	def read_back(self, name: str) -> list[LedgerRow]:
		"""The document's live Stock Ledger Entries, one per Batch when the row carries a Serial and Batch Bundle."""
		entries = frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_type": STOCK_ENTRY, "voucher_no": name, "is_cancelled": 0},
			fields=["item_code", "warehouse", "actual_qty", "batch_no", "serial_and_batch_bundle"],
		)
		out: list[LedgerRow] = []
		for entry in entries:
			out.extend(self._ledger_rows(entry))
		return out

	def _ledger_rows(self, entry: dict) -> list[LedgerRow]:
		item, warehouse = entry["item_code"], entry["warehouse"]
		bundle = entry.get("serial_and_batch_bundle")
		if not bundle:
			return [LedgerRow(item, warehouse, str(entry["actual_qty"]), entry.get("batch_no") or None)]
		sign = -1 if Decimal(str(entry["actual_qty"])) < 0 else 1
		per_batch: dict[str | None, Decimal] = {}
		for e in frappe.get_all(
			"Serial and Batch Entry", filters={"parent": bundle}, fields=["batch_no", "qty"]
		):
			batch = e["batch_no"] or None
			per_batch[batch] = per_batch.get(batch, Decimal(0)) + abs(Decimal(str(e["qty"])))
		# The sign comes from the ledger entry, never from the bundle entry (its sign convention is not proven),
		# and the sum is exact: a float sum would turn a correctly posted leg into a false mismatch.
		return [LedgerRow(item, warehouse, str(sign * qty), batch) for batch, qty in per_batch.items()]

	def commit(self) -> None:
		frappe.db.commit()
