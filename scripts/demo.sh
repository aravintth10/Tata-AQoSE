#!/usr/bin/env bash
# The scripted demo, about 8 minutes, no sudo:
#   scripts/demo.sh [scenario] [seed]        default S6, seed 1
# 1. Reset: tear down any old testbed, build the binaries.
# 2. Introduce the condition: run the scenario (a call, then file sharing
#    on the same laptop at 30 s) with plain CAKE, then with AQoSE, on the
#    same seed, so both see identical traffic.
# 3. Collect evidence: each run directory holds per-packet metrics, CAKE's
#    tin statistics, every AQoSE decision with its reason, the verifier's
#    verdicts and CPU per component.
# 4. Report: a side-by-side summary and a second-by-second chart.
set -euo pipefail
cd "$(dirname "$0")/.."
scenario=${1:-S6}
seed=${2:-1}
out=results-demo
export PYTHONPATH=ml

echo "== 1. reset"
if pgrep -f "[a]qos_ml.run (one|matrix)" >/dev/null; then
    echo "Another run is using the testbed; try again when it has finished." >&2
    exit 1
fi
bin/aqos-testbed down >/dev/null 2>&1 || true
make -s build
for config in B2 O2; do rm -rf -- "$(printf '%s/%s-%s-s%04d' "${out:?}" "$scenario" "$config" "$seed")"; done

echo "== 2. introduce the condition: $scenario, seed $seed, plain CAKE then AQoSE"
for config in B2 O2; do
    .venv/bin/python -m aqos_ml.run one --scenario "$scenario" --config "$config" --seed "$seed" --results "$out"
done

echo "== 3. evidence"
for config in B2 O2; do
    run=$(printf '%s/%s-%s-s%04d' "$out" "$scenario" "$config" "$seed")
    ls "$run"
done
run=$(printf '%s/%s-O2-s%04d' "$out" "$scenario" "$seed")
echo "-- AQoSE's first decisions, with their reasons:"
head -3 "$run/actions.jsonl" | .venv/bin/python -c "
import json, sys
for line in sys.stdin:
    a = json.loads(line)
    print('  %6.2f s  %-8s %s' % (a['t_s'], a['outcome'], a['proposal']['reason']))"

echo "== 4. report"
.venv/bin/python -m aqos_ml.run table --results "$out" --out reports/demo.md
.venv/bin/python - "$out" "$scenario" <<'EOF'
import sys
from pathlib import Path
from aqos_ml import charts
results, scenario = Path(sys.argv[1]), sys.argv[2]
print(charts.timeline(results, scenario, "call", Path("reports/demo-charts")))
EOF
echo "reports/demo.md"
