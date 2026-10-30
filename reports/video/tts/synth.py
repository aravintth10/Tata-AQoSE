"""Synthesises both parts with Kokoro and writes the timings the renderer and
the mixer read.

    venv/bin/python synth.py [voice] [speed]

part1.json: overview; real time -> animation time map, line start times.
part2.json: walkthrough; per scene its start, duration and cue times.
"""
import json, sys
from pathlib import Path
import numpy as np, soundfile as sf
from kokoro_onnx import Kokoro, EspeakConfig
import script

HERE = Path(__file__).parent
VOICE = sys.argv[1] if len(sys.argv) > 1 else "af_heart"
SPEED = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
LANG = "en-gb" if VOICE[:1] == "b" else "en-us"
K = Kokoro(str(HERE / "kokoro-v1.0.onnx"), str(HERE / "voices-v1.0.bin"),
           espeak_config=EspeakConfig(lib_path="/usr/lib64/libespeak-ng.so.1", data_path="/usr/share/espeak-ng-data"))
OUT = HERE / f"lines-{VOICE}"
OUT.mkdir(exist_ok=True)


def say(name, text):
    """Synthesise once per (voice, text); return (file, seconds)."""
    for word in sorted(script.PRONOUNCE, key=len, reverse=True):
        text = text.replace(word, script.PRONOUNCE[word])
    path = OUT / f"{name}.wav"
    key = OUT / f"{name}.txt"
    if not (path.exists() and key.exists() and key.read_text() == f"{SPEED}|{text}"):
        audio, sr = K.create(text, voice=VOICE, speed=SPEED, lang=LANG)
        sf.write(path, audio, sr)
        key.write_text(f"{SPEED}|{text}")
    info = sf.info(path)
    print(f"{name:14s} {info.duration:5.1f}s  {text[:70]}", flush=True)
    return str(path), info.duration


# ---- part 1: stretch each scene of the existing animation to fit its speech
GAP, TAIL = 0.35, 0.8
lines = []
for i, (anchor, text) in enumerate(script.PART1):
    f, d = say(f"p1-{i:02d}", text)
    lines.append({"anchor": anchor, "file": f, "dur": round(d, 3)})
segments, real = [], 0.0
for a, b in script.PART1_SCENES:
    mine = [l for l in lines if a <= l["anchor"] < b]
    k = 1.0
    while True:
        prev = real
        for l in mine:
            l["start"] = round(max(real + (l["anchor"] - a) * k, prev + GAP), 3)
            prev = l["start"] + l["dur"]
        if not mine or prev + TAIL <= real + (b - a) * k:
            break
        k += 0.01
    segments.append({"a": a, "b": b, "A": round(real, 3), "B": round(real + (b - a) * k, 3), "k": round(k, 3)})
    real += (b - a) * k
json.dump({"voice": VOICE, "duration": round(real, 3), "segments": segments, "lines": lines}, open(HERE / f"part1-{VOICE}.json", "w"), indent=1)
print(f"part 1: {real:.1f}s; stretch " + " ".join(f"{s['k']:.2f}" for s in segments))

# ---- part 2: scenes laid end to end; every line is a cue
LEAD, GAP2, TAIL2 = 0.9, 0.55, 1.4
scenes, plines, t = [], [], 0.0
for sid, texts in script.PART2:
    local, cues = LEAD, []
    for j, text in enumerate(texts):
        f, d = say(f"p2-{sid}-{j}", text)
        cues.append(round(local, 3))
        plines.append({"file": f, "start": round(t + local, 3), "dur": round(d, 3)})
        local += d + GAP2
    dur = max(local - GAP2 + TAIL2, 5.0)
    scenes.append({"id": sid, "start": round(t, 3), "dur": round(dur, 3), "cues": cues})
    t += dur
json.dump({"voice": VOICE, "duration": round(t, 3), "scenes": scenes, "lines": plines}, open(HERE / f"part2-{VOICE}.json", "w"), indent=1)
print(f"part 2: {t:.1f}s over {len(scenes)} scenes")
