# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1359 — site-side operations that can be checked without a site, against a minimal frappe
stand-in: the replacement Administrator password (OD-9) is random, used by nothing else and appears
in no output; the no-commit switch the bench-lane test relies on."""

import json
import logging
import sys
import types

import pytest

from retail_tower_erpnext_connector.provisioner import site_ops


class _Flags(dict):
	pass


@pytest.fixture
def stub(monkeypatch):
	state = types.SimpleNamespace(passwords=[], commits=0)
	frappe = types.ModuleType("frappe")
	frappe.flags = _Flags()
	frappe.db = types.SimpleNamespace(commit=lambda: setattr(state, "commits", state.commits + 1))
	utils = types.ModuleType("frappe.utils")
	password = types.ModuleType("frappe.utils.password")

	def update_password(user, pwd, logout_all_sessions=False):
		state.passwords.append((user, pwd, logout_all_sessions))

	password.update_password = update_password
	frappe.utils = utils
	utils.password = password
	monkeypatch.setitem(sys.modules, "frappe", frappe)
	monkeypatch.setitem(sys.modules, "frappe.utils", utils)
	monkeypatch.setitem(sys.modules, "frappe.utils.password", password)
	state.frappe = frappe
	return state


def test_the_replacement_admin_password_is_random_single_use_and_in_no_output(stub, capsys, caplog):
	caplog.set_level(logging.DEBUG)
	first = site_ops.run({"op": "reset_admin_password"})
	second = site_ops.run({"op": "reset_admin_password"})
	(user1, pw1, logout1), (_, pw2, _) = stub.passwords
	assert user1 == "Administrator" and logout1 is True  # every session of the throwaway value ends
	assert pw1 != pw2 and len(pw1) >= 32
	out = capsys.readouterr()
	recorded = " ".join([json.dumps(first), json.dumps(second), out.out, out.err, caplog.text])
	assert pw1 not in recorded and pw2 not in recorded
	assert first == {"ok": True}


def test_nothing_is_committed_when_the_caller_runs_its_own_transaction(stub):
	site_ops._commit()
	assert stub.commits == 1
	stub.frappe.flags["rt_provisioner_no_commit"] = True
	site_ops._commit()
	assert stub.commits == 1


def test_an_unknown_operation_is_rejected():
	with pytest.raises(site_ops.OpError):
		site_ops.run({"op": "drop_everything"})


# --- store warehouses and posting currency, against a minimal frappe stand-in ---------------------


class _Warehouses:
	"""``frappe.get_all`` / ``get_doc`` / ``db.get_value`` for Warehouse rows only."""

	def __init__(self, rows):
		self.rows = rows
		self.inserted = []

	def get_all(self, doctype, filters, fields, limit):
		hits = [
			r
			for r in self.rows
			if r["warehouse_name"] == filters["warehouse_name"] and r["company"] == filters["company"]
		]
		return [{k: r[k] for k in fields} for r in hits[:limit]]

	def get_doc(self, data):
		store = self

		class Doc:
			name = f"{data['warehouse_name']} - RT"

			def insert(self):
				store.rows.append({**data, "name": self.name, "is_group": 0, "disabled": 0})
				store.inserted.append(self.name)

		return Doc()


@pytest.fixture
def warehouses(monkeypatch):
	rows = [
		{
			"name": "All Warehouses - RT",
			"warehouse_name": "All Warehouses",
			"company": "Co",
			"is_group": 1,
			"disabled": 0,
		},
	]
	store = _Warehouses(rows)
	frappe = types.ModuleType("frappe")
	frappe.get_all = store.get_all
	frappe.get_doc = store.get_doc
	frappe.db = types.SimpleNamespace(get_value=lambda *a, **k: "All Warehouses - RT")
	monkeypatch.setitem(sys.modules, "frappe", frappe)
	return store


def _store(store_id, name):
	return {"storeId": store_id, "displayName": name}


def test_a_store_named_like_a_group_warehouse_gets_a_leaf_warehouse(warehouses):
	name = site_ops._warehouse_for(_store("5f1e2a3b-4c5d-4e6f-8a9b-0c1d2e3f4a5b", "All Warehouses"), "Co", {})
	assert name != "All Warehouses - RT"
	assert name.startswith("All Warehouses (5f1e2a3b)")


def test_a_suffixed_name_owned_by_another_store_is_not_shared(warehouses):
	a = _store("aaaaaaaa-0000-4000-8000-000000000001", "Main")
	b = _store("bbbbbbbb-0000-4000-8000-000000000002", "Main")
	c = _store("bbbbbbbb-0000-4000-8000-000000000003", "Main")  # same first eight characters as b
	taken = {}
	for store in (a, b, c):
		taken[site_ops._warehouse_for(store, "Co", taken)] = store["storeId"]
	assert len(set(taken)) == 3


def test_an_unmapped_leaf_warehouse_from_an_earlier_run_is_reused(warehouses):
	store = _store("aaaaaaaa-0000-4000-8000-000000000001", "Main")
	first = site_ops._warehouse_for(store, "Co", {})
	again = site_ops._warehouse_for(store, "Co", {})
	assert first == again and warehouses.inserted == [first]


def test_a_disabled_warehouse_is_skipped(warehouses):
	warehouses.rows.append(
		{"name": "Main - RT", "warehouse_name": "Main", "company": "Co", "is_group": 0, "disabled": 1}
	)
	name = site_ops._warehouse_for(_store("aaaaaaaa-0000-4000-8000-000000000001", "Main"), "Co", {})
	assert name != "Main - RT"


def test_bootstrap_sets_the_posting_currency_from_the_company_once(monkeypatch):
	class Settings:
		def __init__(self):
			self.posting_currency = None
			self.warehouse_map = []
			self.store_customer_map = []

		def append(self, table, row):
			getattr(self, table).append(types.SimpleNamespace(**row))

		def save(self):
			pass

	settings = Settings()
	frappe = types.ModuleType("frappe")
	frappe.flags = _Flags()
	frappe.flags["rt_provisioner_no_commit"] = True
	frappe.get_single = lambda name: settings
	frappe.db = types.SimpleNamespace(
		exists=lambda *a, **k: True,
		get_value=lambda doctype, filters=None, fieldname=None, **k: (
			"EGP" if doctype == "Company" else "Retail Walk-in"
		),
	)
	monkeypatch.setitem(sys.modules, "frappe", frappe)
	monkeypatch.setattr(site_ops, "_warehouse_for", lambda store, company, taken: "W - RT")
	args = {
		"company": "Co",
		"walk_in_customer": "Retail Walk-in",
		"stores": [_store("a" * 8 + "-0000-4000-8000-000000000001", "Main")],
	}
	site_ops.bootstrap_stores(args)
	assert settings.posting_currency == "EGP"
	settings.posting_currency = "USD"  # an operator's value is never overwritten
	site_ops.bootstrap_stores(args)
	assert settings.posting_currency == "USD"


def _bootstrap_settings(monkeypatch, tender_rows):
	class Settings:
		def __init__(self):
			self.posting_currency = "EGP"
			self.warehouse_map = []
			self.store_customer_map = []
			self.tender_mode_map = list(tender_rows)

		def append(self, table, row):
			getattr(self, table).append(types.SimpleNamespace(**row))

		def save(self):
			pass

	settings = Settings()
	frappe = types.ModuleType("frappe")
	frappe.flags = _Flags()
	frappe.flags["rt_provisioner_no_commit"] = True
	frappe.get_single = lambda name: settings
	frappe.db = types.SimpleNamespace(exists=lambda *a, **k: True, get_value=lambda *a, **k: "EGP")
	monkeypatch.setitem(sys.modules, "frappe", frappe)
	return settings


def test_bootstrap_maps_cash_to_the_template_mode_of_payment_with_no_stores(monkeypatch):
	settings = _bootstrap_settings(monkeypatch, [])
	args = {
		"company": "Co",
		"walk_in_customer": "Retail Walk-in",
		"stores": [],
		"cash_mode_of_payment": "Cash",
	}
	site_ops.bootstrap_stores(args)
	site_ops.bootstrap_stores(args)  # get-or-create: no second row
	assert [(r.tender_method, r.mode_of_payment) for r in settings.tender_mode_map] == [("cash", "Cash")]


def test_bootstrap_never_replaces_an_operators_cash_mapping(monkeypatch):
	row = types.SimpleNamespace(tender_method="cash", mode_of_payment="Cash on Hand")
	settings = _bootstrap_settings(monkeypatch, [row])
	site_ops.bootstrap_stores(
		{"company": "Co", "walk_in_customer": "x", "stores": [], "cash_mode_of_payment": "Cash"}
	)
	assert [(r.tender_method, r.mode_of_payment) for r in settings.tender_mode_map] == [
		("cash", "Cash on Hand")
	]


# --- RT-1362 S3: the pending-slot write and the confirmation call ----------------------------------

_STAGE_ARGS = {
	"registration_id": "reg-1",
	"credential_id": "cred-new",
	"issued_at": "2026-11-27T10:00:00Z",
	"expires_at": "2027-02-25T10:00:00Z",
}


@pytest.fixture
def slots(monkeypatch, stub):
	from retail_tower_erpnext_connector.connector import credential_slots

	calls, promoted, probes = [], [], []
	state = types.SimpleNamespace(occupant=None, status=401)
	monkeypatch.setattr(credential_slots, "has_pending_slot", lambda: True)
	monkeypatch.setattr(credential_slots, "stage_pending", lambda **kw: calls.append(kw) or "write")
	monkeypatch.setattr(credential_slots, "occupant_to_settle", lambda cid, issued: state.occupant)
	monkeypatch.setattr(credential_slots, "promote_pending", lambda p: promoted.append(p) or True)
	sender = types.ModuleType("retail_tower_erpnext_connector.connector.health.sender")
	sender.probe_credential = lambda secret: probes.append(secret) or state.status
	monkeypatch.setitem(sys.modules, "retail_tower_erpnext_connector.connector.health.sender", sender)
	return types.SimpleNamespace(
		module=credential_slots, calls=calls, stub=stub, state=state, promoted=promoted, probes=probes
	)


def test_the_pending_write_passes_the_secret_from_stdin_only_and_commits(slots):
	args = {**_STAGE_ARGS, "deadline": "2026-11-27T10:15:00Z"}
	out = site_ops.run({"op": "stage_pending_credential", "args": args, "secret": "s3"})
	assert out == {"ok": True, "staged": "write"}
	assert slots.calls == [{**_STAGE_ARGS, "deadline": "2026-11-27T10:15:00Z", "secret": "s3"}]
	assert slots.stub.commits == 1 and slots.probes == []  # an empty slot needs no settling


def _occupant(slots):
	slots.state.occupant = slots.module.PendingCredential(
		"cred-A", "occupant-secret", "2026-11-27T09:00:00Z", "2027-02-25T09:00:00Z"
	)


def test_an_accepted_occupant_is_promoted_before_the_write(slots):
	_occupant(slots)
	slots.state.status = 200
	out = site_ops.run({"op": "stage_pending_credential", "args": dict(_STAGE_ARGS), "secret": "s3"})
	assert out["staged"] == "write"
	assert slots.probes == ["occupant-secret"]
	assert [p.credential_id for p in slots.promoted] == ["cred-A"] and len(slots.calls) == 1


def test_a_revoked_occupant_is_overwritten_without_promotion(slots):
	_occupant(slots)
	slots.state.status = 401
	assert site_ops.run({"op": "stage_pending_credential", "args": dict(_STAGE_ARGS), "secret": "s3"})[
		"staged"
	] == ("write")
	assert slots.promoted == [] and len(slots.calls) == 1


@pytest.mark.parametrize("status", [None, 404, 429, 500])
def test_an_occupant_that_cannot_be_settled_defers_the_write(slots, status):
	_occupant(slots)
	slots.state.status = status
	out = site_ops.run({"op": "stage_pending_credential", "args": dict(_STAGE_ARGS), "secret": "s3"})
	assert out == {"ok": True, "staged": "deferred"}
	assert slots.calls == [] and slots.promoted == []


def test_a_site_without_the_pending_slot_is_a_compatibility_error(slots, monkeypatch):
	monkeypatch.setattr(slots.module, "has_pending_slot", lambda: False)
	with pytest.raises(site_ops.OpError) as err:
		site_ops.run({"op": "stage_pending_credential", "args": dict(_STAGE_ARGS), "secret": "s3"})
	assert err.value.error_class == "compatibility" and slots.calls == []


def test_another_registration_is_a_conflict_and_no_secret_is_a_rejection(slots, monkeypatch):
	def other(**kw):
		raise LookupError("x")

	monkeypatch.setattr(slots.module, "stage_pending", other)
	with pytest.raises(site_ops.OpError) as err:
		site_ops.run({"op": "stage_pending_credential", "args": dict(_STAGE_ARGS), "secret": "s3"})
	assert err.value.error_class == "conflict"
	with pytest.raises(site_ops.OpError) as err:
		site_ops.run({"op": "stage_pending_credential", "args": dict(_STAGE_ARGS)})
	assert err.value.error_class == "permanently_rejected"


def test_the_confirmation_makes_one_heartbeat_then_reports_both_slots(slots, monkeypatch):
	beats = []
	sender = types.ModuleType("retail_tower_erpnext_connector.connector.health.sender")
	sender.run_heartbeat = lambda: beats.append(1)
	monkeypatch.setitem(sys.modules, "retail_tower_erpnext_connector.connector.health.sender", sender)
	ids = {"current_credential_id": "cred-new", "pending_credential_id": None, "pending_issued_at": None}
	monkeypatch.setattr(slots.module, "slot_ids", lambda: dict(ids))
	assert site_ops.run({"op": "confirm_credential", "args": {"credential_id": "cred-new"}}) == {
		"ok": True,
		**ids,
	}
	assert beats == [1]
