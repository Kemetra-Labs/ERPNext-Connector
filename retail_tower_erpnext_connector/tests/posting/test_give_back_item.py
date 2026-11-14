# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1340 — the fixed promotion give-back Item (owner decision 2026-11-14).

The pure checks (:mod:`posting.give_back_item`) and the install/migrate step that creates the Item
(:func:`connector.schema.ensure_give_back_item`), the latter against a minimal frappe stand-in.
"""

import importlib
import sys
import types

import pytest

from retail_tower_erpnext_connector.connector.posting import give_back_item as gbi
from retail_tower_erpnext_connector.connector.posting.return_builder import (
	GiveBackItem,
	GiveBackItemNotConfigured,
)

_SCHEMA = "retail_tower_erpnext_connector.connector.schema"
_MISSING = object()
_OK = {"disabled": 0, "is_stock_item": 0, "is_sales_item": 1, "stock_uom": "Nos"}


class TestResolve:
	def test_a_usable_item_resolves_with_its_own_stock_uom(self):
		assert gbi.resolve_give_back_item({**_OK, "stock_uom": "Unit"}) == GiveBackItem(
			"RT-PROMO-GIVEBACK", "Unit"
		)

	@pytest.mark.parametrize(
		("row", "why"),
		[
			(None, "does not exist"),
			({**_OK, "disabled": 1}, "is disabled"),
			({**_OK, "is_stock_item": 1}, "is a stock Item"),
			({**_OK, "stock_uom": None}, "has no stock UOM"),
			({**_OK, "is_sales_item": 0}, "is not a sales Item"),
		],
	)
	def test_an_unusable_item_fails_closed_with_the_reason(self, row, why):
		with pytest.raises(GiveBackItemNotConfigured, match=why):
			gbi.resolve_give_back_item(row)


class _Frappe(types.ModuleType):
	def __init__(self, *, existing=(), insert_error=None):
		super().__init__("frappe")
		self.existing = set(existing)
		self.inserted, self.commits, self.logged = [], 0, []
		self.insert_error = insert_error
		self.savepoints, self.rollbacks, self.set_names = [], [], []
		self.db = types.SimpleNamespace(
			exists=lambda doctype, name: (doctype, name) in self.existing,
			get_value=self._get_value,
			commit=self._commit,
			savepoint=self.savepoints.append,
			rollback=lambda save_point=None: self.rollbacks.append(save_point),
		)
		self.logger = lambda name=None: types.SimpleNamespace(error=self.logged.append)

	def _commit(self):
		self.commits += 1

	def _get_value(self, doctype, filters, field, order_by=None):
		# Leaf Item Groups only; the named lookup asks for "Services" itself.
		assert doctype == "Item Group" and filters["is_group"] == 0
		leaves = sorted(name for kind, name in self.existing if kind == "Item Group")
		if "name" in filters:
			return filters["name"] if filters["name"] in leaves else None
		return leaves[0] if leaves else None

	def get_doc(self, payload):
		fake = self

		class _Doc:
			def insert(self, ignore_permissions=False, set_name=None):
				fake.set_names.append(set_name)
				if fake.insert_error is not None:
					raise fake.insert_error
				fake.inserted.append(payload)

		return _Doc()


@pytest.fixture
def load_schema():
	saved = [(name, sys.modules.get(name, _MISSING)) for name in ("frappe", _SCHEMA)]

	def _load(fake):
		sys.modules["frappe"] = fake
		sys.modules.pop(_SCHEMA, None)
		return importlib.import_module(_SCHEMA)

	yield _load
	for name, value in saved:
		sys.modules.pop(name, None)
		if value is not _MISSING:
			sys.modules[name] = value


_READY = {("Item Group", "Products"), ("Item Group", "Services"), ("UOM", "Nos")}


class TestEnsureGiveBackItem:
	def test_a_missing_item_is_created_non_stock_and_committed(self, load_schema):
		fake = _Frappe(existing=_READY)
		load_schema(fake).ensure_give_back_item()
		((doc),) = fake.inserted
		assert doc["item_group"] == "Services"  # a leaf group, never the "All Item Groups" root
		assert (doc["doctype"], doc["item_code"], doc["is_stock_item"], doc["stock_uom"]) == (
			"Item",
			"RT-PROMO-GIVEBACK",
			0,
			"Nos",
		)
		assert fake.commits == 1

	def test_an_existing_item_is_never_touched(self, load_schema):
		fake = _Frappe(existing=_READY | {("Item", "RT-PROMO-GIVEBACK")})
		load_schema(fake).ensure_give_back_item()
		assert fake.inserted == [] and fake.commits == 0

	def test_before_the_setup_wizard_it_is_deferred_and_logged(self, load_schema):
		fake = _Frappe(existing=())
		load_schema(fake).ensure_give_back_item()
		assert fake.inserted == []
		assert fake.logged[0]["event"] == "give_back_item.deferred"

	def test_an_insert_failure_never_fails_the_migrate(self, load_schema):
		fake = _Frappe(existing=_READY, insert_error=RuntimeError("Income Account missing"))
		load_schema(fake).ensure_give_back_item()  # no raise
		assert fake.logged[0]["event"] == "give_back_item.ensure_failed"
		assert "RuntimeError: Income Account missing" in fake.logged[0]["detail"]

	def test_the_item_is_named_by_its_code_whatever_the_naming_setting(self, load_schema):
		# Review: Item.autoname gives a series name under item_naming_by = "Naming Series"; set_name skips it.
		fake = _Frappe(existing=_READY)
		load_schema(fake).ensure_give_back_item()
		assert fake.set_names == ["RT-PROMO-GIVEBACK"]

	def test_a_failed_insert_is_rolled_back_to_its_savepoint(self, load_schema):
		fake = _Frappe(existing=_READY, insert_error=RuntimeError("on_update failed"))
		load_schema(fake).ensure_give_back_item()
		assert fake.savepoints == ["rt_give_back_item"] and fake.rollbacks == ["rt_give_back_item"]
		assert fake.commits == 0

	def test_without_services_the_first_leaf_group_is_used(self, load_schema):
		fake = _Frappe(existing={("Item Group", "Products"), ("UOM", "Nos")})
		load_schema(fake).ensure_give_back_item()
		assert fake.inserted[0]["item_group"] == "Products"
