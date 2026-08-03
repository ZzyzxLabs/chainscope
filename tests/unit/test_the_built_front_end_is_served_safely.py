"""Serving the built front end must not serve anything else.

The path comes from outside the process. `Path` discards the left side of a
join with an absolute path and `..` walks, so a URL is a request to read a file
of the caller's choosing unless every candidate is resolved and checked to be
inside the export. Same containment rule as the bundle reader, for the same
reason.

The token is substituted into the HTML rather than fetched, because a page that
asks an endpoint for a token needs that endpoint to be unauthenticated --- which
is the same as having no token at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chainscope.server.static import StaticSite, content_type


@pytest.fixture
def export(tmp_path: Path) -> Path:
    root = tmp_path / "out"
    (root / "case").mkdir(parents=True)
    (root / "index.html").write_text("<html><head><title>x</title></head><body></body></html>")
    (root / "case" / "index.html").write_text("<html><head></head><body>case</body></html>")
    (root / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("not part of the site")
    return root


def test_it_serves_what_is_in_the_export(export: Path) -> None:
    site = StaticSite(export)
    assert site.available
    assert site.resolve("/") == (export / "index.html").resolve()
    assert site.resolve("/app.js") == (export / "app.js").resolve()


def test_a_trailing_slash_route_resolves_to_its_index(export: Path) -> None:
    """`trailingSlash: true` exports /case as case/index.html."""
    site = StaticSite(export)
    assert site.resolve("/case/") == (export / "case" / "index.html").resolve()
    assert site.resolve("/case") == (export / "case" / "index.html").resolve()


@pytest.mark.parametrize(
    "attack",
    [
        "/../secret.txt",
        "/case/../../secret.txt",
        "/%2e%2e/secret.txt",
        "/....//secret.txt",
    ],
)
def test_it_refuses_to_escape_the_export(export: Path, attack: str) -> None:
    site = StaticSite(export)
    resolved = site.resolve(attack)
    assert resolved is None or resolved.is_relative_to(export.resolve()), (
        f"{attack!r} reached outside the export"
    )


def test_a_query_string_is_not_part_of_the_path(export: Path) -> None:
    site = StaticSite(export)
    assert site.resolve("/case/?a=0xabc&c=1") == (export / "case" / "index.html").resolve()


def test_the_token_is_injected_into_html(export: Path) -> None:
    site = StaticSite(export)
    body = site.read(
        export / "index.html", token="s3cret", store="/tmp/case.db", writable=True
    ).decode()
    assert "__CHAINSCOPE__" in body
    payload = json.loads(body.split("__CHAINSCOPE__=", 1)[1].split(";</script>", 1)[0])
    assert payload == {"token": "s3cret", "store": "/tmp/case.db", "writable": True}
    # Immediately after <head>, so no bundle can run first and find it missing.
    assert body.index("__CHAINSCOPE__") < body.index("<title>")


def test_a_token_with_a_quote_cannot_break_out(export: Path) -> None:
    """JSON-encoded, so a token cannot end its string and start being code."""
    body = (
        StaticSite(export)
        .read(
            export / "index.html",
            token='";alert(1);var x="',
            store="",
            writable=False,
        )
        .decode()
    )
    payload = json.loads(body.split("__CHAINSCOPE__=", 1)[1].split(";</script>", 1)[0])
    assert payload["token"] == '";alert(1);var x="'


def test_non_html_is_returned_untouched(export: Path) -> None:
    site = StaticSite(export)
    assert (
        site.read(export / "app.js", token="t", store="", writable=False) == b"console.log(1)"
    )


def test_a_missing_export_is_not_an_error(tmp_path: Path) -> None:
    """`pip install chainscope` must not require Node to look at a case."""
    assert StaticSite(tmp_path / "nothing").available is False


def test_javascript_is_never_served_as_plain_text() -> None:
    """Browsers refuse it, and the failure is miserable to diagnose remotely."""
    assert content_type(Path("a.js")).startswith("text/javascript")
    assert content_type(Path("a.css")).startswith("text/css")
    assert content_type(Path("a.woff2")) == "font/woff2"
    assert content_type(Path("a.unknown")) == "application/octet-stream"
