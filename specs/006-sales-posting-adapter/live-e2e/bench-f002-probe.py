# Gate G5 / F-002 crash-window probe — runs INSIDE the configured frappe bench.
#
# F-002: a crash BETWEEN sinv.submit() and store.record_posted() leaves no Posting Log row, so a
# DP2 re-offer re-enters post_work_item with an empty replay guard and submits a SECOND Sales
# Invoice — a duplicate (exactly-once violated). This probe reproduces that on real frappe and
# demonstrates the fix in three states (see specs/006 wave-status):
#
#   1. NO index            -> re-post after simulated crash => TWO SIs   (F-002 reproduced = RED)
#   2. index, no glue catch -> dup-key hits except Exception -> _reject  => ONE SI but FALSE reject
#   3. index + glue catch  -> dup-key -> resolve to existing SI, ack posted => ONE SI, posted (GREEN)
#
# The active state is whatever the current code + DB index are; this probe just observes + asserts.
# Run:
#   bench --site retail.localhost execute \
#     retail_tower_erpnext_connector.connector.posting.bench_f002_probe.main
#
# RT_E2E_EXPECT in {two_si, false_reject, one_si_posted} selects the assertion for the current state.

import os

import frappe

from retail_tower_erpnext_connector.connector.posting import frappe_glue
from retail_tower_erpnext_connector.connector.posting.transport import PostingFeedClient
from retail_tower_erpnext_connector.connector.posting.frappe_store import FrappePostingLogStore
from retail_tower_erpnext_connector.connector.posting.tender import TenderModeMap
from retail_tower_erpnext_connector.connector.posting.uom import (
    PreResolvedWarehouse,
    StoreCustomerMap,
    UomMap,
)

DP2_BASE = os.environ.get("RT_E2E_DP2_BASE", "http://172.20.0.1:3000")
TOKEN = os.environ.get("RT_E2E_TOKEN", "rt_e2e_connector_token_0123456789abcdefghijABCD")
STORE_ID = "22222222-2222-2222-2222-222222222222"
EXPECT = os.environ.get("RT_E2E_EXPECT", "two_si")

import requests


class _Resp:
    def __init__(self, r):
        self.status = r.status_code
        self.headers = dict(r.headers)
        try:
            self.body = r.json()
        except Exception:
            self.body = {}


class RequestsTransport:
    def __init__(self, base, token):
        self._base = base.rstrip("/")
        self._auth = {"Authorization": "Bearer " + token}

    def get(self, path, *, params, headers):
        r = requests.get(self._base + path, params=params, headers={**self._auth, **headers}, timeout=15)
        r.raise_for_status()
        return r.json()

    def post(self, path, *, json, headers):
        return _Resp(requests.post(self._base + path, json=json, headers={**self._auth, **headers}, timeout=15))


def main():
    print("=== F-002 crash-window probe | expect=%s ===" % EXPECT)
    transport = RequestsTransport(DP2_BASE, TOKEN)
    client = PostingFeedClient(transport, correlation_id="f002-probe")
    store = FrappePostingLogStore()
    uom_map = UomMap({"each": "Nos"})
    warehouses = PreResolvedWarehouse({STORE_ID: {"doctype": "Warehouse", "name": "Stores - E2E"}})
    # F-009: the customer comes from the store->Customer map; the company is the site default
    # (set by post_work_item, RT-378). No builder wrapping.
    customers = StoreCustomerMap({STORE_ID: os.environ.get("RT_E2E_CUSTOMER", "E2E Customer")})
    tenders = TenderModeMap({"cash": os.environ.get("RT_E2E_CASH_MODE_OF_PAYMENT", "Cash")})

    page = client.pull_postings(since=None, limit=100)
    if not page.items:
        raise SystemExit("FAILED: no pending item — re-seed dp2-seed.sql first")
    wi = page.items[0]
    ext = wi.external_id
    print("WORK ITEM ref=%s ext=%s" % (wi.work_item_ref, ext))

    def post():
        return frappe_glue.post_work_item(
            wi, client=client, store=store, uom_map=uom_map,
            warehouses=warehouses, customers=customers, tenders=tenders,
            correlation_id="f002-probe",
        )

    # 1st post — the legitimate one.
    o1 = post()
    print("FIRST_OUTCOME=%s" % o1)

    # SIMULATE THE CRASH: drop the Posting Log row so the replay guard finds nothing on re-offer
    # (exactly the crash-between-submit-and-record state). The SI in ERPNext stays.
    frappe.db.delete("Posting Log", {"source_system": wi.source_system, "external_id": ext})
    frappe.db.commit()
    print("SIMULATED CRASH: deleted Posting Log rows for (%s,%s)" % (wi.source_system, ext))

    # 2nd post — the re-offer after the crash. This is where F-002 bites without the fix.
    try:
        o2 = post()
        print("SECOND_OUTCOME=%s" % o2)
        second_raised = None
    except Exception as exc:  # only to OBSERVE the raw exception in the no-catch state
        o2 = None
        second_raised = "%s.%s: %s" % (type(exc).__module__, type(exc).__name__, str(exc)[:200])
        print("SECOND_RAISED=%s" % second_raised)

    si_count = frappe.db.count("Sales Invoice", {"rt_external_id": ext})
    print("SALES_INVOICES_FOR_%s=%d" % (ext, si_count))

    # Assertion per the state we're demonstrating.
    if EXPECT == "two_si":
        if si_count != 2:
            raise SystemExit("RED EXPECTED 2 SIs (F-002), got %d — bug not reproduced" % si_count)
        print("=== RED CONFIRMED: F-002 reproduced — crash window created a DUPLICATE SI ===")
    elif EXPECT == "false_reject":
        if not (si_count == 1 and o2 == "permanently_rejected"):
            raise SystemExit("EXPECTED 1 SI + false reject, got si=%d o2=%s" % (si_count, o2))
        print("=== index-only: dup blocked at DB but sale FALSELY rejected — part-2 catch needed ===")
    elif EXPECT == "one_si_posted":
        if not (si_count == 1 and o2 == "posted"):
            raise SystemExit("GREEN EXPECTED 1 SI + posted echo, got si=%d o2=%s" % (si_count, o2))
        print("=== GREEN: crash re-offer resolved to the existing SI, acked posted, NO duplicate ===")
