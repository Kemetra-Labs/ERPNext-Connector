# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""T040/T042 — local unit tests for idempotency replay logic.

The store is abstracted behind :class:`IdempotencyStore` (Protocol); tests use an in-memory
fake. The concrete Frappe-DocType-backed store is the deferred [GATED] T020 adapter and a
⏳ BENCH-VALIDATION concern. Replay key: a sale_post keys on (sourceSystem, externalId) — the
012 O-3 anchor; a reversal keys on (sourceSystem, workItemRef) — the Connector #28 re-key, since
DP2 emits the ORIGINAL sale's externalId on a reversal work-item. No frappe here.
"""

import dataclasses

import pytest

from retail_tower_erpnext_connector.connector.posting import contracts as c
from retail_tower_erpnext_connector.connector.posting import idempotency as idem


class InMemoryStore:
	"""A fake implementing the IdempotencyStore Protocol."""

	def __init__(self):
		self._posted: dict[tuple[str, str], c.ErpnextDocumentRef] = {}

	def get_document_ref(self, key: tuple[str, str]) -> c.ErpnextDocumentRef | None:
		return self._posted.get(key)

	def record_posted(self, key: tuple[str, str], document_ref: c.ErpnextDocumentRef) -> None:
		existing = self._posted.get(key)
		if existing is not None and existing != document_ref:
			raise idem.IdempotencyConflict(key, existing, document_ref)
		self._posted[key] = document_ref


KEY = ("pos-pulse", "POS-9001")
DOC = c.ErpnextDocumentRef("Sales Invoice", "ACC-SINV-0001")


class TestReplayGuard:
	def test_first_post_is_not_a_replay(self):
		store = InMemoryStore()
		existing = idem.replay_guard(store, KEY)
		assert existing is None

	def test_already_posted_short_circuits_to_recorded_document_ref(self):
		# T040: same (sourceSystem, externalId) → return the existing documentRef,
		# builder/submit not re-entered.
		store = InMemoryStore()
		store.record_posted(KEY, DOC)
		existing = idem.replay_guard(store, KEY)
		assert existing == DOC

	def test_record_then_replay_echoes_same_ref(self):
		store = InMemoryStore()
		store.record_posted(KEY, DOC)
		# A re-offer of the same key echoes the SAME documentRef (Principle IV / O-3).
		assert idem.replay_guard(store, KEY) == DOC


class TestAckReplay:
	def test_same_logical_outcome_replays_deterministically(self):
		# T042: recording the same (key, documentRef) twice is idempotent — no error.
		store = InMemoryStore()
		store.record_posted(KEY, DOC)
		store.record_posted(KEY, DOC)  # replay — must not raise
		assert store.get_document_ref(KEY) == DOC

	def test_different_outcome_for_same_key_conflicts(self):
		# T042: a DIFFERENT documentRef for the same key → 409-class conflict.
		store = InMemoryStore()
		store.record_posted(KEY, DOC)
		other = c.ErpnextDocumentRef("Sales Invoice", "ACC-SINV-9999")
		with pytest.raises(idem.IdempotencyConflict):
			store.record_posted(KEY, other)


class TestKeyDerivation:
	def test_sale_post_key_comes_from_external_id_anchor(self):
		# FORWARD path UNCHANGED: a sale_post keys on (sourceSystem, externalId) — the 012 O-3 anchor.
		wi = _work_item()
		assert idem.key_for(wi) == ("pos-pulse", "POS-9001")
		assert idem.provenance_id(wi) == "POS-9001"

	def test_reversal_key_comes_from_work_item_ref_not_external_id(self):
		# Connector #28 RE-KEY: a reversal keys on (sourceSystem, workItemRef), NOT externalId —
		# because DP2 emits the ORIGINAL sale's id as the reversal's top-level externalId.
		wi = _reversal_work_item()
		assert idem.provenance_id(wi) == wi.work_item_ref
		assert idem.key_for(wi) == ("pos-pulse", wi.work_item_ref)
		assert idem.key_for(wi) != ("pos-pulse", wi.external_id)

	def test_reversal_and_original_sale_post_sharing_external_id_get_distinct_keys(self):
		# THE TEST THAT WOULD HAVE CAUGHT #28. The reversal's top-level externalId is the ORIGINAL
		# sale's id, so sale_post and reversal SHARE external_id ("POS-9001"). Keying both on
		# external_id would collide — the reversal guard would echo the original SI and ack posted
		# with no credit note (silent mis-success). The re-key makes the keys DISTINCT.
		sale_post = _work_item()  # external_id "POS-9001"
		reversal = _reversal_work_item()  # top-level external_id ALSO "POS-9001"
		assert sale_post.external_id == reversal.external_id == "POS-9001"
		assert idem.key_for(sale_post) != idem.key_for(reversal)

	def test_provenance_id_fails_closed_on_unknown_kind(self):
		# FAIL-CLOSED: provenance_id is the single #28 discriminator; an unexpected kind must raise
		# rather than silently fall through to external_id. from_wire rejects bogus kinds, so we
		# force one onto the frozen work-item via dataclasses.replace.
		bogus = dataclasses.replace(_work_item(), kind="quantum_post")
		with pytest.raises(ValueError, match="unexpected work-item kind"):
			idem.provenance_id(bogus)
		# key_for delegates to provenance_id, so it raises too.
		with pytest.raises(ValueError, match="unexpected work-item kind"):
			idem.key_for(bogus)

	def test_two_reversals_of_one_original_get_distinct_keys(self):
		# N:1: two reversals of ONE original share external_id but differ by work_item_ref → two
		# DISTINCT replay keys (no collision in the store).
		rev_a = _reversal_work_item(work_item_ref="55555555-5555-4555-8555-55555555000a")
		rev_b = _reversal_work_item(work_item_ref="55555555-5555-4555-8555-55555555000b")
		assert rev_a.external_id == rev_b.external_id == "POS-9001"
		assert idem.key_for(rev_a) != idem.key_for(rev_b)

	def test_both_key_shapes_coexist_in_one_store_without_collision(self):
		# STORE both-shape handling: a sale_post key (sourceSystem, externalId) and a reversal key
		# (sourceSystem, workItemRef) for the SAME logical sale live in one store as DISTINCT
		# tuple[str, str] entries — no type confusion, no overwrite.
		store = InMemoryStore()
		sale_post = _work_item()
		reversal = _reversal_work_item()
		sale_doc = c.ErpnextDocumentRef("Sales Invoice", "ACC-SINV-0001")
		rev_doc = c.ErpnextDocumentRef("Sales Invoice", "ACC-SINV-0002")
		store.record_posted(idem.key_for(sale_post), sale_doc)
		store.record_posted(idem.key_for(reversal), rev_doc)
		assert store.get_document_ref(idem.key_for(sale_post)) == sale_doc
		assert store.get_document_ref(idem.key_for(reversal)) == rev_doc


def _reversal_work_item(
	*,
	work_item_ref: str = "55555555-5555-4555-8555-555555555555",
	external_id: str = "POS-9001",
) -> c.PostingWorkItem:
	"""A reversal work-item on the REAL wire: top-level externalId is the ORIGINAL sale's id
	("POS-9001", same as the sale_post fixture), with the distinct discriminator in workItemRef."""
	return c.PostingWorkItem.from_wire(
		{
			"workItemRef": work_item_ref,
			"kind": "reversal",
			"sourceSystem": "pos-pulse",
			"externalId": external_id,
			"payloadHash": "b" * 64,
			"businessDate": "2026-06-05",
			"itemCursor": "cursor-2",
			"reversalOf": {
				"sourceSystem": "pos-pulse",
				"externalId": "POS-9001",
				"reversalKind": "refund",
			},
			"sale": {
				"saleRef": "22222222-2222-4222-8222-222222222222",
				"storeId": "33333333-3333-4333-8333-333333333333",
				"currencyCode": "EGP",
				"posTotal": "230.00",
				"occurredAt": "2026-06-01T10:00:00Z",
				"businessDate": "2026-06-01",
				"sourceSystem": "pos-pulse",
				"externalId": "POS-9001",
				"lines": [
					{
						"lineName": "Item A",
						"unitPrice": "100.00",
						"currencyCode": "EGP",
						"quantity": "2",
						"lineAmount": "200.00",
						"unit": "each",
						"erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"},
					}
				],
			},
		}
	)


def _work_item() -> c.PostingWorkItem:
	return c.PostingWorkItem.from_wire(
		{
			"workItemRef": "11111111-1111-4111-8111-111111111111",
			"kind": "sale_post",
			"sourceSystem": "pos-pulse",
			"externalId": "POS-9001",
			"payloadHash": "a" * 64,
			"businessDate": "2026-06-01",
			"itemCursor": "cursor-1",
			"sale": {
				"saleRef": "22222222-2222-4222-8222-222222222222",
				"storeId": "33333333-3333-4333-8333-333333333333",
				"currencyCode": "EGP",
				"posTotal": "100.00",
				"occurredAt": "2026-06-01T10:00:00Z",
				"businessDate": "2026-06-01",
				"sourceSystem": "pos-pulse",
				"externalId": "POS-9001",
				"lines": [
					{
						"lineName": "Item A",
						"unitPrice": "100.00",
						"currencyCode": "EGP",
						"quantity": "1",
						"lineAmount": "100.00",
						"unit": "each",
						"erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"},
					}
				],
			},
		}
	)
