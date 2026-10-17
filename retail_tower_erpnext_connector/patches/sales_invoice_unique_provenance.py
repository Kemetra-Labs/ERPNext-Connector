# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Add a composite UNIQUE index on Sales Invoice (rt_source_system, rt_external_id).

This closes Gate G5 / F-002 (the crash-between-submit-and-record window): the Posting Log unique
index makes the connector's OWN dedup exactly-once, but a crash AFTER ``sinv.submit()`` and BEFORE
``record_posted()`` leaves no Posting Log row — so a DP2 re-offer's replay guard finds nothing and
submits a SECOND Sales Invoice. A UNIQUE index on the SI's provenance columns makes that second
submit fail at the database, which ``frappe_glue.post_work_item`` catches and resolves to the
already-posted SI (echo its documentRef) instead of creating a duplicate.

The columns are the ``rt_source_system`` / ``rt_external_id`` provenance Custom Fields (declared in
``fixtures/custom_field.json``, synced before this patch). MariaDB allows MULTIPLE NULLs in a unique
index, so manual / non-connector Sales Invoices (which carry NULL provenance) are unaffected — only
connector-posted SIs (both columns non-NULL) are deduped.

Idempotent + reversible:
  - rollback note: DROP INDEX unique_rt_si_provenance ON the "tabSales Invoice" table (additive
    index; safe to drop — no data loss, reverts to non-unique behaviour).
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

_INDEX = "unique_rt_si_provenance"
_TABLE = "tabSales Invoice"

# The two indexed provenance fields, kept IN SYNC with fixtures/custom_field.json (length 100/200).
# We create them here rather than depend on fixture-sync ORDER: Frappe runs application patches
# BEFORE fixture synchronization, and a patch that returned early on missing columns would be
# recorded as applied and NEVER re-run (patches do not auto-re-run) — silently leaving the F-002
# dedup disabled on fresh installs. So the patch OWNS its prerequisite: ensure the columns, then index.
_INDEXED_FIELDS = {
	"Sales Invoice": [
		{
			"fieldname": "rt_source_system",
			"label": "RT Source System",
			"fieldtype": "Data",
			"length": 100,
			"insert_after": "naming_series",
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


def execute() -> None:
	if not frappe.db.table_exists("Sales Invoice"):
		return  # defensive — erpnext should have created it
	# OWN the prerequisite: upsert the indexed provenance Custom Fields (idempotent — safe whether or
	# not the fixture sync already ran). Do NOT rely on fixture-sync ordering.
	create_custom_fields(_INDEXED_FIELDS, ignore_validate=True)
	cols = {c["Field"] for c in frappe.db.sql(f"SHOW COLUMNS FROM `{_TABLE}`", as_dict=True)}
	if "rt_source_system" not in cols or "rt_external_id" not in cols:
		# FAIL LOUD: a fail-closed dedup guard that can't install its columns must FAIL the migrate,
		# never record itself green having done nothing (Principle VI — silent success is the bug the
		# reviewer flagged). Raising leaves the patch UN-logged, so a fixed re-run can apply it.
		raise RuntimeError(
			"sales_invoice_unique_provenance: rt_source_system/rt_external_id columns absent after "
			"create_custom_fields — cannot add the F-002 dedup index; aborting migrate."
		)
	existing = frappe.db.sql(f"SHOW INDEX FROM `{_TABLE}` WHERE Key_name = %s", (_INDEX,))
	if existing:
		return  # index already present — idempotent (guards against the line being re-added)
	frappe.db.sql(
		f"ALTER TABLE `{_TABLE}` ADD UNIQUE INDEX `{_INDEX}` (`rt_source_system`, `rt_external_id`)"
	)
