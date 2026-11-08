"""Check the running local demo and its public metrics contract."""

import json
import os
import urllib.request


def main():
    base = os.environ.get("BASE_URL", "http://127.0.0.1:8080").rstrip("/")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for path in ("/healthz", "/readyz", "/api/work"):
        with opener.open(base + path, timeout=5) as response:
            if response.status != 200 or not isinstance(json.load(response), dict):
                raise RuntimeError(f"Unexpected response: {path}")
        print(f"PASS {path}")
    with opener.open(base + "/metrics", timeout=5) as response:
        metrics = response.read().decode()
    for name in ("demo_http_requests_total", "demo_http_request_duration_seconds_bucket",
                 "demo_http_request_duration_seconds_count"):
        if name not in metrics:
            raise RuntimeError(f"Missing metric: {name}")
    print("PASS /metrics")


if __name__ == "__main__":
    main()
