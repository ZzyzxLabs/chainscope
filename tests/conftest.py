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


#: Address families that reach another machine. Everything else --- Unix
#: sockets, and the socketpair asyncio builds for its own self-pipe --- is
#: local IPC that happens to use the socket API.
_NETWORK_FAMILIES = {socket.AF_INET, socket.AF_INET6}


def _guarded_socket(family: int = socket.AF_INET, *args: Any, **kwargs: Any) -> Any:
    """Allow local sockets, refuse networked ones.

    Blocking ``socket.socket`` outright was the first attempt and it is too
    broad: ``asyncio.run`` creates a socketpair for its own wake-up pipe, so
    every async test failed claiming it had tried to reach the network. The
    guard exists to stop a test depending on a remote host, and a self-pipe
    depends on nothing.
    """
    if family in _NETWORK_FAMILIES:
        raise _refuse()
    return _real_socket(family, *args, **kwargs)


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest) -> Iterator[None]:
    """Disable networked sockets unless the test is marked ``network``."""
    if request.node.get_closest_marker("network"):
        yield
        return
    socket.socket = _guarded_socket  # type: ignore[assignment,misc]
    socket.create_connection = _blocked  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket = _real_socket  # type: ignore[misc]
        socket.create_connection = _real_create_connection


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
