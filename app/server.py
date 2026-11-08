"""HTTP service with explicit, thread-safe Prometheus instrumentation.

Run with ``python -m app.server``. Fault injection is opt-in and intended only
for a local lab. Metrics describe valid work operations, not probe requests or
requests rejected during input validation.
"""

from __future__ import annotations

import json
import os
import re
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from urllib.parse import parse_qs, urlsplit


class Metrics:
    """Keep a consistent snapshot even when requests finish concurrently."""

    BUCKETS = (0.1, 0.3, 0.5, 1.0, 2.5)

    def __init__(self) -> None:
        self._lock = Lock()
        self._requests = {200: 0, 500: 0}
        self._buckets = [0 for _ in self.BUCKETS]
        self._duration_sum = 0.0
        self._duration_count = 0

    def observe(self, status: int, duration: float) -> None:
        if status not in (200, 500) or duration < 0:
            raise ValueError("Expected a work status (200 or 500) and non-negative duration")
        with self._lock:
            self._requests[status] += 1
            self._duration_sum += duration
            self._duration_count += 1
            for index, bound in enumerate(self.BUCKETS):
                if duration <= bound:
                    self._buckets[index] += 1

    def render(self) -> bytes:
        with self._lock:
            lines = [
                "# HELP demo_http_requests_total Completed valid /api/work operations by status.",
                "# TYPE demo_http_requests_total counter",
                *(f'demo_http_requests_total{{status="{status}"}} {count}'
                  for status, count in self._requests.items()),
                "# HELP demo_http_request_duration_seconds Duration of valid /api/work operations.",
                "# TYPE demo_http_request_duration_seconds histogram",
                *(f'demo_http_request_duration_seconds_bucket{{le="{bound:g}"}} {count}'
                  for bound, count in zip(self.BUCKETS, self._buckets)),
                f'demo_http_request_duration_seconds_bucket{{le="+Inf"}} {self._duration_count}',
                f"demo_http_request_duration_seconds_sum {self._duration_sum:.9f}",
                f"demo_http_request_duration_seconds_count {self._duration_count}",
            ]
        return ("\n".join(lines) + "\n").encode("utf-8")


class DemoServer(ThreadingHTTPServer):
    """Inject settings and metrics so tests do not mutate process environment."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], *, enable_fault_injection: bool = False):
        self.metrics = Metrics()
        self.enable_fault_injection = enable_fault_injection
        super().__init__(address, DemoHandler)


class DemoHandler(BaseHTTPRequestHandler):
    server: DemoServer
    server_version = "devops-demo"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        # One structured log per request, suitable for `docker compose logs`.
        print(json.dumps({"message": format % args, "client": self.client_address[0]}), flush=True)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, object]) -> None:
        body = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:
        try:
            request = urlsplit(self.path)
            query = parse_qs(request.query, keep_blank_values=True, max_num_fields=10)
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid request URL"})
            return
        if request.path == "/healthz":
            self._json(HTTPStatus.OK, {"status": "ok"})
        elif request.path == "/readyz":
            self._json(HTTPStatus.OK, {"status": "ready"})
        elif request.path == "/metrics":
            self._send(HTTPStatus.OK, self.server.metrics.render(), "text/plain; version=0.0.4; charset=utf-8")
        elif request.path == "/api/work":
            self._work(query)
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _work(self, query: dict[str, list[str]]) -> None:
        if set(query) - {"fail", "delay_ms"}:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "allowed parameters: fail, delay_ms"})
            return
        if any(len(values) != 1 for values in query.values()):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "parameters must not be repeated"})
            return
        fail = query.get("fail", ["0"])[0]
        delay_text = query.get("delay_ms", ["0"])[0]
        if fail not in {"0", "1"}:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "fail must be 0 or 1"})
            return
        if not re.fullmatch(r"[0-9]{1,4}", delay_text) or int(delay_text) > 2000:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "delay_ms must be an integer from 0 to 2000"})
            return
        if query and not self.server.enable_fault_injection:
            self._json(HTTPStatus.FORBIDDEN, {"error": "fault injection is disabled"})
            return

        start = time.perf_counter()
        delay_ms = int(delay_text)
        if delay_ms:
            time.sleep(delay_ms / 1000)
        status = HTTPStatus.INTERNAL_SERVER_ERROR if fail == "1" else HTTPStatus.OK
        duration = time.perf_counter() - start
        self.server.metrics.observe(status, duration)
        self._json(status, {
            "status": "error" if fail == "1" else "ok",
            "message": "injected failure" if fail == "1" else "work completed",
            "delay_ms": delay_ms,
        })


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    enabled = os.environ.get("ENABLE_FAULT_INJECTION", "false").lower() == "true"
    server = DemoServer(("0.0.0.0", port), enable_fault_injection=enabled)
    print(json.dumps({"message": "service started", "port": port, "fault_injection": enabled}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
