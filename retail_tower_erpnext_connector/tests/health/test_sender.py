# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-564 — the heartbeat sender tick, against a frappe stand-in (the RT-39 / RT-379 pattern).

Covered here: a beat sends only the contract's self-reported fields over the pollers' auth-backed
transport (Bearer from ``get_password``, never logged); an unconfigured connector skips; every
failure (transport, 401, 400, 5xx, a broken report, a failing DB probe) ends in one structured log
line and never raises; and posting is unaffected — the heartbeat is its own scheduled job, the
posting tick in the same minute still pulls when the heartbeat fails, and neither poller imports
the heartbeat. The real bench run stays a deferred validation step (standing-rules §6).
"""

import ast
import importlib
import json
import re
import sys
import types
from pathlib import Path

import pytest

import retail_tower_erpnext_connector
from retail_tower_erpnext_connector import hooks

_ROOT = "retail_tower_erpnext_connector"
_SENDER = f"{_ROOT}.connector.health.sender"
_POSTING = f"{_ROOT}.connector.posting"
_POLLER = f"{_POSTING}.poller"
_MISSING = object()
_TOKEN = "rt-connector-secret-token-value"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_Z_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_CONTRACT_KEYS = {"connectorVersion", "backlogIndicator", "erpnextReachable", "sourceClockAt"}


class _Logger:
	def __init__(self):
		self.records = []

	def __getattr__(self, level):
		return lambda record: self.records.append((level, record))


class _Settings:
	def __init__(self, base_url="https://dp2.example", token=_TOKEN):
		self.dp2_base_url = base_url
		self._token = token

	def get_password(self, field):
		assert field == "dp2_token"
		return self._token


class _Frappe(types.ModuleType):
	def __init__(self, settings=None, *, db_ok=True):
		super().__init__("frappe")
		self._logger = _Logger()
		self.logger = lambda name=None: self._logger
		self._settings = settings if settings is not None else _Settings()
		self.get_doc = lambda doctype: self._settings
		self.sql_calls = []

		def _sql(query, *args, **kwargs):
			self.sql_calls.append(query)
			if not db_ok:
				raise RuntimeError("database unavailable")
			return ((1,),)

		self._cache = {}
		self.cache = lambda: types.SimpleNamespace(
			get_value=self._cache.get, set_value=lambda k, v: self._cache.__setitem__(k, v)
		)
		self.db = types.SimpleNamespace(
			sql=_sql,
			get_single_value=lambda doctype, field: None,
			set_single_value=lambda *a, **k: None,
			commit=lambda: None,
		)
		self.exceptions = types.SimpleNamespace()

	@property
	def records(self):
		return self._logger.records


class _Requests(types.ModuleType):
	"""A ``requests`` stand-in for the pollers' ``_RequestsTransport`` (not installed in CI)."""

	def __init__(self, *, status=200, body=None, error=None):
		super().__init__("requests")
		self.calls = []
		self._status, self._body, self._error = status, body, error

	def post(self, url, *, json, headers, timeout):
		self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
		if self._error is not None:
			raise self._error
		body = self._body if self._body is not None else {"acknowledgedAt": "2026-10-26T09:00:00.000Z"}
		return types.SimpleNamespace(status_code=self._status, headers={}, json=lambda: body)


@pytest.fixture
def load(monkeypatch):
	"""Load the sender (and the posting poller it borrows the transport from) against a stand-in."""
	posting_pkg = importlib.import_module(_POSTING)
	health_pkg = importlib.import_module(f"{_ROOT}.connector.health")
	names = ("frappe", "requests", _SENDER, _POLLER, f"{_POSTING}.frappe_store", f"{_POSTING}.frappe_glue")
	saved = [(sys.modules, n, sys.modules.get(n, _MISSING)) for n in names]
	saved += [
		(vars(posting_pkg), n, vars(posting_pkg).get(n, _MISSING))
		for n in ("poller", "frappe_store", "frappe_glue")
	]
	saved += [(vars(health_pkg), "sender", vars(health_pkg).get("sender", _MISSING))]

	def _load(fake, requests=None):
		sys.modules["frappe"] = fake
		sys.modules["requests"] = requests if requests is not None else _Requests()
		for name in names[2:]:
			sys.modules.pop(name, None)
		return importlib.import_module(_SENDER)

	yield _load
	for namespace, name, value in saved:
		namespace.pop(name, None)
		if value is not _MISSING:
			namespace[name] = value


def _logged_text(fake):
	return json.dumps([record for _level, record in fake.records], default=str)


def _levels(fake):
	return [(level, record["event"]) for level, record in fake.records if level != "debug"]


class TestBeat:
	def test_sends_the_self_reported_fields_over_the_bearer_transport(self, load):
		fake, requests = _Frappe(), _Requests()
		sender = load(fake, requests)
		sender.run_heartbeat()

		assert len(requests.calls) == 1
		call = requests.calls[0]
		assert call["url"] == "https://dp2.example/api/connector/v1/erpnext/health/heartbeat"
		assert call["headers"]["Authorization"] == "Bearer " + _TOKEN
		assert _UUID.match(call["headers"]["X-Request-Id"])
		assert call["timeout"] == 30
		body = call["json"]
		assert set(body) <= _CONTRACT_KEYS
		assert body["connectorVersion"] == retail_tower_erpnext_connector.__version__
		assert body["erpnextReachable"] is True
		assert _Z_TIME.match(body["sourceClockAt"])
		assert "backlogIndicator" not in body  # no truthful count on the connector side
		assert _levels(fake) == []
		assert fake.sql_calls == ["SELECT 1"]

	def test_a_failing_db_probe_reports_unreachable_and_still_beats(self, load):
		fake, requests = _Frappe(db_ok=False), _Requests()
		load(fake, requests).run_heartbeat()
		assert requests.calls[0]["json"]["erpnextReachable"] is False
		assert _levels(fake) == []

	def test_the_token_is_never_logged(self, load):
		fake = _Frappe()
		load(fake, _Requests()).run_heartbeat()
		assert _TOKEN not in _logged_text(fake)

	def test_one_request_id_per_tick(self, load):
		requests = _Requests()
		sender = load(_Frappe(), requests)
		sender.run_heartbeat()
		sender.run_heartbeat()
		ids = [call["headers"]["X-Request-Id"] for call in requests.calls]
		assert len(set(ids)) == 2


class TestFailsQuietly:
	@pytest.mark.parametrize(
		"settings",
		[_Settings(base_url=""), _Settings(token=None), _Settings(token="")],
		ids=["no-base-url", "no-token", "empty-token"],
	)
	def test_an_unconfigured_connector_skips_without_calling(self, load, settings):
		fake, requests = _Frappe(settings), _Requests()
		load(fake, requests).run_heartbeat()
		assert requests.calls == []
		assert _levels(fake) == [("warning", "connector.heartbeat.skipped")]

	def test_a_transport_error_is_logged_scrubbed_and_not_raised(self, load):
		error = ConnectionError("connect failed; Authorization: Bearer " + _TOKEN)
		fake = _Frappe()
		load(fake, _Requests(error=error)).run_heartbeat()
		assert _levels(fake) == [("warning", "connector.heartbeat.failed")]
		record = fake.records[0][1]
		assert record["error_class"] == "ConnectionError"
		assert _UUID.match(record["request_id"])
		assert _TOKEN not in _logged_text(fake)

	def test_401_is_an_operator_error_and_not_raised(self, load):
		fake = _Frappe()
		body = {"error": {"code": "unauthorized", "message": "Unauthorized"}}
		load(fake, _Requests(status=401, body=body)).run_heartbeat()
		assert _levels(fake) == [("error", "connector.heartbeat.unauthorized")]
		assert fake.records[0][1]["status"] == 401
		assert fake.records[0][1]["code"] == "unauthorized"

	def test_400_is_a_contract_error_and_not_raised(self, load):
		fake = _Frappe()
		body = {"error": {"code": "validation_error", "message": "bad"}}
		load(fake, _Requests(status=400, body=body)).run_heartbeat()
		assert _levels(fake) == [("error", "connector.heartbeat.rejected")]

	@pytest.mark.parametrize("status", [404, 429, 500, 503])
	def test_another_status_is_a_warning_and_not_raised(self, load, status):
		fake = _Frappe()
		load(fake, _Requests(status=status, body={"error": {"code": "system_failure"}})).run_heartbeat()
		assert _levels(fake) == [("warning", "connector.heartbeat.failed")]
		assert fake.records[0][1]["status"] == status

	def test_a_broken_report_is_logged_and_not_raised(self, load, monkeypatch):
		fake, requests = _Frappe(), _Requests()
		sender = load(fake, requests)
		monkeypatch.setattr(sender, "_connector_version", lambda: "")
		sender.run_heartbeat()
		assert requests.calls == []
		assert _levels(fake) == [("warning", "connector.heartbeat.failed")]
		assert fake.records[0][1]["error_class"] == "ValueError"

	def test_a_failing_settings_read_skips(self, load):
		fake = _Frappe()
		sender = load(fake, _Requests())

		def _boom(doctype):
			raise RuntimeError("DocType Connector Settings not found")

		fake.get_doc = _boom
		sender.run_heartbeat()
		assert _levels(fake) == [("warning", "connector.heartbeat.skipped")]


class TestPostingUnaffected:
	def test_the_heartbeat_is_a_separate_job_from_posting(self):
		minute = hooks.scheduler_events["cron"]["* * * * *"]
		assert f"{_SENDER}.run_heartbeat" in minute
		assert f"{_POLLER}.run_posting_poll" in minute
		assert len(minute) == len(set(minute))

	def test_a_failing_heartbeat_does_not_stop_the_posting_tick(self, load, monkeypatch):
		"""Run every job of the per-minute cron in order, as the scheduler would, with DP2's
		heartbeat endpoint down: the heartbeat logs and returns, and the posting tick still pulls."""
		fake = _Frappe()
		sender = load(fake, _Requests(error=ConnectionError("connection refused")))
		poller = importlib.import_module(_POLLER)
		index_guard = importlib.import_module(f"{_POSTING}.index_guard")
		monkeypatch.setattr(
			index_guard, "evaluate", lambda probe: index_guard.GuardResult(ok=True, missing=())
		)
		pages = []
		monkeypatch.setattr(
			poller, "_build_posting_path", lambda _rid: (object(), lambda item: None, lambda: True)
		)
		monkeypatch.setattr(
			poller.worker,
			"process_page",
			lambda *a, **k: (
				pages.append(1)
				or types.SimpleNamespace(
					degraded=False, degraded_message=None, cursor="c1", next_page_token=None
				)
			),
		)
		jobs = {
			f"{_SENDER}.run_heartbeat": sender.run_heartbeat,
			f"{_POLLER}.run_posting_poll": poller.run_posting_poll,
		}
		for path in reversed(hooks.scheduler_events["cron"]["* * * * *"]):  # heartbeat first
			jobs[path]()

		assert pages == [1]
		assert ("warning", "connector.heartbeat.failed") in _levels(fake)

	@pytest.mark.parametrize("module", ["posting/poller.py", "posting/worker.py", "bin_view/poller.py"])
	def test_no_poller_imports_the_heartbeat(self, module):
		source = (Path(retail_tower_erpnext_connector.__file__).parent / "connector" / module).read_text()
		for node in ast.walk(ast.parse(source)):
			if isinstance(node, ast.ImportFrom):
				assert "health" not in (node.module or "")
			elif isinstance(node, ast.Import):
				assert all("health" not in alias.name for alias in node.names)
