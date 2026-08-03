"""Case bundles: an investigation someone else can rerun.

An analysis nobody can reproduce is an assertion. A bundle packages the results
*and the recorded responses that produced them*, so a third party can rerun the
whole thing offline, with no API keys and no network, and get byte-identical
output.

Three uses, in rough order of how often they matter:

**Verification.** Handing over a bundle instead of a PDF changes the
conversation from "do you believe me" to "here, check it".

**Offline testing.** Record once, replay in CI forever. Network in CI produces
flaky tests, and flaky tests drive contributors away faster than missing
features.

**Preservation.** Providers change their APIs, prune history, and go out of
business. A bundle keeps what the chain said at the time you looked.

Commercial platforms generally cannot offer this, because their underlying data
is not permitted to leave the platform.

**Bundles are untrusted input.** One you received was produced by someone else;
treat it as you would any file from a stranger. Loading is deliberately limited
to JSON --- no pickle, no eval, no dynamic import.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.result import Result
from ..transport.cache import Cache

__all__ = ["MANIFEST_VERSION", "Bundle", "BundleError"]

MANIFEST_VERSION = 1


class BundleError(RuntimeError):
    """A bundle is malformed, or written by an incompatible version."""


@dataclass
class Bundle:
    """A directory holding one investigation."""

    path: Path
    title: str = ""
    subject: str = ""
    notes: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    tool_version: str = "0.1.0.dev0"
    results: list[dict[str, Any]] = field(default_factory=list)
    has_case: bool = False
    has_audit: bool = False

    # ---------------------------------------------------------------- writing

    @classmethod
    def create(
        cls, path: Path | str, *, title: str = "", subject: str = "", notes: str = ""
    ) -> Bundle:
        p = Path(path)
        (p / "results").mkdir(parents=True, exist_ok=True)
        b = cls(path=p, title=title, subject=subject, notes=notes)
        b._write_manifest()
        return b

    def add_result(self, result: Result) -> None:
        index = len(self.results)
        name = f"{index:02d}-{result.analyzer}.json"
        (self.path / "results" / name).write_text(result.to_json(), encoding="utf-8")
        self.results.append(
            {
                "file": name,
                "analyzer": result.analyzer,
                "version": result.version,
                "findings": len(result.findings),
                "hypotheses": len(result.hypotheses),
                "warnings": len(result.warnings),
                "params": result.params,
            }
        )
        self._write_manifest()

    def attach_cache(self, cache: Cache) -> int:
        """Copy the query cache in. This is what makes replay possible.

        Without it a bundle documents *what* was concluded but not *from what*,
        and the reader is back to taking your word for it.
        """
        cache.close()
        target = self.path / "queries.sqlite"
        shutil.copy2(cache.path, target)
        return target.stat().st_size

    def attach_case(self, case_db: Path | str) -> int:
        """Copy the case record in --- notes, leads, correspondence, the log.

        Separate from `attach_cache` because the two are not the same kind of
        thing. The cache is derived and rebuildable; `case.db` is what a person
        wrote and cannot be recovered by re-running anything. A bundle that
        carries only the cache hands over the evidence and loses the reasoning.
        """
        source = Path(case_db)
        if not source.is_file():
            raise BundleError(
                f"no case record at {source}. Export without one by passing "
                f"--no-case, but the notes and leads are the part that cannot "
                f"be rebuilt by re-running the tool"
            )
        target = self.path / "case.db"
        shutil.copy2(source, target)
        self.has_case = True
        self._write_manifest()
        return target.stat().st_size

    def attach_audit(self, audit_log: Path | str) -> int:
        """Copy the request log in --- what was asked, of whom, and when."""
        source = Path(audit_log)
        if not source.is_file():
            raise BundleError(f"no audit log at {source}")
        target = self.path / "audit.jsonl"
        shutil.copy2(source, target)
        self.has_audit = True
        self._write_manifest()
        return target.stat().st_size

    def add_report(self, markdown: str, name: str = "report.md") -> None:
        (self.path / name).write_text(markdown, encoding="utf-8")

    def _write_manifest(self) -> None:
        (self.path / "manifest.json").write_text(
            json.dumps(
                {
                    "manifest_version": MANIFEST_VERSION,
                    "tool": "chainscope",
                    "tool_version": self.tool_version,
                    "title": self.title,
                    "subject": self.subject,
                    "notes": self.notes,
                    "created_at": self.created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    # Stated, so a reader knows what is *absent* rather than
                    # inferring it from a missing file. A bundle without a case
                    # record is a legitimate thing to send; a bundle that lost
                    # one silently is not.
                    "has_case": self.has_case,
                    "has_audit": self.has_audit,
                    "results": self.results,
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

    def archive(self, destination: Path | str | None = None) -> Path:
        """Zip the bundle for handing over."""
        dest = Path(destination) if destination else self.path.with_suffix(".zip")
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in sorted(self.path.rglob("*")):
                if item.is_file():
                    zf.write(item, item.relative_to(self.path))
        return dest

    # ---------------------------------------------------------------- reading

    @classmethod
    def open(cls, path: Path | str) -> Bundle:
        """Load a bundle. Treats its contents as untrusted."""
        p = Path(path)
        manifest_file = p / "manifest.json"
        if not manifest_file.exists():
            raise BundleError(f"{p} has no manifest.json --- not a bundle")
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise BundleError(f"manifest is not valid JSON: {exc}") from exc

        version = manifest.get("manifest_version")
        if version != MANIFEST_VERSION:
            raise BundleError(
                f"bundle manifest version {version} but this chainscope reads "
                f"version {MANIFEST_VERSION}"
            )

        created = manifest.get("created_at")
        return cls(
            path=p,
            title=str(manifest.get("title", "")),
            subject=str(manifest.get("subject", "")),
            notes=str(manifest.get("notes", "")),
            created_at=(
                datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                if created
                else datetime.now(timezone.utc)
            ),
            tool_version=str(manifest.get("tool_version", "unknown")),
            results=list(manifest.get("results", [])),
            has_case=bool(manifest.get("has_case", False)),
            has_audit=bool(manifest.get("has_audit", False)),
        )

    @classmethod
    def unpack(cls, archive: Path | str, into: Path | str) -> Bundle:
        """Extract a zipped bundle and open it. Treats the archive as hostile.

        A zip is a list of names somebody else chose, and Python will happily
        write `../../.ssh/authorized_keys` if asked. Each entry is resolved
        against the destination and rejected if it lands outside --- the same
        check `read_result` makes on manifest filenames, for the same reason.
        """
        dest = Path(into).resolve()
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                target = (dest / member.filename).resolve()
                if not target.is_relative_to(dest):
                    raise BundleError(
                        f"bundle entry {member.filename!r} escapes the "
                        f"destination directory; refusing to extract it"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
        return cls.open(dest)

    def case_db(self) -> Path | None:
        """The case record inside this bundle, or None if it carries none."""
        candidate = self.path / "case.db"
        return candidate if candidate.is_file() else None

    def read_result(self, index: int) -> dict[str, Any]:
        """One recorded result, read from inside this bundle and nowhere else.

        The manifest is somebody else's file --- this module's own docstring says
        so --- and the filename in it was joined straight onto the bundle path.
        `pathlib` discards the left side of a join with an absolute path, so an
        entry reading `/etc/passwd` read `/etc/passwd`, and `../../..` walked
        wherever it liked. A stated threat model that nothing enforces is the
        pattern this review keeps finding.
        """
        entry = self.results[index]
        root = (self.path / "results").resolve()
        target = (root / str(entry["file"])).resolve()
        if target != root and root not in target.parents:
            raise BundleError(
                f"manifest entry {entry['file']!r} resolves outside the bundle. "
                f"A bundle is untrusted input; it may only name files inside "
                f"itself."
            )
        data: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))
        return data

    def replay_cache(self) -> Cache | None:
        """Open the bundled query cache for offline replay."""
        db = self.path / "queries.sqlite"
        return Cache(db) if db.exists() else None

    @property
    def replayable(self) -> bool:
        return (self.path / "queries.sqlite").exists()

    def summary(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "subject": self.subject,
            "created_at": self.created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tool_version": self.tool_version,
            "analyses": len(self.results),
            "total_findings": sum(int(r.get("findings", 0)) for r in self.results),
            "total_warnings": sum(int(r.get("warnings", 0)) for r in self.results),
            "replayable": self.replayable,
        }
