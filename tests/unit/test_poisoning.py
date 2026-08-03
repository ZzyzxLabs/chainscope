"""Addresses ground to be mistaken for other addresses.

The attack is cheap and aims at attention, not cryptography: grind a vanity
address matching the first four and last four characters of one the victim
really uses, send it a zero-value transfer so it lands in their history, and
wait for them to copy "the address I sent to last time".

Measured on a real case: 37 counterparties, and *nine* groups sharing a 4+4
match. Matching 4 hex characters at each end is 32 bits, so across 666 pairs the
chance of even one collision is about 1.6e-7. Nine is not luck.

The second half of this file is a correction, and it is the more important half.
The first version read "the subject paid this address" out of ERC-20 `Transfer`
events --- events emitted by the token contract. A forged token can log anything
its author wants, including a payment the victim never made. Run against the
real case it announced "the subject paid 4 of these 5", which was false, and
false in the most persuasive words available. 24 of the 27 addresses in a
lookalike group there appear *only* in forged-token transfers.
"""

from __future__ import annotations

from types import SimpleNamespace

from chainscope.analysis.poisoning import (
    DEFAULT_EDGES,
    chance_of_collision,
    find_lookalikes,
    findings,
)
from chainscope.core.chainid import ETHEREUM

REAL_USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
FORGED = "0xa599e8c7f4bac6512e250055a96a20a72bbac75e"
SUBJECT = "0xe8bde8169a2f6ed6855201afcac7be05a5639b25"

#: A real lookalike group from the case, and one unrelated address.
GENUINE = "0x5bfb6836cc38d4b4f3949b99464afed728b5add8"
IMPOSTOR = "0x5bfbfac19e0d3b5a56abdd0c6a31d31e3b85add8"
UNRELATED = "0x1111111111111111111111111111111111111111"


def _t(sender: str, recipient: str, asset: str, raw: int = 1, when: int = 1) -> object:
    return SimpleNamespace(
        chain=ETHEREUM,
        sender=SimpleNamespace(key=sender.lower()),
        recipient=SimpleNamespace(key=recipient.lower()),
        asset=SimpleNamespace(key=asset.lower()),
        # Marked per line rather than per file: the Cyrillic here is the
        # fixture, and an *accidental* confusable elsewhere in this file should
        # still be caught.
        amount=SimpleNamespace(
            raw=raw,
            symbol="USDC" if asset == REAL_USDC else "UЅDC",  # noqa: RUF001
        ),
        timestamp=when,
    )


class TestTheArithmeticIsTheFinding:
    def test_a_collision_among_a_few_addresses_is_essentially_impossible(self) -> None:
        assert chance_of_collision(37) < 1e-6

    def test_it_grows_with_the_set(self) -> None:
        assert chance_of_collision(1000) > chance_of_collision(37)

    def test_one_address_cannot_collide(self) -> None:
        assert chance_of_collision(1) == 0.0
        assert chance_of_collision(0) == 0.0

    def test_a_shorter_window_is_much_weaker(self) -> None:
        # 2+2 is 16 bits. The default is 4+4 for a reason, and the reason is
        # checkable rather than asserted.
        assert chance_of_collision(37, edges=2) > 1000 * chance_of_collision(37, edges=4)

    def test_the_number_reaches_the_report(self) -> None:
        # "These look similar" invites "coincidences happen". They do, at a rate
        # the finding has to state.
        rows = [
            _t(SUBJECT, GENUINE, REAL_USDC),
            _t(IMPOSTOR, SUBJECT, FORGED, raw=0),
        ]
        groups, examined = find_lookalikes(rows, SUBJECT, chain=ETHEREUM)
        found = findings(groups, examined)
        assert "chance_of_one_collision" in found[0].data


class TestGrouping:
    def test_two_addresses_sharing_both_ends_are_a_group(self) -> None:
        rows = [_t(SUBJECT, GENUINE, REAL_USDC), _t(IMPOSTOR, SUBJECT, REAL_USDC)]
        groups, _ = find_lookalikes(rows, SUBJECT, chain=ETHEREUM)
        assert len(groups) == 1
        assert {m.address for m in groups[0].members} == {GENUINE, IMPOSTOR}

    def test_an_unrelated_address_forms_no_group(self) -> None:
        rows = [_t(SUBJECT, GENUINE, REAL_USDC), _t(SUBJECT, UNRELATED, REAL_USDC)]
        groups, _ = find_lookalikes(rows, SUBJECT, chain=ETHEREUM)
        assert groups == []

    def test_the_subject_is_not_its_own_counterparty(self) -> None:
        rows = [_t(SUBJECT, SUBJECT, REAL_USDC)]
        _, examined = find_lookalikes(rows, SUBJECT, chain=ETHEREUM)
        assert examined == 0

    def test_the_default_window_is_what_a_wallet_truncates_to(self) -> None:
        assert DEFAULT_EDGES == 4


class TestWhichOneIsReal:
    def test_a_payment_in_a_trusted_asset_decides_it(self) -> None:
        rows = [
            _t(SUBJECT, GENUINE, REAL_USDC),
            _t(IMPOSTOR, SUBJECT, REAL_USDC, raw=0),
        ]
        groups, _ = find_lookalikes(rows, SUBJECT, chain=ETHEREUM)
        assert groups[0].is_decidable
        assert [m.address for m in groups[0].paid] == [GENUINE]
        assert [m.address for m in groups[0].suspects] == [IMPOSTOR]

    def test_a_payment_logged_by_a_forged_token_decides_nothing(self) -> None:
        """The correction, stated as a test.

        Identical to the case above except that the payment is recorded by a
        token that failed the impersonation check --- that is, by the attacker.
        The old code called this address paid; it is not paid, it is *claimed*.
        """
        rows = [
            _t(SUBJECT, GENUINE, FORGED),
            _t(IMPOSTOR, SUBJECT, FORGED, raw=0),
        ]
        groups, _ = find_lookalikes(rows, SUBJECT, chain=ETHEREUM)
        assert groups[0].paid == []
        assert not groups[0].is_decidable

    def test_the_forged_claim_is_reported_rather_than_dropped(self) -> None:
        # A reader who is not shown it may find the same transfer elsewhere and
        # reach the wrong conclusion unaided.
        rows = [_t(SUBJECT, GENUINE, FORGED), _t(IMPOSTOR, SUBJECT, FORGED, raw=0)]
        groups, _ = find_lookalikes(rows, SUBJECT, chain=ETHEREUM)
        assert sum(m.sent_untrusted for m in groups[0].members) == 1
        assert "from the attacker" in groups[0].describe()

    def test_the_native_asset_is_trusted(self) -> None:
        # It has no contract, so there is nothing to forge --- its transfers are
        # recorded by the chain rather than emitted by anybody's code.
        rows = [
            SimpleNamespace(
                chain=ETHEREUM,
                sender=SimpleNamespace(key=SUBJECT),
                recipient=SimpleNamespace(key=GENUINE),
                asset=None,
                amount=SimpleNamespace(raw=10**18, symbol="ETH"),
                timestamp=1,
            ),
            _t(IMPOSTOR, SUBJECT, FORGED, raw=0),
        ]
        groups, _ = find_lookalikes(rows, SUBJECT, chain=ETHEREUM)
        assert groups[0].is_decidable

    def test_paying_several_members_is_not_decidable(self) -> None:
        rows = [
            _t(SUBJECT, GENUINE, REAL_USDC),
            _t(SUBJECT, IMPOSTOR, REAL_USDC),
        ]
        groups, _ = find_lookalikes(rows, SUBJECT, chain=ETHEREUM)
        assert not groups[0].is_decidable
        assert "Not decidable" in groups[0].describe()

    def test_it_refuses_to_nominate_when_undecidable(self) -> None:
        # Guessing here points at an address somebody may then send money to,
        # which is the harm the module exists to prevent.
        rows = [_t(SUBJECT, GENUINE, REAL_USDC), _t(SUBJECT, IMPOSTOR, REAL_USDC)]
        groups, examined = find_lookalikes(rows, SUBJECT, chain=ETHEREUM)
        detail = " ".join(f.detail for f in findings(groups, examined))
        assert "No member is nominated" in detail


class TestTheFindings:
    def _rows(self) -> list[object]:
        return [_t(SUBJECT, GENUINE, REAL_USDC), _t(IMPOSTOR, SUBJECT, REAL_USDC, raw=0)]

    def test_nothing_is_reported_when_nothing_collides(self) -> None:
        rows = [_t(SUBJECT, UNRELATED, REAL_USDC)]
        groups, examined = find_lookalikes(rows, SUBJECT, chain=ETHEREUM)
        assert findings(groups, examined) == []

    def test_a_collision_outranks_the_rest_of_the_report(self) -> None:
        from chainscope.core.result import Severity

        groups, examined = find_lookalikes(self._rows(), SUBJECT, chain=ETHEREUM)
        found = findings(groups, examined)
        # The consequence is a payment to the wrong party. It is a warning about
        # an action the reader is about to take, not a note about the case.
        assert found[0].severity == Severity.CRITICAL

    def test_each_member_is_listed_with_its_evidence(self) -> None:
        groups, examined = find_lookalikes(self._rows(), SUBJECT, chain=ETHEREUM)
        detail = findings(groups, examined)[1].detail
        assert GENUINE in detail and IMPOSTOR in detail
        assert "zero-value" in detail


class TestTheAnalyzerNotJustTheHelper:
    """The tests above exercise `find_lookalikes` and `findings`.

    What a caller actually receives is a `Result` --- with hypotheses, params
    and warnings --- and none of that was covered. A helper can be correct while
    the analyzer wiring it up drops half of it on the floor.
    """

    def _report(self):
        rows = [
            _t(SUBJECT, GENUINE, REAL_USDC),
            _t(IMPOSTOR, SUBJECT, REAL_USDC, raw=0),
        ]
        groups, examined = find_lookalikes(rows, SUBJECT, chain=ETHEREUM)
        return groups, examined

    def test_which_member_is_real_is_a_hypothesis_not_a_finding(self) -> None:
        """The existence of a group is arithmetic. Which member was meant is an
        inference, and it is the inference whose being wrong sends money to the
        attacker."""
        from chainscope.analysis.poisoning import hypotheses

        groups, examined = self._report()
        found = hypotheses(groups, examined)
        assert found and GENUINE in found[0].claim

    def test_it_shows_the_score_factors(self) -> None:
        from chainscope.analysis.poisoning import hypotheses

        groups, examined = self._report()
        names = {f.name for f in hypotheses(groups, examined)[0].factors}
        assert "paid_in_a_trusted_asset" in names
        assert "only_attacker_authored_evidence" in names

    def test_an_undecidable_group_still_produces_one(self) -> None:
        # Omitting it would leave the undecidable case invisible in the
        # structured output, which is where it most needs to be.
        from chainscope.analysis.poisoning import hypotheses

        rows = [_t(SUBJECT, GENUINE, REAL_USDC), _t(SUBJECT, IMPOSTOR, REAL_USDC)]
        groups, examined = find_lookalikes(rows, SUBJECT, chain=ETHEREUM)
        found = hypotheses(groups, examined)
        assert found and "cannot be told" in found[0].claim

    def test_an_undecidable_one_scores_lower(self) -> None:
        from chainscope.analysis.poisoning import hypotheses

        decidable = hypotheses(*self._report())[0]
        rows = [_t(SUBJECT, GENUINE, REAL_USDC), _t(SUBJECT, IMPOSTOR, REAL_USDC)]
        groups, examined = find_lookalikes(rows, SUBJECT, chain=ETHEREUM)
        assert hypotheses(groups, examined)[0].score < decidable.score

    def test_no_hypothesis_can_claim_more_than_medium(self) -> None:
        # Enforced by the type, asserted here because it is the property the
        # type exists for.
        from chainscope.analysis.poisoning import hypotheses
        from chainscope.core.attribution import Confidence

        for h in hypotheses(*self._report()):
            assert h.confidence <= Confidence.MEDIUM

    def test_severity_follows_the_probability(self) -> None:
        """A 1e-7 collision is grinding. A likely one is a coincidence, and
        calling that CRITICAL trains the reader to skip the section."""
        from chainscope.core.result import Severity

        groups, examined = self._report()
        assert findings(groups, examined)[0].severity == Severity.CRITICAL
        # A huge address set makes a collision unremarkable.
        assert findings(groups, 10**6)[0].severity != Severity.CRITICAL

    def test_the_wording_changes_with_it(self) -> None:
        groups, examined = self._report()
        assert "not a coincidence" in findings(groups, examined)[0].detail
        assert "candidates rather than as proof" in findings(groups, 10**6)[0].detail
