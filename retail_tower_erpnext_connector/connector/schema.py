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


def ensure_g5_indexes() -> None:
	"""Ensure both Gate G5 indexes (install + every migrate)."""
	ensure_posting_log_index()
	ensure_sales_invoice_index()


def after_install() -> None:
	ensure_g5_indexes()


def after_migrate() -> None:
	ensure_g5_indexes()


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
