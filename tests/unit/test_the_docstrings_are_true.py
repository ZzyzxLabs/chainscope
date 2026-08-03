"""The load-bearing promises in this session's modules, checked against the code.

Every claim here is quoted from a docstring somebody would act on. They are
gathered in one file because the failure they guard against has a shape: the
documentation and the behaviour drift apart, and only the documentation gets
read. This session found it three times ---

* `case/leads.py` said the record of who checked is the most valuable thing
  here, and `settle` silently overwrote it;
* `analysis/poisoning.py` said three signals distinguish the real address, and
  only one entered the verdict;
* `analysis/impersonation.py` said *compare the contract, never the symbol
  string*, and `trusted_assets` --- inside that very module --- asked by symbol,
  so a real contract whose symbol a provider omitted became untrusted and every
  route through it was reported as the attacker's fabrication.

The third was found by writing this audit, not by any test that existed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from chainscope.analysis.impersonation import report, trusted_assets
from chainscope.analysis.poisoning import chance_of_collision, find_lookalikes
from chainscope.analysis.route import find_routes, hubs_in
from chainscope.analysis.route import findings as route_findings
from chainscope.core.chainid import ETHEREUM
from chainscope.core.confusable import confusable
from chainscope.osint.leads import Lead

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
REAL_USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


def _t(sender, recipient, minutes, asset=REAL_USDC, symbol="USDC", raw=100):
    return SimpleNamespace(
        chain=ETHEREUM,
        sender=SimpleNamespace(key=sender),
        recipient=SimpleNamespace(key=recipient),
        timestamp=T0 + timedelta(minutes=minutes),
        asset=SimpleNamespace(key=asset) if asset else None,
        amount=SimpleNamespace(raw=raw, symbol=symbol, decimals=6),
        tx=SimpleNamespace(hash=f"{sender}{recipient}{minutes}"),
    )


class TestConfusable:
    """"Case is preserved --- `usdc` and `USDC` are not a forgery of each other.""" ""

    def test_case_is_not_folded(self) -> None:
        assert not confusable("usdc", "USDC")

    def test_digits_that_look_like_letters_are_left_alone(self) -> None:
        # "Folding them would make SOL and S0L indistinguishable in the wrong
        # direction --- turning a real symbol into a reported forgery."
        assert not confusable("S0L", "SOL")


class TestImpersonation:
    def test_trust_is_decided_by_contract_not_symbol(self) -> None:
        """The module's own rule, applied to itself.

        A transfer of the real USDC contract carrying no symbol --- which
        several providers emit --- must still be trusted. It was not, and the
        consequence was silent: every route through it read as fabricated.
        """
        rows = [_t("a", "b", 1, symbol="")]
        assert REAL_USDC in trusted_assets(rows, ETHEREUM)

    def test_a_forgery_is_trusted_by_neither(self) -> None:
        rows = [_t("a", "b", 1, asset="0x" + "9" * 40, symbol="")]
        assert "0x" + "9" * 40 not in trusted_assets(rows, ETHEREUM)

    def test_unlisted_is_not_a_clean_bill(self) -> None:
        # "Absence of a canonical entry means the check had nothing to say."
        rows = [
            SimpleNamespace(
                chain=ETHEREUM,
                asset=SimpleNamespace(key="0x" + "3" * 40),
                amount=SimpleNamespace(symbol="NEWCOIN", decimals=18),
            )
        ]
        assert "no registry" in report(rows, ETHEREUM).summary().lower()


class TestRoute:
    def test_a_path_cannot_carry_more_than_its_narrowest_hop(self) -> None:
        rows = [_t("a", "m", 1, raw=10**9), _t("m", "b", 2, raw=7)]
        routes, _ = find_routes(rows, "a", "b", chain=ETHEREUM)
        assert routes[0].carries == 7

    def test_a_hub_is_never_invented(self) -> None:
        # "It cannot be *invented* --- an address with 25 counterparties here
        # has at least 25 --- so every hub reported is genuinely one."
        assert hubs_in([_t("a", "b", 1)]) == set()

    def test_no_route_is_not_proof_of_no_connection(self) -> None:
        found = route_findings(*find_routes([], "a", "b"), "a", "b")
        assert "not proof the two are unconnected" in found[0].detail

    def test_believable_routes_come_first(self) -> None:
        # Even when longer: a route made of the attacker's own log entries must
        # never be the first thing read.
        rows = [
            _t("a", "b", 1, asset="0x" + "9" * 40),
            _t("a", "m", 1),
            _t("m", "b", 2),
        ]
        routes, _ = find_routes(rows, "a", "b", chain=ETHEREUM)
        assert routes[0].is_believable
        assert routes[0].length > routes[-1].length


class TestPoisoning:
    def test_repetition_does_not_decide_the_verdict(self) -> None:
        """ "Only the first signal decides anything."

        Fifty inbound transfers from the impostor must not outweigh one
        outbound payment to the genuine address.
        """
        subject = "0x" + "e" * 40
        genuine = "0x5bfb6836cc38d4b4f3949b99464afed728b5add8"
        impostor = "0x5bfbfac19e0d3b5a56abdd0c6a31d31e3b85add8"
        rows = [_t(subject, genuine, 1)]
        rows += [_t(impostor, subject, n) for n in range(2, 52)]
        groups, _ = find_lookalikes(rows, subject, chain=ETHEREUM)
        assert groups[0].is_decidable
        assert groups[0].paid[0].address == genuine

    def test_the_collision_probability_grows_with_the_set(self) -> None:
        assert chance_of_collision(1000) > chance_of_collision(37)


class TestLeads:
    def test_a_lead_cannot_exist_without_a_verification_step(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="verification step"):
            Lead(
                address="0x" + "a" * 40,
                kind="twitter",
                value="x",
                source="s",
                asserted_by="y",
                verify_by="   ",
            )


class TestEnsLookup:
    def test_text_records_are_fetched_after_the_confirmation_gate(self) -> None:
        """Checked structurally as well as behaviourally.

        The behavioural test asserts no `text()` call goes out for an
        unconfirmed record. This asserts the gate is still *upstream* of the
        fetch in the source, so a refactor that reorders them fails here rather
        than silently sending another person's handles.
        """
        import inspect

        from chainscope.attribution.ens_lookup import EnsLookup

        source = inspect.getsource(EnsLookup.look_up)
        assert source.index("is_confirmed") < source.index("self.text_of")


class TestTheVerificationReviewFound:
    """A second review, over the fixes from the first. No criticals, and these.

    Two were the *same* defect half-fixed --- a lesson worth keeping in one
    place: correcting one side of a pair and not the other can be worse than
    leaving both wrong, because the two sides had at least agreed.
    """

    def test_the_resolver_cache_is_readable_by_the_path_that_wrote_it(self) -> None:
        """`resolve` used `address_key`, `resolve_many` used `.lower()`.

        Fixing only the *write* made `resolve_many`'s read and write disagree,
        so on Solana, Sui and Bitcoin its cache became write-only: a batch
        resolve of a thousand addresses re-fetched every one on the next call.
        """
        from chainscope.attribution.base import Source, SourceMeta
        from chainscope.attribution.resolver import Resolver
        from chainscope.core.chainid import SOLANA

        solana_address = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"

        class Counting(Source):
            name = "counting"

            def __init__(self) -> None:
                self.meta = SourceMeta(publisher="X", license="MIT", redistributable=True)
                self.calls = 0

            def ready(self) -> bool:
                return True

            def lookup(self, address, chain=None):  # type: ignore[no-untyped-def]
                self.calls += 1
                return []

        source = Counting()
        resolver = Resolver([source])
        resolver.resolve_many([solana_address], SOLANA)
        resolver.resolve_many([solana_address], SOLANA)
        resolver.resolve(solana_address, SOLANA)
        assert source.calls == 1

    def test_a_nan_timestamp_does_not_manufacture_a_route(self) -> None:
        """NaN compares False against everything, so `hop.at < since` was always
        False and a NaN passed every ordering check --- the one input that could
        defeat the whole time-respecting property."""
        rows = [
            SimpleNamespace(
                sender=SimpleNamespace(key=a),
                recipient=SimpleNamespace(key=b),
                timestamp=t,
                asset=None,
                amount=SimpleNamespace(raw=1, symbol=""),
                tx=SimpleNamespace(hash=""),
            )
            for a, b, t in (("a", "m", float("nan")), ("m", "b", 2.0))
        ]
        routes, notes = find_routes(rows, "a", "b")
        assert routes == []
        assert notes["undated_transfers_ignored"] == 1

    def test_infinity_is_not_a_moment_either(self) -> None:
        rows = [
            SimpleNamespace(
                sender=SimpleNamespace(key=a),
                recipient=SimpleNamespace(key=b),
                timestamp=t,
                asset=None,
                amount=SimpleNamespace(raw=1, symbol=""),
                tx=SimpleNamespace(hash=""),
            )
            for a, b, t in (("a", "m", float("inf")), ("m", "b", 2.0))
        ]
        assert find_routes(rows, "a", "b")[0] == []

    def test_a_nomination_carries_the_field_it_was_chosen_from(self) -> None:
        """`Hypothesis.alternatives` exists so a nomination cannot be read
        without the candidates it beat --- and here those candidates are the
        addresses somebody might pay by mistake."""
        from chainscope.analysis.poisoning import hypotheses

        subject = "0x" + "e" * 40
        genuine = "0x5bfb6836cc38d4b4f3949b99464afed728b5add8"
        impostor = "0x5bfbfac19e0d3b5a56abdd0c6a31d31e3b85add8"
        rows = [_t(subject, genuine, 1), _t(impostor, subject, 2, raw=0)]
        groups, examined = find_lookalikes(rows, subject, chain=ETHEREUM)
        found = hypotheses(groups, examined)
        assert found[0].alternatives
        assert impostor in " ".join(a.claim for a in found[0].alternatives)


class TestWhatCodexFound:
    """A third review, by a different tool. Three findings, all real.

    Two of them were the *half-fix* pattern for the third time in this session:
    one side of a pair corrected and the other left, which is worse than leaving
    both wrong, because the two sides had at least agreed with each other.
    """

    SOLANA_A = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
    SOLANA_B = "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj"
    SOLANA_C = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    def _sol(self, sender: str, recipient: str, minutes: int) -> object:
        return SimpleNamespace(
            sender=SimpleNamespace(key=sender),
            recipient=SimpleNamespace(key=recipient),
            timestamp=T0 + timedelta(minutes=minutes),
            asset=None,
            amount=SimpleNamespace(raw=1, symbol="SOL"),
            tx=SimpleNamespace(hash=""),
        )

    def test_a_base58_address_matches_itself(self) -> None:
        """`_fold` preserved case and `_key` lowercased, so on Solana, Sui and
        Bitcoin an address never matched itself: every route missed, every
        contributor reported unlinked, the subject not recognised as the
        subject."""
        from chainscope.analysis.route import _fold, _key

        transfer = SimpleNamespace(sender=SimpleNamespace(key=self.SOLANA_A))
        assert _fold(self.SOLANA_A) == _key(transfer, "sender")

    def test_and_a_solana_route_is_found(self) -> None:
        from chainscope.core.chainid import SOLANA

        rows = [
            self._sol(self.SOLANA_A, self.SOLANA_B, 1),
            self._sol(self.SOLANA_B, self.SOLANA_C, 2),
        ]
        routes, _ = find_routes(rows, self.SOLANA_A, self.SOLANA_C, chain=SOLANA)
        assert routes
        assert routes[0].addresses[0] == self.SOLANA_A

    def test_the_case_is_preserved_in_the_answer(self) -> None:
        # Not merely matched --- the address reported back must be the one that
        # exists, not a lowercased string that does not.
        from chainscope.core.chainid import SOLANA

        rows = [self._sol(self.SOLANA_A, self.SOLANA_B, 1)]
        routes, _ = find_routes(rows, self.SOLANA_A, self.SOLANA_B, chain=SOLANA)
        assert routes[0].addresses == [self.SOLANA_A, self.SOLANA_B]

    def test_all_three_modules_normalise_the_same_way(self) -> None:
        """The property, rather than three separate examples of it.

        `poisoning._key` takes an address and the other two take a transfer and
        a field, so they are called differently --- but they must agree, because
        `contributors` runs `poisoning`'s and `route`'s logic over one ledger.
        """
        from chainscope.analysis import contributors as c
        from chainscope.analysis import poisoning as p
        from chainscope.analysis import route as r

        transfer = SimpleNamespace(sender=SimpleNamespace(key=self.SOLANA_A))
        keys = {
            r._key(transfer, "sender"),
            c._key(transfer, "sender"),
            p._key(SimpleNamespace(key=self.SOLANA_A)),
        }
        assert keys == {self.SOLANA_A}

    def test_an_old_lead_index_is_migrated(self, tmp_path: object) -> None:
        """`CREATE UNIQUE INDEX IF NOT EXISTS` does nothing when an index of
        that name already exists, whatever columns it covers.

        So a case file written before `chain` joined the key kept the old
        two-column index and went on merging the same address across chains,
        silently, while the schema in the source said otherwise. New databases
        were correct and existing ones were not.
        """
        import sqlite3

        from chainscope.case.leads import LeadStore
        from chainscope.osint.leads import Lead

        path = tmp_path / "old.db"  # type: ignore[operator]
        con = sqlite3.connect(path)
        con.executescript(
            "CREATE TABLE leads (id INTEGER PRIMARY KEY, at INTEGER, analyst TEXT, "
            "address TEXT, chain TEXT, kind TEXT, value TEXT, source TEXT, "
            "asserted_by TEXT, verify_by TEXT, verdict TEXT DEFAULT 'open', "
            "reason TEXT DEFAULT '', settled_at INTEGER, settled_by TEXT DEFAULT '');"
            "CREATE UNIQUE INDEX ux_leads_identity ON leads(address, kind, value);"
        )
        con.commit()
        con.close()

        store = LeadStore(path)
        try:
            lead = Lead(
                address="0x" + "a" * 40,
                kind="twitter",
                value="x",
                source="s",
                asserted_by="y",
                verify_by="check it",
            )
            first, _ = store.add(lead, "me", "eip155:1")
            second, was_new = store.add(lead, "me", "eip155:56")
            assert first != second
            assert was_new
        finally:
            store.close()

    def test_migrating_twice_is_harmless(self, tmp_path: object) -> None:
        from chainscope.case.leads import LeadStore

        path = tmp_path / "twice.db"  # type: ignore[operator]
        LeadStore(path).close()
        store = LeadStore(path)
        try:
            row = store._conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'ux_leads_identity'"
            ).fetchone()
            assert "chain" in row["sql"]
        finally:
            store.close()
