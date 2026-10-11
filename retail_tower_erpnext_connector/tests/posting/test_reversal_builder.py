# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Arc A S1 — local unit tests for the work-item → reversing-document builder.

A reversal work-item (012 ``kind=reversal``) posts a **negative-qty return Sales Invoice**
(``is_return=1`` — in ERPNext a credit note IS a Sales Invoice with ``is_return=1``), chosen for
symmetric DP-017 reconciliation against the 1:1 forward SI.

THE CRITICAL CORRECTNESS CONSTRAINT (F-002 + Connector #28): the unique provenance index spans
ALL Sales Invoices via ``rt_external_id``. DP2 emits the ORIGINAL sale's ``externalId`` as the
top-level anchor on a reversal work-item — so ``work_item.externalId == reversalOf.externalId ==
sale.externalId`` (all the original sale's id). The per-reversal-distinct on-wire value is
``workItemRef``. The reversing builder MUST write ``workItemRef`` into ``rt_external_id`` — NOT the
top-level ``externalId`` (which is the original's). Writing the original's id would collide with the
original SI's unique key and be FALSELY treated as a dup-recovery, silently echoing the original
invoice with no credit note (#28). The fixture below now models the REAL wire: ``externalId`` is the
SAME as the original (``POS-9001``) and ``workItemRef`` is the distinct discriminator, so a builder
reading ``externalId`` instead of ``workItemRef`` fails this test.

Pure dict-transform: no frappe, no ERPNext call. Composes on the forward builder so money
conformance (FR-009) runs once on positive magnitudes; qty/amount are then negated.
"""

import pytest

from retail_tower_erpnext_connector.connector.posting import contracts as c
from retail_tower_erpnext_connector.connector.posting import reversal_builder as rb


# A reversal work-item modeling the REAL DP2 wire (Connector #28): the top-level externalId is the
# ORIGINAL sale's id ("POS-9001") — IDENTICAL to reversalOf.externalId AND sale.externalId. The
# per-reversal-distinct value is workItemRef. This fixture design is load-bearing for the F-002/#28
# test (see module docstring): distinctness is varied via `work_item_ref`, NOT `external_id`.
def _reversal_work_item(
	*,
	work_item_ref: str = "55555555-5555-4555-8555-555555555555",
	external_id: str = "POS-9001",
	**overrides,
) -> c.PostingWorkItem:
	line = {
		"lineName": "Item A",
		"unitPrice": "100.00",
		"currencyCode": "EGP",
		"quantity": "2",
		"lineAmount": "200.00",
		"taxAmount": "30.00",
		"unit": "each",
		"erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"},
	}
	wire = {
		"workItemRef": work_item_ref,  # the per-reversal-distinct discriminator (#28 re-key target)
		"kind": "reversal",
		"sourceSystem": "pos-pulse",
		"externalId": external_id,  # DP2 emits the ORIGINAL sale's id here (NOT per-reversal-distinct)
		"payloadHash": "b" * 64,
		"businessDate": "2026-06-05",
		"itemCursor": "cursor-2",
		"reversalOf": {
			"sourceSystem": "pos-pulse",
			"externalId": "POS-9001",  # the ORIGINAL sale id — must NOT be written to rt_external_id
			"reversalKind": "refund",
		},
		"sale": {
			"saleRef": "22222222-2222-4222-8222-222222222222",
			"storeId": "33333333-3333-4333-8333-333333333333",
			"currencyCode": "EGP",
			"posTotal": "230.00",
			"occurredAt": "2026-06-01T10:00:00Z",
			"businessDate": "2026-06-01",  # the ORIGINAL business date — credit note posts on its OWN
			"sourceSystem": "pos-pulse",
			"externalId": "POS-9001",  # the ORIGINAL sale id (snapshot) — must NOT leak into rt_external_id
			"lines": [line],
		},
	}
	wire.update(overrides)
	return c.PostingWorkItem.from_wire(wire)


def _reversal_work_item_two_lines(lines: list[dict]) -> c.PostingWorkItem:
	"""A reversal work-item carrying MULTIPLE sale lines (for the multi-line negation test)."""
	wire = {
		"workItemRef": "55555555-5555-4555-8555-555555555555",
		"kind": "reversal",
		"sourceSystem": "pos-pulse",
		"externalId": "POS-9001",  # the ORIGINAL sale's id (real wire, #28)
		"payloadHash": "b" * 64,
		"businessDate": "2026-06-05",
		"itemCursor": "cursor-2",
		"reversalOf": {
			"sourceSystem": "pos-pulse",
			"externalId": "POS-9001",
			"reversalKind": "refund",
		},
		"sale": {
			"saleRef": "22222222-2222-4222-8222-222222222222",
			"storeId": "33333333-3333-4333-8333-333333333333",
			"currencyCode": "EGP",
			"posTotal": "246.50",
			"occurredAt": "2026-06-01T10:00:00Z",
			"businessDate": "2026-06-01",
			"sourceSystem": "pos-pulse",
			"externalId": "POS-9001",
			"lines": lines,
		},
	}
	return c.PostingWorkItem.from_wire(wire)


def _uom(unit: str) -> str:
	mapping = {"each": "Nos"}
	if unit not in mapping:
		from retail_tower_erpnext_connector.connector.posting import builder as b

		raise b.UnmappedUnit(unit)
	return mapping[unit]


def _warehouse(store_id: str) -> dict:
	return {"doctype": "Warehouse", "name": "Main - RT"}


def _customer(store_id: str) -> str:
	return "Walk-in Customer - RT"


def _build(work_item=None, **kw):
	return rb.build_reversing_invoice(
		work_item or _reversal_work_item(),
		uom_for=_uom,
		warehouse_for=_warehouse,
		customer_for=_customer,
		**kw,
	)


class TestF002ProvenanceIdentity:
	def test_fixture_models_real_wire_external_id_is_the_original_sales(self):
		# THE "false fixture corrected" guard (Connector #28). DP2 emits the ORIGINAL sale's id as
		# the reversal's top-level externalId. Assert the fixture now matches that REAL wire shape:
		# work_item.external_id == reversal_of.external_id == sale.external_id == "POS-9001", while
		# work_item_ref is the distinct discriminator. The OLD fixture (distinct externalId) was a
		# FALSE wire shape that masked the #28 bug.
		wi = _reversal_work_item()
		assert wi.external_id == "POS-9001"
		assert wi.external_id == wi.reversal_of.external_id
		assert wi.external_id == wi.sale.external_id
		assert wi.work_item_ref != wi.external_id

	def test_rt_external_id_is_work_item_ref_not_the_shared_external_id(self):
		# THE HEADLINE TEST (Connector #28). The reversing doc's rt_external_id MUST be the reversal
		# work-item's work_item_ref (the per-reversal-distinct on-wire value), NOT the top-level
		# externalId — which is the ORIGINAL sale's id (also in reversalOf.externalId and the sale
		# snapshot). A builder reading external_id would collide with the original SI's unique
		# provenance slot, be falsely treated as dup-recovery, and silently echo the original invoice.
		wi = _reversal_work_item()
		doc = _build(wi)
		assert doc["rt_external_id"] == wi.work_item_ref
		# Explicitly prove the shared original-sale id was NOT used.
		assert doc["rt_external_id"] != wi.external_id  # "POS-9001"
		assert doc["rt_external_id"] != wi.reversal_of.external_id  # "POS-9001"
		assert doc["rt_external_id"] != wi.sale.external_id  # "POS-9001"

	def test_rt_source_system_is_work_item_own(self):
		wi = _reversal_work_item()
		doc = _build(wi)
		assert doc["rt_source_system"] == wi.source_system


class TestNegativeQty:
	def test_qty_is_negated(self):
		# A return Sales Invoice carries NEGATIVE line quantities.
		doc = _build()
		assert doc["items"][0]["qty"] == "-2"

	def test_amount_is_negated(self):
		# amount mirrors qty sign so the credit note totals are negative.
		doc = _build()
		assert doc["items"][0]["amount"] == "-200.00"

	def test_rate_stays_positive(self):
		# ERPNext return semantics: the unit rate stays positive; the qty carries the sign.
		doc = _build()
		assert doc["items"][0]["rate"] == "100.00"

	def test_qty_and_amount_are_strings_not_float(self):
		item = _build()["items"][0]
		for value in (item["qty"], item["amount"], item["rate"]):
			assert isinstance(value, str)
			assert not isinstance(value, float)

	def test_every_line_is_negated_multi_line(self):
		# A single-line fixture would let a bug that negates only items[0] (or copies it) slip
		# through. This work-item carries TWO lines with DISTINCT magnitudes; assert EVERY line's
		# qty AND amount are negated independently (rate stays positive on each).
		two_lines = [
			{
				"lineName": "Item A",
				"unitPrice": "100.00",
				"currencyCode": "EGP",
				"quantity": "2",
				"lineAmount": "200.00",
				"taxAmount": "30.00",
				"unit": "each",
				"erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"},
			},
			{
				"lineName": "Item B",
				"unitPrice": "5.50",
				"currencyCode": "EGP",
				"quantity": "3",
				"lineAmount": "16.50",
				"taxAmount": "0.00",
				"unit": "each",
				"erpnextItemRef": {"doctype": "Item", "name": "ITEM-B"},
			},
		]
		doc = _build(_reversal_work_item_two_lines(two_lines))
		items = doc["items"]
		assert len(items) == 2
		# Line 0 negated.
		assert items[0]["qty"] == "-2"
		assert items[0]["amount"] == "-200.00"
		assert items[0]["rate"] == "100.00"
		# Line 1 negated INDEPENDENTLY (distinct magnitudes — a copy-of-items[0] bug fails here).
		assert items[1]["qty"] == "-3"
		assert items[1]["amount"] == "-16.50"
		assert items[1]["rate"] == "5.50"


class TestNegateHelper:
	def test_positive_integer_negated(self):
		assert rb._negate("2") == "-2"

	def test_positive_decimal_negated(self):
		assert rb._negate("200.00") == "-200.00"

	def test_already_negative_raises(self):
		# Fail-closed: an already-negative magnitude is a contract violation, NOT a silent
		# double-negation back to a spurious positive.
		with pytest.raises(ValueError):
			rb._negate("-5.00")

	def test_zero_variants_pass_through_unchanged(self):
		for zero in ("0", "0.0", "0.00", "0.000", "0.0000"):
			assert rb._negate(zero) == zero


class TestIsReturnAndReturnAgainst:
	def test_is_return_flag_set(self):
		doc = _build()
		assert doc["is_return"] == 1

	def test_doctype_is_sales_invoice(self):
		# A credit note IS a Sales Invoice with is_return=1 (CHECKPOINT-1 decision).
		doc = _build()
		assert doc["doctype"] == "Sales Invoice"

	def test_return_against_not_resolved_in_pure_layer(self):
		# The pure builder cannot resolve reversal_of -> the original SI's ERPNext docname
		# (that needs a DB hit; the injected signature has no SI-resolver). It emits a standalone
		# is_return=1 credit note; the bench-pending frappe_glue leg sets return_against before
		# insert. ERPNext accepts is_return=1 WITHOUT return_against, so the payload is valid.
		doc = _build()
		assert doc.get("return_against") is None


class TestCardinalityNto1:
	def test_two_reversals_sharing_one_original_get_distinct_rt_external_id(self):
		# Reversal->original is N:1 (successive partial returns). Per the REAL wire (#28), both
		# reversals SHARE the original sale's top-level externalId ("POS-9001"); distinctness comes
		# ONLY from work_item_ref. Two reversals of the same original sale must still produce
		# DISTINCT rt_external_id (else they'd collide in the unique provenance index).
		wir_a = "55555555-5555-4555-8555-55555555000a"
		wir_b = "55555555-5555-4555-8555-55555555000b"
		doc_a = _build(_reversal_work_item(work_item_ref=wir_a))
		doc_b = _build(_reversal_work_item(work_item_ref=wir_b))
		assert doc_a["rt_external_id"] == wir_a
		assert doc_b["rt_external_id"] == wir_b
		assert doc_a["rt_external_id"] != doc_b["rt_external_id"]
		# ...yet both carry the SAME top-level externalId (the original sale) — distinctness is NOT
		# from external_id. This is exactly the #28 condition the re-key must survive.
		wi_a = _reversal_work_item(work_item_ref=wir_a)
		wi_b = _reversal_work_item(work_item_ref=wir_b)
		assert wi_a.external_id == wi_b.external_id == "POS-9001"
		assert wi_a.reversal_of.external_id == wi_b.reversal_of.external_id


class TestResolutionMirrorsForward:
	def test_customer_currency_warehouse_uom_resolution(self):
		doc = _build()
		assert doc["customer"] == "Walk-in Customer - RT"
		assert doc["currency"] == "EGP"
		assert doc["items"][0]["item_code"] == "ITEM-A"
		assert doc["items"][0]["uom"] == "Nos"
		assert doc["items"][0]["warehouse"] == "Main - RT"

	def test_unmapped_unit_propagates(self):
		from retail_tower_erpnext_connector.connector.posting import builder as b

		def _uom_unmapped(unit: str) -> str:
			raise b.UnmappedUnit(unit)

		with pytest.raises(b.UnmappedUnit):
			rb.build_reversing_invoice(
				_reversal_work_item(),
				uom_for=_uom_unmapped,
				warehouse_for=_warehouse,
				customer_for=_customer,
			)

	def test_unmapped_store_propagates(self):
		from retail_tower_erpnext_connector.connector.posting import uom as u

		def _unmapped(store_id: str) -> str:
			raise u.UnmappedStore(store_id)

		with pytest.raises(u.UnmappedStore):
			rb.build_reversing_invoice(
				_reversal_work_item(),
				uom_for=_uom,
				warehouse_for=_warehouse,
				customer_for=_unmapped,
			)


class TestPostingDate:
	def test_credit_note_posts_on_reversal_work_item_business_date(self):
		# The credit note must post on the REVERSAL's own businessDate (its fiscal period), NOT the
		# original sale's snapshot businessDate. The fixture sets work-item businessDate "2026-06-05"
		# vs the sale snapshot's original "2026-06-01".
		doc = _build()
		assert doc["posting_date"] == "2026-06-05"


class TestRejectsSalePost:
	def test_sale_post_work_item_is_rejected(self):
		# build_reversing_invoice is for reversal work-items only; a sale_post must not be built
		# into a credit note (it has no reversal_of and would mis-post).
		sale_post = c.PostingWorkItem.from_wire(
			{
				"workItemRef": "11111111-1111-4111-8111-111111111111",
				"kind": "sale_post",
				"sourceSystem": "pos-pulse",
				"externalId": "POS-9001",
				"payloadHash": "a" * 64,
				"businessDate": "2026-06-01",
				"itemCursor": "cursor-1",
				"sale": {
					"saleRef": "22222222-2222-4222-8222-222222222222",
					"storeId": "33333333-3333-4333-8333-333333333333",
					"currencyCode": "EGP",
					"posTotal": "230.00",
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
							"unit": "each",
							"erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"},
						}
					],
				},
			}
		)
		with pytest.raises(ValueError):
			rb.build_reversing_invoice(
				sale_post, uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer
			)


def _void_work_item() -> c.PostingWorkItem:
	"""The default reversal fixture, re-kinded to a FULL VOID (reversalKind=void)."""
	wi = _reversal_work_item()
	return c.PostingWorkItem(
		work_item_ref=wi.work_item_ref,
		kind=wi.kind,
		source_system=wi.source_system,
		external_id=wi.external_id,
		payload_hash=wi.payload_hash,
		business_date=wi.business_date,
		sale=wi.sale,
		item_cursor=wi.item_cursor,
		reversal_of=c.ReversalRef(
			source_system=wi.reversal_of.source_system,
			external_id=wi.reversal_of.external_id,
			reversal_kind="void",
		),
	)


class TestStockEffectByReversalKind:
	"""RT-48 (owner decision RT-47 D5): only a FULL VOID restores stock in this implementation.

	A refund is amount-only today (RecordRefundRequest carries no lines) while this builder negates
	EVERY sale line, so a refund with update_stock=1 would restock the full sold quantity. Refund
	stock semantics are deferred to RT-14/RT-16, so a refund credit note MUST NOT move stock.
	"""

	def test_refund_credit_note_never_moves_stock(self):
		doc = _build()  # the default fixture is reversalKind=refund
		assert doc["update_stock"] == 0

	def test_void_return_invoice_moves_stock_by_default(self):
		# Default 1: the glue MIRRORS the original's update_stock before insert (stock_policy). If that
		# step were ever skipped against a legacy update_stock=0 original, ERPNext refuses the return
		# loudly ("'Update Stock' can not be checked…") rather than silently restoring nothing.
		doc = _build(_void_work_item())
		assert doc["update_stock"] == 1
		assert doc["is_return"] == 1



class TestNoRounding:
	"""RT-80: a credit note inherits the forward builder's ``disable_rounded_total=1``.

	Otherwise a fractional void/return would round its negative total exactly as the sale did, and
	the credit note would not equal the reversed amount.
	"""

	def test_refund_credit_note_disables_rounded_total(self):
		doc = _build()  # the default fixture is reversalKind=refund
		assert doc["disable_rounded_total"] == 1

	def test_void_return_invoice_disables_rounded_total(self):
		doc = _build(_void_work_item())
		assert doc["disable_rounded_total"] == 1


class TestPostingTime:
	"""RT-49 (decision 10312): a reversal keeps its businessDate via set_posting_time=1."""

	def test_sets_set_posting_time_and_business_date(self):
		doc = rb.build_reversing_invoice(
			_reversal_work_item(), uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer
		)
		assert doc["set_posting_time"] == 1
		assert doc["posting_date"] == "2026-06-05"

	def test_default_stamp_clamps_a_sale_time_from_an_earlier_day(self):
		# The fixture's reversal businessDate (06-05) is after the sale's occurredAt day (06-01), so
		# the sale-derived time is clamped to the start of the reversal's business day.
		doc = rb.build_reversing_invoice(
			_reversal_work_item(), uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer
		)
		assert doc["posting_time"] == "00:00:00.000000"

	def test_applies_the_injected_stamp(self):
		from retail_tower_erpnext_connector.connector.posting import posting_time as pt

		doc = rb.build_reversing_invoice(
			_reversal_work_item(),
			uom_for=_uom,
			warehouse_for=_warehouse,
			customer_for=_customer,
			posting_stamp=pt.PostingStamp("2026-06-05", "09:30:00.000000"),
		)
		assert (doc["posting_date"], doc["posting_time"]) == ("2026-06-05", "09:30:00.000000")


def _with_tenders(work_item, *tenders):
	"""The reversal work item's sale snapshot carrying ``tenders`` (the ORIGINAL sale's tenders)."""
	import dataclasses

	return dataclasses.replace(work_item, sale=dataclasses.replace(work_item.sale, tenders=tuple(tenders)))


class TestVoidSettlement:
	"""RT-78 / RT-10 D6: a void refunds by mirroring the sale's tenders as NEGATIVE payments."""

	def test_void_mirrors_sale_tenders_as_negative_payments(self):
		wi = _with_tenders(
			_void_work_item(), c.SaleTender("cash", "150.00"), c.SaleTender("card_external", "50.00", "AB12")
		)
		doc = _build(wi)
		assert doc["is_pos"] == 1 and doc["is_return"] == 1
		# Amounts only: the Mode of Payment comes from the ORIGINAL invoice's rows in the glue
		# (stock_policy.link_void_to_original), never from the current tender map.
		assert doc["payments"] == [
			{"mode_of_payment": None, "amount": "-150.00"},
			{"mode_of_payment": None, "amount": "-50.00"},
		]

	def test_void_does_not_need_the_current_tender_map(self):
		# Codex P2 / Greptile, PR #48: a mapping removed after the sale must not make a fully
		# reconstructable void fail as UnmappedTender before the original invoice is read.
		wi = _with_tenders(_void_work_item(), c.SaleTender("cash", "200.00"))
		assert _build(wi)["payments"] == [{"mode_of_payment": None, "amount": "-200.00"}]

	def test_void_tenders_must_still_cover_the_line_total(self):
		from retail_tower_erpnext_connector.connector.posting import tender as t

		with pytest.raises(t.TenderMismatch):
			_build(_with_tenders(_void_work_item(), c.SaleTender("cash", "150.00")))

	def test_void_of_a_tender_unknown_sale_stays_an_outstanding_credit_note(self):
		doc = _build(_void_work_item())
		assert "is_pos" not in doc and "payments" not in doc

	def test_refund_never_mirrors_the_sale_tenders(self):
		# A legacy refund is rejected upstream (RT-71); even so the builder must not pay out the whole
		# sale's tenders on it. A return pays out its own refundTenders (RT-16), never sale.tenders.
		wi = _with_tenders(_reversal_work_item(), c.SaleTender("cash", "200.00"))
		doc = _build(wi)
		assert "is_pos" not in doc and "payments" not in doc



def _timed_void(recorded_at, business_date):
	"""The default reversal fixture as a VOID carrying the reversal's own RT-63 time and business date."""
	return _reversal_work_item(
		reversalOf={
			"sourceSystem": "pos-pulse",
			"externalId": "POS-9001",
			"reversalKind": "void",
			"recordedAt": recorded_at,
			"businessDate": business_date,
		}
	)


class TestReversalOwnTimestamp:
	"""RT-16 / RT-63 (decision 10348): a reversal posts ON its own businessDate AT its recordedAt.

	The sale (fixture) occurred 2026-06-01T10:00:00Z. Without the RT-63 fields the RT-49 fallback
	(the sale's time, clamped into the work item's businessDate) still applies.
	"""

	def test_late_void_posts_on_its_own_day_not_back_dated(self):
		doc = _build(_timed_void("2026-06-05T09:30:00Z", "2026-06-05"))
		assert (doc["posting_date"], doc["posting_time"]) == ("2026-06-05", "09:30:00.000000")

	def test_same_day_void_posts_at_its_recorded_time(self):
		doc = _build(_timed_void("2026-06-01T12:15:00Z", "2026-06-01"))
		assert (doc["posting_date"], doc["posting_time"]) == ("2026-06-01", "12:15:00.000000")

	def test_missing_fields_fall_back_to_the_sale_rule(self):
		# No recordedAt/businessDate (older Backend-Core): the sale's 10:00Z clamped into the work
		# item's businessDate 2026-06-05 → the start of that day (unchanged RT-49 behaviour).
		doc = _build(_void_work_item())
		assert (doc["posting_date"], doc["posting_time"]) == ("2026-06-05", "00:00:00.000000")

	def test_stamp_source_prefers_the_reversal_fields(self):
		assert rb.reversal_stamp_source(_timed_void("2026-06-05T09:30:00Z", "2026-06-05")) == (
			"2026-06-05T09:30:00Z",
			"2026-06-05",
		)

	def test_stamp_source_falls_back_to_the_sale_time_and_work_item_date(self):
		assert rb.reversal_stamp_source(_void_work_item()) == ("2026-06-01T10:00:00Z", "2026-06-05")

	def test_original_posted_later_still_raises_the_stamp(self):
		# A POS clock ahead of the server can leave the original invoice later than the void's
		# server-recorded time on the same day: the glue keeps raise_to_original (RT-49 guard).
		import datetime

		from retail_tower_erpnext_connector.connector.posting import posting_time as pt

		occurred, day = rb.reversal_stamp_source(_timed_void("2026-06-01T12:15:00Z", "2026-06-01"))
		stamp = pt.stamp_for(occurred, day, pt.UTC_CLOCK)
		raised = pt.raise_to_original(stamp, datetime.date(2026, 6, 1), datetime.timedelta(hours=13))
		assert (raised.posting_date, raised.posting_time) == ("2026-06-01", "13:00:00.000000")
		assert "raised_to_original" in raised.adjustments


class TestRepricingProtection:
	"""RT-377: refund and void credit notes inherit ``ignore_pricing_rule`` and a positive
	``price_list_rate`` equal to the rate from the forward builder."""

	def test_refund_credit_note_carries_both_protections(self):
		doc = _build()  # the default fixture is reversalKind=refund
		assert doc["ignore_pricing_rule"] == 1
		assert all(i["price_list_rate"] == i["rate"] for i in doc["items"])

	def test_void_credit_note_keeps_price_list_rate_positive(self):
		doc = _build(_void_work_item())
		assert doc["ignore_pricing_rule"] == 1
		for item in doc["items"]:
			assert item["price_list_rate"] == item["rate"]
			assert not item["price_list_rate"].startswith("-")


class TestExplicitCompany:
	def test_the_credit_note_carries_the_given_company(self):
		assert _build(company="RT Co")["company"] == "RT Co"
