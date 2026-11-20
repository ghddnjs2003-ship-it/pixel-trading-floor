"""판정 리포트 아카이브 테스트 — 파일이 실제로 생기고, 입력 상태(read/absent)가 그대로 남는가."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from backend.reports import list_reports, render_markdown, write_report

RUN = {
    "symbol": "BTCUSDT", "display": "BTC", "mode": "algo", "price": 62000.0,
    "model": "gemini-3.5-flash-lite", "action": "HOLD", "confidence": 55,
    "rationale": "1R 앞 저항과 내 계좌 중복 보유로 관망",
    "inputs": {"시세": "읽음(144봉 5분봉)", "경제일정": "읽음(2건)", "뉴스": "없음 — 지어내지 마세요"},
    "portfolio": {"자본USDT": 10000.0, "누적손익USDT": 0.0, "누적손익%": 0.0, "오늘손익USDT": 0.0,
                  "체결수": 0, "승률%": None, "열린포지션": []},
    "regime": {"국면": "정상 범위", "변동성": {"ATR%": 0.42, "ATR%백분위(100봉)": 55},
               "추세": {"4H EMA50 대비": "위"}, "심리": {"공포탐욕": {"값": 12, "이름": "Extreme Fear"}},
               "이벤트": [{"시각KST": "21:30", "제목": "CPI", "중요도": "high"}], "헤드라인": []},
    "lessons": ["최근 3건 1승 2패 · 합 -1.00R"],
    "rule": {"side": None, "kind": None, "label": None, "grade": None, "rr": None, "reasons": ["조건 미충족"]},
    "ai_filter": {"decision": "PASS", "loss_prob": 45, "reason": "1R 이내 장애물 없음"},
    "killswitch": {"ok": True, "reason": "정상"},
    "risk_turns": [{"role": "보수적 리스크 심사자", "report": "데이터 공백이 크다"},
                   {"role": "공격적 리스크 심사자", "report": "4R 승자 흐름은 살아 있다"}],
    "blocked": None,
}


class ReportTests(unittest.TestCase):
    def test_writes_markdown_and_json_with_input_status(self):
        d = tempfile.mkdtemp()
        now = datetime(2026, 11, 20, 12, 30, tzinfo=timezone.utc)
        out = write_report(RUN, out_dir=d, now=now)
        self.assertTrue(out["ok"], out)
        md = Path(out["md"]).read_text(encoding="utf-8")
        self.assertIn("판정 리포트", md)
        self.assertIn("## 입력 상태", md)
        self.assertIn("읽음(2건)", md)
        self.assertIn("없음 — 지어내지 마세요", md)
        self.assertIn("정상 범위", md)
        self.assertIn("최근 3건", md)
        self.assertIn("보수적 리스크 심사자", md)
        self.assertIn("모의 판정입니다", md)
        self.assertIn("2026-11-20-123000-BTC.md", out["md"])
        payload = json.loads(Path(out["json"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["action"], "HOLD")
        self.assertEqual(payload["inputs"]["경제일정"], "읽음(2건)")

    def test_minimal_run_does_not_crash(self):
        md = render_markdown({"symbol": "ETHUSDT", "action": "BUY"})
        self.assertIn("ETHUSDT", md)
        self.assertIn("## 입력 상태", md)

    def test_list_reports_newest_first(self):
        d = tempfile.mkdtemp()
        write_report({**RUN, "display": "BTC"}, out_dir=d,
                     now=datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc))
        write_report({**RUN, "display": "ETH"}, out_dir=d,
                     now=datetime(2026, 11, 20, 11, 0, tzinfo=timezone.utc))
        rows = list_reports(d)
        self.assertEqual(len(rows), 2)
        self.assertIn("ETH", rows[0]["name"])


if __name__ == "__main__":
    unittest.main()
