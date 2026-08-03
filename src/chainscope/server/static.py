"""Serve the built front end, with this run's token in it.

The Next front end is a static export: HTML, CSS and JS on disk, no Node at
runtime. This serves it from the same process that answers the API, which is
not an implementation detail --- it is what keeps the page same-origin with its
data and keeps the token out of a URL. A front end on its own port would mean
CORS, a second thing to run, and a credential travelling in a query string.

**The token is substituted into the HTML, never fetched.** A page that asks an
endpoint for a token needs that endpoint to be unauthenticated, which is the
same as having no token. So `serve` rewrites a placeholder in the exported HTML
on the way out, exactly as the previous inline page did.

**Missing build is not an error.** `pip install chainscope` gets a Python
package; nobody should need Node to look at their own case. When the export is
absent this reports that plainly and the caller falls back to the inline page.
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = ["EXPORT_DIR", "StaticSite", "cache_control", "content_type"]

#: Where `npm run build` puts the export, relative to the repository root.
#: Shipped in the wheel as package data so an installed copy has it too.
EXPORT_DIR = "web_export"

#: What the export expects to find. Written into the HTML head, so the page has
#: its token before its first script runs and never has to ask for one.
_BOOT = "<script>window.__CHAINSCOPE__=%s;</script>"

_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".txt": "text/plain; charset=utf-8",
}


def cache_control(path: Path) -> str:
    """How long a built asset may be reused.

    HTML is **never** cached. It carries this run's token, and a cached copy
    would hand a stale credential to the next run --- or, worse, keep working
    after the server that minted it has gone. It is also how a rebuilt UI
    reaches the reader at all: a cached shell loading fresh chunks is how a
    page ends up half-updated and blaming the server.

    Everything else is content-hashed by the build (`app-a7ba4d37.js`), so a
    changed file is a changed name and a long cache is safe.
    """
    if path.suffix.lower() in (".html", ".txt", ".json"):
        return "no-store"
    return "public, max-age=31536000, immutable"


def content_type(path: Path) -> str:
    """The type for a built asset.

    An explicit table rather than `mimetypes`, whose answers depend on the
    machine's `/etc/mime.types`. A `.js` served as `text/plain` is refused by
    every browser, and debugging that from a user's report is miserable.
    """
    return _TYPES.get(path.suffix.lower(), "application/octet-stream")


class StaticSite:
    """A built export on disk, if there is one."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    @property
    def available(self) -> bool:
        return (self.root / "index.html").is_file()

    def resolve(self, url_path: str) -> Path | None:
        """Map a URL to a file inside the export, or `None`.

        Every candidate is resolved and checked to be inside the root. The URL
        comes from outside the process, `Path` discards the left side of a join
        with an absolute path, and `..` walks --- the same containment check the
        bundle reader makes on manifest filenames, for the same reason.
        """
        clean = url_path.split("?", 1)[0].split("#", 1)[0].lstrip("/")
        if not clean or clean.endswith("/"):
            clean += "index.html"

        candidates = [clean]
        if not Path(clean).suffix:
            # `trailingSlash: true` exports `/case` as `case/index.html`.
            candidates += [f"{clean}/index.html", f"{clean}.html"]

        for candidate in candidates:
            target = (self.root / candidate).resolve()
            if not target.is_relative_to(self.root):
                return None
            if target.is_file():
                return target
        return None

    def read(self, target: Path, *, token: str, store: str, writable: bool) -> bytes:
        """The file's bytes, with the boot object injected into any HTML.

        Injected per request rather than baked at build time because the token
        is minted per run --- a built-in one would be the same for every user of
        every copy, which is not a token.
        """
        data = target.read_bytes()
        if target.suffix.lower() != ".html":
            return data

        boot = _BOOT % json.dumps({"token": token, "store": store, "writable": writable})
        text = data.decode("utf-8")
        # Before anything else in <head>, so no bundle can run first and find
        # the object missing.
        marker = "<head>"
        index = text.find(marker)
        if index == -1:
            return (boot + text).encode()
        cut = index + len(marker)
        return (text[:cut] + boot + text[cut:]).encode()
