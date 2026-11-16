# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1429 — a whole poller tick while Backend-Core refuses the Connector credential (no bench).

The RT-39 tick harness (real ``run_posting_poll`` / ``run_bin_view_poll`` over a frappe stand-in and a
recording transport) is reused, with the auth circuit pre-opened in the fake site cache.

* a held-back tick makes no call, charges nothing, reports nothing and ends quietly;
* a posting whose ack is refused aborts the page: the error reaches the scheduler, the cursor is NOT
  advanced (a cursor save would hit the stand-in's missing ``db``), so the item is re-offered once the
  credential is fixed, and the refusal is not captured a second time.
"""

import importlib
import sys
import time
import types

import pytest

from retail_tower_erpnext_connector.connector import auth_circuit_store
from retail_tower_erpnext_connector.connector.auth_circuit import AuthRefused

from .test_request_id import (
	_MISSING,
	_ROOT,
	_SENTRY,
	_bin_view_tick,
	_fake_frappe,
	_posting_page,
	_posting_tick,
	_RecordingTransport,
)


@pytest.fixture
def stub():
	"""The RT-39 ``frappe`` stand-in (and its cleanup of connector modules imported under it), plus a ``db``
	because the feed cursor falls back to Connector Settings on a cache miss, and the cache delete the
	circuit reset needs."""
	before = set(sys.modules)
	saved_frappe = sys.modules.get("frappe", _MISSING)
	fake = _fake_frappe()
	cache = fake.cache()
	cache.delete_value = lambda key: cache.values.pop(key, None)
	fake.db = types.SimpleNamespace(
		get_single_value=lambda doctype, field: None,
		set_single_value=lambda *a, **k: None,
		commit=lambda: None,
	)
	sys.modules["frappe"] = fake
	yield fake
	for name in set(sys.modules) - before:
		if name.startswith(_ROOT):
			sys.modules.pop(name, None)
			parent, _, child = name.rpartition(".")
			if parent in sys.modules:
				vars(sys.modules[parent]).pop(child, None)
	if saved_frappe is _MISSING:
		sys.modules.pop("frappe", None)
	else:
		sys.modules["frappe"] = saved_frappe


def _open_the_circuit(stub, *, retry_in_s=600.0):
	now = time.time()
	stub.cache().values[auth_circuit_store.CACHE_KEY] = {
		"key": ":",  # the stand-in settings carry no credential id and no token
		"failures": 1,
		"status": 401,
		"opened_at": now,
		"next_probe_at": now + retry_in_s,
	}


@pytest.fixture
def captures(monkeypatch):
	sentry = importlib.import_module(_SENTRY)
	captured = []
	monkeypatch.setattr(sentry, "capture_tick_error", lambda *a, **k: captured.append((a, k)) or True)
	return captured


def _events(stub):
	return [record.get("event") for record in stub.logger().records]


def test_a_posting_tick_is_held_back_quietly_while_the_credential_is_refused(stub, captures, monkeypatch):
	_open_the_circuit(stub)
	transport, glue_calls = _posting_tick(monkeypatch)
	assert transport.calls == []  # no pull and no ack
	assert glue_calls == []  # nothing was posted
	assert "posting.poll.skipped" in _events(stub)
	assert captures == []  # a held-back tick is not an error


def test_a_bin_view_tick_is_held_back_before_anything_is_charged(stub, captures, monkeypatch):
	_open_the_circuit(stub)
	transport = _bin_view_tick(monkeypatch)
	assert transport.calls == []
	assert "bin_view.poll.skipped" in _events(stub)
	assert captures == []


def test_a_tick_runs_normally_once_the_probe_window_has_passed(stub, captures, monkeypatch):
	_open_the_circuit(stub, retry_in_s=-1.0)  # the probe is due
	transport, glue_calls = _posting_tick(monkeypatch)
	assert [method for method, _path, _headers in transport.calls] == ["GET", "POST"]
	assert len(glue_calls) == 1
	assert captures == []


def test_a_refused_ack_aborts_the_page_without_advancing_the_cursor_or_a_second_capture(
	stub, captures, monkeypatch
):
	def _ack_is_refused(work_item, **_deps):
		raise AuthRefused(401, reported=True)  # what the posting client raises for a 401 ack

	transport = _RecordingTransport(_posting_page())
	with pytest.raises(AuthRefused):
		_posting_tick(monkeypatch, transport, post=_ack_is_refused)
	assert [method for method, _path, _headers in transport.calls] == ["GET"]  # the pull happened
	assert captures == []  # the circuit reported it once; the tick does not report it again


def test_the_page_is_not_lost_it_is_offered_again_after_the_fix(stub, captures, monkeypatch):
	def _ack_is_refused(work_item, **_deps):
		raise AuthRefused(401, reported=True)

	with pytest.raises(AuthRefused):
		_posting_tick(monkeypatch, _RecordingTransport(_posting_page()), post=_ack_is_refused)
	# The operator forces a probe (or pasted a new token, which is a new circuit key): the same feed
	# position is pulled again.
	auth_circuit_store.reset_auth_circuit()
	transport, glue_calls = _posting_tick(monkeypatch, _RecordingTransport(_posting_page()))
	assert [method for method, _path, _headers in transport.calls] == ["GET", "POST"]
	assert len(glue_calls) == 1  # the item is posted (or replayed) on the re-offer
