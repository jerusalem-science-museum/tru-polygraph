"""Cleaning filters for the 500 Hz GSR stream (pure numpy/scipy, no GUI).

All filters take a 1-D array of voltages (or ADC codes) and the sample rate
``fs`` in Hz and return a new float ndarray of the same length.  Inputs that
are too short to filter are returned unchanged (as a float array), exactly
like the old ``apply_50hz_notch_filter`` in main.py (formerly polygraph_app.py / gsr_polygraph.py).

Filters are zero-phase (forward-backward) so they do not shift GSR responses
in time, which matters because question markers are compared with the signal.

Recommended default: ``clean_gsr(values, fs)``.  See NOISE_ANALYSIS.md.
"""

from __future__ import annotations

import numpy as np
from scipy import signal

MAINS_HZ = 50.0
_MIN_LEN = 30  # below this many samples we do not filter at all
FS_TOLERANCE = 0.01  # Use the measured sample rate only if within 1% of the nominal rate.
GAP_FACTOR = 1.5  # A sample interval above 1.5 periods means dropped samples.


def _as_float(values) -> np.ndarray:
    return np.array(values, dtype=float, copy=True).ravel()


# --------------------------------------------------------------- timing ---

def unwrap_micros(arduino_time_us) -> np.ndarray:
    """Unwrap a 32-bit ``micros()`` series into a monotonic int64 series."""
    t = np.asarray(arduino_time_us, dtype=np.int64).ravel()
    if t.size == 0:
        return t.copy()
    step = np.diff(t) % (1 << 32)  # forward distance modulo 2**32
    return t[0] + np.concatenate(([0], np.cumsum(step)))


def estimate_fs(arduino_time_us) -> float:
    """Sample rate from Arduino timestamps: 1e6 / median(dt).

    Handles the 32-bit wrap of ``micros()``.  The median ignores stalls and
    dropped samples.  Returns 0.0 if fewer than two timestamps are given.
    """
    t = unwrap_micros(arduino_time_us)
    if t.size < 2:
        return 0.0
    dt = np.diff(t)
    dt = dt[dt > 0]
    if dt.size == 0:
        return 0.0
    return float(1e6 / np.median(dt))


def resample_uniform(time_s, values, fs: float):
    """Linear interpolation onto a uniform grid; returns (t_uniform, v_uniform).

    Use after dropped samples / jitter, before the filters (which assume a
    uniform grid).
    """
    t = np.asarray(time_s, dtype=float)
    v = np.asarray(values, dtype=float)
    if t.size < 2:
        return t.copy(), v.copy()
    grid = np.arange(t[0], t[-1], 1.0 / fs)
    return grid, np.interp(grid, t, v)


# -------------------------------------------------------------- filters ---

def notch_50(values, fs: float, f0: float = MAINS_HZ, q: float = 30.0) -> np.ndarray:
    """The ORIGINAL approach: one IIR notch at 50 Hz (Q=30), zero-phase.

    Kept for comparison.  It leaves 100/150/200 Hz and the aliased
    harmonics untouched and is only ~1.7 Hz wide, so mains drift or an
    Arduino clock error of a few tenths of a percent defeats it.
    """
    x = _as_float(values)
    if x.size < _MIN_LEN or f0 >= fs / 2:
        return x
    b, a = signal.iirnotch(f0, q, fs=fs)
    return signal.filtfilt(b, a, x, padlen=min(x.size - 1, 3 * int(fs / f0) * 3))


def comb_notch_mains(values, fs: float, f0: float = MAINS_HZ, harmonics=None,
                     q: float = 30.0) -> np.ndarray:
    """Cascade of notches at f0, 2*f0, ... (all multiples below Nyquist).

    ``harmonics`` is an iterable of integer multiples; default is every
    multiple strictly below 0.98 * Nyquist.  Zero-phase.
    """
    x = _as_float(values)
    if x.size < _MIN_LEN:
        return x
    nyq = fs / 2.0
    if harmonics is None:
        harmonics = range(1, int(np.floor(0.98 * nyq / f0)) + 1)
    sos = []
    for k in harmonics:
        f = k * f0
        if 0 < f < 0.98 * nyq:
            b, a = signal.iirnotch(f, q, fs=fs)
            sos.append(signal.tf2sos(b, a))
    if not sos:
        return x
    sos = np.vstack(sos)
    return signal.sosfiltfilt(sos, x, padlen=min(x.size - 1, int(fs)))


def lowpass(values, fs: float, cutoff_hz: float = 5.0, order: int = 4) -> np.ndarray:
    """Butterworth low-pass, zero-phase (``sosfiltfilt``).

    Effective order is 2*order (forward + backward).  The edge padding is
    one second long so slow GSR levels do not produce start/end transients.
    """
    x = _as_float(values)
    if x.size < _MIN_LEN or cutoff_hz >= fs / 2:
        return x
    sos = signal.butter(order, cutoff_hz, btype="low", fs=fs, output="sos")
    return signal.sosfiltfilt(sos, x, padlen=min(x.size - 1, int(fs)))


def moving_average_mains(values, fs: float, f0: float = MAINS_HZ) -> np.ndarray:
    """Centered moving average exactly one mains period long (20 ms at 50 Hz).

    A boxcar of length fs/f0 samples has a transfer-function null at f0 and
    at EVERY multiple of f0, i.e. all mains harmonics, including the ones
    aliased down from above Nyquist.  If fs/f0 is not an integer the end
    taps get fractional weights, so a measured fs (``estimate_fs``) keeps the
    nulls on the mains even when the Arduino clock is slightly off.
    Zero-phase because the kernel is symmetric.
    """
    x = _as_float(values)
    if x.size < _MIN_LEN or fs / f0 < 2:
        return x
    length = fs / f0
    half = int(np.ceil(length / 2.0 + 0.5))
    j = np.arange(-half, half + 1)
    kernel = np.clip(length / 2.0 - (np.abs(j) - 0.5), 0.0, 1.0)
    kernel /= kernel.sum()
    padded = np.pad(x, half, mode="reflect", reflect_type="odd")
    return np.convolve(padded, kernel, mode="valid")


def clean_gsr(values, fs: float) -> np.ndarray:
    """RECOMMENDED cleaning for GSR sampled at ``fs`` (about 500 Hz).

    Stage 1: moving average one mains period long (nulls 50 Hz and every
             harmonic, tolerant of fs measured from Arduino timestamps).
    Stage 2: 2nd-order Butterworth low-pass at 5 Hz applied forward and
             backward (zero-phase, effective 4th order, -80 dB at 50 Hz).
             Kills everything above the GSR band, including LED/switching
             ripple that aliases to 100 Hz, and most white/quantisation
             noise.  Order 2 (not 4) halves the step overshoot (3.4 % vs
             6.8 %) at the same cutoff.
    Both stages are zero-phase, so GSR onsets are not delayed.  Inputs that
    are too short are returned unchanged.  Use the measured fs
    (``estimate_fs(arduino_time_us)``); fall back to 500.0.
    """
    x = _as_float(values)
    if x.size < _MIN_LEN or fs <= 0:
        return x
    return lowpass(moving_average_mains(x, fs), fs, cutoff_hz=5.0, order=2)


def clean_recording(voltage_values, nominal_fs: float, arduino_time_us=None) -> np.ndarray:
    """Clean a recorded GSR series with clean_gsr; always returns one value per input.

    fs is measured from the Arduino timestamps when they are available and within 1% of
    nominal_fs, otherwise nominal_fs is used. If samples were dropped (an interval
    longer than 1.5 periods), the filters would see a distorted time axis, so the series is
    resampled onto a uniform grid (resample_uniform), cleaned, and interpolated
    back onto the original sample times, which keeps the output aligned row for row.
    """
    values = np.asarray(voltage_values, dtype=float)
    fs = nominal_fs
    times_s = None
    if arduino_time_us is not None and len(arduino_time_us) == len(values) and len(values) >= 2:
        measured = estimate_fs(arduino_time_us)
        if np.isfinite(measured) and abs(measured - nominal_fs) <= FS_TOLERANCE * nominal_fs:
            fs = measured
        times_s = unwrap_micros(arduino_time_us) / 1e6

    if times_s is not None:
        steps = np.diff(times_s)
        if np.all(steps > 0) and steps.max() > GAP_FACTOR / fs:
            grid, uniform = resample_uniform(times_s, values, fs)
            cleaned = clean_gsr(uniform, fs)
            return np.interp(times_s, grid, cleaned)
    return clean_gsr(values, fs)


# Registry used by the diagnostics tool: name -> callable(values, fs)
FILTERS = {
    "notch_50 (old)": notch_50,
    "comb_notch": comb_notch_mains,
    "moving_avg_20ms": moving_average_mains,
    "lowpass_5Hz": lowpass,
    "clean_gsr": clean_gsr,
}
