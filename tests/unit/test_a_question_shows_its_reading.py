"""A plain-language layer must show how it read the question.

The interpretation is the part most likely to be wrong. A layer that reads
"who paid this in the last week", quietly decides it means something else, and
returns an answer has produced a confident response to a question nobody asked
--- which in this domain ends as a claim about a person.

So `interpret` returns a plan, never an answer, and the plan carries the
reading, the caveat, and anything it could not honour.
"""

from __future__ import annotations

import pytest

from chainscope.server import ask

ADDR = "0x098B716B8Aaf21512996dC57EB0615e2383E2f96"
NOW = 1_754_200_000


def test_direction_is_not_reversed() -> None:
    """ "who paid X" and "where did X send" are opposite accusations."""
    assert ask.interpret(f"who paid {ADDR}").params["direction"] == "in"
    assert ask.interpret(f"where did {ADDR} send money").params["direction"] == "out"


def test_an_unhonoured_filter_is_reported_not_dropped() -> None:
    """`/flows` has no time filter, so asking for one must be visible."""
    plan = ask.interpret(f"who paid {ADDR} in the last week", now=NOW)
    assert plan.endpoint == "/flows"
    assert plan.unknowns, "the time window vanished without a word"
    assert "last week" in plan.unknowns[0]


def test_a_window_the_endpoint_can_honour_becomes_a_parameter() -> None:
    plan = ask.interpret(f"expand {ADDR} in the last 7 days", now=NOW)
    assert plan.params["since"] == NOW - 604_800
    assert not plan.unknowns


def test_a_relative_window_needs_a_reference_time() -> None:
    """Otherwise the same question means a different week each day."""
    with pytest.raises(ValueError, match="reference time"):
        ask.interpret(f"who paid {ADDR} in the last week")


def test_an_unknown_question_is_refused_with_its_vocabulary() -> None:
    """A nearest guess here answers something nobody asked."""
    with pytest.raises(ValueError) as caught:
        ask.interpret("make me a sandwich")
    message = str(caught.value)
    assert "not understood" in message
    assert "what is known about" in message, "refusal must say what it does know"


def test_a_question_needing_an_address_says_so() -> None:
    with pytest.raises(ValueError, match="needs an address"):
        ask.interpret("where did the money go")


def test_identity_beats_asset_wording() -> None:
    """ "is this really USDC" names an asset but asks about identity."""
    plan = ask.interpret(f"is {ADDR} really USDC")
    assert plan.params.get("name") == "impersonation"


def test_every_plan_carries_a_caveat() -> None:
    """What an answer does not settle is never left to the reader."""
    for question in (
        f"what is known about {ADDR}",
        f"where did {ADDR} send money",
        f"who paid {ADDR}",
        f"expand {ADDR}",
        f"is {ADDR} impersonating anything",
        f"was {ADDR} poisoned",
        f"who funded {ADDR}",
        "how many transfers are in this case",
    ):
        plan = ask.interpret(question)
        assert plan.reading, f"{question!r} produced no reading"
        assert plan.caveat, f"{question!r} produced no caveat"


def test_it_returns_a_plan_and_never_an_answer() -> None:
    """Proxying would hide the reading behind a result."""
    plan = ask.interpret(f"what is known about {ADDR}")
    assert set(plan.to_dict()) == {"endpoint", "params", "reading", "caveat", "ignored"}


def test_it_does_not_reach_the_network() -> None:
    """The question contains an address; sending it anywhere leaks the case."""
    source = __import__("pathlib").Path("src/chainscope/server/ask.py").read_text()
    for forbidden in ("requests", "urlopen", "httpx", "openai", "anthropic", "fetch("):
        assert forbidden not in source, f"ask.py reaches out via {forbidden}"
