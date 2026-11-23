"""바이비트 테스트넷 클라이언트 테스트 — 안전 규칙이 '말'이 아니라 '동작'인지 확인한다.

확인하는 것
1) 키가 없으면 mock: 네트워크 요청이 한 번도 나가지 않는다
2) BYBIT_TESTNET=false, 메인넷 주소, 알 수 없는 값은 만들 때 거부한다
3) V5 서명(GET query / POST body)과 헤더 형식
4) BTC/ETH/SOL 수량을 qtyStep으로 내림, 명목가 상한(기준자본×레버리지) 반영
5) 최소 수량으로 진입할 때 예상 손실이 예산을 넘으면 주문 거부
6) 손실 예산 2~4%, 레버리지 1~5배의 명시적 검증
7) TP/SL이 함께 실린 payload 형식(Full 모드)과 청산 주문(reduceOnly)
"""

import json
import os
import unittest
from decimal import Decimal

from backend.bybit_client import (
    DEFAULT_LEVERAGE,
    DEMO_BASE_URL,
    MAX_SEED_USD,
    TESTNET_BASE_URL,
    BybitAPIError,
    BybitClient,
    BybitConfigError,
    BybitError,
    OrderRejected,
    fmt_dec,
    floor_to_step,
    risk_limits,
    v5_signature,
)

ENV_KEYS = ("BYBIT_API_KEY", "BYBIT_API_SECRET", "BYBIT_TESTNET", "BYBIT_DEMO", "BYBIT_ENV")


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


def no_network(*_args, **_kwargs):
    raise AssertionError("mock 모드에서 네트워크 요청이 나갔습니다")


class BybitClientTestBase(unittest.TestCase):
    def setUp(self):
        self._saved = {key: os.environ.pop(key, None) for key in ENV_KEYS}

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def mock_client(self, **kwargs):
        kwargs.setdefault("transport", no_network)
        return BybitClient(**kwargs)

    def live_client(self, transport, **kwargs):
        kwargs.setdefault("api_key", "test-key")
        kwargs.setdefault("api_secret", "test-secret")
        return BybitClient(transport=transport, **kwargs)


class ModeGuardTests(BybitClientTestBase):
    def test_no_keys_means_mock_and_no_network(self):
        client = self.mock_client()
        self.assertTrue(client.is_mock)
        balance = client.get_balance()
        self.assertEqual(balance["equity"], 50.0)
        self.assertTrue(balance["mock"])
        self.assertEqual(client.get_positions(), [])
        client.set_leverage("BTCUSDT", 3)
        order = client.place_market_order("BTCUSDT", "Buy", stop_loss=59000, take_profit=62000,
                                          entry_price=60000)
        self.assertTrue(order["mock"])
        self.assertTrue(order["orderId"].startswith("mock-"))
        self.assertEqual(len(client.get_positions("BTCUSDT")), 1)
        closed = client.close_position("BTCUSDT")
        self.assertIn("estimatedPnl", closed["closed"])
        self.assertEqual(client.get_positions("BTCUSDT"), [])

    def test_env_keys_switch_to_live_mode(self):
        os.environ["BYBIT_API_KEY"] = "k"
        os.environ["BYBIT_API_SECRET"] = "s"
        transport = FakeTransport({"/v5/account/wallet-balance": {
            "retCode": 0, "result": {"list": [{"totalEquity": "50", "coin": [
                {"coin": "USDT", "equity": "50", "walletBalance": "50", "availableToWithdraw": "50"}]}]}}})
        client = BybitClient(transport=transport)
        self.assertFalse(client.is_mock)
        self.assertEqual(client.base_url, TESTNET_BASE_URL)
        self.assertEqual(len(transport.calls), 0)
        self.assertEqual(client.get_balance()["equity"], 50.0)

    def test_single_key_is_refused(self):
        os.environ["BYBIT_API_KEY"] = "only-key"
        with self.assertRaises(BybitConfigError):
            BybitClient(transport=no_network)

    def test_testnet_false_is_refused(self):
        os.environ["BYBIT_TESTNET"] = "false"
        with self.assertRaises(BybitConfigError) as ctx:
            BybitClient(transport=no_network)
        self.assertIn("실전", str(ctx.exception))
        self.assertIn("BYBIT_TESTNET", str(ctx.exception))

    def test_unknown_testnet_value_is_refused(self):
        os.environ["BYBIT_TESTNET"] = "maybe"
        with self.assertRaises(BybitConfigError):
            BybitClient(transport=no_network)

    def test_mainnet_url_is_refused(self):
        for url in ("https://api.bybit.com", "https://api.bybit.com/", "http://api-testnet.bybit.com"):
            with self.subTest(url=url), self.assertRaises(BybitConfigError):
                BybitClient(api_key="k", api_secret="s", base_url=url, transport=no_network)

    def test_explicit_testnet_flag_wins(self):
        os.environ["BYBIT_TESTNET"] = "false"
        client = BybitClient(testnet=True, transport=no_network)
        self.assertTrue(client.is_mock)
        self.assertEqual(client.base_url, TESTNET_BASE_URL)


class DemoModeTests(BybitClientTestBase):
    """Bybit Demo Trading(api-demo.bybit.com) — 테스트넷과 같은 샌드박스, 메인넷은 여전히 거부."""

    BALANCE = {"/v5/account/wallet-balance": {
        "retCode": 0, "result": {"list": [{"totalEquity": "50", "coin": [
            {"coin": "USDT", "equity": "50", "walletBalance": "50", "availableToWithdraw": "50"}]}]}}}

    def demo_client(self, transport=None, **kwargs):
        kwargs.setdefault("api_key", "demo-key")
        kwargs.setdefault("api_secret", "demo-secret")
        return BybitClient(transport=transport or FakeTransport(self.BALANCE), **kwargs)

    def test_demo_env_var_selects_demo_base_url(self):
        os.environ["BYBIT_DEMO"] = "true"
        transport = FakeTransport(self.BALANCE)
        client = self.demo_client(transport)
        self.assertEqual(client.env, "demo")
        self.assertEqual(client.base_url, DEMO_BASE_URL)
        self.assertFalse(client.testnet)              # '테스트넷인가'는 False (데모이지 테스트넷은 아니다)
        self.assertTrue(client.sandbox)
        self.assertFalse(client.is_mock)
        self.assertEqual(client.get_balance()["equity"], 50.0)
        self.assertTrue(any("api-demo.bybit.com" in note for note in client.notes))

    def test_demo_env_alias_bybit_env(self):
        os.environ["BYBIT_ENV"] = "demo"
        client = self.demo_client()
        self.assertEqual(client.env, "demo")
        self.assertEqual(client.base_url, DEMO_BASE_URL)

    def test_demo_wins_over_testnet_when_both_true(self):
        os.environ["BYBIT_DEMO"] = "true"
        os.environ["BYBIT_TESTNET"] = "true"
        client = self.demo_client()
        self.assertEqual(client.env, "demo")
        self.assertEqual(client.base_url, DEMO_BASE_URL)

    def test_demo_runs_even_with_bybit_testnet_false(self):
        os.environ["BYBIT_DEMO"] = "true"
        os.environ["BYBIT_TESTNET"] = "false"
        client = self.demo_client()
        self.assertEqual(client.env, "demo")
        self.assertEqual(client.base_url, DEMO_BASE_URL)

    def test_demo_false_keeps_testnet(self):
        os.environ["BYBIT_DEMO"] = "false"
        os.environ["BYBIT_TESTNET"] = "true"
        client = self.demo_client()
        self.assertEqual(client.env, "testnet")
        self.assertEqual(client.base_url, TESTNET_BASE_URL)

    def test_both_false_is_still_refused(self):
        os.environ["BYBIT_DEMO"] = "false"
        os.environ["BYBIT_TESTNET"] = "false"
        with self.assertRaises(BybitConfigError) as ctx:
            BybitClient(api_key="k", api_secret="s", transport=no_network)
        self.assertIn("실전", str(ctx.exception))
        self.assertIn("BYBIT_TESTNET", str(ctx.exception))

    def test_mainnet_stays_refused_in_demo_env(self):
        os.environ["BYBIT_DEMO"] = "true"
        for url in ("https://api.bybit.com", "https://api.bybit.com/",
                    "http://api-demo.bybit.com", "https://api-demo.bybit.com.evil.com"):
            with self.subTest(url=url), self.assertRaises(BybitConfigError):
                BybitClient(api_key="k", api_secret="s", base_url=url, transport=no_network)

    def test_demo_base_url_alone_selects_demo(self):
        client = self.demo_client(base_url="https://api-demo.bybit.com/")
        self.assertEqual(client.env, "demo")
        self.assertEqual(client.base_url, DEMO_BASE_URL)

    def test_env_argument_and_conflicting_base_url_is_refused(self):
        with self.assertRaises(BybitConfigError):
            BybitClient(api_key="k", api_secret="s", env="demo",
                        base_url=TESTNET_BASE_URL, transport=no_network)

    def test_env_argument_rejects_mainnet(self):
        for env in ("mainnet", "live", "production"):
            with self.subTest(env=env), self.assertRaises(BybitConfigError):
                BybitClient(api_key="k", api_secret="s", env=env, transport=no_network)

    def test_unknown_demo_value_is_refused(self):
        os.environ["BYBIT_DEMO"] = "maybe"
        with self.assertRaises(BybitConfigError):
            BybitClient(api_key="k", api_secret="s", transport=no_network)

    def test_mode_and_limits_report_demo(self):
        os.environ["BYBIT_DEMO"] = "true"
        mode = self.demo_client().mode()
        self.assertEqual(mode["env"], "demo")
        self.assertEqual(mode["baseUrl"], DEMO_BASE_URL)
        self.assertTrue(mode["demo"])
        self.assertFalse(mode["testnet"])
        self.assertTrue(mode["sandbox"])
        self.assertEqual(mode["label"], "데모 트레이딩")
        limits = risk_limits("demo")
        self.assertTrue(limits["sandboxOnly"])
        self.assertEqual(limits["baseUrl"], DEMO_BASE_URL)
        self.assertEqual(limits["envBaseUrls"]["testnet"], TESTNET_BASE_URL)
        self.assertIn("demo", repr(self.demo_client()))

    def test_demo_order_and_close_use_demo_base_with_same_signing(self):
        os.environ["BYBIT_DEMO"] = "true"
        transport = FakeTransport({
            "/v5/market/instruments-info": {"retCode": 0, "result": {"list": [{
                "symbol": "BTCUSDT",
                "lotSizeFilter": {"minOrderQty": "0.001", "qtyStep": "0.001", "maxOrderQty": "100"},
                "priceFilter": {"tickSize": "0.10"}}]}},
            "/v5/position/set-leverage": {"retCode": 0, "result": {}},
            "/v5/order/create": {"retCode": 0, "result": {"orderId": "demo-1"}},
        })
        client = self.demo_client(transport)
        order = client.place_market_order("BTCUSDT", "Buy", stop_loss=59000, take_profit=62000,
                                          entry_price=60000, balance=50.0)
        self.assertEqual(client.base_url, DEMO_BASE_URL)
        self.assertEqual(order["orderId"], "demo-1")
        create = [c for c in transport.calls if c["path"] == "/v5/order/create"][0]
        body_text = json.dumps(create["body"], separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(
            create["headers"]["X-BAPI-SIGN"],
            v5_signature("demo-secret", create["headers"]["X-BAPI-TIMESTAMP"], "demo-key", "5000",
                         body_text))
        self.assertEqual(create["body"]["qty"], "0.001")

    def test_demo_closed_pnl_error_is_visible_as_api_error(self):
        # 데모에서 부가 조회가 404/미지원이면 BybitAPIError로 드러난다 — 감싸는 것은 호출하는 쪽(testnet_auto)
        os.environ["BYBIT_DEMO"] = "true"
        transport = FakeTransport({"/v5/position/closed-pnl": {
            "retCode": 404, "retMsg": "Not Found"}})
        with self.assertRaises(BybitAPIError) as ctx:
            self.demo_client(transport).get_closed_pnl("BTCUSDT")
        self.assertEqual(ctx.exception.ret_code, 404)


class SignatureTests(BybitClientTestBase):
    def test_fixed_vectors(self):
        # 독립 계산값(hmac)으로 고정한 벡터 — 서명 조립 순서가 바뀌면 여기서 잡힌다
        self.assertEqual(
            v5_signature("test-secret", "1700000000000", "test-key", "5000",
                         "category=linear&symbol=BTCUSDT"),
            "9a7c8cfd6ba1a7c498aa4dd5a7f9cfbba01fcb6eebae734ffe0d775870a1a3fb")
        self.assertEqual(
            v5_signature("test-secret", "1700000000000", "test-key", "5000",
                         '{"category":"linear","symbol":"BTCUSDT","side":"Buy","orderType":"Market","qty":"0.001"}'),
            "539846e49fd34035fd6d8e1cb491ee4d855c3707544e0d839d0fad8186f8574d")

    def test_get_request_headers_are_signed_over_query(self):
        transport = FakeTransport({"/v5/account/wallet-balance": {
            "retCode": 0, "result": {"list": [{"coin": [{"coin": "USDT", "equity": "50"}]}]}}})
        client = self.live_client(transport)
        client.get_balance()
        call = transport.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["params"], {"accountType": "UNIFIED", "coin": "USDT"})
        headers = call["headers"]
        self.assertEqual(headers["X-BAPI-API-KEY"], "test-key")
        self.assertEqual(headers["X-BAPI-RECV-WINDOW"], "5000")
        self.assertEqual(
            headers["X-BAPI-SIGN"],
            v5_signature("test-secret", headers["X-BAPI-TIMESTAMP"], "test-key", "5000",
                         "accountType=UNIFIED&coin=USDT"))

    def test_post_request_headers_are_signed_over_body(self):
        transport = FakeTransport({
            "/v5/market/instruments-info": {"retCode": 0, "result": {"list": [{
                "symbol": "BTCUSDT",
                "lotSizeFilter": {"minOrderQty": "0.001", "qtyStep": "0.001", "maxOrderQty": "100"},
                "priceFilter": {"tickSize": "0.10"}}]}},
            "/v5/position/set-leverage": {"retCode": 0, "result": {}},
            "/v5/order/create": {"retCode": 0, "result": {"orderId": "abc-123"}},
        })
        client = self.live_client(transport)
        client.place_market_order("BTCUSDT", "Buy", stop_loss=59000, take_profit=62000,
                                  entry_price=60000, balance=50.0)
        create = [c for c in transport.calls if c["path"] == "/v5/order/create"][0]
        self.assertEqual(create["method"], "POST")
        body_text = json.dumps(create["body"], separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(
            create["headers"]["X-BAPI-SIGN"],
            v5_signature("test-secret", create["headers"]["X-BAPI-TIMESTAMP"], "test-key", "5000",
                         body_text))
        self.assertEqual(create["headers"]["Content-Type"], "application/json")


class RiskBaseTests(BybitClientTestBase):
    def test_mock_balance_capped_at_50(self):
        self.assertEqual(self.mock_client(mock_balance=200).get_balance()["riskBaseUsd"], MAX_SEED_USD)
        self.assertEqual(self.mock_client(mock_balance=20).get_balance()["riskBaseUsd"], 20.0)

    def test_live_balance_capped_at_50(self):
        for equity, expected in (("200", 50.0), ("20.5", 20.5)):
            with self.subTest(equity=equity):
                transport = FakeTransport({"/v5/account/wallet-balance": {
                    "retCode": 0, "result": {"list": [{"coin": [{"coin": "USDT", "equity": equity}]}]}}})
                self.assertEqual(self.live_client(transport).get_balance()["riskBaseUsd"], expected)

    def test_zero_balance_is_rejected(self):
        client = self.mock_client(mock_balance=0)
        with self.assertRaises(OrderRejected):
            client.plan_entry("BTCUSDT", "Buy", 60000, 59000, 62000)


class SizingTests(BybitClientTestBase):
    def test_quantity_floor_per_symbol(self):
        cases = [
            ("BTCUSDT", 60000, 59000, "0.001", "0.001", None),
            ("BTCUSDT", 60000, 59600, "0.003", "0.001", 5),
            ("ETHUSDT", 2000, 1900, "0.01", "0.01", None),
            ("SOLUSDT", 150, 140, "0.1", "0.1", None),
        ]
        for symbol, entry, stop, qty, step, leverage in cases:
            with self.subTest(symbol=symbol, stop=stop):
                plan = self.mock_client().plan_entry(symbol, "Buy", entry, stop, entry + 1000,
                                                     leverage=leverage)
                self.assertEqual(fmt_dec(plan.qty), qty)
                self.assertEqual(plan.qty % Decimal(step), 0)
                self.assertLessEqual(plan.expected_loss_usd, plan.loss_budget_usd + 1e-9)
                self.assertEqual(plan.loss_budget_usd, MAX_SEED_USD * 0.03)

    def test_floor_helper_never_rounds_up(self):
        self.assertEqual(floor_to_step(0.0099, 0.001), Decimal("0.009"))
        self.assertEqual(fmt_dec(floor_to_step("1.999", "0.1")), "1.9")

    def test_notional_cap_limits_quantity(self):
        # SOL 150달러, 손절 1달러 거리 → 예산 1.5달러면 1.5개지만 2배 상한(100달러)이 먼저 걸린다
        plan = self.mock_client().plan_entry("SOLUSDT", "Buy", 150, 149, 160, leverage=2)
        self.assertEqual(fmt_dec(plan.qty), "0.6")
        self.assertLessEqual(plan.notional_usd, 50 * 2 + 1e-9)
        self.assertTrue(any("명목가" in note for note in plan.notes))

    def test_min_quantity_loss_over_budget_is_rejected(self):
        # BTC 최소 0.001 × 2000달러 거리 = 2달러 > 예산 1.5달러(50달러의 3%)
        with self.assertRaises(OrderRejected) as ctx:
            self.mock_client().plan_entry("BTCUSDT", "Buy", 60000, 58000, 62000)
        message = str(ctx.exception)
        self.assertIn("최소 주문 수량", message)
        self.assertIn("2.00", message)
        self.assertIn("1.50", message)

    def test_small_balance_uses_balance_not_50(self):
        client = self.mock_client(mock_balance=20)
        plan = client.plan_entry("SOLUSDT", "Buy", 150, 146, 160)
        self.assertEqual(plan.risk_base_usd, 20.0)
        self.assertAlmostEqual(plan.loss_budget_usd, 0.6)
        self.assertEqual(fmt_dec(plan.qty), "0.1")
        with self.assertRaises(OrderRejected) as ctx:
            client.plan_entry("BTCUSDT", "Buy", 60000, 59000, 62000)   # 최소 수량 손실 1달러 > 0.6달러
        self.assertIn("0.60", str(ctx.exception))

    def test_explicit_quantity_still_checked(self):
        client = self.mock_client()
        with self.assertRaises(OrderRejected):
            client.plan_entry("BTCUSDT", "Buy", 60000, 59000, 62000, qty=0.01)   # 손실 10달러
        with self.assertRaises(OrderRejected):
            client.plan_entry("BTCUSDT", "Buy", 60000, 59000, 62000, qty=0.0005)  # 최소 수량 미만
        plan = client.plan_entry("BTCUSDT", "Buy", 60000, 59000, 62000, qty="0.0017")
        self.assertEqual(fmt_dec(plan.qty), "0.001")

    def test_expected_loss_and_budget_reported(self):
        plan = self.mock_client().plan_entry("BTCUSDT", "Buy", 60000, 59000, 62000).to_dict()
        self.assertEqual(plan["riskBaseUsd"], 50.0)
        self.assertEqual(plan["lossBudgetUsd"], 1.5)
        self.assertEqual(plan["expectedLossUsd"], 1.0)
        self.assertEqual(plan["notionalUsd"], 60.0)
        self.assertEqual(plan["leverage"], DEFAULT_LEVERAGE)
        self.assertEqual(plan["qty"], "0.001")


class RuleValidationTests(BybitClientTestBase):
    def test_loss_pct_range(self):
        client = self.mock_client()
        for pct in (1.9, 4.01, 0, -3):
            with self.subTest(pct=pct), self.assertRaises(ValueError):
                client.plan_entry("BTCUSDT", "Buy", 60000, 59000, 62000, loss_pct=pct)
        for pct in (2.0, 3.0, 4.0):
            with self.subTest(pct=pct):
                self.assertEqual(client.plan_entry("BTCUSDT", "Buy", 60000, 59000, 62000,
                                                   loss_pct=pct).loss_pct, pct)

    def test_leverage_cap(self):
        client = self.mock_client()
        for leverage in (5.5, 10, 0, 0.5):
            with self.subTest(leverage=leverage), self.assertRaises(ValueError):
                client.set_leverage("BTCUSDT", leverage)
            with self.subTest(leverage=leverage), self.assertRaises(ValueError):
                client.plan_entry("BTCUSDT", "Buy", 60000, 59000, 62000, leverage=leverage)
        self.assertEqual(client.set_leverage("BTCUSDT", 5)["leverage"], "5")

    def test_leverage_is_applied_before_order(self):
        transport = FakeTransport({
            "/v5/market/instruments-info": {"retCode": 0, "result": {"list": [{
                "symbol": "BTCUSDT",
                "lotSizeFilter": {"minOrderQty": "0.001", "qtyStep": "0.001"},
                "priceFilter": {"tickSize": "0.10"}}]}},
            "/v5/position/set-leverage": {"retCode": 0, "result": {}},
            "/v5/order/create": {"retCode": 0, "result": {"orderId": "abc-123"}},
        })
        client = self.live_client(transport)
        client.place_market_order("BTCUSDT", "Buy", stop_loss=59000, take_profit=62000,
                                  entry_price=60000, balance=50.0, leverage=4)
        paths = [c["path"] for c in transport.calls]
        self.assertEqual(paths[0], "/v5/market/instruments-info")
        self.assertEqual(paths[1], "/v5/position/set-leverage")
        self.assertEqual(paths[2], "/v5/order/create")
        self.assertEqual(transport.calls[1]["body"]["buyLeverage"], "4")
        self.assertEqual(transport.calls[1]["body"]["sellLeverage"], "4")

    def test_tp_sl_direction_validation(self):
        client = self.mock_client()
        cases = [
            ("Buy", 60000, 61000, 62000),    # 롱 손절이 위
            ("Buy", 60000, 59000, 58000),    # 롱 목표가 아래
            ("Sell", 60000, 59000, 58000),   # 숏 손절이 아래
            ("Sell", 60000, 61000, 62000),   # 숏 목표가 위
            ("Buy", 60000, 60000, 62000),    # 손절 = 진입
        ]
        for side, entry, stop, tp in cases:
            with self.subTest(side=side, stop=stop, tp=tp), self.assertRaises(ValueError):
                client.plan_entry("BTCUSDT", side, entry, stop, tp)
        with self.assertRaises(ValueError):
            client.plan_entry("BTCUSDT", "hold", 60000, 59000, 62000)


class OrderPayloadTests(BybitClientTestBase):
    def test_mock_order_carries_full_tp_sl(self):
        client = self.mock_client()
        order = client.place_market_order("BTCUSDT", "Buy", stop_loss=59000, take_profit=62000,
                                          entry_price=60000)
        payload = order["payload"]
        self.assertEqual(payload["category"], "linear")
        self.assertEqual(payload["symbol"], "BTCUSDT")
        self.assertEqual(payload["side"], "Buy")
        self.assertEqual(payload["orderType"], "Market")
        self.assertEqual(payload["qty"], "0.001")
        self.assertEqual(payload["positionIdx"], 0)
        self.assertEqual(payload["tpslMode"], "Full")
        self.assertEqual(payload["takeProfit"], "62000")
        self.assertEqual(payload["stopLoss"], "59000")
        self.assertEqual(payload["tpTriggerBy"], "LastPrice")
        self.assertEqual(payload["slTriggerBy"], "LastPrice")

    def test_second_position_on_same_symbol_is_rejected(self):
        client = self.mock_client()
        client.place_market_order("BTCUSDT", "Buy", stop_loss=59000, take_profit=62000, entry_price=60000)
        with self.assertRaises(OrderRejected):
            client.place_market_order("BTCUSDT", "Buy", stop_loss=58000, take_profit=63000, entry_price=60000)

    def test_short_side_uses_sell(self):
        client = self.mock_client()
        order = client.place_market_order("ETHUSDT", "Sell", stop_loss=2100, take_profit=1800,
                                          entry_price=2000)
        self.assertEqual(order["payload"]["side"], "Sell")
        self.assertEqual(order["plan"]["side"], "Sell")

    def test_api_error_is_raised_not_swallowed(self):
        transport = FakeTransport({
            "/v5/market/instruments-info": {"retCode": 0, "result": {"list": [{
                "symbol": "BTCUSDT",
                "lotSizeFilter": {"minOrderQty": "0.001", "qtyStep": "0.001"},
                "priceFilter": {"tickSize": "0.10"}}]}},
            "/v5/position/set-leverage": {"retCode": 0, "result": {}},
            "/v5/order/create": {"retCode": 10001, "retMsg": "params error"},
        })
        client = self.live_client(transport)
        with self.assertRaises(BybitAPIError) as ctx:
            client.place_market_order("BTCUSDT", "Buy", stop_loss=59000, take_profit=62000,
                                      entry_price=60000, balance=50.0)
        self.assertEqual(ctx.exception.ret_code, 10001)
        self.assertEqual(len([c for c in transport.calls if c["path"] == "/v5/order/create"]), 1)

    def test_close_position_payload_is_reduce_only(self):
        transport = FakeTransport({
            "/v5/position/list": {"retCode": 0, "result": {"list": [{
                "symbol": "BTCUSDT", "side": "Buy", "size": "0.002", "avgPrice": "60000",
                "stopLoss": "59000", "takeProfit": "62000", "leverage": "3"}]}},
            "/v5/market/instruments-info": {"retCode": 0, "result": {"list": [{
                "symbol": "BTCUSDT",
                "lotSizeFilter": {"minOrderQty": "0.001", "qtyStep": "0.001"},
                "priceFilter": {"tickSize": "0.10"}}]}},
            "/v5/order/create": {"retCode": 0, "result": {"orderId": "close-1"}},
        })
        client = self.live_client(transport)
        result = client.close_position("BTCUSDT")
        payload = result["payload"]
        self.assertEqual(payload["side"], "Sell")
        self.assertEqual(payload["qty"], "0.002")
        self.assertTrue(payload["reduceOnly"])
        self.assertEqual(payload["orderType"], "Market")
        self.assertEqual(result["orderId"], "close-1")

    def test_close_without_position_is_rejected(self):
        with self.assertRaises(OrderRejected):
            self.mock_client().close_position("ETHUSDT")

    def test_stop_loss_that_rounds_onto_entry_is_rejected(self):
        client = self.mock_client()
        with self.assertRaises(OrderRejected) as ctx:
            client.place_market_order("BTCUSDT", "Buy", stop_loss=59999.95, take_profit=62000,
                                      entry_price=60000)
        self.assertIn("반올림", str(ctx.exception))

    def test_sync_time_sets_offset(self):
        transport = FakeTransport({"/v5/market/time": {
            "retCode": 0, "result": {"timeSecond": "1800000000", "timeNano": "0"},
            "time": 1800000000000}})
        client = self.live_client(transport)
        self.assertIsInstance(client.sync_time(), int)
        self.assertNotEqual(client.sync_time(), 0)

    def test_closed_pnl_is_read_only(self):
        transport = FakeTransport({"/v5/position/closed-pnl": {"retCode": 0, "result": {"list": [{
            "symbol": "BTCUSDT", "side": "Sell", "qty": "0.001", "avgEntryPrice": "60000",
            "avgExitPrice": "60900", "closedPnl": "-0.95", "updatedTime": "1800000000000"}]}}})
        client = self.live_client(transport)
        rows = client.get_closed_pnl("BTCUSDT", limit=3)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["side"], "Sell")
        self.assertAlmostEqual(rows[0]["closedPnl"], -0.95)
        call = transport.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["params"], {"category": "linear", "symbol": "BTCUSDT", "limit": "3"})
        with self.assertRaises(BybitError):
            self.mock_client().get_closed_pnl("BTCUSDT")

    def test_instrument_falls_back_when_api_fails(self):
        transport = FakeTransport({
            "/v5/market/instruments-info": {"retCode": 10006, "retMsg": "Too many visits"},
            "/v5/account/wallet-balance": {"retCode": 0, "result": {"list": [
                {"coin": [{"coin": "USDT", "equity": "50"}]}]}},
        })
        client = self.live_client(transport)
        spec = client.get_instrument("BTCUSDT")
        self.assertEqual(fmt_dec(spec.min_order_qty), "0.001")
        self.assertTrue(any("폴백" in note for note in client.notes))
        with self.assertRaises(OrderRejected):
            client.get_instrument("XRPUSDT")


if __name__ == "__main__":
    unittest.main()
