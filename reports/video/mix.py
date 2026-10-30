"""Mixes the narration lines at their start times and muxes them onto the video.
    python3 mix.py timing.json video.mp4 out.mp4"""
import json, subprocess, sys
tj = json.load(open(sys.argv[1])); video, out = sys.argv[2], sys.argv[3]
args = ["ffmpeg", "-y", "-v", "error", "-i", video]
for l in tj["lines"]: args += ["-i", l["file"]]
parts = [f"[{i + 1}:a]adelay={int(l['start'] * 1000)}:all=1,aresample=48000[a{i}]" for i, l in enumerate(tj["lines"])]
mix = "".join(f"[a{i}]" for i in range(len(tj["lines"])))
flt = ";".join(parts) + f";{mix}amix=inputs={len(tj['lines'])}:normalize=0,apad,atrim=0:{tj['duration']},loudnorm=I=-16:TP=-1.5:LRA=11[aout]"
args += ["-filter_complex", flt, "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", out]
subprocess.run(args, check=True)
print("wrote", out)
