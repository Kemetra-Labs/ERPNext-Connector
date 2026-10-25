# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt
"""RT-379 — the poller composes the Compatibility Profile with the Gate G5 index guard (no bench).

Order of one tick: G5 guard → build the posting path (config) → Compatibility Profile → pull.
A critical profile finding pauses posting: no page is pulled, so the backlog stays in Backend-Core
(D2). Warnings alone let posting proceed. The profile sees the maps the build already parsed.
"""

import importlib
import sys
import types

import pytest

_POLLER = "retail_tower_erpnext_connector.connector.posting.poller"
_PKG = "retail_tower_erpnext_connector.connector.posting"
_MISSING = object()


class _Logger:
	def __init__(self):
		self.records = []

	def error(self, record):
		self.records.append(record)

	info = warning = error


class _Frappe(types.ModuleType):
	def __init__(self, settings=None):
		super().__init__("frappe")
		self._logger = _Logger()
		self.logger = lambda name=None: self._logger
		self._cache = {}
		self.cache = lambda: types.SimpleNamespace(
			get_value=self._cache.get, set_value=lambda k, v: self._cache.__setitem__(k, v)
		)
		self.db = types.SimpleNamespace(
			get_single_value=lambda doctype, field: None,
			set_single_value=lambda *a, **k: None,
			commit=lambda: None,
		)
		self._settings = settings
		self.get_doc = lambda doctype: self._settings
		self.exceptions = types.SimpleNamespace()


@pytest.fixture
def load_poller(monkeypatch):
	pkg = importlib.import_module(_PKG)
	names = ("frappe", _POLLER, f"{_PKG}.frappe_store", f"{_PKG}.frappe_glue")
	saved = [(sys.modules, n, sys.modules.get(n, _MISSING)) for n in names]
	saved += [(vars(pkg), n, vars(pkg).get(n, _MISSING)) for n in ("poller", "frappe_store", "frappe_glue")]

	def _load(fake, *, guard_ok=True):
		sys.modules["frappe"] = fake
		for name in names[1:]:
			sys.modules.pop(name, None)
		poller = importlib.import_module(_POLLER)
		index_guard = importlib.import_module(f"{_PKG}.index_guard")
		schema = importlib.import_module("retail_tower_erpnext_connector.connector.schema")
		monkeypatch.setattr(schema, "index_present", lambda *a: guard_ok)
		monkeypatch.setattr(
			index_guard, "evaluate", lambda probe: index_guard.GuardResult(ok=bool(probe()), missing=())
		)
		return poller

	yield _load
	for namespace, name, value in saved:
		namespace.pop(name, None)
		if value is not _MISSING:
			namespace[name] = value


def _wire(monkeypatch, poller, *, profile_ok):
	"""Stub the posting path and the page pull; return the call log."""
	calls = {"build": 0, "preflight": 0, "pages": 0}

	def _preflight():
		calls["preflight"] += 1
		return profile_ok

	def _build(_correlation_id):
		calls["build"] += 1
		return object(), lambda item: None, _preflight

	def _page(*args, **kwargs):
		calls["pages"] += 1
		return types.SimpleNamespace(degraded=False, degraded_message=None, cursor="c1", next_page_token=None)

	monkeypatch.setattr(poller, "_build_posting_path", _build)
	monkeypatch.setattr(poller.worker, "process_page", _page)
	return calls


class TestComposition:
	def test_a_compatible_site_pulls_pages(self, load_poller, monkeypatch):
		poller = load_poller(_Frappe())
		calls = _wire(monkeypatch, poller, profile_ok=True)
		poller.run_posting_poll()
		assert calls == {"build": 1, "preflight": 1, "pages": 1}

	def test_a_critical_profile_pauses_posting_and_pulls_nothing(self, load_poller, monkeypatch):
		poller = load_poller(_Frappe())
		calls = _wire(monkeypatch, poller, profile_ok=False)
		poller.run_posting_poll()
		assert calls == {"build": 1, "preflight": 1, "pages": 0}

	def test_a_preflight_that_raises_pulls_nothing(self, load_poller, monkeypatch):
		# Trivial 7: an unexpected profile error never lets the tick pull.
		poller = load_poller(_Frappe())
		calls = _wire(monkeypatch, poller, profile_ok=True)

		def _build(_correlation_id):
			def _boom():
				raise RuntimeError("profile bug")

			return object(), lambda item: None, _boom

		monkeypatch.setattr(poller, "_build_posting_path", _build)
		poller.run_posting_poll()
		assert calls["pages"] == 0

	def test_the_g5_guard_runs_first_and_its_failure_skips_the_profile(self, load_poller, monkeypatch):
		poller = load_poller(_Frappe(), guard_ok=False)
		calls = _wire(monkeypatch, poller, profile_ok=True)
		poller.run_posting_poll()
		assert calls == {"build": 0, "preflight": 0, "pages": 0}


class _Settings:
	"""A configured Connector Settings single (maps as child rows)."""

	def __init__(self):
		self.dp2_base_url = "https://dp2.example"
		self.uom_map = [{"dp2_unit": "each", "erpnext_uom": "Nos"}]
		self.warehouse_map = [{"store_id": "store-1", "warehouse": "Stores - RT"}]
		self.store_customer_map = [{"store_id": "store-1", "customer": "Walk-in"}]
		self.tender_mode_map = [{"tender_method": "cash", "mode_of_payment": "Cash"}]

	def get_password(self, field):
		return "token-value"


class TestPreflightSeesTheParsedMaps:
	def test_the_build_binds_the_profile_to_the_parsed_maps(self, load_poller, monkeypatch):
		settings = _Settings()
		poller = load_poller(_Frappe(settings))
		glue = importlib.import_module(f"{_PKG}.compatibility_glue")
		seen = {}
		monkeypatch.setattr(glue, "run_preflight", lambda s, maps: seen.update(settings=s, maps=maps) or True)
		monkeypatch.setattr(poller, "_warn_credential_lifecycle", lambda s: None)
		_client, _post_valid, preflight = poller._build_posting_path("5f0c6f7e-3c1a-4b8e-9d2f-1a2b3c4d5e6f")
		assert preflight() is True
		assert seen["settings"] is settings
		assert seen["maps"] == {
			"uom": {"each": "Nos"},
			"warehouse": {"store-1": {"doctype": "Warehouse", "name": "Stores - RT"}},
			"customer": {"store-1": "Walk-in"},
			"tender": {"cash": "Cash"},
		}
