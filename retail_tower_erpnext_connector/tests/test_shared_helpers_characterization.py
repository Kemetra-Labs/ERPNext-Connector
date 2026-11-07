# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt
"""RT-1218 — characterization of the duplicated transport / text helpers, one per helper per call site.

Written against the code BEFORE the helpers were shared (``connector/http_transport.py``,
``connector/text.py``) and kept unchanged across that refactor: the same input must give the same
output at every call site.

* message scrubbing (``_safe``) — posting poller, bin-view poller, heartbeat sender (scrubbed, no
  limit) and the compatibility glue (scrubbed, then cut at 500 characters);
* the auth-backed HTTP transport — the request URL, params / JSON, headers, timeout and auth that the
  posting poller, the bin-view poller and the heartbeat sender send, how the response is read, and
  the refusal when ``dp2_base_url`` / ``dp2_token`` is unset;
* the error code read from a refusal body (``_error_code``) — bin-view (envelope, then a flat
  ``code``, no limit) and heartbeat (envelope only, cut at 64 characters).

The frappe-coupled modules load against a minimal stand-in, with ``requests`` faked: no bench, no
network.
"""

import importlib
import sys
import types

import pytest

_ROOT = "retail_tower_erpnext_connector"
_C = f"{_ROOT}.connector"
_POSTING_POLLER = f"{_C}.posting.poller"
_COMPAT_GLUE = f"{_C}.posting.compatibility_glue"
_BIN_VIEW_POLLER = f"{_C}.bin_view.poller"
_SENDER = f"{_C}.health.sender"
_MISSING = object()

# A credential-bearing, over-long failure message (both a key=value secret and a Bearer token).
_MESSAGE = "dp2_token=abc123 then Bearer xyz.789 done " + "x" * 600
_SCRUBBED = "[redacted] then [redacted] done " + "x" * 600  # 632 characters, no limit
_MISSING_CONFIG = "Connector Settings: dp2_base_url and dp2_token are required"


# --- stand-ins ----------------------------------------------------------------------------------


class _Logger:
	def __init__(self):
		self.records = []

	def error(self, record):
		self.records.append(("error", record))

	def warning(self, record):
		self.records.append(("warning", record))

	def info(self, record):
		self.records.append(("info", record))


class _Cache:
	def get_value(self, _key):
		return None

	def set_value(self, _key, _value):
		pass


class _Settings:
	def __init__(self, base_url=" https://dp2.example/api/ ", token="tok-1"):
		self.dp2_base_url = base_url
		self._token = token

	def get_password(self, field):
		assert field == "dp2_token"
		return self._token


class _Db:
	def __init__(self):
		self.write_error = None

	def set_single_value(self, *_a, **_k):
		if self.write_error is not None:
			raise self.write_error

	def sql(self, *_a, **_k):
		return [[1]]


def _fake_frappe():
	fake = types.ModuleType("frappe")
	fake.exceptions = types.SimpleNamespace()
	fake.log = _Logger()
	fake.logger = lambda *_a, **_k: fake.log
	fake.cache_obj = _Cache()
	fake.cache = lambda: fake.cache_obj
	fake.db = _Db()
	fake.settings = _Settings()
	fake.get_doc_error = None

	def _get_doc(*_a, **_k):
		if fake.get_doc_error is not None:
			raise fake.get_doc_error
		return fake.settings

	fake.get_doc = _get_doc
	fake.utils = types.SimpleNamespace(now_datetime=lambda: "2026-11-06 10:00:00")
	return fake


class _Resp:
	def __init__(self, status_code=201, body=None, headers=None, json_error=False):
		self.status_code = status_code
		self.headers = headers or {"Idempotent-Replayed": "true"}
		self._body = body if body is not None else {"ok": True}
		self._json_error = json_error
		self.raised = False

	def json(self):
		if self._json_error:
			raise ValueError("not json")
		return self._body

	def raise_for_status(self):
		self.raised = True


class _Requests(types.ModuleType):
	"""A ``requests`` stand-in recording every call (``requests`` is not installed in CI)."""

	def __init__(self):
		super().__init__("requests")
		self.calls = []
		self.next_get = _Resp(200, {"items": []})
		self.next_post = _Resp()

	def get(self, url, **kwargs):
		self.calls.append(("GET", url, kwargs))
		return self.next_get

	def post(self, url, **kwargs):
		self.calls.append(("POST", url, kwargs))
		return self.next_post


@pytest.fixture
def stub():
	"""Install the frappe + requests stand-ins; drop every connector module imported under them."""
	before = set(sys.modules)
	saved = {name: sys.modules.get(name, _MISSING) for name in ("frappe", "requests")}
	fake, requests = _fake_frappe(), _Requests()
	sys.modules["frappe"] = fake
	sys.modules["requests"] = requests
	# Modules already imported frappe-free (e.g. by other tests) must re-import against the stand-in.
	reloaded = [n for n in (_POSTING_POLLER, _COMPAT_GLUE, _BIN_VIEW_POLLER, _SENDER) if n in sys.modules]
	saved_mods = {n: sys.modules.pop(n) for n in reloaded}
	yield types.SimpleNamespace(frappe=fake, requests=requests)
	for name in set(sys.modules) - before:
		if name.startswith(_ROOT):
			sys.modules.pop(name, None)
			parent, _, child = name.rpartition(".")
			if parent in sys.modules:
				vars(sys.modules[parent]).pop(child, None)
	for name, module in saved_mods.items():
		sys.modules[name] = module
	for name, value in saved.items():
		if value is _MISSING:
			sys.modules.pop(name, None)
		else:
			sys.modules[name] = value


def _records(fake, event):
	return [
		(level, rec) for level, rec in fake.log.records if isinstance(rec, dict) and rec.get("event") == event
	]


# --- _safe: one per call site ---------------------------------------------------------------------


def test_posting_poller_scrubs_the_skip_reason_without_a_limit(stub):
	poller = importlib.import_module(_POSTING_POLLER)
	stub.frappe.get_doc_error = RuntimeError(_MESSAGE)

	assert poller._ready_posting_path("rid-1") is None
	assert _records(stub.frappe, "posting.poll.skipped") == [
		("warning", {"event": "posting.poll.skipped", "reason": _SCRUBBED})
	]


def test_bin_view_poller_scrubs_the_skip_reason_without_a_limit(stub):
	poller = importlib.import_module(_BIN_VIEW_POLLER)
	stub.frappe.get_doc_error = RuntimeError(_MESSAGE)

	poller.run_bin_view_poll()
	assert _records(stub.frappe, "bin_view.poll.skipped") == [
		("warning", {"event": "bin_view.poll.skipped", "reason": _SCRUBBED})
	]


def test_heartbeat_sender_scrubs_the_skip_reason_without_a_limit(stub):
	sender = importlib.import_module(_SENDER)
	stub.frappe.get_doc_error = RuntimeError(_MESSAGE)

	sender.run_heartbeat()
	((level, record),) = _records(stub.frappe, "connector.heartbeat.skipped")
	assert level == "warning"
	assert record["reason"] == _SCRUBBED
	assert stub.requests.calls == []


def test_compatibility_glue_scrubs_then_cuts_the_detail_at_500(stub):
	glue = importlib.import_module(_COMPAT_GLUE)
	stub.frappe.db.write_error = RuntimeError(_MESSAGE)

	glue._write({"compatibility_status": "OK"})
	((level, record),) = _records(stub.frappe, "compatibility.record_failed")
	assert level == "error"
	assert record["detail"] == ("RuntimeError: " + _SCRUBBED)[:500]
	assert len(record["detail"]) == 500


# --- the auth-backed HTTP transport: one per call site ---------------------------------------------


def _posting_transport(stub):
	poller = importlib.import_module(_POSTING_POLLER)
	poller._warn_credential_lifecycle = lambda _settings: None
	poller._load_uom_map = lambda _s: {"each": "Nos"}
	poller._load_warehouse_map = lambda _s: {"store": {"warehouse": "Stores - RT"}}
	poller._load_store_customer_map = lambda _s: {"store": "Walk-in"}
	poller._load_tender_mode_map = lambda _s: {}
	client, _post_valid, _preflight = poller._build_posting_path("rid-1")
	return client._transport


def _bin_view_transport(stub):
	poller = importlib.import_module(_BIN_VIEW_POLLER)
	client, _reader, _clock = poller._build_bin_view_path("rid-1")
	return client._transport


def _heartbeat_transport(stub):
	sender = importlib.import_module(_SENDER)
	return sender._build_client("rid-1")._transport


_SITES = {
	"posting_poller": (_posting_transport, _POSTING_POLLER, "_ready_posting_path"),
	"bin_view_poller": (_bin_view_transport, _BIN_VIEW_POLLER, None),
	"heartbeat_sender": (_heartbeat_transport, _SENDER, None),
}


@pytest.mark.parametrize("site", sorted(_SITES))
def test_transport_get_sends_the_same_request(stub, site):
	transport = _SITES[site][0](stub)

	body = transport.get("/connector/feed", params={"limit": 50}, headers={"X-Request-Id": "rid-1"})
	assert body == {"items": []}
	assert stub.requests.next_get.raised is True  # GET raises for an HTTP error status
	assert stub.requests.calls == [
		(
			"GET",
			"https://dp2.example/api/connector/feed",
			{
				"params": {"limit": 50},
				"headers": {"Authorization": "Bearer tok-1", "X-Request-Id": "rid-1"},
				"timeout": 30,
			},
		)
	]
	assert list(stub.requests.calls[0][2]["headers"]) == ["Authorization", "X-Request-Id"]


@pytest.mark.parametrize("site", sorted(_SITES))
def test_transport_post_sends_the_same_request_and_reads_the_status(stub, site):
	transport = _SITES[site][0](stub)
	stub.requests.next_post = _Resp(409, {"error": {"code": "conflict"}}, {"Idempotent-Replayed": "false"})

	resp = transport.post("/connector/ack", json={"a": 1}, headers={"Idempotency-Key": "k-1"})
	assert (resp.status, resp.headers, resp.body) == (
		409,
		{"Idempotent-Replayed": "false"},
		{"error": {"code": "conflict"}},
	)
	assert stub.requests.calls == [
		(
			"POST",
			"https://dp2.example/api/connector/ack",
			{
				"json": {"a": 1},
				"headers": {"Authorization": "Bearer tok-1", "Idempotency-Key": "k-1"},
				"timeout": 30,
			},
		)
	]


@pytest.mark.parametrize("site", sorted(_SITES))
def test_transport_post_reads_a_non_json_body_as_empty(stub, site):
	transport = _SITES[site][0](stub)
	stub.requests.next_post = _Resp(502, json_error=True, headers={"X": "y"})

	resp = transport.post("/p", json={}, headers={})
	assert (resp.status, resp.headers, resp.body) == (502, {"X": "y"}, {})


@pytest.mark.parametrize("site", sorted(_SITES))
def test_caller_headers_win_over_the_auth_header(stub, site):
	transport = _SITES[site][0](stub)

	transport.post("/p", json={}, headers={"Authorization": "Bearer other"})
	assert stub.requests.calls[0][2]["headers"] == {"Authorization": "Bearer other"}


@pytest.mark.parametrize(
	("base_url", "token"),
	[("", "tok-1"), ("   ", "tok-1"), ("https://dp2.example", ""), ("https://dp2.example", None)],
)
@pytest.mark.parametrize("site", sorted(_SITES))
def test_transport_refuses_a_missing_base_url_or_token(stub, site, base_url, token):
	stub.frappe.settings = _Settings(base_url=base_url, token=token)

	with pytest.raises(ValueError) as raised:
		_SITES[site][0](stub)
	assert str(raised.value) == _MISSING_CONFIG
	assert stub.requests.calls == []


# --- _error_code: one per call site (frappe-free) ------------------------------------------------

_LONG_CODE = "c" * 100


def _status(status, body):
	return types.SimpleNamespace(status=status, headers={}, body=body)


@pytest.mark.parametrize(
	("status", "body", "kind", "code"),
	[
		(409, {"error": {"code": "idempotency_key_conflict"}}, "ReportConflict", "idempotency_key_conflict"),
		(
			409,
			{"error": {"code": "window_sequence_conflict"}},
			"WindowSequenceConflict",
			"window_sequence_conflict",
		),
		(
			409,
			{"code": "window_sequence_conflict"},
			"WindowSequenceConflict",
			"window_sequence_conflict",
		),  # flat
		(409, {"error": {"code": ""}, "code": "flat_wins"}, "ReportConflict", "flat_wins"),
		(409, {"error": "not-a-dict", "code": "flat_wins"}, "ReportConflict", "flat_wins"),
		(409, {"error": {"code": _LONG_CODE}}, "ReportConflict", _LONG_CODE),  # no limit
		(409, {"error": {"code": 7}}, "ReportConflict", "7"),
		(409, {}, "ReportConflict", "idempotency_key_conflict"),
		(404, {"error": {"code": "run_not_found"}}, "ReportNotFound", "run_not_found"),
		(404, {"code": "flat_404"}, "ReportNotFound", "flat_404"),
		(404, {"message": "no code"}, "ReportNotFound", "not_found"),
		(404, None, "ReportNotFound", "not_found"),
		(404, ["not", "a", "dict"], "ReportNotFound", "not_found"),
	],
)
def test_bin_view_error_code(status, body, kind, code):
	from retail_tower_erpnext_connector.connector.bin_view import transport

	with pytest.raises(getattr(transport, kind)) as raised:
		transport.BinViewClient._interpret_report(_status(status, body), window_seq=3)
	assert type(raised.value).__name__ == kind
	assert str(raised.value) == code


class _HeartbeatTransport:
	def __init__(self, resp):
		self._resp = resp

	def post(self, path, *, json, headers):
		return self._resp


@pytest.mark.parametrize(
	("status", "body", "kind", "code"),
	[
		(401, {"error": {"code": "credential_revoked"}}, "HeartbeatUnauthorized", "credential_revoked"),
		(401, {"code": "flat_ignored"}, "HeartbeatUnauthorized", "unauthorized"),  # no flat fallback
		(400, {"error": {"code": ""}, "code": "flat_ignored"}, "HeartbeatRejected", "validation_error"),
		(400, {"error": {"code": _LONG_CODE}}, "HeartbeatRejected", "c" * 64),  # cut at 64
		(503, {"error": {"code": 12345}}, "HeartbeatError", "12345"),
		(503, {"error": "not-a-dict"}, "HeartbeatError", "unexpected_status"),
		(503, None, "HeartbeatError", "unexpected_status"),
		(503, ["x"], "HeartbeatError", "unexpected_status"),
	],
)
def test_heartbeat_error_code(status, body, kind, code):
	from retail_tower_erpnext_connector.connector.health import heartbeat

	client = heartbeat.HeartbeatClient(_HeartbeatTransport(_status(status, body)), correlation_id="rid-1")
	with pytest.raises(heartbeat.HeartbeatError) as raised:
		client.report(heartbeat.HeartbeatReport())
	assert type(raised.value).__name__ == kind
	assert (raised.value.status, raised.value.code) == (status, code)
	assert str(raised.value) == f"heartbeat refused: status {status}, code {code}"
