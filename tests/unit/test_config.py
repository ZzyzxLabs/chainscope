"""Settings loading.

Two behaviours carry real weight. Environment must beat ``.env``, because CI
and container runtimes inject configuration that way and a checked-out file
winning against them is a debugging session nobody enjoys. And a missing
credential must not fail until something needs it, or the offline suite becomes
unrunnable without keys --- which would defeat the point of committing
cassettes at all.
"""

import pytest

from chainscope.config import ENV_KEYS, ConfigError, Settings, load_dotenv
from chainscope.transport.credentials import forget_secret


@pytest.fixture
def env_file(tmp_path):
    def write(text: str):
        path = tmp_path / ".env"
        path.write_text(text)
        return path

    return write


class TestDotenvParsing:
    def test_basic_pairs(self, env_file):
        assert load_dotenv(env_file("A=1\nB=two\n")) == {"A": "1", "B": "two"}

    def test_comments_and_blanks_are_skipped(self, env_file):
        got = load_dotenv(env_file("# a comment\n\nA=1\n   \n"))
        assert got == {"A": "1"}

    def test_export_prefix(self, env_file):
        assert load_dotenv(env_file("export A=1\n")) == {"A": "1"}

    def test_quotes_are_stripped(self, env_file):
        assert load_dotenv(env_file("A='x'\nB=\"y\"\n")) == {"A": "x", "B": "y"}

    def test_unbalanced_quote_is_left_alone(self, env_file):
        """Half-stripping would silently corrupt the value."""
        assert load_dotenv(env_file('A="x\n'))["A"] == '"x'

    def test_values_containing_equals_survive(self, env_file):
        """Base64 and URLs both contain them."""
        assert load_dotenv(env_file("A=a=b=c\n"))["A"] == "a=b=c"

    def test_missing_file_is_empty(self, tmp_path):
        assert load_dotenv(tmp_path / "nope.env") == {}

    def test_it_never_mutates_the_process_environment(self, env_file, monkeypatch):
        """A library that edits os.environ on import breaks its host's tests."""
        import os

        monkeypatch.delenv("CHAINSCOPE_TEST_ONLY", raising=False)
        load_dotenv(env_file("CHAINSCOPE_TEST_ONLY=1\n"))
        assert "CHAINSCOPE_TEST_ONLY" not in os.environ

    def test_it_searches_upward(self, tmp_path):
        """A CLI run from inside a case folder still finds the project's .env."""
        (tmp_path / ".env").write_text("A=found\n")
        nested = tmp_path / "cases" / "2026-01"
        nested.mkdir(parents=True)
        assert load_dotenv(search_from=nested) == {"A": "found"}


class TestPrecedence:
    def test_environment_beats_dotenv(self, env_file):
        path = env_file("ETHERSCAN_API_KEY=from-the-file\n")
        s = Settings.load({"ETHERSCAN_API_KEY": "from-the-environment"}, dotenv=path)
        try:
            assert s.key("etherscan").reveal() == "from-the-environment"
        finally:
            forget_secret("from-the-environment")
            forget_secret("from-the-file")

    def test_dotenv_fills_what_the_environment_lacks(self, env_file):
        path = env_file("ETHERSCAN_API_KEY=from-the-file\n")
        s = Settings.load({}, dotenv=path)
        try:
            assert s.key("etherscan").reveal() == "from-the-file"
        finally:
            forget_secret("from-the-file")

    def test_an_empty_environment_variable_does_not_mask_the_file(self, env_file):
        """`FOO=` in a shell profile is absence, not an instruction to unset."""
        path = env_file("ETHERSCAN_API_KEY=from-the-file\n")
        s = Settings.load({"ETHERSCAN_API_KEY": ""}, dotenv=path)
        try:
            assert s.key("etherscan").reveal() == "from-the-file"
        finally:
            forget_secret("from-the-file")


class TestCredentials:
    def test_absence_is_not_an_error(self, tmp_path):
        """Import-time failure would make the offline suite need keys."""
        s = Settings.load({}, dotenv=tmp_path / "none.env")
        assert not s.has("etherscan")
        assert s.configured() == []

    def test_require_names_the_variable_and_where_to_get_one(self, tmp_path):
        s = Settings.load({}, dotenv=tmp_path / "none.env")
        with pytest.raises(ConfigError) as exc:
            s.require("etherscan")
        assert "ETHERSCAN_API_KEY" in str(exc.value)
        assert "https://" in str(exc.value)

    def test_unknown_provider_is_rejected(self, tmp_path):
        s = Settings.load({}, dotenv=tmp_path / "none.env")
        with pytest.raises(ConfigError, match="unknown provider"):
            s.key("nosuchservice")

    def test_summary_carries_no_credential(self, env_file):
        path = env_file("ETHERSCAN_API_KEY=supersecretvalue9999\n")
        s = Settings.load({}, dotenv=path)
        try:
            assert "supersecretvalue9999" not in str(s.summary())
            assert s.summary()["etherscan"] == "...9999"
        finally:
            forget_secret("supersecretvalue9999")

    def test_the_settings_object_does_not_print_its_keys(self, env_file):
        path = env_file("ETHERSCAN_API_KEY=supersecretvalue9999\n")
        s = Settings.load({}, dotenv=path)
        try:
            assert "supersecretvalue9999" not in repr(s)
        finally:
            forget_secret("supersecretvalue9999")

    def test_every_known_provider_has_a_source_url(self):
        """An error saying "set ETHERSCAN_API_KEY" and stopping has told the
        reader the one thing they already knew."""
        for _var, where in ENV_KEYS.values():
            assert where.startswith("https://")


class TestRpcEndpoints:
    def test_endpoints_are_read_by_name(self, env_file):
        path = env_file("CHAINSCOPE_RPC_ETHEREUM=https://eth.example\n")
        s = Settings.load({}, dotenv=path)
        assert s.rpc_for("ethereum") == "https://eth.example"
        assert s.rpc_for("ETHEREUM") == "https://eth.example"

    def test_an_unconfigured_chain_is_none(self, tmp_path):
        assert Settings.load({}, dotenv=tmp_path / "none.env").rpc_for("bsc") is None

    def test_chains_are_not_limited_to_a_fixed_list(self, env_file):
        """Adding a chain must not require editing this package."""
        path = env_file("CHAINSCOPE_RPC_SUI=https://sui.example\n")
        assert Settings.load({}, dotenv=path).rpc_for("sui") == "https://sui.example"

    def test_an_endpoint_embedding_a_key_is_registered_for_scrubbing(self, env_file):
        """Alchemy and Helius put the key in the path, so the URL itself is a
        credential and must not reach a cassette or an audit log."""
        from chainscope.transport.credentials import scrub_value

        url = "https://eth-mainnet.g.alchemy.com/v2/aaaaaaaaaaaaaaaaaaaaaaaa"
        path = env_file(f"CHAINSCOPE_RPC_ETHEREUM={url}\n")
        Settings.load({}, dotenv=path)
        try:
            assert url not in scrub_value(f"failed against {url}")
        finally:
            forget_secret(url)


class TestNumbers:
    def test_defaults(self, tmp_path):
        s = Settings.load({}, dotenv=tmp_path / "none.env")
        assert s.rate_limit == 5.0
        assert s.timeout == 30.0

    def test_values_are_read(self, env_file):
        path = env_file("CHAINSCOPE_RATE_LIMIT=2\nCHAINSCOPE_TIMEOUT=45\n")
        s = Settings.load({}, dotenv=path)
        assert (s.rate_limit, s.timeout) == (2.0, 45.0)

    def test_a_typo_is_loud(self, env_file):
        """Falling back to the default would hide the typo behind an
        unexplained change in behaviour: a rate limit that silently reverts to
        5/s looks like the remote being slow, not like a broken config line."""
        path = env_file("CHAINSCOPE_RATE_LIMIT=fast\n")
        with pytest.raises(ConfigError, match="not a number"):
            Settings.load({}, dotenv=path)
