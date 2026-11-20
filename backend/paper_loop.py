"""실시간 모의매매 루프 — 후보1(30분 M1 · 아시아 · 3ATR · 4R · 96봉)을 **지금 장에서** 돌린다.

무엇을 하나
1) 공개 API(OKX/비트겟/바이낸스 폴백)로 최신 30분봉을 받아 **확정 봉만** 쓴다. 진행 중인 봉은 버리고,
   그 시가를 '다음 봉 시가'로 삼아 모의 진입가로 쓴다(추격 금지 규칙 그대로).
2) `strategy.Engine`으로 마지막 확정 봉의 M1 후보를 찾고, `ai_filter`로 PASS/REJECT와 그 이유를 받는다.
3) 열린 모의 포지션은 새로 확정된 봉마다 손절·4R 목표·96봉 시간청산을 판정해 체결 기록을 남긴다.
   (같은 봉은 두 번 평가하지 않는다 — `last_bar_time`·`signals_seen`)
4) `risk.KillSwitch`로 진입 가능 여부를 확인하고, 청산마다 실현 손익을 넘겨 연속 손절·일일 손실에서 멈춘다.
5) 결과는 `data/paper_state.json`(현재 포지션·통계·킬스위치 상태)과 `data/paper_log.jsonl`(매 스캔)에 남긴다.

주문은 내지 않는다. 거래소 키도 쓰지 않는다. 공개 시세 + 규칙 + AI 판정만으로
"지금 규칙이라면 무엇을 했을까"를 남기는 것이 이 모듈의 전부다.

실행
    python -m backend.paper_loop --once                    # 1회(스케줄러 연동용)
    python -m backend.paper_loop --loop --interval 300     # 5분마다
    python -m backend.paper_loop --status                  # 현재 상태 출력
    python -m backend.paper_loop --reset                   # 모의 계좌 초기화
"""

import argparse
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .ai_filter import AIFilter, build_context, nominee_setup, prepare_series
from .backtest import (INTERVAL_MS, BacktestConfig, _bps, in_session, load_candles, session_hours)
from .memory import LessonMemory, duplicate_entry, portfolio_context
from .regime import RegimeGuardConfig, features as regime_features, guard as regime_guard, series as regime_series
from .risk import KillSwitch
from .strategy import Engine, Params

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
KST = timezone(timedelta(hours=9))


def _iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def _kst(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=KST).strftime("%Y-%m-%d %H:%M")


@dataclass
class PaperConfig:
    """모의매매 설정 — 규칙은 후보1 그대로, 파일 경로만 테스트에서 바꿀 수 있게 열어 둔다."""

    symbol: str = "BTCUSDT"
    interval: str = "30"
    limit: int = 3000
    source: str = "auto"
    initial_equity: float = 10_000.0
    risk_pct: float = 1.0
    fee_bps: float = 5.0
    slippage_bps: float = 2.0
    max_leverage: float = 5.0
    target_r: float = 4.0
    stop_atr: float = 3.0
    max_bars: int = 96
    session: str = "asia"
    data_dir: str = str(DATA)
    state_path: str = ""
    log_path: str = ""
    killswitch_path: str = ""
    memory_path: str = ""              # 복기 메모리(청산 1건 = 한 줄)
    memory_md_path: str = ""
    log_tail: int = 50                 # /api/paper가 돌려줄 최근 로그 줄 수
    regime_guard: str = "off"          # off | dead | dead+panic — 기본은 끔(실측 없이 켜면 성적을 깎는다)

    def __post_init__(self):
        base = Path(self.data_dir)
        if not self.state_path:
            self.state_path = str(base / "paper_state.json")
        if not self.log_path:
            self.log_path = str(base / "paper_log.jsonl")
        if not self.killswitch_path:
            self.killswitch_path = str(base / "paper_killswitch.json")
        if not self.memory_path:
            self.memory_path = str(base / "paper_memory.jsonl")
        if not self.memory_md_path:
            self.memory_md_path = str(base / "memory.md")

    def to_dict(self) -> Dict:
        from dataclasses import asdict
        return asdict(self)


class PaperLoop:
    """한 번 만들고 tick()을 반복해서 부른다. 상태 파일이 있으면 이어서 돈다(프로세스 재시작 안전)."""

    def __init__(self, cfg: Optional[PaperConfig] = None, ai=None, killswitch: Optional[KillSwitch] = None,
                 fetch=None, now=None, memory: Optional[LessonMemory] = None):
        self.cfg = cfg or PaperConfig()
        self.ai = ai                    # None = 설정에서 만든다, False = AI 없이 규칙만(테스트·수동)
        self.fetch = fetch or self._fetch_default
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.killswitch = killswitch or KillSwitch(state_path=self.cfg.killswitch_path)
        self.memory = memory or LessonMemory(path=self.cfg.memory_path, md_path=self.cfg.memory_md_path)
        self.lock = threading.Lock()
        self._ai_filter = None

    def _regime_guard_cfg(self) -> RegimeGuardConfig:
        """설정 문자열 → 가드 설정. 기본은 꺼짐 — 켜면 국면이 진입을 막는다."""
        raw = (self.cfg.regime_guard or "off").strip().lower()
        if raw in ("", "off", "0", "false"):
            return RegimeGuardConfig(enabled=False)
        return RegimeGuardConfig(enabled=True, block_dead="dead" in raw, block_panic="panic" in raw)

    # ------------------------------------------------------------------ 준비
    def rule(self):
        """후보1 규칙 — 엔진 파라미터와 체결 모형(설정값 반영)."""
        params, cfg = nominee_setup()
        params = Params(**{**params.to_dict(), "stop_mode": "atr", "stop_atr": self.cfg.stop_atr})
        cfg = BacktestConfig(**{**cfg.to_dict(), "max_bars": self.cfg.max_bars,
                                "target_r": self.cfg.target_r, "session_filter": self.cfg.session,
                                "initial_equity": self.cfg.initial_equity, "risk_pct": self.cfg.risk_pct,
                                "fee_bps": self.cfg.fee_bps, "slippage_bps": self.cfg.slippage_bps,
                                "max_leverage": self.cfg.max_leverage})
        return params, cfg

    def _fetch_default(self) -> Dict:
        return load_candles(self.cfg.symbol, str(self.cfg.interval), self.cfg.limit, self.cfg.source)

    def _filter(self):
        """설정이 켜져 있으면 AI 필터를 만든다. 키가 없거나 실패하면 None(규칙만 돈다)."""
        if self.ai is False:
            return None
        if self.ai is not None:
            return self.ai
        if self._ai_filter is None:
            try:
                from .config import ai_filter_enabled, get_ai_filter_config
                self._ai_filter = AIFilter(get_ai_filter_config()) if ai_filter_enabled() else False
            except Exception as exc:                    # 필터가 안 서도 루프는 계속
                print(f"[paper] AI 필터 비활성: {exc}", flush=True)
                self._ai_filter = False
        return self._ai_filter or None

    # ------------------------------------------------------------------ 상태 파일
    def _blank_state(self) -> Dict:
        return {
            "version": 1, "symbol": self.cfg.symbol, "interval": int(self.cfg.interval),
            "rule": {"name": "후보1", "kinds": ["M1"], "session": self.cfg.session,
                     "stop_atr": self.cfg.stop_atr, "target_r": self.cfg.target_r,
                     "max_bars": self.cfg.max_bars},
            "created_at": self.now().isoformat(timespec="seconds"),
            "updated_at": None, "scan_bar": None, "scan_count": 0,
            "initial_equity": self.cfg.initial_equity, "equity": self.cfg.initial_equity,
            "position": None, "closed": [], "stats": {}, "signals_seen": [],
            "killswitch": {}, "last_scan": {},
        }

    def load_state(self) -> Dict:
        path = self.cfg.state_path
        if not path or not os.path.isfile(path):
            state = self._blank_state()
            self._recompute(state)
            return state
        try:
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
            if not isinstance(saved, dict):
                raise ValueError("상태 파일이 객체가 아닙니다")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"[paper] 상태 파일을 읽지 못해 새로 시작합니다: {exc}", flush=True)
            state = self._blank_state()
            self._recompute(state)
            return state
        state = {**self._blank_state(), **saved}
        # 설정이 바뀌어도 계좌·포지션은 이어 간다. 규칙 표기만 최신 설정으로 맞춘다.
        state["rule"] = self._blank_state()["rule"]
        state["symbol"] = self.cfg.symbol
        state["interval"] = int(self.cfg.interval)
        self._recompute(state)
        return state

    def save_state(self, state: Dict) -> None:
        path = self.cfg.state_path
        if not path:
            return
        state["updated_at"] = self.now().isoformat(timespec="seconds")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _append_log(self, record: Dict) -> None:
        path = self.cfg.log_path
        if not path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def tail_log(self, limit: Optional[int] = None) -> List[Dict]:
        path = self.cfg.log_path
        limit = limit or self.cfg.log_tail
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
        """청산 기록에서 통계를 다시 만든다(이어 돌려도 숫자가 어긋나지 않게)."""
        closed = state.get("closed") or []
        wins = [t for t in closed if t["pnl"] > 0]
        losses = [t for t in closed if t["pnl"] < 0]
        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = -sum(t["pnl"] for t in losses)
        peak = state.get("initial_equity") or self.cfg.initial_equity
        mdd = 0.0
        streak = worst = 0
        for t in closed:
            peak = max(peak, t.get("equity") or peak)
            if peak > 0:
                mdd = max(mdd, (peak - (t.get("equity") or peak)) / peak * 100)
            streak = streak + 1 if t["pnl"] < 0 else 0
            worst = max(worst, streak)
        r_values = [t.get("r") or 0.0 for t in closed]
        n = len(closed)
        state["stats"] = {
            "trades": n, "wins": len(wins), "losses": len(losses),
            "win_rate_pct": round(len(wins) / n * 100, 1) if n else None,
            "net_pnl": round(sum(t["pnl"] for t in closed), 2),
            "net_pct": round((state.get("equity", self.cfg.initial_equity) - self.cfg.initial_equity)
                             / self.cfg.initial_equity * 100, 2),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (
                None if not wins else None),
            "avg_r": round(sum(r_values) / n, 3) if n else None,
            "r_sum": round(sum(r_values), 2),
            "mdd_pct": round(mdd, 2),
            "max_losing_streak": worst,
            "targets": sum(1 for t in closed if t["reason"] == "TARGET"),
            "stops": sum(1 for t in closed if t["reason"] == "STOP"),
            "time_exits": sum(1 for t in closed if t["reason"] == "TIME"),
            "last_trade_at": closed[-1].get("exit_time") if closed else None,
        }
        return state["stats"]

    # ------------------------------------------------------------------ 스캔 본체
    def tick(self, candles: Optional[Sequence[Dict]] = None, now_ms: Optional[int] = None,
             signals: Optional[Sequence[Dict]] = None) -> Dict:
        """한 번 스캔한다. candles·signals를 주면 네트워크·엔진 없이 돈다(테스트·재현)."""
        with self.lock:
            now_ms = int(now_ms if now_ms is not None else self.now().timestamp() * 1000)
            source = None
            if candles is None:
                data = self.fetch()
                candles, source = data["candles"], data.get("source")
            candles = sorted((c for c in candles if c.get("t") is not None), key=lambda c: int(c["t"]))
            tf_ms = INTERVAL_MS.get(str(self.cfg.interval), 1_800_000)
            next_open = None
            while len(candles) > 1 and int(candles[-1]["t"]) + tf_ms > now_ms:
                forming = candles.pop()                  # 진행 중인 봉 — 신호 판정에 쓰지 않는다
                next_open = float(forming["o"])          # 이 봉의 시가 = 마지막 확정 봉의 '다음 봉 시가'
            if not candles:
                raise RuntimeError("확정 봉이 없습니다(캔들 부족 또는 시각 이상)")
            last = candles[-1]
            state = self.load_state()
            events: List[str] = []
            action = "HOLD"
            signal = verdict = None

            # 0) 킬스위치 상태(오늘 시작 자본·진입 가능 여부)
            self.killswitch.start_day(state.get("equity") or self.cfg.initial_equity)
            entry_ok = self.killswitch.allow_entry()

            # 1) 열린 포지션 관리 — 새로 확정된 봉만 본다
            if state.get("position"):
                action, closed = self._manage(state, candles, tf_ms)
                if closed:
                    events.append(f"청산 {closed['reason']} {closed['r']:+.2f}R")
                    entry_ok = self.killswitch.allow_entry()

            # 2) 새 신호 — 마지막 확정 봉에 M1 후보가 섰는가
            params, cfg = self.rule()
            if signals is None:
                engine = Engine(params)
                signals = engine.run(candles)
                capture = engine.capture
            else:
                capture = {}
            signal, verdict, action, note = self._maybe_enter(state, candles, signals, capture,
                                                              cfg, next_open, now_ms, entry_ok, action)
            if note:
                events.append(note)

            state["scan_bar"] = int(last["t"])
            state["scan_count"] = int(state.get("scan_count") or 0) + 1
            state["killswitch"] = self._killswitch_summary()
            self._recompute(state)
            try:
                from . import regime as regime_mod
                regime_label = regime_mod.classify(regime_features(candles, len(candles) - 1))
            except Exception:
                regime_label = None
            record = {
                "ts": self.now().isoformat(timespec="seconds"),
                "bar": _iso(last["t"]), "bar_kst": _kst(last["t"]),
                "price": last["c"], "bars": len(candles), "source": source,
                "equity": state.get("equity"),
                "action": action, "events": events,
                "position": self._position_view(state),
                "stats": state["stats"],
                "signal": None if not signal else {
                    "time": _iso(signal["time"]), "side": signal["side"], "kind": signal["kind"],
                    "label": signal.get("label"), "entry": signal["entry"], "stop": signal["stop"],
                    "target": signal["target"], "reasons": signal.get("reasons")},
                "filter": verdict,
                "regime": regime_label,
                "killswitch": {"ok": entry_ok.get("ok"), "reason": entry_ok.get("reason")},
            }
            state["last_scan"] = {k: record[k] for k in ("ts", "bar", "action", "events", "price")}
            self.save_state(state)
            self._append_log(record)
            return record

    # ------------------------------------------------------------------ 진입
    def _maybe_enter(self, state: Dict, candles: List[Dict], signals: Sequence[Dict], capture: Dict,
                     cfg: BacktestConfig, next_open: Optional[float], now_ms: int,
                     entry_ok: Dict, action: str):
        """마지막 확정 봉의 후보 하나를 조건(세션·킬스위치·AI)에 걸어 모의 진입한다."""
        last = candles[-1]
        tf_ms = INTERVAL_MS.get(str(self.cfg.interval), 1_800_000)
        bar_signals = [s for s in signals if int(s["index"]) == len(candles) - 1
                       and s.get("kind") in ("M1",)]
        if not bar_signals:
            return None, None, action, None
        sig = bar_signals[0]
        key = f"{int(sig['time'])}|{sig['side']}"
        seen = state.setdefault("signals_seen", [])
        if key in seen:
            return None, None, action, None
        seen.append(key)
        del seen[:-200]                                   # 최근 200개만 남긴다

        entry_bar_time = int(last["t"]) + tf_ms
        if not in_session(entry_bar_time, session_hours(cfg)):
            return sig, None, "SKIP", f"세션 밖 진입({self.cfg.session}) — 후보는 기록"
        held = duplicate_entry(state, self.cfg.symbol, sig["side"])
        if held:
            return sig, None, "SKIP", held + " — 후보는 기록"
        if state.get("position"):
            return sig, None, "SKIP", "이미 포지션 보유 중 — 후보는 기록"
        if not entry_ok.get("ok"):
            return sig, None, "SKIP", f"킬스위치: {entry_ok.get('reason')}"
        if now_ms - entry_bar_time >= tf_ms:
            return sig, None, "SKIP", "진입 시점(다음 봉)이 이미 지나 평가하지 않음"
        atr = float(sig.get("atr") or 0.0)
        price = next_open if next_open else float(last["c"])
        if atr <= 0 or price <= 0:
            return sig, None, "SKIP", "ATR·가격이 없어 진입할 수 없음"
        if self.killswitch.check_volatility(atr, price):
            return sig, None, "SKIP", "킬스위치: 급변(ATR/가격)"

        # 시장 국면 가드 — 기본은 꺼짐(정보로만 남긴다). 켰을 때만 진입을 막는다.
        ser = prepare_series(candles)
        reg_feat = regime_features(candles, int(sig["index"]), ser)
        guard = regime_guard(sig.get("side"), reg_feat, self._regime_guard_cfg())
        if not guard.get("ok"):
            return sig, None, "SKIP", guard.get("reason") or "국면 가드"

        # AI 맥락 필터 — 판정만 받는다(사이즈·손절·목표는 규칙 그대로)
        ai = self._filter()
        if ai is None:
            verdict = {"decision": "PASS", "loss_prob": None, "reason": "AI 필터 비활성 — 규칙만으로 기록",
                       "risk_flags": [], "cached": False, "error": None}
        else:
            ai.cfg.symbol = self.cfg.symbol
            extra = self._ai_extra(candles, state, sig, ser)
            context = build_context(candles, sig, capture.get(sig["index"]),
                                    ser, ai.cfg, trade_cfg=cfg,
                                    symbol=self.cfg.symbol, extra=extra)
            verdict = ai.review(sig, context)
        if verdict.get("decision") != "PASS":
            return sig, verdict, "REJECT", f"AI REJECT(loss_prob {verdict.get('loss_prob')}): " \
                                           f"{verdict.get('reason')}"

        side = sig["side"]
        slip, fee = _bps(self.cfg.slippage_bps), _bps(self.cfg.fee_bps)
        entry = price * (1 + slip) if side == "LONG" else price * (1 - slip)
        dist = self.cfg.stop_atr * atr
        stop = entry - dist if side == "LONG" else entry + dist
        target = entry + dist * self.cfg.target_r if side == "LONG" else entry - dist * self.cfg.target_r
        if (side == "LONG" and entry <= stop) or (side == "SHORT" and entry >= stop):
            return sig, verdict, "SKIP", "진입가가 손절을 지나 체결 규칙이 무의미"
        if (side == "LONG" and entry >= target) or (side == "SHORT" and entry <= target):
            return sig, verdict, "SKIP", "진입가가 이미 목표를 지나 체결 규칙이 무의미"

        equity = float(state.get("equity") or self.cfg.initial_equity)
        risk_usd = equity * self.cfg.risk_pct / 100
        risk_per_unit = abs(entry - stop)
        qty = risk_usd / risk_per_unit
        if qty * entry > equity * self.cfg.max_leverage:
            qty = equity * self.cfg.max_leverage / entry
            risk_usd = qty * risk_per_unit
        state["position"] = {
            "side": side, "entry": round(entry, 4), "stop": round(stop, 4), "target": round(target, 4),
            "initial_stop": round(stop, 4), "qty": round(qty, 8), "risk_usd": round(risk_usd, 2),
            "risk": round(risk_per_unit, 4), "atr": atr,
            "signal_time": int(sig["time"]), "entry_time": self.now().isoformat(timespec="seconds"),
            "entry_bar_time": entry_bar_time, "last_bar_time": None, "bars": 0,
            "ai": verdict, "reasons": sig.get("reasons") or [],
        }
        return sig, verdict, "OPEN", (f"모의 {'롱' if side == 'LONG' else '숏'} 진입 {entry:,.2f} · "
                                      f"손절 {stop:,.2f} · 목표 {target:,.2f} ({self.cfg.target_r:g}R) · "
                                      f"리스크 {risk_usd:,.2f} USDT")

    # ------------------------------------------------------------------ AI 문맥
    def _ai_extra(self, candles: List[Dict], state: Dict, sig: Dict, ser: Optional[Dict] = None) -> Dict:
        """AI 심사에 함께 넣는 '내 책·교훈·국면' — 값이 없으면 키를 비워 보내지 않는다."""
        extra: Dict = {}
        try:
            ks = self.killswitch.allow_entry()
            extra["내 계좌"] = portfolio_context(state, ks, symbol=self.cfg.symbol)
        except Exception:
            pass
        try:
            lines = self.memory.prompt_lines(5, as_of_ms=int(sig.get("time") or 0) or None)
            if lines:
                extra["최근 복기 교훈"] = lines
        except Exception:
            pass
        try:
            from . import regime as regime_mod
            ser = ser or regime_series(candles)
            extra["시장 국면"] = regime_mod.summary(
                candles, int(sig["index"]), ser,
                global_json=regime_mod.load_json(regime_mod.DATA / "global.json"),
                calendar=regime_mod.load_json(regime_mod.DATA / "calendar.json"),
                now_ms=int(candles[int(sig["index"])]["t"]))
        except Exception:
            pass
        return extra

    # ------------------------------------------------------------------ 청산
    def _manage(self, state: Dict, candles: List[Dict], tf_ms: int):
        """열린 포지션을 새로 확정된 봉들에 대고 판정한다(손절 우선·시간청산)."""
        pos = state["position"]
        side = pos["side"]
        slip, fee = _bps(self.cfg.slippage_bps), _bps(self.cfg.fee_bps)
        action = "HOLD"
        for bar in candles:
            t = int(bar["t"])
            if t < pos["entry_bar_time"]:
                continue
            if pos.get("last_bar_time") is not None and t <= pos["last_bar_time"]:
                continue
            pos["bars"] = int(pos.get("bars") or 0) + 1
            pos["last_bar_time"] = t
            hit_stop = (bar["l"] <= pos["stop"]) if side == "LONG" else (bar["h"] >= pos["stop"])
            hit_target = (bar["h"] >= pos["target"]) if side == "LONG" else (bar["l"] <= pos["target"])
            reason = price = None
            if hit_stop:                                  # 같은 봉에서 둘 다 닿으면 손절 우선(비관)
                reason, price = "STOP", pos["stop"]
            elif hit_target:
                reason, price = "TARGET", pos["target"]
            elif pos["bars"] >= self.cfg.max_bars:
                reason, price = "TIME", bar["c"]
            if reason is None:
                continue
            fill = price * (1 - slip) if side == "LONG" else price * (1 + slip)
            gross = ((fill - pos["entry"]) if side == "LONG" else (pos["entry"] - fill)) * pos["qty"]
            fees = (pos["entry"] + fill) * pos["qty"] * fee + price * pos["qty"] * slip
            pnl = gross - fees
            equity = float(state.get("equity") or self.cfg.initial_equity) + pnl
            trade = {
                "side": side, "entry": pos["entry"], "exit": round(fill, 4), "stop": pos["stop"],
                "target": pos["target"], "qty": pos["qty"], "risk_usd": pos["risk_usd"],
                "reason": reason, "pnl": round(pnl, 2), "equity": round(equity, 2),
                "r": round(pnl / pos["risk_usd"], 3) if pos["risk_usd"] else 0.0,
                "bars": pos["bars"], "signal_time": pos["signal_time"],
                "entry_time": pos["entry_time"], "exit_time": self.now().isoformat(timespec="seconds"),
                "exit_bar_time": t, "ai_decision": (pos.get("ai") or {}).get("decision"),
                "ai_reason": (pos.get("ai") or {}).get("reason"),
            }
            state["closed"].append(trade)
            state["equity"] = round(equity, 2)
            state["position"] = None
            try:
                trade["symbol"] = self.cfg.symbol
                rec = self.memory.record(trade, symbol=self.cfg.symbol)   # 복기 메모리(P0-2)
                trade["lesson"] = f"{rec.get('reason_ko')} {rec.get('r')}R → data/memory.md"
            except Exception as exc:              # 메모리를 못 써도 청산 기록은 남는다
                trade["lesson"] = None
                print(f"[paper] 복기 메모리 기록 실패: {exc}", flush=True)
            self.killswitch.record_trade(trade["pnl"], equity)
            return "CLOSE", trade
        return action, None

    # ------------------------------------------------------------------ 조회
    def _position_view(self, state: Dict) -> Optional[Dict]:
        pos = state.get("position")
        if not pos:
            return None
        return {
            "side": pos["side"], "entry": pos["entry"], "stop": pos["stop"], "target": pos["target"],
            "qty": pos["qty"], "risk_usd": pos["risk_usd"], "bars": pos.get("bars"),
            "entry_at": pos.get("entry_time"), "entry_bar": _iso(pos.get("entry_bar_time")),
            "ai": (pos.get("ai") or {}).get("decision"),
        }

    def _killswitch_summary(self) -> Dict:
        status = self.killswitch.status()
        st = status["state"]
        return {"ok": status["entry"]["ok"], "reason": status["entry"]["reason"],
                "tripped": st.get("tripped"), "tripped_until": st.get("tripped_until"),
                "manual_stop": st.get("manual_stop"), "realized_pnl": st.get("realized_pnl"),
                "trades_today": st.get("trades"), "loss_pct": status.get("loss_pct"),
                "rules": status["rules"]}

    def status(self) -> Dict:
        state = self.load_state()
        try:
            lessons = self.memory.prompt_lines(5)
        except Exception:
            lessons = []
        return {"ok": True, "state": state, "killswitch": self._killswitch_summary(),
                "rule": state["rule"], "settings": self.cfg.to_dict(),
                "lessons": lessons, "log": self.tail_log()}

    def reset(self) -> Dict:
        state = self._blank_state()
        self._recompute(state)
        self.save_state(state)
        self.killswitch.reset()
        try:
            self.memory.reset()          # 계좌를 새로 시작하면 복기 메모리도 같이 지운다
        except Exception:
            pass
        return state


# ---------------------------------------------------------------------- 외부용 함수

def paper_status(cfg: Optional[PaperConfig] = None) -> Dict:
    """/api/paper — 현재 상태(포지션·통계·킬스위치)와 최근 로그."""
    return PaperLoop(cfg).status()


def paper_tick(cfg: Optional[PaperConfig] = None) -> Dict:
    """/api/paper/tick — 한 번 스캔한다(주문 없음)."""
    return PaperLoop(cfg).tick()


# ---------------------------------------------------------------------- CLI

def _print_record(rec: Dict) -> None:
    st = rec["stats"]
    print(f"[paper] {rec['ts']} · 봉 {rec['bar']} · {rec['action']} · {rec['price']:,.2f}"
          + (f" · {' | '.join(rec['events'])}" if rec["events"] else ""), flush=True)
    print(f"        체결 {st['trades']}건 · 승률 {st['win_rate_pct']}% · PF {st['profit_factor']} · "
          f"순손익 {st['net_pnl']:+.2f} USDT({st['net_pct']:+.2f}%) · 평균 R {st['avg_r']} · "
          f"MDD {st['mdd_pct']}% · 자본 {rec.get('equity'):,.2f} USDT", flush=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="후보1 실시간 모의매매 루프(주문 없음)")
    ap.add_argument("--once", action="store_true", help="한 번만 스캔(스케줄러 연동용)")
    ap.add_argument("--loop", action="store_true", help="지정 간격으로 계속")
    ap.add_argument("--interval", type=int, default=300, help="--loop 반복 간격(초)")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--source", default="auto", choices=["auto", "bybit", "okx", "bitget", "binance"])
    ap.add_argument("--limit", type=int, default=3000, help="받을 봉 수")
    ap.add_argument("--no-ai", action="store_true", help="AI 필터 없이 규칙만 기록")
    ap.add_argument("--state", default=None, help="상태 파일 경로")
    ap.add_argument("--log", default=None, help="로그 파일 경로")
    ap.add_argument("--killswitch", default=None, help="킬스위치 상태 파일 경로")
    ap.add_argument("--memory", default=None, help="복기 메모리 파일 경로(기본 data/paper_memory.jsonl)")
    ap.add_argument("--regime-guard", default="off", choices=["off", "dead", "panic", "dead+panic"],
                    help="국면 가드(기본 off — 백테스트 근거가 약해 스스로 막지 않는다)")
    ap.add_argument("--status", action="store_true", help="현재 상태만 출력")
    ap.add_argument("--reset", action="store_true", help="모의 계좌 초기화")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    cfg = PaperConfig(symbol=args.symbol, source=args.source, limit=args.limit,
                      state_path=args.state or "", log_path=args.log or "",
                      killswitch_path=args.killswitch or "", memory_path=args.memory or "",
                      regime_guard=args.regime_guard)
    loop = PaperLoop(cfg, ai=False if args.no_ai else None)

    if args.status:
        print(json.dumps(loop.status(), ensure_ascii=False, indent=2))
        return 0
    if args.reset:
        loop.reset()
        print("[paper] 모의 계좌를 초기화했습니다.")
        return 0

    def once() -> int:
        try:
            rec = loop.tick()
        except Exception as exc:                     # 시세·파일 실패는 한 번의 스캔 실패로 남긴다
            print(f"[paper] 스캔 실패: {exc}", flush=True)
            return 1
        if not args.quiet:
            _print_record(rec)
        return 0

    if args.loop:
        if not args.quiet:
            print(f"[paper] {cfg.symbol} {cfg.interval}분봉 모의매매 루프 — {args.interval}초 간격 · 주문 없음",
                  flush=True)
        while True:
            once()
            time.sleep(max(10, args.interval))
    return once()


if __name__ == "__main__":
    raise SystemExit(main())
