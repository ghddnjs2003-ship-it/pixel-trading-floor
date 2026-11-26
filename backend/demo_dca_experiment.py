"""데모 전용 리치식 1~4차 물타기(DCA) 실험 엔진 — 바이비트 Demo Trading(모의 USDT)에서만 돈다.

무엇을 하나 (원본 규칙: RICHTrading/richtrading.github.io `b91184e` 커밋 `ctl/g/config.js`의 `dcaRules`·`followRules`)
1) **1차 진입** — 5분 완성봉 문맥으로 방향을 정한다. 기본은 Gemini가 5분봉+4시간봉의 단기 추세/반전을 보고
   `LONG`/`SHORT`/`HOLD`를 고르고, 키가 없거나 AI가 실패하면 기술적 필터(EMA20/50 배열 · 4시간 정렬 ·
   RSI14 과매수/과매도 · BB20/2.5σ 이탈)가 대신 고른다. 레버리지 기본 **50배**(1~50 조정 가능),
   1차 증거금은 가용 잔고의 **13%**.
2) **익절 — 2단계 분할 + 본절 보호**(`--split-tp`, 기본 켜짐) — 평단 대비 **증거금 +10%**(가격 변동
   10/레버리지 %)에 닿으면 보유의 **50%를 reduce-only 시장가로 부분 익절**하고, 남은 50%의 손절선을
   **본절(평단 + 증거금 1.5% ≈ 수수료 여유)**로 즉시 올려 원금 손실을 막는다. **+20%**에서 남은 물량을
   전량 익절한다. 50달러 소액에서 50% 수량·잔여 수량이 거래소 최소 주문 단위(qtyStep/minOrderQty/
   minNotionalValue)에 못 미치면 분할을 건너뛰고 **+10%에서 전량 익절**로 안전하게 돌아간다(사유를 로그에 남김).
   `--no-split-tp`면 원본처럼 +10% 전량 익절 하나만 쓴다. 1~4차 어느 때든 평단이 바뀌면 익절가도 새
   평단에서 다시 계산한다(avgReset). 1~3차에는 (분할 전까지) 손절 주문을 걸지 않는다(원본과 같음).
   분할이 켜져 있는 동안 거래소 TP는 **2차 목표(+20%)**에 걸리므로, 1차 부분 익절은 이 엔진이 한다.
3) **물타기 2·3·4차** — `basis: margin`(선은 증거금 %). 차수마다 **그 순간 평단**에서 다시 잰다.
   - 2차: 증거금 **-25%** 도달 + 5분봉 **RSI(14)** 롱 `<35`/숏 `>65` → 가용 잔고의 **15%** 추가.
     조건이 없어도 증거금 **-40%**(강제선)에 도달하면 추가한다(원본 `dcaDecision`의 force).
   - 3차: 증거금 **-40%** + **볼린저(20, 2.5σ)** 이탈(롱 하단 아래/숏 상단 위) → 가용 잔고의 **33%** 추가.
     강제선 **-60%**.
   - 4차: 증거금 **-60%** + 볼린저 이탈 → 남은 가용 잔고의 **100%**(사용 가능의 95% 상한) 추가.
     강제선 **-80%**.
4) **자동 손절** — 4차 진입 뒤에만, 평단→추정 청산가 거리의 **90%**(`--sl-dist-pct`) 지점에
   reduce-only `STOP` 주문을 건다(추정 청산가면 원본은 80% — `--sl-est-dist-pct`). `--sl-mode pct`면
   증거금 `--sl-pct`(기본 50%) 지점에 건다.
5) **종목** — 기본 `ETHUSDT`(50달러 시드로도 4분할 최소 주문 가능), `SOLUSDT`·`BTCUSDT` 지원.
   최소 주문 수량·최소 명목가 미달이면 주문하지 않고 사유에 숫자를 남긴다.
6) **루프** — 포지션이 **없을 때**는 30초마다 봉으로 물타기 선을 보고, 5분봉이 마감될 때만 1차 진입을
   새로 판단한다(AI 호출도 그때 한 번 — 무료 한도 보호). 포지션이 **열려 있는 동안**은 **5초마다**
   바이비트 현재가(시세 티커만 조회)를 읽어 1차 분할 익절·본절 이탈·2차 익절·물타기 선 도달을 즉시
   확인한다. 조건(RSI·볼린저)은 마지막 5분봉 스캔이 남긴 결과를 쓰고, 강제선은 가격만으로 판단한다.
   물타기 차수·시간 청산처럼 봉이 필요한 처리는 5초 감시가 "전체 점검"으로 넘긴다. 대기 중 1분마다
   상태 한 줄. Ctrl+C 즉시 종료(열린 포지션의 TP/SL은 거래소에 남는다).

지키는 선 (절대)
- **데모 전용**: `BybitClient`의 `env="demo"`(api-demo.bybit.com)만 받는다. 테스트넷·메인넷 클라이언트는
  만들 때부터 `BybitConfigError`로 거부한다. 메인넷은 `bybit_client` 자체가 열지 않는다.
- **기존 검증 자산과 완전 분리**: `backend/paper_loop.py`·`backend/testnet_auto.py`·`backend/demo_ai_experiment.py`와
  그 기록(`data/paper_*`, `data/testnet_auto_*`, `data/demo_ai_experiment_*`)은 읽지도 쓰지도 않는다.
  이 모듈의 파일은 모두 `demo_dca_*`이며, 경로가 다른 모듈 파일을 가리키면 만들 때부터 거부한다.
- **다른 엔진과 동시 실행 금지**: 같은 데모 계좌에서 오토 모드·AI 실험이 돌고 있으면(잠금 파일이 살아 있으면)
  `--loop`/`--once` 실행을 거부한다(수동 확인). `--mock`은 주문이 없으므로 허용.
- **결과 불확실은 재시도하지 않는다**: 주문 전송이 오류로 끝나면 같은 스캔에서 다시 보내지 않고,
  다음 스캔에서 거래소 포지션을 보고 판단한다.
- **AI 장애는 진입 중단이 아니라 관망**: 호출·해석 실패는 기술적 필터로 넘어가고, 그것도 애매하면 HOLD.

실행
    python -m backend.demo_dca_experiment --once                  # 1회 점검(키 없으면 mock)
    python -m backend.demo_dca_experiment --once --mock           # 키가 있어도 드라이런
    python -m backend.demo_dca_experiment --loop                  # 포지션 없으면 30초 · 있으면 5초 감시
    python -m backend.demo_dca_experiment --loop --no-split-tp    # 원본처럼 +10% 전량 익절만
    python -m backend.demo_dca_experiment --status                # 계좌·포지션·차수·성적
    python -m backend.demo_dca_experiment --reset / --stop / --resume
    run_demo_dca_experiment.bat                                   # Windows 더블클릭

파일
    data/demo_dca_state.json          (mock이면 ...state.mock.json)
    data/demo_dca_log.jsonl           (mock이면 ...log.mock.jsonl)
    data/demo_dca_killswitch.json     (mock이면 ...killswitch.mock.json)
    data/demo_dca_state*_pause.txt    (--stop 표시)
    data/demo_dca_state*.json.lock    (동시 실행 방지)
"""

from __future__ import annotations

import argparse
import atexit
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .backtest import load_candles
from .bybit_client import (ENV_LABELS, BybitAPIError, BybitClient,
                           BybitConfigError, BybitError, Instrument, OrderRejected,
                           fmt_dec, floor_to_step, round_to_step)
from .config import get_provider_config, load_env_file
from .providers import make_provider
from .risk import KillSwitch, KillSwitchRules
from .strategy import atr_series, ema_series
from .testnet_auto import RunLock

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
KST = timezone(timedelta(hours=9))

RULES_VERSION = "dca-20261105-r1"       # 원본 dca-rules.json 판 (ctl/g/config.js b91184e)
RULES_SOURCE = "partner-reply-2026-10-16"

BAR_TF = "5"                            # 5분 완성봉
BAR_TF_MS = 300_000
BAR_DELAY_SEC = 30                      # 봉 마감 뒤 이만큼 지나면 그 봉을 '확정'으로 본다
MIN_CANDLES = 120                       # RSI Wilder가 99개 종가를 쓴다 — 여유 있게
MIN_CANDLES_4H = 30
RSI_WINDOW = 99                         # dca-ind.js rsiBars
STALE_MS = 600_000                      # 마지막 완성봉이 10분 넘게 옛것이면 조건은 거짓
DEFAULT_SCAN_SEC = 30
DEFAULT_STATUS_SEC = 60
DEFAULT_POS_TICK_SEC = 5                # 포지션이 열려 있는 동안 현재가를 보는 간격(초)
DEFAULT_TP2_PCT = 20.0                  # 분할 익절 2차 목표(증거금 +20%)
DEFAULT_TP1_FRAC = 0.5                  # 1차에서 익절하는 보유 비율
DEFAULT_BE_PCT = 1.5                    # 1차 뒤 본절 손절 = 평단 + 증거금 1.5%(수수료 여유)
RETRY_SEC = 60

DEFAULT_LEV = 50.0
MAX_LEV = 50.0
MIN_BASE_USD = 10.0                     # 원본 follow-math MATH.minBase
AVAIL_FRAC = 0.95                       # 원본 follow-math MATH.availFrac
LIQ_BUFFER = 0.0038                     # 원본 경험식: 예상 청산 폭 = 1/L − 0.0038
MIN_TICKS = 2                           # 자동 손절은 청산가에서 2 tick 이상 안쪽
MIN_LIQ_GAP = 0.001                     # 거래소 청산가가 평단과 0.1% 미만이면 믿지 않는다

LAST_STAGE = 4
FORBIDDEN_PREFIXES = ("paper_", "testnet_auto", "demo_ai_experiment", "ai_cache")
REQUIRED_PREFIX = "demo_dca_"


# ---------------------------------------------------------------------- 차수 표

@dataclass(frozen=True)
class StageRule:
    """원본 dca-rules.json의 stages[] 한 줄 — 선은 증거금 기준 %."""

    stage: int
    cond_pct: float          # 이 증거금 손실에 닿고 아래 cond가 참이면 추가
    force_pct: float         # 조건과 무관하게 이 손실에 닿으면 추가(강제선)
    avail_pct: float         # 그 차수에 넣는 증거금 비율(기준 잔고 대비 %)
    cond: str                # "rsi" | "bb"
    rsi_len: int = 14
    long_below: float = 35.0
    short_above: float = 65.0
    bb_len: int = 20
    bb_k: float = 2.5
    reserve_lev_pct: Optional[float] = None   # 마지막 차수에만: 남겨 둘 증거금(배율×비율)

    def to_dict(self) -> Dict:
        return asdict(self)


DEFAULT_STAGES: Tuple[StageRule, StageRule, StageRule] = (
    StageRule(stage=2, cond_pct=25.0, force_pct=40.0, avail_pct=15.0, cond="rsi",
              rsi_len=14, long_below=35.0, short_above=65.0),
    StageRule(stage=3, cond_pct=40.0, force_pct=60.0, avail_pct=33.0, cond="bb",
              bb_len=20, bb_k=2.5),
    StageRule(stage=4, cond_pct=60.0, force_pct=80.0, avail_pct=100.0, cond="bb",
              bb_len=20, bb_k=2.5),
)


# ---------------------------------------------------------------------- 설정

@dataclass
class DCAConfig:
    """실험 설정. 원본에서 회원이 바꿀 수 있는 값(배율·1차·익절·운용 상한)만 기본으로 열어 둔다."""

    symbol: str = "ETHUSDT"
    leverage: float = DEFAULT_LEV
    first_pct: float = 13.0              # 1차 증거금 = 가용 잔고 × 이 %(원본 member.firstPct)
    tp_pct: float = 10.0                 # 증거금 기준 1차 익절 %(원본 member.tpPct)
    split_tp: bool = True                # 2단계 분할 익절 + 본절 보호(끄면 +tp_pct 전량 익절)
    tp2_pct: float = DEFAULT_TP2_PCT     # 2차 목표(남은 물량) — 증거금 %
    tp1_frac: float = DEFAULT_TP1_FRAC   # 1차에서 줄이는 비율(0.5 = 50%)
    be_pct: float = DEFAULT_BE_PCT       # 1차 뒤 본절 손절(증거금 %)
    pos_tick_sec: int = DEFAULT_POS_TICK_SEC   # 포지션 보유 중 현재가 확인 간격(초)
    cap_pct: float = 100.0               # 운용 상한(가용 잔고 대비 %) — 원본 autoRun.capPct
    source: str = "auto"                 # 5분봉 출처 auto|bybit|okx|bitget
    limit: int = 200                     # 받을 5분봉 수
    four_hour_limit: int = 100           # 받을 4시간봉 수
    stages: Tuple[StageRule, ...] = DEFAULT_STAGES
    reserve_lev_pct: Optional[float] = None   # 마지막 차수에 강제 적용(원본 0.48, 기본 없음)
    entry_mode: str = "auto"             # auto(AI→기술) | ai | tech
    force_entry: str = ""                # "", "long", "short" — 드라이런에서 진입 방향 고정(시험용)
    tp_after_add: str = "reset"          # 원본 avgReset=true
    sl_mode: str = "auto"                # auto(평단→청산가 90%) | pct | off
    sl_dist_pct: float = 90.0            # 거래소 청산가를 알 때 자동 손절 거리 %
    sl_est_dist_pct: float = 90.0        # 추정 청산가일 때(원본 기본은 80)
    sl_pct: float = 50.0                 # sl_mode=pct일 때 증거금 손실 %
    max_trades_per_day: int = 6          # 하루에 시작하는 매매(1~4차 한 묶음) 수
    daily_loss_pct: float = 25.0         # 일일 손실 킬스위치
    max_consecutive_losses: int = 3
    min_equity_pct: float = 50.0         # 시작 자본의 이 % 아래면 사람 확인 전까지 중단
    max_hold_bars: int = 288             # 5분봉 24시간 — 시간 청산
    time_exit: bool = True
    cooldown_min: int = 0                # 청산 뒤 새 매매까지 쉬는 시간(0=같은 봉만 금지)
    scan_sec: int = DEFAULT_SCAN_SEC
    status_sec: int = DEFAULT_STATUS_SEC
    data_dir: str = str(DATA)
    state_path: str = ""
    log_path: str = ""
    killswitch_path: str = ""

    def to_dict(self) -> Dict:
        out = asdict(self)
        out["stages"] = [s.to_dict() if isinstance(s, StageRule) else s for s in self.stages]
        return out


def validate_settings(cfg: DCAConfig) -> None:
    """주문 전에 규칙 범위를 검증한다 — 잘못된 값은 실행 자체를 막는다."""
    lev = float(cfg.leverage)
    if not (1.0 <= lev <= MAX_LEV):
        raise ValueError(f"레버리지는 1~{MAX_LEV:g}배만 허용합니다: {lev:g}배")
    if not (1.0 <= float(cfg.first_pct) <= 30.0):
        raise ValueError(f"1차 비중은 1~30%만 허용합니다: {cfg.first_pct}%")
    if not (5.0 <= float(cfg.tp_pct) <= 50.0):
        raise ValueError(f"익절(증거금)은 5~50%만 허용합니다: {cfg.tp_pct}%")
    if not (float(cfg.tp_pct) < float(cfg.tp2_pct) <= 200.0):
        raise ValueError(f"2차 목표는 1차({cfg.tp_pct}%)보다 크고 200% 이하여야 합니다: {cfg.tp2_pct}%")
    if not (0.1 <= float(cfg.tp1_frac) <= 0.9):
        raise ValueError(f"분할 비율은 10~90%만 허용합니다: {cfg.tp1_frac}")
    if not (0.0 <= float(cfg.be_pct) < float(cfg.tp_pct)):
        raise ValueError(f"본절 손절은 0 이상 1차 익절({cfg.tp_pct}%) 미만이어야 합니다: {cfg.be_pct}%")
    if not (1 <= int(cfg.pos_tick_sec) <= 300):
        raise ValueError(f"포지션 감시 간격은 1~300초만 허용합니다: {cfg.pos_tick_sec}초")
    if not (1.0 <= float(cfg.cap_pct) <= 100.0):
        raise ValueError(f"운용 상한은 1~100%만 허용합니다: {cfg.cap_pct}%")
    if str(cfg.sl_mode) not in ("auto", "pct", "off"):
        raise ValueError(f"손절 방식은 auto·pct·off만: {cfg.sl_mode}")
    if not (5.0 <= float(cfg.sl_pct) <= 75.0):
        raise ValueError(f"직접 손절은 5~75%만 허용합니다: {cfg.sl_pct}%")
    for name, value in (("sl_dist_pct", cfg.sl_dist_pct), ("sl_est_dist_pct", cfg.sl_est_dist_pct)):
        if not (1.0 <= float(value) <= 99.0):
            raise ValueError(f"{name}는 1~99%만 허용합니다: {value}")
    if str(cfg.entry_mode) not in ("auto", "ai", "tech"):
        raise ValueError(f"진입 방식은 auto·ai·tech만: {cfg.entry_mode}")
    if str(cfg.force_entry or "").lower() not in ("", "long", "short"):
        raise ValueError(f"--force-entry는 long 또는 short만: {cfg.force_entry}")
    if str(cfg.source) not in ("auto", "bybit", "okx", "bitget"):
        raise ValueError(f"봉 출처는 auto·bybit·okx·bitget만: {cfg.source}")
    if not (1 <= int(cfg.max_trades_per_day) <= 20):
        raise ValueError(f"하루 매매 수는 1~20회만 허용합니다: {cfg.max_trades_per_day}")
    if not (1.0 <= float(cfg.daily_loss_pct) <= 90.0):
        raise ValueError(f"일일 손실 킬스위치는 1~90%만 허용합니다: {cfg.daily_loss_pct}%")
    if not (1.0 <= float(cfg.min_equity_pct) <= 99.0):
        raise ValueError(f"자본 하한은 1~99%만 허용합니다: {cfg.min_equity_pct}%")
    if not (0 <= int(cfg.max_hold_bars) <= 100000):
        raise ValueError(f"최대 보유 봉 수가 올바르지 않습니다: {cfg.max_hold_bars}")
    stages = list(cfg.stages or [])
    if len(stages) != 3 or [s.stage for s in stages] != [2, 3, 4]:
        raise ValueError("차수 표는 2·3·4차가 빠짐 없이 이어져야 합니다")
    for s in stages:
        if not (1 <= s.cond_pct < s.force_pct <= 95):
            raise ValueError(f"{s.stage}차 선이 올바르지 않습니다: cond {s.cond_pct} < force {s.force_pct} ≤ 95")
        if not (1 <= s.avail_pct <= 100):
            raise ValueError(f"{s.stage}차 비중은 1~100%: {s.avail_pct}")
        if s.cond == "rsi" and not (0 < s.long_below < s.short_above < 100):
            raise ValueError(f"{s.stage}차 RSI 선이 올바르지 않습니다")
        if s.cond == "bb" and s.bb_k < 0.5:
            raise ValueError(f"{s.stage}차 볼린저 k가 너무 작습니다: {s.bb_k}")
    if cfg.reserve_lev_pct is not None and not (0 <= float(cfg.reserve_lev_pct) <= 0.5):
        raise ValueError(f"reserve_lev_pct는 0~0.5만: {cfg.reserve_lev_pct}")


def stage_rule(cfg: DCAConfig, stage: int) -> Optional[StageRule]:
    for s in cfg.stages:
        if s.stage == int(stage):
            return s
    return None


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
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return round(v, nd)


def _f(value) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if (math.isnan(v) or math.isinf(v)) else v


def _side_ko(side: str) -> str:
    return "롱" if str(side).lower() in ("buy", "long") else "숏"


def _env_flag(name: str, default: bool = False) -> bool:
    """환경변수 참·거짓 — DCA_SPLIT_TP 같은 스위치용(이상한 값은 기본값)."""
    raw = os.getenv(name)
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in ("1", "true", "yes", "on", "y"):
        return True
    if value in ("0", "false", "no", "off", "n"):
        return False
    return default


def _dir_of(side: str) -> int:
    return 1 if str(side).lower() in ("buy", "long") else -1


def _decimals(step: Decimal) -> int:
    text = format(_dec(step).normalize(), "f")
    return len(text.split(".")[1]) if "." in text else 0


def _dec(value) -> Decimal:
    return Decimal(str(value))


def ceil_to_step(value, step) -> Decimal:
    unit = _dec(step)
    return (_dec(value) / unit).to_integral_value(rounding=ROUND_CEILING) * unit


def floor_step(value, step) -> Decimal:
    unit = _dec(step)
    return (_dec(value) / unit).to_integral_value(rounding=ROUND_FLOOR) * unit


def tf_ms(interval: str = BAR_TF) -> int:
    return BAR_TF_MS


def next_bar_close_ms(now_ms: int, step_ms: int = BAR_TF_MS, delay_sec: int = BAR_DELAY_SEC) -> int:
    return ((int(now_ms) // int(step_ms)) + 1) * int(step_ms) + int(delay_sec * 1000)


def closed_bars(candles: Sequence[Dict], step_ms: int, now_ms: int) -> List[Dict]:
    """진행 중인 봉을 뺀 확정 봉만. 5분봉 지표는 완성봉으로만 계산한다(원본 closedOnly)."""
    out = sorted((c for c in candles if c.get("t") is not None), key=lambda c: int(c["t"]))
    while len(out) > 1 and int(out[-1]["t"]) + int(step_ms) > int(now_ms):
        out.pop()
    return out


# ---------------------------------------------------------------------- 지표 (원본 dca-ind.js 이식)

def rsi_wilder(closes: Sequence[float], length: int = 14) -> Optional[float]:
    """Wilder RSI — 마지막 99개 종가, 처음 length개 변화의 평균으로 시드하고 이후 Wilder 평활.

    원본 dca-ind.js와 같은 식: 평균 손실 0이면 100(이익도 0이면 50).
    """
    if not isinstance(length, int) or length < 2 or length >= RSI_WINDOW:
        return None
    if len(closes) < RSI_WINDOW:
        return None
    window = [float(c) for c in closes[-RSI_WINDOW:]]
    if any(math.isnan(v) or math.isinf(v) for v in window):
        return None
    gain = 0.0
    loss = 0.0
    for i in range(1, length + 1):
        diff = window[i] - window[i - 1]
        if diff > 0:
            gain += diff
        else:
            loss -= diff
    avg_gain = gain / length
    avg_loss = loss / length
    for i in range(length + 1, len(window)):
        diff = window[i] - window[i - 1]
        avg_gain = (avg_gain * (length - 1) + (diff if diff > 0 else 0.0)) / length
        avg_loss = (avg_loss * (length - 1) + (-diff if diff < 0 else 0.0)) / length
    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def bollinger(closes: Sequence[float], length: int = 20, k: float = 2.5) -> Optional[Dict]:
    """모집단 표준편차(÷len)로 중간·상단·하단 — 원본 dca-ind.js bollinger와 같은 식."""
    if not isinstance(length, int) or length < 2 or k is None or k < 0:
        return None
    if len(closes) < length:
        return None
    window = [float(c) for c in closes[-length:]]
    if any(math.isnan(v) or math.isinf(v) for v in window):
        return None
    mid = sum(window) / length
    sd = math.sqrt(sum((v - mid) ** 2 for v in window) / length)
    return {"mid": mid, "upper": mid + k * sd, "lower": mid - k * sd, "sd": sd}


def cond_met(rule: StageRule, dir_: int, closes: Sequence[float], px: float,
             last_tick: Optional[int], now_ms: Optional[int],
             stale_ms: int = STALE_MS) -> bool:
    """그 차수의 5분봉 조건 — 모르면 거짓(그 차수는 강제선만 동작한다).

    원본 condMet: 봉이 모자라거나·마지막 완성봉이 닫힌 지 stale_ms가 지났거나·모르는 꼴이면 거짓.
    """
    if dir_ not in (1, -1):
        return False
    if not last_tick or now_ms is None:
        return False
    if int(now_ms) - (int(last_tick) + BAR_TF_MS) > int(stale_ms):
        return False
    if rule.cond == "rsi":
        value = rsi_wilder(closes, rule.rsi_len)
        if value is None:
            return False
        return value < rule.long_below if dir_ == 1 else value > rule.short_above
    if rule.cond == "bb":
        bands = bollinger(closes, rule.bb_len, rule.bb_k)
        if bands is None or px is None or px <= 0:
            return False
        return px < bands["lower"] if dir_ == 1 else px > bands["upper"]
    return False


def adverse_pct(dir_: int, avg: float, px: float, lev: float) -> float:
    """불리한 쪽 움직임을 증거금 %로 — 평단 대비 가격 변화 × 배율(원본 adversePct). 음수 = 손실."""
    if not (avg > 0) or not (lev > 0):
        return 0.0
    return (dir_ * (avg - px) / avg) * 100.0 * lev


def dca_decision(margin_pct: float, rule: StageRule, cond_ok: bool = False) -> Optional[str]:
    """강제선 이상이면 force, 조건부 선 이상이고 조건이 참이면 cond (원본 dcaDecision)."""
    if rule is None or margin_pct is None or math.isnan(margin_pct):
        return None
    if margin_pct >= rule.force_pct - 1e-9:
        return "force"
    if margin_pct >= rule.cond_pct - 1e-9 and cond_ok is True:
        return "cond"
    return None


def stage_margin_pct(rule: StageRule, lev: float, reserve_lev_pct: Optional[float] = None) -> float:
    """그 차수의 증거금 비율 — 마지막 차수에 reserveLevPct가 있으면 min(95, avail − 배율×예비)."""
    reserve = rule.reserve_lev_pct if reserve_lev_pct is None else reserve_lev_pct
    if reserve is None:
        return float(rule.avail_pct)
    return min(95.0, float(rule.avail_pct) - float(lev) * float(reserve))


def margin_amount(base: float, pct: float, available: float, avail_frac: float = AVAIL_FRAC) -> float:
    """base × pct% — 사용 가능 잔고의 avail_frac(0.95) 상한(원본 availCap)."""
    if not (base > 0) or not (pct > 0):
        return 0.0
    amount = float(base) * float(pct) / 100.0
    cap = max(0.0, float(available)) * float(avail_frac)
    return max(0.0, min(amount, cap))


def qty_for(margin: float, lev: float, px: float, spec: Instrument) -> Decimal:
    """증거금 × 배율 ÷ 가격 → qtyStep 내림. 최소 수량·최소 명목가 미달이면 OrderRejected(숫자를 남긴다)."""
    if not (px > 0):
        raise OrderRejected("가격이 0이라 수량을 계산할 수 없습니다")
    if not (margin > 0) or not (lev > 0):
        raise OrderRejected(f"증거금 ${margin:.2f}·배율 {lev:g}배로는 주문할 수 없습니다")
    raw = _dec(margin * lev / px)
    qty = floor_to_step(raw, spec.qty_step)
    notional = float(qty) * px
    if qty <= 0 or qty < spec.min_order_qty:
        min_notional = float(spec.min_order_qty) * px
        raise OrderRejected(
            f"증거금 ${margin:.2f}×{lev:g}배 = 명목 ${margin * lev:,.2f} → 수량 {fmt_dec(qty)}이(가) "
            f"최소 주문 수량 {fmt_dec(spec.min_order_qty)}(약 ${min_notional:,.2f})에 못 미칩니다 — "
            "종목을 ETH/SOL로 바꾸거나 1차 비중을 올리세요")
    if spec.min_notional is not None and notional < float(spec.min_notional):
        raise OrderRejected(
            f"명목가 ${notional:,.2f}가 최소 주문 금액 ${fmt_dec(spec.min_notional)}보다 작습니다 — 주문하지 않습니다")
    if spec.max_order_qty is not None and qty > spec.max_order_qty:
        raise OrderRejected(f"수량 {fmt_dec(qty)}이 최대 주문 수량 {fmt_dec(spec.max_order_qty)}을 넘습니다")
    return qty


def tp_price_for(dir_: int, avg: float, tp_pct: float, lev: float, tick) -> Decimal:
    """익절가 = 평단 × (1 + 방향 × 익절% / (100×배율)) — 원본 tpPriceFor."""
    return round_to_step(avg * (1 + (dir_ * float(tp_pct)) / (100.0 * float(lev))), tick)


def pct_stop_price(dir_: int, avg: float, sl_pct: float, lev: float, tick) -> Decimal:
    """직접 % 손절 = 평단 × (1 − 방향 × 손절%/(100×배율)), 평단 쪽 tick 반올림(원본 pctStopFor)."""
    raw = avg * (1 - (dir_ * abs(float(sl_pct))) / (100.0 * float(lev)))
    unit = _dec(tick)
    n = _dec(raw) / unit
    k = n.to_integral_value(rounding=ROUND_CEILING) if dir_ == 1 else n.to_integral_value(rounding=ROUND_FLOOR)
    return k * unit


def breakeven_stop_price(dir_: int, avg: float, be_pct: float, lev: float, tick) -> Decimal:
    """본절 손절가 = 평단 × (1 + 방향 × 본절%(증거금) / (100×배율)) — 최소 1 tick 수익 쪽.

    50배에서 증거금 +1.5%는 가격 +0.03%다. tickSize가 그보다 크면 1 tick만 수익 쪽에 둔다
    (거래소는 손절가가 진입가와 같으면 주문을 거부한다).
    """
    if not (avg > 0) or not (lev > 0) or not (tick > 0):
        return Decimal("0")
    raw = float(avg) * (1 + (dir_ * float(be_pct or 0.0)) / (100.0 * float(lev)))
    unit = _dec(tick)
    base = _dec(avg) / unit
    n = (_dec(raw) / unit)
    if dir_ == 1:
        k = max(n.to_integral_value(rounding=ROUND_CEILING),
                base.to_integral_value(rounding=ROUND_FLOOR) + 1)
    else:
        k = min(n.to_integral_value(rounding=ROUND_FLOOR),
                base.to_integral_value(rounding=ROUND_CEILING) - 1)
    return k * unit


def split_qty(qty, spec: Instrument, frac: float = 0.5,
              price: Optional[float] = None) -> Tuple[Decimal, Optional[str]]:
    """분할 익절 수량 — 보유의 frac(기본 50%)을 qtyStep으로 내림한다.

    (수량, 실패 사유). 부분 수량·잔여 수량이 최소 주문 수량에 못 미치거나 부분 명목가가 최소 주문
    금액에 못 미치면 수량 0과 사유를 돌려준다 — 호출 쪽은 전량 익절로 돌아간다(소액 폴백).
    """
    total = floor_to_step(qty, spec.qty_step)
    if total <= 0:
        return Decimal("0"), "보유 수량이 없습니다"
    part = floor_to_step(_dec(total) * _dec(frac), spec.qty_step)
    rest = _dec(total) - part
    if part <= 0:
        return Decimal("0"), (f"분할 수량이 최소 주문 수량({fmt_dec(spec.min_order_qty)})·"
                              f"qtyStep({fmt_dec(spec.qty_step)})에 못 미칩니다")
    if part < spec.min_order_qty:
        return Decimal("0"), (f"분할 수량 {fmt_dec(part)}이 최소 주문 수량 "
                              f"{fmt_dec(spec.min_order_qty)} 미만입니다")
    if rest < spec.min_order_qty:
        return Decimal("0"), (f"남는 수량 {fmt_dec(rest)}이 최소 주문 수량 "
                              f"{fmt_dec(spec.min_order_qty)} 미만입니다")
    if spec.min_notional is not None and price:
        notional = float(part) * float(price)
        if notional < float(spec.min_notional):
            return Decimal("0"), (f"분할 명목가 ${notional:,.2f}가 최소 주문 금액 "
                                  f"${fmt_dec(spec.min_notional)} 미만입니다")
    return part, None


def liq_of(dir_: int, avg: float, lev: float, liquid_price=None) -> Optional[Dict]:
    """청산가 — 거래소 값이 손실 쪽이고 평단과 0.1%~1/L 사이라면 그것, 아니면 경험식(원본 liqOf)."""
    if dir_ not in (1, -1) or not (avg > 0) or not (lev >= 1):
        return None
    ex = _f(liquid_price) if liquid_price is not None else 0.0
    if ex > 0:
        d = ((avg - ex) * dir_) / avg
        if d >= MIN_LIQ_GAP and d <= 1.0 / lev:
            return {"liq": ex, "src": "exchange"}
    width = 1.0 / lev - LIQ_BUFFER
    if not (width > MIN_LIQ_GAP):
        return None
    return {"liq": avg * (1 - dir_ * width), "src": "estimate"}


def auto_sl_trigger(dir_: int, avg: float, liq: float, src: str, tick,
                    dist_pct: float = 90.0, est_dist_pct: float = 90.0) -> Optional[Decimal]:
    """평단 + d×(청산가−평단) — 평단 쪽 tick 반올림, 청산가에서 2 tick 이상 안쪽(원본 autoSlTrigger)."""
    d = (float(est_dist_pct) if src == "estimate" else float(dist_pct)) / 100.0
    if dir_ not in (1, -1) or not (avg > 0) or not (liq > 0) or not (d > 0) or not (tick > 0):
        return None
    raw = avg + d * (liq - avg)
    bound = liq + dir_ * MIN_TICKS * float(tick)
    if dir_ == 1:
        trigger = max(ceil_to_step(raw, tick), ceil_to_step(bound, tick))
    else:
        trigger = min(floor_step(raw, tick), floor_step(bound, tick))
    if dir_ * (avg - float(trigger)) <= 0:
        return None
    return trigger


# ---------------------------------------------------------------------- 클라이언트

class DcaBybitClient(BybitClient):
    """리치식 DCA 실험용 클라이언트 — 부모의 샌드박스 주소 가드는 그대로, 레버리지 상한만 50배로 연다.

    부모의 `place_market_order`는 1회 손실 2~4%·레버리지 5배 규칙(50달러 안전 전략)에 묶여 있다.
    이 실험은 증거금 비율로 수량을 정하므로 주문 경로를 따로 두되, 서명·주소·규격 조회는 부모 것을 쓴다.
    """

    MAX_DCA_LEVERAGE = MAX_LEV

    @staticmethod
    def validate_leverage(leverage) -> float:
        lev = float(DEFAULT_LEV if leverage is None else leverage)
        if not (1.0 <= lev <= MAX_LEV):
            raise ValueError(f"이 실험의 레버리지는 1~{MAX_LEV:g}배만 허용합니다: {lev:g}배")
        return lev

    # ---------------------------------------------------------------- 조회

    def get_balance_detail(self, coin: str = "USDT") -> Dict:
        """잔고 + 진짜 쓸 수 있는 증거금. 데모 통합계좌는 totalAvailableBalance를 우선한다."""
        coin = coin.upper()
        if self.is_mock:
            bal = self.get_balance(coin)
            return {**bal, "totalAvailable": bal.get("equity"), "usedMargin": 0.0}
        data = self._request("GET", "/v5/account/wallet-balance",
                             {"accountType": "UNIFIED", "coin": coin})
        accounts = (data.get("result") or {}).get("list") or []
        if not accounts:
            raise BybitAPIError(-1, "잔고 응답에 계정이 없습니다", path="/v5/account/wallet-balance")
        account = accounts[0]
        rows = {row.get("coin"): row for row in (account.get("coin") or [])}
        row = rows.get(coin) or {}
        equity = _f(row.get("equity")) or _f(row.get("walletBalance")) or _f(account.get("totalEquity"))
        total_available = _f(account.get("totalAvailableBalance"))
        available = total_available or _f(row.get("availableToWithdraw")) or _f(row.get("walletBalance")) or equity
        return {"mock": False, "coin": coin, "equity": equity, "available": available,
                "wallet": _f(row.get("walletBalance")) or equity,
                "totalAvailable": total_available or None,
                "accountType": account.get("accountType", "UNIFIED")}

    def get_position(self, symbol: str) -> Optional[Dict]:
        """열린 포지션 하나 — 청산가(liqPrice)까지. 없으면 None."""
        symbol = symbol.upper()
        if self.is_mock:
            pos = self._mock_positions.get(symbol)
            if not pos or float(pos.get("size") or 0) <= 0:
                return None
            row = dict(pos)
            lev = float(row.get("leverage") or DEFAULT_LEV)
            est = liq_of(_dir_of(row["side"]), float(row["entryPrice"]), lev)
            row["liqPrice"] = est["liq"] if est else 0.0
            return row
        data = self._request("GET", "/v5/position/list", {"category": "linear", "symbol": symbol})
        rows = []
        for item in (data.get("result") or {}).get("list") or []:
            size = _f(item.get("size"))
            if size <= 0:
                continue
            rows.append({
                "symbol": item.get("symbol"), "side": item.get("side"), "size": size,
                "entryPrice": _f(item.get("avgPrice")), "liqPrice": _f(item.get("liqPrice")),
                "stopLoss": _f(item.get("stopLoss")), "takeProfit": _f(item.get("takeProfit")),
                "leverage": _f(item.get("leverage")), "unrealisedPnl": _f(item.get("unrealisedPnl")),
                "updatedTime": item.get("updatedTime"),
            })
        if not rows:
            return None
        return max(rows, key=lambda p: float(p["size"]))

    # ---------------------------------------------------------------- 주문

    def set_mock_price(self, symbol: str, price: float) -> None:
        """mock 장부의 시세를 봉 종가와 맞춘다(드라이런·시험이 같은 가격을 보게)."""
        if self.is_mock and price and price > 0:
            self._mock_prices[symbol.upper()] = float(price)

    def place_dca_order(self, symbol: str, side: str, qty, reduce_only: bool = False) -> Dict:
        """시장가 주문(증거금 기준 수량) — TP/SL은 체결 뒤 set_trading_stop으로 평단에서 건다."""
        symbol = symbol.upper()
        side_n = self.normalize_side(side)
        amount = floor_to_step(qty, self.get_instrument(symbol).qty_step)
        if amount <= 0:
            raise OrderRejected(f"{symbol} 주문 수량 {qty}이 qtyStep보다 작습니다")
        payload = {
            "category": "linear", "symbol": symbol, "side": side_n, "orderType": "Market",
            "qty": fmt_dec(amount), "positionIdx": 0,
        }
        if reduce_only:
            payload["reduceOnly"] = True
        if self.is_mock:
            self._mock_seq += 1
            order_id = f"mock-dca-{self._mock_seq:04d}"
            price = self.get_last_price(symbol)
            pos = self._mock_positions.get(symbol)
            if pos and float(pos.get("size") or 0) > 0:
                if str(pos.get("side")) != side_n:
                    if not reduce_only:
                        raise OrderRejected(f"{symbol}에 반대 포지션({pos.get('side')})이 있습니다 — 한 방향만 물타기합니다")
                    # reduce-only 부분 익절 — 보유를 줄이고 남은 수량을 돌려준다
                    old_size = float(pos["size"])
                    cut = min(float(amount), old_size)
                    left = old_size - cut
                    sign = 1.0 if str(pos["side"]) == "Buy" else -1.0
                    pnl = (price - float(pos["entryPrice"])) * cut * sign
                    if left <= 1e-12:
                        self._mock_positions.pop(symbol, None)
                    else:
                        pos["size"] = left
                    self._mock_orders.append({"payload": payload, "result": {"orderId": order_id}})
                    return {"ok": True, "mock": True, "orderId": order_id, "payload": payload,
                            "fill_price": price, "closed": {"qty": cut, "price": price,
                                                             "estimatedPnl": round(pnl, 4),
                                                             "remaining": round(max(0.0, left), 8)}}
                if reduce_only:
                    raise OrderRejected("mock에서는 reduceOnly 추가 주문을 쓰지 않습니다")
                old_size = float(pos["size"])
                new_size = old_size + float(amount)
                pos["entryPrice"] = (float(pos["entryPrice"]) * old_size + price * float(amount)) / new_size \
                    if new_size > 0 else price
                pos["size"] = new_size
            else:
                if reduce_only:
                    raise OrderRejected(f"{symbol}에 청산할 포지션이 없습니다")
                self._mock_positions[symbol] = {
                    "symbol": symbol, "side": side_n, "size": float(amount),
                    "entryPrice": price, "stopLoss": 0.0, "takeProfit": 0.0,
                    "leverage": float(self._mock_leverage.get(symbol) or DEFAULT_LEV),
                    "unrealisedPnl": 0.0,
                }
            self._mock_orders.append({"payload": payload, "result": {"orderId": order_id}})
            return {"ok": True, "mock": True, "orderId": order_id, "payload": payload,
                    "fill_price": price}
        self._request("POST", "/v5/order/create", None, payload)
        # 주문 응답에는 체결가가 없다 — 포지션에서 읽는다(다음 호출). 여기서는 접수 사실만.
        return {"ok": True, "mock": False, "orderId": None, "payload": payload, "fill_price": None}

    def set_trading_stop(self, symbol: str, take_profit=None, stop_loss=None) -> Dict:
        """포지션 전체(tpslMode Full)의 TP/SL을 새 평단에 맞춰 다시 건다. None은 건드리지 않는다."""
        symbol = symbol.upper()
        spec = self.get_instrument(symbol)
        tp_text = None if take_profit is None else fmt_dec(round_to_step(take_profit, spec.tick_size))
        sl_text = None if stop_loss is None else fmt_dec(round_to_step(stop_loss, spec.tick_size))
        if self.is_mock:
            pos = self._mock_positions.get(symbol)
            if not pos:
                raise OrderRejected(f"{symbol}에 포지션이 없어 TP/SL을 걸 수 없습니다")
            if tp_text is not None:
                pos["takeProfit"] = float(tp_text)
            if sl_text is not None:
                pos["stopLoss"] = float(sl_text)
            return {"ok": True, "mock": True, "takeProfit": tp_text, "stopLoss": sl_text}
        body = {"category": "linear", "symbol": symbol, "positionIdx": 0, "tpslMode": "Full",
                "tpTriggerBy": "LastPrice", "slTriggerBy": "LastPrice"}
        if tp_text is not None:
            body["takeProfit"] = tp_text
        if sl_text is not None:
            body["stopLoss"] = sl_text
        if tp_text is None and sl_text is None:
            return {"ok": True, "mock": False, "note": "바꿀 값이 없습니다"}
        try:
            self._request("POST", "/v5/position/trading-stop", None, body)
        except BybitAPIError as exc:
            # 34040: 값이 이미 같다 — 실패가 아니다
            if getattr(exc, "ret_code", None) == 34040:
                return {"ok": True, "mock": False, "takeProfit": tp_text, "stopLoss": sl_text,
                        "note": "이미 같은 값이었습니다"}
            raise
        return {"ok": True, "mock": False, "takeProfit": tp_text, "stopLoss": sl_text}


def build_client(force_mock: bool = False) -> DcaBybitClient:
    """데모 전용 클라이언트. force_mock이면 키가 있어도 mock(네트워크 주문 없음)."""
    if force_mock:
        return DcaBybitClient(api_key="", api_secret="", env="demo")
    return DcaBybitClient(env="demo")


def other_engine_locks(data_dir: Optional[str] = None, stale_sec: int = 2 * 3600) -> List[str]:
    """같은 데모 계좌를 쓰는 다른 엔진의 살아 있는 잠금 파일 — 있으면 동시 실행을 거부한다."""
    base = Path(data_dir or DATA)
    found: List[str] = []
    for pattern in ("testnet_auto_state*.lock", "demo_ai_experiment_state*.lock"):
        for path in sorted(base.glob(pattern)):
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                continue
            if age < stale_sec:
                found.append(str(path))
    return found


# ---------------------------------------------------------------------- AI 1차 진입

ENTRY_WORDS = {
    "LONG": "LONG", "BUY": "LONG", "롱": "LONG", "매수": "LONG", "상승": "LONG",
    "SHORT": "SHORT", "SELL": "SHORT", "숏": "SHORT", "매도": "SHORT", "하락": "SHORT",
    "HOLD": "HOLD", "WAIT": "HOLD", "NONE": "HOLD", "관망": "HOLD", "대기": "HOLD", "보류": "HOLD",
}

ENTRY_SYSTEM = """당신은 바이비트 **데모 계좌**(모의 USDT, 실돈 아님)에서 {symbol} 5분봉으로 **1차 진입 방향**을 고르는 실험용 판단자입니다.
이 매매는 리치식 1~4차 물타기(DCA)입니다. 1차 진입 뒤 불리한 쪽으로 가면 2·3·4차에 증거금을 더 넣고,
익절은 평단 대비 증거금 +{tp_pct:g}%(가격 {tp_move:.2f}%)입니다. 4차까지 가면 손실도 커지므로 **1차 방향이 중요합니다.**

판단 기준
1. 5분봉 EMA20/50 배열과 현재가 위치, 최근 12봉 흐름.
2. 4시간봉 EMA20/50 배열과 기울기 — 큰 추세와 같은 방향이면 가점, 반대면 감점.
3. RSI14·볼린저(20, 2.5σ) 위치 — 과매도/과매수에서의 되돌림인지, 추세 지속인지.
4. ATR% — 너무 낮으면 익절까지 오래 걸리고, 너무 높으면 물타기 선이 쉽게 닿습니다.
5. 확신이 없으면 HOLD. 관망은 실패가 아닙니다.

규칙
- 사유는 한국어 1~2문장, 숫자를 인용해 구체적으로. 뉴스·펀더멘털은 지어내지 마세요.
- 출력은 JSON 객체 하나만: {{"decision":"LONG"|"SHORT"|"HOLD","reason":"한국어 1~2문장"}}"""


def parse_json_object(raw: str) -> Dict:
    import re
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
        obj = json.loads(match.group(0))
    if not isinstance(obj, dict):
        raise ValueError("JSON 객체가 아닙니다")
    return obj


def parse_entry_response(raw: str) -> Dict:
    base = {"decision": "HOLD", "reason": "", "parse_error": None}
    try:
        obj = parse_json_object(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        return {**base, "reason": f"AI 응답을 읽지 못해 관망: {exc}", "parse_error": str(exc)}
    decision = ENTRY_WORDS.get(str(obj.get("decision", obj.get("action", obj.get("verdict")))).strip().upper())
    if decision is None:
        return {**base, "reason": f"AI 판단을 알아보지 못해 관망(decision={obj.get('decision')!r})",
                "parse_error": "알 수 없는 decision"}
    reason = str(obj.get("reason") or obj.get("이유") or "").strip()[:400] or "사유 없음"
    return {"decision": decision, "reason": reason, "parse_error": None}


class EntryDecider:
    """1차 진입 방향 — AI(기본) → 실패하면 기술적 필터. 절대 예외로 루프를 죽이지 않는다."""

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
        except Exception as exc:
            return {"error": str(exc)[:200]}

    def decide(self, context: Dict, cfg: DCAConfig) -> Dict:
        tp_move = float(cfg.tp_pct) / (100.0 * float(cfg.leverage)) * 100.0
        system = ENTRY_SYSTEM.format(symbol=cfg.symbol, tp_pct=float(cfg.tp_pct), tp_move=tp_move)
        try:
            self.calls += 1
            raw = self._ensure().complete(
                system, "아래 5분봉·4시간봉 문맥을 보고 1차 진입 방향을 고르세요. JSON 하나만 출력합니다.\n"
                        + json.dumps(context, ensure_ascii=False, separators=(",", ":")))
            result = parse_entry_response(raw)
        except Exception as exc:              # 키 없음·네트워크·한도
            self.errors += 1
            self.last_error = str(exc)[:300]
            result = {"decision": "HOLD", "reason": f"AI 호출 실패: {self.last_error}",
                      "parse_error": None, "error": self.last_error}
        result["model"] = self.describe().get("model")
        return result


def build_entry_context(bars5: Sequence[Dict], bars4h: Sequence[Dict], price: float) -> Dict:
    """5분봉·4시간봉 문맥 — AI에게 주는 숫자 묶음(가격·지표만)."""
    closes5 = [float(b["c"]) for b in bars5]
    highs5 = [float(b["h"]) for b in bars5]
    lows5 = [float(b["l"]) for b in bars5]
    ema20 = ema_series(closes5, 20)[-1] if len(closes5) >= 20 else None
    ema50 = ema_series(closes5, 50)[-1] if len(closes5) >= 50 else None
    atr = atr_series(list(bars5), 14)[-1] if len(closes5) >= 15 else None
    closes4 = [float(b["c"]) for b in bars4h]
    ema20_4 = ema_series(closes4, 20)[-1] if len(closes4) >= 20 else None
    ema50_4 = ema_series(closes4, 50)[-1] if len(closes4) >= 50 else None
    bands = bollinger(closes5, 20, 2.5)
    recent = bars5[-12:]
    return {
        "종목": None,       # 호출 쪽에서 채운다
        "현재가": _num(price),
        "5분봉": {
            "EMA20": _num(ema20), "EMA50": _num(ema50),
            "RSI14": _num(rsi_wilder(closes5, 14)),
            "볼린저20_2.5": None if not bands else {k: _num(v) for k, v in bands.items() if k != "sd"},
            "ATR14": _num(atr),
            "ATR%": _num((atr / price * 100) if atr and price else None),
            "직전20봉고저": [_num(max(highs5[-20:])), _num(min(lows5[-20:]))],
            "최근12봉": [{"o": _num(b["o"]), "h": _num(b["h"]), "l": _num(b["l"]),
                        "c": _num(b["c"]), "v": _num(b.get("v"))} for b in recent],
        },
        "4시간봉": {
            "EMA20": _num(ema20_4), "EMA50": _num(ema50_4),
            "최근6봉종가": [_num(c) for c in closes4[-6:]],
        },
    }


def tech_direction(bars5: Sequence[Dict], bars4h: Sequence[Dict], price: float) -> Dict:
    """기술적 1차 필터 — 추세 정렬 3표 + 과매도/과매수 반전 2표. |점수| ≥ 2일 때만 방향."""
    closes5 = [float(b["c"]) for b in bars5]
    closes4 = [float(b["c"]) for b in bars4h]
    ema20 = ema_series(closes5, 20)[-1]
    ema50 = ema_series(closes5, 50)[-1]
    ema20_4 = ema_series(closes4, 20)[-1] if len(closes4) >= 20 else None
    ema50_4 = ema_series(closes4, 50)[-1] if len(closes4) >= 50 else None
    rsi = rsi_wilder(closes5, 14)
    bands = bollinger(closes5, 20, 2.5)
    score = 0
    notes = []
    if ema20 is not None and ema50 is not None:
        if ema20 > ema50 * 1.0000001:
            score += 1
            notes.append("5분 EMA20>50")
        elif ema20 < ema50 * 0.9999999:
            score -= 1
            notes.append("5분 EMA20<50")
    if ema50 is not None:
        if price > ema50 * 1.0000001:
            score += 1
        elif price < ema50 * 0.9999999:
            score -= 1
    if ema20_4 is not None and ema50_4 is not None:
        if ema20_4 > ema50_4 * 1.0000001:
            score += 1
            notes.append("4시간 EMA20>50")
        elif ema20_4 < ema50_4 * 0.9999999:
            score -= 1
            notes.append("4시간 EMA20<50")
    if rsi is not None and bands is not None:
        if rsi < 35 and price <= bands["lower"]:
            score += 2
            notes.append(f"RSI {rsi:.1f} 과매도·하단 이탈")
        elif rsi > 65 and price >= bands["upper"]:
            score -= 2
            notes.append(f"RSI {rsi:.1f} 과매수·상단 이탈")
        else:
            notes.append(f"RSI {rsi:.1f}")
    if score >= 2:
        return {"decision": "LONG", "score": score, "reason": "기술 필터 롱: " + " · ".join(notes)}
    if score <= -2:
        return {"decision": "SHORT", "score": score, "reason": "기술 필터 숏: " + " · ".join(notes)}
    return {"decision": "HOLD", "score": score, "reason": "기술 필터 관망: " + " · ".join(notes)}


# ---------------------------------------------------------------------- 엔진

class DemoDcaExperiment:
    """한 번 만들고 tick()을 반복한다. 상태 파일이 있으면 이어서 돈다(재시작 안전)."""

    __test__ = False

    def __init__(self, cfg: Optional[DCAConfig] = None, client: Optional[BybitClient] = None,
                 ai=None, fetch=None, killswitch: Optional[KillSwitch] = None, now=None):
        self.cfg = cfg or DCAConfig()
        validate_settings(self.cfg)
        self.client = client if client is not None else DcaBybitClient(env="demo")
        env = getattr(self.client, "env", None)
        if env is None:
            env = "testnet" if getattr(self.client, "testnet", False) else None
        if env != "demo":
            raise BybitConfigError(
                "이 실험은 바이비트 Demo Trading(api-demo.bybit.com) 전용입니다 — "
                f"지금 클라이언트 환경은 {env!r}입니다. 테스트넷·메인넷으로는 주문하지 않습니다")
        if not getattr(self.client, "sandbox", True):
            raise BybitConfigError("샌드박스 가드가 꺼진 클라이언트입니다 — 데모 전용 실험은 주문하지 않습니다")
        self.env = "demo"
        self.mock = bool(self.client.is_mock)
        self.mode = "mock" if self.mock else "demo"
        self.ai = ai if ai is not None else EntryDecider()
        self.fetch = fetch or self._fetch_default
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._last_price: Optional[float] = None
        self._last_log_at: Optional[float] = None
        self.state_path, self.log_path, self.killswitch_path, self.pause_path = self._resolve_paths()
        self._assert_paths_safe()
        rules = KillSwitchRules(
            daily_loss_pct=float(self.cfg.daily_loss_pct),
            max_consecutive_losses=int(self.cfg.max_consecutive_losses),
            max_daily_trades=int(self.cfg.max_trades_per_day),
            volatility_pause_atr_pct=0.0,          # 급변 일시정지는 이 실험에서 쓰지 않는다
            cooldown_minutes=60,
            min_equity_pct=float(self.cfg.min_equity_pct),
        )
        self.killswitch = killswitch or KillSwitch(rules, state_path=self.killswitch_path)

    # ------------------------------------------------------------------ 경로

    def _resolve_paths(self):
        base = Path(self.cfg.data_dir)
        suffix = ".mock" if self.mock else ""
        state = self.cfg.state_path or str(base / f"demo_dca_state{suffix}.json")
        log = self.cfg.log_path or str(base / f"demo_dca_log{suffix}.jsonl")
        ks = self.cfg.killswitch_path or str(base / f"demo_dca_killswitch{suffix}.json")
        pause = str(Path(state).with_name(Path(state).stem + "_pause.txt"))
        return state, log, ks, pause

    def _assert_paths_safe(self) -> None:
        for path in (self.state_path, self.log_path, self.killswitch_path, self.pause_path):
            if not path:
                continue
            name = Path(path).name
            if any(name.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
                raise ValueError(
                    f"{name}은(는) 기존 모의매매·테스트넷·AI 실험이 쓰는 파일입니다 — "
                    "DCA 실험은 demo_dca_* 파일만 씁니다")
            if not name.startswith(REQUIRED_PREFIX):
                raise ValueError(f"이 실험의 기록 파일 이름은 demo_dca_*여야 합니다: {name}")

    # ------------------------------------------------------------------ 시세

    @property
    def tf_ms(self) -> int:
        return BAR_TF_MS

    def paused(self) -> bool:
        return bool(self.pause_path) and os.path.isfile(self.pause_path)

    def _fetch_default(self) -> Dict:
        order = [self.cfg.source] if self.cfg.source in ("bybit", "okx", "bitget") else ["bybit", "okx", "bitget"]
        errors: List[str] = []
        for name in order:
            try:
                c = load_candles(self.cfg.symbol, BAR_TF, self.cfg.limit, name)
                h4 = load_candles(self.cfg.symbol, "240", self.cfg.four_hour_limit, name)
                candles, candles_4h = c.get("candles") or [], h4.get("candles") or []
                if len(candles) < MIN_CANDLES or len(candles_4h) < MIN_CANDLES_4H:
                    errors.append(f"{name}: 봉 부족({len(candles)}/{len(candles_4h)})")
                    continue
                return {"source": name, "candles": candles, "candles_4h": candles_4h}
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        raise RuntimeError("5분봉·4시간봉을 받지 못했습니다 — " + " / ".join(errors))

    def _price_now(self, bars: Sequence[Dict]) -> float:
        """mock은 봉 종가, 실주문은 거래소 현재가(실패하면 봉 종가)."""
        last = float(bars[-1]["c"]) if bars else None
        if self.mock:
            self._last_price = last
            return float(last)
        try:
            price = float(self.client.get_last_price(self.cfg.symbol))
            self._last_price = price
            return price
        except Exception:
            return float(last or 0.0)

    def _account(self) -> Dict:
        """잔고·사용 증거금·쓸 수 있는 증거금. 읽지 못하면 예외를 그대로 올린다."""
        if hasattr(self.client, "get_balance_detail"):
            bal = self.client.get_balance_detail()
        else:
            bal = self.client.get_balance()
        positions = self.client.get_positions(self.cfg.symbol)
        equity = float(bal.get("equity") or 0.0)
        used = sum(float(p.get("size") or 0) * float(p.get("entryPrice") or 0)
                   / max(1.0, float(p.get("leverage") or self.cfg.leverage))
                   for p in positions if float(p.get("size") or 0) > 0)
        available = bal.get("totalAvailable") or bal.get("available")
        available = float(available) if available is not None else max(0.0, equity - used)
        # 거래소가 말하는 사용 가능액과 '자본 − 사용 증거금' 중 작은 쪽 — 주문 거부(잔고 부족)를 줄인다
        return {"equity": equity, "available": max(0.0, min(available, max(0.0, equity - used))),
                "used": used, "mock": bool(bal.get("mock"))}

    # ------------------------------------------------------------------ 상태

    def _blank_state(self) -> Dict:
        return {
            "version": 1, "mode": self.mode, "kind": "demo_dca_experiment",
            "symbol": self.cfg.symbol, "rules": self._rules_view(),
            "created_at": self.now().isoformat(timespec="seconds"), "updated_at": None,
            "scan_count": 0, "scan_bar": None, "last_heartbeat": None,
            "position": None, "exchange_position": None, "closed": [],
            "equity": None, "stats": {}, "killswitch": {}, "last_scan": {}, "ai_last": None,
            "day": {"date": self._today(), "entries": 0, "closes": 0, "adds": 0},
            "last_exit_bar": None, "last_exit_at": None,
        }

    def _rules_view(self) -> Dict:
        return {
            "version": RULES_VERSION, "source": RULES_SOURCE, "basis": "margin", "avgReset": True,
            "bars": {"tf": BAR_TF, "closedOnly": True},
            "leverage": float(self.cfg.leverage), "firstPct": float(self.cfg.first_pct),
            "tpPct": float(self.cfg.tp_pct), "capPct": float(self.cfg.cap_pct),
            "splitTp": {"on": bool(self.cfg.split_tp), "tp1Pct": float(self.cfg.tp_pct),
                        "tp2Pct": float(self.cfg.tp2_pct), "frac": float(self.cfg.tp1_frac),
                        "bePct": float(self.cfg.be_pct), "basis": "margin",
                        "fallback": "minQty/minNotional 미달이면 +tp1Pct 전량 익절"},
            "posTickSec": int(self.cfg.pos_tick_sec),
            "stages": [s.to_dict() for s in self.cfg.stages],
            "reserveLevPct": self.cfg.reserve_lev_pct,
            "autoSl": {"afterStage": LAST_STAGE, "mode": self.cfg.sl_mode,
                       "liqDistPct": float(self.cfg.sl_dist_pct),
                       "estDistPct": float(self.cfg.sl_est_dist_pct),
                       "slPct": float(self.cfg.sl_pct)},
            "env": self.mode, "sandboxOnly": True, "baseUrl": self.client.base_url,
        }

    def _today(self) -> str:
        return self.now().astimezone(KST).date().isoformat()

    def _roll_day(self, state: Dict) -> None:
        today = self._today()
        day = state.get("day") or {}
        if day.get("date") != today:
            state["day"] = {"date": today, "entries": 0, "closes": 0, "adds": 0}

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
            print(f"[demo-dca] 상태 파일을 읽지 못해 새로 시작합니다: {exc}", flush=True)
            self._recompute(state)
            return state
        state = {**state, **saved}
        state["rules"] = self._rules_view()          # 설정을 바꿔 다시 켜면 새 규칙을 따른다
        if self.mock and state.get("position"):
            # mock 계좌는 프로세스와 함께 사라진다 — 남은 '내 포지션'은 유령이므로 조용히 정리한다
            try:
                live = [p for p in self.client.get_positions(self.cfg.symbol)
                        if float(p.get("size") or 0) > 0]
            except BybitError:
                live = []
            if not live:
                state["position"] = None
                state["note"] = "mock 계정은 프로세스와 함께 사라집니다 — 이전 mock 포지션 기록을 정리했습니다"
        self._recompute(state)
        return state

    def save_state(self, state: Dict) -> None:
        state["updated_at"] = self.now().isoformat(timespec="seconds")
        state["mode"] = self.mode
        state["kind"] = "demo_dca_experiment"
        path = self.state_path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _append_log(self, record: Dict) -> None:
        path = self.log_path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def tail_log(self, limit: int = 20) -> List[Dict]:
        path = self.log_path
        if not path or not os.path.isfile(path):
            return []
        rows: List[Dict] = []
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        return rows[-limit:]

    # ------------------------------------------------------------------ 통계

    def _recompute(self, state: Dict) -> Dict:
        closed = state.get("closed") or []
        known = [t for t in closed if t.get("pnl") is not None]
        wins = [t for t in known if t["pnl"] > 0]
        losses = [t for t in known if t["pnl"] < 0]
        n = len(known)
        margin_pcts = [t.get("margin_pct") for t in known if t.get("margin_pct") is not None]
        state["stats"] = {
            "trades": n, "unknown_pnl": len(closed) - n,
            "wins": len(wins), "losses": len(losses),
            "win_rate_pct": round(len(wins) / n * 100, 1) if n else None,
            "net_pnl": round(sum(t["pnl"] for t in known), 2),
            "avg_margin_pct": round(sum(margin_pcts) / len(margin_pcts), 1) if margin_pcts else None,
            "targets": sum(1 for t in closed if t.get("reason") == "TARGET"),
            "stops": sum(1 for t in closed if t.get("reason") == "STOP"),
            "liquidations": sum(1 for t in closed if t.get("reason") == "LIQUIDATION"),
            "time_exits": sum(1 for t in closed if t.get("reason") == "TIME"),
            "breakeven_exits": sum(1 for t in closed if t.get("reason") == "BREAKEVEN"),
            "partial_trades": sum(1 for t in closed if int(t.get("tp_stage") or 0) >= 1),
            "partial_pnl": round(sum(float(t.get("partial_pnl") or 0.0) for t in closed), 4),
            "other_exits": sum(1 for t in closed if t.get("reason") not in
                               ("TARGET", "STOP", "LIQUIDATION", "TIME", "BREAKEVEN")),
            "stage4": sum(1 for t in closed if int(t.get("stage") or 1) >= 4),
            "avg_stage": round(sum(int(t.get("stage") or 1) for t in closed) / len(closed), 2) if closed else None,
            "estimated_pnl": sum(1 for t in closed if t.get("pnl_source") not in (None, "exchange")),
            "last_trade_at": None,
            "last_trade_kst": None,
        }
        if closed:
            state["stats"]["last_trade_at"] = closed[-1].get("closed_at")
            state["stats"]["last_trade_kst"] = closed[-1].get("closed_kst")
        return state["stats"]

    def _killswitch_summary(self) -> Dict:
        status = self.killswitch.status()
        st = status["state"]
        return {"ok": status["entry"]["ok"], "reason": status["entry"]["reason"],
                "tripped": st.get("tripped"), "tripped_until": st.get("tripped_until"),
                "manual_stop": st.get("manual_stop"), "realized_pnl": st.get("realized_pnl"),
                "trades_today": st.get("trades"), "loss_pct": status.get("loss_pct"),
                "rules": status["rules"]}

    # ------------------------------------------------------------------ 스캔

    def tick(self, candles: Optional[Sequence[Dict]] = None,
             candles_4h: Optional[Sequence[Dict]] = None,
             now_ms: Optional[int] = None) -> Dict:
        """한 번 점검한다. candles를 주면 네트워크 없이 돈다(시험·재생)."""
        now_ms = int(now_ms if now_ms is not None else self.now().timestamp() * 1000)
        source = None
        if candles is None:
            data = self.fetch()
            candles, candles_4h = data["candles"], data.get("candles_4h")
            source = data.get("source")
        bars = closed_bars(candles, self.tf_ms, now_ms)
        bars_4h = closed_bars(candles_4h or [], 4 * 3_600_000, now_ms)
        if not bars:
            raise RuntimeError("확정 봉이 없습니다(캔들 부족 또는 시각 이상)")
        last = bars[-1]
        state = self.load_state()
        self._roll_day(state)
        state["last_context"] = None
        if len(bars) < MIN_CANDLES:
            return self._finish(state, last, "ERROR",
                                [f"확정 봉이 {len(bars)}개뿐입니다 — 5분봉 {MIN_CANDLES}개가 필요합니다"],
                                source=source, bars=len(bars))

        price = self._price_now(bars)
        if self.mock and price:
            self.client.set_mock_price(self.cfg.symbol, price)

        try:
            account = self._account()
        except BybitError as exc:
            hint = ""
            if getattr(exc, "ret_code", None) in (10003, 10004, 33004):
                hint = " — Demo Trading(모의투자) 키인지 확인하세요(테스트넷 키는 데모에서 인증되지 않습니다)"
            return self._finish(state, last, "ERROR", [f"거래소 조회 실패: {exc}{hint}"],
                                source=source, bars=len(bars), price=price)
        equity = float(account.get("equity") or 0.0)
        self.killswitch.start_day(equity)
        if equity <= 0:
            return self._finish(state, last, "ERROR",
                                [f"잔고가 0입니다 — {ENV_LABELS[self.env]} 계정에 모의 USDT를 채우세요"],
                                source=source, bars=len(bars), price=price, account=account)

        try:
            ex = self.client.get_position(self.cfg.symbol) if hasattr(self.client, "get_position") \
                else next((p for p in self.client.get_positions(self.cfg.symbol)
                           if float(p.get("size") or 0) > 0), None)
        except BybitError as exc:
            return self._finish(state, last, "ERROR", [f"포지션 조회 실패: {exc}"],
                                source=source, bars=len(bars), price=price, account=account)
        if ex:
            state["exchange_position"] = {k: _num(v) if k not in ("side", "symbol") else v
                                          for k, v in ex.items()}
        else:
            state["exchange_position"] = None

        local = state.get("position")
        events: List[str] = []
        ai_result = None
        order = None
        action = "WAIT"

        if local and ex:
            action, events, ai_result, order = self._manage(state, local, ex, bars, bars_4h,
                                                            price, account, last, now_ms)
        elif local and not ex:
            action, events = self._position_gone(state, local, bars, price, account, last, now_ms)
        elif ex and not local:
            adopted, events = self._adopt(state, ex, price, account, last)
            if adopted:
                action, events, ai_result, order = self._manage(state, state["position"], ex, bars,
                                                               bars_4h, price, account, last, now_ms)
                events = ["내 1차 주문이 뒤늦게 보였습니다 — 포지션을 이어받았습니다"] + events
            else:
                action = "SKIP"
                events.append(f"거래소에 이미 {_side_ko(ex['side'])} {ex['size']} 포지션이 있습니다 — "
                              "내 기록이 아니므로 건드리지 않습니다(수동 진입·다른 엔진)")
        else:
            action, events, ai_result, order = self._try_entry(state, bars, bars_4h, price,
                                                               account, last, now_ms)
        return self._finish(state, last, action, events, source=source, bars=len(bars),
                            price=price, account=account, ai=ai_result, order=order)

    # ------------------------------------------------------------------ 5초 포지션 감시

    def monitor_position(self, price: Optional[float] = None,
                         now_ms: Optional[int] = None) -> Dict:
        """포지션이 열려 있는 동안의 **5초 감시 한 번** — 시세(티커)만 읽고 바로 판단한다.

        * 캔들도 AI도 부르지 않는다(무료 한도 보호). 물타기 조건(RSI·볼린저)은 마지막 5분봉 스캔이
          남긴 `cond_ok`를 쓰고, **강제선은 가격만으로** 판단한다.
        * 1차 목표 도달 → 절반 reduce-only 익절 + 본절 손절 이동, 2차 목표 도달 → 남은 물량 청산.
        * 본절 아래로 돌아서면(mock) 본절 청산으로 기록한다(실주문은 거래소 SL이 한다).
        * 물타기 차수·시간 청산처럼 봉이 필요한 처리는 `delegate=True`로 돌려주고,
          호출 쪽이 `tick()`을 부른다(가격만으로는 수량·조건 이력을 정확히 못 짚기 때문).
        """
        now_ms = int(now_ms if now_ms is not None else self.now().timestamp() * 1000)
        state = self.load_state()
        pos = state.get("position")
        rec: Dict = {"ts": self.now().isoformat(timespec="seconds"), "kind": "monitor",
                     "mode": self.mode, "env": self.env, "sandbox_only": True,
                     "base_url": self.client.base_url, "action": "IDLE", "price": None,
                     "events": [], "delegate": False, "position": None,
                     "margin_pnl_pct": None, "stage": None, "tp_stage": None}
        if not pos:
            rec["events"] = ["포지션 없음 — 다음 5분봉 마감까지 기다립니다"]
            return rec
        px = price
        if px is None:
            try:
                px = float(self.client.get_last_price(self.cfg.symbol))
            except (BybitError, ValueError, TypeError):
                px = self._last_price
        if not px or float(px) <= 0:
            rec["action"] = "ERROR"
            rec["events"] = ["시세 조회 실패 — 다음 감시에서 다시 봅니다"]
            return rec
        px = float(px)
        self._last_price = px
        if self.mock:
            self.client.set_mock_price(self.cfg.symbol, px)
        rec["price"] = _num(px)
        dir_ = int(pos.get("dir") or _dir_of(pos.get("side")))
        avg = float(pos.get("avg") or 0.0)
        lev = float(pos.get("leverage") or self.cfg.leverage)
        profit = -adverse_pct(dir_, avg, px, lev)
        rec["margin_pnl_pct"] = _num(profit, 1)
        rec["stage"] = int(pos.get("stage") or 1)
        rec["tp_stage"] = int(pos.get("tp_stage") or 0)
        targets = self._targets(pos)
        events: List[str] = []
        action = "WAIT"
        account = {"equity": state.get("equity")}
        if pos.get("force_close"):
            rec["delegate"] = True
            events.append("자동 손절 자리를 지나 정리가 필요합니다 — 전체 점검을 바로 돌립니다")
            action = "DELEGATE"
        elif targets["ladder"] and not targets["done"] and profit >= float(self.cfg.tp_pct) - 1e-9:
            done, note = self._partial_tp(state, pos, px)
            events.append(note)
            action = "TP1" if done else "FALLBACK"
            if not done and self.mock:
                # 폴백 — 거래소 TP가 +tp_pct%에 걸려 있으니 mock은 그 가격에서 전량 청산으로 본다
                if dir_ * (px - float(targets["tp1"])) >= 0:
                    _a, evs = self._close(state, pos, None, account, None, reason="TARGET",
                                          exit_price=px, now_ms=now_ms)
                    events += evs
        elif targets["done"] and profit >= (float(self.cfg.tp2_pct) if targets["ladder"]
                                           else float(self.cfg.tp_pct)) - 1e-9:
            if self.mock:
                action, evs = self._close(state, pos, None, account, None, reason="TARGET",
                                          exit_price=px, now_ms=now_ms)
                events += evs
            else:
                events.append(f"목표(증거금 +{self.cfg.tp2_pct:g}%) 도달 — 거래소 TP가 남은 물량을 익절합니다")
        elif targets["done"] and float(pos.get("be") or 0) > 0 and dir_ * (px - float(pos["be"])) <= 0:
            if self.mock:
                action, evs = self._close(state, pos, None, account, None, reason="BREAKEVEN",
                                          exit_price=float(pos["be"]), now_ms=now_ms)
                events += evs
            elif not pos.get("stop"):
                rec["delegate"] = True
                action = "DELEGATE"
                events.append("본절 손절이 아직 걸리지 않은 상태에서 가격이 본절 아래로 — "
                              "전체 점검으로 바로 보호합니다")
            else:
                events.append(f"가격이 본절 {float(pos['be']):,.2f} 아래로 — 거래소 손절이 나갑니다")
        else:
            next_stage = int(pos.get("stage") or 1) + 1
            rule = stage_rule(self.cfg, next_stage)
            adverse = adverse_pct(dir_, avg, px, lev)
            if rule is not None:
                cond_ok = bool((pos.get("cond_ok") or {}).get(str(next_stage)))
                decision = dca_decision(adverse, rule, cond_ok)
                if decision:
                    rec["delegate"] = True
                    action = "DELEGATE"
                    events.append(
                        f"{next_stage}차 {'강제선' if decision == 'force' else '조건선'} 도달"
                        f"(증거금 {-adverse:+.1f}%) — 5분봉 점검으로 바로 물타기를 실행합니다")
        if not events:
            next_stage = int(pos.get("stage") or 1) + 1
            events.append(f"감시 중 — 다음 {next_stage}차/목표까지 기다립니다")
        rec["action"] = action
        rec["events"] = events
        rec["position"] = self._position_view(state)
        state["last_monitor"] = {k: rec[k] for k in ("ts", "action", "price", "margin_pnl_pct",
                                                    "events", "tp_stage")}
        state["monitor_count"] = int(state.get("monitor_count") or 0) + 1
        self.save_state(state)
        self._log_record(state, rec)
        return rec

    # ------------------------------------------------------------------ 보유 관리

    def _manage(self, state: Dict, pos: Dict, ex: Dict, bars: List[Dict], bars_4h: List[Dict],
                price: float, account: Dict, last: Dict, now_ms: int):
        events: List[str] = []
        action = "WAIT"
        side = str(ex.get("side") or pos.get("side"))
        if side != str(pos.get("side")):
            return ("SKIP", [f"거래소 포지션 방향({side})이 내 기록({pos.get('side')})과 달라 건드리지 않습니다"], None, None)
        # 거래소 값이 정본 — 수량·평단을 맞춘다
        pos["side"] = side
        pos["dir"] = _dir_of(side)
        pos["qty"] = round(float(ex.get("size") or pos.get("qty") or 0.0), 8)
        pos["avg"] = round(float(ex.get("entryPrice") or pos.get("avg") or 0.0), 6)
        lev = float(ex.get("leverage") or pos.get("leverage") or self.cfg.leverage)
        pos["leverage"] = lev
        pos["margin"] = round(pos["qty"] * pos["avg"] / max(1.0, lev), 4)
        pos["liq"] = _num(ex.get("liqPrice")) or pos.get("liq")
        pos["unrealized"] = _num(ex.get("unrealisedPnl"))

        # 1) 4차 뒤 자동 손절 자리를 가격이 이미 지났다면 시장가로 정리한다(조건 주문을 걸 수 없음)
        if pos.get("force_close"):
            reason = str(pos.get("force_close"))
            exit_price = price
            if self.mock and pos.get("force_close_price"):
                exit_price = float(pos["force_close_price"])
            action, events = self._close(state, pos, ex, account, last, reason=reason,
                                         exit_price=exit_price, now_ms=now_ms)
            label = "손절" if reason == "STOP" else "청산"
            return action, events + [f"자동 손절 자리를 이미 지나 {label}했습니다"], None, None
        # 2) mock은 봉 고저로 TP/SL 체결을 흉내 낸다(분할 1차도 같은 봉에서 본다)
        if self.mock:
            hit = self._mock_exit(pos, bars, price)
            if hit:
                if hit.get("partial"):
                    done, note = self._partial_tp(state, pos, hit["price"])
                    events.append(note)
                    if done:
                        return "TP1", events, None, None
                    hit = self._mock_exit(pos, bars, price)      # 폴백 — 같은 봉에서 전량 익절
                    if not hit or hit.get("partial"):
                        return "WAIT", events, None, None
                action, evs = self._close(state, pos, None, account, last,
                                          reason=hit["reason"], exit_price=hit["price"],
                                          now_ms=now_ms)
                return action, events + evs, None, None
        # 2-b) 실주문 — 5초 감시가 놓졌더라도 봉 점검에서 1차 분할/2차 목표를 다시 본다
        if not self.mock:
            targets = self._targets(pos)
            profit = -adverse_pct(pos["dir"], pos["avg"], price, lev)
            if targets["ladder"] and not targets["done"] and profit >= float(self.cfg.tp_pct) - 1e-9:
                done, note = self._partial_tp(state, pos, price)
                events.append(note)
                if done:
                    return "TP1", events, None, None
            elif targets["done"] and profit >= float(self.cfg.tp2_pct) - 1e-9:
                action, evs = self._close(state, pos, ex, account, last, reason="TARGET",
                                          exit_price=price, now_ms=now_ms)
                return action, events + evs, None, None
        # 3) 시간 청산
        if self.cfg.time_exit and self._bars_held(pos, last) >= int(self.cfg.max_hold_bars):
            action, events = self._close(state, pos, ex, account, last, reason="TIME", now_ms=now_ms)
            return action, events, None, None
        # 3-b) 봉마다 한 번 — 거래소 TP/SL이 내 평단과 맞는지 확인하고 어긋났으면 다시 건다
        fix = self._ensure_protection(pos, ex, last)
        if fix:
            events.append(fix)
        # 4) 물타기 차수
        next_stage = int(pos.get("stage") or 1) + 1
        rule = stage_rule(self.cfg, next_stage)
        if rule is None:
            return ("WAIT", events + [f"{int(pos.get('stage'))}차까지 완료 — 익절·자동 손절만 기다립니다"], None, None)
        adverse = adverse_pct(pos["dir"], pos["avg"], price, lev)
        closes = [float(b["c"]) for b in bars]
        cond_ok = cond_met(rule, pos["dir"], closes, price, last["t"], now_ms)
        # 5초 감시가 캔들 없이도 조건을 볼 수 있게 결과를 남겨 둔다(다음 5분봉에서 다시 계산)
        pos["cond_ok"] = {**(pos.get("cond_ok") or {}), str(next_stage): bool(cond_ok)}
        decision = dca_decision(adverse, rule, cond_ok)
        cond_label = ("RSI14" if rule.cond == "rsi" else f"BB{rule.bb_len}/{rule.bb_k:g}σ")
        if decision is None:
            events.append(
                f"{next_stage}차 대기 — 증거금 {-adverse:+.1f}% · 조건 {rule.cond_pct:g}%({cond_label} "
                f"{'참' if cond_ok else '거짓'}) · 강제 {rule.force_pct:g}%")
            return ("WAIT", events, None, None)
        events.append(f"{next_stage}차 {'강제선' if decision == 'force' else '조건 충족'} — "
                      f"증거금 {-adverse:+.1f}% ({cond_label} {'참' if cond_ok else '거짓'})")
        done, note, order = self._add_stage(state, pos, rule, price, account, last, decision)
        if done:
            action = "ADD"
            events.append(note)
        else:
            action = "SKIP"
            events.append(note)
        return (action, events, None, order)

    def _adopt(self, state: Dict, ex: Dict, price: float, account: Dict, last: Dict):
        """1차 주문 직후 조회에서 안 보였던 내 포지션을 다음 점검에서 이어받는다(TP 없는 방치 방지)."""
        cand = state.get("orphan_candidate") or {}
        if not cand:
            return False, []
        bar = int(cand.get("bar") or 0)
        if int(last["t"]) - bar > 3 * self.tf_ms or str(ex.get("side")) != str(cand.get("side")):
            state["orphan_candidate"] = None
            return False, []
        qty = float(ex.get("size") or 0.0)
        avg = float(ex.get("entryPrice") or price)
        lev = float(ex.get("leverage") or self.cfg.leverage)
        capital = float(cand.get("capital") or 0.0) or (float(account.get("available") or 0.0)
                                                        + float(account.get("used") or 0.0))
        state["position"] = {
            "side": str(ex.get("side")), "dir": _dir_of(str(ex.get("side"))), "stage": 1,
            "qty": round(qty, 8), "avg": round(avg, 6), "leverage": lev,
            "margin": round(qty * avg / max(1.0, lev), 4), "capital": round(capital, 4),
            "available_at_open": _num((account or {}).get("available")),
            "tp": None, "stop": None, "liq": _num(ex.get("liqPrice")),
            "entry_bar": bar, "last_fill_bar": bar,
            "opened_at": cand.get("at") or self.now().isoformat(timespec="seconds"),
            "fills": [{"stage": 1, "qty": _num(qty, 8), "price": _num(price), "avgAfter": _num(avg),
                       "margin": None, "at": cand.get("at"), "bar": _iso(bar),
                       "order_id": cand.get("order_id"), "trigger": "entry_adopted"}],
            "ai": state.get("ai_last") or {},
        }
        state["orphan_candidate"] = None
        state["day"]["entries"] = int((state.get("day") or {}).get("entries") or 0) + 1
        notes = self._apply_protection(state["position"], last, price)
        return True, notes

    def _targets(self, pos: Dict) -> Dict:
        """이 포지션의 다음 목표·거래소 TP·본절 손절가 — 분할 여부를 한 곳에서 정한다.

        분할이 켜졌고 1차를 아직 안 했으면 거래소 TP를 **2차(+tp2_pct%)**에 걸어 둔다(1차 부분 익절을
        거래소 TP가 먼저 전량 청산해 버리지 않게). 1차 뒤에는 다음 목표가 2차이고, 소액 폴백이면
        거래소 TP가 **+tp_pct% 전량 익절**이다.
        """
        spec = self.client.get_instrument(self.cfg.symbol)
        dir_ = int(pos.get("dir") or _dir_of(pos.get("side")))
        avg = float(pos.get("avg") or 0.0)
        lev = float(pos.get("leverage") or self.cfg.leverage)
        ladder = bool(self.cfg.split_tp) and not pos.get("split_off")
        done = int(pos.get("tp_stage") or 0) >= 1
        if avg <= 0:
            return {"tp1": 0.0, "tp2": 0.0, "next": 0.0, "exchange": 0.0, "be": 0.0,
                    "ladder": ladder, "done": done}
        t1 = float(tp_price_for(dir_, avg, self.cfg.tp_pct, lev, spec.tick_size))
        t2 = float(tp_price_for(dir_, avg, self.cfg.tp2_pct, lev, spec.tick_size))
        return {"tp1": t1, "tp2": t2,
                "next": t2 if done else t1,
                "exchange": t2 if ladder else t1,
                "be": float(breakeven_stop_price(dir_, avg, self.cfg.be_pct, lev, spec.tick_size)),
                "ladder": ladder, "done": done}

    def _ensure_protection(self, pos: Dict, ex: Dict, last: Dict) -> Optional[str]:
        """한 봉에 한 번 거래소 TP(4차면 SL, 1차 익절 뒤에는 본절 SL)를 내 평단과 맞춘다."""
        if pos.get("tp_check_bar") == int(last["t"]):
            return None
        pos["tp_check_bar"] = int(last["t"])
        spec = self.client.get_instrument(self.cfg.symbol)
        avg = float(pos.get("avg") or 0.0)
        if avg <= 0:
            return None
        targets = self._targets(pos)
        want_tp = float(targets["exchange"])
        pos["tp"] = targets["next"]
        pos["tp1"] = targets["tp1"]
        pos["tp2"] = targets["tp2"]
        pos["be"] = targets["be"]
        have_tp = _f(ex.get("takeProfit"))
        need_tp = abs(have_tp - want_tp) > float(spec.tick_size) * 1.5
        want_sl: Optional[float] = None
        if targets["done"] and targets["be"] > 0:
            want_sl = float(targets["be"])                      # 1차 익절 뒤 본절 보호
        elif int(pos.get("stage") or 1) >= LAST_STAGE and self.cfg.sl_mode != "off":
            want_sl = float(pos.get("stop") or 0.0) or None
        have_sl = _f(ex.get("stopLoss"))
        need_sl = want_sl is not None and abs(have_sl - want_sl) > float(spec.tick_size) * 1.5
        if not (need_tp or need_sl):
            return None
        try:
            self.client.set_trading_stop(self.cfg.symbol, take_profit=want_tp if need_tp else None,
                                         stop_loss=want_sl if need_sl else None)
        except BybitError as exc:
            return f"TP/SL 다시 걸기 실패: {exc}"
        return ("거래소 TP/SL을 새 평단·본절에 맞췄습니다" if targets["done"]
                else "거래소 TP/SL을 새 평단에 맞췄습니다")

    def _partial_tp(self, state: Dict, pos: Dict, price: float) -> Tuple[bool, str]:
        """1차 목표에서 보유의 절반을 reduce-only로 익절하고 남은 물량 손절을 본절로 올린다.

        분할 수량·잔여 수량이 거래소 최소 단위에 못 미치면 주문하지 않고 `split_off`를 남겨
        +tp_pct%에서 전량 익절로 돌아간다(소액 폴백). 실패 사유는 문장으로 돌려준다.
        """
        spec = self.client.get_instrument(self.cfg.symbol)
        dir_ = int(pos.get("dir") or _dir_of(pos.get("side")))
        avg = float(pos.get("avg") or 0.0)
        qty = float(pos.get("qty") or 0.0)
        part, why = split_qty(qty, spec, float(self.cfg.tp1_frac), price)
        if part <= 0:
            pos["split_off"] = why or "분할 불가"
            notes = self._apply_protection(pos, None, price)        # 거래소 TP를 +tp1% 전량으로 되돌린다
            tail = (" · " + " · ".join(notes)) if notes else ""
            return False, (f"분할 익절 건너뜀 — {why} → 평단 대비 증거금 +{self.cfg.tp_pct:g}%에서 "
                           f"전량 익절로 대체합니다{tail}")
        side = "Sell" if dir_ == 1 else "Buy"
        try:
            order = self.client.reduce_only_order(self.cfg.symbol, side, part)
        except (OrderRejected, BybitError) as exc:
            return False, f"1차 분할 익절 실패: {exc} — 다음 감시에서 다시 시도합니다"
        try:
            ex = self.client.get_position(self.cfg.symbol)
        except BybitError:
            ex = None
        rest = float((ex or {}).get("size") or 0.0) or max(0.0, qty - float(part))
        realized = dir_ * (float(price) - avg) * float(part)
        pos["realized"] = round(float(pos.get("realized") or 0.0) + realized, 6)
        pos["margin_total"] = round(float(pos.get("margin_total") or pos.get("margin") or 0.0), 4)
        pos["qty"] = round(rest, 8)
        pos["margin"] = round(rest * avg / max(1.0, float(pos.get("leverage") or 1)), 4)
        pos["tp_stage"] = 1
        pos["partial"] = {"at": self.now().isoformat(timespec="seconds"),
                          "qty": _num(float(part), 8), "price": _num(price),
                          "realized": _num(realized, 4), "order_id": (order or {}).get("orderId")}
        pos["fills"] = list(pos.get("fills") or []) + [{
            "stage": "TP1", "qty": _num(float(part), 8), "price": _num(price),
            "margin": None, "at": self.now().isoformat(timespec="seconds"),
            "bar": None, "order_id": (order or {}).get("orderId"), "trigger": "partial_tp"}]
        notes = self._apply_protection(pos, None, price)            # TP → 2차, SL → 본절
        pos["tp_check_bar"] = None
        note = (f"1차 분할 익절 {float(part):g}개 @ {float(price):,.2f} "
                f"(실현 {realized:+.2f} USDT) · 남은 {rest:g}개 · 본절 손절 "
                f"{float(pos.get('be') or 0):,.2f} · 2차 목표 {float(pos.get('tp2') or 0):,.2f}"
                f"(증거금 +{self.cfg.tp2_pct:g}%)")
        if notes:
            note += " · " + " · ".join(notes)
        return True, note

    def _bars_held(self, pos: Dict, last: Dict) -> int:
        entry_bar = int(pos.get("entry_bar") or 0)
        return max(0, (int(last["t"]) - entry_bar) // self.tf_ms + 1) if entry_bar else 0

    def _mock_exit(self, pos: Dict, bars: List[Dict], price: float) -> Optional[Dict]:
        """mock 청산 — 진입 봉 이후 봉 고저가 1차·2차 목표와 손절을 건드렸는지(없으면 현재가).

        분할이 켜져 있고 1차 전이면 1차 목표에 닿는 순간을 `partial`로 돌려준다(호출 쪽이 절반 익절).
        """
        stop = float(pos.get("stop") or 0.0)
        dir_ = int(pos.get("dir") or _dir_of(pos.get("side")))
        targets = self._targets(pos)
        ladder = bool(targets["ladder"]) and not targets["done"]
        t1 = float(targets["tp1"] or 0.0)
        exit_tp = float(targets["exchange"] or 0.0)
        from_ms = int(pos.get("last_fill_bar") or pos.get("entry_bar") or 0) + self.tf_ms
        seq = [b for b in bars if int(b["t"]) >= from_ms]
        for bar in seq:
            high, low = float(bar["h"]), float(bar["l"])
            if stop > 0:
                if dir_ == 1 and low <= stop:
                    return {"reason": "STOP", "price": stop}
                if dir_ == -1 and high >= stop:
                    return {"reason": "STOP", "price": stop}
            if ladder and t1 > 0:
                if (dir_ == 1 and high >= t1) or (dir_ == -1 and low <= t1):
                    return {"reason": "TP1", "price": t1, "partial": True}
            if exit_tp > 0:
                if dir_ == 1 and high >= exit_tp:
                    return {"reason": "TARGET", "price": exit_tp}
                if dir_ == -1 and low <= exit_tp:
                    return {"reason": "TARGET", "price": exit_tp}
        if stop > 0 and (dir_ * (price - stop) <= 0):
            return {"reason": "STOP", "price": stop}
        if exit_tp > 0 and (dir_ * (price - exit_tp) >= 0):
            return {"reason": "TARGET", "price": exit_tp}
        return None

    def _position_gone(self, state: Dict, pos: Dict, bars: List[Dict], price: float,
                       account: Dict, last: Dict, now_ms: int):
        """내 기록은 있는데 거래소 포지션이 사라졌다 — 청산으로 남긴다."""
        tp = float(pos.get("tp_exchange") or pos.get("tp") or 0.0)   # 거래소에 걸어 둔 목표
        stop = float(pos.get("stop") or 0.0)
        dir_ = int(pos.get("dir") or _dir_of(pos.get("side")))
        reason = "OTHER"
        exit_price = price
        if tp > 0 and dir_ * (price - tp) >= 0:
            reason, exit_price = "TARGET", tp
        elif stop > 0 and dir_ * (price - stop) <= 0:
            reason, exit_price = "STOP", stop
        elif pos.get("liq") and dir_ * (price - float(pos["liq"])) <= 0:
            reason, exit_price = "LIQUIDATION", float(pos["liq"])
        return self._close(state, pos, None, account, last, reason=reason,
                           exit_price=exit_price, now_ms=now_ms)

    def _add_stage(self, state: Dict, pos: Dict, rule: StageRule, price: float,
                   account: Dict, last: Dict, decision: str = "cond"):
        """다음 차수 주문 — 살아 있는지 확인 → 증거금 → 수량 → 시장가 → 새 평단에 TP(4차면 SL)."""
        stage = int(rule.stage)
        dir_ = int(pos["dir"])
        side = "Buy" if dir_ == 1 else "Sell"
        available = float(account.get("available") or 0.0)
        capital = float(pos.get("capital") or 0.0) or available
        used = float(pos.get("qty") or 0.0) * float(pos.get("avg") or 0.0) / max(1.0, float(pos.get("leverage") or 1))
        base = min(available, max(0.0, capital - used))
        reserve = self.cfg.reserve_lev_pct if stage == LAST_STAGE else None
        pct = stage_margin_pct(rule, float(pos.get("leverage") or self.cfg.leverage), reserve)
        margin = margin_amount(base, pct, available)
        if margin <= 0:
            return False, (f"{stage}차 증거금이 0입니다 — 가용 ${available:.2f} · 남은 운용한도 "
                           f"${max(0.0, capital - used):.2f}"), None
        spec = self.client.get_instrument(self.cfg.symbol)
        try:
            qty = qty_for(margin, float(pos.get("leverage") or self.cfg.leverage), price, spec)
        except OrderRejected as exc:
            return False, f"{stage}차 주문 거부: {exc}", None
        try:
            order = self.client.place_dca_order(self.cfg.symbol, side, qty)
        except OrderRejected as exc:
            return False, f"{stage}차 주문 거부: {exc}", None
        except BybitAPIError as exc:
            return False, (f"{stage}차 주문 전송 오류({exc}) — 결과 불확실: 같은 스캔에서 다시 보내지 않고 "
                           "다음 스캔에서 포지션을 확인합니다"), None
        except (ValueError, BybitConfigError) as exc:
            return False, f"{stage}차 주문 규칙 위반: {exc}", None
        # 체결 뒤 거래소 포지션에서 평단을 읽는다
        try:
            ex = self.client.get_position(self.cfg.symbol)
        except BybitError:
            ex = None
        old_qty = float(pos["qty"])
        old_avg = float(pos["avg"])

        def read_fill(row):
            size = float((row or {}).get("size") or 0.0)
            avg_price = float((row or {}).get("entryPrice") or 0.0)
            return size, avg_price

        new_qty, avg = read_fill(ex)
        if not self.mock and new_qty <= old_qty + 1e-12:
            time.sleep(0.4)                      # 시장가 체결 반영을 한 번 더 기다린다
            try:
                ex = self.client.get_position(self.cfg.symbol)
            except BybitError:
                ex = None
            new_qty, avg = read_fill(ex)
        if new_qty <= old_qty + 1e-12:
            # 주문은 접수되었는데 체결이 아직 보이지 않는다 — 차수만 올리고 수량·평단은 다음 점검에 거래소 값으로 맞춘다
            pos["stage"] = stage
            pos["last_add_at"] = self.now().isoformat(timespec="seconds")
            state["day"]["adds"] = int((state.get("day") or {}).get("adds") or 0) + 1
            return True, (f"{stage}차 주문 {(order or {}).get('orderId')} 접수 — 체결이 아직 보이지 않아 "
                          "다음 점검에서 거래소 값으로 맞춥니다"), order
        if avg <= 0:
            avg = (old_avg * old_qty + price * float(qty)) / new_qty if new_qty > 0 else price
        avg = round(avg, 6)
        filled = new_qty - old_qty
        fill_price = float(price)
        if filled > 1e-12:
            inferred = (new_qty * avg - old_qty * old_avg) / filled
            if inferred > 0 and price > 0 and abs(inferred - price) / price <= 0.05:
                fill_price = inferred
        pos["fills"] = list(pos.get("fills") or []) + [{
            "stage": stage, "qty": _num(float(qty), 8), "price": _num(fill_price),
            "avgAfter": _num(avg), "margin": _num(margin),
            "at": self.now().isoformat(timespec="seconds"),
            "bar": _iso(last["t"]), "order_id": (order or {}).get("orderId"),
            "trigger": decision,
        }]
        pos["stage"] = stage
        pos["qty"] = round(new_qty, 8)
        pos["avg"] = avg
        pos["margin"] = round(new_qty * avg / max(1.0, float(pos.get("leverage") or 1)), 4)
        pos["margin_total"] = pos["margin"]        # 손익률(%)은 이번 평단의 총 증거금으로 잰다
        fresh_liq = _num((ex or {}).get("liqPrice"))
        if fresh_liq:
            pos["liq"] = fresh_liq          # 새 평단의 청산가 — 옛값으로 손절 자리를 재지 않는다
        pos["last_fill_bar"] = int(last["t"])
        pos["last_add_at"] = self.now().isoformat(timespec="seconds")
        state["day"]["adds"] = int((state.get("day") or {}).get("adds") or 0) + 1
        note = (f"{stage}차 물타기 체결 {float(qty):g}개 @ {fill_price:,.2f} · 증거금 ${pos['margin']:.2f} "
                f"({pct:g}%) · 새 평단 {avg:,.2f} · 총 {new_qty:g}개")
        notes2 = self._apply_protection(pos, last, price)
        if notes2:
            note += " · " + " · ".join(notes2)
        return True, note, order

    def _apply_protection(self, pos: Dict, last: Optional[Dict], price: Optional[float] = None) -> List[str]:
        """새 평단에 거래소 TP(1차 전이면 2차 목표)·SL(4차 또는 1차 뒤 본절)을 다시 건다."""
        notes: List[str] = []
        spec = self.client.get_instrument(self.cfg.symbol)
        dir_ = int(pos["dir"])
        avg = float(pos["avg"])
        stage = int(pos.get("stage") or 1)
        targets = self._targets(pos)
        tp = targets["exchange"]
        pos["tp"] = targets["next"]
        pos["tp1"] = targets["tp1"]
        pos["tp2"] = targets["tp2"]
        pos["tp_exchange"] = tp
        pos["be"] = targets["be"]
        sl = None
        if targets["done"] and targets["be"] > 0:
            sl = _dec(targets["be"])                                   # 1차 익절 뒤 본절 보호
        elif stage >= LAST_STAGE and self.cfg.sl_mode != "off":
            if self.cfg.sl_mode == "pct":
                sl = pct_stop_price(dir_, avg, self.cfg.sl_pct, float(pos["leverage"]), spec.tick_size)
            else:
                est = liq_of(dir_, avg, float(pos["leverage"]), pos.get("liq"))
                if est:
                    trig = auto_sl_trigger(dir_, avg, est["liq"], est["src"], spec.tick_size,
                                           self.cfg.sl_dist_pct, self.cfg.sl_est_dist_pct)
                    if trig is not None:
                        sl = trig
                        pos["liq"] = _num(est["liq"])
                        pos["stop_src"] = est["src"]
                    else:
                        notes.append("자동 손절 자리가 없어 직접 % 손절로 대신합니다")
                else:
                    notes.append("청산가를 계산할 수 없어 직접 % 손절로 대신합니다")
                if sl is None:
                    sl = pct_stop_price(dir_, avg, self.cfg.sl_pct, float(pos["leverage"]), spec.tick_size)
                    pos["stop_src"] = "pct"
        if sl is not None and price:
            # 지금 가격이 이미 손절 자리를 지났다면 거래소가 조건 주문을 받지 않는다 — 다음 점검에서 정리
            if dir_ * (float(price) - float(sl)) <= 0:
                pos["force_close"] = "STOP"
                pos["force_close_price"] = float(sl)
                notes.append("자동 손절 자리를 이미 지나 다음 점검에서 즉시 청산합니다")
                sl = None
        try:
            self.client.set_trading_stop(self.cfg.symbol, take_profit=tp, stop_loss=sl)
        except BybitError as exc:
            notes.append(f"TP/SL 설정 실패: {exc}")
            return notes
        pos["tp"] = float(targets["next"])          # 화면·감시가 보는 '다음 목표' (거래소 TP와 다를 수 있다)
        if sl is not None:
            pos["stop"] = float(sl)
        return notes

    # ------------------------------------------------------------------ 진입

    def _try_entry(self, state: Dict, bars: List[Dict], bars_4h: List[Dict], price: float,
                   account: Dict, last: Dict, now_ms: int):
        if self.paused():
            return "SKIP", ["수동 정지 중(--resume로 해제)"], None, None
        entry_ok = self.killswitch.allow_entry()
        if not entry_ok.get("ok"):
            return "SKIP", [f"킬스위치: {entry_ok.get('reason')}"], None, None
        entries = int((state.get("day") or {}).get("entries") or 0)
        if entries >= int(self.cfg.max_trades_per_day):
            return "SKIP", [f"오늘 매매 {entries}회 — 하루 최대 {self.cfg.max_trades_per_day}회"], None, None
        last_exit_bar = state.get("last_exit_bar")
        if last_exit_bar is not None and int(last["t"]) <= int(last_exit_bar):
            return "WAIT", ["방금 청산한 봉에서는 새 매매를 시작하지 않습니다"], None, None
        last_exit_at = state.get("last_exit_at")
        if self.cfg.cooldown_min and last_exit_at:
            try:
                elapsed = self.now() - datetime.fromisoformat(last_exit_at)
                if elapsed.total_seconds() < int(self.cfg.cooldown_min) * 60:
                    return "WAIT", [f"청산 뒤 {self.cfg.cooldown_min}분 쉬는 중입니다"], None, None
            except ValueError:
                pass
        # 1차 진입은 5분봉이 마감될 때만 새로 판단한다(AI 호출·과매매 방지)
        if state.get("scan_bar") is not None and int(state["scan_bar"]) == int(last["t"]) \
                and not self.cfg.force_entry:
            return "WAIT", ["이미 이 5분봉에서 진입을 판단했습니다 — 다음 봉 마감까지 기다립니다"], None, None

        direction, ai_result = self._direction(bars, bars_4h, price, state)
        if direction not in ("LONG", "SHORT"):
            return "HOLD", [f"진입 관망: {ai_result.get('reason')}"], ai_result, None
        dir_ = 1 if direction == "LONG" else -1
        side = "Buy" if dir_ == 1 else "Sell"
        available = float(account.get("available") or 0.0)
        capital = available * float(self.cfg.cap_pct) / 100.0
        if capital < MIN_BASE_USD:
            return "SKIP", [f"가용 잔고 ${capital:.2f}가 기준 ${MIN_BASE_USD:g} 미만입니다"], ai_result, None
        margin = margin_amount(capital, float(self.cfg.first_pct), available)
        spec = self.client.get_instrument(self.cfg.symbol)
        try:
            qty = qty_for(margin, float(self.cfg.leverage), price, spec)
        except OrderRejected as exc:
            return "SKIP", [f"1차 주문 거부: {exc}"], ai_result, None
        try:
            self.client.set_leverage(self.cfg.symbol, float(self.cfg.leverage))
        except BybitAPIError as exc:
            return "SKIP", [f"레버리지 {self.cfg.leverage:g}배 설정 실패: {exc} "
                            "(종목 최대 배율·포지션 모드를 확인하세요)"], ai_result, None
        except ValueError as exc:
            return "SKIP", [f"레버리지 설정 거부: {exc}"], ai_result, None
        try:
            order = self.client.place_dca_order(self.cfg.symbol, side, qty)
        except OrderRejected as exc:
            return "SKIP", [f"1차 주문 거부: {exc}"], ai_result, None
        except BybitAPIError as exc:
            return "ERROR", [f"1차 주문 전송 오류({exc}) — 결과 불확실: 다음 스캔에서 포지션을 확인합니다"], ai_result, None
        except (ValueError, BybitConfigError) as exc:
            return "SKIP", [f"1차 주문 규칙 위반: {exc}"], ai_result, None
        try:
            ex = self.client.get_position(self.cfg.symbol)
        except BybitError:
            ex = None
        if not ex and not self.mock:
            time.sleep(0.4)                 # 시장가 체결 반영을 한 번 더 기다린다
            try:
                ex = self.client.get_position(self.cfg.symbol)
            except BybitError:
                ex = None
        if not ex:
            # 주문은 접수되었는데 아직 포지션이 보이지 않는다 — 다음 점검에서 이 주문을 이어받도록 남긴다
            state["orphan_candidate"] = {
                "side": side, "qty": float(qty),
                "bar": int(last["t"]), "at": self.now().isoformat(timespec="seconds"),
                "order_id": (order or {}).get("orderId"), "capital": round(capital, 4),
            }
            return "ERROR", [f"1차 주문({(order or {}).get('orderId')}) 접수 — 체결이 아직 보이지 않아 "
                             "다음 점검에서 포지션을 확인하고, 있으면 이어받아 TP를 겁니다"], ai_result, order
        state["orphan_candidate"] = None
        avg = float(ex.get("entryPrice") or price)
        state["position"] = {
            "side": str(ex.get("side") or side), "dir": _dir_of(str(ex.get("side") or side)),
            "stage": 1, "qty": round(float(ex.get("size") or qty), 8), "avg": round(avg, 6),
            "leverage": float(ex.get("leverage") or self.cfg.leverage),
            "margin": round(float(ex.get("size") or qty) * avg / max(1.0, float(self.cfg.leverage)), 4),
            "capital": round(capital, 4), "available_at_open": round(available, 4),
            "tp": None, "stop": None, "liq": _num(ex.get("liqPrice")),
            "entry_bar": int(last["t"]), "last_fill_bar": int(last["t"]),
            "opened_at": self.now().isoformat(timespec="seconds"),
            "fills": [{"stage": 1, "qty": _num(float(ex.get("size") or qty), 8), "price": _num(price),
                       "avgAfter": _num(avg), "margin": _num(margin),
                       "at": self.now().isoformat(timespec="seconds"), "bar": _iso(last["t"]),
                       "order_id": (order or {}).get("orderId"), "trigger": "entry"}],
            "ai": self._ai_view(ai_result),
        }
        state["day"]["entries"] = entries + 1
        notes = self._apply_protection(state["position"], last, price)
        pos = state["position"]
        target_note = (f"1차 목표 {float(pos.get('tp') or 0):,.2f}"
                       f"(평단 대비 증거금 +{self.cfg.tp_pct:g}%)")
        if self.cfg.split_tp:
            target_note += (f" · 2차 {float(pos.get('tp_exchange') or 0):,.2f}"
                            f"(+{self.cfg.tp2_pct:g}%)에서 전량")
        events = [f"{ENV_LABELS[self.env]} {'롱' if dir_ == 1 else '숏'} 1차 진입 "
                  f"{pos['qty']:g}개 @ {avg:,.2f} · 증거금 ${pos['margin']:.2f}({self.cfg.first_pct:g}%) · "
                  f"레버리지 {pos['leverage']:g}배 · {target_note} · 주문 {(order or {}).get('orderId')}"]
        reason = (ai_result or {}).get("reason")
        if reason:
            events.append(f"판단({(ai_result or {}).get('mode') or 'ai'}): {reason}")
        if notes:
            events.append(" · ".join(notes))
        return "OPEN", events, ai_result, order

    def _direction(self, bars: List[Dict], bars_4h: List[Dict], price: float, state: Dict):
        """방향 결정 — force_entry > ai/auto(AI→기술) > tech."""
        forced = str(self.cfg.force_entry or "").lower()
        if forced in ("long", "short"):
            result = {"decision": "LONG" if forced == "long" else "SHORT",
                      "reason": f"--force-entry {forced} (시험용 고정)"}
            state["ai_last"] = {"at": self.now().isoformat(timespec="seconds"), "mode": "forced", **result}
            return result["decision"], result
        context = None
        if self.cfg.entry_mode in ("auto", "ai"):
            context = build_entry_context(bars, bars_4h, price)
            context["종목"] = self.cfg.symbol
            try:
                result = self.ai.decide(context, self.cfg)
            except Exception as exc:
                result = {"decision": "HOLD", "reason": f"AI 호출 실패: {exc}", "error": str(exc)[:300]}
            result["mode"] = "ai"
            state["ai_last"] = {"at": self.now().isoformat(timespec="seconds"), **self._ai_view(result)}
            if result.get("decision") in ("LONG", "SHORT"):
                return result["decision"], result
            if self.cfg.entry_mode == "ai":
                return "HOLD", result
            # auto: AI가 '부를 수 없었거나 대답을 읽지 못했을 때'만 기술 필터로 넘어간다.
            # AI가 분명히 HOLD라고 했으면(HOLD는 판단이다) 기술 필터로 뒤집지 않는다.
            if not (result.get("error") or result.get("parse_error")):
                return "HOLD", result
            tech = tech_direction(bars, bars_4h, price)
            tech["mode"] = "tech_fallback"
            tech["ai_reason"] = result.get("reason")
            state["ai_last"] = {"at": self.now().isoformat(timespec="seconds"), **self._ai_view(tech)}
            return tech["decision"], tech
        tech = tech_direction(bars, bars_4h, price)
        tech["mode"] = "tech"
        state["ai_last"] = {"at": self.now().isoformat(timespec="seconds"), **self._ai_view(tech)}
        return tech["decision"], tech

    # ------------------------------------------------------------------ 청산

    def _close(self, state: Dict, pos: Dict, ex: Optional[Dict], account: Dict, last: Dict,
               reason: str, exit_price: Optional[float] = None, now_ms: Optional[int] = None):
        if self.mock and exit_price:
            self.client.set_mock_price(self.cfg.symbol, float(exit_price))
        try:
            result = self.client.close_position(self.cfg.symbol)
            sent = f"청산 주문 {result.get('orderId')}"
        except OrderRejected as exc:
            sent = f"청산 주문: {exc}"
        except BybitError as exc:
            return "ERROR", [f"청산 실패({exc}) — 다음 스캔에서 다시 시도합니다"]
        still: List[Dict] = []
        try:
            rows = self.client.get_positions(self.cfg.symbol)
            still = [p for p in rows if float(p.get("size") or 0) > 0]
        except BybitError:
            still = []
        if still:
            return "ERROR", [f"{sent} · 청산 뒤에도 포지션 {still[0]['size']}이 남아 기록을 미룹니다"]
        trade = self._record_close(state, pos, account, reason_hint=reason,
                                   estimate_price=exit_price, last=last)
        state["position"] = None
        detail = (f"{trade['pnl']:+.2f} USDT ({trade['margin_pct']:+.1f}% 증거금)"
                  if trade.get("pnl") is not None and trade.get("margin_pct") is not None
                  else (f"{trade['pnl']:+.2f} USDT" if trade.get("pnl") is not None else "손익 미확인"))
        return "CLOSE", [f"{sent} · {trade['reason_ko']} · {detail} · {int(pos.get('stage') or 1)}차까지"]

    def _record_close(self, state: Dict, pos: Dict, account: Dict, reason_hint: str,
                      estimate_price: Optional[float] = None, last: Optional[Dict] = None) -> Dict:
        rows: List[Dict] = []
        pnl: Optional[float] = None
        exit_price = float(estimate_price) if estimate_price else None
        source: Optional[str] = None
        if not self.mock:
            try:
                rows = self.client.get_closed_pnl(self.cfg.symbol, limit=5)
            except Exception as exc:
                print(f"[demo-dca] 청산 손익 조회 실패(추정으로 계속): {exc}", flush=True)
        pick = None
        for row in rows:
            if str(row.get("side") or "").lower() not in ("buy", "sell"):
                continue
            if float(row.get("qty") or 0) <= 0:
                continue
            row_avg = float(row.get("avgEntryPrice") or 0.0)
            mine = float(pos.get("avg") or 0.0)
            if row_avg and mine and abs(row_avg - mine) / mine > 0.01:
                continue                      # 내 평단과 1% 넘게 다른 옛 청산은 보지 않는다
            if str(row.get("side")).lower() != str(pos.get("side") or "").lower():
                continue
            pick = row
            break
        if pick is not None:
            pnl = _num(pick.get("closedPnl"), 4)
            exit_price = float(pick.get("avgExitPrice") or 0.0) or exit_price
            source = "exchange"
        if pnl is None and exit_price and float(pos.get("avg") or 0) > 0:
            pnl = round(_dir_of(pos.get("side")) * (float(exit_price) - float(pos["avg"]))
                        * float(pos.get("qty") or 0), 4)
            source = source or ("mock" if self.mock else "estimate_price")
        partial_pnl = _num(pos.get("realized"), 4) if pos.get("realized") else None
        if pnl is not None and partial_pnl is not None and source not in (None, "exchange"):
            pnl = round(float(pnl) + float(partial_pnl), 4)      # 1차 부분 익절 몫을 합쳐 거래 손익으로
        dir_ = _dir_of(pos.get("side"))
        margin = float(pos.get("margin_total") or pos.get("margin") or 0.0) or (
            float(pos.get("qty") or 0) * float(pos.get("avg") or 0) / max(1.0, float(pos.get("leverage") or 1)))
        margin_pct = round(pnl / margin * 100, 1) if (pnl is not None and margin > 0) else None
        reason = reason_hint
        if reason_hint not in ("TARGET", "STOP", "LIQUIDATION", "TIME", "BREAKEVEN"):
            tp = float(pos.get("tp_exchange") or pos.get("tp") or 0.0)
            stop = float(pos.get("stop") or 0.0)
            if tp and exit_price and dir_ * (float(exit_price) - tp) >= 0:
                reason = "TARGET"
            elif stop and exit_price and dir_ * (float(exit_price) - stop) <= 0:
                reason = "STOP"
            else:
                reason = "OTHER"
        ko = {"TARGET": "익절", "STOP": "손절", "LIQUIDATION": "강제청산",
              "TIME": "시각 청산", "BREAKEVEN": "본절 청산",
              "OTHER": "수동·외부 청산"}.get(reason, reason)
        trade = {
            "opened_at": pos.get("opened_at"), "closed_at": self.now().isoformat(timespec="seconds"),
            "closed_kst": self.now().astimezone(KST).strftime("%Y-%m-%d %H:%M:%S"),
            "side": pos.get("side"), "dir": dir_, "stage": int(pos.get("stage") or 1),
            "avg": _num(pos.get("avg")), "exit_price": _num(exit_price), "qty": _num(pos.get("qty"), 8),
            "margin": _num(margin), "pnl": _num(pnl, 4), "pnl_source": source, "margin_pct": margin_pct,
            "reason": reason, "reason_ko": ko,
            "tp_stage": int(pos.get("tp_stage") or 0), "partial_pnl": partial_pnl,
            "realized": _num(pos.get("realized"), 4), "be": _num(pos.get("be")),
            "split_off": pos.get("split_off"),
            "bars_held": self._bars_held(pos, last) if last else None,
            "fills": pos.get("fills") or [], "ai": pos.get("ai") or {},
        }
        state["closed"] = list(state.get("closed") or []) + [trade]
        if last:
            state["last_exit_bar"] = int(last["t"])
        state["last_exit_at"] = self.now().isoformat(timespec="seconds")
        state["day"]["closes"] = int((state.get("day") or {}).get("closes") or 0) + 1
        equity = account.get("equity")
        try:
            self.killswitch.record_trade(float(pnl or 0.0), equity=float(equity) if equity else None)
        except Exception as exc:
            print(f"[demo-dca] 킬스위치 기록 실패(계속): {exc}", flush=True)
        self._recompute(state)
        return trade

    # ------------------------------------------------------------------ 마무리

    @staticmethod
    def _ai_view(result: Optional[Dict]) -> Dict:
        if not result:
            return {}
        keys = ("decision", "reason", "mode", "model", "score", "parse_error", "error", "ai_reason")
        return {k: result.get(k) for k in keys if result.get(k) is not None}

    def _position_view(self, state: Dict) -> Optional[Dict]:
        pos = state.get("position")
        if not pos:
            return None
        price = self._last_price
        lev = float(pos.get("leverage") or self.cfg.leverage)
        avg = float(pos.get("avg") or 0)
        pct = -adverse_pct(int(pos.get("dir") or 1), avg, float(price), lev) if price else None
        if pct is not None and abs(pct) < 0.05:
            pct = 0.0
        return {"side": pos.get("side"), "stage": int(pos.get("stage") or 1),
                "qty": _num(pos.get("qty"), 8), "avg": _num(avg),
                "leverage": lev, "margin": _num(pos.get("margin")),
                "tp": _num(pos.get("tp")), "tp2": _num(pos.get("tp2")), "stop": _num(pos.get("stop")),
                "liq": _num(pos.get("liq")), "margin_pnl_pct": _num(pct, 1),
                "tp_stage": int(pos.get("tp_stage") or 0), "be": _num(pos.get("be")),
                "split_off": pos.get("split_off"), "realized": _num(pos.get("realized")),
                "opened_at": pos.get("opened_at")}

    def _finish(self, state: Dict, last: Dict, action: str, events: List[str], *,
                source=None, bars: Optional[int] = None, price: Optional[float] = None,
                account: Optional[Dict] = None, ai=None, order=None) -> Dict:
        self._roll_day(state)
        state["scan_bar"] = int(last["t"])
        state["scan_count"] = int(state.get("scan_count") or 0) + 1
        if price:
            self._last_price = float(price)
        if action == "CLOSE":
            state["last_exit_bar"] = int(last["t"])
        if account and account.get("equity") is not None:
            state["equity"] = round(float(account["equity"]), 2)
        state["killswitch"] = self._killswitch_summary()
        self._recompute(state)
        record = {
            "ts": self.now().isoformat(timespec="seconds"),
            "mode": self.mode, "env": self.env, "sandbox_only": True, "base_url": self.client.base_url,
            "bar": _iso(last["t"]), "bar_kst": _kst(last["t"]), "price": _num(price),
            "bars": bars, "source": source, "equity": state.get("equity"),
            "available": _num((account or {}).get("available")),
            "action": action, "events": events, "ai": self._ai_view(ai),
            "position": self._position_view(state),
            "order": None if not order else {"orderId": order.get("orderId"),
                                             "mock": order.get("mock"),
                                             "payload": order.get("payload")},
            "stats": state["stats"], "day": dict(state.get("day") or {}),
            "killswitch": {"ok": state["killswitch"].get("ok"),
                           "reason": state["killswitch"].get("reason"),
                           "trades_today": state["killswitch"].get("trades_today"),
                           "realized_pnl": state["killswitch"].get("realized_pnl")},
        }
        state["last_scan"] = {k: record[k] for k in ("ts", "bar", "action", "events", "price")}
        self.save_state(state)
        self._log_record(state, record)
        return record

    def _log_record(self, state: Dict, record: Dict) -> None:
        """행동은 항상, 대기(WAIT/HOLD)는 10분에 한 번만 남긴다(로그가 하루 3천 줄로 불어나지 않게)."""
        action = record.get("action")
        now = self.now().timestamp()
        if action in ("OPEN", "ADD", "CLOSE", "SKIP", "ERROR", "TP1", "FALLBACK", "BREAKEVEN",
                      "DELEGATE"):
            self._append_log(record)
            state["last_heartbeat"] = record["ts"]
            self.save_state(state)
            return
        last = state.get("last_heartbeat")
        due = True
        if last:
            try:
                due = (now - datetime.fromisoformat(last).timestamp()) >= 600
            except ValueError:
                due = True
        if due:
            self._append_log(record)
            state["last_heartbeat"] = record["ts"]
            self.save_state(state)

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
               "symbol": self.cfg.symbol, "client": self.client.mode(),
               "rules": state["rules"], "state": state, "killswitch": self.killswitch.status(),
               "settings": self.cfg.to_dict(), "log": self.tail_log(),
               "ai": self.ai.describe() if hasattr(self.ai, "describe") else None}
        try:
            out["account"] = (self.client.get_balance_detail()
                              if hasattr(self.client, "get_balance_detail") else self.client.get_balance())
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

def format_live_line(*, now_ms: int, symbol: str, price: Optional[float], state: Dict,
                     due_ms: Optional[int] = None, note: str = "") -> str:
    when = datetime.fromtimestamp(int(now_ms) / 1000, tz=KST).strftime("%H:%M:%S")
    parts = [f"[demo-dca] {when} KST"]
    parts.append(f"{symbol} " + (f"{float(price):,.2f}" if price else "시세 조회 실패"))
    pos = state.get("position")
    if pos:
        side = _side_ko(pos.get("side") or "")
        lev = float(pos.get("leverage") or DEFAULT_LEV)
        avg = float(pos.get("avg") or 0.0)
        pct = -adverse_pct(int(pos.get("dir") or 1), avg, float(price or avg), lev)
        if abs(pct) < 0.05:
            pct = 0.0
        parts.append(f"{side} {int(pos.get('stage') or 1)}차 {pos.get('qty')} @ {avg:,.2f}")
        parts.append(f"증거금 {pct:+.1f}%")
        if int(pos.get("tp_stage") or 0) >= 1:
            parts.append("1차 익절 완료")
        next_stage = int(pos.get("stage") or 1) + 1
        rule = None
        for s in (state.get("rules") or {}).get("stages") or []:
            if int(s.get("stage") or 0) == next_stage:
                rule = s
        if rule:
            parts.append(f"다음 {next_stage}차 {rule['cond_pct']:g}%/{rule['force_pct']:g}%")
        if pos.get("tp"):
            parts.append(f"TP {float(pos['tp']):,.2f}")
        if pos.get("stop"):
            parts.append(f"SL {float(pos['stop']):,.2f}")
        if not pos.get("stop") and int(pos.get("tp_stage") or 0) == 0 and pos.get("be"):
            parts.append(f"본절 대기 {float(pos['be']):,.2f}")
    else:
        parts.append("포지션 없음")
    ai_last = state.get("ai_last") or {}
    if ai_last.get("decision"):
        parts.append(f"최근 판단 {ai_last.get('decision')}")
    if due_ms:
        remain = max(0, int(due_ms) - int(now_ms)) // 1000
        at = datetime.fromtimestamp(int(due_ms) / 1000, tz=KST).strftime("%H:%M:%S")
        parts.append(f"다음 진입 판단 {at} ({remain // 60}분 {remain % 60:02d}초)")
    if note:
        parts.append(note)
    return " · ".join(parts)


def print_banner(engine: DemoDcaExperiment) -> None:
    print("[demo-dca] EXPERIMENTAL RICH-STYLE 1~4 DCA MODE (Bybit Demo ONLY · NO REAL MONEY)", flush=True)
    if engine.mock:
        print("[demo-dca] 모드: mock — 키가 없거나 --mock입니다. 실제 주문은 나가지 않습니다.", flush=True)
    else:
        print("[demo-dca] 모드: 데모 실주문 — api-demo.bybit.com · 모의 USDT만 움직입니다.", flush=True)
    cfg = engine.cfg
    print(f"[demo-dca] 1차: 가용 잔고 {cfg.first_pct:g}% 증거금 · 레버리지 {cfg.leverage:g}배 · "
          f"1차 익절 평단 대비 증거금 +{cfg.tp_pct:g}% (가격 {cfg.tp_pct / cfg.leverage:.2f}%)", flush=True)
    if cfg.split_tp:
        print(f"[demo-dca] 분할 익절: +{cfg.tp_pct:g}%에서 {cfg.tp1_frac * 100:g}% reduce-only 익절 + "
              f"본절 손절(+{cfg.be_pct:g}%) · 2차 +{cfg.tp2_pct:g}% 전량 · "
              f"소액 최소수량 미달이면 +{cfg.tp_pct:g}% 전량 폴백", flush=True)
    else:
        print(f"[demo-dca] 분할 익절 꺼짐(--no-split-tp) — +{cfg.tp_pct:g}%에서 전량 익절(원본 방식)",
              flush=True)
    for s in cfg.stages:
        cond = f"RSI{s.rsi_len} 롱<{s.long_below:g}/숏>{s.short_above:g}" if s.cond == "rsi" \
            else f"BB{s.bb_len}/{s.bb_k:g}σ 이탈"
        print(f"[demo-dca] {s.stage}차: 증거금 -{s.cond_pct:g}%({cond}) / 강제 -{s.force_pct:g}% → "
              f"가용 {s.avail_pct:g}% 추가", flush=True)
    sl = "끔" if cfg.sl_mode == "off" else (
        f"4차 뒤 평단→청산가 {cfg.sl_dist_pct:g}% (추정 {cfg.sl_est_dist_pct:g}%)"
        if cfg.sl_mode == "auto" else f"4차 뒤 증거금 -{cfg.sl_pct:g}%")
    print(f"[demo-dca] 자동 손절: {sl} · 시간 청산 {cfg.max_hold_bars}봉(5분) · "
          f"하루 {cfg.max_trades_per_day}회 · 일일 손실 {cfg.daily_loss_pct:g}% 킬스위치", flush=True)
    print(f"[demo-dca] 5분 완성봉 기준 · 포지션 없으면 {cfg.scan_sec}초 · 있으면 {cfg.pos_tick_sec}초마다 "
          f"현재가 확인(시세만) · 1분마다 상태 한 줄", flush=True)
    print(f"[demo-dca] 1차 방향: {cfg.entry_mode} "
          f"({'AI 실패 시 기술 필터' if cfg.entry_mode == 'auto' else cfg.entry_mode}) · "
          f"AI {engine.ai.describe() if hasattr(engine.ai, 'describe') else '준비됨'}", flush=True)
    print(f"[demo-dca] 기록: {engine.state_path} (모의매매·오토 모드·AI 실험과 분리)", flush=True)
    print("[demo-dca] 같은 데모 계좌에서 run_testnet_auto.bat·run_demo_ai_experiment.bat을 동시에 켜지 마세요.",
          flush=True)


def print_record(rec: Dict) -> None:
    st = rec["stats"]
    line = f"[demo-dca] {rec['ts']} · 봉 {rec['bar_kst']} KST · {rec['action']}"
    if rec.get("price"):
        line += f" · {float(rec['price']):,.2f}"
    if rec.get("events"):
        line += " · " + " | ".join(rec["events"])
    print(line, flush=True)
    pos = rec.get("position")
    if pos:
        print(f"        보유 {_side_ko(pos['side'])} {pos['stage']}차 {pos['qty']} @ {float(pos['avg']):,.2f} · "
              f"증거금 {pos['margin']} USDT({pos['margin_pnl_pct']:+.1f}%) · "
              f"TP {pos['tp']} · SL {pos['stop'] if pos['stop'] else '-'}", flush=True)
    win = st.get("win_rate_pct")
    print(f"        매매 {st['trades']}건 · 승률 {win if win is not None else '-'}% · "
          f"순손익 {st['net_pnl']:+.2f} USDT · 평균 증거금 {st['avg_margin_pct'] if st['avg_margin_pct'] is not None else '-'}% · "
          f"자본 {rec.get('equity')} USDT", flush=True)


def print_monitor(rec: Dict) -> None:
    """5초 감시의 결과 중 사람이 볼 만한 것(분할 익절·폴백·본절·청산)만 한 줄로."""
    when = datetime.fromtimestamp(
        datetime.fromisoformat(rec["ts"]).timestamp(), tz=KST).strftime("%H:%M:%S")
    line = f"[demo-dca] {when} KST · 5초 감시 · {rec.get('action')}"
    if rec.get("price"):
        line += f" · {float(rec['price']):,.2f}"
    if rec.get("margin_pnl_pct") is not None:
        line += f" · 증거금 {float(rec['margin_pnl_pct']):+.1f}%"
    if rec.get("events"):
        line += " · " + " | ".join(rec["events"])
    pos = rec.get("position")
    if pos:
        line += (f"  → 보유 {pos['qty']} @ {float(pos['avg']):,.2f}"
                 f" · TP {pos['tp']} · SL {pos['stop'] if pos.get('stop') else '-'}")
    print(line, flush=True)


def print_status(engine: DemoDcaExperiment) -> None:
    status = engine.status()
    acct = status.get("account") or {}
    print(f"[demo-dca] 모드 {status['mode']} · {status['symbol']} · 주소 {status['client']['baseUrl']}",
          flush=True)
    rules = status["rules"]
    print(f"        1차 {rules['firstPct']:g}% · {rules['leverage']:g}배 · 익절 증거금 +{rules['tpPct']:g}% · "
          f"자동 손절 {rules['autoSl']['mode']} · 진입 {('정지(--resume)' if engine.paused() else '허용')}",
          flush=True)
    split = rules.get("splitTp") or {}
    if split.get("on"):
        print(f"        분할 익절 +{split['tp1Pct']:g}%에서 {split['frac'] * 100:g}% reduce-only + 본절 "
              f"+{split['bePct']:g}% · +{split['tp2Pct']:g}% 전량 · 포지션 {rules.get('posTickSec')}초 감시",
              flush=True)
    else:
        print("        분할 익절 꺼짐 — 1차 목표에서 전량 익절", flush=True)
    if status.get("account_error"):
        print(f"        계좌 조회 실패: {status['account_error']}", flush=True)
    else:
        print(f"        잔고 {acct.get('equity')} USDT · 사용 가능 {acct.get('available')} USDT · "
              f"열린 포지션 {len(status.get('positions') or [])}개", flush=True)
    state = status["state"]
    pos = state.get("position")
    if pos:
        last = state.get("last_scan") or {}
        price = last.get("price")
        pct = adverse_pct(int(pos.get("dir") or 1), float(pos.get("avg") or 0),
                          float(price or pos.get("avg") or 0), float(pos.get("leverage") or DEFAULT_LEV))
        print(f"        내 포지션 {_side_ko(pos['side'])} {pos['stage']}차 {pos['qty']} @ "
              f"{float(pos['avg']):,.2f} · 증거금 ${pos['margin']} ({-pct:+.1f}%) · "
              f"TP {pos['tp']} · SL {pos['stop'] if pos.get('stop') else '-'} · "
              f"청산가 추정 {pos.get('liq') or '-'}", flush=True)
    ks = status["killswitch"]
    print(f"        킬스위치 진입 {'가능' if ks['entry']['ok'] else '금지'} ({ks['entry']['reason']}) · "
          f"오늘 매매 {ks['state'].get('trades')}건 · 실현손익 {ks['state'].get('realized_pnl')} · "
          f"오늘 시작 {(state.get('day') or {}).get('entries')}/{engine.cfg.max_trades_per_day}",
          flush=True)
    ai_last = state.get("ai_last") or {}
    if ai_last:
        print(f"        최근 판단 {ai_last.get('decision')} ({ai_last.get('mode')}) · {ai_last.get('reason')}",
              flush=True)
    st = state["stats"]
    win = st.get("win_rate_pct")
    print(f"        누적 {st.get('trades')}건 · 승률 {win if win is not None else '-'}% · "
          f"순손익 {st.get('net_pnl')} USDT · 익절 {st.get('targets')} · 손절 {st.get('stops')} · "
          f"4차까지 {st.get('stage4')}건 · 평균 차수 {st.get('avg_stage') if st.get('avg_stage') is not None else '-'}",
          flush=True)
    if status.get("log"):
        print("        최근 기록:", flush=True)
        for row in status["log"][-5:]:
            print(f"         · {row.get('bar_kst')} {row.get('action')} "
                  f"{' | '.join(row.get('events') or [])[:120]}", flush=True)


# ---------------------------------------------------------------------- 루프

def run_live_loop(engine: DemoDcaExperiment, *, quiet: bool = False,
                  scan_sec: int = DEFAULT_SCAN_SEC, status_sec: int = DEFAULT_STATUS_SEC,
                  pos_tick_sec: int = DEFAULT_POS_TICK_SEC,
                  max_rounds: Optional[int] = None, clock=None, sleep=None, out=None) -> int:
    """포지션이 없으면 scan_sec마다 점검, 있으면 pos_tick_sec(기본 5초)마다 시세만 보고 판단한다.

    대기 중 status_sec마다 상태 한 줄. 5초 감시가 "전체 점검"을 요구하면(물타기 차수·강제 정리)
    바로 tick()을 불러 처리한다. Ctrl+C는 즉시 받는다.
    """
    out = out or (lambda line: print(line, flush=True))
    clock = clock or (lambda: int(engine.now().timestamp() * 1000))
    sleep = sleep or time.sleep
    rounds = 0
    next_status = clock() + max(1, int(status_sec)) * 1000
    try:
        while True:
            try:
                state = engine.load_state()
            except Exception:
                state = {}
            has_pos = bool(state.get("position"))
            rec = None
            mon = None
            try:
                if has_pos:
                    mon = engine.monitor_position()
                    if mon.get("delegate"):
                        rec = engine.tick()
                else:
                    rec = engine.tick()
            except Exception as exc:
                if not quiet:
                    out(f"[demo-dca] 점검 실패: {exc} — {RETRY_SEC}초 뒤 다시")
            rounds += 1
            if not quiet:
                if rec is not None and rec.get("action") not in ("WAIT", "HOLD"):
                    print_record(rec)
                elif mon is not None and mon.get("action") in ("TP1", "FALLBACK", "CLOSE",
                                                               "BREAKEVEN"):
                    print_monitor(mon)
            if max_rounds is not None and rounds >= int(max_rounds):
                return 0
            try:
                state = engine.load_state()
            except Exception:
                state = {}
            interval = max(1, int(pos_tick_sec)) if state.get("position") else max(5, int(scan_sec))
            due = clock() + interval * 1000
            while True:
                now = clock()
                if now >= due:
                    break
                sleep(min(1.0, max(0.05, (due - now) / 1000.0)))
                now = clock()
                if quiet or now < next_status:
                    continue
                out(format_live_line(now_ms=now, symbol=engine.cfg.symbol,
                                     price=engine._last_price, state=state,
                                     due_ms=next_bar_close_ms(now, engine.tf_ms),
                                     note="Ctrl+C로 멈춤"))
                while next_status <= now:
                    next_status += max(1, int(status_sec)) * 1000
    except KeyboardInterrupt:
        if not quiet:
            out("\n[demo-dca] 안전 종료. 열린 포지션이 있으면 거래소의 익절·손절이 계속 걸려 있습니다.")
        return 0


# ---------------------------------------------------------------------- CLI

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="데모 전용 리치식 1~4차 물타기(DCA) 실험(바이비트 Demo 전용)")
    ap.add_argument("--once", action="store_true", help="한 번만 점검(스케줄러·첫 점검용)")
    ap.add_argument("--loop", action="store_true", help="계속 점검(30초 간격, Ctrl+C 종료)")
    ap.add_argument("--status", action="store_true", help="계좌·포지션·차수·성적")
    ap.add_argument("--mock", action="store_true", help="키가 있어도 mock으로만 돈다(주문 없음)")
    ap.add_argument("--symbol", default=None, help="ETHUSDT(기본)·SOLUSDT·BTCUSDT (env DCA_SYMBOL)")
    ap.add_argument("--leverage", type=float, default=None,
                    help="레버리지 1~50배(기본 50, env DCA_LEVERAGE)")
    ap.add_argument("--first-pct", type=float, default=None,
                    help="1차 증거금 비중 %%(기본 13, env DCA_FIRST_PCT)")
    ap.add_argument("--tp-pct", type=float, default=None,
                    help="1차 익절 증거금 %% (기본 10, env DCA_TP_PCT)")
    ap.add_argument("--split-tp", dest="split_tp", action="store_true", default=None,
                    help="2단계 분할 익절 + 본절 보호(기본 켜짐)")
    ap.add_argument("--no-split-tp", dest="split_tp", action="store_false",
                    help="분할 없이 +tp-pct에서 전량 익절(원본 방식)")
    ap.add_argument("--tp2-pct", type=float, default=None,
                    help="2차 익절 증거금 %% (기본 20, env DCA_TP2_PCT)")
    ap.add_argument("--tp1-frac", type=float, default=None,
                    help="1차에서 줄이는 비율 (기본 0.5)")
    ap.add_argument("--be-pct", type=float, default=None,
                    help="1차 뒤 본절 손절 = 평단 + 증거금 %% (기본 1.5)")
    ap.add_argument("--pos-tick-sec", type=int, default=None,
                    help="포지션 보유 중 현재가 확인 간격(초, 기본 5)")
    ap.add_argument("--cap-pct", type=float, default=100.0, help="운용 상한(가용 잔고 %%, 기본 100)")
    ap.add_argument("--entry-mode", default="auto", choices=["auto", "ai", "tech"],
                    help="1차 방향: auto(AI→기술)·ai·tech")
    ap.add_argument("--force-entry", default="", choices=["", "long", "short"],
                    help="시험용 — 1차 방향을 고정한다(드라이런에서 1~4차를 볼 때)")
    ap.add_argument("--sl-mode", default="auto", choices=["auto", "pct", "off"],
                    help="4차 뒤 손절: auto(청산가 90%%)·pct(증거금 %%)·off")
    ap.add_argument("--sl-pct", type=float, default=50.0, help="sl-mode=pct일 때 증거금 손실 %%(기본 50)")
    ap.add_argument("--sl-dist-pct", type=float, default=90.0, help="자동 손절 거리 %% (거래소 청산가)")
    ap.add_argument("--sl-est-dist-pct", type=float, default=90.0,
                    help="자동 손절 거리 %% (추정 청산가 — 원본 기본 80)")
    ap.add_argument("--reserve-lev-pct", type=float, default=None,
                    help="원본 그대로: 4차에 배율×이 비율만큼 증거금을 남긴다(예: 0.48)")
    ap.add_argument("--source", default="auto", choices=["auto", "bybit", "okx", "bitget"],
                    help="5분봉 출처(bybit이 지역 차단이면 okx로 넘어간다)")
    ap.add_argument("--max-trades", type=int, default=6, help="하루 매매(1~4차 한 묶음) 수")
    ap.add_argument("--daily-loss-pct", type=float, default=25.0, help="일일 손실 킬스위치 %%")
    ap.add_argument("--min-equity-pct", type=float, default=50.0, help="자본 하한 %% — 아래면 사람 확인 전 중단")
    ap.add_argument("--max-hold-bars", type=int, default=288, help="시간 청산까지 보유 5분봉")
    ap.add_argument("--no-time-exit", action="store_true", help="시간 청산을 쓰지 않는다")
    ap.add_argument("--cooldown-min", type=int, default=0, help="청산 뒤 새 매매까지 쉬는 분")
    ap.add_argument("--scan-sec", type=int, default=DEFAULT_SCAN_SEC, help="물타기 선 점검 간격(초)")
    ap.add_argument("--status-sec", type=int, default=DEFAULT_STATUS_SEC, help="상태 한 줄 간격(초)")
    ap.add_argument("--model", default="", help="AI 모델 덮어쓰기(기본은 .env)")
    ap.add_argument("--data-dir", default=None, help="기록 폴더(기본 data)")
    ap.add_argument("--state", default=None, help="상태 파일 경로(시험용)")
    ap.add_argument("--log", default=None, help="로그 파일 경로(시험용)")
    ap.add_argument("--killswitch", default=None, help="킬스위치 파일 경로(시험용)")
    ap.add_argument("--reset", action="store_true", help="로컬 기록 초기화(열린 포지션 있으면 거부)")
    ap.add_argument("--stop", action="store_true", help="신규 매매 수동 정지")
    ap.add_argument("--resume", action="store_true", help="수동 정지 해제")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    load_env_file()

    def pick(value_arg, env_name, default):
        if value_arg is not None:
            return value_arg
        raw = os.getenv(env_name, "").strip()
        if not raw:
            return default
        try:
            return type(default)(raw)
        except (TypeError, ValueError):
            print(f"[demo-dca] {env_name} 값을 읽을 수 없습니다: {raw!r} — 기본값 {default}을 씁니다", flush=True)
            return default

    def flag(value_arg, env_name, default: bool) -> bool:
        """켜고 끄는 스위치 — --flag/--no-flag > env > 기본값."""
        if value_arg is not None:
            return bool(value_arg)
        return _env_flag(env_name, default)

    cfg = DCAConfig(
        symbol=str(pick(args.symbol, "DCA_SYMBOL", "ETHUSDT")).upper(),
        leverage=pick(args.leverage, "DCA_LEVERAGE", DEFAULT_LEV),
        first_pct=pick(args.first_pct, "DCA_FIRST_PCT", 13.0),
        tp_pct=pick(args.tp_pct, "DCA_TP_PCT", 10.0),
        split_tp=flag(args.split_tp, "DCA_SPLIT_TP", True),
        tp2_pct=pick(args.tp2_pct, "DCA_TP2_PCT", DEFAULT_TP2_PCT),
        tp1_frac=pick(args.tp1_frac, "DCA_TP1_FRAC", DEFAULT_TP1_FRAC),
        be_pct=pick(args.be_pct, "DCA_BE_PCT", DEFAULT_BE_PCT),
        pos_tick_sec=max(1, int(pick(args.pos_tick_sec, "DCA_POS_TICK_SEC", DEFAULT_POS_TICK_SEC))),
        cap_pct=args.cap_pct, source=args.source,
        entry_mode=args.entry_mode, force_entry=args.force_entry,
        reserve_lev_pct=args.reserve_lev_pct, sl_mode=args.sl_mode, sl_pct=args.sl_pct,
        sl_dist_pct=args.sl_dist_pct, sl_est_dist_pct=args.sl_est_dist_pct,
        max_trades_per_day=args.max_trades, daily_loss_pct=args.daily_loss_pct,
        min_equity_pct=args.min_equity_pct, max_hold_bars=args.max_hold_bars,
        time_exit=not args.no_time_exit, cooldown_min=max(0, int(args.cooldown_min)),
        scan_sec=max(5, int(args.scan_sec)), status_sec=max(5, int(args.status_sec)),
        data_dir=args.data_dir or str(DATA),
        state_path=args.state or "", log_path=args.log or "", killswitch_path=args.killswitch or "",
    )
    try:
        validate_settings(cfg)
    except ValueError as exc:
        print(f"[demo-dca] 설정 오류: {exc}", flush=True)
        return 2

    try:
        client = build_client(force_mock=args.mock)
    except BybitConfigError as exc:
        print(f"[demo-dca] 설정 오류: {exc}", flush=True)
        return 2

    try:
        engine = DemoDcaExperiment(cfg, client=client, ai=EntryDecider(model=args.model))
    except (BybitConfigError, ValueError) as exc:
        print(f"[demo-dca] 설정 오류: {exc}", flush=True)
        return 2

    if not args.quiet:
        print_banner(engine)
        sync = engine.sync_clock()
        if sync.get("error"):
            print(f"[demo-dca] 시각 동기화 실패(계속): {sync['error']}", flush=True)
        elif not sync.get("mock"):
            print(f"[demo-dca] 거래소 시각과 {sync['offset_ms'] / 1000:+.1f}초 차이를 맞췄습니다", flush=True)

    if args.status:
        print_status(engine)
        return 0
    if args.stop or args.resume:
        if args.stop:
            os.makedirs(os.path.dirname(os.path.abspath(engine.pause_path)), exist_ok=True)
            Path(engine.pause_path).write_text(
                f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} 신규 매매 정지\n",
                encoding="utf-8")
        elif os.path.isfile(engine.pause_path):
            os.remove(engine.pause_path)
        print(f"[demo-dca] 신규 매매 {'정지' if args.stop else '재개'} — 열린 포지션은 거래소 TP/SL이 계속 관리합니다.",
              flush=True)
        return 0

    if not args.mock:
        others = other_engine_locks(cfg.data_dir)
        if others:
            print("[demo-dca] 다른 엔진이 같은 데모 계좌에서 돌고 있는 것 같습니다:\n         "
                  + "\n         ".join(others)
                  + "\n         한 계좌에 두 엔진이 주문하면 서로의 포지션을 건드립니다 — "
                    "그 창을 먼저 끄고(잠금 파일이 남았으면 지우고) 다시 실행하세요.", flush=True)
            return 1

    lock = RunLock(engine.state_path + ".lock")
    if not lock.acquire():
        print(f"[demo-dca] 이미 실행 중인 것 같습니다({engine.state_path}.lock). 두 번 돌리면 주문이 "
              "겹칩니다 — 확실히 꺼져 있다면 그 파일을 지우고 다시 실행하세요.", flush=True)
        return 1
    atexit.register(lock.release)

    if args.reset:
        try:
            engine.reset()
        except (RuntimeError, BybitError) as exc:
            print(f"[demo-dca] 초기화하지 않았습니다: {exc}", flush=True)
            return 1
        print("[demo-dca] 로컬 기록을 초기화했습니다(거래소 계좌는 그대로).", flush=True)
        return 0

    def once() -> int:
        try:
            rec = engine.tick()
        except Exception as exc:
            print(f"[demo-dca] 점검 실패: {exc}", flush=True)
            return 1
        if not args.quiet:
            print_record(rec)
        return 0

    if args.loop:
        if not args.quiet:
            print(f"[demo-dca] {cfg.symbol} 5분봉 · 포지션 없으면 {cfg.scan_sec}초 · 있으면 "
                  f"{cfg.pos_tick_sec}초 감시 · {cfg.status_sec}초마다 상태 한 줄 · 상태 {engine.state_path}",
                  flush=True)
        return run_live_loop(engine, quiet=args.quiet, scan_sec=cfg.scan_sec,
                             status_sec=cfg.status_sec, pos_tick_sec=cfg.pos_tick_sec)
    return once()


if __name__ == "__main__":
    raise SystemExit(main())
