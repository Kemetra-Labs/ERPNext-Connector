# ERPNext ↔ Retail Tower DocType Mapping Reference

**Spec**: 002 — DocType Mapping Reference | **Status**: Draft (pending decision sign-offs) | **Date**: 2026-06-04

This is the reviewed reference for how each **ERPNext** concept corresponds to a **Retail
Tower** concept, as modeled in the **Data-Pulse-2** backend. It is the agreed vocabulary that
unblocks the downstream connector specs (003 auth, 004 product export, 005 inventory, 006
sales posting, 007 tax). It is a documentation + decision artifact only — no connector code
(spec FR-008, constitution Principle VII).

---

## How to read this matrix

Each row records:

| Column | Meaning |
|--------|---------|
| **ERPNext concept** | The ERPNext-side concept being mapped. |
| **Retail Tower counterpart** | The Data-Pulse-2 entity or contract that models it. |
| **Owner of truth** | Which system is authoritative for that concept. |
| **Status** | `Resolved` · `Resolved (reference only)` · `Decision needed` · `Deferred → NNN`. |
| **Data-Pulse-2 source** | A locatable path in the Data-Pulse-2 repo confirming the counterpart. |

## Sources & precedence

- **Data-Pulse-2 (DP2) is authoritative for the Retail Tower side of every mapping**
  (spec FR-003, constitution Principle I). This reference *cites* DP2's model; it does not
  re-derive it. DP2 repo: `C:\Users\user\Documents\GitHub\Data-Pulse-2`.
- **Within DP2, code and contracts outrank prose.** Where DP2's Drizzle schema
  (`packages/db/src/schema/`), SQL migrations (`packages/db/drizzle/`), or OpenAPI contracts
  (`packages/contracts/openapi/`) diverge from DP2 prose docs, the schema/contract wins and
  the prose is treated as superseded (see "Superseded sources" below).
- **ERPNext documents are addressed only via the generic `{doctype, name}` reference** that
  DP2's connector contract exposes (`ErpnextDocumentRef`), never via ERPNext field-level
  names (spec FR-004, constitution Principle II). This preserves version-independence: a
  future ERPNext version change must not invalidate this mapping at the field level.
- **Concept altitude only.** This reference maps *concepts*. Field-by-field ERPNext document
  construction is out of scope and owned by spec 006 (sales posting) and later (spec FR-010).

---

## Mapping Matrix

| ERPNext concept | Retail Tower counterpart (Data-Pulse-2) | Owner of truth | Status | Data-Pulse-2 source |
|-----------------|------------------------------------------|----------------|--------|---------------------|
| **Company** | `tenants` table | DP2 | Resolved | `packages/db/src/schema/tenants.ts` |
| **Warehouse** | `stores` table (store / branch) | DP2 | Resolved | `packages/db/src/schema/stores.ts` |
| **Item** | `tenant_products` (with `global_products`, `store_product_overrides`) | DP2 owns the product record; ERPNext owns Item identity for posting | Resolved | `packages/db/src/schema/catalog/tenant-products.ts` |
| **Item Barcode** | `product_aliases` where `identifier_type = 'barcode'` | DP2 | Resolved | `packages/db/src/schema/catalog/product-aliases.ts` |
| **UOM** | free-text `unit` (sale lines) / `stocking_unit` (inventory) — **no UOM master entity** | DP2 (free text); connector maps unit→ERPNext UOM (Option A, signed) | **Signed** → [`mapping-uom.md`](../decisions/mapping-uom.md) (connector-side unit→UOM map, unmapped fails closed) | `packages/db/src/schema/sales/sale-lines.ts`, `packages/db/src/schema/inventory/stock-movements.ts` |
| **Price List** | `price_history` (interval-versioned) + `default_price` on `tenant_products` — **no Price List entity** | DP2 amounts are authoritative; ERPNext Price List is a document-validity reference only | Resolved (reference only) | `packages/db/src/schema/catalog/price-history.ts` |
| **POS Invoice / Sales Invoice** | `sales` (immutable header) + `sale_lines` (frozen snapshots) | DP2 owns the sale fact; ERPNext owns the posted accounting document | Resolved | `packages/db/src/schema/sales/sales.ts`, `packages/db/src/schema/sales/sale-lines.ts` |
| **Payment Entry / Mode of Payment** | not modeled as a DP2 table; `sales.pos_total` is the sale total, **not** tender | ERPNext (tender not modeled in DP2) | **Deferred → 006** (sales posting / tender) | `packages/db/src/schema/sales/sales.ts` (no tender columns — Payment Entry absent from DP2 schema; the only tender-adjacent surface is the POS voucher contract `packages/contracts/openapi/pos-payments/vouchers.yaml`) |
| **Return Invoice / Refund** | `sale_refunds` + `sale_voids` (append-only terminal events; never mutate the sale) | DP2 | Resolved | `packages/db/src/schema/sales/sale-terminal-events.ts` |
| **Customer** | not modeled in DP2 (walk-in retail) | ERPNext-owned; default walk-in Customer per POS Profile (Option A, signed) | **Signed → 006** → [`mapping-customer.md`](../decisions/mapping-customer.md) (configured default walk-in Customer) | (absent in DP2 — confirmed by survey; no schema file) |

### Product → ERPNext Item correlation (concept level)

The link between a Retail Tower product and an ERPNext Item is owned by Data-Pulse-2 via the
**`erpnext_item_map`** table (migration `0017`), not by the connector (constitution
Principle I). At concept level (field-level posting is spec 006):

- `tenant_product_id` → `tenant_products` (FK); `erpnext_item_ref` is **text, no FK** (ERPNext
  is external and version-independent).
- `state ∈ {suggested, confirmed}` — suggest-then-confirm; **only `confirmed` rows are
  resolvable for posting**.
- A 1:1 active invariant holds per `(tenant, tenant_product)` (re-point is append-only:
  retire + insert).
- **Source**: `packages/db/src/schema/catalog/erpnext-item-map.ts`,
  `packages/db/drizzle/0017_erpnext_item_map.sql`.

### Store → ERPNext Warehouse authority (RT-290 M-2/M-3/M-10, RT-434)

- **Authority.** Backend-Core's `erpnext_warehouse_map` is the warehouse mapping authority. Since
  the frozen resolution (RT-331, posting feed 012 `Sale.warehouseRef`), a work item carries its
  Warehouse on the feed and the Connector posts to it.
- **Local fallback only.** The Connector Settings warehouse map (`RT Warehouse Map Row`) applies
  only to a work item with no `warehouseRef`, that is, an intent created before the freeze. It will
  be retired once no pre-freeze intents remain; do not treat it as a second source of truth.
- **One store per Warehouse.** A Warehouse belongs to at most one store. The local map rejects a
  Warehouse listed for two stores (`config.parse_warehouse_map` raises a `ConfigError` naming the
  stores, and the poll tick is skipped). RT-433 (pending) adds the same rule to Backend-Core's map.
- **Feed refs are checked per item.** The compatibility preflight checks the local map's
  Warehouses before a poll tick. A feed `warehouseRef` is checked for each sale before insert with
  the same rules (`warehouse_policy`): a group Warehouse, a Warehouse of another company or a missing
  Warehouse is rejected as `validation`, and no ERP document is created. A void or return uses the
  original invoice rows' warehouses and is not re-checked.

### ERPNext document addressing

ERPNext documents (the *result* of posting) are referenced only via the generic
`ErpnextDocumentRef = { doctype, name }` schema in the DP2 connector contract — e.g.
`{ "doctype": "Sales Invoice", "name": "<erp-doc-id>" }`. No ERPNext field-level names appear
in this reference (FR-004, Principle II).

- **Source**: `packages/contracts/openapi/erpnext-connector/posting-feed.yaml`
  (`ErpnextDocumentRef`, `required: [doctype, name]`); connector endpoints
  `GET /api/connector/v1/erpnext/postings` and
  `POST /api/connector/v1/erpnext/postings/{workItemRef}/outcome`.

---

## Multi-tenancy alignment

Data-Pulse-2 isolates tenants by **per-row `tenant_id` + PostgreSQL Row-Level Security**
(sales/inventory tables use RLS ENABLE + FORCE, fail-closed on empty tenant GUC). The
connector's principal is tenant-scoped; tenant/store derive from the authenticated scope,
never from request bodies. This aligns with the connector foundation's site-scoped tenancy
(spec 001) and constitution Principle I.

---

## Superseded sources

- Data-Pulse-2's `docs/ROADMAP-ERP.md` is **self-flagged stale** (its erratum notes the
  008→012 numbering is superseded and reclaims 011–017 as future ERPNext-arc identifiers).
  Where it diverges from the DP2 schema and the connector contract, the schema/contract is
  authoritative (FR-007).
- The 011–017 spec directories named in that roadmap erratum are **future/claimed
  identifiers — they do not yet exist** in the DP2 repo. This reference therefore cites only
  DP2 **code and contracts** (the Drizzle schema under `packages/db/src/schema/` and the
  OpenAPI contracts under `packages/contracts/openapi/`) as primary and authoritative; no
  DP2 prose mapping doc is relied upon, consistent with the code-over-prose precedence above.

## Downstream gating

| Concept | Status | Blocks |
|---------|--------|--------|
| UOM | ✅ Signed (Option A, 2026-06-04) | Was blocking specs 004 & 006 — now **unblocked**: connector-side unit→UOM map, unmapped fails closed. See [`mapping-uom.md`](../decisions/mapping-uom.md). |
| Customer | ✅ Signed (Option A, 2026-06-04) | Was blocking spec 006 — now **unblocked**: configured default walk-in Customer (ERPNext-owned). See [`mapping-customer.md`](../decisions/mapping-customer.md). |
| Payment Entry | Deferred → 006 | (no decision record; owned by spec 006) |

> **Note**: "Customer" is included as a concept surfaced by the Data-Pulse-2 survey (DP2 has
> no Customer entity for walk-in retail), beyond the spec's core enumerated list — recorded
> here for completeness because sales posting (006) will need a resolution.
