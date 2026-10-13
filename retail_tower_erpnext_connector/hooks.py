app_name = "retail_tower_erpnext_connector"
app_title = "Retail Tower ERPNext Connector"
app_publisher = "Retail Tower OS"
app_description = (
	"Integration layer between Retail Tower OS (via Data-Pulse-2) and ERPNext / Frappe. "
	"Foundation only: app scaffold and Connector Settings placeholder. No business mutation."
)
app_email = "admin@rahmaqanater.org"
app_license = "Apache-2.0"

# ERPNext is the ERP/accounting/inventory backend this connector integrates with.
# Declaring it here ensures the app cannot be installed on a site without ERPNext.
# This declares the dependency; the supported VERSION range is documented as policy
# (docs/decisions/version-pin-upgrade-policy.md), not enforced as install-blocking logic.
required_apps = ["erpnext"]

# -----------------------------------------------------------------------------
# Foundation scope (spec 001) registered NO hooks. Spec 006 (sales posting),
# against its reviewed contracts, introduces the first runtime registrations:
#   - fixtures: provenance Custom Fields on Sales Invoice (audit linkage +
#     idempotency key; F-009 — frappe silently drops undeclared fields).
#   - scheduler_events: the posting poller (pull -> post -> ack).
# No doc_events / override_doctype_class are registered (the connector posts via
# its own scheduled worker; it does not intercept ERPNext document events).
# See the constitution (Principle VII) and specs/006-sales-posting-adapter/.
# -----------------------------------------------------------------------------

# Export ONLY the connector's own provenance Custom Fields (scoped by name), never the
# whole Custom Field table — so fixtures sync touches nothing else on the site.
fixtures = [
	{
		"dt": "Custom Field",
		"filters": [
			[
				"name",
				"in",
				[
					"Sales Invoice-rt_source_system",
					"Sales Invoice-rt_external_id",
					"Sales Invoice-rt_sale_ref",
					# RT-16 / RT-14 D6 (authorized, decision 10343): the sale-line identity a
					# partial return is matched on.
					"Sales Invoice Item-rt_line_ref",
				],
			]
		],
	}
]

# RT-58 — Gate G5 exactly-once indexes must exist on EVERY site. Frappe's install_app marks all
# patches completed WITHOUT running them (RT-54 root cause), so the patch-only indexes were absent
# on fresh installs. These idempotent hooks ensure both indexes on install and on every migrate
# (fail-loud if one cannot be created); the patches remain for the upgrade path.
after_install = "retail_tower_erpnext_connector.connector.schema.after_install"
after_migrate = "retail_tower_erpnext_connector.connector.schema.after_migrate"

# The posting poller — pulls posting work-items from Data-Pulse-2, posts each to ERPNext,
# and acks the outcome (T093). The job entrypoint lives in the posting module. It runs every
# minute (Frappe's finest cron granularity) for the RT-45 target of a sale reaching ERPNext in
# <= ~1 minute; each run drains the whole pending backlog, and Frappe never overlaps two runs.
scheduler_events = {
	"cron": {
		"* * * * *": [
			"retail_tower_erpnext_connector.connector.posting.poller.run_posting_poll",
		],
		"*/5 * * * *": [
			# The 019 bin-view poller — pulls wanted Bin-view reads from Data-Pulse-2,
			# reads the live ERPNext Bin on-hand per warehouse, and reports the snapshot
			# back (feeds the 017 stock reconciliation). On-hand QUANTITY only; no valuation.
			"retail_tower_erpnext_connector.connector.bin_view.poller.run_bin_view_poll",
		],
	}
}
