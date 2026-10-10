# NTSC Tools

An NTSC composite video generator and analyser for the **Digilent Analog
Discovery Pro 2230**, built on the WaveForms SDK.

It generates a standards-shaped NTSC signal on AWG channel 1 and, with a
`W1 -> CH1` jumper, measures what actually comes out using the device's own
oscilloscope. It can also profile a reference signal from a broadcast
generator and diff the two parameter by parameter — which is how most of the
real problems in this project were found.

Verified against a **Leader LV5700A** waveform monitor / vectorscope and a CRT.

---

## Quick start

```bash
# 75% EIA colour bars, 525-line interlaced — the good path
python3 ntsc.py --deep --color --play-scale 1.0 --locked

# a bouncing ball over the bars, looped from deep memory
python3 ntsc.py --deep --color --play-scale 1.0 --locked --motion

# monochrome bars, no setup needed
python3 ntsc.py

# build and validate a frame offline, no hardware
python3 ntsc.py --dump

python3 ntsc.py --help
```

**Wiring:** `W1` to the composite centre conductor, `GND` to the shield. For
measurement, jumper `W1` to `CH1` as well — the scope and AWG run
concurrently.

A **75 Ω resistor in series with W1** (plus `--series-75`) makes the output
behave like a real video source: 286 mV of sync depth into a terminated load,
572 mV unterminated. Without it the AWG's near-zero output impedance delivers
the same level either way, which is correct into a terminated load but reads
half of what a broadcast generator does in an unterminated chain.

**The series resistor cannot raise the level at a terminated load, and is not
meant to.** `--series-75` doubles the AWG drive and the 75 Ω / 75 Ω divider
halves it again, so the load sees exactly what it saw with no resistor and no
flag. What the resistor buys is a correct source impedance — no reflections off
an unterminated stub, which is what edge overshoot usually is. To change
*level*, use `--level`.

**`--series-75` assumes a divider you have to verify.** The ×2 is only right
if the chain presents a single 75 Ω termination. Always check the level against
a flat `--pattern white` field after fitting the resistor; don't assume the ×2
landed. On this bench it didn't — see the open item in
[NTSC_FINDINGS.md](NTSC_FINDINGS.md).

Without the resistor the source impedance is ~0 Ω, so it ignores the load and
the level is correct no matter how the chain terminates. That is the safe
default, and it is why `--series-75` is off unless asked for.

## Setting output level

75% bars peak at **76.88 IRE** by construction (setup + 0.925 × 0.75) and never
reach 100, so they are the wrong thing to calibrate against. Use a flat field:

```bash
# unbounded motion: a bouncing ball, buffer-swapped from deep memory
python3 ntsc.py --deep --color --play-scale 1.0 --locked --motion --dual \
                --series-75 --level 0.67

# flat 100 IRE white field -- set --level so the monitor reads 100 IRE
python3 ntsc.py --deep --color --play-scale 1.0 --locked --pattern white

# full-amplitude bars: 100 IRE white, chroma peaks ~134 IRE (some inputs clip)
python3 ntsc.py --deep --color --play-scale 1.0 --locked --bar-amp 100
```

Note that `--locked` includes `--level 0.9`, i.e. everything runs 10% low:
76.88 × 0.9 = **69.2 IRE** peak white on default bars. Add `--level 1.0` to
undo that once the chain tolerates it.

## What it produces

`--deep --color` emits a four-field sequence of 75% EIA colour bars:

| | value |
|---|---|
| structure | 525 lines interlaced, two fields of 262.5 |
| sample rate | 14.285714 MS/s (100 MHz ÷ 7), 908 samples/line |
| line rate | 15733.17 Hz (−0.0070%) |
| field rate | 59.9359 Hz |
| subcarrier | 3.579295 MHz, locked to exactly 227.5 × line rate |
| sequence | 953,400 samples, 238,875 whole subcarrier cycles — loops seamlessly |
| levels | sync −40 IRE, blanking 0, black +7.5, white +100 |
| setup | 7.5 IRE pedestal (`--setup 0` for zero-setup / NTSC-J) |
| bar amplitude | 75% EIA, peak white 76.88 IRE (`--bar-amp 100` for full) |
| burst | 9 cycles, 40 IRE p-p, 0.63 µs breezeway |

Chroma vector angles land within **0.04°** of the published NTSC vectorscope
values, and complements are exactly 180° apart. Luma is band-limited to
4.2 MHz and the chroma axes to 1.3 MHz, as a real encoder does.

## Generation paths

| path | rate | samples/line | colour | host involvement |
|---|---|---|---|---|
| `--deep` | 14.286 MS/s | 908 | yes | **none after setup** |
| `--deep --motion` | 14.286 MS/s | 908 | yes | **none at all** |
| `--stream` | ~5.8 MS/s measured | ~370 | can't sustain it | continuous feed loop |
| default (`funcCustom`) | 0.847 MS/s | 54 | no | none |

`--deep` preloads the whole sequence into the device's analog-out play memory
(134 M samples — 9.4 seconds of video) and plays it with an unbounded run.
`--stream` feeds chunks via `PlayData` and is superseded for anything that
fits in play memory. `funcCustom` loops a 32 Ki buffer in hardware, which is
rock solid but caps samples-per-line.

## Motion

```bash
# 4 s loop, ~1 screen-width/sec — the default
python3 ntsc.py --deep --color --play-scale 1.0 --locked --motion

# 9.4 s loop: slower, more natural motion, 1 GB of host RAM
python3 ntsc.py --deep --color --play-scale 1.0 --locked --motion \
                --motion-seconds 9.4
```

A white, luma-only ball bounces over the static bars. The whole animation goes
into play memory in **one `DataSet`** and loops with run 0 / repeat 0 — the
same mechanism `--deep` uses for a static picture — so the hardware loop *is*
the animation and there is no host feed at all.

**`--dual` removes the 9.34 s ceiling.** One buffer of `--dual-seq` whole
four-field sequences (default 4 = 267 ms) loops in hardware while the next
pass is generated and handed over with `NodeDataSet` +
`FDwfAnalogOutConfigure(ch, 3)`. Still no `PlayData`, so still none of the
sample loss streaming suffers. Measured: **0 defects over 14,284 square-wave
half-periods** with applies happening, generation 2 ms and upload 45 ms against
a 267 ms budget, 356 applies with none refused, 1.36 G samples of unique
content. Confirmed on a CRT — no rolling, no stutter.

Two constraints the code enforces or relies on: `NodeDataSet` **silently does
nothing below about 2,000,000 samples** (it returns success and the channel
keeps playing stale content, so `--dual` refuses and names the minimum
`--dual-seq`), and the apply takes effect **immediately** rather than at a
buffer wrap — harmless here because every pass carries identical sync
structure, so only the ball position can move.

Note the ball's path repeats about every 5 s: a constant-velocity bouncing
ball is periodic (x period 4.76 s, y 6.45 s at the default speeds) even though
the data is new every pass.

Only the ball moves, so the static four-field background is built once and
`memmove`'d per sequence with the ball painted over it as contiguous runs:
6,271 samples of edit per 953,400, and 0.08 s to build all 60 sequences.

| `--motion-seconds` | sequences | loop | host RAM | ball speed |
|---|---|---|---|---|
| 1.0 | 15 | 1.00 s | 114 MB | 4.00 w/s |
| **4.0** (default) | 60 | 4.00 s | 458 MB | 1.00 w/s |
| 9.4 | 140 | 9.34 s | 1068 MB | 0.43 w/s |

**Loop length buys slower motion.** A seamless loop needs the content to close,
not just the buffer: each four-field sequence already closes on 238,875 whole
subcarrier cycles, but the ball must also complete a whole number of bounce
cycles, and the x and y counts have to be coprime and distinct — equal counts
put the axes in lockstep and the ball slides along one diagonal instead of
bouncing. Rounding to whole cycles is what ties speed to loop length.

`--motion-stream` selects an alternative path that appends with `PlayData`
instead. **It has never produced output** — see Status.

## Measurement

```bash
# conformance report: levels, sync depth, burst, breezeway, fsc/fH lock
python3 ntsc.py --deep --color --locked --analyze

# per-line metrics, reporting WHERE anomalies fall and whether they repeat
python3 ntsc.py --deep --color --locked --scan

# profile a reference generator (nothing of ours running), then diff
python3 ntsc.py --capture /tmp/ref.csv --seconds 0.25
python3 ntsc.py --deep --color --locked --save-capture /tmp/ours.csv --scan
python3 ntsc.py --compare /tmp/ref.csv /tmp/ours.csv
```

`--scan` reports the *position* and *spacing* of anomalies, not just their
magnitude. That matters: a defect affecting one line in a thousand is
invisible in an average and obvious in a spacing histogram.

`--compare` was the most useful tool here. Diffing against a signal that
demonstrably works beats deciding what a compliant signal should look like and
testing against that belief.

## Device quirks worth knowing

Documented in full, with measurements, in **[NTSC_FINDINGS.md](NTSC_FINDINGS.md)**.
The three that cost the most time:

1. **The AWG quantises its sample rate to an integer divider of 100 MHz.** Ask
   for anything else and it runs at the nearest faster divider, stalling a
   whole sample periodically to hold the average — which is whole-sample
   timing jitter, not a small analogue disturbance. Request exactly `100e6/N`.
2. **Play memory is reported by `FDwfAnalogOutNodePlayInfo`, not
   `DataInfo`.** On deep-memory devices (the manual names the ADP2230) that
   figure is *how many samples one `DataSet` call accepts* — 134 M, not a
   streaming FIFO. `DataInfo` reports 32768 for every function in every
   configuration and is not the ceiling.
3. **Repeating a fixed-length play run costs ~24 samples of re-arm at each
   boundary**, putting one line per sequence 1.66 µs long. Use an unbounded
   run (`--run-infinite`) instead.

Also: `FDwfEnumDevices` does not exist (it is `FDwfEnum`);
`FDwfAnalogOutStatus` must be called every iteration of a feed loop or
`PlayStatus` returns stale values; and the play rate scale factor between
requested and actual is configuration-dependent, so measure it rather than
assume.

## NTSC facts that bit us

- **Horizontal sync runs on one continuous grid for the whole frame.** The
  oscillator never re-phases. The half-line offset that creates interlace
  belongs to the **vertical** sync alone. Shifting the h-grid between fields
  makes a monitor's AFC slew half a line every field — visible as flagging at
  the top of the picture.
- `fsc = 227.5 × fH` puts a half subcarrier cycle on every line, so a 525-line
  interlaced frame is 119,437.5 cycles and needs **four fields** to close.
  Progressive 262 lines is 59,605 cycles exactly and closes in one frame.
- Vertical-interval lines sit at **blanking (0 IRE)**, not black. The 7.5 IRE
  setup pedestal applies to active picture only. `--setup <IRE>` moves the
  pedestal (0 gives zero-setup NTSC-J, where black and blanking coincide);
  luma maps 0..1 onto pedestal..white and chroma takes the same gain, so 75%
  bars peak at 76.88 IRE with setup and exactly 75.00 without it.
- Saturated red and blue on 75% bars legally reach **−16 IRE**, so a sync
  slicer set at −12 IRE will false-trigger on chroma.

## Repository

| file | |
|---|---|
| `ntsc.py` | the generator and analyser |
| `NTSC_FINDINGS.md` | measured device and NTSC facts, with the numbers |
| `NTSC_SCORECARD.md` | an honest account of which theories held up |
| `NTSC_STREAMING_STRATEGIES.md` | options considered for unbounded output, with the result |
| `TODO.md` | outstanding work, including a cleanup pass |

Scope captures are gitignored — they run to 10 M samples (~200 MB) each.

## Requirements

- Digilent WaveForms installed (provides `dwf.framework` / `libdwf`)
- Python 3, standard library only — no third-party packages

Tested on macOS with WaveForms SDK 3.25.1. The library path is selected per
platform, so Windows and Linux should work, though neither has been tried.

## Status

Colour bars lock on a Leader LV5700A and display correctly on a CRT. One known
anomaly remains: a single line per sequence at ~73% peak white, roughly 90%
through the sequence. Sync width, line period and burst amplitude all scan
clean. The subcarrier sits 250 Hz below nominal as a consequence of locking it
to 227.5 × our line rate; `--fsc-nominal` trades that for SCH phase walking.

`--motion` works: a bouncing ball loops out of deep memory with no host
involvement.

**Unbounded motion works** via `--motion --dual`: a deep-memory buffer swapped
with `FDwfAnalogOutConfigure(ch, 3)`, measured at **0 defects** over 14,284
square-wave half-periods with applies in flight, and confirmed on a CRT.

**Closed: streamed video is not achievable on this unit.** `PlayData`
streaming works — `--motion-stream` sustains the colour rate indefinitely with
a clean feed — but the device loses and gains whole **32-sample blocks** at
about 900 per second, which is 15 timing displacements of 2.24 µs per field
and far more than a monitor's vertical lock will absorb.

Established against a hardware control (`funcSquare`, no streaming in the
path), which measured clean to ±0.003 samples over 5,714 half-periods:

| source | defects |
|---|---|
| `funcSquare`, hardware | **none**, median 100.000 (99.997..100.003) |
| `funcPlayPattern`, streamed | **36 of 5,718**, range 0.912..132.001 |

The event rate is fixed rather than throughput-dependent — dropping to
11.111 MS/s left it at ~825/s and made the net loss worse (0.087% → 0.254%)
— and `lost`/`corrupt` report zero throughout, so those counters cannot
validate a stream. Full numbers in **[NTSC_FINDINGS.md](NTSC_FINDINGS.md)**.

Use `--motion` (deep-memory loop) for a clean picture. The streaming path is
kept for signals tolerant of 0.1% sample loss.
