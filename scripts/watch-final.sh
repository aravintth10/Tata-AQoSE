#!/usr/bin/env bash
# Live view of everything after the freeze: the final runs, the ablation
# and the real-call runs, as one progress bar with an expected finish,
# each stage's state, and the latest log lines.
#   scripts/watch-final.sh
# Ctrl-C stops the watching, not the runs.
cd "$(dirname "$0")/.." || exit 1
log=results-final/final-runs.log
rlog=realcall.log
width=50
trap 'tput cnorm 2>/dev/null; echo; exit 0' INT TERM
tput civis 2>/dev/null

# Stage, its header in the log, its number of runs.
stages=("replay|== replay|180" "b3|== b3|20" "fresh|== fresh|200" "ablation 0.5|== ablation 0.5|12"
        "ablation 1.0|== ablation 1.0|12" "ablation 1.5|== ablation 1.5|12" "real calls|real calls, frozen|40"
        "real calls v1|model that never saw|10")
total=0
for s in "${stages[@]}"; do total=$((total + ${s##*|})); done
start=$(stat -c %W "$log" 2>/dev/null); [[ $start =~ ^[0-9]+$ && $start -gt 0 ]] || start=$(stat -c %Y FREEZE-STATUS)

progress() {  # runs finished in one stage: 0, its total, or from the [k/N] lines after its header
    local name=$1 header=$2 n=$3
    if [[ $name == real* ]]; then
        # REALCALL-STATUS names the step in progress; the log has its [k/N] lines.
        local status; status=$(cat REALCALL-STATUS 2>/dev/null)
        local order=("real calls, frozen" "model that never saw" "reports" "done") mine=0 now=-1 i
        for i in "${!order[@]}"; do
            [[ ${order[$i]} == "$header" ]] && mine=$i
            [[ $status == *"${order[$i]}"* ]] && now=$i
        done
        if ((now > mine)); then echo "$n"
        elif ((now == mine)); then
            local k; k=$(grep -o '^\[[0-9]*/'"$n"'\]' "$rlog" 2>/dev/null | tail -1 | tr -d '[]' | cut -d/ -f1)
            echo $(( ${k:-1} - 1 ))
        else echo 0; fi
        return
    fi
    local after; after=$(awk -v h="$header" 'index($0,h)==1{f=1;next} f' "$log" 2>/dev/null)
    grep -q "^$header" "$log" 2>/dev/null || { echo 0; return; }
    if grep -q '^== ' <<<"$after"; then echo "$n"; return; fi
    local k; k=$(grep -o '^\[[0-9]*/[0-9]*\]' <<<"$after" | tail -1 | tr -d '[]' | cut -d/ -f1)
    echo $(( ${k:-1} - 1 ))
}

while :; do
    done_runs=0; lines=()
    for s in "${stages[@]}"; do
        IFS='|' read -r name header n <<<"$s"
        p=$(progress "$name" "$header" "$n"); done_runs=$((done_runs + p))
        if ((p >= n)); then state="done"; elif ((p > 0)) || { [[ $name != real* ]] && grep -q "^$header" "$log" 2>/dev/null; } \
              || { [[ $name == real* ]] && grep -q "$header" REALCALL-STATUS 2>/dev/null; }; then state="running"; else state="waiting"; fi
        lines+=("$(printf '  %-14s %4d / %-4d %s' "$name" "$p" "$n" "$state")")
    done
    now=$(date +%s); elapsed=$((now - start))
    percent=$((100 * done_runs / total)); filled=$((width * done_runs / total))
    bar=$(printf '%*s' "$filled" '' | tr ' ' '#')$(printf '%*s' $((width - filled)) '' | tr ' ' '.')
    pace="-" finish="-"
    if ((done_runs > 0)); then
        per=$((elapsed / done_runs)); pace="$((per / 60)) min $((per % 60)) s per run"
        finish=$(date -d "@$((now + (total - done_runs) * per))" '+%a %H:%M')
    fi
    printf '\033[H\033[2J'
    echo "AQoSE after the freeze                                   $(date '+%a %H:%M:%S')"
    echo
    printf '  [%s] %3d%%   %d of %d runs\n' "$bar" "$percent" "$done_runs" "$total"
    echo "  pace: $pace     expected finish: $finish (then reports, ~10 min)"
    echo
    printf '%s\n' "${lines[@]}"
    echo
    echo "  freeze:     $(cat FREEZE-STATUS 2>/dev/null)"
    echo "  real calls: $(cat REALCALL-STATUS 2>/dev/null)"
    invalid=$(grep -l '"valid": false' results-final/*/manifest.json results-fresh/*/manifest.json \
              results-ablation-*/*/manifest.json results-realcall*/*/manifest.json 2>/dev/null | wc -l)
    echo "  invalid runs so far: $invalid"
    echo
    echo "  latest:"
    { tail -4 "$log"; [[ -s $rlog ]] && tail -3 "$rlog"; } 2>/dev/null | sed 's/^/    /'
    sleep 5
done
