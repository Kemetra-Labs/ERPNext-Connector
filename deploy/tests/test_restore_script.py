"""RT-927 Task 5: the owner-run restore script (guards and plan; the host steps are rehearsed on lab-03)."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

LAB = Path(__file__).resolve().parents[1] / "lab"
SCRIPT = LAB / "restore.sh"

RUN = "4242"
SITE = "erp.lab.example"
DIGEST_BEFORE = "rt-erpnext:16.51.0-274589a3dd54"
KEY_VALUE = "SECRET-ENCRYPTION-KEY-VALUE-0123"
FILES = {
    "db": "20261029_101010-erp_lab_example-database.sql.gz",
    "pub": "20261029_101010-erp_lab_example-files.tar",
    "priv": "20261029_101010-erp_lab_example-private-files.tar",
    "cfg": "20261029_101010-erp_lab_example-site_config_backup.json",
}

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not installed")


def _make_run(root: Path, *, resumed: bool = False, key: bool = True, digest: str = DIGEST_BEFORE,
              corrupt: bool = False, started: str | None = "2026-10-29T09:00:00Z") -> Path:
    run_dir = root / RUN
    run_dir.mkdir(parents=True)
    contents = {
        FILES["db"]: b"db-bytes",
        FILES["pub"]: b"pub-bytes",
        FILES["priv"]: b"priv-bytes",
        FILES["cfg"]: json.dumps({"encryption_key": KEY_VALUE} if key else {"db_name": "x"}).encode(),
    }
    for name, data in contents.items():
        (run_dir / name).write_bytes(data)
    sums = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in contents.items())
    (run_dir / "SHA256SUMS").write_text(sums, encoding="utf-8", newline="\n")
    if corrupt:
        (run_dir / FILES["db"]).write_bytes(b"tampered")
    (run_dir / "compose.yml").write_text("x-erp: &erp\n  image: " + digest + "\n", encoding="utf-8", newline="\n")
    ledger = {
        "runId": RUN, "site": SITE, "digestBefore": digest, "outcome": "migrated",
        "backup": {"dir": str(run_dir), "sha256": "0" * 64, "files": list(FILES.values())},
    }
    if started is not None:
        ledger["startedAt"] = started
    if resumed:
        ledger["resumedAt"] = "2026-10-29T10:00:00Z"
    (run_dir / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8", newline="\n")
    return run_dir


def _run(root: Path, *args: str, path_prefix: Path | None = None) -> subprocess.CompletedProcess:
    # The same bash the test process would find (on Windows a bare "bash" can be the WSL launcher).
    env = {**os.environ, "RT_PYTHON": sys.executable.replace("\\", "/"),
           # The host-wide marker; tests point it at their own tmp dir (never under --backups-root).
           "RT_RESUME_MARKER": (root / "host" / "last-resumed-at").as_posix()}
    if path_prefix is not None:
        env["PATH"] = os.pathsep.join([str(path_prefix), env["PATH"]])
    return subprocess.run([BASH, SCRIPT.name, *args], cwd=LAB, capture_output=True, text=True, env=env, check=False)


def _dry(root: Path, run: str = RUN, **kw) -> subprocess.CompletedProcess:
    return _run(root, run, "--backups-root", root.as_posix(), "--dry-run", **kw)


def test_bash_syntax():
    proc = subprocess.run([BASH, "-n", SCRIPT.name], cwd=LAB, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


def test_refuses_after_resume(tmp_path):
    _make_run(tmp_path, resumed=True)
    proc = _dry(tmp_path)
    assert proc.returncode == 3
    assert "forward-fix only" in proc.stderr
    assert "PLAN" not in proc.stdout


def test_refuses_after_a_partial_resume_even_if_the_run_failed(tmp_path):
    run_dir = _make_run(tmp_path, resumed=True)
    ledger = json.loads((run_dir / "ledger.json").read_text(encoding="utf-8"))
    ledger["outcome"] = "failed"
    (run_dir / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8", newline="\n")
    assert _dry(tmp_path).returncode == 3


def test_refuses_missing_encryption_key(tmp_path):
    _make_run(tmp_path, key=False)
    proc = _dry(tmp_path)
    assert proc.returncode == 4
    assert "encryption_key" in proc.stderr


def test_refuses_a_backup_that_fails_its_checksums(tmp_path):
    _make_run(tmp_path, corrupt=True)
    proc = _dry(tmp_path)
    assert proc.returncode == 4
    assert "checksum" in proc.stderr.lower()


def test_plan_order(tmp_path):
    _make_run(tmp_path)
    proc = _dry(tmp_path)
    assert proc.returncode == 0, proc.stderr
    steps = re.findall(r"^PLAN \d+ ([a-z-]+)", proc.stdout, re.M)
    assert steps == ["stop", "new-volume", "set-encryption-key", "force-restore", "previous-image", "ping"]


def test_secret_value_is_never_printed(tmp_path):
    _make_run(tmp_path)
    proc = _dry(tmp_path)
    assert proc.returncode == 0 and "PLAN" in proc.stdout, "the plan must print for this check to mean anything"
    assert KEY_VALUE not in proc.stdout + proc.stderr


def test_dry_run_runs_no_docker_command(tmp_path):
    _make_run(tmp_path)
    fake = tmp_path / "bin"
    fake.mkdir()
    marker = tmp_path / "docker-was-called"
    (fake / "docker").write_text(f"#!/bin/sh\ntouch '{marker.as_posix()}'\nexit 1\n", encoding="utf-8", newline="\n")
    (fake / "docker").chmod(0o755)
    proc = _dry(tmp_path, path_prefix=fake)
    assert proc.returncode == 0, proc.stderr
    assert not marker.exists()


@pytest.mark.parametrize("args", [
    ["../x", "--dry-run"],
    ["12 34", "--dry-run"],
    ["--dry-run"],
    ["4242", "--dry-run", "--unknown"],
])
def test_bad_arguments_exit_2(tmp_path, args):
    proc = _run(tmp_path, *args)
    assert proc.returncode == 2


def test_missing_ledger_exits_2(tmp_path):
    proc = _dry(tmp_path)
    assert proc.returncode == 2


def test_invalid_digest_before_exits_2(tmp_path):
    _make_run(tmp_path, digest="latest")
    assert _dry(tmp_path).returncode == 2


def test_never_deletes_the_old_volume_or_downgrades_in_place():
    text = SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert "volume rm" not in text and "volume prune" not in text
    assert "down -v" not in text and "--volumes" not in text
    assert "DROP DATABASE" not in text.upper()


def test_encryption_key_is_copied_before_the_force_restore():
    text = SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert text.index("set-encryption-key") < text.index("force-restore")
    assert "--force restore" in text


def test_refuses_when_any_later_run_resumed_the_workers(tmp_path):
    """Deploy #0 flow: run A held, run B resumed. Restoring A would lose what B posted."""
    _make_run(tmp_path)
    _marker(tmp_path, "2026-10-29T09:30:00Z")
    proc = _dry(tmp_path)
    assert proc.returncode == 3
    assert "forward-fix only" in proc.stderr


def test_an_older_host_resume_does_not_block_this_run(tmp_path):
    _make_run(tmp_path)
    _marker(tmp_path, "2026-10-28T08:00:00Z")
    assert _dry(tmp_path).returncode == 0


def test_a_host_resume_with_no_run_start_time_refuses(tmp_path):
    _make_run(tmp_path, started=None)
    _marker(tmp_path, "2026-10-28T08:00:00Z")
    assert _dry(tmp_path).returncode == 3


def _marker(root: Path, when: str) -> None:
    (root / "host").mkdir(exist_ok=True)
    (root / "host" / "last-resumed-at").write_text(when + "\n", encoding="utf-8", newline="\n")


def test_a_marker_under_backups_root_is_not_the_host_marker(tmp_path):
    """The marker path is host-wide, independent of --backups-root (PR #94 review)."""
    _make_run(tmp_path)
    (tmp_path / "last-resumed-at").write_text("2026-10-29T09:30:00Z\n", encoding="utf-8", newline="\n")
    assert _dry(tmp_path).returncode == 0
    _marker(tmp_path, "2026-10-29T09:30:00Z")
    assert _dry(tmp_path).returncode == 3


def test_marker_is_read_and_written_at_one_host_path():
    text = SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert 'MARKER="${RT_RESUME_MARKER:-/var/backups/rt-erp/last-resumed-at}"' in text
    assert "$BACKUPS_ROOT/last-resumed-at" not in text


def test_host_marker_is_checked_again_after_the_lock():
    """A deploy may resume between the first check and the lock (PR #94 review)."""
    text = SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
    lock = text.index("flock -n 9")
    assert "check_host_marker" in text[: text.index('echo "PLAN 1')]
    assert text.index("check_host_marker", lock) < text.index('echo "STEP 1 stop"')


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_takes_the_deploy_lock_before_stopping_anything():
    """A restore must not stop the stack while RT-ErpLabDeploy swaps or migrates (PR #94 review)."""
    text = _script()
    lock = text.index("flock -n 9")
    assert "/run/rt-erp-deploy.lock" in text[:lock]
    assert lock < text.index('echo "STEP 1 stop"') or lock < text.index('"$DC" stop')
    assert text.index("dry run: nothing was changed") < lock, "a dry run takes no lock"


def test_records_the_resume_before_the_services_start_again():
    """STEP 5 starts the workers: the host marker must close every earlier restore window first."""
    text = _script()
    step5 = text[text.index('echo "STEP 5 previous-image"'):]
    assert step5.index('mv -f "$MARKER.tmp" "$MARKER"') < step5.index("dcx up -d")


def test_reads_the_mariadb_root_password_variable_lab03_sets():
    """lab-03's mariadb container sets MARIADB_ROOT_PASSWORD (read-only host check, RT-927 Task 7);
    MYSQL_ROOT_PASSWORD stays as the fallback for older images."""
    text = SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
    line = next(line for line in text.splitlines() if line.startswith("RT_DB_ROOT_PW="))
    assert line.index("MARIADB_ROOT_PASSWORD") < line.index("MYSQL_ROOT_PASSWORD")
    assert '[ -n "$RT_DB_ROOT_PW" ] || die 5' in text
