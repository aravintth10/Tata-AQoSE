# Explainer video

The published video: <https://youtu.be/k6ccbBaxwUg>. A 2–3 minute overview, then a scene-by-scene walkthrough of how AQoSE works, narrated with the Kokoro-82M voice model.

The MP4s are not committed (each is 80–105 MB); this folder holds everything that builds them.

| File | What it is |
| --- | --- |
| `aqose.html` | Part 1, the overview: an SVG animation driven by `window.render(t)` |
| `deep.html` | Part 2, the walkthrough: 13 scenes whose animations start on narration cues |
| `tts/script.py` | The narration, line by line, and a pronunciation map |
| `tts/synth.py` | Speaks every line with Kokoro and writes the timing files the renderer and mixer read |
| `render.js` | Drives headless Chrome frame by frame and pipes JPEG frames to ffmpeg |
| `mix.py` | Places each spoken line at its start time and muxes the audio onto the video |
| `voice.sh` | One voice end to end: synthesise, render both parts, mix, join |
| `thumb.html`, `thumb.js` | The YouTube thumbnail |

## Building

Needs Google Chrome, ffmpeg (with libopenh264 or libx264), Node 18+ and Python 3 with `kokoro-onnx`, `soundfile` and `numpy`.

```
npm install                               # puppeteer-core
cd tts
python3 -m venv venv && venv/bin/pip install kokoro-onnx soundfile numpy
# download kokoro-v1.0.onnx and voices-v1.0.bin from the kokoro-onnx releases into tts/
cd ..
./voice.sh bf_emma                        # about 12 minutes of video; bf_emma, am_puck, bm_george, af_nicole were rendered
```

`synth.py` points espeak-ng at the system install (`/usr/lib64/libespeak-ng.so.1`); change the path on other distributions. Frames are encoded with an explicit full-to-limited colour-range conversion, so light greys do not clip to white in players.
