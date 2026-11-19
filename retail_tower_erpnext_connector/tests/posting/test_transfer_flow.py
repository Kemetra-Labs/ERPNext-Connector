# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1514 — the ``transfer_dispatch`` and ``transfer_receive`` legs against a fake ERP port (no frappe, no bench).

Every outcome of both legs is exercised here: the native ERPNext goods-in-transit pair (a Material Transfer
with add-to-transit, then a Material Transfer against it), the whole-document refusals (a Stock Entry is
one document, so one bad line refuses the whole leg and nothing is posted), Draft-aware idempotency,
the ledger read-back, and the receipt rules (linked dispatch row, remaining quantity, over-receipt).
The real-ERPNext behaviour is NOT proven here: that needs the v16 bench (see the PR, NOT_VERIFIED).
"""

from __future__ import annotations

import copy
from decimal import Decimal

import pytest

from retail_tower_erpnext_connector.connector.posting import contracts as c
from retail_tower_erpnext_connector.connector.posting import transfer_flow as tf
from retail_tower_erpnext_connector.connector.posting.idempotency import IdempotencyConflict
from retail_tower_erpnext_connector.connector.posting.uom import UomMap

from .test_transfer_contracts import (
	LINE1,
	LINE2,
	RECEIPT,
	TRANSFER,
	dispatch_wire,
	receive_wire,
)

_UOMS = UomMap({"Box": "Box", "Strip": "Strip", "Nos": "Nos"})
_CAIRO = "Africa/Cairo"
_SRC, _TRANSIT, _DEST = "Store A - RT", "Goods In Transit - RT", "Store B - RT"
_PANADOL, _AMOXIL, _BATCH = "PANADOL-24", "AMOXIL-12", "AMX-2611"
_DISPATCH_DOC = "MAT-STE-2026-00042"


class _Client:
	def __init__(self) -> None:
		self.acks: list[tuple[str, dict, str | None]] = []

	def ack_outcome(self, ref, ack, *, idempotency_key=None):
		self.acks.append((ref, ack.to_wire(), idempotency_key))

	@property
	def last(self) -> dict:
		return self.acks[-1][1]


class _Store:
	def __init__(self) -> None:
		self.recorded: dict = {}
		self.conflict = False

	def get_document_ref(self, key):
		return self.recorded.get(key)

	def record_posted(self, key, ref):
		if self.conflict:
			raise IdempotencyConflict(key, ref, ref)
		self.recorded[key] = ref


def _row(item, batch, qty, s, t):
	return tf.TransferRow(item, batch, qty, s, t)


def _dispatch_rows() -> tuple[tf.TransferRow, ...]:
	return (
		_row(_PANADOL, None, "10", _SRC, _TRANSIT),
		_row(_AMOXIL, _BATCH, "4.5", _SRC, _TRANSIT),
	)


def _dispatch_ledger() -> list[tf.LedgerRow]:
	return [
		tf.LedgerRow(_PANADOL, _SRC, "-10", None),
		tf.LedgerRow(_PANADOL, _TRANSIT, "10", None),
		tf.LedgerRow(_AMOXIL, _SRC, "-4.5", _BATCH),
		tf.LedgerRow(_AMOXIL, _TRANSIT, "4.5", _BATCH),
	]


def _receive_ledger() -> list[tf.LedgerRow]:
	return [tf.LedgerRow(_PANADOL, _TRANSIT, "-6", None), tf.LedgerRow(_PANADOL, _DEST, "6", None)]


def _existing(**kw) -> tf.ExistingTransfer:
	base = dict(
		name="MAT-STE-2026-00099",
		docstatus=1,
		purpose="Material Transfer",
		add_to_transit=True,
		outgoing_stock_entry=None,
		posting_date="2026-11-20",
		posting_time="11:30:00.000000",
		rows=_dispatch_rows(),
	)
	base.update(kw)
	return tf.ExistingTransfer(**base)


def _dispatch_facts(**kw) -> tf.DispatchFacts:
	base = dict(
		name=_DISPATCH_DOC,
		docstatus=1,
		purpose="Material Transfer",
		company="RT Company",
		add_to_transit=True,
		rows=(
			tf.DispatchRow(1, "d-row-1", _PANADOL, None, "10", "0", _TRANSIT, LINE1),
			tf.DispatchRow(2, "d-row-2", _AMOXIL, _BATCH, "4.5", "0", _TRANSIT, LINE2),
		),
	)
	base.update(kw)
	return tf.DispatchFacts(**base)


class _Port:
	"""A scriptable ERP site: items, batches, warehouses, existing documents and the next submit."""

	def __init__(self) -> None:
		self.ready = True
		self.facts = {
			_PANADOL: tf.TransferItemFacts(True, "Box", False, False),
			_AMOXIL: tf.TransferItemFacts(True, "Strip", True, False),
		}
		self.batch_problems: dict[tuple[str, str], str] = {}
		self.warehouse_problems: dict[str, str] = {}
		self.transit_problems: dict[str, str] = {}
		self.docs: list[tf.ExistingTransfer] = []
		self.dispatch: tf.DispatchFacts | None = _dispatch_facts()
		self.submit_error: Exception | None = None
		self.submit_races = False
		self.race_docs: list[tf.ExistingTransfer] | None = None
		self.submitted: list[dict] = []
		self.committed = 0
		self.ledger: list[tf.LedgerRow] | None = None
		self.last_stamp = None

	def indexes_ready(self):
		return self.ready

	def company(self):
		return "RT Company"

	def item_facts(self, codes):
		return {k: v for k, v in self.facts.items() if k in codes}

	def batch_problem(self, item, batch, stamp):
		self.last_stamp = stamp
		return self.batch_problems.get((item, batch))

	def warehouse_problem(self, warehouse, company):
		return self.warehouse_problems.get(warehouse)

	def transit_warehouse_problem(self, warehouse, company):
		return self.transit_problems.get(warehouse)

	def existing_documents(self, source, external):
		return list(self.docs)

	def dispatch_document(self, name):
		return self.dispatch if self.dispatch and self.dispatch.name == name else None

	def submit(self, payload):
		if self.submit_error:
			raise self.submit_error
		if self.submit_races:
			self.docs = self.race_docs if self.race_docs is not None else [_existing(rows=_dispatch_rows())]
			raise tf.TransferDuplicateProvenance("dup")
		self.submitted.append(payload)
		return "MAT-STE-2026-00099"

	def read_back(self, name):
		assert self.ledger is not None  # `_run` sets the leg's own healthy ledger unless a test overrides it
		return self.ledger

	def commit(self):
		self.committed += 1


def _run(wire: dict, port: _Port | None = None, store: _Store | None = None, client: _Client | None = None):
	port = port or _Port()
	store = store or _Store()
	client = client or _Client()
	if port.ledger is None:
		port.ledger = _dispatch_ledger() if wire["kind"] == "transfer_dispatch" else _receive_ledger()
	item = c.PostingWorkItem.from_wire(wire)
	outcome = tf.post_transfer_work_item(
		item, client=client, store=store, port=port, site_tz=_CAIRO, uom_map=_UOMS
	)
	return outcome, client, port, store


def _dispatch(mutate=None) -> dict:
	wire = dispatch_wire()
	if mutate:
		mutate(wire["transfer"])
	return wire


def _receive(mutate=None) -> dict:
	wire = receive_wire()
	if mutate:
		mutate(wire["receipt"])
	return wire


def _reason(client: _Client) -> str:
	return client.last["reason"]["message"]


# ---------------------------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------------------------


class TestDispatchPosts:
	def test_it_posts_one_add_to_transit_material_transfer_and_acks_posted_with_the_stock_entry(self):
		outcome, client, port, store = _run(_dispatch())
		assert outcome == "posted"
		assert len(port.submitted) == 1
		assert client.last["outcome"] == "posted"
		assert client.last["documentRef"] == {"doctype": "Stock Entry", "name": "MAT-STE-2026-00099"}
		assert store.recorded

	def test_the_payload_is_the_native_goods_in_transit_dispatch(self):
		_, _, port, _ = _run(_dispatch())
		doc = port.submitted[0]
		assert doc["doctype"] == "Stock Entry"
		assert doc["stock_entry_type"] == "Material Transfer"
		assert doc["purpose"] == "Material Transfer"
		assert doc["add_to_transit"] == 1
		assert (doc["from_warehouse"], doc["to_warehouse"]) == (_SRC, _TRANSIT)
		assert doc["company"] == "RT Company"
		assert doc["set_posting_time"] == 1
		assert "outgoing_stock_entry" not in doc

	def test_the_payload_carries_the_leg_provenance_key(self):
		_, _, port, _ = _run(_dispatch())
		doc = port.submitted[0]
		assert doc["rt_source_system"] == "retail-tower"
		assert doc["rt_external_id"] == f"transfer_dispatch:{TRANSFER}"

	def test_the_posting_datetime_is_the_frozen_instant_in_the_site_timezone_never_now(self):
		_, _, port, _ = _run(_dispatch())
		doc = port.submitted[0]
		# 09:30 UTC is 11:30 in Cairo in November (EET, UTC+2).
		assert doc["posting_date"] == "2026-11-20"
		assert doc["posting_time"].startswith("11:30:00")

	def test_each_line_is_a_row_from_the_source_to_the_transit_warehouse_with_its_line_ref(self):
		_, _, port, _ = _run(_dispatch())
		first, second = port.submitted[0]["items"]
		assert first["item_code"] == _PANADOL
		assert (first["s_warehouse"], first["t_warehouse"]) == (_SRC, _TRANSIT)
		assert first["qty"] == "10"
		assert first["uom"] == "Box" and first["conversion_factor"] == 1
		assert first["rt_transfer_line_ref"] == LINE1
		assert "batch_no" not in first
		assert second["qty"] == "4.5"
		assert second["rt_transfer_line_ref"] == LINE2

	def test_a_batch_line_names_its_batch_and_uses_the_batch_fields(self):
		_, _, port, _ = _run(_dispatch())
		second = port.submitted[0]["items"][1]
		assert second["batch_no"] == _BATCH
		assert second["use_serial_batch_fields"] == 1

	def test_there_is_no_valuation_rate_anywhere_in_the_payload(self):
		_, _, port, _ = _run(_dispatch())
		text = repr(port.submitted[0]).lower()
		assert "valuation" not in text and "basic_rate" not in text

	def test_the_erp_effects_are_committed_before_the_ack(self):
		_, client, port, _ = _run(_dispatch())
		assert port.committed == 1
		assert len(client.acks) == 1

	def test_the_ack_key_is_the_per_outcome_key_of_the_work_item(self):
		_, client, _, _ = _run(_dispatch())
		assert client.acks[0][2] == "00000000-0000-7000-8000-00000000d001:posted"


class TestDispatchShape:
	@pytest.mark.parametrize(
		"mutate",
		[
			lambda t: t["lines"][1].update(lineSeq=3),  # not contiguous 1..n
			lambda t: t["lines"][1].update(lineSeq=1),  # duplicate lineSeq
			lambda t: t["lines"][1].update(transferLineRef=LINE1),  # duplicate transferLineRef
			lambda t: t.update(postingDatetime="2026-11-20T09:30:00"),  # not UTC
			lambda t: t.update(
				transitWarehouseRef={"doctype": "Warehouse", "name": _SRC}
			),  # source == transit
		],
	)
	def test_a_malformed_dispatch_is_rejected_as_validation_and_nothing_is_posted(self, mutate):
		outcome, client, port, _ = _run(_dispatch(mutate))
		assert outcome == "permanently_rejected"
		assert client.last["reason"]["category"] == "validation"
		assert "invalid_shape" in _reason(client)
		assert port.submitted == [] and port.committed == 0

	def test_an_external_id_that_is_not_the_leg_key_of_this_transfer_is_rejected(self):
		wire = _dispatch()
		wire["externalId"] = "transfer_dispatch:00000000-0000-7000-8000-00000000ffff"
		outcome, client, port, _ = _run(wire)
		assert outcome == "permanently_rejected"
		assert "provenance key" in _reason(client)
		assert port.submitted == []

	def test_the_provenance_suffix_compares_case_insensitively(self):
		wire = _dispatch()
		wire["externalId"] = f"transfer_dispatch:{TRANSFER.upper()}"
		outcome, _, _, _ = _run(wire)
		assert outcome == "posted"


class TestDispatchPreconditions:
	def test_a_missing_unique_index_pauses_the_leg_instead_of_posting_unguarded(self):
		port = _Port()
		port.ready = False
		outcome, client, _, _ = _run(_dispatch(), port)
		assert outcome == "failed_transient"
		assert client.last["outcome"] == "failed_transient"
		assert port.submitted == []

	@pytest.mark.parametrize(
		"setup, category, text",
		[
			(lambda p: p.facts.pop(_PANADOL), "unmapped_item", "does not exist"),
			(
				lambda p: p.facts.update({_AMOXIL: tf.TransferItemFacts(True, "Strip", True, True)}),
				"validation",
				"serial",
			),
			(
				lambda p: p.facts.update({_AMOXIL: tf.TransferItemFacts(True, "Strip", False, False)}),
				"validation",
				"not Batch tracked",
			),
			(
				lambda p: p.facts.update({_PANADOL: tf.TransferItemFacts(True, "Box", True, False)}),
				"validation",
				"needs a Batch",
			),
			(
				lambda p: p.facts.update({_PANADOL: tf.TransferItemFacts(True, "Nos", False, False)}),
				"validation",
				"stock UOM",
			),
			(lambda p: p.warehouse_problems.update({_SRC: "frozen"}), "validation", "frozen"),
			(
				lambda p: p.transit_problems.update({_TRANSIT: "not a Transit warehouse"}),
				"validation",
				"Transit",
			),
			(
				lambda p: p.batch_problems.update({(_AMOXIL, _BATCH): "Batch expired"}),
				"validation",
				"expired",
			),
		],
	)
	def test_one_bad_line_or_warehouse_refuses_the_whole_document_and_posts_nothing(
		self, setup, category, text
	):
		port = _Port()
		setup(port)
		outcome, client, _, _ = _run(_dispatch(), port)
		assert outcome == "permanently_rejected"
		assert client.last["reason"]["category"] == category
		assert text in _reason(client)
		assert port.submitted == [] and port.committed == 0

	def test_an_unmapped_unit_refuses_the_document(self):
		wire = _dispatch(lambda t: t["lines"][0].update(unit="Carton"))
		outcome, client, port, _ = _run(wire)
		assert outcome == "permanently_rejected"
		assert "UOM map" in _reason(client)
		assert port.submitted == []

	def test_the_batch_is_checked_against_the_frozen_posting_date(self):
		_, _, port, _ = _run(_dispatch())
		assert port.last_stamp is not None and port.last_stamp.posting_date == "2026-11-20"


class TestDispatchIdempotency:
	def test_a_matching_submitted_document_is_echoed_after_a_fresh_read_back(self):
		port = _Port()
		port.docs = [_existing()]
		outcome, client, _, store = _run(_dispatch(), port)
		assert outcome == "posted"
		assert port.submitted == []
		assert client.last["documentRef"]["name"] == "MAT-STE-2026-00099"
		assert store.recorded

	def test_an_echo_whose_ledger_no_longer_matches_is_reconciliation_required_never_posted(self):
		port = _Port()
		port.docs = [_existing()]
		port.ledger = [tf.LedgerRow(_PANADOL, _SRC, "-9", None)]
		outcome, client, _, _ = _run(_dispatch(), port)
		assert outcome == "reconciliation_required"
		assert client.last["documentRef"]["name"] == "MAT-STE-2026-00099"

	@pytest.mark.parametrize(
		"doc, why",
		[
			(_existing(posting_date="2026-11-19"), "stamped"),
			(_existing(add_to_transit=False), "add to transit"),
			(_existing(purpose="Material Issue"), "purpose"),
			(_existing(outgoing_stock_entry="X"), "against"),
			(_existing(rows=_dispatch_rows()[:1]), "rows"),
			(_existing(rows=(_row(_PANADOL, None, "9", _SRC, _TRANSIT), _dispatch_rows()[1])), "rows"),
		],
	)
	def test_a_submitted_document_that_is_not_this_intent_is_reconciliation_required(self, doc, why):
		port = _Port()
		port.docs = [doc]
		outcome, client, _, _ = _run(_dispatch(), port)
		assert outcome == "reconciliation_required"
		assert why in _reason(client)
		assert port.submitted == []

	def test_two_submitted_documents_with_the_key_are_reconciliation_required(self):
		port = _Port()
		port.docs = [_existing(name="A"), _existing(name="B")]
		outcome, client, _, _ = _run(_dispatch(), port)
		assert outcome == "reconciliation_required"
		assert "2 submitted" in _reason(client)

	def test_a_draft_that_is_in_flight_is_never_resubmitted(self):
		port = _Port()
		port.docs = [_existing(docstatus=0, in_flight=True)]
		outcome, _, _, _ = _run(_dispatch(), port)
		assert outcome == "failed_transient"
		assert port.submitted == []

	def test_a_failed_draft_is_reconciliation_required_with_its_failure(self):
		port = _Port()
		port.docs = [_existing(docstatus=0, failure="Action Failed: negative stock")]
		outcome, client, _, _ = _run(_dispatch(), port)
		assert outcome == "reconciliation_required"
		assert "negative stock" in _reason(client)

	def test_a_cancelled_document_is_an_unknown_outcome_never_reposted(self):
		port = _Port()
		port.docs = [_existing(docstatus=2)]
		outcome, client, _, _ = _run(_dispatch(), port)
		assert outcome == "reconciliation_required"
		assert "cancelled" in _reason(client)
		assert port.submitted == []

	def test_a_lost_race_on_the_unique_index_reclassifies_and_echoes_never_resubmits(self):
		port = _Port()
		port.submit_races = True
		outcome, _, port, _ = _run(_dispatch(), port)
		assert outcome == "posted"
		assert port.submitted == []

	def test_a_lost_race_that_the_lookup_cannot_see_yet_is_transient(self):
		port = _Port()
		port.submit_races = True
		port.race_docs = []
		outcome, _, _, _ = _run(_dispatch(), port)
		assert outcome == "failed_transient"

	def test_a_recorded_key_conflict_does_not_fail_the_ack(self):
		store = _Store()
		store.conflict = True
		outcome, _, _, _ = _run(_dispatch(), store=store)
		assert outcome == "posted"


class TestDispatchSubmitFailures:
	def test_a_transient_erp_failure_acks_failed_transient(self):
		port = _Port()
		port.submit_error = tf.TransferTransient("deadlock")
		outcome, client, _, _ = _run(_dispatch(), port)
		assert outcome == "failed_transient"
		assert client.last["outcome"] == "failed_transient"

	@pytest.mark.parametrize(
		"code, category",
		[
			("closed_period", "closed_period"),
			("back_dated_permission", "other"),
			("invalid_shape", "validation"),
		],
	)
	def test_an_erp_refusal_is_rejected_with_its_category(self, code, category):
		port = _Port()
		port.submit_error = tf.TransferRefused(code, "ERPNext said no")
		outcome, client, _, _ = _run(_dispatch(), port)
		assert outcome == "permanently_rejected"
		assert client.last["reason"]["category"] == category

	def test_an_unexpected_failure_before_the_submit_is_rejected_never_escapes(self):
		port = _Port()
		port.submit_error = RuntimeError("boom")
		outcome, client, _, _ = _run(_dispatch(), port)
		assert outcome == "permanently_rejected"
		assert "before anything was posted" in _reason(client)


class TestDispatchVerification:
	@pytest.mark.parametrize(
		"ledger",
		[
			[],  # nothing read back
			[
				tf.LedgerRow(_PANADOL, _SRC, "-10", None),
				tf.LedgerRow(_PANADOL, _TRANSIT, "10", None),
			],  # a line missing
			[
				*_dispatch_ledger()[:3],
				tf.LedgerRow(_AMOXIL, _TRANSIT, "4", _BATCH),  # wrong quantity at transit
			],
			[
				tf.LedgerRow(_PANADOL, _SRC, "-10", None),
				tf.LedgerRow(_PANADOL, _TRANSIT, "10", None),
				tf.LedgerRow(_AMOXIL, _SRC, "-4.5", _BATCH),
				tf.LedgerRow(_AMOXIL, _TRANSIT, "4.5", "OTHER-BATCH"),  # the batch differs on the two sides
			],
			[*_dispatch_ledger(), tf.LedgerRow(_PANADOL, "Elsewhere - RT", "1", None)],  # an unexpected entry
		],
	)
	def test_a_ledger_that_is_not_the_intent_is_reconciliation_required_with_the_created_document(
		self, ledger
	):
		port = _Port()
		port.ledger = ledger
		outcome, client, _, store = _run(_dispatch(), port)
		assert outcome == "reconciliation_required"
		assert client.last["documentRef"]["name"] == "MAT-STE-2026-00099"
		assert "verification failed" in _reason(client)
		assert store.recorded == {}

	def test_two_rows_of_one_item_in_one_warehouse_are_verified_in_aggregate(self):
		wire = _dispatch(
			lambda t: (
				t["lines"][0].update(quantity="6"),
				t["lines"][1].update(
					erpnextItemRef={"doctype": "Item", "name": _PANADOL},
					erpnextBatchRef=None,
					quantity="4",
					unit="Box",
				),
			)
		)
		port = _Port()
		port.ledger = [
			tf.LedgerRow(_PANADOL, _SRC, "-10", None),
			tf.LedgerRow(_PANADOL, _TRANSIT, "10", None),
		]
		outcome, _, _, _ = _run(wire, port)
		assert outcome == "posted"


# ---------------------------------------------------------------------------------------------
# Receive
# ---------------------------------------------------------------------------------------------


class TestReceivePosts:
	def test_it_posts_one_material_transfer_against_the_dispatch_and_acks_posted(self):
		outcome, client, port, store = _run(_receive())
		assert outcome == "posted"
		assert client.last["documentRef"] == {"doctype": "Stock Entry", "name": "MAT-STE-2026-00099"}
		assert len(port.submitted) == 1 and port.committed == 1
		assert store.recorded

	def test_the_payload_moves_stock_from_transit_to_the_destination_against_the_dispatch(self):
		_, _, port, _ = _run(_receive())
		doc = port.submitted[0]
		assert doc["stock_entry_type"] == "Material Transfer" and doc["purpose"] == "Material Transfer"
		assert doc["outgoing_stock_entry"] == _DISPATCH_DOC
		assert (doc["from_warehouse"], doc["to_warehouse"]) == (_TRANSIT, _DEST)
		assert doc.get("add_to_transit", 0) == 0
		assert doc["rt_source_system"] == "retail-tower"
		assert doc["rt_external_id"] == f"transfer_receipt:{RECEIPT}"
		assert doc["posting_time"].startswith("13:00:00")  # 11:00 UTC is 13:00 in Cairo

	def test_each_line_names_its_dispatch_row_and_document(self):
		_, _, port, _ = _run(_receive())
		(row,) = port.submitted[0]["items"]
		assert row["item_code"] == _PANADOL and row["qty"] == "6"
		assert (row["s_warehouse"], row["t_warehouse"]) == (_TRANSIT, _DEST)
		assert row["ste_detail"] == "d-row-1"
		assert row["against_stock_entry"] == _DISPATCH_DOC
		assert row["rt_transfer_line_ref"] == LINE1

	def test_a_partial_receipt_up_to_the_remaining_quantity_posts(self):
		port = _Port()
		port.dispatch = _dispatch_facts(
			rows=(tf.DispatchRow(1, "d-row-1", _PANADOL, None, "10", "4", _TRANSIT, LINE1),)
		)
		outcome, _, _, _ = _run(_receive(), port)  # 6 received, 4 already transferred, 10 dispatched
		assert outcome == "posted"

	def test_a_batch_receipt_carries_the_batch(self):
		wire = _receive(
			lambda r: r["lines"].__setitem__(
				0,
				{
					"lineSeq": 1,
					"dispatchLineSeq": 2,
					"transferLineRef": LINE2,
					"erpnextItemRef": {"doctype": "Item", "name": _AMOXIL},
					"erpnextBatchRef": {"doctype": "Batch", "name": _BATCH},
					"quantity": "2",
					"unit": "Strip",
				},
			)
		)
		port = _Port()
		port.ledger = [
			tf.LedgerRow(_AMOXIL, _TRANSIT, "-2", _BATCH),
			tf.LedgerRow(_AMOXIL, _DEST, "2", _BATCH),
		]
		outcome, _, port, _ = _run(wire, port)
		assert outcome == "posted"
		row = port.submitted[0]["items"][0]
		assert row["batch_no"] == _BATCH and row["use_serial_batch_fields"] == 1
		assert row["ste_detail"] == "d-row-2"


class TestReceiveRejects:
	@pytest.mark.parametrize(
		"setup, text",
		[
			(lambda p: setattr(p, "dispatch", None), "dispatch document"),
			(lambda p: setattr(p, "dispatch", _dispatch_facts(docstatus=0)), "not submitted"),
			(lambda p: setattr(p, "dispatch", _dispatch_facts(docstatus=2)), "not submitted"),
			(lambda p: setattr(p, "dispatch", _dispatch_facts(add_to_transit=False)), "add to transit"),
			(
				lambda p: setattr(p, "dispatch", _dispatch_facts(purpose="Material Issue")),
				"Material Transfer",
			),
			(lambda p: setattr(p, "dispatch", _dispatch_facts(company="Other Co")), "company"),
			(lambda p: setattr(p, "dispatch", _dispatch_facts(rows=())), "row 1"),
			(
				lambda p: setattr(
					p,
					"dispatch",
					_dispatch_facts(
						rows=(tf.DispatchRow(1, "d-row-1", _AMOXIL, None, "10", "0", _TRANSIT, LINE1),)
					),
				),
				"item",
			),
			(
				lambda p: setattr(
					p,
					"dispatch",
					_dispatch_facts(
						rows=(tf.DispatchRow(1, "d-row-1", _PANADOL, "B1", "10", "0", _TRANSIT, LINE1),)
					),
				),
				"batch",
			),
			(
				lambda p: setattr(
					p,
					"dispatch",
					_dispatch_facts(
						rows=(tf.DispatchRow(1, "d-row-1", _PANADOL, None, "10", "0", _TRANSIT, "other-ref"),)
					),
				),
				"transferLineRef",
			),
			(
				lambda p: setattr(
					p,
					"dispatch",
					_dispatch_facts(
						rows=(tf.DispatchRow(1, "d-row-1", _PANADOL, None, "10", "0", _TRANSIT, None),)
					),
				),
				"transferLineRef",
			),
			(
				lambda p: setattr(
					p,
					"dispatch",
					_dispatch_facts(
						rows=(
							tf.DispatchRow(1, "d-row-1", _PANADOL, None, "10", "0", "Other Transit", LINE1),
						)
					),
				),
				"transit",
			),
			(
				lambda p: setattr(
					p,
					"dispatch",
					_dispatch_facts(
						rows=(tf.DispatchRow(1, "d-row-1", _PANADOL, None, "10", "5", _TRANSIT, LINE1),)
					),
				),
				"over-receipt",
			),
			(lambda p: p.warehouse_problems.update({_DEST: "frozen"}), "frozen"),
			(lambda p: p.transit_problems.update({_TRANSIT: "not a Transit warehouse"}), "Transit"),
			(lambda p: p.facts.pop(_PANADOL), "does not exist"),
		],
	)
	def test_a_receipt_that_cannot_be_posted_is_rejected_before_anything_is_written(self, setup, text):
		port = _Port()
		setup(port)
		outcome, client, _, _ = _run(_receive(), port)
		assert outcome == "permanently_rejected"
		assert text in _reason(client)
		assert port.submitted == [] and port.committed == 0

	def test_a_receipt_above_the_remaining_quantity_is_refused_as_an_over_receipt(self):
		wire = _receive(lambda r: r["lines"][0].update(quantity="10.5"))
		outcome, client, port, _ = _run(wire)
		assert outcome == "permanently_rejected"
		assert "over-receipt" in _reason(client)
		assert port.submitted == []

	def test_two_lines_on_one_dispatch_row_are_summed_against_the_remaining_quantity(self):
		def two(r):
			second = copy.deepcopy(r["lines"][0])
			second["lineSeq"] = 2
			second["quantity"] = "5"
			r["lines"].append(second)

		outcome, client, _, _ = _run(_receive(two))  # 6 + 5 > 10
		assert outcome == "permanently_rejected"
		assert "over-receipt" in _reason(client)

	@pytest.mark.parametrize(
		"mutate",
		[
			lambda r: r["lines"][0].update(lineSeq=2),  # not contiguous
			lambda r: r.update(
				destinationWarehouseRef={"doctype": "Warehouse", "name": _TRANSIT}
			),  # dest == transit
			lambda r: r.update(postingDatetime="2026-11-21T11:00:00"),  # not UTC
		],
	)
	def test_a_malformed_receipt_is_rejected_as_validation(self, mutate):
		outcome, client, port, _ = _run(_receive(mutate))
		assert outcome == "permanently_rejected"
		assert client.last["reason"]["category"] == "validation"
		assert port.submitted == []

	def test_an_external_id_that_is_not_this_receipt_is_rejected(self):
		wire = _receive()
		wire["externalId"] = "transfer_receipt:00000000-0000-7000-8000-00000000ffff"
		outcome, client, _, _ = _run(wire)
		assert outcome == "permanently_rejected"
		assert "provenance key" in _reason(client)


class TestReceiveIdempotencyAndVerification:
	def _doc(self, **kw):
		base = dict(
			outgoing_stock_entry=_DISPATCH_DOC,
			add_to_transit=False,
			posting_time="13:00:00.000000",
			posting_date="2026-11-21",
			rows=(_row(_PANADOL, None, "6", _TRANSIT, _DEST),),
		)
		base.update(kw)
		return _existing(**base)

	def test_a_matching_submitted_receipt_is_echoed_after_a_read_back(self):
		port = _Port()
		port.docs = [self._doc()]
		outcome, _, _, _ = _run(_receive(), port)
		assert outcome == "posted"
		assert port.submitted == []

	@pytest.mark.parametrize(
		"doc_kw",
		[
			dict(outgoing_stock_entry="OTHER"),
			dict(rows=(_row(_PANADOL, None, "5", _TRANSIT, _DEST),)),
			dict(rows=(_row(_PANADOL, None, "6", _TRANSIT, "Elsewhere - RT"),)),
		],
	)
	def test_a_receipt_document_that_is_not_this_intent_is_reconciliation_required(self, doc_kw):
		port = _Port()
		port.docs = [self._doc(**doc_kw)]
		outcome, _, _, _ = _run(_receive(), port)
		assert outcome == "reconciliation_required"
		assert port.submitted == []

	@pytest.mark.parametrize(
		"ledger",
		[
			[],
			[tf.LedgerRow(_PANADOL, _TRANSIT, "-6", None)],  # nothing arrived at the destination
			[tf.LedgerRow(_PANADOL, _TRANSIT, "-6", None), tf.LedgerRow(_PANADOL, _DEST, "5", None)],
		],
	)
	def test_a_receipt_ledger_that_is_not_the_intent_is_reconciliation_required(self, ledger):
		port = _Port()
		port.ledger = ledger
		outcome, client, _, _ = _run(_receive(), port)
		assert outcome == "reconciliation_required"
		assert client.last["documentRef"]["name"] == "MAT-STE-2026-00099"

	def test_the_receive_leg_uses_its_own_replay_key(self):
		_, _, _, store = _run(_receive())
		assert next(iter(store.recorded)) == ("retail-tower", f"transfer_receipt:{RECEIPT}")


class TestSharedRules:
	def test_the_amounts_are_exact_decimals(self):
		assert Decimal("4.5") + Decimal("10") == Decimal("14.5")  # guards the fixture, not the flow
		_, _, port, _ = _run(_dispatch())
		assert all(isinstance(r["qty"], str) for r in port.submitted[0]["items"])

	def test_it_refuses_a_work_item_of_another_kind(self):
		item = c.PostingWorkItem.from_wire(dispatch_wire())
		sale_like = copy.copy(item)
		object.__setattr__(sale_like, "kind", "sale_post")
		with pytest.raises(ValueError, match="transfer"):
			tf.post_transfer_work_item(
				sale_like, client=_Client(), store=_Store(), port=_Port(), site_tz=_CAIRO, uom_map=_UOMS
			)


class TestRetryOfAnAlreadyPostedLegEchoesBeforeAnyLiveStateCheck:
	"""RT-1514 review: the provenance lookup comes FIRST. A document that exists is echoed (after a read-back),
	even when the live site state it was posted against has since moved on."""

	def _posted_receipt(self):
		return _existing(
			outgoing_stock_entry=_DISPATCH_DOC,
			add_to_transit=False,
			posting_date="2026-11-21",
			posting_time="13:00:00.000000",
			rows=(_row(_PANADOL, None, "6", _TRANSIT, _DEST),),
		)

	def test_a_receipt_that_took_the_last_quantity_is_echoed_not_rejected_as_an_over_receipt(self):
		port = _Port()
		port.docs = [self._posted_receipt()]
		# ERPNext already counted this receipt in the dispatch row when it submitted: 10 of 10 transferred.
		port.dispatch = _dispatch_facts(
			rows=(tf.DispatchRow(1, "d-row-1", _PANADOL, None, "10", "10", _TRANSIT, LINE1),)
		)
		outcome, client, _, _ = _run(_receive(), port)
		assert outcome == "posted"
		assert port.submitted == []
		assert client.last["documentRef"]["name"] == "MAT-STE-2026-00099"

	def test_a_posted_dispatch_is_echoed_even_if_its_source_warehouse_has_since_been_frozen(self):
		port = _Port()
		port.docs = [_existing()]
		port.warehouse_problems[_SRC] = "frozen"
		outcome, _, _, _ = _run(_dispatch(), port)
		assert outcome == "posted"
		assert port.submitted == []

	def test_a_posted_dispatch_is_echoed_even_if_its_item_or_batch_state_changed(self):
		port = _Port()
		port.docs = [_existing()]
		port.facts.pop(_PANADOL)
		port.batch_problems[(_AMOXIL, _BATCH)] = "Batch expired"
		outcome, _, _, _ = _run(_dispatch(), port)
		assert outcome == "posted"

	def test_a_posted_receipt_is_echoed_even_if_the_dispatch_document_can_no_longer_be_read(self):
		port = _Port()
		port.docs = [self._posted_receipt()]
		port.dispatch = None
		outcome, _, _, _ = _run(_receive(), port)
		assert outcome == "posted"
		assert port.submitted == []

	def test_a_reconciliation_decision_does_not_need_the_live_checks_either(self):
		port = _Port()
		port.docs = [_existing(rows=_dispatch_rows()[:1])]
		port.warehouse_problems[_SRC] = "frozen"
		outcome, _, _, _ = _run(_dispatch(), port)
		assert outcome == "reconciliation_required"

	def test_the_live_checks_still_refuse_a_leg_that_has_no_document_yet(self):
		port = _Port()
		port.warehouse_problems[_SRC] = "frozen"
		outcome, _, _, _ = _run(_dispatch(), port)
		assert outcome == "permanently_rejected"
		assert port.submitted == []


class TestDispatchRowsFollowLineSeq:
	"""RT-1514 review: the contract says ``lineSeq`` IS the dispatch Stock Entry row ``idx``."""

	def test_the_payload_rows_are_in_line_seq_order_whatever_the_wire_order(self):
		wire = _dispatch(lambda t: t["lines"].reverse())
		_, _, port, _ = _run(wire)
		first, second = port.submitted[0]["items"]
		assert (first["item_code"], first["rt_transfer_line_ref"]) == (_PANADOL, LINE1)
		assert (second["item_code"], second["rt_transfer_line_ref"]) == (_AMOXIL, LINE2)
