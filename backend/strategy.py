"""더블 S + MDS v7 룰 엔진 — 트레이딩뷰 통합 지표(indicators/double-s-mds-v7.pine)의 '판정' 부분.

이 파일은 v7 파인스크립트의 규칙을 화면 없이(백테스트·서버 판정·AI 두뇌 문맥) 돌리기 위해 옮긴 것이다.
파인스크립트의 라벨 서식·박스 그리기·대시보드는 옮기지 않았고, 신호가 서는지/안 서는지만 다룬다.
표준 라이브러리만 쓴다(의존성 0).

용어
- BSL/SSL : 스윙 고점·저점의 유동성 레벨(2봉 프랙털, 최근 lookback 봉)
- 스윕    : 꼬리가 레벨을 넘고 종가는 레벨 안쪽으로 돌아온 봉
- 거부(rejection) : 스윕 봉 하나로 회복이 끝난 경우 (v6/v7의 `거부` 라벨)
- 복귀(reclaim)   : 스윕 봉이 레벨 밖에서 마감하고 다음 봉이 안쪽으로 복귀한 경우 (`복귀` 라벨)
- 매물대 존      : 최근 구간에서 거래가 몰린 가격대(거래량 프로파일). 확정 존은 생성 뒤
                    min_zone_age 봉이 지나야 M1 기준선으로 쓴다(점선 → 실선 규칙)
- M1      : 존 경계를 거래량과 함께 몸통 종가로 돌파한 뒤, 첫 리테스트에서 경계 지지를 확인하고 진입
- M1-2    : M1 손절 뒤 반대편 지지가 버티고 거래량을 실어 재돌파할 때의 재진입

시간 규칙
- 신호는 i번 봉이 '끝난 뒤' 확정되고, 진입은 i+1봉 시가다(추격 금지·다음 봉 시가 진입).
- 그래서 evaluate(candles, i)는 i까지의 데이터만 본다(미래 참조 없음).

값이 없거나 규칙이 서지 않으면 side=None을 돌려준다. 지어내지 않는다.
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Sequence, Tuple


# ----------------------------------------------------------------------------- 파라미터

@dataclass
class Params:
    """v7 기본값. 파인스크립트 입력값과 같은 이름을 쓴다."""

    atr_period: int = 14
    swing_lookback: int = 24            # 대추세선·스윙 유동성 룩백
    level_keep: int = 5                 # BSL/SSL 각각 최대 개수

    # 더블 S 스윕 필터 (v7: 0.05~1.60 ATR)
    sweep_min_atr: float = 0.05
    sweep_max_atr: float = 1.60
    sweep_invalidate_atr: float = 0.5   # 종가가 레벨 밖으로 0.5 ATR 넘게 마감하면 셋업 무효
    rvol_a: float = 1.5                 # A등급 기준 상대 거래량
    level_min_age: int = 4              # 스윙 레벨이 최소 이만큼 오래돼야 유동성으로 본다
    level_freshness: int = 12           # 이미 스윕된 레벨은 이 봉 수 동안 다시 쓰지 않는다
    require_bos: bool = True            # 회복 봉이 직전 봉 구조를 깨야(BOS) 진입 — v6의 2대 트리거

    # MDS 매물대 존
    zone_lookback: int = 192            # 존을 만들 때 보는 봉 수
    zone_bins: int = 60                 # 가격 구간(빈) 수
    zone_min_share: float = 0.10        # 존이 차지해야 하는 최소 거래량 비중
    zone_min_age: int = 4               # 확정 존 최소 나이(점선 → 실선)

    # M1 돌파·리테스트
    volume_mult: float = 1.15           # 돌파봉 거래량 = 20봉 평균 × 1.15 이상
    body_ratio: float = 0.5             # 몸통이 봉 전체 길이의 절반 이상(찐 돌파)
    retest_max_bars: int = 15           # M1 쿨타임(첫 리테스트까지만)
    retest_volume_drop: bool = True     # 리테스트 거래량이 돌파봉보다 줄어야 인정

    # 보조 필터
    cci_period: int = 20                # 공식 스펙 CCI(20, hlc3)
    cci_mode: str = "sign"              # off | sign(방향 일치) | threshold(±100)
    bb_period: int = 20
    bb_mult: float = 2.0
    bb_expand: bool = True              # 볼린저 밴드 폭이 확장 중일 때만

    # 목표·손절
    stop_buffer_atr: float = 0.15       # 손절은 경계·스윕 극점에서 0.15 ATR 바깥
    min_rr: float = 1.5                 # 최소 손익비(1:1.5, v7의 1.5R 기준)
    target_r_multiples: Tuple[float, ...] = (1.5,)

    def to_dict(self) -> Dict:
        return asdict(self)


# ----------------------------------------------------------------------------- 지표

def atr_series(candles: Sequence[Dict], period: int = 14) -> List[Optional[float]]:
    """Wilder ATR. 값이 모자란 구간은 None."""
    out: List[Optional[float]] = [None] * len(candles)
    if len(candles) < 2:
        return out
    trs = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c["h"] - c["l"])
        else:
            pc = candles[i - 1]["c"]
            trs.append(max(c["h"] - c["l"], abs(c["h"] - pc), abs(c["l"] - pc)))
    if len(trs) < period:
        return out
    first = sum(trs[:period]) / period
    out[period - 1] = first
    prev = first
    for i in range(period, len(trs)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def cci_series(candles: Sequence[Dict], period: int = 20) -> List[Optional[float]]:
    """CCI(period, hlc3)."""
    out: List[Optional[float]] = [None] * len(candles)
    tp = [(c["h"] + c["l"] + c["c"]) / 3 for c in candles]
    for i in range(len(candles)):
        if i < period - 1:
            continue
        win = tp[i - period + 1:i + 1]
        mean = sum(win) / period
        mad = sum(abs(v - mean) for v in win) / period
        if mad == 0:
            out[i] = 0.0
        else:
            out[i] = (tp[i] - mean) / (0.015 * mad)
    return out


def bollinger_series(candles: Sequence[Dict], period: int = 20,
                     mult: float = 2.0) -> List[Optional[Tuple[float, float, float]]]:
    """(mid, upper, lower) — 종가 기준 모표준편차."""
    out: List[Optional[Tuple[float, float, float]]] = [None] * len(candles)
    closes = [c["c"] for c in candles]
    for i in range(len(closes)):
        if i < period - 1:
            continue
        win = closes[i - period + 1:i + 1]
        mid = sum(win) / period
        sd = (sum((v - mid) ** 2 for v in win) / period) ** 0.5
        out[i] = (mid, mid + mult * sd, mid - mult * sd)
    return out


def ema_series(values: Sequence[float], period: int) -> List[float]:
    k = 2 / (period + 1)
    out = []
    prev = values[0] if values else 0.0
    for i, v in enumerate(values):
        prev = v if i == 0 else v * k + prev * (1 - k)
        out.append(prev)
    return out


def swing_levels(candles: Sequence[Dict], end: int, lookback: int = 24,
                 keep: int = 5, min_age: int = 4) -> Tuple[List[float], List[float]]:
    """end 봉까지의 2봉 프랙털 스윙 고점(BSL)·저점(SSL). 가까운 순으로 keep개.

    min_age — 방금 생긴 스윙(형성에 필요한 2봉 확인만 끝난 것)은 유동성으로 보지 않는다.
    """
    start = max(2, end - lookback)
    highs: List[float] = []
    lows: List[float] = []
    for i in range(start, max(start, end - min_age - 1)):
        c, p2, p1 = candles[i], candles[i - 2], candles[i - 1]
        n1, n2 = candles[i + 1], candles[i + 2]
        if c["h"] >= p1["h"] and c["h"] >= p2["h"] and c["h"] >= n1["h"] and c["h"] >= n2["h"]:
            highs.append(c["h"])
        if c["l"] <= p1["l"] and c["l"] <= p2["l"] and c["l"] <= n1["l"] and c["l"] <= n2["l"]:
            lows.append(c["l"])
    price = candles[end]["c"] if candles else 0.0
    # 이미 스윕돼 되돌아온 레벨(꼬리 초과 + 종가 복귀)은 이미 한 번 반응한 자리 — 다시 쓰지 않는다.
    # 꼬리만 스친 것은 레벨을 무효화하지 않는다(그게 이 매매법의 신호이므로).
    recent = candles[max(0, end - 12):end]
    fresh_highs = [h for h in highs if not any(c["h"] > h and c["c"] < h for c in recent)]
    fresh_lows = [l for l in lows if not any(c["l"] < l and c["c"] > l for c in recent)]
    highs = sorted({h for h in fresh_highs if h >= price}, key=lambda v: v)[:keep]
    lows = sorted({l for l in fresh_lows if l <= price}, key=lambda v: -v)[:keep]
    return highs, lows


# ----------------------------------------------------------------------------- 매물대 존

def volume_zones(candles: Sequence[Dict], end: int, params: Params) -> List[Dict]:
    """end 봉까지로 만든 거래량 프로파일 존(내림차순 요약).

    가격을 zone_bins칸으로 나눠 typical price((h+l+c)/3)에 그 봉 거래량을 쌓고,
    상위 거래량 칸이 이어지면 하나의 존으로 묶는다. 존은 [low, high]와 거래량 비중을 갖는다.
    """
    lookback = params.zone_lookback
    start = max(0, end - lookback + 1)
    window = candles[start:end + 1]
    if len(window) < 20:
        return []
    hi = max(c["h"] for c in window)
    lo = min(c["l"] for c in window)
    if not hi > lo:
        return []
    bins = params.zone_bins
    step = (hi - lo) / bins
    vols = [0.0] * bins
    for c in window:
        tp = (c["h"] + c["l"] + c["c"]) / 3
        idx = min(bins - 1, max(0, int((tp - lo) / step)))
        vols[idx] += c.get("v", 1.0)
    total = sum(vols)
    if total <= 0:
        return []
    # 상위 25% 칸을 매물대로 본다(거래가 몰린 가격대)
    order = sorted(range(bins), key=lambda i: -vols[i])
    hot = set(order[:max(2, bins // 4)])
    zones: List[Dict] = []
    i = 0
    while i < bins:
        if i not in hot:
            i += 1
            continue
        j = i
        while j + 1 < bins and (j + 1) in hot:
            j += 1
        share = sum(vols[i:j + 1]) / total
        if share >= params.zone_min_share:
            zones.append({
                "low": lo + step * i,
                "high": lo + step * (j + 1),
                "share": share,
                "from": window[0]["t"],
                "to": window[-1]["t"],
            })
        i = j + 1
    zones.sort(key=lambda z: -z["share"])
    return zones


# ----------------------------------------------------------------------------- 판정

def _body_ratio(c: Dict) -> float:
    rng = c["h"] - c["l"]
    if rng <= 0:
        return 0.0
    return abs(c["c"] - c["o"]) / rng


def _rvol(candles: Sequence[Dict], i: int, period: int = 20) -> Optional[float]:
    if i < period:
        return None
    base = sum(c.get("v", 0.0) for c in candles[i - period:i]) / period
    if base <= 0:
        return None
    return candles[i].get("v", 0.0) / base


def _blank(reason: str = "") -> Dict:
    return {
        "side": None, "kind": None, "grade": None, "index": None,
        "entry": None, "stop": None, "target": None, "rr": None,
        "zone": None, "reasons": [reason] if reason else [],
    }


def evaluate(candles: Sequence[Dict], i: int, params: Optional[Params] = None,
             trend_ema: Optional[Sequence[float]] = None) -> Dict:
    """i번 봉이 끝난 시점의 신호. 진입은 i+1 시가(호출부 책임)."""
    p = params or Params()
    if i is None or i < max(p.atr_period + 2, 30):
        return _blank("봉이 모자람")
    c = candles[i]
    atr = atr_series(candles[:i + 1], p.atr_period)[-1]
    if not atr:
        return _blank("ATR 없음")
    highs, lows = swing_levels(candles, i, p.swing_lookback, p.level_keep)
    ema = trend_ema or ema_series([x["c"] for x in candles[:i + 1]], 50)
    trend_up = c["c"] > ema[-1] * 1.001
    trend_down = c["c"] < ema[-1] * 0.999

    ds = _doubles(candles, i, p, atr, highs, lows, trend_up, trend_down)
    if ds["side"]:
        return ds
    m1 = _mds_m1(candles, i, p, atr, trend_up, trend_down)
    if m1["side"]:
        return m1
    return ds if ds["reasons"] else _blank("조건 미충족")


def _doubles(candles, i, p: Params, atr, highs, lows, trend_up, trend_down) -> Dict:
    """더블 S — BSL/SSL 스윕 뒤 회복(거부·복귀)과 마이너 스윙 돌파."""
    c = candles[i]
    rvol = _rvol(candles, i)
    reasons: List[str] = []

    # 숏: BSL 상향 스윕
    for level in highs:
        depth = (c["h"] - level) / atr
        if depth < p.sweep_min_atr or depth > p.sweep_max_atr:
            continue
        if c["c"] < level and c["o"] < level:                      # 꼬리만 나갔다가 종가 복귀
            if p.require_bos and i >= 1 and not c["c"] < candles[i - 1]["l"]:
                continue                                           # 구조(직전 봉 저점)를 깬 뒤에만 진입
            reasons.append(f"BSL {level:.2f} 스윕(깊이 {depth:.2f} ATR) · 종가 복귀")
            return _make_signal("SHORT", "DS", "거부", i, c["c"], c["h"] + p.stop_buffer_atr * atr,
                                [l for l in lows if l < c["c"]], reasons, rvol, p, atr, trend_down)
        # 복귀: 직전 봉이 레벨 밖에서 마감, 이번 봉이 안쪽 마감
        if i >= 1:
            prev = candles[i - 1]
            if prev["c"] > level and c["c"] < level:
                d2 = (prev["h"] - level) / atr
                if p.require_bos and i >= 2 and not c["c"] < min(candles[i - 1]["l"], candles[i - 2]["l"]):
                    continue
                if p.sweep_min_atr <= d2 <= p.sweep_max_atr:
                    reasons.append(f"BSL {level:.2f} 스윕 후 다음 봉 복귀({d2:.2f} ATR)")
                    return _make_signal("SHORT", "DS", "복귀", i, c["c"],
                                        max(c["h"], prev["h"]) + p.stop_buffer_atr * atr,
                                        [l for l in lows if l < c["c"]], reasons, rvol, p, atr, trend_down)

    # 롱: SSL 하향 스윕
    for level in lows:
        depth = (level - c["l"]) / atr
        if depth < p.sweep_min_atr or depth > p.sweep_max_atr:
            continue
        if c["c"] > level and c["o"] > level:
            if p.require_bos and i >= 1 and not c["c"] > candles[i - 1]["h"]:
                continue
            reasons.append(f"SSL {level:.2f} 스윕(깊이 {depth:.2f} ATR) · 종가 복귀")
            return _make_signal("LONG", "DS", "거부", i, c["c"], c["l"] - p.stop_buffer_atr * atr,
                                [h for h in highs if h > c["c"]], reasons, rvol, p, atr, trend_up)
        if i >= 1:
            prev = candles[i - 1]
            if prev["c"] < level and c["c"] > level:
                d2 = (level - prev["l"]) / atr
                if p.require_bos and i >= 2 and not c["c"] > max(candles[i - 1]["h"], candles[i - 2]["h"]):
                    continue
                if p.sweep_min_atr <= d2 <= p.sweep_max_atr:
                    reasons.append(f"SSL {level:.2f} 스윕 후 다음 봉 복귀({d2:.2f} ATR)")
                    return _make_signal("LONG", "DS", "복귀", i, c["c"],
                                        min(c["l"], prev["l"]) - p.stop_buffer_atr * atr,
                                        [h for h in highs if h > c["c"]], reasons, rvol, p, atr, trend_up)
    return _blank()


def _mds_m1(candles, i, p: Params, atr, trend_up, trend_down) -> Dict:
    """MDS M1 — 매물대 존 돌파 뒤 첫 리테스트에서 경계 지지 확인."""
    c = candles[i]
    # 확정 존 — zone_min_age봉 전 시점의 프로파일로 만든다(점선 → 실선 규칙: 방금 생긴 존은 기준선이 아니다)
    ref = i - p.zone_min_age
    zones = volume_zones(candles, ref, p) if ref > 40 else []
    if not zones:
        return _blank("매물대 없음")
    cci = cci_series(candles[:i + 1], p.cci_period)[-1]
    bb = bollinger_series(candles[:i + 1], p.bb_period, p.bb_mult)
    bb_now, bb_prev = bb[-1], (bb[-2] if len(bb) > 1 else None)
    width_now = (bb_now[1] - bb_now[2]) if bb_now else None
    width_prev = (bb_prev[1] - bb_prev[2]) if bb_prev else None

    for zone in zones:
        top, bottom = zone["high"], zone["low"]
        # 존이 확정된 지 min_zone_age봉이 지났는지 — 존의 마지막 관측이 아니라 형성 시점을 본다
        for direction in ("LONG", "SHORT"):
            breakout_idx = None
            for j in range(max(0, i - p.retest_max_bars - 20), i):
                b = candles[j]
                rv = _rvol(candles, j)
                if rv is None or rv < p.volume_mult or _body_ratio(b) < p.body_ratio:
                    continue
                if direction == "LONG" and b["c"] > top and b["o"] < top:
                    breakout_idx = j
                    break
                if direction == "SHORT" and b["c"] < bottom and b["o"] > bottom:
                    breakout_idx = j
                    break
            if breakout_idx is None or i - breakout_idx > p.retest_max_bars or i - breakout_idx < 1:
                continue
            # 첫 리테스트만: 돌파 뒤 경계에 다시 닿은 첫 봉이어야 한다
            retest_before = False
            for j in range(breakout_idx + 1, i):
                if direction == "LONG" and candles[j]["l"] <= top:
                    retest_before = True
                if direction == "SHORT" and candles[j]["h"] >= bottom:
                    retest_before = True
            if retest_before:
                continue
            reasons: List[str] = []
            if direction == "LONG":
                touched = c["l"] <= top
                held = c["c"] > top
                ok_trend = trend_up or cci is not None and cci > 0
                ok_cci = p.cci_mode == "off" or (cci is not None and (cci > 0 if p.cci_mode == "sign" else cci >= 100))
                ok_bb = (not p.bb_expand) or (width_now and width_prev and width_now >= width_prev)
                vol_ok = (not p.retest_volume_drop) or c.get("v", 0) < max(candles[breakout_idx].get("v", 0), 1e-9)
                if touched and held and ok_trend and ok_cci and ok_bb and vol_ok:
                    reasons.append(f"존 상단 {top:.2f} 돌파 뒤 첫 리테스트 지지")
                    reasons.append(f"CCI {cci:.0f} · BB 폭 확장 · 리테스트 거래량 감소")
                    stop = top - p.stop_buffer_atr * atr
                    return _make_signal("LONG", "M1", "실선 존", i, c["c"], stop,
                                        [z["high"] for z in zones if z["high"] > c["c"]],
                                        reasons, _rvol(candles, i), p, atr, True, zone={**zone, "broke_at": breakout_idx})
            else:
                touched = c["h"] >= bottom
                held = c["c"] < bottom
                ok_trend = trend_down or cci is not None and cci < 0
                ok_cci = p.cci_mode == "off" or (cci is not None and (cci < 0 if p.cci_mode == "sign" else cci <= -100))
                ok_bb = (not p.bb_expand) or (width_now and width_prev and width_now >= width_prev)
                vol_ok = (not p.retest_volume_drop) or c.get("v", 0) < max(candles[breakout_idx].get("v", 0), 1e-9)
                if touched and held and ok_trend and ok_cci and ok_bb and vol_ok:
                    reasons.append(f"존 하단 {bottom:.2f} 돌파 뒤 첫 리테스트 저항")
                    reasons.append(f"CCI {cci:.0f} · BB 폭 확장 · 리테스트 거래량 감소")
                    stop = bottom + p.stop_buffer_atr * atr
                    return _make_signal("SHORT", "M1", "실선 존", i, c["c"], stop,
                                        [z["low"] for z in zones if z["low"] < c["c"]],
                                        reasons, _rvol(candles, i), p, atr, True, zone={**zone, "broke_at": breakout_idx})
    return _blank()


def _make_signal(side: str, kind: str, label: str, i: int, entry: float, stop: float,
                 targets: Sequence[float], reasons: List[str], rvol: Optional[float],
                 p: Params, atr: float, trend_agree: bool, zone: Optional[Dict] = None) -> Dict:
    """목표는 반대편 유동성 중 첫 값, 없으면 1.5R. 손익비가 min_rr 미만이면 신호를 버린다."""
    risk = abs(entry - stop)
    if risk <= 0 or not atr:
        return _blank("손절 폭 0")
    target = None
    for t in targets:
        if t is None:
            continue
        if side == "LONG" and t > entry and (t - entry) / risk >= p.min_rr:
            target = t
            break
        if side == "SHORT" and t < entry and (entry - t) / risk >= p.min_rr:
            target = t
            break
    rr = None
    if target is not None:
        rr = (abs(target - entry)) / risk
    if target is None:
        target = entry + risk * p.min_rr if side == "LONG" else entry - risk * p.min_rr
        rr = p.min_rr
        reasons = reasons + ["반대편 유동성이 1.5R보다 가까워 1.5R 목표로 대체"]
    grade = "A" if (rvol is not None and rvol >= p.rvol_a and trend_agree) else "B"
    return {
        "side": side, "kind": kind, "grade": grade, "index": i, "label": label,
        "entry": entry, "stop": stop, "target": target, "rr": round(rr, 2),
        "risk": risk, "atr": round(atr, 6), "rvol": None if rvol is None else round(rvol, 2),
        "level": None, "zone": zone, "reasons": reasons,
    }


def scan(candles: Sequence[Dict], params: Optional[Params] = None,
         start: int = 0) -> List[Dict]:
    """전 구간 신호 목록. 같은 봉에서 신호가 여럿 서면 더블 S를 먼저 쓴다."""
    p = params or Params()
    out = []
    for i in range(max(start, 30), len(candles) - 1):
        sig = evaluate(candles, i, p)
        if sig.get("side"):
            out.append(sig)
    return out
