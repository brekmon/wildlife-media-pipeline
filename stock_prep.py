#!/usr/bin/env python3
"""
COLORADO NATIVE BIRDS - STOCK PREP

Turn the raw archive into licensable stock clips, with the keywording done.

    python stock_prep.py plan "D:\\sweep\\sweep_plan.json"   -> measure + pick
    python stock_prep.py cut  "D:\\stock\\stock_plan.json"   -> render masters
    python stock_prep.py meta "D:\\stock\\stock_plan.json"   -> BirdNET -> upload CSV

WHY THIS EXISTS
---------------
Stock agencies pay for clips that are (a) clean and (b) findable. Everyone gets
(a) roughly right and almost nobody does (b), because keywording 300 clips by
hand is miserable. BirdNET already knows which species is audible in every
second of this archive, so (b) is nearly free here. That is the whole edge - do
not ship clips without the metadata step, it is the part that earns the money.

WHAT MAKES A CLIP STOCK-VIABLE (all measured, none asserted)
------------------------------------------------------------
    duration     5-20s          buyers cut to 5-10s; under 5 is unsellable
    highlights   <3% at 254+    blown sky/feeder = instant reviewer rejection
    shadows      <5% at 2-      crushed blacks read as cheap
    sharpness    reported       relative gradient energy; outliers get flagged
                                for the contact sheet, never auto-rejected

A clip is only CLEARED after a human looks at the contact sheet. This script
picks candidates and measures them; it does not decide that footage is good.

BRAND: stock masters carry NO overlays, NO logo, NO text, NO music. That is the
opposite of the channel deliverable and it is not a style choice - agencies
reject anything with burned-in branding.

SPECIES: a species only becomes a keyword above --conf (default 0.65). Anything
weaker lands in review_species for a human call. Mislabelled species is the top
cause of stock rejection and it poisons the whole batch's standing.

ROTATION: vertically-shot clips (C0476/C0526/C0527 and friends) carry a rotate
tag that a re-encode silently ignores, producing sideways masters. This script
reads the tag and injects -noautorotate plus the matching transpose.
bird_sweep.py never had to care because it stream-copies; this one re-encodes,
so it does.

AUDIO: natural sound raises the price of a wildlife clip, so it is kept - but
the A7C II records 4 channels and they must never be summed. One channel is
selected (--achan, default 1). Which one is better is per-clip; ch3 is the
fixed omni.

Requires: Python 3.8+, numpy, ffmpeg/ffprobe on PATH, birdnet_analyzer for meta.
"""

import argparse
import base64
import csv
import html
import json
import os
import re
import subprocess
import sys
import time

import numpy as np

# ---- stock-viability targets ----------------------------------------------
MIN_SEC = 5.0           # under this no agency will take it
MAX_SEC = 20.0          # over this is wasted encode; buyers cut short anyway
IDEAL_LO, IDEAL_HI = 8.0, 15.0
CLIP_HI_PCT = 3.0       # % pixels at 254+ -> blown
CLIP_LO_PCT = 5.0       # % pixels at 2-   -> crushed
PROBE_FRAMES = 5        # frames sampled per candidate for the measurement
SPECIES_CONF = 0.65     # keyword floor; below this a human decides

# plain markers: Windows PowerShell 5.1 does not render ANSI colour by default
def ok(s):   return f"[KEEP] {s}"
def bad(s):  return f"[DROP] {s}"
def warn(s): return f"[FLAG] {s}"


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def hhmmss(t):
    t = max(0.0, float(t))
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{int((t % 1) * 1000):03d}"


def slug(s):
    return re.sub(r"[^A-Za-z0-9]+", "-", str(s)).strip("-").lower()


def human_bytes(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{u}"
        n /= 1024.0
    return f"{n:.1f}PB"


# ---- rotation --------------------------------------------------------------
def rotation_of(path):
    """Degrees of display rotation, or 0.

    Checks the legacy rotate tag AND the displaymatrix side-data: Sony writes
    the tag, ffmpeg 8 reports the side data, and reading only one of them is
    exactly how a clip ships sideways.
    """
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
               "-print_format", "json", "-show_streams", path])
    try:
        st = json.loads(out.stdout)["streams"][0]
    except Exception:
        return 0
    tag = st.get("tags", {}).get("rotate")
    if tag:
        try:
            return int(float(tag)) % 360
        except Exception:
            pass
    for sd in st.get("side_data_list", []) or []:
        if "rotation" in sd:
            try:
                return int(-float(sd["rotation"])) % 360
            except Exception:
                pass
    return 0


def transpose_for(deg):
    """The filter that undoes `deg`, given we decode with -noautorotate."""
    return {90: "transpose=1", 180: "transpose=1,transpose=1",
            270: "transpose=2"}.get(deg % 360)


def probe(path):
    out = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", path])
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
    naud = sum(1 for s in d.get("streams", []) if s.get("codec_type") == "audio")
    return dict(
        width=int(v.get("width") or 0), height=int(v.get("height") or 0),
        fps=round(fps, 3), vcodec=v.get("codec_name", "?"),
        duration=float(fmt.get("duration") or 0.0),
        size=int(fmt.get("size") or os.path.getsize(path)),
        rotation=rotation_of(path), naudio=naud,
    )


# ---- measurement -----------------------------------------------------------
def sample_gray(path, t, w=320, hwaccel=None):
    """One greyscale frame at t as a numpy array.

    Small on purpose: the exposure and gradient stats survive the downscale,
    and decoding candidates at full 4K would take all night.
    """
    cmd = ["ffmpeg", "-v", "error"]
    if hwaccel:
        cmd += ["-hwaccel", hwaccel]
    cmd += ["-noautorotate", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
            "-vf", f"scale={w}:-2,format=gray", "-f", "rawvideo", "-"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or not r.stdout:
        return None
    n = len(r.stdout)
    h = n // w
    if h == 0:
        return None
    return np.frombuffer(r.stdout[: w * h], dtype=np.uint8).reshape(h, w)


def measure(path, start, end, hwaccel=None):
    """Exposure and sharpness across PROBE_FRAMES samples inside the event."""
    ts = np.linspace(start + 0.3, max(start + 0.4, end - 0.3), PROBE_FRAMES)
    hi, lo, sharp, got = [], [], [], 0
    for t in ts:
        g = sample_gray(path, float(t), hwaccel=hwaccel)
        if g is None:
            continue
        got += 1
        f = g.astype(np.float32)
        hi.append(100.0 * float((g >= 254).mean()))
        lo.append(100.0 * float((g <= 2).mean()))
        gx = float(np.abs(np.diff(f, axis=1)).mean())
        gy = float(np.abs(np.diff(f, axis=0)).mean())
        sharp.append((gx + gy) / 2.0)
    if not got:
        return None
    return dict(frames=got,
                clip_hi=round(max(hi), 3), clip_lo=round(max(lo), 3),
                sharp=round(float(np.mean(sharp)), 3))


def thumb_b64(path, t, hwaccel=None):
    cmd = ["ffmpeg", "-v", "error"]
    if hwaccel:
        cmd += ["-hwaccel", hwaccel]
    cmd += ["-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
            "-vf", "scale=380:-2", "-f", "image2", "-vcodec", "mjpeg", "-"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or not r.stdout:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(r.stdout).decode()


# ---- plan ------------------------------------------------------------------
def window_event(ev):
    """Trim an event down to a stock-length window centred on its middle.

    Sweep events run as long as the bird stays. Stock wants 8-15s, so a 90s
    visit becomes one good window, not a 90s file nobody licenses.
    """
    start, end, dur = ev["start"], ev["end"], ev["dur"]
    if dur < MIN_SEC:
        return None
    if dur <= MAX_SEC:
        return start, end
    mid = (start + end) / 2.0
    half = IDEAL_HI / 2.0
    return max(start, mid - half), min(end, mid + half)


def do_plan(a):
    with open(a.plan, encoding="utf-8") as fh:
        sweep = json.load(fh)

    outdir = a.outdir or os.path.join(os.path.dirname(os.path.abspath(a.plan)), "stock")
    os.makedirs(outdir, exist_ok=True)

    out = dict(planned_at=time.strftime("%Y-%m-%d %H:%M"),
               source_plan=os.path.abspath(a.plan), outdir=os.path.abspath(outdir),
               conf_floor=a.conf, clips=[])

    n_in = n_short = n_blown = n_crushed = 0

    for f in sweep.get("files", []):
        path = f["path"]
        if not os.path.exists(path):
            print(warn(f"missing source, skipped: {path}"))
            continue
        info = probe(path)
        base = os.path.splitext(os.path.basename(path))[0]
        rot = info["rotation"]
        print(f"\n{os.path.basename(path)}  {info['width']}x{info['height']} "
              f"{info['fps']}fps  rot={rot}  {info['naudio']}ch-streams")
        if rot:
            print(warn(f"rotation {rot} deg -> will re-encode with "
                       f"-noautorotate,{transpose_for(rot)}"))

        for i, ev in enumerate(f.get("events", []), 1):
            n_in += 1
            win = window_event(ev)
            if win is None:
                n_short += 1
                continue
            s, e = win
            m = measure(path, s, e, hwaccel=a.hwaccel)
            if m is None:
                print(bad(f"{base} ev{i:03d} - could not decode a probe frame"))
                continue

            reasons = []
            if m["clip_hi"] > CLIP_HI_PCT:
                reasons.append(f"blown {m['clip_hi']:.1f}%")
                n_blown += 1
            if m["clip_lo"] > CLIP_LO_PCT:
                reasons.append(f"crushed {m['clip_lo']:.1f}%")
                n_crushed += 1

            clip = dict(
                id=f"{base}-{i:03d}", source=os.path.abspath(path),
                start=round(s, 3), end=round(e, 3), dur=round(e - s, 3),
                start_tc=hhmmss(s), rotation=rot,
                width=info["width"], height=info["height"], fps=info["fps"],
                naudio=info["naudio"], measured=m,
                status="candidate" if not reasons else "rejected",
                reasons=reasons,
            )
            if not a.no_thumbs:
                clip["thumb"] = thumb_b64(path, (s + e) / 2.0, hwaccel=a.hwaccel)
            out["clips"].append(clip)

            line = f"{base} ev{i:03d}  {hhmmss(s)}  {e-s:5.1f}s  " \
                   f"hi {m['clip_hi']:5.2f}%  lo {m['clip_lo']:5.2f}%  " \
                   f"sharp {m['sharp']:6.2f}"
            print(bad(line + "  " + ", ".join(reasons)) if reasons else ok(line))

    # Sharpness is only meaningful relative to the rest of the batch, so flag
    # the soft tail rather than pretending an absolute threshold exists.
    cands = [c for c in out["clips"] if c["status"] == "candidate"]
    if len(cands) >= 4:
        vals = np.array([c["measured"]["sharp"] for c in cands])
        floor = float(np.percentile(vals, 20))
        for c in cands:
            if c["measured"]["sharp"] <= floor:
                c["reasons"].append(f"soft for this batch (<= p20 {floor:.2f})")
                c["status"] = "review"
        out["sharp_p20"] = round(floor, 3)

    plan_path = os.path.join(outdir, "stock_plan.json")
    out["plan_path"] = os.path.abspath(plan_path)
    with open(plan_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    report = os.path.join(outdir, "stock_contact_sheet.html")
    write_sheet(out, report)

    n_c = sum(1 for c in out["clips"] if c["status"] == "candidate")
    n_r = sum(1 for c in out["clips"] if c["status"] == "review")
    n_x = sum(1 for c in out["clips"] if c["status"] == "rejected")
    tot = sum(c["dur"] for c in out["clips"] if c["status"] != "rejected")
    print(f"\n{'-'*66}")
    print(f"  {n_in} sweep events in")
    print(f"  {n_short} dropped under {MIN_SEC:.0f}s")
    print(f"  {n_c} candidates  {n_r} need a look  {n_x} rejected on exposure")
    print(f"  {tot/60:.1f} min of sellable footage")
    print(f"{'-'*66}")
    print("Wrote:")
    print(f"  sheet  {report}")
    print(f"  plan   {plan_path}")
    print("\nOPEN THE CONTACT SHEET. Nothing is cleared until you have looked")
    print("at the frames. Then:")
    print(f"  python stock_prep.py cut \"{plan_path}\"")


def write_sheet(plan, path):
    css = """
    body{background:#14161a;color:#e9edf2;font:14px/1.5 system-ui,Segoe UI,sans-serif;margin:0;padding:28px}
    h1{font-size:20px;margin:0 0 4px} .sub{color:#93a1b0;margin-bottom:22px;font-size:13px}
    .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}
    .c{background:#1c2027;border:1px solid #2b313a;border-radius:9px;overflow:hidden}
    .c img{width:100%;display:block;background:#000}
    .b{padding:9px 11px}
    .id{font-weight:600;font-size:13px}
    .m{color:#93a1b0;font-size:12px;margin-top:3px;font-variant-numeric:tabular-nums}
    .t{display:inline-block;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:600;margin-top:6px}
    .candidate{background:#123524;color:#57d98a} .review{background:#3a3213;color:#e3c04a}
    .rejected{background:#3a1a1a;color:#e08585}
    .r{color:#e08585;font-size:12px;margin-top:5px}
    .h{color:#7fb4e0;font-size:12px;margin-top:6px;border-top:1px solid #2b313a;padding-top:6px}
    .h em{color:#93a1b0;font-style:normal;font-size:11px}
    """
    p = [f"<!doctype html><meta charset=utf-8><title>Stock contact sheet</title><style>{css}</style>",
         "<h1>Stock contact sheet</h1>",
         f"<div class=sub>{html.escape(plan['planned_at'])} &middot; "
         f"{len(plan['clips'])} windows &middot; look at every frame before cutting</div>",
         "<div class=grid>"]
    order = {"candidate": 0, "review": 1, "rejected": 2}
    for c in sorted(plan["clips"], key=lambda x: (order.get(x["status"], 9), x["id"])):
        m = c["measured"]
        p.append("<div class=c>")
        if c.get("thumb"):
            p.append(f"<img src='{c['thumb']}'>")
        p.append("<div class=b>")
        p.append(f"<div class=id>{html.escape(c['id'])}</div>")
        p.append(f"<div class=m>{c['start_tc']} &middot; {c['dur']:.1f}s &middot; "
                 f"{c['width']}x{c['height']}"
                 + (f" &middot; rot {c['rotation']}" if c['rotation'] else "") + "</div>")
        p.append(f"<div class=m>hi {m['clip_hi']:.2f}% &middot; lo {m['clip_lo']:.2f}% "
                 f"&middot; sharp {m['sharp']:.2f}</div>")
        p.append(f"<div class='t {c['status']}'>{c['status'].upper()}</div>")
        if c.get("species_heard"):
            p.append("<div class=h>heard: "
                     + html.escape(", ".join(c["species_heard"]))
                     + "<br><em>audio only - confirm against the frame</em></div>")
        if c["reasons"]:
            p.append(f"<div class=r>{html.escape(', '.join(c['reasons']))}</div>")
        p.append("</div></div>")
    p.append("</div>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(p))


# ---- cut -------------------------------------------------------------------
def build_cut_cmd(c, dest, achan=1, h264=False, hwaccel=None):
    """Full re-encode to a delivery master. Rotation-aware by construction."""
    cmd = ["ffmpeg", "-y", "-v", "error", "-nostats"]
    if hwaccel:
        cmd += ["-hwaccel", hwaccel]
    # -noautorotate BEFORE -i, then undo the rotation ourselves. Letting ffmpeg
    # autorotate and also applying a filter is what produces sideways output.
    cmd += ["-noautorotate", "-ss", f"{c['start']:.3f}", "-i", c["source"],
            "-t", f"{c['dur']:.3f}"]
    vf = []
    tp = transpose_for(c.get("rotation", 0))
    if tp:
        vf.append(tp)
    if vf:
        cmd += ["-vf", ",".join(vf)]
    if h264:
        cmd += ["-c:v", "libx264", "-preset", "slow", "-crf", "14",
                "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le"]
    # One channel, never a sum. Agencies take mono natural sound happily.
    if c.get("naudio", 0) > 0:
        idx = max(0, achan - 1)
        cmd += ["-map", "0:v:0", "-map", f"0:a:{idx}?",
                "-c:a", "pcm_s16le", "-ac", "1"]
    else:
        cmd += ["-map", "0:v:0", "-an"]
    cmd += ["-map_metadata", "-1", dest]
    return cmd


def do_cut(a):
    with open(a.plan, encoding="utf-8") as fh:
        plan = json.load(fh)
    outdir = a.outdir or os.path.join(plan["outdir"], "masters")
    os.makedirs(outdir, exist_ok=True)

    want = {"candidate"} if not a.include_review else {"candidate", "review"}
    todo = [c for c in plan["clips"] if c["status"] in want]
    if not todo:
        print("Nothing to cut. Re-run plan, or pass --include-review.")
        return
    ext = ".mp4" if a.h264 else ".mov"
    print(f"Cutting {len(todo)} masters -> {outdir}\n")

    done = 0
    for n, c in enumerate(todo, 1):
        dest = os.path.join(outdir, c["id"] + ext)
        print(f"[{n}/{len(todo)}] {c['id']}  {c['dur']:.1f}s"
              + (f"  rot {c['rotation']}" if c.get("rotation") else ""))
        r = subprocess.run(build_cut_cmd(c, dest, a.achan, a.h264, a.hwaccel))
        if r.returncode != 0 or not os.path.exists(dest):
            print(bad(f"  render failed: {c['id']}"))
            c["master"] = None
            continue
        # Verify the master really came out the orientation we intended.
        got = probe(dest)
        exp_w, exp_h = c["width"], c["height"]
        if transpose_for(c.get("rotation", 0)):
            exp_w, exp_h = exp_h, exp_w
        if (got["width"], got["height"]) != (exp_w, exp_h):
            print(bad(f"  ORIENTATION MISMATCH: got {got['width']}x{got['height']}, "
                      f"expected {exp_w}x{exp_h} - do not ship this one"))
            c["orientation_ok"] = False
        else:
            c["orientation_ok"] = True
        c["master"] = os.path.abspath(dest)
        c["master_bytes"] = got["size"]
        done += 1
        print(f"       {got['width']}x{got['height']}  {human_bytes(got['size'])}")

    with open(a.plan, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=1)
    bad_orient = [c["id"] for c in todo if c.get("orientation_ok") is False]
    print(f"\n{done}/{len(todo)} masters written to {outdir}")
    if bad_orient:
        print(bad(f"orientation mismatch on: {', '.join(bad_orient)}"))
    print(f"\nNext:\n  python stock_prep.py meta \"{a.plan}\"")


# ---- meta ------------------------------------------------------------------
BEHAVIOUR_HINTS = [
    ("feeder", "bird feeder"), ("hummer", "hummingbird"), ("fount", "water"),
    ("drink", "drinking"), ("fight", "territorial"), ("mob", "flock"),
]


def birdnet_species(wav_dir, conf, lat=-1.0, lon=-1.0):
    """Run BirdNET over a directory of wavs. Returns {stem: [(species, conf)]}.

    INPUT is positional in birdnet_analyzer 2.4; -o is the output folder.
    lat/lon of -1 disables the location filter. Passing a location that does
    not match where the footage was actually shot will SUPPRESS correct
    species, so the default is no filter - set it deliberately.
    """
    out = {}
    r = run([sys.executable, "-m", "birdnet_analyzer.analyze", wav_dir,
             "-o", wav_dir, "--min_conf", str(min(conf, 0.35)),
             "--lat", str(lat), "--lon", str(lon),
             "--rtype", "csv", "-t", "4"])
    if r.returncode != 0:
        print(warn("birdnet_analyzer failed; keywords will be species-free"))
        print(r.stderr.strip()[:400])
        return out
    for fn in os.listdir(wav_dir):
        if not fn.lower().endswith(".csv"):
            continue
        stem = re.sub(r"\.BirdNET.*$", "", fn, flags=re.I)
        stem = os.path.splitext(stem)[0]
        hits = {}
        try:
            with open(os.path.join(wav_dir, fn), encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    name = (row.get("Common name") or row.get("Common_name")
                            or row.get("common_name") or "").strip()
                    try:
                        cf = float(row.get("Confidence") or row.get("confidence") or 0)
                    except ValueError:
                        cf = 0.0
                    if name:
                        hits[name] = max(hits.get(name, 0.0), cf)
        except Exception:
            continue
        out[stem] = sorted(hits.items(), key=lambda kv: -kv[1])
    return out


def do_meta(a):
    with open(a.plan, encoding="utf-8") as fh:
        plan = json.load(fh)
    masters = [c for c in plan["clips"] if c.get("master") and os.path.exists(c["master"])]
    if not masters:
        print("No masters found. Run `cut` first.")
        return

    wav_dir = os.path.join(plan["outdir"], "_birdnet_wav")
    os.makedirs(wav_dir, exist_ok=True)
    print(f"Extracting {len(masters)} audio stems for BirdNET...")
    for c in masters:
        w = os.path.join(wav_dir, c["id"] + ".wav")
        if not os.path.exists(w):
            run(["ffmpeg", "-y", "-v", "error", "-i", c["master"],
                 "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", w])

    loc = (f"location-filtered {a.lat},{a.lon}" if a.lat != -1 and a.lon != -1
           else "no location filter")
    print(f"Running BirdNET (local, {loc})...")
    species = birdnet_species(wav_dir, a.conf, a.lat, a.lon)

    rows = []
    for c in masters:
        hits = species.get(c["id"], [])
        strong = [n for n, cf in hits if cf >= a.conf]
        weak = [f"{n} ({cf:.2f})" for n, cf in hits if cf < a.conf]
        kws = ["bird", "birds", "wildlife", "nature", "Colorado", "birdwatching",
               "backyard", "avian", "4K", "wild bird"]
        for s in strong:
            kws += [s, s.split()[-1]]
        low = c["id"].lower() + " " + os.path.basename(c["source"]).lower()
        for needle, kw in BEHAVIOUR_HINTS:
            if needle in low:
                kws.append(kw)
        seen, kw_out = set(), []
        for k in kws:
            if k.lower() not in seen:
                seen.add(k.lower())
                kw_out.append(k)

        # Deliberately generic. BirdNET heard a species; it did not SEE one.
        # The on-screen bird is filled in by a human in species_onscreen, and
        # `finalize` rewrites the title from that. See the SPECIES note in the
        # module docstring - this is the single most expensive thing to get
        # wrong on a stock account.
        title = "Wild bird at a backyard feeder in Colorado"
        desc = (f"{title}. Filmed in the Colorado Front Range with natural sound. "
                f"{c['dur']:.0f} seconds, {c['width']}x{c['height']}.")
        c["species_heard"] = strong
        c["review_species"] = weak
        c["keywords"] = kw_out
        rows.append(dict(
            filename=os.path.basename(c["master"]),
            species_onscreen="",          # <- YOU fill this in, from the frame
            title=title, description=desc,
            keywords=";".join(kw_out[:49]),
            species_heard=", ".join(strong),
            species_heard_weak=", ".join(weak),
            duration_sec=f"{c['dur']:.2f}",
            resolution=f"{c['width']}x{c['height']}", fps=f"{c['fps']:g}",
            editorial="no", released="not required - wildlife, no property",
        ))

    csv_path = os.path.join(plan["outdir"], "stock_upload.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    with open(a.plan, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=1)
    # Redraw the sheet so the frame and what was heard sit side by side - that
    # is the whole review surface for filling in species_onscreen.
    write_sheet(plan, os.path.join(plan["outdir"], "stock_contact_sheet.html"))

    heard = sum(1 for r in rows if r["species_heard"])
    print(f"\nWrote {csv_path}  ({len(rows)} clips)")
    print(f"  {heard} have an audible species above {a.conf} - as a LEAD only")
    print(f"""
{'='*66}
BirdNET tells you what was AUDIBLE, not what is ON SCREEN. In this yard
the Broad-tailed Hummingbird is on virtually every recording, so it will
be 'heard' over footage of a jay, a nuthatch, or an empty feeder.

Stock buyers search for what they SEE. So:

  1. open  stock_contact_sheet.html  next to the CSV
  2. for each row, look at the frame and type the bird you actually see
     into the  species_onscreen  column (leave blank if unsure)
  3. python stock_prep.py finalize "{csv_path}"

finalize rewrites titles and keywords from species_onscreen. Rows left
blank ship as generic 'wild bird' - which sells for less, but never gets
an account flagged for mislabelling.
{'='*66}""")


def do_finalize(a):
    """Rewrite titles/keywords from the species_onscreen column a human filled."""
    with open(a.csv, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        print("Empty CSV.")
        return
    if "species_onscreen" not in rows[0]:
        print(bad("no species_onscreen column - is this a stock_upload.csv?"))
        return

    named = 0
    for r in rows:
        seen = (r.get("species_onscreen") or "").strip()
        if not seen:
            continue
        named += 1
        r["title"] = f"{seen} at a backyard feeder in Colorado"
        r["description"] = (
            f"{seen} filmed at a backyard feeder in the Colorado Front Range "
            f"with natural sound. {float(r['duration_sec']):.0f} seconds, "
            f"{r['resolution']}.")
        kws = [k for k in r["keywords"].split(";") if k]
        # Put the confirmed on-screen species first; agencies weight early
        # keywords most heavily, and drop anything only heard, never seen.
        # Drop the heard-only species AND its bare last word - `meta` adds both
        # ("Broad-tailed Hummingbird" and "Hummingbird"), and leaving the short
        # token behind is how a scrub-jay clip ships tagged 'Hummingbird'.
        heard = set()
        for h in (r.get("species_heard") or "").split(","):
            h = h.strip()
            if h:
                heard.add(h.lower())
                heard.add(h.split()[-1].lower())
        kws = [k for k in kws if k.lower() not in heard]
        lead = [seen, seen.split()[-1]]
        seen_set, out = set(), []
        for k in lead + kws:
            if k and k.lower() not in seen_set:
                seen_set.add(k.lower())
                out.append(k)
        r["keywords"] = ";".join(out[:49])

    dest = os.path.splitext(a.csv)[0] + "_final.csv"
    with open(dest, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {dest}")
    print(f"  {named}/{len(rows)} carry a confirmed on-screen species")
    print(f"  {len(rows)-named} ship as generic 'wild bird'")
    if named < len(rows):
        print(warn("generic rows sell for less - worth a second pass later"))


def main():
    ap = argparse.ArgumentParser(
        description="Turn swept bird footage into licensable stock clips.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="measure sweep events and pick stock windows")
    p.add_argument("plan", help="sweep_plan.json from bird_sweep.py")
    p.add_argument("--outdir")
    p.add_argument("--conf", type=float, default=SPECIES_CONF)
    p.add_argument("--hwaccel")
    p.add_argument("--no-thumbs", action="store_true")
    p.set_defaults(func=do_plan)

    c = sub.add_parser("cut", help="render unbranded delivery masters")
    c.add_argument("plan", help="stock_plan.json")
    c.add_argument("--outdir")
    c.add_argument("--achan", type=int, default=1, help="1-based audio stream")
    c.add_argument("--h264", action="store_true", help="x264 CRF14 instead of ProRes")
    c.add_argument("--include-review", action="store_true")
    c.add_argument("--hwaccel")
    c.set_defaults(func=do_cut)

    m = sub.add_parser("meta", help="BirdNET keywording -> stock_upload.csv")
    m.add_argument("plan", help="stock_plan.json")
    m.add_argument("--conf", type=float, default=SPECIES_CONF)
    m.add_argument("--lat", type=float, default=-1.0,
                   help="recording latitude; -1 disables the location filter")
    m.add_argument("--lon", type=float, default=-1.0,
                   help="recording longitude; -1 disables the location filter")
    m.set_defaults(func=do_meta)

    z = sub.add_parser("finalize",
                       help="rewrite titles/keywords from species_onscreen")
    z.add_argument("csv", help="stock_upload.csv, after you filled species_onscreen")
    z.set_defaults(func=do_finalize)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
