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

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from backend.ai_filter import FilterConfig
from backend.bybit_client import BybitClient, TESTNET_BASE_URL
from backend.testnet_auto import (AutoConfig, RunLock, TestnetAuto, build_client, main,
                                  next_bar_close_ms)

TF = 1_800_000                     # 30분
START = 1_790_000_000_000          # 2026-09-19 14:00 UTC → 진입 봉은 02:00 UTC(아시아 세션)
PRICE = 60_000.0
ATR = 300.0                        # 3ATR = 900 → 0.001개 손실 $0.90 (예산 $1.50 안)
ENV_KEYS = ("BYBIT_API_KEY", "BYBIT_API_SECRET", "BYBIT_TESTNET")


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


if __name__ == "__main__":
    unittest.main()
