#!/bin/bash
# ==============================================================================
#  LAL10 Fashion Intelligence — Master Scraper Runner (All 10 Categories)
#
#  Usage:
#    bash run_all_scrapers.sh             # Run all 10 in parallel (default)
#    bash run_all_scrapers.sh --seq       # Run all 10 sequentially (one by one)
#    bash run_all_scrapers.sh --status    # Check live status & progress
#    bash run_all_scrapers.sh --stop      # Stop all currently running scrapers
#    bash run_all_scrapers.sh --only Maneet # Run only a specific category
# ==============================================================================

set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$BASE_DIR/.scraper_logs"
PIDS_FILE="$BASE_DIR/.scraper_pids"
LOCK_FILE="$BASE_DIR/.run_all_scrapers.lock"
PYTHON="${PYTHON:-python3}"

mkdir -p "$LOG_DIR"

# ─── CONCURRENCY LOCK ───────────────────────────────────────────────────────
# Guards against two invocations (parallel or sequential) racing the same
# Myntra session/cookie and the same DBs at once.
acquire_lock() {
  if [[ -f "$LOCK_FILE" ]]; then
    local lock_pid
    lock_pid="$(cat "$LOCK_FILE" 2>/dev/null || true)"
    if [[ -n "$lock_pid" ]] && kill -0 "$lock_pid" 2>/dev/null; then
      echo -e "\n${RED}A run_all_scrapers.sh run is already active (PID $lock_pid).${NC}"
      echo -e "Use '${CYAN}bash run_all_scrapers.sh --stop${NC}' first if you want to replace it.\n"
      exit 1
    fi
    echo -e "${YELLOW}Found a stale lock file (PID $lock_pid not running) — removing it.${NC}"
    rm -f "$LOCK_FILE"
  fi
  echo "$$" > "$LOCK_FILE"
  trap 'rm -f "$LOCK_FILE"' EXIT
}

# ─── CATEGORY SCRAPERS: "FOLDER|DB_NAME|PORT|LABEL" ─────────────────────────
# Defined once in categories.json (see categories.py).
CATEGORIES=()
while IFS= read -r line; do CATEGORIES+=("$line"); done < <("$PYTHON" "$BASE_DIR/categories.py" '{folder}|{db}|{port}|{name}')
[[ ${#CATEGORIES[@]} -gt 0 ]] || { echo "No categories loaded from $BASE_DIR/categories.json" >&2; exit 1; }

# ─── COLORS ─────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; CYAN='\033[0;36m'; RED='\033[0;31m'
YELLOW='\033[1;33m'; MAGENTA='\033[0;35m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'
TICK="${GREEN}✔${NC}"; CROSS="${RED}✗${NC}"; ARROW="${CYAN}➜${NC}"

# ─── HELPERS ────────────────────────────────────────────────────────────────
# Scrapers are launched as a bare "main.py" from inside their folder, so match on
# each main.py process's working directory, not its command line.
category_running() {
  local folder="$1" spid
  for spid in $(pgrep -f "main.py" 2>/dev/null); do
    if [[ "$(lsof -a -p "$spid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')" == "$BASE_DIR/$folder" ]]; then
      return 0
    fi
  done
  return 1
}

# macOS ships bash 3.2, which has no ${var,,}.
lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# ─── STOP ACTION ────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--stop" ]]; then
  echo -e "\n${BOLD}${YELLOW}Stopping all running scrapers…${NC}"

  # Sequential runs never populate $PIDS_FILE (only parallel mode does), so
  # kill the whole process group of the locked run — this covers both modes
  # without a blanket pkill hitting unrelated main.py processes.
  if [[ -f "$LOCK_FILE" ]]; then
    lock_pid="$(cat "$LOCK_FILE" 2>/dev/null || true)"
    if [[ -n "$lock_pid" ]] && kill -0 "$lock_pid" 2>/dev/null; then
      pgid="$(ps -o pgid= -p "$lock_pid" 2>/dev/null | tr -d ' ')"
      if [[ -n "$pgid" ]]; then
        kill -TERM "-$pgid" 2>/dev/null && echo -e "  ${TICK} Stopped run group (PGID $pgid, launcher PID $lock_pid)" || true
      fi
    fi
    rm -f "$LOCK_FILE"
  fi

  if [[ -f "$PIDS_FILE" ]]; then
    while IFS= read -r line; do
      pid="${line%%|*}"
      if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null && echo -e "  ${TICK} Stopped scraper PID $pid" || true
      fi
    done < "$PIDS_FILE"
    rm -f "$PIDS_FILE"
  fi
  echo -e "${GREEN}All scrapers stopped.${NC}\n"
  exit 0
fi

# ─── STATUS ACTION ──────────────────────────────────────────────────────────
if [[ "${1:-}" == "--status" ]]; then
  echo -e "\n${BOLD}${CYAN}════════════════════════════════════════════════════════════════${NC}"
  echo -e "${BOLD}  📊  LAL10 Scraper Live Status (All ${#CATEGORIES[@]} Categories)${NC}"
  echo -e "${BOLD}${CYAN}════════════════════════════════════════════════════════════════${NC}\n"
  printf "  ${BOLD}%-24s %-8s %-12s %s${NC}\n" "Category" "Port" "Status" "Latest Log Output"
  printf "  %s\n" "────────────────────────────────────────────────────────────────"

  for cat in "${CATEGORIES[@]}"; do
    IFS='|' read -r folder db port label <<< "$cat"
    log_file="$LOG_DIR/${folder// /_}_scraper.log"
    
    is_running=false
    if category_running "$folder"; then
      is_running=true
    fi

    last_log=""
    if [[ -f "$log_file" ]]; then
      last_log=$(tail -n 1 "$log_file" 2>/dev/null | cut -c1-60 || "")
    fi

    if [[ "$is_running" == true ]]; then
      status="${YELLOW}RUNNING${NC}"
    else
      status="${GREEN}IDLE / DONE${NC}"
    fi

    printf "  %-24s :%-7s %-18b %s\n" "$label" "$port" "$status" "$last_log"
  done
  echo ""
  exit 0
fi

# ─── PARSE OPTIONS ──────────────────────────────────────────────────────────
MODE="parallel"
ONLY_CAT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --seq|--sequential) MODE="sequential"; shift ;;
    --parallel)         MODE="parallel"; shift ;;
    --only)             ONLY_CAT="$2"; shift 2 ;;
    *)                  shift ;;
  esac
done

acquire_lock

echo ""
echo -e "${BOLD}${MAGENTA}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${MAGENTA}║  🚀  LAL10 Fashion Intelligence — Master Scraper Launcher     ║${NC}"
printf "${BOLD}${MAGENTA}║  Mode: %-10s | Total Categories: %-3s                       ║${NC}\n" "$(echo "$MODE" | tr '[:lower:]' '[:upper:]')" "${#CATEGORIES[@]}"
echo -e "${BOLD}${MAGENTA}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""

touch "$PIDS_FILE"

# ─── EXECUTION ──────────────────────────────────────────────────────────────
RUN_COUNT=0
LAUNCHED=()

for cat in "${CATEGORIES[@]}"; do
  IFS='|' read -r folder db port label <<< "$cat"

  if [[ -n "$ONLY_CAT" ]] && [[ "$(lower "$label")" != *"$(lower "$ONLY_CAT")"* ]] && [[ "$(lower "$folder")" != *"$(lower "$ONLY_CAT")"* ]]; then
    continue
  fi

  # Never start a second scraper on a database that is already being scraped
  # (e.g. a manual run still going when the daily cron fires).
  if category_running "$folder"; then
    echo -e "  ${YELLOW}⏭  $label is already being scraped — skipping.${NC}"
    continue
  fi

  cat_dir="$BASE_DIR/$folder"
  if [[ ! -f "$cat_dir/main.py" ]]; then
    echo -e "  ${CROSS} ${RED}main.py not found in $folder${NC}"
    continue
  fi

  log_file="$LOG_DIR/${folder// /_}_scraper.log"
  RUN_COUNT=$((RUN_COUNT + 1))
  LAUNCHED+=("$folder")

  if [[ "$MODE" == "parallel" ]]; then
    echo -e "  ${ARROW} Launching ${BOLD}$label${NC} scraper in background (Port $port)…"
    (
      cd "$cat_dir"
      "$PYTHON" main.py > "$log_file" 2>&1
    ) &
    pid=$!
    echo "$pid|$folder|$label" >> "$PIDS_FILE"
    echo -e "    ${TICK} PID: ${CYAN}$pid${NC} | Log: ${DIM}$log_file${NC}"
  else
    echo -e "\n${BOLD}${CYAN}▶ Running $label scraper ($folder)…${NC}"
    (
      cd "$cat_dir"
      "$PYTHON" main.py 2>&1 | tee "$log_file"
    )
    echo -e "  ${TICK} $label finished."
  fi
done

echo ""
if [[ "$MODE" == "parallel" ]]; then
  echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════════════════${NC}"
  echo -e "${BOLD}  ✔  All $RUN_COUNT scrapers launched in parallel!${NC}"
  echo -e "  • Check status anytime:  ${CYAN}bash run_all_scrapers.sh --status${NC}"
  echo -e "  • View logs directory:   ${CYAN}$LOG_DIR${NC}"
  echo -e "  • Stop all scrapers:     ${CYAN}bash run_all_scrapers.sh --stop${NC}"
  echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════════════════${NC}\n"

  # Unattended runs (cron) wait for every scraper so the lock is held until the
  # whole run finishes; otherwise the next run could overlap a slow category.
  if [[ ! -t 1 ]]; then
    wait
    echo "[$(date)] All scrapers finished."
  fi
fi

# ─── PUSH TO EC2 ────────────────────────────────────────────────────────────
# Scraping only fills the local databases; without this the nightly run never
# reached production. Unattended (scheduled) runs sync once every scraper has
# finished; set AUTO_SYNC_EC2=0 in .env (or the environment) to turn it off.
AUTO_SYNC_EC2="${AUTO_SYNC_EC2:-$(sed -n 's/^AUTO_SYNC_EC2=//p' "$BASE_DIR/.env" 2>/dev/null | head -1)}"
if [[ "${AUTO_SYNC_EC2:-1}" != "0" && ! -t 1 && "$RUN_COUNT" -gt 0 ]]; then
  echo "[$(date)] Syncing to EC2…"
  if [[ -z "$ONLY_CAT" ]]; then
    bash "$BASE_DIR/pipeline.sh" --sync-only || echo "[$(date)] EC2 sync reported failures — see $BASE_DIR/.pipeline_logs/"
  else
    # --only here matches loosely; pipeline.sh needs exact folder names, so sync what actually ran.
    for f in "${LAUNCHED[@]}"; do
      bash "$BASE_DIR/pipeline.sh" --sync-only --only "$f" || echo "[$(date)] EC2 sync of $f reported failures — see $BASE_DIR/.pipeline_logs/"
    done
  fi
  echo "[$(date)] EC2 sync step done."
fi
