# ADP2230 + NTSC — Hard-Won Facts

Everything in here was **measured or read from the vendor docs**, not inferred.
Written because the inferred stuff was wrong far more often than not.

Device: Analog Discovery Pro 2230, SN 210417BAF32C, WaveForms SDK 3.25.1,
`/Library/Frameworks/dwf.framework` on macOS. Header:
`/Library/Frameworks/dwf.framework/Versions/A/Headers/dwf.h`.
Manual: `/Applications/WaveForms.app/Contents/Resources/SDK/WaveForms SDK Reference Manual.pdf`
(163 pages). Examples: `.../SDK/samples/py/` — **read them, they matter.**

---

## 1. SDK gotchas that cost real time

| Thing | Truth |
|---|---|
| `FUNC` enum | 0 DC, 1 Sine, 2 Square, 3 Triangle, 4 RampUp, **5 RampDown**, 6 Noise, 7 Pulse, 8 Trapezium, 9 SinePower, 10 SineNA, **28 CustomPattern, 29 PlayPattern, 30 Custom, 31 Play** |
| Original script's bug | `funcPlay = c_int(5)` = **funcRampDown**. It emitted a sawtooth and ignored the buffer entirely. |
| Device enumeration | `FDwfEnum` — **`FDwfEnumDevices` does not exist** |
| `DwfParamDACFrequency` (25) | **Not supported on ADP2230** — returns failure |
| `FDwfAnalogOutStatus` | **Must be called every loop iteration.** `PlayStatus` returns stale values (`free == 0`) without it. Not an optional round trip. |
| Scope input stage | Needs **~2 s to settle** after a range/offset change. The first capture reads garbage levels (e.g. 0.001–0.105 V when the signal is 0–1 V). |
| `TRIGSRC`, `DwfState`, `FUNC` | `unsigned char` — use `c_ubyte` for out-params |
| Unknown CLI flags | Were silently ignored, so a typo ran the default path and looked like a successful test of something else. Now rejected. |

## 2. funcCustom vs the play functions

**`funcCustom` (30)** — loops a buffer in hardware.
- `FrequencySet` = **buffer repetition rate** (i.e. frame rate), *not* a sample rate
- Data must be normalized to **−1…+1**; output = `offset + amplitude × sample`
- Buffer capped at **32768** samples (16384 in config 1)
- `FDwfAnalogOutNodeDataSet` **before** `FrequencySet`, per the shipped example

**`funcPlay` (31) / `funcPlayPattern` (29)** — host-fed or preloaded.
- `FrequencySet` = **sample rate**
- `FDwfAnalogOutNodeDataInfo` max = **32768 for all four functions, every config**
- `FDwfAnalogOutNodePlayInfo` max is **NOT a FIFO** — see §3

**Play rate scale factor — do NOT trust `FrequencyGet`.** What the readback
*reports* as accepted ÷ requested:

| | funcPlay | funcPlayPattern |
|---|---|---|
| config 0, 2, 3 | 4.00× | 0.50× |
| config 1, 4, 5 | 1.00× | 0.50× |

These are **reported** values, and at least one is wrong: config 3 + `funcPlay`
reports 4.00× while a scope-calibrated measurement gives **1.0000×**, and
requesting the rate directly produces a verified-correct signal. Always
calibrate against the scope (see §3).

## 3. Deep memory — the thing I got backwards

Manual, on `FDwfAnalogOutNodePlayInfo`:

> "Retrieves information about the play buffer size. **For devices with deep
> memory (ADP3X50, ADP2230) up to this number of samples can be set at once.**
> For devices without deep memory this will report zero but play mode can be
> still used by sending data in chunks."

And on the lost/corrupted counters:

> "...reduce the frequency or runtime to be **less or equal to the device buffer
> size (runtime <= buffer size / frequency)**."

So **`PlayInfo` max = how many samples one `DataSet` call accepts**, and the
ADP2230 is named as a deep-memory device. `DECIAnalogOutPlaySize` per config:

| config | AWG buf | scope buf | digital-out | **play memory** |
|---|---|---|---|---|
| 0 | 32768 | 67108864 | 32768 | **67108864** |
| 1 | 16384 | 32768 | 16384 | 0 |
| 2 | 32768 | 67108864 | 32768 | **67108864** |
| 3 | 32768 | 67108864 | 32768 | **134217728** |
| 4 | 32768 | 67108864 | 32768 | 0 |
| 5 | 32768 | 134217728 | 32768 | 0 |

A 525-line frame at 908 samples/line is **476,700 samples = 0.71%** of 67 M.
The four-field colour sequence is 953,400 = 1.42%.

`FDwfDeviceOpenEx(szOpt, phdwf)` options: `index:#`, `sn:#`, `name:`,
`config:#`, `ip:`, `secure:#`, `analogin-memory:#`, `analogout-memory:#`,
`digitalin-memory:#` (bytes). Defaults: 256 MiB analog-in, **128 MiB
analog-out play**, 128 MiB digital-in.

**Chunked streaming measurements (for reference only — superseded by deep
memory):** configs 0/2/3 output flat when fed in chunks; configs 1/4/5 output
correctly but sustain only ~5.8 MS/s because `free` never exceeds ~32768 and
the loop runs ~2.5–3 ms per iteration.

### Deep-memory recipe that works (verified)

Config 3, `funcPlay` (31), **no host feed loop at all**:

```
AutoConfigureSet(0)
NodeEnableSet(1); NodeFunctionSet(funcPlay)
NodeFrequencySet(rate / true_scale)       # sample rate
NodeAmplitudeSet(0.5); NodeOffsetSet(0.5)
NodeDataSet(data, 476700)                 # WHOLE sequence, one call
RunSet(total / rate)                      # == sequence duration
RepeatSet(0)                              # infinite
WaitSet(0.0); Configure(1)                # then do nothing
```

**`RunSet` must be derived from the rate actually achieved.** Computing it from
the intended rate while the hardware runs slower truncates the sequence
mid-frame and restarts forever — which on screen is crosshatching plus
scrolling artifacts, not an obviously "wrong rate" picture.

**The play rate factor must be calibrated against the scope.**
`FrequencyGet` is unreliable for play mode — observed reporting 4× the request
in config 0, and echoing the request in config 3 where the true factor is
**1.0**. Calibrate by playing a square wave of known period *in samples* and
measuring it: `actual_rate = samples_per_period / measured_period`. In config 3
with `funcPlay` the measured factor is **1.0000×** (requested 2.000000 MS/s →
measured 2.000000 MS/s, 0 ns jitter).

## 4. The AWG clock — cause of all the jitter

**The AWG quantizes its sample rate to an integer divider of 100 MHz.** Ask for
a rate between dividers and it runs at the nearest *faster* divider and
**stalls a whole sample** periodically to hold the long-term average.

Measured at 54 samples/line: requested 1177.0 ns/sample, got **1170.4 ns
(= 100 MHz ÷ 117)**, with an extra sample inserted on **32% of lines**. Visible
in h-sync widths: 1094 pulses at 4 samples, **27 at 5 samples**, 1 at 3.

**Fix: request exactly `100e6 / N`.** Result: peak-to-peak jitter **0 ns** over
1064 lines, every pulse width identical to the nanosecond.

Consequences:
- The jitter scales with sample period, so chasing better sync widths by
  *lowering* the sample rate makes jitter **worse**.
- There is a three-way trade (sync width / line rate / sample count). For
  interlaced in a 32768 buffer only four divider combinations are viable:

| spl | N | line rate | line err | sync worst | active |
|---|---|---|---|---|---|
| **54** | **118** | 15693.66 Hz | **−0.258%** | **2.61%** | 45 |
| 60 | 106 | 15723.27 Hz | **−0.070%** | 9.79% | 51 |
| 52 | 122 | 15762.93 Hz | +0.182% | 6.09% | 43 |
| 54 | 117 | 15827.79 Hz | +0.594% | 1.74% | 45 |

- DigitalOut divides the **same 100 MHz** by an integer, so it can never land
  in this situation. 16 channels, 32768 bits/channel, internal clock
  **100.000 MHz** exactly.

## 5. NTSC facts that actually bit us

**Line timing** (63.5556 µs, 15734.2657 Hz): h-sync 4.7, back porch 4.7,
active 52.6556, front porch 1.5 µs. Equalizing pulse 2.3 µs, broad vertical
pulse 27.1 µs, serration 4.7 µs.

**Levels** — 1 V p-p over 140 IRE, sync tip referenced to 0 V:
sync 0.0000 V (−40 IRE), blanking 0.2857 (0 IRE), black 0.3393 (+7.5 IRE),
white 1.0000 (+100 IRE). For `funcCustom`: amplitude **0.5**, offset **0.5**,
data `(v − 0.5) / 0.5`.

**Vertical-interval lines sit at BLANKING (0 IRE), not black.** The 7.5 IRE
setup pedestal applies to active picture only.

**A 75 ohm series resistor does not raise the level — it cannot.** The AWG is
a near-zero-impedance source. Compare, at a 75 ohm terminated load:

| | AWG drive | at the load |
|---|---|---|
| no resistor, no flag | 1.00 V p-p | **1.00 V p-p** |
| 75 ohm + `--series-75` (x2) | 2.00 V p-p | **1.00 V p-p** |

Identical. `--series-75` doubles the drive and the 75/75 divider halves it
back. The resistor buys a correct *source impedance* (no reflection off an
unterminated stub — the usual cause of edge overshoot), not amplitude. Level is
`--level`, and with the resistor fitted `--level` means the same thing at the
load as it did without it.

**MEASURED: streamed PlayData loses and gains whole 32-SAMPLE BLOCKS.**
Found with `--stream-square`, which streams a square wave of known
half-period in samples and measures each half-period back; DC cannot show a
dropped sample and video is too tangled to attribute one. On a clean feed
(chunk generation 5.7% of budget, 14.292 MS/s fed, fill positive throughout,
`lost` and `corrupt` both 0):

| | |
|---|---|
| defects | 36 of 5,720 half-periods (0.63%) |
| size | always a whole 32-sample block: -32.00, +32.00, or split (-23.09 with -8.00) |
| rate | ~900/s, about 15 per field |
| each worth | 2.24 us of timing displacement at 14.286 MS/s |
| net | -655 samples per 40 ms, 16,378/s lost, 0.115% of the stream |
| spacing | irregular, median 158 half-periods, range 1..328 |

The irregular spacing rules out the sequence wrap and any fixed chunk
boundary. `lost` and `corrupt` report zero throughout, so **those counters
cannot be used to validate a stream** -- this is the third time they have read
clean on a measurably broken one.

15 displacements of 2.24 us per field is ample to keep a monitor's AFC and
vertical lock unsettled, which matches a picture that rolls, twists, and
tracks feed health rather than content.

**CONFIRMED against a hardware control.** The same square wave, the same
scope settings, the same analysis, differing only in how the samples reach the
DAC:

| source | edges (5,714 expected) | defects | median half-period |
|---|---|---|---|
| `funcSquare`, generated in hardware | 5,715 | **none** | 100.000 (99.997..100.003) |
| `funcPlayPattern`, streamed | 5,719 | **36 of 5,718** | 100.000, range 0.912..132.001 |

The control is clean to **+/-0.003 samples**, so the DAC and the capture path
are sound and the streamed defects are real. Streaming on this device loses
and gains whole 32-sample blocks at about 900 per second -- 0.115% of the
stream -- and `lost`/`corrupt` report zero throughout.

**Not caused by our write sizes.** `--stream-align 32` changed nothing: 38 of
5,720 against 36 of 5,720 unaligned, net -696 against -655. The device drops
blocks regardless of how the writes are shaped.

**ROOT CAUSE: one 32-sample block is lost per FIFO revolution.** Found by
running the SDK's documented loop (`--stream-doc`) instead of the clock-paced
one, which removes the overfeeding and leaves only the device's own defect:

| | |
|---|---|
| defects | 14 of 4,448 half-periods |
| size | **every one exactly -32.00 samples** |
| spacing | **328 half-periods = 32,800 samples, range 328..328** |

32,800 - 32 = **32,768**, the FIFO size exactly. So the gap between losses is
one FIFO consumed plus the 32 that vanished: the ring buffer drops a
32-sample block every time it wraps. 32/32,800 = **0.0976%**, deterministic,
and independent of congestion, scheduling or how we shape our writes -- which
is why `--stream-align 32` changed nothing.

The clock-paced loop shows this PLUS irregular extra losses (0.2538% against
0.1008% for the documented loop at the same rate). Those extras are
overfeeding -- my first theory, which I abandoned for the wrong reason: the
guard band I added to test it also shrank the chunk and starved the feed, and
I read the starvation as a refutation.

**Why the documented loop is not a fix either.** It costs 2470 us per pass
(AnalogOutStatus 1393 + PlayData 1077, the latter FLAT regardless of size)
while the FIFO holds only 2294 us at 14.286 MS/s, so the colour rate is
structurally out of reach -- the buffer empties before the next refill can
arrive. The arithmetic says 11.111 MS/s should fit (2949 us of FIFO), but
measured it sustained only 8.769 MS/s, so PlayData evidently blocks when the
buffer is full and the real per-pass cost is higher than the isolated timing.

| call | cost | notes |
|---|---|---|
| `FDwfAnalogOutStatus` | 1393 us | the only real device round trip |
| `FDwfAnalogOutNodePlayData` | ~1077 us | FLAT from 1 Ki to 32 Ki samples |
| `FDwfAnalogOutNodePlayStatus` | 0.7 us | local read of cached state |
| `FDwfDeviceParamGet` | 0.4 us | local |

`analogout-memory:#` in `FDwfDeviceOpenEx` does NOT deepen the FIFO -- tried
at 16, 64, 128, 256 and 512 MiB, `DataInfo` max stayed 32,768 every time. So
the FIFO depth is fixed and the trap cannot be opened from the host: a valid
`free` costs more time than the buffer holds.

**Pre-compensation implemented, and it does NOT work: the loss is
phase-locked to a FIFO address we cannot control.** `--stream-pad 32
--stream-pad-every 32768` inserts 32 hold samples per 32,768 real ones so the
discarded block should be padding. Verified byte-correct offline (content
position advances only over real samples, padding is a hold of the last real
sample, 0 of 4,200 samples differ from expectation). On hardware, with the
documented loop at 10 MS/s so overfeeding does not confound it:

| | spacing between defects | net sample error |
|---|---|---|
| no padding | 328 half-periods, range 328..328 | **-384.0** |
| 32 per 32,768 | 23 half-periods, range 1..304 | **-384.0** |

**Identical net loss.** The padding is not replacing the dropped block; the
drop still lands on real samples, and the padding merely scatters the defects
(regular 328 spacing becomes irregular 1..304) and adds +/-64-sample signatures
where a pad and a drop interact. That signature confirms the mechanism -- the
device really is discarding a block at a ring boundary -- but the boundary is
a fixed FIFO ADDRESS, and our write offsets never line up with it.

Aligning it would need the ring phase, and nothing in the API exposes it.
Writing exactly `fifo` samples per call would pin the phase, but a write can
only be as large as `free`, which is only 32,768 when the buffer is empty --
i.e. when it is already underrunning. Sweeping blindly is 1,024 distinct
32-aligned phases at ~10 s per test. So this is a dead end from the host.

**The event rate is FIXED, not throughput-dependent, so no sample rate is
clean.** Measured at two rates, both with a clean feed and a clean hardware
control:

| div | MS/s | defects | events/s | net lost/s | % of stream | us per block |
|---|---|---|---|---|---|---|
| 7 | 14.286 | 36/5,718 (0.63%) | 900 | 12,422 | 0.087% | 2.24 |
| 9 | 11.111 | 33/4,455 (0.74%) | 825 | 28,200 | 0.254% | 2.88 |

Dropping the rate by 22% did not reduce the event rate at all, and made the
damage WORSE on both measures that matter: the net loss rose from 0.087% to
0.254% of the stream, and each 32-sample block costs more time at a lower
rate (2.24 us to 2.88 us). ~850-900 events per second is close to the 1 kHz
USB frame rate -- about one lost block per frame -- but whatever the
mechanism, it is fixed-rate and below anything the host controls.

**Consequence for video.** Fifteen 2.24 us displacements per field is far more
than a monitor's AFC or vertical lock will absorb, which is why the streamed
picture rolls and twists while identical content from deep memory is perfect.
There is no host-side fix: the feed is already clean by every measure
available (right rate, positive fill, zero counters, generation at 5-10% of
budget, GC disabled), and no rate at which the device is clean.

**So streamed video is not achievable on this unit.** The streaming
architecture works and is worth keeping -- it sustains the colour rate
indefinitely and would be fine for audio or any signal tolerant of 0.1%
sample loss -- but a clean picture has to come from the deep-memory loop.

**Two diagnostics in this project broke what they measured.** `AnalogOutStatus`
at 10 Hz cost 1792 us of a 2.29 ms FIFO, and the first `--stream-square`
filler wrote samples from Python at 2.761 ms per chunk against the same
budget -- it reported its own starvation as 31 device defects. `stream_feed`
now refuses to start when chunk generation exceeds 30% of the FIFO budget.

**SETTLED: PlayData streams ONLY where play memory is 0.** Measured with
`--feed-matrix`, which primes DC at -0.5 V and streams DC at +0.5 V so the
capture says which one is on the wire. Both SDK recipes, all six
configurations:

| cfg | play memory | max cFree | fed MS/s | CH1 | |
|---|---|---|---|---|---|
| 0 | 67,108,864 | 67,098,880 | 63.2 | **-0.499** | loops the prime |
| 1 | 0 | 16,360 | 1.9 | **+0.502** | **streams** |
| 2 | 67,108,864 | 67,105,536 | 64.0 | **-0.499** | loops the prime |
| 3 | 134,217,728 | 134,202,624 | 72.3 | **-0.499** | loops the prime |
| 4 | 0 | 32,680 | 2.1 | **+0.502** | **streams** |
| 5 | 0 | 32,688 | 2.0 | **+0.502** | **streams** |

The deep-memory configurations accept PlayData at 63-72 MS/s, report zero
lost and zero corrupted, and play the primed buffer on loop instead. The
counters never complain, so only a capture tells you. `pick_deep_config`
selects the deepest play memory, which is therefore the WORST choice for
streaming -- that is why `--motion`'s streaming path was dead while `--stream`
(which uses `pick_config`, preferring play size 0) produced video.

**Our call sequence matched neither shipped recipe.** From the SDK samples:

| | AnalogOut_Play.py | AnalogOut_PlayTest.py | what we had |
|---|---|---|---|
| function | funcPlay (31) | funcPlayPattern (29) | funcPlay |
| repeat | RepeatSet(1) | not set | RepeatSet(0) |
| run | RunSet(duration) | not set | RunSet(0.0) |
| feed size | exactly cFree | exactly cFree | host-side budget |

Both prime with the DataInfo size and `continue` when cFree is 0. **Feeding
more than cFree is an error** -- that, not a device limitation, was the
"PlayData refused" seen in the streaming configurations on the first attempt.

**The rate ceiling is FIFO depth / loop period.** A streaming configuration
has about 32,768 samples of FIFO, which is only 2.29 ms of video at the colour
rate, so the whole feed loop has to complete inside 2.29 ms. Against a 1413 us
round trip:

| loop structure | period | max rate | |
|---|---|---|---|
| Status + PlayStatus + PlayData | 4.24 ms | 7.73 MS/s | short |
| PlayStatus + PlayData | 2.83 ms | 11.60 MS/s | short |
| PlayData only, paced | 1.41 ms | 23.19 MS/s | enough |

**SETTLED by `--feed-tune`: colour streaming works at 14.295 MS/s.** The whole
thing turns on one measurement -- the two status calls are not comparable:

| call | cost in a streaming config |
|---|---|
| `FDwfAnalogOutStatus` | **1792 us** |
| `FDwfAnalogOutNodePlayStatus` | **2 us** |

900x apart, and that explains every row:

| loop | max clean | why |
|---|---|---|
| Status + PlayStatus + PlayData | 2.0 MS/s | `AnalogOutStatus` in the hot loop |
| PlayStatus + PlayData | **0** | stale `free`, fed nothing, looped the prime |
| **PlayData paced, PlayStatus every 100 ms** | **14.286 MS/s** | neither problem |

The two constraints look contradictory and are not: `PlayStatus` returns
STALE values unless `AnalogOutStatus` is called, but `AnalogOutStatus` is far
too slow to call per iteration. Pacing the FIFO fill against the wall clock
needs neither -- the device consumes at exactly the rate we set, so
`fill = fed - elapsed * rate`. `PlayStatus` is then polled occasionally
purely to read the lost and corrupted counters. The 2-call row is the proof
of the stale-`free` half: 0.000 MS/s fed and CH1 sitting on the primed level.

Note the SDK examples use the 3-call form, which is correct and clean -- just
only to about 2 MS/s on this unit. Audio does not need more.

**The working recipe for feeding arbitrary data:**

1. `pick_stream_config()` -- play memory 0, deepest scope buffer (config 5)
2. `funcPlayPattern`, run and repeat left at their defaults
3. prime `DataInfo` samples (32768 = 2.29 ms at the colour rate)
4. feed with `PlayData` only, sized from a clock-paced fill estimate
5. poll `PlayStatus` every 100 ms for the counters, never for sizing
6. generate the signal PER CHUNK, never per frame or per sequence

**The 2.29 ms FIFO is the whole design constraint.** Everything the feed loop
does between `PlayData` calls is spent against it, and two things that look
harmless do not fit:

| work | cost | share of a 2.29 ms FIFO |
|---|---|---|
| `FDwfAnalogOutStatus` | 1792 us | **78%** |
| rebuilding a 953,400-sample sequence on wrap | 800 us | **35%** |
| `FDwfAnalogOutNodePlayStatus` | 2 us | 0% |
| generating one 32,768-sample chunk on demand | 22 us | **1%** |

Adding the first two to a loop that had measured clean cost 2.3% of all
samples, dropped in FIFO-sized chunks -- 32,768 lost per 100 ms poll, minimum
fill -2.59 ms. That dropout rate breaks sync and rolls the picture. Note the
rate itself was fine: measured -0.052%, a field rate of 59.9035 Hz against
59.9401 nominal, which a CRT holds comfortably. **Rolling was the dropouts,
not the timing.**

So generate on demand: `fill_fn(dst, abs_pos, n)` writes n samples starting at
an absolute sample index. Per chunk that is a 262 KB memmove plus a few short
runs -- 22 us against 800 us for the same work done per sequence, and no stall
concentrated at a wrap. Verified byte-identical to building whole sequences,
including chunks that straddle a sequence boundary.

What works, and what `--motion` now does: put the whole animation in ONE
DataSet and let run 0 / repeat 0 loop it in hardware -- the same mechanism
`--deep` uses for a static picture. 140 four-field sequences fit in play
memory (133,476,000 of 134,217,728 samples), which is 9.34 s at 1,068 MB of
host doubles.

A looping buffer is only seamless if the CONTENT closes too. Every four-field
sequence already closes on a whole number of subcarrier cycles (238,875), so
any whole number of them does; the ball does not, so its bounce must complete
a whole number of cycles over the loop. The cycle counts must also be coprime
and distinct -- equal counts put x and y in lockstep and the ball slides along
one diagonal instead of bouncing. A short loop therefore forces fast motion:
4.0 s gives 1.00 w/s, 9.34 s gives 0.428 w/s. That is the real trade, and
memory is what buys slower motion.

**`PlayStatus`'s `free` is writable space, not fill.** Feeding it directly is
self-limiting: the buffer fills, then settles at the consumption rate. Do not
derive an in-flight depth as `cap - free` and gate on it -- that inverts the
test and feeds nothing. Bound overrun by capping the chunk instead (256 Ki
measured clean, 4 Mi corrupted 211,200 samples). Observed values of `free` are
not yet consistent between runs: `--stream-probe` saw it near the full 134 M
after priming, while a full-sequence prime reported 890,585. A feed policy
that only requires `free > 0` is immune to the difference.

**OPEN: `--series-75` doubled the drive and the reading went DOWN.** Measured
with a 75 ohm resistor fitted in series with W1, reading peak white off a flat
100 IRE field (`--pattern white`) on the Leader:

| run (resistor fitted both times) | AWG drive | read at the Leader | implied path gain |
|---|---|---|---|
| `--level 1.0` (no flag) | 1.00 V p-p | **100 IRE** | 1.00 |
| `--level 1.0 --series-75` | 2.00 V p-p | **72 IRE** | 0.36 |

**These are mutually inconsistent.** A passive linear chain cannot produce less
output from more drive, so no single path gain explains both rows. Candidates,
none yet confirmed:

1. The larger amplitude/offset request is being clamped by the configuration.
   `report_output_window()` now reads back what the AWG actually accepted,
   against its real limits, in all three generation paths -- added precisely to
   settle this without guessing.
2. The Leader was overloaded on the second row (2 V p-p into an input expecting
   1 V) and the reading is not meaningful.
3. The resistor is not in series the way it looks, or is not 75 ohm.
4. Something else moved between the runs (a termination switch, the Leader's
   gain/scale setting).

Note row 1's gain of 1.00 implies the chain is **unterminated**, since a 75 ohm
series resistor into a 75 ohm load would read 50 IRE. That contradicts a
monitor that is displaying a picture, which is a further reason to distrust the
model rather than the measurements.

**Lesson, again:** the two changes in that A/B were *designed* to cancel
(resistor + x2 at gain 0.50 == no resistor + x1 at gain 1.00, both 100 IRE), so
when they didn't cancel I reached for a cause. I assumed the resistor had been
removed, concluded "double termination, or a 133 ohm resistor", and wrote it up
with a confident table. The premise was wrong. Confirm what changed between two
runs before explaining why they differ.

**75% bars cannot reach 100 IRE.** Peak white is `setup + 0.925 x 0.75` =
**76.88 IRE**, and `--locked` carries `--level 0.9`, so the default command
tops out at **69.2 IRE**. That is correct, not a loss. Calibrate level against
`--pattern white` (a flat 100.00 IRE field, no chroma) or use `--bar-amp 100`
for full-amplitude bars, where chroma peaks reach 134 IRE.

**The setup pedestal is a variant, not a constant.** 7.5 IRE is NTSC-M (North
America); NTSC-J (Japan) and much modern gear use **zero setup**, where black
and blanking coincide. `--setup <IRE>` selects it, and three things have to
move together or saturation stops matching luma:

| | 7.5 IRE setup | zero setup |
|---|---|---|
| black | 0.3393 V (+7.5 IRE) | 0.2857 V (0 IRE) |
| luma map | `0.075 + 0.925·Y` | `Y` |
| chroma gain | 0.925 | 1.0 |
| 75% bar peak white | 76.88 IRE | 75.00 IRE |

The pedestal costs active range, so chroma takes the same `1 − setup` gain the
luma compression applies. Sync depth, blanking and burst amplitude do not move.
At zero setup `--vbi-black` becomes a no-op, since black *is* blanking.

**Interlace — the one that caused the flagging:**
- 525 lines, two fields of 262.5, field 59.94 Hz, frame 29.97 Hz
- **Horizontal sync runs on ONE continuous grid for the entire frame.** The
  oscillator never re-phases. Every h-sync sits at the same line phase in both
  fields.
- **The half-line offset belongs to the VERTICAL sync alone.** Field 1's pulse
  train starts on a line boundary; field 2's starts half a line later, so the
  vertical ramp begins half a line down and field 2's lines interleave.
- Shifting the h-grid between fields forces the monitor's AFC to slew 31.78 µs
  at every field boundary → **flagging** (top lines bent to one side).
- Per field: 6 equalizing + 6 broad + 6 equalizing half-line pulses (9 lines),
  then 13 blanked lines, then picture.
- Field 2's pulse train ends mid-line → **a half-line of blanking** before
  h-sync resumes on the grid.
- Implementation that works: lay out in half-lines, write all normal lines on
  the even half-line grid first, then paint the two pulse trains over them so
  the truncated half-lines fall out correctly (including the frame wrap).
- Correct validation: **all h-sync line-phases identical** AND vertical sync
  separation **262.5000 lines**. (Testing for a 0.5 h-phase offset *confirms
  the bug* — see the scorecard.)

**Colour:**
- Subcarrier 3.579545 MHz; **fsc = 227.5 × fH**, a half cycle per line, hence
  the **four-field (two-frame) sequence** — 227.5 × 525 = 119,437.5 cycles per
  frame, not an integer.
- Burst: 9 cycles, 40 IRE p-p, on the back porch, phase 180° (−U axis).
- 4× fsc = **14.31818 MS/s** is the standard digital rate. 100 MHz ÷ 7 =
  14.285714 MS/s gives **908 samples/line, line rate −0.0070%**, 3.99 samples
  per subcarrier cycle — the best divider available.
- Better to generate fsc as **exactly 227.5 × our actual line rate** than at
  the nominal 3.579545 MHz: keeps the four-field sequence standard and burst
  coherent, at 249 Hz (−0.007%) absolute error.
- At 847 kS/s the subcarrier is **8.4× above Nyquist** — one sample spans 4.2
  whole subcarrier cycles. Colour is impossible at that rate, full stop.
- 100 MHz cannot divide to the subcarrier: 100 ÷ 28 = 3.571429 MHz, **8116 Hz
  off** (spec ±10 Hz). Irrelevant if you sample at 4× fsc and compute the
  subcarrier into the sample values.

## 6. What is known-good right now

**`funcCustom` interlaced** (default, no flags) — measured clean:
config 0, 54 samples/line, 100 MHz ÷ 118 = 847457.6 S/s, line 15693.66 Hz
(−0.258%), field 59.7854 Hz, **0 ns jitter over 1064 lines**, pulse widths
4.720 / 2.360 / 27.140 µs with **+0/−0 ns spread**, 262.500 lines per field,
h-sync phases all identical. Looks perfect on a CRT.

**Progressive 240p** (`--progressive`) also clean, 82 samples/line default.

**`--deep` interlaced — the best signal available, measured:**
config 3, `funcPlay`, **908 samples/line**, 100 MHz ÷ 7 = 14.285714 MS/s,
476,700 samples preloaded (0.36% of 134 M), zero host involvement.

| | measured | spec |
|---|---|---|
| h-sync | 4.6902 µs | 4.7 |
| equalizing | 2.3100 µs (±10 ns) | 2.3 |
| broad | 27.0900 µs (±10 ns) | 27.1 |
| line period | 63.5600 µs, **20 ns p-p jitter** | 63.5556 |
| line rate | 15733.16 Hz (**−0.0070%**) | 15734.2657 |
| field rate | 59.9338 Hz (**+0.0034%**) | 59.94 |
| lines/field | 262.509 | 262.5 |
| bar edge stability | **0 ns p-p over 1064 lines** | — |
| vertical groups | 4 × exactly 6 broad pulses | 6 |

vs `funcCustom`: 54 → 908 samples/line, line error −0.258% → −0.0070%,
45 → 753 active samples. **This is the platform colour should be built on:**
3.99 samples per subcarrier cycle, and the four-field colour sequence
(953,400 samples) is 0.71% of available play memory.

Minor open item: h-sync spread showed one outlier at −730 ns out of 1121
pulses, almost certainly a pulse truncated by the capture window rather than a
real glitch — everything else measured ±10 ns.

## 7. Measurement lessons (the expensive ones)

1. **Validate against the spec, never against your own implementation.** My
   interlace test asserted the bug as the pass condition.
2. **Sample enough to catch rare events.** A 48-line probe missed stalls that
   occur on 2% of lines and produced a confidently wrong "fixed 440 ns defect,
   independent of rate" conclusion.
3. **Measure with the actual sample period, not the nominal one.** Comparing
   phases against 63.5556 µs when the real line was 63.56 µs manufactured a
   drift that wasn't there.
4. **Read back what the device accepted, then confirm with the scope.**
   `FrequencyGet` reported a rate the hardware wasn't using.
5. **Verify a capability before building on it.** A "no signal on CH1" reading
   on a channel that worked five minutes earlier is a reason to stop, not to
   go hunting sample rates.
6. **Never "optimize" working reference code.** Removing `AnalogOutStatus` from
   the hot loop broke the data path; bounding in-flight depth from `PlayInfo`
   fed nothing at all.
7. **Read the vendor docs and all the examples first.** Both the 4× scale and
   the deep-memory answer were sitting in files already on disk.

## 8. Colour, and what it took to lock a Leader LV5700A

**Working command:**

```
python3 ntsc.py --deep --color --play-scale 1.0 --locked
```

`--locked` = `--level 0.9 --burst-start 4.8 --burst-amp 41 --run-infinite`.

### The two defects that mattered

**1. A ~24-sample gap at every play-run repeat.** Asking for a run of exactly
the sequence duration and repeating it (`RunSet(total/rate)` + `RepeatSet(0)`)
costs re-arm overhead at each boundary. Measured: anomalies every 66,739.7 us
against a 66,738.0 us sequence, one line per sequence running **1.66 us long**.
A CRT rides through it; a waveform monitor's line PLL does not.

* **Fix: `RunSet(0.0)` — an unbounded run with the sequence preloaded**, so
  there is no repeat boundary. Line jitter went **1940 ns -> 40 ns**
  (reference generator: 20 ns).
* Rotating the sequence so the loop boundary sits in blanked VBI
  (`loop_align_offset`) stopped the gap corrupting sync and burst, but did
  not remove it. Keep both.

**2. Output level.** A broadcast generator has a 75 ohm source impedance and
delivers 1 V p-p into a matched load, 2 V p-p unterminated. The AWG's output
impedance is near zero, so it delivers the same either way. Through the same
unterminated chain the reference measured **617.7 mV** of sync depth and we
measured **328.0 mV**. `--level 0.9` is what actually locked the instrument --
*lower* than default, not higher. `--level 2` (1.429 V peak) overdrove the
chain and killed the display entirely.

### Diff against a broadcast generator, same chain

| | reference | ours | |
|---|---|---|---|
| h-sync / eq / broad | 4.78 / 2.26 / 27.06 us | 4.68 / 2.30 / 27.08 | match |
| line period | 63.5600 us | 63.5600 | match |
| line jitter | 20 ns | 40 ns | match |
| field rate | 59.9408 Hz | 59.9359 | match |
| lines per field | 262.479 | 262.500 | match |
| VBI train | 6 eq / 6 broad / 6 eq | identical | match |
| burst start | 4.800 us | 4.820 | match |
| burst amplitude | 40.9 IRE | 37.7 | close |

### Still open

* One line per sequence at ~73% peak white (3 of 2873 lines). Everything else
  scans clean: 0 sync-width, 0 line-period, 0 burst-amplitude anomalies.
* fsc is 250 Hz below nominal when locked to 227.5 x line rate (`--fsc-nominal`
  trades that for SCH walking). Exact NTSC needs a 100.227273 MHz system clock
  -- `DwfParamFrequency` accepts the value but whether the hardware follows is
  unverified (`--sys-clock`).

### Tooling notes

* `--capture F` profiles an external reference with nothing generating;
  `--compare A B` diffs two captures parameter by parameter. This is what
  finally located the problem -- diffing against a known-good signal rather
  than against my assumptions.
* `--scan` reports per-line anomalies **and where they fall**. Averages hide
  a once-per-sequence defect completely; the position and spacing is the
  signal.
* Do not trust `h phase spread` over long captures -- it is dominated by
  line-period estimation error and reported the *reference* as re-phasing half
  a line. Burst frequency from a single-burst DFT is also meaningless: the
  2.45 us burst gives a ~400 kHz main lobe.
