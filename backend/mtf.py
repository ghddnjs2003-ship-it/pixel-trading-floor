"""다중 시간봉(MTF) — 큰 봉은 방향·매물대, 작은 봉은 정밀 진입.

왜 이 파일이 있나
-----------------
`strategy.py`의 v7 엔진은 한 시간봉 위에서만 판정한다. 사람이 차트를 볼 때는 다르다.
**4시간봉으로 방향과 큰 매물대를 정하고, 5·15분봉으로 그 자리에 왔을 때의 트리거를 잡는다.**
이 파일은 그 두 층을 분리해 기계 규칙으로 옮기고, 인샘플/아웃샘플로만 판정한다.

구조
----
1. 상위 프레임(HTF: 60분 또는 240분) — **완성된 봉만** 본다.
   - `ema`      : 종가 > EMA(20) > EMA(50)이면 롱 편향, 반대면 숏 편향
   - `ema200`   : 위 조건 + 종가 > EMA(200) (장기 추세까지 같은 방향)
   - `breakout` : 종가가 직전 `breakout_lookback`봉 고가/저가를 깼으면 그 방향(반대 돌파까지 유지)
   - `zone`     : HTF 매물대(몸통 피벗 ± `zone_atr_mult`×HTF ATR)에 닿았다가 안쪽으로 마감한 방향
                  (지지/저항 확인) — 그 상태를 `zone_hold_bars`봉 동안 유지
   - `ema_zone` : `ema` 편향 + 가격이 그 방향의 HTF 매물대 `zone_near_atr` 안에 있음(눌림 자리)
2. 하위 프레임(LTF) — v7 엔진의 트리거(M1 리테스트 / 더블 S 스윕 거부)를 그대로 쓴다.
   HTF 편향과 반대 방향 신호는 버린다. `ltf_zone_dist_atr>0`이면 HTF 매물대에서 그 거리 안의 신호만 남긴다.
3. 손절·목표 — LTF ATR 배수 / HTF ATR 배수 / HTF 매물대 반대편 중에서 고른다.
   목표는 R 배수 또는 반대편 HTF 매물대.

시점 규칙(미래 참조 없음)
-------------------------
- LTF 봉 i가 끝나고 신호가 서고, 진입은 i+1봉 시가다(`backtest.simulate` 그대로).
- HTF 편향은 **봉 i의 시작 시각까지 완전히 끝난 HTF 봉**만 쓴다:
  `htf[k].t + htf_minutes <= candles[i].t`인 마지막 k. 진행 중인 HTF 봉은 보지 않는다.
- 그래서 HTF 지표(EMA·ATR·피벗)도 그 k봉의 값이다(`test_mtf.py`가 잘라 보기로 고정).

사용 예
    python -m backend.mtf --json data/btc_15m_45000.json --htf 240 --preset grid --top 20
    python -m backend.mtf --json data/btc_5m_105000.json --htf 60 --preset grid --json-out out/mtf_5m_1h.json
"""

import argparse
import json
import os
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .backtest import BacktestConfig, simulate, summarize
from .lab import candidates, table, top_by
from .strategy import Engine, Params, atr_series, ema_series, pivots, resample


# ----------------------------------------------------------------------------- 파라미터

@dataclass
class MTFParams:
    """상위 봉 규칙 + 하위 봉 손절·목표. `strategy.Params`(하위 신호)와 분리해 둔다."""

    htf_minutes: int = 240               # 60(1시간) | 240(4시간)
    htf_mode: str = "ema"                # ema | ema200 | breakout | zone | ema_zone
    htf_ema_fast: int = 20
    htf_ema_slow: int = 50
    htf_ema_len: int = 200
    htf_piv: int = 5                     # HTF 몸통 피벗 좌우 봉수
    zone_atr_mult: float = 0.30          # HTF ATR 배수 두께(v7 파인과 같은 0.30)
    zone_per_side: int = 3
    zone_max_bars: int = 60
    zone_near_atr: float = 1.0           # 가격이 존에서 이만큼(HTF ATR) 안이면 '눌림'
    zone_hold_bars: int = 3              # 존 지지/저항 확인 뒤 편향 유지 봉수(HTF)
    breakout_lookback: int = 20
    allow_kinds: Tuple[str, ...] = ("M1",)
    ltf_zone_dist_atr: float = 0.0       # 0=끔. LTF 신호가 HTF 존에서 이 거리(HTF ATR) 안
    stop_mode: str = "htf_atr"           # ltf_atr | htf_atr | htf_zone
    stop_atr: float = 2.0
    stop_buf_atr: float = 0.10           # htf_zone 손절을 존 바깥으로 밀어내는 폭
    target_mode: str = "r"               # r | htf_zone
    target_r: float = 4.0
    target_cap_r: float = 0.0            # >0이면 HTF 존 목표가 이 R보다 멀 때 자른다

    def to_dict(self) -> Dict:
        return asdict(self)


# ----------------------------------------------------------------------------- 상위 봉 상태

def htf_frame(candles: Sequence[Dict], mp: MTFParams) -> Dict:
    """LTF 봉마다 '그 시점에 완전히 끝난 HTF 봉'의 지표·편향·가까운 매물대를 미리 계산한다."""
    htf = resample(candles, mp.htf_minutes)
    tf_ms = mp.htf_minutes * 60_000
    closes = [c["c"] for c in htf]
    ema_fast = ema_series(closes, mp.htf_ema_fast)
    ema_slow = ema_series(closes, mp.htf_ema_slow)
    ema_long = ema_series(closes, mp.htf_ema_len) if mp.htf_ema_len else [None] * len(htf)
    atr = atr_series(htf)
    ph = pivots([max(x["o"], x["c"]) for x in htf], mp.htf_piv, mp.htf_piv, "high")
    pl = pivots([min(x["o"], x["c"]) for x in htf], mp.htf_piv, mp.htf_piv, "low")

    res_z: List[Dict] = []      # 저항(가격 위)
    sup_z: List[Dict] = []      # 지지(가격 아래)
    out_res: List[Optional[Dict]] = [None] * len(htf)
    out_sup: List[Optional[Dict]] = [None] * len(htf)
    zone_dir: List[int] = [0] * len(htf)      # 그 봉에서 존 지지/저항이 확인된 방향
    break_dir: List[int] = [0] * len(htf)

    for k, c in enumerate(htf):
        a = atr[k]
        if ph[k] is not None and a:
            mid = ph[k]
            res_z.append({"top": mid + mp.zone_atr_mult * a, "bot": mid - mp.zone_atr_mult * a, "born": k})
        if pl[k] is not None and a:
            mid = pl[k]
            sup_z.append({"top": mid + mp.zone_atr_mult * a, "bot": mid - mp.zone_atr_mult * a, "born": k})
        # 오래된 존·먼 존 정리(방향별 zone_per_side개)
        for arr in (res_z, sup_z):
            while len(arr) > mp.zone_per_side:
                wi = max(range(len(arr)), key=lambda z: abs((arr[z]["top"] + arr[z]["bot"]) / 2 - c["c"]))
                arr.pop(wi)
            for z in list(arr):
                if k - z["born"] > mp.zone_max_bars:
                    arr.remove(z)

        below = [z for z in sup_z if z["top"] <= c["c"]]
        above = [z for z in res_z if z["bot"] >= c["c"]]
        out_sup[k] = max(below, key=lambda z: z["top"]) if below else None
        out_res[k] = min(above, key=lambda z: z["bot"]) if above else None

        # 존 지지/저항 확인: 꼬리가 존에 닿고 몸통은 안쪽으로 마감
        if out_sup[k] and c["l"] <= out_sup[k]["top"] and c["c"] > out_sup[k]["top"]:
            zone_dir[k] = 1
        elif out_res[k] and c["h"] >= out_res[k]["bot"] and c["c"] < out_res[k]["bot"]:
            zone_dir[k] = -1

        # 구조 돌파 상태(직전 lookback봉 고가/저가)
        lo = max(0, k - mp.breakout_lookback)
        if k > 0:
            prev_high = max(x["h"] for x in htf[lo:k])
            prev_low = min(x["l"] for x in htf[lo:k])
            if c["c"] > prev_high:
                break_dir[k] = 1
            elif c["c"] < prev_low:
                break_dir[k] = -1
            else:
                break_dir[k] = break_dir[k - 1]

    # 편향
    bias: List[int] = [0] * len(htf)
    last_hold = {"dir": 0, "bar": -10 ** 9}
    last_break = break_dir[0] if break_dir else 0
    for k, c in enumerate(htf):
        if zone_dir[k]:
            last_hold = {"dir": zone_dir[k], "bar": k}
        if break_dir[k]:
            last_break = break_dir[k]
        up = ema_fast[k] is not None and ema_slow[k] is not None and c["c"] > ema_fast[k] > ema_slow[k]
        dn = ema_fast[k] is not None and ema_slow[k] is not None and c["c"] < ema_fast[k] < ema_slow[k]
        if mp.htf_mode == "ema":
            bias[k] = 1 if up else (-1 if dn else 0)
        elif mp.htf_mode == "ema200":
            long_ok = ema_long[k] is not None and c["c"] > ema_long[k]
            bias[k] = 1 if (up and long_ok) else (-1 if (dn and ema_long[k] is not None and c["c"] < ema_long[k]) else 0)
        elif mp.htf_mode == "breakout":
            bias[k] = last_break
        elif mp.htf_mode == "zone":
            bias[k] = last_hold["dir"] if k - last_hold["bar"] <= mp.zone_hold_bars else 0
        elif mp.htf_mode == "ema_zone":
            d = 1 if up else (-1 if dn else 0)
            z = out_sup[k] if d > 0 else (out_res[k] if d < 0 else None)
            near = (d != 0 and z is not None and atr[k] and
                    abs(c["c"] - (z["top"] if d > 0 else z["bot"])) <= mp.zone_near_atr * atr[k])
            bias[k] = d if near else 0
        else:
            raise ValueError(f"알 수 없는 htf_mode: {mp.htf_mode}")

    # LTF 봉 i → 그 시점까지 끝난 마지막 HTF 봉 k
    closed: List[int] = [-1] * len(candles)
    k2 = 0
    for i, c in enumerate(candles):
        while k2 < len(htf) and int(htf[k2]["t"]) + tf_ms <= int(c["t"]):
            k2 += 1
        closed[i] = k2 - 1

    return {"htf": htf, "closed": closed, "bias": bias, "atr": atr, "ema_fast": ema_fast,
            "ema_slow": ema_slow, "ema_long": ema_long, "sup": out_sup, "res": out_res}


# ----------------------------------------------------------------------------- 신호

def mtf_signals(candles: Sequence[Dict], mp: MTFParams,
                ltf_params: Optional[Params] = None,
                base: Optional[Sequence[Dict]] = None,
                frame: Optional[Dict] = None) -> List[Dict]:
    """LTF 엔진 신호 중 HTF 편향과 같은 방향만 남기고, 손절·목표를 MTF 규칙으로 바꾼다.

    `base`(LTF 엔진 신호)와 `frame`(HTF 상태)을 넘기면 다시 계산하지 않는다 — 격자에서 수백 번 부를 때 필요하다.
    """
    base = list(base) if base is not None else Engine(ltf_params or Params()).run(candles)
    fr = frame if frame is not None else htf_frame(candles, mp)
    out: List[Dict] = []
    for s in base:
        if s["kind"] not in mp.allow_kinds:
            continue
        i = int(s["index"])
        k = fr["closed"][i]
        if k < 1:
            continue
        b = fr["bias"][k]
        want = 1 if s["side"] == "LONG" else -1
        if b != want:
            continue
        sup, res = fr["sup"][k], fr["res"][k]
        hatr = fr["atr"][k]
        zone = sup if want > 0 else res
        if mp.ltf_zone_dist_atr > 0:
            if zone is None or not hatr:
                continue
            edge = zone["top"] if want > 0 else zone["bot"]
            if abs(candles[i]["c"] - edge) > mp.ltf_zone_dist_atr * hatr:
                continue
        sig = dict(s)
        sig["htf_bias"] = b
        sig["htf_time"] = fr["htf"][k]["t"]
        # 손절
        if mp.stop_mode == "ltf_atr":
            sig["stop_atr"] = mp.stop_atr
            sig.pop("stop_dist", None)
        elif mp.stop_mode == "htf_atr":
            if not hatr:
                continue
            sig["stop_atr"] = None
            sig["stop_dist"] = mp.stop_atr * hatr
            # 문맥·표시용 손절은 신호봉 종가 기준으로 적는다(실제 체결은 다음 봉 시가 기준 — simulate)
            e0 = float(sig["entry"])
            sig["stop"] = e0 - sig["stop_dist"] if want > 0 else e0 + sig["stop_dist"]
        elif mp.stop_mode == "htf_zone":
            if zone is None or not hatr:
                continue
            buf = mp.stop_buf_atr * hatr
            sig["stop_atr"] = None
            sig["stop_dist"] = None
            sig["stop"] = zone["bot"] - buf if want > 0 else zone["top"] + buf
        else:
            raise ValueError(f"알 수 없는 stop_mode: {mp.stop_mode}")
        # 목표
        if mp.target_mode == "htf_zone":
            opp = fr["res"][k] if want > 0 else fr["sup"][k]
            if opp is None:
                continue
            sig["target"] = opp["bot"] if want > 0 else opp["top"]
        out.append(sig)
    return out


def run_mtf(candles: Sequence[Dict], mp: MTFParams, cfg: BacktestConfig,
            ltf_params: Optional[Params] = None) -> Dict:
    sigs = mtf_signals(candles, mp, ltf_params)
    res = simulate(candles, sigs, cfg, ltf_params or Params())
    res["signals"] = sigs
    return res


FRAME_KEYS = ("htf_minutes", "htf_mode", "htf_ema_fast", "htf_ema_slow", "htf_ema_len",
              "htf_piv", "zone_atr_mult", "zone_per_side", "zone_max_bars", "zone_near_atr",
              "zone_hold_bars", "breakout_lookback")


def frame_key(mp: MTFParams) -> str:
    return json.dumps({k: getattr(mp, k) for k in FRAME_KEYS}, sort_keys=True, ensure_ascii=False)


# ----------------------------------------------------------------------------- 격자·표

BASE_C = dict(fee_bps=5.0, slippage_bps=2.0, risk_pct=1.0, initial_equity=10_000.0)


def preset_grid(ltf_minutes: int) -> List[Dict]:
    """HTF 규칙 × LTF 트리거 × 손절 × 목표 격자. 5분봉은 좁은 손절을 뺀다(수수료)."""
    ltf_stops = (3.0, 4.0) if ltf_minutes <= 5 else (2.0, 3.0, 4.0)
    axes_modes = ["ema", "ema200", "breakout", "zone", "ema_zone"]
    specs: List[Dict] = []
    for mode in axes_modes:
        for kinds in (("M1",), ("DS",), ("DS", "M1")):
            for stop_mode, stop_atr in ([("ltf_atr", v) for v in ltf_stops] +
                                        [("htf_atr", 1.0), ("htf_atr", 1.5), ("htf_zone", 0.0)]):
                for tgt in [("r", 3.0), ("r", 4.0), ("r", 6.0), ("htf_zone", 0.0)]:
                    label = (f"{mode} | {'+'.join(kinds)} | {stop_mode}"
                             f"{'' if stop_mode == 'htf_zone' else ' ' + str(stop_atr)} | "
                             f"{'HTF존' if tgt[0] == 'htf_zone' else str(tgt[1]) + 'R'}")
                    specs.append({
                        "name": label,
                        "mp": {"htf_mode": mode, "allow_kinds": list(kinds), "stop_mode": stop_mode,
                               "stop_atr": stop_atr, "target_mode": tgt[0], "target_r": tgt[1]},
                        "c": {"allow_kinds": kinds,
                              "target_mode": "signal" if tgt[0] == "htf_zone" else "r",
                              "target_r": tgt[1]},
                    })
    return specs


def run_rows(candles: Sequence[Dict], specs: Sequence[Dict], split: float = 0.6,
             ltf_minutes: int = 15, htf_minutes: int = 240,
             base_c: Optional[Dict] = None, ltf_params: Optional[Params] = None) -> List[Dict]:
    ltf = ltf_params or Params()
    base = Engine(ltf).run(candles)          # LTF 엔진은 한 번만
    frames: Dict[str, Dict] = {}
    sig_cache: Dict[str, List[Dict]] = {}
    split_t = candles[int(len(candles) * split)]["t"]
    rows: List[Dict] = []
    for spec in specs:
        mp = MTFParams(**{**spec["mp"], "htf_minutes": htf_minutes})
        cfg = BacktestConfig(**{**BASE_C, **(base_c or {}), **spec["c"]})
        fk = frame_key(mp)
        if fk not in frames:
            frames[fk] = htf_frame(candles, mp)
        pk = json.dumps(mp.to_dict(), sort_keys=True, ensure_ascii=False)
        if pk not in sig_cache:
            sig_cache[pk] = mtf_signals(candles, mp, ltf, base, frames[fk])
        res = simulate(candles, sig_cache[pk], cfg, ltf)
        trades, s = res["trades"], res["stats"]
        is_t = [t for t in trades if t["entry_time"] < split_t]
        os_t = [t for t in trades if t["entry_time"] >= split_t]
        is_s = summarize(is_t, cfg, cfg.initial_equity)
        os_s = summarize(os_t, cfg, cfg.initial_equity)
        net = lambda xs: round(sum(t["pnl"] for t in xs) / cfg.initial_equity * 100, 2)
        rows.append({
            "name": spec["name"], "mp": mp.to_dict(), "c": cfg.to_dict(),
            "trades": s["trades"], "win": s["win_rate_pct"], "pf": s["profit_factor"],
            "net": s["return_pct"], "mdd": s["max_drawdown_pct"], "avg_r": s["avg_r"],
            "cost_r": s["avg_cost_r"], "bars": s["avg_bars_held"],
            "is_trades": len(is_t), "is_net": net(is_t), "is_pf": is_s["profit_factor"],
            "is_win": is_s["win_rate_pct"],
            "os_trades": len(os_t), "os_net": net(os_t), "os_pf": os_s["profit_factor"],
            "os_win": os_s["win_rate_pct"],
            "os_months": len(os_s["monthly"]),
            "os_pos_months": sum(1 for m in os_s["monthly"] if m["net_pnl"] > 0),
            "os_long": os_s["by_side"]["LONG"]["net_pnl"], "os_short": os_s["by_side"]["SHORT"]["net_pnl"],
            "ds": s["by_kind"]["DS"]["trades"], "m1": s["by_kind"]["M1"]["trades"],
        })
    return rows


def report(candles: Sequence[Dict], mp: MTFParams, cfg: BacktestConfig,
           placebo_reps: int = 100, label: str = "MTF 후보",
           ltf_params: Optional[Params] = None) -> Dict:
    """MTF 후보 하나를 `verdict.py`와 같은 순서로 검증한다(분할·분기·비용·부트스트랩·플라시보)."""
    from .verdict import _month, _quarter, bootstrap, placebo

    sigs = mtf_signals(candles, mp, ltf_params)
    res = simulate(candles, sigs, cfg, ltf_params or Params())
    trades, s, n = res["trades"], res["stats"], len(candles)
    out = [f"■ {label} — {n}봉 · 신호 {len(sigs)}개 · 체결 {s['trades']}건",
           f"  승률 {s['win_rate_pct']}% · PF {s['profit_factor']} · 순손익 {s['net_pnl']:,.0f} USDT "
           f"({s['return_pct']}%) · MDD {s['max_drawdown_pct']}%",
           f"  평균 R {s['avg_r']} (건당 비용 {s['avg_cost_r']}R) · 평균 보유 {s['avg_bars_held']}봉",
           f"  방향 롱 {s['by_side']['LONG']['trades']}건 {s['by_side']['LONG']['net_pnl']:,.0f}$ · "
           f"숏 {s['by_side']['SHORT']['trades']}건 {s['by_side']['SHORT']['net_pnl']:,.0f}$"]
    reasons: Dict[str, int] = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    out.append("  청산 사유 " + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1])))
    out.append("\n  [분할 지점별]")
    for split in (0.5, 0.6, 0.7):
        st = candles[int(n * split)]["t"]
        a = [t for t in trades if t["entry_time"] < st]
        b = [t for t in trades if t["entry_time"] >= st]
        sa, sb = summarize(a, cfg, cfg.initial_equity), summarize(b, cfg, cfg.initial_equity)
        na = sum(t["pnl"] for t in a) / cfg.initial_equity * 100
        nb = sum(t["pnl"] for t in b) / cfg.initial_equity * 100
        out.append(f"    {int(split * 100)}/{100 - int(split * 100)}  IS {len(a):>3d}건 {na:+6.2f}% "
                   f"PF {sa['profit_factor']}  |  OS {len(b):>3d}건 {nb:+6.2f}% PF {sb['profit_factor']}")
    q: Dict[str, Dict] = {}
    for t in trades:
        k = _quarter(t["entry_time"])
        q.setdefault(k, {"n": 0, "pnl": 0.0})
        q[k]["n"] += 1
        q[k]["pnl"] += t["pnl"]
    out.append("\n  [분기별] " + " · ".join(f"{k} {v['pnl'] / cfg.initial_equity * 100:+.1f}%({v['n']})"
                                      for k, v in sorted(q.items())))
    m: Dict[str, Dict] = {}
    for t in trades:
        k = _month(t["entry_time"])
        m.setdefault(k, {"n": 0, "pnl": 0.0})
        m[k]["n"] += 1
        m[k]["pnl"] += t["pnl"]
    out.append("  [월별] " + " · ".join(f"{k[2:]} {v['pnl'] / cfg.initial_equity * 100:+.1f}%"
                                     for k, v in sorted(m.items())))
    out.append("\n  [비용 민감도 — 편도 수수료]")
    for fee in (0.0, 2.0, 5.0, 7.0):
        c2 = BacktestConfig(**{**cfg.to_dict(), "fee_bps": fee})
        r2 = simulate(candles, sigs, c2, ltf_params or Params())
        out.append(f"    {fee:>4.1f}bp  체결 {r2['stats']['trades']:>3d}  PF {r2['stats']['profit_factor']}  "
                   f"순손익 {r2['stats']['return_pct']:+.1f}%")
    bs_all, bs_os = bootstrap(trades), bootstrap([t for t in trades if t["entry_time"] >= candles[int(n * 0.6)]["t"]])
    out.append(f"\n  [부트스트랩] 전체 {bs_all['mean_r']:+.3f} [{bs_all['ci95_low']:+.3f}, {bs_all['ci95_high']:+.3f}] "
               f"P(평균>0)={bs_all['p_mean_positive']}")
    if bs_os.get("n"):
        out.append(f"    아웃샘플 {bs_os['mean_r']:+.3f} [{bs_os['ci95_low']:+.3f}, {bs_os['ci95_high']:+.3f}] "
                   f"P(평균>0)={bs_os['p_mean_positive']}")
    pl = placebo(candles, sigs, ltf_params or Params(), cfg, reps=placebo_reps) if placebo_reps else None
    if pl and pl.get("reps"):
        out.append(f"\n  [플라시보] 실제 {pl['real_net_pct']:+.2f}% (대조군 중앙 {pl['placebo_median_net_pct']:+.2f}%, "
                   f"상위5% {pl['placebo_p95_net_pct']:+.2f}%) → 백분위 {pl['real_percentile']}")
        out.append(f"    아웃샘플 실제 {pl['real_os']:+.2f}% (중앙 {pl['placebo_median_os_pct']:+.2f}%, "
                   f"상위5% {pl['placebo_p95_os_pct']:+.2f}%)")
    print("\n".join(out))
    return {"stats": s, "bootstrap_all": bs_all, "bootstrap_os": bs_os, "placebo": pl,
            "monthly": m, "quarterly": q, "reasons": reasons}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="MTF(상위 방향·매물대 + 하위 정밀 진입) 격자")
    ap.add_argument("--json", required=True)
    ap.add_argument("--htf", type=int, default=240, choices=[60, 240])
    ap.add_argument("--ltf", type=int, default=0, help="0이면 JSON 봉 그대로(5·15분 권장)")
    ap.add_argument("--preset", default="grid", choices=["grid"])
    ap.add_argument("--split", type=float, default=0.6)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args(argv)

    candles = json.load(open(args.json, encoding="utf-8"))
    candles.sort(key=lambda c: c["t"])
    ltf_minutes = args.ltf or 0
    if args.ltf and args.ltf != 15:
        candles = resample(candles, args.ltf)
    specs = preset_grid(ltf_minutes or 15)
    print(f"■ MTF {args.htf}분(상위) 위 {args.ltf or '원본'}분(하위) · {len(candles)}봉 · "
          f"조합 {len(specs)}종 · 인샘플 {int(len(candles) * args.split)}봉")
    rows = run_rows(candles, specs, args.split, ltf_minutes or 15, args.htf)
    print("\n■ 인샘플 순손익 상위")
    print(table(top_by(rows, "is_net", args.top), args.top))
    print("\n■ 아웃샘플 순손익 상위")
    print(table(top_by(rows, "os_net", args.top), args.top))
    cands = candidates(rows)
    print(f"\n■ 인샘플·아웃샘플 모두 플러스: {len(cands)}종")
    print(table(cands, 20) if cands else "  (없음)")
    if args.json_out:
        os.makedirs(os.path.dirname(args.json_out) or ".", exist_ok=True)
        json.dump(rows, open(args.json_out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"\n저장: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
