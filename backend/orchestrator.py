"""Provider-independent 13-agent analysis flow that emits the UI's SSE events."""

import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen


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
    "neutral": "── 리스크 심사 · 중립 ──",
    "risky": "── 리스크 심사 · 공격 ──",
    "pm": "── 5/5 PM 최종 승인 ──",
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


class AnalysisOrchestrator:
    def __init__(self, provider, market_loader=fetch_market):
        self.provider = provider
        self.market_loader = market_loader

    def run(self, symbol, emit):
        market = self.market_loader(symbol)
        emit({"type": "run:start", "symbol": market["symbol"], "display": market["display"], "mode": "algo", "auto": False, "model": "LLM 13인"})
        emit({"type": "log", "kind": "sys", "line": f"> {market['symbol']} 시세 수신 완료 · Bybit USDT 무기한"})
        emit({
            "type": "market", "symbol": market["symbol"], "display": market["display"], "axis": "perp", "tf": "5m",
            "priceLine": f"${market['price']:,.2f} · Bybit USDT 무기한",
            "candles": market["candles"],
        })
        context = {
            "symbol": market["symbol"],
            "price": market["price"],
            "20개 봉 등락률(%)": market["change20BarsPct"],
            "최근 20개 봉 고저": market["range20Bars"],
            "최근 12개 5분봉 OHLC": market["candles"][-12:],
            "뉴스": "제공되지 않음",
            "펀더멘털 자료": "제공되지 않음",
            "소셜 자료": "제공되지 않음",
        }
        history = []
        turns = [
            ("analysts", [("taro", None), ("diana", None), ("nova", None), ("vibe", None)]),
            ("research1", [("bull", 1), ("bear", 2)]),
            ("research2", [("bull", 3), ("bear", 4)]),
            ("ace", [("ace", None)]),
            ("safe", [("safe", None)]),
            ("neutral", [("neutral", None)]),
            ("risky", [("risky", None)]),
            ("pm", [("pm", None)]),
        ]
        answers = {}
        for stage, agents in turns:
            emit({"type": "log", "kind": "stage", "line": STAGES[stage]})
            for agent_id, turn in agents:
                role, assignment = AGENTS[agent_id]
                debate = json.dumps(history[-10:], ensure_ascii=False)
                user_prompt = (
                    f"역할: {role}\n업무: {assignment}\n"
                    f"시장 자료(JSON): {json.dumps(context, ensure_ascii=False)}\n"
                    f"앞선 토론(JSON): {debate}\n"
                    "bubble과 report는 한국어로 작성하세요. PM은 최종 action을 BUY, SELL, HOLD 중 하나로 반드시 결정하고, "
                    "충분한 근거가 없거나 위험이 과도하면 HOLD를 선택하세요."
                )
                emit({"type": "agent:start", "id": agent_id, **({"turn": turn} if turn else {})})
                answer, bubble, report = _parse_answer(self.provider.complete(SYSTEM_PROMPT, user_prompt))
                history.append({"agent": agent_id, "role": role, "report": report})
                answers[agent_id] = answer
                emit({
                    "type": "agent:done", "id": agent_id, "bubble": bubble, "report": report,
                    **({"turn": turn} if turn else {}),
                })

        final = answers["pm"]
        action = str(final.get("action", "HOLD")).upper()
        if action not in ("BUY", "SELL", "HOLD"):
            action = "HOLD"
        try:
            confidence = max(0, min(100, int(final.get("confidence", 50))))
        except (TypeError, ValueError):
            confidence = 50
        verdict = str(final.get("verdict", "APPROVE")).upper()
        if verdict not in ("APPROVE", "AMEND", "REJECT"):
            verdict = "APPROVE"
        emit({
            "type": "decision", "action": action, "confidence": confidence,
            "entry": f"{market['price']:,.2f}" if action != "HOLD" else "—",
            "stop": "—", "target": "—",
            "rationale": str(final.get("rationale", final.get("report", "")))[:2000],
            "report": str(final.get("report", ""))[:6000],
            "verdict": verdict, "display": market["display"],
        })
        emit({"type": "log", "kind": "stage", "line": f">>> 최종 판정: {action} ({confidence}%) · PM {verdict}"})
        emit({"type": "run:end"})
