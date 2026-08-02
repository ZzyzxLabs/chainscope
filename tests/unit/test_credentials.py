"""Credential handling.

The test that matters most is the cache-key one. Hashing a credential leaks
nothing, so the old behaviour looked safe --- and it was. It was also useless,
because it made every cache entry personal to one key, which silently falsified
the promise that a recorded cache replays on someone else's machine with no
credentials. A bug that only shows up as "the fixtures you shipped miss on my
laptop" is one nobody diagnoses.
"""

from typing import ClassVar

import pytest

from chainscope.transport.credentials import (
    PLACEHOLDER,
    Secret,
    endpoint_identity,
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


class TestConcurrency:
    def test_registering_while_scrubbing_does_not_explode(self):
        """Providers register from whichever thread built them, while a sweep
        is already scrubbing responses on others. Iterating a set during a
        write raises --- inside the code whose job is to stop a key reaching a
        log."""
        import threading

        stop = threading.Event()
        errors: list[BaseException] = []

        def churn() -> None:
            try:
                for i in range(400):
                    register_secret(f"rotating-credential-{i:06d}")
                    forget_secret(f"rotating-credential-{i:06d}")
            except BaseException as exc:
                errors.append(exc)
            finally:
                stop.set()

        def scrub() -> None:
            try:
                while not stop.is_set():
                    scrub_value("a response body mentioning nothing in particular")
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=churn), threading.Thread(target=scrub)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not errors


class TestEndpointIdentity:
    """Cache keys must lose the credential and keep the endpoint.

    Full redaction did the first and not the second: an RPC URL registered
    whole as a credential reduced to "<redacted>", so every chain served by one
    provider collapsed onto a single cache entry and an Ethereum query could
    return BSC's answer. Public nodes carrying no credential at all collided
    too, because the URL had been registered regardless.
    """

    PAYLOAD: ClassVar[dict[str, object]] = {
        "jsonrpc": "2.0",
        "method": "eth_getBalance",
        "params": ["0xabc", "latest"],
    }

    def _key(self, url):
        return _request_key("POST", url, self.PAYLOAD)

    @pytest.mark.parametrize(
        ("eth", "bsc"),
        [
            (
                "https://eth.g.alchemy.com/v2/" + "a" * 28,
                "https://bnb.g.alchemy.com/v2/" + "a" * 28,
            ),
            ("https://rpc.ankr.com/eth/" + "a" * 28, "https://rpc.ankr.com/bsc/" + "a" * 28),
            ("https://ethereum-rpc.publicnode.com", "https://bsc-rpc.publicnode.com"),
        ],
    )
    def test_two_chains_never_share_a_cache_entry(self, eth, bsc):
        register_secret(eth)
        register_secret(bsc)
        try:
            assert self._key(eth) != self._key(bsc)
        finally:
            forget_secret(eth)
            forget_secret(bsc)

    @pytest.mark.parametrize(
        "template",
        ["https://rpc.ankr.com/eth/{}", "https://eth.g.alchemy.com/v2/{}"],
    )
    def test_the_same_chain_under_two_credentials_is_one_entry(self, template):
        """The portability guarantee, which the fix must not cost."""
        a, b = template.format("a" * 28), template.format("z" * 28)
        register_secret(a)
        register_secret(b)
        try:
            assert self._key(a) == self._key(b)
        finally:
            forget_secret(a)
            forget_secret(b)

    def test_the_key_carries_no_credential(self):
        secret = "q" * 32
        url = f"https://rpc.example.com/eth/{secret}"
        register_secret(url)
        try:
            assert secret not in endpoint_identity(url)
            assert "rpc.example.com" in endpoint_identity(url)
        finally:
            forget_secret(url)

    def test_short_path_segments_are_not_mistaken_for_keys(self):
        """Chain names and API versions are routes, not credentials."""
        identity = endpoint_identity("https://rpc.example.com/v1/eth/mainnet")
        assert identity.endswith("/v1/eth/mainnet")

    def test_a_query_string_is_dropped_rather_than_scrubbed(self):
        """Callers that build one pass the parameters separately, and those are
        hashed there."""
        assert "apikey" not in endpoint_identity("https://x.example/rpc?apikey=" + "k" * 30)

    def test_something_that_is_not_a_url_is_fully_redacted(self):
        """An unrecognised string is likelier to be a bare credential than an
        endpoint."""
        secret = "n" * 40
        register_secret(secret)
        try:
            assert secret not in endpoint_identity(secret)
        finally:
            forget_secret(secret)
