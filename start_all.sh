#!/bin/bash
# ============================================================
#  LAL10 Fashion Intelligence — Start All Services
#  Run: bash start_all.sh
#  Stops: bash start_all.sh stop
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PIDS_FILE="$SCRIPT_DIR/.running_pids"

if [ -x "$SCRIPT_DIR/venv/bin/python3" ]; then
  PYTHON="$SCRIPT_DIR/venv/bin/python3"
else
  PYTHON="python3"
fi

# ─── SERVICE DEFINITIONS ─── (name, folder, port)
declare -a SERVICES=(
  "Activewear|ACTIVEWEAR|3008"
  "Polo|POLOS|3009"
  "Kids|Kids|3010"
  "Shirts|Shirts|3011"
  "Westernwear|Westerwear|3012"
  "Innerwear|Hosiery|3013"
  "Occasionwear|Ocassionwear|3014"
  "Women Ethnic|WOMEN ETHNIC|3015"
  "Special Arrow X USpolo|SPECIAL|3019"
  "Maneet|Maneet|3020"
)

HUB_PORT=3000

# ─── COLORS ───
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; MAGENTA='\033[0;35m'; CYAN='\033[0;36m'
BOLD='\033[1m'; NC='\033[0m'

log()  { echo -e "${CYAN}[HUB]${NC} $*"; }
ok()   { echo -e "${GREEN}[OK]${NC}  $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERR]${NC} $*"; }
head() { echo -e "\n${BOLD}${MAGENTA}$*${NC}"; }

# ─── STOP ALL ───
stop_all() {
  head "Stopping all LAL10 services…"
  if [ -f "$PIDS_FILE" ]; then
    while IFS= read -r pid; do
      if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null && ok "Stopped PID $pid" || warn "Could not stop PID $pid"
      fi
    done < "$PIDS_FILE"
    rm -f "$PIDS_FILE"
  fi
  # Also kill by pattern
  pkill -f "hub_server.py" 2>/dev/null || true
  pkill -f "server.py" 2>/dev/null || true
  for svc in "${SERVICES[@]}"; do
    IFS='|' read -r name folder port <<< "$svc"
    if [ -n "$port" ]; then
      lsof -ti :"$port" | xargs kill -9 2>/dev/null || true
    fi
  done
  ok "All services stopped."
}

# ─── CHECK PORT ───
is_port_free() {
  ! lsof -i TCP:"$1" -sTCP:LISTEN -t &>/dev/null
}

port_in_use_pid() {
  lsof -i TCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -1
}

# ─── MAIN ───
if [ "$1" = "stop" ]; then
  stop_all
  exit 0
fi

echo ""
echo -e "${BOLD}${MAGENTA}════════════════════════════════════════════════════${NC}"
echo -e "${BOLD}  🏮  LAL10 Fashion Intelligence Platform${NC}"
echo -e "${BOLD}  Starting all 9 category services + Hub${NC}"
echo -e "${BOLD}${MAGENTA}════════════════════════════════════════════════════${NC}"
echo ""

> "$PIDS_FILE"  # Reset PID file

# ─── INIT POSTGRESQL DATABASES ───
head "Initializing PostgreSQL databases…"
PSQL_CMD=""
for cmd in psql /usr/local/bin/psql /opt/homebrew/bin/psql; do
  if command -v "$cmd" &>/dev/null; then
    PSQL_CMD="$cmd"
    break
  fi
done

if [ -f "$SCRIPT_DIR/setup_databases.py" ]; then
  PGPASSWORD="${PGPASSWORD:-alan1234}" "$PYTHON" "$SCRIPT_DIR/setup_databases.py" 2>/dev/null && ok "Databases verified & ready" || warn "Database check finished (verify PostgreSQL is running)"
else
  warn "setup_databases.py not found — skipping automated DB check."
fi

echo ""

# ─── START HUB ───
head "Starting Hub Server (port $HUB_PORT)…"
if ! is_port_free "$HUB_PORT"; then
  EXISTING_PID=$(port_in_use_pid "$HUB_PORT")
  warn "Port $HUB_PORT already in use (PID: $EXISTING_PID) — skipping Hub"
else
  LOG_FILE="$SCRIPT_DIR/.hub_server.log"
  cd "$SCRIPT_DIR" && \
    HUB_PORT=$HUB_PORT "$PYTHON" hub_server.py > "$LOG_FILE" 2>&1 &
  HUB_PID=$!
  echo "$HUB_PID" >> "$PIDS_FILE"
  sleep 0.8
  if kill -0 "$HUB_PID" 2>/dev/null; then
    ok "Hub started (PID: $HUB_PID) → http://localhost:$HUB_PORT"
  else
    err "Hub failed to start. Check $LOG_FILE"
  fi
fi

# ─── START CATEGORY SERVICES ───
head "Starting 9 category intelligence services…"
echo ""

for svc in "${SERVICES[@]}"; do
  IFS='|' read -r name folder port <<< "$svc"

  printf "  %-18s (port %-5s) " "$name" "$port"

  # Check if port is already in use
  if ! is_port_free "$port"; then
    EXISTING_PID=$(port_in_use_pid "$port")
    echo -e "${YELLOW}⚠ already running (PID: $EXISTING_PID)${NC}"
    continue
  fi

  SVC_DIR="$SCRIPT_DIR/$folder"
  if [ ! -f "$SVC_DIR/server.py" ]; then
    echo -e "${RED}✗ server.py not found in $folder${NC}"
    continue
  fi

  LOG="$SVC_DIR/logs/server.log"
  mkdir -p "$SVC_DIR/logs"

  cd "$SVC_DIR" && \
    PORT="$port" "$PYTHON" server.py > "$LOG" 2>&1 &
  SVC_PID=$!
  echo "$SVC_PID" >> "$PIDS_FILE"

  # Give it a moment to crash-check
  sleep 0.5
  if kill -0 "$SVC_PID" 2>/dev/null; then
    echo -e "${GREEN}✔ started (PID: $SVC_PID)${NC}"
  else
    echo -e "${RED}✗ failed — check $LOG${NC}"
  fi
done

echo ""
head "Service Summary"
echo ""
printf "  ${BOLD}%-22s %-8s %-12s${NC}\n" "Category" "Port" "Status"
printf "  %s\n" "───────────────────────────────────────────"
sleep 1.5  # Let services settle

for svc in "${SERVICES[@]}"; do
  IFS='|' read -r name folder port <<< "$svc"
  if is_port_free "$port"; then
    STATUS="${RED}OFFLINE${NC}"
  else
    STATUS="${GREEN}ONLINE${NC}"
  fi
  printf "  %-22s :${CYAN}%-8s${NC} %b\n" "$name" "$port" "$STATUS"
done

echo ""
echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════${NC}"
echo -e "${BOLD}  🏮  Central Hub:   http://localhost:${HUB_PORT}${NC}"
echo -e "${BOLD}  Stop all:        bash start_all.sh stop${NC}"
echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════${NC}"
echo ""
