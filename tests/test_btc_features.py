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

    def test_sorted_bulk_path_matches_unsorted(self):
        bars = []
        for i in range(70):
            c = 1000.0 + i
            bars.append((i * 60_000, c, c + 1, c - 1, c + 0.5, 2.0))
        entry = 65 * 60
        a = window_features(bars, entry, lookback=60)
        b = window_features(list(reversed(bars)), entry, lookback=60)
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertEqual(a.shape, b.shape)
        for i in range(a.shape[0]):
            for j in range(a.shape[1]):
                self.assertAlmostEqual(float(a[i, j]), float(b[i, j]), places=5)


if __name__ == "__main__":
    unittest.main()
