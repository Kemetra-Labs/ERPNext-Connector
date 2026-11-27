# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1362 S3 — the ``credential.rotate`` handler against the fake feed (1.2.0-draft rules) and the fake
bench with dual-slot Connector Settings: the completion sequence on the site named by ``erpnextSiteRef``,
the lease id on every call after ``claimed``, the site check before any issue, the fenced pending-slot
write, recovery from a lost response or a failed write, and "stop and re-pull" (never ``dependency``)."""

import json

import pytest

from retail_tower_erpnext_connector.provisioner.config import ProvisionerConfig
from retail_tower_erpnext_connector.provisioner.engine import (
	CONFLICT,
	REJECTED,
	TRANSIENT,
	VERIFIED,
	ProvisioningEngine,
)
from retail_tower_erpnext_connector.provisioner.errors import LeaseLost, LeaseTooShort, ProtocolError
from retail_tower_erpnext_connector.provisioner.feed_client import FeedClient
from retail_tower_erpnext_connector.provisioner.lock import FileLock
from retail_tower_erpnext_connector.provisioner.runner import ProvisionerRunner

from .fakes import (
	ADOPTED_SITE,
	REGISTRATION,
	REQUEST_ID,
	SECRET,
	SITE,
	FakeClock,
	FakeFeed,
	FakeGateway,
)


class Env:
	def __init__(self, **wire):
		self.clock = FakeClock()
		self.feed = FakeFeed(self.clock)
		self.gw = FakeGateway()
		self.gw.backend = self.feed.present  # the Connector's heartbeat reaches this Backend-Core
		self.gw.probe = lambda credential_id: 200 if self.feed.present(credential_id) else 401
		self.gw.clock = self.clock
		self.config = ProvisionerConfig(
			base_url="https://bc.example", bench_path="/bench", credential_file="/secrets/prov"
		)
		self.feed.add_rotate(**wire)
		self.site = self.gw.seed_adopted()

	def engine(self) -> ProvisioningEngine:
		return ProvisioningEngine(
			self.gw, FeedClient(self.feed), self.config, clock=self.clock, sleep=lambda s: None
		)

	def run(self) -> str:
		(request,) = FeedClient(self.feed).pull().items
		return self.engine().process(request)

	@property
	def state(self):
		return self.feed.requests[REQUEST_ID]

	def ops(self) -> list[str]:
		return [c[2] for c in self.gw.calls if c[0] == "site_op"]

	def feed_paths(self) -> list[str]:
		return [c[1].rsplit("/", 1)[-1] for c in self.feed.calls if c[0] == "POST"]


@pytest.fixture
def env():
	return Env()


def test_the_rotation_completes_without_a_succeeded_outcome(env):
	assert env.run() == VERIFIED
	# ``verified`` arrives after Backend-Core recorded ``succeeded`` (the heartbeat presented the pending
	# credential): a late report with the FINAL lease id, answered 200 and changing nothing.
	assert env.state.steps == [("claimed", "done"), ("credential_bound", "done")]
	assert env.feed_paths()[-1] == "steps"
	assert "outcome" not in env.feed_paths()  # Backend-Core completes it when the pending one is used
	assert env.state.terminal == "succeeded"
	assert env.site["credential_id"] == "cred-rot-1" and env.site["pending"] is None  # promoted
	assert env.state.revoked == ["cred-current"]  # the old credential, server-side, at acceptance


def test_every_call_after_claimed_carries_the_claims_lease_id(env):
	env.run()
	(claimed, *later) = env.state.lease_headers
	assert claimed == ("steps", None)
	assert later and all(header == env.state.final_lease_id for _, header in later)
	assert [path for path, _ in later] == ["credential", "steps", "steps"]


def test_the_site_is_found_by_erpnext_site_ref_never_by_site_name(env):
	env.run()
	touched = {c[1] for c in env.gw.calls if c[0] == "site_op"}
	assert touched == {ADOPTED_SITE} and SITE not in env.gw.sites


def test_a_stored_current_credential_does_not_skip_the_rotation(env):
	# Unlike tenant.provision, a credential id of this registration on the site is the starting point.
	assert env.site["credential_id"] == "cred-current"
	env.run()
	assert env.state.credentials_issued == 1
	assert env.ops() == ["inspect", "stage_pending_credential", "confirm_credential"]


def test_the_secret_reaches_the_site_only_through_the_pending_write(env):
	env.run()
	with_secret = [p for p in env.gw.stdin_payloads if p["secret"]]
	assert [p["op"] for p in with_secret] == ["stage_pending_credential"]
	sent = json.dumps([c[2] for c in env.feed.calls]) + json.dumps([c for c in env.gw.calls])
	assert SECRET not in sent


def test_no_credential_call_when_the_site_is_not_on_this_host():
	env = Env(erpnextSiteRef="other.localhost")
	assert env.run() == CONFLICT
	assert "credential" not in env.feed_paths()
	assert env.state.steps[-1] == ("credential_bound", "conflict")
	assert env.state.terminal == "conflict"


def test_no_credential_call_when_the_settings_name_another_registration(env):
	env.site["registration_id"] = "11111111-2222-4333-8444-555555555555"
	assert env.run() == CONFLICT
	assert "credential" not in env.feed_paths()
	assert env.state.credentials_issued == 0


@pytest.mark.parametrize("ref", ["../etc", "a/b", "a\\b", "..", ".hidden", "", None, "x" * 300])
def test_an_unsafe_site_ref_never_reaches_the_bench(ref):
	env = Env(erpnextSiteRef=ref)
	assert env.run() == REJECTED
	assert env.gw.calls == [] and "credential" not in env.feed_paths()


def test_a_lost_credential_response_is_recovered_by_repeating_the_call(env):
	issue = env.feed._rotate_credential
	lost = []

	def lose_first(s, headers):
		resp = issue(s, headers)
		if not lost:
			lost.append(resp)
			raise ConnectionError("response lost")
		return resp

	env.feed._rotate_credential = lose_first
	assert env.run() == VERIFIED
	assert env.state.credentials_issued == 2
	assert "cred-rot-1" in env.state.revoked  # the repeat revoked the unpresented one
	assert env.site["credential_id"] == "cred-rot-2"


def test_a_failed_write_is_recovered_by_repeating_the_call_then_writing_again(env):
	env.gw.stage_fails = [RuntimeError("bench rt-provision-site-step failed (exit 1)")]
	assert env.run() == VERIFIED
	assert env.state.credentials_issued == 2 and "cred-rot-1" in env.state.revoked
	assert env.site["credential_id"] == "cred-rot-2"


def test_writes_that_keep_failing_end_as_a_transient_failure_and_current_keeps_posting(env):
	env.gw.stage_fails = [RuntimeError("bench failed")] * 3
	assert env.run() == TRANSIENT
	assert env.state.steps[-1] == ("credential_bound", "failed_transient")
	assert env.site["credential_id"] == "cred-current" and env.site["pending"] is None


def test_an_older_credential_never_replaces_a_newer_staged_one(env):
	env.site["pending"] = {"credential_id": "cred-newer", "issued_at": "2030-01-01T00:00:00Z"}
	with pytest.raises(LeaseLost):
		env.run()
	assert env.site["pending"]["credential_id"] == "cred-newer"  # nothing written
	assert env.state.steps == [("claimed", "done")]  # nothing reported: a newer worker owns it
	assert env.site["credential_id"] == "cred-current"


def test_a_late_retry_never_stages_a_credential_older_than_the_promoted_current_one(env):
	# B was issued later and is already promoted (PENDING empty); the late retry of A stages nothing.
	env.site["credential_id"], env.site["current_issued_at"] = "cred-B", "2026-11-14T10:05:00Z"
	with pytest.raises(LeaseLost):
		env.run()  # A is issued at 10:00:00.000001Z, before B
	assert env.site["pending"] is None and env.site["credential_id"] == "cred-B"
	assert env.state.steps == [("claimed", "done")]  # stop and re-pull, never a dependency failure


def test_a_retry_of_an_already_staged_credential_is_a_no_op(env):
	env.site["pending"] = {"credential_id": "cred-rot-1", "issued_at": "2026-11-14T10:00:00.000001Z"}
	assert env.run() == VERIFIED
	assert env.site["credential_id"] == "cred-rot-1"


def test_a_refused_credential_call_stops_and_re_pulls_never_a_dependency_failure(env):
	env.feed.credential_status = 404
	with pytest.raises(LeaseLost):
		env.run()
	assert env.state.steps == [("claimed", "done")] and env.state.terminal is None


def test_a_lease_lost_on_a_step_stops_without_reporting(env):
	stage = env.gw._stage

	def superseded(s, args, secret):
		result = stage(s, args, secret)
		env.state.lease_id = "00000000-0000-4000-8000-000000000000"  # a newer claim took the lease
		return result

	env.gw._stage = superseded
	with pytest.raises(LeaseLost):
		env.run()
	assert env.state.steps == [("claimed", "done")] and env.state.terminal is None


def test_a_replayed_claim_is_lease_lost_so_two_workers_never_share_a_lease_id(env):
	ack = env.feed._rotate_ack
	lost = []

	def lose_first_claim(s, body, headers):
		resp = ack(s, body, headers)
		if body["step"] == "claimed" and not lost:
			lost.append(resp)
			raise ConnectionError("response lost")
		return resp

	env.feed._rotate_ack = lose_first_claim
	with pytest.raises(LeaseLost):
		env.run()
	assert env.state.credentials_issued == 0


def test_a_claim_without_a_lease_id_does_nothing(env):
	result = env.feed._ack_result
	env.feed._ack_result = lambda s, step: {k: v for k, v in result(s, step).items() if k != "leaseId"}
	with pytest.raises(ProtocolError):
		env.run()
	assert env.gw.calls == [] and env.state.credentials_issued == 0


def test_a_lease_too_short_writes_nothing(env):
	env.config = ProvisionerConfig(
		base_url="https://bc.example",
		bench_path="/bench",
		credential_file="/secrets/prov",
		short_timeout_seconds=10_000,
	)
	with pytest.raises(LeaseTooShort):
		env.run()
	assert "stage_pending_credential" not in env.ops()


def test_a_pending_credential_not_accepted_yet_is_a_transient_failure(env):
	env.gw.backend = lambda credential_id: False  # Backend-Core unreachable from the site
	assert env.run() == TRANSIENT
	assert env.state.steps[-1] == ("verified", "failed_transient")
	assert env.site["pending"]["credential_id"] == "cred-rot-1"  # staged, CURRENT untouched
	assert env.site["credential_id"] == "cred-current"
	assert "outcome" not in env.feed_paths()


def test_a_newer_pending_credential_during_confirmation_stops_and_re_pulls(env):
	confirm = env.gw._confirm

	def newer_staged(s):
		s["pending"] = {"credential_id": "cred-from-a-newer-worker", "issued_at": "2030-01-01T00:00:00Z"}
		return confirm(s)

	env.gw.backend = lambda credential_id: False
	env.gw._confirm = newer_staged
	with pytest.raises(LeaseLost):
		env.run()
	assert ("verified", "failed_transient") not in env.state.steps


def test_a_site_without_the_pending_slot_fails_before_any_credential_is_issued(env):
	env.site["pending_slot"] = False  # this Connector release is not migrated on the site
	assert env.run() == REJECTED
	assert env.state.steps[-1] == ("credential_bound", "compatibility")
	assert "credential" not in env.feed_paths() and env.state.credentials_issued == 0
	assert env.site["credential_id"] == "cred-current"


def test_a_compatibility_refusal_of_the_write_itself_is_not_retried(env):
	from retail_tower_erpnext_connector.provisioner.errors import ErrorClass, ProvisioningError

	env.gw.stage_fails = [ProvisioningError(ErrorClass.compatibility, "no pending credential slot")]
	assert env.run() == REJECTED
	assert env.state.steps[-1] == ("credential_bound", "compatibility")
	assert env.state.credentials_issued == 1 and env.site["credential_id"] == "cred-current"


def _accepted_but_not_promoted(env) -> None:
	"""Rotation A finished server-side (Backend-Core accepted A, revoked the old one, succeeded) but
	the 2xx was lost before the local promotion: A is Backend-Core's current, yet still sits in the
	PENDING slot, and the CURRENT slot holds the revoked one."""
	env.site["pending"] = {"credential_id": "cred-A", "issued_at": "2026-11-14T09:00:00Z"}
	env.state.current_id = "cred-A"
	env.state.revoked = ["cred-current"]


def test_a_later_rotation_promotes_an_accepted_occupant_before_replacing_it(env):
	_accepted_but_not_promoted(env)
	env.gw.backend = lambda credential_id: False  # rotation B is then never accepted
	assert env.run() == TRANSIENT
	assert env.site["credential_id"] == "cred-A"  # the working credential is CURRENT now
	assert env.site["pending"]["credential_id"] == "cred-rot-1"
	# B fails terminally and Backend-Core revokes it: the site still holds Backend-Core's current one.
	env.state.revoked.append("cred-rot-1")
	assert env.feed.present(env.site["credential_id"]) is True


def test_an_occupant_that_cannot_be_settled_defers_the_write(env):
	_accepted_but_not_promoted(env)
	env.gw.probe = lambda credential_id: None  # no answer: unknown whether A is current
	with pytest.raises(LeaseLost):
		env.run()
	assert env.site["pending"]["credential_id"] == "cred-A"  # nothing replaced
	assert env.site["credential_id"] == "cred-current"
	assert env.state.steps == [("claimed", "done")]


def test_a_revoked_occupant_is_replaced(env):
	env.site["pending"] = {"credential_id": "cred-old-pending", "issued_at": "2026-11-14T09:00:00Z"}
	assert env.run() == VERIFIED  # the probe answers 401: revoked, so it is overwritten
	assert env.site["credential_id"] == "cred-rot-1"


def test_the_write_carries_the_lease_end_and_is_bounded_by_it(env):
	env.run()
	(stage,) = [c[3] for c in env.gw.calls if c[0] == "site_op" and c[2] == "stage_pending_credential"]
	lease_end = env.feed.requests[REQUEST_ID].lease_expires
	assert stage["deadline"] <= lease_end.isoformat().replace("+00:00", "Z")
	((_, timeout),) = [t for t in env.gw.timeouts if t[0] == "stage_pending_credential"]
	assert 0 < timeout <= 900


def test_a_write_delayed_past_the_lease_end_writes_nothing(env):
	stage = env.gw._stage

	def stalled(s, args, secret):
		env.clock.advance(901)  # the bench call stalled past the lease; a newer worker may own it now
		return stage(s, args, secret)

	env.gw._stage = stalled
	with pytest.raises(LeaseTooShort):
		env.run()
	assert env.site["pending"] is None and env.site["credential_id"] == "cred-current"
	assert env.state.steps == [("claimed", "done")]


def test_a_write_killed_at_the_lease_end_stops_and_re_pulls(env):
	env.gw.stage_fails = [TimeoutError("bench command timed out after 899s")]
	with pytest.raises(LeaseTooShort):
		env.run()
	assert env.state.credentials_issued == 1 and env.state.steps == [("claimed", "done")]


def test_the_runner_releases_a_rotation_that_must_be_re_pulled(env, tmp_path):
	env.feed.credential_status = 404
	runner = ProvisionerRunner(
		env.engine(), FeedClient(env.feed), FileLock(str(tmp_path / "lock")), clock=env.clock
	)
	assert runner.run_once()["results"] == {REQUEST_ID: "released"}


def test_the_request_carries_the_registration_this_site_names():
	assert Env().state.wire["connectorRegistrationId"] == REGISTRATION
