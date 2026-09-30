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

# ─── CONFIG ─────────────────────────────────────────────────────
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
EC2_IP="13.203.86.141"
EC2_USER="ubuntu"
EC2_KEY="$HOME/Downloads/myntralal10.pem"
EC2="$EC2_USER@$EC2_IP"
REMOTE_BASE="/var/www/myntra_gtm"
PG_PASS="alan1234"
PG_USER="postgres"
PG_HOST="127.0.0.1"
LOG_DIR="$BASE_DIR/.pipeline_logs"
LOG_FILE="$LOG_DIR/pipeline_$(date +%Y%m%d_%H%M%S).log"
PYTHON="python3"

# ─── 9 CATEGORY DEFINITIONS: "FOLDER|EC2_FOLDER|DB_NAME|PORT|LABEL" ─
declare -a CATEGORIES=(
  "Maneet|Maneet|maneet_brands_shirts|3020|Maneet"
  "SPECIAL|SPECIAL|ghanshaym_special|3019|Special Arrow X USpolo"
  "Shirts|Shirts|gtm_shirts_myntra|3011|Shirts"
  "POLOS|POLOS|polos_myntra_data|3009|Polo"
  "ACTIVEWEAR|ACTIVEWEAR|activewear_myntra_data|3008|Activewear"
  "Kids|Kids|kids_gtm|3010|Kidswear"
  "Westerwear|Westerwear|westernwear_gtm|3012|Westernwear"
  "Hosiery|Hosiery|hosiery_gtm|3013|Innerwear"
  "Ocassionwear|Ocassionwear|ocassionwear_gtm|3014|Occasionwear"
  "WOMEN ETHNIC|WOMEN ETHNIC|women_ethnic_myntra_data|3015|Women Ethnic"
)

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
import psycopg2, os, sys

conn = psycopg2.connect(host='$PG_HOST', user='$PG_USER', password='$PG_PASS', dbname='$DB')
conn.autocommit = True
cur = conn.cursor()

# A query exception (bad connection, missing table, permission error) must NOT
# be treated the same as "table legitimately has 0 rows" — silently writing
# "-- empty" and returning 0 for both cases means a broken local read can wipe
# a healthy production table and reload it with nothing. Exceptions propagate
# and abort this whole export (non-zero exit), which the caller in pipeline.sh
# treats as EXPORT_FAIL and skips the EC2 sync for this category.
def export_table(cur, table, cols, path, conflict="ON CONFLICT DO NOTHING"):
    cur.execute(f"SELECT {','.join(cols)} FROM {table} ORDER BY 1,2")
    rows = cur.fetchall()
    with open(path, 'w') as f:
        f.write(f"-- {table} export: {len(rows)} rows\n")
        for row in rows:
            vals = []
            for v in row:
                if v is None: vals.append("NULL")
                elif isinstance(v, str): vals.append("'" + v.replace("'","''") + "'")
                elif isinstance(v, bool): vals.append("1" if v else "0")
                elif hasattr(v, 'isoformat'): vals.append("'" + str(v) + "'")
                else: vals.append(str(v))
            f.write(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join(vals)}) {conflict};\n")
    return len(rows)

os.makedirs('$OUT_DIR', exist_ok=True)

n_sizes    = export_table(cur, "product_sizes",
    ["product_id","size","sku_id","available","inventory_count","raw_inventory_count","inventory_quality"],
    "$OUT_DIR/sizes.sql")
n_snaps    = export_table(cur, "daily_inventory_snapshots",
    ["snapshot_date","product_id","brand","category","selling_price","mrp","discount_percentage","is_in_stock","total_stock"],
    "$OUT_DIR/snapshots.sql")
n_analytics = export_table(cur, "daily_sales_analytics",
    ["analytics_date","product_id","brand","category","units_sold","revenue_generated","stock_added","price_delta","ros","stock_status"],
    "$OUT_DIR/analytics.sql")
n_changes  = export_table(cur, "product_change_history",
    ["product_id","recorded_at","event_num","categories_changed","stock_old","stock_new","stock_delta",
     "color_name","size_changes","price_old","price_new","price_delta","mrp_old","mrp_new",
     "discount_amt_old","discount_amt_new","discount_pct_old","discount_pct_new",
     "rating_old","rating_new","reviews_old","reviews_new"],
    "$OUT_DIR/changes.sql")

# Get KPIs
try:
    cur.execute("SELECT COALESCE(SUM(units_sold),0), ROUND(COALESCE(SUM(revenue_generated),0)::numeric,2) FROM daily_sales_analytics")
    kpi = cur.fetchone()
    print(f"units={int(kpi[0])},revenue={float(kpi[1])},sizes={n_sizes},snaps={n_snaps},analytics={n_analytics},changes={n_changes}")
except:
    print("units=0,revenue=0.0,sizes=0,snaps=0,analytics=0,changes=0")
PYEOF
}

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

  # ── EXPORT LOCAL DB ─────────────────────────────────────────
  info "Exporting local DB ($DB)..."
  mkdir -p "$TMP_OUT"
  KPI_LINE=$(export_db_to_sql "$DB" "$TMP_OUT" 2>>"$LOG_FILE") || { fail "DB export failed"; CAT_STATUS="EXPORT_FAIL"; continue; }

  set +e
  UNITS=$(echo "$KPI_LINE" | grep -oE 'units=[0-9]+' | cut -d= -f2)
  REVENUE=$(echo "$KPI_LINE" | grep -oE 'revenue=[0-9.]+' | cut -d= -f2)
  SIZES=$(echo "$KPI_LINE" | grep -oE 'sizes=[0-9]+' | cut -d= -f2)
  SNAPS=$(echo "$KPI_LINE" | grep -oE 'snaps=[0-9]+' | cut -d= -f2)
  set -e
  UNITS=${UNITS:-0}
  REVENUE=${REVENUE:-0.0}
  SIZES=${SIZES:-0}
  SNAPS=${SNAPS:-0}

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
    continue
  fi

  # ── SYNC TO EC2 ─────────────────────────────────────────────
  if [[ "$DRY_RUN" == false ]]; then
    info "Syncing to EC2..."

    # Upload SQL files
    scp -o StrictHostKeyChecking=no -o ConnectTimeout=15 -q -i "$EC2_KEY" \
      "$TMP_OUT/sizes.sql" "$TMP_OUT/snapshots.sql" \
      "$TMP_OUT/analytics.sql" "$TMP_OUT/changes.sql" \
      "$EC2:/tmp/" 2>>"$LOG_FILE" || { fail "Upload failed"; CAT_STATUS="SCP_FAIL"; continue; }

    # Apply on EC2 + restart service
    EC2_DB="$DB"
    EC2_PORT="$PORT"
    EC2_FOLDER_ESCAPED="$EC2_FOLDER"

    ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 -i "$EC2_KEY" "$EC2" \
      "PGPASSWORD=$PG_PASS psql -h 127.0.0.1 -U $PG_USER -d $EC2_DB -q -c \
        'DELETE FROM product_change_history; DELETE FROM daily_sales_analytics; DELETE FROM daily_inventory_snapshots; DELETE FROM product_sizes;' \
       && PGPASSWORD=$PG_PASS psql -h 127.0.0.1 -U $PG_USER -d $EC2_DB -q -f /tmp/sizes.sql \
       && PGPASSWORD=$PG_PASS psql -h 127.0.0.1 -U $PG_USER -d $EC2_DB -q -f /tmp/snapshots.sql \
       && PGPASSWORD=$PG_PASS psql -h 127.0.0.1 -U $PG_USER -d $EC2_DB -q -f /tmp/analytics.sql \
       && PGPASSWORD=$PG_PASS psql -h 127.0.0.1 -U $PG_USER -d $EC2_DB -q -f /tmp/changes.sql \
       && pkill -f 'gunicorn.*$EC2_PORT' 2>/dev/null || true \
       && sleep 1 \
       && PORT=$EC2_PORT SKIP_PREWARM=1 /var/www/myntra_gtm/venv/bin/gunicorn \
            --workers 1 --threads 4 --worker-class gthread \
            --bind 127.0.0.1:$EC2_PORT --timeout 180 \
            --worker-tmp-dir /dev/shm --log-level warning \
            --chdir '/var/www/myntra_gtm/$EC2_FOLDER_ESCAPED' \
            server:app >> '/var/www/myntra_gtm/$EC2_FOLDER_ESCAPED/logs/server.log' 2>&1 & \
       && sleep 2 \
       && curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$EC2_PORT/api/health" \
      2>>"$LOG_FILE" | grep -E '^2[0-9][0-9]$' > /dev/null && ok "EC2 synced, :$EC2_PORT ✅" || { fail "EC2 apply/restart failed"; CAT_STATUS="EC2_FAIL"; }
  fi

  CAT_END=$(date +%s)
  CAT_SECS=$((CAT_END - CAT_START))
  RESULTS+=("$LABEL|$CAT_STATUS|${UNITS}|₹${REVENUE}|${CAT_SECS}s")

  # Cleanup temp files
  rm -rf "$TMP_OUT"
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
