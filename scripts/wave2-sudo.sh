#!/usr/bin/env bash
# Everything in wave 2 that needs sudo, in one go. Run it as yourself:
#
#   scripts/wave2-sudo.sh                 the two captures S6 and S7 need
#   scripts/wave2-sudo.sh all             every activity
#   scripts/wave2-sudo.sh gaming p2p      just these
#
# It asks for the sudo password once, then:
#   1. installs the optional tools (conntrack-tools, iperf3, irtt)
#   2. records one capture per activity, asking you to start each one
#
# It does not touch the testbed, so it is safe while experiments run.
#
# AQOS_APP, AQOS_NOTES and AQOS_SECONDS answer the three questions asked
# per activity in advance (scripts/capture-whatsapp.sh uses them).
#
# The captures need you. For each activity: get the app ready but do NOT
# start the activity, press Enter, and start it only once recording has
# begun. The classifier works from a flow's first 8 packets, so a call or
# download that was already running when recording began is useless.
set -euo pipefail
cd "$(dirname "$0")/.."

ALL=(voice_call video_call gaming cloud_gaming abr_video web_browsing bulk_download game_update backup_upload p2p)
case "${1:-}" in
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    "")  activities=(video_call cloud_gaming) ;;
    all) activities=("${ALL[@]}") ;;
    *)   activities=("$@") ;;
esac
for a in "${activities[@]}"; do
    if [[ ! " ${ALL[*]} " == *" $a "* ]]; then
        echo "Unknown activity: $a. One of: ${ALL[*]}" >&2
        exit 1
    fi
done
if [[ $EUID -eq 0 ]]; then
    echo "Run this without sudo; it asks for the password itself, so the files stay yours." >&2
    exit 1
fi

capture() { PYTHONPATH=ml .venv/bin/python -m aqos_ml.capture "$@"; }

echo "== sudo"
sudo -v
# Keeps the password cached while you set up each activity.
( while sleep 50; do sudo -n true 2>/dev/null || exit; done ) &
keepalive=$!
gro_iface=
cleanup() {
    kill $keepalive 2>/dev/null || true
    [[ -n $gro_iface ]] && sudo -n ethtool -K "$gro_iface" gro on 2>/dev/null || true
}
trap cleanup EXIT

echo
echo "== 1. Optional tools"
missing=()
command -v conntrack >/dev/null || missing+=(conntrack-tools)
command -v iperf3 >/dev/null || missing+=(iperf3)
command -v irtt >/dev/null || missing+=(irtt)
if [[ ${#missing[@]} -eq 0 ]]; then
    echo "already installed"
elif ! sudo dnf install -y "${missing[@]}"; then
    echo "WARN: could not install ${missing[*]}; nothing in wave 2 depends on them"
fi

echo
echo "== 2. Captures"
iface=${AQOS_IFACE:-$(ip route show default | awk '{print $5; exit}')}
if [[ -z $iface ]]; then
    echo "No default route found. Set AQOS_IFACE=<interface> and run again." >&2
    exit 1
fi
network=${AQOS_NETWORK:-$(nmcli -t -f NAME connection show --active 2>/dev/null | head -n 1)}
network=${network:-unknown}
device=${AQOS_DEVICE:-$(hostname -s)}
echo "Interface $iface, network \"$network\", device $device"
echo "Close everything else that uses the network before each capture."
mkdir -p data/raw

# Receive offload merges packets before tcpdump sees them, so a capture
# would hold packets of 9,000 bytes that never crossed the link.
if ethtool -k "$iface" 2>/dev/null | grep -q '^generic-receive-offload: on'; then
    sudo ethtool -K "$iface" gro off
    gro_iface=$iface
    echo "Receive offload on $iface is off for the captures and comes back on at exit."
fi

kept=0
for activity in "${activities[@]}"; do
    echo
    echo "---- $activity"
    app=${AQOS_APP:-}
    [[ -n $app ]] || read -rp "App name (for example \"Google Meet\"), or s to skip: " app
    if [[ -z $app || $app == s ]]; then
        echo "skipped"
        continue
    fi
    notes=${AQOS_NOTES:-}
    [[ -n ${AQOS_NOTES+set} ]] || read -rp "What exactly, optional (for example \"Asphalt 9, racing\" or \"1-to-1 call, camera on\"): " notes
    seconds=${AQOS_SECONDS:-}
    [[ -n $seconds ]] || read -rp "Seconds to record [120]: " seconds
    seconds=${seconds:-120}
    echo "Get $app ready, but do NOT start the $activity yet (not in the call, download not started,"
    echo "game fully closed back to the list). A stream still running from the last round is useless."
    read -rp "Press Enter to start recording. Then wait 3 seconds and start it. "

    capture record --activity "$activity" --app "$app" --device "$device" \
        --network "$network" --iface "$iface" --seconds "$seconds" --notes "$notes"
    session=$(ls -t data/raw/*.json | head -n 1)
    sudo chown "$USER" "${session%.json}.pcap"

    echo
    capture review "$session" --top 12
    echo
    echo "Keep only the flows that are $app itself: usually the one or two largest."
    echo "A flow marked UNUSABLE cannot be kept."
    # Nothing is ever deleted here: a capture that is not accepted stays in
    # data/raw, unused, so it can be looked at again.
    while true; do
        read -rp "Flow numbers to keep (for example 0 or 0,1), or n to leave it unaccepted: " answer
        if [[ $answer == n ]]; then
            echo "left unaccepted: $session"
            break
        fi
        if [[ -z $answer ]]; then
            echo "Type the numbers from the first column, or n."
            continue
        fi
        if capture accept "$session" --keep "$answer"; then
            kept=$((kept + 1))
            break
        fi
        echo "Not accepted. Try other numbers, or n. If the main flow is UNUSABLE, record this one again."
    done
done

echo
echo "Done: $kept session(s) accepted in data/raw/."
