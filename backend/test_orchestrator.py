import json
import unittest

from backend.orchestrator import AnalysisOrchestrator


class FakeProvider:
    def __init__(self):
        self.prompts = []

    def complete(self, system_prompt, user_prompt):
        self.prompts.append((system_prompt, user_prompt))
        if len(self.prompts) == 13:
            return json.dumps({
                "bubble": "숏 승인",
                "report": "토론을 종합해 숏 진입을 승인합니다.",
                "action": "SELL",
                "confidence": 71,
                "rationale": "제공된 가격 자료 기준 하락 우위",
                "verdict": "APPROVE",
            })
        return json.dumps({"bubble": "자료 확인", "report": "제공된 자료를 검토했습니다."})


class OrchestratorTests(unittest.TestCase):
    def test_runs_thirteen_calls_in_requested_order_and_emits_final_decision(self):
        market = {
            "symbol": "BTCUSDT",
            "display": "BTC",
            "price": 65000.0,
            "change20BarsPct": -1.2,
            "range20Bars": {"low": 64000.0, "high": 66000.0},
            "candles": [{"t": 1, "o": 2, "h": 3, "l": 1, "c": 2}],
        }
        provider = FakeProvider()
        events = []
        AnalysisOrchestrator(provider, market_loader=lambda _: market).run("BTC", events.append)

        completed = [event for event in events if event["type"] == "agent:done"]
        self.assertEqual(len(provider.prompts), 13)
        self.assertEqual([event["id"] for event in completed], [
            "taro", "diana", "nova", "vibe", "bull", "bear", "bull", "bear",
            "ace", "safe", "neutral", "risky", "pm",
        ])
        decision = next(event for event in events if event["type"] == "decision")
        self.assertEqual(decision["action"], "SELL")
        self.assertEqual(decision["confidence"], 71)
        self.assertEqual(events[-1], {"type": "run:end"})

    def test_invalid_pm_action_falls_back_to_hold(self):
        class InvalidActionProvider(FakeProvider):
            def complete(self, system_prompt, user_prompt):
                if len(self.prompts) == 12:
                    self.prompts.append((system_prompt, user_prompt))
                    return json.dumps({"bubble": "최종", "report": "검토", "action": "MOON"})
                return super().complete(system_prompt, user_prompt)

        market = {
            "symbol": "BTCUSDT", "display": "BTC", "price": 1.0,
            "change20BarsPct": 0, "range20Bars": {"low": 1, "high": 1},
            "candles": [{"t": 1, "o": 1, "h": 1, "l": 1, "c": 1}],
        }
        events = []
        AnalysisOrchestrator(InvalidActionProvider(), market_loader=lambda _: market).run("BTC", events.append)
        decision = next(event for event in events if event["type"] == "decision")
        self.assertEqual(decision["action"], "HOLD")


if __name__ == "__main__":
    unittest.main()
