"""RT-927 Task 4: the SSM document RT-ErpLabDeploy (the security boundary on lab-03).

Static checks on the JSON file, plus `bash -n` on the script it carries. The script itself only
runs on the lab host; these tests pin the order and the guards the design (RT-926 v2) requires.
"""

import base64
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

LAB = Path(__file__).resolve().parents[1] / "lab"
DOC_PATH = LAB / "ssm" / "RT-ErpLabDeploy.json"

SERVICES = ("backend", "frontend", "websocket", "scheduler", "queue-short", "queue-long")
IMAGE_RE = r"^(rt-erpnext:16\.51\.0-[0-9a-f]{12}|ghcr\.io/kemetra-labs/rt-erpnext@sha256:[0-9a-f]{64})$"


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n")


@pytest.fixture(scope="module")
def doc() -> dict:
    return json.loads(_lf(DOC_PATH.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def run_lines(doc) -> list[str]:
    steps = doc["mainSteps"]
    assert len(steps) == 1 and steps[0]["action"] == "aws:runShellScript"
    return steps[0]["inputs"]["runCommand"]


@pytest.fixture(scope="module")
def body(run_lines) -> str:
    text = "\n".join(run_lines)
    match = re.search(r"<<'RT_ERP_SCRIPT'\n(.*?)\nRT_ERP_SCRIPT\n", text, re.S)
    assert match, "the script must sit between <<'RT_ERP_SCRIPT' and RT_ERP_SCRIPT"
    return match.group(1)


def _pos(text: str, needle: str) -> int:
    index = text.find(needle)
    assert index >= 0, f"missing: {needle}"
    return index


def test_document_shape(doc):
    assert doc["schemaVersion"] == "2.2"
    step = doc["mainSteps"][0]
    assert step["precondition"] == {"StringEquals": ["platformType", "Linux"]}
    assert step["inputs"]["timeoutSeconds"] == "3600"


def test_document_is_well_under_the_ssm_size_limit():
    assert len(DOC_PATH.read_bytes()) < 60_000


def test_params_env_var_only_and_no_placeholders(doc, run_lines, body):
    assert doc["parameters"]
    for name, param in doc["parameters"].items():
        assert param["interpolationType"] == "ENV_VAR", name
        assert param["type"] == "String", name
        assert "allowedPattern" in param or "allowedValues" in param, name
    assert "{{" not in "\n".join(run_lines)
    assert "{{" not in body


def test_parameter_set(doc):
    assert set(doc["parameters"]) == {
        "TargetDigest", "CurrentImages", "CurrentTree", "ExpectedVersion", "Hold", "RunId",
        "SaveCurrentImage", "MinFreeGiB", "JiraKey", "RollbackClass",
    }
    params = doc["parameters"]
    assert params["TargetDigest"]["allowedPattern"] == "^sha256:[0-9a-f]{64}$"
    assert params["CurrentTree"]["allowedPattern"] == "^[0-9a-f]{40}$"
    assert params["ExpectedVersion"]["allowedPattern"] == "^[0-9]+\\.[0-9]+\\.[0-9]+$"
    assert params["RunId"]["allowedPattern"] == "^[0-9]{1,20}$"
    assert params["MinFreeGiB"]["allowedPattern"] == "^[0-9]{1,3}$"
    assert params["JiraKey"]["allowedPattern"] == "^RT-[1-9][0-9]{0,5}$"
    for name in ("Hold", "SaveCurrentImage"):
        assert params[name]["allowedValues"] == ["true", "false"]
    assert params["RollbackClass"]["allowedValues"] == ["image-only", "restore"]


def test_every_parameter_is_read_and_validated_in_the_script(doc, body):
    for name in doc["parameters"]:
        assert f"SSM_{name}" in body, name


def test_current_images_value_pattern_is_enforced(body):
    assert IMAGE_RE in body.replace("\\\\", "\\") or IMAGE_RE.replace("\\.", "[.]") in body


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_bash_syntax(body, tmp_path):
    script = tmp_path / "erp-deploy.sh"
    script.write_bytes(body.encode("utf-8"))
    # A bare file name from its own directory: a Windows path does not survive Git Bash.
    proc = subprocess.run(["bash", "-n", script.name], cwd=tmp_path, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


def test_step_order(body):
    markers = ["RT_GATE check", "RT_GATE stop", "RT_GATE backup", "RT_GATE swap", "RT_GATE migrate",
               "RT_GATE verify", "RT_GATE resume"]
    positions = [_pos(body, m) for m in markers]
    assert positions == sorted(positions), dict(zip(markers, positions, strict=True))


def test_workers_stopped_before_backup(body):
    stop = _pos(body, "docker stop -t 120")
    backup = _pos(body, 'bench --site "$SITE" backup --with-files')
    assert stop < backup
    stop_line = next(line for line in body.splitlines() if "docker stop -t 120" in line)
    window = body[stop - 400: stop + 400]
    for worker in ("scheduler", "queue-short", "queue-long"):
        assert worker in window or worker in stop_line, worker


def test_no_swap_without_a_passed_backup(body):
    assert _pos(body, "RT_GATE backup=pass") < _pos(body, "RT_GATE swap")
    assert _pos(body, "gzip -t") < _pos(body, "RT_GATE backup=pass")
    assert _pos(body, "tar -tf") < _pos(body, "RT_GATE backup=pass")
    assert "encryption_key" in body


def test_no_start_of_workers_before_resume(body):
    lines = body.splitlines()
    resume_index = next(i for i, line in enumerate(lines) if "start scheduler queue-short queue-long" in line)
    for line in lines[:resume_index]:
        if not any(w in line for w in ("scheduler", "queue-short", "queue-long")):
            continue
        assert " up -d" not in line, line
        assert not re.search(r"\bstart\b(?!-)", line.replace("--no-start", "")), line
    assert any("--no-start" in line and "scheduler" in line for line in lines[:resume_index])


def test_workers_are_created_not_started_and_backend_is_recreated_without_deps(body):
    assert "up -d --no-deps backend frontend websocket" in body
    assert "up --no-start --no-deps scheduler queue-short queue-long" in body


def test_flush_cache_only_and_before_migrate(body):
    flush_lines = [line for line in body.splitlines() if "FLUSHALL" in line]
    assert flush_lines and all("redis-cache" in line for line in flush_lines)
    recreate = _pos(body, "up -d --no-deps backend frontend websocket")
    assert recreate < _pos(body, "FLUSHALL") < _pos(body, 'bench --site "$SITE" migrate')


def test_single_site_required(body):
    assert "site-not-unique" in body
    assert "currentsite.txt" in body


def test_hold_mode_writes_no_resume(body):
    held = _pos(body, "RT_OUTCOME=held")
    main_flow = body[_pos(body, "# ---- step 1: check ----"):]
    # Before the hold exit, the main flow never records a resume (recover_workers runs only on a failure).
    assert "mark_resumed" not in main_flow[: main_flow.index("RT_OUTCOME=held")]
    assert "resumedAt" not in main_flow[: main_flow.index("RT_OUTCOME=held")]
    window = body[held - 300: held + 200]
    assert 'HOLD" = "true"' in body[_pos(body, "RT_GATE verify"):held]
    assert "exit 0" in window


def test_resumed_at_is_written_before_workers_start_and_before_post_resume_checks(body):
    resume = _pos(body, "GATE=resume")
    written = body.index("mark_resumed", resume)
    started = _pos(body, "start scheduler queue-short queue-long")
    post = _pos(body, "RT_GATE postresume")
    assert written < started < post
    assert _pos(body, "RT_RESUMED_AT=") > started - 400


def test_disk_check_is_part_of_step_one(body):
    check_pass = _pos(body, "RT_GATE check=pass")
    disk = _pos(body, "MIN_FREE_GIB")
    assert "df " in body[:check_pass]
    assert disk < check_pass


def test_step_one_checks(body):
    check_end = _pos(body, "RT_GATE check=pass")
    head = body[:check_end]
    for needle in ("compose.yml", "erp.env", "python3", "ping", "docker pull", "tree_hash"):
        assert needle in head, needle
    for service in SERVICES:
        assert service in head, service


def test_mixed_current_images_are_compared_per_service(body):
    assert "CURRENT_IMAGES" in body
    assert "current-images-map-incomplete" in body
    assert "current-image-mismatch" in body


def test_tree_must_match_the_declared_current_tree(body):
    assert "current-tree-mismatch" in body
    assert "rt.connector.tree" in body


def test_swap_edits_exactly_one_image_line_under_the_anchor(body):
    assert "x-erp" in body
    assert "x-erp-image-line-count" in body


def test_all_six_services_must_run_one_image_after_the_swap(body):
    assert "RT_DIGEST_AFTER=" in body
    assert "one-digest-mismatch" in body
    assert 'for svc in $SERVICES; do\n  [ "$(ctr_image_id "$(cid_of "$svc")")" = "$TARGET_IMAGE_ID" ]' in body
    assert 'SERVICES="backend frontend websocket scheduler queue-short queue-long"' in body


def test_probe_is_run_before_resume_and_each_check_is_a_gate(body):
    assert _pos(body, "preresume_probe.py") < _pos(body, "start scheduler queue-short queue-long")
    assert "RT_GATE probe_" in body


def test_stdout_contract(body):
    assert 'exec 3>&1 >>"$LOG" 2>&1' in body
    lines = body.splitlines()
    for line in lines:
        if ">&3" in line:
            assert line.strip().startswith("say()"), line
    said = re.findall(r'^\s*say "([^"]*)"', body, re.M)
    assert said, "the script reports through say"
    for text in said:
        assert text.startswith("RT_"), text
    # Before the log redirect, the only direct stdout writes are RT_ lines too.
    head = body[: _pos(body, 'exec 3>&1 >>"$LOG" 2>&1')]
    for match in re.finditer(r"printf '([^']*)'", head):
        assert match.group(1).startswith("RT_"), match.group(0)


def test_no_stdout_bypass_through_tee_or_dev_tty(body):
    assert "/dev/tty" not in body
    assert "| tee" not in body


def test_never_prints_secrets(body):
    assert not re.search(r"cat\s+\S*erp\.env", body)
    assert "printenv" not in body
    assert not re.search(r"(^|[;&|(]\s*)env\s*(\||$)", body, re.M), "the env command would dump secrets"
    for line in body.splitlines():
        if re.search(r"docker (image )?inspect", line) and not line.lstrip().startswith("#"):
            assert "| python3" in line, f"inspect output must go to python3, never to the log: {line}"


def test_flock_serializes_deploys(body):
    assert "flock -n" in body
    assert "/run/rt-erp-deploy.lock" in body


def test_log_is_root_only_under_the_log_dir(body):
    assert "/var/log/rt-deploy" in body
    assert "umask 077" in body
    assert "erp-$RUN_ID.log" in body or "erp-${RUN_ID}.log" in body


def test_backup_goes_to_a_dated_run_folder_outside_the_volumes(body):
    assert "/var/backups/rt-erp" in body
    assert "ledger.json" in body
    assert "SHA256SUMS" in body


def test_save_current_image_uses_docker_save(body):
    assert "docker save" in body
    assert "SAVE_CURRENT_IMAGE" in body


def test_failure_before_the_swap_restarts_the_workers(body):
    assert "SWAPPED" in body
    assert "workers-restarted" in body


def test_outcome_lines(body):
    for outcome in ("RT_OUTCOME=deployed", "RT_OUTCOME=held", "RT_OUTCOME=failed"):
        assert outcome in body


def test_gate_failure_names_a_reason_code(body):
    assert 'say "RT_GATE $GATE=fail:$1"' in body


def test_helpers_are_embedded_and_equal_the_repo_files(body):
    for const, repo_file in (("RT_PROBE_B64", "preresume_probe.py"), ("RT_TREEHASH_B64", "tree_hash.py")):
        match = re.search(rf"^{const}='([A-Za-z0-9+/=]+)'$", body, re.M)
        assert match, f"{const} is not a single-quoted base64 line"
        embedded = base64.b64decode(match.group(1)).decode("utf-8")
        assert embedded == _lf((LAB / repo_file).read_text(encoding="utf-8")), (
            f"{const} is stale: refresh it from deploy/lab/{repo_file}"
        )


def test_recovering_the_workers_is_recorded_as_a_resume(body):
    """Workers restarted after a failure post again: the restore window must close (review C1)."""
    func = body[_pos(body, "recover_workers() {"):_pos(body, "stop() {")]
    assert func.index("mark_resumed") < func.index('start $WORKERS')


def test_every_resume_writes_the_host_wide_marker_before_the_workers_run(body):
    mark = body[_pos(body, "mark_resumed() {"):]
    mark = mark[: mark.index("\n}\n")]
    assert "RESUME_MARKER" in mark and "ledger resumedAt=" in mark
    assert 'RESUME_MARKER="$BACKUP_ROOT/last-resumed-at"' in body
    resume = body[_pos(body, "GATE=resume"):]
    assert resume.index("mark_resumed") < resume.index("start scheduler queue-short queue-long")


def test_err_trap_runs_only_in_the_main_shell(body):
    """With set -E the trap also fires inside $(...) subshells (review I3)."""
    trap_line = next(line for line in body.splitlines() if line.startswith("trap '") and "ERR" in line)
    assert '[ "$BASHPID" = "$$" ] || exit 1' in trap_line


def test_workers_are_not_restarted_when_the_resume_cannot_be_recorded(body):
    """A failed marker write must leave the workers down: posting may not resume unrecorded (PR #94 review)."""
    func = body[_pos(body, "recover_workers() {"):_pos(body, "stop() {")]
    assert "mark_resumed || true" not in func
    assert func.index("if ! mark_resumed") < func.index("start $WORKERS")
    assert "recover=fail:resume-not-recorded" in func
    mark = body[_pos(body, "mark_resumed() {"):]
    mark = mark[: mark.index("\n}\n")]
    assert mark.count("|| return 1") >= 2, "every write in mark_resumed must fail the function"


def test_allowed_patterns_fit_the_ssm_regex_engine(doc):
    """SSM validates allowedPattern with Go RE2, which refuses a repeat count above 1000.

    create-document failed on lab-03 with `invalid repeat count: {2,2046}` (RT-927 Task 7).
    """
    for name, param in doc["parameters"].items():
        for low, high in re.findall(r"\{(\d+)(?:,(\d+))?\}", param.get("allowedPattern", "")):
            assert int(high or low) <= 1000, f"{name}: repeat {{{low},{high}}} is above RE2's limit"


def test_current_images_limit_is_the_same_in_the_document_and_the_script(doc, body):
    pattern = doc["parameters"]["CurrentImages"]["allowedPattern"]
    limit = int(re.search(r",(\d+)\}", pattern).group(1)) + 2
    assert f'[ "${{#CURRENT_IMAGES}}" -le {limit} ]' in body


def _db_volume_helper(body: str) -> str:
    match = re.search(r"^PY_DB_VOLUME='(.*?)'$", body, re.S | re.M)
    assert match, "PY_DB_VOLUME must be a single-quoted python block"
    return match.group(1)


def _resolve(body: str, mode: str, data: object) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", _db_volume_helper(body), mode], input=json.dumps(data),
                          capture_output=True, text=True, check=False)


def test_step_one_refuses_a_live_db_volume_that_differs_from_compose(body):
    """lab-03 ran MariaDB on a restore volume while compose.yml still mounted erp_db: the next
    recreate would have gone back to the old database (RT-927). Checked before anything stops."""
    check = body[_pos(body, "# ---- step 1: check ----"):_pos(body, "RT_GATE check=pass")]
    assert 'cid_of mariadb' in check
    assert "config --format json" in check
    assert 'stop "mariadb-volume-differs-from-compose"' in check
    assert check.index("mariadb-volume-differs-from-compose") < check.index("docker pull")
    # dc config interpolates erp.env: its output goes straight to python3, never to a file or the log.
    config_line = next(line for line in check.splitlines() if "config --format json" in line)
    assert "| python3 -c \"$PY_DB_VOLUME\" compose" in config_line
    inspect_line = next(line for line in check.splitlines() if "docker inspect \"$MARIADB_CID\"" in line)
    assert "| python3 -c \"$PY_DB_VOLUME\" container" in inspect_line


COMPOSE_CONFIG = {
    "name": "rt-erp",
    "services": {"mariadb": {"volumes": [
        {"type": "bind", "source": "/opt/rt-erp/my.cnf", "target": "/etc/mysql/conf.d/my.cnf"},
        {"type": "volume", "source": "erp_db", "target": "/var/lib/mysql", "volume": {}},
    ]}},
    "volumes": {"erp_db": {"name": "rt-erp_erp_db"},
                "rt-erp_mariadb-restore-7": {"name": "rt-erp_mariadb-restore-7", "external": True}},
}


def _container(mounts: list[dict]) -> list[dict]:
    return [{"Mounts": mounts}]


def test_db_volume_helper_resolves_the_compose_source_through_its_volume_name(body):
    proc = _resolve(body, "compose", COMPOSE_CONFIG)
    assert proc.returncode == 0 and proc.stdout.strip() == "rt-erp_erp_db", proc.stderr


def test_db_volume_helper_resolves_an_external_restore_volume(body):
    data = json.loads(json.dumps(COMPOSE_CONFIG))
    data["services"]["mariadb"]["volumes"][1]["source"] = "rt-erp_mariadb-restore-7"
    proc = _resolve(body, "compose", data)
    assert proc.stdout.strip() == "rt-erp_mariadb-restore-7", proc.stderr


def test_db_volume_helper_reads_the_running_container_mount(body):
    proc = _resolve(body, "container", _container([
        {"Type": "bind", "Source": "/x", "Destination": "/etc/mysql/conf.d/my.cnf"},
        {"Type": "volume", "Name": "rt-erp_mariadb-restore-7", "Destination": "/var/lib/mysql"},
    ]))
    assert proc.returncode == 0 and proc.stdout.strip() == "rt-erp_mariadb-restore-7", proc.stderr


@pytest.mark.parametrize("mode,data", [
    ("container", [{"Mounts": [{"Type": "bind", "Source": "/srv/db", "Destination": "/var/lib/mysql"}]}]),
    ("container", [{"Mounts": []}]),
    ("compose", {"services": {"mariadb": {"volumes": [
        {"type": "bind", "source": "/srv/db", "target": "/var/lib/mysql"}]}}, "volumes": {}}),
    ("compose", {"services": {"backend": {}}, "volumes": {}}),
])
def test_db_volume_helper_fails_on_anything_but_one_named_volume(body, mode, data):
    proc = _resolve(body, mode, data)
    assert proc.returncode != 0 and proc.stdout.strip() == ""


# ---- the site scheduler (RT-927, lab-03 2026-10-29): a disabled scheduler must never pass as resumed ----

POLL_METHOD = "retail_tower_erpnext_connector.connector.posting.poller.run_posting_poll"


def test_check_gate_stops_a_deploy_whose_site_scheduler_is_disabled(body):
    """The deploy's job is to resume posting: a disabled scheduler stops it before anything is stopped."""
    check = body[_pos(body, "# ---- step 1: check ----"):_pos(body, "RT_GATE check=pass")]
    assert 'bench --site "$SITE" scheduler status' in check
    assert 'stop "scheduler-disabled"' in check
    assert 'stop "scheduler-status-unreadable"' in check
    assert check.index('stop "scheduler-disabled"') < check.index("docker pull")
    assert _pos(body, 'stop "scheduler-disabled"') < _pos(body, "STOPPED=1")


def test_postresume_waits_for_the_posting_poll_to_run_after_the_resume(body):
    post = body[_pos(body, "GATE=postresume"):_pos(body, "RT_GATE postresume=pass")]
    assert 'stop "posting-poll-did-not-run"' in post
    assert "$RESUMED_AT" in post
    assert "PY_POLL_RAN" in post
    assert POLL_METHOD in body or "$CONNECTOR_APP.connector.posting.poller.run_posting_poll" in body
    # bounded by wall clock: 180 s from the start of the wait, read time included
    assert "POLL_DEADLINE=$((SECONDS + 180))" in post
    # the deadline is checked before every sleep, and no sleep crosses it
    assert 'pause=$((POLL_DEADLINE - SECONDS))' in post and '[ "$pause" -le 0 ]' in post
    assert '[ "$pause" -le 15 ] || pause=15' in post and 'sleep "$pause"' in post
    assert "sleep 15" not in post
    assert "polled" not in post
    # each read is capped at 30 s and at the time left, so a hung docker exec cannot pass the deadline
    assert 'left=$((POLL_DEADLINE - SECONDS)); [ "$left" -le 30 ] || left=30' in post
    assert 'timeout "$left" docker exec' in post
    # a helper that cannot read the job (import, connect, query) is not a scheduler that did not run
    assert post.index('stop "posting-poll-unreadable"') < post.index('stop "posting-poll-did-not-run"')
    assert _pos(body, "RT_GATE resume=pass") < _pos(body, 'stop "posting-poll-did-not-run"')


def test_postresume_failures_are_monitoring_never_a_rollback(body):
    post = body[_pos(body, "GATE=postresume"):_pos(body, "RT_GATE postresume=pass")]
    for word in ("restore", "docker stop", "dc\" stop", "compose.yml"):
        assert word not in post, word


def _poll_helper(body: str) -> str:
    match = re.search(r"^PY_POLL_RAN='(.*?)'$", body, re.S | re.M)
    assert match, "PY_POLL_RAN must be a single-quoted python block"
    return match.group(1)


FAKE_FRAPPE = '''import os
from datetime import datetime
class _DB:
    def get_value(self, doctype, filters, field):
        assert doctype == "Scheduled Job Type" and field == "last_execution", (doctype, field)
        assert filters == {"method": os.environ["EXPECT_METHOD"]}, filters
        if os.environ.get("FAKE_RAISE"):
            raise RuntimeError("no database")
        value = os.environ.get("FAKE_LAST") or None
        # Frappe returns a datetime for a Datetime field
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f") if value else None
db = _DB()
def init(site, sites_path):
    assert site == "lab.localhost" and sites_path == "."
def connect():
    pass
def destroy():
    pass
'''

FAKE_FRAPPE_UTILS = '''from datetime import datetime
def get_datetime(value):
    if isinstance(value, datetime):
        return value
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f")
def get_system_timezone():
    return "Africa/Cairo"
'''


def _poll(body: str, tmp_path: Path, last: str | None, resumed: str,
          fail: bool = False) -> subprocess.CompletedProcess:
    pkg = tmp_path / "fake" / "frappe"
    (pkg / "utils").mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(FAKE_FRAPPE, encoding="utf-8")
    (pkg / "utils" / "__init__.py").write_text(FAKE_FRAPPE_UTILS, encoding="utf-8")
    env = {**__import__("os").environ, "PYTHONPATH": str(tmp_path / "fake"), "FAKE_LAST": last or "",
           "EXPECT_METHOD": POLL_METHOD, "FAKE_RAISE": "1" if fail else ""}
    return subprocess.run([sys.executable, "-c", _poll_helper(body), "lab.localhost", resumed, POLL_METHOD],
                          capture_output=True, text=True, env=env, check=False)


def _cairo_available() -> bool:
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo("Africa/Cairo")
    except Exception:  # no tz database on this host
        return False
    return True


@pytest.mark.skipif(not _cairo_available(), reason="no Africa/Cairo in the tz database")
@pytest.mark.parametrize("last,ran", [
    # 10:00Z is 13:00 in Cairo (UTC+3 in July). 12:30 local is 09:30Z: BEFORE the resume, although a
    # naive comparison of the stored local time with the UTC resume time says it is later.
    ("2026-07-15 12:30:00.000000", False),
    ("2026-07-15 12:59:59.000000", False),
    ("2026-07-15 13:00:30.123456", True),
    (None, False),
])
def test_poll_helper_compares_the_site_local_last_execution_with_the_utc_resume(body, tmp_path, last, ran):
    proc = _poll(body, tmp_path, last, "2026-07-15T10:00:00Z")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == ("posting-poll=ran" if ran else "posting-poll=waiting")


@pytest.mark.skipif(not _cairo_available(), reason="no Africa/Cairo in the tz database")
def test_poll_helper_reports_an_error_instead_of_waiting_when_it_cannot_read(body, tmp_path):
    proc = _poll(body, tmp_path, "2026-07-15 13:00:30.000000", "2026-07-15T10:00:00Z", fail=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "posting-poll=error"


@pytest.mark.skipif(not _cairo_available(), reason="no Africa/Cairo in the tz database")
def test_poll_helper_fails_closed_in_the_repeated_hour_when_dst_ends(body, tmp_path):
    """Egypt falls back at 24:00 on 2026-10-29: 23:00-23:59 local happens twice. A run at 21:30Z (the
    second 23:30) is read as the first one (20:30Z, fold=0): the gate may wait in vain, never pass early."""
    proc = _poll(body, tmp_path, "2026-10-29 23:30:00.000000", "2026-10-29T21:10:00Z")
    assert proc.stdout.strip().splitlines()[-1] == "posting-poll=waiting"
    early = _poll(body, tmp_path, "2026-10-29 23:05:00.000000", "2026-10-29T20:10:00Z")
    assert early.stdout.strip().splitlines()[-1] == "posting-poll=waiting"


def test_scheduler_stop_reasons_are_in_the_script(body):
    """That every stop reason fits the workflow's RESULT_RE is checked from the root, in
    .github/release/test_erp_lab_workflows.py: a product test may not read the root .github/."""
    for reason in ("scheduler-disabled", "scheduler-status-unreadable", "posting-poll-did-not-run",
                   "posting-poll-unreadable"):
        assert f'stop "{reason}"' in body
        assert re.fullmatch(r"[a-z0-9-]+", reason)
