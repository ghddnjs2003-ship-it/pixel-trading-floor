"""킬스위치 — 실전 자동매매가 스스로 멈춰야 하는 조건을 한 곳에 모은 것.

왜 필요한가
- 룰 엔진(backend/strategy.py)은 신호를 낸다. 그러나 신호가 맞아도 계좌가 크게 다치면
  그날의 판단력·자본이 남지 않는다. 그래서 '무엇을 사고팔지'와 분리된 '오늘은 아예 하지 않는다'를 둔다.

감시하는 것
1) 일일 최대 손실 — 오늘 실현 손익이 계좌의 daily_loss_pct%를 넘으면 당일 중단
2) 연속 손절     — 연속 max_consecutive_losses번 손절하면 당일 중단
3) 일일 거래 수   — max_daily_trades를 넘으면 당일 중단(과매매 방지)
4) 급변 감지     — 최근 봉들이 volatility_pause_atr_pct% 넘게 움직이면 cooldown_minutes 동안 일시 중단
5) 수동 정지     — 사용자가 멈추면 직접 풀기 전까지 진입 금지

상태
- state_path(기본 backend/state/killswitch.json)에 남긴다. 날짜가 바뀌면(KST 기준) 자동 초기화.
- 파일이 없거나 깨졌으면 새 상태로 시작하고 그 사실을 status()에 남긴다(조용히 초기화하지 않음).

이 모듈은 주문을 내지 않는다. 진입 가능 여부(allow_entry)만 답한다.
"""

import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

KST = timezone(timedelta(hours=9))


@dataclass
class KillSwitchRules:
    daily_loss_pct: float = 3.0            # 오늘 손실이 계좌의 3%면 중단
    max_consecutive_losses: int = 3        # 3연속 손절이면 중단
    max_daily_trades: int = 20             # 하루 20회 초과 중단
    volatility_pause_atr_pct: float = 2.5  # 최근 봉 ATR이 가격의 2.5%를 넘으면
    cooldown_minutes: int = 30             # 30분 쉼
    min_equity_pct: float = 90.0           # 시작 자본의 90% 아래로 내려가면 무기한 중단(사람 확인 필요)

    def to_dict(self) -> Dict:
        return asdict(self)


def _now() -> datetime:
    return datetime.now(KST)


class KillSwitch:
    def __init__(self, rules: Optional[KillSwitchRules] = None,
                 state_path: Optional[str] = None, now=None):
        self.rules = rules or KillSwitchRules()
        self.state_path = state_path
        self._now = now or _now
        self.note = ""            # _load가 남기는 안내를 덮어쓰지 않도록 먼저 초기화한다
        self.state = self._load()

    # ------------------------------------------------------------------ 상태

    def _blank(self) -> Dict:
        return {
            "date": self._now().date().isoformat(),
            "start_equity": None,
            "equity": None,
            "realized_pnl": 0.0,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "consecutive_losses": 0,
            "tripped": False,
            "trip_reason": "",
            "tripped_until": None,
            "manual_stop": False,
            "last_update": self._now().isoformat(timespec="seconds"),
        }

    def _load(self) -> Dict:
        state = self._blank()
        if not self.state_path or not os.path.isfile(self.state_path):
            return state
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                saved = json.load(fh)
            if isinstance(saved, dict):
                state.update({k: v for k, v in saved.items() if k in state})
            else:
                self.note = "상태 파일 형식이 달라 새로 시작했습니다"
        except (OSError, json.JSONDecodeError):
            self.note = "상태 파일을 읽지 못해 새로 시작했습니다"
        return state

    def save(self) -> None:
        self.state["last_update"] = self._now().isoformat(timespec="seconds")
        if not self.state_path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.state_path)), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)

    def _roll_if_new_day(self) -> None:
        today = self._now().date().isoformat()
        if self.state.get("date") != today:
            keep = {k: self.state.get(k) for k in ("manual_stop", "start_equity")}
            self.state = self._blank()
            if keep.get("manual_stop"):
                self.state["manual_stop"] = True
            if keep.get("equity") is not None:
                self.state["start_equity"] = keep.get("equity")
                self.state["equity"] = keep.get("equity")
            self.save()

    # ------------------------------------------------------------------ 이벤트

    def start_day(self, equity: float) -> None:
        """그날 첫 체크. 그날의 시작 자본을 기록한다(없을 때만)."""
        self._roll_if_new_day()
        self.state["equity"] = equity
        if self.state.get("start_equity") is None:
            self.state["start_equity"] = equity
        self.save()

    def record_trade(self, pnl: float, equity: Optional[float] = None) -> Dict:
        """청산 1건 반영. 손실이면 연속 손절 수가 늘고, 규칙을 넘으면 그 자리에서 멈춘다."""
        self._roll_if_new_day()
        self.state["trades"] += 1
        self.state["realized_pnl"] = round(self.state["realized_pnl"] + pnl, 2)
        if equity is not None:
            self.state["equity"] = equity
        if pnl < 0:
            self.state["losses"] += 1
            self.state["consecutive_losses"] += 1
        elif pnl > 0:
            self.state["wins"] += 1
            self.state["consecutive_losses"] = 0

        r = self.rules
        if r.max_consecutive_losses and self.state["consecutive_losses"] >= r.max_consecutive_losses:
            self.trip(f"연속 {self.state['consecutive_losses']}회 손절")
        elif r.max_daily_trades and self.state["trades"] >= r.max_daily_trades:
            self.trip(f"당일 거래 {self.state['trades']}회 도달")
        else:
            loss_pct = self._loss_pct()
            if r.daily_loss_pct and loss_pct is not None and loss_pct >= r.daily_loss_pct:
                self.trip(f"당일 손실 {loss_pct:.2f}% (기준 {r.daily_loss_pct}%)")
            elif r.min_equity_pct and self.state.get("start_equity") and self.state.get("equity"):
                keep = self.state["equity"] / self.state["start_equity"] * 100
                if keep <= r.min_equity_pct:
                    self.trip(f"자본 {keep:.1f}%까지 하락 — 사람 확인 전까지 중단")
        self.save()
        return self.status()

    def _loss_pct(self) -> Optional[float]:
        start = self.state.get("start_equity")
        if not start:
            return None
        return max(0.0, -self.state["realized_pnl"] / start * 100)

    def trip(self, reason: str, minutes: Optional[int] = None) -> None:
        self.state["tripped"] = True
        self.state["trip_reason"] = reason
        until = self._now() + timedelta(minutes=minutes if minutes is not None else self.rules.cooldown_minutes)
        self.state["tripped_until"] = until.isoformat(timespec="seconds")

    def check_volatility(self, atr: Optional[float], price: Optional[float]) -> bool:
        """급변 감지 — ATR/가격이 기준을 넘으면 일시 중단. 넘으면 True."""
        r = self.rules
        if not atr or not price or price <= 0 or not r.volatility_pause_atr_pct:
            return False
        pct = atr / price * 100
        if pct >= r.volatility_pause_atr_pct:
            self.trip(f"급변 — ATR {pct:.2f}% (기준 {r.volatility_pause_atr_pct}%)", minutes=r.cooldown_minutes)
            self.save()
            return True
        return False

    def manual_stop(self, on: bool = True, reason: str = "사용자 정지") -> Dict:
        self.state["manual_stop"] = bool(on)
        if on:
            self.state["trip_reason"] = reason
            self.state["tripped"] = True
            self.state["tripped_until"] = None      # 직접 풀기 전까지
        else:
            self.state["tripped"] = False
            self.state["trip_reason"] = ""
            self.state["tripped_until"] = None
        self.save()
        return self.status()

    def reset(self) -> Dict:
        """사람이 확인하고 다시 켤 때. 그날의 손실·연속 손절만 지운다(수동 정지는 유지)."""
        keep_manual = self.state.get("manual_stop", False)
        equity = self.state.get("equity")
        self.state = self._blank()
        self.state["manual_stop"] = keep_manual
        if equity is not None:
            self.state["start_equity"] = equity
            self.state["equity"] = equity
        self.save()
        return self.status()

    # ------------------------------------------------------------------ 판단

    def allow_entry(self) -> Dict:
        """지금 진입해도 되는가 — (ok, 사유, 남은 시간)."""
        self._roll_if_new_day()
        if self.state.get("manual_stop"):
            return {"ok": False, "reason": "수동 정지 중", "until": None}
        if self.state.get("tripped"):
            until = self.state.get("tripped_until")
            if until:
                try:
                    until_dt = datetime.fromisoformat(until)
                except ValueError:
                    until_dt = None
                if until_dt and self._now() < until_dt:
                    left = int((until_dt - self._now()).total_seconds() // 60)
                    return {"ok": False, "reason": self.state.get("trip_reason", "중단"), "until": until,
                            "minutes_left": max(1, left)}
                self.state["tripped"] = False
                self.state["trip_reason"] = ""
                self.state["tripped_until"] = None
                self.state["consecutive_losses"] = 0
                self.save()
            else:
                return {"ok": False, "reason": self.state.get("trip_reason", "중단"), "until": None}
        return {"ok": True, "reason": "정상", "until": None}

    def status(self) -> Dict:
        return {
            "rules": self.rules.to_dict(),
            "state": dict(self.state),
            "loss_pct": None if self._loss_pct() is None else round(self._loss_pct(), 2),
            "entry": self.allow_entry(),
            "note": self.note,
        }
