"""바이비트 V5 **테스트넷·데모 전용** 소액 주문 클라이언트 — 50달러 시드로만 움직이는 안전장치를 코드에 박아 둔 것.

왜 테스트넷·데모만인가
- 이 모듈은 실전 자동매매의 '주문을 내는 손'이다. 손이 실수하면 잃는 것은 사용자의 돈이다.
- 그래서 주소를 안전한 두 곳으로만 고정한다.
    · 테스트넷        https://api-testnet.bybit.com   (BYBIT_TESTNET=true, 기본값)
    · 데모 트레이딩   https://api-demo.bybit.com      (BYBIT_DEMO=true 또는 BYBIT_ENV=demo)
  둘 다 실돈이 아닌 모의 USDT만 움직인다. 다른 주소(특히 api.bybit.com)를 넘기면 만들 때부터
  BybitConfigError로 거부하고, BYBIT_TESTNET=false도 같은 이유로 거부한다.
  실전으로 넘어갈 때는 이 파일을 고치는 것이 아니라, 사람이 별도로 결정하고 승인해야 한다.
- BYBIT_DEMO=true가 BYBIT_TESTNET=true와 함께 켜져 있으면 **데모가 이긴다**(둘 다 샌드박스이고,
  데모는 '명시적으로 데모를 고른' 의도이므로). 둘 다 꺼져 있으면 남는 것은 메인넷뿐이므로 거부한다.
  우선순위: env 인자 > BYBIT_DEMO > BYBIT_ENV > BYBIT_TESTNET > 기본(testnet).
- 바이비트 Demo Trading은 api-testnet과 base URL만 다르고 V5 REST 경로·HMAC-SHA256 서명이 같다.
  다만 데모 계정은 /v5/position/closed-pnl 같은 일부 부가 조회가 제한될 수 있어, 그 조회는
  '손익 미확인'으로 남기고 주문·포지션 관리(핵심 호출)는 계속 돌도록 호출하는 쪽에서 감싼다.

왜 mock 모드가 기본인가
- BYBIT_API_KEY / BYBIT_API_SECRET이 없으면 네트워크 요청을 한 번도 보내지 않는 mock 모드로 돈다.
  키를 넣기 전에 화면·흐름·수량 계산을 그대로 시험해 볼 수 있고, 키 없이 실수로 주문이 나갈 일도 없다.
- mock의 잔고(기본 50 USDT)와 포지션은 메모리에만 있다. 프로세스가 끝나면 사라진다.

50달러 시드 안전 설계(요약)
1) 리스크 기준 자본 = min(실제 잔고, 50). 잔고가 50보다 크면 50까지만, 작으면 잔고 전부.
2) 1회 손실 예산 = 기준 자본 × 손실률(기본 3%, 2~4%만 허용 — 범위 밖은 ValueError).
3) 레버리지 상한 5배(1 미만·5 초과는 ValueError). 기본 3배는 '명목가 상한' 계산에만 쓰고,
   주문 전에 set_leverage로 거래소 레버리지도 같은 값으로 맞춘다(계산과 실제가 어긋나지 않게).
4) 수량은 종목의 qtyStep에 맞춰 내림(floor). 최소 주문 수량으로 진입했을 때 예상 손실이 예산을
   넘으면 주문을 거부한다. 예산에 맞는 수량이 최소 수량보다 작으면 최소 수량으로 올리되,
   그 손실이 예산 안이고 명목가가 기준자본×레버리지 안일 때만 허용한다.
5) 진입가·손절가·목표가의 방향이 틀리면(롱인데 손절이 진입가 위 등) 주문 대신 ValueError.

V5 REST 서명
- GET:  HMAC_SHA256(secret, timestamp + api_key + recv_window + query_string)
- POST: HMAC_SHA256(secret, timestamp + api_key + recv_window + json_body)
- 헤더 X-BAPI-API-KEY / X-BAPI-TIMESTAMP / X-BAPI-RECV-WINDOW / X-BAPI-SIGN
  서명에 넣은 query_string·json_body와 실제로 보낸 본문이 같아야 하므로 한 곳(_request)에서 만든다.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP
from typing import Callable, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .config import load_env_file

# ------------------------------------------------------------------ 상수

TESTNET_BASE_URL = "https://api-testnet.bybit.com"
TESTNET_HOST = "api-testnet.bybit.com"
DEMO_BASE_URL = "https://api-demo.bybit.com"       # 바이비트 Demo Trading
DEMO_HOST = "api-demo.bybit.com"

# 허용하는 두 환경 — 이 밖(메인넷 api.bybit.com)은 어떤 경로로도 열지 않는다
SANDBOX_ENVS = ("testnet", "demo")
ENV_BASE_URLS = {"testnet": TESTNET_BASE_URL, "demo": DEMO_BASE_URL}
ENV_HOSTS = {"testnet": TESTNET_HOST, "demo": DEMO_HOST}
ENV_LABELS = {"testnet": "테스트넷", "demo": "데모 트레이딩"}

_TRUTHY = ("true", "1", "yes", "on", "")
_FALSY = ("false", "0", "no", "off")

MAX_SEED_USD = 50.0          # 리스크 계산에 쓰는 최대 시드
DEFAULT_LOSS_PCT = 3.0       # 1회 손실 예산 기본값
MIN_LOSS_PCT = 2.0           # 허용 하한
MAX_LOSS_PCT = 4.0           # 허용 상한
DEFAULT_LEVERAGE = 3.0       # 명목가 상한 계산 기본값(50달러로 BTC 최소 수량을 담을 수 있는 선)
MAX_LEVERAGE = 5.0           # 레버리지 상한
RECV_WINDOW = "5000"         # Bybit에 허용하는 수신 창(ms)

_LOSS_EPS = 1e-9             # 부동소수 비교 여유


class BybitError(RuntimeError):
    """이 모듈이 내는 모든 오류의 뿌리."""


class BybitConfigError(BybitError):
    """설정이 안전 규칙을 어겼다 — 주소·테스트넷 플래그·키 형식."""


class BybitAPIError(BybitError):
    """거래소가 오류를 돌려줬다(retCode ≠ 0) 또는 연결이 실패했다."""

    def __init__(self, ret_code, ret_msg: str, status: Optional[int] = None, path: str = ""):
        self.ret_code = ret_code
        self.ret_msg = ret_msg
        self.status = status
        self.path = path
        where = f" ({path})" if path else ""
        super().__init__(f"Bybit 오류 {ret_code}: {ret_msg}{where}")


class OrderRejected(BybitError):
    """리스크 규칙에 걸려 주문을 내지 않았다. 사유에 숫자를 남긴다(예산·수량·손실)."""


# ------------------------------------------------------------------ 수량·호가 계산

def _dec(value) -> Decimal:
    """float의 이진 오차를 물고 오지 않도록 항상 문자열로 바꿔 Decimal로 만든다."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"숫자로 읽을 수 없습니다: {value!r}") from exc


def floor_to_step(value, step) -> Decimal:
    """수량을 qtyStep 단위로 내림 — 거래소는 스텝에 맞지 않는 수량을 거부하므로 반드시 내림한다."""
    unit = _dec(step)
    if unit <= 0:
        raise ValueError(f"qtyStep은 0보다 커야 합니다: {step!r}")
    return (_dec(value) / unit).to_integral_value(rounding=ROUND_FLOOR) * unit


def round_to_step(value, step) -> Decimal:
    """가격을 tickSize 단위로 반올림 — TP/SL 가격이 호가 단위에 맞아야 주문이 접수된다."""
    unit = _dec(step)
    if unit <= 0:
        raise ValueError(f"tickSize는 0보다 커야 합니다: {step!r}")
    return (_dec(value) / unit).to_integral_value(rounding=ROUND_HALF_UP) * unit


def fmt_dec(value) -> str:
    """Decimal을 '0.001'처럼 지수·끝자리 0 없는 문자열로. Bybit는 문자열 수량/가격을 받는다."""
    return format(_dec(value).normalize(), "f")


# ------------------------------------------------------------------ 종목 규격

@dataclass(frozen=True)
class Instrument:
    symbol: str
    min_order_qty: Decimal
    qty_step: Decimal
    tick_size: Decimal
    max_order_qty: Optional[Decimal] = None
    min_notional: Optional[Decimal] = None

    @classmethod
    def from_api(cls, item: Dict) -> "Instrument":
        lot = item.get("lotSizeFilter") or {}
        price = item.get("priceFilter") or {}
        return cls(
            symbol=item["symbol"],
            min_order_qty=_dec(lot.get("minOrderQty") or 0),
            qty_step=_dec(lot.get("qtyStep") or 0.001),
            tick_size=_dec(price.get("tickSize") or 0.01),
            max_order_qty=_dec(lot["maxOrderQty"]) if lot.get("maxOrderQty") else None,
            min_notional=_dec(lot["minNotionalValue"]) if lot.get("minNotionalValue") else None,
        )

    def to_dict(self) -> Dict:
        return {
            "symbol": self.symbol,
            "minOrderQty": fmt_dec(self.min_order_qty),
            "qtyStep": fmt_dec(self.qty_step),
            "tickSize": fmt_dec(self.tick_size),
            "maxOrderQty": None if self.max_order_qty is None else fmt_dec(self.max_order_qty),
            "minNotionalValue": None if self.min_notional is None else fmt_dec(self.min_notional),
        }


# 테스트넷 instruments-info를 읽지 못했을 때 쓰는 폴백(BTCUSDT는 공식 문서 예시값, 2026-11 확인).
FALLBACK_INSTRUMENTS: Dict[str, Instrument] = {
    "BTCUSDT": Instrument("BTCUSDT", Decimal("0.001"), Decimal("0.001"), Decimal("0.10"),
                          Decimal("100"), Decimal("5")),
    "ETHUSDT": Instrument("ETHUSDT", Decimal("0.01"), Decimal("0.01"), Decimal("0.01"),
                          Decimal("2000"), Decimal("5")),
    "SOLUSDT": Instrument("SOLUSDT", Decimal("0.1"), Decimal("0.1"), Decimal("0.01"),
                          Decimal("10000"), Decimal("5")),
}

# mock 모드의 마지막 체결가 흉내(실제 시세가 아니다 — mock에서만 쓴다)
MOCK_PRICES = {"BTCUSDT": 60000.0, "ETHUSDT": 2000.0, "SOLUSDT": 150.0}


# ------------------------------------------------------------------ 서명

def v5_signature(api_secret: str, timestamp: str, api_key: str, recv_window: str, payload: str) -> str:
    """Bybit V5 HMAC-SHA256 서명. payload는 GET이면 query string, POST면 JSON 본문 문자열."""
    message = f"{timestamp}{api_key}{recv_window}{payload}"
    return hmac.new(api_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def build_headers(api_key: str, api_secret: str, timestamp: str, recv_window: str,
                  payload: str, has_body: bool = False) -> Dict[str, str]:
    headers = {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN": v5_signature(api_secret, timestamp, api_key, recv_window, payload),
        "X-BAPI-SIGN-TYPE": "2",
        "Accept": "application/json",
    }
    if has_body:
        headers["Content-Type"] = "application/json"
    return headers


# ------------------------------------------------------------------ 리스크 계산 결과

@dataclass
class RiskPlan:
    symbol: str
    side: str                 # "Buy" | "Sell"
    entry_price: float
    stop_price: float
    take_profit: float
    qty: Decimal
    qty_step: Decimal
    tick_size: Decimal
    risk_base_usd: float      # min(잔고, 50)
    loss_pct: float
    loss_budget_usd: float
    expected_loss_usd: float
    notional_usd: float
    leverage: float
    notes: List[str]

    def to_dict(self) -> Dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "entryPrice": self.entry_price,
            "stopPrice": self.stop_price,
            "takeProfit": self.take_profit,
            "qty": fmt_dec(self.qty),
            "riskBaseUsd": round(self.risk_base_usd, 2),
            "lossPct": self.loss_pct,
            "lossBudgetUsd": round(self.loss_budget_usd, 2),
            "expectedLossUsd": round(self.expected_loss_usd, 2),
            "notionalUsd": round(self.notional_usd, 2),
            "leverage": self.leverage,
            "notes": list(self.notes),
        }


# ------------------------------------------------------------------ 클라이언트

Transport = Callable[[str, str, Dict, Optional[Dict], Dict], Dict]


def _truthy_env(raw: str) -> bool:
    return raw.strip().lower() in ("true", "1", "yes", "on")


class BybitClient:
    """바이비트 V5 클라이언트(테스트넷·데모 전용). 키가 없으면 mock, 있으면 서명 요청."""

    def __init__(self, api_key: Optional[str] = None, api_secret: Optional[str] = None,
                 testnet: Optional[bool] = None, base_url: Optional[str] = None,
                 timeout: float = 10.0, transport: Optional[Transport] = None,
                 mock_balance: float = 50.0, env: Optional[str] = None):
        load_env_file()
        env_name, explicit = self._resolve_env(env, testnet)
        self.env, self.base_url = self._resolve_target(env_name, explicit, base_url)
        self.testnet = self.env == "testnet"      # 하위 호환: '테스트넷인가'(데모면 False)

        key = (api_key if api_key is not None else os.getenv("BYBIT_API_KEY", "")).strip()
        secret = (api_secret if api_secret is not None else os.getenv("BYBIT_API_SECRET", "")).strip()
        if bool(key) != bool(secret):
            raise BybitConfigError("BYBIT_API_KEY와 BYBIT_API_SECRET 중 하나만 있습니다 — 둘 다 넣거나 둘 다 비우세요")
        self.api_key = key
        self.api_secret = secret
        self.is_mock = not (key and secret)

        self.timeout = float(timeout)
        self.transport: Transport = transport or self._http_request
        self.recv_window = RECV_WINDOW
        self._time_offset_ms = 0
        self._instruments: Dict[str, Instrument] = {}
        self.notes: List[str] = []

        # mock 장부(메모리에만 존재)
        self._mock_balance = float(mock_balance)
        self._mock_positions: Dict[str, Dict] = {}
        self._mock_orders: List[Dict] = []
        self._mock_leverage: Dict[str, float] = {}
        self._mock_prices = dict(MOCK_PRICES)
        self._mock_seq = 0

        if self.is_mock:
            self.notes.append("mock 모드 — 키가 없어 실제 네트워크 요청을 보내지 않습니다")
        else:
            self.notes.append(f"{ENV_LABELS[self.env]} 실주문 — {self.base_url} · 모의 USDT만 움직입니다")

    # -------------------------------------------------------------- 설정 검증

    @property
    def sandbox(self) -> bool:
        """메인넷이 아닌 안전한 환경(테스트넷 또는 데모)인가 — 항상 True여야 한다."""
        return self.env in SANDBOX_ENVS

    @classmethod
    def _resolve_env(cls, explicit_env: Optional[str],
                     explicit_testnet: Optional[bool]) -> "tuple[str, bool]":
        """env 이름과 '인자로 직접 골랐는가'를 정한다(인자로 골랐으면 base_url도 그 환경이어야 한다)."""
        raw_env = (explicit_env or "").strip().lower()
        if raw_env:
            if raw_env not in SANDBOX_ENVS:
                raise BybitConfigError(
                    f"env는 testnet 또는 demo만 허용합니다: {explicit_env!r}. "
                    "실전(메인넷) 거래는 이 모듈에서 거부합니다")
            return raw_env, True
        if explicit_testnet is not None:
            if explicit_testnet:
                return "testnet", True
            raise BybitConfigError(
                "testnet=False — 실전(메인넷) 거래는 이 모듈에서 거부합니다. "
                "테스트넷 검증이 끝나고 사람이 따로 승인할 때까지 테스트넷만 씁니다. "
                "바이비트 데모 트레이딩을 쓰려면 env='demo' 또는 BYBIT_DEMO=true")

        demo_raw = os.getenv("BYBIT_DEMO")
        if demo_raw is not None and demo_raw.strip() != "":
            value = demo_raw.strip().lower()
            if value in _TRUTHY:
                return "demo", False          # 데모가 테스트넷보다 우선
            if value not in _FALSY:
                raise BybitConfigError(
                    f"BYBIT_DEMO 값을 읽을 수 없습니다: {demo_raw!r} (true 또는 false)")

        env_raw = os.getenv("BYBIT_ENV")
        if env_raw is not None and env_raw.strip() != "":
            value = env_raw.strip().lower()
            if value in SANDBOX_ENVS:
                return value, False
            raise BybitConfigError(
                f"BYBIT_ENV 값을 읽을 수 없습니다: {env_raw!r} (testnet 또는 demo; "
                "실전 mainnet은 이 모듈에서 거부합니다)")

        raw = os.getenv("BYBIT_TESTNET", "true")
        value = (raw or "").strip().lower()
        if value in _TRUTHY:
            return "testnet", False
        if value in _FALSY:
            raise BybitConfigError(
                "BYBIT_TESTNET=false — 실전(메인넷) 거래는 이 모듈에서 거부합니다. "
                "테스트넷 검증이 끝나고 사람이 따로 승인할 때까지 BYBIT_TESTNET=true로 두세요. "
                "(바이비트 데모 트레이딩을 쓰려면 BYBIT_DEMO=true 또는 BYBIT_ENV=demo)")
        raise BybitConfigError(f"BYBIT_TESTNET 값을 읽을 수 없습니다: {raw!r} (true 또는 false)")

    @classmethod
    def _resolve_target(cls, env_name: str, explicit: bool,
                        base_url: Optional[str]) -> "tuple[str, str]":
        """환경 이름과 실제 주소를 맞춘다. 샌드박스 두 주소만 허용한다."""
        if not base_url:
            return env_name, ENV_BASE_URLS[env_name]
        url = base_url.strip().rstrip("/")
        if not url.startswith("https://"):
            raise BybitConfigError("Bybit API는 https로만 부릅니다")
        host = (urlparse(url).hostname or "").lower()
        host_env = next((name for name, h in ENV_HOSTS.items() if h == host), None)
        if host_env is None:
            raise BybitConfigError(
                f"테스트넷·데모 주소만 허용합니다 — {TESTNET_HOST} / {DEMO_HOST} 대신 "
                f"{host or url!r}이 들어왔습니다. 실전 도메인(api.bybit.com)은 이 모듈에서 거부합니다")
        if explicit and host_env != env_name:
            raise BybitConfigError(
                f"환경({env_name})과 주소({host})가 서로 다릅니다 — 한쪽만 고쳐서 섞이지 않게 합니다. "
                f"{ENV_BASE_URLS[env_name]} 또는 {ENV_LABELS[host_env]} 설정을 함께 맞추세요")
        return host_env, url

    # -------------------------------------------------------------- 전송

    def _http_request(self, method: str, path: str, params: Dict, body: Optional[Dict],
                      headers: Dict) -> Dict:
        url = self.base_url + path
        if params:
            url += "?" + urlencode(params)
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}
            raise BybitAPIError(payload.get("retCode", exc.code), payload.get("retMsg", raw[:200]),
                                status=exc.code, path=path) from None
        except URLError as exc:
            raise BybitAPIError(-1, f"네트워크 연결 실패: {exc.reason}", path=path) from None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise BybitAPIError(-1, "응답이 JSON이 아닙니다", path=path) from None

    def _request(self, method: str, path: str, params: Optional[Dict] = None,
                 body: Optional[Dict] = None) -> Dict:
        if self.is_mock:
            raise BybitError("mock 모드에서는 네트워크 요청을 보내지 않습니다")
        params = dict(params or {})
        payload = urlencode(params) if (method == "GET" and params) else ""
        if method != "GET" and body is not None:
            payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        timestamp = str(int(time.time() * 1000) + self._time_offset_ms)
        headers = build_headers(self.api_key, self.api_secret, timestamp, self.recv_window,
                                payload, has_body=body is not None)
        data = self.transport(method, path, params, body, headers)
        if not isinstance(data, dict):
            raise BybitAPIError(-1, "응답 형식이 올바르지 않습니다", path=path)
        code = data.get("retCode")
        if code != 0:
            raise BybitAPIError(code, data.get("retMsg", "알 수 없는 오류"), path=path)
        return data

    def sync_time(self) -> int:
        """서버 시각과의 차이를 재서 이후 요청 타임스탬프에 반영한다(Bybit 10002 방지)."""
        if self.is_mock:
            return 0
        local_ms = int(time.time() * 1000)
        data = self._request("GET", "/v5/market/time")
        result = data.get("result") or {}
        server_ms = int(data.get("time") or 0)
        if not server_ms and result.get("timeSecond"):
            server_ms = int(result["timeSecond"]) * 1000
            if result.get("timeNano"):
                server_ms += int(result["timeNano"]) // 1_000_000
        if not server_ms:
            return 0
        self._time_offset_ms = server_ms - local_ms
        return self._time_offset_ms

    # -------------------------------------------------------------- 조회

    def get_balance(self, coin: str = "USDT") -> Dict:
        """잔고. risk_base_usd = min(잔고, 50) — 이 값이 모든 리스크 계산의 기준이다."""
        coin = coin.upper()
        if self.is_mock:
            equity = self._mock_balance
            return {"mock": True, "coin": coin, "equity": equity, "available": equity,
                    "wallet": equity, "riskBaseUsd": round(max(0.0, min(equity, MAX_SEED_USD)), 2),
                    "accountType": "UNIFIED"}
        data = self._request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": coin})
        accounts = (data.get("result") or {}).get("list") or []
        if not accounts:
            raise BybitAPIError(-1, "잔고 응답에 계정이 없습니다", path="/v5/account/wallet-balance")
        account = accounts[0]
        rows = {row.get("coin"): row for row in (account.get("coin") or [])}
        row = rows.get(coin) or {}
        equity = _f(row.get("equity")) or _f(row.get("walletBalance")) or _f(account.get("totalEquity"))
        available = _f(row.get("availableToWithdraw")) or _f(row.get("walletBalance")) or 0.0
        return {"mock": False, "coin": coin, "equity": equity, "available": available,
                "wallet": _f(row.get("walletBalance")) or equity,
                "riskBaseUsd": round(max(0.0, min(equity, MAX_SEED_USD)), 2),
                "accountType": account.get("accountType", "UNIFIED")}

    def get_positions(self, symbol: Optional[str] = None) -> List[Dict]:
        """열린 포지션(수량 0은 뺀다). symbol이 없으면 USDT 선물 전체."""
        symbol = symbol.upper() if symbol else None
        if self.is_mock:
            rows = [dict(p) for p in self._mock_positions.values() if p["size"] > 0]
            return [p for p in rows if symbol is None or p["symbol"] == symbol]
        params = {"category": "linear"}
        if symbol:
            params["symbol"] = symbol
        else:
            params["settleCoin"] = "USDT"
        data = self._request("GET", "/v5/position/list", params)
        rows = []
        for item in (data.get("result") or {}).get("list") or []:
            size = _f(item.get("size"))
            if size <= 0:
                continue
            rows.append({
                "symbol": item.get("symbol"),
                "side": item.get("side"),
                "size": size,
                "entryPrice": _f(item.get("avgPrice")),
                "stopLoss": _f(item.get("stopLoss")),
                "takeProfit": _f(item.get("takeProfit")),
                "leverage": _f(item.get("leverage")),
                "unrealisedPnl": _f(item.get("unrealisedPnl")),
                "updatedTime": item.get("updatedTime"),
            })
        return rows

    def get_last_price(self, symbol: str) -> float:
        symbol = symbol.upper()
        if self.is_mock:
            price = self._mock_prices.get(symbol)
            if not price:
                raise OrderRejected(f"{symbol} mock 시세가 없습니다 — MOCK_PRICES에 먼저 넣으세요")
            return float(price)
        data = self._request("GET", "/v5/market/tickers", {"category": "linear", "symbol": symbol})
        rows = (data.get("result") or {}).get("list") or []
        price = _f(rows[0].get("lastPrice")) if rows else 0.0
        if price <= 0:
            raise BybitAPIError(-1, f"{symbol} 현재가를 읽지 못했습니다", path="/v5/market/tickers")
        return price

    def get_closed_pnl(self, symbol: str, limit: int = 5) -> List[Dict]:
        """최근 청산 손익(조회 전용). 포지션이 사라진 뒤 '얼마로 끝났는지'를 거래소 기록으로 확인한다.

        주문을 내지 않는다. mock 모드에는 거래소 기록이 없으므로 예외를 낸다 — 모의 청산 손익은
        호출하는 쪽(예: testnet_auto)이 확정 봉으로 계산한다.
        데모 트레이딩 계정은 이 엔드포인트가 제한될 수 있다(BybitAPIError). 부가 조회이므로
        호출하는 쪽이 '손익 미확인'으로 감싸고 핵심 루프는 계속 돌아야 한다.
        """
        symbol = symbol.upper()
        if self.is_mock:
            raise BybitError("mock 모드에는 거래소 청산 기록이 없습니다 — 청산 손익은 봉으로 계산합니다")
        data = self._request("GET", "/v5/position/closed-pnl",
                             {"category": "linear", "symbol": symbol, "limit": str(int(limit))})
        rows = []
        for item in (data.get("result") or {}).get("list") or []:
            rows.append({
                "symbol": item.get("symbol"),
                "side": item.get("side"),
                "qty": _f(item.get("qty") or item.get("closedSize")),
                "avgEntryPrice": _f(item.get("avgEntryPrice")),
                "avgExitPrice": _f(item.get("avgExitPrice")),
                "closedPnl": _f(item.get("closedPnl")),
                "orderId": item.get("orderId"),
                "updatedTime": item.get("updatedTime"),
            })
        return rows

    def get_instrument(self, symbol: str) -> Instrument:
        """종목 규격(minOrderQty·qtyStep·tickSize). 실패하면 확인된 폴백 표로 물러난다(사실은 notes에 남긴다)."""
        symbol = symbol.upper()
        if symbol in self._instruments:
            return self._instruments[symbol]
        spec = None
        if not self.is_mock:
            try:
                data = self._request("GET", "/v5/market/instruments-info",
                                     {"category": "linear", "symbol": symbol})
                rows = (data.get("result") or {}).get("list") or []
                if rows:
                    spec = Instrument.from_api(rows[0])
            except (BybitAPIError, KeyError, ValueError):
                spec = None
        if spec is None:
            spec = FALLBACK_INSTRUMENTS.get(symbol)
            if spec is None:
                raise OrderRejected(f"{symbol} 종목 규격을 알 수 없습니다 — 최소 수량·스텝을 확인할 수 없어 주문하지 않습니다")
            if not self.is_mock:
                self.notes.append(f"{symbol} instruments-info를 읽지 못해 폴백 규격을 썼습니다")
        self._instruments[symbol] = spec
        return spec

    # -------------------------------------------------------------- 검증·계획

    @staticmethod
    def normalize_side(side: str) -> str:
        value = str(side or "").strip().lower()
        if value in ("buy", "long", "b"):
            return "Buy"
        if value in ("sell", "short", "s"):
            return "Sell"
        raise ValueError(f"side는 Buy 또는 Sell이어야 합니다: {side!r}")

    @staticmethod
    def validate_loss_pct(loss_pct) -> float:
        pct = float(DEFAULT_LOSS_PCT if loss_pct is None else loss_pct)
        if not (MIN_LOSS_PCT <= pct <= MAX_LOSS_PCT):
            raise ValueError(
                f"1회 손실 예산은 {MIN_LOSS_PCT:g}%~{MAX_LOSS_PCT:g}%만 허용합니다: {pct:g}%")
        return pct

    @staticmethod
    def validate_leverage(leverage) -> float:
        lev = float(DEFAULT_LEVERAGE if leverage is None else leverage)
        if not (1.0 <= lev <= MAX_LEVERAGE):
            raise ValueError(f"레버리지는 1~{MAX_LEVERAGE:g}배만 허용합니다: {lev:g}배")
        return lev

    def risk_base_for(self, balance: Optional[float] = None) -> float:
        """리스크 기준 자본 = min(잔고, 50). 잔고를 안 주면 거래소 잔고를 읽는다."""
        if balance is None:
            balance = self.get_balance()["equity"]
        equity = float(balance)
        if equity <= 0:
            raise OrderRejected("잔고가 0입니다 — 주문할 수 없습니다")
        return min(equity, MAX_SEED_USD)

    def plan_entry(self, symbol: str, side: str, entry_price, stop_price, take_profit,
                   balance: Optional[float] = None, loss_pct=None, leverage=None,
                   qty=None) -> RiskPlan:
        """주문 전 계산만 한다(전송 없음). 규칙을 넘으면 OrderRejected/ValueError로 멈춘다."""
        symbol = symbol.upper()
        side = self.normalize_side(side)
        entry = float(entry_price)
        stop = float(stop_price)
        target = float(take_profit)
        if entry <= 0 or stop <= 0 or target <= 0:
            raise ValueError("진입가·손절가·목표가는 0보다 커야 합니다")
        if stop == entry:
            raise ValueError("손절가가 진입가와 같습니다 — 손실 거리를 정할 수 없습니다")
        if side == "Buy":
            if stop >= entry:
                raise ValueError(f"롱 포지션의 손절가는 진입가 아래여야 합니다 (진입 {entry:g} · 손절 {stop:g})")
            if target <= entry:
                raise ValueError(f"롱 포지션의 목표가는 진입가 위여야 합니다 (진입 {entry:g} · 목표 {target:g})")
        else:
            if stop <= entry:
                raise ValueError(f"숏 포지션의 손절가는 진입가 위여야 합니다 (진입 {entry:g} · 손절 {stop:g})")
            if target >= entry:
                raise ValueError(f"숏 포지션의 목표가는 진입가 아래여야 합니다 (진입 {entry:g} · 목표 {target:g})")

        pct = self.validate_loss_pct(loss_pct)
        lev = self.validate_leverage(leverage)
        spec = self.get_instrument(symbol)

        risk_base = self.risk_base_for(balance)
        budget = risk_base * pct / 100.0
        risk_per_unit = abs(entry - stop)
        notes: List[str] = []
        if risk_base < MAX_SEED_USD:
            notes.append(f"잔고가 ${MAX_SEED_USD:g} 미만이라 잔고 ${risk_base:.2f}를 기준으로 계산했습니다")

        if qty is not None:
            requested = floor_to_step(qty, spec.qty_step)
            if requested <= 0:
                raise OrderRejected(f"요청 수량 {qty}이 qtyStep {fmt_dec(spec.qty_step)} 미만입니다")
            if requested < spec.min_order_qty:
                raise OrderRejected(
                    f"요청 수량 {fmt_dec(requested)}이 최소 주문 수량 {fmt_dec(spec.min_order_qty)}보다 작습니다")
            qty_used = requested
            notes.append("요청 수량을 qtyStep에 맞춰 내림했습니다")
        else:
            qty_ideal = Decimal(str(budget / risk_per_unit))
            notional_cap_qty = Decimal(str(risk_base * lev / entry))
            qty_used = floor_to_step(min(qty_ideal, notional_cap_qty), spec.qty_step)
            if qty_ideal > notional_cap_qty:
                notes.append("손실 예산보다 명목가 상한(기준자본×레버리지)이 먼저 걸렸습니다")
            if qty_used < spec.min_order_qty:
                min_loss = float(spec.min_order_qty) * risk_per_unit
                if min_loss > budget + _LOSS_EPS:
                    raise OrderRejected(
                        f"최소 주문 수량 {fmt_dec(spec.min_order_qty)}로 진입하면 예상 손실 "
                        f"${min_loss:.2f}가 손실 예산 ${budget:.2f}(기준 ${risk_base:.2f}의 {pct:g}%)을 넘습니다 — 주문 거부")
                if float(spec.min_order_qty) * entry > risk_base * lev + _LOSS_EPS:
                    raise OrderRejected(
                        f"최소 수량 {fmt_dec(spec.min_order_qty)}의 명목가 ${float(spec.min_order_qty) * entry:,.2f}가 "
                        f"레버리지 {lev:g}배 상한(${risk_base * lev:,.2f})을 넘습니다 — 주문 거부")
                qty_used = spec.min_order_qty
                notes.append("예산에 맞는 수량이 최소 주문 수량보다 작아 최소 수량으로 올렸습니다")

        expected_loss = float(qty_used) * risk_per_unit
        if expected_loss > budget + _LOSS_EPS:
            raise OrderRejected(
                f"예상 손실 ${expected_loss:.2f}가 손실 예산 ${budget:.2f}를 넘습니다 — 주문 거부")
        notional = float(qty_used) * entry
        if notional > risk_base * lev + _LOSS_EPS:
            raise OrderRejected(
                f"명목가 ${notional:,.2f}가 레버리지 {lev:g}배 상한(${risk_base * lev:,.2f})을 넘습니다 — 주문 거부")
        if spec.max_order_qty is not None and qty_used > spec.max_order_qty:
            raise OrderRejected(
                f"수량 {fmt_dec(qty_used)}이 최대 주문 수량 {fmt_dec(spec.max_order_qty)}을 넘습니다")
        if spec.min_notional is not None and notional < float(spec.min_notional):
            raise OrderRejected(
                f"명목가 ${notional:,.2f}가 최소 주문 금액 ${fmt_dec(spec.min_notional)}보다 작습니다 — 주문 거부")

        return RiskPlan(
            symbol=symbol, side=side, entry_price=entry, stop_price=stop, take_profit=target,
            qty=qty_used, qty_step=spec.qty_step, tick_size=spec.tick_size,
            risk_base_usd=risk_base, loss_pct=pct, loss_budget_usd=budget,
            expected_loss_usd=expected_loss, notional_usd=notional, leverage=lev, notes=notes,
        )

    # -------------------------------------------------------------- 주문

    def set_leverage(self, symbol: str, leverage, margin_mode: Optional[str] = None) -> Dict:
        """레버리지 설정(1~5배). 범위 밖은 ValueError — 거래소에 보내기 전에 막는다."""
        symbol = symbol.upper()
        lev = self.validate_leverage(leverage)
        text = fmt_dec(_dec(lev).normalize())
        if self.is_mock:
            self._mock_leverage[symbol] = lev
            return {"ok": True, "mock": True, "symbol": symbol, "leverage": text}
        body = {"category": "linear", "symbol": symbol, "buyLeverage": text, "sellLeverage": text}
        try:
            data = self._request("POST", "/v5/position/set-leverage", None, body)
        except BybitAPIError as exc:
            # 110043: 이미 같은 레버리지 — 실패가 아니다
            if exc.ret_code == 110043:
                return {"ok": True, "mock": False, "symbol": symbol, "leverage": text,
                        "note": "이미 같은 레버리지였습니다"}
            raise
        return {"ok": True, "mock": False, "symbol": symbol, "leverage": text, "raw": data.get("result")}

    def place_market_order(self, symbol: str, side: str, stop_loss, take_profit,
                           qty=None, entry_price=None, balance: Optional[float] = None,
                           loss_pct=None, leverage=None) -> Dict:
        """시장가 진입 + 전체 포지션 TP/SL 동시 설정.

        qty를 안 주면 50달러 기준 손실 예산으로 수량을 계산한다. entry_price를 안 주면 현재가.
        모든 검증을 통과하지 못하면 거래소에 보내지 않고 OrderRejected/ValueError로 멈춘다.
        """
        symbol = symbol.upper()
        side_n = self.normalize_side(side)
        lev = self.validate_leverage(leverage)
        price = float(entry_price) if entry_price is not None else self.get_last_price(symbol)
        plan = self.plan_entry(symbol, side_n, price, stop_loss, take_profit,
                               balance=balance, loss_pct=loss_pct, leverage=lev, qty=qty)

        stop_text = fmt_dec(round_to_step(plan.stop_price, plan.tick_size))
        tp_text = fmt_dec(round_to_step(plan.take_profit, plan.tick_size))
        # 호가 단위 반올림으로 손절가가 진입가와 같아지면 손실 거리가 0이 된다 — 보내지 않는다
        stop_rounded = float(stop_text)
        if (plan.side == "Buy" and stop_rounded >= plan.entry_price) or \
           (plan.side == "Sell" and stop_rounded <= plan.entry_price):
            raise OrderRejected(
                f"호가 단위 {fmt_dec(plan.tick_size)} 반올림 후 손절가 {stop_text}가 진입가 {plan.entry_price:g}에 너무 가깝습니다 — 주문 거부")

        payload = {
            "category": "linear",
            "symbol": plan.symbol,
            "side": plan.side,
            "orderType": "Market",
            "qty": fmt_dec(plan.qty),
            "positionIdx": 0,
            "takeProfit": tp_text,
            "stopLoss": stop_text,
            "tpslMode": "Full",
            "tpTriggerBy": "LastPrice",
            "slTriggerBy": "LastPrice",
            "tpOrderType": "Market",
            "slOrderType": "Market",
        }

        if self.is_mock:
            self._mock_seq += 1
            order_id = f"mock-{self._mock_seq:04d}"
            result = {"orderId": order_id, "orderLinkId": ""}
            if plan.symbol in self._mock_positions:
                raise OrderRejected(f"{plan.symbol}에 이미 포지션이 있습니다 — 한 종목 한 포지션만 엽니다")
            self._mock_leverage[symbol] = lev
            self._mock_positions[plan.symbol] = {
                "symbol": plan.symbol, "side": plan.side, "size": float(plan.qty),
                "entryPrice": plan.entry_price, "stopLoss": float(stop_text),
                "takeProfit": float(tp_text), "leverage": plan.leverage, "unrealisedPnl": 0.0,
            }
            self._mock_orders.append({"payload": payload, "result": result})
        else:
            # 검증을 모두 통과한 뒤에야 거래소 상태(레버리지)를 바꾸고 주문을 낸다
            self.set_leverage(symbol, lev)
            data = self._request("POST", "/v5/order/create", None, payload)
            result = data.get("result") or {}

        return {"ok": True, "mock": self.is_mock, "orderId": result.get("orderId"),
                "orderLinkId": result.get("orderLinkId", ""), "payload": payload,
                "plan": plan.to_dict(), "notes": self.notes + plan.notes}

    def reduce_only_order(self, symbol: str, side: Optional[str] = None, qty=None) -> Dict:
        """포지션을 **줄이는** 시장가 reduceOnly 주문 — 부분 익절·본절 정리에 쓴다.

        side를 안 주면 포지션의 반대 방향으로 낸다. 수량이 없거나, qtyStep보다 작거나, 최소 주문 수량·
        최소 명목가에 못 미치거나, 보유 수량을 넘으면 거래소에 보내지 않고 OrderRejected로 멈춘다
        (포지션을 넘겨 줄이거나 반대 포지션을 여는 일이 없다).
        """
        symbol = symbol.upper()
        positions = [p for p in self.get_positions(symbol) if p["symbol"] == symbol and p["size"] > 0]
        if not positions:
            raise OrderRejected(f"{symbol}에 줄일 포지션이 없습니다")
        position = max(positions, key=lambda p: p["size"])
        spec = self.get_instrument(symbol)
        held = floor_to_step(position["size"], spec.qty_step)
        if qty is None:
            amount = held
        else:
            amount = floor_to_step(qty, spec.qty_step)
        if amount <= 0:
            raise OrderRejected(f"{symbol} 부분 청산 수량 {qty}이 qtyStep {fmt_dec(spec.qty_step)} 미만입니다")
        if amount < spec.min_order_qty:
            raise OrderRejected(
                f"{symbol} 부분 청산 수량 {fmt_dec(amount)}이 최소 주문 수량 "
                f"{fmt_dec(spec.min_order_qty)}보다 작습니다")
        if amount > held:
            raise OrderRejected(
                f"{symbol} 부분 청산 수량 {fmt_dec(amount)}이 보유 {fmt_dec(held)}를 넘습니다")
        price = self.get_last_price(symbol)
        if spec.min_notional is not None and float(amount) * float(price) < float(spec.min_notional):
            raise OrderRejected(
                f"{symbol} 부분 청산 명목가 ${float(amount) * float(price):,.2f}가 최소 주문 금액 "
                f"${fmt_dec(spec.min_notional)}보다 작습니다")
        side_n = self.normalize_side(side) if side else ("Sell" if position["side"] == "Buy" else "Buy")
        if side_n == position["side"]:
            raise OrderRejected(f"{symbol} reduceOnly 주문 방향({side_n})이 포지션과 같습니다")
        payload = {
            "category": "linear", "symbol": symbol, "side": side_n, "orderType": "Market",
            "qty": fmt_dec(amount), "positionIdx": 0, "reduceOnly": True,
        }
        if self.is_mock:
            self._mock_seq += 1
            order_id = f"mock-{self._mock_seq:04d}"
            row = self._mock_positions.get(symbol)
            if not row:
                raise OrderRejected(f"{symbol}에 줄일 포지션이 없습니다")
            left = float(row["size"]) - float(amount)
            sign = 1.0 if row["side"] == "Buy" else -1.0
            pnl = (float(price) - float(row["entryPrice"])) * float(amount) * sign
            if left <= 1e-12:
                self._mock_positions.pop(symbol, None)
            else:
                row["size"] = left
            result = {"orderId": order_id}
            self._mock_orders.append({"payload": payload, "result": result})
            return {"ok": True, "mock": True, "orderId": order_id, "payload": payload,
                    "closed": {"symbol": symbol, "side": position["side"],
                               "qty": fmt_dec(amount), "entryPrice": position["entryPrice"],
                               "price": float(price), "estimatedPnl": round(pnl, 4),
                               "remaining": round(max(0.0, left), 8)}}
        data = self._request("POST", "/v5/order/create", None, payload)
        result = data.get("result") or {}
        return {"ok": True, "mock": False, "orderId": result.get("orderId"), "payload": payload}

    def set_trading_stop(self, symbol: str, take_profit=None, stop_loss=None) -> Dict:
        """열린 포지션의 TP/SL을 바꾼다(포지션 전체, tpslMode Full). None인 쪽은 건드리지 않는다.

        본절 보호처럼 손절선만 옮길 때 쓴다. Bybit 34040("값이 이미 같음")은 실패가 아니다.
        """
        symbol = symbol.upper()
        spec = self.get_instrument(symbol)
        tp_text = None if take_profit is None else fmt_dec(round_to_step(take_profit, spec.tick_size))
        sl_text = None if stop_loss is None else fmt_dec(round_to_step(stop_loss, spec.tick_size))
        if tp_text is None and sl_text is None:
            return {"ok": True, "mock": self.is_mock, "note": "바꿀 값이 없습니다"}
        if self.is_mock:
            row = self._mock_positions.get(symbol)
            if not row:
                raise OrderRejected(f"{symbol}에 포지션이 없어 TP/SL을 걸 수 없습니다")
            if tp_text is not None:
                row["takeProfit"] = float(tp_text)
            if sl_text is not None:
                row["stopLoss"] = float(sl_text)
            return {"ok": True, "mock": True, "takeProfit": tp_text, "stopLoss": sl_text}
        body = {"category": "linear", "symbol": symbol, "positionIdx": 0, "tpslMode": "Full",
                "tpTriggerBy": "LastPrice", "slTriggerBy": "LastPrice"}
        if tp_text is not None:
            body["takeProfit"] = tp_text
        if sl_text is not None:
            body["stopLoss"] = sl_text
        try:
            self._request("POST", "/v5/position/trading-stop", None, body)
        except BybitAPIError as exc:
            if getattr(exc, "ret_code", None) == 34040:
                return {"ok": True, "mock": False, "takeProfit": tp_text, "stopLoss": sl_text,
                        "note": "이미 같은 값이었습니다"}
            raise
        return {"ok": True, "mock": False, "takeProfit": tp_text, "stopLoss": sl_text}

    def close_position(self, symbol: str) -> Dict:
        """전량 시장가 청산(reduceOnly). 열린 포지션이 없으면 OrderRejected."""
        symbol = symbol.upper()
        positions = [p for p in self.get_positions(symbol) if p["symbol"] == symbol and p["size"] > 0]
        if not positions:
            raise OrderRejected(f"{symbol}에 청산할 포지션이 없습니다")
        position = max(positions, key=lambda p: p["size"])
        spec = self.get_instrument(symbol)
        qty = floor_to_step(position["size"], spec.qty_step)
        if qty <= 0:
            raise OrderRejected(f"{symbol} 포지션 수량 {position['size']}이 qtyStep보다 작습니다")
        side = "Sell" if position["side"] == "Buy" else "Buy"
        payload = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": fmt_dec(qty),
            "positionIdx": 0,
            "reduceOnly": True,
        }
        if self.is_mock:
            self._mock_seq += 1
            order_id = f"mock-{self._mock_seq:04d}"
            price = self.get_last_price(symbol)
            factor = 1 if position["side"] == "Buy" else -1
            pnl = (price - position["entryPrice"]) * float(qty) * factor
            self._mock_positions.pop(symbol, None)
            result = {"orderId": order_id}
            self._mock_orders.append({"payload": payload, "result": result})
            return {"ok": True, "mock": True, "orderId": order_id, "payload": payload,
                    "closed": {"symbol": symbol, "side": position["side"], "qty": fmt_dec(qty),
                               "entryPrice": position["entryPrice"], "price": price,
                               "estimatedPnl": round(pnl, 4)}}
        data = self._request("POST", "/v5/order/create", None, payload)
        result = data.get("result") or {}
        return {"ok": True, "mock": False, "orderId": result.get("orderId"), "payload": payload,
                "closed": {"symbol": symbol, "side": position["side"], "qty": fmt_dec(qty),
                           "entryPrice": position["entryPrice"]}}

    # -------------------------------------------------------------- 안내

    def mode(self) -> Dict:
        return {"mock": self.is_mock, "baseUrl": self.base_url, "env": self.env,
                "testnet": self.env == "testnet", "demo": self.env == "demo",
                "label": ENV_LABELS[self.env], "sandbox": self.sandbox,
                "recvWindow": self.recv_window, "notes": list(self.notes),
                "limits": risk_limits(self.env)}

    def __repr__(self) -> str:
        return f"BybitClient(mode={'mock' if self.is_mock else self.env}, base={self.base_url})"


def _f(value) -> float:
    """Bybit가 '' 또는 None을 섞어 주므로 안전하게 0으로."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def risk_limits(env: str = "testnet") -> Dict:
    """화면·문서에 그대로 보여 줄 수 있는 안전 규칙 요약."""
    env = env if env in SANDBOX_ENVS else "testnet"
    return {
        "maxSeedUsd": MAX_SEED_USD,
        "lossPctDefault": DEFAULT_LOSS_PCT,
        "lossPctRange": [MIN_LOSS_PCT, MAX_LOSS_PCT],
        "leverageDefault": DEFAULT_LEVERAGE,
        "maxLeverage": MAX_LEVERAGE,
        "testnetOnly": True,              # 하위 호환: '메인넷 아님'
        "sandboxOnly": True,              # 샌드박스(테스트넷·데모) 전용
        "env": env,
        "baseUrl": ENV_BASE_URLS[env],
        "envBaseUrls": dict(ENV_BASE_URLS),
        "symbols": sorted(FALLBACK_INSTRUMENTS),
        "liquidationHint": f"{ENV_LABELS[env]}에서도 청산은 일어난다 — 손절은 주문에 함께 실어 보낸다",
    }
