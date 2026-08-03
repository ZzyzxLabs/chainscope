"""Test-wide guards.

The important one is the network block. "CI must not touch the network" is easy
to write in a contributing guide and easy to violate by accident --- one helper
that falls back to a live fetch, and the suite starts failing on someone else's
machine for reasons they cannot reproduce. Flaky tests drive contributors away
faster than missing features do.

So the rule is enforced rather than documented: sockets are disabled for the
whole suite. A test that genuinely needs the network must say so explicitly with
``@pytest.mark.network``, and those are deselected by default.

**Installed when this file is imported, not when a test starts.** It used to go
on in an autouse fixture, which covers a test's body and nothing else. Measured,
five things went straight out:

* module-level code in a test file, which runs at collection
* a ``session``-scoped fixture
* a ``module``-scoped fixture
* ``from socket import getaddrinfo`` --- the name was bound before the fixture
  replaced the module attribute, so the guard never saw it
* ``from socket import socket``, likewise, and then ``.connect`` on it

The last two are the reason the guard patches **methods on the socket class**
rather than swapping ``socket.socket`` for a subclass. A caller holding the
class object directly --- which is what a from-import is, and what several HTTP
libraries do at import time --- kept the real ``connect``. Patching the class in
place leaves nowhere to hold a reference to.

That inverts the fixture's job: the guard is on by default and the fixture
*lifts* it for a test marked ``network``.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_sendto = socket.socket.sendto
_real_sendmsg = socket.socket.sendmsg
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


# Free functions bound onto `socket.socket` below. Written as functions rather
# than as a subclass because a subclass only guards code that goes through
# `socket.socket` *at call time*, and a from-import does not.
#
# Two earlier attempts were both wrong, and the way they were wrong is worth
# keeping written down.
#
# Blocking `socket.socket` outright broke every async test: `asyncio.run` builds
# a self-pipe, and a self-pipe reaches nobody.
#
# Allowing everything except AF_INET/AF_INET6 fixed that on Unix and broke
# Windows, where the proactor event loop's self-pipe *is* an AF_INET socket over
# loopback. The family says nothing about whether a connection leaves the
# machine. The destination does, and it is only known at `connect` time.


def _connect(self: Any, address: Any) -> None:
    if not _is_local(address):
        raise _refuse()
    _real_connect(self, address)


def _connect_ex(self: Any, address: Any) -> int:
    if not _is_local(address):
        raise _refuse()
    return int(_real_connect_ex(self, address))


def _sendto(self: Any, data: Any, *args: Any) -> int:
    """Datagrams carry their destination per call.

    An unconnected UDP socket never touches `connect`, so guarding that alone
    leaves a route out: `sendto(payload, ("8.8.8.8", 53))` would have gone
    straight through.
    """
    destination = args[-1] if args else None
    if destination is not None and not _is_local(destination):
        raise _refuse()
    return int(_real_sendto(self, data, *args))


def _sendmsg(self: Any, *args: Any, **kwargs: Any) -> int:
    """The same route as `sendto`, and it was open.

    `sendmsg(buffers, ancdata, flags, address)` carries its destination in the
    fourth positional argument. The reasoning written above for `sendto` applies
    here unchanged, and this one was not guarded. Measured: `sendmsg` to 8.8.8.8
    went straight through.
    """
    destination = args[3] if len(args) > 3 else kwargs.get("address")
    if destination is not None and not _is_local(destination):
        raise _refuse()
    return int(_real_sendmsg(self, *args, **kwargs))


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


#: Set on the `socket` module, not here, so a *second copy* of this file loaded
#: under a second module name can see it. That happened: a test did `import
#: conftest` while pytest had already loaded the same file as `tests.conftest`,
#: and the second copy re-ran `_install()` --- capturing the first copy's
#: guarded functions as its "real" ones, and raising its own, different
#: `NetworkAccessAttempted` that no caller could catch. Harmless while the guard
#: lived in a fixture; not once import installs it.
_INSTALLED = "_chainscope_network_guard"


def _install() -> None:
    if getattr(socket, _INSTALLED, None) not in (None, __name__):
        raise RuntimeError(
            f"the network guard is already installed by {getattr(socket, _INSTALLED)!r}. "
            f"This file has been imported twice under two names --- import it as "
            f"`tests.conftest`, not `conftest`, so there is one guard and one "
            f"exception class."
        )
    setattr(socket, _INSTALLED, __name__)
    socket.socket.connect = _connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _connect_ex  # type: ignore[method-assign]
    socket.socket.sendto = _sendto  # type: ignore[method-assign]
    socket.socket.sendmsg = _sendmsg  # type: ignore[method-assign]
    socket.create_connection = _guarded_create_connection  # type: ignore[assignment]
    socket.getaddrinfo = _guarded_getaddrinfo  # type: ignore[assignment]


def _lift() -> None:
    socket.socket.connect = _real_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _real_connect_ex  # type: ignore[method-assign]
    socket.socket.sendto = _real_sendto  # type: ignore[method-assign]
    socket.socket.sendmsg = _real_sendmsg  # type: ignore[method-assign]
    socket.create_connection = _real_create_connection
    socket.getaddrinfo = _real_getaddrinfo


# At import, so that collection, module-level code and every fixture scope are
# covered --- and so that a test module's own `from socket import ...` binds the
# guarded names. See the module docstring for what was escaping.
_install()


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest) -> Iterator[None]:
    """Lift the guard for a test marked ``network``; leave it on otherwise."""
    if not request.node.get_closest_marker("network"):
        yield
        return
    _lift()
    try:
        yield
    finally:
        _install()


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
