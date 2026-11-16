# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Gate G5 schema invariants — ensured on install AND every migrate (RT-58). ⏳ BENCH-VALIDATION.

Root cause (RT-54): Frappe's ``install_app`` marks every ``patches.txt`` entry completed WITHOUT
running it (``frappe.installer.set_all_patches_as_completed``). The two exactly-once indexes were
created only inside patches, so every FRESH install silently lacked them. The patches stay for the
upgrade path; this module makes the invariant hold everywhere:

  - ``after_install`` / ``after_migrate`` hooks (``hooks.py``) call :func:`ensure_g5_indexes`;
  - the Posting Log controller's ``on_doctype_update`` also ensures its own index on DocType sync
    (ERPNext's pattern: ``erpnext/stock/doctype/bin/bin.py`` ``on_doctype_update`` → ``add_unique``).

Both ensure steps are idempotent. If an index cannot be created — e.g. duplicate provenance rows
already exist — they FAIL LOUD (the migrate errors) instead of recording silent success; the
poller's runtime guard (``posting.index_guard``) keeps posting paused until it is fixed.
"""

from __future__ import annotations

import frappe

from .posting.index_guard import index_matches

POSTING_LOG_INDEX = "unique_rt_posting_idem"
SALES_INVOICE_INDEX = "unique_rt_si_provenance"
POSTING_LOG_COLUMNS = ("source_system", "external_id")
SALES_INVOICE_COLUMNS = ("rt_source_system", "rt_external_id")
STOCK_RECONCILIATION_INDEX = "unique_rt_sr_provenance"  # RT-1422, the inventory_count leg's own index
STOCK_RECONCILIATION_COLUMNS = ("rt_source_system", "rt_external_id")


class G5IndexError(RuntimeError):
	"""A Gate G5 unique index could not be created (fail-closed; posting stays paused)."""


def _index_rows(doctype: str, index_name: str) -> list:
	return frappe.db.sql(f"SHOW INDEX FROM `tab{doctype}` WHERE Key_name = %s", (index_name,), as_dict=True)


def index_present(doctype: str, index_name: str, columns: tuple[str, ...]) -> bool:
	"""True only if ``index_name`` is the exact UNIQUE index on ``columns`` (the guard's probe).

	A same-named non-unique or wrong-column index does NOT count (Codex P1, PR #43): it would
	pass a name-only check while providing no exactly-once constraint.
	"""
	return index_matches(_index_rows(doctype, index_name), columns)


def ensure_posting_log_index() -> None:
	"""Idempotently add ``unique_rt_posting_idem`` on Posting Log ``(source_system, external_id)``."""
	_ensure(
		"Posting Log",
		POSTING_LOG_INDEX,
		POSTING_LOG_COLUMNS,
		lambda: frappe.db.add_unique(
			"Posting Log", list(POSTING_LOG_COLUMNS), constraint_name=POSTING_LOG_INDEX
		),
	)


def ensure_sales_invoice_index() -> None:
	"""Idempotently ensure the provenance Custom Fields and ``unique_rt_si_provenance`` on Sales Invoice.

	The Custom Field definitions come from the upgrade patch (one source of truth). The fields are
	created and COMMITTED before the index DDL: inside ``after_migrate`` Frappe refuses an
	``ALTER TABLE`` while the transaction holds uncommitted writes (``ImplicitCommitError``, seen on
	the RT-58 bench), so the index goes through ``frappe.db.add_unique``, which commits first, the
	same path as Posting Log.
	"""
	_ensure("Sales Invoice", SALES_INVOICE_INDEX, SALES_INVOICE_COLUMNS, _create_sales_invoice_index)


def _create_sales_invoice_index() -> None:
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	from retail_tower_erpnext_connector.patches import sales_invoice_unique_provenance as patch

	create_custom_fields(patch._INDEXED_FIELDS, ignore_validate=True)
	frappe.db.commit()
	frappe.db.add_unique("Sales Invoice", list(SALES_INVOICE_COLUMNS), constraint_name=SALES_INVOICE_INDEX)


def ensure_stock_reconciliation_index() -> None:
	"""Idempotently ensure the count provenance Custom Fields and ``unique_rt_sr_provenance`` (RT-1422).

	Same path as the Sales Invoice index, plus the data preflight: duplicate provenance keys make the
	patch's helper raise, so the migrate fails loud and the index is never created over them.
	"""
	_ensure(
		"Stock Reconciliation",
		STOCK_RECONCILIATION_INDEX,
		STOCK_RECONCILIATION_COLUMNS,
		_create_stock_reconciliation_index,
	)


def _create_stock_reconciliation_index() -> None:
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	from retail_tower_erpnext_connector.connector.posting.index_guard import duplicate_provenance_message
	from retail_tower_erpnext_connector.patches import stock_reconciliation_unique_provenance as patch

	create_custom_fields(patch._INDEXED_FIELDS, ignore_validate=True)
	frappe.db.commit()
	duplicates = patch.duplicate_rows()
	if duplicates:
		raise G5IndexError(duplicate_provenance_message(duplicates))
	frappe.db.add_unique(
		"Stock Reconciliation", list(STOCK_RECONCILIATION_COLUMNS), constraint_name=STOCK_RECONCILIATION_INDEX
	)


def ensure_g5_indexes() -> None:
	"""Ensure both Gate G5 indexes (install + every migrate)."""
	ensure_posting_log_index()
	ensure_sales_invoice_index()


def after_install() -> None:
	ensure_g5_indexes()
	ensure_stock_reconciliation_index()
	ensure_give_back_item()


def after_migrate() -> None:
	ensure_g5_indexes()
	ensure_stock_reconciliation_index()
	ensure_give_back_item()


# A fresh ERPNext site has these after its setup wizard; before it, the Item is created on a later migrate.
# A leaf Item Group: "Services" (the give-back is not a product) when the site has it, else the first leaf.
_GIVE_BACK_ITEM_GROUP = "Services"
_GIVE_BACK_UOM = "Nos"
_GIVE_BACK_SAVEPOINT = "rt_give_back_item"


def ensure_give_back_item() -> None:
	"""Create the fixed non-stock promotion give-back Item when it is missing (RT-1340, owner decision).

	⏳ BENCH-VALIDATION. Idempotent: an existing Item is never changed, even a wrong one (the
	Compatibility Profile warns about it and give-back returns stay ``failed_transient``). The Item is
	inserted with ``set_name`` so a site whose Stock Settings ``item_naming_by`` is "Naming Series" still
	names it ``RT-PROMO-GIVEBACK`` (ERPNext's ``Item.autoname`` would otherwise give it a series name
	and overwrite ``item_code``). A failed insert is rolled back to its savepoint. Never fails
	the migrate: unlike the G5 indexes, posting is safe without it, so a failure is logged at error and
	the next migrate tries again.
	"""
	from .posting.give_back_item import GIVE_BACK_ITEM_CODE, GIVE_BACK_ITEM_NAME

	try:
		if frappe.db.exists("Item", GIVE_BACK_ITEM_CODE):
			return
		item_group = _give_back_item_group()
		if not (item_group and frappe.db.exists("UOM", _GIVE_BACK_UOM)):
			_log_give_back("give_back_item.deferred", "Item Group or UOM not set up yet (setup wizard)")
			return
		frappe.db.savepoint(_GIVE_BACK_SAVEPOINT)
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": GIVE_BACK_ITEM_CODE,
				"item_name": GIVE_BACK_ITEM_NAME,
				"item_group": item_group,
				"stock_uom": _GIVE_BACK_UOM,
				"is_stock_item": 0,
				"is_sales_item": 1,
				"is_purchase_item": 0,
				"include_item_in_manufacturing": 0,
				"description": "Posted by the Retail Tower connector: a promotion discount given back on a "
				"return after an earlier clawback (OD-11). Do not rename, disable or make it a stock Item.",
			}
		).insert(ignore_permissions=True, set_name=GIVE_BACK_ITEM_CODE)
		frappe.db.commit()
	except Exception as exc:  # the migrate must not fail for it; logged at error so operators see it
		_rollback_give_back()
		_log_give_back("give_back_item.ensure_failed", f"{type(exc).__name__}: {exc}")


def _give_back_item_group() -> str | None:
	"""A leaf Item Group for the give-back Item, or None before the setup wizard created any."""
	leaf = frappe.db.get_value("Item Group", {"name": _GIVE_BACK_ITEM_GROUP, "is_group": 0}, "name")
	return leaf or frappe.db.get_value("Item Group", {"is_group": 0}, "name", order_by="name asc")


def _rollback_give_back() -> None:
	"""Undo a half-written Item so the next migrate tries again (a missing savepoint is ignored)."""
	try:
		frappe.db.rollback(save_point=_GIVE_BACK_SAVEPOINT)
	except Exception as exc:
		_log_give_back("give_back_item.rollback_failed", f"{type(exc).__name__}: {exc}")


def _log_give_back(event: str, detail: str) -> None:
	frappe.logger("retail_tower_posting").error({"event": event, "detail": detail})


def _ensure(doctype: str, index_name: str, columns: tuple[str, ...], create) -> None:
	if index_present(doctype, index_name, columns):
		return
	if _index_rows(doctype, index_name):
		# A same-named index with the WRONG definition: add_unique would see the name and skip, so
		# never "repair" silently — fail loud and leave the fix to an operator (posting stays paused).
		raise G5IndexError(
			f"Gate G5: {index_name} on {doctype} exists but is not UNIQUE on {columns}. "
			"Drop it and re-run migrate; posting stays paused until the exact index exists."
		)
	try:
		create()
	except Exception as exc:
		raise G5IndexError(
			f"Gate G5: could not create {index_name} on {doctype} ({type(exc).__name__}: {exc}). "
			"Duplicate provenance rows may exist; see docs/runbooks/staging-install.md. "
			"Posting stays paused until this index exists."
		) from exc
	if not index_present(doctype, index_name, columns):
		raise G5IndexError(f"Gate G5: {index_name} on {doctype} still absent after creation")
