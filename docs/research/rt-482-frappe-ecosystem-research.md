# RT-482 — Frappe ecosystem products: compatibility, operational value and go-to-market fit

**Work Mode:** Planning (research only). **Consolidated:** 2026-10-15.
**Status of this file:** a research record. It was committed on 2026-10-15 under an explicit owner instruction. The owner gave it first through the coordinating session and then confirmed it directly in this session. The owner's words: "tell all session that do recomnded actiions aand if sensitive question call advisor / merge auhtrized after ci green and commetns resolved".

This is a documented deviation from RT-482's acceptance criterion "no repo edits". Planning mode allows approved docs updates.

The file records evidence only. It authorizes no installation, migration, implementation or Confluence change.

## 0. Read this first: where this report comes from

RT-482's research **already existed before this session.** It was posted on RT-482 on 2026-10-14 as comments 1/3, 2/3 and 3/3 (ranking, evidence matrix, PoC and conflicts) plus 4/4 (re-evaluation against the v16-latest target).

This file does two things:

1. **Consolidates** those four comments into one document. Sections marked **[carried]** restate them; the full tables still live on the Jira comments.
2. **Delta-checks** them on 2026-10-15. Sections marked **[new 2026-10-15]** hold the delta-check results:
   - new upstream tags;
   - Needs-verification items closed against official pages;
   - Confluence Decisions v48 status;
   - a screen of third-party retail Marketplace apps, which the earlier work did not cover.

**Evidence labels**

| Label | Meaning |
| --- | --- |
| **S** | Read first-hand: GitHub source, tags, CI files or an official vendor page fetched directly. |
| **SS** | From a search summary of an official page only, not fetched directly. |
| **NV** | Needs verification. |

No row in this report is runtime-proven by Retail Tower.

**Baselines**

- Connector: `origin/main` = `65a002a` (HEAD of this worktree, verified 2026-10-15).
- Witnessed ERP: Frappe / ERPNext **15.110.0**.
- Only Retail Tower v16 runtime: the RT-195 disposable lab. It ran ERPNext 16.50.0, Frappe 16.51.0, Python 3.14.8, Node 24.21.0 and MariaDB 11.8.9, and it tested Egypt Compliance, not these products.

**Connector surface relevant to compatibility (S, `main@65a002a`)**

- `hooks.py`:
  - `required_apps = ["erpnext"]`;
  - a Custom Field fixture scoped by name (Sales Invoice / Sales Invoice Item `rt_*`);
  - `after_install` / `after_migrate` index hooks;
  - two cron jobs: the posting poller and the bin-view poller.
- **No `doc_events` and no `override_doctype_class`.** That is why product apps that add `doc_events` on the posting site conflict with the Connector design (finding F3).
- `pyproject.toml`: `requires-python >=3.10`, Frappe managed by bench. The README, `docs/decisions/version-pin-upgrade-policy.md` and the staging runbook still declare v15. Changing them is RT-201's follow-up, not this ticket's.

---

## 1. Decision summary [carried, with deltas applied]

| Rank | Product | Outcome | Customer-facing or internal | Practical (0–5) | Marketing (0–5) |
| --- | --- | --- | --- | --- | --- |
| 1 | **Frappe Helpdesk** | **PILOT EXPERIMENT** (internal support desk), with hard gate **G0: install on v16** | Internal efficiency; indirect customer benefit | 3.4 | 2.0 |
| 2 | Frappe HR (HRMS) | LATER (one blocker removed by the v16 decision) | Customer-facing for chains | 2.6 | 3.0 |
| 3 | Frappe CRM | LATER — internal GTM only | Internal | 2.8 | 1.0 |
| 4 | Frappe Insights | LATER — internal only; **REJECT as tenant-facing** | Internal | 2.4 | 1.25 |
| 5 | Frappe Learning (LMS) | LATER | Customer-facing enabler (onboarding) | 2.8 | 2.0 |
| 6 | Frappe Cloud | LATER — only under the RT-317 reopen conditions | Platform hosting | 2.6 | 1.75 |
| 7 | Frappe Builder | LATER (optional internal marketing site) | Internal | 3.2 | 1.0 |
| — | Raven | REJECT | — | 1.8 | 1.25 |
| — | Drive | REJECT (archived; successor Frappe Suite has no releases) | — | — | — |
| — | Event Streaming (framework extension) | REJECT (archived, no v16 branch) | — | — | — |
| — | Print Designer | REJECT (archived); v16 has a built-in Print Format Editor | — | — | — |
| — | Gameplan, Mail, eCommerce Integrations | REJECT for the current role | — | — | — |
| — | **Third-party retail POS Marketplace apps** [new] | **REJECT** (architecture; see §5) | — | — | — |
| — | Lending | OUT OF SCOPE | — | — | — |

**Bottom line**

- **No product is "adopt now".**
- No product is Arabic-first or Egypt-ready enough to market.
- The v16 direction removes version blockers. It does **not** remove the tenancy, identity, Arabic, Egypt-localization or catalog-authority blockers.

### Scoring criteria [carried]

Scores are analyst judgement, not measurements.

**Practical** = mean of five criteria, each 0–5:

- **P1 Need:** a documented Jira/Confluence requirement exists.
- **P2 Architecture fit:** no boundary violation, no second authority, no new cross-repo contract.
- **P3 Effort/TCO:** 5 means ≤ 1 week and ≤ €10/mo.
- **P4 Risk:** 5 means low risk.
- **P5 Readiness:** v16 evidence, maintenance activity, Arabic/RTL.

**Marketing** = mean of four criteria, each 0–5:

- **M1 Buyer value.**
- **M2 Substantiable differentiation.**
- **M3 Egypt / Arabic-first fit.**
- **M4 Packaging potential.**

| Product | P1 | P2 | P3 | P4 | P5 | M1 | M2 | M3 | M4 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Helpdesk | 4 | 4 | 4 | 3 | 2 | 3 | 2 | 2 | 1 |
| HRMS | 2 | 4 | 2 | 2 | 3 | 4 | 3 | 1 | 4 |
| CRM | 2 | 3 | 4 | 3 | 2 | 1 | 1 | 1 | 1 |
| Insights | 2 | 2 | 3 | 2 | 3 | 2 | 1 | 1 | 1 |
| LMS | 1 | 4 | 3 | 3 | 3 | 3 | 2 | 1 | 2 |
| Frappe Cloud | 2 | 3 | 2 | 3 | 3 | 2 | 1 | 2 | 2 |
| Builder | 1 | 5 | 4 | 4 | 2 | 1 | 1 | 1 | 1 |
| Raven | 1 | 1 | 3 | 2 | 2 | 2 | 1 | 1 | 1 |

---

## 2. What changed since 2026-10-14 [new 2026-10-15]

### 2.1 New upstream tags

Read with `git ls-remote` and the GitHub API.

| Repo | Previously recorded | Now latest | Commit / date (UTC) | Requirement at the new tag | Effect |
| --- | --- | --- | --- | --- | --- |
| frappe/frappe | v16.52.0 | **v16.52.0** (unchanged) | `4515014c87` · 2026-10-14 | Python `>=3.14,<3.15` | none |
| frappe/erpnext | v16.50.0 | **v16.51.0** | `fd97695de7` · 2026-10-14 22:47 | `frappe >=16.50.0,<17`; Python `>=3.14` | The latest coherent set is now **Frappe v16.52.0 + ERPNext v16.51.0**. These remain **research candidates, not pins** (Decisions v48). The full release notes (32 KB, grepped, S) **do include changes on the Connector's posting path**, which become RT-201 V3/V4 inputs (see §7). |
| frappe/hrms | v16.50.0 | **v16.51.0** | `2478afc7a8` · 2026-10-15 07:41 | `required_apps = ["frappe/erpnext"]` | Still in lockstep with ERPNext. If HRMS may ever co-reside on the ERP site, the pin pair must move together. |
| frappe/helpdesk | v1.30.2 | **v1.31.0** | `7d7e394e75` · 2026-10-14 21:08 | **`frappe >=15.123.1,<17`** (was `>=15.116.1`); still `required_apps = ["telephony"]` | See §2.2. |
| frappe/crm | v1.87.0 | v1.87.0 (unchanged) | — | — | none |
| frappe/insights | v3.14.4 | v3.14.4 (unchanged) | — | — | none |
| frappe/lms | v2.66.0 | v2.66.0 (unchanged) | — | — | none |
| frappe/builder | v1.35.1 | v1.35.1 (unchanged) | — | — | none |
| frappe/raven | v3.0.0 | v3.0.0 (unchanged) | — | — | none |
| frappe/telephony | `develop` only, no tags | unchanged | — | — | Remains a Helpdesk dependency risk. |
| frappe/suite | `develop` only | unchanged, no tags | — | — | The Drive successor is still unreleased. |

### 2.2 Helpdesk v1.31.0: what it changes for the PoC (S)

The v1.30.2 → v1.31.0 range has 40 commits. They include:

- `refactor!: comments and notifications doctype (#3700)` — a breaking schema change; it matters for an upgrade rehearsal.
- `fix(perms): agents & tickets (#3806)` and `fix: customer manager portal permissions (#3821)` — directly relevant to PoC gate **G1 (cross-customer isolation)**.
- `fix: show translated field labels in the customer portal (#3839)` — relevant to G2 (Arabic).
- `fix(realtime): gate ticket rooms on frappe v15 and v16`.

**CI is still v15-only.** `.github/helpers/install.sh@v1.31.0` clones Frappe `version-15` and runs `bench get-app erpnext --branch version-15`. The commit `ci(ui-tests): run on main-hotfix against frappe v15` confirms it. **Helpdesk's v16 evidence therefore stays Weak.** Gate G0 (v16 install, hard) stands unchanged.

**PoC pin update:** use **Helpdesk v1.31.0** (or the latest tag at execution) instead of v1.30.2. G1 must be re-run on the pinned tag, because portal permissions changed in this release.

### 2.3 Needs-verification items re-checked against official pages

| # | Item (from comment 3/3 §3) | Result 2026-10-15 | Label |
| --- | --- | --- | --- |
| 1 | Helpdesk `main` and CRM ERPNext integration on v16 | Helpdesk: CI still v15 at v1.31.0 (above). CRM: unchanged. | Still **NV** (no upstream v16 CI) |
| 2a | Frappe support window | [frappe.io/support-versions](https://frappe.io/support-versions): v14 EOL 31 Jan 2026; **v15 EOL "End of 2027 (planned)"**; **v16 EOL "End of 2029 (planned)"**; the same for Frappe HR. | **S** (was SS) |
| 2b | Frappe Cloud pricing | [frappe.io/cloud/pricing](https://frappe.io/cloud/pricing): **Sites from $5/mo**, listing "Custom app deployment" as a Sites feature. The $5 tier is the restricted Tiny Plan (see 2c), so "custom app deployment" applies only to higher Sites tiers. **Servers from $40/mo**. No uptime percentage; "Product Warranty" is bug-fix support, with "priority SLA (optional)" on Servers. Offsite backups included. Providers: Hetzner, AWS, OCI. [frappe.io/helpdesk/pricing](https://frappe.io/helpdesk/pricing) instead says **Servers from $20/mo**. **The two official pages disagree.** | **S** (with an internal inconsistency) |
| 2c | Private-bench floor (~$25/mo) for a custom app such as the Connector | The [Tiny Plan terms](https://docs.frappe.io/cloud/tiny-plan) (updated 2026-02-16) rule the $5 tier out for the Connector. **Custom apps are not permitted**; only allow-listed apps can be installed. The plan is unavailable for private bench groups. Background jobs run every 10 minutes, which breaks the Connector's every-minute posting cron (RT-45). It exists only in the Mumbai, KSA and Virginia regions. **The $5 tier cannot host the Connector.** The cheapest plan that can host it, and its price, are not published. | **S** (Tiny Plan excluded); eligible plan and price **NV** |
| 2d | Regions, Egypt availability, offsite-backup region | The [data-residency page](https://docs.frappe.io/customer-guide/security/data-residency.md) returned **HTTP 404** on direct fetch. A search snippet says clusters exist in "Asia, Europe, the Middle East, Africa and North America" across AWS, DigitalOcean, Hetzner and OCI, but names no cluster. The earlier "no Egypt region; backups default to Mumbai" stays unproven. | **NV** |
| 3 | JSM Free agent limit | The Atlassian pricing page is the source (search summary of atlassian.com): **Free = $0, 3 agents, 2 GB storage, community support.** Exceeding the agent limit auto-starts a 7-day Standard trial. Free products are deactivated after 120 days of inactivity ([Jira Cloud plans](https://confluence.atlassian.com/display/SERVICEDESKCLOUD/Jira+Cloud+plans)). The direct fetch returned only navigation (truncated). | **SS** (was third-party) |
| 4–9 | Arabic/RTL usability in the lab, Egypt payroll for HRMS, Insights against Backend-Core RLS, Clerk via Social Login Key and HS256 ID tokens, Suite GA / Raven Android / SCIM, ERPNext tenancy for HRMS | Unchanged; all need a lab or owner decision. Suite still has no tags. | **NV** |

### 2.4 Confluence Decisions register (S, page 5341185, read 2026-10-15)

The "next safe action" in comment 4/4 (owner confirms the v16-latest direction) **is done.** The Decisions register now contains "ERPNext/Frappe pre-pilot baseline — owner-approved 2026-10-14":

- the pilot must not plan to remain on v15;
- target the latest stable, mutually compatible pair, pinned at verification time;
- Frappe v16.52.0 / ERPNext v16.50.0 are research candidates, not pins;
- RT-201 owns the evidence;
- no automatic v15 fallback;
- staged separately from Egypt Compliance;
- "RT-482 evaluates optional Frappe apps against the verified upgraded baseline; no app adoption is implied."

**Consequence:** the open next action narrows to one decision: whether to authorize the Helpdesk vs JSM Free PoC (with G0) as a separate Verification item.

---

## 3. Cross-cutting findings [carried]

These drive the ranking and are version-independent.

### F1 — Tenancy

No in-scope product isolates tenants within one site; Frappe's model is one site per tenant. Any tenant-facing use means either a site per tenant (ops cost × N) or cross-tenant data in one site.

### F2 — Identity

- Admin-Console authenticates through Backend-Core cookie sessions. POS operators authenticate through Clerk.
- Every Frappe product adds a second Frappe-User login.
- Frappe as an OIDC provider signs ID tokens with HS256 only. Frappe has no SCIM.
- Real SSO needs a new identity decision.

### F3 — Same-site coupling

Helpdesk "ERPNext HD Settings" (Customer sync) and CRM `sync_products` (Item sync) add `doc_events` to the ERPNext site the Connector posts to. That conflicts with RT-200 and with Catalog/Customer authority (CLAUDE.md §5).

**Proposed rule:** no Frappe product app on the posting ERPNext site. The only candidate exception is HRMS, which `required_apps` forces onto ERPNext, under the Staff/HR boundary.

### F4 — Arabic

Product-app Arabic coverage ranges from about 18% (LMS) to about 33% (HRMS).

| App | RTL status |
| --- | --- |
| CRM, Insights | No RTL |
| Builder | RTL request #138 open |
| HRMS | RTL bugs #5424/#5423 filed against 16.50.0 |
| Helpdesk | RTL supported since v1.29.0 |

### F5 — Version window

- v16 requires Python `>=3.14,<3.15`, Node ≥ 24 and MariaDB 10.6–11.8.
- Most official one-command product images are still built on v15. Any v16 product site needs its own pinned image built from the frappe_docker custom-image recipe.

### F6 — Licensing

| License | Products |
| --- | --- |
| AGPL-3.0 | Helpdesk, CRM, Insights, LMS, Raven, Drive, Telephony, Mail, Press |
| GPL-3.0 | HRMS, ERPNext, Lending, eCommerce Integrations |
| MIT | Frappe, Builder, Payments, Wiki |

- Never copy AGPL/GPL code into the Apache-2.0 Connector (`hooks.py: app_license = "Apache-2.0"`).
- A modified, network-exposed deployment triggers the AGPL source offer.

### F7 — Webhooks are not an event bus

Frappe v16 webhooks make 3 attempts and then only log the failure. Nothing may rely on them for correctness; this is consistent with RT-200.

---

## 4. Per-product matrix (condensed) [carried + delta]

Full tables A–F, with links, are in RT-482 comment 2/3. This is the per-dimension condensation the brief asked for.

| Product | Version compatibility (stable channel → v16 evidence) | Architecture fit (Connector-only adapter, no fork, RT catalog/price authority) | Operational value | Commercial / marketing angle | Licensing | Cost / complexity |
| --- | --- | --- | --- | --- | --- | --- |
| **Helpdesk** | `main` v1.31.0, `frappe >=15.123.1,<17`; CI v15 only → **Weak** (S) | OK as a **separate platform site with no ERPNext**. Its ERPNext integration adds Customer `doc_events` → keep it off. Ticket number goes in the RT-414 `ticket_ref` manually; no contract change. | Fills a real pilot gap: no support intake or SLA record for RT-20 | Internal or service quality only. A substantiable claim ("every request gets a ticket and a published response target") exists only after the SLA is measured. | AGPL-3.0 (+ Telephony AGPL, no tags) | Self-host ≈ €7/mo VM + 2–4 h/mo upkeep (NV). FC: the $5 Tiny Plan allow-lists Helpdesk but not its `telephony` dependency (S), so the eligible plan is NV. JSM Free $0 for 3 agents (SS). |
| **HRMS** | `version-16` v16.51.0, lockstep with ERPNext → **Strong** (S) | Must sit **on an ERPNext site** (`required_apps`). Governed by the Staff/HR boundary: Backend-Core owns the Staff ↔ Employee binding; the Connector does payout mechanics. Co-residence couples upgrades. | Real for chains (attendance, leave, payroll, commission payout) | **The only plausibly sellable add-on** for chains. But: no Egypt payroll/social-insurance package (`hrms/regional` = india, UAE), and RTL bugs remain. | GPL-3.0 | Multi-week Backend-Core + Connector contracts, plus an Egypt localization of unknown size |
| **CRM** | `main` v1.87.0, `>=15,<17`; v16 CI runs without ERPNext → Medium | Internal GTM site only. Same-site `sync_products` would make ERPNext a second catalog authority → off. | Low before commercial launch | None for retailers. The Customer/CRM boundary defers retailer CRM. | AGPL-3.0 | ~1 day; a spreadsheet suffices |
| **Insights** | `version-3` v3.14.4; nightly v15 + v16 matrix → **Strong** | **Reject tenant-facing:** one DB role per source bypasses Backend-Core RLS; it would be a second analytics truth. Internal only, read-only. | Low–medium internal | None. "Advanced BI" is parity and has no RTL. | AGPL-3.0 (`hooks.py` says GPLv3: metadata inconsistent) | 2–3 days internal + a gated read-only role |
| **Learning** | `main` v2.66.0, `version-16` branch and image → Strong-medium | Separate site, no ERP coupling; every learner needs a Frappe User | None with one pilot store | Onboarding enabler later. Content is the real cost (Arabic about 18%). | AGPL-3.0 (+ Payments MIT) | 1–2 days + content |
| **Raven** | `main` v3.0.0, declared only → Medium-weak | **Reject:** AI bots can write Frappe documents (`allow_bot_to_write_documents`), which conflicts with the AI baseline and Backend-Core authority | Low; WhatsApp dominates | None | AGPL-3.0 | — |
| **Drive** | Archived; successor Suite has no tags (S) | — | — | — | AGPL-3.0 | — |
| **Builder** | `master` v1.35.1, `>=15,<18`, declared only → Weak | Fine as a separate public site; RTL request open | Low | Internal marketing site only | MIT | 1–2 days + design |
| **Frappe Cloud** | v16 bench groups exist (SS) | Would host ERPNext + Connector; Connector outbound polling is unchanged | Less ERP ops work | Possible partner channel later; no SLA % on the official page (S) | Press is AGPL-3.0 | Sites from $5, but the $5 Tiny Plan cannot host the Connector: no custom apps, 10-min background jobs (S). Servers from $40 or $20 (pages disagree, S). The eligible Connector plan, regions and backup residency are NV. |
| **Framework extensions** | v16: API keys, OAuth2/OIDC (HS256 ID tokens), Social Login Key, LDAP, Webhook (non-durable), Server Scripts (off by default) | Event Streaming is archived with no v16 branch → reject. RT-200 already chose explicit transport over `doc_events`. | — | — | MIT | — |
| **Lending** | `version-16` v16.6.2 | Out of scope: receivables belong to Backend-Core | — | — | GPL-3.0 | — |

Persona workflows (retailer / store operator / platform owner / internal support) and build-vs-integrate alternatives are in comment 2/3, tables D and F, and are unchanged.

---

## 5. Third-party retail Marketplace apps: screening [new 2026-10-15]

The brief asked about "marketplace apps relevant to retail", which the 2026-10-14 work did not cover. This is a screening, not a full evaluation.

The Frappe Cloud Marketplace has a Retail category. A "POS" search returns about 75 results (SS, [marketplace](https://cloud.frappe.io/marketplace)). Marketplace apps must be MIT- or GPL-compatible (SS, [docs](https://docs.frappe.io/cloud/marketplace.md)).

| App | What it is | Evidence | Verdict | Reason |
| --- | --- | --- | --- | --- |
| [POSNext](https://cloud.frappe.io/marketplace/apps/pos_next) (BrainWise) | ERPNext POS replacement; offline mode, MENA focus; listings claim v16/v15 | Repo `BrainWise-DEV/POSNext`, AGPL-3.0, pushed 2026-10-14 (S); listing (SS) | **REJECT** | Makes ERPNext the cashier and sale authority. That violates CLAUDE.md §4 ("ERPNext POS is reference behavior only"), `Kemetra/POS` ownership of the cashier workflow, and Catalog Authority §5. This is consistent with RT-231's mobile decision (Marketplace apps are reference-only for mobile clients). |
| [Easy POS](https://cloud.frappe.io/marketplace/apps/easy_pos) | Replaces the ERPNext POS screen; claims v16/v15 | Listing (SS) | **REJECT** | Same reason. |
| POS Awesome (v15 fork `defendicon/POS-Awesome-V15`) | Vue POS on ERPNext | GPL-3.0, active, pushed 2026-10-09 (S); original `yrestom/POS-Awesome` moved | **REJECT** (reference only) | Same reason. Useful only as UX reference for cashier flows, consistent with "ERPNext POS is reference behavior". |
| GETPOS (NestorBird) | Cloud POS, multi-store | Listing (SS) | **REJECT** | Same reason, plus a second catalog/price authority. |
| URY (`ury-erp/ury`) | Restaurant POS on ERPNext | AGPL-3.0, active (S) | **REJECT** | Restaurant domain; same POS-authority conflict. |
| eCommerce Integrations (Shopify, etc.) | ERPNext ↔ store sync | Carried from comment 2/3 | **REJECT** | Makes ERPNext a second catalog/order authority. |
| Egypt e-invoice / e-receipt apps | Fiscal | — | **Not re-evaluated** | Owned by RT-195 (Egypt Compliance), which has a decision; not duplicated here. |

**Rule of thumb confirmed:** any retail Marketplace app that puts a cashier, price list or item master into ERPNext as the operational authority is architecturally excluded, regardless of version or license.

---

## 6. Proposed bounded PoC for the top candidate [carried + delta]

This is a **draft for the owner. No Jira item has been created.**

- **Title:** Verification — Platform support desk PoC: Frappe Helpdesk vs Jira Service Management Free (disposable lab, internal only).
- **Repos:** none. No repo, contract, migration or Backend-Core change.

**Lab**

- A separate disposable site, never on the posting ERPNext site or bench, and **without ERPNext**.
- Pinned versions: Frappe v16 latest stable (v16.52.0 today, re-checked at execution) + **Helpdesk v1.31.0** + Telephony pinned by SHA.
- Our own pinned image built from the frappe_docker recipe, not `ghcr.io/frappe/helpdesk:stable`, which is built on v15.

**Data:** synthetic only. Two HD Customers, each with a manager and an ordinary contact, plus two agents.

**Scenarios**

- email-to-ticket;
- SLA on Africa/Cairo hours;
- assignment rule;
- canned replies;
- five Arabic KB articles;
- agent 2FA;
- portal login;
- Arabic/RTL user;
- ticket number copied into the RT-414 `ticket_ref` field;
- backup and restore;
- one minor-tag upgrade (it crosses the `#3700` schema refactor if started from v1.30.x);
- RAM and CPU measurement.

**Gates**

| Gate | Pass criterion | On fail |
| --- | --- | --- |
| **G0 v16 install (hard)** | Clean install and migrate on pinned v16; Helpdesk server tests run on v16, failures recorded | HOLD Helpdesk; continue JSM leg only |
| G1 Isolation (hard) | 0 cross-customer visibility; a non-manager sees only own tickets. Re-test after #3806/#3821 | No-go |
| G2 Arabic | Core agent and portal flows render RTL; gaps closable through Translation records without forking | No-go for the portal; agent-only may continue |
| G3 Operations | Usable in ≤ 1 day; backup/restore proven; one clean minor upgrade; fits a 4 GiB VM | Hold |
| G4 Comparison | Beats JSM Free on at least one owner-weighted criterion (Arabic portal, cost beyond 3 agents, data residency); JSM's advantage is no Frappe-version coupling | Choose JSM Free or Jira + email |
| G5 Privacy | Owner approves ticket-content rules (no retail PII or secrets, retention, role access), consistent with RT-391/RT-414 | No-go |

- **Effort:** 2–3 lab days.
- **Rough TCO if adopted (self-hosted):** ≈ €7.09/mo VM (RT-315 price basis) + 2–4 h/mo upkeep (NV).
- **Comparators:**
  - Frappe Cloud: the lowest plan that permits Helpdesk v1.31.0 **and** its `telephony` dependency. Record that plan's price and limits during the PoC.
    - The $5 Tiny Plan allow-lists Helpdesk but not Telephony (S); whether Frappe Cloud installs the dependency anyway is NV.
    - The Helpdesk pricing page claims "unlimited tickets, agents, and customers" on all plans (S);
  - JSM Free $0 for 3 agents (SS).
- **Decision output:** ADOPT CANDIDATE / HOLD / REJECT, with exactly one next safe action.

---

## 7. Conflicts with RT-201 and other work [carried + delta]

### RT-201

- **No product apps in RT-201's lab.** Co-installing them would contaminate V1–V3 evidence.
- V0 should pin the latest coherent stable set at execution, now **Frappe v16.52.0 + ERPNext v16.51.0** (was ERPNext v16.50.0). The comment pin ERPNext v16.37.0 / Frappe v16.36.1 is superseded; v16.37.0 is the same commit as v16.49.0.
- If HRMS may co-reside, the HRMS tag must track the ERPNext tag (both v16.51.0 today).
- **[new] ERPNext v16.51.0 release-note items on the Connector's paths.** These come from the official release body (S), not from a runtime check. RT-201 V3/V4 should include them if v16.51.0+ is pinned:
  - *Not applicable to the Connector:* "standalone" Sales Invoice returns are now valued at the current stock valuation rate ([#60615](https://github.com/frappe/erpnext/pull/60615)). This only covers returns without `return_against`. `frappe_glue._post_reversal` resolves the original invoice and sets `doc_payload["return_against"]` before inserting every void and partial return (`connector/posting/frappe_glue.py:726-756`), so neither Connector path takes the changed branch. Keep it out of RT-201's V3/V4 scope.
  - **Returns no longer carry over or auto-allocate advances** (Sales Invoice / POS Invoice / Purchase Invoice returns).
  - **"Item details and prices now account for the transaction type; returns can use their original price list only when a party is selected."** This touches the Pricing 657 representation-protection regression (RT-283).
  - **Sales Invoice stock quantities are rounded to stock-ledger precision**; entries that round to zero are skipped. This touches UOM/precision gate 663.
  - **Toggling Disable Rounded Total recalculates** Rounded Total and Rounding Adjustment immediately.
- [new] Frappe `v15.123.1` exists (S), so Helpdesk v1.31.0's new floor (`>=15.123.1`) is installable on a released v15 tag. That is moot for the PoC, which targets v16.
- **Static V4 inputs** (`main@65a002a`, unchanged and not verified):
  - `item_wise_tax_detail` is not referenced;
  - `frappe.db.commit()` appears only in the scheduler path and in migrate, not in doc hooks;
  - 14 `get_all`/`get_list` call sites with only 4 explicit `order_by`. v16 changes the default sort to `creation`.

### Other work

| Item | Interaction |
| --- | --- |
| RT-200 | No `doc_events` (F3); webhooks are non-durable (F7) |
| RT-195 | Egypt Compliance; not duplicated |
| RT-391 / RT-414 | Only the existing optional `ticket_ref` is used |
| RT-32 / RT-36 | NocoBase rejection; the same second-login / second-app concerns apply. Do not replace Admin-Console with Frappe products. |
| RT-480 | PostHog is product telemetry, not BI; no overlap with Insights |
| RT-231 | Marketplace apps are reference-only for mobile |
| RT-315 / RT-317 | The ERP VM is sized for ERPNext + Connector only; Frappe Cloud is a reopen-condition alternative |

**No duplicate implementation tickets were created.**

---

## 8. Still open (Needs verification)

1. Helpdesk and CRM (ERPNext link) on v16: no upstream CI.
2. Frappe Cloud:
   - whether the Connector, as a private custom app, can run on a Sites plan;
   - the Servers floor ($20 or $40: the official pages disagree);
   - regions, Egypt availability and offsite-backup region (the data-residency page returned 404).
3. JSM Free terms: confirm on the live Atlassian pricing page during the PoC. Also confirm packaging: one third-party source (Sept 2026) says JSM is now sold only inside the Service Collection, while Atlassian's site still lists it as a standalone product.
4. Arabic/RTL usability per app, in a lab.
5. HRMS Egypt payroll/social insurance.
6. Insights against an RLS schema through a read-only role.
7. Clerk or another IdP through Social Login Key; HS256-only ID tokens.
8. Suite GA; SCIM.
9. ERPNext tenancy model for any HRMS placement.

## 9. Next safe action

The owner decides whether to authorize the drafted **Helpdesk vs JSM Free support-desk PoC (with G0 v16 gate)** as a separate Verification Jira item. Until then, no Frappe product is installed anywhere.
