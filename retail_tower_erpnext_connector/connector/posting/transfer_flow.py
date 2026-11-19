# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""The ``transfer_dispatch`` and ``transfer_receive`` posting legs (RT-1514, posting-feed 1.17.0-draft):
ERPNext's native goods-in-transit pair, exactly once, verified, never faked.

Pure orchestration over an injected :class:`TransferErpPort` (the Frappe-backed adapter is
``frappe_transfer_port``; nothing here imports frappe, so every branch is unit-tested locally).

What the legs guarantee (RT-291 planning record, RT-291 comments 17800 and 17801; Gate 8 G8-1..G8-6):

  - the dispatch is ONE Stock Entry (Material Transfer, add to transit) from the source store warehouse to
    the tenant transit warehouse; each receipt is ONE further Material Transfer from the transit warehouse
    to the destination warehouse against the dispatch Stock Entry, linked row by row (``ste_detail``), so
    several partial receipts post against one dispatch (ERPNext's own ``transferred_qty`` guard);
  - a Stock Entry is one document, so a transfer leg is all or nothing: one bad line, warehouse or link
    refuses the WHOLE leg before anything is written (``permanently_rejected``), never a partial post;
  - the posting datetime is the work item's frozen instant converted to the site timezone, never "now";
  - exactly once: the provenance key ``rt_source_system`` / ``rt_external_id`` on the Stock Entry is looked
    up across Draft, submitted AND cancelled documents before anything is created. A submitted match is
    echoed only after the ledger is read back again; an in-flight Draft is never resubmitted; a failed Draft,
    a cancelled document, several documents or a mismatching document is ``reconciliation_required`` and is
    never re-posted or edited (fail closed);
  - tracked items: a serial Item is refused; a Batch Item posts only with the Batch the feed names, and an
    untracked Item posts only without one (the Connector never guesses a Batch, as for a sale);
  - after the write the Stock Ledger must show exactly the intent (dispatch: minus at the source and plus at
    transit; receipt: minus at transit and plus at the destination; the same Batch on both sides). A
    mismatch on a persisted document is ``reconciliation_required`` with that document, never a false
    ``posted`` (G8-4).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from .contracts import (
	ErpnextDocumentRef,
	OutcomeAckRequest,
	PostingWorkItem,
	RejectionReason,
	TransferDispatchPosting,
	TransferReceivePosting,
)
from .count_flow import (
	CountDuplicateProvenance,
	CountRefused,
	CountTransient,
	Decision,
	SiteStamp,
	site_stamp,
)
from .idempotency import IdempotencyConflict, IdempotencyStore, key_for
from .posting_time import format_time
from .reasons import plain_bounded_message
from .transport import PostingFeedClient
from .uom import UnmappedUnit, UomMap

STOCK_ENTRY = "Stock Entry"
MATERIAL_TRANSFER = "Material Transfer"

# The typed errors of an ERP leg are the same three as the count leg's: a retryable failure, an ERPNext
# refusal before anything was created, and the unique provenance index rejecting a second document.
TransferTransient = CountTransient
TransferRefused = CountRefused
TransferDuplicateProvenance = CountDuplicateProvenance

# Refusal code -> the existing RejectionReason.category it is reported as; every other code is `validation`.
_ACK_CATEGORY = {
	"closed_period": "closed_period",
	"unmapped_item": "unmapped_item",
	"back_dated_permission": "other",
	"provenance_conflict": "other",
}


@dataclass(frozen=True)
class TransferItemFacts:
	"""What the site says about one Item: existence, stock UOM and tracking."""

	exists: bool
	stock_uom: str | None = None
	has_batch_no: bool = False
	has_serial_no: bool = False


@dataclass(frozen=True)
class TransferRow:
	"""One Stock Entry row, as intended or as found: Item, Batch (or None), quantity and warehouses."""

	item_code: str
	batch: str | None
	qty: str
	s_warehouse: str
	t_warehouse: str


@dataclass(frozen=True)
class ExistingTransfer:
	"""One Stock Entry found by provenance (Draft, submitted or cancelled)."""

	name: str
	docstatus: int
	purpose: str
	add_to_transit: bool
	outgoing_stock_entry: str | None
	posting_date: str
	posting_time: str
	rows: tuple[TransferRow, ...]
	in_flight: bool = False  # Draft that is locked or has a queued job: someone is submitting it
	failure: str | None = None  # an `Action Failed` comment, for a Draft that is not in flight


@dataclass(frozen=True)
class DispatchRow:
	"""One row of the dispatch Stock Entry as the receipt needs it."""

	idx: int
	row_name: str  # the Stock Entry Detail name: the `ste_detail` a receipt row links to
	item_code: str
	batch: str | None
	qty: str
	transferred_qty: str  # what submitted receipts already took
	t_warehouse: str
	line_ref: str | None  # the `rt_transfer_line_ref` stored on the row at dispatch


@dataclass(frozen=True)
class DispatchFacts:
	"""The dispatch Stock Entry a receipt names."""

	name: str
	docstatus: int
	purpose: str
	company: str
	add_to_transit: bool
	rows: tuple[DispatchRow, ...]


@dataclass(frozen=True)
class LedgerRow:
	"""One Stock Ledger Entry of a posted document: Item, Warehouse, signed quantity and Batch (or None)."""

	item_code: str
	warehouse: str
	actual_qty: str
	batch: str | None


class TransferErpPort(Protocol):
	"""Everything the legs need from the site. Transient failures raise :class:`TransferTransient`."""

	def indexes_ready(self) -> bool: ...

	def company(self) -> str: ...

	def item_facts(self, item_codes: list[str]) -> dict[str, TransferItemFacts]: ...

	def batch_problem(self, item_code: str, batch: str, stamp: SiteStamp) -> str | None: ...

	def warehouse_problem(self, warehouse: str, company: str) -> str | None: ...

	def transit_warehouse_problem(self, warehouse: str, company: str) -> str | None: ...

	def existing_documents(self, source_system: str, external_id: str) -> list[ExistingTransfer]: ...

	def dispatch_document(self, name: str) -> DispatchFacts | None: ...

	def submit(self, payload: dict) -> str: ...

	def read_back(self, name: str) -> list[LedgerRow]: ...

	def commit(self) -> None: ...


# ---------------------------------------------------------------------------------------------
# What one leg intends
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Leg:
	"""One posting intent.

	``rows``, ``add_to_transit`` and ``outgoing`` are derived from the work item ALONE: they are what the lookup of
	an existing document and the ledger verification compare against, so a document that already exists is judged
	without touching the live site state it was posted against (a receipt's own submit already moved the dispatch
	row's ``transferred_qty``; a warehouse may since have been frozen). ``build`` is the part that needs the live
	site: it validates (raising the typed refusal) and then returns the payload. It runs only when a NEW document is
	about to be created.
	"""

	rows: tuple[TransferRow, ...]
	add_to_transit: bool
	outgoing: str | None
	build: Callable[
		[str, SiteStamp, str, str], dict
	]  # (company, stamp, source_system, external_id) -> payload


# ---------------------------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------------------------


def _refuse(code: str, message: str) -> TransferRefused:
	return TransferRefused(code, message)


def _dispatch_shape_problem(work_item: PostingWorkItem, body: TransferDispatchPosting) -> str | None:
	"""The rules JSON Schema cannot express (posting-feed 1.17.0-draft note), or ``None``."""
	expected = f"transfer_dispatch:{body.transfer_ref}"
	if work_item.external_id.lower() != expected.lower():
		return f"externalId {work_item.external_id!r} is not the provenance key {expected!r}"
	seqs = sorted(line.line_seq for line in body.lines)
	if seqs != list(range(1, len(body.lines) + 1)):
		return "lineSeq must be unique and contiguous from 1"
	if len({line.transfer_line_ref for line in body.lines}) != len(body.lines):
		return "transferLineRef is not unique within the dispatch"
	if body.source_warehouse == body.transit_warehouse:
		return "the source and the transit warehouse are the same"
	return None


def _receive_shape_problem(work_item: PostingWorkItem, body: TransferReceivePosting) -> str | None:
	expected = f"transfer_receipt:{body.receipt_ref}"
	if work_item.external_id.lower() != expected.lower():
		return f"externalId {work_item.external_id!r} is not the provenance key {expected!r}"
	seqs = sorted(line.line_seq for line in body.lines)
	if seqs != list(range(1, len(body.lines) + 1)):
		return "lineSeq must be unique and contiguous from 1"
	if body.destination_warehouse == body.transit_warehouse:
		return "the destination and the transit warehouse are the same"
	return None


def _mapped_unit(uom_map: UomMap, unit: str) -> str | None:
	try:
		return uom_map.resolve(unit)
	except UnmappedUnit:
		return None


def _item_problem(code: str, facts: TransferItemFacts | None, batch: str | None, unit: str, uom_map: UomMap):
	"""Raise the typed refusal of one line, or return. Order: Item, tracking, unit."""
	if facts is None or not facts.exists:
		raise _refuse("unmapped_item", f"Item {code!r} does not exist on this site")
	if facts.has_serial_no:
		raise _refuse(
			"serial_unsupported", f"Item {code!r} is serial tracked; serial transfers are not supported"
		)
	if facts.has_batch_no and batch is None:
		raise _refuse("batch_required", f"Item {code!r} is Batch tracked and needs a Batch on the line")
	if not facts.has_batch_no and batch is not None:
		raise _refuse(
			"batch_unexpected", f"Item {code!r} is not Batch tracked but the line names Batch {batch!r}"
		)
	mapped = _mapped_unit(uom_map, unit)
	if mapped is None:
		raise _refuse("uom_mismatch", f"unit {unit!r} is not in the Connector UOM map")
	if facts.stock_uom != mapped:
		raise _refuse(
			"uom_mismatch",
			f"unit {unit!r} maps to {mapped!r}, not the stock UOM {facts.stock_uom!r} of {code!r}",
		)


def _row_payload(row: TransferRow, uom: str | None, extra: dict) -> dict:
	payload = {
		"item_code": row.item_code,
		"s_warehouse": row.s_warehouse,
		"t_warehouse": row.t_warehouse,
		"qty": row.qty,
		"uom": uom,
		"conversion_factor": 1,
		**extra,
	}
	if row.batch is not None:
		payload["batch_no"] = row.batch
		payload["use_serial_batch_fields"] = 1
	return payload


def _header(company: str, stamp: SiteStamp, source_system: str, external_id: str) -> dict:
	return {
		"doctype": STOCK_ENTRY,
		"stock_entry_type": MATERIAL_TRANSFER,
		"purpose": MATERIAL_TRANSFER,
		"company": company,
		"set_posting_time": 1,
		"posting_date": stamp.posting_date,
		"posting_time": stamp.posting_time,
		"rt_source_system": source_system,
		"rt_external_id": external_id,
	}


def _dispatch_leg(body: TransferDispatchPosting, port: TransferErpPort, uom_map: UomMap) -> _Leg:
	# Rows follow ``lineSeq``: the contract says it IS the dispatch Stock Entry row ``idx``, which a receipt names.
	lines = sorted(body.lines, key=lambda line: line.line_seq)
	rows = tuple(
		TransferRow(line.item_code, line.batch, line.quantity, body.source_warehouse, body.transit_warehouse)
		for line in lines
	)

	def build(company: str, stamp: SiteStamp, source_system: str, external_id: str) -> dict:
		facts = port.item_facts(sorted({line.item_code for line in lines}))
		for line in lines:
			_item_problem(line.item_code, facts.get(line.item_code), line.batch, line.unit, uom_map)
			if line.batch is not None:
				problem = port.batch_problem(line.item_code, line.batch, stamp)
				if problem:
					raise _refuse("batch_invalid", f"Batch {line.batch!r} of {line.item_code!r}: {problem}")
		problem = port.warehouse_problem(body.source_warehouse, company)
		if problem:
			raise _refuse("unmapped_bucket", f"source warehouse {body.source_warehouse!r}: {problem}")
		problem = port.transit_warehouse_problem(body.transit_warehouse, company)
		if problem:
			raise _refuse("unmapped_bucket", f"transit warehouse {body.transit_warehouse!r}: {problem}")
		doc = _header(company, stamp, source_system, external_id)
		doc.update(
			add_to_transit=1,
			from_warehouse=body.source_warehouse,
			to_warehouse=body.transit_warehouse,
			items=[
				_row_payload(
					row,
					facts[line.item_code].stock_uom,
					{"rt_transfer_line_ref": line.transfer_line_ref},
				)
				for row, line in zip(rows, lines, strict=True)
			],
		)
		return doc

	return _Leg(rows=rows, add_to_transit=True, outgoing=None, build=build)


def _dispatch_problem(dispatch: DispatchFacts | None, name: str, company: str) -> None:
	"""Raise the typed refusal of the dispatch document a receipt names, or return."""
	if dispatch is None:
		raise _refuse("dispatch_document_missing", f"dispatch document {name!r} was not found on this site")
	if dispatch.docstatus != 1:
		raise _refuse(
			"dispatch_document_invalid",
			f"dispatch document {name!r} is not submitted (docstatus {dispatch.docstatus})",
		)
	if dispatch.purpose != MATERIAL_TRANSFER:
		raise _refuse("dispatch_document_invalid", f"dispatch document {name!r} is not a Material Transfer")
	if not dispatch.add_to_transit:
		raise _refuse(
			"dispatch_document_invalid", f"dispatch document {name!r} was not made with add to transit"
		)
	if dispatch.company != company:
		raise _refuse(
			"dispatch_document_invalid",
			f"dispatch document {name!r} belongs to company {dispatch.company!r}, not {company!r}",
		)


def _link_problem(line, row: DispatchRow | None, transit: str) -> None:
	"""Raise the typed refusal of one receipt line's link to its dispatch row, or return."""
	seq = line.dispatch_line_seq
	if row is None:
		raise _refuse("dispatch_row_mismatch", f"dispatch row {seq} does not exist on the dispatch document")
	if row.item_code != line.item_code:
		raise _refuse(
			"dispatch_row_mismatch",
			f"receipt item {line.item_code!r} is not the item {row.item_code!r} of dispatch row {seq}",
		)
	if row.batch != line.batch:
		raise _refuse(
			"dispatch_row_mismatch",
			f"receipt batch {line.batch!r} is not the batch {row.batch!r} of dispatch row {seq}",
		)
	if row.line_ref is None or row.line_ref != line.transfer_line_ref:
		raise _refuse(
			"dispatch_row_mismatch",
			f"dispatch row {seq} carries transferLineRef {row.line_ref!r}, the receipt line names "
			f"{line.transfer_line_ref!r}",
		)
	if row.t_warehouse != transit:
		raise _refuse(
			"dispatch_row_mismatch",
			f"dispatch row {seq} moved stock into {row.t_warehouse!r}, not the transit warehouse {transit!r}",
		)


def _remaining_problem(body: TransferReceivePosting, by_idx: dict[int, DispatchRow]) -> None:
	taken: dict[int, Decimal] = defaultdict(Decimal)
	for line in body.lines:
		taken[line.dispatch_line_seq] += Decimal(line.quantity)
	for seq, total in sorted(taken.items()):
		row = by_idx[seq]
		remaining = Decimal(row.qty) - Decimal(row.transferred_qty)
		if total > remaining:
			raise _refuse(
				"over_receipt",
				f"over-receipt: the receipt takes {total} of dispatch row {seq}, only {remaining} remains",
			)


def _receive_leg(body: TransferReceivePosting, port: TransferErpPort, uom_map: UomMap) -> _Leg:
	rows = tuple(
		TransferRow(
			line.item_code, line.batch, line.quantity, body.transit_warehouse, body.destination_warehouse
		)
		for line in body.lines
	)

	def build(company: str, stamp: SiteStamp, source_system: str, external_id: str) -> dict:
		dispatch = port.dispatch_document(body.dispatch_document)
		_dispatch_problem(dispatch, body.dispatch_document, company)
		assert dispatch is not None
		facts = port.item_facts(sorted({line.item_code for line in body.lines}))
		by_idx = {row.idx: row for row in dispatch.rows}
		for line in body.lines:
			_item_problem(line.item_code, facts.get(line.item_code), line.batch, line.unit, uom_map)
			_link_problem(line, by_idx.get(line.dispatch_line_seq), body.transit_warehouse)
		_remaining_problem(body, by_idx)
		problem = port.warehouse_problem(body.destination_warehouse, company)
		if problem:
			raise _refuse(
				"unmapped_bucket", f"destination warehouse {body.destination_warehouse!r}: {problem}"
			)
		problem = port.transit_warehouse_problem(body.transit_warehouse, company)
		if problem:
			raise _refuse("unmapped_bucket", f"transit warehouse {body.transit_warehouse!r}: {problem}")
		doc = _header(company, stamp, source_system, external_id)
		doc.update(
			outgoing_stock_entry=dispatch.name,
			from_warehouse=body.transit_warehouse,
			to_warehouse=body.destination_warehouse,
			items=[
				_row_payload(
					row,
					facts[line.item_code].stock_uom,
					{
						"ste_detail": by_idx[line.dispatch_line_seq].row_name,
						"against_stock_entry": dispatch.name,
						"rt_transfer_line_ref": line.transfer_line_ref,
					},
				)
				for row, line in zip(rows, body.lines, strict=True)
			],
		)
		return doc

	return _Leg(rows=rows, add_to_transit=False, outgoing=body.dispatch_document, build=build)


def _row_keys(rows: tuple[TransferRow, ...]) -> list[tuple[str, str, str, str, Decimal]]:
	return sorted((r.item_code, r.batch or "", r.s_warehouse, r.t_warehouse, Decimal(r.qty)) for r in rows)


def document_mismatch(existing: ExistingTransfer, leg: _Leg, stamp: SiteStamp) -> str | None:
	"""Why an existing SUBMITTED document is not this intent, or ``None`` when it matches."""
	if (
		existing.posting_date != stamp.posting_date
		or format_time(existing.posting_time) != stamp.posting_time
	):
		return f"{existing.name} is stamped {existing.posting_date} {existing.posting_time}, not the frozen posting time"
	if existing.purpose != MATERIAL_TRANSFER:
		return f"{existing.name} has purpose {existing.purpose!r}, not {MATERIAL_TRANSFER!r}"
	if bool(existing.add_to_transit) != leg.add_to_transit:
		return f"{existing.name} add to transit is {bool(existing.add_to_transit)}, the intent expects {leg.add_to_transit}"
	if (existing.outgoing_stock_entry or None) != leg.outgoing:
		return f"{existing.name} is made against {existing.outgoing_stock_entry!r}, the intent is against {leg.outgoing!r}"
	if _row_keys(existing.rows) != _row_keys(leg.rows):
		return f"{existing.name} rows differ from the intent"
	return None


def classify_existing(docs: list[ExistingTransfer], leg: _Leg, stamp: SiteStamp) -> Decision:
	"""Draft-aware, fail-closed: only an empty lookup lets a new document be created."""
	if not docs:
		return Decision("proceed")
	submitted = [d for d in docs if d.docstatus == 1]
	if len(submitted) > 1:
		return Decision("reconcile", submitted[0], f"{len(submitted)} submitted documents carry this key")  # type: ignore[arg-type]
	if submitted:
		why = document_mismatch(submitted[0], leg, stamp)
		return Decision("reconcile", submitted[0], why) if why else Decision("echo", submitted[0])  # type: ignore[arg-type]
	if len(docs) > 1:
		return Decision("reconcile", docs[0], f"{len(docs)} documents carry this key")  # type: ignore[arg-type]
	doc = docs[0]
	if doc.docstatus == 2:
		return Decision("reconcile", doc, f"{doc.name} is cancelled; the outcome is unknown, never re-posted")  # type: ignore[arg-type]
	if doc.in_flight:
		return Decision("in_flight", doc)  # type: ignore[arg-type]
	reason = f": {doc.failure}" if doc.failure else ""
	return Decision(
		"reconcile",
		doc,  # type: ignore[arg-type]
		f"{doc.name} is a Draft that is not in flight and did not submit{reason}",
	)


def verification_problem(leg: _Leg, ledger: list[LedgerRow]) -> str | None:
	"""The ledger shows exactly the intent: minus at each source and plus at each target, the same Batch."""
	expected: dict[tuple[str, str, str | None], Decimal] = defaultdict(Decimal)
	for row in leg.rows:
		qty = Decimal(row.qty)
		expected[(row.item_code, row.s_warehouse, row.batch)] -= qty
		expected[(row.item_code, row.t_warehouse, row.batch)] += qty
	actual: dict[tuple[str, str, str | None], Decimal] = defaultdict(Decimal)
	for entry in ledger:
		actual[(entry.item_code, entry.warehouse, entry.batch)] += Decimal(entry.actual_qty)
	for key in sorted(set(expected) | set(actual), key=str):
		want, got = expected.get(key), actual.get(key)
		if want is None:
			return f"unexpected Stock Ledger Entry for {key[0]} at {key[1]} (batch {key[2]})"
		if got is None:
			return f"no Stock Ledger Entry read back for {key[0]} at {key[1]} (batch {key[2]})"
		if got != want:
			return f"{key[0]} at {key[1]} (batch {key[2]}): ledger moved {got}, expected {want}"
	return None


# ---------------------------------------------------------------------------------------------
# Acks
# ---------------------------------------------------------------------------------------------


def _ack_key(work_item: PostingWorkItem, outcome: str) -> str:
	"""The same per-outcome Idempotency-Key rules as the sale and count legs (RT-171, RT-333)."""
	if outcome == "failed_transient":
		return f"{work_item.work_item_ref}:failed_transient:{work_item.item_cursor}"
	if work_item.resolution_version is not None:
		return f"{work_item.work_item_ref}:{outcome}:v{work_item.resolution_version}"
	return f"{work_item.work_item_ref}:{outcome}"


class _Acker:
	"""Sends the typed acks of one work item (the per-outcome key and version echo included)."""

	def __init__(self, client: PostingFeedClient, work_item: PostingWorkItem, port: TransferErpPort) -> None:
		self._client, self._item, self._port = client, work_item, port
		self.created: ErpnextDocumentRef | None = None  # set once this run has submitted a document

	def _send(self, ack: OutcomeAckRequest, outcome: str, *, commit: bool = False) -> str:
		if commit:
			self._port.commit()  # RT-458: the ERP effects are durable before Backend-Core hears of them
		self._client.ack_outcome(self._item.work_item_ref, ack, idempotency_key=_ack_key(self._item, outcome))
		return outcome

	def posted(self, ref: ErpnextDocumentRef) -> str:
		ack = OutcomeAckRequest.posted(ref, resolution_version=self._item.resolution_version)
		return self._send(ack, "posted", commit=True)

	def transient(self) -> str:
		ack = OutcomeAckRequest.failed_transient(resolution_version=self._item.resolution_version)
		return self._send(ack, "failed_transient")

	def rejected(self, category: str, message: str) -> str:
		reason = RejectionReason(category=category, message=plain_bounded_message(message))
		ack = OutcomeAckRequest.permanently_rejected(reason, resolution_version=self._item.resolution_version)
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


def post_transfer_work_item(
	work_item: PostingWorkItem,
	*,
	client: PostingFeedClient,
	store: IdempotencyStore,
	port: TransferErpPort,
	site_tz: str,
	uom_map: UomMap,
) -> str:
	"""Post one ``transfer_dispatch`` or ``transfer_receive`` work item and ack the outcome. Returns the outcome."""
	if work_item.kind not in ("transfer_dispatch", "transfer_receive"):
		raise ValueError(f"post_transfer_work_item handles the transfer kinds, got {work_item.kind!r}")
	acker = _Acker(client, work_item, port)
	try:
		return _post(work_item, acker, store, port, (site_tz, uom_map))
	except TransferTransient:
		return acker.transient()
	except TransferRefused as exc:
		return acker.rejected(_ACK_CATEGORY.get(exc.code, "validation"), f"{exc.code}: {exc.message}")
	except Exception as exc:
		# Terminal-outcome invariant (as the sale and count legs): an unclassified failure never escapes to
		# abort the page. After a successful submit the document exists, so it is reconciled; before, rejected.
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
	port: TransferErpPort,
	env: tuple[str, UomMap],
) -> str:
	site_tz, uom_map = env
	dispatch, receipt = work_item.transfer, work_item.receipt
	body = dispatch if work_item.kind == "transfer_dispatch" else receipt
	assert body is not None  # parse guarantees the kind's own body
	problem = (
		_dispatch_shape_problem(work_item, dispatch)  # type: ignore[arg-type]
		if work_item.kind == "transfer_dispatch"
		else _receive_shape_problem(work_item, receipt)  # type: ignore[arg-type]
	)
	if problem:
		raise _refuse("invalid_shape", problem)
	try:
		stamp = site_stamp(body.posting_datetime, site_tz)
	except ValueError as exc:
		raise _refuse("invalid_shape", str(exc)) from exc
	if not port.indexes_ready():
		return acker.transient()  # the unique provenance index is absent: pause, never post unguarded
	company = port.company()
	leg = (
		_dispatch_leg(dispatch, port, uom_map)  # type: ignore[arg-type]
		if work_item.kind == "transfer_dispatch"
		else _receive_leg(receipt, port, uom_map)  # type: ignore[arg-type]
	)
	# The lookup of an existing document comes BEFORE any live-site check: a retry of a leg that already posted is
	# echoed (after a read-back), never rejected because the site moved on since (RT-1514 review).
	docs = port.existing_documents(work_item.source_system, work_item.external_id)
	decision = classify_existing(docs, leg, stamp)
	if decision.action != "proceed":
		return _existing(work_item, acker, store, port, decision, leg)
	return _create(work_item, acker, store, port, (leg, stamp, company))


def _existing(
	work_item: PostingWorkItem,
	acker: _Acker,
	store: IdempotencyStore,
	port: TransferErpPort,
	decision: Decision,
	leg: _Leg,
) -> str:
	doc = decision.document
	assert doc is not None
	if decision.action == "in_flight":
		return acker.transient()  # never resubmit a Draft someone is already submitting
	ref = ErpnextDocumentRef(doctype=STOCK_ENTRY, name=doc.name)
	if decision.action == "reconcile":
		return acker.reconcile(ref, decision.message)
	return _echo(work_item, acker, store, port, ref, leg)


def _echo(
	work_item: PostingWorkItem,
	acker: _Acker,
	store: IdempotencyStore,
	port: TransferErpPort,
	ref: ErpnextDocumentRef,
	leg: _Leg,
) -> str:
	"""A matching submitted document is confirmed ONLY after the ledger is read back again.

	The first run may have died after the submit and before its verification or ack (a crash window), or its
	`reconciliation_required` ack may have been lost: an echo must never turn either into a `posted`.
	"""
	problem = verification_problem(leg, port.read_back(ref.name))
	if problem:
		return acker.reconcile(ref, f"verification failed on the existing document: {problem}")
	_record(store, work_item, ref)
	return acker.posted(ref)


def _record(store: IdempotencyStore, work_item: PostingWorkItem, ref: ErpnextDocumentRef) -> None:
	try:
		store.record_posted(key_for(work_item), ref)
	except IdempotencyConflict:
		pass  # another worker recorded the same key first; the provenance lookup is the authority


def _create(
	work_item: PostingWorkItem,
	acker: _Acker,
	store: IdempotencyStore,
	port: TransferErpPort,
	ctx: tuple[_Leg, SiteStamp, str],
) -> str:
	leg, stamp, company = ctx
	payload = leg.build(company, stamp, work_item.source_system, work_item.external_id)
	try:
		name = port.submit(payload)
	except TransferDuplicateProvenance:
		# A race or a crash window: a document with this key exists now. Re-classify, never resubmit.
		decision = classify_existing(
			port.existing_documents(work_item.source_system, work_item.external_id), leg, stamp
		)
		if decision.action == "proceed":
			return acker.transient()  # the index fired but the lookup sees nothing yet: retry later
		return _existing(work_item, acker, store, port, decision, leg)
	ref = ErpnextDocumentRef(doctype=STOCK_ENTRY, name=name)
	acker.created = ref
	problem = verification_problem(leg, port.read_back(name))
	if problem:
		return acker.reconcile(ref, f"verification failed: {problem}")
	_record(store, work_item, ref)
	return acker.posted(ref)
