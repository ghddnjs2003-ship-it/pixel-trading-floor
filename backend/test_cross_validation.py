"""교차 검증 도구 테스트 — 시장 사양·표 만들기. 실제 Gemini·거래소는 부르지 않는다."""

import unittest

from backend.cross_validation import MARKETS, conclusion, market_setup, verdict_rows


class MarketSetupTests(unittest.TestCase):
    def test_all_markets_share_the_nominee_rule(self):
        for key in ("btc15", "btc60", "eth30"):
            params, cfg = market_setup(key)
            self.assertEqual(params.stop_mode, "atr")
            self.assertEqual(params.stop_atr, 3.0)
            self.assertEqual(cfg.target_mode, "r")
            self.assertEqual(cfg.target_r, 4.0)
            self.assertEqual(cfg.session_filter, "asia")
            self.assertEqual(tuple(cfg.allow_kinds), ("M1",))
            self.assertEqual(cfg.max_bars, MARKETS[key]["max_bars"])

    def test_prompt_versions_do_not_collide(self):
        versions = [MARKETS[k]["prompt_version"] for k in MARKETS]
        self.assertEqual(len(versions), len(set(versions)))
        # 30분 BTC는 이전 캐시와 같은 버전을 그대로 쓴다(다시 부르지 않게)
        self.assertNotIn("ctx-v2", versions)

    def test_time_limits_match_the_bar_size(self):
        self.assertEqual(MARKETS["btc15"]["max_bars"], 96)   # 15분 × 96 = 24시간
        self.assertEqual(MARKETS["btc60"]["max_bars"], 48)   # 60분 × 48 = 48시간
        self.assertEqual(MARKETS["eth30"]["max_bars"], 96)


def _market(avg_r, pf, net, four_r=(0, 0), worst=None):
    block = lambda r, p, n: {"trades": 10, "win": 40.0, "pf": p, "net_pct": n, "avg_r": r,
                             "mdd": 5.0, "targets": 1, "stops": 5, "times": 4, "boot": {}}
    return {"symbol": "BTCUSDT", "timeframe": "30분봉",
            "baseline": {"all": block(avg_r[0], pf[0], net[0])},
            "filtered": {"all": block(avg_r[1], pf[1], net[1])},
            "reviews": {"count": 60, "reject_pct": 25.0},
            "hit": {"four_r_winners": {"n": four_r[0], "rejected": four_r[1], "reject_pct": None},
                    "worst_decile": {"n": 5, "rejected": worst or 0, "reject_pct": None}}}


class VerdictTests(unittest.TestCase):
    def test_table_and_conclusion(self):
        markets = {"a": _market((0.06, 0.15), (1.07, 1.22), (2.58, 6.16), (4, 0), 2),
                   "b": _market((0.1, -0.1), (1.0, 0.7), (1.0, -3.0), (3, 2), 0)}
        rows = verdict_rows(markets)
        self.assertEqual(len(rows), 2)
        self.assertIn("1.07 → 1.22", rows[0])
        verdict = conclusion(markets)
        self.assertEqual(verdict["improved_markets"], 1)
        self.assertEqual(verdict["not_improved_markets"], 1)
        self.assertAlmostEqual(verdict["markets"][0]["avg_r_delta"], 0.09, places=3)
        self.assertEqual(verdict["markets"][1]["four_r_kept"], "1/3")


if __name__ == "__main__":
    unittest.main()
