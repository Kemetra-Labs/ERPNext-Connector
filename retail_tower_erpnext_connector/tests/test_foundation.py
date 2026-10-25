# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Foundation tests for spec 001 — Frappe App Foundation.

These assert against a LIVE installed app and therefore run on a staging ERPNext
bench (``bench --site <site> run-tests --app retail_tower_erpnext_connector``),
not in this repository's local environment.

They enforce, as automated checks, the foundation guarantees from spec 001:
  - T007: the app is installed and carries clear metadata (FR-001, FR-002)
  - T008: the app introduces NO business mutation and does NOT fork ERPNext
          (FR-005, FR-006 — constitution principles VII and II)
  - T011: Connector Settings is a Single DocType resolving to one record (FR-004)

Later specs (006 posting, 019 bin view, RT-321, RT-379, RT-883) added reviewed configuration, mapping
tables and a second poller. The checks below allow exactly those, by name, and nothing else.
"""

import unittest

import frappe

APP_NAME = "retail_tower_erpnext_connector"
MODULE_NAME = "Connector"
SETTINGS_DOCTYPE = "Connector Settings"

# DocType field names that would indicate this app writes ERPNext business data.
# The foundation must own NO DocType resembling these (spec FR-005).
BUSINESS_DOCTYPE_HINTS = ("item", "stock", "price", "sales", "invoice", "warehouse", "bin")

# Connector Settings child tables that MAP Retail Tower ids to ERPNext names (spec 006). They hold
# configuration, not business records, so a hint in their name is expected. A new name here is a
# reviewed decision, never a way to silence the check.
MAPPING_DOCTYPES = frozenset({"RT Warehouse Map Row"})

# The only scheduled jobs the app may register: the spec 006 posting poller and the spec 019
# bin-view poller. Anything else is a new background writer and needs its own review.
ALLOWED_SCHEDULED_JOBS = frozenset(
	{
		"retail_tower_erpnext_connector.connector.posting.poller.run_posting_poll",
		"retail_tower_erpnext_connector.connector.bin_view.poller.run_bin_view_poll",
	}
)

# Connector Settings fields approved by spec 006 (Data-Pulse-2 link, mapping tables), RT-321
# (credential lifecycle), RT-379 (compatibility profile) and RT-883 (the pollers' feed state). A
# field outside this set fails.
APPROVED_SETTINGS_FIELDS = frozenset(
	{
		"dp2_base_url",
		"dp2_token",
		"dp2_connector_registration_id",
		"dp2_credential_id",
		"dp2_credential_issued_at",
		"dp2_credential_expires_at",
		"dp2_credential_warn_days",
		"uom_map",
		"warehouse_map",
		"store_customer_map",
		"tender_mode_map",
		"posting_currency",
		"compatibility_status",
		"compatibility_checked_at",
		"compatibility_fingerprint",
		"compatibility_report",
		"posting_feed_cursor",
		"bin_view_feed_cursor",
		"bin_view_retry_state",
	}
)


class TestFoundationInstall(unittest.TestCase):
	"""T007 — install + metadata (FR-001, FR-002)."""

	def test_app_is_installed(self):
		self.assertIn(
			APP_NAME,
			frappe.get_installed_apps(),
			f"{APP_NAME} should appear in the site's installed apps (FR-001)",
		)

	def test_app_metadata_is_present(self):
		hooks = frappe.get_hooks(app_name=APP_NAME)
		# get_hooks returns lists; take the first entry for scalar identity fields.
		self.assertEqual(_first(hooks.get("app_name")), APP_NAME)
		self.assertTrue(_first(hooks.get("app_title")), "app_title must be set (FR-002)")
		self.assertTrue(_first(hooks.get("app_publisher")), "app_publisher must be set (FR-002)")
		self.assertTrue(_first(hooks.get("app_description")), "app_description must be set (FR-002)")
		self.assertTrue(_first(hooks.get("app_license")), "app_license must be set (FR-002)")


class TestFoundationNoMutationNoFork(unittest.TestCase):
	"""T008 — no business mutation (FR-005) and no ERPNext fork (FR-006)."""

	def test_app_declares_no_business_doctypes(self):
		"""The app must own no DocType that represents product/stock/price/sales data.

		Scans every DocType in every module this app ships (read from the app's
		modules.txt), not just the primary module, so the guarantee holds even if a
		future spec adds a second module.
		"""
		app_modules = frappe.get_all(
			"Module Def",
			filters={"app_name": APP_NAME},
			pluck="name",
		)
		# Fall back to the known module if Module Def lookup returns nothing in a
		# minimal test site (the foundation ships exactly one module: "Connector").
		app_modules = app_modules or [MODULE_NAME]
		app_doctypes = frappe.get_all(
			"DocType",
			filters={"module": ["in", app_modules]},
			pluck="name",
		)
		for dt in app_doctypes:
			if dt in MAPPING_DOCTYPES:
				# The exemption holds only while it stays a Connector Settings child table.
				self.assertTrue(
					frappe.get_meta(dt).istable,
					f"Mapping DocType '{dt}' must stay a child table to be exempt (FR-005)",
				)
				continue
			lowered = dt.lower()
			for hint in BUSINESS_DOCTYPE_HINTS:
				self.assertNotIn(
					hint,
					lowered,
					f"Foundation DocType '{dt}' resembles business data ('{hint}') — "
					f"forbidden at foundation (FR-005, constitution VII)",
				)

	def test_app_registers_no_event_intercept_or_override_hooks(self):
		"""No doc_events / override_doctype_class are registered (FR-005, constitution VII).

		The connector never intercepts ERPNext document events or overrides core classes — it
		posts via its OWN scheduled worker against the reviewed 012 contract. As of spec 006 the
		app DOES register a `scheduler_events` posting poller and `fixtures` (its provenance
		Custom Fields); those are reviewed, contract-bound registrations, so they are no longer
		"forbidden" — only event-interception / class-override remain forbidden.
		"""
		hooks = frappe.get_hooks(app_name=APP_NAME)
		for forbidden in ("doc_events", "override_doctype_class", "override_whitelisted_methods"):
			self.assertFalse(
				hooks.get(forbidden),
				f"Connector must register no '{forbidden}' (FR-005, constitution VII)",
			)

	def test_scheduler_events_only_register_the_reviewed_pollers(self):
		"""Specs 006 and 019: only the posting poller and the bin-view poller are scheduled."""
		hooks = frappe.get_hooks(app_name=APP_NAME)
		scheduler = hooks.get("scheduler_events") or {}
		registered = _flatten_scheduler_jobs(scheduler)
		self.assertIn(
			"retail_tower_erpnext_connector.connector.posting.poller.run_posting_poll",
			registered,
			"the posting poller scheduler_event must be registered (spec 006)",
		)
		for job in registered:
			self.assertIn(
				job,
				ALLOWED_SCHEDULED_JOBS,
				f"Unexpected scheduled job '{job}' — only the reviewed pollers are allowed (specs 006, 019)",
			)

	def test_app_does_not_fork_erpnext(self):
		"""FR-006 / constitution II: the app embeds no copied ERPNext or Frappe core module."""
		import os

		import retail_tower_erpnext_connector

		app_root = os.path.dirname(os.path.abspath(retail_tower_erpnext_connector.__file__))
		forbidden_dirs = {"erpnext", "frappe"}
		for dirpath, dirnames, _filenames in os.walk(app_root):
			for d in dirnames:
				self.assertNotIn(
					d,
					forbidden_dirs,
					f"App embeds a '{d}/' directory under {dirpath} — ERPNext/Frappe "
					f"core must not be copied into this app (FR-006, constitution II)",
				)


class TestConnectorSettingsSingleton(unittest.TestCase):
	"""T011 — Connector Settings is a Single DocType resolving to one record (FR-004)."""

	def test_connector_settings_is_single(self):
		meta = frappe.get_meta(SETTINGS_DOCTYPE)
		self.assertTrue(
			meta.issingle,
			f"{SETTINGS_DOCTYPE} must be a Single DocType (FR-004, research Decision 1)",
		)

	def test_connector_settings_resolves_to_one_record(self):
		# A Single DocType always resolves to exactly one cached document named after itself.
		doc = frappe.get_single(SETTINGS_DOCTYPE)
		self.assertEqual(doc.name, SETTINGS_DOCTYPE)

	def test_connector_settings_has_only_approved_fields(self):
		"""Every functional Connector Settings field is one a spec approved (006, RT-321, RT-379, RT-883)."""
		meta = frappe.get_meta(SETTINGS_DOCTYPE)
		functional = {
			f.fieldname
			for f in meta.fields
			if f.fieldtype not in ("Section Break", "Column Break", "Tab Break", "HTML")
		}
		self.assertEqual(
			sorted(functional - APPROVED_SETTINGS_FIELDS),
			[],
			"Connector Settings has fields no spec approved",
		)

	def test_feed_state_survives_a_cache_miss(self):
		"""RT-883: a persisted cursor and retry set are read back from the database after a cache miss."""
		from retail_tower_erpnext_connector.connector import feed_state

		fields = ("posting_feed_cursor", "bin_view_retry_state")
		before = {f: frappe.db.get_single_value(SETTINGS_DOCTYPE, f) for f in fields}
		cursor_key, retry_key = "rt_test_rt883_cursor", "rt_test_rt883_retry"
		retry_set = {"r-2": {"failures": 2}, "r-1": {"failures": 1}}
		try:
			feed_state.save(cursor_key, "posting_feed_cursor", "rt883-cursor")
			feed_state.save_json(retry_key, "bin_view_retry_state", retry_set)
			frappe.cache().delete_value([cursor_key, retry_key])
			self.assertEqual(feed_state.load(cursor_key, "posting_feed_cursor"), "rt883-cursor")
			self.assertEqual(list(feed_state.load_json(retry_key, "bin_view_retry_state")), ["r-2", "r-1"])
		finally:
			frappe.cache().delete_value([cursor_key, retry_key])
			for field, value in before.items():
				frappe.db.set_single_value(SETTINGS_DOCTYPE, field, value, update_modified=False)
			frappe.db.commit()


def _first(value):
	"""frappe.get_hooks returns lists; return the first element or the value itself."""
	if isinstance(value, (list, tuple)):
		return value[0] if value else None
	return value


def _flatten_scheduler_jobs(scheduler):
	"""Collect every job dotted-path string from a scheduler_events hook, at any nesting.

	The hook can be ``{"cron": {"*/5 * * * *": [job, ...]}}`` (cron — a dict of cron-expr → jobs)
	or ``{"hourly": [job, ...]}`` (interval — a list of jobs), and ``frappe.get_hooks`` may
	list-wrap values. Recurse, treating only str leaves as jobs (so cron-expression KEYS are never
	mistaken for jobs — the bug Codex caught: iterating ``dict.values()`` once yielded the cron dict,
	then iterating that yielded its expression keys).
	"""
	jobs: list[str] = []

	def _walk(node):
		if isinstance(node, str):
			jobs.append(node)
		elif isinstance(node, dict):
			for v in node.values():
				_walk(v)
		elif isinstance(node, (list, tuple)):
			for v in node:
				_walk(v)

	_walk(scheduler)
	return jobs


if __name__ == "__main__":
	unittest.main()
