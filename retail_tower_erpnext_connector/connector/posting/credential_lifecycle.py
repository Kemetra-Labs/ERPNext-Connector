# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Pure credential-lifecycle helpers (spec 007 — Connector Admin Counterpart, D9 / G-2).

Today the connector is purely *reactive* about its DP2 credential (E-5): the only signal of a
credential problem is a 401 on a live pull/ack. Spec 007 records the credential's bounded
``expires_at`` in Connector Settings (§5) so the connector can be *proactive* — compare ``now()``
to the expiry and surface an operator-facing 'credential expiring — request rotation' state BEFORE
a 401 ever fires (§6, Goal G-2).

The connector CANNOT self-rotate: the 018 admin surface that mints credentials is human-session-only
(N-5). These helpers only *evaluate* lifecycle state and *apply* a rotation the operator/admin
performed out-of-band; they never call DP2 and never mint a credential.

Design — time is INJECTED, not read from a hidden clock:
  - ``evaluate_expiry`` takes ``now`` and ``expires_at`` as explicit ``datetime`` params, so the
    warning-window boundary is deterministically testable (the ``posting/transport.py`` injected-fake
    pattern). The bench-only poller shell passes ``frappe.utils.now_datetime()``.

Secret discipline (S-1 / Gate G4): these helpers are NEVER given the raw token. They operate on the
NON-secret lifecycle fields only (registration id, credential id, expiry) — so no operator-facing
message can leak a secret.

This module imports NO frappe.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum

# OQ-1 (resolved): how far ahead to warn is OPERATOR-CONFIGURABLE via Connector Settings
# `dp2_credential_warn_days` (parsed by :func:`parse_warn_within`); 14 days when unset. The channel
# is a structured ops-log event emitted by the poller (`posting.credential.expiring`). The helper
# still takes the lead time as a parameter so the policy is explicit, not hard-coded.
DEFAULT_WARN_WITHIN = timedelta(days=14)


class ConfigError(Exception):
    """A Connector Settings lifecycle value is malformed (e.g. a negative/non-numeric warn-days) —
    fail loud rather than silently fall back, which would hide a fat-fingered setting (Principle VI)."""


def parse_warn_within(warn_days: object) -> timedelta:
    """Parse Connector Settings ``dp2_credential_warn_days`` → a :class:`timedelta` (OQ-1a).

    Blank / ``None`` / ``0`` (an unset Frappe Int) → :data:`DEFAULT_WARN_WITHIN` (the operator left
    it alone). A negative or non-numeric value is a :class:`ConfigError` — surfaced loud, never a
    silent fallback that would warn on the wrong schedule. Frappe may hand the value back as a
    string, so a numeric string is accepted.
    """
    if warn_days is None or warn_days == "":
        return DEFAULT_WARN_WITHIN
    try:
        days = int(warn_days)
    except (TypeError, ValueError):
        raise ConfigError(
            f"dp2_credential_warn_days must be a whole number of days, got {warn_days!r}"
        ) from None
    if days == 0:
        return DEFAULT_WARN_WITHIN
    if days < 0:
        raise ConfigError(f"dp2_credential_warn_days must not be negative, got {days}")
    return timedelta(days=days)


class CredentialExpiryStatus(str, Enum):  # noqa: UP042 - StrEnum changes str()/format() output (RT-923)
    """The lifecycle state derived from the recorded ``expires_at`` (§6).

    ``UNKNOWN`` is distinct from ``ACTIVE``: a legacy / not-yet-reconfigured credential has no
    recorded expiry (E-5) — we cannot *prove* it is healthy, so we do not claim ``ACTIVE`` (a
    missing expiry must never masquerade as a healthy credential — Principle VI). It simply cannot
    be warned-on proactively and falls back to the reactive 401 path.
    """

    ACTIVE = "active"
    EXPIRY_WARNING = "expiry_warning"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CredentialExpiryState:
    """The evaluated expiry state + the operator-facing surfacing decision (§6)."""

    status: CredentialExpiryStatus
    should_warn: bool
    message: str


class CredentialAuthStatus(str, Enum):  # noqa: UP042 - StrEnum changes str()/format() output (RT-923)
    """The reactive auth state derived from a live pull/ack result (003 §8).

    Independent of :class:`CredentialExpiryStatus`: a credential revoked or disabled BEFORE its
    recorded expiry (admin revoke / disabled instance — E-4) yields a 401 while ``evaluate_expiry``
    would still report ``ACTIVE``. Both signals must coexist (§6 has two distinct 401 transitions).
    """

    OK = "ok"
    AUTH_FAILED = "auth_failed"


@dataclass(frozen=True)
class CredentialAuthState:
    """The surfacing decision for a live auth result (003 §8 obligations)."""

    status: CredentialAuthStatus
    reauthenticate_required: bool
    retryable: bool
    message: str


def classify_auth_failure(*, operation: str) -> CredentialAuthState:
    """Classify a non-disclosing 401 on a live pull/ack into a re-authenticate state (003 §8, T2.2).

    Preserves the existing 003 §8 obligations exactly:
      1. surface a clear operator-facing **re-authenticate** state;
      2. never expose the raw/previous token or any credential in the message (S-1 / G-4);
      3. never treat the 401 as transient — ``retryable=False``, no blind retry.

    The 401 body is non-disclosing (it does not distinguish revoked / invalid / missing-binding —
    auth-policy §8), so the surfaced message MUST NOT invent a specific cause it cannot know. The
    classifier is given only the ``operation`` ("pull"/"ack") for the operator log — never a token.
    """
    return CredentialAuthState(
        status=CredentialAuthStatus.AUTH_FAILED,
        reauthenticate_required=True,
        retryable=False,
        message=(
            f"DP2 refused the connector credential on {operation} (401, non-disclosing) — "
            "re-authenticate: have the tenant admin re-provision / rotate the connector credential, "
            "then reconfigure Connector Settings. Not retried (a 401 is a credential problem, "
            "not a transient)."
        ),
    )


@dataclass(frozen=True)
class CredentialRefs:
    """The non-secret registration-linked credential refs recorded in Connector Settings (§5).

    Immutable: a rotation produces a NEW instance (:func:`apply_rotation`), never a mutation — so a
    caller holding the pre-rotation refs is unaffected (repo immutable-by-default style).
    """

    registration_id: str
    credential_id: str
    expires_at: datetime | None


def evaluate_expiry(
    *,
    now: datetime,
    expires_at: datetime | None,
    warn_within: timedelta = DEFAULT_WARN_WITHIN,
    credential_id: str | None = None,
) -> CredentialExpiryState:
    """Classify the credential's expiry state and decide whether to surface an operator warning.

    - no recorded ``expires_at`` → ``UNKNOWN`` (cannot warn proactively — reactive fallback);
    - ``now`` past ``expires_at`` → ``EXPIRED`` (warn — the next live call will 401, E-4);
    - within ``warn_within`` of expiry (inclusive) → ``EXPIRY_WARNING`` (warn — request rotation);
    - otherwise → ``ACTIVE`` (no warning).

    The ``message`` names the credential by its NON-secret id only — never the raw token (S-1/G-4).
    """
    cred = credential_id or "(unrecorded)"

    if expires_at is None:
        return CredentialExpiryState(
            status=CredentialExpiryStatus.UNKNOWN,
            should_warn=False,
            message=(
                f"DP2 credential {cred} has no recorded expiry — cannot warn before expiry; "
                "reconfigure Connector Settings with the issued credential's expires_at (007 §5)."
            ),
        )

    if now >= expires_at:
        return CredentialExpiryState(
            status=CredentialExpiryStatus.EXPIRED,
            should_warn=True,
            message=(
                f"DP2 credential {cred} expired at {expires_at.isoformat()} — request rotation; "
                "live pull/ack will be refused (non-disclosing 401)."
            ),
        )

    if expires_at - now <= warn_within:
        return CredentialExpiryState(
            status=CredentialExpiryStatus.EXPIRY_WARNING,
            should_warn=True,
            message=(
                f"DP2 credential {cred} expires at {expires_at.isoformat()} — request rotation "
                "from the tenant admin before it lapses (the connector cannot self-rotate)."
            ),
        )

    return CredentialExpiryState(
        status=CredentialExpiryStatus.ACTIVE,
        should_warn=False,
        message=f"DP2 credential {cred} active until {expires_at.isoformat()}.",
    )


def build_expiry_warning(
    *,
    now: datetime,
    expires_at: datetime | None,
    warn_days_raw: object,
    credential_id: str | None,
) -> dict | None:
    """Compose the per-tick credential-expiry warning payload (OQ-1b) — a TOTAL function.

    Combines :func:`parse_warn_within` + :func:`evaluate_expiry` and returns the structured
    ops-log payload the poller emits, or ``None`` when no warning should surface. It NEVER raises:
    a malformed ``warn_days_raw`` returns a distinct ``posting.credential.warn_check_failed`` payload
    instead, so an advisory warning can never abort the posting tick (best-effort). This keeps the
    branching + best-effort policy in the locally-testable layer, leaving the poller a thin shell
    (frappe-free decision, per the repo's config.py discipline).

    The payload carries only the NON-secret credential id + expiry message — never a token (S-1/G4).
    """
    try:
        warn_within = parse_warn_within(warn_days_raw)
    except ConfigError as exc:
        return {"event": "posting.credential.warn_check_failed", "detail": str(exc)}

    state = evaluate_expiry(
        now=now, expires_at=expires_at, warn_within=warn_within, credential_id=credential_id
    )
    if not state.should_warn:
        return None
    return {
        "event": "posting.credential.expiring",
        "status": state.status.value,
        "detail": state.message,
    }


def check_registration_link(*, token_present: bool, registration_id: str | None) -> dict | None:
    """Advisory local mirror of the US4 server-side link invariant (007 OQ-2) — a TOTAL function.

    The US4 guard refuses an unlinked `connector`-scoped token server-side (E-4). This surfaces the
    same condition LOCALLY as a warning: when a `dp2_token` is configured but no
    `dp2_connector_registration_id` is recorded, the credential is a legacy UNLINKED token.

    **Warn-only by design (owner-decided):** an unlinked legacy token still works until US4
    enforcement reaches the connector's environment (E-5), and during the D10 cutover the operator
    legitimately has the token set before the registration ref — so this returns a warning payload,
    never blocks (a hard skip would risk a self-inflicted outage on a working token). Returns
    ``None`` when properly linked, or when no token is set (the unconfigured state is handled by the
    base-url/token gate, not here).

    Takes only the token's PRESENCE (a bool) — never the token value (S-1 / Gate G4).
    """
    if not token_present:
        return None
    if (registration_id or "").strip():
        return None
    return {
        "event": "posting.credential.unlinked",
        "detail": (
            "DP2 token is configured but no dp2_connector_registration_id is recorded — this is a "
            "legacy unlinked credential. Register the connector instance and reconfigure with the "
            "registration ref (007 §7); it will be refused once the US4 guard enforcement is live."
        ),
    }


def apply_rotation(
    refs: CredentialRefs,
    *,
    new_credential_id: str,
    new_expires_at: datetime | None,
) -> CredentialRefs:
    """Apply an out-of-band rotation: swap credential id + expiry, KEEP the registration id (§6).

    The ``connector_registration`` is the stable per-tenant identity that survives credential
    rotation (E-1/E-3). The US4 guard resolves the active credential by token id back to that
    registration (E-4), so the registration link MUST NOT change on rotate — clearing it would
    orphan the instance. Returns a NEW :class:`CredentialRefs`; never mutates the input.
    """
    return replace(refs, credential_id=new_credential_id, expires_at=new_expires_at)
