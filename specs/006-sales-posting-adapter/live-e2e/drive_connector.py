# Tier-2 live-flow e2e — connector DRIVE harness (runs INSIDE the frappe bench).
#
# Constructs the real connector path inline (NOT the gated poller stubs): a requests-backed
# status-aware HttpTransport -> PostingFeedClient -> frappe_glue.post_work_item, with the
# FrappePostingLogStore (Gate G5), a UomMap, and a PreResolvedWarehouse. This is the
# connector-side e2e harness the gated poller would otherwise own (see tier2-e2e-milestone memory).
#
# Run inside the bench — `bench execute` imports this module and calls main() with real module
# scope + a frappe site/db context (NOT `bench console < file`, which runs as IPython cells and
# does NOT reliably invoke an entry point — it validates nothing). First copy this file into the
# bench app dir so it is importable, then:
#   docker cp drive_connector.py \
#     devcontainer-frappe-1:/workspace/development/frappe-bench/apps/retail_tower_erpnext_connector/retail_tower_erpnext_connector/connector/posting/drive_connector.py
#   docker exec -e RT_E2E_MODE=reject -i devcontainer-frappe-1 bash -lc \
#     'cd /workspace/development/frappe-bench && bench --site retail.localhost execute \
#        retail_tower_erpnext_connector.connector.posting.drive_connector.main'
# (The `if __name__ == "__main__"` guard below also lets it run as a plain script under a
#  frappe-initialised interpreter; it does NOT fire on the `bench execute` import path.)
#
# RT_E2E_MODE env: "reject" (faithful, default: empty store->Customer map, so the post fails closed
# on UnmappedStore -> validation) or "posted" (maps the seeded store to RT_E2E_CUSTOMER; F-009: the
# customer comes from the operator map, the company is the site default set by post_work_item).

import os

import requests

from retail_tower_erpnext_connector.connector.posting import frappe_glue
from retail_tower_erpnext_connector.connector.posting.transport import PostingFeedClient
from retail_tower_erpnext_connector.connector.posting.frappe_store import FrappePostingLogStore
from retail_tower_erpnext_connector.connector.posting.tender import TenderModeMap
from retail_tower_erpnext_connector.connector.posting.uom import (
    PreResolvedWarehouse,
    StoreCustomerMap,
    UomMap,
)

# NOTE: a temporary _shim_dp2_erpnext_item_ref() used to wrap DP2's bare-string `erpnextItemRef`
# into the 012 object {doctype,name} (issue #506). REMOVED once DP2 PR #508 merged — DP2 now serves
# the correct object shape, so the connector parses the REAL wire with no compensation.


class _Resp:
    """Status-aware response for transport.post (the client reads .status/.headers/.body)."""

    def __init__(self, r):
        self.status = r.status_code
        self.headers = dict(r.headers)
        try:
            self.body = r.json()
        except Exception:
            self.body = {}


class RequestsTransport:
    """Real HTTP transport to DP2 over the spec-003 connectorBearer (Authorization: Bearer)."""

    def __init__(self, base, token):
        self._base = base.rstrip("/")
        self._auth = {"Authorization": "Bearer " + token}

    def get(self, path, *, params, headers):
        r = requests.get(
            self._base + path, params=params, headers={**self._auth, **headers}, timeout=15
        )
        r.raise_for_status()
        return r.json()  # DP2 serves the contract-correct erpnextItemRef object (post-#508)

    def post(self, path, *, json, headers):
        # Return a status-aware object so PostingFeedClient._interpret_ack can read 201/200/409/404.
        r = requests.post(
            self._base + path, json=json, headers={**self._auth, **headers}, timeout=15
        )
        return _Resp(r)


def main():
    DP2_BASE = os.environ.get("RT_E2E_DP2_BASE", "http://172.20.0.1:3000")
    TOKEN = os.environ.get("RT_E2E_TOKEN", "rt_e2e_connector_token_0123456789abcdefghijABCD")
    STORE_ID = "22222222-2222-2222-2222-222222222222"
    MODE = os.environ.get("RT_E2E_MODE", "reject")

    print("=== RT connector live-flow drive | mode=%s | dp2=%s ===" % (MODE, DP2_BASE))
    correlation_id = "e2e-correlation-0001"  # stable X-Request-Id for the run (Principle V)

    transport = RequestsTransport(DP2_BASE, TOKEN)
    client = PostingFeedClient(transport, correlation_id=correlation_id)
    store = FrappePostingLogStore()
    # "each" must map (else build fails as UnmappedUnit before reaching submit). Use a UOM that
    # exists on the bench. In reject-mode the store->Customer map below is empty, so the connector
    # fails closed on UnmappedStore before any Sales Invoice is submitted: the faithful reject path.
    uom_map = UomMap({"each": "Nos"})
    warehouses = PreResolvedWarehouse({STORE_ID: {"doctype": "Warehouse", "name": "Stores - E2E"}})

    # Customer resolution (F-009): the builder needs a store->Customer map and never fabricates one.
    # Posted-mode maps the seeded store to a bench Customer; reject-mode leaves the map empty so
    # the post fails closed on UnmappedStore (the faithful reject path). The company is the site
    # default (Global Defaults), set by post_work_item itself (RT-378) -- no harness injection.
    customer = os.environ.get("RT_E2E_CUSTOMER", "E2E Customer")
    customers = StoreCustomerMap({STORE_ID: customer} if MODE == "posted" else {})
    # The seeded sale carries no tenders; the map is only consulted for a tender-bearing sale.
    tenders = TenderModeMap({"cash": os.environ.get("RT_E2E_CASH_MODE_OF_PAYMENT", "Cash")})

    # --- Pull one page, post the single work-item, observe the outcome. ------------------------
    page = client.pull_postings(since=None, limit=100)
    print("PULLED items=%d cursor=%s next=%s" % (len(page.items), page.cursor, page.next_page_token))
    if not page.items:
        # FAIL LOUD: a run that pulls nothing validated nothing. Exiting 0 here would let an
        # operator record a green "validation" that never pulled/posted/acked (the seed was not
        # applied, or the row is already terminal). Raise so the harness exits nonzero.
        raise SystemExit(
            "FAILED: feed returned 0 pending items — nothing was validated. "
            "Apply dp2-seed.sql (fresh posting id) before running."
        )

    wi = page.items[0]
    print("WORK ITEM ref=%s kind=%s lines=%d item0=%s" % (
        wi.work_item_ref, wi.kind, len(wi.sale.lines), wi.sale.lines[0].erpnext_item_ref.name))

    outcome = frappe_glue.post_work_item(
        wi,
        client=client,
        store=store,
        uom_map=uom_map,
        warehouses=warehouses,
        customers=customers,
        tenders=tenders,
        correlation_id=correlation_id,
    )
    print("OUTCOME=%s" % outcome)

    # --- G5 replay (posted-mode only): drive the SAME wi a SECOND time IN-PROCESS. The feed only
    # serves `pending` rows, so a re-pull would return nothing — replay must be exercised by
    # re-posting the same work-item. post_work_item's replay guard (store.get_document_ref(key))
    # must echo the existing documentRef and post NO second Sales Invoice (Gate G5 — exactly-once).
    # The second ack reuses key {workItemRef}:posted with the same body → DP2 returns
    # 200 + Idempotent-Replayed:true (the ack-replay path). Proof is asserted at the ERPNext SOURCE
    # below: exactly ONE Sales Invoice for this rt_external_id. ----------------------------------
    if MODE == "posted":
        if outcome != "posted":
            raise SystemExit("FAILED: posted-mode first post returned %r, expected 'posted'" % outcome)
        replay_outcome = frappe_glue.post_work_item(
            wi, client=client, store=store, uom_map=uom_map,
            warehouses=warehouses, customers=customers, tenders=tenders,
            correlation_id=correlation_id,
        )
        print("REPLAY_OUTCOME=%s" % replay_outcome)
        if replay_outcome != "posted":
            raise SystemExit("FAILED: G5 replay returned %r, expected 'posted' (echo)" % replay_outcome)
        # Source-of-truth G5 assertion: exactly ONE Sales Invoice for this sale's external_id.
        import frappe  # available under bench execute
        si_count = frappe.db.count("Sales Invoice", {"rt_external_id": wi.external_id})
        print("SALES_INVOICES_FOR_%s=%d (expect exactly 1)" % (wi.external_id, si_count))
        if si_count != 1:
            raise SystemExit(
                "FAILED: G5 violated — %d Sales Invoices for rt_external_id=%s (expected exactly 1; "
                "the replay must echo, never post a duplicate)." % (si_count, wi.external_id)
            )
        print("=== G5 OK: replay echoed the documentRef; exactly one Sales Invoice exists ===")

    # Confirm DP2 recorded the outcome (the feed should no longer offer this ref).
    after = client.pull_postings(since=None, limit=100)
    still = any(i.work_item_ref == wi.work_item_ref for i in after.items)
    print("DP2 feed still offers this ref after ack? %s (expect False)" % still)
    if still:
        # FAIL LOUD: the terminal ack did NOT remove the work-item from DP2's pending feed, so the
        # terminal transition the runbook claims was NOT validated (e.g. a stale-key 409 isolated by
        # the worker, or the row never transitioned). Do not print DONE and exit 0 — raise.
        raise SystemExit(
            "FAILED: outcome=%s but DP2 still offers %s as pending — the terminal transition was "
            "NOT recorded (likely a stale ack idempotency key; re-seed with a fresh posting id)."
            % (outcome, wi.work_item_ref)
        )
    print("=== DONE: outcome=%s, feed_cleared=True ===" % outcome)


# `bench execute ...drive_connector.main` imports this module and calls main() itself — the guard
# does NOT fire on that import path (so main() never runs twice). The guard only triggers when the
# file is run as a plain script under a frappe-initialised interpreter.
if __name__ == "__main__":
    main()
