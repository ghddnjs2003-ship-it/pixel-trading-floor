"""후보 조합 하나를 끝까지 검증한다 — 분할·분기·비용·부트스트랩·플라시보.

실험실(`lab.py`)에서 인샘플/아웃샘플이 모두 플러스인 칸을 찾아도, 그 칸이 우연일 수 있다.
조합을 수백 개 돌리면 하나쯤은 그렇게 나오기 때문이다. 이 파일은 후보 하나를 놓고

1. 분할 지점을 바꿔도(50/60/70%) 같은 방향인가
2. 분기·월별로 몰려 있지 않은가
3. 비용 가정을 바꿔도 살아남는가
4. 거래 R의 부트스트랩 신뢰구간이 0을 넘는가
5. 신호 시각만 무작위로 옮긴 대조군(플라시보)보다 나은가

를 차례로 본다. 5번이 핵심이다 — 같은 손절·목표·세션·추세 필터를 두고 진입 시점만
무작위로 바꿔도 비슷한 성적이 나오면, 그 성적은 규칙이 아니라 드리프트·변동성의 산물이다.

사용 예
    python -m backend.verdict --json data/btc_15m_45000.json --tf 15 \
      --base-p '{"stop_mode":"atr","stop_atr":3.0,"use_htf_trend":true,"htf_ema_len":200,
                 "ds_htf_filter":true,"allow_sides":["LONG"]}' \
      --base-c '{"target_mode":"r","target_r":4.0,"max_bars":96,"session_filter":"asia"}' \
      --placebo 100
"""

import argparse
import json
import random
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from .backtest import BacktestConfig, simulate, summarize
from .strategy import Engine, Params, resample

BASE_C = dict(fee_bps=5.0, slippage_bps=2.0, risk_pct=1.0, initial_equity=10_000.0)


def _load(path: str) -> List[Dict]:
    candles = json.load(open(path, encoding="utf-8"))
    candles.sort(key=lambda c: c["t"])
    return candles


def _month(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m")


def _quarter(ms: int) -> str:
    d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def bootstrap(trades: Sequence[Dict], reps: int = 2000, seed: int = 7) -> Dict:
    """거래 R의 평균에 대한 부트스트랩 신뢰구간 — 표본이 작을 때 '플러스'의 불확실성을 본다."""
    rs = [t["r"] for t in trades]
    if not rs:
        return {"n": 0}
    rnd = random.Random(seed)
    n = len(rs)
    means = sorted(sum(rnd.choice(rs) for _ in range(n)) / n for _ in range(reps))
    return {
        "n": n,
        "mean_r": round(sum(rs) / n, 3),
        "ci95_low": round(means[int(0.025 * reps)], 3),
        "ci95_high": round(means[min(reps - 1, int(0.975 * reps))], 3),
        "p_mean_positive": round(sum(1 for m in means if m > 0) / reps, 3),
    }


def placebo(candles: Sequence[Dict], signals: Sequence[Dict], p: Params,
            cfg: BacktestConfig, reps: int = 100, seed: int = 11,
            split: float = 0.6) -> Dict:
    """플라시보 — 신호의 시각만 무작위로 옮기고 나머지(방향·손절 배수·목표)는 그대로 둔다.

    같은 규칙·같은 비용으로 진입 시점만 바꿨을 때의 성적 분포. 실제 성적이 이 분포의
    상위 5% 밖에 있어야 "그 시각에 진입하는 것"이 값을 한다고 말할 수 있다.
    """
    rnd = random.Random(seed)
    n = len(candles)
    if not signals:
        return {"reps": 0}
    if cfg.target_mode != "r":
        # 신호 목표(target_mode=signal)는 원래 봉의 가격이라 시각을 옮기면 의미가 깨진다.
        # 플라시보는 반드시 R 목표(+ATR 손절)에서만 쓴다.
        return {"reps": 0, "skipped": "target_mode=signal이라 플라시보를 계산하지 않음"}
    split_t = candles[int(n * split)]["t"]
    real = simulate(candles, signals, cfg, p)
    real_tr = real["trades"]
    real_is = sum(t["pnl"] for t in real_tr if t["entry_time"] < split_t)
    real_os = sum(t["pnl"] for t in real_tr if t["entry_time"] >= split_t)
    k = min(len(signals), n - 2)
    outs = []
    for _ in range(reps):
        idxs = rnd.sample(range(0, n - 1), k)
        fake = [{**s, "index": idx, "time": candles[idx]["t"]} for s, idx in zip(signals, idxs)]
        res = simulate(candles, fake, cfg, p)
        tr = res["trades"]
        outs.append({
            "net": res["stats"]["return_pct"],
            "is": round(sum(t["pnl"] for t in tr if t["entry_time"] < split_t)
                        / cfg.initial_equity * 100, 2),
            "os": round(sum(t["pnl"] for t in tr if t["entry_time"] >= split_t)
                        / cfg.initial_equity * 100, 2),
        })
    nets = sorted(o["net"] for o in outs)
    oss = sorted(o["os"] for o in outs)
    real_net = real["stats"]["return_pct"]
    pct = sum(1 for x in nets if x < real_net) / len(nets)
    return {
        "reps": reps,
        "real_net_pct": real_net,
        "placebo_median_net_pct": round(nets[len(nets) // 2], 2),
        "placebo_p95_net_pct": round(nets[int(0.95 * len(nets))], 2),
        "real_percentile": round(pct * 100, 1),
        "real_is": round(real_is / cfg.initial_equity * 100, 2),
        "real_os": round(real_os / cfg.initial_equity * 100, 2),
        "placebo_median_os_pct": round(oss[len(oss) // 2], 2),
        "placebo_p95_os_pct": round(oss[int(0.95 * len(oss))], 2),
    }


def _line(label: str, val) -> str:
    return f"  {label:<10s} {val}"


def report(candles: Sequence[Dict], p: Params, cfg: BacktestConfig,
           placebo_reps: int = 100, label: str = "후보") -> Dict:
    signals = Engine(p).run(candles)
    res = simulate(candles, signals, cfg, p)
    trades = res["trades"]
    s = res["stats"]
    n = len(candles)
    out: List[str] = [f"■ {label} — {n}봉 · 신호 {len(signals)}개 · 체결 {s['trades']}건"]
    out.append(_line("승률", f"{s['win_rate_pct']}%"))
    out.append(_line("PF", f"{s['profit_factor']}"))
    out.append(_line("순손익", f"{s['net_pnl']:,.0f} USDT ({s['return_pct']}%)"))
    out.append(_line("MDD", f"{s['max_drawdown_pct']}%"))
    out.append(_line("평균 R", f"{s['avg_r']} (건당 비용 {s['avg_cost_r']}R)"))
    out.append(_line("평균 보유", f"{s['avg_bars_held']}봉"))
    out.append(_line("방향", f"롱 {s['by_side']['LONG']['trades']}건 {s['by_side']['LONG']['net_pnl']:,.0f}$ · "
                             f"숏 {s['by_side']['SHORT']['trades']}건 {s['by_side']['SHORT']['net_pnl']:,.0f}$"))
    reasons: Dict[str, int] = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    out.append(_line("청산 사유", ", ".join(f"{k} {v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1]))))

    out.append("\n  [분할 지점별]")
    for split in (0.5, 0.6, 0.7):
        st = candles[int(n * split)]["t"]
        is_t = [t for t in trades if t["entry_time"] < st]
        os_t = [t for t in trades if t["entry_time"] >= st]
        a = summarize(is_t, cfg, cfg.initial_equity)
        b = summarize(os_t, cfg, cfg.initial_equity)
        ia = sum(t["pnl"] for t in is_t) / cfg.initial_equity * 100
        ob = sum(t["pnl"] for t in os_t) / cfg.initial_equity * 100
        out.append(f"    {int(split * 100)}/{100 - int(split * 100)}  "
                   f"IS {len(is_t):>3d}건 {ia:+6.2f}% PF {a['profit_factor']}  |  "
                   f"OS {len(os_t):>3d}건 {ob:+6.2f}% PF {b['profit_factor']}")

    q = {}
    for t in trades:
        k = _quarter(t["entry_time"])
        b = q.setdefault(k, {"n": 0, "pnl": 0.0})
        b["n"] += 1
        b["pnl"] += t["pnl"]
    out.append("\n  [분기별 순손익]")
    out.append("    " + " · ".join(f"{k} {v['pnl'] / cfg.initial_equity * 100:+.1f}%({v['n']})"
                                   for k, v in sorted(q.items())))
    m = {}
    for t in trades:
        k = _month(t["entry_time"])
        b = m.setdefault(k, {"n": 0, "pnl": 0.0})
        b["n"] += 1
        b["pnl"] += t["pnl"]
    out.append("  [월별 순손익]")
    out.append("    " + " · ".join(f"{k[2:]} {v['pnl'] / cfg.initial_equity * 100:+.1f}%"
                                   for k, v in sorted(m.items())))

    out.append("\n  [비용 민감도 — 편도 수수료]")
    for fee in (0.0, 2.0, 5.0, 7.0):
        c2 = BacktestConfig(**{**cfg.to_dict(), "fee_bps": fee})
        r2 = simulate(candles, signals, c2, p)
        out.append(f"    {fee:>4.1f}bp  체결 {r2['stats']['trades']:>3d}  PF {r2['stats']['profit_factor']}  "
                   f"순손익 {r2['stats']['return_pct']:+.1f}%")

    split_t = candles[int(n * 0.6)]["t"]
    os_trades = [t for t in trades if t["entry_time"] >= split_t]
    bs_all = bootstrap(trades)
    bs_os = bootstrap(os_trades)
    out.append("\n  [부트스트랩 (평균 R 95% 신뢰구간)]")
    out.append(f"    전체 {bs_all['mean_r']:+.3f}  [{bs_all['ci95_low']:+.3f}, {bs_all['ci95_high']:+.3f}]  "
               f"P(평균>0)={bs_all['p_mean_positive']}")
    if bs_os.get("n"):
        out.append(f"    아웃샘플 {bs_os['mean_r']:+.3f}  [{bs_os['ci95_low']:+.3f}, {bs_os['ci95_high']:+.3f}]  "
                   f"P(평균>0)={bs_os['p_mean_positive']}")

    pl = None
    if placebo_reps:
        pl = placebo(candles, signals, p, cfg, reps=placebo_reps)
        if not pl.get("reps"):
            out.append(f"\n  [플라시보] 생략 — {pl.get('skipped', '계산 불가')}")
            print("\n".join(out))
            return {"stats": s, "bootstrap_all": bs_all, "bootstrap_os": bs_os, "placebo": pl,
                    "monthly": m, "quarterly": q, "reasons": reasons}
        out.append("\n  [플라시보 — 진입 시각만 무작위로 옮긴 대조군]")
        out.append(f"    실제 전체 {pl['real_net_pct']:+.2f}% (대조군 중앙 {pl['placebo_median_net_pct']:+.2f}%, "
                   f"상위5% {pl['placebo_p95_net_pct']:+.2f}%) → 백분위 {pl['real_percentile']}")
        out.append(f"    실제 아웃샘플 {pl['real_os']:+.2f}% (대조군 중앙 {pl['placebo_median_os_pct']:+.2f}%, "
                   f"상위5% {pl['placebo_p95_os_pct']:+.2f}%)")
    print("\n".join(out))
    return {"stats": s, "bootstrap_all": bs_all, "bootstrap_os": bs_os, "placebo": pl,
            "monthly": m, "quarterly": q, "reasons": reasons}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="후보 조합 심층 검증")
    ap.add_argument("--json", required=True)
    ap.add_argument("--tf", type=int, default=15, choices=[15, 30, 60], help="15분봉 데이터를 이 봉으로 리샘플")
    ap.add_argument("--native", action="store_true", help="JSON을 그 봉 그대로 쓴다(리샘플 안 함)")
    ap.add_argument("--base-p", default="{}")
    ap.add_argument("--base-c", default="{}")
    ap.add_argument("--placebo", type=int, default=100)
    ap.add_argument("--label", default="후보")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args(argv)
    candles = _load(args.json)
    if args.tf != 15 and not args.native:
        candles = resample(candles, args.tf)
    p = Params(**json.loads(args.base_p))
    cfg = BacktestConfig(**{**BASE_C, **json.loads(args.base_c)})
    rep = report(candles, p, cfg, args.placebo, args.label)
    if args.json_out:
        json.dump(rep, open(args.json_out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
