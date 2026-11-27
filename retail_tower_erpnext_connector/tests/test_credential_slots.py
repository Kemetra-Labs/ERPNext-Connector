# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1362 S3 — dual-slot credentials in the shared transport (``provisioning-feed.yaml`` 1.2.0-draft).

The transport presents PENDING first, falls back to CURRENT on a refusal and clears nothing, promotes
PENDING on its first accepted call, and retries a refused CURRENT with the one another job promoted.
Neither fallback reaches the RT-1429 auth circuit: only a genuine refusal of every credential opens it.
``requests`` is a stand-in that answers per bearer token, so every assertion is on the calls it saw.
"""

import sys
import types

import pytest

from retail_tower_erpnext_connector.connector import auth_circuit_store, credential_slots
from retail_tower_erpnext_connector.connector.auth_circuit import (
	AuthCircuit,
	AuthRefused,
	MemoryCircuitStore,
)
from retail_tower_erpnext_connector.connector.credential_slots import (
	NOOP,
	STALE,
	WRITE,
	PendingCredential,
	read_pending,
	stage_decision,
)
from retail_tower_erpnext_connector.connector.http_transport import RequestsTransport, build_http_transport

# Built at run time and low-entropy: a secret-shaped literal in any commit keeps gitleaks red.
OLD = "old-" + "a" * 24
NEW = "new-" + "b" * 24
NEWER = "newer-" + "c" * 24


class _Response:
	def __init__(self, status):
		self.status_code = status
		self.headers = {}

	def json(self):
		return {"items": [], "cursor": "1"}

	def raise_for_status(self):
		if self.status_code >= 400:
			raise RuntimeError(f"{self.status_code} Client Error")


class _FakeRequests(types.ModuleType):
	"""Backend-Core as the bearer sees it: ``accepted`` tokens get ``status``, the rest 401."""

	def __init__(self):
		super().__init__("requests")
		self.accepted = {OLD}
		self.status = 200
		self.calls: list[tuple[str, str]] = []

	def _answer(self, method, headers):
		token = headers["Authorization"].removeprefix("Bearer ")
		self.calls.append((method, token))
		return _Response(self.status if token in self.accepted else 401)

	def get(self, url, **kwargs):
		return self._answer("GET", kwargs["headers"])

	def post(self, url, **kwargs):
		return self._answer("POST", kwargs["headers"])


@pytest.fixture
def backend(monkeypatch):
	fake = _FakeRequests()
	monkeypatch.setitem(sys.modules, "requests", fake)
	return fake


def _pending(secret=NEW, credential_id="cred-new"):
	return PendingCredential(credential_id, secret, "2026-11-27T10:00:00Z", "2027-02-25T10:00:00Z")


class _Rig:
	def __init__(self, *, pending=None, reload=None, promote_raises=False):
		self.circuit = AuthCircuit(MemoryCircuitStore(), clock=lambda: 1_000.0)
		self.opened: list[int] = []
		self.promoted: list[PendingCredential] = []
		self.slot_errors: list[str] = []

		def promote(p):
			if promote_raises:
				raise RuntimeError("lock wait timeout")
			self.promoted.append(p)
			return True

		self.transport = RequestsTransport(
			"https://dp2.example",
			OLD,
			circuit=self.circuit,
			on_open=self.opened.append,
			pending=pending,
			on_promote=promote,
			reload_token=reload,
			on_slot_error=lambda what, exc: self.slot_errors.append(what),
		)

	def post(self):
		return self.transport.post("/api/x", json={}, headers={})


# --- the transport -------------------------------------------------------------------------------


def test_an_accepted_pending_credential_is_promoted_once_and_used_from_then_on(backend):
	backend.accepted = {OLD, NEW}
	rig = _Rig(pending=_pending())
	assert rig.post().status == 200
	assert rig.post().status == 200
	assert backend.calls == [("POST", NEW), ("POST", NEW)]
	assert [p.credential_id for p in rig.promoted] == ["cred-new"]
	assert rig.circuit.blocked() is False


def test_a_refused_pending_credential_falls_back_to_current_and_never_trips_the_circuit(backend):
	rig = _Rig(pending=_pending())  # Backend-Core revoked it (a newer repeat, a failed rotation)
	assert rig.post().status == 200
	assert backend.calls == [("POST", NEW), ("POST", OLD)]
	assert rig.promoted == [] and rig.opened == []
	assert rig.circuit.blocked() is False and rig.circuit.next_probe_at() is None
	rig.post()  # not presented again by this transport
	assert backend.calls[-1] == ("POST", OLD) and len(backend.calls) == 3


def test_a_get_with_a_refused_pending_credential_does_not_raise_when_current_works(backend):
	rig = _Rig(pending=_pending())
	assert rig.transport.get("/api/x", params={}, headers={}) == {"items": [], "cursor": "1"}
	assert rig.circuit.blocked() is False


def test_only_a_refusal_of_every_credential_opens_the_circuit(backend):
	backend.accepted = set()
	rig = _Rig(pending=_pending())
	with pytest.raises(AuthRefused):
		rig.transport.get("/api/x", params={}, headers={})
	assert backend.calls == [("GET", NEW), ("GET", OLD)]
	assert rig.opened == [401]


def test_a_refused_current_is_retried_with_the_credential_another_job_promoted(backend):
	backend.accepted = {NEW}  # Backend-Core revoked OLD when it accepted NEW from another job
	rig = _Rig(reload=lambda: NEW)
	assert rig.post().status == 200
	assert backend.calls == [("POST", OLD), ("POST", NEW)]
	assert rig.opened == [] and rig.circuit.blocked() is False
	rig.post()
	assert backend.calls[-1] == ("POST", NEW)


def test_a_refused_current_with_nothing_newer_stored_is_a_genuine_refusal(backend):
	backend.accepted = set()
	rig = _Rig(reload=lambda: OLD)
	rig.post()
	assert backend.calls == [("POST", OLD)]
	assert rig.opened == [401]


def test_a_failed_promotion_write_never_fails_the_call(backend):
	backend.accepted = {OLD, NEW}
	rig = _Rig(pending=_pending(), promote_raises=True)
	assert rig.post().status == 200
	assert rig.slot_errors == ["promote"]
	assert backend.calls == [("POST", NEW)]


def test_a_failed_reload_leaves_the_refusal_to_the_circuit(backend):
	backend.accepted = set()

	def broken():
		raise RuntimeError("db down")

	rig = _Rig(reload=broken)
	rig.post()
	assert backend.calls == [("POST", OLD)]
	assert rig.opened == [401]  # reported once, by the circuit


@pytest.mark.parametrize("status", [400, 404, 409, 413, 429])
def test_only_a_2xx_promotes_a_pending_credential(backend, status):
	# An unmatched route, a body-parser refusal or an edge rate limit answers before authentication:
	# promoting on it could replace CURRENT with a revoked credential.
	backend.accepted = {OLD, NEW}
	backend.status = status
	rig = _Rig(pending=_pending())
	assert rig.post().status == status
	assert rig.promoted == [] and rig.opened == []
	backend.status = 200
	rig.post()  # still staged: presented again, promoted on the first 2xx
	assert backend.calls == [("POST", NEW), ("POST", NEW)]
	assert [p.credential_id for p in rig.promoted] == ["cred-new"]


def test_a_server_error_with_pending_proves_nothing_and_keeps_it_staged(backend):
	backend.accepted = {OLD, NEW}
	backend.status = 503
	rig = _Rig(pending=_pending())
	assert rig.post().status == 503
	backend.status = 200
	rig.post()
	assert backend.calls == [("POST", NEW), ("POST", NEW)]
	assert len(rig.promoted) == 1


# --- reading the slots ---------------------------------------------------------------------------


class _Settings:
	def __init__(self, pending_id=None, pending_secret=None, credential_id="cred-old"):
		self.dp2_base_url = "https://dp2.example"
		self.dp2_credential_id = credential_id
		self.dp2_pending_credential_id = pending_id
		self.dp2_pending_credential_issued_at = "2026-11-27T10:00:00Z" if pending_id else None
		self.dp2_pending_credential_expires_at = "2027-02-25T10:00:00Z" if pending_id else None
		self._secrets = {"dp2_token": OLD, "dp2_pending_token": pending_secret}
		self.asked: list[str] = []

	def get_password(self, fieldname, raise_exception=True):
		self.asked.append(fieldname)
		value = self._secrets.get(fieldname)
		if value is None and raise_exception:
			raise RuntimeError("Password not found")
		return value


def test_build_reads_the_pending_slot_only_when_one_is_staged(backend):
	empty = _Settings()
	build_http_transport(empty, environ={})
	assert empty.asked == ["dp2_token"]
	staged = _Settings(pending_id="cred-new", pending_secret=NEW)
	backend.accepted = {OLD, NEW}
	build_http_transport(staged, environ={}).post("/api/x", json={}, headers={})
	assert backend.calls == [("POST", NEW)]


def test_a_pending_id_without_a_secret_is_no_pending_credential():
	assert read_pending(_Settings(pending_id="cred-new", pending_secret=None)) is None
	assert read_pending(_Settings(pending_id="  ")) is None
	assert read_pending(object()) is None


def test_the_pending_credential_never_shows_its_secret():
	pending = read_pending(_Settings(pending_id="cred-new", pending_secret=NEW))
	assert pending.credential_id == "cred-new" and pending.issued_at == "2026-11-27T10:00:00Z"
	assert NEW not in repr(pending) and NEW not in str(pending)


def test_a_staged_pending_credential_is_a_new_circuit_key():
	plain = auth_circuit_store.credential_key(_Settings())
	staged = auth_circuit_store.credential_key(_Settings(pending_id="cred-new", pending_secret=NEW))
	assert staged == plain + "|cred-new"
	assert NEW not in staged and OLD not in staged


# --- the compare-and-set rule of the pending slot ------------------------------------------------

T0 = "2026-11-27T10:00:00.000001Z"
T1 = "2026-11-27T10:00:00.000002Z"


def _decide(**over):
	args = {"current_id": "cred-old", "pending_id": None, "pending_issued_at": None}
	args.update(over)
	return stage_decision(new_id="cred-2", new_issued_at=T1, **args)


def test_an_empty_slot_or_an_earlier_one_is_written():
	assert _decide() == WRITE
	assert _decide(pending_id="cred-1", pending_issued_at=T0) == WRITE


def test_an_older_write_than_the_staged_one_is_refused():
	assert _decide(pending_id="cred-3", pending_issued_at="2026-11-27T10:00:00.000003Z") == STALE


def test_an_equal_issued_at_from_another_credential_is_refused():
	assert _decide(pending_id="cred-9", pending_issued_at=T1) == STALE


def test_the_same_credential_again_is_a_no_op():
	assert _decide(pending_id="cred-2", pending_issued_at=T1) == NOOP
	assert _decide(pending_id="cred-2", pending_issued_at="2026-11-27T12:00:00+02:00") == STALE


def test_a_credential_already_promoted_is_a_no_op():
	assert _decide(current_id="cred-2") == NOOP


def test_a_credential_not_later_than_current_is_never_staged():
	# A later credential is already CURRENT (promoted, PENDING empty): a late retry of an older one is
	# refused, also when it would otherwise replace an earlier pending one.
	assert _decide(current_issued_at="2026-11-27T10:00:00.000005Z") == STALE
	assert _decide(current_issued_at=T1) == STALE  # the same instant is not later
	assert _decide(current_issued_at=T0, pending_id="cred-1", pending_issued_at=T0) == WRITE
	assert _decide(current_issued_at=T0) == WRITE
	assert _decide(current_issued_at="garbage") == WRITE  # an unreadable fence is no fence


def test_the_compare_is_made_in_utc_not_in_the_written_offset():
	# 12:00:00+02:00 is 10:00:00Z: earlier than T1 although it reads later.
	assert _decide(pending_id="cred-1", pending_issued_at="2026-11-27T12:00:00+02:00") == WRITE


def test_an_unreadable_stored_time_is_overwritten_and_an_unreadable_new_one_refused():
	assert _decide(pending_id="cred-1", pending_issued_at="garbage") == WRITE
	with pytest.raises(ValueError):
		stage_decision(
			current_id=None, pending_id=None, pending_issued_at=None, new_id="c", new_issued_at="2026-11-27"
		)


def test_the_slot_field_names_are_the_doctypes():
	import json
	from pathlib import Path

	doctype = json.loads(
		(
			Path(credential_slots.__file__).parent
			/ "doctype"
			/ "connector_settings"
			/ "connector_settings.json"
		).read_text(encoding="utf-8")
	)
	fields = {f["fieldname"]: f for f in doctype["fields"]}
	for name in credential_slots.PENDING_FIELDS:
		assert name in fields and not fields[name].get("reqd") and not fields[name].get("unique")
	assert fields[credential_slots.PENDING_TOKEN]["fieldtype"] == "Password"


# --- the frappe-backed writes, against a stand-in of the Single's rows and the password store --------


class _Db:
	"""``tabSingles`` and ``__Auth`` as the transaction sees them. ``snapshot`` stands in for what a
	plain (non-locking) read returns when this job read the rows before another job's commit; a
	locking read always returns the latest rows."""

	def __init__(self, singles):
		self.singles = dict(singles)
		self.snapshot: dict | None = None
		self.auth: dict[str, str] = {}
		self.commits = 0
		self.locked = False
		self.lock_wait: int | None = None
		self.fail_insert = False  # Frappe's set_single_value is a DELETE then an INSERT: fail between
		self.savepoints: dict[str, tuple[dict, dict]] = {}

	def sql(self, query, values=(), as_list=False):
		if query.startswith("set session innodb_lock_wait_timeout"):
			self.lock_wait = values[0]
			return ()
		assert "`tabSingles`" in query and values == ("Connector Settings",)
		if "for update" in query:
			self.locked = True
			return [[k, v] for k, v in self.singles.items()]
		return [[k, v] for k, v in (self.snapshot if self.snapshot is not None else self.singles).items()]

	def set_single_value(self, doctype, values, *args, **kwargs):
		assert doctype == "Connector Settings"
		assert kwargs.get("update_modified", True) is True  # a stale form save is then refused
		for key in values:
			self.singles.pop(key, None)  # the DELETE
		if self.fail_insert:
			raise RuntimeError("Lock wait timeout exceeded")  # before the INSERT
		self.singles.update(values)

	def savepoint(self, name):
		self.savepoints[name] = (dict(self.singles), dict(self.auth))

	def rollback(self, save_point=None):
		assert save_point in self.savepoints  # never the caller's whole transaction
		singles, auth = self.savepoints[save_point]
		self.singles, self.auth = dict(singles), dict(auth)

	def commit(self):
		self.commits += 1
		self.locked = False


@pytest.fixture
def frappe_db(monkeypatch):
	from datetime import datetime

	db = _Db({"dp2_connector_registration_id": "reg-1", "dp2_credential_id": "cred-old", "dp2_token": "*"})
	frappe = types.ModuleType("frappe")
	frappe.db = db
	frappe.flags = {}

	def get_doc(doctype):
		assert doctype == "Connector Settings"
		doc = types.SimpleNamespace(**db.singles)
		doc.get_password = lambda fieldname, raise_exception=True: db.auth.get(fieldname)
		return doc

	frappe.get_doc = get_doc
	frappe.get_meta = lambda doctype: types.SimpleNamespace(has_field=lambda name: True)
	utils = types.ModuleType("frappe.utils")
	utils.get_datetime = lambda value: value if isinstance(value, datetime) else datetime.fromisoformat(value)
	utils.convert_utc_to_system_timezone = lambda value: value  # the stand-in site runs in UTC
	db.zone = "UTC"
	utils.get_system_timezone = lambda: db.zone
	password = types.ModuleType("frappe.utils.password")
	password.set_encrypted_password = lambda dt, dn, pwd, fieldname: db.auth.__setitem__(fieldname, pwd)
	password.remove_encrypted_password = lambda dt, dn, fieldname: db.auth.pop(fieldname, None)

	def get_decrypted_password(dt, dn, fieldname, raise_exception=True):
		assert raise_exception is False
		return db.auth.get(fieldname)

	password.get_decrypted_password = get_decrypted_password
	frappe.utils = utils
	utils.password = password
	for name, module in (("frappe", frappe), ("frappe.utils", utils), ("frappe.utils.password", password)):
		monkeypatch.setitem(sys.modules, name, module)
	db.auth["dp2_token"] = OLD
	db.frappe = frappe
	return db


def _stage(credential_id="cred-new", issued_at=T1, secret=NEW, registration_id="reg-1"):
	return credential_slots.stage_pending(
		registration_id=registration_id,
		credential_id=credential_id,
		issued_at=issued_at,
		expires_at="2027-02-25T10:00:00Z",
		secret=secret,
	)


def test_staging_writes_the_pending_slot_only_under_the_lock(frappe_db):
	assert _stage() == WRITE
	assert frappe_db.locked  # the site process commits right after (site_ops)
	assert frappe_db.auth == {"dp2_token": OLD, "dp2_pending_token": NEW}
	assert frappe_db.singles["dp2_pending_credential_id"] == "cred-new"
	assert frappe_db.singles["dp2_pending_credential_issued_at"] == T1
	assert frappe_db.singles["dp2_pending_token"] == "*" * len(NEW)  # Frappe's mask, never the secret
	assert frappe_db.singles["dp2_credential_id"] == "cred-old"


def test_staging_compares_with_the_latest_rows_not_a_stale_snapshot(frappe_db):
	frappe_db.snapshot = dict(frappe_db.singles)  # read before a newer worker committed its write
	frappe_db.singles.update(
		dp2_pending_credential_id="cred-3", dp2_pending_credential_issued_at="2026-11-27T10:00:00.000003Z"
	)
	assert _stage() == STALE


def test_staging_refuses_an_older_credential_and_another_registration(frappe_db):
	_stage(credential_id="cred-3", issued_at="2026-11-27T10:00:00.000003Z", secret=NEWER)
	assert _stage() == STALE
	assert frappe_db.auth["dp2_pending_token"] == NEWER
	with pytest.raises(LookupError):
		_stage(registration_id="reg-2")


def test_promotion_moves_pending_to_current_and_empties_the_slot(frappe_db):
	_stage()
	frappe_db.commit()
	assert credential_slots.promote_pending(_pending(credential_id="cred-new")) is True
	assert frappe_db.auth == {"dp2_token": NEW}
	assert frappe_db.singles["dp2_credential_id"] == "cred-new"
	assert frappe_db.singles["dp2_pending_credential_id"] is None
	assert frappe_db.commits == 2 and not frappe_db.locked


def test_a_promotion_that_lost_the_race_takes_no_lock(frappe_db):
	_stage(credential_id="cred-newer", secret=NEWER)  # a newer rotation staged after this one was read
	frappe_db.commit()
	assert credential_slots.promote_pending(_pending(credential_id="cred-new")) is False
	assert frappe_db.auth == {"dp2_token": OLD, "dp2_pending_token": NEWER}
	assert frappe_db.commits == 1 and not frappe_db.locked


def test_a_promotion_that_loses_the_race_under_the_lock_releases_it(frappe_db):
	_stage()
	frappe_db.commit()
	frappe_db.snapshot = dict(frappe_db.singles)  # this job still sees cred-new as pending
	frappe_db.singles["dp2_pending_credential_id"] = None  # another job promoted it meanwhile
	assert credential_slots.promote_pending(_pending(credential_id="cred-new")) is False
	assert not frappe_db.locked and frappe_db.auth["dp2_token"] == OLD


def test_promotion_honours_the_no_commit_flag(frappe_db):
	_stage()
	frappe_db.commit()
	frappe_db.frappe.flags["rt_provisioner_no_commit"] = True
	assert credential_slots.promote_pending(_pending(credential_id="cred-new")) is True
	assert frappe_db.commits == 1


def test_reload_reads_the_current_secret_without_a_lock(frappe_db):
	assert credential_slots.reload_current_token() == OLD
	frappe_db.auth.pop("dp2_token")
	assert credential_slots.reload_current_token() is None
	assert not frappe_db.locked


def test_slot_ids_name_both_slots_and_never_a_secret(frappe_db):
	_stage()
	frappe_db.commit()
	assert credential_slots.slot_ids() == {
		"current_credential_id": "cred-old",
		"pending_credential_id": "cred-new",
		"pending_issued_at": T1,
	}
	assert not frappe_db.locked


def test_after_a_promotion_an_earlier_credential_is_refused_by_the_current_fence(frappe_db):
	_stage(credential_id="cred-B", issued_at=T1, secret=NEWER)
	frappe_db.commit()
	assert credential_slots.promote_pending(PendingCredential("cred-B", NEWER, T1, "2027-02-25T10:00:00Z"))
	assert _stage(credential_id="cred-A", issued_at=T0) == STALE  # the late retry of A
	assert "dp2_pending_token" not in frappe_db.auth and frappe_db.auth["dp2_token"] == NEWER
	assert _stage(credential_id="cred-C", issued_at="2026-11-27T10:00:00.000003Z") == WRITE


def test_promotion_writes_the_exact_utc_issue_time_of_current(frappe_db):
	_stage(credential_id="cred-B", issued_at=T1, secret=NEWER)
	frappe_db.commit()
	credential_slots.promote_pending(PendingCredential("cred-B", NEWER, T1, "2027-02-25T10:00:00Z"))
	assert frappe_db.singles["dp2_current_credential_issued_at_utc"] == T1


# America/New_York falls back on 2026-11-01: local 01:30 is both 05:30Z (EDT) and 06:30Z (EST).
_NY_AMBIGUOUS_LOCAL = "2026-11-01T01:30:00"


def test_in_a_fall_back_hour_the_exact_utc_fence_refuses_an_older_credential(frappe_db):
	frappe_db.zone = "America/New_York"
	frappe_db.singles.update(
		dp2_credential_issued_at=_NY_AMBIGUOUS_LOCAL,  # what the local Datetime shows for 06:30Z
		dp2_current_credential_issued_at_utc="2026-11-01T06:30:00Z",
	)
	assert _stage(credential_id="cred-older", issued_at="2026-11-01T06:15:00Z") == STALE
	assert "dp2_pending_token" not in frappe_db.auth
	assert _stage(credential_id="cred-newer", issued_at="2026-11-01T06:31:00Z") == WRITE


def test_a_legacy_ambiguous_local_time_resolves_to_the_later_instant(frappe_db):
	# Bound before the UTC field existed: only the local Datetime. The stricter reading (06:30Z, not
	# 05:30Z) keeps the older 06:15Z credential out; at worst a newer one re-pulls once.
	frappe_db.zone = "America/New_York"
	frappe_db.singles["dp2_credential_issued_at"] = _NY_AMBIGUOUS_LOCAL
	assert _stage(credential_id="cred-older", issued_at="2026-11-01T06:15:00Z") == STALE
	assert _stage(credential_id="cred-newer", issued_at="2026-11-01T06:31:00Z") == WRITE


def test_a_promotion_that_fails_part_way_leaves_both_slots_intact(frappe_db):
	_stage()
	frappe_db.commit()
	before = (dict(frappe_db.singles), dict(frappe_db.auth))
	frappe_db.fail_insert = True  # the DELETE ran, the INSERT failed
	with pytest.raises(RuntimeError):
		credential_slots.promote_pending(_pending(credential_id="cred-new"))
	assert not frappe_db.locked
	frappe_db.commit()  # the confirmation site op commits later: nothing half-written survives it
	assert (frappe_db.singles, frappe_db.auth) == before


def test_a_failed_promotion_through_the_transport_still_returns_the_answer_and_keeps_the_slots(
	frappe_db, backend
):
	_stage()
	frappe_db.commit()
	before = (dict(frappe_db.singles), dict(frappe_db.auth))
	frappe_db.fail_insert = True
	backend.accepted = {OLD, NEW}
	errors = []
	transport = RequestsTransport(
		"https://dp2.example",
		OLD,
		pending=_pending(credential_id="cred-new"),
		on_promote=credential_slots.promote_pending,
		on_slot_error=lambda what, exc: errors.append(what),
	)
	assert transport.post("/api/x", json={}, headers={}).status == 200
	assert errors == ["promote"]
	assert (frappe_db.singles, frappe_db.auth) == before  # the next accepted call promotes again


def test_a_stage_that_fails_part_way_writes_nothing(frappe_db):
	frappe_db.fail_insert = True
	with pytest.raises(RuntimeError):
		_stage()
	assert "dp2_pending_token" not in frappe_db.auth and "dp2_pending_credential_id" not in frappe_db.singles


def _clock(*times):
	from datetime import datetime

	values = iter(datetime.fromisoformat(t) for t in times)
	return lambda: next(values)


def test_a_stage_after_the_lease_end_writes_nothing_and_takes_no_lock(frappe_db):
	staged = credential_slots.stage_pending(
		registration_id="reg-1",
		credential_id="cred-new",
		issued_at=T1,
		expires_at="2027-02-25T10:00:00Z",
		secret=NEW,
		deadline="2026-11-27T10:15:00Z",
		now=_clock("2026-11-27T10:15:01+00:00"),
	)
	assert staged == credential_slots.EXPIRED
	assert not frappe_db.locked and "dp2_pending_token" not in frappe_db.auth


def test_the_lease_end_is_checked_again_under_the_lock_and_bounds_the_lock_wait(frappe_db):
	staged = credential_slots.stage_pending(
		registration_id="reg-1",
		credential_id="cred-new",
		issued_at=T1,
		expires_at="2027-02-25T10:00:00Z",
		secret=NEW,
		deadline="2026-11-27T10:15:00Z",
		# 40 s left when it starts; the lock was granted only after the lease ended
		now=_clock("2026-11-27T10:14:20+00:00", "2026-11-27T10:15:00+00:00"),
	)
	assert staged == credential_slots.EXPIRED
	assert frappe_db.lock_wait == 40
	assert "dp2_pending_token" not in frappe_db.auth


def test_a_stage_within_the_lease_writes(frappe_db):
	staged = credential_slots.stage_pending(
		registration_id="reg-1",
		credential_id="cred-new",
		issued_at=T1,
		expires_at="2027-02-25T10:00:00Z",
		secret=NEW,
		deadline="2026-11-27T10:15:00Z",
		now=_clock("2026-11-27T10:14:00+00:00", "2026-11-27T10:14:01+00:00"),
	)
	assert staged == WRITE and frappe_db.auth["dp2_pending_token"] == NEW


def test_only_an_occupant_the_write_would_replace_is_settled(frappe_db):
	assert credential_slots.occupant_to_settle("cred-new", T1) is None  # empty slot
	_stage(credential_id="cred-A", issued_at=T0, secret=NEWER)
	frappe_db.commit()
	occupant = credential_slots.occupant_to_settle("cred-new", T1)
	assert occupant.credential_id == "cred-A" and occupant.secret == NEWER
	assert credential_slots.occupant_to_settle("cred-A", T0) is None  # the same credential: a no-op
	assert credential_slots.occupant_to_settle("cred-0", "2026-11-27T09:00:00Z") is None  # older: stale
