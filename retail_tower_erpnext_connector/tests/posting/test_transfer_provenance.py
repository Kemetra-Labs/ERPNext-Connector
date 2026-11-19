# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1514 — the transfer legs' provenance plumbing: fields, unique index, patch, install hooks (no frappe).

The Stock Entry provenance fields and the unique index are what make each transfer leg exactly-once and
let the stock import recognise the legs' own ledger rows as Retail-origin (it reads these same two fields
from any voucher type that has them). The row field ``rt_transfer_line_ref`` is what a receipt row is
checked against.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path

import pytest

from retail_tower_erpnext_connector import hooks
from retail_tower_erpnext_connector.connector.posting import compatibility_profile as cp
from retail_tower_erpnext_connector.connector.posting import index_guard as ig

_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = _ROOT / "fixtures" / "custom_field.json"
_PATCHES_TXT = _ROOT / "patches.txt"


def _fixture() -> dict[str, dict]:
	return {f["name"]: f for f in json.loads(_FIXTURE.read_text(encoding="utf-8"))}


class TestFieldsAndIndex:
	@pytest.mark.parametrize(
		"name, dt, fieldname, length",
		[
			("Stock Entry-rt_source_system", "Stock Entry", "rt_source_system", 100),
			("Stock Entry-rt_external_id", "Stock Entry", "rt_external_id", 200),
			("Stock Entry Detail-rt_transfer_line_ref", "Stock Entry Detail", "rt_transfer_line_ref", 140),
		],
	)
	def test_the_transfer_custom_fields_ship_read_only_and_never_copied(self, name, dt, fieldname, length):
		field = _fixture()[name]
		assert (field["dt"], field["fieldname"], field["fieldtype"], field["length"]) == (
			dt,
			fieldname,
			"Data",
			length,
		)
		assert (field["read_only"], field["no_copy"]) == (1, 1)

	def test_the_transfer_custom_fields_are_exported_by_hooks(self):
		(fixture,) = [f for f in hooks.fixtures if f["dt"] == "Custom Field"]
		(name_filter,) = [f for f in fixture["filters"] if f[0] == "name"]
		assert set(cp.TRANSFER_CUSTOM_FIELDS) <= set(name_filter[2])
		assert set(cp.TRANSFER_CUSTOM_FIELDS) == {
			"Stock Entry-rt_source_system",
			"Stock Entry-rt_external_id",
			"Stock Entry Detail-rt_transfer_line_ref",
		}

	def test_the_transfer_index_is_its_own_guard_and_never_pauses_sales_or_counts(self):
		assert ig.TRANSFER_INDEXES == (
			("Stock Entry", "unique_rt_se_provenance", ("rt_source_system", "rt_external_id")),
		)
		assert not set(ig.TRANSFER_INDEXES) & set(ig.G5_INDEXES)
		assert not set(ig.TRANSFER_INDEXES) & set(ig.COUNT_INDEXES)
		assert set(cp.TRANSFER_CUSTOM_FIELDS).isdisjoint(cp.REQUIRED_CUSTOM_FIELDS)
		assert set(cp.TRANSFER_CUSTOM_FIELDS).isdisjoint(cp.COUNT_CUSTOM_FIELDS)

	def test_the_provenance_field_descriptions_cite_the_issue_and_say_do_not_edit(self):
		for name in ("Stock Entry-rt_source_system", "Stock Entry-rt_external_id"):
			text = _fixture()[name]["description"]
			assert "RT-1514" in text and "Do not edit" in text


class TestPatchAndInstall:
	@pytest.fixture
	def frappe_standin(self):
		saved = {n: sys.modules.get(n) for n in ("frappe", "frappe.custom", "frappe.custom.doctype")}
		names = [
			"frappe",
			"frappe.custom",
			"frappe.custom.doctype",
			"frappe.custom.doctype.custom_field",
			"frappe.custom.doctype.custom_field.custom_field",
		]
		saved = {n: sys.modules.get(n) for n in names}
		fake = types.ModuleType("frappe")
		fake.db = types.SimpleNamespace(commit=lambda: None)
		sys.modules["frappe"] = fake
		for n in names[1:-1]:
			sys.modules[n] = types.ModuleType(n)
		leaf = types.ModuleType(names[-1])
		leaf.create_custom_fields = lambda *a, **k: None
		sys.modules[names[-1]] = leaf
		for mod in (
			"retail_tower_erpnext_connector.patches.stock_entry_unique_provenance",
			"retail_tower_erpnext_connector.connector.schema",
		):
			sys.modules.pop(mod, None)
		yield fake
		for n, v in saved.items():
			sys.modules.pop(n, None)
			if v is not None:
				sys.modules[n] = v
		for mod in (
			"retail_tower_erpnext_connector.patches.stock_entry_unique_provenance",
			"retail_tower_erpnext_connector.connector.schema",
		):
			sys.modules.pop(mod, None)

	def test_the_patch_owns_the_indexed_columns_in_sync_with_the_fixture(self, frappe_standin):
		patch = importlib.import_module(
			"retail_tower_erpnext_connector.patches.stock_entry_unique_provenance"
		)
		by_field = {f["fieldname"]: f for f in patch._INDEXED_FIELDS["Stock Entry"]}
		assert set(by_field) == {"rt_source_system", "rt_external_id"}
		fixture = _fixture()
		for fieldname, spec in by_field.items():
			shipped = fixture[f"Stock Entry-{fieldname}"]
			for key in (
				"fieldtype",
				"length",
				"insert_after",
				"read_only",
				"no_copy",
				"print_hide",
				"translatable",
			):
				assert spec[key] == shipped[key], (fieldname, key)

	def test_the_patch_is_registered_after_model_sync(self):
		text = _PATCHES_TXT.read_text(encoding="utf-8")
		assert "retail_tower_erpnext_connector.patches.stock_entry_unique_provenance" in text
		post = text.split("[post_model_sync]", 1)[1]
		assert "stock_entry_unique_provenance" in post

	def test_the_index_constants_name_the_stock_entry_index(self, frappe_standin):
		schema = importlib.import_module("retail_tower_erpnext_connector.connector.schema")
		assert schema.STOCK_ENTRY_INDEX == "unique_rt_se_provenance"
		assert schema.STOCK_ENTRY_COLUMNS == ("rt_source_system", "rt_external_id")

	@pytest.mark.parametrize("hook", ["after_install", "after_migrate"])
	def test_install_and_every_migrate_ensure_the_stock_entry_index(self, frappe_standin, monkeypatch, hook):
		schema = importlib.import_module("retail_tower_erpnext_connector.connector.schema")
		calls: list[str] = []
		for name in (
			"ensure_g5_indexes",
			"ensure_stock_reconciliation_index",
			"ensure_stock_entry_index",
			"ensure_give_back_item",
		):
			monkeypatch.setattr(schema, name, lambda n=name: calls.append(n))
		getattr(schema, hook)()
		assert "ensure_stock_entry_index" in calls
