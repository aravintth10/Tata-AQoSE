#!/usr/bin/env bash
# Every run after the freeze, in order of importance, then the reports.
# Safe to stop and start again: finished runs are skipped.
#   scripts/final-runs.sh > results-final/final-runs.log 2>&1
set -uo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=ml
run() { .venv/bin/python -u -m aqos_ml.run matrix "$@"; }

mkdir -p results-final results-fresh
# B0, B2 and B4 are unchanged by the freeze: their matrix runs pair with
# the frozen classifier on the same seeds.
for d in results-core/*-B[024]-s00*; do
    [[ -d $d && ! -e results-final/$(basename "$d") ]] && cp -a --reflink=auto "$d" results-final/
done

echo "== replay $(date '+%a %H:%M')"; run configs/matrix-final-replay.yaml --results results-final
echo "== b3 $(date '+%a %H:%M')"; run configs/matrix-final-b3.yaml --results results-final
echo "== fresh $(date '+%a %H:%M')"; run configs/matrix-final-fresh.yaml --results results-fresh
for noise in 0.5 1.0 1.5; do
    echo "== ablation $noise $(date '+%a %H:%M')"
    AQOS_SERVICE=python AQOS_SERVE_ARGS="--feature-noise $noise" run configs/matrix-ablation.yaml --results "results-ablation-$noise"
done
echo "== reports $(date '+%a %H:%M')"
.venv/bin/python -m aqos_ml.report --results results-final --aa results-aa --out reports/final.md
.venv/bin/python -m aqos_ml.report --results results-fresh --out reports/fresh.md
.venv/bin/python -m aqos_ml.charts --results results-final --aa results-aa --out reports/charts
.venv/bin/python -m aqos_ml.ablation > /dev/null
echo "== done $(date '+%a %H:%M')"
