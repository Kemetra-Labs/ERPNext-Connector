# Implementation Plan: Third-Party (Payer) Posting Adapter

**Spec**: [spec.md](spec.md) | **Re-scoped by**: RT-419 (2026-11-23) | **Parent decision**: RT-413 (Option B, R-1)

The first plan (Payment Entry, claim and remittance posting behind a new 012 settlement transport and rider
011-DR-POSTING-R1) is superseded; it is in git history only. Backend-Core removed the receivable runtime
(RT-422), so none of that transport exists or is needed.

## Official-first (EXTEND)

- **Official capability:** ERPNext v16 Sales Invoice with `customer` = the debtor and POS payment rows
  (`is_pos = 1`, `payments`); a part-paid invoice leaves the rest as `outstanding_amount` on the Customer's
  receivable. Frappe `db.get_value` on `Customer` (`name`, `disabled`, `is_frozen`).
- **Existing Retail Tower implementation:** `connector/posting/builder.py` (`build_sales_invoice`),
  `tender.py` (RT-78 payments), `frappe_glue.py` (`post_work_item`, replay guard, `_submit_atomically`).
- **Decision:** EXTEND. One new builder input (`payer_customer_for`), one new pure module (`payer.py`), a
  coverage rule next to the RT-78 tender rule, and a partial-settlement check next to `assert_settled`.
- **Not built:** a payer map doctype or Connector Settings field (the contract's `payerExternalRef` already is
  the Customer link), a Payment Entry, any new work-item kind, any Backend-Core change.

## Design

| Concern | Where | Rule |
|---|---|---|
| Parse | `contracts.py` `SaleResponsibility`, `_parse_responsibilities` | absent = `()`; else exactly one strict entry; `null`, `[]`, a second payer, a non-positive amount or a bad `sequence` = `ValueError` (`validation`) |
| Resolve payer | `payer.py` (pure), `frappe_glue._assert_payer_postable` (the one read, in the submit stage like the RT-434 warehouse check) | exact-name Customer, not disabled, not frozen; else `UnmappedPayer` (`validation`, that item only); an invoice already submitted under the provenance is recovered, never rejected |
| Build | `builder.build_sales_invoice(payer_customer_for=...)` | a `sale_post` with responsibilities: debtor = payer, store map not read; no resolver = fail closed |
| Coverage | `tender.build_payments(responsibility_amounts=...)`, `CoverageMismatch` | `Σ tenders + Σ responsibilities == Σ lines`, also for a full-credit sale |
| Settlement check | `tender.assert_partially_settled`, `frappe_glue._assert_settlement` | paid == Σ tenders, outstanding == Σ responsibilities, grand total their sum, no change or write-off; a receivable account in another currency is refused, not compared |
| Replay | `frappe_glue._replay_expected`, `representation_check.replay_exemptions` | payer sales keep tenders and compare `customer`; no Customer read |
| Reversal | `reversal_policy.assert_reversal_supported` | refused when the sale has responsibilities |
| Exactly-once | Posting Log, `unique_rt_si_provenance` | unchanged |

## Phases

1. **Done in RT-419:** the table above, with frappe-free unit tests and the bench test.
2. **Owner rollout (not in RT-419):** deploy the Connector (release class `image-only`), then set
   `ERPNEXT_FEED_RESPONSIBILITIES_ENABLED` in Backend-Core and run its re-head SQL so held credit sales are
   offered.
3. **Later issues:** multi-payer (contract plus multi-document ack), reversal of a payer sale, receiving
   payments and short-pay outcomes on the ERPNext or Marley side (spec section 4).

## Out of scope

Payment Entries, claim or remittance valuation, receivable ledgers, tax per payer, credit terms, any change to
Backend-Core, the contract or its pin.
