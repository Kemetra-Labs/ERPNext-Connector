# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1514 — the read-models of the two shipment-transfer work-item kinds (posting-feed 1.17.0-draft).

The module under test imports no frappe. These pin the parse rules of ``transfer_dispatch`` and
``transfer_receive`` (flat, strict, one body per kind), the provenance key of each leg, and that the
heartbeat keeps advertising only the kinds Backend-Core's closed enum accepts until RT-1513.
"""

from __future__ import annotations

import copy

import pytest

from retail_tower_erpnext_connector.connector.health.heartbeat import advertised_work_item_kinds
from retail_tower_erpnext_connector.connector.posting import contracts as c
from retail_tower_erpnext_connector.connector.posting.idempotency import key_for, provenance_id

TRANSFER = "00000000-0000-7000-8000-00000000d0e1"
RECEIPT = "00000000-0000-7000-8000-00000000d2e1"
LINE1 = "00000000-0000-7000-8000-00000000d1e1"
LINE2 = "00000000-0000-7000-8000-00000000d1e2"


def dispatch_wire() -> dict:
	return {
		"workItemRef": "00000000-0000-7000-8000-00000000d001",
		"kind": "transfer_dispatch",
		"sourceSystem": "retail-tower",
		"externalId": f"transfer_dispatch:{TRANSFER}",
		"payloadHash": "a" * 64,
		"businessDate": "2026-11-20",
		"itemCursor": "c1",
		"transfer": {
			"transferRef": TRANSFER,
			"sourceStoreId": "00000000-0000-7000-8000-00000000570a",
			"destinationStoreId": "00000000-0000-7000-8000-00000000570b",
			"sourceWarehouseRef": {"doctype": "Warehouse", "name": "Store A - RT"},
			"transitWarehouseRef": {"doctype": "Warehouse", "name": "Goods In Transit - RT"},
			"postingDatetime": "2026-11-20T09:30:00.000000Z",
			"lines": [
				{
					"lineSeq": 1,
					"transferLineRef": LINE1,
					"erpnextItemRef": {"doctype": "Item", "name": "PANADOL-24"},
					"erpnextBatchRef": None,
					"quantity": "10",
					"unit": "Box",
				},
				{
					"lineSeq": 2,
					"transferLineRef": LINE2,
					"erpnextItemRef": {"doctype": "Item", "name": "AMOXIL-12"},
					"erpnextBatchRef": {"doctype": "Batch", "name": "AMX-2611"},
					"quantity": "4.5",
					"unit": "Strip",
				},
			],
		},
	}


def receive_wire() -> dict:
	return {
		"workItemRef": "00000000-0000-7000-8000-00000000d002",
		"kind": "transfer_receive",
		"sourceSystem": "retail-tower",
		"externalId": f"transfer_receipt:{RECEIPT}",
		"payloadHash": "b" * 64,
		"businessDate": "2026-11-21",
		"itemCursor": "c2",
		"receipt": {
			"transferRef": TRANSFER,
			"receiptRef": RECEIPT,
			"destinationStoreId": "00000000-0000-7000-8000-00000000570b",
			"transitWarehouseRef": {"doctype": "Warehouse", "name": "Goods In Transit - RT"},
			"destinationWarehouseRef": {"doctype": "Warehouse", "name": "Store B - RT"},
			"dispatchDocumentRef": {"doctype": "Stock Entry", "name": "MAT-STE-2026-00042"},
			"postingDatetime": "2026-11-21T11:00:00.000000Z",
			"lines": [
				{
					"lineSeq": 1,
					"dispatchLineSeq": 1,
					"transferLineRef": LINE1,
					"erpnextItemRef": {"doctype": "Item", "name": "PANADOL-24"},
					"erpnextBatchRef": None,
					"quantity": "6",
					"unit": "Box",
				}
			],
		},
	}


class TestDispatchParse:
	def test_a_dispatch_item_parses_with_its_body_and_no_sale(self):
		item = c.PostingWorkItem.from_wire(dispatch_wire())
		assert item.kind == "transfer_dispatch"
		assert item.sale is None and item.count is None and item.receipt is None
		body = item.transfer
		assert body is not None
		assert body.transfer_ref == TRANSFER
		assert body.source_warehouse == "Store A - RT"
		assert body.transit_warehouse == "Goods In Transit - RT"
		assert body.posting_datetime == "2026-11-20T09:30:00.000000Z"
		first, second = body.lines
		assert (first.line_seq, first.item_code, first.batch, first.quantity, first.unit) == (
			1,
			"PANADOL-24",
			None,
			"10",
			"Box",
		)
		assert (second.batch, second.quantity, second.transfer_line_ref) == ("AMX-2611", "4.5", LINE2)

	def test_the_dispatch_provenance_key_is_the_external_id(self):
		item = c.PostingWorkItem.from_wire(dispatch_wire())
		assert provenance_id(item) == f"transfer_dispatch:{TRANSFER}"
		assert key_for(item) == ("retail-tower", f"transfer_dispatch:{TRANSFER}")

	def test_the_kinds_are_known_to_the_parser(self):
		assert {"transfer_dispatch", "transfer_receive"} <= c.WORK_ITEM_KINDS

	@pytest.mark.parametrize("field", ["sale", "count", "receipt", "reversalOf"])
	def test_a_dispatch_item_with_a_foreign_body_is_malformed(self, field):
		wire = dispatch_wire()
		wire[field] = {} if field != "reversalOf" else {"x": 1}
		with pytest.raises(ValueError, match="transfer_dispatch"):
			c.PostingWorkItem.from_wire(wire)

	def test_a_dispatch_item_without_its_body_is_malformed(self):
		wire = dispatch_wire()
		del wire["transfer"]
		with pytest.raises(ValueError, match="transfer_dispatch"):
			c.PostingWorkItem.from_wire(wire)

	def test_a_sale_item_may_not_carry_a_transfer_body(self):
		wire = dispatch_wire()
		wire["kind"] = "sale_post"
		wire["sale"] = {"__placeholder": True}
		with pytest.raises(ValueError):
			c.PostingWorkItem.from_wire(wire)

	@pytest.mark.parametrize(
		"mutate, match",
		[
			(lambda t: t.update(extra="x"), "invalid_shape"),
			(lambda t: t["lines"][0].update(valuationRate="1"), "valuation_rate_forbidden"),
			(lambda t: t["lines"][0].update(serialNo="S1"), "invalid_shape"),
			(lambda t: t["lines"][0].update(quantity="0"), "quantity"),
			(lambda t: t["lines"][0].update(quantity="-1"), "quantity"),
			(lambda t: t["lines"][0].update(quantity="1.1234567"), "quantity"),
			(lambda t: t["lines"][0].update(quantity="1\n"), "quantity"),
			(lambda t: t["lines"][0].update(lineSeq=0), "lineSeq"),
			(lambda t: t["lines"][0].update(unit=""), "unit"),
			(lambda t: t["lines"][0].update(transferLineRef=""), "transferLineRef"),
			(
				lambda t: t["lines"][1].update(erpnextBatchRef={"doctype": "Item", "name": "X"}),
				"TransferDispatchLine.erpnextBatchRef",
			),
			(
				lambda t: t["lines"][1].update(erpnextBatchRef={"doctype": "Batch", "name": ""}),
				"TransferDispatchLine.erpnextBatchRef.name",
			),
			(lambda t: t.update(lines=[]), "lines"),
			(lambda t: t.update(lines=t["lines"][:1] * 101), "lines"),
			(
				lambda t: t.update(sourceWarehouseRef={"doctype": "Item", "name": "x"}),
				"TransferDispatchPosting.sourceWarehouseRef",
			),
			(
				lambda t: t.update(transitWarehouseRef={"doctype": "Warehouse", "name": ""}),
				"TransferDispatchPosting.transitWarehouseRef.name",
			),
			(lambda t: t.update(sourceStoreId=None), "TransferDispatchPosting.sourceStoreId"),
			(lambda t: t.update(sourceStoreId=7), "TransferDispatchPosting.sourceStoreId"),
			(lambda t: t.update(destinationStoreId=None), "TransferDispatchPosting.destinationStoreId"),
			(lambda t: t.update(destinationStoreId=""), "TransferDispatchPosting.destinationStoreId"),
			(lambda t: t.update(postingDatetime=""), "postingDatetime"),
		],
	)
	def test_a_malformed_dispatch_body_is_refused(self, mutate, match):
		wire = dispatch_wire()
		mutate(wire["transfer"])
		with pytest.raises(ValueError, match=match):
			c.PostingWorkItem.from_wire(wire)

	def test_a_missing_required_line_field_is_refused(self):
		for field in ["lineSeq", "transferLineRef", "erpnextItemRef", "erpnextBatchRef", "quantity", "unit"]:
			wire = dispatch_wire()
			del wire["transfer"]["lines"][0][field]
			with pytest.raises((ValueError, KeyError)):
				c.PostingWorkItem.from_wire(wire)


class TestReceiveParse:
	def test_a_receive_item_parses_with_its_body(self):
		item = c.PostingWorkItem.from_wire(receive_wire())
		assert item.kind == "transfer_receive"
		assert item.sale is None and item.count is None and item.transfer is None
		body = item.receipt
		assert body is not None
		assert (body.transfer_ref, body.receipt_ref) == (TRANSFER, RECEIPT)
		assert body.destination_warehouse == "Store B - RT"
		assert body.dispatch_document == "MAT-STE-2026-00042"
		line = body.lines[0]
		assert (line.line_seq, line.dispatch_line_seq, line.quantity) == (1, 1, "6")

	def test_the_receipt_provenance_key_is_the_external_id(self):
		item = c.PostingWorkItem.from_wire(receive_wire())
		assert key_for(item) == ("retail-tower", f"transfer_receipt:{RECEIPT}")

	@pytest.mark.parametrize("field", ["sale", "count", "transfer"])
	def test_a_receive_item_with_a_foreign_body_is_malformed(self, field):
		wire = receive_wire()
		wire[field] = {}
		with pytest.raises(ValueError, match="transfer_receive"):
			c.PostingWorkItem.from_wire(wire)

	@pytest.mark.parametrize(
		"mutate, match",
		[
			(
				lambda r: r.update(dispatchDocumentRef={"doctype": "Sales Invoice", "name": "X"}),
				"Stock Entry",
			),
			(lambda r: r.update(dispatchDocumentRef={"doctype": "Stock Entry", "name": ""}), "Stock Entry"),
			(lambda r: r["lines"][0].update(dispatchLineSeq=0), "dispatchLineSeq"),
			(lambda r: r["lines"][0].pop("dispatchLineSeq"), "dispatchLineSeq"),
			(lambda r: r.update(unknown=1), "invalid_shape"),
			(lambda r: r.update(destinationStoreId=None), "TransferReceivePosting.destinationStoreId"),
			(lambda r: r.update(destinationStoreId=7), "TransferReceivePosting.destinationStoreId"),
			(
				lambda r: r.update(transitWarehouseRef={"doctype": "Item", "name": "x"}),
				"TransferReceivePosting.transitWarehouseRef",
			),
			(
				lambda r: r.update(destinationWarehouseRef={"doctype": "Warehouse", "name": ""}),
				"TransferReceivePosting.destinationWarehouseRef.name",
			),
			(
				lambda r: r["lines"][0].update(erpnextBatchRef={"doctype": "Item", "name": "X"}),
				"TransferReceiveLine.erpnextBatchRef",
			),
		],
	)
	def test_a_malformed_receipt_body_is_refused(self, mutate, match):
		wire = receive_wire()
		mutate(wire["receipt"])
		with pytest.raises((ValueError, KeyError), match=match):
			c.PostingWorkItem.from_wire(wire)

	def test_parsing_does_not_mutate_its_input(self):
		wire = receive_wire()
		before = copy.deepcopy(wire)
		c.PostingWorkItem.from_wire(wire)
		assert wire == before


class TestHeartbeatAdvertisement:
	def test_the_heartbeat_keeps_advertising_only_the_kinds_backend_core_knows(self):
		assert advertised_work_item_kinds() == ("inventory_count", "reversal", "sale_post")

	def test_the_advertised_kinds_are_a_subset_of_the_accepted_kinds(self):
		assert set(advertised_work_item_kinds()) < c.WORK_ITEM_KINDS
