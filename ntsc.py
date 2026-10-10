#!/usr/bin/env python3
"""
NTSC composite video generator and analyser for the Digilent Analog Discovery
Pro 2230, driven through the WaveForms SDK.

Generates a standards-shaped NTSC signal on AWG channel 1 (W1) and, with a
W1 -> CH1 jumper, measures what actually comes out using the device's own
oscilloscope. It can also profile a reference signal from another generator
and diff the two parameter by parameter.

    python3 ntsc.py --deep --color --play-scale 1.0 --locked
        75% EIA colour bars, 525-line interlaced, four-field sequence.
        908 samples/line at 14.285714 MS/s, line rate within 0.007%,
        subcarrier locked to 227.5 x line rate. This is the good path.

    python3 ntsc.py
        Monochrome bars via funcCustom's looping 32 Ki buffer. Works with no
        setup but is limited to 54 samples/line and 0.258% on line rate.

Three generation paths, in order of quality:

  --deep    Preloads the whole sequence into the device's analog-out play
            memory (134 M samples) and plays it with an unbounded run, so
            there is no host involvement and no repeat boundary. Full rate,
            colour capable. Needs a configuration that reports play memory.
  --stream  Feeds chunks via PlayData. Measured ~5.8 MS/s sustained, which is
            short of the 14.3 MS/s colour needs. Superseded by --deep for
            anything that fits in play memory (9.4 s of video).
  default   funcCustom loops a 32 Ki buffer entirely in hardware. Rock solid,
            but the buffer caps samples-per-line.

Measurement:

  --analyze   Conformance report on CH1: levels, sync depth, burst placement
              and amplitude, breezeway, fsc/fH lock.
  --scan      Per-line metrics across several sequences, reporting where
              anomalies fall and whether they are periodic. Averages hide a
              once-per-sequence defect completely.
  --verify    Line period, jitter, bar-edge stability, field structure.
  --capture F Profile an external reference with nothing generating.
  --compare A B   Diff two captures, listing every parameter that differs.

Wiring: W1 -> composite centre conductor, GND -> shield. A 75 ohm resistor in
series with W1 (plus --series-75) makes the output behave like a real video
source: 286 mV of sync depth into a terminated load, 572 mV unterminated.

Device quirks this works around are documented in NTSC_FINDINGS.md -- notably
that the AWG quantises its sample rate to an integer divider of 100 MHz and
stalls whole samples otherwise, and that play memory is reported by
FDwfAnalogOutNodePlayInfo rather than FDwfAnalogOutNodeDataInfo.

Run with --help for the full flag list.
"""

import ctypes
import gc
import math
import sys
import time

# ----------------------------------------------------------------------------
# WaveForms SDK constants (verified against dwf.h)
# ----------------------------------------------------------------------------
funcCustom = ctypes.c_int(30)
AnalogOutNodeCarrier = ctypes.c_int(0)
DwfAnalogOutIdleInitial = ctypes.c_int(2)
DECIAnalogInBufferSize = ctypes.c_int(7)
DECIAnalogOutBufferSize = ctypes.c_int(8)
DECIAnalogOutChannelCount = ctypes.c_int(2)
DECIDigitalOutBufferSize = ctypes.c_int(10)
DECIAnalogOutPlaySize = ctypes.c_int(13)
funcPlay = ctypes.c_int(31)
funcPlayPattern = ctypes.c_int(29)
funcCustomPattern = ctypes.c_int(28)
DwfDigitalOutTypeCustom = ctypes.c_int(1)
DwfDigitalOutIdleLow = ctypes.c_int(1)
DwfDigitalOutTypePlay = ctypes.c_int(5)
DwfParamDigitalVoltage = ctypes.c_int(13)
DwfParamDACFrequency = ctypes.c_int(25)
DwfParamFrequency = ctypes.c_int(8)
acqmodeSingle = ctypes.c_int(0)
filterDecimate = ctypes.c_int(0)
trigsrcNone = ctypes.c_ubyte(0)
DwfStateDone = 2
DwfStateRunning = 3
DwfStateArmed = 1
DwfStateWait = 7

# ----------------------------------------------------------------------------
# NTSC timing (RS-170, monochrome)
# ----------------------------------------------------------------------------
LINE_FREQ_HZ = 15734.2657
LINE_US = 1e6 / LINE_FREQ_HZ       # 63.5556 us
HALF_LINE_US = LINE_US / 2.0

H_SYNC_US = 4.7
BACK_PORCH_US = 4.7                # no colorburst: monochrome
FRONT_PORCH_US = 1.5
ACTIVE_US = LINE_US - (H_SYNC_US + BACK_PORCH_US + FRONT_PORCH_US)

EQ_PULSE_US = 2.3                  # equalizing pulse, one per half line
V_SYNC_PULSE_US = 27.1             # broad vertical pulse, one per half line

# Vertical interval: 3 lines of equalizing pulses, 3 of broad vertical sync,
# 3 more of equalizing, then plain blanked lines. The rest is picture.
EQ_LINES = 3
VSYNC_LINES = 3
POST_EQ_LINES = 3
EXTRA_BLANK_LINES = 13
VBI_LINES = EQ_LINES + VSYNC_LINES + POST_EQ_LINES + EXTRA_BLANK_LINES

# Broadcast NTSC is 525 lines interlaced: two fields of 262.5 lines at
# 59.94 Hz, the half line being what offsets field 2 between field 1's lines.
# Both fields must sit in the one looping buffer, which halves the samples per
# line versus progressive -- the price of a standards-valid signal.
INTERLACED = True                  # --progressive for 262-line 240p instead
FIELD_HALF_LINES = 525             # 262.5 lines per field
TOTAL_LINES = 262                  # progressive line count, --lines
PATTERN = "bars8"                  # overridable with --pattern

# Prefer the most samples per line (sharpest picture) over the most accurate
# sync pulse widths. Off by default: a sync separator discriminates pulses by
# width, so bad width quantization is what stops a monitor or a vectorscope
# locking -- far more than a small line-rate error. --max-res to flip it.
MAX_RES = False

# The AWG quantizes its sample rate to an integer divider of this clock and,
# when the requested rate is not achievable, runs at the nearest faster one
# and stalls a whole sample periodically to hold the average -- which is the
# ragged-edge jitter. DwfParamDACFrequency is not supported on the ADP2230,
# so this is inferred from measurement: at 54 samples/line the realized
# sample period was 1170.4 ns = 100 MHz / 117 to within 0.03%. Landing the
# request exactly on 100 MHz / N should remove the need to stall at all.
AWG_CLOCK_HZ = 100e6

# --save-capture <path>: write the raw --verify scope capture as CSV.
SAVE_CAPTURE = None

# ----------------------------------------------------------------------------
# Levels, volts at the coax, sync tip referenced to 0 V.
# 1 V p-p over 140 IRE: sync -40, blank 0, black +7.5, white +100 IRE.
# ----------------------------------------------------------------------------
IRE = 1.0 / 140.0
V_SYNC = 0.0
V_BLANK = 40 * IRE                 # 0.2857 V
# Setup pedestal. 7.5 IRE is NTSC-M (North America); 0 is zero setup, used by
# NTSC-J (Japan) and by much modern gear, where black sits at blanking. --setup
# changes it, and set_setup_ire() recomputes everything derived from it.
SETUP_IRE = 7.5
V_BLACK = (40 + SETUP_IRE) * IRE   # 0.3393 V at 7.5 IRE setup
V_WHITE = 140 * IRE                # 1.0000 V

V_MID = (V_SYNC + V_WHITE) / 2.0   # 0.5 V -> AWG offset
V_SPAN = (V_WHITE - V_SYNC) / 2.0  # 0.5 V -> AWG amplitude (peak, 1 Vpp)

# Set True only if you add a 75 ohm resistor in series with W1. The AWG is a
# low-impedance buffered output, so driving the TV's 75 ohm input directly
# already gives the correct 1 V p-p; a series match would halve it.
SOURCE_SERIES_75R = False

# Output level scale. A broadcast generator has a 75 ohm source impedance and
# delivers 1 V p-p into a matched load -- 2 V p-p unterminated. The AWG has a
# near-zero source impedance and delivers the same level either way, so in an
# unterminated chain the reference measures twice what we do: 617.7 mV of sync
# depth against our 328.3. --level 2 matches it.
LEVEL = 1.0

# How the preloaded sequence is made to run forever. "repeat" asks for a run
# of exactly the sequence duration and repeats it, which costs ~24 samples of
# re-arm at every boundary -- measured as one line per sequence running 1.66 us
# long, against a reference generator's 20 ns of jitter. "infinite" asks for an
# unbounded run instead, so there is no repeat boundary to pay for.
RUN_INFINITE = False

# Load N back-to-back copies of the sequence, so any per-run overhead is paid
# N times less often. Costs host memory: each copy is 7.6 MB.
COPIES = 1

# Standard composite DC convention: blanking at 0 V, sync tip at -286 mV,
# white at +714 mV. This script historically built from sync tip = 0 V, which
# puts the whole signal 286 mV high. Most inputs clamp on the back porch so it
# usually does not matter, but it is a real non-compliance for a DC-coupled
# instrument. --sync-tip-zero restores the old behaviour.
STD_DC = True

# Vertical-interval lines at blanking (0 IRE) per spec, or at black (7.5 IRE)
# as this script originally emitted. Changed partway through debugging, so it
# is one of the few differences from the configuration that last locked on the
# waveform monitor. --vbi-black restores the old behaviour.
VBI_BLACK = False

# --sys-clock: try to retune the device system clock. 7 * 4fsc =
# 100,227,273 Hz makes divider 7 give exactly 4fsc, so at 910
# samples/line the line rate, the subcarrier and the 227.5 ratio are
# all exact at once -- if the hardware honours the setting.
SYS_CLOCK = None


# ----------------------------------------------------------------------------
# Waveform synthesis
# ----------------------------------------------------------------------------
def active_lines():
    return TOTAL_LINES - VBI_LINES


PATTERNS = ("bars8", "gray8", "white")


def active_level(frac):
    """Luma for a point `frac` (0..1) across the active part of a line."""
    if PATTERN == "white":
        return V_WHITE                   # flat 100 IRE level reference
    idx = min(int(frac * 8), 7)
    if PATTERN == "gray8":
        return V_WHITE - (V_WHITE - V_BLACK) * (idx / 7.0)
    return V_WHITE if idx % 2 == 0 else V_BLACK


def picture_line_level(t_us, blanked):
    if t_us < H_SYNC_US:
        return V_SYNC
    if t_us < H_SYNC_US + BACK_PORCH_US:
        return V_BLANK
    start = H_SYNC_US + BACK_PORCH_US
    if t_us < start + ACTIVE_US:
        return V_BLACK if blanked else active_level((t_us - start) / ACTIVE_US)
    return V_BLANK                                   # front porch


def half_line_level(t_us, pulse_us):
    """A line built from two identical half-line pulses."""
    return V_SYNC if (t_us % HALF_LINE_US) < pulse_us else V_BLANK


def line_level(line_idx, t_us):
    if line_idx < EQ_LINES:
        return half_line_level(t_us, EQ_PULSE_US)
    line_idx -= EQ_LINES
    if line_idx < VSYNC_LINES:
        return half_line_level(t_us, V_SYNC_PULSE_US)
    line_idx -= VSYNC_LINES
    if line_idx < POST_EQ_LINES:
        return half_line_level(t_us, EQ_PULSE_US)
    line_idx -= POST_EQ_LINES
    return picture_line_level(t_us, blanked=line_idx < EXTRA_BLANK_LINES)


def kind_of(line_idx):
    """Lines of the same kind are identical, so they can be built once."""
    if line_idx < EQ_LINES:
        return "eq"
    if line_idx < EQ_LINES + VSYNC_LINES:
        return "vsync"
    if line_idx < EQ_LINES + VSYNC_LINES + POST_EQ_LINES:
        return "eq2"
    if line_idx < VBI_LINES:
        return "blank"
    return "active"


def half_line_samples(spl, pulse_us):
    """A line of two identical half-line pulses, on the sample grid.

    Built from spl//2 so both half-lines are the same length; deriving them
    from time instead makes the two pulses differ by a sample whenever the
    half-line is not a whole number of samples.
    """
    half_n = spl // 2
    us = LINE_US / spl
    pulse_n = round(pulse_us / us)
    half = [V_SYNC] * pulse_n + [V_BLANK] * (half_n - pulse_n)
    line = half + half
    return line + [V_BLANK] * (spl - len(line))


def picture_line_samples(spl, blanked):
    """A normal line built on the sample grid rather than on time.

    At 125 samples/line a bar is 12.9 samples wide, so deriving bar edges from
    time leaves them at fractional sample positions and the eight bars come out
    13/13/13/13/12/13/13/13 samples -- visibly uneven. Splitting the active
    sample count instead keeps every bar within one sample of its neighbours.
    """
    us = LINE_US / spl
    sync_n = round(H_SYNC_US / us)
    bp_n = round(BACK_PORCH_US / us)
    fp_n = round(FRONT_PORCH_US / us)
    active_n = spl - sync_n - bp_n - fp_n
    line = [V_SYNC] * sync_n + [V_BLANK] * bp_n
    for i in range(active_n):
        if blanked:
            # Vertical-interval lines sit at blanking (0 IRE), not black.
            # The 7.5 IRE setup pedestal applies to active picture only, and
            # putting it here lifts the whole vertical interval 7.5 IRE,
            # which is DC the monitor's clamp has to absorb.
            line.append(V_BLACK if VBI_BLACK else V_BLANK)
        else:
            line.append(active_level((i * 8 // active_n) / 8.0 + 1e-9))
    line += [V_BLANK] * fp_n
    return line


def frame_lines():
    """Lines held in the buffer: 525 interlaced, else the progressive count."""
    return 525 if INTERLACED else TOTAL_LINES


def build_interlaced_volts(spl):
    """One 525-line interlaced frame, in volts.

    Horizontal sync lives on a SINGLE continuous grid for the whole frame --
    one pulse every line period, at every even half-line. The horizontal
    oscillator must never re-phase between fields; if it does, the monitor's
    AFC has to slew half a line at each field boundary and the top of the
    picture bends ("flagging").

    Interlace comes from the VERTICAL sync phase instead: field 1's pulse
    train starts on a line boundary, field 2's starts half a line later, so
    the vertical ramp begins half a line down and field 2's lines fall
    between field 1's.
    """
    if spl % 2:
        raise RuntimeError("interlace needs an even samples-per-line")
    half = spl // 2
    total = 2 * FIELD_HALF_LINES * half
    us = LINE_US / spl

    eq_n = round(EQ_PULSE_US / us)
    br_n = round(V_SYNC_PULSE_US / us)
    eq_half = [V_SYNC] * eq_n + [V_BLANK] * (half - eq_n)
    br_half = [V_SYNC] * br_n + [V_BLANK] * (half - br_n)
    active = picture_line_samples(spl, False)
    blanked = picture_line_samples(spl, True)

    frame = [V_BLANK] * total

    def put(at, seg):
        for j, v in enumerate(seg):
            frame[(at + j) % total] = v

    # Every line position on the one grid. The 13 lines following each field's
    # pulse train stay blanked; the pulse trains themselves overwrite below.
    blank_hl = set(range(18, 18 + 2 * EXTRA_BLANK_LINES, 2))
    blank_hl |= set(range(FIELD_HALF_LINES + 19,
                          FIELD_HALF_LINES + 19 + 2 * EXTRA_BLANK_LINES, 2))
    for hl in range(0, 2 * FIELD_HALF_LINES, 2):
        put(hl * half, blanked if hl in blank_hl else active)

    # 6 equalizing, 6 broad, 6 equalizing half-line pulses per field. Field 2
    # starts on an odd half-line, which is the interlace offset.
    for start in (0, FIELD_HALF_LINES):
        for k in range(18):
            put((start + k) * half, eq_half if (k < 6 or k >= 12) else br_half)

    # Field 2's train ends mid-line, so blank the remaining half-line rather
    # than leaving the tail of a picture line there.
    put((FIELD_HALF_LINES + 18) * half, [V_BLANK] * half)
    return frame


def build_frame_volts(samples_per_line):
    """One frame as volts at the coax, sync tip referenced to 0 V.

    Lines of the same kind are built once and reused, so all picture lines are
    identical and bar edges are exactly vertical.
    """
    if INTERLACED:
        return build_interlaced_volts(samples_per_line)
    cache = {}
    frame = []
    for line in range(TOTAL_LINES):
        k = kind_of(line)
        if k not in cache:
            if k in ("blank", "active"):
                cache[k] = picture_line_samples(samples_per_line, k == "blank")
            else:
                pulse = V_SYNC_PULSE_US if k == "vsync" else EQ_PULSE_US
                cache[k] = half_line_samples(samples_per_line, pulse)
        frame.extend(cache[k])
    return frame


def build_frame(samples_per_line):
    """The same frame normalized to -1..+1, as funcCustom requires."""
    return [(v - V_MID) / V_SPAN
            for v in build_frame_volts(samples_per_line)]


def describe(spl, rate=None):
    us = 1e6 / rate if rate else LINE_US / spl
    rate = rate or LINE_FREQ_HZ * spl
    return (
        f"{spl} samples/line ({us:.3f} us/sample, "
        f"{rate / 1e6:.3f} MS/s)\n"
        f"      h-sync {H_SYNC_US / us:.1f} smp, eq {EQ_PULSE_US / us:.1f} smp, "
        f"serration {(HALF_LINE_US - V_SYNC_PULSE_US) / us:.1f} smp, "
        f"bar {ACTIVE_US / 8 / us:.1f} smp"
    )


# ----------------------------------------------------------------------------
# Timing selection
# ----------------------------------------------------------------------------
def sync_timing_error(spl):
    """Worst relative error in the pulse widths a sync separator measures.

    Horizontal sync is 4.7 us, equalizing pulses 2.3 us, broad pulses 27.1 us,
    and the serration left over in a half-line should come back to 4.7 us.
    All four get rounded onto the sample grid, and the worst of them governs
    whether downstream sync separation works.
    """
    us = LINE_US / spl
    worst = 0.0
    for target in (H_SYNC_US, EQ_PULSE_US, V_SYNC_PULSE_US):
        n = round(target / us)
        if n < 1:
            return 1e9
        worst = max(worst, abs(n * us / target - 1))
    serration = spl // 2 - round(V_SYNC_PULSE_US / us)
    if serration < 1:
        return 1e9
    return max(worst, abs(serration * us / H_SYNC_US - 1))


def report_timing(spl, rate=None):
    """Print the pulse widths actually produced, against spec.

    Uses the real sample period when known: the divider's period differs from
    the nominal LINE_US/spl by whatever the line-rate error is.
    """
    us = 1e6 / rate if rate else LINE_US / spl
    rows = (("h-sync", H_SYNC_US, round(H_SYNC_US / us)),
            ("equalizing", EQ_PULSE_US, round(EQ_PULSE_US / us)),
            ("broad", V_SYNC_PULSE_US, round(V_SYNC_PULSE_US / us)),
            ("serration", H_SYNC_US, spl // 2 - round(V_SYNC_PULSE_US / us)))
    parts = [f"{name} {n * us:.3f}us({(n * us / t - 1) * 100:+.1f}%)"
             for name, t, n in rows]
    print("      pulse widths: " + ", ".join(parts))


def choose_timing(buffer_max, dac_hz, spl_force=None):
    """Pick samples-per-line so the needed sample rate is close to an exact
    integer divider of the AWG DAC clock.

    The AWG steps the buffer at DAC_clock / N for integer N. If the rate we
    ask for is not one of those, the realized line rate sits off 15734 Hz, so
    search for an N that lands close while keeping the sample count high.
    """
    room = buffer_max // frame_lines()
    if spl_force:
        if spl_force > room:
            raise RuntimeError(
                f"--spl {spl_force} x {frame_lines()} lines exceeds the "
                f"{buffer_max} sample AWG buffer (max {room}).")
        lo = hi = spl_force
    else:
        hi = min(1024, room)
        lo = max(32, min(48, hi))
    if hi < 32:
        raise RuntimeError(
            f"AWG buffer of {buffer_max} samples gives only {room} "
            f"samples/line for {frame_lines()} lines; too coarse to form sync.")

    best = None
    # Even counts only: the vertical interval splits each line in half,
    # and interlace needs a whole number of samples per half-line.
    for spl in range(lo + (lo % 2), hi + 1, 2):
        want = LINE_FREQ_HZ * spl
        if dac_hz:
            n = max(1, round(dac_hz / want))
            rate = dac_hz / n
        else:
            n, rate = 0, want
        err = abs(rate / spl / LINE_FREQ_HZ - 1)
        if MAX_RES:
            key = (-spl, round(err, 9))
        else:
            key = (round(sync_timing_error(spl), 4), round(err, 9), -spl)
        if best is None or key < best[0]:
            best = (key, spl, rate, n, err)
    _, spl, rate, n, err = best
    return spl, rate, n, err


# ----------------------------------------------------------------------------
# Offline check
# ----------------------------------------------------------------------------
def runs_below(samples, dt_us, thresh):
    """(start_us, width_us) for every run of samples below `thresh`."""
    out, start = [], None
    for i, s in enumerate(samples):
        if s < thresh and start is None:
            start = i
        elif s >= thresh and start is not None:
            out.append((start * dt_us, (i - start) * dt_us))
            start = None
    if start is not None:
        out.append((start * dt_us, (len(samples) - start) * dt_us))
    return out


def dump(spl, path="ntsc_frame.csv"):
    frame = build_frame(spl)
    print(f"Frame: {describe(spl)}")
    print(f"      {frame_lines()} lines, {len(frame)} samples, "
          f"{LINE_FREQ_HZ / frame_lines():.4f} Hz frame rate")
    if INTERLACED:
        print(f"      interlaced: 2 fields of 262.5 lines, "
              f"{LINE_FREQ_HZ / 262.5:.3f} Hz field rate")
        print(f"      per field: 6 eq + 6 broad + 6 eq half-line pulses, "
              f"{EXTRA_BLANK_LINES} blank lines, "
              f"{254 - EXTRA_BLANK_LINES} active")
    else:
        print(f"      progressive layout: {EQ_LINES} eq + {VSYNC_LINES} vsync "
              f"+ {POST_EQ_LINES} eq + {EXTRA_BLANK_LINES} blank + "
              f"{active_lines()} active")

    lo, hi = min(frame), max(frame)
    print(f"      normalized {lo:+.4f}..{hi:+.4f} -> "
          f"{V_MID + V_SPAN * lo:.4f} V..{V_MID + V_SPAN * hi:.4f} V")
    assert lo >= -1.0 and hi <= 1.0, "data must stay within -1..+1"
    assert len(frame) == frame_lines() * spl

    dt = LINE_US / spl
    thresh = ((V_SYNC + V_BLANK) / 2.0 - V_MID) / V_SPAN
    widths = [w for _, w in runs_below(frame, dt, thresh)]
    print(f"      {len(widths)} sync pulses, widths (us): "
          f"{sorted({round(w, 2) for w in widths})}")

    with open(path, "w") as f:
        f.write("sample,volts\n")
        for i, s in enumerate(frame):
            f.write(f"{i},{V_MID + V_SPAN * s:.5f}\n")
    print(f"Wrote {path}")


# ----------------------------------------------------------------------------
# Hardware
# ----------------------------------------------------------------------------
def load_dwf():
    if sys.platform.startswith("win"):
        return ctypes.cdll.dwf
    if sys.platform.startswith("darwin"):
        return ctypes.cdll.LoadLibrary("/Library/Frameworks/dwf.framework/dwf")
    return ctypes.cdll.LoadLibrary("libdwf.so")


def last_error(dwf):
    buf = ctypes.create_string_buffer(512)
    dwf.FDwfGetLastErrorMsg(buf)
    return buf.value.decode(errors="replace").strip() or "(no detail)"


def check(dwf, rc, what):
    if rc == 0:
        raise RuntimeError(f"{what} failed: {last_error(dwf)}")


def pick_config(dwf, for_stream=False, force=None):
    """Choose a device configuration.

    For funcCustom, the whole frame must fit the AWG buffer, so the deepest
    one wins. For funcPlay the requirement is different and counter-intuitive:
    measured on this unit, only the configurations reporting
    DECIAnalogOutPlaySize == 0 actually drive the output. Those advertising a
    67 M or 134 M play FIFO accept data and consume it at the correct rate
    with zero lost or corrupted samples, yet leave W1 parked on sample 0.
    """
    ndev = ctypes.c_int()
    check(dwf, dwf.FDwfEnum(ctypes.c_int(0), ctypes.byref(ndev)), "FDwfEnum")
    if ndev.value == 0:
        raise RuntimeError("No Digilent device found. Close WaveForms and replug.")

    name, sn = ctypes.create_string_buffer(32), ctypes.create_string_buffer(32)
    dwf.FDwfEnumDeviceName(ctypes.c_int(0), name)
    dwf.FDwfEnumSN(ctypes.c_int(0), sn)
    print(f"  device 0: {name.value.decode()} {sn.value.decode()}")

    ncfg = ctypes.c_int()
    check(dwf, dwf.FDwfEnumConfig(ctypes.c_int(0), ctypes.byref(ncfg)),
          "FDwfEnumConfig")

    best_idx, best_key = -1, None
    for i in range(ncfg.value):
        awg, chans, scope = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        dwf.FDwfEnumConfigInfo(ctypes.c_int(i), DECIAnalogOutBufferSize,
                               ctypes.byref(awg))
        dwf.FDwfEnumConfigInfo(ctypes.c_int(i), DECIAnalogOutChannelCount,
                               ctypes.byref(chans))
        dwf.FDwfEnumConfigInfo(ctypes.c_int(i), DECIAnalogInBufferSize,
                               ctypes.byref(scope))
        dout, play = ctypes.c_int(), ctypes.c_int()
        dwf.FDwfEnumConfigInfo(ctypes.c_int(i), DECIDigitalOutBufferSize,
                               ctypes.byref(dout))
        dwf.FDwfEnumConfigInfo(ctypes.c_int(i), DECIAnalogOutPlaySize,
                               ctypes.byref(play))
        print(f"  config {i}: AWG buffer {awg.value}, {chans.value} AWG ch, "
              f"scope buffer {scope.value}, digital-out {dout.value}, "
              f"AWG play {play.value}")
        if chans.value < 1:
            continue
        if for_stream:
            if play.value != 0:
                continue                      # advertises a FIFO, stays flat
            key = (awg.value, scope.value)
        else:
            key = (awg.value,)
        if best_key is None or key > best_key:
            best_idx, best_key = i, key

    if force is not None:
        if not 0 <= force < ncfg.value:
            raise RuntimeError(f"--config {force} out of range "
                               f"(0..{ncfg.value - 1}).")
        print(f"  -> config {force} (forced)")
        return force
    if best_idx < 0:
        raise RuntimeError(
            "No configuration supports streaming analog output."
            if for_stream else
            "No configuration with an analog output channel.")
    print(f"  -> config {best_idx} "
          f"({'streaming-capable, ' if for_stream else ''}key {best_key})")
    return best_idx


def report_output_window(dwf, hdwf, ch, amplitude, offset):
    """Print what the AWG actually accepted, against what we asked for.

    Amplitude and offset are clamped to what the selected configuration
    supports, so a request is not a measurement. Must be called AFTER
    FDwfAnalogOutConfigure: with FDwfDeviceAutoConfigureSet(0) nothing is
    applied until then, and a Get before it returns the device's previous
    state -- which reads exactly like a clamp and is not one. A mismatch on a
    value that lies inside the range the device itself reports is therefore a
    stale readback, never a clamp; only --analyze settles what came out.

    These are the device's own output figures, before any series resistor and
    before the chain loads it. Do not assume the series divider is 0.5 -- it
    is whatever the chain actually presents, so measure it.
    """
    lim = {}
    for name, info, get in (
            ("amplitude", dwf.FDwfAnalogOutNodeAmplitudeInfo,
             dwf.FDwfAnalogOutNodeAmplitudeGet),
            ("offset", dwf.FDwfAnalogOutNodeOffsetInfo,
             dwf.FDwfAnalogOutNodeOffsetGet)):
        lo, hi, got = ctypes.c_double(), ctypes.c_double(), ctypes.c_double()
        check(dwf, info(hdwf, ch, AnalogOutNodeCarrier,
                        ctypes.byref(lo), ctypes.byref(hi)), name + "Info")
        check(dwf, get(hdwf, ch, AnalogOutNodeCarrier, ctypes.byref(got)),
              name + "Get")
        lim[name] = (lo.value, hi.value, got.value)

    clamped = False
    for name, want in (("amplitude", amplitude), ("offset", offset)):
        lo, hi, got = lim[name]
        print(f"      {name:9} asked {want:+.4f} V -> device {got:+.4f} V "
              f"(range {lo:+.3f}..{hi:+.3f})")
        if abs(want - got) <= 1e-3:
            continue
        if lo - 1e-9 <= want <= hi + 1e-9:
            # The request is inside the range the device itself reports, so it
            # cannot have been clamped -- the readback is stale, not the
            # output. Measure the output instead of believing this number.
            print(f"      NOTE: {name} readback disagrees by "
                  f"{abs(want - got) * 1000:.1f} mV but {want:+.4f} is inside "
                  f"the device's own {lo:+.3f}..{hi:+.3f} range, so this is a "
                  f"stale readback, not a clamp. Trust --analyze.")
            continue
        clamped = True
        print(f"      WARNING: {name} clamped by "
              f"{abs(want - got) * 1000:.1f} mV -- {want:+.4f} V is outside "
              f"the device's {lo:+.3f}..{hi:+.3f} range. The sample data is "
              f"fine; the output window is not, so levels will read low.")

    amp, off = lim["amplitude"][2], lim["offset"][2]
    tail = (" -> half of that into a terminated 75 ohm load"
            if SOURCE_SERIES_75R else "")
    print(f"      at W1: {off - amp:+.4f} V .. {off + amp:+.4f} V "
          f"({2 * amp * 1000:.1f} mV p-p){tail}")
    return not clamped


def start_awg(dwf, hdwf, ch, spl, rate, quiet=False):
    # Same voltage handling as the deep path, so the only difference between
    # them is the playback mechanism -- not the DC level or the window.
    volts = build_frame_volts(spl)
    total = len(volts)
    frame_hz = rate / total
    if STD_DC:
        volts = [v - V_BLANK for v in volts]
    vlo, vhi = min(volts), max(volts)
    offset = (vlo + vhi) / 2.0
    amplitude = (vhi - vlo) / 2.0
    cbuf = (ctypes.c_double * total)()
    cbuf[:] = [(v - offset) / amplitude for v in volts]
    scale_v = LEVEL * (2.0 if SOURCE_SERIES_75R else 1.0)
    amplitude *= scale_v
    offset *= scale_v

    check(dwf, dwf.FDwfAnalogOutNodeEnableSet(hdwf, ch, AnalogOutNodeCarrier,
                                              ctypes.c_int(1)), "NodeEnableSet")
    check(dwf, dwf.FDwfAnalogOutNodeFunctionSet(hdwf, ch, AnalogOutNodeCarrier,
                                                funcCustom), "NodeFunctionSet")
    check(dwf, dwf.FDwfAnalogOutNodeDataSet(hdwf, ch, AnalogOutNodeCarrier,
                                            cbuf, ctypes.c_int(total)),
          "NodeDataSet")
    # In funcCustom the frequency is the repetition rate of the whole buffer,
    # i.e. the frame rate -- not the line rate and not a sample rate.
    check(dwf, dwf.FDwfAnalogOutNodeFrequencySet(hdwf, ch, AnalogOutNodeCarrier,
                                                 ctypes.c_double(frame_hz)),
          "NodeFrequencySet")
    check(dwf, dwf.FDwfAnalogOutNodeAmplitudeSet(hdwf, ch, AnalogOutNodeCarrier,
                                                 ctypes.c_double(amplitude)),
          "NodeAmplitudeSet")
    check(dwf, dwf.FDwfAnalogOutNodeOffsetSet(hdwf, ch, AnalogOutNodeCarrier,
                                              ctypes.c_double(offset)),
          "NodeOffsetSet")
    check(dwf, dwf.FDwfAnalogOutNodePhaseSet(hdwf, ch, AnalogOutNodeCarrier,
                                             ctypes.c_double(0.0)),
          "NodePhaseSet")
    check(dwf, dwf.FDwfAnalogOutRunSet(hdwf, ch, ctypes.c_double(0.0)), "RunSet")
    check(dwf, dwf.FDwfAnalogOutRepeatSet(hdwf, ch, ctypes.c_int(0)), "RepeatSet")
    dwf.FDwfAnalogOutIdleSet(hdwf, ch, DwfAnalogOutIdleInitial)
    check(dwf, dwf.FDwfAnalogOutConfigure(hdwf, ch, ctypes.c_int(1)),
          "AnalogOutConfigure")
    report_output_window(dwf, hdwf, ch, amplitude, offset)

    got = ctypes.c_double()
    dwf.FDwfAnalogOutNodeFrequencyGet(hdwf, ch, AnalogOutNodeCarrier,
                                      ctypes.byref(got))
    if quiet:
        return total, frame_hz
    print(f"      buffer {total} samples, frame requested {frame_hz:.5f} Hz, "
          f"device reports {got.value:.5f} Hz")
    print(f"      implied line rate {got.value * frame_lines():.2f} Hz "
          f"({(got.value * frame_lines() / LINE_FREQ_HZ - 1) * 100:+.3f}%)")
    print(f"      amplitude {amplitude:.4f} V, offset {offset:.4f} V, "
          f"blanking at {0.0 if STD_DC else V_BLANK:.3f} V "
          f"{'(standard)' if STD_DC else '(sync tip at 0 V)'}")
    return total, frame_hz


# ----------------------------------------------------------------------------
# AWG streaming path (--stream)
#
# funcCustom loops a 32 Ki buffer, which caps the sample rate near 850 kS/s:
# 54 samples per line, 2.6% sync-width error, 0.258% line-rate error, and no
# possibility of a 3.58 MHz colour subcarrier (8.4x above Nyquist there).
#
# funcPlay streams from the host instead. The device reports a 67,108,864
# sample play FIFO -- 4.7 seconds at 14.3 MS/s -- so host scheduling jitter
# is a non-issue, and PlayStatus returns lost/corrupted counters so underruns
# are measured rather than assumed. Nothing loops in hardware, so the
# four-field colour sequence stops being a buffer-depth problem too.
# ----------------------------------------------------------------------------
# Default streaming ceiling. 908 samples/line already exceeds what NTSC's
# 4.2 MHz luma can resolve (~442 points across the active line), and 4
# samples per subcarrier cycle is the standard digital-NTSC rate, so going
# faster only burns USB bandwidth. --stream-rate to raise it.
STREAM_MAX_RATE = 15e6

# Seconds the AWG waits before starting, so the FIFO can be filled first.
START_WAIT_S = 1.0

# Override for the requested-vs-actual play rate factor (--play-scale).
# The ADP2230's FrequencyGet reports 4x the requested rate, but the measured
# consumption suggests the hardware may run 4x faster again. None of this is
# documented, so the factor stays overridable and the loopback measurement is
# the arbiter rather than any readback.
PLAY_SCALE_OVERRIDE = None


# Minimum samples per subcarrier cycle for colour. Picking purely on
# line-rate accuracy once chose divider 14 -- 454 samples/line and 1.996
# samples per cycle, exactly Nyquist -- which silently collapsed the chroma
# peak from +102.5 to +94.66 IRE. Above about 3 the reconstruction holds.
MIN_SAMPLES_PER_SC = 3.0


def choose_stream_timing(max_rate, colour=False):
    """Integer divider of the AWG clock with the most accurate line rate.

    Same divider constraint as funcCustom -- the rate must be AWG_CLOCK_HZ/N
    or the hardware stalls whole samples -- but without the buffer ceiling,
    so N can be small and samples-per-line large.

    For colour the rate must also carry the subcarrier: fsc is 227.5 x the
    line rate, so a divider that leaves fewer than MIN_SAMPLES_PER_SC samples
    per cycle is rejected however good its line rate looks.
    """
    best = None
    max_rate = min(max_rate, STREAM_MAX_RATE)
    for n in range(2, 40):
        rate = AWG_CLOCK_HZ / n
        if rate > max_rate:
            continue
        spl = round(rate / LINE_FREQ_HZ)
        spl += spl % 2                      # even: interlace needs half-lines
        if spl < 120:              # still far better than funcCustom's 54
            continue
        line_hz = rate / spl
        if colour and rate / (SC_RATIO * line_hz) < MIN_SAMPLES_PER_SC:
            continue
        err = abs(line_hz / LINE_FREQ_HZ - 1)
        key = (round(err, 9), -spl)
        if best is None or key < best[0]:
            best = (key, n, spl, rate, line_hz)
    if best is None:
        raise RuntimeError(
            f"No usable streaming divider at or below "
            f"{max_rate / 1e6:.3f} MS/s"
            + (f" with at least {MIN_SAMPLES_PER_SC} samples per subcarrier "
               f"cycle. Raise --stream-rate." if colour else
               ". Raise --stream-rate."))
    _, n, spl, rate, line_hz = best
    return n, spl, rate, line_hz


def play_rate_scale(dwf, hdwf, ch, probe=2e6):
    """Measure the factor between requested and accepted play rate.

    The ADP2230 accepts exactly 4x whatever play rate is requested (measured
    across 1..50 MS/s), so asking for the rate you want gives video four
    times too fast. The factor is not documented anywhere I can find, so it
    is measured rather than assumed -- a different unit or firmware may well
    behave differently, and silently running 4x off is hard to spot.
    """
    got = ctypes.c_double()
    if dwf.FDwfAnalogOutNodeFrequencySet(hdwf, ch, AnalogOutNodeCarrier,
                                         ctypes.c_double(probe)) != 1:
        return 1.0
    dwf.FDwfAnalogOutNodeFrequencyGet(hdwf, ch, AnalogOutNodeCarrier,
                                      ctypes.byref(got))
    if got.value <= 0:
        return 1.0
    return got.value / probe


def start_stream(dwf, hdwf, ch, spl, rate, scale=1.0, colour=False):
    """Configure funcPlay and prefill, returning the sequence to feed."""
    if colour:
        volts, fsc = build_colour_volts(spl, rate)
        print(f"      subcarrier {fsc / 1e6:.6f} MHz, "
              f"fsc/fH = {fsc / (rate / spl):.4f}")
    else:
        volts = build_frame_volts(spl)
    total = len(volts)
    if STD_DC:
        volts = [v - V_BLANK for v in volts]
    vlo, vhi = min(volts), max(volts)
    offset = (vlo + vhi) / 2.0
    amplitude = (vhi - vlo) / 2.0
    # Slice-assign rather than passing half a million positional arguments.
    data = (ctypes.c_double * total)()
    data[:] = [(v - offset) / amplitude for v in volts]
    scale_v = LEVEL * (2.0 if SOURCE_SERIES_75R else 1.0)
    amplitude *= scale_v
    offset *= scale_v

    dwf.FDwfDeviceAutoConfigureSet(hdwf, ctypes.c_int(0))
    check(dwf, dwf.FDwfAnalogOutNodeEnableSet(hdwf, ch, AnalogOutNodeCarrier,
                                              ctypes.c_int(1)), "NodeEnableSet")
    check(dwf, dwf.FDwfAnalogOutNodeFunctionSet(hdwf, ch, AnalogOutNodeCarrier,
                                                funcPlay), "NodeFunctionSet")
    # For funcPlay the frequency IS the sample rate, not a repetition rate --
    # but it has to be pre-divided by the measured scale factor to land on
    # the rate we actually want.
    check(dwf, dwf.FDwfAnalogOutNodeFrequencySet(hdwf, ch, AnalogOutNodeCarrier,
                                                 ctypes.c_double(rate / scale)),
          "NodeFrequencySet")
    check(dwf, dwf.FDwfAnalogOutNodeAmplitudeSet(hdwf, ch, AnalogOutNodeCarrier,
                                                 ctypes.c_double(amplitude)),
          "NodeAmplitudeSet")
    check(dwf, dwf.FDwfAnalogOutNodeOffsetSet(hdwf, ch, AnalogOutNodeCarrier,
                                              ctypes.c_double(offset)),
          "NodeOffsetSet")
    check(dwf, dwf.FDwfAnalogOutRunSet(hdwf, ch, ctypes.c_double(0.0)),
          "RunSet")                          # 0 = run until stopped
    check(dwf, dwf.FDwfAnalogOutRepeatSet(hdwf, ch, ctypes.c_int(0)),
          "RepeatSet")
    # DataSet can only prefill 32768 samples -- 2.3 ms -- so playback would
    # starve before the first PlayData arrives. A wait before the run lets
    # the feed loop fill the FIFO properly first.
    check(dwf, dwf.FDwfAnalogOutWaitSet(hdwf, ch, ctypes.c_double(START_WAIT_S)),
          "WaitSet")

    dmin, prefill = ctypes.c_int(), ctypes.c_int()
    dwf.FDwfAnalogOutNodeDataInfo(hdwf, ch, AnalogOutNodeCarrier,
                                  ctypes.byref(dmin), ctypes.byref(prefill))
    n = min(prefill.value, total)
    check(dwf, dwf.FDwfAnalogOutNodeDataSet(hdwf, ch, AnalogOutNodeCarrier,
                                            data, ctypes.c_int(n)),
          "NodeDataSet")
    check(dwf, dwf.FDwfAnalogOutConfigure(hdwf, ch, ctypes.c_int(1)),
          "AnalogOutConfigure")
    report_output_window(dwf, hdwf, ch, amplitude, offset)
    # Read back rather than assume: a play rate that silently did not take is
    # indistinguishable from a feed-loop bug by looking at the feed rate.
    gf = ctypes.c_double()
    gfn = ctypes.c_ubyte()
    grun = ctypes.c_double()
    grep = ctypes.c_int()
    dwf.FDwfAnalogOutNodeFunctionGet(hdwf, ch, AnalogOutNodeCarrier,
                                     ctypes.byref(gfn))
    dwf.FDwfAnalogOutNodeFrequencyGet(hdwf, ch, AnalogOutNodeCarrier,
                                      ctypes.byref(gf))
    dwf.FDwfAnalogOutRunGet(hdwf, ch, ctypes.byref(grun))
    dwf.FDwfAnalogOutRepeatGet(hdwf, ch, ctypes.byref(grep))
    print(f"      readback: function {gfn.value} (want 31), "
          f"rate {gf.value / 1e6:.6f} MS/s (want {rate / 1e6:.6f}), "
          f"run {grun.value}, repeat {grep.value}")
    if gf.value > 0 and abs(gf.value / rate - 1) > 0.001:
        print(f"      NOTE: readback {gf.value / 1e6:.6f} MS/s is "
              f"{gf.value / rate:.3f}x the intended {rate / 1e6:.6f} MS/s. "
              f"The loopback measurement decides which is real.")
    pmin, pmax = ctypes.c_int(), ctypes.c_int()
    dwf.FDwfAnalogOutNodePlayInfo(hdwf, ch, AnalogOutNodeCarrier,
                                  ctypes.byref(pmin), ctypes.byref(pmax))
    drain_ms = pmax.value / rate * 1000 if rate else 0
    print(f"      play FIFO {pmin.value}..{pmax.value} samples "
          f"= {drain_ms:.2f} ms of slack at this rate")
    print(f"      sequence {total} samples ({total * 8 / 1e6:.1f} MB), "
          f"prefilled {n}")
    print(f"      amplitude {amplitude:.4f} V, offset {offset:.4f} V on W1")
    return data, total, n % total, (gf.value or rate)


def stream_measure(dwf, hdwf, spl, rate, scope_rate=50e6, lines=24):
    """Measure the real output rate from a short loopback capture.

    Deliberately makes no assumption about the pulse widths or line period,
    because the whole point is that the rate may not be what we asked for --
    filtering by expected widths would hide exactly the error we are hunting.
    """
    n = int(scope_rate * lines * LINE_US / 1e6)
    samples = capture(dwf, hdwf, scope_rate, n)
    dt = 1e6 / scope_rate
    ordered = sorted(samples)
    lo = ordered[len(ordered) // 100]
    hi = ordered[-(len(ordered) // 100) - 1]
    print(f"  [measure] levels {lo:.3f}..{hi:.3f} V over "
          f"{n * dt:.0f} us at {scope_rate / 1e6:.0f} MS/s")
    if hi - lo < 0.2:
        print("  [measure] no signal on CH1 -- is W1 jumpered to it?")
        return False
    runs = runs_below(samples, dt, lo + 0.13 * (hi - lo))
    if len(runs) < 4:
        print(f"  [measure] only {len(runs)} pulses found")
        return False
    widths = sorted(w for _, w in runs)
    starts = [t for t, _ in runs]
    gaps = sorted(b - a for a, b in zip(starts, starts[1:]))
    med = gaps[len(gaps) // 2]
    print(f"  [measure] {len(runs)} pulses, widths "
          f"{widths[0]:.3f}..{widths[-1]:.3f} us "
          f"(median {widths[len(widths) // 2]:.3f})")
    print(f"  [measure] median pulse interval {med:.4f} us")
    if med <= 0:
        return False
    implied_line = med
    implied_rate = spl / implied_line * 1e6
    print(f"  [measure] if that is a line: {1e6 / implied_line:.1f} Hz line "
          f"rate, implied sample rate {implied_rate / 1e6:.3f} MS/s")
    print(f"  [measure] intended {rate / 1e6:.3f} MS/s "
          f"-> actual/intended = {implied_rate / rate:.3f}x")
    if abs(implied_rate / rate - 1) < 0.02:
        print("  [measure] OK: the hardware is running at the intended rate.")
    else:
        print("  [measure] MISMATCH: the hardware is NOT at the intended rate.")
    return True


def stream_loop(dwf, hdwf, ch, data, total, pos, rate,
                measure_spl=None, chunk_cap=1 << 20,
                probe_s=6.0):
    """Feed the sequence cyclically, reporting any lost or corrupted samples.

    Feeds whatever PlayStatus reports free, which is what the shipped example
    does. Do not try to bound the in-flight depth from PlayInfo's maximum --
    that is the largest settable buffer, not the live FIFO size, so
    `capacity - free` is not the depth and clamps the feed to nothing.
    """
    sts = ctypes.c_ubyte()
    free, lost, corrupt = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
    t_lost = t_corrupt = fed = 0
    worst_lost = 0
    iters = 0
    # Two copies back to back, so a read starting anywhere in the first copy
    # can always run `total` samples without splitting at the wrap.
    twice = (ctypes.c_double * (2 * total))()
    twice[:total] = data[:]
    twice[total:] = data[:]
    t0 = last = time.time()
    print("\nStreaming. Press Ctrl+C to stop.")

    started = False
    next_measure = 4.0
    while True:
        # Must be called every iteration: this is what refreshes the status
        # that PlayStatus then reports. Skipping it leaves free pinned at 0.
        check(dwf, dwf.FDwfAnalogOutStatus(hdwf, ch, ctypes.byref(sts)),
              "AnalogOutStatus")
        if sts.value == DwfStateRunning:
            started = True
        elif started:
            raise RuntimeError(f"AWG stopped (state {sts.value}); "
                               f"{t_lost} lost, {t_corrupt} corrupted.")
        elif sts.value not in (DwfStateWait, DwfStateArmed):
            raise RuntimeError(f"AWG in unexpected state {sts.value} "
                               f"before starting.")
        check(dwf, dwf.FDwfAnalogOutNodePlayStatus(hdwf, ch, AnalogOutNodeCarrier,
                                                   ctypes.byref(free),
                                                   ctypes.byref(lost),
                                                   ctypes.byref(corrupt)),
              "NodePlayStatus")
        t_lost += lost.value
        t_corrupt += corrupt.value

        # Feed exactly what the FIFO reports free. Now that the rate is
        # right this is self-limiting -- it fills, then settles at the
        # consumption rate. Pacing it against the wall clock instead let the
        # depth creep up until the FIFO overflowed and corrupted samples.
        want = min(free.value, chunk_cap, total)
        if want > 0:
            check(dwf, dwf.FDwfAnalogOutNodePlayData(
                hdwf, ch, AnalogOutNodeCarrier,
                ctypes.byref(twice, pos * 8), ctypes.c_int(want)),
                "NodePlayData")
            pos = (pos + want) % total
            fed += want
        iters += 1

        worst_lost = max(worst_lost, lost.value)
        now = time.time()
        if measure_spl and started and now - t0 > next_measure:
            print("\nMeasuring the real output rate...")
            if stream_measure(dwf, hdwf, measure_spl, rate):
                measure_spl = None
            else:
                next_measure = now - t0 + 15.0
                print("  [measure] will retry in 15 s "
                      "(connect W1 to CH1 if it is not already)")
            last = time.time()
        if now - last >= 5.0:
            last = now
            # Report the latest reading alongside the running total: the
            # counters' exact semantics are unclear, and a cumulative value
            # summed every poll inflates quadratically.
            print(f"  {now - t0:6.0f}s  fed {fed / 1e6:8.2f} Ms "
                  f"({fed / (now - t0) / 1e6:5.2f} MS/s)  free {free.value}  "
                  f"lost(now) {lost.value}  "
                  f"{iters / (now - t0):4.0f} it/s "
                  f"({1000 * (now - t0) / max(iters, 1):4.1f} ms/it)  "
                  f"corrupt {t_corrupt}"
                  + ("" if not (worst_lost or t_corrupt)
                     else f"  <-- GLITCH (corrupt total {t_corrupt})"))
        # Decide early whether this rate is sustainable at all. The working
        # configurations have a shallow play FIFO, so the ceiling is
        # fifo / iteration-time, not USB bandwidth.
        if probe_s and started and now - t0 > probe_s:
            elapsed = now - t0
            # The measured feed rate IS the sustainable rate. PlayInfo reports
            # max=0 in the configurations that actually stream, so deriving it
            # from FIFO depth over iteration time was guesswork -- it claimed
            # 10 MS/s while the real throughput was 5.8.
            sustainable = fed / elapsed
            if t_lost > rate * 0.01:
                print(f"\n  starving: fed {sustainable / 1e6:.2f} MS/s of "
                      f"{rate / 1e6:.2f} needed, {t_lost} lost "
                      f"({iters / elapsed:.0f} it/s)")
                return "starved", sustainable
            print(f"  sustained cleanly for {probe_s:.0f}s "
                  f"({sustainable / 1e6:.1f} MS/s headroom)")
            probe_s = None
        if fed == 0 and now - t0 > START_WAIT_S + 2.0:
            raise RuntimeError(
                f"Fed nothing in {now - t0:.1f}s (PlayStatus free="
                f"{free.value}). The device is not accepting play data.")
        if want == 0:
            time.sleep(0.0005)


# ----------------------------------------------------------------------------
# NTSC colour encoding (--color)
#
# composite = Y + U*sin(theta) + V*cos(theta),  theta = 2*pi*fsc*t
#
# Worked in IRE/100 ("v") throughout, where v = 0 is blanking and v = 1 is
# white, so sync is -0.40 and the 7.5 IRE pedestal is +0.075. Converting at
# the end: volts = V_BLANK + v * (V_WHITE - V_BLANK).
#
# fsc is generated as exactly SC_RATIO * the ACTUAL line rate, not the nominal
# 3.579545 MHz. That keeps fsc/fH at exactly 227.5 so the four-field sequence
# closes and burst stays coherent; the absolute frequency lands 0.007% low,
# which any colour PLL pulls without noticing.
#
# Because fsc/fH is a half-integer, subcarrier phase inverts line to line and
# only repeats after four fields -- 227.5 * 1050 = 238,875 whole cycles. The
# phase is taken from the absolute sample index, so all of that is automatic.
# ----------------------------------------------------------------------------
# Band-limiting, as a real encoder does. NTSC allows 4.2 MHz of luma and
# about 1.3 MHz on each chroma axis. Stepping U and V abruptly at a bar
# boundary throws sidebands well outside the chroma band and into luma, and
# 70 ns luma steps carry energy straight through 3.58 MHz -- both show up as
# busy, fringed bar edges. --no-filter to compare.
LUMA_MHZ = 4.2
CHROMA_MHZ = 1.3

# NTSC specifies a sync rise/fall time of 140 ns +/-20. Emitting single-sample
# steps instead makes the edges effectively instantaneous and broadband, and
# the AWG's reconstruction filter rings on them -- visible as overshoot going
# into and out of sync. A hard-gated burst rings the same way, so its envelope
# gets a short taper too.
FILTER_CHROMA = True


def lowpass_taps(fc_norm, n):
    """Windowed-sinc low-pass, n odd taps, cutoff in cycles per sample."""
    half, taps = n // 2, []
    for i in range(n):
        k = i - half
        if k == 0:
            h = 2.0 * fc_norm
        else:
            x = 2.0 * math.pi * fc_norm * k
            h = 2.0 * fc_norm * math.sin(x) / x
        taps.append(h * (0.54 - 0.46 * math.cos(2.0 * math.pi * i / (n - 1))))
    total = sum(taps)
    return [t / total for t in taps]          # unity DC gain


def filter_clamped(xs, taps):
    """Convolve, holding the end values rather than fading toward zero."""
    half, n, last = len(taps) // 2, len(xs), len(xs) - 1
    out = []
    for i in range(n):
        acc = 0.0
        for j, c in enumerate(taps):
            k = i + j - half
            acc += c * xs[0 if k < 0 else (last if k > last else k)]
        out.append(acc)
    return out


SC_RATIO = 227.5                   # fsc / line rate, exactly
# Generate the subcarrier at the nominal 3.579545 MHz instead of locking it to
# 227.5 x our actual line rate. Our line rate is 0.007% low, so the two cannot
# both be exact: locking to 227.5 puts fsc 250 Hz below nominal (25x the
# +/-10 Hz spec), while nominal fsc makes fsc/fH 227.5161 so SCH phase walks.
# --fsc-nominal picks the second, purely to find out which one an instrument
# objects to.
FSC_NOMINAL = False
FSC_HZ = 3579545.0
V_SYNC_IRE = -0.40                 # -40 IRE
V_BLACK_IRE = SETUP_IRE / 100.0    # pedestal as a fraction of white
BURST_AMP = 0.20                   # 40 IRE peak-to-peak
BURST_START_US = 5.3               # after start of sync: 0.6 us into back porch
# Measured against a broadcast generator through the same chain: its burst
# envelope starts at 4.80 us and reaches 40.9 IRE, where ours started at 5.34
# and reached 36.0. --burst-start and --burst-amp make those adjustable.
BURST_CYCLES = 9
CHROMA_GAIN = 1.0 - V_BLACK_IRE    # same compression the setup applies to luma
SATURATION = 1.0                   # --sat to trim


def set_setup_ire(ire):
    """Set the setup pedestal, in IRE, and everything derived from it.

    Luma maps 0..1 onto pedestal..white, so the pedestal costs active range;
    chroma takes the same gain to keep saturation matched to luma. At 0 IRE
    black and blanking coincide and --vbi-black becomes a no-op.
    """
    global SETUP_IRE, V_BLACK, V_BLACK_IRE, CHROMA_GAIN
    if not 0.0 <= ire <= 20.0:
        raise RuntimeError(f"--setup {ire} IRE is outside 0..20.")
    SETUP_IRE = ire
    V_BLACK = (40 + ire) * IRE
    V_BLACK_IRE = ire / 100.0
    CHROMA_GAIN = 1.0 - V_BLACK_IRE

# 75% EIA colour bars, in R'G'B' 0..1, standard left-to-right order.
# Bar amplitude. 75% is the EIA/SMPTE norm and peaks at 76.88 IRE, NOT 100 --
# setup + 0.925 x 0.75. --bar-amp 100 gives full-amplitude bars (100 IRE white,
# and yellow/cyan chroma peaks reaching ~131 IRE, which some inputs clip).
BAR_AMP = 0.75
BAR_PRIMARIES = (
    (1, 1, 1),                     # white
    (1, 1, 0),                     # yellow
    (0, 1, 1),                     # cyan
    (0, 1, 0),                     # green
    (1, 0, 1),                     # magenta
    (1, 0, 0),                     # red
    (0, 0, 1),                     # blue
    (0, 0, 0),                     # black
)
COLOUR_BARS = tuple(tuple(BAR_AMP * c for c in bar) for bar in BAR_PRIMARIES)


def yuv(r, g, b):
    """Luma and the two chroma components, standard NTSC coefficients."""
    y = 0.299 * r + 0.587 * g + 0.114 * b
    return y, 0.492111 * (b - y), 0.877283 * (r - y)


BAR_YUV = tuple(yuv(*c) for c in COLOUR_BARS)


def set_bar_amp(pct):
    """Set colour-bar amplitude, in percent, and rebuild the bar table."""
    global BAR_AMP, COLOUR_BARS, BAR_YUV
    if not 0.0 < pct <= 100.0:
        raise RuntimeError(f"--bar-amp {pct} is outside 0..100 percent.")
    BAR_AMP = pct / 100.0
    COLOUR_BARS = tuple(tuple(BAR_AMP * c for c in bar) for bar in BAR_PRIMARIES)
    BAR_YUV = tuple(yuv(*c) for c in COLOUR_BARS)


def colour_fields():
    """Fields in one looping colour sequence, and half-lines per field.

    fsc/fH = 227.5 means a half subcarrier cycle per line, so a 525-line
    interlaced frame is 119,437.5 cycles -- it takes two frames (four fields)
    to close with a whole number, 238,875. Progressive 262 lines is
    227.5 x 262 = 59,605 cycles exactly, so a single frame closes.
    """
    if INTERLACED:
        return 4, FIELD_HALF_LINES
    return 1, 2 * TOTAL_LINES


def build_colour_volts(spl, rate):
    """Four-field NTSC colour-bar sequence, in volts at the coax."""
    if spl % 2:
        raise RuntimeError("interlace needs an even samples-per-line")
    half = spl // 2
    fields, fhl = colour_fields()
    total = fields * fhl * half
    us = 1e6 / rate
    line_us = spl * us
    if FSC_NOMINAL:
        # Nearest subcarrier to nominal that still completes a whole number
        # of cycles over the sequence, so the loop point has no phase jump.
        # Letting fsc be exactly nominal leaves a fractional cycle count and
        # a burst discontinuity 15 times a second, which is worse than being
        # 250 Hz off.
        step = rate / total
        fsc = round(FSC_HZ / step) * step
    else:
        fsc = SC_RATIO * (1e6 / line_us)
    w = 2.0 * math.pi * fsc / rate          # radians per sample

    sync_n = round(H_SYNC_US / us)
    bp_n = round(BACK_PORCH_US / us)
    fp_n = round(FRONT_PORCH_US / us)
    active_n = spl - sync_n - bp_n - fp_n
    b0 = round(BURST_START_US / us)
    bn = round(BURST_CYCLES / fsc * 1e6 / us)
    # Taper the burst envelope so it is not hard-gated.

    eq_n = round(EQ_PULSE_US / us)
    br_n = round(V_SYNC_PULSE_US / us)

    def pulse(width_n, span_n):
        """A sync pulse of width_n samples within a span_n sample slot."""
        return ([V_SYNC_IRE] * min(width_n, span_n)
                + [0.0] * max(0, span_n - width_n))

    eq_half = pulse(eq_n, half)
    br_half = pulse(br_n, half)

    frame = [0.0] * total

    def put(at, seg):
        for j, v in enumerate(seg):
            frame[(at + j) % total] = v

    # Which line positions are blanked: the 13 lines after each field's train.
    blank_hl = set()
    for f in range(fields):
        base = f * fhl
        first = base + (18 if base % 2 == 0 else 19)
        blank_hl |= set(range(first, first + 2 * EXTRA_BLANK_LINES, 2))

    # Every active line carries the same bars, so build and band-limit the
    # luma and chroma envelopes once and reuse them.
    ya, ua, va = [], [], []
    # --pattern white is a flat 100 IRE field, no chroma: the signal to set
    # output level by, since 75% bars peak at 76.88 IRE and never reach 100.
    bars = ((1.0, 0.0, 0.0),) if PATTERN == "white" else BAR_YUV
    nb = len(bars)
    for k in range(active_n):
        y, u, v = bars[min(k * nb // active_n, nb - 1)]
        ya.append(V_BLACK_IRE + (1.0 - V_BLACK_IRE) * y)
        ua.append(u * CHROMA_GAIN * SATURATION)
        va.append(v * CHROMA_GAIN * SATURATION)
    if FILTER_CHROMA:
        ya = filter_clamped(ya, lowpass_taps(LUMA_MHZ * 1e6 / rate, 21))
        ct = lowpass_taps(CHROMA_MHZ * 1e6 / rate, 33)
        ua = filter_clamped(ua, ct)
        va = filter_clamped(va, ct)

    sin, cos = math.sin, math.cos
    # Normal lines on the one continuous h-sync grid (every even half-line).
    for hl in range(0, fields * fhl, 2):
        s0 = hl * half
        blanked = hl in blank_hl
        line = pulse(sync_n, spl)
        for k in range(sync_n, spl):
            if k < sync_n + bp_n:
                if b0 <= k < b0 + bn:
                    line[k] = -BURST_AMP * sin(w * (s0 + k))
                continue
            if k >= sync_n + bp_n + active_n:
                continue                      # front porch, blanking
            if blanked:
                continue                      # vertical-interval line
            a = k - sync_n - bp_n
            th = w * (s0 + k)
            line[k] = ya[a] + ua[a] * sin(th) + va[a] * cos(th)
        put(s0, line)

    # Pulse trains last, so the half-lines they truncate come out right.
    for f in range(fields):
        base = f * fhl
        for k in range(18):
            put((base + k) * half, eq_half if (k < 6 or k >= 12) else br_half)
        if base % 2:                          # train ended mid-line
            put((base + 18) * half, [0.0] * half)

    span = V_WHITE - V_BLANK
    return [V_BLANK + v * span for v in frame], fsc


# ----------------------------------------------------------------------------
# Deep-memory playback (--deep)
#
# From the SDK manual, on FDwfAnalogOutNodePlayInfo:
#   "For devices with deep memory (ADP3X50, ADP2230) up to this number of
#    samples can be set at once. For devices without deep memory this will
#    report zero but play mode can be still used by sending data in chunks."
# and on the lost/corrupted counters:
#   "...reduce the frequency or runtime to be less or equal to the device
#    buffer size (runtime <= buffer size / frequency)."
#
# So the 67 M that PlayInfo reports is how much can be loaded in ONE DataSet
# call, not a streaming FIFO. Load the whole frame, set the run length to the
# sequence duration, repeat infinitely, and the device plays it out of deep
# memory with no host involvement at all -- no feed loop, no underruns.
# A 525-line frame at 908 samples/line is 476,700 samples: 0.7% of it.
# ----------------------------------------------------------------------------
# The device inserts a small gap (~24 samples, 1.7 us) at every play-run
# repeat. Measured: anomalies every 66,739.7 us against a 66,738.0 us
# sequence. Wherever that gap falls, it truncates whatever was in progress --
# landing mid-sync it cut a pulse from 4.68 to 3.96 us, one line per sequence.
# Rotating the sequence so sample 0 sits at blanking level inside the vertical
# interval puts the gap in blanked VBI, where the idle level (IdleInitial =
# first sample = blanking) is indistinguishable from its surroundings.
LOOP_ALIGN = True


def loop_align_offset(spl):
    """Sample offset placing the loop boundary inside vertical blanking."""
    half = spl // 2
    us = LINE_US / spl
    hl = 18 + 2 * (EXTRA_BLANK_LINES // 2)      # mid blanked-VBI lines
    into = round((H_SYNC_US + BACK_PORCH_US + 10.0) / us)
    return hl * half + into


def pick_deep_config(dwf, force=None):
    """Configuration with the most analog-out play memory."""
    ndev, ncfg = ctypes.c_int(), ctypes.c_int()
    check(dwf, dwf.FDwfEnum(ctypes.c_int(0), ctypes.byref(ndev)), "FDwfEnum")
    check(dwf, dwf.FDwfEnumConfig(ctypes.c_int(0), ctypes.byref(ncfg)),
          "FDwfEnumConfig")
    best, best_play = -1, -1
    for i in range(ncfg.value):
        play, scope = ctypes.c_int(), ctypes.c_int()
        dwf.FDwfEnumConfigInfo(ctypes.c_int(i), DECIAnalogOutPlaySize,
                               ctypes.byref(play))
        dwf.FDwfEnumConfigInfo(ctypes.c_int(i), DECIAnalogInBufferSize,
                               ctypes.byref(scope))
        print(f"  config {i}: play memory {play.value} samples, "
              f"scope buffer {scope.value}")
        if play.value > best_play:
            best, best_play = i, play.value
    if force is not None:
        print(f"  -> config {force} (forced)")
        return force
    if best_play <= 0:
        raise RuntimeError(
            "No configuration reports analog-out play memory; this device "
            "cannot preload a full frame.")
    print(f"  -> config {best} ({best_play} samples of play memory)")
    return best


def calibrate_play_rate(dwf, hdwf, ch, func, requested=2e6):
    """Measure the real play rate with the scope. Needs W1 -> CH1.

    FDwfAnalogOutNodeFrequencyGet cannot be trusted for play mode: in config 0
    it reported 4x the requested rate, in config 3 it echoed the request back
    while the hardware ran at 2x, and in config 5 it was honest at 1x. The
    scope is the only ground truth, so measure a square wave of known period
    in samples and divide.
    """
    PERIOD, N = 4000, 400000           # 100 cycles, 2000 samples per half
    data = (ctypes.c_double * N)()
    half = PERIOD // 2
    data[:] = [1.0 if (i % PERIOD) < half else -1.0 for i in range(N)]

    dwf.FDwfDeviceAutoConfigureSet(hdwf, ctypes.c_int(0))
    dwf.FDwfAnalogOutNodeEnableSet(hdwf, ch, AnalogOutNodeCarrier,
                                   ctypes.c_int(1))
    dwf.FDwfAnalogOutNodeFunctionSet(hdwf, ch, AnalogOutNodeCarrier, func)
    dwf.FDwfAnalogOutNodeFrequencySet(hdwf, ch, AnalogOutNodeCarrier,
                                      ctypes.c_double(requested))
    dwf.FDwfAnalogOutNodeAmplitudeSet(hdwf, ch, AnalogOutNodeCarrier,
                                      ctypes.c_double(1.0))
    dwf.FDwfAnalogOutNodeOffsetSet(hdwf, ch, AnalogOutNodeCarrier,
                                   ctypes.c_double(0.0))
    dwf.FDwfAnalogOutNodeDataSet(hdwf, ch, AnalogOutNodeCarrier, data,
                                 ctypes.c_int(N))
    dwf.FDwfAnalogOutRunSet(hdwf, ch, ctypes.c_double(0.0))
    dwf.FDwfAnalogOutRepeatSet(hdwf, ch, ctypes.c_int(0))
    check(dwf, dwf.FDwfAnalogOutConfigure(hdwf, ch, ctypes.c_int(1)),
          "calibration Configure")

    scope_rate, n = 25e6, 1 << 19       # ~21 ms
    samples = capture(dwf, hdwf, scope_rate, n)
    dt = 1e6 / scope_rate
    lo, hi = min(samples), max(samples)
    if hi - lo < 0.5:
        raise RuntimeError(f"Calibration saw no square wave on CH1 "
                           f"({lo:+.3f}..{hi:+.3f} V). Jumper W1 to CH1, "
                           f"or pass --play-scale 1.0 to skip calibration "
                           f"(measured correct for this device).")
    mid = (lo + hi) / 2.0
    edges = [(i - 1 + (mid - samples[i - 1]) / (samples[i] - samples[i - 1])) * dt
             for i in range(1, len(samples))
             if samples[i - 1] < mid <= samples[i]]
    if len(edges) < 3:
        raise RuntimeError(f"Calibration found only {len(edges)} edges.")
    gaps = sorted(b - a for a, b in zip(edges, edges[1:]))
    period_us = gaps[len(gaps) // 2]
    actual = PERIOD / period_us * 1e6
    dwf.FDwfAnalogOutReset(hdwf, ch)
    print(f"      calibration: requested {requested / 1e6:.6f} MS/s, "
          f"measured period {period_us:.3f} us over {PERIOD} samples "
          f"-> {actual / 1e6:.6f} MS/s")
    print(f"      true scale {actual / requested:.4f}x "
          f"({len(edges)} edges, jitter "
          f"{(gaps[-1] - gaps[0]) * 1000:.0f} ns)")
    return actual / requested


def start_deep(dwf, hdwf, ch, spl, rate, scale, func, colour=False):
    """Preload the whole sequence into deep memory and loop it in hardware."""
    if colour:
        volts, fsc = build_colour_volts(spl, rate)
        print(f"      subcarrier {fsc / 1e6:.6f} MHz "
              f"({fsc - 3579545.0:+.0f} Hz vs nominal), "
              f"fsc/fH = {fsc / (rate / spl):.4f} "
              f"{'(nominal fsc, SCH walks)' if FSC_NOMINAL else '(locked 227.5)'}"
              f", {rate / fsc:.4f} samples/cycle")
    else:
        volts = build_frame_volts(spl)
    if COPIES > 1:
        volts = volts * COPIES
        print(f"      {COPIES} copies back to back: "
              f"{len(volts)} samples, any per-run overhead paid "
              f"{COPIES}x less often")
    total = len(volts)
    if LOOP_ALIGN:
        k = loop_align_offset(spl) % total
        volts = volts[k:] + volts[:k]
        lvl = (volts[0] - V_BLANK) / (V_WHITE - V_BLANK) * 100
        print(f"      loop-aligned by {k} samples; boundary sits at "
              f"{lvl:+.1f} IRE (blanking) inside the vertical interval")
    if STD_DC:
        # Standard composite: blanking 0 V, sync tip -286 mV, white +714 mV.
        volts = [v - V_BLANK for v in volts]
    # Derive the output window from the data: colour peaks above 100 IRE and
    # a fixed 0..1 V window would clip them.
    vlo, vhi = min(volts), max(volts)
    offset = (vlo + vhi) / 2.0
    amplitude = (vhi - vlo) / 2.0
    data = (ctypes.c_double * total)()
    data[:] = [(v - offset) / amplitude for v in volts]
    scale_v = LEVEL * (2.0 if SOURCE_SERIES_75R else 1.0)
    amplitude *= scale_v
    offset *= scale_v
    base = 0.0 if STD_DC else V_BLANK
    print(f"      output window {vlo:.4f}..{vhi:.4f} V "
          f"({(vlo - base) / (V_WHITE - V_BLANK) * 100:+.1f}..."
          f"{(vhi - base) / (V_WHITE - V_BLANK) * 100:+.1f} IRE), "
          f"blanking at {base:.3f} V "
          f"{'(standard)' if STD_DC else '(sync tip at 0 V)'}")

    dwf.FDwfDeviceAutoConfigureSet(hdwf, ctypes.c_int(0))
    check(dwf, dwf.FDwfAnalogOutNodeEnableSet(hdwf, ch, AnalogOutNodeCarrier,
                                              ctypes.c_int(1)), "NodeEnableSet")
    check(dwf, dwf.FDwfAnalogOutNodeFunctionSet(hdwf, ch, AnalogOutNodeCarrier,
                                                func), "NodeFunctionSet")
    pmin, pmax = ctypes.c_int(), ctypes.c_int()
    check(dwf, dwf.FDwfAnalogOutNodePlayInfo(hdwf, ch, AnalogOutNodeCarrier,
                                             ctypes.byref(pmin),
                                             ctypes.byref(pmax)),
          "NodePlayInfo")
    print(f"      play memory {pmin.value}..{pmax.value} samples; "
          f"sequence needs {total} ({100.0 * total / max(pmax.value, 1):.2f}%)")
    if total > pmax.value:
        raise RuntimeError(f"Sequence of {total} samples exceeds the "
                           f"{pmax.value} the device can hold at once.")

    check(dwf, dwf.FDwfAnalogOutNodeFrequencySet(hdwf, ch, AnalogOutNodeCarrier,
                                                 ctypes.c_double(rate / scale)),
          "NodeFrequencySet")
    check(dwf, dwf.FDwfAnalogOutNodeAmplitudeSet(hdwf, ch, AnalogOutNodeCarrier,
                                                 ctypes.c_double(amplitude)),
          "NodeAmplitudeSet")
    check(dwf, dwf.FDwfAnalogOutNodeOffsetSet(hdwf, ch, AnalogOutNodeCarrier,
                                              ctypes.c_double(offset)),
          "NodeOffsetSet")
    # The whole sequence in one call -- this is what deep memory buys.
    check(dwf, dwf.FDwfAnalogOutNodeDataSet(hdwf, ch, AnalogOutNodeCarrier,
                                            data, ctypes.c_int(total)),
          "NodeDataSet")
    # runtime == sequence duration, repeated forever: entirely in hardware.
    # Run length from the rate we actually achieve. Using the intended rate
    # when the hardware runs at half speed truncates the sequence mid-frame
    # and restarts -- which looks like crosshatching and scrolling artifacts.
    run_s = 0.0 if RUN_INFINITE else total / rate
    check(dwf, dwf.FDwfAnalogOutRunSet(hdwf, ch, ctypes.c_double(run_s)),
          "RunSet")
    print(f"      run {'infinite (no repeat boundary)' if RUN_INFINITE else
                      f'{run_s * 1000:.3f} ms, repeated'}")
    check(dwf, dwf.FDwfAnalogOutWaitSet(hdwf, ch, ctypes.c_double(0.0)),
          "WaitSet")
    check(dwf, dwf.FDwfAnalogOutRepeatSet(hdwf, ch, ctypes.c_int(0)),
          "RepeatSet")
    dwf.FDwfAnalogOutIdleSet(hdwf, ch, DwfAnalogOutIdleInitial)
    check(dwf, dwf.FDwfAnalogOutConfigure(hdwf, ch, ctypes.c_int(1)),
          "AnalogOutConfigure")
    report_output_window(dwf, hdwf, ch, amplitude, offset)

    gf, grun = ctypes.c_double(), ctypes.c_double()
    dwf.FDwfAnalogOutNodeFrequencyGet(hdwf, ch, AnalogOutNodeCarrier,
                                      ctypes.byref(gf))
    dwf.FDwfAnalogOutRunGet(hdwf, ch, ctypes.byref(grun))
    print(f"      readback rate {gf.value / 1e6:.6f} MS/s "
          f"(want {rate / 1e6:.6f}), run {grun.value * 1000:.3f} ms "
          f"(sequence {total / rate * 1000:.3f} ms)")
    print(f"      amplitude {amplitude:.4f} V, offset {offset:.4f} V on W1")
    return total


def clock_info(dwf, hdwf):
    """Can the reference/system clock be changed? Exact NTSC needs it.

    We need sample rate = 4*fsc = 14.318182 MHz. From 100 MHz that is
    100/6.985 -- no integer divider. With the clock at 7 * 4fsc =
    100.227273 MHz, divider 7 would land exactly, giving fsc nominal AND
    fsc/fH = 227.5 at 910 samples/line (exactly 4 samples per cycle).
    """
    names = (("DwfParamFrequency", 8), ("DwfParamExtFreq", 9),
             ("DwfParamClockMode", 10), ("DwfParamDigFrequency", 19),
             ("DwfParamDACFrequency", 25))
    print("\nClock parameters (device, then global):")
    for nm, pid in names:
        dv, gv = ctypes.c_int(-1), ctypes.c_int(-1)
        rd = dwf.FDwfDeviceParamGet(hdwf, ctypes.c_int(pid), ctypes.byref(dv))
        rg = dwf.FDwfParamGet(ctypes.c_int(pid), ctypes.byref(gv))
        print(f"  {nm:22} device {'ok' if rd else '--'} {dv.value:>12}   "
              f"global {'ok' if rg else '--'} {gv.value:>12}")

    want = int(round(7 * 4 * 3579545.0))        # 100,227,273 Hz
    print(f"\n  trying to set the system clock to {want} Hz "
          f"(7 x 4fsc):")
    ok = dwf.FDwfDeviceParamSet(hdwf, ctypes.c_int(8), ctypes.c_int(want))
    back = ctypes.c_int(-1)
    dwf.FDwfDeviceParamGet(hdwf, ctypes.c_int(8), ctypes.byref(back))
    print(f"    set returned {ok}, reads back {back.value}"
          + ("" if ok else f"   ({last_error(dwf)})"))
    if ok and back.value == want:
        print("    ACCEPTED -- exact NTSC timing may be reachable; this needs "
              "a scope measurement to confirm the hardware followed.")
    else:
        print("    not settable this way. The remaining route to an exact "
              "subcarrier is an external reference via DwfParamClockMode=2 "
              "plus DwfParamExtFreq.")


def measure_fsc_precise(samples, dt, starts, blank_v, line_us):
    """Burst frequency from phase advance over a known number of lines.

    A DFT over one 2.45 us burst has a main lobe about 400 kHz wide, so it
    cannot pin the subcarrier at all -- earlier readings of "3.570 MHz" and
    "5.42 MHz" were that lobe, not the signal. Phase advance over a long
    baseline is precise, but needs the integer cycle count. Anchor it on the
    nominal subcarrier over two lines (where NTSC predicts a whole 455
    cycles), then extend the baseline. Assumes fsc is within a few kHz of
    nominal, which is stated rather than hidden.
    """
    def phase_at(t_us):
        a, b = int((t_us + 5.40) / dt), int((t_us + 7.75) / dt)
        if a < 0 or b > len(samples):
            return None
        re = im = 0.0
        for i in range(a, b):
            ang = 2.0 * math.pi * FSC_HZ * (i - a) * dt * 1e-6
            y = samples[i] - blank_v
            re += y * math.cos(ang)
            im += y * math.sin(ang)
        return math.atan2(im, re) if math.hypot(re, im) > 1e-9 else None

    f = FSC_HZ
    for gap in (2, 20, 200, min(1200, len(starts) - 2)):
        if gap < 2 or gap + 1 >= len(starts):
            continue
        ests = []
        for i0 in range(1, min(len(starts) - gap - 1, 30)):
            p0, p1 = phase_at(starts[i0]), phase_at(starts[i0 + gap])
            if p0 is None or p1 is None:
                continue
            tsec = (starts[i0 + gap] - starts[i0]) * 1e-6
            cycles = round(f * tsec)            # integer, from current estimate
            drift = (p1 - p0 + math.pi) % (2.0 * math.pi) - math.pi
            ests.append((cycles - drift / (2.0 * math.pi)) / tsec)
        if ests:
            f = sorted(ests)[len(ests) // 2]
    return f


def render_window(samples, dt, t0_us, span_us, label, marks=()):
    """ASCII plot of a slice of the capture, so anomalies are seen not guessed."""
    a = max(0, int(t0_us / dt))
    b = min(len(samples), int((t0_us + span_us) / dt))
    if b - a < 10:
        return
    w = samples[a:b]
    vmin, vmax = min(w), max(w)
    rows, cols = 15, 108
    step = max(1, len(w) // cols)
    grid = [[" "] * cols for _ in range(rows)]
    for c in range(min(cols, len(w) // step)):
        seg = w[c * step:(c + 1) * step]
        for val in (min(seg), max(seg)):
            r = int((vmax - val) / (vmax - vmin + 1e-12) * (rows - 1))
            grid[min(max(r, 0), rows - 1)][c] = "*"
    print(f"\n  {label}  ({t0_us:.2f}..{t0_us + span_us:.2f} us, "
          f"{step * dt * 1000:.0f} ns/col, {vmin * 1000:+.0f}.."
          f"{vmax * 1000:+.0f} mV)")
    for r, row in enumerate(grid):
        v = vmax - r * (vmax - vmin) / (rows - 1)
        print(f"   {v * 1000:+7.0f} |{''.join(row)}")
    if marks:
        line = [" "] * cols
        for mt in marks:
            c = int((mt - t0_us) / dt / step)
            if 0 <= c < cols:
                line[c] = "^"
        print(f"           {''.join(line)}")


def scan_anomalies(dwf, hdwf, seconds=0.20, scope_rate=50e6, save=None):
    """Per-line metrics across several sequences, hunting periodic defects.

    The reported fault is intermittent and repetitive, so measure every line
    and look at where the outliers fall rather than averaging them away.
    """
    print("\nPer-line anomaly scan...")
    bmin, bmax = ctypes.c_int(), ctypes.c_int()
    dwf.FDwfAnalogInBufferSizeInfo(hdwf, ctypes.byref(bmin), ctypes.byref(bmax))
    n = min(int(scope_rate * seconds), bmax.value)
    samples = capture(dwf, hdwf, scope_rate, n)
    dt = 1e6 / scope_rate
    if save:
        with open(save, "w") as f:
            f.write("t_us,volts\n")
            for i, v in enumerate(samples):
                f.write(f"{i * dt:.6f},{v:.6f}\n")
        print(f"  wrote {save} ({len(samples)} samples)")
    srt = sorted(samples)
    lo, hi = srt[len(srt) // 200], srt[-(len(srt) // 200) - 1]
    runs = runs_below(samples, dt, lo + 0.13 * (hi - lo))
    hs = [(t, w) for t, w in runs if 3.5 <= w <= 15]
    print(f"  {n * dt / 1000:.1f} ms, {len(hs)} h-sync pulses")
    if len(hs) < 100:
        print("  too few lines to scan.")
        return

    blank = []
    for t, _ in hs[2:60]:
        a, b = int((t + 62.0) / dt), int((t + 63.0) / dt)
        if b < len(samples):
            blank.append(sum(samples[a:b]) / (b - a))
    blank_v = sorted(blank)[len(blank) // 2]

    rows = []
    for i in range(len(hs) - 1):
        t, w = hs[i]
        period = hs[i + 1][0] - t
        if period > 1.5 * LINE_US:
            continue          # spans a vertical interval; not an anomaly
        a, b = int((t + 5.4) / dt), int((t + 7.75) / dt)
        if b >= len(samples):
            break
        sg = [x - blank_v for x in samples[a:b]]
        bamp = max(sg) - min(sg)
        a2, b2 = int((t + 10.0) / dt), int((t + 60.0) / dt)
        pk = max(samples[a2:b2]) if b2 < len(samples) else 0.0
        rows.append((i, t, w, period, bamp, pk))
    # Blanked vertical-interval lines carry no picture, so judge peak white
    # only on lines that have some.
    pk_med = sorted(r[5] for r in rows)[len(rows) // 2]
    rows_pic = [r for r in rows if r[5] > 0.5 * pk_med]

    def flag(idx, name, tol, src=None):
        src = src if src is not None else rows
        vals = sorted(r[idx] for r in src)
        med = vals[len(vals) // 2]
        bad = [r for r in src if abs(r[idx] - med) > tol]
        print(f"  {name:>14}: median {med:9.4f}, "
              f"{len(bad)} of {len(src)} lines outside +/-{tol}")
        if bad:
            pos = [r[0] for r in bad[:14]]
            print(f"  {'':>14}  first offenders at line {pos}")
            if len(bad) > 2:
                gaps = [b[0] - a[0] for a, b in zip(bad, bad[1:])]
                g = sorted(gaps)[len(gaps) // 2]
                print(f"  {'':>14}  median spacing {g} lines "
                      f"= {g * 63.556 / 1000:.2f} ms"
                      f"{'  <-- PERIODIC' if gaps.count(g) > len(gaps) // 3 else ''}")
            r = bad[0]
            print(f"  {'':>14}  worst: line {r[0]} at t={r[1]:.3f} us, "
                  f"value {r[idx]:.4f} vs median {med:.4f} "
                  f"(delta {(r[idx] - med) * 1000:+.0f} ns"
                  f"{f', {(r[idx] - med) / (1e6 / 14285714.2857):+.1f} samples'
                     if idx in (2, 3) else ''})")
            anomalies.append((name, r[1]))
        return med

    anomalies = []
    flag(2, "sync width", 0.25)
    flag(3, "line period", 0.10)
    flag(4, "burst amp", 0.040)
    flag(5, "peak white", 0.050, rows_pic)

    # Show the waveform at each anomaly rather than describing it.
    for name, t in anomalies[:3]:
        render_window(samples, dt, t - 4.0, 24.0,
                      f"{name} anomaly at t={t:.3f} us",
                      marks=(t, t + LINE_US))
        render_window(samples, dt, t - 100.0, 800.0,
                      f"{name} anomaly, wide view (vertical interval)")

    f, fhl = colour_fields()
    seq_lines = (f * fhl // 2 if "--color" in sys.argv
                 else (525 if INTERLACED else TOTAL_LINES))
    print(f"  (sequence repeats every {seq_lines} lines = "
          f"{seq_lines * 63.556 / 1000:.2f} ms)")


# ----------------------------------------------------------------------------
# Capture a reference signal and diff it against ours (--capture / --compare)
#
# Measures whatever is on CH1 without assuming it came from this script, so a
# broadcast generator's output can be profiled the same way as ours and the
# two compared parameter by parameter.
# ----------------------------------------------------------------------------
def load_capture(path):
    vals = []
    with open(path) as f:
        head = f.readline()
        for ln in f:
            c = ln.rfind(",")
            vals.append(float(ln[c + 1:]))
    return vals


def profile_capture(samples, dt):
    """Every NTSC parameter we can measure, with no structural assumptions."""
    p = {}
    srt = sorted(samples)
    lo, hi = srt[len(srt) // 200], srt[-(len(srt) // 200) - 1]
    p["extremes"] = (lo, hi)
    runs = runs_below(samples, dt, lo + 0.13 * (hi - lo))
    if len(runs) < 20:
        return None
    narrow = [(t, w) for t, w in runs if w < 3.5]
    normal = [(t, w) for t, w in runs if 3.5 <= w <= 15]
    broad = [(t, w) for t, w in runs if w > 15]
    p["counts"] = (len(normal), len(narrow), len(broad))
    med = lambda xs: sorted(xs)[len(xs) // 2] if xs else float("nan")
    p["w_sync"] = med([w for _, w in normal])
    p["w_eq"] = med([w for _, w in narrow])
    p["w_broad"] = med([w for _, w in broad])

    starts = [t for t, _ in normal]
    gaps = [b - a for a, b in zip(starts, starts[1:]) if 40 < b - a < 90]
    p["line_us"] = med(gaps)
    p["line_jitter_ns"] = (max(gaps) - min(gaps)) * 1000 if gaps else float("nan")
    line_us = p["line_us"]

    # Levels: sync tip mid-pulse, blanking in the front porch, peak picture.
    sv, bv, pv = [], [], []
    for t, w in normal[3:203]:
        a, b = int((t + 1.0) / dt), int((t + min(w, 4.0) - 0.4) / dt)
        if b < len(samples) and b > a:
            sv.append(sum(samples[a:b]) / (b - a))
        a, b = int((t + line_us - 1.4) / dt), int((t + line_us - 0.4) / dt)
        if b < len(samples) and b > a:
            bv.append(sum(samples[a:b]) / (b - a))
        a, b = int((t + 11.0) / dt), int((t + line_us - 2.0) / dt)
        if b < len(samples) and b > a:
            pv.append(max(samples[a:b]))
    p["v_sync"], p["v_blank"], p["v_peak"] = med(sv), med(bv), med(pv)
    depth = p["v_blank"] - p["v_sync"]
    p["sync_mv"] = depth * 1000
    p["ire"] = lambda v: (v - p["v_blank"]) / depth * 40.0 if depth else 0.0

    # Vertical intervals.
    groups = []
    for t, _ in broad:
        if not groups or t - groups[-1][-1] > 5 * line_us:
            groups.append([t])
        else:
            groups[-1].append(t)
    p["groups"] = len(groups)
    p["per_group"] = sorted({len(g) for g in groups})
    if len(groups) >= 2:
        fp = [b[0] - a[0] for a, b in zip(groups, groups[1:])]
        p["field_us"] = med(fp)
        p["lines_per_field"] = p["field_us"] / line_us
    else:
        p["field_us"] = p["lines_per_field"] = float("nan")

    # Is horizontal sync on one continuous grid? Fit sync times against their
    # line numbers and look at the residuals. Taking (t - t0) / line_us modulo
    # one line instead lets a 20 ns error in line_us accumulate into whole
    # lines over a long capture -- that is why it used to report a broadcast
    # generator as re-phasing half a line.
    if len(starts) > 50 and line_us:
        per = line_us
        for _ in range(4):
            nums = [round((t - starts[0]) / per) for t in starts]
            sx = sum(nums)
            sxx = sum(k * k for k in nums)
            st = sum(starts)
            sxt = sum(k * t for k in zip(nums, starts) for k, t in [k])
            m = len(starts)
            den = m * sxx - sx * sx
            if den == 0:
                break
            per = (m * sxt - sx * st) / den
        a0 = (st - per * sx) / m
        resid = [t - (a0 + per * k) for k, t in zip(nums, starts)]
        p["h_phase_spread"] = (max(resid) - min(resid)) / per
        p["line_us_fit"] = per
    else:
        p["h_phase_spread"] = float("nan")
        p["line_us_fit"] = float("nan")

    # Colourburst on the back porch.
    bst = []
    for t, wdt in normal[4:204]:
        # Start past the sync rising edge. A fixed 4.8 us window began inside
        # that edge and reported its own left boundary as the burst start,
        # which is how a reference generator appeared to have no breezeway.
        a, b = int((t + wdt + 0.25) / dt), int((t + 9.2) / dt)
        if b >= len(samples) or b <= a:
            continue
        sg = [x - p["v_blank"] for x in samples[a:b]]
        pk = max(abs(x) for x in sg)
        if pk < 0.02 * max(depth, 1e-9) / 0.286 * 0.286:
            continue
        if pk < 0.015:
            continue
        on = [i for i, x in enumerate(sg) if abs(x) > 0.3 * pk]
        if len(on) < 15:
            continue
        base = a * dt - t
        bst.append((base + on[0] * dt, base + on[-1] * dt, 2 * pk, wdt))
    if bst:
        p["burst_start"] = med([x[0] for x in bst])
        p["burst_end"] = med([x[1] for x in bst])
        p["burst_mv"] = med([x[2] for x in bst]) * 1000
        p["burst_lines"] = len(bst)
        p["breezeway"] = med([x[0] - x[3] for x in bst])
    else:
        p["burst_start"] = p["burst_end"] = p["burst_mv"] = float("nan")
        p["breezeway"] = float("nan")
        p["burst_lines"] = 0

    # The pulse train through one vertical interval, in order.
    p["vbi"] = []
    if groups:
        g0 = groups[len(groups) // 2][0]
        seq = [(t, w) for t, w in runs if g0 - 4 * line_us <= t <= g0 + 8 * line_us]
        prev = None
        for t, w in seq:
            p["vbi"].append((round(w, 2),
                             round(t - prev, 2) if prev is not None else 0.0))
            prev = t
    return p


def show_profile(p, name):
    ire = p["ire"]
    print(f"\n=== {name} ===")
    print(f"  levels      sync {p['v_sync'] * 1000:+8.1f} mV   "
          f"blank {p['v_blank'] * 1000:+8.1f} mV   "
          f"peak {p['v_peak'] * 1000:+8.1f} mV ({ire(p['v_peak']):+.1f} IRE)")
    print(f"  sync depth  {p['sync_mv']:.1f} mV (spec 286)")
    print(f"  pulses      {p['counts'][0]} normal, {p['counts'][1]} equalizing, "
          f"{p['counts'][2]} broad")
    print(f"  widths      h-sync {p['w_sync']:.3f}  eq {p['w_eq']:.3f}  "
          f"broad {p['w_broad']:.3f} us")
    print(f"  line        {p['line_us']:.4f} us = {1e6 / p['line_us']:.2f} Hz, "
          f"jitter {p['line_jitter_ns']:.0f} ns")
    print(f"  vertical    {p['groups']} groups, pulses per group {p['per_group']}")
    print(f"  field       {p['field_us'] / 1000:.4f} ms = "
          f"{1e6 / p['field_us']:.4f} Hz, {p['lines_per_field']:.3f} lines")
    print(f"  h-grid      phase spread {p['h_phase_spread']:.5f} of a line "
          f"({'continuous' if p['h_phase_spread'] < 0.02 else 'RE-PHASES'}); "
          f"fitted line {p['line_us_fit']:.5f} us = "
          f"{1e6 / p['line_us_fit']:.2f} Hz")
    if p["burst_lines"]:
        print(f"  burst       on {p['burst_lines']} lines, "
              f"{p['burst_start']:.3f}..{p['burst_end']:.3f} us after sync, "
              f"{p['burst_mv']:.1f} mV p-p ({p['burst_mv'] / p['sync_mv'] * 40:.1f} IRE)")
        print(f"  breezeway   {p['breezeway']:.3f} us from end of sync to "
              f"start of burst (spec ~0.6)")
    else:
        print(f"  burst       none detected (monochrome)")
    print(f"  vertical interval, pulse widths and spacings (us):")
    for i in range(0, min(len(p["vbi"]), 48), 8):
        chunk = p["vbi"][i:i + 8]
        print("    " + "  ".join(f"{w:5.2f}/{g:6.2f}" for w, g in chunk))


def compare_captures(path_a, path_b, dt=0.020):
    a = profile_capture(load_capture(path_a), dt)
    b = profile_capture(load_capture(path_b), dt)
    if not a or not b:
        raise RuntimeError("one of the captures has too few sync pulses")
    show_profile(a, f"REFERENCE  {path_a}")
    show_profile(b, f"OURS       {path_b}")
    print("\n=== differences ===")
    rows = (("sync depth mV", "sync_mv", 10.0), ("h-sync us", "w_sync", 0.15),
            ("eq us", "w_eq", 0.15), ("broad us", "w_broad", 0.5),
            ("line us", "line_us", 0.02),
            ("line jitter ns", "line_jitter_ns", 60.0),
            ("field ms", "field_us", 30.0),
            ("lines/field", "lines_per_field", 0.3),
            ("h phase spread", "h_phase_spread", 0.02),
            ("burst start us", "burst_start", 0.3),
            ("breezeway us", "breezeway", 0.2),
            ("burst mV", "burst_mv", 40.0))
    for name, key, tol in rows:
        x, y = a.get(key, float("nan")), b.get(key, float("nan"))
        if key == "field_us":
            x, y = x / 1000.0, y / 1000.0
        bad = not (abs(x - y) <= tol) if x == x and y == y else True
        print(f"  {name:>16}  ref {x:12.4f}   ours {y:12.4f}   "
              f"{'DIFFERENT' if bad else 'match'}")
    print(f"  {'vertical groups':>16}  ref {a['per_group']}   "
          f"ours {b['per_group']}   "
          f"{'match' if a['per_group'] == b['per_group'] else 'DIFFERENT'}")


def capture_only(dwf, path, seconds=0.25, scope_rate=50e6):
    """Capture CH1 with nothing generating, for profiling a reference feed."""
    ndev, ncfg = ctypes.c_int(), ctypes.c_int()
    check(dwf, dwf.FDwfEnum(ctypes.c_int(0), ctypes.byref(ndev)), "FDwfEnum")
    check(dwf, dwf.FDwfEnumConfig(ctypes.c_int(0), ctypes.byref(ncfg)),
          "FDwfEnumConfig")
    best, best_buf = 0, -1
    for i in range(ncfg.value):
        sb = ctypes.c_int()
        dwf.FDwfEnumConfigInfo(ctypes.c_int(i), DECIAnalogInBufferSize,
                               ctypes.byref(sb))
        if sb.value > best_buf:
            best, best_buf = i, sb.value
    hdwf = ctypes.c_int()
    check(dwf, dwf.FDwfDeviceConfigOpen(ctypes.c_int(0), ctypes.c_int(best),
                                        ctypes.byref(hdwf)), "DeviceConfigOpen")
    print(f"  config {best}, scope buffer {best_buf}")
    try:
        setup_scope(dwf, hdwf)
        capture(dwf, hdwf, scope_rate, 1 << 16)          # discard: settling
        n = min(int(scope_rate * seconds), best_buf)
        samples = capture(dwf, hdwf, scope_rate, n)
        dt = 1e6 / scope_rate
        with open(path, "w") as f:
            f.write("t_us,volts\n")
            for i, v in enumerate(samples):
                f.write(f"{i * dt:.6f},{v:.6f}\n")
        print(f"  wrote {path} ({n} samples, {n * dt / 1000:.1f} ms)")
        p = profile_capture(samples, dt)
        if p:
            show_profile(p, path)
        else:
            print("  too few sync pulses to profile -- is a signal connected?")
    finally:
        dwf.FDwfDeviceClose(hdwf)


def ntsc_analyze(dwf, hdwf, seconds=0.05, scope_rate=50e6, save=None):
    """Measure what a vectorscope actually keys on, from the real signal.

    Reports levels, burst placement/duration/amplitude/frequency, the
    fsc/fH ratio, and the line-to-line burst phase relationship. Makes no
    assumption that the signal is ours -- it measures whatever is on CH1,
    so it is equally valid on a loop-through from other equipment.
    """
    print("\nNTSC conformance analysis of CH1...")
    n = int(scope_rate * seconds)
    bmin, bmax = ctypes.c_int(), ctypes.c_int()
    dwf.FDwfAnalogInBufferSizeInfo(hdwf, ctypes.byref(bmin), ctypes.byref(bmax))
    n = min(n, bmax.value)
    samples = capture(dwf, hdwf, scope_rate, n)
    dt = 1e6 / scope_rate                       # us per sample
    if save:
        with open(save, "w") as f:
            f.write("t_us,volts\n")
            for i, v in enumerate(samples):
                f.write(f"{i * dt:.6f},{v:.6f}\n")
        print(f"  wrote raw capture to {save} ({len(samples)} samples)")
    ordered = sorted(samples)
    lo = ordered[len(ordered) // 200]
    hi = ordered[-(len(ordered) // 200) - 1]
    print(f"  capture {n} samples, {n * dt / 1000:.1f} ms at "
          f"{scope_rate / 1e6:.0f} MS/s; extremes {lo:+.4f}..{hi:+.4f} V")
    if hi - lo < 0.2:
        print("  no signal on CH1.")
        return

    runs = runs_below(samples, dt, lo + 0.13 * (hi - lo))
    hs = [(t, w) for t, w in runs if 3.5 <= w <= 15]
    if len(hs) < 20:
        print(f"  only {len(hs)} horizontal sync pulses found.")
        return
    starts = [t for t, _ in hs]
    gaps = [b - a for a, b in zip(starts, starts[1:])
            if 0.9 * LINE_US < b - a < 1.1 * LINE_US]
    line_us = sum(gaps) / len(gaps)
    fh = 1e6 / line_us

    def seg(t0, t1, s0):
        a, b = int((s0 + t0) / dt), int((s0 + t1) / dt)
        return samples[max(a, 0):min(b, len(samples))]

    # Levels, averaged over many lines.
    sync_v, blank_v, peak_v = [], [], []
    for t0, _w in hs[2:202]:
        s = seg(1.0, 4.0, t0)
        if s:
            sync_v.append(sum(s) / len(s))
        s = seg(8.2, 9.2, t0)                   # back porch after burst
        if s:
            blank_v.append(sum(s) / len(s))
        s = seg(10.0, 60.0, t0)
        if s:
            peak_v.append(max(s))
    sync_v = sorted(sync_v)[len(sync_v) // 2]
    blank_v = sorted(blank_v)[len(blank_v) // 2]
    peak_v = sorted(peak_v)[len(peak_v) // 2]
    ire = lambda v: (v - blank_v) / (blank_v - sync_v) * 40.0
    print(f"  levels: sync tip {sync_v * 1000:+7.1f} mV ({ire(sync_v):+6.1f} IRE)"
          f"   blanking {blank_v * 1000:+7.1f} mV ({ire(blank_v):+5.1f} IRE)")
    print(f"          peak white {peak_v * 1000:+7.1f} mV "
          f"({ire(peak_v):+6.1f} IRE); sync depth "
          f"{(blank_v - sync_v) * 1000:.1f} mV (want 286)")
    print(f"  blanking DC offset from 0 V: {blank_v * 1000:+.1f} mV "
          f"{'(standard)' if abs(blank_v) < 0.03 else '(NON-STANDARD)'}")

    # Burst: find the envelope, then take the frequency from a DFT over it.
    # Counting blanking-level crossings across the whole window counts NOISE
    # in the flat porch either side of the burst, which reported 17.5 cycles
    # at 5.4 MHz for a burst that is demonstrably 9 cycles at 3.58 MHz.
    res = []
    for t0, _w in hs[4:84]:
        sg = seg(_w + 0.25, 8.8, t0)
        if len(sg) < 40:
            continue
        sg = [x - blank_v for x in sg]
        pk = max(abs(x) for x in sg)
        if pk < 0.02:
            continue
        on = [i for i, x in enumerate(sg) if abs(x) > 0.25 * pk]
        if len(on) < 20:
            continue
        env0 = _w + 0.25 + on[0] * dt
        env1 = _w + 0.25 + on[-1] * dt
        wv = sg[on[0]:on[-1] + 1]
        best, bf, f = 0.0, 0.0, 2.0e6
        while f <= 9.0e6:
            re = im = 0.0
            for i2, x in enumerate(wv):
                a2 = 2.0 * math.pi * f * i2 * dt * 1e-6
                re += x * math.cos(a2)
                im += x * math.sin(a2)
            mag = math.hypot(re, im) / len(wv)
            if mag > best:
                best, bf = mag, f
            f += 2.5e3
        res.append((env0, env1, bf, 2.0 * pk))
    if not res:
        print("  NO COLOURBURST DETECTED on the back porch.")
        print("  (a vectorscope treats this as monochrome and blanks the "
              "vector display)")
        return
    mid = lambda xs: sorted(xs)[len(xs) // 2]
    b_start, b_end = mid([r[0] for r in res]), mid([r[1] for r in res])
    fsc, b_amp = mid([r[2] for r in res]), mid([r[3] for r in res])
    cycles = (b_end - b_start) * fsc / 1e6
    print(f"  burst on {len(res)} of {len(hs[4:84])} lines examined")
    print(f"    envelope {b_start:.3f}..{b_end:.3f} us after sync edge "
          f"(spec start 4.71..5.3)")
    print(f"    {cycles:.1f} cycles (spec 8..10), "
          f"amplitude {b_amp * 1000:.1f} mV p-p "
          f"({b_amp / (blank_v - sync_v) * 40:.1f} IRE, spec 40)")
    fine = measure_fsc_precise(samples, dt, [t for t, _ in hs], blank_v,
                               line_us)
    print(f"    frequency {fine:.1f} Hz = {fine / 1e6:.6f} MHz "
          f"({fine - 3579545.45:+.1f} Hz vs 3.579545, spec +/-10 Hz) "
          f"{'IN SPEC' if abs(fine - 3579545.45) <= 10 else 'OUT OF SPEC'}")
    print(f"    (single-burst DFT said {fsc / 1e6:.3f} MHz; its main lobe is "
          f"~400 kHz wide so that figure is meaningless)")
    fsc = fine
    print(f"  line rate {fh:.2f} Hz; fsc/fH = {fsc / fh:.4f} "
          f"(NTSC requires 227.5)")
    err = abs(fsc / fh - 227.5)
    print(f"    {'LOCKED' if err < 0.05 else 'NOT LOCKED'} "
          f"-- error {err:.4f} cycles/line")

    # Render the first 13 us of a line: sync, back porch, burst, picture start.
    t0 = hs[8][0]
    span = seg(0.0, 13.0, t0)
    if span:
        vmin, vmax = min(span), max(span)
        rows, cols = 17, 104
        step = max(1, len(span) // cols)
        grid = [[" "] * cols for _ in range(rows)]
        for c in range(min(cols, len(span) // step)):
            v = span[c * step]
            r = int((vmax - v) / (vmax - vmin + 1e-12) * (rows - 1))
            grid[min(max(r, 0), rows - 1)][c] = "*"
        print(f"\n  one line, first 13 us ({vmin * 1000:+.0f}.."
              f"{vmax * 1000:+.0f} mV, {step * dt * 1000:.0f} ns per column):")
        for r, row in enumerate(grid):
            v = vmax - r * (vmax - vmin) / (rows - 1)
            print(f"   {v * 1000:+7.0f} mV |{''.join(row)}")
        marks = [" "] * cols
        for us_mark in (4.7, 5.3, 7.8, 9.4):
            c = int(us_mark / dt / step)
            if 0 <= c < cols:
                marks[c] = "^"
        print(f"           {''.join(marks)}")
        print(f"           ^ = 4.7 (sync end)  5.3 (burst start)  "
              f"7.8 (burst end)  9.4 (picture)")


# ----------------------------------------------------------------------------
# Loopback measurement of the real output
# ----------------------------------------------------------------------------
def capture_while_feeding(dwf, hdwf, ch, data, total, pos, rate_hz,
                          nsamples):
    """Acquire on CH1 while continuing to push play data.

    A blocking capture stops the feed for long enough that the FIFO can empty,
    which would make a working stream look like a parked output.
    """
    check(dwf, dwf.FDwfAnalogInFrequencySet(hdwf, ctypes.c_double(rate_hz)),
          "InFrequencySet")
    check(dwf, dwf.FDwfAnalogInBufferSizeSet(hdwf, ctypes.c_int(nsamples)),
          "InBufferSizeSet")
    check(dwf, dwf.FDwfAnalogInConfigure(hdwf, ctypes.c_int(1),
                                         ctypes.c_int(1)), "InConfigure")
    sts = ctypes.c_ubyte()
    free = ctypes.c_int()
    lost, corrupt = ctypes.c_int(), ctypes.c_int()
    deadline = time.time() + 5.0
    while True:
        dwf.FDwfAnalogOutNodePlayStatus(hdwf, ch, AnalogOutNodeCarrier,
                                        ctypes.byref(free),
                                        ctypes.byref(lost),
                                        ctypes.byref(corrupt))
        w = min(free.value, 1 << 16)
        while w > 0:
            take = min(w, total - pos)
            dwf.FDwfAnalogOutNodePlayData(hdwf, ch, AnalogOutNodeCarrier,
                                          ctypes.byref(data, pos * 8),
                                          ctypes.c_int(take))
            pos = (pos + take) % total
            w -= take
        check(dwf, dwf.FDwfAnalogInStatus(hdwf, ctypes.c_int(1),
                                          ctypes.byref(sts)), "InStatus")
        if sts.value == DwfStateDone or time.time() > deadline:
            break
    buf = (ctypes.c_double * nsamples)()
    check(dwf, dwf.FDwfAnalogInStatusData(hdwf, ctypes.c_int(0), buf,
                                          ctypes.c_int(nsamples)),
          "InStatusData")
    return list(buf), pos


def ramp_play_once(dwf, hdwf, ch, data, N, want, scale,
                   repeat=0, run_s=0.0, wait_s=0.0, seconds=0.7,
                   func=None):
    """Configure a play function for one ramp and report what reaches W1."""
    func = func if func is not None else funcPlay
    dwf.FDwfAnalogOutReset(hdwf, ch)
    dwf.FDwfDeviceAutoConfigureSet(hdwf, ctypes.c_int(0))
    dwf.FDwfAnalogOutNodeEnableSet(hdwf, ch, AnalogOutNodeCarrier,
                                   ctypes.c_int(1))
    dwf.FDwfAnalogOutNodeFunctionSet(hdwf, ch, AnalogOutNodeCarrier, func)
    dwf.FDwfAnalogOutRepeatSet(hdwf, ch, ctypes.c_int(repeat))
    dwf.FDwfAnalogOutRunSet(hdwf, ch, ctypes.c_double(run_s))
    if wait_s:
        dwf.FDwfAnalogOutWaitSet(hdwf, ch, ctypes.c_double(wait_s))
    dwf.FDwfAnalogOutNodeFrequencySet(hdwf, ch, AnalogOutNodeCarrier,
                                      ctypes.c_double(want / scale))
    dwf.FDwfAnalogOutNodeAmplitudeSet(hdwf, ch, AnalogOutNodeCarrier,
                                      ctypes.c_double(1.0))
    dwf.FDwfAnalogOutNodeOffsetSet(hdwf, ch, AnalogOutNodeCarrier,
                                   ctypes.c_double(0.0))
    dmin, cb = ctypes.c_int(), ctypes.c_int()
    dwf.FDwfAnalogOutNodeDataInfo(hdwf, ch, AnalogOutNodeCarrier,
                                  ctypes.byref(dmin), ctypes.byref(cb))
    n0 = min(cb.value, N)
    dwf.FDwfAnalogOutNodeDataSet(hdwf, ch, AnalogOutNodeCarrier, data,
                                 ctypes.c_int(n0))
    if dwf.FDwfAnalogOutConfigure(hdwf, ch, ctypes.c_int(1)) != 1:
        return None

    sts, free = ctypes.c_ubyte(), ctypes.c_int()
    lost, corrupt = ctypes.c_int(), ctypes.c_int()
    pos, fed = n0 % N, 0
    t0 = time.time()
    while time.time() - t0 < seconds + wait_s:
        dwf.FDwfAnalogOutStatus(hdwf, ch, ctypes.byref(sts))
        dwf.FDwfAnalogOutNodePlayStatus(hdwf, ch, AnalogOutNodeCarrier,
                                        ctypes.byref(free), ctypes.byref(lost),
                                        ctypes.byref(corrupt))
        w = min(free.value, 1 << 16)
        while w > 0:
            take = min(w, N - pos)
            dwf.FDwfAnalogOutNodePlayData(hdwf, ch, AnalogOutNodeCarrier,
                                          ctypes.byref(data, pos * 8),
                                          ctypes.c_int(take))
            pos = (pos + take) % N
            w -= take
            fed += take
    samples, pos = capture_while_feeding(dwf, hdwf, ch, data, N, pos,
                                         10e6, 1 << 15)
    gf = ctypes.c_double()
    dwf.FDwfAnalogOutNodeFrequencyGet(hdwf, ch, AnalogOutNodeCarrier,
                                      ctypes.byref(gf))
    return (sts.value, fed, gf.value, lost.value, corrupt.value,
            min(samples), max(samples))


def play_configs(dwf):
    """Try both streaming functions in every device configuration.

    funcPlay (31) and funcPlayPattern (29) are different functions, and the
    shipped examples use both. funcPlay drives the output only in the
    configurations reporting a 0-sample play buffer -- which are exactly the
    ones with no slack to absorb host latency. If funcPlayPattern works in a
    configuration that has the 67 M buffer, streaming stops being marginal.
    """
    ndev, ncfg = ctypes.c_int(), ctypes.c_int()
    check(dwf, dwf.FDwfEnum(ctypes.c_int(0), ctypes.byref(ndev)), "FDwfEnum")
    check(dwf, dwf.FDwfEnumConfig(ctypes.c_int(0), ctypes.byref(ncfg)),
          "FDwfEnumConfig")

    N, want = 2000, 1e6
    print(f"\nA {N}-sample ramp through each play function and configuration")
    print(f"  {'cfg':>3} {'function':>16} {'play FIFO':>11} {'scale':>6} "
          f"{'state':>5} {'fed':>8} {'CH1 min':>8} {'CH1 max':>8}  verdict")
    for i in range(ncfg.value):
        hdwf = ctypes.c_int()
        if dwf.FDwfDeviceConfigOpen(ctypes.c_int(0), ctypes.c_int(i),
                                    ctypes.byref(hdwf)) != 1 or not hdwf.value:
            print(f"  {i:3d}  cannot open: {last_error(dwf)}")
            continue
        ch = ctypes.c_int(0)
        try:
            data = (ctypes.c_double * N)()
            data[:] = [-1.0 + 2.0 * (k / (N - 1)) for k in range(N)]
            setup_scope(dwf, hdwf)
            capture(dwf, hdwf, 10e6, 1 << 14)
            for fname, fid in (("funcPlay", 31), ("funcPlayPattern", 29)):
                fn = ctypes.c_int(fid)
                dwf.FDwfAnalogOutNodeFunctionSet(hdwf, ch, AnalogOutNodeCarrier,
                                                 fn)
                pmin, pmax = ctypes.c_int(), ctypes.c_int()
                dwf.FDwfAnalogOutNodePlayInfo(hdwf, ch, AnalogOutNodeCarrier,
                                              ctypes.byref(pmin),
                                              ctypes.byref(pmax))
                scale = PLAY_SCALE_OVERRIDE or play_rate_scale(dwf, hdwf, ch)
                r = ramp_play_once(dwf, hdwf, ch, data, N, want, scale,
                                   func=fn)
                if r is None:
                    print(f"  {i:3d} {fname:>16} {pmax.value:11d} "
                          f"{scale:6.2f}  configure failed")
                    continue
                st, fed, rate, lost, corrupt, lo, hi = r
                ok = "OUTPUT" if hi - lo > 0.2 else "flat"
                print(f"  {i:3d} {fname:>16} {pmax.value:11d} {scale:6.2f} "
                      f"{st:5d} {fed:8d} {lo:+8.3f} {hi:+8.3f}  {ok}")
        finally:
            dwf.FDwfAnalogOutReset(hdwf, ch)
            dwf.FDwfDeviceClose(hdwf)


def play_test(dwf, hdwf, ch):
    """Minimal funcPlay check: stream a ramp and see if anything reaches W1.

    Isolates play-mode plumbing from the video waveform. The shipped example
    uses a finite run length with repeat 1; this script used run 0 / repeat 0
    for continuous output, and the output was flat. If only the finite-run
    variants produce a signal, then 0 does not mean "infinite" here.
    """
    print("\nMinimal funcPlay test (needs W1 -> CH1)...")
    N = 2000
    data = (ctypes.c_double * N)()
    data[:] = [-1.0 + 2.0 * (i / (N - 1)) for i in range(N)]   # ramp -1..+1
    dwf.FDwfAnalogOutNodeFunctionSet(hdwf, ch, AnalogOutNodeCarrier, funcPlay)
    scale = PLAY_SCALE_OVERRIDE or play_rate_scale(dwf, hdwf, ch)
    want = 1e6                                   # 1 MS/s -> 500 Hz ramp
    print(f"  ramp {N} samples at {want / 1e6:.3f} MS/s "
          f"(scale {scale:.3f}x), amplitude 1 V, offset 0 V")
    setup_scope(dwf, hdwf)
    capture(dwf, hdwf, 10e6, 1 << 14)            # discard: settling

    sts, free = ctypes.c_ubyte(), ctypes.c_int()
    lost, corrupt = ctypes.c_int(), ctypes.c_int()
    dmin, cb = ctypes.c_int(), ctypes.c_int()
    for label, repeat, run_s, wait_s in (
            ("example style: repeat 1, finite run", 1, N / want, 0.0),
            ("repeat 0, run 0 (what we were doing)", 0, 0.0, 0.0),
            ("repeat 0, run 0, wait 1 s", 0, 0.0, 1.0),
            ("repeat 1, long finite run (3600 s)", 1, 3600.0, 0.0)):
        dwf.FDwfAnalogOutReset(hdwf, ch)
        dwf.FDwfDeviceAutoConfigureSet(hdwf, ctypes.c_int(0))
        dwf.FDwfAnalogOutNodeEnableSet(hdwf, ch, AnalogOutNodeCarrier,
                                       ctypes.c_int(1))
        dwf.FDwfAnalogOutNodeFunctionSet(hdwf, ch, AnalogOutNodeCarrier,
                                         funcPlay)
        dwf.FDwfAnalogOutRepeatSet(hdwf, ch, ctypes.c_int(repeat))
        dwf.FDwfAnalogOutRunSet(hdwf, ch, ctypes.c_double(run_s))
        if wait_s:
            dwf.FDwfAnalogOutWaitSet(hdwf, ch, ctypes.c_double(wait_s))
        dwf.FDwfAnalogOutNodeFrequencySet(hdwf, ch, AnalogOutNodeCarrier,
                                          ctypes.c_double(want / scale))
        dwf.FDwfAnalogOutNodeAmplitudeSet(hdwf, ch, AnalogOutNodeCarrier,
                                          ctypes.c_double(1.0))
        dwf.FDwfAnalogOutNodeOffsetSet(hdwf, ch, AnalogOutNodeCarrier,
                                       ctypes.c_double(0.0))
        dwf.FDwfAnalogOutNodeDataInfo(hdwf, ch, AnalogOutNodeCarrier,
                                      ctypes.byref(dmin), ctypes.byref(cb))
        n0 = min(cb.value, N)
        dwf.FDwfAnalogOutNodeDataSet(hdwf, ch, AnalogOutNodeCarrier, data,
                                     ctypes.c_int(n0))
        if dwf.FDwfAnalogOutConfigure(hdwf, ch, ctypes.c_int(1)) != 1:
            print(f"  {label:38} configure failed: {last_error(dwf)}")
            continue

        pos, fed = n0 % N, 0
        t0 = time.time()
        while time.time() - t0 < 1.0 + wait_s:
            dwf.FDwfAnalogOutStatus(hdwf, ch, ctypes.byref(sts))
            dwf.FDwfAnalogOutNodePlayStatus(hdwf, ch, AnalogOutNodeCarrier,
                                            ctypes.byref(free),
                                            ctypes.byref(lost),
                                            ctypes.byref(corrupt))
            w = min(free.value, 1 << 16)
            while w > 0:
                take = min(w, N - pos)
                dwf.FDwfAnalogOutNodePlayData(
                    hdwf, ch, AnalogOutNodeCarrier,
                    ctypes.byref(data, pos * 8), ctypes.c_int(take))
                pos = (pos + take) % N
                w -= take
                fed += take

        samples, pos = capture_while_feeding(dwf, hdwf, ch, data, N, pos,
                                             10e6, 1 << 15)
        lo, hi = min(samples), max(samples)
        gf = ctypes.c_double()
        dwf.FDwfAnalogOutNodeFrequencyGet(hdwf, ch, AnalogOutNodeCarrier,
                                          ctypes.byref(gf))
        verdict = "  <-- OUTPUT" if hi - lo > 0.2 else "  (flat)"
        print(f"  {label:38} state {sts.value} fed {fed:7d} "
              f"rate {gf.value / 1e6:7.3f} lost {lost.value} "
              f"corrupt {corrupt.value} CH1 {lo:+.3f}..{hi:+.3f} V{verdict}")
    dwf.FDwfAnalogOutReset(hdwf, ch)


FUNC_NAMES = (("funcCustomPattern", 28), ("funcPlayPattern", 29),
              ("funcCustom", 30), ("funcPlay", 31))


def pattern_info(dwf):
    """How many samples each output function will accept, per configuration.

    FDwfAnalogOutNodeDataInfo returns a different maximum depending on which
    function is selected. Only funcPlay was ever queried here, which reported
    32768 and was taken as the ceiling -- while the configuration advertises a
    67 M play buffer. If a looping function accepts the whole sequence, the
    hardware plays it with no host streaming at all.
    """
    ndev, ncfg = ctypes.c_int(), ctypes.c_int()
    check(dwf, dwf.FDwfEnum(ctypes.c_int(0), ctypes.byref(ndev)), "FDwfEnum")
    check(dwf, dwf.FDwfEnumConfig(ctypes.c_int(0), ctypes.byref(ncfg)),
          "FDwfEnumConfig")
    print("\nSamples accepted by FDwfAnalogOutNodeDataSet, per function:")
    hdr = "  cfg  " + "  ".join(f"{n:>18}" for n, _ in FUNC_NAMES)
    print(hdr)
    ch = ctypes.c_int(0)
    for i in range(ncfg.value):
        hdwf = ctypes.c_int()
        if dwf.FDwfDeviceConfigOpen(ctypes.c_int(0), ctypes.c_int(i),
                                    ctypes.byref(hdwf)) != 1 or not hdwf.value:
            print(f"  {i:3d}  (cannot open)")
            continue
        try:
            cells = []
            fsfunc = ctypes.c_uint()
            dwf.FDwfAnalogOutNodeFunctionInfo(hdwf, ch, AnalogOutNodeCarrier,
                                              ctypes.byref(fsfunc))
            for name, fid in FUNC_NAMES:
                if not (fsfunc.value >> fid) & 1:
                    cells.append(f"{'unsupported':>18}")
                    continue
                dwf.FDwfAnalogOutNodeFunctionSet(hdwf, ch, AnalogOutNodeCarrier,
                                                 ctypes.c_int(fid))
                dmin, dmax = ctypes.c_int(), ctypes.c_int()
                dwf.FDwfAnalogOutNodeDataInfo(hdwf, ch, AnalogOutNodeCarrier,
                                              ctypes.byref(dmin),
                                              ctypes.byref(dmax))
                cells.append(f"{dmax.value:>18d}")
            print(f"  {i:3d}  " + "  ".join(cells))
        finally:
            dwf.FDwfDeviceClose(hdwf)
    need = frame_lines() * 908
    print(f"\n  one interlaced frame at 908 samples/line = {need} samples")
    print(f"  four-field colour sequence              = {2 * need} samples")
    print("  any column at or above those needs no host streaming at all.")


def play_info(dwf, hdwf):
    """Report AWG streaming (funcPlay) capability.

    funcCustom loops a 32 Ki buffer, which caps us near 850 kS/s and rules
    out the 3.58 MHz colour subcarrier entirely. funcPlay streams from the
    host instead: no buffer limit, no four-field coherence problem (nothing
    loops), and the ceiling is USB throughput rather than memory depth.
    """
    print("\nAWG streaming (funcPlay):")
    ch = ctypes.c_int(0)
    fsfunc = ctypes.c_uint()
    dwf.FDwfAnalogOutNodeFunctionInfo(hdwf, ch, AnalogOutNodeCarrier,
                                      ctypes.byref(fsfunc))
    print(f"  funcPlay supported: {bool((fsfunc.value >> 31) & 1)}")

    pmin, pmax = ctypes.c_int(), ctypes.c_int()
    if dwf.FDwfAnalogOutNodePlayInfo(hdwf, ch, AnalogOutNodeCarrier,
                                     ctypes.byref(pmin), ctypes.byref(pmax)):
        print(f"  device play FIFO: {pmin.value}..{pmax.value} samples")

    check(dwf, dwf.FDwfAnalogOutNodeFunctionSet(hdwf, ch, AnalogOutNodeCarrier,
                                                funcPlay), "set funcPlay")
    fmin, fmax = ctypes.c_double(), ctypes.c_double()
    if dwf.FDwfAnalogOutNodeFrequencyInfo(hdwf, ch, AnalogOutNodeCarrier,
                                          ctypes.byref(fmin),
                                          ctypes.byref(fmax)):
        print(f"  play sample rate: {fmin.value:.1f} .. "
              f"{fmax.value / 1e6:.3f} MS/s")
        for want in (4 * 3579545.0, 100e6 / 7, 100e6 / 4):
            ok = "OK" if want <= fmax.value else "TOO FAST"
            print(f"    {want / 1e6:9.6f} MS/s -> {ok}")

    # Map requested -> accepted play rate. The ADP2230 took 4x what was
    # asked for at 14.285714 MS/s, so the relationship has to be measured
    # rather than assumed before any rate can be trusted.
    print(f"\n  requested vs accepted play rate:")
    print(f"    {'requested':>14} {'accepted':>14} {'ratio':>8}")
    got = ctypes.c_double()
    for want in (1e6, 2e6, 3.571428e6, 7.142857e6, 14.285714e6,
                 14.31818e6, 20e6, 25e6, 50e6, 100e6):
        if dwf.FDwfAnalogOutNodeFrequencySet(hdwf, ch, AnalogOutNodeCarrier,
                                             ctypes.c_double(want)) != 1:
            print(f"    {want / 1e6:11.6f} MS/s  {'rejected':>14}")
            continue
        dwf.FDwfAnalogOutNodeFrequencyGet(hdwf, ch, AnalogOutNodeCarrier,
                                          ctypes.byref(got))
        ratio = got.value / want if want else 0
        print(f"    {want / 1e6:11.6f} MS/s {got.value / 1e6:11.6f} MS/s "
              f"{ratio:8.4f}")

    fsc = 3579545.0
    print(f"\n  divider options that fit a colour subcarrier:")
    print(f"    {'rate':>12} {'smp/line':>9} {'line rate':>11} {'err':>9} "
          f"{'smp/cycle':>10} {'MB/s wire':>10}")
    for n in range(4, 12):
        rate = AWG_CLOCK_HZ / n
        spl = round(rate / LINE_FREQ_HZ)
        spl += spl % 2
        line_hz = rate / spl
        print(f"    {rate / 1e6:9.6f} MS/s {spl:9d} {line_hz:9.2f} Hz "
              f"{(line_hz / LINE_FREQ_HZ - 1) * 100:+8.4f}% "
              f"{rate / fsc:10.2f} {rate * 2 / 1e6:9.0f}")


def setup_scope(dwf, hdwf, range_v=5.0):
    """Configure the input stage once and let it settle.

    The first acquisition after a range/offset change comes back at the wrong
    level until the input stage stabilizes, which is what made the earlier
    fine-pass capture read 0.001..0.105 V while the next one was correct.
    """
    check(dwf, dwf.FDwfAnalogInChannelEnableSet(hdwf, ctypes.c_int(0),
                                                ctypes.c_int(1)),
          "InChannelEnableSet")
    dwf.FDwfAnalogInChannelFilterSet(hdwf, ctypes.c_int(0), filterDecimate)
    check(dwf, dwf.FDwfAnalogInChannelRangeSet(hdwf, ctypes.c_int(0),
                                               ctypes.c_double(range_v)),
          "InChannelRangeSet")
    dwf.FDwfAnalogInChannelOffsetSet(hdwf, ctypes.c_int(0), ctypes.c_double(0.0))
    check(dwf, dwf.FDwfAnalogInAcquisitionModeSet(hdwf, acqmodeSingle),
          "InAcquisitionModeSet")
    dwf.FDwfAnalogInTriggerSourceSet(hdwf, trigsrcNone)
    check(dwf, dwf.FDwfAnalogInConfigure(hdwf, ctypes.c_int(1),
                                         ctypes.c_int(0)), "InConfigure")
    time.sleep(2.0)


def capture(dwf, hdwf, rate_hz, nsamples):
    check(dwf, dwf.FDwfAnalogInFrequencySet(hdwf, ctypes.c_double(rate_hz)),
          "InFrequencySet")
    check(dwf, dwf.FDwfAnalogInBufferSizeSet(hdwf, ctypes.c_int(nsamples)),
          "InBufferSizeSet")
    check(dwf, dwf.FDwfAnalogInConfigure(hdwf, ctypes.c_int(1),
                                         ctypes.c_int(1)), "InConfigure")

    sts = ctypes.c_ubyte()
    deadline = time.time() + 30.0
    while True:
        check(dwf, dwf.FDwfAnalogInStatus(hdwf, ctypes.c_int(1),
                                          ctypes.byref(sts)), "InStatus")
        if sts.value == DwfStateDone:
            break
        if time.time() > deadline:
            raise RuntimeError("Scope acquisition timed out.")
        time.sleep(0.005)

    buf = (ctypes.c_double * nsamples)()
    check(dwf, dwf.FDwfAnalogInStatusData(hdwf, ctypes.c_int(0), buf,
                                          ctypes.c_int(nsamples)),
          "InStatusData")
    return list(buf)


def stats(name, want, vals, unit="us"):
    if not vals:
        return
    mean = sum(vals) / len(vals)
    extra = ""
    if len(vals) > 1:
        extra = (f", spread {(max(vals) - mean) * 1000:+.0f}/"
                 f"{(min(vals) - mean) * 1000:+.0f} ns")
    print(f"  {name:>12} {mean:9.4f} {unit}  (want {want:7.4f}), "
          f"n={len(vals)}{extra}")


def plot_edges(offsets, mean, rows=44, width=61):
    """Strip-plot bar-edge position against line number.

    All picture lines are identical in the uploaded buffer, so this should be
    a straight vertical line. A sawtooth here is the "shark's teeth" showing
    up in the signal itself; a straight line means they are the display's.
    """
    dev = [(o - mean) * 1000.0 for o in offsets]          # ns
    span = max(20.0, max(abs(d) for d in dev))
    # Consecutive lines, not decimated: a tooth only a few lines long would
    # alias away if we sampled every Nth line.
    print(f"  edge position, first {min(rows, len(dev))} consecutive lines "
          f"(+/-{span:.0f} ns full scale, over all {len(dev)} lines):")
    mid = width // 2
    for i in range(min(len(dev), rows)):
        col = mid + int(round(dev[i] / span * mid))
        col = min(width - 1, max(0, col))
        bar = [" "] * width
        bar[mid] = "|"
        bar[col] = "*"
        print(f"   line {i:4d} {''.join(bar)}")


def sync_pulses(samples, rate_hz, label):
    """Find sync pulses in a real capture, with a level-adaptive threshold."""
    dt_us = 1e6 / rate_hz
    ordered = sorted(samples)
    lo = ordered[len(ordered) // 100]
    hi = ordered[-(len(ordered) // 100) - 1]
    # Slice between sync tip and blanking. 13% of the span puts it near
    # -22 IRE: below the -16 IRE that saturated red and blue chroma legally
    # reaches on 75% colour bars, and well above the -40 IRE sync tip. A 20%
    # slice sits at -12 IRE and false-triggers on chroma.
    thresh = lo + 0.13 * (hi - lo)
    runs = runs_below(samples, dt_us, thresh)
    print(f"  [{label}] {rate_hz / 1e6:.3f} MS/s, {len(samples)} samples, "
          f"{len(samples) * dt_us / 1000:.2f} ms span")
    print(f"  [{label}] levels {lo:.3f}..{hi:.3f} V, threshold {thresh:.3f} V, "
          f"{len(runs)} pulses")
    if hi - lo < 0.2:
        print(f"  [{label}] almost no signal -- is W1 jumpered to CH1?")
        return None
    if len(runs) < 4:
        print(f"  [{label}] too few sync pulses to analyze.")
        return None
    return runs


def verify(dwf, hdwf, frame_hz, source="W1", groups_per_seq=None):
    """Measure the real output: pulse widths, line period and jitter, the
    vertical sync groups, the frame period, and lines per frame."""
    print(f"\nMeasuring the actual output "
          f"(needs a {source} -> CH1 jumper)...")
    rmin, rmax = ctypes.c_double(), ctypes.c_double()
    dwf.FDwfAnalogInFrequencyInfo(hdwf, ctypes.byref(rmin), ctypes.byref(rmax))
    bmin, bmax = ctypes.c_int(), ctypes.c_int()
    dwf.FDwfAnalogInBufferSizeInfo(hdwf, ctypes.byref(bmin), ctypes.byref(bmax))
    print(f"  scope: up to {rmax.value / 1e6:.1f} MS/s, "
          f"{bmax.value} sample buffer")

    # One capture spanning a bit over two frames is enough for everything,
    # and 50 MS/s resolves a 4.7 us sync pulse into 235 points.
    frame_us = 1e6 / frame_hz
    rate = min(rmax.value, 50e6)
    want_n = int(rate * 2.2 * frame_us / 1e6)
    n = min(bmax.value, want_n)
    if n < want_n:
        print(f"  note: buffer caps the span at {n / rate * 1000:.1f} ms")

    setup_scope(dwf, hdwf)
    capture(dwf, hdwf, rate, min(n, 1 << 16))        # discard: settling
    samples = capture(dwf, hdwf, rate, n)

    if SAVE_CAPTURE:
        with open(SAVE_CAPTURE, "w") as f:
            f.write("t_us,volts\n")
            for i, v in enumerate(samples):
                f.write(f"{i * 1e6 / rate:.6f},{v:.6f}\n")
        print(f"  wrote raw capture to {SAVE_CAPTURE} ({len(samples)} samples)")

    runs = sync_pulses(samples, rate, "scope")
    if not runs:
        return

    widths = [w for _, w in runs]
    eq = [w for w in widths if w < 3.5]
    hs = [w for w in widths if 3.5 <= w < 15]
    broad = [w for w in widths if w >= 15]
    print(f"  measured pulse widths:")
    stats("h-sync", H_SYNC_US, hs)
    stats("equalizing", EQ_PULSE_US, eq)
    stats("broad", V_SYNC_PULSE_US, broad)

    # Line period and line-to-line jitter, from normal h-sync leading edges.
    starts = [s for s, w in runs if 3.5 <= w < 15]
    gaps = [b - a for a, b in zip(starts, starts[1:])
            if 0.9 * LINE_US < b - a < 1.1 * LINE_US]
    line_us = None
    if gaps:
        line_us = sum(gaps) / len(gaps)
        print(f"  line period")
        stats("period", LINE_US, gaps)
        print(f"               -> {1e6 / line_us:.2f} Hz "
              f"({(LINE_US / line_us - 1) * 100:+.4f}% vs 15734.27 Hz)")
        jitter = max(gaps) - min(gaps)
        verdict = "clean" if jitter < 0.05 else "VISIBLE as ragged edges"
        print(f"               peak-to-peak jitter {jitter * 1000:.0f} ns "
              f"({jitter / LINE_US * 100:.3f}% of a line) -- {verdict}")

    # Bar-edge position, line by line. Every picture line is identical in the
    # buffer, so any spread here is the hardware's, not the generator's --
    # this is the number that says whether ragged edges are in the signal or
    # are the TV's own doing.
    dt_us = 1e6 / rate
    ordered = sorted(samples)
    base = ordered[len(ordered) // 100]
    top = ordered[-(len(ordered) // 100) - 1]
    luma = base + 0.67 * (top - base)           # between black and white
    offsets = []
    for s0, s1 in zip(starts, starts[1:]):
        if not 0.9 * LINE_US < s1 - s0 < 1.1 * LINE_US:
            continue
        # Start inside the back porch so the blanking-to-white step at the
        # start of active video is the first crossing found on every line.
        i0 = int((s0 + H_SYNC_US + BACK_PORCH_US * 0.5) / dt_us)
        i1 = int(s1 / dt_us)
        for i in range(i0, min(i1, len(samples) - 1)):
            if samples[i] < luma <= samples[i + 1]:
                offsets.append(i * dt_us - s0)
                break
    if len(offsets) > 2:
        mean = sum(offsets) / len(offsets)
        pp = max(offsets) - min(offsets)
        print(f"  first bar edge {mean:.4f} us after sync, "
              f"peak-to-peak {pp * 1000:.0f} ns over {len(offsets)} lines")
        if pp <= 1.5 * dt_us:
            print("               stable to within one sample: the ragged "
                  "edges are NOT in the signal")
        else:
            print("               MOVING line to line -- the raggedness is "
                  "in the output")
        plot_edges(offsets, mean)

    # Vertical intervals.
    groups = []
    for s in [s for s, w in runs if w >= 15]:
        if not groups or s - groups[-1][-1] > 5 * LINE_US:
            groups.append([s])
        else:
            groups[-1].append(s)
    counts = sorted({len(g) for g in groups})
    print(f"  vertical: {len(groups)} sync groups, broad pulses per group "
          f"{counts} (want [6])")
    per = groups_per_seq or (2 if INTERLACED else 1)
    unit = "field" if INTERLACED else "frame"

    if len(groups) >= 2:
        periods = [b[0] - a[0] for a, b in zip(groups, groups[1:])]
        meas = sum(periods) / len(periods)
        # Broad-pulse groups come once per field, so under interlace the
        # measured spacing is the field period, not the frame period.
        want_hz = frame_hz * per
        want_lines = frame_lines() / per
        print(f"  {unit} period {meas / 1000:.4f} ms -> {1e6 / meas:.4f} Hz "
              f"(want {want_hz:.4f} Hz, "
              f"{(want_hz * meas / 1e6 - 1) * 100:+.4f}%)")
        if line_us:
            lines = meas / line_us
            print(f"  lines per {unit} {lines:.3f} (uploaded {want_lines})")
            if abs(lines - want_lines) > 0.25:
                print("  >> MISMATCH: the device is not playing back the "
                      "frame we uploaded.")
            else:
                print(f"  >> {unit} length matches the uploaded buffer.")


# ----------------------------------------------------------------------------
# Motion (--motion): a bouncing ball over the static colour bars.
#
# Measured with --stream-probe: PlayData sustains 46 MS/s at a 256 Ki chunk
# against the 14.286 MS/s colour rate, so unbounded streaming has ~3.2x of
# headroom. Two things that measurement settled:
#
#  - PlayStatus's `free` is the WRITABLE SPACE, not the fill. Feeding it
#    directly is self-limiting: the buffer fills, then settles at the
#    consumption rate. Deriving an in-flight depth as `cap - free` inverts
#    that and feeds nothing at all -- which is how the first version of this
#    shipped, and is the same mistake as the old `capacity - free` bug.
#    Overrun is avoided by capping the chunk, not by gating on a depth: the
#    4 Mi chunk corrupted 211,200 samples, 256 Ki none.
#  - A round trip costs 1413 us, three per iteration, so chunks below ~59 Ki
#    cannot keep up no matter how fast the host builds data.
#
# Only the ball moves, so the static four-field background is built once and
# memmove'd into a work buffer per sequence; the ball is then painted over it
# as a handful of contiguous runs. That is ~8000 samples of edit per 953,400,
# which is why this needs no C extension.
# ----------------------------------------------------------------------------
BALL_DIA = 0.12                    # diameter, as a fraction of picture height
BALL_SPEED_X = 0.42                # picture widths per second
BALL_SPEED_Y = 0.31                # picture heights per second
BALL_IRE = 100.0                   # luma-only ball; no chroma, so no phase
BALL_EDGE_N = 3                    # samples of soft edge, each side
# Start part-way along both ramps, so t=0 is mid-picture rather than hard in
# the top-left corner. A ball parked at (r, r) is indistinguishable from a
# ball that is not advancing at all, which cost a hardware round trip once.
BALL_PHASE_X = 0.37
BALL_PHASE_Y = 0.61
PICTURE_ASPECT = 4.0 / 3.0
FEED_CHUNK = 1 << 18               # 256 Ki: 46 MS/s measured, zero corruption
# How far ahead of the playhead to keep the device. The feed is throttled
# against the wall clock, not against `free`: the consumption rate is exactly
# the sample rate we set, so cumulative fed <= elapsed * rate + this is a hard
# bound on in-flight depth. Feeding `free` instead ran 3.08x too fast and put
# 760 M samples nowhere, with lost and corrupt both still reporting zero.
FEED_PRELOAD_S = 0.5
# --stream-rate-check: pay 1792 us per poll for a trustworthy `free` so the
# device's real consumption rate can be computed. Underruns while it runs.
STREAM_RATE_CHECK = False
# --stream-capture <s>: seconds into a stream to take an interleaved scope
# capture of our own output, then profile it. Acquisition is in hardware, so
# only the final read stalls the feed -- after the samples are already taken.
STREAM_CAPTURE_AT = None
# Which documented streaming recipe to use. --feed-matrix proved both put data
# on the output, but it streamed DC, which cannot reveal a wrong sample rate.
# "pattern" is AnalogOut_PlayTest.py (funcPlayPattern, run/repeat default) and
# is what continuous streaming is meant to use; "play" is AnalogOut_Play.py
# (funcPlay, RepeatSet(1), a finite run) and is the function --deep uses, which
# plays our content perfectly. If the picture rolls on one and not the other,
# the function is the variable.
STREAM_FUNC = "pattern"
STREAM_RUN_S = 3600.0              # "play" needs a finite run length
# --stream-static: stream the static bars with NO ball, so the content is
# identical to what --deep plays perfectly. That reduces the difference between
# "perfect" and "rolling" to delivery alone -- chunked PlayData in config 5
# versus one DataSet in config 3 -- with content held constant.
STREAM_STATIC = False
# --stream-align G: quantise every PlayData to a multiple of G samples. We
# currently send a continuously varying count, and --feed-matrix could not have
# noticed per-chunk padding or alignment because it streamed DC. 908 is one
# line; AnalogIn reports a buffer granularity of 16.
STREAM_ALIGN = 1
# Pacing against an assumed rate is OPEN LOOP and cannot hold: the device runs
# 0.05% slow, so the fill estimate falls ~0.54 ms behind every second, and
# after ~4 s we write into a FIFO we believe is empty and is actually full.
# The device then drops samples silently -- level corruption, with lost and
# corrupt reporting nothing. Two things close the loop:
#   GUARD     headroom kept free, >= the worst drift between resyncs, so an
#             optimistic estimate still cannot overfill.
#   RESYNC_S  how often to pay 1792 us for a TRUE `free`, then refill by
#             exactly that much and reset the estimate.
# Both default OFF. They were added on a theory that the feed was overfeeding
# into a full FIFO, and the measurement refuted it: with GUARD 16384 and a 1 s
# resync the drift came back at -200 ms (starving, not overfeeding) and lost
# went from 0 to 7.7 billion. Halving the target fill halves the samples per
# PlayData call, and throughput per iteration is call-bound, so the guard
# simply starved the device. Without them the feed is clean -- 14.29 MS/s,
# lost 0, corrupt 0, for 26 s. Kept as knobs, not as defaults.
STREAM_GUARD = 0
STREAM_RESYNC_S = 0.0              # 0 disables the resync entirely
# --stream-capture-s: acquisition window. Must exceed one field (16.7 ms) to
# contain a vertical interval, which is what a rolling picture is about.
STREAM_CAPTURE_S = 0.040
DWF_STATE_NAMES = {DwfStateArmed: "armed", DwfStateDone: "done",
                   DwfStateRunning: "running", DwfStateWait: "wait"}


def feed_amount(free, remaining, chunk=FEED_CHUNK):
    """How many samples to hand the device this iteration.

    `free` is what PlayStatus reports WRITABLE -- not the fill. Feeding it
    directly is self-limiting: the buffer fills, then settles at the
    consumption rate. `remaining` is what is left of the current sequence.

    Pure on purpose: the feed policy is the part that broke (an in-flight
    depth derived as `cap - free` inverted the test and fed nothing), and a
    policy that is a function can be simulated against a model device without
    plugging anything in. See the simulation in the commit message.
    """
    return max(0, min(free, chunk, remaining))


def motion_geometry(spl, rate):
    """Where every active picture line starts, grouped by field.

    Mirrors build_colour_volts' layout exactly: lines sit on even half-lines,
    line hl starting at sample hl * (spl // 2), with each field's first 18
    half-lines overwritten by the pulse train and the next EXTRA_BLANK_LINES
    lines blanked.
    """
    us = 1e6 / rate
    half = spl // 2
    fields, fhl = colour_fields()
    sync_n = round(H_SYNC_US / us)
    bp_n = round(BACK_PORCH_US / us)
    fp_n = round(FRONT_PORCH_US / us)
    active_n = spl - sync_n - bp_n - fp_n
    rows = []
    for f in range(fields):
        base = f * fhl
        first = base + (18 if base % 2 == 0 else 19)
        start = first + 2 * EXTRA_BLANK_LINES
        rows.append([hl * half for hl in range(start, base + fhl, 2)])
    return {"fields": fields, "rows": rows, "active0": sync_n + bp_n,
            "active_n": active_n, "total": fields * fhl * half}


def ball_runs(geom, field, t, white, blank_v, sx=None, sy=None):
    """The contiguous sample runs the ball covers in one field.

    `t` is elapsed seconds, derived from the real sample rate rather than the
    nominal field rate. Returns (start_index, values) pairs. The ball is luma
    only, so its samples are a flat level with a short raised-cosine edge and
    carry no subcarrier term -- which is what lets them be painted over a
    background at any subcarrier phase without a discontinuity.
    """
    rows = geom["rows"][field]
    n = len(rows)
    r = BALL_DIA / 2.0
    # Bounce by reflecting a linear ramp: no state to drift.
    def bounce(v):
        v %= 2.0
        return v if v <= 1.0 else 2.0 - v
    sx = BALL_SPEED_X if sx is None else sx
    sy = BALL_SPEED_Y if sy is None else sy
    cx = r + (1.0 - 2 * r) * bounce(BALL_PHASE_X + sx * t)
    cy = r + (1.0 - 2 * r) * bounce(BALL_PHASE_Y + sy * t)

    out = []
    a0, an = geom["active0"], geom["active_n"]
    for i, s0 in enumerate(rows):
        # Interlace: this field's rows sit half a line below the other's.
        y = (i + 0.5 * (field & 1)) / n
        dy = y - cy
        if abs(dy) >= r:
            continue
        hw = math.sqrt(r * r - dy * dy) / PICTURE_ASPECT   # -> width units
        x0 = int((cx - hw) * an)
        x1 = int(math.ceil((cx + hw) * an))
        x0, x1 = max(0, x0), min(an, x1)
        if x1 <= x0:
            continue
        w = x1 - x0
        e = min(BALL_EDGE_N, w // 2)
        vals = [white] * w
        for k in range(e):                      # raised-cosine both edges
            g = 0.5 - 0.5 * math.cos(math.pi * (k + 0.5) / e)
            vals[k] = blank_v + (white - blank_v) * g
            vals[w - 1 - k] = vals[k]
        out.append((s0 + a0 + x0, vals))
    return out


def build_motion_background(spl, rate):
    """Static four-field background, normalised, plus its output window.

    The ball is pure white and the bars' chroma already peaks above white, so
    the window taken from the background also covers the ball -- asserted
    rather than assumed, since a ball outside the window would clip.
    """
    volts, fsc = build_colour_volts(spl, rate)
    if STD_DC:
        volts = [v - V_BLANK for v in volts]
    base = 0.0 if STD_DC else V_BLANK
    ball_v = base + (BALL_IRE / 100.0) * (V_WHITE - V_BLANK)
    vlo, vhi = min(volts), max(volts)
    if not vlo <= ball_v <= vhi:
        raise RuntimeError(
            f"ball at {BALL_IRE} IRE ({ball_v:.4f} V) falls outside the "
            f"background window {vlo:.4f}..{vhi:.4f} V and would clip")
    offset = (vlo + vhi) / 2.0
    amplitude = (vhi - vlo) / 2.0
    total = len(volts)
    bg = (ctypes.c_double * total)()
    bg[:] = [(v - offset) / amplitude for v in volts]
    norm = lambda v: (v - offset) / amplitude
    print(f"      background {total:,} samples, window {vlo:.4f}..{vhi:.4f} V "
          f"({(vlo - base) / (V_WHITE - V_BLANK) * 100:+.1f}.."
          f"{(vhi - base) / (V_WHITE - V_BLANK) * 100:+.1f} IRE)")
    print(f"      ball {BALL_DIA * 100:.0f}% of picture height at "
          f"{BALL_IRE:.0f} IRE, {BALL_SPEED_X:.2f} w/s x "
          f"{BALL_SPEED_Y:.2f} h/s, luma only")
    return bg, total, amplitude, offset, norm(ball_v), norm(base), fsc


MOTION_SECONDS = 4.0               # default --motion loop length


def motion_loop_plan(rate, seq_samples, seconds, cap):
    """Pick a loop length, and ball speeds that make the motion seamless.

    Every four-field sequence closes on a whole number of subcarrier cycles
    (238,875), so any whole number of them also closes -- the loop point is
    already clean for the signal. The ball is not: it has to complete a whole
    number of bounce cycles over the loop or it jumps at the seam. So the
    requested speeds are rounded to the nearest whole-cycle count.
    """
    seq_t = seq_samples / rate
    n = max(1, round(seconds / seq_t))
    n = min(n, cap // seq_samples)
    loop_t = n * seq_t
    cyc_x = max(1, round(BALL_SPEED_X * loop_t / 2.0))
    cyc_y = max(1, round(BALL_SPEED_Y * loop_t / 2.0))
    # Equal cycle counts put x and y in lockstep and the ball traces a single
    # diagonal line instead of bouncing; a common factor makes the path retrace
    # itself partway through the loop. Nudge x until the pair is coprime and
    # distinct. A short loop therefore forces faster motion -- that is the real
    # trade, and --motion-seconds is how to buy slower motion with memory.
    while cyc_x == cyc_y or math.gcd(cyc_x, cyc_y) != 1:
        cyc_x += 1
    return n, loop_t, 2.0 * cyc_x / loop_t, 2.0 * cyc_y / loop_t, cyc_x, cyc_y


def start_motion(dwf, hdwf, ch, spl, rate, scale):
    """A bouncing ball looped out of deep memory -- no host feed at all.

    Uses the one configuration measured to actually reach the screen: the
    whole buffer in a single DataSet with run 0 and repeat 0, which is how
    --deep plays its static signal. The only difference here is that the
    buffer holds many four-field sequences with the ball at a different
    position in each, so the hardware loop IS the animation.
    """
    bg, total, amplitude, offset, ball_n, blank_n, fsc = \
        build_motion_background(spl, rate)
    geom = motion_geometry(spl, rate)
    if geom["total"] != total:
        raise RuntimeError(f"geometry/background disagree: "
                           f"{geom['total']} vs {total}")
    fields = geom["fields"]
    field_s = total / fields / rate

    pmin, pmax = ctypes.c_int(), ctypes.c_int()
    check(dwf, dwf.FDwfAnalogOutNodePlayInfo(hdwf, ch, AnalogOutNodeCarrier,
                                             ctypes.byref(pmin),
                                             ctypes.byref(pmax)), "PlayInfo")
    n, loop_t, sx, sy, cx_n, cy_n = motion_loop_plan(
        rate, total, MOTION_SECONDS, pmax.value)
    big_n = n * total
    print(f"      loop {n} sequences = {big_n:,} samples, {loop_t:.3f} s "
          f"({big_n / pmax.value * 100:.1f}% of play memory, "
          f"{big_n * 8 / 1e6:.0f} MB on the host)")
    print(f"      ball {cx_n} horizontal and {cy_n} vertical bounce cycles "
          f"per loop -> {sx:.3f} w/s x {sy:.3f} h/s, seamless at the seam")

    t0 = time.time()
    big = (ctypes.c_double * big_n)()
    for k in range(n):
        ctypes.memmove(ctypes.byref(big, k * total * 8), bg, total * 8)
        for f in range(fields):
            t = (k * fields + f) * field_s
            for idx, vals in ball_runs(geom, f, t, ball_n, blank_n, sx, sy):
                a = k * total + idx
                big[a:a + len(vals)] = vals
    print(f"      built {n} sequences in {time.time() - t0:.2f} s")

    check(dwf, dwf.FDwfAnalogOutReset(hdwf, ch), "AnalogOutReset")
    dwf.FDwfDeviceAutoConfigureSet(hdwf, ctypes.c_int(0))
    for fn_, a in ((dwf.FDwfAnalogOutNodeEnableSet, ctypes.c_int(1)),
                   (dwf.FDwfAnalogOutNodeFunctionSet, funcPlay)):
        check(dwf, fn_(hdwf, ch, AnalogOutNodeCarrier, a), "node setup")
    scale_v = LEVEL * (2.0 if SOURCE_SERIES_75R else 1.0)
    check(dwf, dwf.FDwfAnalogOutNodeFrequencySet(
        hdwf, ch, AnalogOutNodeCarrier, ctypes.c_double(rate / scale)),
        "FrequencySet")
    check(dwf, dwf.FDwfAnalogOutNodeAmplitudeSet(
        hdwf, ch, AnalogOutNodeCarrier,
        ctypes.c_double(amplitude * scale_v)), "AmplitudeSet")
    check(dwf, dwf.FDwfAnalogOutNodeOffsetSet(
        hdwf, ch, AnalogOutNodeCarrier,
        ctypes.c_double(offset * scale_v)), "OffsetSet")
    t0 = time.time()
    check(dwf, dwf.FDwfAnalogOutNodeDataSet(
        hdwf, ch, AnalogOutNodeCarrier, big, ctypes.c_int(big_n)), "DataSet")
    print(f"      uploaded {big_n * 8 / 1e6:.0f} MB in "
          f"{time.time() - t0:.1f} s")
    # Exactly what --deep does, and the only thing measured to reach a screen.
    check(dwf, dwf.FDwfAnalogOutRunSet(hdwf, ch, ctypes.c_double(0.0)),
          "RunSet")
    check(dwf, dwf.FDwfAnalogOutRepeatSet(hdwf, ch, ctypes.c_int(0)),
          "RepeatSet")
    check(dwf, dwf.FDwfAnalogOutWaitSet(hdwf, ch, ctypes.c_double(0.0)),
          "WaitSet")
    check(dwf, dwf.FDwfAnalogOutConfigure(hdwf, ch, ctypes.c_int(1)),
          "Configure")
    report_output_window(dwf, hdwf, ch, amplitude * scale_v, offset * scale_v)
    print(f"\nLooping {loop_t:.3f} s of motion from deep memory, entirely in "
          f"hardware. Press Ctrl+C to stop.")
    return big_n


def stream_chunk(fifo, fill, remaining, align=1):
    """Samples to hand over now, given the estimated FIFO fill.

    `fill` may be NEGATIVE when the feed has fallen behind the playhead, and
    clamping it at zero is what keeps the request inside the FIFO. Without the
    clamp, being 0.13 ms behind asks for 34,669 samples of a 32,768-sample
    FIFO and the device rejects the whole call with "Invalid data count
    provided". feed_tune's loop had the clamp; porting it into stream_feed
    dropped it.

    The redundant `fifo` term is a deliberate invariant: whatever the fill
    estimate does, the request can never exceed the FIFO.
    """
    room = fifo - max(0.0, fill)
    n = max(0, int(min(room, fifo, remaining)))
    if align > 1:
        n -= n % align
    return n


def pick_stream_config(dwf, force=None):
    """Configuration that can actually stream: play memory 0, deepest scope.

    Measured by --feed-matrix: PlayData reaches W1 only where
    DECIAnalogOutPlaySize is 0. The configurations advertising 67 M or 134 M
    of play memory accept PlayData at 63-72 MS/s, report zero lost and zero
    corrupted, and play the primed buffer on loop instead -- so pick_deep_config
    is exactly the wrong chooser for a stream.
    """
    ndev, ncfg = ctypes.c_int(), ctypes.c_int()
    check(dwf, dwf.FDwfEnum(ctypes.c_int(0), ctypes.byref(ndev)), "FDwfEnum")
    if ndev.value == 0:
        raise RuntimeError("No Digilent device found. Close WaveForms and replug.")
    check(dwf, dwf.FDwfEnumConfig(ctypes.c_int(0), ctypes.byref(ncfg)),
          "FDwfEnumConfig")
    best, best_scope = -1, -1
    for i in range(ncfg.value):
        play, scope = ctypes.c_int(), ctypes.c_int()
        dwf.FDwfEnumConfigInfo(ctypes.c_int(i), DECIAnalogOutPlaySize,
                               ctypes.byref(play))
        dwf.FDwfEnumConfigInfo(ctypes.c_int(i), DECIAnalogInBufferSize,
                               ctypes.byref(scope))
        print(f"  config {i}: play memory {play.value} samples, "
              f"scope buffer {scope.value}"
              + ("  <- can stream" if play.value == 0 else ""))
        if play.value == 0 and scope.value > best_scope:
            best, best_scope = i, scope.value
    if force is not None:
        print(f"  -> config {force} (forced)")
        return force
    if best < 0:
        raise RuntimeError("No configuration reports play memory 0, so none "
                           "can stream on this unit. Run --feed-matrix.")
    print(f"  -> config {best} (streams; scope buffer {best_scope:,})")
    return best


def check_chain_gain(prof, expect_sync_v):
    """Warn when the captured sync depth does not track what we are driving.

    A passive chain has a fixed gain, so measured sync depth must scale with
    the output level. If it does not, something between W1 and the scope is
    REGENERATING the signal -- a time base corrector does exactly that -- and
    the capture then describes that device's output, not ours. Two runs an
    output level of 1.87x apart both measured 534.8 and 534.7 mV of sync
    depth, which is how this was eventually noticed; it should have been
    flagged the first time.
    """
    if not expect_sync_v:
        return
    got = prof.get("sync_mv")
    if not got:
        return
    want = expect_sync_v * 1000.0
    ratio = got / want
    print(f"  chain gain  driving {want:.1f} mV of sync at W1, measured "
          f"{got:.1f} mV -> {ratio:.3f}x")
    if not 0.2 < ratio < 5.0:
        print("  WARNING: implausible chain gain. Check the probe point.")
    print("  NOTE: if this ratio changes when --level changes, the chain is "
          "not\n        passive -- something is regenerating sync, and this "
          "capture\n        describes ITS output rather than ours. Tap W1 "
          "directly.")


def describe_capture(samples, dt_us):
    """Say what a capture actually contains when no sync was found.

    profile_capture reports nan for everything when it finds no h-sync, which
    says nothing about why. A flat trace, a noisy one and a real waveform at
    the wrong level all produce the same nans, and they need different fixes.
    """
    srt = sorted(samples)
    n = len(srt)
    q = lambda f: srt[min(n - 1, int(f * n))]
    lo, hi = q(0.005), q(0.995)
    print(f"\n=== capture has no recognisable h-sync ===")
    print(f"  {n:,} samples, {n * dt_us:.2f} us at "
          f"{1.0 / dt_us:.1f} MS/s")
    print(f"  min {min(samples) * 1000:+8.1f} mV   max "
          f"{max(samples) * 1000:+8.1f} mV   p-p "
          f"{(max(samples) - min(samples)) * 1000:8.1f} mV")
    print(f"  percentiles  0.5% {lo * 1000:+8.1f}   50% {q(0.5) * 1000:+8.1f}"
          f"   99.5% {hi * 1000:+8.1f} mV")
    span = hi - lo
    if span < 0.050:
        print(f"  FLAT: 99% of samples inside {span * 1000:.1f} mV. Nothing is "
              f"driving the input, or the capture missed the signal.")
        return
    # How long does it stay below the sync threshold? Real sync is ~4.7 us.
    runs = runs_below(samples, dt_us, lo + 0.13 * span)
    if runs:
        w = sorted(x for _, x in runs)
        print(f"  {len(runs):,} runs below the sync threshold, widths "
              f"{w[0]:.3f}..{w[-1]:.3f} us, median {w[len(w) // 2]:.3f} "
              f"(h-sync should be ~4.7)")
        if w[-1] < 0.5:
            print(f"  NOISE: the longest excursion is {w[-1] * 1000:.0f} ns. "
                  f"This is a threshold crossed by noise, not sync pulses. "
                  f"The usual cause is an unsettled input stage -- discard one "
                  f"capture after setup_scope.")
    print(f"  first 16 samples (mV): "
          + " ".join(f"{v * 1000:+.0f}" for v in samples[:16]))


def stream_line_jitter(samples, dt_us, rate, spl):
    """Line-to-line sync spacing from a capture -- the "sync slightly off" test.

    Reports the distribution of intervals between successive horizontal sync
    edges. A clean generator holds this to a few tens of nanoseconds. Gaps that
    are an exact multiple of a chunk, or of a line, say something different
    from gaps that are random.
    """
    srt = sorted(samples)
    lo, hi = srt[len(srt) // 200], srt[-(len(srt) // 200) - 1]
    runs = runs_below(samples, dt_us, lo + 0.13 * (hi - lo))
    edges = [t for t, w in runs if 3.5 <= w <= 15]        # h-sync only
    if len(edges) < 20:
        print("  jitter: too few h-sync edges")
        return
    iv = [(edges[i + 1] - edges[i]) for i in range(len(edges) - 1)]
    iv = [x for x in iv if x < 200.0]                     # drop VBI gaps
    if not iv:
        print("  jitter: no usable line intervals")
        return
    srt_iv = sorted(iv)
    med = srt_iv[len(srt_iv) // 2]
    nominal = spl / rate * 1e6 if spl else med
    print(f"  line interval  median {med:.4f} us (built {nominal:.4f}), "
          f"spread {srt_iv[0]:.4f}..{srt_iv[-1]:.4f} us over {len(iv)} lines")
    print(f"                 p-p jitter {(srt_iv[-1] - srt_iv[0]) * 1000:.0f} ns, "
          f"median error {(med - nominal) * 1000:+.0f} ns")
    outliers = [(i, x) for i, x in enumerate(iv) if abs(x - med) > 0.100]
    if not outliers:
        print("                 no line more than 100 ns off the median")
        return
    print(f"                 {len(outliers)} of {len(iv)} lines more than "
          f"100 ns off:")
    for i, x in outliers[:8]:
        print(f"                   line {i:4} {x:9.4f} us "
              f"({(x - med) * 1000:+.0f} ns)")
    if len(outliers) > 1:
        gaps = [outliers[k + 1][0] - outliers[k][0]
                for k in range(len(outliers) - 1)]
        gs = sorted(gaps)
        print(f"                 spacing between outliers: median "
              f"{gs[len(gs) // 2]} lines, range {gs[0]}..{gs[-1]}")


def stream_feed(dwf, hdwf, ch, fill_fn, rate, fifo, poll=0.1, report=5.0,
                rate_check=False, spl_hint=None, capture_at=None,
                capture_path=None, capture_s=0.040, scope_rate=50e6,
                expect_sync_v=None):
    """Stream an arbitrary signal, generated on demand, paced against a clock.

    `fill_fn(dst, abs_pos, n)` writes n samples of the signal starting at
    absolute sample index abs_pos into `dst`. That is the whole interface: the
    content can be anything, is never bounded by device memory, and nothing is
    precomputed.

    The structure is the one --feed-tune measured clean at 14.295 MS/s, and it
    is shaped entirely by two timings in a streaming configuration:

        FDwfAnalogOutStatus            1792 us
        FDwfAnalogOutNodePlayStatus       2 us

    The FIFO is only about 2.29 ms deep at the colour rate, so a single
    AnalogOutStatus is 78% of the whole budget and underruns the device. It is
    therefore NEVER called here unless rate_check asks for it. PlayStatus is
    cheap, but returns STALE values without it -- so `free` is used only for
    the lost and corrupted counters, never for sizing. Sizing comes from the
    clock: the device consumes at exactly the rate we set, so
    fill = fed - elapsed * rate.

    rate_check trades the signal for a measurement: it pays for
    AnalogOutStatus so `free` is trustworthy, and then
    (fed - (fifo - free)) / elapsed is the device's real consumption rate. It
    WILL underrun while running, so it is a diagnostic, not a mode to play in.
    """
    # A garbage collection pass longer than the FIFO's 2.29 ms starves the
    # device, and the counters do not report it. The loop allocates nothing
    # per iteration, so there is nothing for the collector to find anyway.
    gc_was_on = gc.isenabled()
    gc.disable()
    staging = (ctypes.c_double * fifo)()
    abs_pos = fifo              # the prime already delivered the first `fifo`
    fed = fifo
    free, lost, corrupt = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
    sts = ctypes.c_ubyte()
    t_poll = 0.0
    t_resync = STREAM_RESYNC_S if STREAM_RESYNC_S > 0 else float("inf")
    resyncs = 0
    drift_sum = 0.0
    t_rep = report
    t0 = time.time()
    worst_fill = float("inf")
    actual = 0.0
    t_lost = t_corr = 0
    # Interleaved capture: acquisition runs in device hardware, so arming it
    # and polling costs almost nothing. Only the final read stalls the feed,
    # and by then the samples are already taken -- so the capture reflects a
    # healthy stream rather than one disturbed by its own measurement.
    cap_n = int(capture_s * scope_rate) if capture_at is not None else 0
    cap_state = "idle"
    in_sts = ctypes.c_ubyte()
    # Its own timer. Sharing t_poll with the PlayStatus poll meant that block
    # advanced the deadline first and this one never fired at all.
    t_cap = 0.0
    cap_armed_at = 0.0
    try:
        while True:
            now = time.time() - t0
            fill = fed - now * rate
            if now > 0.25:
                worst_fill = min(worst_fill, fill)

            # Closed-loop resync. Read the TRUE free space, refill by exactly
            # that much -- the only write size we can be certain of -- and
            # reset the estimate to "full". Costs 1792 us, during which a full
            # FIFO drains to about 0.5 ms, so it must not run often.
            if (STREAM_RESYNC_S > 0 and cap_state != "armed"
                    and now >= t_resync):
                check(dwf, dwf.FDwfAnalogOutStatus(hdwf, ch,
                                                   ctypes.byref(sts)),
                      "AnalogOutStatus")
                check(dwf, dwf.FDwfAnalogOutNodePlayStatus(
                    hdwf, ch, AnalogOutNodeCarrier, ctypes.byref(free),
                    ctypes.byref(lost), ctypes.byref(corrupt)), "PlayStatus")
                t_lost += lost.value
                t_corr += corrupt.value
                room = min(max(0, free.value), fifo)
                if STREAM_ALIGN > 1:
                    room -= room % STREAM_ALIGN
                if room > 0:
                    fill_fn(staging, abs_pos, room)
                    check(dwf, dwf.FDwfAnalogOutNodePlayData(
                        hdwf, ch, AnalogOutNodeCarrier, staging,
                        ctypes.c_int(room)), "PlayData(resync)")
                    abs_pos += room
                    fed += room
                now = time.time() - t0
                drift_sum += (fed - now * rate) - fifo
                fed = now * rate + fifo          # estimate := FIFO full
                resyncs += 1
                t_resync = now + STREAM_RESYNC_S

            target = max(1, fifo - STREAM_GUARD)
            n = stream_chunk(target, fill, fifo, STREAM_ALIGN)
            if n > 0:
                fill_fn(staging, abs_pos, n)
                if dwf.FDwfAnalogOutNodePlayData(
                        hdwf, ch, AnalogOutNodeCarrier, staging,
                        ctypes.c_int(n)) != 1:
                    raise RuntimeError(f"PlayData refused {n} samples at "
                                       f"{now:.2f} s: {last_error(dwf)}")
                fed += n
                abs_pos += n

            if now >= t_poll:
                if rate_check:
                    dwf.FDwfAnalogOutStatus(hdwf, ch, ctypes.byref(sts))
                dwf.FDwfAnalogOutNodePlayStatus(hdwf, ch, AnalogOutNodeCarrier,
                                                ctypes.byref(free),
                                                ctypes.byref(lost),
                                                ctypes.byref(corrupt))
                t_lost += lost.value
                t_corr += corrupt.value
                if rate_check:
                    now = time.time() - t0
                    if now > 0.5:
                        in_fifo = min(max(0, fifo - free.value), fifo)
                        actual = (fed - in_fifo) / now
                t_poll = now + poll

            if capture_at is not None and cap_state != "done":
                if cap_state == "idle" and now >= capture_at:
                    # Every one of these was unchecked, and a refused
                    # BufferSizeSet leaves StatusData filling nothing -- which
                    # reads as a FLAT capture and looks like a dead cable.
                    bmin, bmax = ctypes.c_int(), ctypes.c_int()
                    check(dwf, dwf.FDwfAnalogInBufferSizeInfo(
                        hdwf, ctypes.byref(bmin), ctypes.byref(bmax)),
                        "InBufferSizeInfo")
                    if cap_n > bmax.value:
                        print(f"  capture clamped {cap_n:,} -> {bmax.value:,} "
                              f"samples (device maximum)")
                        cap_n = bmax.value
                    check(dwf, dwf.FDwfAnalogInFrequencySet(
                        hdwf, ctypes.c_double(scope_rate)), "InFrequencySet")
                    check(dwf, dwf.FDwfAnalogInBufferSizeSet(
                        hdwf, ctypes.c_int(cap_n)), "InBufferSizeSet")
                    # Fill the FIFO completely first: once armed we stop
                    # feeding, so whatever is in there is all the signal the
                    # capture will see.
                    n0 = stream_chunk(fifo, fed - now * rate, fifo,
                                      STREAM_ALIGN)
                    if n0 > 0:
                        fill_fn(staging, abs_pos, n0)
                        check(dwf, dwf.FDwfAnalogOutNodePlayData(
                            hdwf, ch, AnalogOutNodeCarrier, staging,
                            ctypes.c_int(n0)), "PlayData(pre-capture)")
                        fed += n0
                        abs_pos += n0
                    check(dwf, dwf.FDwfAnalogInConfigure(
                        hdwf, ctypes.c_int(1), ctypes.c_int(1)),
                        "InConfigure")
                    gf, gn = ctypes.c_double(), ctypes.c_int()
                    check(dwf, dwf.FDwfAnalogInFrequencyGet(
                        hdwf, ctypes.byref(gf)), "InFrequencyGet")
                    check(dwf, dwf.FDwfAnalogInBufferSizeGet(
                        hdwf, ctypes.byref(gn)), "InBufferSizeGet")
                    print(f"  arming scope at {now:.1f} s: asked {cap_n:,} "
                          f"samples at {scope_rate / 1e6:.0f} MS/s, device "
                          f"gave {gn.value:,} at {gf.value / 1e6:.3f} MS/s "
                          f"(range {bmin.value:,}..{bmax.value:,})")
                    if gn.value != cap_n:
                        print(f"  NOTE: buffer is {gn.value:,}, not "
                              f"{cap_n:,}; reading the smaller count")
                        cap_n = gn.value
                    cap_state = "armed"
                    cap_armed_at = now
                    t_cap = now + 0.05
                elif cap_state == "armed":
                    # Keep feeding during acquisition. The pause here was
                    # added on a concurrency theory that turned out to be a
                    # disconnected CH1; with the jumper in place a running
                    # feed captures fine, and the window has to span more
                    # than one field (16.7 ms) to contain a vertical interval.
                    t_cap = now + 0.002
                    check(dwf, dwf.FDwfAnalogInStatus(
                        hdwf, ctypes.c_int(0), ctypes.byref(in_sts)),
                        "InStatus(poll)")
                    if (in_sts.value != DwfStateDone
                            and now - cap_armed_at > 10.0):
                        st = DWF_STATE_NAMES.get(in_sts.value,
                                                 in_sts.value)
                        print(f"  capture timed out after 10 s in state "
                              f"{st} -- no trigger? nothing written.")
                        cap_state = "done"
                    elif in_sts.value == DwfStateDone:
                        print(f"  acquired at {now:.1f} s "
                              f"(lost {t_lost:,}, corrupt {t_corr:,} so far)")
                        check(dwf, dwf.FDwfAnalogInStatus(
                            hdwf, ctypes.c_int(1), ctypes.byref(in_sts)),
                            "InStatus(read)")
                        buf = (ctypes.c_double * cap_n)()
                        check(dwf, dwf.FDwfAnalogInStatusData(
                            hdwf, ctypes.c_int(0), buf, ctypes.c_int(cap_n)),
                            "InStatusData")
                        samples = list(buf)
                        cap_state = "done"
                        if capture_path:
                            with open(capture_path, "w") as f:
                                f.write("t,v\n")
                                for i, v in enumerate(samples):
                                    f.write(f"{i / scope_rate:.9f},{v:.6f}\n")
                            print(f"  wrote {capture_path}")
                        # dt is in MICROSECONDS here (compare_captures
                        # defaults to 0.020 for 50 MS/s), not seconds.
                        dt_us = 1e6 / scope_rate
                        prof = profile_capture(samples, dt_us)
                        if prof is None or not prof.get("counts", (0,))[0]:
                            describe_capture(samples, dt_us)
                        else:
                            show_profile(prof, "streamed output")
                            check_chain_gain(prof, expect_sync_v)
                        stream_line_jitter(samples, dt_us, rate, spl_hint)
                        # Reading a large buffer stalls the loop, so the
                        # clock-paced estimate is stale afterwards. Re-baseline
                        # it to "FIFO full" rather than chasing the deficit.
                        t0 = time.time() - (fed - fifo) / rate
                        print("  pacing clock re-baselined after readback")

            if now >= t_rep:
                wf = (0.0 if worst_fill == float("inf")
                      else worst_fill / rate * 1000)
                dr = (drift_sum / resyncs / rate * 1000) if resyncs else 0.0
                extra = (f"  device {actual / 1e6:6.3f} MS/s "
                         f"({(actual / rate - 1) * 100:+.3f}%)"
                         if actual else "")
                print(f"  {now:6.1f} s  fed {fed / now / 1e6:6.3f} MS/s "
                      f"(need {rate / 1e6:.3f}){extra}  fill "
                      f"{fill / rate * 1000:+5.2f} ms  min {wf:+5.2f} ms  "
                      f"resync {resyncs} (drift {dr:+.2f} ms)  "
                      f"lost {t_lost:,}  corrupt {t_corr:,}")
                t_rep = now + report
    except KeyboardInterrupt:
        el = time.time() - t0
        wf = 0.0 if worst_fill == float("inf") else worst_fill / rate * 1000
        print(f"\nStopped after {el:.1f} s: {fed / 1e6:.1f} M samples at "
              f"{fed / el / 1e6:.3f} MS/s, minimum fill {wf:.2f} ms, "
              f"lost {t_lost:,}, corrupt {t_corr:,}")
        if gc_was_on:
            gc.enable()
        if actual and spl_hint:
            line_hz = actual / spl_hint
            print(f"  device consumed {actual / 1e6:.4f} MS/s vs "
                  f"{rate / 1e6:.4f} requested "
                  f"({(actual / rate - 1) * 100:+.3f}%)")
            print(f"  -> line {line_hz:.2f} Hz (NTSC 15734.27), field "
                  f"{line_hz / 262.5:.4f} Hz (NTSC 59.9401)")


def make_background_filler(bg, total):
    """A fill_fn that streams the static background, with no ball painted.

    Exists to hold content constant: this is byte-for-byte what --deep loads
    into deep memory and plays perfectly, so if streaming it still rolls, the
    cause is delivery and not anything we generate.
    """
    def fill(dst, abs_pos, n):
        done = 0
        while done < n:
            off = (abs_pos + done) % total
            m = min(n - done, total - off)
            ctypes.memmove(ctypes.byref(dst, done * 8),
                           ctypes.byref(bg, off * 8), m * 8)
            done += m
    return fill


def make_motion_filler(bg, total, geom, ball_n, blank_n, rate):
    """A fill_fn that paints the bouncing ball over the background on demand.

    Generating per chunk instead of per sequence is what keeps the feed loop
    inside the FIFO budget: rebuilding a whole 953,400-sample sequence on each
    wrap cost about 0.8 ms, 35% of a 2.29 ms FIFO, and underran the device.
    Per chunk it is a 262 KB memmove plus a handful of short runs.

    It is also the general shape for streaming anything: given an absolute
    sample index and a count, produce those samples.
    """
    fields = geom["fields"]
    field_s = total / fields / rate
    cache = {}

    def runs_for(seq):
        """Ball runs for one sequence, as (absolute index, values)."""
        got = cache.get(seq)
        if got is None:
            got = []
            for f in range(fields):
                t = (seq * fields + f) * field_s
                for idx, vals in ball_runs(geom, f, t, ball_n, blank_n):
                    got.append((seq * total + idx, vals))
            cache[seq] = got
            for k in list(cache):
                if k < seq - 1:         # keep only the current and previous
                    del cache[k]
        return got

    def fill(dst, abs_pos, n):
        # Background first, wrapping at the sequence boundary.
        done = 0
        while done < n:
            off = (abs_pos + done) % total
            m = min(n - done, total - off)
            ctypes.memmove(ctypes.byref(dst, done * 8),
                           ctypes.byref(bg, off * 8), m * 8)
            done += m
        # Then any ball runs that overlap this window.
        end = abs_pos + n
        for seq in range(abs_pos // total, (end - 1) // total + 1):
            for a, vals in runs_for(seq):
                b = a + len(vals)
                if b <= abs_pos or a >= end:
                    continue
                lo, hi = max(a, abs_pos), min(b, end)
                dst[lo - abs_pos:hi - abs_pos] = vals[lo - a:hi - a]
    return fill


def start_motion_stream(dwf, hdwf, ch, spl, rate, scale):
    """Stream the bouncing ball indefinitely, generated chunk by chunk.

    Nothing is precomputed and nothing repeats: the ball's position is a
    function of the absolute sample index, so this runs until stopped.
    """
    bg, total, amplitude, offset, ball_n, blank_n, fsc = \
        build_motion_background(spl, rate)
    geom = motion_geometry(spl, rate)
    if STREAM_STATIC:
        fill_fn = make_background_filler(bg, total)
        print("      --stream-static: no ball. Content is byte-identical to "
              "what --deep plays,\n"
              "      so delivery is the only remaining difference.")
    else:
        fill_fn = make_motion_filler(bg, total, geom, ball_n, blank_n, rate)

    if STREAM_CAPTURE_AT is not None:
        # Before the AWG, matching the order feed_one_config used -- which did
        # read CH1 correctly in this configuration. setup_scope documents that
        # the first acquisition after a range change is wrong, so discard one;
        # doing it here costs the feed nothing because nothing is streaming yet.
        setup_scope(dwf, hdwf)
        capture(dwf, hdwf, 25e6, 1 << 16)              # discard: settling

    check(dwf, dwf.FDwfAnalogOutReset(hdwf, ch), "AnalogOutReset")
    dwf.FDwfDeviceAutoConfigureSet(hdwf, ctypes.c_int(0))
    check(dwf, dwf.FDwfAnalogOutNodeEnableSet(hdwf, ch, AnalogOutNodeCarrier,
                                              ctypes.c_int(1)), "NodeEnableSet")
    if STREAM_FUNC == "play":
        # AnalogOut_Play.py: funcPlay, repeat 1, a finite run. Unbounded is
        # approximated with a long run length.
        check(dwf, dwf.FDwfAnalogOutNodeFunctionSet(
            hdwf, ch, AnalogOutNodeCarrier, funcPlay), "NodeFunctionSet")
        check(dwf, dwf.FDwfAnalogOutRepeatSet(hdwf, ch, ctypes.c_int(1)),
              "RepeatSet")
        check(dwf, dwf.FDwfAnalogOutRunSet(hdwf, ch,
                                           ctypes.c_double(STREAM_RUN_S)),
              "RunSet")
    else:
        # AnalogOut_PlayTest.py: funcPlayPattern, run and repeat at defaults.
        check(dwf, dwf.FDwfAnalogOutNodeFunctionSet(
            hdwf, ch, AnalogOutNodeCarrier, funcPlayPattern),
            "NodeFunctionSet")
    print(f"      streaming recipe {STREAM_FUNC!r}: "
          + ("funcPlay, RepeatSet(1), RunSet("
             f"{STREAM_RUN_S:.0f} s) -- AnalogOut_Play.py"
             if STREAM_FUNC == "play"
             else "funcPlayPattern, run/repeat default "
                  "-- AnalogOut_PlayTest.py"))
    check(dwf, dwf.FDwfAnalogOutNodeFrequencySet(
        hdwf, ch, AnalogOutNodeCarrier, ctypes.c_double(rate / scale)),
        "FrequencySet")
    scale_v = LEVEL * (2.0 if SOURCE_SERIES_75R else 1.0)
    check(dwf, dwf.FDwfAnalogOutNodeAmplitudeSet(
        hdwf, ch, AnalogOutNodeCarrier,
        ctypes.c_double(amplitude * scale_v)), "AmplitudeSet")
    check(dwf, dwf.FDwfAnalogOutNodeOffsetSet(
        hdwf, ch, AnalogOutNodeCarrier,
        ctypes.c_double(offset * scale_v)), "OffsetSet")
    cbuf = ctypes.c_int(0)
    check(dwf, dwf.FDwfAnalogOutNodeDataInfo(hdwf, ch, AnalogOutNodeCarrier, 0,
                                             ctypes.byref(cbuf)), "DataInfo")
    fifo = max(1, cbuf.value)
    prime = (ctypes.c_double * fifo)()
    fill_fn(prime, 0, fifo)
    check(dwf, dwf.FDwfAnalogOutNodeDataSet(
        hdwf, ch, AnalogOutNodeCarrier, prime, ctypes.c_int(fifo)), "DataSet")
    check(dwf, dwf.FDwfAnalogOutConfigure(hdwf, ch, ctypes.c_int(1)),
          "Configure")
    report_output_window(dwf, hdwf, ch, amplitude * scale_v, offset * scale_v)

    t = time.time()
    probe = (ctypes.c_double * fifo)()
    for _ in range(20):
        fill_fn(probe, 12345, fifo)
    gen = (time.time() - t) / 20
    print(f"      FIFO {fifo:,} samples ({fifo / rate * 1000:.2f} ms budget); "
          f"generating a chunk costs {gen * 1000:.3f} ms "
          f"({gen / (fifo / rate) * 100:.1f}% of it)")
    if STREAM_RATE_CHECK:
        print("      --stream-rate-check: paying 1792 us per poll for a "
              "trustworthy `free`.\n"
              "      This WILL underrun and break the picture. Diagnostic only.")
    print("\nStreaming unbounded motion. Press Ctrl+C to stop.")
    stream_feed(dwf, hdwf, ch, fill_fn, rate, fifo,
                rate_check=STREAM_RATE_CHECK, spl_hint=spl,
                capture_at=STREAM_CAPTURE_AT, capture_path=SAVE_CAPTURE,
                capture_s=STREAM_CAPTURE_S,
                expect_sync_v=(V_BLANK - V_SYNC) * scale_v)
    return total


# Levels for the feed matrix. The primed and streamed data must be DIFFERENT
# so the capture can say which one is actually playing. An API success code
# cannot: config 3 returns success for every PlayData call and leaves W1 dead.
FEED_PRIME_V = -0.5
FEED_STREAM_V = +0.5


# The two streaming recipes the SDK actually ships, quoted so the diff from
# them stays visible. Our code matched NEITHER: it used funcPlay with
# RunSet(0.0) and RepeatSet(0), and fed a chunk sized by a host-side budget
# rather than by cFree. Writing more than cFree is an error, which is what
# "PlayData refused" was.
#
#   AnalogOut_Play.py      funcPlay(31), RepeatSet(1), RunSet(duration)
#   AnalogOut_PlayTest.py  funcPlayPattern(29), run/repeat left at defaults
#
# Both prime with the DataInfo buffer size, then feed EXACTLY cFree each
# iteration and `continue` when cFree is 0.
FEED_RECIPES = (
    ("play+rep1+run", "AnalogOut_Play.py: funcPlay, RepeatSet(1), RunSet(s)"),
    ("pattern+free", "AnalogOut_PlayTest.py: funcPlayPattern, defaults"),
)


def feed_one_config(dwf, cfg, recipe, target, secs=1.2):
    """One configuration, one recipe. Returns (verdict, volts, fed, elapsed).

    Primes DC at FEED_PRIME_V and streams DC at FEED_STREAM_V, so the capture
    says which is playing -- a success code cannot.
    """
    hdwf = ctypes.c_int()
    if dwf.FDwfDeviceConfigOpen(ctypes.c_int(0), ctypes.c_int(cfg),
                                ctypes.byref(hdwf)) != 1 or hdwf.value == 0:
        return None
    try:
        ch = ctypes.c_int(0)
        setup_scope(dwf, hdwf)
        dwf.FDwfAnalogOutReset(hdwf, ch)
        dwf.FDwfDeviceAutoConfigureSet(hdwf, ctypes.c_int(0))
        dwf.FDwfAnalogOutNodeEnableSet(hdwf, ch, AnalogOutNodeCarrier,
                                       ctypes.c_int(1))
        fn = funcPlay if recipe == "play+rep1+run" else funcPlayPattern
        dwf.FDwfAnalogOutNodeFunctionSet(hdwf, ch, AnalogOutNodeCarrier, fn)
        dwf.FDwfAnalogOutNodeFrequencySet(hdwf, ch, AnalogOutNodeCarrier,
                                          ctypes.c_double(target))
        dwf.FDwfAnalogOutNodeAmplitudeSet(hdwf, ch, AnalogOutNodeCarrier,
                                          ctypes.c_double(1.0))
        if recipe == "play+rep1+run":
            dwf.FDwfAnalogOutRepeatSet(hdwf, ch, ctypes.c_int(1))
            dwf.FDwfAnalogOutRunSet(hdwf, ch, ctypes.c_double(secs + 2.0))
        # else: run and repeat left at their defaults, as PlayTest does

        cbuf = ctypes.c_int(0)
        dwf.FDwfAnalogOutNodeDataInfo(hdwf, ch, AnalogOutNodeCarrier, 0,
                                      ctypes.byref(cbuf))
        n_pre = max(1, cbuf.value)
        primed = (ctypes.c_double * n_pre)()
        for i in range(n_pre):
            primed[i] = FEED_PRIME_V
        if dwf.FDwfAnalogOutNodeDataSet(hdwf, ch, AnalogOutNodeCarrier,
                                        primed, ctypes.c_int(n_pre)) != 1:
            return ("DataSet refused", None, 0, 0.0, n_pre, 0)
        if dwf.FDwfAnalogOutConfigure(hdwf, ch, ctypes.c_int(1)) != 1:
            return ("Configure refused", None, 0, 0.0, n_pre, 0)

        SRC = 1 << 20
        src = (ctypes.c_double * SRC)()
        for i in range(SRC):
            src[i] = FEED_STREAM_V
        sts = ctypes.c_ubyte()
        free, lost, corrupt = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        fed = 0
        t_lost = t_corr = 0
        free_max = 0
        note = ""
        t0 = time.time()
        while time.time() - t0 < secs:
            if dwf.FDwfAnalogOutStatus(hdwf, ch, ctypes.byref(sts)) != 1:
                note = "Status error"
                break
            if sts.value != DwfStateRunning:
                note = (f"left running ({DWF_STATE_NAMES.get(sts.value, sts.value)})")
                break
            dwf.FDwfAnalogOutNodePlayStatus(hdwf, ch, AnalogOutNodeCarrier,
                                            ctypes.byref(free),
                                            ctypes.byref(lost),
                                            ctypes.byref(corrupt))
            t_lost += lost.value
            t_corr += corrupt.value
            free_max = max(free_max, free.value)
            if free.value == 0:
                continue
            # Exactly cFree, clamped to our source buffer -- as both examples
            # do. Feeding more than cFree is an error.
            n = min(free.value, SRC)
            if dwf.FDwfAnalogOutNodePlayData(hdwf, ch, AnalogOutNodeCarrier,
                                             src, ctypes.c_int(n)) != 1:
                note = "PlayData refused"
                break
            fed += n
        el = time.time() - t0

        s = capture(dwf, hdwf, 1e6, 1 << 13)
        v = sorted(s)[len(s) // 2]
        if abs(v - FEED_STREAM_V) < 0.15:
            verdict = "STREAM on output"
        elif abs(v - FEED_PRIME_V) < 0.15:
            verdict = "looping prime"
        elif abs(v) < 0.1:
            verdict = "W1 dead"
        else:
            verdict = f"odd {v:+.2f} V"
        if note:
            verdict += f" [{note}]"
        return (verdict, v, fed, el, n_pre, free_max, t_lost, t_corr)
    finally:
        dwf.FDwfDeviceClose(hdwf)


def feed_matrix(dwf, target=100e6 / 7):
    """Configuration x recipe: which combination puts a stream on W1?

    Needs W1 -> CH1. The question this answers is the one that blocks feeding
    arbitrary data: does PlayData reach the output, under the call sequences
    the SDK's own examples use, and at what rate.
    """
    ndev = ctypes.c_int()
    check(dwf, dwf.FDwfEnum(ctypes.c_int(0), ctypes.byref(ndev)), "FDwfEnum")
    if ndev.value == 0:
        raise RuntimeError("No Digilent device found. Close WaveForms and "
                           "replug.")
    name, sn = ctypes.create_string_buffer(32), ctypes.create_string_buffer(32)
    dwf.FDwfEnumDeviceName(ctypes.c_int(0), name)
    dwf.FDwfEnumSN(ctypes.c_int(0), sn)
    print(f"  device 0: {name.value.decode()} {sn.value.decode()}")
    ncfg = ctypes.c_int()
    check(dwf, dwf.FDwfEnumConfig(ctypes.c_int(0), ctypes.byref(ncfg)),
          "FDwfEnumConfig")

    print(f"\nFeed matrix: prime DC {FEED_PRIME_V:+.2f} V, stream DC "
          f"{FEED_STREAM_V:+.2f} V at {target / 1e6:.3f} MS/s. "
          f"Needs W1 -> CH1.")
    for key, desc in FEED_RECIPES:
        print(f"  {key:16} {desc}")
    good = []
    for key, _ in FEED_RECIPES:
        print(f"\n  recipe {key}")
        print(f"    {'cfg':>3} {'play mem':>12} {'prefill':>8} {'free max':>10} "
              f"{'MS/s':>8} {'CH1':>7}  verdict")
        for i in range(ncfg.value):
            play = ctypes.c_int()
            dwf.FDwfEnumConfigInfo(ctypes.c_int(i), DECIAnalogOutPlaySize,
                                   ctypes.byref(play))
            r = feed_one_config(dwf, i, key, target)
            if r is None:
                print(f"    {i:>3} {play.value:>12,} "
                      f"{'':>8} {'':>10} {'':>8} {'':>7}  could not open")
                continue
            verdict, v, fed, el, n_pre, fmax = r[0], r[1], r[2], r[3], r[4], r[5]
            ms = f"{fed / el / 1e6:8.3f}" if el > 0 else f"{'':>8}"
            vs = f"{v:+7.3f}" if v is not None else f"{'':>7}"
            print(f"    {i:>3} {play.value:>12,} {n_pre:>8,} {fmax:>10,} "
                  f"{ms} {vs}  {verdict}")
            if verdict.startswith("STREAM"):
                good.append((key, i, fed / el / 1e6))
    print()
    if not good:
        print("  Nothing streamed reached W1 under either documented recipe.")
        return good
    print("  Combinations that put the stream on the output:")
    for key, i, ms in good:
        head = ms / (target / 1e6)
        print(f"    {key} config {i}: {ms:.3f} MS/s = {head:.2f}x the "
              f"{target / 1e6:.3f} MS/s colour rate"
              + ("" if head >= 1.0 else "  -- short for colour"))
    return good


# Loop structures to try in a streaming configuration. The examples use the
# three-call form; the question is whether dropping calls buys enough rate,
# because sustaining R with a FIFO of D samples needs a loop period under
# D/R -- 2.29 ms for 32768 samples at the colour rate.
FEED_LOOPS = (
    ("3call", "Status + PlayStatus + PlayData (as the SDK examples)"),
    ("2call", "PlayStatus + PlayData (drop AnalogOutStatus)"),
    ("paced", "PlayData only, fill tracked against the clock, "
              "PlayStatus every 100 ms to resync"),
)


def feed_tune(dwf, secs=1.2):
    """Maximum sustainable PlayData rate in a streaming configuration.

    Streaming only works where play memory is 0 (measured by --feed-matrix),
    and those configurations have a FIFO of roughly 32768 samples. So the rate
    ceiling is FIFO depth divided by loop period, and the loop period is
    dominated by USB round trips. This measures the round trip in the chosen
    configuration -- the 1413 us figure came from config 3 and may not apply --
    then sweeps loop structure against target rate, checking the output each
    time rather than trusting the counters.
    """
    ndev, ncfg = ctypes.c_int(), ctypes.c_int()
    check(dwf, dwf.FDwfEnum(ctypes.c_int(0), ctypes.byref(ndev)), "FDwfEnum")
    check(dwf, dwf.FDwfEnumConfig(ctypes.c_int(0), ctypes.byref(ncfg)),
          "FDwfEnumConfig")
    cfg, best_scope = -1, -1
    for i in range(ncfg.value):
        play, scope = ctypes.c_int(), ctypes.c_int()
        dwf.FDwfEnumConfigInfo(ctypes.c_int(i), DECIAnalogOutPlaySize,
                               ctypes.byref(play))
        dwf.FDwfEnumConfigInfo(ctypes.c_int(i), DECIAnalogInBufferSize,
                               ctypes.byref(scope))
        if play.value == 0 and scope.value > best_scope:
            cfg, best_scope = i, scope.value
    if cfg < 0:
        raise RuntimeError("No configuration with play memory 0; none of them "
                           "can stream. Run --feed-matrix.")
    print(f"  streaming configuration {cfg} (play memory 0, scope buffer "
          f"{best_scope:,})")

    hdwf = ctypes.c_int()
    check(dwf, dwf.FDwfDeviceConfigOpen(ctypes.c_int(0), ctypes.c_int(cfg),
                                        ctypes.byref(hdwf)), "ConfigOpen")
    try:
        ch = ctypes.c_int(0)
        setup_scope(dwf, hdwf)
        sts = ctypes.c_ubyte()
        free, lost, corrupt = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()

        # Round trip in THIS configuration, per call type.
        def timeit(fn, n=150):
            t = time.time()
            for _ in range(n):
                fn()
            return (time.time() - t) / n
        rt_s = timeit(lambda: dwf.FDwfAnalogOutStatus(hdwf, ch,
                                                      ctypes.byref(sts)))
        rt_p = timeit(lambda: dwf.FDwfAnalogOutNodePlayStatus(
            hdwf, ch, AnalogOutNodeCarrier, ctypes.byref(free),
            ctypes.byref(lost), ctypes.byref(corrupt)))
        print(f"  round trip here: AnalogOutStatus {rt_s * 1e6:.0f} us, "
              f"PlayStatus {rt_p * 1e6:.0f} us")

        SRC = 1 << 18
        src = (ctypes.c_double * SRC)()
        for i in range(SRC):
            src[i] = FEED_STREAM_V

        def arm(target):
            dwf.FDwfAnalogOutReset(hdwf, ch)
            dwf.FDwfDeviceAutoConfigureSet(hdwf, ctypes.c_int(0))
            dwf.FDwfAnalogOutNodeEnableSet(hdwf, ch, AnalogOutNodeCarrier,
                                           ctypes.c_int(1))
            dwf.FDwfAnalogOutNodeFunctionSet(hdwf, ch, AnalogOutNodeCarrier,
                                             funcPlayPattern)
            dwf.FDwfAnalogOutNodeFrequencySet(hdwf, ch, AnalogOutNodeCarrier,
                                              ctypes.c_double(target))
            dwf.FDwfAnalogOutNodeAmplitudeSet(hdwf, ch, AnalogOutNodeCarrier,
                                              ctypes.c_double(1.0))
            cbuf = ctypes.c_int(0)
            dwf.FDwfAnalogOutNodeDataInfo(hdwf, ch, AnalogOutNodeCarrier, 0,
                                          ctypes.byref(cbuf))
            pre = (ctypes.c_double * max(1, cbuf.value))()
            for i in range(len(pre)):
                pre[i] = FEED_PRIME_V
            dwf.FDwfAnalogOutNodeDataSet(hdwf, ch, AnalogOutNodeCarrier, pre,
                                         ctypes.c_int(len(pre)))
            dwf.FDwfAnalogOutConfigure(hdwf, ch, ctypes.c_int(1))
            return len(pre)

        def run(kind, target):
            arm(target)
            fed = t_lost = t_corr = 0
            depth = 1 << 15                 # refined from observed cFree
            t0 = time.time()
            t_poll = 0.0
            fill = 0.0
            err = ""
            while True:
                now = time.time() - t0
                if now >= secs:
                    break
                if kind == "3call":
                    if dwf.FDwfAnalogOutStatus(hdwf, ch,
                                               ctypes.byref(sts)) != 1:
                        err = "Status error"; break
                    if sts.value != DwfStateRunning:
                        err = DWF_STATE_NAMES.get(sts.value, "?"); break
                if kind in ("3call", "2call"):
                    dwf.FDwfAnalogOutNodePlayStatus(
                        hdwf, ch, AnalogOutNodeCarrier, ctypes.byref(free),
                        ctypes.byref(lost), ctypes.byref(corrupt))
                    t_lost += lost.value
                    t_corr += corrupt.value
                    depth = max(depth, free.value)
                    n = min(free.value, SRC)
                else:
                    if now >= t_poll:
                        dwf.FDwfAnalogOutNodePlayStatus(
                            hdwf, ch, AnalogOutNodeCarrier,
                            ctypes.byref(free), ctypes.byref(lost),
                            ctypes.byref(corrupt))
                        t_lost += lost.value
                        t_corr += corrupt.value
                        depth = max(depth, free.value)
                        fill = depth - free.value     # resync to the truth
                        t_poll = now + 0.1
                    else:
                        fill = max(0.0, fed - now * target)
                    n = int(min(depth - fill, SRC))
                if n <= 0:
                    continue
                if dwf.FDwfAnalogOutNodePlayData(
                        hdwf, ch, AnalogOutNodeCarrier, src,
                        ctypes.c_int(n)) != 1:
                    err = "PlayData refused"; break
                fed += n
            el = time.time() - t0
            s = capture(dwf, hdwf, 1e6, 1 << 13)
            v = sorted(s)[len(s) // 2]
            return fed / el / 1e6, t_lost, t_corr, v, depth, err

        # Sweep PAST the colour rate. The earlier sweep stopped at 14.286 and
        # reported "achievable", which told us nothing about the margin -- and
        # the margin is what decides whether occasional host hiccups turn into
        # dropouts.
        targets = (1e6, 5e6, 11e6, 100e6 / 7, 16e6, 20e6, 25e6, 30e6)
        print("\n  Throughput only -- DC cannot reveal dropped samples, and "
              "the counters\n  read 0 on a stream that was measurably "
              "corrupted. Integrity needs\n  --stream-capture on the real "
              "waveform.")
        print(f"\n  {'loop':6} {'target':>9} {'achieved':>9} {'lost':>8} "
              f"{'corrupt':>8} {'CH1':>7} {'depth':>8}  verdict")
        best = {}
        for kind, _ in FEED_LOOPS:
            for tg in targets:
                ms, lo, co, v, depth, err = run(kind, tg)
                ok = (not err and lo == 0 and co == 0
                      and abs(v - FEED_STREAM_V) < 0.15
                      and ms >= tg / 1e6 * 0.999)
                if ok:
                    best[kind] = max(best.get(kind, 0.0), tg / 1e6)
                # "meets rate", NOT "clean": this streams DC, and a dropped
                # or duplicated DC sample is indistinguishable from a kept
                # one. lost and corrupt both read 0 on a stream that a direct
                # capture showed had 179 us of jitter and 28 bad lines in 607.
                # So this measures THROUGHPUT only. Signal integrity has to be
                # measured on the real waveform, with --stream-capture.
                short = max(0.0, tg / 1e6 - ms) * 1e6
                verdict = err or ("meets rate" if ok else
                                  f"short {short / 908:.0f} lines/s")
                print(f"  {kind:6} {tg / 1e6:9.3f} {ms:9.3f} {lo:>8,} "
                      f"{co:>8,} {v:+7.3f} {depth:>8,}  {verdict}")
        print()
        need = 100e6 / 7 / 1e6
        for kind, desc in FEED_LOOPS:
            b = best.get(kind, 0.0)
            print(f"  {kind:6} max clean {b:6.3f} MS/s "
                  f"({b / need:4.2f}x colour)  {desc}")
        top = max(best.values()) if best else 0.0
        print()
        if top >= need:
            print(f"  Colour streaming is achievable at {top:.3f} MS/s.")
        else:
            print(f"  Best clean rate {top:.3f} MS/s against "
                  f"{need:.3f} needed for colour.")
            print(f"  Monochrome needs far less: 54 samples/line is "
                  f"{54 * 15734 / 1e6:.3f} MS/s, 370 is "
                  f"{370 * 15734 / 1e6:.3f} MS/s.")
        return best
    finally:
        dwf.FDwfDeviceClose(hdwf)


def stream_probe(dwf, hdwf, ch, target=100e6 / 7):
    """Measure sustainable PlayData throughput, and say what limits it.

    The feed loop is already synthesis-free and zero-copy, so if it cannot
    reach the colour rate the cost is per-call overhead, not host compute --
    and no amount of C or Rust changes that. What decides it is how many
    samples the device reports free per round trip, so this sweeps chunk size
    and times each API call separately:

      latency-bound   PlayData time stays flat as the chunk grows; the fixed
                      cost of the round trips dominates. Bigger chunks win.
      bandwidth-bound PlayData time scales with the chunk. The wire is full
                      and the rate is the ceiling.
    """
    scale = PLAY_SCALE_OVERRIDE or 1.0
    print(f"\nPlayData throughput probe (target {target / 1e6:.6f} MS/s "
          f"for colour, scale {scale:.3f}x)")

    # A cheap ramp: content is irrelevant to throughput, and synthesising a
    # real sequence here would only slow the setup down.
    SRC = 1 << 22                                 # 4 Mi samples, 32 MB
    src = (ctypes.c_double * SRC)()
    for i in range(SRC):
        src[i] = -1.0 + 2.0 * (i / (SRC - 1))

    sts = ctypes.c_ubyte()
    free, lost, corrupt = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()

    # Fixed cost of a single round trip, with nothing else going on.
    t = time.time()
    for _ in range(200):
        dwf.FDwfAnalogOutStatus(hdwf, ch, ctypes.byref(sts))
    rt = (time.time() - t) / 200
    print(f"  round trip: AnalogOutStatus {rt * 1e6:.1f} us "
          f"-> {1.0 / rt:.0f} calls/s ceiling per transaction")

    print(f"\n  {'chunk':>9} {'MS/s':>8} {'iters/s':>8} {'free avg':>9} "
          f"{'free max':>9} {'PlayData':>9} {'status':>8} {'lost':>7} {'corr':>6}")
    best = (0.0, 0)
    for cap in (1 << 12, 1 << 14, 1 << 16, 1 << 18, 1 << 20, 1 << 22):
        check(dwf, dwf.FDwfAnalogOutReset(hdwf, ch), "Reset")
        dwf.FDwfDeviceAutoConfigureSet(hdwf, ctypes.c_int(0))
        for fn, a in ((dwf.FDwfAnalogOutNodeEnableSet, ctypes.c_int(1)),
                      (dwf.FDwfAnalogOutNodeFunctionSet, funcPlay)):
            check(dwf, fn(hdwf, ch, AnalogOutNodeCarrier, a), "node setup")
        check(dwf, dwf.FDwfAnalogOutNodeFrequencySet(
            hdwf, ch, AnalogOutNodeCarrier,
            ctypes.c_double(target / scale)), "FrequencySet")
        check(dwf, dwf.FDwfAnalogOutNodeAmplitudeSet(
            hdwf, ch, AnalogOutNodeCarrier, ctypes.c_double(1.0)),
            "AmplitudeSet")
        check(dwf, dwf.FDwfAnalogOutNodeOffsetSet(
            hdwf, ch, AnalogOutNodeCarrier, ctypes.c_double(0.0)),
            "OffsetSet")
        # Prime with a chunk, then run unbounded and append.
        prime = min(SRC, 1 << 20)
        check(dwf, dwf.FDwfAnalogOutNodeDataSet(
            hdwf, ch, AnalogOutNodeCarrier, src, ctypes.c_int(prime)),
            "DataSet")
        check(dwf, dwf.FDwfAnalogOutRunSet(hdwf, ch, ctypes.c_double(0.0)),
              "RunSet")
        check(dwf, dwf.FDwfAnalogOutRepeatSet(hdwf, ch, ctypes.c_int(0)),
              "RepeatSet")
        check(dwf, dwf.FDwfAnalogOutConfigure(hdwf, ch, ctypes.c_int(1)),
              "Configure")

        pos = 0
        fed = iters = fsum = fmax = 0
        t_pd = t_st = 0.0
        t_lost = t_corr = 0
        t0 = time.time()
        while time.time() - t0 < 2.5:
            ta = time.time()
            check(dwf, dwf.FDwfAnalogOutStatus(hdwf, ch, ctypes.byref(sts)),
                  "AnalogOutStatus")
            check(dwf, dwf.FDwfAnalogOutNodePlayStatus(
                hdwf, ch, AnalogOutNodeCarrier, ctypes.byref(free),
                ctypes.byref(lost), ctypes.byref(corrupt)), "PlayStatus")
            tb = time.time()
            t_st += tb - ta
            t_lost += lost.value
            t_corr += corrupt.value
            fsum += free.value
            fmax = max(fmax, free.value)
            want = min(free.value, cap, SRC - pos)
            if want > 0:
                check(dwf, dwf.FDwfAnalogOutNodePlayData(
                    hdwf, ch, AnalogOutNodeCarrier,
                    ctypes.byref(src, pos * 8), ctypes.c_int(want)),
                    "PlayData")
                t_pd += time.time() - tb
                pos = (pos + want) % SRC
                fed += want
            iters += 1
        el = time.time() - t0
        ms = fed / el / 1e6
        if ms > best[0]:
            best = (ms, cap)
        print(f"  {cap:>9,} {ms:>8.3f} {iters / el:>8.0f} "
              f"{fsum / max(iters, 1):>9,.0f} {fmax:>9,} "
              f"{t_pd / el * 100:>8.1f}% {t_st / el * 100:>7.1f}% "
              f"{t_lost:>7,} {t_corr:>6,}")
    dwf.FDwfAnalogOutReset(hdwf, ch)

    ms, cap = best
    print(f"\n  best {ms:.3f} MS/s at chunk {cap:,} "
          f"({ms / (target / 1e6) * 100:.0f}% of the {target / 1e6:.3f} MS/s "
          f"colour rate)")
    if ms >= target / 1e6:
        print("  VERDICT: unbounded streaming at the colour rate is viable.")
    else:
        need = target / 1e6 / max(ms, 1e-9)
        print(f"  VERDICT: short by {need:.2f}x. Unbounded colour streaming is\n"
              f"           not sustainable at this chunk size. Options: a long\n"
              f"           deep-memory loop (9.34 s), or a lower sample rate.")
    return ms

def main():
    argv = sys.argv[1:]

    def opt(flag, cast, default):
        return cast(argv[argv.index(flag) + 1]) if flag in argv else default

    flags = {"--help", "-h",
             # generation
             "--deep", "--stream", "--color", "--progressive", "--locked",
             "--run-infinite", "--series-75", "--max-res", "--motion",
             "--motion-stream", "--stream-rate-check", "--stream-static",
             # measurement
             "--dump", "--verify", "--analyze", "--scan", "--compare",
             "--capture",
             # device capability probes
             "--play-info", "--play-test", "--play-configs", "--pattern-info",
             "--clock-info", "--stream-probe", "--feed-matrix",
             "--feed-tune",
             # A/B switches for things that were tested and are documented
             "--no-filter", "--sync-tip-zero", "--vbi-black",
             "--no-loop-align", "--fsc-nominal"}
    opts = {"--lines", "--spl", "--pattern", "--save-capture",
            "--stream-rate", "--play-scale", "--config", "--sat",
            "--sys-clock", "--capture", "--seconds", "--level",
            "--burst-start", "--burst-amp", "--copies", "--setup",
            "--bar-amp", "--motion-seconds", "--stream-capture",
            "--stream-func", "--stream-align", "--stream-resync",
            "--stream-guard", "--stream-capture-s"}
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        print("Flags:")
        for f in sorted(flags):
            print(f"  {f}")
        print("\nOptions taking a value:")
        for o in sorted(opts):
            print(f"  {o} <value>")
        return

    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--compare":
            i += 3                      # takes two file paths
            continue
        if a in opts:
            i += 2
            continue
        if a in flags:
            i += 1
            continue
        raise RuntimeError(f"Unknown argument {a!r}. Flags: "
                           f"{' '.join(sorted(flags))}; options taking a "
                           f"value: {' '.join(sorted(opts))}")

    global TOTAL_LINES, PATTERN, SAVE_CAPTURE, INTERLACED
    global MAX_RES, STREAM_MAX_RATE, SATURATION, PLAY_SCALE_OVERRIDE
    global FILTER_CHROMA, STD_DC, FSC_NOMINAL, SYS_CLOCK, LOOP_ALIGN
    global VBI_BLACK, LEVEL, BURST_START_US, BURST_AMP
    global RUN_INFINITE, COPIES
    global SOURCE_SERIES_75R, MOTION_SECONDS, STREAM_RATE_CHECK
    global STREAM_CAPTURE_AT, STREAM_FUNC, STREAM_STATIC, STREAM_ALIGN
    global STREAM_RESYNC_S, STREAM_GUARD, STREAM_CAPTURE_S
    # --locked: the combination measured to lock a Leader LV5700A through a
    # vectorscope-and-monitor chain. Individual flags still override it.
    if "--locked" in argv:
        LEVEL, BURST_AMP = 0.9, 41.0 / 200.0
        argv = argv + ["--run-infinite"]
    LEVEL = opt("--level", float, LEVEL)
    SOURCE_SERIES_75R = "--series-75" in argv
    RUN_INFINITE = "--run-infinite" in argv
    COPIES = max(1, opt("--copies", int, 1))
    BURST_START_US = opt("--burst-start", float, BURST_START_US)
    BURST_AMP = opt("--burst-amp", float, BURST_AMP * 200.0) / 200.0
    if "--locked" in argv:
        print(f"Preset --locked: level 0.9, burst {BURST_AMP * 200:.0f} IRE "
              f"at {BURST_START_US} us, infinite run")
    set_setup_ire(opt("--setup", float, SETUP_IRE))
    set_bar_amp(opt("--bar-amp", float, BAR_AMP * 100.0))
    STREAM_RATE_CHECK = "--stream-rate-check" in argv
    STREAM_CAPTURE_AT = opt("--stream-capture", float, None)
    STREAM_STATIC = "--stream-static" in argv
    STREAM_ALIGN = max(1, opt("--stream-align", int, 1))
    STREAM_RESYNC_S = opt("--stream-resync", float, STREAM_RESYNC_S)
    STREAM_GUARD = max(0, opt("--stream-guard", int, STREAM_GUARD))
    STREAM_CAPTURE_S = opt("--stream-capture-s", float, STREAM_CAPTURE_S)
    STREAM_FUNC = opt("--stream-func", str, STREAM_FUNC)
    if STREAM_FUNC not in ("play", "pattern"):
        raise RuntimeError("--stream-func must be 'play' or 'pattern'.")
    MOTION_SECONDS = opt("--motion-seconds", float, MOTION_SECONDS)
    if MOTION_SECONDS <= 0:
        raise RuntimeError("--motion-seconds must be positive.")
    VBI_BLACK = "--vbi-black" in argv
    LOOP_ALIGN = "--no-loop-align" not in argv
    SYS_CLOCK = opt("--sys-clock", float, None)
    FSC_NOMINAL = "--fsc-nominal" in argv
    FILTER_CHROMA = "--no-filter" not in argv
    STD_DC = "--sync-tip-zero" not in argv
    SATURATION = opt("--sat", float, SATURATION)
    PLAY_SCALE_OVERRIDE = opt("--play-scale", float, None)
    STREAM_MAX_RATE = opt("--stream-rate", float, STREAM_MAX_RATE)
    INTERLACED = "--progressive" not in argv
    MAX_RES = "--max-res" in argv
    SAVE_CAPTURE = opt("--save-capture", str, None)
    TOTAL_LINES = opt("--lines", int, TOTAL_LINES)
    PATTERN = opt("--pattern", str, PATTERN)
    if PATTERN not in PATTERNS:
        raise RuntimeError(f"Unknown --pattern {PATTERN!r}; "
                           f"choose from {', '.join(PATTERNS)}.")
    spl_force = opt("--spl", int, None)

    if "--color" in argv and not ({"--deep", "--stream"} & set(argv)):
        raise RuntimeError("--color needs --deep (recommended) or --stream.")
    if "--motion-stream" in argv and "--motion" not in argv:
        raise RuntimeError("--motion-stream selects the feed path for "
                           "--motion; pass --motion as well.")
    if "--motion" in argv:
        if "--deep" not in argv or "--color" not in argv:
            raise RuntimeError("--motion needs --deep --color: it streams a "
                               "ball over the colour bars from play memory.")
        bad = {"--analyze", "--scan", "--verify"} & set(argv)
        if bad:
            raise RuntimeError(
                f"--motion cannot be combined with {' '.join(sorted(bad))}: "
                f"those capture CH1 while the feed loop must keep play memory "
                f"topped up, and a stalled feed corrupts the output. Analyse "
                f"the static signal instead (drop --motion).")
    if "--color" in argv and "--stream" in argv:
        print("NOTE: --stream feeds chunks via PlayData and measured ~5.8 MS/s\n"
              "      sustained; colour needs 14.3 MS/s, so expect starvation.\n"
              "      --deep preloads the whole sequence into play memory and\n"
              "      runs at full rate with no host feed at all.")

    if "--compare" in argv:
        i = argv.index("--compare")
        compare_captures(argv[i + 1], argv[i + 2])
        return

    if "--dump" in argv:
        dump(spl_force or 1024)
        return

    dwf = load_dwf()
    ver = ctypes.create_string_buffer(32)
    dwf.FDwfGetVersion(ver)
    print(f"WaveForms SDK {ver.value.decode()}")

    if "--pattern-info" in argv:
        pattern_info(dwf)
        return

    if "--feed-matrix" in argv:
        feed_matrix(dwf)
        return

    if "--feed-tune" in argv:
        feed_tune(dwf)
        return

    if "--play-configs" in argv:
        play_configs(dwf)
        return

    if "--capture" in argv:
        print("Capturing CH1 (nothing generating)...")
        capture_only(dwf, opt("--capture", str, "/tmp/reference.csv"),
                     seconds=opt("--seconds", float, 0.25))
        return

    print("Enumerating...")
    if "--motion-stream" in argv:
        cfg = pick_stream_config(dwf, force=opt("--config", int, None))
    elif "--deep" in argv:
        cfg = pick_deep_config(dwf, force=opt("--config", int, None))
    else:
        cfg = pick_config(dwf, for_stream="--stream" in argv,
                          force=opt("--config", int, None))

    hdwf = ctypes.c_int()
    check(dwf, dwf.FDwfDeviceConfigOpen(ctypes.c_int(0), ctypes.c_int(cfg),
                                        ctypes.byref(hdwf)), "DeviceConfigOpen")
    if hdwf.value == 0:
        raise RuntimeError(f"Could not open device: {last_error(dwf)}")

    ch = ctypes.c_int(0)
    try:
        if "--play-test" in argv:
            play_test(dwf, hdwf, ch)
            return

        if "--stream-probe" in argv:
            stream_probe(dwf, hdwf, ch)
            return

        if "--play-info" in argv:
            play_info(dwf, hdwf)
            return

        if "--clock-info" in argv:
            clock_info(dwf, hdwf)
            return

        if "--deep" in argv:
            fn = funcPlay
            dwf.FDwfAnalogOutNodeFunctionSet(hdwf, ch, AnalogOutNodeCarrier, fn)
            if PLAY_SCALE_OVERRIDE:
                scale = PLAY_SCALE_OVERRIDE
            else:
                setup_scope(dwf, hdwf)
                capture(dwf, hdwf, 25e6, 1 << 16)      # discard: settling
                scale = calibrate_play_rate(dwf, hdwf, ch, fn)
            div, spl, rate, line_hz = choose_stream_timing(
                100e6, colour="--color" in argv)
            print(f"Deep-memory playback: {frame_lines()}-line "
                  f"{'interlaced' if INTERLACED else 'progressive'}, "
                  f"func {fn.value}, rate scale {scale:.4f}x")
            print(f"      {describe(spl, rate)}")
            report_timing(spl, rate)
            print(f"      100 MHz/{div} exactly, line {line_hz:.2f} Hz "
                  f"({(line_hz / LINE_FREQ_HZ - 1) * 100:+.4f}%)")
            if "--motion" in argv:
                if "--motion-stream" in argv:
                    start_motion_stream(dwf, hdwf, ch, spl, rate, scale)
                else:
                    start_motion(dwf, hdwf, ch, spl, rate, scale)
                    try:
                        while True:
                            time.sleep(0.5)
                    except KeyboardInterrupt:
                        print("\nStopped.")
                return
            total = start_deep(dwf, hdwf, ch, spl, rate, scale, fn,
                               colour="--color" in argv)
            if "--verify" in argv or "--analyze" in argv:
                setup_scope(dwf, hdwf)
                capture(dwf, hdwf, 50e6, 1 << 16)          # discard: settling
            if "--analyze" in argv:
                ntsc_analyze(dwf, hdwf, save=SAVE_CAPTURE)
            if "--scan" in argv:
                scan_anomalies(dwf, hdwf, save=SAVE_CAPTURE)
            if "--verify" in argv:
                verify(dwf, hdwf, rate / total,
                       groups_per_seq=(colour_fields()[0]
                                       if "--color" in argv
                                       else (2 if INTERLACED else 1)))
            print(f"      (pass --play-scale {scale:.4f} to skip calibration)")
            print("\nRunning from deep memory. Press Ctrl+C to stop.")
            while True:
                time.sleep(1)

        if "--stream" in argv:
            fmin, fmax = ctypes.c_double(), ctypes.c_double()
            check(dwf, dwf.FDwfAnalogOutNodeFunctionSet(
                hdwf, ch, AnalogOutNodeCarrier, funcPlay), "set funcPlay")
            check(dwf, dwf.FDwfAnalogOutNodeFrequencyInfo(
                hdwf, ch, AnalogOutNodeCarrier, ctypes.byref(fmin),
                ctypes.byref(fmax)), "play FrequencyInfo")
            scale = (PLAY_SCALE_OVERRIDE if PLAY_SCALE_OVERRIDE
                     else play_rate_scale(dwf, hdwf, ch))
            print(f"Streaming {frame_lines()}-line "
                  f"{'interlaced' if INTERLACED else 'progressive'} frame, "
                  f"play rate scale {scale:.4f}x")
            want_measure = "--verify" in argv
            if want_measure:
                # Settle the input stage now; doing it mid-stream would stall
                # the feed for two seconds and starve the FIFO.
                setup_scope(dwf, hdwf)
                capture(dwf, hdwf, 50e6, 1 << 16)       # discard
            cap = fmax.value * scale
            streamed = False
            for attempt in range(4):
                div, spl, rate, line_hz = choose_stream_timing(cap)
                print(f"\nAttempt {attempt + 1}: {spl} samples/line at "
                      f"{rate / 1e6:.6f} MS/s (100 MHz/{div}), line "
                      f"{line_hz:.2f} Hz "
                      f"({(line_hz / LINE_FREQ_HZ - 1) * 100:+.4f}%)")
                report_timing(spl, rate)
                data, total, pos, got = start_stream(dwf, hdwf, ch, spl, rate,
                                                     scale,
                                                     "--color" in argv)
                why, sustainable = stream_loop(dwf, hdwf, ch, data, total,
                                               pos, got,
                                               spl if want_measure else None)
                if why != "starved":
                    streamed = True
                    break
                # Strictly below both the measured throughput and the rate
                # that just failed, so the ladder cannot pick the same rate.
                cap = min(sustainable * 0.85, rate * 0.9)
                dwf.FDwfAnalogOutReset(hdwf, ch)
                try:
                    choose_stream_timing(cap)
                except RuntimeError:
                    break
                print(f"  retrying with a {cap / 1e6:.2f} MS/s ceiling")
            if not streamed:
                print("\nStreaming cannot be sustained on this device; "
                      "falling back to the looped funcCustom path.")
            else:
                return

        fsfunc = ctypes.c_uint()
        check(dwf, dwf.FDwfAnalogOutNodeFunctionInfo(hdwf, ch,
                                                     AnalogOutNodeCarrier,
                                                     ctypes.byref(fsfunc)),
              "NodeFunctionInfo")
        if not (fsfunc.value >> 30) & 1:
            raise RuntimeError("Channel does not support funcCustom.")

        smin, smax = ctypes.c_int(), ctypes.c_int()
        check(dwf, dwf.FDwfAnalogOutNodeDataInfo(hdwf, ch, AnalogOutNodeCarrier,
                                                 ctypes.byref(smin),
                                                 ctypes.byref(smax)),
              "NodeDataInfo")
        dac = ctypes.c_int()
        if dwf.FDwfDeviceParamGet(hdwf, DwfParamDACFrequency,
                                  ctypes.byref(dac)) == 0:
            dac.value = 0
        clk = f"{dac.value / 1e6:.3f} MHz" if dac.value else "unknown"
        print(f"AWG: {smin.value}..{smax.value} samples, DAC clock {clk}")

        awg_clk = float(dac.value) if dac.value else AWG_CLOCK_HZ
        if not dac.value:
            print(f"      assuming a {awg_clk / 1e6:.0f} MHz AWG divider clock "
                  f"(device does not report it)")
        spl, rate, n, err = choose_timing(smax.value, awg_clk, spl_force)
        kind = ("525-line interlaced frame (2 fields of 262.5 lines)"
                if INTERLACED else
                f"{TOTAL_LINES}-line progressive frame "
                f"({active_lines()} active)")
        print(f"Synthesizing {kind} at")
        print(f"      {describe(spl, rate)}")
        report_timing(spl, rate)
        if n:
            print(f"      DAC divider N={n} -> {rate / 1e6:.6f} MS/s, "
                  f"line rate error {err * 100:+.4f}%")
        if spl < 96:
            print(f"      NOTE: coarse; {LINE_US / spl:.2f} us per sample.")

        if "--fill" in argv and INTERLACED:
            raise RuntimeError("--fill only applies to --progressive.")
        total, frame_hz = start_awg(dwf, hdwf, ch, spl, rate)

        if "--analyze" in argv:
            setup_scope(dwf, hdwf)
            capture(dwf, hdwf, 50e6, 1 << 16)
            ntsc_analyze(dwf, hdwf, save=SAVE_CAPTURE)
        if "--verify" in argv:
            verify(dwf, hdwf, frame_hz)

        print("\nRunning. Press Ctrl+C to stop.")
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        dwf.FDwfAnalogOutConfigure(hdwf, ch, ctypes.c_int(0))
        dwf.FDwfDigitalOutConfigure(hdwf, ctypes.c_int(0))
        dwf.FDwfDeviceCloseAll()
        print("Device closed.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
