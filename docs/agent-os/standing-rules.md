# Retail Tower ERPNext Connector — Repo Engineering Gates

> These rules apply to **every** agent action in this repo. They are not
> negotiable defaults — they are hard contracts.
>
> **These gates layer on top of the RT operating instructions in `CLAUDE.md`
> — they do not replace them.** The RT operating instructions in `CLAUDE.md`
> govern work source and authority (Jira RT is the work-management system;
> a Jira issue, `Execute RT-XX`, is the unit of work). This file governs
> repo-local engineering gates: what surfaces are forbidden, what git
> operations are never autonomous, and what stops an agent mid-task.
>
> The project **constitution** (`.specify/memory/constitution.md`, Principles
> I–VII) binds every Jira issue. The **Spec-Kit** flow (`specs/NNN/{spec,plan,tasks}.md`)
> remains the spec-authoring process used within a Jira issue's scope. Where
> these rules and the constitution overlap, the constitution wins.
>
> **The former "Agent OS v1" dispatch system (Maestro orchestrator, slice
> briefs, `execution-map.yaml`, `wave-status.md`) is retired.** It is not
> part of any active workflow. See `CLAUDE.md` for the current operating
> model.

---

## 0. Constitution is supreme

Every Jira issue MUST comply with `.specify/memory/constitution.md`:

- **I — Data-Pulse-2 is the only orchestration boundary.** Cite DP2 (i.e.
  `Kemetra/Backend-Core`); never re-derive its model. No path bypasses DP2.
- **II — No ERPNext fork.** Never copy ERPNext/Frappe core into the app;
  address ERPNext only via generic `{doctype, name}`.
- **III — Additive & upgrade-safe.** Migrations ship with rollback notes;
  version pinning is documented; staging-before-production.
- **IV — Idempotent mutations.** (Applies once mutation work begins, spec 006+.)
- **V — Observable failures.** Structured logs + correlation IDs (spec 003+).
- **VI — Fiscal & stock truth is never hidden.** Record uncertainty as explicit
  status, never silently omit.
- **VII — Spec-driven, contract-first.** No business mutation before the
  relevant contract spec is reviewed; ambiguous mappings gate downstream specs.

A Jira issue that conflicts with a constitution principle is a hard stop —
adjust the issue's scope, never dilute the principle (the constitution
changes only via an explicit, separate constitution update).

## 1. Branch hygiene

- **Always start from latest `origin/main`.** `git fetch origin && git pull --ff-only origin main`
  before creating any branch or worktree. If local `main` is dirty or cannot
  fast-forward, stop and report.
- **One worktree per task.** Worktrees live under
  `C:\Users\user\Documents\GitHub\connector-<short-slug>`. Never reuse a
  worktree across unrelated Jira issues.
- **Branch names match task intent:** `feat/`, `fix/`, `test/`, `docs/`,
  `chore/`, `refactor/`, `perf/`, `ci/`. Include the spec ID or task ID when
  one applies (e.g. `docs/002-doctype-mapping-reference`).

## 2. Scope discipline

- **Small, reviewable changes.** One concern per branch. If a Jira issue's
  scope spans more than ~3 logically related files, ask whether it should be
  split.
- **No combining unrelated work.** A docs change does not also edit app code.
- **RED-then-GREEN where applicable.** Where a bug fix has a non-trivial proof,
  ship a failing test commit then a fix commit. (For this repo, test execution
  is a deferred staging step — see §6.)
- **Reference Spec-Kit task IDs** when a Jira issue implements a `tasks.md`
  entry (e.g. T012).

## 3. Forbidden surfaces — gates required

The Jira issue's stated scope defines what this task may touch. By default,
the following are **forbidden** and require explicit authorization stated in
the Jira issue (or given directly by the owner) before any tool call:

- `.specify/**` — the constitution, Spec-Kit templates, and `.specify` config.
  **Highest sensitivity:** the constitution governs everything else.
- `retail_tower_erpnext_connector/hooks.py` — app metadata + hook registrations
  (a mutation hook here would breach Principle VII).
- `retail_tower_erpnext_connector/**/doctype/**/*.json` — DocType definitions
  (structural schema; `issingle`, fields, permissions).
- `pyproject.toml` — packaging + version pin (Principle III).
- `retail_tower_erpnext_connector/patches.txt` and any future migration/patch.
- `.github/**` — CI workflows (none exist yet; adding any is gated).
- **Merged spec artifacts** under `specs/NNN/{spec,plan,tasks,research,data-model,quickstart}.md`
  once their PR has merged — these are the reviewed record. Re-opening them
  requires a fresh Jira issue or a constitution-style amendment, not a silent
  edit.

When a task legitimately needs one of these, the Jira issue states the exact
path and the user or owner has approved before any tool call.

## 4. Out-of-scope surfaces — never touch unless requested

From the README non-goals — **not active work**, never modified unless the
user explicitly asks:

- **Do not fork ERPNext** or copy ERPNext/Frappe core code into this repo.
- **POS-Pulse cashier UI** (separate repo, `Kemetra/POS` — never implemented here).
- **Retail-Tower-Console backend logic** (separate repo, `Kemetra/Admin-Console`).
- **Direct Frappe calls from POS-Pulse** (must flow through Data-Pulse-2 /
  `Kemetra/Backend-Core`).
- **The Data-Pulse-2 / Backend-Core repo itself** — it is read-only reference
  for the Retail Tower side of mappings. Never edit it from this repo.

If a Jira issue implies touching one of these, stop and confirm before proceeding.

## 5. Git operations — never autonomous

- **Never `git add -A` or `git add .`.** Always stage by exact path. (`-A`
  has historically swept in secrets and untracked artifacts.)
- **Never commit, push, open a PR, or merge without explicit instruction.**
  Asking once for a session does not authorize a second commit. Asking for
  commit does not authorize push. Asking for push does not authorize PR.
- **Never `--no-verify`, `--no-gpg-sign`, or force-push to `main`.** If a hook
  fails, fix the underlying issue and create a new commit. (Force-push to a
  feature branch also requires explicit authorization — it rewrites remote
  history.)
- **Never amend a published commit.** Always make a new commit.
- **Commit subjects** follow `<type>: <description>` and end with the
  `Co-Authored-By` trailer per the repo's git convention.

## 6. Validation contract — no local bench

Every Jira issue's acceptance criteria define what must pass. The agent runs
the checks, reports results verbatim, and **does not declare GREEN without
empirical evidence**.

**This machine has no local Frappe bench.** Validation therefore splits:

- **Local checks (run now):** `python -m py_compile`, JSON validity
  (`json.load`), TOML validity (`tomllib`), `ruff` if configured, the
  forbidden-path audit, `git diff --check`, and — for docs changes — DP2
  citation-locates-in-repo checks and acceptance-scenario review.
- **Bench-validation (deferred to a staging ERPNext v16 site, the only supported line):**
  `bench install-app`, `bench migrate`, `bench run-tests`. These are marked
  `⏳ BENCH-VALIDATION` in `tasks.md` and the Jira issue, NOT run locally, and
  NOT claimed as passing until executed on a real bench. (This is the pattern
  established in spec 001's tasks.md.)

Do not weaken a test or add a default-skip to make red turn green. If a check
cannot run locally, mark it deferred — never fake it.

## 7. Stop conditions

The agent stops and reports — does not silently work around — when:

- The working tree is dirty in unexpected ways (files outside the Jira
  issue's stated scope).
- A required input (file, helper, prior commit, DP2 citation path) does not exist.
- The task implies touching a forbidden surface (§3) without explicit
  authorization stated in the Jira issue or by the owner.
- The task would conflict with a constitution principle (§0).
- Validation produces a result that doesn't match the issue's acceptance criteria.
- A claimed predecessor (e.g. "builds on PR #X") is not actually merged/accessible.
- Anything about the Jira issue is ambiguous about whether authorization for
  a gated surface is open — this is also the RT operating instructions §14
  "Jira scope is ambiguous" condition: stop and ask on the issue.

"Scope creep" — making the task 20% larger to fix something tangential — is a
stop condition.

## 8. Reporting

End-of-task reports always include:

- Worktree path and branch name
- Changed files (exact list, with line counts)
- Validation results (each command, pass/fail, key output; bench steps marked deferred)
- Forbidden-path check result (empty / list of violations)
- Confirmation that no commit/push/PR happened unless explicitly authorized
- Next recommended action per the RT operating instructions §15 Final Report

## 9. Persistent context

- **Read `CLAUDE.md`** at session start (RT operating instructions, machine-specific
  context: Windows, miniforge3 Python, encoding rules; and the active Spec-Kit
  plan pointer).
- **Read `~/.claude/global-lessons.md`** when debugging — many problems are
  already solved there.
- **Read project memory** (`~/.claude-work/projects/<project>/memory/MEMORY.md`)
  — e.g. the Frappe-no-bench and DP2-citation-verification lessons.
- **Update memory** when you learn something durable. Use the `MEMORY.md`
  index; never write memory content into the index itself.

## 10. Dynamic workflows — scoped to one Jira issue

Dynamic workflows (the `Workflow` tool and any multi-agent orchestration it
spawns) are constrained to work on **one Jira issue at a time** — they
orchestrate a single issue's scope, they do not become a parallel authority
that bypasses these rules. By default:

- **One Jira issue per workflow** unless the user explicitly approves running
  several.
- **Subagents are read-only by default.** Editable subagents require explicit approval.
- **Exactly one implementation path may edit files.** Parallel editable agents
  are forbidden (they race on shared files/state).
- **The Jira issue's stated scope still binds.** A workflow does not widen it.
- **Forbidden surfaces (§3) and out-of-scope surfaces (§4) stay off-limits.**
  A workflow cannot self-grant a gate.
- **Stop-before-commit holds (§5).**
- **Inspect the raw script before repeat use** — to confirm it honors the
  issue's scope, read-only default, single edit path, forbidden surfaces, and
  the stop boundary.

In short: a workflow is a way to *run a Jira issue's task with fan-out*, not
a way to escape scope discipline.
