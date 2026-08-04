"""A stub package that only exists on one machine makes the gates a lie.

Every push to main failed CI for weeks with one error::

    src/chainscope/attribution/sources/tagpack.py:156:
      error: Library stubs not installed for "yaml"

and nobody could reproduce it, because reproducing it required a machine that
had *never* installed `types-PyYAML`. A local venv that picked the stub up once
--- from an editor, from `mypy --install-types`, from an earlier experiment ---
keeps it, so `scripts/ci-local.sh` type-checked against a dependency set CI
does not have and reported green.

That is the worst shape a gate can take: passing locally, failing remotely, on
something invisible from where the work happens. The fix is one line in
`pyproject.toml`; this test is what makes the next one fail in the right place.

It is deliberately near-vacuous in CI --- where only declared packages are
installed, so there is nothing undeclared to find --- and useful on a
developer's machine, which is where the divergence lives.
"""

from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


#: A quoted requirement, e.g. `"types-PyYAML>=6.0"` or `"rich>=13.0"`.
_REQUIREMENT = re.compile(r'"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*[<>=!~;"]')


@pytest.fixture(scope="module")
def declared() -> set[str]:
    """Every distribution named anywhere in pyproject, lowercased.

    Read with a regex rather than a TOML parser on purpose. `tomllib` is
    standard library from 3.11 and CI runs **3.10**, so importing it here
    would trade one remote-only failure for another --- which is precisely the
    class of bug this file exists to stop. `tomli` would be a dependency added
    for the benefit of a guard, which is worse than being approximate: the
    question is only ever "does this name appear as a requirement", and a
    false positive costs nothing while a missing parser costs a red build.
    """
    text = PYPROJECT.read_text()
    return {name.lower().replace("_", "-") for name in _REQUIREMENT.findall(text)}


def installed_stub_packages() -> set[str]:
    return {
        (dist.metadata["Name"] or "").lower().replace("_", "-")
        for dist in metadata.distributions()
        if (dist.metadata["Name"] or "").lower().startswith("types-")
    }


def test_every_installed_stub_package_is_declared(declared: set[str]) -> None:
    """Otherwise mypy sees more here than it does in CI."""
    undeclared = sorted(installed_stub_packages() - declared)
    assert not undeclared, (
        "these type-stub packages are installed here but declared nowhere in "
        f"pyproject.toml: {undeclared}. mypy is therefore checking against a "
        "dependency set CI does not have, and a local green run says nothing "
        "about the remote one. Add them to the `dev` extra."
    )


def test_the_stub_for_the_one_optional_parser_we_import_is_declared(
    declared: set[str],
) -> None:
    """`tagpack.py` imports yaml behind an optional extra, and CI installs
    `all`. The stub has to travel with the parser or the type check fails on a
    module the runtime handles fine."""
    assert "types-pyyaml" in declared, (
        "`chainscope.attribution.sources.tagpack` imports yaml and CI installs "
        "the `all` extra, so mypy will resolve the import and demand stubs. "
        "This exact omission failed every push for weeks."
    )


def test_pyproject_is_where_this_is_asserted_from() -> None:
    """A guard that reads the wrong file passes for the wrong reason."""
    assert PYPROJECT.is_file()
    assert "[project]" in PYPROJECT.read_text()
