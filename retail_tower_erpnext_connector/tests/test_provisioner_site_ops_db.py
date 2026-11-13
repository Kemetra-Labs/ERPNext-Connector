# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1359 — the provisioner's site-side operations on a real site (bench lane).

The frappe-free tests under ``tests/provisioner`` prove the orchestrator against a fake bench. This
module runs the site-side operations on a real ERPNext v16 site: the setup wizard for the base, then
template v1 (perpetual inventory, FIFO, Cash Mode of Payment with its account, one "Retail Walk-in"
customer), per-store warehouses with the Connector Settings maps, and the credential write. It is the
bench-lane test for ``setup_complete`` (an internal wizard API, pinned to the validated Frappe and
ERPNext versions); a version bump must re-run it.

Isolation (so no other test sees this module's state):

* Every operation except the wizard runs inside the test transaction with
  ``frappe.flags.rt_provisioner_no_commit`` set, and ``tearDown`` rolls it back: no Warehouse,
  Customer, Mode of Payment, Connector Settings row or setting survives, and ``default_company`` and
  the global defaults are never written. The site-config marker (a file, outside any transaction) is
  written by its own test and removed again in cleanup.
* The wizard cannot be rolled back (``setup_complete`` commits and runs DDL). It runs only when the
  site is not yet set up; on a site that is already set up it is skipped and the template tests run
  against that site's own single Company. The wizard result on a fresh bench site is one Company
  and ``default_company``; the Connector's other real-DB tests (G5 index, bin-view tracking, poll
  scheduling) neither read nor need either, and every other Connector test uses frappe stand-ins.

Runs only on a bench (``bench --site <site> run-tests --app retail_tower_erpnext_connector``, the PR
lane and the nightly ``connector-bench-nightly.yml``); without a connected site it skips. A real
``bench new-site`` end to end is not here (it needs a bench-level runner): that is RT-1360 and the lab.
"""

import json
import unittest

try:
	import frappe
except ImportError:  # the frappe-free unit-test environment
	frappe = None

TENANT_STORES = [
	{"storeId": "5f1e2a3b-4c5d-4e6f-8a9b-0c1d2e3f4a5b", "displayName": "RT1359 Main Store"},
	{"storeId": "6a1e2a3b-4c5d-4e6f-8a9b-0c1d2e3f4a5c", "displayName": "RT1359 Second Store"},
]
TEMPLATE_ARGS = {"template_id": "egypt-retail", "template_version": 1}
WIZARD_ARGS = {
	"country": "Egypt",
	"currency": "EGP",
	"timezone": "Africa/Cairo",
	"language": "ar",
	"company_name": "RT1359 Template Co",
	"abbr": "RT",
	"chart_of_accounts": "Standard",
	"fiscal_year": "calendar",
}
SECRET = "-".join(["rt1359", "test", "secret", "not", "a", "credential"])
SETTINGS = "Connector Settings"
NO_COMMIT = "rt_provisioner_no_commit"


def _connected_site() -> bool:
	return frappe is not None and bool(getattr(getattr(frappe, "local", None), "site", None))


class TestProvisionerSiteOps(unittest.TestCase):
	def setUp(self):
		if not _connected_site():
			self.skipTest("needs a bench site with the app installed (bench run-tests)")
		from retail_tower_erpnext_connector.provisioner import site_ops

		self.ops = site_ops
		if not self.ops.inspect()["setup_complete"]:
			self.ops.setup_wizard(WIZARD_ARGS)  # not undoable: only on a site that is not set up yet
		companies = frappe.get_all("Company", pluck="name")
		if len(companies) != 1:
			self.skipTest("the site does not have exactly one Company")
		self.company = companies[0]
		self.global_defaults = frappe.db.get_singles_dict("Global Defaults")
		frappe.flags[NO_COMMIT] = True
		self.addCleanup(self._rollback)

	def _rollback(self):
		frappe.flags.pop(NO_COMMIT, None)
		frappe.db.rollback()

	def _args(self, **extra):
		return {"company": self.company, **extra}

	def test_template_v1_is_complete_and_idempotent(self):
		self.ops.apply_template({**TEMPLATE_ARGS, **self._args()})
		counts = lambda: (  # noqa: E731
			frappe.db.count("Customer"),
			frappe.db.count("Mode of Payment"),
			frappe.db.count("Mode of Payment Account"),
		)
		before = counts()
		self.ops.apply_template({**TEMPLATE_ARGS, **self._args()})  # idempotent: nothing twice
		self.assertEqual(before, counts())

		self.assertTrue(frappe.db.get_value("Company", self.company, "enable_perpetual_inventory"))
		self.assertEqual(frappe.db.get_single_value("Stock Settings", "valuation_method"), "FIFO")
		cash = frappe.get_doc("Mode of Payment", "Cash")
		self.assertTrue(any(r.company == self.company and r.default_account for r in cash.accounts))
		self.assertEqual(frappe.db.count("Customer", {"customer_name": "Retail Walk-in"}), 1)
		self.assertEqual(frappe.db.get_singles_dict("Global Defaults"), self.global_defaults)

	def test_stores_get_warehouses_and_maps_and_a_rerun_changes_nothing(self):
		self.ops.apply_template({**TEMPLATE_ARGS, **self._args()})
		args = self._args(
			stores=TENANT_STORES, walk_in_customer="Retail Walk-in", cash_mode_of_payment="Cash"
		)
		self.ops.bootstrap_stores(args)
		warehouses = frappe.db.count("Warehouse")
		self.ops.bootstrap_stores(args)
		self.assertEqual(frappe.db.count("Warehouse"), warehouses)

		self.assertIn("cash", self.ops.inspect()["tender_methods"])
		self.assertTrue(frappe.get_single(SETTINGS).posting_currency)
		stores = self.ops.inspect()["stores"]
		self.assertEqual(set(stores), {s["storeId"] for s in TENANT_STORES})
		for entry in stores.values():
			self.assertTrue(frappe.db.exists("Warehouse", entry["warehouse"]))
			self.assertTrue(frappe.db.exists("Customer", entry["customer"]))
		self.assertEqual(len({e["warehouse"] for e in stores.values()}), 2)

	def test_the_credential_is_written_in_process_and_never_echoed(self):
		self.ops.bind_credential(
			{
				"base_url": "https://backend.example",
				"registration_id": "3d2c7f60-91a4-4c0b-9f3e-6f1c2a8b4e55",
				"credential_id": "9a4e1d2b-7c3f-4b86-8e0a-5d1f3c7a9b22",
				"issued_at": "2026-11-14T10:00:00Z",
				"expires_at": "2027-11-14T10:00:00Z",
			},
			SECRET,
		)
		self.assertEqual(frappe.get_single(SETTINGS).get_password("dp2_token"), SECRET)
		state = self.ops.inspect()
		self.assertEqual(state["credential_id"], "9a4e1d2b-7c3f-4b86-8e0a-5d1f3c7a9b22")
		self.assertNotIn(SECRET, str(state))
		self.assertNotIn(SECRET, repr(frappe.get_site_config()))

	def test_inspect_reports_what_the_engine_checks(self):
		state = self.ops.inspect()
		self.assertTrue(state["setup_complete"])
		self.assertEqual(state["companies"], [self.company])
		self.assertTrue(state["default_company"])
		self.assertTrue(state["indexes_ok"])
		self.assertTrue(state["versions"]["frappe"].startswith("16."))
		self.assertTrue(state["versions"]["erpnext"].startswith("16."))

	def test_the_languages_a_request_can_carry_resolve_on_this_site(self):
		# the first real request carries `ar`; the wizard above runs in `en`
		for code in ("ar", "en"):
			self.assertTrue(self.ops._language_name(code), code)

	def test_the_template_marker_is_recorded_in_site_config_and_removed_again(self):
		path = frappe.get_site_path("site_config.json")
		with open(path, encoding="utf-8") as handle:
			original = json.load(handle)
		self.addCleanup(self._restore_site_config, path, original)
		spec = self.ops.spec_for("egypt-retail", 1)
		self.ops.record_template(spec)
		self.assertEqual(self.ops.inspect()["template"], {"id": "egypt-retail", "version": 1})

	@staticmethod
	def _restore_site_config(path, original):
		with open(path, "w", encoding="utf-8") as handle:
			json.dump(original, handle, indent=1)
		frappe.local.conf.pop("rt_template_applied", None)
