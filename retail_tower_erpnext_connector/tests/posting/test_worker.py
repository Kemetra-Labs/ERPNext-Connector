# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""re-P2b worker loop — local unit tests (Task A).

Per the ratified re-P2b decision (hybrid isolate + raise-after): a feed page line missing
`erpnextItemRef` is a TERMINAL validation rejection for that work item only — the connector
acks it `permanently_rejected`/`validation`, the valid items on the same page still process,
and the page is marked degraded with an operational alert raised AFTER per-item outcomes are
recorded (never an abort that strands valid or already-acked items). No frappe.
"""

import pytest

from retail_tower_erpnext_connector.connector.posting import contracts as c
from retail_tower_erpnext_connector.connector.posting import transport as t
from retail_tower_erpnext_connector.connector.posting import worker as w


def _good_item(external_id: str) -> dict:
    return {
        "workItemRef": f"wi-{external_id}",
        "kind": "sale_post",
        "sourceSystem": "pos-pulse",
        "externalId": external_id,
        "payloadHash": "a" * 64,
        "businessDate": "2026-06-01",
        "itemCursor": f"cur-{external_id}",
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


def _bad_item(external_id: str) -> dict:
    """A line missing erpnextItemRef — the upstream contract violation (re-P2b)."""
    item = _good_item(external_id)
    del item["sale"]["lines"][0]["erpnextItemRef"]
    return item


class RecordingClient:
    """A fake PostingFeedClient: records acks, never hits the network."""

    def __init__(self):
        self.acks: list[tuple[str, str, str | None]] = []  # (work_item_ref, outcome, category)

    def ack_outcome(self, work_item_ref, ack, *, idempotency_key=None):
        cat = ack.reason.category if ack.reason else None
        self.acks.append((work_item_ref, ack.outcome, cat))
        return {}


class TestPullPostingsRawIsolation:
    def test_valid_items_parsed_invalid_isolated(self):
        # transport must NOT abort the page on a bad line — it returns valid items parsed and
        # invalid raw entries separately (re-P2b).
        page = {
            "items": [_good_item("A"), _bad_item("B"), _good_item("C")],
            "cursor": "page-1",
            "next_page_token": None,
        }

        class FakeTransport:
            def get(self, path, *, params, headers):
                return page

            def post(self, path, *, json, headers):
                return {}

        client = t.PostingFeedClient(FakeTransport(), correlation_id="r")
        result = client.pull_postings_raw(since=None)
        assert len(result.items) == 2  # A and C parsed
        assert {wi.external_id for wi in result.items} == {"A", "C"}
        assert len(result.invalid) == 1  # B isolated
        assert result.invalid[0].error_kind == "missing_erpnext_item_ref"
        assert result.cursor == "page-1"


class TestWorkerReP2b:
    def test_bad_item_acked_rejected_good_items_continue_page_degraded_returns_advancing(self):
        page = {
            "items": [_good_item("A"), _bad_item("B"), _good_item("C")],
            "cursor": "page-1",
            "next_page_token": None,
        }

        class FakeTransport:
            def get(self, path, *, params, headers):
                return page

            def post(self, path, *, json, headers):
                return {}

        client = t.PostingFeedClient(FakeTransport(), correlation_id="r")
        recording = RecordingClient()
        posted = []

        def poster(wi):
            posted.append(wi.external_id)

        # re-P2b: the worker posts valid items, acks bad ones, and RETURNS a degraded result
        # (it does NOT raise) — every item reached a terminal outcome, so the cursor must advance.
        result = w.process_page(client, recording, since=None, post_valid=poster)

        assert posted == ["A", "C"]  # valid items processed
        assert ("wi-B", "permanently_rejected", "validation") in recording.acks  # bad item rejected
        assert result.degraded is True
        assert result.cursor == "page-1"  # cursor advances — no page-level retry of acked items
        assert "wi-B" in result.degraded_message  # alert names the bad item
        assert result.rejected_refs == ("wi-B",)

    def test_clean_page_does_not_raise(self):
        page = {
            "items": [_good_item("A"), _good_item("C")],
            "cursor": "page-1",
            "next_page_token": None,
        }

        class FakeTransport:
            def get(self, path, *, params, headers):
                return page

            def post(self, path, *, json, headers):
                return {}

        client = t.PostingFeedClient(FakeTransport(), correlation_id="r")
        recording = RecordingClient()
        posted = []
        result = w.process_page(client, recording, since=None, post_valid=lambda wi: posted.append(wi.external_id))
        assert posted == ["A", "C"]
        assert result.degraded is False
        assert result.cursor == "page-1"

    def test_never_substitutes_item_identity(self):
        # The worker must NOT resolve/search/create/substitute — a bad line is rejected, full stop.
        page = {"items": [_bad_item("B")], "cursor": "page-1", "next_page_token": None}

        class FakeTransport:
            def get(self, path, *, params, headers):
                return page

            def post(self, path, *, json, headers):
                return {}

        client = t.PostingFeedClient(FakeTransport(), correlation_id="r")
        recording = RecordingClient()
        posted = []
        result = w.process_page(client, recording, since=None, post_valid=lambda wi: posted.append(wi))
        assert posted == []  # nothing posted; no substitute item invented
        assert recording.acks == [("wi-B", "permanently_rejected", "validation")]
        assert result.degraded is True
        assert result.cursor == "page-1"  # advances even when the whole page was bad


class TestAckFailureIsolation:
    """A 409/404 from a valid item's ack must NOT propagate and wedge the loop (poison-pill).

    Mirrors the re-P2b isolation: the failed item is logged/marked, the page completes degraded,
    and the cursor advances — DP2 owns reconciliation (017), the connector does not blind-retry.
    """

    def _page(self):
        return {
            "items": [_good_item("A"), _good_item("C")],
            "cursor": "page-1",
            "next_page_token": None,
        }

    def test_ack_conflict_on_a_valid_item_does_not_propagate(self):
        page = self._page()

        class FakeTransport:
            def get(self, path, *, params, headers):
                return page

            def post(self, path, *, json, headers):
                return {}

        client = t.PostingFeedClient(FakeTransport(), correlation_id="r")
        recording = RecordingClient()

        def poster(wi):
            # the live glue's ack can raise AckConflict (409) — must be isolated, not fatal.
            raise t.AckConflict("idempotency_key_conflict")

        result = w.process_page(client, recording, since=None, post_valid=poster)
        # the page completes (no propagation), is marked degraded, and the cursor advances
        assert result.degraded is True
        assert result.cursor == "page-1"
        assert set(result.ack_failed_refs) == {"wi-A", "wi-C"}

    def test_ack_not_found_also_isolated(self):
        page = self._page()

        class FakeTransport:
            def get(self, path, *, params, headers):
                return page

            def post(self, path, *, json, headers):
                return {}

        client = t.PostingFeedClient(FakeTransport(), correlation_id="r")
        recording = RecordingClient()
        result = w.process_page(
            client, recording, since=None,
            post_valid=lambda wi: (_ for _ in ()).throw(t.AckNotFound("not_found")),
        )
        assert result.degraded is True
        assert result.cursor == "page-1"


class KeyRecordingClient:
    """Records the full ack wire body and its idempotency key."""

    def __init__(self):
        self.acks: list[tuple[str, dict, str]] = []

    def ack_outcome(self, work_item_ref, ack, *, idempotency_key=None):
        self.acks.append((work_item_ref, ack.to_wire(), idempotency_key))
        return {}


def _reject_isolated(raw_item: dict) -> tuple[dict, str]:
    page = {"items": [raw_item], "cursor": "page-1", "next_page_token": None}

    class FakeTransport:
        def get(self, path, *, params, headers):
            return page

        def post(self, path, *, json, headers):
            return {}

    recording = KeyRecordingClient()
    w.process_page(
        t.PostingFeedClient(FakeTransport(), correlation_id="r"), recording, since=None, post_valid=lambda wi: None
    )
    _, wire, key = recording.acks[0]
    return wire, key


class TestIsolatedRejectionCarriesTheVersion:
    """RT-333: a malformed versioned item is fenced like any other attempt (Backend-Core #722)."""

    def test_a_versioned_malformed_item_echoes_and_keys_its_version(self):
        item = _bad_item("B")
        item["resolutionVersion"] = 2
        wire, key = _reject_isolated(item)
        assert wire["resolutionVersion"] == 2
        assert key == "wi-B:permanently_rejected:v2"

    @pytest.mark.parametrize("bad_version", [None, 0, "2", True])
    def test_an_unversioned_or_unreadable_version_keeps_the_legacy_ack(self, bad_version):
        item = _bad_item("B")
        if bad_version is not None:
            item["resolutionVersion"] = bad_version
        wire, key = _reject_isolated(item)
        assert "resolutionVersion" not in wire
        assert key == "wi-B:permanently_rejected"


class _Response:
    def __init__(self, status: int, body: dict | None = None):
        self.status, self.headers, self.body = status, {}, body or {}


class _BackendCoreAck:
    """A transport that enforces Backend-Core's ack contract: ``reason.message`` is 1..1000 chars.

    Anything else is answered 400, which ``PostingFeedClient._interpret_ack`` raises as RuntimeError.
    """

    def __init__(self, page: dict):
        self.page, self.posted = page, []

    def get(self, path, *, params, headers):
        return self.page

    def post(self, path, *, json, headers):
        self.posted.append(json)
        message = json.get("reason", {}).get("message", "")
        if not 1 <= len(message) <= 1000:
            return _Response(400, {"code": "validation_failed"})
        return _Response(201)


def _malformed_with(value: str) -> dict:
    """A malformed item whose parse error echoes ``value`` verbatim (``got {value!r}``)."""
    item = _good_item("B")
    item["sale"]["tenders"] = [{"method": value, "amount": "100.00"}]
    return item


class TestInvalidItemReasonIsBounded:
    """RT-409 scope 3: the isolated-item reason goes through the same scrub + bound as every reason."""

    def test_a_400_on_an_isolated_item_ack_escapes_the_page(self):
        # The path the issue asked to verify: Backend-Core's 400 is a RuntimeError the loop does not
        # catch, so the poller never saves the cursor and re-pulls the page every tick.
        page = {"items": [_bad_item("B")], "cursor": "page-1", "next_page_token": None}

        class Refusing(_BackendCoreAck):
            def post(self, path, *, json, headers):
                return _Response(400)

        client = t.PostingFeedClient(Refusing(page), correlation_id="r")
        with pytest.raises(RuntimeError, match="unexpected ack status 400"):
            w.process_page(client, client, since=None, post_valid=lambda wi: None)

    def test_an_over_long_parse_error_no_longer_stalls_the_page(self):
        page = {"items": [_malformed_with("x" * 5000), _good_item("C")], "cursor": "page-1", "next_page_token": None}
        transport = _BackendCoreAck(page)
        client = t.PostingFeedClient(transport, correlation_id="r")
        posted = []
        result = w.process_page(client, client, since=None, post_valid=lambda wi: posted.append(wi.external_id))
        assert result.cursor == "page-1" and result.rejected_refs == ("wi-B",)
        assert posted == ["C"]
        (ack,) = transport.posted
        assert ack["reason"]["category"] == "validation"
        assert 1 <= len(ack["reason"]["message"]) <= 1000
        assert ack["reason"]["message"].startswith("malformed_work_item: SaleTender.method")

    def test_a_credential_echoed_by_the_parse_error_is_scrubbed(self):
        wire, _key = _reject_isolated(_malformed_with("dp2_token=abc123"))
        assert "abc123" not in wire["reason"]["message"]
        assert "[redacted]" in wire["reason"]["message"]

    def test_a_quoted_credential_with_spaces_is_scrubbed_in_full(self):
        # CodeRabbit (PR #68): the parse error echoes `got 'dp2_token=alpha beta'`; no part may leak.
        wire, _key = _reject_isolated(_malformed_with("dp2_token=alpha beta"))
        assert "alpha" not in wire["reason"]["message"] and "beta" not in wire["reason"]["message"]

    def test_a_credential_holding_the_other_quote_is_scrubbed_in_full(self):
        # Codex (PR #69): the repr is single-quoted, so the double quotes are part of the credential.
        wire, _key = _reject_isolated(_malformed_with('dp2_token=alpha "beta gamma" omega'))
        assert "beta" not in wire["reason"]["message"] and "omega" not in wire["reason"]["message"]

    def test_markup_echoed_by_the_parse_error_is_plain_text(self):
        wire, _key = _reject_isolated(_malformed_with("<b>cash</b>"))
        assert "<" not in wire["reason"]["message"]
