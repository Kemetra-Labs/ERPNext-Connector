# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1429 — the shared transport classifies a refused credential and honours the auth circuit.

``requests`` is bench-only, so a stand-in module records every call: "an open circuit makes zero network
calls" is asserted on that record, not inferred.
"""

import sys
import types

import pytest

from retail_tower_erpnext_connector.connector.auth_circuit import (
	AuthCircuit,
	AuthCircuitOpen,
	AuthRefused,
	MemoryCircuitStore,
)
from retail_tower_erpnext_connector.connector.http_transport import RequestsTransport, StatusResponse

# Built at run time and low-entropy: a secret-shaped literal in any commit keeps gitleaks red.
TOKEN = "fake-" + "x" * 24


class _HTTPError(Exception):
	pass


class _Response:
	def __init__(self, status, body=None):
		self.status_code = status
		self.headers = {}
		self._body = body if body is not None else {}

	def json(self):
		return self._body

	def raise_for_status(self):
		if self.status_code >= 400:
			raise _HTTPError(f"{self.status_code} Client Error")


class _FakeRequests(types.ModuleType):
	def __init__(self, status):
		super().__init__("requests")
		self.status = status
		self.calls = []

	def get(self, url, **kwargs):
		self.calls.append(("GET", url))
		return _Response(self.status, {"items": [], "cursor": "1"})

	def post(self, url, **kwargs):
		self.calls.append(("POST", url))
		return _Response(self.status)


@pytest.fixture
def fake_requests(monkeypatch):
	fake = _FakeRequests(200)
	monkeypatch.setitem(sys.modules, "requests", fake)
	return fake


class _Clock:
	now = 1_000.0

	def __call__(self):
		return self.now


def _transport(opened=None):
	clock = _Clock()
	circuit = AuthCircuit(MemoryCircuitStore(), clock=clock)
	transport = RequestsTransport(
		"https://dp2.example", TOKEN, circuit=circuit, on_open=None if opened is None else opened.append
	)
	return transport, circuit, clock


@pytest.mark.parametrize("status", [401, 403])
def test_get_turns_an_auth_refusal_into_a_typed_error_and_opens_the_circuit(fake_requests, status):
	fake_requests.status = status
	opened = []
	transport, circuit, _ = _transport(opened)
	with pytest.raises(AuthRefused) as refused:
		transport.get("/p", params={}, headers={})
	assert refused.value.status == status
	assert refused.value.reported is True
	assert circuit.blocked() is True
	assert opened == [status]  # the transition is reported once


def test_an_open_circuit_makes_no_network_call(fake_requests):
	fake_requests.status = 401
	transport, _, _ = _transport()
	with pytest.raises(AuthRefused):
		transport.get("/p", params={}, headers={})
	calls_after_refusal = len(fake_requests.calls)
	for _ in range(50):
		with pytest.raises(AuthCircuitOpen):
			transport.get("/p", params={}, headers={})
		with pytest.raises(AuthCircuitOpen):
			transport.post("/p", json={}, headers={})
	assert len(fake_requests.calls) == calls_after_refusal


def test_post_records_the_refusal_but_still_returns_the_status_response(fake_requests):
	# HeartbeatClient (and the ack/report clients) read the status themselves; that must not change.
	fake_requests.status = 401
	opened = []
	transport, circuit, _ = _transport(opened)
	response = transport.post("/p", json={}, headers={})
	assert isinstance(response, StatusResponse)
	assert response.status == 401
	assert circuit.blocked() is True
	assert opened == [401]


def test_a_probe_that_succeeds_closes_the_circuit(fake_requests):
	fake_requests.status = 401
	transport, circuit, clock = _transport()
	with pytest.raises(AuthRefused):
		transport.get("/p", params={}, headers={})
	fake_requests.status = 200
	clock.now += 61
	assert transport.get("/p", params={}, headers={}) == {"items": [], "cursor": "1"}
	assert circuit.blocked() is False


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_errors_behave_as_before_and_leave_the_circuit_alone(fake_requests, status):
	fake_requests.status = status
	transport, circuit, _ = _transport()
	for _ in range(5):
		with pytest.raises(_HTTPError):
			transport.get("/p", params={}, headers={})
	assert circuit.blocked() is False
	assert len(fake_requests.calls) == 5  # every call went out: no backoff for a 5xx


def test_a_5xx_does_not_close_an_open_circuit(fake_requests):
	fake_requests.status = 401
	transport, circuit, clock = _transport()
	with pytest.raises(AuthRefused):
		transport.get("/p", params={}, headers={})
	fake_requests.status = 502
	clock.now += 61
	with pytest.raises(_HTTPError):
		transport.get("/p", params={}, headers={})  # the probe reached the edge but proved nothing
	assert circuit.blocked() is True


def test_other_client_errors_keep_raising_and_prove_the_credential_was_accepted(fake_requests):
	fake_requests.status = 401
	transport, circuit, clock = _transport()
	with pytest.raises(AuthRefused):
		transport.get("/p", params={}, headers={})
	fake_requests.status = 404
	clock.now += 61
	with pytest.raises(_HTTPError):
		transport.get("/p", params={}, headers={})
	assert circuit.blocked() is False


def test_without_a_circuit_a_refusal_is_still_typed_but_not_marked_reported(fake_requests):
	fake_requests.status = 401
	transport = RequestsTransport("https://dp2.example", TOKEN)
	with pytest.raises(AuthRefused) as refused:
		transport.get("/p", params={}, headers={})
	assert refused.value.reported is False


def test_the_token_never_appears_in_an_error_or_a_callback(fake_requests):
	fake_requests.status = 401
	opened = []
	transport, _, _ = _transport(opened)
	with pytest.raises(AuthRefused) as refused:
		transport.get("/p", params={}, headers={})
	with pytest.raises(AuthCircuitOpen) as held:
		transport.get("/p", params={}, headers={})
	for text in (str(refused.value), repr(refused.value), str(held.value), repr(opened)):
		assert TOKEN not in text
