# bird_sweep — cut the dead air out of feeder footage

> ## STOP — DO NOT USE ON HUMMINGBIRD FOOTAGE (tested 21 Aug 2026)
>
> Two failure modes were measured on real clips today. Both are documented in
> full further down under **Known limitations**. In short:
>
> - **Before the fix**, the detector latched and kept **100% of four clips** —
>   useless, but safe.
> - **After the fix**, it drops ~20% — and `sweep_verify.py` showed a
>   hummingbird in **almost every dropped frame**. That is the dangerous
>   direction: it deletes footage that cannot be re-shot.
>
> **Never run `cut` on a plan you have not checked with `sweep_verify.py`.**
> That tool is what caught this, and it caught it instantly.


Finds the stretches of a locked-off feeder recording where a bird is actually
present, and produces one condensed file per source clip with the empty time
removed. **Lossless** — it stream-copies the original frames, no re-encode, no
generation loss. Your originals are never modified or deleted.

---

## One-time setup (Windows)

```powershell
winget install Gyan.FFmpeg
winget install Python.Python.3.12
pip install numpy
```

Close and reopen the terminal afterwards so `ffmpeg` is on PATH. Check with:

```powershell
ffmpeg -version
```

---

## Using it

Two passes. Always scan first, look at the report, then cut.

**1 — Scan** (reads only, writes nothing but a report):

```powershell
cd "$env:USERPROFILE\Desktop\Colorado Native Birds"
python Tools\bird_sweep.py scan "C0556.MP4" --outdir "Tools\sweep" --hwaccel cuda
```

Open `Tools\sweep\sweep_report.html`. Every detected visit has a thumbnail, a
timecode, and the projected space saving.

**2 — Cut**, once the report looks right:

```powershell
python Tools\bird_sweep.py cut "Tools\sweep\sweep_plan.json"
```

Condensed files land in `Tools\sweep\condensed\`.

Whole folders work too:

```powershell
python Tools\bird_sweep.py scan "Special Raw Footage\*.MP4" --outdir "Tools\sweep"
```

---

## The one flag worth learning: `--roi`

Restrict detection to a box around the feeder, given as percentages of the
frame — `x,y,width,height` measured from the top-left:

```powershell
python Tools\bird_sweep.py scan "C0556.MP4" --roi 30,20,45,55
```

This is the single biggest lever on accuracy. Wind-moving branches at the edge
of frame are the main thing that makes a sweep keep footage it shouldn't. The
script already learns and ignores restless areas automatically, but an explicit
ROI is more reliable than any heuristic.

---

## Presets

```
--preset conservative    keeps more, 5s padding, merges visits <30s apart
--preset balanced        default: 3s padding, merges visits <15s apart
--preset aggressive      keeps least, 2s padding, no merging
```

Individual knobs — `--pix`, `--area`, `--pre`, `--post`, `--merge` — override the
preset if you want to tune.

If a visit you know about is missing from the report, go one preset softer or
lower `--area`. If it kept a lot of empty perch, go one harder.

---

## What it does about the hard cases

- **A bird that lands and sits perfectly still.** Frame-to-frame motion detection
  loses this — the bird stops moving and the event ends while it's still sitting
  there. This compares each frame against a reference background built only from
  idle frames, so presence keeps registering, not just movement.
- **Clouds crossing the sun.** Brightness and contrast are matched between frame
  and reference before comparing, so a light change doesn't read as a bird.
  Important at 7,000 ft where the light swings hard.
- **Wind in the branches.** The first 60 seconds are used to learn which parts of
  the frame are never still; those are excluded afterward. The discriminator is
  how *often* a pixel changes, not how hard — a branch changes most of the time,
  a bird only occasionally.
- **The first 60 seconds are always kept in full,** because detection isn't
  trustworthy while it's still calibrating. Costs a few hundred MB, guarantees
  nothing gets dropped in the window where the script is least sure.
- **Noise floor** is measured per clip, so a grainy ISO-6400 dusk session and a
  clean morning both work without retuning.

---

## Safety

- Originals are opened read-only. The script has no delete path at all.
- **Watch the condensed file all the way through before you delete anything.**
  It's a fast watch — that's the point of it.
- Keep the `.XML` sidecars (`C0556M01.XML`) with the originals if you archive
  them; they hold the camera metadata.
- `sweep_events.csv` maps every kept segment back to its timecode in the original,
  so even after you condense, you know where each piece came from.

## A note on what you're trading away

Concatenating visits into one file per source is the most space-efficient option
and it's what you asked for, but be aware of what it costs: the timecode
relationship to the original is gone, and visits that happened an hour apart now
butt against each other with a hard cut and an audio discontinuity. The CSV is
your map back. If you later find you want individual clips per visit instead,
that's a small change to the cut pass — say the word.


---

## Known limitations (measured 21 Aug 2026)

### The detector cannot see hummingbirds

Detection runs at **320x180**. A hummingbird at a feeder is three to five pixels
there — and its contribution to the whole-frame changed-pixel fraction is
**smaller than the clip's own baseline variation** from drifting cloud, shifting
light and compression noise.

Measured on `Ant Moat.mp4`, 300s sample:

| | changed-pixel fraction |
|---|---|
| minimum over 300s | 0.00158 |
| median | 0.01842 |
| p90 | 0.03082 |
| conservative preset trigger | **0.00100** |

The trigger sits *below the observed minimum*, so every frame tripped it.

### The floor could never adapt (fixed)

`trigger = max(area, floor x 3)`, but the floor was learned **only from frames
judged idle**. With the trigger below the noise, no frame was ever idle, the
floor stayed 0.0 forever, and the mechanism meant to correct over-triggering was
dead on arrival. Fixed by seeding the floor from the calibration window at the
25th percentile (`FLOOR_SEED_PCT`).

**That fix works as designed** — floor went 0.0 -> 0.0417, events 1 -> 4, 20%
dropped. **But the threshold it produces is now above the hummingbird signal**,
so the dropped 20% is full of birds. The fix corrected the latch and exposed the
real problem underneath.

### What would actually solve it

Whole-frame changed-pixel fraction is the wrong statistic for a small subject.
In rough order of expected payoff:

1. **Connected-component (blob) detection** instead of a global fraction. A bird
   is a compact blob; a light shift is diffuse. This is the principled fix.
2. **`--roi` cropped tight to the feeders.** A bird occupies a far larger
   fraction of a small ROI, and the README already calls this the single best
   lever. Cheapest thing to try.
3. **Higher `DET_W`/`DET_H`.** Signal scales with subject area while the
   baseline does not, so 640x360 should improve separation ~4x.

### Where it may still be useful untested

Larger birds — jays, blackbirds, woodpeckers — occupy far more pixels and may
separate cleanly. **Not verified.** Run `sweep_verify.py` and look before
trusting any of it.
