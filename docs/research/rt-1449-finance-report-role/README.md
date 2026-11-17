# RT-1449 — finance-report role proof (blueprint v2, Step 0)

**Jira:** RT-1449 (Verification), origin RT-1448. **Blueprint:**
[Accounting & Finance UX Integration Blueprint v2](https://rahmaqanater.atlassian.net/wiki/spaces/RETAIL/pages/80805889),
sections 4, 9 and 11.2. **Evidence class:** a throwaway ERPNext v16 bench on GitHub Actions
(Frappe v16.52.0 / ERPNext v16.51.0, the RT-494 pins), never a lab, tenant or production site.

## What it answers

Can a read-only ERPNext role run the finance reports the way the Connector will run them (through
`frappe.desk.query_report.run`, as a service user, in a background job on its own queue)? The blueprint
treats the answers as assumptions V1–V10 until this runs.

| Case | Blueprint item | What it records |
|---|---|---|
| `bench_identity` | V10 | versions, installed apps, seed description |
| `role_configuration` | V1 | whether the standard permissions were copied before the Custom DocPerm was added, and each report's role list |
| `v1_reports_inline`, `v1_v3_reports_as_jobs` | V1, V3 | all nine reports as the service user, inline and as a job on the `finance_report` queue: pass/fail, runtime, rows, payload bytes |
| `v2_write_refusal`, `v2_user_without_role` | V2 | write rights and insert attempts of the service user; every report as a user without the role |
| `stock_role_and_auditor_comparison` | V1 (informational) | the stock `Accounts User` after the customization, and the stock `Auditor` alone |
| `v4_parity` | V4 | service user equals Administrator; Trial Balance and profit against independent SQL |
| `v5_two_company` | V5 | a second Company's data, with and without a Company User Permission |
| `v6_prepared_and_size` | V6 | the Stock Balance prepared-report flow and its completion-time field, and the General Ledger runtime against rows |
| `v7_cost_centre` | V7 | the GL legs of a sale and a return per cost centre, and whether `cost_center` isolates a store |
| `v9_load_signal` | V9 | a small write job's latency with and without report load on the other queue |
| `v8_closed_period` | V8 | a Sales Invoice posted into a closed Accounting Period |

Every case is independent: a failure is recorded with ERPNext's own message and the run goes on.

## How it runs

Only through `.github/workflows/finance-report-role-bench.yml` (manual `workflow_dispatch`). The
workflow starts two bench workers (without `CI` set), then imports the module and calls `main()` from the
bench's `sites` directory with this directory on `PYTHONPATH`: it is not run with `python -m`, because
the report jobs are pickled by module and name and a worker cannot unpickle a function of `__main__`.
It refuses to start unless `RT_PROOF_THROWAWAY=1` and `RT_PROOF_SITE` name the throwaway site, the
database host is local, and every Company on the site is its own.

A case is recorded as failed when it raised or when any nested result says `ok: false`; the stock-role and
Auditor comparison is informational and never fails its case. Every report runs live
(`ignore_prepared_report=True`) except the explicit prepared-report flow in `v6_prepared_and_size`. The results are written to `finance-report-proof-results.json`
(uploaded as the `finance-report-proof` artifact) and summarised in the job summary.

This machine has no local bench (`docs/agent-os/standing-rules.md` §6), so the script is not run locally.
The local checks are `ruff format`, `ruff check` and `py_compile`.

## Not product code

Nothing in the Connector, Backend-Core or the Admin-Console imports this script. It posts only synthetic
data and configures only its own throwaway site. A result here is evidence for a decision; it is not an
approval to build.
