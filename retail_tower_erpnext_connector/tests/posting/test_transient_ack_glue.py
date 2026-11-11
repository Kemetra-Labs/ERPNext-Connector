# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-171 — the ``failed_transient`` ack Idempotency-Key is per OFFER, in the REAL glue (no bench).

Backend-Core's ack route is ``@Idempotent``: a stored key is REPLAYED without running the handler.
With the old ``{workItemRef}:failed_transient`` key, the SECOND transient of one work-item was
replayed, the row was never re-headed, and it stayed pending forever. The transient key now carries
the offer's ``itemCursor`` (DP2 issues a new one per re-offer), so each re-offer's ack reaches the
handler while a resend of one offer still dedupes. ``posted`` / ``permanently_rejected`` keys are
byte-identical to before.

``frappe_glue`` imports frappe, so it is loaded against a minimal stand-in (the RT-71 / RT-78
pattern). The stand-in's Sales Invoice raises ``TimeoutError`` (always a transient) on insert while
``fake.transient`` is set, and the stand-in can hold an ORIGINAL invoice for the reversal leg.

RT-172 — a reversal whose original sale has no submitted invoice YET acks ``failed_transient`` with
that per-offer key (signal ``posting.reversal.awaiting_original``), inserts nothing, and posts with
``return_against`` once the original exists. It never acks ``validation`` for that case: DP2's
retry budget ends it as ``retry_budget_exhausted``. A reversal with no ``reversalOf`` at all is still
``permanently_rejected / validation``.
"""

import datetime
import importlib
import sys
import types
from decimal import Decimal

import pytest

from retail_tower_erpnext_connector.connector.posting import contracts as c
from retail_tower_erpnext_connector.connector.posting.tender import TenderModeMap
from retail_tower_erpnext_connector.connector.posting.uom import (
	PreResolvedWarehouse,
	StoreCustomerMap,
	UomMap,
)

_GLUE = "retail_tower_erpnext_connector.connector.posting.frappe_glue"
_PKG = "retail_tower_erpnext_connector.connector.posting"
_MISSING = object()
_STORE = "33333333-3333-4333-8333-333333333333"
_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_SALE_REF = "17171717-1717-4171-8171-171717171717"
_REVERSAL_REF = "17272727-2727-4272-8272-272727272727"
_ORIGINAL_EXTERNAL_ID = "POS-17100"
_ORIGINAL = "ACC-SINV-2026-17100"


class _Logger:
	def __init__(self):
		self.records = []

	def info(self, record):
		self.records.append(record)

	warning = error = info


class _Invoice:
	def __init__(self, payload, fake):
		self.payload, self._fake, self.name, self.values = payload, fake, "ACC-SINV-2026-17999", {}

	def insert(self):
		if self._fake.transient:
			raise TimeoutError("Lock wait timeout exceeded")
		grand = sum(Decimal(i["amount"]) for i in self.payload["items"])
		paid = sum(Decimal(p["amount"]) for p in self.payload.get("payments") or [])
		self.values = {
			"grand_total": float(grand),
			"paid_amount": float(paid),
			"change_amount": 0.0,
			"write_off_amount": 0.0,
			"outstanding_amount": float(grand - paid),
		}
		self._fake.events.append("insert")

	def get(self, field):
		# RT-378: ERPNext holds the payload as sent, on the site default company, plus computed totals.
		return {"company": "RT Company", **self.payload, **self.values}.get(field)

	def reload(self):
		pass  # RT-378 read-back after submit: the database holds what insert computed

	def submit(self):
		self._fake.events.append("submit")


class _Frappe(types.ModuleType):
	"""``originals`` maps a sale's ``externalId`` to its submitted forward invoice (mutable)."""

	def __init__(self, *, transient=False, originals=None):
		super().__init__("frappe")
		self.ValidationError = type("ValidationError", (Exception,), {})
		self.exceptions = types.SimpleNamespace(
			UniqueValidationError=type("UniqueValidationError", (self.ValidationError,), {})
		)
		self._logger = _Logger()
		self.logger = lambda name=None: self._logger
		self.events, self.payloads = [], []
		self.transient = transient
		self.originals = dict(originals or {})
		self.db = types.SimpleNamespace(
			get_value=self._get_value,
			get_single_value=lambda doctype, field: "RT Company",  # RT-378 site default company
			savepoint=lambda name: self.events.append("savepoint"),
			rollback=lambda save_point=None: self.events.append("rollback"),
			commit=lambda: None,  # RT-458 order is asserted in test_commit_before_ack_glue
		)
		self.utils = types.SimpleNamespace(get_system_timezone=lambda: "UTC")

	def _get_value(self, doctype, filters, fields, **_):
		if isinstance(filters, dict):  # provenance lookups (original / crash-window recovery)
			if filters.get("docstatus") == 1:
				return self.originals.get(filters.get("rt_external_id"))
			return None
		assert filters == _ORIGINAL
		if fields == ["posting_date", "posting_time"]:
			return {"posting_date": datetime.date(2026, 6, 1), "posting_time": datetime.timedelta(hours=10)}
		if fields == "company":
			return "Original Co"  # RT-378: a reversal replay is held to the original's company
		return {
			"update_stock": 1,
			"disable_rounded_total": 1,
			"is_pos": 0,
			"customer": "Original Customer",
			"company": "Original Co",
		}

	def get_all(self, doctype, **kwargs):
		if doctype == "Sales Invoice Item":
			row = {
				"name": "row-a",
				"item_code": "ITEM-A",
				"qty": 1.0,
				"idx": 1,
				"warehouse": "Stores - A",
				"rt_line_ref": _A,
				"uom": "Nos",
				"conversion_factor": 1.0,
			}
			return [{f: row[f] for f in kwargs["fields"] if f in row}]
		return []  # Items (no batch/serial) and the original's payments (unpaid original)

	def get_doc(self, payload):
		self.payloads.append(payload)
		return _Invoice(payload, self)


class _Client:
	def __init__(self):
		self.acks = []

	def ack_outcome(self, ref, request, idempotency_key):
		self.acks.append({"ref": ref, "ack": request.to_wire(), "key": idempotency_key})


class _Store:
	def __init__(self):
		self.writes = []

	def get_document_ref(self, key):
		return None

	def record_posted(self, key, ref):
		self.writes.append((key, ref))


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


def _sale(external_id):
	return {
		"saleRef": "22222222-2222-4222-8222-222222222222",
		"storeId": _STORE,
		"currencyCode": "EGP",
		"posTotal": "100.00",
		"occurredAt": "2026-06-01T10:00:00Z",
		"businessDate": "2026-06-01",
		"sourceSystem": "pos-pulse",
		"externalId": external_id,
		"lines": [
			{
				"lineRef": _A,
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


def _sale_post(cursor, *, unit="each", tax_amount=None):
	sale = _sale("POS-17101")
	sale["lines"][0]["unit"] = unit
	if tax_amount is not None:
		sale["lines"][0]["taxAmount"] = tax_amount
	return c.PostingWorkItem.from_wire(
		{
			"workItemRef": _SALE_REF,
			"kind": "sale_post",
			"sourceSystem": "pos-pulse",
			"externalId": "POS-17101",
			"payloadHash": "a" * 64,
			"businessDate": "2026-06-01",
			"itemCursor": str(cursor),
			"sale": sale,
		}
	)


def _reversal(kind, cursor):
	ref = {
		"sourceSystem": "pos-pulse",
		"externalId": _ORIGINAL_EXTERNAL_ID,
		"reversalKind": kind,
		"recordedAt": "2026-06-05T09:30:00Z",
		"businessDate": "2026-06-05",
	}
	if kind == "return":
		ref["returnLines"] = [{"lineRef": _A, "quantity": "1", "lineAmount": "100.00"}]
		ref["refundTenders"] = [{"method": "cash", "amount": "100.00"}]
	return c.PostingWorkItem.from_wire(
		{
			"workItemRef": _REVERSAL_REF,
			"kind": "reversal",
			"sourceSystem": "pos-pulse",
			"externalId": _ORIGINAL_EXTERNAL_ID,
			"payloadHash": "b" * 64,
			"businessDate": "2026-06-01",
			"itemCursor": str(cursor),
			"reversalOf": ref,
			"sale": _sale(_ORIGINAL_EXTERNAL_ID),
		}
	)


def _post(glue, work_item, client=None, store=None):
	client, store = client or _Client(), store or _Store()
	outcome = glue.post_work_item(
		work_item,
		client=client,
		store=store,
		uom_map=UomMap({"each": "Nos"}),
		warehouses=PreResolvedWarehouse({_STORE: {"doctype": "Warehouse", "name": "Map WH"}}),
		customers=StoreCustomerMap({_STORE: "Walk-in"}),
		tenders=TenderModeMap({"cash": "Cash"}),
		correlation_id="rt171-test",
	)
	return outcome, client, store


class TestTransientAckKeyPerOffer:
	def test_sale_post_transients_at_different_cursors_get_different_keys_then_posts(self, load_glue):
		fake = _Frappe(transient=True)
		glue = load_glue(fake)
		client = _Client()

		assert _post(glue, _sale_post(10), client)[0] == "failed_transient"
		assert _post(glue, _sale_post(12), client)[0] == "failed_transient"
		fake.transient = False
		assert _post(glue, _sale_post(14), client)[0] == "posted"

		assert [a["key"] for a in client.acks] == [
			f"{_SALE_REF}:failed_transient:10",
			f"{_SALE_REF}:failed_transient:12",
			f"{_SALE_REF}:posted",
		]
		assert [a["ack"]["outcome"] for a in client.acks] == [
			"failed_transient",
			"failed_transient",
			"posted",
		]
		assert fake.events.count("submit") == 1  # nothing half-posted by the transients
		assert fake.events.count("rollback") == 2

	def test_a_resend_of_one_offer_reuses_its_key(self, load_glue):
		glue = load_glue(_Frappe(transient=True))
		client = _Client()
		_post(glue, _sale_post(10), client)
		_post(glue, _sale_post(10), client)  # the same offer, re-processed (e.g. a dropped response)
		assert client.acks[0]["key"] == client.acks[1]["key"] == f"{_SALE_REF}:failed_transient:10"

	def test_reversal_transient_key_carries_the_offer_cursor(self, load_glue):
		fake = _Frappe(transient=True, originals={_ORIGINAL_EXTERNAL_ID: _ORIGINAL})
		glue = load_glue(fake)
		client = _Client()

		assert _post(glue, _reversal("void", 20), client)[0] == "failed_transient"
		assert _post(glue, _reversal("void", 22), client)[0] == "failed_transient"
		fake.transient = False
		assert _post(glue, _reversal("void", 24), client)[0] == "posted"

		assert [a["key"] for a in client.acks] == [
			f"{_REVERSAL_REF}:failed_transient:20",
			f"{_REVERSAL_REF}:failed_transient:22",
			f"{_REVERSAL_REF}:posted",
		]
		assert fake.payloads[-1]["return_against"] == _ORIGINAL


class TestTerminalAckKeysUnchanged:
	"""``posted`` and ``permanently_rejected`` happen at most once — their keys stay byte-identical."""

	def test_posted_key(self, load_glue):
		_outcome, client, _store = _post(load_glue(_Frappe()), _sale_post(10))
		assert client.acks[0]["key"] == f"{_SALE_REF}:posted"

	def test_permanently_rejected_key(self, load_glue):
		_outcome, client, _store = _post(load_glue(_Frappe()), _sale_post(10, unit="crate"))
		assert client.acks[0]["ack"]["outcome"] == "permanently_rejected"
		assert client.acks[0]["key"] == f"{_SALE_REF}:permanently_rejected"


class TestReversalAwaitsOriginal:
	@pytest.mark.parametrize("kind", ["void", "return"])
	def test_reversal_before_its_original_waits_then_posts_against_it(self, load_glue, kind):
		fake = _Frappe()  # the original sale has no submitted invoice yet
		glue = load_glue(fake)
		client = _Client()

		assert _post(glue, _reversal(kind, 11), client)[0] == "failed_transient"
		assert client.acks == [
			{
				"ref": _REVERSAL_REF,
				"ack": {"outcome": "failed_transient"},
				"key": f"{_REVERSAL_REF}:failed_transient:11",
			}
		]
		assert fake.payloads == [] and fake.events == []  # nothing built into ERPNext, no insert
		(signal,) = [r for r in fake._logger.records if r["event"] == "posting.reversal.awaiting_original"]
		assert signal["work_item_ref"] == _REVERSAL_REF and signal["reversal_kind"] == kind

		fake.originals[_ORIGINAL_EXTERNAL_ID] = _ORIGINAL  # the original sale posts
		assert _post(glue, _reversal(kind, 13), client)[0] == "posted"
		assert client.acks[1]["key"] == f"{_REVERSAL_REF}:posted"
		(payload,) = fake.payloads
		assert payload["return_against"] == _ORIGINAL and payload["is_return"] == 1
		assert fake.events == ["savepoint", "insert", "submit"]

	def test_never_acks_validation_while_waiting(self, load_glue):
		# DP2 owns the budget (POSTING_RETRY_BUDGET): every offer the connector sees is a transient,
		# each under its own key, so the end state is retry_budget_exhausted — never validation.
		fake = _Frappe()
		glue = load_glue(fake)
		client = _Client()
		cursors = [11, 13, 15, 17, 19]
		for cursor in cursors:
			assert _post(glue, _reversal("void", cursor), client)[0] == "failed_transient"
		assert {a["ack"]["outcome"] for a in client.acks} == {"failed_transient"}
		assert [a["key"] for a in client.acks] == [f"{_REVERSAL_REF}:failed_transient:{n}" for n in cursors]
		assert fake.payloads == []

	def test_reversal_without_reversal_of_is_still_a_validation_rejection(self, load_glue):
		wi = _reversal("void", 11)
		malformed = c.PostingWorkItem(
			work_item_ref=wi.work_item_ref,
			kind=wi.kind,
			source_system=wi.source_system,
			external_id=wi.external_id,
			payload_hash=wi.payload_hash,
			business_date=wi.business_date,
			sale=wi.sale,
			item_cursor=wi.item_cursor,
			reversal_of=None,
		)
		fake = _Frappe(originals={_ORIGINAL_EXTERNAL_ID: _ORIGINAL})
		outcome, client, _store = _post(load_glue(fake), malformed)
		assert outcome == "permanently_rejected"
		assert client.acks[0]["ack"]["reason"]["category"] == "validation"
		assert client.acks[0]["key"] == f"{_REVERSAL_REF}:permanently_rejected"
		assert fake.payloads == []


class _DeadlockFrappe(_Frappe):
	"""Frappe v16 raises ``frappe.QueryDeadlockError(Exception)`` on a MariaDB deadlock.

	Source: frappe/frappe v16.52.0 ``frappe/exceptions.py`` (``class QueryDeadlockError(Exception)``)
	and ``frappe/database/database.py`` (``elif self.is_deadlocked(e): raise frappe.QueryDeadlockError``).
	It is not a ``ValidationError``, so before RT-1312 it fell through to ``permanently_rejected/other``.
	"""

	def __init__(self):
		super().__init__()
		self.QueryDeadlockError = type("QueryDeadlockError", (Exception,), {})
		self.deadlock = True
		self._savepoint_lost = False

		# MariaDB rolls the deadlock victim's WHOLE transaction back, so the savepoint is gone and
		# ``ROLLBACK TO SAVEPOINT`` fails (ER_SP_DOES_NOT_EXIST, 1305) — Frappe's rollback(save_point=)
		# runs exactly that statement. A full rollback still works.
		def _rollback(save_point=None):
			if save_point is not None and self._savepoint_lost:
				raise RuntimeError("(1305, 'SAVEPOINT rt_submit does not exist')")
			self.events.append("rollback" if save_point is not None else "full_rollback")

		self.db.rollback = _rollback

	def get_doc(self, payload):
		invoice = super().get_doc(payload)
		if self.deadlock:

			def _raise():
				self._savepoint_lost = True
				raise self.QueryDeadlockError("Deadlock found when trying to get lock")

			invoice.insert = _raise
		return invoice


class TestDeadlockIsTransient:
	"""RT-1312 F3: a database deadlock on insert is retryable, never a permanent rejection."""

	def test_deadlock_on_insert_acks_failed_transient_then_posts(self, load_glue):
		fake = _DeadlockFrappe()
		glue = load_glue(fake)
		client = _Client()

		assert _post(glue, _sale_post(30), client)[0] == "failed_transient"
		fake.deadlock = False
		assert _post(glue, _sale_post(32), client)[0] == "posted"

		assert [a["ack"]["outcome"] for a in client.acks] == ["failed_transient", "posted"]
		assert client.acks[0]["key"] == f"{_SALE_REF}:failed_transient:30"
		assert fake.events.count("submit") == 1
		# The lost savepoint did not mask the deadlock: the transaction was fully rolled back.
		assert fake.events.count("full_rollback") == 1


class TestTaxedSaleIsAValidationRejection:
	"""RT-1312 F5 / D3: a forward sale with a non-zero line ``taxAmount`` acks validation, inserts nothing."""

	def test_taxed_sale_acks_permanently_rejected_validation(self, load_glue):
		fake = _Frappe()
		outcome, client, _store = _post(load_glue(fake), _sale_post(40, tax_amount="14.00"))
		assert outcome == "permanently_rejected"
		assert client.acks[0]["ack"]["reason"]["category"] == "validation"
		assert fake.payloads == [] and "insert" not in fake.events


class _ReadFailFrappe(_Frappe):
	"""RT-1312 Phase 2 item 6 (RT-1125 R-04): a transient database error in a READ stage.

	``fail`` names the one read that raises a transient error; every other read answers normally.
	Frappe v16.52.0 raises ``QueryTimeoutError`` for a lock-wait timeout (MariaDB 1205, ``is_timedout``
	in ``frappe/database/database.py`` ``sql()``) and ``QueryDeadlockError`` for a deadlock.
	"""

	def __init__(self, fail, *, exc=None, originals=None):
		super().__init__(originals=originals)
		self.QueryTimeoutError = type("QueryTimeoutError", (Exception,), {})
		self.QueryDeadlockError = type("QueryDeadlockError", (Exception,), {})
		self.fail = fail
		self.exc = exc or self.QueryTimeoutError("(1205, 'Lock wait timeout exceeded')")
		self.duplicate = False
		self.utils = types.SimpleNamespace(get_system_timezone=self._timezone)

	def _raise_if(self, read):
		if self.fail == read:
			raise self.exc

	def _timezone(self):
		self._raise_if("timezone")
		return "UTC"

	def _get_value(self, doctype, filters, fields, **kwargs):
		if isinstance(filters, dict):
			if filters.get("rt_external_id") == _ORIGINAL_EXTERNAL_ID:
				self._raise_if("resolve_original")
			else:
				self._raise_if("find_posted")
		elif fields == ["posting_date", "posting_time"]:
			self._raise_if("original_posting")
		elif isinstance(fields, list) and "update_stock" in fields:
			self._raise_if("original_invoice")
		return super()._get_value(doctype, filters, fields, **kwargs)

	def get_all(self, doctype, **kwargs):
		if doctype == "Item":
			self._raise_if("item_tracking")
		return super().get_all(doctype, **kwargs)

	def get_doc(self, payload):
		invoice = super().get_doc(payload)
		if self.duplicate:

			def _duplicate():
				raise self.exceptions.UniqueValidationError("Duplicate entry for unique_rt_si_provenance")

			invoice.insert = _duplicate
		return invoice


class _ReadFailStore(_Store):
	def __init__(self, exc):
		super().__init__()
		self.exc = exc

	def get_document_ref(self, key):
		raise self.exc


def _assert_transient_only(client, fake, ref, cursor):
	assert [a["ack"]["outcome"] for a in client.acks] == ["failed_transient"]
	assert client.acks[0]["key"] == f"{ref}:failed_transient:{cursor}"
	assert "insert" not in fake.events and "submit" not in fake.events
	(signal,) = [r for r in fake._logger.records if r["event"] == "posting.transient"]
	assert signal["work_item_ref"] == ref


class TestReadStageTransientIsRetryable:
	"""RT-1312 Phase 2 item 6: a transient error in a read stage acks ``failed_transient``.

	Before it, the read stages mapped any error to ``permanently_rejected / other`` (a sale stranded
	behind a repair), and the unwrapped reads raised out of ``post_work_item`` and aborted the page.
	"""

	@pytest.mark.parametrize(
		("fail", "make", "ref"),
		[
			("timezone", lambda: _sale_post(40), _SALE_REF),  # _build_or_reject via _posting_stamp
			("item_tracking", lambda: _sale_post(40), _SALE_REF),  # _guard_tracked_items
			("resolve_original", lambda: _reversal("void", 40), _REVERSAL_REF),  # _resolve_original_invoice
			("original_posting", lambda: _reversal("void", 40), _REVERSAL_REF),  # _read_original_posting
			("original_invoice", lambda: _reversal("void", 40), _REVERSAL_REF),  # _read_original_invoice
			("original_invoice", lambda: _reversal("return", 40), _REVERSAL_REF),
		],
	)
	def test_read_stage_lock_wait_acks_failed_transient(self, load_glue, fail, make, ref):
		fake = _ReadFailFrappe(fail, originals={_ORIGINAL_EXTERNAL_ID: _ORIGINAL})
		outcome, client, _store = _post(load_glue(fake), make())
		assert outcome == "failed_transient"
		_assert_transient_only(client, fake, ref, 40)

	def test_read_stage_deadlock_acks_failed_transient(self, load_glue):
		fake = _ReadFailFrappe("item_tracking")
		fake.exc = fake.QueryDeadlockError("(1213, 'Deadlock found when trying to get lock')")
		outcome, client, _store = _post(load_glue(fake), _sale_post(41))
		assert outcome == "failed_transient"
		_assert_transient_only(client, fake, _SALE_REF, 41)

	@pytest.mark.parametrize(
		("make", "ref"), [(lambda: _sale_post(42), _SALE_REF), (lambda: _reversal("void", 42), _REVERSAL_REF)]
	)
	def test_replay_guard_read_acks_failed_transient(self, load_glue, make, ref):
		fake = _ReadFailFrappe(None, originals={_ORIGINAL_EXTERNAL_ID: _ORIGINAL})
		glue = load_glue(fake)
		store = _ReadFailStore(fake.QueryTimeoutError("(1205, 'Lock wait timeout exceeded')"))
		outcome, client, _store = _post(glue, make(), store=store)
		assert outcome == "failed_transient"
		_assert_transient_only(client, fake, ref, 42)
		assert fake.payloads == []  # nothing built before the replay guard answers

	def test_duplicate_recovery_lookup_acks_failed_transient(self, load_glue):
		# _find_posted_invoice inside _recover_duplicate_provenance (the G5 crash-window recovery).
		fake = _ReadFailFrappe("find_posted")
		fake.duplicate = True
		outcome, client, store = _post(load_glue(fake), _sale_post(43))
		assert outcome == "failed_transient"
		assert [a["ack"]["outcome"] for a in client.acks] == ["failed_transient"]
		assert client.acks[0]["key"] == f"{_SALE_REF}:failed_transient:43"
		assert store.writes == [] and "submit" not in fake.events

	def test_unsupported_reversal_lookup_acks_failed_transient(self, load_glue):
		# _find_posted_invoice inside _recover_or_reject_unsupported (RT-71 amount-only refund).
		fake = _ReadFailFrappe("find_posted", originals={_ORIGINAL_EXTERNAL_ID: _ORIGINAL})
		outcome, client, store = _post(load_glue(fake), _reversal("refund", 44))
		assert outcome == "failed_transient"
		_assert_transient_only(client, fake, _REVERSAL_REF, 44)
		assert store.writes == []

	def test_non_transient_read_error_is_still_other(self, load_glue):
		# Only the transient set changes; an unclassified read error stays a permanent ``other``.
		fake = _ReadFailFrappe(
			"original_invoice", exc=RuntimeError("boom"), originals={_ORIGINAL_EXTERNAL_ID: _ORIGINAL}
		)
		outcome, client, _store = _post(load_glue(fake), _reversal("void", 45))
		assert outcome == "permanently_rejected"
		assert client.acks[0]["ack"]["reason"]["category"] == "other"

	def test_read_stage_transient_then_posts_on_re_offer(self, load_glue):
		fake = _ReadFailFrappe("original_posting", originals={_ORIGINAL_EXTERNAL_ID: _ORIGINAL})
		glue = load_glue(fake)
		client = _Client()
		assert _post(glue, _reversal("void", 46), client)[0] == "failed_transient"
		fake.fail = None
		assert _post(glue, _reversal("void", 48), client)[0] == "posted"
		assert [a["key"] for a in client.acks] == [
			f"{_REVERSAL_REF}:failed_transient:46",
			f"{_REVERSAL_REF}:posted",
		]
