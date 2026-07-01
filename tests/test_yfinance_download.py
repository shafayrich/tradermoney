import unittest
import warnings

import app as trader_app


class DummyYF:
    @staticmethod
    def download(*args, **kwargs):
        warnings.warn("possibly delisted; no price data found", UserWarning)
        return None


class SafeYFinanceDownloadTests(unittest.TestCase):
    def test_safe_yf_download_returns_none_for_delisted_warning(self):
        df = trader_app._safe_yf_download("BRK.B", "1d", "1m", yf_module=DummyYF())
        self.assertIsNone(df)


if __name__ == "__main__":
    unittest.main()
