# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1359 — the provisioner's pull client against the fake Backend-Core feed (contract
``provisioning-feed.yaml``): pull, ack with ``Idempotency-Key {requestId}:{step}:{attempt}``,
attempt floor, lease loss, the one-time credential, and the outcome."""

import pytest

from retail_tower_erpnext_connector.provisioner.errors import (
	AttemptCollision,
	AuthRejected,
	CredentialRefused,
	FeedUnavailable,
	LeaseLost,
)
from retail_tower_erpnext_connector.provisioner.feed_client import FeedClient, ProvisioningRequest
from retail_tower_erpnext_connector.provisioner.lease import AckSession

from .fakes import LEASE_S, REQUEST_ID, SECRET, FakeClock, FakeFeed, make_wire


@pytest.fixture
def feed():
	f = FakeFeed(FakeClock())
	f.add()
	return f


def test_pull_parses_the_request_and_the_cursor(feed):
	page = FeedClient(feed).pull()
	assert page.cursor == "1"
	req = page.items[0]
	assert isinstance(req, ProvisioningRequest)
	assert (req.id, req.kind, req.site_name, req.template_id, req.template_version) == (
		REQUEST_ID,
		"tenant.provision",
		"t0b9a3f0e6c5d4e2a8d52-pilot",
		"egypt-retail",
		1,
	)
	assert [s.display_name for s in req.stores] == ["Main Store", "Second Store"]


def test_pull_passes_cursor_and_limit(feed):
	FeedClient(feed).pull(cursor="abc", limit=5)
	assert feed.calls[0][2]["params"] == {"cursor": "abc", "limit": 5}


def test_a_malformed_request_is_a_protocol_error():
	with pytest.raises(ValueError):
		ProvisioningRequest.from_wire({"kind": "tenant.provision"})


def test_ack_sends_the_contract_key_and_returns_the_lease(feed):
	client = FeedClient(feed)
	lease = client.ack(REQUEST_ID, "claimed", "done", 1)
	call = feed.calls[-1]
	assert call[1].endswith(f"/requests/{REQUEST_ID}/steps")
	assert call[2]["headers"]["Idempotency-Key"] == f"{REQUEST_ID}:claimed:1"
	assert call[2]["json"] == {"step": "claimed", "outcome": "done", "attempt": 1}
	assert (lease - feed.clock()).total_seconds() == LEASE_S


def test_a_failed_step_carries_error_class_reason_and_detail(feed):
	client = FeedClient(feed)
	client.ack(REQUEST_ID, "claimed", "done", 1)
	client.ack(REQUEST_ID, "site_created", "permanently_rejected", 2, reason="unknown template", detail="d")
	assert feed.calls[-1][2]["json"] == {
		"step": "site_created",
		"outcome": "permanently_rejected",
		"attempt": 2,
		"errorClass": "permanently_rejected",
		"reason": "unknown template",
		"detail": "d",
	}


def test_a_lost_lease_is_lease_lost(feed):
	with pytest.raises(LeaseLost):
		FeedClient(feed).ack(REQUEST_ID, "site_created", "done", 1)


def test_a_reused_key_with_a_different_body_is_an_attempt_collision(feed):
	client = FeedClient(feed)
	client.ack(REQUEST_ID, "claimed", "done", 1)
	with pytest.raises(AttemptCollision):
		client.ack(REQUEST_ID, "claimed", "capacity", 1)


def test_401_is_a_fatal_auth_rejection(feed):
	feed.bearer_in_use = "other"
	with pytest.raises(AuthRejected):
		FeedClient(feed).pull()


def test_5xx_and_transport_errors_are_feed_unavailable(feed):
	feed.fail_next = [503]
	with pytest.raises(FeedUnavailable):
		FeedClient(feed).pull()
	feed.fail_next = [ConnectionError("reset")]
	with pytest.raises(FeedUnavailable):
		FeedClient(feed).pull()


def test_the_credential_is_returned_once_and_never_shown_in_repr(feed):
	client = FeedClient(feed)
	client.ack(REQUEST_ID, "claimed", "done", 1)
	cred = client.issue_credential(REQUEST_ID)
	assert cred.secret == SECRET
	assert cred.credential_id == "cred-1"
	assert SECRET not in repr(cred) and SECRET not in str(cred)
	assert "Idempotency-Key" not in feed.calls[-1][2]["headers"]


def test_a_refused_credential_is_credential_refused(feed):
	with pytest.raises(CredentialRefused):
		FeedClient(feed).issue_credential(REQUEST_ID)  # no lease: the non-disclosing 404


def test_outcome_key_and_body(feed):
	client = FeedClient(feed)
	client.ack(REQUEST_ID, "claimed", "done", 1)
	client.complete(REQUEST_ID, "conflict", 2, reason="site name exists with another tenant marker")
	call = feed.calls[-1]
	assert call[2]["headers"]["Idempotency-Key"] == f"{REQUEST_ID}:outcome:2"
	assert call[2]["json"] == {"outcome": "conflict", "reason": "site name exists with another tenant marker"}


def test_the_ack_session_counts_attempts_above_the_floor(feed):
	feed.add(**make_wire(provisioningRequestId="aaaaaaaa-1111-4222-8333-444444444444"))
	feed.requests["aaaaaaaa-1111-4222-8333-444444444444"].max_attempt = 7
	req = next(r for r in FeedClient(feed).pull().items if r.id.startswith("aaaaaaaa"))
	assert req.attempt_floor == 7
	session = AckSession(FeedClient(feed), req, clock=feed.clock, sleep=lambda s: None)
	session.ack("claimed", "done")
	session.ack("site_created", "done")
	keys = [c[2]["headers"]["Idempotency-Key"] for c in feed.calls if "Idempotency-Key" in c[2]["headers"]]
	assert keys[-2:] == [f"{req.id}:claimed:8", f"{req.id}:site_created:9"]


def test_the_ack_session_retries_a_lost_response_with_the_same_attempt(feed):
	session = AckSession(
		FeedClient(feed), FeedClient(feed).pull().items[0], clock=feed.clock, sleep=lambda s: None
	)
	feed.fail_next = [503]
	session.ack("claimed", "done")
	keys = [c[2]["headers"]["Idempotency-Key"] for c in feed.calls if "Idempotency-Key" in c[2]["headers"]]
	assert keys == [f"{REQUEST_ID}:claimed:1", f"{REQUEST_ID}:claimed:1"]


def test_the_ack_session_moves_to_a_new_attempt_on_a_collision(feed):
	req = FeedClient(feed).pull().items[0]
	feed.requests[REQUEST_ID].max_attempt = 3  # the floor moved after the pull (another provisioner)
	session = AckSession(FeedClient(feed), req, clock=feed.clock, sleep=lambda s: None)
	session.ack("claimed", "done")
	assert feed.requests[REQUEST_ID].max_attempt >= 4


def test_the_ack_session_tracks_the_remaining_lease(feed):
	session = AckSession(
		FeedClient(feed), FeedClient(feed).pull().items[0], clock=feed.clock, sleep=lambda s: None
	)
	session.ack("claimed", "done")
	assert session.remaining_seconds() == LEASE_S
	feed.clock.advance(100)
	assert session.remaining_seconds() == LEASE_S - 100


def test_ids_from_the_feed_must_be_canonical_uuids():
	good = make_wire()
	assert ProvisioningRequest.from_wire(good).id == REQUEST_ID
	for key in ("provisioningRequestId", "tenantId", "connectorRegistrationId"):
		for bad in ("x/../../admin", "", "7B0F3C1E3F1A4A529C1C0A8F6D2F6A11", REQUEST_ID + "/x"):
			with pytest.raises(ValueError):
				ProvisioningRequest.from_wire({**good, key: bad})


def test_a_request_with_a_bad_id_is_dropped_from_the_page_not_worked(feed):
	feed.add(**make_wire(provisioningRequestId="not-a-uuid"))
	ids = [r.id for r in FeedClient(feed).pull().items]
	assert ids == [REQUEST_ID]


class _Body:
	"""A transport whose responses are whatever the test says, to exercise malformed success bodies."""

	def __init__(self, get=None, post=None):
		self._get, self._post = get, post

	def get(self, path, *, params, headers):
		return self._get

	def post(self, path, *, json, headers):
		return self._post


def test_a_pull_that_is_not_a_page_object_is_a_protocol_error():
	from retail_tower_erpnext_connector.provisioner.errors import ProtocolError

	for body in ([], "x", {"items": "no"}):
		with pytest.raises(ProtocolError):
			FeedClient(_Body(get=body)).pull()


def test_a_page_item_that_is_not_an_object_is_dropped_not_fatal():
	page = FeedClient(_Body(get={"items": ["x", make_wire()], "cursor": "c"})).pull()
	assert [r.id for r in page.items] == [REQUEST_ID]


@pytest.mark.parametrize(
	"body",
	[{}, {"leaseExpiresAt": None}, {"leaseExpiresAt": "soon"}, {"leaseExpiresAt": "2026-11-14T10:20:00"}],
)
def test_an_ack_without_a_valid_lease_is_a_protocol_error(body):
	from retail_tower_erpnext_connector.provisioner.errors import ProtocolError
	from retail_tower_erpnext_connector.provisioner.feed_client import Response

	with pytest.raises(ProtocolError):
		FeedClient(_Body(post=Response(200, {}, body))).ack(REQUEST_ID, "claimed", "done", 1)


def test_an_incomplete_credential_response_is_a_protocol_error_and_names_no_secret():
	from retail_tower_erpnext_connector.provisioner.errors import ProtocolError
	from retail_tower_erpnext_connector.provisioner.feed_client import Response

	with pytest.raises(ProtocolError) as err:
		FeedClient(_Body(post=Response(201, {}, {"secret": SECRET}))).issue_credential(REQUEST_ID)
	assert SECRET not in str(err.value)
