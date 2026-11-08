> Program-wide operating instructions (Retail Tower OS §1–15 and the RT-948 push/merge rules) live in the root [CLAUDE.md](../../CLAUDE.md), which always loads. Only this product's additions are kept here.


# Retail Tower ERPNext Connector — Agent Context

Custom Frappe / ERPNext connector app integrating Retail Tower OS (via Data-Pulse-2, i.e.
`Kemetra/Backend-Core` — see the legacy name aliases in §3 of the root CLAUDE.md) with ERPNext. The
Data-Pulse-2 / Backend-Core repo is **read-only reference** for the Retail Tower side
of mappings — never edit it from here, never re-derive its model.

## Repo-specific read order

**The former "Agent OS / Maestro" dispatch system is retired and removed** (superseded by the RT operating instructions in the root CLAUDE.md). The Maestro playbook, `agent-profiles.yaml`, `slice-schema.yaml`, and the per-spec `execution-map.yaml`/`wave-status.md` templates have been deleted — see git history if the old dispatch mechanism needs to be referenced. `docs/agent-os/standing-rules.md` has been rewritten to speak in Jira-native terms and remains the current repo engineering gates. "Execute slice X" is not a valid task form; the unit of work is a Jira issue (`Execute RT-XX`).

Bootstrap read order for every session:

1. `git fetch origin`; branch a new task from current `origin/main`, but on an existing task branch or worktree stay on it and compare with `origin/main` (root CLAUDE.md §7).
2. [.specify/memory/constitution.md](.specify/memory/constitution.md) — 7 Core Principles; supreme source of truth for all design constraints.
3. [docs/agent-os/standing-rules.md](docs/agent-os/standing-rules.md) — repo engineering gates (constitution deference, branch hygiene, forbidden paths, no-local-bench validation, git discipline, stop conditions); subordinate to the RT operating instructions in the root CLAUDE.md for anything about work source, authority, or scope.
4. GitHub PRs / reviews — current authoritative state for in-flight work.

The Spec-Kit flow (specify → clarify → plan → tasks → analyze → implement) remains the spec-authoring process used within a Jira issue's scope; it does not replace Jira as the work-management authority. Do not duplicate standing-rules content here; `standing-rules.md` governs on engineering-gate questions.

## Allowed observability egress (RT-635, OBS-1 §6)

> **Allowed observability egress.** This service may send telemetry only to Sentry (`*.ingest.de.sentry.io`, org `kemetra`) and Datadog (`*.datadoghq.eu`, the EU1 org, per the owner decision of 2026-10-19 that supersedes D5; `*.us5.datadoghq.com` stays allowed only until the RT-736 cutover is verified and as rollback; or the internal Datadog Agent). It must follow the Observability contract (RT-629): allowlisted fields only, no PII, secrets or business payloads, default-inert without keys, and never blocking on vendor failure. Any new destination is a separate owner decision.

In this repo that is `connector/observability/` only: a Connector-owned Sentry `Hub(Client(...))`, errors only, inert unless `RT_CONNECTOR_SENTRY_DSN` is set (`*.ingest.de.sentry.io` DSNs only), tagged `service=erpnext-connector`, `env` (`DEPLOYMENT_ENVIRONMENT`), `version`/`release` (`RT_CONNECTOR_RELEASE`, SHA12) and the tick `request_id`. Never call `sentry_sdk.init()`, never bind a client to the global hub, and never enable Frappe's built-in integration (`FRAPPE_SENTRY_DSN`, `enable_telemetry`): it reports through the global hub with request bodies.

<!-- SPECKIT START -->
For additional context about technologies to be used, project structure,
shell commands, and other important information, read the current plan:
`specs/006-sales-posting-adapter/plan.md`

Current main state: specs 001-003 are done. Spec 004's product-mapping policy remains historical
context where relevant, but later spec 006 implementation now carries the active sales-posting
runtime evidence. Spec 006 sales-posting adapter / poller activation / live-flow work has landed:
`hooks.py` registers `retail_tower_erpnext_connector.connector.posting.poller.run_posting_poll`
under `scheduler_events`, and the poller reads Connector Settings for `dp2_base_url`, `dp2_token`,
the UOM map, warehouse map, and store-customer map. Use
`specs/006-sales-posting-adapter/wave-status.md` plus GitHub/main as the current evidence.

Data-Pulse-2 / Backend-Core repo (authoritative reference, read-only):
`C:\Users\user\Documents\GitHub\Backend-Core`.
<!-- SPECKIT END -->
