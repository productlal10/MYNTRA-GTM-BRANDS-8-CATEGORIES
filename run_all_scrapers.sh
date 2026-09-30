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

# ─── 10 CATEGORY SCRAPERS: "FOLDER|DB_NAME|PORT|LABEL" ──────────────────────
declare -a CATEGORIES=(
  "Maneet|maneet_brands_shirts|3020|Maneet"
  "SPECIAL|ghanshaym_special|3019|Special Arrow X USpolo"
  "Shirts|gtm_shirts_myntra|3011|Shirts"
  "POLOS|polos_myntra_data|3009|Polo"
  "ACTIVEWEAR|activewear_myntra_data|3008|Activewear"
  "Kids|kids_gtm|3010|Kidswear"
  "Westerwear|westernwear_gtm|3012|Westernwear"
  "Hosiery|hosiery_gtm|3013|Innerwear"
  "Ocassionwear|ocassionwear_gtm|3014|Occasionwear"
  "WOMEN ETHNIC|women_ethnic_myntra_data|3015|Women Ethnic"
)

# ─── COLORS ─────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; CYAN='\033[0;36m'; RED='\033[0;31m'
YELLOW='\033[1;33m'; MAGENTA='\033[0;35m'; BOLD='\033[1m'; NC='\033[0m'
TICK="${GREEN}✔${NC}"; CROSS="${RED}✗${NC}"; ARROW="${CYAN}➜${NC}"

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
  echo -e "${BOLD}  📊  LAL10 Scraper Live Status (All 10 Categories)${NC}"
  echo -e "${BOLD}${CYAN}════════════════════════════════════════════════════════════════${NC}\n"
  printf "  ${BOLD}%-24s %-8s %-12s %s${NC}\n" "Category" "Port" "Status" "Latest Log Output"
  printf "  %s\n" "────────────────────────────────────────────────────────────────"

  for cat in "${CATEGORIES[@]}"; do
    IFS='|' read -r folder db port label <<< "$cat"
    log_file="$LOG_DIR/${folder// /_}_scraper.log"
    
    is_running=false
    if pgrep -f "$folder/main.py" >/dev/null 2>&1; then
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
echo -e "${BOLD}${MAGENTA}║  Mode: PARALLEL | Total Categories: 10                          ║${NC}"
echo -e "${BOLD}${MAGENTA}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""

touch "$PIDS_FILE"

# ─── EXECUTION ──────────────────────────────────────────────────────────────
RUN_COUNT=0

for cat in "${CATEGORIES[@]}"; do
  IFS='|' read -r folder db port label <<< "$cat"

  if [[ -n "$ONLY_CAT" ]] && [[ "${label,,}" != *"${ONLY_CAT,,}"* ]] && [[ "${folder,,}" != *"${ONLY_CAT,,}"* ]]; then
    continue
  fi

  cat_dir="$BASE_DIR/$folder"
  if [[ ! -f "$cat_dir/main.py" ]]; then
    echo -e "  ${CROSS} ${RED}main.py not found in $folder${NC}"
    continue
  fi

  log_file="$LOG_DIR/${folder// /_}_scraper.log"
  RUN_COUNT=$((RUN_COUNT + 1))

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
fi
