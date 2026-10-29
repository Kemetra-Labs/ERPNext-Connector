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
# Exit codes: 0 done (or a clean dry run); 2 bad usage, no ledger, invalid ledger values;
#             3 refused because the scheduler already resumed (forward-fix only);
#             4 the backup is not restorable (checksums, missing encryption_key);
#             5 a restore step failed.
#
# ASSUMPTIONS about the lab-03 host, to be confirmed at the restore rehearsal (plan Task 7):
#   - the dc wrapper passes the rest of its arguments to `docker compose` (so `-f` works);
#   - the compose services are named mariadb and backend, and MariaDB keeps its data in /var/lib/mysql;
#   - the mariadb container has MYSQL_ROOT_PASSWORD, and bench restore takes --db-root-password.
set -Eeuo pipefail
export LC_ALL=C
umask 077

STACK_DIR=/opt/rt-erp
DC="$STACK_DIR/dc"
COMPOSE="$STACK_DIR/compose.yml"
BACKUPS_ROOT=/var/backups/rt-erp
BENCH_DIR=/home/frappe/frappe-bench
SITE_URL=http://127.0.0.1:18000
PYTHON="${RT_PYTHON:-python3}"
IMAGE_RE='^(rt-erpnext:16\.51\.0-[0-9a-f]{12}|ghcr\.io/kemetra-labs/rt-erpnext@sha256:[0-9a-f]{64})$'

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

NEW_VOL="rt-erp_mariadb-restore-$RUN_ID"
OVERRIDE="$RUN_DIR/restore-override.yml"

echo "restore of run $RUN_ID, site $SITE, previous image $DIGEST_BEFORE"
echo "PLAN 1 stop: stop every service of the project (the old database volume is left as it is)"
echo "PLAN 2 new-volume: create the volume $NEW_VOL and start MariaDB on it"
echo "PLAN 3 set-encryption-key: copy encryption_key from $CFG_FILE into the site config (the value is never printed)"
echo "PLAN 4 force-restore: bench --site $SITE --force restore $DB_FILE with the public and private files"
echo "PLAN 5 previous-image: put back the compose file saved before the swap (image $DIGEST_BEFORE) and start all services"
echo "PLAN 6 ping: wait for $SITE to answer /api/method/ping with 200"
[ "$DRY_RUN" = 0 ] || { echo "dry run: nothing was changed"; exit 0; }

dcx() { "$DC" -f "$COMPOSE" -f "$OVERRIDE" "$@"; }

# The same lock RT-ErpLabDeploy holds: never stop the stack under a deploy that swaps or migrates.
exec 9>/run/rt-erp-deploy.lock
flock -n 9 || die 5 "a deploy is running (/run/rt-erp-deploy.lock is held); wait for it to end"
# A deploy may have resumed between the first check and the lock: check again under the lock.
check_host_marker

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
dcx up -d --no-deps mariadb || die 5 "MariaDB did not start on the new volume"
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
RT_DB_ROOT_PW="$(docker exec "$MARIADB_CID" sh -c 'printf %s "$MYSQL_ROOT_PASSWORD"')"
export RT_DB_ROOT_PW
docker exec -e RT_DB_ROOT_PW "$BACKEND_CID" sh -c 'bench --site "$0" --force restore "$1" --with-public-files "$2" --with-private-files "$3" --db-root-password "$RT_DB_ROOT_PW"' \
  "$SITE" "$IN_DIR/$DB_FILE" "$IN_DIR/$PUB_FILE" "$IN_DIR/$PRIV_FILE" || die 5 "bench restore failed"
unset RT_DB_ROOT_PW

echo "STEP 5 previous-image"
cp "$RUN_DIR/compose.yml" "$COMPOSE"
# The workers start with the services, so posting resumes on the restored database: record it
# first (the deploy's rule), which also refuses any later restore of this or an earlier run.
mkdir -p "$(dirname "$MARKER")"
printf '%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$MARKER.tmp" \
  && mv -f "$MARKER.tmp" "$MARKER" \
  || die 5 "could not record the resume marker"
dcx up -d || die 5 "could not start the services on the previous image"

echo "STEP 6 ping"
waited=0
until [ "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 -H "Host: $SITE" "$SITE_URL/api/method/ping" || true)" = 200 ]; do
  [ "$waited" -lt 150 ] || die 5 "the site did not answer ping 200 after the restore"
  sleep 5
  waited=$((waited + 5))
done
echo "restore complete: $SITE answers ping."
echo "NOTE: MariaDB now runs on $NEW_VOL through $OVERRIDE. Fold that mount into $COMPOSE before the next deploy,"
echo "      or the next compose run goes back to the old volume. The old volume was not touched."
