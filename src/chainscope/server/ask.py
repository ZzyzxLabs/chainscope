"""Plain-language questions, turned into a call you can see before it runs.

Somebody who has not memorised this tool arrives with a question, not a
command: "who paid this address in the last week", "is this really USDC",
"where did the money go". The CLI and the MCP surface both answer those, and
both require knowing which of fourteen tools to reach for.

**This does not call a model.** That is a deliberate limit and worth stating
plainly, because a natural-language box usually implies one. Sending the
question somewhere would mean sending the address in it, and a forensics tool
that transmits which addresses are under investigation has broken its first
promise regardless of how good the answer is. So this is a parser: patterns
over the vocabulary this domain actually uses, mapped onto the endpoints that
already exist.

The cost is real --- it understands the phrasings encoded here and nothing
else. The design follows from that:

**It shows the call before making it.** Every answer names the endpoint and
parameters it derived, so a reader can see it understood "last week" as a
timestamp and disagree. A natural-language layer that hides its
interpretation is a layer that can silently answer a different question.

**It refuses rather than guesses.** An unrecognised question returns the
vocabulary it does know, not a nearest match. Guessing here means confidently
answering something nobody asked, which in this domain ends in a claim about a
person.

An agent that *does* have a model should use MCP or the HTTP endpoints
directly and do its own planning --- this exists for the person typing into a
box, and for an agent that wants the same interpretation a person would get.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Plan", "interpret", "vocabulary"]

#: An EVM address, or a base58/bech32 one long enough not to be a word.
_ADDRESS = re.compile(
    r"\b(0x[0-9a-fA-F]{40}|[13][a-km-zA-HJ-NP-Z1-9]{25,34}|[A-HJ-NP-Za-km-z1-9]{32,44})\b"
)

#: Relative windows, in seconds. Resolved against a caller-supplied `now` so
#: the same question asked twice in one session cannot mean two windows, and so
#: tests are not hostage to the clock.
_WINDOWS: tuple[tuple[str, int], ...] = (
    (r"\b(?:last|past)\s+(?:24\s*hours?|day)\b", 86_400),
    (r"\b(?:last|past)\s+(?:7\s*days?|week)\b", 604_800),
    (r"\b(?:last|past)\s+(?:30\s*days?|month)\b", 2_592_000),
    (r"\b(?:last|past)\s+(?:90\s*days?|quarter|3\s*months?)\b", 7_776_000),
    (r"\b(?:last|past)\s+year\b", 31_536_000),
)


@dataclass
class Plan:
    """What the question was understood to mean, and what will be called.

    Carried back to the caller in full. The interpretation is the part most
    likely to be wrong, so it is the part shown --- an answer to a
    misunderstood question is worse than no answer, and only visible if the
    reading is visible.
    """

    endpoint: str
    params: dict[str, Any]
    reading: str
    """One sentence, in the reader's terms, of what this will do."""

    caveat: str = ""
    """What this answer will not settle, when that is not obvious."""

    unknowns: list[str] = field(default_factory=list)
    """Parts of the question that were ignored. Never silently dropped."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "params": self.params,
            "reading": self.reading,
            "caveat": self.caveat,
            "ignored": self.unknowns,
        }


def interpret(question: str, *, chain: str = "eip155:1", now: int = 0) -> Plan:
    """Turn a question into a plan, or raise `ValueError` naming what it knows.

    `now` is passed in rather than read from the clock so that a relative
    window resolves once, visibly, and so the same question is reproducible ---
    a case that cannot be replayed is not evidence, and that applies to how the
    question was read as much as to the data.
    """
    text = " ".join(question.lower().split())
    if not text:
        raise ValueError("ask something. " + vocabulary())

    found = _ADDRESS.search(question)
    address = found.group(1) if found else ""

    since, window_said = _window(text, now)
    unknowns: list[str] = []
    if since and not now:
        # Refusing rather than substituting the clock: a window silently
        # resolved against "whenever this ran" makes the same question mean
        # different things on different days.
        raise ValueError(
            f"{window_said!r} needs a reference time. Pass `now` as a unix "
            f"timestamp so the window is recorded rather than assumed"
        )

    # Ordered most specific first. "is this really USDC" mentions an asset and
    # a question of identity, and the identity reading has to win.
    for pattern, build in _INTENTS:
        match = re.search(pattern, text)
        if not match:
            continue
        plan = build(address, chain, since, match, text)
        if plan is None:
            continue
        if window_said and "since" not in plan.params:
            unknowns.append(f"the time window ({window_said}) --- this view has no time filter")
        plan.unknowns = unknowns
        return plan

    raise ValueError(f"not understood: {question!r}. " + vocabulary())


def vocabulary() -> str:
    """What this understands. Returned on failure instead of a nearest guess."""
    return (
        "Try: 'what is known about <address>', 'where did <address> send money', "
        "'who paid <address> in the last week', 'is <address> impersonating "
        "anything', 'was <address> address-poisoned', 'who funded <address>', "
        "'expand <address>', or 'how many transfers are in this case'."
    )


def _window(text: str, now: int) -> tuple[int | None, str]:
    for pattern, seconds in _WINDOWS:
        found = re.search(pattern, text)
        if found:
            return (now - seconds if now else 1), found.group(0)
    return None, ""


def _need(address: str) -> str:
    if not address:
        raise ValueError(
            "that question needs an address in it, and none was recognised. "
            "Paste the full address rather than a shortened form"
        )
    return address


def _known(address: str, chain: str, since: int | None, m: Any, text: str) -> Plan:
    return Plan(
        endpoint="/resolve",
        params={"address": _need(address), "chain": chain},
        reading=f"what every configured source says about {address}",
        caveat=(
            "An empty answer means nobody consulted has named it, which is not "
            "the same as it being unlabelled everywhere, and not at all the "
            "same as it being benign. Check `unreachable_sources`."
        ),
    )


def _sent(address: str, chain: str, since: int | None, m: Any, text: str) -> Plan:
    return Plan(
        endpoint="/flows",
        params={"address": _need(address), "chain": chain, "direction": "out"},
        reading=f"where {address} sent money, largest first",
        caveat=(
            "Only what is already in this case. Use 'expand' to fetch more from "
            "a chain --- until then, a short list may mean a short look."
        ),
    )


def _received(address: str, chain: str, since: int | None, m: Any, text: str) -> Plan:
    plan = Plan(
        endpoint="/flows",
        params={"address": _need(address), "chain": chain, "direction": "in"},
        reading=f"who paid {address}, largest first",
        caveat=(
            "Inbound flows include transfers the owner never asked for. A "
            "payment to an address is not consent by it."
        ),
    )
    return plan


def _expand(address: str, chain: str, since: int | None, m: Any, text: str) -> Plan:
    params: dict[str, Any] = {
        "address": _need(address),
        "chain": chain,
        "direction": "out,in",
    }
    if since:
        params["since"] = since
    return Plan(
        endpoint="/expand",
        params=params,
        reading=f"fetch one hop out from {address} and merge it into the case",
        caveat=(
            "The only thing here that spends a rate limit. The reply says how "
            "many flows a filter excluded, because an excluded flow and an "
            "absent one leave the same smaller graph."
        ),
    )


def _impersonation(address: str, chain: str, since: int | None, m: Any, text: str) -> Plan:
    return Plan(
        endpoint="/analyze",
        params={"name": "impersonation", "address": _need(address), "chain": chain},
        reading=f"whether any asset around {address} claims a symbol it does not own",
        caveat=(
            "Compares the contract, never the symbol. UNLISTED means there is "
            "no canonical entry to compare against, which is neither an "
            "accusation nor a clearance."
        ),
    )


def _poisoning(address: str, chain: str, since: int | None, m: Any, text: str) -> Plan:
    return Plan(
        endpoint="/analyze",
        params={"name": "poisoning", "address": _need(address), "chain": chain},
        reading=f"whether addresses near {address} were built to be mistaken for each other",
        caveat=(
            "A lookalike group is a lead. The probability that a collision was "
            "chance is reported with it; read that number before acting."
        ),
    )


def _contributors(address: str, chain: str, since: int | None, m: Any, text: str) -> Plan:
    return Plan(
        endpoint="/analyze",
        params={"name": "contributors", "address": _need(address), "chain": chain},
        reading=f"who funded {address}, and how each is linked to it",
        caveat=(
            "Links are graded --- same wallet, reachable, co-funded --- and a "
            "shared funder is not a shared owner."
        ),
    )


def _stats(address: str, chain: str, since: int | None, m: Any, text: str) -> Plan:
    return Plan(
        endpoint="/health",
        params={},
        reading="how much is in this case",
        caveat="Counts what has been fetched, which is not what exists on a chain.",
    )


#: Intent patterns, most specific first. Regexes rather than keywords so
#: "sent to" and "who sent" do not collide: the first is outbound from the
#: subject, the second inbound to it, and mixing them reverses the direction of
#: an accusation.
_INTENTS: tuple[tuple[str, Any], ...] = (
    (r"\b(?:impersonat|pretend|fake|forged|really\s+(?:usdc|usdt|weth|eth))", _impersonation),
    (r"\b(?:poison|lookalike|look.alike|similar\s+address|mistak)", _poisoning),
    (
        r"\b(?:who\s+funded|funded\s+by|contributor|first\s+funded|source\s+of\s+funds)",
        _contributors,
    ),
    (r"\b(?:expand|follow\s+the\s+money|one\s+hop|fetch|pull\s+in)\b", _expand),
    (
        r"\b(?:who\s+paid|paid\s+(?:it|this|into)|received|incoming|inbound|who\s+sent)\b",
        _received,
    ),
    (r"\b(?:where\s+did|sent\s+to|outgoing|outbound|where\s+.*\bgo\b|spent)", _sent),
    (
        r"\b(?:what\s+is\s+known|who\s+is|what\s+is|label|attribut|identif|tell\s+me\s+about)",
        _known,
    ),
    (r"\b(?:how\s+many|how\s+much|stats|size\s+of|count)\b", _stats),
)
