"""Strategy ζ — smart-money copy shadow collector.

Watches the top wallets from Polymarket's public profit leaderboards and
records, for every fresh BUY they make, what a copy-trade would look like
for us *at the moment we detect it*: the executable ask price, depth at
ask, and the detection latency. Pure shadow — this module never places
orders and never imports the executor.

Pre-registered kill gates (decided before data collection, 2026-07-20):
  G1. Median detection latency must be < 5 minutes. If our polling loop
      systematically sees trades later than that, copy-trading is dead on
      latency alone regardless of signal quality.
  G2. Copy EV after spread must be > 0 on resolved markets: buying at OUR
      executable ask (not the trader's price) must beat holding cash.
If either gate fails at the Day-21 review, Strategy ζ is killed. No
parameter tweaking to resurrect it.

Data sources (all free, verified 2026-07-20):
  - https://lb-api.polymarket.com/profit?window={1d,7d,30d,all}   leaderboards
  - https://data-api.polymarket.com/trades?user=0x...             wallet fills
  - https://clob.polymarket.com/book?token_id=...                 orderbooks
  - https://gamma-api.polymarket.com/markets?condition_ids=0x...  market meta
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import requests

from polymarket_mvp.common import utc_now_iso
from polymarket_mvp.db import upsert_market_snapshot

SIGNAL_NAME = "smartmoney_copy"

LB_API = "https://lb-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# Roster construction: union of these leaderboard windows.
ROSTER_WINDOWS: Tuple[str, ...] = ("30d", "all")
ROSTER_PER_WINDOW = 25
ROSTER_TTL_SECONDS = 24 * 3600

# Collection filters. Deliberately loose — record broadly, cut at analysis.
MIN_NOTIONAL_USDC = 50.0        # ignore dust fills (MM noise like 0.01-share prints)
MAX_TRADE_AGE_SECONDS = 1800    # never backfill trades older than 30 min
MAX_SIGNALS_PER_WALLET_PER_RUN = 5

_REQUEST_TIMEOUT = 15
_USER_AGENT = "polymarket-mvp-smartmoney-shadow/1.0"


@dataclass
class CopySignal:
    market_id: str
    outcome: str
    recommendation: str          # 'bet' when an executable ask exists, else 'no_match'
    trader_price: float          # model_p — the smart wallet's fill price
    our_ask: Optional[float]     # market_p — best ask when we detected the trade
    edge: Optional[float]        # trader_price - our_ask (≤0 means we'd pay up)
    payload: Dict[str, Any] = field(default_factory=dict)


def _session_or_default(session: Optional[requests.Session]) -> requests.Session:
    if session is not None:
        return session
    s = requests.Session()
    s.headers.update({"User-Agent": _USER_AGENT})
    return s


# ---------------------------------------------------------------------------
# smartmoney_state KV helpers
# ---------------------------------------------------------------------------

def state_get(conn, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM smartmoney_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def state_set(conn, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO smartmoney_state (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, utc_now_iso()),
    )


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------

def fetch_leaderboard(session: requests.Session, window: str, limit: int) -> List[Dict[str, Any]]:
    r = session.get(
        f"{LB_API}/profit", params={"window": window, "limit": limit}, timeout=_REQUEST_TIMEOUT
    )
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def build_roster(session: requests.Session) -> List[Dict[str, Any]]:
    """Union of profit leaders across ROSTER_WINDOWS, deduped by wallet."""
    seen: Dict[str, Dict[str, Any]] = {}
    for window in ROSTER_WINDOWS:
        try:
            entries = fetch_leaderboard(session, window, ROSTER_PER_WINDOW)
        except Exception:
            continue
        for rank, entry in enumerate(entries, 1):
            wallet = (entry.get("proxyWallet") or "").lower()
            if not wallet:
                continue
            existing = seen.get(wallet)
            record = {
                "wallet": wallet,
                "name": entry.get("name") or entry.get("pseudonym") or "",
                "windows": {window: {"rank": rank, "amount": entry.get("amount")}},
            }
            if existing is None:
                seen[wallet] = record
            else:
                existing["windows"].update(record["windows"])
    return list(seen.values())


def load_or_refresh_roster(conn, session: requests.Session) -> List[Dict[str, Any]]:
    raw = state_get(conn, "roster")
    if raw:
        try:
            stored = json.loads(raw)
            age = time.time() - float(stored.get("fetched_at_epoch", 0))
            if age < ROSTER_TTL_SECONDS and stored.get("wallets"):
                return stored["wallets"]
        except Exception:
            pass
    wallets = build_roster(session)
    if wallets:
        state_set(conn, "roster", json.dumps({
            "fetched_at_epoch": time.time(),
            "fetched_at": utc_now_iso(),
            "wallets": wallets,
        }, ensure_ascii=False))
    return wallets


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------

def fetch_wallet_trades(session: requests.Session, wallet: str, limit: int = 25) -> List[Dict[str, Any]]:
    r = session.get(
        f"{DATA_API}/trades", params={"user": wallet, "limit": limit}, timeout=_REQUEST_TIMEOUT
    )
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def filter_new_trades(
    trades: Sequence[Mapping[str, Any]],
    cursor_ts: float,
    *,
    now_epoch: Optional[float] = None,
    min_notional: float = MIN_NOTIONAL_USDC,
    max_age_seconds: float = MAX_TRADE_AGE_SECONDS,
    max_per_wallet: int = MAX_SIGNALS_PER_WALLET_PER_RUN,
) -> List[Dict[str, Any]]:
    """BUY fills newer than the cursor, recent enough, and big enough to copy."""
    now = time.time() if now_epoch is None else now_epoch
    fresh: List[Dict[str, Any]] = []
    for t in trades:
        try:
            ts = float(t.get("timestamp") or 0)
            price = float(t.get("price") or 0)
            size = float(t.get("size") or 0)
        except (TypeError, ValueError):
            continue
        if t.get("side") != "BUY":
            continue
        if ts <= cursor_ts:
            continue
        if now - ts > max_age_seconds:
            continue
        if price * size < min_notional:
            continue
        if not t.get("asset") or not t.get("conditionId"):
            continue
        fresh.append(dict(t))
    fresh.sort(key=lambda t: float(t["timestamp"]))
    return fresh[:max_per_wallet]


# ---------------------------------------------------------------------------
# Orderbook + market metadata
# ---------------------------------------------------------------------------

def fetch_best_levels(session: requests.Session, token_id: str) -> Dict[str, Optional[float]]:
    """Best ask/bid + size at best ask for a CLOB token. None fields when empty."""
    out: Dict[str, Optional[float]] = {"ask": None, "bid": None, "ask_size": None, "bid_size": None}
    try:
        r = session.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=_REQUEST_TIMEOUT)
        if r.status_code != 200:
            return out
        book = r.json()
    except Exception:
        return out
    asks = book.get("asks") or []
    bids = book.get("bids") or []
    if asks:
        best_ask = min(float(a["price"]) for a in asks)
        out["ask"] = best_ask
        out["ask_size"] = sum(float(a["size"]) for a in asks if float(a["price"]) == best_ask)
    if bids:
        best_bid = max(float(b["price"]) for b in bids)
        out["bid"] = best_bid
        out["bid_size"] = sum(float(b["size"]) for b in bids if float(b["price"]) == best_bid)
    return out


def resolve_gamma_market(session: requests.Session, condition_id: str) -> Optional[Dict[str, Any]]:
    try:
        r = session.get(
            f"{GAMMA_API}/markets", params={"condition_ids": condition_id}, timeout=_REQUEST_TIMEOUT
        )
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None
    if isinstance(data, list) and data:
        return data[0]
    return None


def _snapshot_dict_from_gamma(gamma: Mapping[str, Any], trade: Mapping[str, Any]) -> Dict[str, Any]:
    outcomes = []
    try:
        names = gamma.get("outcomes")
        prices = gamma.get("outcomePrices")
        if isinstance(names, str):
            names = json.loads(names)
        if isinstance(prices, str):
            prices = json.loads(prices)
        if names:
            for i, name in enumerate(names):
                price = None
                if prices and i < len(prices):
                    try:
                        price = float(prices[i])
                    except (TypeError, ValueError):
                        price = None
                outcomes.append({"name": name, "price": price})
    except Exception:
        outcomes = []
    if not outcomes:
        outcomes = [{"name": trade.get("outcome") or "?", "price": trade.get("price")}]
    slug = gamma.get("slug") or trade.get("slug")
    return {
        "market_id": str(gamma.get("id")),
        "question": gamma.get("question") or trade.get("title"),
        "slug": slug,
        "market_url": f"https://polymarket.com/event/{trade.get('eventSlug') or slug}",
        "condition_id": gamma.get("conditionId") or trade.get("conditionId"),
        "active": bool(gamma.get("active", True)),
        "closed": bool(gamma.get("closed", False)),
        "accepting_orders": bool(gamma.get("acceptingOrders", True)),
        "end_date": gamma.get("endDate"),
        "liquidity_usdc": _float_or_none(gamma.get("liquidity")),
        "volume_24h_usdc": _float_or_none(gamma.get("volume24hr")),
        "outcomes": outcomes,
        "source": "smartmoney_shadow",
    }


def _snapshot_dict_from_trade_only(trade: Mapping[str, Any]) -> Dict[str, Any]:
    """Fallback when Gamma lookup fails: key the snapshot by conditionId."""
    return {
        "market_id": str(trade.get("conditionId")),
        "question": trade.get("title"),
        "slug": trade.get("slug"),
        "market_url": f"https://polymarket.com/event/{trade.get('eventSlug') or trade.get('slug')}",
        "condition_id": trade.get("conditionId"),
        "active": True,
        "closed": False,
        "accepting_orders": True,
        "outcomes": [{"name": trade.get("outcome") or "?", "price": trade.get("price")}],
        "source": "smartmoney_shadow",
    }


def _float_or_none(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Signal construction + persistence
# ---------------------------------------------------------------------------

def already_recorded(conn, tx_hash: str, asset: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM signal_events
        WHERE signal_name = ?
          AND json_extract(payload_json, '$.tx') = ?
          AND json_extract(payload_json, '$.token_id') = ?
        LIMIT 1
        """,
        (SIGNAL_NAME, tx_hash, asset),
    ).fetchone()
    return row is not None


def build_copy_signal(
    trade: Mapping[str, Any],
    levels: Mapping[str, Optional[float]],
    market_id: str,
    wallet_meta: Mapping[str, Any],
    *,
    now_epoch: Optional[float] = None,
) -> CopySignal:
    now = time.time() if now_epoch is None else now_epoch
    trade_ts = float(trade.get("timestamp") or 0)
    trader_price = float(trade.get("price") or 0)
    size = float(trade.get("size") or 0)
    ask = levels.get("ask")
    recommendation = "bet" if ask is not None else "no_match"
    edge = (trader_price - ask) if ask is not None else None
    payload = {
        "wallet": trade.get("proxyWallet"),
        "wallet_name": wallet_meta.get("name") or trade.get("name") or "",
        "wallet_windows": wallet_meta.get("windows") or {},
        "tx": trade.get("transactionHash"),
        "token_id": trade.get("asset"),
        "condition_id": trade.get("conditionId"),
        "slug": trade.get("slug"),
        "event_slug": trade.get("eventSlug"),
        "outcome_index": trade.get("outcomeIndex"),
        "trade_ts": trade_ts,
        "detect_ts": now,
        "latency_seconds": round(now - trade_ts, 1),
        "trade_price": trader_price,
        "trade_size": size,
        "notional_usdc": round(trader_price * size, 2),
        "ask": levels.get("ask"),
        "ask_size": levels.get("ask_size"),
        "bid": levels.get("bid"),
        "bid_size": levels.get("bid_size"),
    }
    return CopySignal(
        market_id=market_id,
        outcome=str(trade.get("outcome") or "?"),
        recommendation=recommendation,
        trader_price=trader_price,
        our_ask=ask,
        edge=edge,
        payload=payload,
    )


def persist_copy_signal(conn, signal: CopySignal) -> None:
    conn.execute(
        """
        INSERT INTO signal_events
        (signal_name, market_id, outcome, recommendation, model_p, market_p, edge,
         size_recommendation_usdc, payload_json, generated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
        """,
        (
            SIGNAL_NAME,
            signal.market_id,
            signal.outcome,
            signal.recommendation,
            signal.trader_price,
            signal.our_ask,
            signal.edge,
            json.dumps(signal.payload, ensure_ascii=False, default=str),
            utc_now_iso(),
        ),
    )


# ---------------------------------------------------------------------------
# One collection pass
# ---------------------------------------------------------------------------

def collect_once(
    conn,
    session: Optional[requests.Session] = None,
    *,
    now_epoch: Optional[float] = None,
) -> Dict[str, int]:
    """Poll every roster wallet once; record new copyable BUYs into signal_events.

    Returns counters for observability: wallets polled, trades seen, signals
    recorded, dedup skips, errors.
    """
    session = _session_or_default(session)
    now = time.time() if now_epoch is None else now_epoch
    stats = {"wallets": 0, "trades_seen": 0, "signals": 0, "dedup_skips": 0, "errors": 0}

    roster = load_or_refresh_roster(conn, session)
    for wallet_meta in roster:
        wallet = wallet_meta["wallet"]
        stats["wallets"] += 1
        cursor_key = f"cursor:{wallet}"
        try:
            cursor_ts = float(state_get(conn, cursor_key) or 0)
        except (TypeError, ValueError):
            cursor_ts = 0.0
        try:
            trades = fetch_wallet_trades(session, wallet)
        except Exception:
            stats["errors"] += 1
            continue
        stats["trades_seen"] += len(trades)
        fresh = filter_new_trades(trades, cursor_ts, now_epoch=now)
        max_ts = cursor_ts
        for trade in fresh:
            max_ts = max(max_ts, float(trade["timestamp"]))
            tx = str(trade.get("transactionHash") or "")
            asset = str(trade.get("asset") or "")
            if already_recorded(conn, tx, asset):
                stats["dedup_skips"] += 1
                continue
            gamma = resolve_gamma_market(session, str(trade["conditionId"]))
            snapshot = (
                _snapshot_dict_from_gamma(gamma, trade)
                if gamma else _snapshot_dict_from_trade_only(trade)
            )
            upsert_market_snapshot(conn, snapshot)
            levels = fetch_best_levels(session, asset)
            signal = build_copy_signal(
                trade, levels, snapshot["market_id"], wallet_meta, now_epoch=time.time()
            )
            persist_copy_signal(conn, signal)
            stats["signals"] += 1
        # Advance the cursor past everything we saw this pass, including
        # filtered-out trades, so dust prints don't get re-examined forever.
        newest_seen = max((float(t.get("timestamp") or 0) for t in trades), default=cursor_ts)
        advanced = max(max_ts, min(newest_seen, now))
        if advanced > cursor_ts:
            state_set(conn, cursor_key, str(advanced))
    conn.commit()
    return stats
