"""Tests for Strategy ζ — smart-money copy shadow collector."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from polymarket_mvp.services.smartmoney_signal import (
    SIGNAL_NAME,
    already_recorded,
    build_copy_signal,
    build_roster,
    collect_once,
    fetch_best_levels,
    filter_new_trades,
    load_or_refresh_roster,
    persist_copy_signal,
    state_get,
    state_set,
)

NOW = 1_784_600_000.0


def _trade(**overrides):
    base = {
        "proxyWallet": "0xabc0000000000000000000000000000000000001",
        "side": "BUY",
        "asset": "1234567890",
        "conditionId": "0xcond1",
        "size": 200.0,
        "price": 0.55,
        "timestamp": NOW - 60,
        "title": "Will X happen?",
        "slug": "will-x-happen",
        "eventSlug": "will-x-happen",
        "outcome": "Yes",
        "outcomeIndex": 0,
        "name": "sharp_trader",
        "transactionHash": "0xtx1",
    }
    base.update(overrides)
    return base


class FilterNewTradesTests(unittest.TestCase):
    def test_accepts_fresh_buy_above_notional(self):
        fresh = filter_new_trades([_trade()], cursor_ts=0, now_epoch=NOW)
        self.assertEqual(len(fresh), 1)

    def test_rejects_sell_side(self):
        fresh = filter_new_trades([_trade(side="SELL")], cursor_ts=0, now_epoch=NOW)
        self.assertEqual(fresh, [])

    def test_rejects_dust_notional(self):
        # 0.01 shares at 0.56 — the swisstony MM dust pattern
        fresh = filter_new_trades(
            [_trade(size=0.01, price=0.56)], cursor_ts=0, now_epoch=NOW
        )
        self.assertEqual(fresh, [])

    def test_rejects_stale_trades(self):
        fresh = filter_new_trades(
            [_trade(timestamp=NOW - 3600)], cursor_ts=0, now_epoch=NOW
        )
        self.assertEqual(fresh, [])

    def test_respects_cursor(self):
        t = _trade(timestamp=NOW - 60)
        fresh = filter_new_trades([t], cursor_ts=NOW - 30, now_epoch=NOW)
        self.assertEqual(fresh, [])
        fresh = filter_new_trades([t], cursor_ts=NOW - 90, now_epoch=NOW)
        self.assertEqual(len(fresh), 1)

    def test_caps_per_wallet_and_sorts_oldest_first(self):
        trades = [
            _trade(timestamp=NOW - 10 * i, transactionHash=f"0xtx{i}")
            for i in range(1, 9)
        ]
        fresh = filter_new_trades(trades, cursor_ts=0, now_epoch=NOW)
        self.assertEqual(len(fresh), 5)
        ts = [t["timestamp"] for t in fresh]
        self.assertEqual(ts, sorted(ts))


class RosterTests(unittest.TestCase):
    def test_build_roster_dedupes_across_windows(self):
        session = Mock()
        lb_30d = [
            {"proxyWallet": "0xAAA", "name": "alice", "amount": 1000.0},
            {"proxyWallet": "0xBBB", "name": "bob", "amount": 500.0},
        ]
        lb_all = [
            {"proxyWallet": "0xAAA", "name": "alice", "amount": 90000.0},
            {"proxyWallet": "0xCCC", "name": "carol", "amount": 80000.0},
        ]
        session.get.side_effect = [
            Mock(status_code=200, raise_for_status=Mock(), json=lambda: lb_30d),
            Mock(status_code=200, raise_for_status=Mock(), json=lambda: lb_all),
        ]
        roster = build_roster(session)
        wallets = {r["wallet"] for r in roster}
        self.assertEqual(wallets, {"0xaaa", "0xbbb", "0xccc"})
        alice = next(r for r in roster if r["wallet"] == "0xaaa")
        self.assertIn("30d", alice["windows"])
        self.assertIn("all", alice["windows"])


class BestLevelsTests(unittest.TestCase):
    def test_extracts_best_ask_and_bid(self):
        session = Mock()
        session.get.return_value = Mock(status_code=200, json=lambda: {
            "asks": [{"price": "0.60", "size": "100"}, {"price": "0.58", "size": "40"}],
            "bids": [{"price": "0.52", "size": "80"}, {"price": "0.55", "size": "30"}],
        })
        levels = fetch_best_levels(session, "tok1")
        self.assertAlmostEqual(levels["ask"], 0.58)
        self.assertAlmostEqual(levels["ask_size"], 40.0)
        self.assertAlmostEqual(levels["bid"], 0.55)

    def test_empty_book_returns_nones(self):
        session = Mock()
        session.get.return_value = Mock(status_code=200, json=lambda: {"asks": [], "bids": []})
        levels = fetch_best_levels(session, "tok1")
        self.assertIsNone(levels["ask"])
        self.assertIsNone(levels["bid"])


class BuildSignalTests(unittest.TestCase):
    def test_bet_when_ask_exists(self):
        levels = {"ask": 0.57, "ask_size": 500.0, "bid": 0.54, "bid_size": 200.0}
        sig = build_copy_signal(_trade(), levels, "12345", {"name": "alice", "windows": {}}, now_epoch=NOW)
        self.assertEqual(sig.recommendation, "bet")
        self.assertAlmostEqual(sig.trader_price, 0.55)
        self.assertAlmostEqual(sig.our_ask, 0.57)
        self.assertAlmostEqual(sig.edge, -0.02)  # we pay 2c more than the smart wallet
        self.assertAlmostEqual(sig.payload["latency_seconds"], 60.0, delta=1.0)
        self.assertAlmostEqual(sig.payload["notional_usdc"], 110.0)

    def test_no_match_when_book_empty(self):
        levels = {"ask": None, "ask_size": None, "bid": None, "bid_size": None}
        sig = build_copy_signal(_trade(), levels, "12345", {}, now_epoch=NOW)
        self.assertEqual(sig.recommendation, "no_match")
        self.assertIsNone(sig.edge)


class DbRoundtripTests(unittest.TestCase):
    def setUp(self):
        from polymarket_mvp.db import init_db
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.db_path = tmp.name
        tmp.close()
        os.unlink(self.db_path)
        self._env = patch.dict(os.environ, {"POLYMARKET_MVP_DB_PATH": self.db_path})
        self._env.start()
        init_db(Path(self.db_path))

    def tearDown(self):
        self._env.stop()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def _conn(self):
        from polymarket_mvp.db import connect_db
        return connect_db(Path(self.db_path))

    def test_state_kv_roundtrip(self):
        with self._conn() as conn:
            self.assertIsNone(state_get(conn, "roster"))
            state_set(conn, "roster", "v1")
            state_set(conn, "roster", "v2")
            self.assertEqual(state_get(conn, "roster"), "v2")

    def test_persist_and_dedup(self):
        levels = {"ask": 0.57, "ask_size": 500.0, "bid": 0.54, "bid_size": 200.0}
        trade = _trade()
        with self._conn() as conn:
            from polymarket_mvp.db import upsert_market_snapshot
            upsert_market_snapshot(conn, {
                "market_id": "12345", "question": "Will X happen?",
                "active": True, "closed": False, "accepting_orders": True,
                "outcomes": [{"name": "Yes", "price": 0.55}],
            })
            sig = build_copy_signal(trade, levels, "12345", {}, now_epoch=NOW)
            persist_copy_signal(conn, sig)
            conn.commit()
            self.assertTrue(already_recorded(conn, "0xtx1", "1234567890"))
            self.assertFalse(already_recorded(conn, "0xtx_other", "1234567890"))
            row = conn.execute(
                "SELECT * FROM signal_events WHERE signal_name = ?", (SIGNAL_NAME,)
            ).fetchone()
            self.assertEqual(row["recommendation"], "bet")
            self.assertAlmostEqual(row["model_p"], 0.55)
            self.assertAlmostEqual(row["market_p"], 0.57)

    def test_collect_once_end_to_end_with_mocked_http(self):
        lb_30d = [{"proxyWallet": "0xABC0000000000000000000000000000000000001", "name": "alice", "amount": 1000.0}]
        trades = [_trade()]
        gamma_market = {
            "id": 999001, "question": "Will X happen?", "slug": "will-x-happen",
            "conditionId": "0xcond1", "active": True, "closed": False,
            "acceptingOrders": True, "endDate": "2026-08-01T00:00:00Z",
            "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps(["0.55", "0.45"]),
            "liquidity": "1000", "volume24hr": "5000",
        }
        book = {
            "asks": [{"price": "0.57", "size": "300"}],
            "bids": [{"price": "0.53", "size": "150"}],
        }

        def fake_get(url, params=None, timeout=None):
            if "lb-api" in url:
                # Same single-wallet page for both roster windows
                return Mock(status_code=200, raise_for_status=Mock(), json=lambda: lb_30d)
            if "data-api" in url:
                return Mock(status_code=200, raise_for_status=Mock(), json=lambda: trades)
            if "gamma-api" in url:
                return Mock(status_code=200, json=lambda: [gamma_market])
            if "clob" in url:
                return Mock(status_code=200, json=lambda: book)
            raise AssertionError(f"unexpected url {url}")

        session = Mock()
        session.get.side_effect = fake_get

        with self._conn() as conn:
            stats = collect_once(conn, session=session, now_epoch=NOW)
            self.assertEqual(stats["signals"], 1)
            self.assertEqual(stats["errors"], 0)

            # market snapshot upserted with the Gamma numeric id
            snap = conn.execute(
                "SELECT * FROM market_snapshots WHERE market_id = '999001'"
            ).fetchone()
            self.assertIsNotNone(snap)

            # second pass: cursor + dedup prevent duplicates
            stats2 = collect_once(conn, session=session, now_epoch=NOW)
            self.assertEqual(stats2["signals"], 0)
            n = conn.execute(
                "SELECT COUNT(*) FROM signal_events WHERE signal_name = ?", (SIGNAL_NAME,)
            ).fetchone()[0]
            self.assertEqual(n, 1)

    def test_collect_once_gamma_failure_falls_back_to_condition_id(self):
        lb = [{"proxyWallet": "0xABC0000000000000000000000000000000000001", "name": "a", "amount": 1.0}]
        trades = [_trade()]
        book = {"asks": [{"price": "0.57", "size": "300"}], "bids": []}

        def fake_get(url, params=None, timeout=None):
            if "lb-api" in url:
                return Mock(status_code=200, raise_for_status=Mock(), json=lambda: lb)
            if "data-api" in url:
                return Mock(status_code=200, raise_for_status=Mock(), json=lambda: trades)
            if "gamma-api" in url:
                return Mock(status_code=500, json=lambda: {})
            if "clob" in url:
                return Mock(status_code=200, json=lambda: book)
            raise AssertionError(url)

        session = Mock()
        session.get.side_effect = fake_get
        with self._conn() as conn:
            stats = collect_once(conn, session=session, now_epoch=NOW)
            self.assertEqual(stats["signals"], 1)
            snap = conn.execute(
                "SELECT * FROM market_snapshots WHERE market_id = '0xcond1'"
            ).fetchone()
            self.assertIsNotNone(snap)


if __name__ == "__main__":
    unittest.main()
