import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from indicators import Candle, CandleError, atr, ema, macd, rsi, study, true_ranges, validate_closed_bars

UTC = timezone.utc
BASE = datetime(2026, 11, 2, tzinfo=UTC)


def bars(count=3, seconds=300):
    return [{"open_at": (BASE + timedelta(seconds=i * seconds)).isoformat(),
             "close_at": (BASE + timedelta(seconds=(i + 1) * seconds)).isoformat(),
             "closed": True, "open": "100", "high": "102", "low": "99", "close": "101"}
            for i in range(count)]


class IndicatorTests(unittest.TestCase):
    def test_wilder_published_seed(self):
        # Classic Wilder worksheet's first 14 changes (15 closing prices).
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
                  45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
        actual = rsi(closes, 14)
        self.assertEqual(actual[:14], [None] * 14)
        self.assertAlmostEqual(actual[14], 70.4641350211, places=8)

    def test_rsi_flat_up_down_conventions(self):
        self.assertEqual(rsi([10] * 30, 24)[-1], 50)
        self.assertEqual(rsi(range(1, 31), 24)[-1], 100)
        self.assertEqual(rsi(range(30, 0, -1), 24)[-1], 0)

    def test_atr_includes_overnight_gap_and_wilder_smoothing(self):
        candles = [Candle(BASE, BASE, 10, 11, 9, 10),
                   Candle(BASE, BASE, 20, 21, 19, 20),
                   Candle(BASE, BASE, 15, 16, 14, 15)]
        self.assertEqual(true_ranges(candles), [2, 11, 6])
        self.assertEqual(atr(candles, 2), [None, 6.5, 6.25])

    def test_ema_sma_seed_then_recursive(self):
        self.assertEqual(ema([1, 2, 3, 4, 5], 3), [None, None, 2, 3, 4])

    def test_macd_constant_history_has_zero_line_and_histogram(self):
        result = macd([123] * 400)
        self.assertTrue(all(value is None for value in result["line"][:25]))
        self.assertTrue(all(value is None for value in result["signal"][:33]))
        self.assertEqual(result["line"][-1], 0)
        self.assertEqual(result["signal"][-1], 0)
        self.assertEqual(result["histogram"][-1], 0)

    def test_macd_linear_ramp_has_analytic_seven_point_gap(self):
        result = macd(range(1, 401))
        self.assertAlmostEqual(result["line"][-1], 7)
        self.assertAlmostEqual(result["signal"][-1], 7)
        self.assertAlmostEqual(result["histogram"][-1], 0)

    def test_prefix_is_unchanged_when_future_values_are_appended(self):
        prefix = [100 + (i % 9) for i in range(80)]
        before = macd(prefix)
        after = macd(prefix + [500, 200, 800])
        for key in before:
            self.assertEqual(before[key], after[key][:len(prefix)])

    def test_operational_study_requires_warmup(self):
        with self.assertRaises(CandleError):
            study(bars(249), 300, BASE + timedelta(days=2), 250)
        with self.assertRaises(CandleError):
            study(bars(100), 300, BASE + timedelta(days=2), 100)
        result = study(bars(400), 300, BASE + timedelta(days=2))
        self.assertEqual(result["rsi14_wilder_close"], 50)
        self.assertEqual(result["rsi24_wilder_close"], 50)
        self.assertEqual(result["atr14_rma"], 3)
        self.assertIsNone(result["latest_volume"])
        self.assertFalse(result["source_authenticated"])

    def test_invalid_indicator_inputs(self):
        for values in ([1, float("nan")], [True], [float("inf")]):
            with self.assertRaises(ValueError):
                ema(values, 2)
        for period in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                ema([1, 2], period)


class CandleValidationTests(unittest.TestCase):
    def validate(self, data):
        return validate_closed_bars(data, 300, BASE + timedelta(hours=1))

    def test_closed_contiguous_history_and_explicit_zero_volume(self):
        data = bars()
        data[-1]["volume"] = 0
        result = self.validate(data)
        self.assertEqual(result[-1].volume, 0)
        self.assertIsNone(result[0].volume)
        self.assertEqual(result[0].open_at.tzinfo, UTC)

    def test_reject_open_candle_even_with_closed_timestamp(self):
        data = bars()
        data[-1]["closed"] = False
        with self.assertRaises(CandleError):
            self.validate(data)

    def test_reject_future_closed_claim(self):
        with self.assertRaises(CandleError):
            validate_closed_bars(bars(), 300, BASE + timedelta(minutes=14))

    def test_reject_missing_ohlc(self):
        data = bars()
        del data[1]["low"]
        with self.assertRaises(CandleError):
            self.validate(data)

    def test_reject_gap_duplicate_and_reverse(self):
        data = bars(4)
        for malformed in ([data[0], data[2]], [data[0], data[0]], list(reversed(data))):
            with self.assertRaises(CandleError):
                self.validate(malformed)

    def test_reject_ohlc_nonfinite_and_negative_volume(self):
        for key, value in (("close", 103), ("low", 103), ("high", float("inf")),
                           ("open", 0), ("volume", -1), ("volume", True)):
            data = bars()
            data[1][key] = value
            with self.assertRaises(CandleError, msg=(key, value)):
                self.validate(data)

    def test_reject_naive_time_and_wrong_duration(self):
        data = bars()
        data[0]["open_at"] = "2026-11-02T00:00:00"
        with self.assertRaises(CandleError):
            self.validate(data)
        data = bars()
        data[0]["close_at"] = "2026-11-02T00:04:59.999Z"
        with self.assertRaises(CandleError):
            self.validate(data)

    def test_exchange_boundary_origin_is_preserved(self):
        data = bars()
        for item in data:
            for key in ("open_at", "close_at"):
                item[key] = (datetime.fromisoformat(item[key]) + timedelta(seconds=2)).isoformat()
        result = self.validate(data)
        self.assertEqual(result[0].open_at.second, 2)


if __name__ == "__main__":
    unittest.main()
