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

echo "== shared module copies"
# Services don't import each other, so shared modules (the structured
# logger, the model client) are copied into each component that uses them.
# Every copy must stay identical to its master in scripts/.
for name in runlog.py llm.py; do
  for copy in services/*/"$name" workflows/*/"$name"; do
    [ -e "$copy" ] || continue
    if ! cmp -s "scripts/$name" "$copy"; then
      echo "!! $copy differs from scripts/$name"
      status=1
    fi
  done
done

[ $status -eq 0 ] && echo "== all suites passed"
exit $status
