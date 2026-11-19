"""MTF(다중 시간봉) 고정 테스트 — '완성된 상위 봉만 본다'와 손절·목표 규칙을 못 박는다.

고정하는 것
1. LTF 봉 i가 보는 HTF 봉은 **i 시작 시각까지 끝난 봉**뿐이다(경계 포함, 미래 참조 없음).
2. 데이터를 잘라도 그 앞 신호는 그대로다(미래 봉을 읽지 않는다는 실행 가능한 증거).
3. HTF 편향과 반대 방향 신호는 버리고, 같은 방향만 남긴다.
4. 손절: LTF ATR 배수 / HTF ATR 배수(`stop_dist`) / HTF 매물대 바깥 — 각각이 실제 체결 모형에 먹힌다.
5. 목표: R 배수는 **실제 진입가·실제 손절폭** 기준이다.
6. 돌파 편향은 반대 돌파가 나올 때까지 유지된다.
7. 격자 프리셋 수·`tradable_signals`(체결된 신호만) 동작.
"""

import json
import os
import unittest

from .ai_filter import tradable_signals
from .backtest import BacktestConfig, simulate
from .lab import preset_long_tf, preset_short_tf
from .mtf import MTFParams, frame_key, htf_frame, mtf_signals
from .strategy import Params

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "data", "btc_15m_3600.json")
BASE_MS = 1_600_000_000_000 - (1_600_000_000_000 % 900_000)
TF_MS = 900_000


def candles_fixture():
    with open(FIXTURE, encoding="utf-8") as fh:
        c = json.load(fh)
    c.sort(key=lambda x: x["t"])
    return c


def synth(n=1200, drift=0.0012, wiggle=0.25, start=100.0, tf_ms=TF_MS):
    """결정적 합성 봉 — 추세·꼬리·거래량이 규칙적이라 편향 검사에 쓴다."""
    out = []
    price = start
    for i in range(n):
        o = price
        c = o * (1 + drift) + wiggle * ((i % 9) - 4) / 4.0
        h = max(o, c) + wiggle * (0.3 + (i % 3) / 10)
        l = min(o, c) - wiggle * (0.3 + (i % 4) / 10)
        out.append({"t": BASE_MS + i * tf_ms, "o": o, "h": h, "l": l, "c": c, "v": 100 + (i % 17)})
        price = c
    return out


def fake_signal(candles, i, side="LONG", kind="M1"):
    c = candles[i]
    return {"side": side, "kind": kind, "grade": "A", "index": i, "time": c["t"],
            "entry": c["c"], "stop": c["c"] * (0.99 if side == "LONG" else 1.01),
            "target": c["c"] * (1.03 if side == "LONG" else 0.97), "rr": 3.0,
            "risk": c["c"] * 0.01, "atr": c["c"] * 0.002, "stop_atr": None,
            "reasons": []}


class ClosedBarTests(unittest.TestCase):
    def test_closed_index_is_last_finished_htf_bar(self):
        c = candles_fixture()
        mp = MTFParams(htf_minutes=60)
        fr = htf_frame(c, mp)
        htf = fr["htf"]
        for i in (0, 1, 4, 5, 100, len(c) // 2, len(c) - 1):
            k = fr["closed"][i]
            if k < 0:
                self.assertLess(c[i]["t"], htf[0]["t"] + 3_600_000)
                continue
            # 그 HTF 봉은 i 시작 전에 끝났다
            self.assertLessEqual(htf[k]["t"] + 3_600_000, c[i]["t"])
            # 다음 HTF 봉은 아직 끝나지 않았다
            if k + 1 < len(htf):
                self.assertGreater(htf[k + 1]["t"] + 3_600_000, c[i]["t"])

    def test_htf_bar_closing_exactly_at_bar_time_counts(self):
        c = candles_fixture()
        mp = MTFParams(htf_minutes=240)
        fr = htf_frame(c, mp)
        for i, bar in enumerate(c):
            k = fr["closed"][i]
            if k >= 0 and fr["htf"][k]["t"] + 240 * 60_000 == bar["t"]:
                self.assertTrue(bar["t"] % (240 * 60_000) == 0)
                self.assertEqual(fr["closed"][i - 1], k - 1)   # 직전 봉은 아직 그 봉을 못 본다
                break

    def test_zones_are_confirmed_only_after_right_bars(self):
        c = candles_fixture()
        mp = MTFParams(htf_minutes=240, htf_piv=5)
        fr = htf_frame(c, mp)
        for k, z in enumerate(fr["sup"]):
            if z is not None:
                self.assertLessEqual(z["born"], k)
        # 피벗 확인 자체가 오른쪽 5봉을 기다린다: born == pivot bar + 5
        self.assertTrue(any(z is not None for z in fr["sup"]))


class NoFutureTests(unittest.TestCase):
    def test_truncated_candles_give_same_earlier_signals(self):
        c = candles_fixture()
        mp = MTFParams(htf_minutes=60, htf_mode="ema", allow_kinds=("M1", "DS"),
                       stop_mode="htf_atr", stop_atr=1.5)
        full = mtf_signals(c, mp)
        self.assertTrue(full, "픽스처에서 MTF 신호가 하나도 없다 — 테스트 전제 확인 필요")
        cut = len(c) // 2
        part = mtf_signals(c[:cut], mp)
        a = [(s["index"], s["side"], round(s["stop"], 4)) for s in full if s["index"] < cut - 1]
        b = [(s["index"], s["side"], round(s["stop"], 4)) for s in part if s["index"] < cut - 1]
        self.assertEqual(a, b)


class BiasTests(unittest.TestCase):
    def test_counter_trend_signals_are_dropped(self):
        c = synth(n=1400, drift=0.0012)          # 꾸준한 상승
        mp = MTFParams(htf_minutes=240, htf_mode="ema", allow_kinds=("M1",))
        fr = htf_frame(c, mp)
        last = fr["bias"][fr["closed"][-1]]
        self.assertEqual(last, 1)
        base = [fake_signal(c, i, "LONG") for i in range(900, 1300, 7)]
        base += [fake_signal(c, i, "SHORT") for i in range(900, 1300, 7)]
        out = mtf_signals(c, mp, base=base, frame=fr)
        self.assertTrue(out)
        self.assertTrue(all(s["side"] == "LONG" and s["htf_bias"] == 1 for s in out))

    def test_breakout_state_persists_until_opposite_break(self):
        c = synth(n=1400, drift=0.0015)
        mp = MTFParams(htf_minutes=240, htf_mode="breakout", breakout_lookback=20)
        fr = htf_frame(c, mp)
        bias = [b for k, b in enumerate(fr["bias"]) if k > 60]
        self.assertEqual(bias[-1], 1)
        flips = sum(1 for a, b in zip(bias, bias[1:]) if a != b)
        self.assertLess(flips, len(bias) // 4)    # 매 봉 새로 판정하지 않고 상태를 들고 간다

    def test_unknown_mode_raises(self):
        c = synth(n=400)
        with self.assertRaises(ValueError):
            htf_frame(c, MTFParams(htf_minutes=60, htf_mode="nope"))


class StopTargetTests(unittest.TestCase):
    def test_htf_atr_stop_distance(self):
        c = candles_fixture()
        mp = MTFParams(htf_minutes=240, htf_mode="ema", allow_kinds=("M1", "DS"),
                       stop_mode="htf_atr", stop_atr=1.5)
        fr = htf_frame(c, mp)
        sigs = mtf_signals(c, mp)
        self.assertTrue(sigs)
        for s in sigs[:20]:
            hatr = fr["atr"][fr["closed"][s["index"]]]
            self.assertAlmostEqual(s["stop_dist"], 1.5 * hatr, places=6)
            self.assertIsNone(s["stop_atr"])

    def test_ltf_atr_stop_uses_ltf_atr(self):
        c = candles_fixture()
        mp = MTFParams(htf_minutes=60, htf_mode="ema", allow_kinds=("M1", "DS"),
                       stop_mode="ltf_atr", stop_atr=3.0)
        sigs = mtf_signals(c, mp)
        self.assertTrue(sigs)
        self.assertTrue(all(s["stop_atr"] == 3.0 for s in sigs))
        self.assertTrue(all("stop_dist" not in s for s in sigs))

    def test_htf_zone_stop_sits_outside_the_zone(self):
        c = candles_fixture()
        mp = MTFParams(htf_minutes=60, htf_mode="ema", allow_kinds=("M1", "DS"),
                       stop_mode="htf_zone", stop_buf_atr=0.1)
        fr = htf_frame(c, mp)
        sigs = mtf_signals(c, mp)
        self.assertTrue(sigs)
        for s in sigs[:20]:
            k = fr["closed"][s["index"]]
            if s["side"] == "LONG":
                z = fr["sup"][k]
                self.assertLess(s["stop"], z["bot"])
            else:
                z = fr["res"][k]
                self.assertGreater(s["stop"], z["top"])

    def test_simulate_uses_stop_dist_from_actual_entry(self):
        c = candles_fixture()
        i = 300
        sig = fake_signal(c, i, "LONG")
        dist = c[i]["c"] * 0.01
        sig["stop_dist"] = dist
        sig["stop_atr"] = None
        res = simulate(c, [sig], BacktestConfig(allow_kinds=("M1",), target_mode="r", target_r=2.0))
        self.assertEqual(len(res["trades"]), 1)
        t = res["trades"][0]
        entry = c[i + 1]["o"] * (1 + 2.0 / 10_000)      # 다음 봉 시가 + 슬리피지
        self.assertAlmostEqual(t["entry"], entry, places=2)
        self.assertAlmostEqual(t["stop"], entry - dist, delta=0.01)
        self.assertAlmostEqual(t["target"], entry + 2 * dist, delta=0.01)

    def test_target_r_multiple_measured_from_real_risk(self):
        c = candles_fixture()
        sigs = mtf_signals(c, MTFParams(htf_minutes=240, htf_mode="ema", allow_kinds=("M1", "DS"),
                                        stop_mode="htf_atr", stop_atr=1.5))
        res = simulate(c, sigs, BacktestConfig(allow_kinds=("M1", "DS"), target_mode="r", target_r=4.0))
        self.assertTrue(res["trades"])
        for t in res["trades"][:30]:
            risk = abs(t["entry"] - t["stop"])
            self.assertAlmostEqual(abs(t["target"] - t["entry"]) / risk, 4.0, places=4)


class GridAndFilterTests(unittest.TestCase):
    def test_preset_sizes(self):
        self.assertEqual(len(preset_short_tf(5)), 3 * 4 * 6 * 6 * 3)
        self.assertEqual(len(preset_short_tf(15)), 3 * 4 * 6 * 6 * 3)
        self.assertEqual(len(preset_long_tf()), 3 * 4 * 9 * 3)

    def test_no_narrow_stops_in_short_presets(self):
        for spec in preset_short_tf(5):
            self.assertGreaterEqual(spec["p"].get("stop_atr", 99), 4.0)
        for spec in preset_short_tf(15):
            self.assertGreaterEqual(spec["p"].get("stop_atr", 99), 2.0)

    def test_frame_key_separates_htf_modes(self):
        a, b = MTFParams(htf_mode="ema"), MTFParams(htf_mode="zone")
        self.assertNotEqual(frame_key(a), frame_key(b))

    def test_tradable_signals_are_the_filled_ones(self):
        c = candles_fixture()
        mp = MTFParams(htf_minutes=60, htf_mode="ema", allow_kinds=("M1", "DS"),
                       stop_mode="htf_atr", stop_atr=1.5)
        sigs = mtf_signals(c, mp)
        cfg = BacktestConfig(allow_kinds=("M1", "DS"), target_mode="r", target_r=3.0)
        traded = tradable_signals(c, sigs, cfg, Params())
        self.assertTrue(traded)
        self.assertLess(len(traded), len(sigs))               # 보유 중 중복 신호는 빠진다
        self.assertEqual(len(traded), len(simulate(c, sigs, cfg, Params())["trades"]))
        self.assertTrue(all(s in sigs for s in traded))


if __name__ == "__main__":
    unittest.main()
