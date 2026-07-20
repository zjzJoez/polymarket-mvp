-- 2026-07-20: smartmoney_state KV table for Strategy ζ (smart-money copy shadow).
-- Stores the tracked-wallet roster (refreshed daily from lb-api leaderboards)
-- and a per-wallet trade cursor so the collector never double-records a trade.
-- Shadow-only: no execution path reads this table.

PRAGMA busy_timeout = 60000;

BEGIN TRANSACTION;

CREATE TABLE IF NOT EXISTS smartmoney_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

COMMIT;
