"""Static app server plus SSE analysis endpoint for the optional LLM engine."""

import json
import queue
import re
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .config import (ai_filter_enabled, ai_filter_is_veto, get_ai_filter_config, get_analysis_options,
                    get_port, get_provider_config)
from .orchestrator import AnalysisOrchestrator, load_paper_portfolio
from .providers import ChatCompletionProvider


ROOT = Path(__file__).resolve().parent.parent
SUBSCRIBERS = set()
SUBSCRIBERS_LOCK = threading.Lock()
RUN_LOCK = threading.Lock()
RUNNING = False
PAPER_LOCK = threading.Lock()          # 모의매매 스캔은 한 번에 하나만(상태 파일 경합 방지)


def publish(event):
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    with SUBSCRIBERS_LOCK:
        subscribers = tuple(SUBSCRIBERS)
    for subscriber in subscribers:
        try:
            subscriber.put_nowait(payload)
        except queue.Full:
            pass


def build_ai_filter():
    """설정이 켜져 있고 키가 있으면 필터를 만든다. 없으면 None(13인 분석은 그대로 돈다)."""
    if not ai_filter_enabled():
        return None
    try:
        from .ai_filter import AIFilter
        return AIFilter(get_ai_filter_config())
    except Exception as exc:              # 필터가 안 서도 분석·시세는 계속
        print(f"[ai-filter] 비활성: {exc}")
        return None


def run_analysis(symbol):
    global RUNNING
    try:
        provider = ChatCompletionProvider(get_provider_config())
        options = get_analysis_options()
        AnalysisOrchestrator(provider, ai_filter=build_ai_filter(),
                             ai_filter_veto=ai_filter_is_veto(),
                             portfolio_loader=load_paper_portfolio,
                             report_dir=options["report_dir"],
                             risk_rounds=options["risk_rounds"]).run(symbol, publish)
    except Exception as exc:
        publish({"type": "run:error", "message": str(exc)})
        publish({"type": "log", "kind": "sys", "line": f"> ⚠ 분석 실패: {exc}"})
        publish({"type": "run:end"})
    finally:
        with RUN_LOCK:
            RUNNING = False


def run_live_filter(payload):
    """실시간 M1+AI 필터 모의판단 — 최근 캔들에서 v7 후보를 찾아 Gemini에게 한 번 심사시킨다.

    주문은 내지 않는다. 돌려주는 것은 '지금 이 자리라면 필터가 뭐라고 하는가'뿐이다.
    """
    from .ai_filter import AIFilter, build_context, nominee_setup, prepare_series, select_signals
    from .backtest import load_candles
    from .strategy import Engine

    raw = str(payload.get("symbol", "BTC")).strip().upper()
    base = re.sub(r"[-_/]?(USDT|USD|PERP)$", "", raw)
    if not re.fullmatch(r"[A-Z0-9]{2,15}", base or ""):
        raise ValueError("심볼을 영문으로 입력하세요 (예: BTC)")
    interval = str(payload.get("interval", "30"))
    if interval not in ("15", "30", "60"):
        raise ValueError("interval은 15 / 30 / 60 중 하나입니다")
    try:
        limit = max(500, min(10000, int(payload.get("limit", 3000))))
        max_age = max(1, min(500, int(payload.get("max_age_bars", 96))))
    except (TypeError, ValueError):
        raise ValueError("limit·max_age_bars는 숫자입니다") from None

    ticker = base + "USDT"
    data = load_candles(ticker, interval, limit, "auto")
    candles = data["candles"]
    params, cfg = nominee_setup()
    engine = Engine(params)
    signals = select_signals(candles, engine.run(candles), cfg)
    if not signals:
        return {"ok": True, "symbol": ticker, "interval": int(interval), "source": data.get("source"),
                "signal": None, "filter": None,
                "note": f"최근 {len(candles)}봉에서 후보1 조건(30분 M1·아시아·3ATR·4R)에 맞는 자리가 없습니다"}
    sig = signals[-1]
    age = len(candles) - 1 - int(sig["index"])
    if age > max_age:
        return {"ok": True, "symbol": ticker, "interval": int(interval), "source": data.get("source"),
                "signal": None, "filter": None,
                "note": f"마지막 후보가 {age}봉 전이라 지금 심사할 자리가 아닙니다(기준 {max_age}봉 이내)"}

    ai = AIFilter(get_ai_filter_config())
    snap = engine.capture.get(sig["index"])
    # 내 책·복기·국면도 같이 심사한다(값이 없으면 키를 비워 보낸다)
    extra = {}
    try:
        from .memory import LessonMemory, portfolio_context
        state, ks = load_paper_portfolio()
        if state:
            extra["내 계좌"] = portfolio_context(state, ks, symbol=ticker)
        lines = LessonMemory().prompt_lines(5, as_of_ms=int(sig["time"]))
        if lines:
            extra["최근 복기 교훈"] = lines
        from . import regime as regime_mod
        extra["시장 국면"] = regime_mod.summary(
            candles, int(sig["index"]), prepare_series(candles),
            global_json=regime_mod.load_json(regime_mod.DATA / "global.json"),
            calendar=regime_mod.load_json(regime_mod.DATA / "calendar.json"),
            now_ms=int(sig["time"]))
    except Exception as exc:                 # 문맥 보강 실패가 심사를 막지 않게
        extra["문맥오류"] = str(exc)[:120]
    context = build_context(candles, sig, snap, prepare_series(candles), ai.cfg, trade_cfg=cfg,
                            symbol=ticker, extra=extra)
    verdict = ai.review(sig, context)
    return {
        "ok": True, "symbol": ticker, "interval": int(interval), "source": data.get("source"),
        "bars": len(candles), "age_bars": age,
        "signal": {k: sig.get(k) for k in ("side", "kind", "label", "grade", "index", "time",
                                            "entry", "stop", "target", "rr", "reasons")},
        "trade_plan": {"entry": sig["entry"], "stop": sig["stop"],
                       "target": round(sig["entry"] + (sig["entry"] - sig["stop"]) * cfg.target_r, 2)
                       if sig["side"] == "LONG" else round(sig["entry"] - (sig["stop"] - sig["entry"]) * cfg.target_r, 2),
                       "target_r": cfg.target_r, "max_bars": cfg.max_bars, "session": cfg.session_filter},
        "filter": verdict,
        "context": context,
        "model": (ai.provider.config.get("model") if ai.provider else None),
        "calls": ai.calls, "cached": bool(verdict.get("cached")),
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/stream":
            self.stream_events()
            return
        if path == "/api/paper":
            self.handle_paper_status()
            return
        super().do_GET()

    def do_POST(self):
        global RUNNING
        path = urlparse(self.path).path
        if path == "/api/ai-filter":
            self.handle_ai_filter()
            return
        if path == "/api/paper/tick":
            self.handle_paper_tick()
            return
        if path != "/api/analyze":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 32768:
                raise ValueError("요청 크기가 올바르지 않습니다")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            symbol = body.get("symbol", "") if isinstance(body, dict) else ""
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError("심볼을 입력하세요")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})
            return
        with RUN_LOCK:
            if RUNNING:
                self.send_json(409, {"ok": False, "error": "이미 분석이 진행 중입니다."})
                return
            RUNNING = True
        threading.Thread(target=run_analysis, args=(symbol.strip(),), daemon=True).start()
        self.send_json(202, {"ok": True})

    def handle_ai_filter(self):
        """실시간 후보 심사 — 읽기 전용(주문 없음). 결과는 JSON 하나."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8192:
                raise ValueError("요청 크기가 올바르지 않습니다")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("JSON 객체를 보내세요")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})
            return
        try:
            self.send_json(200, run_live_filter(body))
        except ValueError as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:                       # 시세·AI 실패는 502로 알린다
            self.send_json(502, {"ok": False, "error": str(exc)})

    def handle_paper_status(self):
        """모의매매 현재 상태 — 포지션·통계·킬스위치·최근 로그(읽기 전용)."""
        try:
            from .paper_loop import paper_status
            self.send_json(200, paper_status())
        except Exception as exc:
            self.send_json(502, {"ok": False, "error": str(exc)})

    def handle_paper_tick(self):
        """모의매매 1회 스캔 — 주문 없음. 시세를 받고 규칙·AI·킬스위치를 한 번 돌린다."""
        if not PAPER_LOCK.acquire(blocking=False):
            self.send_json(409, {"ok": False, "error": "이미 스캔이 진행 중입니다."})
            return
        try:
            from .paper_loop import paper_tick
            self.send_json(200, {"ok": True, "record": paper_tick()})
        except Exception as exc:                       # 시세·AI 실패는 502로 알린다
            self.send_json(502, {"ok": False, "error": str(exc)})
        finally:
            PAPER_LOCK.release()

    def stream_events(self):
        subscriber = queue.Queue(maxsize=256)
        with SUBSCRIBERS_LOCK:
            SUBSCRIBERS.add(subscriber)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    payload = subscriber.get(timeout=15)
                    message = f"data: {payload}\n\n".encode("utf-8")
                except queue.Empty:
                    message = b": keep-alive\n\n"
                self.wfile.write(message)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with SUBSCRIBERS_LOCK:
                SUBSCRIBERS.discard(subscriber)

    def send_json(self, status, value):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        print(f"[http] {self.address_string()} {fmt % args}")


def main():
    port = get_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    print(f"Pixel Trading server: http://127.0.0.1:{port}/index.html")
    print(f"LLM mode: http://127.0.0.1:{port}/index.html?engine=llm")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
