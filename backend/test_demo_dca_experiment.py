"""데모 전용 리치식 1~4차 물타기(DCA) 실험 테스트 — 규칙이 '말'이 아니라 '동작'인지.

실제 거래소·Gemini를 부르지 않는다(캔들·가짜 AI·mock 클라이언트). 키가 없어도 항상 돈다.
확인하는 것
1) 지표 — RSI Wilder(원본 dca-ind.js 식), 볼린저(20, 2.5σ) 모집단 표준편차, 봉 나이(stale) 조건
2) 차수 판정 — 증거금 %(basis margin) · 조건선/강제선 · 조건이 거짓이면 강제선 전에는 추가 없음
3) 수량·증거금 — 13%/15%/33%/100%, 사용 가능 95% 상한, reserveLevPct(0.48), 최소 주문 수량 거부
4) 익절·자동 손절 — 평단 대비 증거금 +10%, 차수마다 평단 재계산(avgReset), 4차 손절 = 청산가 90%
5) 엔진 — 1차 진입 → 2·3·4차 → 익절/손절/시간 청산, 같은 봉 재진입 금지, 하루 제한, 킬스위치
6) 안전선 — 데모 전용(테스트넷 거부), demo_dca_* 파일만, 다른 엔진 잠금 감지
7) CLI — --status/--once --mock, 경로 오염 거부
"""

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from io import StringIO
from pathlib import Path

from backend.bybit_client import DEMO_BASE_URL, BybitClient, BybitConfigError, OrderRejected
from backend.demo_dca_experiment import (BAR_TF_MS, DcaBybitClient, DCAConfig,
                                         DemoDcaExperiment, adverse_pct, auto_sl_trigger,
                                         bollinger, closed_bars, cond_met, dca_decision,
                                         format_live_line, liq_of, main, margin_amount,
                                         next_bar_close_ms, other_engine_locks,
                                         pct_stop_price, print_banner, qty_for, rsi_wilder,
                                         stage_margin_pct, stage_rule, tech_direction,
                                         tp_price_for, validate_settings)

TF = BAR_TF_MS
START = (1_790_000_000_000 // TF) * TF
ENV_KEYS = ("BYBIT_API_KEY", "BYBIT_API_SECRET", "BYBIT_TESTNET", "BYBIT_DEMO", "BYBIT_ENV",
            "LLM_PROVIDER", "DCA_SYMBOL", "DCA_LEVERAGE", "DCA_FIRST_PCT", "DCA_TP_PCT")


def bars(prices, start=START, wick=0.0002):
    """가격 목록 → 5분봉. 고·저는 ±wick(0.02%) — 0.2% 익절을 우연히 건드리지 않게."""
    out = []
    prev = float(prices[0])
    for i, raw in enumerate(prices):
        price = float(raw)
        out.append({"t": start + i * TF, "o": prev, "h": max(prev, price) * (1 + wick),
                    "l": min(prev, price) * (1 - wick), "c": price, "v": 1.0})
        prev = price
    return out


def flat(n=120, price=2000.0):
    return bars([price] * n)


def now_after(candles):
    return int(candles[-1]["t"]) + TF + 5_000


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = {key: os.environ.pop(key, None) for key in ENV_KEYS}
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is not None:
                os.environ[key] = value

    def cfg(self, **kw):
        base = dict(symbol="ETHUSDT", leverage=50.0, first_pct=13.0, tp_pct=10.0,
                    force_entry="long", max_hold_bars=10_000, data_dir=self.dir,
                    state_path=os.path.join(self.dir, "demo_dca_state.mock.json"),
                    log_path=os.path.join(self.dir, "demo_dca_log.mock.jsonl"),
                    killswitch_path=os.path.join(self.dir, "demo_dca_killswitch.mock.json"))
        base.update(kw)
        return DCAConfig(**base)

    def engine(self, cfg=None, client=None):
        client = client or DcaBybitClient(api_key="", api_secret="", env="demo")
        return DemoDcaExperiment(cfg or self.cfg(), client=client)

    def tick(self, eng, candles, candles_4h=None):
        return eng.tick(candles=candles, candles_4h=candles_4h or candles,
                        now_ms=now_after(candles))


class TestIndicators(Base):
    def test_rsi_wilder_matches_wilder_definition(self):
        series = [100.0 + (i % 7) * 0.3 - (i % 5) * 0.2 for i in range(99)]
        self.assertAlmostEqual(rsi_wilder(series, 14), 42.85427054935078, places=9)

    def test_rsi_edges(self):
        self.assertEqual(rsi_wilder([123.45] * 120, 14), 50.0)
        self.assertEqual(rsi_wilder([100 + i for i in range(120)], 14), 100.0)
        self.assertEqual(rsi_wilder([200 - i for i in range(120)], 14), 0.0)
        self.assertIsNone(rsi_wilder([100.0] * 98, 14))       # 99개가 안 되면 모름
        self.assertIsNone(rsi_wilder([100.0] * 120, 1))       # len < 2

    def test_bollinger_population_sd(self):
        b = bollinger([1, 2, 3, 4, 5], 5, 2.5)
        self.assertAlmostEqual(b["mid"], 3.0)
        self.assertAlmostEqual(b["sd"], 1.4142135623730951)   # 모집단(÷5)
        self.assertAlmostEqual(b["upper"], 3.0 + 2.5 * 1.4142135623730951)
        self.assertAlmostEqual(b["lower"], 3.0 - 2.5 * 1.4142135623730951)
        self.assertIsNone(bollinger([1, 2], 5, 2.5))

    def test_cond_rsi_and_bb(self):
        rsi_rule = stage_rule(DCAConfig(), 2)
        falling = bars([2000 - i * 0.5 for i in range(120)])
        closes = [b["c"] for b in falling]
        self.assertTrue(cond_met(rsi_rule, 1, closes, closes[-1], falling[-1]["t"], now_after(falling)))
        self.assertFalse(cond_met(rsi_rule, -1, closes, closes[-1], falling[-1]["t"], now_after(falling)))
        rising = bars([2000 + i * 0.5 for i in range(120)])
        closes_up = [b["c"] for b in rising]
        self.assertTrue(cond_met(rsi_rule, -1, closes_up, closes_up[-1], rising[-1]["t"], now_after(rising)))
        bb_rule = stage_rule(DCAConfig(), 3)
        flat_bars = flat(120, 2000.0)
        flat_closes = [b["c"] for b in flat_bars]
        # 폭 0인 평평한 봉 — 2001은 상단 위(숏 참), 1999는 하단 아래(롱 참)
        self.assertFalse(cond_met(bb_rule, 1, flat_closes, 2001.0, flat_bars[-1]["t"], now_after(flat_bars)))
        self.assertTrue(cond_met(bb_rule, -1, flat_closes, 2001.0, flat_bars[-1]["t"], now_after(flat_bars)))
        self.assertTrue(cond_met(bb_rule, 1, flat_closes, 1999.0, flat_bars[-1]["t"], now_after(flat_bars)))
        # 최근 20봉이 흔들리고 현재가가 하단 아래면 참
        noisy = flat(120, 2000.0)
        for i in range(100, 120):
            noisy[i]["c"] = 2000.0 + (5.0 if i % 2 else -5.0)
            noisy[i]["h"] = noisy[i]["c"] + 1
            noisy[i]["l"] = noisy[i]["c"] - 1
        noisy_closes = [b["c"] for b in noisy]
        lower = bollinger(noisy_closes, 20, 2.5)["lower"]
        self.assertTrue(cond_met(bb_rule, 1, noisy_closes, lower - 1.0, noisy[-1]["t"], now_after(noisy)))
        self.assertFalse(cond_met(bb_rule, -1, noisy_closes, lower - 1.0, noisy[-1]["t"], now_after(noisy)))

    def test_cond_is_false_when_bar_is_stale_or_shape_unknown(self):
        rule = stage_rule(DCAConfig(), 2)
        candles = bars([2000 - i * 0.5 for i in range(120)])
        closes = [b["c"] for b in candles]
        last = candles[-1]["t"]
        # 마지막 완성봉이 닫힌 지 10분이 넘으면 거짓
        self.assertFalse(cond_met(rule, 1, closes, closes[-1], last, last + TF + 600_001))
        self.assertTrue(cond_met(rule, 1, closes, closes[-1], last, last + TF + 599_000))
        # 봉 시각을 모르면 거짓
        self.assertFalse(cond_met(rule, 1, closes, closes[-1], None, last))
        # 모르는 조건 꼴은 거짓 — 공용 기본 차수 표를 건드리지 않게 복사본을 쓴다
        import dataclasses
        weird = dataclasses.replace(stage_rule(DCAConfig(), 2), cond="macd")
        self.assertFalse(cond_met(weird, 1, closes, closes[-1], last, now_after(candles)))

    def test_closed_bars_drops_running_bar(self):
        candles = flat(10)
        self.assertEqual(len(closed_bars(candles, TF, candles[-1]["t"] + TF - 1)), 9)
        self.assertEqual(len(closed_bars(candles, TF, candles[-1]["t"] + TF)), 10)


class TestStageRules(Base):
    def test_adverse_pct_is_margin_based(self):
        # 롱 50배에서 가격 -0.5% = 증거금 +25% 불리(양수 = 손실)
        self.assertAlmostEqual(adverse_pct(1, 2000.0, 1990.0, 50.0), 25.0)
        self.assertAlmostEqual(adverse_pct(1, 2000.0, 2010.0, 50.0), -25.0)
        # 숏은 반대
        self.assertAlmostEqual(adverse_pct(-1, 2000.0, 2010.0, 50.0), 25.0)

    def test_dca_decision_cond_and_force(self):
        rule = stage_rule(DCAConfig(), 2)      # cond 25 / force 40
        self.assertIsNone(dca_decision(24.9, rule, True))
        self.assertIsNone(dca_decision(30.0, rule, False))     # 조건선에 닿아도 조건이 거짓이면 대기
        self.assertEqual(dca_decision(30.0, rule, True), "cond")
        self.assertEqual(dca_decision(25.0, rule, True), "cond")
        self.assertEqual(dca_decision(40.0, rule, False), "force")
        self.assertEqual(dca_decision(39.999, rule, False), None)
        self.assertIsNone(dca_decision(float("nan"), rule, True))

    def test_stage_margin_pct_and_reserve(self):
        cfg = DCAConfig()
        s4 = stage_rule(cfg, 4)
        self.assertEqual(stage_margin_pct(s4, 50.0), 100.0)                 # 기본: 남은 가용 100%
        self.assertEqual(stage_margin_pct(s4, 50.0, 0.48), 76.0)           # 원본 reserveLevPct
        self.assertEqual(stage_margin_pct(stage_rule(cfg, 2), 50.0), 15.0)
        self.assertEqual(stage_margin_pct(stage_rule(cfg, 3), 50.0), 33.0)

    def test_margin_amount_caps_at_95_percent_of_available(self):
        self.assertAlmostEqual(margin_amount(50.0, 13.0, 50.0), 6.5)
        self.assertAlmostEqual(margin_amount(50.0, 100.0, 50.0), 47.5)     # 95% 상한
        self.assertAlmostEqual(margin_amount(0.0, 100.0, 50.0), 0.0)
        self.assertAlmostEqual(margin_amount(50.0, 0.0, 50.0), 0.0)
        # base가 가용보다 작으면 base가 먼저 걸린다
        self.assertAlmostEqual(margin_amount(20.0, 100.0, 50.0), 20.0)

    def test_qty_for_floors_and_refuses_below_minimum(self):
        eth = DcaBybitClient.get_instrument(DcaBybitClient(api_key="", api_secret="", env="demo"),
                                            "ETHUSDT")
        self.assertEqual(eth.min_order_qty, Decimal("0.01"))
        qty = qty_for(6.5, 50.0, 2000.0, eth)          # 325 / 2000 = 0.1625 → 0.16
        self.assertEqual(qty, Decimal("0.16"))
        btc = DcaBybitClient.get_instrument(DcaBybitClient(api_key="", api_secret="", env="demo"),
                                            "BTCUSDT")
        with self.assertRaises(OrderRejected) as ctx:
            qty_for(0.5, 50.0, 60_000.0, btc)          # 25 / 60000 = 0.0004 → 0
        self.assertIn("최소 주문 수량", str(ctx.exception))
        with self.assertRaises(OrderRejected):
            qty_for(0.0, 50.0, 2000.0, eth)
        self.assertEqual(qty_for(1.0, 50.0, 2000.0, eth), Decimal("0.02"))   # 50/2000 = 0.025 → 내림

    def test_tp_price_is_margin_percent(self):
        tick = Decimal("0.01")
        # 50배 · 증거금 +10% → 가격 +0.2%
        self.assertEqual(tp_price_for(1, 2000.0, 10.0, 50.0, tick), Decimal("2004.00"))
        self.assertEqual(tp_price_for(-1, 2000.0, 10.0, 50.0, tick), Decimal("1996.00"))

    def test_pct_stop_rounds_toward_average(self):
        tick = Decimal("0.01")
        self.assertEqual(pct_stop_price(1, 2000.0, 50.0, 50.0, tick), Decimal("1980.00"))
        self.assertEqual(pct_stop_price(-1, 2000.0, 50.0, 50.0, tick), Decimal("2020.00"))

    def test_liquidation_estimate_and_auto_sl(self):
        est = liq_of(1, 2000.0, 50.0)
        self.assertEqual(est["src"], "estimate")
        self.assertAlmostEqual(est["liq"], 2000.0 * (1 - (1 / 50 - 0.0038)))
        trigger = auto_sl_trigger(1, 2000.0, est["liq"], est["src"], Decimal("0.01"))
        expected = 2000.0 + 0.9 * (est["liq"] - 2000.0)
        self.assertAlmostEqual(float(trigger), expected, places=2)
        self.assertLess(float(trigger), 2000.0)
        self.assertGreater(float(trigger), est["liq"])
        # 추정 청산가 원본 기본 80%도 지원
        t80 = auto_sl_trigger(1, 2000.0, est["liq"], "estimate", Decimal("0.01"),
                              dist_pct=90, est_dist_pct=80)
        self.assertGreater(float(t80), float(trigger))          # 80%는 청산가에 더 가깝다
        # 거래소 청산가를 주면 90%
        ex = liq_of(1, 2000.0, 50.0, 1966.0)
        self.assertEqual(ex["src"], "exchange")
        self.assertEqual(float(ex["liq"]), 1966.0)

    def test_validate_settings(self):
        validate_settings(DCAConfig())
        for bad in (dict(leverage=51), dict(leverage=0.5), dict(first_pct=31), dict(tp_pct=4),
                    dict(sl_pct=80), dict(sl_mode="x"), dict(source="binance"),
                    dict(max_trades_per_day=0), dict(force_entry="up"),
                    dict(min_equity_pct=100)):
            with self.assertRaises(ValueError):
                validate_settings(DCAConfig(**bad))


class TestOrderClient(Base):
    def client(self):
        return DcaBybitClient(api_key="", api_secret="", env="demo")

    def test_demo_only_base_url_and_leverage_cap(self):
        client = self.client()
        self.assertEqual(client.base_url, DEMO_BASE_URL)
        self.assertTrue(client.sandbox)
        self.assertEqual(DcaBybitClient.validate_leverage(50), 50.0)
        with self.assertRaises(ValueError):
            DcaBybitClient.validate_leverage(51)
        # 부모(기존 안전 전략)의 5배 상한은 그대로다 — 이 실험이 기존 규칙을 풀지 않았다
        with self.assertRaises(ValueError):
            BybitClient.validate_leverage(10)

    def test_mock_orders_average_the_position(self):
        client = self.client()
        client.set_mock_price("ETHUSDT", 2000.0)
        first = client.place_dca_order("ETHUSDT", "Buy", Decimal("0.16"))
        self.assertTrue(first["mock"])
        pos = client.get_position("ETHUSDT")
        self.assertEqual(pos["size"], 0.16)
        self.assertEqual(pos["entryPrice"], 2000.0)
        client.set_mock_price("ETHUSDT", 1900.0)
        client.place_dca_order("ETHUSDT", "Buy", Decimal("0.16"))
        pos = client.get_position("ETHUSDT")
        self.assertAlmostEqual(pos["size"], 0.32)
        self.assertAlmostEqual(pos["entryPrice"], 1950.0)
        self.assertGreater(pos["liqPrice"], 0)
        # 반대 방향 추가는 거부된다(한 방향만 물타기)
        with self.assertRaises(OrderRejected):
            client.place_dca_order("ETHUSDT", "Sell", Decimal("0.1"))

    def test_set_trading_stop_mock(self):
        client = self.client()
        client.place_dca_order("ETHUSDT", "Sell", Decimal("0.1"))
        client.set_trading_stop("ETHUSDT", take_profit=1990.0, stop_loss=2030.0)
        pos = client.get_position("ETHUSDT")
        self.assertEqual(pos["takeProfit"], 1990.0)
        self.assertEqual(pos["stopLoss"], 2030.0)

    def test_close_position_mock_realises_pnl(self):
        client = self.client()
        client.set_mock_price("ETHUSDT", 2000.0)
        client.place_dca_order("ETHUSDT", "Buy", Decimal("0.1"))
        client.set_mock_price("ETHUSDT", 2010.0)
        result = client.close_position("ETHUSDT")
        self.assertEqual(result["mock"], True)
        self.assertIsNone(client.get_position("ETHUSDT"))


class TestEntry(Base):
    def test_long_entry_margin_tp_and_no_stop(self):
        eng = self.engine()
        candles = flat(120)
        rec = self.tick(eng, candles)
        self.assertEqual(rec["action"], "OPEN", rec["events"])
        pos = rec["position"]
        self.assertEqual(pos["side"], "Buy")
        self.assertEqual(pos["stage"], 1)
        # 50달러 × 13% = 6.5 증거금 → 6.5×50/2000 = 0.1625 → 0.16개
        self.assertAlmostEqual(pos["qty"], 0.16)
        self.assertAlmostEqual(pos["margin"], 6.4, places=2)
        # 익절 = 평단 +0.2% (증거금 +10%)
        self.assertAlmostEqual(pos["tp"], 2004.0, places=2)
        self.assertIsNone(pos["stop"])                      # 1~3차에는 손절 없음
        self.assertEqual(eng.load_state()["day"]["entries"], 1)
        self.assertEqual(eng.killswitch.state["trades"], 0)  # 아직 청산 없음

    def test_short_entry(self):
        eng = self.engine(self.cfg(force_entry="short"))
        rec = self.tick(eng, flat(120))
        self.assertEqual(rec["action"], "OPEN")
        self.assertEqual(rec["position"]["side"], "Sell")
        self.assertAlmostEqual(rec["position"]["tp"], 1996.0, places=2)

    def test_same_bar_does_not_open_twice(self):
        eng = self.engine()
        candles = flat(120)
        first = self.tick(eng, candles)
        self.assertEqual(first["action"], "OPEN")
        second = eng.tick(candles=candles, candles_4h=candles, now_ms=now_after(candles) + 30_000)
        self.assertNotEqual(second["action"], "OPEN")
        self.assertEqual(len(eng.load_state()["closed"]), 0)

    def test_ai_hold_blocks_entry(self):
        class HoldAI:
            def describe(self):
                return {"provider": "fake", "model": "fake"}

            def decide(self, context, cfg):
                return {"decision": "HOLD", "reason": "방향 불명", "model": "fake"}

        # 기술 필터는 상승 추세라 LONG을 낼 봉 — AI의 분명한 HOLD가 그것을 뒤집지 않아야 한다
        eng = self.engine(self.cfg(force_entry="", entry_mode="auto"))
        eng.ai = HoldAI()
        rec = self.tick(eng, bars([2000 + i * 1.0 for i in range(120)]))
        self.assertEqual(rec["action"], "HOLD")
        self.assertIsNone(rec["position"])
        self.assertIn("방향 불명", rec["events"][0])

    def test_ai_failure_falls_back_to_tech_filter(self):
        class BoomAI:
            def describe(self):
                return {"error": "no key"}

            def decide(self, context, cfg):
                raise RuntimeError("boom")

        eng = self.engine(self.cfg(force_entry="", entry_mode="auto"))
        eng.ai = BoomAI()
        rec = self.tick(eng, bars([2000 + i * 1.0 for i in range(120)]))
        self.assertEqual(rec["action"], "OPEN", rec["events"])
        self.assertEqual(rec["position"]["side"], "Buy")
        self.assertIn("기술", " ".join(rec["events"]))

    def test_paused_killswitch_and_daily_cap(self):
        eng = self.engine()
        Path(eng.pause_path).write_text("stop", encoding="utf-8")
        rec = self.tick(eng, flat(120))
        self.assertEqual(rec["action"], "SKIP")
        self.assertIn("수동 정지", rec["events"][0])
        os.remove(eng.pause_path)
        eng.killswitch.manual_stop(True, "시험")
        rec2 = self.tick(eng, flat(120))
        self.assertEqual(rec2["action"], "SKIP")
        self.assertIn("킬스위치", rec2["events"][0])

    def test_adopts_own_order_that_appears_one_tick_late(self):
        class BlindClient(DcaBybitClient):
            """주문 직후 한 번은 포지션이 안 보이는 거래소(체결 반영 지연)를 흉내 낸다."""

            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.blind = 0

            def place_dca_order(self, symbol, side, qty, reduce_only=False):
                result = super().place_dca_order(symbol, side, qty, reduce_only)
                self.blind = 1
                return result

            def get_position(self, symbol):
                if self.blind > 0:
                    self.blind -= 1
                    return None
                return super().get_position(symbol)

        client = BlindClient(api_key="", api_secret="", env="demo")
        eng = self.engine(client=client)
        candles = flat(120, 2000.0)
        first = self.tick(eng, candles)
        self.assertEqual(first["action"], "ERROR", first["events"])
        state = eng.load_state()
        self.assertIsNone(state["position"])
        self.assertTrue(state.get("orphan_candidate"))
        # 다음 점검 — 포지션이 보이면 이어받고 익절을 건다
        second = eng.tick(candles=candles, candles_4h=candles, now_ms=now_after(candles) + 30_000)
        self.assertIn(second["action"], ("WAIT", "HOLD", "ADD"), second["events"])
        state = eng.load_state()
        self.assertIsNotNone(state["position"])
        self.assertEqual(state["position"]["stage"], 1)
        self.assertIsNotNone(state["position"]["tp"])
        self.assertIsNone(state.get("orphan_candidate"))
        self.assertEqual(state["day"]["entries"], 1)

    def test_minimum_order_refusal_has_numbers(self):
        eng = self.engine(self.cfg(symbol="BTCUSDT", first_pct=1.0, force_entry="long"))
        eng.client.set_mock_price("BTCUSDT", 60_000.0)
        candles = flat(120, 60_000.0)
        rec = self.tick(eng, candles)
        self.assertEqual(rec["action"], "SKIP")
        self.assertIn("최소 주문 수량", " ".join(rec["events"]))


class TestDcaStages(Base):
    def open_long(self, eng):
        candles = flat(120, 2000.0)
        rec = self.tick(eng, candles)
        self.assertEqual(rec["action"], "OPEN", rec["events"])
        return candles

    def test_force_stage2_at_minus_40_margin(self):
        eng = self.engine()
        candles = self.open_long(eng)
        candles = candles + bars([1984.0], start=START + 120 * TF)   # -0.8% → 증거금 -40%
        rec = self.tick(eng, candles)
        self.assertEqual(rec["action"], "ADD", rec["events"])
        pos = rec["position"]
        self.assertEqual(pos["stage"], 2)
        self.assertGreater(pos["qty"], 0.16)
        # 새 평단에서 익절을 다시 계산한다(avgReset)
        self.assertAlmostEqual(pos["avg"], 1992.0, places=2)
        self.assertAlmostEqual(pos["tp"], 1995.98, places=2)
        self.assertIsNone(pos["stop"])
        self.assertEqual(eng.load_state()["day"]["adds"], 1)

    def test_cond_stage2_needs_rsi(self):
        eng = self.engine()
        # 완만한 하락으로 RSI를 낮춰 둔 뒤 -25% 지점에서 추가
        prices = [2000 - i * 0.5 for i in range(120)]
        candles = bars(prices)
        rec = self.tick(eng, candles)
        self.assertEqual(rec["action"], "OPEN", rec["events"])
        last = prices[-1]
        target = last * (1 - 0.005)          # 50배에서 가격 -0.5% = 증거금 -25%
        candles = candles + bars([target], start=START + 120 * TF)
        rec2 = self.tick(eng, candles)
        self.assertEqual(rec2["action"], "ADD", rec2["events"])
        self.assertIn("조건 충족", " ".join(rec2["events"]))
        self.assertEqual(rec2["position"]["stage"], 2)

    def test_no_add_between_cond_and_force_without_condition(self):
        eng = self.engine(self.cfg(force_entry="short"))
        # 상승 추세 → 숏은 불리, RSI는 과매수(>65)라 숏 조건은 참일 수 있다.
        # 조건이 거짓인 상황을 만들기 위해 조용한 하락 뒤 반등을 쓴다.
        prices = [2000 - i * 0.2 for i in range(110)] + [1978.0 - i * 0.05 for i in range(10)]
        candles = bars(prices)
        rec = self.tick(eng, candles)
        self.assertEqual(rec["action"], "OPEN", rec["events"])
        # -30% (조건선 25% 위, 강제선 40% 아래) — RSI 조건이 거짓이면 추가하지 않는다
        target = float(candles[-1]["c"]) * (1 + 0.006)
        candles = candles + bars([target], start=START + 120 * TF)
        rec2 = self.tick(eng, candles)
        if rec2["action"] == "ADD":
            self.assertNotIn("대기", " ".join(rec2["events"]))
        else:
            self.assertEqual(rec2["action"], "WAIT", rec2["events"])
            self.assertIn("대기", " ".join(rec2["events"]))

    def test_full_path_to_stage4_and_auto_stop(self):
        eng = self.engine()
        candles = self.open_long(eng)
        for i, price in enumerate((1984.0, 1968.0, 1950.0)):
            candles = candles + bars([price], start=START + (120 + i) * TF)
            rec = self.tick(eng, candles)
            self.assertEqual(rec["action"], "ADD", rec["events"])
        state = eng.load_state()
        pos = state["position"]
        self.assertEqual(pos["stage"], 4)
        # 4차 뒤 자동 손절 — 평단→추정 청산가 거리의 90%, 청산가보다 2 tick 안쪽
        self.assertIsNotNone(pos["stop"])
        liq = liq_of(1, pos["avg"], 50.0)["liq"]
        self.assertLess(pos["stop"], pos["avg"])
        self.assertGreater(pos["stop"], liq)
        self.assertAlmostEqual((pos["avg"] - pos["stop"]) / (pos["avg"] - liq), 0.90, places=2)
        ex = eng.client.get_position("ETHUSDT")
        self.assertAlmostEqual(ex["stopLoss"], pos["stop"], places=2)
        # 4차 증거금 = 남은 가용의 100%(사용 가능 95% 상한) → 총 증거금이 시드의 대부분
        self.assertGreater(pos["margin"], 40.0)
        self.assertLess(pos["margin"], 50.0)

    def test_reserve_lev_pct_reduces_stage4(self):
        eng = self.engine(self.cfg(reserve_lev_pct=0.48))
        candles = self.open_long(eng)
        for i, price in enumerate((1984.0, 1968.0, 1950.0)):
            candles = candles + bars([price], start=START + (120 + i) * TF)
            self.tick(eng, candles)
        pos = eng.load_state()["position"]
        self.assertEqual(pos["stage"], 4)
        # 100% − 50×0.48 = 76% → 4차 증거금이 95% 상한(기본)보다 작다
        self.assertLess(pos["margin"], 45.0)

    def test_stage4_when_stop_is_already_passed_closes_next_tick(self):
        eng = self.engine()
        candles = self.open_long(eng)
        for i, price in enumerate((1984.0, 1968.0, 1900.0)):
            candles = candles + bars([price], start=START + (120 + i) * TF)
            self.tick(eng, candles)
        self.assertTrue(eng.load_state()["position"].get("force_close"))
        candles = candles + bars([1895.0], start=START + 123 * TF)
        rec = self.tick(eng, candles)
        self.assertEqual(rec["action"], "CLOSE", rec["events"])
        self.assertIsNone(rec["position"])
        trade = eng.load_state()["closed"][-1]
        self.assertEqual(trade["reason"], "STOP")
        self.assertEqual(trade["stage"], 4)

    def test_stage_margins_follow_table(self):
        eng = self.engine()
        candles = self.open_long(eng)
        seen = []
        for i, price in enumerate((1984.0, 1968.0, 1940.0)):
            candles = candles + bars([price], start=START + (120 + i) * TF)
            rec = self.tick(eng, candles)
            seen.append((rec["position"]["stage"], rec["position"]["margin"]))
        self.assertEqual([s for s, _ in seen], [2, 3, 4])
        self.assertLess(seen[0][1], seen[1][1])
        self.assertLess(seen[1][1], seen[2][1])


class TestClose(Base):
    def test_take_profit_closes_and_records(self):
        eng = self.engine()
        candles = flat(120, 2000.0)
        self.assertEqual(self.tick(eng, candles)["action"], "OPEN")
        candles = candles + bars([2004.0], start=START + 120 * TF)
        rec = self.tick(eng, candles)
        self.assertEqual(rec["action"], "CLOSE", rec["events"])
        state = eng.load_state()
        self.assertIsNone(state["position"])
        trade = state["closed"][-1]
        self.assertEqual(trade["reason"], "TARGET")
        self.assertAlmostEqual(trade["pnl"], 0.64, places=2)      # 0.16개 × 4.0
        self.assertAlmostEqual(trade["margin_pct"], 10.0, places=0)
        self.assertEqual(state["stats"]["targets"], 1)
        self.assertEqual(eng.killswitch.state["trades"], 1)
        self.assertEqual(eng.killswitch.state["realized_pnl"], 0.64)

    def test_stop_after_stage4_records_loss(self):
        eng = self.engine()
        candles = flat(120, 2000.0)
        self.tick(eng, candles)
        for i, price in enumerate((1984.0, 1968.0, 1945.0)):
            candles = candles + bars([price], start=START + (120 + i) * TF)
            self.tick(eng, candles)
        candles = candles + bars([1930.0], start=START + 123 * TF)
        rec = self.tick(eng, candles)
        self.assertEqual(rec["action"], "CLOSE", rec["events"])
        trade = eng.load_state()["closed"][-1]
        self.assertEqual(trade["reason"], "STOP")
        self.assertLess(trade["pnl"], 0)
        self.assertLess(trade["margin_pct"], -20)

    def test_time_exit(self):
        eng = self.engine(self.cfg(max_hold_bars=2))
        candles = flat(120, 2000.0)
        self.tick(eng, candles)
        for i in range(3):
            candles = candles + bars([2000.0], start=START + (120 + i) * TF)
            rec = self.tick(eng, candles)
        self.assertEqual(rec["action"], "CLOSE", rec["events"])
        self.assertEqual(eng.load_state()["closed"][-1]["reason"], "TIME")

    def test_no_reentry_on_same_bar_after_close(self):
        eng = self.engine()
        candles = flat(120, 2000.0)
        self.tick(eng, candles)
        candles = candles + bars([2004.0], start=START + 120 * TF)
        self.assertEqual(self.tick(eng, candles)["action"], "CLOSE")
        again = eng.tick(candles=candles, candles_4h=candles, now_ms=now_after(candles) + 30_000)
        self.assertNotEqual(again["action"], "OPEN")

    def test_daily_trade_cap_blocks_next_cycle(self):
        eng = self.engine(self.cfg(max_trades_per_day=1))
        candles = flat(120, 2000.0)
        self.tick(eng, candles)
        candles = candles + bars([2004.0], start=START + 120 * TF)
        self.assertEqual(self.tick(eng, candles)["action"], "CLOSE")
        candles = candles + bars([2000.0], start=START + 121 * TF)
        rec = self.tick(eng, candles)
        self.assertEqual(rec["action"], "SKIP")
        self.assertTrue("하루 최대" in " ".join(rec["events"])
                        or "당일 거래" in " ".join(rec["events"]), rec["events"])


class TestGuards(Base):
    def test_engine_refuses_testnet(self):
        with self.assertRaises(BybitConfigError):
            DemoDcaExperiment(self.cfg(), client=DcaBybitClient(
                api_key="", api_secret="", env="testnet"))

    def test_paths_must_be_demo_dca(self):
        for bad in ("paper_state.json", "testnet_auto_state.json", "demo_ai_experiment_state.json",
                    "ai_cache.json"):
            with self.assertRaises(ValueError):
                DemoDcaExperiment(self.cfg(state_path=os.path.join(self.dir, bad)),
                                  client=DcaBybitClient(api_key="", api_secret="", env="demo"))
        for weird in ("state.json", "dca_state.json"):
            with self.assertRaises(ValueError):
                DemoDcaExperiment(self.cfg(state_path=os.path.join(self.dir, weird)),
                                  client=DcaBybitClient(api_key="", api_secret="", env="demo"))

    def test_default_files_are_isolated(self):
        eng = self.engine(self.cfg(state_path="", log_path="", killswitch_path=""))
        names = [Path(eng.state_path).name, Path(eng.log_path).name,
                 Path(eng.killswitch_path).name, Path(eng.pause_path).name]
        self.assertTrue(all(n.startswith("demo_dca_") for n in names), names)
        self.assertTrue(all("." + "mock" in n for n in names[:3]), names)
        for name in names:
            for prefix in ("paper_", "testnet_auto", "demo_ai_experiment"):
                self.assertFalse(name.startswith(prefix))

    def test_other_engine_locks(self):
        lock = Path(self.dir) / "testnet_auto_state.json.lock"
        lock.write_text("1", encoding="utf-8")
        self.assertIn(str(lock), other_engine_locks(self.dir))
        self.assertEqual(other_engine_locks(self.dir, stale_sec=0), [])
        ai_lock = Path(self.dir) / "demo_ai_experiment_state.mock.json.lock"
        ai_lock.write_text("1", encoding="utf-8")
        self.assertEqual(len(other_engine_locks(self.dir)), 2)

    def test_dca_lock_is_separate_from_others(self):
        eng = self.engine()
        self.assertTrue(eng.state_path.endswith(".mock.json"))
        self.assertEqual(eng.state_path + ".lock", eng.state_path + ".lock")
        self.assertNotIn("testnet_auto", eng.state_path)
        self.assertNotIn("demo_ai_experiment", eng.state_path)


class TestStateAndCli(Base):
    def test_state_roundtrip(self):
        eng = self.engine()
        candles = flat(120, 2000.0)
        self.tick(eng, candles)
        raw = json.loads(Path(eng.state_path).read_text(encoding="utf-8"))
        self.assertEqual(raw["kind"], "demo_dca_experiment")
        self.assertEqual(raw["symbol"], "ETHUSDT")
        self.assertEqual(raw["rules"]["basis"], "margin")
        self.assertEqual([s["stage"] for s in raw["rules"]["stages"]], [2, 3, 4])
        self.assertTrue(raw["rules"]["sandboxOnly"])
        again = eng.load_state()
        self.assertEqual(again["position"]["stage"], 1)
        self.assertAlmostEqual(again["position"]["qty"], 0.16)

    def test_mock_position_vanishes_with_process(self):
        eng = self.engine()
        candles = flat(120, 2000.0)
        self.tick(eng, candles)
        # 새 프로세스 = 새 mock 계좌(포지션 없음) → 유령 포지션을 청산 기록으로 남기지 않는다
        eng2 = self.engine()
        state = eng2.load_state()
        self.assertIsNone(state["position"])
        self.assertIn("mock", state.get("note") or "")
        self.assertEqual(state["closed"], [])

    def test_log_throttles_waits(self):
        eng = self.engine()
        candles = flat(120, 2000.0)
        self.tick(eng, candles)
        first = len(Path(eng.log_path).read_text(encoding="utf-8").strip().splitlines())
        for _ in range(3):
            eng.tick(candles=candles, candles_4h=candles, now_ms=now_after(candles) + 30_000)
        second = len(Path(eng.log_path).read_text(encoding="utf-8").strip().splitlines())
        self.assertEqual(first, second)

    def test_status_cli(self):
        buf = StringIO()
        with redirect_stdout(buf):
            code = main(["--status", "--mock", "--data-dir", self.dir])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("EXPERIMENTAL RICH-STYLE", out)
        self.assertIn("2차: 증거금 -25%", out)
        self.assertIn("모드 mock", out)

    def test_env_overrides_leverage_and_symbol(self):
        os.environ["DCA_LEVERAGE"] = "25"
        os.environ["DCA_SYMBOL"] = "SOLUSDT"
        buf = StringIO()
        with redirect_stdout(buf):
            code = main(["--status", "--mock", "--data-dir", self.dir])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("25배", out)
        self.assertIn("SOLUSDT", out)

    def test_once_cli_with_stub_fetch(self):
        candles = flat(120, 2000.0)
        orig = DemoDcaExperiment._fetch_default
        DemoDcaExperiment._fetch_default = lambda self: {"source": "stub", "candles": candles,
                                                         "candles_4h": candles}
        try:
            buf = StringIO()
            with redirect_stdout(buf):
                code = main(["--once", "--mock", "--force-entry", "long", "--data-dir", self.dir])
        finally:
            DemoDcaExperiment._fetch_default = orig
        self.assertEqual(code, 0)
        state = json.loads(Path(self.dir, "demo_dca_state.mock.json").read_text(encoding="utf-8"))
        self.assertIsNotNone(state["position"])
        self.assertEqual(state["position"]["stage"], 1)
        self.assertEqual(state["last_scan"]["action"], "OPEN")

    def test_bad_state_path_cli_refuses(self):
        buf = StringIO()
        with redirect_stdout(buf):
            code = main(["--once", "--mock", "--data-dir", self.dir,
                         "--state", os.path.join(self.dir, "paper_state.json")])
        self.assertEqual(code, 2)
        self.assertIn("demo_dca_", buf.getvalue())

    def test_reset_clears_local_state(self):
        eng = self.engine()
        candles = flat(120, 2000.0)
        self.tick(eng, candles)
        eng.client._mock_positions.clear()
        eng.reset()
        self.assertIsNone(eng.load_state()["position"])
        self.assertEqual(eng.load_state()["closed"], [])


class TestReplaySafety(Base):
    def test_tech_filter_direction(self):
        rising = bars([2000 + i * 1.0 for i in range(120)])
        rec = tech_direction(rising, rising, rising[-1]["c"])
        self.assertEqual(rec["decision"], "LONG")
        self.assertGreaterEqual(rec["score"], 2)
        falling = bars([2200 - i * 1.0 for i in range(120)])
        rec = tech_direction(falling, falling, falling[-1]["c"])
        self.assertEqual(rec["decision"], "SHORT")
        flat_bars = flat(120, 2000.0)
        self.assertEqual(tech_direction(flat_bars, flat_bars, 2000.0)["decision"], "HOLD")

    def test_live_line_and_banner_do_not_raise(self):
        eng = self.engine()
        candles = flat(120, 2000.0)
        rec = self.tick(eng, candles)
        line = format_live_line(now_ms=now_after(candles), symbol="ETHUSDT", price=2000.0,
                                state=eng.load_state(), due_ms=next_bar_close_ms(now_after(candles)))
        self.assertIn("[demo-dca]", line)
        self.assertIn("1차", line)
        buf = StringIO()
        with redirect_stdout(buf):
            print_banner(eng)
        self.assertIn("NO REAL MONEY", buf.getvalue())
        self.assertIn("4차", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
