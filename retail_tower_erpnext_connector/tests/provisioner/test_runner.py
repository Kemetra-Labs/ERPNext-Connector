# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1359 — the pull loop (one request at a time under the bench lock), the credential-file
transport, and the entrypoint's one-argument rule."""

import json
import logging
import os
import sys

import pytest

from retail_tower_erpnext_connector.provisioner import __main__ as entry
from retail_tower_erpnext_connector.provisioner.errors import AuthRejected, CredentialFileError
from retail_tower_erpnext_connector.provisioner.lock import FileLock
from retail_tower_erpnext_connector.provisioner.runner import ProvisionerRunner
from retail_tower_erpnext_connector.provisioner.transport import ProvisionerTransport

from .fakes import SECRET, STORE_A, TENANT
from .test_engine import Env


def _runner(env: Env, tmp_path) -> ProvisionerRunner:
	return ProvisionerRunner(env.engine(), env.feed_client(), FileLock(tmp_path / "p.lock"), clock=env.clock)


def test_one_pass_provisions_what_it_pulls(tmp_path):
	env = Env()
	env.feed.add()
	summary = _runner(env, tmp_path).run_once()
	assert summary == {"lock": "acquired", "results": {env.state.wire["provisioningRequestId"]: "succeeded"}}


def test_a_held_lock_means_nothing_is_pulled(tmp_path):
	env = Env()
	env.feed.add()
	other = FileLock(tmp_path / "p.lock")
	other.acquire()
	try:
		summary = _runner(env, tmp_path).run_once()
	finally:
		other.release()
	assert summary == {"lock": "held", "results": {}}
	assert env.feed.calls == [] and env.state.terminal is None


def test_an_invalid_provisioner_credential_stops_the_run(tmp_path):
	env = Env()
	env.feed.add()
	env.feed.bearer_in_use = "revoked"
	with pytest.raises(AuthRejected):
		_runner(env, tmp_path).run_once()


def test_a_request_under_backoff_is_not_worked(tmp_path):
	env = Env()
	state = env.feed.add()
	state.wire["notBefore"] = "2999-01-01T00:00:00Z"
	assert _runner(env, tmp_path).run_once()["results"] == {}
	assert env.gw.calls == []


def test_the_lock_is_released_after_a_run_even_when_it_fails(tmp_path):
	env = Env()
	env.feed.add()
	env.feed.bearer_in_use = "revoked"
	with pytest.raises(AuthRejected):
		_runner(env, tmp_path).run_once()
	second = FileLock(tmp_path / "p.lock")
	second.acquire()
	second.release()


# --- the transport reads the credential file per request and never keeps it -------------------


class _Recorder:
	"""Stands in for the shared RequestsTransport: records the token each built transport got."""

	def __init__(self):
		self.tokens = []

	def __call__(self, base, token):
		self.tokens.append((base, token))
		return self

	def get(self, path, *, params, headers):
		return {"items": [], "cursor": "x"}

	def post(self, path, *, json, headers):
		return {"posted": path}


def test_the_credential_is_read_from_the_file_at_each_request(tmp_path):
	cred = tmp_path / "prov"
	cred.write_text(SECRET + "\n", encoding="utf-8")
	if os.name == "posix":
		cred.chmod(0o600)
	rec = _Recorder()
	t = ProvisionerTransport("https://bc.example", str(cred), factory=rec)
	assert t.get("/p", params={"limit": 1}, headers={}) == {"items": [], "cursor": "x"}
	cred.write_text("rotated-secret\n", encoding="utf-8")
	t.post("/p", json=None, headers={})
	assert rec.tokens == [("https://bc.example", SECRET), ("https://bc.example", "rotated-secret")]
	assert SECRET not in repr(t) and SECRET not in repr(t.__dict__)
	assert "rotated-secret" not in repr(t.__dict__)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_a_credential_file_readable_by_others_is_refused(tmp_path):
	cred = tmp_path / "prov"
	cred.write_text(SECRET, encoding="utf-8")
	cred.chmod(0o644)
	with pytest.raises(CredentialFileError):
		ProvisionerTransport("https://bc.example", str(cred), factory=_Recorder()).get(
			"/p", params={}, headers={}
		)


def test_an_empty_credential_file_is_refused(tmp_path):
	cred = tmp_path / "prov"
	cred.write_text("  \n", encoding="utf-8")
	if os.name == "posix":
		cred.chmod(0o600)
	with pytest.raises(CredentialFileError):
		ProvisionerTransport("https://bc.example", str(cred), factory=_Recorder()).get(
			"/p", params={}, headers={}
		)


# --- the entrypoint ------------------------------------------------------------------------------


def test_the_entrypoint_takes_a_config_path_and_nothing_secret():
	args = entry.parse_args(["--config", "/etc/rt/provisioner.json"])
	assert args.config == "/etc/rt/provisioner.json" and args.once is False
	with pytest.raises(SystemExit):
		entry.parse_args(["--config", "x", "--token", "abc"])
	with pytest.raises(SystemExit):
		entry.parse_args([])


def test_log_lines_are_scrubbed_by_the_logging_filter(caplog):
	entry.install_log_scrubbing()
	logger = logging.getLogger("rt.provisioner")
	caplog.set_level(logging.INFO)
	logger.info("failed: Authorization: Bearer abc.def.ghi password=hunter2")
	assert "hunter2" not in caplog.text and "abc.def.ghi" not in caplog.text


def test_main_runs_once_and_returns_zero(tmp_path, monkeypatch):
	cfg = tmp_path / "c.json"
	cfg.write_text(
		json.dumps({"base_url": "https://bc.example", "bench_path": str(tmp_path), "credential_file": "/x"}),
		encoding="utf-8",
	)
	called = {}
	monkeypatch.setattr(entry, "build_runner", lambda config: _FakeRunner(called))
	assert entry.main(["--config", str(cfg), "--once"]) == 0
	assert called == {"once": True}
	_ = (sys, TENANT, STORE_A)


class _FakeRunner:
	def __init__(self, called):
		self.called = called

	def run_once(self):
		self.called["once"] = True
		return {"lock": "acquired", "results": {}}


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks and modes")
def test_a_symlinked_credential_file_is_refused(tmp_path):
	real = tmp_path / "real"
	real.write_text(SECRET, encoding="utf-8")
	real.chmod(0o600)
	link = tmp_path / "prov"
	link.symlink_to(real)
	with pytest.raises(CredentialFileError):
		ProvisionerTransport("https://bc.example", str(link), factory=_Recorder()).get(
			"/p", params={}, headers={}
		)


def test_a_missing_credential_file_is_a_fatal_local_error_not_an_outage(tmp_path):
	from retail_tower_erpnext_connector.provisioner.feed_client import FeedClient

	client = FeedClient(
		ProvisionerTransport("https://bc.example", str(tmp_path / "none"), factory=_Recorder())
	)
	with pytest.raises(CredentialFileError):
		client.pull()


# --- one bad request or a failed pull does not take the process down -----------------------------


def test_a_failed_pull_ends_the_pass_without_raising(tmp_path):
	env = Env()
	env.feed.add()
	env.feed.fail_next = [503]
	summary = _runner(env, tmp_path).run_once()
	assert summary["results"] == {} and summary["error"] == "FeedUnavailable"


def test_a_request_the_feed_answers_unexpectedly_is_skipped_and_the_next_one_still_runs(tmp_path):
	from retail_tower_erpnext_connector.provisioner.feed_client import Response

	env = Env()
	env.feed.add(provisioningRequestId="aaaaaaaa-1111-4222-8333-444444444444")
	env.feed.add(provisioningRequestId="bbbbbbbb-1111-4222-8333-444444444444")
	real_post = env.feed.post

	def flaky(path, *, json, headers):
		if "aaaaaaaa" in path:
			return Response(400, {}, {"error": {"code": "validation_error", "message": "x"}})
		return real_post(path, json=json, headers=headers)

	env.feed.post = flaky
	results = _runner(env, tmp_path).run_once()["results"]
	assert results["aaaaaaaa-1111-4222-8333-444444444444"] == "error"
	assert results["bbbbbbbb-1111-4222-8333-444444444444"] == "succeeded"


def test_main_reports_an_unexpected_failure_as_one_line_and_a_nonzero_exit(tmp_path, monkeypatch, caplog):
	cfg = tmp_path / "c.json"
	cfg.write_text(
		json.dumps({"base_url": "https://bc.example", "bench_path": str(tmp_path), "credential_file": "/x"}),
		encoding="utf-8",
	)

	class Boom:
		def run_once(self):
			raise RuntimeError("--admin-password hunter2")

	monkeypatch.setattr(entry, "build_runner", lambda config: Boom())
	caplog.set_level(logging.ERROR)
	assert entry.main(["--config", str(cfg), "--once"]) == 4
	assert "hunter2" not in caplog.text


def test_tenant_requests_behind_many_skipped_kinds_are_still_reached(tmp_path):
	env = Env()
	for n in range(60):  # more than one page of requests this provisioner leaves alone
		env.feed.add(provisioningRequestId=f"00000000-0000-4000-8000-{n:012d}", kind="store.provision")
	env.feed.add()
	results = _runner(env, tmp_path).run_once()["results"]
	assert results[env.state.wire["provisioningRequestId"]] == "succeeded"


def test_a_page_with_items_but_no_cursor_ends_the_pull_and_no_request_is_worked_twice(tmp_path):
	from retail_tower_erpnext_connector.provisioner.feed_client import FeedClient, Page

	env = Env()
	env.feed.add()
	request = env.pull()[0]
	calls = []

	class OnePageNoCursor(FeedClient):
		def pull(self, cursor=None, limit=20):
			calls.append(cursor)
			return Page([request], "")

	runner = ProvisionerRunner(
		env.engine(), OnePageNoCursor(env.feed), FileLock(tmp_path / "p.lock"), clock=env.clock
	)
	assert runner.run_once()["results"] == {request.id: "succeeded"}
	assert calls == [None]


def test_a_repeated_cursor_ends_the_pull_and_dedupes(tmp_path):
	from retail_tower_erpnext_connector.provisioner.feed_client import FeedClient, Page

	env = Env()
	env.feed.add()
	request = env.pull()[0]
	calls = []

	class StuckCursor(FeedClient):
		def pull(self, cursor=None, limit=20):
			calls.append(cursor)
			return Page([request], "same")

	runner = ProvisionerRunner(
		env.engine(), StuckCursor(env.feed), FileLock(tmp_path / "p.lock"), clock=env.clock
	)
	assert runner.run_once()["results"] == {request.id: "succeeded"}
	assert calls == [None, "same"]
