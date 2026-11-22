# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1431 — the auth circuit as an operator sees it: state, last status, opened at, next probe (no bench).

The view is a pure function of the RT-1429 cache record. The tests pin what it shows, that reading it never
writes the cache, and that no credential id, token or fingerprint reaches it. ``frappe`` is a stand-in.
"""

import json
import sys
import types
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from retail_tower_erpnext_connector.connector import auth_circuit_store
from retail_tower_erpnext_connector.connector.auth_circuit import AuthCircuit, CircuitState
from retail_tower_erpnext_connector.connector.auth_circuit_status import (
	NOTE_OTHER_CREDENTIAL,
	NOTE_PROBE_DUE,
	STATE_CLOSED,
	STATE_OPEN,
	build_status,
)

CONNECTOR = Path(auth_circuit_store.__file__).parent
REPORT_DIR = CONNECTOR / "report" / "connector_auth_circuit"
POSTING_LOG_JSON = CONNECTOR / "doctype" / "posting_log" / "posting_log.json"
_MISSING = object()
# Built at run time and low-entropy: a secret-shaped literal in any commit keeps gitleaks red.
TOKEN = "fake-" + "x" * 24
OPENED = 1_800_000_000.0
KEY = "cred-1:abcdef012345"
SITE_TZ = timezone(timedelta(hours=2))


def _state(key=KEY, failures=3, status=401, opened=OPENED, probe=OPENED + 240):
	return CircuitState(key=key, failures=failures, status=status, opened_at=opened, next_probe_at=probe)


def _at(epoch):
	return datetime.fromtimestamp(epoch, UTC)


# --- the pure view ---------------------------------------------------------------------------------


def test_nothing_recorded_reads_as_closed_with_nothing_to_show():
	status = build_status(None, current_key=KEY, now=OPENED)
	assert status.state == STATE_CLOSED
	assert (status.last_status, status.failures, status.opened_at, status.next_probe_at) == (
		None,
		None,
		None,
		None,
	)
	assert status.note == ""


def test_an_open_circuit_shows_state_last_status_last_change_and_next_probe():
	status = build_status(_state(), current_key=KEY, now=OPENED + 100)
	assert status.state == STATE_OPEN
	assert status.last_status == 401
	assert status.failures == 3
	assert status.opened_at == _at(OPENED)
	assert status.next_probe_at == _at(OPENED + 240)
	assert status.note == ""


def test_a_403_is_shown_as_403():
	assert build_status(_state(status=403), current_key=KEY, now=OPENED).last_status == 403


def test_a_due_probe_is_said_so():
	status = build_status(_state(), current_key=KEY, now=OPENED + 240)
	assert status.state == STATE_OPEN
	assert status.note == NOTE_PROBE_DUE


def test_a_record_for_a_previous_credential_reads_as_closed_with_a_note():
	status = build_status(_state(key="cred-1:other0000000"), current_key=KEY, now=OPENED)
	assert status.state == STATE_CLOSED
	assert status.note == NOTE_OTHER_CREDENTIAL
	assert status.opened_at is None
	assert status.next_probe_at is None


def test_a_time_that_cannot_be_a_date_shows_no_time_not_an_error():
	status = build_status(_state(opened=1e30, probe=1e30), current_key=KEY, now=OPENED)
	assert status.state == STATE_OPEN
	assert status.opened_at is None
	assert status.next_probe_at is None


def test_the_scripted_form_is_json_safe_and_carries_no_key():
	as_dict = build_status(_state(), current_key=KEY, now=OPENED).to_dict()
	assert as_dict == {
		"state": "open",
		"last_status": 401,
		"failures": 3,
		"opened_at": _at(OPENED).isoformat(),
		"next_probe_at": _at(OPENED + 240).isoformat(),
		"note": "",
	}
	text = json.dumps(as_dict)
	assert "cred-1" not in text
	assert "abcdef012345" not in text


# --- the live read: a read of the shared cache, never a write -------------------------------------------


class _Cache:
	"""A dict-backed cache that records every call: the read path must make only reads."""

	def __init__(self):
		self.values = {}
		self.calls = []

	def get_value(self, key):
		self.calls.append(("get_value", key))
		return self.values.get(key)

	def set_value(self, key, value):
		self.calls.append(("set_value", key))
		self.values[key] = value

	def delete_value(self, key):
		self.calls.append(("delete_value", key))
		self.values.pop(key, None)


class _Settings:
	def __init__(self, credential_id="cred-1", token=TOKEN):
		self.dp2_credential_id = credential_id
		self._token = token

	def get_password(self, fieldname):
		return self._token


@pytest.fixture
def frappe_stub():
	saved = sys.modules.get("frappe", _MISSING)
	fake = types.ModuleType("frappe")
	cache = _Cache()
	fake.cache = lambda: cache
	fake.get_doc = lambda doctype, *args: _Settings()
	fake.the_cache = cache
	sys.modules["frappe"] = fake
	yield fake
	sys.modules.pop("frappe", None)
	if saved is not _MISSING:
		sys.modules["frappe"] = saved


def _open_the_circuit(settings, status=401):
	AuthCircuit(
		auth_circuit_store.FrappeCircuitStore(), credential_key=auth_circuit_store.credential_key(settings)
	).record_refusal(status)


def test_the_live_read_shows_a_circuit_another_job_opened(frappe_stub):
	_open_the_circuit(_Settings(), status=403)
	status = auth_circuit_store.read_auth_circuit()
	assert (status.state, status.last_status, status.failures) == (STATE_OPEN, 403, 1)
	assert status.opened_at is not None
	assert status.next_probe_at is not None


def test_the_live_read_of_a_closed_circuit(frappe_stub):
	assert auth_circuit_store.read_auth_circuit().state == STATE_CLOSED


def test_reading_never_writes_the_cache_even_for_another_credentials_record(frappe_stub):
	# AuthCircuit would clear this record (it belongs to a previous token); a view must leave it alone.
	_open_the_circuit(_Settings(token="other-" + "y" * 24))
	before = dict(frappe_stub.the_cache.values)
	frappe_stub.the_cache.calls.clear()
	status = auth_circuit_store.read_auth_circuit()
	assert status.state == STATE_CLOSED
	assert status.note == NOTE_OTHER_CREDENTIAL
	assert [name for name, _key in frappe_stub.the_cache.calls] == ["get_value"]
	assert frappe_stub.the_cache.values == before


def test_the_scripted_read_shows_neither_the_token_nor_the_key(frappe_stub):
	_open_the_circuit(_Settings())
	text = json.dumps(auth_circuit_store.read_auth_circuit_status())
	key = auth_circuit_store.credential_key(_Settings())
	assert TOKEN not in text
	assert key not in text
	assert key.split(":", 1)[1] not in text  # the fingerprint is token-derived too


# --- the report -------------------------------------------------------------------------------------


@pytest.fixture
def report(frappe_stub):
	saved_utils = sys.modules.get("frappe.utils", _MISSING)
	utils = types.ModuleType("frappe.utils")
	# A site at UTC+2 (fixed offset, no tzdata needed): the report must show the shifted wall-clock time.
	utils.convert_utc_to_system_timezone = lambda moment: moment.astimezone(SITE_TZ)
	frappe_stub.utils = utils
	sys.modules["frappe.utils"] = utils
	from retail_tower_erpnext_connector.connector.report.connector_auth_circuit import (
		connector_auth_circuit,
	)

	yield connector_auth_circuit
	sys.modules.pop("frappe.utils", None)
	if saved_utils is not _MISSING:
		sys.modules["frappe.utils"] = saved_utils


def test_the_report_has_one_row_with_every_column_filled_for_an_open_circuit(report):
	_open_the_circuit(_Settings())
	columns, rows = report.execute({})
	assert [c["fieldname"] for c in columns] == [
		"state",
		"last_status",
		"failures",
		"opened_at",
		"next_probe_at",
		"note",
	]
	assert len(rows) == 1
	row = rows[0]
	assert row["state"] == STATE_OPEN
	assert row["last_status"] == 401
	assert row["opened_at"].tzinfo is None
	assert row["next_probe_at"] > row["opened_at"]


def test_the_report_shows_times_in_the_site_timezone_as_naive_datetimes(report):
	row = report.to_row(build_status(_state(), current_key=KEY, now=OPENED))
	assert row["opened_at"] == _at(OPENED).astimezone(SITE_TZ).replace(tzinfo=None)
	assert row["opened_at"] == _at(OPENED).replace(tzinfo=None) + timedelta(hours=2)
	assert row["next_probe_at"] == row["opened_at"] + timedelta(seconds=240)


def test_the_report_row_of_a_closed_circuit_is_just_closed(report):
	_columns, rows = report.execute()
	assert rows == [
		{
			"state": STATE_CLOSED,
			"last_status": None,
			"failures": None,
			"opened_at": None,
			"next_probe_at": None,
			"note": "",
		}
	]


def test_the_report_rows_carry_no_token_or_key(report):
	_open_the_circuit(_Settings())
	_columns, rows = report.execute({})
	text = repr(rows)
	assert TOKEN not in text
	assert "cred-1" not in text
	key = auth_circuit_store.credential_key(_Settings())
	assert key.split(":", 1)[1] not in text  # the token-derived fingerprint


# --- the standard report definition Frappe syncs on migrate ---------------------------------------------


def _definition():
	return json.loads((REPORT_DIR / "connector_auth_circuit.json").read_text(encoding="utf-8"))


def test_the_report_definition_is_a_standard_script_report_a_system_manager_can_run():
	definition = _definition()
	assert definition["doctype"] == "Report"
	assert definition["name"] == definition["report_name"] == "Connector Auth Circuit"
	assert definition["report_type"] == "Script Report"
	assert definition["is_standard"] == "Yes"
	assert definition["module"] == "Connector"
	assert definition["modified"]  # sync skips a file without one
	assert definition["prepared_report"] == 0
	# Frappe flips prepared_report on after a run over 15 s unless this (real) field is set; a prepared run
	# would show stale live state. ("disable_prepared_report" is not a Report field and is ignored.)
	assert definition["disable_prepared_report_automation"] == 1
	assert "disable_prepared_report" not in definition
	assert definition["roles"] == [{"role": "System Manager"}]


def test_the_report_runs_on_a_doctype_that_grants_system_manager_the_report_right():
	# query_report.run() requires the "report" permission on ref_doctype; Connector Settings grants none
	# (and a permissions change there would be a restore-class release), so the report is on Posting Log.
	assert _definition()["ref_doctype"] == "Posting Log"
	grants = json.loads(POSTING_LOG_JSON.read_text(encoding="utf-8"))["permissions"]
	assert any(g["role"] == "System Manager" and g.get("report") == 1 for g in grants)


def test_the_report_folders_are_packages_frappe_can_import():
	assert (REPORT_DIR.parent / "__init__.py").is_file()
	assert (REPORT_DIR / "__init__.py").is_file()
