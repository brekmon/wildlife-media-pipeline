# wildlife-media-pipeline

[![CI](https://github.com/brekmon/wildlife-media-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/brekmon/wildlife-media-pipeline/actions/workflows/ci.yml)

Python tooling that turns a multi-terabyte archive of locked-off wildlife
footage into finished, measured, standards-compliant deliverables, with the
dead air removed and the species already identified.

Built and run in production by a one-person studio. Every tool here is used on
real footage, not written for demonstration.

---

## The design rule

> **A check only counts if a tool produced the number, or a human looked at the
> frame.**

Everything in this repository follows from that. Where a value can be measured,
it is measured and printed. Where it cannot, the tool says so by name and refuses
to imply it passed. Nothing is asserted quietly.

That rule exists because the failure mode in an automated media pipeline is not
a crash, it is a build that reports success while shipping something wrong.

---

## The problem

A locked-off camera pointed at a feeder produces hours of footage in which
almost nothing happens, at 4K, with no metadata of any kind. Three jobs follow
from that, and all three are miserable by hand:

1. **Find the seconds that matter** in hours of near-identical frames.
2. **Know what is in them.** An unlabelled archive is unsearchable, and an
   unsearchable archive may as well not exist.
3. **Prove a deliverable meets spec** before it ships, every time, not when
   somebody remembers to check.

---

## The tools

### `bird_sweep.py` — find activity, cut the dead air
Two passes, always in this order.

**SCAN** reads the footage, finds every stretch where something is at the
feeder, and writes a plan (JSON) plus a self-contained HTML report with
thumbnails. Nothing is written to the footage.

**CUT** reads the plan and produces one condensed file per source clip.
Lossless stream copy: no re-encode, no generation loss, originals never
modified or deleted.

```bash
python bird_sweep.py scan "D:\Footage\Raw\*.MP4" --outdir "D:\sweep"
python bird_sweep.py cut  "D:\sweep\sweep_plan.json"
```

Sensitivity presets (`conservative` / `balanced` / `aggressive`) and an ROI mask
to ignore wind-blown branches at the edge of frame, which is the single largest
source of false positives. `--hwaccel cuda` for NVIDIA decode on 4K.

### `sweep_verify.py` — check what the detector threw away
The scan report shows what was **kept**, and a detector that keeps too much
looks perfect there. That is the wrong direction to check.

For a footage archive the only unrecoverable failure is a subject inside a
stretch that got dropped. So this samples frames from the **dropped time only**
and tiles them with timecodes. If you can see a bird in the sheet, the settings
are too aggressive and the plan must not be cut.

Verifying the negative case is the difference between a tool you trust and a
tool that quietly loses your best shot.

### `release_gate.py` — nothing ships unmeasured
Run against a folder of finished files before anything is published.

| Check | Target |
|---|---|
| Integrated loudness | -14 LUFS, tolerance 0.5 |
| True peak | at or below -1 dBTP |
| Burned-in text | above 0.75 of frame height, inside 0.02-0.92 width |
| Highlight clipping | under 3% of pixels at 254+ |

It also prints, by name, every check a machine **cannot** make, so a human gate
cannot be skipped by assuming it passed.

### `audio_gate.py` — one number for "does the subject stand out"
```
separation = (99th percentile short-time level) - (40th percentile), above 1.5 kHz
```
The 99th percentile is the calls and pecks. The 40th is the road hum between
them. The difference is what the ear actually judges.

This replaced an earlier metric that averaged energy in a "bird band" across the
clip, which is dominated by constant hum because the subject only makes sound
occasionally. That metric reported denoising as destroying the bird when it was
doing the opposite. The docstring keeps the postmortem, because the wrong metric
was more expensive than the bug.

### `stock_prep.py` — the metadata is the product
Selects licensable clips against measured criteria and generates the keywording
from neural audio classification output.

| Criterion | Threshold | Why |
|---|---|---|
| Duration | 5-20s | buyers cut to 5-10s; under 5 is unsellable |
| Highlights | under 3% at 254+ | blown sky reads as an instant rejection |
| Shadows | under 5% at 2- | crushed blacks read as cheap |
| Sharpness | reported | outliers flagged for review, never auto-rejected |

Stock agencies pay for clips that are clean **and findable**. Most people get
clean roughly right and skip findable, because keywording hundreds of clips by
hand is miserable. Audio classification already knows which species is audible
in every second, so the keywording is nearly free.

A clip is only cleared after a human looks at the contact sheet. The script
picks candidates and measures them. It does not decide that footage is good.

---

## Two principles worth stealing

**Plan, then apply.** Every destructive operation is split in two. The first
pass measures and writes a reviewable plan and an HTML report; it touches
nothing. The second pass applies that plan. You always see what will happen
before it happens, and the originals are never the working copy.

**Verify the negative.** Checking that the output looks right only catches
errors of commission. The expensive errors are omissions: the shot that was
silently dropped, the segment that vanished from a transition chain, the tape
that stopped scanning eight minutes in. Every check here that matters looks at
what was discarded, not at what survived.

---

## Requirements

Python 3.8+, `numpy`, `Pillow`, and `ffmpeg` / `ffprobe` on `PATH`.
NVENC and CUDA are optional and used when present.

---

## Tests

```
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

127 tests, run on every push against Python 3.10 through 3.13. No ffmpeg is
needed: every test targets pure logic, because that is where the silent errors
live. The subprocess layer is exercised by running the tools on real footage,
which CI cannot honestly do.

The weight sits on four things:

- **What the cut throws away.** `dropped_ranges` computes the complement of the
  kept events, and it is the whole safety net. Checking a detector's output only
  catches errors of commission — a page of hits looks perfect even when the
  settings are far too aggressive, because it cannot show you the bird that was
  dropped. Tests cover overlapping detections, unsorted input, events running
  past the container's stated duration, and the case that matters most: no
  detections at all must report the entire file as discarded, not an empty list
  that reads like "all clear".
- **Rotation.** The pipeline decodes with `-noautorotate`, so the transpose
  filter is the only thing between a vertically shot clip and a sideways
  delivery. Each angle is pinned, including that 90 and 270 are not
  interchangeable and that an unexpected angle returns nothing rather than
  guessing at the nearest right angle.
- **Delivery thresholds.** -14 LUFS, -1 dBTP, and the Shorts safe zone measured
  by alpha bounding box rather than by trusting the coordinates the renderer was
  given. Resolution independence is asserted, so the verdict cannot depend on
  the export preset.
- **What git is allowed to track.** No media, no run output, no `.private-terms`,
  and no absolute home-directory path left in a default argument, which
  would name the machine's user.

The suite is also run a second time under a different `PYTHONHASHSEED`. Set
iteration order is randomised per process, and a tool that answers the same
question differently on different runs is not verifiable.

---

## What is not in this repository

The footage, the finished films, and the client work. Also a second agent skill
covering personal family-archive editing, which is kept private because it names
real people including children. Judgement about what not to publish is part of
the job.

## Licence

MIT. See `LICENSE`.
