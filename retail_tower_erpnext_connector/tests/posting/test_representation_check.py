# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt
"""RT-378 (RT-283 F3, design B) — ERPNext persisted exactly what Retail Tower sent (pure).

The expected representation is the payload the Connector built; the persisted one is what the
ERPNext Sales Invoice holds (read from the in-memory document after insert, or from the database
after submit / on replay). Every mismatch reason starts with ``representation:`` (D1).
"""

import pytest

from retail_tower_erpnext_connector.connector.posting import contracts as c
from retail_tower_erpnext_connector.connector.posting.representation_check import (
	RepresentationMismatch,
	check_representation,
	expected_from_payload,
	persisted_from_doc,
	replay_exemptions,
)

_COMPANY = "RT Company"


def _payload(*, sign=1):
	"""What the builder sends: exact-decimal strings (a reversal negates qty and amount)."""
	neg = "-" if sign < 0 else ""
	return {
		"doctype": "Sales Invoice",
		"customer": "Walk-in",
		"company": _COMPANY,  # RT-378 finding 2: set explicitly by the builder
		"currency": "EGP",
		"ignore_pricing_rule": 1,
		"items": [
			{
				"item_code": "ITEM-A",
				"qty": f"{neg}2",
				"uom": "Nos",
				"rate": "9.50",
				"price_list_rate": "9.50",
				"amount": f"{neg}19.00",
				"warehouse": "Store WH",
				"currency": "EGP",
			},
			{
				"item_code": "ITEM-B",
				"qty": f"{neg}1",
				"uom": "Kg",
				"rate": "10.49",
				"price_list_rate": "10.49",
				"amount": f"{neg}10.49",
				"warehouse": "Store WH",
				"currency": "EGP",
			},
		],
	}


def _doc(*, sign=1):
	"""What ERPNext persisted for ``_payload``: floats, as Frappe returns them."""
	return {
		"currency": "EGP",
		"customer": "Walk-in",
		"company": _COMPANY,
		"ignore_pricing_rule": 1,
		"grand_total": sign * 29.49,
		"taxes": [],
		"items": [
			_row("ITEM-A", "Nos", qty=sign * 2.0, rate=9.5, amount=sign * 19.0),
			_row("ITEM-B", "Kg", qty=sign * 1.0, rate=10.49, amount=sign * 10.49),
		],
	}


def _row(item_code, uom, **numbers):
	"""``numbers`` are the row's ``qty``, ``rate`` and ``amount``; the list price is the rate."""
	return {
		"item_code": item_code,
		"uom": uom,
		**numbers,
		"warehouse": "Store WH",
		"price_list_rate": numbers["rate"],
		"discount_percentage": 0.0,
		"discount_amount": 0.0,
		"margin_type": "",
		"margin_rate_or_amount": 0.0,
	}


def _check(doc, payload=None, *, exempt=frozenset(), legacy_allowed=False):
	expected = expected_from_payload(payload or _payload(), exempt=exempt)
	return check_representation(expected, persisted_from_doc(doc), legacy_allowed=legacy_allowed)


def _with_line(field, value, line=0):
	doc = _doc()
	doc["items"][line][field] = value
	return doc


class TestMatchingDocument:
	def test_an_exact_copy_passes(self):
		assert _check(_doc()) is None

	def test_numbers_compare_exactly_across_string_and_float(self):
		# "9.50" sent, 9.5 persisted: the same value.
		assert _check(_with_line("rate", 9.5)) is None

	def test_a_reversal_with_negative_qty_and_amount_and_positive_rate_passes(self):
		assert _check(_doc(sign=-1), _payload(sign=-1)) is None

	def test_a_mirrored_rounded_void_passes_because_rounding_is_not_in_grand_total(self):
		# Codex PR #66: a void of a pre-RT-80 sale mirrors disable_rounded_total=0. ERPNext v15 keeps
		# grand_total = net_total (unrounded) without taxes; the rounding lives only in rounded_total /
		# rounding_adjustment (taxes_and_totals.calculate_totals / set_rounded_total), which are not compared.
		doc = _doc(sign=-1)
		doc.update(rounded_total=-29.0, rounding_adjustment=0.49)
		assert _check(doc, _payload(sign=-1)) is None

	def test_unset_discount_and_margin_fields_count_as_zero(self):
		doc = _doc()
		for field in ("discount_percentage", "discount_amount", "margin_type", "margin_rate_or_amount"):
			doc["items"][0][field] = None
		assert _check(doc) is None


class TestLineMismatch:
	@pytest.mark.parametrize(
		("field", "value"),
		[
			("item_code", "ITEM-Z"),
			("qty", 3.0),
			("uom", "Box"),
			("rate", 9.0),
			("amount", 18.0),
			("warehouse", "Other WH"),
			("price_list_rate", 10.0),
			("discount_percentage", 5.0),
			("discount_amount", 0.5),
			("margin_type", "Amount"),
			("margin_rate_or_amount", 0.5),
		],
	)
	def test_each_compared_field_is_a_mismatch(self, field, value):
		reason = _check(_with_line(field, value))
		assert reason is not None
		assert reason.startswith("representation:")
		assert field in reason

	@pytest.mark.parametrize(
		"field", ["item_code", "qty", "uom", "rate", "amount", "warehouse", "price_list_rate"]
	)
	def test_a_missing_commercial_value_is_a_mismatch(self, field):
		assert _check(_with_line(field, None)) is not None

	def test_a_non_numeric_persisted_number_is_a_mismatch(self):
		assert _check(_with_line("qty", "two")) is not None

	def test_price_list_rate_must_equal_the_rate_sent(self):
		# ERPNext kept the rate but filled the list price from its own price list.
		assert _check(_with_line("price_list_rate", 12.0)) is not None

	def test_a_reversal_persisted_with_a_positive_qty_is_a_mismatch(self):
		doc = _doc(sign=-1)
		doc["items"][0]["qty"] = 2.0
		assert _check(doc, _payload(sign=-1)) is not None

	def test_an_extra_erp_row_is_a_mismatch(self):
		doc = _doc()
		doc["items"].append(_row("FREE-ITEM", "Nos", qty=1.0, rate=0.0, amount=0.0))
		doc["grand_total"] = 29.49
		reason = _check(doc)
		assert reason is not None and "row" in reason

	def test_a_missing_erp_row_is_a_mismatch(self):
		doc = _doc()
		doc["items"].pop()
		doc["grand_total"] = 19.0
		assert _check(doc) is not None

	def test_the_reason_names_the_line_position(self):
		assert "line 2" in _check(_with_line("rate", 10.0, line=1))


class TestDocumentMismatch:
	@pytest.mark.parametrize(
		("field", "value"), [("currency", "USD"), ("customer", "Other"), ("company", "Other Co")]
	)
	def test_each_document_field_is_a_mismatch(self, field, value):
		doc = _doc()
		doc[field] = value
		reason = _check(doc)
		assert reason is not None and field in reason

	@pytest.mark.parametrize("field", ["currency", "customer", "company"])
	def test_a_missing_document_value_is_a_mismatch(self, field):
		doc = _doc()
		doc[field] = None
		assert _check(doc) is not None

	def test_any_tax_row_is_a_mismatch_while_tax_is_deferred(self):
		doc = _doc()
		doc["taxes"] = [{"charge_type": "On Net Total", "rate": 14.0, "tax_amount": 4.13}]
		reason = _check(doc)
		assert reason is not None and "tax" in reason

	def test_a_grand_total_other_than_the_line_sum_is_a_mismatch(self):
		doc = _doc()
		doc["grand_total"] = 29.0  # e.g. a document-level discount
		reason = _check(doc)
		assert reason is not None and "grand_total" in reason

	def test_a_missing_grand_total_is_a_mismatch(self):
		doc = _doc()
		doc["grand_total"] = None
		assert _check(doc) is not None

	@pytest.mark.parametrize("company", [None, ""])
	def test_the_payload_must_carry_a_company(self, company):
		payload = {**_payload(), "company": company}
		with pytest.raises(RepresentationMismatch, match=r"^representation:"):
			expected_from_payload(payload)


class TestExemptions:
	def test_an_exempt_warehouse_is_not_compared(self):
		assert _check(_with_line("warehouse", "Original WH"), exempt=frozenset({"warehouse"})) is None

	def test_an_exempt_uom_and_customer_are_not_compared(self):
		doc = _with_line("uom", "Box")
		doc["customer"] = "Original Customer"
		assert _check(doc, exempt=frozenset({"uom", "customer"})) is None

	def test_an_unknown_exemption_is_refused(self):
		with pytest.raises(ValueError):
			expected_from_payload(_payload(), exempt=frozenset({"rate"}))


class TestMismatchException:
	def test_it_carries_the_prefixed_reason(self):
		exc = RepresentationMismatch("representation: line 1 rate 9.00 != sent 9.50")
		assert str(exc) == "representation: line 1 rate 9.00 != sent 9.50"


def _work_item(reversal_kind=None):
	wire = {
		"workItemRef": "11111111-1111-4111-8111-111111111111",
		"kind": "reversal" if reversal_kind else "sale_post",
		"sourceSystem": "pos-pulse",
		"externalId": "POS-1",
		"payloadHash": "a" * 64,
		"businessDate": "2026-06-01",
		"itemCursor": "7",
		"sale": {
			"saleRef": "22222222-2222-4222-8222-222222222222",
			"storeId": "33333333-3333-4333-8333-333333333333",
			"currencyCode": "EGP",
			"posTotal": "10.00",
			"occurredAt": "2026-06-01T10:00:00Z",
			"businessDate": "2026-06-01",
			"sourceSystem": "pos-pulse",
			"externalId": "POS-1",
			"lines": [
				{
					"lineRef": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
					"lineName": "A",
					"unitPrice": "10.00",
					"currencyCode": "EGP",
					"quantity": "1",
					"lineAmount": "10.00",
					"unit": "each",
					"erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"},
				}
			],
		},
	}
	if reversal_kind:
		wire["reversalOf"] = {
			"sourceSystem": "pos-pulse",
			"externalId": "POS-1",
			"reversalKind": reversal_kind,
		}
		if reversal_kind == "return":
			wire["reversalOf"]["recordedAt"] = "2026-06-05T09:30:00Z"
			wire["reversalOf"]["businessDate"] = "2026-06-05"
			wire["reversalOf"]["returnLines"] = [
				{"lineRef": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "quantity": "1", "lineAmount": "10.00"}
			]
			wire["reversalOf"]["refundTenders"] = [{"method": "cash", "amount": "10.00"}]
	return c.PostingWorkItem.from_wire(wire)


class TestReplayExemptions:
	"""On replay RT-331 owns the warehouse. The frozen resolution carries no UOM or customer, and
	today's maps may differ from the ones the invoice was posted with, so a replay never compares them
	(the insert-time check did). Same for every kind."""

	@pytest.mark.parametrize("kind", ["void", "refund", "return"])
	def test_a_credit_note_replay_exempts_warehouse_uom_and_customer(self, kind):
		# Its company is the ORIGINAL invoice's, which cannot change after submit: still compared.
		assert replay_exemptions(_work_item(kind)) == frozenset({"warehouse", "uom", "customer"})

	def test_a_sale_replay_also_exempts_the_company(self):
		# Codex PR #66: the expected company is today's site default, not a frozen value, so a changed
		# default must not read as document drift. The insert-time check verified it.
		assert replay_exemptions(_work_item()) == frozenset({"warehouse", "uom", "customer", "company"})

	def test_an_exempt_company_needs_none_in_the_payload(self):
		payload = {**_payload(), "company": None}
		doc = {**_doc(), "company": "Any Co"}
		assert _check(doc, payload, exempt=frozenset({"company"})) is None


class TestPricingFlagSplit:
	"""RT-378 finding 3: ``ignore_pricing_rule`` separates RT-377+ invoices (1) from older ones (0)."""

	def test_a_fresh_invoice_without_the_flag_is_a_mismatch(self):
		doc = {**_doc(), "ignore_pricing_rule": 0}
		reason = _check(doc)
		assert reason is not None and "ignore_pricing_rule" in reason

	def _old_invoice(self):
		"""A pre-RT-377 invoice: ERPNext filled its own list price and the difference as a discount,
		and its company was ERPNext's default (the builder set none)."""
		doc = {**_doc(), "ignore_pricing_rule": 0, "company": "ERPNext Default Co"}
		doc["items"][0].update(price_list_rate=10.0, discount_amount=0.5, margin_type="Amount")
		return doc

	def test_an_old_invoice_on_replay_skips_list_price_discount_margin_and_company(self):
		assert _check(self._old_invoice(), legacy_allowed=True) is None

	def test_an_old_invoice_on_replay_is_still_held_to_the_rate_it_was_sent(self):
		doc = self._old_invoice()
		doc["items"][0].update(rate=9.0, amount=18.0)
		doc["grand_total"] = 28.49
		assert _check(doc, legacy_allowed=True) is not None

	@pytest.mark.parametrize(
		("field", "value"), [("currency", "USD"), ("taxes", [{"tax_amount": 1.0}]), ("grand_total", 1.0)]
	)
	def test_an_old_invoice_on_replay_is_still_held_to_currency_tax_and_total(self, field, value):
		doc = {**self._old_invoice(), field: value}
		assert _check(doc, legacy_allowed=True) is not None

	def test_a_new_invoice_on_replay_gets_the_full_field_set(self):
		doc = _with_line("discount_amount", 0.5)
		assert _check(doc, legacy_allowed=True) is not None
