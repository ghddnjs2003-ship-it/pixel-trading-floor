"""AI 맥락 필터 테스트 — 응답 해석, 문맥(미래 참조 금지), 캐시, 오류 정책, 무작위 대조군.

실제 Gemini 호출은 하지 않는다(가짜 제공자). 네트워크·키 없이 항상 돈다.
"""

import json
import os
import tempfile
import unittest

from backend.ai_filter import (AIFilter, FilterConfig, build_context, decide, parse_decision,
                               prepare_series, random_control, render_prompt, review_signals,
                               select_signals)
from backend.backtest import BacktestConfig
from backend.providers import ProviderError
from backend.strategy import Params


def candles_from(prices, volume=10.0, spread=1.0, step_ms=900_000):
    out = []
    prev = prices[0]
    for i, c in enumerate(prices):
        o = prev
        out.append({"t": 1_700_000_000_000 + i * step_ms, "o": o, "h": max(o, c) + spread / 2,
                    "l": min(o, c) - spread / 2, "c": c, "v": volume})
        prev = c
    return out


def zigzag(n=220, base=30000.0, amp=120.0, step=40.0):
    """추세 + 되돌림이 섞인 가격 — 지표가 None이 아니게 만들 만큼만 길면 된다."""
    prices = []
    for i in range(n):
        drift = step * (i - n / 2) / n * 4
        wave = amp * (1 if (i // 6) % 2 == 0 else -1)
        prices.append(base + drift + wave)
    return prices


def m1_signal(candles, side="LONG", index=None):
    i = len(candles) - 2 if index is None else index
    bar = candles[i]
    entry = bar["c"]
    stop = entry - 90.0 if side == "LONG" else entry + 90.0
    target = entry + 360.0 if side == "LONG" else entry - 360.0
    return {"side": side, "kind": "M1", "label": "M1 롱" if side == "LONG" else "M1 숏",
            "grade": "A", "index": i, "time": bar["t"], "entry": entry, "stop": stop,
            "target": target, "rr": 4.0, "risk": 90.0, "atr": 30.0, "stop_atr": 3.0,
            "level": entry - 30.0, "reasons": ["테스트"]}


class FakeProvider:
    """프롬프트별 응답을 돌려주는 가짜 제공자. 부른 횟수를 센다."""

    def __init__(self, loss_prob=45, raw=None):
        self.loss_prob = loss_prob
        self.raw = raw
        self.prompts = []
        self.config = {"model": "fake-model"}

    def complete(self, system_prompt, user_prompt):
        self.prompts.append((system_prompt, user_prompt))
        if self.raw is not None:
            return self.raw
        return json.dumps({"loss_prob": self.loss_prob, "decision": "PASS",
                           "reason": "테스트 근거", "risk_flags": []}, ensure_ascii=False)


class DecisionTests(unittest.TestCase):
    def test_parses_plain_json(self):
        d = parse_decision('{"decision":"REJECT","reason":"저항 1R","confidence":80,"risk_flags":["zone"]}')
        self.assertEqual(d["decision"], "REJECT")
        self.assertEqual(d["reason"], "저항 1R")
        self.assertEqual(d["confidence"], 80)
        self.assertEqual(d["risk_flags"], ["zone"])

    def test_strips_code_fence_and_aliases(self):
        self.assertEqual(parse_decision('```json\n{"decision":"block"}\n```')["decision"], "REJECT")
        self.assertEqual(parse_decision('설명\n{"decision":"ALLOW"}\n끝')["decision"], "PASS")

    def test_loss_prob_beats_model_decision_and_uses_threshold(self):
        raw = '{"loss_prob":62,"decision":"PASS"}'
        self.assertEqual(parse_decision(raw, 60)["decision"], "REJECT")
        self.assertEqual(parse_decision(raw, 70)["decision"], "PASS")
        self.assertEqual(parse_decision(raw)["loss_prob"], 62.0)

    def test_loss_prob_is_clamped(self):
        self.assertEqual(parse_decision('{"loss_prob":140}')["loss_prob"], 100.0)
        self.assertEqual(parse_decision('{"loss_prob":-3}')["loss_prob"], 0.0)

    def test_invalid_decision_raises(self):
        with self.assertRaises(ValueError):
            parse_decision('{"decision":"MOON"}')
        with self.assertRaises(ValueError):
            parse_decision("그냥 문장")

    def test_decide_uses_probability_when_present(self):
        self.assertEqual(decide({"loss_prob": 65, "decision": "PASS"}, 60), "REJECT")
        self.assertEqual(decide({"loss_prob": 65, "decision": "PASS"}, 70), "PASS")
        self.assertEqual(decide({"decision": "REJECT"}, 60), "REJECT")


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.candles = candles_from(zigzag())
        self.series = prepare_series(self.candles)
        self.sig = m1_signal(self.candles)

    def test_context_is_serializable_without_nan(self):
        ctx = build_context(self.candles, self.sig, None, self.series)
        json.dumps(ctx, allow_nan=False, ensure_ascii=False)
        self.assertEqual(ctx["후보"]["방향"], "LONG")
        self.assertIn("최근봉", ctx)

    def test_context_never_reads_future_bars(self):
        """같은 신호 봉까지의 자료면, 뒤 봉을 통째로 바꿔도 문맥이 같아야 한다."""
        i = self.sig["index"]
        base = build_context(self.candles[:i + 1], self.sig, None, prepare_series(self.candles[:i + 1]))
        altered = [dict(c) for c in self.candles]
        for k in range(i + 1, len(altered)):
            altered[k] = {**altered[k], "o": 1.0, "h": 99999.0, "l": 1.0, "c": 2.0, "v": 1e9}
        after = build_context(altered, self.sig, None, prepare_series(altered))
        self.assertEqual(json.dumps(base, sort_keys=True, ensure_ascii=False),
                         json.dumps(after, sort_keys=True, ensure_ascii=False))

    def test_effective_target_follows_backtest_config(self):
        ctx = build_context(self.candles, self.sig, None, self.series,
                            trade_cfg=BacktestConfig(target_mode="r", target_r=4.0))
        entry, stop = self.sig["entry"], self.sig["stop"]
        risk = abs(entry - stop)
        self.assertAlmostEqual(ctx["거래구조"]["목표"], round(entry + risk * 4.0, 2), places=2)
        self.assertEqual(ctx["거래구조"]["목표근거"], "고정 4R")
        self.assertAlmostEqual(ctx["거래구조"]["손익비"], 4.0, places=2)
        self.assertAlmostEqual(ctx["목표경로"]["목표까지ATR"], 12.0, places=1)

    def test_short_target_is_below_entry(self):
        sig = m1_signal(self.candles, side="SHORT")
        ctx = build_context(self.candles, sig, None, self.series,
                            trade_cfg=BacktestConfig(target_mode="r", target_r=4.0))
        self.assertLess(ctx["거래구조"]["목표"], sig["entry"])

    def test_prompt_contains_context_and_json_instruction(self):
        ctx = build_context(self.candles, self.sig, None, self.series)
        prompt = render_prompt(ctx)
        self.assertIn("loss_prob", prompt)
        self.assertIn("후보·문맥", prompt)


class SelectSignalTests(unittest.TestCase):
    def test_filters_kind_and_session(self):
        # 2023-11-14 00:00 UTC부터 30분 간격
        start = 1_700_000_000_000 - (1_700_000_000_000 % 1_800_000)
        candles = [{"t": start + i * 1_800_000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 1}
                   for i in range(48)]
        in_asia = next(i for i, c in enumerate(candles) if (c["t"] // 3_600_000) % 24 == 2)
        out_asia = next(i for i, c in enumerate(candles) if (c["t"] // 3_600_000) % 24 == 15)
        mk = lambda i, kind: {"side": "LONG", "kind": kind, "index": i, "time": candles[i]["t"]}
        cfg = BacktestConfig(allow_kinds=("M1",), session_filter="asia")
        kept = select_signals(candles, [mk(in_asia, "M1"), mk(out_asia, "M1"), mk(in_asia, "DS")], cfg)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["kind"], "M1")
        self.assertEqual(kept[0]["index"], in_asia)


class CacheAndErrorTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cache = os.path.join(self.dir.name, "ai_cache.json")
        self.candles = candles_from(zigzag())
        self.sig = m1_signal(self.candles)

    def tearDown(self):
        self.dir.cleanup()

    def test_same_context_is_not_called_twice(self):
        provider = FakeProvider(loss_prob=45)
        ai = AIFilter(FilterConfig(cache_path=self.cache), provider=provider)
        first = ai.review(self.sig, candles=self.candles)
        second = ai.review(self.sig, candles=self.candles)
        self.assertEqual(first["decision"], "PASS")
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(len(provider.prompts), 1)
        self.assertEqual(ai.cache_hits, 1)
        self.assertTrue(os.path.isfile(self.cache))

    def test_cache_survives_new_instance(self):
        ai = AIFilter(FilterConfig(cache_path=self.cache), provider=FakeProvider(loss_prob=70))
        ai.review(self.sig, candles=self.candles)
        again = AIFilter(FilterConfig(cache_path=self.cache), provider=FakeProvider())
        verdict = again.review(self.sig, candles=self.candles)
        self.assertEqual(verdict["decision"], "REJECT")
        self.assertTrue(verdict["cached"])
        self.assertEqual(len(again.provider.prompts), 0)

    def test_error_passes_by_default_and_records_reason(self):
        class Boom:
            config = {"model": "boom"}

            def complete(self, *a):
                raise ProviderError("gemini HTTP 429")

        ai = AIFilter(FilterConfig(cache_path=None), provider=Boom())
        verdict = ai.review(self.sig, candles=self.candles)
        self.assertEqual(verdict["decision"], "PASS")
        self.assertIn("429", verdict["error"])
        self.assertEqual(ai.errors, 1)

    def test_error_can_reject_instead(self):
        class Boom:
            config = {"model": "boom"}

            def complete(self, *a):
                raise ProviderError("down")

        ai = AIFilter(FilterConfig(cache_path=None, on_error="reject"), provider=Boom())
        self.assertEqual(ai.review(self.sig, candles=self.candles)["decision"], "REJECT")

    def test_budget_exhaustion_passes_without_error_count(self):
        ai = AIFilter(FilterConfig(cache_path=None, max_calls=0), provider=FakeProvider())
        self.assertEqual(ai.review(self.sig, candles=self.candles)["decision"], "PASS")
        ai.cfg.max_calls = 0
        ai2 = AIFilter(FilterConfig(cache_path=None, max_calls=1), provider=FakeProvider())
        ai2.calls = 1
        verdict = ai2.review(self.sig, candles=self.candles)
        self.assertTrue(verdict["budget"])
        self.assertIsNone(verdict["error"])
        self.assertEqual(ai2.errors, 0)

    def test_review_signals_keeps_only_pass(self):
        candles = candles_from(zigzag())
        series = prepare_series(candles)
        sigs = [m1_signal(candles, index=len(candles) - 6),
                m1_signal(candles, index=len(candles) - 4)]
        sigs[1]["side"] = "SHORT"
        sigs[1]["time"] = candles[len(candles) - 4]["t"]
        verdicts = iter([json.dumps({"loss_prob": 20, "decision": "PASS", "reason": "ok"}),
                         json.dumps({"loss_prob": 80, "decision": "REJECT", "reason": "막힘"})])

        class Sequence:
            config = {"model": "seq"}

            def complete(self, *a):
                return next(verdicts)

        ai = AIFilter(FilterConfig(cache_path=None, min_interval=0), provider=Sequence())
        kept, reviews = review_signals(candles, sigs, ai, series, capture={})
        self.assertEqual(len(reviews), 2)
        self.assertEqual(len(kept), 1)
        self.assertEqual(reviews[1]["ai"]["decision"], "REJECT")


class RandomControlTests(unittest.TestCase):
    def test_observed_above_random_gets_small_p(self):
        trades = [{"r": 4.0, "pnl": 40.0}] * 5 + [{"r": -1.0, "pnl": -10.0}] * 15
        rc = random_control(trades, kept_n=5, observed_avg_r=4.0, draws=300)
        self.assertEqual(rc["kept_n"], 5)
        self.assertLess(rc["p_value"], 0.05)
        self.assertGreater(rc["observed_avg_r"], rc["random_p95"])

    def test_too_few_trades_returns_empty(self):
        self.assertEqual(random_control([{"r": 1.0}], 1, 1.0), {})


if __name__ == "__main__":
    unittest.main()
