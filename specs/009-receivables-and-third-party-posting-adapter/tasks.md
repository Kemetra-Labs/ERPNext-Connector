---
description: "Task list — Third-Party (Payer) Posting Adapter (Connector 009, re-scoped by RT-419)"
---

# Tasks: Third-Party (Payer) Posting Adapter

**Spec**: [spec.md](spec.md) | **Plan**: [plan.md](plan.md) | **Re-scoped by**: RT-419 (2026-11-23)

The first task list (Payment Entry, claim and remittance phases, the 012 settlement extension and the
011-DR-POSTING-R1 gates) is superseded and removed; it is in git history only. Backend-Core deleted the
receivable runtime (RT-422), so those tasks no longer exist.

Paths are relative to `integrations/erpnext-connector/retail_tower_erpnext_connector/`.

## Phase 1: Payer posting (RT-419)

- [x] **T001** Parse `Sale.responsibilities` strictly: exactly one entry, positive exact-decimal amount,
  `sequence >= 1`; `null`, `[]` and a second payer are errors (`connector/posting/contracts.py`).
- [x] **T002** Resolve the payer: the link is the Customer name; exact name, not disabled, not frozen, else
  `UnmappedPayer` (`connector/posting/payer.py`, `errors.py`, glue read `_assert_payer_postable`, which recovers an already-submitted invoice instead of rejecting it).
- [x] **T003** Builder: a `sale_post` with responsibilities debits the payer's Customer, never the store's
  (`connector/posting/builder.py`).
- [x] **T004** Coverage: `Σ tenders + Σ responsibilities == Σ lines`, also for a full-credit sale
  (`connector/posting/tender.py`, `CoverageMismatch`).
- [x] **T005** Pre-submit partial-settlement check: paid == Σ tenders, outstanding == Σ responsibilities
  (`tender.assert_partially_settled`, `frappe_glue._assert_settlement`).
- [x] **T006** Replay: keep the tenders, compare the customer, read no Customer row
  (`frappe_glue._replay_expected`, `representation_check.replay_exemptions`).
- [x] **T007** Refuse a reversal of a payer-bearing sale (`connector/posting/reversal_policy.py`).
- [x] **T008** Unit tests `tests/posting/test_payer_posting.py` and `test_payer_posting_glue.py`.
- [x] **T009** Bench evidence (green `connector/bench` run 45091859797 for the first head; see the RT-419 comment) `tests/test_rt419_payer_posting_db.py` (CI job `connector/bench`, ERPNext v16).
- [x] **T010** Re-scope this spec folder.

## Phase 2: Owner rollout (not part of RT-419)

- [ ] **T011** Deploy the Connector release (`rollback_class.py` says `image-only`).
- [ ] **T012** In Backend-Core, set `ERPNEXT_FEED_RESPONSIBILITIES_ENABLED` and run its re-head SQL.

## Later issues (do not pre-decide)

- [ ] **T-L1** Multi-payer sales: a later contract version with a multi-document ack.
- [ ] **T-L2** Reversal of a payer-bearing sale: a later contract issue.
- [ ] **T-L3** Receiving payments and short-pay outcomes
  (`contractual_adjustment | customer_responsibility | dispute | write_off`) on the ERPNext or Marley side,
  never in Backend-Core.
