"""킬스위치 테스트 — 규칙별 중단, 날짜 초기화, 쿨다운 해제, 상태 저장."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from backend.risk import KST, KillSwitch, KillSwitchRules


class FakeClock:
    def __init__(self, start=None):
        self.now = start or datetime(2026, 11, 19, 9, 0, tzinfo=KST)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now = self.now + timedelta(**kw)


class KillSwitchTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.rules = KillSwitchRules(daily_loss_pct=2.0, max_consecutive_losses=3, max_daily_trades=5,
                                     volatility_pause_atr_pct=2.0, cooldown_minutes=30)
        self.ks = KillSwitch(self.rules, state_path=None, now=self.clock)
        self.ks.start_day(1000.0)

    def test_allows_entry_by_default(self):
        self.assertTrue(self.ks.allow_entry()["ok"])

    def test_consecutive_losses_trip(self):
        for pnl in (-10, -10, -10):
            status = self.ks.record_trade(pnl)
        self.assertFalse(self.ks.allow_entry()["ok"])
        self.assertIn("연속", status["state"]["trip_reason"])
        # 30분이 지나면 다시 열린다
        self.clock.advance(minutes=31)
        self.assertTrue(self.ks.allow_entry()["ok"])

    def test_win_resets_loss_streak(self):
        self.ks.record_trade(-5)
        self.ks.record_trade(-5)
        self.ks.record_trade(25)
        self.assertEqual(self.ks.status()["state"]["consecutive_losses"], 0)
        self.assertTrue(self.ks.allow_entry()["ok"])

    def test_daily_loss_limit_trip(self):
        self.ks.record_trade(-25, equity=975.0)     # 2.5% > 2%
        self.assertFalse(self.ks.allow_entry()["ok"])
        self.assertIn("당일 손실", self.ks.status()["state"]["trip_reason"])

    def test_daily_trade_cap(self):
        for _ in range(5):
            self.ks.record_trade(1)
        self.assertFalse(self.ks.allow_entry()["ok"])
        self.assertIn("거래", self.ks.status()["state"]["trip_reason"])

    def test_volatility_pause_uses_cooldown(self):
        tripped = self.ks.check_volatility(atr=3.0, price=100.0)   # 3% > 2%
        self.assertTrue(tripped)
        entry = self.ks.allow_entry()
        self.assertFalse(entry["ok"])
        self.assertIn("급변", entry["reason"])

    def test_new_day_resets_losses(self):
        self.ks.record_trade(-30, equity=970.0)
        self.assertFalse(self.ks.allow_entry()["ok"])
        self.clock.advance(days=1)
        self.assertTrue(self.ks.allow_entry()["ok"])
        self.assertEqual(self.ks.status()["state"]["trades"], 0)

    def test_manual_stop_holds_until_released(self):
        self.ks.manual_stop(True)
        self.clock.advance(hours=10)
        self.assertFalse(self.ks.allow_entry()["ok"])
        self.ks.manual_stop(False)
        self.assertTrue(self.ks.allow_entry()["ok"])

    def test_reset_keeps_manual_stop(self):
        self.ks.manual_stop(True, "점검")
        self.ks.reset()
        self.assertTrue(self.ks.status()["state"]["manual_stop"])
        self.assertFalse(self.ks.allow_entry()["ok"])

    def test_state_file_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state", "killswitch.json")
            ks = KillSwitch(self.rules, state_path=path, now=self.clock)
            ks.start_day(500.0)
            ks.record_trade(-10)
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
            self.assertEqual(saved["realized_pnl"], -10)
            again = KillSwitch(self.rules, state_path=path, now=self.clock)
            self.assertEqual(again.status()["state"]["realized_pnl"], -10)
            self.assertEqual(again.status()["state"]["start_equity"], 500.0)

    def test_broken_state_file_is_reported_not_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "killswitch.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            ks = KillSwitch(self.rules, state_path=path, now=self.clock)
            self.assertIn("읽지 못해", ks.status()["note"])


if __name__ == "__main__":
    unittest.main()
