"""Getting labels in: bulk import, with the provenance rules still enforced.

The framework's central promise is that other people bring their own labels.
That makes this module the place where somebody's first ten minutes are either
productive or spent reading source, so it takes the file they already have --- a
CSV exported from a spreadsheet, a JSON dump from an explorer, a JSONL feed ---
rather than a format invented here.

**What it will not do is let a label in without a source.** That is not a policy
this module implements; it is a property of
:class:`~chainscope.core.attribution.Attribution`, which cannot be constructed
without one. This module's job is to fail at the point of *import*, naming the
row, instead of somewhere later with a stack trace. A label whose origin nobody
recorded becomes, three tools downstream, a fact nobody can trace back.

**Conflicts are reported, never resolved silently.** Two sources disagreeing
about an address is ordinary and often the interesting part --- an exchange
label over a mixer label is a finding, not a data-quality problem. Import says
what disagrees and writes both; the resolver decides at read time.

**Every import is a dry run first.** ``--apply`` is a separate step. Getting
thirty thousand mislabelled addresses out of a store is much harder than not
putting them in, and a column-mapping mistake produces exactly that.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.attribution import Attribution, Category, Confidence, Method
from ..core.chainid import ChainId

__all__ = [
    "DEFAULT_COLUMNS",
    "ImportError_",
    "ImportPlan",
    "RowError",
    "ingest_file",
    "parse_rows",
    "plan_import",
]


class ImportError_(RuntimeError):
    """The file could not be read or understood. Named with a trailing
    underscore to leave the builtin alone."""


@dataclass(frozen=True, slots=True)
class RowError:
    """One row that could not become an :class:`Attribution`.

    Carries the row number because "invalid category" in a thirty-thousand-line
    file is not actionable without it.
    """

    row: int
    reason: str
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"row {self.row}: {self.reason}"


@dataclass(frozen=True, slots=True)
class Conflict:
    """An incoming claim that disagrees with one already stored."""

    address: str
    incoming: Attribution
    existing: Attribution

    def __str__(self) -> str:
        return (
            f"{self.address[:12]}…: incoming {self.incoming.category.value}"
            f"/{self.incoming.confidence.name} from {self.incoming.source} vs "
            f"stored {self.existing.category.value}/{self.existing.confidence.name} "
            f"from {self.existing.source}"
        )


@dataclass
class ImportPlan:
    """What an import would do, before it does it."""

    attributions: list[Attribution] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    duplicates: int = 0
    source: str = ""
    path: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.attributions) and not self.errors

    def summary(self) -> dict[str, Any]:
        by_category: dict[str, int] = {}
        for a in self.attributions:
            key = a.category.value
            by_category[key] = by_category.get(key, 0) + 1
        return {
            "path": self.path,
            "source": self.source,
            "ready": len(self.attributions),
            "errors": len(self.errors),
            "conflicts": len(self.conflicts),
            "duplicates_within_file": self.duplicates,
            "by_category": dict(sorted(by_category.items())),
        }


#: Column names accepted for each field, in preference order. Generous on
#: purpose: the file somebody already has is more likely to say "wallet" or
#: "tag" than to match a schema they have never read.
DEFAULT_COLUMNS: dict[str, tuple[str, ...]] = {
    "address": ("address", "addr", "wallet", "account", "entity_address"),
    "label": ("label", "name", "tag", "entity", "nametag", "description"),
    "category": ("category", "type", "kind", "entity_type", "class"),
    "confidence": ("confidence", "certainty", "score"),
    "chain": ("chain", "network", "chain_id", "blockchain"),
    "rationale": ("rationale", "reason", "note", "notes", "evidence", "comment"),
    "method": ("method", "how", "basis"),
    "observed_at": ("observed_at", "date", "timestamp", "seen", "first_seen"),
}

#: Words seen in real label dumps, mapped to the categories this project uses.
_CATEGORY_ALIASES: dict[str, Category] = {
    "exchange": Category.CEX,
    "cex": Category.CEX,
    "centralized exchange": Category.CEX,
    "dex": Category.DEX,
    "decentralized exchange": Category.DEX,
    "bridge": Category.BRIDGE,
    "mixer": Category.MIXER,
    "tumbler": Category.MIXER,
    "sanctioned": Category.SANCTIONED,
    "sanction": Category.SANCTIONED,
    "ofac": Category.SANCTIONED,
    "sdn": Category.SANCTIONED,
    "illicit": Category.ILLICIT,
    "scam": Category.ILLICIT,
    "phishing": Category.ILLICIT,
    "hack": Category.ILLICIT,
    "exploit": Category.ILLICIT,
    "token": Category.TOKEN,
    "contract": Category.CONTRACT,
    "service": Category.SERVICE,
}

_CONFIDENCE_ALIASES: dict[str, Confidence] = {
    "certain": Confidence.CERTAIN,
    "confirmed": Confidence.CERTAIN,
    "high": Confidence.HIGH,
    "medium": Confidence.MEDIUM,
    "med": Confidence.MEDIUM,
    "low": Confidence.LOW,
    "speculative": Confidence.SPECULATIVE,
    "guess": Confidence.SPECULATIVE,
    "unconfirmed": Confidence.SPECULATIVE,
}

_METHOD_ALIASES: dict[str, Method] = {
    "list": Method.LIST,
    "label": Method.LABEL,
    "nametag": Method.LABEL,
    "onchain": Method.ONCHAIN,
    "on-chain": Method.ONCHAIN,
    "heuristic": Method.HEURISTIC,
    "inference": Method.INFERENCE,
    "inferred": Method.INFERENCE,
    "manual": Method.MANUAL,
}


# --------------------------------------------------------------------- reading


def _rows_from_csv(path: Path) -> Iterator[Mapping[str, Any]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        # utf-8-sig, because a spreadsheet export begins with a BOM and the
        # first column name silently becomes "﻿address" without it --- the
        # kind of failure that looks like "your importer cannot find my address
        # column" and takes an hour to see.
        yield from csv.DictReader(fh)


def _rows_from_json(path: Path) -> Iterator[Mapping[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        for position, record in enumerate(data, 1):
            if isinstance(record, dict):
                yield record
            else:
                # Surfaced as a row rather than dropped. A file with malformed
                # entries otherwise imports "cleanly" while data goes missing.
                yield {
                    "_malformed": (
                        f"entry {position} is a {type(record).__name__}, not an object"
                    )
                }
    elif isinstance(data, dict):
        # The address-keyed shape that label files usually take.
        for address, value in data.items():
            if isinstance(value, dict):
                yield {"address": address, **value}
            else:
                yield {"address": address, "label": str(value)}
    else:
        raise ImportError_(f"{path}: expected a list or an object at the top level")


def _rows_from_jsonl(path: Path) -> Iterator[Mapping[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                raise ImportError_(f"{path}:{n}: {exc}") from exc
            if isinstance(record, dict):
                yield record
            else:
                yield {"_malformed": f"line {n} is a {type(record).__name__}, not an object"}


def read_rows(path: Path | str) -> Iterator[Mapping[str, Any]]:
    """Yield raw records from CSV, JSON, or JSONL, chosen by extension."""
    file = Path(path)
    if not file.is_file():
        raise ImportError_(f"no such file: {file}")
    suffix = file.suffix.lower()
    if suffix == ".csv":
        return _rows_from_csv(file)
    if suffix in (".jsonl", ".ndjson"):
        return _rows_from_jsonl(file)
    if suffix == ".json":
        return _rows_from_json(file)
    raise ImportError_(
        f"{file}: unrecognised extension {suffix!r}. Supported: .csv, .json, .jsonl"
    )


# --------------------------------------------------------------------- mapping


def _pick(row: Mapping[str, Any], names: Sequence[str]) -> Any:
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for name in names:
        if name in lowered and lowered[name] not in (None, ""):
            return lowered[name]
    return None


def _category(value: Any) -> Category:
    if value in (None, ""):
        return Category.SERVICE
    text = str(value).strip().lower()
    if text in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[text]
    try:
        return Category(text)
    except ValueError as exc:
        known = ", ".join(sorted({c.value for c in Category}))
        raise ValueError(f"unknown category {value!r} (known: {known})") from exc


def _confidence(value: Any, default: Confidence) -> Confidence:
    if value in (None, ""):
        return default
    text = str(value).strip().lower()
    if text in _CONFIDENCE_ALIASES:
        return _CONFIDENCE_ALIASES[text]
    if text.isdigit():
        n = int(text)
        if 0 <= n <= 4:
            return Confidence(n)
    raise ValueError(
        f"unknown confidence {value!r} (names: {', '.join(_CONFIDENCE_ALIASES)}, or 0-4)"
    )


def _method(value: Any, default: Method) -> Method:
    if value in (None, ""):
        return default
    text = str(value).strip().lower()
    if text in _METHOD_ALIASES:
        return _METHOD_ALIASES[text]
    try:
        return Method(text)
    except ValueError as exc:
        raise ValueError(f"unknown method {value!r}") from exc


def _observed(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text.isdigit():
        return datetime.fromtimestamp(int(text), tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    source: str,
    chain: ChainId | None = None,
    default_confidence: Confidence = Confidence.MEDIUM,
    default_method: Method = Method.LIST,
    columns: Mapping[str, Sequence[str]] | None = None,
) -> tuple[list[Attribution], list[RowError]]:
    """Turn raw records into attributions, collecting failures rather than raising.

    One bad row must not lose the other twenty-nine thousand, and a caller
    deciding whether to proceed needs to see all the problems at once rather
    than one per re-run.
    """
    if not source.strip():
        raise ImportError_(
            "an import needs a source --- the name of the list, dump, or person "
            "the labels came from. A label whose origin nobody recorded becomes, "
            "three tools later, a fact nobody can trace back."
        )

    mapping = {**DEFAULT_COLUMNS, **(columns or {})}
    good: list[Attribution] = []
    bad: list[RowError] = []

    for n, row in enumerate(rows, 1):
        try:
            if "_malformed" in row:
                raise ValueError(str(row["_malformed"]))
            address = _pick(row, mapping["address"])
            if not address:
                raise ValueError(
                    f"no address column found (looked for: {', '.join(mapping['address'])})"
                )
            label = _pick(row, mapping["label"])
            if not label:
                raise ValueError(
                    f"no label column found (looked for: {', '.join(mapping['label'])})"
                )

            confidence = _confidence(_pick(row, mapping["confidence"]), default_confidence)
            rationale = str(_pick(row, mapping["rationale"]) or "")

            row_chain = chain
            raw_chain = _pick(row, mapping["chain"])
            if raw_chain:
                row_chain = _to_chain(raw_chain)

            good.append(
                Attribution(
                    label=str(label).strip(),
                    category=_category(_pick(row, mapping["category"])),
                    confidence=confidence,
                    method=_method(_pick(row, mapping["method"]), default_method),
                    source=source,
                    address=str(address).strip(),
                    chain=row_chain,
                    rationale=rationale,
                    observed_at=_observed(_pick(row, mapping["observed_at"])),
                )
            )
        except Exception as exc:
            bad.append(RowError(row=n, reason=str(exc), raw=dict(row)))

    return good, bad


def _to_chain(value: Any) -> ChainId | None:
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.isdigit():
            return ChainId.evm(int(text))
        if ":" in text:
            namespace, _, reference = text.partition(":")
            return ChainId(namespace, reference)
    except ValueError:
        pass
    # A bare name such as "ethereum" is ambiguous across namespaces and is not
    # guessed at: a label filed against the wrong chain is worse than one filed
    # against none, because it looks answered.
    from ..core import chainid as _chainid

    named = getattr(_chainid, text.upper(), None)
    return named if isinstance(named, ChainId) else None


# --------------------------------------------------------------------- planning


def plan_import(
    path: Path | str,
    *,
    source: str,
    chain: ChainId | None = None,
    existing: Mapping[str, Sequence[Attribution]] | None = None,
    default_confidence: Confidence = Confidence.MEDIUM,
    default_method: Method = Method.LIST,
    columns: Mapping[str, Sequence[str]] | None = None,
) -> ImportPlan:
    """Work out what an import would do, without doing it.

    ``existing`` maps address to the claims already stored, so conflicts are
    reported before anything is written rather than discovered afterwards.
    """
    file = Path(path)
    attributions, errors = parse_rows(
        read_rows(file),
        source=source,
        chain=chain,
        default_confidence=default_confidence,
        default_method=default_method,
        columns=columns,
    )

    seen: set[tuple[str, str, str]] = set()
    unique: list[Attribution] = []
    duplicates = 0
    for a in attributions:
        # The chain belongs in the key. Without it, the same label on Ethereum
        # and BSC collapses to one row and a chain-scoped attribution is lost
        # before it is written.
        key = ((a.address or "").lower(), a.label.lower(), str(a.chain or ""))
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        unique.append(a)

    conflicts: list[Conflict] = []
    if existing:
        lookup = {k.lower(): v for k, v in existing.items()}
        for a in unique:
            for prior in lookup.get((a.address or "").lower(), ()):
                if _disagrees(a, prior):
                    conflicts.append(Conflict(a.address or "", a, prior))

    return ImportPlan(
        attributions=unique,
        errors=errors,
        conflicts=conflicts,
        duplicates=duplicates,
        source=source,
        path=str(file),
    )


def _disagrees(a: Attribution, b: Attribution) -> bool:
    """Whether two claims about one address are genuinely in tension.

    Sanctions are excluded from the comparison because they are an overlay
    rather than a service type: a sanctioned mixer is both, and counting that
    as a disagreement would fire on nearly every sanctioned entity.
    """
    if Category.SANCTIONED in (a.category, b.category):
        return False
    return a.category != b.category


# --------------------------------------------------------------------- applying


def ingest_file(
    path: Path | str,
    store: Any,
    *,
    source: str,
    chain: ChainId | None = None,
    apply: bool = False,
    default_confidence: Confidence = Confidence.MEDIUM,
    default_method: Method = Method.LIST,
    columns: Mapping[str, Sequence[str]] | None = None,
) -> ImportPlan:
    """Plan an import and, only if ``apply``, write it.

    Defaulting to a dry run is deliberate. Undoing thirty thousand mislabelled
    addresses is far harder than not writing them, and a column-mapping mistake
    produces exactly that quantity of them.
    """
    # Parsed first, so the store is only asked about addresses the file
    # actually mentions. `plan_import` is called twice rather than reaching
    # into its internals: the second pass is over already-validated rows and
    # costs nothing next to reading the file.
    parsed = plan_import(
        path,
        source=source,
        chain=chain,
        default_confidence=default_confidence,
        default_method=default_method,
        columns=columns,
    )

    # Conflicts were dead in this path until this existed: `ingest_file` holds
    # the store and never asked it anything, so `chainscope tag file --apply`
    # imported a "mixer" claim over an existing "cex" one and reported nothing.
    # The whole point of recording disagreement is that somebody sees it.
    existing: dict[str, list[Attribution]] = {}
    for attribution in parsed.attributions:
        address = attribution.address or ""
        if address and address not in existing:
            try:
                existing[address] = list(store.attributions(address))
            except Exception:
                existing[address] = []

    plan = plan_import(
        path,
        source=source,
        chain=chain,
        existing=existing,
        default_confidence=default_confidence,
        default_method=default_method,
        columns=columns,
    )
    if apply and plan.attributions:
        store.put_attributions(plan.attributions)
    return plan
