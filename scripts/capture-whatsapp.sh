#!/usr/bin/env bash
# Records WhatsApp voice and video calls for the dataset, the same way the
# cloud-gaming sessions were recorded. Run it as yourself:
#
#   scripts/capture-whatsapp.sh              three voice calls, then three video calls
#   scripts/capture-whatsapp.sh voice        voice calls only
#   scripts/capture-whatsapp.sh video 2      two video calls
#
# Three sessions of each kind, because a session is never split: one goes
# to training, one to validation and one to the final test.
#
# You need a second person, or a second phone, to answer.
#
# Where the call runs decides what is recorded:
#   - On this laptop (WhatsApp Web or the desktop app): nothing to set.
#   - On a phone: put the phone on this laptop's Wi-Fi hotspot and name the
#     hotspot's interface, or the call never crosses the recorded
#     interface:  AQOS_IFACE=wlp0s20f0u1 scripts/capture-whatsapp.sh
#     (`ip -br link` lists the interfaces; the hotspot's is the one that
#     appears when the hotspot is switched on.)
#
# For each call:
#   1. Open the chat with the other person. Do NOT call yet.
#   2. Press Enter. Recording starts.
#   3. Wait 3 seconds, then place the call. The other side answers.
#   4. Talk, and for a video call move in front of the camera, until the
#      recording stops by itself (90 s). Then hang up.
#   5. Pick the call's flows from the list: WhatsApp is the one or two
#      largest UDP flows, usually to port 3478 or a high port. The small
#      TCP flows to port 443 or 5222 are chat and signalling; leave them.
#
# Close everything else that uses the network first, on the phone too
# (backups, updates, other chats' media downloads).
#
# It does not touch the testbed, so it is safe while experiments run.
set -euo pipefail
cd "$(dirname "$0")/.."

kind=${1:-both}
count=${2:-3}
case "$kind" in
    -h|--help) sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    voice|video|both) ;;
    *) echo "voice, video or nothing for both; not \"$kind\"" >&2; exit 1 ;;
esac
if ! [[ $count =~ ^[1-9][0-9]?$ ]]; then
    echo "How many calls of each kind: a number, not \"$count\"" >&2
    exit 1
fi
read -rp "Where does the call run: laptop or phone? [laptop] " where
where=${where:-laptop}
if [[ $where == phone && -z ${AQOS_IFACE:-} ]]; then
    echo "For a call on a phone, set AQOS_IFACE to the hotspot's interface and run again:" >&2
    ip -br link | awk '$1 != "lo" {print "  " $1, $2}' >&2
    exit 1
fi
read -rp "Who or what is on the other end (for example \"phone on mobile data, same city\"): " far

round() {
    local activity=$1 what=$2 camera=$3
    for i in $(seq "$count"); do
        echo
        echo "======== WhatsApp $what call $i of $count"
        AQOS_APP="WhatsApp" AQOS_SECONDS=90 \
            AQOS_NOTES="1-to-1 $what call$camera, run on the $where, far end: ${far:-not said}, call $i of $count" \
            scripts/wave2-sudo.sh "$activity"
        if [[ $i -lt $count ]]; then
            read -rp "Hang up. Press Enter when both sides are back on the chat screen. "
        fi
    done
}

[[ $kind == video ]] || round voice_call voice ""
[[ $kind == voice ]] || round video_call video ", camera on both sides"

echo
echo "Recorded sessions are in data/raw/. To put them in the dataset:"
echo "  PYTHONPATH=ml .venv/bin/python -m aqos_ml.dataset build --name v2"
