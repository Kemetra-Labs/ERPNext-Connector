# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1429 — a refused credential is reported once, when the circuit opens, and never once per tick.

The unit under test is the seam between the transport's circuit and the Connector's Sentry client:
``report_unhandled`` (what every poller entrypoint wraps its tick in) and the on-open capture.
"""

import sys
import types
from unittest import mock

import pytest

from retail_tower_erpnext_connector.connector.auth_circuit import (
	AuthCircuit,
	AuthCircuitOpen,
	AuthRefused,
	MemoryCircuitStore,
)
from retail_tower_erpnext_connector.connector.http_transport import RequestsTransport
from retail_tower_erpnext_connector.connector.observability import sentry_client
from retail_tower_erpnext_connector.connector.observability.scrubber import ALLOWED_TAGS
from retail_tower_erpnext_connector.connector.posting.transport import PostingFeedClient

_REQUEST_ID = "5f0c6f7e-3c1a-4b8e-9d2f-1a2b3c4d5e6f"
_DSN = "https://0123456789abcdef0123456789abcdef@o123.ingest.de.sentry.io/4567"
_ENV = {sentry_client.DSN_ENV: _DSN, sentry_client.ENVIRONMENT_ENV: "staging"}
# Built at run time and low-entropy: a secret-shaped literal in any commit keeps gitleaks red.
TOKEN = "fake-" + "x" * 24


@pytest.fixture(autouse=True)
def _fresh_sentry_cache():
	sentry_client.reset_cache()
	yield
	sentry_client.reset_cache()


def test_the_tags_the_auth_capture_uses_are_on_the_obs1_allowlist():
	# A tag outside the allowlist is stripped by ``scrub_event`` before it leaves the process.
	assert {"status", "outcome"} <= ALLOWED_TAGS


def test_report_unhandled_does_not_capture_a_refusal_the_circuit_already_reported():
	with mock.patch.object(sentry_client, "capture_tick_error") as capture:
		with pytest.raises(AuthRefused):
			with sentry_client.report_unhandled(job_name="posting_poll", request_id=_REQUEST_ID):
				raise AuthRefused(401, reported=True)
	capture.assert_not_called()


def test_report_unhandled_still_captures_a_refusal_nobody_reported():
	with mock.patch.object(sentry_client, "capture_tick_error") as capture:
		with pytest.raises(AuthRefused):
			with sentry_client.report_unhandled(job_name="posting_poll", request_id=_REQUEST_ID):
				raise AuthRefused(401, reported=False)
	capture.assert_called_once()


def test_report_unhandled_skips_a_held_back_tick_quietly():
	with mock.patch.object(sentry_client, "capture_tick_error") as capture:
		with sentry_client.report_unhandled(job_name="posting_poll", request_id=_REQUEST_ID):
			raise AuthCircuitOpen(retry_in_s=30)  # does not propagate: the tick simply ends
	capture.assert_not_called()


def test_other_errors_are_still_captured_and_re_raised():
	boom = RuntimeError("boom")
	with mock.patch.object(sentry_client, "capture_tick_error") as capture:
		with pytest.raises(RuntimeError):
			with sentry_client.report_unhandled(job_name="posting_poll", request_id=_REQUEST_ID):
				raise boom
	capture.assert_called_once()


def test_the_open_capture_is_one_error_tagged_with_the_status_and_the_outcome():
	hub = mock.MagicMock()
	with mock.patch.object(sentry_client, "_hub_for", return_value=hub):
		assert (
			sentry_client.capture_auth_circuit_open(
				401, request_id=_REQUEST_ID, job_name="posting_poll", environ=_ENV
			)
			is True
		)
	hub.capture_exception.assert_called_once()
	exc = hub.capture_exception.call_args.args[0]
	tags = hub.capture_exception.call_args.kwargs["tags"]
	assert isinstance(exc, AuthRefused)
	assert tags["status"] == "401"
	assert tags["outcome"] == "auth_failure"
	assert tags["job_name"] == "posting_poll"
	assert tags["request_id"] == _REQUEST_ID
	assert tags["service"] == sentry_client.SERVICE


def test_the_open_capture_is_inert_without_a_dsn_and_never_raises():
	assert (
		sentry_client.capture_auth_circuit_open(401, request_id=_REQUEST_ID, job_name="x", environ={})
		is False
	)
	with mock.patch.object(sentry_client, "_hub_for", side_effect=RuntimeError("sdk down")):
		assert (
			sentry_client.capture_auth_circuit_open(401, request_id=_REQUEST_ID, job_name="x", environ=_ENV)
			is False
		)


class _Resp:
	def __init__(self, status):
		self.status_code = status
		self.headers = {}

	def json(self):
		return {}

	def raise_for_status(self):
		if self.status_code >= 400:
			raise RuntimeError(f"HTTP {self.status_code}")


def test_100_refused_ticks_produce_exactly_one_sentry_capture(monkeypatch):
	# The incident: the credential expired and every minute's tick was refused. Run 100 ticks through the
	# real transport, circuit and report_unhandled; only the opening refusal may reach Sentry.
	fake = types.ModuleType("requests")
	fake.calls = 0

	def get(url, **kwargs):
		fake.calls += 1
		return _Resp(401)

	fake.get = get
	monkeypatch.setitem(sys.modules, "requests", fake)

	clock = mock.Mock(return_value=1_000.0)
	circuit = AuthCircuit(MemoryCircuitStore(), clock=clock)
	captures = []
	transport = RequestsTransport(
		"https://dp2.example",
		TOKEN,
		circuit=circuit,
		on_open=lambda status: captures.append(
			sentry_client.capture_auth_circuit_open(
				status, request_id=_REQUEST_ID, job_name="posting_poll", environ={}
			)
		),
	)

	with mock.patch.object(sentry_client, "capture_tick_error") as generic_capture:
		for _ in range(100):
			clock.return_value += 1.0
			try:
				with sentry_client.report_unhandled(job_name="posting_poll", request_id=_REQUEST_ID):
					transport.get("/api/connector/v1/erpnext/postings", params={}, headers={})
			except AuthRefused:
				pass  # the opening tick still fails once, as a failed tick should

	assert len(captures) == 1  # exactly one open event
	# Every capture that reached the SDK entry point is that one event: none is a per-tick generic one.
	assert generic_capture.call_count == 1
	assert generic_capture.call_args.kwargs["extra_tags"]["outcome"] == "auth_failure"
	# 100 ticks one second apart: the opening call, plus ONE probe once the 60 s window ended (refused
	# again, so the window doubled and no second probe fits). The other 98 ticks never left the process.
	assert fake.calls == 2


def test_a_refused_ack_is_a_typed_refusal_not_a_generic_runtime_error():
	# Today a 401 on the ack POST surfaces as ``RuntimeError("unexpected ack status 401")``, which the
	# tick reports as a fresh generic error every minute.
	class _Transport:
		def post(self, path, *, json, headers):
			return types.SimpleNamespace(status=401, headers={}, body={})

	client = PostingFeedClient(_Transport(), correlation_id=_REQUEST_ID)
	with pytest.raises(AuthRefused) as refused:
		client._interpret_ack(types.SimpleNamespace(status=401, headers={}, body={}))
	assert refused.value.status == 401
	assert TOKEN not in str(refused.value)
	with pytest.raises(AuthRefused):
		client._interpret_ack(types.SimpleNamespace(status=403, headers={}, body={}))
	# 409 and 404 keep their per-item meaning.
	from retail_tower_erpnext_connector.connector.posting.transport import AckConflict, AckNotFound

	with pytest.raises(AckConflict):
		client._interpret_ack(types.SimpleNamespace(status=409, headers={}, body={}))
	with pytest.raises(AckNotFound):
		client._interpret_ack(types.SimpleNamespace(status=404, headers={}, body={}))
