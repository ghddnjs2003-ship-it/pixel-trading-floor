"""P0-1·P0-2 테스트 — 내 책(모의 계좌) 문맥과 복기 메모리.

실제 브라우저·거래소·AI를 부르지 않는다(tempfile만 쓴다).
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.memory import (LessonMemory, duplicate_entry, lesson_of, lessons_text,
                            portfolio_context)

KST = timezone(timedelta(hours=9))
NOW = int(datetime(2026, 11, 20, 12, 0, tzinfo=KST).timestamp() * 1000)


def state_with_position(symbol="BTCUSDT", side="LONG"):
    return {
        "symbol": symbol, "equity": 10_250.0, "initial_equity": 10_000.0,
        "position": {"side": side, "entry": 60_000.0, "stop": 59_000.0, "target": 64_000.0,
                     "qty": 0.1, "last_price": 60_500.0, "bars": 3},
        "stats": {"trades": 4, "win_rate_pct": 50.0},
        "closed": [
            {"side": "LONG", "pnl": 500.0, "r": 4.0, "reason": "TARGET",
             "exit_time": datetime(2026, 11, 20, 9, 0, tzinfo=KST).isoformat(timespec="seconds")},
            {"side": "SHORT", "pnl": -100.0, "r": -1.0, "reason": "STOP",
             "exit_time": datetime(2026, 11, 19, 9, 0, tzinfo=KST).isoformat(timespec="seconds")},
        ],
    }


class PortfolioContextTests(unittest.TestCase):
    def test_reports_balance_positions_and_today_pnl(self):
        ctx = portfolio_context(state_with_position(), {"ok": False, "reason": "연속 3회 손절"}, now_ms=NOW)
        self.assertEqual(ctx["자본USDT"], 10_250.0)
        self.assertEqual(ctx["누적손익USDT"], 250.0)
        self.assertEqual(ctx["열린포지션수"], 1)
        self.assertEqual(ctx["열린포지션"][0]["방향"], "LONG")
        self.assertAlmostEqual(ctx["열린포지션"][0]["평가손익USDT"], 50.0, places=2)
        self.assertEqual(ctx["오늘손익USDT"], 500.0)          # 오늘(KST) 청산만
        self.assertEqual(ctx["오늘청산"], 1)
        self.assertEqual(ctx["보유종목"], ["BTCUSDT"])
        self.assertFalse(ctx["킬스위치"]["진입가능"])
        self.assertIn("중복", ctx["중복진입금지"])

    def test_empty_account_does_not_invent_numbers(self):
        ctx = portfolio_context(None, None, symbol="BTCUSDT", now_ms=NOW)
        self.assertIsNone(ctx["자본USDT"])
        self.assertIsNone(ctx["누적손익USDT"])
        self.assertEqual(ctx["열린포지션수"], 0)

    def test_duplicate_entry_messages(self):
        st = state_with_position()
        self.assertIn("같은 방향 중복 진입 금지", duplicate_entry(st, "BTCUSDT", "LONG"))
        self.assertIn("반대 방향은 청산 뒤에만", duplicate_entry(st, "BTCUSDT", "SHORT"))
        self.assertIsNone(duplicate_entry(st, "ETHUSDT", "LONG"))     # 다른 종목은 무관
        self.assertIsNone(duplicate_entry({"position": None}, "BTCUSDT", "LONG"))


def trade(r=1.0, reason="STOP", side="LONG", exit_ms=None, pnl=None):
    exit_ms = exit_ms if exit_ms is not None else NOW
    return {"side": side, "reason": reason, "r": r, "pnl": pnl if pnl is not None else r * 100,
            "entry": 100.0, "exit": 101.0, "bars": 4,
            "exit_time": datetime.fromtimestamp(exit_ms / 1000, tz=KST).isoformat(timespec="seconds"),
            "ai_decision": "PASS", "ai_reason": "1R 앞 매물대 없음"}


class LessonMemoryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.mem = LessonMemory(data_dir=self.dir)

    def test_record_writes_jsonl_and_markdown(self):
        rec = self.mem.record(trade(-1.0, "STOP", "LONG"), symbol="BTCUSDT")
        self.assertEqual(rec["reason_ko"], "손절")
        lines = [json.loads(x) for x in
                 Path(self.dir, "paper_memory.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["symbol"], "BTCUSDT")
        md = Path(self.dir, "memory.md").read_text(encoding="utf-8")
        self.assertIn("모의매매 복기 메모", md)
        self.assertIn("손절", md)

    def test_prompt_lines_are_three_facts_plus_lesson(self):
        self.mem.record(trade(-1.0, "STOP", "LONG", exit_ms=NOW - 3600_000), symbol="BTCUSDT")
        self.mem.record(trade(-1.0, "STOP", "LONG", exit_ms=NOW - 1800_000), symbol="BTCUSDT")
        lines = self.mem.prompt_lines(5)
        self.assertIn("0승 2패", lines[0])
        self.assertIn("연속 손실 2건", lines[0])
        self.assertTrue(any(l.startswith("· ") for l in lines))
        self.assertTrue(any(l.startswith("교훈:") for l in lines))    # 롱 손실 몰림 → 교훈
        self.assertLessEqual(len(lines), 3 + 2)

    def test_as_of_hides_the_future(self):
        old = NOW - 10 * 3600_000
        self.mem.record(trade(-1.0, "STOP", "LONG", exit_ms=old), symbol="BTCUSDT")
        self.mem.record(trade(4.0, "TARGET", "LONG", exit_ms=NOW), symbol="BTCUSDT")
        self.assertEqual(len(self.mem.recent(5, as_of_ms=NOW)), 1)          # 그 시점 이전 것만
        self.assertEqual(self.mem.recent(5, as_of_ms=NOW)[0]["reason"], "STOP")
        self.assertEqual(len(self.mem.recent(5)), 2)                        # 실시간은 최신

    def test_empty_memory_says_so_instead_of_inventing(self):
        self.assertEqual(self.mem.records(), [])
        self.assertIn("아직 청산한", self.mem.prompt_lines(5)[0])
        self.assertIn("아직 청산한", lessons_text([])[0])

    def test_lesson_of_normalizes_iso_and_millisecond_times(self):
        rec = lesson_of({"side": "SHORT", "reason": "TARGET", "r": 3.95, "pnl": 400,
                         "exit_time": "2026-11-20T01:00:00+00:00"})
        self.assertEqual(rec["side"], "SHORT")
        self.assertEqual(rec["reason_ko"], "목표")
        self.assertTrue(rec["win"])
        self.assertIsInstance(rec["exit_ms"], int)

    def test_reset_clears_both_files(self):
        self.mem.record(trade(1.0, "TARGET"), symbol="BTCUSDT")
        self.mem.reset()
        self.assertEqual(self.mem.records(), [])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "paper_memory.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "memory.md")))


if __name__ == "__main__":
    unittest.main()
