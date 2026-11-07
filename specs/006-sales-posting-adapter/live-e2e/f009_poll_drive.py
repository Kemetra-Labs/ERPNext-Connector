# F-009 acceptance proof (bench-only) — drive the REAL scheduled entrypoint run_posting_poll()
# with NO harness customer injection. The customer comes from Connector Settings (store→Customer
# map). Proves the connector can post in production: config → pull → build (customer from config) →
# submit → ack → DP2 posted.
#
# Copy into the bench app dir, then:
#   bench --site retail.localhost execute
#     retail_tower_erpnext_connector.connector.posting.f009_poll_drive.main
#
# Env (set by the runner; RT_CONNECTOR_ALLOW_INSECURE_DP2=1 is defaulted below, RT-1259): RT_E2E_EXTERNAL_ID = the seed's external_id (E2E-SALE-...), so the
# verification can find the exact SI/posting row this run created.

import os

import frappe

from retail_tower_erpnext_connector.connector.posting import poller

# The seed's fixed identifiers (dp2-seed.sql).
STORE_ID = "22222222-2222-2222-2222-222222222222"
# The bench DP2 is plain http on a non-loopback address; the builder refuses that unless opted in (RT-1259).
os.environ.setdefault("RT_CONNECTOR_ALLOW_INSECURE_DP2", "1")
DP2_BASE = os.environ.get("RT_E2E_DP2_BASE", "http://172.20.0.1:3000")
RAW_TOKEN = os.environ.get("RT_E2E_TOKEN", "rt_e2e_connector_token_0123456789abcdefghijABCD")
EXTERNAL_ID = os.environ.get("RT_E2E_EXTERNAL_ID", "")
SOURCE_SYSTEM = "retail_tower_pos"
# Masters the posted-path session created (preflight confirmed they exist + default company E2E Co).
CUSTOMER = os.environ.get("RT_E2E_CUSTOMER", "E2E Customer")
WAREHOUSE = "Stores - E2E"


def _configure_settings():
    """Set Connector Settings: base-URL, token (Password), and the three maps incl. store→customer.

    This is what an operator does in the UI. The store→customer map is the F-009 closure: the
    connector reads the customer from HERE, not from any harness injection.
    """
    s = frappe.get_single("Connector Settings")
    s.dp2_base_url = DP2_BASE
    s.dp2_token = RAW_TOKEN  # Password field — frappe encrypts on save; poller reads get_password
    s.set("uom_map", [])
    s.append("uom_map", {"dp2_unit": "each", "erpnext_uom": "Nos"})
    s.set("warehouse_map", [])
    s.append("warehouse_map", {"store_id": STORE_ID, "warehouse": WAREHOUSE})
    s.set("store_customer_map", [])
    s.append("store_customer_map", {"store_id": STORE_ID, "customer": CUSTOMER})  # F-009
    s.save()
    frappe.db.commit()
    print("CONFIGURED Connector Settings: base=%s store->customer={%s: %s}" % (DP2_BASE, STORE_ID, CUSTOMER))


def main():
    print("=== F-009 acceptance — run_posting_poll() with NO harness injection ===")
    print("external_id=%r" % EXTERNAL_ID)
    if not EXTERNAL_ID:
        raise SystemExit("FAILED: RT_E2E_EXTERNAL_ID not set — cannot verify the exact SI/row")

    _configure_settings()

    # Reset the cached cursor so the poll pulls from the start (the bench cache may hold a stale
    # cursor from a prior session, which would skip our freshly-seeded pending row).
    frappe.cache().delete_value("rt_posting_cursor")
    # Clear the Single-doctype cache so the poller's fresh get_doc + get_password reads from disk,
    # not a stale in-process cache of the doc we just saved (Single password caching can bite).
    frappe.clear_cache(doctype="Connector Settings")

    # DRIVE THE REAL SCHEDULED ENTRYPOINT. No harness, no monkeypatch, no injected client/maps —
    # run_posting_poll() builds everything from Connector Settings via _build_posting_path().
    print(">>> calling poller.run_posting_poll() <<<")
    poller.run_posting_poll()
    print(">>> run_posting_poll() returned <<<")

    # --- Verify at the ERPNext source (not a harness print). ---
    si_name = frappe.db.get_value(
        "Sales Invoice",
        {"rt_source_system": SOURCE_SYSTEM, "rt_external_id": EXTERNAL_ID, "docstatus": 1},
        "name",
    )
    print("SALES_INVOICE_FOR_%s=%r" % (EXTERNAL_ID, si_name))
    if not si_name:
        # Show the poll's skip/reject signal if no SI — was it config-skipped or rejected?
        raise SystemExit(
            "FAILED: no submitted Sales Invoice for external_id=%s. The poll either skipped "
            "(config) or rejected the work-item. Check the bench error log for "
            "posting.poll.skipped / posting.rejected." % EXTERNAL_ID
        )

    si = frappe.get_doc("Sales Invoice", si_name)
    print("SI customer=%r company=%r docstatus=%s grand_total=%s" % (
        si.customer, si.company, si.docstatus, si.grand_total))
    print("SI rt_source_system=%r rt_external_id=%r rt_sale_ref=%r" % (
        si.get("rt_source_system"), si.get("rt_external_id"), si.get("rt_sale_ref")))

    # F-009 assertions: the customer is the CONFIGURED one (not fabricated, not harness-injected).
    if si.customer != CUSTOMER:
        raise SystemExit("FAILED: SI customer=%r, expected configured %r" % (si.customer, CUSTOMER))
    if si.get("rt_source_system") != SOURCE_SYSTEM or si.get("rt_external_id") != EXTERNAL_ID:
        raise SystemExit("FAILED: provenance custom fields did not land on the SI")
    if si.docstatus != 1:
        raise SystemExit("FAILED: SI not submitted (docstatus=%s)" % si.docstatus)

    # Exactly one SI for this external_id (no duplicate; the poll posts once).
    n = frappe.db.count("Sales Invoice", {"rt_external_id": EXTERNAL_ID})
    print("SI_COUNT_FOR_%s=%d (expect 1)" % (EXTERNAL_ID, n))
    if n != 1:
        raise SystemExit("FAILED: %d Sales Invoices for external_id=%s (expected exactly 1)" % (n, EXTERNAL_ID))

    print("=== F-009 GREEN: run_posting_poll posted SI %s for customer %r (from Connector Settings, "
          "no harness injection); provenance landed; exactly one SI. ===" % (si_name, si.customer))
    print("NEXT: the runner verifies DP2 recorded the row as posted with this documentRef.")
