"""
COLORADO NATIVE BIRDS - RELEASE GATE
Run this on a folder of finished Shorts before anything ships.

    python release_gate.py "<folder of .mp4>" [--overlays <dir>] [--long-form]

Design rule: a check only counts if a tool produced the number or a human looked
at the frame. Everything this script prints is measured. Everything it CANNOT
measure is listed at the end as a manual gate that names the artifact which must
exist - so it can't be quietly skipped by asserting it passed.
"""
import os, sys, re, json, subprocess, tempfile, argparse

# ---- targets ---------------------------------------------------------------
LUFS_TARGET = -14.0
LUFS_TOL = 0.5          # -14.5..-13.5 is inaudibly close; YouTube only turns things DOWN.
                        # Tight enough to still catch loudnorm's 1-3 dB undershoot on short clips.
EPS = 1e-9              # so a value exactly on the boundary is not failed by float error
TP_CEILING = -1.0
SHORT_MAX_SEC = 60.0
SAFE_BOTTOM = 0.75      # burned-in text must stay above this fraction of height
SAFE_RIGHT = 0.92       # ...and left of this fraction of width
SAFE_LEFT = 0.02
CLIP_HI_WARN = 3.0      # % of pixels at 254+ in any channel

# plain markers: Windows PowerShell 5.1 does not render ANSI colour by default
DIM, RS = "", ""
def ok(s):   return f"[PASS] {s}"
def bad(s):  return f"[FAIL] {s}"
def warn(s): return f"[WARN] {s}"


def probe(path):
    d = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path],
        capture_output=True, text=True).stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    return d, v, a


def decode_clean(path):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"],
                       capture_output=True, text=True)
    return (r.stderr.strip() == "", r.stderr.strip()[:300])


def loudness(path):
    """Measured on the file as delivered - do NOT re-pan to mono first."""
    t = subprocess.run(["ffmpeg", "-v", "info", "-i", path, "-af",
                        "ebur128=peak=true:framelog=quiet", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    def g(pat):
        m = re.findall(pat, t)
        return float(m[-1]) if m else float("nan")
    return (g(r"I:\s*(-?\d+\.\d+)\s*LUFS"),
            g(r"Peak:\s*(-?\d+\.\d+)\s*dBFS"),
            g(r"LRA:\s*(-?\d+\.\d+)\s*LU"))


def clipping(path, dur, n=5):
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        return None
    hi = []
    with tempfile.TemporaryDirectory() as td:
        for i in range(n):
            t = dur * (i + 0.5) / n
            p = os.path.join(td, f"f{i}.png")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", path,
                            "-frames:v", "1", p], check=False)
            if os.path.exists(p):
                a = np.asarray(Image.open(p).convert("RGB"))
                hi.append((a >= 254).mean(axis=(0, 1)) * 100)
    if not hi:
        return None
    import numpy as np
    return np.mean(hi, axis=0)


def overlay_safe_zone(png):
    """Exact check: alpha bounding box of the burned-in text vs the Shorts UI zone."""
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        return None
    im = Image.open(png).convert("RGBA")
    a = np.asarray(im)[:, :, 3]
    ys, xs = np.nonzero(a > 8)
    if len(ys) == 0:
        return None
    H, W = a.shape
    return dict(top=ys.min()/H, bottom=ys.max()/H, left=xs.min()/W, right=xs.max()/W, size=(W, H))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--overlays", default=None)
    ap.add_argument("--long-form", action="store_true")
    args = ap.parse_args()

    files = sorted(f for f in os.listdir(args.folder) if f.lower().endswith((".mp4", ".mov")))
    if not files:
        print("no video files found"); return 1
    print(f"\nRELEASE GATE - {len(files)} file(s) in {args.folder}\n" + "=" * 78)

    failures, warnings = [], []
    for fn in files:
        p = os.path.join(args.folder, fn)
        d, v, a = probe(p)
        dur = float(d["format"]["duration"])
        name = os.path.splitext(fn)[0]
        print(f"\n{name}")
        print(f"  {DIM}{v['width']}x{v['height']}  {eval(v['r_frame_rate']):.2f} fps  "
              f"{dur:.2f}s  {a['channels']}ch {a['sample_rate']}Hz  "
              f"{int(d['format']['size'])/1e6:.1f} MB{RS}")

        # 1. decode integrity
        clean, err = decode_clean(p)
        print("  1 decode      ", ok("no errors") if clean else bad(err))
        if not clean: failures.append(f"{name}: decode errors")

        # 2. loudness, measured as delivered
        I, TP, LRA = loudness(p)
        li = abs(I - LUFS_TARGET) <= LUFS_TOL + EPS
        lp = TP <= TP_CEILING + EPS
        print(f"  2 loudness    ", (ok if li else bad)(f"I {I:+.2f} LUFS (target {LUFS_TARGET} +/-{LUFS_TOL})"))
        print(f"    true peak   ", (ok if lp else bad)(f"{TP:+.2f} dBTP (ceiling {TP_CEILING})") + f"   {DIM}LRA {LRA:.1f}{RS}")
        if not li: failures.append(f"{name}: {I:+.2f} LUFS off target")
        if not lp: failures.append(f"{name}: {TP:+.2f} dBTP over ceiling")

        # 3. format sanity
        issues = []
        if not args.long_form:
            if v["width"] >= v["height"]: issues.append("not vertical")
            if dur > SHORT_MAX_SEC: issues.append(f"{dur:.1f}s exceeds Shorts max")
        if a["channels"] != 2: issues.append(f"{a['channels']}ch (expected stereo)")
        print("  3 format      ", ok("vertical, stereo, within length") if not issues else bad("; ".join(issues)))
        if issues: failures.append(f"{name}: {'; '.join(issues)}")

        # 4. clipping
        c = clipping(p, dur)
        if c is None:
            print("  4 clipping    ", warn("Pillow/numpy unavailable - not measured"))
            warnings.append(f"{name}: clipping not measured")
        else:
            hot = c.max() > CLIP_HI_WARN
            print(f"  4 clipping    ", (warn if hot else ok)(
                f"R/G/B at rail {c[0]:.2f}/{c[1]:.2f}/{c[2]:.2f}%"))
            if hot: warnings.append(f"{name}: {c.max():.1f}% clipped in one channel")

        # 5. burned-in text safe zone
        z = None
        if args.overlays:
            for cand in (f"hook_{name}.png", f"{name}.png"):
                q = os.path.join(args.overlays, cand)
                if os.path.exists(q):
                    z = overlay_safe_zone(q); break
        if z is None:
            print("  5 safe zone   ", warn("no matching hook overlay - verify by eye"))
            warnings.append(f"{name}: safe zone not measured")
        else:
            good = z["bottom"] <= SAFE_BOTTOM and z["right"] <= SAFE_RIGHT and z["left"] >= SAFE_LEFT
            print(f"  5 safe zone   ", (ok if good else bad)(
                f"text spans y {z['top']:.2f}-{z['bottom']:.2f}, x {z['left']:.2f}-{z['right']:.2f} "
                f"(limits: bottom<={SAFE_BOTTOM}, right<={SAFE_RIGHT})"))
            if not good: failures.append(f"{name}: burned-in text enters the Shorts UI zone")

    # ---- summary ----------------------------------------------------------
    print("\n" + "=" * 78)
    if failures:
        print(f"GATE FAILED - {len(failures)} blocking issue(s):")
        for f in failures: print("   x " + f)
    else:
        print("ALL MEASURED CHECKS PASSED")
    if warnings:
        print(f"\n{len(warnings)} warning(s):")
        for w in warnings: print("   ! " + w)

    print(f"""
{'-'*78}
MANUAL GATES - these cannot be measured. Each names the artifact that must
exist. If the artifact was not produced and looked at, the gate is NOT passed.

  A  CROPS AIMED         Crop rectangles drawn on real frames and inspected
                         before rendering.            artifact: cropcheck sheet
  B  SPECIES / SEX / AGE  Every on-screen or in-copy claim backed by a
                         high-res crop, confidence stated. Never a pronoun for
                         a species that cannot be sexed on sight.
                                                      artifact: id crop sheet
  C  SCRUB FRAME          A clean, strong cover frame exists in the first
                         seconds WITH the hook text up; timestamp recorded.
                                                      artifact: scrub sheet
  D  AUTHENTICITY COPY    No "unedited", no "one continuous take", no "no AI
                         was used". Use the narrow wording.
  E  INTERPOLATION        Only if source was 29.97p: eyeball consecutive
                         synthesized frames at peak motion. PSNR hides tearing.

Reminder: YouTube altered/synthetic-content checkbox = NO.
{'-'*78}""")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
