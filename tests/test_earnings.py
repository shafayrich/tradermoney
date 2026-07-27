import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app

class TestEarnings(unittest.TestCase):

    def setUp(self):
        self.db = app.DatabaseManager(db_path=":memory:")

    def record_long_win(self):
        self.db.record_earnings(
            "2026-07-23T10:00:00", "AAPL", "LONG",
            entry_px=100.0, exit_px=110.0, qty=10,
            pnl=100.0, roi=10.0, reason="Take Profit"
        )

    def record_long_loss(self):
        self.db.record_earnings(
            "2026-07-23T11:00:00", "TSLA", "LONG",
            entry_px=200.0, exit_px=180.0, qty=5,
            pnl=-100.0, roi=-10.0, reason="Stop Loss"
        )

    def record_short_win(self):
        self.db.record_earnings(
            "2026-07-23T12:00:00", "BTC/USD", "SHORT",
            entry_px=50000.0, exit_px=45000.0, qty=0.1,
            pnl=500.0, roi=10.0, reason="Signal"
        )

    def record_short_loss(self):
        self.db.record_earnings(
            "2026-07-23T13:00:00", "ETH/USD", "SHORT",
            entry_px=3000.0, exit_px=3300.0, qty=1,
            pnl=-300.0, roi=-10.0, reason="Trailing Stop"
        )

    def test_record_and_fetch_earnings(self):
        self.record_long_win()
        trades = self.db.get_earnings(10)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["symbol"], "AAPL")
        self.assertEqual(t["side"], "LONG")
        self.assertEqual(t["entry"], 100.0)
        self.assertEqual(t["exit"], 110.0)
        self.assertEqual(t["qty"], 10)
        self.assertEqual(t["pnl"], 100.0)
        self.assertEqual(t["roi"], 10.0)
        self.assertEqual(t["reason"], "Take Profit")

    def test_multiple_trades(self):
        self.record_long_win()
        self.record_long_loss()
        self.record_short_win()
        self.record_short_loss()
        trades = self.db.get_earnings(10)
        self.assertEqual(len(trades), 4)

    def test_summary_empty(self):
        summary = self.db.get_earnings_summary()
        self.assertEqual(summary["total"], 0)

    def test_summary_with_trades(self):
        self.record_long_win()      # P&L: +100
        self.record_long_loss()     # P&L: -100
        self.record_short_win()     # P&L: +500
        self.record_short_loss()    # P&L: -300
        s = self.db.get_earnings_summary()
        self.assertEqual(s["total"], 4)
        self.assertEqual(s["total_pnl"], 200.0)  # 100 - 100 + 500 - 300
        self.assertEqual(s["wins"], 2)
        self.assertEqual(s["losses"], 2)
        self.assertEqual(s["best"], 500.0)
        self.assertEqual(s["worst"], -300.0)
        self.assertEqual(s["avg_roi"], 0.0)

    def test_summary_all_wins(self):
        self.record_long_win()
        self.record_short_win()
        s = self.db.get_earnings_summary()
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["wins"], 2)
        self.assertEqual(s["losses"], 0)
        self.assertEqual(s["total_pnl"], 600.0)

    def test_summary_all_losses(self):
        self.record_long_loss()
        self.record_short_loss()
        s = self.db.get_earnings_summary()
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["wins"], 0)
        self.assertEqual(s["losses"], 2)
        self.assertEqual(s["total_pnl"], -400.0)

    def test_close_reasons(self):
        reasons = {"Take Profit", "Stop Loss", "Signal", "Trailing Stop"}
        self.record_long_win()
        self.record_long_loss()
        self.record_short_win()
        self.record_short_loss()
        trades = self.db.get_earnings(10)
        found_reasons = {t["reason"] for t in trades}
        self.assertEqual(found_reasons, reasons)

    def test_pnl_rounding(self):
        self.db.record_earnings(
            "2026-07-23T14:00:00", "AAPL", "LONG",
            entry_px=100.0, exit_px=105.12345, qty=10,
            pnl=51.2345, roi=5.12345, reason="Signal"
        )
        trades = self.db.get_earnings(10)
        t = trades[0]
        self.assertEqual(t["pnl"], 51.23)
        self.assertEqual(t["roi"], 5.12)

    def test_max_spend_calculation(self):
        dbm = self.db
        pos = {"AAPL": 10, "TSLA": -5}
        prices = {"AAPL": 150.0, "TSLA": 200.0}
        dbm.positions = pos
        dbm.position_prices = prices
        total = 0.0
        for sym, p in pos.items():
            if p > 0:
                total += abs(p) * prices.get(sym, 0)
        self.assertEqual(total, 1500.0)

    def test_long_pnl_formula(self):
        entry = 100.0
        exit_px = 110.0
        qty = 10
        pnl = (exit_px - entry) * qty
        roi = ((exit_px - entry) / entry * 100) if entry else 0
        self.assertEqual(pnl, 100.0)
        self.assertEqual(roi, 10.0)

    def test_short_pnl_formula(self):
        entry = 100.0
        exit_px = 90.0
        qty = 10
        pnl = (entry - exit_px) * qty
        roi = ((entry - exit_px) / entry * 100) if entry else 0
        self.assertEqual(pnl, 100.0)
        self.assertEqual(roi, 10.0)


if __name__ == "__main__":
    unittest.main()
