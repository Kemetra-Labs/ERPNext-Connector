# Decision: Connector Credential Lifecycle (registration-linked, proactively-warned)

**Spec**: 007 — Connector Admin Counterpart (D9+D10) | **Status**: 🟢 SIGNED — codeable subset implemented (#34/#35/#36) | **Date**: 2026-06-12 | **Signed**: Ahmed Shaaban
**Supersedes**: the *reactive-only* credential model in [data-pulse-auth-and-api-policy.md §8](./data-pulse-auth-and-api-policy.md) (003)
**Closes**: [connector-token-scope.md](./connector-token-scope.md) — "awaiting DP2 delivery" (the awaited scope landed; see E-6 below)

## What changed

Spec 003 §8 recorded the connector's credential handling as **purely reactive**:

> `authenticated → token_revoked (401) → authenticated (operator re-provisions a fresh token)` …
> *"No automatic token rotation is in scope … token renewal requires explicit operator action."*

The credential was a bare, hand-edited `dp2_token` (a `Password` field in Connector Settings)
with **no link to a registration** and **no recorded lifecycle**. The only signal of a credential
problem was a 401 *after the fact*.

DP-2 has since **shipped** (018, PR #516) the operator-facing counterpart this connector was waiting
for: a `connector_registration` identity that survives credential rotation, a dedicated `connector`
bearer scope, a credential linked to the registration, and an admin surface to
register/issue/rotate/revoke/disable. The tightened `ConnectorAuthGuard` (US4) now **requires** a
presented token to be a `connector`-scoped credential linked to a non-disabled registration in its
own tenant — enforced at runtime, independently of the deferred DB consistency CHECK.

Spec 007 adopts that boundary on the connector side. The credential model is now
**registration-linked and proactively-warned**, not reactive-only.

## Evidence (the awaited delivery landed — closes connector-token-scope.md)

- **E-6.** `connector-token-scope.md` signed **Option A** (2026-06-04): *"DP2 provisions a new dedicated
  connector scope … MUST NOT reuse `dashboard_api`/`pos`/`pos_operator`,"* status "🟢 SIGNED — awaiting
  DP2 delivery," SC-001 blocked on DP-2 delivery. DP-2's `0021` migration now ships the `connector`
  scope + the admin issue/rotate/revoke surface (`connector-admin.yaml`, 018). **The awaited delivery
  has landed**; this is its connector-side adoption. The `connector-token-scope.md` dependency is
  therefore **CLOSED** (direction was already signed; the blocking delivery is now shipped).

## Decisions recorded

- **Lifecycle is registration-linked, not a bare token.** Connector Settings additively records the
  non-secret `dp2_connector_registration_id`, `dp2_credential_id`, `dp2_credential_issued_at`,
  `dp2_credential_expires_at`. `dp2_token` stays the only `Password` (secret) field — sourced from the
  once-shown `IssuedCredential.secret`, never hand-minted. *(007 §5; #34.)*
- **Proactive expiry warning replaces reactive-only.** The connector compares `now()` to
  `dp2_credential_expires_at` and emits an operator-facing `posting.credential.expiring` ops alert
  *before* a 401. The warning lead time is operator-configurable (`dp2_credential_warn_days`, default
  14). The connector **cannot self-rotate** — the 018 admin surface is human-session-only; it warns and
  prompts only. *(007 §6 / OQ-1; #35.)*
- **The 401 re-auth handling is preserved from 003 §8.** A 401 surfaces a non-disclosing
  re-authenticate state, never exposes the raw/previous token, and is never blind-retried as transient.
  This reactive path now **coexists** with the proactive expiry path (a credential revoked before its
  expiry surfaces as a 401 while the expiry model still reports "active"). *(007 T2.2; #34.)*
- **A refused credential is a state, not a per-tick error (RT-1429).** A 401 or 403 from Backend-Core opens
  a shared *auth circuit* (`connector/auth_circuit.py`; state in the site cache, one flag for all
  pollers because they share one token). While it is open no call leaves the site except one probe per
  backoff window (60 s, doubling, capped at 15 min); a held-back tick is a clean "skipped" log line and
  charges nothing. Opening it is reported once (one `connector.auth_circuit.opened` log line and one
  Sentry event tagged `outcome=auth_failure`); the recovery is logged only (`connector.auth_circuit.closed`),
  because Sentry here is errors-only (OBS-1 §4). **The circuit is keyed on the configured credential** (its id plus a short one-way fingerprint of the token),
  so replacing the token, however it is changed, is a new key and takes effect on the next tick with no
  hook on Connector Settings (a DocType controller change would make the ERP release `restore` class,
  RT-927); `bench execute retail_tower_erpnext_connector.connector.auth_circuit_store.reset_auth_circuit`
  forces an immediate probe. A 5xx never opens or closes it. A refused ack does not lose a sale: the Posting Log
  row is written before the ack, so the re-offered item resolves to the recorded invoice. There is no
  scheduler-start hook, so the first heartbeat after a deploy is the start-up check. *(RT-1429.)*
- **Local link-invariant is advisory (warn-only), OQ-2.** When a token is configured but no
  registration ref is recorded (a legacy unlinked credential), the connector emits a
  `posting.credential.unlinked` warning — it does **not** block. An unlinked legacy token still works
  until US4 enforcement is live, and the cutover legitimately sets the token before the registration
  ref; a hard block would risk a self-inflicted outage on a working credential. *(007 OQ-2; #36.)*
- **The 012 posting-feed runtime auth is unchanged.** Opaque revocable `connectorBearer`, scope
  `connector`, pull/ack — only *how the credential is provisioned/linked/rotated* changed (018), not
  the runtime auth scheme (028 §15 / OQ-10; mTLS is the documented upgrade path, not v1). *(007 G-4.)*

## Cutover

The legacy hand-edited token must be replaced with a registration-linked credential **before** US4
enforcement reaches the connector's environment, or it gets a breaking non-disclosing 401. The
operational sequence (register → issue-linked → reconfigure → verify → revoke-legacy) is the
[connector credential cutover runbook](../runbooks/connector-credential-cutover.md) (007 §7 / D10).

## Status of the spec-007 work

- **Implemented + merged (codeable subset):** the DocType lifecycle fields, the pure decision logic
  (expiry classification, configurable warning, 401 re-auth surfacing, rotation, link-invariant), and
  the poller wiring — #34 / #35 / #36, all locally unit-tested.
- **⏳ BENCH-VALIDATION (deferred, standing-rules §6):** `bench migrate` of the new fields and the
  poller's `frappe.logger` emit path run on a staging ERPNext v15 bench, not locally.
- **Operational (not connector code):** the cutover (T3 — human tenant-admin on the session-only admin
  surface + a live DP-2 round-trip) — see the runbook.
- **Open owner decision:** OQ-3 (where the human-operated admin-API client lives — Console per 028 §20,
  or a thin operator tool) — a cross-repo routing call, not a connector deliverable.
