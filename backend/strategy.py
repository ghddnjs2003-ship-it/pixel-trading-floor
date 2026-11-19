"""더블 S + MDS v7 룰 엔진 — 트레이딩뷰 통합 지표(indicators/double-s-mds-v7.pine)의 '판정' 이식.

이 파일은 v7 파인스크립트의 규칙을 화면 없이(백테스트·서버 판정·AI 두뇌 문맥) 돌리기 위한 것이다.
라벨 서식·박스 그리기·대시보드는 옮기지 않았고, **신호가 서는 자리와 안 서는 자리**만 다룬다.

2026-11-19 개정 — 파인과 1:1 대조를 거쳐 아래를 파인 그대로 맞췄다(이전 초안과 다른 점):
- 스윙 레벨: 파인의 `ta.pivothigh/low(high/low, 10, 10)` + 배열 최대 5개(소모된 레벨도 자리를 차지한다)
- 스윕: 깊이 0.05~1.60 ATR, 같은 봉 거부(종가 복귀) 또는 다음 봉 복귀, 0.5 ATR 완전 돌파 시 폐기
- 추세 필터: EMA20/EMA50 + 3연속 음봉·양봉 (파인의 isStrongUp/isStrongDown)
- BOS(직전 봉 구조 돌파) 요구 없음 — v7 파인에는 없는 조건이었다(초안이 넣어 신호를 지웠다)
- 매물대 존: 거래량 프로파일이 아니라 **1시간봉 몸통 피벗(좌우 5봉) ± 0.30 × 1H ATR** (파인 [9])
- M1: 존 상단/하단을 거래량 1.15배로 종가 돌파 → 리테스트 8봉 이내, 0.15 ATR 터치, 경계 유지,
  CCI(20, hlc3) 방향 + 볼린저 폭 확장(10봉 평균 대비), 리테스트 거래량 감소, 15봉 쿨타임, 방향별 연속 2회
- 손절: M1은 존 반대편 경계(파인의 m1Stop), 더블 S는 스윕 극점 밖(파인에 없는 값 — CES 룰)

용어
- BSL/SSL : 스윙 고점·저점의 유동성 레벨(좌우 10봉 프랙털 피벗)
- 스윕    : 꼬리가 레벨을 넘고 종가는 레벨 안쪽으로 돌아온 봉
- 거부(rejection) : 스윕 봉 하나로 회복이 끝난 경우 (`▲/▼ · 거부` 라벨)
- 복귀(reclaim)   : 스윕 봉이 레벨 밖에서 마감하고 다음 봉이 안쪽으로 복귀한 경우 (`복귀` 라벨)
- 매물대 존      : 1시간봉 몸통 피벗을 ±0.30 ATR 두께로 감싼 띠. 확정(실선) 전에도 M1 기준선이 된다
- M1      : 존 경계를 거래량과 함께 몸통 종가로 돌파한 뒤, 첫 리테스트에서 경계 지지를 확인하고 진입
- M1-2    : M1 손절(존 재진입) 뒤 같은 경계를 거래량 실어 재돌파할 때의 재진입

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
    """v7 파인 입력값과 같은 이름·같은 기본값을 쓴다(화면 설정을 바꾸면 여기도 같이)."""

    # [1] 기본
    atr_period: int = 14

    # [3] 유동성 레벨 — pLen(스윙 룩백) 10, 최대 5개
    swing_piv: int = 10
    level_keep: int = 5

    # [4] 스윕 깊이·무효화 (파인 grp_sweep)
    sweep_min_atr: float = 0.05
    sweep_max_atr: float = 1.60
    sweep_invalidate_atr: float = 0.5

    # [2] 추세 필터 (파인 useTrendFilter)
    use_trend_filter: bool = True

    # [9] MDS 매물대 존 (파인 grp_box)
    box_tf_minutes: int = 60
    box_piv: int = 5
    box_atr_mult: float = 0.30
    box_per_side: int = 3
    box_max_bars: int = 60
    box_min_age: int = 4
    box_exit_buf_atr: float = 0.10

    # [10] M1 돌파·리테스트 (파인 grp_m1 / grp_retest)
    volume_mult: float = 1.15
    vol_len: int = 20
    retest_max_bars: int = 8
    retest_touch_atr: float = 0.15
    retest_volume_drop: bool = True
    retest_vol_avg_mult: float = 1.10
    use_m12: bool = True
    m12_bars: int = 12
    cooldown_bars: int = 15
    max_consec_signals: int = 2

    # [8] 내부 필터 (파인 grp_filt)
    cci_period: int = 20
    cci_mode: str = "sign"              # off | sign(부호) | threshold(±100)
    bb_period: int = 20
    bb_mult: float = 2.0
    bb_expand: bool = True

    # 상위 추세 동조 (파인 grp_htf, 기본 OFF) — 파인은 M1 리테스트에만 쓴다
    use_htf_trend: bool = False
    htf_ema_len: int = 50
    ds_htf_filter: bool = False         # 더블 S에도 4H 추세 동조를 요구(파인에는 없는 실험 옵션)
    allow_sides: Tuple[str, ...] = ("LONG", "SHORT")

    # 신호 종류 토글 (파인 useDoubleS / useMDS)
    use_ds: bool = True
    use_m1: bool = True

    # 손절·목표 (파인 밖 — CES 룰·백테스트용). M1 손절은 파인의 존 경계를 쓴다.
    stop_mode: str = "structure"        # structure(스윕 극점·존 반대편) | atr(진입가 ∓ 배수 × ATR)
    stop_atr: float = 1.0               # stop_mode == "atr"일 때 두 신호 공통 손절 폭(ATR 배수)
    stop_buffer_atr: float = 0.15       # 더블 S 손절: 스윕 극점에서 0.15 ATR 바깥
    m1_stop_buffer_atr: float = 0.0     # M1 손절: 존 반대편 경계에서 이만큼 더 바깥(0 = 파인 그대로)
    min_rr: float = 1.5                 # 목표는 반대편 유동성·존, 없으면 이 배수 R
    target_r: float = 1.5
    ds_target: str = "liq"              # liq(반대편 유동성) | zone(반대편 1H 매물대) | r(고정 배수)
    m1_target: str = "zone"             # zone(반대편 1H 매물대) | liq(반대편 유동성) | r(고정 배수)
    require_bos: bool = False           # 백테스트 비교용 옵션(v7 파인에는 없음)

    # 신호 선별 — 라벨·변동성·거래량
    ds_labels: Tuple[str, ...] = ("거부", "복귀")   # 더블 S 라벨 중 쓸 것
    min_rvol: float = 0.0               # 더블 S 신호 봉의 상대 거래량 하한(0 = 제한 없음)
    min_atr_pct: float = 0.0            # ATR/가격 × 100 하한(죽은 장 회피). 0 = 제한 없음
    max_atr_pct: float = 100.0          # ATR/가격 × 100 상한(폭발 장 회피)

    def to_dict(self) -> Dict:
        return asdict(self)


# ----------------------------------------------------------------------------- 지표

def atr_series(candles: Sequence[Dict], period: int = 14) -> List[Optional[float]]:
    """Wilder ATR (파인 ta.atr과 같은 RMA 방식). 값이 모자란 구간은 None."""
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
    prev = sum(trs[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(trs)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def sma_series(values: Sequence[Optional[float]], period: int) -> List[Optional[float]]:
    """단순이동평균 — 창 안에 결측이 하나라도 있으면 None(파인의 na 전파와 같다)."""
    out: List[Optional[float]] = [None] * len(values)
    acc = 0.0
    cnt = 0
    for i, v in enumerate(values):
        if v is not None:
            acc += v
            cnt += 1
        if i >= period:
            old = values[i - period]
            if old is not None:
                acc -= old
                cnt -= 1
        if i >= period - 1 and cnt == period:
            out[i] = acc / period
    return out


def ema_series(values: Sequence[float], period: int) -> List[Optional[float]]:
    """파인 ta.ema와 같이 첫 period개 SMA로 시드한다."""
    out: List[Optional[float]] = [None] * len(values)
    n = len(values)
    if n < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    k = 2 / (period + 1)
    for i in range(period, n):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def cci_series(candles: Sequence[Dict], period: int = 20) -> List[Optional[float]]:
    """CCI(period, hlc3) — 파인 ta.cci와 같은 평균절대편차 방식."""
    out: List[Optional[float]] = [None] * len(candles)
    tp = [(c["h"] + c["l"] + c["c"]) / 3 for c in candles]
    for i in range(len(candles)):
        if i < period - 1:
            continue
        win = tp[i - period + 1:i + 1]
        mean = sum(win) / period
        mad = sum(abs(v - mean) for v in win) / period
        out[i] = 0.0 if mad == 0 else (tp[i] - mean) / (0.015 * mad)
    return out


def bollinger_width_series(candles: Sequence[Dict], period: int = 20,
                           mult: float = 2.0) -> List[Optional[float]]:
    """볼린저 폭 % — 파인의 bbWidth = (상단-하단)/중심 × 100. 표준편차는 모표준편차."""
    out: List[Optional[float]] = [None] * len(candles)
    closes = [c["c"] for c in candles]
    for i in range(len(closes)):
        if i < period - 1:
            continue
        win = closes[i - period + 1:i + 1]
        mid = sum(win) / period
        sd = (sum((v - mid) ** 2 for v in win) / period) ** 0.5
        out[i] = ((mid + mult * sd) - (mid - mult * sd)) / mid * 100 if mid else 0.0
    return out


def pivots(values: Sequence[float], left: int, right: int, kind: str = "high") -> List[Optional[float]]:
    """파인 ta.pivothigh/pivotlow 재현 — 확인 봉(피벗 + right)에서 값이 나온다.

    트레이딩뷰 피벗은 '같은 값'을 허용한다. 몸통 고점(max(open,close))처럼 앞봉 종가와
    다음 봉 시가가 이어지는 계열에서는 같은 값이 늘어서므로, 양쪽 모두 초과를 요구하면
    피벗이 하나도 잡히지 않는다(1H 매물대 존이 사라진다). 왼쪽 이상(≥) · 오른쪽 초과(>)로
    두면 같은 값이 늘어선 구간에서 마지막 봉 하나만 피벗이 된다.
    """
    n = len(values)
    out: List[Optional[float]] = [None] * n
    for i in range(left + right, n):
        p = i - right
        v = values[p]
        ok = True
        if kind == "high":
            for j in range(p - left, p):
                if values[j] >= v:
                    ok = False
                    break
            if ok:
                for j in range(p + 1, p + right + 1):
                    if values[j] > v:
                        ok = False
                        break
        else:
            for j in range(p - left, p):
                if values[j] <= v:
                    ok = False
                    break
            if ok:
                for j in range(p + 1, p + right + 1):
                    if values[j] < v:
                        ok = False
                        break
        if ok:
            out[i] = v
    return out


def _bucket(t: int, minutes: int) -> int:
    ms = minutes * 60_000
    return t - (t % ms)


def resample(candles: Sequence[Dict], minutes: int) -> List[Dict]:
    """상위 시간봉 집계 — 봉 시작 시각을 minutes 격자에 맞춘다(파인 request.security의 봉 경계)."""
    out: List[Dict] = []
    cur: Optional[Dict] = None
    for c in candles:
        b = _bucket(int(c["t"]), minutes)
        if cur is None or cur["t"] != b:
            if cur is not None:
                out.append(cur)
            cur = {"t": b, "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"], "v": c.get("v", 0.0)}
        else:
            cur["h"] = max(cur["h"], c["h"])
            cur["l"] = min(cur["l"], c["l"])
            cur["c"] = c["c"]
            cur["v"] += c.get("v", 0.0)
    if cur is not None:
        out.append(cur)
    return out


# ----------------------------------------------------------------------------- 상태 객체

@dataclass
class Level:
    price: float
    born: int                 # 파인이 라벨을 다는 봉(피벗 확인 봉)
    pivot_bar: int            # 실제 피벗 봉
    consumed: bool = False
    swept: bool = False
    consumed_at: Optional[int] = None

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class Zone:
    top: float
    bot: float
    mid: float
    is_res: bool
    born: int
    frozen: bool = False
    frozen_bar: Optional[int] = None
    pivot: Optional[float] = None

    def to_dict(self) -> Dict:
        return asdict(self)


def _blank(reason: str = "") -> Dict:
    return {
        "side": None, "kind": None, "grade": None, "index": None, "time": None,
        "entry": None, "stop": None, "target": None, "rr": None,
        "zone": None, "level": None, "reasons": [reason] if reason else [],
    }


# ----------------------------------------------------------------------------- 엔진

class Engine:
    """한 번 만들고 run(candles)으로 전 구간을 훑는다. 상태(레벨·존)는 self.state에 남는다."""

    def __init__(self, params: Optional[Params] = None):
        self.p = params or Params()
        self.state: Dict = {"bsl": [], "ssl": [], "res": [], "sup": []}
        self.signals: List[Dict] = []

    # -- 준비 ---------------------------------------------------------------
    def _prepare(self, candles: Sequence[Dict]) -> Dict:
        p = self.p
        n = len(candles)
        closes = [c["c"] for c in candles]
        atr = atr_series(candles, p.atr_period)
        ema_fast = ema_series(closes, 20)
        ema_slow = ema_series(closes, 50)
        cci = cci_series(candles, p.cci_period)
        bbw = bollinger_width_series(candles, p.bb_period, p.bb_mult)
        bbw_avg = sma_series(bbw, 10)
        volsma = sma_series([c.get("v", 0.0) for c in candles], p.vol_len)
        ph = pivots([c["h"] for c in candles], p.swing_piv, p.swing_piv, "high")
        pl = pivots([c["l"] for c in candles], p.swing_piv, p.swing_piv, "low")

        htf = resample(candles, p.box_tf_minutes)
        htf_atr = atr_series(htf, p.atr_period)
        htf_ph = pivots([max(x["o"], x["c"]) for x in htf], p.box_piv, p.box_piv, "high")
        htf_pl = pivots([min(x["o"], x["c"]) for x in htf], p.box_piv, p.box_piv, "low")
        tf_ms = p.box_tf_minutes * 60_000
        closed: List[int] = [-1] * n
        k = 0
        for i, c in enumerate(candles):
            while k < len(htf) and int(htf[k]["t"]) + tf_ms <= int(c["t"]):
                k += 1
            closed[i] = k - 1

        # 상위 추세 동조(옵션) — 4H EMA50
        htf4 = resample(candles, 240)
        ema4 = ema_series([x["c"] for x in htf4], p.htf_ema_len)
        tf4_ms = 240 * 60_000
        closed4: List[int] = [-1] * n
        k = 0
        for i, c in enumerate(candles):
            while k < len(htf4) and int(htf4[k]["t"]) + tf4_ms <= int(c["t"]):
                k += 1
            closed4[i] = k - 1

        return {"atr": atr, "ema_fast": ema_fast, "ema_slow": ema_slow, "cci": cci,
                "bbw": bbw, "bbw_avg": bbw_avg, "volsma": volsma, "ph": ph, "pl": pl,
                "htf_atr": htf_atr, "htf_ph": htf_ph, "htf_pl": htf_pl, "closed": closed,
                "ema4": ema4, "closed4": closed4}

    # -- 본체 ---------------------------------------------------------------
    def run(self, candles: Sequence[Dict]) -> List[Dict]:
        p = self.p
        pre = self._prepare(candles)
        atr, ema_fast, ema_slow, cci = pre["atr"], pre["ema_fast"], pre["ema_slow"], pre["cci"]
        bbw, bbw_avg, volsma = pre["bbw"], pre["bbw_avg"], pre["volsma"]
        ph, pl = pre["ph"], pre["pl"]
        htf_atr, htf_ph, htf_pl, closed = pre["htf_atr"], pre["htf_ph"], pre["htf_pl"], pre["closed"]
        ema4, closed4 = pre["ema4"], pre["closed4"]

        bsl: List[Level] = []
        ssl: List[Level] = []
        res: List[Zone] = []
        sup: List[Zone] = []
        signals: List[Dict] = []

        last_m1_bar: Optional[int] = None
        consec_long = 0
        consec_short = 0
        up = {"armed": False, "bar": None, "lvl": None, "bot": None, "vol": None,
              "m12": False, "stopped": False, "stopbar": None}
        dn = dict(up)

        def trim(arr: List[Zone], close: float) -> None:
            while len(arr) > p.box_per_side:
                wi = max(range(len(arr)), key=lambda k2: abs(arr[k2].mid - close))
                arr.pop(wi)

        for i in range(len(candles)):
            c = candles[i]
            a = atr[i]
            if a is None or ema_fast[i] is None or ema_slow[i] is None:
                continue

            # [3] 유동성 레벨 — 새 피벗이 확인되면 배열에 넣고, 넘치면 가장 오래된 것부터 버린다
            if ph[i] is not None:
                bsl.append(Level(price=ph[i], born=i, pivot_bar=i - p.swing_piv))
                if len(bsl) > p.level_keep:
                    bsl.pop(0)
            if pl[i] is not None:
                ssl.append(Level(price=pl[i], born=i, pivot_bar=i - p.swing_piv))
                if len(ssl) > p.level_keep:
                    ssl.pop(0)

            # [2] 단기 추세 필터
            strong_up = ((c["c"] > ema_fast[i] and ema_fast[i] > ema_slow[i])
                         or (i >= 2 and c["c"] > c["o"] and candles[i - 1]["c"] > candles[i - 1]["o"]
                             and candles[i - 2]["c"] > candles[i - 2]["o"]))
            strong_dn = ((c["c"] < ema_fast[i] and ema_fast[i] < ema_slow[i])
                         or (i >= 2 and c["c"] < c["o"] and candles[i - 1]["c"] < candles[i - 1]["o"]
                             and candles[i - 2]["c"] < candles[i - 2]["o"]))
            rng = max(c["h"] - c["l"], 1e-9)
            upper_wick = c["h"] - max(c["o"], c["c"])
            lower_wick = min(c["o"], c["c"]) - c["l"]
            prev = candles[i - 1] if i >= 1 else None

            short_rej = short_rec = long_rej = long_rec = False
            hit_level: Optional[Level] = None

            # [4] 숏 스윕 (BSL)
            if p.use_ds:
                for it in bsl:
                    if it.consumed:
                        continue
                    depth = (c["h"] - it.price) / a
                    sweep = c["h"] > it.price and p.sweep_min_atr <= depth <= p.sweep_max_atr
                    bearish = c["c"] < c["o"] or (upper_wick >= rng * 0.4 and c["c"] < c["h"] - rng * 0.3)
                    pass_trend = (not p.use_trend_filter) or (not strong_up) or (
                        prev is not None and c["c"] < prev["l"] and c["c"] < c["o"])
                    if sweep and c["c"] < it.price and bearish and pass_trend:
                        it.consumed = it.swept = True
                        it.consumed_at = i
                        short_rej = True
                        hit_level = hit_level or it
                        continue
                    if prev is not None:
                        d2 = (prev["h"] - it.price) / a
                        prev_above = (prev["h"] > it.price and prev["c"] >= it.price
                                      and p.sweep_min_atr <= d2 <= p.sweep_max_atr)
                        if prev_above and c["c"] < it.price and c["c"] < c["o"] and pass_trend:
                            it.consumed = it.swept = True
                            it.consumed_at = i
                            short_rec = True
                            hit_level = hit_level or it
                            continue
                    if c["c"] > it.price + a * p.sweep_invalidate_atr and c["c"] > c["o"]:
                        it.consumed = True
                        it.consumed_at = i
            # [4] 롱 스윕 (SSL)
            if p.use_ds:
                for it in ssl:
                    if it.consumed:
                        continue
                    depth = (it.price - c["l"]) / a
                    sweep = c["l"] < it.price and p.sweep_min_atr <= depth <= p.sweep_max_atr
                    bullish = c["c"] > c["o"] or (lower_wick >= rng * 0.4 and c["c"] > c["l"] + rng * 0.3)
                    pass_trend = (not p.use_trend_filter) or (not strong_dn) or (
                        prev is not None and c["c"] > prev["h"] and c["c"] > c["o"])
                    if sweep and c["c"] > it.price and bullish and pass_trend:
                        it.consumed = it.swept = True
                        it.consumed_at = i
                        long_rej = True
                        hit_level = hit_level or it
                        continue
                    if prev is not None:
                        d2 = (it.price - prev["l"]) / a
                        prev_below = (prev["l"] < it.price and prev["c"] <= it.price
                                      and p.sweep_min_atr <= d2 <= p.sweep_max_atr)
                        if prev_below and c["c"] > it.price and c["c"] > c["o"] and pass_trend:
                            it.consumed = it.swept = True
                            it.consumed_at = i
                            long_rec = True
                            hit_level = hit_level or it
                            continue
                    if c["c"] < it.price - a * p.sweep_invalidate_atr and c["c"] < c["o"]:
                        it.consumed = True
                        it.consumed_at = i

            # [5] 더블 S 신호
            k4 = closed4[i]
            ema4_val = ema4[k4] if k4 >= 0 else None
            ds_up_ok = (not p.ds_htf_filter) or (ema4_val is not None and c["c"] > ema4_val)
            ds_dn_ok = (not p.ds_htf_filter) or (ema4_val is not None and c["c"] < ema4_val)
            if short_rej or short_rec:
                sig = self._ds_signal(candles, i, "SHORT", "거부" if short_rej else "복귀",
                                      c["h"], a, hit_level, bsl, ssl, "BSL", htf_ok=ds_dn_ok,
                                      res=res, sup=sup)
                if sig:
                    signals.append(sig)
            if long_rej or long_rec:
                sig = self._ds_signal(candles, i, "LONG", "거부" if long_rej else "복귀",
                                      c["l"], a, hit_level, bsl, ssl, "SSL", htf_ok=ds_up_ok,
                                      res=res, sup=sup)
                if sig:
                    signals.append(sig)

            # [9] 1H 매물대 존 — 상위 시간봉 몸통 피벗이 확인된 직후 시간에 만든다
            L = closed[i]
            Lp = closed[i - 1] if i >= 1 else -1
            hph_now = htf_ph[L] if L >= 0 else None
            hph_prev = htf_ph[Lp] if Lp >= 0 else None
            hpl_now = htf_pl[L] if L >= 0 else None
            hpl_prev = htf_pl[Lp] if Lp >= 0 else None
            base_atr = htf_atr[L] if L >= 0 and htf_atr[L] else a * 4.0
            zone_w = base_atr * p.box_atr_mult

            if p.use_m1 and p.use_ds and hph_now is not None and (hph_prev is None or hph_now != hph_prev):
                zt, zb = hph_now + zone_w, hph_now - zone_w
                if not any(abs(z.mid - hph_now) <= zone_w * 1.5 for z in res):
                    res.append(Zone(top=zt, bot=zb, mid=(zt + zb) / 2, is_res=True, born=i, pivot=hph_now))
                trim(res, c["c"])
            if p.use_m1 and p.use_ds and hpl_now is not None and (hpl_prev is None or hpl_now != hpl_prev):
                zt, zb = hpl_now + zone_w, hpl_now - zone_w
                if not any(abs(z.mid - hpl_now) <= zone_w * 1.5 for z in sup):
                    sup.append(Zone(top=zt, bot=zb, mid=(zt + zb) / 2, is_res=False, born=i, pivot=hpl_now))
                trim(sup, c["c"])

            # 존 확정(점선 → 실선): 이탈 + 최소 4봉, 또는 60봉 경과
            for arr in (res, sup):
                for z in arr:
                    if z.frozen:
                        continue
                    broke = (c["c"] > z.top + a * p.box_exit_buf_atr
                             or c["c"] < z.bot - a * p.box_exit_buf_atr)
                    if (broke and i - z.born >= p.box_min_age) or i - z.born >= p.box_max_bars:
                        z.frozen = True
                        z.frozen_bar = i

            # [10] M1 타점
            vol_sma = volsma[i]
            vol_ok = vol_sma is not None and c.get("v", 0.0) > vol_sma * p.volume_mult
            if p.use_m1 and prev is not None:
                arm_top = None
                for z in res:
                    if prev["c"] <= z.top and c["c"] > z.top and vol_ok:
                        if arm_top is None or z.top < arm_top[0]:
                            arm_top = (z.top, z.bot)
                if arm_top is not None:
                    up.update(armed=True, bar=i, lvl=arm_top[0], bot=arm_top[1], vol=c.get("v", 0.0),
                              m12=False, stopped=False)
                arm_bot = None
                for z in sup:
                    if prev["c"] >= z.bot and c["c"] < z.bot and vol_ok:
                        if arm_bot is None or z.bot > arm_bot[0]:
                            arm_bot = (z.bot, z.top)
                if arm_bot is not None:
                    dn.update(armed=True, bar=i, lvl=arm_bot[0], bot=arm_bot[1], vol=c.get("v", 0.0),
                              m12=False, stopped=False)

            cci_now = cci[i]
            bb_expand = (bbw[i] is not None and bbw_avg[i] is not None and bbw[i] > bbw_avg[i])
            if p.cci_mode == "off":
                cci_long = cci_short = True
            elif p.cci_mode == "threshold":
                cci_long = cci_now is not None and cci_now >= 100
                cci_short = cci_now is not None and cci_now <= -100
            else:
                cci_long = cci_now is not None and cci_now > 0
                cci_short = cci_now is not None and cci_now < 0
            filt_long = cci_long and ((not p.bb_expand) or bb_expand)
            filt_short = cci_short and ((not p.bb_expand) or bb_expand)

            htf_up = htf_dn = True
            if p.use_htf_trend:
                k4 = closed4[i]
                ema_val = ema4[k4] if k4 >= 0 else None
                htf_up = ema_val is not None and c["c"] > ema_val
                htf_dn = ema_val is not None and c["c"] < ema_val

            sig_long = sig_long_m12 = sig_short = sig_short_m12 = False
            if p.use_m1 and up["armed"] and i > up["bar"]:
                age = i - up["bar"]
                touch = c["l"] <= up["lvl"] + a * p.retest_touch_atr
                holds = c["c"] > up["lvl"]
                bull = c["c"] > c["o"]
                vol_drop = (not p.retest_volume_drop) or up["vol"] is None or c.get("v", 0.0) < up["vol"] or (
                    vol_sma is not None and c.get("v", 0.0) <= vol_sma * p.retest_vol_avg_mult)
                if age <= p.retest_max_bars and touch and holds and bull and filt_long and vol_drop and htf_up:
                    if up["m12"]:
                        sig_long_m12 = True
                    else:
                        sig_long = True
                    up["armed"] = False
                elif c["c"] < up["bot"]:
                    up["armed"] = False
                    up["stopped"] = True
                    up["stopbar"] = i
                elif age > p.retest_max_bars:
                    up["armed"] = False
            if p.use_m1 and dn["armed"] and i > dn["bar"]:
                age = i - dn["bar"]
                touch = c["h"] >= dn["lvl"] - a * p.retest_touch_atr
                holds = c["c"] < dn["lvl"]
                bear = c["c"] < c["o"]
                vol_drop = (not p.retest_volume_drop) or dn["vol"] is None or c.get("v", 0.0) < dn["vol"] or (
                    vol_sma is not None and c.get("v", 0.0) <= vol_sma * p.retest_vol_avg_mult)
                if age <= p.retest_max_bars and touch and holds and bear and filt_short and vol_drop and htf_dn:
                    if dn["m12"]:
                        sig_short_m12 = True
                    else:
                        sig_short = True
                    dn["armed"] = False
                elif c["c"] > dn["bot"]:
                    dn["armed"] = False
                    dn["stopped"] = True
                    dn["stopbar"] = i
                elif age > p.retest_max_bars:
                    dn["armed"] = False

            # M1-2 — 손절(존 재진입) 뒤 같은 경계 재돌파
            if p.use_m1 and p.use_m12:
                if up["stopped"] and not up["armed"]:
                    if i - up["stopbar"] > p.m12_bars:
                        up["stopped"] = False
                    elif prev is not None and c["c"] > up["lvl"] and prev["c"] <= up["lvl"] and vol_ok:
                        up.update(stopped=False, armed=True, bar=i, m12=True)
                if dn["stopped"] and not dn["armed"]:
                    if i - dn["stopbar"] > p.m12_bars:
                        dn["stopped"] = False
                    elif prev is not None and c["c"] < dn["lvl"] and prev["c"] >= dn["lvl"] and vol_ok:
                        dn.update(stopped=False, armed=True, bar=i, m12=True)

            # 라벨 + 쿨타임 + 연속 신호 제한
            if last_m1_bar is not None and i - last_m1_bar > p.cooldown_bars * 3:
                consec_long = consec_short = 0
            cool = last_m1_bar is None or i - last_m1_bar >= p.cooldown_bars

            if (sig_long or sig_long_m12) and cool and consec_long < p.max_consec_signals:
                sig = self._m1_signal(candles, i, "LONG", sig_long, up, res, sup, a,
                                      sig_long_m12, up["lvl"], bsl, ssl)
                if sig:
                    signals.append(sig)
                    last_m1_bar = i
                    consec_long += 1
                    consec_short = 0
            if (sig_short or sig_short_m12) and cool and consec_short < p.max_consec_signals:
                sig = self._m1_signal(candles, i, "SHORT", sig_short, dn, res, sup, a,
                                      sig_short_m12, dn["lvl"], bsl, ssl)
                if sig:
                    signals.append(sig)
                    last_m1_bar = i
                    consec_short += 1
                    consec_long = 0

        self.state = {"bsl": bsl, "ssl": ssl, "res": res, "sup": sup}
        self.signals = signals
        return signals

    # -- 신호 만들기 --------------------------------------------------------
    def _ds_signal(self, candles, i: int, side: str, label: str, extreme: float, atr: float,
                   level: Optional[Level], bsl: List[Level], ssl: List[Level], level_name: str,
                   htf_ok: bool = True, res: Optional[List[Zone]] = None,
                   sup: Optional[List[Zone]] = None) -> Optional[Dict]:
        p = self.p
        c = candles[i]
        entry = c["c"]
        if side not in p.allow_sides or label not in p.ds_labels:
            return None
        if not htf_ok:
            return None
        if not _vol_band_ok(p, entry, atr):
            return None
        rvol = _rvol(candles, i, p.vol_len)
        if p.min_rvol > 0 and (rvol is None or rvol < p.min_rvol):
            return None
        if side == "LONG":
            if p.require_bos and i >= 1 and not c["c"] > candles[i - 1]["h"]:
                return None
            liq = sorted([l.price for l in bsl if not l.consumed and l.price > entry])
            zone = sorted([z.bot for z in (res or []) if z.bot > entry])
        else:
            if p.require_bos and i >= 1 and not c["c"] < candles[i - 1]["l"]:
                return None
            liq = sorted([l.price for l in ssl if not l.consumed and l.price < entry], reverse=True)
            zone = sorted([z.top for z in (sup or []) if z.top < entry], reverse=True)
        if p.stop_mode == "atr":
            stop = entry - p.stop_atr * atr if side == "LONG" else entry + p.stop_atr * atr
            stop_atr = p.stop_atr
        else:
            stop = extreme - p.stop_buffer_atr * atr if side == "LONG" else extreme + p.stop_buffer_atr * atr
            stop_atr = None
        risk = abs(entry - stop)
        if risk <= 0:
            return None
        cands = {"liq": liq, "zone": zone, "r": []}[p.ds_target]
        target, fallback = _first_target(side, entry, risk, cands, p.min_rr, p.target_r)
        reasons = []
        if level is not None:
            reasons.append(f"{level_name} {level.price:,.1f} 스윕 후 {'복귀' if label == '복귀' else '거부'}")
        why = "1.5R 대체" if fallback else f"{target:,.1f}"
        if not fallback and p.ds_target != "liq":
            why += "(존)" if p.ds_target == "zone" else ""
        reasons.append(f"손절 {stop:,.1f} · 목표 {why}")
        return {
            "side": side, "kind": "DS", "label": label, "index": i, "time": c["t"],
            "entry": entry, "stop": stop, "target": target, "rr": round(abs(target - entry) / risk, 2),
            "risk": risk, "atr": atr, "stop_atr": stop_atr,
            "rvol": None if rvol is None else round(rvol, 2),
            "grade": "A" if (rvol is not None and rvol >= 1.5) else "B",
            "level": None if level is None else level.price,
            "zone": None, "reasons": reasons,
        }

    def _m1_signal(self, candles, i: int, side: str, primary: bool, state: Dict,
                   res: List[Zone], sup: List[Zone], atr: float, m12: bool, lvl: float,
                   bsl: Optional[List[Level]] = None,
                   ssl: Optional[List[Level]] = None) -> Optional[Dict]:
        p = self.p
        c = candles[i]
        entry = c["c"]
        if side not in p.allow_sides:      # 방향 제한은 두 신호 종류 모두에 걸린다(더블 S만 걸리던 버그 수정)
            return None
        if not _vol_band_ok(p, entry, atr):
            return None
        buf = p.m1_stop_buffer_atr * atr
        if p.stop_mode == "atr":
            stop = entry - p.stop_atr * atr if side == "LONG" else entry + p.stop_atr * atr
            stop_atr = p.stop_atr
        else:
            stop = state["bot"] - buf if side == "LONG" else state["bot"] + buf
            stop_atr = None
        if side == "LONG":
            zone = sorted([z.bot for z in res if z.bot > lvl])
            liq = sorted([l.price for l in (bsl or []) if not l.consumed and l.price > entry])
        else:
            zone = sorted([z.top for z in sup if z.top < lvl], reverse=True)
            liq = sorted([l.price for l in (ssl or []) if not l.consumed and l.price < entry], reverse=True)
        risk = abs(entry - stop)
        if risk <= 0:
            return None
        cands = {"zone": zone, "liq": liq, "r": []}[p.m1_target]
        target, fallback = _first_target(side, entry, risk, cands, p.min_rr, p.target_r)
        reasons = [f"{'존 상단' if side == 'LONG' else '존 하단'} {lvl:,.1f} 돌파 뒤 첫 리테스트 "
                   f"{'지지' if side == 'LONG' else '저항'}"]
        reasons.append(f"손절 {stop:,.1f}({'ATR' if stop_atr else '존 반대편'}) · "
                       f"목표 {'1.5R 대체' if fallback else f'{target:,.1f}'}")
        return {
            "side": side, "kind": "M1", "label": ("M1-2" if m12 else "M1") + (" 롱" if side == "LONG" else " 숏"),
            "index": i, "time": c["t"], "entry": entry, "stop": stop, "target": target,
            "rr": round(abs(target - entry) / risk, 2), "risk": risk, "atr": atr, "stop_atr": stop_atr,
            "rvol": None, "grade": "A" if primary else "B", "level": lvl, "zone": None,
            "reasons": reasons,
        }


def _first_target(side: str, entry: float, risk: float, cands: Sequence[float],
                  min_rr: float, target_r: float):
    """목표 후보 중 최소 손익비를 넘는 첫 자리. 없으면 고정 배수 R.

    반환: (목표가, 대체 여부). 대체했으면 True.
    """
    for t in cands:
        if side == "LONG" and (t - entry) / risk >= min_rr:
            return t, False
        if side == "SHORT" and (entry - t) / risk >= min_rr:
            return t, False
    return (entry + risk * target_r if side == "LONG" else entry - risk * target_r), True


def _vol_band_ok(p: Params, price: float, atr: float) -> bool:
    """변동성 구간 필터 — ATR/가격%가 [min_atr_pct, max_atr_pct] 안인지."""
    if price <= 0 or atr is None:
        return False
    pct = atr / price * 100
    return p.min_atr_pct <= pct <= p.max_atr_pct


def _rvol(candles: Sequence[Dict], i: int, period: int = 20) -> Optional[float]:
    if i < period:
        return None
    base = sum(c.get("v", 0.0) for c in candles[i - period:i]) / period
    if base <= 0:
        return None
    return candles[i].get("v", 0.0) / base


# ----------------------------------------------------------------------------- 공개 API

def scan(candles: Sequence[Dict], params: Optional[Params] = None, start: int = 0) -> List[Dict]:
    """전 구간 신호 목록. 신호는 봉이 끝난 뒤 확정되고, 진입은 다음 봉 시가(호출부 책임)."""
    p = params or Params()
    signals = Engine(p).run(candles)
    if start <= 0:
        return signals
    return [s for s in signals if s["index"] >= start]


def evaluate(candles: Sequence[Dict], i: int, params: Optional[Params] = None,
             trend_ema: Optional[Sequence[float]] = None) -> Dict:
    """i번 봉이 끝난 시점의 신호. 엔진을 앞에서부터 다시 돌려 i봉의 신호만 돌려준다(미래 참조 없음)."""
    p = params or Params()
    if candles is None or i is None or i < 0 or i >= len(candles):
        return _blank("봉이 모자람")
    if i < max(p.swing_piv * 2 + 2, p.atr_period + 2):
        return _blank("봉이 모자람")
    signals = Engine(p).run(list(candles[:i + 1]))
    for s in signals:
        if s["index"] == i:
            return s
    return _blank("조건 미충족")


def snapshot(candles: Sequence[Dict], params: Optional[Params] = None) -> Dict:
    """마지막 봉 기준 엔진 상태 — 살아 있는 BSL/SSL, 매물대 존, 대기 상태. 차트 대조·대시보드용."""
    eng = Engine(params or Params())
    signals = eng.run(candles)
    last = len(candles) - 1
    return {
        "last_index": last,
        "time": candles[last]["t"] if candles else None,
        "bsl": [round(l.price, 2) for l in eng.state["bsl"] if not l.consumed],
        "ssl": [round(l.price, 2) for l in eng.state["ssl"] if not l.consumed],
        "res": [z.to_dict() for z in eng.state["res"]],
        "sup": [z.to_dict() for z in eng.state["sup"]],
        "last_signals": signals[-5:],
        "signal_count": len(signals),
    }
