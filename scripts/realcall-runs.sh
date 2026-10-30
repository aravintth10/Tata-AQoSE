#!/usr/bin/env bash
# After the final runs: bring the real-call scenarios into the main tree,
# check, run them, and report. Started detached; stops at the first
# failure and says why in REALCALL-STATUS.
#   scripts/realcall-runs.sh <main tree> <worktree with the replay branch>
set -uo pipefail
main=$1 next=$2
status() { echo "$(date '+%a %H:%M:%S') $*" | tee "$main/REALCALL-STATUS"; }
stop() { status "STOPPED: $*"; exit 1; }

status "waiting for the final runs"
until grep -q "^== done" "$main/results-final/final-runs.log" 2>/dev/null \
      && ! pgrep -f "[a]qos_ml.run matrix" >/dev/null && ! pgrep -f "[f]inal-runs.sh" >/dev/null; do
    sleep 60
done

cd "$main" || stop "no main tree"
status "merging the replay branch"
git merge --no-edit replay || { git merge --abort; stop "the replay branch does not merge cleanly"; }
export PYTHONPATH=ml
status "testing"
.venv/bin/python -m pytest -q -p no:cacheprovider ml/tests/test_plan.py ml/tests/test_run.py >/dev/null || stop "tests failed"

status "real calls, frozen system"
.venv/bin/python -u -m aqos_ml.run matrix configs/matrix-realcall.yaml --results results-realcall || stop "matrix-realcall"
status "real calls, model that never saw WhatsApp"
AQOS_SERVICE=python AQOS_MODEL=lgbm-v1-fe91bbad .venv/bin/python -u -m aqos_ml.run matrix configs/matrix-realcall-v1.yaml \
    --results results-realcall-v1 || stop "matrix-realcall-v1"
status "reports"
.venv/bin/python -m aqos_ml.report --results results-realcall --out reports/realcall.md || status "report failed; the runs are in results-realcall"
.venv/bin/python -m aqos_ml.report --results results-realcall-v1 --out reports/realcall-v1.md || status "report v1 failed; the runs are in results-realcall-v1"
status "done"
