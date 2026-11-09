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

RT-918 tracking read (stock-view 1.4) — :class:`FrappeTrackingReader`, BENCH-VALIDATION:

* Item flags: ``Item.has_batch_no`` / ``has_serial_no`` / ``has_expiry_date``.
* Batch-wise BOOK quantity per warehouse, from the Stock Ledger (the same source ERPNext's own
  batch queries sum), for the batch Items only, ``is_cancelled = 0``:
    - bundle-backed rows (v15+): ``Stock Ledger Entry.serial_and_batch_bundle`` →
      ``Serial and Batch Entry`` (``batch_no``, signed ``qty``), summed per (Item, Batch);
    - legacy rows (pre-bundle ledgers): ``Stock Ledger Entry.batch_no`` with NO bundle,
      ``actual_qty`` summed per (Item, Batch). A row is counted by exactly one of the two, so
      nothing is double-counted.
  A Batch with a non-zero book quantity in the warehouse is listed, negative included (the
  contract allows it). A Batch whose summed quantity is zero at the contract precision is omitted
  before its master is read (captain decision on RT-918: a used-up Batch has no presence, and every
  lifetime Batch would exceed the 200 cap). Expired and disabled Batches are listed (evidence, never
  filtered) — which is why ERPNext's ``get_batch_qty`` (it drops expired/disabled Batches for
  selling) is not used.
* Batch master: ``Batch.expiry_date`` and ``Batch.disabled``.
Quantity only — no valuation field is read. Off unless the site config key
:data:`TRACKING_SWITCH` is truthy (:func:`tracking_enabled`): the Connector must not send
``tracking`` to an environment before Backend-Core's route body limit (RT-1284) is deployed there.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import frappe

from .worker import RawBatch, RawBin, RawItemTracking, RawTrackingRead, TrackingReadError, is_zero_qty

_BIN_FIELDS = ["item_code", "actual_qty", "stock_uom"]
_ITEM_TRACKING_FIELDS = ["name", "has_batch_no", "has_serial_no", "has_expiry_date"]
_BATCH_MASTER_FIELDS = ["name", "item", "expiry_date", "disabled"]
# Batch names per master query: one read can see up to 500 Items x 200+ Batches, so the ``in``
# filter is chunked to stay bounded.
_MASTER_CHUNK = 1000

# RT-918 release switch (site config, ``bench --site <site> set-config``). Absent or falsy → the
# Connector reports the pre-1.4 shape (no ``tracking``). Turn it on for a site only once that
# environment runs a Backend-Core with RT-1284's route body limit.
TRACKING_SWITCH = "rt_connector_bin_view_send_tracking"


def tracking_enabled() -> bool:
	"""True when the site config turns the RT-918 ``tracking`` evidence on (default off).

	On only for ``1`` / ``"1"`` (or a float string truncating to 1, as Frappe's ``cint`` does),
	``True`` or ``"true"``; anything else, including absent, is off. Never raises.
	"""
	conf = getattr(frappe, "conf", None) or {}
	value = conf.get(TRACKING_SWITCH)
	if isinstance(value, bool):
		return value
	try:
		return int(float(value or 0)) == 1
	except TypeError, ValueError, OverflowError:
		return str(value).strip().lower() == "true"


class UtcClock:
	"""Connector ``readAt`` clock — ISO-8601 UTC (millisecond precision, trailing 'Z')."""

	def now_iso(self) -> str:
		# RT-1237: an aware UTC instant, never frappe.utils.now_datetime() — that is the
		# site's naive local wall time, so on a non-UTC site it would be mislabelled 'Z'.
		return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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


class FrappeTrackingReader:
	"""Reads the ERPNext tracking master + batch-wise book quantity (RT-918). ⏳ BENCH-VALIDATION.

	See the module docstring for the sources. One call covers at most one report window of Items
	(the worker chunks), so the Item and ledger ``in`` filters hold at most 500 Items; the Batch
	master read is chunked by :data:`_MASTER_CHUNK` names (it runs before the worker's 200-Batch
	refusal, so it can see more).
	"""

	def read_tracking(self, *, erpnext_warehouse_ref: str, item_codes: list[str]) -> RawTrackingRead:
		codes = sorted(set(item_codes))
		if not codes:
			return RawTrackingRead(items=(), batches=())
		items = tuple(
			RawItemTracking(
				item_code=str(r["name"]),
				has_batch_no=_flag(r.get("has_batch_no")),
				has_serial_no=_flag(r.get("has_serial_no")),
				has_expiry_date=_flag(r.get("has_expiry_date")),
			)
			for r in frappe.get_all("Item", filters={"name": ["in", codes]}, fields=_ITEM_TRACKING_FIELDS)
		)
		batch_items = sorted(i.item_code for i in items if i.has_batch_no)
		if not batch_items:
			return RawTrackingRead(items=items, batches=())
		qty: dict[tuple[str, str], float] = {}
		for row in [
			*_bundle_batch_qty(erpnext_warehouse_ref, batch_items),
			*_legacy_batch_qty(erpnext_warehouse_ref, batch_items),
		]:
			key = (str(row["item_code"]), str(row["batch_no"]))
			qty[key] = qty.get(key, 0.0) + float(row["qty"] or 0.0)
		qty = {key: total for key, total in qty.items() if not is_zero_qty(total)}
		masters = _batch_masters(sorted({batch for _item, batch in qty}))
		batches = tuple(
			_raw_batch(item, batch, total, masters.get(batch)) for (item, batch), total in sorted(qty.items())
		)
		return RawTrackingRead(items=items, batches=batches)


def _flag(value) -> bool:
	return bool(int(value or 0))


def _bundle_batch_qty(warehouse: str, item_codes: list[str]) -> list[dict]:
	"""Σ ``Serial and Batch Entry.qty`` per (Item, Batch) of the warehouse's live ledger rows. ⏳ BENCH."""
	from frappe.query_builder.functions import Sum

	sle = frappe.qb.DocType("Stock Ledger Entry")
	sbe = frappe.qb.DocType("Serial and Batch Entry")
	return (
		frappe.qb.from_(sle)
		.inner_join(sbe)
		.on(sbe.parent == sle.serial_and_batch_bundle)
		.select(sle.item_code, sbe.batch_no, Sum(sbe.qty).as_("qty"))
		.where(
			(sle.warehouse == warehouse)
			& (sle.is_cancelled == 0)
			& sle.item_code.isin(item_codes)
			& sbe.batch_no.isnotnull()
			& (sbe.batch_no != "")
		)
		.groupby(sle.item_code, sbe.batch_no)
		.run(as_dict=True)
	)


def _legacy_batch_qty(warehouse: str, item_codes: list[str]) -> list[dict]:
	"""Σ ``Stock Ledger Entry.actual_qty`` per (Item, Batch) of rows with a ``batch_no`` and NO bundle. ⏳ BENCH."""
	from frappe.query_builder.functions import IfNull, Sum

	sle = frappe.qb.DocType("Stock Ledger Entry")
	return (
		frappe.qb.from_(sle)
		.select(sle.item_code, sle.batch_no, Sum(sle.actual_qty).as_("qty"))
		.where(
			(sle.warehouse == warehouse)
			& (sle.is_cancelled == 0)
			& sle.item_code.isin(item_codes)
			& (IfNull(sle.serial_and_batch_bundle, "") == "")
			& (IfNull(sle.batch_no, "") != "")
		)
		.groupby(sle.item_code, sle.batch_no)
		.run(as_dict=True)
	)


def _batch_masters(names: list[str]) -> dict[str, dict]:
	"""The Batch master rows of ``names``, read in chunks of :data:`_MASTER_CHUNK` names."""
	masters: dict[str, dict] = {}
	for i in range(0, len(names), _MASTER_CHUNK):
		chunk = names[i : i + _MASTER_CHUNK]
		rows = frappe.get_all("Batch", filters={"name": ["in", chunk]}, fields=_BATCH_MASTER_FIELDS)
		masters.update({str(r["name"]): r for r in rows})
	return masters


def _raw_batch(item_code: str, batch_no: str, qty: float, master: dict | None) -> RawBatch:
	if master is None:
		raise TrackingReadError(
			f"Batch {batch_no!r} of Item {item_code!r} has ledger rows but no Batch master"
		)
	if master.get("item") and str(master["item"]) != item_code:
		raise TrackingReadError(
			f"Batch {batch_no!r} has ledger rows for Item {item_code!r} but its master names another Item"
		)
	return RawBatch(
		item_code=item_code,
		batch_no=batch_no,
		qty=qty,
		expiry_date=_iso_date(master.get("expiry_date")),
		disabled=_flag(master.get("disabled")),
	)


def _iso_date(value) -> str | None:
	"""ERPNext ``Date`` → ``YYYY-MM-DD`` (``None`` when empty)."""
	if not value:
		return None
	if isinstance(value, datetime):
		return value.date().isoformat()
	if isinstance(value, date):
		return value.isoformat()
	# A string from the DB: re-render it as a strict calendar date (malformed -> ValueError, retried).
	return date.fromisoformat(str(value)[:10]).isoformat()
