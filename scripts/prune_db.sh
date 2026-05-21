#!/bin/bash
# Nightly retention: prune old rows from bloat-prone tables.
# Uses busy_timeout to handle any concurrent writers.
#
# History:
#   2026-05-21 alpha-lab-* timers removed; feature_snapshots retention (7d) added
#              as a safety net so a future writer can't blow up the DB 12× again.
set -euo pipefail
DB="${POLYMARKET_MVP_DB_PATH:-/home/ubuntu/polymarket-mvp/var/polymarket_mvp.sqlite3}"
LOG="${PRUNE_LOG:-/home/ubuntu/polymarket-mvp/var/prune.log}"
SQLITE=(sqlite3 -cmd '.timeout 60000' "$DB")
{
  echo "=== $(date -u +%FT%TZ) prune start ==="
  "${SQLITE[@]}" "DELETE FROM market_state_history WHERE observed_at < datetime('now', '-30 days');"
  "${SQLITE[@]}" "DELETE FROM polymarket_orderbook_snapshots WHERE captured_at < datetime('now', '-3 days');"
  "${SQLITE[@]}" "DELETE FROM feature_snapshots WHERE created_at < datetime('now', '-7 days');"
  "${SQLITE[@]}" "SELECT 'msh=' || COUNT(*) FROM market_state_history; SELECT 'ob=' || COUNT(*) FROM polymarket_orderbook_snapshots; SELECT 'fs=' || COUNT(*) FROM feature_snapshots;"
  "${SQLITE[@]}" 'PRAGMA wal_checkpoint(TRUNCATE);'
  echo "=== $(date -u +%FT%TZ) prune done ==="
} >> "$LOG" 2>&1
