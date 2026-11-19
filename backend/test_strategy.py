"""v7 룰 엔진 단위 테스트 — 지표 정의, 신호 조건, 미래 참조 금지."""

import unittest

from backend.strategy import (
    Params, atr_series, bollinger_series, cci_series, evaluate, scan, swing_levels,
    volume_zones, _mds_m1, _make_signal, _blank,
)


def candles_from(prices, volume=10.0, spread=1.0):
    """종가 목록 → OHLC 봉 목록. 시작 시각은 15분 간격."""
    out = []
    prev = prices[0]
    for i, c in enumerate(prices):
        o = prev
        h = max(o, c) + spread / 2
        l = min(o, c) - spread / 2
        out.append({"t": 1_700_000_000_000 + i * 900_000, "o": o, "h": h, "l": l, "c": c, "v": volume})
        prev = c
    return out


class IndicatorTests(unittest.TestCase):
    def test_atr_is_positive_and_matches_range_for_flat_bars(self):
        cs = [{"t": i, "o": 100, "h": 101, "l": 99, "c": 100, "v": 1} for i in range(30)]
        atr = atr_series(cs, 14)
        self.assertIsNone(atr[10])
        self.assertAlmostEqual(atr[-1], 2.0, places=6)

    def test_cci_positive_when_price_rises(self):
        cs = candles_from([100 + i for i in range(40)])
        cci = cci_series(cs, 20)
        self.assertIsNone(cci[10])
        self.assertGreater(cci[-1], 0)

    def test_bollinger_brackets_middle(self):
        cs = candles_from([100 + (i % 5) for i in range(40)])
        bb = bollinger_series(cs, 20, 2.0)
        mid, upper, lower = bb[-1]
        self.assertLess(lower, mid)
        self.assertGreater(upper, mid)

    def test_swing_levels_split_above_and_below_price(self):
        prices = [100, 105, 110, 105, 100, 95, 100, 105, 106]
        cs = candles_from(prices, spread=2)
        highs, lows = swing_levels(cs, len(cs) - 1, lookback=24, keep=5)
        self.assertTrue(all(h >= cs[-1]["c"] for h in highs))
        self.assertTrue(all(l <= cs[-1]["c"] for l in lows))


class ZoneTests(unittest.TestCase):
    def test_volume_zone_lands_on_busy_price_band(self):
        prices = [100 + (i % 5) for i in range(80)] + [102] * 80
        cs = candles_from(prices, volume=5.0)
        zones = volume_zones(cs, len(cs) - 1, Params())
        self.assertTrue(zones)
        top = zones[0]
        self.assertLessEqual(top["low"], 107)
        self.assertGreaterEqual(top["high"], 98)
        self.assertGreater(top["share"], 0)


class SignalTests(unittest.TestCase):
    def test_evaluate_returns_no_side_when_bars_are_missing(self):
        sig = evaluate(candles_from([100, 101, 99]), 2)
        self.assertIsNone(sig["side"])
        self.assertTrue(sig["reasons"])

    def test_m1_long_after_volume_breakout_and_first_retest(self):
        params = Params(zone_lookback=120, zone_min_age=4, bb_expand=False, cci_mode="off")
        # 80봉은 100~110 박스, 그 뒤 105에서 115로 거래량 실린 돌파, 마지막 봉은 110 지지 확인
        prices = [100 + (i % 5) * 2 for i in range(80)]      # 100,102,104,106,108 반복
        cs = candles_from(prices, volume=10.0)
        cs.append({"t": cs[-1]["t"] + 900_000, "o": 105, "h": 116, "l": 104, "c": 115, "v": 40.0})
        cs.append({"t": cs[-1]["t"] + 900_000, "o": 114, "h": 114.5, "l": 107, "c": 112, "v": 8.0})
        self.assertEqual(len(cs), 82)

        sig = _mds_m1(cs, 81, params, atr=1.5, trend_up=True, trend_down=False)
        self.assertEqual(sig["side"], "LONG")
        self.assertEqual(sig["kind"], "M1")
        self.assertLess(sig["stop"], sig["entry"])
        self.assertGreaterEqual(sig["rr"], params.min_rr)

    def test_make_signal_falls_back_to_min_rr_target(self):
        sig = _make_signal("LONG", "DS", "거부", 10, 100.0, 99.0, [], ["테스트"], 2.0, Params(), 0.5, False)
        self.assertAlmostEqual(sig["target"], 101.5, places=6)
        self.assertEqual(sig["rr"], 1.5)

    def test_scan_never_reads_future_bars(self):
        prices = [100 + (i % 7) for i in range(120)]
        cs = candles_from(prices, volume=20.0)
        full = scan(cs, Params())
        # 마지막 봉을 잘라도 그 앞 신호는 그대로여야 한다(미래 참조 없음)
        cut = scan(cs[:-3], Params())
        key = lambda s: (s["index"], s["side"], s["kind"])
        self.assertEqual([key(s) for s in cut], [key(s) for s in full if s["index"] < len(cs) - 3])

    def test_blank_has_full_shape(self):
        b = _blank("이유")
        for k in ("side", "kind", "grade", "index", "entry", "stop", "target", "rr", "reasons"):
            self.assertIn(k, b)


if __name__ == "__main__":
    unittest.main()
