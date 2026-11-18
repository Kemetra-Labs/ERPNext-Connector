# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1460 (audit A-21): ``errors`` and ``frappe_exceptions`` are leaf modules with the old names intact.

``UnmappedUnit`` moved from ``builder`` to ``errors`` so ``uom`` no longer imports ``builder``; the two
Frappe exception sets moved from ``frappe_glue`` to ``frappe_exceptions`` so ``frappe_count_port`` no
longer imports ``frappe_glue``. Moving a class can change ``isinstance`` identity if some code still
reaches it by the old path, so these tests pin that every path yields the one class object.
"""

import ast
import importlib
import sys
import types
from pathlib import Path

import pytest

from retail_tower_erpnext_connector.connector.posting import builder, count_flow, errors, uom

_PKG = "retail_tower_erpnext_connector.connector.posting"
_POSTING = Path(builder.__file__).parent


def test_unmapped_unit_is_one_class_on_every_import_path():
	assert builder.UnmappedUnit is errors.UnmappedUnit
	assert uom.UnmappedUnit is errors.UnmappedUnit
	assert count_flow.UnmappedUnit is errors.UnmappedUnit


def test_unmapped_unit_raised_by_the_uom_map_is_caught_by_the_old_builder_name():
	with pytest.raises(builder.UnmappedUnit) as caught:
		uom.UomMap({}).resolve("each")
	assert isinstance(caught.value, errors.UnmappedUnit)
	assert caught.value.unit == "each"


@pytest.mark.parametrize("name", ["errors.py", "frappe_exceptions.py"])
def test_leaf_modules_import_nothing_from_the_package(name):
	tree = ast.parse((_POSTING / name).read_text(encoding="utf-8"))
	for node in ast.walk(tree):
		if isinstance(node, ast.ImportFrom):
			assert node.level == 0 and not (node.module or "").startswith("retail_tower_erpnext_connector"), (
				name
			)
		if isinstance(node, ast.Import):
			assert not any(a.name.startswith("retail_tower_erpnext_connector") for a in node.names), name


def test_frappe_exception_sets_track_the_current_frappe(monkeypatch):
	"""Resolved at call time, so they follow whichever ``frappe`` is installed (and the glue tests' stand-in)."""
	fake = types.ModuleType("frappe")
	fake.QueryDeadlockError = type("QueryDeadlockError", (Exception,), {})
	fake.exceptions = types.SimpleNamespace(
		UniqueValidationError=type("UniqueValidationError", (Exception,), {}),
		DuplicateEntryError=type("DuplicateEntryError", (Exception,), {}),
	)
	monkeypatch.setitem(sys.modules, "frappe", fake)
	module = importlib.import_module(f"{_PKG}.frappe_exceptions")
	transient = module._transient_exceptions()
	assert fake.QueryDeadlockError in transient and TimeoutError in transient
	assert set(module._dup_provenance_exceptions()) == {
		fake.exceptions.UniqueValidationError,
		fake.exceptions.DuplicateEntryError,
	}


def test_dup_provenance_degrades_to_exception_when_frappe_names_none(monkeypatch):
	fake = types.ModuleType("frappe")
	fake.exceptions = types.SimpleNamespace()
	monkeypatch.setitem(sys.modules, "frappe", fake)
	module = importlib.import_module(f"{_PKG}.frappe_exceptions")
	assert module._dup_provenance_exceptions() == (Exception,)
