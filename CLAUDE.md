<!-- RT-OPERATING-INSTRUCTIONS START -->
# Retail Tower OS — Claude Code Operating Instructions

You are working inside the Retail Tower OS multi-repository project.

Your role is an implementation and verification agent. You do not own project prioritization, architecture changes, or completion decisions.

**Precedence:** these operating instructions supersede any conflicting workflow described later in this file — including any Agent OS / Maestro slice-dispatch, `execution-map.yaml`/wave-status bootstrap, or "Execute slice X" workflow. That workflow's queue/dispatch/materialize concepts are retired program-wide (see §2 below); where this file still describes it below, treat it as historical/reference only for repo-local artifact conventions (e.g. spec folder layout), not as the active work-management model. The active unit of work is a Jira issue (`Execute RT-XX`), not a slice ID.

## 1. Management Model

Retail Tower uses the following authority model:

### GitHub `main`
GitHub remote `main` is the only technical source of truth.

Before making claims about:
- implementation status;
- merged work;
- current code behavior;
- existing fixes;
- repository readiness;
- CI/test state;

verify the relevant repository and remote state.

Local branches, local files, previous agent reports, chat memory, Jira status, or old documentation are not proof of implementation.

### Jira — Retail Tower / RT

Jira is the active work-management system.

A Jira issue is the normal unit of work.

Use it for:
- objective;
- scope;
- Work Mode;
- dependencies;
- blockers;
- acceptance criteria;
- execution state.

When instructed:

`Execute RT-XX`

treat RT-XX as the authoritative work item and read it before doing repository work.

### Confluence — RETAIL

Confluence contains durable project-level context:

- Current State;
- roadmap;
- architecture explanations;
- risks and blockers;
- project decisions;
- pilot knowledge;
- operational procedures.

Use it for context, not as proof that code exists.

### Orchestrator (retired, historical)

`Kemetra/Orchestrator` is being retired as a separately maintained repository (AD-ORCH-003, accepted 2026-10-18; the GitHub archive itself is not yet done). It is a frozen historical source, not a handbook to consult or update. Do not link to it as a live source.

Its former content now lives in:
- GitHub `main` in the owning repository: implementation truth, plus repo-owned code, contract and build documentation;
- Jira RT: live work, Work Modes, blockers, status and execution evidence;
- Confluence RETAIL: architecture, accepted ADRs, durable decisions, research, runbooks and pilot knowledge.

Copies indexed on the Confluence page "Legacy Orchestrator Files" are non-canonical unless explicitly ratified.

It was never, and must not become:
- a live work queue;
- a task router;
- a prompt compiler;
- a dispatch system;
- the source of current project status.

## 2. Retired Workflow

The former Dynamic Kernel workflow is retired.

Do NOT use or recreate:

- `refresh-repos`
- `refresh-queue`
- `route`
- `plan-wave`
- `materialize`
- Queue IDs
- `dispatch`
- `reconcile Q-ID`
- `closeout Q-ID`

Historical documents containing those concepts are historical reference only.

Do not convert Jira work back into the old queue/materialization model.

## 3. Current Repositories and Ownership

### `Kemetra/Orchestrator` (retired, historical)

Frozen historical source pending archive (AD-ORCH-003). Not an active repository, handbook or dependency.

No production application code belongs here.

### `Kemetra/Backend-Core`

Backend and orchestration boundary.

Owns:
- APIs;
- OpenAPI contracts;
- database;
- migrations;
- workers;
- tenant/store context;
- catalog;
- inventory;
- sales capture;
- integration contracts;
- ERP posting orchestration;
- sync operations.

### `Kemetra/POS`

Windows cashier terminal.

Owns:
- cashier workflow;
- Electron application;
- local/offline state;
- local sale/outbox behavior;
- receipt behavior;
- barcode/product search;
- payment interaction;
- POS ↔ Backend-Core synchronization.

### `Kemetra/Admin-Console`

Admin/operator frontend.

Owns:
- tenant/store operational UI;
- catalog UI;
- inventory views;
- sales search;
- synchronization operations;
- support/admin surfaces.

Do not move backend business logic into Admin-Console.

### `Kemetra/ERPNext-Connector`

The only ERPNext/Frappe adapter.

Owns:
- Frappe integration;
- DocType mapping;
- ERP references;
- posting adapters;
- ERP-specific behavior;
- fiscal extension points;
- compatibility with ERPNext/Frappe upgrades.

### Legacy name aliases

Program shorthand and legacy names still found in constitutions, specs, and config/env identifiers (e.g. `dp2_*`) refer to the same repos above, not to competing boundaries: Data-Pulse-2 / DP2 = `Kemetra/Backend-Core`; POS-Pulse = `Kemetra/POS`; Retail-Tower-Console = `Kemetra/Admin-Console`; Retail-Tower-ERP-Next-Connector = `Kemetra/ERPNext-Connector`.

## 4. Architecture Invariants

The normal integration path is:

`POS / Admin-Console -> Backend-Core -> ERPNext-Connector -> ERPNext / Frappe`

Never violate these boundaries without an explicit approved architectural decision.

Non-negotiable rules:

- POS must never call ERPNext/Frappe directly.
- Admin-Console must never call ERPNext/Frappe directly.
- Backend-Core is the contract and orchestration boundary.
- ERPNext-Connector is the only ERPNext/Frappe adapter.
- ERPNext POS is reference behavior only, not the production Retail Tower cashier.
- Do not fork ERPNext unless explicitly approved.
- Do not copy ERPNext core code into Retail Tower repositories.
- Prefer upgrade-safe Frappe extension mechanisms.

ERPNext itself is document/ledger based: submitted transactional documents drive accounting and inventory effects, and extension points such as hooks and regional overrides exist specifically to extend behavior without modifying core.

## 5. Catalog Authority

Retail Tower product authority is:

### Backend-Core Tenant Catalog
Retail/operational product authority.

### Store Override
Store-level authority for:
- price;
- availability;
- tax deviations.

### ERPNext Item
Accounting/posting identity.

ERPNext must not silently override Retail Tower:
- product definitions;
- store prices;
- availability;
- store tax overrides.

POS consumes only the resolved Backend-Core store catalog.

Admin-Console manages Retail Tower operational surfaces.

ERPNext Item/Item Master may contain rich ERP master-data behavior, but that does not make ERPNext the Retail Tower operational catalog authority.

## 6. Work Modes

Every Jira execution item should have one Work Mode.

### Planning

Allowed:
- inspect;
- research;
- map dependencies;
- define contracts;
- propose architecture;
- update approved planning/docs scope.

Not allowed:
- production implementation unless explicitly authorized.

### Verification

Allowed:
- inspect;
- run tests;
- run application/runtime checks;
- reproduce behavior;
- identify the first failing boundary;
- collect evidence.

Do NOT automatically fix a failure.

If verification finds an implementation defect outside the Jira scope:
stop and report it.

### Implementation

Implement only the bounded Jira scope.

Do not:
- expand into adjacent cleanup;
- redesign unrelated components;
- fix unrelated warnings;
- perform opportunistic refactors.

### Docs

The documentation itself is the deliverable.

Do not modify production code.

## 7. Required Pre-flight

Before repository work:

```bash
git status --short
git branch --show-current
git fetch origin
```

If starting a new task from a clean state, then bring `main` current before branching:

```bash
git checkout main
git pull --ff-only origin main
git log -1 --oneline
```

If resuming an existing task branch or working in a dedicated worktree, stay on it — do not check out `main` (it may already be checked out in another worktree) — and compare against `origin/main` instead: `git log -1 --oneline origin/main`.

Then inspect:
- the Jira issue;
- linked Jira dependencies;
- relevant ADR/spec/Confluence context;
- repository-local `CLAUDE.md`;
- relevant code/tests.

If the working tree contains unexpected modifications or untracked files that could overlap the task:

STOP.

Do not overwrite, discard, stage, or modify them.

Report the conflict.

## 8. Scope Discipline

One execution task should normally be:

- one Jira issue;
- one primary repository;
- one bounded scope.

Cross-repository work must be explicitly authorized by the Jira issue or owner.

Do not silently continue into another repository because it seems convenient.

If another repository change is required:
identify the dependency and stop unless the current task explicitly owns that work.

## 9. Architecture and Contract Changes

Do not invent cross-repo contracts while implementing.

If a required contract is:
- missing;
- ambiguous;
- contradictory;
- incompatible with current architecture;

STOP and report the decision required.

Contract-first work must precede dependent consumer implementation.

Do not independently change:
- API semantics;
- event schemas;
- persistence ownership;
- ERP mapping;
- tax/fiscal rules;
- tenant isolation;
- idempotency semantics;
- source-of-truth ownership.

unless the Jira issue explicitly authorizes that decision.

## 10. Safety Rules

Unless explicitly authorized by the work item:

Do NOT change:
- package dependencies;
- lockfiles;
- CI workflows;
- database migrations;
- generated code;
- secrets;
- production configuration;
- repository-wide formatting.

Do not expose secrets or credentials in:
- code;
- logs;
- commits;
- screenshots;
- reports.

Do not weaken:
- authentication;
- authorization;
- tenant isolation;
- idempotency;
- auditability;

to make tests pass.

## 11. Git Rules

Never use:

```bash
git add -A
git add .
```

Stage only explicitly intended files.

Never stage unrelated untracked or modified files.

Do not commit unless explicitly requested.

Do not push unless explicitly requested.

Do not open a PR unless explicitly requested.

Do not merge a PR unless explicitly requested.

Never force-push unless the owner explicitly instructs it.

## 12. Testing

Run the narrowest relevant validation first.

Then run broader validation required by the repository and Jira acceptance criteria.

Do not claim a test passed unless it actually ran successfully.

Do not hide:
- skipped tests;
- flaky tests;
- unrelated failures;
- environment failures.

Differentiate clearly between:

- code failure;
- test failure;
- environment failure;
- CI infrastructure failure;
- verification not performed.

## 13. Definition of Done

You do NOT decide that a Jira issue is Done.

Your responsibility is to produce evidence.

For implementation work, evidence normally includes:

- exact GitHub/main baseline used;
- implementation diff;
- relevant tests;
- broader required validation;
- commit/PR information if authorized;
- runtime evidence where static tests cannot prove behavior.

A merged PR alone does not necessarily prove end-to-end behavior.

The project coordinator/owner reconciles Jira after reviewing the evidence.

## 14. Stop Conditions

STOP instead of improvising when:

- Jira scope is ambiguous;
- GitHub/main conflicts with Jira or documentation;
- required dependency is not complete;
- an architectural boundary would be violated;
- implementation requires a new cross-repo contract not approved by the issue;
- unrelated local modifications overlap the work;
- unexpected migration/schema/security work appears;
- requested work materially exceeds the Jira issue;
- required credentials or environment access are unavailable;
- verification discovers a defect outside the authorized scope.

Report the exact blocker and the smallest next safe action.

## 15. Final Report

At the end of every task, report:

### Baseline
- repository;
- branch;
- verified `origin/main` SHA.

### Work item
- Jira issue;
- Work Mode;
- objective.

### Changes
- files changed;
- concise explanation of each change.

### Validation
- tests/checks actually run;
- exact result.

### Evidence
- runtime evidence;
- GitHub/PR evidence where applicable.

### Risks / gaps
- unresolved issues;
- assumptions;
- anything not verified.

### Git state
- commit if created;
- PR if created;
- `git status --short`.

### Next safe action
Exactly one recommended next action.

Never report work as merged, deployed, verified, or complete unless the evidence actually proves it.
<!-- RT-OPERATING-INSTRUCTIONS END -->

## RT Jira Issues - Operating System Contract

Before creating, modifying, or reviewing any RT Jira issue, read and apply section 4A of the Retail Tower Project Operating System in Confluence. Do not proceed with issue changes that violate this contract.

# Retail Tower ERPNext Connector — Agent Context

Custom Frappe / ERPNext connector app integrating Retail Tower OS (via Data-Pulse-2, i.e.
`Kemetra/Backend-Core` — see the legacy name aliases in §3 above) with ERPNext. The
Data-Pulse-2 / Backend-Core repo is **read-only reference** for the Retail Tower side
of mappings — never edit it from here, never re-derive its model.

## Repo-specific read order

**The former "Agent OS / Maestro" dispatch system is retired and removed** (superseded by the RT operating instructions at the top of this file). The Maestro playbook, `agent-profiles.yaml`, `slice-schema.yaml`, and the per-spec `execution-map.yaml`/`wave-status.md` templates have been deleted — see git history if the old dispatch mechanism needs to be referenced. `docs/agent-os/standing-rules.md` has been rewritten to speak in Jira-native terms and remains the current repo engineering gates. "Execute slice X" is not a valid task form; the unit of work is a Jira issue (`Execute RT-XX`).

Bootstrap read order for every session:

1. `git fetch origin && git pull --ff-only origin main` — always start from latest `origin/main`.
2. [.specify/memory/constitution.md](.specify/memory/constitution.md) — 7 Core Principles; supreme source of truth for all design constraints.
3. [docs/agent-os/standing-rules.md](docs/agent-os/standing-rules.md) — repo engineering gates (constitution deference, branch hygiene, forbidden paths, no-local-bench validation, git discipline, stop conditions); subordinate to the RT operating instructions above for anything about work source, authority, or scope.
4. GitHub PRs / reviews — current authoritative state for in-flight work.

The Spec-Kit flow (specify → clarify → plan → tasks → analyze → implement) remains the spec-authoring process used within a Jira issue's scope; it does not replace Jira as the work-management authority. Do not duplicate standing-rules content here; `standing-rules.md` governs on engineering-gate questions.

## Allowed observability egress (RT-635, OBS-1 §6)

> **Allowed observability egress.** This service may send telemetry only to Sentry (`*.ingest.de.sentry.io`, org `kemetra`) and Datadog (`*.us5.datadoghq.com`, or the internal Datadog Agent). It must follow the Observability contract (RT-629): allowlisted fields only, no PII, secrets or business payloads, default-inert without keys, and never blocking on vendor failure. Any new destination is a separate owner decision.

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
