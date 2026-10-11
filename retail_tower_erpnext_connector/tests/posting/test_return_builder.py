# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-16 — the pure partial-return (credit note) builder (RT-14 D1/D3/D6, RT-10 D6).

A ``reversalKind: return`` posts a return Sales Invoice carrying ONLY the returned lines, negated,
each tagged with its ``lineRef`` (matched to the original invoice row by the glue), and pays the
cash out from ``refundTenders`` (never from the sale's tenders). All fixtures are literal.
"""

import dataclasses
from decimal import Decimal

import pytest

from retail_tower_erpnext_connector.connector.posting import contracts as c
from retail_tower_erpnext_connector.connector.posting import return_builder as rtb
from retail_tower_erpnext_connector.connector.posting import tender as t
from retail_tower_erpnext_connector.connector.posting.builder import UnmappedUnit

_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _line(ref, item, qty, amount):
	"""A sale line whose unit price is amount / qty (exact for these fixtures)."""
	price = f"{Decimal(amount) / Decimal(qty):.2f}"
	return {
		"lineRef": ref,
		"lineName": item,
		"unitPrice": price,
		"currencyCode": "EGP",
		"quantity": qty,
		"lineAmount": amount,
		"taxAmount": None,
		"unit": "each",
		"erpnextItemRef": {"doctype": "Item", "name": item},
	}


def _return(return_lines=None, refund_tenders=None, sale_tenders=None, *, drop_refund=False):
	"""A two-line sale (A: 3 x 100.00, B: 1 x 50.00) and a return of part of it."""
	ref = {
		"sourceSystem": "pos-pulse",
		"externalId": "POS-9001",
		"reversalKind": "return",
		"recordedAt": "2026-06-05T09:30:00Z",
		"businessDate": "2026-06-05",
		"returnLines": return_lines
		or [{"lineRef": _A, "quantity": "1", "lineAmount": "100.00", "taxAmount": None}],
		"refundTenders": refund_tenders or [{"method": "cash", "amount": "100.00"}],
	}
	if drop_refund:
		ref.pop("refundTenders")
	sale = {
		"saleRef": "22222222-2222-4222-8222-222222222222",
		"storeId": "33333333-3333-4333-8333-333333333333",
		"currencyCode": "EGP",
		"posTotal": "350.00",
		"occurredAt": "2026-06-01T10:00:00Z",
		"businessDate": "2026-06-01",
		"sourceSystem": "pos-pulse",
		"externalId": "POS-9001",
		"lines": [
			_line(_A, "ITEM-A", "3", "300.00"),
			_line(_B, "ITEM-B", "1", "50.00"),
		],
	}
	if sale_tenders is not None:
		sale["tenders"] = sale_tenders
	return c.PostingWorkItem.from_wire(
		{
			"workItemRef": "77777777-7777-4777-8777-777777777777",
			"kind": "reversal",
			"sourceSystem": "pos-pulse",
			"externalId": "POS-9001",
			"payloadHash": "d" * 64,
			"businessDate": "2026-06-01",
			"itemCursor": "cursor-16",
			"reversalOf": ref,
			"sale": sale,
		}
	)


def _uom(unit):
	if unit != "each":
		raise UnmappedUnit(unit)
	return "Nos"


def _build(work_item, **kw):
	resolvers = rtb.ReturnResolvers(
		uom_for=_uom,
		warehouse_for=lambda store: {"doctype": "Warehouse", "name": "Main - RT"},
		customer_for=lambda store: "Walk-in Customer - RT",
		mode_of_payment_for=kw.pop("mode_of_payment_for", t.TenderModeMap({"cash": "Cash"}).resolve),
	)
	return rtb.build_return_invoice(work_item, resolvers, **kw)


class TestReturnedLinesOnly:
	def test_credit_note_carries_only_the_returned_line_negated(self):
		doc = _build(_return())
		assert doc["items"] == [
			{
				"item_code": "ITEM-A",
				"qty": "-1",
				"uom": "Nos",
				"rate": "100.00",
				"price_list_rate": "100.00",  # RT-377 / D6: positive, equal to the rate
				"amount": "-100.00",
				"warehouse": "Main - RT",
				"currency": "EGP",
				"rt_line_ref": _A,
			}
		]

	def test_several_returned_lines_keep_the_return_order(self):
		doc = _build(
			_return(
				return_lines=[
					{"lineRef": _B, "quantity": "1", "lineAmount": "50.00", "taxAmount": None},
					{"lineRef": _A, "quantity": "2", "lineAmount": "200.00", "taxAmount": None},
				],
				refund_tenders=[{"method": "cash", "amount": "250.00"}],
			)
		)
		assert [(i["item_code"], i["qty"], i["amount"], i["rt_line_ref"]) for i in doc["items"]] == [
			("ITEM-B", "-1", "-50.00", _B),
			("ITEM-A", "-2", "-200.00", _A),
		]

	def test_fractional_quantity_of_a_weighed_line(self):
		doc = _build(
			_return(
				return_lines=[{"lineRef": _A, "quantity": "0.5", "lineAmount": "50.00", "taxAmount": None}],
				refund_tenders=[{"method": "cash", "amount": "50.00"}],
			)
		)
		assert (doc["items"][0]["qty"], doc["items"][0]["amount"]) == ("-0.5", "-50.00")


class TestCreditNoteHeader:
	def test_is_an_unrounded_stock_moving_return(self):
		doc = _build(_return())
		assert (doc["doctype"], doc["is_return"], doc["update_stock"], doc["disable_rounded_total"]) == (
			"Sales Invoice",
			1,
			1,
			1,
		)

	def test_provenance_is_the_return_work_item_not_the_sale(self):
		doc = _build(_return())
		assert (doc["rt_source_system"], doc["rt_external_id"], doc["rt_sale_ref"]) == (
			"pos-pulse",
			"77777777-7777-4777-8777-777777777777",
			"22222222-2222-4222-8222-222222222222",
		)
		assert (doc["customer"], doc["currency"]) == ("Walk-in Customer - RT", "EGP")

	def test_posts_on_its_own_business_date_at_its_recorded_time(self):
		doc = _build(_return())
		assert (doc["set_posting_time"], doc["posting_date"], doc["posting_time"]) == (
			1,
			"2026-06-05",
			"09:30:00.000000",
		)


class TestRefundPayout:
	"""RT-10 D6 / RT-14 D3: the cash paid out now, from refundTenders through the CURRENT map."""

	def test_pays_out_the_refund_tenders_as_negative_payments(self):
		doc = _build(_return())
		assert (doc["is_pos"], doc["payments"]) == (1, [{"mode_of_payment": "Cash", "amount": "-100.00"}])

	def test_never_mirrors_the_sale_tenders(self):
		doc = _build(_return(sale_tenders=[{"method": "cash", "amount": "350.00"}]))
		assert doc["payments"] == [{"mode_of_payment": "Cash", "amount": "-100.00"}]

	def test_a_return_without_refund_tenders_is_rejected(self):
		# The contract INVARIANT: every return carries refundTenders. Posting an outstanding credit
		# note instead would hide cash that left the drawer (Principle VI).
		with pytest.raises(rtb.MissingRefundTenders):
			_build(_return(drop_refund=True))

	def test_refund_total_must_equal_the_returned_total(self):
		with pytest.raises(t.TenderMismatch):
			_build(_return(refund_tenders=[{"method": "cash", "amount": "99.99"}]))

	def test_an_unmapped_refund_method_fails_closed(self):
		with pytest.raises(t.UnmappedTender):
			_build(_return(), mode_of_payment_for=t.TenderModeMap({}).resolve)

	@pytest.mark.parametrize("refund", ["100.00", "114.00"])
	def test_a_taxed_return_line_is_rejected_while_tax_is_not_posted(self, refund):
		# Codex P2 PR #50: no tax rows are posted (VAT 0 today). A taxed line must be rejected even
		# when the refund equals the line amount, never posted with its tax silently dropped.
		lines = [{"lineRef": _A, "quantity": "1", "lineAmount": "100.00", "taxAmount": "14.00"}]
		with pytest.raises(rtb.ReturnTaxNotPosted, match=r"14\.00"):
			_build(_return(return_lines=lines, refund_tenders=[{"method": "cash", "amount": refund}]))

	def test_a_zero_tax_amount_is_accepted(self):
		lines = [{"lineRef": _A, "quantity": "1", "lineAmount": "100.00", "taxAmount": "0.00"}]
		assert _build(_return(return_lines=lines))["items"][0]["amount"] == "-100.00"


class TestPricing:
	def test_line_amount_must_equal_unit_price_times_returned_quantity(self):
		# ERPNext recomputes amount = rate x qty; a return priced otherwise would post a different
		# amount than Backend-Core recorded, so it fails closed instead.
		lines = [{"lineRef": _A, "quantity": "1", "lineAmount": "99.99", "taxAmount": None}]
		with pytest.raises(rtb.ReturnPricingMismatch, match=r"99.99"):
			_build(_return(return_lines=lines, refund_tenders=[{"method": "cash", "amount": "99.99"}]))


class TestKindGuard:
	def test_rejects_a_non_return_work_item(self):
		wi = _return()
		void = dataclasses.replace(
			wi, reversal_of=dataclasses.replace(wi.reversal_of, reversal_kind="void", return_lines=(), refund_tenders=())
		)
		with pytest.raises(ValueError, match="return"):
			_build(void)


class TestRepricingProtection:
	"""RT-377: a return composes on the forward builder, so it carries both protections; the rate and
	price_list_rate stay positive while qty and amount are negated."""

	def test_return_carries_ignore_pricing_rule_and_positive_price_list_rate(self):
		doc = _build(_return())
		assert doc["ignore_pricing_rule"] == 1
		for item in doc["items"]:
			assert item["price_list_rate"] == item["rate"]
			assert not item["price_list_rate"].startswith("-")
