"""Exercise the HTTP contract and the metric invariants without dependencies."""

import contextlib
import io
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from app.server import DemoServer, Metrics


class HTTPContractTests(unittest.TestCase):
    def setUp(self):
        self.logs = contextlib.redirect_stdout(io.StringIO())
        self.logs.__enter__()
        self.server = DemoServer(("127.0.0.1", 0), enable_fault_injection=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.logs.__exit__(None, None, None)

    def get(self, path):
        try:
            response = urllib.request.urlopen(self.base + path, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read().decode("utf-8")

    def metric(self, metric_name):
        _, _, body = self.get("/metrics")
        return dict(line.rsplit(" ", 1) for line in body.splitlines() if not line.startswith("#"))[metric_name]

    def test_probes_are_json_and_do_not_count_as_work(self):
        for path, expected in (("/healthz", "ok"), ("/readyz", "ready")):
            status, headers, body = self.get(path)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"status": expected})
            self.assertIn("application/json", headers["Content-Type"])
            self.assertEqual(int(headers["Content-Length"]), len(body.encode("utf-8")))
        status, _, body = self.get("/does-not-exist")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "not found"})
        self.assertEqual(self.metric('demo_http_requests_total{status="200"}'), "0")
        self.assertEqual(self.metric('demo_http_requests_total{status="500"}'), "0")
        self.assertEqual(self.metric("demo_http_request_duration_seconds_count"), "0")

    def test_work_and_failure_use_separate_counters_and_same_histogram(self):
        for path, expected_status in (("/api/work", 200), ("/api/work?fail=1", 500)):
            status, _, body = self.get(path)
            self.assertEqual(status, expected_status)
            self.assertEqual(json.loads(body)["status"], "ok" if expected_status == 200 else "error")
        self.assertEqual(self.metric('demo_http_requests_total{status="200"}'), "1")
        self.assertEqual(self.metric('demo_http_requests_total{status="500"}'), "1")
        self.assertEqual(self.metric("demo_http_request_duration_seconds_count"), "2")
        self.assertEqual(self.metric('demo_http_request_duration_seconds_bucket{le="+Inf"}'), "2")

    def test_faults_are_disabled_by_default(self):
        self.server.enable_fault_injection = False
        for path in ("/api/work?fail=1", "/api/work?delay_ms=600"):
            status, _, body = self.get(path)
            self.assertEqual(status, 403)
            self.assertEqual(json.loads(body)["error"], "fault injection is disabled")
        self.assertEqual(self.get("/api/work")[0], 200)
        self.assertEqual(self.metric('demo_http_requests_total{status="500"}'), "0")
        self.assertEqual(self.metric("demo_http_request_duration_seconds_count"), "1")
        with DemoServer(("127.0.0.1", 0)) as default_server:
            self.assertFalse(default_server.enable_fault_injection)

    def test_delay_validation_rejects_unbounded_or_ambiguous_inputs(self):
        for query in (
            "delay_ms=-1", "delay_ms=2001", "delay_ms=99999999999999999999",
            "delay_ms=0.5", "delay_ms=nan", "delay_ms=", "delay_ms=1&delay_ms=2",
            "fail=yes", "unknown=1",
        ):
            with self.subTest(query=query):
                status, _, body = self.get("/api/work?" + query)
                self.assertEqual(status, 400)
                self.assertIn("error", json.loads(body))
        self.assertEqual(self.metric("demo_http_request_duration_seconds_count"), "0")

    def test_delay_is_observed_in_histogram(self):
        start = time.perf_counter()
        status, _, body = self.get("/api/work?delay_ms=600")
        elapsed = time.perf_counter() - start
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["delay_ms"], 600)
        self.assertGreaterEqual(elapsed, 0.6)
        self.assertEqual(self.metric('demo_http_request_duration_seconds_bucket{le="0.5"}'), "0")
        self.assertEqual(self.metric('demo_http_request_duration_seconds_bucket{le="+Inf"}'), "1")
        self.assertGreaterEqual(float(self.metric("demo_http_request_duration_seconds_sum")), 0.6)

    def test_concurrent_requests_preserve_counter_and_histogram_counts(self):
        paths = ["/api/work?fail=1" if index % 3 == 0 else "/api/work" for index in range(30)]
        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(lambda path: self.get(path)[0], paths))
        self.assertEqual(statuses.count(200), 20)
        self.assertEqual(statuses.count(500), 10)
        self.assertEqual(self.metric('demo_http_requests_total{status="200"}'), "20")
        self.assertEqual(self.metric('demo_http_requests_total{status="500"}'), "10")
        self.assertEqual(self.metric("demo_http_request_duration_seconds_count"), "30")
        self.assertEqual(self.metric('demo_http_request_duration_seconds_bucket{le="+Inf"}'), "30")


class HistogramTests(unittest.TestCase):
    def as_values(self, metrics):
        return {
            name: float(value)
            for name, value in (
                line.rsplit(" ", 1)
                for line in metrics.render().decode("utf-8").splitlines()
                if not line.startswith("#")
            )
        }

    def test_series_exist_before_first_request(self):
        values = self.as_values(Metrics())
        self.assertEqual(len(values), 10)
        self.assertTrue(all(value == 0 for value in values.values()))

    def test_histogram_buckets_are_cumulative_and_include_boundaries(self):
        metrics = Metrics()
        for duration in (0.05, 0.1, 0.3, 0.5, 1.0, 2.5, 3.0):
            metrics.observe(200, duration)
        values = self.as_values(metrics)
        for bound, expected in (("0.1", 2), ("0.3", 3), ("0.5", 4), ("1", 5), ("2.5", 6), ("+Inf", 7)):
            self.assertEqual(values[f'demo_http_request_duration_seconds_bucket{{le="{bound}"}}'], expected)
        self.assertEqual(values["demo_http_request_duration_seconds_count"], 7)
        self.assertAlmostEqual(values["demo_http_request_duration_seconds_sum"], 7.45)


if __name__ == "__main__":
    unittest.main()
