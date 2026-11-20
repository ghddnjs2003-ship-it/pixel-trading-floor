"""P0-3 테스트 — 시장 국면 요약과 선택적 안전 가드.

핵심 성질
1) 미래 참조 없음(features(candles, i)는 i번 봉까지만 본다)
2) 없는 값은 지어내지 않는다(None / '없음')
3) 가드는 기본 꺼짐 — 검증된 후보 성적을 규칙 변경으로 깎지 않는다
"""

import unittest

from backend import regime
from backend.regime_eval import variants

TF = 1_800_000
START = 1_790_000_000_000


def candles_of(closes, spread=0.4):
    return [{"t": START + i * TF, "o": c, "h": c + spread, "l": c - spread, "c": c, "v": 10.0}
            for i, c in enumerate(closes)]


def trending(n=200, step=1.0, start=100.0):
    return candles_of([start + i * step for i in range(n)])


def flat(n=200, price=100.0):
    return candles_of([price for _ in range(n)])


class FeatureTests(unittest.TestCase):
    def test_features_do_not_look_at_future_candles(self):
        base = trending(200)
        i = 150
        before = regime.features(base, i)
        noisy = [dict(c) for c in base]
        for c in noisy[i + 1:]:                     # 미래 봉만 크게 흔든다
            c["h"] += 50.0
            c["l"] -= 50.0
            c["c"] += 30.0
        after = regime.features(noisy, i)
        self.assertEqual(before, after)

    def test_volatility_ratio_and_position_are_numbers(self):
        f = regime.features(trending(900), 899)
        self.assertIsNotNone(f["ATR%"])
        self.assertIsNotNone(f["ATR비율"])
        self.assertIsNotNone(f["추세강도(EMA간격/ATR)"])
        self.assertGreater(f["48봉위치%"], 95.0)     # 단조 상승 → 범위 최상단
        self.assertIn(f["4H추세"], ("위", "아래"))

    def test_short_history_reports_none_instead_of_guessing(self):
        f = regime.features(trending(20), 19)
        self.assertIsNone(f["ATR%백분위(100봉)"])     # 20봉으로 백분위를 만들지 않는다
        self.assertIsNone(f["추세강도(EMA간격/ATR)"])  # EMA50이 아직 없다

    def test_classify_labels(self):
        self.assertEqual(regime.classify({"ATR%": 2.0}), "급변(변동성 폭발)")
        self.assertEqual(regime.classify({"ATR%": 0.3, "ATR%백분위(100봉)": 5}), "죽은 장(변동성 위축)")
        self.assertEqual(regime.classify({"ATR%": 0.4, "ATR%백분위(100봉)": 50,
                                          "추세강도(EMA간격/ATR)": 1.4}), "추세장")
        self.assertEqual(regime.classify({"ATR%": 0.4, "ATR%백분위(100봉)": 50,
                                          "추세강도(EMA간격/ATR)": 0.1}), "횡보(방향 없음)")
        self.assertEqual(regime.classify({}), "판단보류")


class GuardTests(unittest.TestCase):
    def test_guard_is_off_by_default(self):
        f = regime.features(trending(200), 199)
        out = regime.guard("LONG", {"ATR%": 9.9, "ATR%백분위(100봉)": 99})
        self.assertTrue(out["ok"])
        self.assertFalse(out["active"])
        self.assertIn("꺼짐", out["reason"])

    def test_enabled_guard_blocks_dead_and_panic(self):
        cfg = regime.RegimeGuardConfig(enabled=True, block_dead=True, block_panic=True)
        dead = regime.guard("LONG", {"ATR%": 0.2, "ATR%백분위(100봉)": 4}, cfg)
        self.assertFalse(dead["ok"])
        self.assertIn("위축", dead["reason"])
        panic = regime.guard("LONG", {"ATR%": 2.0, "ATR%백분위(100봉)": 60}, cfg)
        self.assertFalse(panic["ok"])
        self.assertIn("폭발", panic["reason"])
        normal = regime.guard("LONG", {"ATR%": 0.4, "ATR%백분위(100봉)": 50}, cfg)
        self.assertTrue(normal["ok"])

    def test_against_htf_is_opt_in(self):
        f = {"ATR%": 0.4, "ATR%백분위(100봉)": 50, "4H추세": "아래"}
        self.assertTrue(regime.guard("LONG", f, regime.RegimeGuardConfig(enabled=True))["ok"])
        off = regime.guard("LONG", f, regime.RegimeGuardConfig(enabled=True, block_against_htf=True))
        self.assertFalse(off["ok"])
        self.assertTrue(regime.guard("SHORT", f, regime.RegimeGuardConfig(enabled=True,
                                                                          block_against_htf=True))["ok"])


class ExternalInputTests(unittest.TestCase):
    def test_fear_greed_reads_global_json(self):
        self.assertEqual(regime.fear_greed({"live": {"fng": {"value": 12, "label": "Extreme Fear"}}}),
                         {"값": 12, "이름": "Extreme Fear", "기준": None})
        self.assertIsNone(regime.fear_greed({"live": {}}))
        self.assertIsNone(regime.fear_greed({"fng": {"value": "n/a"}}))

    def test_upcoming_events_window_and_order(self):
        now = START
        calendar = {"events": [
            {"atMs": now + 3_600_000, "title": "FOMC", "impact": "high", "time": "10:00"},
            {"atMs": now + 2 * 3_600_000, "title": "CPI", "impact": "low", "time": "11:00"},
            {"atMs": now + 40 * 3_600_000, "title": "먼 일정", "impact": "high", "time": "내일"},
        ]}
        rows = regime.upcoming_events(calendar, now_ms=now, hours=24)
        self.assertEqual([r["제목"] for r in rows], ["FOMC", "CPI"])     # 중요도 먼저
        self.assertEqual(regime.upcoming_events({}, now_ms=now), [])

    def test_summary_marks_inputs_read_or_absent(self):
        c = trending(200)
        s = regime.summary(c, 199, global_json={"live": {"fng": {"value": 40, "label": "Neutral"}}},
                           calendar=None, news=None, now_ms=c[-1]["t"])
        self.assertEqual(s["입력"]["공포탐욕지수"], "읽음")
        self.assertEqual(s["입력"]["경제일정"], "없음")
        self.assertEqual(s["입력"]["뉴스헤드라인"], "없음")
        self.assertEqual(s["심리"]["공포탐욕"]["값"], 40)
        self.assertIn(s["국면"], ("추세장", "정상 범위", "죽은 장(변동성 위축)", "횡보(방향 없음)"))

    def test_summary_never_invents_regime_from_flat_data(self):
        s = regime.summary(flat(200), 199)
        self.assertIsNotNone(s["국면"])


class VariantTests(unittest.TestCase):
    def test_filter_variants_separate_dead_from_panic(self):
        names = [v["name"] for v in variants()]
        self.assertTrue(any("위축" in n for n in names))
        self.assertTrue(any("폭발" in n for n in names))
        dead = next(v for v in variants() if "백분위≤10" in v["name"])
        self.assertTrue(dead["test"]({"ATR%백분위(100봉)": 5}, "LONG"))
        self.assertFalse(dead["test"]({"ATR%백분위(100봉)": 60}, "LONG"))


if __name__ == "__main__":
    unittest.main()
