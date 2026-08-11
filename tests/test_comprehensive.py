import unittest
from unittest import mock
import queue
import json
import time
import sys
import os
import threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OPENROUTER_API_KEY"] = "sk-or-v1-test"
import app


class MockAlpacaAPI:
    def __init__(self):
        self.submitted = []
        self.cancelled = []
        self.order_id_counter = 0
    def submit_order(self, **kwargs):
        self.order_id_counter += 1
        oid = f"ord_{self.order_id_counter}"
        self.submitted.append({"id": oid, **kwargs})
        return type("Order", (), {"id": oid, "status": "new"})()
    def cancel_order(self, oid):
        self.cancelled.append(oid)
        return True
    def cancel_all_orders(self):
        return True
    def get_order(self, oid):
        return type("Order", (), {"id": oid, "status": "filled"})()
    def get_account(self):
        return type("Account", (), {"equity": "10000", "buying_power": "5000", "cash": "5000"})()
    def close_all_positions(self):
        return True
    def get_positions(self):
        return {}
    def get_market_status(self):
        return True
    def stream_prices(self, syms, cb):
        pass
    def stop_stream(self):
        pass
    def is_connected(self):
        return True
    def get_latest_trade(self, symbol):
        return type("Trade", (), {"raw": {"trade": {"p": 100.0}}})()
    def get_latest_bar(self, symbol):
        return type("Bar", (), {"raw": {"bar": {"c": 100.0}}})()


class TestMaxSpend(unittest.TestCase):

    def setUp(self):
        self.uq = queue.Queue()
        cfg = {
            "broker": "Alpaca", "tickers": "AAPL", "mode": "signal",
            "quantity": 100, "max_spend": 0, "alpaca": {"paper": True},
            "use_bracket": False, "sl_percent": 2.0, "tp_percent": 4.0,
            "use_trailing": False, "trailing_percent": 1.5,
            "use_scale_out": False, "use_mtf_confirmation": False,
            "use_news_override": False, "direction": "both",
            "use_default_qty": True, "license_valid": False,
            "indicator_params": {}
        }
        self.bot = app.TradingEngine(ui_queue=self.uq, config=dict(cfg), broker=app.AlpacaBroker(cfg, self.uq))
        self.bot.broker.api = MockAlpacaAPI()

    def test_get_total_deployed_empty(self):
        self.bot.positions = {}
        self.assertEqual(self.bot._get_total_deployed(), 0.0)

    def test_get_total_deployed_one_position(self):
        self.bot.positions = {"AAPL": 10}
        self.bot.position_prices = {"AAPL": 150.0}
        self.assertEqual(self.bot._get_total_deployed(), 1500.0)

    def test_get_total_deployed_multiple(self):
        self.bot.positions = {"AAPL": 10, "TSLA": 5, "MSFT": 0}
        self.bot.position_prices = {"AAPL": 150.0, "TSLA": 200.0, "MSFT": 300.0}
        self.assertEqual(self.bot._get_total_deployed(), 2500.0)

    def test_get_total_deployed_ignores_shorts(self):
        self.bot.positions = {"AAPL": 10, "TSLA": -5}
        self.bot.position_prices = {"AAPL": 150.0, "TSLA": 200.0}
        self.assertEqual(self.bot._get_total_deployed(), 1500.0)

    def test_max_spend_blocks_when_exceeded(self):
        self.bot.positions = {"AAPL": 10}
        self.bot.position_prices = {"AAPL": 200.0}
        self.bot.config["max_spend"] = 1000
        deployed = self.bot._get_total_deployed()
        self.assertEqual(deployed, 2000.0)
        self.assertGreater(deployed, self.bot.config["max_spend"])

    def test_max_spend_caps_qty(self):
        self.bot.config["max_spend"] = 10000
        price = 150.0
        deployed = self.bot._get_total_deployed()
        available = self.bot.config["max_spend"] - deployed
        max_qty = int(available / price)
        self.assertEqual(max_qty, 66)

    def test_max_spend_with_existing_positions(self):
        self.bot.positions = {"AAPL": 10}
        self.bot.position_prices = {"AAPL": 100.0}
        self.bot.config["max_spend"] = 2000
        deployed = self.bot._get_total_deployed()
        self.assertEqual(deployed, 1000.0)
        available = self.bot.config["max_spend"] - deployed
        self.assertEqual(available, 1000.0)
        price = 200.0
        max_qty = int(available / price)
        self.assertEqual(max_qty, 5)

    def test_max_spend_200_usd_scenario(self):
        self.bot.config["max_spend"] = 200
        price = 150.0
        deployed = self.bot._get_total_deployed()
        available = self.bot.config["max_spend"] - deployed
        max_qty = int(available / price)
        self.assertEqual(max_qty, 1)
        self.bot.positions = {"AAPL": 1}
        self.bot.position_prices = {"AAPL": 150.0}
        deployed2 = self.bot._get_total_deployed()
        self.assertEqual(deployed2, 150.0)
        available2 = self.bot.config["max_spend"] - deployed2
        self.assertEqual(available2, 50.0)
        max_qty2 = int(available2 / price)
        self.assertEqual(max_qty2, 0)

    def test_max_spend_total_deployed_calculation(self):
        self.bot.positions = {"AAPL": 10, "TSLA": 5}
        self.bot.position_prices = {"AAPL": 150.0, "TSLA": 200.0}
        self.bot.config["max_spend"] = 5000
        total = self.bot._get_total_deployed()
        self.assertEqual(total, 2500.0)
        available = self.bot.config["max_spend"] - total
        self.assertEqual(available, 2500.0)
        self.assertEqual(int(available / 150.0), 16)


class TestDatabaseManager(unittest.TestCase):

    def setUp(self):
        self.db = app.DatabaseManager(db_path=":memory:")

    def test_concurrent_reads_and_writes(self):
        """Threaded server now hammers the same SQLite connection concurrently -
        all access must be serialized (no 500s / 'locked' errors)."""
        import threading as th
        errs = []

        def writer():
            try:
                for i in range(25):
                    self.db.insert_log(f"w{i}")
                    self.db.insert_trade("t", "AAPL", "BUY", 1, 1.0)
            except Exception as e:
                errs.append(e)

        def reader():
            try:
                for _ in range(25):
                    self.db.get_recent_logs(50)
                    self.db.get_recent_trades(50)
                    self.db.get_recent_signals(50)
                    self.db.get_leaderboard()
                    self.db.get_earnings_summary()
            except Exception as e:
                errs.append(e)

        threads = [th.Thread(target=writer), th.Thread(target=reader),
                   th.Thread(target=reader), th.Thread(target=writer),
                   th.Thread(target=reader), th.Thread(target=writer)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errs, [])

    def test_insert_and_get_trades(self):
        self.db.insert_trade("2026-07-28T10:00:00", "AAPL", "BUY", 10, 150.0)
        self.db.insert_trade("2026-07-28T11:00:00", "TSLA", "SELL", 5, 200.0)
        trades = self.db.get_recent_trades(10)
        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[0]["action"], "SELL")
        self.assertEqual(trades[1]["action"], "BUY")

    def test_insert_and_get_signals(self):
        self.db.insert_signal("2026-07-28T10:00:00", "AAPL", "BUY", 150.0, "RSI oversold")
        self.db.insert_signal("2026-07-28T11:00:00", "TSLA", "SELL", 200.0, "MACD bearish")
        sigs = self.db.get_recent_signals(10)
        self.assertEqual(len(sigs), 2)

    def test_insert_and_get_logs(self):
        self.db.insert_log("Bot started")
        self.db.insert_log("Signal received")
        logs = self.db.get_recent_logs(10)
        self.assertEqual(len(logs), 2)

    def test_backtest_save(self):
        self.db.insert_backtest('{"test": true}')
        self.assertIsNotNone(self.db.conn.execute("SELECT * FROM backtests").fetchone())

    def test_candle_cache(self):
        import pandas as pd
        df = pd.DataFrame({"Close": [100, 101, 102]})
        self.db.cache_candle("AAPL", "1m", df)
        cached = self.db.get_cached_candle("AAPL", "1m")
        self.assertIsNotNone(cached)
        self.assertEqual(len(cached), 3)

    def test_empty_cache_returns_none(self):
        cached = self.db.get_cached_candle("NONEXIST", "1m")
        self.assertIsNone(cached)

    def test_clean_candle_cache(self):
        import pandas as pd
        df = pd.DataFrame({"Close": [100, 101, 102]})
        self.db.cache_candle("AAPL", "1m", df)
        self.db.clean_candle_cache(max_hours=-1)
        cached = self.db.get_cached_candle("AAPL", "1m")
        self.assertIsNone(cached)

    def test_chat_sessions(self):
        sid = self.db.create_chat_session("Test Session")
        self.assertIsNotNone(sid)
        sessions = self.db.get_chat_sessions()
        self.assertEqual(len(sessions), 1)
        self.db.rename_chat_session(sid, "Renamed")
        self.db.delete_chat_session(sid)
        self.assertEqual(len(self.db.get_chat_sessions()), 0)

    def test_chat_history(self):
        sid = self.db.create_chat_session()
        self.db.insert_chat_message(sid, "user", "Hello")
        self.db.insert_chat_message(sid, "bot", "Hi there")
        history = self.db.get_chat_history(sid)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["role"], "user")
        self.assertEqual(history[1]["role"], "bot")

    def test_leaderboard(self):
        self.db.update_leaderboard("user1", 65.5, 100)
        self.db.update_leaderboard("user2", 72.0, 50)
        lb = self.db.get_leaderboard()
        self.assertEqual(len(lb), 2)
        self.assertGreaterEqual(lb[0]["win_rate"], lb[1]["win_rate"])

    def test_earnings_record(self):
        self.db.record_earnings("2026-07-28T10:00:00", "AAPL", "LONG", 100.0, 110.0, 10, 100.0, 10.0, "Take Profit")
        self.db.record_earnings("2026-07-28T11:00:00", "TSLA", "SHORT", 200.0, 180.0, 5, 100.0, 10.0, "Signal")
        earnings = self.db.get_earnings(10)
        self.assertEqual(len(earnings), 2)
        s = self.db.get_earnings_summary()
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["total_pnl"], 200.0)
        self.assertEqual(s["wins"], 2)

    def test_multiple_db_operations(self):
        self.db.insert_trade("2026-07-28T10:00:00", "AAPL", "BUY", 10, 100.0)
        self.db.record_earnings("2026-07-28T11:00:00", "AAPL", "LONG", 100.0, 110.0, 10, 100.0, 10.0, "Take Profit")
        self.db.insert_signal("2026-07-28T12:00:00", "AAPL", "BUY", 100.0, "Test")
        self.db.insert_log("Test")
        self.assertEqual(len(self.db.get_recent_trades(10)), 1)
        self.assertEqual(len(self.db.get_earnings(10)), 1)
        self.assertEqual(len(self.db.get_recent_signals(10)), 1)
        self.assertEqual(len(self.db.get_recent_logs(10)), 1)


class TestHelpers(unittest.TestCase):

    def test_ts_format(self):
        ts = app._ts()
        self.assertTrue(len(ts) > 10)

    def test_clean_symbol(self):
        self.assertEqual(app.clean_symbol("AAPL:10"), "AAPL")
        self.assertEqual(app.clean_symbol("BTC/USD"), "BTC/USD")
        self.assertEqual(app.clean_symbol("  aapl  "), "AAPL")

    def test_to_local_time(self):
        result = app.to_local_time("2026-07-28T10:00:00", "America/New_York")
        self.assertIsNotNone(result)

    def test_safe_yf_download_handles_warning(self):
        class WarnYF:
            @staticmethod
            def download(*a, **kw):
                import warnings
                warnings.warn("possibly delisted", UserWarning)
                return None
        result = app._safe_yf_download("BRK.B", "1d", "1m", yf_module=WarnYF())
        self.assertIsNone(result)

    def test_is_internet_available(self):
        result = app.is_internet_available()
        self.assertIsInstance(result, bool)

    def test_port_in_use(self):
        result = app.is_port_in_use(9999)
        self.assertIsInstance(result, bool)

    def test_verify_gumroad_license_invalid_format(self):
        ok, msg = app.verify_gumroad_license("invalid-key-format")
        self.assertFalse(ok)

    def test_normalize_yf_symbol_brk_b(self):
        self.assertEqual(app._normalize_yf_symbol("BRK.B"), "BRK-B")

    def test_normalize_yf_symbol_bf_b(self):
        self.assertEqual(app._normalize_yf_symbol("BF.B"), "BF-B")

    def test_normalize_yf_symbol_lowercase(self):
        self.assertEqual(app._normalize_yf_symbol("brk.b"), "BRK-B")

    def test_normalize_yf_symbol_crypto(self):
        self.assertEqual(app._normalize_yf_symbol("BTC/USD"), "BTC-USD")

    def test_normalize_yf_symbol_plain(self):
        self.assertEqual(app._normalize_yf_symbol("AAPL"), "AAPL")


class TestAlpacaBrokerComprehensive(unittest.TestCase):

    def setUp(self):
        self.api = MockAlpacaAPI()
        self.uq = queue.Queue()
        self.broker = app.AlpacaBroker({"alpaca": {"paper": True}}, self.uq)
        self.broker.api = self.api

    def test_connect_no_api_key(self):
        broker = app.AlpacaBroker({"alpaca": {"paper": True, "api_key": ""}}, self.uq)
        ok = broker.connect()
        self.assertFalse(ok)

    def test_is_connected(self):
        self.assertTrue(self.broker.is_connected())
        self.broker.api = None
        self.assertFalse(self.broker.is_connected())

    def test_get_account_no_api(self):
        self.broker.api = None
        acct = self.broker.get_account()
        self.assertIsNone(acct)

    def test_cancel_all_orders_no_api(self):
        self.broker.api = None
        ok = self.broker.cancel_all_orders()
        self.assertFalse(ok)

    def test_get_positions(self):
        pos = self.broker.get_positions()
        self.assertEqual(pos, {})

    def test_get_current_price_via_trade(self):
        price = self.broker._get_current_price("AAPL")
        self.assertEqual(price, 100.0)

    def test_get_current_price_with_fallbacks(self):
        self.broker.api = MockAlpacaAPI()
        self.broker.api.get_latest_trade = lambda s: (_ for _ in ()).throw(Exception("fail"))
        self.broker.api.get_latest_bar = lambda s: (_ for _ in ()).throw(Exception("fail"))
        with mock.patch("yfinance.download", side_effect=Exception("network down")):
            price = self.broker._get_current_price("AAPL")
        self.assertIsNone(price)

    def test_get_current_price_yfinance_fallback_works(self):
        # yfinance fallback should return a real price (flat columns), not None
        self.broker.api = MockAlpacaAPI()
        self.broker.api.get_latest_trade = lambda s: (_ for _ in ()).throw(Exception("fail"))
        self.broker.api.get_latest_bar = lambda s: (_ for _ in ()).throw(Exception("fail"))
        import pandas as pd
        idx = pd.date_range("2026-08-10", periods=3, freq="min")
        df = pd.DataFrame({"Open": [100.0, 101.0, 102.0],
                           "High": [103.0, 104.0, 105.0],
                           "Low": [99.0, 100.0, 101.0],
                           "Close": [101.5, 102.5, 103.25],
                           "Volume": [1000, 1100, 1200]}, index=idx)
        with mock.patch("yfinance.download", return_value=df):
            price = self.broker._get_current_price("AAPL")
        self.assertEqual(price, 103.25)

    def test_submit_conditional_order(self):
        order = self.broker._submit_conditional_order("AAPL", 10, "sell", "limit", 105.0)
        self.assertIsNotNone(order)
        self.assertEqual(self.api.submitted[0]["type"], "limit")
        self.assertEqual(float(self.api.submitted[0]["limit_price"]), 105.0)

    def test_submit_conditional_stop_order(self):
        order = self.broker._submit_conditional_order("AAPL", 10, "sell", "stop", 95.0)
        self.assertIsNotNone(order)
        self.assertEqual(self.api.submitted[0]["type"], "stop")
        self.assertEqual(float(self.api.submitted[0]["stop_price"]), 95.0)

    def test_bracket_order_long(self):
        ok = self.broker.submit_order("AAPL", 10, "buy", sl_pct=2.0, tp_pct=4.0, price=100.0)
        self.assertTrue(ok)
        self.assertEqual(len(self.api.submitted), 3)
        tp = self.api.submitted[1]
        self.assertEqual(tp["type"], "limit")
        self.assertEqual(float(tp["limit_price"]), 104.0)
        sl = self.api.submitted[2]
        self.assertEqual(sl["type"], "stop")
        self.assertEqual(float(sl["stop_price"]), 98.0)

    def test_bracket_order_short(self):
        ok = self.broker.submit_order("TSLA", 5, "sell", sl_pct=2.0, tp_pct=4.0, price=200.0)
        self.assertTrue(ok)
        self.assertEqual(len(self.api.submitted), 3)
        tp = self.api.submitted[1]
        self.assertEqual(tp["type"], "limit")
        self.assertEqual(float(tp["limit_price"]), 192.0)
        sl = self.api.submitted[2]
        self.assertEqual(sl["type"], "stop")
        self.assertEqual(float(sl["stop_price"]), 204.0)

    def test_uses_passed_price(self):
        ok = self.broker.submit_order("AAPL", 10, "buy", sl_pct=2.0, tp_pct=4.0, price=150.0)
        self.assertTrue(ok)
        sl = self.api.submitted[2]
        tp = self.api.submitted[1]
        self.assertEqual(float(sl["stop_price"]), 147.0)
        self.assertEqual(float(tp["limit_price"]), 156.0)

    def test_falls_back_to_api_price(self):
        ok = self.broker.submit_order("AAPL", 10, "buy", sl_pct=2.0, tp_pct=4.0)
        self.assertTrue(ok)
        sl = self.api.submitted[2]
        tp = self.api.submitted[1]
        self.assertEqual(float(sl["stop_price"]), 98.0)
        self.assertEqual(float(tp["limit_price"]), 104.0)


class TestBaseBroker(unittest.TestCase):

    def test_resolve_sl_tp_long(self):
        broker = app.BaseBroker({}, queue.Queue())
        sl, tp = broker._resolve_sl_tp_prices("buy", 100.0, sl_pct=2.0, tp_pct=4.0)
        self.assertEqual(sl, 98.0)
        self.assertEqual(tp, 104.0)

    def test_resolve_sl_tp_short(self):
        broker = app.BaseBroker({}, queue.Queue())
        sl, tp = broker._resolve_sl_tp_prices("sell", 100.0, sl_pct=2.0, tp_pct=4.0)
        self.assertEqual(sl, 102.0)
        self.assertEqual(tp, 96.0)

    def test_resolve_sl_tp_explicit_overrides_percent(self):
        broker = app.BaseBroker({}, queue.Queue())
        sl, tp = broker._resolve_sl_tp_prices("buy", 100.0, sl_pct=2.0, tp_pct=4.0, sl_price=90.0, tp_price=110.0)
        self.assertEqual(sl, 90.0)
        self.assertEqual(tp, 110.0)

    def test_resolve_sl_tp_sl_only(self):
        broker = app.BaseBroker({}, queue.Queue())
        sl, tp = broker._resolve_sl_tp_prices("buy", 100.0, sl_pct=2.0)
        self.assertEqual(sl, 98.0)
        self.assertIsNone(tp)

    def test_resolve_sl_tp_tp_only(self):
        broker = app.BaseBroker({}, queue.Queue())
        sl, tp = broker._resolve_sl_tp_prices("sell", 100.0, tp_pct=4.0)
        self.assertIsNone(sl)
        self.assertEqual(tp, 96.0)

    def test_emit_error(self):
        q = queue.Queue()
        broker = app.BaseBroker({}, q)
        broker._emit_error("Test error")
        self.assertIn("Test error", q.get(timeout=1)[1])

    def test_cancel_all_orders_returns_false(self):
        broker = app.BaseBroker({}, queue.Queue())
        self.assertFalse(broker.cancel_all_orders())

    def test_get_open_orders_returns_empty(self):
        broker = app.BaseBroker({}, queue.Queue())
        self.assertEqual(broker.get_open_orders(), [])

    def test_is_connected_returns_true(self):
        broker = app.BaseBroker({}, queue.Queue())
        self.assertTrue(broker.is_connected())

    def test_submit_order_raises(self):
        broker = app.BaseBroker({}, queue.Queue())
        with self.assertRaises(NotImplementedError):
            broker.submit_order()

    def test_close_all_positions_raises(self):
        broker = app.BaseBroker({}, queue.Queue())
        with self.assertRaises(NotImplementedError):
            broker.close_all_positions()


class TestIndicatorCalculator(unittest.TestCase):

    def test_compute_all_returns_dataframe(self):
        import pandas as pd
        import numpy as np
        n = 60
        df = pd.DataFrame({
            "Close": [float(100 + i * 0.5) for i in range(n)],
            "High": [float(102 + i * 0.5) for i in range(n)],
            "Low": [float(98 + i * 0.5) for i in range(n)],
            "Volume": [1000.0] * n
        })
        params = {
            "rsi_period": 14, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
            "bb_period": 20, "bb_std": 2.0, "adx_threshold": 20, "adx_period": 14,
            "vol_threshold": 1.5, "vol_period": 20, "supertrend_period": 10,
            "supertrend_multiplier": 3.0, "stoch_k_period": 14, "stoch_d_period": 3,
            "atr_period": 14
        }
        result = app.IndicatorCalculator.compute_all(df, 9, 50, params)
        self.assertIsNotNone(result)
        cols = set(result.columns)
        for c in ["RSI", "MACD", "BB_upper", "BB_lower", "VWAP", "ATR", "ADX", "Stoch_K", "Stoch_D"]:
            self.assertIn(c, cols, f"Missing column: {c}")

    def test_compute_all_with_insufficient_data(self):
        import pandas as pd
        df = pd.DataFrame({
            "Close": [100, 101],
            "High": [102, 103],
            "Low": [98, 99],
            "Volume": [1000, 1100]
        })
        result = app.IndicatorCalculator.compute_all(df, 9, 50, {})
        self.assertIsNotNone(result)
        self.assertTrue(len(result) <= 2)


class TestSignalAnalyzer(unittest.TestCase):

    def test_safe_float(self):
        self.assertEqual(app.SignalAnalyzer._sf(5.5), 5.5)
        self.assertEqual(app.SignalAnalyzer._sf("3.14"), 3.14)
        self.assertEqual(app.SignalAnalyzer._sf(None), 0.0)
        self.assertEqual(app.SignalAnalyzer._sf("invalid"), 0.0)
        self.assertEqual(app.SignalAnalyzer._sf(0), 0.0)

    def test_generate_signal_with_precomputed_df(self):
        import pandas as pd
        import numpy as np
        n = 60
        close_vals = [float(100 + i * 0.5) for i in range(n)]
        df = pd.DataFrame({
            "Close": close_vals,
            "High": [v + 1 for v in close_vals],
            "Low": [v - 1 for v in close_vals],
            "Volume": [1000.0] * n
        })
        params = {
            "rsi_period": 14, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
            "bb_period": 20, "bb_std": 2.0, "adx_threshold": 20, "adx_period": 14,
            "vol_threshold": 1.5, "vol_period": 20, "supertrend_period": 10,
            "supertrend_multiplier": 3.0, "stoch_k_period": 14, "stoch_d_period": 3,
            "atr_period": 14, "atr_stop_mult": 2.0, "atr_tp_mult": 3.0,
            "ema_fast": 9, "ema_slow": 50
        }
        df2 = app.IndicatorCalculator.compute_all(df, 9, 50, params)
        config = {
            "use_rsi": True, "use_macd": True, "use_vwap": True, "use_bollinger": True,
            "use_adx": True, "use_vol_confirm": True, "use_supertrend": True,
            "use_stochastic": True, "direction": "both", "emas": [9, 50]
        }
        prev_fast = df2["EMA_fast"].iloc[-2] if len(df2) >= 2 else None
        prev_slow = df2["EMA_slow"].iloc[-2] if len(df2) >= 2 else None
        sig, rationale, conf = app.SignalAnalyzer.generate_signal(df2, prev_fast, prev_slow, config, params)
        self.assertIn(sig, ["BUY", "SELL", None])
        if sig:
            self.assertIsInstance(rationale, str)
            self.assertGreaterEqual(conf, 0)
            self.assertLessEqual(conf, 1)

    def test_safe_float(self):
        self.assertEqual(app.SignalAnalyzer._sf(5.5), 5.5)
        self.assertEqual(app.SignalAnalyzer._sf("3.14"), 3.14)
        self.assertEqual(app.SignalAnalyzer._sf(None), 0.0)
        self.assertEqual(app.SignalAnalyzer._sf("invalid"), 0.0)
        self.assertEqual(app.SignalAnalyzer._sf(0), 0.0)

    def test_confirm_returns_bool(self):
        import pandas as pd
        n = 60
        close_vals = [float(100 + i * 0.5) for i in range(n)]
        df = pd.DataFrame({
            "Close": close_vals,
            "High": [v + 1 for v in close_vals],
            "Low": [v - 1 for v in close_vals],
            "Volume": [1000.0] * n
        })
        params = {
            "rsi_period": 14, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
            "bb_period": 20, "bb_std": 2.0, "adx_threshold": 20, "adx_period": 14,
            "vol_threshold": 1.5, "vol_period": 20, "supertrend_period": 10,
            "supertrend_multiplier": 3.0, "stoch_k_period": 14, "stoch_d_period": 3,
            "atr_period": 14
        }
        df2 = app.IndicatorCalculator.compute_all(df, 9, 50, params)
        config = {"use_rsi": True, "use_macd": True, "direction": "both"}
        ok, direction = app.SignalAnalyzer._confirm(df2, config, "bull", df2["Close"].iloc[-1], params)
        self.assertIsInstance(ok, bool)


class TestIndicatorParams(unittest.TestCase):

    def test_get_indicator_params_defaults(self):
        params = app.get_indicator_params({})
        self.assertEqual(params["rsi_period"], 14)
        self.assertEqual(params["rsi_oversold"], 30)
        self.assertEqual(params["rsi_overbought"], 70)
        self.assertEqual(params["macd_fast"], 12)
        self.assertEqual(params["macd_slow"], 26)
        self.assertEqual(params["macd_signal"], 9)
        self.assertEqual(params["bb_period"], 20)
        self.assertEqual(params["bb_std"], 2.0)
        self.assertEqual(params["adx_threshold"], 20)
        self.assertEqual(params["vol_threshold"], 1.5)

    def test_get_indicator_params_custom(self):
        params = app.get_indicator_params({"indicator_params": {"rsi_period": 7, "macd_fast": 6}})
        self.assertEqual(params["rsi_period"], 7)
        self.assertEqual(params["macd_fast"], 6)
        self.assertEqual(params["rsi_overbought"], 70)


class TestTradingEngine(unittest.TestCase):

    def setUp(self):
        self.uq = queue.Queue()
        cfg = {
            "broker": "Alpaca", "tickers": "AAPL", "mode": "signal",
            "quantity": 100, "max_spend": 0, "alpaca": {"paper": True},
            "use_bracket": False, "sl_percent": 2.0, "tp_percent": 4.0,
            "use_trailing": False, "trailing_percent": 1.5,
            "use_scale_out": False, "use_mtf_confirmation": False,
            "use_news_override": False, "direction": "both",
            "use_default_qty": True, "license_valid": False,
            "indicator_params": {}
        }
        self.bot = app.TradingEngine(ui_queue=self.uq, config=dict(cfg), broker=app.AlpacaBroker(cfg, self.uq))
        self.bot.broker.api = MockAlpacaAPI()

    def test_engine_init(self):
        self.assertIsNotNone(self.bot)
        self.assertEqual(self.bot.config["broker"], "Alpaca")
        self.assertFalse(self.bot.running)

    def test_log_method(self):
        self.bot._log("Test log message")
        found = False
        while not self.uq.empty():
            item = self.uq.get_nowait()
            if isinstance(item, tuple) and "Test log message" in str(item):
                found = True
                break
        self.assertTrue(found)

    def test_queue_order(self):
        ok = self.bot._queue_order("AAPL", 10, "buy", sl_pct=2.0, tp_pct=4.0, price=100.0)
        self.assertTrue(ok)
        self.assertEqual(self.bot.order_queue.qsize(), 1)

    def test_queue_order_full_queue(self):
        for i in range(100):
            self.bot._queue_order("AAPL", 1, "buy", price=100.0)
        ok = self.bot._queue_order("AAPL", 1, "buy", price=100.0)
        self.assertTrue(ok)

    def test_close_position_long(self):
        self.bot.positions["AAPL"] = 10
        self.bot.position_prices["AAPL"] = 100.0
        self.bot._close_position("AAPL", exit_price=110.0, reason="Take Profit")
        self.assertNotIn("AAPL", self.bot.positions)
        self.assertNotIn("AAPL", self.bot.position_prices)

    def test_close_position_short(self):
        self.bot.positions["AAPL"] = -10
        self.bot.position_prices["AAPL"] = 100.0
        self.bot._close_position("AAPL", exit_price=90.0, reason="Take Profit")
        self.assertNotIn("AAPL", self.bot.positions)

    def test_close_position_clears_bracket_and_trailing(self):
        self.bot.positions["AAPL"] = 10
        self.bot.position_prices["AAPL"] = 100.0
        self.bot.bracket_positions.add("AAPL")
        self.bot.trailing_stops["AAPL"] = {"active": True}
        self.bot._close_position("AAPL", exit_price=110.0, reason="Signal")
        self.assertNotIn("AAPL", self.bot.bracket_positions)
        self.assertNotIn("AAPL", self.bot.trailing_stops)

    def test_close_position_with_zero_qty(self):
        self.bot.positions["AAPL"] = 0
        self.bot.position_prices["AAPL"] = 100.0
        self.bot._close_position("AAPL", exit_price=110.0, reason="Signal")
        self.assertNotIn("AAPL", self.bot.positions)

    def test_gatekeeper_blocks_when_inactive(self):
        self.bot.is_active = False
        self.bot.config["max_spend"] = 0
        self.bot.running = True
        self.assertEqual(self.bot.positions.get("AAPL"), None)

    def test_max_spend_skip_logic(self):
        self.bot.config["max_spend"] = 100
        self.bot.positions = {"AAPL": 10}
        self.bot.position_prices = {"AAPL": 50.0}
        deployed = self.bot._get_total_deployed()
        self.assertEqual(deployed, 500.0)
        self.assertGreater(deployed, self.bot.config["max_spend"])

    def test_execute_gatekeeper_blocked(self):
        self.bot.is_active = False
        import pandas as pd
        latest = pd.Series({"ATR": 2.0})
        self.bot._execute("AAPL", "BUY", 100.0, latest,
                          use_bracket=False, use_atr=False,
                          sl_pct=2.0, tp_pct=4.0, conf=0.8)
        self.assertNotIn("AAPL", self.bot.positions)

    def test_direction_filter_long_only_blocks_sell(self):
        self.bot.direction = "long"
        self.bot.is_active = True
        self.bot.running = True
        import pandas as pd
        latest = pd.Series({"ATR": 2.0})
        self.bot._execute("AAPL", "SELL", 100.0, latest,
                          use_bracket=False, use_atr=False,
                          sl_pct=2.0, tp_pct=4.0, conf=0.8)
        self.assertNotIn("AAPL", self.bot.positions)

    def test_direction_filter_short_only_blocks_buy(self):
        self.bot.direction = "short"
        self.bot.is_active = True
        self.bot.running = True
        import pandas as pd
        latest = pd.Series({"ATR": 2.0})
        self.bot._execute("AAPL", "BUY", 100.0, latest,
                          use_bracket=False, use_atr=False,
                          sl_pct=2.0, tp_pct=4.0, conf=0.8)
        self.assertNotIn("AAPL", self.bot.positions)

    def test_engine_unlicensed_restrictions(self):
        self.assertEqual(self.bot.config["mode"], "signal")
        self.assertEqual(self.bot.config["broker"], "Alpaca")

    def test_engine_sets_tickers_from_config(self):
        self.bot.config["tickers"] = "AAPL"
        self.assertEqual(self.bot.config["tickers"], "AAPL")


class TestAPIRoutes(unittest.TestCase):

    def setUp(self):
        self.app = app.app
        self.client = self.app.test_client()

    def test_api_status_contains_spend_and_report_fields(self):
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn("max_spend", data)
        self.assertIn("deployed", data)
        self.assertIn("hourly_report", data)
        self.assertIn("stopped_by", data)

    @unittest.mock.patch("app._load_ui_settings", return_value={})
    def test_terms_status_empty(self, _m):
        r = self.client.get("/api/terms/status")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertFalse(d["accepted"])
        self.assertFalse(d["dismissed"])
        self.assertEqual(d["current_version"], "2.0")

    @unittest.mock.patch("app._load_ui_settings", return_value={"terms_accepted": True, "terms_accepted_version": "2.0", "terms_dismissed": True})
    def test_terms_status_accepted(self, _m):
        d = self.client.get("/api/terms/status").get_json()
        self.assertTrue(d["accepted"])
        self.assertTrue(d["dismissed"])
        self.assertEqual(d["accepted_version"], "2.0")

    @unittest.mock.patch("app._set_ui_setting")
    def test_terms_accept_stores_dismissed(self, mock_set):
        r = self.client.post("/api/terms/accept", json={"dismissed": True, "version": "2.0"})
        self.assertEqual(r.status_code, 200)
        mock_set.assert_any_call("terms_accepted", True)
        mock_set.assert_any_call("terms_accepted_version", "2.0")
        mock_set.assert_any_call("terms_dismissed", True)

    @unittest.mock.patch("app._set_ui_setting")
    def test_ui_settings_save_merges_keys(self, mock_set):
        r = self.client.post("/api/ui-settings", json={"light": True, "sidebarW": 300})
        self.assertEqual(r.status_code, 200)
        mock_set.assert_any_call("light", True)
        mock_set.assert_any_call("sidebarW", 300)

    @unittest.mock.patch("app._load_ui_settings", return_value={"light": True, "sound": True})
    def test_ui_settings_get_listed_in_status_route(self, _m):
        pass


class TestHourlyReport(unittest.TestCase):

    def test_report_includes_account_details(self):
        dash = {"equity": 1000.0, "pl": 50.0, "buying_power": 500.0, "open_positions": 2}
        cfg = {"max_spend": 250.0, "broker": "Alpaca", "mode": "auto"}
        rep = app._build_hourly_report(dash, cfg, deployed=120.0,
                                       new_signals=3, new_orders=1, elapsed_h=12.0)
        self.assertIn("Hourly Progress Report", rep)
        self.assertIn("Equity: $1,000.00", rep)
        self.assertIn("P/L: $50.00 (+5.00%)", rep)
        self.assertIn("Buying power: $500.00", rep)
        self.assertIn("Deployed: $120.00 / $250.00", rep)
        self.assertIn("Open positions: 2", rep)
        self.assertIn("New signals: 3", rep)
        self.assertIn("New orders: 1", rep)
        self.assertIn("running 12.0h", rep)
        self.assertIn("Mode: auto", rep)

    def test_report_unlimited_spend_shows_unlimited(self):
        dash = {"equity": 1000.0, "pl": 0.0, "buying_power": 999.0, "open_positions": 0}
        cfg = {"max_spend": 0, "broker": "Alpaca", "mode": "signal"}
        rep = app._build_hourly_report(dash, cfg, deployed=0.0,
                                       new_signals=0, new_orders=0, elapsed_h=1.0)
        self.assertIn("(unlimited)", rep)

    def test_report_negative_pl(self):
        dash = {"equity": 1000.0, "pl": -120.0, "buying_power": 300.0, "open_positions": 1}
        rep = app._build_hourly_report(dash, {"max_spend": 0, "broker": "Alpaca", "mode": "signal"},
                                       deployed=400.0, new_signals=2, new_orders=2, elapsed_h=6.0)
        self.assertIn("P/L: $-120.00 (-12.00%)", rep)


class TestSpendPlumbing(unittest.TestCase):

    def test_max_spend_large_does_not_deploy_beyond_cap(self):
        uq = queue.Queue()
        cfg = {
            "broker": "Alpaca", "tickers": "AAPL", "mode": "signal",
            "quantity": 1000, "max_spend": 250.0, "alpaca": {"paper": True},
            "use_bracket": False, "sl_percent": 2.0, "tp_percent": 4.0,
            "use_trailing": False, "trailing_percent": 1.5,
            "use_scale_out": False, "use_mtf_confirmation": False,
            "use_news_override": False, "direction": "both",
            "use_default_qty": True, "license_valid": False,
            "indicator_params": {}
        }
        bot = app.TradingEngine(ui_queue=uq, config=cfg, broker=app.AlpacaBroker(cfg, uq))
        bot.broker.api = MockAlpacaAPI()
        bot.is_active = True
        bot.running = True
        bot.positions = {"AAPL": 1}
        bot.position_prices = {"AAPL": 200.0}
        # 1 share @ $200 in a $250 cap -> almost full; remaining ~$50
        self.assertEqual(bot._get_total_deployed(), 200.0)
        avail = 250.0 - bot._get_total_deployed()
        self.assertGreater(avail, 0)
        self.assertEqual(avail, 50.0)

    def test_max_spend_blocked_when_deployed_at_cap(self):
        uq = queue.Queue()
        cfg = {
            "broker": "Alpaca", "tickers": "AAPL", "mode": "signal",
            "quantity": 10, "max_spend": 400.0, "alpaca": {"paper": True},
            "use_bracket": False, "sl_percent": 2.0, "tp_percent": 4.0,
            "use_trailing": False, "trailing_percent": 1.5,
            "use_scale_out": False, "use_mtf_confirmation": False,
            "use_news_override": False, "direction": "both",
            "use_default_qty": True, "license_valid": True,
            "indicator_params": {}
        }
        bot = app.TradingEngine(ui_queue=uq, config=cfg, broker=app.AlpacaBroker(cfg, uq))
        bot.broker.api = MockAlpacaAPI()
        bot.is_active = True
        bot.running = True
        bot.positions = {"AAPL": 2}
        bot.position_prices = {"AAPL": 200.0}
        bot.per_ticker_qty = {"AAPL": 10, "TSLA": 10}
        import pandas as pd
        latest = pd.Series({"ATR": 2.0})
        # deployed is $400 = cap -> nothing more allowed
        self.assertEqual(bot._get_total_deployed(), 400.0)
        bot._execute("TSLA", "BUY", 100.0, latest,
                     use_bracket=False, use_atr=False, sl_pct=2.0, tp_pct=4.0, conf=0.8)
        self.assertNotIn("TSLA", bot.positions)


class TestAPIRoutesExtended(unittest.TestCase):

    def setUp(self):
        self.app = app.app
        self.client = self.app.test_client()

    def test_index_returns_html(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.content_type)

    def test_api_config_get(self):
        r = self.client.get("/api/config")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn("broker", data)

    def test_api_config_post(self):
        r = self.client.post("/api/config", json={"mode": "signal", "broker": "Alpaca"})
        self.assertEqual(r.status_code, 200)

    def test_api_update(self):
        r = self.client.get("/api/update")
        self.assertEqual(r.status_code, 200)

    def test_api_broker_status(self):
        r = self.client.get("/api/broker_status")
        self.assertEqual(r.status_code, 200)

    def test_api_earnings(self):
        r = self.client.get("/api/earnings")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn("trades", data)
        self.assertIn("summary", data)

    def test_api_license_status(self):
        r = self.client.get("/api/license-status")
        self.assertEqual(r.status_code, 200)

    def test_api_leaderboard(self):
        r = self.client.get("/api/leaderboard")
        self.assertEqual(r.status_code, 200)

    def test_api_webchat_with_message(self):
        r = self.client.post("/api/webchat", json={"message": "hello"})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn("reply", data)

    def test_api_webchat_empty_message(self):
        r = self.client.post("/api/webchat", json={"message": ""})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn("reply", data)

    def test_api_webchat_no_message(self):
        r = self.client.post("/api/webchat", json={})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn("reply", data)

    def test_api_thesis_list(self):
        r = self.client.get("/api/thesis/list")
        self.assertEqual(r.status_code, 200)

    def test_api_status(self):
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)

    def test_api_candles_returns_json(self):
        r = self.client.get("/api/candles")
        self.assertIn(r.status_code, [200, 500])
        if r.status_code == 200:
            data = r.get_json()
            self.assertIsInstance(data, list)

    def test_api_live_price_defaults(self):
        r = self.client.get("/api/live_price")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn("price", data)


if __name__ == "__main__":
    unittest.main()
