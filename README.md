# Polymarket Trading OS v0.2

Local-first event-driven Polymarket trading system with strict human approval and SQLite as the single business source of truth.

This repository is designed around three responsibilities:

- LLM/OpenClaw: proposes structured trades
- Python pipeline: scans markets, fetches context, applies risk, handles Telegram, executes orders
- Risk + approval gate: blocks anything unsafe or unapproved

## System Overview

End-to-end pipeline:

1. `poly-scanner` scans active near-expiry markets from Gamma and upserts `market_snapshots`.
2. `event-fetcher` enriches each market with CryptoPanic, Apify Twitter (soft-fail enabled), and Perplexity summary.
3. `proposal-generator` persists normalized proposals in SQLite.
4. `risk-engine` updates proposal state to `pending_approval` or `risk_blocked`.
5. `tg-approver send` pushes inline `Approve/Reject` buttons to Telegram.
6. `tg-approver serve` receives callback webhooks, records decisions, and auto-executes approved proposals.
7. `poly-executor` executes in `mock` or `real` mode, with slippage and session safety checks.
8. Execution and approval events are fully auditable in SQLite (+ JSON artifacts for debugging).

## Current v0.2 Behavior

- SQLite is the only state authority for proposal, approval, execution, and resolution status.
- Proposal outcomes support any non-empty outcome label (for example `Yes/No`, `Up/Down`).
- Risk gate includes configurable slippage limit (`max_slippage_bps`) and executable market check.
- Telegram approval callback is idempotent on `callback_query_id`.
- Approved callback can auto-trigger executor (`TG_AUTO_EXECUTE_ON_APPROVE=true`).
- Real executor prevents duplicate real execution for the same proposal when prior real status is `live/submitted/filled`.
- Real execution preflight includes:
  - API key visibility check
  - collateral balance parse/check
  - balance sanity limit (`SESSION_MAX_BALANCE_USDC`)
  - per-run spend limit (`SESSION_MAX_SPEND_USDC`)

## Repository Layout

```text
src/polymarket_mvp/
  common.py
  db.py
  db_init.py
  poly_scanner.py
  event_fetcher.py
  proposer.py
  risk_engine.py
  tg_approver.py
  poly_executor.py
  mock_executor.py
  resolution_backfill.py

skills/poly_scanner/run.py
skills/tg_approver/run.py
scripts/mock_proposal.py
scripts/mock_execute.py
schema.sql
workflows/openclaw-polymarket-mvp.yaml
```

## Installation

Python 3.11+ is recommended (real execution requires Python 3.10+).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

For real trading:

```bash
pip install -e .[real-exec]
```

## Environment Configuration

Copy `.env.example`:

```bash
cp .env.example .env
```

Key groups:

- Core
  - `POLYMARKET_MVP_STATE_DIR`
  - `POLYMARKET_MVP_DB_PATH`
- Telegram
  - `TG_BOT_TOKEN`
  - `TG_CHAT_ID`
  - `TG_WEBHOOK_SECRET`
  - `TG_AUTO_EXECUTE_ON_APPROVE` (`true/false`)
  - `TG_AUTO_EXECUTE_MODE` (`real` or `mock`)
- Context adapters
  - `CRYPTOPANIC_AUTH_TOKEN`
  - `APIFY_TOKEN` or `APIFY_API_KEY`
  - `PERPLEXITY_API_KEY`
  - `PERPLEXITY_MODEL` (default `sonar`)
- Risk controls
  - `POLY_RISK_MAX_ORDER_USDC`
  - `POLY_RISK_MIN_CONFIDENCE`
  - `POLY_RISK_MAX_SLIPPAGE_BPS`
  - `POLY_RISK_REQUIRE_EXECUTABLE_MARKET`
  - `POLYMARKET_AVAILABLE_BALANCE_U` (used by mock/risk checks)
- Real execution / CLOB
  - `POLY_CLOB_HOST`
  - `CHAIN_ID` (or `POLY_CLOB_CHAIN_ID`)
  - `SIGNATURE_TYPE` (or `POLY_CLOB_SIGNATURE_TYPE`)
  - `FUNDER` (or `POLY_CLOB_FUNDER`)
  - `POLY_API_KEY`
  - `POLY_API_SECRET`
  - `POLY_API_PASSPHRASE`
  - `POLY_CLOB_SIGNER_KEY`
  - `SESSION_MAX_BALANCE_USDC`
  - `SESSION_MAX_SPEND_USDC`

## Command Entry Points

Console scripts (after `pip install -e .`):

```bash
db-init
poly-scanner
event-fetcher
proposal-generator
risk-engine
tg-approver
poly-executor
poly-mock-executor
backfill-resolutions
```

Equivalent module form:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.<module>
```

Skill wrappers:

```bash
python skills/poly_scanner/run.py ...
python skills/tg_approver/run.py ...
```

## Proposal Schema

Normalized proposal format:

```json
{
  "market_id": "1657334",
  "outcome": "Up",
  "confidence_score": 0.51,
  "recommended_size_usdc": 5.0,
  "reasoning": "Trade thesis",
  "max_slippage_bps": 700
}
```

Notes:

- `outcome` is a non-empty string and must match an outcome in market snapshot (`Yes/No`, `Up/Down`, etc.).
- If a proposal omits `max_slippage_bps`, default normalization can fill `500`.

## Quick Start (End-to-End)

### 1) Initialize database

```bash
PYTHONPATH=src python3 -m polymarket_mvp.db_init
```

### 2) Scan markets

```bash
PYTHONPATH=src python3 -m polymarket_mvp.poly_scanner \
  --min-liquidity 10000 \
  --max-expiry-days 7 \
  --output artifacts/markets.json
```

### 3) Fetch event context

```bash
PYTHONPATH=src python3 -m polymarket_mvp.event_fetcher \
  --market-file artifacts/markets.json \
  --output artifacts/contexts.json
```

### 4) Generate proposals

Heuristic path:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.proposer \
  --market-file artifacts/markets.json \
  --context-file artifacts/contexts.json \
  --engine heuristic \
  --size-usdc 10 \
  --top 3 \
  --max-slippage-bps 500 \
  --output artifacts/proposals.json
```

LLM/OpenClaw path:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.proposer \
  --market-file artifacts/markets.json \
  --context-file artifacts/contexts.json \
  --engine openclaw_llm \
  --proposal-file artifacts/openclaw-proposals.json \
  --output artifacts/proposals.json
```

### 5) Apply risk gate

```bash
PYTHONPATH=src python3 -m polymarket_mvp.risk_engine \
  --proposal-file artifacts/proposals.json \
  --output artifacts/risk.json
```

### 6) Run Telegram approval server

Keep this process running:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.tg_approver serve --port 8787
```

### 7) Expose webhook publicly (ngrok)

```bash
ngrok http 8787
```

Register webhook:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.tg_approver set-webhook \
  --webhook-url https://<your-ngrok-domain>
```

Inspect webhook state:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.tg_approver webhook-info
```

### 8) Send proposals to Telegram

```bash
PYTHONPATH=src python3 -m polymarket_mvp.tg_approver send \
  --proposal-file artifacts/proposals.json \
  --output artifacts/approval-request.json
```

### 9) Approve/Reject in Telegram

- `Reject`: proposal status becomes `rejected`.
- `Approve`: proposal status becomes `approved`, and if `TG_AUTO_EXECUTE_ON_APPROVE=true`, executor runs automatically.

### 10) Check status and execution

```bash
PYTHONPATH=src python3 -m polymarket_mvp.tg_approver status \
  --proposal-file artifacts/proposals.json \
  --output artifacts/approval-status.json
```

Manual executor fallback:

```bash
PYTHONPATH=src python3 -m polymarket_mvp.poly_executor \
  --proposal-file artifacts/proposals.json \
  --mode real \
  --output artifacts/execution.json
```

## Mock vs Real Execution

- `mock` mode:
  - no real order is sent
  - execution records are still persisted for replay/testing
- `real` mode:
  - py-clob-client submits real GTC orders
  - preflight and session limits are enforced
  - slippage gate can block before submit

## Data Model and Status Semantics

Main proposal statuses:

- `proposed`
- `risk_blocked`
- `pending_approval`
- `approved`
- `rejected`
- `executed`
- `failed`

Execution statuses (real path observed in DB):

- `live`: order accepted but not fully matched
- `filled`: matched/filled
- `failed`: blocked by risk/slippage/preflight/submit error

SQLite tables:

- `market_snapshots`
- `market_contexts`
- `proposals`
- `proposal_contexts`
- `approvals`
- `executions`
- `market_resolutions`

## Known Operational Notes

- `event_fetcher` Twitter adapter soft-fails when Apify plan/access blocks the actor; it inserts a fallback context instead of breaking the pipeline.
- Heuristic proposer still only generates proposals for explicit `Yes/No` binary markets. For other labels (`Up/Down`), use `openclaw_llm` proposal file path.
- Telegram callback retries are common; approval and auto-execution paths are idempotent by callback/order checks.
- Existing long-running `tg_approver serve` processes must be restarted after code updates.

## Troubleshooting

### Approve clicked but no trade execution

Check:

1. `tg_approver serve` process is running.
2. `webhook-info` URL points to a live tunnel and `/healthz` is reachable.
3. `TG_AUTO_EXECUTE_ON_APPROVE=true`.
4. Proposal is actually `approved` in SQLite.

### Order blocked by slippage

- Compare `observed_worst_price` vs `requested_price` and `max_slippage_bps`.
- For volatile micro-timeframe markets, either:
  - increase proposal `max_slippage_bps`, or
  - improve reference-price strategy in executor.

### Real execution fails with SDK/import issues

- Use Python 3.10+.
- Install optional deps:

```bash
pip install -e .[real-exec]
```

### VSCode/Pylance shows false import/type errors

- Ensure interpreter is `.venv/bin/python`.
- Workspace settings include `src` in analysis path.

## Security and Repo Hygiene

- `.env`, `artifacts/`, and `var/` are ignored by git.
- Never commit real API secrets or signer keys.
- Use low-balance execution wallets for live tests.
- Perplexity summaries are prioritized ahead of CryptoPanic and Twitter snippets
- `mock` execution is intended to stay as the regression-safe path even after `real` trading is enabled
