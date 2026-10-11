# Decision: ERPNext Pricing Representation Protection, Compatibility Profile & Post-Posting Verification

**Jira**: RT-283 (Planning) | **Status**: 🟢 OWNER-RATIFIED (D1–D5, 2026-10-11) — ⏳ NOT IMPLEMENTED
**Source decisions**: Retail Tower Pricing & Store Overrides baseline (Confluence RETAIL page 23855106),
decisions 649–687 — §R ERPNext boundary, §S ERPNext Compatibility Profile, §T Post-posting verification.
**Follow-ups**: RT-376 (F1) · RT-377 (F2) · RT-378 (F3) · RT-379 (F4) · RT-380 (F5)

> This document records a **planning decision**. It does not claim any of the behaviour below exists on
> `main`. The "Current state" table was verified against `origin/main` `de445c5`.

## Question

Retail Tower is the pricing authority. The Connector translates a finalized sale into an ERPNext
Sales Invoice and must never let ERPNext silently reprice it (decisions 656–659). How does the Connector:

1. suppress ERPNext repricing (657, Planning Gate 1);
2. detect an incompatible ERPNext site before a sale depends on it (665–674);
3. prove that the commercial fields ERPNext persisted are exactly the ones Retail Tower sent (675–687)?

## Current state (`origin/main` `de445c5`)

| Decision | State | Where |
|---|---|---|
| 650–654: explicit Item / UOM / qty / rate / amount | Sent | `posting/builder.py` |
| 657: `ignore_pricing_rule`-equivalent | **Missing.** The flag is never set. Return and reversal builders compose on `build_sales_invoice`, so none of them set it. | `builder.py`, `return_builder.py`, `reversal_builder.py` |
| 659: no ERP repricing | **Not proven.** ERPNext fills in missing item details on insert, so a matching selling Pricing Rule or price-list discount can rewrite `rate`, and `amount` is recomputed. Runtime proof is RT-376. | `frappe_glue._submit_atomically` |
| 665–674: Compatibility Profile | **Partial.** Only the Gate G5 index guard pauses posting. The version is declared, not enforced. | `index_guard.py`, `version-pin-upgrade-policy.md` |
| 675–683: post-posting verification | **Mostly missing.** RT-78 `assert_settled` checks totals on paid invoices only. RT-331 `check_existing` checks docstatus, warehouse and item identity only on replay/recovery. | `tender.py`, `resolution_check.py` |
| 684–687: no auto-cancel; idempotency; durable backlog; fail closed | Hold | G5 indexes, Posting Log, savepoint rollback, `reconciliation_required` |

## Decision

### A — Repricing protection (657) → RT-377

Every Connector-built Sales Invoice carries `ignore_pricing_rule = 1`. It is set in `build_sales_invoice`, so
sale, return and reversal all inherit it. The flag is **per document**. The Connector never disables Pricing
Rules or edits Selling Settings or any other global ERP configuration (668/669): the site may also serve
ERP-native sales. The flag alone is not trusted as proof; B verifies the outcome.

### B — Representation verification (675–687) → RT-378

A pure, frappe-free `posting/representation_check.py` compares the ERP document against the payload.
It covers every commercial field the Connector sends:

- **Per line** (Sales Invoice Item): Item, qty, UOM, rate, amount and warehouse, exactly.
- **Per document** (Sales Invoice): currency (ERPNext v15 holds currency on the parent, not on Sales
  Invoice Item), customer, and company (the expected company for the store; today the site default).
  It also requires no tax rows that Retail Tower did not send, and grand total equal to the sum of line
  amounts while tax is deferred.

Posting date and time are already governed by RT-49 and are out of this check's scope.

1. **Before submit**: after `insert()`, inside the existing `_submit_atomically` savepoint. A mismatch rolls
   back (nothing persists) and fails closed.
2. **After submit**: read the invoice back from the DB before acking `posted`. A mismatch acks
   `reconciliation_required`. The Connector **never** cancels or amends the document (684).
3. **Replay / recovery**: RT-331 `check_existing` gains the same representation fields.

### C — Compatibility Profile preflight (665–674) → RT-379

A pure `posting/compatibility_profile.py` evaluates a snapshot the glue reads. It is composed with the
existing G5 index guard, not a replacement for it.

- **Critical**: the poller does not pull; nothing is rejected; the backlog stays in Backend-Core (686). Triggers:
  - Frappe/ERPNext major outside the supported set;
  - RT custom fields missing;
  - currency precision lower than Retail Tower minor units;
  - a mapped UOM / Warehouse / Customer / Mode of Payment missing or disabled;
  - company currency ≠ posting currency;
  - a default Sales Taxes template or Item Tax Template that would auto-apply.
- **Warning** (recorded and logged; posting continues):
  - Selling Settings `validate_selling_price` enabled (input to Pricing Planning Gate 2);
  - enabled selling Pricing Rules (neutralised by A; shown for visibility).
- **Drift** (672/673): a fingerprint of versions plus the checked settings; a structured `compatibility.drift`
  event when it changes.

## Owner decisions (ratified 2026-10-11)

| # | Decision |
|---|---|
| D1 | Before-submit mismatch → `permanently_rejected` / `validation` with a `representation:` reason prefix. After-submit or replay mismatch → existing `reconciliation_required`. **No 012 contract change; FailureKind set unchanged.** |
| D2 | A critical Compatibility Profile failure **pauses posting** (G5 pattern). It does not reject sales one by one. |
| D3 | Compatibility visibility is **Connector-local** (Connector Settings status + structured logs). An Admin Console surface needs a separate contract-first Backend-Core item. |
| D4 | While Egyptian VAT is deferred, an auto-applied tax row on a Retail Tower invoice **fails closed** (critical in preflight, mismatch in B). Exact tax verification (681) waits for the Fiscal contract. |
| D5 | The supported set stays **v15** until RT-201 decides v16. RT-201 V3 carries the 657/671 pricing regression. |

## Follow-ups

| Key | Mode | Scope | Depends on |
|---|---|---|---|
| RT-376 (F1) | Verification | Runtime-prove repricing exposure on rt9, with and without the flag | — |
| RT-377 (F2) | Implementation | A | RT-376 |
| RT-378 (F3) | Implementation | B | D1, D4 |
| RT-379 (F4) | Implementation | C | D2–D5 |
| RT-380 (F5) | Verification | A–C end to end on rt9 | RT-377, RT-378, RT-379 |

## Out of scope

- Pricing Planning Gate 2 (below-cost behaviour), beyond the detection in C → RT-282.
- Planning Gate 3 (Fiscal gross/net/tax) → Fiscal.
- Any Backend-Core / Admin Console connector-health contract (D3).
- Any ERPNext fork, core patch or global setting change.
