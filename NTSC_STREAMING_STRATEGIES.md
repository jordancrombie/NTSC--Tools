# Unbounded output on the ADP2230 — strategy options

Research notes, not a plan of action. Written after `PlayData` streaming was
measured to lose a 32-sample block per 32,768-sample FIFO revolution
(0.0976%, deterministic, phase-locked to a ring address we cannot control —
see **NTSC_FINDINGS.md**).

Nothing here is implemented. Confidence labels are deliberate.

---

## What the measurements constrain

| | measured | source |
|---|---|---|
| `FDwfAnalogOutNodePlayData` | ~1077 µs **flat**, 1 Ki to 32 Ki samples | `--transport` |
| `FDwfAnalogOutStatus` | 1393 µs, the only real device round trip | `--transport` |
| `FDwfAnalogOutNodePlayStatus` | 0.7 µs — local read of cached state | `--transport` |
| `FDwfAnalogOutNodeDataSet` | **458 MB in 0.7 s ≈ 654 MB/s ≈ 82 MS/s** | `--motion` upload |
| streaming FIFO | 32,768 samples, fixed | `--memory-probe` |

The decisive asymmetry: **`PlayData` is latency-bound and `DataSet` is not.**
`DataSet` moves data 5.7× faster than the colour rate needs. Every strategy
below is really a question of whether content can be swapped through `DataSet`
instead of trickled through `PlayData`.

---

## A. Double-buffered `DataSet` + `FDwfAnalogOutConfigure(ch, 3)`

**The strongest candidate.** The manual documents fStart = 3:

> *"Value 3 will apply the configuration dynamically without changing the
> state of the instrument. … fStart: 0 stop, 1 start, 3 apply."*

and `samples/py/AnalogOut_DualBuffer.py` demonstrates it: set data, `Configure(ch, 1)`
to start, then later set **new** data and `Configure(ch, 3)` to apply it while
still running.

If that works with `funcPlay` on a deep-memory channel, we keep the path that
already produces a byte-perfect picture and simply swap the content at loop
boundaries. No `PlayData`, no FIFO, therefore no wrap loss.

- **Bandwidth:** fine. A 953,400-sample sequence is 7.6 MB; at 654 MB/s that
  is ~12 ms against a 66.7 ms playout. A 140-sequence buffer is 9.34 s of
  playout against ~1.6 s of upload.
- **Unknown:** whether `Configure(3)` applies a new `NodeDataSet` to a running
  `funcPlay` channel at all, and whether the swap is sample-seamless.
- **Known risk, documented on the AD3 — same generation:** the *first*
  `Configure(ch, 3)` after a channel start can briefly pause the output, with
  stochastic discontinuities in roughly 20–50% of events, and apply can send
  the output to zero instead of applying. The reported workaround is a
  sacrificial pair of applies (nudge frequency by +0.01 Hz and back) before
  the real change.
- **Cost to evaluate:** one run. Load sequence A, start, `DataSet` sequence B,
  `Configure(3)`, and capture across the boundary with `--stream-square`,
  which already resolves a single lost or duplicated sample.

**Confidence the mechanism exists: high** (manual + shipped example).
**Confidence it is seamless here: unknown** — that is the whole question.

## B. `funcDualCustom` / `funcDualPattern` explicit double buffer

The documented double-buffer functions (26 and 27). `AnalogOut_DualBuffer.py`
halves `DataInfo` max and treats each half as a buffer.

- **Likely blocker:** `DataInfo` max measured 32,768 in every configuration,
  so each buffer would be 16,384 samples — 1.15 ms, about 18 lines at
  14.286 MS/s. Useless for video *unless* `DataInfo` under `funcDualCustom`
  reports larger on a deep-memory device, the way `PlayInfo` does for
  `funcPlay` (134 M vs `DataInfo`'s 32,768).
- **Cost to evaluate:** trivial — set the function, read `DataInfo`. Worth
  doing in the same session as A.

**Confidence: low**, but the check is nearly free and it would be decisive.

## C. Channel ping-pong with master / trigger

Three AWG channels. Play A on one, preload the next, hand over on trigger,
alternate. `FDwfAnalogOutMasterSet`, `FDwfAnalogOutTriggerSourceSet`,
`FDwfAnalogOutRepeatTriggerSet` and `FDwfAnalogOutIdleSet` are all present.

- **Blocker:** the channels are separate pins. Combining them needs an
  external passive sum or an analog switch, and the idle channel must sit at a
  defined level (`FDwfAnalogOutIdleSet` offers Disable / Offset / Initial).
- The hand-off must be sample-exact or the seam is a visible defect, and
  nothing suggests the trigger path is that precise.
- **Cost:** hardware build plus a lot of timing work.

**Confidence: low.** Listed for completeness; I would not start here.

## D. Accept the loss, pre-compensate the rate, and pin its position

The loss is equivalent to the output running 0.0976% fast. Generating content
0.0976% longer would correct the *average* line rate.

- **What it does not fix:** the loss is a 32-sample (2.24 µs) jump at one
  point, not a uniform stretch, so one line in ~36 is displaced. That is the
  "twisting" rather than the rolling.
- **Possible refinement:** if the content period were an exact multiple of
  32,768, the loss would always land at the same content offset — always the
  same line, every frame. A monitor may tolerate one permanently displaced
  line far better than a walking one, especially if it can be placed in the
  vertical interval. Requires a sequence length that is simultaneously a
  multiple of 32,768, a whole number of four-field NTSC sequences, and closed
  on the subcarrier. Not obviously solvable.

**Confidence of a *perfect* result: nil. Of a usable one: medium.** Real
design work for an imperfect outcome.

## E. Report it to Digilent

Independent of everything above, and free.

Two defects, both reproducible in about twenty seconds with
`--stream-doc --stream-square 100`:

1. One 32-sample block lost per FIFO revolution under `funcPlayPattern`
   streaming, deterministic.
2. **`cdDataLost` and `cdDataCorrupted` report zero throughout.** Arguably the
   more serious of the two: the documented way to validate a stream cannot
   detect this, which is what made it cost so many hardware cycles here.

A firmware fix would make streaming work properly, which no host-side
workaround can.

**Do this regardless of which strategy is chosen.**

## F. `DigitalOut` play with an external DAC

The digital side has `FDwfDigitalOutPlayDataSet` **and
`FDwfDigitalOutPlayUpdateSet`** — in-place update of a playing buffer. The
analog side has no equivalent, which is notable: the digital path explicitly
supports unbounded content.

- 16 DIO at up to 100 MHz; an 8-bit R-2R ladder gives adequate video
  resolution.
- **Cost:** hardware build, and the project already explored and removed a
  resistor-ladder path once. Analog quality of a ladder on DIO pins is
  unproven for composite video.

**Confidence the API supports it: high. That it yields broadcast-quality
composite: unproven.** A fallback if A fails and unbounded output is genuinely
required.

## G. Keep the 9.34 s loop

`--motion` already produces a byte-perfect, hardware-timed, seamless loop with
no host involvement. For a bench test-pattern generator that is a reasonable
deliverable; 9.34 s of non-repeating motion is longer than most test sequences.

**Cost: zero. Already working.**

---

## My view

Pursue **A**, with **B** as a free side-check in the same session, and do **E**
regardless. A is the only option that plausibly gives unbounded, defect-free
output without hardware work, and it rests on a documented API plus a shipped
example rather than on inference. The AD3 apply-glitch reports are a genuine
risk, but they describe a *transient at the first apply* with a known
workaround, not a steady-state defect.

If A proves not to be seamless, I would stop and keep **G** rather than move to
C, D or F. The deliverable is a test-signal generator; none of those three buys
enough to justify its cost, and D cannot be made perfect even in principle.

One caution on method, given how this investigation went: A should be validated
with `--stream-square` against the `funcSquare` hardware control **before** any
video judgement, and the boundary between buffers should be captured
explicitly. The failure mode to look for is a one-off discontinuity at each
swap, which video symptoms would not distinguish from the problems already
diagnosed.

---

## Sources

- WaveForms SDK Reference Manual (`FDwfAnalogOutConfigure`, fStart 0/1/3;
  `FDwfDeviceOpenEx` options; `FDwfAnalogOutNodePlayData` loop order;
  `cdDataLost` semantics) — on disk at
  `/Applications/WaveForms.app/Contents/Resources/SDK/WaveForms SDK Reference Manual.pdf`
- `samples/py/AnalogOut_DualBuffer.py`, `AnalogOut_Play.py`,
  `AnalogOut_PlayTest.py`
- <https://digilent.com/reference/software/waveforms/waveforms-sdk/reference-manual>
- <https://forum.digilent.com/topic/33982-ad3-first-fdwfanalogoutconfigurech-3-glitch-observed-after-channel-start>
- <https://forum.digilent.com/topic/22260-applying-analogoutconfigure-dynamically-fstart3-for-master-slave-configured-channels-in-analog-discovery-studio/>
- <https://forum.digilent.com/topic/32912-play-with-discovery-3-being-sure-no-data-is-lost/>
