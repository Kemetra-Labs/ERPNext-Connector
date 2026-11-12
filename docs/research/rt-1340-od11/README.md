# RT-1340 — OD-11 proof: how a promotion clawback posts on an ERPNext v16 credit note

**Jira:** RT-1340 (RT-286 slice S8). **Decision:** spec 048 OD-11
([decision record](../../../../../services/backend-core/specs/048-offer-engine-v1/decision-record.md#od-11--how-a-clawback-is-posted-to-erpnext)).
**Run:** 2026-11-12. **Evidence class:** local disposable bench, never the lab hosts or the rt9 tenant.

## Bench

| | |
|---|---|
| Frappe / ERPNext | v16.52.0 / v16.51.0: the RT-494 pins of the `connector/bench` lane. The lab's ERP image is built from ERPNext 16.51.0 too (`build-erp-image.yml`; a repo record, not observed on the host) |
| Database | MariaDB 10.11 (the lane uses 11.8) |
| Site | fresh site, setup wizard: Egypt, EGP, Standard chart, perpetual inventory, FIFO valuation |
| Script | [`od11_proof.py`](od11_proof.py), run unchanged on a fresh site. It refuses a site with any Company other than its own |

Every document has the Connector's shape: `ignore_pricing_rule = 1`, `disable_rounded_total = 1`,
`update_stock = 1`, `is_pos = 1` with a Cash payment, `price_list_rate` / `discount_amount` / `rate` per row,
and, on a credit note, `return_against` plus `sales_invoice_item` on each row. Stock is valued at 50.00 a unit.

## Results

Sale E1 (spec 048 §6.1): 300.00 trigger, plus a 200.00 reward at 40% off (80.00), so 420.00 paid. ERPNext keeps
the reward row as `price_list_rate 200`, `discount_amount 80`, `rate 120`. Grand total and paid are 420.00, and the
invoice is Paid.

| Case | Credit note rows (qty × rate, list price, discount) | Grand total | Refund | ERPNext |
|---|---|---|---|---|
| **A1** option A, return the 300, clawback 80 | −1 × 220.00 (300.00, 80.00) | −220.00 | 220.00 | **accepted**; Return, outstanding 0 |
| **A2** option A with a split, 3 × 100 returned, clawback 10.01 | −1 × 96.66, −1 × 96.66, −1 × 96.67 (100.00 each), all on the same original row | −289.99 | 289.99 | **accepted**; a fourth unit is refused ("Cannot return more than 0.0") |
| A1c the reward unit at its net (no clawback) | −1 × 120.00 (200.00, 80.00) | −120.00 | 120.00 | accepted |
| **B1** option B, the 300 at 300 + clawback row qty +1 × 80 | — | — | 220.00 | **refused**: "For an item RT-PROMO-CLAWBACK, quantity must be negative number" |
| **B2** option B, clawback row qty −1 × −80 | — | — | 220.00 | **refused**: "rate must be a positive number. To Allow negative rates, enable `Allow Negative rates for Items` in Selling Settings" |
| S3 step 2 under A: the reward unit at 200 after a clawback of 80 | −1 × 200.00 against an original rate of 120.00 | — | 200.00 | **refused**: "Rate cannot be greater than the rate used in Sales Invoice" |
| S3 under B | — | — | 220.00, then 200.00 | **refused** at step 1 (as B1) |
| S3 hybrid: step 1 under A, then the reward at its net + a give-back row | −1 × 120.00 (200.00, 80.00) and −1 × 80.00 on the non-stock clawback item | −200.00 | 200.00 | **accepted**, with ERPNext's non-raising message "Returned Item RT-PROMO-CLAWBACK does not exist in Sales Invoice"; total refunded 420.00 = paid |

GL of A1, which is the same shape as a plain return:

| Account | Debit | Credit |
|---|---|---|
| Sales | 220.00 | |
| Debtors | 220.00 | 220.00 |
| Cash | | 220.00 |
| Stock In Hand | 50.00 | |
| Cost of Goods Sold | | 50.00 |

The GL of A2 matches it: Sales debit 289.99, Cash credit 289.99, and stock back at 3 × 50.00. Every voucher's GL
balances. Each returned unit comes back into stock at its valuation, never at the selling rate.

## What this shows

1. **Option A posts exactly.** The credit-note total equals the refund to the minor unit. A qty-1 split keeps
   every row linked to the same original row, and ERPNext's cumulative over-return cap still applies. The
   reduced rate is a native visible discount (`price_list_rate` minus `discount_amount`), so the clawback stays
   readable on the credit note, and no account or item is added.
2. **Option B cannot post natively.** ERPNext v16 refuses a positive-quantity row in a return, and refuses a
   negative rate unless a site-wide Selling Settings switch is turned on. The Connector never changes Selling
   Settings (RT-283 D6, decisions 668/669), so B would need ERPNext configuration.
3. **A credit-note row can never exceed the original row's rate** (`validate_returned_items`, `sales_and_purchase_return.py`,
   unless the Item is valued at Moving Average). Golden sequence S3 step 2 expects a refund of 200.00 for a unit
   sold at a net of 120.00, so it cannot be posted under A alone. It can be posted with a hybrid: the reward unit at
   its net, plus a give-back row of quantity −1 on a non-stock item that is not on the original invoice. ERPNext
   accepts that row and only shows a message. It needs an ERPNext Item for the give-back, and ERPNext also adds an
   Item Price for it in Standard Selling the first time it is used. The RT-1335 `recordReturn` text cannot carry
   this refund either: `returnTotal` = Σ returned `lineAmount` − Σ clawback = 120.00 − 0.00. This is a contract
   question for Backend-Core, with the hybrid as one postable answer. It is recorded on RT-1340.
