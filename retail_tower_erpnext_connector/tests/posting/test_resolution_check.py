# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt
"""RT-331 — verify an EXISTING ERP document against the work item's frozen resolution (pure).

The ERP Integration baseline: "matching provenance alone proves identity candidate, not
correctness". On a replay-guard or duplicate-recovery hit the connector compares the existing
invoice with the frozen refs before acking ``posted``. A mismatch (or a document that is not
submitted) means ``reconciliation_required``; ``None`` means the document matches.
"""

from retail_tower_erpnext_connector.connector.posting import contracts as c
from retail_tower_erpnext_connector.connector.posting.representation_check import (
	expected_from_payload,
	persisted_from_doc,
)
from retail_tower_erpnext_connector.connector.posting.resolution_check import (
	InvoiceLine,
	InvoiceSnapshot,
	check_existing,
	check_replayed_representation,
)

_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _line(ref, item):
	return {
		"lineRef": ref,
		"lineName": item,
		"unitPrice": "10.00",
		"currencyCode": "EGP",
		"quantity": "1",
		"lineAmount": "10.00",
		"unit": "each",
		"erpnextItemRef": {"doctype": "Item", "name": item},
	}


def _item(kind="sale_post", warehouse="WH-F"):
	sale = {
		"saleRef": "22222222-2222-4222-8222-222222222222",
		"storeId": "33333333-3333-4333-8333-333333333333",
		"currencyCode": "EGP",
		"posTotal": "20.00",
		"occurredAt": "2026-06-01T10:00:00Z",
		"businessDate": "2026-06-01",
		"sourceSystem": "pos-pulse",
		"externalId": "POS-1",
		"lines": [_line(_A, "ITEM-A"), _line(_B, "ITEM-B")],
	}
	if warehouse is not None:
		sale["warehouseRef"] = {"doctype": "Warehouse", "name": warehouse}
	wire = {
		"workItemRef": "11111111-1111-4111-8111-111111111111",
		"kind": kind,
		"sourceSystem": "pos-pulse",
		"externalId": "POS-1",
		"payloadHash": "a" * 64,
		"businessDate": "2026-06-01",
		"itemCursor": "7",
		"resolutionVersion": 1,
		"sale": sale,
	}
	if kind == "reversal":
		wire["reversalOf"] = {
			"sourceSystem": "pos-pulse",
			"externalId": "POS-1",
			"reversalKind": "void",
			"recordedAt": "2026-06-05T09:30:00Z",
			"businessDate": "2026-06-05",
		}
	return c.PostingWorkItem.from_wire(wire)


def _invoice(*lines, docstatus=1):
	return InvoiceSnapshot(docstatus=docstatus, items=tuple(InvoiceLine(*line) for line in lines))


class TestSalePost:
	def test_a_matching_invoice_passes(self):
		assert check_existing(_item(), _invoice(("ITEM-A", "WH-F", _A), ("ITEM-B", "WH-F", _B))) is None

	def test_a_different_item_on_a_frozen_line_is_a_mismatch(self):
		assert check_existing(_item(), _invoice(("ITEM-Z", "WH-F", _A), ("ITEM-B", "WH-F", _B)))

	def test_a_different_warehouse_is_a_mismatch(self):
		assert check_existing(_item(), _invoice(("ITEM-A", "WH-X", _A), ("ITEM-B", "WH-F", _B)))

	def test_no_frozen_warehouse_means_the_warehouse_is_not_compared(self):
		item = _item(warehouse=None)
		assert check_existing(item, _invoice(("ITEM-A", "WH-X", _A), ("ITEM-B", "WH-X", _B))) is None

	def test_a_missing_frozen_line_is_a_mismatch(self):
		assert check_existing(_item(), _invoice(("ITEM-A", "WH-F", _A)))

	def test_an_unknown_line_ref_is_a_mismatch(self):
		other = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
		assert check_existing(_item(), _invoice(("ITEM-A", "WH-F", _A), ("ITEM-B", "WH-F", other)))

	def test_without_line_refs_the_item_codes_must_match_as_a_multiset(self):
		assert check_existing(_item(), _invoice(("ITEM-B", "WH-F", None), ("ITEM-A", "WH-F", None))) is None
		assert check_existing(_item(), _invoice(("ITEM-A", "WH-F", None), ("ITEM-A", "WH-F", None)))

	def test_a_document_that_is_not_submitted_is_a_mismatch(self):
		assert check_existing(_item(), _invoice(("ITEM-A", "WH-F", _A), ("ITEM-B", "WH-F", _B), docstatus=2))


class TestReversal:
	def test_a_partial_return_passes_with_only_its_returned_line(self):
		assert check_existing(_return(_A), _invoice(("ITEM-A", "WH-F", _A))) is None

	def test_a_different_item_is_a_mismatch(self):
		assert check_existing(_item("reversal"), _invoice(("ITEM-Z", "WH-F", _A)))


def _return(*returned_refs):
	item = _item("reversal")
	wire_lines = [{"lineRef": ref, "quantity": "1", "lineAmount": "10.00"} for ref in returned_refs]
	return c.PostingWorkItem.from_wire(
		{
			"workItemRef": "11111111-1111-4111-8111-111111111111",
			"kind": "reversal",
			"sourceSystem": "pos-pulse",
			"externalId": "POS-1",
			"payloadHash": "a" * 64,
			"businessDate": "2026-06-01",
			"itemCursor": "7",
			"resolutionVersion": 1,
			"sale": {
				"saleRef": item.sale.sale_ref,
				"storeId": item.sale.store_id,
				"currencyCode": "EGP",
				"posTotal": "20.00",
				"occurredAt": "2026-06-01T10:00:00Z",
				"businessDate": "2026-06-01",
				"sourceSystem": "pos-pulse",
				"externalId": "POS-1",
				"lines": [_line(_A, "ITEM-A"), _line(_B, "ITEM-B")],
				"warehouseRef": {"doctype": "Warehouse", "name": "WH-F"},
			},
			"reversalOf": {
				"sourceSystem": "pos-pulse",
				"externalId": "POS-1",
				"reversalKind": "return",
				"recordedAt": "2026-06-05T09:30:00Z",
				"businessDate": "2026-06-05",
				"returnLines": wire_lines,
				"refundTenders": [{"method": "cash", "amount": "10.00"}],
			},
		}
	)


class TestReviewFindings:
	def test_a_return_must_match_the_lines_it_returned_not_any_sale_line(self):
		assert check_existing(_return(_A), _invoice(("ITEM-A", "WH-F", _A))) is None
		assert check_existing(_return(_A), _invoice(("ITEM-B", "WH-F", _B)))

	def test_a_void_must_cover_every_sale_line(self):
		assert check_existing(_item("reversal"), _invoice(("ITEM-A", "WH-F", _A))) is not None
		both = _invoice(("ITEM-A", "WH-F", _A), ("ITEM-B", "WH-F", _B))
		assert check_existing(_item("reversal"), both) is None

	def test_a_reversal_is_not_held_to_the_frozen_warehouse(self):
		# The reversal deliberately takes the ORIGINAL invoice rows' warehouses.
		original_rows = _invoice(("ITEM-A", "WH-ORIG", _A), ("ITEM-B", "WH-ORIG", _B))
		assert check_existing(_item("reversal"), original_rows) is None

	def test_stamped_rows_in_a_mixed_invoice_are_checked_by_identity(self):
		mixed = _invoice(("ITEM-B", "WH-F", _A), ("ITEM-A", "WH-F", None))
		assert check_existing(_item(), mixed)

	def test_a_duplicate_stamped_line_is_a_mismatch(self):
		dup = _invoice(("ITEM-A", "WH-F", _A), ("ITEM-A", "WH-F", _A), ("ITEM-B", "WH-F", _B))
		assert check_existing(_item(), dup)


def _mixed(kind="sale_post"):
	"""A sale with a referenced ITEM-A line and a legacy (unreferenced) ITEM-B line."""
	item = _item(kind)
	wire_lines = [_line(_A, "ITEM-A"), {k: v for k, v in _line(None, "ITEM-B").items() if k != "lineRef"}]
	return c.PostingWorkItem.from_wire(
		{
			"workItemRef": item.work_item_ref,
			"kind": kind,
			"sourceSystem": "pos-pulse",
			"externalId": "POS-1",
			"payloadHash": "a" * 64,
			"businessDate": "2026-06-01",
			"itemCursor": "7",
			"resolutionVersion": 1,
			"sale": {
				"saleRef": item.sale.sale_ref,
				"storeId": item.sale.store_id,
				"currencyCode": "EGP",
				"posTotal": "20.00",
				"occurredAt": "2026-06-01T10:00:00Z",
				"businessDate": "2026-06-01",
				"sourceSystem": "pos-pulse",
				"externalId": "POS-1",
				"lines": wire_lines,
				"warehouseRef": {"doctype": "Warehouse", "name": "WH-F"},
			},
		}
	)


class TestMixedReferencedAndLegacyLines:
	def test_an_unreferenced_sale_line_missing_from_the_invoice_is_a_mismatch(self):
		assert check_existing(_mixed(), _invoice(("ITEM-A", "WH-F", _A)))

	def test_the_unreferenced_line_is_matched_by_item_code(self):
		assert check_existing(_mixed(), _invoice(("ITEM-A", "WH-F", _A), ("ITEM-B", "WH-F", None))) is None


def _refund():
	"""A legacy refund reversal: unlinked, so its rows keep the frozen warehouse (not the original's)."""
	item = _item("reversal")
	return c.PostingWorkItem.from_wire(
		{
			"workItemRef": item.work_item_ref,
			"kind": "reversal",
			"sourceSystem": "pos-pulse",
			"externalId": "POS-1",
			"payloadHash": "a" * 64,
			"businessDate": "2026-06-01",
			"itemCursor": "7",
			"resolutionVersion": 1,
			"sale": {
				"saleRef": item.sale.sale_ref,
				"storeId": item.sale.store_id,
				"currencyCode": "EGP",
				"posTotal": "20.00",
				"occurredAt": "2026-06-01T10:00:00Z",
				"businessDate": "2026-06-01",
				"sourceSystem": "pos-pulse",
				"externalId": "POS-1",
				"lines": [_line(_A, "ITEM-A"), _line(_B, "ITEM-B")],
				"warehouseRef": {"doctype": "Warehouse", "name": "WH-F"},
			},
			"reversalOf": {
				"sourceSystem": "pos-pulse",
				"externalId": "POS-1",
				"reversalKind": "refund",
				"recordedAt": "2026-06-05T09:30:00Z",
				"businessDate": "2026-06-05",
			},
		}
	)


class TestRefundIsHeldToTheFrozenWarehouse:
	def test_a_refund_on_another_warehouse_is_a_mismatch(self):
		assert check_existing(_refund(), _invoice(("ITEM-A", "WH-X", _A), ("ITEM-B", "WH-X", _B)))

	def test_a_refund_on_the_frozen_warehouse_matches(self):
		assert check_existing(_refund(), _invoice(("ITEM-A", "WH-F", _A), ("ITEM-B", "WH-F", _B))) is None


def _expected(rate="10.00"):
	"""RT-378: the representation a replay rebuilds from the work item (both sale lines)."""
	line = {"qty": "1", "uom": "Nos", "rate": rate, "price_list_rate": rate, "amount": "10.00", "warehouse": "WH-F"}
	payload = {
		"currency": "EGP",
		"customer": "Walk-in",
		"company": "RT Co",
		"items": [{"item_code": "ITEM-A", **line}, {"item_code": "ITEM-B", **line}],
	}
	return expected_from_payload(payload, exempt=frozenset({"warehouse"}))


def _persisted(rate=10.0, flag=1):
	row = {
		"qty": 1.0,
		"uom": "Nos",
		"rate": rate,
		"amount": 10.0,
		"warehouse": "WH-F",
		"price_list_rate": rate,
		"discount_percentage": 0.0,
		"discount_amount": 0.0,
		"margin_type": "",
		"margin_rate_or_amount": 0.0,
	}
	return persisted_from_doc(
		{
			"currency": "EGP",
			"customer": "Walk-in",
			"company": "RT Co",
			"ignore_pricing_rule": flag,
			"grand_total": 20.0,
			"taxes": [],
			"items": [{"item_code": "ITEM-A", **row}, {"item_code": "ITEM-B", **row}],
		}
	)


def _represented(representation):
	return InvoiceSnapshot(
		docstatus=1,
		items=(InvoiceLine("ITEM-A", "WH-F", _A), InvoiceLine("ITEM-B", "WH-F", _B)),
		representation=representation,
	)


class TestRepresentationOnReplay:
	"""RT-378: a replay / recovery also checks the representation fields (RT-283 B.3)."""

	def test_a_matching_representation_passes(self):
		assert check_existing(_item(), _represented(_persisted()), expected=_expected()) is None

	def test_a_repriced_existing_invoice_is_a_mismatch(self):
		reason = check_existing(_item(), _represented(_persisted(rate=9.0)), expected=_expected())
		assert reason is not None and reason.startswith("representation:")

	def test_the_identity_checks_run_first(self):
		snapshot = InvoiceSnapshot(
			docstatus=2,
			items=(InvoiceLine("ITEM-A", "WH-F", _A), InvoiceLine("ITEM-B", "WH-F", _B)),
			representation=_persisted(rate=9.0),
		)
		assert "not submitted" in check_existing(_item(), snapshot, expected=_expected())

	def test_without_an_expected_representation_only_the_identity_is_checked(self):
		assert check_existing(_item(), _represented(_persisted(rate=9.0))) is None

	def test_an_old_invoice_is_checked_with_the_reduced_field_set(self):
		# RT-378 finding 3: pre-RT-377 (ignore_pricing_rule 0) is not held to list price / discount.
		old = _persisted(flag=0)
		assert check_existing(_item(), _represented(old), expected=_expected()) is None

	def test_the_representation_alone_can_be_checked_for_an_unversioned_replay(self):
		# Unversioned work items skip the RT-331 identity checks but not the representation.
		assert check_replayed_representation(_represented(_persisted()), _expected()) is None
		reason = check_replayed_representation(_represented(_persisted(rate=9.0)), _expected())
		assert reason is not None and reason.startswith("representation:")

	def test_an_expected_representation_without_a_persisted_one_is_a_mismatch(self):
		reason = check_existing(_item(), _represented(None), expected=_expected())
		assert reason is not None and reason.startswith("representation:")
