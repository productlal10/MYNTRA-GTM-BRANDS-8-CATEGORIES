#!/bin/bash
# ================================================================
#  LAL10 Arvind New GenZ — Daily Scrape + Sync to EC2
#  Run: bash scrape_and_sync.sh [--sync-only] [--dry-run]
#  Thin wrapper: the scrape, export, upload and EC2 restart all live in
#  the shared ../pipeline.sh (EC2 settings come from ../.env).
# ================================================================
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec bash "$SCRIPT_DIR/../pipeline.sh" --only "$(basename "$SCRIPT_DIR")" "$@"
