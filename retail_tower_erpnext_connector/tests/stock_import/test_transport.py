# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1287 — the ``stock-import.yaml`` pull/report client and the contract DTOs (frappe-free)."""

import pytest

from retail_tower_erpnext_connector.connector.stock_import import transport as t
from retail_tower_erpnext_connector.connector.stock_import.contracts import (
	LedgerKey,
	RecordedStockImport,
	StockImportRequest,
)

_REF = "11111111-1111-4111-8111-111111111111"


def _wire(after=None, **over):
	wire = {
		"requestRef": _REF,
		"runRef": "22222222-2222-4222-8222-222222222222",
		"pageSeq": 0,
		"storeId": "33333333-3333-4333-8333-333333333333",
		"erpnextWarehouseRef": "Stores - RT",
		"after": after,
		"settleSeconds": 300,
		"overlapSeconds": 600,
		"maxRows": 100,
		"itemCursor": "ic-1",
	}
	wire.update(over)
	return wire


class _Resp:
	def __init__(self, status, body=None, headers=None):
		self.status, self.body, self.headers = status, body or {}, headers or {}


class _HttpError(Exception):
	def __init__(self, status, body):
		super().__init__(f"HTTP {status}")
		self.response = type("R", (), {"status_code": status, "json": lambda _self: body})()


class _Transport:
	def __init__(self, get=None, post=None):
		self._get, self._post = get, post
		self.calls = []

	def get(self, path, *, params, headers):
		self.calls.append(("GET", path, params, headers))
		if isinstance(self._get, Exception):
			raise self._get
		return self._get

	def post(self, path, *, json, headers):
		self.calls.append(("POST", path, json, headers))
		return self._post


def _client(**kw):
	transport = _Transport(**kw)
	return t.StockImportClient(transport, correlation_id="rid-1"), transport


# --- DTOs ----------------------------------------------------------------------------------------


def test_request_round_trips():
	after = {"creation": "2026-11-11T10:00:00.000000+02:00", "ledgerEntryRef": "SLE-1", "entryIndex": 2}
	for wire in (
		_wire(),
		_wire(after=after),
		_wire(after={**after, "ledgerEntryRef": None, "entryIndex": None}),
	):
		assert StockImportRequest.from_wire(wire).to_wire() == wire


def test_request_ignores_unknown_keys():
	assert StockImportRequest.from_wire({**_wire(), "future": 1}).max_rows == 100


@pytest.mark.parametrize("over", [{"maxRows": 0}, {"maxRows": 101}, {"settleSeconds": 3601}])
def test_request_out_of_range_is_refused(over):
	with pytest.raises(ValueError):
		StockImportRequest.from_wire(_wire(**over))


def test_ledger_key_null_pairing_is_enforced():
	with pytest.raises(ValueError):
		LedgerKey.from_wire(
			{"creation": "2026-11-11T10:00:00+02:00", "ledgerEntryRef": "SLE-1", "entryIndex": None}
		)
	with pytest.raises(ValueError):
		LedgerKey.from_wire(
			{"creation": "2026-11-11T10:00:00+02:00", "ledgerEntryRef": "SLE-1", "entryIndex": True}
		)


def test_recorded_is_tolerant():
	assert RecordedStockImport.from_wire(None) == RecordedStockImport(None, None, None, None)
	rec = RecordedStockImport.from_wire(
		{"requestRef": _REF, "recordedRowCount": 3, "exhausted": True, "refused": False}
	)
	assert (rec.recorded_row_count, rec.exhausted, rec.refused) == (3, True, False)


# --- pull ----------------------------------------------------------------------------------------


def test_pull_sends_limit_since_and_correlation_and_parses_the_page():
	client, transport = _client(get={"items": [_wire()], "cursor": "c-2", "next_page_token": None})
	page = client.pull_requests(since="c-1", limit=900)
	_, path, params, headers = transport.calls[0]
	assert path == "/api/connector/v1/erpnext/stock-import-requests"
	assert params == {"limit": 500, "since": "c-1"}
	assert headers == {"X-Request-Id": "rid-1"}
	assert page.cursor == "c-2" and page.next_page_token is None
	assert page.items[0].request_ref == _REF


def test_pull_without_since_omits_it():
	client, transport = _client(get={"items": [], "cursor": "c-0", "next_page_token": None})
	client.pull_requests(since=None)
	assert transport.calls[0][2] == {"limit": 100}


def test_pull_keeps_valid_items_when_one_does_not_parse():
	bad = {**_wire(), "requestRef": "bad-ref", "maxRows": 0}
	client, _ = _client(get={"items": [bad, _wire()], "cursor": "c-2", "next_page_token": None})
	page = client.pull_requests(since=None)
	assert [i.request_ref for i in page.items] == [_REF]
	[(ref, detail)] = page.invalid_items
	assert ref == "bad-ref" and detail.startswith("ValueError")


def test_pull_missing_cursor_raises():
	client, _ = _client(get={"items": [], "cursor": "", "next_page_token": None})
	with pytest.raises(ValueError):
		client.pull_requests(since=None)


def test_pull_snapshot_required_is_recognised():
	client, _ = _client(get=_HttpError(409, {"error": {"code": "snapshot_required", "message": "x"}}))
	with pytest.raises(t.SnapshotRequired):
		client.pull_requests(since="old")


def test_pull_other_http_errors_propagate():
	client, _ = _client(get=_HttpError(500, {"error": {"code": "system_failure"}}))
	with pytest.raises(_HttpError):
		client.pull_requests(since=None)


# --- report --------------------------------------------------------------------------------------


def test_report_posts_body_with_the_contract_key():
	body = {"rows": [], "readThrough": None, "exhausted": True, "readAt": "x", "refusal": None}
	client, transport = _client(post=_Resp(201, {"requestRef": _REF, "recordedRowCount": 0}))
	result = client.report_rows(_REF, body)
	_, path, sent, headers = transport.calls[0]
	assert path == f"/api/connector/v1/erpnext/stock-import-requests/{_REF}/report"
	assert sent is body
	assert headers == {"X-Request-Id": "rid-1", "Idempotency-Key": f"stockimport-{_REF}"}
	assert len(headers["Idempotency-Key"]) == 48
	assert result.replayed is False and result.recorded.recorded_row_count == 0


def test_report_replay_is_success():
	client, _ = _client(post=_Resp(200, {}, {"Idempotent-Replayed": "true"}))
	assert client.report_rows(_REF, {}).replayed is True


@pytest.mark.parametrize(
	("status", "body", "exc", "code"),
	[
		(409, {"error": {"code": "import_report_conflict"}}, t.ReportConflict, "import_report_conflict"),
		(409, {}, t.ReportConflict, "idempotency_key_conflict"),
		(404, {"error": {"code": "not_found"}}, t.ReportNotFound, None),
		(400, {"error": {"code": "validation_error"}}, t.ReportRejected, None),
		(413, {"error": {"code": "payload_too_large"}}, t.ReportRejected, None),
		(500, {}, RuntimeError, None),
	],
)
def test_report_statuses(status, body, exc, code):
	client, _ = _client(post=_Resp(status, body))
	with pytest.raises(exc) as info:
		client.report_rows(_REF, {})
	if code is not None:
		assert info.value.code == code


def test_report_plain_dict_transport_is_success():
	client, _ = _client(post={"recordedRowCount": 2})
	assert client.report_rows(_REF, {}).recorded.recorded_row_count == 2
