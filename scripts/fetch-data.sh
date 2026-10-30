#!/usr/bin/env bash
# Downloads the datasets listed in scripts/datasets.tsv, and never gives up:
# a lost connection, a dropped Wi-Fi or an unplugged disk pauses it, and it
# carries on from the same byte when they come back.
#
#   scripts/fetch-data.sh start     run in the background; survives closing the terminal
#   scripts/fetch-data.sh status    what is done, what is left, the last log lines
#   scripts/fetch-data.sh stop      pause; start again resumes
#   scripts/fetch-data.sh run       run in this terminal
#
# While it runs the laptop is kept from going to sleep when idle. Closing the
# lid may still suspend it; the download resumes on wake.
set -uo pipefail
cd "$(dirname "$0")/.."

LIST=scripts/datasets.tsv
LOG=data/fetch.log
UNIT=aqos-fetch
RETRY_S=20

log() { printf '%s  %s\n' "$(date '+%m-%d %H:%M:%S')" "$*" | tee -a "$LOG"; }

entries() { grep -vE '^\s*(#|$)' "$LIST"; }

# Prints "<http status> <size>". Status 000 means the server cannot be
# reached; size is empty when the server does not say.
probe() {
    curl -sIL --connect-timeout 15 --max-time 30 "${auth[@]}" -w 'status %{http_code}\n' "$1" \
        | tr -d '\r' | awk 'tolower($1) == "content-length:" { size = $2 }
                            $1 == "status" { code = $2 } END { print code, size }'
}

local_size() { stat -c %s "$1" 2>/dev/null || echo 0; }

human() { numfmt --to=iec --suffix=B "$1" 2>/dev/null || echo "$1"; }

fetch_one() {
    local url=$1 dest=$2 cookie=${3:-} want have code
    if [[ -e $dest.done ]]; then
        log "done: $dest ($(human "$(local_size "$dest")"))"
        return
    fi
    while true; do
        # Some sites only serve a browser that has registered. The third
        # column names a file holding that browser's cookie.
        auth=()
        if [[ -n $cookie ]]; then
            if [[ ! -s $cookie ]]; then
                log "waiting: $cookie is missing (see scripts/datasets.tsv)"
                sleep "$RETRY_S"
                continue
            fi
            auth=(-H "Cookie: $(tr -d '\n' < "$cookie")")
        fi
        # A destination on an unmounted disk must wait, not land elsewhere.
        if ! mkdir -p "$(dirname "$dest")" 2>/dev/null || [[ ! -w $(dirname "$dest") ]]; then
            log "waiting: $(dirname "$dest") is not available (disk not mounted?)"
            sleep "$RETRY_S"
            continue
        fi
        read -r code want < <(probe "$url")
        if [[ $code == 401 || $code == 403 ]]; then
            log "waiting: the site refused (HTTP $code). The registration has expired: register again in the browser and put the new cookie in $cookie"
            sleep 60
            continue
        fi
        if [[ $code != 200 ]]; then
            log "waiting: no connection to $(echo "$url" | cut -d/ -f3) (HTTP $code)"
            sleep "$RETRY_S"
            continue
        fi
        have=$(local_size "$dest")
        if [[ -n $want ]] && (( have == want )); then
            touch "$dest.done"
            log "done: $dest ($(human "$want"))"
            return
        fi
        if [[ -n $want ]] && (( have > want )); then
            log "restart: $dest is larger than the server's copy"
            rm -f "$dest"
        fi
        log "fetching: $dest, $(human "$have") of $(human "${want:-0}")"
        # Give up on a transfer that crawls below 1 kB/s for a minute, so a
        # dead connection is retried instead of hanging.
        curl -sL --fail -C - --connect-timeout 20 --speed-limit 1000 --speed-time 60 \
            "${auth[@]}" -o "$dest" "$url"
        status=$?
        # Without a size from the server, a clean exit is the only sign that
        # the file is whole.
        if [[ -z $want && $status -eq 0 ]]; then
            touch "$dest.done"
            log "done: $dest ($(human "$(local_size "$dest")"))"
            return
        fi
        log "paused (curl exit $status): $dest at $(human "$(local_size "$dest")")"
        sleep "$RETRY_S"
    done
}

# A registration lasts only while the site keeps hearing from us, and the
# files that need it may wait hours for their turn. Ask for one byte of
# each every few minutes so the registration is still good when they start.
keep_registered() {
    while sleep 240; do
        while IFS=$'\t' read -r url dest cookie; do
            if [[ -n $cookie && -s $cookie && ! -e $dest.done ]]; then
                curl -s -o /dev/null --max-time 30 -r 0-0 -H "Cookie: $(tr -d '\n' < "$cookie")" "$url"
            fi
        done < <(entries)
    done
}

run() {
    mkdir -p data
    keep_registered &
    trap "kill $! 2>/dev/null" EXIT
    log "start: $(entries | wc -l) files"
    while IFS=$'\t' read -r url dest cookie; do
        fetch_one "$url" "$dest" "$cookie"
    done < <(entries)
    log "all done"
}

status() {
    systemctl --user is-active --quiet "$UNIT" && echo "running" || echo "not running"
    while IFS=$'\t' read -r url dest cookie; do
        printf '  %-55s %s\n' "$dest" "$(human "$(local_size "$dest")")"
    done < <(entries)
    echo "last log lines:"
    tail -n 5 "$LOG" 2>/dev/null | sed 's/^/  /'
}

case "${1:-}" in
    run) run ;;
    start)
        if systemctl --user is-active --quiet "$UNIT"; then
            echo "already running"
            exit 0
        fi
        systemctl --user reset-failed "$UNIT" 2>/dev/null
        systemd-run --user --unit "$UNIT" --collect --working-directory "$PWD" \
            systemd-inhibit --what=idle:sleep --who=aqos-fetch --why="downloading datasets" \
            "$PWD/scripts/fetch-data.sh" run
        echo "started; follow with: scripts/fetch-data.sh status"
        ;;
    stop) systemctl --user stop "$UNIT" && echo "stopped; start resumes where it left off" ;;
    status) status ;;
    *) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
