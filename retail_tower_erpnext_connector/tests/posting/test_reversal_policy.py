# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-71 — contain amount-only refunds (RT-14 F1, owner decision D8). No frappe.

Backend-Core never projects the refund amount into the posting feed, so a ``reversalKind=refund``
work-item carries EVERY sale line at full value and the reversal builder would negate them all:
any refund, even a partial one, posted a credit note for the whole sale (RT-48 bench:
ACC-SINV-2026-00028 = -24.00 against a 24.00 sale). Until line-aware returns land (RT-14 → RT-16),
the connector must reject refunds instead of posting them.
"""


import pytest

from retail_tower_erpnext_connector.connector.posting import contracts as c
from retail_tower_erpnext_connector.connector.posting import reversal_policy as rp


def _work_item(kind="reversal", reversal_kind="refund") -> c.PostingWorkItem:
	wire = {
		"workItemRef": "44444444-4444-4444-8444-444444444444",
		"kind": kind,
		"sourceSystem": "pos-pulse",
		"externalId": "POS-9001",
		"payloadHash": "b" * 64,
		"businessDate": "2026-06-05",
		"itemCursor": "cursor-2",
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
	if kind == "reversal":
		wire["reversalOf"] = {"sourceSystem": "pos-pulse", "externalId": "POS-9001", "reversalKind": reversal_kind}
	return c.PostingWorkItem.from_wire(wire)


class TestRefundContainment:
	def test_refund_is_unsupported(self):
		with pytest.raises(rp.UnsupportedReversal) as exc:
			rp.assert_reversal_supported(_work_item(reversal_kind="refund"))
		# The reason names the gap and the follow-up, so an operator knows it is by design.
		message = str(exc.value)
		assert "refund" in message
		assert "RT-14" in message

	def test_void_is_supported(self):
		rp.assert_reversal_supported(_work_item(reversal_kind="void"))

	def test_sale_post_is_not_a_reversal_and_passes(self):
		rp.assert_reversal_supported(_work_item(kind="sale_post"))

	def test_reason_fits_the_ack_contract(self):
		# DP2 ack contract: reason.message is 1..1000 characters (reasons.REASON_MESSAGE_MAX).
		with pytest.raises(rp.UnsupportedReversal) as exc:
			rp.assert_reversal_supported(_work_item())
		assert 1 <= len(str(exc.value)) <= 1000


class TestGlueOrdering:
	"""The glue is frappe-only; pin WHERE it applies the policy (bench evidence proves behaviour)."""

	def _reversal_source(self):
		from pathlib import Path

		path = Path(rp.__file__).with_name("frappe_glue.py")
		source = path.read_text(encoding="utf-8")
		start = source.index("def _post_reversal(")
		end = source.index("\ndef ", start + 1)
		return source[start:end]

	def test_policy_runs_after_the_replay_guard_and_before_any_build(self):
		body = self._reversal_source()
		replay = body.index("store.get_document_ref(key)")
		policy = body.index("assert_reversal_supported(work_item)")
		build = body.index("_build_reversal_or_reject(")  # RT-16: the builder dispatch (RT-468: via helper)
		assert replay < policy < build
		# The validation rejection and the crash-window recovery are exercised in
		# test_refund_containment_glue.py (the real glue against a frappe stand-in).
