"""Leads: somewhere to look next, kept apart from anything concluded.

An investigation runs out of chain long before it runs out of question. The
money reaches a deposit address and the next move is off-chain --- a handle, a
domain, a forum post --- and that move is where a careful tool most easily
stops being careful, because the material stops being verifiable and nobody
changes how they talk about it.

**A lead is not an attribution, and this module exists to keep them apart.**
:class:`~chainscope.core.attribution.Attribution` says *what an address is*.
A lead says *where somebody might find out*. An ENS text record reading
``com.twitter = alice`` does not mean the address belongs to @alice; it means
whoever controls that name typed "alice" into a field. Recorded as an
attribution it would sit in the store next to forward-confirmed claims and be
quoted as one.

So leads are a separate type, they never become claims on their own, and every
one carries :attr:`Lead.verify_by` --- the specific thing that would confirm it.
A lead with no stated verification step is a rumour with a schema.

**The asymmetry is the whole point, and it is the same one ENS already has.**
:mod:`chainscope.attribution.ens` refuses an unconfirmed reverse record because
anybody can point a name at any address; confirmation means resolving forward
and landing back where you started. A text record has the identical shape
across a boundary this tool *cannot* cross: confirming ``com.twitter = alice``
means @alice publishing this address, which lives on a website. So the
verification step is stated and the confirmation is left to a person.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..attribution.ens import EnsRecord

__all__ = ["TEXT_KEYS", "Lead", "leads_from_text_records"]

#: ENS text-record keys worth following, and what each is.
#:
#: Deliberately a fixed map rather than "every key present". A resolver can
#: hold arbitrary keys, and turning an unknown one into a lead named after
#: itself produces confident-looking noise --- the failure this package is
#: arranged against, in a module whose material is already the least reliable
#: thing here.
TEXT_KEYS = {
    "com.twitter": "twitter",
    "com.github": "github",
    "com.discord": "discord",
    "org.telegram": "telegram",
    "email": "email",
    "url": "url",
    "description": "description",
}

#: How each kind is confirmed. The point of the module: the step is named, and
#: it is always something a **person** does somewhere this tool cannot reach.
_VERIFY = {
    "twitter": "check whether that account has published this address itself",
    "github": "check whether that account's profile or commits carry this address",
    "discord": "check whether that handle is used by the account this claims",
    "telegram": "check whether that handle publishes this address",
    "email": "an address in a record is not proof of control of the mailbox",
    "url": "check whether that site publishes this address, not merely the name",
    "description": "free text set by the name owner; treat as a starting point only",
}


@dataclass(frozen=True, slots=True)
class Lead:
    """Somewhere to look, and what would settle it.

    Never a claim about the address. Everything here is self-asserted by
    whoever controls the record it came from, which is stated on the face of
    every lead rather than left to the reader.
    """

    address: str
    kind: str
    """``twitter``, ``github``, ``url``… one of :data:`TEXT_KEYS`'s values."""

    value: str
    source: str
    """Where this was read, precisely enough to fetch again."""

    asserted_by: str
    """Who put it there. For a text record, the owner of the name --- which is
    not necessarily the address, and the difference is the lead's whole risk."""

    verify_by: str
    """What would confirm it. Required: a lead without one is a rumour."""

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("a lead needs a value")
        if not self.verify_by.strip():
            raise ValueError(
                "a lead needs a verification step. Without one it is a rumour "
                "with a schema, and it will be read as a finding"
            )

    def __str__(self) -> str:
        return f"{self.kind}: {self.value} (asserted by {self.asserted_by})"


def leads_from_text_records(
    record: EnsRecord, text: dict[str, str] | None = None
) -> list[Lead]:
    """Turn an ENS name's text records into leads.

    Only from a **forward-confirmed** name. An unconfirmed one is a claim by a
    stranger about somebody else's address, and its text records are then that
    stranger's text records --- following them would attach another person's
    handles to this address, which is worse than finding nothing.

    Unknown keys are skipped rather than passed through. A resolver can hold
    anything, and a lead named after a key nobody recognises reads as a finding
    about a field the reader assumes somebody understood.
    """
    if not record.is_confirmed:
        return []

    found: list[Lead] = []
    for key, kind in TEXT_KEYS.items():
        value = (text or {}).get(key, "").strip()
        if not value:
            continue
        found.append(
            Lead(
                address=record.address,
                kind=kind,
                value=value,
                source=f"ENS text record {key} on {record.name}",
                # The name's owner, said plainly. On a confirmed record the
                # address did set the reverse entry --- but the *text* records
                # belong to whoever controls the name today, which can have
                # changed hands since.
                asserted_by=f"the owner of {record.name}",
                verify_by=_VERIFY[kind],
            )
        )
    return found
