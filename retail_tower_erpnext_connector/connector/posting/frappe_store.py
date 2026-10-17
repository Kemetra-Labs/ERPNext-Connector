# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Frappe-backed IdempotencyStore adapter (Task B) — ⏳ BENCH-VALIDATION.

The concrete :class:`IdempotencyStore` (the Protocol in ``idempotency.py``) backed by the
``Posting Log`` DocType. The composite UNIQUE index on ``(source_system, external_id)`` (migration
``posting_log_unique_idem``) makes posting exactly-once at the database level (Gate G5): a
``record_posted`` that races or replays against an existing pair raises, and is mapped to the
in-process :class:`IdempotencyConflict` (echo the recorded ref — no duplicate document).

Imports ``frappe`` → un-runnable on the local host; validated on the bench (standing-rules §6).
"""

from __future__ import annotations

import frappe

from .contracts import ErpnextDocumentRef
from .idempotency import IdempotencyConflict, Key

_DOCTYPE = "Posting Log"

# The exceptions a duplicate (source_system, external_id) insert can raise. On bench frappe v15 the
# composite-key/autoname collision surfaces as DuplicateEntryError; UniqueValidationError is included
# defensively. Resolved via getattr so a frappe without one of the names degrades, not errors.
_DUPLICATE_EXCEPTIONS = tuple(
	exc
	for exc in (
		getattr(frappe.exceptions, "DuplicateEntryError", None),
		getattr(frappe.exceptions, "UniqueValidationError", None),
	)
	if exc is not None
) or (Exception,)


class FrappePostingLogStore:
	"""``IdempotencyStore`` backed by the Posting Log DocType."""

	def get_document_ref(self, key: Key) -> ErpnextDocumentRef | None:
		source_system, external_id = key
		row = frappe.db.get_value(
			_DOCTYPE,
			{"source_system": source_system, "external_id": external_id},
			["document_doctype", "document_name"],
			as_dict=True,
		)
		if not row or not row.get("document_name"):
			return None
		return ErpnextDocumentRef(doctype=row["document_doctype"], name=row["document_name"])

	def record_posted(self, key: Key, document_ref: ErpnextDocumentRef) -> None:
		source_system, external_id = key
		existing = self.get_document_ref(key)
		if existing is not None:
			if existing != document_ref:
				raise IdempotencyConflict(key, existing, document_ref)
			return  # same ref recorded again — idempotent no-op
		try:
			frappe.get_doc(
				{
					"doctype": _DOCTYPE,
					"source_system": source_system,
					"external_id": external_id,
					"document_doctype": document_ref.doctype,
					"document_name": document_ref.name,
					"outcome": "posted",
				}
			).insert(ignore_permissions=True)
		except _DUPLICATE_EXCEPTIONS:
			# A concurrent worker won the race and inserted first. The collision surfaces as
			# DuplicateEntryError (the autoname/primary-key or the composite UNIQUE index) — verified
			# on bench frappe v15; UniqueValidationError is included defensively. Re-read and treat
			# the recorded ref as authoritative (Gate G5: exactly-one document per key).
			winner = self.get_document_ref(key)
			if winner is not None and winner != document_ref:
				raise IdempotencyConflict(key, winner, document_ref) from None
