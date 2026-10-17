# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-581 — real-database check of the Gate G5 / F-002 Sales Invoice provenance index.

The unit tests (``test_idempotency``, ``test_index_guard``, ``test_commit_before_ack_glue``) prove the
guard logic against fakes. This module proves the constraint itself on a real MariaDB: on a fresh
install the ``after_install`` hook (``connector/schema.py``, RT-58) must leave
``unique_rt_si_provenance`` in place, so a second Sales Invoice row with the same
``(rt_source_system, rt_external_id)`` is rejected by the database — the "no duplicate ERP posts"
guarantee a crash-window replay relies on.

Runs only on a bench (``bench --site <site> run-tests --app retail_tower_erpnext_connector``; the
nightly ``bench-nightly.yml`` job). Without a connected site it skips, so the frappe-free pytest run
in ``ci.yml`` is unaffected. Every row is inserted inside the test transaction and rolled back.
"""

import unittest
import uuid

try:
	import frappe
except ImportError:  # the frappe-free unit-test environment (ci.yml)
	frappe = None

SALES_INVOICE = "Sales Invoice"
SOURCE_SYSTEM = "rt581-bench-test"


def _connected_site() -> bool:
	# Checked at run time, not import time: a fake ``frappe`` left in sys.modules by a unit test has
	# no connected site, and a real bench connects before it runs the tests.
	return frappe is not None and bool(getattr(getattr(frappe, "local", None), "site", None))


class TestSalesInvoiceProvenanceUniqueIndex(unittest.TestCase):
	"""Gate G5 / F-002 — the database rejects a duplicate connector provenance pair."""

	def setUp(self):
		if not _connected_site():
			self.skipTest("needs a bench site with the app installed (bench run-tests)")
		from retail_tower_erpnext_connector.connector import schema

		self.schema = schema

	def tearDown(self):
		if _connected_site():
			frappe.db.rollback()

	def _insert(self, external_id):
		# db_insert writes the row without ERPNext validation (no company, customer or items needed):
		# the database constraint is what is under test, not the posting flow. Each row gets its own
		# name so a PRIMARY-key collision can never stand in for the provenance index.
		doc = frappe.get_doc(
			{
				"doctype": SALES_INVOICE,
				"name": f"RT581-{uuid.uuid4().hex}",
				"rt_source_system": SOURCE_SYSTEM if external_id is not None else None,
				"rt_external_id": external_id,
			}
		)
		doc.db_insert()
		return doc

	def test_index_is_installed(self):
		self.assertTrue(
			self.schema.index_present(
				SALES_INVOICE, self.schema.SALES_INVOICE_INDEX, self.schema.SALES_INVOICE_COLUMNS
			),
			f"{self.schema.SALES_INVOICE_INDEX} is missing or not UNIQUE on "
			f"{self.schema.SALES_INVOICE_COLUMNS} after install",
		)

	def test_duplicate_provenance_is_rejected_by_the_unique_index(self):
		external_id = f"sale-{uuid.uuid4().hex}"
		self._insert(external_id)

		with self.assertRaises(frappe.UniqueValidationError) as ctx:
			self._insert(external_id)
		self.assertIn(self.schema.SALES_INVOICE_INDEX, str(ctx.exception))

	def test_distinct_provenance_is_accepted(self):
		self._insert(f"sale-{uuid.uuid4().hex}")
		self._insert(f"sale-{uuid.uuid4().hex}")

	def test_null_provenance_is_not_deduplicated(self):
		# Manual / non-connector invoices carry NULL provenance; MariaDB allows many NULLs in a UNIQUE
		# index, so the guard must never block them.
		self._insert(None)
		self._insert(None)
