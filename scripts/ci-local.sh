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
# WHAT THIS CANNOT CATCH: CI also runs Windows and macOS, and Python 3.10
# through 3.13. Platform-specific failures --- a path separator, an event loop
# that behaves differently, a stdlib change --- will still only appear there.
# The last one that reached main was exactly that shape: Windows' proactor
# event loop opens an AF_INET socket for its own self-pipe, which nothing on a
# Unix machine does. Treat a green run here as "no obvious problem", not as
# "CI will pass".

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
