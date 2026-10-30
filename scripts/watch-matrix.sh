#!/usr/bin/env bash
# Live progress of a matrix run, from its results directory:
#   scripts/watch-matrix.sh                    the full matrix (results-core, 450 runs)
#   scripts/watch-matrix.sh results-aa 90      another directory and its number of runs
# A bar with the share done, the pace and the expected finish, the last
# few runs with their verdict, and the log of the run in progress.
# Ctrl-C stops the watching, not the matrix.
dir=${1:-results-core}
total=${2:-450}
cd "$(dirname "$0")/.." || exit 1
width=50
trap 'tput cnorm 2>/dev/null; echo; exit 0' INT TERM
tput civis 2>/dev/null

while :; do
    mapfile -t manifests < <(ls -tr "$dir"/*/manifest.json 2>/dev/null)
    done_runs=${#manifests[@]}
    invalid=$(grep -l '"valid": false' "${manifests[@]}" 2>/dev/null | wc -l)
    percent=$((total > 0 ? 100 * done_runs / total : 0))
    filled=$((total > 0 ? width * done_runs / total : 0))
    bar=$(printf '%*s' "$filled" '' | tr ' ' '#')$(printf '%*s' $((width - filled)) '' | tr ' ' '.')

    pace="-" left="-" finish="-"
    if ((done_runs >= 2)); then
        first=$(stat -c %Y "${manifests[0]}")
        last=$(stat -c %Y "${manifests[-1]}")
        per=$(((last - first) / (done_runs - 1)))
        if ((per > 0)); then
            remaining=$(((total - done_runs) * per))
            pace="${per} s per run"
            left="$((remaining / 3600)) h $((remaining % 3600 / 60)) min"
            finish=$(date -d "@$((last + remaining))" "+%a %H:%M")
        fi
    fi
    current=$(readlink "$dir/current" 2>/dev/null)
    running=no
    pgrep -f "aqos_ml.run matrix" >/dev/null && running=yes

    clear
    echo "$dir                                             $(date '+%a %H:%M:%S')"
    echo
    echo "  [$bar] $percent%"
    echo "  $done_runs of $total runs done, $invalid invalid"
    echo "  pace $pace    left $left    finishes $finish"
    if ((done_runs >= total)); then
        echo "  FINISHED"
    elif [[ $running == no ]]; then
        echo "  NOT RUNNING: no matrix process found"
    fi
    echo
    echo "  last runs:"
    for manifest in "${manifests[@]: -8}"; do
        run=$(basename "$(dirname "$manifest")")
        if grep -q '"valid": true' "$manifest"; then
            echo "    valid    $run"
        else
            echo "    INVALID  $run: $(grep invalid_reason "$manifest" | cut -c22-130)"
        fi
    done
    if [[ -n $current && ! -f $dir/$current/manifest.json ]]; then
        started=$(stat -c %Y "$dir/$current" 2>/dev/null || echo 0)
        echo
        echo "  running: $current, $(($(date +%s) - started)) s in"
        tail -n 3 "$dir/$current/runner.log" 2>/dev/null | cut -c1-110 | sed 's/^/    /'
    fi
    sleep 5
done
