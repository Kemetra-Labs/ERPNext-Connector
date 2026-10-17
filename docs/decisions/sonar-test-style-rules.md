# Decision: SonarCloud Test-Style Rules S9073 and S5778

**Jira**: RT-469 (Work Mode: Planning) | **Status**: Selected — recommended option, chosen by the
coordinator under the owner's standing delegation of 2026-10-17; owner may revisit |
**Date**: 2026-10-17 | **Baseline**: `origin/main` @ `391b717`

`python:S9073` ("Split this composite assertion into separate assertions", 95 hits) and
`python:S5778` ("Refactor this exception test to have only one invocation possibly throwing",
80 hits) are 175 of the connector's 222 Sonar issues, all under
`retail_tower_erpnext_connector/tests/**`. This record classifies a sample of each, picks a
disposition and drafts the follow-up. It changes docs only; no test, Sonar or CI setting is
changed here.

---

## 1. Method

SonarCloud's API was not reachable from this environment (proxy 403), so the hits were
reproduced locally with an AST scan of `retail_tower_erpnext_connector/tests` on `391b717`:

- S9073: `assert` whose test is a `BoolOp` with `and`. **94** hits (Sonar: 95 on `9cb465e4`).
- S5778: a `with pytest.raises(...)` block whose body contains more than one call expression.
  **80** hits (Sonar: 80).

Top files match the ticket (`posting/test_compatibility_profile.py` 21 S9073,
`posting/test_contracts.py` 22 S5778, `posting/test_stock_policy.py` 13 S5778,
`posting/test_return_glue.py` 14 S9073). The counts are close enough to treat the scan as the
same population. A sample of 14 per rule (every 7th / 6th hit) was read by hand. Then all 80
S5778 hits were classified by shape, because S5778 is the rule that can hide a real
test-correctness problem.

## 2. S9073 — composite assertions

**Sample** (file:line under `retail_tower_erpnext_connector/tests/`):

| Location | Assertion |
|---|---|
| `bin_view/test_paged_worker.py:211` | `assert all(chunks[:-1]) and (n == 0 or chunks[-1])` |
| `bin_view/test_poller.py:612` | `assert sched["reason"] == "attempt_deadline" and sched["failures"] == 1` |
| `posting/test_commit_before_ack_glue.py:351` | `assert "rollback" in timeline and "commit" not in timeline` |
| `posting/test_compatibility_glue.py:363` | `assert "compatibility_checked_at" in values and values["compatibility_report"].startswith("OK")` |
| `posting/test_compatibility_profile.py:105` | `assert len(critical) == 1 and "precision" in critical[0]` |
| `posting/test_compatibility_profile.py:156` | `assert len(critical) == 1 and "disabled" in critical[0]` |
| `posting/test_compatibility_profile.py:276` | `assert result.ok and result.status == "WARNING"` |
| `posting/test_connector_settings_shape.py:165` | `assert f["fieldtype"] == fieldtype and f.get("read_only") == 1` |
| `posting/test_representation_glue.py:332` | `assert fake.payloads == [] and "insert" not in fake.events` |
| `posting/test_resolution_check.py:337` | `assert reason is not None and reason.startswith("representation:")` |
| `posting/test_return_glue.py:272` | `assert outcome == "permanently_rejected" and client.acks[0]["reason"]["category"] == "validation"` |
| `posting/test_return_glue.py:420` | `assert record["category"] == "validation" and record["reason_message"] == message` |
| `posting/test_settlement_glue.py:261` | `assert fake.events == ["savepoint", "insert", "rollback"] and store.writes == []` |
| `posting/test_worker.py:334` | `assert result.cursor == "page-1" and result.rejected_refs == ("wi-B",)` |

**Classification: 14/14 pure style.** Every conjunct is still checked. An `and` assertion fails
if any part is false, so no regression can pass. pytest's assertion rewriting already prints the
value of each operand on failure, so splitting adds little diagnostic value. Several are guarded
on purpose (`len(critical) == 1 and … critical[0]`, `reason is not None and reason.startswith(…)`):
the first operand keeps the second from raising an `IndexError` / `AttributeError`. Splitting
them is safe but gains nothing. Rewriting 94 sites is pure churn, with merge-conflict risk on the
most active test files.

## 3. S5778 — more than one call inside `pytest.raises`

**Sample**:

| Location | Body inside `pytest.raises` |
|---|---|
| `bin_view/test_paged_worker.py:222` | `w.process_request(_request(), client=t.BinViewClient(transport, correlation_id="c"), reader=reader, clock=_Clock())` |
| `bin_view/test_paged_worker.py:340` | `w.process_request(_request(), client=t.BinViewClient(…), reader=_Reader(_bins(1037)), clock=_Clock(), deadline=w.AttemptDeadline(…))` |
| `bin_view/test_transport.py:169` | `_client(fake).report_snapshot("req-1", _window_report(3), idempotency_key="k")` |
| `posting/test_builder.py:421` | `_settle(_tendered([...]), mode_of_payment_for=only_cash)` (`match="card_external"`) |
| `posting/test_contracts.py:235` | `c.PostingWorkItem.from_wire(self._with_tenders([...]))` (`match="method"`) |
| `posting/test_contracts.py:310` | `c.PostingWorkItem.from_wire(self._reversal(**ref))` (`match="null"`) |
| `posting/test_contracts.py:378` | `c.PostingWorkItem.from_wire(self._reversal(recordedAt=…, businessDate=…))` (`match="businessDate"`) |
| `posting/test_contracts.py:483` | `c.PostingWorkItem.from_wire(_return_wire(returnLines=[element]))` (`match="object"`) |
| `posting/test_representation_check.py:234` | `expected_from_payload(_payload(), exempt=frozenset({"rate"}))` |
| `posting/test_return_builder.py:190` | `_build(_return(refund_tenders=[...]))` |
| `posting/test_reversal_builder.py:499` | `_build(_with_tenders(_void_work_item(), c.SaleTender("cash", "150.00")))` |
| `posting/test_stock_policy.py:161` | `_link(_doc(is_return=0))` |
| `posting/test_stock_policy.py:318` | `_link_return(_return_doc((_A, "ITEM-B", "-1")))` (`match="ITEM-B"`) |
| `posting/test_transport.py:208` | `client.ack_outcome("wi-1", c.OutcomeAckRequest.failed_transient())` |

**Full-population shape (all 80):** every hit is **one statement with one top-level call**. The
extra calls Sonar counts are argument builders nested inside it (`_doc(…)`, `_wire(…)`,
`frozenset(…)`, `t.BinViewClient(…)`). No hit has two sequential calls where the first could
raise and skip the call under test.

| Shape | Count |
|---|---|
| Domain-specific exception type, with `match=` | 14 |
| Domain-specific exception type, no `match=` | 34 |
| Generic `ValueError`, with `match=` | 22 |
| Generic `ValueError`, no `match=` | **10** |

**Masking analysis.** The way an S5778 hit can hide a regression: a nested builder raises the
expected exception before the call under test runs. For a domain type (`TenderMismatch`,
`UnmappedTender`, `ReturnLineMismatch`, `AckConflict`, `WindowLimitExceeded`, …) the test helpers
cannot raise it, so the risk is nil. With `match=`, the message pins the source. The residual
risk is the **10 generic `ValueError` without `match=`**. Their builders were read:
`_client` (`BinViewClient` constructor with no validation), `_wire` / `_doc` / `_return_doc` /
`_payload` (plain dict literals), `PostingClock` (a plain frozen dataclass; the time zone is
resolved inside `stamp_for`), `timedelta(days=1)`. None of them raises `ValueError`. **So no
S5778 hit is a genuine masking case today: 80/80 are style.**

A related weakness does exist, but it is not the one S5778 describes. In those 10 tests a bare
`ValueError` passes for **any** `ValueError` from the call under test, not only the one the test
names. Example: `posting/test_posting_time.py:67`
(`test_unknown_site_timezone_is_rejected`) would still pass if a regression made `stamp_for`
reject the valid timestamp `"2026-09-27T10:15:30Z"` before it reached the time-zone lookup.
Adding `match=` fixes that; splitting calls does not. The 10:

- `bin_view/test_transport.py:101`
- `posting/test_posting_time.py:67`, `:122`
- `posting/test_representation_check.py:234`
- `posting/test_resolution_contracts.py:72`, `:80`, `:86`, `:91`
- `posting/test_stock_policy.py:161`, `:335`

## 4. Options

| Option | What | Cost | Value |
|---|---|---|---|
| (a) Refactor genuine S5778 cases + accept S9073 | Genuine S5778 cases = **0**, so this equals "accept both" | — | — |
| (b) Refactor all 175 | Split 94 asserts; hoist builders out of 80 `raises` blocks | High churn in the busiest test files; review load | No correctness gain (§2, §3) |
| (c) Accept both as won't-fix for test code, by ignore rule; plus a small `match=` hardening of the 10 tests (**Recommended**) | Project-level "Ignore Issues on Multiple Criteria" for `python:S9073` and `python:S5778` on `retail_tower_erpnext_connector/tests/**`; separate small test-only issue for the 10 `match=` additions | One admin setting; ~10 one-line test edits | Removes 175 noise issues so the remaining count reflects real problems; closes the one real weakness found |
| (d) Change the org quality profile (deactivate both rules) | Org-level setting | Affects every Kemetra project | Too broad: S5778 can be a real problem in other repos |

## 5. Decision

**Selected: option (c).** Selected — recommended option, chosen by the coordinator under the
owner's standing delegation of 2026-10-17; owner may revisit.

1. **Sonar disposition (proposed; needs a SonarCloud project admin, not applied here).** In
   SonarCloud → project `ERPNext-Connector` → Administration → General Settings → Analysis Scope →
   *Ignore Issues on Multiple Criteria*, add:
   - rule `python:S9073`, file path pattern `retail_tower_erpnext_connector/tests/**`
   - rule `python:S5778`, file path pattern `retail_tower_erpnext_connector/tests/**`

   This is a project setting, not the org quality profile, so other Kemetra repos are unaffected.
   If the owner prefers it in version control, the same keys
   (`sonar.issue.ignore.multicriteria=e1,e2`, `…e1.ruleKey`, `…e1.resourceKey`) can go in
   `.sonarcloud.properties`. **Verify first** that automatic analysis honours them; that is not
   verified here. As a fallback, bulk-mark the 175 issues "Accept" in the Sonar UI with a link to
   this record. Do **not** change the org quality profile (option d).
2. **Follow-up Implementation issue (test-only, low priority)** — draft:
   - **Title**: ERPNext-Connector — pin the expected message on 10 bare `pytest.raises(ValueError)`
     tests
   - **Work Mode**: Implementation. **Authorizes**: the 5 test files listed in §3 only.
   - **Scope**: add a `match=` that names the specific validation failure to each of the 10 sites
     in §3. No production code change, no other test change.
   - **Acceptance**: CI green; each new `match=` fails when the asserted message is changed
     (spot-checked on at least 2 sites); ruff clean.
3. New tests should still prefer one assertion per fact and a `match=` on generic exceptions.
   This is a review guideline, not a gate.

## 6. Next safe action

A SonarCloud project admin applies the §5.1 ignore criteria (or bulk-accepts the 175 issues) and
records it on RT-469.
