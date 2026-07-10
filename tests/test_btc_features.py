import unittest

from src.ml.btc_features import window_features


class WindowFeaturesTests(unittest.TestCase):
    def test_excludes_current_open_kline(self):
        bars = [(i * 60_000, 1.0, 1.0, 1.0, 1.0, 1.0) for i in range(62)]
        # This bar is open at entry and must not affect the returned features.
        bars.append((62 * 60_000, 1.0, 100.0, 0.01, 100.0, 1.0))
        features = window_features(bars, 62 * 60, lookback=60)
        self.assertIsNotNone(features)
        self.assertEqual(float(features[-1, 0]), 0.0)


if __name__ == "__main__":
    unittest.main()
