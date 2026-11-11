# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Frappe glue for the ERP-origin stock import reader (RT-1287): BENCH-VALIDATION.

:class:`FrappeLedgerSource` implements :class:`.reader.LedgerSource` over ERPNext v16
(Frappe v16.52.0 / ERPNext v16.51.0, the pinned bench). It imports ``frappe``, so it is
validated on a bench, not locally (standing-rules §6); the read rule itself lives in
:mod:`.reader` (unit-tested locally).

Reads (QUANTITY and facts only, no valuation field):

* ``Stock Ledger Entry`` at the warehouse, ``docstatus`` 1 (submitted; cancelled rows and the
  reverse rows of a cancellation are flagged ``is_cancelled`` and still read), keyset
  (``creation``, ``name``) in SQL so the database collation orders and compares the names, and
  ``creation`` ≤ the settle cutoff;
* ``Serial and Batch Entry`` of each row's ``serial_and_batch_bundle``, by ``idx``;
* the voucher's ``purpose``, ``is_return``, ``return_against``, Retail provenance
  (``rt_source_system`` / ``rt_external_id``; a reversal carries its ``work_item_ref`` in
  ``rt_external_id``, #28 re-key) and the governed correction reference (:data:`CORRECTION_REF_FIELD`)
  — each only when the voucher DocType has that column, else its "does not apply" value.

The settle clock is ``frappe.utils.now_datetime()``: the site clock that stamps ``creation``
(naive, in the site's system time zone), not the database server's ``NOW()``.

The correction reference (Q3) has no Custom Field yet: creating one on Stock Entry and Stock
Reconciliation is a fixtures / ``hooks.py`` change (a gated surface) and belongs with the governed
break-glass path. Until it exists :func:`voucher_facts` reports ``correctionRef`` null, which
Backend-Core treats as "no reference" (a finding, never an import).
"""

from __future__ import annotations

from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo

import frappe

from .reader import LedgerStart, RawBundleEntry, RawLedgerEntry, VoucherFacts

# Site config switch (``bench --site <site> set-config``). Absent or falsy → the poller does
# nothing. Turn it on for a site only once that environment's Backend-Core serves the
# stock-import routes (RT-1288): the contract's rollout order is Backend-Core first.
ENABLE_SWITCH = "rt_connector_stock_import_enabled"

# The governed break-glass correction reference (Q3, 897, 913) on Stock Entry / Stock
# Reconciliation. Not created by RT-1287 (see the module docstring).
CORRECTION_REF_FIELD = "rt_correction_ref"
_PROVENANCE_FIELDS = ("rt_source_system", "rt_external_id")
_FACT_FIELDS = ("purpose", "is_return", "return_against")
_IN_CHUNK = 500

_SLE_FIELDS = (
	"name",
	"creation",
	"voucher_type",
	"voucher_no",
	"voucher_detail_no",
	"posting_date",
	"posting_time",
	"is_cancelled",
	"actual_qty",
	"stock_uom",
	"item_code",
	"batch_no",
	"serial_and_batch_bundle",
)


def enabled() -> bool:
	"""True when the site config turns the stock import on (default off). Never raises."""
	conf = getattr(frappe, "conf", None) or {}
	value = conf.get(ENABLE_SWITCH)
	if isinstance(value, bool):
		return value
	try:
		return int(float(value or 0)) == 1
	except TypeError, ValueError, OverflowError:
		return str(value).strip().lower() == "true"


class FrappeLedgerSource:
	"""The ERPNext Stock Ledger reads of one report (BENCH-VALIDATION)."""

	def warehouse_exists(self, warehouse: str) -> bool:
		return bool(frappe.db.exists("Warehouse", warehouse))

	def site_now(self) -> datetime:
		return frappe.utils.now_datetime()

	def site_timezone(self) -> tzinfo:
		return ZoneInfo(frappe.utils.get_system_timezone())

	def read_ledger(
		self, warehouse: str, *, start: LedgerStart | None, cutoff: datetime, limit: int
	) -> list[RawLedgerEntry]:
		sle = frappe.qb.DocType("Stock Ledger Entry")
		query = (
			frappe.qb.from_(sle)
			.select(*(sle[f] for f in _SLE_FIELDS))
			.where(sle.warehouse == warehouse)
			.where(sle.docstatus == 1)
			.where(sle.creation <= cutoff)
		)
		if start is not None:
			if start.name is None:
				query = query.where(sle.creation >= start.creation)
			else:
				same = sle.name >= start.name if start.inclusive else sle.name > start.name
				query = query.where(
					(sle.creation > start.creation) | ((sle.creation == start.creation) & same)
				)
		rows = query.orderby(sle.creation).orderby(sle.name).limit(limit).run(as_dict=True)
		return [_raw_entry(row) for row in rows]

	def bundle_entries(self, bundles: list[str]) -> dict[str, list[RawBundleEntry]]:
		out: dict[str, list[RawBundleEntry]] = {}
		for chunk in _chunks(bundles):
			rows = frappe.get_all(
				"Serial and Batch Entry",
				filters={"parent": ["in", chunk], "parenttype": "Serial and Batch Bundle"},
				fields=["parent", "idx", "batch_no", "qty"],
				order_by="parent asc, idx asc",
			)
			for row in rows:
				out.setdefault(row["parent"], []).append(
					RawBundleEntry(batch_no=row.get("batch_no") or None, qty=float(row.get("qty") or 0))
				)
		return out

	def voucher_facts(self, vouchers: list[tuple[str, str]]) -> dict[tuple[str, str], VoucherFacts]:
		by_type: dict[str, list[str]] = {}
		for voucher_type, voucher_no in vouchers:
			by_type.setdefault(voucher_type, []).append(voucher_no)
		out: dict[tuple[str, str], VoucherFacts] = {}
		for voucher_type, names in by_type.items():
			columns = [
				f
				for f in (*_FACT_FIELDS, *_PROVENANCE_FIELDS, CORRECTION_REF_FIELD)
				if frappe.db.has_column(voucher_type, f)
			]
			for chunk in _chunks(names):
				for row in frappe.get_all(
					voucher_type, filters={"name": ["in", chunk]}, fields=["name", *columns]
				):
					out[(voucher_type, row["name"])] = _facts(row)
		return out


def _facts(row) -> VoucherFacts:
	return VoucherFacts(
		purpose=_text(row.get("purpose")),
		is_return=bool(row.get("is_return")),
		return_against=_text(row.get("return_against")),
		has_retail_provenance=any(_text(row.get(f)) for f in _PROVENANCE_FIELDS),
		correction_ref=_text(row.get(CORRECTION_REF_FIELD)),
	)


def _text(value) -> str | None:
	text = "" if value is None else str(value).strip()
	return text or None


def _raw_entry(row) -> RawLedgerEntry:
	return RawLedgerEntry(
		name=row["name"],
		creation=row["creation"],
		voucher_type=row["voucher_type"],
		voucher_no=row["voucher_no"],
		voucher_detail_no=_text(row.get("voucher_detail_no")),
		posting_date=row["posting_date"],
		posting_time=row["posting_time"],
		is_cancelled=bool(row.get("is_cancelled")),
		actual_qty=float(row.get("actual_qty") or 0),
		stock_uom=row["stock_uom"],
		item_code=row["item_code"],
		batch_no=_text(row.get("batch_no")),
		serial_and_batch_bundle=_text(row.get("serial_and_batch_bundle")),
	)


def _chunks(values: list[str]):
	for i in range(0, len(values), _IN_CHUNK):
		yield values[i : i + _IN_CHUNK]
