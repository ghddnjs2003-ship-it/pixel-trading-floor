"""타 시간봉·ETH 교차 검증 — 30분 BTC에서 고른 AI 맥락 필터를 다른 시장에 **그대로** 걸어 본다.

무엇을 하나
- 규칙(후보1: 엔진 M1 · 아시아(00-08 UTC) 진입 · 3ATR 손절 · 4R 목표 · N봉 시간청산)은 그대로 두고
  시장만 바꾼다: **BTC 15분봉 · BTC 1시간봉 · ETH 30분봉**.
- 각 시장의 최근 후보 40~80건만 Gemini에게 심사시킨다(2초 간격·문맥 캐시·호출 예산으로 429 보호).
- 같은 구간에서 **필터 전 / 필터 후**(체결 수·승률·PF·순손익·MDD·평균 R·4R 도달)를 비교한다.

프롬프트
- 루브릭(가중치·문턱 60·출력 형식)은 30분 BTC에서 정한 그대로다. 바뀌는 것은 문맥에 적히는
  **종목·시간봉 이름**뿐이다(`ai_filter.system_prompt`). BTC 30분 문맥은 이전 실행과 같은 문자열이라
  캐시(`data/ai_cache.json`)가 그대로 맞는다.
- 시간봉이 다른 시장은 캐시가 섞이지 않도록 prompt_version에 시장 이름을 붙인다.

사용 예
    python -m backend.cross_validation --market btc15 --sample 60 --max-calls 80
    python -m backend.cross_validation --market all --summary data/ai_filter_cross_validation_2026-11-19.json
    python -m backend.cross_validation --market eth30 --dry-run      # 호출 없이 프롬프트만
"""

import argparse
import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .ai_filter import (AIFilter, FilterConfig, _block, build_context, compare, nominee_setup,
                        prepare_series, random_control, render_prompt, review_signals,
                        select_signals, sweep_thresholds, system_prompt)
from .backtest import BacktestConfig, simulate
from .strategy import Engine, Params, resample

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# 시장별 사양 — 규칙은 후보1 그대로, 바꾸는 것은 종목·시간봉·시간청산 봉 수뿐이다.
MARKETS: Dict[str, Dict] = {
    "btc15": {
        "symbol": "BTCUSDT", "tf": 15, "tf_label": "15분봉", "resample": None,
        "max_bars": 96,                       # 15분봉 96봉 = 24시간
        "candles": "data/btc_15m_45000.json",
        "rule": "M1 · 아시아 3ATR · 4R · 96봉(24h)",
        "prompt_version": "ctx-v2|BTCUSDT|15m",
    },
    "btc60": {
        "symbol": "BTCUSDT", "tf": 60, "tf_label": "1시간봉", "resample": None,
        "max_bars": 48,                       # 1시간봉 48봉 = 48시간
        "candles": "data/btc_60m_65000.json",
        "rule": "M1 · 아시아 3ATR · 4R · 48봉(48h)",
        "prompt_version": "ctx-v2|BTCUSDT|60m",
    },
    "eth30": {
        "symbol": "ETHUSDT", "tf": 30, "tf_label": "30분봉", "resample": 30,
        "max_bars": 96,                       # 30분봉 96봉 = 48시간
        "candles": "data/eth_15m_45000.json",  # 15분봉을 30분봉으로 묶어 쓴다
        "rule": "M1 · 아시아 3ATR · 4R · 96봉(48h)",
        "prompt_version": "ctx-v2|ETHUSDT|30m",
    },
}


def market_setup(key: str) -> tuple:
    """시장 사양 → (Params, BacktestConfig). 후보1과 같은 엔진·체결 모형, 시간청산 봉 수만 다르다."""
    spec = MARKETS[key]
    params, cfg = nominee_setup()
    cfg = BacktestConfig(**{**cfg.to_dict(), "max_bars": spec["max_bars"]})
    return params, cfg


def load_candles(key: str, path: Optional[str] = None) -> List[Dict]:
    spec = MARKETS[key]
    with open(path or str(ROOT / spec["candles"]), encoding="utf-8") as fh:
        candles = json.load(fh)
    candles.sort(key=lambda c: c["t"])
    if spec["resample"]:
        candles = resample(candles, spec["resample"])
    if len(candles) < 200:
        raise RuntimeError(f"{key}: 봉이 너무 적습니다({len(candles)})")
    return candles


def _iso(ms: Optional[int]) -> Optional[str]:
    if not ms:
        return None
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(ms) / 1000)) + "Z"


def run_market(key: str, ai: AIFilter, sample: int = 60, split: float = 0.6,
               candles_path: Optional[str] = None, dry_run: bool = False,
               offset: int = 0) -> Dict:
    """한 시장의 최근 후보를 심사하고 필터 전/후를 같은 구간에서 비교한다.

    offset > 0이면 끝에서 offset건 앞까지를 쓴다(호출 한도를 피해 나눠 돌릴 때 — 캐시가 이어 준다).
    """
    spec = MARKETS[key]
    params, cfg = market_setup(key)
    candles = load_candles(key, candles_path)
    engine = Engine(params)
    signals = engine.run(candles)
    candidates = select_signals(candles, signals, cfg)
    if not candidates:
        raise RuntimeError(f"{key}: 후보1 조건에 맞는 M1 신호가 없습니다")
    end = max(1, len(candidates) - max(0, offset))
    window = candidates[max(0, end - sample):end] if sample else candidates[:end]
    start = int(window[0]["index"])

    ai.cfg.symbol = spec["symbol"]
    ai.cfg.tf_label = spec["tf_label"]
    ai.cfg.prompt_version = spec["prompt_version"]

    series = prepare_series(candles)
    if dry_run:
        prompts = [render_prompt(build_context(candles, s, engine.capture.get(s["index"]), series,
                                               ai.cfg, trade_cfg=cfg, symbol=spec["symbol"]))
                   for s in window]
        return {
            "market": key, "symbol": spec["symbol"], "timeframe": spec["tf_label"], "rule": spec["rule"],
            "window": {"signals": len(window), "first": _iso(window[0]["time"]),
                       "last": _iso(window[-1]["time"])},
            "dry_run": {"prompts": len(prompts),
                        "avg_prompt_chars": sum(len(p) for p in prompts) // max(1, len(prompts)),
                        "system_prompt": system_prompt(spec["symbol"], spec["tf_label"])[:120]},
        }

    t0 = time.time()
    kept, reviews = review_signals(candles, window, ai, series, engine.capture,
                                   trade_cfg=cfg, symbol=spec["symbol"])
    result = compare(candles, window, kept, reviews, cfg, params, split, start_index=start)
    result["sweep"] = sweep_thresholds(candles, window, reviews, cfg, params, split, start_index=start)

    summary = {
        "market": key, "symbol": spec["symbol"], "timeframe": spec["tf_label"], "rule": spec["rule"],
        "data": {"file": str(candles_path or spec["candles"]), "bars": len(candles),
                 "from": _iso(candles[0]["t"]), "to": _iso(candles[-1]["t"])},
        "window": {"signals": len(window), "kept": len(kept),
                   "first": _iso(window[0]["time"]), "last": _iso(window[-1]["time"]),
                   "bars": len(candles) - start, "span": _iso(candles[start]["t"]) + "~" + _iso(candles[-1]["t"])},
        "reviews": result["reviews"],
        "baseline": result["baseline"],
        "filtered": result["filtered"],
        "hit": result["hit"],
        "random_control": result["random_control"],
        "sweep": result["sweep"],
        "meta": {"model": (ai.provider.config.get("model") if ai.provider else None),
                 "threshold": ai.cfg.loss_threshold,
                 "calls": ai.calls, "cache_hits": ai.cache_hits, "errors": ai.errors,
                 "budget_hits": ai.budget_hits,
                 "avg_latency_s": round(sum(ai.latencies) / len(ai.latencies), 2) if ai.latencies else None,
                 "wall_s": round(time.time() - t0, 1)},
    }
    # 리뷰 원장(어떤 자리를 얼마로 걸렀는지) — 표에는 요약만, 여기에는 전부
    ledger = [{"time": _iso(r["time"]), "side": r["side"], "entry": r["entry"],
               "loss_prob": r["ai"].get("loss_prob"), "decision": r["ai"]["decision"],
               "reason": r["ai"].get("reason"), "error": r["ai"].get("error"),
               "cached": r["ai"].get("cached")} for r in reviews]
    return {"summary": summary, "ledger": ledger}


def verdict_rows(markets: Dict[str, Dict]) -> List[str]:
    """시장별 필터 전/후를 한 표로. 교차 검증의 결론은 이 표가 말한다."""
    rows = []
    for key, m in markets.items():
        b, f = m["baseline"]["all"], m["filtered"]["all"]
        rv = m["reviews"]
        rows.append(
            f"| {m['symbol']} {m['timeframe']} | {rv['count']} | {rv['reject_pct']}% | "
            f"{b['trades']} → {f['trades']} | {b['win']} → {f['win']} | {b['pf']} → {f['pf']} | "
            f"{b['net_pct']:+.2f}% → {f['net_pct']:+.2f}% | {b['avg_r']:+.3f} → {f['avg_r']:+.3f} | "
            f"{b['mdd']:.1f}% → {f['mdd']:.1f}% |"
        )
    return rows


def conclusion(markets: Dict[str, Dict]) -> Dict:
    """숫자로만 말한다 — 몇 개 시장서 개선됐고, 4R 승자를 얼마나 죽였나."""
    better = worse = 0
    details = []
    for key, m in markets.items():
        b, f = m["baseline"]["all"], m["filtered"]["all"]
        good = (f["pf"] or 0) > (b["pf"] or 0) and f["avg_r"] > b["avg_r"]
        better += 1 if good else 0
        worse += 0 if good else 1
        w4 = m["hit"]["four_r_winners"]
        worst = m["hit"]["worst_decile"]
        details.append({
            "market": key,
            "avg_r_delta": round(f["avg_r"] - b["avg_r"], 3),
            "pf_delta": round((f["pf"] or 0) - (b["pf"] or 0), 2),
            "net_pct_delta": round(f["net_pct"] - b["net_pct"], 2),
            "reject_pct": m["reviews"]["reject_pct"],
            "four_r_kept": f"{w4['n'] - w4['rejected']}/{w4['n']}" if w4["n"] else "-",
            "worst_decile_rejected_pct": worst["reject_pct"],
        })
    return {"improved_markets": better, "not_improved_markets": worse, "markets": details}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="AI 맥락 필터 타 시간봉·ETH 교차 검증")
    ap.add_argument("--market", default="btc15", help="btc15 | btc60 | eth30 | all")
    ap.add_argument("--candles", default=None, help="캔들 JSON 경로(기본은 시장 사양)")
    ap.add_argument("--sample", type=int, default=60, help="최근 후보 몇 건을 심사할지(40~80 권장)")
    ap.add_argument("--offset", type=int, default=0, help="끝에서 이만큼 앞까지를 쓴다(나눠 돌릴 때)")
    ap.add_argument("--split", type=float, default=0.6)
    ap.add_argument("--min-interval", type=float, default=2.0, help="호출 사이 최소 간격(초)")
    ap.add_argument("--max-calls", type=int, default=0, help="이번 실행의 최대 호출 수")
    ap.add_argument("--threshold", type=float, default=60.0)
    ap.add_argument("--model", default="", help="덮어쓸 모델(기본은 .env)")
    ap.add_argument("--cache", default=str(DATA / "ai_cache.json"))
    ap.add_argument("--out-dir", default=str(ROOT / "out"))
    ap.add_argument("--summary", default=None, help="합친 요약 JSON 경로(예: data/ai_filter_cross_validation_2026-11-19.json)")
    ap.add_argument("--dry-run", action="store_true", help="호출 없이 프롬프트만 만든다")
    args = ap.parse_args(argv)

    keys = list(MARKETS) if args.market == "all" else [args.market]
    for key in keys:
        if key not in MARKETS:
            raise SystemExit(f"알 수 없는 시장: {key} (btc15·btc60·eth30·all)")

    os.makedirs(args.out_dir, exist_ok=True)
    ai = AIFilter(FilterConfig(model=args.model, min_interval=args.min_interval,
                               cache_path=args.cache, max_calls=args.max_calls,
                               loss_threshold=args.threshold))
    summaries: Dict[str, Dict] = {}
    for key in keys:
        print(f"\n■ {key} — {MARKETS[key]['symbol']} {MARKETS[key]['tf_label']} · {MARKETS[key]['rule']}",
              flush=True)
        out = run_market(key, ai, sample=args.sample, split=args.split,
                         candles_path=args.candles if len(keys) == 1 else None,
                         dry_run=args.dry_run, offset=args.offset)
        if args.dry_run:
            print(f"  호출 없이 프롬프트 {out['dry_run']['prompts']}개 · 평균 "
                  f"{out['dry_run']['avg_prompt_chars']}자 · {out['window']['first']}~{out['window']['last']}",
                  flush=True)
            continue
        summary, ledger = out["summary"], out["ledger"]
        summaries[key] = summary
        path = os.path.join(args.out_dir, f"xval_{key}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"summary": summary, "ledger": ledger}, fh, ensure_ascii=False, indent=1)
        b, f = summary["baseline"]["all"], summary["filtered"]["all"]
        print(f"  심사 {summary['reviews']['count']}건 · REJECT {summary['reviews']['reject_pct']}% "
              f"({summary['window']['first']}~{summary['window']['last']}) · "
              f"호출 {summary['meta']['calls']} · 캐시 {summary['meta']['cache_hits']} · "
              f"오류 {summary['meta']['errors']} · {summary['meta']['wall_s']}초", flush=True)
        print(f"  필터 전: 체결 {b['trades']} · 승률 {b['win']}% · PF {b['pf']} · {b['net_pct']:+.2f}% · "
              f"평균 R {b['avg_r']:+.3f} · MDD {b['mdd']}%", flush=True)
        print(f"  필터 후: 체결 {f['trades']} · 승률 {f['win']}% · PF {f['pf']} · {f['net_pct']:+.2f}% · "
              f"평균 R {f['avg_r']:+.3f} · MDD {f['mdd']}%", flush=True)
        print(f"  저장: {path}", flush=True)

    if args.summary and not args.dry_run:
        # 이미 만들어 둔 시장 결과도 합친다(한 시장씩 나눠 돌릴 수 있게).
        for key in MARKETS:
            path = os.path.join(args.out_dir, f"xval_{key}.json")
            if key in summaries or not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    summaries[key] = json.load(fh)["summary"]
            except (OSError, json.JSONDecodeError, KeyError):
                continue
        merged = {
            "title": "AI 맥락 필터 교차 검증 — 타 시간봉·ETH (문턱 60·같은 루브릭)",
            "date": time.strftime("%Y-%m-%d"),
            "method": {
                "rule": "후보1 — 엔진 M1 · 아시아(00-08 UTC) 진입 · 3ATR 손절 · 4R 목표 · N봉 시간청산",
                "filter": "ai_filter.py 그대로 — loss_prob ≥ 60이면 REJECT, 프롬프트 루브릭 무변경",
                "sample": f"시장별 최근 후보 {args.sample}건(40~80) · 호출 간격 {args.min_interval}초 · 문맥 캐시",
                "execution": "다음 봉 시가 진입 · 같은 봉 손절/목표 동시 도달 시 손절 우선 · 수수료 5bp·슬리피지 2bp",
                "window": "각 시장의 첫 심사 후보 봉부터 마지막 봉까지(그 앞 신호는 체결하지 않음)",
            },
            "model": (ai.provider.config.get("model") if ai.provider else None) or args.model or None,
            "threshold": args.threshold,
            "markets": summaries,
            "verdict": conclusion(summaries),
            "table": verdict_rows(summaries),
        }
        os.makedirs(os.path.dirname(args.summary) or ".", exist_ok=True)
        with open(args.summary, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, ensure_ascii=False, indent=1)
        print(f"\n요약 저장: {args.summary}", flush=True)
        if merged["table"]:
            print("\n".join(merged["table"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
