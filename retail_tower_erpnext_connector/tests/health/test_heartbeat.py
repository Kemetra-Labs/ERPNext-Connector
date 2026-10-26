# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-564 — the heartbeat body and client against spec 020 ``connectorReportHeartbeat`` (no frappe).

The body carries only the four ``HeartbeatReport`` keys (``additionalProperties: false``), never an
identity field, and only within the contract's bounds. A 200 returns the server-clock ack; 400 / 401
/ any other status raise a typed error that carries the status and error code only.
"""

import types
from datetime import datetime, timedelta, timezone

import pytest

from retail_tower_erpnext_connector.connector.health import heartbeat as hb

_CONTRACT_KEYS = {"connectorVersion", "backlogIndicator", "erpnextReachable", "sourceClockAt"}


class _Transport:
	def __init__(self, resp):
		self.resp = resp
		self.calls = []

	def post(self, path, *, json, headers):
		self.calls.append((path, json, headers))
		return self.resp


def _resp(status, body=None):
	return types.SimpleNamespace(status=status, headers={}, body=body if body is not None else {})


class TestReportBody:
	def test_an_empty_report_is_an_empty_body(self):
		assert hb.HeartbeatReport().to_wire() == {}

	def test_a_full_report_uses_only_the_contract_keys(self):
		body = hb.HeartbeatReport(
			connector_version="0.3.0",
			backlog_indicator=0,
			erpnext_reachable=True,
			source_clock_at="2026-10-26T10:00:00.000Z",
		).to_wire()
		assert body == {
			"connectorVersion": "0.3.0",
			"backlogIndicator": 0,
			"erpnextReachable": True,
			"sourceClockAt": "2026-10-26T10:00:00.000Z",
		}
		assert set(body) <= _CONTRACT_KEYS

	def test_no_identity_field_is_ever_sent(self):
		body = hb.HeartbeatReport(connector_version="1", erpnext_reachable=False).to_wire()
		for forbidden in ("tenant_id", "registration_id", "last_seen_at", "tenantId", "registrationId"):
			assert forbidden not in body

	def test_unreachable_is_sent_as_false_not_dropped(self):
		assert hb.HeartbeatReport(erpnext_reachable=False).to_wire() == {"erpnextReachable": False}

	@pytest.mark.parametrize("version", ["", "   ", "x" * 65])
	def test_a_version_outside_1_to_64_chars_is_refused(self, version):
		with pytest.raises(ValueError):
			hb.HeartbeatReport(connector_version=version).to_wire()

	def test_a_64_char_version_is_accepted(self):
		assert hb.HeartbeatReport(connector_version="v" * 64).to_wire() == {"connectorVersion": "v" * 64}

	@pytest.mark.parametrize("backlog", [-1, 2147483648, True, 1.5, "3"])
	def test_a_backlog_outside_int4_is_refused(self, backlog):
		with pytest.raises(ValueError):
			hb.HeartbeatReport(backlog_indicator=backlog).to_wire()

	def test_the_int4_bounds_are_accepted(self):
		assert hb.HeartbeatReport(backlog_indicator=2147483647).to_wire() == {"backlogIndicator": 2147483647}

	def test_a_non_boolean_reachability_is_refused(self):
		with pytest.raises(ValueError):
			hb.HeartbeatReport(erpnext_reachable="yes").to_wire()


class TestSourceClock:
	def test_utc_with_milliseconds_and_a_z_suffix(self):
		moment = datetime(2026, 10, 26, 9, 5, 7, 123456, tzinfo=timezone.utc)
		assert hb.format_source_clock(moment) == "2026-10-26T09:05:07.123Z"

	def test_another_offset_is_converted_to_utc(self):
		moment = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone(timedelta(hours=3)))
		assert hb.format_source_clock(moment) == "2026-10-26T09:00:00.000Z"

	def test_a_naive_datetime_is_refused(self):
		with pytest.raises(ValueError):
			hb.format_source_clock(datetime(2026, 10, 26, 9, 0, 0))


class TestClient:
	def test_posts_to_the_contract_path_with_the_request_id(self):
		transport = _Transport(_resp(200, {"acknowledgedAt": "2026-10-26T09:00:00.000Z"}))
		client = hb.HeartbeatClient(transport, correlation_id="rid-1")
		ack = client.report(hb.HeartbeatReport(connector_version="0.3.0"))
		assert transport.calls == [
			(
				"/api/connector/v1/erpnext/health/heartbeat",
				{"connectorVersion": "0.3.0"},
				{"X-Request-Id": "rid-1"},
			)
		]
		assert ack == hb.HeartbeatAck(acknowledged_at="2026-10-26T09:00:00.000Z")

	def test_the_client_sets_no_auth_header_itself(self):
		# Authorization comes from the shared auth-backed transport, never from this client.
		transport = _Transport(_resp(200, {"acknowledgedAt": "t"}))
		hb.HeartbeatClient(transport, correlation_id="rid").report(hb.HeartbeatReport())
		assert set(transport.calls[0][2]) == {"X-Request-Id"}

	def test_a_plain_dict_response_is_a_200(self):
		transport = _Transport({"acknowledgedAt": "t"})
		ack = hb.HeartbeatClient(transport, correlation_id="rid").report(hb.HeartbeatReport())
		assert ack.acknowledged_at == "t"

	def test_a_200_without_an_ack_time_still_succeeds(self):
		transport = _Transport(_resp(200, {}))
		ack = hb.HeartbeatClient(transport, correlation_id="rid").report(hb.HeartbeatReport())
		assert ack.acknowledged_at is None

	def test_401_raises_unauthorized_with_the_code(self):
		transport = _Transport(_resp(401, {"error": {"code": "unauthorized", "message": "Unauthorized"}}))
		with pytest.raises(hb.HeartbeatUnauthorized) as caught:
			hb.HeartbeatClient(transport, correlation_id="rid").report(hb.HeartbeatReport())
		assert (caught.value.status, caught.value.code) == (401, "unauthorized")

	def test_400_raises_rejected(self):
		transport = _Transport(_resp(400, {"error": {"code": "validation_error", "message": "bad"}}))
		with pytest.raises(hb.HeartbeatRejected) as caught:
			hb.HeartbeatClient(transport, correlation_id="rid").report(hb.HeartbeatReport())
		assert caught.value.code == "validation_error"

	@pytest.mark.parametrize("status", [201, 404, 429, 500, 503])
	def test_any_other_status_raises_a_heartbeat_error(self, status):
		transport = _Transport(_resp(status, {"error": {"code": "system_failure"}}))
		with pytest.raises(hb.HeartbeatError) as caught:
			hb.HeartbeatClient(transport, correlation_id="rid").report(hb.HeartbeatReport())
		assert type(caught.value) is hb.HeartbeatError
		assert caught.value.status == status

	def test_the_error_keeps_only_the_code_not_the_body(self):
		body = {"error": {"code": "system_failure", "message": "detail Bearer abc.def"}, "extra": "x"}
		transport = _Transport(_resp(500, body))
		with pytest.raises(hb.HeartbeatError) as caught:
			hb.HeartbeatClient(transport, correlation_id="rid").report(hb.HeartbeatReport())
		assert "abc.def" not in str(caught.value)
		assert "detail" not in str(caught.value)

	def test_a_non_envelope_error_body_falls_back_to_a_default_code(self):
		transport = _Transport(_resp(502, "<html>bad gateway</html>"))
		with pytest.raises(hb.HeartbeatError) as caught:
			hb.HeartbeatClient(transport, correlation_id="rid").report(hb.HeartbeatReport())
		assert caught.value.code == "unexpected_status"

	def test_an_invalid_report_is_refused_before_any_call(self):
		transport = _Transport(_resp(200, {}))
		with pytest.raises(ValueError):
			hb.HeartbeatClient(transport, correlation_id="rid").report(
				hb.HeartbeatReport(backlog_indicator=-1)
			)
		assert transport.calls == []
