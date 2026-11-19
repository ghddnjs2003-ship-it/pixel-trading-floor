"""v7 룰 엔진 테스트 — 지표 정의, 피벗·상위봉 집계, 스윕 신호, 쿨타임, 미래 참조 금지."""

import unittest

from backend.strategy import (
    Engine, Level, Params, Zone, atr_series, bollinger_width_series, cci_series, ema_series,
    evaluate, pivots, resample, scan, sma_series, snapshot, _blank,
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

    def test_bollinger_width_is_percent_and_positive(self):
        cs = candles_from([100 + (i % 5) for i in range(40)])
        w = bollinger_width_series(cs, 20, 2.0)
        self.assertIsNone(w[10])
        self.assertGreater(w[-1], 0)

    def test_ema_seeds_with_sma(self):
        vals = [float(i) for i in range(30)]
        ema = ema_series(vals, 5)
        self.assertIsNone(ema[3])
        self.assertAlmostEqual(ema[4], sum(vals[:5]) / 5)

    def test_sma_keeps_none_when_window_has_gap(self):
        vals = [1.0, 2.0, None, 4.0, 5.0, 6.0]
        sma = sma_series(vals, 2)
        self.assertIsNone(sma[2])
        self.assertIsNone(sma[3])
        self.assertAlmostEqual(sma[5], (5.0 + 6.0) / 2)


class PivotTests(unittest.TestCase):
    def test_pivot_high_appears_on_confirm_bar(self):
        vals = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 9, 8, 7, 6, 5]
        ph = pivots(vals, 3, 3, "high")
        self.assertIsNone(ph[6])
        self.assertEqual(ph[12], 10)          # 피벗(9) + 오른쪽 3봉 뒤 = 12

    def test_plateau_yields_single_pivot(self):
        # 몸통 고점처럼 같은 값이 이어지는 계열 — 같은 값은 피벗 하나로만 잡힌다
        vals = [1, 2, 3, 5, 5, 5, 2, 1, 1, 1, 1]
        ph = pivots(vals, 2, 2, "high")
        got = [i for i, v in enumerate(ph) if v is not None]
        self.assertEqual(len(got), 1)         # 5,5,5가 늘어서도 피벗은 하나
        self.assertEqual(ph[got[0]], 5)

    def test_pivot_low_mirrors_high(self):
        vals = [10, 9, 8, 7, 6, 5, 4, 3, 4, 5, 6]
        pl = sorted(v for v in pivots(vals, 2, 2, "low") if v is not None)
        self.assertIn(3, pl)


class ResampleTests(unittest.TestCase):
    def test_four_15m_bars_make_one_hour(self):
        cs = candles_from([100 + i for i in range(8)])
        h = resample(cs, 60)
        self.assertEqual(len(h), 2)
        self.assertEqual(h[0]["t"] % 3_600_000, 0)
        self.assertEqual(h[0]["o"], cs[0]["o"])
        self.assertEqual(h[0]["c"], cs[3]["c"])
        self.assertEqual(h[0]["h"], max(c["h"] for c in cs[:4]))
        self.assertEqual(h[1]["v"], sum(c["v"] for c in cs[4:8]))


class SweepSignalTests(unittest.TestCase):
    """더블 S — 스윕 후 종가 복귀(거부)에서 신호가 서고, 손절은 스윕 극점 밖이다."""

    def _series(self):
        # 워밍업(EMA50) 60봉 + 30봉 하락 → 저점 95(피벗) → 반등 → 다시 95 아래 꼬리 스윕 후 종가 복귀
        prices = [110.0] * 60
        prices += [110 - i * 0.5 for i in range(30)]                # 110 → 95.5
        prices += [95.0, 96.0, 97.0, 98.0, 99.0, 100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        prices += [104.0, 103.0, 102.0, 101.0, 100.0, 99.0, 98.0, 97.0]
        prices += [96.0, 97.5]                                       # 마지막 봉: 95를 찍고 97.5 마감
        cs = candles_from(prices, volume=10.0, spread=0.4)
        cs[-2]["l"] = 94.6                                           # 스윕 꼬리
        return cs

    def test_rejection_fires_and_stop_sits_below_sweep(self):
        cs = self._series()
        p = Params(use_trend_filter=False, use_m1=False)
        sigs = Engine(p).run(cs)
        longs = [s for s in sigs if s["side"] == "LONG"]
        self.assertTrue(longs, "스윕 후 복귀에서 롱 신호가 서야 한다")
        sig = longs[0]
        self.assertEqual(sig["kind"], "DS")
        self.assertEqual(sig["label"], "거부")
        self.assertEqual(sig["index"], len(cs) - 2)
        self.assertLess(sig["stop"], cs[-2]["l"])
        self.assertGreater(sig["target"], sig["entry"])

    def test_no_signal_when_sweep_too_deep(self):
        cs = self._series()
        p = Params(use_trend_filter=False, use_m1=False, sweep_max_atr=0.10)
        sigs = Engine(p).run(cs)
        self.assertFalse([s for s in sigs if s["side"] == "LONG"])

    def test_consumed_level_is_not_reused(self):
        cs = self._series()
        p = Params(use_trend_filter=False, use_m1=False)
        sigs = Engine(p).run(cs)
        self.assertEqual(len([s for s in sigs if s["side"] == "LONG"]), 1)


class StructuralOptionTests(unittest.TestCase):
    """2026-11-19 구조 재설계 옵션 — ATR 고정 손절, 라벨·변동성 선별, 목표 기준, 방향 제한."""

    def _series(self):
        prices = [110.0] * 60
        prices += [110 - i * 0.5 for i in range(30)]
        prices += [95.0, 96.0, 97.0, 98.0, 99.0, 100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        prices += [104.0, 103.0, 102.0, 101.0, 100.0, 99.0, 98.0, 97.0]
        prices += [96.0, 97.5]
        cs = candles_from(prices, volume=10.0, spread=0.4)
        cs[-2]["l"] = 94.6
        return cs

    def test_atr_stop_mode_replaces_structure_stop(self):
        cs = self._series()
        base = dict(use_trend_filter=False, use_m1=False)
        struct = [s for s in Engine(Params(**base)).run(cs) if s["kind"] == "DS"][0]
        atr = [s for s in Engine(Params(stop_mode="atr", stop_atr=2.0, **base)).run(cs)
               if s["kind"] == "DS"][0]
        self.assertIsNone(struct["stop_atr"])
        self.assertEqual(atr["stop_atr"], 2.0)
        self.assertAlmostEqual(atr["stop"], atr["entry"] - 2.0 * atr["atr"])
        self.assertNotAlmostEqual(struct["stop"], atr["stop"], places=4)

    def test_empty_label_selection_suppresses_double_s(self):
        cs = self._series()
        sigs = Engine(Params(use_trend_filter=False, use_m1=False, ds_labels=())).run(cs)
        self.assertFalse([s for s in sigs if s["kind"] == "DS"])

    def test_volatility_band_can_switch_everything_off(self):
        cs = self._series()
        sigs = Engine(Params(min_atr_pct=50.0)).run(cs)
        self.assertEqual(sigs, [])

    def test_target_zone_uses_opposite_1h_zone(self):
        cs = candles_from([100.0] * 10, spread=1.0)
        eng = Engine(Params(stop_mode="atr", stop_atr=0.5, ds_target="zone", min_rr=1.5))
        level = Level(price=99.0, born=0, pivot_bar=0)
        res = [Zone(top=103.0, bot=101.0, mid=102.0, is_res=True, born=0)]
        sig = eng._ds_signal(cs, 5, "LONG", "거부", 98.0, 1.0, level, [], [], "SSL", True, res, [])
        self.assertAlmostEqual(sig["target"], 101.0)      # 반대편 존 하단
        self.assertAlmostEqual(sig["stop"], 99.5)         # ATR 0.5배

    def test_target_liquidity_uses_opposite_level(self):
        cs = candles_from([100.0] * 10, spread=1.0)
        eng = Engine(Params(stop_mode="atr", stop_atr=0.5, ds_target="liq", min_rr=1.5))
        level = Level(price=99.0, born=0, pivot_bar=0)
        bsl = [Level(price=104.0, born=0, pivot_bar=0)]
        sig = eng._ds_signal(cs, 5, "LONG", "거부", 98.0, 1.0, level, bsl, [], "SSL", True, [], [])
        self.assertAlmostEqual(sig["target"], 104.0)

    def test_allow_sides_applies_to_m1_too(self):
        cs = candles_from([100.0] * 40, spread=1.0)
        state = {"bot": 98.0, "lvl": 101.0}
        res = [Zone(top=103.0, bot=101.0, mid=102.0, is_res=True, born=0)]
        short_only = Engine(Params(allow_sides=("SHORT",)))
        self.assertIsNone(short_only._m1_signal(cs, 30, "LONG", True, state, res, [], 1.0, False, 101.0))
        both = Engine(Params(allow_sides=("LONG", "SHORT")))
        sig = both._m1_signal(cs, 30, "LONG", True, state, res, [], 1.0, False, 101.0)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["side"], "LONG")


class ApiTests(unittest.TestCase):
    def test_blank_has_full_shape(self):
        b = _blank("이유")
        for k in ("side", "kind", "grade", "index", "entry", "stop", "target", "rr", "reasons"):
            self.assertIn(k, b)

    def test_evaluate_needs_bars(self):
        sig = evaluate(candles_from([100, 101, 99]), 2)
        self.assertIsNone(sig["side"])
        self.assertTrue(sig["reasons"])

    def test_scan_never_reads_future_bars(self):
        prices = [100 + (i % 7) + (0.4 if i % 3 == 0 else 0) for i in range(160)]
        cs = candles_from(prices, volume=20.0)
        full = scan(cs, Params())
        cut = scan(cs[:-5], Params())
        key = lambda s: (s["index"], s["side"], s["kind"])
        self.assertEqual([key(s) for s in cut], [key(s) for s in full if s["index"] < len(cs) - 5])

    def test_snapshot_reports_levels_and_zones(self):
        cs = candles_from([100 + (i % 9) for i in range(300)], volume=15.0)
        snap = snapshot(cs, Params())
        for k in ("bsl", "ssl", "res", "sup", "last_signals"):
            self.assertIn(k, snap)


if __name__ == "__main__":
    unittest.main()
