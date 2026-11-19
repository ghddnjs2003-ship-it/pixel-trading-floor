"""구조 실험실 — 손절·목표·청산·세션·시간봉을 격자로 돌려 인샘플/아웃샘플을 함께 본다.

`explore.py`가 사람이 읽는 조합 표라면, 이 파일은 **실험 설계**용이다.
축(axis)을 격자로 곱해 수백 조합을 돌리고, 인샘플(앞 60%)에서 고른 것이 아웃샘플(뒤 40%)에서
살아남는지만 본다. 조합을 많이 돌릴수록 하나쯤 잘 나오는 것은 당연하므로,
보고는 ① 인샘플 선택 → ② 아웃샘플 확인 → ③ 월별 분포 순서로만 읽는다.

사용 예
    python -m backend.lab --json data/btc_15m_45000.json --tf 15 --preset core
    python -m backend.lab --json data/btc_15m_45000.json --tf 60 --preset core
    python -m backend.lab --json data/btc_15m_45000.json --preset filter --json-out out/filter.json

주의
- 체결·비용 가정(수수료 5bp·슬리피지 2bp·손절 우선·다음 봉 시가)은 `backtest.simulate` 하나만 쓴다.
- 표본이 작은 조합(인샘플 15건 미만)은 후보에서 뺀다.
"""

import argparse
import itertools
import json
import os
from typing import Dict, List, Optional, Sequence

from .backtest import BacktestConfig, load_candles, simulate, summarize
from .strategy import Engine, Params, resample

BASE_P = dict(min_rr=1.5, stop_buffer_atr=0.15, m1_stop_buffer_atr=0.0)
BASE_C = dict(exit_mode="bracket", target_mode="signal", fee_bps=5.0, slippage_bps=2.0,
              risk_pct=1.0, initial_equity=10_000.0)


def _key(d: Dict) -> str:
    return json.dumps(d, sort_keys=True, ensure_ascii=False, default=str)


def build(axes: Dict[str, List[Dict]]) -> List[Dict]:
    """축 이름 → [{label, p, c}] 격자의 데카르트 곱. 잡은 라벨을 이름으로 붙인다."""
    names = list(axes)
    out: List[Dict] = []
    for combo in itertools.product(*[axes[n] for n in names]):
        p: Dict = {}
        c: Dict = {}
        label = []
        for item in combo:
            p.update(item.get("p") or {})
            c.update(item.get("c") or {})
            label.append(item["label"])
        out.append({"name": " | ".join(label), "p": p, "c": c})
    return out


def run_rows(candles: Sequence[Dict], specs: Sequence[Dict], split: float = 0.6,
             base_p: Optional[Dict] = None, base_c: Optional[Dict] = None) -> List[Dict]:
    split_t = candles[int(len(candles) * split)]["t"]
    cache: Dict[str, List[Dict]] = {}
    rows: List[Dict] = []
    for spec in specs:
        p = Params(**{**BASE_P, **(base_p or {}), **spec["p"]})
        c = {**BASE_C, **(base_c or {}), **spec["c"]}
        cfg = BacktestConfig(**c)
        pk = _key(p.to_dict())
        if pk not in cache:
            cache[pk] = Engine(p).run(candles)
        res = simulate(candles, cache[pk], cfg, p)
        trades = res["trades"]
        s = res["stats"]
        is_t = [t for t in trades if t["entry_time"] < split_t]
        os_t = [t for t in trades if t["entry_time"] >= split_t]
        is_s = summarize(is_t, cfg, cfg.initial_equity)
        os_s = summarize(os_t, cfg, cfg.initial_equity)
        is_net = round(sum(t["pnl"] for t in is_t) / cfg.initial_equity * 100, 2)
        os_net = round(sum(t["pnl"] for t in os_t) / cfg.initial_equity * 100, 2)
        os_pos_months = sum(1 for m in os_s["monthly"] if m["net_pnl"] > 0)
        rows.append({
            "name": spec["name"], "p": p.to_dict(), "c": cfg.to_dict(),
            "trades": s["trades"], "win": s["win_rate_pct"], "pf": s["profit_factor"],
            "net": s["return_pct"], "mdd": s["max_drawdown_pct"], "avg_r": s["avg_r"],
            "cost_r": s["avg_cost_r"], "bars": s["avg_bars_held"],
            "is_trades": len(is_t), "is_net": is_net, "is_pf": is_s["profit_factor"],
            "is_win": is_s["win_rate_pct"],
            "os_trades": len(os_t), "os_net": os_net, "os_pf": os_s["profit_factor"],
            "os_win": os_s["win_rate_pct"],
            "os_months": len(os_s["monthly"]), "os_pos_months": os_pos_months,
            "os_long": os_s["by_side"]["LONG"]["net_pnl"], "os_short": os_s["by_side"]["SHORT"]["net_pnl"],
            "ds": s["by_kind"]["DS"]["trades"], "m1": s["by_kind"]["M1"]["trades"],
        })
    return rows


def _f(v, spec="{:.2f}"):
    return "-" if v is None else (spec.format(v) if isinstance(v, (int, float)) else str(v))


def table(rows: Sequence[Dict], limit: int = 40) -> str:
    head = (f"{'조합':<52s} {'체결':>4s} {'승률':>5s} {'PF':>5s} {'전체%':>7s} {'MDD':>6s} "
            f"{'IS건':>4s} {'IS%':>7s} {'IS PF':>5s} {'OS건':>4s} {'OS%':>7s} {'OS PF':>5s} {'OS월+':>5s}")
    out = [head]
    for r in rows[:limit]:
        out.append(f"{r['name'][:52]:<52s} {r['trades']:>4d} {_f(r['win'], '{:.1f}'):>5s} {_f(r['pf']):>5s} "
                   f"{_f(r['net']):>7s} {_f(r['mdd'], '{:.1f}'):>6s} "
                   f"{r['is_trades']:>4d} {_f(r['is_net']):>7s} {_f(r['is_pf']):>5s} "
                   f"{r['os_trades']:>4d} {_f(r['os_net']):>7s} {_f(r['os_pf']):>5s} "
                   f"{r['os_pos_months']}/{r['os_months']}")
    return "\n".join(out)


def candidates(rows: Sequence[Dict], min_is: int = 15, min_os: int = 15) -> List[Dict]:
    """인샘플·아웃샘플 모두 플러스인 조합 — 본선 후보."""
    ok = [r for r in rows if r["is_trades"] >= min_is and r["os_trades"] >= min_os
          and r["is_net"] > 0 and r["os_net"] > 0 and (r["pf"] or 0) > 1.0]
    return sorted(ok, key=lambda r: min(r["is_net"], r["os_net"]), reverse=True)


def top_by(rows: Sequence[Dict], key: str, limit: int = 10, min_is: int = 15) -> List[Dict]:
    pool = [r for r in rows if r["is_trades"] >= min_is]
    pool = [r for r in pool if r.get(key) is not None]
    return sorted(pool, key=lambda r: r[key], reverse=True)[:limit]


# ----------------------------------------------------------------------------- 프리셋

def preset_core() -> List[Dict]:
    """손절 × 목표 × 청산 — 신호 종류별로 넓게. 세션·상위추세는 아직 건다."""
    axes = {
        "kind": [
            {"label": "통합", "c": {"allow_kinds": ("DS", "M1")}},
            {"label": "더블S", "c": {"allow_kinds": ("DS",)}},
            {"label": "M1", "c": {"allow_kinds": ("M1",)}},
        ],
        "stop": [
            {"label": "구조손절", "p": {"stop_mode": "structure"}},
            {"label": "1ATR", "p": {"stop_mode": "atr", "stop_atr": 1.0}},
            {"label": "1.5ATR", "p": {"stop_mode": "atr", "stop_atr": 1.5}},
            {"label": "2ATR", "p": {"stop_mode": "atr", "stop_atr": 2.0}},
            {"label": "3ATR", "p": {"stop_mode": "atr", "stop_atr": 3.0}},
        ],
        "target": [
            {"label": "구조목표", "c": {"target_mode": "signal"}},
            {"label": "1R", "c": {"target_mode": "r", "target_r": 1.0}},
            {"label": "1.5R", "c": {"target_mode": "r", "target_r": 1.5}},
            {"label": "2R", "c": {"target_mode": "r", "target_r": 2.0}},
            {"label": "3R", "c": {"target_mode": "r", "target_r": 3.0}},
            {"label": "4R", "c": {"target_mode": "r", "target_r": 4.0}},
        ],
        "exit": [
            {"label": "목표청산", "c": {"exit_mode": "bracket"}},
            {"label": "추적2ATR", "c": {"exit_mode": "trail", "trail_atr": 2.0}},
        ],
    }
    return build(axes)


def preset_filter() -> List[Dict]:
    """세션·상위추세·시간손절·신호선별 — 손절/목표는 고정된 기본형 위에서."""
    axes = {
        "kind": [
            {"label": "통합", "c": {"allow_kinds": ("DS", "M1")}},
            {"label": "더블S", "c": {"allow_kinds": ("DS",)}},
            {"label": "M1", "c": {"allow_kinds": ("M1",)}},
        ],
        "session": [
            {"label": "전일", "c": {"session_filter": "off"}},
            {"label": "런던+뉴욕", "c": {"session_filter": "london_ny"}},
            {"label": "뉴욕", "c": {"session_filter": "ny"}},
            {"label": "아시아", "c": {"session_filter": "asia"}},
        ],
        "htf": [
            {"label": "상위off", "p": {}},
            {"label": "4H200", "p": {"use_htf_trend": True, "htf_ema_len": 200, "ds_htf_filter": True}},
        ],
        "time": [
            {"label": "시간무제한", "c": {"max_bars": 0}},
            {"label": "24봉청산", "c": {"max_bars": 24}},
        ],
        "label": [
            {"label": "거부+복귀", "p": {"ds_labels": ("거부", "복귀")}},
            {"label": "거부만", "p": {"ds_labels": ("거부",)}},
        ],
    }
    return build(axes)


def preset_refine() -> List[Dict]:
    """기본형(손절·목표·청산은 --base로 고정) 위에 선별·세션·상위추세·시간·방향만 곱한다."""
    axes = {
        "kind": [
            {"label": "통합", "c": {"allow_kinds": ("DS", "M1")}},
            {"label": "더블S", "c": {"allow_kinds": ("DS",)}},
            {"label": "M1", "c": {"allow_kinds": ("M1",)}},
        ],
        "session": [
            {"label": "전일", "c": {"session_filter": "off"}},
            {"label": "런던+뉴욕", "c": {"session_filter": "london_ny"}},
            {"label": "뉴욕", "c": {"session_filter": "ny"}},
            {"label": "아시아", "c": {"session_filter": "asia"}},
            {"label": "런던밖", "c": {"session_filter": "outside_ln"}},
        ],
        "htf": [
            {"label": "상위off", "p": {}},
            {"label": "4H200동조", "p": {"use_htf_trend": True, "htf_ema_len": 200, "ds_htf_filter": True}},
        ],
        "label": [
            {"label": "거부+복귀", "p": {"ds_labels": ("거부", "복귀")}},
            {"label": "거부만", "p": {"ds_labels": ("거부",)}},
        ],
        "time": [
            {"label": "시간무제한", "c": {"max_bars": 0}},
            {"label": "48봉청산", "c": {"max_bars": 48}},
            {"label": "96봉청산", "c": {"max_bars": 96}},
        ],
        "side": [
            {"label": "양방향", "p": {"allow_sides": ("LONG", "SHORT")}},
            {"label": "롱만", "p": {"allow_sides": ("LONG",)}},
            {"label": "숏만", "p": {"allow_sides": ("SHORT",)}},
        ],
    }
    return build(axes)


def preset_sens(base_p: Dict, base_c: Dict) -> List[Dict]:
    """민감도 — 기준 조합에서 축을 하나씩만 바꿔 본다(날카로운 파라미터가 아닌지 확인).

    여러 축을 동시에 바꾸지 않으므로 "어느 축이 성과를 만들었는지"를 읽을 수 있다.
    """
    specs: List[Dict] = []

    def add(name: str, p: Optional[Dict] = None, c: Optional[Dict] = None) -> None:
        specs.append({"name": name, "p": {**base_p, **(p or {})}, "c": {**base_c, **(c or {})}})

    add("기준")
    for v in (1.5, 2.0, 2.5, 3.0, 4.0, 5.0):
        add(f"손절 {v}ATR", p={"stop_atr": v})
    for v in (2.0, 3.0, 4.0, 5.0, 6.0):
        add(f"목표 {v}R", c={"target_mode": "r", "target_r": v})
    for v in (0, 24, 48, 96, 192, 384):
        add(f"시간손절 {v}봉", c={"max_bars": v})
    for s in ("off", "asia", "london", "ny", "london_ny", "outside_ln"):
        add(f"세션 {s}", c={"session_filter": s})
    for h0, h1 in ((0, 4), (4, 8), (8, 12), (12, 16), (16, 20), (20, 24)):
        add(f"UTC {h0:02d}-{h1:02d}시", c={"sessions": ((h0, h1),)})
    add("상위추세 off", p={"use_htf_trend": False, "ds_htf_filter": False})
    add("4H EMA50 동조", p={"use_htf_trend": True, "htf_ema_len": 50})
    add("4H EMA200 동조", p={"use_htf_trend": True, "htf_ema_len": 200})
    add("4H200 두 신호", p={"use_htf_trend": True, "htf_ema_len": 200, "ds_htf_filter": True})
    add("세션 00-12시", c={"sessions": ((0, 12),)})
    add("세션 00-10시", c={"sessions": ((0, 10),)})
    add("세션 04-12시", c={"sessions": ((4, 12),)})
    add("세션 00-08 + 08-12", c={"session_filter": "off", "sessions": ((0, 8), (8, 12))})
    for sides in (("LONG", "SHORT"), ("LONG",), ("SHORT",)):
        add("방향 " + "+".join(sides), p={"allow_sides": sides})
    for kinds in (("DS", "M1"), ("DS",), ("M1",)):
        add("종류 " + "+".join(kinds), c={"allow_kinds": kinds})
    add("추적 2ATR", c={"exit_mode": "trail", "trail_atr": 2.0})
    add("추적 3ATR", c={"exit_mode": "trail", "trail_atr": 3.0})
    add("반익+추적", c={"exit_mode": "half_trail", "trail_atr": 2.0})
    add("본전+추적", c={"exit_mode": "be_trail", "trail_atr": 2.0})
    return specs


def preset_short_tf(tf: int = 5) -> List[Dict]:
    """짧은 호흡(5·15분) 전용 격자 — 수수료 벽을 먼저 넘는 축부터 본다.

    5·15분봉은 왕복 14bp가 손절 폭의 큰 몫이라 **좁은 손절(1~2ATR)은 후보에서 뺀다**
    (README 구조 실험 (1): 1ATR이면 건당 0.7R을 비용으로 낸다). 그래서 손절 하한을 5분 4ATR·15분 2ATR로 두고,
    세션(유동성 구간)·신호 종류·청산 방식·4H 추세 동조를 함께 곱한다.
    """
    stops = ("4", "6", "8", "12") if tf <= 5 else ("2", "3", "4", "6")
    axes = {
        "kind": [
            {"label": "통합", "c": {"allow_kinds": ("DS", "M1")}},
            {"label": "더블S", "c": {"allow_kinds": ("DS",)}},
            {"label": "M1", "c": {"allow_kinds": ("M1",)}},
        ],
        "stop": [{"label": f"{s}ATR", "p": {"stop_mode": "atr", "stop_atr": float(s)}} for s in stops],
        "exit": [
            {"label": "2R", "c": {"target_mode": "r", "target_r": 2.0}},
            {"label": "3R", "c": {"target_mode": "r", "target_r": 3.0}},
            {"label": "4R", "c": {"target_mode": "r", "target_r": 4.0}},
            {"label": "6R", "c": {"target_mode": "r", "target_r": 6.0}},
            {"label": "추적3ATR", "c": {"exit_mode": "trail", "trail_atr": 3.0}},
            {"label": "반익+추적3ATR", "c": {"exit_mode": "half_trail", "trail_atr": 3.0}},
        ],
        "session": [
            {"label": "전일", "c": {"session_filter": "off"}},
            {"label": "런던", "c": {"session_filter": "london"}},
            {"label": "뉴욕", "c": {"session_filter": "ny"}},
            {"label": "런던+뉴욕", "c": {"session_filter": "london_ny"}},
            {"label": "뉴욕개장", "c": {"session_filter": "ny_open"}},
            {"label": "아시아", "c": {"session_filter": "asia"}},
        ],
        "htf": [
            {"label": "상위off", "p": {}},
            {"label": "4H50동조", "p": {"use_htf_trend": True, "htf_ema_len": 50, "ds_htf_filter": True}},
            {"label": "4H200동조", "p": {"use_htf_trend": True, "htf_ema_len": 200, "ds_htf_filter": True}},
        ],
    }
    return build(axes)


def preset_long_tf() -> List[Dict]:
    """긴 호흡(1·4시간) 전용 격자 — 목표 R을 넓게, 시간 손절·추적 청산을 함께 본다.

    구조 목표(`signal`: 반대편 유동성·1H 매물대)를 기준선으로 두고 1.5~6R, 추적 2종, 시간 손절을 곱한다.
    """
    exits = [{"label": "목표청산", "c": {"target_mode": "signal"}}]
    exits += [{"label": f"{r}R", "c": {"target_mode": "r", "target_r": r}} for r in (1.5, 2.0, 3.0, 4.0, 6.0)]
    exits += [
        {"label": "추적3ATR", "c": {"exit_mode": "trail", "trail_atr": 3.0}},
        {"label": "반익+추적3ATR", "c": {"exit_mode": "half_trail", "trail_atr": 3.0}},
        {"label": "본전+추적3ATR", "c": {"exit_mode": "be_trail", "trail_atr": 3.0}},
    ]
    axes = {
        "kind": [
            {"label": "통합", "c": {"allow_kinds": ("DS", "M1")}},
            {"label": "더블S", "c": {"allow_kinds": ("DS",)}},
            {"label": "M1", "c": {"allow_kinds": ("M1",)}},
        ],
        "stop": [
            {"label": "구조손절", "p": {"stop_mode": "structure"}},
            {"label": "2ATR", "p": {"stop_mode": "atr", "stop_atr": 2.0}},
            {"label": "3ATR", "p": {"stop_mode": "atr", "stop_atr": 3.0}},
            {"label": "4ATR", "p": {"stop_mode": "atr", "stop_atr": 4.0}},
        ],
        "exit": exits,
        "time": [
            {"label": "시간무제한", "c": {"max_bars": 0}},
            {"label": "24봉청산", "c": {"max_bars": 24}},
            {"label": "96봉청산", "c": {"max_bars": 96}},
        ],
    }
    return build(axes)


def preset_nominee() -> List[Dict]:
    """2026-11-19 구조 실험에서 남긴 후보 — "지금 유일하게 살아남은 두 칸"을 같은 틀에서 다시 본다.

    둘 다 아웃샘플 플러스이지만 재현 범위가 좁다(README '구조 재설계 실험' 참고).
    """
    return [
        {"name": "후보1 30분 M1 아시아(00-08 UTC) 3ATR 4R 96봉",
         "p": {"stop_mode": "atr", "stop_atr": 3.0},
         "c": {"allow_kinds": ("M1",), "target_mode": "r", "target_r": 4.0,
               "max_bars": 96, "session_filter": "asia"}},
        {"name": "후보2 (15분 전용) M1 변동성>=0.3% 3ATR 구조목표",
         "p": {"stop_mode": "atr", "stop_atr": 3.0, "min_atr_pct": 0.3},
         "c": {"allow_kinds": ("M1",), "target_mode": "signal"}},
        {"name": "대조군 기준선 15분 통합 구조손절 구조목표",
         "p": {}, "c": {}},
    ]


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="v7 구조 실험실")
    ap.add_argument("--json", default=None, help="15분봉 캔들 JSON")
    ap.add_argument("--source", default="bitget")
    ap.add_argument("--limit", type=int, default=30000)
    ap.add_argument("--tf", type=int, default=15, choices=[5, 15, 30, 60, 240], help="기준 시간봉(분)")
    ap.add_argument("--native", action="store_true", help="JSON을 그 봉 그대로 쓴다(리샘플 안 함)")
    ap.add_argument("--box-tf", type=int, default=0,
                    help="매물대 존을 만드는 상위 봉(분). 0이면 자동(min(기준봉,60) 이상: 4시간봉은 240)")
    ap.add_argument("--split", type=float, default=0.6)
    ap.add_argument("--preset", default="core",
                    choices=["core", "filter", "refine", "sens", "nominee", "short", "long"])
    ap.add_argument("--base-p", default=None, help="기본 Params 덮어쓰기(JSON 문자열)")
    ap.add_argument("--base-c", default=None, help="기본 BacktestConfig 덮어쓰기(JSON 문자열)")
    ap.add_argument("--order", default="sorted", choices=["sorted", "given"], help="출력 순서")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args(argv)

    candles = (_load(args.json) if args.json
               else load_candles("BTCUSDT", "15", args.limit, args.source)["candles"])
    if args.tf != 15 and not args.native:
        candles = resample(candles, args.tf)
    base_p = json.loads(args.base_p) if args.base_p else None
    base_c = json.loads(args.base_c) if args.base_c else None
    # 4시간봉을 기준봉으로 쓰면 1시간 리샘플이 불가능하다 — 존은 기준봉 몸통 피벗으로 만든다.
    box_tf = args.box_tf or (args.tf if args.tf >= 60 else 60)
    if args.tf != 60 or args.box_tf:
        base_p = {**(base_p or {}), "box_tf_minutes": box_tf}
    if args.preset == "sens":
        specs = preset_sens(base_p or {}, base_c or {})
    else:
        specs = {"core": preset_core, "filter": preset_filter, "refine": preset_refine,
                 "nominee": preset_nominee, "short": lambda: preset_short_tf(args.tf),
                 "long": preset_long_tf}[args.preset]()
    print(f"■ {len(candles)}봉 · {args.tf}분(존 {box_tf}분) · 조합 {len(specs)}종 · "
          f"인샘플 {int(len(candles) * args.split)}봉/아웃샘플 {len(candles) - int(len(candles) * args.split)}봉")
    rows = run_rows(candles, specs, args.split, base_p, base_c)
    if args.order == "given":
        print("\n■ 민감도(축을 하나씩만 바꿈)")
        print(table(rows, len(rows)))
    else:
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


def _load(path: str) -> List[Dict]:
    candles = json.load(open(path, encoding="utf-8"))
    candles.sort(key=lambda c: c["t"])
    return candles


if __name__ == "__main__":
    raise SystemExit(main())
