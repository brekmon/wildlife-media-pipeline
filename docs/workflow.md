# Video Project Workflow

Distilled from the a large multi-camera trip build build. The order matters — several
steps here exist because doing them late cost real time.

---

## 0. Before anything: establish the truth about the media

**Find the master copy and prove it.** Duplicate folders are normal. On the
trip, `F:\Trip2024\MP4` looked like the source but was a *partial* copy —
77 byte-identical clips missing 6 that only existed on D:. Hash-compare, don't
assume.

**Video `creation_time` is UTC. Stills EXIF is local.** Always convert before
building a timeline. Raw values put 61 trip clips at "22:00" and none between
03:00–10:00 — an impossible shooting pattern, and the tell that something's
wrong. This single correction moved 13 clips to different days and changed two
structural conclusions.

**Apply `exif_transpose` to every still before measuring anything.** Reading raw
pixel dimensions undercounted portrait images as 79 when the true figure was 160
of 462. Uncorrected, sideways images ship.

**Check `rotation` on every clip.** `rot=-90` clips need `-noautorotate` plus a
transpose under `filter_complex`. **Verify the direction by rendering a frame** —
`transpose=2` produced upside-down output on this camera; `transpose=1` was
correct. Contact sheets auto-rotate, so they look right while the render is
wrong. Only playback catches it.

---

## 1. Inventory and screen

- ffprobe everything: duration, resolution, fps, codec, rotation, audio streams
- Contact-sheet every clip; **look at all of them**
- Write a per-clip log: content, rating, best in/out seconds
- **Sampled frames are triage, not judgment.** A clip was rejected as "out of
  focus" from one frame that happened to land on a blur; other frames were fine.

**Ratio check before committing to a length.** Highlights normally select from
10× the finished runtime. Under about 3× there is no selectivity left and the
cut will be padded. Say so before agreeing a target.

---

## 2. Stills: technical cull, then human selection

**This split is the important lesson.** Machine-cull on measurable criteria —
sharpness, exposure, faces intact, resolution floor, near-duplicate grouping.
Then **the human picks which photos matter.** A technically clean cull kept
plenty of frames that were sharp and emotionally inert; 23 chosen photos beat
113 well-measured ones.

- Resolution floor for 4K: **2560 px effective width** after the 16:9 crop
- Group near-duplicates and keep one; bursts are where the volume hides
- Never delete — sort into Reject/Review/Keep/Top

---

## 3. Conform

- One frame rate, one resolution, one pixel format
- **Lock each clip's audio to its video length.** Per-clip drift of 10–47 ms
  accumulates through a concat demuxer into ~1 s of lip-sync error by the end
  of a 72-item timeline. Snap audio to whole video frames.
- Fill-frame rule: hard-crop where the subject survives, blurred fill where it
  doesn't. **Cropping off a head is a hard failure**; a blurred band is a soft
  one. Keep a named exception list for shots that matter more than the rule.

---

## 4. Audio

Run `audio_gate.py` first for a baseline, then treat.

- **Separation is the wrong headline metric for non-wildlife material.** It
  assumes a sparse transient against constant noise. In a travel film the
  ambience *is* the subject and low separation means nothing.
- **Floor spread is the real problem** — it's what makes hum lurch at cuts.
- Denoise with a **measured profile from each clip's own quiet frames**, and
  **only process clips above target.** Subtracting from already-quiet clips
  drags the whole set down and leaves the spread unchanged.
- Use a **closed loop**: search the subtraction strength until the floor lands
  on target. Open-loop scaling overshot by 13 dB.
- Check musical noise (frame-to-frame variability in quiet passages); under
  1.3× is inaudible.

---

## 5. Colour

**Correct only in the direction of the fault.** The first pass pulled everything
toward a per-day mean and broke two shots: a night exterior was lifted into grey
murk, and correctly-cool daylight was warmed into a pink cast.

- Only pull **down** clips that are too bright; leave genuinely dark shots alone
- Only pull **back** clips that are too warm; never add warmth to cool ones
- Only **reduce** saturation, never boost toward a target
- Partial pulls (~55%), not absolute targets — sunset should stay warm

**Look at frames before and after.** Both regressions were invisible in the
numbers and obvious in the picture.

---

## 6. Master

- **`alimiter` BOOSTS unless you pass `level=disabled`** — its makeup gain is on
  by default, so adding a limiter to control true peak raises it instead
- **Measure true peak on the ENCODED file.** AAC adds several dB of inter-sample
  overshoot; a mix limited to −2.1 dBFS measured +2.9 dBTP after encoding
- `ebur128` prints two `Peak:` lines — parse the **True peak** block
- **Encode → measure → adjust, and keep the closest PASSING candidate.** A loop
  that keeps the *last* attempt discards a good answer it already found
- Target −14 LUFS / ≤ −1 dBTP; every output needs its own headroom solve

---

## 7. Deliver and clean up

- Render a **1080p review copy** — 4K at 30–95 Mbps drops frames on playback
  and reads as "the edit is jumpy" when the file is fine
- Distribute the 4K master to every Finished Videos folder, **hash-verified**
- Then `project_cleanup.py --tier 2 --apply` (~25 GB back on a project this size)

---

## Machine notes

- **No NVENC** (driver too old) — libx264 only
- **Throttle to `cores - 3`.** Unlimited threads on the 5950X makes the mouse
  stutter. Every long render should pass `-threads`.
- Windows paths break ffmpeg filter parsing on colons — `cd` and use bare names
- Python writes CRLF; use `newline="\n"` for any file ffmpeg reads

---

## The pattern behind most of the mistakes

Every significant error took the same shape: **measure correctly, then apply a
symmetric correction to an asymmetric problem.**

- Audio: minimised every floor instead of raising the low and lowering the high
- Colour: pulled every clip toward a mean instead of correcting only the faults
- Loudness: chased a target without checking the direction of the miss

The fix each time was the same — decide *which way* each item is wrong before
correcting it, and verify by looking, not only by measuring.
