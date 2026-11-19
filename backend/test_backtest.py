"""백테스트 체결 모형 테스트 — 다음 봉 시가 진입, 손절 우선, 수수료·슬리피지 반영."""

import unittest

from backend.backtest import BacktestConfig, simulate, summarize


def bar(t, o, h, l, c, v=10.0):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


class SimulateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = BacktestConfig(initial_equity=1_000.0, risk_pct=1.0, fee_bps=0.0, slippage_bps=0.0, max_leverage=100)

    def test_enters_at_next_open_and_exits_at_target(self):
        candles = [bar(1, 100, 101, 99, 100), bar(2, 100, 101, 99, 100),
                   bar(3, 100.5, 103, 100, 102), bar(4, 102, 104, 101, 103)]
        sig = [{"index": 1, "side": "LONG", "kind": "DS", "grade": "A", "entry": 100, "stop": 99,
                "target": 102.5, "rr": 2.5, "reasons": ["테스트"]}]
        out = simulate(candles, sig, self.cfg)
        self.assertEqual(len(out["trades"]), 1)
        trade = out["trades"][0]
        self.assertEqual(trade["entry"], 100.5)      # 신호 다음 봉(3번) 시가
        self.assertEqual(trade["reason"], "TARGET")
        self.assertEqual(trade["exit"], 102.5)
        self.assertGreater(trade["pnl"], 0)
        # 리스크 1% = 10 USDT, 손절 폭 |100.5-99| = 1.5 → 수량 6.667, 목표까지 2.0 → 약 +13.33
        self.assertAlmostEqual(out["final_equity"], 1000.0 + 10.0 / 1.5 * 2.0, places=1)

    def test_stop_wins_when_both_touched_in_same_bar(self):
        candles = [bar(1, 100, 101, 99, 100), bar(2, 100, 101, 99, 100),
                   bar(3, 100, 105, 95, 100)]
        sig = [{"index": 1, "side": "LONG", "kind": "M1", "grade": "B", "entry": 100, "stop": 98,
                "target": 104, "rr": 2.0, "reasons": []}]
        out = simulate(candles, sig, self.cfg)
        self.assertEqual(out["trades"][0]["reason"], "STOP")
        self.assertLess(out["trades"][0]["pnl"], 0)

    def test_short_target_and_fees_reduce_pnl(self):
        cfg = BacktestConfig(initial_equity=1_000.0, risk_pct=1.0, fee_bps=5.0, slippage_bps=2.0, max_leverage=100)
        candles = [bar(1, 100, 101, 99, 100), bar(2, 100, 101, 99, 100),
                   bar(3, 100, 100, 97, 98)]
        sig = [{"index": 1, "side": "SHORT", "kind": "DS", "grade": "A", "entry": 100, "stop": 101,
                "target": 98, "rr": 2.0, "reasons": []}]
        out = simulate(candles, sig, cfg)
        trade = out["trades"][0]
        self.assertEqual(trade["reason"], "TARGET")
        gross = (100.0 * (1 - 0.0002) - 98.0 * (1 + 0.0002)) * trade["qty"]
        self.assertLess(trade["pnl"], gross)          # 수수료만큼 줄어야 한다
        self.assertGreater(trade["pnl"], 0)

    def test_signal_skipped_when_gap_already_past_stop(self):
        candles = [bar(1, 100, 101, 99, 100), bar(2, 97, 98, 96, 97), bar(3, 96, 97, 95, 96)]
        sig = [{"index": 1, "side": "LONG", "kind": "DS", "grade": "A", "entry": 100, "stop": 98,
                "target": 104, "rr": 2.0, "reasons": []}]
        out = simulate(candles, sig, self.cfg)
        self.assertEqual(len(out["trades"]), 0)
        self.assertEqual(out["stats"]["skipped"]["리스크초과"], 1)

    def test_one_position_at_a_time(self):
        candles = [bar(i, 100, 101, 99, 100) for i in range(1, 8)]
        sig = [{"index": 1, "side": "LONG", "kind": "DS", "grade": "A", "entry": 100, "stop": 90,
                "target": 130, "rr": 3.0, "reasons": []},
               {"index": 3, "side": "LONG", "kind": "DS", "grade": "A", "entry": 100, "stop": 90,
                "target": 130, "rr": 3.0, "reasons": []}]
        out = simulate(candles, sig, self.cfg)
        self.assertEqual(len(out["trades"]), 0)        # 아직 청산 전 — 열린 채로 끝난다


class StructuralExitTests(unittest.TestCase):
    """2026-11-19 구조 재설계 옵션 — 세션 필터, ATR 고정 손절, 추적 청산, 목표 상한, 시간 손절."""

    def setUp(self):
        self.cfg = BacktestConfig(initial_equity=1_000.0, risk_pct=1.0, fee_bps=0.0,
                                  slippage_bps=0.0, max_leverage=100)

    def _sig(self, **kw):
        base = {"index": 1, "side": "LONG", "kind": "M1", "grade": "A", "entry": 100.0,
                "stop": 99.0, "target": 104.0, "rr": 4.0, "atr": 1.0, "reasons": []}
        base.update(kw)
        return base

    def test_session_filter_blocks_entry_outside_hours(self):
        # 봉 시각 1_700_000_000_000 = UTC 00:13. 세션을 (12,16)으로 두면 진입이 막힌다.
        candles = [bar(1, 100, 101, 99, 100), bar(2, 100, 101, 99, 100), bar(3, 100, 103, 99, 102)]
        blocked = simulate(candles, [self._sig()], BacktestConfig(**{**self.cfg.to_dict(),
                                                                     "sessions": ((12, 16),)}))
        self.assertEqual(len(blocked["trades"]), 0)
        self.assertEqual(blocked["stats"]["skipped"]["세션제외"], 1)
        allowed = simulate(candles, [self._sig()], BacktestConfig(**{**self.cfg.to_dict(),
                                                                    "sessions": ((0, 4),)}))
        self.assertEqual(len(allowed["trades"]), 1)

    def test_atr_stop_is_measured_from_real_entry(self):
        # 신호 종가 100, 다음 봉 시가 100.5, ATR 1.0 · 2배 → 손절 98.5 (신호가 99가 아니라)
        candles = [bar(1, 100, 101, 99, 100), bar(2, 100.5, 101, 100, 100.5), bar(3, 100.5, 101, 98.0, 98.4)]
        sig = self._sig(stop=99.0, stop_atr=2.0, target=110.0)
        out = simulate(candles, [sig], self.cfg)
        t = out["trades"][0]
        self.assertEqual(t["entry"], 100.5)
        self.assertAlmostEqual(t["stop"], 98.5, places=6)
        self.assertEqual(t["reason"], "STOP")

    def test_trail_exit_follows_peak(self):
        cfg = BacktestConfig(**{**self.cfg.to_dict(), "exit_mode": "trail", "trail_atr": 2.0})
        candles = [bar(1, 100, 101, 99, 100), bar(2, 100, 101, 99, 100),
                   bar(3, 100, 104, 102.5, 103), bar(4, 103, 103.5, 101.5, 101.8)]
        out = simulate(candles, [self._sig(target=200.0, stop_atr=1.0)], cfg)
        t = out["trades"][0]
        self.assertEqual(t["reason"], "TRAIL")
        self.assertAlmostEqual(t["exit"], 102.0, places=6)   # 고점 104 − 2 × ATR 1
        self.assertGreater(t["pnl"], 0)

    def test_target_cap_trims_structure_target(self):
        cfg = BacktestConfig(**{**self.cfg.to_dict(), "target_cap_r": 2.0})
        candles = [bar(1, 100, 101, 99, 100), bar(2, 100, 101, 99, 100), bar(3, 100, 103, 99.5, 102.5)]
        out = simulate(candles, [self._sig(stop_atr=1.0, target=140.0)], cfg)
        t = out["trades"][0]
        self.assertEqual(t["reason"], "TARGET")
        self.assertAlmostEqual(t["exit"], 102.0, places=6)

    def test_time_stop_closes_at_close(self):
        cfg = BacktestConfig(**{**self.cfg.to_dict(), "max_bars": 2})
        candles = [bar(1, 100, 101, 99, 100), bar(2, 100, 101, 99, 100),
                   bar(3, 100, 100.5, 99.5, 100.2), bar(4, 100.2, 100.4, 99.8, 100.1),
                   bar(5, 100.1, 100.3, 99.9, 100.0)]
        out = simulate(candles, [self._sig(stop_atr=1.0, target=200.0)], cfg)
        t = out["trades"][0]
        self.assertEqual(t["reason"], "TIME")
        self.assertEqual(t["bars"], 2)


class SummaryTests(unittest.TestCase):
    def test_summary_math(self):
        cfg = BacktestConfig(initial_equity=1_000.0)
        trades = [
            {"pnl": 20.0, "r": 2.0, "equity": 1020.0, "kind": "DS", "bars": 4},
            {"pnl": -10.0, "r": -1.0, "equity": 1010.0, "kind": "DS", "bars": 6},
            {"pnl": -10.0, "r": -1.0, "equity": 1000.0, "kind": "M1", "bars": 3},
            {"pnl": 30.0, "r": 3.0, "equity": 1030.0, "kind": "M1", "bars": 5},
        ]
        s = summarize(trades, cfg, 1030.0)
        self.assertEqual(s["trades"], 4)
        self.assertEqual(s["win_rate_pct"], 50.0)
        self.assertEqual(s["profit_factor"], 2.5)
        self.assertEqual(s["max_losing_streak"], 2)
        self.assertEqual(s["max_drawdown_pct"], 1.96)
        self.assertEqual(s["by_kind"]["DS"]["trades"], 2)
        self.assertEqual(s["by_kind"]["M1"]["win_rate_pct"], 50.0)
        self.assertIn("monthly", s)
        self.assertIn("by_side", s)

    def test_empty_summary_does_not_invent_numbers(self):
        s = summarize([], BacktestConfig(), 1_000.0)
        self.assertEqual(s["trades"], 0)
        self.assertIsNone(s["win_rate_pct"])
        self.assertIsNone(s["avg_r"])


if __name__ == "__main__":
    unittest.main()
