# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt
"""RT-331 — 012 posting-feed 1.6.0-draft: the frozen resolution on the wire (Backend-Core RT-332).

The work item carries ``resolutionVersion`` and ``sale.warehouseRef`` (both optional, omitted for an
intent created before Backend-Core froze it). The connector echoes ``resolutionVersion`` on the ack,
and reports an existing ERP document that does not match the frozen resolution with the
``reconciliation_required`` outcome (the existing ``documentRef`` + a ``reason``).
"""

import pytest

from retail_tower_erpnext_connector.connector.posting import contracts as c

_DOC = c.ErpnextDocumentRef(doctype="Sales Invoice", name="ACC-SINV-2026-00042")
_REASON = c.RejectionReason(category="validation", message="item differs from the frozen resolution")


def _wire(**extra):
	sale = {
		"saleRef": "22222222-2222-4222-8222-222222222222",
		"storeId": "33333333-3333-4333-8333-333333333333",
		"currencyCode": "EGP",
		"posTotal": "100.00",
		"occurredAt": "2026-06-01T10:00:00Z",
		"businessDate": "2026-06-01",
		"sourceSystem": "pos-pulse",
		"externalId": "POS-1",
		"lines": [
			{
				"lineRef": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
				"lineName": "Item A",
				"unitPrice": "100.00",
				"currencyCode": "EGP",
				"quantity": "1",
				"lineAmount": "100.00",
				"unit": "each",
				"erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"},
			}
		],
	}
	sale.update(extra.pop("sale_extra", {}))
	wire = {
		"workItemRef": "11111111-1111-4111-8111-111111111111",
		"kind": "sale_post",
		"sourceSystem": "pos-pulse",
		"externalId": "POS-1",
		"payloadHash": "a" * 64,
		"businessDate": "2026-06-01",
		"itemCursor": "7",
		"sale": sale,
	}
	wire.update(extra)
	return wire


class TestWorkItemFrozenResolution:
	def test_parses_resolution_version_and_warehouse_ref(self):
		item = c.PostingWorkItem.from_wire(
			_wire(resolutionVersion=2, sale_extra={"warehouseRef": {"doctype": "Warehouse", "name": "WH-F"}})
		)
		assert item.resolution_version == 2
		assert item.sale.warehouse_ref == {"doctype": "Warehouse", "name": "WH-F"}

	def test_both_are_optional(self):
		item = c.PostingWorkItem.from_wire(_wire())
		assert item.resolution_version is None
		assert item.sale.warehouse_ref is None

	@pytest.mark.parametrize("bad", [0, -1, "2", 1.5, True])
	def test_rejects_a_non_positive_or_non_integer_version(self, bad):
		with pytest.raises(ValueError, match=r"resolutionVersion must be a positive integer"):
			c.PostingWorkItem.from_wire(_wire(resolutionVersion=bad))

	@pytest.mark.parametrize(
		"bad",
		[{"doctype": "Item", "name": "WH"}, {"doctype": "Warehouse", "name": ""}, {"doctype": "Warehouse"}],
	)
	def test_rejects_a_malformed_warehouse_ref(self, bad):
		with pytest.raises(
			ValueError, match=r"Sale\.warehouseRef(\.name)? must be a (Warehouse reference|non-empty string)"
		):
			c.PostingWorkItem.from_wire(_wire(sale_extra={"warehouseRef": bad}))

	def test_rejects_an_explicit_null_version(self):
		# Omission is the legacy (pre-freeze) shape; an explicit null is malformed and must not
		# silently disable the replay verification (PR #60 review).
		with pytest.raises(ValueError, match=r"resolutionVersion must be a positive integer, got None"):
			c.PostingWorkItem.from_wire(_wire(resolutionVersion=None))

	def test_rejects_an_explicit_null_warehouse_ref(self):
		# An explicit null must not fall back to the mutable local warehouse map (PR #60 review).
		with pytest.raises(ValueError, match=r"Sale\.warehouseRef must be a Warehouse reference, got None"):
			c.PostingWorkItem.from_wire(_wire(resolutionVersion=2, sale_extra={"warehouseRef": None}))


class TestAckFrozenResolution:
	def test_posted_echoes_the_resolution_version(self):
		wire = c.OutcomeAckRequest.posted(_DOC, resolution_version=3).to_wire()
		assert wire["resolutionVersion"] == 3

	def test_posted_without_a_version_sends_none(self):
		assert "resolutionVersion" not in c.OutcomeAckRequest.posted(_DOC).to_wire()

	def test_reconciliation_required_carries_the_existing_document_and_a_reason(self):
		wire = c.OutcomeAckRequest.reconciliation_required(_DOC, _REASON, resolution_version=3).to_wire()
		assert wire == {
			"outcome": "reconciliation_required",
			"documentRef": {"doctype": "Sales Invoice", "name": "ACC-SINV-2026-00042"},
			"reason": {"category": "validation", "message": "item differs from the frozen resolution"},
			"resolutionVersion": 3,
		}

	@pytest.mark.parametrize(
		"kwargs", [{"document_ref": None, "reason": _REASON}, {"document_ref": _DOC, "reason": None}]
	)
	def test_reconciliation_required_needs_both(self, kwargs):
		with pytest.raises(ValueError):
			c.OutcomeAckRequest(outcome="reconciliation_required", **kwargs)
