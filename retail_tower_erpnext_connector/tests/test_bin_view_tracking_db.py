# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-918 — real-database check of the bin-view tracking read (``FrappeTrackingReader``).

The unit tests (``tests/bin_view/test_tracking*.py``) prove the evidence logic against fakes; the two
Stock Ledger aggregate queries are only exercised here, on a real ERPNext schema: the bundle-backed
sum (``Stock Ledger Entry`` → ``Serial and Batch Entry``), the legacy ``batch_no`` sum, the
``is_cancelled`` filter, the warehouse filter, and the Item / Batch master reads.

Rows are written with ``db_insert`` (no ERPNext stock validation): the query against the schema is
under test, not the stock posting flow. Every row is inserted inside the test transaction and rolled
back. Runs only on a bench (``bench --site <site> run-tests --app retail_tower_erpnext_connector``,
the ``connector/bench`` job); without a connected site it skips, so the frappe-free pytest run is
unaffected.
"""

import unittest
import uuid

try:
	import frappe
except ImportError:  # the frappe-free unit-test environment
	frappe = None


def _connected_site() -> bool:
	# Checked at run time: a fake ``frappe`` left in sys.modules by a unit test has no connected site.
	return frappe is not None and bool(getattr(getattr(frappe, "local", None), "site", None))


class TestBinViewTrackingRead(unittest.TestCase):
	"""stock-view 1.4 evidence: Item flags + batch-wise book quantity per warehouse."""

	def setUp(self):
		if not _connected_site():
			self.skipTest("needs a bench site with the app installed (bench run-tests)")
		from retail_tower_erpnext_connector.connector.bin_view import frappe_glue

		self.glue = frappe_glue
		tag = uuid.uuid4().hex[:10]
		self.wh = f"RT918-WH-{tag}"
		self.other_wh = f"RT918-WH2-{tag}"
		self.med = f"RT918-MED-{tag}"
		self.soap = f"RT918-SOAP-{tag}"
		self.b1 = f"RT918-B1-{tag}"
		self.b2 = f"RT918-B2-{tag}"
		self.b3 = f"RT918-B3-{tag}"
		self.tag = tag

	def tearDown(self):
		if _connected_site():
			frappe.db.rollback()

	def _insert(self, values):
		doc = frappe.get_doc(values)
		doc.db_insert()
		return doc

	def _item(self, name, *, batch):
		self._insert(
			{
				"doctype": "Item",
				"name": name,
				"item_code": name,
				"item_name": name,
				"stock_uom": "Nos",
				"has_batch_no": int(batch),
				"has_expiry_date": int(batch),
				"has_serial_no": 0,
			}
		)

	def _batch(self, name, *, expiry, disabled):
		self._insert(
			{
				"doctype": "Batch",
				"name": name,
				"batch_id": name,
				"item": self.med,
				"expiry_date": expiry,
				"disabled": disabled,
			}
		)

	def _sle(self, *, warehouse, qty, bundle=None, batch_no=None, cancelled=0):
		self._insert(
			{
				"doctype": "Stock Ledger Entry",
				"name": f"RT918-SLE-{uuid.uuid4().hex}",
				"item_code": self.med,
				"warehouse": warehouse,
				"actual_qty": qty,
				"serial_and_batch_bundle": bundle,
				"batch_no": batch_no,
				"is_cancelled": cancelled,
				"docstatus": 1,
			}
		)

	def _bundle_entry(self, bundle, *, batch_no, qty, warehouse):
		self._insert(
			{
				"doctype": "Serial and Batch Entry",
				"name": f"RT918-SBE-{uuid.uuid4().hex}",
				"parent": bundle,
				"parenttype": "Serial and Batch Bundle",
				"parentfield": "entries",
				"batch_no": batch_no,
				"qty": qty,
				"warehouse": warehouse,
			}
		)

	def _ledger(self):
		bundle_in, bundle_out = f"RT918-SBB-IN-{self.tag}", f"RT918-SBB-OUT-{self.tag}"
		cancelled, elsewhere = f"RT918-SBB-X-{self.tag}", f"RT918-SBB-W2-{self.tag}"
		# B1: +10 in, -4 out (bundle-backed) -> 6; a cancelled +99 and another warehouse's +7 are ignored.
		self._sle(warehouse=self.wh, qty=10, bundle=bundle_in)
		self._bundle_entry(bundle_in, batch_no=self.b1, qty=10, warehouse=self.wh)
		self._sle(warehouse=self.wh, qty=-4, bundle=bundle_out)
		self._bundle_entry(bundle_out, batch_no=self.b1, qty=-4, warehouse=self.wh)
		self._sle(warehouse=self.wh, qty=99, bundle=cancelled, cancelled=1)
		self._bundle_entry(cancelled, batch_no=self.b1, qty=99, warehouse=self.wh)
		self._sle(warehouse=self.other_wh, qty=7, bundle=elsewhere)
		self._bundle_entry(elsewhere, batch_no=self.b1, qty=7, warehouse=self.other_wh)
		# B2: a legacy (pre-bundle) ledger row with batch_no and no bundle -> -1.5.
		self._sle(warehouse=self.wh, qty=-1.5, batch_no=self.b2)
		# B3: in and out to zero (bundle-backed) -> omitted (a used-up Batch has no presence).
		zero_in, zero_out = f"RT918-SBB-Z1-{self.tag}", f"RT918-SBB-Z2-{self.tag}"
		self._sle(warehouse=self.wh, qty=2, bundle=zero_in)
		self._bundle_entry(zero_in, batch_no=self.b3, qty=2, warehouse=self.wh)
		self._sle(warehouse=self.wh, qty=-2, bundle=zero_out)
		self._bundle_entry(zero_out, batch_no=self.b3, qty=-2, warehouse=self.wh)

	def test_reads_flags_and_batch_wise_book_quantity_for_the_warehouse(self):
		self._item(self.med, batch=True)
		self._item(self.soap, batch=False)
		self._batch(self.b1, expiry="2027-03-31", disabled=0)
		self._batch(self.b2, expiry=None, disabled=1)
		self._batch(self.b3, expiry="2026-01-01", disabled=0)
		self._ledger()

		read = self.glue.FrappeTrackingReader().read_tracking(
			erpnext_warehouse_ref=self.wh, item_codes=[self.med, self.soap]
		)

		flags = {i.item_code: (i.has_batch_no, i.has_serial_no, i.has_expiry_date) for i in read.items}
		self.assertEqual(flags, {self.med: (True, False, True), self.soap: (False, False, False)})
		got = {(b.item_code, b.batch_no): (round(b.qty, 6), b.expiry_date, b.disabled) for b in read.batches}
		self.assertEqual(
			got,
			{
				(self.med, self.b1): (6.0, "2027-03-31", False),
				(self.med, self.b2): (-1.5, None, True),
			},
		)

	def test_an_item_with_no_ledger_in_the_warehouse_has_no_batches(self):
		self._item(self.med, batch=True)
		read = self.glue.FrappeTrackingReader().read_tracking(
			erpnext_warehouse_ref=self.wh, item_codes=[self.med]
		)
		self.assertEqual(read.batches, ())
		self.assertTrue(read.items[0].has_batch_no)
