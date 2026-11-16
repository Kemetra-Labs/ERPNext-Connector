# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1429 — the live auth circuit: shared Redis state, the guarded transport and the reset on save (no bench).

``frappe`` is a stand-in with a dict-backed cache and a recording logger.
"""

import sys
import types
from unittest import mock

import pytest

from retail_tower_erpnext_connector.connector import auth_circuit_store
from retail_tower_erpnext_connector.connector.auth_circuit import AuthCircuit, AuthCircuitOpen
from retail_tower_erpnext_connector.connector.http_transport import RequestsTransport

_MISSING = object()
# Built at run time and low-entropy: a secret-shaped literal in any commit keeps gitleaks red.
TOKEN = "fake-" + "x" * 24


class _Logger:
	def __init__(self, sink):
		self._sink = sink

	def error(self, payload):
		self._sink.append(("error", payload))

	def info(self, payload):
		self._sink.append(("info", payload))


class _Frappe(types.ModuleType):
	def __init__(self):
		super().__init__("frappe")
		self.values = {}
		self.logs = []
		self.cache = lambda: types.SimpleNamespace(
			get_value=self.values.get,
			set_value=self.values.__setitem__,
			delete_value=lambda key: self.values.pop(key, None),
		)
		self.logger = lambda name: _Logger(self.logs)


class _Settings:
	def __init__(self, credential_id="cred-1", token=TOKEN):
		self.dp2_base_url = "https://dp2.example"
		self.dp2_credential_id = credential_id
		self._token = token

	def get_password(self, fieldname):
		return self._token


@pytest.fixture
def frappe_stub():
	saved = sys.modules.get("frappe", _MISSING)
	fake = _Frappe()
	sys.modules["frappe"] = fake
	yield fake
	sys.modules.pop("frappe", None)
	if saved is not _MISSING:
		sys.modules["frappe"] = saved


def _open_the_circuit(settings, status=401):
	AuthCircuit(
		auth_circuit_store.FrappeCircuitStore(), credential_key=auth_circuit_store.credential_key(settings)
	).record_refusal(status)


def test_the_state_round_trips_through_the_site_cache(frappe_stub):
	store = auth_circuit_store.FrappeCircuitStore()
	assert store.load() is None
	circuit = AuthCircuit(store, credential_key="cred-1")
	assert circuit.record_refusal(401) is True
	assert auth_circuit_store.CACHE_KEY in frappe_stub.values
	assert AuthCircuit(store, credential_key="cred-1").blocked() is True  # another job sees it
	store.clear()
	assert store.load() is None


def test_a_damaged_cache_record_reads_as_closed_never_as_blocked(frappe_stub):
	frappe_stub.values[auth_circuit_store.CACHE_KEY] = {"key": "cred-1", "failures": "many"}
	assert auth_circuit_store.FrappeCircuitStore().load() is None
	frappe_stub.values[auth_circuit_store.CACHE_KEY] = "garbage"
	assert auth_circuit_store.FrappeCircuitStore().load() is None


def test_a_closed_circuit_builds_a_transport_wired_to_it(frappe_stub):
	transport = auth_circuit_store.guarded_transport(_Settings(), job_name="posting_poll", request_id="rid")
	assert isinstance(transport, RequestsTransport)


def test_an_open_circuit_holds_the_tick_back_before_any_transport_exists(frappe_stub):
	settings = _Settings()
	_open_the_circuit(settings)
	with mock.patch(
		"retail_tower_erpnext_connector.connector.http_transport.build_http_transport",
		side_effect=AssertionError("built a transport while the credential is refused"),
	):
		with pytest.raises(AuthCircuitOpen) as held:
			auth_circuit_store.guarded_transport(settings, job_name="posting_poll", request_id="rid")
	assert TOKEN not in str(held.value)


def test_pasting_a_new_token_closes_the_circuit_on_the_very_next_tick(frappe_stub):
	# The credential id is unchanged (the lab never set it): only the token differs. No Settings hook is
	# involved, so this holds however the token was changed (form, bench, provisioner).
	_open_the_circuit(_Settings(token=TOKEN))
	with pytest.raises(AuthCircuitOpen):
		auth_circuit_store.guarded_transport(
			_Settings(token=TOKEN), job_name="posting_poll", request_id="rid"
		)
	assert isinstance(
		auth_circuit_store.guarded_transport(
			_Settings(token="other-" + "y" * 24), job_name="posting_poll", request_id="rid"
		),
		RequestsTransport,
	)


def test_the_operator_reset_forces_an_immediate_probe(frappe_stub):
	settings = _Settings()
	_open_the_circuit(settings)
	with pytest.raises(AuthCircuitOpen):
		auth_circuit_store.guarded_transport(settings, job_name="posting_poll", request_id="rid")
	auth_circuit_store.reset_auth_circuit()
	assert isinstance(
		auth_circuit_store.guarded_transport(settings, job_name="posting_poll", request_id="rid"),
		RequestsTransport,
	)


def test_the_circuit_key_never_contains_the_token():
	key = auth_circuit_store.credential_key(_Settings(credential_id="cred-1"))
	assert key.startswith("cred-1:")
	assert TOKEN not in key
	assert len(key.split(":")[1]) == 12  # a short one-way fingerprint, not the token


def test_an_unreadable_token_gives_a_stable_key_and_does_not_raise():
	class _Broken:
		dp2_credential_id = ""

		def get_password(self, fieldname):
			raise RuntimeError("cannot decrypt")

	assert auth_circuit_store.credential_key(_Broken()) == ":"


def test_a_re_issued_credential_id_is_not_held_by_the_old_credentials_state(frappe_stub):
	_open_the_circuit(_Settings(credential_id="cred-old"))
	new = _Settings(credential_id="cred-new")
	assert isinstance(
		auth_circuit_store.guarded_transport(new, job_name="posting_poll", request_id="rid"),
		RequestsTransport,
	)


def test_the_open_callback_logs_once_and_captures_once_without_the_token(frappe_stub):
	with mock.patch.object(auth_circuit_store, "capture_auth_circuit_open") as capture:
		auth_circuit_store._on_open("posting_poll", "rid")(401)
	capture.assert_called_once_with(401, request_id="rid", job_name="posting_poll")
	assert frappe_stub.logs == [
		(
			"error",
			{
				"event": "connector.auth_circuit.opened",
				"status": 401,
				"job_name": "posting_poll",
				"request_id": "rid",
			},
		)
	]
	assert TOKEN not in repr(frappe_stub.logs)


def test_the_close_callback_logs_but_never_reaches_sentry(frappe_stub):
	with mock.patch.object(auth_circuit_store, "capture_auth_circuit_open") as capture:
		auth_circuit_store._on_close("posting_poll", "rid")()
	capture.assert_not_called()
	assert [event for _, event in frappe_stub.logs] == [
		{"event": "connector.auth_circuit.closed", "job_name": "posting_poll", "request_id": "rid"}
	]
