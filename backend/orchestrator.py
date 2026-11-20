"""Provider-independent 13-agent analysis flow that emits the UI's SSE events.

2026-11-20 P0 보강
- 내 계좌(모의 잔고·보유)와 복기 교훈, 시장 국면(변동성·추세·F&G·경제일정)을 프롬프트 문맥에 넣는다.
- 리스크 3인(보수 → 중립 → 공격)이 앞 심사자의 논거를 받아 교차 검증한다(선택 2라운드).
- 같은 종목 중복 진입은 문맥과 게이트 양쪽에서 막는다.
- 분석 1회를 `data/reports/` 에 md·json 으로 남긴다(입력 상태 read/absent 포함).
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from . import regime as regime_mod
from .memory import LessonMemory, duplicate_entry, portfolio_context
from .reports import write_report
from .risk import KillSwitch, KillSwitchRules
from .strategy import Engine, Params, evaluate

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


AGENTS = {
    "taro": ("기술 분석가", "가격 추세, 캔들 구조, 변동성을 분석합니다."),
    "diana": ("펀더멘털 분석가", "제공된 자료의 펀더멘털을 검토하고, 없는 자료는 없다고 명시합니다."),
    "nova": ("뉴스 분석가", "제공된 뉴스 근거만 분석하고, 뉴스가 없으면 추측하지 않습니다."),
    "vibe": ("소셜 분석가", "제공된 소셜·심리 근거만 분석하고, 없는 자료는 없다고 명시합니다."),
    "bull": ("BULL · 매수 측", "롱 진입을 지지할 수 있는 근거와 반대 근거를 평가합니다."),
    "bear": ("BEAR · 매도 측", "숏 진입을 지지할 수 있는 근거와 반대 근거를 평가합니다."),
    "ace": ("ACE · 수석 트레이더", "토론과 자료를 종합해 실행 가능한 제안을 작성합니다."),
    "safe": ("보수적 리스크 심사자", "최악의 손실, 데이터 공백, 진입 보류 필요성을 검토합니다."),
    "neutral": ("중립 리스크 심사자", "양측 의견을 비교하고 리스크 대비 근거를 검토합니다."),
    "risky": ("공격적 리스크 심사자", "기회와 리스크를 검토하되 손실 가능성을 숨기지 않습니다."),
    "pm": ("PM · 최종 승인자", "전체 토론과 리스크 심사를 바탕으로 최종 BUY, SELL, HOLD를 결정합니다."),
}

SYSTEM_PROMPT = """당신은 금융 분석팀의 한 구성원입니다. 제공된 자료만 사용하고 가격·뉴스·펀더멘털·소셜 데이터를 지어내지 마세요. 확실하지 않거나 자료가 없으면 분명히 밝히세요. 수익을 보장하지 말고 위험을 간결히 설명하세요. 반드시 유효한 JSON 객체 하나만 출력하세요. 필드는 bubble(한국어 한 문장), report(한국어 브리핑), action(BUY/SELL/HOLD 중 하나; PM만), confidence(0~100 정수; PM만), rationale(최종 근거; PM만), verdict(APPROVE/AMEND/REJECT; PM만)입니다."""

STAGES = {
    "analysts": "── 1/5 애널리스트 브리핑 · 기술 / 펀더멘털 / 뉴스 / 소셜 ──",
    "research1": "── 2/5 리서치룸 BULL vs BEAR · 1라운드 ──",
    "research2": "── BULL vs BEAR · 2라운드 반박 ──",
    "ace": "── 3/5 ACE 결재 · 토론 서류 전달 ──",
    "safe": "── 4/5 리스크 심사 · 보수 ──",
    "neutral": "── 리스크 심사 · 중립 (보수 논거 교차 검증) ──",
    "risky": "── 리스크 심사 · 공격 (보수·중립 논거 반박) ──",
    "risk_round": "── 리스크 심사 · 교차 반박 라운드 ──",
    "pm": "── 5/5 PM 최종 승인 ──",
}

# 앞 심사자의 논거를 반드시 받아 검증하게 하는 지시(교차 토론)
CROSS_REVIEW = {
    "safe": ("보수 심사자로서 최악의 손실과 데이터 공백을 먼저 지적하세요. "
             "아직 다른 심사자 발언은 없습니다."),
    "neutral": ("앞선 SAFE 심사자의 핵심 논거를 report 안에서 한 줄로 인용하고 '동의' 또는 '반대'를 분명히 밝힌 뒤, "
                "양쪽을 저울질해 판단하세요."),
    "risky": ("SAFE와 NEUTRAL의 논거를 각각 한 줄로 인용하고, 동의하는 부분과 반대하는 부분을 밝힌 뒤 "
              "공격적 관점의 근거를 제시하세요. 손실 가능성은 숨기지 마세요."),
    "pm": ("리스크 3인(SAFE·NEUTRAL·RISKY)의 합의점과 불일치를 한 줄로 요약한 뒤 최종 판단을 내리세요. "
           "'내 계좌'에 이미 같은 종목 포지션이 있으면 중복 진입 대신 HOLD를 고르세요."),
}


def fetch_market(symbol, timeout=12):
    base = re.sub(r"[-_/]?(USDT|USD|PERP)$", "", str(symbol).strip().upper())
    if not re.fullmatch(r"[A-Z0-9]{2,15}", base):
        raise ValueError("심볼을 영문으로 입력하세요 (예: BTC)")
    ticker = base + "USDT"
    query = urlencode({"category": "linear", "symbol": ticker, "interval": "5", "limit": "144"})
    try:
        with urlopen(f"https://api.bybit.com/v5/market/kline?{query}", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"Bybit 시세 요청 실패 (HTTP {exc.code})") from None
    except URLError as exc:
        raise RuntimeError(f"Bybit 시세 연결 실패: {exc.reason}") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Bybit 캔들 응답 형식이 올바르지 않습니다") from None
    if not isinstance(payload, dict) or payload.get("retCode") != 0:
        message = payload.get("retMsg", "unknown error") if isinstance(payload, dict) else "invalid response"
        raise RuntimeError(f"Bybit 시세 응답 오류: {message}")
    try:
        rows = payload.get("result", {}).get("list", [])
        candles = [
            {"t": int(row[0]), "o": float(row[1]), "h": float(row[2]), "l": float(row[3]), "c": float(row[4])}
            for row in reversed(rows)
        ]
    except (KeyError, TypeError, IndexError, ValueError):
        raise RuntimeError("Bybit 캔들 응답 형식이 올바르지 않습니다") from None
    if len(candles) < 20:
        raise RuntimeError(f"분석할 캔들이 부족합니다 ({len(candles)}개)")
    closes = [candle["c"] for candle in candles]
    move = ((closes[-1] / closes[-20]) - 1) * 100 if closes[-20] else 0
    high = max(candle["h"] for candle in candles[-20:])
    low = min(candle["l"] for candle in candles[-20:])
    return {
        "symbol": ticker,
        "display": base,
        "price": closes[-1],
        "change20BarsPct": round(move, 3),
        "range20Bars": {"low": low, "high": high},
        "candles": candles,
    }


def _parse_answer(raw):
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        answer = json.loads(text)
    except json.JSONDecodeError:
        answer = {"bubble": text[:100], "report": text}
    if not isinstance(answer, dict):
        raise RuntimeError("에이전트 응답 JSON이 객체가 아닙니다")
    bubble = str(answer.get("bubble", "")).strip()
    report = str(answer.get("report", "")).strip()
    if not bubble:
        bubble = report[:100] or "자료를 검토했습니다."
    if not report:
        report = bubble
    return answer, bubble[:240], report[:6000]


def load_paper_portfolio(data_dir: Optional[str] = None):
    """`data/paper_state.json` → (state, killswitch 요약). 파일이 없으면 (None, None)."""
    base = Path(data_dir) if data_dir else DATA_DIR
    try:
        with open(base / "paper_state.json", encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None, None
    ks = state.get("killswitch") if isinstance(state, dict) else None
    return state, ks if isinstance(ks, dict) else None


def _load_json(path: Path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


class AnalysisOrchestrator:
    def __init__(self, provider, market_loader=fetch_market, strategy_params=None, killswitch=None,
                 ai_filter=None, ai_filter_veto=True, portfolio_loader: Optional[Callable] = None,
                 lessons: Optional[LessonMemory] = None, data_dir: Optional[str] = None,
                 report_dir: Optional[str] = None, risk_rounds: int = 1, clock=None):
        self.provider = provider
        self.market_loader = market_loader
        # 더블 S + MDS v7 룰 엔진 — AI 판단 앞에 '규칙이 선 자리'를 함께 놓는다
        self.strategy_params = strategy_params or Params()
        self.killswitch = killswitch if killswitch is not None else KillSwitch(KillSwitchRules(), state_path=None)
        # AI 맥락 필터 — 규칙이 선 자리를 Gemini가 다시 심사한다(없으면 이 단계를 건너뛴다)
        self.ai_filter = ai_filter
        self.ai_filter_veto = ai_filter_veto
        # 내 책(P0-1)·복기(P0-2)·국면(P0-3) — 로더가 없으면 '없음'으로 정직하게 남긴다
        self.portfolio_loader = portfolio_loader
        self.data_dir = Path(data_dir) if data_dir else DATA_DIR
        self.lessons = lessons if lessons is not None else LessonMemory(data_dir=str(self.data_dir))
        self.report_dir = report_dir
        self.risk_rounds = max(1, int(risk_rounds or 1))
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------ 문맥
    def _portfolio(self, symbol: Optional[str]):
        if not self.portfolio_loader:
            return None, None
        try:
            state, ks = self.portfolio_loader()
        except Exception:                       # 계좌 파일이 깨져도 분석은 계속
            return None, None
        if not isinstance(state, dict):
            return None, None
        return portfolio_context(state, ks, symbol=symbol), state

    def _regime(self, candles: Sequence[Dict]):
        try:
            last = candles[-1]
            now_ms = int(last["t"])
            global_json = _load_json(self.data_dir / "global.json")
            calendar = _load_json(self.data_dir / "calendar.json")
            ser = regime_mod.series(candles)
            return regime_mod.summary(candles, len(candles) - 1, ser, global_json=global_json,
                                      calendar=calendar, now_ms=now_ms)
        except Exception as exc:                # 국면을 못 만들면 없다고 남긴다
            return {"국면": None, "입력": {"시장국면": f"계산 실패: {exc}"}}

    def _rule_view(self, candles):
        """마지막 봉 기준 v7 판정 + 그 봉의 엔진 상태. 캔들이 모자라거나 오류면 None(지어내지 않는다)."""
        try:
            sig = evaluate(candles, len(candles) - 1, self.strategy_params)
        except Exception as exc:                     # 규칙이 깨져도 분석 전체를 멈추지 않는다
            return {"side": None, "reasons": [f"룰 엔진 오류: {exc}"]}, None
        snap = self._snapshot(candles) if sig.get("side") else None
        return sig, snap

    def _snapshot(self, candles):
        """마지막 봉까지의 존·레벨·지표 — AI 필터의 문맥(미래 봉은 보지 않는다)."""
        try:
            eng = Engine(self.strategy_params)
            eng.run(candles, capture_at=[len(candles) - 1])
            return eng.capture.get(len(candles) - 1)
        except Exception:
            return None

    def _ai_review(self, rule, candles, snap, extra=None):
        """AI 필터 한 번. 실패는 통과로 두고 이유를 남긴다(매매를 멈추지 않는다)."""
        try:
            verdict = self.ai_filter.review(rule, candles=candles, snap=snap, extra=extra)
        except Exception as exc:
            verdict = {"decision": "PASS", "reason": f"AI 필터 오류: {exc}", "error": str(exc)}
        return {k: verdict.get(k) for k in
                ("decision", "loss_prob", "reason", "confidence", "model", "cached", "error")}

    def _turns(self):
        turns = [
            ("analysts", [("taro", None), ("diana", None), ("nova", None), ("vibe", None)]),
            ("research1", [("bull", 1), ("bear", 2)]),
            ("research2", [("bull", 3), ("bear", 4)]),
            ("ace", [("ace", None)]),
            ("safe", [("safe", None)]),
            ("neutral", [("neutral", None)]),
            ("risky", [("risky", None)]),
        ]
        for _ in range(1, self.risk_rounds):
            turns.append(("risk_round", [("safe", None), ("neutral", None), ("risky", None)]))
        turns.append(("pm", [("pm", None)]))
        return turns

    # ------------------------------------------------------------------ 본체
    def run(self, symbol, emit):
        market = self.market_loader(symbol)
        model_name = None
        try:
            model_name = (getattr(self.provider, "config", None) or {}).get("model")
        except Exception:
            model_name = None
        emit({"type": "run:start", "symbol": market["symbol"], "display": market["display"], "mode": "algo",
              "auto": False, "model": model_name or "LLM 13인"})
        emit({"type": "log", "kind": "sys", "line": f"> {market['symbol']} 시세 수신 완료 · Bybit USDT 무기한"})
        emit({
            "type": "market", "symbol": market["symbol"], "display": market["display"], "axis": "perp", "tf": "5m",
            "priceLine": f"${market['price']:,.2f} · Bybit USDT 무기한",
            "candles": market["candles"],
        })

        candles = market["candles"]
        portfolio, paper_state = self._portfolio(market["symbol"])
        regime_view = self._regime(candles)
        try:
            lessons = self.lessons.prompt_lines(5)
        except Exception:
            lessons = ["복기 메모리를 읽지 못했습니다 — 실적을 지어내지 마세요."]
        if portfolio:
            emit({"type": "log", "kind": "sys",
                  "line": f"> 내 책: 자본 {portfolio.get('자본USDT')} USDT · 열린 포지션 "
                          f"{portfolio.get('열린포지션수')}건 · 오늘 {portfolio.get('오늘손익USDT')} USDT"})
        if regime_view.get("국면"):
            emit({"type": "log", "kind": "sys", "line": f"> 시장 국면: {regime_view.get('국면')}"})

        inputs = {
            "시세": f"읽음({len(candles)}봉 5분봉 · Bybit)",
            "내 계좌": f"읽음(열린 포지션 {portfolio.get('열린포지션수')}건)" if portfolio else "없음",
            "복기 메모리": "읽음" if lessons and "읽지 못" not in lessons[0] else "없음",
            "경제일정": (regime_view.get("입력") or {}).get("경제일정", "없음"),
            "공포탐욕지수": (regime_view.get("입력") or {}).get("공포탐욕지수", "없음"),
            "뉴스": "없음 — 지어내지 마세요",
            "펀더멘털 자료": "없음 — 지어내지 마세요",
            "소셜 자료": "없음 — 지어내지 마세요",
        }
        context = {
            "symbol": market["symbol"],
            "price": market["price"],
            "20개 봉 등락률(%)": market["change20BarsPct"],
            "최근 20개 봉 고저": market["range20Bars"],
            "최근 12개 5분봉 OHLC": candles[-12:],
            "내 계좌": portfolio or "제공되지 않음(모의 계좌 파일 없음)",
            "최근 복기 교훈": lessons,
            "시장 국면": regime_view,
            "뉴스": "제공되지 않음 — 지어내지 마세요",
            "펀더멘털 자료": "제공되지 않음 — 지어내지 마세요",
            "소셜 자료": "제공되지 않음 — 지어내지 마세요",
            "입력 상태": inputs,
        }

        # v7 룰 엔진(더블 S + MDS) 판정을 자료로 넣는다 — AI는 규칙이 선 자리를 확인하고 말한다
        rule, rule_snap = self._rule_view(candles)
        if rule and rule.get("side"):
            context["v7 규칙 판정"] = {
                "근거": rule.get("kind"), "라벨": rule.get("label"), "방향": rule.get("side"),
                "등급": rule.get("grade"), "손절": rule.get("stop"), "목표": rule.get("target"),
                "손익비": rule.get("rr"), "이유": rule.get("reasons"),
            }
            emit({"type": "log", "kind": "sys",
                  "line": f"> v7 규칙: {rule.get('side')} · {rule.get('kind')} {rule.get('label')} · "
                          f"손익비 {rule.get('rr')} · {', '.join(rule.get('reasons') or [])}"})
        else:
            context["v7 규칙 판정"] = {"방향": "없음", "이유": (rule or {}).get("reasons") or ["조건 미충족"]}
            emit({"type": "log", "kind": "sys", "line": "> v7 규칙: 이번 봉에는 진입 자리가 없습니다"})

        # AI 맥락 필터 — 규칙이 선 자리만 심사한다(자리가 없으면 호출하지 않는다 = 한도 절약)
        extra = {"내 계좌": portfolio, "최근 복기 교훈": lessons, "시장 국면": regime_view}
        ai_view = None
        if self.ai_filter is not None and (rule or {}).get("side"):
            ai_view = self._ai_review(rule, candles, rule_snap, extra=extra)
            context["AI 맥락 필터"] = ai_view
            inputs["AI 맥락 필터"] = "읽음"
            prob = ai_view.get("loss_prob")
            mark = "⛔ REJECT" if ai_view.get("decision") == "REJECT" else "✅ PASS"
            emit({"type": "log", "kind": "sys",
                  "line": f"> AI 맥락 필터: {mark}" + (f" · 손절확률 {prob:g}" if isinstance(prob, (int, float)) else "")
                          + (f" · {ai_view.get('reason')}" if ai_view.get("reason") else "")})
        else:
            inputs["AI 맥락 필터"] = "없음(필터 꺼짐 또는 자리 없음)"

        history = []
        answers = {}
        for stage, agents in self._turns():
            emit({"type": "log", "kind": "stage", "line": STAGES[stage]})
            round_no = 1 if stage in ("safe", "neutral", "risky") else 0
            for agent_id, turn in agents:
                role, assignment = AGENTS[agent_id]
                debate = json.dumps(history[-12:], ensure_ascii=False)
                guide = CROSS_REVIEW.get(agent_id, "")
                if stage == "risk_round":
                    guide = ("교차 반박 라운드입니다. 앞선 리스크 3인의 발언을 근거로 삼아 "
                             "반대 심사자의 주장을 1줄로 반박하거나, 설득됐으면 입장을 바꾸고 그 이유를 밝히세요.")
                user_prompt = (
                    f"역할: {role}\n업무: {assignment}\n"
                    f"시장 자료(JSON): {json.dumps(context, ensure_ascii=False)}\n"
                    f"앞선 토론(JSON): {debate}\n"
                    + (f"추가 지시: {guide}\n" if guide else "")
                    + "bubble과 report는 한국어로 작성하세요. PM은 최종 action을 BUY, SELL, HOLD 중 하나로 반드시 결정하고, "
                      "충분한 근거가 없거나 위험이 과도하면 HOLD를 선택하세요."
                )
                emit({"type": "agent:start", "id": agent_id, **( {"turn": turn} if turn else {})})
                answer, bubble, report = _parse_answer(self.provider.complete(SYSTEM_PROMPT, user_prompt))
                history.append({"agent": agent_id, "role": role, "report": report,
                                **({"round": stage} if stage.startswith("risk") else {})})
                answers[agent_id] = answer
                emit({
                    "type": "agent:done", "id": agent_id, "bubble": bubble, "report": report,
                    **( {"turn": turn} if turn else {}),
                    **({"round": stage} if stage in ("risk_round",) else {}),
                })
        _ = round_no

        final = answers["pm"]
        action = str(final.get("action", "HOLD")).upper()
        if action not in ("BUY", "SELL", "HOLD"):
            action = "HOLD"

        # AI 맥락 필터 거부 — 규칙이 선 자리라도 문맥이 무너졌으면 진입 대신 관망
        ai_note = None
        if (ai_view and self.ai_filter_veto and ai_view.get("decision") == "REJECT" and action != "HOLD"):
            ai_note = f"AI 맥락 필터 거부: {ai_view.get('reason') or '문맥 위험'}"
            emit({"type": "log", "kind": "sys", "line": f"> ⛔ {ai_note} — 진입 대신 관망으로 바꿉니다"})
            action = "HOLD"

        # 중복 진입 — 같은 종목 포지션이 이미 있으면 새로 열지 않는다(문맥으로도 알렸지만 게이트로도 막는다)
        dup_note = None
        if action != "HOLD" and portfolio:
            dup_note = duplicate_entry(paper_state, market["symbol"],
                                       "LONG" if action == "BUY" else "SHORT")
            if dup_note:
                emit({"type": "log", "kind": "sys", "line": f"> ⛔ {dup_note} — 진입 대신 관망으로 바꿉니다"})
                action = "HOLD"

        # 킬스위치 — 오늘 이미 한도에 닿았으면 AI 판단과 무관하게 진입하지 않는다
        gate = self.killswitch.allow_entry()
        gate_note = None
        if action != "HOLD" and not gate.get("ok"):
            gate_note = f"킬스위치: {gate.get('reason')}"
            emit({"type": "log", "kind": "sys", "line": f"> ⛔ {gate_note} — 진입 대신 관망으로 바꿉니다"})
            action = "HOLD"
        try:
            confidence = max(0, min(100, int(final.get("confidence", 50))))
        except (TypeError, ValueError):
            confidence = 50
        verdict = str(final.get("verdict", "APPROVE")).upper()
        if verdict not in ("APPROVE", "AMEND", "REJECT"):
            verdict = "APPROVE"

        decision = {
            "type": "decision", "action": action, "confidence": confidence,
            "entry": f"{market['price']:,.2f}" if action != "HOLD" else "—",
            "stop": "—", "target": "—",
            "rationale": str(final.get("rationale", final.get("report", "")))[:2000],
            "report": str(final.get("report", ""))[:6000],
            "verdict": verdict, "display": market["display"],
            "rule": {
                "engine": "double-s-mds-v7",
                "side": (rule or {}).get("side"), "kind": (rule or {}).get("kind"),
                "label": (rule or {}).get("label"), "grade": (rule or {}).get("grade"),
                "rr": (rule or {}).get("rr"), "reasons": (rule or {}).get("reasons") or [],
            },
            "killswitch": {"ok": bool(gate.get("ok")), "reason": gate.get("reason"), "blocked": gate_note},
            "ai_filter": ai_view, "ai_filter_blocked": ai_note,
            "portfolio": portfolio, "regime": regime_view, "lessons": lessons,
            "duplicate_blocked": dup_note, "inputs": inputs,
            "model": model_name,
        }
        emit(decision)
        emit({"type": "log", "kind": "stage", "line": f">>> 최종 판정: {action} ({confidence}%) · PM {verdict}"})

        # 판정 리포트 아카이브 — 입력 상태·근거·모델·게이트를 한 장에 남긴다
        if self.report_dir:
            risk_turns = [{"role": AGENTS[a["agent"]][0], "report": a.get("report")}
                          for a in history if a["agent"] in ("safe", "neutral", "risky")]
            archived = write_report({
                "symbol": market["symbol"], "display": market["display"], "mode": "algo",
                "price": market["price"], "model": model_name, "action": action, "confidence": confidence,
                "rationale": decision["rationale"], "inputs": inputs, "rule": decision["rule"],
                "ai_filter": ai_view, "killswitch": decision["killswitch"], "portfolio": portfolio,
                "regime": regime_view, "lessons": lessons, "risk_turns": risk_turns,
                "blocked": ai_note or dup_note or gate_note,
            }, out_dir=str(self.report_dir), now=self.clock().astimezone(regime_mod.KST))
            if archived.get("ok"):
                emit({"type": "log", "kind": "sys", "line": f"> 리포트 저장: {Path(archived['md']).name}"})
            else:
                emit({"type": "log", "kind": "sys", "line": f"> 리포트 저장 실패: {archived.get('error')}"})
        emit({"type": "run:end"})
