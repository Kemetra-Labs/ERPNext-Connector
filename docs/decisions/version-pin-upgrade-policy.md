# Decision: Version Pin & Upgrade Policy

**Spec**: 001 — Frappe App Foundation | **Status**: Accepted | **Date**: 2026-06-04
**Amended**: 2026-10-15, RT-494 (v16 added after the RT-201 verification; owner decision: run on
the latest stable line). 2026-10-25: v15 dropped (owner decision; no site runs it), v16 only.

Implements FR-007 (supported version range) and supports FR-008 (upgrade policy).
Anchored to constitution Principle III (additive & upgrade-safe changes).

---

## Supported version range

| Component | Supported line | Validated pin | Notes |
|-----------|----------------|---------------|-------|
| Frappe Framework | **v16** | 16.52.0 | Host framework |
| ERPNext | **v16** | 16.51.0 | ERP/accounting/inventory backend; declared as `required_apps` in `hooks.py` |
| Python | **>= 3.10** (v16 bench: >= 3.14) | 3.14.8 | The app's floor is `requires-python` in `pyproject.toml`; the bench decides the actual version |
| Node | v16 bench: >= 24 | 24.22.0 | The connector ships no JS; Node matters only for the bench |
| MariaDB | 11.8 | 11.8.9 | As in the frappe_docker image for v16 |

**v16 is the only supported line** (the latest stable Frappe/ERPNext line). v15 was dropped on
2026-10-25 by owner decision (no site runs it); a v15 site pauses posting like any unsupported
major. Frappe and ERPNext must be on the **same** major. v16 was validated on RT-201 (install,
v15 → v16 migration, rollback, G5/idempotency,
posting, returns, tender and pricing regression; Jira comment 11893). The app version is
**`0.3.0`** (`retail_tower_erpnext_connector/__init__.py`; `0.3.x` is v16-only, `0.2.x` was v15 and
v16, `0.1.x` was v15-only).

The Compatibility Profile (RT-379) holds the same set in `SUPPORTED_MAJORS`: on a site outside it
the posting poller does not pull (posting is paused; nothing is rejected or lost).

### v16 bench prerequisites

- `scheduler_tick_interval: 60` in `sites/common_site_config.json`. Frappe v16's default
  scheduler tick is 240 s (v15: 60 s), so without it the every-minute posting poller runs only
  about every 4 minutes and RT-45 (a sale reaches ERPNext in about a minute) is missed. Measured on
  RT-201.
- When the database moves from MariaDB 10.6 to 11.8 in place, run `mariadb-upgrade` (the official
  image skips it unless `MARIADB_AUTO_UPGRADE` is set).
- Set the site timezone (setup wizard) before installing the connector. Otherwise the scheduled
  job types are stamped with a `creation` in the future and do not run (seen on v15 and v16).

## Enforcement stance — declared, not blocked

Version compatibility is **declared and documented, not enforced as install-blocking logic**
(research.md Decision 2). The foundation adds no `before_install` version gate.

- `required_apps = ["erpnext"]` ensures the app cannot install on a site **without** ERPNext,
  but does not pin a specific ERPNext *version*.
- If a site runs an ERPNext/Frappe version outside the supported line (v16), that
  configuration is **unsupported**. The expected operator action is to align the site to a
  supported line (or wait for a connector release that supports the target line) before
  installing or upgrading. `bench` will surface gross incompatibilities during install/migrate.
- Install is not blocked, but posting is: the Compatibility Profile pauses the posting poller on
  an unsupported or mixed-major site (see above).

## Revising the pin

The supported range is revised only through this document plus a corresponding bump to the
app version and `pyproject.toml`, validated via the upgrade workflow in
[`../runbooks/upgrade-compatibility.md`](../runbooks/upgrade-compatibility.md). Production
upgrades follow the staging-first sequence defined there — never applied directly in
production (constitution Principle III).
