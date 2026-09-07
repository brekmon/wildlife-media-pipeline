"""
COLORADO NATIVE BIRDS - AUDIO GATE

    python audio_gate.py <file-or-folder> [--stream N]

Answers one question in one number: DOES THE BIRD STAND OUT FROM THE NOISE?

    separation = (99th percentile short-time level) - (40th percentile)
                 measured above 1.5 kHz

The 99th percentile is the pecks and calls. The 40th is the road hum between
them. Their difference is what the ear judges. Bigger is better.

WHY NOT AVERAGE BAND ENERGY
---------------------------
Averaging energy in a "bird band" across a whole clip is dominated by the
CONSTANT hum, because the bird only makes sound occasionally. On 2026-08-10
that metric said denoising was destroying the bird - the average fell 27 dB -
when in fact the floor was falling and the pecks were untouched. Hours were
lost to it. Never judge bird audio by average band energy again.

TARGETS
-------
    separation  >= 18 dB   good
                >= 14 dB   acceptable
                <  14 dB   the shot will sound noisy no matter what is done
    floor spread across shots in one video  <= 6 dB
        (a bigger step is audible as the hum lurching at the cut; either treat
         the noisy shot harder or lengthen the audio cross-fade at that join)
"""
import argparse
import glob
import os
import subprocess
import sys

import numpy as np

SR = 48000
GOOD, OK = 18.0, 14.0


def levels(path, stream=0, hp=1500, win=1024):
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-map", f"0:a:{stream}",
         "-af", f"highpass=f={hp}", "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
        capture_output=True)
    x = np.frombuffer(r.stdout, dtype=np.float32).astype(np.float64)
    if len(x) < win * 8:
        return None
    e = np.array([np.sqrt(np.mean(x[i:i + win] ** 2)) + 1e-12
                  for i in range(0, len(x) - win, win)])
    return 20 * np.log10(e)


def report(path, stream=0):
    db = levels(path, stream)
    if db is None:
        print(f"  {os.path.basename(path):<44} (too short to measure)")
        return None
    floor = float(np.percentile(db, 40))
    peaks = float(np.percentile(db, 99))
    sep = peaks - floor
    tag = "PASS" if sep >= GOOD else ("OK  " if sep >= OK else "FAIL")
    print(f"  [{tag}] {os.path.basename(path):<44} "
          f"floor {floor:7.1f}  peaks {peaks:7.1f}  separation {sep:5.1f} dB")
    return floor, sep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    ap.add_argument("--stream", type=int, default=0)
    a = ap.parse_args()

    files = ([a.target] if os.path.isfile(a.target)
             else sorted(glob.glob(os.path.join(a.target, "*.m*"))))
    if not files:
        sys.exit("nothing to measure")

    print(f"AUDIO GATE - {len(files)} file(s)\n" + "=" * 78)
    rows = [r for r in (report(f, a.stream) for f in files) if r]
    if not rows:
        return
    floors = [f for f, _ in rows]
    seps = [s for _, s in rows]
    spread = max(floors) - min(floors)
    print("=" * 78)
    print(f"  worst separation: {min(seps):.1f} dB   "
          f"{'OK' if min(seps) >= OK else 'BELOW THRESHOLD'}")
    print(f"  floor spread:     {spread:.1f} dB   "
          f"{'OK' if spread <= 6 else 'AUDIBLE STEP AT THE CUTS - treat or cross-fade'}")


if __name__ == "__main__":
    main()
