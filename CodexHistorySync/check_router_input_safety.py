"""Router input and telemetry safety checks with no live config, ports, or credentials."""

from __future__ import annotations

from collections import deque
from email.message import Message
from io import BytesIO
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_sota_router import (  # noqa: E402
    MAX_REQUEST_BODY_BYTES,
    RouterState,
    SotaRouterHandler,
)


class BrokenLogPath:
    @property
    def parent(self) -> "BrokenLogPath":
        return self

    def mkdir(self, **_kwargs: object) -> None:
        return

    def open(self, *_args: object, **_kwargs: object) -> object:
        raise RuntimeError("synthetic telemetry failure")


def read_body(*headers: tuple[str, str], body: bytes = b"") -> tuple[bytes | None, list[tuple[int, object]]]:
    handler = SotaRouterHandler.__new__(SotaRouterHandler)
    message = Message()
    for name, value in headers:
        message.add_header(name, value)
    handler.headers = message
    handler.rfile = BytesIO(body)
    responses: list[tuple[int, object]] = []
    handler._json_response = lambda status, payload: responses.append((status, payload))
    return handler._read_request_body(), responses


def check(label: str, condition: bool) -> bool:
    print(f"  {'PASS' if condition else 'FAIL'} {label}")
    return condition


def main() -> int:
    results: list[bool] = []

    value, responses = read_body(("Content-Length", "3"), body=b"abc")
    results.append(check("valid Content-Length is read exactly", value == b"abc" and not responses))

    for label, header in (
        ("non-numeric Content-Length returns 400", "abc"),
        ("negative Content-Length returns 400", "-1"),
        ("signed Content-Length returns 400", "+1"),
    ):
        value, responses = read_body(("Content-Length", header), body=b"ignored")
        results.append(check(label, value is None and responses and responses[0][0] == 400))

    value, responses = read_body(
        ("Content-Length", "1"), ("Content-Length", "1"), body=b"xx"
    )
    results.append(
        check("duplicate Content-Length returns 400", value is None and responses[0][0] == 400)
    )

    value, responses = read_body(
        ("Content-Length", str(MAX_REQUEST_BODY_BYTES + 1)), body=b""
    )
    results.append(
        check("oversized Content-Length returns 413", value is None and responses[0][0] == 413)
    )

    value, responses = read_body(("Transfer-Encoding", "chunked"), body=b"0\r\n\r\n")
    results.append(
        check("unsupported chunked request returns 400", value is None and responses[0][0] == 400)
    )

    value, responses = read_body(("Content-Length", "4"), body=b"abc")
    results.append(
        check("truncated request body returns 400", value is None and responses[0][0] == 400)
    )

    state = RouterState.__new__(RouterState)
    state.log_path = BrokenLogPath()
    state.requests = 0
    state.recent = {}
    state.lock = threading.Lock()
    state._log_lock = threading.Lock()
    state.record("vendor", "POST", "/responses", 200, 0.01)
    results.append(
        check(
            "arbitrary telemetry failures do not escape or lose health accounting",
            state.requests == 1 and state.recent["vendor"] == deque([True], maxlen=5),
        )
    )

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
