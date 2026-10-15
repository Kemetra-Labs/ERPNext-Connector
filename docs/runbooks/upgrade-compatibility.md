# Runbook: Upgrade & Compatibility

**Spec**: 001 — Frappe App Foundation | **Implements**: FR-008 | **Date**: 2026-06-04

Defines the safe upgrade sequence for `retail_tower_erpnext_connector`. Anchored to
constitution Principle III: **upgrades are validated in staging, with a rehearsed
backup/restore, before any production change — never applied directly in production.**

---

## Compatibility matrix

| Connector version | Frappe | ERPNext | Python | Validated on |
|-------------------|--------|---------|--------|--------------|
| 0.1.x (foundation) | v15 | v15 | >= 3.10 | v15.110.0 (rt9 lab) |
| 0.2.x (RT-494) | v15, v16 (same major for both) | v15, v16 | >= 3.10 (v16 bench: >= 3.14) | v16.51.0 / Frappe 16.52.0, MariaDB 11.8 (RT-201) |

A v15 → v16 move also needs the v16 bench prerequisites in the version policy:
`scheduler_tick_interval: 60`, `mariadb-upgrade` for MariaDB 10.6 → 11.8, and the site timezone
set before the connector is installed. RT-201 rehearsed the whole sequence below on disposable
stacks (Confluence Lab and Bench Runbook, section 22): backup, image swap and `bench migrate`
(about 80 s, no data or config change), regression, and restore of the v15 backup into a clean
v15 bench. Rollback is that restore, never an in-place database downgrade.

The supported line is governed by
[`../decisions/version-pin-upgrade-policy.md`](../decisions/version-pin-upgrade-policy.md).

## Upgrade sequence (mandatory order)

> **No step may be skipped. Production is never the first place an upgrade is applied.**

1. **Stage the target versions** — bring a staging bench to the intended Frappe/ERPNext/connector
   versions.
2. **Back up staging** — take a full site backup (DB + files) before touching the staging site:
   ```bash
   bench --site <staging-site> backup --with-files
   ```
3. **Rehearse restore** — confirm the backup restores cleanly onto a scratch site. An upgrade
   is not approved until restore has been demonstrated, not just assumed.
4. **Apply the upgrade on staging**:
   ```bash
   bench get-app --branch <target> erpnext   # if ERPNext line changes
   bench update --pull --patch
   bench --site <staging-site> migrate
   ```
5. **Run the connector regression checks on staging**:
   ```bash
   bench --site <staging-site> run-tests --app retail_tower_erpnext_connector
   ```
   Plus the verification table in [`staging-install.md`](./staging-install.md).
6. **Sign-off** — record staging results. Only after a green regression run AND a rehearsed
   restore is a production upgrade approved (constitution gate G8).
7. **Back up production**, then apply the identical sequence to production with the same
   versions validated on staging. Keep the production backup until the upgrade is confirmed
   stable.

## Rollback

If any step fails on staging, fix forward on staging — never on production. If a production
upgrade fails, restore from the production backup taken in step 7. Because the foundation
introduces no business mutation and no migrations (`patches.txt` empty), foundation rollback
is limited to removing the app; later specs with migrations extend this rollback policy.

## Regression checklist (foundation scope)

- [ ] App installs on the upgraded staging site without error.
- [ ] `Connector Settings` singleton exists and opens.
- [ ] `run-tests` passes (install, no-mutation, no-fork, singleton).
- [ ] ERPNext product/stock/sales data unchanged by install/uninstall.
- [ ] Backup taken and restore rehearsed before production.
