#!/bin/bash
# ============================================================
#  LAL10 Fashion Intelligence — Production Manager
# ============================================================

BASE_DIR="/var/www/myntra_gtm"
PIDS_FILE="$BASE_DIR/.running_pids"
PYTHON="$BASE_DIR/venv/bin/python3"
GUNICORN="$BASE_DIR/venv/bin/gunicorn"
HUB_PORT=3000

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

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

stop_all() {
  echo -e "${YELLOW}Stopping all LAL10 services...${NC}"
  pkill -f 'hub_server.py' 2>/dev/null || true
  pkill -f 'gunicorn.*server:app' 2>/dev/null || true
  pkill -f 'python.*server.py' 2>/dev/null || true
  if [ -f "$PIDS_FILE" ]; then
    while IFS= read -r pid; do
      kill -9 "$pid" 2>/dev/null || true
    done < "$PIDS_FILE"
    rm -f "$PIDS_FILE"
  fi
  sleep 1
  echo -e "${GREEN}[OK] All services stopped.${NC}"
}

start_all() {
  stop_all

  echo -e "${BOLD}Starting LAL10 Services in Production Mode...${NC}"
  touch "$PIDS_FILE"

  # Start Hub Server
  echo -n "Starting Hub Server (port $HUB_PORT)... "
  cd "$BASE_DIR"
  SKIP_PREWARM=1 "$PYTHON" hub_server.py > "$BASE_DIR/.hub_server.log" 2>&1 &
  HUB_PID=$!
  echo "$HUB_PID" >> "$PIDS_FILE"
  sleep 1
  if kill -0 "$HUB_PID" 2>/dev/null; then
    echo -e "${GREEN}OK (PID: $HUB_PID)${NC}"
  else
    echo -e "${RED}FAILED${NC}"
  fi

  # Start Category Services with Gunicorn (low memory footprint)
  for svc in "${SERVICES[@]}"; do
    IFS='|' read -r name folder port <<< "$svc"
    SVC_DIR="$BASE_DIR/$folder"
    LOG="$SVC_DIR/logs/server.log"
    mkdir -p "$SVC_DIR/logs"

    echo -n "Starting $name (port $port)... "
    if [ ! -f "$SVC_DIR/server.py" ]; then
      echo -e "${RED}server.py not found in $folder${NC}"
      continue
    fi

    cd "$SVC_DIR"
    SKIP_PREWARM=1 PORT="$port" "$GUNICORN" \
      --workers 1 \
      --threads 4 \
      --worker-class gthread \
      --bind "127.0.0.1:$port" \
      --timeout 180 \
      --keep-alive 5 \
      --worker-tmp-dir /dev/shm \
      --log-level warning \
      server:app >> "$LOG" 2>&1 &

    SVC_PID=$!
    echo "$SVC_PID" >> "$PIDS_FILE"
    sleep 0.5

    if kill -0 "$SVC_PID" 2>/dev/null; then
      echo -e "${GREEN}OK (PID: $SVC_PID)${NC}"
    else
      echo -e "${RED}FAILED — check $LOG${NC}"
    fi
  done

  echo ""
  echo -e "${BOLD}Checking Port Statuses:${NC}"
  sleep 2
  for svc in "${SERVICES[@]}"; do
    IFS='|' read -r name folder port <<< "$svc"
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$port/api/health" 2>/dev/null || echo "000")
    if [ "$STATUS" == "200" ]; then
      echo -e "  $name (:$port): ${GREEN}HEALTHY (200)${NC}"
    else
      echo -e "  $name (:$port): ${RED}HTTP $STATUS${NC}"
    fi
  done

  HUB_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:3000/" 2>/dev/null || echo "000")
  echo -e "  Central Hub (:3000): ${GREEN}HTTP $HUB_STATUS${NC}"
}

status_all() {
  echo -e "${BOLD}=== System Status ===${NC}"
  free -m
  echo ""
  echo -e "${BOLD}=== Active Ports ===${NC}"
  ss -tlpn | grep -E ':300[0-9]|:301[0-9]|:80'
}

case "$1" in
  stop)
    stop_all
    ;;
  status)
    status_all
    ;;
  *)
    start_all
    ;;
esac
