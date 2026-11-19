# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Add a composite UNIQUE index on Stock Entry (rt_source_system, rt_external_id) (RT-1514).

The transfer legs are exactly-once only while this index exists: a second submit of an already-posted
dispatch or receipt (a crash between submit and record, or two workers racing) must fail at the database.
It spans Draft, submitted and cancelled documents, so a cancelled document keeps its key and a leg is never
silently re-posted under it.

Data preflight (fail closed): before the index is created, every non-empty provenance pair is checked for
duplicates. Duplicates mean documents carrying the same key already exist; the patch raises with the
offending keys and creates nothing, so an operator resolves them (the connector never edits or cancels an ERP
document to hide a mismatch). The columns are NULL on every Stock Entry made by hand, and MariaDB allows many
NULLs in a unique index, so those are unaffected.

Rollback note: DROP INDEX unique_rt_se_provenance ON the "tabStock Entry" table (additive).
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from retail_tower_erpnext_connector.connector.posting.index_guard import duplicate_provenance_message

_INDEX = "unique_rt_se_provenance"
_TABLE = "tabStock Entry"

# Kept IN SYNC with fixtures/custom_field.json. The patch owns its prerequisite columns, because Frappe runs
# patches before fixture sync and a patch that returned early would be recorded as applied.
_INDEXED_FIELDS = {
	"Stock Entry": [
		{
			"fieldname": "rt_source_system",
			"label": "RT Source System",
			"fieldtype": "Data",
			"length": 100,
			"insert_after": "posting_time",
			"read_only": 1,
			"no_copy": 1,
			"print_hide": 1,
			"translatable": 0,
		},
		{
			"fieldname": "rt_external_id",
			"label": "RT External Id",
			"fieldtype": "Data",
			"length": 200,
			"insert_after": "rt_source_system",
			"read_only": 1,
			"no_copy": 1,
			"print_hide": 1,
			"translatable": 0,
		},
	]
}


def duplicate_rows() -> list[dict]:
	"""Provenance pairs held by more than one Stock Entry (any docstatus)."""
	return frappe.db.sql(
		f"""SELECT rt_source_system, rt_external_id, COUNT(*) AS n FROM `{_TABLE}`
		WHERE rt_source_system IS NOT NULL AND rt_source_system != ''
		AND rt_external_id IS NOT NULL AND rt_external_id != ''
		GROUP BY rt_source_system, rt_external_id HAVING COUNT(*) > 1""",
		as_dict=True,
	)


def execute() -> None:
	if not frappe.db.table_exists("Stock Entry"):
		return  # defensive: erpnext should have created it
	create_custom_fields(_INDEXED_FIELDS, ignore_validate=True)
	cols = {c["Field"] for c in frappe.db.sql(f"SHOW COLUMNS FROM `{_TABLE}`", as_dict=True)}
	if "rt_source_system" not in cols or "rt_external_id" not in cols:
		raise RuntimeError(
			"stock_entry_unique_provenance: provenance columns absent after create_custom_fields; "
			"cannot add the transfer dedup index; aborting migrate."
		)
	duplicates = duplicate_rows()
	if duplicates:
		raise RuntimeError(duplicate_provenance_message(duplicates))
	if frappe.db.sql(f"SHOW INDEX FROM `{_TABLE}` WHERE Key_name = %s", (_INDEX,)):
		return  # already present: idempotent
	frappe.db.sql(
		f"ALTER TABLE `{_TABLE}` ADD UNIQUE INDEX `{_INDEX}` (`rt_source_system`, `rt_external_id`)"
	)
