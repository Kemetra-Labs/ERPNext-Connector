# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Frappe glue for the bin-view worker — BENCH-VALIDATION.

The frappe-touching seam: reads the live ERPNext ``Bin`` rows for a warehouse and
supplies the connector ``readAt`` clock. Imports ``frappe`` (and reads the ``Bin``
doctype), so it is validated on a bench, not locally (standing-rules §6). The pure
quantize/report logic it feeds lives in :mod:`.worker` (unit-tested locally).

Validated ``Bin`` shape (real staging doctype): ``item_code`` (Link Item),
``warehouse`` (Link Warehouse), ``actual_qty`` (Float), ``stock_uom`` (Link UOM).
Reads QUANTITY only — NO valuation/cost field (019 / Principle IX).

RT-176 paged read (stock-view 1.2) — CONSISTENCY ASSUMPTION (not bench-verified): the
keyset pages of one :meth:`FrappeBinReader.read_bins_paged` call are issued back-to-back
inside the scheduler job's single DB transaction (Frappe opens one per job; nothing in the
bin-view tick commits — the cursor/retry state lives in the Redis cache). On MariaDB/InnoDB
under REPEATABLE READ (the InnoDB default) every page therefore reads the same consistent
snapshot. If the session runs READ COMMITTED (or on Postgres), each page sees its own
snapshot: keyset paging over the unique per-warehouse ``item_code`` still never repeats or
skips an item that exists throughout the read, but a quantity moved between two pages may
reflect a slightly later instant. Bench evidence for this is a recorded follow-up.
"""

from __future__ import annotations

import frappe

from .worker import RawBin

_BIN_FIELDS = ["item_code", "actual_qty", "stock_uom"]


class UtcClock:
	"""Connector ``readAt`` clock — ISO-8601 UTC (millisecond precision, trailing 'Z')."""

	def now_iso(self) -> str:
		# frappe.utils.now_datetime is naive-local; use a UTC instant + the contract's
		# date-time shape. get_datetime() round-trips through frappe's tz handling.
		from frappe.utils import now_datetime

		dt = now_datetime()
		return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class FrappeBinReader:
	"""Reads live ERPNext ``Bin`` rows for a warehouse (the 019 on-hand source).

	Reads on-hand QUANTITY only. v1 (:meth:`read_bins`): the request's window carries
	null bounds (a single ≤500-item window per warehouse), so the reader returns up to
	``item_window.max_items`` + 1 rows ordered by ``item_code``. v1.2 connector-paged
	requests use :meth:`read_bins_paged` — the connector owns the window boundaries, so
	no DP2-supplied bound is ever translated into a filter.
	"""

	def read_bins(self, *, erpnext_warehouse_ref: str, item_window) -> list[RawBin]:
		# Read max_items + 1 so the worker can DETECT a warehouse that exceeds the
		# single v1 window (it raises WindowOverflowError rather than truncate).
		limit = int(getattr(item_window, "max_items", 500)) + 1
		rows = frappe.get_all(
			"Bin",
			filters={"warehouse": erpnext_warehouse_ref},
			fields=_BIN_FIELDS,
			order_by="item_code asc",
			limit=limit,
		)
		return [_to_raw(r) for r in rows]

	def read_bins_paged(self, *, erpnext_warehouse_ref: str, page_size: int, max_rows: int) -> list[RawBin]:
		"""ONE read attempt as keyset pages (stock-view 1.2 connector-paged request, RT-176).

		Each page is ``item_code > <last item_code of the previous page>`` ordered by
		``item_code`` and limited to ``page_size`` rows; the read stops at a short page or at
		``max_rows`` rows (the worker passes ``maxWindows x maxItems + 1`` so an over-limit
		warehouse is detectable). Keyset — not OFFSET — so pages cannot overlap and the cost
		per page does not grow with depth. A Bin is unique per (item_code, warehouse), so
		``item_code`` is a total order within the warehouse. See the module docstring for the
		single-transaction consistency assumption.
		"""
		page_size = max(int(page_size), 1)
		max_rows = max(int(max_rows), 0)
		rows: list[dict] = []
		last_item_code: str | None = None
		while len(rows) < max_rows:
			want = min(page_size, max_rows - len(rows))
			filters: dict = {"warehouse": erpnext_warehouse_ref}
			if last_item_code is not None:
				filters["item_code"] = [">", last_item_code]
			page = frappe.get_all(
				"Bin",
				filters=filters,
				fields=_BIN_FIELDS,
				order_by="item_code asc",
				limit=want,
			)
			rows.extend(page)
			if len(page) < want:
				break  # short page — the warehouse is exhausted
			last_item_code = str(page[-1]["item_code"])
		return [_to_raw(r) for r in rows]


def _to_raw(row) -> RawBin:
	return RawBin(
		item_code=str(row["item_code"]),
		actual_qty=float(row["actual_qty"] or 0.0),
		stock_uom=str(row["stock_uom"] or ""),
	)
