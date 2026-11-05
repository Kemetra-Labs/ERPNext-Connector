# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Add the composite UNIQUE index on Posting Log (source_system, external_id).

This is the database-level Gate G5 guarantee: exactly one Posting Log row — and therefore one
ERPNext document — per ``(source_system, external_id)`` (the 012 O-3 idempotency anchor). A
replayed or concurrent insert for the same pair fails at the DB, so the connector resolves to the
recorded ``document_name`` instead of creating a duplicate.

Idempotent + reversible:
  - rollback note: DROP INDEX unique_rt_posting_idem ON the "tabPosting Log" table (additive
    index; safe to drop — no data loss, reverts to non-unique behaviour).
"""

import frappe

_INDEX = "unique_rt_posting_idem"
_TABLE = "tabPosting Log"


def execute() -> None:
	# No-op if the DocType table isn't there yet (defensive; post_model_sync should have created it).
	if not frappe.db.table_exists("Posting Log"):
		return
	existing = frappe.db.sql(f"SHOW INDEX FROM `{_TABLE}` WHERE Key_name = %s", (_INDEX,))
	if existing:
		return  # already applied — idempotent re-run
	frappe.db.sql(f"ALTER TABLE `{_TABLE}` ADD UNIQUE INDEX `{_INDEX}` (`source_system`, `external_id`)")
