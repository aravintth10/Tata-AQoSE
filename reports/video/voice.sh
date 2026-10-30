#!/usr/bin/env bash
# One voice end to end:  ./voice.sh af_nicole
set -euo pipefail
V=$1; cd "$(dirname "$0")"; T=tts; export BITRATE=4M
echo "== $V synth $(date +%T)"
(cd $T && venv/bin/python synth.py "$V" 1.0 | grep -E "^part")
echo "== $V part 1 $(date +%T)"
TIMING=$T/part1-$V.json node render.js video p1-$V-silent.mp4 30 | grep -v "^fonts" || true
python3 mix.py $T/part1-$V.json p1-$V-silent.mp4 p1-$V.mp4 && rm -f p1-$V-silent.mp4
echo "== $V part 2 $(date +%T)"
PAGE=deep.html DEEP=$T/part2-$V.json node render.js video p2-$V-silent.mp4 30 | grep -v "^fonts" || true
python3 mix.py $T/part2-$V.json p2-$V-silent.mp4 p2-$V.mp4 && rm -f p2-$V-silent.mp4
printf "file 'p1-%s.mp4'\nfile 'p2-%s.mp4'\n" "$V" "$V" > join-$V.txt
ffmpeg -y -v error -f concat -safe 0 -i join-$V.txt -c copy -movflags +faststart aqose-explainer-$V.mp4 && rm -f p1-$V.mp4 p2-$V.mp4
ffprobe -v error -show_entries format=duration,size -of compact aqose-explainer-$V.mp4
echo "== $V done $(date +%T)"
