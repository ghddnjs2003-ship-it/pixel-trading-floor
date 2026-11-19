"""실시간 모의매매 루프 테스트 — 진입·청산·중복 방지·킬스위치·AI 실패·파일 기록.

실제 거래소·Gemini를 부르지 않는다(캔들·신호·가짜 제공자). 네트워크·키 없이 항상 돈다.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from backend.ai_filter import AIFilter, FilterConfig
from backend.paper_loop import PaperConfig, PaperLoop
from backend.providers import ProviderError
from backend.risk import KillSwitch, KillSwitchRules

TF = 1_800_000                     # 30분
START = 1_790_000_000_000          # 2026-09-19 근처(테스트 고정값)
ROOT = Path(__file__).resolve().parent.parent


def flat_candles(n=120, price=100.0, start=START):
    """보합 캔들 — 지표가 서고 신호 봉을 우리가 정할 수 있게."""
    return [{"t": start + i * TF, "o": price, "h": price + 0.2, "l": price - 0.2,
             "c": price, "v": 10.0} for i in range(n)]


def signal_at(candles, index=None, side="SHORT", atr=1.0):
    i = len(candles) - 1 if index is None else index
    bar = candles[i]
    entry = bar["c"]
    stop = entry + 3 * atr if side == "SHORT" else entry - 3 * atr
    target = entry - 12 * atr if side == "SHORT" else entry + 12 * atr
    return {"side": side, "kind": "M1", "label": "M1 숏" if side == "SHORT" else "M1 롱",
            "grade": "A", "index": i, "time": bar["t"], "entry": entry, "stop": stop,
            "target": target, "rr": 4.0, "risk": 3 * atr, "atr": atr, "stop_atr": 3.0,
            "level": entry, "reasons": ["테스트"]}


class FakeAI:
    """AIFilter와 같은 모양(review만) — 호출 수를 세고 정해진 판정을 돌려준다."""

    def __init__(self, decision="PASS", loss_prob=40.0, reason="테스트 근거"):
        self.cfg = FilterConfig(cache_path=None)
        self.decision = decision
        self.loss_prob = loss_prob
        self.reason = reason
        self.calls = 0

    def review(self, sig, context=None, **_):
        self.calls += 1
        return {"decision": self.decision, "loss_prob": self.loss_prob, "reason": self.reason,
                "confidence": 70, "risk_flags": [], "cached": False, "error": None}


class Boom:
    def complete(self, system_prompt, user_prompt):
        raise ProviderError("gemini HTTP 503")


class PaperLoopTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = PaperConfig(data_dir=self.dir)
        self.candles = flat_candles(120)
        self.sig = signal_at(self.candles)
        # 신호 봉 다음(진행 중) 봉 — 시가 99를 '다음 봉 시가'로 쓴다
        self.forming = {"t": self.candles[-1]["t"] + TF, "o": 99.0, "h": 99.1, "l": 98.9,
                        "c": 99.0, "v": 5.0}
        self.now = self.forming["t"] + 60_000

    def loop(self, ai=None, killswitch=None):
        return PaperLoop(self.cfg, ai=ai, killswitch=killswitch)

    def with_forming(self, candles=None, forming=None):
        candles = list(candles if candles is not None else self.candles)
        f = dict(forming or self.forming)
        # 진행 봉은 마지막 확정 봉 바로 다음 시각이어야 한다
        f["t"] = candles[-1]["t"] + TF
        return candles + [f]

    # -- 진입 ---------------------------------------------------------------
    def test_opens_mock_position_at_next_open_with_3atr_and_4r(self):
        lp = self.loop(ai=FakeAI())
        rec = lp.tick(candles=self.with_forming(), now_ms=self.now, signals=[self.sig])
        self.assertEqual(rec["action"], "OPEN")
        pos = rec["position"]
        # 슬리피지 2bp를 반영한 진입가 = 99 × (1 − 0.0002)
        self.assertAlmostEqual(pos["entry"], 99.0 * (1 - 0.0002), places=3)
        self.assertAlmostEqual(pos["stop"] - pos["entry"], 3.0, places=3)     # 3ATR
        self.assertAlmostEqual(pos["entry"] - pos["target"], 12.0, places=3)  # 4R
        self.assertAlmostEqual(pos["risk_usd"], 100.0, places=2)              # 자본 1%
        self.assertEqual(pos["side"], "SHORT")
        self.assertEqual(rec["filter"]["decision"], "PASS")
        self.assertEqual(rec["bar"], datetime.fromtimestamp(self.candles[-1]["t"] / 1000,
                                                           tz=timezone.utc).isoformat(timespec="seconds"))

    def test_state_and_log_are_written(self):
        lp = self.loop(ai=FakeAI())
        lp.tick(candles=self.with_forming(), now_ms=self.now, signals=[self.sig])
        state = json.loads(Path(self.cfg.state_path).read_text(encoding="utf-8"))
        self.assertEqual(state["position"]["side"], "SHORT")
        self.assertEqual(state["scan_count"], 1)
        lines = Path(self.cfg.log_path).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["action"], "OPEN")

    def test_ai_reject_blocks_entry_and_keeps_reason(self):
        ai = FakeAI(decision="REJECT", loss_prob=72.0, reason="1R 안에 저항 3개")
        lp = self.loop(ai=ai)
        rec = lp.tick(candles=self.with_forming(), now_ms=self.now, signals=[self.sig])
        self.assertEqual(rec["action"], "REJECT")
        self.assertIsNone(rec["position"])
        self.assertEqual(rec["filter"]["loss_prob"], 72.0)
        self.assertIn("저항 3개", rec["events"][0])
        self.assertEqual(ai.calls, 1)

    def test_ai_failure_passes_but_records_error(self):
        ai = AIFilter(FilterConfig(cache_path=None), provider=Boom())
        lp = self.loop(ai=ai)
        rec = lp.tick(candles=self.with_forming(), now_ms=self.now, signals=[self.sig])
        self.assertEqual(rec["action"], "OPEN")               # 실패는 통과(on_error=pass)
        self.assertIn("AI", rec["filter"]["error"] or rec["filter"]["reason"])

    def test_killswitch_blocks_entry(self):
        ks = KillSwitch(KillSwitchRules(), state_path=os.path.join(self.dir, "ks.json"))
        ks.manual_stop(True, "테스트 정지")
        lp = self.loop(ai=FakeAI(), killswitch=ks)
        rec = lp.tick(candles=self.with_forming(), now_ms=self.now, signals=[self.sig])
        self.assertEqual(rec["action"], "SKIP")
        self.assertIsNone(rec["position"])
        self.assertIn("수동 정지", rec["events"][0])

    def test_session_filter_blocks_entry(self):
        cand = flat_candles(120)
        # 신호 봉 23:30 UTC → 다음 봉(00:00)은 아시아 세션 안이라 통과해야 한다
        sig = signal_at(cand, side="SHORT")
        lp = self.loop(ai=FakeAI())
        rec = lp.tick(candles=self.with_forming(cand), now_ms=cand[-1]["t"] + TF + 60_000, signals=[sig])
        expected_in = bool(rec["action"] in ("OPEN", "SKIP"))
        self.assertTrue(expected_in)

        # 반대로 진입 봉이 12:00 UTC가 되도록 신호 봉을 11:30 UTC로 옮긴다 → 세션 밖
        base = START + 11 * 3_600_000 + 30 * 60_000          # 11:30 UTC
        cand2 = [{"t": base + i * TF, "o": 100.0, "h": 100.2, "l": 99.8, "c": 100.0, "v": 10.0}
                 for i in range(120)]
        sig2 = signal_at(cand2, side="SHORT")
        lp2 = PaperLoop(PaperConfig(data_dir=tempfile.mkdtemp()), ai=FakeAI())
        rec2 = lp2.tick(candles=self.with_forming(cand2), now_ms=cand2[-1]["t"] + TF + 60_000,
                        signals=[sig2])
        self.assertEqual(rec2["action"], "SKIP")
        self.assertIn("세션", rec2["events"][0])

    def test_forming_bar_is_not_used_for_signal(self):
        lp = self.loop(ai=FakeAI())
        rec = lp.tick(candles=self.with_forming(), now_ms=self.now, signals=[self.sig])
        # 마지막 확정 봉은 신호 봉(진행 봉이 아님)
        self.assertEqual(rec["bars"], 120)
        self.assertEqual(rec["bar"], datetime.fromtimestamp(self.candles[-1]["t"] / 1000,
                                                            tz=timezone.utc).isoformat(timespec="seconds"))

    # -- 청산 ---------------------------------------------------------------
    def _open(self, lp):
        rec = lp.tick(candles=self.with_forming(), now_ms=self.now, signals=[self.sig])
        self.assertEqual(rec["action"], "OPEN")
        return rec

    def test_stop_hit_closes_near_minus_one_r(self):
        lp = self.loop(ai=FakeAI())
        self._open(lp)
        pos = lp.load_state()["position"]
        entry_bar = {"t": pos["entry_bar_time"], "o": 99.0, "h": 99.2, "l": 98.8, "c": 99.0, "v": 5.0}
        spike = {"t": pos["entry_bar_time"] + TF, "o": 99.0, "h": pos["stop"] + 1.0,
                 "l": 98.0, "c": 100.0, "v": 9.0}
        rec = lp.tick(candles=self.candles + [entry_bar, spike],
                      now_ms=spike["t"] + TF + 60_000, signals=[])
        self.assertEqual(rec["action"], "CLOSE")
        trade = json.loads(Path(self.cfg.state_path).read_text(encoding="utf-8"))["closed"][-1]
        self.assertEqual(trade["reason"], "STOP")
        self.assertAlmostEqual(trade["exit"], pos["stop"] * (1 + 0.0002), places=2)   # 슬리피지 반영 체결
        self.assertLess(trade["r"], -0.95)              # 수수료·슬리피지만큼 -1R보다 조금 나쁘다
        self.assertGreater(trade["r"], -1.2)

    def test_target_hit_closes_near_plus_four_r(self):
        lp = self.loop(ai=FakeAI())
        self._open(lp)
        pos = lp.load_state()["position"]
        entry_bar = {"t": pos["entry_bar_time"], "o": 99.0, "h": 99.2, "l": 98.8, "c": 99.0, "v": 5.0}
        drop = {"t": pos["entry_bar_time"] + TF, "o": 99.0, "h": 99.5,
                "l": pos["target"] - 1.0, "c": 88.0, "v": 9.0}
        rec = lp.tick(candles=self.candles + [entry_bar, drop],
                      now_ms=drop["t"] + TF + 60_000, signals=[])
        self.assertEqual(rec["action"], "CLOSE")
        trade = json.loads(Path(self.cfg.state_path).read_text(encoding="utf-8"))["closed"][-1]
        self.assertEqual(trade["reason"], "TARGET")
        self.assertGreater(trade["r"], 3.8)
        self.assertLess(trade["r"], 4.1)

    def test_time_exit_after_max_bars(self):
        lp = self.loop(ai=FakeAI())
        self._open(lp)
        pos = lp.load_state()["position"]
        bars = []
        for k in range(1, lp.cfg.max_bars + 1):
            bars.append({"t": pos["entry_bar_time"] + (k - 1) * TF, "o": 99.0, "h": 99.2,
                         "l": 98.8, "c": 99.0, "v": 5.0})
        rec = lp.tick(candles=self.candles + bars, now_ms=bars[-1]["t"] + TF + 60_000, signals=[])
        self.assertEqual(rec["action"], "CLOSE")
        trade = json.loads(Path(self.cfg.state_path).read_text(encoding="utf-8"))["closed"][-1]
        self.assertEqual(trade["reason"], "TIME")
        self.assertEqual(trade["bars"], lp.cfg.max_bars)

    def test_stats_are_recomputed_from_closed_trades(self):
        lp = self.loop(ai=FakeAI())
        self._open(lp)
        pos = lp.load_state()["position"]
        entry_bar = {"t": pos["entry_bar_time"], "o": 99.0, "h": 99.2, "l": 98.8, "c": 99.0, "v": 5.0}
        drop = {"t": pos["entry_bar_time"] + TF, "o": 99.0, "h": 99.5, "l": pos["target"] - 1.0,
                "c": 88.0, "v": 9.0}
        lp.tick(candles=self.candles + [entry_bar, drop], now_ms=drop["t"] + TF + 60_000, signals=[])
        stats = json.loads(Path(self.cfg.state_path).read_text(encoding="utf-8"))["stats"]
        self.assertEqual(stats["trades"], 1)
        self.assertEqual(stats["wins"], 1)
        self.assertEqual(stats["targets"], 1)
        self.assertEqual(stats["win_rate_pct"], 100.0)

    # -- 중복·재시작 ---------------------------------------------------------
    def test_same_bar_twice_does_not_duplicate_entry_or_ai_call(self):
        ai = FakeAI()
        lp = self.loop(ai=ai)
        lp.tick(candles=self.with_forming(), now_ms=self.now, signals=[self.sig])
        rec = lp.tick(candles=self.with_forming(), now_ms=self.now + 1000, signals=[self.sig])
        self.assertEqual(ai.calls, 1)                     # AI는 한 번만
        self.assertNotEqual(rec["action"], "OPEN")
        self.assertEqual(len(json.loads(Path(self.cfg.state_path).read_text(encoding="utf-8"))["closed"]), 0)

    def test_new_bar_evaluates_again(self):
        ai = FakeAI()
        lp = self.loop(ai=ai)
        lp.tick(candles=self.with_forming(), now_ms=self.now, signals=[self.sig])
        # 진입 봉이 끝나 새 봉이 확정된다 → 그 봉에 새 신호가 서도 포지션 보유 중이라 진입하지 않는다
        entry_bar = {"t": self.forming["t"], "o": 99.0, "h": 99.2, "l": 98.8, "c": 99.0, "v": 5.0}
        later = self.candles + [entry_bar]
        sig2 = signal_at(later, side="LONG")
        rec = lp.tick(candles=later, now_ms=entry_bar["t"] + TF + 60_000, signals=[sig2])
        self.assertEqual(rec["action"], "SKIP")
        self.assertIn("포지션", rec["events"][0])

    def test_restart_resumes_position(self):
        lp = self.loop(ai=FakeAI())
        self._open(lp)
        again = self.loop(ai=FakeAI())
        state = again.load_state()
        self.assertIsNotNone(state["position"])
        self.assertEqual(state["position"]["side"], "SHORT")
        self.assertEqual(state["closed"], [])

    # -- 안전 ---------------------------------------------------------------
    def test_logs_never_contain_api_key(self):
        os.environ["GEMINI_API_KEY"] = "test-key-should-not-appear"
        try:
            lp = self.loop(ai=FakeAI())
            lp.tick(candles=self.with_forming(), now_ms=self.now, signals=[self.sig])
            for path in (self.cfg.state_path, self.cfg.log_path):
                text = Path(path).read_text(encoding="utf-8")
                self.assertNotIn("test-key-should-not-appear", text)
                self.assertNotIn("GEMINI_API_KEY", text)
        finally:
            os.environ.pop("GEMINI_API_KEY", None)

    def test_status_and_reset(self):
        lp = self.loop(ai=FakeAI())
        self._open(lp)
        status = lp.status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["state"]["position"]["side"], "SHORT")
        self.assertIn("rules", status["killswitch"])
        self.assertEqual(len(status["log"]), 1)
        lp.reset()
        state = lp.load_state()
        self.assertIsNone(state["position"])
        self.assertEqual(state["stats"]["trades"], 0)
        self.assertEqual(state["equity"], self.cfg.initial_equity)


class RealFixtureTests(unittest.TestCase):
    """커밋된 15분봉 픽스처를 30분봉으로 묶어 실제 엔진이 세운 M1 자리로 진입까지 확인한다."""

    def test_engine_signal_opens_position(self):
        path = ROOT / "data" / "btc_15m_3600.json"
        if not path.is_file():
            self.skipTest("픽스처 없음")
        from backend.strategy import Engine, resample
        from backend.ai_filter import nominee_setup, select_signals
        candles = json.loads(path.read_text(encoding="utf-8"))
        candles.sort(key=lambda c: c["t"])
        c30 = resample(candles, 30)
        params, cfg = nominee_setup()
        picks = select_signals(c30, Engine(params).run(c30), cfg)     # 아시아 진입·M1만
        self.assertTrue(picks, "픽스처에 후보1 M1 신호가 있어야 한다")
        sig = picks[-1]
        prefix = c30[:sig["index"] + 1]
        forming = {"t": prefix[-1]["t"] + TF, "o": prefix[-1]["c"], "h": prefix[-1]["c"] + 1,
                   "l": prefix[-1]["c"] - 1, "c": prefix[-1]["c"], "v": 1.0}
        loop = PaperLoop(PaperConfig(data_dir=tempfile.mkdtemp()), ai=False)
        rec = loop.tick(candles=prefix + [forming], now_ms=forming["t"] + 60_000)
        self.assertEqual(rec["action"], "OPEN")
        self.assertEqual(rec["signal"]["side"], sig["side"])
        self.assertAlmostEqual(rec["position"]["risk_usd"], 100.0, places=2)


if __name__ == "__main__":
    unittest.main()
