"""테스트넷 오토 모드 테스트 — 두뇌와 손이 실제로 이어지는지, 안전장치가 '말'이 아니라 '동작'인지.

실제 거래소·Gemini를 부르지 않는다(캔들·신호·가짜 전송). 키가 없어도 항상 돈다.
확인하는 것
1) 키가 없거나 --mock이면 네트워크 주문이 한 번도 나가지 않는다
2) 후보1 신호 → 다음 봉 시가·3ATR 손절·4R 목표 → bybit_client의 50달러 규칙으로 주문(TP/SL 동봉)
3) AI REJECT·킬스위치·세션 밖·이미 열린 포지션은 진입을 막고 사유를 남긴다
4) 같은 봉·같은 신호로 두 번 주문하지 않는다
5) mock 청산(손절·목표·96봉 시간)이 기록되고 통계·킬스위치에 반영된다
6) 실테스트넷에서 포지션이 사라지면 거래소 청산 기록으로 손익을 남기고, 내 기록과 다른 포지션은 건드리지 않는다
7) 모의매매(paper_*) 파일은 읽지도 쓰지도 않는다 · 상태·로그에 API 키가 들어가지 않는다
"""

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from backend.ai_filter import FilterConfig
from backend.bybit_client import (DEMO_BASE_URL, TESTNET_BASE_URL, BybitClient, BybitConfigError,
                                  BybitError)
from backend.testnet_auto import (AutoConfig, RunLock, TestnetAuto, build_client, format_live_line,
                                  main, nearest_level, next_bar_close_ms, next_scan_ms, print_banner,
                                  run_live_loop, run_smoke_test, swing_levels, volatility_spike)

TF = 1_800_000                     # 30분
START = 1_790_000_000_000          # 2026-09-19 14:00 UTC → 진입 봉은 02:00 UTC(아시아 세션)
PRICE = 60_000.0
ATR = 300.0                        # 3ATR = 900 → 0.001개 손실 $0.90 (예산 $1.50 안)
ENV_KEYS = ("BYBIT_API_KEY", "BYBIT_API_SECRET", "BYBIT_TESTNET", "BYBIT_DEMO", "BYBIT_ENV",
            "BYBIT_ADAPTIVE")


def no_network(*_args, **_kwargs):
    raise AssertionError("mock 모드에서 네트워크 요청이 나갔습니다")


def flat_candles(n=120, price=PRICE, start=START):
    return [{"t": start + i * TF, "o": price, "h": price + 10, "l": price - 10,
             "c": price, "v": 10.0} for i in range(n)]


def signal_at(candles, side="SHORT", atr=ATR, index=None):
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


class FakeTransport:
    """실제 네트워크 대신 정해진 응답을 돌려주고, 보낸 요청을 그대로 남긴다."""

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


class TestnetAutoTestBase(unittest.TestCase):
    def setUp(self):
        self._saved = {key: os.environ.pop(key, None) for key in ENV_KEYS}
        self.dir = tempfile.mkdtemp()
        self.cfg = AutoConfig(data_dir=self.dir)
        self.candles = flat_candles(120)
        self.sig = signal_at(self.candles)
        # 신호 봉 다음(진행 중) 봉 — 시가 60,100을 '다음 봉 시가'로 쓴다
        self.forming = {"t": self.candles[-1]["t"] + TF, "o": PRICE + 100, "h": PRICE + 120,
                        "l": PRICE + 80, "c": PRICE + 100, "v": 5.0}
        self.now = self.forming["t"] + 60_000

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def client(self, mock_balance=50.0):
        return BybitClient(api_key="", api_secret="", transport=no_network,
                           mock_balance=mock_balance)

    def engine(self, ai=None, client=None, cfg=None):
        return TestnetAuto(cfg or self.cfg, client=client or self.client(), ai=ai)

    def with_forming(self, candles=None, forming=None):
        candles = list(candles if candles is not None else self.candles)
        f = dict(forming or self.forming)
        f["t"] = candles[-1]["t"] + TF
        return candles + [f]

    def tick(self, engine, signals=None, candles=None, now_ms=None):
        return engine.tick(candles=self.with_forming(candles), now_ms=now_ms or self.now,
                           signals=[self.sig] if signals is None else signals)


class ModeTests(TestnetAutoTestBase):
    def test_no_keys_means_mock_and_no_network(self):
        engine = self.engine()
        self.assertTrue(engine.mock)
        self.assertTrue(engine.state_path.endswith("testnet_auto_state.mock.json"))
        rec = self.tick(engine)
        self.assertEqual(rec["mode"], "mock")
        self.assertTrue(rec["testnet_only"])
        self.assertEqual(rec["base_url"], TESTNET_BASE_URL)

    def test_force_mock_wins_over_env_keys(self):
        os.environ["BYBIT_API_KEY"] = "k"
        os.environ["BYBIT_API_SECRET"] = "s"
        client = build_client(force_mock=True)
        self.assertTrue(client.is_mock)
        self.assertEqual(client.base_url, TESTNET_BASE_URL)

    def test_env_keys_build_live_testnet_client(self):
        os.environ["BYBIT_API_KEY"] = "k"
        os.environ["BYBIT_API_SECRET"] = "s"
        client = build_client()
        self.assertFalse(client.is_mock)
        self.assertEqual(client.base_url, TESTNET_BASE_URL)

    def test_state_paths_never_point_at_paper_files(self):
        with self.assertRaises(ValueError):
            TestnetAuto(AutoConfig(data_dir=self.dir,
                                   state_path=os.path.join(self.dir, "paper_state.json")),
                        client=self.client())
        with self.assertRaises(ValueError):
            TestnetAuto(AutoConfig(data_dir=self.dir,
                                   killswitch_path=os.path.join(self.dir, "paper_killswitch.json")),
                        client=self.client())

    def test_default_state_is_written_and_paper_files_are_not_touched(self):
        engine = self.engine(ai=FakeAI())
        self.tick(engine)
        names = sorted(os.listdir(self.dir))
        self.assertIn("testnet_auto_state.mock.json", names)
        self.assertIn("testnet_auto_log.mock.jsonl", names)
        self.assertFalse([n for n in names if n.startswith("paper_")])

    def test_next_bar_close_is_after_bar_plus_delay(self):
        now = START + 1234
        expected = (START // TF + 1) * TF + 90_000
        self.assertEqual(next_bar_close_ms(now, TF), expected)
        self.assertEqual(next_bar_close_ms(expected, TF), expected + TF)


class DemoModeTests(TestnetAutoTestBase):
    """바이비트 Demo Trading(api-demo.bybit.com) — 테스트넷과 같은 흐름, 기록은 따로, 부가 조회 실패는 견딘다."""

    def live_demo(self, responses):
        transport = FakeTransport(responses)
        client = BybitClient(api_key="k", api_secret="s", env="demo", transport=transport)
        return client, transport

    def test_build_client_uses_demo_env_var(self):
        os.environ["BYBIT_API_KEY"] = "k"
        os.environ["BYBIT_API_SECRET"] = "s"
        os.environ["BYBIT_DEMO"] = "true"
        client = build_client()
        self.assertEqual(client.env, "demo")
        self.assertEqual(client.base_url, DEMO_BASE_URL)
        self.assertFalse(client.is_mock)

    def test_build_client_env_argument_selects_demo(self):
        os.environ["BYBIT_API_KEY"] = "k"
        os.environ["BYBIT_API_SECRET"] = "s"
        client = build_client(env="demo")
        self.assertEqual(client.env, "demo")
        self.assertEqual(client.base_url, DEMO_BASE_URL)

    def test_force_mock_keeps_demo_env_but_no_order(self):
        client = build_client(force_mock=True, env="demo")
        self.assertTrue(client.is_mock)
        self.assertEqual(client.env, "demo")
        self.assertEqual(client.base_url, DEMO_BASE_URL)

    def test_demo_state_paths_are_separate_from_testnet(self):
        client = BybitClient(api_key="k", api_secret="s", env="demo", transport=no_network)
        engine = TestnetAuto(self.cfg, client=client, ai=False)
        self.assertEqual(engine.mode, "demo")
        self.assertEqual(engine.env, "demo")
        self.assertTrue(engine.state_path.endswith("testnet_auto_state.demo.json"))
        self.assertTrue(engine.log_path.endswith("testnet_auto_log.demo.jsonl"))
        self.assertTrue(engine.pause_path.endswith("testnet_auto_pause.demo.txt"))
        state = engine.load_state()
        self.assertEqual(state["mode"], "demo")
        self.assertEqual(state["rule"]["env"], "demo")
        self.assertEqual(state["rule"]["base_url"], DEMO_BASE_URL)
        self.assertTrue(state["rule"]["testnet_only"])
        # 테스트넷 엔진은 같은 폴더라도 다른 파일을 쓴다
        testnet_engine = TestnetAuto(self.cfg, client=BybitClient(api_key="", api_secret=""), ai=False)
        self.assertTrue(testnet_engine.state_path.endswith("testnet_auto_state.mock.json"))

    def test_demo_signal_places_order_on_demo_base(self):
        client, transport = self.live_demo({
            "/v5/account/wallet-balance": BALANCE_50,
            "/v5/position/list": NO_POSITIONS,
            "/v5/market/tickers": {"retCode": 0, "result": {"list": [{"lastPrice": "60100"}]}},
            "/v5/market/instruments-info": {"retCode": 0, "result": {"list": [{
                "symbol": "BTCUSDT",
                "lotSizeFilter": {"minOrderQty": "0.001", "qtyStep": "0.001", "maxOrderQty": "100"},
                "priceFilter": {"tickSize": "0.10"}}]}},
            "/v5/position/set-leverage": {"retCode": 0, "result": {}},
            "/v5/order/create": {"retCode": 0, "result": {"orderId": "demo-1"}},
        })
        engine = self.engine(ai=FakeAI(), client=client)
        self.assertEqual(engine.mode, "demo")
        rec = self.tick(engine)
        self.assertEqual(rec["action"], "OPEN")
        self.assertEqual(rec["mode"], "demo")
        self.assertEqual(rec["base_url"], DEMO_BASE_URL)
        self.assertTrue(rec["sandbox_only"])
        self.assertEqual(rec["order"]["orderId"], "demo-1")
        self.assertIn("/v5/order/create", [c["path"] for c in transport.calls])
        state = json.loads(Path(engine.state_path).read_text(encoding="utf-8"))
        self.assertEqual(state["mode"], "demo")
        self.assertEqual(state["position"]["mode"], "demo")
        self.assertEqual(state["position"]["order_id"], "demo-1")

    def test_demo_closed_pnl_404_does_not_kill_the_loop(self):
        client, transport = self.live_demo({
            "/v5/account/wallet-balance": BALANCE_50,
            "/v5/position/list": NO_POSITIONS,
            "/v5/position/closed-pnl": {"retCode": 404, "retMsg": "Not Found"},
        })
        engine = self.engine(ai=FakeAI(), client=client)
        engine.save_state({**engine.load_state(), "position": {
            "side": "Sell", "qty": "0.001", "entry": PRICE + 100, "stop": PRICE + 1000,
            "target": PRICE - 3500, "leverage": 3.0, "risk_usd": 0.9, "order_id": "demo-2",
            "mode": "demo", "signal_time": self.candles[-1]["t"] - TF,
            "entry_bar_time": self.candles[-1]["t"], "entry_time": "2026-09-19T00:00:00+00:00",
            "ai": {"decision": "PASS"}}})
        rec = self.tick(engine, signals=[])
        self.assertEqual(rec["action"], "CLOSE")          # 청산 자체는 기록하고 루프는 계속
        self.assertIn("손익", " ".join(rec["events"]))
        state = json.loads(Path(engine.state_path).read_text(encoding="utf-8"))
        trade = state["closed"][-1]
        self.assertIsNone(trade["pnl"])
        self.assertIn("미확인", trade.get("note", ""))
        self.assertIn("데모", trade.get("note", ""))
        self.assertNotIn("/v5/order/create", [c["path"] for c in transport.calls])

    def test_unsupported_extra_lookup_never_raises(self):
        class BrokenPnl(BybitClient):
            def get_closed_pnl(self, symbol, limit=5):
                raise RuntimeError("데모에서 이 조회는 지원되지 않습니다")

        client = BrokenPnl(api_key="k", api_secret="s", env="demo", transport=no_network)
        engine = self.engine(ai=FakeAI(), client=client)
        state = engine.load_state()
        state["position"] = {"side": "Sell", "qty": "0.001", "entry": PRICE, "stop": PRICE + 900,
                            "target": PRICE - 3600, "risk_usd": 0.9, "signal_time": 1,
                            "entry_bar_time": self.candles[-1]["t"]}
        trade = engine._record_exchange_close(state, state["position"], {"equity": 50.0})
        self.assertIsNone(trade["pnl"])
        self.assertIsNone(state["position"])

    def test_banner_says_demo_and_base_url(self):
        client = BybitClient(api_key="k", api_secret="s", env="demo", transport=no_network)
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_banner(TestnetAuto(self.cfg, client=client, ai=False))
        out = buf.getvalue()
        self.assertIn("데모 실주문", out)
        self.assertIn("api-demo.bybit.com", out)
        self.assertIn("모의 USDT만 움직입니다", out)
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_banner(self.engine(ai=False))
        self.assertIn("mock", buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_banner(TestnetAuto(self.cfg, ai=False,
                                     client=BybitClient(api_key="k", api_secret="s",
                                                        transport=no_network)))
        self.assertIn("테스트넷 실주문", buf.getvalue())

    def test_cli_demo_flag_status_works_in_mock(self):
        code = main(["--status", "--mock", "--demo", "--quiet",
                     "--state", os.path.join(self.dir, "demo_state.json"),
                     "--log", os.path.join(self.dir, "demo_log.jsonl"),
                     "--killswitch", os.path.join(self.dir, "demo_ks.json")])
        self.assertEqual(code, 0)

    def test_non_sandbox_client_is_refused(self):
        class MainnetClient:
            env = "mainnet"
            is_mock = False
            base_url = "https://api.bybit.com"

        with self.assertRaises(BybitConfigError):
            TestnetAuto(self.cfg, client=MainnetClient(), ai=False)


class EntryTests(TestnetAutoTestBase):
    def test_signal_places_testnet_order_with_3atr_stop_and_4r_target(self):
        client = self.client()
        rec = self.tick(self.engine(ai=FakeAI(), client=client))
        self.assertEqual(rec["action"], "OPEN")
        positions = client.get_positions("BTCUSDT")
        self.assertEqual(len(positions), 1)
        pos = positions[0]
        self.assertEqual(pos["side"], "Sell")
        self.assertEqual(pos["size"], 0.001)                    # 50달러 예산이 정한 수량
        self.assertAlmostEqual(pos["entryPrice"], PRICE + 100, places=2)   # 다음 봉 시가
        self.assertAlmostEqual(pos["stopLoss"], PRICE + 100 + 3 * ATR, places=2)
        self.assertAlmostEqual(pos["takeProfit"], PRICE + 100 - 4 * 3 * ATR, places=2)
        payload = rec["order"]["payload"]
        self.assertEqual(payload["orderType"], "Market")
        self.assertEqual(payload["tpslMode"], "Full")
        self.assertEqual(float(payload["qty"]), 0.001)
        self.assertEqual(rec["order"]["plan"]["lossBudgetUsd"], 1.5)
        self.assertLessEqual(rec["order"]["plan"]["expectedLossUsd"], 1.5)
        self.assertLessEqual(rec["order"]["plan"]["leverage"], 5)

    def test_state_and_log_record_the_position(self):
        engine = self.engine(ai=FakeAI())
        self.tick(engine)
        state = json.loads(Path(engine.state_path).read_text(encoding="utf-8"))
        self.assertEqual(state["position"]["side"], "Sell")
        self.assertEqual(state["position"]["qty"], "0.001")
        self.assertEqual(state["scan_count"], 1)
        lines = Path(engine.log_path).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(json.loads(lines[0])["action"], "OPEN")

    def test_ai_reject_blocks_order_and_keeps_reason(self):
        ai = FakeAI(decision="REJECT", loss_prob=72.0, reason="1R 안에 저항 3개")
        client = self.client()
        rec = self.tick(self.engine(ai=ai, client=client))
        self.assertEqual(rec["action"], "REJECT")
        self.assertEqual(client.get_positions("BTCUSDT"), [])
        self.assertEqual(ai.calls, 1)
        self.assertIn("저항 3개", rec["events"][0])

    def test_same_signal_is_not_reviewed_or_ordered_twice(self):
        ai = FakeAI(decision="REJECT", reason="첫 판정")
        engine = self.engine(ai=ai)
        self.tick(engine)
        ai.decision = "PASS"
        rec = self.tick(engine)
        self.assertNotEqual(rec["action"], "OPEN")
        self.assertEqual(ai.calls, 1)                          # AI도 한 번만
        state = json.loads(Path(engine.state_path).read_text(encoding="utf-8"))
        self.assertIsNone(state["position"])

    def test_killswitch_manual_stop_blocks_entry(self):
        engine = self.engine(ai=FakeAI())
        engine.killswitch.manual_stop(True, "테스트 정지")
        rec = self.tick(engine)
        self.assertEqual(rec["action"], "SKIP")
        self.assertIn("수동 정지", rec["events"][0])
        self.assertEqual(engine.client.get_positions("BTCUSDT"), [])

    def test_session_outside_asia_blocks_entry(self):
        # 00:00 UTC 시작 → 진입 봉이 12:00 UTC(아시아 세션 밖)
        candles = flat_candles(120, start=START - 14 * 3_600_000)
        forming = {"t": candles[-1]["t"] + TF, "o": PRICE, "h": PRICE + 10, "l": PRICE - 10,
                   "c": PRICE, "v": 5.0}
        rec = self.tick(self.engine(ai=FakeAI()), signals=[signal_at(candles)],
                        candles=candles, now_ms=forming["t"] + 60_000)
        self.assertEqual(rec["action"], "SKIP")
        self.assertIn("세션", rec["events"][0])

    def test_entry_window_blocks_a_late_start(self):
        # 봉이 시작되고 10분이 지난 뒤 실행하면(늦은 시작) 추격하지 않는다
        engine = self.engine(ai=FakeAI())
        rec = self.tick(engine, now_ms=self.forming["t"] + 600_000)
        self.assertEqual(rec["action"], "SKIP")
        self.assertIn("진입 창", rec["events"][0])
        self.assertEqual(engine.client.get_positions("BTCUSDT"), [])

    def test_existing_exchange_position_blocks_new_entry(self):
        client = self.client()
        client.place_market_order("BTCUSDT", "Buy", stop_loss=PRICE - 1000,
                                  take_profit=PRICE + 1000, entry_price=PRICE)
        rec = self.tick(self.engine(ai=FakeAI(), client=client))
        self.assertEqual(rec["action"], "SKIP")
        self.assertIn("이미", rec["events"][0])
        self.assertEqual(len(client.get_positions("BTCUSDT")), 1)

    def test_order_rejected_by_50_dollar_rule_is_reported(self):
        # 손절 거리 2,000달러 → 최소 수량 0.001의 손실 $2 > 예산 $1.5 → 주문 거부(SKIP)
        candles = flat_candles(120)
        sig = signal_at(candles, atr=2_000 / 3)
        engine = self.engine(ai=FakeAI())
        rec = self.tick(engine, signals=[sig])
        self.assertEqual(rec["action"], "SKIP")
        self.assertIn("주문 거부", rec["events"][0])
        self.assertIn("최소 주문 수량", rec["events"][0])
        self.assertEqual(engine.client.get_positions("BTCUSDT"), [])

    def test_restart_resumes_position(self):
        engine = self.engine(ai=FakeAI())
        self.tick(engine)
        again = self.engine(ai=FakeAI(), client=engine.client)
        self.assertIsNotNone(json.loads(Path(again.state_path).read_text(encoding="utf-8"))["position"])
        rec = self.tick(again, signals=[])
        self.assertNotEqual(rec["action"], "OPEN")
        self.assertEqual(len(again.client.get_positions("BTCUSDT")), 1)

    def test_logs_never_contain_api_key(self):
        os.environ["BYBIT_API_KEY"] = "test-key-should-not-appear"
        os.environ["BYBIT_API_SECRET"] = "test-secret-should-not-appear"
        engine = self.engine(ai=FakeAI())
        self.tick(engine)
        for path in (engine.state_path, engine.log_path):
            text = Path(path).read_text(encoding="utf-8")
            self.assertNotIn("test-key-should-not-appear", text)
            self.assertNotIn("test-secret-should-not-appear", text)
            self.assertNotIn("BYBIT_API", text)


class MockExitTests(TestnetAutoTestBase):
    def open_position(self, engine):
        rec = self.tick(engine, signals=[self.sig])
        self.assertEqual(rec["action"], "OPEN")
        return json.loads(Path(engine.state_path).read_text(encoding="utf-8"))["position"]

    def test_stop_hit_records_minus_one_r(self):
        engine = self.engine(ai=FakeAI())
        pos = self.open_position(engine)
        spike = {"t": pos["entry_bar_time"], "o": PRICE, "h": pos["stop"] + 100,
                 "l": PRICE - 100, "c": PRICE, "v": 5.0}
        rec = self.tick(engine, signals=[], candles=self.candles + [spike],
                        now_ms=spike["t"] + TF + 60_000)
        self.assertEqual(rec["action"], "CLOSE")
        trade = json.loads(Path(engine.state_path).read_text(encoding="utf-8"))["closed"][-1]
        self.assertEqual(trade["reason"], "STOP")
        self.assertAlmostEqual(trade["r"], -1.0, places=2)
        self.assertAlmostEqual(trade["pnl"], -0.9, places=2)
        self.assertIsNone(rec["position"])
        self.assertEqual(engine.client.get_positions("BTCUSDT"), [])

    def test_target_hit_records_plus_four_r(self):
        engine = self.engine(ai=FakeAI())
        pos = self.open_position(engine)
        drop = {"t": pos["entry_bar_time"], "o": PRICE, "h": PRICE + 10,
                "l": pos["target"] - 100, "c": PRICE, "v": 5.0}
        rec = self.tick(engine, signals=[], candles=self.candles + [drop],
                        now_ms=drop["t"] + TF + 60_000)
        self.assertEqual(rec["action"], "CLOSE")
        trade = json.loads(Path(engine.state_path).read_text(encoding="utf-8"))["closed"][-1]
        self.assertEqual(trade["reason"], "TARGET")
        self.assertAlmostEqual(trade["r"], 4.0, places=2)
        self.assertGreater(trade["pnl"], 3.5)

    def test_time_exit_after_max_bars(self):
        engine = self.engine(ai=FakeAI())
        pos = self.open_position(engine)
        bars = [{"t": pos["entry_bar_time"] + k * TF, "o": PRICE, "h": PRICE + 10,
                 "l": PRICE - 10, "c": PRICE, "v": 5.0} for k in range(engine.cfg.max_bars)]
        rec = self.tick(engine, signals=[], candles=self.candles + bars,
                        now_ms=bars[-1]["t"] + TF + 60_000)
        self.assertEqual(rec["action"], "CLOSE")
        trade = json.loads(Path(engine.state_path).read_text(encoding="utf-8"))["closed"][-1]
        self.assertEqual(trade["reason"], "TIME")

    def test_stats_and_killswitch_follow_closed_trades(self):
        engine = self.engine(ai=FakeAI())
        pos = self.open_position(engine)
        spike = {"t": pos["entry_bar_time"], "o": PRICE, "h": pos["stop"] + 100,
                 "l": PRICE - 100, "c": PRICE, "v": 5.0}
        self.tick(engine, signals=[], candles=self.candles + [spike],
                  now_ms=spike["t"] + TF + 60_000)
        state = json.loads(Path(engine.state_path).read_text(encoding="utf-8"))
        self.assertEqual(state["stats"]["trades"], 1)
        self.assertEqual(state["stats"]["stops"], 1)
        self.assertEqual(state["stats"]["win_rate_pct"], 0.0)
        self.assertEqual(state["killswitch"]["trades_today"], 1)
        self.assertLess(state["killswitch"]["realized_pnl"], 0)


class ExchangeTruthTests(TestnetAutoTestBase):
    """실테스트넷(가짜 전송) — 거래소가 진실이다. 포지션이 사라지면 기록을 남기고, 남의 포지션은 건드리지 않는다."""

    def live(self, responses):
        transport = FakeTransport(responses)
        client = BybitClient(api_key="k", api_secret="s", transport=transport)
        return client, transport

    def seed_position(self, engine, **over):
        state = engine.load_state()
        state["position"] = {
            "side": "Sell", "qty": "0.001", "entry": PRICE + 100, "stop": PRICE + 1000,
            "target": PRICE - 3500, "leverage": 3.0, "risk_usd": 0.9,
            "order_id": "seed-1", "signal_time": self.candles[-1]["t"] - TF,
            "entry_bar_time": self.candles[-1]["t"], "entry_time": "2026-09-19T00:00:00+00:00",
            "ai": {"decision": "PASS"}, **over}
        engine.save_state(state)
        return state["position"]

    def test_position_closed_on_exchange_is_recorded_from_closed_pnl(self):
        client, transport = self.live({
            "/v5/account/wallet-balance": BALANCE_50,
            "/v5/position/list": NO_POSITIONS,
            "/v5/position/closed-pnl": {"retCode": 0, "result": {"list": [{
                "symbol": "BTCUSDT", "side": "Sell", "qty": "0.001",
                "avgEntryPrice": str(PRICE + 100), "avgExitPrice": str(PRICE + 1000),
                "closedPnl": "-0.95", "updatedTime": "1790214200001"}]}},
        })
        engine = self.engine(ai=FakeAI(), client=client)
        self.seed_position(engine)
        rec = self.tick(engine, signals=[])
        self.assertEqual(rec["action"], "CLOSE")
        state = json.loads(Path(engine.state_path).read_text(encoding="utf-8"))
        trade = state["closed"][-1]
        self.assertEqual(trade["reason"], "STOP")
        self.assertAlmostEqual(trade["pnl"], -0.95, places=2)
        self.assertIsNone(state["position"])
        paths = [c["path"] for c in transport.calls]
        self.assertNotIn("/v5/order/create", paths)            # 청산은 거래소가 이미 했다

    def test_foreign_position_is_not_touched(self):
        client, transport = self.live({
            "/v5/account/wallet-balance": BALANCE_50,
            "/v5/position/list": {"retCode": 0, "result": {"list": [{
                "symbol": "BTCUSDT", "side": "Buy", "size": "0.002", "avgPrice": "60000",
                "stopLoss": "", "takeProfit": "", "leverage": "3"}]}},
        })
        rec = self.tick(self.engine(ai=FakeAI(), client=client))
        self.assertEqual(rec["action"], "SKIP")
        self.assertIn("포지션", rec["events"][0])
        self.assertNotIn("/v5/order/create", [c["path"] for c in transport.calls])

    def test_local_and_exchange_mismatch_is_not_managed(self):
        client, transport = self.live({
            "/v5/account/wallet-balance": BALANCE_50,
            "/v5/position/list": {"retCode": 0, "result": {"list": [{
                "symbol": "BTCUSDT", "side": "Buy", "size": "0.001", "avgPrice": "60000",
                "stopLoss": "", "takeProfit": "", "leverage": "3"}]}},
        })
        engine = self.engine(ai=FakeAI(), client=client)
        self.seed_position(engine)                              # 내 기록은 Sell
        rec = self.tick(engine, signals=[])
        self.assertEqual(rec["action"], "SKIP")
        self.assertIn("다릅니다", rec["events"][0])
        self.assertNotIn("/v5/order/create", [c["path"] for c in transport.calls])

    def test_live_price_already_past_stop_blocks_order(self):
        client, transport = self.live({
            "/v5/account/wallet-balance": BALANCE_50,
            "/v5/position/list": NO_POSITIONS,
            "/v5/market/tickers": {"retCode": 0, "result": {"list": [{"lastPrice": "62000"}]}},
        })
        rec = self.tick(self.engine(ai=FakeAI(), client=client))
        self.assertEqual(rec["action"], "SKIP")
        self.assertIn("손절", rec["events"][0])
        self.assertNotIn("/v5/order/create", [c["path"] for c in transport.calls])

    def test_balance_error_stops_the_scan_without_ordering(self):
        client, transport = self.live({"/v5/account/wallet-balance": {
            "retCode": 10001, "retMsg": "params error"}})
        rec = self.tick(self.engine(ai=FakeAI(), client=client))
        self.assertEqual(rec["action"], "ERROR")
        self.assertIn("거래소 조회 실패", rec["events"][0])
        self.assertEqual([c["path"] for c in transport.calls], ["/v5/account/wallet-balance"])

    def test_live_order_sends_tp_sl_and_leverage(self):
        client, transport = self.live({
            "/v5/account/wallet-balance": BALANCE_50,
            "/v5/position/list": NO_POSITIONS,
            "/v5/market/tickers": {"retCode": 0, "result": {"list": [{"lastPrice": "60100"}]}},
            "/v5/market/instruments-info": {"retCode": 0, "result": {"list": [{
                "symbol": "BTCUSDT",
                "lotSizeFilter": {"minOrderQty": "0.001", "qtyStep": "0.001", "maxOrderQty": "100"},
                "priceFilter": {"tickSize": "0.10"}}]}},
            "/v5/position/set-leverage": {"retCode": 0, "result": {}},
            "/v5/order/create": {"retCode": 0, "result": {"orderId": "live-1"}},
        })
        engine = self.engine(ai=FakeAI(), client=client)
        rec = self.tick(engine)
        self.assertEqual(rec["action"], "OPEN")
        body = [c for c in transport.calls if c["path"] == "/v5/order/create"][0]["body"]
        self.assertEqual(body["symbol"], "BTCUSDT")
        self.assertEqual(body["side"], "Sell")
        self.assertEqual(body["qty"], "0.001")
        self.assertEqual(body["stopLoss"], str(int(PRICE + 100 + 3 * ATR)))
        self.assertEqual(body["takeProfit"], str(int(PRICE + 100 - 12 * ATR)))
        self.assertEqual(body["tpslMode"], "Full")
        lev = [c for c in transport.calls if c["path"] == "/v5/position/set-leverage"][0]["body"]
        self.assertEqual(lev["buyLeverage"], "3")
        state = json.loads(Path(engine.state_path).read_text(encoding="utf-8"))
        self.assertEqual(state["position"]["order_id"], "live-1")
        self.assertEqual(state["mode"], "testnet")
        self.assertTrue(state["rule"]["testnet_only"])

    def test_sync_clock_uses_exchange_time_and_mock_needs_no_request(self):
        client, transport = self.live({"/v5/market/time": {
            "retCode": 0, "result": {"timeSecond": "1800000000", "timeNano": "0"},
            "time": 1800000000000}})
        result = self.engine(client=client).sync_clock()
        self.assertFalse(result["mock"])
        self.assertIsInstance(result["offset_ms"], int)
        self.assertNotEqual(result["offset_ms"], 0)
        self.assertEqual([c["path"] for c in transport.calls], ["/v5/market/time"])
        self.assertEqual(self.engine().sync_clock(), {"offset_ms": 0, "mock": True})

    def test_status_reports_account_and_rule(self):
        engine = self.engine(ai=False)
        self.tick(engine)
        status = engine.status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["mode"], "mock")
        self.assertEqual(status["client"]["baseUrl"], TESTNET_BASE_URL)
        self.assertEqual(status["account"]["equity"], 50.0)
        self.assertTrue(status["rule"]["testnet_only"])
        self.assertEqual(len(status["log"]), 1)

    def test_closed_pnl_before_entry_is_ignored(self):
        client, transport = self.live({
            "/v5/account/wallet-balance": BALANCE_50,
            "/v5/position/list": NO_POSITIONS,
            "/v5/position/closed-pnl": {"retCode": 0, "result": {"list": [{
                "symbol": "BTCUSDT", "side": "Sell", "qty": "0.001",
                "avgEntryPrice": "60000", "avgExitPrice": "60900",
                "closedPnl": "-5.00", "updatedTime": "1000"}]}},   # 진입 이전 옛 기록
        })
        engine = self.engine(ai=FakeAI(), client=client)
        self.seed_position(engine)
        rec = self.tick(engine, signals=[])
        self.assertEqual(rec["action"], "CLOSE")
        trade = json.loads(Path(engine.state_path).read_text(encoding="utf-8"))["closed"][-1]
        self.assertIsNone(trade["pnl"])                        # 남의 옛 손익을 내 것으로 쓰지 않는다
        self.assertIn("미확인", trade.get("note", "미확인"))


class StopAndLockTests(TestnetAutoTestBase):
    def test_pause_file_blocks_entry(self):
        engine = self.engine(ai=FakeAI())
        Path(engine.pause_path).write_text("stop\n", encoding="utf-8")
        self.assertTrue(engine.paused())
        rec = self.tick(engine)
        self.assertEqual(rec["action"], "SKIP")
        self.assertIn("수동 정지", rec["events"][0])
        self.assertEqual(engine.client.get_positions("BTCUSDT"), [])

    def test_stop_and_resume_cli_manage_the_pause_file(self):
        common = ["--mock", "--state", os.path.join(self.dir, "cli_state.json"),
                  "--log", os.path.join(self.dir, "cli_log.jsonl"),
                  "--killswitch", os.path.join(self.dir, "cli_ks.json"), "--quiet"]
        self.assertEqual(main(["--stop"] + common), 0)
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "testnet_auto_pause.mock.txt")))
        self.assertEqual(main(["--resume"] + common), 0)
        self.assertFalse(os.path.isfile(os.path.join(self.dir, "testnet_auto_pause.mock.txt")))

    def test_run_lock_blocks_a_second_process(self):
        path = os.path.join(self.dir, "run.lock")
        first = RunLock(path)
        self.assertTrue(first.acquire())
        self.assertFalse(RunLock(path).acquire())          # 두 번째 창은 거부
        first.release()
        self.assertTrue(RunLock(path).acquire())

    def test_stale_lock_is_taken_over(self):
        path = os.path.join(self.dir, "stale.lock")
        Path(path).write_text("old\n", encoding="utf-8")
        old = time.time() - 3 * 3600
        os.utime(path, (old, old))
        lock = RunLock(path)
        self.assertTrue(lock.acquire())                    # 꺼진 프로세스의 흔적은 넘어간다
        lock.release()


class ReplayAndCliTests(TestnetAutoTestBase):
    def write_candles(self):
        path = os.path.join(self.dir, "candles.json")
        Path(path).write_text(json.dumps(self.candles), encoding="utf-8")
        return path

    def test_replay_runs_offline_and_writes_log(self):
        state = os.path.join(self.dir, "replay_state.json")
        log = os.path.join(self.dir, "replay_log.jsonl")
        ks = os.path.join(self.dir, "replay_ks.json")
        code = main(["--once", "--mock", "--no-ai", "--candles", self.write_candles(),
                     "--state", state, "--log", log, "--killswitch", ks, "--quiet"])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(state))
        self.assertEqual(len(Path(log).read_text(encoding="utf-8").strip().splitlines()), 1)

    def test_bad_loss_pct_is_refused_before_any_order(self):
        code = main(["--once", "--mock", "--loss-pct", "9", "--quiet"])
        self.assertEqual(code, 2)

    def test_reset_refuses_while_position_is_open(self):
        engine = self.engine(ai=FakeAI())
        self.tick(engine)
        with self.assertRaises(RuntimeError):
            engine.reset()
        self.assertEqual(len(engine.client.get_positions("BTCUSDT")), 1)

    def test_engine_signal_from_fixture_reaches_order(self):
        """커밋된 15분봉을 30분봉으로 묶어 실제 엔진이 세운 M1 자리로 진입까지 확인한다."""
        path = Path(__file__).resolve().parent.parent / "data" / "btc_15m_3600.json"
        if not path.is_file():
            self.skipTest("픽스처 없음")
        from backend.ai_filter import nominee_setup, select_signals
        from backend.strategy import Engine, resample
        candles = json.loads(path.read_text(encoding="utf-8"))
        candles.sort(key=lambda c: c["t"])
        c30 = resample(candles, 30)
        params, cfg = nominee_setup()
        picks = select_signals(c30, Engine(params).run(c30), cfg)
        self.assertTrue(picks, "픽스처에 후보1 M1 신호가 있어야 한다")
        sig = picks[-1]
        prefix = c30[:sig["index"] + 1]
        forming = {"t": prefix[-1]["t"] + TF, "o": prefix[-1]["c"], "h": prefix[-1]["c"] + 1,
                   "l": prefix[-1]["c"] - 1, "c": prefix[-1]["c"], "v": 1.0}
        engine = TestnetAuto(AutoConfig(data_dir=tempfile.mkdtemp(), replay=True),
                             client=self.client(), ai=False)
        rec = engine.tick(candles=prefix + [forming], now_ms=forming["t"] + 60_000)
        self.assertEqual(rec["action"], "OPEN")
        self.assertEqual(rec["signal"]["side"], sig["side"])
        self.assertEqual(len(engine.client.get_positions("BTCUSDT")), 1)


def staged_candles(prior_high, prior_low, last_high, last_low, n=79):
    """앞 n봉은 같은 폭, 마지막 확정 봉만 다른 폭 — 변동성·기준선 상황을 따로 만든다(최소 60봉 이상)."""
    bars = [{"t": START + i * TF, "o": PRICE, "h": prior_high, "l": prior_low, "c": PRICE,
             "v": 10.0} for i in range(n)]
    bars.append({"t": START + n * TF, "o": PRICE, "h": last_high, "l": last_low, "c": PRICE,
                 "v": 10.0})
    return bars


def mixed_volatility_candles(n=79):
    """5봉마다 큰 봉, 나머지는 작은 봉 — ATR 평균은 낮은데 마지막 봉만 폭이 큰 '변동성 급증'."""
    bars = []
    for i in range(n):
        wide = (i % 5 == 0)
        bars.append({"t": START + i * TF, "o": PRICE, "h": PRICE + (800 if wide else 25),
                     "l": PRICE - (800 if wide else 25), "c": PRICE, "v": 10.0})
    bars.append({"t": START + n * TF, "o": PRICE, "h": PRICE + 900, "l": PRICE - 900,
                 "c": PRICE, "v": 10.0})
    return bars


class FakeClock:
    """실제로 기다리지 않는 시계 — sleep이 시각을 앞으로 밀고, 정해진 시각에 Ctrl+C를 흔낸다."""

    def __init__(self, start_ms=START, stop_at=None):
        self.ms = int(start_ms)
        self.stop_at = stop_at
        self.sleeps = 0

    def now(self):
        return self.ms

    def sleep(self, sec):
        self.sleeps += 1
        self.ms += int(round(float(sec) * 1000))
        if self.stop_at is not None and self.ms >= self.stop_at:
            raise KeyboardInterrupt


class StubEngine:
    """run_live_loop가 쓰는 부분(tick·quote·state·cfg)만 있는 가짜 엔진 — 주기·출력만 시험한다."""

    def __init__(self, watch=None, adaptive=True, price=PRICE, state=None):
        self.cfg = AutoConfig(data_dir=tempfile.mkdtemp(), adaptive=adaptive)
        self.watch = watch or {"adaptive": adaptive, "mode": "normal", "interval_min": 30,
                               "reasons": []}
        self.price = price
        self.state = state if state is not None else {"position": None}
        self.ticks = 0

    @property
    def interval_min(self):
        return 30

    def tick(self):
        self.ticks += 1
        return {"ts": "2026-09-19T02:01:30+00:00", "bar_kst": "2026-09-19 11:30",
                "action": "HOLD", "price": self.price, "events": [], "position": None,
                "equity": 50.0, "watch": self.watch,
                "stats": {"trades": 0, "win_rate_pct": None, "net_pnl": 0.0, "avg_r": None}}

    def quote(self):
        return {"price": self.price, "source": "ticker", "note": ""}

    def load_state(self):
        return self.state


class AdaptiveWatchTests(TestnetAutoTestBase):
    """상황 맞춤 감시 — 집중 조건·주기 결정·안전장치 유지."""

    def adaptive_engine(self, ai=None, client=None, **cfg):
        return self.engine(ai=ai, client=client,
                           cfg=AutoConfig(data_dir=self.dir, adaptive=True, **cfg))

    def tick_bars(self, engine, bars, signals=None):
        forming = {"t": bars[-1]["t"] + TF, "o": bars[-1]["c"], "h": bars[-1]["c"] + 5,
                   "l": bars[-1]["c"] - 5, "c": bars[-1]["c"], "v": 1.0}
        return engine.tick(candles=bars + [forming], now_ms=forming["t"] + 60_000,
                           signals=[] if signals is None else signals)

    def test_calm_market_far_from_levels_stays_on_the_bar_interval(self):
        bars = staged_candles(PRICE + 600, PRICE - 600, PRICE + 100, PRICE - 100)
        rec = self.tick_bars(self.adaptive_engine(ai=False), bars)
        self.assertEqual(rec["watch"]["mode"], "normal")
        self.assertEqual(rec["watch"]["interval_min"], 30)
        self.assertEqual(rec["watch"]["reasons"], [])
        self.assertIsNone(volatility_spike(bars))

    def test_volatility_spike_switches_to_5_minutes(self):
        bars = mixed_volatility_candles()
        self.assertGreater(volatility_spike(bars), 1.5)
        rec = self.tick_bars(self.adaptive_engine(ai=False), bars)
        self.assertEqual(rec["watch"]["mode"], "focus")
        self.assertEqual(rec["watch"]["interval_min"], 5)
        self.assertTrue(any("변동성" in r for r in rec["watch"]["reasons"]))

    def test_price_near_swing_high_switches_to_5_minutes(self):
        bars = staged_candles(PRICE + 150, PRICE - 450, PRICE + 80, PRICE - 80)
        bsl, ssl = swing_levels(bars)
        self.assertAlmostEqual(bsl, PRICE + 150, places=2)
        level = nearest_level(PRICE, bsl, ssl)
        self.assertEqual(level["name"], "BSL")
        rec = self.tick_bars(self.adaptive_engine(ai=False), bars)
        self.assertEqual(rec["watch"]["mode"], "focus")
        self.assertTrue(any("BSL" in r for r in rec["watch"]["reasons"]))

    def test_nearest_level_picks_the_closer_side_and_respects_the_band(self):
        self.assertEqual(nearest_level(PRICE, PRICE + 100, PRICE - 500)["name"], "BSL")
        self.assertEqual(nearest_level(PRICE, PRICE + 500, PRICE - 100)["name"], "SSL")
        self.assertIsNone(nearest_level(PRICE, PRICE + 1000, PRICE - 1000))
        self.assertIsNone(nearest_level(None, PRICE + 10, PRICE - 10))

    def test_open_position_switches_to_focus_and_reason_is_logged(self):
        engine = self.adaptive_engine(ai=FakeAI())
        first = self.tick(engine)
        self.assertEqual(first["action"], "OPEN")
        self.assertEqual(first["watch"]["interval_min"], 5)         # 주문한 스캔부터 다음 대기가 5분
        rec = self.tick(engine, signals=[])
        self.assertEqual(rec["watch"]["mode"], "focus")
        self.assertEqual(rec["watch"]["interval_min"], 5)
        self.assertIn("열린 포지션", rec["watch"]["reasons"])

    def test_adaptive_off_never_shortens_the_wait(self):
        engine = self.engine(ai=FakeAI())                       # 기본값 adaptive=False
        self.tick(engine)
        rec = self.tick(engine, signals=[])
        self.assertEqual(rec["watch"]["mode"], "normal")
        self.assertEqual(rec["watch"]["interval_min"], 30)
        self.assertFalse(rec["watch"]["adaptive"])
        self.assertIn("열린 포지션", rec["watch"]["reasons"])    # 조건은 적어 두되 주기는 그대로

    def test_watch_is_saved_in_state_and_log(self):
        engine = self.adaptive_engine(ai=False)
        self.tick_bars(engine, mixed_volatility_candles())
        state = json.loads(Path(engine.state_path).read_text(encoding="utf-8"))
        self.assertEqual(state["watch"]["mode"], "focus")
        self.assertEqual(state["watch"]["interval_min"], 5)
        row = json.loads(Path(engine.log_path).read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(row["watch"]["interval_min"], 5)
        self.assertIn("변동성", " ".join(row["watch"]["reasons"]))

    def test_focus_mode_keeps_the_50_dollar_safety_limits(self):
        client = self.client()
        engine = self.adaptive_engine(ai=FakeAI(), client=client)
        rec = self.tick(engine)
        self.assertEqual(rec["action"], "OPEN")
        plan = rec["order"]["plan"]
        self.assertLessEqual(plan["expectedLossUsd"], 1.5)       # 50달러 × 3%
        self.assertLessEqual(plan["lossBudgetUsd"], 1.5)
        self.assertLessEqual(plan["leverage"], 5)
        self.assertEqual(rec["base_url"], TESTNET_BASE_URL)
        self.assertTrue(rec["testnet_only"])
        self.assertTrue(rec["sandbox_only"])

    def test_focus_scans_do_not_review_the_same_signal_again(self):
        ai = FakeAI()
        engine = self.adaptive_engine(ai=ai)
        self.tick(engine)
        self.assertEqual(ai.calls, 1)
        rec = engine.tick(candles=self.with_forming(), now_ms=self.now + 5 * 60_000,
                          signals=[self.sig])
        self.assertNotEqual(rec["action"], "OPEN")
        self.assertEqual(ai.calls, 1)                            # 같은 신호를 다시 심사하지 않는다

    def test_focus_scan_with_open_position_skips_ai_for_a_new_signal(self):
        ai = FakeAI()
        engine = self.adaptive_engine(ai=ai)
        self.tick(engine)
        extra = {"t": self.candles[-1]["t"] + TF, "o": PRICE, "h": PRICE + 300,
                 "l": PRICE - 300, "c": PRICE, "v": 10.0}
        bars = self.candles + [extra]
        forming = {"t": extra["t"] + TF, "o": PRICE, "h": PRICE + 300, "l": PRICE - 300,
                   "c": PRICE, "v": 1.0}
        rec = engine.tick(candles=bars + [forming], now_ms=forming["t"] + 60_000,
                          signals=[signal_at(bars)])
        self.assertNotEqual(rec["action"], "OPEN")
        self.assertEqual(ai.calls, 1)                            # 포지션 중이면 AI를 부르지 않는다
        self.assertEqual(rec["watch"]["mode"], "focus")

    def test_next_scan_ms_matches_bar_close_for_30_and_tightens_to_5(self):
        now = START + 30_000
        self.assertEqual(next_scan_ms(now, 30), next_bar_close_ms(now, TF))
        focus = next_scan_ms(now, 5)
        self.assertLessEqual(focus - now, 5 * 60_000)          # 5분 격자의 다음 마감+90초
        self.assertGreater(focus - now, 0)
        self.assertEqual(next_scan_ms(focus, 5) - focus, 5 * 60_000)
        at30 = next_scan_ms(now, 30)
        self.assertEqual(next_scan_ms(at30, 30) - at30, 30 * 60_000)   # 30분 격자도 그대로 돈다


class LiveTickerTests(TestnetAutoTestBase):
    """1분 상태 줄 + 루프 반응성 — 실제로 기다리지 않고 시계를 흉내내 확인한다."""

    def test_live_line_shows_price_mode_and_countdown(self):
        watch = {"adaptive": True, "mode": "normal", "interval_min": 30, "reasons": []}
        line = format_live_line(now_ms=START, symbol="BTCUSDT", price=60_975.5,
                                state={"position": None}, watch=watch,
                                due_ms=START + 28 * 60_000 + 30_000)
        self.assertIn("KST", line)
        self.assertIn("BTCUSDT 60,975.50", line)
        self.assertIn("포지션 없음", line)
        self.assertIn("감시 일반 30분", line)
        self.assertIn("28분 30초 남음", line)

    def test_live_line_shows_position_pnl_and_focus_reason(self):
        pos = {"side": "Buy", "qty": "0.001", "entry": 60_000.0, "stop": 59_100.0,
               "target": 63_600.0, "risk_usd": 0.9}
        watch = {"adaptive": True, "mode": "focus", "interval_min": 5,
                 "reasons": ["열린 포지션"]}
        line = format_live_line(now_ms=START, symbol="BTCUSDT", price=60_150.0,
                                state={"position": pos}, watch=watch, due_ms=START + 60_000)
        self.assertIn("보유 롱 0.001 @ 60,000.00", line)
        self.assertIn("평가 +0.15 USDT(+0.17R)", line)
        self.assertIn("감시 집중 5분", line)
        self.assertIn("열린 포지션", line)

    def test_live_line_survives_a_missing_price(self):
        line = format_live_line(now_ms=START, symbol="BTCUSDT", price=None,
                                state={"position": None}, watch=None, due_ms=None)
        self.assertIn("시세 조회 실패", line)
        self.assertNotIn("None", line)

    def test_quote_falls_back_to_the_last_scan_price(self):
        self.assertAlmostEqual(self.engine().quote()["price"], 60_000.0, places=2)
        cfg = AutoConfig(data_dir=self.dir, symbol="DOGEUSDT")   # mock 시세에 없는 종목
        engine = TestnetAuto(cfg, client=self.client(), ai=False)
        state = engine.load_state()
        state["last_scan"] = {"ts": "x", "price": 123.4}
        engine.save_state(state)
        quote = engine.quote()
        self.assertEqual(quote["price"], 123.4)
        self.assertEqual(quote["source"], "last_scan")
        self.assertIsNone(TestnetAuto(AutoConfig(data_dir=tempfile.mkdtemp(), symbol="DOGEUSDT"),
                                      client=self.client(), ai=False).quote()["price"])

    def test_loop_prints_one_line_per_minute_while_waiting(self):
        engine = StubEngine(watch={"adaptive": True, "mode": "focus", "interval_min": 5,
                                   "reasons": ["열린 포지션"]})
        clock = FakeClock(START)
        lines = []
        code = run_live_loop(engine, clock=clock.now, sleep=clock.sleep, out=lines.append,
                             max_rounds=3)
        self.assertEqual(code, 0)
        live = [l for l in lines if "포지션 없음" in l]
        self.assertGreaterEqual(len(live), 4)                     # 5분 대기 × 2회 → 1분마다
        self.assertTrue(all("감시 집중 5분" in l for l in live))
        self.assertTrue(all("다음 점검" in l and "남음" in l for l in live))

    def test_loop_honours_the_focus_interval_from_the_scan(self):
        engine = StubEngine(watch={"adaptive": True, "mode": "focus", "interval_min": 5,
                                   "reasons": ["변동성 급증"]})
        clock = FakeClock(START)
        seen = []
        original = engine.tick

        def tick():
            seen.append(clock.ms)
            return original()

        engine.tick = tick
        run_live_loop(engine, clock=clock.now, sleep=clock.sleep, out=lambda _l: None,
                      max_rounds=2)
        self.assertEqual(seen[1] - seen[0], next_scan_ms(START, 5) - START)   # 30분이 아니라 5분

    def test_loop_keeps_30_minutes_when_adaptive_is_off(self):
        engine = StubEngine(watch={"adaptive": False, "mode": "focus", "interval_min": 5,
                                   "reasons": []}, adaptive=False)
        clock = FakeClock(START)
        seen = []
        original = engine.tick

        def tick():
            seen.append(clock.ms)
            return original()

        engine.tick = tick
        run_live_loop(engine, clock=clock.now, sleep=clock.sleep, out=lambda _l: None,
                      max_rounds=2)
        self.assertEqual(seen[1] - seen[0], next_scan_ms(START, 30) - START)

    def test_loop_stops_immediately_on_ctrl_c(self):
        engine = StubEngine()
        clock = FakeClock(START, stop_at=START + 3 * 60_000 + 30_000)
        lines = []
        code = run_live_loop(engine, clock=clock.now, sleep=clock.sleep, out=lines.append)
        self.assertEqual(code, 0)
        self.assertIn("안전 종료", "".join(lines))
        self.assertEqual(engine.ticks, 1)
        self.assertLess(clock.sleeps, 400)                        # 1초 단위로 끊어 잔다

    def test_loop_retries_two_minutes_after_a_failed_scan(self):
        engine = StubEngine()
        clock = FakeClock(START)
        seen = []
        calls = {"n": 0}

        def flaky_tick():
            seen.append(clock.ms)
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("캔들을 받지 못했습니다")
            return StubEngine.tick(engine)

        engine.tick = flaky_tick
        lines = []
        code = run_live_loop(engine, clock=clock.now, sleep=clock.sleep, out=lines.append,
                             max_rounds=2)
        self.assertEqual(code, 0)
        self.assertEqual(seen[1] - seen[0], 120_000)              # 30분이 아니라 2분 뒤 재시도
        self.assertIn("스캔 실패", "".join(lines))

    def test_quiet_loop_prints_nothing(self):
        engine = StubEngine()
        clock = FakeClock(START)
        lines = []
        run_live_loop(engine, quiet=True, clock=clock.now, sleep=clock.sleep,
                      out=lines.append, max_rounds=1)
        self.assertEqual(lines, [])

    def test_cli_adaptive_flag_and_env_reach_the_scan(self):
        path = os.path.join(self.dir, "adaptive_candles.json")
        Path(path).write_text(json.dumps(self.candles), encoding="utf-8")

        def run(tag, extra):
            state = os.path.join(self.dir, f"{tag}_state.json")
            log = os.path.join(self.dir, f"{tag}_log.jsonl")
            code = main(["--once", "--mock", "--no-ai", "--candles", path,
                         "--state", state, "--log", log,
                         "--killswitch", os.path.join(self.dir, f"{tag}_ks.json"),
                         "--quiet"] + extra)
            self.assertEqual(code, 0)
            row = json.loads(Path(log).read_text(encoding="utf-8").strip().splitlines()[-1])
            return row["watch"]["adaptive"]

        self.assertTrue(run("flag", ["--adaptive"]))
        self.assertFalse(run("off", ["--adaptive", "--no-adaptive"]))
        self.assertFalse(run("plain", []))
        os.environ["BYBIT_ADAPTIVE"] = "true"
        self.assertTrue(run("env", []))
        self.assertFalse(run("env_off", ["--no-adaptive"]))


class PositionMonitorTests(TestnetAutoTestBase):
    """5초 포지션 감시 — +1.5R 절반 익절·본절 이동·소액 폴백(물타기 없음·안전선 그대로)."""

    def seeded(self, qty=0.004, entry=PRICE, stop=PRICE - 900, **over):
        engine = self.engine(ai=FakeAI())
        pos = {"side": "Buy", "qty": str(qty), "entry": float(entry),
               "stop": float(stop), "stop0": float(stop), "target": float(entry) + 3600,
               "leverage": 3.0, "risk_usd": abs(float(entry) - float(stop)) * float(qty),
               "risk_per_unit": abs(float(entry) - float(stop)), "order_id": "seed-1",
               "signal_time": self.candles[-1]["t"], "entry_bar_time": self.candles[-1]["t"],
               "entry_time": "2026-09-19T00:00:00+00:00", "ai": {"decision": "PASS"},
               "tp1_done": False, "be": None, "partial": None, "realized": 0.0, **over}
        state = engine.load_state()
        state["position"] = pos
        engine.save_state(state)
        engine.client._mock_positions["BTCUSDT"] = {
            "symbol": "BTCUSDT", "side": "Buy", "size": float(qty), "entryPrice": float(entry),
            "stopLoss": float(stop), "takeProfit": float(entry) + 3600, "leverage": 3.0,
            "unrealisedPnl": 0.0}
        return engine, pos

    def test_defaults_are_five_seconds_and_one_and_a_half_r(self):
        engine = self.engine(ai=False)
        self.assertEqual(engine.cfg.pos_tick_sec, 5)
        self.assertTrue(engine.cfg.partial_tp)
        self.assertAlmostEqual(engine.cfg.be_trigger_r, 1.5)
        self.assertAlmostEqual(engine.cfg.be_buffer_pct, 0.1)

    def test_first_target_takes_half_and_moves_stop_to_breakeven(self):
        engine, _pos = self.seeded(qty=0.004)
        rec = engine.monitor_position(price=PRICE + 1350)          # 1.5R
        self.assertEqual(rec["action"], "PARTIAL", rec["events"])
        self.assertAlmostEqual(rec["r"], 1.5, places=2)
        state = json.loads(Path(engine.state_path).read_text(encoding="utf-8"))
        pos = state["position"]
        self.assertTrue(pos["tp1_done"])
        self.assertAlmostEqual(float(pos["qty"]), 0.002)            # 절반만 남음
        self.assertAlmostEqual(pos["realized"], 2.7, places=2)      # 0.002 × 1350
        self.assertGreater(pos["stop"], PRICE)                       # 본절 위
        self.assertAlmostEqual(pos["stop"], 60060.0, places=2)       # 진입 +0.1%
        ex = engine.client._mock_positions["BTCUSDT"]
        self.assertAlmostEqual(ex["size"], 0.002)
        self.assertAlmostEqual(ex["stopLoss"], 60060.0, places=2)
        self.assertAlmostEqual(ex["takeProfit"], 63600.0, places=2)  # 4R 목표는 그대로
        self.assertEqual(rec["position"]["tp1_done"], True)

    def test_only_half_is_closed_once(self):
        engine, _pos = self.seeded(qty=0.004)
        engine.monitor_position(price=PRICE + 1350)
        second = engine.monitor_position(price=PRICE + 1500)
        self.assertNotEqual(second["action"], "PARTIAL")
        state = json.loads(Path(engine.state_path).read_text(encoding="utf-8"))
        self.assertAlmostEqual(float(state["position"]["qty"]), 0.002)

    def test_small_position_skips_partial_but_still_moves_breakeven(self):
        engine, _pos = self.seeded(qty=0.001)                     # BTC 최소 수량 → 절반 불가
        rec = engine.monitor_position(price=PRICE + 1350)
        self.assertEqual(rec["action"], "BREAKEVEN", rec["events"])
        self.assertIn("분할 익절 건너뜀", " ".join(rec["events"]))
        self.assertIn("최소 주문 수량", " ".join(rec["events"]))
        state = json.loads(Path(engine.state_path).read_text(encoding="utf-8"))
        pos = state["position"]
        self.assertAlmostEqual(float(pos["qty"]), 0.001)            # 수량 그대로
        self.assertTrue(pos["tp1_done"])
        self.assertAlmostEqual(pos["stop"], 60060.0, places=2)        # 본절은 옮겼다
        self.assertIsNone(pos.get("partial"))
        self.assertAlmostEqual(engine.client._mock_positions["BTCUSDT"]["stopLoss"], 60060.0, places=2)

    def test_monitor_does_not_call_ai_or_touch_entry_rules(self):
        ai = FakeAI()
        engine, _pos = self.seeded(qty=0.004)
        engine.ai = ai
        engine.monitor_position(price=PRICE + 300)
        engine.monitor_position(price=PRICE + 600)
        self.assertEqual(ai.calls, 0)                                # 5초 감시는 AI를 부르지 않는다
        state = json.loads(Path(engine.state_path).read_text(encoding="utf-8"))
        self.assertEqual(state["position"]["target"], PRICE + 3600) # 4R 목표 그대로
        self.assertEqual(state["position"]["stop0"], PRICE - 900)   # 원래 3ATR 손절 기록 유지

    def test_partial_then_four_r_target_counts_both_legs(self):
        lifted = flat_candles(120, price=PRICE + 400)          # 진입 뒤 봉들 — 본절 위에서만 움직임
        engine, _pos = self.seeded(qty=0.004, entry_bar_time=lifted[-1]["t"])
        engine.monitor_position(price=PRICE + 1350)
        bar = {"t": lifted[-1]["t"] + TF, "o": PRICE + 3600, "h": PRICE + 3700,
               "l": PRICE + 1000, "c": PRICE + 3600, "v": 5.0}
        rec = engine.tick(candles=lifted + [bar], now_ms=bar["t"] + TF + 60_000, signals=[])
        self.assertEqual(rec["action"], "CLOSE", rec["events"])
        trade = json.loads(Path(engine.state_path).read_text(encoding="utf-8"))["closed"][-1]
        self.assertEqual(trade["reason"], "TARGET")
        self.assertAlmostEqual(trade["partial_pnl"], 2.7, places=2)
        self.assertAlmostEqual(trade["pnl"], 9.9, places=2)          # 2.7 + 0.002×3600
        self.assertAlmostEqual(trade["r"], 2.75, places=2)           # ½×1.5R + ½×4R

    def test_no_partial_flag_keeps_the_old_behaviour(self):
        engine = self.engine(ai=FakeAI(), cfg=AutoConfig(data_dir=self.dir, partial_tp=False))
        state = engine.load_state()
        state["position"] = {"side": "Buy", "qty": "0.004", "entry": PRICE,
                             "stop": PRICE - 900, "stop0": PRICE - 900, "target": PRICE + 3600,
                             "leverage": 3.0, "risk_usd": 3.6, "risk_per_unit": 900.0,
                             "tp1_done": False, "realized": 0.0}
        engine.save_state(state)
        engine.client._mock_positions["BTCUSDT"] = {
            "symbol": "BTCUSDT", "side": "Buy", "size": 0.004, "entryPrice": PRICE,
            "stopLoss": PRICE - 900, "takeProfit": PRICE + 3600, "leverage": 3.0}
        rec = engine.monitor_position(price=PRICE + 1350)
        self.assertEqual(rec["action"], "WAIT")
        state = json.loads(Path(engine.state_path).read_text(encoding="utf-8"))
        self.assertFalse(state["position"]["tp1_done"])
        self.assertAlmostEqual(float(state["position"]["qty"]), 0.004)
        self.assertAlmostEqual(state["position"]["stop"], PRICE - 900)

    def test_breakeven_exit_is_named_and_keeps_the_gain(self):
        lifted = flat_candles(120, price=PRICE + 400)
        engine, _pos = self.seeded(qty=0.004, entry_bar_time=lifted[-1]["t"])
        engine.monitor_position(price=PRICE + 1350)
        bar = {"t": lifted[-1]["t"] + TF, "o": PRICE + 200, "h": PRICE + 260,
               "l": 60_000.0, "c": 60_010.0, "v": 5.0}
        rec = engine.tick(candles=lifted + [bar], now_ms=bar["t"] + TF + 60_000, signals=[])
        self.assertEqual(rec["action"], "CLOSE", rec["events"])
        trade = json.loads(Path(engine.state_path).read_text(encoding="utf-8"))["closed"][-1]
        self.assertEqual(trade["reason"], "BREAKEVEN")
        self.assertAlmostEqual(trade["pnl"], 2.82, places=2)         # 본절 +0.12 + 1차 익절 2.7
        self.assertGreater(trade["pnl"], 0)                          # 원금 손실 없이 마감

    def test_live_partial_sends_reduce_only_and_breakeven_stop(self):
        pos_before = {"retCode": 0, "result": {"list": [{
            "symbol": "BTCUSDT", "side": "Buy", "size": "0.004", "avgPrice": "60000",
            "stopLoss": "59100", "takeProfit": "63600", "leverage": "3",
            "unrealisedPnl": ""}]}}
        pos_after = {"retCode": 0, "result": {"list": [{
            "symbol": "BTCUSDT", "side": "Buy", "size": "0.002", "avgPrice": "60000",
            "stopLoss": "60060", "takeProfit": "63600", "leverage": "3",
            "unrealisedPnl": ""}]}}
        transport = SequenceTransport({
            "/v5/market/tickers": [{"retCode": 0, "result": {"list": [{"lastPrice": "61350"}]}}],
            "/v5/market/instruments-info": [INSTRUMENTS],
            "/v5/order/create": [{"retCode": 0, "result": {"orderId": "live-partial"}}],
            "/v5/position/list": [pos_before, pos_after],
            "/v5/position/trading-stop": [{"retCode": 0, "result": {}}],
        })
        client = BybitClient(api_key="k", api_secret="s", transport=transport)
        engine, _pos = self.seeded(qty=0.004)
        engine.client = client
        rec = engine.monitor_position()
        self.assertEqual(rec["action"], "PARTIAL", rec["events"])
        order = [c for c in transport.calls if c["path"] == "/v5/order/create"][0]["body"]
        self.assertEqual(order["reduceOnly"], True)
        self.assertEqual(order["side"], "Sell")
        self.assertEqual(order["qty"], "0.002")
        self.assertEqual(order["orderType"], "Market")
        stop = [c for c in transport.calls if c["path"] == "/v5/position/trading-stop"][0]["body"]
        self.assertEqual(stop["stopLoss"], "60060")       # 진입 +0.1% 본절
        self.assertNotIn("takeProfit", stop)               # 4R 목표는 건드리지 않는다
        state = json.loads(Path(engine.state_path).read_text(encoding="utf-8"))
        self.assertAlmostEqual(float(state["position"]["qty"]), 0.002)
        self.assertTrue(state["position"]["tp1_done"])

    def test_failed_breakeven_retries_without_a_second_partial(self):
        engine, _pos = self.seeded(qty=0.004)
        calls = {"n": 0}
        original = engine.client.set_trading_stop

        def flaky(symbol, take_profit=None, stop_loss=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise BybitError("일시 오류(시험)")
            return original(symbol, take_profit=take_profit, stop_loss=stop_loss)

        engine.client.set_trading_stop = flaky
        first = engine.monitor_position(price=PRICE + 1350)
        self.assertEqual(first["action"], "PARTIAL", first["events"])
        state = json.loads(Path(engine.state_path).read_text(encoding="utf-8"))
        self.assertAlmostEqual(float(state["position"]["qty"]), 0.002)
        self.assertFalse(state["position"]["tp1_done"])          # 본절은 아직 못 옮김
        second = engine.monitor_position(price=PRICE + 1360)
        self.assertEqual(second["action"], "PARTIAL", second["events"])
        state = json.loads(Path(engine.state_path).read_text(encoding="utf-8"))
        self.assertAlmostEqual(float(state["position"]["qty"]), 0.002)   # 또 줄이지 않는다
        self.assertTrue(state["position"]["tp1_done"])           # 본절은 다시 시도해 성공
        self.assertAlmostEqual(state["position"]["stop"], 60060.0, places=2)

    def test_cli_pos_tick_and_partial_flags(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--status", "--mock", "--pos-tick-sec", "7", "--no-partial-tp",
                         "--state", os.path.join(self.dir, "s.json"),
                         "--log", os.path.join(self.dir, "l.jsonl"),
                         "--killswitch", os.path.join(self.dir, "k.json")])
        self.assertEqual(code, 0)
        self.assertIn("본절 이동 꺼짐", buf.getvalue())


class MonitorLoopTests(TestnetAutoTestBase):
    """루프가 포지션 보유 중에 현재가를 5초마다 보는지(대기 주기와 별개로)."""

    class Stub:
        def __init__(self, position=True, pos_tick_sec=5):
            self.cfg = AutoConfig(data_dir=tempfile.mkdtemp(), pos_tick_sec=pos_tick_sec)
            self.state = {"position": ({"side": "Buy", "qty": "0.004", "entry": PRICE,
                                        "stop": PRICE - 900, "target": PRICE + 3600}
                                       if position else None)}

        @property
        def interval_min(self):
            return 30

        def tick(self):
            return {"ts": "x", "bar_kst": "x", "action": "HOLD", "price": PRICE, "events": [],
                    "position": self.state.get("position"), "equity": 50.0,
                    "watch": {"adaptive": False, "mode": "normal", "interval_min": 30,
                              "reasons": []},
                    "stats": {"trades": 0, "win_rate_pct": None, "net_pnl": 0.0, "avg_r": None}}

        def monitor_position(self):
            return {"ts": "2026-09-19T02:02:00+00:00", "action": "WAIT", "price": PRICE,
                    "r": 0.2, "events": [], "position": self.state.get("position")}

        def quote(self):
            return {"price": PRICE, "source": "ticker"}

        def load_state(self):
            return dict(self.state)

    def test_position_is_checked_every_five_seconds(self):
        engine = self.Stub(position=True)
        clock = FakeClock(START)
        stamps = []
        original = engine.monitor_position

        def mon():
            stamps.append(clock.ms)
            return original()

        engine.monitor_position = mon
        run_live_loop(engine, clock=clock.now, sleep=clock.sleep, out=lambda _l: None,
                      max_rounds=2)
        self.assertGreaterEqual(len(stamps), 2)
        self.assertEqual(stamps[1] - stamps[0], 5_000)               # 30분/5분이 아니라 5초

    def test_pos_tick_sec_is_configurable(self):
        engine = self.Stub(position=True, pos_tick_sec=9)
        clock = FakeClock(START)
        stamps = []

        def mon():
            stamps.append(clock.ms)
            return {"ts": "2026-09-19T02:02:00+00:00", "action": "WAIT", "price": PRICE,
                    "r": 0.1, "events": [], "position": engine.state.get("position")}

        engine.monitor_position = mon
        run_live_loop(engine, clock=clock.now, sleep=clock.sleep, out=lambda _l: None,
                      max_rounds=2)
        self.assertEqual(stamps[1] - stamps[0], 9_000)

    def test_without_position_there_is_no_price_watch(self):
        engine = self.Stub(position=False)
        clock = FakeClock(START)
        calls = []
        engine.monitor_position = lambda: calls.append(1) or {"action": "IDLE"}
        run_live_loop(engine, clock=clock.now, sleep=clock.sleep, out=lambda _l: None,
                      max_rounds=1)
        self.assertEqual(calls, [])


class SequenceTransport:
    """경로별로 응답을 **순서대로** 돌려주는 가짜 전송 — 주문 전/후 포지션 목록이 달라야 하는 흐름용.

    큐에 하나만 남으면 그 응답을 계속 돌려준다(청산 뒤 재확인처럼 같은 '빈 목록'이 반복되는 경우).
    """

    def __init__(self, responses):
        self.queues = {path: list(items) for path, items in responses.items()}
        self.calls = []

    def __call__(self, method, path, params, body, headers):
        self.calls.append({"method": method, "path": path, "params": dict(params),
                           "body": body, "headers": dict(headers)})
        queue = self.queues.get(path)
        if not queue:
            raise AssertionError(f"예상하지 못한 요청: {method} {path}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        return json.loads(json.dumps(item))


SMOKE_POSITION = {"retCode": 0, "result": {"list": [{
    "symbol": "BTCUSDT", "side": "Buy", "size": "0.002", "avgPrice": "60000",
    "stopLoss": "59400", "takeProfit": "61200", "leverage": "3",
    "unrealisedPnl": "0.4", "updatedTime": "1790220000000"}]}}
SMOKE_CLOSED_PNL = {"retCode": 0, "result": {"list": [{
    "symbol": "BTCUSDT", "side": "Buy", "qty": "0.002", "avgEntryPrice": "60000",
    "avgExitPrice": "59990", "closedPnl": "-0.02", "updatedTime": "1790220003000"}]}}
INSTRUMENTS = {"retCode": 0, "result": {"list": [{
    "symbol": "BTCUSDT",
    "lotSizeFilter": {"minOrderQty": "0.001", "qtyStep": "0.001", "maxOrderQty": "100"},
    "priceFilter": {"tickSize": "0.10"}}]}}


def smoke_responses(**over):
    """시험 주문 성공 흐름의 응답 — 인자로 한 경로씩 바꿔 끼울 수 있다."""
    responses = {
        "/v5/account/wallet-balance": [BALANCE_50],
        "/v5/position/list": [NO_POSITIONS, SMOKE_POSITION, SMOKE_POSITION, NO_POSITIONS],
        "/v5/market/tickers": [{"retCode": 0, "result": {"list": [{"lastPrice": "60000"}]}}],
        "/v5/market/instruments-info": [INSTRUMENTS],
        "/v5/position/set-leverage": [{"retCode": 0, "result": {}}],
        "/v5/order/create": [{"retCode": 0, "result": {"orderId": "smoke-entry"}},
                             {"retCode": 0, "result": {"orderId": "smoke-close"}}],
        "/v5/position/closed-pnl": [SMOKE_CLOSED_PNL],
    }
    responses.update(over)
    return responses


class SmokeTestTests(TestnetAutoTestBase):
    """--smoke-test — 샌드박스 1회 주문 → 3초 뒤 포지션 확인 → 즉시 청산.

    실제로 기다리지 않는다(sleep 주입). 확인하는 것
    1) 주문 1회(TP/SL 동봉) → 포지션 확인 → 청산 1회 → 잔여 0건 흐름
    2) 키가 없거나(mock) 이미 열린 포지션이 있으면 주문하지 않는다
    3) 데모의 closed-pnl 미지원이 시험을 죽이지 않는다
    4) 모의매매·오토 모드 상태 파일은 읽지도 쓰지도 않는다
    """

    def live(self, responses, env=None):
        transport = SequenceTransport(responses)
        client = BybitClient(api_key="k", api_secret="s", env=env, transport=transport)
        return client, transport

    def demo_engine(self, responses=None, **cfg):
        client, transport = self.live(smoke_responses(**(responses or {})), env="demo")
        engine = TestnetAuto(AutoConfig(data_dir=self.dir, **cfg), client=client, ai=False)
        return engine, transport

    def run_smoke(self, engine):
        lines = []
        result = run_smoke_test(engine, sleep=lambda _sec: None, out=lines.append)
        return result, lines

    def test_full_flow_orders_checks_and_closes(self):
        engine, transport = self.demo_engine()
        result, lines = self.run_smoke(engine)
        self.assertTrue(result["ok"], "\n".join(lines))
        bodies = [c["body"] for c in transport.calls if c["path"] == "/v5/order/create"]
        self.assertEqual(len(bodies), 2)                        # 진입 1회 + 청산 1회
        entry, close = bodies
        self.assertEqual(entry["side"], "Buy")
        self.assertEqual(entry["orderType"], "Market")
        self.assertEqual(entry["stopLoss"], "59400")           # 현재가 -1%
        self.assertEqual(entry["takeProfit"], "61200")         # 현재가 +2%
        self.assertEqual(entry["tpslMode"], "Full")
        self.assertEqual(entry["qty"], "0.002")                # 50달러·3%·3배 규칙
        self.assertEqual(close["side"], "Sell")
        self.assertTrue(close["reduceOnly"])
        self.assertEqual(close["qty"], "0.002")
        text = "\n".join(lines)
        self.assertIn("포지션 확인", text)
        self.assertIn("미실현손익", text)
        self.assertIn("잔여 포지션 0건", text)
        self.assertIn("실제 돈은 나가지 않았습니다", text)
        self.assertIsNotNone(result["closed_pnl"])
        self.assertNotIn("미지원", text)

    def test_state_and_paper_files_are_not_touched(self):
        engine, transport = self.demo_engine()
        state_path = Path(engine.state_path)
        paper = Path(self.dir) / "paper_state.json"
        sentinel = '{"scan_count": 7, "closed": [{"pnl": 1.0}], "signals_seen": ["x"]}'
        state_path.write_text(sentinel, encoding="utf-8")
        paper.write_text(sentinel, encoding="utf-8")
        result, _lines = self.run_smoke(engine)
        self.assertTrue(result["ok"])
        self.assertEqual(state_path.read_text(encoding="utf-8"), sentinel)
        self.assertEqual(paper.read_text(encoding="utf-8"), sentinel)
        self.assertFalse((Path(self.dir) / "testnet_auto_log.demo.jsonl").exists())

    def test_existing_position_is_refused_without_ordering(self):
        engine, transport = self.demo_engine({
            "/v5/position/list": [{"retCode": 0, "result": {"list": [{
                "symbol": "BTCUSDT", "side": "Buy", "size": "0.003", "avgPrice": "60000",
                "stopLoss": "", "takeProfit": "", "leverage": "3"}]}}],
            "/v5/order/create": [{"retCode": 0, "result": {"orderId": "never"}}],
        })
        result, lines = self.run_smoke(engine)
        self.assertFalse(result["ok"])
        self.assertIn("열린 포지션이 있어 시험 주문을 건너뜁니다", result["refused"])
        self.assertNotIn("/v5/order/create", [c["path"] for c in transport.calls])
        self.assertNotIn("/v5/account/wallet-balance", [c["path"] for c in transport.calls])

    def test_missing_keys_refuse_before_any_request(self):
        engine = self.engine(ai=False)                          # 키 없음 → mock
        self.assertTrue(engine.mock)
        result, lines = self.run_smoke(engine)
        self.assertFalse(result["ok"])
        self.assertIn("BYBIT_API_KEY/SECRET이 필요합니다(샌드박스 전용)", "\n".join(lines))

    def test_demo_closed_pnl_unsupported_does_not_break_the_test(self):
        engine, transport = self.demo_engine({
            "/v5/position/closed-pnl": [{"retCode": 404, "retMsg": "Not Found"}]})
        result, lines = self.run_smoke(engine)
        self.assertTrue(result["ok"])
        self.assertIn("데모 계정 정책상 closed-pnl 미지원", "\n".join(lines))
        self.assertIn("데모 계정 정책상 closed-pnl 미지원", result["pnl_note"])

    def test_order_rejected_by_the_50_dollar_rule_is_reported(self):
        engine, transport = self.demo_engine({
            "/v5/account/wallet-balance": [{"retCode": 0, "result": {"list": [{
                "totalEquity": "1", "coin": [{"coin": "USDT", "equity": "1",
                                                "walletBalance": "1",
                                                "availableToWithdraw": "1"}]}]}}]})
        result, lines = self.run_smoke(engine)
        self.assertFalse(result["ok"])
        self.assertIn("주문 거부", result["refused"])
        self.assertIn("최소 주문 수량", result["refused"])
        self.assertNotIn("/v5/order/create", [c["path"] for c in transport.calls])

    def test_uncertain_order_error_is_not_repeated(self):
        engine, transport = self.demo_engine({
            "/v5/order/create": [{"retCode": 10016, "retMsg": "server error"},
                                 {"retCode": 0, "result": {"orderId": "smoke-close"}}]})
        result, lines = self.run_smoke(engine)
        self.assertFalse(result["ok"])
        self.assertIn("다시 보내지 않고", "\n".join(lines))
        creates = [c["body"] for c in transport.calls if c["path"] == "/v5/order/create"]
        self.assertEqual([b["side"] for b in creates].count("Buy"), 1)   # 진입 주문은 한 번만
        if len(creates) > 1:                                    # 오류 뒤 포지션이 보이면 정리 청산
            self.assertTrue(creates[1]["reduceOnly"])

    def test_cli_refuses_in_mock_mode(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--smoke-test", "--mock", "--demo", "--quiet",
                         "--state", os.path.join(self.dir, "smoke_state.json"),
                         "--log", os.path.join(self.dir, "smoke_log.jsonl"),
                         "--killswitch", os.path.join(self.dir, "smoke_ks.json")])
        self.assertEqual(code, 1)
        self.assertIn("BYBIT_API_KEY/SECRET이 필요합니다", buf.getvalue())
        self.assertFalse(os.path.exists(os.path.join(self.dir, "smoke_state.json")))

    def test_cli_refuses_while_a_loop_holds_the_lock(self):
        state = os.path.join(self.dir, "locked_state.json")
        os.environ["BYBIT_API_KEY"] = "k"
        os.environ["BYBIT_API_SECRET"] = "s"
        Path(state + ".lock").write_text("1234 running\n", encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--smoke-test", "--demo", "--quiet", "--state", state,
                         "--log", os.path.join(self.dir, "locked_log.jsonl"),
                         "--killswitch", os.path.join(self.dir, "locked_ks.json")])
        self.assertEqual(code, 1)
        self.assertIn("이미 실행 중인 것 같습니다", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
