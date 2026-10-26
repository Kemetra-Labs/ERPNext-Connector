# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-452 — local unit tests for the pure batch-posting policy (no frappe).

RT-50 Gate 7 G7-10 / G7-11 (owner decision RT-50 comment 11765): a batch Item posts only with the
Batch the feed supplies, a serial Item stays fail-closed, an Item whose flags disagree with
``trackingMode`` is ``validation``, a credit note takes the original row's Batch, and after submit the
ledger and the bundle must hold exactly the intended Batch and quantity.
"""

import pytest

from retail_tower_erpnext_connector.connector.posting import batch_policy as bp
from retail_tower_erpnext_connector.connector.posting import contracts as c
from retail_tower_erpnext_connector.connector.posting.stock_policy import (
	ReturnLineMismatch,
	UnsupportedTrackedItem,
)

_PLAIN = {"has_batch_no": 0, "has_serial_no": 0}
_BATCH = {"has_batch_no": 1, "has_serial_no": 0}
_SERIAL = {"has_batch_no": 0, "has_serial_no": 1}
_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _doc(*codes, update_stock=1, is_return=0, **row):
	return {
		"update_stock": update_stock,
		"is_return": is_return,
		"items": [{"item_code": code, "qty": "1", **row} for code in codes],
	}


def _lt(mode=None, ref=None):
	return bp.LineTracking(tracking_mode=mode, batch_ref=ref)


class TestTrackingMatrix:
	"""The per-row stock-policy matrix: ref / no ref x batch / untracked x serial x flag mismatch."""

	def test_untracked_lines_without_tracking_fields_pass(self):
		bp.assert_tracking_postable(_doc("A", "B"), [_lt(), _lt()], {"A": _PLAIN, "B": _PLAIN})

	def test_untracked_line_with_mode_none_passes(self):
		bp.assert_tracking_postable(_doc("A"), [_lt("none")], {"A": _PLAIN})

	def test_batch_line_with_its_ref_passes(self):
		bp.assert_tracking_postable(_doc("A"), [_lt("batch", "B1")], {"A": _BATCH})

	def test_batch_item_without_tracking_fields_fails_closed(self):
		# An older feed omits both fields: the RT-47 D4 fail-closed behaviour is unchanged.
		with pytest.raises(bp.TrackedLineRejected, match="trackingMode is None"):
			bp.assert_tracking_postable(_doc("A"), [_lt()], {"A": _BATCH})

	def test_batch_item_with_mode_batch_but_no_ref_fails_closed(self):
		with pytest.raises(bp.TrackedLineRejected, match="without an erpnextBatchRef"):
			bp.assert_tracking_postable(_doc("A"), [_lt("batch")], {"A": _BATCH})

	def test_batch_item_with_a_ref_but_no_mode_fails_closed(self):
		with pytest.raises(bp.TrackedLineRejected, match="trackingMode is None"):
			bp.assert_tracking_postable(_doc("A"), [_lt(None, "B1")], {"A": _BATCH})

	def test_batch_item_with_mode_none_is_a_flag_mismatch(self):
		with pytest.raises(bp.TrackedLineRejected, match="trackingMode is 'none'"):
			bp.assert_tracking_postable(_doc("A"), [_lt("none")], {"A": _BATCH})

	def test_mode_batch_on_an_untracked_item_is_a_flag_mismatch(self):
		with pytest.raises(bp.TrackedLineRejected, match="not batch-tracked"):
			bp.assert_tracking_postable(_doc("A"), [_lt("batch", "B1")], {"A": _PLAIN})

	def test_a_ref_on_an_untracked_item_is_refused(self):
		with pytest.raises(bp.TrackedLineRejected, match="erpnextBatchRef 'B1' on an Item"):
			bp.assert_tracking_postable(_doc("A"), [_lt("none", "B1")], {"A": _PLAIN})

	def test_a_ref_without_mode_on_an_untracked_item_is_refused(self):
		with pytest.raises(bp.TrackedLineRejected, match="erpnextBatchRef"):
			bp.assert_tracking_postable(_doc("A"), [_lt(None, "B1")], {"A": _PLAIN})

	@pytest.mark.parametrize("flags", [_PLAIN, _BATCH])
	def test_mode_serial_is_refused(self, flags):
		with pytest.raises(bp.TrackedLineRejected, match="serial"):
			bp.assert_tracking_postable(_doc("A"), [_lt("serial")], {"A": flags})

	@pytest.mark.parametrize("line", [_lt(), _lt("serial"), _lt("batch", "B1")])
	def test_a_serial_item_stays_fail_closed(self, line):
		with pytest.raises(UnsupportedTrackedItem, match="serial-tracked Item"):
			bp.assert_tracking_postable(_doc("A"), [line], {"A": _SERIAL})

	def test_a_serial_and_batch_item_stays_fail_closed(self):
		with pytest.raises(UnsupportedTrackedItem):
			bp.assert_tracking_postable(_doc("A"), [_lt("batch", "B1")], {"A": {"has_batch_no": 1, "has_serial_no": 1}})

	def test_two_lines_of_one_item_are_checked_per_row(self):
		# Two lines of one batch Item: the second has no ref, so the sale fails closed.
		with pytest.raises(bp.TrackedLineRejected, match="row 2"):
			bp.assert_tracking_postable(_doc("A", "A"), [_lt("batch", "B1"), _lt("batch")], {"A": _BATCH})

	def test_an_item_absent_from_the_flags_read_is_left_to_erpnext(self):
		bp.assert_tracking_postable(_doc("A"), [_lt()], {})

	def test_a_document_that_moves_no_stock_is_not_checked(self):
		# A refund credit note (update_stock=0) involves no Serial and Batch Bundle.
		bp.assert_tracking_postable(_doc("A", update_stock=0), [_lt()], {"A": _SERIAL})

	def test_a_row_count_that_differs_from_the_feed_fails_closed(self):
		with pytest.raises(bp.TrackedLineRejected, match="2 row"):
			bp.assert_tracking_postable(_doc("A", "B"), [_lt()], {"A": _PLAIN, "B": _PLAIN})


class TestBatchMaster:
	def test_a_batch_of_the_row_item_passes(self):
		bp.assert_batches_belong(_doc("A", batch_no="B1"), {"B1": {"item": "A"}})

	def test_an_unknown_batch_is_refused(self):
		with pytest.raises(bp.TrackedLineRejected, match="does not exist"):
			bp.assert_batches_belong(_doc("A", batch_no="B1"), {})

	def test_a_batch_of_another_item_is_refused(self):
		with pytest.raises(bp.TrackedLineRejected, match="belongs to Item 'Z'"):
			bp.assert_batches_belong(_doc("A", batch_no="B1"), {"B1": {"item": "Z"}})

	def test_rows_without_a_batch_are_not_checked(self):
		bp.assert_batches_belong(_doc("A"), {})


class TestSaleIntents:
	def test_each_batch_row_intends_its_own_batch(self):
		doc = {"update_stock": 1, "items": [
			{"item_code": "A", "batch_no": "B1"}, {"item_code": "P"}, {"item_code": "A", "batch_no": "B2"}
		]}
		assert bp.sale_batch_intents(doc, {"A": _BATCH, "P": _PLAIN}) == {1: "B1", 3: "B2"}

	def test_an_untracked_sale_has_no_intent(self):
		assert bp.sale_batch_intents(_doc("P"), {"P": _PLAIN}) == {}

	def test_a_sale_that_moves_no_stock_has_no_intent(self):
		assert bp.sale_batch_intents(_doc("A", update_stock=0, batch_no="B1"), {"A": _BATCH}) == {}


def _credit(*rows, update_stock=1):
	return {"update_stock": update_stock, "is_return": 1, "items": list(rows)}


class TestReturnIntents:
	"""G7-10: the credit note row carries no Batch; ERPNext rebuilds the original row's."""

	def test_a_batch_row_intends_the_original_rows_batch(self):
		doc = _credit({"item_code": "A", "sales_invoice_item": "row-a"}, {"item_code": "P", "sales_invoice_item": "row-p"})
		lines = [_lt("batch", "B1"), _lt("none")]
		intents = bp.return_batch_intents(doc, lines, {"A": _BATCH, "P": _PLAIN}, {"row-a": ["B1"]})
		assert intents == {1: "B1"}

	def test_a_feed_line_without_a_ref_still_takes_the_original_batch(self):
		doc = _credit({"item_code": "A", "sales_invoice_item": "row-a"})
		assert bp.return_batch_intents(doc, [_lt("batch")], {"A": _BATCH}, {"row-a": ["B1"]}) == {1: "B1"}

	def test_an_original_without_a_bundle_fails_closed(self):
		doc = _credit({"item_code": "A", "sales_invoice_item": "row-a"})
		with pytest.raises(ReturnLineMismatch, match="no Serial and Batch Bundle"):
			bp.return_batch_intents(doc, [_lt("batch", "B1")], {"A": _BATCH}, {"row-a": None})

	def test_an_original_row_that_was_not_read_fails_closed(self):
		doc = _credit({"item_code": "A", "sales_invoice_item": "row-a"})
		with pytest.raises(ReturnLineMismatch, match="no Serial and Batch Bundle"):
			bp.return_batch_intents(doc, [_lt("batch", "B1")], {"A": _BATCH}, {})

	def test_an_original_with_several_batches_fails_closed(self):
		doc = _credit({"item_code": "A", "sales_invoice_item": "row-a"})
		with pytest.raises(ReturnLineMismatch, match="several Batches"):
			bp.return_batch_intents(doc, [_lt("batch", "B1")], {"A": _BATCH}, {"row-a": ["B1", "B2"]})

	def test_a_feed_batch_that_differs_from_the_original_fails_closed(self):
		doc = _credit({"item_code": "A", "sales_invoice_item": "row-a"})
		with pytest.raises(ReturnLineMismatch, match="'B9' != the original row's Batch 'B1'"):
			bp.return_batch_intents(doc, [_lt("batch", "B9")], {"A": _BATCH}, {"row-a": ["B1"]})

	def test_an_unlinked_batch_row_fails_closed(self):
		doc = _credit({"item_code": "A"})
		with pytest.raises(ReturnLineMismatch, match="not linked"):
			bp.return_batch_intents(doc, [_lt("batch", "B1")], {"A": _BATCH}, {})

	@pytest.mark.parametrize("own", [{"batch_no": "B1"}, {"use_serial_batch_fields": 1}])
	def test_a_credit_note_row_with_a_batch_of_its_own_fails_closed(self, own):
		doc = _credit({"item_code": "A", "sales_invoice_item": "row-a", **own})
		with pytest.raises(ReturnLineMismatch, match="of its own"):
			bp.return_batch_intents(doc, [_lt("batch", "B1")], {"A": _BATCH}, {"row-a": ["B1"]})

	def test_a_credit_note_that_moves_no_stock_has_no_intent(self):
		doc = _credit({"item_code": "A", "sales_invoice_item": "row-a"}, update_stock=0)
		assert bp.return_batch_intents(doc, [_lt("batch", "B1")], {"A": _BATCH}, {}) == {}


def _rows(stock_qty=-2.0, bundle="SABB-1"):
	return [{"name": "row-1", "idx": 1, "stock_qty": stock_qty, "serial_and_batch_bundle": bundle}]


def _ledger(actual_qty=-2.0, bundle="SABB-1", row="row-1"):
	return [{"voucher_detail_no": row, "actual_qty": actual_qty, "serial_and_batch_bundle": bundle}]


def _entries(batch="B1", qty=-2.0, bundle="SABB-1"):
	return [{"parent": bundle, "batch_no": batch, "qty": qty}]


def _verify(*, inward=False, rows=None, ledger=None, entries=None, intents=None):
	bp.verify_batch_postings(
		{1: "B1"} if intents is None else intents,
		inward=inward,
		rows=_rows() if rows is None else rows,
		ledger=_ledger() if ledger is None else ledger,
		entries=_entries() if entries is None else entries,
	)


class TestPostWriteVerification:
	"""891 / 892: the SLE and bundle Batch and quantity must equal the intent."""

	def test_a_sale_of_two_on_its_batch_verifies(self):
		_verify()

	def test_a_return_of_one_back_onto_its_batch_verifies(self):
		_verify(inward=True, rows=_rows(-1.0), ledger=_ledger(1.0), entries=_entries(qty=1.0))

	def test_no_intent_reads_nothing(self):
		_verify(intents={}, rows=[], ledger=[], entries=[])

	def test_another_batch_is_a_mismatch(self):
		with pytest.raises(bp.BatchPostingMismatch, match="posted Batch 'B2', expected 'B1'"):
			_verify(entries=_entries(batch="B2"))

	def test_a_split_across_batches_is_a_mismatch(self):
		entries = _entries(qty=-1.0) + _entries(batch="B2", qty=-1.0)
		with pytest.raises(bp.BatchPostingMismatch, match="2 Batch entries"):
			_verify(entries=entries)

	def test_a_bundle_quantity_other_than_the_row_is_a_mismatch(self):
		with pytest.raises(bp.BatchPostingMismatch, match=r"moved 1.0 of Batch"):
			_verify(entries=_entries(qty=-1.0))

	def test_a_row_without_a_bundle_is_a_mismatch(self):
		with pytest.raises(bp.BatchPostingMismatch, match="no Serial and Batch Bundle"):
			_verify(rows=_rows(bundle=None))

	def test_a_missing_ledger_entry_is_a_mismatch(self):
		with pytest.raises(bp.BatchPostingMismatch, match="0 Stock Ledger Entries"):
			_verify(ledger=[])

	def test_two_ledger_entries_for_one_row_are_a_mismatch(self):
		with pytest.raises(bp.BatchPostingMismatch, match="2 Stock Ledger Entries"):
			_verify(ledger=_ledger() + _ledger())

	def test_a_ledger_quantity_other_than_the_row_is_a_mismatch(self):
		with pytest.raises(bp.BatchPostingMismatch, match=r"moved -1.0, expected -2"):
			_verify(ledger=_ledger(-1.0))

	def test_a_sale_that_moved_stock_in_is_a_mismatch(self):
		with pytest.raises(bp.BatchPostingMismatch, match="expected -2"):
			_verify(ledger=_ledger(2.0))

	def test_a_ledger_entry_through_another_bundle_is_a_mismatch(self):
		with pytest.raises(bp.BatchPostingMismatch, match="does not use the row's bundle"):
			_verify(ledger=_ledger(bundle="SABB-9"))

	def test_a_missing_row_is_a_mismatch(self):
		with pytest.raises(bp.BatchPostingMismatch, match="0 submitted invoice row"):
			_verify(rows=[])


def _line(ref, item, **tracking):
	return {"lineRef": ref, "lineName": item, "unitPrice": "10.00", "currencyCode": "EGP", "quantity": "2",
		"lineAmount": "20.00", "unit": "each", "erpnextItemRef": {"doctype": "Item", "name": item}, **tracking}


def _work_item(kind="sale_post", reversal=None):
	return c.PostingWorkItem.from_wire({
		"workItemRef": "11111111-1111-4111-8111-111111111111", "kind": kind, "sourceSystem": "pos-pulse",
		"externalId": "POS-1", "payloadHash": "a" * 64, "businessDate": "2026-06-01", "itemCursor": "c",
		**({"reversalOf": reversal} if reversal else {}),
		"sale": {"saleRef": "22222222-2222-4222-8222-222222222222", "storeId": "s", "currencyCode": "EGP",
			"posTotal": "40.00", "occurredAt": "2026-06-01T10:00:00Z", "businessDate": "2026-06-01",
			"sourceSystem": "pos-pulse", "externalId": "POS-1",
			"lines": [
				_line(_A, "A", trackingMode="batch", erpnextBatchRef={"doctype": "Batch", "name": "B1"}),
				_line(_B, "P", trackingMode="none", erpnextBatchRef=None),
			]},
	})


class TestLineTracking:
	def test_a_sale_has_one_intent_per_sale_line(self):
		assert bp.line_tracking(_work_item()) == (_lt("batch", "B1"), _lt("none"))

	def test_a_return_has_one_intent_per_returned_line_in_return_order(self):
		reversal = {"sourceSystem": "pos-pulse", "externalId": "POS-1", "reversalKind": "return",
			"recordedAt": "2026-06-02T10:00:00Z", "businessDate": "2026-06-02",
			"returnLines": [{"lineRef": _B, "quantity": "1", "lineAmount": "10.00"},
				{"lineRef": _A, "quantity": "1", "lineAmount": "10.00"}],
			"refundTenders": [{"method": "cash", "amount": "20.00"}]}
		assert bp.line_tracking(_work_item("reversal", reversal)) == (_lt("none"), _lt("batch", "B1"))
