"""과거 캔들 백테스트 — 더블 S + MDS v7 룰 엔진(backend/strategy.py)을 실제 봉 위에서 돌린다.

설계 원칙
- 신호는 봉이 끝난 뒤 확정되고, 진입은 **다음 봉 시가**다(추격 금지).
- 한 번에 한 포지션. 같은 방향 재신호는 무시하고, 포지션이 열려 있으면 새 신호를 받지 않는다.
- 손절·목표가 같은 봉 안에서 둘 다 닿으면 **손절을 먼저** 친 것으로 계산한다(비관 가정).
- 수수료·슬리피지를 넣는다. 실전에서 사라지는 기대값을 백테스트가 감추지 않게.
- 결과는 승률·평균 R·기대값·손익비·최대 낙폭·연속 손실로 낸다.

데이터
- 공개 API(바이비트·OKX·비트겟·바이낸스) 또는 저장한 CSV/JSON을 읽는다. API 키는 필요 없다.
- 바이비트는 지역(서버 위치)에 따라 403/451로 막힐 수 있어 다른 거래소로 물러날 수 있게 했다.
- `--source auto`면 위 순서로 되는 곳을 쓴다.

사용 예
    python -m backend.backtest --symbol BTCUSDT --interval 15 --limit 1500 --source auto
    python -m backend.backtest --csv data/btc_15m.csv --fee-bps 5 --slippage-bps 2
"""

import argparse
import csv
import json
import math
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .strategy import Params, scan


# ----------------------------------------------------------------------------- 데이터

INTERVAL_MS = {
    "1": 60_000, "1m": 60_000, "3": 180_000, "5": 300_000, "5m": 300_000,
    "15": 900_000, "15m": 900_000, "30": 1_800_000, "60": 3_600_000, "1h": 3_600_000,
    "240": 14_400_000, "4h": 14_400_000, "D": 86_400_000, "1d": 86_400_000,
}

_OKX_BAR = {"1": "1m", "5": "5m", "15": "15m", "30": "30m", "60": "1H", "240": "4H", "D": "1D"}
_BITGET_BAR = {"1": "1m", "5": "5m", "15": "15m", "30": "30m", "60": "1H", "240": "4H", "D": "1D"}


def _get_json(url: str, timeout: int = 20):
    req = Request(url, headers={"User-Agent": "pixel-trading-floor-backtest/1.0"})
    with urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


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
    """비트겟 v2 candles — 200개 단위."""
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

    def to_dict(self) -> Dict:
        return asdict(self)


def _bps(x: float) -> float:
    return x / 10_000.0


def simulate(candles: Sequence[Dict], signals: Sequence[Dict], cfg: BacktestConfig,
             params: Optional[Params] = None) -> Dict:
    """신호 목록을 봉 위에서 체결시킨다. 다음 봉 시가 진입, 손절·목표 도달 시 청산."""
    p = params or Params()
    by_index = {s["index"]: s for s in signals}
    equity = cfg.initial_equity
    peak = equity
    max_dd = 0.0
    trades: List[Dict] = []
    open_trade: Optional[Dict] = None
    streak = 0
    max_streak = 0
    skipped = {"신호없음": 0, "포지션중": 0, "종류제외": 0, "리스크초과": 0}

    for i in range(len(candles)):
        bar = candles[i]

        # 보유 중이면 이 봉에서 청산 여부를 본다(같은 봉에서 둘 다 닿으면 손절 우선)
        if open_trade:
            side = open_trade["side"]
            stop_hit = (bar["l"] <= open_trade["stop"]) if side == "LONG" else (bar["h"] >= open_trade["stop"])
            target_hit = (bar["h"] >= open_trade["target"]) if side == "LONG" else (bar["l"] <= open_trade["target"])
            exit_price = None
            reason = None
            if stop_hit:
                exit_price, reason = open_trade["stop"], "STOP"
            elif target_hit:
                exit_price, reason = open_trade["target"], "TARGET"
            if exit_price is not None:
                slip = _bps(cfg.slippage_bps)
                fill = exit_price * (1 - slip) if side == "LONG" else exit_price * (1 + slip)
                qty = open_trade["qty"]
                gross = (fill - open_trade["entry"]) * qty if side == "LONG" else (open_trade["entry"] - fill) * qty
                fees = (open_trade["entry"] + fill) * qty * _bps(cfg.fee_bps)
                pnl = gross - fees
                equity += pnl
                peak = max(peak, equity)
                max_dd = max(max_dd, (peak - equity) / peak * 100 if peak else 0.0)
                r_mult = pnl / open_trade["risk_usd"] if open_trade["risk_usd"] else 0.0
                if pnl < 0:
                    streak += 1
                    max_streak = max(max_streak, streak)
                else:
                    streak = 0
                trades.append({
                    **{k: open_trade[k] for k in ("side", "kind", "grade", "entry", "stop", "target", "qty", "risk_usd")},
                    "entry_time": open_trade["entry_time"], "exit_time": bar["t"], "exit": fill,
                    "reason": reason, "pnl": round(pnl, 2), "r": round(r_mult, 3),
                    "equity": round(equity, 2), "bars": i - open_trade["entry_bar"],
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
        slip = _bps(cfg.slippage_bps)
        entry = entry_bar["o"] * (1 + slip) if sig["side"] == "LONG" else entry_bar["o"] * (1 - slip)
        stop, target = sig["stop"], sig["target"]
        # 시가가 이미 손절/목표를 지나 있으면 체결하지 않는다(갭에서 규칙이 무의미해짐)
        if sig["side"] == "LONG" and (entry <= stop or entry >= target):
            skipped["리스크초과"] += 1
            continue
        if sig["side"] == "SHORT" and (entry >= stop or entry <= target):
            skipped["리스크초과"] += 1
            continue
        risk_per_unit = abs(entry - stop)
        if risk_per_unit <= 0:
            continue
        risk_usd = equity * cfg.risk_pct / 100
        qty = risk_usd / risk_per_unit
        notional = qty * entry
        if notional > equity * cfg.max_leverage:      # 레버리지 상한에 걸리면 수량을 줄인다
            qty = equity * cfg.max_leverage / entry
            risk_usd = qty * risk_per_unit
        open_trade = {
            "side": sig["side"], "kind": sig["kind"], "grade": sig.get("grade"),
            "entry": entry, "stop": stop, "target": target, "qty": qty, "risk_usd": risk_usd,
            "entry_time": entry_bar["t"], "entry_bar": i + 1,
            "signal_bar": i, "reasons": sig.get("reasons", []),
        }

    stats = summarize(trades, cfg, equity)
    stats["skipped"] = skipped
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
    return {
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="더블 S + MDS v7 룰 백테스트")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="15", help="1/5/15/30/60/240/D (분·시간·일)")
    ap.add_argument("--limit", type=int, default=1500, help="받을 봉 수")
    ap.add_argument("--source", default="auto", choices=["auto", "bybit", "okx", "bitget", "binance"])
    ap.add_argument("--csv", default=None, help="저장한 캔들 CSV(시각,시가,고가,저가,종가,거래량)")
    ap.add_argument("--equity", type=float, default=10_000.0)
    ap.add_argument("--risk-pct", type=float, default=1.0)
    ap.add_argument("--fee-bps", type=float, default=5.0)
    ap.add_argument("--slippage-bps", type=float, default=2.0)
    ap.add_argument("--min-rr", type=float, default=1.5)
    ap.add_argument("--cci-mode", default="sign", choices=["off", "sign", "threshold"])
    ap.add_argument("--volume-mult", type=float, default=1.15, help="M1 돌파 거래량 배수")
    ap.add_argument("--bb-expand", default=True, action="store_true", help="M1 볼린저 폭 확장 요구(기본)")
    ap.add_argument("--no-bb-expand", dest="bb_expand", action="store_false")
    ap.add_argument("--sweep-max", type=float, default=1.6, help="더블 S 스윕 최대 깊이(ATR)")
    ap.add_argument("--bos", default=True, action="store_true", help="회복 봉의 구조 돌파(BOS) 요구 — v6 기본")
    ap.add_argument("--no-bos", dest="bos", action="store_false")
    ap.add_argument("--allow", default="DS,M1", help="체결할 신호 종류(예: DS,M1 / DS)")
    ap.add_argument("--json-out", default=None, help="결과 JSON 저장 경로")
    args = ap.parse_args(argv)

    params = Params(min_rr=args.min_rr, cci_mode=args.cci_mode, volume_mult=args.volume_mult,
                    bb_expand=args.bb_expand, sweep_max_atr=args.sweep_max, require_bos=args.bos)
    cfg = BacktestConfig(initial_equity=args.equity, risk_pct=args.risk_pct,
                         fee_bps=args.fee_bps, slippage_bps=args.slippage_bps,
                         allow_kinds=tuple(k.strip().upper() for k in args.allow.split(",") if k.strip()))
    result = run_backtest(args.symbol, args.interval, args.limit, args.source, args.csv, cfg, params)
    print(_report(result))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print(f"  저장: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
