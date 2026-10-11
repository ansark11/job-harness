#!/usr/bin/env bash
# Runs every component's test suite; exits non-zero if any fails.
# This is <TEST_COMMAND> for the agent instructions (see CLAUDE.md).
# Suites are discovered: any services/<name>/tests or workflows/<name>/tests
# runs with that component's own venv (./python/bin/python) if it has one.
set -u
cd "$(dirname "$0")/.."
status=0

suite() {
  local dir=$1 py=python3
  [ -x "$dir/python/bin/python" ] && py=./python/bin/python
  echo "== $dir"
  if ! (cd "$dir" && "$py" -m unittest discover -s tests -t .); then
    echo "!! $dir tests FAILED"
    status=1
  fi
}

for dir in services/* workflows/*; do
  [ -d "$dir/tests" ] && suite "$dir"
done

echo "== scripts"
if ! python3 -m unittest discover -s scripts/tests -t scripts; then
  echo "!! scripts tests FAILED"
  status=1
fi

echo "== runlog.py copies"
# Services don't import each other, so the structured logger is copied into
# each component. The copies must stay identical to scripts/runlog.py.
for copy in services/*/runlog.py workflows/*/runlog.py; do
  [ -e "$copy" ] || continue
  if ! cmp -s scripts/runlog.py "$copy"; then
    echo "!! $copy differs from scripts/runlog.py"
    status=1
  fi
done

[ $status -eq 0 ] && echo "== all suites passed"
exit $status
