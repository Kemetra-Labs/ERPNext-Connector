# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Posting Log DocType controller (connector spec 006, Task B).

One row per posted Data-Pulse-2 sale, keyed on ``(source_system, external_id)`` — the 012 O-3
wire idempotency anchor. The composite UNIQUE index on that pair (added by the
``posting_log_unique_idem`` migration patch) is the database-level guarantee that makes posting
exactly-once (Gate G5): a concurrent or replayed insert for the same pair fails at the DB, so the
connector resolves to the already-recorded ``document_name`` instead of creating a second ERPNext
document — closing the crash-between-submit-and-record window that the in-memory store could not.
"""

import frappe
from frappe.model.document import Document


class PostingLog(Document):
	"""Idempotency + audit record for one ERPNext sales posting."""

	def validate(self) -> None:
		# Defence-in-depth above the DB unique index: reject an obviously malformed key early.
		if not self.source_system or not self.external_id:
			frappe.throw("Posting Log requires both source_system and external_id (012 O-3 key)")


def on_doctype_update() -> None:
	"""Ensure the Gate G5 unique index whenever Frappe syncs this DocType (install included).

	RT-58: a fresh install never runs the ``posting_log_unique_idem`` patch (RT-54 root cause).
	This follows ERPNext's ``Bin.on_doctype_update`` → ``frappe.db.add_unique`` pattern; the
	``after_migrate`` hook also re-ensures it on every migrate.
	"""
	from retail_tower_erpnext_connector.connector.schema import ensure_posting_log_index

	ensure_posting_log_index()
