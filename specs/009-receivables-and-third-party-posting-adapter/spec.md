# Feature Specification: Third-Party (Payer) Posting Adapter

**Feature Branch**: `009-receivables-and-third-party-posting-adapter` (the folder name is kept so links
and history stay valid)

**Feature ID**: 009

**Status**: RE-SCOPED by RT-419 (2026-11-23), on the owner's delegation of 2026-11-23 ("i delegate you to
choose recommended actions", "finish these implementation tickets"). The first draft (2026-06-16, Option A:
Backend-Core owned a receivable ledger and the Connector posted Payment Entries and claim valuations) is
**superseded**. It is kept only in git history (`git log -- specs/009-receivables-and-third-party-posting-adapter`).

> **Numbering note.** `009` was owner-directed. Connector specs `005` and `008` are intentional gaps; do
> not backfill them.

---

## 0. What changed, and why

The owner narrowed plan 035 to **Option B** (RT-413, decision R-1): Backend-Core does not run a second
accounts-receivable ledger. ERPNext is the accounting engine, so what a payer owes is ordinary **Accounts
Receivable on the payer's own ERPNext Customer**. Backend-Core keeps only the **payer responsibility
facts** of a sale: `Sale.responsibilities[]` (`pos-sales/sales.yaml`), carried to the Connector as
`Sale.responsibilities` on the posting feed (`erpnext-connector/posting-feed.yaml`, RT-417).

Consequences for this spec:

| First draft (removed) | Now |
|---|---|
| Receivable, `payment_application`, `claim`, `remittance` and `reconciliation_result` posting commands | **Removed.** Backend-Core deleted that runtime and tables (RT-422, migration 0129; `settlement.yaml`, `x-retired-operations`). The Connector never consumed them. |
| A Payment Entry per cash application, a valuation document per claim or remittance | **Removed.** No Payment Entry is posted by the Connector. Receiving money from a payer is an ERPNext user action on that payer's Customer (or the Marley bridge), outside the posting feed. |
| A new 012 settlement transport and the 011-DR-POSTING-R1 rider | **Removed.** The only transport is the existing `connectorPullPostings` / `connectorAckOutcome` boundary, and `sale_post` already carries what is needed. |
| Backend-Core decides short-pay outcomes | **Never in Core.** See section 4. |

What stays is the **single-Sales-Invoice payer-debtor model**, section 2.

## 1. Architecture (unchanged invariants)

```
POS / Admin-Console -> Backend-Core -> ERPNext-Connector -> ERPNext / Frappe
```

The Connector reaches Backend-Core only through the posting-feed boundary and never calls
`settlement.yaml`'s Console or POS routes. ERPNext remains the accounting engine; the Connector is the only
ERPNext adapter; nothing is forked and no ERPNext core code is copied.

## 2. The posting model (implemented by RT-419)

For a `sale_post` whose `Sale.responsibilities` names a payer:

1. **Debtor.** The Sales Invoice's `customer` is the payer's ERPNext Customer. The contract defines
   `payerExternalRef` as "the payer's ERPNext Customer link" (`SaleResponsibility.payerExternalRef`,
   `PayerAccount.externalRef`), so the mapping is the **identity**: no map-row doctype and no Connector
   Settings field (the release stays `image-only`, RT-927). The store's Customer map is not read.
2. **Fail closed.** A null, blank, unknown, disabled or frozen payer, or a link that names no Customer under
   exactly that name, is `permanently_rejected` / `validation` for that item only (`UnmappedPayer`; read in the submit stage, where an invoice already
   submitted under the same provenance is recovered instead of rejected). There is
   no fallback to the store's Customer or any default Customer, and no global pause (payers are not in the
   Compatibility Profile).
3. **One payer per sale, one Sales Invoice per work item.** The contract caps `responsibilities` at
   `maxItems: 1`. A second entry, an empty list or an explicit `null` is a parse error (`validation`), never a
   split and never a guess. A multi-payer sale needs a later contract version together with the multi-document
   ack it would need; it is not decided here.
4. **Tenders still settle on the invoice (RT-78).** Each tender is a `payments` row with `is_pos = 1`
   (Mode of Payment from the tender map). The payer's share is the residual and stays **outstanding on the
   payer's Customer**. A full-credit sale (no tenders) is an unpaid invoice to the payer, with no `is_pos`.
5. **Coverage.** `Σ tenders + Σ responsibilities` must equal the invoice total built from the lines (not
   `posTotal`), else `validation` (`CoverageMismatch`). The Connector never adjusts a line or moves an amount
   between payers to force a match. Before submit, ERPNext's computed totals must show `paid_amount == Σ
   tenders`, `outstanding_amount == Σ responsibilities`, `grand_total` their sum, and no change or write-off
   (`assert_partially_settled`; a receivable account in another currency is refused rather than compared across
   currencies); otherwise the insert is rolled back and the item is rejected.
6. **Exactly-once is unchanged.** Posting Log plus the `unique_rt_si_provenance` index; the payer path adds no
   second document.
7. **Replay.** An existing payer invoice is verified against its frozen debtor: the rebuild keeps the tenders
   and compares `customer` (the payer link is the Customer name), reading no current Customer row, so a
   payer disabled after posting does not break the replay.
8. **Reversal.** A reversal of a sale with responsibilities is refused (`validation`): the contract leaves it
   undefined (RT-417, 035 OQ-4) and a credit note on the store's Customer would credit the wrong debtor.
   Backend-Core must not offer one; the Connector refuses it as a second guard.
9. **Sales without responsibilities** build and post exactly as before (store Customer, RT-78 tenders, RT-10 D8
   tender-unknown).

Rollout order is **Connector first** (posting-feed RT-417 ROLLOUT ORDER): Backend-Core offers a payer-bearing
`sale_post` only after the Connector that supports it is deployed and the owner sets
`ERPNEXT_FEED_RESPONSIBILITIES_ENABLED` (plus its re-head SQL). Those are owner steps, not part of this spec.

## 3. Non-goals

- No Payment Entry, claim, remittance, receivable or reconciliation document, and no new work-item kind.
- No tax allocation per payer (tax is not posted while VAT is 0; `SaleTaxNotPosted` still applies).
- No change to Backend-Core, the contract or its pinned copy.
- No reversal or return of a payer-bearing sale (section 2.8).
- No credit terms, credit limits or ageing: ERPNext's Customer and Receivable reports own them.

## 4. Short-pay outcome vocabulary (recorded, not implemented)

When a payer pays less than it owes, the outcome is one of
`contractual_adjustment | customer_responsibility | dispute | write_off`. That vocabulary **lives in ERPNext,
the Connector or the Marley bridge, never in Backend-Core**: Core records who owes how much of a sale and
nothing about how a shortfall is resolved. RT-419 posts none of these outcomes. A later issue that
implements one must do it on the ERPNext side (for example a Payment Entry or Journal Entry against the
payer's Customer, or a write-off) and must not add a state, balance or outcome column to Core.

## 5. Acceptance evidence

- Frappe-free unit tests (`tests/posting/test_payer_posting.py`, `test_payer_posting_glue.py`): single payer,
  cash plus credit, full credit, unmapped / disabled / frozen / case-folded payer, more than one payer,
  coverage mismatch, settlement drift, replay, reversal refusal, and unchanged non-responsibility paths.
- Bench evidence on ERPNext v16 (`tests/test_rt419_payer_posting_db.py`, CI job `connector/bench`): a
  cash plus credit sale posts an invoice whose customer is the payer, whose `outstanding_amount` equals the
  credit share, and whose GL entries balance with the receivable booked against the payer.
- `test_foundation` passes.

## 6. Open items (owner)

- Multi-payer sales (later contract version and a multi-document ack).
- Reversal of a payer-bearing sale (a later contract issue).
- Receiving payments and short-pay outcomes (section 4), on the ERPNext or Marley side.
- Turning the Backend-Core flag on, after the Connector deploys.
