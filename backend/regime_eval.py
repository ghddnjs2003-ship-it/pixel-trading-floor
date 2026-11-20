"""국면·변동성 필터 실측 — 30분 M1 아시아 후보(아웃샘플 PF 2.02)의 성적을 깎는지 높이는지.

왜 따로 두나
- 국면 필터는 '그럴듯해 보이는' 규칙이라, 검증 없이 켜면 이미 검증된 후보의 성적을 깎는다.
- 그래서 여기서는 **이미 받아 둔 AI 심사 캐시**만 재생해 0회 호출로 비교한다(무료 한도 429 보호).
- 필터는 신호 목록에서 미리 빼고 체결 모형을 다시 돌린다(포지션 중복·다음 신호 진입까지 그대로 반영).

실행
    python3 -m backend.regime_eval --json data/btc_30m_60000.json --out data/regime_check_2026-11-20.json
"""

import argparse
import json
import os
import random
from typing import Dict, List, Optional, Sequence

from . import regime as regime_mod
from .ai_filter import (AIFilter, FilterConfig, _block, build_context, nominee_setup,
                        prepare_series, select_signals)
from .backtest import simulate
from .strategy import Engine


def cached_decisions(candles: Sequence[Dict], signals: Sequence[Dict], engine: Engine,
                     cfg, series, cache_path: str, model: str = "gemini-3.5-flash-lite",
                     prompt_version: str = "ctx-v2") -> Dict:
    """AI 심사를 호출하지 않고 캐시에서만 판정을 꺼낸다. 캐시가 없는 신호는 missing으로 남긴다."""
    ai = AIFilter(FilterConfig(model=model, cache_path=cache_path, prompt_version=prompt_version))
    out, missing = {}, 0
    for sig in signals:
        ctx = build_context(candles, sig, engine.capture.get(sig["index"]), series, ai.cfg, trade_cfg=cfg)
        key = ai.cache_key(ctx)
        hit = ai.cache.get(key)
        if not hit:
            missing += 1
            continue
        out[(int(sig["time"]), sig["side"])] = hit
    return {"decisions": out, "missing": missing, "calls": 0}


def variants() -> List[Dict]:
    """비교할 국면 필터 목록 — 이름 / 판정 함수(특징 dict, 방향 → 차단하면 True)."""
    def dead(p, side):
        return p.get("ATR%백분위(100봉)") is not None and p["ATR%백분위(100봉)"] <= 10

    def dead15(p, side):
        return p.get("ATR%백분위(100봉)") is not None and p["ATR%백분위(100봉)"] <= 15

    def panic(p, side):
        return (p.get("ATR%백분위(100봉)") is not None and p["ATR%백분위(100봉)"] >= 95) or \
               (p.get("ATR%") is not None and p["ATR%"] >= 1.6)

    def atr_hi(p, side):
        return p.get("ATR%") is not None and p["ATR%"] >= 1.2

    def htf(p, side):
        want = "위" if side == "LONG" else "아래"
        return p.get("4H추세") is not None and p["4H추세"] != want

    def trend_weak(p, side):
        return p.get("추세강도(EMA간격/ATR)") is not None and p["추세강도(EMA간격/ATR)"] < 0.35

    return [
        {"name": "변동성 위축 차단(백분위≤10)", "test": dead},
        {"name": "변동성 위축 차단(백분위≤15)", "test": dead15},
        {"name": "변동성 폭발 차단(백분위≥95 or ATR%≥1.6)", "test": panic},
        {"name": "ATR% 1.2 이상 차단", "test": atr_hi},
        {"name": "위축+폭발 차단", "test": lambda p, side: dead(p, side) or panic(p, side)},
        {"name": "횡보(추세강도<0.35) 차단", "test": trend_weak},
        {"name": "4H 추세 반대 차단", "test": htf},
    ]


def evaluate(candles: Sequence[Dict], signals: Sequence[Dict], decisions: Dict,
             cfg, params, split: float = 0.6, feats: Optional[Dict] = None,
             list_variants: Optional[List[Dict]] = None, draws: int = 0,
             seed: int = 13) -> Dict:
    """AI 통과 신호에 국면 필터를 하나씩 씌워 인샘플/아웃샘플 성적을 비교한다.

    draws>0이면 **같은 건수를 무작위로 빼는 대조군**을 돌려, 국면 필터의 개선이 우연인지 본다.
    """
    ser = prepare_series(candles)
    feats = feats if feats is not None else {
        int(s["time"]): regime_mod.features(candles, int(s["index"]), ser) for s in signals
    }
    kept = [s for s in signals if (decisions.get((int(s["time"]), s["side"])) or {}).get("decision") == "PASS"]
    split_t = candles[min(len(candles) - 1, int(len(candles) * split))]["t"]

    def block(trades):
        is_t = [t for t in trades if t["entry_time"] < split_t]
        os_t = [t for t in trades if t["entry_time"] >= split_t]
        return {"all": _block(trades, cfg), "is": _block(is_t, cfg), "os": _block(os_t, cfg)}

    base = simulate(candles, kept, cfg, params)
    base_trades = base["trades"]
    winners = {(t["signal_time"], t["side"]) for t in base_trades
               if t["reason"] == "TARGET" and (t.get("r") or 0) >= 3.5}
    rows = [{"이름": "없음(AI 필터만)", "남긴신호": len(kept), "체결": len(base_trades),
             "4R승자보존": f"{len(winners)}/{len(winners)}", **block(base_trades)}]
    for v in (list_variants if list_variants is not None else variants()):
        test = v["test"]
        picked = []
        for s in kept:
            f = feats.get(int(s["time"])) or {}
            if test(f, s["side"]):
                continue
            picked.append(s)
        res = simulate(candles, picked, cfg, params)
        traded = {(t["signal_time"], t["side"]) for t in res["trades"]}
        kept_winners = len(winners & traded)
        row = {"이름": v["name"], "남긴신호": len(picked), "체결": len(res["trades"]),
               "4R승자보존": f"{kept_winners}/{len(winners)}", **block(res["trades"])}
        if draws and len(picked) < len(kept):
            row["무작위대조"] = random_removal(candles, kept, len(kept) - len(picked), cfg, params,
                                              split_t, row["os"].get("avg_r"), draws=draws, seed=seed)
        rows.append(row)
    # 'side' 문자열 대신 함수로 바꿔 둔다(위 루프에서 필요)
    return {"baseline": rows[0], "variants": rows[1:], "kept_signals": len(kept),
            "all_signals": len(signals), "split_time": split_t}

def random_removal(candles, kept: Sequence[Dict], n_remove: int, cfg, params, split_t,
                   observed_os_avg_r: Optional[float], draws: int = 500, seed: int = 13) -> Dict:
    """같은 건수를 무작위로 뺐을 때의 아웃샘플 평균 R 분포 — 국면 필터가 우연보다 나은지."""
    if not n_remove or observed_os_avg_r is None:
        return {}
    rng = random.Random(seed)
    idx = list(range(len(kept)))
    vals: List[float] = []
    for _ in range(max(1, draws)):
        drop = set(rng.sample(idx, n_remove))
        sub = [s for i, s in enumerate(kept) if i not in drop]
        trades = [t for t in simulate(candles, sub, cfg, params)["trades"] if t["entry_time"] >= split_t]
        rs = [t["r"] for t in trades if t.get("r") is not None]
        if rs:
            vals.append(sum(rs) / len(rs))
    if not vals:
        return {}
    vals.sort()
    return {"제거건수": n_remove, "관측_아웃평균R": round(observed_os_avg_r, 3),
            "무작위중앙": round(vals[len(vals) // 2], 3),
            "무작위상위5%": round(vals[int(len(vals) * 0.95)], 3),
            "p값": round(sum(1 for v in vals if v >= observed_os_avg_r) / len(vals), 3)}


def _fmt(b: Dict) -> str:
    def f(v, nd=2):
        return "-" if v is None else f"{v:.{nd}f}"
    return (f"{b['trades']:>3d}건 · 승률 {f(b['win'],1):>5s}% · PF {f(b['pf']):>4s} · "
            f"순손익 {b['net_pct']:>7.2f}% · 평균R {f(b['avg_r'],3):>7s} · MDD {f(b['mdd'],1):>5s}% · "
            f"4R {b['targets']:>2d}")


def report_text(result: Dict) -> str:
    lines = [f"■ AI 통과 신호 {result['kept_signals']}건(전체 {result['all_signals']}건) — 국면 필터를 하나씩 씌운 결과",
             f"  기준(AI 필터만): {_fmt(result['baseline']['all'])}",
             f"    인샘플 {_fmt(result['baseline']['is'])}",
             f"    아웃샘플 {_fmt(result['baseline']['os'])}",
             "", "■ 국면 필터별(4R 승자 보존 수가 핵심)"]
    for row in result["variants"]:
        o, i = row["os"], row["is"]
        lines.append(f"  {row['이름']}")
        lines.append(f"    신호 {row['남긴신호']}건 · 체결 {row['체결']}건 · 4R승자 {row['4R승자보존']}"
                     f" | 전체 PF {row['all']['pf']}")
        lines.append(f"    인샘플 PF {i['pf']} (순손익 {i['net_pct']:+.2f}%) · "
                     f"아웃샘플 PF {o['pf']} (순손익 {o['net_pct']:+.2f}%, {o['trades']}건)")
        rc = row.get("무작위대조") or {}
        if rc:
            lines.append(f"    무작위 대조(같은 {rc['제거건수']}건 제거): 아웃 평균R {rc['관측_아웃평균R']:+.3f}"
                         f" vs 무작위 중앙 {rc['무작위중앙']:+.3f} · 상위5% {rc['무작위상위5%']:+.3f} · p={rc['p값']}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="국면·변동성 필터가 30분 M1 후보 성적을 깎는지 실측")
    ap.add_argument("--json", default="data/btc_30m_60000.json")
    ap.add_argument("--cache", default="data/ai_cache.json")
    ap.add_argument("--model", default="gemini-3.5-flash-lite")
    ap.add_argument("--prompt-version", default="ctx-v2")
    ap.add_argument("--split", type=float, default=0.6)
    ap.add_argument("--draws", type=int, default=500, help="무작위 제거 대조군 횟수(0이면 생략)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    with open(args.json, encoding="utf-8") as fh:
        candles = json.load(fh)
    candles.sort(key=lambda c: c["t"])
    params, cfg = nominee_setup()
    engine = Engine(params)
    signals = select_signals(candles, engine.run(candles), cfg)
    series = prepare_series(candles)
    got = cached_decisions(candles, signals, engine, cfg, series, args.cache,
                           model=args.model, prompt_version=args.prompt_version)
    if got["missing"]:
        print(f"⚠ 캐시에 없는 신호 {got['missing']}건 — 호출하지 않고 나머지로만 비교합니다(한도 보호)")
    # 'side'로 표시된 변형은 함수로 바꾼다
    feats = {int(s["time"]): regime_mod.features(candles, int(s["index"]), series) for s in signals}
    result = evaluate(candles, signals, got["decisions"], cfg, params, split=args.split, feats=feats,
                      draws=args.draws)
    result["meta"] = {"bars": len(candles), "cache_missing": got["missing"], "api_calls": 0,
                      "model": args.model, "prompt_version": args.prompt_version,
                      "period": {"from": candles[0]["t"], "to": candles[-1]["t"]}}
    print(report_text(result))
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=1)
        print(f"\n저장: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
