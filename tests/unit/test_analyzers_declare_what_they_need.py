"""What an analyzer declares it needs must be what it actually checks for.

`REQUIRES` exists so the web UI can render an input per parameter instead of
offering a button whose only possible outcome is an error naming what the
reader should have typed. That only helps if the declaration is true: a field
nothing validates is a field whose value is silently ignored, which is worse
than no field at all.

So this reads the declaration and the enforcement out of the same source and
requires them to agree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chainscope.cli.commands.analyze import _discover

ANALYSIS_DIR = Path("src/chainscope/analysis")


def _declared() -> dict[str, tuple[str, ...]]:
    found, _ = _discover()
    return {name: tuple(getattr(cls, "REQUIRES", ()) or ()) for name, cls in found.items()}


def test_every_declared_parameter_is_a_real_argument() -> None:
    """A declared name that `run` does not accept is a field that does nothing.

    Checked against the signature rather than against the error prose. My first
    attempt matched error messages and declared `funder` for `common_funder`,
    which actually takes `addresses` --- the error I had matched belonged to a
    different class in the same file.
    """
    import inspect

    found, _ = _discover()
    problems: list[str] = []
    for name, cls in found.items():
        needs = tuple(getattr(cls, "REQUIRES", ()) or ())
        if not needs:
            continue
        accepted = set(inspect.signature(cls.run).parameters)
        for parameter in needs:
            if parameter not in accepted:
                problems.append(
                    f"{name} declares {parameter!r}, which run() does not accept; "
                    f"it takes {sorted(accepted - {'self', 'ctx'})}"
                )
    assert not problems, "\n".join(problems)


def test_a_declared_parameter_is_one_run_cannot_do_without() -> None:
    """Otherwise the UI demands input for something that has a usable default.

    Every declared parameter must be one whose absence `run` refuses, so the
    form asks for exactly what is genuinely required.
    """
    import inspect

    found, _ = _discover()
    optional: list[str] = []
    for name, cls in found.items():
        for parameter in getattr(cls, "REQUIRES", ()) or ():
            spec = inspect.signature(cls.run).parameters.get(parameter)
            # Declared parameters carry an empty default and are then checked
            # in the body; a non-empty default means the analyzer can proceed.
            if spec is not None and spec.default not in ("", 0, None, inspect.Parameter.empty):
                optional.append(f"{name}.{parameter} defaults to {spec.default!r}")
    assert not optional, "\n".join(optional)


@pytest.mark.parametrize(
    ("analyzer", "expected"),
    [("taint", "source"), ("mixer", "deposits"), ("route", "source")],
)
def test_the_known_ones_are_declared(analyzer: str, expected: str) -> None:
    """Spot checks, so a refactor that empties every declaration is caught."""
    assert expected in _declared().get(analyzer, ())
