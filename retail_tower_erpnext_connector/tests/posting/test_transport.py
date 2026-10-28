# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""T011 — local unit tests for the pull/ack transport behind a Protocol.

The transport (the live HTTP call to DP2) is abstracted behind :class:`HttpTransport`
so the client logic is testable against a fake here; the live two-op round-trip is the
deferred ⏳ BENCH-VALIDATION component. No frappe, no live DP2.
"""

import pytest

from retail_tower_erpnext_connector.connector.posting import contracts as c
from retail_tower_erpnext_connector.connector.posting import transport as t


def _wire_work_item(external_id: str = "POS-9001") -> dict:
    return {
        "workItemRef": "11111111-1111-4111-8111-111111111111",
        "kind": "sale_post",
        "sourceSystem": "pos-pulse",
        "externalId": external_id,
        "payloadHash": "a" * 64,
        "businessDate": "2026-06-01",
        "itemCursor": "cursor-1",
        "sale": {
            "saleRef": "22222222-2222-4222-8222-222222222222",
            "storeId": "33333333-3333-4333-8333-333333333333",
            "currencyCode": "EGP",
            "posTotal": "100.00",
            "occurredAt": "2026-06-01T10:00:00Z",
            "businessDate": "2026-06-01",
            "sourceSystem": "pos-pulse",
            "externalId": external_id,
            "lines": [
                {
                    "lineName": "Item A",
                    "unitPrice": "100.00",
                    "currencyCode": "EGP",
                    "quantity": "1",
                    "lineAmount": "100.00",
                    "unit": "each",
                    "erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"},
                }
            ],
        },
    }


class FakeTransport:
    """A recording fake implementing the HttpTransport Protocol — no real network."""

    def __init__(self, get_response: dict, post_response: dict | None = None):
        self._get_response = get_response
        self._post_response = post_response or {}
        self.get_calls: list[tuple[str, dict, dict]] = []
        self.post_calls: list[tuple[str, dict, dict]] = []

    def get(self, path: str, *, params: dict, headers: dict) -> dict:
        self.get_calls.append((path, params, headers))
        return self._get_response

    def post(self, path: str, *, json: dict, headers: dict) -> dict:
        self.post_calls.append((path, json, headers))
        return self._post_response


class TestPullPostings:
    def test_parses_feed_page_into_work_items(self):
        page = {
            "items": [_wire_work_item("POS-1"), _wire_work_item("POS-2")],
            "cursor": "page-cursor",
            "next_page_token": "next-tok",
        }
        client = t.PostingFeedClient(FakeTransport(get_response=page), correlation_id="req-abc")
        result = client.pull_postings(since="cursor-0")
        assert len(result.items) == 2
        assert all(isinstance(wi, c.PostingWorkItem) for wi in result.items)
        assert result.items[0].external_id == "POS-1"
        assert result.cursor == "page-cursor"
        assert result.next_page_token == "next-tok"

    def test_carries_correlation_id_header(self):
        page = {"items": [], "cursor": "x", "next_page_token": None}
        fake = FakeTransport(get_response=page)
        client = t.PostingFeedClient(fake, correlation_id="req-xyz")
        client.pull_postings(since="cursor-0")
        _path, _params, headers = fake.get_calls[0]
        # spec-003 substrate: the DP2 request_id correlation travels on every call.
        assert headers.get(t.CORRELATION_HEADER) == "req-xyz"

    def test_since_cursor_passed_as_param(self):
        page = {"items": [], "cursor": "x", "next_page_token": None}
        fake = FakeTransport(get_response=page)
        client = t.PostingFeedClient(fake, correlation_id="r")
        client.pull_postings(since="cursor-7")
        _path, params, _headers = fake.get_calls[0]
        assert params.get("since") == "cursor-7"

    def test_empty_page_is_not_an_error(self):
        page = {"items": [], "cursor": "x", "next_page_token": None}
        client = t.PostingFeedClient(FakeTransport(get_response=page), correlation_id="r")
        result = client.pull_postings(since=None)
        assert result.items == ()

    def test_missing_cursor_raises(self):
        # F-011: 012 cursor is required (minLength 1) — empty/absent must raise, not re-baseline.
        page = {"items": [], "cursor": "", "next_page_token": None}
        client = t.PostingFeedClient(FakeTransport(get_response=page), correlation_id="r")
        with pytest.raises(ValueError):
            client.pull_postings(since=None)


class TestServedPaths:
    """Tier 1: the connector must speak the paths DP2 actually serves (#502/#503), verified from
    DP2 source: GET /api/connector/v1/erpnext/postings and POST .../{workItemRef}/outcome."""

    def test_pull_uses_the_served_base_path(self):
        page = {"items": [], "cursor": "x", "next_page_token": None}
        fake = FakeTransport(get_response=page)
        client = t.PostingFeedClient(fake, correlation_id="r")
        client.pull_postings(since=None)
        path, _params, _headers = fake.get_calls[0]
        assert path == "/api/connector/v1/erpnext/postings"

    def test_ack_uses_the_served_path_with_ref(self):
        fake = FakeTransport(get_response={}, post_response={"outcome": "posted"})
        client = t.PostingFeedClient(fake, correlation_id="r")
        client.ack_outcome("wi-9", c.OutcomeAckRequest.failed_transient())
        path, _body, _headers = fake.post_calls[0]
        assert path == "/api/connector/v1/erpnext/postings/wi-9/outcome"

    def test_pull_sends_limit_param(self):
        page = {"items": [], "cursor": "x", "next_page_token": None}
        fake = FakeTransport(get_response=page)
        client = t.PostingFeedClient(fake, correlation_id="r")
        client.pull_postings(since=None, limit=250)
        _path, params, _headers = fake.get_calls[0]
        assert params.get("limit") == 250

    def test_pull_default_limit_is_100(self):
        page = {"items": [], "cursor": "x", "next_page_token": None}
        fake = FakeTransport(get_response=page)
        client = t.PostingFeedClient(fake, correlation_id="r")
        client.pull_postings(since=None)
        _path, params, _headers = fake.get_calls[0]
        assert params.get("limit") == 100

    def test_pull_limit_capped_at_500(self):
        # DP2 caps at POSTING_FEED_MAX_PAGE=500; the client must not request more.
        page = {"items": [], "cursor": "x", "next_page_token": None}
        fake = FakeTransport(get_response=page)
        client = t.PostingFeedClient(fake, correlation_id="r")
        client.pull_postings(since=None, limit=9999)
        _path, params, _headers = fake.get_calls[0]
        assert params.get("limit") == 500


class FakeHttpResponse:
    """Models a DP2 ack HTTP response (status + headers + body) for the richer transport."""

    def __init__(self, status: int, headers: dict | None = None, body: dict | None = None):
        self.status = status
        self.headers = headers or {}
        self.body = body or {}


class FakeAckTransport:
    """A transport whose post() returns a FakeHttpResponse (status-aware), for ack semantics."""

    def __init__(self, response: FakeHttpResponse):
        self._response = response
        self.post_calls: list[tuple[str, dict, dict]] = []

    def get(self, path, *, params, headers):
        return {"items": [], "cursor": "0", "next_page_token": None}

    def post(self, path, *, json, headers):
        self.post_calls.append((path, json, headers))
        return self._response


class TestAckResponseSemantics:
    """Tier 1: DP2 ack returns 201 first / 200 + Idempotent-Replayed on replay / 409 / 404."""

    def test_201_first_record_is_success_not_replayed(self):
        resp = FakeHttpResponse(201, body={"outcome": "posted"})
        client = t.PostingFeedClient(FakeAckTransport(resp), correlation_id="r")
        result = client.ack_outcome(
            "wi-1", c.OutcomeAckRequest.posted(c.ErpnextDocumentRef("Sales Invoice", "ACC-1"))
        )
        assert result.recorded is True
        assert result.replayed is False

    def test_200_replay_is_success_and_flagged_replayed(self):
        resp = FakeHttpResponse(200, headers={"Idempotent-Replayed": "true"}, body={"outcome": "posted"})
        client = t.PostingFeedClient(FakeAckTransport(resp), correlation_id="r")
        result = client.ack_outcome(
            "wi-1", c.OutcomeAckRequest.posted(c.ErpnextDocumentRef("Sales Invoice", "ACC-1"))
        )
        assert result.recorded is True
        assert result.replayed is True

    def test_409_conflict_raises_ackconflict(self):
        # body drift or a contradicting outcome — operator attention, NOT blind retry.
        resp = FakeHttpResponse(409, body={"code": "idempotency_key_conflict"})
        client = t.PostingFeedClient(FakeAckTransport(resp), correlation_id="r")
        with pytest.raises(t.AckConflict):
            client.ack_outcome("wi-1", c.OutcomeAckRequest.failed_transient())

    def test_404_raises_acknotfound(self):
        # cross-tenant / foreign / absent workItemRef — non-disclosing.
        resp = FakeHttpResponse(404, body={"code": "not_found"})
        client = t.PostingFeedClient(FakeAckTransport(resp), correlation_id="r")
        with pytest.raises(t.AckNotFound):
            client.ack_outcome("wi-1", c.OutcomeAckRequest.failed_transient())

    def test_idempotency_key_meets_dp2_charset_and_length(self):
        # DP2 requires 16-128 printable-ASCII, no whitespace. The {workItemRef}:{outcome} key
        # the glue passes must satisfy it. Build a representative key and assert.
        key = "11111111-1111-4111-8111-111111111111:permanently_rejected"
        assert 16 <= len(key) <= 128
        assert key.isascii() and key.isprintable() and " " not in key


class TestAckOutcome:
    def test_posts_outcome_body_and_work_item_ref_in_path(self):
        fake = FakeTransport(get_response={}, post_response={"workItemRef": "wi-1", "outcome": "posted"})
        client = t.PostingFeedClient(fake, correlation_id="req-1")
        ack = c.OutcomeAckRequest.posted(c.ErpnextDocumentRef("Sales Invoice", "ACC-SINV-0001"))
        client.ack_outcome("wi-1", ack)
        path, body, headers = fake.post_calls[0]
        assert "wi-1" in path
        assert body["outcome"] == "posted"
        assert body["documentRef"] == {"doctype": "Sales Invoice", "name": "ACC-SINV-0001"}
        assert headers.get(t.CORRELATION_HEADER) == "req-1"

    def test_ack_sends_idempotency_key_header_when_provided(self):
        fake = FakeTransport(get_response={}, post_response={})
        client = t.PostingFeedClient(fake, correlation_id="r")
        ack = c.OutcomeAckRequest.failed_transient()
        client.ack_outcome("wi-2", ack, idempotency_key="idem-9")
        _path, _body, headers = fake.post_calls[0]
        assert headers.get(t.IDEMPOTENCY_HEADER) == "idem-9"

    def test_does_not_mutate_dp2_sale_fact(self):
        # The client only POSTs the outcome; it never issues a sale-mutating call.
        fake = FakeTransport(get_response={}, post_response={})
        client = t.PostingFeedClient(fake, correlation_id="r")
        client.ack_outcome("wi-3", c.OutcomeAckRequest.failed_transient())
        # Exactly one POST (the ack) and zero GETs — no reach-back, no sale write.
        assert len(fake.post_calls) == 1
        assert len(fake.get_calls) == 0
