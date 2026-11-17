import http.server
import io
import threading
import unittest

import speedtest

PAYLOAD = b"x" * 256_000


class _Handler(http.server.BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):
        if self.path == "/missing":
            self.send_error(404)
            return
        type(self).hits += 1
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
        self.wfile.write(PAYLOAD)

    def log_message(self, format, *args):  # keep test output clean
        pass


class LocalServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Handler.hits = 0

    def test_ten_sequential_requests_download_everything(self):
        summary = speedtest.run(f"{self.base}/file", count=10, timeout=5, out=io.StringIO())
        self.assertEqual(_Handler.hits, 10)
        self.assertEqual(summary.requests, 10)
        self.assertEqual(summary.total_bytes, 10 * len(PAYLOAD))
        self.assertGreater(summary.megabits_per_second, 0)

    def test_http_error_exits_with_code_1(self):
        self.assertEqual(speedtest.main([f"{self.base}/missing", "-n", "2"]), 1)


class SummaryMathTest(unittest.TestCase):
    def test_average_and_units(self):
        summary = speedtest.summarize([
            speedtest.Measurement(seconds=1.0, bytes_received=1_000_000),
            speedtest.Measurement(seconds=3.0, bytes_received=3_000_000),
        ])
        self.assertEqual(summary.total_bytes, 4_000_000)
        self.assertAlmostEqual(summary.average_seconds, 2.0)
        self.assertAlmostEqual(summary.megabytes_per_second, 1.0)  # 4 MB / 4 s
        self.assertAlmostEqual(summary.megabits_per_second, 8.0)

    def test_empty_input_is_rejected(self):
        with self.assertRaises(ValueError):
            speedtest.summarize([])


class ArgsTest(unittest.TestCase):
    def test_rejects_non_http_url_and_bad_count(self):
        for argv in (["ftp://example.com/a"], ["https://example.com/a", "-n", "0"]):
            with self.assertRaises(SystemExit):
                speedtest.parse_args(argv)


if __name__ == "__main__":
    unittest.main()
