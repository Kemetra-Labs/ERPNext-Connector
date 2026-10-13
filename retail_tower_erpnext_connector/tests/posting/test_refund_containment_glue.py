# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-71 — executable coverage of the refund containment in the REAL glue (no bench).

``frappe_glue`` imports frappe, so it is loaded here against a minimal stand-in module that
provides only what the refund path touches: ``db.get_value`` (the provenance lookup), ``logger``,
``ValidationError`` and ``exceptions``. ``get_doc`` fails the test if called, which proves no
Sales Invoice is built or submitted. The stand-in is removed afterwards, and any real ``frappe`` /
``frappe_glue`` modules are restored.

Cases (Greptile P2 + Codex P1, PR #46):
  - a refund with nothing posted is rejected as ``validation`` and posts nothing;
  - a legacy refund whose invoice exists but whose Posting Log row is missing (crash window) is
    recovered and acked ``posted``, never rejected;
  - an already-recorded legacy refund still replays its document.
"""

import importlib
import sys
import types

import pytest

from retail_tower_erpnext_connector.connector.posting import contracts as c
from retail_tower_erpnext_connector.connector.posting.contracts import ErpnextDocumentRef

_GLUE = "retail_tower_erpnext_connector.connector.posting.frappe_glue"
_PKG = "retail_tower_erpnext_connector.connector.posting"
_MISSING = object()


class _Logger:
	def __init__(self):
		self.records = []

	def info(self, record):
		self.records.append(("info", record))

	def warning(self, record):
		self.records.append(("warning", record))

	def error(self, record):
		self.records.append(("error", record))


class _Frappe(types.ModuleType):
	def __init__(self, posted_name=None):
		super().__init__("frappe")
		self.ValidationError = type("ValidationError", (Exception,), {})
		self.exceptions = types.SimpleNamespace()
		self.lookups = []
		self._logger = _Logger()
		self.logger = lambda name=None: self._logger
		self.db = types.SimpleNamespace(get_value=self._get_value, commit=self._commit)
		self.commits = 0
		self._posted_name = posted_name

	def _commit(self):
		self.commits += 1  # RT-458

	def _get_value(self, doctype, filters, field, **_):
		if field == "company":  # RT-378: the original invoice's company, read for a replay
			return "RT Company"
		self.lookups.append((doctype, dict(filters), field))
		if filters.get("rt_external_id") == "POS-9001":
			return "ACC-SINV-2026-00001"  # the ORIGINAL sale's invoice (a replay reads its company)
		return self._posted_name

	def get_doc(self, doctype, name=None):
		if name is None:
			pytest.fail("a refund must never build or submit a Sales Invoice")
		return _legacy_refund_invoice()  # RT-378: an existing invoice is read back to verify it


def _legacy_refund_invoice():
	"""The legacy (pre-RT-71, pre-RT-377) refund credit note as ERPNext holds it."""
	row = {"item_code": "ITEM-A", "qty": -2.0, "uom": "Nos", "rate": 100.0, "amount": -200.0,
		"warehouse": "Stores - A", "price_list_rate": 100.0}
	return {"docstatus": 1, "currency": "EGP", "customer": "Walk-in", "company": "RT Company",
		"ignore_pricing_rule": 0, "grand_total": -200.0, "taxes": [], "items": [row]}


class _Client:
	def __init__(self, fake=None):
		self.acks, self._fake = [], fake

	def ack_outcome(self, ref, request, idempotency_key):
		# RT-458: how many commits had happened when the ack left (None when the test does not track it).
		commits = self._fake.commits if self._fake is not None else None
		self.acks.append({"ref": ref, "ack": request.to_wire(), "key": idempotency_key, "commits": commits})


class _Store:
	def __init__(self, recorded=None):
		self.recorded = recorded
		self.writes = []

	def get_document_ref(self, key):
		return self.recorded

	def record_posted(self, key, ref):
		self.writes.append((key, ref))


def _restore(namespace: dict, name: str, value) -> None:
	"""Put ``namespace[name]`` back to ``value``, or remove it if it was absent before the test."""
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


def _refund() -> c.PostingWorkItem:
	return c.PostingWorkItem.from_wire(
		{
			"workItemRef": "44444444-4444-4444-8444-444444444444",
			"kind": "reversal",
			"sourceSystem": "pos-pulse",
			"externalId": "POS-9001",
			"payloadHash": "b" * 64,
			"businessDate": "2026-06-05",
			"itemCursor": "cursor-2",
			"reversalOf": {"sourceSystem": "pos-pulse", "externalId": "POS-9001", "reversalKind": "refund"},
			"sale": {
				"saleRef": "22222222-2222-4222-8222-222222222222",
				"storeId": "33333333-3333-4333-8333-333333333333",
				"currencyCode": "EGP",
				"posTotal": "200.00",
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
	)


def _post(glue, store, client):
	return glue.post_work_item(
		_refund(),
		client=client,
		store=store,
		uom_map=None,
		warehouses=None,
		customers=None,
		tenders=None,
		correlation_id="rt71-test",
	)


class TestRefundContainmentInGlue:
	def test_refund_with_nothing_posted_is_rejected_and_posts_nothing(self, load_glue):
		fake = _Frappe(posted_name=None)
		glue = load_glue(fake)
		client, store = _Client(), _Store()
		assert _post(glue, store, client) == "permanently_rejected"
		(ack,) = client.acks
		assert ack["ack"]["outcome"] == "permanently_rejected"
		assert ack["ack"]["reason"]["category"] == "validation"
		assert store.writes == []
		assert fake.commits == 0  # RT-458: a rejection never commits

	def test_crash_window_legacy_refund_is_recovered_not_rejected(self, load_glue):
		# The invoice exists under this reversal's provenance, but the Posting Log row is missing.
		fake = _Frappe(posted_name="ACC-SINV-2026-00028")
		glue = load_glue(fake)
		client, store = _Client(fake), _Store()
		assert _post(glue, store, client) == "posted"
		(ack,) = client.acks
		assert ack["ack"]["outcome"] == "posted"
		assert ack["ack"]["documentRef"]["name"] == "ACC-SINV-2026-00028"
		assert [ref.name for _key, ref in store.writes] == ["ACC-SINV-2026-00028"]  # back-filled
		assert ack["commits"] == 1  # RT-458: the back-filled Posting Log row is durable before the ack
		# The recovery lookup used the reversal's own provenance (work_item_ref), never the sale's id.
		_doctype, filters, _field = fake.lookups[0]
		assert filters["rt_external_id"] == "44444444-4444-4444-8444-444444444444"
		assert filters["docstatus"] == 1

	def test_recorded_legacy_refund_still_replays(self, load_glue):
		fake = _Frappe(posted_name=None)
		glue = load_glue(fake)
		client = _Client()
		store = _Store(recorded=ErpnextDocumentRef(doctype="Sales Invoice", name="ACC-SINV-2026-00028"))
		assert _post(glue, store, client) == "posted"
		assert client.acks[0]["ack"]["documentRef"]["name"] == "ACC-SINV-2026-00028"
		# The replay guard answered first: no recovery lookup by the reversal's own provenance.
		assert all(f["rt_external_id"] != "44444444-4444-4444-8444-444444444444" for _d, f, _x in fake.lookups)
