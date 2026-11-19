"""백테스트 조합 탐색 — v7 룰을 손절·목표·필터 조합별로 돌려 표로 비교한다.

    python -m backend.explore --json data/btc_15m.json
    python -m backend.explore --json data/btc_15m.json --split 0.6 --top 15

원칙
- 표본이 작으면 숫자가 잘 나와도 믿지 않는다. 체결 수·승률·PF·순손익·MDD·평균R을 함께 본다.
- 구간을 앞 60% / 뒤 40%로 갈라 **인샘플에서 고른 조합이 아웃샘플에서도 남는지** 같이 낸다.
- 수수료 5bp·슬리피지 2bp·손절 우선 체결은 모든 조합에 똑같이 적용한다(조합끼리만 비교).
"""

import argparse
import itertools
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

from .backtest import BacktestConfig, load_candles, simulate, summarize
from .strategy import Engine, Params

KST = timezone(timedelta(hours=9))


def kst(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=KST).strftime("%m-%d %H:%M")


def _load(path: Optional[str], source: str, limit: int) -> List[Dict]:
    if path:
        candles = json.load(open(path, encoding="utf-8"))
        candles.sort(key=lambda c: c["t"])
        return candles
    return load_candles("BTCUSDT", "15", limit, source)["candles"]


def _fmt(row: Dict) -> str:
    def f(v, spec="{}"):
        return "-" if v is None else (spec.format(v) if not isinstance(v, float) else spec.format(round(v, 2)))
    return (f"{row['name']:<34s} {row['trades']:>4d} {f(row['win']):>6s} {f(row['pf']):>6s} "
            f"{f(row['net']):>8s} {f(row['mdd']):>6s} {f(row['avg_r']):>6s} "
            f"{row['is_trades']:>4d}/{f(row['is_net']):>7s} {row['os_trades']:>4d}/{f(row['os_net']):>7s}")


HEADER = (f"{'조합':<34s} {'체결':>4s} {'승률%':>6s} {'PF':>6s} {'순손익%':>8s} {'MDD%':>6s} {'평균R':>6s} "
          f"{'IS':>4s}/{'손익%':>7s} {'OS':>4s}/{'손익%':>7s}")


def build_configs() -> List[Dict]:
    """조합 목록 — (이름, Params 덮어쓰기, BacktestConfig 덮어쓰기)."""
    base_p = dict(min_rr=1.5, stop_buffer_atr=0.15, m1_stop_buffer_atr=0.0)
    base_c = dict(exit_mode="bracket", target_mode="signal", fee_bps=5.0, slippage_bps=2.0)
    cfgs: List[Dict] = []

    def add(name, p=None, c=None):
        cfgs.append({"name": name, "p": {**base_p, **(p or {})}, "c": {**base_c, **(c or {})}})

    # A. 신호 종류
    add("A1 더블S 단독", c={"allow_kinds": ("DS",)})
    add("A2 M1 단독", c={"allow_kinds": ("M1",)})
    add("A3 통합(기본)", c={"allow_kinds": ("DS", "M1")})

    # B. 목표 기준
    for tname, tmode in (("반대편 유동성", "signal"), ("1.5R", "r:1.5"), ("2.0R", "r:2.0"), ("2.5R", "r:2.5")):
        c = {"target_mode": "signal"} if tmode == "signal" else {"target_mode": "r", "target_r": float(tmode[2:])}
        add(f"B 목표 {tname} (통합)", c=c)
        add(f"B 목표 {tname} (더블S만)", c={**c, "allow_kinds": ("DS",)})
    # C. 청산 방식
    add("C 반익+본전+ATR추적 (통합)", c={"exit_mode": "half_trail", "trail_atr": 1.5})
    add("C 반익+본전+ATR추적 (더블S)", c={"exit_mode": "half_trail", "trail_atr": 1.5, "allow_kinds": ("DS",)})
    add("C 2.0R 목표 + ATR추적 (통합)", c={"exit_mode": "half_trail", "trail_atr": 1.0, "target_mode": "r", "target_r": 2.0})
    # D. 손절 기준
    for buf in (0.0, 0.15, 0.30):
        add(f"D 더블S 손절 버퍼 {buf:.2f} ATR", p={"stop_buffer_atr": buf}, c={"allow_kinds": ("DS",)})
    for buf in (0.0, 0.15):
        add(f"D M1 손절 버퍼 {buf:.2f} ATR", p={"m1_stop_buffer_atr": buf}, c={"allow_kinds": ("M1",)})
    # E. 상위 추세 필터
    add("E 상위추세 끔 (통합)")
    add("E 4H EMA50 (통합)", p={"use_htf_trend": True, "htf_ema_len": 50})
    add("E 4H EMA200 (통합)", p={"use_htf_trend": True, "htf_ema_len": 200})
    add("E 4H EMA200 (더블S)", p={"use_htf_trend": True, "htf_ema_len": 200}, c={"allow_kinds": ("DS",)})
    add("E 4H EMA200 (M1)", p={"use_htf_trend": True, "htf_ema_len": 200}, c={"allow_kinds": ("M1",)})
    # F. BOS
    add("F BOS 요구 (더블S)", p={"require_bos": True}, c={"allow_kinds": ("DS",)})
    add("F BOS 요구 (통합)", p={"require_bos": True})
    # H. 더블 S에 상위 추세 동조를 요구(파인 밖 실험 옵션) · 방향 제한
    add("H 더블S+4H EMA200 (통합)", p={"ds_htf_filter": True, "htf_ema_len": 200})
    add("H 더블S+4H EMA200 (더블S)", p={"ds_htf_filter": True, "htf_ema_len": 200}, c={"allow_kinds": ("DS",)})
    add("H 더블S+4H EMA50 (더블S)", p={"ds_htf_filter": True, "htf_ema_len": 50}, c={"allow_kinds": ("DS",)})
    add("H 롱만 (통합)", p={"allow_sides": ("LONG",)})
    add("H 숏만 (통합)", p={"allow_sides": ("SHORT",)})
    add("H 롱만+4H EMA200 (통합)", p={"allow_sides": ("LONG",), "use_htf_trend": True, "htf_ema_len": 200, "ds_htf_filter": True})
    add("H 숏만+4H EMA200 (통합)", p={"allow_sides": ("SHORT",), "use_htf_trend": True, "htf_ema_len": 200, "ds_htf_filter": True})

    # G. 손익비 문턱(체결 시)
    for rr in (1.0, 1.5, 2.0):
        add(f"G 최소 손익비 {rr:.1f}R (통합)", c={"min_rr": rr})
    return cfgs


def run_configs(candles: List[Dict], cfgs: List[Dict], split: float) -> List[Dict]:
    split_t = candles[int(len(candles) * split)]["t"]
    engine_cache: Dict[str, List[Dict]] = {}
    rows: List[Dict] = []
    for cfg in cfgs:
        p = Params(**cfg["p"])
        key = json.dumps({**cfg["p"]}, sort_keys=True, ensure_ascii=False)
        if key not in engine_cache:
            engine_cache[key] = Engine(p).run(candles)
        signals = engine_cache[key]
        bcfg = BacktestConfig(**cfg["c"])
        res = simulate(candles, signals, bcfg, p)
        trades = res["trades"]
        s = res["stats"]
        is_trades = [t for t in trades if t["entry_time"] < split_t]
        os_trades = [t for t in trades if t["entry_time"] >= split_t]
        is_sum = summarize(is_trades, bcfg, bcfg.initial_equity)
        os_sum = summarize(os_trades, bcfg, bcfg.initial_equity)
        is_net = round(sum(t["pnl"] for t in is_trades) / bcfg.initial_equity * 100, 2)
        os_net = round(sum(t["pnl"] for t in os_trades) / bcfg.initial_equity * 100, 2)
        rows.append({
            "name": cfg["name"], "trades": s["trades"], "win": s["win_rate_pct"], "pf": s["profit_factor"],
            "net": s["return_pct"], "mdd": s["max_drawdown_pct"], "avg_r": s["avg_r"],
            "is_trades": len(is_trades), "is_net": is_net, "is_pf": is_sum["profit_factor"],
            "os_trades": len(os_trades), "os_net": os_net, "os_pf": os_sum["profit_factor"],
            "ds": s["by_kind"]["DS"]["trades"], "m1": s["by_kind"]["M1"]["trades"],
            "net_pnl": s["net_pnl"],
        })
    return rows


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="v7 룰 백테스트 조합 탐색")
    ap.add_argument("--json", default=None, help="캔들 JSON 경로")
    ap.add_argument("--source", default="bitget", help="JSON 없이 받을 때 거래소")
    ap.add_argument("--limit", type=int, default=8200)
    ap.add_argument("--split", type=float, default=0.6, help="인샘플 비율(앞 구간)")
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--only", default=None, help="이름에 이 글자가 든 조합만 실행")
    args = ap.parse_args(argv)

    candles = _load(args.json, args.source, args.limit)
    print(f"■ 데이터 {len(candles)}봉 ({kst(candles[0]['t'])} ~ {kst(candles[-1]['t'])}) · "
          f"인샘플 {int(len(candles) * args.split)}봉 / 아웃샘플 {len(candles) - int(len(candles) * args.split)}봉")
    cfgs = build_configs()
    if args.only:
        cfgs = [c for c in cfgs if args.only in c["name"]]
    rows = run_configs(candles, cfgs, args.split)
    print(HEADER)
    for r in rows:
        print(_fmt(r))

    def top(key, n=5):
        return sorted([r for r in rows if r["trades"] >= 10], key=lambda r: (r[key] is not None, r[key]), reverse=True)[:n]

    print("\n■ 인샘플 순손익 상위 5 (체결 10건 이상)")
    for r in top("is_net"):
        print(f"  {r['name']:<34s} IS {r['is_net']:>6}% (PF {r['is_pf']}) → OS {r['os_net']:>6}% (PF {r['os_pf']}, {r['os_trades']}건)")
    print("■ 전체 순손익 상위 5 (체결 10건 이상)")
    for r in top("net"):
        print(f"  {r['name']:<34s} 전체 {r['net']:>6}% · 체결 {r['trades']}건 · PF {r['pf']} · MDD {r['mdd']}%")
    if args.json_out:
        json.dump(rows, open(args.json_out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"\n저장: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
