# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt
"""RT-1429 — a refused credential at the ack never loses or duplicates a sale (no bench).

When Backend-Core refuses the Connector credential (401/403), ``PostingFeedClient.ack_outcome`` raises
``AuthRefused``. The glue must let it propagate (a broad ``except Exception`` would turn it into a terminal
outcome), and the sale's ERPNext effects must already be durable, because ``_commit_before_ack`` (RT-458)
commits BEFORE a ``posted`` ack. The page then aborts without the cursor advancing, and Backend-Core
re-offers the item: the replay guard or the ``unique_rt_si_provenance`` recovery resolves it to the one
invoice that exists.

The harness is the RT-1351 one (``test_concurrency_conflict_glue``).
"""

import pytest

from retail_tower_erpnext_connector.connector.auth_circuit import AuthRefused
from retail_tower_erpnext_connector.connector.posting.tender import TenderModeMap
from retail_tower_erpnext_connector.connector.posting.uom import (
	PreResolvedWarehouse,
	StoreCustomerMap,
	UomMap,
)

from . import test_concurrency_conflict_glue as harness

load_glue = harness.load_glue  # the harness's fixture, re-exported by name


class _RefusingClient(harness._Client):
	"""Backend-Core refuses the credential: every ack raises, and nothing is recorded as acked."""

	def ack_outcome(self, ref, request, idempotency_key):
		self.world.timeline.append("ack_refused")
		raise AuthRefused(401, reported=True)


def _post(glue, world, cursor, client):
	return glue.post_work_item(
		harness._work_item(cursor),
		client=client,
		store=harness._Store(world),
		uom_map=UomMap({"each": "Nos"}),
		warehouses=PreResolvedWarehouse({harness._STORE: {"doctype": "Warehouse", "name": "Map WH"}}),
		customers=StoreCustomerMap({harness._STORE: "Walk-in"}),
		tenders=TenderModeMap({"cash": "Cash"}),
		correlation_id="rt1429-test",
	)


class TestARefusedPostedAck:
	def test_it_propagates_and_the_invoice_is_already_committed_and_recorded(self, load_glue):
		world = harness._World()
		glue = load_glue(harness._Frappe(world))

		with pytest.raises(AuthRefused):
			_post(glue, world, 7, _RefusingClient(world))

		timeline = world.timeline
		assert world.records == [harness.c.ErpnextDocumentRef(doctype="Sales Invoice", name=harness._FRESH)]
		assert timeline.count("submit") == 1
		assert timeline.index("record_posted") < timeline.index("commit") < timeline.index("ack_refused")
		assert timeline[-1] == "ack_refused"  # nothing after the refusal: not turned into an outcome
		assert "rollback" not in timeline  # the committed invoice was not undone
		assert world.acks == []  # Backend-Core was told nothing, so it re-offers the item

	def test_the_re_offer_after_the_fix_acks_posted_once_without_a_second_submit(self, load_glue):
		world = harness._World()
		glue = load_glue(harness._Frappe(world))

		with pytest.raises(AuthRefused):
			_post(glue, world, 7, _RefusingClient(world))
		# The operator saved a new token. Backend-Core re-offers the item; the invoice from offer 1 exists
		# under the sale's provenance, so the unique index refuses a second insert and the recovery echoes it.
		world.insert_error, world.provenance_hit = "duplicate", True
		outcome = _post(glue, world, 9, harness._Client(world))

		assert outcome == "posted"
		assert world.timeline.count("submit") == 1  # never a second submit across both offers
		assert [a["wire"]["outcome"] for a in world.acks] == ["posted"]  # exactly one posted ack


class TestARefusedRejectionOrTransientAck:
	def test_a_refused_rejection_ack_propagates_and_commits_nothing(self, load_glue):
		world = harness._World()
		fake = harness._Frappe(world)
		world.submit_error = fake.ValidationError("Item ITEM-A is disabled")
		glue = load_glue(fake)

		with pytest.raises(AuthRefused):
			_post(glue, world, 7, _RefusingClient(world))

		assert (
			"commit" not in world.timeline and world.records == []
		)  # "rejected" still means "nothing posted"
		assert world.acks == []

	def test_a_refused_failed_transient_ack_propagates(self, load_glue):
		world = harness._World()
		fake = harness._Frappe(world)
		world.submit_error = harness._conflict(fake)
		glue = load_glue(fake)

		with pytest.raises(AuthRefused):
			_post(glue, world, 7, _RefusingClient(world))

		assert "commit" not in world.timeline and world.records == []
		assert world.acks == []
