"""Test-wide guards.

The important one is the network block. "CI must not touch the network" is easy
to write in a contributing guide and easy to violate by accident --- one helper
that falls back to a live fetch, and the suite starts failing on someone else's
machine for reasons they cannot reproduce. Flaky tests drive contributors away
faster than missing features do.

So the rule is enforced rather than documented: sockets are disabled for the
whole suite. A test that genuinely needs the network must say so explicitly with
``@pytest.mark.network``, and those are deselected by default.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest

_real_socket = socket.socket
_real_create_connection = socket.create_connection
_real_getaddrinfo = socket.getaddrinfo


class NetworkAccessAttempted(RuntimeError):
    """Raised when a test reaches for the network without declaring it."""


def _refuse() -> NetworkAccessAttempted:
    return NetworkAccessAttempted(
        "This test tried to open a network connection.\n"
        "\n"
        "chainscope's suite runs offline so that it is reproducible on any "
        "machine and in CI. Either:\n"
        "  * record a cassette under tests/cassettes/ and replay it, or\n"
        "  * mark the test @pytest.mark.network (deselected by default, and "
        "never run in CI).\n"
    )


def _blocked(*args: Any, **kwargs: Any) -> Any:
    raise _refuse()


#: Hosts that are this machine. A connection to one of these depends on nothing
#: outside the test run, which is the only property this guard cares about.
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0", "", "::"})


def _is_local(address: Any) -> bool:
    """Whether an address refers to this machine.

    Anything that is not a host/port tuple --- a Unix socket path, an
    ``AF_NETLINK`` address --- is local by construction.
    """
    if not isinstance(address, tuple) or not address:
        return True
    host = address[0]
    if not isinstance(host, str):
        return False
    return host.strip("[]") in _LOOPBACK


class _GuardedSocket(_real_socket):  # type: ignore[misc,valid-type]
    """A socket that can be created freely but only connected locally.

    Two earlier attempts were both wrong, and the way they were wrong is worth
    keeping written down.

    Blocking ``socket.socket`` outright broke every async test: ``asyncio.run``
    builds a self-pipe, and a self-pipe reaches nobody.

    Allowing everything except ``AF_INET``/``AF_INET6`` fixed that on Unix and
    broke Windows, where the proactor event loop's self-pipe *is* an AF_INET
    socket over loopback. The family says nothing about whether a connection
    leaves the machine.

    The destination does, and it is only known at ``connect`` time --- so that
    is where the check belongs.
    """

    def connect(self, address: Any) -> None:
        if not _is_local(address):
            raise _refuse()
        super().connect(address)

    def connect_ex(self, address: Any) -> int:
        if not _is_local(address):
            raise _refuse()
        return int(super().connect_ex(address))

    def sendto(self, data: Any, *args: Any) -> int:
        """Datagrams carry their destination per call.

        An unconnected UDP socket never touches `connect`, so guarding that
        alone leaves a route out: `sendto(payload, ("8.8.8.8", 53))` would have
        gone straight through.
        """
        destination = args[-1] if args else None
        if destination is not None and not _is_local(destination):
            raise _refuse()
        return int(super().sendto(data, *args))

    def sendmsg(self, *args: Any, **kwargs: Any) -> int:
        """The same route as `sendto`, and it was open.

        `sendmsg(buffers, ancdata, flags, address)` carries its destination in
        the fourth positional argument. The reasoning written above for
        `sendto` --- an unconnected socket never touches `connect`, so guarding
        that alone leaves a route out --- applies here unchanged, and this one
        was not guarded. Measured: `sendmsg` to 8.8.8.8 went straight through.
        """
        destination = args[3] if len(args) > 3 else kwargs.get("address")
        if destination is not None and not _is_local(destination):
            raise _refuse()
        return int(super().sendmsg(*args, **kwargs))


def _guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
    if not _is_local(address):
        raise _refuse()
    return _real_create_connection(address, *args, **kwargs)


def _guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
    """Resolution is network access, and it happens before any connect.

    A test that resolved `example.com` reached a DNS server and the guard never
    saw it --- `connect` is where the *destination* is known, and a lookup has
    already left the machine by then. Measured: `getaddrinfo("example.com")`
    escaped.

    Loopback names still resolve, because the local server tests need them and
    `localhost` reaches nobody.
    """
    if not _is_local((host, 0)):
        raise _refuse()
    return _real_getaddrinfo(host, *args, **kwargs)


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest) -> Iterator[None]:
    """Disable networked sockets unless the test is marked ``network``."""
    if request.node.get_closest_marker("network"):
        yield
        return
    socket.socket = _GuardedSocket  # type: ignore[assignment,misc]
    socket.create_connection = _guarded_create_connection  # type: ignore[assignment]
    socket.getaddrinfo = _guarded_getaddrinfo  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket = _real_socket  # type: ignore[misc]
        socket.create_connection = _real_create_connection
        socket.getaddrinfo = _real_getaddrinfo


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect network tests unless ``--network`` was passed."""
    if config.getoption("--network"):
        return
    skip = pytest.mark.skip(reason="needs --network (never enabled in CI)")
    for item in items:
        if item.get_closest_marker("network"):
            item.add_marker(skip)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--network",
        action="store_true",
        default=False,
        help="run tests marked @pytest.mark.network against live APIs",
    )
