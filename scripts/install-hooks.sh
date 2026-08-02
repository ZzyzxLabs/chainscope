#!/usr/bin/env bash
# Install git hooks that run the same gates as CI.
#
#   scripts/install-hooks.sh
#
# Uses `core.hooksPath` rather than writing into .git/hooks, so the hooks are
# version-controlled, reviewable, and arrive with a clone --- which is the one
# thing husky actually buys, and it needs no Node to get it. This project has
# no package.json and adding one to install a hook runner would be a strange
# trade.
#
# Two hooks, split by cost:
#
#   pre-commit  ruff + format, on staged files only. Under a second. Catches
#               the slip you would otherwise discover after pushing.
#   pre-push    the full local gate: types and the whole suite. Seconds, not
#               minutes, and it is the last point before other people see it.
#
# Both are bypassable with --no-verify, deliberately. A hook that cannot be
# skipped gets uninstalled the first time someone needs to push a work in
# progress, and then it is protecting nobody.

set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p .githooks

cat > .githooks/pre-commit <<'HOOK'
#!/usr/bin/env bash
# Formatting and lint on staged Python only. Cheap enough to never be resented.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"

# NUL-delimited into an array rather than relying on word splitting. An
# unquoted "$files" is at the mercy of IFS, and when it is not split the whole
# newline-joined list arrives as one filename -- which ruff reports as a
# missing file, naming only the first path. That cost a confusing ten minutes
# the first time this hook ran.
# Read NUL-delimited into an array and filter in the loop. Two earlier
# versions were wrong: an unquoted "$files" is at the mercy of IFS, and when
# it is not split the whole newline-joined list arrives as one enormous
# filename that ruff reports as missing while naming only the first path.
# Piping `-z` output through grep is no better --- the separators are NULs,
# so stripping them joins every path into a single line.
files=()
while IFS= read -r -d '' f; do
  [[ "$f" == *.py ]] && files+=("$f")
done < <(git diff --cached --name-only --diff-filter=ACMR -z)
[ ${#files[@]} -eq 0 ] && exit 0

PY="${PY:-.venv/bin/python}"; [ -x "$PY" ] || PY="python3"
BIN="$(dirname "$PY")"; [ -x "$BIN/ruff" ] || BIN=""
ruff() { if [ -n "$BIN" ]; then "$BIN/ruff" "$@"; else "$PY" -m ruff "$@"; fi; }

fail=0
if ! out=$(ruff check "${files[@]}" 2>&1); then echo "$out" | tail -20; fail=1; fi
if ! out=$(ruff format --check "${files[@]}" 2>&1); then
  echo "$out" | tail -10
  echo "  fix: ruff format ${files[*]}"
  fail=1
fi

if [ "$fail" = 1 ]; then
  echo
  echo "pre-commit failed. --no-verify skips this if you need to."
  exit 1
fi
HOOK

cat > .githooks/pre-push <<'HOOK'
#!/usr/bin/env bash
# The full local gate. The last point before anyone else sees this.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"
exec scripts/ci-local.sh
HOOK

chmod +x .githooks/pre-commit .githooks/pre-push scripts/ci-local.sh
git config core.hooksPath .githooks

cat <<EOF

hooks installed (core.hooksPath = .githooks)

  pre-commit   ruff check + format, staged files only
  pre-push     scripts/ci-local.sh --- lint, types, full suite

Skip either with --no-verify.

What these cannot cover: CI also runs Windows, macOS, and Python 3.10 through
3.13. The last failure that reached main was Windows-only --- its event loop
opens a loopback TCP socket for an internal pipe, which nothing on a Unix
machine does. A green run here means "no obvious problem", not "CI will pass".
EOF
