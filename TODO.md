# TODO

## Cleanup pass — the big one

This project accumulated a lot of experimental scaffolding while diagnosing
the streaming problem. Most of it earned its place as a measurement, but not
all of it should survive in a tool someone else has to read.

**Flags and functions added during the investigation.** Each needs a verdict:
keep as a documented diagnostic, keep but mark experimental, or delete.

| | verdict needed |
|---|---|
| `--transport` | **keep** — round-trip timings are load-bearing facts |
| `--feed-matrix` | **keep** — the config × recipe table is the reason streaming is understood |
| `--memory-probe` | keep? only ever returned a negative result (`analogout-memory` does nothing) |
| `--feed-tune` | keep? its verdict was misleading until the threshold was tightened |
| `--stream-probe` | **probably delete** — superseded by `--feed-tune`, and its original verdict was wrong |
| `--stream-square` / `square_integrity` | **keep** — the only instrument that can validate a stream |
| `square_deep_control` | **keep** — the hardware control; badly named now that it uses `funcSquare`, not deep memory |
| `--stream-pad` / `--stream-pad-every` | keep as a documented dead end, or delete |
| `--stream-guard` / `--stream-resync` | keep as documented dead ends, or delete — both default off and made things worse |
| `--stream-doc` | **keep** — the SDK's own loop, useful for comparison |
| `--stream-static` / `--stream-align` | keep? both were isolation tests that came back negative |
| `--stream-rate-check` | **probably delete** — largely redundant with the `--transport` timings |
| `--dual-probe` / `--dual-change` / `--dual-sacrificial` | keep the probe, drop `--dual-sacrificial` (measured harmful) |
| `locate_change` | **keep**, but it is now a timeline printer and the name no longer fits |

**Other cleanup:**

- `ntsc.py` is well past 4,000 lines. Consider splitting measurement tooling
  from generation, or at least grouping the probes.
- 48 pre-existing lines plus some added this session exceed the file's 79-column
  style. Worth one sweep.
- `--motion-stream` and its whole feed path (`stream_feed`, `feed_amount`,
  `stream_chunk`, `make_padded_filler`) are retained for a path that cannot
  produce clean video on this device. Decide whether that stays as a reference
  implementation or goes.
- Several functions carry long comments narrating *why* a wrong approach was
  wrong. Valuable context, but some belongs in `NTSC_FINDINGS.md` rather than
  inline.
- `--help` lists 40+ flags flat. Group them: generation, measurement,
  diagnostics, experiments.
- Check for dead names and uncalled functions again, as was done before the
  first commit.

## Open questions

- **Report to Digilent.** Two reproducible defects, about 20 s each with
  `--stream-doc --stream-square 100`: one 32-sample block lost per
  32,768-sample FIFO revolution under `funcPlayPattern`, and `cdDataLost` /
  `cdDataCorrupted` reading **zero** throughout. The silent counters are
  arguably the worse bug.
- **Ball trajectory periodicity.** A constant-velocity bouncing ball repeats
  (x 4.76 s, y 6.45 s at the defaults). Setting the y speed to x/φ would make
  it visibly endless. Undecided whether a predictable pattern is preferable
  on a bench.
- **Phase-timed applies for `--dual`.** Not needed — the picture is clean —
  but it would reduce the ball's position discontinuity at a swap to near
  zero if that ever matters.
- **Edge overshoot** on all transitions, suspected unterminated-chain
  reflections, never proven. Edge shaping was tried and made it worse.
- **One line per sequence at ~73% peak white** in the original static `--deep`
  path, roughly 90% through the sequence. Never explained.
- **`--sys-clock 100227273`** for exact NTSC timing is accepted by
  `DwfParamFrequency` but was never verified on hardware.
