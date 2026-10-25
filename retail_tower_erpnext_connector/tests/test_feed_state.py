# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt
"""RT-883 — ``feed_state``: Connector Settings is the durable copy, the cache the fast path (no bench)."""

import importlib
import sys
import types

import pytest

_MODULE = "retail_tower_erpnext_connector.connector.feed_state"
_MISSING = object()


class _Frappe(types.ModuleType):
	def __init__(self):
		super().__init__("frappe")
		self.cache_values = {}
		self.cache = lambda: types.SimpleNamespace(
			get_value=self.cache_values.get, set_value=self.cache_values.__setitem__
		)
		self.singles = {}
		self.events = []
		self.fail_write = False
		self.db = types.SimpleNamespace(
			get_single_value=lambda doctype, field: self.singles.get(field),
			set_single_value=self._set_single_value,
			commit=lambda: self.events.append("commit"),
		)

	def _set_single_value(self, doctype, field, value, update_modified=True):
		assert doctype == "Connector Settings" and update_modified is False
		if self.fail_write:
			raise RuntimeError("database unavailable")
		self.singles[field] = value
		self.events.append(("write", field))


@pytest.fixture
def state():
	saved = [(n, sys.modules.get(n, _MISSING)) for n in ("frappe", _MODULE)]
	fake = _Frappe()
	sys.modules["frappe"] = fake
	sys.modules.pop(_MODULE, None)
	module = importlib.import_module(_MODULE)
	module.fake = fake
	yield module
	for name, value in saved:
		sys.modules.pop(name, None)
		if value is not _MISSING:
			sys.modules[name] = value


def test_load_prefers_the_cache(state):
	state.fake.cache_values["k"] = "cached"
	state.fake.singles["f"] = "persisted"
	assert state.load("k", "f") == "cached"


def test_load_after_a_cache_miss_reads_the_field_and_warms_the_cache(state):
	state.fake.singles["f"] = "persisted"
	assert state.load("k", "f") == "persisted"
	assert state.fake.cache_values["k"] == "persisted"


@pytest.mark.parametrize("stored", [None, ""])
def test_load_with_nothing_stored_is_none(state, stored):
	state.fake.singles["f"] = stored
	assert state.load("k", "f") is None
	assert "k" not in state.fake.cache_values


def test_save_writes_and_commits_before_the_cache_moves(state):
	state.save("k", "f", "c-2")
	assert state.fake.singles["f"] == "c-2"
	assert state.fake.events == [("write", "f"), "commit"]
	assert state.fake.cache_values["k"] == "c-2"


def test_a_failed_write_leaves_the_cache_where_it_was(state):
	state.fake.cache_values["k"] = "c-1"
	state.fake.fail_write = True
	with pytest.raises(RuntimeError):
		state.save("k", "f", "c-2")
	assert state.fake.cache_values["k"] == "c-1"


def test_json_state_round_trips_in_order_through_the_field(state):
	value = {"r-2": {"failures": 2}, "r-1": {"failures": 1}}  # oldest first, not sorted
	state.save_json("k", "f", value)
	state.fake.cache_values.clear()
	loaded = state.load_json("k", "f")
	assert loaded == value and list(loaded) == ["r-2", "r-1"]
	assert state.fake.cache_values["k"] == value


@pytest.mark.parametrize("stored", [None, ""])
def test_load_json_with_nothing_stored_is_none(state, stored):
	state.fake.singles["f"] = stored
	assert state.load_json("k", "f") is None


def test_an_empty_retry_set_in_the_cache_is_not_a_miss(state):
	state.fake.cache_values["k"] = {}
	state.fake.singles["f"] = '{"r-1": {"failures": 1}}'
	assert state.load_json("k", "f") == {}
