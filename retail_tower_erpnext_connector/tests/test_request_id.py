# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-39 — the Connector's outbound ``X-Request-Id`` is a UUID Backend-Core keeps and echoes.

Backend-Core's ``RequestIdInterceptor`` (``apps/api/src/common/request-id.interceptor.ts``) honours
an inbound ``X-Request-Id`` only when it matches ``UUID_RE`` below (documented there as UUID v4 or
v7); anything else is replaced by a freshly minted id, so a ``frappe.generate_hash(length=16)``
value left the two sides logging different ids.

Cases:
  - :func:`new_request_id` yields a lower-case, hyphenated UUIDv4 matching Backend-Core's regex,
    fresh on every call;
  - a full posting tick (``run_posting_poll``) sends ONE UUID ``X-Request-Id`` on every call of the
    tick (pull + ack) and hands the SAME value + client to the glue (its acks and request_id logs);
    the ack ``Idempotency-Key`` is still the work-item-derived key, NOT the request id;
  - a full bin-view tick (``run_bin_view_poll``) does the same for pull + report, with the report
    ``Idempotency-Key`` still ``binview-<requestRef>``;
  - each tick mints its own id.

The pollers import frappe, so they are loaded against a minimal stand-in (the RT-71 / RT-78
pattern); every frappe/HTTP seam is faked — no bench, no live Backend-Core.
"""

import importlib
import re
import sys
import types
import uuid

import pytest

from retail_tower_erpnext_connector.connector.request_id import new_request_id

# Mirrors Backend-Core apps/api/src/common/request-id.interceptor.ts ``UUID_RE`` verbatim.
BACKEND_CORE_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

_ROOT = "retail_tower_erpnext_connector"
_POSTING_POLLER = f"{_ROOT}.connector.posting.poller"
_POSTING_GLUE = f"{_ROOT}.connector.posting.frappe_glue"
_INDEX_GUARD = f"{_ROOT}.connector.posting.index_guard"
_BIN_VIEW_POLLER = f"{_ROOT}.connector.bin_view.poller"
_BIN_VIEW_GLUE = f"{_ROOT}.connector.bin_view.frappe_glue"
_MISSING = object()

_STORE = "33333333-3333-4333-8333-333333333333"
_BAD_REF = "99999999-9999-4999-8999-999999999999"
_REQUEST_REF = "11111111-1111-4111-8111-111111111111"


def _assert_backend_core_accepts(value: str) -> None:
	assert BACKEND_CORE_UUID_RE.match(value), value
	parsed = uuid.UUID(value)
	assert parsed.version == 4
	assert str(parsed) == value  # canonical lower-case hyphenated form — echoed back byte-for-byte


# --- the helper ---------------------------------------------------------------------------------


def test_new_request_id_is_a_backend_core_compatible_uuid4():
	_assert_backend_core_accepts(new_request_id())


def test_new_request_id_is_fresh_per_call():
	assert len({new_request_id() for _ in range(50)}) == 50


def test_former_generate_hash_shape_is_rejected_by_backend_core():
	# The pre-RT-39 value (a 16-char hex hash) is NOT kept by Backend-Core — the gap this closes.
	assert not BACKEND_CORE_UUID_RE.match("a1b2c3d4e5f6a7b8")


# --- a minimal frappe stand-in + loader -----------------------------------------------------------


class _Logger:
	def __init__(self):
		self.records = []

	def info(self, record):
		self.records.append(record)

	warning = error = info


class _Cache:
	def __init__(self):
		self.values = {}

	def get_value(self, key):
		return self.values.get(key)

	def set_value(self, key, value):
		self.values[key] = value


def _fake_frappe():
	fake = types.ModuleType("frappe")
	fake.exceptions = types.SimpleNamespace()
	logger, cache = _Logger(), _Cache()
	fake.logger = lambda *_a, **_k: logger
	fake.cache = lambda: cache
	fake.get_doc = lambda *_a, **_k: types.SimpleNamespace()
	return fake


class _RecordingTransport:
	"""Records every outbound call's headers; replies with one canned page then success."""

	def __init__(self, page: dict):
		self._page = page
		self.calls: list[tuple[str, str, dict]] = []

	def get(self, path, *, params, headers):
		self.calls.append(("GET", path, dict(headers)))
		return self._page

	def post(self, path, *, json, headers):
		self.calls.append(("POST", path, dict(headers)))
		return {}  # a plain body — the clients read it as a 201 success


@pytest.fixture
def frappe_stub(monkeypatch):
	"""Install the stand-in as ``frappe`` and drop every connector module imported under it after."""
	before = set(sys.modules)
	saved_frappe = sys.modules.get("frappe", _MISSING)
	fake = _fake_frappe()
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


def _request_ids(transport: _RecordingTransport) -> set[str]:
	return {headers["X-Request-Id"] for _method, _path, headers in transport.calls}


# --- posting poller -----------------------------------------------------------------------------


def _wire_work_item() -> dict:
	line = {
		"lineName": "Item A",
		"unitPrice": "100.00",
		"currencyCode": "EGP",
		"quantity": "1",
		"lineAmount": "100.00",
		"unit": "each",
		"erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"},
	}
	return {
		"workItemRef": "11111111-1111-4111-8111-111111111111",
		"kind": "sale_post",
		"sourceSystem": "pos-pulse",
		"externalId": "POS-9001",
		"payloadHash": "a" * 64,
		"businessDate": "2026-06-01",
		"itemCursor": "c-1",
		"sale": {
			"saleRef": "22222222-2222-4222-8222-222222222222",
			"storeId": _STORE,
			"currencyCode": "EGP",
			"posTotal": "100.00",
			"occurredAt": "2026-06-01T10:00:00Z",
			"businessDate": "2026-06-01",
			"sourceSystem": "pos-pulse",
			"externalId": "POS-9001",
			"lines": [line],
		},
	}


def _posting_page() -> dict:
	# A valid item (→ post_valid → glue) and a malformed one (→ the worker's real ack path).
	return {
		"items": [_wire_work_item(), {"workItemRef": _BAD_REF, "kind": "bogus"}],
		"cursor": "c-1",
		"next_page_token": None,
	}


def _posting_tick(monkeypatch, transport=None):
	"""Run one real ``run_posting_poll`` tick over faked frappe/HTTP seams; return what it sent."""
	poller = importlib.import_module(_POSTING_POLLER)
	glue = importlib.import_module(_POSTING_GLUE)
	guard = importlib.import_module(_INDEX_GUARD)

	transport = transport or _RecordingTransport(_posting_page())
	monkeypatch.setattr(guard, "evaluate", lambda _probe: guard.GuardResult(ok=True, missing=()))
	# RT-379: a compatible site (the profile itself is covered by test_compatibility_*).
	profile = importlib.import_module("retail_tower_erpnext_connector.connector.posting.compatibility_glue")
	monkeypatch.setattr(profile, "run_preflight", lambda _settings, _maps: True)
	monkeypatch.setattr(poller, "_build_http_transport", lambda _settings: transport)
	monkeypatch.setattr(poller, "_warn_credential_lifecycle", lambda _settings: None)
	monkeypatch.setattr(poller, "_load_uom_map", lambda _s: {"each": "Nos"})
	monkeypatch.setattr(poller, "_load_warehouse_map", lambda _s: {_STORE: {"warehouse": "Stores - RT"}})
	monkeypatch.setattr(poller, "_load_store_customer_map", lambda _s: {_STORE: "Walk-in"})
	monkeypatch.setattr(poller, "_load_tender_mode_map", lambda _s: {})

	glue_calls = []

	def _record_post(work_item, *, client, correlation_id, **_deps):
		glue_calls.append((work_item.work_item_ref, correlation_id, client))

	monkeypatch.setattr(glue, "post_work_item", _record_post)
	poller.run_posting_poll()
	return transport, glue_calls


def test_posting_tick_sends_one_uuid_request_id_on_every_call(frappe_stub, monkeypatch):
	transport, glue_calls = _posting_tick(monkeypatch)

	assert [m for m, _p, _h in transport.calls] == ["GET", "POST"]  # pull, the malformed item's ack
	ids = _request_ids(transport)
	assert len(ids) == 1  # stable for the whole tick
	(request_id,) = ids
	_assert_backend_core_accepts(request_id)
	# The valid item reaches the glue with the SAME id (its acks + request_id logs) and the SAME
	# client, so every glue-side call of the tick carries this X-Request-Id too.
	((work_item_ref, glue_request_id, glue_client),) = glue_calls
	assert work_item_ref == "11111111-1111-4111-8111-111111111111"
	assert glue_request_id == request_id
	assert glue_client._headers()["X-Request-Id"] == request_id


def test_posting_tick_keeps_idempotency_keys_independent_of_the_request_id(frappe_stub, monkeypatch):
	transport, _ = _posting_tick(monkeypatch)

	keys = [h.get("Idempotency-Key") for m, _p, h in transport.calls if m == "POST"]
	assert keys == [f"{_BAD_REF}:permanently_rejected"]  # work-item-derived, unchanged by RT-39


def test_each_posting_tick_mints_its_own_request_id(frappe_stub, monkeypatch):
	first, _ = _posting_tick(monkeypatch)
	second, _ = _posting_tick(monkeypatch)
	assert _request_ids(first) != _request_ids(second)


# --- bin-view poller ----------------------------------------------------------------------------


class _Reader:
	def read_bins(self, *, erpnext_warehouse_ref, item_window):
		from retail_tower_erpnext_connector.connector.bin_view.worker import RawBin

		return [RawBin(item_code="ITEM-A", actual_qty=12.5, stock_uom="Nos")]


class _Clock:
	def now_iso(self):
		return "2026-06-01T10:00:00.000Z"


def _bin_view_page() -> dict:
	return {
		"items": [
			{
				"requestRef": _REQUEST_REF,
				"storeId": _STORE,
				"erpnextWarehouseRef": "ERP-WH-1",
				"runRef": "44444444-4444-4444-8444-444444444444",
				"itemWindow": {"windowSeq": 0, "maxItems": 500, "fromItemRef": None, "toItemRef": None},
				"itemCursor": _REQUEST_REF,
			}
		],
		"cursor": "c-1",
		"next_page_token": None,
	}


def _bin_view_tick(monkeypatch, transport=None):
	"""Run one real ``run_bin_view_poll`` tick over faked frappe/HTTP seams; return what it sent."""
	poller = importlib.import_module(_BIN_VIEW_POLLER)
	posting_poller = importlib.import_module(_POSTING_POLLER)
	glue = importlib.import_module(_BIN_VIEW_GLUE)

	transport = transport or _RecordingTransport(_bin_view_page())
	monkeypatch.setattr(posting_poller, "_build_http_transport", lambda _settings: transport)
	monkeypatch.setattr(glue, "FrappeBinReader", _Reader)
	monkeypatch.setattr(glue, "UtcClock", _Clock)
	poller.run_bin_view_poll()
	return transport


def test_bin_view_tick_sends_one_uuid_request_id_on_every_call(frappe_stub, monkeypatch):
	transport = _bin_view_tick(monkeypatch)

	assert [m for m, _p, _h in transport.calls] == ["GET", "POST"]  # pull, report
	ids = _request_ids(transport)
	assert len(ids) == 1
	_assert_backend_core_accepts(next(iter(ids)))


def test_bin_view_tick_keeps_the_request_ref_idempotency_key(frappe_stub, monkeypatch):
	transport = _bin_view_tick(monkeypatch)

	(_method, _path, headers) = transport.calls[1]
	assert headers["Idempotency-Key"] == f"binview-{_REQUEST_REF}"


def test_each_bin_view_tick_mints_its_own_request_id(frappe_stub, monkeypatch):
	assert _request_ids(_bin_view_tick(monkeypatch)) != _request_ids(_bin_view_tick(monkeypatch))


# --- RT-635 (OBS-6): an unhandled tick error reaches Sentry tagged with the SAME request id ------
# The Connector-only Sentry client is replaced by a recorder (it has its own tests in
# test_observability_sentry.py); these prove the wiring: the tag equals the X-Request-Id the tick
# sent to Backend-Core, the original error still propagates to the scheduler, a healthy tick never
# touches Sentry, and a failing Sentry never stops posting (AC4 / AC5 at unit level).

_SENTRY = f"{_ROOT}.connector.observability.sentry_client"
_DSN = "https://0123456789abcdef0123456789abcdef@o123.ingest.de.sentry.io/4567"


class _FailingPullTransport(_RecordingTransport):
	"""Records the pull's headers, then fails it the way ``requests`` does when DP2 is down."""

	def get(self, path, *, params, headers):
		super().get(path, params=params, headers=headers)
		raise ConnectionError("GET https://dp2.example/v1/feed?since=c-1 failed")


def _record_captures(monkeypatch) -> list:
	sentry = importlib.import_module(_SENTRY)
	captured = []

	def _capture(exc, *, request_id, job_name):
		captured.append((exc, request_id, job_name))
		return True

	monkeypatch.setattr(sentry, "capture_tick_error", _capture)
	return captured


def test_posting_tick_error_is_reported_with_the_sent_request_id(frappe_stub, monkeypatch):
	captured = _record_captures(monkeypatch)
	transport = _FailingPullTransport(_posting_page())

	with pytest.raises(ConnectionError) as raised:
		_posting_tick(monkeypatch, transport)

	((exc, request_id, job_name),) = captured
	assert raised.value is exc  # the scheduler still sees the original error
	assert job_name == "posting_poll"
	assert _request_ids(transport) == {request_id}  # same id as Backend-Core logged


def test_bin_view_tick_error_is_reported_with_the_sent_request_id(frappe_stub, monkeypatch):
	captured = _record_captures(monkeypatch)
	transport = _FailingPullTransport(_bin_view_page())

	with pytest.raises(ConnectionError) as raised:
		_bin_view_tick(monkeypatch, transport)

	((exc, request_id, job_name),) = captured
	assert raised.value is exc
	assert job_name == "bin_view_poll"
	assert _request_ids(transport) == {request_id}



def test_paged_report_job_error_is_reported_and_reraised(frappe_stub, monkeypatch):
	captured = _record_captures(monkeypatch)
	poller = importlib.import_module(_BIN_VIEW_POLLER)

	malformed = {"requestRef": "not-a-request"}  # no storeId: an unhandled KeyError in the job
	with pytest.raises(KeyError, match="storeId") as raised:
		poller.run_paged_report_job(malformed)

	((exc, request_id, job_name),) = captured
	assert raised.value is exc
	assert job_name == "bin_view_paged_report"
	_assert_backend_core_accepts(request_id)

def test_healthy_ticks_never_touch_sentry(frappe_stub, monkeypatch):
	monkeypatch.setenv("RT_CONNECTOR_SENTRY_DSN", _DSN)
	sentry = importlib.import_module(_SENTRY)

	def _never(*_a, **_k):
		raise AssertionError("the SDK was loaded on a healthy tick")

	monkeypatch.setattr(sentry, "_load_sdk", _never)
	_posting_tick(monkeypatch)
	_bin_view_tick(monkeypatch)


def test_posting_continues_while_sentry_is_blocked(frappe_stub, monkeypatch):
	monkeypatch.setenv("RT_CONNECTOR_SENTRY_DSN", _DSN)
	sentry = importlib.import_module(_SENTRY)
	sentry.reset_cache()

	class _BlockedHub:
		def __init__(self, _client):
			pass

		def capture_exception(self, *_a, **_k):
			raise OSError("sentry ingest unreachable")

	sdk = types.SimpleNamespace(Client=lambda **_options: object(), Hub=_BlockedHub)
	monkeypatch.setattr(sentry, "_load_sdk", lambda: sdk)

	failing = _FailingPullTransport(_posting_page())
	with pytest.raises(ConnectionError):  # the tick's own error, never Sentry's
		_posting_tick(monkeypatch, failing)
	_transport, glue_calls = _posting_tick(monkeypatch)  # the next tick still posts
	assert len(glue_calls) == 1
	sentry.reset_cache()
