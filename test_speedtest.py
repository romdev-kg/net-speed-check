import contextlib
import http.client
import http.server
import io
import socket
import threading
import time
import unittest

import speedtest

PAYLOAD = b"x" * 256_000


class _Handler(http.server.BaseHTTPRequestHandler):
    lock = threading.Lock()
    hits = 0
    in_flight = 0
    max_in_flight = 0  # stays at 1 only if requests never overlap

    def do_GET(self):
        if self.path == "/missing":
            self.send_error(404)
            return
        if self.path == "/truncated":
            # promise 1000 bytes, send 10, then drop the connection
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b"x" * 10)
            return
        cls = type(self)
        with cls.lock:
            cls.hits += 1
            cls.in_flight += 1
            cls.max_in_flight = max(cls.max_in_flight, cls.in_flight)
        try:
            time.sleep(0.01)  # widen the window in which overlapping requests would be seen
            self.send_response(200)
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)
        finally:
            with cls.lock:
                cls.in_flight -= 1

    def log_message(self, format, *args):  # keep test output clean
        pass


def _quiet_main(argv):
    """Run main() and return (exit code, stdout text, stderr text)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = speedtest.main(argv)
    return code, out.getvalue(), err.getvalue()


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
        _Handler.hits = _Handler.in_flight = _Handler.max_in_flight = 0

    def test_ten_requests_run_one_after_another_and_download_everything(self):
        summary = speedtest.run(f"{self.base}/file", count=10, timeout=5, out=io.StringIO())
        self.assertEqual(_Handler.hits, 10)
        self.assertEqual(_Handler.max_in_flight, 1)
        self.assertEqual(summary.requests, 10)
        self.assertEqual(summary.total_bytes, 10 * len(PAYLOAD))
        self.assertGreater(summary.megabits_per_second, 0)

    def test_main_prints_speed_to_redirected_stdout(self):
        code, out, err = _quiet_main([f"{self.base}/file", "-n", "2"])
        self.assertEqual(code, 0)
        self.assertIn("[2/2]", out)
        self.assertIn("Мбит/с", out)
        self.assertEqual(err, "")

    def test_http_error_exits_with_code_1_and_names_the_request(self):
        code, _, err = _quiet_main([f"{self.base}/missing", "-n", "2"])
        self.assertEqual(code, 1)
        self.assertIn("запрос 1 из 2", err)
        self.assertIn("404", err)

    def test_truncated_response_is_an_error_not_a_measurement(self):
        with self.assertRaises(speedtest.DownloadError) as caught:
            speedtest.run(f"{self.base}/truncated", count=1, timeout=5, out=io.StringIO())
        self.assertIn("получено 10 из 1000 байт", str(caught.exception))

    def test_connection_refused_exits_with_code_1(self):
        with socket.socket() as probe:  # find a port nobody listens on
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]
        code, _, err = _quiet_main([f"http://127.0.0.1:{free_port}/file", "-n", "1"])
        self.assertEqual(code, 1)
        self.assertIn("не удалось подключиться", err)


class ErrorMessageTest(unittest.TestCase):
    def test_low_level_errors_become_readable_messages(self):
        self.assertIn("некорректный ответ", speedtest.describe_error(http.client.BadStatusLine("SSH-2.0")))
        self.assertIn("таймаут", speedtest.describe_error(socket.timeout("timed out")))
        self.assertIn("нельзя отправить", speedtest.describe_error(UnicodeEncodeError("latin-1", "я", 0, 1, "x")))


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
    def test_url_is_normalized(self):
        self.assertEqual(
            speedtest.normalize_url("  HTTPS://example.com/my file/фото.jpg?a=1&b=я#top "),
            "https://example.com/my%20file/%D1%84%D0%BE%D1%82%D0%BE.jpg?a=1&b=%D1%8F",
        )
        # an already encoded address must not be encoded twice
        self.assertEqual(
            speedtest.normalize_url("http://example.com/a%20b"), "http://example.com/a%20b"
        )

    def test_bad_arguments_are_rejected_with_exit_code_2(self):
        bad = (
            ["ftp://example.com/a"],
            ["example.com/a"],
            ["http://example.com:abc/a"],
            ["http://[::1/a"],
            ["https://example.com/a", "-n", "0"],
            ["https://example.com/a", "--timeout", "0"],
            ["https://example.com/a", "--timeout", "-1"],
            ["https://example.com/a", "--timeout", "nan"],
            ["https://example.com/a", "--timeout", "inf"],
        )
        for argv in bad:
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    speedtest.parse_args(argv)
                self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
