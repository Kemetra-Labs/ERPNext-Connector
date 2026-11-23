# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-419 — the real glue posts a credit / third-party sale to the payer's Customer (stand-in frappe, no bench).

Covers what the pure builder cannot: the payer Customer read (exists under exactly that name, not disabled,
not frozen, transient error retried), the pre-submit partial-settlement check, the ack of every fail-closed
case as ``permanently_rejected`` / ``validation`` (never the store's Customer), and the replay rebuild of an
existing payer invoice. The bench proof is ``tests/test_rt419_payer_posting_db.py``.
"""

import pytest

from retail_tower_erpnext_connector.connector.posting import contracts as c
from retail_tower_erpnext_connector.connector.posting.representation_check import persisted_from_doc
from retail_tower_erpnext_connector.connector.posting.resolution_check import (
	InvoiceSnapshot,
	check_replayed_representation,
)
from retail_tower_erpnext_connector.tests.posting.test_representation_glue import (
	_COMPANY,
	_STORE,
	_erp_view,
	_Frappe,
	_post,
)
from retail_tower_erpnext_connector.tests.posting.test_representation_glue import (
	load_glue as _shared_load_glue,  # noqa: F401  (registered under this name as the base fixture)
)


@pytest.fixture
def load_glue(request):
	"""The shared stand-in-frappe glue loader (``test_representation_glue``)."""
	return request.getfixturevalue("_shared_load_glue")


_PAYER = "Acme Insurance"
_REF = "41941941-9419-4419-8419-419419419419"
_OK_CUSTOMER = {"name": _PAYER, "disabled": 0, "is_frozen": 0}


def _work_item(*, tenders=None, responsibilities=None):
	sale = {
		"saleRef": "22222222-2222-4222-8222-222222222222",
		"storeId": _STORE,
		"currencyCode": "EGP",
		"posTotal": "19.00",
		"occurredAt": "2026-06-01T10:00:00Z",
		"businessDate": "2026-06-01",
		"sourceSystem": "pos-pulse",
		"externalId": "POS-41901",
		"lines": [
			{
				"lineRef": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
				"lineName": "Item A",
				"unitPrice": "9.50",
				"currencyCode": "EGP",
				"quantity": "2",
				"lineAmount": "19.00",
				"unit": "each",
				"erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"},
			}
		],
	}
	if tenders is not None:
		sale["tenders"] = tenders
	if responsibilities is not None:
		sale["responsibilities"] = responsibilities
	return c.PostingWorkItem.from_wire(
		{
			"workItemRef": _REF,
			"kind": "sale_post",
			"sourceSystem": "pos-pulse",
			"externalId": "POS-41901",
			"payloadHash": "b" * 64,
			"businessDate": "2026-06-01",
			"itemCursor": "11",
			"resolutionVersion": 2,
			"sale": sale,
		}
	)


def _credit(amount="19.00"):
	return [{"payerExternalRef": _PAYER, "amount": amount, "sequence": 1}]


def _fake(customer=_OK_CUSTOMER, **kw):
	"""The shared stand-in, with a ``Customer`` read (``customer``: the row, ``None`` or an exception)."""
	fake = _Frappe(**kw)
	reads = []

	def get_value(doctype, filters, fields=None, **_):
		if doctype != "Customer":
			return None  # no provenance hit
		reads.append((filters, fields))
		if isinstance(customer, BaseException):
			raise customer
		return customer

	fake.db.get_value = get_value
	fake.customer_reads = reads
	return fake


class _Doc(dict):
	"""A stored Sales Invoice: a mapping with ``get``, like the Frappe document the glue reads."""


def _reason(client):
	return client.acks[0]["ack"]["reason"]


class TestPostsToThePayer:
	def test_full_credit_posts_an_unpaid_invoice_on_the_payer(self, load_glue):
		fake = _fake()
		outcome, client, store = _post(load_glue(fake), _work_item(responsibilities=_credit()))
		assert outcome == "posted"
		payload = fake.payloads[0]
		assert payload["customer"] == _PAYER
		assert "is_pos" not in payload and "payments" not in payload
		assert fake.customer_reads == [(_PAYER, ["name", "disabled", "is_frozen"])]
		assert fake.events == ["savepoint", "insert", "submit", "reload"]
		assert client.acks[0]["ack"]["outcome"] == "posted"
		assert len(store.writes) == 1

	def test_cash_plus_credit_posts_a_part_paid_invoice_on_the_payer(self, load_glue):
		fake = _fake()
		work_item = _work_item(
			tenders=[{"method": "cash", "amount": "4.00"}], responsibilities=_credit("15.00")
		)
		outcome, _client, _store = _post(load_glue(fake), work_item)
		assert outcome == "posted"
		payload = fake.payloads[0]
		assert payload["customer"] == _PAYER
		assert (payload["is_pos"], payload["payments"]) == (
			1,
			[{"mode_of_payment": "Cash", "amount": "4.00"}],
		)

	def test_a_sale_without_responsibilities_reads_no_customer_and_uses_the_store_map(self, load_glue):
		fake = _fake()
		outcome, _client, _store = _post(load_glue(fake), _work_item())
		assert outcome == "posted"
		assert fake.payloads[0]["customer"] == "Walk-in"
		assert fake.customer_reads == []


class TestFailsClosed:
	@pytest.mark.parametrize(
		"customer",
		[
			None,
			{"name": _PAYER, "disabled": 1, "is_frozen": 0},
			{"name": _PAYER, "disabled": 0, "is_frozen": 1},
			{"name": _PAYER.upper(), "disabled": 0, "is_frozen": 0},
		],
	)
	def test_a_missing_disabled_frozen_or_case_folded_payer_is_rejected_as_validation(
		self, load_glue, customer
	):
		fake = _fake(customer=customer)
		outcome, client, store = _post(load_glue(fake), _work_item(responsibilities=_credit()))
		assert outcome == "permanently_rejected"
		assert _reason(client)["category"] == "validation"
		assert _reason(client)["message"].startswith("UnmappedPayer:")
		assert fake.payloads == [] and fake.events == []  # nothing inserted, nothing submitted
		assert store.writes == []

	def test_a_transient_error_reading_the_payer_is_retried_not_rejected(self, load_glue):
		fake = _fake(customer=TimeoutError("Lock wait timeout exceeded"))
		outcome, client, store = _post(load_glue(fake), _work_item(responsibilities=_credit()))
		assert outcome == "failed_transient"
		assert client.acks[0]["ack"]["outcome"] == "failed_transient"
		assert store.writes == []

	def test_a_payer_disabled_after_the_invoice_was_submitted_recovers_it_instead_of_rejecting(
		self, load_glue
	):
		# A crash after submit left no Posting Log row, then the payer was disabled: the re-offer must echo
		# the submitted invoice (a rejection would leave ERPNext holding what Backend-Core is told was refused).
		work_item = _work_item(responsibilities=_credit())
		first = _fake()
		assert _post(load_glue(first), work_item)[0] == "posted"
		submitted = {**_erp_view(first.payloads[0]), "docstatus": 1}
		submitted["items"] = [
			{**row, "rt_line_ref": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"} for row in submitted["items"]
		]

		fake = _fake(customer={"name": _PAYER, "disabled": 1, "is_frozen": 0})
		customer_lookup = fake.db.get_value

		def get_value(doctype, filters, fields=None, **kw):
			if doctype == "Sales Invoice":
				return "ACC-SINV-RECOVER"
			return customer_lookup(doctype, filters, fields, **kw)

		fake.db.get_value = get_value
		fake.get_doc = lambda *args: _Doc(submitted)
		outcome, client, store = _post(load_glue(fake), work_item)
		assert outcome == "posted", client.acks
		assert client.acks[0]["ack"]["documentRef"]["name"] == "ACC-SINV-RECOVER"
		assert fake.payloads == []  # nothing inserted
		assert [ref.name for _key, ref in store.writes] == [
			"ACC-SINV-RECOVER"
		]  # the Posting Log is back-filled

	def test_a_total_that_does_not_cover_the_lines_is_rejected_before_insert(self, load_glue):
		fake = _fake()
		outcome, client, _store = _post(load_glue(fake), _work_item(responsibilities=_credit("18.99")))
		assert outcome == "permanently_rejected"
		assert _reason(client)["category"] == "validation"
		assert _reason(client)["message"].startswith("CoverageMismatch:")
		assert fake.payloads == []

	@pytest.mark.parametrize(
		"mutate",
		[
			lambda doc: doc.update(outstanding_amount=19.0, paid_amount=0.0),  # the payment row was dropped
			lambda doc: doc.update(outstanding_amount=0.0, paid_amount=19.0),  # ERPNext settled it in full
			lambda doc: doc.update(write_off_amount=0.01),
			lambda doc: doc.update(change_amount=1.0),
		],
	)
	def test_a_split_other_than_requested_is_settlement_drift_rolled_back(self, load_glue, mutate):
		fake = _fake(on_insert=mutate)
		work_item = _work_item(
			tenders=[{"method": "cash", "amount": "4.00"}], responsibilities=_credit("15.00")
		)
		outcome, client, store = _post(load_glue(fake), work_item)
		assert outcome == "permanently_rejected"
		assert fake.events == ["savepoint", "insert", "rollback"]
		assert _reason(client)["category"] == "validation"
		assert _reason(client)["message"].startswith("SettlementDrift:")
		assert store.writes == []

	def test_a_full_credit_invoice_that_comes_back_paid_is_drift(self, load_glue):
		fake = _fake(on_insert=lambda doc: doc.update(paid_amount=19.0, outstanding_amount=0.0))
		outcome, client, _store = _post(load_glue(fake), _work_item(responsibilities=_credit()))
		assert outcome == "permanently_rejected"
		assert _reason(client)["message"].startswith("SettlementDrift:")

	def test_a_reversal_of_a_payer_sale_is_never_posted(self, load_glue):
		wire = {
			"workItemRef": _REF,
			"kind": "reversal",
			"sourceSystem": "pos-pulse",
			"externalId": "POS-41901-VOID",
			"payloadHash": "b" * 64,
			"businessDate": "2026-06-02",
			"itemCursor": "12",
			"reversalOf": {
				"sourceSystem": "pos-pulse",
				"externalId": "POS-41901",
				"reversalKind": "void",
				"recordedAt": "2026-06-02T10:00:00Z",
				"businessDate": "2026-06-02",
			},
			"sale": {
				**_sale_wire(),
				"responsibilities": _credit(),
			},
		}
		fake = _fake()
		outcome, client, _store = _post(load_glue(fake), c.PostingWorkItem.from_wire(wire))
		assert outcome == "permanently_rejected"
		assert _reason(client)["category"] == "validation"
		assert "responsib" in _reason(client)["message"]
		assert fake.payloads == []


def _sale_wire():
	wire = _work_item().sale
	return {
		"saleRef": wire.sale_ref,
		"storeId": wire.store_id,
		"currencyCode": wire.currency_code,
		"posTotal": wire.pos_total,
		"occurredAt": wire.occurred_at,
		"businessDate": wire.business_date,
		"sourceSystem": wire.source_system,
		"externalId": wire.external_id,
		"lines": [
			{
				"lineName": line.line_name,
				"unitPrice": line.unit_price,
				"currencyCode": line.currency_code,
				"quantity": line.quantity,
				"lineAmount": line.line_amount,
				"unit": line.unit,
				"erpnextItemRef": {"doctype": "Item", "name": line.erpnext_item_ref.name},
			}
			for line in wire.lines
		],
	}


class TestReplayOfAnExistingPayerInvoice:
	"""A re-offered, already-posted payer sale is verified against its frozen debtor, never re-posted."""

	def _expected(self, load_glue, work_item):
		glue = load_glue(_fake(customer=RuntimeError("a replay must not read a Customer")))
		return glue._replay_expected(work_item, _COMPANY)

	def _snapshot(self, work_item, customer):
		payload = {
			"currency": "EGP",
			"customer": customer,
			"company": _COMPANY,
			"ignore_pricing_rule": 1,
			"items": [
				{
					"item_code": "ITEM-A",
					"qty": "2",
					"uom": "Nos",
					"rate": "9.50",
					"amount": "19.00",
					"warehouse": "Store WH",
					"price_list_rate": "9.50",
				}
			],
		}
		doc = {**_erp_view(payload), "customer": customer}
		return InvoiceSnapshot(docstatus=1, items=(), representation=persisted_from_doc(doc))

	def test_the_rebuild_of_a_cash_plus_credit_sale_needs_no_customer_read_and_keeps_the_payer(
		self, load_glue
	):
		work_item = _work_item(
			tenders=[{"method": "cash", "amount": "4.00"}], responsibilities=_credit("15.00")
		)
		expected = self._expected(load_glue, work_item)  # no CoverageMismatch: the tenders are kept
		assert expected.customer == _PAYER

	def test_an_invoice_on_the_payer_matches(self, load_glue):
		work_item = _work_item(responsibilities=_credit())
		expected = self._expected(load_glue, work_item)
		assert check_replayed_representation(self._snapshot(work_item, _PAYER), expected) is None

	@pytest.mark.parametrize("wrong", ["Walk-in", "Some Other Payer"])
	def test_an_invoice_on_another_debtor_is_a_mismatch(self, load_glue, wrong):
		work_item = _work_item(responsibilities=_credit())
		expected = self._expected(load_glue, work_item)
		reason = check_replayed_representation(self._snapshot(work_item, wrong), expected)
		assert reason is not None and reason.startswith("representation:") and "customer" in reason

	def test_a_sale_without_responsibilities_still_exempts_the_customer_on_replay(self, load_glue):
		work_item = _work_item(tenders=[{"method": "cash", "amount": "19.00"}])
		expected = self._expected(load_glue, work_item)
		assert expected.customer is None
