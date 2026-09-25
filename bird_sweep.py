#!/usr/bin/env python3
"""
bird_sweep.py — find bird activity in locked-off feeder footage and cut out the dead air.

Two passes, always in this order:

  1) SCAN  — reads the footage, finds every stretch where something is at the
             feeder, and writes a plan (JSON) plus a self-contained HTML report
             with thumbnails. Nothing is written to your footage.

  2) CUT   — reads the plan and produces ONE condensed file per source clip,
             with the dead air removed. Lossless stream copy: no re-encode, no
             generation loss. Your originals are never modified or deleted.

Usage
-----
  python bird_sweep.py scan "D:\\path\\C0556.MP4"
  python bird_sweep.py scan "D:\\Footage\\*.MP4" --outdir "D:\\sweep"
  python bird_sweep.py cut  "D:\\sweep\\sweep_plan.json"

Useful options for scan
-----------------------
  --preset conservative|balanced|aggressive   (default: balanced)
  --roi 30,20,45,55        Only look inside this box (x,y,w,h as % of frame).
                           The single best way to kill false positives from
                           wind-blown branches at the edges of frame.
  --hwaccel cuda           Use the NVIDIA decoder. Much faster on 4K.

Requires: Python 3.8+, numpy, and ffmpeg/ffprobe on PATH.
"""

import argparse
import base64
import glob
import html
import json
import os
import shutil
import subprocess
import sys
import time

import numpy as np

# ---------------------------------------------------------------- presets ---
# pix   : per-pixel brightness change (0-255) that counts as "different"
# area  : fraction of the frame that must differ to START an event
# pre   : seconds kept before an event starts (protects the arrival)
# post  : seconds kept after an event ends (protects the departure)
# merge : two events closer than this get joined into one
# minev : events shorter than this are discarded as noise
#
# For reference, at 4K source the 'area' figures correspond roughly to a subject
# of: conservative ~90px wide, balanced ~130px, aggressive ~200px in the 4K frame.
PRESETS = {
    "conservative": dict(pix=10, area=0.0010, pre=5.0, post=5.0, merge=30.0, minev=0.4),
    "balanced":     dict(pix=14, area=0.0020, pre=3.0, post=3.0, merge=15.0, minev=0.5),
    "aggressive":   dict(pix=20, area=0.0045, pre=2.0, post=2.0, merge=0.0,  minev=0.8),
}

DET_W, DET_H = 320, 180      # detection resolution — small on purpose, it's plenty
DET_FPS = 4                  # detection sample rate
SUSTAIN = 0.35               # once triggered, an event continues down to this
                             # fraction of the trigger threshold (hysteresis)

# The background reference is a temporal MEDIAN of frames spread over a window,
# not a running average. A median ignores anything present in a minority of its
# samples, so a bird passing through never contaminates the reference — which an
# averaging background does, leaving a ghost that reads as permanent activity.
BG_WINDOW_S = 20.0           # span the median samples are drawn from
BG_SAMPLES = 7               # how many frames go into the median (odd)
BG_REFRESH_S = 2.0           # how often to recompute it
MAX_FREEZE_S = 180           # hard cap on how long the reference may stay frozen
                             # during one event; a backstop against a permanent
                             # scene change (feeder moved, snow) latching on forever

# Restless-pixel mask. The opening stretch of the clip is used to learn which
# pixels are never still — a branch swaying in the wind, a glinting water dish,
# grass moving. Those are excluded from scoring for the rest of the file.
#
# The discriminator is DUTY CYCLE, not amplitude, and that distinction is the
# whole trick: a wind-blown branch changes a given pixel most of the time, while
# even a long bird visit only occupies its pixels for a fraction of the window.
# Thresholding on how *hard* a pixel changes would throw the birds out with the
# branches, since a dark branch against bright sky swings just as hard as a bird.
CALIB_S = 60.0               # learning window (kept in full — never culled)
RESTLESS_DUTY = 0.25         # a pixel changing this often is scenery, not a bird
RESTLESS_GROW = 2            # grow the mask by this many pixels; the fringe of a
                             # moving branch is restless too, just less often
MAX_MASK_FRAC = 0.25         # refuse to mask more than this much of the frame

# Thresholds adapt to each clip's own idle noise. A clean ISO-100 morning and a
# grainy ISO-6400 dusk have very different floors, and a fixed threshold that
# suits one will either latch permanently or go deaf on the other.
FLOOR_WINDOW = 400           # idle samples used to estimate the noise floor
FLOOR_TRIGGER_K = 3.0        # trigger must clear this multiple of the floor
FLOOR_SUSTAIN_K = 1.6        # and an event releases below this multiple
FLOOR_SEED_PCT = 25          # percentile of the calibration window used to prime
                             # the floor — see the note in scan_file. Low on
                             # purpose: the calibration window usually contains
                             # birds, and a low percentile tracks the quiet
                             # baseline rather than the busy average.
SATURATION_WARN = 0.95       # if a clip keeps more than this, the detector has
                             # almost certainly latched. Say so loudly.

# ---------------------------------------------------------- audio channel ---
# BirdNET as a SECOND, INDEPENDENT channel. Video answers "did anything move";
# audio answers "did anything call". They fail in completely different ways — a
# hummingbird hovering at the edge of frame is nearly invisible to an
# area-threshold detector and unmistakable to BirdNET — so the union of the two
# is much harder to slip past than either alone.
#
# The asymmetry driving every default here: a false positive keeps some dead air
# and costs disk. A false negative deletes a bird that cannot be re-shot.
# Everything below is tuned to fail toward keeping.
#
# BirdNET runs ONCE at a deliberately low confidence and its CSV is cached next
# to the plan. The retention threshold is applied afterwards in Python, so
# re-tuning it is instant and never re-runs the analyser.
AUDIO_SCAN_CONF = 0.25       # what BirdNET is actually run at (cached)
AUDIO_KEEP_CONF = 0.65       # what counts as a keep — strict on purpose
AUDIO_PAD = 1.0              # BirdNET reports 3s blocks; soften the edges
# BirdNET filters candidate species by location. Set these to wherever YOU
# record. The defaults are central Colorado and will be wrong anywhere else.
BIRDNET_LAT, BIRDNET_LON = 39.0, -105.5

# Force-keep windows. These bypass detection entirely.
MARK_PRE, MARK_POST = 8.0, 5.0   # around every Catalyst shot mark. A mark lands a
                                 # split second AFTER the event, so the window is
                                 # deliberately lopsided toward the past.
KEEP_TAIL_S = 30.0               # the last N seconds of every clip. The best
                                 # ending on this channel — the deserted block at
                                 # the end of One Block, No Peace — exists only
                                 # because the camera kept running after
                                 # everything left. Empty is not worthless.


# ------------------------------------------------------------------ utils ---
def need(tool):
    if shutil.which(tool) is None:
        sys.exit(f"ERROR: '{tool}' not found on PATH. Install ffmpeg and try again.")


def hhmmss(sec):
    sec = max(0.0, float(sec))
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{n:,.0f} B"
        n /= 1024.0


def probe(path):
    """Pull duration, size, codec and fps out of a media file."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}:\n{out.stderr.strip()}")
    d = json.loads(out.stdout)
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    if v is None:
        raise RuntimeError(f"No video stream in {path}")

    fps = 0.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            num, den = v.get(key, "0/1").split("/")
            if float(den):
                fps = float(num) / float(den)
                if fps:
                    break
        except Exception:
            pass

    fmt = d.get("format", {})
    dur = float(fmt.get("duration") or v.get("duration") or 0.0)
    size = int(fmt.get("size") or os.path.getsize(path))
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)

    return dict(
        duration=dur,
        size=size,
        fps=round(fps, 3),
        vcodec=v.get("codec_name", "?"),
        profile=v.get("profile", ""),
        width=v.get("width"),
        height=v.get("height"),
        acodec=(a or {}).get("codec_name"),
        bitrate=int(fmt.get("bit_rate") or 0),
    )


def all_intra(info):
    """All-Intra (XAVC S-I) means every frame is a keyframe, so stream-copy cuts
    land exactly where we ask. Long GOP snaps back to the nearest keyframe."""
    # Note this reads the ffprobe PROFILE ("High 4:2:2 Intra"), not the Sony
    # format name ("XAVC S-I"), which never appears in the stream metadata.
    #
    # This used to end with `or "422" in p and "10" in p and "intra" in p`. That
    # clause could never change the answer: it ends in the same test the first
    # operand already makes, so it is true only when the result is already true.
    # Verified identical across every combination of the relevant fragments
    # before removing it. Behaviour is unchanged; the dead half is gone.
    p = (info.get("profile") or "").lower()
    return "intra" in p


# -------------------------------------------------------------- detection ---
def scan_file(path, info, cfg, roi=None, hwaccel=None, quiet=False):
    """Decode the clip small and grey, and find every stretch containing a bird.

    Uses a slowly-adapting background reference rather than plain frame-to-frame
    differencing. That matters: a bird that lands and then sits perfectly still
    produces almost no frame-to-frame change, and a naive motion detector would
    end the event while the bird is still sitting there.
    """
    vf = [f"fps={DET_FPS}"]
    if roi:
        x, y, w, h = roi
        vf.append(
            f"crop=iw*{w/100:.4f}:ih*{h/100:.4f}:iw*{x/100:.4f}:ih*{y/100:.4f}"
        )
    vf.append(f"scale={DET_W}:{DET_H}:flags=fast_bilinear")
    vf.append("format=gray")

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if hwaccel:
        cmd += ["-hwaccel", hwaccel]
    cmd += ["-i", path, "-an", "-sn", "-vf", ",".join(vf),
            "-f", "rawvideo", "-pix_fmt", "gray", "-"]

    frame_bytes = DET_W * DET_H
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    from collections import deque

    win = max(BG_SAMPLES, int(BG_WINDOW_S * DET_FPS))
    buf = deque(maxlen=win)     # recent frames, newest last
    bg = None
    hits = None                 # per-pixel count of "changed" during calibration
    mask = None                 # True = pixel is usable (not restless scenery)
    n_used = DET_W * DET_H
    freeze_start = None
    in_event = False            # hysteresis state
    flags = []                  # one bool per sampled frame
    scores = []                 # fraction of frame differing, for the timeline
    idx = 0
    t0 = time.time()
    masked_frac = 0.0
    calib_fracs = []                         # primes the floor; see below
    floor_buf = deque(maxlen=FLOOR_WINDOW)   # idle-only scores
    refresh = max(1, int(BG_REFRESH_S * DET_FPS))
    calib_frames = max(int(CALIB_S * DET_FPS), BG_SAMPLES * 2)

    stats = {}

    def median_bg():
        """Median of a few frames spread across the window. Cheap, and immune to
        a subject that occupies a minority of the span."""
        n = len(buf)
        picks = np.linspace(0, n - 1, min(BG_SAMPLES, n)).astype(int)
        b = np.median(np.stack([buf[i] for i in picks]), axis=0)
        stats["med"] = float(np.median(b))
        stats["scale"] = float(np.median(np.abs(b - stats["med"]))) or 1.0
        return b

    try:
        while True:
            raw = proc.stdout.read(frame_bytes)
            if not raw or len(raw) < frame_bytes:
                break
            f = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)

            # Prime the reference before judging anything.
            if bg is None:
                buf.append(f)
                if len(buf) < BG_SAMPLES:
                    flags.append(False)
                    scores.append(0.0)
                    idx += 1
                    continue
                bg = median_bg()

            # Cancel out global light change before comparing. A cloud crossing
            # the sun shifts brightness AND squashes contrast, so matching the
            # median alone is not enough — fit both an offset and a gain, using
            # medians rather than means so the bird itself cannot drag the fit.
            # Without this, every passing cloud reads as a five-minute event.
            m_f = float(np.median(f))
            s_f = float(np.median(np.abs(f - m_f))) or 1.0
            gain = min(1.30, max(0.77, stats["scale"] / s_f))
            diff = np.abs((f - m_f) * gain + stats["med"] - bg)

            changed = diff > cfg["pix"]

            if idx < calib_frames:
                # Learning phase: measure how often each pixel changes, and judge
                # nothing. This window is kept in full, so no bird can be lost.
                if hits is None:
                    hits = np.zeros_like(diff)
                hits += changed
                # Seed the noise floor from the calibration window.
                #
                # THIS IS LOAD-BEARING. The floor is otherwise learned only from
                # frames judged idle — but if the threshold starts too low, no
                # frame is ever idle, the floor never fills, and the mechanism
                # meant to correct over-triggering is disabled exactly when it is
                # needed. Measured on Ant Moat: the *minimum* changed-pixel
                # fraction over 300s was 0.00158, against a conservative trigger
                # of 0.00100. Every frame tripped it, the clip latched into one
                # unbroken event, and the tool kept 100% of four different clips
                # while reporting noise_floor 0.0.
                calib_fracs.append(float(changed.mean()))
                buf.append(f)
                if idx % refresh == 0:
                    bg = median_bg()
                scores.append(0.0)
                flags.append(False)
                idx += 1
                continue

            if mask is None:
                duty = (hits / float(max(1, idx))).reshape(DET_H, DET_W)
                restless = duty > RESTLESS_DUTY
                # Grow it: the edge of a swaying branch crosses a given pixel
                # less than half the time, so it survives the duty test while
                # still firing often enough to matter.
                grown = restless.copy()
                for dy in range(-RESTLESS_GROW, RESTLESS_GROW + 1):
                    for dx in range(-RESTLESS_GROW, RESTLESS_GROW + 1):
                        grown |= np.roll(np.roll(restless, dy, 0), dx, 1)
                # Guard against masking the world away if the whole frame moves
                # (a pan, heavy snow) — better to over-keep than to go blind.
                if grown.mean() > MAX_MASK_FRAC:
                    grown[:] = False
                mask = ~grown.reshape(-1)
                n_used = max(1, int(mask.sum()))
                masked_frac = 1.0 - n_used / float(DET_W * DET_H)

                # Prime the floor from calibration, using a LOW percentile rather
                # than the median: the calibration window usually has birds in it
                # too, and a low percentile reflects the quiet baseline instead of
                # the busy average. Erring low keeps the detector sensitive, which
                # is the safe direction — over-keeping costs disk, under-keeping
                # costs footage that cannot be re-shot.
                if calib_fracs:
                    seed = float(np.percentile(calib_fracs, FLOOR_SEED_PCT))
                    floor_buf.extend([seed] * FLOOR_WINDOW)

            frac = float(np.count_nonzero(changed & mask)) / n_used

            # Adapt to this clip's own idle noise floor.
            floor = np.median(floor_buf) if len(floor_buf) >= 40 else 0.0
            trigger = max(cfg["area"], floor * FLOOR_TRIGGER_K)
            sustain = max(cfg["area"] * SUSTAIN, floor * FLOOR_SUSTAIN_K)

            # Hysteresis: it takes a clear signal to start an event, but only a
            # weak one to keep it going. A bird that turns side-on, hops behind
            # a branch, or tucks its head briefly shrinks its own silhouette;
            # without this the event chatters into fragments.
            if in_event:
                active = frac > sustain
            else:
                active = frac > trigger
            in_event = active

            scores.append(frac)
            flags.append(active)

            if active:
                # The reference holds still while something is there, because
                # only idle frames ever enter its buffer. That is what lets a
                # bird land and then sit motionless without the detector losing
                # it — plain frame-to-frame motion detection fails exactly here.
                if freeze_start is None:
                    freeze_start = idx
                elif (idx - freeze_start) / DET_FPS > MAX_FREEZE_S:
                    # Nothing legitimate sits at a feeder this long. The scene
                    # itself changed — feeder swung, snow settled, a hard shadow
                    # moved in. Accept the new normal instead of latching forever.
                    buf.clear()
                    buf.append(f)
                    bg = median_bg()
                    freeze_start = None
                    in_event = False
            else:
                freeze_start = None
                buf.append(f)
                floor_buf.append(frac)
                if idx % refresh == 0:
                    bg = median_bg()


            idx += 1
            if not quiet and idx % (DET_FPS * 120) == 0:
                done = idx / DET_FPS
                pct = 100.0 * done / info["duration"] if info["duration"] else 0
                rate = done / max(1e-6, time.time() - t0)
                eta = (info["duration"] - done) / rate if rate else 0
                sys.stdout.write(
                    f"\r    scanned {hhmmss(done)} / {hhmmss(info['duration'])}"
                    f"  ({pct:5.1f}%)  {rate:.0f}x realtime  ETA {hhmmss(eta)}   "
                )
                sys.stdout.flush()
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        err = proc.stderr.read().decode("utf-8", "replace")
        proc.wait()

    if not quiet:
        sys.stdout.write("\r" + " " * 96 + "\r")
        sys.stdout.flush()

    if idx == 0:
        raise RuntimeError(f"Decoded no frames from {path}.\n{err.strip()}")

    return (build_events(flags, cfg, info["duration"]), scores,
            {"masked_pct": round(100.0 * masked_frac, 2),
             "noise_floor": round(float(np.median(floor_buf)) if floor_buf else 0.0, 6),
             "vflags": np.asarray(flags, dtype=bool)})


def birdnet_week(path):
    """BirdNET wants a 1-48 'week' (four per month) so it can weight the model
    to what is actually present at this latitude in this season."""
    import datetime
    d = datetime.date.fromtimestamp(os.path.getmtime(path))
    return (d.month - 1) * 4 + min(4, (d.day + 6) // 8 + 1)


def run_birdnet(path, cache_dir, quiet=False):
    """Extract ch1 and run BirdNET over it. Returns [(start, end, species, conf)].

    ch1 (a:0) is the ECM-M1 dial channel — the same one every cut on this channel
    is taken from. Never the summed mix.

    The CSV is cached on the clip's basename, so a rescan or a threshold change
    costs nothing. Delete the cache dir to force a re-analyse.
    """
    os.makedirs(cache_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(path))[0]
    csv_path = os.path.join(cache_dir, base + ".BirdNET.results.csv")

    if not os.path.exists(csv_path):
        wav = os.path.join(cache_dir, base + ".ch1.wav")
        if not os.path.exists(wav):
            if not quiet:
                print("    extracting ch1 for BirdNET...")
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                 "-i", path, "-map", "0:a:0", "-ac", "1", "-ar", "48000", wav],
                check=True)
        if not quiet:
            print("    running BirdNET...")
        subprocess.run(
            [sys.executable, "-m", "birdnet_analyzer.analyze", wav,
             "-o", cache_dir,
             "--lat", str(BIRDNET_LAT), "--lon", str(BIRDNET_LON),
             "--week", str(birdnet_week(path)),
             "--min_conf", str(AUDIO_SCAN_CONF),
             "--rtype", "csv", "--skip_existing_results"],
            check=True,
            stdout=subprocess.DEVNULL if quiet else None)
        try:
            os.remove(wav)          # the wav is large and trivially regenerated
        except OSError:
            pass

    if not os.path.exists(csv_path):
        # BirdNET names its output from the input stem; find whatever it wrote.
        cands = [f for f in os.listdir(cache_dir)
                 if f.startswith(base) and f.lower().endswith(".csv")]
        if not cands:
            return []
        csv_path = os.path.join(cache_dir, cands[0])

    import csv as _csv
    out = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(_csv.reader(fh))
    if not rows:
        return []
    head = [h.strip().lower() for h in rows[0]]

    def col(*names):
        for i, h in enumerate(head):
            if any(n in h for n in names):
                return i
        return None

    i_s, i_e = col("start"), col("end")
    i_c, i_n = col("confidence"), col("common")
    if i_s is None or i_c is None:
        return []
    for r in rows[1:]:
        try:
            out.append((float(r[i_s]),
                        float(r[i_e]) if i_e is not None else float(r[i_s]) + 3.0,
                        r[i_n].strip() if i_n is not None else "?",
                        float(r[i_c])))
        except (ValueError, IndexError):
            continue
    return out


def audio_flags(dets, conf, nframes, duration):
    """Detections above `conf` become an active/idle array on the video grid."""
    f = np.zeros(nframes, dtype=bool)
    kept = []
    for s, e, sp, c in dets:
        if c < conf:
            continue
        kept.append((s, e, sp, c))
        a = max(0, int((s - AUDIO_PAD) * DET_FPS))
        b = min(nframes, int((e + AUDIO_PAD) * DET_FPS) + 1)
        if b > a:
            f[a:b] = True
    return f, kept


def sidecar_marks(path, fps):
    """Force-keep windows around every Catalyst shot mark.

    He marked these deliberately. Nothing a detector says should ever be allowed
    to throw one away — this is the one channel with a human in it.
    """
    xml = os.path.splitext(path)[0] + "M01.XML"
    if not os.path.exists(xml):
        return [], 0
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from marks import marks as _marks
        got = _marks(xml, fps)
    except Exception:
        return [], 0
    return [(max(0.0, sec - MARK_PRE), sec + MARK_POST) for _, sec, _ in got], len(got)


def build_events(flags, cfg, duration, force=None):
    """Turn the per-frame active/idle flags into padded, merged clip ranges.

    The opening calibration window is always kept in full — detection isn't
    trustworthy there, and keeping a few seconds of empty feeder costs almost
    nothing next to the risk of silently dropping a bird."""
    raw = [(0.0, min(CALIB_S, duration))]
    start = None
    for i, a in enumerate(flags):
        if a and start is None:
            start = i
        elif not a and start is not None:
            raw.append((start / DET_FPS, i / DET_FPS))
            start = None
    if start is not None:
        raw.append((start / DET_FPS, len(flags) / DET_FPS))

    # Drop single-frame flickers.
    raw = [(a, b) for a, b in raw if (b - a) >= cfg["minev"]]

    # Pad, so we never clip the arrival or the departure.
    padded = [(max(0.0, a - cfg["pre"]), min(duration, b + cfg["post"])) for a, b in raw]

    # Force-keep windows (Catalyst shot marks, the tail) bypass detection, the
    # minimum-event filter and the padding. They are kept because a human said
    # so, and no threshold gets a vote.
    for a, b in (force or []):
        a, b = max(0.0, a), min(duration, b)
        if b > a:
            padded.append((a, b))
    padded.sort()

    # Merge anything that ends up close together — two visits 8s apart should be
    # one clip, not two, and the gap between them is worth keeping for context.
    merged = []
    for a, b in padded:
        if merged and a - merged[-1][1] <= cfg["merge"]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])

    return [dict(start=round(a, 3), end=round(b, 3), dur=round(b - a, 3))
            for a, b in merged if b > a]


# ------------------------------------------------------------- thumbnails ---
def thumb_b64(path, t, hwaccel=None, width=300):
    """Grab one JPEG at time t and return it base64-encoded for the report."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if hwaccel:
        cmd += ["-hwaccel", hwaccel]
    cmd += ["-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
            "-vf", f"scale={width}:-2", "-q:v", "5", "-f", "image2", "-vcodec", "mjpeg", "-"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or not r.stdout:
        return None
    return base64.b64encode(r.stdout).decode("ascii")


# ----------------------------------------------------------------- report ---
CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin:0; padding:32px; background:#14161a; color:#e6e8ec;
       font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif; }
h1 { font-size:24px; margin:0 0 4px; letter-spacing:-.01em; }
h2 { font-size:17px; margin:0 0 2px; letter-spacing:-.01em; }
.sub { color:#9aa3af; font-size:13px; margin:0 0 28px; }
.card { background:#1b1e24; border:1px solid #2a2f38; border-radius:12px;
        padding:20px; margin-bottom:20px; }
.kpis { display:flex; flex-wrap:wrap; gap:12px; margin:16px 0 4px; }
.kpi { background:#20242c; border:1px solid #2f3641; border-radius:10px;
       padding:12px 16px; min-width:132px; }
.kpi .v { font-size:21px; font-weight:600; letter-spacing:-.02em; }
.kpi .l { font-size:11px; color:#9aa3af; text-transform:uppercase;
          letter-spacing:.06em; margin-top:3px; }
.save .v { color:#7fd6a3; }
.meta { color:#9aa3af; font-size:12.5px; margin-top:6px; }
.bar { position:relative; height:26px; background:#242832; border-radius:5px;
       overflow:hidden; margin:16px 0 6px; }
.bar i { position:absolute; top:0; bottom:0; background:#5b9dd9; }
.axis { display:flex; justify-content:space-between; color:#79818d; font-size:11px; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(210px,1fr));
        gap:14px; margin-top:18px; }
.ev { background:#20242c; border:1px solid #2f3641; border-radius:9px;
      overflow:hidden; }
.ev img { width:100%; display:block; background:#0c0e11; aspect-ratio:16/9;
          object-fit:cover; }
.ev .b { padding:9px 11px; }
.ev .tc { font-variant-numeric:tabular-nums; font-size:13px; font-weight:600; }
.ev .d { color:#9aa3af; font-size:11.5px; margin-top:2px; }
.none { color:#e0a561; }
code { background:#20242c; padding:2px 6px; border-radius:4px; font-size:12.5px; }
.foot { color:#79818d; font-size:12.5px; margin-top:26px; }
"""


def write_report(plan, out_html):
    tot_src = sum(f["info"]["duration"] for f in plan["files"])
    tot_keep = sum(f["kept_seconds"] for f in plan["files"])
    tot_bytes = sum(f["info"]["size"] for f in plan["files"])
    est_keep_bytes = sum(f["est_out_bytes"] for f in plan["files"])
    saved = tot_bytes - est_keep_bytes
    pct = (100.0 * saved / tot_bytes) if tot_bytes else 0
    nev = sum(len(f["events"]) for f in plan["files"])

    p = []
    p.append("<!doctype html><meta charset='utf-8'>")
    p.append("<title>Bird activity sweep</title>")
    p.append(f"<style>{CSS}</style>")
    p.append("<h1>Bird activity sweep</h1>")
    p.append(
        f"<p class='sub'>{len(plan['files'])} file(s) &middot; preset "
        f"<code>{html.escape(plan['preset'])}</code> &middot; scanned "
        f"{html.escape(plan['scanned_at'])}</p>"
    )

    p.append("<div class='card'><h2>Projected result</h2>")
    p.append("<div class='kpis'>")
    p.append(f"<div class='kpi'><div class='v'>{hhmmss(tot_src)}</div><div class='l'>Source</div></div>")
    p.append(f"<div class='kpi'><div class='v'>{hhmmss(tot_keep)}</div><div class='l'>Kept</div></div>")
    p.append(f"<div class='kpi'><div class='v'>{nev}</div><div class='l'>Events</div></div>")
    p.append(f"<div class='kpi'><div class='v'>{human_bytes(tot_bytes)}</div><div class='l'>On disk now</div></div>")
    p.append(f"<div class='kpi save'><div class='v'>{human_bytes(saved)}</div><div class='l'>Freed ({pct:.0f}%)</div></div>")
    p.append("</div>")
    p.append("<p class='meta'>Size figures are estimates from average bitrate. "
             "Nothing has been written yet.</p></div>")

    for f in plan["files"]:
        i = f["info"]
        dur = i["duration"] or 1
        p.append("<div class='card'>")
        p.append(f"<h2>{html.escape(os.path.basename(f['path']))}</h2>")
        p.append(
            f"<p class='meta'>{i['width']}&times;{i['height']} &middot; {i['fps']} fps &middot; "
            f"{html.escape(i['vcodec'])} {html.escape(i['profile'] or '')} &middot; "
            f"{human_bytes(i['size'])} &middot; {hhmmss(i['duration'])} &middot; "
            f"cuts are <strong>{'frame-accurate' if f['frame_accurate'] else 'keyframe-snapped'}</strong></p>"
        )
        m = f.get("meta") or {}
        if m:
            p.append(
                f"<p class='meta'>Ignored {m.get('masked_pct', 0)}% of frame as "
                f"restless scenery &middot; noise floor {m.get('noise_floor', 0):.5f}"
                f"{' &middot; <strong>check this</strong> if a visit is missing' if (m.get('masked_pct') or 0) > 8 else ''}</p>"
            )

        p.append("<div class='bar'>")
        for e in f["events"]:
            left = 100.0 * e["start"] / dur
            wid = max(0.15, 100.0 * e["dur"] / dur)
            p.append(f"<i style='left:{left:.3f}%;width:{wid:.3f}%'></i>")
        p.append("</div>")
        p.append(f"<div class='axis'><span>00:00:00</span><span>{hhmmss(i['duration'])}</span></div>")

        p.append("<div class='kpis'>")
        p.append(f"<div class='kpi'><div class='v'>{len(f['events'])}</div><div class='l'>Events</div></div>")
        p.append(f"<div class='kpi'><div class='v'>{hhmmss(f['kept_seconds'])}</div><div class='l'>Kept</div></div>")
        p.append(f"<div class='kpi'><div class='v'>{hhmmss(i['duration'] - f['kept_seconds'])}</div><div class='l'>Dropped</div></div>")
        sv = i["size"] - f["est_out_bytes"]
        sp = (100.0 * sv / i["size"]) if i["size"] else 0
        p.append(f"<div class='kpi save'><div class='v'>{human_bytes(sv)}</div><div class='l'>Freed ({sp:.0f}%)</div></div>")
        p.append("</div>")

        if not f["events"]:
            p.append("<p class='none'>No activity detected. This file would be skipped "
                     "entirely &mdash; check the ROI and preset before trusting that.</p>")
        else:
            p.append("<div class='grid'>")
            for n, e in enumerate(f["events"], 1):
                img = e.get("thumb")
                tag = (f"<img src='data:image/jpeg;base64,{img}' alt=''>"
                       if img else "<img alt=''>")
                p.append(
                    f"<div class='ev'>{tag}<div class='b'>"
                    f"<div class='tc'>{hhmmss(e['start'])} &ndash; {hhmmss(e['end'])}</div>"
                    f"<div class='d'>#{n} &middot; {e['dur']:.1f}s</div>"
                    f"</div></div>"
                )
            p.append("</div>")
        p.append("</div>")

    p.append("<p class='foot'>Thumbnail is the midpoint of each event. If you see "
             "empty perches here, raise the preset or set an <code>--roi</code>. "
             "If you know a visit is missing, lower it.<br>"
             "Run the cut with: <code>python bird_sweep.py cut "
             f"\"{html.escape(plan['plan_path'])}\"</code></p>")

    with open(out_html, "w", encoding="utf-8") as fh:
        fh.write("\n".join(p))


# -------------------------------------------------------------------- cut ---
def concat_list(path, events, list_path):
    """Write an ffmpeg concat-demuxer script that pulls the kept ranges straight
    out of the source file. No intermediate files, no re-encode."""
    src = os.path.abspath(path).replace("\\", "/").replace("'", "'\\''")
    with open(list_path, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(f"file '{src}'\n")
            fh.write(f"inpoint {e['start']:.3f}\n")
            fh.write(f"outpoint {e['end']:.3f}\n")


def cut_file(entry, outdir, keep_lists=False):
    path = entry["path"]
    events = entry["events"]
    if not events:
        return None, "no events — skipped"

    base = os.path.splitext(os.path.basename(path))[0]
    out = os.path.join(outdir, f"{base}_condensed.mp4")
    lst = os.path.join(outdir, f"{base}_concat.txt")
    concat_list(path, events, lst)

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-fflags", "+genpts",
        "-f", "concat", "-safe", "0", "-i", lst,
        # -map is NOT optional. Without it ffmpeg picks one stream per type and
        # this camera records FOUR mono PCM tracks — measured 21 Aug 2026, a cut
        # of C0476 came back with 1 of 4 audio channels and nobody would notice
        # until the ch3 omni was needed and gone. The Sony data stream (codec
        # "none") cannot be written into MP4 at all, so it is dropped
        # deliberately rather than failing the whole cut.
        "-map", "0:v", "-map", "0:a",
        "-c", "copy", "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart", out,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if not keep_lists:
        try:
            os.remove(lst)
        except OSError:
            pass
    if r.returncode != 0:
        return None, r.stderr.strip()[:500]
    return out, None


# ------------------------------------------------------------------- main ---
def expand(patterns):
    out = []
    for p in patterns:
        hits = glob.glob(p)
        if hits:
            out.extend(sorted(h for h in hits if os.path.isfile(h)))
        elif os.path.isfile(p):
            out.append(p)
        else:
            print(f"  ! no match: {p}")
    return out


def cmd_scan(a):
    need("ffmpeg"); need("ffprobe")
    files = expand(a.paths)
    if not files:
        sys.exit("No input files matched.")

    cfg = dict(PRESETS[a.preset])
    for k, v in (("pix", a.pix), ("area", a.area), ("pre", a.pre),
                 ("post", a.post), ("merge", a.merge)):
        if v is not None:
            cfg[k] = v

    roi = None
    if a.roi:
        try:
            roi = [float(x) for x in a.roi.split(",")]
            assert len(roi) == 4
        except Exception:
            sys.exit("--roi must be four numbers: x,y,w,h as percentages, e.g. 30,20,45,55")

    outdir = a.outdir or os.path.dirname(os.path.abspath(files[0]))
    os.makedirs(outdir, exist_ok=True)

    plan = dict(
        version=1,
        preset=a.preset,
        config=cfg,
        roi=roi,
        scanned_at=time.strftime("%Y-%m-%d %H:%M"),
        outdir=outdir,
        files=[],
    )

    for n, path in enumerate(files, 1):
        print(f"[{n}/{len(files)}] {os.path.basename(path)}")
        info = probe(path)
        print(f"    {info['width']}x{info['height']} {info['fps']}fps "
              f"{info['vcodec']} {info['profile']} · {hhmmss(info['duration'])} · "
              f"{human_bytes(info['size'])}")

        t0 = time.time()
        events, _, meta = scan_file(path, info, cfg, roi=roi, hwaccel=a.hwaccel)
        vflags = meta.pop("vflags")
        dur = info["duration"]

        # --- second channel: what CALLED, as opposed to what moved ---------
        aflags = np.zeros_like(vflags)
        adets = []
        if a.audio:
            try:
                dets = run_birdnet(path, os.path.join(outdir, "birdnet"))
                aflags, adets = audio_flags(dets, a.audio_conf, len(vflags), dur)
            except Exception as exc:
                print(f"    ! BirdNET failed ({exc}) — falling back to video only")

        # --- force-keep windows: a human said so --------------------------
        force, nmarks = [], 0
        if not a.no_marks:
            force, nmarks = sidecar_marks(path, info["fps"])
            if nmarks:
                print(f"    {nmarks} Catalyst shot marks — force-kept")
        if a.keep_tail > 0 and dur > a.keep_tail:
            force.append((dur - a.keep_tail, dur))

        combined = vflags | aflags
        events = build_events(combined, cfg, dur, force=force)
        kept = sum(e["dur"] for e in events)
        rate = info["size"] / max(1e-6, info["duration"]) if info["duration"] else 0

        # --- what each channel actually bought, in seconds ----------------
        kept_v = sum(e["dur"] for e in build_events(vflags, cfg, dur, force=force))
        for e in events:
            i0 = max(0, int(e["start"] * DET_FPS))
            i1 = min(len(vflags), int(e["end"] * DET_FPS) + 1)
            tag = ("V" if vflags[i0:i1].any() else "") + ("A" if aflags[i0:i1].any() else "")
            e["by"] = tag or "M"        # M = forced: a shot mark or the tail
        meta["audio"] = dict(
            enabled=bool(a.audio),
            conf=a.audio_conf,
            detections=len(adets),
            species=sorted({sp for _, _, sp, _ in adets})[:40],
            kept_video_only=round(kept_v, 2),
            kept_combined=round(kept, 2),
            audio_cost_seconds=round(kept - kept_v, 2),
        )
        meta["marks"] = nmarks
        if a.audio:
            n_a_only = sum(1 for e in events if e["by"] == "A")
            print(f"    audio: {len(adets)} calls >= {a.audio_conf} · "
                  f"{n_a_only} events audio-only · "
                  f"costs {hhmmss(kept - kept_v)} extra retained")

        if not a.no_thumbs:
            for e in events[: a.max_thumbs]:
                e["thumb"] = thumb_b64(path, (e["start"] + e["end"]) / 2.0, a.hwaccel)

        plan["files"].append(dict(
            path=os.path.abspath(path),
            info=info,
            events=events,
            kept_seconds=round(kept, 2),
            est_out_bytes=int(kept * rate),
            frame_accurate=all_intra(info),
            meta=meta,
        ))

        pct = 100.0 * (1 - kept / info["duration"]) if info["duration"] else 0
        print(f"    {len(events)} events · keeping {hhmmss(kept)} of "
              f"{hhmmss(info['duration'])} · drops {pct:.0f}% "
              f"(~{human_bytes(info['size'] - int(kept * rate))}) "
              f"· {time.time()-t0:.0f}s\n")

    plan_path = os.path.join(outdir, "sweep_plan.json")
    plan["plan_path"] = os.path.abspath(plan_path)
    with open(plan_path, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=1)

    report = os.path.join(outdir, "sweep_report.html")
    write_report(plan, report)

    # CSV of original timecodes — the map back to where each kept chunk came from.
    csv_path = os.path.join(outdir, "sweep_events.csv")
    with open(csv_path, "w", encoding="utf-8") as fh:
        fh.write("file,event,start_tc,end_tc,start_sec,end_sec,duration_sec\n")
        for f in plan["files"]:
            b = os.path.basename(f["path"])
            for i, e in enumerate(f["events"], 1):
                fh.write(f'"{b}",{i},{hhmmss(e["start"])},{hhmmss(e["end"])},'
                         f'{e["start"]:.3f},{e["end"]:.3f},{e["dur"]:.3f}\n')

    print("Wrote:")
    print(f"  report  {report}")
    print(f"  plan    {plan_path}")
    print(f"  csv     {csv_path}")
    print(f"\nOpen the report. If it looks right:\n  python bird_sweep.py cut \"{plan_path}\"")


def cmd_cut(a):
    need("ffmpeg")
    with open(a.plan, encoding="utf-8") as fh:
        plan = json.load(fh)

    outdir = a.outdir or os.path.join(os.path.dirname(os.path.abspath(a.plan)), "condensed")
    os.makedirs(outdir, exist_ok=True)

    made, freed = [], 0
    for n, entry in enumerate(plan["files"], 1):
        name = os.path.basename(entry["path"])
        print(f"[{n}/{len(plan['files'])}] {name}")
        if not os.path.exists(entry["path"]):
            print("    ! source missing — skipped\n")
            continue
        t0 = time.time()
        out, err = cut_file(entry, outdir, keep_lists=a.keep_lists)
        if err:
            print(f"    ! {err}\n")
            continue
        sz = os.path.getsize(out)
        freed += entry["info"]["size"] - sz
        made.append(out)
        print(f"    -> {os.path.basename(out)}  {human_bytes(sz)} "
              f"(was {human_bytes(entry['info']['size'])})  {time.time()-t0:.0f}s\n")

    print(f"Done. {len(made)} file(s) in {outdir}")
    print(f"Space freed once you delete the originals: {human_bytes(freed)}")
    print("\nOriginals were NOT touched. Watch the condensed files all the way "
          "through before you delete anything.")


def main():
    ap = argparse.ArgumentParser(
        description="Find bird activity in locked-off footage and cut out the dead air.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="analyse footage and write a plan + report")
    s.add_argument("paths", nargs="+", help="files or globs")
    s.add_argument("--preset", choices=list(PRESETS), default="balanced")
    s.add_argument("--roi", help="x,y,w,h as %% of frame, e.g. 30,20,45,55")
    s.add_argument("--outdir")
    s.add_argument("--hwaccel", help="e.g. cuda, qsv, d3d11va")
    s.add_argument("--no-thumbs", action="store_true")
    s.add_argument("--max-thumbs", type=int, default=400)
    s.add_argument("--pix", type=float)
    s.add_argument("--area", type=float)
    s.add_argument("--pre", type=float)
    s.add_argument("--post", type=float)
    s.add_argument("--merge", type=float)
    s.add_argument("--audio", action="store_true",
                   help="add the BirdNET audio channel (recommended for banking)")
    s.add_argument("--audio-conf", type=float, default=AUDIO_KEEP_CONF,
                   help=f"confidence a call needs to keep a window (default {AUDIO_KEEP_CONF}). "
                        "Re-tuning this is instant; BirdNET is not re-run.")
    s.add_argument("--no-marks", action="store_true",
                   help="do NOT force-keep windows around Catalyst shot marks")
    s.add_argument("--keep-tail", type=float, default=KEEP_TAIL_S,
                   help=f"always keep the last N seconds (default {KEEP_TAIL_S}); 0 disables")
    s.set_defaults(func=cmd_scan)

    c = sub.add_parser("cut", help="execute a plan (lossless, originals untouched)")
    c.add_argument("plan")
    c.add_argument("--outdir")
    c.add_argument("--keep-lists", action="store_true")
    c.set_defaults(func=cmd_cut)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
