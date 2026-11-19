"""AI 맥락 필터 — v7 신호 후보 하나를 Gemini에게 문맥과 함께 보내 PASS/REJECT를 받는다.

왜 이 부품이 필요한가 (README '구조 재설계 실험' (6))
- 기계 규칙만으로는 시간봉·종목을 넘어 재현되는 플러스 조합이 없다.
- 후보1(30분 M1 · 아시아 3ATR·4R·96봉)의 143건은 4R 도달 18건이 전체 +29.9R 중 +54.5R를 만든다.
  나머지는 83건 전액 손절(−83R)과 44건 시간 청산이다. 상위 10% 거래가 전부를 만든다.
- 오라클로 최악 10%(14건)만 제거하면 평균 R이 +0.209 → +0.362로 올라 부트스트랩 95% 구간이 0을 넘는다.
- 그래서 이 필터의 합격선은 **"7건 중 1건꼴로 손실 후보를 맞게 거르고, 4R 승자를 죽이지 않는 것"** 이다.

설계 원칙
1) 미래 참조 없음 — 문맥은 신호 봉(i)까지의 캔들·엔진 스냅숏만 쓴다. 진입·청산은 `backtest.simulate`가 한다.
2) 판정만 한다 — 사이즈·손절·목표를 바꾸지 않는다. 규칙 엔진이 세운 자리를 AI가 다시 설계하지 않는다.
3) 실패는 통과(on_error=pass) — AI 장애가 매매를 멈추게 하지 않는다. 대신 review에 error를 남겨 집계한다.
4) 호출 한도 — 프롬프트 해시 캐시 + 최소 간격(기본 1.5초) + 429/503 백오프. 같은 문맥은 두 번 부르지 않는다.

사용 예
    python -m backend.ai_filter --json data/btc_30m_60000.json --native \\
        --preset nominee --json-out out/ai_filter_nominee.json
    GEMINI_MODEL=gemini-3.5-flash-lite python -m backend.server      # 라이브(/api/ai-filter)
"""

import argparse
import hashlib
import json
import math
import os
import random
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .backtest import (BacktestConfig, _target_price, in_session, load_candles, session_hours,
                       simulate, summarize)
from .providers import ProviderError, make_provider
from .strategy import Engine, Params, atr_series, ema_series, resample

KST = timezone(timedelta(hours=9))
CACHE_DIR = Path(__file__).resolve().parent.parent / "data"

SYSTEM_PROMPT_TEMPLATE = """당신은 {symbol} 무기한 선물 {tf_label} 단타 후보의 문맥 심사역입니다.
후보는 규칙 엔진(더블 S + MDS v7)이 세운 M1 자리이고, 진입은 다음 봉 시가, 손절·목표는 문맥 '거래구조'에 적힌 값입니다.
당신이 답할 것은 하나입니다 — loss_prob: "이 후보가 목표에 닿기 전에 손절로 끝날 확률" 0~100.

평가 기준(50에서 시작해 가감, 근거가 없으면 50 근처에 그대로 두세요):
1. 목표 경로(가장 중요): 1R(손절폭과 같은 거리) 안쪽에 반대편 1H 매물대나 유동성 레벨이 버티면 +20. 1~1.5R 안쪽이면 +10.
   3~8 ATR 떨어진 장애물은 정상입니다 — 4R 목표(12 ATR)에는 장애물이 있는 게 보통이니 감점하지 마세요.
2. 상위 추세: 4H EMA50과 {tf_ema} EMA20/50 배열이 신호와 반대면 +15, 순응하면 −10.
3. 위치: 48·96봉 범위의 극단(상위 85%에서 롱, 하위 15%에서 숏)이면 추격 +10. 존 리테스트·EMA 근처 되돌림이면 −5.
4. 소진: CCI 절대값 200 초과 +10, 볼린저 폭이 10봉 평균의 1.6배 초과 +10, 거래량 3배 이상 급증 봉 직후 +5.
5. 변동성: ATR%가 0.15 미만이면 시간손절 위험 +10, 1.2 초과면 손절 대비 노이즈 +5.
6. 시간대: 아시아 세션 후반(06-08 UTC)은 유럽 개장 전 변동성 +5.

보정: 실제로 REJECT는 전체 후보의 10~15%만 나와야 정상입니다. 기본값 50을 함부로 올리지 말고,
위 근거를 숫자로 댈 수 있을 때만 60을 넘기세요. loss_prob 60 이상이면 REJECT, 59 이하면 PASS입니다.

출력은 JSON 객체 하나만: {"loss_prob": 0~100 정수, "decision":"PASS"|"REJECT", "reason":"한국어 1~2문장, 숫자 근거 포함", "risk_flags":["..."]}
뉴스·펀더멘털은 제공되지 않았으니 지어내지 마세요."""


def system_prompt(symbol: str = "BTCUSDT", tf_label: str = "30분봉") -> str:
    """심사 지시문 — 루브릭(가중치·문턱·출력 형식)은 30분 BTC에서 정한 그대로 두고,

    문맥에 적히는 종목·시간봉 이름만 실제 시장으로 바꾼다("30분봉" → "1시간봉"). 추세 항의
    "30분 EMA20/50"처럼 봉을 빼고 쓰는 자리는 "1시간 EMA20/50"으로 맞춘다.
    기본값(system_prompt())은 이전 문자열과 같다(기존 캐시·테스트와 호환).
    """
    tf_ema = tf_label[:-1] if tf_label.endswith("봉") else tf_label
    return (SYSTEM_PROMPT_TEMPLATE.replace("{tf_label}", tf_label)
            .replace("{tf_ema}", tf_ema).replace("{symbol}", symbol))


SYSTEM_PROMPT = system_prompt()      # 기본값(30분 BTC) 지시문


# ----------------------------------------------------------------------------- 설정

@dataclass
class FilterConfig:
    provider: str = ""              # "" = backend/.env의 LLM_PROVIDER(auto)
    model: str = ""                 # "" = 제공자 기본 모델
    min_interval: float = 2.0       # 호출 사이 최소 간격(초) — 무료 한도(분당) 보호
    cache_path: Optional[str] = str(CACHE_DIR / "ai_cache.json")
    max_calls: int = 0              # 0 = 제한 없음. 넘으면 남은 후보는 통과 처리(집계에 남김)
    on_error: str = "pass"          # pass | reject — AI 장애 시 기본은 통과
    temperature: float = 0.0
    max_output_tokens: int = 512
    recent_bars: int = 12
    loss_threshold: float = 60.0    # loss_prob가 이 값 이상이면 REJECT (인샘플에서 고른 운용점)
    prompt_version: str = "ctx-v2"
    symbol: str = "BTCUSDT"         # 지시문·문맥에 적는 종목(타 종목 교차 검증용)
    tf_label: str = "30분봉"         # 지시문에 적는 시간봉 이름

    def resolved(self) -> "FilterConfig":
        return FilterConfig(**{**asdict(self)})


def decision_aliases(raw: str) -> Optional[str]:
    """LLM이 쓰는 여러 표현을 PASS/REJECT로 접는다. 모르는 값은 None(지어내지 않는다)."""
    word = (raw or "").strip().upper()
    if word in ("PASS", "APPROVE", "APPROVED", "KEEP", "ALLOW", "GO", "통과", "승인"):
        return "PASS"
    if word in ("REJECT", "REJECTED", "BLOCK", "SKIP", "DROP", "NO", "거부", "제외", "탈락"):
        return "REJECT"
    return None


def decide(ai_result: Dict, threshold: float = 60.0) -> str:
    """loss_prob가 있으면 문턱으로 다시 접는다(같은 확률 → 같은 판정, 재현 가능)."""
    prob = ai_result.get("loss_prob")
    if prob is not None:
        return "REJECT" if float(prob) >= float(threshold) else "PASS"
    return ai_result.get("decision", "PASS")


def parse_decision(raw: str, threshold: float = 60.0) -> Dict:
    """모델 응답 → {"decision","reason","confidence","risk_flags"}. 형식이 깨지면 ValueError."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    obj = None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                obj = json.loads(match.group(0))
            except json.JSONDecodeError:
                obj = None
    if not isinstance(obj, dict):
        raise ValueError("JSON 객체가 아닙니다")
    prob = obj.get("loss_prob", obj.get("loss_probability"))
    try:
        prob = None if prob is None else max(0.0, min(100.0, float(prob)))
    except (TypeError, ValueError):
        prob = None
    decision = decision_aliases(str(obj.get("decision", obj.get("verdict", ""))))
    if prob is not None:
        # 확률이 오면 확률을 기준으로 다시 접는다(같은 문턱·같은 규칙 = 재현 가능)
        decision = "REJECT" if prob >= threshold else "PASS"
    if decision is None:
        raise ValueError(f"decision 값이 PASS/REJECT가 아닙니다: {obj.get('decision')!r}")
    try:
        conf = max(0, min(100, int(obj.get("confidence", 50))))
    except (TypeError, ValueError):
        conf = 50
    flags = obj.get("risk_flags")
    if not isinstance(flags, list):
        flags = []
    return {
        "decision": decision,
        "loss_prob": prob,
        "reason": str(obj.get("reason", "")).strip()[:400],
        "confidence": conf,
        "risk_flags": [str(f)[:60] for f in flags[:6]],
    }


# ----------------------------------------------------------------------------- 문맥 만들기

def _r(x, nd: int = 2):
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return round(v, nd)


def _rsi(closes: Sequence[float], period: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains += max(diff, 0.0)
        losses += max(-diff, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(diff, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-diff, 0.0)) / period
        out[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return out


def prepare_series(candles: Sequence[Dict]) -> Dict:
    """문맥에 쓸 지표를 한 번만 계산해 둔다(후보마다 다시 돌리지 않게)."""
    closes = [c["c"] for c in candles]
    atr = atr_series(candles, 14)
    atr_pct = [(_r(a / closes[i] * 100, 4) if a else None) for i, a in enumerate(atr)]
    return {
        "atr": atr,
        "atr_pct": atr_pct,
        "ema20": ema_series(closes, 20),
        "ema50": ema_series(closes, 50),
        "rsi14": _rsi(closes, 14),
    }


def _session_label(ts_ms: int) -> str:
    h = (int(ts_ms) // 3_600_000) % 24
    if 0 <= h < 8:
        return f"asia(00-08 UTC, {h}시간 경과)"
    if 7 <= h < 16:
        return f"london(07-16 UTC, {h}시간)"
    if 12 <= h < 21:
        return f"ny(12-21 UTC, {h}시간)"
    return f"late(21-24 UTC, {h}시간)"


def _bar_row(bar: Dict) -> Dict:
    o, h, l, c = bar["o"], bar["h"], bar["l"], bar["c"]
    dt = datetime.fromtimestamp(int(bar["t"]) / 1000, tz=timezone.utc)
    return {
        "시각": dt.strftime("%m-%d %H:%M"),
        "o": _r(o), "h": _r(h), "l": _r(l), "c": _r(c),
        "v": _r(bar.get("v", 0.0), 1),
        "방향": "양" if c > o else ("음" if c < o else "보합"),
    }


def _range_stats(candles: Sequence[Dict], i: int, span: int) -> Dict:
    lo = max(0, i - span + 1)
    window = candles[lo:i + 1]
    if not window:
        return {}
    hi = max(b["h"] for b in window)
    low = min(b["l"] for b in window)
    close = window[-1]["c"]
    pos = (close - low) / (hi - low) * 100 if hi > low else 50.0
    return {"고": _r(hi), "저": _r(low), "현재위치%": _r(pos, 1)}


def _atr_percentile(series: Dict, i: int, span: int = 100) -> Optional[float]:
    pct = (series or {}).get("atr_pct") or []
    if i >= len(pct) or pct[i] is None:
        return None
    vals = [v for v in pct[max(0, i - span + 1):i + 1] if v is not None]
    if len(vals) < 10:
        return None
    below = sum(1 for v in vals if v <= series["atr_pct"][i])
    return round(below / len(vals) * 100, 0)


def _streak(candles: Sequence[Dict], i: int) -> int:
    """연속 양봉(+)·음봉(−) 수. 0이면 보합."""
    count = 0
    for k in range(i, max(-1, i - 12), -1):
        c = candles[k]
        if c["c"] > c["o"]:
            sign = 1
        elif c["c"] < c["o"]:
            sign = -1
        else:
            break
        if count == 0:
            count = sign
        elif (count > 0) != (sign > 0):
            break
        else:
            count += sign
    return count


def build_context(candles: Sequence[Dict], signal: Dict, snap: Optional[Dict] = None,
                  series: Optional[Dict] = None, cfg: Optional[FilterConfig] = None,
                  symbol: str = "BTCUSDT", trade_cfg: Optional[BacktestConfig] = None) -> Dict:
    """신호 봉(i)까지의 정보만으로 심사용 문맥을 만든다 — 미래 봉은 읽지 않는다.

    trade_cfg를 주면 **체결 모형이 실제로 쓸 목표**를 문맥에 넣는다(예: 후보1의 고정 4R).
    신호 자체의 구조 목표(1.5R 대체)를 그대로 보여주면 AI가 다른 목표를 심사하게 된다.
    """
    cfg = cfg or FilterConfig()
    i = int(signal["index"])
    bar = candles[i]
    side = signal["side"]
    entry = float(signal["entry"])
    stop = float(signal["stop"])
    target = float(signal["target"])
    risk = abs(entry - stop)
    target_label = "신호 구조 목표"
    if trade_cfg is not None and risk > 0:
        effective = _target_price(side, entry, risk, signal, trade_cfg)
        if trade_cfg.target_mode == "r":
            target_label = "고정 %gR" % trade_cfg.target_r
        elif effective is not None and abs(effective - target) > 1e-9:
            target_label = "체결 모형 목표(상한 적용)"
        if effective is not None:
            target = float(effective)
    atr = float(signal.get("atr") or (snap or {}).get("atr") or 0.0) or None
    snap = snap or {}
    series = series or {}
    atr_ref = atr or 1e-9

    zone_rows = {"저항": [], "지지": []}
    for z in (snap.get("res") or []):
        top, bot = float(z["top"]), float(z["bot"])
        dist = (bot - entry) / atr_ref if side == "LONG" and top > entry else (
            (entry - top) / atr_ref if side == "SHORT" and bot < entry else None)
        zone_rows["저항"].append({"top": _r(top), "bot": _r(bot), "확정": bool(z.get("frozen")),
                                 "거리ATR": _r(dist, 2)})
    for z in (snap.get("sup") or []):
        top, bot = float(z["top"]), float(z["bot"])
        dist = (bot - entry) / atr_ref if side == "LONG" and top < entry else (
            (entry - bot) / atr_ref if side == "SHORT" and bot > entry else None)
        zone_rows["지지"].append({"top": _r(top), "bot": _r(bot), "확정": bool(z.get("frozen")),
                                 "거리ATR": _r(dist, 2)})
    for key in zone_rows:
        zone_rows[key].sort(key=lambda z: z["거리ATR"] if z["거리ATR"] is not None else 1e9)
        zone_rows[key] = zone_rows[key][:3]
    inside = any(float(z["bot"]) <= entry <= float(z["top"])
                 for z in (snap.get("res") or []) + (snap.get("sup") or []))

    bsl = sorted([float(x) for x in (snap.get("bsl") or [])])
    ssl = sorted([float(x) for x in (snap.get("ssl") or [])], reverse=True)
    liq: Dict[str, List[Dict]] = {"위BSL": [], "아래SSL": []}
    if side == "LONG":
        liq["위BSL"] = [{"가격": _r(p), "거리ATR": _r((p - entry) / atr_ref, 2)} for p in bsl if p > entry][:2]
        liq["아래SSL"] = [{"가격": _r(p), "거리ATR": _r((entry - p) / atr_ref, 2)} for p in ssl if p < entry][:2]
    else:
        liq["아래SSL"] = [{"가격": _r(p), "거리ATR": _r((entry - p) / atr_ref, 2)} for p in ssl if p < entry][:2]
        liq["위BSL"] = [{"가격": _r(p), "거리ATR": _r((p - entry) / atr_ref, 2)} for p in bsl if p > entry][:2]

    # 목표까지의 경로에 놓인 장애물 — 반대편 매물대 경계와 유동성 레벨
    lo, hi = (entry, target) if target > entry else (target, entry)
    obstacles: List[Dict] = []
    for z in (snap.get("res") or []) + (snap.get("sup") or []):
        for edge in (float(z["top"]), float(z["bot"])):
            if lo < edge < hi:
                obstacles.append({"종류": "매물대", "가격": _r(edge),
                                  "거리ATR": _r(abs(edge - entry) / atr_ref, 2)})
                break
    for p in bsl + ssl:
        if lo < p < hi:
            obstacles.append({"종류": "유동성", "가격": _r(p), "거리ATR": _r(abs(p - entry) / atr_ref, 2)})
    obstacles.sort(key=lambda o: o["거리ATR"] or 1e9)

    ema20, ema50 = snap.get("ema20"), snap.get("ema50")
    cci, bbw, bbw_avg = snap.get("cci"), snap.get("bbw"), snap.get("bbw_avg")
    htf_ema = snap.get("htf_ema")
    volsma = snap.get("volsma")
    rvol = snap.get("rvol")
    rsi_list = series.get("rsi14") or []
    atr_pct = (atr / entry * 100) if atr and entry else None
    ema20_slope = None
    if series.get("ema20") and i >= 5 and series["ema20"][i] and series["ema20"][i - 5]:
        ema20_slope = (series["ema20"][i] / series["ema20"][i - 5] - 1) * 100
    recent = candles[max(0, i - cfg.recent_bars + 1):i + 1]

    context = {
        "심사시각": {
            "UTC": datetime.fromtimestamp(int(bar["t"]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "KST": datetime.fromtimestamp(int(bar["t"]) / 1000, tz=KST).strftime("%Y-%m-%d %H:%M"),
            "세션": _session_label(bar["t"]),
        },
        "후보": {"종목": symbol, "방향": side, "종류": signal.get("kind"),
                 "라벨": signal.get("label"), "등급": signal.get("grade")},
        "거래구조": {
            "신호봉종가": _r(entry), "손절": _r(stop), "목표": _r(target), "목표근거": target_label,
            "손절폭%": _r(risk / entry * 100 if entry else None),
            "손익비": _r(abs(target - entry) / risk if risk else None),
            "손절ATR배수": _r(risk / atr_ref, 2),
            "목표ATR배수": _r(abs(target - entry) / atr_ref, 2),
        },
        "변동성": {
            "ATR": _r(atr), "ATR%": _r(atr_pct, 3),
            "ATR%백분위(100봉)": _atr_percentile(series, i),
        },
        "추세": {
            "EMA20": _r(ema20), "EMA50": _r(ema50),
            "종가vsEMA20%": _r((entry - ema20) / ema20 * 100 if ema20 else None),
            "EMA20vsEMA50%": _r((ema20 - ema50) / ema50 * 100 if ema20 and ema50 else None),
            "EMA20기울기5봉%": _r(ema20_slope, 3),
            "4H_EMA50": _r(htf_ema),
            "종가vs4H_EMA50%": _r((entry - htf_ema) / htf_ema * 100 if htf_ema else None),
        },
        "모멘텀": {
            "CCI20": _r(cci, 1),
            "볼린저폭%": _r(bbw, 3),
            "볼린저폭/10봉평균": _r(bbw / bbw_avg, 2) if bbw and bbw_avg else None,
            "RSI14": _r(rsi_list[i] if i < len(rsi_list) else None, 1),
            "연속봉": _streak(candles, i),
            "신호봉몸통%": _r(abs(bar["c"] - bar["o"]) / (bar["h"] - bar["l"]) * 100
                          if bar["h"] > bar["l"] else 0, 1),
        },
        "거래량": {
            "신호봉/20봉평균": _r(rvol, 2),
            "20봉평균": _r(volsma, 1),
            "최근3봉배수": [_r((c.get("v", 0.0) / volsma), 2) if volsma else None for c in candles[max(0, i - 2):i + 1]],
        },
        "범위": {"48봉": _range_stats(candles, i, 48), "96봉": _range_stats(candles, i, 96)},
        "매물대": {**zone_rows, "현재존내부": inside},
        "유동성": liq,
        "목표경로": {
            "목표까지ATR": _r(abs(target - entry) / atr_ref, 1),
            "1R_ATR": _r(risk / atr_ref, 1),
            "1R이내장애물수": sum(1 for o in obstacles if (o["거리ATR"] or 99) <= risk / atr_ref),
            "장애물수": len(obstacles),
            "가장가까운장애물ATR": obstacles[0]["거리ATR"] if obstacles else None,
            "장애물": [o for o in obstacles if (o["거리ATR"] or 0) <= 6][:4],
        },
        "최근봉": [_bar_row(c) for c in recent],
    }
    return context


def render_prompt(context: Dict) -> str:
    return ("아래 후보와 문맥을 심사하세요. loss_prob(목표 전 손절 확률 0~100)과 "
            "decision(PASS/REJECT)을 담은 JSON 하나만 출력합니다.\n"
            "후보·문맥(JSON):\n" + json.dumps(context, ensure_ascii=False, separators=(",", ":")))


# ----------------------------------------------------------------------------- 필터 본체

class AIFilter:
    """Gemini(OpenAI 호환) 한 곳으로 후보를 심사한다. 캐시·간격·오류 처리를 안에 둔다."""

    def __init__(self, cfg: Optional[FilterConfig] = None, provider=None):
        self.cfg = (cfg or FilterConfig()).resolved()
        self.provider = provider
        self.calls = 0
        self.cache_hits = 0
        self.errors = 0
        self.budget_hits = 0
        self.latencies: List[float] = []
        self._last_call = 0.0
        self.cache: Dict[str, Dict] = self._load_cache()

    # -- 캐시 ---------------------------------------------------------------
    def _load_cache(self) -> Dict[str, Dict]:
        path = self.cfg.cache_path
        if not path or not os.path.isfile(path):
            return {}
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def save_cache(self) -> None:
        path = self.cfg.cache_path
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.cache, fh, ensure_ascii=False, indent=0)
            os.replace(tmp, path)
        except OSError:
            pass                              # 캐시를 못 써도 심사는 계속한다

    def cache_key(self, context: Dict) -> str:
        """(프롬프트 버전, 모델, 문맥) 해시 — 같은 문맥은 두 번 부르지 않는다.

        판정 기준(지시문)을 바꾸면 FilterConfig.prompt_version을 올려 캐시를 갈아 끼운다.
        """
        model = self.cfg.model or "(default)"
        payload = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(f"{self.cfg.prompt_version}|{model}|{payload}".encode("utf-8")).hexdigest()

    # -- 제공자 -------------------------------------------------------------
    def _get_provider(self):
        if self.provider is None:
            from .config import get_provider_config
            conf = get_provider_config()
            if self.cfg.model:
                conf["model"] = self.cfg.model
            conf["temperature"] = self.cfg.temperature
            conf["retries"] = 2
            self.provider = make_provider(conf)
        return self.provider

    def _throttle(self) -> None:
        gap = self.cfg.min_interval - (time.monotonic() - self._last_call)
        if gap > 0:
            time.sleep(gap)
        self._last_call = time.monotonic()

    def _failure(self, reason: str, is_error: bool = True) -> Dict:
        decision = "REJECT" if self.cfg.on_error == "reject" else "PASS"
        return {"decision": decision, "reason": reason, "confidence": 0,
                "risk_flags": ["ai_error"] if is_error else ["budget"], "cached": False,
                "error": reason if is_error else None, "budget": not is_error}

    # -- 심사 ---------------------------------------------------------------
    def review(self, signal: Dict, context: Optional[Dict] = None,
               candles: Optional[Sequence[Dict]] = None, snap: Optional[Dict] = None,
               series: Optional[Dict] = None, trade_cfg: Optional[BacktestConfig] = None) -> Dict:
        if context is None:
            if candles is None:
                raise ValueError("context 또는 candles가 필요합니다")
            if series is None:
                series = prepare_series(candles)
            context = build_context(candles, signal, snap, series, self.cfg, trade_cfg=trade_cfg)
        prompt = render_prompt(context)
        key = self.cache_key(context)
        if key in self.cache:
            self.cache_hits += 1
            hit = dict(self.cache[key])
            hit["cached"] = True
            return hit
        if self.cfg.max_calls and self.calls >= self.cfg.max_calls:
            self.budget_hits += 1
            return self._failure(f"호출 예산({self.cfg.max_calls}) 소진 — 통과 처리", is_error=False)
        self._throttle()
        self.calls += 1
        t0 = time.monotonic()
        try:
            raw = self._get_provider().complete(system_prompt(self.cfg.symbol, self.cfg.tf_label), prompt)
            parsed = parse_decision(raw, self.cfg.loss_threshold)
        except ProviderError as exc:
            self.errors += 1
            return self._failure(f"AI 호출 실패: {str(exc)[:160]}")
        except ValueError as exc:
            self.errors += 1
            return self._failure(f"AI 응답 해석 실패: {exc}")
        finally:
            self.latencies.append(time.monotonic() - t0)
        result = {**parsed, "cached": False, "error": None,
                  "model": (self._get_provider().config or {}).get("model")}
        self.cache[key] = {k: v for k, v in result.items() if k != "cached"}
        self.save_cache()
        return result


def review_signals(candles: Sequence[Dict], signals: Sequence[Dict], ai: AIFilter,
                   series: Optional[Dict] = None, capture: Optional[Dict] = None,
                   log_every: int = 0, emit=None,
                   trade_cfg: Optional[BacktestConfig] = None,
                   symbol: str = "BTCUSDT") -> Tuple[List[Dict], List[Dict]]:
    """신호마다 심사해 (통과 신호, 심사 기록)을 돌려준다. 기록은 신호 dict + ai 판정 + 문맥."""
    series = series if series is not None else prepare_series(candles)
    capture = capture or {}
    kept: List[Dict] = []
    reviews: List[Dict] = []
    for n, sig in enumerate(signals, 1):
        snap = capture.get(sig["index"])
        context = build_context(candles, sig, snap, series, ai.cfg, trade_cfg=trade_cfg, symbol=symbol)
        verdict = ai.review(sig, context)
        reviews.append({
            "index": sig["index"], "time": sig["time"], "side": sig["side"],
            "kind": sig["kind"], "label": sig.get("label"), "entry": sig["entry"],
            "stop": sig["stop"], "target": sig["target"], "rr": sig.get("rr"),
            "ai": verdict, "context": context,
        })
        if verdict.get("decision") == "PASS":
            kept.append(sig)
        if log_every and n % log_every == 0:
            line = (f"  심사 {n}/{len(signals)} · 통과 {len(kept)} · 거부 {n - len(kept)} · "
                    f"호출 {ai.calls} · 캐시 {ai.cache_hits} · 오류 {ai.errors}")
            print(line, flush=True)
            if emit:
                emit(line)
    ai.save_cache()
    return kept, reviews


# ----------------------------------------------------------------------------- 성과 비교

def _boot_avg_r(values: Sequence[float], draws: int = 2000, seed: int = 7) -> Dict:
    """평균 R 부트스트랩 95% 구간 — 표본이 작아 '플러스'를 단정하지 않기 위한 장치."""
    vals = [v for v in values if v is not None]
    if len(vals) < 5:
        return {"mean": round(sum(vals) / len(vals), 3) if vals else None,
                "lo": None, "hi": None, "p_positive": None}
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        sample = [vals[rng.randrange(len(vals))] for _ in range(len(vals))]
        means.append(sum(sample) / len(sample))
    means.sort()
    return {"mean": round(sum(vals) / len(vals), 3),
            "lo": round(means[int(draws * 0.025)], 3),
            "hi": round(means[int(draws * 0.975)], 3),
            "p_positive": round(sum(1 for m in means if m > 0) / draws, 3)}


def _block(trades: Sequence[Dict], cfg: BacktestConfig) -> Dict:
    s = summarize(trades, cfg, cfg.initial_equity)
    return {
        "trades": s["trades"], "win": s["win_rate_pct"], "pf": s["profit_factor"],
        "net_pct": round(sum(t["pnl"] for t in trades) / cfg.initial_equity * 100, 2),
        "avg_r": s["avg_r"], "mdd": s["max_drawdown_pct"],
        "targets": sum(1 for t in trades if t["reason"] == "TARGET"),
        "stops": sum(1 for t in trades if t["reason"] == "STOP"),
        "times": sum(1 for t in trades if t["reason"] == "TIME"),
        "boot": _boot_avg_r([t["r"] for t in trades]),
    }


def random_control(trades: Sequence[Dict], kept_n: int, observed_avg_r: Optional[float],
                   draws: int = 2000, seed: int = 11) -> Dict:
    """같은 건수만 무작위로 거부했을 때의 평균 R 분포 — AI 선별이 우연보다 나은지 보는 대조군."""
    r_vals = [t["r"] for t in trades if t.get("r") is not None]
    if len(r_vals) < 10 or not kept_n or observed_avg_r is None:
        return {}
    kept_n = min(kept_n, len(r_vals))
    rng = random.Random(seed)
    sims = []
    for _ in range(draws):
        sample = rng.sample(r_vals, kept_n)
        sims.append(sum(sample) / kept_n)
    sims.sort()
    return {
        "kept_n": kept_n,
        "observed_avg_r": round(observed_avg_r, 3),
        "random_median": round(sims[draws // 2], 3),
        "random_p95": round(sims[int(draws * 0.95)], 3),
        "p_value": round(sum(1 for v in sims if v >= observed_avg_r) / draws, 3),
    }


def compare(candles: Sequence[Dict], signals: Sequence[Dict], kept: Sequence[Dict],
            reviews: Sequence[Dict], cfg: BacktestConfig, params: Params,
            split: float = 0.6, start_index: int = 0) -> Dict:
    """필터 전/후를 같은 체결 모형으로 돌려 비교한다. 인샘플/아웃샘플을 따로 낸다.

    start_index > 0이면 그 봉부터를 한 구간으로 본다(최근 구간만 심사한 교차 검증용).
    신호는 호출부에서 이미 그 구간만 넘기므로, 인샘플/아웃샘플 경계만 그 구간 안에서 다시 잡는다.
    """
    base = simulate(candles, signals, cfg, params)
    filt = simulate(candles, kept, cfg, params)
    split_i = start_index + int((len(candles) - start_index) * split)
    split_t = candles[min(len(candles) - 1, max(0, split_i))]["t"]

    def split_trades(trades):
        return ([t for t in trades if t["entry_time"] < split_t],
                [t for t in trades if t["entry_time"] >= split_t])

    base_is, base_os = split_trades(base["trades"])
    filt_is, filt_os = split_trades(filt["trades"])

    # 신호 시각·방향으로 심사 기록을 거래에 붙인다
    verdicts = {(r["time"], r["side"]): r["ai"] for r in reviews}
    for t in base["trades"]:
        v = verdicts.get((t["signal_time"], t["side"]))
        t["ai"] = v["decision"] if v else None
        t["ai_reason"] = v.get("reason") if v else None

    r_sorted = sorted(t["r"] for t in base["trades"])
    p10 = r_sorted[max(0, int(len(r_sorted) * 0.10) - 1)] if r_sorted else None
    worst = [t for t in base["trades"] if p10 is not None and t["r"] <= p10]
    winners4 = [t for t in base["trades"] if t["reason"] == "TARGET" and (t["r"] or 0) >= 3.5]

    def rate(group):
        reviewed = [t for t in group if t["ai"]]
        rejected = [t for t in reviewed if t["ai"] == "REJECT"]
        return {"n": len(group), "reviewed": len(reviewed), "rejected": len(rejected),
                "reject_pct": round(len(rejected) / len(reviewed) * 100, 1) if reviewed else None}

    return {
        "baseline": {"all": _block(base["trades"], cfg), "is": _block(base_is, cfg),
                     "os": _block(base_os, cfg)},
        "filtered": {"all": _block(filt["trades"], cfg), "is": _block(filt_is, cfg),
                     "os": _block(filt_os, cfg)},
        "signals": {"total": len(signals), "kept": len(kept), "rejected": len(signals) - len(kept),
                    "reject_pct": round((len(signals) - len(kept)) / len(signals) * 100, 1) if signals else None},
        "reviews": {
            "count": len(reviews),
            "pass": sum(1 for r in reviews if r["ai"]["decision"] == "PASS"),
            "reject": sum(1 for r in reviews if r["ai"]["decision"] == "REJECT"),
            "errors": sum(1 for r in reviews if r["ai"].get("error")),
            "reject_pct": round(sum(1 for r in reviews if r["ai"]["decision"] == "REJECT")
                                / len(reviews) * 100, 1) if reviews else None,
        },
        "hit": {
            "worst_decile": {"n": len(worst), "cut_r": p10, **rate(worst)},
            "four_r_winners": {"n": len(winners4), **rate(winners4)},
            "all_trades": rate(base["trades"]),
            "losing_trades": rate([t for t in base["trades"] if t["pnl"] < 0]),
            "winning_trades": rate([t for t in base["trades"] if t["pnl"] > 0]),
        },
        "random_control": {
            "all": random_control(base["trades"], len(filt["trades"]), _block(filt["trades"], cfg)["avg_r"]),
            "is": random_control(base_is, len(filt_is), _block(filt_is, cfg)["avg_r"]),
            "os": random_control(base_os, len(filt_os), _block(filt_os, cfg)["avg_r"]),
        },
        "skipped": {"baseline": base["stats"].get("skipped"), "filtered": filt["stats"].get("skipped")},
        "trades": base["trades"],
        "filtered_trades": filt["trades"],
        "unmatched_reviews": [r for r in reviews if (r["time"], r["side"]) not in
                              {(t["signal_time"], t["side"]) for t in base["trades"]}],
    }


def sweep_thresholds(candles: Sequence[Dict], signals: Sequence[Dict], reviews: Sequence[Dict],
                     cfg: BacktestConfig, params: Params, split: float = 0.6,
                     thresholds: Sequence[float] = (45, 50, 55, 60, 65, 70, 80),
                     start_index: int = 0) -> List[Dict]:
    """문턱별 필터 성과 — 운용점은 인샘플에서만 고르고, 아웃샘플은 그 문턱 그대로 확인한다."""
    split_i = start_index + int((len(candles) - start_index) * split)
    split_t = candles[min(len(candles) - 1, max(0, split_i))]["t"]
    rows: List[Dict] = []
    for th in thresholds:
        kept = [s for s in signals
                if decide(next((r["ai"] for r in reviews
                                if r["time"] == s["time"] and r["side"] == s["side"]), {}), th) == "PASS"]
        res = simulate(candles, kept, cfg, params)
        trades = res["trades"]
        is_t = [t for t in trades if t["entry_time"] < split_t]
        os_t = [t for t in trades if t["entry_time"] >= split_t]
        rows.append({"threshold": th, "kept": len(kept),
                     "rejected": len(reviews) - len(kept),
                     "all": _block(trades, cfg), "is": _block(is_t, cfg), "os": _block(os_t, cfg)})
    return rows


def prob_histogram(reviews: Sequence[Dict]) -> List[Dict]:
    """loss_prob 분포 — 모델이 극단에만 몰리는지(보정이 되는지) 보는 용도."""
    buckets = [(0, 20), (20, 40), (40, 50), (50, 60), (60, 70), (70, 80), (80, 101)]
    rows = []
    for lo, hi in buckets:
        vals = [r["ai"].get("loss_prob") for r in reviews
                if r["ai"].get("loss_prob") is not None and lo <= r["ai"]["loss_prob"] < hi]
        rows.append({"구간": f"{lo}~{hi if hi <= 100 else 100}", "n": len(vals)})
    return rows


def _fmt_side(block: Dict) -> str:
    def f(v, nd=2):
        return "-" if v is None else (f"{v:.{nd}f}" if isinstance(v, (int, float)) else str(v))
    return (f"{block['trades']:>3d}건 · 승률 {f(block['win'],1):>5s}% · PF {f(block['pf']):>4s} · "
            f"순손익 {block['net_pct']:>7.2f}% · 평균R {f(block['avg_r'],3):>7s} · MDD {f(block['mdd'],1):>5s}% "
            f"· 4R {block['targets']:>2d}/손절 {block['stops']:>2d}/시간 {block['times']:>2d} "
            f"· 부트 95% [{f(block['boot']['lo'],3)}, {f(block['boot']['hi'],3)}]")


def report_text(result: Dict) -> str:
    b, f = result["baseline"], result["filtered"]
    rv, hit = result["reviews"], result["hit"]
    lines = [
        f"■ 심사 {rv['count']}건 — PASS {rv['pass']} · REJECT {rv['reject']} ({rv['reject_pct']}%) · 오류 {rv['errors']}",
        f"  신호 {result['signals']['total']}건 중 통과 {result['signals']['kept']}건(체결 후보)",
        "",
        f"  전체    기준: {_fmt_side(b['all'])}",
        f"          필터: {_fmt_side(f['all'])}",
        f"  인샘플  기준: {_fmt_side(b['is'])}",
        f"          필터: {_fmt_side(f['is'])}",
        f"  아웃샘플 기준: {_fmt_side(b['os'])}",
        f"          필터: {_fmt_side(f['os'])}",
        "",
        "■ 걸러낸 대상 (기준 체결 거래에 심사 결과를 붙임)",
        f"  최악 10%({hit['worst_decile']['n']}건, R≤{hit['worst_decile']['cut_r']}): "
        f"심사 {hit['worst_decile']['reviewed']}건 중 거부 {hit['worst_decile']['rejected']}건 "
        f"({hit['worst_decile']['reject_pct']}%)",
        f"  4R 승자({hit['four_r_winners']['n']}건): 심사 {hit['four_r_winners']['reviewed']}건 중 "
        f"거부 {hit['four_r_winners']['rejected']}건 ({hit['four_r_winners']['reject_pct']}%)",
        f"  손실 거래: 심사 {hit['losing_trades']['reviewed']}건 중 거부 {hit['losing_trades']['rejected']}건 "
        f"({hit['losing_trades']['reject_pct']}%) · 이익 거래: 거부 {hit['winning_trades']['rejected']}/{hit['winning_trades']['reviewed']} "
        f"({hit['winning_trades']['reject_pct']}%)",
        f"  체결로 이어지지 않은 신호(포지션 중·세션 밖) 심사 {len(result['unmatched_reviews'])}건",
    ]
    sweep = result.get("sweep") or []
    if sweep:
        lines += ["", "■ 문턱(loss_prob ≥ X면 거부) — 운용점은 인샘플에서 고른다"]
        lines.append(f"  {'문턱':>4s} {'거부':>4s} | {'IS건':>4s} {'IS PF':>5s} {'IS손익%':>7s} {'IS평균R':>7s} "
                     f"{'IS 4R':>5s} | {'OS건':>4s} {'OS PF':>5s} {'OS손익%':>7s} {'OS평균R':>7s}")
        for row in sweep:
            i, o = row["is"], row["os"]
            def f(v, nd=2):
                return "-" if v is None else f"{v:.{nd}f}"
            lines.append(f"  {row['threshold']:>4.0f} {row['rejected']:>4d} | {i['trades']:>4d} {f(i['pf']):>5s} "
                         f"{i['net_pct']:>7.2f} {f(i['avg_r'],3):>7s} {i['targets']:>5d} | {o['trades']:>4d} "
                         f"{f(o['pf']):>5s} {o['net_pct']:>7.2f} {f(o['avg_r'],3):>7s}")
    rc = (result.get("random_control") or {}).get("os") or {}
    if rc:
        lines += ["", "■ 무작위 거부 대조군(아웃샘플, 같은 건수 " + str(rc["kept_n"]) + "건만 남김)",
                  f"  실제 평균 R {rc['observed_avg_r']:+.3f} · 무작위 중앙 {rc['random_median']:+.3f} · "
                  f"상위 5% {rc['random_p95']:+.3f} · p={rc['p_value']}"]
    hist = result.get("prob_histogram") or []
    if hist:
        lines += ["", "■ loss_prob 분포 " + " · ".join(f"{h['구간']}:{h['n']}" for h in hist)]
    return "\n".join(lines)


# ----------------------------------------------------------------------------- 프리셋·CLI

def nominee_setup() -> Tuple[Params, BacktestConfig]:
    """후보1 — 30분 M1 · 아시아(00-08 UTC) · 3ATR · 4R · 96봉 시간손절 (README 실측 (4))."""
    params = Params(stop_mode="atr", stop_atr=3.0)
    cfg = BacktestConfig(allow_kinds=("M1",), target_mode="r", target_r=4.0,
                         max_bars=96, session_filter="asia")
    return params, cfg


def select_signals(candles: Sequence[Dict], signals: Sequence[Dict],
                   cfg: BacktestConfig) -> List[Dict]:
    """체결 모형이 고려할 신호만 남긴다(종류·세션·다음 봉 존재). 포지션 중복은 simulate가 걷는다.

    이렇게 줌아야 AI 호출이 실제 후보 수(후보1: 143건 + 포지션 중복 9건)에 맞고,
    '심사했는데 체결되지 않은 신호'를 성과 탓으로 오해하지 않는다.
    """
    hours = session_hours(cfg)
    out: List[Dict] = []
    for s in signals:
        if s["kind"] not in cfg.allow_kinds:
            continue
        i = int(s["index"])
        if i + 1 >= len(candles) or not in_session(candles[i + 1]["t"], hours):
            continue
        out.append(s)
    return out


def load(path: str) -> List[Dict]:
    candles = json.load(open(path, encoding="utf-8"))
    candles.sort(key=lambda c: c["t"])
    return candles


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="v7 후보 → Gemini 맥락 필터 실측")
    ap.add_argument("--json", default=None, help="캔들 JSON(없으면 거래소에서 받는다)")
    ap.add_argument("--source", default="bitget")
    ap.add_argument("--limit", type=int, default=60000)
    ap.add_argument("--tf", type=int, default=0, choices=[0, 15, 30, 60], help="0이면 JSON 봉 그대로")
    ap.add_argument("--native", action="store_true", help="JSON을 그 봉 그대로 쓴다")
    ap.add_argument("--preset", default="nominee", choices=["nominee", "none"])
    ap.add_argument("--base-p", default=None, help="Params 덮어쓰기(JSON)")
    ap.add_argument("--base-c", default=None, help="BacktestConfig 덮어쓰기(JSON)")
    ap.add_argument("--split", type=float, default=0.6)
    ap.add_argument("--model", default="", help="덮어쓸 모델(기본은 .env GEMINI_MODEL)")
    ap.add_argument("--min-interval", type=float, default=2.0, help="호출 사이 최소 간격(초)")
    ap.add_argument("--cache", default=str(CACHE_DIR / "ai_cache.json"))
    ap.add_argument("--max-calls", type=int, default=0, help="이번 실행의 최대 호출 수(초과분은 통과)")
    ap.add_argument("--on-error", default="pass", choices=["pass", "reject"])
    ap.add_argument("--threshold", type=float, default=60.0, help="loss_prob가 이 값 이상이면 REJECT")
    ap.add_argument("--no-sweep", action="store_true", help="문턱 표를 생략")
    ap.add_argument("--only-os", action="store_true", help="아웃샘플 신호만 심사(프롬프트를 인샘플로 고정)")
    ap.add_argument("--only-is", action="store_true", help="인샘플 신호만 심사")
    ap.add_argument("--dry-run", action="store_true", help="프롬프트만 만들고 호출하지 않는다")
    ap.add_argument("--save-prompts", default=None, help="만든 프롬프트를 JSONL로 저장")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args(argv)

    candles = (load(args.json) if args.json else
               load_candles("BTCUSDT", "15", args.limit, args.source)["candles"])
    if args.tf and not args.native:
        candles = resample(candles, args.tf)
    params, cfg = nominee_setup()
    if args.preset == "none":
        params, cfg = Params(), BacktestConfig()
    if args.base_p:
        params = Params(**{**params.to_dict(), **json.loads(args.base_p)})
    if args.base_c:
        cfg = BacktestConfig(**{**cfg.to_dict(), **json.loads(args.base_c)})

    engine = Engine(params)
    signals = engine.run(candles)          # capture_at=None → 신호 봉마다 상태 스냅숏
    signals = select_signals(candles, signals, cfg)
    split_i = int(len(candles) * args.split)
    split_t = candles[split_i]["t"]
    if args.only_os:
        signals = [s for s in signals if s["time"] >= split_t]
    if args.only_is:
        signals = [s for s in signals if s["time"] < split_t]
    print(f"■ {len(candles)}봉 · 신호 {len(signals)}건 · 모델 {args.model or os.getenv('GEMINI_MODEL') or 'gemini 기본'}"
          f" · 캐시 {args.cache}")

    series = prepare_series(candles)
    if args.dry_run:
        prompts = [render_prompt(build_context(candles, s, engine.capture.get(s["index"]), series,
                                               trade_cfg=cfg))
                   for s in signals]
        if args.save_prompts:
            with open(args.save_prompts, "w", encoding="utf-8") as fh:
                for s, p in zip(signals, prompts):
                    fh.write(json.dumps({"time": s["time"], "side": s["side"], "prompt": p},
                                        ensure_ascii=False) + "\n")
        print(f"  호출 없이 프롬프트 {len(prompts)}개 생성 · 평균 {sum(len(p) for p in prompts)//max(1,len(prompts))}자")
        return 0

    ai = AIFilter(FilterConfig(model=args.model, min_interval=args.min_interval,
                               cache_path=args.cache, max_calls=args.max_calls,
                               on_error=args.on_error, loss_threshold=args.threshold))
    kept, reviews = review_signals(candles, signals, ai, series, engine.capture, log_every=20,
                                   trade_cfg=cfg)
    result = compare(candles, signals, kept, reviews, cfg, params, args.split)
    result["threshold"] = args.threshold
    result["review_log"] = [{
        "time": r["time"], "side": r["side"], "kind": r["kind"], "entry": r["entry"],
        "loss_prob": r["ai"].get("loss_prob"), "decision": r["ai"]["decision"],
        "reason": r["ai"].get("reason"), "error": r["ai"].get("error"), "cached": r["ai"].get("cached"),
    } for r in reviews]
    result["prob_histogram"] = prob_histogram(reviews)
    if not args.no_sweep:
        result["sweep"] = sweep_thresholds(candles, signals, reviews, cfg, params, args.split)
    result["meta"] = {
        "bars": len(candles), "tf": args.tf or "native", "split": args.split,
        "model": (ai.provider.config.get("model") if ai.provider else None) or args.model or None,
        "calls": ai.calls, "cache_hits": ai.cache_hits, "errors": ai.errors,
        "budget_hits": ai.budget_hits,
        "avg_latency_s": round(sum(ai.latencies) / len(ai.latencies), 2) if ai.latencies else None,
        "params": params.to_dict(), "cfg": cfg.to_dict(),
        "period": {"from": candles[0]["t"], "to": candles[-1]["t"]},
    }
    print()
    print(report_text(result))
    print(f"\n  호출 {ai.calls} · 캐시 {ai.cache_hits} · 오류 {ai.errors} · 예산초과 {ai.budget_hits} "
          f"· 평균 {result['meta']['avg_latency_s']}초")
    if args.json_out:
        os.makedirs(os.path.dirname(args.json_out) or ".", exist_ok=True)
        json.dump(result, open(args.json_out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"저장: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
