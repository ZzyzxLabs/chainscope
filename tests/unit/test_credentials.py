"""Credential handling.

The test that matters most is the cache-key one. Hashing a credential leaks
nothing, so the old behaviour looked safe --- and it was. It was also useless,
because it made every cache entry personal to one key, which silently falsified
the promise that a recorded cache replays on someone else's machine with no
credentials. A bug that only shows up as "the fixtures you shipped miss on my
laptop" is one nobody diagnoses.
"""

import pytest

from chainscope.transport.credentials import (
    PLACEHOLDER,
    Secret,
    forget_secret,
    redact,
    redact_headers,
    register_secret,
    scrub_params,
    scrub_value,
)
from chainscope.transport.http import _request_key

URL = "https://api.etherscan.io/v2/api"
QUERY = {"chainid": 1, "module": "account", "action": "txlist", "address": "0xabc"}


class TestCacheKeyPortability:
    def test_same_query_different_key_is_one_entry(self):
        """The whole reason a shipped cache is worth shipping."""
        a = _request_key("GET", URL, {**QUERY, "apikey": "AAAAAAAAAAAAAAAA"})
        b = _request_key("GET", URL, {**QUERY, "apikey": "BBBBBBBBBBBBBBBB"})
        assert a == b

    def test_no_credential_at_all_still_matches(self):
        """Replay has to work for someone who never had a key."""
        a = _request_key("GET", URL, {**QUERY, "apikey": "AAAAAAAAAAAAAAAA"})
        assert _request_key("GET", URL, {**QUERY, "apikey": ""}) == a

    def test_different_questions_still_differ(self):
        """Scrubbing must not collapse genuinely distinct requests."""
        a = _request_key("GET", URL, {**QUERY, "apikey": "K"})
        b = _request_key("GET", URL, {**QUERY, "address": "0xdef", "apikey": "K"})
        assert a != b

    def test_pagination_is_still_part_of_the_question(self):
        a = _request_key("GET", URL, {**QUERY, "offset": 10})
        b = _request_key("GET", URL, {**QUERY, "offset": 20})
        assert a != b

    def test_key_embedded_in_the_url_path(self):
        """Alchemy and Helius put it there rather than in a query string."""
        a = _request_key("POST", "https://eth.g.alchemy.com/v2/" + "a" * 32, {"m": 1})
        b = _request_key("POST", "https://eth.g.alchemy.com/v2/" + "b" * 32, {"m": 1})
        assert a == b

    def test_different_hosts_do_not_collide(self):
        a = _request_key("POST", "https://eth.g.alchemy.com/v2/" + "a" * 32, {"m": 1})
        b = _request_key("POST", "https://bsc.g.alchemy.com/v2/" + "a" * 32, {"m": 1})
        assert a != b


class TestScrubbing:
    @pytest.mark.parametrize(
        "name", ["apikey", "api_key", "API_KEY", "token", "auth", "secret", "signature"]
    )
    def test_known_parameter_names(self, name):
        assert scrub_params({name: "hunter2hunter2"})[name] == PLACEHOLDER

    def test_ordinary_values_survive(self):
        params = {"address": "0xabc", "startblock": 0, "sort": "asc"}
        assert scrub_params(params) == params

    def test_nested_payloads(self):
        """A credential sits inside a JSON-RPC body as easily as a query string."""
        got = scrub_params({"params": [{"apikey": "aaaaaaaaaaaaaaaa"}], "method": "eth_call"})
        assert got["params"][0]["apikey"] == PLACEHOLDER
        assert got["method"] == "eth_call"

    def test_url_query_string(self):
        assert "hunter2" not in redact(f"{URL}?apikey=hunter2hunter2&address=0xabc")

    def test_headers(self):
        got = redact_headers({"Authorization": "Bearer xyz", "Accept": "application/json"})
        assert got["Authorization"] == PLACEHOLDER
        assert got["Accept"] == "application/json"

    def test_ordinary_path_segments_survive(self):
        """`/v2/api` must not be mistaken for `/v2/<key>`."""
        assert redact(URL) == URL


class TestRegistry:
    def test_a_registered_value_is_scrubbed_anywhere(self):
        """The catch-all for shapes the patterns do not anticipate --- a key
        echoed back inside an error string, for instance."""
        secret = "zzzz-9999-zzzz-9999"
        register_secret(secret)
        try:
            assert secret not in scrub_value(f"invalid key: {secret}, try again")
        finally:
            forget_secret(secret)

    def test_short_values_are_not_registered(self):
        """Blindly replacing a short string would corrupt unrelated data."""
        register_secret("abc")
        try:
            assert scrub_value("abc123") == "abc123"
        finally:
            forget_secret("abc")

    def test_longest_match_wins(self):
        """An endpoint URL contains the key it embeds. Replacing the shorter one
        first leaves a remainder the longer entry no longer matches."""
        key = "k" * 32
        url = f"https://eth.example/v2/{key}"
        register_secret(key)
        register_secret(url)
        try:
            assert scrub_value(f"failed against {url}") == f"failed against {PLACEHOLDER}"
        finally:
            forget_secret(key)
            forget_secret(url)


class TestSecret:
    def test_does_not_print_itself(self):
        s = Secret("supersecretvalue", "etherscan")
        try:
            assert "supersecretvalue" not in repr(s)
            assert "supersecretvalue" not in str(s)
            assert "supersecretvalue" not in f"{s}"
        finally:
            forget_secret("supersecretvalue")

    def test_reveal_is_explicit(self):
        s = Secret("supersecretvalue", "etherscan")
        try:
            assert s.reveal() == "supersecretvalue"
        finally:
            forget_secret("supersecretvalue")

    def test_empty_is_falsey(self):
        assert not Secret("", "unset")
        assert Secret("something-long-enough", "set")
        forget_secret("something-long-enough")

    def test_hint_distinguishes_two_configured_keys(self):
        """ "set" cannot tell the key you meant from the expired one."""
        a, b = Secret("aaaaaaaaaaaa1234"), Secret("bbbbbbbbbbbb5678")
        try:
            assert a.hint() != b.hint()
            assert a.hint() == "...1234"
        finally:
            forget_secret(a.reveal())
            forget_secret(b.reveal())

    def test_constructing_one_registers_it(self):
        s = Secret("registered-on-construction", "x")
        try:
            assert s.reveal() not in scrub_value(f"leaked {s.reveal()}")
        finally:
            forget_secret(s.reveal())
