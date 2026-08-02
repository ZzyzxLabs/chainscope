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
        )

    def read_result(self, index: int) -> dict[str, Any]:
        entry = self.results[index]
        raw = (self.path / "results" / str(entry["file"])).read_text(encoding="utf-8")
        data: dict[str, Any] = json.loads(raw)
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
