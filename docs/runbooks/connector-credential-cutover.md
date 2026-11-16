# Runbook: Connector Credential Cutover (legacy token → registration-linked credential)

**Spec**: 007 — Connector Admin Counterpart (D10) | **Implements**: spec §7 cutover | **Date**: 2026-06-12

Operational procedure to migrate the connector from a legacy **hand-edited unlinked `dp2_token`** to a
**registration-linked, `connector`-scoped credential** issued via DP-2's 018 admin surface — with **no
lockout window**. See [`../decisions/connector-credential-lifecycle.md`](../decisions/connector-credential-lifecycle.md)
for the why.

> **Timing constraint (the whole reason this runbook exists).** DP-2's tightened US4
> `ConnectorAuthGuard` requires the presented token to be a `connector`-scoped credential linked to a
> non-disabled registration, enforced **at runtime, independently of the deferred DB consistency
> CHECK**. A legacy unlinked token works only until that enforcement reaches the connector's
> environment — after that it gets a breaking **non-disclosing 401**. **This entire cutover MUST
> complete before US4 enforcement is live in the connector's environment.** Confirm the rollout timing
> with the DP-2 team first.

---

## Roles

- **Tenant admin (human)** — operates the session-only 018 admin surface (`connector-admin.yaml` is
  `cookieAuth`-only; a machine bearer is rejected). Drives register / issue / revoke.
- **Connector operator / ops** — runs `bench migrate`, configures Connector Settings, coordinates the window.

The connector machine **never calls the admin surface** — it only *consumes* the issued credential.

## Prerequisites

- DP-2 `origin/main` carries the shipped 018 surface (`connector-admin.yaml`) + migration `0021`
  (the `connector` scope, `connector_registration`, the US4 guard) — re-confirm at cutover time.
- The connector app is deployed at the 007 level (#34/#35/#36 merged) so Connector Settings has the
  lifecycle fields (`dp2_connector_registration_id`, `dp2_credential_id`, `dp2_credential_expires_at`,
  `dp2_credential_warn_days`). Run `bench migrate` on the site if not already applied (additive,
  backward-compatible — the existing `dp2_token`/`dp2_base_url` are untouched).
- A maintenance window agreed with the DP-2 rollout of US4 enforcement (see the timing constraint).

## Procedure (spec §7 — order is load-bearing)

1. **Register the connector instance** *(tenant admin)* — call `tenantAdminRegisterConnectorInstance`
   for the correct `(environment, erpnext_site_ref)`. At most one active registration per tuple
   (the `0021` unique constraint). Record the returned `connector_registration` id.
2. **Issue a linked credential** *(tenant admin)* — call `tenantAdminIssueConnectorCredential` for that
   registration. Capture, from the response shown **exactly once**: the raw `secret`, the
   `credential_id`, and the bounded `expires_at`. **Never log, paste into chat, or screenshot the raw
   secret** (Gate G4 / S-1).
3. **Reconfigure the connector** *(operator)* — in **Connector Settings**, set:
   - `dp2_token` ← the new raw `secret` (Password field),
   - `dp2_connector_registration_id` ← the registration id from step 1,
   - `dp2_credential_id` ← the `credential_id` from step 2,
   - `dp2_credential_expires_at` ← the `expires_at` from step 2,
   - (optional) `dp2_credential_issued_at`, and `dp2_credential_warn_days` if not the default 14.

   Once the registration ref is set, the connector's `posting.credential.unlinked` advisory clears.
4. **Verify under the new credential** *(operator)* — confirm a live
   `connectorPullPostings` + `connectorAckOutcome` round-trip succeeds — i.e. it passes the tightened
   US4 guard (`findActiveConnectorCredentialByTokenId` resolves the linked instance). A poller tick
   that pulls + acks without a 401 is the proof. **Do not proceed to step 5 until this passes.**
5. **Revoke the legacy token** *(tenant admin)* — call `tenantAdminRevokeConnectorCredential` for the
   old hand-edited credential, **only after** step 4 verifies the new one. This ordering guarantees no
   lockout window (the old token stays valid until the new one is proven).

## Rollback

If step 4 verification fails, the **legacy token is not yet revoked** (step 5 is gated on step 4) — the
connector keeps operating on the old token until US4 enforcement forces the issue. Re-check the
Connector Settings values (especially that `dp2_token` is the new secret and the registration/credential
ids match what 018 issued), then re-attempt from step 3. If the new credential cannot be made to verify
before US4 enforcement goes live, escalate to the DP-2 team to hold the enforcement rollout.

## Post-cutover: operating the credential (RT-1434)

After the cutover the credential has a lifecycle that **the system operates**. Nobody handles a raw
secret, except in the rehearsed last resort below. The why is in the RT-1432 decision record
([Confluence](https://rahmaqanater.atlassian.net/wiki/spaces/RETAIL/pages/79790082), decisions D1–D8)
and in RT-1428, the incident it answers: a hand-issued 30-day token expired with no warning and no
renewal path.

**The secret rule (RT-1352 PD7) applies to every path.** A raw secret never goes on a command line,
in an environment variable, in a log, in a file, in chat, in a ticket or in a screenshot. A rotation
replaces `dp2_token`, `dp2_credential_id` and `dp2_credential_expires_at`. It leaves
`dp2_connector_registration_id` unchanged, because the registration outlives its rotations.

> **Status (2026-11-16).** Paths 1 and 2 depend on work that has not shipped yet: managed registrations
> (RT-1358, merged), the provisioner runtime (RT-1359, RT-1360), automatic and forced rotation (RT-1362),
> the lab conversion (RT-1433) and the server-side expiry gauge (RT-1400 S2). Until they are live **and**
> rehearsed in the RT-1435 drill, path 3 is the only working path. The lab credential is valid until
> 2027-02-14. Its warning fires on 2027-01-31, and the fallback deadline is 2027-01-15.

### 1. Normal operation: automatic rotation (no step)

A managed registration (`managed_by` set, RT-1358) rotates itself 14 days before expiry. The
provisioner calls the rotate operation and writes the new secret straight into Connector Settings
(RT-1362). The secret never passes through a person.

Watch these signals; none of them asks for an action while it stays green:

| Signal | Where | Means |
|---|---|---|
| Expiry gauge | Backend-Core metrics (RT-1400 S2) | Days left on the active credential. It is measured on the server, so a hand-typed date cannot mislead it. |
| `connector.auth_circuit.opened` + Sentry `outcome=auth_failure` | Connector log; Sentry project `erpnext-connector`, alert 1502909 | The Connector received a 401 or 403 on its credential. A 401 means Backend-Core refused the credential. A 403 can also come from an edge or origin rule, so confirm it came from Backend-Core first (see `403 origin_rejected` below). Then go to path 2. |
| `posting.credential.expiring` | Connector log, `dp2_credential_warn_days` ahead | The warning fires on every tick from the same 14-day point at which automatic rotation starts, and it does not check rotation status. Check whether a rotation is pending or running first. Go to path 2 only if it has not completed. |
| Connector stale rule | Backend-Core, 5 min without a heartbeat | The Connector is silent. Check the circuit before anything else. |

### 2. Emergency: forced rotation (credential refused, leaked or about to expire)

1. A platform admin triggers **rotate now** for the registration (RT-1362). The trigger is audited,
   allowed only for a managed registration, and protected against repeats.
2. The provisioner rotates and writes the new secret into Connector Settings. Nobody copies anything.
3. Confirm the recovery, in this order:
   - the next tick calls Backend-Core at once. A new token is a new circuit key, so no refusal is on
     record for it. `connector.auth_circuit.closed` is **not** logged on this path: it appears only when
     a probe succeeds under the same key after a refusal. Do not wait for it or repeat steps because
     it is missing. If the old token's record is still holding ticks back,
     `bench execute retail_tower_erpnext_connector.connector.auth_circuit_store.reset_auth_circuit`
     clears it;
   - the heartbeat is accepted and the stale rule clears;
   - Sentry gets no new `outcome=auth_failure` event.
4. For a **leak**, also confirm in the audit log that the old credential is revoked. Rotation revokes it
   atomically.

### 3. Last resort: human rotation (only when the provisioner is unavailable)

Use this only when path 2 cannot run: the provisioner is down or not deployed, or the registration is
not yet managed. The secret moves **only through the clipboard**, from the browser tab that receives it
into the Connector Settings password field.

**Prerequisites**

- An admin who is signed in to the Admin Console. The connector-admin operations take only a human
  cookie session; a machine bearer is refused. For a **managed** registration (`managed_by` set,
  RT-1358) the admin must be a **platform admin**; an owner or tenant admin is refused with a 404.
  A tenant admin is enough only for a registration that is not yet managed.
- The AWS Session Manager plugin, installed locally, and the `rt-readonly` AWS profile. The browser
  console of Session Manager cannot forward ports.
- The registration id (`dp2_connector_registration_id` in Connector Settings).

**Steps**

1. **Open the ERPNext desk through a port-forward.** On lab-03 the desk is bound to
   `127.0.0.1:18000` only, so it has no public link:

   ```bash
   aws ssm start-session --profile rt-readonly --target mi-0757780c152fd920a --document-name AWS-StartPortForwardingSession --parameters portNumber=18000,localPortNumber=18000
   ```

   Open `http://lab.localhost:18000/app/connector-settings`. The `lab.localhost` host name picks the
   Frappe site. Sign in, but do not edit yet.
2. **Rotate from the signed-in Admin Console tab.** Open its developer console. The call is
   same-origin and carries the session cookie. Keep the response in a page variable, never print it:

   ```js
   const REGISTRATION_ID = "<registration id>"; // replace the placeholder with the id, keep the quotes
   const r = await fetch(`/api/v1/connector/instances/${REGISTRATION_ID}/credentials/rotate`, {
     method: "POST", credentials: "include",
     headers: { "Idempotency-Key": crypto.randomUUID() },
   });
   window.__s = r.ok ? await r.json() : null;
   console.log(r.status, window.__s && window.__s.credential_id, window.__s && window.__s.expires_at);
   ```

   A 201 returns the secret **once** and revokes the old
   credential at the same moment, so the Connector is refused from now until step 4 is done.
3. **Copy the secret to the clipboard.** Run this at the top level of the console, never inside
   `.then`, because `copy()` is not defined there and the secret would be lost:

   ```js
   copy(window.__s.secret); delete window.__s;
   ```

4. **Paste into Connector Settings** in the desk tab: `dp2_token` (a Password field) ← the clipboard.
   Then type `dp2_credential_id` and `dp2_credential_expires_at` from the step 2 log line, which
   holds no secret, and save.
5. **Clear the clipboard.** Copy any other text, then clear the Windows clipboard history (Win+V →
   Clear all) if it is on. Close the console.
6. **Confirm** as in path 2, step 3, and record the credential id, the expiry, the time and who did it
   on the Jira issue. Never record the secret.

**If something goes wrong**

- **The secret was lost** (closed tab, failed copy): rotate again with a **new** `Idempotency-Key`. A
  retry with the same key returns `409 idempotency_key_conflict` and no secret. The lost credential is
  revoked by the new rotation.
- **401 or 404 on the rotate call:** the session is not a human admin session, or the id is wrong or
  out of scope, or the registration is managed and the signed-in admin is not a platform admin. All of
  these answers are non-disclosing, so check the admin role before assuming the id is wrong.
- **403 `origin_rejected`:** the call did not come from an allowed origin. Run it from the Admin
  Console tab, not from another site.

Never fall back to an environment variable, a `bench` command or a file to move the secret, as
happened in the RT-1428 recovery on 2026-11-16.

### 4. Rehearsal

Do not rely on a path before the lab drill RT-1435 has exercised it. The drill covers automatic
rotation, forced rotation, refusal plus alert, and this last resort. Link its evidence here when it
runs. If the drill finds that a step differs from this runbook, change the runbook (RT-1434 AC2).
