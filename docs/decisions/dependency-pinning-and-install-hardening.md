# Decision: Dependency Pinning, Lock File and pip Install Hardening

**Jira**: RT-467 (Work Mode: Planning) | **Status**: Selected — recommended option, chosen by the
coordinator under the owner's standing delegation of 2026-10-17; owner may revisit |
**Date**: 2026-10-17 | **Baseline**: `origin/main` @ `391b717`

Covers SonarCloud `text:S8565` (no lock file) and `githubactions:S8541` (pip install without
`--only-binary`). This record is a plan only. The `pyproject.toml`, lock file and `.github/**`
changes it proposes are **[GATED]**: they belong to a separate Implementation issue (draft scope in
§7). This PR changes docs only.

---

## 1. Findings and current state

The ticket's baseline was `9cb465e4` (2026-10-13). `main` has moved since then.

| Finding | Where | State on `391b717` |
|---|---|---|
| `githubactions:S8541` | `.github/workflows/ci.yml` "Install test tools" | **Fixed on main.** RT-635 (#82, `391b717`) changed the step to `python -m pip install --only-binary :all: pytest==9.1.1 pytest-cov==7.1.0 ruff==0.15.20 sentry-sdk==1.45.1`. The two installs in `bench-nightly.yml` (RT-581, #80) already use `--only-binary :all:`. Sonar should close it on its next analysis of `main`. What is left is the hash gap (§2). |
| `text:S8565` | `pyproject.toml` | **Open.** No `uv.lock` / `poetry.lock` / `pdm.lock` / `pylock.toml` in the repo. |

## 2. Inventory: what is declared, and who installs it

**`pyproject.toml`** (flit_core build backend):

- `[project].dependencies = []`. Frappe is "installed and managed by bench"; ERPNext is a hard
  runtime dependency through `required_apps = ["erpnext"]` in `hooks.py`.
- `[tool.bench.dev-dependencies]`: empty.
- No `requirements*.txt`, no `setup.py`, no `[dependency-groups]`.

**Third-party imports in app code.** Apart from the standard library, `frappe` and `erpnext`, the
production code imports only `requests` (`connector/posting/poller.py`) and `sentry_sdk`
(`connector/observability/sentry_client.py`, lazily, only when `RT_CONNECTOR_SENTRY_DSN` is set).
Both come from Frappe's own environment (Frappe v15/v16 ship `requests` and
`sentry-sdk~=1.45.1`). The app therefore has **no runtime dependency of its own to lock**.

**Install paths.**

| Path | Who installs what | How |
|---|---|---|
| Production / staging bench | bench | `bench get-app retail_tower_erpnext_connector <repo-url>` (`docs/runbooks/staging-install.md`), which does an editable install of the app into the bench env (`pip install -e apps/<app>`; bench 5.x uses uv). It resolves `[project].dependencies` only, inside the env that Frappe's own pins already fix. |
| `ci.yml` (every PR, `main`) | CI tools | `pip install --only-binary :all:` of 4 exact pins (above). **Transitive deps are unpinned and no hashes are checked**: `certifi`, `coverage`, `iniconfig`, `packaging`, `pluggy`, `pygments`, `tomli`, `urllib3` float to the newest release. |
| `bench-nightly.yml` | bench + test tool | `pip install --only-binary :all: frappe-bench==5.31.0`, then `bench pip install --only-binary :all: pytest==9.1.1`. Frappe / ERPNext come from pinned git tags (`v16.52.0` / `v16.51.0`). |

## 3. What Frappe/bench apps do (convention)

- Frappe and ERPNext declare their own runtime deps in `pyproject.toml` with compatible-release or
  exact pins, and **commit no lock file**. The bench env is the integration point for every app on
  the site.
- A custom app declares only its *extra* runtime deps in `[project].dependencies`, and dev-only
  tools in `[tool.bench.dev-dependencies]` (installed by `bench setup requirements --dev`).
- bench does not read lock files (`uv.lock`, `pylock.toml`, …) of an app. A lock file in an app
  repo is therefore inert for production installs; it can only govern dev/CI.

Consequence: locking the app's **runtime** env would be wrong. It would either be ignored by
bench or, if forced, fight Frappe's own pins (`requests`, `sentry-sdk`, …) and break the
upgrade-safe path required by constitution Principle III and
[`version-pin-upgrade-policy.md`](./version-pin-upgrade-policy.md). Production pinning stays where
it is today: the Frappe / ERPNext version pair.

## 4. Options

| Option | Closes S8565 | Hash-checked CI | Bench impact | Notes |
|---|---|---|---|---|
| **A. Accept S8565 (won't fix)**, keep the 4 exact pins | No (marked won't-fix) | No | None | Leaves 8 floating transitive CI deps unchecked. |
| **B. Hash-pinned `requirements-ci.txt`** + `pip install --require-hashes --only-binary :all: --no-deps` | No (Sonar only recognises lock-file names) | Yes | None | Fixes the supply-chain gap but S8565 stays open. |
| **C. `pylock.toml` (PEP 751) for a CI dependency group; CI installs it with hash checks and no sdist builds** (**Recommended**) | Yes (expected, see §6) | Yes | None | Standard, tool-neutral format. Runtime deps stay unlocked by design. |
| **D. `uv.lock` project lock + `uv sync --locked`** | Yes | Yes | None | Locks the (empty) project too and implies a uv-managed project. Heavier than needed, and easy to misread as "runtime is locked". |
| **E. Lock runtime deps** | — | — | **Conflicts** | Rejected (§3). |

## 5. Recommended design (Option C)

1. **Declare the CI tool set once**, as a PEP 735 dependency group in `pyproject.toml`:

   ```toml
   [dependency-groups]
   ci = [
       "pytest==9.1.1",
       "pytest-cov==7.1.0",
       "ruff==0.15.20",
       "sentry-sdk==1.45.1",  # Frappe v15/v16 pin; see the RT-635 comment in ci.yml
   ]
   ```

   `[project].dependencies` stays empty. bench does not install dependency groups, so the
   production install is unchanged.
2. **Lock it** into a committed `pylock.toml` at the repo root, with sha256 hashes for every wheel,
   wheels only, for the CI runtime (Linux x86_64, Python 3.11):

   ```bash
   uv pip compile pyproject.toml --group ci --python-version 3.11 \
     --python-platform x86_64-manylinux_2_28 --only-binary :all: -o pylock.toml
   ```

3. **Install it in `ci.yml`** with hash checking and no source builds, replacing the current
   "Install test tools" step:

   ```yaml
   - uses: astral-sh/setup-uv@<commit-sha> # vX.Y.Z — SHA-pinned like the other actions
     with:
       version: "<exact uv version>"
   - name: Install test tools (hash-locked)
     run: uv pip sync --system --require-hashes --no-build pylock.toml
   ```

   `--no-build` is uv's equivalent of `--only-binary :all:` (no setup scripts run).
   `--require-hashes` makes uv refuse any wheel without a matching hash. uv is chosen over pip for
   this step because pip's `pylock.toml` install is still marked experimental (pip 26.2), and the
   runner's bundled pip may be older. bench 5.x already installs apps with uv, so this adds no new
   tool family.
4. **`bench-nightly.yml`**: leave as is in the first Implementation issue. It already uses
   `--only-binary :all:`, its two installs are exact pins, and Frappe/ERPNext are pinned git tags.
   Hash-locking `frappe-bench` itself is an optional follow-up (bench pulls many deps; low value
   for an informational nightly).
5. **Refresh process.** Bump a tool pin in `[dependency-groups].ci`, re-run the §5.2 command and
   commit both files in one PR; the CI run on that PR is the check. Keep `sentry-sdk` at the
   version Frappe pins (RT-635). Automated refresh (Dependabot/Renovate) is a separate decision
   and is not part of this scope.

### Upgrade compatibility (constitution Principle III)

- Production pins are unchanged: the supported Frappe/ERPNext pair in
  `version-pin-upgrade-policy.md` stays the only runtime pin. A Frappe/ERPNext upgrade never needs
  to touch `pylock.toml`.
- When `ci.yml` moves to the v16 runtime (Python 3.14), regenerate with `--python-version 3.14`.
  The lock is per-runtime by design; the PR that changes `python-version` regenerates it.
- If the app ever gains its own runtime dependency, it goes in `[project].dependencies` with a
  compatible-release range (`~=`) that is checked against both supported Frappe lines; it is not
  locked.

## 6. Evidence (prototype, scratchpad only; nothing committed)

Run on 2026-10-17 against `391b717`, uv 0.11.32, CPython 3.11:

- §5.2 command, with the `[dependency-groups].ci` block added to a *copy* of `pyproject.toml`,
  resolved 12 packages, all with sha256 hashes: `certifi 2026.7.22`, `coverage 7.16.2`,
  `iniconfig 2.3.1`, `packaging 26.3`, `pluggy 1.7.0`, `pygments 2.21.0`, `pytest 9.1.1`,
  `pytest-cov 7.1.0`, `ruff 0.15.20`, `sentry-sdk 1.45.1`, `tomli 2.5.0`, `urllib3 2.8.0`.
  Every one has a wheel for Python 3.11 / manylinux x86_64 (`--only-binary :all:` succeeded).
- `uv pip sync --require-hashes --no-build pylock.toml` into a clean 3.11 venv: OK. With one hash
  altered: failed with a hash-mismatch error, exit 1.
- In that venv, `ruff check retail_tower_erpnext_connector`: clean; the CI pytest command:
  `1023 passed, 4 skipped, 22 subtests passed`.
- Option B checked too: `pip install --require-hashes --only-binary :all: --no-deps -r
  requirements-ci.txt` (hashes from `uv pip compile --generate-hashes`): OK.

Not verified here (no SonarCloud API access from this environment): that Sonar's S8565 check
accepts a root `pylock.toml`. PEP 751 names `pylock.toml` as the standard lock file and the RT-467
ticket text lists it among the accepted names; confirm on the first analysis after the
Implementation PR merges.

## 7. Follow-up Implementation issue (draft)

**Title**: ERPNext-Connector — hash-locked CI tool install via `pylock.toml` (Sonar S8565) [GATED]

**Work Mode**: Implementation. **Authorizes**: `pyproject.toml` (add `[dependency-groups].ci`
only), new root `pylock.toml`, `.github/workflows/ci.yml` (the "Install test tools" step and one
new SHA-pinned `astral-sh/setup-uv` step only).

**Scope**

1. Add `[dependency-groups].ci` exactly as §5.1. No change to `[project]`, `[build-system]` or
   `[tool.bench.*]`.
2. Generate and commit `pylock.toml` with the §5.2 command (record the uv version in the PR).
3. Replace the CI install step as §5.3, with the action and uv version both pinned exactly.
4. Document the refresh command (§5.5) in a comment above the CI step.

**Acceptance criteria**

1. CI green on the PR; the job log shows the hash-locked install and the same pytest/ruff results
   as `main`.
2. `bench get-app` path unaffected: `bench-nightly.yml` run (workflow_dispatch) green on the PR
   branch.
3. After merge, SonarCloud shows S8565 closed (or, if Sonar does not recognise `pylock.toml`,
   that is recorded on the issue and the owner decides between Option D and won't-fix).
4. S8541 confirmed closed on `main`.

**Out of scope**: `bench-nightly.yml`, runtime dependencies, Dependabot/Renovate.

## 8. Next safe action

Create the §7 Implementation issue in Jira and have the owner authorize its gated paths.

## Addendum 2026-10-28: lock moved to Python 3.14 (RT-923)

The sections above record the 2026-10-17 decision on the 3.11 runtime and are kept as written.
v15 was dropped (owner decision 2026-10-25), so the CI runtime is now the v16 one, as the
"Upgrade compatibility" section foresaw:

- The connector's Python comes from `integrations/erpnext-connector/.python-version` (`3.14`);
  `requires-python = ">=3.14"` and ruff `target-version = "py314"`. Frappe v16.52.0 itself
  requires `>=3.14,<3.15` and pins `sentry-sdk~=1.45.1`, so the `sentry-sdk==1.45.1` pin stays.
- `pylock.toml` was regenerated in place with the header command and `--python-version 3.14`,
  uv 0.11.32. No version changed; `tomli` dropped out (only needed below 3.11). Every locked tool
  still installs wheels-only with hashes.
- Evidence (WSL Ubuntu, CPython 3.14.6, uv 0.11.32, the hash-locked set): `ruff check` clean,
  the CI pytest command `1309 passed, 4 skipped, 104 subtests passed`.
