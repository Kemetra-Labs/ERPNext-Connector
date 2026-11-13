# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1359 — the real gateway's bench invocations: argument lists only, no shell, no secret in
argv or environment, timeouts, the site marker in ``site_config.json``, and the JSON line a
site-side command prints."""

import json
import os
import sys

import pytest

from retail_tower_erpnext_connector.provisioner.bench import RunResult, SubprocessBench
from retail_tower_erpnext_connector.provisioner.config import ProvisionerConfig
from retail_tower_erpnext_connector.provisioner.gateway import BenchGateway, SiteUnreadable

from .fakes import SECRET


class RecordingRunner:
	def __init__(self, reply=None):
		self.calls = []
		self.reply = reply or (lambda argv, stdin: RunResult(0, "", ""))

	def run(self, argv, *, stdin=None, timeout, cwd):
		self.calls.append({"argv": list(argv), "stdin": stdin, "timeout": timeout, "cwd": cwd})
		return self.reply(argv, stdin)


@pytest.fixture
def setup(tmp_path):
	cfg = ProvisionerConfig(
		base_url="https://bc.example",
		bench_path=str(tmp_path),
		credential_file="/secrets/prov",
		bench_executable="/usr/local/bin/bench",
	)
	(tmp_path / "sites").mkdir()
	runner = RecordingRunner()
	return BenchGateway(cfg, runner), runner, tmp_path


def test_new_site_is_an_argument_list_with_an_explicit_db_name_and_no_root_secret(setup):
	gw, runner, root = setup
	gw.new_site("t1-pilot", "rtp_abc", "throwaway-admin-pw")
	call = runner.calls[0]
	assert call["argv"] == [
		"/usr/local/bin/bench",
		"new-site",
		"--db-name",
		"rtp_abc",
		"--admin-password",
		"throwaway-admin-pw",
		"t1-pilot",
	]
	assert call["stdin"] is None and call["cwd"] == str(root) and call["timeout"] > 0
	assert not any("root" in a for a in call["argv"])  # the root login comes from common_site_config


def test_drop_site_and_install_app_arguments(setup):
	gw, runner, _ = setup
	gw.drop_site("t1-pilot")
	gw.install_app("t1-pilot", "erpnext")
	assert runner.calls[0]["argv"][1:] == ["drop-site", "t1-pilot", "--no-backup", "--force"]
	assert runner.calls[1]["argv"][1:] == ["--site", "t1-pilot", "install-app", "erpnext"]


def test_a_failed_command_raises_with_scrubbed_output(setup):
	gw, runner, _ = setup
	runner.reply = lambda a, s: RunResult(1, "", "Error: Authorization: Bearer abc.def.ghi password=hunter2")
	with pytest.raises(RuntimeError) as err:
		gw.install_app("s", "erpnext")
	assert "hunter2" not in str(err.value) and "abc.def.ghi" not in str(err.value)


def test_site_op_puts_the_spec_and_the_secret_on_stdin_only(setup):
	gw, runner, _ = setup
	runner.reply = lambda a, s: RunResult(0, 'noise\nRTPROV:{"ok": true}\n', "")
	out = gw.site_op("t1-pilot", "bind_credential", {"credential_id": "c1"}, secret=SECRET)
	call = runner.calls[0]
	assert out == {"ok": True}
	assert call["argv"] == ["/usr/local/bin/bench", "--site", "t1-pilot", "rt-provision-site-step"]
	assert json.loads(call["stdin"]) == {
		"op": "bind_credential",
		"args": {"credential_id": "c1"},
		"secret": SECRET,
	}
	assert SECRET not in repr(call["argv"])


def test_site_op_without_a_secret_sends_none(setup):
	gw, runner, _ = setup
	runner.reply = lambda a, s: RunResult(0, 'RTPROV:{"x": 1}', "")
	gw.site_op("s", "inspect")
	assert "secret" not in json.loads(runner.calls[0]["stdin"])


def test_a_site_op_without_a_result_line_is_a_failure(setup):
	gw, runner, _ = setup
	runner.reply = lambda a, s: RunResult(0, "nothing useful", "")
	with pytest.raises(RuntimeError):
		gw.site_op("s", "inspect")


def test_an_unreadable_site_is_reported_for_inspect(setup):
	gw, runner, _ = setup
	runner.reply = lambda a, s: RunResult(1, "", "OperationalError: Unknown database")
	with pytest.raises(SiteUnreadable):
		gw.installed_apps("s")


def test_the_marker_lives_in_site_config_json(setup):
	gw, _, root = setup
	(root / "sites" / "s1").mkdir()
	(root / "sites" / "s1" / "site_config.json").write_text(json.dumps({"db_name": "x"}), encoding="utf-8")
	assert gw.read_marker("s1") is None
	gw.write_marker("s1", {"rt_tenant_id": "T", "rt_step_in_progress": "install_erpnext"})
	assert gw.read_marker("s1") == {"rt_tenant_id": "T", "rt_step_in_progress": "install_erpnext"}
	gw.write_marker("s1", {"rt_step_in_progress": None})
	stored = json.loads((root / "sites" / "s1" / "site_config.json").read_text(encoding="utf-8"))
	assert stored == {"db_name": "x", "rt_tenant_id": "T"}  # the Frappe keys are kept


def test_the_ceiling_counts_only_sites_that_carry_a_marker(setup):
	gw, _, root = setup
	for name, cfg in {
		"a": {"rt_tenant_id": "T"},
		"b": {"rt_tenant_id": "U"},
		"c": {},
		"assets": None,
	}.items():
		(root / "sites" / name).mkdir()
		if cfg is not None:
			(root / "sites" / name / "site_config.json").write_text(json.dumps(cfg), encoding="utf-8")
	assert gw.count_sites() == 2
	assert gw.site_exists("a") and not gw.site_exists("zzz")


def test_queue_depth_comes_from_the_bench_command(setup):
	gw, runner, _ = setup
	runner.reply = lambda a, s: RunResult(0, 'RTPROV:{"depth": 41}', "")
	assert gw.queue_depth() == 41
	assert runner.calls[0]["argv"][1:] == ["rt-provision-queue-depth"]


# --- the real subprocess runner ------------------------------------------------------------------


def test_the_subprocess_runner_uses_no_shell_closes_stdin_and_enforces_the_timeout(tmp_path):
	runner = SubprocessBench()
	ok = runner.run(
		[sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"], timeout=20, cwd=str(tmp_path)
	)
	assert ok.returncode == 0 and ok.stdout.strip() == "''"  # no terminal to prompt on
	piped = runner.run(
		[sys.executable, "-c", "import sys; print(sys.stdin.read())"],
		stdin=b"hello",
		timeout=20,
		cwd=str(tmp_path),
	)
	assert piped.stdout.strip() == "hello"
	with pytest.raises(TimeoutError):
		runner.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1, cwd=str(tmp_path))
	# a shell metacharacter is data, not syntax
	meta = runner.run(
		[sys.executable, "-c", "import sys; print(sys.argv[1])", "a; echo pwned"],
		timeout=20,
		cwd=str(tmp_path),
	)
	assert meta.stdout.strip() == "a; echo pwned"


def test_the_intent_journal_lives_on_the_bench_volume_and_round_trips(setup):
	gw, _, root = setup
	assert gw.read_intent("s1") is None
	gw.write_intent("s1", "T", "R")
	assert gw.read_intent("s1") == {"tenant_id": "T", "request_id": "R", "site": "s1"}
	assert (root / ".rt-provisioner" / "s1.json").is_file()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_rewriting_site_config_keeps_its_mode(setup):
	gw, _, root = setup
	(root / "sites" / "s1").mkdir()
	path = root / "sites" / "s1" / "site_config.json"
	path.write_text("{}", encoding="utf-8")
	path.chmod(0o664)
	gw.write_marker("s1", {"rt_tenant_id": "T"})
	assert path.stat().st_mode & 0o777 == 0o664


def test_a_corrupt_site_config_does_not_stop_the_site_count(setup):
	gw, _, root = setup
	for name, text in {"a": '{"rt_tenant_id": "T"}', "broken": "{not json"}.items():
		(root / "sites" / name).mkdir()
		(root / "sites" / name / "site_config.json").write_text(text, encoding="utf-8")
	assert gw.count_sites() == 1


@pytest.mark.skipif(os.name != "posix" or not hasattr(os, "geteuid"), reason="POSIX ownership")
def test_rewriting_site_config_keeps_the_owner_when_it_can(setup):
	gw, _, root = setup
	(root / "sites" / "s1").mkdir()
	path = root / "sites" / "s1" / "site_config.json"
	path.write_text("{}", encoding="utf-8")
	before = path.stat()
	gw.write_marker("s1", {"rt_tenant_id": "T"})
	after = path.stat()
	assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)


def test_the_exception_text_never_carries_the_bench_output(setup):
	gw, runner, _ = setup
	runner.reply = lambda a, s: RunResult(
		1, "", "DuplicateEntryError: Warehouse Acme Trading - RT already exists"
	)
	with pytest.raises(RuntimeError) as err:
		gw.install_app("s", "erpnext")
	assert "Acme" not in str(err.value) and "exit 1" in str(err.value)


def test_write_marker_keeps_the_mode_of_site_config_on_the_gateway_path(setup):
	gw, _, root = setup
	(root / "sites" / "s1").mkdir()
	path = root / "sites" / "s1" / "site_config.json"
	path.write_text("{}", encoding="utf-8")
	if os.name == "posix":
		path.chmod(0o664)
	gw.write_marker("s1", {"rt_tenant_id": "T"})
	if os.name == "posix":
		assert path.stat().st_mode & 0o777 == 0o664
	assert json.loads(path.read_text(encoding="utf-8")) == {"rt_tenant_id": "T"}


def test_a_failed_new_site_logs_no_command_output_even_when_it_echoes_the_password(setup, caplog):
	import logging

	gw, runner, _ = setup
	pw = "Zk3x" + "yz_abcDEF"
	runner.reply = lambda a, s: RunResult(
		2, f"Usage: bench new-site [OPTIONS] SITE (--admin-password {pw})", f"Error: bad option {pw}"
	)
	caplog.set_level(logging.DEBUG)
	with pytest.raises(RuntimeError) as err:
		gw.new_site("t1-pilot", "rtp_abc", pw)
	assert pw not in caplog.text and pw not in str(err.value)
	assert "bench new-site failed" in caplog.text  # only the fixed command name is logged
	assert "exit 2" in str(err.value)
