# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1359 — the safety invariants: the secret never reaches arguments, environment or logs; the
lock admits one run; the orchestrator never imports frappe; and no whitelisted method, document
override or hook was added."""

import ast
import json
import logging
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest

from retail_tower_erpnext_connector.provisioner.config import ProvisionerConfig, load_config
from retail_tower_erpnext_connector.provisioner.lock import FileLock, LockHeld

from .fakes import SECRET
from .test_engine import Env

APP_ROOT = Path(__file__).resolve().parents[2]
PKG = APP_ROOT / "provisioner"


# --- the secret ----------------------------------------------------------------------------------


def test_the_credential_secret_reaches_only_the_bind_operation_stdin(caplog):
	caplog.set_level(logging.DEBUG)
	e = Env()
	e.feed.add()
	assert e.run() == ["succeeded"]
	carrying = [p for p in e.gw.stdin_payloads if p["secret"]]
	assert [p["op"] for p in carrying] == ["bind_credential"]
	assert SECRET == carrying[0]["secret"]
	# never in any argument handed to the bench, in the op arguments, in what was sent to the feed,
	# in the acks, or in a log line
	assert SECRET not in repr(e.gw.calls)
	assert SECRET not in repr([p["args"] for p in e.gw.stdin_payloads])
	assert SECRET not in repr([c[2]["json"] for c in e.feed.calls])
	assert SECRET not in caplog.text
	assert all(SECRET not in str(r.args) for r in caplog.records)


def test_the_throwaway_admin_password_is_never_logged_or_sent(caplog):
	caplog.set_level(logging.DEBUG)
	e = Env()
	e.feed.add()
	e.run()
	(pw,) = e.gw.admin_passwords
	assert pw not in caplog.text and pw not in repr([c[2]["json"] for c in e.feed.calls])
	assert pw not in repr(e.gw.stdin_payloads)


def test_failure_details_sent_to_the_feed_are_scrubbed():
	e = Env()
	e.feed.add()
	e.gw.fail_op["setup_wizard"] = RuntimeError(
		"connect failed Authorization: Bearer abc.def.ghi password=hunter2 for owner@example.com"
	)
	e.run()
	ack = [c for c in e.feed.calls if c[1].endswith("/steps")][-1][2]["json"]
	text = json.dumps(ack)
	assert "hunter2" not in text and "abc.def.ghi" not in text and "owner@example.com" not in text


# --- configuration: a path to the credential, never the credential ------------------------------


def _write(path: Path, data: dict) -> Path:
	path.write_text(json.dumps(data), encoding="utf-8")
	return path


def test_the_config_is_read_from_a_file_and_holds_only_the_credential_path(tmp_path):
	cfg = load_config(
		_write(
			tmp_path / "c.json",
			{"base_url": "https://bc.example", "bench_path": "/b", "credential_file": "/secrets/prov"},
		)
	)
	assert isinstance(cfg, ProvisionerConfig)
	assert cfg.credential_file == "/secrets/prov" and cfg.max_sites == 20
	assert cfg.queue_depth_threshold == 500 and cfg.step_budget_seconds == 300


@pytest.mark.parametrize("key", ["credential", "token", "password", "db_root_password", "secret"])
def test_a_config_that_carries_a_secret_is_refused(tmp_path, key):
	data = {"base_url": "https://bc.example", "bench_path": "/b", "credential_file": "/c", key: "x"}
	with pytest.raises(ValueError, match="secret"):
		load_config(_write(tmp_path / "c.json", data))


def test_plain_http_to_backend_core_is_refused(tmp_path):
	with pytest.raises(ValueError, match="https"):
		load_config(
			_write(
				tmp_path / "c.json", {"base_url": "http://bc", "bench_path": "/b", "credential_file": "/c"}
			)
		)


# --- the lock ------------------------------------------------------------------------------------


def test_a_second_concurrent_run_is_refused(tmp_path):
	path = tmp_path / "prov.lock"
	first = FileLock(path)
	first.acquire()
	try:
		with pytest.raises(LockHeld):
			FileLock(path).acquire()
	finally:
		first.release()
	again = FileLock(path)
	again.acquire()  # released locks can be taken again
	again.release()


# --- orchestrator isolation ----------------------------------------------------------------------


def test_the_orchestrator_imports_no_frappe():
	modules = sorted(
		p.stem for p in PKG.glob("*.py") if p.stem not in {"site_ops", "template_site", "__init__"}
	)
	code = (
		"import sys, importlib\n"
		f"mods = {modules!r}\n"
		"for m in mods:\n"
		"    importlib.import_module('retail_tower_erpnext_connector.provisioner.' + m)\n"
		"bad = sorted(k for k in sys.modules if k == 'frappe' or k.startswith('frappe.'))\n"
		"print('FRAPPE:' + ','.join(bad))\n"
	)
	out = subprocess.run(
		[sys.executable, "-c", code], cwd=APP_ROOT.parent, capture_output=True, text=True, check=True
	).stdout
	assert out.strip().endswith("FRAPPE:"), out


# --- no whitelisted method, no override, no hook --------------------------------------------------

_FORBIDDEN_NAMES = {"whitelist", "allow_guest"}
_FORBIDDEN_HOOKS = {
	"override_doctype_class",
	"override_whitelisted_methods",
	"doc_events",
	"before_request",
	"after_request",
	"website_route_rules",
	"permission_query_conditions",
	"has_permission",
}


def _python_files():
	"""Every module of the app package except its tests: acceptance 6 covers the whole app."""
	for path in APP_ROOT.rglob("*.py"):
		if "tests" not in path.relative_to(APP_ROOT).parts:
			yield path


def test_no_whitelisted_method_was_added():
	# Token scan, not a parse: the app targets Python 3.14 syntax a 3.13 laptop cannot parse, and a
	# NAME token ignores comments and strings.
	for path in _python_files():
		with path.open("rb") as handle:
			names = {t.string for t in tokenize.tokenize(handle.readline) if t.type == tokenize.NAME}
		assert not (names & _FORBIDDEN_NAMES), f"{path.name}: {sorted(names & _FORBIDDEN_NAMES)}"


def test_hooks_register_no_override_and_no_provisioner_hook():
	tree = ast.parse((APP_ROOT / "hooks.py").read_text(encoding="utf-8"))
	assigned = {
		t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)
	}
	assert not (assigned & _FORBIDDEN_HOOKS)
	assert "provisioner" not in (APP_ROOT / "hooks.py").read_text(encoding="utf-8")


def test_the_bench_commands_are_registered_in_a_commands_module():
	tree = ast.parse((APP_ROOT / "commands.py").read_text(encoding="utf-8"))
	assert any(
		isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "commands" for t in n.targets)
		for n in tree.body
	)


# --- scrubbing (security review) ------------------------------------------------------------------


def test_scrub_removes_known_secrets_an_admin_password_argument_and_line_breaks():
	from retail_tower_erpnext_connector.provisioner.errors import scrub

	admin_pw = "Zk3x" + "yz_abcDEF"
	text = (
		f"Command ['bench', 'new-site', '--admin-password', '{admin_pw}', 'site'] failed"
		+ chr(10)
		+ "forged line"
	)
	out = scrub(text)
	assert admin_pw not in out and chr(10) not in out
	known = "rtc_" + "SECRET123"
	assert known not in scrub(f"KeyError: '{known}'", 1000, (known,))


def test_a_timeout_error_does_not_carry_the_argument_list():
	from retail_tower_erpnext_connector.provisioner.bench import SubprocessBench

	with pytest.raises(TimeoutError) as err:
		SubprocessBench().run(
			[sys.executable, "-c", "import time; time.sleep(30)", "--admin-password", "hunter2"],
			timeout=1,
			cwd=str(APP_ROOT),
		)
	assert err.value.__cause__ is None and err.value.__suppress_context__
	assert "hunter2" not in str(err.value)


# --- config validation ---------------------------------------------------------------------------


@pytest.mark.parametrize(
	("key", "value"),
	[
		("db_name_prefix", "x`; drop"),
		("db_name_prefix", "-rtp"),
		("db_name_prefix", "a" * 40),
		("max_sites", 0),
		("step_budget_seconds", "300"),
		("queue_depth_threshold", True),
	],
)
def test_bad_config_values_are_refused(tmp_path, key, value):
	data = {"base_url": "https://bc.example", "bench_path": "/b", "credential_file": "/c", key: value}
	with pytest.raises(ValueError):
		load_config(_write(tmp_path / "c.json", data))


def test_no_orchestrator_module_names_frappe_even_inside_a_function():
	# A NAME-token scan: the subprocess import test cannot see a function-local import.
	for path in PKG.glob("*.py"):
		if path.stem in {"site_ops"}:
			continue
		with path.open("rb") as handle:
			names = {t.string for t in tokenize.tokenize(handle.readline) if t.type == tokenize.NAME}
		assert "frappe" not in names, path.name
