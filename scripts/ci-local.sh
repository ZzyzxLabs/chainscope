#!/usr/bin/env bash
# Run the same gates CI runs, locally, in the same order.
#
#   scripts/ci-local.sh          fast: lint, types, tests
#   scripts/ci-local.sh --full   adds the build, the cassette scan, and a
#                                second interpreter if one is installed
#
# Ordered cheapest-first so the common failure is the first thing reported. A
# gate that makes you wait ninety seconds to be told about a formatting slip
# gets bypassed, and a bypassed gate is not a gate.
#
# These gates are the primary ones. GitHub runs a single Linux/3.10 job on
# push; everything else there --- Windows, the rest of the version matrix,
# Docker, Nix, uvx --- is manual (`workflow_dispatch` with `full`), because
# paying minutes per push for a second opinion on what already ran here is a
# poor trade.
#
# WHAT THIS CANNOT CATCH, and what to run `full` for: platform-specific
# failures. The last one that reached main was exactly that shape --- Windows'
# proactor event loop opens an AF_INET socket for its own self-pipe, which
# nothing on a Unix machine does. Packaging is the other: the Nix flake was
# written on a machine without Nix and shipped without a keccak backend, and
# the on-demand job is the only thing that has ever checked it.
#
# So: green here means "no obvious problem". Run `full` before a release, after
# touching packaging, and when anything reaches the network or the loopback
# server.

set -uo pipefail
cd "$(dirname "$0")/.."

PY="${PY:-.venv/bin/python}"
[ -x "$PY" ] || PY="python3"
BIN="$(dirname "$PY")"
[ -x "$BIN/ruff" ] || BIN=""

FULL=0
[ "${1:-}" = "--full" ] && FULL=1

bold=$'\033[1m'; red=$'\033[31m'; green=$'\033[32m'; dim=$'\033[2m'; off=$'\033[0m'
failed=()
start=$SECONDS

step() {
  local name="$1"; shift
  local began=$SECONDS
  printf '%s%-34s%s' "$dim" "$name" "$off"
  if out=$("$@" 2>&1); then
    printf '\r\033[K%s%-34s%s %s✓%s %ss\n' "$bold" "$name" "$off" "$green" "$off" "$((SECONDS - began))"
  else
    printf '\r\033[K%s%-34s%s %s✗%s %ss\n' "$bold" "$name" "$off" "$red" "$off" "$((SECONDS - began))"
    echo "$out" | tail -25 | sed 's/^/    /'
    failed+=("$name")
  fi
}

ruff() { if [ -n "$BIN" ]; then "$BIN/ruff" "$@"; else "$PY" -m ruff "$@"; fi; }
mypy() { if [ -n "$BIN" ]; then "$BIN/mypy" "$@"; else "$PY" -m mypy "$@"; fi; }

echo "${bold}local gates${off} ${dim}($("$PY" --version 2>&1))${off}"
echo

step "ruff check"        ruff check src tests scripts
step "ruff format"       ruff format --check src tests scripts
step "mypy (strict)"     mypy
step "pytest"            "$PY" -m pytest -q --tb=short

# Cheap, pure-Python, and previously GitHub-only. They belong on this side of
# the line: they cost under a second and catch things that are expensive to
# discover late --- a key in a committed fixture is a disclosed key by the time
# anyone reads the diff.
step "attribution sources documented" "$PY" - <<'PY'
import pathlib, sys
src = pathlib.Path("src/chainscope/attribution/sources")
if not src.exists():
    sys.exit(0)
doc = pathlib.Path("docs/data-sources.md").read_text().lower()
missing = [p.stem for p in src.glob("*.py")
           if not p.stem.startswith("_") and p.stem.lower() not in doc]
if missing:
    print("Undocumented attribution sources:", ", ".join(missing))
    print("Add a row to docs/data-sources.md: publisher, license,")
    print("redistribution terms, confidence level.")
    sys.exit(1)
PY

if [ "$FULL" = 1 ]; then
  step "fixtures carry no secrets" "$PY" - <<'PY'
import pathlib, re, sys
FIX = sorted(pathlib.Path("tests/cassettes").glob("*.json"))
SUSPECT = re.compile(
    r"(?i)(apikey|api_key|token|secret|password|authorization)"
    r"\"?\s*[:=]\s*\"?([A-Za-z0-9_\-]{16,})")
PATH_KEY = re.compile(r"/(v\d+)/([A-Za-z0-9_-]{20,})")
bad = []
for f in FIX:
    text = f.read_text()
    bad += [f"{f}: {m.group(1)}" for m in SUSPECT.finditer(text) if m.group(2) != "redacted"]
    bad += [f"{f}: /{m.group(1)}/..." for m in PATH_KEY.finditer(text)]
if bad:
    print("\n".join(bad)); sys.exit(1)
PY

  step "build & metadata" bash -c "$PY -m build --outdir /tmp/cs-dist >/dev/null && $PY -m twine check /tmp/cs-dist/*"

  # A second interpreter catches the syntax-level 3.10/3.13 splits that the
  # primary one hides. Only if one happens to be installed; not worth a prompt.
  for alt in python3.10 python3.13; do
    if command -v "$alt" >/dev/null 2>&1; then
      step "import on $alt" "$alt" -c "import sys; sys.path.insert(0,'src'); import chainscope"
    fi
  done
fi

echo
if [ ${#failed[@]} -eq 0 ]; then
  echo "${green}${bold}all gates passed${off} in ${dim}$((SECONDS - start))s${off}"
  [ "$FULL" = 0 ] && echo "${dim}(--full adds the build and fixture scan; CI still owns Windows and the version matrix)${off}"
  exit 0
fi
echo "${red}${bold}${#failed[@]} failed:${off} ${failed[*]}"
exit 1
