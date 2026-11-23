# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-419 — credit / third-party sales post to the PAYER's ERPNext Customer (pure units, no frappe).

``Sale.responsibilities`` (posting-feed 1.8.0-draft, RT-417) is parsed strictly, the builder makes the
payer's Customer the debtor, the tenders stay ``payments`` rows (RT-78) and the residual stays outstanding
on the payer. Fail closed: a null, blank, unknown, disabled or frozen payer, a second payer, or a total that
does not cover the lines is ``validation`` and never falls back to the store's Customer. A sale without
``responsibilities`` builds exactly as before.
"""

from typing import ClassVar

import pytest

from retail_tower_erpnext_connector.connector.posting import builder as b
from retail_tower_erpnext_connector.connector.posting import contracts as c
from retail_tower_erpnext_connector.connector.posting import payer as p
from retail_tower_erpnext_connector.connector.posting import tender as t
from retail_tower_erpnext_connector.connector.posting.errors import UnmappedPayer
from retail_tower_erpnext_connector.connector.posting.reversal_policy import (
	UnsupportedReversal,
	assert_reversal_supported,
)

_PAYER = "Acme Insurance"
_MODES = t.TenderModeMap({"cash": "Cash", "card_external": "Card Clearing"}).resolve


def _resp(amount="200.00", *, ref=_PAYER, sequence=1):
	return {"payerExternalRef": ref, "amount": amount, "sequence": sequence}


_UNSET = object()


def _work_item(*, tenders=None, responsibilities=_UNSET, kind="sale_post", total="200.00", **sale_extra):
	sale = {
		"saleRef": "22222222-2222-4222-8222-222222222222",
		"storeId": "33333333-3333-4333-8333-333333333333",
		"currencyCode": "EGP",
		"posTotal": total,
		"occurredAt": "2026-06-01T10:00:00Z",
		"businessDate": "2026-06-01",
		"sourceSystem": "pos-pulse",
		"externalId": "POS-9001",
		"lines": [
			{
				"lineName": "Item A",
				"unitPrice": "100.00",
				"currencyCode": "EGP",
				"quantity": "2",
				"lineAmount": "200.00",
				"taxAmount": None,
				"unit": "each",
				"erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"},
			}
		],
		**sale_extra,
	}
	if tenders is not None:
		sale["tenders"] = tenders
	if responsibilities is not _UNSET:
		sale["responsibilities"] = responsibilities
	return c.PostingWorkItem.from_wire(
		{
			"workItemRef": "11111111-1111-4111-8111-111111111111",
			"kind": kind,
			"sourceSystem": "pos-pulse",
			"externalId": "POS-9001",
			"payloadHash": "a" * 64,
			"businessDate": "2026-06-01",
			"itemCursor": "cursor-1",
			"sale": sale,
		}
	)


def _uom(unit):
	return "Nos"


def _warehouse(store_id):
	return {"doctype": "Warehouse", "name": "Main - RT"}


def _store_customer(store_id):
	return "Walk-in Customer - RT"


def _payer_customer(ref):
	return ref  # the identity mapping (RT-419 decision 1); existence is the glue's read


def _build(work_item, **kw):
	return b.build_sales_invoice(
		work_item,
		uom_for=_uom,
		warehouse_for=_warehouse,
		customer_for=kw.pop("customer_for", _store_customer),
		payer_customer_for=kw.pop("payer_customer_for", _payer_customer),
		mode_of_payment_for=kw.pop("mode_of_payment_for", _MODES),
		**kw,
	)


class TestParsing:
	def test_absent_responsibilities_is_the_legacy_sale(self):
		assert _work_item().sale.responsibilities == ()

	def test_a_single_payer_is_parsed(self):
		(r,) = _work_item(responsibilities=[_resp("120.50", sequence=2)]).sale.responsibilities
		assert (r.payer_external_ref, r.amount, r.sequence) == (_PAYER, "120.50", 2)

	@pytest.mark.parametrize(
		"raw",
		[
			None,  # an explicit null must not fall through to "no responsibilities" (wrong debtor)
			[],
			"x",
			{"payerExternalRef": _PAYER},
			[_resp(), _resp(sequence=2, ref="Other")],  # maxItems 1: one SI per work item
			["not an object"],
			[{"payerExternalRef": _PAYER, "amount": "5.00"}],  # sequence is required
			[_resp("0.00")],  # strictly positive
			[_resp("0")],
			[_resp("-1.00")],
			[_resp("1.23456")],
			[_resp("5.00\n")],  # a trailing newline never matches (ECMA `$` does not accept it)
			[_resp("0.00\n")],
			[_resp(200)],  # a number, never a float on the wire
			[_resp(sequence=0)],
			[_resp(sequence=True)],
			[_resp(ref=7)],
		],
	)
	def test_a_malformed_list_is_a_parse_error_that_acks_validation(self, raw):
		with pytest.raises(ValueError):
			_work_item(responsibilities=raw)

	@pytest.mark.parametrize("ref", [None, "", "   "])
	def test_a_null_or_blank_payer_ref_parses_and_is_rejected_by_the_builder(self, ref):
		work_item = _work_item(responsibilities=[_resp(ref=ref)])
		with pytest.raises(UnmappedPayer, match="blank"):
			_build(work_item)


class TestPayerResolution:
	def test_an_enabled_customer_resolves_to_its_exact_name(self):
		assert p.resolve_payer_customer(_PAYER, {"name": _PAYER, "disabled": 0, "is_frozen": 0}) == _PAYER

	@pytest.mark.parametrize(
		"row",
		[
			None,
			{"name": _PAYER.lower(), "disabled": 0, "is_frozen": 0},  # a case-folded hit is not the link
			{"name": _PAYER, "disabled": 1, "is_frozen": 0},
			{"name": _PAYER, "disabled": 0, "is_frozen": 1},
		],
	)
	def test_a_missing_disabled_frozen_or_case_folded_customer_is_unmapped(self, row):
		with pytest.raises(UnmappedPayer):
			p.resolve_payer_customer(_PAYER, row)


class TestSinglePayer:
	def test_full_credit_is_an_unpaid_invoice_on_the_payers_customer(self):
		doc = _build(_work_item(responsibilities=[_resp("200.00")]))
		assert doc["customer"] == _PAYER
		assert "is_pos" not in doc and "payments" not in doc  # ERPNext refuses is_pos with no payment row
		assert doc["update_stock"] == 1 and doc["disable_rounded_total"] == 1
		assert doc["rt_external_id"] == "POS-9001"  # provenance (exactly-once) is untouched

	def test_the_store_customer_is_never_consulted(self):
		def _no_store(store_id):
			raise AssertionError("the store map must not be read for a payer-bearing sale")

		doc = _build(_work_item(responsibilities=[_resp("200.00")]), customer_for=_no_store)
		assert doc["customer"] == _PAYER

	def test_the_payer_resolver_receives_the_ref_verbatim(self):
		seen = []

		def _capture(ref):
			seen.append(ref)
			return ref

		_build(_work_item(responsibilities=[_resp("200.00")]), payer_customer_for=_capture)
		assert seen == [_PAYER]


class TestCashPlusCredit:
	def test_tenders_become_payment_rows_and_the_residual_stays_on_the_payer(self):
		doc = _build(
			_work_item(
				tenders=[{"method": "cash", "amount": "50.00"}],
				responsibilities=[_resp("150.00")],
			)
		)
		assert doc["customer"] == _PAYER
		assert doc["is_pos"] == 1
		assert doc["payments"] == [{"mode_of_payment": "Cash", "amount": "50.00"}]

	def test_split_tenders_keep_one_row_per_tender_in_sequence_order(self):
		doc = _build(
			_work_item(
				tenders=[
					{"method": "card_external", "amount": "20.00", "reference": "AB12", "sequence": 2},
					{"method": "cash", "amount": "30.00", "sequence": 1},
				],
				responsibilities=[_resp("150.00")],
			)
		)
		assert [row["amount"] for row in doc["payments"]] == ["30.00", "20.00"]

	def test_an_unmapped_tender_method_still_fails_closed(self):
		with pytest.raises(t.UnmappedTender):
			_build(
				_work_item(
					tenders=[{"method": "cash", "amount": "50.00"}], responsibilities=[_resp("150.00")]
				),
				mode_of_payment_for=t.TenderModeMap({}).resolve,
			)


class TestFailClosed:
	def test_no_payer_resolver_wired_never_falls_back_to_the_store(self):
		with pytest.raises(UnmappedPayer):
			b.build_sales_invoice(
				_work_item(responsibilities=[_resp("200.00")]),
				uom_for=_uom,
				warehouse_for=_warehouse,
				customer_for=_store_customer,
			)

	def test_an_unknown_payer_is_unmapped(self):
		def _unknown(ref):
			raise UnmappedPayer(ref, "no such Customer")

		with pytest.raises(UnmappedPayer):
			_build(_work_item(responsibilities=[_resp("200.00")]), payer_customer_for=_unknown)

	@pytest.mark.parametrize(
		("tenders", "amount"),
		[
			(None, "199.99"),  # credit alone does not cover the lines
			(None, "200.01"),
			([{"method": "cash", "amount": "50.00"}], "149.99"),
			([{"method": "cash", "amount": "50.00"}], "150.01"),
			([{"method": "cash", "amount": "200.00"}], "1.00"),  # tenders alone cover it: credit adds
		],
	)
	def test_a_total_that_does_not_cover_the_lines_is_rejected(self, tenders, amount):
		with pytest.raises(t.CoverageMismatch):
			_build(_work_item(tenders=tenders, responsibilities=[_resp(amount)]))

	def test_coverage_is_against_the_lines_not_the_pos_total(self):
		# posTotal differing from the lines is a reconciliation case: the connector never forces a match.
		with pytest.raises(t.CoverageMismatch):
			_build(_work_item(total="999.00", responsibilities=[_resp("199.00")]))
		doc = _build(_work_item(total="999.00", responsibilities=[_resp("200.00")]))
		assert doc["customer"] == _PAYER

	def test_coverage_compares_exact_decimals(self):
		doc = _build(
			_work_item(tenders=[{"method": "cash", "amount": "0.10"}], responsibilities=[_resp("199.9")])
		)
		assert doc["payments"] == [{"mode_of_payment": "Cash", "amount": "0.10"}]


class TestUnchangedPaths:
	def test_a_sale_without_responsibilities_builds_through_the_store_map_exactly_as_before(self):
		doc = _build(_work_item(tenders=[{"method": "cash", "amount": "200.00"}]))
		assert doc["customer"] == "Walk-in Customer - RT"
		assert doc["is_pos"] == 1
		assert doc["payments"] == [{"mode_of_payment": "Cash", "amount": "200.00"}]

	def test_a_tender_unknown_sale_is_still_an_unpaid_invoice_on_the_store_customer(self):
		doc = _build(_work_item())
		assert doc["customer"] == "Walk-in Customer - RT"
		assert "is_pos" not in doc and "payments" not in doc

	def test_a_tender_mismatch_without_responsibilities_is_still_a_tender_mismatch(self):
		with pytest.raises(t.TenderMismatch):
			_build(_work_item(tenders=[{"method": "cash", "amount": "150.00"}]))

	def test_legacy_build_payments_call_is_unchanged(self):
		rows = t.build_payments(
			[c.SaleTender("cash", "20.00")], line_amounts=["20.00"], mode_of_payment_for=_MODES
		)
		assert rows == [{"mode_of_payment": "Cash", "amount": "20.00"}]
		assert t.build_payments([], line_amounts=["20.00"], mode_of_payment_for=None) == []


class TestPartialSettlement:
	"""The pre-submit check for a payer-bearing sale: paid == Σ tenders, outstanding == Σ responsibilities."""

	_OK: ClassVar[dict[str, float]] = {
		"grand_total": 200.0,
		"paid_amount": 50.0,
		"change_amount": 0.0,
		"write_off_amount": 0.0,
		"outstanding_amount": 150.0,
	}

	def test_an_exact_partial_settlement_passes(self):
		t.assert_partially_settled(self._OK, expected_paid="50.00", expected_outstanding="150.00")

	def test_full_credit_is_all_outstanding(self):
		totals = {**self._OK, "paid_amount": 0.0, "outstanding_amount": 200.0}
		t.assert_partially_settled(totals, expected_paid="0", expected_outstanding="200.00")

	@pytest.mark.parametrize(
		"drift",
		[
			{"paid_amount": 49.0, "outstanding_amount": 151.0},
			{"outstanding_amount": 149.99},
			{"change_amount": 1.0},
			{"write_off_amount": 0.01},
			{"grand_total": 199.99},
		],
	)
	def test_any_drift_is_rejected(self, drift):
		with pytest.raises(t.SettlementDrift):
			t.assert_partially_settled(
				{**self._OK, **drift}, expected_paid="50.00", expected_outstanding="150.00"
			)

	def test_a_receivable_account_in_another_currency_is_refused_not_compared(self):
		# outstanding_amount is in the party account's currency: 7500 EGP is not 150 USD, and the
		# connector converts nothing.
		totals = {
			**self._OK,
			"outstanding_amount": 7500.0,
			"currency": "USD",
			"party_account_currency": "EGP",
		}
		with pytest.raises(t.SettlementDrift, match="receivable account currency"):
			t.assert_partially_settled(totals, expected_paid="50.00", expected_outstanding="150.00")

	def test_the_same_currency_on_both_passes(self):
		totals = {**self._OK, "currency": "EGP", "party_account_currency": "EGP"}
		t.assert_partially_settled(totals, expected_paid="50.00", expected_outstanding="150.00")


def _void_of(*, with_responsibilities: bool):
	sale = {
		"saleRef": "22222222-2222-4222-8222-222222222222",
		"storeId": "33333333-3333-4333-8333-333333333333",
		"currencyCode": "EGP",
		"posTotal": "200.00",
		"occurredAt": "2026-06-01T10:00:00Z",
		"businessDate": "2026-06-01",
		"sourceSystem": "pos-pulse",
		"externalId": "POS-9001",
		"lines": [
			{
				"lineName": "Item A",
				"unitPrice": "100.00",
				"currencyCode": "EGP",
				"quantity": "2",
				"lineAmount": "200.00",
				"taxAmount": None,
				"unit": "each",
				"erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"},
			}
		],
	}
	if with_responsibilities:
		sale["responsibilities"] = [_resp("200.00")]
	return c.PostingWorkItem.from_wire(
		{
			"workItemRef": "11111111-1111-4111-8111-111111111112",
			"kind": "reversal",
			"sourceSystem": "pos-pulse",
			"externalId": "POS-9001",
			"payloadHash": "a" * 64,
			"businessDate": "2026-06-02",
			"itemCursor": "cursor-2",
			"reversalOf": {
				"sourceSystem": "pos-pulse",
				"externalId": "POS-9001",
				"reversalKind": "void",
				"recordedAt": "2026-06-02T10:00:00Z",
				"businessDate": "2026-06-02",
			},
			"sale": sale,
		}
	)


class TestReversalOfAPayerSale:
	"""The contract leaves a reversal of a responsibility-bearing sale undefined: never post it."""

	def test_a_void_of_a_payer_sale_is_unsupported(self):
		with pytest.raises(UnsupportedReversal, match="responsib"):
			assert_reversal_supported(_void_of(with_responsibilities=True))

	def test_a_void_of_an_ordinary_sale_stays_supported(self):
		assert_reversal_supported(_void_of(with_responsibilities=False))  # raises nothing
