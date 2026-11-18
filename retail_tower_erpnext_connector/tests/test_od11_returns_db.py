# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1340 acceptance criterion 1 — OD-11 returns posted by the REAL glue on a real ERPNext v16 site.

The research script (``docs/research/erpnext-connector/rt-1340-od11/`` at the repo root, outside the
Connector tree) proved the document shapes with hand-written dicts.
This module posts the Connector's own work items through ``frappe_glue.post_work_item`` on the bench
site: the builder, the link to the original rows, the RT-452 tracking check, insert + submit, the RT-378
read-back and the Posting Log, exactly as the poller does. One sale (golden E1 plus a split line and a
second reward), then four returns:

* A1   — option A: the 300.00 unit returned with 80.00 clawed back posts at 220.00;
* A2   — option A with a split: 3 x 100.00 with 10.01 clawed back posts 96.66 + 96.66 + 96.67;
* S3#2 — Hybrid: the reward unit at its net 120.00 plus an 80.00 give-back row on RT-PROMO-GIVEBACK;
* G2   — a second give-back of another amount (40.00): ERPNext may have created an Item Price for the
  give-back Item after the first one, and the row must still post at its own amount.

Each credit note must equal its refund to the minor unit, be fully paid, and have balanced GL entries.

Isolation: ``ensure_give_back_item`` runs first (it commits, exactly as ``bench migrate`` would, and the
Item is what every site gets). Everything after it runs in the test transaction with ``frappe.db.commit``
patched to a no-op, and is rolled back in cleanup: no Item, stock, invoice or Posting Log row survives.
Runs only on a bench (``bench --site <site> run-tests``); without a connected site it skips.
"""

import unittest
from decimal import Decimal
from unittest import mock

try:
	import frappe
except ImportError:  # the frappe-free unit-test environment
	frappe = None

GIVE_BACK = "RT-PROMO-GIVEBACK"
STORE = "7b1e2a3b-4c5d-4e6f-8a9b-0c1d2e3f4a5d"
CUSTOMER = "RT1340 Bench Customer"
ITEMS = ("RT1340-A", "RT1340-B", "RT1340-C", "RT1340-D", "RT1340-E", "RT1340-F")
LINE = {code: f"0d134000-0000-7000-8000-00000000000{i}" for i, code in enumerate(ITEMS, start=1)}


def _connected_site() -> bool:
	return frappe is not None and bool(getattr(getattr(frappe, "local", None), "site", None))


class _Client:
	"""Records the acks the glue sends (no Backend-Core on the bench)."""

	def __init__(self):
		self.acks = []

	def ack_outcome(self, ref, request, idempotency_key):
		self.acks.append(request.to_wire())


class TestOd11ReturnsOnBench(unittest.TestCase):
	def setUp(self):
		if not _connected_site():
			self.skipTest("needs a bench site with the app installed (bench run-tests)")
		from retail_tower_erpnext_connector.provisioner import site_ops
		from retail_tower_erpnext_connector.tests.test_provisioner_site_ops_db import WIZARD_ARGS

		if not site_ops.inspect()["setup_complete"]:
			site_ops.setup_wizard(WIZARD_ARGS)  # not undoable: only on a site that is not set up yet
		companies = frappe.get_all("Company", pluck="name")
		if len(companies) != 1:
			self.skipTest("the site does not have exactly one Company")
		self.company = companies[0]

		from retail_tower_erpnext_connector.connector.schema import ensure_give_back_item

		ensure_give_back_item()  # what bench migrate does; commits, so it runs before the patch below

		self.addCleanup(frappe.db.rollback)
		patcher = mock.patch.object(frappe.db, "commit", lambda *args, **kwargs: None)
		patcher.start()
		self.addCleanup(patcher.stop)
		self.warehouse = self._prepare_site()

	# ------------------------------------------------------------------ site fixtures (rolled back)

	def _prepare_site(self) -> str:
		company = self.company
		warehouse = frappe.get_all(
			"Warehouse",
			filters={"company": company, "is_group": 0},
			pluck="name",
			order_by="name asc",
			limit=1,
		)[0]
		group = frappe.db.get_value("Item Group", {"is_group": 0}, "name", order_by="name asc")
		for code in ITEMS:
			if not frappe.db.exists("Item", code):
				frappe.get_doc(
					{
						"doctype": "Item",
						"item_code": code,
						"item_name": code,
						"item_group": group,
						"stock_uom": "Nos",
						"is_stock_item": 1,
						"is_sales_item": 1,
						"valuation_rate": 50,
					}
				).insert(ignore_permissions=True, set_name=code)
		if not frappe.db.exists("Customer", CUSTOMER):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": CUSTOMER,
					"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}),
					"territory": frappe.db.get_value("Territory", {"is_group": 0}),
				}
			).insert(ignore_permissions=True)
		mop = frappe.get_doc("Mode of Payment", "Cash")
		if not any(row.company == company for row in mop.accounts):
			cash = frappe.db.get_value("Company", company, "default_cash_account") or frappe.db.get_value(
				"Account", {"company": company, "account_type": "Cash", "is_group": 0}
			)
			mop.append("accounts", {"company": company, "default_account": cash})
			mop.save(ignore_permissions=True)
		receipt = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": company,
				# Before the sale's business date (2026-06-01): the glue posts on that date, so stock received
				# "today" would not exist yet when the sale is valued (NegativeStockError on the first run).
				"set_posting_time": 1,
				"posting_date": "2026-01-02",
				"posting_time": "08:00:00",
				"items": [
					{
						"item_code": code,
						"qty": 20,
						"t_warehouse": warehouse,
						"basic_rate": 50,
						"uom": "Nos",
						"conversion_factor": 1,
					}
					for code in ITEMS
				],
			}
		)
		receipt.insert(ignore_permissions=True)
		receipt.submit()
		return warehouse

	# ------------------------------------------------------------------ work items

	def _sale_lines(self):
		def line(code, unit, qty, amount):
			return {
				"lineRef": LINE[code],
				"lineName": code,
				"unitPrice": unit,
				"currencyCode": "EGP",
				"quantity": qty,
				"lineAmount": amount,
				"taxAmount": None,
				"unit": "each",
				"erpnextItemRef": {"doctype": "Item", "name": code},
			}

		# E1 (300 + the 200 reward at 80 off) + a 3 x 100 line + three more rewards (100 at 40 off) = 900.00
		return [
			line("RT1340-A", "300.00", "1", "300.00"),
			line("RT1340-B", "200.00", "1", "120.00"),
			line("RT1340-C", "100.00", "3", "300.00"),
			line("RT1340-D", "100.00", "1", "60.00"),
			line("RT1340-E", "100.00", "1", "60.00"),
			line("RT1340-F", "100.00", "1", "60.00"),
		]

	def _sale(self):
		return {
			"saleRef": "22222222-2222-4222-8222-222222221340",
			"storeId": STORE,
			"currencyCode": "EGP",
			"posTotal": "900.00",
			"occurredAt": "2026-06-01T10:00:00Z",
			"businessDate": "2026-06-01",
			"sourceSystem": "pos-pulse",
			"externalId": "POS-RT1340-BENCH",
			"lines": self._sale_lines(),
			"tenders": [{"method": "cash", "amount": "900.00"}],
		}

	def _work_item(self, ref, kind, **extra):
		from retail_tower_erpnext_connector.connector.posting import contracts as c

		return c.PostingWorkItem.from_wire(
			{
				"workItemRef": ref,
				"kind": kind,
				"sourceSystem": "pos-pulse",
				"externalId": "POS-RT1340-BENCH",
				"payloadHash": "b" * 64,
				"businessDate": "2026-06-01",
				"itemCursor": f"c-{ref[-4:]}",
				"sale": self._sale(),
				**extra,
			}
		)

	def _return(self, ref, minute, lines, refund):
		reversal = {
			"sourceSystem": "pos-pulse",
			"externalId": "POS-RT1340-BENCH",
			"reversalKind": "return",
			"recordedAt": f"2026-06-05T09:{minute:02d}:00Z",
			"businessDate": "2026-06-05",
			"returnLines": [{**line, "taxAmount": None} for line in lines],
			"refundTenders": [{"method": "cash", "amount": refund}],
		}
		return self._work_item(ref, "reversal", reversalOf=reversal)

	def _post(self, work_item):
		from retail_tower_erpnext_connector.connector.posting import frappe_glue
		from retail_tower_erpnext_connector.connector.posting.frappe_store import FrappePostingLogStore
		from retail_tower_erpnext_connector.connector.posting.tender import TenderModeMap
		from retail_tower_erpnext_connector.connector.posting.uom import (
			PreResolvedWarehouse,
			StoreCustomerMap,
			UomMap,
		)

		client = _Client()
		outcome = frappe_glue.post_work_item(
			work_item,
			client=client,
			store=FrappePostingLogStore(),
			uom_map=UomMap({"each": "Nos"}),
			warehouses=PreResolvedWarehouse({STORE: {"doctype": "Warehouse", "name": self.warehouse}}),
			customers=StoreCustomerMap({STORE: CUSTOMER}),
			tenders=TenderModeMap({"cash": "Cash"}),
			correlation_id="rt1340-bench",
		)
		self.assertEqual(outcome, "posted", client.acks)
		return frappe.get_doc("Sales Invoice", client.acks[-1]["documentRef"]["name"])

	def _assert_credit_note(self, doc, refund):
		self.assertEqual((doc.docstatus, doc.is_return), (1, 1))
		self.assertEqual(Decimal(str(doc.grand_total)), -Decimal(refund))
		self.assertEqual(Decimal(str(doc.outstanding_amount)), Decimal(0))
		gl = frappe.get_all(
			"GL Entry", filters={"voucher_no": doc.name, "is_cancelled": 0}, fields=["debit", "credit"]
		)
		self.assertTrue(gl)
		self.assertEqual(sum(Decimal(str(e.debit)) for e in gl), sum(Decimal(str(e.credit)) for e in gl))

	def _rows(self, doc):
		return [
			(row.item_code, row.qty, Decimal(str(row.rate)), Decimal(str(row.amount))) for row in doc.items
		]

	# ------------------------------------------------------------------ the proof

	def test_the_give_back_item_exists_non_stock_under_its_fixed_name(self):
		row = frappe.db.get_value(
			"Item", GIVE_BACK, ["item_code", "disabled", "is_stock_item", "stock_uom"], as_dict=True
		)
		self.assertIsNotNone(row, "ensure_give_back_item did not create the Item")
		self.assertEqual((row.item_code, row.disabled, row.is_stock_item), (GIVE_BACK, 0, 0))

	def test_od11_returns_post_credit_notes_equal_to_the_refund(self):
		sale = self._post(self._work_item("13400000-0000-4000-8000-000000000001", "sale_post"))
		self.assertEqual(Decimal(str(sale.grand_total)), Decimal("900.00"))

		a1 = self._post(
			self._return(
				"13400000-0000-4000-8000-000000000002",
				10,
				[
					{
						"lineRef": LINE["RT1340-A"],
						"quantity": "1",
						"lineAmount": "300.00",
						"promotionClawbackAmount": "80.00",
					}
				],
				"220.00",
			)
		)
		self._assert_credit_note(a1, "220.00")
		self.assertEqual(self._rows(a1), [("RT1340-A", -1.0, Decimal("220.00"), Decimal("-220.00"))])

		a2 = self._post(
			self._return(
				"13400000-0000-4000-8000-000000000003",
				20,
				[
					{
						"lineRef": LINE["RT1340-C"],
						"quantity": "3",
						"lineAmount": "300.00",
						"promotionClawbackAmount": "10.01",
					}
				],
				"289.99",
			)
		)
		self._assert_credit_note(a2, "289.99")
		self.assertEqual(
			sorted(row[3] for row in self._rows(a2)),
			[Decimal("-96.67"), Decimal("-96.66"), Decimal("-96.66")],
		)

		s3 = self._post(
			self._return(
				"13400000-0000-4000-8000-000000000004",
				30,
				[
					{
						"lineRef": LINE["RT1340-B"],
						"quantity": "1",
						"lineAmount": "120.00",
						"promotionGiveBackAmount": "80.00",
					}
				],
				"200.00",
			)
		)
		self._assert_credit_note(s3, "200.00")
		self.assertEqual(
			self._rows(s3),
			[
				("RT1340-B", -1.0, Decimal("120.00"), Decimal("-120.00")),
				(GIVE_BACK, -1.0, Decimal("80.00"), Decimal("-80.00")),
			],
		)
		self.assertFalse(s3.items[1].sales_invoice_item)  # the give-back row is not on the original invoice

		g2 = self._post(
			self._return(
				"13400000-0000-4000-8000-000000000005",
				40,
				[
					{
						"lineRef": LINE["RT1340-D"],
						"quantity": "1",
						"lineAmount": "60.00",
						"promotionGiveBackAmount": "40.00",
					}
				],
				"100.00",
			)
		)
		self._assert_credit_note(g2, "100.00")
		give_back = g2.items[1]
		self.assertEqual(
			(give_back.item_code, Decimal(str(give_back.price_list_rate)), Decimal(str(give_back.rate))),
			(GIVE_BACK, Decimal("40.00"), Decimal("40.00")),
		)

		# Two give-backs on ONE credit note (CodeRabbit, PR #584): ERPNext's duplicate check for non-stock rows
		# compares item_code + description, so each give-back row carries its own description.
		g3 = self._post(
			self._return(
				"13400000-0000-4000-8000-000000000006",
				50,
				[
					{
						"lineRef": LINE["RT1340-E"],
						"quantity": "1",
						"lineAmount": "60.00",
						"promotionGiveBackAmount": "40.00",
					},
					{
						"lineRef": LINE["RT1340-F"],
						"quantity": "1",
						"lineAmount": "60.00",
						"promotionGiveBackAmount": "40.00",
					},
				],
				"200.00",
			)
		)
		self._assert_credit_note(g3, "200.00")
		self.assertEqual([row.item_code for row in g3.items].count(GIVE_BACK), 2)
