import unittest
import queue
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


class MockAlpacaAPI:
    def __init__(self):
        self.submitted = []
        self.cancelled = []
        self.order_id_counter = 0

    def submit_order(self, **kwargs):
        self.order_id_counter += 1
        order_id = f"ord_{self.order_id_counter}"
        self.submitted.append({"id": order_id, **kwargs})
        return type("Order", (), {"id": order_id, "status": "new"})()

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return True

    def cancel_all_orders(self):
        return True

    def get_order(self, order_id):
        return type("Order", (), {"id": order_id, "status": "filled"})()

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


class TestAlpacaBrokerSLTP(unittest.TestCase):

    def setUp(self):
        self.api = MockAlpacaAPI()
        self.uq = queue.Queue()
        self.broker = app.AlpacaBroker({"alpaca": {"paper": True}}, self.uq)
        self.broker.api = self.api

    def test_simple_market_order(self):
        ok = self.broker.submit_order("AAPL", 10, "buy")
        self.assertTrue(ok)
        self.assertEqual(len(self.api.submitted), 1)
        order = self.api.submitted[0]
        self.assertEqual(order["side"], "buy")
        self.assertEqual(order["qty"], 10)
        self.assertEqual(order["symbol"], "AAPL")
        self.assertEqual(order["type"], "market")

    def test_bracket_order_long(self):
        ok = self.broker.submit_order("AAPL", 10, "buy", sl_pct=2.0, tp_pct=4.0, price=100.0)
        self.assertTrue(ok)
        self.assertEqual(len(self.api.submitted), 1)
        order = self.api.submitted[0]
        self.assertEqual(order["side"], "buy")
        self.assertEqual(order["qty"], 10)
        self.assertEqual(order["symbol"], "AAPL")
        self.assertEqual(order["order_class"], "bracket")
        self.assertEqual(float(order["take_profit"]["limit_price"]), 104.0)
        self.assertEqual(float(order["stop_loss"]["stop_price"]), 98.0)

    def test_bracket_order_short(self):
        ok = self.broker.submit_order("TSLA", 5, "sell", sl_pct=2.0, tp_pct=4.0, price=200.0)
        self.assertTrue(ok)
        self.assertEqual(len(self.api.submitted), 1)
        order = self.api.submitted[0]
        self.assertEqual(order["side"], "sell")
        self.assertEqual(order["order_class"], "bracket")
        self.assertEqual(float(order["take_profit"]["limit_price"]), 192.0)
        self.assertEqual(float(order["stop_loss"]["stop_price"]), 204.0)

    def test_bracket_order_with_explicit_prices(self):
        ok = self.broker.submit_order("AAPL", 10, "buy", sl_price=95.0, tp_price=110.0, price=100.0)
        self.assertTrue(ok)
        self.assertEqual(len(self.api.submitted), 1)
        order = self.api.submitted[0]
        self.assertEqual(float(order["stop_loss"]["stop_price"]), 95.0)
        self.assertEqual(float(order["take_profit"]["limit_price"]), 110.0)

    def test_sl_price_long(self):
        sl, tp = self.broker._resolve_sl_tp_prices("buy", 100.0, sl_pct=2.0, tp_pct=4.0)
        self.assertEqual(sl, 98.0)
        self.assertEqual(tp, 104.0)

    def test_sl_price_short(self):
        sl, tp = self.broker._resolve_sl_tp_prices("sell", 200.0, sl_pct=2.0, tp_pct=4.0)
        self.assertEqual(sl, 204.0)
        self.assertEqual(tp, 192.0)

    def test_sl_only(self):
        ok = self.broker.submit_order("AAPL", 10, "buy", sl_pct=2.0, price=100.0)
        self.assertTrue(ok)
        self.assertEqual(len(self.api.submitted), 2)

    def test_tp_only(self):
        ok = self.broker.submit_order("AAPL", 10, "buy", tp_pct=4.0, price=100.0)
        self.assertTrue(ok)
        self.assertEqual(len(self.api.submitted), 2)

    def test_no_connection_returns_false(self):
        self.broker.api = None
        ok = self.broker.submit_order("AAPL", 10, "buy")
        self.assertFalse(ok)

    def test_uses_passed_price_not_api_price(self):
        ok = self.broker.submit_order("AAPL", 10, "buy", sl_pct=2.0, tp_pct=4.0, price=150.0)
        self.assertTrue(ok)
        self.assertEqual(len(self.api.submitted), 1)
        order = self.api.submitted[0]
        self.assertEqual(float(order["stop_loss"]["stop_price"]), 147.0)
        self.assertEqual(float(order["take_profit"]["limit_price"]), 156.0)

    def test_falls_back_to_api_price_when_no_price_passed(self):
        ok = self.broker.submit_order("AAPL", 10, "buy", sl_pct=2.0, tp_pct=4.0)
        self.assertTrue(ok)
        self.assertGreaterEqual(len(self.api.submitted), 1)
        if self.api.submitted[0].get("order_class") == "bracket":
            order = self.api.submitted[0]
            self.assertEqual(float(order["stop_loss"]["stop_price"]), 98.0)
            self.assertEqual(float(order["take_profit"]["limit_price"]), 104.0)


if __name__ == "__main__":
    unittest.main()
