#!/usr/bin/env python3
"""
sweep_verify.py - look at what bird_sweep decided to THROW AWAY.

The scan report shows you what was kept, and a detector that keeps too much
looks fine there. That is the wrong direction to check. For a footage bank the
only failure that matters is the one you cannot undo: a bird inside a stretch
that got dropped.

This samples frames from the DROPPED time only and tiles them with timecodes.
Look at the sheet. If you can see a bird in it, the settings are too aggressive
and the plan should not be cut.

  python Tools/sweep_verify.py <sweep_plan.json>
  python Tools/sweep_verify.py <sweep_plan.json> --n 60 --cols 8

Requires: numpy-free. ffmpeg on PATH, Pillow.
"""

import argparse
import json
import os
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont


def hhmmss(s):
    s = max(0, int(round(s)))
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"


def rotation_of(path):
    """Vertically-shot clips carry a rotation tag. Ignoring it renders the
    contact sheet sideways, which is a known trap on this footage."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream_side_data=rotation",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, check=True).stdout.strip()
        return int(float(out.splitlines()[0])) if out else 0
    except Exception:
        return 0


def dropped_ranges(events, duration):
    """The complement of the kept events - what the cut would discard."""
    gaps, prev = [], 0.0
    for e in sorted(events, key=lambda x: x["start"]):
        if e["start"] > prev:
            gaps.append((prev, e["start"]))
        prev = max(prev, e["end"])
    if duration > prev:
        gaps.append((prev, duration))
    return [(a, b) for a, b in gaps if b - a > 0.5]


def sample_times(gaps, n):
    """Spread n samples across the gaps in proportion to their length, so a
    long dead stretch gets looked at more than a two-second one."""
    total = sum(b - a for a, b in gaps)
    if total <= 0:
        return []
    times = []
    for a, b in gaps:
        k = max(1, int(round(n * (b - a) / total)))
        for i in range(k):
            times.append(a + (b - a) * (i + 0.5) / k)
    return sorted(times)[:n * 2]


def grab(path, t, rot, width=320):
    vf = [f"scale={width}:-2"]
    if rot in (90, -270):
        vf.insert(0, "transpose=1")
    elif rot in (-90, 270):
        vf.insert(0, "transpose=2")
    elif abs(rot) == 180:
        vf.insert(0, "transpose=1,transpose=1")
    cmd = ["ffmpeg", "-v", "error", "-noautorotate", "-ss", f"{t:.3f}",
           "-i", path, "-frames:v", "1", "-vf", ",".join(vf),
           "-f", "image2pipe", "-vcodec", "png", "-"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or not r.stdout:
        return None
    import io
    return Image.open(io.BytesIO(r.stdout)).convert("RGB")


def sheet(path, times, rot, cols, out_png):
    tiles = []
    for t in times:
        im = grab(path, t, rot)
        if im is not None:
            tiles.append((t, im))
    if not tiles:
        return None, 0
    tw, th = tiles[0][1].size
    rows = (len(tiles) + cols - 1) // cols
    lab = 16
    canvas = Image.new("RGB", (cols * tw, rows * (th + lab)), (17, 17, 17))
    d = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("arialbd.ttf", 12)
    except Exception:
        font = ImageFont.load_default()
    for i, (t, im) in enumerate(tiles):
        x, y = (i % cols) * tw, (i // cols) * (th + lab)
        canvas.paste(im, (x, y))
        d.text((x + 4, y + th + 2), hhmmss(t), fill=(255, 210, 120), font=font)
    canvas.save(out_png)
    return out_png, len(tiles)


def main():
    ap = argparse.ArgumentParser(description="Show what bird_sweep would discard.")
    ap.add_argument("plan")
    ap.add_argument("--n", type=int, default=48, help="frames per file (default 48)")
    ap.add_argument("--cols", type=int, default=6)
    ap.add_argument("--outdir")
    a = ap.parse_args()

    with open(a.plan, encoding="utf-8") as fh:
        plan = json.load(fh)
    outdir = a.outdir or os.path.join(os.path.dirname(os.path.abspath(a.plan)), "verify")
    os.makedirs(outdir, exist_ok=True)

    for f in plan["files"]:
        path, dur = f["path"], f["info"]["duration"]
        gaps = dropped_ranges(f["events"], dur)
        drop = sum(b - a for a, b in gaps)
        base = os.path.splitext(os.path.basename(path))[0]
        print(f"{base}: {len(f['events'])} kept events, {len(gaps)} dropped stretches, "
              f"{hhmmss(drop)} discarded ({100*drop/dur:.0f}%)")
        if not gaps:
            print("   nothing dropped - nothing to verify\n")
            continue
        longest = max(gaps, key=lambda g: g[1] - g[0])
        print(f"   longest dropped stretch: {hhmmss(longest[0])}-{hhmmss(longest[1])} "
              f"({hhmmss(longest[1]-longest[0])})")
        out = os.path.join(outdir, base + ".dropped.png")
        png, n = sheet(path, sample_times(gaps, a.n), rotation_of(path), a.cols, out)
        if png:
            print(f"   wrote {n} frames -> {png}\n")

    print("Look at every sheet. A bird in one of these is a false negative -\n"
          "loosen the preset and rescan. Do NOT cut a plan you have not verified.")


if __name__ == "__main__":
    main()
