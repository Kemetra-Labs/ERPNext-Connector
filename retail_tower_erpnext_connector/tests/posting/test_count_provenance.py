# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1422 — the count leg's provenance plumbing: fields, unique index, preflight, key (no frappe).

The fields and the index are what make a count chunk exactly-once and let the stock import recognise the
count's own ledger rows as Retail-origin (``hasRetailProvenance`` reads these same two fields).
"""

from __future__ import annotations

import json
from pathlib import Path

from retail_tower_erpnext_connector import hooks
from retail_tower_erpnext_connector.connector.posting import compatibility_profile as cp
from retail_tower_erpnext_connector.connector.posting import contracts as c
from retail_tower_erpnext_connector.connector.posting import index_guard as ig
from retail_tower_erpnext_connector.connector.posting.idempotency import key_for, provenance_id

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "custom_field.json"
_SESSION = "00000000-0000-7000-8000-00000000c0e1"


def _count_item() -> c.PostingWorkItem:
	return c.PostingWorkItem.from_wire(
		{
			"workItemRef": "00000000-0000-7000-8000-00000000c001",
			"kind": "inventory_count",
			"sourceSystem": "retail-tower",
			"externalId": f"count:{_SESSION}:2",
			"payloadHash": "b" * 64,
			"businessDate": "2026-11-16",
			"itemCursor": "c1",
			"count": {
				"countSessionRef": _SESSION,
				"chunk": 2,
				"chunkCount": 2,
				"storeId": "00000000-0000-7000-8000-00000000570e",
				"mappingBucketRef": "bucket:store:stock",
				"postingDatetime": "2026-11-16T09:30:00.000001Z",
				"lines": [
					{
						"lineSeq": 1,
						"erpnextItemRef": {"doctype": "Item", "name": "PANADOL-24"},
						"warehouseRef": {"doctype": "Warehouse", "name": "Stores - RT"},
						"countedQuantity": "7",
						"unit": "Nos",
					}
				],
			},
		}
	)


class TestKinds:
	def test_the_connector_accepts_the_count_kind_and_the_noop_outcome(self):
		assert "inventory_count" in c.WORK_ITEM_KINDS
		assert {"sale_post", "reversal"} <= c.WORK_ITEM_KINDS
		assert "verified_noop" in c.OUTCOMES
		assert {"posted", "failed_transient", "permanently_rejected", "reconciliation_required"} <= c.OUTCOMES

	def test_the_provenance_key_of_a_count_chunk_is_its_external_id(self):
		item = _count_item()
		assert provenance_id(item) == f"count:{_SESSION}:2"
		assert key_for(item) == ("retail-tower", f"count:{_SESSION}:2")


class TestFieldsAndIndex:
	def test_the_stock_reconciliation_provenance_fields_ship_read_only_and_are_exported(self):
		by_name = {f["name"]: f for f in json.loads(_FIXTURE.read_text(encoding="utf-8"))}
		for name, length in (
			("Stock Reconciliation-rt_source_system", 100),
			("Stock Reconciliation-rt_external_id", 200),
		):
			field = by_name[name]
			assert (field["dt"], field["fieldtype"], field["length"]) == (
				"Stock Reconciliation",
				"Data",
				length,
			)
			assert (field["read_only"], field["no_copy"]) == (1, 1)
		(fixture,) = [f for f in hooks.fixtures if f["dt"] == "Custom Field"]
		(name_filter,) = [f for f in fixture["filters"] if f[0] == "name"]
		assert set(cp.COUNT_CUSTOM_FIELDS) <= set(name_filter[2])

	def test_the_count_index_is_its_own_guard_and_never_pauses_sales(self):
		assert ig.COUNT_INDEXES == (
			("Stock Reconciliation", "unique_rt_sr_provenance", ("rt_source_system", "rt_external_id")),
		)
		assert not set(ig.COUNT_INDEXES) & set(ig.G5_INDEXES)
		assert set(cp.COUNT_CUSTOM_FIELDS).isdisjoint(cp.REQUIRED_CUSTOM_FIELDS)

	def test_the_index_must_be_the_exact_unique_constraint(self):
		rows = [
			{"Seq_in_index": 1, "Column_name": "rt_source_system", "Non_unique": 0, "Sub_part": None},
			{"Seq_in_index": 2, "Column_name": "rt_external_id", "Non_unique": 0, "Sub_part": None},
		]
		assert ig.index_matches(rows, ("rt_source_system", "rt_external_id"))
		assert not ig.index_matches(
			[{**r, "Non_unique": 1} for r in rows], ("rt_source_system", "rt_external_id")
		)
		assert not ig.index_matches(rows[:1], ("rt_source_system", "rt_external_id"))


class TestDataPreflight:
	def test_duplicate_keys_block_the_index_with_the_keys_named(self):
		message = ig.duplicate_provenance_message(
			[{"rt_source_system": "retail-tower", "rt_external_id": f"count:{_SESSION}", "n": 2}]
		)
		assert "unique index was NOT created" in message
		assert f"count:{_SESSION}" in message and "x2" in message
		assert "never edit or cancel" in message

	def test_the_message_names_at_most_five_keys_but_counts_them_all(self):
		rows = [{"rt_source_system": "s", "rt_external_id": f"count:{i}", "n": 2} for i in range(9)]
		message = ig.duplicate_provenance_message(rows)
		assert "9 key(s)" in message and "count:4" in message and "count:5" not in message
