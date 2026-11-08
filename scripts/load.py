"""Bounded local-lab traffic generator; no third-party dependencies."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    limit = parser.add_mutually_exclusive_group()
    limit.add_argument("--requests", type=int)
    limit.add_argument("--duration", type=float)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--failure-every", type=int, default=0)
    parser.add_argument("--delay-ms", type=int, default=0)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument("--url", default=os.environ.get("BASE_URL", "http://127.0.0.1:8080"))
    args = parser.parse_args()
    if args.requests is None and args.duration is None:
        args.requests = 120
    if args.requests is not None and not 1 <= args.requests <= 10000:
        parser.error("--requests must be 1..10000")
    if args.duration is not None and not 0 < args.duration <= 600:
        parser.error("--duration must be >0 and <=600 seconds")
    if not 1 <= args.concurrency <= 32 or not 0 <= args.delay_ms <= 2000:
        parser.error("--concurrency must be 1..32; --delay-ms must be 0..2000")
    if args.failure_every < 0 or not 0.01 <= args.interval <= 10:
        parser.error("--failure-every must be >=0; --interval must be 0.01..10 seconds")
    target = urllib.parse.urlsplit(args.url)
    if target.scheme != "http" or target.hostname not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("This drill targets local HTTP services only")
    if target.username or target.password or target.path not in {"", "/"} or target.query or target.fragment:
        parser.error("--url must be a local origin without credentials, path or query")

    lock = threading.Lock()
    stop = threading.Event()
    codes = Counter()
    unexpected = 0
    sequence = 0
    started = time.monotonic()
    deadline = started + args.duration if args.duration is not None else None
    # Ignore proxy environment variables for this loopback-only lab.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def worker():
        nonlocal sequence, unexpected
        while not stop.is_set():
            with lock:
                if (deadline is not None and time.monotonic() >= deadline) or (
                    args.requests is not None and sequence >= args.requests
                ):
                    return
                sequence += 1
                number = sequence
            should_fail = bool(args.failure_every and number % args.failure_every == 0)
            query = {}
            if should_fail:
                query["fail"] = "1"
            if args.delay_ms:
                query["delay_ms"] = str(args.delay_ms)
            url = args.url.rstrip("/") + "/api/work"
            if query:
                url += "?" + urllib.parse.urlencode(query)
            try:
                with opener.open(url, timeout=5) as response:
                    response.read()
                    status = response.status
            except urllib.error.HTTPError as exc:
                status = exc.code
                exc.close()
            except (OSError, urllib.error.URLError):
                status = "connection_error"
            with lock:
                codes[str(status)] += 1
                unexpected += status != (500 if should_fail else 200)
            stop.wait(args.interval)

    print("Sending local lab traffic. Ctrl+C stops the drill.", flush=True)
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(worker) for _ in range(args.concurrency)]
        try:
            for future in futures:
                future.result()
        except KeyboardInterrupt:
            stop.set()
    print(json.dumps({"requests": sum(codes.values()), "status_codes": dict(codes),
                      "unexpected_responses": unexpected,
                      "elapsed_seconds": round(time.monotonic() - started, 2)}, indent=2))
    return 1 if unexpected else 0


if __name__ == "__main__":
    raise SystemExit(main())
