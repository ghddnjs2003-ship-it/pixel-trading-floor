"""과거 캔들 백테스트 — 더블 S + MDS v7 룰 엔진(backend/strategy.py)을 실제 봉 위에서 돌린다.

설계 원칙
- 신호는 봉이 끝난 뒤 확정되고, 진입은 **다음 봉 시가**다(추격 금지).
- 한 번에 한 포지션. 같은 방향 재신호는 무시하고, 포지션이 열려 있으면 새 신호를 받지 않는다.
- 손절·목표가 같은 봉 안에서 둘 다 닿으면 **손절을 먼저** 친 것으로 계산한다(비관 가정).
- 수수료·슬리피지를 넣는다. 실전에서 사라지는 기대값을 백테스트가 감추지 않게.
- 결과는 승률·평균 R·기대값·손익비·최대 낙폭·연속 손실로 낸다.

청산 방식 (cfg.exit_mode)
- bracket   : 목표가 한 번에 전량 청산(기본). 손절·목표를 같은 봉에서 둘 다 닿으면 손절 우선.
- half_trail: 1R에서 절반 익절 + 손절을 본전으로, 남은 절반은 ATR 추적 손절/최종 목표.
- trail     : 목표 없이 ATR 추적 손절로 추세를 끝까지 따라간다(초기 손절만 고정).
- be_trail  : +be_trigger_r R에서 손절을 본전으로 옮기고, 그 뒤 ATR 추적.

진입 필터
- session_filter: UTC 시간대로 진입 시간을 제한한다(asia/london/ny/london_ny/ny_open/outside_ln).
- max_bars: 진입 후 N봉이 지나면 종가 청산(시간 손절). 0 = 제한 없음.

데이터
- 공개 API(바이비트·OKX·비트겟·바이낸스) 또는 저장한 CSV/JSON을 읽는다. API 키는 필요 없다.
- 바이비트는 지역(서버 위치)에 따라 403/451로 막힐 수 있어 다른 거래소로 물러날 수 있게 했다.
- `--source auto`면 위 순서로 되는 곳을 쓴다.

사용 예
    python -m backend.backtest --symbol BTCUSDT --interval 15 --limit 3000 --source auto
    python -m backend.backtest --symbol BTCUSDT --limit 3000 --source bitget --kind DS --target r:2.0
    python -m backend.backtest --csv data/btc_15m.csv --fee-bps 5 --slippage-bps 2
"""

import argparse
import csv
import json
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .strategy import Params, atr_series, scan


# ----------------------------------------------------------------------------- 데이터

INTERVAL_MS = {
    "1": 60_000, "1m": 60_000, "3": 180_000, "5": 300_000, "5m": 300_000,
    "15": 900_000, "15m": 900_000, "30": 1_800_000, "60": 3_600_000, "1h": 3_600_000,
    "240": 14_400_000, "4h": 14_400_000, "D": 86_400_000, "1d": 86_400_000,
}

_OKX_BAR = {"1": "1m", "5": "5m", "15": "15m", "30": "30m", "60": "1H", "240": "4H", "D": "1D"}
_BITGET_BAR = {"1": "1m", "5": "5m", "15": "15m", "30": "30m", "60": "1H", "240": "4H", "D": "1D"}


def _get_json(url: str, timeout: int = 20, retries: int = 4):
    """공개 API 호출 — 429(호출 한도)면 잠깐 쉬었다 다시. 장시간 이력 받을 때 필요하다."""
    req = Request(url, headers={"User-Agent": "pixel-trading-floor-backtest/1.0"})
    for attempt in range(retries):
        try:
            with urlopen(req, timeout=timeout) as res:
                return json.loads(res.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code == 429 and attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise


def _rows_to_candles(rows: Sequence[Sequence]) -> List[Dict]:
    """[시각, 시가, 고가, 저가, 종가, 거래량] 형태를 dict로. 오래된 것부터 정렬."""
    out = []
    for r in rows:
        try:
            out.append({
                "t": int(float(r[0])), "o": float(r[1]), "h": float(r[2]),
                "l": float(r[3]), "c": float(r[4]), "v": float(r[5]) if len(r) > 5 else 0.0,
            })
        except (TypeError, ValueError, IndexError):
            continue
    out.sort(key=lambda c: c["t"])
    return out


def fetch_bybit(symbol: str, interval: str, limit: int) -> List[Dict]:
    """바이비트 V5 kline — 지역 차단이면 RuntimeError."""
    got: List[Dict] = []
    end = None
    while len(got) < limit:
        q = {"category": "linear", "symbol": symbol, "interval": interval, "limit": min(1000, limit - len(got))}
        if end:
            q["end"] = end
        body = _get_json(f"https://api.bybit.com/v5/market/kline?{urlencode(q)}")
        if body.get("retCode") != 0:
            raise RuntimeError(f"Bybit 응답 오류: {body.get('retMsg')}")
        rows = body.get("result", {}).get("list", [])
        if not rows:
            break
        chunk = _rows_to_candles(rows)
        got = chunk + got
        end = chunk[0]["t"] - 1
    return got[-limit:]


def fetch_okx(symbol: str, interval: str, limit: int) -> List[Dict]:
    """OKX candles — BTCUSDT → BTC-USDT-SWAP. 100개씩 밀어 받는다."""
    base = symbol.upper().replace("/", "").replace("-", "")
    if base.endswith("USDT"):
        inst = f"{base[:-4]}-USDT-SWAP"
    else:
        raise RuntimeError("OKX는 USDT 무기한만 지원합니다 (예: BTCUSDT)")
    bar = _OKX_BAR.get(interval, "15m")
    got: List[Dict] = []
    after = None
    while len(got) < limit:
        q = {"instId": inst, "bar": bar, "limit": str(min(100, limit - len(got)))}
        if after:
            q["after"] = after
        body = _get_json(f"https://www.okx.com/api/v5/market/history-candles?{urlencode(q)}")
        if body.get("code") != "0":
            raise RuntimeError(f"OKX 응답 오류: {body.get('msg')}")
        rows = body.get("data", [])
        if not rows:
            break
        chunk = _rows_to_candles([[r[0], r[1], r[2], r[3], r[4], r[5]] for r in rows])
        got = chunk + got
        after = str(chunk[0]["t"])
    return got[-limit:]


def fetch_bitget(symbol: str, interval: str, limit: int) -> List[Dict]:
    """비트겟 v2 candles — 200개 단위. 2026-11-19 기준 이 환경에서 15분봉 3,600개 검증."""
    product = "USDT-FUTURES"
    bar = _BITGET_BAR.get(interval, "15m")
    got: List[Dict] = []
    end = None
    while len(got) < limit:
        q = {"symbol": symbol.upper(), "productType": product, "granularity": bar,
             "limit": str(min(200, limit - len(got)))}
        if end:
            q["endTime"] = str(end)
        body = _get_json(f"https://api.bitget.com/api/v2/mix/market/history-candles?{urlencode(q)}")
        if str(body.get("code")) != "00000":
            raise RuntimeError(f"Bitget 응답 오류: {body.get('msg')}")
        rows = body.get("data") or []
        if not rows:
            break
        chunk = _rows_to_candles(rows)
        got = chunk + got
        end = chunk[0]["t"] - 1
        time.sleep(0.25)
    return got[-limit:]


def fetch_binance(symbol: str, interval: str, limit: int) -> List[Dict]:
    rows = _get_json(
        f"https://fapi.binance.com/fapi/v1/klines?{urlencode({'symbol': symbol.upper(), 'interval': interval if interval[-1].isalpha() else interval + 'm', 'limit': min(1500, limit)})}"
    )
    if isinstance(rows, dict):
        raise RuntimeError(f"Binance 응답 오류: {rows.get('msg')}")
    return _rows_to_candles([[r[0], r[1], r[2], r[3], r[4], r[5]] for r in rows])


SOURCES = {"bybit": fetch_bybit, "okx": fetch_okx, "bitget": fetch_bitget, "binance": fetch_binance}


def load_candles(symbol: str = "BTCUSDT", interval: str = "15", limit: int = 1500,
                 source: str = "auto", csv_path: Optional[str] = None) -> Dict:
    """캔들 + 실제로 값을 준 거래소 이름을 돌려준다."""
    if csv_path:
        rows: List[Dict] = []
        with open(csv_path, newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            for row in reader:
                if not row or not row[0].strip() or not row[0].strip()[0].isdigit():
                    continue   # 헤더
                rows.append(row)
        candles = _rows_to_candles([[r[0], r[1], r[2], r[3], r[4], r[5] if len(r) > 5 else 0] for r in rows])
        if not candles:
            raise RuntimeError(f"{csv_path}에서 캔들을 읽지 못했습니다")
        return {"source": f"csv:{os.path.basename(csv_path)}", "symbol": symbol,
                "interval": interval, "candles": candles[-limit:]}

    order = [source] if source != "auto" else ["bybit", "okx", "bitget", "binance"]
    order = [o for o in order if o in SOURCES]
    errors = []
    for name in order:
        fn = SOURCES.get(name)
        if not fn:
            errors.append(f"{name}: 알 수 없는 거래소")
            continue
        try:
            candles = fn(symbol, interval, limit)
            if len(candles) >= 60:
                return {"source": name, "symbol": symbol, "interval": interval, "candles": candles}
            errors.append(f"{name}: 캔들 부족({len(candles)})")
        except (HTTPError, URLError, RuntimeError, ValueError, TimeoutError) as exc:
            errors.append(f"{name}: {exc}")
        time.sleep(0.2)
    raise RuntimeError("캔들을 받지 못했습니다 — " + " | ".join(errors))


# ----------------------------------------------------------------------------- 체결 모형

@dataclass
class BacktestConfig:
    initial_equity: float = 10_000.0
    risk_pct: float = 1.0            # 거래당 계좌 리스크 %
    fee_bps: float = 5.0             # 편도 수수료(bp). 바이비트 taker 0.055% ≈ 5.5bp
    slippage_bps: float = 2.0        # 편도 슬리피지(bp)
    max_leverage: float = 5.0        # 증거금 대비 최대 배수(명목 상한)
    allow_kinds: tuple = ("DS", "M1")
    exit_mode: str = "bracket"       # bracket | half_trail
    half_exit_r: float = 1.0         # half_trail: 절반 익절 지점(R배)
    trail_atr: float = 1.5           # half_trail: 남은 절반 추적 손절 폭(ATR 배수)
    max_bars: int = 0                # 0 = 제한 없음
    target_mode: str = "signal"      # signal(신호 목표) | r(고정 R)
    target_r: float = 1.5
    target_cap_r: float = 0.0        # >0이면 구조 목표가 이 R보다 멀 때 이 R로 자른다
    min_rr: float = 0.0              # 0 = 신호 목표를 그대로. >0이면 이보다 가까운 신호는 건너뛴다
    be_trigger_r: float = 1.0        # be_trail: 손절을 본전으로 옮기는 R
    session_filter: str = "off"      # off | asia | london | ny | london_ny | ny_open | outside_ln
    sessions: tuple = ()             # 직접 지정: ((시작UTC시, 끝UTC시), ...) — 있으면 session_filter보다 우선

    def to_dict(self) -> Dict:
        return asdict(self)


def _bps(x: float) -> float:
    return x / 10_000.0


# UTC 시간대 — BTC 유동성이 실리는 구간(시간 단위). 세션 이름을 바꾸면 여기만 고치면 된다.
SESSION_HOURS = {
    "off": (),
    "asia": ((0, 8),),
    "london": ((7, 16),),
    "ny": ((12, 21),),
    "ny_open": ((13, 18),),
    "london_ny": ((7, 21),),
    "outside_ln": ((0, 7), (21, 24)),
}


def session_hours(cfg: BacktestConfig) -> tuple:
    if cfg.sessions:
        return tuple(cfg.sessions)
    return SESSION_HOURS.get(cfg.session_filter, ())


def in_session(ts_ms: int, hours) -> bool:
    """봉 시각(ms)의 UTC 시가 시간대 안인지. 시간대를 주지 않았으면 항상 True."""
    if not hours:
        return True
    h = (int(ts_ms) // 3_600_000) % 24
    return any(a <= h < b for a, b in hours)


def _target_price(side: str, entry: float, risk: float, sig: Dict, cfg: BacktestConfig) -> float:
    if cfg.target_mode == "r":
        return entry + risk * cfg.target_r if side == "LONG" else entry - risk * cfg.target_r
    target = sig["target"]
    if cfg.target_cap_r > 0:
        cap = entry + risk * cfg.target_cap_r if side == "LONG" else entry - risk * cfg.target_cap_r
        target = min(target, cap) if side == "LONG" else max(target, cap)
    return target


def simulate(candles: Sequence[Dict], signals: Sequence[Dict], cfg: BacktestConfig,
             params: Optional[Params] = None) -> Dict:
    """신호 목록을 봉 위에서 체결시킨다. 다음 봉 시가 진입, 청산은 cfg.exit_mode에 따라."""
    p = params or Params()
    need_atr = cfg.exit_mode in ("half_trail", "trail", "be_trail")
    atr = atr_series(candles, p.atr_period) if need_atr else None
    by_index = {s["index"]: s for s in signals}
    equity = cfg.initial_equity
    trades: List[Dict] = []
    open_trade: Optional[Dict] = None
    skipped = {"신호없음": 0, "포지션중": 0, "종류제외": 0, "리스크초과": 0, "손익비미달": 0,
               "세션제외": 0}
    slip = _bps(cfg.slippage_bps)
    fee = _bps(cfg.fee_bps)
    hours = session_hours(cfg)

    def close_part(trade: Dict, price: float, frac: float, reason: str) -> float:
        qty = trade["qty"] * frac
        fill = price * (1 - slip) if trade["side"] == "LONG" else price * (1 + slip)
        gross = (fill - trade["entry"]) * qty if trade["side"] == "LONG" else (trade["entry"] - fill) * qty
        fees = (trade["entry"] + fill) * qty * fee
        trade["cost"] = trade.get("cost", 0.0) + fees + price * qty * slip
        return gross - fees

    for i in range(len(candles)):
        bar = candles[i]

        if open_trade:
            t = open_trade
            side = t["side"]
            hit_stop = (bar["l"] <= t["stop"]) if side == "LONG" else (bar["h"] >= t["stop"])
            hit_target = t["target"] is not None and (
                (bar["h"] >= t["target"]) if side == "LONG" else (bar["l"] <= t["target"]))
            pnl = 0.0
            reason = None
            exit_price = None
            parts: List[Dict] = []
            time_up = bool(cfg.max_bars) and (i - t["entry_bar"]) >= cfg.max_bars

            if cfg.exit_mode == "half_trail" and not t["half_done"]:
                first = t["first_target"]
                hit_first = (bar["h"] >= first) if side == "LONG" else (bar["l"] <= first)
                if hit_stop:
                    pnl = close_part(t, t["stop"], 1.0, "STOP")
                    reason, exit_price = "STOP", t["stop"]
                elif hit_first:
                    pnl += close_part(t, first, 0.5, "HALF")
                    parts.append({"price": first, "frac": 0.5, "reason": "HALF"})
                    t["half_done"] = True
                    t["stop"] = t["entry"]                       # 본전으로
                    # 같은 봉에서 본전 손절에 닿았으면 남은 절반도 정리(비관)
                    left_stop = (bar["l"] <= t["stop"]) if side == "LONG" else (bar["h"] >= t["stop"])
                    if left_stop:
                        pnl += close_part(t, t["stop"], 0.5, "STOP")
                        parts.append({"price": t["stop"], "frac": 0.5, "reason": "STOP"})
                        reason, exit_price = "HALF_THEN_BE", t["stop"]
                    elif hit_target:
                        pnl += close_part(t, t["target"], 0.5, "TARGET")
                        parts.append({"price": t["target"], "frac": 0.5, "reason": "TARGET"})
                        reason, exit_price = "TARGET", t["target"]
            elif cfg.exit_mode == "half_trail" and t["half_done"]:
                # 추적 손절 — 직전까지의 고점/저점에서 trail_atr × ATR
                a = atr[i] if atr and atr[i] else t["atr"]
                if side == "LONG":
                    t["peak"] = max(t["peak"], bar["h"])
                    t["stop"] = max(t["stop"], t["peak"] - cfg.trail_atr * a)
                else:
                    t["peak"] = min(t["peak"], bar["l"])
                    t["stop"] = min(t["stop"], t["peak"] + cfg.trail_atr * a)
                hit_stop = (bar["l"] <= t["stop"]) if side == "LONG" else (bar["h"] >= t["stop"])
                if hit_stop:
                    pnl = close_part(t, t["stop"], 0.5, "TRAIL")
                    parts.append({"price": t["stop"], "frac": 0.5, "reason": "TRAIL"})
                    reason, exit_price = "TRAIL", t["stop"]
                elif hit_target:
                    pnl = close_part(t, t["target"], 0.5, "TARGET")
                    parts.append({"price": t["target"], "frac": 0.5, "reason": "TARGET"})
                    reason, exit_price = "TARGET", t["target"]
            elif cfg.exit_mode in ("trail", "be_trail"):
                # 목표 없이 추세를 따라간다 — 초기 손절 → (본전) → ATR 추적
                a = atr[i] if atr and atr[i] else t["atr"]
                if hit_stop:
                    reason = "TRAIL" if t["trailed"] else "STOP"
                    pnl = close_part(t, t["stop"], 1.0, reason)
                    exit_price = t["stop"]
                else:
                    if cfg.exit_mode == "be_trail" and not t["be_done"]:
                        trig = (t["entry"] + cfg.be_trigger_r * t["risk"] if side == "LONG"
                                else t["entry"] - cfg.be_trigger_r * t["risk"])
                        reached = (bar["h"] >= trig) if side == "LONG" else (bar["l"] <= trig)
                        if reached:
                            t["stop"] = t["entry"]
                            t["be_done"] = True
                            t["trailed"] = True
                    if side == "LONG":
                        t["peak"] = max(t["peak"], bar["h"])
                        t["stop"] = max(t["stop"], t["peak"] - cfg.trail_atr * a)
                        if t["stop"] > t["initial_stop"]:
                            t["trailed"] = True
                    else:
                        t["peak"] = min(t["peak"], bar["l"])
                        t["stop"] = min(t["stop"], t["peak"] + cfg.trail_atr * a)
                        if t["stop"] < t["initial_stop"]:
                            t["trailed"] = True
                    if hit_stop:
                        pnl = close_part(t, t["stop"], 1.0, "TRAIL")
                        reason, exit_price = "TRAIL", t["stop"]
                    elif time_up:
                        pnl = close_part(t, bar["c"], 1.0, "TIME")
                        reason, exit_price = "TIME", bar["c"]
            else:
                if hit_stop:
                    pnl = close_part(t, t["stop"], 1.0, "STOP")
                    reason, exit_price = "STOP", t["stop"]
                elif hit_target:
                    pnl = close_part(t, t["target"], 1.0, "TARGET")
                    reason, exit_price = "TARGET", t["target"]

            if exit_price is None and time_up and not t["half_done"]:
                pnl = close_part(t, bar["c"], 1.0, "TIME")
                reason, exit_price = "TIME", bar["c"]
            elif exit_price is None and time_up and t["half_done"]:
                pnl += close_part(t, bar["c"], 0.5, "TIME")
                parts.append({"price": bar["c"], "frac": 0.5, "reason": "TIME"})
                reason, exit_price = "TIME", bar["c"]

            if exit_price is not None:
                equity += pnl
                trades.append({
                    **{k: t[k] for k in ("side", "kind", "grade", "entry", "initial_stop", "qty", "risk_usd")},
                    "stop": round(t["initial_stop"], 2),
                    "target": round(t["target"], 2) if t["target"] is not None else None,
                    "entry_time": t["entry_time"], "exit_time": bar["t"], "exit": exit_price,
                    "reason": reason, "pnl": round(pnl, 2),
                    "r": round(pnl / t["risk_usd"], 3) if t["risk_usd"] else 0.0,
                    "equity": round(equity, 2), "bars": i - t["entry_bar"],
                    "parts": parts, "signal_time": t["signal_time"],
                    "cost": round(t.get("cost", 0.0), 2),
                    "reasons": t.get("reasons", []),
                })
                open_trade = None
            if open_trade:
                continue   # 이 봉은 청산·보유로 끝

        sig = by_index.get(i)
        if not sig:
            skipped["신호없음"] += 1
            continue
        if i + 1 >= len(candles):
            continue
        if sig["kind"] not in cfg.allow_kinds:
            skipped["종류제외"] += 1
            continue
        if open_trade:
            skipped["포지션중"] += 1
            continue

        entry_bar = candles[i + 1]
        if not in_session(entry_bar["t"], hours):     # 세션·시간대 필터
            skipped["세션제외"] += 1
            continue
        side = sig["side"]
        entry = entry_bar["o"] * (1 + slip) if side == "LONG" else entry_bar["o"] * (1 - slip)
        stop = sig["stop"]
        if sig.get("stop_atr"):                       # ATR 고정 손절 — 실제 진입가 기준으로 다시 잡는다
            dist = sig["stop_atr"] * sig["atr"]
            stop = entry - dist if side == "LONG" else entry + dist
        # 시가가 이미 손절/목표를 지나 있으면 체결하지 않는다(갭에서 규칙이 무의미해짐)
        if side == "LONG" and entry <= stop:
            skipped["리스크초과"] += 1
            continue
        if side == "SHORT" and entry >= stop:
            skipped["리스크초과"] += 1
            continue
        risk_per_unit = abs(entry - stop)
        if risk_per_unit <= 0:
            continue
        target = _target_price(side, entry, risk_per_unit, sig, cfg)
        if cfg.exit_mode in ("trail", "be_trail"):
            target = None                             # 추적 청산에는 목표가를 쓰지 않는다
        elif cfg.min_rr and abs(target - entry) / risk_per_unit < cfg.min_rr - 1e-9:
            skipped["손익비미달"] += 1
            continue
        if target is not None:
            if side == "LONG" and entry >= target:
                skipped["리스크초과"] += 1
                continue
            if side == "SHORT" and entry <= target:
                skipped["리스크초과"] += 1
                continue
        risk_usd = equity * cfg.risk_pct / 100
        qty = risk_usd / risk_per_unit
        notional = qty * entry
        if notional > equity * cfg.max_leverage:      # 레버리지 상한에 걸리면 수량을 줄인다
            qty = equity * cfg.max_leverage / entry
            risk_usd = qty * risk_per_unit
        open_trade = {
            "side": side, "kind": sig["kind"], "grade": sig.get("grade"),
            "entry": entry, "stop": stop, "target": target, "qty": qty, "risk_usd": risk_usd,
            "initial_stop": stop, "risk": risk_per_unit,
            "entry_time": entry_bar["t"], "entry_bar": i + 1, "signal_bar": i,
            "signal_time": sig.get("time"), "atr": sig.get("atr"), "cost": 0.0,
            "half_done": False, "peak": entry, "be_done": False, "trailed": False,
            "first_target": entry + risk_per_unit * cfg.half_exit_r if side == "LONG"
            else entry - risk_per_unit * cfg.half_exit_r,
            "reasons": sig.get("reasons", []),
        }

    stats = summarize(trades, cfg, equity)
    stats["skipped"] = skipped
    stats["open_trade"] = open_trade is not None
    stats["source_symbol"] = None
    return {"stats": stats, "trades": trades, "final_equity": round(equity, 2)}


def summarize(trades: Sequence[Dict], cfg: BacktestConfig, equity: float) -> Dict:
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    r_values = [t["r"] for t in trades]
    peak = cfg.initial_equity
    max_dd = 0.0
    curve = []
    for t in trades:
        peak = max(peak, t["equity"])
        max_dd = max(max_dd, (peak - t["equity"]) / peak * 100 if peak else 0.0)
        curve.append(t["equity"])
    streak = worst = 0
    for t in trades:
        streak = streak + 1 if t["pnl"] < 0 else 0
        worst = max(worst, streak)

    def _bucket(key) -> Dict:
        out = {}
        for t in trades:
            k = key(t)
            b = out.setdefault(k, {"trades": 0, "wins": 0, "pnl": 0.0})
            b["trades"] += 1
            b["wins"] += 1 if t["pnl"] > 0 else 0
            b["pnl"] += t["pnl"]
        return out

    by_side = {}
    for side in ("LONG", "SHORT"):
        s = _bucket(lambda t, sd=side: t.get("side")).get(side)
        by_side[side] = {
            "trades": s["trades"] if s else 0,
            "win_rate_pct": round(s["wins"] / s["trades"] * 100, 1) if s and s["trades"] else None,
            "net_pnl": round(s["pnl"], 2) if s else 0.0,
        }
    months = _bucket(lambda t: datetime.fromtimestamp(t["entry_time"] / 1000, tz=timezone.utc)
                     .strftime("%Y-%m") if t.get("entry_time") else "-")
    monthly = [{"month": m, "trades": b["trades"],
                "win_rate_pct": round(b["wins"] / b["trades"] * 100, 1),
                "net_pnl": round(b["pnl"], 2)} for m, b in sorted(months.items()) if m != "-"]
    cost_r = [(t.get("cost") or 0.0) / t["risk_usd"] for t in trades if t.get("risk_usd")]
    return {
        "by_side": by_side,
        "monthly": monthly,
        "avg_cost_r": round(sum(cost_r) / len(cost_r), 3) if cost_r else None,
        "trades": n,
        "win_rate_pct": round(len(wins) / n * 100, 1) if n else None,
        "avg_r": round(sum(r_values) / n, 2) if n else None,
        "expectancy_pct": round(sum(t["pnl"] for t in trades) / cfg.initial_equity / n * 100, 3) if n else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (None if not wins else float("inf")),
        "net_pnl": round(sum(t["pnl"] for t in trades), 2),
        "return_pct": round((equity - cfg.initial_equity) / cfg.initial_equity * 100, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "max_losing_streak": worst,
        "avg_bars_held": round(sum(t["bars"] for t in trades) / n, 1) if n else None,
        "by_kind": {
            kind: {
                "trades": sum(1 for t in trades if t["kind"] == kind),
                "win_rate_pct": (round(sum(1 for t in trades if t["kind"] == kind and t["pnl"] > 0)
                                       / max(1, sum(1 for t in trades if t["kind"] == kind)) * 100, 1)),
            }
            for kind in ("DS", "M1")
        },
    }


def run_backtest(symbol: str = "BTCUSDT", interval: str = "15", limit: int = 1500,
                 source: str = "auto", csv_path: Optional[str] = None,
                 cfg: Optional[BacktestConfig] = None, params: Optional[Params] = None) -> Dict:
    data = load_candles(_csv_symbol(symbol), interval, limit, source, csv_path)
    cfg = cfg or BacktestConfig()
    params = params or Params()
    signals = scan(data["candles"], params)
    result = simulate(data["candles"], signals, cfg, params)
    result["source"] = data["source"]
    result["symbol"] = symbol
    result["interval"] = interval
    result["bars"] = len(data["candles"])
    if data["candles"]:
        result["period"] = {
            "from": datetime.fromtimestamp(data["candles"][0]["t"] / 1000, tz=timezone.utc).isoformat(),
            "to": datetime.fromtimestamp(data["candles"][-1]["t"] / 1000, tz=timezone.utc).isoformat(),
        }
    result["signal_count"] = len(signals)
    return result


def _csv_symbol(symbol: str) -> str:
    return symbol.upper()


# ----------------------------------------------------------------------------- CLI

def _report(result: Dict) -> str:
    s = result["stats"]
    lines = [
        f"■ 백테스트 {result['symbol']} · {result['interval']}분봉 · {result['bars']}봉 "
        f"({result.get('period', {}).get('from', '?')} ~ {result.get('period', {}).get('to', '?')})",
        f"  데이터: {result['source']} · 신호 {result['signal_count']}개 · 체결 {s['trades']}건",
        f"  승률 {s['win_rate_pct']}% · 평균 R {s['avg_r']} · 손익비 PF {s['profit_factor']}",
        f"  순손익 {s['net_pnl']:,.2f} USDT ({s['return_pct']}%) · 최대낙폭 {s['max_drawdown_pct']}% · "
        f"최장 연속손실 {s['max_losing_streak']}",
        f"  종류별: 더블S {s['by_kind']['DS']['trades']}건({s['by_kind']['DS']['win_rate_pct']}%) · "
        f"M1 {s['by_kind']['M1']['trades']}건({s['by_kind']['M1']['win_rate_pct']}%)",
    ]
    if s["trades"] == 0:
        lines.append("  ※ 체결 0건 — 이 구간에서는 v7 규칙이 선 자리가 없었습니다(규칙을 풀지 않고 그대로 보고합니다).")
    return "\n".join(lines)


def _parse_target(text: str) -> Dict:
    """--target signal | r:1.5 | r:2.0 | r:2.5"""
    if text in (None, "", "signal"):
        return {"target_mode": "signal"}
    if text.startswith("r:"):
        return {"target_mode": "r", "target_r": float(text[2:])}
    raise SystemExit("--target은 signal 또는 r:1.5 / r:2.0 / r:2.5 형태로 주세요")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="더블 S + MDS v7 룰 백테스트")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="15", help="1/5/15/30/60/240/D (분·시간·일)")
    ap.add_argument("--limit", type=int, default=1500, help="받을 봉 수")
    ap.add_argument("--source", default="auto", choices=["auto", "bybit", "okx", "bitget", "binance"])
    ap.add_argument("--csv", default=None, help="저장한 캔들 CSV(시각,시가,고가,저가,종가,거래량)")
    ap.add_argument("--json-in", default=None, help="저장해 둔 캔들 JSON(엔진 대조·재현용)")
    ap.add_argument("--equity", type=float, default=10_000.0)
    ap.add_argument("--risk-pct", type=float, default=1.0)
    ap.add_argument("--fee-bps", type=float, default=5.0)
    ap.add_argument("--slippage-bps", type=float, default=2.0)
    ap.add_argument("--min-rr", type=float, default=1.5, help="엔진의 최소 손익비(목표 대체 기준)")
    ap.add_argument("--tm", type=float, default=0.0, help="체결 시 최소 손익비(0 = 신호 목표 그대로)")
    ap.add_argument("--cci-mode", default="sign", choices=["off", "sign", "threshold"])
    ap.add_argument("--volume-mult", type=float, default=1.15, help="M1 돌파 거래량 배수")
    ap.add_argument("--bb-expand", default=True, action="store_true", help="M1 볼린저 폭 확장 요구(기본)")
    ap.add_argument("--no-bb-expand", dest="bb_expand", action="store_false")
    ap.add_argument("--sweep-max", type=float, default=1.6, help="더블 S 스윕 최대 깊이(ATR)")
    ap.add_argument("--bos", default=False, action="store_true", help="회복 봉의 구조 돌파(BOS) 요구(파인에는 없음)")
    ap.add_argument("--stop-buf", type=float, default=0.15, help="더블 S 손절 버퍼(ATR)")
    ap.add_argument("--m1-stop-buf", type=float, default=0.0, help="M1 손절 버퍼(ATR, 0 = 존 경계)")
    ap.add_argument("--stop-mode", default="structure", choices=["structure", "atr"],
                    help="structure(스빕 극점·존 반대편) | atr(진입가 ∓ 배수 × ATR)")
    ap.add_argument("--stop-atr", type=float, default=1.0, help="--stop-mode atr일 때 손절 폭(ATR)")
    ap.add_argument("--ds-target", default="liq", choices=["liq", "zone", "r"], help="더블 S 목표 기준")
    ap.add_argument("--m1-target", default="zone", choices=["zone", "liq", "r"], help="M1 목표 기준")
    ap.add_argument("--ds-labels", default="거부,복귀", help="더블 S 라벨 선택(예: 거부)")
    ap.add_argument("--min-rvol", type=float, default=0.0, help="더블 S 신호 봉 상대거래량 하한")
    ap.add_argument("--htf", default="off", choices=["off", "ema50", "ema200"], help="상위 4H 추세 필터")
    ap.add_argument("--target", default="signal", help="signal | r:1.5 | r:2.0 | r:3.0")
    ap.add_argument("--target-cap-r", type=float, default=0.0, help="구조 목표를 이 R로 자름(0 = 안 자름)")
    ap.add_argument("--exit-mode", default="bracket", choices=["bracket", "half_trail", "trail", "be_trail"])
    ap.add_argument("--trail-atr", type=float, default=1.5)
    ap.add_argument("--be-trigger-r", type=float, default=1.0, help="be_trail: 본전 이동 R")
    ap.add_argument("--max-bars", type=int, default=0)
    ap.add_argument("--session", default="off",
                    choices=["off", "asia", "london", "ny", "ny_open", "london_ny", "outside_ln"])
    ap.add_argument("--allow", default="DS,M1", help="체결할 신호 종류(예: DS,M1 / DS)")
    ap.add_argument("--json-out", default=None, help="결과 JSON 저장 경로")
    args = ap.parse_args(argv)

    htf_len = {"off": 50, "ema50": 50, "ema200": 200}[args.htf]
    params = Params(min_rr=args.min_rr, cci_mode=args.cci_mode, volume_mult=args.volume_mult,
                    bb_expand=args.bb_expand, sweep_max_atr=args.sweep_max, require_bos=args.bos,
                    stop_buffer_atr=args.stop_buf, m1_stop_buffer_atr=args.m1_stop_buf,
                    stop_mode=args.stop_mode, stop_atr=args.stop_atr,
                    ds_target=args.ds_target, m1_target=args.m1_target,
                    ds_labels=tuple(s.strip() for s in args.ds_labels.split(",") if s.strip()),
                    min_rvol=args.min_rvol,
                    use_htf_trend=args.htf != "off", htf_ema_len=htf_len)
    cfg = BacktestConfig(initial_equity=args.equity, risk_pct=args.risk_pct,
                         fee_bps=args.fee_bps, slippage_bps=args.slippage_bps,
                         allow_kinds=tuple(k.strip().upper() for k in args.allow.split(",") if k.strip()),
                         exit_mode=args.exit_mode, trail_atr=args.trail_atr, max_bars=args.max_bars,
                         be_trigger_r=args.be_trigger_r, target_cap_r=args.target_cap_r,
                         session_filter=args.session,
                         min_rr=args.tm, **_parse_target(args.target))
    if args.json_in:
        candles = json.load(open(args.json_in, encoding="utf-8"))
        signals = scan(candles, params)
        result = simulate(candles, signals, cfg, params)
        result.update(source=f"json:{os.path.basename(args.json_in)}", symbol=args.symbol,
                      interval=args.interval, bars=len(candles), signal_count=len(signals),
                      period={"from": candles[0]["t"], "to": candles[-1]["t"]})
    else:
        result = run_backtest(args.symbol, args.interval, args.limit, args.source, args.csv, cfg, params)
    print(_report(result))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print(f"  저장: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
