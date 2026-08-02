"""Who may read the label store, decided without binding a socket.

The loopback server answers with ``Access-Control-Allow-Origin`` for any origin
it accepts, so this predicate decides which pages can read a user's
attributions. It used to be a bare ``startswith`` against every configured
value, which is right for one shape and wrong for the other:
``https://etherscan.io`` also admitted ``https://etherscan.io.evil.com``, a
domain anybody can register.

These live outside the socket tests on purpose. Those carry the ``network``
marker and are deselected by default, which is a poor home for an access rule.
"""

from __future__ import annotations

import pytest

from chainscope.server.local import DEFAULT_ORIGINS, origin_allowed


class TestExtensionSchemes:
    """A bare scheme is a prefix on purpose: every extension has its own id."""

    @pytest.mark.parametrize(
        "origin",
        ["chrome-extension://abcdefghijklmnop", "moz-extension://1234-5678"],
    )
    def test_any_extension_id_is_admitted(self, origin):
        assert origin_allowed(origin, DEFAULT_ORIGINS) == origin

    def test_the_bare_scheme_alone_is_not_an_origin(self):
        assert origin_allowed("chrome-extension://", DEFAULT_ORIGINS) is None

    def test_a_different_scheme_is_refused(self):
        assert origin_allowed("https://evil.com", DEFAULT_ORIGINS) is None

    def test_a_scheme_that_merely_starts_the_same_is_refused(self):
        assert origin_allowed("chrome-extension-evil://x", DEFAULT_ORIGINS) is None


class TestHostBearingOrigins:
    """The bug. A configured host matches exactly or not at all."""

    ALLOWED = ("https://etherscan.io",)

    def test_the_exact_origin_is_admitted(self):
        assert origin_allowed("https://etherscan.io", self.ALLOWED) == "https://etherscan.io"

    @pytest.mark.parametrize(
        "origin",
        [
            "https://etherscan.io.evil.com",
            "https://etherscan.io-evil.com",
            "https://etherscan.io@evil.com",
            "https://etherscan.iox",
        ],
    )
    def test_a_lookalike_is_refused(self, origin):
        assert origin_allowed(origin, self.ALLOWED) is None

    def test_a_different_scheme_on_the_same_host_is_refused(self):
        """An http page and an https page are different origins, and the http
        one can be tampered with in transit."""
        assert origin_allowed("http://etherscan.io", self.ALLOWED) is None

    def test_a_subdomain_is_not_the_configured_origin(self):
        assert origin_allowed("https://api.etherscan.io", self.ALLOWED) is None

    def test_a_port_makes_it_a_different_origin(self):
        assert origin_allowed("https://etherscan.io:8443", self.ALLOWED) is None


class TestTheEmptyCases:
    def test_no_origin_header_is_not_an_allowed_origin(self):
        assert origin_allowed("", DEFAULT_ORIGINS) is None

    def test_an_empty_allowlist_admits_nothing(self):
        assert origin_allowed("chrome-extension://abc", ()) is None

    def test_the_two_shapes_coexist(self):
        allowed = ("chrome-extension://", "https://etherscan.io")
        assert origin_allowed("chrome-extension://abc", allowed)
        assert origin_allowed("https://etherscan.io", allowed)
        assert origin_allowed("https://etherscan.io.evil.com", allowed) is None
