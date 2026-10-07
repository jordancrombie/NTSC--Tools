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
| burst | 9 cycles, 40 IRE p-p, 0.63 µs breezeway |

Chroma vector angles land within **0.04°** of the published NTSC vectorscope
values, and complements are exactly 180° apart. Luma is band-limited to
4.2 MHz and the chroma axes to 1.3 MHz, as a real encoder does.

## Generation paths

| path | rate | samples/line | colour | host involvement |
|---|---|---|---|---|
| `--deep` | 14.286 MS/s | 908 | yes | **none after setup** |
| `--stream` | ~5.8 MS/s measured | ~370 | can't sustain it | continuous feed loop |
| default (`funcCustom`) | 0.847 MS/s | 54 | no | none |

`--deep` preloads the whole sequence into the device's analog-out play memory
(134 M samples — 9.4 seconds of video) and plays it with an unbounded run.
`--stream` feeds chunks via `PlayData` and is superseded for anything that
fits in play memory. `funcCustom` loops a 32 Ki buffer in hardware, which is
rock solid but caps samples-per-line.

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
  setup pedestal applies to active picture only.
- Saturated red and blue on 75% bars legally reach **−16 IRE**, so a sync
  slicer set at −12 IRE will false-trigger on chroma.

## Repository

| file | |
|---|---|
| `ntsc.py` | the generator and analyser |
| `NTSC_FINDINGS.md` | measured device and NTSC facts, with the numbers |
| `NTSC_SCORECARD.md` | an honest account of which theories held up |

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
