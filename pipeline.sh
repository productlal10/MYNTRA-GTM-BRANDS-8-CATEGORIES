#!/bin/bash
# ================================================================
#  LAL10 Fashion Intelligence — Master Daily Pipeline
#  Scrapes all 9 categories one by one, then syncs each to EC2
#
#  Usage:
#    bash pipeline.sh              # scrape + sync all 9
#    bash pipeline.sh --only SPECIAL        # single category
#    bash pipeline.sh --skip SPECIAL        # skip one category
#    bash pipeline.sh --dry-run    # scrape only, no EC2 sync
#    bash pipeline.sh --sync-only  # skip scrape, only sync DBs
#    bash pipeline.sh --force-empty-sync  # allow syncing a 0-row export (normally refused)
# ================================================================

set -euo pipefail

# Keep the Mac awake for the whole run (idle sleep otherwise freezes ssh mid-transfer). The nightly
# launchd job already runs under caffeinate; this covers manual runs and the admin Sync button.
if [[ "$(uname)" == "Darwin" && -z "${PIPELINE_CAFFEINATED:-}" ]] && command -v caffeinate >/dev/null; then
  export PIPELINE_CAFFEINATED=1
  exec caffeinate -i bash "$0" "$@"
fi

# ─── CONFIG ─────────────────────────────────────────────────────
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
# Settings come from the environment first, then the git-ignored .env.
env_setting() { sed -n "s/^$1=//p" "$BASE_DIR/.env" 2>/dev/null | head -1 || true; }
EC2_IP="${EC2_HOST:-$(env_setting EC2_HOST)}"
EC2_USER="${EC2_USER:-$(env_setting EC2_USER)}"; EC2_USER="${EC2_USER:-ubuntu}"
EC2_KEY="${EC2_KEY:-$(env_setting EC2_KEY)}"
EC2="$EC2_USER@$EC2_IP"
REMOTE_BASE="${EC2_REMOTE_BASE:-$(env_setting EC2_REMOTE_BASE)}"; REMOTE_BASE="${REMOTE_BASE:-/var/www/myntra_gtm}"
PG_PASS="${PG_PASSWORD:-$(env_setting PG_PASSWORD)}"
PG_USER="${PG_USER:-$(env_setting PG_USER)}"; PG_USER="${PG_USER:-postgres}"
PG_HOST="${PG_HOST:-$(env_setting PG_HOST)}"; PG_HOST="${PG_HOST:-127.0.0.1}"
# EC2 Postgres password: EC2_PG_PASSWORD in .env, else the same as the local one.
EC2_PG_PASS="${EC2_PG_PASSWORD:-$(env_setting EC2_PG_PASSWORD)}"
EC2_PG_PASS="${EC2_PG_PASS:-$PG_PASS}"
# Uploads go to disk under REMOTE_BASE: EC2's /tmp is a 2 GB RAM disk, smaller than the larger exports.
REMOTE_SYNC_BASE="$REMOTE_BASE/.sync_tmp"
LOG_DIR="$BASE_DIR/.pipeline_logs"
LOG_FILE="$LOG_DIR/pipeline_$(date +%Y%m%d_%H%M%S).log"
PYTHON="python3"

# ─── CATEGORY DEFINITIONS: "FOLDER|EC2_FOLDER|DB_NAME|PORT|LABEL" ─
# Defined once in categories.json (see categories.py); EC2 uses the same folder names.
CATEGORIES=()
while IFS= read -r line; do CATEGORIES+=("$line"); done < <("$PYTHON" "$BASE_DIR/categories.py" '{folder}|{folder}|{db}|{port}|{name}')
[[ ${#CATEGORIES[@]} -gt 0 ]] || { echo "No categories loaded from $BASE_DIR/categories.json" >&2; exit 1; }

# ─── PARSE ARGS ─────────────────────────────────────────────────
DRY_RUN=false
SYNC_ONLY=false
ONLY_CAT=""
SKIP_CAT=""
FORCE_EMPTY_SYNC=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)          DRY_RUN=true; shift ;;
    --sync-only)        SYNC_ONLY=true; shift ;;
    --only)             ONLY_CAT="$2"; shift 2 ;;
    --skip)             SKIP_CAT="$2"; shift 2 ;;
    --force-empty-sync) FORCE_EMPTY_SYNC=true; shift ;;
    *)                  shift ;;
  esac
done

# ─── SET DEFAULTS for unset vars ────────────────────────────────
ONLY_CAT="${ONLY_CAT:-}"
SKIP_CAT="${SKIP_CAT:-}"

# One run at a time: the nightly sync and the admin page's "Sync to EC2" must not overlap.
LOCK_DIR="$BASE_DIR/.pipeline.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  OTHER_PID="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [[ -n "$OTHER_PID" ]] && kill -0 "$OTHER_PID" 2>/dev/null; then
    echo "Another pipeline run (PID $OTHER_PID) is in progress — not starting a second one." >&2
    exit 1
  fi
  rm -rf "$LOCK_DIR"; mkdir "$LOCK_DIR"   # left behind by a run that crashed
fi
echo $$ > "$LOCK_DIR/pid"

if [[ "$DRY_RUN" == false ]]; then
  [[ -n "$EC2_IP" ]] || { echo "EC2_HOST is not set (add it to $BASE_DIR/.env)" >&2; exit 1; }
  [[ -f "$EC2_KEY" ]] || { echo "EC2 key not found: '$EC2_KEY' (set EC2_KEY in $BASE_DIR/.env)" >&2; exit 1; }
fi

# ─── COLORS ─────────────────────────────────────────────────────
GREEN='\033[0;32m'; CYAN='\033[0;36m'; RED='\033[0;31m'
YELLOW='\033[1;33m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'
TICK="${GREEN}✓${NC}"; CROSS="${RED}✗${NC}"; ARROW="${CYAN}→${NC}"

ok()    { echo -e "    ${TICK} $*" | tee -a "$LOG_FILE"; }
fail()  { echo -e "    ${CROSS} $*" | tee -a "$LOG_FILE"; }
info()  { echo -e "  ${ARROW} $*" | tee -a "$LOG_FILE"; }
warn()  { echo -e "  ${YELLOW}!${NC} $*" | tee -a "$LOG_FILE"; }
header(){ echo -e "\n${BOLD}$*${NC}" | tee -a "$LOG_FILE"; }

# ─── INIT ────────────────────────────────────────────────────────
mkdir -p "$LOG_DIR"
PIPELINE_START=$(date +%s)

echo -e "" | tee "$LOG_FILE"
echo -e "${CYAN}╔══════════════════════════════════════════════════════╗${NC}" | tee -a "$LOG_FILE"
echo -e "${CYAN}║  LAL10 Fashion Intelligence — Daily Pipeline         ║${NC}" | tee -a "$LOG_FILE"
echo -e "${CYAN}║  $(date '+%a %d %b %Y, %I:%M %p')                          ║${NC}" | tee -a "$LOG_FILE"
echo -e "${CYAN}╚══════════════════════════════════════════════════════╝${NC}" | tee -a "$LOG_FILE"
[[ "$DRY_RUN" == true ]] && echo -e "  ${YELLOW}DRY RUN — EC2 sync skipped${NC}" | tee -a "$LOG_FILE"
[[ "$SYNC_ONLY" == true ]] && echo -e "  ${YELLOW}SYNC ONLY — Scrape skipped${NC}" | tee -a "$LOG_FILE"
[[ -n "$ONLY_CAT" ]] && echo -e "  ${YELLOW}Only running: $ONLY_CAT${NC}" | tee -a "$LOG_FILE"
[[ -n "$SKIP_CAT" ]] && echo -e "  ${YELLOW}Skipping: $SKIP_CAT${NC}" | tee -a "$LOG_FILE"

# ─── TRACK RESULTS ───────────────────────────────────────────────
declare -a RESULTS=()
TOTAL_UNITS=0
TOTAL_REVENUE=0

# ─── PYTHON HELPER: Export DB tables ─────────────────────────────
export_db_to_sql() {
  local DB=$1
  local OUT_DIR=$2

  PGPASSWORD=$PG_PASS $PYTHON - << PYEOF
import psycopg2, os, sys, json, gzip

conn = psycopg2.connect(host='$PG_HOST', user='$PG_USER', password='$PG_PASS', dbname='$DB')
cur = conn.cursor()

def insertable_cols(table):
    """Every column Postgres lets us insert (generated columns are computed by Postgres)."""
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s AND is_generated = 'NEVER' ORDER BY ordinal_position", (table,))
    return [r[0] for r in cur.fetchall()]

def export_table(cur, table, cols, path, conflict="ON CONFLICT DO NOTHING", preamble=""):
    """Write gzip-compressed INSERTs, streaming rows so big tables never sit in memory at once."""
    cur.execute(f"SELECT column_name, data_type, udt_name FROM information_schema.columns WHERE table_schema='public' AND table_name='{table}'")
    col_meta = {r[0]: r[2] for r in cur.fetchall()}
    stream = conn.cursor(name=f"export_{table}")
    stream.itersize = 5000
    stream.execute(f"SELECT {','.join(cols)} FROM {table} ORDER BY 1,2")
    n = 0
    with gzip.open(path, 'wt', encoding='utf-8', compresslevel=4) as f:
        f.write(f"-- {table} export\n")
        f.write("SET standard_conforming_strings = on;\n")
        f.write(preamble)
        for row in stream:
            n += 1
            vals = []
            for col_name, v in zip(cols, row):
                udt = col_meta.get(col_name, '')
                if v is None:
                    vals.append("NULL")
                elif isinstance(v, bool):
                    vals.append("TRUE" if v else "FALSE")
                elif udt in ('jsonb', 'json') or isinstance(v, dict):
                    vals.append(cur.mogrify("%s", (json.dumps(v),)).decode('utf-8') + "::jsonb")
                elif udt.startswith('_') or isinstance(v, (list, tuple)):
                    elem = udt[1:] if udt.startswith('_') else 'text'
                    vals.append(cur.mogrify("%s", (list(v),)).decode('utf-8') + f"::{elem}[]")
                else:
                    vals.append(cur.mogrify("%s", (v,)).decode('utf-8'))
            f.write(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join(vals)}) {conflict};\n")
    stream.close()
    return n

os.makedirs('$OUT_DIR', exist_ok=True)

# Products must reach EC2 before the rows that reference them (product_sizes has a
# foreign key), so they are upserted rather than wiped and reloaded.
# Generated columns (seller_state, seller_city) are computed by Postgres and cannot be inserted.
product_cols = insertable_cols("products")
product_upsert = "ON CONFLICT (product_id) DO UPDATE SET " + ", ".join(
    f"{c} = EXCLUDED.{c}" for c in product_cols if c != "product_id")
n_products = export_table(cur, "products", product_cols, "$OUT_DIR/products.sql.gz", conflict=product_upsert)

n_sizes    = export_table(cur, "product_sizes",
    ["product_id","size","sku_id","available","inventory_count","raw_inventory_count","inventory_quality"],
    "$OUT_DIR/sizes.sql.gz")
n_snaps    = export_table(cur, "daily_inventory_snapshots",
    ["snapshot_date","product_id","brand","category","selling_price","mrp","discount_percentage","is_in_stock","total_stock"],
    "$OUT_DIR/snapshots.sql.gz")
n_analytics = export_table(cur, "daily_sales_analytics",
    ["analytics_date","product_id","brand","category","units_sold","revenue_generated","stock_added","price_delta","ros","stock_status"],
    "$OUT_DIR/analytics.sql.gz")
# Only SPECIAL keeps a product_change_history table; elsewhere it does not exist.
cur.execute("SELECT to_regclass('public.product_change_history') IS NOT NULL")
if cur.fetchone()[0]:
    n_changes = export_table(cur, "product_change_history",
        ["product_id","recorded_at","event_num","categories_changed","stock_old","stock_new","stock_delta",
         "color_name","size_changes","price_old","price_new","price_delta","mrp_old","mrp_new",
         "discount_amt_old","discount_amt_new","discount_pct_old","discount_pct_new",
         "rating_old","rating_new","reviews_old","reviews_new"],
        "$OUT_DIR/changes.sql.gz")
else:
    n_changes = -1

# Scraper run history (powers the dashboards' scraper monitor). Loaded on EC2 in its own
# transaction, so a schema difference there can never block the main data above.
ops_parts = []
for t in ("scraper_runs", "scraper_brand_progress", "scraper_events", "scraper_errors"):
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{t}",))
    if cur.fetchone()[0]:
        ops_parts.append(t)
for i, t in enumerate(ops_parts):
    export_table(cur, t, insertable_cols(t), f"$OUT_DIR/ops_{i}.sql.gz", conflict="", preamble=f"DELETE FROM {t};\n")

# RoS v2 calculation tables (ros_daily_clean, ros_pair_locks, ros_runs, ros_health)
ros_parts = []
for t in ("ros_daily_clean", "ros_pair_locks", "ros_runs", "ros_health"):
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{t}",))
    if cur.fetchone()[0]:
        ros_parts.append(t)
for i, t in enumerate(ros_parts):
    export_table(cur, t, insertable_cols(t), f"$OUT_DIR/ros_{i}.sql.gz", conflict="", preamble=f"DELETE FROM {t};\n")

# Get KPIs (row counts above are real even if this summary query fails)
try:
    cur.execute("SELECT COALESCE(SUM(units_sold),0), ROUND(COALESCE(SUM(revenue_generated),0)::numeric,2) FROM daily_sales_analytics")
    kpi = cur.fetchone()
    units, revenue = int(kpi[0]), float(kpi[1])
except Exception as e:
    print(f"KPI query failed: {e}", file=sys.stderr)
    conn.rollback()
    units, revenue = 0, 0.0
print(f"units={units},revenue={revenue},sizes={n_sizes},snaps={n_snaps},analytics={n_analytics},changes={n_changes}")
PYEOF
}

# Records a category's outcome and removes its export files; every path out of the loop body calls it.
finish_category() {
  CAT_END=$(date +%s)
  CAT_SECS=$((CAT_END - CAT_START))
  RESULTS+=("$LABEL|$CAT_STATUS|${UNITS}|₹${REVENUE}|${CAT_SECS}s")
  rm -rf "$TMP_OUT"
}

# Large exports must not be left behind in /tmp if the pipeline is stopped or crashes.
TMP_OUT=""
trap '[[ -n "$TMP_OUT" ]] && rm -rf "$TMP_OUT"; rm -rf "$LOCK_DIR"' EXIT

# rsync 3+ quotes remote paths itself (-s); macOS' rsync 2.6.9 needs spaces escaped by hand.
if rsync --version 2>/dev/null | head -1 | grep -qE 'version [3-9]'; then
  RSYNC_PROTECT="-s"; RSYNC_ESCAPE=false
else
  RSYNC_PROTECT=""; RSYNC_ESCAPE=true
fi

# ─── MAIN LOOP: Each category ────────────────────────────────────
CAT_NUM=0
CAT_TOTAL=${#CATEGORIES[@]}

for cat_def in "${CATEGORIES[@]}"; do
  IFS='|' read -r FOLDER EC2_FOLDER DB PORT LABEL <<< "$cat_def"
  CAT_NUM=$((CAT_NUM + 1))
  CAT_DIR="$BASE_DIR/$FOLDER"
  TMP_OUT="/tmp/lal10_sync_${DB}"

  # Apply filters
  [[ -n "$ONLY_CAT" && "$FOLDER" != "$ONLY_CAT" && "$LABEL" != "$ONLY_CAT" ]] && continue
  [[ -n "$SKIP_CAT" && ("$FOLDER" == "$SKIP_CAT" || "$LABEL" == "$SKIP_CAT") ]] && { warn "Skipping $LABEL"; continue; }

  echo "" | tee -a "$LOG_FILE"
  echo -e "${BOLD}${CYAN}[$CAT_NUM/$CAT_TOTAL] $LABEL${NC}  ${DIM}($FOLDER → $DB :$PORT)${NC}" | tee -a "$LOG_FILE"
  CAT_START=$(date +%s)
  CAT_STATUS="OK"
  UNITS=0; REVENUE=0.0

  # ── SCRAPE ──────────────────────────────────────────────────
  if [[ "$SYNC_ONLY" == false ]]; then
    info "Scraping..."
    if [[ ! -f "$CAT_DIR/main.py" ]]; then
      fail "No main.py found in $FOLDER — skipping scrape"
      CAT_STATUS="NO_SCRAPER"
    else
      SCRAPE_TMP=$(mktemp)
      # Run scraper without arbitrary time limit; tee live output to terminal + log + tmp file
      set +e
      (cd "$CAT_DIR" && $PYTHON main.py --allow-listing-fallback 2>&1) \
        | tee -a "$LOG_FILE" "$SCRAPE_TMP" \
        | sed 's/^/    /'
      SCRAPE_EXIT=${PIPESTATUS[0]}
      set -e
      SCRAPE_OUT=$(cat "$SCRAPE_TMP"); rm -f "$SCRAPE_TMP"

      if [[ $SCRAPE_EXIT -ne 0 ]]; then
        warn "Scraper exited with code $SCRAPE_EXIT — attempting snapshot fallback"
        (cd "$CAT_DIR" && $PYTHON -c "from database import Database; Database().take_daily_snapshot()" 2>>"$LOG_FILE" || true)
      fi

      set +e
      SCRAPED=$(echo "$SCRAPE_OUT" | grep -oE '"(successful_count|saved_products|products_saved)":\s*[0-9]+' | grep -oE '[0-9]+' | tail -1)
      FAILED=$(echo "$SCRAPE_OUT"  | grep -oE '"(failed_count|pdp_failed)":\s*[0-9]+'     | grep -oE '[0-9]+' | tail -1)
      set -e
      SCRAPED=${SCRAPED:-0}; FAILED=${FAILED:-0}
      if [[ $SCRAPED -gt 0 ]]; then
        ok "Scraped $SCRAPED products (${FAILED} failed)"
      else
        warn "Scraper returned 0 successful — Myntra may be blocking or data unchanged"
      fi
    fi
  fi

  # ── RUN ROS V2 ALGORITHM ────────────────────────────────────
  info "Running RoS v2 Cleaning Algorithm ($DB)..."
  $PYTHON "$BASE_DIR/ros_engine.py" --db "$DB" >>"$LOG_FILE" 2>&1 || warn "RoS v2 run failed for $DB"

  # ── EXPORT LOCAL DB ─────────────────────────────────────────
  info "Exporting local DB ($DB)..."
  mkdir -p "$TMP_OUT"
  KPI_LINE=$(export_db_to_sql "$DB" "$TMP_OUT" 2>>"$LOG_FILE") || { fail "DB export failed"; CAT_STATUS="EXPORT_FAIL"; finish_category; continue; }

  set +e
  UNITS=$(echo "$KPI_LINE" | grep -oE 'units=[0-9]+' | cut -d= -f2)
  REVENUE=$(echo "$KPI_LINE" | grep -oE 'revenue=[0-9.]+' | cut -d= -f2)
  SIZES=$(echo "$KPI_LINE" | grep -oE 'sizes=[0-9]+' | cut -d= -f2)
  SNAPS=$(echo "$KPI_LINE" | grep -oE 'snaps=[0-9]+' | cut -d= -f2)
  CHANGES=$(echo "$KPI_LINE" | grep -oE 'changes=-?[0-9]+' | cut -d= -f2)
  set -e
  UNITS=${UNITS:-0}
  REVENUE=${REVENUE:-0.0}
  SIZES=${SIZES:-0}
  SNAPS=${SNAPS:-0}
  CHANGES=${CHANGES:--1}

  ok "Exported: $SIZES sizes | $SNAPS snapshots | units_sold=$UNITS | revenue=₹$REVENUE"
  TOTAL_UNITS=$((TOTAL_UNITS + UNITS))
  TOTAL_REVENUE=$(echo "$TOTAL_REVENUE + $REVENUE" | bc 2>/dev/null || echo "$TOTAL_REVENUE")

  # ── SAFETY GUARD: refuse to wipe production with an empty export ────
  # A local export with 0 sizes AND 0 snapshots almost always means the
  # scraper didn't run cleanly or the local DB read failed, not that the
  # catalog is genuinely empty. Syncing this would DELETE the EC2 tables
  # and reload them with nothing. Require an explicit override to do that.
  if [[ "$DRY_RUN" == false && "$SIZES" -eq 0 && "$SNAPS" -eq 0 && "$FORCE_EMPTY_SYNC" == false ]]; then
    fail "Export produced 0 sizes and 0 snapshots — refusing to wipe EC2 '$DB' with empty data. Re-run with --force-empty-sync to override."
    CAT_STATUS="EMPTY_EXPORT_SKIPPED"
    finish_category
    continue
  fi

  # ── SYNC TO EC2 ─────────────────────────────────────────────
  if [[ "$DRY_RUN" == false ]]; then
    info "Syncing to EC2..."

    EC2_DB="$DB"
    EC2_PORT="$PORT"
    EC2_FOLDER_ESCAPED="$EC2_FOLDER"
    REMOTE_SYNC_DIR="$REMOTE_SYNC_BASE/$DB"
    # Keep-alive: a link that dies (e.g. the Mac sleeping) fails in ~3 min instead of hanging forever.
    SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 -i "$EC2_KEY")

    # Sync category codebase (web/, server.py, database.py, config, etc.).
    # .env stays out: EC2 keeps its own credentials and settings.
    RSYNC_DEST_DIR="$REMOTE_BASE/$EC2_FOLDER/"
    [[ "$RSYNC_ESCAPE" == true ]] && RSYNC_DEST_DIR="${RSYNC_DEST_DIR// /\\ }"
    rsync -azL -q $RSYNC_PROTECT -e "ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 -i $EC2_KEY" \
      --exclude '.git' --exclude '__pycache__' --exclude 'venv' --exclude 'logs' --exclude '*.log' --exclude '.env' --exclude 'tmp' \
      "$CAT_DIR/" "$EC2:$RSYNC_DEST_DIR" 2>>"$LOG_FILE" || warn "Code rsync failed — data sync continues (see log)"

    # Upload SQL files (compressed in transit)
    ssh "${SSH_OPTS[@]}" "$EC2" "mkdir -p '$REMOTE_SYNC_DIR'" 2>>"$LOG_FILE" \
      && scp "${SSH_OPTS[@]}" -C -q \
        "$TMP_OUT"/*.sql.gz \
        "$EC2:$REMOTE_SYNC_DIR/" 2>>"$LOG_FILE" \
      || { fail "Upload failed"; CAT_STATUS="SCP_FAIL"
           ssh "${SSH_OPTS[@]}" "$EC2" "rm -rf '$REMOTE_SYNC_DIR'" 2>>"$LOG_FILE" || true
           finish_category; continue; }

    # product_change_history only exists where the local DB has it (SPECIAL).
    if [[ $CHANGES -ge 0 ]]; then
      CHANGES_DELETE="DELETE FROM product_change_history; "
      CHANGES_LOAD="$REMOTE_SYNC_DIR/changes.sql.gz"
    else
      CHANGES_DELETE=""
      CHANGES_LOAD=""
    fi

    cat > "$TMP_OUT/remote.sh" << REMOTE_EOF
EC2_DB="$EC2_DB"
EC2_PORT="$EC2_PORT"
EC2_FOLDER="$EC2_FOLDER_ESCAPED"
CHANGES_DELETE="$CHANGES_DELETE"
CHANGES_LOAD="$CHANGES_LOAD"
PG_USER="$PG_USER"
EC2_PG_PASS="$EC2_PG_PASS"
SYNC_DIR="$REMOTE_SYNC_DIR"
REMOTE_BASE="$REMOTE_BASE"

set -o pipefail
mkdir -p "\$REMOTE_BASE/\$EC2_FOLDER/logs"
export PGPASSWORD=\$EC2_PG_PASS
# Decompress straight into psql: no uncompressed copy ever lands on EC2's disk.
gunzip -c "\$SYNC_DIR/products.sql.gz" "\$SYNC_DIR/sizes.sql.gz" "\$SYNC_DIR/snapshots.sql.gz" "\$SYNC_DIR/analytics.sql.gz" \$CHANGES_LOAD \
  | psql -h 127.0.0.1 -U \$PG_USER -d \$EC2_DB -q -v ON_ERROR_STOP=1 --single-transaction \
      -c "\${CHANGES_DELETE}DELETE FROM daily_sales_analytics; DELETE FROM daily_inventory_snapshots; DELETE FROM product_sizes;" \
      -f -
psql_status=\$?
if [ \$psql_status -eq 0 ] && ls "\$SYNC_DIR"/ops_*.sql.gz >/dev/null 2>&1; then
  gunzip -c "\$SYNC_DIR"/ops_*.sql.gz \
    | psql -h 127.0.0.1 -U \$PG_USER -d \$EC2_DB -q -v ON_ERROR_STOP=1 --single-transaction -f - \
    || echo "OPS_SYNC_FAILED"
fi
if [ \$psql_status -eq 0 ] && ls "\$SYNC_DIR"/ros_*.sql.gz >/dev/null 2>&1; then
  gunzip -c "\$SYNC_DIR"/ros_*.sql.gz \
    | psql -h 127.0.0.1 -U \$PG_USER -d \$EC2_DB -q -v ON_ERROR_STOP=1 --single-transaction -f - \
    || echo "ROS_SYNC_FAILED"
fi
rm -rf "\$SYNC_DIR"
[ \$psql_status -eq 0 ] || exit 1

# Cached API responses were computed from the old data; drop them before the restart.
rm -rf "\$REMOTE_BASE/\$EC2_FOLDER/tmp/api_response_cache"

pkill -f "gunicorn.*127.0.0.1:\$EC2_PORT" 2>/dev/null || true
sleep 1

nohup env PORT=\$EC2_PORT SKIP_PREWARM=1 "\$REMOTE_BASE/venv/bin/gunicorn" \
  --workers 1 --threads 4 --worker-class gthread \
  --bind 127.0.0.1:\$EC2_PORT --timeout 180 --keep-alive 5 \
  --worker-tmp-dir /dev/shm --log-level warning \
  --chdir "\$REMOTE_BASE/\$EC2_FOLDER" \
  server:app >> "\$REMOTE_BASE/\$EC2_FOLDER/logs/server.log" 2>&1 < /dev/null &

sleep 2
for i in 1 2 3 4 5; do
  code=\$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:\$EC2_PORT/api/health" 2>/dev/null || echo "000")
  if [ "\$code" = "200" ]; then
    echo "HEALTH_200"
    exit 0
  fi
  sleep 1
done
echo "HEALTH_FAILED_\$code"
exit 1
REMOTE_EOF

    # Inside "if" so a remote failure marks this category failed instead of tripping set -e.
    if REMOTE_OUT=$(ssh "${SSH_OPTS[@]}" "$EC2" 'bash -s' < "$TMP_OUT/remote.sh" 2>&1) \
       && [[ "$REMOTE_OUT" == *HEALTH_200* ]]; then
      echo "$REMOTE_OUT" >> "$LOG_FILE"
      ok "EC2 synced, :$EC2_PORT ✅"
      [[ "$REMOTE_OUT" == *OPS_SYNC_FAILED* ]] && warn "Scraper history tables did not load on EC2 (dashboard data is fine) — see log"
    else
      echo "$REMOTE_OUT" >> "$LOG_FILE"
      fail "EC2 apply/restart failed"
      CAT_STATUS="EC2_FAIL"
    fi
  fi

  finish_category
done

# ─── SUMMARY ─────────────────────────────────────────────────────
PIPELINE_END=$(date +%s)
PIPELINE_SECS=$((PIPELINE_END - PIPELINE_START))
PIPELINE_MIN=$((PIPELINE_SECS / 60))
PIPELINE_SEC=$((PIPELINE_SECS % 60))

echo "" | tee -a "$LOG_FILE"
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════╗${NC}" | tee -a "$LOG_FILE"
echo -e "${BOLD}${CYAN}║               PIPELINE SUMMARY                       ║${NC}" | tee -a "$LOG_FILE"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════╝${NC}" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"
printf "  %-20s %-10s %-10s %-16s %-8s\n" "CATEGORY" "STATUS" "UNITS" "REVENUE" "TIME" | tee -a "$LOG_FILE"
echo -e "  $(printf '%.0s─' {1..65})" | tee -a "$LOG_FILE"

ALL_OK=true
for result in "${RESULTS[@]:-}"; do
  [[ -z "$result" ]] && continue
  IFS='|' read -r lbl status units rev secs <<< "$result"
  if [[ "$status" == "OK" ]]; then
    STATUS_COL="${GREEN}OK${NC}"
  else
    STATUS_COL="${RED}$status${NC}"
    ALL_OK=false
  fi
  printf "  %-20s " "$lbl" | tee -a "$LOG_FILE"
  echo -e "${STATUS_COL}         $units       $rev          $secs" | tee -a "$LOG_FILE"
done

echo "" | tee -a "$LOG_FILE"
echo -e "  ${BOLD}TOTAL UNITS SOLD  : $TOTAL_UNITS${NC}" | tee -a "$LOG_FILE"
echo -e "  ${BOLD}TOTAL REVENUE     : ₹$TOTAL_REVENUE${NC}" | tee -a "$LOG_FILE"
echo -e "  ${DIM}Pipeline duration : ${PIPELINE_MIN}m ${PIPELINE_SEC}s${NC}" | tee -a "$LOG_FILE"
echo -e "  ${DIM}Log saved to      : $LOG_FILE${NC}" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

if [[ "$ALL_OK" == true ]]; then
  echo -e "${GREEN}  ✅  All categories scraped and synced successfully!${NC}" | tee -a "$LOG_FILE"
else
  echo -e "${YELLOW}  ⚠️  Some categories had issues. Check log: $LOG_FILE${NC}" | tee -a "$LOG_FILE"
fi
echo "" | tee -a "$LOG_FILE"
[[ "$ALL_OK" == true ]] || exit 1
