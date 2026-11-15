"""Static app server plus SSE analysis endpoint for the optional LLM engine."""

import json
import queue
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .config import get_port, get_provider_config
from .orchestrator import AnalysisOrchestrator
from .providers import ChatCompletionProvider


ROOT = Path(__file__).resolve().parent.parent
SUBSCRIBERS = set()
SUBSCRIBERS_LOCK = threading.Lock()
RUN_LOCK = threading.Lock()
RUNNING = False


def publish(event):
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    with SUBSCRIBERS_LOCK:
        subscribers = tuple(SUBSCRIBERS)
    for subscriber in subscribers:
        try:
            subscriber.put_nowait(payload)
        except queue.Full:
            pass


def run_analysis(symbol):
    global RUNNING
    try:
        provider = ChatCompletionProvider(get_provider_config())
        AnalysisOrchestrator(provider).run(symbol, publish)
    except Exception as exc:
        publish({"type": "run:error", "message": str(exc)})
        publish({"type": "log", "kind": "sys", "line": f"> ⚠ 분석 실패: {exc}"})
        publish({"type": "run:end"})
    finally:
        with RUN_LOCK:
            RUNNING = False


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        if urlparse(self.path).path == "/api/stream":
            self.stream_events()
            return
        super().do_GET()

    def do_POST(self):
        global RUNNING
        if urlparse(self.path).path != "/api/analyze":
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
