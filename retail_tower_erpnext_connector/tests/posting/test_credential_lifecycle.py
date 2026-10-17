# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Local unit tests for the pure credential-lifecycle helpers (007 T2.3 → drives T2.1/T2.2).

Spec 007 (Connector Admin Counterpart, D9/G-2) replaces the connector's purely-reactive auth model
(E-5: only a 401 ever surfaces a credential problem) with a proactive one: from the recorded
``dp2_credential_expires_at`` the connector compares ``now()`` to the expiry and surfaces an
operator-facing 'credential expiring — request rotation' state BEFORE a 401 ever fires. The
connector CANNOT self-rotate (the 018 admin surface is session-only — N-5); it only warns/prompts.

These helpers are pure (frappe-free) so the branching is unit-testable WITHOUT a bench, mirroring
the ``posting/transport.py`` injected-fake pattern. Time is INJECTED (``now`` + ``expires_at`` are
explicit params) — no hidden clock, so the warning-fires/-does-not boundary is deterministic. The
bench-only poller shell passes ``frappe.utils.now_datetime()``.

This module imports NO frappe.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from retail_tower_erpnext_connector.connector.posting import credential_lifecycle as cl

# A fixed reference instant (injected, never `datetime.now()`) — keeps every assertion deterministic.
_NOW = datetime(2026, 6, 12, 12, 0, 0)
# The spec's default warning lead time is a plan-phase OQ-1; the helper takes it as a parameter
# with a documented default so the policy is not silently hard-coded.
_LEAD = timedelta(days=14)


class TestCredentialExpiryState:
	def test_active_when_expiry_far_in_future(self):
		# Expiry well beyond the warning window → plain 'active', no operator action prompted.
		state = cl.evaluate_expiry(now=_NOW, expires_at=_NOW + timedelta(days=60), warn_within=_LEAD)
		assert state.status == cl.CredentialExpiryStatus.ACTIVE
		assert state.should_warn is False

	def test_warns_when_expiry_within_lead_window(self):
		# Inside the lead window (but not yet expired) → 'expiry_warning', operator prompted to
		# request a rotation. This is the substantive new behavior vs the reactive E-5 model.
		state = cl.evaluate_expiry(now=_NOW, expires_at=_NOW + timedelta(days=3), warn_within=_LEAD)
		assert state.status == cl.CredentialExpiryStatus.EXPIRY_WARNING
		assert state.should_warn is True

	def test_boundary_exactly_at_lead_window_warns(self):
		# Exactly `warn_within` away is inside the window (inclusive) — warn, don't wait one more day.
		state = cl.evaluate_expiry(now=_NOW, expires_at=_NOW + _LEAD, warn_within=_LEAD)
		assert state.status == cl.CredentialExpiryStatus.EXPIRY_WARNING
		assert state.should_warn is True

	def test_expired_when_now_past_expiry(self):
		# Past expiry → 'expired' (distinct from the in-window warning): the next live call will get
		# the US4 non-disclosing 401; surface it as an expired-credential state, still warn.
		state = cl.evaluate_expiry(now=_NOW, expires_at=_NOW - timedelta(minutes=1), warn_within=_LEAD)
		assert state.status == cl.CredentialExpiryStatus.EXPIRED
		assert state.should_warn is True

	def test_unknown_when_no_expiry_recorded(self):
		# A legacy / not-yet-reconfigured credential has no recorded expiry (E-5). That is NOT
		# 'active' (we cannot prove it is) and NOT a crash — it is 'unknown': cannot warn proactively,
		# falls back to the reactive 401 path. Distinguishing this prevents a missing expiry from
		# masquerading as a healthy credential (Principle VI).
		state = cl.evaluate_expiry(now=_NOW, expires_at=None, warn_within=_LEAD)
		assert state.status == cl.CredentialExpiryStatus.UNKNOWN
		assert state.should_warn is False

	def test_message_never_contains_a_secret_value(self):
		# S-1 / G-4: the operator-facing message names the credential by its NON-secret id and the
		# expiry, never the raw token. The helper is not even given the secret — but assert the
		# surfaced message carries only non-secret lifecycle fields.
		state = cl.evaluate_expiry(
			now=_NOW,
			expires_at=_NOW + timedelta(days=2),
			warn_within=_LEAD,
			credential_id="cred_abc123",
		)
		assert "cred_abc123" in state.message
		assert "request rotation" in state.message.lower()


class TestParseWarnWithin:
	# OQ-1a (configurable warn lead-time): the operator sets dp2_credential_warn_days in Connector
	# Settings. The PARSE lives here (pure, locally testable); only the field read is bench-only.
	# Blank/unset → the documented default (operator left it alone); a non-numeric or negative value
	# is a CONFIG error surfaced loud (Principle VI — never silently fall back, which would hide a
	# fat-fingered setting and warn on the wrong schedule).
	def test_blank_or_none_yields_default(self):
		assert cl.parse_warn_within(None) == cl.DEFAULT_WARN_WITHIN
		assert cl.parse_warn_within("") == cl.DEFAULT_WARN_WITHIN
		assert cl.parse_warn_within(0) == cl.DEFAULT_WARN_WITHIN  # 0/unset Frappe Int → default

	def test_positive_int_becomes_timedelta_days(self):
		assert cl.parse_warn_within(7) == timedelta(days=7)
		assert cl.parse_warn_within("30") == timedelta(days=30)  # Frappe may hand back a string

	def test_negative_is_config_error(self):
		with pytest.raises(cl.ConfigError):
			cl.parse_warn_within(-1)

	def test_non_numeric_is_config_error(self):
		with pytest.raises(cl.ConfigError):
			cl.parse_warn_within("soon")


class TestAuthFailedSurfacing:
	# T2.2 / 003 §8: a 401 on pull/ack is a CREDENTIAL problem, not a server transient. It must
	# surface a non-disclosing 're-authenticate' state, never expose the raw/previous token, and
	# never be blind-retried as transient. This is the reactive path that COEXISTS with the proactive
	# expiry path — a credential revoked BEFORE expiry (admin revoke / disabled instance, E-4)
	# surfaces here even while evaluate_expiry would still call it 'active'.
	def test_401_surfaces_reauth_state(self):
		state = cl.classify_auth_failure(operation="pull")
		assert state.status == cl.CredentialAuthStatus.AUTH_FAILED
		assert state.reauthenticate_required is True

	def test_401_is_not_transient_no_blind_retry(self):
		# The connector MUST NOT retry a 401 as if the server were transiently failing (003 §8).
		state = cl.classify_auth_failure(operation="ack")
		assert state.retryable is False

	def test_401_message_never_exposes_a_token(self):
		# S-1 / 003 §8 #2: the operator-facing message must NOT contain the raw token, the previous
		# token, or any credential. The classifier is not even given a token — assert the surfaced
		# message stays non-disclosing and carries only the operation + a re-auth instruction.
		leaked = "supersecret-bearer-value-1234567890"  # a fake token literal, not a real secret
		state = cl.classify_auth_failure(operation="pull")
		assert leaked not in state.message
		assert "re-authenticate" in state.message.lower() or "re-provision" in state.message.lower()

	def test_401_message_is_non_disclosing(self):
		# The 401 body itself is non-disclosing (does not distinguish revoked/invalid/missing-binding,
		# auth-policy §8). The surfaced state must NOT invent a specific cause it cannot know.
		state = cl.classify_auth_failure(operation="pull")
		# must not claim a specific disclosed cause the non-disclosing 401 cannot support
		assert "revoked" not in state.message.lower()


class TestBuildExpiryWarning:
	# OQ-1b — the TOTAL helper the poller shell calls each tick: composes parse_warn_within +
	# evaluate_expiry and returns a log-payload dict when a warning should surface, or None when it
	# should not. It NEVER raises — a malformed warn_days returns a 'warn_check_failed' payload
	# instead, so the poller's posting tick is never aborted by an advisory warning (best-effort).
	# This is the branching/exception policy moved OUT of the frappe-coupled poller into the
	# locally-testable layer (config.py's stated discipline).
	def test_returns_none_when_active(self):
		assert (
			cl.build_expiry_warning(
				now=_NOW,
				expires_at=_NOW + timedelta(days=60),
				warn_days_raw=14,
				credential_id="cred_x",
			)
			is None
		)

	def test_returns_none_when_unknown(self):
		# No recorded expiry → UNKNOWN → should_warn False → no payload (reactive fallback).
		assert (
			cl.build_expiry_warning(now=_NOW, expires_at=None, warn_days_raw=14, credential_id="cred_x")
			is None
		)

	def test_returns_payload_when_expiring(self):
		payload = cl.build_expiry_warning(
			now=_NOW,
			expires_at=_NOW + timedelta(days=3),
			warn_days_raw=14,
			credential_id="cred_x",
		)
		assert payload is not None
		assert payload["event"] == "posting.credential.expiring"
		assert payload["status"] == cl.CredentialExpiryStatus.EXPIRY_WARNING.value
		assert "cred_x" in payload["detail"]

	def test_respects_configured_warn_days(self):
		# With a 1-day window, an expiry 3 days out is still ACTIVE → no payload. Proves the
		# configured value (not the 14-day default) actually drives the decision.
		assert (
			cl.build_expiry_warning(
				now=_NOW,
				expires_at=_NOW + timedelta(days=3),
				warn_days_raw=1,
				credential_id="cred_x",
			)
			is None
		)

	def test_malformed_warn_days_returns_warn_check_failed_not_raise(self):
		# THE point of the total helper: a fat-fingered warn_days must NOT raise (which would abort
		# the posting tick); it returns a distinct best-effort failure payload the poller logs.
		payload = cl.build_expiry_warning(
			now=_NOW,
			expires_at=_NOW + timedelta(days=3),
			warn_days_raw="soon",
			credential_id="cred_x",
		)
		assert payload is not None
		assert payload["event"] == "posting.credential.warn_check_failed"

	def test_payload_never_contains_a_token(self):
		# S-1 / G-4: the helper is given only non-secret fields; assert nothing token-like leaks.
		payload = cl.build_expiry_warning(
			now=_NOW,
			expires_at=_NOW - timedelta(days=1),  # expired → payload present
			warn_days_raw=14,
			credential_id="cred_x",
		)
		assert payload is not None
		assert "Bearer" not in payload["detail"]


class TestCheckRegistrationLink:
	# OQ-2 (warn-only): mirror the US4 server-side link (E-4) as a LOCAL advisory. When a token is
	# configured but no dp2_connector_registration_id is recorded, the credential is a legacy
	# UNLINKED token — it still works until US4 enforcement is live (E-5), so we WARN, never block
	# (a hard skip could halt a working token mid-cutover before US4 is live). Total fn: returns a
	# payload to warn, or None. Takes only the token's PRESENCE (a bool), never the token value (S-1).
	def test_unlinked_token_returns_warning(self):
		payload = cl.check_registration_link(token_present=True, registration_id="")
		assert payload is not None
		assert payload["event"] == "posting.credential.unlinked"

	def test_unlinked_token_none_registration_returns_warning(self):
		payload = cl.check_registration_link(token_present=True, registration_id=None)
		assert payload is not None
		assert payload["event"] == "posting.credential.unlinked"

	def test_linked_token_returns_none(self):
		# Token + registration both present → properly linked → no warning.
		assert cl.check_registration_link(token_present=True, registration_id="reg_abc") is None

	def test_no_token_returns_none(self):
		# No token configured at all is the unconfigured state (handled elsewhere by the
		# base-url/token gate); the link invariant only fires when a token IS set but unlinked.
		assert cl.check_registration_link(token_present=False, registration_id="") is None
		assert cl.check_registration_link(token_present=False, registration_id=None) is None

	def test_whitespace_only_registration_counts_as_unlinked(self):
		# A registration id of only whitespace is not a real link — treat as empty.
		payload = cl.check_registration_link(token_present=True, registration_id="   ")
		assert payload is not None
		assert payload["event"] == "posting.credential.unlinked"

	def test_payload_carries_no_token(self):
		# S-1 / G-4: the helper is given a bool, not the token — assert the message stays clean.
		payload = cl.check_registration_link(token_present=True, registration_id="")
		assert "Bearer" not in payload["detail"]
		assert "register" in payload["detail"].lower()


class TestRotationPreservesRegistration:
	# §6 / E-1/E-3: a rotate swaps the secret + credential id + expiry, but the registration id is
	# the STABLE identity that survives rotation. This guards the apply-the-rotation transform so a
	# rotate never accidentally clears or changes the registration link (which would orphan the
	# instance from the US4 guard's findActiveConnectorCredentialByTokenId lookup, E-4).
	def test_rotation_swaps_credential_fields_keeps_registration(self):
		before = cl.CredentialRefs(
			registration_id="reg_stable",
			credential_id="cred_old",
			expires_at=_NOW + timedelta(days=1),
		)
		after = cl.apply_rotation(
			before,
			new_credential_id="cred_new",
			new_expires_at=_NOW + timedelta(days=90),
		)
		assert after.registration_id == "reg_stable"  # unchanged — survives rotation
		assert after.credential_id == "cred_new"
		assert after.expires_at == _NOW + timedelta(days=90)

	def test_rotation_does_not_mutate_the_input(self):
		# Immutable-by-default (repo coding style): apply_rotation returns a NEW CredentialRefs and
		# leaves the original untouched, so a caller holding the pre-rotation refs is unaffected.
		before = cl.CredentialRefs(
			registration_id="reg_stable",
			credential_id="cred_old",
			expires_at=_NOW + timedelta(days=1),
		)
		cl.apply_rotation(before, new_credential_id="cred_new", new_expires_at=_NOW)
		assert before.credential_id == "cred_old"
		assert before.expires_at == _NOW + timedelta(days=1)
