"""P0 배선 통합 테스트 — 13인 분석(orchestrator)과 모의매매 루프(paper_loop)에
내 책·복기·국면·교차 토론·리포트가 실제로 붙었는가.

네트워크·AI를 부르지 않는다(가짜 제공자·가짜 필터·tempfile).
"""

import json
import tempfile
import unittest
from pathlib import Path

from backend.paper_loop import PaperConfig, PaperLoop
from backend.test_paper_loop import TF, FakeAI, flat_candles, signal_at
from backend.test_orchestrator import FakeProvider
from backend.orchestrator import AnalysisOrchestrator

MARKET = {
    "symbol": "BTCUSDT", "display": "BTC", "price": 65000.0, "change20BarsPct": -1.2,
    "range20Bars": {"low": 64000.0, "high": 66000.0},
    "candles": [{"t": 1_790_000_000_000 + i * 300_000, "o": 65000.0 + i, "h": 65050.0 + i,
                 "l": 64950.0 + i, "c": 65000.0 + i, "v": 10.0} for i in range(200)],
}

RULE = {"side": "LONG", "kind": "M1", "label": "M1 롱", "grade": "A", "index": 0,
        "time": 1, "entry": 65000.0, "stop": 64400.0, "target": 67400.0, "rr": 4.0,
        "atr": 200.0, "stop_atr": 3.0, "reasons": ["테스트"]}


def paper_state_with_position(side="LONG"):
    return {"symbol": "BTCUSDT", "equity": 10_000.0, "initial_equity": 10_000.0,
            "position": {"side": side, "entry": 64_000.0, "stop": 63_000.0, "target": 68_000.0,
                         "qty": 0.1, "bars": 2},
            "stats": {"trades": 1, "win_rate_pct": 0.0},
            "closed": [], "killswitch": {"ok": True, "reason": "정상"}}


class BuyProvider(FakeProvider):
    """PM(13번째 호출)만 BUY를 고르는 가짜 제공자."""

    def complete(self, system_prompt, user_prompt):
        self.prompts.append((system_prompt, user_prompt))
        if len(self.prompts) == 13:
            return json.dumps({"bubble": "롱 승인", "report": "자료를 종합해 롱 진입을 승인합니다.",
                               "action": "BUY", "confidence": 66, "rationale": "추세 순응",
                               "verdict": "APPROVE"})
        return json.dumps({"bubble": "자료 확인", "report": "제공된 자료를 검토했습니다."})


class OrchestratorPortfolioTests(unittest.TestCase):
    def build(self, provider, **kw):
        orch = AnalysisOrchestrator(provider, market_loader=lambda _: MARKET, **kw)
        orch._rule_view = lambda candles: (RULE, None)
        return orch

    def test_portfolio_and_lessons_reach_every_prompt(self):
        lessons_dir = tempfile.mkdtemp()
        provider = BuyProvider()
        orch = self.build(provider, portfolio_loader=lambda: (paper_state_with_position(), None),
                          data_dir=lessons_dir, lessons=None)
        events = []
        orch.run("BTC", events.append)
        first_prompt = provider.prompts[0][1]
        self.assertIn("내 계좌", first_prompt)
        self.assertIn("10000.0", first_prompt)          # 자본이 문맥에 실제 값으로 들어간다
        self.assertIn("시장 국면", first_prompt)
        self.assertIn("최근 복기 교훈", first_prompt)
        decision = next(e for e in events if e["type"] == "decision")
        self.assertEqual(decision["portfolio"]["열린포지션수"], 1)
        self.assertIn("국면", decision["regime"])

    def test_same_symbol_position_blocks_new_entry(self):
        provider = BuyProvider()
        orch = self.build(provider, portfolio_loader=lambda: (paper_state_with_position("LONG"), None))
        events = []
        orch.run("BTC", events.append)
        decision = next(e for e in events if e["type"] == "decision")
        self.assertEqual(decision["action"], "HOLD")            # PM은 BUY였지만 중복으로 막힘
        self.assertIn("중복", decision["duplicate_blocked"] or "")
        self.assertTrue(any("중복" in e.get("line", "") for e in events if e["type"] == "log"))

    def test_other_symbol_position_does_not_block(self):
        provider = BuyProvider()
        state = paper_state_with_position("LONG")
        state["symbol"] = "ETHUSDT"
        orch = self.build(provider, portfolio_loader=lambda: (state, None))
        events = []
        orch.run("BTC", events.append)
        decision = next(e for e in events if e["type"] == "decision")
        self.assertEqual(decision["action"], "BUY")
        self.assertIsNone(decision["duplicate_blocked"])

    def test_risk_three_cross_review_prompts(self):
        provider = FakeProvider()
        orch = self.build(provider)
        orch.run("BTC", [].append)
        prompts = [p[1] for p in provider.prompts]
        safe, neutral, risky = prompts[9], prompts[10], prompts[11]
        self.assertIn("보수 심사자", safe)
        self.assertIn("SAFE", neutral)
        self.assertIn("동의", neutral)
        self.assertIn("SAFE와 NEUTRAL", risky)
        # 앞사람 발언이 실제로 다음 프롬프트의 '앞선 토론'에 들어간다
        self.assertIn("보수적 리스크 심사자", neutral)
        self.assertIn("중립 리스크 심사자", risky)

    def test_optional_second_risk_round_adds_three_calls(self):
        provider = FakeProvider()
        orch = self.build(provider, risk_rounds=2)
        events = []
        orch.run("BTC", events.append)
        self.assertEqual(len(provider.prompts), 16)
        ids = [e["id"] for e in events if e["type"] == "agent:done"]
        self.assertEqual(ids.count("safe"), 2)
        self.assertEqual(ids[-1], "pm")

    def test_report_archive_written_with_input_status(self):
        provider = FakeProvider()
        out = tempfile.mkdtemp()
        data = tempfile.mkdtemp()
        orch = self.build(provider, report_dir=out, data_dir=data, portfolio_loader=None)
        events = []
        orch.run("BTC", events.append)
        files = sorted(Path(out).glob("*.md"))
        self.assertEqual(len(files), 1)
        md = files[0].read_text(encoding="utf-8")
        self.assertIn("## 입력 상태", md)
        self.assertIn("경제일정", md)
        self.assertIn("## 최종 판정", md)
        self.assertTrue(any("리포트 저장" in e.get("line", "") for e in events if e["type"] == "log"))

    def test_no_report_dir_means_no_files(self):
        provider = FakeProvider()
        orch = self.build(provider)          # report_dir 기본값 None
        events = []
        orch.run("BTC", events.append)
        self.assertFalse(any("리포트 저장" in e.get("line", "") for e in events if e["type"] == "log"))


class CapturingAI:
    def __init__(self, decision="PASS"):
        from backend.ai_filter import FilterConfig
        self.cfg = FilterConfig(cache_path=None, symbol="BTCUSDT")
        self.decision = decision
        self.context = None

    def review(self, sig, context=None, **kw):
        self.context = context
        return {"decision": self.decision, "loss_prob": 40.0, "reason": "테스트", "confidence": 70,
                "risk_flags": [], "cached": False, "error": None}


class PaperLoopMemoryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = PaperConfig(data_dir=self.dir)
        self.candles = flat_candles(120)
        self.sig = signal_at(self.candles)
        self.forming = {"t": self.candles[-1]["t"] + TF, "o": 99.0, "h": 99.1, "l": 98.9, "c": 99.0, "v": 5.0}
        self.now = self.forming["t"] + 60_000

    def loop(self, ai=None):
        return PaperLoop(self.cfg, ai=ai)

    def open_position(self, lp):
        rec = lp.tick(candles=self.candles + [self.forming], now_ms=self.now, signals=[self.sig])
        self.assertEqual(rec["action"], "OPEN")
        return lp.load_state()["position"]

    def test_ai_context_carries_account_lessons_and_regime(self):
        ai = CapturingAI()
        lp = self.loop(ai=ai)
        lp.tick(candles=self.candles + [self.forming], now_ms=self.now, signals=[self.sig])
        self.assertIsNotNone(ai.context)
        self.assertIn("내 계좌", ai.context)
        self.assertIn("시장 국면", ai.context)
        self.assertIn("최근 복기 교훈", ai.context)
        self.assertEqual(ai.context["내 계좌"]["열린포지션수"], 0)

    def test_close_writes_lesson_memory(self):
        lp = self.loop(ai=FakeAI())
        pos = self.open_position(lp)
        entry_bar = {"t": pos["entry_bar_time"], "o": 99.0, "h": 99.2, "l": 98.8, "c": 99.0, "v": 5.0}
        spike = {"t": pos["entry_bar_time"] + TF, "o": 99.0, "h": pos["stop"] + 1.0, "l": 98.0,
                 "c": 100.0, "v": 9.0}
        rec = lp.tick(candles=self.candles + [entry_bar, spike], now_ms=spike["t"] + TF + 60_000, signals=[])
        self.assertEqual(rec["action"], "CLOSE")
        mem = Path(self.dir, "paper_memory.jsonl")
        md = Path(self.dir, "memory.md")
        self.assertTrue(mem.is_file())
        self.assertTrue(md.is_file())
        rows = [json.loads(x) for x in mem.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "STOP")
        self.assertIn("손절", md.read_text(encoding="utf-8"))
        status = lp.status()
        self.assertTrue(any("1패" in line or "0승" in line for line in status["lessons"]))

    def test_duplicate_signal_is_skipped_with_reason(self):
        lp = self.loop(ai=FakeAI())
        pos = self.open_position(lp)                 # SHORT 포지션
        state = lp.load_state()
        state["position"] = {**state["position"], "entry_bar_time": self.candles[-1]["t"] + TF,
                             "last_bar_time": None, "bars": 0}
        lp.save_state(state)
        sig2 = signal_at(self.candles, side="LONG")   # 반대 방향 신호
        rec = lp.tick(candles=self.candles + [self.forming], now_ms=self.now, signals=[sig2])
        self.assertEqual(rec["action"], "SKIP")
        self.assertIn("청산 뒤", rec["events"][0])
        self.assertIsNotNone(pos)

    def test_regime_guard_off_by_default_and_can_block(self):
        lp = self.loop(ai=FakeAI())
        rec = lp.tick(candles=self.candles + [self.forming], now_ms=self.now, signals=[self.sig])
        self.assertEqual(rec["action"], "OPEN")             # 기본 off — 막지 않는다
        # 변동성이 계속 죽어 가는 장(앞은 넓고 뒤는 좁다) → ATR 백분위가 낮다
        quiet = flat_candles(120)
        for i, bar in enumerate(quiet):
            spread = 5.0 - 4.95 * i / 119
            bar["h"] = bar["o"] + spread
            bar["l"] = bar["o"] - spread
        sig_q = signal_at(quiet)
        cfg = PaperConfig(data_dir=tempfile.mkdtemp(), regime_guard="dead")
        lp2 = PaperLoop(cfg, ai=FakeAI())
        rec2 = lp2.tick(candles=quiet + [{"t": quiet[-1]["t"] + TF, "o": 99.0, "h": 99.1,
                                          "l": 98.9, "c": 99.0, "v": 5.0}],
                        now_ms=quiet[-1]["t"] + TF + 60_000, signals=[sig_q])
        self.assertEqual(rec2["action"], "SKIP")            # 가드를 켰을 때만 막힌다
        self.assertIn("국면 가드", rec2["events"][0])

    def test_reset_clears_memory(self):
        lp = self.loop(ai=FakeAI())
        self.open_position(lp)
        pos = lp.load_state()["position"]
        entry_bar = {"t": pos["entry_bar_time"], "o": 99.0, "h": 99.2, "l": 98.8, "c": 99.0, "v": 5.0}
        spike = {"t": pos["entry_bar_time"] + TF, "o": 99.0, "h": pos["stop"] + 1.0, "l": 98.0,
                 "c": 100.0, "v": 9.0}
        lp.tick(candles=self.candles + [entry_bar, spike], now_ms=spike["t"] + TF + 60_000, signals=[])
        lp.reset()
        self.assertFalse(Path(self.dir, "paper_memory.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
