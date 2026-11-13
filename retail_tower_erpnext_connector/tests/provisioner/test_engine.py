# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1359 — the step engine: each step's check and apply against a fake bench and a fake
Backend-Core feed; resume after a crash at every point; the PV0 recovery rules; ``conflict`` on a
marker mismatch; every error class mapped to the contract outcome."""

import hashlib

import pytest

from retail_tower_erpnext_connector.provisioner.config import ProvisionerConfig
from retail_tower_erpnext_connector.provisioner.engine import ProvisioningEngine
from retail_tower_erpnext_connector.provisioner.errors import (
	ErrorClass,
	LeaseLost,
	LeaseTooShort,
	ProvisioningError,
)
from retail_tower_erpnext_connector.provisioner.feed_client import FeedClient

from .fakes import (
	LEASE_S,
	OTHER_TENANT,
	REQUEST_ID,
	SITE,
	STORE_A,
	STORE_B,
	TENANT,
	CrashPlan,
	FakeClock,
	FakeFeed,
	FakeGateway,
	SimulatedCrash,
)

APP = "retail_tower_erpnext_connector"


class Env:
	def __init__(self, crash_at=None, **config):
		self.clock = FakeClock()
		self.plan = CrashPlan(crash_at)
		self.feed = FakeFeed(self.clock, self.plan)
		self.gw = FakeGateway(self.plan)
		self.config = ProvisionerConfig(
			base_url="https://bc.example", bench_path="/bench", credential_file="/secrets/prov", **config
		)

	def engine(self) -> ProvisioningEngine:
		return ProvisioningEngine(
			self.gw, FeedClient(self.feed), self.config, clock=self.clock, sleep=lambda s: None
		)

	def feed_client(self) -> FeedClient:
		return FeedClient(self.feed)

	def pull(self):
		return FeedClient(self.feed).pull().items

	def run(self) -> list[str]:
		return [self.engine().process(r) for r in self.pull()]

	@property
	def state(self):
		return self.feed.requests[REQUEST_ID]

	def steps(self):
		return [s for s in self.state.steps]


@pytest.fixture
def env():
	e = Env()
	e.feed.add()
	return e


def _final_site_ok(env: Env) -> None:
	site = env.gw.sites[SITE]
	assert site["marker"]["rt_tenant_id"] == TENANT
	assert site["marker"].get("rt_step_in_progress") is None
	assert {"frappe", "erpnext", APP} <= set(site["apps"])
	assert site["setup_complete"] and len(site["companies"]) == 1 and site["default_company"]
	assert site["template"] == {"id": "egypt-retail", "version": 1}
	assert site["scheduler"] and site["indexes_ok"] and site["credential_id"] and site["token_set"]
	assert site["admin_reset"] is True
	assert set(site["stores"]) == {STORE_A, STORE_B}


# --- the happy path ------------------------------------------------------------------------------


def test_a_new_tenant_is_provisioned_and_the_outcome_reports_the_result(env):
	assert env.run() == ["succeeded"]
	assert env.steps() == [
		("claimed", "done"),
		("site_created", "done"),
		("erp_configured", "done"),
		("connector_installed", "done"),
		("credential_bound", "done"),
		("verified", "done"),
	]
	body = env.state.terminal_body
	assert body["outcome"] == "succeeded"
	result = body["result"]
	assert result["siteName"] == SITE
	assert result["templateVersion"] == 1
	assert result["fiscalReadiness"] == "not_assessed"
	assert result["frappeVersion"] == "16.52.0" and result["erpnextVersion"] == "16.51.0"
	assert {s["storeId"]: (s["warehouseRef"], s["customerRef"]) for s in result["stores"]} == {
		STORE_A: ("Main Store - RT", "Retail Walk-in"),
		STORE_B: ("Second Store - RT", "Retail Walk-in"),
	}
	_final_site_ok(env)


def test_the_site_is_created_with_an_explicit_db_name_and_a_throwaway_admin_password(env):
	env.run()
	(_, site, db_name) = next(c for c in env.gw.calls if c[0] == "new_site")
	expected = "rtp_" + hashlib.sha256(SITE.encode()).hexdigest()[:16]
	assert (site, db_name) == (SITE, expected)
	# the throwaway admin password is replaced on the site straight after creation
	order = [c[0] if c[0] != "site_op" else f"op:{c[2]}" for c in env.gw.calls if c[0] != "write_marker"]
	assert order.index("op:reset_admin_password") == order.index("new_site") + 1


def test_the_template_is_applied_with_the_request_locale_and_the_legal_name(env):
	env.run()
	args = next(c[3] for c in env.gw.calls if c[0] == "site_op" and c[2] == "setup_wizard")
	assert (args["country"], args["currency"], args["timezone"], args["language"]) == (
		"Egypt",
		"EGP",
		"Africa/Cairo",
		"ar",
	)
	assert args["company_name"] == "Example Trading Co" and args["abbr"] == "RT"
	assert args["chart_of_accounts"] == "Standard"
	tpl = next(c[3] for c in env.gw.calls if c[0] == "site_op" and c[2] == "apply_template")
	assert (tpl["template_id"], tpl["template_version"]) == ("egypt-retail", 1)


def test_a_finished_site_is_not_touched_again(env):
	env.run()
	# the outcome was lost: the request is offered again on a fresh feed, the site is already done
	env.feed.requests[REQUEST_ID].terminal = None
	env.feed.requests[REQUEST_ID].lease_expires = None
	env.gw.calls.clear()
	assert env.run() == ["succeeded"]
	mutating = [c for c in env.gw.calls if c[0] in {"new_site", "drop_site", "install_app"}]
	ops = [c[2] for c in env.gw.calls if c[0] == "site_op" and c[2] != "inspect"]
	assert mutating == [] and ops == []
	assert env.state.credentials_issued == 1


# --- resume after a crash at every point --------------------------------------------------------


def _clean_trace_length() -> int:
	e = Env()
	e.feed.add()
	e.run()
	return len(e.plan.trace)


@pytest.mark.parametrize("crash_at", range(_clean_trace_length()))
def test_a_run_killed_at_any_point_resumes_to_success(crash_at):
	e = Env(crash_at=crash_at)
	e.feed.add()
	with pytest.raises(SimulatedCrash):
		e.run()
	e.plan.crash_at = None
	e.clock.advance(LEASE_S + 1)  # the dead run's lease expires; the request is offered again
	for _ in range(3):
		e.run()
		if e.state.terminal:
			break
	assert e.state.terminal == "succeeded"
	_final_site_ok(e)
	assert e.state.credentials_issued <= 2  # a lost credential response is a rotation, never more


# --- identity: the marker and the derived name ---------------------------------------------------


def test_a_site_with_another_tenants_marker_is_a_conflict_and_is_never_touched():
	e = Env()
	e.feed.add()
	e.gw.seed_foreign(SITE)
	assert e.run() == ["conflict"]
	assert e.state.terminal == "conflict"
	assert ("site_created", "conflict") in e.steps()
	assert not [c for c in e.gw.calls if c[0] in {"new_site", "drop_site", "install_app"}]
	assert e.gw.sites[SITE]["marker"]["rt_tenant_id"] == OTHER_TENANT


def test_an_unmarked_site_that_already_has_apps_is_a_conflict():
	e = Env()
	e.feed.add()
	e.gw.seed_foreign(SITE)
	e.gw.sites[SITE]["marker"] = None
	assert e.run() == ["conflict"]
	assert not [c for c in e.gw.calls if c[0] == "drop_site"]


def test_a_site_name_that_is_not_derived_from_the_tenant_id_is_a_conflict_before_any_bench_call():
	e = Env()
	e.feed.add(siteName="tffffffffffffffffffff-pilot")
	assert e.run() == ["conflict"]
	assert e.gw.calls == []


def test_a_second_company_on_the_site_is_a_conflict():
	e = Env()
	e.feed.add()
	e.run()
	e.state.terminal = None
	e.state.lease_expires = None
	e.gw.sites[SITE]["companies"].append("Another Co")
	assert e.run() == ["conflict"]


# --- PV0 recovery rules --------------------------------------------------------------------------


def _marked_site(e: Env, **marker):
	e.gw.sites[SITE] = {
		"marker": {"rt_tenant_id": TENANT, "rt_provisioning_request_id": REQUEST_ID, **marker},
		"apps": ["frappe"],
		**_blank(),
	}


@pytest.mark.parametrize("step", ["install_erpnext", "install_connector"])
def test_a_kill_during_an_install_drops_and_recreates_the_site(step):
	e = Env()
	e.feed.add()
	_marked_site(e, rt_step_in_progress=step)
	e.gw.sites[SITE]["broken"] = True
	assert e.run() == ["succeeded"]
	assert [c[0] for c in e.gw.calls if c[0] in {"drop_site", "new_site"}][:2] == ["drop_site", "new_site"]
	_final_site_ok(e)


def test_a_half_created_site_without_a_marker_is_dropped_and_recreated():
	e = Env()
	e.feed.add()
	e.gw.sites[SITE] = {"marker": None, "apps": ["frappe"], "broken": True, **_blank()}
	e.gw.intents[SITE] = {"tenant_id": TENANT, "request_id": REQUEST_ID}  # journal: we started it
	assert e.run() == ["succeeded"]
	assert e.gw.calls[0][0] == "drop_site"


def test_an_unmarked_frappe_only_site_with_no_journal_entry_is_a_conflict_and_is_not_dropped():
	e = Env()
	e.feed.add()
	e.gw.sites[SITE] = {"marker": None, "apps": ["frappe"], **_blank()}
	assert e.run() == ["conflict"]
	assert not [c for c in e.gw.calls if c[0] in {"drop_site", "new_site"}]


def test_a_journal_entry_of_another_tenant_does_not_allow_a_drop():
	e = Env()
	e.feed.add()
	e.gw.sites[SITE] = {"marker": None, "apps": ["frappe"], **_blank()}
	e.gw.intents[SITE] = {"tenant_id": OTHER_TENANT, "request_id": REQUEST_ID}
	assert e.run() == ["conflict"]
	assert not [c for c in e.gw.calls if c[0] == "drop_site"]


def test_the_journal_entry_is_written_before_the_site_is_created():
	e = Env()
	e.feed.add()
	e.run()
	names = [c[0] for c in e.gw.calls]
	assert names.index("write_intent") < names.index("new_site")


def test_names_longer_than_erpnext_allows_are_rejected_before_any_bench_call():
	for over in (
		{"legalName": "x" * 141},
		{"stores": [{"storeId": STORE_A, "displayName": "y" * 136}]},  # 136 + " - RT" > 140
	):
		e = Env()
		e.feed.add(**over)
		assert e.run() == ["rejected"]
		assert e.gw.calls == []


def test_a_dotted_site_name_with_the_derived_prefix_is_accepted():
	e = Env()
	e.feed.add(siteName="t0b9a3f0e6c5d4e2a8d52.pilot.example")
	assert e.run() == ["succeeded"]


def _blank():
	from .fakes import _blank_site

	return _blank_site()


def test_a_kill_at_a_later_step_resumes_in_place_without_dropping():
	e = Env()
	e.feed.add()
	e.run()  # a complete site
	site = e.gw.sites[SITE]
	site["template"] = None  # killed during the template
	site["scheduler"] = False
	e.state.terminal = None
	e.state.lease_expires = None
	e.gw.calls.clear()
	assert e.run() == ["succeeded"]
	assert not [c for c in e.gw.calls if c[0] in {"drop_site", "new_site"}]
	assert site["template"] == {"id": "egypt-retail", "version": 1} and site["scheduler"]


def test_a_dirty_site_after_the_credential_was_bound_is_never_recreated():
	e = Env()
	e.feed.add()
	_marked_site(e, rt_step_in_progress="install_connector")
	e.gw.sites[SITE]["credential_id"] = "cred-9"
	assert e.run() == ["rejected"]
	assert not [c for c in e.gw.calls if c[0] in {"drop_site", "new_site"}]
	assert e.state.terminal == "permanently_rejected"


# --- error classes -> contract outcomes ----------------------------------------------------------


def _fail_at_wizard(exc: Exception) -> Env:
	e = Env()
	e.feed.add()
	e.gw.fail_op["setup_wizard"] = exc
	return e


@pytest.mark.parametrize(
	("exc", "result", "step_outcome", "terminal"),
	[
		(RuntimeError("database unavailable"), "transient", "failed_transient", None),
		(TimeoutError("bench timed out"), "transient", "failed_transient", None),
		(ProvisioningError(ErrorClass.capacity, "disk full"), "capacity", "capacity", None),
		(
			ProvisioningError(ErrorClass.permanently_rejected, "unknown template"),
			"rejected",
			"permanently_rejected",
			"permanently_rejected",
		),
		(ProvisioningError(ErrorClass.conflict, "second company"), "conflict", "conflict", "conflict"),
		(
			ProvisioningError(ErrorClass.compatibility, "frappe 15"),
			"rejected",
			"compatibility",
			"permanently_rejected",
		),
		(
			ProvisioningError(ErrorClass.dependency, "registration disabled"),
			"rejected",
			"dependency",
			"permanently_rejected",
		),
	],
)
def test_each_error_class_is_mapped_to_the_contract_outcome(exc, result, step_outcome, terminal):
	e = _fail_at_wizard(exc)
	assert e.run() == [result]
	assert e.steps()[-1] == ("erp_configured", step_outcome)
	assert e.state.terminal == terminal
	if terminal and step_outcome in {"compatibility", "dependency"}:
		assert step_outcome in e.state.terminal_body["reason"]
	ack = [c for c in e.feed.calls if c[1].endswith("/steps")][-1][2]["json"]
	assert ack["errorClass"] == step_outcome
	assert "Traceback" not in str(ack)


def test_a_credential_refusal_is_the_dependency_outcome(env):
	env.feed.credential_status = 404
	assert env.run() == ["rejected"]
	assert env.steps()[-1] == ("credential_bound", "dependency")
	assert "dependency" in env.state.terminal_body["reason"]


def test_a_lost_credential_response_repeats_the_call_as_a_rotation(env):
	env.gw.fail_op["bind_credential"] = RuntimeError("bench died")
	assert env.run() == ["transient"]
	env.gw.fail_op.clear()
	env.clock.advance(LEASE_S + 1)
	assert env.run() == ["succeeded"]
	assert env.state.credentials_issued == 2


def test_an_unsupported_frappe_line_is_a_compatibility_failure(env):
	env.gw.versions["frappe"] = "15.80.0"
	assert env.run() == ["rejected"]
	assert any(s == ("erp_configured", "compatibility") for s in env.steps())
	assert not [c for c in env.gw.calls if c[0] == "site_op" and c[2] == "bind_credential"]


def test_the_site_ceiling_is_a_capacity_hold_before_any_site_is_created(env):
	env.config = ProvisionerConfig(
		base_url="https://bc.example", bench_path="/b", credential_file="/c", max_sites=20
	)
	env.gw.site_limit_count = 20
	assert env.run() == ["capacity"]
	assert env.steps()[-1] == ("site_created", "capacity")
	assert not [c for c in env.gw.calls if c[0] == "new_site"]
	assert env.state.terminal is None


def test_a_deep_queue_is_a_capacity_hold(env):
	env.gw.queue = 500
	assert env.run() == ["capacity"]
	assert not [c for c in env.gw.calls if c[0] == "new_site"]
	env.gw.queue = 499


def test_an_unknown_template_or_locale_is_rejected_before_the_site_is_touched():
	for over in (
		{"template": {"templateId": "egypt-retail", "templateVersion": 2}},
		{"locale": {"country": "SA", "currency": "SAR", "timezone": "Asia/Riyadh", "language": "ar"}},
	):
		e = Env()
		e.feed.add(**over)
		assert e.run() == ["rejected"]
		assert e.state.terminal == "permanently_rejected"
		assert e.gw.calls == []


def test_the_other_request_kinds_are_left_for_their_own_handlers():
	e = Env()
	e.feed.add(kind="store.provision")
	assert e.run() == ["skipped"]
	assert e.state.steps == [] and e.gw.calls == []


# --- the lease -----------------------------------------------------------------------------------


def test_a_long_step_is_not_started_when_the_lease_is_shorter_than_the_budget():
	e = Env(step_budget_seconds=LEASE_S + 1)
	e.feed.add()
	with pytest.raises(LeaseTooShort):
		e.run()
	assert not [c for c in e.gw.calls if c[0] in {"new_site", "install_app"}]


def test_losing_the_lease_stops_the_request():
	e = Env()
	e.feed.add()

	original = e.gw.new_site

	def new_site_then_lose_the_lease(*a, **k):
		original(*a, **k)
		e.state.lease_expires = e.clock()  # another provisioner took it

	e.gw.new_site = new_site_then_lose_the_lease
	with pytest.raises(LeaseLost):
		e.run()
	assert e.state.terminal is None


# --- validation hardening (security review) -------------------------------------------------------


@pytest.mark.parametrize(
	"name",
	[
		SITE + chr(10),  # a trailing line break passes a $-anchored match
		"t" + "0" * 19,  # too short
		SITE + "x" * 60,  # longer than a DNS label
		"-" + SITE,
		"../" + SITE,
		SITE + "/x",
	],
)
def test_a_malformed_site_name_is_a_conflict_before_any_bench_call(name):
	e = Env()
	e.feed.add(siteName=name)
	assert e.run() == ["conflict"]
	assert e.gw.calls == []


def test_the_throwaway_admin_password_is_redacted_from_a_failure_detail():
	e = Env()
	e.feed.add()
	original = e.gw.new_site

	def failing(site, db_name, admin_password):
		original(site, db_name, admin_password)
		raise RuntimeError(f"Command ['bench', '--admin-password', '{admin_password}'] failed")

	e.gw.new_site = failing
	e.run()
	(pw,) = e.gw.admin_passwords
	sent = repr([c[2]["json"] for c in e.feed.calls])
	assert pw not in sent and "[redacted]" in sent


def test_a_credential_for_another_registration_is_a_dependency_failure():
	e = Env()
	e.feed.add()
	real = e.feed.post

	def other_registration(path, *, json, headers):
		resp = real(path, json=json, headers=headers)
		if path.endswith("/credential"):
			resp.body["registrationId"] = "99999999-1111-4222-8333-444444444444"
		return resp

	e.feed.post = other_registration
	assert e.run() == ["rejected"]
	assert e.steps()[-1] == ("credential_bound", "dependency")
	assert not [c for c in e.gw.calls if c[0] == "site_op" and c[2] == "bind_credential"]


def test_a_recorded_credential_binding_means_the_site_is_never_recreated_even_if_unreadable():
	e = Env()
	e.feed.add()
	_marked_site(e, rt_step_in_progress="install_connector", rt_credential_bound=True)
	e.gw.sites[SITE]["broken"] = True
	assert e.run() == ["rejected"]
	assert not [c for c in e.gw.calls if c[0] in {"drop_site", "new_site"}]


def test_the_binding_is_recorded_in_the_marker_after_a_successful_bind(env):
	env.run()
	assert env.gw.sites[SITE]["marker"]["rt_credential_bound"] is True


# --- review round 2 ---------------------------------------------------------------------------------


def test_the_intent_journal_is_cleared_once_the_marker_exists():
	e = Env()
	e.feed.add()
	e.run()
	assert SITE not in e.gw.intents
	names = [c[0] for c in e.gw.calls]
	assert names.index("clear_intent") > names.index("write_intent")


def test_a_failing_drop_is_transient_and_creates_nothing_new():
	e = Env()
	e.feed.add()
	_marked_site(e, rt_step_in_progress="install_erpnext")
	e.gw.sites[SITE]["broken"] = True
	e.gw.drop_fails = True
	assert e.run() == ["transient"]
	assert not [c for c in e.gw.calls if c[0] == "new_site"]
	assert SITE in e.gw.sites


def test_a_site_provisioned_with_another_template_version_is_not_rewritten():
	e = Env()
	e.feed.add()
	_marked_site(e, rt_template="egypt-retail@0")
	assert e.run() == ["rejected"]
	assert not [c for c in e.gw.calls if c[0] in {"drop_site", "new_site", "install_app"}]


def test_a_credential_of_another_registration_on_the_site_is_replaced():
	e = Env()
	e.feed.add()
	e.run()
	site = e.gw.sites[SITE]
	site["registration_id"] = "99999999-1111-4222-8333-444444444444"  # an earlier generation
	e.state.terminal = None
	e.state.lease_expires = None
	e.state.credentials_issued = 0
	assert e.run() == ["succeeded"]
	assert e.state.credentials_issued == 1
	assert site["registration_id"] == e.state.wire["connectorRegistrationId"]


def test_a_second_company_found_at_verify_is_a_conflict():
	e = Env()
	e.feed.add()
	original = e.gw.site_op

	def two_companies(site, op, args=None, secret=None):
		out = original(site, op, args, secret)
		if op == "inspect" and e.gw.sites[site]["credential_id"]:
			out["companies"] = ["A", "B"]
		return out

	e.gw.site_op = two_companies
	assert e.run() == ["conflict"]


def test_the_cash_tender_is_mapped_even_when_the_tenant_has_no_store_yet():
	e = Env()
	e.feed.add(stores=[])
	assert e.run() == ["succeeded"]
	assert e.gw.sites[SITE]["tenders"] == ["cash"]
	assert e.state.terminal_body["result"]["stores"] == []


def test_a_site_without_the_cash_tender_is_not_reported_verified():
	e = Env()
	e.feed.add()
	e.run()
	e.state.terminal = None
	e.state.lease_expires = None
	e.gw.sites[SITE]["tenders"] = []
	e.gw.sites[SITE]["stores"] = {}  # an earlier run that never reached the maps
	assert e.run() == ["succeeded"]  # the maps step re-applies it
	assert e.gw.sites[SITE]["tenders"] == ["cash"]


def test_a_blank_posting_currency_is_set_even_when_every_store_is_already_mapped():
	e = Env()
	e.feed.add()
	e.run()
	e.state.terminal = None
	e.state.lease_expires = None
	e.gw.sites[SITE]["posting_currency"] = None  # maps and tender present, currency blank
	e.gw.calls.clear()
	assert e.run() == ["succeeded"]
	assert e.gw.sites[SITE]["posting_currency"] == "EGP"
	assert [c[2] for c in e.gw.calls if c[0] == "site_op" and c[2] == "bootstrap_stores"] == [
		"bootstrap_stores"
	]


def test_a_blank_posting_currency_is_set_for_a_tenant_with_no_store():
	e = Env()
	e.feed.add(stores=[])
	assert e.run() == ["succeeded"]
	assert e.gw.sites[SITE]["posting_currency"] == "EGP"
