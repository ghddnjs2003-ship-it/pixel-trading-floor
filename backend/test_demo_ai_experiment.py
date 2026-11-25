"""데모 전용 AI 자율 실험 모드 테스트 — 판단·주문·청산·안전선이 '말'이 아니라 '동작'인지.

실제 거래소·Gemini를 부르지 않는다(캔들·가짜 AI·가짜 전송). 키가 없어도 항상 돈다.
확인하는 것
1) AI 응답 파싱 — LONG/SHORT/HOLD/CLOSE, 코드블록, 한국어 별칭, 배수 클램프, 깨진 응답은 HOLD
2) LONG/SHORT 진입 — ATR×stop_atr 손절 · target_r 목표 · bybit_client의 50달러 규칙 · TP/SL 동봉
3) 보유 중 HOLD/CLOSE — AI가 CLOSE면 청산 기록, HOLD면 그대로, 시간 청산·손절·목표 청산
4) 과매매 가드 — 하루 4회·일일 손실 10% 킬스위치·수동 정지·같은 봉 재진입 금지
5) 샌드박스 전용 가드 — 메인넷 클라이언트 거부, --mock/데모만
6) 기존 paper_* · testnet_auto_* 파일 무오염(읽지도 쓰지도 않음)
7) 1분 상태 줄·다음 점검 시각·--status CLI
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from backend.bybit_client import (DEMO_BASE_URL, TESTNET_BASE_URL, BybitClient,
                                  BybitConfigError)
from backend.demo_ai_experiment import (AIDecider, DemoAIExperiment, ExperimentConfig,
                                        build_context, format_live_line, main,
                                        next_bar_close_ms, parse_entry_response,
                                        parse_manage_response, print_banner, run_live_loop,
                                        validate_settings)

TF = 900_000                                  # 15분
START = (1_790_000_000_000 // TF) * TF        # 15분 격자에 맞춘 시작
PRICE = 60_000.0
ATR = 20.0                                    # flat 봉(고+10/저-10)의 Wilder ATR
ENV_KEYS = ("BYBIT_API_KEY", "BYBIT_API_SECRET", "BYBIT_TESTNET", "BYBIT_DEMO", "BYBIT_ENV",
            "LLM_PROVIDER")


def no_network(*_args, **_kwargs):
    raise AssertionError("mock 모드에서 네트워크 요청이 나갔습니다")


def flat_candles(n=120, price=PRICE, step=TF, start=START):
    return [{"t": start + i * step, "o": price, "h": price + 10, "l": price - 10,
             "c": price, "v": 10.0} for i in range(n)]


def flat_4h(n=40, price=PRICE):
    return flat_candles(n, price, step=4 * 3_600_000)


class FakeAI:
    """AIDecider와 같은 모양 — 호출 수와 넘겨받은 문맥을 남기고 정해진 판단을 돌려준다."""

    def __init__(self, entry=None, manage=None, entry_exc=None, manage_exc=None):
        self.entry = entry or {"decision": "HOLD", "stop_atr": 2.0, "target_r": 2.0,
                               "reason": "관망", "model": "fake"}
        self.manage = manage or {"decision": "HOLD", "reason": "유지", "model": "fake"}
        self.entry_exc = entry_exc
        self.manage_exc = manage_exc
        self.entry_calls = 0
        self.manage_calls = 0
        self.last_entry_context = None
        self.last_manage_context = None

    def describe(self):
        return {"provider": "fake", "model": "fake"}

    def decide_entry(self, context, cfg=None):
        self.entry_calls += 1
        self.last_entry_context = context
        if self.entry_exc:
            raise self.entry_exc
        return dict(self.entry)

    def decide_manage(self, context, cfg=None):
        self.manage_calls += 1
        self.last_manage_context = context
        if self.manage_exc:
            raise self.manage_exc
        return dict(self.manage)


def long_ai(stop_atr=2.0, target_r=2.0, reason="EMA20 위 · BSL 돌파"):
    return FakeAI(entry={"decision": "LONG", "stop_atr": stop_atr, "target_r": target_r,
                         "reason": reason, "model": "fake"})


def short_ai(stop_atr=2.0, target_r=2.0, reason="SSL 이탈"):
    return FakeAI(entry={"decision": "SHORT", "stop_atr": stop_atr, "target_r": target_r,
                         "reason": reason, "model": "fake"})


class FakeTransport:
    """실제 네트워크 대신 정해진 응답을 돌려주고, 보낸 요청을 남긴다."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def __call__(self, method, path, params, body, headers):
        self.calls.append({"method": method, "path": path, "params": dict(params),
                           "body": body, "headers": dict(headers)})
        response = self.responses.get(path)
        if response is None:
            raise AssertionError(f"예상하지 못한 요청: {method} {path}")
        return json.loads(json.dumps(response))


BALANCE_50 = {"retCode": 0, "result": {"list": [{"totalEquity": "50", "coin": [
    {"coin": "USDT", "equity": "50", "walletBalance": "50", "availableToWithdraw": "50"}]}]}}
NO_POSITIONS = {"retCode": 0, "result": {"list": []}}


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = {key: os.environ.pop(key, None) for key in ENV_KEYS}
        self.dir = tempfile.mkdtemp()
        self.cfg = ExperimentConfig(data_dir=self.dir)
        self.candles = flat_candles(120)
        self.candles_4h = flat_4h(40)
        self.now = self.candles[-1]["t"] + TF + 30_000

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def client(self, mock_balance=50.0):
        return BybitClient(api_key="", api_secret="", env="demo", transport=no_network,
                           mock_balance=mock_balance)

    def engine(self, ai=None, client=None, cfg=None):
        return DemoAIExperiment(cfg or self.cfg, client=client or self.client(), ai=ai or FakeAI())

    def tick(self, engine, candles=None, now_ms=None, candles_4h=None):
        return engine.tick(candles=list(candles if candles is not None else self.candles),
                           candles_4h=list(candles_4h if candles_4h is not None else self.candles_4h),
                           now_ms=now_ms or self.now)

    def state(self, engine):
        return json.loads(Path(engine.state_path).read_text(encoding="utf-8"))

    def open_long(self, engine):
        rec = self.tick(engine)
        self.assertEqual(rec["action"], "OPEN")
        return self.state(engine)["position"]


# ---------------------------------------------------------------------- 응답 해석

class ParseTests(unittest.TestCase):
    def test_entry_long_json(self):
        out = parse_entry_response('{"decision":"LONG","stop_atr":2.0,"target_r":2.5,"reason":"EMA20 위"}')
        self.assertEqual(out["decision"], "LONG")
        self.assertEqual(out["stop_atr"], 2.0)
        self.assertEqual(out["target_r"], 2.5)
        self.assertEqual(out["reason"], "EMA20 위")
        self.assertIsNone(out["parse_error"])

    def test_entry_code_fence_and_korean_alias(self):
        out = parse_entry_response('```json\n{"decision":"숏","stop_atr":1.8,"target_r":2.0,'
                                   '"reason":"SSL 이탈"}\n```')
        self.assertEqual(out["decision"], "SHORT")
        self.assertEqual(out["stop_atr"], 1.8)

    def test_entry_multipliers_are_clamped(self):
        out = parse_entry_response('{"decision":"LONG","stop_atr":9,"target_r":0.2,"reason":"x"}')
        self.assertEqual(out["stop_atr"], 3.0)
        self.assertEqual(out["target_r"], 1.5)
        self.assertTrue(out["clamped"])

    def test_entry_missing_multipliers_use_defaults(self):
        out = parse_entry_response('{"decision":"SHORT","reason":"x"}')
        self.assertEqual(out["stop_atr"], 2.0)
        self.assertEqual(out["target_r"], 2.0)

    def test_entry_unknown_decision_is_hold(self):
        out = parse_entry_response('{"decision":"MAYBE","reason":"글쎄"}')
        self.assertEqual(out["decision"], "HOLD")
        self.assertTrue(out["parse_error"])

    def test_entry_garbage_is_hold(self):
        out = parse_entry_response("지금은 사도 될 것 같습니다")
        self.assertEqual(out["decision"], "HOLD")
        self.assertIn("관망", out["reason"])
        self.assertTrue(out["parse_error"])

    def test_entry_empty_is_hold(self):
        out = parse_entry_response("")
        self.assertEqual(out["decision"], "HOLD")

    def test_manage_close_korean_and_hold(self):
        self.assertEqual(parse_manage_response('{"decision":"청산","reason":"EMA 역전"}')["decision"],
                         "CLOSE")
        self.assertEqual(parse_manage_response('{"decision":"유지","reason":"추세 유효"}')["decision"],
                         "HOLD")

    def test_manage_garbage_is_hold(self):
        out = parse_manage_response("not json")
        self.assertEqual(out["decision"], "HOLD")
        self.assertTrue(out["parse_error"])

    def test_ai_decider_wraps_provider_errors(self):
        class Broken:
            def complete(self, system, prompt):
                raise RuntimeError("한도 초과")

        ai = AIDecider(provider=Broken(), model="broken")
        out = ai.decide_entry({"현재가": 1})
        self.assertEqual(out["decision"], "HOLD")
        self.assertIn("AI 호출 실패", out["reason"])
        self.assertEqual(ai.errors, 1)
        out = ai.decide_manage({"현재가": 1})
        self.assertEqual(out["decision"], "HOLD")


# ---------------------------------------------------------------------- 안전선·설정

class GuardTests(Base):
    def test_mainnet_client_is_refused(self):
        class MainnetClient:
            env = "mainnet"
            is_mock = False
            base_url = "https://api.bybit.com"

        with self.assertRaises(BybitConfigError):
            DemoAIExperiment(self.cfg, client=MainnetClient(), ai=FakeAI())

    def test_client_without_sandbox_env_is_refused(self):
        class UnknownClient:
            is_mock = False
            base_url = "https://api.bybit.com"

        with self.assertRaises(BybitConfigError):
            DemoAIExperiment(self.cfg, client=UnknownClient(), ai=FakeAI())

    def test_paper_and_testnet_paths_are_refused(self):
        for name in ("paper_state.json", "paper_killswitch.json", "testnet_auto_state.json",
                     "ai_cache.json", "testnet_auto_log.demo.jsonl"):
            with self.assertRaises(ValueError, msg=name):
                DemoAIExperiment(ExperimentConfig(data_dir=self.dir,
                                                  state_path=os.path.join(self.dir, name)),
                                 client=self.client(), ai=FakeAI())
        with self.assertRaises(ValueError):
            DemoAIExperiment(ExperimentConfig(data_dir=self.dir,
                                              killswitch_path=os.path.join(self.dir, "paper_killswitch.json")),
                             client=self.client(), ai=FakeAI())

    def test_default_state_names_are_dedicated(self):
        engine = self.engine()
        self.assertTrue(engine.state_path.endswith("demo_ai_experiment_state.mock.json"))
        self.assertTrue(engine.log_path.endswith("demo_ai_experiment_log.mock.jsonl"))
        self.assertTrue(engine.killswitch_path.endswith("demo_ai_experiment_killswitch.mock.json"))
        self.assertTrue(engine.pause_path.endswith("demo_ai_experiment_state.mock_pause.txt"))
        self.assertEqual(engine.state_path + ".lock",
                         str(Path(engine.state_path).with_suffix(".json.lock")))

    def test_paper_and_testnet_files_are_never_touched(self):
        sentinels = {
            "paper_state.json": '{"capital": 10000}',
            "paper_log.jsonl": '{"secret": "paper"}\n',
            "paper_killswitch.json": '{"paper": true}',
            "testnet_auto_state.demo.json": '{"position": "testnet"}',
            "testnet_auto_log.demo.jsonl": '{"x": 1}\n',
            "ai_cache.json": '{"cache": true}',
        }
        for name, text in sentinels.items():
            (Path(self.dir) / name).write_text(text, encoding="utf-8")
        before = {name: (Path(self.dir) / name).read_text(encoding="utf-8") for name in sentinels}

        engine = self.engine(ai=long_ai())
        self.tick(engine)
        after = {name: (Path(self.dir) / name).read_text(encoding="utf-8") for name in sentinels}
        self.assertEqual(before, after)
        created = sorted(os.listdir(self.dir))
        self.assertFalse([n for n in created if n.startswith("paper_") and n not in sentinels])
        self.assertFalse([n for n in created if n.startswith("testnet_auto") and n not in sentinels])

    def test_settings_validation(self):
        for kwargs in ({"leverage": 4}, {"leverage": 0.5}, {"loss_pct": 5}, {"loss_pct": 1},
                       {"interval": "5"}, {"max_trades_per_day": 0}, {"daily_loss_pct": 60}):
            cfg = ExperimentConfig(data_dir=self.dir, **kwargs)
            with self.assertRaises(ValueError, msg=str(kwargs)):
                validate_settings(cfg)

    def test_api_keys_never_reach_state_or_log(self):
        os.environ["BYBIT_API_KEY"] = "demo-key-should-not-appear"
        os.environ["BYBIT_API_SECRET"] = "demo-secret-should-not-appear"
        engine = self.engine(ai=long_ai())
        self.tick(engine)
        for path in (engine.state_path, engine.log_path):
            text = Path(path).read_text(encoding="utf-8")
            self.assertNotIn("demo-key-should-not-appear", text)
            self.assertNotIn("demo-secret-should-not-appear", text)

    def test_banner_says_experimental_and_no_real_money(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_banner(self.engine())
        out = buf.getvalue()
        self.assertIn("EXPERIMENTAL AI AUTONOMOUS MODE", out)
        self.assertIn("Bybit Sandbox ONLY", out)
        self.assertIn("NO REAL MONEY", out)
        self.assertIn("하루 4회", out)
        self.assertIn("일일 손실 10%", out)


# ---------------------------------------------------------------------- 진입

class EntryTests(Base):
    def test_long_enters_with_ai_multipliers_and_50_dollar_rules(self):
        client = self.client()
        ai = long_ai(stop_atr=2.0, target_r=2.5)
        rec = self.tick(self.engine(ai=ai, client=client))
        self.assertEqual(rec["action"], "OPEN")
        pos = client.get_positions("BTCUSDT")[0]
        self.assertEqual(pos["side"], "Buy")
        self.assertAlmostEqual(pos["entryPrice"], PRICE, places=2)
        self.assertAlmostEqual(pos["stopLoss"], PRICE - 2 * ATR, places=2)      # 59,960
        self.assertAlmostEqual(pos["takeProfit"], PRICE + 2.5 * 2 * ATR, places=2)  # 60,100
        payload = rec["order"]["payload"]
        self.assertEqual(payload["orderType"], "Market")
        self.assertEqual(payload["tpslMode"], "Full")
        self.assertLessEqual(rec["order"]["plan"]["expectedLossUsd"], 1.5)
        self.assertLessEqual(rec["order"]["plan"]["leverage"], 3)
        self.assertEqual(rec["order"]["plan"]["lossBudgetUsd"], 1.5)

    def test_short_enters_sell_with_stop_above_and_target_below(self):
        client = self.client()
        rec = self.tick(self.engine(ai=short_ai(stop_atr=1.5, target_r=3.0), client=client))
        self.assertEqual(rec["action"], "OPEN")
        pos = client.get_positions("BTCUSDT")[0]
        self.assertEqual(pos["side"], "Sell")
        self.assertAlmostEqual(pos["stopLoss"], PRICE + 1.5 * ATR, places=2)
        self.assertAlmostEqual(pos["takeProfit"], PRICE - 3.0 * 1.5 * ATR, places=2)

    def test_hold_places_no_order_but_records_reason(self):
        ai = FakeAI(entry={"decision": "HOLD", "stop_atr": 2.0, "target_r": 2.0,
                           "reason": "방향 불명확", "model": "fake"})
        client = self.client()
        engine = self.engine(ai=ai, client=client)
        rec = self.tick(engine)
        self.assertEqual(rec["action"], "HOLD")
        self.assertIn("방향 불명확", rec["events"][0])
        self.assertEqual(client.get_positions("BTCUSDT"), [])
        self.assertEqual(self.state(engine)["ai_last"]["decision"], "HOLD")
        self.assertEqual(ai.entry_calls, 1)

    def test_ai_exception_is_hold_not_order(self):
        client = self.client()
        engine = self.engine(ai=FakeAI(entry_exc=RuntimeError("429 한도")), client=client)
        rec = self.tick(engine)
        self.assertEqual(rec["action"], "HOLD")
        self.assertIn("AI 호출 실패", rec["events"][0])
        self.assertEqual(client.get_positions("BTCUSDT"), [])

    def test_manual_stop_blocks_before_ai_call(self):
        ai = long_ai()
        engine = self.engine(ai=ai)
        engine.killswitch.manual_stop(True, "테스트 정지")
        rec = self.tick(engine)
        self.assertEqual(rec["action"], "SKIP")
        self.assertIn("수동 정지", rec["events"][0])
        self.assertEqual(ai.entry_calls, 0)

    def test_max_trades_per_day_blocks_entry(self):
        ai = long_ai()
        engine = self.engine(ai=ai)
        state = engine.load_state()
        state["day"] = {"date": engine._today(), "entries": 4, "closes": 0}
        engine.save_state(state)
        rec = self.tick(engine)
        self.assertEqual(rec["action"], "SKIP")
        self.assertIn("하루 최대 4회", rec["events"][0])
        self.assertEqual(ai.entry_calls, 0)

    def test_killswitch_tripped_blocks_entry(self):
        engine = self.engine(ai=long_ai())
        engine.killswitch.trip("테스트 중단", minutes=30)
        engine.killswitch.save()
        rec = self.tick(engine)
        self.assertEqual(rec["action"], "SKIP")
        self.assertIn("킬스위치", rec["events"][0])

    def test_order_rejected_by_50_dollar_rule_is_reported(self):
        # stop 3×ATR 600 → 최소 수량 0.001의 손실 $0.60... 예산 $1.5 안이므로 ATR을 키운다
        candles = flat_candles(120)
        for bar in candles:
            bar["h"], bar["l"] = PRICE + 400, PRICE - 400        # ATR 800 → 3ATR 2,400
        ai = long_ai(stop_atr=3.0)
        engine = self.engine(ai=ai)
        rec = self.tick(engine, candles=candles)
        self.assertEqual(rec["action"], "SKIP")
        self.assertIn("주문 거부", rec["events"][1])
        self.assertEqual(engine.client.get_positions("BTCUSDT"), [])

    def test_foreign_exchange_position_blocks_and_is_not_touched(self):
        client = self.client()
        client.place_market_order("BTCUSDT", "Buy", stop_loss=PRICE - 1000,
                                  take_profit=PRICE + 1000, entry_price=PRICE)
        ai = long_ai()
        rec = self.tick(self.engine(ai=ai, client=client))
        self.assertEqual(rec["action"], "SKIP")
        self.assertIn("이미", rec["events"][0])
        self.assertEqual(ai.entry_calls, 0)
        self.assertEqual(len(client.get_positions("BTCUSDT")), 1)

    def test_entry_context_carries_indicators_liquidity_and_account(self):
        ai = long_ai()
        self.tick(self.engine(ai=ai))
        ctx = ai.last_entry_context
        indicators = ctx["지표"]
        for key in ("EMA20", "EMA50", "ATR14", "RSI14", "CCI20", "볼린저%B", "연속봉"):
            self.assertIn(key, indicators)
        for key in ("BSL(직전20봉고점)", "SSL(직전20봉저점)"):
            self.assertIn(key, ctx["유동성"])
        self.assertEqual(len(ctx["최근12봉"]), 12)
        self.assertIn("4시간봉", ctx)
        self.assertIn("계좌", ctx)
        self.assertEqual(ctx["계좌"]["하루최대진입"], 4)
        self.assertIn("새 진입", ctx["요청"])


# ---------------------------------------------------------------------- 보유 관리

class ManageTests(Base):
    def test_hold_keeps_position(self):
        ai = long_ai()
        engine = self.engine(ai=ai)
        self.open_long(engine)
        ai.manage = {"decision": "HOLD", "reason": "추세 유효", "model": "fake"}
        rec = self.tick(engine)
        self.assertEqual(rec["action"], "HOLD")
        self.assertIn("AI HOLD: 추세 유효", rec["events"][-1])
        self.assertIsNotNone(self.state(engine)["position"])
        self.assertEqual(ai.manage_calls, 1)
        self.assertEqual(len(engine.client.get_positions("BTCUSDT")), 1)

    def test_close_records_ai_close_and_flat_position(self):
        ai = long_ai()
        engine = self.engine(ai=ai)
        self.open_long(engine)
        ai.manage = {"decision": "CLOSE", "reason": "EMA20 아래로 이탈", "model": "fake"}
        rec = self.tick(engine)
        self.assertEqual(rec["action"], "CLOSE")
        state = self.state(engine)
        self.assertIsNone(state["position"])
        trade = state["closed"][-1]
        self.assertEqual(trade["reason"], "AI_CLOSE")
        self.assertEqual(trade["ai_decision"], "LONG")
        self.assertEqual(state["stats"]["ai_closes"], 1)
        self.assertEqual(state["stats"]["trades"], 1)
        self.assertEqual(engine.client.get_positions("BTCUSDT"), [])

    def test_manage_ai_exception_keeps_position(self):
        ai = long_ai()
        engine = self.engine(ai=ai)
        self.open_long(engine)
        ai.manage_exc = RuntimeError("timeout")
        rec = self.tick(engine)
        self.assertEqual(rec["action"], "HOLD")
        self.assertIn("AI 호출 실패", rec["events"][-1])
        self.assertIsNotNone(self.state(engine)["position"])

    def test_manage_context_shows_pnl_and_hold_bars(self):
        ai = long_ai()
        engine = self.engine(ai=ai)
        pos = self.open_long(engine)
        self.tick(engine, candles=self.candles + [{"t": pos["entry_bar_time"], "o": PRICE,
                                                   "h": PRICE + 10, "l": PRICE - 10, "c": PRICE,
                                                   "v": 5.0}],
                  now_ms=pos["entry_bar_time"] + TF + 30_000)
        ctx = ai.last_manage_context
        self.assertEqual(ctx["포지션"]["방향"], "롱")
        self.assertEqual(ctx["포지션"]["보유봉"], 1)
        self.assertIn("평가손익USDT", ctx["포지션"])
        self.assertIn("평가R", ctx["포지션"])

    def test_mock_stop_hit_records_minus_one_r(self):
        engine = self.engine(ai=long_ai())
        pos = self.open_long(engine)
        spike = {"t": pos["entry_bar_time"], "o": PRICE, "h": PRICE + 10,
                 "l": pos["stop"] - 100, "c": PRICE, "v": 5.0}
        rec = self.tick(engine, candles=self.candles + [spike],
                        now_ms=spike["t"] + TF + 30_000)
        self.assertEqual(rec["action"], "CLOSE")
        trade = self.state(engine)["closed"][-1]
        self.assertEqual(trade["reason"], "STOP")
        self.assertAlmostEqual(trade["r"], -1.0, places=2)
        self.assertAlmostEqual(trade["pnl"], -0.08, places=2)
        self.assertEqual(trade["pnl_source"], "bar_simulation")

    def test_mock_target_hit_records_plus_two_r(self):
        engine = self.engine(ai=long_ai())
        pos = self.open_long(engine)
        spike = {"t": pos["entry_bar_time"], "o": PRICE, "h": pos["target"] + 100,
                 "l": PRICE - 10, "c": PRICE, "v": 5.0}
        rec = self.tick(engine, candles=self.candles + [spike],
                        now_ms=spike["t"] + TF + 30_000)
        self.assertEqual(rec["action"], "CLOSE")
        trade = self.state(engine)["closed"][-1]
        self.assertEqual(trade["reason"], "TARGET")
        self.assertAlmostEqual(trade["r"], 2.0, places=2)

    def test_time_exit_after_max_hold_bars(self):
        cfg = ExperimentConfig(data_dir=self.dir, max_hold_bars=3)
        engine = self.engine(ai=long_ai(), cfg=cfg)
        pos = self.open_long(engine)
        bars = [{"t": pos["entry_bar_time"] + k * TF, "o": PRICE, "h": PRICE + 10,
                 "l": PRICE - 10, "c": PRICE, "v": 5.0} for k in range(4)]
        rec = self.tick(engine, candles=self.candles + bars,
                        now_ms=bars[-1]["t"] + TF + 30_000)
        self.assertEqual(rec["action"], "CLOSE")
        trade = self.state(engine)["closed"][-1]
        self.assertEqual(trade["reason"], "TIME")

    def test_no_reentry_in_the_same_bar_after_exit(self):
        ai = long_ai()
        engine = self.engine(ai=ai)
        pos = self.open_long(engine)
        spike = {"t": pos["entry_bar_time"], "o": PRICE, "h": PRICE + 10,
                 "l": pos["stop"] - 100, "c": PRICE, "v": 5.0}
        now = spike["t"] + TF + 30_000
        self.tick(engine, candles=self.candles + [spike], now_ms=now)
        calls = ai.entry_calls
        rec = self.tick(engine, candles=self.candles + [spike], now_ms=now + 60_000)
        self.assertEqual(rec["action"], "SKIP")
        self.assertIn("방금 청산한 봉", rec["events"][0])
        self.assertEqual(ai.entry_calls, calls)

    def test_position_vanished_infers_bar_exit_and_estimates_pnl(self):
        engine = self.engine(ai=long_ai())
        pos = self.open_long(engine)
        engine.client.close_position("BTCUSDT")          # 거래소에서 손절로 닫힌 상황을 흉내
        spike = {"t": pos["entry_bar_time"], "o": PRICE, "h": PRICE + 10,
                 "l": pos["stop"] - 100, "c": PRICE, "v": 5.0}
        rec = self.tick(engine, candles=self.candles + [spike],
                        now_ms=spike["t"] + TF + 30_000)
        self.assertEqual(rec["action"], "CLOSE")
        trade = self.state(engine)["closed"][-1]
        self.assertEqual(trade["reason"], "STOP")
        self.assertEqual(trade["pnl_source"], "bar_simulation")
        self.assertAlmostEqual(trade["r"], -1.0, places=2)

    def test_holding_state_survives_restart(self):
        engine = self.engine(ai=long_ai())
        self.open_long(engine)
        again = self.engine(ai=long_ai(), client=engine.client)
        rec = self.tick(again)
        self.assertNotEqual(rec["action"], "OPEN")
        self.assertIsNotNone(self.state(again)["position"])


# ---------------------------------------------------------------------- 실거래소(가짜 전송)

class ExchangeTruthTests(Base):
    def live_demo(self, responses):
        transport = FakeTransport(responses)
        client = BybitClient(api_key="k", api_secret="s", env="demo", transport=transport)
        return client, transport

    def seed_position(self, engine, entry_bar_time=None, qty="0.001"):
        state = engine.load_state()
        state["position"] = {
            "side": "Buy", "qty": qty, "entry": PRICE, "stop": PRICE - 2 * ATR,
            "target": PRICE + 2 * 2 * ATR, "leverage": 3.0, "risk_usd": 0.04,
            "order_id": "seed-1", "mode": "demo",
            "signal_time": self.candles[-1]["t"] - TF,
            "entry_bar_time": entry_bar_time or (self.candles[-1]["t"] + TF),
            "entry_time": "2026-09-22T00:00:00+00:00",
            "ai": {"decision": "LONG", "stop_atr": 2.0, "target_r": 2.0, "reason": "시드"},
        }
        engine.save_state(state)

    def test_demo_closed_pnl_404_keeps_loop_alive(self):
        client, transport = self.live_demo({
            "/v5/account/wallet-balance": BALANCE_50,
            "/v5/position/list": NO_POSITIONS,
            "/v5/position/closed-pnl": {"retCode": 404, "retMsg": "Not Found"},
        })
        engine = self.engine(ai=FakeAI(), client=client)
        self.seed_position(engine)
        rec = self.tick(engine)
        self.assertEqual(rec["action"], "CLOSE")
        state = self.state(engine)
        trade = state["closed"][-1]
        self.assertIsNone(trade["pnl"])
        self.assertIn("확인하지 못했", trade["note"])
        self.assertIn("데모", trade["note"])
        self.assertNotIn("/v5/order/create", [c["path"] for c in transport.calls])

    def test_demo_closed_pnl_is_recorded_with_reason(self):
        row = {"symbol": "BTCUSDT", "side": "Buy", "qty": "0.001",
               "avgEntryPrice": str(PRICE), "avgExitPrice": str(PRICE + 2 * 2 * ATR),
               "closedPnl": "0.08", "orderId": "x",
               "updatedTime": int(self.candles[-1]["t"]) + 60_000}
        client, _ = self.live_demo({
            "/v5/account/wallet-balance": BALANCE_50,
            "/v5/position/list": NO_POSITIONS,
            "/v5/position/closed-pnl": {"retCode": 0, "result": {"list": [row]}},
        })
        engine = self.engine(ai=FakeAI(), client=client)
        self.seed_position(engine)
        rec = self.tick(engine)
        self.assertEqual(rec["action"], "CLOSE")
        trade = self.state(engine)["closed"][-1]
        self.assertEqual(trade["reason"], "TARGET")
        self.assertEqual(trade["pnl_source"], "exchange")
        self.assertAlmostEqual(trade["pnl"], 0.08, places=4)
        self.assertAlmostEqual(trade["r"], 2.0, places=2)

    def test_exchange_read_failure_stops_the_scan_without_order(self):
        client, transport = self.live_demo({
            "/v5/account/wallet-balance": {"retCode": 10003, "retMsg": "invalid api key"},
        })
        engine = self.engine(ai=long_ai(), client=client)
        rec = self.tick(engine)
        self.assertEqual(rec["action"], "ERROR")
        self.assertIn("거래소 조회 실패", rec["events"][0])
        self.assertEqual([c["path"] for c in transport.calls], ["/v5/account/wallet-balance"])

    def test_demo_long_order_goes_to_demo_base(self):
        client, transport = self.live_demo({
            "/v5/account/wallet-balance": BALANCE_50,
            "/v5/position/list": NO_POSITIONS,
            "/v5/market/tickers": {"retCode": 0, "result": {"list": [{"lastPrice": "60000"}]}},
            "/v5/market/instruments-info": {"retCode": 0, "result": {"list": [{
                "symbol": "BTCUSDT",
                "lotSizeFilter": {"minOrderQty": "0.001", "qtyStep": "0.001", "maxOrderQty": "100"},
                "priceFilter": {"tickSize": "0.10"}}]}},
            "/v5/position/set-leverage": {"retCode": 0, "result": {}},
            "/v5/order/create": {"retCode": 0, "result": {"orderId": "demo-ai-1"}},
        })
        engine = self.engine(ai=long_ai(), client=client)
        rec = self.tick(engine)
        self.assertEqual(rec["action"], "OPEN")
        self.assertEqual(rec["base_url"], DEMO_BASE_URL)
        self.assertTrue(rec["sandbox_only"])
        self.assertEqual(rec["order"]["orderId"], "demo-ai-1")
        self.assertIn("/v5/order/create", [c["path"] for c in transport.calls])
        self.assertEqual(self.state(engine)["position"]["order_id"], "demo-ai-1")


# ---------------------------------------------------------------------- 킬스위치

class KillSwitchTests(Base):
    def test_rules_are_demo_ai_specific(self):
        engine = self.engine()
        rules = engine.killswitch.rules
        self.assertEqual(rules.daily_loss_pct, 10.0)
        self.assertEqual(rules.max_daily_trades, 4)
        self.assertEqual(rules.max_consecutive_losses, 0)      # 끔
        self.assertEqual(rules.volatility_pause_atr_pct, 0.0)  # 끔

    def test_four_trades_trip_the_daily_limit(self):
        engine = self.engine()
        ks = engine.killswitch
        ks.start_day(50.0)
        for _ in range(4):
            ks.record_trade(-0.01, 49.96)
        self.assertFalse(ks.allow_entry()["ok"])
        self.assertIn("당일 거래 4회", ks.allow_entry()["reason"])

    def test_ten_percent_daily_loss_trips(self):
        engine = self.engine()
        ks = engine.killswitch
        ks.start_day(50.0)
        ks.record_trade(-5.0, 45.0)                            # -10%
        self.assertFalse(ks.allow_entry()["ok"])
        self.assertIn("당일 손실", ks.allow_entry()["reason"])

    def test_day_entries_reset_on_state_load(self):
        engine = self.engine()
        state = engine.load_state()
        state["day"] = {"date": "2020-01-01", "entries": 4, "closes": 4}
        engine.save_state(state)
        loaded = engine.load_state()
        self.assertEqual(loaded["day"]["entries"], 0)
        self.assertEqual(loaded["day"]["date"], engine._today())


# ---------------------------------------------------------------------- 문맥·시각·화면

class ContextTests(Base):
    def test_forming_bar_is_dropped(self):
        ai = long_ai()
        engine = self.engine(ai=ai)
        forming = {"t": self.candles[-1]["t"] + TF, "o": PRICE + 500, "h": PRICE + 900,
                   "l": PRICE - 900, "c": PRICE + 800, "v": 99.0}
        rec = self.tick(engine, candles=self.candles + [forming],
                        now_ms=self.candles[-1]["t"] + TF + 30_000)
        self.assertEqual(rec["action"], "OPEN")
        self.assertEqual(ai.last_entry_context["현재가"], PRICE)   # 진행 중 봉은 쓰지 않는다
        self.assertEqual(ai.last_entry_context["최근12봉"][-1]["c"], PRICE)

    def test_context_survives_missing_4h(self):
        ctx = build_context(self.candles, None)
        self.assertNotIn("4시간봉", ctx)
        self.assertEqual(ctx["지표"]["ATR14"], ATR)

    def test_next_bar_close_waits_60_seconds(self):
        now = START + 1234
        self.assertEqual(next_bar_close_ms(now, TF), (START // TF + 1) * TF + 60_000)
        self.assertEqual(next_bar_close_ms(START + TF + 59_000, TF), START + 2 * TF + 60_000)

    def test_live_line_shows_price_position_ai_and_remaining(self):
        engine = self.engine(ai=long_ai())
        self.open_long(engine)
        state = self.state(engine)
        due = self.now + 2 * 60_000
        line = format_live_line(now_ms=self.now, symbol="BTCUSDT", price=PRICE + 5,
                                state=state, ai_last={"decision": "LONG", "reason": "EMA20 위"},
                                due_ms=due)
        self.assertIn("[demo-ai]", line)
        self.assertIn("보유 롱", line)
        self.assertIn("평가", line)
        self.assertIn("최근 AI LONG", line)
        self.assertIn("2분 00초 남음", line)

    def test_live_line_without_position(self):
        line = format_live_line(now_ms=self.now, symbol="BTCUSDT", price=PRICE, state={},
                                ai_last=None, due_ms=None)
        self.assertIn("포지션 없음", line)
        self.assertNotIn("최근 AI", line)


# ---------------------------------------------------------------------- CLI·루프

class LoopTests(Base):
    def test_loop_runs_a_round_and_prints_next_scan(self):
        ai = FakeAI()
        engine = self.engine(ai=ai)
        clock = {"ms": self.now}
        lines = []

        def sleep(sec):
            clock["ms"] += int(sec * 1000)

        def fetch():
            return {"source": "fake", "candles": self.candles, "candles_4h": self.candles_4h}

        engine.fetch = fetch
        engine.now = lambda: __import__("datetime").datetime.fromtimestamp(clock["ms"] / 1000,
                                                                          tz=__import__("datetime").timezone.utc)
        code = run_live_loop(engine, tick_sec=1, max_rounds=1, clock=lambda: clock["ms"],
                             sleep=sleep, out=lines.append)
        self.assertEqual(code, 0)
        self.assertTrue(any("[demo-ai]" in line and "다음 점검" in line for line in lines))
        self.assertEqual(ai.entry_calls, 1)

    def test_cli_status_works_in_mock(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--status", "--mock", "--quiet",
                         "--state", os.path.join(self.dir, "demo_state.json"),
                         "--log", os.path.join(self.dir, "demo_log.jsonl"),
                         "--killswitch", os.path.join(self.dir, "demo_ks.json")])
        self.assertEqual(code, 0)

    def test_cli_rejects_unsafe_settings(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["--once", "--mock", "--leverage", "5"]), 2)
            self.assertEqual(main(["--once", "--mock", "--loss-pct", "9"]), 2)
            self.assertEqual(main(["--once", "--mock", "--max-trades", "0"]), 2)
        with self.assertRaises(SystemExit) as ctx:      # argparse가 선택지 밖 값을 바로 거부
            main(["--once", "--mock", "--interval", "5"])
        self.assertEqual(ctx.exception.code, 2)

    def test_cli_mock_once_writes_only_experiment_files(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--once", "--mock", "--quiet",
                         "--state", os.path.join(self.dir, "demo_ai_experiment_state.json"),
                         "--log", os.path.join(self.dir, "demo_ai_experiment_log.jsonl"),
                         "--killswitch", os.path.join(self.dir, "demo_ai_experiment_killswitch.json")])
        self.assertEqual(code, 0)
        names = sorted(os.listdir(self.dir))
        self.assertTrue(any(n.startswith("demo_ai_experiment_state") for n in names))
        self.assertFalse([n for n in names if n.startswith(("paper_", "testnet_auto"))])


if __name__ == "__main__":
    unittest.main()
