import unittest
import sys
import os
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OPENROUTER_API_KEY"] = "sk-or-v1-test"
import app

CFG = {
    "broker": "Alpaca", "tickers": "AAPL", "mode": "signal",
    "quantity": 1, "max_spend": 0, "alpaca": {"paper": True},
    "use_bracket": False, "sl_percent": 2.0, "tp_percent": 4.0,
    "use_trailing": False, "trailing_percent": 1.5,
    "use_scale_out": False, "use_mtf_confirmation": False,
    "use_news_override": False, "direction": "both",
    "use_default_qty": True, "license_valid": False,
    "indicator_params": {},
}


def sigs_from(sym, seq):
    """seq: list of (time, signal, price). Returns signal dicts."""
    out = []
    for t, s, p in seq:
        out.append({
            "time": t, "signal": s, "symbol": sym,
            "price": p, "shares": 0, "confidence": 0.7,
            "reason": "test", "indicators": {},
        })
    return out


class TestSymbolSim(unittest.TestCase):

    def test_small_profit_keeps_portfolio_math_correct(self):
        # $1000 start, buy @100, sell @108 -> +$8. Final MUST be ~1008, never thousands.
        cfg = dict(CFG, direction="long")
        s = sigs_from("AAPL", [
            ("2026-08-01 09:30:00", "BUY", 100.0),
            ("2026-08-01 10:30:00", "SELL", 108.0),
        ])
        trades, stats = app._run_symbol_sim(s, 1, 1000.0, cfg)
        self.assertAlmostEqual(stats["total_pnl"], 8.0, delta=0.5)
        self.assertAlmostEqual(stats["final_cash"], 1008.0, delta=0.5)
        self.assertEqual(stats["total_trades"], 1)
        self.assertGreater(stats["win_rate"], 0)

    def test_cannot_buy_more_than_capital(self):
        # qty=10 @ $300 with $1000 -> should scale down to 3 shares ($900), not skip/overspend
        s = sigs_from("AAPL", [
            ("2026-08-01 09:30:00", "BUY", 300.0),
            ("2026-08-01 10:30:00", "SELL", 310.0),
        ])
        trades, stats = app._run_symbol_sim(s, 10, 1000.0, CFG)
        entry = next(t for t in trades if t["type"] == "entry")
        self.assertEqual(entry["shares"], 3)
        self.assertLessEqual(stats["final_cash"], 1000.0 + 30.0 + 1.0)

    def test_single_share_worth_more_than_capital_is_skipped(self):
        # qty=1 @ $2000 with $1000 -> cannot afford, no entry, no loss
        s = sigs_from("AAPL", [
            ("2026-08-01 09:30:00", "BUY", 2000.0),
            ("2026-08-01 10:30:00", "SELL", 2100.0),
        ])
        trades, stats = app._run_symbol_sim(s, 1, 1000.0, CFG)
        self.assertEqual(stats["total_trades"], 0)
        self.assertEqual(stats["final_cash"], 1000.0)

    def test_short_requires_margin_within_capital(self):
        cfg = dict(CFG, direction="short")
        s = sigs_from("AAPL", [
            ("2026-08-01 09:30:00", "SELL", 100.0),
            ("2026-08-01 10:30:00", "BUY", 95.0),
        ])
        trades, stats = app._run_symbol_sim(s, 5, 1000.0, cfg)
        # 5 shares @ ~100 = $500 margin <= $1000, allowed
        self.assertEqual(stats["total_trades"], 1)
        self.assertGreater(stats["total_pnl"], 0)
        self.assertGreater(stats["final_cash"], 1000.0)

    def test_short_scaled_to_margin(self):
        # 20 shares @ $100 = $2000 > $1000 -> scale to 10 shares
        cfg = dict(CFG, direction="short")
        s = sigs_from("AAPL", [
            ("2026-08-01 09:30:00", "SELL", 100.0),
            ("2026-08-01 10:30:00", "BUY", 95.0),
        ])
        trades, stats = app._run_symbol_sim(s, 20, 1000.0, cfg)
        entry = next(t for t in trades if t["type"] == "entry")
        self.assertEqual(entry["shares"], 10)
        self.assertLess(stats["final_cash"], 1000.0 + 50.0 + 1.0)

    def test_long_only_direction_ignores_sells(self):
        cfg = dict(CFG, direction="long")
        s = sigs_from("AAPL", [
            ("2026-08-01 09:30:00", "SELL", 100.0),
        ])
        trades, stats = app._run_symbol_sim(s, 1, 1000.0, cfg)
        self.assertEqual(stats["total_trades"], 0)
        self.assertEqual(stats["final_cash"], 1000.0)

    def test_loss_reduces_capital_accurately(self):
        s = sigs_from("AAPL", [
            ("2026-08-01 09:30:00", "BUY", 100.0),
            ("2026-08-01 10:30:00", "SELL", 92.0),
        ])
        trades, stats = app._run_symbol_sim(s, 1, 1000.0, CFG)
        self.assertLess(stats["total_pnl"], 0)
        self.assertAlmostEqual(stats["final_cash"], 1000.0 + stats["total_pnl"], delta=0.5)


class TestPortfolioAggregation(unittest.TestCase):

    def _mk_trade(self, sym, pnl, eprice=100.0, shares=1, typ="exit"):
        return {
            "entry_time": "2026-08-01 09:30:00", "exit_time": "2026-08-01 10:30:00",
            "side": "LONG", "symbol": sym,
            "entry_price": eprice, "exit_price": eprice + pnl / shares,
            "shares": shares, "pnl": pnl, "type": typ,
            "reason_open": "", "reason_close": "", "indicators_at_entry": {},
            "days_held": 1,
        }

    def test_final_equals_initial_plus_total_pnl(self):
        trades = [
            self._mk_trade("AAPL", 8.0),
            self._mk_trade("MSFT", -2.0),
            self._mk_trade("AAPL", 4.0),
        ]
        p = app._aggregate_portfolio(trades, 1000.0)
        self.assertEqual(p["total_pnl"], 10.0)
        self.assertEqual(p["final_cash"], 1010.0)
        self.assertAlmostEqual(p["roi"], 1.0, delta=0.01)

    def test_final_is_never_initial_times_ticker_count(self):
        # 127 tickers, tiny pnl -> final must stay ~ initial + pnl, NOT initial*127
        trades = [self._mk_trade(f"S{i}", 0.1) for i in range(127)]
        p = app._aggregate_portfolio(trades, 1000.0)
        self.assertEqual(p["total_trades"], 127)
        self.assertAlmostEqual(p["final_cash"], 1012.7, delta=0.5)
        self.assertLess(p["final_cash"], 1000 * 127)

    def test_zero_trades_keeps_initial(self):
        p = app._aggregate_portfolio([], 500.0)
        self.assertEqual(p["final_cash"], 500.0)
        self.assertEqual(p["total_pnl"], 0.0)
        self.assertEqual(p["roi"], 0.0)

    def test_all_losses_no_crash(self):
        trades = [self._mk_trade("AAPL", -50.0)]
        p = app._aggregate_portfolio(trades, 1000.0)
        self.assertEqual(p["final_cash"], 950.0)
        self.assertEqual(p["profit_factor"], 0.0)
        self.assertEqual(p["win_rate"], 0.0)

    def test_max_drawdown_from_equity_curve(self):
        trades = [
            self._mk_trade("AAPL", -100.0),
            self._mk_trade("MSFT", 150.0),
        ]
        p = app._aggregate_portfolio(trades, 1000.0)
        self.assertGreater(p["max_drawdown_pct"], 0)
        self.assertEqual(p["final_cash"], 1050.0)


class TestSymbolCandidates(unittest.TestCase):

    def test_crypto_slash_normalized(self):
        self.assertEqual(app._yf_symbol_candidates("BTC/USD"), ["BTC-USD"])

    def test_bare_crypto_names(self):
        self.assertIn("MATIC-USD", app._yf_symbol_candidates("MATIC"))

    def test_japan_suffix(self):
        self.assertIn("7203.T", app._yf_symbol_candidates("7203"))

    def test_taiwan_suffix(self):
        self.assertEqual(app._yf_symbol_candidates("2330")[0], "2330.TW")

    def test_hong_kong_suffix(self):
        self.assertEqual(app._yf_symbol_candidates("0700")[0], "0700.HK")

    def test_korea_suffix(self):
        self.assertEqual(app._yf_symbol_candidates("005930")[0], "005930.KS")

    def test_aus_suffix(self):
        self.assertEqual(app._yf_symbol_candidates("CBA")[0], "CBA.AX")

    def test_aus_suffix_wbc(self):
        self.assertEqual(app._yf_symbol_candidates("WBC")[0], "WBC.AX")

    def test_matic_falls_back_to_pol(self):
        cands = app._yf_symbol_candidates("MATIC")
        self.assertIn("POL-USD", cands)

    def test_second_hk_batch_prefers_hk(self):
        for sym in ("9988", "3690", "1810", "9618", "0939", "1398", "3988", "2318", "1211"):
            self.assertEqual(app._yf_symbol_candidates(sym)[0], sym + ".HK", sym)

    def test_tw_batch_prefers_tw(self):
        for sym in ("2317", "2454", "2308", "2382"):
            self.assertEqual(app._yf_symbol_candidates(sym)[0], sym + ".TW", sym)

    def test_kr_batch_prefers_ks(self):
        for sym in ("000660", "035420", "005380", "051910"):
            self.assertEqual(app._yf_symbol_candidates(sym)[0], sym + ".KS", sym)

    def test_us_ticker_unchanged(self):
        self.assertEqual(app._yf_symbol_candidates("AAPL"), ["AAPL", "AAPL.AX"])

    def test_quantity_suffix_stripped(self):
        self.assertEqual(app._yf_symbol_candidates("AAPL:10"), ["AAPL", "AAPL.AX"])


class TestSafeDownloadNormalizes(unittest.TestCase):

    @staticmethod
    def _make_multiindex_df():
        import pandas as pd
        idx = pd.date_range("2026-08-01", periods=3, freq="D")
        cols = pd.MultiIndex.from_product(
            [["Open", "High", "Low", "Close", "Volume"], ["AAPL"]],
            names=["Price", "Ticker"])
        data = [[100.0, 105.0, 99.0, 104.0, 1000],
                [104.0, 110.0, 103.0, 109.0, 1200],
                [109.0, 112.0, 108.0, 110.0, 900]]
        return pd.DataFrame(data, index=idx, columns=cols)

    def test_multiindex_flattened_and_nan_dropped(self):
        df = self._make_multiindex_df()
        df.iloc[-1, df.columns.get_level_values(0).tolist().index("Close")] = float("nan")

        class FakeYf:
            def download(self, *a, **kw):
                return df

        out = app._safe_yf_download("AAPL", period="1mo", interval="1d",
                                    yf_module=FakeYf(), retries=1)
        self.assertIsNotNone(out)
        self.assertEqual(out.columns.nlevels, 1)
        self.assertIn("Close", out.columns)
        self.assertEqual(len(out), 2)

    def test_all_nan_close_returns_none(self):
        df = self._make_multiindex_df()
        close_level = df.columns.get_level_values(0).tolist().index("Close")
        for i in range(len(df)):
            df.iloc[i, close_level] = float("nan")

        class FakeYf:
            def download(self, *a, **kw):
                return df

        out = app._safe_yf_download("AAPL", period="1mo", interval="1d",
                                    yf_module=FakeYf(), retries=1)
        self.assertIsNone(out)


class TestBacktestWarmupWindow(unittest.TestCase):
    """Daily backtests must fetch warmup history but only trade the requested window."""

    @staticmethod
    def _make_trending_df(n_bars, start_price=100.0, seed=7):
        import pandas as pd
        import numpy as np
        idx = pd.date_range(end="2026-08-10", periods=n_bars, freq="B")
        rng = np.random.default_rng(seed)
        close = start_price + np.cumsum(rng.normal(0.05, 1.0, n_bars))
        return pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 1,
                             "Close": close, "Volume": np.full(n_bars, 1_000_000)},
                            index=idx)

    def test_backtest_fetches_warmup_and_gates_window(self):
        # 100 bars fetched (30 requested + 70 warmup), crossover only in
        # warmup region -> no trades allowed outside requested window.
        import pandas as pd
        df = self._make_trending_df(100)
        requested_days = 30
        warmup_days = app.min_period + 20 if hasattr(app, "min_period") else 70
        fetched_periods = []

        def fake_download(sym, period="1d", interval="1m", yf_module=None, retries=3, **kw):
            fetched_periods.append(period)
            n = int(period.rstrip("d"))
            return self._make_trending_df(n)

        cfg = dict(CFG, direction="both", emas=[9, 50], timeframe="1d",
                   indicator_params={"adx_threshold": 25, "vol_threshold": 0.5})
        with mock.patch("app._safe_yf_download", side_effect=fake_download):
            r = app.app.test_client().post(
                "/api/backtest",
                json={"config": cfg, "days": requested_days, "portfolio": True})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(fetched_periods, "expected a download for AAPL")
        self.assertGreaterEqual(int(fetched_periods[0].rstrip("d")), requested_days,
                                "should fetch extra warmup history")
        # Every trade must be within the last `requested_days` calendar days
        res = data.get("results", {})
        p = data.get("portfolio", {})
        cutoff = pd.Timestamp("2026-08-10") - pd.Timedelta(days=requested_days)
        for sym, rdict in res.items():
            for tr in rdict.get("trades", []):
                t = pd.Timestamp(tr.get("time", "").split(" ")[0])
                self.assertGreaterEqual(t, cutoff, f"{sym} traded before requested window")


if __name__ == "__main__":
    unittest.main()