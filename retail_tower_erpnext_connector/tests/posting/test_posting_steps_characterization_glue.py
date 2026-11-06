# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1228 — characterization of the sale and reversal posting legs in the REAL glue (no bench).

RT-1228 moves the steps ``post_work_item`` and ``_post_reversal`` share (build, submit, record in the
Posting Log, ack) into helpers. That must not change the ERPNext documents created, the Posting Log
calls, Gate G5, the failure taxonomy or the retry behaviour. These tests pin all of it as observed
on the code BEFORE the refactor, for three fixtures (a sale, a full void, a partial return) and for
every branch of the shared steps:

  - the document dict handed to ``frappe.get_doc`` (doctype, fields, amounts, taxes, references);
  - the Posting Log calls (``get_document_ref`` keys, ``record_posted`` key and ref);
  - each ack (outcome wire body and Idempotency-Key) and each structured log signal (level, fields);
  - the transaction events (savepoint, insert, submit, rollback, reload, commit).

Each scenario's whole trace is compared with ``posting-steps-golden.json``, recorded from the
unchanged code. The golden file is evidence: a refactor that needs it rewritten is not
behaviour-preserving. Set ``RT1228_REGEN=1`` only to record it on a commit that changes no glue.
"""

import dataclasses
import datetime
import importlib
import json
import os
import sys
import types
from decimal import Decimal
from pathlib import Path

import pytest

from retail_tower_erpnext_connector.connector.posting import contracts as c
from retail_tower_erpnext_connector.connector.posting.idempotency import IdempotencyConflict
from retail_tower_erpnext_connector.connector.posting.tender import TenderModeMap
from retail_tower_erpnext_connector.connector.posting.uom import (
	PreResolvedWarehouse,
	StoreCustomerMap,
	UomMap,
)

_GLUE = "retail_tower_erpnext_connector.connector.posting.frappe_glue"
_PKG = "retail_tower_erpnext_connector.connector.posting"
_GOLDEN = Path(__file__).with_name("posting-steps-golden.json")
_MISSING = object()

_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
_STORE = "33333333-3333-4333-8333-333333333333"
_TENDER_REF = "77777777-7777-4777-8777-777777777777"
_ORIGINAL = "ACC-SINV-2026-00100"  # the original sale's invoice, which every reversal targets
_ORIGINAL_EXTERNAL_ID = "POS-9001"


# --- frappe stand-in -------------------------------------------------------------------------------


class _Logger:
	def __init__(self, sink):
		self._sink = sink

	def info(self, record):
		self._sink.append(("info", record))

	def warning(self, record):
		self._sink.append(("warning", record))

	def error(self, record):
		self._sink.append(("error", record))


class _Invoice:
	"""A Sales Invoice that computes its totals from the payload on insert, like ERPNext."""

	def __init__(self, payload, fake):
		self.payload, self._fake, self.values, self.name = payload, fake, {}, None

	def insert(self):
		self._fake.events.append("insert")
		if self._fake.raise_on_insert is not None:
			raise self._fake.raise_on_insert
		provenance = (self.payload.get("rt_source_system"), self.payload.get("rt_external_id"))
		if provenance in self._fake.provenance:  # GATE G5: the unique_rt_si_provenance index
			raise self._fake.exceptions.UniqueValidationError("Duplicate entry for unique_rt_si_provenance")
		grand = sum(Decimal(str(i["amount"])) for i in self.payload["items"])
		paid = sum(Decimal(str(p["amount"])) for p in self.payload.get("payments") or [])
		self.values = {
			"grand_total": float(grand),
			"paid_amount": float(paid),
			"change_amount": 0.0,
			"write_off_amount": 0.0,
			"outstanding_amount": float(grand - paid),
		}
		self._fake.counter += 1
		self.name = f"ACC-SINV-2026-{200 + self._fake.counter:05d}"

	def get(self, field):
		return {**self.payload, **self.values}.get(field)

	def submit(self):
		self._fake.events.append("submit")
		if self._fake.raise_on_submit is not None:
			raise self._fake.raise_on_submit
		self._fake.persisted[self.name] = {**self.payload, **self.values, "docstatus": 1}
		self._fake.provenance[(self.payload.get("rt_source_system"), self.payload.get("rt_external_id"))] = (
			self.name
		)

	def reload(self):
		self._fake.events.append("reload")
		if self._fake.raise_on_reload is not None:
			raise self._fake.raise_on_reload
		self.values.update(self._fake.after_submit)


class _Frappe(types.ModuleType):
	"""One original invoice (rows A: 3 sold, B: 1 sold; paid 400 Cash) plus what this run submits."""

	def __init__(self):
		super().__init__("frappe")
		self.ValidationError = type("ValidationError", (Exception,), {})
		self.QueryTimeoutError = type("QueryTimeoutError", (Exception,), {})
		self.DoesNotExistError = type("DoesNotExistError", (Exception,), {})
		self.exceptions = types.SimpleNamespace(
			UniqueValidationError=type("UniqueValidationError", (self.ValidationError,), {})
		)
		self.logs, self.events, self.payloads = [], [], []
		self.logger = lambda name=None: _Logger(self.logs)
		self.persisted, self.provenance, self.counter = {}, {}, 0
		self.raise_on_insert = self.raise_on_submit = self.raise_on_reload = None
		self.after_submit = {}
		self.default_company = "RT Company"
		self.original_posted = True
		self.warehouses = {"Frozen WH": {"company": "RT Company", "is_group": 0}}
		self._rows = [
			{
				"name": "row-a",
				"item_code": "ITEM-A",
				"qty": 3.0,
				"idx": 1,
				"warehouse": "Stores - A",
				"rt_line_ref": _A,
				"uom": "Box",
				"conversion_factor": 12.0,
			},
			{
				"name": "row-b",
				"item_code": "ITEM-B",
				"qty": 1.0,
				"idx": 2,
				"warehouse": "Stores - B",
				"rt_line_ref": _B,
				"uom": "Nos",
				"conversion_factor": 1.0,
			},
		]
		self.db = types.SimpleNamespace(
			get_value=self._get_value,
			get_single_value=lambda doctype, field: self.default_company,
			savepoint=lambda name: self.events.append("savepoint"),
			rollback=lambda save_point=None: self.events.append("rollback"),
			commit=lambda: self.events.append("commit"),
		)
		self.utils = types.SimpleNamespace(get_system_timezone=lambda: "UTC")

	def reset_trace(self):
		self.logs.clear()
		self.events.clear()
		self.payloads.clear()

	def _get_value(self, doctype, filters, fields, **_):
		if doctype == "Warehouse":
			return self.warehouses.get(filters)
		assert doctype == "Sales Invoice"
		if isinstance(filters, dict):  # provenance lookups (original / already-posted document)
			assert filters.get("docstatus") == 1
			provenance = (filters["rt_source_system"], filters["rt_external_id"])
			if provenance == ("pos-pulse", _ORIGINAL_EXTERNAL_ID) and self.original_posted:
				return _ORIGINAL
			return self.provenance.get(provenance)
		if filters == _ORIGINAL:
			if fields == ["posting_date", "posting_time"]:
				return {
					"posting_date": datetime.date(2026, 6, 1),
					"posting_time": datetime.timedelta(hours=10),
				}
			if fields == "company":
				return "Original Co"
			return {
				"update_stock": 1,
				"disable_rounded_total": 1,
				"is_pos": 1,
				"customer": "Original Customer",
				"company": "Original Co",
			}
		raise AssertionError(f"unexpected get_value {doctype!r} {filters!r} {fields!r}")

	def get_all(self, doctype, **kwargs):
		filters = kwargs.get("filters") or {}
		if doctype == "Sales Invoice Item" and filters.get("parent") == _ORIGINAL:
			return [{f: r[f] for f in kwargs["fields"] if f in r} for r in self._rows]
		if doctype == "Sales Invoice Payment" and filters.get("parent") == _ORIGINAL:
			return [{"mode_of_payment": "Cash", "amount": 400.0, "idx": 1}]
		return []  # Items: no batch / serial tracking

	def get_doc(self, payload, name=None):
		if name is not None:  # a replay reads the already-posted invoice back
			if name not in self.persisted:
				raise self.DoesNotExistError(name)
			return dict(self.persisted[name])
		self.payloads.append(payload)
		return _Invoice(payload, self)


class _Client:
	def __init__(self):
		self.acks = []

	def ack_outcome(self, ref, request, idempotency_key):
		self.acks.append(
			{"work_item_ref": ref, "body": request.to_wire(), "idempotency_key": idempotency_key}
		)


class _Store:
	"""The Posting Log: records every call; ``conflict_with`` makes ``record_posted`` lose a race."""

	def __init__(self):
		self.calls, self.rows, self.conflict_with = [], {}, None

	def get_document_ref(self, key):
		self.calls.append(("get_document_ref", key))
		return self.rows.get(key)

	def record_posted(self, key, ref):
		self.calls.append(("record_posted", key, ref))
		if self.conflict_with is not None:
			raise IdempotencyConflict(key, self.conflict_with, ref)
		self.rows[key] = ref


def _restore(namespace, name, value):
	namespace.pop(name, None)
	if value is not _MISSING:
		namespace[name] = value


@pytest.fixture
def load_glue():
	pkg = importlib.import_module(_PKG)
	saved = [
		(sys.modules, "frappe", sys.modules.get("frappe", _MISSING)),
		(sys.modules, _GLUE, sys.modules.get(_GLUE, _MISSING)),
		(vars(pkg), "frappe_glue", vars(pkg).get("frappe_glue", _MISSING)),
	]

	def _load(fake):
		sys.modules["frappe"] = fake
		sys.modules.pop(_GLUE, None)
		return importlib.import_module(_GLUE)

	yield _load
	for namespace, name, value in saved:
		_restore(namespace, name, value)


# --- fixtures ----------------------------------------------------------------------------------------


def _line(ref, item, qty, amount):
	return {
		"lineRef": ref,
		"lineName": item,
		"unitPrice": "100.00",
		"currencyCode": "EGP",
		"quantity": qty,
		"lineAmount": amount,
		"unit": "each",
		"erpnextItemRef": {"doctype": "Item", "name": item},
	}


def _sale_body(external_id, **extra):
	return {
		"saleRef": "22222222-2222-4222-8222-222222222222",
		"storeId": _STORE,
		"currencyCode": "EGP",
		"posTotal": "400.00",
		"occurredAt": "2026-06-01T10:00:00Z",
		"businessDate": "2026-06-01",
		"sourceSystem": "pos-pulse",
		"externalId": external_id,
		"lines": [_line(_A, "ITEM-A", "3", "300.00"), _line(_B, "ITEM-B", "1", "100.00")],
		"tenders": [{"method": "cash", "amount": "400.00", "tenderRef": _TENDER_REF}],
		**extra,
	}


def _sale(*, warehouse_ref=None):
	extra = {"warehouseRef": {"doctype": "Warehouse", "name": warehouse_ref}} if warehouse_ref else {}
	return c.PostingWorkItem.from_wire(
		{
			"workItemRef": "66666666-6666-4666-8666-666666666666",
			"kind": "sale_post",
			"sourceSystem": "pos-pulse",
			"externalId": "POS-7001",
			"payloadHash": "c" * 64,
			"businessDate": "2026-06-01",
			"itemCursor": "cursor-sale",
			"resolutionVersion": 2,
			"sale": _sale_body("POS-7001", **extra),
		}
	)


def _reversal(work_item_ref, reversal_of, cursor):
	return c.PostingWorkItem.from_wire(
		{
			"workItemRef": work_item_ref,
			"kind": "reversal",
			"sourceSystem": "pos-pulse",
			"externalId": _ORIGINAL_EXTERNAL_ID,
			"payloadHash": "e" * 64,
			"businessDate": "2026-06-01",
			"itemCursor": cursor,
			"reversalOf": {"sourceSystem": "pos-pulse", "externalId": _ORIGINAL_EXTERNAL_ID, **reversal_of},
			"sale": _sale_body(_ORIGINAL_EXTERNAL_ID),
		}
	)


def _void():
	return _reversal(
		"99999999-9999-4999-8999-999999999999",
		{"reversalKind": "void", "recordedAt": "2026-06-03T08:00:00Z", "businessDate": "2026-06-03"},
		"cursor-void",
	)


def _return():
	return _reversal(
		"88888888-8888-4888-8888-888888888888",
		{
			"reversalKind": "return",
			"recordedAt": "2026-06-05T09:30:00Z",
			"businessDate": "2026-06-05",
			"returnLines": [{"lineRef": _A, "quantity": "1", "lineAmount": "100.00"}],
			"refundTenders": [{"method": "cash", "amount": "100.00"}],
		},
		"cursor-return",
	)


def _refund():
	"""An amount-only refund: RT-71 rejects it (UnsupportedReversal) before anything is built."""
	return _reversal("55555555-5555-4555-8555-555555555555", {"reversalKind": "refund"}, "cursor-refund")


_FIXTURES = {"sale": _sale, "void": _void, "return": _return}


# --- the run and its trace ---------------------------------------------------------------------------


def _post(glue, work_item, client, store, *, uom=None, customers=None):
	return glue.post_work_item(
		work_item,
		client=client,
		store=store,
		uom_map=UomMap({"each": "Nos"} if uom is None else uom),
		warehouses=PreResolvedWarehouse({_STORE: {"doctype": "Warehouse", "name": "Map WH"}}),
		customers=StoreCustomerMap({_STORE: "Walk-in"} if customers is None else customers),
		tenders=TenderModeMap({"cash": "Cash"}),
		correlation_id="rt1228-test",
	)


def _encode(value):
	if dataclasses.is_dataclass(value) and not isinstance(value, type):
		return {"__type__": type(value).__name__, **dataclasses.asdict(value)}
	if isinstance(value, Decimal | datetime.date | datetime.time | datetime.timedelta):
		return f"{type(value).__name__}({value})"
	raise TypeError(f"not encodable: {value!r}")


def _trace(outcome, fake, client, store):
	return json.loads(
		json.dumps(
			{
				"outcome": outcome,
				"documents": fake.payloads,
				"events": fake.events,
				"posting_log": store.calls,
				"acks": client.acks,
				"signals": fake.logs,
			},
			default=_encode,
			sort_keys=True,
		)
	)


def _prime(glue, fake, work_item):
	"""Post ``work_item`` once (the invoice now exists), then clear the recorded trace."""
	client, store = _Client(), _Store()
	assert _post(glue, work_item, client, store) == "posted"
	fake.reset_trace()
	return store


_CONFLICT_REF = c.ErpnextDocumentRef(doctype="Sales Invoice", name="ACC-SINV-2026-00201")


def _set_replay(glue, fake, store, work_item):  # the Posting Log already holds the document (lost ack)
	store.rows = _prime(glue, fake, work_item).rows


def _set_duplicate_recovered(glue, fake, store, work_item):  # G5: submitted, crashed before record_posted
	_prime(glue, fake, work_item)


def _set_duplicate_recovered_conflict(glue, fake, store, work_item):  # ...another worker backfilled first
	_prime(glue, fake, work_item)
	store.conflict_with = _CONFLICT_REF


def _set_record_conflict(glue, fake, store, work_item):  # another worker recorded this key first
	_prime(glue, fake, work_item)
	fake.provenance.clear()  # this run's insert is not refused by the index
	store.conflict_with = _CONFLICT_REF


def _raise_on(stage, make_exc):
	def _set(glue, fake, store, work_item):
		setattr(fake, f"raise_on_{stage}", make_exc(fake))

	return _set


def _set_readback_mismatch(glue, fake, store, work_item):
	fake.after_submit = {"grand_total": 1.0}


# scenario → (setup, post kwargs); every scenario runs for the sale, the void and the return.
_COMMON = {
	"posted": (None, {}),
	"replay": (_set_replay, {}),
	"duplicate_recovered": (_set_duplicate_recovered, {}),
	"duplicate_recovered_conflict": (_set_duplicate_recovered_conflict, {}),
	"duplicate_unresolved": (  # the index fired, but no submitted invoice is found
		_raise_on("insert", lambda f: f.exceptions.UniqueValidationError("Duplicate entry")),
		{},
	),
	"record_conflict": (_set_record_conflict, {}),
	"transient_insert": (_raise_on("insert", lambda f: f.QueryTimeoutError("lock wait timeout")), {}),
	"transient_submit": (_raise_on("submit", lambda f: TimeoutError("timed out")), {}),
	"validation_insert": (_raise_on("insert", lambda f: f.ValidationError("Item ITEM-A is disabled")), {}),
	"other_insert": (_raise_on("insert", lambda f: RuntimeError("boom")), {}),
	"readback_mismatch": (_set_readback_mismatch, {}),
	"readback_unreadable": (_raise_on("reload", lambda f: f.QueryTimeoutError("reload timeout")), {}),
	"unmapped_unit": (None, {"uom": {}}),
	"unmapped_store": (None, {"customers": {}}),
}


def _run(load_glue, scenario, make):
	fake = _Frappe()
	glue = load_glue(fake)
	client, store = _Client(), _Store()
	work_item = make()
	setup, kwargs = _COMMON[scenario]
	if setup is not None:
		setup(glue, fake, store, work_item)
	outcome = _post(glue, work_item, client, store, **kwargs)
	return _trace(outcome, fake, client, store)


def _group_warehouse(fake):
	fake.warehouses = {"Frozen WH": {"company": "RT Company", "is_group": 1}}


def _special_site_company_missing(glue, fake):
	fake.default_company = None
	return _sale()


def _special_frozen_warehouse_rejected(glue, fake):
	_group_warehouse(fake)
	return _sale(warehouse_ref="Frozen WH")


def _special_frozen_warehouse_recovered(glue, fake):
	work_item = _sale(warehouse_ref="Frozen WH")
	_prime(glue, fake, work_item)
	_group_warehouse(fake)
	return work_item


def _special_void_awaiting_original(glue, fake):
	fake.original_posted = False
	return _void()


def _special_return_awaiting_original(glue, fake):
	fake.original_posted = False
	return _return()


def _special_refund_recovered(glue, fake):
	work_item = _refund()
	fake.provenance[("pos-pulse", work_item.work_item_ref)] = "ACC-SINV-2026-00150"
	fake.persisted["ACC-SINV-2026-00150"] = {"docstatus": 1, "items": [], "company": "Original Co"}
	return work_item


def _special_original_posting_unreadable(glue, fake):
	original_get_value = fake.db.get_value

	def _get_value(doctype, filters, fields, **kw):
		if filters == _ORIGINAL and fields == ["posting_date", "posting_time"]:
			return None
		return original_get_value(doctype, filters, fields, **kw)

	fake.db.get_value = _get_value
	return _void()


def _special_void_line_mismatch(glue, fake):
	fake._rows = fake._rows[:1]
	return _void()


_SPECIAL = {
	"sale_site_company_missing": _special_site_company_missing,
	"sale_frozen_warehouse_posted": lambda glue, fake: _sale(warehouse_ref="Frozen WH"),
	"sale_frozen_warehouse_rejected": _special_frozen_warehouse_rejected,
	"sale_frozen_warehouse_recovered": _special_frozen_warehouse_recovered,
	"unsupported_kind": lambda glue, fake: dataclasses.replace(_sale(), kind="adjustment"),
	"void_awaiting_original": _special_void_awaiting_original,
	"return_awaiting_original": _special_return_awaiting_original,
	"refund_rejected": lambda glue, fake: _refund(),
	"refund_recovered": _special_refund_recovered,
	"void_no_reversal_of": lambda glue, fake: dataclasses.replace(_void(), reversal_of=None),
	"void_original_posting_unreadable": _special_original_posting_unreadable,
	"void_line_mismatch": _special_void_line_mismatch,
}


def _run_special(load_glue, scenario):
	fake = _Frappe()
	glue = load_glue(fake)
	client, store = _Client(), _Store()
	work_item = _SPECIAL[scenario](glue, fake)
	try:
		outcome = _post(glue, work_item, client, store)
	except Exception as exc:  # pinned too: an unknown kind fails in key_for, before any ack
		outcome = f"raised {type(exc).__name__}: {exc}"
	return _trace(outcome, fake, client, store)


_CASES = [(f"{fixture}:{scenario}", fixture, scenario) for fixture in _FIXTURES for scenario in _COMMON] + [
	(scenario, None, scenario) for scenario in _SPECIAL
]


def _observe(load_glue, fixture, scenario):
	if fixture is None:
		return _run_special(load_glue, scenario)
	return _run(load_glue, scenario, _FIXTURES[fixture])


@pytest.fixture(scope="module")
def golden():
	if os.environ.get("RT1228_REGEN") == "1":
		return None
	return json.loads(_GOLDEN.read_text(encoding="utf-8"))


_RECORDED = {}


@pytest.mark.parametrize(("case", "fixture", "scenario"), _CASES, ids=[case for case, _, _ in _CASES])
def test_posting_trace_matches_golden(load_glue, golden, case, fixture, scenario):
	observed = _observe(load_glue, fixture, scenario)
	if golden is None:
		_RECORDED[case] = observed
		_GOLDEN.write_text(json.dumps(_RECORDED, indent=1, sort_keys=True) + "\n", encoding="utf-8")
		return
	assert observed == golden[case]


def test_golden_covers_exactly_the_cases(golden):
	if golden is None:
		pytest.skip("recording")
	assert sorted(golden) == sorted(case for case, _, _ in _CASES)


class TestPinnedFacts:
	"""Readable pins for the most important facts in the golden traces (so a reviewer need not diff
	the JSON to see them)."""

	def test_sale_posts_one_invoice_records_it_and_acks_posted(self, load_glue):
		trace = _run(load_glue, "posted", _sale)
		(doc,) = trace["documents"]
		assert doc["doctype"] == "Sales Invoice" and doc["company"] == "RT Company"
		assert (doc["rt_source_system"], doc["rt_external_id"]) == ("pos-pulse", "POS-7001")
		assert trace["events"] == ["savepoint", "insert", "submit", "reload", "commit"]
		assert [call[0] for call in trace["posting_log"]] == ["get_document_ref", "record_posted"]
		(ack,) = trace["acks"]
		assert ack["body"]["outcome"] == "posted" and ack["idempotency_key"].endswith(":posted:v2")

	@pytest.mark.parametrize("fixture", ["void", "return"])
	def test_reversal_posts_a_credit_note_against_the_original(self, load_glue, fixture):
		trace = _run(load_glue, "posted", _FIXTURES[fixture])
		(doc,) = trace["documents"]
		assert doc["is_return"] == 1 and doc["return_against"] == _ORIGINAL
		assert doc["company"] == "Original Co"
		# The reversal's own provenance (#28 re-key), never the original sale's.
		assert doc["rt_external_id"] != _ORIGINAL_EXTERNAL_ID
		# The reversal leg runs the replay guard twice (post_work_item, then _post_reversal).
		assert [call[0] for call in trace["posting_log"]] == [
			"get_document_ref",
			"get_document_ref",
			"record_posted",
		]

	@pytest.mark.parametrize("fixture", list(_FIXTURES))
	def test_g5_duplicate_is_recovered_never_rejected(self, load_glue, fixture):
		trace = _run(load_glue, "duplicate_recovered", _FIXTURES[fixture])
		assert trace["outcome"] == "posted"
		assert trace["events"][:3] == ["savepoint", "insert", "rollback"]
		assert trace["posting_log"][-1][0] == "record_posted"  # the backfill
		assert any(record["event"] == "posting.recovered" for _, record in trace["signals"])

	@pytest.mark.parametrize("fixture", list(_FIXTURES))
	def test_transient_acks_failed_transient_with_the_per_offer_key(self, load_glue, fixture):
		trace = _run(load_glue, "transient_insert", _FIXTURES[fixture])
		(ack,) = trace["acks"]
		assert ack["body"]["outcome"] == "failed_transient"
		assert ack["idempotency_key"].endswith(f":failed_transient:cursor-{fixture}")
		assert trace["events"] == ["savepoint", "insert", "rollback"]
		assert [call[0] for call in trace["posting_log"]].count("record_posted") == 0
