"""AI 맥락 필터 배선 테스트 — 13인 분석(orchestrator) 게이트와 실시간 엔드포인트(run_live_filter).

네트워크·키 없이 돈다(시세 로더와 필터를 가짜로 바꾼다).
"""

import json
import unittest

from backend import server
from backend.backtest import BacktestConfig
from backend.orchestrator import AnalysisOrchestrator

from backend.test_orchestrator import FakeProvider


MARKET = {
    "symbol": "BTCUSDT", "display": "BTC", "price": 65000.0, "change20BarsPct": -1.2,
    "range20Bars": {"low": 64000.0, "high": 66000.0},
    "candles": [{"t": 1, "o": 2, "h": 3, "l": 1, "c": 2}],
}

RULE = {"side": "LONG", "kind": "M1", "label": "M1 롱", "grade": "A", "index": 0,
        "time": 1, "entry": 65000.0, "stop": 64400.0, "target": 67400.0, "rr": 4.0,
        "atr": 200.0, "stop_atr": 3.0, "reasons": ["테스트"]}


class RecordingFilter:
    def __init__(self, decision="REJECT", loss_prob=72):
        from backend.ai_filter import FilterConfig
        self.decision = decision
        self.loss_prob = loss_prob
        self.calls = 0
        self.cfg = FilterConfig(cache_path=None)
        self.cache = {}
        self.provider = None
        self.calls = 0

    def review(self, signal, context=None, candles=None, snap=None, series=None, trade_cfg=None):
        self.calls += 1
        return {"decision": self.decision, "loss_prob": self.loss_prob, "reason": "1R 앞 저항",
                "confidence": 80, "model": "fake", "cached": False, "error": None}

    def save_cache(self):
        pass


class OrchestratorGateTests(unittest.TestCase):
    def _run(self, ai, veto=True):
        events = []
        orch = AnalysisOrchestrator(FakeProvider(), market_loader=lambda _: MARKET,
                                    ai_filter=ai, ai_filter_veto=veto)
        orch._rule_view = lambda candles: (RULE, None)      # 규칙이 선 자리를 가정
        orch.run("BTC", events.append)
        decision = next(e for e in events if e["type"] == "decision")
        return decision, events

    def test_reject_blocks_entry_and_is_reported(self):
        ai = RecordingFilter("REJECT")
        decision, events = self._run(ai)
        self.assertEqual(ai.calls, 1)
        self.assertEqual(decision["action"], "HOLD")          # PM은 SELL을 골랐지만 필터가 막았다
        self.assertEqual(decision["ai_filter"]["decision"], "REJECT")
        self.assertIn("AI 맥락 필터", decision["ai_filter_blocked"] or "")
        self.assertTrue(any("AI 맥락 필터" in e.get("line", "") for e in events if e["type"] == "log"))

    def test_pass_lets_pm_decision_through(self):
        ai = RecordingFilter("PASS", loss_prob=30)
        decision, _ = self._run(ai)
        self.assertEqual(decision["action"], "SELL")
        self.assertIsNone(decision["ai_filter_blocked"])
        self.assertEqual(decision["ai_filter"]["loss_prob"], 30)

    def test_advisory_mode_records_but_does_not_block(self):
        ai = RecordingFilter("REJECT")
        decision, _ = self._run(ai, veto=False)
        self.assertEqual(decision["action"], "SELL")
        self.assertEqual(decision["ai_filter"]["decision"], "REJECT")
        self.assertIsNone(decision["ai_filter_blocked"])

    def test_no_signal_means_no_ai_call(self):
        ai = RecordingFilter("REJECT")
        orch = AnalysisOrchestrator(FakeProvider(), market_loader=lambda _: MARKET, ai_filter=ai)
        events = []
        orch.run("BTC", events.append)          # 1봉짜리 시장 → 규칙 자리 없음
        self.assertEqual(ai.calls, 0)
        decision = next(e for e in events if e["type"] == "decision")
        self.assertIsNone(decision["ai_filter"])

    def test_filter_error_does_not_stop_analysis(self):
        class Boom:
            def review(self, *a, **k):
                raise RuntimeError("quota")

        events = []
        orch = AnalysisOrchestrator(FakeProvider(), market_loader=lambda _: MARKET,
                                    ai_filter=Boom())
        orch._rule_view = lambda candles: (RULE, None)
        orch.run("BTC", events.append)
        decision = next(e for e in events if e["type"] == "decision")
        self.assertEqual(decision["action"], "SELL")          # 오류는 통과로 처리
        self.assertIn("quota", decision["ai_filter"]["reason"])


class LiveFilterTests(unittest.TestCase):
    def setUp(self):
        start = 1_700_000_000_000 - (1_700_000_000_000 % 1_800_000)
        self.candles = [{"t": start + i * 1_800_000, "o": 30000, "h": 30100, "l": 29900,
                         "c": 30050, "v": 10} for i in range(300)]
        self.original = server.load_candles if hasattr(server, "load_candles") else None

    def _patch_loader(self):
        import backend.backtest as bt
        original = bt.load_candles

        def fake(symbol, interval, limit, source="auto"):
            return {"candles": self.candles, "source": "fake"}

        bt.load_candles = fake
        self.addCleanup(setattr, bt, "load_candles", original)

    def test_rejects_bad_symbol_and_interval(self):
        self._patch_loader()
        with self.assertRaises(ValueError):
            server.run_live_filter({"symbol": "!!", "interval": "30"})
        with self.assertRaises(ValueError):
            server.run_live_filter({"symbol": "BTC", "interval": "5"})

    def test_reports_no_candidate_without_calling_the_model(self):
        self._patch_loader()
        result = server.run_live_filter({"symbol": "BTC", "interval": "30", "limit": 500})
        self.assertTrue(result["ok"])
        self.assertIsNone(result["signal"])
        self.assertIsNone(result["filter"])
        self.assertIn("자리가 없습니다", result["note"])
        json.dumps(result, ensure_ascii=False)      # 응답은 그대로 JSON이어야 한다

    def test_live_filter_is_read_only_and_returns_plan_when_signal_exists(self):
        """후보가 있으면 계획(진입·손절·4R 목표)과 판정을 함께 돌려주고, 주문 필드는 만들지 않는다."""
        import backend.ai_filter as af
        self._patch_loader()
        candles = self.candles
        signal = {"side": "LONG", "kind": "M1", "label": "M1 롱", "grade": "A", "index": len(candles) - 2,
                  "time": candles[-2]["t"], "entry": 30050.0, "stop": 29960.0, "target": 30050.0,
                  "rr": 4.0, "atr": 30.0, "stop_atr": 3.0, "reasons": ["테스트"]}
        original_select = af.select_signals
        original_filter = af.AIFilter
        af.select_signals = lambda c, s, cfg: [signal]
        af.AIFilter = lambda cfg=None, provider=None: RecordingFilter("PASS", 35)
        self.addCleanup(setattr, af, "select_signals", original_select)
        self.addCleanup(setattr, af, "AIFilter", original_filter)

        result = server.run_live_filter({"symbol": "BTC", "interval": "30", "limit": 500})
        self.assertEqual(result["filter"]["decision"], "PASS")
        self.assertAlmostEqual(result["trade_plan"]["target"], 30050.0 + 90.0 * 4, places=2)
        self.assertNotIn("order", json.dumps(result, ensure_ascii=False).lower())


if __name__ == "__main__":
    unittest.main()
