# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""The ``inventory_count`` posting leg (RT-1422, posting-feed 1.15.0-draft): one chunk -> one native
ERPNext Stock Reconciliation, exactly once, verified, never faked.

Pure orchestration over an injected :class:`CountErpPort` (the Frappe-backed adapter is
``frappe_count_port``; nothing here imports frappe, so every branch is unit-tested locally).

What the leg guarantees (RT-292 comments 17219/17223/17224, Confluence 78938114 G6-2/G6-3/G6-6):

  - the intent is an ABSOLUTE counted quantity per Item and Warehouse, never a Retail variance, and
    never a valuation rate (the payload has no rate field at all);
  - the posting datetime is the work item's frozen Count Cut plus one microsecond, converted to the
    site timezone; there is no fallback to "now";
  - exactly once: the provenance key ``rt_source_system`` / ``rt_external_id`` on the Stock
    Reconciliation is looked up across Draft, submitted AND cancelled documents before anything is
    created. A submitted match is echoed; an in-flight Draft is never resubmitted (``failed_transient``);
    a failed Draft, a cancelled document, several documents or a mismatching document is
    ``reconciliation_required`` and is never re-posted or edited (fail closed);
  - tracked (Batch / serial) Items, a UOM other than the Item's stock UOM, an unknown Item and a
    count-up of an Item with no stock history are refused per line with a typed refusal, and the
    other lines still post (G6-2);
  - lines already at their target are a verified no-op: no document, an explicit ``verified_noop``
    ack with the per-line evidence, never a fabricated ``documentRef``;
  - after the write the Stock Ledger ``qty_after`` of every posted row must equal the target and the
    Bin must equal the target plus the later movements; value and GL convergence are NOT awaited.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import (
	CountLineOutcome,
	CountPosting,
	CountPostingLine,
	CountRefusal,
	ErpnextDocumentRef,
	OutcomeAckRequest,
	PostingWorkItem,
	RejectionReason,
)
from .idempotency import IdempotencyConflict, IdempotencyStore, key_for
from .posting_time import format_time
from .reasons import plain_bounded_message
from .transport import PostingFeedClient
from .uom import UnmappedUnit, UomMap

STOCK_RECONCILIATION = "Stock Reconciliation"

# Refusal code -> the existing RejectionReason.category it is reported as. It mirrors the
# `CountRefusal` allOf of posting-feed 1.15.0-draft; every other code reports `validation`.
_ACK_CATEGORY = {
	"closed_period": "closed_period",
	"unmapped_item": "unmapped_item",
	"back_dated_permission": "other",
	"provenance_conflict": "other",
}


class CountTransient(Exception):
	"""A retryable ERPNext/DB failure (timeout, lock, deadlock): ack ``failed_transient``."""


class CountRefused(Exception):
	"""ERPNext refused the document before anything was created (freeze, permission, validation)."""

	def __init__(self, code: str, message: str) -> None:
		super().__init__(message)
		self.code = code
		self.message = message


class CountDuplicateProvenance(Exception):
	"""The unique provenance index rejected a second document for this key (a race or a crash window)."""


@dataclass(frozen=True)
class ItemFacts:
	"""What the site says about one Item: existence, stock UOM, tracking and stock history."""

	exists: bool
	stock_uom: str | None = None
	tracked: bool = False
	has_history: bool = False


@dataclass(frozen=True)
class ExistingCount:
	"""One Stock Reconciliation found by provenance (Draft, submitted or cancelled)."""

	name: str
	docstatus: int
	posting_date: str
	posting_time: str
	rows: tuple[tuple[str, str, str], ...]  # (item_code, warehouse, qty)
	in_flight: bool = False  # Draft that is locked or has a queued job: someone is submitting it
	failure: str | None = None  # an `Action Failed` comment, for a Draft that is not in flight


@dataclass(frozen=True)
class LineReadBack:
	"""The ledger facts read back for one posted row."""

	item_code: str
	warehouse: str
	qty_after: str  # the count's own Stock Ledger Entry `qty_after_transaction`
	bin_qty: str  # the Bin's `actual_qty` now
	later_movement: str  # net `actual_qty` of ledger entries after the count's own, same Item and Warehouse


@dataclass(frozen=True)
class SiteStamp:
	posting_date: str
	posting_time: str


class CountErpPort(Protocol):
	"""Everything the leg needs from the site. Transient failures raise :class:`CountTransient`."""

	def indexes_ready(self) -> bool: ...

	def company(self) -> str: ...

	def item_facts(self, item_codes: list[str]) -> dict[str, ItemFacts]: ...

	def warehouse_problem(self, warehouse: str, company: str) -> str | None: ...

	def existing_documents(self, source_system: str, external_id: str) -> list[ExistingCount]: ...

	def balance_at(self, item_code: str, warehouse: str, stamp: SiteStamp) -> Decimal: ...

	def submit(self, payload: dict) -> str: ...

	def read_back(self, name: str, rows: list[tuple[str, str]]) -> list[LineReadBack]: ...

	def commit(self) -> None: ...


# ---------------------------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------------------------


def refusal(code: str, message: str, line_seq: int | None = None) -> CountRefusal:
	return CountRefusal(
		code=code,
		ack_category=_ACK_CATEGORY.get(code, "validation"),
		message=plain_bounded_message(message),
		line_seq=line_seq,
	)


def site_stamp(posting_datetime: str, site_tz: str) -> SiteStamp:
	"""The frozen UTC instant as ERPNext's naive site-local ``(posting_date, posting_time)``."""
	if not posting_datetime.endswith("Z"):
		raise ValueError(f"postingDatetime must be UTC (Z), got {posting_datetime!r}")
	instant = datetime.fromisoformat(posting_datetime[:-1] + "+00:00").astimezone(UTC)
	try:
		local = instant.astimezone(ZoneInfo(site_tz))
	except (ZoneInfoNotFoundError, ValueError) as exc:
		raise ValueError(f"unknown ERPNext site timezone {site_tz!r}") from exc
	return SiteStamp(local.date().isoformat(), format_time(local.time()))


def chunk_shape_problem(work_item: PostingWorkItem) -> str | None:
	"""The rules JSON Schema cannot express (posting-feed 1.15.0-draft note), or ``None``."""
	count = work_item.count
	if count is None:
		return "inventory_count work item without count"
	expected = f"count:{count.count_session_ref}"
	if count.chunk_count > 1:
		expected = f"{expected}:{count.chunk}"
	if work_item.external_id.lower() != expected.lower():
		return f"externalId {work_item.external_id!r} is not the provenance key {expected!r}"
	if count.chunk > count.chunk_count:
		return f"chunk {count.chunk} is above chunkCount {count.chunk_count}"
	seqs = [line.line_seq for line in count.lines]
	if len(set(seqs)) != len(seqs):
		return "lineSeq is not unique within the chunk"
	if len({(line.item_code, line.warehouse) for line in count.lines}) != len(count.lines):
		return "the same Item and Warehouse is counted twice in one chunk"
	return None


def line_refusal(
	line: CountPostingLine,
	facts: ItemFacts | None,
	warehouse_problem: str | None,
	mapped_unit: str | None,
) -> CountRefusal | None:
	"""A line-level refusal (the line is excluded, the rest of the chunk still posts), or ``None``.

	``mapped_unit`` is the ERPNext UOM the Retail ``line.unit`` resolves to through the Connector's UOM map
	(``None`` when the unit is unmapped): Backend-Core sends the Retail unit, exactly as on a sale, and only
	the Connector knows the ERP UOM. The count is refused unless that UOM is the Item's stock UOM.
	"""
	seq = line.line_seq
	if facts is None or not facts.exists:
		return refusal("unmapped_item", f"Item {line.item_code!r} does not exist on this site", seq)
	if facts.tracked:
		return refusal("tracked_item_unsupported", f"Item {line.item_code!r} is Batch or serial tracked", seq)
	if mapped_unit is None:
		return refusal("uom_mismatch", f"unit {line.unit!r} is not in the Connector UOM map", seq)
	if facts.stock_uom != mapped_unit:
		return refusal(
			"uom_mismatch",
			f"unit {line.unit!r} maps to {mapped_unit!r}, not the stock UOM {facts.stock_uom!r} of {line.item_code!r}",
			seq,
		)
	if warehouse_problem:
		return refusal("unmapped_bucket", warehouse_problem, seq)
	return None


def history_refusal(line: CountPostingLine, facts: ItemFacts, balance: Decimal) -> CountRefusal | None:
	"""A count-up of an Item with no stock or valuation history cannot post (G6-2)."""
	if Decimal(line.counted_quantity) > balance and not facts.has_history:
		return refusal(
			"no_valuation_history",
			f"count-up of {line.item_code!r} with no stock history cannot post",
			line.line_seq,
		)
	return None


def build_stock_reconciliation(
	count: CountPosting,
	lines: list[CountPostingLine],
	stamp: SiteStamp,
	*,
	company: str,
	source_system: str,
	external_id: str,
) -> dict:
	"""The native Stock Reconciliation payload. There is deliberately no valuation rate."""
	return {
		"doctype": STOCK_RECONCILIATION,
		"purpose": STOCK_RECONCILIATION,
		"company": company,
		"set_posting_time": 1,
		"posting_date": stamp.posting_date,
		"posting_time": stamp.posting_time,
		"rt_source_system": source_system,
		"rt_external_id": external_id,
		"items": [
			{"item_code": line.item_code, "warehouse": line.warehouse, "qty": line.counted_quantity}
			for line in lines
		],
	}


def document_mismatch(existing: ExistingCount, stamp: SiteStamp, lines: list[CountPostingLine]) -> str | None:
	"""Why an existing SUBMITTED document is not this intent, or ``None`` when it matches."""
	if (
		existing.posting_date != stamp.posting_date
		or format_time(existing.posting_time) != stamp.posting_time
	):
		return (
			f"{existing.name} is stamped {existing.posting_date} {existing.posting_time}, not the frozen cut"
		)
	by_key = {(line.item_code, line.warehouse): Decimal(line.counted_quantity) for line in lines}
	for item, warehouse, qty in existing.rows:
		target = by_key.get((item, warehouse))
		if target is None:
			return f"{existing.name} holds a row ({item}, {warehouse}) that this intent does not count"
		if Decimal(qty) != target:
			return f"{existing.name} sets {item} at {warehouse} to {qty}, the intent counts {target}"
	return None


@dataclass(frozen=True)
class Decision:
	"""What an existing-document lookup means: ``proceed``, ``echo``, ``in_flight`` or ``reconcile``."""

	action: str
	document: ExistingCount | None = None
	message: str = ""


def classify_existing(docs: list[ExistingCount], stamp: SiteStamp, lines: list[CountPostingLine]) -> Decision:
	"""Draft-aware, fail-closed: only an empty lookup lets a new document be created."""
	if not docs:
		return Decision("proceed")
	submitted = [d for d in docs if d.docstatus == 1]
	if len(submitted) > 1:
		return Decision("reconcile", submitted[0], f"{len(submitted)} submitted documents carry this key")
	if submitted:
		why = document_mismatch(submitted[0], stamp, lines)
		return Decision("reconcile", submitted[0], why) if why else Decision("echo", submitted[0])
	drafts = [d for d in docs if d.docstatus == 0]
	if len(docs) > 1:
		return Decision("reconcile", docs[0], f"{len(docs)} documents carry this key")
	return _single_unsubmitted(docs[0], drafts)


def _single_unsubmitted(doc: ExistingCount, drafts: list[ExistingCount]) -> Decision:
	if doc.docstatus == 2:
		return Decision("reconcile", doc, f"{doc.name} is cancelled; the outcome is unknown, never re-posted")
	if drafts and doc.in_flight:
		return Decision("in_flight", doc)
	reason = f": {doc.failure}" if doc.failure else ""
	return Decision(
		"reconcile", doc, f"{doc.name} is a Draft that is not in flight and did not submit{reason}"
	)


def verification_problem(posted: list[CountPostingLine], read_back: list[LineReadBack]) -> str | None:
	"""Ledger ``qty_after`` equals the target and the Bin equals target plus later movements."""
	seen = {(r.item_code, r.warehouse): r for r in read_back}
	for line in posted:
		row = seen.get((line.item_code, line.warehouse))
		target = Decimal(line.counted_quantity)
		if row is None:
			return f"no Stock Ledger Entry read back for {line.item_code} at {line.warehouse}"
		if Decimal(row.qty_after) != target:
			return f"{line.item_code} at {line.warehouse}: qty_after {row.qty_after}, expected {target}"
		if Decimal(row.bin_qty) != target + Decimal(row.later_movement):
			return (
				f"{line.item_code} at {line.warehouse}: Bin {row.bin_qty}, expected "
				f"{target} plus later movements {row.later_movement}"
			)
	return None


# ---------------------------------------------------------------------------------------------
# Acks
# ---------------------------------------------------------------------------------------------


def _ack_key(work_item: PostingWorkItem, outcome: str) -> str:
	"""The same per-outcome Idempotency-Key rules as the sale leg (RT-171, RT-333)."""
	if outcome == "failed_transient":
		return f"{work_item.work_item_ref}:failed_transient:{work_item.item_cursor}"
	if work_item.resolution_version is not None:
		return f"{work_item.work_item_ref}:{outcome}:v{work_item.resolution_version}"
	return f"{work_item.work_item_ref}:{outcome}"


class _Acker:
	"""Sends the typed acks of one work item (the per-outcome key and version echo included)."""

	def __init__(self, client: PostingFeedClient, work_item: PostingWorkItem, port: CountErpPort) -> None:
		self._client, self._item, self._port = client, work_item, port
		self.created: ErpnextDocumentRef | None = None  # set once this run has submitted a document

	def _send(self, ack: OutcomeAckRequest, outcome: str, *, commit: bool = False) -> str:
		if commit:
			self._port.commit()  # RT-458: the ERP effects are durable before Backend-Core hears of them
		self._client.ack_outcome(self._item.work_item_ref, ack, idempotency_key=_ack_key(self._item, outcome))
		return outcome

	def posted(self, ref: ErpnextDocumentRef, lines: tuple[CountLineOutcome, ...]) -> str:
		version = self._item.resolution_version
		return self._send(
			OutcomeAckRequest.posted(ref, resolution_version=version, lines=lines), "posted", commit=True
		)

	def verified_noop(self, lines: tuple[CountLineOutcome, ...]) -> str:
		ack = OutcomeAckRequest.verified_noop(lines, resolution_version=self._item.resolution_version)
		return self._send(ack, "verified_noop")

	def transient(self) -> str:
		ack = OutcomeAckRequest.failed_transient(resolution_version=self._item.resolution_version)
		return self._send(ack, "failed_transient")

	def rejected(self, category: str, message: str, lines: tuple[CountLineOutcome, ...] | None = None) -> str:
		reason = RejectionReason(category=category, message=plain_bounded_message(message))
		version = self._item.resolution_version
		ack = OutcomeAckRequest.permanently_rejected(reason, resolution_version=version, lines=lines)
		return self._send(ack, "permanently_rejected")

	def reconcile(self, ref: ErpnextDocumentRef, message: str) -> str:
		reason = RejectionReason(category="other", message=plain_bounded_message(message))
		ack = OutcomeAckRequest.reconciliation_required(
			ref, reason, resolution_version=self._item.resolution_version
		)
		return self._send(ack, "reconciliation_required", commit=True)


# ---------------------------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Plan:
	"""The lines of a chunk after the line-level rules: what to post, what is a no-op, what is refused."""

	to_post: list[CountPostingLine]
	noop: list[CountPostingLine]
	refused: dict[int, CountRefusal]


def _rejected_outcomes(plan: _Plan) -> list[CountLineOutcome]:
	return [
		CountLineOutcome(line_seq=seq, result="rejected", verified_qty_after=None, refusal=ref)
		for seq, ref in sorted(plan.refused.items())
	]


def _line_outcomes(plan: _Plan, *, adjusted: bool) -> tuple[CountLineOutcome, ...]:
	"""Per-line evidence. ``verifiedQtyAfter`` is the target itself, so a replay builds the same body."""
	posted = [
		CountLineOutcome(line.line_seq, "adjusted" if adjusted else "verified_noop", line.counted_quantity)
		for line in (plan.to_post if adjusted else [])
	]
	noops = [CountLineOutcome(line.line_seq, "verified_noop", line.counted_quantity) for line in plan.noop]
	return tuple(sorted([*posted, *noops, *_rejected_outcomes(plan)], key=lambda o: o.line_seq))


def _mapped_unit(uom_map: UomMap, unit: str) -> str | None:
	try:
		return uom_map.resolve(unit)
	except UnmappedUnit:
		return None


def _plan_lines(
	count: CountPosting, port: CountErpPort, stamp: SiteStamp, company: str, uom_map: UomMap
) -> _Plan:
	facts = port.item_facts(sorted({line.item_code for line in count.lines}))
	to_post: list[CountPostingLine] = []
	noop: list[CountPostingLine] = []
	refused: dict[int, CountRefusal] = {}
	warehouse_problems = {
		w: port.warehouse_problem(w, company) for w in sorted({x.warehouse for x in count.lines})
	}
	for line in count.lines:
		problem = line_refusal(
			line,
			facts.get(line.item_code),
			warehouse_problems[line.warehouse],
			_mapped_unit(uom_map, line.unit),
		)
		if problem is None:
			balance = port.balance_at(line.item_code, line.warehouse, stamp)
			problem = history_refusal(line, facts[line.item_code], balance)
			if problem is None:
				(noop if Decimal(line.counted_quantity) == balance else to_post).append(line)
		if problem is not None:
			refused[line.line_seq] = problem
	return _Plan(to_post=to_post, noop=noop, refused=refused)


def _all_refused(acker: _Acker, plan: _Plan) -> str:
	first = next(iter(plan.refused.values()))
	message = f"every counted line was refused; first: {first.code}: {first.message}"
	return acker.rejected(first.ack_category, message, tuple(_rejected_outcomes(plan)))


def post_count_work_item(
	work_item: PostingWorkItem,
	*,
	client: PostingFeedClient,
	store: IdempotencyStore,
	port: CountErpPort,
	site_tz: str,
	uom_map: UomMap,
) -> str:
	"""Post one ``inventory_count`` chunk and ack the outcome. Returns the outcome string."""
	acker = _Acker(client, work_item, port)
	try:
		return _post(work_item, acker, store, port, (site_tz, uom_map))
	except CountTransient:
		return acker.transient()
	except CountRefused as exc:
		return acker.rejected(_ACK_CATEGORY.get(exc.code, "validation"), f"{exc.code}: {exc.message}")
	except Exception as exc:
		# Terminal-outcome invariant (as the sale leg): an unclassified failure never escapes to abort the
		# page. After a successful submit the document exists, so it is reconciled; before, it is rejected.
		message = f"{type(exc).__name__}: {exc}"
		if acker.created is not None:
			return acker.reconcile(
				acker.created, f"unexpected failure after the document was submitted: {message}"
			)
		return acker.rejected("other", f"unexpected failure before anything was posted: {message}")


def _post(
	work_item: PostingWorkItem,
	acker: _Acker,
	store: IdempotencyStore,
	port: CountErpPort,
	env: tuple[str, UomMap],
) -> str:
	count = work_item.count
	assert count is not None  # parse guarantees it for inventory_count
	problem = chunk_shape_problem(work_item)
	if problem:
		return acker.rejected("validation", f"invalid_shape: {problem}")
	try:
		stamp = site_stamp(count.posting_datetime, env[0])
	except ValueError as exc:
		return acker.rejected("validation", f"invalid_shape: {exc}")
	if not port.indexes_ready():
		return acker.transient()  # the unique provenance index is absent: pause, never post unguarded
	company = port.company()
	plan = _plan_lines(count, port, stamp, company, env[1])
	return _decide(work_item, acker, store, port, (count, stamp, plan, company))


def _decide(
	work_item: PostingWorkItem,
	acker: _Acker,
	store: IdempotencyStore,
	port: CountErpPort,
	ctx: tuple[CountPosting, SiteStamp, _Plan, str],
) -> str:
	count, stamp, plan, company = ctx
	accepted = [*plan.to_post, *plan.noop]
	docs = port.existing_documents(work_item.source_system, work_item.external_id)
	decision = classify_existing(docs, stamp, accepted)
	if decision.action != "proceed":
		return _existing(work_item, acker, store, port, decision, plan)
	if not accepted:
		return _all_refused(acker, plan)
	if not plan.to_post:
		return acker.verified_noop(_line_outcomes(plan, adjusted=False))
	return _create(work_item, acker, store, port, (count, stamp, plan, company))


def _existing(
	work_item: PostingWorkItem,
	acker: _Acker,
	store: IdempotencyStore,
	port: CountErpPort,
	decision: Decision,
	plan: _Plan,
) -> str:
	doc = decision.document
	assert doc is not None or decision.action == "proceed"
	if decision.action == "in_flight":
		return acker.transient()  # never resubmit a Draft someone is already submitting
	ref = ErpnextDocumentRef(doctype=STOCK_RECONCILIATION, name=doc.name)  # type: ignore[union-attr]
	if decision.action == "reconcile":
		return acker.reconcile(ref, decision.message)
	return _echo(work_item, acker, store, port, ref, doc, plan)  # type: ignore[arg-type]


def _echo(
	work_item: PostingWorkItem,
	acker: _Acker,
	store: IdempotencyStore,
	port: CountErpPort,
	ref: ErpnextDocumentRef,
	doc: ExistingCount,
	plan: _Plan,
) -> str:
	"""A matching submitted document is confirmed ONLY after the ledger is read back again.

	The first run may have died after the submit and before its verification or ack (a crash window),
	or its `reconciliation_required` ack may have been lost: an echo must never turn either into a
	`posted`. The body is unchanged when the read-back passes, so the Idempotency-Key never meets a
	different body.
	"""
	posted_keys = {(r[0], r[1]) for r in doc.rows}
	in_doc = [line for line in [*plan.to_post, *plan.noop] if (line.item_code, line.warehouse) in posted_keys]
	problem = verification_problem(in_doc, port.read_back(doc.name, sorted(posted_keys)))
	if problem:
		return acker.reconcile(ref, f"verification failed on the existing document: {problem}")
	_record(store, work_item, ref)
	return acker.posted(ref, _echo_outcomes(plan, posted_keys))


def _echo_outcomes(plan: _Plan, posted_keys: set[tuple[str, str]]) -> tuple[CountLineOutcome, ...]:
	"""A replay's lines match the first run's: rows in the document are adjusted, the rest were no-ops."""
	in_doc = [line for line in [*plan.to_post, *plan.noop] if (line.item_code, line.warehouse) in posted_keys]
	rest = [
		line for line in [*plan.to_post, *plan.noop] if (line.item_code, line.warehouse) not in posted_keys
	]
	adjusted = [CountLineOutcome(line.line_seq, "adjusted", line.counted_quantity) for line in in_doc]
	noops = [CountLineOutcome(line.line_seq, "verified_noop", line.counted_quantity) for line in rest]
	return tuple(sorted([*adjusted, *noops, *_rejected_outcomes(plan)], key=lambda o: o.line_seq))


def _record(store: IdempotencyStore, work_item: PostingWorkItem, ref: ErpnextDocumentRef) -> None:
	try:
		store.record_posted(key_for(work_item), ref)
	except IdempotencyConflict:
		pass  # another worker recorded the same key first; the provenance lookup is the authority


def _create(
	work_item: PostingWorkItem,
	acker: _Acker,
	store: IdempotencyStore,
	port: CountErpPort,
	ctx: tuple[CountPosting, SiteStamp, _Plan, str],
) -> str:
	count, stamp, plan, company = ctx
	payload = build_stock_reconciliation(
		count,
		plan.to_post,
		stamp,
		company=company,
		source_system=work_item.source_system,
		external_id=work_item.external_id,
	)
	try:
		name = port.submit(payload)
	except CountDuplicateProvenance:
		# A race or a crash window: a document with this key exists now. Re-classify, never resubmit.
		docs = port.existing_documents(work_item.source_system, work_item.external_id)
		decision = classify_existing(docs, stamp, [*plan.to_post, *plan.noop])
		if decision.action == "proceed":
			return acker.transient()  # the index fired but the lookup sees nothing yet: retry later
		return _existing(work_item, acker, store, port, decision, plan)
	ref = ErpnextDocumentRef(doctype=STOCK_RECONCILIATION, name=name)
	acker.created = ref
	read_back = port.read_back(name, [(line.item_code, line.warehouse) for line in plan.to_post])
	problem = verification_problem(plan.to_post, read_back)
	if problem:
		return acker.reconcile(ref, f"verification failed: {problem}")
	_record(store, work_item, ref)
	return acker.posted(ref, _line_outcomes(plan, adjusted=True))
