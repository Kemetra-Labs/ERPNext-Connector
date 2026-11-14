# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1340 — every golden return Backend-Core emits (RT-1367 vectors) posts a credit note equal to its refund.

``od11-golden-returns.json`` is a Connector-owned, hand-copied subset of the Backend-Core golden vectors
(never read from Backend-Core at test time: MR boundary). Each case is built through the real return
builder: the credit note rows and the refund payment must both total exactly ``-refund``, so the per-line
clawback and give-back the feed carries reconcile with what the customer was paid.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from retail_tower_erpnext_connector.connector.posting import contracts as c
from retail_tower_erpnext_connector.connector.posting import return_builder as rtb
from retail_tower_erpnext_connector.connector.posting import tender as t

_CASES = json.loads((Path(__file__).parent / "od11-golden-returns.json").read_text(encoding="utf-8"))["cases"]
_STORE = "33333333-3333-4333-8333-333333333333"


def _work_item(case):
	lines = [
		{
			"lineRef": line["saleLineRef"],
			"lineName": f"ITEM-{i}",
			"unitPrice": line["unitPrice"],
			"currencyCode": case["currencyCode"],
			"quantity": line["quantity"],
			"lineAmount": line["lineAmount"],
			"taxAmount": None,
			"unit": "each",
			"erpnextItemRef": {"doctype": "Item", "name": f"ITEM-{i}"},
		}
		for i, line in enumerate(case["saleLines"], start=1)
	]
	return c.PostingWorkItem.from_wire(
		{
			"workItemRef": "77777777-7777-4777-8777-777777777777",
			"kind": "reversal",
			"sourceSystem": "pos-pulse",
			"externalId": "POS-OD11",
			"payloadHash": "f" * 64,
			"businessDate": "2026-06-01",
			"itemCursor": "c-od11",
			"reversalOf": {
				"sourceSystem": "pos-pulse",
				"externalId": "POS-OD11",
				"reversalKind": "return",
				"recordedAt": "2026-06-05T09:30:00Z",
				"businessDate": "2026-06-05",
				"returnLines": [{**line, "taxAmount": None} for line in case["returnLines"]],
				"refundTenders": [{"method": "cash", "amount": case["refund"]}],
			},
			"sale": {
				"saleRef": "22222222-2222-4222-8222-222222222222",
				"storeId": _STORE,
				"currencyCode": case["currencyCode"],
				"posTotal": f"{sum(Decimal(line['lineAmount']) for line in case['saleLines']):f}",
				"occurredAt": "2026-06-01T10:00:00Z",
				"businessDate": "2026-06-01",
				"sourceSystem": "pos-pulse",
				"externalId": "POS-OD11",
				"lines": lines,
			},
		}
	)


def _build(work_item):
	resolvers = rtb.ReturnResolvers(
		uom_for=lambda unit: "Nos",
		warehouse_for=lambda store: {"doctype": "Warehouse", "name": "Main - RT"},
		customer_for=lambda store: "Walk-in Customer - RT",
		mode_of_payment_for=t.TenderModeMap({"cash": "Cash"}).resolve,
		give_back_item=lambda: rtb.GiveBackItem(item_code="RT-PROMO-GIVEBACK", uom="Nos"),
	)
	return rtb.build_return_invoice(work_item, resolvers)


def test_the_fixture_covers_a_clawback_and_a_give_back():
	amounts = [line for case in _CASES for line in case["returnLines"]]
	assert any("promotionClawbackAmount" in line for line in amounts)
	assert any("promotionGiveBackAmount" in line for line in amounts)


@pytest.mark.parametrize("case", _CASES, ids=[case["id"] for case in _CASES])
def test_the_credit_note_equals_the_refund(case):
	doc = _build(_work_item(case))
	refund = Decimal(case["refund"])
	assert sum(Decimal(item["amount"]) for item in doc["items"]) == -refund
	assert [Decimal(p["amount"]) for p in doc["payments"]] == [-refund]
	assert rtb.expected_row_counts(_work_item(case)).total() == len(doc["items"])
