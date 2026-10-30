#!/usr/bin/env bash
# The freeze, unattended. Waits until the pre-freeze matrix and its A/A
# runs have finished, merges the freeze branch into main, builds and
# tests, runs a short smoke check of the frozen system, and starts the
# final runs. Stops at the first failure and says why in its log; nothing
# is run on a system that failed a check.
#   scripts/freeze.sh <main tree> <freeze worktree> > freeze.log 2>&1
set -euo pipefail
main=$(cd "$1" && pwd)
next=$(cd "$2" && pwd)
say() { echo "$(date '+%a %H:%M:%S') $*"; }
fail() { say "STOPPED: $*"; echo "STOPPED: $*" > "$main/FREEZE-STATUS"; exit 1; }

say "waiting for the matrix and the A/A runs"
# The pre-freeze chain ends by writing reports/core.md; between its matrix
# and its A/A runs there is a moment with no matrix process at all.
while [[ ! -f $main/reports/core.md ]] || pgrep -f "aqos_ml.run matri[x]" >/dev/null \
        || pgrep -f "aqos_ml.repor[t]" >/dev/null; do sleep 60; done
aa=$(ls "$main"/results-aa/*/manifest.json 2>/dev/null | wc -l)
core=$(ls "$main"/results-core/*/manifest.json 2>/dev/null | wc -l)
say "matrix: $core runs, A/A: $aa runs"
[[ $core -ge 450 ]] || fail "the matrix has $core of 450 runs"

say "merging the freeze branch"
cd "$main"
[[ -z $(git status --porcelain --untracked-files=no) ]] || fail "the main tree has uncommitted changes"
git merge --ff-only freeze || fail "the freeze branch does not fast-forward"
cp -a --reflink=auto "$next/third_party" "$main/" 2>/dev/null || true
for f in "$next"/models/rf-*/model.joblib; do
    dest="$main/models/$(basename "$(dirname "$f")")"
    mkdir -p "$dest" && cp -n "$f" "$dest/"
done

say "building and testing"
make build >/dev/null || fail "make build"
make check > freeze-check.log 2>&1 || fail "make check: see freeze-check.log"

say "smoke check of the frozen system"
export PYTHONPATH=ml
for spec in "S6 O2" "S7 O1" "S2 B3"; do
    set -- $spec
    .venv/bin/python -m aqos_ml.run one --scenario "$1" --config "$2" --seed 1 --results results-freeze-smoke \
        > /dev/null 2>&1 || fail "smoke run $1 $2 was not valid: see results-freeze-smoke"
done
grep -q '"cpu_s"' results-freeze-smoke/S6-O2-s0001/controller.json || fail "the controller did not record its CPU"
grep -q "aqos-classify" results-freeze-smoke/S6-O2-s0001/logs/ml-service.err \
    || say "note: S6 O2 did not run the Rust service (see its ml-service.err)"
grep -q "SHAPER" results-freeze-smoke/S2-B3-s0001/cake-autorate.b3.log \
    || fail "cake-autorate changed no rate in S2"

echo "FROZEN at $(git rev-parse --short HEAD); final runs started $(date '+%a %H:%M')" > FREEZE-STATUS
say "frozen at $(git rev-parse --short HEAD); starting the final runs"
mkdir -p results-final
exec scripts/final-runs.sh > results-final/final-runs.log 2>&1
