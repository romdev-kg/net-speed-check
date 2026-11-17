#!/usr/bin/env python3
"""Measure download speed by fetching one URL several times in a row.

Each request is sent only after the previous response has been read to the
end. The script reports the average time per request, the total amount of
data received and the resulting download speed.
"""
from __future__ import annotations

import argparse
import http.client
import math
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

CHUNK_SIZE = 64 * 1024  # read the body in chunks so large files never sit in memory
USER_AGENT = "net-speed-check/1.0"

# Everything urllib can raise for a bad address, a network problem or a broken
# response. ValueError covers addresses that cannot be encoded into a request.
NETWORK_ERRORS = (OSError, http.client.HTTPException, ValueError)


class DownloadError(Exception):
    """A request failed. The message is ready to be shown to the user."""


@dataclass(frozen=True)
class Measurement:
    seconds: float  # from sending the request to reading the last byte of the body
    bytes_received: int  # body bytes as they came over the wire (no decompression)


@dataclass(frozen=True)
class Summary:
    requests: int
    total_bytes: int
    total_seconds: float

    @property
    def average_seconds(self) -> float:
        return self.total_seconds / self.requests

    @property
    def megabytes_per_second(self) -> float:
        if self.total_seconds <= 0:
            return 0.0
        return self.total_bytes / self.total_seconds / 1_000_000

    @property
    def megabits_per_second(self) -> float:
        return self.megabytes_per_second * 8


def build_opener() -> urllib.request.OpenerDirector:
    """Prepare the reusable parts of an HTTP client once.

    Loading CA certificates and reading proxy settings takes time. Doing it
    here keeps that one-off cost out of the first measurement.
    """
    https = urllib.request.HTTPSHandler(context=ssl.create_default_context())
    return urllib.request.build_opener(https)


def fetch_once(opener: urllib.request.OpenerDirector, url: str, timeout: float) -> Measurement:
    """Download the whole response body once and time it."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            # Ask for the raw bytes: a compressed transfer would be smaller
            # than the file the user asked us to download.
            "Accept-Encoding": "identity",
            # Avoid being served from an intermediate cache.
            "Cache-Control": "no-cache",
        },
    )
    started = time.perf_counter()
    received = 0
    with opener.open(request, timeout=timeout) as response:
        expected = response.headers.get("Content-Length")
        while chunk := response.read(CHUNK_SIZE):
            received += len(chunk)
    elapsed = time.perf_counter() - started
    # If the connection drops mid-body, read() just returns nothing more and
    # raises no error. Compare with the size the server promised.
    if expected is not None and expected.isdigit() and received != int(expected):
        raise DownloadError(f"ответ оборвался: получено {received} из {expected} байт")
    return Measurement(seconds=elapsed, bytes_received=received)


def describe_error(exc: Exception) -> str:
    """Turn a low-level exception into a short message for the user."""
    if isinstance(exc, DownloadError):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):  # must come before URLError, its parent
        return f"сервер ответил {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return f"не удалось подключиться: {exc.reason}"
    if isinstance(exc, socket.timeout):  # the same class as TimeoutError on Python 3.10+
        return "сервер перестал отвечать (таймаут)"
    if isinstance(exc, http.client.HTTPException):  # not HTTP at all, or the body broke off
        return f"сервер прислал некорректный ответ ({type(exc).__name__})"
    if isinstance(exc, UnicodeError):  # e.g. a Cyrillic domain name
        return "в адресе есть символы, которые нельзя отправить в запросе"
    return str(exc) or type(exc).__name__


def summarize(measurements: list[Measurement]) -> Summary:
    """Aggregate per-request results."""
    if not measurements:
        raise ValueError("no measurements to summarize")
    return Summary(
        requests=len(measurements),
        total_bytes=sum(m.bytes_received for m in measurements),
        total_seconds=sum(m.seconds for m in measurements),
    )


def format_summary(summary: Summary) -> str:
    return "\n".join(
        [
            f"Запросов:              {summary.requests}",
            f"Скачано всего:         {summary.total_bytes / 1_000_000:.2f} МБ ({summary.total_bytes} байт)",
            f"Среднее время запроса: {summary.average_seconds * 1000:.0f} мс",
            f"Скорость скачивания:   {summary.megabits_per_second:.2f} Мбит/с "
            f"({summary.megabytes_per_second:.2f} МБ/с)",
        ]
    )


def run(url: str, count: int, timeout: float, out=None) -> Summary:
    """Make `count` requests one after another and print the results.

    Raises DownloadError as soon as one request fails: an incomplete series
    would give a misleading average, so there is no partial summary.
    """
    out = out or sys.stdout  # looked up at call time so redirected stdout is respected
    opener = build_opener()
    measurements: list[Measurement] = []
    for number in range(1, count + 1):
        try:
            measurement = fetch_once(opener, url, timeout)
        except (DownloadError, *NETWORK_ERRORS) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()  # release the socket of the error response
            raise DownloadError(f"запрос {number} из {count} не удался: {describe_error(exc)}") from exc
        measurements.append(measurement)
        # flush so progress shows up immediately even when output is piped
        print(
            f"[{number}/{count}] {measurement.bytes_received} байт за {measurement.seconds * 1000:.0f} мс",
            file=out,
            flush=True,
        )
    summary = summarize(measurements)
    print(file=out)
    print(format_summary(summary), file=out, flush=True)
    return summary


def normalize_url(raw: str) -> str:
    """Validate the address and percent-encode spaces and non-ASCII characters."""
    try:
        parts = urllib.parse.urlsplit(raw.strip())
        parts.port  # reading it raises ValueError when the port is malformed
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"некорректный адрес: {exc}") from None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise argparse.ArgumentTypeError("адрес должен начинаться с http:// или https://")
    return urllib.parse.urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc,
            # "%" stays as is so already-encoded addresses are not encoded twice
            urllib.parse.quote(parts.path, safe="/%:@!$&'()*+,;=~"),
            urllib.parse.quote(parts.query, safe="/%:@!$&'()*+,;=~?"),
            "",  # the fragment (#...) is never sent to the server
        )
    )


def positive_int(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("должно быть не меньше 1")
    return value


def positive_seconds(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("должно быть положительным числом")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Замер скорости скачивания по одному адресу.")
    parser.add_argument(
        "url",
        type=normalize_url,
        help="адрес, к которому стучаться (лучше тяжёлый файл, от нескольких мегабайт)",
    )
    parser.add_argument(
        "-n", "--count", type=positive_int, default=10, help="сколько запросов сделать (по умолчанию 10)"
    )
    parser.add_argument(
        "--timeout",
        type=positive_seconds,
        default=30.0,
        help="сколько секунд ждать данных от сервера, прежде чем считать запрос зависшим (по умолчанию 30)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run(args.url, args.count, args.timeout)
    except DownloadError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nПрервано.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
