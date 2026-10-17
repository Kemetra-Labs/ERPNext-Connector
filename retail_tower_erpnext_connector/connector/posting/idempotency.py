# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Idempotency replay logic for posting (T040/T042).

A FORWARD ``sale_post`` is keyed on the 012 O-3 wire anchor ``(sourceSystem, externalId)``
(FR-005): the same logical sale maps to the same ERPNext document, and a re-offer echoes the
existing ``documentRef`` unchanged (Principle IV / Gate G5). The connector introduces NO new
idempotency primitive beyond the 012 contract.

REVERSAL re-key (Connector #28): DP2 emits the ORIGINAL sale's ``externalId`` as the top-level
anchor on a ``kind=reversal`` work-item (per-reversal distinctness lives only in
``source_ref_id``, which is NOT on the wire). Keying a reversal on ``externalId`` would collide
with the original sale's replay slot — the guard would echo the original sale's invoice and ack
``posted`` with no credit note (silent mis-success). The connector-side per-reversal-distinct
value that IS on the wire is ``work_item_ref`` (the 012 ``PostingWorkItem.workItemRef`` status-row
id, required, already the ack identity). So a reversal is keyed on ``(sourceSystem, workItemRef)``.

ONE DISCRIMINATOR, THREE CONSUMERS: :func:`provenance_id` is the single source of truth for the
per-posting-distinct provenance value. It is the second element of the replay key (here), the
``rt_external_id`` the builder writes (``reversal_builder``), and the value
``frappe_glue._find_posted_invoice`` looks up by. All three MUST read it so the crash-recovery
path cannot drift back into the #28 bug.

The persistence is abstracted behind :class:`IdempotencyStore` (a Protocol — the repo's
Repository pattern). The concrete Frappe-DocType-backed store is the deferred ``[GATED]``
T020 adapter (its DocType JSON is a forbidden surface, standing-rules §3) and a
⏳ BENCH-VALIDATION concern. This module imports NO frappe.
"""

from __future__ import annotations

from typing import Protocol

from .contracts import ErpnextDocumentRef, PostingWorkItem

Key = tuple[str, str]


class IdempotencyConflict(Exception):
	"""The same replay key already maps to a DIFFERENT document (012 409-class conflict).

	Reusing an idempotency key with a different logical outcome is rejected — the connector
	never silently overwrites a recorded posting (Principle IV).
	"""

	def __init__(self, key: Key, existing: ErpnextDocumentRef, attempted: ErpnextDocumentRef) -> None:
		super().__init__(
			f"idempotency conflict for {key}: already posted as {existing}, "
			f"attempted {attempted} (409 idempotency_key_conflict)"
		)
		self.key = key
		self.existing = existing
		self.attempted = attempted


class IdempotencyStore(Protocol):
	"""Persistence for ``(sourceSystem, externalId) → documentRef``.

	``record_posted`` MUST raise :class:`IdempotencyConflict` if the key already maps to a
	different ``documentRef`` (re-recording the same ref is a no-op replay)."""

	def get_document_ref(self, key: Key) -> ErpnextDocumentRef | None: ...

	def record_posted(self, key: Key, document_ref: ErpnextDocumentRef) -> None: ...


def provenance_id(work_item: PostingWorkItem) -> str:
	"""The per-posting-distinct provenance value for a work-item (the #28 discriminator).

	- ``sale_post`` → ``external_id`` (the 012 O-3 anchor; forward 1:1, UNCHANGED).
	- ``reversal`` → ``work_item_ref`` (the per-reversal-distinct on-wire value; ``external_id``
	  on a reversal is the ORIGINAL sale's id and is NOT distinct per reversal).

	This single value is the second element of the replay key (:func:`key_for`), the
	``rt_external_id`` written by the reversal builder, and the lookup value
	``frappe_glue._find_posted_invoice`` queries by — they MUST all read it (see module docstring).
	"""
	if work_item.kind == "reversal":
		return work_item.work_item_ref
	if work_item.kind == "sale_post":
		return work_item.external_id
	raise ValueError(f"provenance_id: unexpected work-item kind {work_item.kind!r}")


def key_for(work_item: PostingWorkItem) -> Key:
	"""The replay key for a work-item.

	``sale_post`` → ``(sourceSystem, externalId)`` (the 012 O-3 anchor — UNCHANGED). ``reversal``
	→ ``(sourceSystem, workItemRef)`` (Connector #28 re-key — see module docstring): a reversal's
	top-level ``externalId`` is the ORIGINAL sale's id, so keying on it would collide with the
	original sale's replay slot and silently echo the original invoice.
	"""
	return (work_item.source_system, provenance_id(work_item))


def replay_guard(store: IdempotencyStore, key: Key) -> ErpnextDocumentRef | None:
	"""Return the recorded ``documentRef`` if this key was already posted, else ``None``.

	A non-``None`` result means the caller MUST short-circuit — echo the existing
	``documentRef`` as an idempotent duplicate ``posted`` and NOT re-build/re-submit.
	"""
	return store.get_document_ref(key)
