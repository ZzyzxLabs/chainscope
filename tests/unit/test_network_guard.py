"""The suite's own network guard.

Worth testing, because it has been loosened twice to unbreak async tests and a
guard that has been loosened without a test is a guard that quietly stops
guarding. These assert both halves: local connections work, remote ones do not.

Neither test opens a real connection. A refusal is raised before any syscall,
and the local cases fail with a connection error from the OS --- which is proof
the guard let them through, and is the only outcome available without a
listener.
"""

import pathlib
import socket

import pytest

from tests.conftest import NetworkAccessAttempted, _is_local


def _try_to_resolve() -> BaseException | None:
    """Reach for the network and report what stopped it, if anything."""
    try:
        socket.getaddrinfo("example.com", 80)
    except BaseException as exc:
        # Deliberately broad: which exception it is *is* the assertion, and
        # narrowing to NetworkAccessAttempted would make "nothing stopped it"
        # and "the guard stopped it" both look like a pass.
        return exc
    return None


#: Evaluated while this module is being imported --- that is, during collection,
#: before any fixture has run. It escaped the old guard.
_AT_IMPORT = _try_to_resolve()


@pytest.fixture(scope="session")
def _resolution_from_a_session_fixture() -> BaseException | None:
    """Set up before any function-scoped fixture, so it escaped the old guard."""
    return _try_to_resolve()


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


class TestDatagramsAreGuardedToo:
    def test_sendto_a_remote_host_is_refused(self):
        """An unconnected UDP socket never touches connect, so guarding that
        alone leaves a route straight out."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            with pytest.raises(NetworkAccessAttempted):
                s.sendto(b"probe", ("8.8.8.8", 53))
        finally:
            s.close()

    def test_sendto_loopback_is_allowed(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.sendto(b"probe", ("127.0.0.1", 9))  # discard port; nothing listens
        except OSError as exc:
            assert not isinstance(exc, NetworkAccessAttempted)
        finally:
            s.close()


class TestTheGuardCoversMoreThanATestBody:
    """It was installed by an autouse fixture, which covers a test's body only.

    Measured, five things went straight out: module-level code in a test file
    (which runs at collection), a session-scoped fixture, a module-scoped
    fixture, `from socket import getaddrinfo`, and `from socket import socket`
    followed by `.connect`.

    The last two are why the guard patches methods **on the socket class** now
    rather than swapping `socket.socket` for a subclass. A caller holding the
    class object directly --- which is what a from-import is, and what several
    HTTP libraries do at import time --- kept the real `connect`, so a provider
    test that quietly fell back to a live fetch would have passed.
    """

    def test_a_name_bound_before_the_test_started_is_still_guarded(self) -> None:
        from socket import getaddrinfo as bound_at_import

        with pytest.raises(NetworkAccessAttempted):
            bound_at_import("example.com", 80)

    def test_the_socket_class_itself_is_guarded(self) -> None:
        from socket import socket as bound_at_import

        sock = bound_at_import()
        try:
            with pytest.raises(NetworkAccessAttempted):
                sock.connect(("1.1.1.1", 80))
        finally:
            sock.close()

    def test_a_session_scoped_fixture_is_guarded(
        self, _resolution_from_a_session_fixture: BaseException | None
    ) -> None:
        """The test that actually distinguishes the timing fix.

        A function-scoped autouse fixture cannot cover a session-scoped one ---
        the wider scope is set up first. So a session fixture that fetched went
        out, and every test depending on it inherited whatever it got.
        """
        assert isinstance(_resolution_from_a_session_fixture, NetworkAccessAttempted)

    def test_module_level_code_is_guarded(self) -> None:
        # Module bodies run at collection, before any fixture. `_AT_IMPORT` was
        # evaluated at the top of this file.
        assert isinstance(_AT_IMPORT, NetworkAccessAttempted)

    def test_a_second_copy_of_this_file_refuses_rather_than_shadowing(self) -> None:
        """Loading the same file under a second module name used to rebind the
        socket methods to the second copy's functions, whose `NetworkAccess-
        Attempted` is a different class --- so every later `pytest.raises` on it
        stopped catching. It is now an error that says what to import instead."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "conftest_second_copy", pathlib.Path(__file__).parent.parent / "conftest.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        with pytest.raises(RuntimeError, match="imported twice"):
            spec.loader.exec_module(module)

    def test_the_guard_still_lets_loopback_through(self) -> None:
        # Everything above would also be satisfied by a guard that blocks
        # everything, which would break the local-server tests.
        assert socket.getaddrinfo("127.0.0.1", 80)
