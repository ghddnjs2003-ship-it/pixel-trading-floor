"""차트 라벨 대조 — 트레이딩뷰 v7 라벨과 파이썬 엔진 신호를 같은 봉 위에서 나란히 본다.

쓰는 법
    # 1) 화면에서 읽은 라벨을 파일로 적어 두고 (한 줄에 "YYYY-MM-DD HH:MM 라벨문구")
    python -m backend.label_audit --json work/btc_15m.json --expected work/chart_labels.txt
    # 2) 라벨 파일 없이 엔진이 낸 신호만 확인
    python -m backend.label_audit --json work/btc_15m.json --from "11-16 15:00" --to "11-19 11:30"

--expected 파일의 라벨 문구는 `롱·거부` `숏·거부` `롱·복귀` `숏·복귀` `M1 롱` `M1 숏`.
±1봉 오차는 시간이 아니라 봉 인덱스로 따진다(15분봉이면 ±15분).
"""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

from .strategy import Engine, Params, scan

KST = timezone(timedelta(hours=9))


def kst(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=KST).strftime("%m-%d %H:%M")


def load(path: str) -> List[Dict]:
    candles = json.load(open(path, encoding="utf-8"))
    if not isinstance(candles, list) or not candles:
        raise SystemExit(f"{path}에 캔들 배열이 없습니다")
    candles.sort(key=lambda c: c["t"])
    return candles


def _norm(label: str) -> str:
    return label.replace("·", "").replace(" ", "").replace("・", "")


def _engine_label(sig: Dict) -> str:
    if sig["kind"] == "DS":
        return f"{'롱' if sig['side'] == 'LONG' else '숏'}·{sig['label']}"
    return f"M1 {'롱' if sig['side'] == 'LONG' else '숏'}" if sig["label"].startswith("M1 ") else sig["label"]


def parse_expected(path: str) -> List[Dict]:
    out = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        stamp = f"{parts[0]} {parts[1]}"
        out.append({"stamp": stamp, "label": parts[2].strip()})
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="v7 차트 라벨 ↔ 파이썬 엔진 대조")
    ap.add_argument("--json", required=True, help="캔들 JSON(오래된 봉부터)")
    ap.add_argument("--expected", default=None, help="화면에서 읽은 라벨 파일")
    ap.add_argument("--from", dest="frm", default=None, help='구간 시작 "11-16 15:00" 또는 ISO')
    ap.add_argument("--to", dest="to", default=None, help='구간 끝 "11-19 11:30"')
    ap.add_argument("--tolerance", type=int, default=1, help="허용 봉 오차(±봉)")
    args = ap.parse_args(argv)

    candles = load(args.json)
    params = Params()
    eng = Engine(params)
    signals = eng.run(candles)
    index_of = {c["t"]: i for i, c in enumerate(candles)}

    def in_window(stamp: str) -> bool:
        if args.frm and stamp < args.frm:
            return False
        if args.to and stamp > args.to:
            return False
        return True

    shown = [s for s in signals if in_window(kst(s["time"]))]
    print(f"■ {os.path.basename(args.json)} · 봉 {len(candles)}개 "
          f"({kst(candles[0]['t'])} ~ {kst(candles[-1]['t'])}) · 신호 {len(signals)}개")
    print(f"  구간 {args.frm or '-'} ~ {args.to or '-'} · 엔진 라벨 {len(shown)}개")
    for s in shown:
        print(f"  {kst(s['time'])}  {_engine_label(s):10s} 진입 {s['entry']:,.1f} "
              f"손절 {s['stop']:,.1f} 목표 {s['target']:,.1f} RR {s['rr']}")

    last = candles[-1]
    live_bsl = [round(l.price, 1) for l in eng.state["bsl"] if not l.consumed]
    live_ssl = [round(l.price, 1) for l in eng.state["ssl"] if not l.consumed]
    print(f"  살아 있는 BSL {live_bsl} / SSL {live_ssl}")
    for z in eng.state["res"]:
        print(f"  저항 {z.bot:,.1f}~{z.top:,.1f} {'확정' if z.frozen else '형성'} (생성 {kst(candles[z.born]['t'])})")
    for z in eng.state["sup"]:
        print(f"  지지 {z.bot:,.1f}~{z.top:,.1f} {'확정' if z.frozen else '형성'} (생성 {kst(candles[z.born]['t'])})")

    if not args.expected:
        return 0

    expected = parse_expected(args.expected)
    print(f"\n■ 화면 라벨 {len(expected)}개와 대조 (±{args.tolerance}봉)")
    sig_by_key = [{"i": s["index"], "stamp": kst(s["time"]), "label": _engine_label(s), "used": False} for s in signals]
    matched = 0
    for exp in expected:
        try:
            exp_dt = datetime.strptime(f"2026-{exp['stamp']}", "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        except ValueError:
            print(f"  ? {exp['stamp']} {exp['label']} — 시각 형식을 읽지 못했습니다")
            continue
        # 가장 가까운 봉 인덱스
        target = min(candles, key=lambda c: abs(c["t"] - exp_dt.timestamp() * 1000))
        ti = index_of[target["t"]]
        want = _norm(exp["label"])
        hit = None
        for s in sig_by_key:
            if s["used"]:
                continue
            if abs(s["i"] - ti) <= args.tolerance and _norm(s["label"]) == want:
                hit = s
                break
        if hit:
            hit["used"] = True
            matched += 1
            print(f"  ✓ {exp['stamp']} {exp['label']:12s} → 엔진 {hit['stamp']} {hit['label']}")
        else:
            near = [s for s in sig_by_key if abs(s["i"] - ti) <= args.tolerance]
            note = (f"같은 봉대의 엔진 라벨: {[s['label'] for s in near]}" if near else "엔진 라벨 없음")
            print(f"  ✗ {exp['stamp']} {exp['label']:12s} — {note}")
    extra = [s for s in sig_by_key if not s["used"] and in_window(s["stamp"])]
    print(f"  일치 {matched}/{len(expected)} · 기대 목록에 없는 엔진 라벨 {len(extra)}개"
          + (f" → {[s['stamp'] + ' ' + s['label'] for s in extra]}" if extra else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
