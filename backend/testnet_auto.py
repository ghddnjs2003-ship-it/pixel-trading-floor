"""테스트넷 오토 모드 — 두뇌(후보1 + AI 심사)와 손(바이비트 테스트넷 주문)을 실제로 잇는 실행 모듈.

무엇을 하나
1) 30분봉을 받아 **확정 봉만** 쓰고(진행 중 봉은 버린다), 마지막 확정 봉에 후보1(M1)이 섰는지 본다.
2) 세션(아시아) → 킬스위치 → AI 맥락 필터(PASS/REJECT) 순으로 통과시킨다. 판정·사유는 로그에 남긴다.
3) 통과하면 다음 봉 시가를 기준으로 3ATR 손절·4R 목표를 계산해 **바이비트 테스트넷에 시장가 주문**을 낸다.
   수량·명목가·손실 예산은 `bybit_client`의 50달러 규칙이 정한다(여기서 따로 계산하지 않는다).
4) 열린 포지션은 거래소에 걸어 둔 TP/SL이 닫는다. 이 모듈은 ① 포지션이 아직 있는지 ② 96봉 시간 청산인지
   만 보고, 청산이 확인되면 거래소 청산 기록(`/v5/position/closed-pnl`)으로 손익을 남긴다.
   mock 모드에는 거래소가 없으므로 확정 봉의 고가·저가로 손절·목표·시간 청산을 흉내 낸다.

지키는 선
- **테스트넷 전용**: 주소는 `https://api-testnet.bybit.com` 고정, 메인넷 거부, 시드 상한 50 USDT,
  1회 손실 2~4%, 레버리지 5배 상한 — 모두 `bybit_client`가 강제한다. 이 모듈은 그 클라이언트만 쓴다.
- **모의매매와 분리**: 서버에서 30분마다 도는 `paper_loop`의 `data/paper_state.json`·`paper_log.jsonl`·
  `paper_killswitch.json`은 읽지도 쓰지도 않는다. 이 모듈의 상태는 `data/testnet_auto_*.json`뿐이고,
  경로가 paper 파일을 가리키면 만들 때부터 거부한다(`_assert_paths_safe`).
- **mock 상태 분리**: 키가 없거나 `--mock`이면 `*.mock.json`을 쓴다. mock에서 잡은 가짜 포지션이
  나중에 실제 테스트넷 진입을 막는 일이 없게 하려는 것이다.
- **중복 진입 금지**: 내 기록에 포지션이 있거나, 거래소에 포지션이 있으면(수동 진입 포함) 신규 진입을 건너뛴다.
  거래소 포지션이 내 기록과 다르면(수동 변경 의심) 손대지 않고 로그만 남긴다 — 남의 포지션을 닫지 않는다.
- **동시 실행 금지**: 같은 상태 파일에 두 프로세스가 붙으면 같은 봉에 주문이 두 번 나갈 수 있다.
  시작할 때 `*.lock`을 잡아 두 번째를 멈춘다(2시간 지난 잠금은 꺼진 프로세스의 흔적으로 보고 넘어간다).
- **결과 불확실은 재시도하지 않는다**: 주문 전송이 오류로 끝나면 같은 스캔에서 다시 보내지 않고,
  다음 스캔에서 거래소 포지션을 보고 판단한다(중복 주문 방지).

실행
    python -m backend.testnet_auto --once                 # 1회 점검·판단(키 없으면 mock)
    python -m backend.testnet_auto --once --mock          # 키가 있어도 mock 드라이런
    python -m backend.testnet_auto --loop                 # 30분 봉 마감 +90초마다 계속(Ctrl+C 종료)
    python -m backend.testnet_auto --status               # 계좌·포지션·킬스위치·최근 기록
    python -m backend.testnet_auto --stop / --resume      # 신규 진입 정지 / 해제(돌고 있는 루프에도 먹힘)
    python -m backend.testnet_auto --once --candles data/btc_15m_3600.json   # 오프라인 재생(주문 없음)
    run_testnet_auto.bat                                  # Windows: 위 --loop을 더블클릭으로
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .ai_filter import AIFilter, build_context, nominee_setup, prepare_series
from .backtest import BacktestConfig, INTERVAL_MS, in_session, load_candles, session_hours
from .bybit_client import (BybitAPIError, BybitClient, BybitConfigError, BybitError,
                           OrderRejected, risk_limits)
from .config import ai_filter_enabled, get_ai_filter_config, get_killswitch_rules
from .risk import KillSwitch
from .strategy import Engine, Params

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
KST = timezone(timedelta(hours=9))

BAR_DELAY_SEC = 90          # 봉 마감 뒤 이만큼 기다렸다가 다음 봉을 '확정'으로 본다(거래소 지연 여유)
MIN_CANDLES = 60            # 신호를 세우려면 최소 이만큼의 확정 봉이 필요하다(load_candles와 같은 하한)
MAX_SEEN = 500              # 같은 신호를 두 번 처리하지 않기 위해 남기는 기억(최근 N개)

# 이 모듈이 절대 건드리면 안 되는 파일 이름 — 서버 30분 모의매매가 쓰는 것들
PAPER_NAMES = ("paper_state.json", "paper_log.jsonl", "paper_killswitch.json",
               "paper_memory.jsonl", "memory.md")


# ---------------------------------------------------------------------- 설정

@dataclass
class AutoConfig:
    """테스트넷 오토 모드 설정. 규칙은 후보1 기본값, 파일 경로만 테스트에서 바꿀 수 있게 열어 둔다."""

    symbol: str = "BTCUSDT"
    interval: str = "30"
    limit: int = 3000
    source: str = "auto"
    loss_pct: float = 3.0            # 1회 손실 예산(기준자본의 %) — bybit_client가 2~4%만 허용
    leverage: float = 3.0            # 레버리지 — bybit_client가 1~5배만 허용
    target_r: float = 4.0            # 목표 = 손절 거리 × 4
    stop_atr: float = 3.0            # 손절 = ATR × 3
    max_bars: int = 96               # 96봉(48시간) 시간 청산
    entry_window_sec: int = 300      # 다음 봉이 시작된 뒤 이 시간 안에만 진입(늦은 추격 금지)
    session: str = "asia"            # 진입 봉이 아시아 세션(00-08 UTC) 안일 때만
    time_exit: bool = True           # 시간 청산 사용
    data_dir: str = str(DATA)
    state_path: str = ""             # 비우면 data_dir 아래 모드별 기본 경로
    log_path: str = ""
    killswitch_path: str = ""
    replay: bool = False             # 과거 캔들 재생(현재가 확인 생략)

    def to_dict(self) -> Dict:
        from dataclasses import asdict
        return asdict(self)


def _iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def _kst(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=KST).strftime("%Y-%m-%d %H:%M")


def next_bar_close_ms(now_ms: int, tf_ms: int, delay_sec: int = BAR_DELAY_SEC) -> int:
    """다음 봉이 마감되고 delay_sec만큼 지난 시각(ms). --loop가 이 시각에 맞춰 돈다."""
    return ((int(now_ms) // int(tf_ms)) + 1) * int(tf_ms) + int(delay_sec * 1000)


# ---------------------------------------------------------------------- 두뇌 + 손

class TestnetAuto:
    """한 번 만들고 tick()을 반복해서 부른다. 상태 파일이 있으면 이어서 돈다(재시작 안전)."""

    __test__ = False          # pytest가 'Test'로 시작하는 이름을 테스트 클래스로 오해하지 않게

    def __init__(self, cfg: Optional[AutoConfig] = None, client: Optional[BybitClient] = None,
                 ai=None, fetch=None, killswitch: Optional[KillSwitch] = None, now=None):
        self.cfg = cfg or AutoConfig()
        self.client = client if client is not None else BybitClient()
        if not getattr(self.client, "testnet", False):
            raise BybitConfigError("테스트넷 클라이언트만 씁니다 — BybitClient의 testnet 가드가 꺼져 있습니다")
        self.mock = bool(self.client.is_mock)
        self.ai = ai                     # None = 설정에서 만든다, False = AI 없이 규칙만
        self.fetch = fetch or self._fetch_default
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.state_path, self.log_path, self.killswitch_path, self.pause_path = self._resolve_paths()
        self._assert_paths_safe()
        self.killswitch = killswitch or KillSwitch(get_killswitch_rules(), state_path=self.killswitch_path)
        self._ai_filter = None

    # ------------------------------------------------------------------ 경로
    def _resolve_paths(self):
        base = Path(self.cfg.data_dir)
        suffix = ".mock" if self.mock else ""
        state = self.cfg.state_path or str(base / f"testnet_auto_state{suffix}.json")
        log = self.cfg.log_path or str(base / f"testnet_auto_log{suffix}.jsonl")
        ks = self.cfg.killswitch_path or str(base / f"testnet_auto_killswitch{suffix}.json")
        pause = str(Path(state).with_name(f"testnet_auto_pause{suffix}.txt"))
        return state, log, ks, pause

    def _assert_paths_safe(self) -> None:
        """모의매매(paper_*) 파일을 실수로 덮어쓰지 않도록 이름을 확인한다."""
        for path in (self.state_path, self.log_path, self.killswitch_path or ""):
            if not path:
                continue
            name = Path(path).name
            if name in PAPER_NAMES or name.startswith("paper_"):
                raise ValueError(
                    f"{name}은(는) 서버 모의매매가 쓰는 파일입니다 — 테스트넷 오토 모드는 "
                    "testnet_auto_*.json 경로만 씁니다")

    # ------------------------------------------------------------------ 규칙
    def rule(self):
        """후보1 규칙 — 손절 폭·목표 R·시간 청산만 설정값으로 덮어쓴다."""
        params, cfg = nominee_setup()
        params = Params(**{**params.to_dict(), "stop_mode": "atr", "stop_atr": self.cfg.stop_atr})
        cfg = BacktestConfig(**{**cfg.to_dict(), "max_bars": self.cfg.max_bars,
                                "target_r": self.cfg.target_r, "session_filter": self.cfg.session})
        return params, cfg

    def _tf_ms(self) -> int:
        return INTERVAL_MS.get(str(self.cfg.interval), 1_800_000)

    def paused(self) -> bool:
        """사람이 --stop으로 세운 정지 표시. 돌고 있는 루프도 다음 스캔에서 바로 본다.

        킬스위치 파일(manual_stop)은 프로세스가 메모리에 들고 있어, 밖에서 --stop으로 켜면
        다음 저장에 덮여 사라진다. 그래서 정지는 파일 하나로 따로 둔다.
        """
        return bool(self.pause_path) and os.path.isfile(self.pause_path)

    def _fetch_default(self) -> Dict:
        return load_candles(self.cfg.symbol, str(self.cfg.interval), self.cfg.limit, self.cfg.source)

    # ------------------------------------------------------------------ AI
    def _filter(self):
        """설정이 켜져 있으면 AI 필터를 만든다. 키가 없거나 실패하면 None(규칙만 돈다).

        심사 캐시만 따로 둔다 — 서버 모의매매(data/ai_cache.json)와 같은 파일을 동시에 쓰지 않으려는 것.
        """
        if self.ai is False:
            return None
        if self.ai is not None:
            return self.ai
        if self._ai_filter is None:
            try:
                if not ai_filter_enabled():
                    self._ai_filter = False
                else:
                    conf = get_ai_filter_config()
                    if conf.cache_path and Path(conf.cache_path).name == "ai_cache.json":
                        suffix = ".mock" if self.mock else ""
                        conf.cache_path = str(Path(self.state_path).parent / f"ai_cache_testnet{suffix}.json")
                    self._ai_filter = AIFilter(conf)
            except Exception as exc:                    # 필터가 안 서도 오토 모드는 계속
                print(f"[testnet-auto] AI 필터 비활성: {exc}", flush=True)
                self._ai_filter = False
        return self._ai_filter or None

    def _ai_extra(self, candles: List[Dict], sig: Dict, ser: Optional[Dict],
                  account: Dict, positions: Sequence[Dict]) -> Dict:
        """AI 심사에 함께 넣는 '테스트넷 계좌·시장 국면'. 값이 없으면 키를 비워 보내지 않는다."""
        extra: Dict = {
            "테스트넷 계좌": {
                "모드": "mock" if self.mock else "testnet",
                "잔고USDT": round(float(account.get("equity") or 0.0), 2),
                "리스크기준USD": account.get("riskBaseUsd"),
                "열린포지션": len(positions),
                "1회손실예산USD": round(float(account.get("riskBaseUsd") or 0.0)
                                      * float(self.cfg.loss_pct) / 100.0, 2),
            }
        }
        try:
            from . import regime as regime_mod
            ser = ser or regime_mod.series(candles)
            extra["시장 국면"] = regime_mod.summary(
                candles, int(sig["index"]), ser,
                global_json=regime_mod.load_json(regime_mod.DATA / "global.json"),
                calendar=regime_mod.load_json(regime_mod.DATA / "calendar.json"),
                now_ms=int(candles[int(sig["index"])]["t"]))
        except Exception:
            pass
        return extra

    # ------------------------------------------------------------------ 상태 파일
    def _blank_state(self) -> Dict:
        return {
            "version": 1, "mode": "mock" if self.mock else "testnet",
            "symbol": self.cfg.symbol, "interval": int(self.cfg.interval),
            "rule": {"name": "후보1", "kinds": ["M1"], "session": self.cfg.session,
                     "stop_atr": self.cfg.stop_atr, "target_r": self.cfg.target_r,
                     "max_bars": self.cfg.max_bars, "loss_pct": self.cfg.loss_pct,
                     "leverage": self.cfg.leverage, "testnet_only": True},
            "created_at": self.now().isoformat(timespec="seconds"), "updated_at": None,
            "scan_count": 0, "scan_bar": None, "signals_seen": [],
            "position": None, "exchange_position": None, "closed": [],
            "equity": None, "stats": {}, "killswitch": {}, "last_scan": {},
        }

    def load_state(self) -> Dict:
        path = self.state_path
        state = self._blank_state()
        if not path or not os.path.isfile(path):
            self._recompute(state)
            return state
        try:
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
            if not isinstance(saved, dict):
                raise ValueError("상태 파일이 객체가 아닙니다")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"[testnet-auto] 상태 파일을 읽지 못해 새로 시작합니다: {exc}", flush=True)
            self._recompute(state)
            return state
        state = {**state, **saved}
        if saved.get("mode") and saved["mode"] != state["mode"] and state.get("position"):
            # mock에서 잡은 가짜 포지션을 실테스트넷 기록으로 착각하지 않는다
            print(f"[testnet-auto] 모드가 다른 상태 파일({saved['mode']} → {state['mode']})입니다 — "
                  "포지션 기록은 무시하고 거래소 상태를 따릅니다", flush=True)
            state["position"] = None
        # 설정이 바뀌어도 포지션·체결 기록은 이어 간다. 규칙 표기만 최신 설정으로 맞춘다.
        state["rule"] = self._blank_state()["rule"]
        state["mode"] = "mock" if self.mock else "testnet"
        state["symbol"] = self.cfg.symbol
        state["interval"] = int(self.cfg.interval)
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
        """청산 기록에서 통계를 다시 만든다. 손익을 확인하지 못한 청산(pnl 없음)은 따로 센다."""
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
            "last_trade_at": closed[-1].get("exit_time") if closed else None,
        }
        return state["stats"]

    # ------------------------------------------------------------------ 스캔 본체
    def tick(self, candles: Optional[Sequence[Dict]] = None, now_ms: Optional[int] = None,
             signals: Optional[Sequence[Dict]] = None) -> Dict:
        """한 번 점검하고 필요하면 테스트넷 주문을 낸다. candles·signals를 주면 네트워크 없이 돈다."""
        now_ms = int(now_ms if now_ms is not None else self.now().timestamp() * 1000)
        tf_ms = self._tf_ms()
        source = None
        if candles is None:
            data = self.fetch()
            candles, source = data["candles"], data.get("source")
        bars = sorted((c for c in candles if c.get("t") is not None), key=lambda c: int(c["t"]))
        next_open: Optional[float] = None
        while len(bars) > 1 and int(bars[-1]["t"]) + tf_ms > now_ms:
            forming = bars.pop()                        # 진행 중인 봉으로 신호를 세우지 않는다
            next_open = float(forming["o"])              # 이 봉의 시가 = 마지막 확정 봉의 '다음 봉 시가'
        if not bars:
            raise RuntimeError("확정 봉이 없습니다(캔들 부족 또는 시각 이상)")
        last = bars[-1]
        state = self.load_state()
        if len(bars) < MIN_CANDLES:
            return self._finish(state, last, "ERROR",
                                [f"확정 봉이 {len(bars)}개뿐입니다 — 최소 {MIN_CANDLES}개가 필요합니다"],
                                source=source, bars=len(bars))

        def finish(action: str, events: List[str], signal=None, verdict=None, order=None,
                   account: Optional[Dict] = None) -> Dict:
            return self._finish(state, last, action, events, source=source, bars=len(bars),
                                signal=signal, verdict=verdict, order=order, account=account)

        # 0) 거래소 상태 — 읽지 못하면 아무 주문도 내지 않는다(계좌를 모르는 채로 쏘지 않는다)
        try:
            balance = self.client.get_balance()
            positions = self.client.get_positions(self.cfg.symbol)
        except BybitError as exc:
            return finish("ERROR", [f"거래소 조회 실패: {exc}"])

        equity = float(balance.get("equity") or 0.0)
        account = {"equity": equity, "available": balance.get("available"),
                   "riskBaseUsd": balance.get("riskBaseUsd"), "mock": bool(balance.get("mock"))}
        self.killswitch.start_day(equity)
        if equity <= 0:
            return finish("ERROR", ["잔고가 0입니다 — 테스트넷 계정에 모의 USDT를 채운 뒤 다시 실행하세요"],
                          account=account)

        rows = [p for p in positions if p.get("symbol") == self.cfg.symbol and float(p.get("size") or 0) > 0]
        ex = max(rows, key=lambda p: float(p["size"])) if rows else None
        local = state.get("position")
        events: List[str] = []
        action = "HOLD"
        signal = verdict = order = None
        blocked = False                                  # 이번 스캔에서 신규 진입 금지

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
                    action, trade = "CLOSE", closed
                    events.append(f"모의 청산 {trade['reason']} {trade['r']:+.2f}R · "
                                  f"손익 {trade['pnl']:+.2f} USDT")
            elif self.cfg.time_exit and self._bars_held(local, last) >= self.cfg.max_bars:
                action, events = self._time_exit(state, local, events, account)
            if state.get("position") or action == "CLOSE":
                blocked = True                           # 아직 보유 중이거나 방금 닫음 — 신규 진입 없음
        elif local and not ex:
            trade = self._record_exchange_close(state, local, account)
            state["exchange_position"] = None
            action = "CLOSE"
            events.append(f"청산 확인 {trade['reason']} · 손익 " +
                          (f"{trade['pnl']:+.2f} USDT ({trade['r']:+.2f}R)" if trade["pnl"] is not None
                           else "미확인(거래소 기록 지연)"))
            blocked = True                               # 한 스캔에서 닫고 바로 다시 열지 않는다
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

        # 2) 새 신호 — 마지막 확정 봉에 후보1(M1)이 섰는가
        params, cfg = self.rule()
        if signals is None:
            engine = Engine(params)
            found = engine.run(bars)
            capture = engine.capture
        else:
            found, capture = list(signals), {}
        bar_signals = [s for s in found if int(s["index"]) == len(bars) - 1 and s.get("kind") == "M1"]
        if not bar_signals:
            if action == "HOLD" and not events:
                events.append(self._hold_note(bars, found))
            return finish(action, events, account=account)

        sig = bar_signals[0]
        signal = sig
        key = f"{int(sig['time'])}|{sig['side']}"
        seen = state.setdefault("signals_seen", [])
        if key in seen:
            return finish(action, events, signal=signal, account=account)
        seen.append(key)
        del seen[:-MAX_SEEN]

        entry_bar_time = int(last["t"]) + tf_ms
        price = next_open if next_open else float(last["c"])
        atr = float(sig.get("atr") or 0.0)
        side = sig["side"]
        stop = target = 0.0
        entry_ok = self.killswitch.allow_entry()

        # 3) 진입 조건 — 세션 → 시간 → 킬스위치 → 급변 → 현재가 → AI
        if blocked:
            events.append(f"후보 {sig['side']} 기록만 하고 진입하지 않음(포지션·중복 사유)")
            return finish(action, events, signal=signal, account=account)
        if not in_session(entry_bar_time, session_hours(cfg)):
            action = "SKIP"
            events.append(f"세션 밖 진입({self.cfg.session}) — 후보는 기록")
        elif now_ms - entry_bar_time >= tf_ms:
            action = "SKIP"
            events.append("진입 시점(다음 봉)이 이미 지나 평가하지 않음")
        elif now_ms - entry_bar_time > self.cfg.entry_window_sec * 1000:
            action = "SKIP"
            events.append(f"다음 봉이 시작된 지 {int((now_ms - entry_bar_time) / 60_000)}분 지나 "
                          f"진입 창({self.cfg.entry_window_sec // 60}분)을 벗어남 — 추격하지 않음")
        elif self.paused():
            action = "SKIP"
            events.append("수동 정지 중(--resume로 해제) — 후보는 기록")
        elif not entry_ok.get("ok"):
            action = "SKIP"
            events.append(f"킬스위치: {entry_ok.get('reason')}")
        elif atr <= 0 or price <= 0:
            action = "SKIP"
            events.append("ATR·가격이 없어 진입할 수 없음")
        elif self.killswitch.check_volatility(atr, price):
            action = "SKIP"
            events.append("킬스위치: 급변(ATR/가격)")
        else:
            dist = self.cfg.stop_atr * atr
            stop = price - dist if side == "LONG" else price + dist
            target = price + dist * self.cfg.target_r if side == "LONG" else price - dist * self.cfg.target_r
            if (side == "LONG" and (price <= stop or price >= target)) or \
               (side == "SHORT" and (price >= stop or price <= target)):
                action = "SKIP"
                events.append("진입가가 손절·목표를 지나 체결 규칙이 무의미")

        # 현재가가 이미 손절을 지났으면 주문하지 않는다(테스트넷 실주문에서만 — mock·재생은 가짜 시세)
        if action not in ("SKIP", "REJECT") and not self.mock and not self.cfg.replay:
            try:
                live = float(self.client.get_last_price(self.cfg.symbol))
            except BybitError as exc:
                live = 0.0
                events.append(f"현재가 조회 실패({exc}) — 현재가 확인 없이 진행하지 않습니다")
                action = "ERROR"
            if live and action != "ERROR":
                if (side == "LONG" and live <= stop) or (side == "SHORT" and live >= stop):
                    action = "SKIP"
                    events.append(f"현재가 {live:,.2f}가 이미 손절 {stop:,.2f}을 지났습니다 — 주문하지 않음")
                elif (side == "LONG" and live >= target) or (side == "SHORT" and live <= target):
                    action = "SKIP"
                    events.append(f"현재가 {live:,.2f}가 이미 목표 {target:,.2f}에 닿았습니다 — 추격하지 않음")

        # 4) AI 맥락 심사 — 판정만 받는다(사이즈·손절·목표는 규칙 그대로)
        if action not in ("SKIP", "ERROR"):
            ai = self._filter()
            if ai is None:
                verdict = {"decision": "PASS", "loss_prob": None,
                           "reason": "AI 필터 비활성 — 규칙만으로 진입", "risk_flags": [],
                           "cached": False, "error": None}
            else:
                ai.cfg.symbol = self.cfg.symbol
                ser = prepare_series(bars)
                context = build_context(bars, sig, capture.get(sig["index"]), ser, ai.cfg,
                                        symbol=self.cfg.symbol,
                                        extra=self._ai_extra(bars, sig, ser, account, rows))
                verdict = ai.review(sig, context)
            if verdict.get("decision") != "PASS":
                action = "REJECT"
                events.append(f"AI REJECT(loss_prob {verdict.get('loss_prob')}): {verdict.get('reason')}")

        if action in ("SKIP", "ERROR", "REJECT"):
            return finish(action, events, signal=signal, verdict=verdict, account=account)

        # 5) 주문 — 수량·명목가·손실 예산은 bybit_client의 50달러 규칙이 정한다
        word = "Buy" if side == "LONG" else "Sell"
        try:
            order = self.client.place_market_order(self.cfg.symbol, word, stop_loss=stop,
                                                   take_profit=target, entry_price=price,
                                                   loss_pct=self.cfg.loss_pct,
                                                   leverage=self.cfg.leverage)
        except OrderRejected as exc:
            events.append(f"주문 거부: {exc}")
            return finish("SKIP", events, signal=signal, verdict=verdict, account=account)
        except (ValueError, BybitConfigError) as exc:
            events.append(f"주문 규칙 위반: {exc}")
            return finish("SKIP", events, signal=signal, verdict=verdict, account=account)
        except BybitAPIError as exc:
            events.append(f"주문 전송 오류({exc}) — 결과 불확실: 같은 스캔에서 다시 보내지 않고 "
                          "다음 스캔에서 거래소 포지션으로 확인합니다")
            return finish("ERROR", events, signal=signal, verdict=verdict, account=account)

        state["position"] = self._position_from_order(order, sig, verdict, entry_bar_time, atr)
        pos = state["position"]
        action = "OPEN"
        events.append(
            f"테스트넷 {'롱' if side == 'LONG' else '숏'} 진입 {pos['qty']}개 @ {float(pos['entry']):,.2f} · "
            f"손절 {float(pos['stop']):,.2f} · 목표 {float(pos['target']):,.2f} · "
            f"예상손실 ${pos['risk_usd']:.2f} · 레버리지 {pos['leverage']:g}배 · 주문 {pos['order_id']}")
        return finish(action, events, signal=signal, verdict=verdict, order=order, account=account)

    # ------------------------------------------------------------------ 기록
    def _finish(self, state: Dict, last: Dict, action: str, events: List[str], *,
                signal=None, verdict=None, order=None, account: Optional[Dict] = None,
                source=None, bars: Optional[int] = None) -> Dict:
        state["scan_bar"] = int(last["t"])
        state["scan_count"] = int(state.get("scan_count") or 0) + 1
        if account and account.get("equity") is not None:
            state["equity"] = round(float(account["equity"]), 2)
        state["killswitch"] = self._killswitch_summary()
        self._recompute(state)
        record = {
            "ts": self.now().isoformat(timespec="seconds"),
            "mode": "mock" if self.mock else "testnet",
            "testnet_only": True, "base_url": self.client.base_url,
            "bar": _iso(last["t"]), "bar_kst": _kst(last["t"]),
            "price": last["c"], "bars": bars, "source": source,
            "equity": state.get("equity"),
            "riskBaseUsd": (account or {}).get("riskBaseUsd"),
            "action": action, "events": events,
            "position": self._position_view(state),
            "exchange": {"open_positions": 1 if state.get("exchange_position") else 0},
            "signal": None if not signal else {
                "time": _iso(signal["time"]), "side": signal["side"], "kind": signal.get("kind"),
                "label": signal.get("label"), "entry": signal.get("entry"), "stop": signal.get("stop"),
                "target": signal.get("target"), "atr": signal.get("atr"),
                "reasons": signal.get("reasons")},
            "filter": verdict,
            "order": None if not order else {
                "orderId": order.get("orderId"), "mock": order.get("mock"),
                "symbol": self.cfg.symbol,
                "payload": order.get("payload"),
                "plan": order.get("plan")},
            "stats": state["stats"],
            "killswitch": {"ok": state["killswitch"].get("ok"),
                           "reason": state["killswitch"].get("reason")},
        }
        state["last_scan"] = {k: record[k] for k in ("ts", "bar", "action", "events", "price")}
        self.save_state(state)
        self._append_log(record)
        return record

    def _hold_note(self, bars: List[Dict], signals: Sequence[Dict]) -> str:
        """신호가 없을 때 로그에 남기는 한 줄 — 왜 관망인지 짧게."""
        return (f"후보 없음(관망) · 확정 봉 {len(bars)}개 · 엔진 신호 {len(signals)}건 · "
                f"마지막 봉 {_kst(bars[-1]['t'])} KST")

    def _position_view(self, state: Dict) -> Optional[Dict]:
        pos = state.get("position")
        if not pos:
            return None
        return {"side": pos["side"], "qty": pos["qty"], "entry": pos["entry"],
                "stop": pos["stop"], "target": pos["target"], "leverage": pos.get("leverage"),
                "risk_usd": pos.get("risk_usd"), "order_id": pos.get("order_id"),
                "entry_at": pos.get("entry_time"), "ai": (pos.get("ai") or {}).get("decision")}

    def _killswitch_summary(self) -> Dict:
        status = self.killswitch.status()
        st = status["state"]
        return {"ok": status["entry"]["ok"], "reason": status["entry"]["reason"],
                "tripped": st.get("tripped"), "tripped_until": st.get("tripped_until"),
                "manual_stop": st.get("manual_stop"), "realized_pnl": st.get("realized_pnl"),
                "trades_today": st.get("trades"), "rules": status["rules"]}

    # ------------------------------------------------------------------ 포지션
    def _position_from_order(self, order: Dict, sig: Dict, verdict: Optional[Dict],
                             entry_bar_time: int, atr: float) -> Dict:
        """주문 결과(계획·payload)를 내 기록으로. 실제로 보낸 값(반올림된 TP/SL)을 남긴다."""
        plan = order.get("plan") or {}
        payload = order.get("payload") or {}
        return {
            "side": plan.get("side") or ("Buy" if sig["side"] == "LONG" else "Sell"),
            "qty": plan.get("qty"),
            "entry": float(payload.get("price") or plan.get("entryPrice") or 0.0),
            "stop": float(payload.get("stopLoss") or plan.get("stopPrice") or 0.0),
            "target": float(payload.get("takeProfit") or plan.get("takeProfit") or 0.0),
            "leverage": plan.get("leverage"),
            "risk_usd": plan.get("expectedLossUsd"),
            "loss_budget_usd": plan.get("lossBudgetUsd"),
            "order_id": order.get("orderId"), "mode": "mock" if self.mock else "testnet",
            "signal_time": int(sig["time"]), "entry_bar_time": int(entry_bar_time),
            "entry_time": self.now().isoformat(timespec="seconds"), "atr": atr,
            "ai": None if not verdict else {k: verdict.get(k) for k in
                                            ("decision", "loss_prob", "reason", "cached", "error")},
            "reasons": sig.get("reasons") or [],
        }

    @staticmethod
    def _side_ko(side: str) -> str:
        return "롱" if str(side).lower() in ("buy", "long") else "숏"

    def _same_position(self, local: Dict, ex: Dict) -> bool:
        """내 기록과 거래소 포지션이 같은 것인가 — 방향과 수량(절반 이내 차이)으로 본다."""
        if str(ex.get("side") or "") != str(local.get("side") or ""):
            return False
        qty = float(local.get("qty") or 0.0)
        size = float(ex.get("size") or 0.0)
        return abs(size - qty) <= max(1e-9, qty * 0.5)

    def _bars_held(self, pos: Dict, last: Dict) -> int:
        tf_ms = self._tf_ms()
        entry_bar = int(pos.get("entry_bar_time") or 0)
        return max(0, (int(last["t"]) - entry_bar) // tf_ms + 1) if entry_bar else 0

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
            if self.cfg.time_exit and count >= self.cfg.max_bars:
                return self._mock_close(state, pos, "TIME", float(bar["c"]), t, equity)
        return None

    def _mock_close(self, state: Dict, pos: Dict, reason: str, price: float, bar_time: int,
                    equity: float) -> Dict:
        """mock 포지션을 닫고(클라이언트 장부도 같이) 체결을 남긴다. 수수료는 넣지 않는다."""
        try:
            self.client.close_position(self.cfg.symbol)
        except (BybitError, OrderRejected) as exc:      # 장부가 이미 비었어도 기록은 남긴다
            print(f"[testnet-auto] mock 청산 실패: {exc}", flush=True)
        qty = float(pos["qty"])
        entry = float(pos["entry"])
        sign = 1.0 if pos["side"] == "Buy" else -1.0
        pnl = (price - entry) * qty * sign              # mock은 수수료를 넣지 않는다
        risk = float(pos.get("risk_usd") or 0.0)
        trade = self._trade(pos, reason, price, round(pnl, 4),
                            round(pnl / risk, 3) if risk else None, bar_time)
        state["closed"].append(trade)
        state["position"] = None
        self.killswitch.record_trade(trade["pnl"], equity)
        return trade

    def _record_exchange_close(self, state: Dict, pos: Dict, account: Dict) -> Dict:
        """거래소에서 이미 닫힌 포지션(TP/SL·수동 청산)을 청산 기록으로 남긴다."""
        rows: List[Dict] = []
        try:
            rows = self.client.get_closed_pnl(self.cfg.symbol, limit=5)
        except BybitError as exc:
            print(f"[testnet-auto] 청산 손익 조회 실패: {exc}", flush=True)
        pick = self._match_closed_pnl(rows, pos)
        pnl = None if pick is None else pick.get("closedPnl")
        exit_price = None if pick is None else pick.get("avgExitPrice")
        reason = self._infer_reason(pos, exit_price)
        risk = float(pos.get("risk_usd") or 0.0)
        trade = self._trade(pos, reason, exit_price if exit_price else float(pos["entry"]),
                            None if pnl is None else round(float(pnl), 4),
                            None if (pnl is None or not risk) else round(float(pnl) / risk, 3),
                            int(pick.get("updatedTime") or 0) if pick else None)
        if pick is None:
            trade["note"] = "거래소 청산 기록을 찾지 못했습니다(손익 미확인)"
        state["closed"].append(trade)
        state["position"] = None
        if trade["pnl"] is not None:
            self.killswitch.record_trade(trade["pnl"], float(account.get("equity") or 0.0))
        return trade

    @staticmethod
    def _match_closed_pnl(rows: Sequence[Dict], pos: Dict) -> Optional[Dict]:
        """내 포지션과 같은 방향·수량의 가장 최근 청산 기록.

        진입 이전의 옛 청산 기록을 집어 오지 않도록, 신호 시각 이후에 갱신된 행만 본다.
        """
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
        """청산가가 손절가·목표가에 붙어 있으면 그 사유로, 아니면 기타(수동·강제 청산)."""
        if not exit_price:
            return "UNKNOWN"
        stop, target = float(pos.get("stop") or 0.0), float(pos.get("target") or 0.0)
        tol = max(float(pos.get("entry") or 0.0) * 0.002, 1e-9)   # 0.2% — 호가·슬리피지 여유
        if stop and abs(float(exit_price) - stop) <= tol:
            return "STOP"
        if target and abs(float(exit_price) - target) <= tol:
            return "TARGET"
        return "OTHER"

    def _trade(self, pos: Dict, reason: str, exit_price: Optional[float], pnl: Optional[float],
               r: Optional[float], exit_bar_time: Optional[int]) -> Dict:
        return {
            "side": pos["side"], "qty": pos["qty"], "entry": pos["entry"], "exit": exit_price,
            "stop": pos["stop"], "target": pos["target"], "risk_usd": pos.get("risk_usd"),
            "reason": reason, "pnl": pnl, "r": r, "mode": "mock" if self.mock else "testnet",
            "order_id": pos.get("order_id"), "signal_time": pos.get("signal_time"),
            "entry_time": pos.get("entry_time"),
            "exit_time": self.now().isoformat(timespec="seconds"),
            "exit_bar_time": exit_bar_time,
            "ai_decision": (pos.get("ai") or {}).get("decision"),
            "ai_reason": (pos.get("ai") or {}).get("reason"),
        }

    def _time_exit(self, state: Dict, pos: Dict, events: List[str], account: Dict):
        """96봉 시간 청산(테스트넷 실주문). 주문 뒤에도 포지션이 남아 있으면 기록을 지우지 않는다."""
        try:
            result = self.client.close_position(self.cfg.symbol)
            events.append(f"{self.cfg.max_bars}봉 시간 청산 주문 전송(주문 {result.get('orderId')})")
        except (BybitError, OrderRejected) as exc:
            events.append(f"시간 청산 실패: {exc} — 다음 스캔에서 다시 시도합니다")
            return "HOLD", events
        try:
            still = [p for p in self.client.get_positions(self.cfg.symbol)
                     if float(p.get("size") or 0) > 0]
        except BybitError:
            still = []
        if still:
            events.append(f"시간 청산 주문 후에도 포지션 {still[0]['size']}이 남아 있습니다 — 기록을 유지합니다")
            return "HOLD", events
        trade = self._record_exchange_close(state, pos, account)
        state["exchange_position"] = None
        trade["reason"] = "TIME" if trade.get("reason") in (None, "UNKNOWN") else trade["reason"]
        events.append(f"청산 확인 TIME · 손익 " +
                      (f"{trade['pnl']:+.2f} USDT" if trade["pnl"] is not None else "미확인"))
        return "CLOSE", events

    # ------------------------------------------------------------------ 조회
    def sync_clock(self) -> Dict:
        """거래소 시각과의 차이를 맞춘다(Bybit 10002 방지). mock은 요청 없이 0."""
        if self.mock:
            return {"offset_ms": 0, "mock": True}
        try:
            return {"offset_ms": self.client.sync_time(), "mock": False}
        except BybitError as exc:
            return {"offset_ms": None, "error": str(exc), "mock": False}

    def status(self) -> Dict:
        state = self.load_state()
        out = {"ok": True, "mode": "mock" if self.mock else "testnet",
               "testnet_only": True, "symbol": self.cfg.symbol,
               "client": self.client.mode(), "rule": state["rule"],
               "state": state, "killswitch": self.killswitch.status(),
               "settings": self.cfg.to_dict(), "log": self.tail_log()}
        try:
            out["account"] = self.client.get_balance()
            out["positions"] = self.client.get_positions(self.cfg.symbol)
        except BybitError as exc:
            out["account_error"] = str(exc)
        return out

    def reset(self) -> Dict:
        """로컬 기록 초기화. 거래소에 열린 포지션이 있으면 거부한다(기록을 잃고 방치되는 일 방지)."""
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


# ---------------------------------------------------------------------- 클라이언트

class RunLock:
    """같은 상태 파일을 두 프로세스가 동시에 쓰지 않게 하는 잠금.

    같은 PC에서 창을 두 개 띄우면 같은 봉에 주문이 두 번 나갈 수 있다(테스트넷에서도 포지션이 겹친다).
    2시간이 지난 잠금은 꺼진 프로세스의 흔적으로 보고 넘어간다.
    """

    def __init__(self, path: str, stale_sec: int = 2 * 3600):
        self.path = path or ""
        self.stale_sec = stale_sec
        self.held = False

    def acquire(self) -> bool:
        if not self.path:
            return True
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        for attempt in (1, 2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if attempt == 2:
                    return False
                try:
                    age = time.time() - os.path.getmtime(self.path)
                except OSError:
                    age = 0
                if age < self.stale_sec:
                    return False
                try:
                    os.remove(self.path)               # 오래된 잠금 — 꺼진 프로세스의 흔적
                except OSError:
                    return False
                continue
            except OSError:
                return False
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(f"{os.getpid()} {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n")
            self.held = True
            return True
        return False

    def release(self) -> None:
        if self.held and self.path:
            try:
                os.remove(self.path)
            except OSError:
                pass
            self.held = False


def build_client(force_mock: bool = False) -> BybitClient:
    """테스트넷 클라이언트. force_mock이면 키가 있어도 mock(네트워크 주문 없음)."""
    if force_mock:
        return BybitClient(api_key="", api_secret="")
    return BybitClient()


# ---------------------------------------------------------------------- 출력

def print_banner(engine: TestnetAuto) -> None:
    if engine.mock:
        print("[testnet-auto] 모드: mock — 키가 없거나 --mock입니다. 실제 주문은 나가지 않습니다.",
              flush=True)
    else:
        print("[testnet-auto] 모드: 테스트넷 실주문 — api-testnet.bybit.com · 모의 USDT만 움직입니다.",
              flush=True)
    lim = risk_limits()
    print(f"[testnet-auto] 안전 규칙: 최대 시드 ${lim['maxSeedUsd']:g} · 1회 손실 "
          f"{lim['lossPctRange'][0]:g}~{lim['lossPctRange'][1]:g}% · 레버리지 ≤ {lim['maxLeverage']:g}배 · "
          f"메인넷 주문 불가", flush=True)


def print_record(rec: Dict) -> None:
    st = rec["stats"]
    print(f"[testnet-auto] {rec['ts']} · 봉 {rec['bar_kst']} KST · {rec['action']} · "
          f"{float(rec['price']):,.2f}"
          + (f" · {' | '.join(rec['events'])}" if rec["events"] else ""), flush=True)
    pos = rec.get("position")
    if pos:
        print(f"        보유 {pos['side']} {pos['qty']} · 진입 {float(pos['entry']):,.2f} · "
              f"손절 {float(pos['stop']):,.2f} · 목표 {float(pos['target']):,.2f}", flush=True)
    win = st.get("win_rate_pct")
    print(f"        체결 {st['trades']}건 · 승률 {win if win is not None else '-'}% · "
          f"순손익 {st['net_pnl']:+.2f} USDT · 평균 R {st['avg_r'] if st['avg_r'] is not None else '-'} · "
          f"자본 {rec.get('equity')} USDT", flush=True)


def print_status(engine: TestnetAuto) -> None:
    status = engine.status()
    acct = status.get("account") or {}
    print(f"[testnet-auto] 모드 {status['mode']} · {status['symbol']} · "
          f"주소 {status['client']['baseUrl']}", flush=True)
    print(f"        진입 {('정지(--resume로 해제)' if engine.paused() else '허용')}", flush=True)
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
          f"오늘 체결 {ks['state'].get('trades')}건 · 실현손익 {ks['state'].get('realized_pnl')}",
          flush=True)
    st = state["stats"]
    win = st.get("win_rate_pct")
    print(f"        누적 체결 {st.get('trades')}건 · 승률 {win if win is not None else '-'}% · "
          f"순손익 {st.get('net_pnl')} USDT · 손익미확인 {st.get('unknown_pnl')}건", flush=True)
    if status.get("log"):
        print("        최근 스캔:", flush=True)
        for row in status["log"][-5:]:
            print(f"         · {row.get('bar_kst')} {row.get('action')} "
                  f"{' | '.join(row.get('events') or [])[:120]}", flush=True)


# ---------------------------------------------------------------------- CLI

def _candle_file(path: str) -> Dict:
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    candles = rows if isinstance(rows, list) else (rows.get("candles") or [])
    if not candles:
        raise ValueError(f"{path}에서 캔들을 읽지 못했습니다")
    return {"source": f"file:{Path(path).name}", "candles": candles}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="바이비트 테스트넷 50달러 오토 모드(테스트넷 전용)")
    ap.add_argument("--once", action="store_true", help="한 번만 점검·판단(스케줄러 연동용)")
    ap.add_argument("--loop", action="store_true", help="30분 봉 마감 +90초마다 계속(Ctrl+C 종료)")
    ap.add_argument("--mock", action="store_true", help="키가 있어도 mock으로만 돈다(주문 없음)")
    ap.add_argument("--no-ai", action="store_true", help="AI 심사 없이 규칙만")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--source", default="auto", choices=["auto", "bybit", "okx", "bitget", "binance"])
    ap.add_argument("--limit", type=int, default=3000, help="받을 30분봉 수")
    ap.add_argument("--loss-pct", type=float, default=3.0, help="1회 손실 예산(2~4%%)")
    ap.add_argument("--leverage", type=float, default=3.0, help="레버리지(1~5배)")
    ap.add_argument("--target-r", type=float, default=4.0)
    ap.add_argument("--stop-atr", type=float, default=3.0)
    ap.add_argument("--max-bars", type=int, default=96)
    ap.add_argument("--no-time-exit", action="store_true", help="96봉 시간 청산을 쓰지 않는다")
    ap.add_argument("--candles", default=None, help="과거 캔들 JSON으로 오프라인 재생(현재가 확인 생략)")
    ap.add_argument("--state", default=None, help="상태 파일 경로")
    ap.add_argument("--log", default=None, help="로그 파일 경로")
    ap.add_argument("--killswitch", default=None, help="킬스위치 상태 파일 경로")
    ap.add_argument("--status", action="store_true", help="계좌·포지션·킬스위치 상태")
    ap.add_argument("--reset", action="store_true", help="로컬 기록 초기화(열린 포지션 있으면 거부)")
    ap.add_argument("--stop", action="store_true", help="진입 수동 정지")
    ap.add_argument("--resume", action="store_true", help="수동 정지 해제")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    # 값 검증은 주문 전에 — 잘못된 손실률·레버리지는 만들 때부터 거부한다
    try:
        BybitClient.validate_loss_pct(args.loss_pct)
        BybitClient.validate_leverage(args.leverage)
    except ValueError as exc:
        print(f"[testnet-auto] 설정 오류: {exc}", flush=True)
        return 2

    cfg = AutoConfig(symbol=args.symbol, source=args.source, limit=args.limit,
                     loss_pct=args.loss_pct, leverage=args.leverage, target_r=args.target_r,
                     stop_atr=args.stop_atr, max_bars=args.max_bars,
                     time_exit=not args.no_time_exit, replay=bool(args.candles),
                     state_path=args.state or "", log_path=args.log or "",
                     killswitch_path=args.killswitch or "")
    try:
        client = build_client(force_mock=args.mock)
    except BybitConfigError as exc:
        print(f"[testnet-auto] 설정 오류: {exc}", flush=True)
        return 2

    engine = TestnetAuto(cfg, client=client, ai=False if args.no_ai else None)
    if not args.quiet:
        print_banner(engine)
        sync = engine.sync_clock()
        if sync.get("error"):
            print(f"[testnet-auto] 시각 동기화 실패(계속 진행): {sync['error']}", flush=True)
        elif not sync.get("mock"):
            print(f"[testnet-auto] 거래소 시각과 {sync['offset_ms'] / 1000:+.1f}초 차이를 맞췄습니다",
                  flush=True)

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
        print(f"[testnet-auto] 신규 진입 {'정지' if args.stop else '재개'} — "
              f"돌고 있는 루프도 다음 스캔부터 따릅니다. "
              "열린 포지션은 거래소 TP/SL이 계속 관리합니다.", flush=True)
        return 0

    lock = RunLock(engine.state_path + ".lock")
    if not lock.acquire():
        print(f"[testnet-auto] 이미 실행 중인 것 같습니다({engine.state_path}.lock). 두 번 돌리면 "
              "주문이 겹칠 수 있어 멈춥니다.\n"
              f"          확실히 꺼져 있다면 그 파일을 지우고 다시 실행하세요.", flush=True)
        return 1
    atexit.register(lock.release)

    if args.reset:
        try:
            engine.reset()
        except (RuntimeError, BybitError) as exc:
            print(f"[testnet-auto] 초기화하지 않았습니다: {exc}", flush=True)
            return 1
        print("[testnet-auto] 로컬 기록을 초기화했습니다(거래소 계좌는 그대로).", flush=True)
        return 0

    if args.candles:
        data = _candle_file(args.candles)
        now_ms = int(data["candles"][-1]["t"]) + engine._tf_ms() + 60_000
        try:
            rec = engine.tick(candles=data["candles"], now_ms=now_ms)
        except Exception as exc:
            print(f"[testnet-auto] 재생 실패: {exc}", flush=True)
            return 1
        if not args.quiet:
            print_record(rec)
        return 0

    def once() -> int:
        try:
            rec = engine.tick()
        except Exception as exc:                     # 시세·파일 실패는 이번 스캔 실패로 남긴다
            print(f"[testnet-auto] 스캔 실패: {exc}", flush=True)
            return 1
        if not args.quiet:
            print_record(rec)
        return 0

    if args.loop:
        tf_ms = engine._tf_ms()
        if not args.quiet:
            print(f"[testnet-auto] {cfg.symbol} {cfg.interval}분봉 · 봉 마감 +{BAR_DELAY_SEC}초마다 점검 · "
                  f"상태 {engine.state_path}", flush=True)
        try:
            while True:
                once()
                now_ms = int(engine.now().timestamp() * 1000)
                target = next_bar_close_ms(now_ms, tf_ms)
                wait = max(5, round((target - now_ms) / 1000))
                if not args.quiet:
                    print(f"[testnet-auto] 다음 점검 {_kst(target)} KST ({wait // 60}분 {wait % 60}초 뒤) — "
                          f"Ctrl+C로 멈춥니다", flush=True)
                time.sleep(wait)
        except KeyboardInterrupt:                    # Ctrl+C — 포지션은 거래소 TP/SL이 계속 관리
            print("\n[testnet-auto] 안전 종료. 열린 포지션이 있으면 거래소의 손절·목표가 계속 걸려 있습니다.",
                  flush=True)
            return 0
    return once()


if __name__ == "__main__":
    raise SystemExit(main())
