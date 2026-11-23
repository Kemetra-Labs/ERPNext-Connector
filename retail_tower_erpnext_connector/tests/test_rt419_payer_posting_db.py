# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-419 acceptance criterion 2 — a credit / third-party sale posted by the REAL glue on an ERPNext v16 site.

The unit tests prove the builder and the glue against a stand-in. This module posts the Connector's own
work items through ``frappe_glue.post_work_item`` on the bench site (builder, payer Customer read, insert,
partial-settlement check, submit, RT-378 read-back, Posting Log), exactly as the poller does:

* cash 50.00 + credit 150.00 on a 200.00 sale: the invoice's customer is the PAYER (never the store's
  Customer), it is a part-paid POS invoice (``paid_amount`` 50.00), ``outstanding_amount`` equals the credit
  share (150.00), the receivable GL row is booked against the payer's party, and the GL balances;
* full credit (no tenders): an unpaid invoice, 200.00 outstanding on the payer;
* an unknown, a disabled and a frozen payer are rejected as ``validation`` and leave no invoice.

Isolation: everything runs in the test transaction with ``frappe.db.commit`` patched to a no-op and is rolled
back in cleanup, so no Customer, stock, invoice or Posting Log row survives. Runs only on a bench
(``bench --site <site> run-tests``); without a connected site it skips.
"""

import unittest
from decimal import Decimal
from unittest import mock

try:
	import frappe
except ImportError:  # the frappe-free unit-test environment
	frappe = None

STORE = "7b1e2a3b-4c5d-4e6f-8a9b-0c1d2e3f4a19"
STORE_CUSTOMER = "RT419 Bench Store Customer"
PAYER = "RT419 Bench Payer"
DISABLED_PAYER = "RT419 Bench Disabled Payer"
FROZEN_PAYER = "RT419 Bench Frozen Payer"
ITEM = "RT419-A"
LINE = "0d419000-0000-7000-8000-000000000001"


def _connected_site() -> bool:
	return frappe is not None and bool(getattr(getattr(frappe, "local", None), "site", None))


class _Client:
	"""Records the acks the glue sends (no Backend-Core on the bench)."""

	def __init__(self):
		self.acks = []

	def ack_outcome(self, ref, request, idempotency_key):
		self.acks.append(request.to_wire())


class TestRt419PayerPostingOnBench(unittest.TestCase):
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

		self.addCleanup(frappe.db.rollback)
		patcher = mock.patch.object(frappe.db, "commit", lambda *args, **kwargs: None)
		patcher.start()
		self.addCleanup(patcher.stop)
		self.warehouse = self._prepare_site()

	# ------------------------------------------------------------------ site fixtures (rolled back)

	def _customer(self, name, **extra):
		if not frappe.db.exists("Customer", name):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": name,
					"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}),
					"territory": frappe.db.get_value("Territory", {"is_group": 0}),
					**extra,
				}
			).insert(
				ignore_permissions=True, set_name=name
			)  # the exact name, whatever the site's naming mode

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
		if not frappe.db.exists("Item", ITEM):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": ITEM,
					"item_name": ITEM,
					"item_group": group,
					"stock_uom": "Nos",
					"is_stock_item": 1,
					"is_sales_item": 1,
					"valuation_rate": 50,
				}
			).insert(ignore_permissions=True, set_name=ITEM)
		self._customer(STORE_CUSTOMER)
		self._customer(PAYER)
		self._customer(DISABLED_PAYER, disabled=1)
		self._customer(FROZEN_PAYER, is_frozen=1)
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
						"item_code": ITEM,
						"qty": 20,
						"t_warehouse": warehouse,
						"basic_rate": 50,
						"uom": "Nos",
						"conversion_factor": 1,
					}
				],
			}
		)
		receipt.insert(ignore_permissions=True)
		receipt.submit()
		return warehouse

	# ------------------------------------------------------------------ work items

	def _work_item(self, ref, external_id, *, tenders=None, payer=PAYER, credit="150.00"):
		from retail_tower_erpnext_connector.connector.posting import contracts as c

		sale = {
			"saleRef": "22222222-2222-4222-8222-222222222419",
			"storeId": STORE,
			"currencyCode": "EGP",
			"posTotal": "200.00",
			"occurredAt": "2026-06-01T10:00:00Z",
			"businessDate": "2026-06-01",
			"sourceSystem": "pos-pulse",
			"externalId": external_id,
			"lines": [
				{
					"lineRef": LINE,
					"lineName": ITEM,
					"unitPrice": "100.00",
					"currencyCode": "EGP",
					"quantity": "2",
					"lineAmount": "200.00",
					"taxAmount": None,
					"unit": "each",
					"erpnextItemRef": {"doctype": "Item", "name": ITEM},
				}
			],
			"responsibilities": [{"payerExternalRef": payer, "amount": credit, "sequence": 1}],
		}
		if tenders is not None:
			sale["tenders"] = tenders
		return c.PostingWorkItem.from_wire(
			{
				"workItemRef": ref,
				"kind": "sale_post",
				"sourceSystem": "pos-pulse",
				"externalId": external_id,
				"payloadHash": "c" * 64,
				"businessDate": "2026-06-01",
				"itemCursor": f"c-{ref[-4:]}",
				"sale": sale,
			}
		)

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
			customers=StoreCustomerMap({STORE: STORE_CUSTOMER}),
			tenders=TenderModeMap({"cash": "Cash"}),
			correlation_id="rt419-bench",
		)
		return outcome, client

	def _posted(self, work_item):
		outcome, client = self._post(work_item)
		self.assertEqual(outcome, "posted", client.acks)
		return frappe.get_doc("Sales Invoice", client.acks[-1]["documentRef"]["name"])

	def _gl(self, doc):
		return frappe.get_all(
			"GL Entry",
			filters={"voucher_no": doc.name, "is_cancelled": 0},
			fields=["account", "party_type", "party", "debit", "credit"],
		)

	def _assert_balanced_and_booked_on_the_payer(self, doc, net_outstanding):
		gl = self._gl(doc)
		self.assertTrue(gl)
		self.assertEqual(sum(Decimal(str(e.debit)) for e in gl), sum(Decimal(str(e.credit)) for e in gl))
		payer_rows = [e for e in gl if e.party_type == "Customer" and e.party == PAYER]
		self.assertTrue(payer_rows, "no receivable GL row is booked against the payer")
		self.assertTrue(all(e.account == doc.debit_to for e in payer_rows))
		self.assertEqual(
			sum(Decimal(str(e.debit)) - Decimal(str(e.credit)) for e in payer_rows), Decimal(net_outstanding)
		)
		self.assertFalse(
			[e for e in gl if e.party == STORE_CUSTOMER], "the store's Customer must not carry any entry"
		)

	# ------------------------------------------------------------------ the proof

	def test_cash_plus_credit_posts_to_the_payer_with_the_credit_share_outstanding(self):
		doc = self._posted(
			self._work_item(
				"41900000-0000-4000-8000-000000000001",
				"POS-RT419-SPLIT",
				tenders=[{"method": "cash", "amount": "50.00"}],
			)
		)
		self.assertEqual((doc.docstatus, doc.customer), (1, PAYER))
		self.assertEqual(Decimal(str(doc.grand_total)), Decimal("200.00"))
		self.assertEqual(Decimal(str(doc.paid_amount)), Decimal("50.00"))
		self.assertEqual(Decimal(str(doc.outstanding_amount)), Decimal("150.00"))
		self.assertEqual(
			[(p.mode_of_payment, Decimal(str(p.amount))) for p in doc.payments], [("Cash", Decimal("50.00"))]
		)
		self._assert_balanced_and_booked_on_the_payer(doc, "150.00")

	def test_full_credit_posts_an_unpaid_invoice_to_the_payer(self):
		doc = self._posted(
			self._work_item("41900000-0000-4000-8000-000000000002", "POS-RT419-FULL", credit="200.00")
		)
		self.assertEqual((doc.docstatus, doc.customer, doc.is_pos), (1, PAYER, 0))
		self.assertEqual(Decimal(str(doc.paid_amount)), Decimal(0))
		self.assertEqual(Decimal(str(doc.outstanding_amount)), Decimal("200.00"))
		self._assert_balanced_and_booked_on_the_payer(doc, "200.00")

	def test_an_unknown_disabled_or_frozen_payer_is_rejected_and_posts_nothing(self):
		for n, payer in enumerate(("RT419 No Such Customer", DISABLED_PAYER, FROZEN_PAYER), start=3):
			with self.subTest(payer=payer):
				external_id = f"POS-RT419-BAD-{n}"
				outcome, client = self._post(
					self._work_item(
						f"41900000-0000-4000-8000-00000000000{n}", external_id, payer=payer, credit="200.00"
					)
				)
				self.assertEqual(outcome, "permanently_rejected", client.acks)
				self.assertEqual(client.acks[-1]["reason"]["category"], "validation")
				self.assertIn("UnmappedPayer", client.acks[-1]["reason"]["message"])
				self.assertFalse(
					frappe.db.exists(
						"Sales Invoice", {"rt_source_system": "pos-pulse", "rt_external_id": external_id}
					)
				)
