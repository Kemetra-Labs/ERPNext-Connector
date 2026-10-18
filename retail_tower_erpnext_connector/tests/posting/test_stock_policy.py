# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-48 — local unit tests for the pure stock-posting policy (no frappe).

Owner decisions (RT-47 comment 10287, RT-48 comment 10291):
  - D4: the technical pilot moves stock only for non-batch / non-serial items. ERPNext would
    otherwise AUTO-PICK a batch (``auto_create_serial_and_batch_bundle_for_outward`` defaults on),
    i.e. guess one, so a batch/serial line on a stock-moving document fails CLOSED.
  - D5: a full void's return invoice mirrors the ORIGINAL invoice's ``update_stock`` (a legacy
    update_stock=0 invoice never fabricates a stock restoration) and references each original line
    via ``sales_invoice_item`` so ERPNext's per-line over-return cap applies.
"""

import pytest

from retail_tower_erpnext_connector.connector.posting import stock_policy as sp


def _doc(*, update_stock=1, is_return=0, items=None):
	return {
		"doctype": "Sales Invoice",
		"update_stock": update_stock,
		"is_return": is_return,
		"items": items
		or [
			{"item_code": "ITEM-A", "qty": "-2" if is_return else "2"},
			{"item_code": "ITEM-B", "qty": "-1.5" if is_return else "1.5"},
		],
	}


_PLAIN = {"has_batch_no": 0, "has_serial_no": 0}


class TestTrackedItemGuard:
	def test_plain_items_pass(self):
		sp.assert_no_tracked_items(_doc(), {"ITEM-A": _PLAIN, "ITEM-B": _PLAIN})

	def test_batch_item_on_stock_moving_doc_fails_closed(self):
		tracking = {"ITEM-A": _PLAIN, "ITEM-B": {"has_batch_no": 1, "has_serial_no": 0}}
		with pytest.raises(sp.UnsupportedTrackedItem) as exc:
			sp.assert_no_tracked_items(_doc(), tracking)
		assert "ITEM-B" in str(exc.value)

	def test_serial_item_on_stock_moving_doc_fails_closed(self):
		tracking = {"ITEM-A": {"has_batch_no": 0, "has_serial_no": 1}, "ITEM-B": _PLAIN}
		with pytest.raises(sp.UnsupportedTrackedItem):
			sp.assert_no_tracked_items(_doc(), tracking)

	def test_non_stock_moving_doc_is_not_checked(self):
		# A refund credit note / legacy-mirrored return posts update_stock=0: no Serial and Batch
		# Bundle is involved, so batch flags are irrelevant there.
		tracking = {"ITEM-A": {"has_batch_no": 1, "has_serial_no": 1}, "ITEM-B": _PLAIN}
		sp.assert_no_tracked_items(_doc(update_stock=0), tracking)

	def test_tracking_item_codes_lists_each_code_once_in_order(self):
		items = [
			{"item_code": "A", "qty": "1"},
			{"item_code": "A", "qty": "2"},
			{"item_code": "B", "qty": "1"},
		]
		assert sp.tracking_item_codes(_doc(items=items)) == ["A", "B"]


def _original_rows():
	# Shaped like frappe.get_all("Sales Invoice Item", fields=[name, item_code, qty, idx]): qty is a
	# FLOAT from the DB, so the policy must compare it as an exact decimal, never float-vs-string.
	return [
		{"name": "row-1", "item_code": "ITEM-A", "qty": 2.0, "idx": 1},
		{"name": "row-2", "item_code": "ITEM-B", "qty": 1.5, "idx": 2},
	]


def _link(
	doc=None,
	*,
	original_update_stock=1,
	original_disable_rounded_total=1,
	original_items=None,
	original_is_pos=0,
	original_payments=(),
):
	return sp.link_void_to_original(
		doc or _doc(is_return=1),
		original_update_stock=original_update_stock,
		original_disable_rounded_total=original_disable_rounded_total,
		original_items=_original_rows() if original_items is None else original_items,
		original_is_pos=original_is_pos,
		original_payments=original_payments,
	)


class TestVoidLinkage:
	def test_mirrors_a_stock_moving_original(self):
		assert _link(original_update_stock=1)["update_stock"] == 1

	def test_legacy_original_never_fabricates_stock_restoration(self):
		assert _link(original_update_stock=0)["update_stock"] == 0

	def test_mirrors_an_unrounded_original(self):
		# RT-80: a sale posted with the fix carries disable_rounded_total=1; its void does too.
		assert _link(original_disable_rounded_total=1)[
			"disable_rounded_total"
		] == 1

	def test_legacy_rounded_original_is_reversed_with_its_own_rounding(self):
		# RT-80 (Codex P2, PR #47): a pre-fix sale posted 10.49 as AR 10.00 + Round Off 0.49. An
		# unrounded -10.49 void would leave a -0.49 customer credit and never reverse the Round Off
		# (rt9 bench T4). Mirroring the original's rounding makes the void net to exactly zero.
		assert _link(original_disable_rounded_total=0)[
			"disable_rounded_total"
		] == 0

	def test_each_line_references_its_original_row_by_position(self):
		doc = _link()
		assert [i["sales_invoice_item"] for i in doc["items"]] == ["row-1", "row-2"]

	def test_original_rows_are_matched_in_idx_order(self):
		doc = _link(original_items=list(reversed(_original_rows())))  # DB rows may arrive unordered
		assert [i["sales_invoice_item"] for i in doc["items"]] == ["row-1", "row-2"]

	def test_does_not_mutate_the_input_doc(self):
		src = _doc(is_return=1)
		_link(src, original_update_stock=0)
		assert src["update_stock"] == 1
		assert "sales_invoice_item" not in src["items"][0]

	def test_line_count_mismatch_fails_closed(self):
		with pytest.raises(sp.ReturnLineMismatch):
			_link(original_items=_original_rows()[:1])

	def test_item_code_mismatch_fails_closed(self):
		rows = _original_rows()
		rows[1] = {**rows[1], "item_code": "ITEM-Z"}
		with pytest.raises(sp.ReturnLineMismatch):
			_link(original_items=rows)

	def test_quantity_mismatch_fails_closed(self):
		# A full void restores EXACTLY the sold quantity — compared as exact decimals.
		rows = _original_rows()
		rows[0] = {**rows[0], "qty": 3.0}
		with pytest.raises(sp.ReturnLineMismatch):
			_link(original_items=rows)

	def test_each_line_restores_stock_to_the_original_rows_warehouse(self):
		# Codex P1 (PR #42): if the store→warehouse map changed between the sale and its void, the
		# builder stamps the CURRENT mapped warehouse. The void must restore stock where the sale
		# took it from, otherwise the depleted warehouse is never credited.
		rows = [
			{**_original_rows()[0], "warehouse": "Old Store - RT"},
			{**_original_rows()[1], "warehouse": "Old Store - RT"},
		]
		doc = _doc(is_return=1)
		for item in doc["items"]:
			item["warehouse"] = "New Store - RT"
		out = _link(doc, original_items=rows)
		assert [i["warehouse"] for i in out["items"]] == ["Old Store - RT", "Old Store - RT"]

	def test_rejects_a_non_return_document(self):
		with pytest.raises(ValueError, match=r"link_void_to_original expects a return \(is_return=1\) document"):
			_link(_doc(is_return=0))


def _paid_void(*payments):
	"""A void return doc built from the reversal's sale.tenders (negated by the reversal builder)."""
	doc = _doc(is_return=1)
	doc["is_pos"] = 1
	doc["payments"] = [{"mode_of_payment": m, "amount": a} for m, a in payments]
	return doc


# Shaped like frappe.get_all("Sales Invoice Payment", fields=[mode_of_payment, amount, idx]): the DB
# amount is a FLOAT, so the policy must compare and copy it as an exact decimal.
_ORIGINAL_PAYMENTS = [
	{"mode_of_payment": "Cash", "amount": 150.0, "idx": 1},
	{"mode_of_payment": "Card Clearing", "amount": 50.0, "idx": 2},
]


class TestVoidSettlementMirror:
	"""RT-78 / RT-10 D6: a void pays back EXACTLY what the original invoice was paid with.

	The payments are rebuilt from the original invoice's own rows (like its warehouse, Codex P1 PR
	#42): a tender map remapped since the sale must not refund through a different Mode of Payment.
	The void's sale.tenders only cross-check it; any disagreement fails closed (validation).
	"""

	def test_void_of_a_paid_original_mirrors_its_payment_rows(self):
		doc = _link(
			_paid_void(("Cash", "-150.00"), ("Card Clearing", "-50.00")),
			original_is_pos=1,
			original_payments=list(reversed(_ORIGINAL_PAYMENTS)),  # DB rows may arrive unordered
		)
		assert doc["is_pos"] == 1
		assert doc["payments"] == [
			{"mode_of_payment": "Cash", "amount": "-150.0"},
			{"mode_of_payment": "Card Clearing", "amount": "-50.0"},
		]

	def test_payment_modes_come_from_the_original_not_the_current_map(self):
		doc = _link(
			_paid_void(("Cash (new)", "-200.00")),
			original_is_pos=1,
			original_payments=[{"mode_of_payment": "Cash", "amount": 200.0, "idx": 1}],
		)
		assert doc["payments"] == [{"mode_of_payment": "Cash", "amount": "-200.0"}]

	def test_tender_unknown_void_of_an_unpaid_original_stays_unsettled(self):
		doc = _link(original_is_pos=0)
		assert "is_pos" not in doc and "payments" not in doc

	def test_paid_void_of_an_unpaid_original_fails_closed(self):
		# Cash would be paid out against a sale ERPNext never recorded as paid.
		with pytest.raises(sp.VoidSettlementMismatch, match="unpaid"):
			_link(_paid_void(("Cash", "-200.00")), original_is_pos=0)

	def test_unpaid_void_of_a_paid_original_fails_closed(self):
		# ERPNext would leave the refund as a customer credit instead of cash out (RT-75 T5).
		with pytest.raises(sp.VoidSettlementMismatch, match="paid"):
			_link(original_is_pos=1, original_payments=_ORIGINAL_PAYMENTS)

	def test_void_tender_total_must_equal_the_original_payments(self):
		with pytest.raises(sp.VoidSettlementMismatch, match="total"):
			_link(_paid_void(("Cash", "-199.00")), original_is_pos=1, original_payments=_ORIGINAL_PAYMENTS)

	def test_a_settlement_mismatch_is_a_return_line_mismatch(self):
		# The glue maps ReturnLineMismatch to validation; the settlement check must ride that path.
		assert issubclass(sp.VoidSettlementMismatch, sp.ReturnLineMismatch)


_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _return_doc(*items):
	"""A partial-return doc as build_return_invoice emits it: negated rows tagged with rt_line_ref."""
	return {
		"doctype": "Sales Invoice",
		"is_return": 1,
		"update_stock": 1,
		"customer": "Customer From Current Map",
		"items": [
			{"item_code": code, "qty": qty, "uom": "Current UOM", "warehouse": "Map WH", "rt_line_ref": ref}
			for ref, code, qty in items
		],
	}


def _original_with_refs():
	# The ORIGINAL invoice's rows only (the glue filters by parent): qty is a DB float.
	return [
		{"name": "row-a", "item_code": "ITEM-A", "qty": 3.0, "idx": 1, "warehouse": "Stores - A", "rt_line_ref": _A,
			"uom": "Box", "conversion_factor": 12.0},
		{"name": "row-b", "item_code": "ITEM-B", "qty": 1.0, "idx": 2, "warehouse": "Stores - B", "rt_line_ref": _B,
			"uom": "Nos", "conversion_factor": 1.0},
	]


def _link_return(doc, *, original_update_stock=1, original_items=None, original_customer="Original Customer"):
	return sp.link_return_to_original(
		doc,
		original_update_stock=original_update_stock,
		original_items=_original_with_refs() if original_items is None else original_items,
		original_customer=original_customer,
	)


class TestReturnLinkage:
	"""RT-16 / RT-14 D6: a partial return links each row to the ORIGINAL row by rt_line_ref.

	Identity, not position (the void rule): a return carries a subset of the lines in any order.
	The original row's warehouse is restored to, ERPNext's per-row over-return cap applies through
	sales_invoice_item, and stock moves only if the sale moved it.
	"""

	def test_links_each_row_to_the_original_row_with_its_line_ref(self):
		doc = _link_return(_return_doc((_B, "ITEM-B", "-1"), (_A, "ITEM-A", "-2")))
		assert [(i["sales_invoice_item"], i["warehouse"]) for i in doc["items"]] == [
			("row-b", "Stores - B"),
			("row-a", "Stores - A"),
		]

	def test_credits_the_original_invoice_customer(self):
		# Codex P2 PR #50: a store->customer map changed since the sale must not redirect the credit.
		assert _link_return(_return_doc((_A, "ITEM-A", "-1")))["customer"] == "Original Customer"

	def test_returns_in_the_original_row_uom(self):
		# Codex P2 PR #50: a unit->UOM map changed since the sale must not change what qty means.
		doc = _link_return(_return_doc((_B, "ITEM-B", "-1"), (_A, "ITEM-A", "-1")))
		assert [i["uom"] for i in doc["items"]] == ["Nos", "Box"]

	def test_returns_with_the_original_row_conversion_factor(self):
		# Codex P2 PR #50 round 3: an Item's UOM factor changed since the sale must not change how
		# many stock units the return restores.
		doc = _link_return(_return_doc((_A, "ITEM-A", "-1")))
		assert (doc["items"][0]["uom"], doc["items"][0]["conversion_factor"]) == ("Box", 12.0)

	def test_mirrors_the_original_update_stock(self):
		assert _link_return(_return_doc((_A, "ITEM-A", "-1")), original_update_stock=0)["update_stock"] == 0
		assert _link_return(_return_doc((_A, "ITEM-A", "-1")), original_update_stock=1)["update_stock"] == 1

	def test_an_original_posted_before_rt16_has_no_line_refs_and_fails_closed(self):
		legacy = [{**row, "rt_line_ref": None} for row in _original_with_refs()]
		with pytest.raises(sp.ReturnLineMismatch, match="rt_line_ref"):
			_link_return(_return_doc((_A, "ITEM-A", "-1")), original_items=legacy)

	def test_a_line_ref_the_original_does_not_carry_fails_closed(self):
		with pytest.raises(sp.ReturnLineMismatch, match="cccccccc"):
			_link_return(_return_doc(("cccccccc-cccc-4ccc-8ccc-cccccccccccc", "ITEM-A", "-1")))

	def test_two_original_rows_with_one_line_ref_fail_closed(self):
		dup = [*_original_with_refs(), {**_original_with_refs()[0], "name": "row-a2", "idx": 3}]
		with pytest.raises(sp.ReturnLineMismatch, match="ambiguous"):
			_link_return(_return_doc((_A, "ITEM-A", "-1")), original_items=dup)

	def test_item_code_must_match_the_original_row(self):
		with pytest.raises(sp.ReturnLineMismatch, match="ITEM-B"):
			_link_return(_return_doc((_A, "ITEM-B", "-1")))

	def test_cannot_return_more_than_the_original_row_sold(self):
		with pytest.raises(sp.ReturnLineMismatch, match="sold"):
			_link_return(_return_doc((_A, "ITEM-A", "-3.5")))

	def test_returning_the_full_sold_quantity_is_allowed(self):
		doc = _link_return(_return_doc((_A, "ITEM-A", "-3")))
		assert doc["items"][0]["sales_invoice_item"] == "row-a"

	def test_does_not_mutate_the_input_doc(self):
		src = _return_doc((_A, "ITEM-A", "-1"))
		_link_return(src)
		assert "sales_invoice_item" not in src["items"][0] and src["items"][0]["warehouse"] == "Map WH"

	def test_rejects_a_non_return_document(self):
		with pytest.raises(ValueError, match=r"link_return_to_original expects a return \(is_return=1\) document"):
			_link_return({**_return_doc((_A, "ITEM-A", "-1")), "is_return": 0})


class TestCompanyMirrorsTheOriginal:
	"""RT-378 finding 2: a credit note must land on the ORIGINAL invoice's company, not the site default."""

	def test_a_credit_note_takes_the_original_company(self):
		doc = {**_return_doc((_A, "ITEM-A", "-1")), "company": "Site Default Co"}
		out = sp.mirror_company(doc, "Original Co")
		assert out["company"] == "Original Co"
		assert doc["company"] == "Site Default Co"  # a copy: the input is not mutated

	@pytest.mark.parametrize("company", [None, ""])
	def test_an_original_without_a_company_fails_closed(self, company):
		with pytest.raises(sp.ReturnLineMismatch, match="company"):
			sp.mirror_company(_return_doc((_A, "ITEM-A", "-1")), company)
