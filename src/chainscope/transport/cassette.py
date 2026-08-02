"""Recorded provider responses, committed to the repository and replayed offline.

A test suite that mocks its providers proves the mock behaves as written. It
cannot catch the failures that actually happen: a field renamed, a number that
started arriving as a string, an error shape nobody anticipated. Those live in
the boundary between our types and someone else's JSON, and only real responses
touch it.

So the shapes are recorded once against the live API and replayed forever.
Committed fixtures also mean a contributor with no API key can run the whole
suite --- which decides whether the second contributor exists.

**A cassette is a :class:`~chainscope.transport.cache.CacheBackend`.** It needs
no changes anywhere else: the transport already routes every request through
that interface, so recording is "point the client at a cassette instead of a
cache". The cache keys carry no credential (see
:mod:`chainscope.transport.credentials`), which is what makes a cassette
recorded with one key replay under another --- or under none.

**Nothing is written until it is verified clean.** Every value is scrubbed on
the way out, and then the serialised file is checked for any registered
credential before it touches the disk. A fixture committed to a public
repository with a live key in it is a disclosed key, and "we redact carefully"
is not a control. :class:`CredentialLeak` is raised rather than the file being
silently written with a warning nobody reads.

The format is JSON with one entry per interaction, ordered by recording. That
is deliberate: a cassette should be reviewable in a pull request by someone
deciding whether the recorded data is what they think it is.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cache import Volatility
from .credentials import scrub_params, scrub_value

__all__ = [
    "Cassette",
    "CassetteError",
    "CredentialLeak",
    "Interaction",
    "Mode",
    "assert_no_credentials",
]

#: Bumped when the on-disk shape changes.
FORMAT_VERSION = 1


class CassetteError(RuntimeError):
    """A cassette could not be read, or was asked for something it lacks."""


class CredentialLeak(CassetteError):
    """A credential survived scrubbing and reached the point of being written.

    Deliberately fatal. The alternative --- writing the file and logging a
    warning --- produces a committed secret and a warning in a scrollback buffer
    that has already been closed.
    """


class Mode:
    """How a cassette responds to a request."""

    REPLAY = "replay"
    """Serve recordings; never write. A miss is the caller's problem --- in the
    test suite it surfaces as the socket block, which is the correct failure:
    the test asked for something nobody recorded."""

    RECORD = "record"
    """Fetch everything live and write it down, overwriting existing entries.
    Used when re-recording after an API changes shape."""

    ONCE = "once"
    """Replay what exists, fetch and append what does not. The mode for adding
    a new test to an existing cassette without re-recording the rest --- and
    therefore without a diff that hides one real change among forty timestamps."""

    ALL = (REPLAY, RECORD, ONCE)


@dataclass
class Interaction:
    """One recorded response."""

    key: str
    response: Any
    provider: str | None = None
    volatility: str = Volatility.SLOW.value
    label: str = ""
    """Free text describing what was asked, for the human reviewing the diff.
    The key is a hash and tells a reader nothing."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "provider": self.provider,
            "volatility": self.volatility,
            "response": self.response,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Interaction:
        if "key" not in raw or "response" not in raw:
            raise CassetteError(f"malformed interaction: {sorted(raw)}")
        return cls(
            key=str(raw["key"]),
            response=raw["response"],
            provider=raw.get("provider"),
            volatility=str(raw.get("volatility", Volatility.SLOW.value)),
            label=str(raw.get("label", "")),
        )


@dataclass
class Cassette:
    """Recorded interactions, keyed by request hash."""

    path: Path
    mode: str = Mode.REPLAY
    _entries: dict[str, Interaction] = field(default_factory=dict, repr=False)
    _order: list[str] = field(default_factory=list, repr=False)
    _label: str = field(default="", repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _dirty: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if self.mode not in Mode.ALL:
            raise CassetteError(f"unknown mode {self.mode!r}; expected one of {Mode.ALL}")
        self.load()

    # ------------------------------------------------------------------ loading

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CassetteError(f"{self.path}: {exc}") from exc

        version = raw.get("version")
        if version != FORMAT_VERSION:
            # Guessing at an older layout is how a fixture starts silently
            # replaying the wrong field.
            raise CassetteError(
                f"{self.path} is format version {version!r}, this build reads "
                f"{FORMAT_VERSION}. Re-record it."
            )

        for item in raw.get("interactions", []):
            entry = Interaction.from_dict(item)
            if entry.key not in self._entries:
                self._order.append(entry.key)
            self._entries[entry.key] = entry

    # ------------------------------------------------------- CacheBackend

    def get(self, key: str, volatility: Volatility) -> Any | None:
        """Return a recorded response.

        Volatility is ignored on purpose. A recording does not go stale --- it
        is a statement about what the API returned at a known moment, and
        expiring it would mean a test that passes today and fails next week for
        no reason connected to the code.
        """
        if self.mode == Mode.RECORD:
            return None
        with self._lock:
            entry = self._entries.get(key)
        return None if entry is None else entry.response

    def put(
        self,
        key: str,
        value: Any,
        volatility: Volatility,
        *,
        provider: str | None = None,
    ) -> None:
        if self.mode == Mode.REPLAY:
            return
        with self._lock:
            if key not in self._entries:
                self._order.append(key)
            self._entries[key] = Interaction(
                key=key,
                response=scrub_params(value),
                provider=provider,
                volatility=volatility.value,
                label=self._label,
            )
            self._dirty = True

    # ------------------------------------------------------------------ labels

    @contextmanager
    def labelling(self, label: str) -> Iterator[None]:
        """Tag everything recorded inside this block.

        A cassette whose entries are bare hashes cannot be reviewed, and an
        unreviewable fixture gets approved on trust. Recording is a deliberate,
        single-threaded operation, so a plain attribute is honest here; the
        label is cosmetic and a concurrent recorder would only mislabel, never
        corrupt.
        """
        previous = self._label
        self._label = label
        try:
            yield
        finally:
            self._label = previous

    # ------------------------------------------------------------------ writing

    def save(self, *, force: bool = False) -> bool:
        """Write the cassette. Returns whether anything was written.

        Refuses to write a file containing a registered credential --- see
        :class:`CredentialLeak`.
        """
        if not self._dirty and not force:
            return False

        with self._lock:
            payload = {
                "version": FORMAT_VERSION,
                "interactions": [self._entries[k].to_dict() for k in self._order],
            }

        blob = json.dumps(payload, indent=2, sort_keys=False, default=str)
        cleaned = scrub_value(blob)

        # The scrub above is the safety net; this is the assertion that it
        # worked. If the two disagree, something reached the file that the
        # structural pass did not recognise as a credential -- a key inside a
        # URL echoed back in an error message, for instance -- and the right
        # response is to stop, not to write the cleaned version and hope.
        if cleaned != blob:
            raise CredentialLeak(
                f"{self.path}: a registered credential survived structural "
                f"scrubbing and appeared in the serialised cassette. Nothing "
                f"was written. This usually means a provider echoed the key "
                f"back inside a response body or an error string; scrub it in "
                f"the provider before recording."
            )

        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: an interrupted save leaves the previous cassette
        # intact rather than a truncated file that fails to parse on the next run.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(blob + "\n", encoding="utf-8")
        tmp.replace(self.path)
        self._dirty = False
        return True

    # ------------------------------------------------------------------ context

    def __enter__(self) -> Cassette:
        return self

    def __exit__(self, exc_type: object, *_: object) -> None:
        # Do not save on the way out of a failed block: a partial recording
        # committed as though complete is worse than no recording.
        if exc_type is None and self.mode != Mode.REPLAY:
            self.save()

    # ------------------------------------------------------------------ inspect

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        return key in self._entries

    def interactions(self) -> list[Interaction]:
        return [self._entries[k] for k in self._order]

    def labels(self) -> list[str]:
        return [self._entries[k].label or "(unlabelled)" for k in self._order]

    def __repr__(self) -> str:
        return f"<Cassette {self.path.name} mode={self.mode} entries={len(self._entries)}>"


def assert_no_credentials(path: Path | str) -> None:
    """Raise if a cassette on disk contains any currently registered credential.

    Belt and braces for CI: :meth:`Cassette.save` cannot write a leaking file,
    but a cassette added by hand, or recorded before a key was registered, never
    passed through it. Cheap enough to run over every fixture on every push.
    """
    text = Path(path).read_text(encoding="utf-8")
    if scrub_value(text) != text:
        raise CredentialLeak(
            f"{path} contains a live credential. Re-record it, and treat the "
            f"key as disclosed if this file was ever pushed."
        )
