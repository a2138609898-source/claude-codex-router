"""Loopback ports for the check scripts, handed out by the OS instead of hardcoded.

Every router fixture used to pin 17896-17898, and three separate scripts pinned the *same*
three. That breaks in the one way that is invisible: on Windows a socket with SO_REUSEADDR --
which ThreadingHTTPServer sets by default -- binds on top of an existing listener, and which of
the two receives a given connection is undefined. So a leftover shadow router, or a second check
running at the same time, raises nothing at all. The fake upstream simply never sees a request,
the hit list stays empty, and every scenario fails for a reason that has nothing to do with what
is under test -- then it all "passes again" once the other process exits. Chasing that costs an
afternoon, twice, because there is no error message anywhere.

Letting the OS pick removes the collision instead of reporting it, and lets the sibling checks
run side by side.
"""

import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def reserve_port() -> int:
    """A port the OS says is free right now, for handing to a child process.

    Racy by construction -- the probe socket has to close before the child can bind -- so
    callers that hand this to a router still have to confirm the thing that answers is their
    own child. Callers that own the listener themselves should use serve_on_free_port, which
    has no window at all.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def serve_on_free_port(
    handler: type[BaseHTTPRequestHandler],
) -> tuple[ThreadingHTTPServer, int]:
    """Bind a fixture server wherever the OS allows, and report where that turned out to be."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    return server, int(server.server_address[1])
