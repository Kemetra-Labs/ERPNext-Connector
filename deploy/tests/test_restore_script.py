"""RT-927 Task 5: the owner-run restore script (guards and plan; the host steps are rehearsed on lab-03)."""

import gzip
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

NEW_VOL = f"rt-erp_mariadb-restore-{RUN}"
# The shape of lab-03's /opt/rt-erp/compose.yml: one x-erp image, mariadb and redis on their own
# images, exactly one /var/lib/mysql mount and one top-level volumes: key.
COMPOSE = """x-erp: &erp
  image: {digest}
  restart: unless-stopped

services:
  backend:
    <<: *erp
  mariadb:
    image: mariadb:11.8
    volumes:
      - {db_vol}:/var/lib/mysql
  redis-cache:
    image: redis:6.2-alpine

volumes:
{extra_vols}  erp_db:
  sites:
"""


def _compose(digest: str = DIGEST_BEFORE, db_vol: str = "erp_db", extra_vols: str = "") -> str:
    return COMPOSE.format(digest=digest, db_vol=db_vol, extra_vols=extra_vols)


def _dump(scheduler: str | None) -> bytes:
    """A gzip dump with the tabSingles row bench backup writes for System Settings.enable_scheduler."""
    rows = "('System Settings','country','Egypt')"
    if scheduler is not None:
        rows += f",('System Settings','enable_scheduler','{scheduler}')"
    text = f"INSERT INTO `tabSingles` VALUES {rows};\n"
    return gzip.compress(text.encode(), mtime=0)


def _make_run(root: Path, *, resumed: bool = False, key: bool = True, digest: str = DIGEST_BEFORE,
              corrupt: bool = False, started: str | None = "2026-10-29T09:00:00Z",
              compose: str | None = None, scheduler: str | None = "1", dump: bytes | None = None) -> Path:
    run_dir = root / RUN
    run_dir.mkdir(parents=True)
    contents = {
        FILES["db"]: dump if dump is not None else _dump(scheduler),
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
    (run_dir / "compose.yml").write_text(compose if compose is not None else _compose(digest),
                                         encoding="utf-8", newline="\n")
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


def _run(root: Path, *args: str, path_prefix: Path | None = None,
         extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    # The same bash the test process would find (on Windows a bare "bash" can be the WSL launcher).
    env = {**os.environ, "RT_PYTHON": sys.executable.replace("\\", "/"),
           # The host-wide marker; tests point it at their own tmp dir (never under --backups-root).
           "RT_RESUME_MARKER": (root / "host" / "last-resumed-at").as_posix(),
           **(extra_env or {})}
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
    assert steps == ["previous-image-present", "stop", "new-volume", "set-encryption-key", "force-restore",
                     "previous-image", "ping"]


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
    assert step5.index('mv -f "$MARKER.tmp" "$MARKER"') < step5.index('"$DC" up -d')


def test_reads_the_mariadb_root_password_variable_lab03_sets():
    """lab-03's mariadb container sets MARIADB_ROOT_PASSWORD (read-only host check, RT-927 Task 7);
    MYSQL_ROOT_PASSWORD stays as the fallback for older images."""
    text = SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
    line = next(line for line in text.splitlines() if line.startswith("RT_DB_ROOT_PW="))
    assert line.index("MARIADB_ROOT_PASSWORD") < line.index("MYSQL_ROOT_PASSWORD")
    assert '[ -n "$RT_DB_ROOT_PW" ] || die 5' in text


def test_waits_for_mariadb_to_be_healthy_before_the_restore():
    """A new volume makes MariaDB initialise first; `up -d` alone returned before it listened and
    bench restore failed with 2002 (lab-03 rehearsal, run 40865158775)."""
    text = _script()
    step2 = text[text.index('echo "STEP 2 new-volume"'):text.index('echo "STEP 3 set-encryption-key"')]
    # A bounded wait: compose treats --wait-timeout 0 as no timeout (PR #108 review).
    assert "dcx up -d --wait --wait-timeout 180 --no-deps mariadb" in step2


def test_moves_the_site_db_user_to_any_host_before_the_services_restart():
    """bench restore creates the site DB user at the backend's current IP; STEP 5 recreates the
    containers with new IPs and every request failed with 1045 (lab-03 rehearsal, run 40865158775)."""
    text = _script()
    step4 = text[text.index('echo "STEP 4 force-restore"'):text.index('echo "STEP 5 previous-image"')]
    assert step4.index("--force restore") < step4.index("RENAME USER")
    assert "db_user" in step4
    assert "die 5" in step4[step4.index("RENAME USER"):]


# ---- stub runs: the whole script against fake dc, docker, curl and flock (RT-927 restore safety) ----

FAKE_DOCKER = """#!/bin/sh
echo "docker $*" >> "$RT_FAKE_LOG"
case "$1" in
  image) grep -qxF "$3" "$RT_FAKE_IMAGES" ;;
  load) cat "$3" >> "$RT_FAKE_IMAGES" ;;
  volume) [ "$2" = create ] ;;
  exec)
    case "$*" in
      *ROOT_PASSWORD*) printf %s fake-root-pw ;;
      *'["db_user"]'*) echo erp_user ;;
      *enable-scheduler*) touch "$RT_FAKE_SCHED" ;;
      *"scheduler status"*)
        if [ -n "${RT_FAKE_SCHED_FORCE:-}" ]; then state="$RT_FAKE_SCHED_FORCE"
        elif [ -f "$RT_FAKE_SCHED" ]; then state=enabled
        else state=disabled; fi
        echo "Scheduler is $state for site erp.lab.example" ;;
      *) cat >/dev/null ;;
    esac ;;
  *) exit 0 ;;
esac
"""

FAKE_DC = """#!/bin/sh
echo "dc $*" >> "$RT_FAKE_LOG"
case "$*" in
  *"ps -q backend") echo backend-cid ;;
  *"ps -q mariadb") echo mariadb-cid ;;
esac
exit 0
"""


def _write_exec(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _stub_run(root: Path, *, present: tuple[str, ...] = (DIGEST_BEFORE, "mariadb:11.8", "redis:6.2-alpine"),
              sched_force: str = "") -> tuple[subprocess.CompletedProcess, Path, list[str]]:
    """Runs restore.sh for real (no --dry-run) with every host command faked. Returns the process,
    the stack dir (its compose.yml is what the script left) and the logged fake calls."""
    bin_dir, stack = root / "bin", root / "stack"
    bin_dir.mkdir()
    stack.mkdir()
    log, images = root / "calls.log", root / "images.txt"
    log.write_text("", encoding="utf-8")
    images.write_text("".join(ref + "\n" for ref in present), encoding="utf-8", newline="\n")
    _write_exec(bin_dir / "docker", FAKE_DOCKER)
    _write_exec(bin_dir / "curl", "#!/bin/sh\nprintf 200\n")
    _write_exec(bin_dir / "flock", "#!/bin/sh\nexit 0\n")
    _write_exec(stack / "dc", FAKE_DC)
    (stack / "compose.yml").write_text("# the swapped compose file\n", encoding="utf-8", newline="\n")
    proc = _run(root, RUN, "--backups-root", root.as_posix(), path_prefix=bin_dir, extra_env={
        "RT_STACK_DIR": stack.as_posix(), "RT_DEPLOY_LOCK": (root / "deploy.lock").as_posix(),
        "RT_FAKE_LOG": log.as_posix(), "RT_FAKE_IMAGES": images.as_posix(),
        "RT_FAKE_SCHED": (root / "sched-enabled").as_posix(), "RT_FAKE_SCHED_FORCE": sched_force})
    calls = log.read_text(encoding="utf-8").splitlines()
    return proc, stack, calls


# Written out by hand, not derived with the script's own insert logic (pr-review finding 3).
FOLDED_TAIL = f"""      - {NEW_VOL}:/var/lib/mysql
  redis-cache:
    image: redis:6.2-alpine

volumes:
  {NEW_VOL}:
    external: true
{{extra_vols}}  erp_db:
  sites:
"""


def _folded(digest: str = DIGEST_BEFORE, extra_vols: str = "") -> str:
    head = _compose(digest).split("      - erp_db:/var/lib/mysql\n")[0]
    return head + FOLDED_TAIL.format(extra_vols=extra_vols)


def test_restore_end_to_end_against_stubs(tmp_path):
    """The whole script once, STEP 1 to STEP 6: compose.yml alone names the new volume at the end."""
    _make_run(tmp_path)
    proc, stack, calls = _stub_run(tmp_path)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "restore complete" in proc.stdout
    assert "NOTE" not in proc.stdout
    assert (stack / "compose.yml").read_text(encoding="utf-8") == _folded()
    dc_calls = [c for c in calls if c.startswith("dc ")]
    assert dc_calls[-1] == "dc up -d", dc_calls
    assert calls.index("dc stop") < calls.index(f"docker volume create {NEW_VOL}")
    assert (tmp_path / "host" / "last-resumed-at").exists()


def test_step5_points_compose_at_the_new_volume_when_the_source_is_an_earlier_restore(tmp_path):
    earlier = "rt-erp_mariadb-restore-1"
    extra = f"  {earlier}:\n    external: true\n"
    _make_run(tmp_path, compose=_compose(db_vol=earlier, extra_vols=extra))
    proc, stack, _ = _stub_run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    text = (stack / "compose.yml").read_text(encoding="utf-8")
    assert f"      - {NEW_VOL}:/var/lib/mysql\n" in text
    assert f"{earlier}:/var/lib/mysql" not in text
    assert text == _folded(extra_vols=extra)


@pytest.mark.parametrize("mounts", ["", "      - erp_db:/var/lib/mysql\n      - other:/var/lib/mysql\n"])
def test_zero_or_two_mysql_mounts_exit_5_before_anything_stops(tmp_path, mounts):
    compose = _compose().replace("      - erp_db:/var/lib/mysql\n", mounts)
    _make_run(tmp_path, compose=compose)
    proc, stack, calls = _stub_run(tmp_path)
    assert proc.returncode == 5, proc.stderr
    assert "/var/lib/mysql" in proc.stderr
    assert "dc stop" not in calls
    assert (stack / "compose.yml").read_text(encoding="utf-8") == "# the swapped compose file\n"


def test_missing_previous_image_without_a_saved_tar_exits_4_before_anything_stops(tmp_path):
    _make_run(tmp_path)
    proc, _, calls = _stub_run(tmp_path, present=("mariadb:11.8", "redis:6.2-alpine"))
    assert proc.returncode == 4, proc.stderr
    assert f"previous image {DIGEST_BEFORE}" in proc.stderr
    assert "dc stop" not in calls
    assert not any(c.startswith("docker load") for c in calls)


def test_missing_previous_image_is_loaded_from_the_saved_tar(tmp_path):
    run_dir = _make_run(tmp_path)
    (run_dir / "current-image-1.tar").write_text(DIGEST_BEFORE + "\n", encoding="utf-8", newline="\n")
    proc, _, calls = _stub_run(tmp_path, present=("mariadb:11.8", "redis:6.2-alpine"))
    assert proc.returncode == 0, proc.stderr
    loads = [c for c in calls if c.startswith("docker load -i")]
    assert loads and loads[0].endswith("current-image-1.tar")
    assert calls.index(loads[0]) < calls.index("dc stop")


def test_an_erp_image_in_compose_outside_the_known_refs_exits_4(tmp_path):
    _make_run(tmp_path, compose=_compose().replace("    <<: *erp\n", "    <<: *erp\n    image: rt-erpnext:latest\n"))
    proc, _, calls = _stub_run(tmp_path)
    assert proc.returncode == 4, proc.stderr
    assert "dc stop" not in calls


def test_non_erp_images_are_checked_but_not_held_to_the_erp_pattern(tmp_path):
    """mariadb and redis run their own images: they must be present, not match IMAGE_RE."""
    _make_run(tmp_path)
    proc, _, calls = _stub_run(tmp_path, present=(DIGEST_BEFORE, "redis:6.2-alpine"))
    assert proc.returncode == 4, proc.stderr
    assert "previous image mariadb:11.8" in proc.stderr
    assert "docker image inspect -- mariadb:11.8" in calls or "docker image inspect mariadb:11.8" in calls
    assert "dc stop" not in calls


def test_restore_no_longer_needs_an_override_after_step_5():
    text = _script()
    step5 = text[text.index('echo "STEP 5 previous-image"'):]
    assert "dcx" not in step5
    assert "NOTE" not in step5
    assert "point compose.yml at $NEW_VOL" in text


def test_step5_keeps_the_indent_of_a_compose_whose_volumes_use_four_spaces(tmp_path):
    """A fixed two-space insert would nest erp_db and sites under the new volume (pr-review finding 1)."""
    compose = """services:
    backend:
        image: rt-erpnext:16.51.0-274589a3dd54
    mariadb:
        image: mariadb:11.8
        volumes:
            - erp_db:/var/lib/mysql

volumes:
    erp_db:
    sites:
"""
    _make_run(tmp_path, compose=compose)
    proc, stack, _ = _stub_run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert (stack / "compose.yml").read_text(encoding="utf-8") == f"""services:
    backend:
        image: rt-erpnext:16.51.0-274589a3dd54
    mariadb:
        image: mariadb:11.8
        volumes:
            - {NEW_VOL}:/var/lib/mysql

volumes:
    {NEW_VOL}:
        external: true
    erp_db:
    sites:
"""


def test_an_image_line_with_a_trailing_comment_is_still_checked(tmp_path):
    """`image: x  # pin` must not slip past the presence check (pr-review finding 4)."""
    _make_run(tmp_path, compose=_compose().replace("image: redis:6.2-alpine", "image: redis:6.2-alpine  # pinned"))
    proc, _, calls = _stub_run(tmp_path, present=(DIGEST_BEFORE, "mariadb:11.8"))
    assert proc.returncode == 4, proc.stderr
    assert "previous image redis:6.2-alpine" in proc.stderr
    assert "dc stop" not in calls


# ---- the site scheduler after the restore (RT-927, lab-03 2026-10-29) ----
# A forced restore into a new volume left System Settings.enable_scheduler=0 although the backup had 1:
# the workers ran, every gate passed, and no connector job ran for hours.


def _index(calls: list[str], needle: str) -> int:
    return next(i for i, call in enumerate(calls) if needle in call)


def test_enables_the_scheduler_when_the_backup_had_it_enabled(tmp_path):
    _make_run(tmp_path, scheduler="1")
    proc, _, calls = _stub_run(tmp_path)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    enable = _index(calls, "enable-scheduler")
    assert _index(calls, "--force restore") < enable
    assert enable < _index(calls, "scheduler status") < calls.index("dc up -d")
    assert f"bench --site {SITE} enable-scheduler" in calls[enable]


def test_leaves_the_scheduler_disabled_when_the_backup_had_it_disabled(tmp_path):
    _make_run(tmp_path, scheduler="0")
    proc, _, calls = _stub_run(tmp_path)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert not any("enable-scheduler" in call for call in calls)
    assert _index(calls, "scheduler status") < calls.index("dc up -d")


@pytest.mark.parametrize("dump", [
    _dump(None),
    _dump("2"),
    gzip.compress(b"INSERT INTO `tabSingles` VALUES ('System Settings','enable_scheduler','0'),"
                  b"('System Settings','enable_scheduler','1');\n", mtime=0),
    b"not a gzip file",
    # a partial or malformed row is not the complete tabSingles row and must not count
    gzip.compress(b"INSERT INTO `tabSingles` VALUES ('System Settings','enable_scheduler','1'garbage);\n", mtime=0),
    gzip.compress(b"-- 'System Settings','enable_scheduler','1'\n", mtime=0),
], ids=["missing", "not-0-or-1", "two-values", "not-gzip", "malformed-row", "comment-not-row"])
def test_a_dump_without_one_clear_scheduler_value_exits_4_before_anything_stops(tmp_path, dump):
    _make_run(tmp_path, dump=dump)
    dry = _dry(tmp_path)
    assert dry.returncode == 4, dry.stderr
    assert "enable_scheduler" in dry.stderr
    assert "PLAN" not in dry.stdout
    proc, stack, calls = _stub_run(tmp_path)
    assert proc.returncode == 4, proc.stderr
    assert "dc stop" not in calls
    assert (stack / "compose.yml").read_text(encoding="utf-8") == "# the swapped compose file\n"


def test_a_scheduler_status_that_differs_from_the_backup_exits_5_and_resumes_nothing(tmp_path):
    _make_run(tmp_path, scheduler="1")
    proc, stack, calls = _stub_run(tmp_path, sched_force="disabled")
    assert proc.returncode == 5, proc.stderr
    assert "scheduler" in proc.stderr
    assert "dc up -d" not in calls
    assert not (tmp_path / "host" / "last-resumed-at").exists(), "the restore window must stay open"
    assert (stack / "compose.yml").read_text(encoding="utf-8") == "# the swapped compose file\n"


def test_an_unreadable_scheduler_status_exits_5(tmp_path):
    _make_run(tmp_path, scheduler="0")
    proc, _, calls = _stub_run(tmp_path, sched_force="confused")
    assert proc.returncode == 5, proc.stderr
    assert "dc up -d" not in calls


def test_the_plan_names_the_scheduler_state_it_will_restore(tmp_path):
    _make_run(tmp_path, scheduler="1")
    proc = _dry(tmp_path)
    assert proc.returncode == 0, proc.stderr
    plan4 = next(line for line in proc.stdout.splitlines() if line.startswith("PLAN 4 "))
    assert "enable-scheduler" in plan4 and "scheduler status" in plan4
