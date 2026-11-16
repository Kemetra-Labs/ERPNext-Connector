# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1422 — the ``inventory_count`` leg against a fake ERP port (no frappe, no bench).

Every outcome of one count chunk is exercised here: a native absolute-target Stock Reconciliation, the
frozen Cut+1µs stamp, no valuation rate, per-line refusals that do not stop the other lines, a verified
no-op that never fakes a documentRef, Draft-aware idempotency (echo / in flight / failed Draft /
cancelled / mismatching / several documents), a lost race on the unique index, ERP refusals, and a
failed post-write verification. The real-ERPNext behaviour is NOT proven here (see the PR: NOT_VERIFIED).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from retail_tower_erpnext_connector.connector.posting import contracts as c
from retail_tower_erpnext_connector.connector.posting import count_flow as cf
from retail_tower_erpnext_connector.connector.posting.idempotency import IdempotencyConflict
from retail_tower_erpnext_connector.connector.posting.uom import UomMap

_SESSION = "00000000-0000-7000-8000-00000000c0e1"
_UOMS = UomMap({"Nos": "Nos", "Strip": "Strip", "Box": "Box", "each": "Nos"})
_STAMP_UTC = "2026-11-16T09:30:00.000001Z"
_CAIRO = "Africa/Cairo"
_PAIR = "PANADOL-24"
_ITEM2 = "AMOXIL-12"
_WH = "Stores - RT"


def _line_wire(seq: int, item: str, qty: str, unit: str = "Nos") -> dict:
	return {
		"lineSeq": seq,
		"erpnextItemRef": {"doctype": "Item", "name": item},
		"warehouseRef": {"doctype": "Warehouse", "name": _WH},
		"countedQuantity": qty,
		"unit": unit,
	}


def _wire(lines: list[dict], *, chunk: int = 1, chunk_count: int = 1, external_id: str | None = None) -> dict:
	key = external_id or (f"count:{_SESSION}" if chunk_count == 1 else f"count:{_SESSION}:{chunk}")
	return {
		"workItemRef": "00000000-0000-7000-8000-00000000c001",
		"kind": "inventory_count",
		"sourceSystem": "retail-tower",
		"externalId": key,
		"payloadHash": "a" * 64,
		"businessDate": "2026-11-16",
		"itemCursor": "c1",
		"count": {
			"countSessionRef": _SESSION,
			"chunk": chunk,
			"chunkCount": chunk_count,
			"storeId": "00000000-0000-7000-8000-00000000570e",
			"mappingBucketRef": "bucket:store:stock",
			"postingDatetime": _STAMP_UTC,
			"lines": lines,
		},
	}


def _item(lines: list[dict] | None = None, **kw) -> c.PostingWorkItem:
	return c.PostingWorkItem.from_wire(
		_wire(lines or [_line_wire(1, _PAIR, "7"), _line_wire(2, _ITEM2, "0", "Strip")], **kw)
	)


class _Client:
	def __init__(self) -> None:
		self.acks: list[tuple[str, dict, str | None]] = []

	def ack_outcome(self, ref, ack, *, idempotency_key=None):
		self.acks.append((ref, ack.to_wire(), idempotency_key))


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


class _Port:
	"""A scriptable ERP site: items, balances, existing documents and the outcome of the next submit."""

	def __init__(self) -> None:
		self.ready = True
		self.facts = {
			_PAIR: cf.ItemFacts(True, "Nos", False, True),
			_ITEM2: cf.ItemFacts(True, "Strip", False, True),
		}
		self.balances: dict[tuple[str, str], Decimal] = {(_PAIR, _WH): Decimal(10), (_ITEM2, _WH): Decimal(4)}
		self.docs: list[cf.ExistingCount] = []
		self.warehouse_problems: dict[str, str] = {}
		self.submit_error: Exception | None = None
		self.submit_races = False
		self.submitted: list[dict] = []
		self.committed = 0
		self.read_back_override: list[cf.LineReadBack] | None = None

	def indexes_ready(self):
		return self.ready

	def company(self):
		return "RT Company"

	def item_facts(self, codes):
		return {k: v for k, v in self.facts.items() if k in codes}

	def warehouse_problem(self, warehouse, company):
		return self.warehouse_problems.get(warehouse)

	def existing_documents(self, source, external):
		return list(self.docs)

	def balance_at(self, item, warehouse, stamp):
		return self.balances.get((item, warehouse), Decimal(0))

	def submit(self, payload):
		if self.submit_error:
			raise self.submit_error
		if self.submit_races:
			self.docs = [_doc(rows=((_PAIR, _WH, "7"), (_ITEM2, _WH, "0")))]
			raise cf.CountDuplicateProvenance("dup")
		self.submitted.append(payload)
		return "MAT-RECO-2026-00017"

	def read_back(self, name, rows):
		if self.read_back_override is not None:
			return self.read_back_override
		if not self.submitted:  # an echo reads back a document that already exists on the site
			qty = {(i, w): q for d in self.docs for i, w, q in d.rows}
			return [cf.LineReadBack(i, w, qty[(i, w)], qty[(i, w)], "0") for i, w in rows]
		qty = {line["item_code"]: line["qty"] for line in self.submitted[-1]["items"]}
		return [cf.LineReadBack(i, w, qty[i], qty[i], "0") for i, w in rows]

	def commit(self):
		self.committed += 1


def _doc(**kw) -> cf.ExistingCount:
	base = {
		"name": "MAT-RECO-2026-00017",
		"docstatus": 1,
		"posting_date": "2026-11-16",
		"posting_time": "11:30:00.000001",
		"rows": ((_PAIR, _WH, "7"), (_ITEM2, _WH, "0")),
	}
	return cf.ExistingCount(**{**base, **kw})


def _run(item=None, port=None, store=None, uom_map=None):
	port, store, client = port or _Port(), store or _Store(), _Client()
	outcome = cf.post_count_work_item(
		item or _item(), client=client, store=store, port=port, site_tz=_CAIRO, uom_map=uom_map or _UOMS
	)
	return outcome, client, port, store


def _ack(client, index=-1) -> dict:
	return client.acks[index][1]


class TestNormalPost:
	def test_posts_absolute_targets_at_the_frozen_cut_plus_one_microsecond(self):
		outcome, _client, port, _store = _run()
		assert outcome == "posted"
		(payload,) = port.submitted
		# 09:30:00.000001 UTC is 11:30:00.000001 in Cairo (UTC+2 in November): the stamp is the cut +1µs.
		assert (payload["posting_date"], payload["posting_time"], payload["set_posting_time"]) == (
			"2026-11-16",
			"11:30:00.000001",
			1,
		)
		assert payload["purpose"] == "Stock Reconciliation" and payload["company"] == "RT Company"
		assert payload["items"] == [
			{"item_code": _PAIR, "warehouse": _WH, "qty": "7"},
			{"item_code": _ITEM2, "warehouse": _WH, "qty": "0"},
		]
		assert (payload["rt_source_system"], payload["rt_external_id"]) == (
			"retail-tower",
			f"count:{_SESSION}",
		)

	def test_the_payload_carries_no_valuation_rate_anywhere(self):
		_, _, port, _ = _run()
		(payload,) = port.submitted
		text = repr(payload).lower()
		assert "valuation" not in text and "rate" not in text
		assert all(set(row) == {"item_code", "warehouse", "qty"} for row in payload["items"])

	def test_acks_posted_with_the_document_and_per_line_evidence_after_committing(self):
		_, client, port, store = _run()
		ack = _ack(client)
		assert ack["outcome"] == "posted"
		assert ack["documentRef"] == {"doctype": "Stock Reconciliation", "name": "MAT-RECO-2026-00017"}
		assert [(x["lineSeq"], x["result"], x["verifiedQtyAfter"]) for x in ack["lines"]] == [
			(1, "adjusted", "7"),
			(2, "adjusted", "0"),
		]
		assert port.committed == 1  # RT-458: durable before Backend-Core hears of it
		assert client.acks[0][2] == "00000000-0000-7000-8000-00000000c001:posted"
		assert next(iter(store.recorded.values())).name == "MAT-RECO-2026-00017"

	def test_a_two_chunk_session_keys_each_chunk_and_freezes_the_same_stamp(self):
		first = _item([_line_wire(1, _PAIR, "7")], chunk=1, chunk_count=2)
		second = _item([_line_wire(1, _ITEM2, "3", "Strip")], chunk=2, chunk_count=2)
		_, _, port1, _ = _run(first)
		_, _, port2, _ = _run(second)
		assert port1.submitted[0]["rt_external_id"] == f"count:{_SESSION}:1"
		assert port2.submitted[0]["rt_external_id"] == f"count:{_SESSION}:2"
		assert port1.submitted[0]["posting_time"] == port2.submitted[0]["posting_time"] == "11:30:00.000001"


class TestNoop:
	def test_every_line_already_at_target_is_a_verified_noop_with_no_document(self):
		port = _Port()
		port.balances = {(_PAIR, _WH): Decimal(7), (_ITEM2, _WH): Decimal(0)}
		outcome, client, port, store = _run(port=port)
		assert outcome == "verified_noop"
		ack = _ack(client)
		assert ack["outcome"] == "verified_noop" and "documentRef" not in ack
		assert [x["result"] for x in ack["lines"]] == ["verified_noop", "verified_noop"]
		assert port.submitted == [] and store.recorded == {}

	def test_a_line_at_target_is_left_out_of_the_document_and_reported_as_a_noop(self):
		port = _Port()
		port.balances = {(_PAIR, _WH): Decimal(7), (_ITEM2, _WH): Decimal(4)}
		_, client, port, _ = _run(port=port)
		assert [r["item_code"] for r in port.submitted[0]["items"]] == [_ITEM2]
		assert [(x["lineSeq"], x["result"]) for x in _ack(client)["lines"]] == [
			(1, "verified_noop"),
			(2, "adjusted"),
		]


class TestLineRefusals:
	@pytest.mark.parametrize(
		("mutate", "code", "category"),
		[
			(lambda p: p.facts.pop(_PAIR), "unmapped_item", "unmapped_item"),
			(
				lambda p: p.facts.update({_PAIR: cf.ItemFacts(True, "Nos", True, True)}),
				"tracked_item_unsupported",
				"validation",
			),
			(
				lambda p: p.facts.update({_PAIR: cf.ItemFacts(True, "Box", False, True)}),
				"uom_mismatch",
				"validation",
			),
			(
				lambda p: p.warehouse_problems.update({_WH: "a group warehouse"}),
				"unmapped_bucket",
				"validation",
			),
		],
	)
	def test_a_refused_line_is_typed_and_the_other_line_still_posts(self, mutate, code, category):
		port = _Port()
		mutate(port)
		item = _item([_line_wire(1, _PAIR, "7"), _line_wire(2, _ITEM2, "2", "Strip")])
		if code == "unmapped_bucket":
			port.warehouse_problems = {}  # the problem is on the first line's warehouse only: use a second warehouse
			wire = _wire([_line_wire(1, _PAIR, "7"), _line_wire(2, _ITEM2, "2", "Strip")])
			wire["count"]["lines"][0]["warehouseRef"]["name"] = "Group - RT"
			port.warehouse_problems["Group - RT"] = "a group warehouse"
			port.balances[(_PAIR, "Group - RT")] = Decimal(0)
			item = c.PostingWorkItem.from_wire(wire)
		outcome, client, port, _ = _run(item, port)
		assert outcome == "posted"
		assert [r["item_code"] for r in port.submitted[0]["items"]] == [_ITEM2]
		refused = [x for x in _ack(client)["lines"] if x["result"] == "rejected"]
		assert len(refused) == 1 and refused[0]["lineSeq"] == 1
		assert refused[0]["refusal"]["code"] == code and refused[0]["refusal"]["ackCategory"] == category
		assert refused[0]["verifiedQtyAfter"] is None

	def test_a_count_up_with_no_stock_history_is_refused_but_a_count_down_to_zero_is_not(self):
		port = _Port()
		port.facts[_PAIR] = cf.ItemFacts(True, "Nos", False, False)
		port.balances[(_PAIR, _WH)] = Decimal(0)
		_, client, port, _ = _run(port=port)
		refused = [x for x in _ack(client)["lines"] if x["result"] == "rejected"]
		assert refused[0]["refusal"]["code"] == "no_valuation_history"
		zero = _Port()
		zero.facts[_PAIR] = cf.ItemFacts(True, "Nos", False, False)
		zero.balances[(_PAIR, _WH)] = Decimal(0)
		item = _item([_line_wire(1, _PAIR, "0")])
		outcome, _, _, _ = _run(item, zero)
		assert outcome == "verified_noop"  # zero against zero is a no-op, never a refusal

	def test_every_line_refused_rejects_the_chunk_permanently_with_the_lines(self):
		port = _Port()
		port.facts.clear()
		outcome, client, port, _ = _run(port=port)
		assert outcome == "permanently_rejected"
		ack = _ack(client)
		assert ack["reason"]["category"] == "unmapped_item" and len(ack["lines"]) == 2
		assert port.submitted == []


class TestShapeRules:
	@pytest.mark.parametrize(
		"item",
		[
			lambda: _item(external_id="count:00000000-0000-7000-8000-0000000000ff"),
			lambda: _item([_line_wire(1, _PAIR, "1"), _line_wire(1, _ITEM2, "1", "Strip")]),
			lambda: _item([_line_wire(1, _PAIR, "1"), _line_wire(2, _PAIR, "2")]),
			lambda: _item(chunk=3, chunk_count=2, external_id=f"count:{_SESSION}:3"),
		],
	)
	def test_a_chunk_that_breaks_a_server_side_rule_is_rejected_before_anything_is_read(self, item):
		outcome, client, port, _ = _run(item())
		assert outcome == "permanently_rejected"
		assert _ack(client)["reason"]["category"] == "validation"
		assert port.submitted == []

	def test_a_site_stamp_needs_a_utc_instant_and_a_known_timezone(self):
		with pytest.raises(ValueError, match="UTC"):
			cf.site_stamp("2026-11-16T09:30:00.000001+02:00", _CAIRO)
		with pytest.raises(ValueError, match="timezone"):
			cf.site_stamp(_STAMP_UTC, "Nowhere/Land")

	def test_a_missing_unique_index_pauses_the_chunk_without_creating_anything(self):
		port = _Port()
		port.ready = False
		outcome, client, port, _ = _run(port=port)
		assert outcome == "failed_transient" and port.submitted == []
		assert client.acks[0][2].endswith(":failed_transient:c1")


class TestDraftAwareIdempotency:
	def test_a_submitted_matching_document_is_echoed_not_reposted(self):
		port = _Port()
		port.docs = [_doc()]
		outcome, client, port, store = _run(port=port)
		assert outcome == "posted" and port.submitted == []
		ack = _ack(client)
		assert ack["documentRef"]["name"] == "MAT-RECO-2026-00017"
		assert [x["result"] for x in ack["lines"]] == ["adjusted", "adjusted"]
		assert len(store.recorded) == 1

	def test_the_replay_ack_body_equals_the_first_runs(self):
		first, client1, _, _ = _run()
		port = _Port()
		port.docs = [_doc()]
		port.balances = {(_PAIR, _WH): Decimal(7), (_ITEM2, _WH): Decimal(0)}  # the cut is now in the ledger
		_, client2, _, _ = _run(port=port)
		assert first == "posted" and _ack(client1) == _ack(client2)
		assert client1.acks[0][2] == client2.acks[0][2]  # one Idempotency-Key, so no 409 at Backend-Core

	def test_an_echo_reads_the_ledger_back_and_never_confirms_a_document_that_does_not_verify(self):
		# A crash after the submit, or a lost reconciliation ack, then a re-offer: the matching submitted
		# document is re-verified before `posted`, never echoed blind.
		port = _Port()
		port.docs = [_doc()]
		port.read_back_override = [
			cf.LineReadBack(_PAIR, _WH, "6", "6", "0"),
			cf.LineReadBack(_ITEM2, _WH, "0", "0", "0"),
		]
		outcome, client, port, store = _run(port=port)
		assert outcome == "reconciliation_required" and port.submitted == [] and store.recorded == {}
		ack = _ack(client)
		assert ack["documentRef"]["name"] == "MAT-RECO-2026-00017"
		assert "verification failed on the existing document" in ack["reason"]["message"]

	def test_a_lost_race_whose_winner_does_not_verify_is_reconciliation_required_too(self):
		port = _Port()
		port.submit_races = True
		port.read_back_override = [cf.LineReadBack(_PAIR, _WH, "7", "99", "0")]
		outcome, _, port, _ = _run(port=port)
		assert outcome == "reconciliation_required" and port.submitted == []

	def test_a_draft_that_is_in_flight_is_never_resubmitted(self):
		port = _Port()
		port.docs = [_doc(docstatus=0, in_flight=True)]
		outcome, _, port, _ = _run(port=port)
		assert outcome == "failed_transient" and port.submitted == []

	def test_a_failed_draft_needs_reconciliation_and_carries_the_erp_reason(self):
		port = _Port()
		port.docs = [_doc(docstatus=0, in_flight=False, failure="Action Failed: negative stock")]
		outcome, client, port, _ = _run(port=port)
		assert outcome == "reconciliation_required" and port.submitted == []
		ack = _ack(client)
		assert (
			ack["documentRef"]["name"] == "MAT-RECO-2026-00017"
			and "negative stock" in ack["reason"]["message"]
		)

	def test_a_cancelled_document_is_an_unknown_outcome_never_reposted(self):
		port = _Port()
		port.docs = [_doc(docstatus=2)]
		outcome, client, port, _ = _run(port=port)
		assert outcome == "reconciliation_required" and port.submitted == []
		assert "cancelled" in _ack(client)["reason"]["message"]

	@pytest.mark.parametrize(
		"doc",
		[
			_doc(posting_time="11:30:00.000000"),
			_doc(posting_date="2026-11-15"),
			_doc(rows=((_PAIR, _WH, "8"), (_ITEM2, _WH, "0"))),
			_doc(rows=((_PAIR, _WH, "7"), ("OTHER", _WH, "1"))),
		],
	)
	def test_a_submitted_document_that_is_not_this_intent_needs_reconciliation(self, doc):
		port = _Port()
		port.docs = [doc]
		outcome, _, port, _ = _run(port=port)
		assert outcome == "reconciliation_required" and port.submitted == []

	def test_two_documents_for_one_key_need_reconciliation(self):
		port = _Port()
		port.docs = [_doc(), _doc(name="MAT-RECO-2026-00018")]
		outcome, _, _, _ = _run(port=port)
		assert outcome == "reconciliation_required"

	def test_a_lost_race_on_the_unique_index_resolves_to_the_winner(self):
		port = _Port()
		port.submit_races = True
		outcome, client, port, _ = _run(port=port)
		assert outcome == "posted" and port.submitted == []
		assert _ack(client)["documentRef"]["name"] == "MAT-RECO-2026-00017"

	def test_a_recorded_conflict_in_the_posting_log_does_not_fail_the_ack(self):
		store = _Store()
		store.conflict = True
		outcome, _, _, _ = _run(store=store)
		assert outcome == "posted"


class TestErpRefusalsAndVerification:
	@pytest.mark.parametrize(
		("code", "category"),
		[
			("closed_period", "closed_period"),
			("back_dated_permission", "other"),
			("invalid_shape", "validation"),
		],
	)
	def test_an_erp_refusal_is_a_typed_permanent_rejection(self, code, category):
		port = _Port()
		port.submit_error = cf.CountRefused(code, "ERP said no")
		outcome, client, _, _ = _run(port=port)
		assert outcome == "permanently_rejected"
		assert (
			_ack(client)["reason"]["category"] == category
			and "ERP said no" in _ack(client)["reason"]["message"]
		)

	def test_a_transient_erp_failure_is_failed_transient(self):
		port = _Port()
		port.submit_error = cf.CountTransient("deadlock")
		outcome, _, _, _ = _run(port=port)
		assert outcome == "failed_transient"

	@pytest.mark.parametrize(
		"read_back",
		[
			[cf.LineReadBack(_PAIR, _WH, "6", "6", "0"), cf.LineReadBack(_ITEM2, _WH, "0", "0", "0")],
			[cf.LineReadBack(_PAIR, _WH, "7", "9", "0"), cf.LineReadBack(_ITEM2, _WH, "0", "0", "0")],
			[cf.LineReadBack(_PAIR, _WH, "7", "7", "0")],
		],
	)
	def test_a_failed_verification_is_reconciliation_required_never_a_false_confirmation(self, read_back):
		port = _Port()
		port.read_back_override = read_back
		outcome, client, _, store = _run(port=port)
		assert outcome == "reconciliation_required"
		assert _ack(client)["documentRef"]["name"] == "MAT-RECO-2026-00017" and store.recorded == {}

	def test_a_later_movement_is_part_of_the_bin_check_not_a_failure(self):
		port = _Port()
		port.read_back_override = [
			cf.LineReadBack(_PAIR, _WH, "7", "5", "-2"),  # a post-cut sale of 2 is already in the ledger
			cf.LineReadBack(_ITEM2, _WH, "0", "0", "0"),
		]
		outcome, _, _, _ = _run(port=port)
		assert outcome == "posted"


class TestTerminalOutcomeInvariant:
	"""An unclassified failure never escapes to abort the page: it reaches a terminal ack."""

	def test_an_unexpected_read_failure_before_anything_is_posted_is_rejected_as_other(self):
		port = _Port()

		def boom(*_):
			raise RuntimeError("db exploded")

		port.balance_at = boom
		outcome, client, port, _ = _run(port=port)
		assert outcome == "permanently_rejected" and port.submitted == []
		assert (
			_ack(client)["reason"]["category"] == "other"
			and "db exploded" in _ack(client)["reason"]["message"]
		)

	def test_an_unexpected_failure_after_the_submit_is_reconciliation_required_with_the_document(self):
		port = _Port()

		def boom(*_):
			raise RuntimeError("read back exploded")

		port.read_back = boom
		outcome, client, _, store = _run(port=port)
		assert outcome == "reconciliation_required" and store.recorded == {}
		assert _ack(client)["documentRef"]["name"] == "MAT-RECO-2026-00017"

	def test_a_transient_read_failure_is_failed_transient(self):
		port = _Port()

		def slow(*_):
			raise cf.CountTransient("lock wait")

		port.item_facts = slow
		outcome, _, _, _ = _run(port=port)
		assert outcome == "failed_transient"

	def test_each_distinct_warehouse_is_looked_up_once_per_chunk(self):
		port = _Port()
		calls = []
		original = port.warehouse_problem
		port.warehouse_problem = lambda w, company: calls.append(w) or original(w, company)
		_run(port=port)
		assert calls == [_WH]


class TestWireModels:
	def test_a_count_work_item_parses_without_a_sale(self):
		item = _item()
		assert item.kind == "inventory_count" and item.sale is None and item.count is not None
		assert [(ln.line_seq, ln.item_code, ln.counted_quantity, ln.unit) for ln in item.count.lines] == [
			(1, _PAIR, "7", "Nos"),
			(2, _ITEM2, "0", "Strip"),
		]

	@pytest.mark.parametrize(
		"mutate",
		[
			lambda w: w.update({"sale": {}}),
			lambda w: w.update({"reversalOf": None, "sale": None}) or w["count"].clear(),
			lambda w: w.pop("count"),
			lambda w: w["count"]["lines"].__setitem__(0, {**w["count"]["lines"][0], "countedQuantity": "-1"}),
			lambda w: w["count"]["lines"].__setitem__(
				0, {**w["count"]["lines"][0], "countedQuantity": "1e3"}
			),
			lambda w: w["count"].update({"lines": [_line_wire(i, _PAIR, "1") for i in range(1, 102)]}),
			lambda w: w["count"].update({"lines": []}),
		],
	)
	def test_a_malformed_count_item_is_isolated_as_a_parse_error(self, mutate):
		wire = _wire([_line_wire(1, _PAIR, "7")])
		mutate(wire)
		with pytest.raises((ValueError, KeyError, TypeError)):
			c.PostingWorkItem.from_wire(wire)

	def test_a_sale_kind_item_carrying_count_is_malformed(self):
		wire = _wire([_line_wire(1, _PAIR, "7")])
		wire["kind"] = "sale_post"
		with pytest.raises(ValueError, match="sale"):
			c.PostingWorkItem.from_wire(wire)

	@pytest.mark.parametrize("where", ["line", "body"])
	@pytest.mark.parametrize(
		"field", ["valuationRate", "valuation_rate", "rate", "erpnextBatchRef", "variance"]
	)
	def test_a_supplied_valuation_or_extra_field_is_refused_not_dropped(self, where, field):
		wire = _wire([_line_wire(1, _PAIR, "7")])
		target = wire["count"]["lines"][0] if where == "line" else wire["count"]
		target[field] = "10"
		with pytest.raises(ValueError, match="count contract forbids"):
			c.PostingWorkItem.from_wire(wire)

	def test_a_valuation_rate_is_named_with_its_typed_code(self):
		wire = _wire([_line_wire(1, _PAIR, "7")])
		wire["count"]["lines"][0]["valuationRate"] = "10"
		with pytest.raises(ValueError, match="valuation_rate_forbidden"):
			c.PostingWorkItem.from_wire(wire)

	def test_an_adjusted_line_cannot_ride_a_verified_noop_ack(self):
		with pytest.raises(ValueError, match="never adjusted"):
			c.OutcomeAckRequest.verified_noop((c.CountLineOutcome(1, "adjusted", "7"),))
		rejected = c.CountLineOutcome(
			2, "rejected", None, c.CountRefusal("uom_mismatch", "validation", "unit", 2)
		)
		assert c.OutcomeAckRequest.verified_noop((c.CountLineOutcome(1, "verified_noop", "7"), rejected))

	def test_an_ack_cannot_fake_a_document_for_a_noop(self):
		line = c.CountLineOutcome(1, "verified_noop", "7")
		with pytest.raises(ValueError, match="verified_noop"):
			c.OutcomeAckRequest(
				outcome="verified_noop",
				document_ref=c.ErpnextDocumentRef("Stock Reconciliation", "X"),
				lines=(line,),
			)
		with pytest.raises(ValueError, match="verified_noop"):
			c.OutcomeAckRequest(outcome="verified_noop")
		assert c.OutcomeAckRequest.verified_noop((line,)).to_wire()["outcome"] == "verified_noop"

	def test_the_sale_and_reversal_ack_wire_is_unchanged(self):
		ref = c.ErpnextDocumentRef("Sales Invoice", "ACC-SINV-1")
		assert c.OutcomeAckRequest.posted(ref).to_wire() == {
			"outcome": "posted",
			"documentRef": {"doctype": "Sales Invoice", "name": "ACC-SINV-1"},
		}


class TestRetailUnitThroughTheUomMap:
	"""Backend-Core sends the RETAIL unit (as on a sale); the Connector resolves it through its UOM map."""

	def test_a_retail_unit_that_maps_to_the_stock_uom_posts(self):
		item = _item([_line_wire(1, _PAIR, "7", "each")])  # "each" -> "Nos", the Item stock UOM
		outcome, _, port, _ = _run(item)
		assert outcome == "posted"
		assert port.submitted[0]["items"] == [{"item_code": _PAIR, "warehouse": _WH, "qty": "7"}]

	def test_a_retail_unit_that_maps_to_another_uom_is_refused(self):
		item = _item([_line_wire(1, _PAIR, "7", "each"), _line_wire(2, _ITEM2, "2", "Strip")])
		odd = UomMap({"each": "Box", "Strip": "Strip"})  # "each" maps to Box, but the Item stock UOM is Nos
		outcome, client, port, _ = _run(item, uom_map=odd)
		assert outcome == "posted"
		assert [r["item_code"] for r in port.submitted[0]["items"]] == [_ITEM2]
		refused = [x for x in _ack(client)["lines"] if x["result"] == "rejected"]
		assert refused[0]["lineSeq"] == 1 and refused[0]["refusal"]["code"] == "uom_mismatch"
		assert "maps to 'Box'" in refused[0]["refusal"]["message"]

	def test_an_unmapped_retail_unit_is_refused_not_guessed(self):
		item = _item([_line_wire(1, _PAIR, "7", "case")])
		outcome, client, port, _ = _run(item)
		assert outcome == "permanently_rejected" and port.submitted == []
		assert _ack(client)["lines"][0]["refusal"]["code"] == "uom_mismatch"
		assert "not in the Connector UOM map" in _ack(client)["lines"][0]["refusal"]["message"]
