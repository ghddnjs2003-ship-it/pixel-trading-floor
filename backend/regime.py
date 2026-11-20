"""시장 국면(Regime) 요약 + 선택적 안전 가드.

P0-3 · 이미 갖고 있는 데이터를 판정에 잇는다
- 가격에서 계산되는 것: 단기/중기 ATR 비율, ATR% 백분위, 추세 강도(EMA 간격·기울기), 4H 상위 추세
- 밖에서 읽어 오는 것: 공포탐욕지수(`data/global.json`), 경제 일정(`data/calendar.json`), 헤드라인
- 무엇을 읽었고 무엇이 없는지 `입력`에 그대로 적는다(tauric의 read/absent 표기 방식).

원칙
1) 이 모듈은 **정보**를 만든다. 판정을 바꾸지 않는다. 가드(RegimeGuard)는 기본 **꺼짐**이고,
   켤 때만 진입을 막는다. 30분 M1 아시아 후보(아웃샘플 PF 2.02)의 성적을 규칙 변경으로 깎지 않기 위해서다.
2) 미래 참조 없음 — features(candles, i)는 i번 봉까지의 값만 본다.
3) 없는 값은 지어내지 않는다(None).
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def _r(x, nd=2):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return round(v, nd)


def _atr(candles: Sequence[Dict], period: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(candles)
    if len(candles) <= period:
        return out
    trs: List[float] = []
    prev_close = candles[0]["c"]
    for c in candles:
        tr = max(c["h"] - c["l"], abs(c["h"] - prev_close), abs(c["l"] - prev_close))
        trs.append(tr)
        prev_close = c["c"]
    first = sum(trs[1:period + 1]) / period
    out[period] = first
    prev = first
    for i in range(period + 1, len(candles)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def _ema(values: Sequence[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    acc = sum(values[:period]) / period
    out[period - 1] = acc
    for i in range(period, len(values)):
        acc = values[i] * k + acc * (1 - k)
        out[i] = acc
    return out


def series(candles: Sequence[Dict]) -> Dict:
    """국면 계산용 지표 — 이미 ai_filter.prepare_series가 있지만 이 모듈은 독립적으로 쓴다."""
    closes = [c["c"] for c in candles]
    atr = _atr(candles, 14)
    return {
        "atr": atr,
        "atr_pct": [(_r(a / closes[i] * 100, 4) if a else None) for i, a in enumerate(atr)],
        "ema20": _ema(closes, 20),
        "ema50": _ema(closes, 50),
    }


def htf_trend(candles: Sequence[Dict], i: int, hours: int = 4, period: int = 50) -> Optional[str]:
    """4시간봉 EMA50 위/아래 — i번 봉까지의 캔들만 4시간으로 묶는다(미래 없음)."""
    if i < 1:
        return None
    tf_ms = int(hours) * 3_600_000
    buckets: Dict[int, Dict] = {}
    order: List[int] = []
    for bar in candles[:i + 1]:
        b = int(bar["t"]) // tf_ms
        if b not in buckets:
            buckets[b] = {"t": b * tf_ms, "o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"]}
            order.append(b)
        else:
            cur = buckets[b]
            cur["h"] = max(cur["h"], bar["h"])
            cur["l"] = min(cur["l"], bar["l"])
            cur["c"] = bar["c"]
    closes = [buckets[b]["c"] for b in order]
    ema = _ema(closes, period)
    if not closes or ema[-1] is None:
        return None
    return "위" if closes[-1] > ema[-1] else "아래"


def features(candles: Sequence[Dict], i: int, ser: Optional[Dict] = None,
             htf_hours: int = 4) -> Dict:
    """i번 봉 시점의 국면 숫자. 값이 모자라면 None(지어내지 않는다)."""
    ser = ser or series(candles)
    atr = ser["atr"]
    atr_pct = ser["atr_pct"]
    ema20, ema50 = ser["ema20"], ser["ema50"]
    if i < 0 or i >= len(candles):
        raise IndexError("봉 인덱스가 범위를 벗어났습니다")
    a = atr[i]
    price = candles[i]["c"]
    short = [v for v in atr[max(0, i - 13):i + 1] if v]
    mid = [v for v in atr[max(0, i - 99):i + 1] if v]
    short_avg = sum(short) / len(short) if len(short) >= 5 else None
    mid_avg = sum(mid) / len(mid) if len(mid) >= 20 else None
    window = [v for v in atr_pct[max(0, i - 99):i + 1] if v is not None]
    pctile = None
    if len(window) >= 20 and atr_pct[i] is not None:
        pctile = round(sum(1 for v in window if v <= atr_pct[i]) / len(window) * 100, 0)
    trend = None
    if a and ema20[i] is not None and ema50[i] is not None:
        trend = _r(abs(ema20[i] - ema50[i]) / a, 2)
    slope = None
    if ema20[i] is not None and i >= 5 and ema20[i - 5]:
        slope = _r((ema20[i] / ema20[i - 5] - 1) * 100, 3)
    hi = max(c["h"] for c in candles[max(0, i - 47):i + 1])
    lo = min(c["l"] for c in candles[max(0, i - 47):i + 1])
    pos = _r((price - lo) / (hi - lo) * 100, 1) if hi > lo else None
    return {
        "단기ATR": _r(short_avg, 4),
        "중기ATR": _r(mid_avg, 4),
        "ATR비율": _r(short_avg / mid_avg, 2) if short_avg and mid_avg else None,
        "ATR%": atr_pct[i],
        "ATR%백분위(100봉)": pctile,
        "추세강도(EMA간격/ATR)": trend,
        "EMA20기울기5봉%": slope,
        "48봉위치%": pos,
        "4H추세": htf_trend(candles, i, htf_hours),
        "가격": price,
    }


def classify(f: Dict) -> str:
    """국면 이름 — 숫자가 부족하면 '판단보류'."""
    ratio = f.get("ATR비율")
    pct = f.get("ATR%백분위(100봉)")
    atr_pct = f.get("ATR%")
    trend = f.get("추세강도(EMA간격/ATR)")
    if (atr_pct is not None and atr_pct >= 1.2) or (ratio is not None and ratio >= 1.8):
        return "급변(변동성 폭발)"
    if ratio is None and pct is None:
        return "판단보류"
    if (pct is not None and pct <= 15) or (ratio is not None and ratio <= 0.62):
        return "죽은 장(변동성 위축)"
    if trend is not None and trend >= 1.0:
        return "추세장"
    if trend is not None and trend < 0.35:
        return "횡보(방향 없음)"
    return "정상 범위"


# --------------------------------------------------------------------- 외부 입력

def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def fear_greed(global_json: Optional[Dict] = None) -> Optional[Dict]:
    """data/global.json(F&G 포함) → {값, 이름, 기준시각}. 없으면 None."""
    data = global_json if isinstance(global_json, dict) else load_json(DATA / "global.json")
    if not isinstance(data, dict):
        return None
    live = data.get("live") or data
    fng = live.get("fng") or data.get("fng")
    if isinstance(fng, dict):
        value = fng.get("value", fng.get("score"))
        try:
            value = int(float(value))
        except (TypeError, ValueError):
            return None
        return {"값": value, "이름": fng.get("label") or fng.get("classification"),
                "기준": fng.get("at") or fng.get("ts") or data.get("at") or data.get("updated_at")}
    try:
        value = int(float(data.get("fng_value")))
    except (TypeError, ValueError):
        return None
    return {"값": value, "이름": data.get("fng_label"), "기준": data.get("updated_at")}


def upcoming_events(calendar: Optional[Dict], now_ms: Optional[int] = None, hours: int = 24) -> List[Dict]:
    """경제 일정 중 지금부터 hours 안에 있는 것(임팩트 높은 것 먼저·최대 4건)."""
    now_ms = now_ms if now_ms is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    events = (calendar or {}).get("events") if isinstance(calendar, dict) else None
    if not isinstance(events, list):
        return []
    limit = now_ms + hours * 3_600_000
    rows = []
    for e in events:
        at = e.get("atMs")
        if not isinstance(at, (int, float)):
            continue
        if now_ms - 15 * 60_000 <= at <= limit:      # 15분 전부터(막 발표된 것 포함)
            rows.append({"제목": e.get("title") or e.get("titleEn"), "시각KST": e.get("time") or e.get("date"),
                         "중요도": e.get("impact"), "예상": e.get("forecast"), "이전": e.get("previous"),
                         "지남": at < now_ms})
    order = {"high": 0, "medium": 1, "low": 2}
    rows.sort(key=lambda r: (order.get(str(r.get("중요도")), 3), r.get("시각KST") or ""))
    return rows[:4]


def summary(candles: Sequence[Dict], i: int, ser: Optional[Dict] = None,
            global_json: Optional[Dict] = None, calendar: Optional[Dict] = None,
            news: Optional[Sequence[Dict]] = None, now_ms: Optional[int] = None) -> Dict:
    """AI 프롬프트에 넣는 국면 요약 — 값 + 입력 상태(read/absent)."""
    f = features(candles, i, ser)
    fng = fear_greed(global_json)
    events = upcoming_events(calendar, now_ms)
    headlines = []
    for item in (news or [])[:3]:
        title = item.get("title") if isinstance(item, dict) else str(item)
        if title:
            headlines.append(str(title)[:90])
    inputs = {
        "가격·변동성": "읽음",
        "공포탐욕지수": "읽음" if fng else "없음",
        "경제일정": f"읽음({len(events)}건)" if events else ("읽음(임박 일정 없음)" if calendar else "없음"),
        "뉴스헤드라인": f"읽음({len(headlines)}건)" if headlines else "없음",
    }
    return {
        "국면": classify(f),
        "변동성": {"ATR%": f["ATR%"], "ATR%백분위(100봉)": f["ATR%백분위(100봉)"],
                   "단기ATR/중기ATR": f["ATR비율"]},
        "추세": {"강도(EMA간격/ATR)": f["추세강도(EMA간격/ATR)"], "EMA20기울기5봉%": f["EMA20기울기5봉%"],
                 "4H EMA50 대비": f["4H추세"], "48봉위치%": f["48봉위치%"]},
        "심리": {"공포탐욕": fng},
        "이벤트": events,
        "헤드라인": headlines,
        "입력": inputs,
    }


# --------------------------------------------------------------------- 가드(기본 꺼짐)

@dataclass
class RegimeGuardConfig:
    """진입을 막는 국면 가드. 기본값은 **꺼짐** — 실측 없이 켜면 성적을 깎을 수 있다."""

    enabled: bool = False
    block_dead: bool = True          # 변동성 위축(ATR 백분위 낮음)에서 진입 금지
    dead_pctile: float = 10.0
    dead_ratio: float = 0.62         # 단기 ATR / 중기 ATR 이 이 값 이하면 위축으로 본다
    block_panic: bool = True         # 변동성 폭발에서 진입 금지
    panic_pctile: float = 95.0
    panic_atr_pct: float = 1.6
    block_against_htf: bool = False  # 4H 추세 반대 진입 금지(기본 꺼짐 — 검증 필요)

    def to_dict(self) -> Dict:
        return {"enabled": self.enabled, "block_dead": self.block_dead, "dead_pctile": self.dead_pctile,
                "dead_ratio": self.dead_ratio,
                "block_panic": self.block_panic, "panic_pctile": self.panic_pctile,
                "panic_atr_pct": self.panic_atr_pct, "block_against_htf": self.block_against_htf}


def guard(side: Optional[str], f: Dict, cfg: Optional[RegimeGuardConfig] = None) -> Dict:
    """진입 가능 여부. cfg가 없거나 enabled=False면 항상 통과(정보만 남긴다)."""
    cfg = cfg or RegimeGuardConfig()
    if not cfg.enabled:
        return {"ok": True, "reason": "가드 꺼짐(국면은 정보로만 사용)", "active": False}
    pct = f.get("ATR%백분위(100봉)")
    atr_pct = f.get("ATR%")
    ratio = f.get("ATR비율")
    if cfg.block_dead and ((pct is not None and pct <= cfg.dead_pctile)
                           or (ratio is not None and ratio <= cfg.dead_ratio)):
        return {"ok": False, "active": True,
                "reason": f"국면 가드: 변동성 위축(ATR 백분위 {pct}, 단기/중기 {ratio}) — 4R 목표까지 갈 힘이 없다"}
    if cfg.block_panic and ((pct is not None and pct >= cfg.panic_pctile)
                            or (atr_pct is not None and atr_pct >= cfg.panic_atr_pct)):
        return {"ok": False, "active": True,
                "reason": f"국면 가드: 변동성 폭발(ATR% {atr_pct}, 백분위 {pct}) — 손절이 먼저 맞기 쉽다"}
    if cfg.block_against_htf and side and f.get("4H추세"):
        want = "위" if side == "LONG" else "아래"
        if f["4H추세"] != want:
            return {"ok": False, "active": True,
                    "reason": f"국면 가드: 4H 추세({f['4H추세']}) 반대 방향 진입"}
    return {"ok": True, "reason": "국면 가드 통과", "active": True}
