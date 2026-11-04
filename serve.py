"""로컬 대시보드 서버 - 새로고침 버튼으로 시세·수급·뉴스를 새로 가져옵니다.

사용법
  python serve.py   ->  브라우저에서 http://127.0.0.1:8765 열기 (포트 변경: PORT=9000)

동작
  - 127.0.0.1(내 PC)에서만 열립니다. 외부에서는 접속할 수 없습니다.
  - 키움 앱키/시크릿키, 네이버 키는 .env 에만 있고 브라우저로 전달되지 않습니다.
  - 새로고침 = 키움(시세·수급·지수) + Yahoo(NBI·환율) + 네이버/구글(뉴스)을 다시 호출
  - 토큰은 서버가 한 번 발급해 재사용하고, 종료(Ctrl+C) 시 폐기합니다.
  - 연속 클릭은 10초 이내면 직전 결과를 돌려줍니다 (키움 호출 제한 보호).
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import report

PORT = int(os.environ.get("PORT", "8765"))
MIN_INTERVAL = 10  # 초


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.kw = None
        self.data = None
        self.ts = 0.0
        p = report.OUT_DIR / "data.json"
        if p.exists():  # 직전 실행 결과가 있으면 첫 화면에 바로 보여줌
            try:
                self.data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass

    def _client(self):
        if self.kw is None:
            appkey, secret = os.environ.get("KIWOOM_APPKEY"), os.environ.get("KIWOOM_SECRETKEY")
            if not appkey or not secret:
                raise RuntimeError("KIWOOM_APPKEY / KIWOOM_SECRETKEY 미설정 (.env 확인)")
            kw = report.Kiwoom(appkey, secret, mock=os.environ.get("KIWOOM_MOCK") == "1")
            kw.login()
            self.kw = kw
        return self.kw

    def refresh(self):
        with self.lock:
            if self.data and time.time() - self.ts < MIN_INTERVAL:
                return self.data
            try:
                data = report.collect(self._client(), require_today=False)
            except Exception as first:  # noqa: BLE001  토큰 만료 등 -> 재발급 후 1회 재시도
                print("재시도(토큰 재발급):", first, file=sys.stderr)
                self.kw = None
                data = report.collect(self._client(), require_today=False)
            self.data, self.ts = data, time.time()
            report.OUT_DIR.mkdir(exist_ok=True)
            (report.OUT_DIR / "data.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return data

    def close(self):
        if self.kw:
            self.kw.revoke()


STATE = State()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 조용히
        pass

    def _host_ok(self):
        # DNS rebinding 방지: 로컬 호스트명으로 들어온 요청만 허용
        return (self.headers.get("Host") or "").split(":")[0] in ("127.0.0.1", "localhost")

    def _send(self, code, body, ctype):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "forbidden", "text/plain; charset=utf-8")
        if self.path.split("?")[0] in ("/", "/index.html"):
            if STATE.data:
                page = report.render_dashboard(STATE.data)
            else:  # 데이터가 아직 없으면 템플릿을 주고, 화면이 로드되며 자동으로 새로고침을 호출
                page = (report.HERE / "index.html").read_text(encoding="utf-8")
            return self._send(200, page, "text/html; charset=utf-8")
        self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, "forbidden", "text/plain; charset=utf-8")
        if self.path.split("?")[0] != "/api/refresh":
            return self._send(404, "not found", "text/plain; charset=utf-8")
        try:
            data = STATE.refresh()
            self._send(200, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")
        except Exception as e:  # noqa: BLE001
            print("새로고침 실패:", e, file=sys.stderr)
            self._send(
                500,
                json.dumps({"error": str(e)}, ensure_ascii=False),
                "application/json; charset=utf-8",
            )


if __name__ == "__main__":
    report.load_env()
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"대시보드: http://127.0.0.1:{PORT}   (종료: Ctrl+C)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STATE.close()
