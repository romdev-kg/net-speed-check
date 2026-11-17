#!/usr/bin/env python3
"""Measure download speed by fetching one URL several times in a row.

Each request is sent only after the previous response has been read to the
end. The script reports the average time per request, the total amount of
data received and the resulting download speed.
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

CHUNK_SIZE = 64 * 1024  # read the body in chunks so large files never sit in memory
USER_AGENT = "net-speed-check/1.0"


@dataclass(frozen=True)
class Measurement:
    seconds: float  # from sending the request to reading the last byte of the body
    bytes_received: int  # body bytes as they came over the wire (no decompression)


@dataclass(frozen=True)
class Summary:
    requests: int
    total_bytes: int
    total_seconds: float
    average_seconds: float
    megabits_per_second: float
    megabytes_per_second: float


def fetch_once(url: str, timeout: float) -> Measurement:
    """Download the whole response body once and time it."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            # Ask for the raw bytes: compressed transfer would make the
            # payload smaller than what the user asked us to download.
            "Accept-Encoding": "identity",
            # Avoid being served from an intermediate cache.
            "Cache-Control": "no-cache",
        },
    )
    started = time.perf_counter()
    received = 0
    with urllib.request.urlopen(request, timeout=timeout) as response:
        while chunk := response.read(CHUNK_SIZE):
            received += len(chunk)
    return Measurement(seconds=time.perf_counter() - started, bytes_received=received)


def summarize(measurements: list[Measurement]) -> Summary:
    """Aggregate per-request results into averages and speed."""
    if not measurements:
        raise ValueError("no measurements to summarize")
    total_bytes = sum(m.bytes_received for m in measurements)
    total_seconds = sum(m.seconds for m in measurements)
    bytes_per_second = total_bytes / total_seconds if total_seconds > 0 else 0.0
    return Summary(
        requests=len(measurements),
        total_bytes=total_bytes,
        total_seconds=total_seconds,
        average_seconds=total_seconds / len(measurements),
        megabits_per_second=bytes_per_second * 8 / 1_000_000,
        megabytes_per_second=bytes_per_second / 1_000_000,
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


def run(url: str, count: int, timeout: float, out=sys.stdout) -> Summary:
    measurements: list[Measurement] = []
    for number in range(1, count + 1):
        measurement = fetch_once(url, timeout)
        measurements.append(measurement)
        print(
            f"[{number}/{count}] {measurement.bytes_received} байт за {measurement.seconds * 1000:.0f} мс",
            file=out,
        )
    summary = summarize(measurements)
    print(file=out)
    print(format_summary(summary), file=out)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Замер скорости скачивания по одному адресу.")
    parser.add_argument("url", help="адрес, к которому стучаться (лучше тяжёлый файл, например картинка)")
    parser.add_argument("-n", "--count", type=int, default=10, help="сколько запросов сделать (по умолчанию 10)")
    parser.add_argument("--timeout", type=float, default=30.0, help="таймаут одного запроса, сек (по умолчанию 30)")
    args = parser.parse_args(argv)
    if args.count < 1:
        parser.error("--count должен быть не меньше 1")
    if not args.url.startswith(("http://", "https://")):
        parser.error("адрес должен начинаться с http:// или https://")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run(args.url, args.count, args.timeout)
    except urllib.error.HTTPError as exc:
        print(f"Ошибка: сервер ответил {exc.code} {exc.reason}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        print(f"Ошибка: не удалось скачать {args.url}: {reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
