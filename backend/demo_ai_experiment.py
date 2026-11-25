"""데모 전용 AI 자율 매매 실험 모드 — AI가 차트를 읽고 스스로 진입·홀딩·청산까지 판단한다.

무엇을 하나
1) OKX/Bitget 공개 15분봉(또는 30분봉)과 4시간봉을 받아, **확정 봉만**으로 시장 문맥을 만든다
   (현재가·EMA20/50·ATR14·RSI14·CCI20·볼린저 위치·최근 20봉 스윙 고저(BSL/SSL)·직전 12봉 흐름 요약).
2) **포지션이 없으면** 설정된 LLM(Gemini 등)에게 `LONG`/`SHORT`/`HOLD`와 손절 배수(`stop_atr` 1.5~3.0),
   목표 배수(`target_r` 1.5~3.0), 한국어 사유를 JSON으로 받는다. `LONG`/`SHORT`면 즉시 데모 계좌에
   시장가로 진입하며, 손절·목표(TP/SL)를 주문에 함께 실어 보낸다.
3) **보유 중이면** 거래소 포지션과 평가손익을 AI에게 보여 주고 `HOLD`(유지)/`CLOSE`(조기 청산)를 받는다.
   `CLOSE`면 `close_position`으로 정리하고 청산 기록을 남긴다. 손절·목표는 거래소에 걸려 있으므로
   AI가 아무 말도 하지 않아도 위험은 제한된다.
4) 루프는 **15분 봉 마감 +60초**마다 돌고, 대기 중 **1분마다 상태 한 줄**(현재가·보유/평가손익·최근 AI 판단·
   다음 점검까지 남은 시간)을 찍는다. 1초 단위로 끊어 자므로 Ctrl+C가 즉시 먹는다.

지키는 선 (절대)
- **샌드박스 전용**: `BybitClient`가 만드는 `demo`(api-demo.bybit.com) 또는 `testnet`(api-testnet.bybit.com)
  클라이언트만 받는다. 메인넷 클라이언트는 만들 때부터 `BybitConfigError`로 거부한다.
- **기존 검증 자산과 완전 분리**: 서버 모의매매(`data/paper_*`, `ai_cache.json`)와 테스트넷 오토 모드
  (`data/testnet_auto_*`)는 읽지도 쓰지도 않는다. 이 모듈의 파일은 이름이 모두 `demo_ai_experiment_*`이며,
  경로가 기존 파일을 가리키면 만들 때부터 거부한다(`_assert_paths_safe`).
- **주문 규칙은 기존 클라이언트 그대로**: 기준 시드 50 USDT · 1회 손실 3%(2~4%) · 레버리지 최대 3배(이 모듈은
  더 보수적으로 3배 상한) · 시장가 + TP/SL 동시 설정. 수량·손실 예산 계산은 `bybit_client`가 한다.
- **과매매·과손실 방지**: 하루 최대 4회 진입, 일일 손실 10% 킬스위치, 손절 3연속 없음(0=끔),
  급변 감지 없음(0=끔), 청산한 봉에서는 같은 봉 재진입 금지.
- **결과 불확실은 재시도하지 않는다**: 주문 전송이 오류로 끝나면 같은 스캔에서 다시 보내지 않고 다음 스캔에서
  거래소 포지션을 보고 판단한다.
- **AI 장애는 매매 중단이 아니라 관망**: 호출 실패·응답 해석 실패는 `HOLD`로 남기고 사유를 로그에 적는다.

실행
    python -m backend.demo_ai_experiment --once                  # 1회 판단(키 없으면 mock)
    python -m backend.demo_ai_experiment --once --mock           # 키가 있어도 드라이런
    python -m backend.demo_ai_experiment --once --testnet        # 테스트넷 계좌로
    python -m backend.demo_ai_experiment --loop                  # 15분 봉 마감 +60초마다 · 1분 상태 줄
    python -m backend.demo_ai_experiment --status                # 계좌·포지션·최근 AI 판단·기록
    python -m backend.demo_ai_experiment --reset / --stop / --resume
    run_demo_ai_experiment.bat                                   # Windows 더블클릭(데모 기본)

파일
    data/demo_ai_experiment_state.json        (mock이면 ...state.mock.json)
    data/demo_ai_experiment_log.jsonl         (mock이면 ...log.mock.jsonl)
    data/demo_ai_experiment_killswitch.json   (mock이면 ...killswitch.mock.json)
    data/demo_ai_experiment_pause.txt         (--stop 표시)
    data/demo_ai_experiment_state*.json.lock  (동시 실행 방지)
"""

from __future__ import annotations

import argparse
import atexit
import json
import math
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .backtest import INTERVAL_MS, load_candles
from .bybit_client import (ENV_LABELS, SANDBOX_ENVS, BybitAPIError, BybitClient,
                           BybitConfigError, BybitError, OrderRejected, risk_limits)
from .config import get_provider_config, load_env_file
from .providers import ProviderError, make_provider
from .risk import KillSwitch, KillSwitchRules
from .strategy import atr_series, cci_series, ema_series
from .testnet_auto import RunLock

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
KST = timezone(timedelta(hours=9))

BAR_DELAY_SEC = 60           # 봉 마감 뒤 이만큼 기다렸다가 다음 봉을 '확정'으로 본다
MIN_CANDLES = 60             # 15분봉 문맥을 만들 최소 확정 봉
MIN_CANDLES_4H = 30          # 4시간봉 EMA20을 세울 최소 봉
DEFAULT_TICK_SEC = 60        # 루프가 상태 한 줄을 찍는 간격(초)
RETRY_SEC = 60               # 스캔 예외 뒤 재시도까지

# 시장 문맥
SWING_LOOKBACK = 20          # BSL/SSL을 잡을 직전 봉 수
RECENT_BARS = 12             # AI에게 보여 줄 직전 봉 수
ATR_PERIOD = 14
RSI_PERIOD = 14
CCI_PERIOD = 20
BB_PERIOD = 20
BB_MULT = 2.0

# AI 판단 범위 — 응답이 이 밖으로 나가면 잘라 낸다
MIN_STOP_ATR, MAX_STOP_ATR, DEFAULT_STOP_ATR = 1.5, 3.0, 2.0
MIN_TARGET_R, MAX_TARGET_R, DEFAULT_TARGET_R = 1.5, 3.0, 2.0

# 이 모듈이 절대 건드리면 안 되는 기존 파일 이름 조각
FORBIDDEN_PREFIXES = ("paper_", "testnet_auto", "ai_cache")


# ---------------------------------------------------------------------- 설정

@dataclass
class ExperimentConfig:
    """실험 설정. 손절·목표 배수는 AI가 고르고, 나머지 위험 한도는 여기서 잠근다."""

    symbol: str = "BTCUSDT"
    interval: str = "15"             # "15" 또는 "30"(분)
    source: str = "auto"             # auto(okx→bitget) | okx | bitget
    limit: int = 300                 # 받을 15·30분봉 수
    four_hour_limit: int = 120       # 받을 4시간봉 수
    loss_pct: float = 3.0            # 1회 손실 예산(기준자본의 %) — 2~4만 허용
    leverage: float = 3.0            # 레버리지 — 1~3만 허용(실험 모드는 더 보수적으로)
    max_trades_per_day: int = 4      # 하루 최대 진입 횟수
    daily_loss_pct: float = 10.0     # 일일 손실 킬스위치(%)
    max_hold_bars: int = 96          # 96봉(15분봉 24시간) 시간 청산 안전장치
    time_exit: bool = True
    tick_sec: int = DEFAULT_TICK_SEC
    data_dir: str = str(DATA)
    state_path: str = ""
    log_path: str = ""
    killswitch_path: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)


def validate_settings(cfg: ExperimentConfig) -> None:
    """주문 전에 위험 한도를 검증한다 — 잘못된 값은 실행 자체를 막는다."""
    BybitClient.validate_loss_pct(cfg.loss_pct)
    lev = float(cfg.leverage)
    if not (1.0 <= lev <= 3.0):
        raise ValueError(f"이 실험 모드의 레버리지는 1~3배만 허용합니다: {lev:g}배")
    if not (1 <= int(cfg.max_trades_per_day) <= 10):
        raise ValueError(f"하루 최대 거래 수는 1~10회만 허용합니다: {cfg.max_trades_per_day}")
    if not (1.0 <= float(cfg.daily_loss_pct) <= 50.0):
        raise ValueError(f"일일 손실 킬스위치는 1~50%만 허용합니다: {cfg.daily_loss_pct}%")
    if str(cfg.interval) not in ("15", "30"):
        raise ValueError(f"시간봉은 15 또는 30분만 지원합니다: {cfg.interval}")
    if not (0 <= int(cfg.max_hold_bars) <= 10000):
        raise ValueError(f"최대 보유 봉 수가 올바르지 않습니다: {cfg.max_hold_bars}")


# ---------------------------------------------------------------------- 작은 도구

def _iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def _kst(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=KST).strftime("%Y-%m-%d %H:%M")


def _num(value, nd: int = 2):
    """JSON에 넣을 숫자 — None·NaN·inf는 None으로(모델이 읽기 쉽게)."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return round(v, nd)


def _clamp_num(value, low: float, high: float, default: float) -> Tuple[float, bool]:
    """AI가 준 배수를 허용 범위로 자른다. (값, 잘렸는지) — 값이 없으면 기본값."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return float(default), True
    if math.isnan(v) or math.isinf(v):
        return float(default), True
    clamped = max(float(low), min(float(high), v))
    return round(clamped, 2), (abs(clamped - v) > 1e-9)


def next_bar_close_ms(now_ms: int, tf_ms: int, delay_sec: int = BAR_DELAY_SEC) -> int:
    """다음 봉이 마감되고 delay_sec만큼 지난 시각(ms). --loop가 이 시각에 맞춰 돈다."""
    return ((int(now_ms) // int(tf_ms)) + 1) * int(tf_ms) + int(delay_sec * 1000)


def tf_ms(interval: str) -> int:
    return INTERVAL_MS.get(str(interval), 15 * 60_000)


def session_label(ts_ms: int) -> str:
    h = (int(ts_ms) // 3_600_000) % 24
    if 0 <= h < 8:
        return f"asia 00-08 UTC ({h}시)"
    if 8 <= h < 16:
        return f"london 08-16 UTC ({h}시)"
    if 16 <= h < 21:
        return f"ny 16-21 UTC ({h}시)"
    return f"late 21-24 UTC ({h}시)"


# ---------------------------------------------------------------------- 시장 문맥

def rsi_series(closes: Sequence[float], period: int = RSI_PERIOD) -> List[Optional[float]]:
    """Wilder RSI — 0~100. 값이 모자라면 None."""
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


def bollinger_position(closes: Sequence[float], period: int = BB_PERIOD,
                       mult: float = BB_MULT) -> Dict:
    """볼린저 %B(0=하단, 100=상단)와 폭, 최근 20창 평균 대비 폭 배수."""
    n = len(closes)
    if n < period:
        return {"pct_b": None, "width_pct": None, "width_ratio": None}
    windows = []
    for end in range(max(period, n - 20), n + 1):
        win = closes[end - period:end]
        mid = sum(win) / period
        sd = (sum((v - mid) ** 2 for v in win) / period) ** 0.5
        windows.append((mid, sd))
    mid, sd = windows[-1]
    upper, lower = mid + mult * sd, mid - mult * sd
    pct_b = (closes[-1] - lower) / (upper - lower) * 100 if upper > lower else 50.0
    width = (upper - lower) / mid * 100 if mid else 0.0
    widths = [((m + mult * s) - (m - mult * s)) / m * 100 if m else 0.0 for m, s in windows]
    avg_width = sum(widths[:-1]) / len(widths[:-1]) if len(widths) > 1 else 0.0
    return {"pct_b": _num(pct_b, 1), "width_pct": _num(width, 3),
            "width_ratio": _num(width / avg_width, 2) if avg_width else None}


def _streak(bars: Sequence[Dict], end: int, limit: int = RECENT_BARS) -> int:
    """연속 양봉(+)·음봉(−) 수. 0이면 보합."""
    count = 0
    for k in range(end, max(-1, end - limit), -1):
        c = bars[k]
        sign = 1 if c["c"] > c["o"] else (-1 if c["c"] < c["o"] else 0)
        if sign == 0:
            break
        if count == 0 or (count > 0) == (sign > 0):
            count += sign
        else:
            break
    return count


def _bar_row(bar: Dict, vol_avg: Optional[float]) -> Dict:
    dt = datetime.fromtimestamp(int(bar["t"]) / 1000, tz=KST)
    row = {"시각": dt.strftime("%m-%d %H:%M"),
           "o": _num(bar["o"]), "h": _num(bar["h"]), "l": _num(bar["l"]), "c": _num(bar["c"]),
           "방향": "양" if bar["c"] > bar["o"] else ("음" if bar["c"] < bar["o"] else "보합")}
    if bar.get("v") is not None:
        row["거래량"] = _num(bar.get("v"), 1)
        if vol_avg:
            row["거래량배수"] = _num(float(bar.get("v") or 0) / vol_avg, 2)
    return row


def build_context(candles: Sequence[Dict], candles_4h: Optional[Sequence[Dict]] = None, *,
                  symbol: str = "BTCUSDT", interval: str = "15",
                  price: Optional[float] = None) -> Dict:
    """확정 봉만으로 AI가 읽을 시장 문맥을 만든다. 4시간봉이 모자라면 그 항목만 비운다."""
    bars = [c for c in candles if c.get("t") is not None]
    if not bars:
        raise ValueError("문맥을 만들 확정 봉이 없습니다")
    closes = [float(c["c"]) for c in bars]
    last = bars[-1]
    close = float(price) if price else closes[-1]

    atr_list = atr_series(bars, ATR_PERIOD)
    atr = atr_list[-1] if atr_list else None
    ema20 = ema_series(closes, 20)[-1]
    ema50 = ema_series(closes, 50)[-1]
    rsi = rsi_series(closes, RSI_PERIOD)[-1]
    cci = cci_series(bars, CCI_PERIOD)[-1]
    bb = bollinger_position(closes)

    prior = bars[-SWING_LOOKBACK - 1:-1] if len(bars) > SWING_LOOKBACK else bars[:-1]
    bsl = max(float(b["h"]) for b in prior) if prior else None
    ssl = min(float(b["l"]) for b in prior) if prior else None
    rng = bars[-SWING_LOOKBACK:] if len(bars) >= SWING_LOOKBACK else bars
    range_hi = max(float(b["h"]) for b in rng)
    range_lo = min(float(b["l"]) for b in rng)
    range_pos = (close - range_lo) / (range_hi - range_lo) * 100 if range_hi > range_lo else 50.0

    recent = bars[-RECENT_BARS:]
    vols = [float(b.get("v") or 0.0) for b in bars[-20:]]
    vol_avg = sum(vols) / len(vols) if vols else None
    recent_closes = [float(b["c"]) for b in recent]
    net_pct = (recent_closes[-1] / recent_closes[0] - 1) * 100 if len(recent_closes) > 1 and recent_closes[0] else None
    ups = sum(1 for b in recent if b["c"] > b["o"])
    downs = sum(1 for b in recent if b["c"] < b["o"])

    ctx: Dict = {
        "종목": symbol,
        "시간봉": f"{interval}분봉",
        "시각": {"UTC": datetime.fromtimestamp(int(last["t"]) / 1000, tz=timezone.utc)
                 .strftime("%Y-%m-%d %H:%M"),
                 "KST": datetime.fromtimestamp(int(last["t"]) / 1000, tz=KST)
                 .strftime("%Y-%m-%d %H:%M"),
                 "세션": session_label(last["t"])},
        "현재가": _num(close),
        "지표": {
            "EMA20": _num(ema20), "EMA50": _num(ema50),
            "종가vsEMA20%": _num((close - ema20) / ema20 * 100 if ema20 else None, 3),
            "EMA20vsEMA50%": _num((ema20 - ema50) / ema50 * 100 if ema20 and ema50 else None, 3),
            "ATR14": _num(atr), "ATR%": _num(atr / close * 100 if atr and close else None, 3),
            "RSI14": _num(rsi, 1),
            "CCI20": _num(cci, 1),
            "볼린저%B": bb["pct_b"], "볼린저폭%": bb["width_pct"],
            "볼린저폭/20봉평균": bb["width_ratio"],
            "연속봉": _streak(bars, len(bars) - 1),
        },
        "유동성": {
            "BSL(직전20봉고점)": _num(bsl), "SSL(직전20봉저점)": _num(ssl),
            "BSL거리ATR": _num((bsl - close) / atr if bsl and atr else None, 2),
            "SSL거리ATR": _num((close - ssl) / atr if ssl and atr else None, 2),
            "20봉범위고": _num(range_hi), "20봉범위저": _num(range_lo),
            "20봉범위내현재위치%": _num(range_pos, 1),
        },
        "최근12봉요약": {
            "양봉": ups, "음봉": downs,
            "순변화%": _num(net_pct, 2),
            "고": _num(max(float(b["h"]) for b in recent)),
            "저": _num(min(float(b["l"]) for b in recent)),
        },
        "최근12봉": [_bar_row(b, vol_avg) for b in recent],
    }

    if candles_4h:
        h4 = [c for c in candles_4h if c.get("t") is not None]
        if h4:
            c4 = [float(c["c"]) for c in h4]
            e20 = ema_series(c4, 20)[-1]
            e50 = ema_series(c4, 50)[-1]
            prev = c4[-7] if len(c4) >= 7 else c4[0]
            ctx["4시간봉"] = {
                "EMA20": _num(e20), "EMA50": _num(e50),
                "종가vsEMA50%": _num((c4[-1] - e50) / e50 * 100 if e50 else None, 3),
                "최근6봉변화%": _num((c4[-1] / prev - 1) * 100 if prev else None, 2),
                "배열": ("상승(EMA20>EMA50)" if e20 and e50 and e20 > e50 else
                         "하락(EMA20<EMA50)" if e20 and e50 and e20 < e50 else "횡보"),
                "최근3봉방향": ["양" if b["c"] > b["o"] else ("음" if b["c"] < b["o"] else "보합")
                                for b in h4[-3:]],
            }
    return ctx


# ---------------------------------------------------------------------- AI 응답 해석

ENTRY_WORDS = {
    "LONG": "LONG", "BUY": "LONG", "롱": "LONG", "매수": "LONG", "상승": "LONG",
    "SHORT": "SHORT", "SELL": "SHORT", "숏": "SHORT", "매도": "SHORT", "하락": "SHORT",
    "HOLD": "HOLD", "WAIT": "HOLD", "NONE": "HOLD", "NO TRADE": "HOLD",
    "관망": "HOLD", "대기": "HOLD", "보류": "HOLD", "없음": "HOLD", "진입안함": "HOLD",
}
MANAGE_WORDS = {
    "HOLD": "HOLD", "KEEP": "HOLD", "유지": "HOLD", "보유": "HOLD", "관망": "HOLD",
    "CLOSE": "CLOSE", "EXIT": "CLOSE", "청산": "CLOSE", "종료": "CLOSE", "닫기": "CLOSE",
    "정리": "CLOSE",
}


def parse_json_object(raw: str) -> Dict:
    """모델 응답에서 JSON 객체 하나를 꺼낸다(코드블록·앞뒤 설명 허용). 실패는 ValueError."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("빈 응답")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise ValueError("JSON 객체를 찾지 못했습니다") from None
        try:
            obj = json.loads(match.group(0))
        except json.JSONDecodeError:
            raise ValueError("JSON 형식이 아닙니다") from None
    if not isinstance(obj, dict):
        raise ValueError("JSON 객체가 아닙니다")
    return obj


def _decision(raw, table: Dict[str, str]) -> Optional[str]:
    word = str(raw or "").strip().upper()
    if word in table:
        return table[word]
    return None


def parse_entry_response(raw: str, cfg: Optional[ExperimentConfig] = None) -> Dict:
    """진입 판단 응답 → decision(LONG/SHORT/HOLD) · stop_atr · target_r · reason.

    해석 실패·모르는 판단은 **HOLD**로 접는다(잘못 읽은 값으로 주문하지 않는다).
    """
    cfg = cfg or ExperimentConfig()
    base = {"stop_atr": float(DEFAULT_STOP_ATR), "target_r": float(DEFAULT_TARGET_R),
            "reason": "", "parse_error": None, "clamped": False}
    try:
        obj = parse_json_object(raw)
    except ValueError as exc:
        return {**base, "decision": "HOLD", "reason": f"AI 응답을 읽지 못해 관망: {exc}",
                "parse_error": str(exc)}
    decision = _decision(obj.get("decision", obj.get("action", obj.get("verdict"))), ENTRY_WORDS)
    if decision is None:
        return {**base, "decision": "HOLD",
                "reason": f"AI 판단을 알아보지 못해 관망(decision={obj.get('decision')!r})",
                "parse_error": "알 수 없는 decision"}
    reason = str(obj.get("reason") or obj.get("이유") or "").strip()[:400] or "사유 없음"
    stop_atr, c1 = _clamp_num(obj.get("stop_atr", obj.get("stop")), MIN_STOP_ATR, MAX_STOP_ATR,
                              DEFAULT_STOP_ATR)
    target_r, c2 = _clamp_num(obj.get("target_r", obj.get("target")), MIN_TARGET_R, MAX_TARGET_R,
                              DEFAULT_TARGET_R)
    return {"decision": decision, "stop_atr": stop_atr, "target_r": target_r, "reason": reason,
            "parse_error": None, "clamped": bool(c1 or c2)}


def parse_manage_response(raw: str) -> Dict:
    """보유 판단 응답 → decision(HOLD/CLOSE) · reason. 해석 실패는 HOLD(그대로 둔다)."""
    try:
        obj = parse_json_object(raw)
    except ValueError as exc:
        return {"decision": "HOLD", "reason": f"AI 응답을 읽지 못해 유지: {exc}",
                "parse_error": str(exc)}
    decision = _decision(obj.get("decision", obj.get("action", obj.get("verdict"))), MANAGE_WORDS)
    if decision is None:
        return {"decision": "HOLD",
                "reason": f"AI 판단을 알아보지 못해 유지(decision={obj.get('decision')!r})",
                "parse_error": "알 수 없는 decision"}
    reason = str(obj.get("reason") or obj.get("이유") or "").strip()[:400] or "사유 없음"
    return {"decision": decision, "reason": reason, "parse_error": None}


ENTRY_SYSTEM = """당신은 바이비트 **데모 계좌**(모의 USDT, 실돈 아님)에서 {symbol} {tf_label}을 보는 실험용 자율 매매 판단자입니다.
지금은 포지션이 없습니다. 새로 진입할지, 관망할지 스스로 정하세요.

판단 기준
1. 추세: {tf} EMA20/50 배열과 4시간봉 EMA50·최근 흐름이 같은 방향인지.
2. 위치: BSL/SSL(직전 20봉 고저)과 20봉 범위 안 현재 위치. 극단 추격은 피합니다.
3. 모멘텀·소진: RSI14·CCI20·볼린저 %B·볼린저 폭·연속봉. 과열/과매수에서의 추격은 감점.
4. 변동성: ATR%가 너무 낮으면 목표 도달이 느리고, 너무 높으면 손절이 쉽게 맞습니다.
5. 최근 12봉 흐름: 양·음봉 수, 순변화, 거래량 배수.

규칙
- 확신이 없으면 HOLD. 관망은 실패가 아닙니다.
- LONG/SHORT를 고르면 stop_atr(손절 거리 = ATR×배수, 1.5~3.0)와 target_r(목표 = 손절거리×배수, 1.5~3.0)을 함께 정합니다.
- 사유는 한국어 1~2문장, 가격·지표 숫자를 인용해 구체적으로.
- 뉴스·펀더멘털은 주어지지 않았으니 지어내지 마세요.
- 출력은 JSON 객체 하나만: {{"decision":"LONG"|"SHORT"|"HOLD","stop_atr":2.0,"target_r":2.0,"reason":"한국어 1~2문장"}}"""

MANAGE_SYSTEM = """당신은 바이비트 **데모 계좌**(모의 USDT, 실돈 아님)에서 {symbol} {tf_label} 포지션을 관리하는 실험용 판단자입니다.
현재 포지션이 있습니다. HOLD(유지) 또는 CLOSE(조기 청산)만 고르세요.

판단 기준
1. 진입 근거가 아직 살아 있는가(추세·EMA 배열·4시간봉 방향).
2. 평가손익(R)과 보유 봉 수 — 목표에 가까우면 유지, 손절에 가까우면 유지(TP/SL이 이미 걸려 있음).
3. 최근 흐름이 진입 방향과 반대로 돌았는가(BSL/SSL 이탈, EMA 역전, 연속 반대봉).
4. 손절·목표는 거래소에 걸려 있으니, 굳이 건드릴 이유가 없으면 HOLD가 정상입니다.

규칙
- 사유는 한국어 1~2문장, 숫자를 인용해 구체적으로.
- 출력은 JSON 객체 하나만: {{"decision":"HOLD"|"CLOSE","reason":"한국어 1~2문장"}}"""


def render_entry_prompt(context: Dict) -> str:
    return ("아래 차트 문맥과 계좌 상태를 보고 지금 새 진입을 할지 판단하세요. "
            "JSON 하나만 출력합니다.\n" + json.dumps(context, ensure_ascii=False, separators=(",", ":")))


def render_manage_prompt(context: Dict) -> str:
    return ("아래 차트 문맥과 현재 포지션을 보고 유지(HOLD)할지 조기 청산(CLOSE)할지 판단하세요. "
            "JSON 하나만 출력합니다.\n" + json.dumps(context, ensure_ascii=False, separators=(",", ":")))


class AIDecider:
    """LLM 호출 + 응답 해석. 실패는 HOLD로 접어 루프를 죽이지 않는다."""

    def __init__(self, provider=None, model: str = ""):
        self.provider = provider
        self.model = model
        self.calls = 0
        self.errors = 0
        self.last_error: Optional[str] = None

    def _ensure(self):
        if self.provider is None:
            conf = get_provider_config()
            if self.model:
                conf["model"] = self.model
            conf["temperature"] = float(conf.get("temperature", 0.2) or 0.2)
            conf["retries"] = 1
            self.provider = make_provider(conf)
        return self.provider

    def describe(self) -> Dict:
        try:
            conf = get_provider_config()
            if self.model:
                conf["model"] = self.model
            return {"provider": conf.get("provider"), "model": conf.get("model")}
        except Exception as exc:                 # 키 없음·제공자 미설정
            return {"error": str(exc)[:200]}

    def _ask(self, system: str, prompt: str) -> str:
        self.calls += 1
        return self._ensure().complete(system, prompt)

    def decide_entry(self, context: Dict, cfg: Optional[ExperimentConfig] = None) -> Dict:
        cfg = cfg or ExperimentConfig()
        system = ENTRY_SYSTEM.format(symbol=cfg.symbol, tf_label=f"{cfg.interval}분봉",
                                     tf=f"{cfg.interval}분")
        try:
            raw = self._ask(system, render_entry_prompt(context))
            result = parse_entry_response(raw, cfg)
        except Exception as exc:                 # ProviderError·네트워크·응답 이상
            self.errors += 1
            self.last_error = str(exc)[:300]
            result = {"decision": "HOLD", "stop_atr": float(DEFAULT_STOP_ATR),
                      "target_r": float(DEFAULT_TARGET_R),
                      "reason": f"AI 호출 실패로 관망: {self.last_error}",
                      "parse_error": None, "clamped": False, "error": self.last_error}
        result["model"] = self.describe().get("model")
        return result

    def decide_manage(self, context: Dict, cfg: Optional[ExperimentConfig] = None) -> Dict:
        cfg = cfg or ExperimentConfig()
        system = MANAGE_SYSTEM.format(symbol=cfg.symbol, tf_label=f"{cfg.interval}분봉")
        try:
            raw = self._ask(system, render_manage_prompt(context))
            result = parse_manage_response(raw)
        except Exception as exc:
            self.errors += 1
            self.last_error = str(exc)[:300]
            result = {"decision": "HOLD", "reason": f"AI 호출 실패로 유지: {self.last_error}",
                      "parse_error": None, "error": self.last_error}
        result["model"] = self.describe().get("model")
        return result


# ---------------------------------------------------------------------- 실험 본체

class DemoAIExperiment:
    """한 번 만들고 tick()을 반복해서 부른다. 상태 파일이 있으면 이어서 돈다(재시작 안전)."""

    __test__ = False          # pytest가 'Test'로 시작하는 이름을 테스트 클래스로 오해하지 않게

    def __init__(self, cfg: Optional[ExperimentConfig] = None, client: Optional[BybitClient] = None,
                 ai=None, fetch=None, killswitch: Optional[KillSwitch] = None, now=None):
        self.cfg = cfg or ExperimentConfig()
        validate_settings(self.cfg)
        self.client = client if client is not None else BybitClient(env="demo")
        env = getattr(self.client, "env", None)
        if env is None:                       # env 속성이 없는 예전·가짜 클라이언트 호환
            env = "testnet" if getattr(self.client, "testnet", False) else None
        if env not in SANDBOX_ENVS:
            raise BybitConfigError(
                "데모·테스트넷 클라이언트만 씁니다 — BybitClient의 샌드박스 가드가 꺼져 있습니다")
        self.env = env
        self.mock = bool(self.client.is_mock)
        self.mode = "mock" if self.mock else env
        self.ai = ai if ai is not None else AIDecider()
        self.fetch = fetch or self._fetch_default
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._last_candle_close: Optional[float] = None
        self.state_path, self.log_path, self.killswitch_path, self.pause_path = self._resolve_paths()
        self._assert_paths_safe()
        rules = KillSwitchRules(daily_loss_pct=float(self.cfg.daily_loss_pct),
                                max_consecutive_losses=0,          # 0 = 이 실험에서는 쓰지 않음
                                max_daily_trades=int(self.cfg.max_trades_per_day),
                                volatility_pause_atr_pct=0.0,      # 0 = 급변 일시정지 끔
                                cooldown_minutes=60,
                                min_equity_pct=90.0)               # -10%면 사람 확인 전까지 중단
        self.killswitch = killswitch or KillSwitch(rules, state_path=self.killswitch_path)

    # ------------------------------------------------------------------ 경로
    def _resolve_paths(self):
        base = Path(self.cfg.data_dir)
        suffix = ".mock" if self.mock else ""      # 드라이런 기록이 데모 계좌를 막지 않게
        state = self.cfg.state_path or str(base / f"demo_ai_experiment_state{suffix}.json")
        log = self.cfg.log_path or str(base / f"demo_ai_experiment_log{suffix}.jsonl")
        ks = self.cfg.killswitch_path or str(base / f"demo_ai_experiment_killswitch{suffix}.json")
        pause = str(Path(state).with_name(Path(state).stem + "_pause.txt"))
        return state, log, ks, pause

    def _assert_paths_safe(self) -> None:
        """모의매매·테스트넷 오토 모드 파일을 실수로 덮어쓰지 않도록 이름을 확인한다."""
        for path in (self.state_path, self.log_path, self.killswitch_path, self.pause_path):
            if not path:
                continue
            name = Path(path).name
            if any(name.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
                raise ValueError(
                    f"{name}은(는) 기존 모의매매·테스트넷 오토 모드가 쓰는 파일입니다 — "
                    "데모 AI 실험은 demo_ai_experiment_* 파일만 씁니다")

    # ------------------------------------------------------------------ 시세
    @property
    def interval_min(self) -> int:
        try:
            return max(1, int(float(str(self.cfg.interval))))
        except (TypeError, ValueError):
            return 15

    @property
    def tf_ms(self) -> int:
        return tf_ms(self.cfg.interval)

    def paused(self) -> bool:
        return bool(self.pause_path) and os.path.isfile(self.pause_path)

    def _fetch_default(self) -> Dict:
        """OKX → Bitget 순으로 15·30분봉과 4시간봉을 받는다. 둘 다 실패하면 예외."""
        order = [self.cfg.source] if self.cfg.source in ("okx", "bitget") else ["okx", "bitget"]
        errors: List[str] = []
        for name in order:
            try:
                c = load_candles(self.cfg.symbol, str(self.cfg.interval), self.cfg.limit, name)
                h4 = load_candles(self.cfg.symbol, "240", self.cfg.four_hour_limit, name)
                candles, candles_4h = c.get("candles") or [], h4.get("candles") or []
                if len(candles) < MIN_CANDLES or len(candles_4h) < MIN_CANDLES_4H:
                    errors.append(f"{name}: 봉 부족({len(candles)}/{len(candles_4h)})")
                    continue
                return {"source": name, "candles": candles, "candles_4h": candles_4h}
            except Exception as exc:                 # 네트워크·응답 이상 → 다음 거래소
                errors.append(f"{name}: {exc}")
        raise RuntimeError("15분봉·4시간봉을 받지 못했습니다 — " + " / ".join(errors))

    def quote(self, candles: Optional[Sequence[Dict]] = None) -> Dict:
        """1분 상태 줄에 쓸 현재가 — 실패해도 루프를 죽이지 않는다."""
        last_close = float(candles[-1]["c"]) if candles else self._last_candle_close
        if self.mock and last_close:
            return {"price": float(last_close), "source": "candle", "note": "mock 시세(봉 종가)"}
        try:
            return {"price": float(self.client.get_last_price(self.cfg.symbol)), "source": "ticker",
                    "note": "mock 시세" if self.mock else ""}
        except Exception as exc:
            last = (self.load_state().get("last_scan") or {}).get("price")
            try:
                last = float(last) if last is not None else None
            except (TypeError, ValueError):
                last = None
            return {"price": last, "source": "last_scan" if last else None,
                    "error": str(exc), "note": "마지막 스캔 가격" if last else "시세 조회 실패"}

    # ------------------------------------------------------------------ 상태 파일
    def _blank_state(self) -> Dict:
        return {
            "version": 1, "mode": self.mode, "kind": "demo_ai_experiment",
            "symbol": self.cfg.symbol, "interval": int(self.cfg.interval),
            "rules": {"loss_pct": self.cfg.loss_pct, "leverage": self.cfg.leverage,
                      "max_trades_per_day": self.cfg.max_trades_per_day,
                      "daily_loss_pct": self.cfg.daily_loss_pct,
                      "max_hold_bars": self.cfg.max_hold_bars, "env": self.mode,
                      "sandbox_only": True, "base_url": self.client.base_url,
                      "stop_atr_range": [MIN_STOP_ATR, MAX_STOP_ATR],
                      "target_r_range": [MIN_TARGET_R, MAX_TARGET_R]},
            "created_at": self.now().isoformat(timespec="seconds"), "updated_at": None,
            "scan_count": 0, "scan_bar": None,
            "position": None, "exchange_position": None, "closed": [],
            "equity": None, "stats": {}, "killswitch": {}, "last_scan": {}, "ai_last": None,
            "day": {"date": self._today(), "entries": 0, "closes": 0},
            "last_exit_bar": None,
        }

    def _today(self) -> str:
        return self.now().astimezone(KST).date().isoformat()

    def _roll_day(self, state: Dict) -> None:
        today = self._today()
        day = state.get("day") or {}
        if day.get("date") != today:
            state["day"] = {"date": today, "entries": 0, "closes": 0}

    def load_state(self) -> Dict:
        state = self._blank_state()
        path = self.state_path
        if not path or not os.path.isfile(path):
            self._recompute(state)
            return state
        try:
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
            if not isinstance(saved, dict):
                raise ValueError("상태 파일이 객체가 아닙니다")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"[demo-ai] 상태 파일을 읽지 못해 새로 시작합니다: {exc}", flush=True)
            self._recompute(state)
            return state
        state = {**state, **saved}
        if saved.get("mode") and saved["mode"] != state["mode"] and state.get("position"):
            print(f"[demo-ai] 모드가 다른 상태 파일({saved['mode']} → {state['mode']})입니다 — "
                  "포지션 기록은 무시하고 거래소 상태를 따릅니다", flush=True)
            state["position"] = None
        state["mode"] = self.mode
        state["symbol"] = self.cfg.symbol
        state["interval"] = int(self.cfg.interval)
        state["rules"] = self._blank_state()["rules"]
        self._roll_day(state)
        self._recompute(state)
        return state

    def save_state(self, state: Dict) -> None:
        path = self.state_path
        if not path:
            return
        state["updated_at"] = self.now().isoformat(timespec="seconds")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _append_log(self, record: Dict) -> None:
        path = self.log_path
        if not path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def tail_log(self, limit: int = 20) -> List[Dict]:
        path = self.log_path
        if not path or not os.path.isfile(path):
            return []
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()[-limit:]
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    # ------------------------------------------------------------------ 통계
    def _recompute(self, state: Dict) -> Dict:
        closed = state.get("closed") or []
        known = [t for t in closed if t.get("pnl") is not None]
        wins = [t for t in known if t["pnl"] > 0]
        losses = [t for t in known if t["pnl"] < 0]
        n = len(known)
        r_values = [t.get("r") for t in known if t.get("r") is not None]
        state["stats"] = {
            "trades": n, "unknown_pnl": len(closed) - n,
            "wins": len(wins), "losses": len(losses),
            "win_rate_pct": round(len(wins) / n * 100, 1) if n else None,
            "net_pnl": round(sum(t["pnl"] for t in known), 2),
            "avg_r": round(sum(r_values) / len(r_values), 3) if r_values else None,
            "r_sum": round(sum(r_values), 2),
            "targets": sum(1 for t in closed if t.get("reason") == "TARGET"),
            "stops": sum(1 for t in closed if t.get("reason") == "STOP"),
            "time_exits": sum(1 for t in closed if t.get("reason") == "TIME"),
            "ai_closes": sum(1 for t in closed if t.get("reason") == "AI_CLOSE"),
            "estimated_pnl": sum(1 for t in closed if t.get("pnl_source") not in (None, "exchange")),
            "last_trade_at": closed[-1].get("exit_time") if closed else None,
        }
        return state["stats"]

    def _killswitch_summary(self) -> Dict:
        status = self.killswitch.status()
        st = status["state"]
        return {"ok": status["entry"]["ok"], "reason": status["entry"]["reason"],
                "tripped": st.get("tripped"), "tripped_until": st.get("tripped_until"),
                "manual_stop": st.get("manual_stop"), "realized_pnl": st.get("realized_pnl"),
                "trades_today": st.get("trades"), "rules": status["rules"]}

    # ------------------------------------------------------------------ 스캔 본체
    def tick(self, candles: Optional[Sequence[Dict]] = None,
             candles_4h: Optional[Sequence[Dict]] = None,
             now_ms: Optional[int] = None) -> Dict:
        """한 번 판단한다. candles를 주면 네트워크 없이 돈다(테스트·재생)."""
        now_ms = int(now_ms if now_ms is not None else self.now().timestamp() * 1000)
        source = None
        if candles is None:
            data = self.fetch()
            candles, candles_4h = data["candles"], data.get("candles_4h")
            source = data.get("source")
        bars = self._closed_bars(candles, self.tf_ms, now_ms)
        bars_4h = self._closed_bars(candles_4h or [], 4 * 3_600_000, now_ms)
        self._last_candle_close = float(bars[-1]["c"]) if bars else None
        if not bars:
            raise RuntimeError("확정 봉이 없습니다(캔들 부족 또는 시각 이상)")
        last = bars[-1]
        state = self.load_state()
        state["last_context"] = None
        if len(bars) < MIN_CANDLES:
            return self._finish(state, last, "ERROR",
                                [f"확정 봉이 {len(bars)}개뿐입니다 — 최소 {MIN_CANDLES}개가 필요합니다"],
                                source=source, bars=len(bars))

        def finish(action: str, events: List[str], ai=None, order=None,
                   account: Optional[Dict] = None) -> Dict:
            return self._finish(state, last, action, events, source=source, bars=len(bars),
                                ai=ai, order=order, account=account)

        # 0) 거래소 상태 — 읽지 못하면 아무 주문도 내지 않는다
        try:
            balance = self.client.get_balance()
            positions = self.client.get_positions(self.cfg.symbol)
        except BybitError as exc:
            hint = ""
            if self.env == "demo" and getattr(exc, "ret_code", None) in (10003, 10004, 33004):
                hint = (" — Demo Trading(모의투자) 키인지 확인하세요"
                        "(테스트넷 키는 데모 계좌에서 인증되지 않습니다)")
            return finish("ERROR", [f"거래소 조회 실패: {exc}{hint}"])
        equity = float(balance.get("equity") or 0.0)
        account = {"equity": equity, "available": balance.get("available"),
                   "riskBaseUsd": balance.get("riskBaseUsd"), "mock": bool(balance.get("mock"))}
        self.killswitch.start_day(equity)
        if equity <= 0:
            return finish("ERROR", [f"잔고가 0입니다 — {ENV_LABELS[self.env]} 계정에 모의 USDT를 "
                                    "채운 뒤 다시 실행하세요"], account=account)

        rows = [p for p in positions if p.get("symbol") == self.cfg.symbol and float(p.get("size") or 0) > 0]
        ex = max(rows, key=lambda p: float(p["size"])) if rows else None
        local = state.get("position")
        events: List[str] = []
        action = "HOLD"
        ai_result = None
        order = None
        blocked = False                                   # 이번 스캔에서 신규 진입 금지
        # 1) 포지션 정리 — 내 기록과 거래소를 맞춘다
        if local and ex and self._same_position(local, ex):
            action = "HOLD"
            events.append(f"보유 중 {self._side_ko(local['side'])} {local['qty']}개 · 진입 "
                          f"{float(local['entry']):,.2f} · 손절 {float(local['stop']):,.2f} · "
                          f"목표 {float(local['target']):,.2f} (TP/SL은 거래소에 걸려 있음)")
            state["exchange_position"] = ex
            if self.mock:
                closed = self._mock_exit(state, bars, equity)
                if closed:
                    action, blocked = "CLOSE", True
                    events.append(f"모의 청산 {closed['reason']} {closed['r']:+.2f}R · "
                                  f"손익 {closed['pnl']:+.2f} USDT")
            elif self.cfg.time_exit and self._bars_held(local, last) >= self.cfg.max_hold_bars:
                done, note = self._close_position(state, local, ex, account, reason="TIME")
                action, blocked = ("CLOSE", True) if done else ("HOLD", True)
                events.append(note)
            if state.get("position") and not blocked:
                price = self._price_now(bars)
                ctx = build_context(bars, bars_4h, symbol=self.cfg.symbol,
                                    interval=self.cfg.interval, price=price)
                pos = state["position"]
                unreal = self._unrealized(price, pos)
                prompt_ctx = {
                    **ctx,
                    "포지션": {
                        "방향": self._side_ko(pos["side"]), "수량": pos.get("qty"),
                        "진입가": _num(pos.get("entry")), "손절": _num(pos.get("stop")),
                        "목표": _num(pos.get("target")),
                        "보유봉": self._bars_held(pos, last),
                        "평가손익USDT": unreal["usd"] if unreal else None,
                        "평가R": unreal["r"] if unreal else None,
                        "진입사유": (pos.get("ai") or {}).get("reason"),
                    },
                    "계좌": self._account_view(account, state),
                }
                state["last_context"] = prompt_ctx
                result = self._call_ai_manage(prompt_ctx)
                ai_result = result
                state["ai_last"] = {"at": self.now().isoformat(timespec="seconds"),
                                    "mode": "manage", **self._ai_view(result)}
                if result.get("decision") == "CLOSE":
                    done, note = self._close_position(state, pos, ex, account, reason="AI_CLOSE")
                    events.append(f"AI CLOSE: {result.get('reason')} — {note}")
                    action, blocked = ("CLOSE", True) if done else ("HOLD", True)
                else:
                    events.append(f"AI HOLD: {result.get('reason')}")
        elif local and not ex:
            reason_hint, est_price = self._infer_bar_exit(local, bars)
            est_pnl = None
            if est_price is not None:
                sign = 1.0 if str(local.get("side")).lower() == "buy" else -1.0
                est_pnl = (float(est_price) - float(local["entry"])) * float(local["qty"]) * sign
            trade = self._record_close(state, local, account, reason_hint=reason_hint,
                                       estimate_pnl=est_pnl, estimate_price=est_price,
                                       estimate_source="bar_simulation")
            state["exchange_position"] = None
            action = "CLOSE"
            events.append(f"청산 확인 {trade['reason']} · 손익 " +
                          (f"{trade['pnl']:+.2f} USDT ({trade['r']:+.2f}R)" if trade["pnl"] is not None
                           else "미확인(거래소 기록 지연)"))
            blocked = True
        elif local and ex:
            action = "SKIP"
            events.append(f"거래소 포지션 {ex['side']} {ex['size']}이 내 기록"
                          f"({local['side']} {local['qty']})과 다릅니다 — 수동 변경으로 보고 건드리지 않습니다")
            state["exchange_position"] = ex
            blocked = True
        elif ex:
            action = "SKIP"
            events.append(f"거래소에 이미 {ex['side']} {ex['size']} 포지션이 있습니다 — 신규 진입 건너뜀")
            state["exchange_position"] = ex
            blocked = True
        else:
            state["exchange_position"] = None

        # 2) 새 진입 — 포지션이 없을 때만, 가드 → AI → 주문
        if not state.get("position") and not blocked:
            action, events, ai_result, order = self._try_entry(
                state, bars, bars_4h, account, last, events)
        return finish(action, events, ai=ai_result, order=order, account=account)

    # ------------------------------------------------------------------ 진입
    def _try_entry(self, state: Dict, bars: List[Dict], bars_4h: List[Dict], account: Dict,
                   last: Dict, events: List[str]):
        # 값싼 가드를 먼저 통과시킨다 — 막힌 스캔에서 AI 호출(무료 한도)을 낭비하지 않는다
        if self.paused():
            return "SKIP", events + ["수동 정지 중(--resume로 해제)"], None, None
        entry_ok = self.killswitch.allow_entry()
        if not entry_ok.get("ok"):
            return "SKIP", events + [f"킬스위치: {entry_ok.get('reason')}"], None, None
        entries = int((state.get("day") or {}).get("entries") or 0)
        if entries >= int(self.cfg.max_trades_per_day):
            return ("SKIP", events + [f"오늘 진입 {entries}회 — 하루 최대 "
                                      f"{self.cfg.max_trades_per_day}회 도달"], None, None)
        last_exit = state.get("last_exit_bar")
        if last_exit is not None and int(last["t"]) <= int(last_exit):
            return ("SKIP", events + ["방금 청산한 봉에서는 다시 진입하지 않습니다"], None, None)

        price = self._price_now(bars)
        ctx = build_context(bars, bars_4h, symbol=self.cfg.symbol, interval=self.cfg.interval,
                            price=price)
        atr = float((ctx.get("지표") or {}).get("ATR14") or 0.0)
        if atr <= 0 or price <= 0:
            return "SKIP", events + ["ATR·가격이 없어 진입할 수 없음"], None, None

        prompt_ctx = {**ctx, "계좌": self._account_view(account, state),
                      "요청": "지금은 포지션이 없습니다. 새 진입(LONG/SHORT) 또는 관망(HOLD)을 판단하세요."}
        state["last_context"] = prompt_ctx
        result = self._call_ai_entry(prompt_ctx)
        state["ai_last"] = {"at": self.now().isoformat(timespec="seconds"), "mode": "entry",
                            **self._ai_view(result)}
        if result.get("decision") not in ("LONG", "SHORT"):
            return "HOLD", events + [f"AI HOLD: {result.get('reason')}"], result, None

        side = result["decision"]
        stop_atr = float(result.get("stop_atr") or DEFAULT_STOP_ATR)
        target_r = float(result.get("target_r") or DEFAULT_TARGET_R)
        dist = stop_atr * atr
        stop = price - dist if side == "LONG" else price + dist
        target = price + dist * target_r if side == "LONG" else price - dist * target_r
        events.append(f"AI {side} 판단(stop {stop_atr:g}ATR · target {target_r:g}R): "
                      f"{result.get('reason')}")

        word = "Buy" if side == "LONG" else "Sell"
        try:
            order = self.client.place_market_order(
                self.cfg.symbol, word, stop_loss=stop, take_profit=target, entry_price=price,
                loss_pct=self.cfg.loss_pct, leverage=self.cfg.leverage, balance=float(account.get("equity") or 0.0))
        except OrderRejected as exc:
            return "SKIP", events + [f"주문 거부: {exc}"], result, None
        except (ValueError, BybitConfigError) as exc:
            return "SKIP", events + [f"주문 규칙 위반: {exc}"], result, None
        except BybitAPIError as exc:
            return ("ERROR", events + [f"주문 전송 오류({exc}) — 결과 불확실: 같은 스캔에서 다시 보내지 "
                                       "않고 다음 스캔에서 거래소 포지션으로 확인합니다"], result, None)

        state["position"] = self._position_from_order(order, result, last, atr)
        state["day"]["entries"] = entries + 1
        pos = state["position"]
        events.append(
            f"{ENV_LABELS[self.env]} {'롱' if side == 'LONG' else '숏'} 진입 {pos['qty']}개 @ "
            f"{float(pos['entry']):,.2f} · 손절 {float(pos['stop']):,.2f} · 목표 "
            f"{float(pos['target']):,.2f} · 예상손실 ${float(pos['risk_usd'] or 0):.2f} · "
            f"레버리지 {float(pos['leverage'] or 0):g}배 · 주문 {pos['order_id']}")
        return "OPEN", events, result, order

    def _price_now(self, bars: Sequence[Dict]) -> float:
        """지금 진입·평가에 쓸 가격. mock은 봉 종가(가짜 시세와 문맥을 맞춘다), 실주문은 거래소 현재가."""
        if self.mock:
            return float(bars[-1]["c"])
        try:
            return float(self.client.get_last_price(self.cfg.symbol))
        except Exception:
            return float(bars[-1]["c"])

    def _account_view(self, account: Dict, state: Dict) -> Dict:
        entries = int((state.get("day") or {}).get("entries") or 0)
        return {"모드": self.mode, "잔고USDT": _num(account.get("equity")),
                "리스크기준USD": account.get("riskBaseUsd"),
                "1회손실예산USD": _num(float(account.get("riskBaseUsd") or 0)
                                     * float(self.cfg.loss_pct) / 100.0),
                "오늘진입": entries, "하루최대진입": int(self.cfg.max_trades_per_day),
                "일일손실킬스위치%": float(self.cfg.daily_loss_pct),
                "실현손익USDT": _num(self.killswitch.state.get("realized_pnl"))}

    def _call_ai_entry(self, context: Dict) -> Dict:
        try:
            return self.ai.decide_entry(context, self.cfg)
        except Exception as exc:                   # 어떤 가짜 AI라도 루프를 죽이지 않는다
            return {"decision": "HOLD", "stop_atr": DEFAULT_STOP_ATR,
                    "target_r": DEFAULT_TARGET_R, "reason": f"AI 호출 실패로 관망: {exc}",
                    "parse_error": None, "error": str(exc)[:300]}

    def _call_ai_manage(self, context: Dict) -> Dict:
        try:
            return self.ai.decide_manage(context, self.cfg)
        except Exception as exc:
            return {"decision": "HOLD", "reason": f"AI 호출 실패로 유지: {exc}",
                    "parse_error": None, "error": str(exc)[:300]}

    # ------------------------------------------------------------------ 청산
    def _close_position(self, state: Dict, pos: Dict, ex: Optional[Dict], account: Dict,
                        reason: str) -> Tuple[bool, str]:
        """거래소 시장가 청산 → 포지션이 사라졌는지 확인 → 기록. (성공, 설명)을 돌려준다."""
        estimate_pnl = None
        if ex:
            estimate_pnl = ex.get("unrealisedPnl")
        try:
            result = self.client.close_position(self.cfg.symbol)
            sent = f"청산 주문 {result.get('orderId')}"
        except OrderRejected as exc:                # 이미 닫혀 있을 수 있다 — 잔여를 확인한다
            sent = f"청산 주문: {exc}"
        except BybitError as exc:
            return False, f"청산 실패({exc}) — 다음 스캔에서 다시 시도합니다"
        still: List[Dict] = []
        try:
            still = [p for p in self.client.get_positions(self.cfg.symbol) if float(p.get("size") or 0) > 0]
        except BybitError:
            still = []
        if still:
            return False, f"{sent} · 청산 뒤에도 포지션 {still[0]['size']}이 남아 기록을 유지합니다"
        trade = self._record_close(state, pos, account, reason_hint=reason,
                                   estimate_pnl=estimate_pnl, estimate_price=None)
        detail = (f"{trade['pnl']:+.2f} USDT ({trade['r']:+.2f}R)" if trade["pnl"] is not None
                  else "손익 미확인")
        return True, f"{sent} · 청산 확인 {trade['reason']} · {detail}"

    def _record_close(self, state: Dict, pos: Dict, account: Dict, reason_hint: Optional[str] = None,
                      estimate_pnl: Optional[float] = None,
                      estimate_price: Optional[float] = None,
                      estimate_source: str = "exchange_unrealised") -> Dict:
        """닫힌 포지션을 청산 기록으로 남긴다.

        거래소 청산 손익(closed-pnl)을 먼저 쓰고, 데모 계정에서 그 조회가 막히면 거래소가 준
        평가손익(unrealisedPnl)을 추정치로 남긴다(출처를 pnl_source에 적는다). 둘 다 없으면 '미확인'.
        """
        rows: List[Dict] = []
        lookup_error = None
        if not self.mock:
            try:
                rows = self.client.get_closed_pnl(self.cfg.symbol, limit=5)
            except Exception as exc:             # 미지원·404·예상 밖 응답도 루프를 죽이지 않는다
                lookup_error = exc
                print(f"[demo-ai] 청산 손익 조회 실패(손익 미확인으로 계속): {exc}", flush=True)
        pick = self._match_closed_pnl(rows, pos) if not self.mock else None
        pnl: Optional[float] = None
        exit_price: Optional[float] = estimate_price
        source: Optional[str] = None
        if pick is not None:
            pnl = pick.get("closedPnl")
            exit_price = pick.get("avgExitPrice")
            source = "exchange"
            reason = reason_hint if reason_hint in ("AI_CLOSE", "TIME") else self._infer_reason(pos, exit_price)
        elif estimate_pnl is not None:
            pnl = round(float(estimate_pnl), 4)
            source = estimate_source
            reason = reason_hint or "OTHER"
        else:
            reason = reason_hint or "UNKNOWN"
        risk = float(pos.get("risk_usd") or 0.0)
        trade = self._trade(pos, reason, exit_price, None if pnl is None else round(float(pnl), 4),
                            None if (pnl is None or not risk) else round(float(pnl) / risk, 3),
                            None if pick is None else int(pick.get("updatedTime") or 0), source)
        if pnl is None:
            trade["note"] = "청산 손익을 확인하지 못했습니다(거래소 기록 지연·미지원)"
            if lookup_error is not None and self.env == "demo":
                trade["note"] += " — 데모 트레이딩은 청산 손익 조회가 제한될 수 있습니다"
        state["closed"].append(trade)
        state["position"] = None
        state.setdefault("day", {})
        state["day"]["closes"] = int(state["day"].get("closes") or 0) + 1
        if trade["pnl"] is not None:
            self.killswitch.record_trade(trade["pnl"], float(account.get("equity") or 0.0))
        return trade

    def _mock_exit(self, state: Dict, bars: List[Dict], equity: float) -> Optional[Dict]:
        """mock 청산 — 거래소가 없으므로 확정 봉의 고가·저가로 손절·목표·시간 청산을 판정한다."""
        pos = state.get("position")
        if not pos:
            return None
        side, stop, target = pos["side"], float(pos["stop"]), float(pos["target"])
        entry_bar = int(pos.get("entry_bar_time") or 0)
        count = 0
        for bar in bars:
            t = int(bar["t"])
            if t < entry_bar:
                continue
            count += 1
            if side == "Buy":
                if float(bar["l"]) <= stop:
                    return self._mock_close(state, pos, "STOP", stop, t, equity)
                if float(bar["h"]) >= target:
                    return self._mock_close(state, pos, "TARGET", target, t, equity)
            else:
                if float(bar["h"]) >= stop:
                    return self._mock_close(state, pos, "STOP", stop, t, equity)
                if float(bar["l"]) <= target:
                    return self._mock_close(state, pos, "TARGET", target, t, equity)
            if self.cfg.time_exit and count >= self.cfg.max_hold_bars:
                return self._mock_close(state, pos, "TIME", float(bar["c"]), t, equity)
        return None

    def _mock_close(self, state: Dict, pos: Dict, reason: str, price: float, bar_time: int,
                    equity: float) -> Dict:
        try:
            self.client.close_position(self.cfg.symbol)
        except (BybitError, OrderRejected) as exc:
            print(f"[demo-ai] mock 청산 실패: {exc}", flush=True)
        qty, entry = float(pos["qty"]), float(pos["entry"])
        sign = 1.0 if pos["side"] == "Buy" else -1.0
        pnl = (float(price) - entry) * qty * sign         # mock은 수수료를 넣지 않는다
        risk = float(pos.get("risk_usd") or 0.0)
        trade = self._trade(pos, reason, float(price), round(pnl, 4),
                            round(pnl / risk, 3) if risk else None, bar_time, "bar_simulation")
        state["closed"].append(trade)
        state["position"] = None
        state.setdefault("day", {})
        state["day"]["closes"] = int(state["day"].get("closes") or 0) + 1
        self.killswitch.record_trade(trade["pnl"], equity)
        return trade

    def _infer_bar_exit(self, pos: Dict, bars: Sequence[Dict]) -> Tuple[Optional[str], Optional[float]]:
        """포지션이 거래소에서 사라졌을 때, 확정 봉의 고가·저가로 손절·목표 중 어느 쪽인지 추정한다.

        거래소 청산 손익 조회가 막힌 데모 계좌에서도 사유·손익 추정치를 남기기 위한 것이다.
        같은 봉이 손절·목표를 모두 건드렸다면 손절(보수적)로 본다.
        """
        entry_bar = int(pos.get("entry_bar_time") or 0)
        stop, target = float(pos.get("stop") or 0.0), float(pos.get("target") or 0.0)
        if not entry_bar or stop <= 0 or target <= 0:
            return None, None
        for bar in bars:
            if int(bar["t"]) < entry_bar:
                continue
            if str(pos.get("side")) == "Buy":
                if float(bar["l"]) <= stop:
                    return "STOP", stop
                if float(bar["h"]) >= target:
                    return "TARGET", target
            else:
                if float(bar["h"]) >= stop:
                    return "STOP", stop
                if float(bar["l"]) <= target:
                    return "TARGET", target
        return None, None

    @staticmethod
    def _match_closed_pnl(rows: Sequence[Dict], pos: Dict) -> Optional[Dict]:
        """내 포지션과 같은 방향·수량의, 신호 시각 이후 갱신된 가장 최근 청산 기록."""
        qty = float(pos.get("qty") or 0.0)
        since = int(pos.get("signal_time") or 0)
        best = None
        for row in rows or []:
            if str(row.get("side") or "") != str(pos.get("side") or ""):
                continue
            if qty and abs(float(row.get("qty") or 0.0) - qty) > max(1e-9, qty * 0.5):
                continue
            if since and int(row.get("updatedTime") or 0) < since:
                continue
            if best is None or int(row.get("updatedTime") or 0) >= int(best.get("updatedTime") or 0):
                best = row
        return best

    def _infer_reason(self, pos: Dict, exit_price: Optional[float]) -> str:
        """청산가가 손절가·목표가 중 어느 쪽에 더 가까운지로 사유를 정한다.

        손절·목표가 가까운(작은) 포지션에서 0.2% 여유가 둘 다를 덮을 수 있으므로,
        먼저 맞는 쪽이 아니라 **가장 가까운 쪽**을 고른다.
        """
        if not exit_price:
            return "UNKNOWN"
        stop, target = float(pos.get("stop") or 0.0), float(pos.get("target") or 0.0)
        tol = max(float(pos.get("entry") or 0.0) * 0.002, 1e-9)
        candidates = []
        if stop:
            candidates.append((abs(float(exit_price) - stop), "STOP"))
        if target:
            candidates.append((abs(float(exit_price) - target), "TARGET"))
        if not candidates:
            return "UNKNOWN"
        distance, reason = min(candidates)
        return reason if distance <= tol else "OTHER"

    # ------------------------------------------------------------------ 포지션 기록
    def _position_from_order(self, order: Dict, ai_result: Dict, last: Dict, atr: float) -> Dict:
        plan = order.get("plan") or {}
        payload = order.get("payload") or {}
        side = plan.get("side") or "Buy"
        return {
            "side": side, "qty": plan.get("qty"),
            "entry": float(plan.get("entryPrice") or 0.0),
            "stop": float(payload.get("stopLoss") or plan.get("stopPrice") or 0.0),
            "target": float(payload.get("takeProfit") or plan.get("takeProfit") or 0.0),
            "leverage": plan.get("leverage"), "risk_usd": plan.get("expectedLossUsd"),
            "loss_budget_usd": plan.get("lossBudgetUsd"), "order_id": order.get("orderId"),
            "mode": self.mode, "signal_time": int(last["t"]),
            "entry_bar_time": int(last["t"]) + self.tf_ms,
            "entry_time": self.now().isoformat(timespec="seconds"), "atr": atr,
            "ai": {k: ai_result.get(k) for k in ("decision", "stop_atr", "target_r", "reason", "model")},
            "reasons": [ai_result.get("reason")] if ai_result.get("reason") else [],
        }

    def _trade(self, pos: Dict, reason: str, exit_price: Optional[float], pnl: Optional[float],
               r: Optional[float], exit_bar_time: Optional[int],
               pnl_source: Optional[str] = None) -> Dict:
        return {
            "side": pos["side"], "qty": pos["qty"], "entry": pos["entry"], "exit": exit_price,
            "stop": pos["stop"], "target": pos["target"], "risk_usd": pos.get("risk_usd"),
            "reason": reason, "pnl": pnl, "r": r, "pnl_source": pnl_source, "mode": self.mode,
            "order_id": pos.get("order_id"), "signal_time": pos.get("signal_time"),
            "entry_time": pos.get("entry_time"),
            "exit_time": self.now().isoformat(timespec="seconds"),
            "exit_bar_time": exit_bar_time,
            "ai_decision": (pos.get("ai") or {}).get("decision"),
            "ai_reason": (pos.get("ai") or {}).get("reason"),
        }

    def _position_view(self, state: Dict) -> Optional[Dict]:
        pos = state.get("position")
        if not pos:
            return None
        return {"side": pos["side"], "qty": pos["qty"], "entry": pos["entry"],
                "stop": pos["stop"], "target": pos["target"], "leverage": pos.get("leverage"),
                "risk_usd": pos.get("risk_usd"), "order_id": pos.get("order_id"),
                "entry_at": pos.get("entry_time"), "ai": (pos.get("ai") or {}).get("decision"),
                "ai_reason": (pos.get("ai") or {}).get("reason")}

    @staticmethod
    def _side_ko(side: str) -> str:
        return "롱" if str(side).lower() in ("buy", "long") else "숏"

    @staticmethod
    def _same_position(local: Dict, ex: Dict) -> bool:
        if str(ex.get("side") or "") != str(local.get("side") or ""):
            return False
        qty = float(local.get("qty") or 0.0)
        size = float(ex.get("size") or 0.0)
        return abs(size - qty) <= max(1e-9, qty * 0.5)

    def _bars_held(self, pos: Dict, last: Dict) -> int:
        step = self.tf_ms
        entry_bar = int(pos.get("entry_bar_time") or 0)
        return max(0, (int(last["t"]) - entry_bar) // step + 1) if entry_bar else 0

    @staticmethod
    def _unrealized(price: Optional[float], pos: Dict) -> Optional[Dict]:
        if not price:
            return None
        entry = float(pos.get("entry") or 0.0)
        qty = float(pos.get("qty") or 0.0)
        if entry <= 0 or qty <= 0:
            return None
        sign = 1.0 if str(pos.get("side") or "").lower() in ("buy", "long") else -1.0
        usd = (float(price) - entry) * qty * sign
        risk = float(pos.get("risk_usd") or 0.0) or abs(entry - float(pos.get("stop") or 0.0)) * qty
        return {"usd": round(usd, 2), "r": round(usd / risk, 2) if risk else None}

    @staticmethod
    def _ai_view(result: Optional[Dict]) -> Dict:
        if not result:
            return {}
        return {k: result.get(k) for k in ("decision", "stop_atr", "target_r", "reason",
                                           "model", "parse_error", "error") if result.get(k) is not None}

    @staticmethod
    def _closed_bars(bars: Sequence[Dict], step_ms: int, now_ms: int) -> List[Dict]:
        out = sorted((c for c in bars if c.get("t") is not None), key=lambda c: int(c["t"]))
        while len(out) > 1 and int(out[-1]["t"]) + int(step_ms) > int(now_ms):
            out.pop()                                  # 진행 중인 봉으로 판단하지 않는다
        return out

    # ------------------------------------------------------------------ 마무리
    def _finish(self, state: Dict, last: Dict, action: str, events: List[str], *,
                ai=None, order=None, account: Optional[Dict] = None,
                source=None, bars: Optional[int] = None) -> Dict:
        self._roll_day(state)
        state["scan_bar"] = int(last["t"])
        if action == "CLOSE":
            state["last_exit_bar"] = int(last["t"])   # 이 봉에서는 다시 열지 않는다
        state["scan_count"] = int(state.get("scan_count") or 0) + 1
        if account and account.get("equity") is not None:
            state["equity"] = round(float(account["equity"]), 2)
        state["killswitch"] = self._killswitch_summary()
        self._recompute(state)
        record = {
            "ts": self.now().isoformat(timespec="seconds"),
            "mode": self.mode, "env": self.env, "sandbox_only": True,
            "base_url": self.client.base_url,
            "bar": _iso(last["t"]), "bar_kst": _kst(last["t"]),
            "price": last["c"], "bars": bars, "source": source,
            "equity": state.get("equity"), "riskBaseUsd": (account or {}).get("riskBaseUsd"),
            "action": action, "events": events,
            "ai": self._ai_view(ai), "context": state.get("last_context"),
            "position": self._position_view(state),
            "order": None if not order else {
                "orderId": order.get("orderId"), "mock": order.get("mock"),
                "symbol": self.cfg.symbol, "payload": order.get("payload"),
                "plan": order.get("plan")},
            "stats": state["stats"],
            "day": dict(state.get("day") or {}),
            "killswitch": {"ok": state["killswitch"].get("ok"),
                           "reason": state["killswitch"].get("reason"),
                           "trades_today": state["killswitch"].get("trades_today"),
                           "realized_pnl": state["killswitch"].get("realized_pnl")},
        }
        state["last_scan"] = {k: record[k] for k in ("ts", "bar", "action", "events", "price")}
        self.save_state(state)
        self._append_log(record)
        return record

    # ------------------------------------------------------------------ 조회
    def sync_clock(self) -> Dict:
        if self.mock:
            return {"offset_ms": 0, "mock": True}
        try:
            return {"offset_ms": self.client.sync_time(), "mock": False}
        except BybitError as exc:
            return {"offset_ms": None, "error": str(exc), "mock": False}

    def status(self) -> Dict:
        state = self.load_state()
        out = {"ok": True, "mode": self.mode, "env": self.env, "sandbox_only": True,
               "symbol": self.cfg.symbol, "client": self.client.mode(), "rules": state["rules"],
               "state": state, "killswitch": self.killswitch.status(),
               "settings": self.cfg.to_dict(), "log": self.tail_log(),
               "ai": self.ai.describe() if hasattr(self.ai, "describe") else None}
        try:
            out["account"] = self.client.get_balance()
            out["positions"] = self.client.get_positions(self.cfg.symbol)
        except BybitError as exc:
            out["account_error"] = str(exc)
        return out

    def reset(self) -> Dict:
        try:
            opened = [p for p in self.client.get_positions(self.cfg.symbol) if float(p.get("size") or 0) > 0]
        except BybitError as exc:
            raise BybitError(f"거래소 포지션을 확인하지 못해 초기화하지 않습니다: {exc}") from None
        if opened:
            raise RuntimeError("거래소에 열린 포지션이 있어 초기화하지 않습니다 — 먼저 청산한 뒤 다시 실행하세요")
        state = self._blank_state()
        self._recompute(state)
        self.save_state(state)
        self.killswitch.reset()
        return state


# ---------------------------------------------------------------------- 화면

def build_client(force_mock: bool = False, env: Optional[str] = "demo") -> BybitClient:
    """샌드박스 클라이언트. force_mock이면 키가 있어도 mock(네트워크 주문 없음)."""
    if force_mock:
        return BybitClient(api_key="", api_secret="", env=env or "demo")
    return BybitClient(env=env)


def format_live_line(*, now_ms: int, symbol: str, price: Optional[float], state: Dict,
                     ai_last: Optional[Dict], due_ms: Optional[int], note: str = "") -> str:
    """루프가 1분마다 찍는 한 줄 — 시세 · 보유/평가손익 · 최근 AI 판단 · 다음 점검까지 남은 시간."""
    when = datetime.fromtimestamp(int(now_ms) / 1000, tz=KST).strftime("%H:%M:%S")
    parts = [f"[demo-ai] {when} KST"]
    parts.append(f"{symbol} " + (f"{float(price):,.2f}" if price else "시세 조회 실패"))
    pos = state.get("position") or state.get("exchange_position")
    if pos:
        side = "롱" if str(pos.get("side") or "").lower() in ("buy", "long") else "숏"
        entry = float(pos.get("entry") or pos.get("entryPrice") or 0.0)
        qty = pos.get("qty") if pos.get("qty") is not None else pos.get("size")
        parts.append(f"보유 {side} {qty} @ {entry:,.2f}")
        pnl = DemoAIExperiment._unrealized(price, pos)
        if pnl is not None:
            r = f"({pnl['r']:+.2f}R)" if pnl.get("r") is not None else ""
            parts.append(f"평가 {pnl['usd']:+.2f} USDT{r}")
    else:
        parts.append("포지션 없음")
    if ai_last:
        decision = ai_last.get("decision") or "-"
        reason = (ai_last.get("reason") or "").replace("\n", " ")[:70]
        parts.append(f"최근 AI {decision}" + (f": {reason}" if reason else ""))
    if due_ms:
        remain = max(0, int(due_ms) - int(now_ms)) // 1000
        at = datetime.fromtimestamp(int(due_ms) / 1000, tz=KST).strftime("%H:%M:%S")
        parts.append(f"다음 점검 {at} ({remain // 60}분 {remain % 60:02d}초 남음)")
    if note:
        parts.append(note)
    return " · ".join(parts)


def print_banner(engine: DemoAIExperiment) -> None:
    print("[demo-ai] EXPERIMENTAL AI AUTONOMOUS MODE (Bybit Sandbox ONLY · NO REAL MONEY)", flush=True)
    if engine.mock:
        print("[demo-ai] 모드: mock — 키가 없거나 --mock입니다. 실제 주문은 나가지 않습니다.", flush=True)
    elif engine.env == "demo":
        print("[demo-ai] 모드: 데모 실주문 — api-demo.bybit.com · 모의 USDT만 움직입니다.", flush=True)
    else:
        print("[demo-ai] 모드: 테스트넷 실주문 — api-testnet.bybit.com · 모의 USDT만 움직입니다.", flush=True)
    lim = risk_limits(engine.env)
    print(f"[demo-ai] 안전 규칙: 최대 시드 ${lim['maxSeedUsd']:g} · 1회 손실 {engine.cfg.loss_pct:g}% · "
          f"레버리지 ≤ {engine.cfg.leverage:g}배 · 하루 {engine.cfg.max_trades_per_day}회 진입 · "
          f"일일 손실 {engine.cfg.daily_loss_pct:g}% 킬스위치 · 메인넷 차단", flush=True)
    print(f"[demo-ai] 판단: {engine.cfg.interval}분봉 마감 +{BAR_DELAY_SEC}초마다 AI가 진입·유지·청산 결정 · "
          f"최대 보유 {engine.cfg.max_hold_bars}봉 · {engine.cfg.tick_sec}초마다 상태 한 줄", flush=True)
    print(f"[demo-ai] AI: {engine.ai.describe() if hasattr(engine.ai, 'describe') else '준비됨'}", flush=True)
    print(f"[demo-ai] 기록: {engine.state_path} (모의매매·테스트넷 오토 모드 기록과 분리)", flush=True)


def print_record(rec: Dict) -> None:
    st = rec["stats"]
    line = (f"[demo-ai] {rec['ts']} · 봉 {rec['bar_kst']} KST · {rec['action']} · "
            f"{float(rec['price']):,.2f}")
    if rec.get("events"):
        line += " · " + " | ".join(rec["events"])
    print(line, flush=True)
    pos = rec.get("position")
    if pos:
        print(f"        보유 {pos['side']} {pos['qty']} · 진입 {float(pos['entry']):,.2f} · "
              f"손절 {float(pos['stop']):,.2f} · 목표 {float(pos['target']):,.2f}", flush=True)
    win = st.get("win_rate_pct")
    print(f"        체결 {st['trades']}건 · 승률 {win if win is not None else '-'}% · "
          f"순손익 {st['net_pnl']:+.2f} USDT · 평균 R {st['avg_r'] if st['avg_r'] is not None else '-'} · "
          f"자본 {rec.get('equity')} USDT", flush=True)


def print_status(engine: DemoAIExperiment) -> None:
    status = engine.status()
    acct = status.get("account") or {}
    label = (status.get("client") or {}).get("label")
    print(f"[demo-ai] 모드 {status['mode']}" + (f"({label})" if label else "") +
          f" · {status['symbol']} · 주소 {status['client']['baseUrl']}", flush=True)
    print(f"        진입 {('정지(--resume로 해제)' if engine.paused() else '허용')} · "
          f"AI {status.get('ai')}", flush=True)
    if status.get("account_error"):
        print(f"        계좌 조회 실패: {status['account_error']}", flush=True)
    else:
        print(f"        잔고 {acct.get('equity')} USDT · 리스크 기준 {acct.get('riskBaseUsd')} USDT · "
              f"열린 포지션 {len(status.get('positions') or [])}개", flush=True)
    state = status["state"]
    pos = state.get("position")
    if pos:
        print(f"        내 기록 포지션 {pos['side']} {pos['qty']} @ {float(pos['entry']):,.2f} "
              f"(주문 {pos.get('order_id')})", flush=True)
    ks = status["killswitch"]
    print(f"        킬스위치 진입 {'가능' if ks['entry']['ok'] else '금지'} ({ks['entry']['reason']}) · "
          f"오늘 체결 {ks['state'].get('trades')}건 · 실현손익 {ks['state'].get('realized_pnl')} · "
          f"오늘 진입 {(state.get('day') or {}).get('entries')}/{engine.cfg.max_trades_per_day}",
          flush=True)
    ai_last = state.get("ai_last") or {}
    if ai_last:
        print(f"        최근 AI {ai_last.get('decision')} · {ai_last.get('reason')}", flush=True)
    st = state["stats"]
    win = st.get("win_rate_pct")
    print(f"        누적 체결 {st.get('trades')}건 · 승률 {win if win is not None else '-'}% · "
          f"순손익 {st.get('net_pnl')} USDT · 손익미확인 {st.get('unknown_pnl')}건 · "
          f"추정손익 {st.get('estimated_pnl')}건", flush=True)
    if status.get("log"):
        print("        최근 스캔:", flush=True)
        for row in status["log"][-5:]:
            print(f"         · {row.get('bar_kst')} {row.get('action')} "
                  f"{' | '.join(row.get('events') or [])[:120]}", flush=True)


# ---------------------------------------------------------------------- 루프

def run_live_loop(engine: DemoAIExperiment, *, quiet: bool = False,
                  tick_sec: int = DEFAULT_TICK_SEC, max_rounds: Optional[int] = None,
                  clock=None, sleep=None, out=None) -> int:
    """봉 마감 +60초 점검 → 대기 중 tick_sec마다 상태 한 줄 → 다음 점검."""
    out = out or (lambda line: print(line, flush=True))
    clock = clock or (lambda: int(engine.now().timestamp() * 1000))
    sleep = sleep or time.sleep
    rounds = 0
    try:
        while True:
            rec = None
            try:
                rec = engine.tick()
            except Exception as exc:              # 시세·파일 실패는 이번 스캔 실패로 남기고 계속
                if not quiet:
                    out(f"[demo-ai] 스캔 실패: {exc} — 다음 점검까지 기다립니다")
            if rec is not None:
                if not quiet:
                    print_record(rec)
            rounds += 1

            if rec is None:
                due = clock() + RETRY_SEC * 1000
                if not quiet:
                    out(f"[demo-ai] 다음 점검 {_kst(due)} KST (스캔 실패 — {RETRY_SEC // 60}분 뒤 재시도)")
            else:
                due = next_bar_close_ms(clock(), engine.tf_ms)
                if not quiet:
                    out(f"[demo-ai] 다음 점검 {_kst(due)} KST · {int(tick_sec)}초마다 상태 한 줄 · "
                        "Ctrl+C로 멈춥니다")
            if max_rounds is not None and rounds >= int(max_rounds):
                return 0

            next_tick = clock() + max(1, int(tick_sec)) * 1000
            while True:
                now = clock()
                if now >= due:
                    break
                sleep(min(1.0, max(0.05, (due - now) / 1000.0)))
                now = clock()
                if quiet or now < next_tick:
                    continue
                state = engine.load_state()
                quote = engine.quote()
                out(format_live_line(now_ms=now, symbol=engine.cfg.symbol,
                                     price=quote.get("price"), state=state,
                                     ai_last=state.get("ai_last"), due_ms=due,
                                     note=quote.get("note") or ""))
                while next_tick <= now:
                    next_tick += max(1, int(tick_sec)) * 1000
    except KeyboardInterrupt:
        if not quiet:
            out("\n[demo-ai] 안전 종료. 열린 포지션이 있으면 거래소의 손절·목표가 계속 걸려 있습니다.")
        return 0


# ---------------------------------------------------------------------- CLI

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="데모 전용 AI 자율 매매 실험 모드(바이비트 샌드박스 전용)")
    ap.add_argument("--once", action="store_true", help="한 번만 판단(스케줄러·첫 점검용)")
    ap.add_argument("--loop", action="store_true", help="봉 마감 +60초마다 계속(Ctrl+C 종료)")
    ap.add_argument("--status", action="store_true", help="계좌·포지션·최근 AI 판단·기록")
    ap.add_argument("--mock", action="store_true", help="키가 있어도 mock으로만 돈다(주문 없음)")
    ap.add_argument("--demo", action="store_true", help="바이비트 Demo Trading으로 돈다(기본)")
    ap.add_argument("--testnet", action="store_true", help="바이비트 테스트넷 계좌로 돈다")
    ap.add_argument("--interval", default="15", choices=["15", "30"], help="판단 시간봉(기본 15분)")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--source", default="auto", choices=["auto", "okx", "bitget"])
    ap.add_argument("--loss-pct", type=float, default=3.0, help="1회 손실 예산(2~4%%)")
    ap.add_argument("--leverage", type=float, default=3.0, help="레버리지(1~3배)")
    ap.add_argument("--max-trades", type=int, default=4, help="하루 최대 진입 횟수")
    ap.add_argument("--daily-loss-pct", type=float, default=10.0, help="일일 손실 킬스위치(%%)")
    ap.add_argument("--max-hold-bars", type=int, default=96, help="시간 청산까지 최대 보유 봉")
    ap.add_argument("--no-time-exit", action="store_true", help="시간 청산을 쓰지 않는다")
    ap.add_argument("--tick-sec", type=int, default=DEFAULT_TICK_SEC, help="상태 한 줄 간격(초)")
    ap.add_argument("--model", default="", help="AI 모델 덮어쓰기(기본은 .env)")
    ap.add_argument("--state", default=None, help="상태 파일 경로(시험용)")
    ap.add_argument("--log", default=None, help="로그 파일 경로(시험용)")
    ap.add_argument("--killswitch", default=None, help="킬스위치 파일 경로(시험용)")
    ap.add_argument("--reset", action="store_true", help="로컬 기록 초기화(열린 포지션 있으면 거부)")
    ap.add_argument("--stop", action="store_true", help="신규 진입 수동 정지")
    ap.add_argument("--resume", action="store_true", help="수동 정지 해제")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    load_env_file()
    if args.testnet and args.demo:
        print("[demo-ai] --demo와 --testnet을 같이 쓸 수 없습니다.", flush=True)
        return 2
    env = "testnet" if args.testnet else "demo"
    cfg = ExperimentConfig(symbol=str(args.symbol).upper(), interval=str(args.interval),
                           source=args.source, loss_pct=args.loss_pct, leverage=args.leverage,
                           max_trades_per_day=args.max_trades, daily_loss_pct=args.daily_loss_pct,
                           max_hold_bars=args.max_hold_bars, time_exit=not args.no_time_exit,
                           tick_sec=max(5, int(args.tick_sec)),
                           state_path=args.state or "", log_path=args.log or "",
                           killswitch_path=args.killswitch or "")
    try:
        validate_settings(cfg)
    except ValueError as exc:
        print(f"[demo-ai] 설정 오류: {exc}", flush=True)
        return 2

    try:
        client = build_client(force_mock=args.mock, env=env)
    except BybitConfigError as exc:
        print(f"[demo-ai] 설정 오류: {exc}", flush=True)
        return 2

    try:
        engine = DemoAIExperiment(cfg, client=client, ai=AIDecider(model=args.model))
    except (BybitConfigError, ValueError) as exc:
        print(f"[demo-ai] 설정 오류: {exc}", flush=True)
        return 2

    if not args.quiet:
        print_banner(engine)
        sync = engine.sync_clock()
        if sync.get("error"):
            print(f"[demo-ai] 시각 동기화 실패(계속 진행): {sync['error']}", flush=True)
        elif not sync.get("mock"):
            print(f"[demo-ai] 거래소 시각과 {sync['offset_ms'] / 1000:+.1f}초 차이를 맞췄습니다", flush=True)

    if args.status:
        print_status(engine)
        return 0
    if args.stop or args.resume:
        if args.stop:
            os.makedirs(os.path.dirname(os.path.abspath(engine.pause_path)), exist_ok=True)
            Path(engine.pause_path).write_text(
                f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} 신규 진입 정지\n",
                encoding="utf-8")
        elif os.path.isfile(engine.pause_path):
            os.remove(engine.pause_path)
        print(f"[demo-ai] 신규 진입 {'정지' if args.stop else '재개'} — 돌고 있는 루프도 다음 스캔부터 "
              "따릅니다. 열린 포지션은 거래소 TP/SL이 계속 관리합니다.", flush=True)
        return 0

    lock = RunLock(engine.state_path + ".lock")
    if not lock.acquire():
        print(f"[demo-ai] 이미 실행 중인 것 같습니다({engine.state_path}.lock). 두 번 돌리면 주문이 "
              "겹칠 수 있어 멈춥니다.\n          확실히 꺼져 있다면 그 파일을 지우고 다시 실행하세요.",
              flush=True)
        return 1
    atexit.register(lock.release)

    if args.reset:
        try:
            engine.reset()
        except (RuntimeError, BybitError) as exc:
            print(f"[demo-ai] 초기화하지 않았습니다: {exc}", flush=True)
            return 1
        print("[demo-ai] 로컬 기록을 초기화했습니다(거래소 계좌는 그대로).", flush=True)
        return 0

    def once() -> int:
        try:
            rec = engine.tick()
        except Exception as exc:
            print(f"[demo-ai] 스캔 실패: {exc}", flush=True)
            return 1
        if not args.quiet:
            print_record(rec)
        return 0

    if args.loop:
        if not args.quiet:
            print(f"[demo-ai] {cfg.symbol} {cfg.interval}분봉 · 봉 마감 +{BAR_DELAY_SEC}초 점검 · "
                  f"{cfg.tick_sec}초마다 상태 한 줄 · 상태 {engine.state_path}", flush=True)
        return run_live_loop(engine, quiet=args.quiet, tick_sec=cfg.tick_sec)
    return once()


if __name__ == "__main__":
    raise SystemExit(main())
