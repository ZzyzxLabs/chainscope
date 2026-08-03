"""How money got from A to B, and the three ways of getting that wrong.

Shortest-hop breadth-first search is the obvious implementation and it is
unsound here, because it has no notion of when anything happened. It will
happily return `A -> X -> B` where X paid B *before* A ever paid X --- a route
the money cannot have taken.

Measured, not assumed. On a real ledger of 55 transfers between 37 addresses:
of 224 multi-hop shortest paths BFS returned, 138 were causally impossible
(62%). Asked the other way --- for how many address pairs BFS claims a
connection that survives a time-respecting search --- 53% of the claims vanish.

The other two errors are quieter. A route through an exchange hot wallet
connects almost any two addresses on a chain and means nothing, because a
custodian commingles what it receives: the link is in the ledger, not in the
money. And a route whose narrowest hop moved 0.001 ETH cannot be how 1,000 ETH
travelled.

The literature term is a *time-respecting path* (Kempe, Kleinberg & Kumar,
STOC 2000); the earliest-arrival algorithms are surveyed in Wu et al., VLDB
2014.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import ClassVar

from chainscope.analysis.route import DEFAULT_HUB_DEGREE, find_routes, findings, hubs_in

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _t(sender: str, recipient: str, minutes: int, raw: int = 100, asset: str = "usdc"):
    return SimpleNamespace(
        sender=SimpleNamespace(key=sender),
        recipient=SimpleNamespace(key=recipient),
        timestamp=T0 + timedelta(minutes=minutes),
        asset=SimpleNamespace(key=asset),
        amount=SimpleNamespace(raw=raw, symbol="USDC"),
        tx=SimpleNamespace(hash=f"0x{sender}{recipient}{minutes}"),
    )


class TestTimeIsRespected:
    #: X pays B *before* A pays X --- impossible. Y pays B after. Only Y is real.
    LEDGER: ClassVar[list] = [
        _t("a", "x", 10),
        _t("x", "b", 5),
        _t("a", "y", 10),
        _t("y", "b", 20),
    ]

    def test_the_impossible_route_is_not_returned(self) -> None:
        routes, _ = find_routes(self.LEDGER, "a", "b")
        assert ["a", "x", "b"] not in [r.addresses for r in routes]

    def test_the_possible_one_is(self) -> None:
        routes, _ = find_routes(self.LEDGER, "a", "b")
        assert [r.addresses for r in routes] == [["a", "y", "b"]]

    def test_timestamps_never_decrease_along_a_route(self) -> None:
        # The property, asserted directly rather than through one example.
        routes, _ = find_routes(self.LEDGER, "a", "b", max_hops=6)
        for route in routes:
            times = [hop.at for hop in route.hops]
            assert times == sorted(times)

    def test_a_simultaneous_hop_is_allowed(self) -> None:
        # Non-decreasing, not strictly increasing: two transfers in one block
        # share a timestamp, and refusing that would drop real routes.
        ledger = [_t("a", "m", 10), _t("m", "b", 10)]
        routes, _ = find_routes(ledger, "a", "b")
        assert [r.addresses for r in routes] == [["a", "m", "b"]]

    def test_a_direct_hop_needs_no_predecessor(self) -> None:
        routes, _ = find_routes([_t("a", "b", 3)], "a", "b")
        assert routes and routes[0].length == 1


class TestHubs:
    def _busy(self) -> list:
        # One address paid by many and paying many: a service.
        rows = [_t("a", "hub", 1)]
        rows += [_t(f"in{n}", "hub", 2) for n in range(DEFAULT_HUB_DEGREE)]
        rows += [_t("hub", f"out{n}", 3) for n in range(DEFAULT_HUB_DEGREE)]
        rows += [_t("hub", "b", 5)]
        return rows

    def test_a_busy_address_is_recognised(self) -> None:
        assert "hub" in hubs_in(self._busy())

    def test_an_ordinary_address_is_not(self) -> None:
        assert hubs_in([_t("a", "m", 1), _t("m", "b", 2)]) == set()

    def test_a_route_through_a_hub_is_not_offered_by_default(self) -> None:
        # It would connect almost any two addresses and mean nothing.
        routes, notes = find_routes(self._busy(), "a", "b")
        assert routes == []
        assert notes["routes_stopped_at_a_hub"] >= 1

    def test_the_exclusion_is_counted_rather_than_silent(self) -> None:
        _, notes = find_routes(self._busy(), "a", "b")
        assert notes["hubs_detected"] == ["hub"]

    def test_it_can_be_asked_for_anyway(self) -> None:
        routes, _ = find_routes(self._busy(), "a", "b", allow_hubs=True)
        assert [r.addresses for r in routes] == [["a", "hub", "b"]]

    def test_and_then_it_says_the_link_is_not_in_the_money(self) -> None:
        routes, _ = find_routes(self._busy(), "a", "b", allow_hubs=True)
        assert routes[0].crosses_hub == "hub"
        assert "commingled" in routes[0].describe()


class TestWhatARouteCanCarry:
    def test_the_narrowest_hop_is_the_ceiling(self) -> None:
        routes, _ = find_routes([_t("a", "m", 1, raw=1000), _t("m", "b", 2, raw=7)], "a", "b")
        assert routes[0].carries == 7

    def test_a_thin_route_is_still_returned(self) -> None:
        # Whether it can carry the sum in question is the reader's call, and
        # dropping it would hide a real hop.
        routes, _ = find_routes([_t("a", "m", 1, raw=10**18), _t("m", "b", 2, raw=1)], "a", "b")
        assert routes and routes[0].carries == 1

    def test_a_changed_asset_is_flagged(self) -> None:
        rows = [_t("a", "m", 1, asset="usdc"), _t("m", "b", 2, asset="weth")]
        routes, _ = find_routes(rows, "a", "b")
        assert not routes[0].single_asset
        assert "not the same coin" in routes[0].describe()


class TestBoundsAndHonesty:
    def test_it_will_not_walk_in_circles(self) -> None:
        rows = [_t("a", "m", 1), _t("m", "a", 2), _t("m", "b", 3)]
        routes, _ = find_routes(rows, "a", "b", max_hops=6)
        for route in routes:
            assert len(set(route.addresses)) == len(route.addresses)

    def test_max_hops_is_honoured(self) -> None:
        chain = [_t(chr(97 + n), chr(98 + n), n) for n in range(6)]
        routes, _ = find_routes(chain, "a", "g", max_hops=3)
        assert routes == []

    def test_the_same_chain_is_found_when_allowed_the_depth(self) -> None:
        chain = [_t(chr(97 + n), chr(98 + n), n) for n in range(6)]
        routes, _ = find_routes(chain, "a", "g", max_hops=6)
        assert routes and routes[0].length == 6

    def test_an_undated_transfer_is_dropped_and_counted(self) -> None:
        # Placing it anywhere in the order would manufacture a route, which is
        # the failure this module exists to avoid.
        undated = SimpleNamespace(
            sender=SimpleNamespace(key="a"),
            recipient=SimpleNamespace(key="b"),
            timestamp=None,
            asset=None,
            amount=SimpleNamespace(raw=1, symbol=""),
            tx=SimpleNamespace(hash="0x0"),
        )
        routes, notes = find_routes([undated], "a", "b")
        assert routes == []
        assert notes["undated_transfers_ignored"] == 1

    def test_shortest_routes_come_first(self) -> None:
        rows = [_t("a", "b", 1), _t("a", "m", 1), _t("m", "b", 2)]
        routes, _ = find_routes(rows, "a", "b")
        assert [r.length for r in routes] == sorted(r.length for r in routes)


class TestTheFindings:
    def test_no_route_is_reported_as_a_result(self) -> None:
        found = findings(*find_routes([_t("a", "m", 1)], "a", "b"), "a", "b")
        assert found and "no route found" in found[0].title

    def test_and_is_not_reported_as_proof_of_no_connection(self) -> None:
        # The most easily misread output in the module.
        found = findings(*find_routes([_t("a", "m", 1)], "a", "b"), "a", "b")
        assert "not proof the two are unconnected" in found[0].detail

    def test_a_found_route_names_its_transactions(self) -> None:
        found = findings(*find_routes([_t("a", "m", 1), _t("m", "b", 2)], "a", "b"), "a", "b")
        assert any(f.data.get("transactions") for f in found)

    def test_the_summary_separates_hub_routes_from_clean_ones(self) -> None:
        found = findings(*find_routes([_t("a", "m", 1), _t("m", "b", 2)], "a", "b"), "a", "b")
        assert "cross no high-degree address" in found[0].detail

    def test_it_does_not_claim_the_money_took_the_route(self) -> None:
        found = findings(*find_routes([_t("a", "m", 1), _t("m", "b", 2)], "a", "b"), "a", "b")
        assert "does not prove any one of them" in found[0].detail


class TestARouteMadeOfForgedTransfers:
    """A token contract emits its own transfer events.

    So a hop moving a forged token is not a movement of money --- it is a claim
    by whoever wrote that contract, who in this context is the person the route
    is about. Run against the real case, the first version of this analyzer
    reported eight routes whose hops all moved a token imitating ETH: a route
    the attacker drew, presented as a finding.
    """

    REAL_USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    FORGED = "0xa599e8c7f4bac6512e250055a96a20a72bbac75e"

    def test_a_route_of_forged_hops_is_marked(self) -> None:
        from chainscope.core.chainid import ETHEREUM

        rows = [_t("a", "m", 1, asset=self.FORGED), _t("m", "b", 2, asset=self.FORGED)]
        routes, _ = find_routes(rows, "a", "b", chain=ETHEREUM)
        assert routes[0].forged_hops == 2
        assert not routes[0].is_believable

    def test_it_says_who_drew_it(self) -> None:
        from chainscope.core.chainid import ETHEREUM

        rows = [_t("a", "m", 1, asset=self.FORGED), _t("m", "b", 2, asset=self.FORGED)]
        routes, _ = find_routes(rows, "a", "b", chain=ETHEREUM)
        assert "route the attacker drew" in routes[0].describe()

    def test_a_genuine_asset_is_believed(self) -> None:
        from chainscope.core.chainid import ETHEREUM

        rows = [_t("a", "m", 1, asset=self.REAL_USDC), _t("m", "b", 2, asset=self.REAL_USDC)]
        routes, _ = find_routes(rows, "a", "b", chain=ETHEREUM)
        assert routes[0].is_believable

    def test_believable_routes_are_ranked_first(self) -> None:
        # Even when longer. A route made of the attacker's own log entries must
        # never be the first thing an investigator reads.
        from chainscope.core.chainid import ETHEREUM

        rows = [
            _t("a", "b", 1, asset=self.FORGED),
            _t("a", "m", 1, asset=self.REAL_USDC),
            _t("m", "b", 2, asset=self.REAL_USDC),
        ]
        routes, _ = find_routes(rows, "a", "b", chain=ETHEREUM)
        assert routes[0].is_believable
        assert routes[0].length > routes[-1].length

    def test_the_forged_route_is_still_returned(self) -> None:
        # Reported, not dropped: the reader may meet the same transfer
        # elsewhere and needs to know what it is.
        from chainscope.core.chainid import ETHEREUM

        rows = [_t("a", "m", 1, asset=self.FORGED), _t("m", "b", 2, asset=self.FORGED)]
        routes, _ = find_routes(rows, "a", "b", chain=ETHEREUM)
        assert len(routes) == 1


class TestWhatTheReviewFound:
    """Three defects, each of which produced a plausible wrong picture."""

    def test_a_ledger_mixing_datetime_and_unix_seconds_does_not_raise(self) -> None:
        """A store returns `datetime`; a provider parsed from JSON returns ints.

        Sorting a list holding both raised `TypeError` from inside the sort,
        several frames from anything a caller recognises --- and the route
        search *is* comparison of times, so it could not have been anywhere
        else.
        """
        rows = [
            _t("a", "m", 1),
            SimpleNamespace(
                sender=SimpleNamespace(key="m"),
                recipient=SimpleNamespace(key="b"),
                timestamp=int((T0 + timedelta(minutes=2)).timestamp()),
                asset=SimpleNamespace(key="usdc"),
                amount=SimpleNamespace(raw=100, symbol="USDC"),
                tx=SimpleNamespace(hash="0xmb"),
            ),
        ]
        routes, _ = find_routes(rows, "a", "b")
        assert [r.addresses for r in routes] == [["a", "m", "b"]]

    def test_a_naive_datetime_is_read_as_utc_not_as_local_time(self) -> None:
        # `.timestamp()` on a naive value takes the machine's offset, so the
        # same ledger would order differently in Taipei and in London.
        naive = SimpleNamespace(
            sender=SimpleNamespace(key="a"),
            recipient=SimpleNamespace(key="b"),
            timestamp=datetime(2026, 1, 1, 12, 0),
            asset=None,
            amount=SimpleNamespace(raw=1, symbol=""),
            tx=SimpleNamespace(hash="0x1"),
        )
        routes, _ = find_routes([naive], "a", "b")
        assert (
            routes
            and routes[0].hops[0].at
            == datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).timestamp()
        )

    def test_the_same_transfer_read_twice_is_one_hop(self) -> None:
        """The analyzer expands from both ends, so every transfer between two
        expanded addresses arrives twice --- and each copy multiplied the routes
        through it. A ledger read twice reported four routes for one path."""
        rows = [_t("a", "m", 1), _t("m", "b", 2)]
        routes, notes = find_routes(rows + rows, "a", "b")
        assert len(routes) == 1
        assert notes["duplicate_transfers_collapsed"] == 2

    def test_the_hub_named_is_the_first_one_reached(self) -> None:
        """It came from `visited`, a set, so a route crossing two hubs named an
        arbitrary one by hash order --- and neither was 'where the money first
        went into a custodian'."""
        rows = [_t("a", "h1", 1)]
        rows += [_t(f"in{n}", "h1", 1) for n in range(DEFAULT_HUB_DEGREE)]
        rows += [_t("h1", f"out{n}", 2) for n in range(DEFAULT_HUB_DEGREE)]
        rows += [_t("h1", "h2", 3)]
        rows += [_t(f"z{n}", "h2", 1) for n in range(DEFAULT_HUB_DEGREE)]
        rows += [_t("h2", f"w{n}", 2) for n in range(DEFAULT_HUB_DEGREE)]
        rows += [_t("h2", "b", 5)]
        routes, _ = find_routes(rows, "a", "b", allow_hubs=True, max_hops=4)
        assert routes
        assert routes[0].crosses_hub == "h1"

    def test_the_params_record_what_decided_the_answer(self) -> None:
        # `allow_hubs` decides whether a route is offered at all and
        # `max_expand` decides how much was searched. Without them the params
        # describe a run nobody can repeat, and "no route" is the result most
        # in need of repeating.
        from chainscope.analysis.route import RouteAnalyzer

        signature = RouteAnalyzer.run.__code__.co_varnames
        assert "allow_hubs" in signature and "max_expand" in signature

    def test_a_forged_route_says_so_in_its_data(self) -> None:
        # It was in the title and the detail but not in `data`, so a route made
        # of the attacker's own log entries was indistinguishable in JSON from
        # one built out of real transfers.
        from chainscope.core.chainid import ETHEREUM

        forged = "0xa599e8c7f4bac6512e250055a96a20a72bbac75e"
        rows = [_t("a", "m", 1, asset=forged), _t("m", "b", 2, asset=forged)]
        routes, notes = find_routes(rows, "a", "b", chain=ETHEREUM)
        found = findings(routes, notes, "a", "b")
        route_data = [f.data for f in found if "believable" in f.data]
        assert route_data and route_data[0]["believable"] is False
        assert route_data[0]["forged_hops"] == 2


class TestTheSearchBudget:
    """`max_routes` bounds what is returned; nothing bounded what is explored.

    A dense graph where few walks reach the target searches exponentially in
    `max_hops` and returns nothing, having spent the time anyway --- and an
    empty result reads as "no route", not as "the search gave up".
    """

    def _dense(self) -> list:
        # Every node points at every later node: many walks, none reaching "b".
        names = [f"n{i}" for i in range(9)]
        return [
            _t(a, b, i + 1) for i, a in enumerate(names) for b in names[names.index(a) + 1 :]
        ]

    def test_a_tiny_budget_stops_the_walk(self) -> None:
        _, notes = find_routes(self._dense(), "n0", "zzz", max_hops=8, max_steps=20)
        assert "search_budget_exhausted" in notes

    def test_and_says_the_result_is_not_all_there_is(self) -> None:
        _, notes = find_routes(self._dense(), "n0", "zzz", max_hops=8, max_steps=20)
        assert "not 'all there is'" in notes["search_budget_exhausted"]

    def test_an_ordinary_search_does_not_trip_it(self) -> None:
        # A warning that fires every run is one people stop reading.
        _, notes = find_routes([_t("a", "m", 1), _t("m", "b", 2)], "a", "b")
        assert "search_budget_exhausted" not in notes

    def test_the_steps_taken_are_reported_either_way(self) -> None:
        _, notes = find_routes([_t("a", "m", 1), _t("m", "b", 2)], "a", "b")
        assert notes["steps"] >= 1


class TestForgedRoutesWithoutAChain:
    def test_a_forged_hop_is_still_caught_without_one(self) -> None:
        """Without a chain, `trusted_assets` accepts a contract canonical on
        *any* chain. A forgery is canonical on none, so it is still caught."""
        forged = "0xa599e8c7f4bac6512e250055a96a20a72bbac75e"
        rows = [_t("a", "m", 1, asset=forged), _t("m", "b", 2, asset=forged)]
        routes, _ = find_routes(rows, "a", "b")
        assert not routes[0].is_believable
        assert "route the attacker drew" in routes[0].describe()

    def test_a_real_asset_is_believed_without_a_chain_too(self) -> None:
        """This used to fail, and the failure was silent.

        Trust was decided from the *verdict*, which is reached from
        `(chain, symbol)` --- and the symbol is the part the attacker chooses.
        So a transfer of the real USDC contract whose symbol a provider omitted
        came back `unlisted`, was untrusted, and every route through it turned
        into "the attacker drew this" with nothing said. The check is now
        contract membership, which is what this package's own rule says.
        """
        real = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
        rows = [_t("a", "m", 1, asset=real), _t("m", "b", 2, asset=real)]
        routes, _ = find_routes(rows, "a", "b")
        assert routes[0].is_believable

    def test_a_real_contract_with_no_symbol_is_still_believed(self) -> None:
        # The case that exposed it: several providers omit `symbol`, and
        # disbelieving real evidence is the expensive direction to be wrong in.
        from chainscope.core.chainid import ETHEREUM

        real = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
        rows = [
            SimpleNamespace(
                sender=SimpleNamespace(key=a),
                recipient=SimpleNamespace(key=b),
                timestamp=T0 + timedelta(minutes=m),
                asset=SimpleNamespace(key=real),
                amount=SimpleNamespace(raw=100, symbol=""),
                tx=SimpleNamespace(hash=f"0x{m}"),
            )
            for a, b, m in (("a", "m", 1), ("m", "b", 2))
        ]
        routes, _ = find_routes(rows, "a", "b", chain=ETHEREUM)
        assert routes[0].is_believable

    def test_and_believed_once_the_chain_is_given(self) -> None:
        from chainscope.core.chainid import ETHEREUM

        real = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
        rows = [_t("a", "m", 1, asset=real), _t("m", "b", 2, asset=real)]
        routes, _ = find_routes(rows, "a", "b", chain=ETHEREUM)
        assert routes[0].is_believable
