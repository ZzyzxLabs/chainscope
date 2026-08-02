"""The suite's own network guard.

Worth testing, because it has been loosened twice to unbreak async tests and a
guard that has been loosened without a test is a guard that quietly stops
guarding. These assert both halves: local connections work, remote ones do not.

Neither test opens a real connection. A refusal is raised before any syscall,
and the local cases fail with a connection error from the OS --- which is proof
the guard let them through, and is the only outcome available without a
listener.
"""

import socket

import pytest

from tests.conftest import NetworkAccessAttempted, _is_local


class TestWhatCountsAsLocal:
    @pytest.mark.parametrize(
        "address",
        [
            ("127.0.0.1", 8080),
            ("::1", 8080),
            ("localhost", 5432),
            ("0.0.0.0", 0),
            ("[::1]", 9000),
        ],
    )
    def test_loopback_is_local(self, address):
        assert _is_local(address)

    @pytest.mark.parametrize(
        "address",
        [
            ("api.etherscan.io", 443),
            ("8.8.8.8", 53),
            ("1.1.1.1", 443),
            ("2606:4700::1111", 443),
        ],
    )
    def test_remote_hosts_are_not(self, address):
        assert not _is_local(address)

    def test_a_unix_socket_path_is_local(self):
        """Not a host/port tuple, so it reaches nothing outside this machine."""
        assert _is_local("/tmp/some.sock")

    def test_an_empty_address_is_local(self):
        assert _is_local(())


class TestTheGuardStillGuards:
    def test_a_remote_connect_is_refused(self):
        """The behaviour the whole fixture exists for."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(NetworkAccessAttempted, match="cassette"):
                s.connect(("api.etherscan.io", 443))
        finally:
            s.close()

    def test_a_remote_create_connection_is_refused(self):
        with pytest.raises(NetworkAccessAttempted):
            socket.create_connection(("api.etherscan.io", 443), timeout=0.1)

    def test_connect_ex_is_guarded_too(self):
        """It is the quieter sibling and returns an error code rather than
        raising, so a provider using it would slip past a guard on connect
        alone."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(NetworkAccessAttempted):
                s.connect_ex(("8.8.8.8", 53))
        finally:
            s.close()

    def test_the_message_says_how_to_proceed(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(NetworkAccessAttempted) as exc:
                s.connect(("example.com", 80))
        finally:
            s.close()
        assert "record a cassette" in str(exc.value)
        assert "pytest.mark.network" in str(exc.value)


class TestLocalStillWorks:
    def test_creating_an_inet_socket_is_allowed(self):
        """Windows' proactor event loop builds one for its own self-pipe, so
        refusing at construction breaks every async test on that platform."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.close()

    def test_a_loopback_connect_is_not_refused(self):
        """It fails with a connection error because nothing is listening ---
        which is precisely the proof that the guard did not intervene."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.2)
        try:
            with pytest.raises(OSError) as exc:
                s.connect(("127.0.0.1", 1))
            assert not isinstance(exc.value, NetworkAccessAttempted)
        finally:
            s.close()

    def test_asyncio_can_run(self):
        """The regression that started all of this."""
        import asyncio

        async def nothing() -> int:
            return 42

        assert asyncio.run(nothing()) == 42
