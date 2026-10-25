# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt
"""RT-883 — the posting poller resumes from the persisted feed cursor after a cache flush (no bench).

The cursor used to live only in ``frappe.cache()``: a Redis flush sent the poller back to the start
of the posting feed (no double posting, thanks to the Posting Log unique index, but a full replay).
It is now persisted in Connector Settings (``posting_feed_cursor``) with the cache as a fast path.
"""

import importlib
import sys
import types

import pytest

_POLLER = "retail_tower_erpnext_connector.connector.posting.poller"
_PKG = "retail_tower_erpnext_connector.connector.posting"
_FEED_STATE = "retail_tower_erpnext_connector.connector.feed_state"
_MISSING = object()


class _Frappe(types.ModuleType):
	"""Just what the tick touches: a logger, the cache and the Connector Settings single values."""

	def __init__(self):
		super().__init__("frappe")
		self.records = []
		self.logger = lambda name=None: types.SimpleNamespace(error=self.records.append)
		self.cache_values = {}
		self.cache = lambda: types.SimpleNamespace(
			get_value=self.cache_values.get, set_value=self.cache_values.__setitem__
		)
		self.singles = {}
		self.commits = 0
		self.db = types.SimpleNamespace(
			get_single_value=lambda doctype, field: self.singles.get(field),
			set_single_value=self._set_single_value,
			commit=self._commit,
		)
		self.exceptions = types.SimpleNamespace()

	def _set_single_value(self, doctype, field, value, update_modified=True):
		assert doctype == "Connector Settings" and update_modified is False
		self.singles[field] = value

	def _commit(self):
		self.commits += 1


@pytest.fixture
def poller(monkeypatch):
	pkg = importlib.import_module(_PKG)
	names = ("frappe", _POLLER, _FEED_STATE, f"{_PKG}.frappe_store", f"{_PKG}.frappe_glue")
	saved = [(sys.modules, n, sys.modules.get(n, _MISSING)) for n in names]
	saved += [(vars(pkg), n, vars(pkg).get(n, _MISSING)) for n in ("poller", "frappe_store", "frappe_glue")]
	fake = _Frappe()
	sys.modules["frappe"] = fake
	for name in names[1:]:
		sys.modules.pop(name, None)
	module = importlib.import_module(_POLLER)
	index_guard = importlib.import_module(f"{_PKG}.index_guard")
	monkeypatch.setattr(index_guard, "evaluate", lambda probe: index_guard.GuardResult(ok=True, missing=()))
	monkeypatch.setattr(module, "_ready_posting_path", lambda _rid: (object(), lambda item: None))
	module.frappe_stub = fake
	yield module
	for namespace, name, value in saved:
		namespace.pop(name, None)
		if value is not _MISSING:
			namespace[name] = value


def _feed(monkeypatch, poller, cursors):
	"""Serve one page per tick, advancing to the next cursor; record each pull's ``since``."""
	pulls = []
	pending = list(cursors)

	def _page(client, ack_client, *, since, post_valid):
		pulls.append(since)
		cursor = pending.pop(0) if pending else since
		return types.SimpleNamespace(
			degraded=False, degraded_message=None, cursor=cursor, next_page_token=None
		)

	monkeypatch.setattr(poller.worker, "process_page", _page)
	return pulls


def test_a_tick_persists_the_cursor_and_commits_it(poller, monkeypatch):
	_feed(monkeypatch, poller, ["c-1"])
	poller.run_posting_poll()
	assert poller.frappe_stub.singles["posting_feed_cursor"] == "c-1"
	assert poller.frappe_stub.cache_values["rt_posting_cursor"] == "c-1"
	assert poller.frappe_stub.commits == 1


def test_after_a_cache_flush_the_poller_resumes_from_the_persisted_cursor(poller, monkeypatch):
	pulls = _feed(monkeypatch, poller, ["c-1", "c-2"])
	poller.run_posting_poll()
	poller.frappe_stub.cache_values.clear()  # Redis flushed between ticks
	poller.run_posting_poll()
	assert pulls == [None, "c-1"]  # not None: the feed is not replayed from the start
	assert poller.frappe_stub.singles["posting_feed_cursor"] == "c-2"


def test_the_cache_stays_the_fast_path(poller, monkeypatch):
	pulls = _feed(monkeypatch, poller, ["c-6"])
	poller.frappe_stub.cache_values["rt_posting_cursor"] = "c-5"
	poller.frappe_stub.singles["posting_feed_cursor"] = "c-4"
	poller.run_posting_poll()
	assert pulls == ["c-5"]


def test_an_upgraded_site_keeps_its_cached_cursor_until_the_first_save(poller, monkeypatch):
	# Before RT-883 the field did not exist: the cached cursor is used and then persisted.
	pulls = _feed(monkeypatch, poller, ["c-9"])
	poller.frappe_stub.cache_values["rt_posting_cursor"] = "c-8"
	poller.run_posting_poll()
	assert pulls == ["c-8"]
	assert poller.frappe_stub.singles["posting_feed_cursor"] == "c-9"
