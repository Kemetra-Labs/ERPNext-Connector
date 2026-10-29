#!/usr/bin/env bash
# RT-927 (design RT-926 v2, section 5): the owner-run restore of the ERP lab database.
#
#   restore.sh <run-id> [--backups-root DIR] [--dry-run]
#
# Run as root on lab-03, with the deploy run's backup under <backups-root>/<run-id>/ (written by
# the RT-ErpLabDeploy document: ledger.json, SHA256SUMS, the four bench backup files, compose.yml).
#
# THE RULE: a restore is allowed only BEFORE the scheduler resumed. The deploy writes the resume
# timestamp to the ledger before it starts the workers, so a ledger that has one (even from a run
# that failed half way) makes this script refuse. After the resume the posted sales are marked
# posted in Backend-Core and are never offered again, so a restore would delete those invoices
# from ERPNext with no way back: the only way forward then is a fix-forward deploy.
#
# It never touches the old database volume and never downgrades a database in place: it restores
# into a NEW volume, keeps the site name, copies the backup's encryption_key into the site config
# first (otherwise every Password field becomes unreadable), restores, puts the previous image back
# and checks /api/method/ping.
#
# Before it stops anything it also checks that every image of the saved compose file is on the host
# (loading the run's saved image tars if one is missing), and that the saved compose file has exactly
# one /var/lib/mysql mount, which STEP 5 points at the new volume: compose.yml alone is then the truth.
#
# Exit codes: 0 done (or a clean dry run); 2 bad usage, no ledger, invalid ledger values;
#             3 refused because the scheduler already resumed (forward-fix only);
#             4 the backup is not restorable (checksums, missing encryption_key, a database dump
#               without one System Settings enable_scheduler value of 0 or 1, a previous image that is
#               neither on the host nor in a saved image tar);
#             5 a restore step failed (including a saved compose file whose mariadb mount cannot be
#               moved to the new volume, and a site scheduler that is not as the backup had it).
#
# ASSUMPTIONS about the lab-03 host, to be confirmed at the restore rehearsal (plan Task 7):
#   - the dc wrapper passes the rest of its arguments to `docker compose` (so `-f` works);
#   - the compose services are named mariadb and backend, and MariaDB keeps its data in /var/lib/mysql;
#   - the mariadb container has MARIADB_ROOT_PASSWORD (seen on lab-03) or the older MYSQL_ROOT_PASSWORD,
#     and bench restore takes --db-root-password.
set -Eeuo pipefail
export LC_ALL=C
umask 077

# RT_STACK_DIR and RT_DEPLOY_LOCK exist for the stub run in the tests only.
STACK_DIR="${RT_STACK_DIR:-/opt/rt-erp}"
DC="$STACK_DIR/dc"
COMPOSE="$STACK_DIR/compose.yml"
LOCK="${RT_DEPLOY_LOCK:-/run/rt-erp-deploy.lock}"
BACKUPS_ROOT=/var/backups/rt-erp
BENCH_DIR=/home/frappe/frappe-bench
SITE_URL=http://127.0.0.1:18000
PYTHON="${RT_PYTHON:-python3}"
IMAGE_RE='^(rt-erpnext:16\.51\.0-[0-9a-f]{12}|ghcr\.io/kemetra-labs/rt-erpnext@sha256:[0-9a-f]{64})$'
# mariadb and redis run their own images: they are checked for presence, held only to a safe charset.
OTHER_IMAGE_RE='^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$'

die() { local code="$1"; shift; echo "restore.sh: $*" >&2; exit "$code"; }

RUN_ID=""
DRY_RUN=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --backups-root) [ "$#" -ge 2 ] || die 2 "--backups-root needs a directory"; BACKUPS_ROOT="$2"; shift ;;
    -*) die 2 "unknown option $1" ;;
    *) [ -z "$RUN_ID" ] || die 2 "one run id only"; RUN_ID="$1" ;;
  esac
  shift
done
[[ "$RUN_ID" =~ ^[0-9]{1,20}$ ]] || die 2 "usage: restore.sh <run-id> [--backups-root DIR] [--dry-run] (run id: digits)"

RUN_DIR="$BACKUPS_ROOT/$RUN_ID"
LEDGER="$RUN_DIR/ledger.json"
[ -f "$LEDGER" ] || die 2 "no ledger at $LEDGER"

# resumedAt, site, digestBefore and the backup file names, one key=value per line. Values that a
# later step trusts are validated below; nothing here prints a secret.
LEDGER_FIELDS="$("$PYTHON" -c 'import json, sys
data = json.load(open(sys.argv[1]))
print("resumedAt=" + str(data.get("resumedAt") or ""))
print("startedAt=" + str(data.get("startedAt") or ""))
print("site=" + str(data.get("site") or ""))
print("digestBefore=" + str(data.get("digestBefore") or ""))
for name in (data.get("backup") or {}).get("files") or []:
    print("file=" + str(name))' "$LEDGER")" || die 2 "the ledger is not valid JSON"

LEDGER_FIELDS="${LEDGER_FIELDS//$'\r'/}"   # a Python on Windows ends lines with CR LF
RESUMED_AT=""; STARTED_AT=""; SITE=""; DIGEST_BEFORE=""; FILES=()
while IFS='=' read -r key value; do
  case "$key" in
    resumedAt) RESUMED_AT="$value" ;;
    startedAt) STARTED_AT="$value" ;;
    site) SITE="$value" ;;
    digestBefore) DIGEST_BEFORE="$value" ;;
    file) FILES+=("$value") ;;
  esac
done <<<"$LEDGER_FIELDS"

if [ -n "$RESUMED_AT" ]; then
  die 3 "restore refused for run $RUN_ID: the scheduler resumed at $RESUMED_AT; forward-fix only (a restore would delete invoices Backend-Core will never re-offer)"
fi
# Any later resume on this host (another run, or a recovery that restarted the workers) also
# closes this run's window: every resume writes the host-wide marker before the workers run. Its
# path is fixed for the host, never under --backups-root (RT_RESUME_MARKER overrides it for tests).
MARKER="${RT_RESUME_MARKER:-/var/backups/rt-erp/last-resumed-at}"
check_host_marker() {
  local last ts_re='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'
  [ -f "$MARKER" ] || return 0
  last="$(tr -d '[:space:]' < "$MARKER")"
  [[ "$last" =~ $ts_re ]] || die 3 "restore refused: $MARKER is unreadable; forward-fix only until the owner checks it"
  [[ "$STARTED_AT" =~ $ts_re ]] || die 3 "restore refused: run $RUN_ID has no start time to compare with the last resume ($last); forward-fix only"
  [[ "$last" < "$STARTED_AT" ]] || die 3 "restore refused for run $RUN_ID: posting resumed on this host at $last, after the run started ($STARTED_AT); forward-fix only"
}
check_host_marker
[[ "$SITE" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die 2 "the ledger has no valid site name"
[[ "$DIGEST_BEFORE" =~ $IMAGE_RE ]] || die 2 "the ledger digestBefore is not a known image reference"
[ "${#FILES[@]}" = 4 ] || die 2 "the ledger must list exactly four backup files"

DB_FILE=""; PUB_FILE=""; PRIV_FILE=""; CFG_FILE=""
for f in "${FILES[@]}"; do
  [[ "$f" =~ ^[0-9]{8}_[0-9]{6}-[A-Za-z0-9._-]+$ ]] || die 2 "bad backup file name in the ledger"
  [ -f "$RUN_DIR/$f" ] || die 2 "backup file missing: $f"
  case "$f" in
    *-private-files.tar) PRIV_FILE="$f" ;;
    *-files.tar) PUB_FILE="$f" ;;
    *-database.sql.gz) DB_FILE="$f" ;;
    *-site_config_backup.json) CFG_FILE="$f" ;;
  esac
done
[ -n "$DB_FILE" ] && [ -n "$PUB_FILE" ] && [ -n "$PRIV_FILE" ] && [ -n "$CFG_FILE" ] || die 2 "the ledger must name the database, files, private files and config backups"
(cd "$RUN_DIR" && sha256sum -c --quiet SHA256SUMS >/dev/null 2>&1) || die 4 "backup checksum mismatch (SHA256SUMS)"
grep -q '"encryption_key"' "$RUN_DIR/$CFG_FILE" || die 4 "the backup config has no encryption_key: a restore would leave every Password field unreadable"
# The backup's System Settings.enable_scheduler. A forced restore into a new volume leaves it 0 whatever
# the backup had (Frappe reads it from the old database, which is not there), so STEP 4 sets it back:
# on lab-03 (2026-10-29) the workers ran on a disabled scheduler and no connector job ran for hours.
SCHED_BACKUP="$("$PYTHON" -c 'import gzip, re, sys
row = re.compile(rb"\(\s*\x27System Settings\x27\s*,\s*\x27enable_scheduler\x27\s*,\s*\x27([^\x27]*)\x27\s*\)\s*[,;]")
found = set()
with gzip.open(sys.argv[1], "rb") as handle:
    for line in handle:
        found.update(row.findall(line))
for value in sorted(found):
    print(value.decode("ascii", "replace"))' "$RUN_DIR/$DB_FILE")" || die 4 "could not read enable_scheduler from $DB_FILE"
SCHED_BACKUP="${SCHED_BACKUP//$'\r'/}"
[[ "$SCHED_BACKUP" =~ ^[01]$ ]] || die 4 "$DB_FILE has no single System Settings enable_scheduler value of 0 or 1: the restore cannot tell whether the scheduler should run"
if [ "$SCHED_BACKUP" = 1 ]; then SCHED_WANT=enabled; else SCHED_WANT=disabled; fi

NEW_VOL="rt-erp_mariadb-restore-$RUN_ID"
OVERRIDE="$RUN_DIR/restore-override.yml"
FOLDED="$RUN_DIR/compose.restore.yml"

# Reads the saved compose file: `images` prints every image: value; `fold NEW_VOL [OUT]` moves the one
# <volume>:/var/lib/mysql mount to NEW_VOL and declares NEW_VOL external under the one top-level
# volumes: key (writing OUT when given). Exact matches only: anything else fails with the reason.
PY_COMPOSE='import re, sys
mode, src = sys.argv[1], sys.argv[2]
with open(src, encoding="utf-8", newline="") as handle:
    lines = handle.read().splitlines(keepends=True)
if mode == "images":
    for line in lines:
        found = re.match(r"\s*image:\s*(\S+)\s*(#.*)?$", line)
        if found:
            print(found.group(1).strip(chr(34) + chr(39)))
    sys.exit(0)
new_vol = sys.argv[3]
mount = re.compile(r"(\s*-\s*)[A-Za-z0-9][A-Za-z0-9_.-]*(:/var/lib/mysql(?::[a-z,]+)?\s*)$")
mounts = [i for i, line in enumerate(lines) if mount.match(line)]
tops = [i for i, line in enumerate(lines) if re.match(r"volumes:\s*$", line)]
if len(mounts) != 1:
    sys.exit("expected exactly one <volume>:/var/lib/mysql mount, found %d" % len(mounts))
if len(tops) != 1:
    sys.exit("expected exactly one top-level volumes: key, found %d" % len(tops))
found = mount.match(lines[mounts[0]])
lines[mounts[0]] = found.group(1) + new_vol + found.group(2)
nl = "\r\n" if lines[tops[0]].endswith("\r\n") else "\n"
# The new key takes the indent of the existing entries, or a nested one would swallow them.
indent = "  "
for line in lines[tops[0] + 1:]:
    if line.strip() and not line.lstrip().startswith("#"):
        if line[0] in " \t":
            indent = line[:len(line) - len(line.lstrip())]
        break
lines.insert(tops[0] + 1, indent + new_vol + ":" + nl + indent * 2 + "external: true" + nl)
if len(sys.argv) > 4:
    with open(sys.argv[4], "w", encoding="utf-8", newline="") as handle:
        handle.write("".join(lines))'
compose_py() { "$PYTHON" -c "$PY_COMPOSE" "$@"; }

# Every image STEP 5 starts again, validated before any docker command sees it.
IMAGES_OUT="$(compose_py images "$RUN_DIR/compose.yml")" || die 4 "could not read the images of the saved compose file"
IMAGES_OUT="${IMAGES_OUT//$'\r'/}"
PREV_IMAGES=()
while IFS= read -r ref; do
  [ -n "$ref" ] || continue
  if [[ "$ref" == *rt-erpnext* ]]; then
    [[ "$ref" =~ $IMAGE_RE ]] || die 4 "the saved compose file names an unknown ERP image: $ref"
  else
    [[ "$ref" =~ $OTHER_IMAGE_RE ]] || die 4 "the saved compose file names an image this script will not pass to docker"
  fi
  PREV_IMAGES+=("$ref")
done <<<"$IMAGES_OUT"
[ "${#PREV_IMAGES[@]}" -gt 0 ] || die 4 "the saved compose file names no image"
compose_py fold "$RUN_DIR/compose.yml" "$NEW_VOL" || die 5 "the saved compose file cannot be pointed at $NEW_VOL"

echo "restore of run $RUN_ID, site $SITE, previous image $DIGEST_BEFORE"
echo "PLAN 0 previous-image-present: every image of the saved compose file is on this host (loaded from the run's saved image tars if not); nothing is stopped before this passes"
echo "PLAN 1 stop: stop every service of the project (the old database volume is left as it is)"
echo "PLAN 2 new-volume: create the volume $NEW_VOL and start MariaDB on it"
echo "PLAN 3 set-encryption-key: copy encryption_key from $CFG_FILE into the site config (the value is never printed)"
echo "PLAN 4 force-restore: bench --site $SITE --force restore $DB_FILE with the public and private files, then the scheduler as the backup had it (enable_scheduler=$SCHED_BACKUP: enable-scheduler if 1) and scheduler status must say $SCHED_WANT"
echo "PLAN 5 previous-image: put back the compose file saved before the swap (image $DIGEST_BEFORE), point compose.yml at $NEW_VOL and start all services"
echo "PLAN 6 ping: wait for $SITE to answer /api/method/ping with 200"
[ "$DRY_RUN" = 0 ] || { echo "dry run: nothing was changed"; exit 0; }

dcx() { "$DC" -f "$COMPOSE" -f "$OVERRIDE" "$@"; }

# The same lock RT-ErpLabDeploy holds: never stop the stack under a deploy that swaps or migrates.
exec 9>"$LOCK"
flock -n 9 || die 5 "a deploy is running ($LOCK is held); wait for it to end"
# A deploy may have resumed between the first check and the lock: check again under the lock.
check_host_marker

# STEP 5 starts these images again: make sure they are here while nothing has been stopped yet.
echo "STEP 0 previous-image-present"
missing_images() {
  local ref
  for ref in "${PREV_IMAGES[@]}"; do
    docker image inspect "$ref" >/dev/null 2>&1 || printf '%s\n' "$ref"
  done
}
MISSING="$(missing_images)"
if [ -n "$MISSING" ]; then
  TARS=()
  for t in "$RUN_DIR"/current-image-*.tar; do
    if [ -f "$t" ]; then TARS+=("$t"); fi
  done
  [ "${#TARS[@]}" -gt 0 ] || die 4 "previous image $(head -n 1 <<<"$MISSING") is not on this host and the run saved no image tar"
  for t in "${TARS[@]}"; do
    docker load -i "$t" || die 4 "could not load the saved image $t"
  done
  MISSING="$(missing_images)"
  [ -z "$MISSING" ] || die 4 "previous image $(head -n 1 <<<"$MISSING") is still not on this host after loading the saved image tars"
fi
compose_py fold "$RUN_DIR/compose.yml" "$NEW_VOL" "$FOLDED" || die 5 "could not write $FOLDED"

echo "STEP 1 stop"
"$DC" stop || die 5 "could not stop the project"
[ -z "$("$DC" ps -q)" ] || die 5 "services are still running"

echo "STEP 2 new-volume"
if docker volume inspect "$NEW_VOL" >/dev/null 2>&1; then die 5 "the volume $NEW_VOL already exists; refusing to reuse it"; fi
docker volume create "$NEW_VOL" >/dev/null
cat > "$OVERRIDE" <<EOF
services:
  mariadb:
    volumes:
      - $NEW_VOL:/var/lib/mysql
volumes:
  $NEW_VOL:
    external: true
EOF
# A new volume makes MariaDB initialise before it listens: wait for its healthcheck, or bench
# restore fails with 2002 (lab-03 rehearsal).
dcx up -d --wait --wait-timeout 180 --no-deps mariadb || die 5 "MariaDB did not become healthy on the new volume"
dcx up -d --no-deps backend || die 5 "the backend did not start"
BACKEND_CID="$(dcx ps -q backend)"
MARIADB_CID="$(dcx ps -q mariadb)"
[ -n "$BACKEND_CID" ] && [ -n "$MARIADB_CID" ] || die 5 "backend or mariadb container not found"

echo "STEP 3 set-encryption-key"
"$PYTHON" -c 'import json, sys
sys.stdout.write(json.load(open(sys.argv[1]))["encryption_key"])' "$RUN_DIR/$CFG_FILE" \
  | docker exec -i "$BACKEND_CID" ./env/bin/python -c 'import json, sys
site = sys.argv[1]
path = "sites/" + site + "/site_config.json"
key = sys.stdin.read()
with open(path) as handle:
    config = json.load(handle)
config["encryption_key"] = key
with open(path, "w") as handle:
    json.dump(config, handle, indent=1)' "$SITE" || die 5 "could not write the encryption key"

echo "STEP 4 force-restore"
IN_DIR="/tmp/restore-$RUN_ID"
docker exec "$BACKEND_CID" mkdir -p "$IN_DIR"
for f in "$DB_FILE" "$PUB_FILE" "$PRIV_FILE"; do
  docker cp "$RUN_DIR/$f" "$BACKEND_CID:$IN_DIR/$f" || die 5 "could not copy $f into the backend"
done
RT_DB_ROOT_PW="$(docker exec "$MARIADB_CID" sh -c 'printf %s "${MARIADB_ROOT_PASSWORD:-$MYSQL_ROOT_PASSWORD}"')"
[ -n "$RT_DB_ROOT_PW" ] || die 5 "the mariadb container has no MARIADB_ROOT_PASSWORD or MYSQL_ROOT_PASSWORD"
export RT_DB_ROOT_PW
docker exec -e RT_DB_ROOT_PW "$BACKEND_CID" sh -c 'bench --site "$0" --force restore "$1" --with-public-files "$2" --with-private-files "$3" --db-root-password "$RT_DB_ROOT_PW"' \
  "$SITE" "$IN_DIR/$DB_FILE" "$IN_DIR/$PUB_FILE" "$IN_DIR/$PRIV_FILE" || die 5 "bench restore failed"
# bench restore creates the site's DB user at the backend's current IP, and STEP 5 recreates the
# containers with new ones (every request then fails with 1045): move the user to any host.
DB_USER="$(docker exec "$BACKEND_CID" ./env/bin/python -c 'import json, sys
print(json.load(open("sites/" + sys.argv[1] + "/site_config.json"))["db_user"])' "$SITE")" || die 5 "could not read the site db_user"
[[ "$DB_USER" =~ ^[A-Za-z0-9_]{1,64}$ ]] || die 5 "the site db_user is not a plain name"
docker exec -e RT_DB_ROOT_PW -e RT_DB_USER="$DB_USER" "$MARIADB_CID" sh -c '
q() { MYSQL_PWD="$RT_DB_ROOT_PW" mariadb -uroot -N -e "$1"; }
hosts="$(q "SELECT host FROM mysql.user WHERE user = \"$RT_DB_USER\"")" || exit 1
[ -n "$hosts" ] && [ "$(printf "%s\n" "$hosts" | wc -l)" = 1 ] || exit 1
[ "$hosts" = "%" ] || q "RENAME USER \`$RT_DB_USER\`@\`$hosts\` TO \`$RT_DB_USER\`@\`%\`"' \
  || die 5 "could not move the site DB user $DB_USER to host %"
unset RT_DB_ROOT_PW
# The forced restore left the scheduler disabled: put back what the backup had, and check it before
# STEP 5 starts the workers, or they run and no scheduled job ever does.
if [ "$SCHED_BACKUP" = 1 ]; then
  docker exec "$BACKEND_CID" bench --site "$SITE" enable-scheduler || die 5 "bench enable-scheduler failed"
fi
SCHED_STATUS="$(docker exec "$BACKEND_CID" bench --site "$SITE" scheduler status)" || die 5 "bench scheduler status failed"
SCHED_NOW="$(grep -oE 'Scheduler is (enabled|disabled)' <<<"$SCHED_STATUS" | sort -u)" || true
[ "$SCHED_NOW" = "Scheduler is $SCHED_WANT" ] \
  || die 5 "the site scheduler is not $SCHED_WANT after the restore (the backup had enable_scheduler=$SCHED_BACKUP). The database is restored and MariaDB and the backend run on $NEW_VOL through $OVERRIDE; the workers were not started, compose.yml was not replaced and the resume marker was not written (see the README before finishing by hand)"

echo "STEP 5 previous-image"
# The saved compose file with the mariadb mount moved to $NEW_VOL (folded and checked in STEP 0): from
# here compose.yml alone is the truth, so a later `dc up` or deploy keeps the restored database.
cp "$FOLDED" "$COMPOSE" || die 5 "could not put back the compose file"
# The workers start with the services, so posting resumes on the restored database: record it
# first (the deploy's rule), which also refuses any later restore of this or an earlier run.
mkdir -p "$(dirname "$MARKER")"
printf '%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$MARKER.tmp" \
  && mv -f "$MARKER.tmp" "$MARKER" \
  || die 5 "could not record the resume marker"
"$DC" up -d || die 5 "could not start the services on the previous image"

echo "STEP 6 ping"
waited=0
until [ "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 -H "Host: $SITE" "$SITE_URL/api/method/ping" || true)" = 200 ]; do
  [ "$waited" -lt 150 ] || die 5 "the site did not answer ping 200 after the restore"
  sleep 5
  waited=$((waited + 5))
done
echo "restore complete: $SITE answers ping; $COMPOSE mounts $NEW_VOL (the old volume was not touched)."
