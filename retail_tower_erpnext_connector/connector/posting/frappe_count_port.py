# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Frappe-backed :class:`~.count_flow.CountErpPort` (RT-1422) — ⏳ BENCH-VALIDATION.

The ONLY count module that imports ``frappe``. It is a thin adapter: every decision (what is a
refusal, a no-op, a duplicate, a failed Draft, a verification failure) lives in the pure
``count_flow``. It reads and writes the NATIVE ERPNext v16 Stock Reconciliation through the ORM and
the stock utilities; it never writes the ledger, the Bin or the GL, never edits or cancels a document,
and never sets a valuation rate.

Not run in this lane: there is no Frappe bench on the development machine (standing rules, no local
bench validation). The behaviours it relies on are the ones proven on the disposable ERPNext 16.51.0 /
Frappe 16.52.0 bench of RT-292 comment 17218: a count at the cut absorbs a late pre-cut sale; the Bin is
correct immediately after a back-dated submit while value and GL lag until the repost job; a freeze or a
missing back-dated permission refuses explicitly before any document is created; a Draft that is in
flight is locked, one that failed is not and carries an ``Action Failed`` comment.
"""

from __future__ import annotations

import functools
from decimal import Decimal

import frappe

from ..schema import index_present
from .count_flow import (
	STOCK_RECONCILIATION,
	CountDuplicateProvenance,
	CountRefused,
	CountTransient,
	ExistingCount,
	ItemFacts,
	LineReadBack,
	SiteStamp,
)
from .frappe_exceptions import _dup_provenance_exceptions, _transient_exceptions
from .index_guard import COUNT_INDEXES
from .posting_time import format_time
from .warehouse_policy import FrozenWarehouseRejected, assert_postable_warehouse

_SAVEPOINT = "rt_count_submit"
_FREEZE_MARKERS = ("frozen", "freeze", "stock transactions before")
_BACKDATE_MARKERS = ("back dated", "backdated", "back-dated", "stock transaction")


def _reads(method):
	"""A read that fails transiently (lock, timeout, deadlock) is ``CountTransient``, never an escape."""

	@functools.wraps(method)
	def wrapper(self, *args, **kwargs):
		try:
			return method(self, *args, **kwargs)
		except _transient_exceptions() as exc:
			raise CountTransient(f"{type(exc).__name__}: {exc}") from exc

	return wrapper


def classify_submit_failure(exc: BaseException) -> Exception:
	"""Map an ERPNext submit failure onto the leg's typed errors (freeze, permission, validation)."""
	text = f"{type(exc).__name__}: {exc}"
	lowered = text.lower()
	if isinstance(exc, frappe.PermissionError) or (
		any(m in lowered for m in _BACKDATE_MARKERS) and "role" in lowered
	):
		return CountRefused("back_dated_permission", text)
	if any(m in lowered for m in _FREEZE_MARKERS):
		return CountRefused("closed_period", text)
	if "valuation rate" in lowered:
		return CountRefused("no_valuation_history", text)
	return CountRefused("invalid_shape", text)


def _order(entry: dict) -> tuple[str, str, str]:
	"""A ledger entry's position in the ledger: posting date, normalised posting time, creation."""
	return (str(entry["posting_date"]), format_time(entry["posting_time"]), str(entry["creation"]))


class FrappeCountPort:
	"""``CountErpPort`` over the live site."""

	@_reads
	def indexes_ready(self) -> bool:
		return all(index_present(dt, name, cols) for dt, name, cols in COUNT_INDEXES)

	@_reads
	def company(self) -> str:
		company = frappe.db.get_single_value("Global Defaults", "default_company")
		if not company:
			raise CountRefused("invalid_shape", "the site default company is not configured")
		return company

	@_reads
	def item_facts(self, item_codes: list[str]) -> dict[str, ItemFacts]:
		rows = frappe.get_all(
			"Item",
			filters={"name": ["in", item_codes]},
			fields=["name", "stock_uom", "has_batch_no", "has_serial_no"],
		)
		with_history = {
			r["item_code"]
			for r in frappe.get_all(
				"Stock Ledger Entry",
				filters={"item_code": ["in", item_codes], "is_cancelled": 0},
				fields=["item_code"],
				group_by="item_code",
			)
		}
		return {
			r["name"]: ItemFacts(
				exists=True,
				stock_uom=r["stock_uom"],
				tracked=bool(r["has_batch_no"] or r["has_serial_no"]),
				has_history=r["name"] in with_history,
			)
			for r in rows
		}

	@_reads
	def warehouse_problem(self, warehouse: str, company: str) -> str | None:
		row = frappe.db.get_value("Warehouse", warehouse, ["company", "is_group"], as_dict=True)
		try:
			assert_postable_warehouse(warehouse, row, company)
		except FrozenWarehouseRejected as exc:
			return str(exc)
		return None

	@_reads
	def existing_documents(self, source_system: str, external_id: str) -> list[ExistingCount]:
		"""Draft, submitted AND cancelled documents carrying the provenance key (never submitted only)."""
		heads = frappe.get_all(
			STOCK_RECONCILIATION,
			filters={"rt_source_system": source_system, "rt_external_id": external_id},
			fields=["name", "docstatus", "posting_date", "posting_time"],
		)
		return [self._existing(head) for head in heads]

	def _existing(self, head: dict) -> ExistingCount:
		rows = frappe.get_all(
			"Stock Reconciliation Item",
			filters={"parent": head["name"]},
			fields=["item_code", "warehouse", "qty"],
			order_by="idx asc",
		)
		draft = int(head["docstatus"]) == 0
		return ExistingCount(
			name=head["name"],
			docstatus=int(head["docstatus"]),
			posting_date=str(head["posting_date"]),
			posting_time=str(head["posting_time"]),
			rows=tuple((r["item_code"], r["warehouse"], str(r["qty"])) for r in rows),
			in_flight=draft and self._locked(head["name"]),
			failure=self._failure_comment(head["name"]) if draft else None,
		)

	def _locked(self, name: str) -> bool:
		"""A Draft being submitted is locked (``queue_action`` locks it first); a failed one is not."""
		return bool(frappe.get_doc(STOCK_RECONCILIATION, name).is_locked)

	def _failure_comment(self, name: str) -> str | None:
		rows = frappe.get_all(
			"Comment",
			filters={
				"reference_doctype": STOCK_RECONCILIATION,
				"reference_name": name,
				"content": ["like", "%Action Failed%"],
			},
			fields=["content"],
			order_by="creation desc",
			limit=1,
		)
		return rows[0]["content"] if rows else None

	@_reads
	def balance_at(self, item_code: str, warehouse: str, stamp: SiteStamp) -> Decimal:
		from erpnext.stock.stock_ledger import get_previous_sle

		sle = get_previous_sle(
			{
				"item_code": item_code,
				"warehouse": warehouse,
				"posting_date": stamp.posting_date,
				"posting_time": stamp.posting_time,
			}
		)
		return Decimal(str((sle or {}).get("qty_after_transaction") or 0))

	def submit(self, payload: dict) -> str:
		"""Insert and submit one Stock Reconciliation as a unit; any failure rolls it back (RT-48 style)."""
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
			return CountTransient(str(exc))
		if isinstance(exc, _dup_provenance_exceptions()):
			return CountDuplicateProvenance(str(exc))
		if isinstance(exc, Exception):
			return classify_submit_failure(exc)
		return exc

	@_reads
	def read_back(self, name: str, rows: list[tuple[str, str]]) -> list[LineReadBack]:
		ledger = frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_type": STOCK_RECONCILIATION, "voucher_no": name, "is_cancelled": 0},
			fields=[
				"item_code",
				"warehouse",
				"qty_after_transaction",
				"posting_date",
				"posting_time",
				"creation",
			],
		)
		by_key = {(e["item_code"], e["warehouse"]): e for e in ledger}
		return [self._line(name, by_key[key]) for key in rows if key in by_key]

	def _line(self, name: str, entry: dict) -> LineReadBack:
		item, warehouse = entry["item_code"], entry["warehouse"]
		bin_qty = frappe.db.get_value("Bin", {"item_code": item, "warehouse": warehouse}, "actual_qty") or 0
		later = frappe.get_all(
			"Stock Ledger Entry",
			filters={
				"item_code": item,
				"warehouse": warehouse,
				"is_cancelled": 0,
				"voucher_no": ["!=", name],
				"posting_date": [">=", entry["posting_date"]],
			},
			fields=["actual_qty", "posting_date", "posting_time", "creation"],
		)
		ours = _order(entry)
		moved = sum(Decimal(str(e["actual_qty"])) for e in later if _order(e) > ours)
		return LineReadBack(
			item_code=item,
			warehouse=warehouse,
			qty_after=str(entry["qty_after_transaction"]),
			bin_qty=str(bin_qty),
			later_movement=str(moved),
		)

	def commit(self) -> None:
		frappe.db.commit()
