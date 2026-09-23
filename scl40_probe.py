#!/usr/bin/env python3
"""Read-only network and HTTP probe for a Shimadzu SCL-40 controller.

This tool deliberately sends no authentication payloads, XML bodies, method
changes, pump commands, or other state-changing requests.  It performs TCP
connection checks and plain HTTP GET requests to a small allowlist of likely
read-only discovery endpoints.
"""

from __future__ import annotations

import argparse
import datetime as dt
import socket
import sys
import time
from collections.abc import Iterable
from pathlib import Path

try:
    import requests
    from requests import Response, Session
    from requests.exceptions import RequestException
except ImportError:  # pragma: no cover - provides a clear Windows setup hint
    print(
        "Missing dependency: requests\n"
        "Install it with: python -m pip install requests",
        file=sys.stderr,
    )
    raise SystemExit(2) from None


SAFE_PATHS = (
    "/",
    "/cgi-bin/",
    "/cgi-bin/Login.cgi",
    "/cgi-bin/Config.cgi",
    "/cgi-bin/Monitor.cgi",
)
PREVIEW_CHARS = 1000


class ProbeLogger:
    """Write the same progress messages to the console and a UTF-8 log."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = path.open("w", encoding="utf-8", newline="\n")

    def write(self, message: str = "") -> None:
        try:
            print(message)
        except UnicodeEncodeError:
            # Older instrument pages may contain bytes that cannot be represented
            # by a Korean Windows console (cp949). Preserve the original text in
            # the UTF-8 log and escape only the console copy.
            encoding = sys.stdout.encoding or "utf-8"
            console_safe = message.encode(
                encoding, errors="backslashreplace"
            ).decode(encoding)
            print(console_safe)
        self._file.write(message + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> ProbeLogger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def timestamp() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def check_tcp(host: str, port: int, timeout: float) -> tuple[bool, float, str]:
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            elapsed_ms = (time.perf_counter() - started) * 1000
            return True, elapsed_ms, "connected"
    except OSError as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return False, elapsed_ms, f"{type(exc).__name__}: {exc}"


def decoded_body(response: Response) -> str:
    """Decode a response for inspection while retaining the raw body in the log."""
    if response.encoding is None:
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def log_response(log: ProbeLogger, response: Response, elapsed_ms: float) -> None:
    log.write(f"Status: {response.status_code} {response.reason}")
    log.write(f"Elapsed: {elapsed_ms:.1f} ms")
    log.write(f"Final URL: {response.url}")
    log.write(f"Redirect count: {len(response.history)}")
    for index, item in enumerate(response.history, start=1):
        log.write(
            f"  Redirect {index}: {item.status_code} {item.url} -> "
            f"{item.headers.get('Location', '<missing Location>')}"
        )

    log.write("Response headers:")
    if response.headers:
        for name, value in response.headers.items():
            log.write(f"  {name}: {value}")
    else:
        log.write("  <none>")

    server = response.headers.get("Server", "<not reported>")
    content_type = response.headers.get("Content-Type", "<not reported>")
    log.write(f"Server information: {server}")
    log.write(f"Content-Type: {content_type}")

    if response.cookies:
        log.write("Response cookies:")
        for cookie in response.cookies:
            log.write(
                f"  {cookie.name}={cookie.value}; domain={cookie.domain}; "
                f"path={cookie.path}; secure={cookie.secure}"
            )
    else:
        log.write("Response cookies: <none>")

    body = decoded_body(response)
    preview = body[:PREVIEW_CHARS]
    log.write(f"Body length: {len(response.content)} bytes / {len(body)} characters")
    log.write(f"Body preview (first {PREVIEW_CHARS} characters):")
    log.write(preview if preview else "<empty body>")
    log.write("Full response body:")
    log.write(body if body else "<empty body>")


def probe_url(
    session: Session,
    log: ProbeLogger,
    url: str,
    timeout: float,
    verify_tls: bool,
) -> None:
    log.write()
    log.write("=" * 78)
    log.write(f"REQUEST: GET {url}")
    log.write("Request body: <none>")
    log.write(f"TLS certificate verification: {verify_tls}")
    started = time.perf_counter()
    try:
        response = session.get(
            url,
            timeout=(timeout, timeout),
            allow_redirects=True,
            verify=verify_tls,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        log_response(log, response, elapsed_ms)
    except RequestException as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        log.write(f"ERROR after {elapsed_ms:.1f} ms: {type(exc).__name__}: {exc}")


def probe_scheme(
    session: Session,
    log: ProbeLogger,
    host: str,
    scheme: str,
    paths: Iterable[str],
    timeout: float,
    verify_tls: bool,
) -> None:
    for path in paths:
        probe_url(session, log, f"{scheme}://{host}{path}", timeout, verify_tls)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only TCP/HTTP discovery probe for Shimadzu SCL-40."
    )
    parser.add_argument("ip", help="SCL-40 IPv4 address, for example 192.168.200.99")
    parser.add_argument(
        "--https",
        action="store_true",
        help="also send read-only GET probes over HTTPS when TCP port 443 is open",
    )
    parser.add_argument(
        "--verify-tls",
        action="store_true",
        help="verify HTTPS certificates (off by default for isolated instruments)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="connect/read timeout in seconds (default: 3.0)",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path.cwd(),
        help="directory for the timestamped log (default: current directory)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.timeout <= 0:
        print("--timeout must be greater than zero", file=sys.stderr)
        return 2

    try:
        socket.inet_aton(args.ip)
    except OSError:
        print(f"Invalid IPv4 address: {args.ip}", file=sys.stderr)
        return 2

    args.log_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    safe_ip = args.ip.replace(".", "-")
    log_path = args.log_dir / f"scl40_probe_{safe_ip}_{stamp}.txt"

    with ProbeLogger(log_path) as log:
        log.write("Shimadzu SCL-40 read-only discovery probe")
        log.write(f"Started: {timestamp()}")
        log.write(f"Target: {args.ip}")
        log.write(f"Timeout: {args.timeout:.1f} seconds")
        log.write(f"Safe endpoint allowlist: {', '.join(SAFE_PATHS)}")
        log.write("State-changing requests enabled: NO")

        tcp_results: dict[int, bool] = {}
        for port in (80, 443):
            is_open, elapsed_ms, detail = check_tcp(args.ip, port, args.timeout)
            tcp_results[port] = is_open
            state = "OPEN" if is_open else "CLOSED/UNREACHABLE"
            log.write(
                f"TCP {port}: {state} ({elapsed_ms:.1f} ms; {detail})"
            )

        session = requests.Session()
        # Direct instrument connection: ignore corporate HTTP proxy environment.
        session.trust_env = False
        session.headers.update(
            {
                "User-Agent": "scl40-probe/1.0 (read-only discovery)",
                "Accept": "text/html, application/xml, text/xml, */*",
                "Connection": "close",
            }
        )

        if tcp_results[80]:
            probe_scheme(
                session, log, args.ip, "http", SAFE_PATHS, args.timeout, True
            )
        else:
            log.write("HTTP GET probes skipped because TCP port 80 is not open.")

        if args.https:
            if tcp_results[443]:
                if not args.verify_tls:
                    requests.packages.urllib3.disable_warnings(  # type: ignore[attr-defined]
                        requests.packages.urllib3.exceptions.InsecureRequestWarning
                    )
                probe_scheme(
                    session,
                    log,
                    args.ip,
                    "https",
                    SAFE_PATHS,
                    args.timeout,
                    args.verify_tls,
                )
            else:
                log.write("HTTPS GET probes skipped because TCP port 443 is not open.")
        else:
            log.write("HTTPS GET probes not requested; use --https to enable them.")

        log.write(f"Finished: {timestamp()}")
        log.write(f"Log file: {log_path.resolve()}")

    print(f"\nProbe complete. Log saved to: {log_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
