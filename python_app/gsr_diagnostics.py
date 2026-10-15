"""GSR noise diagnostics.

    python python_app/gsr_diagnostics.py <voltage.csv> [--save out.png] [--show]
    python python_app/gsr_diagnostics.py --selftest [--save out.png]
    python python_app/gsr_diagnostics.py --port COM5 --seconds 30 [--save out.png]

Input CSV (written by polygraph_app.py): columns time_s, arduino_time_us,
raw_adc, voltage_v.  Only voltage_v or raw_adc is required; the other columns
are optional.  Produces a multi-panel figure and a text report (measured fs,
timing, band RMS, noise RMS before/after each filter, recommendation).
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import numpy as np
from scipy import ndimage, signal

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gsr_filters as gf  # noqa: E402

ADC_REF_V = 5.0
ADC_MAX = 1023
LSB_V = ADC_REF_V / ADC_MAX
MAINS = 50.0
FILTER_ORDER = list(gf.FILTERS)
COLORS = {
    "raw": "#9a9a9a",
    "notch_50 (old)": "#d95f02",
    "comb_notch": "#7570b3",
    "moving_avg_20ms": "#1b9e77",
    "lowpass_5Hz": "#e7298a",
    "clean_gsr": "#1f4e9c",
}


# ------------------------------------------------------------------ I/O ---

def load_csv(path) -> dict:
    cols: dict[str, list[float]] = {}
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for name in reader.fieldnames or []:
            cols[name.strip()] = []
        for row in reader:
            for name in cols:
                try:
                    cols[name].append(float(row[name]))
                except (TypeError, ValueError, KeyError):
                    cols[name].append(np.nan)
    arr = {k: np.asarray(v, dtype=float) for k, v in cols.items()}
    data: dict = {}
    if "raw_adc" in arr:
        data["raw_adc"] = arr["raw_adc"]
    if "voltage_v" in arr:
        data["voltage"] = arr["voltage_v"]
    elif "raw_adc" in arr:
        data["voltage"] = arr["raw_adc"] * LSB_V
    elif "voltage" in arr:
        data["voltage"] = arr["voltage"]
    else:
        raise ValueError("CSV needs a voltage_v or raw_adc column")
    if "arduino_time_us" in arr and np.isfinite(arr["arduino_time_us"]).all():
        data["arduino_us"] = arr["arduino_time_us"].astype(np.int64)
    elif "timeSeconds" in arr:  # MATLAB recordings
        data["arduino_us"] = np.round(arr["timeSeconds"] * 1e6).astype(np.int64)
    if "time_s" in arr:
        data["pc_time_s"] = arr["time_s"]
    return data


def capture_serial(port: str, seconds: float) -> dict:
    """Read GSR lines ('t_us,adc') for N seconds; ignore 'B,' and '#' lines."""
    import serial

    from arduino import BAUD_RATE, parse_line

    t_us, adc, arrival = [], [], []
    with serial.Serial(port, BAUD_RATE, timeout=0.25) as ser:
        time.sleep(2.0)
        ser.reset_input_buffer()
        start = time.monotonic()
        while time.monotonic() - start < seconds:
            line = ser.readline()
            if not line:
                continue
            try:
                parsed = parse_line(line.decode("ascii", errors="strict"))
            except (UnicodeDecodeError, ValueError):
                continue
            if parsed[0] != "gsr":
                continue
            t_us.append(parsed[1])
            adc.append(parsed[2])
            arrival.append(time.monotonic() - start)
    adc = np.asarray(adc, dtype=float)
    return {"raw_adc": adc, "voltage": adc * LSB_V,
            "arduino_us": np.asarray(t_us, dtype=np.int64),
            "pc_time_s": np.asarray(arrival)}


# ------------------------------------------------------------- analysis ---

def timing_stats(arduino_us) -> dict:
    t = gf.unwrap_micros(arduino_us)
    dt = np.diff(t).astype(float)
    if dt.size == 0:
        return {}
    med = float(np.median(dt[dt > 0])) if (dt > 0).any() else 0.0
    late = dt > 1.5 * med
    # missing = elapsed time / nominal period - samples actually present.  Robust to
    # stalls that are followed by catch-up samples (late but not lost).
    dropped = max(int(round((t[-1] - t[0]) / med)) - (len(t) - 1), 0) if med > 0 else 0
    return {
        "t_s": (t - t[0]) / 1e6, "dt_us": dt, "median_us": med,
        "fs": 1e6 / med if med else 0.0, "mean_us": float(dt.mean()),
        "std_us": float(dt.std()), "min_us": float(dt.min()),
        "max_us": float(dt.max()), "dropped": dropped,
        "late_n": int(np.sum(late)), "zero_dt": int(np.sum(dt <= 0)),
        "p99_us": float(np.percentile(np.abs(dt - med), 99)),
    }


def band_rms(x, fs, bands, nperseg=4096) -> list[float]:
    nperseg = int(min(len(x), nperseg))
    f, p = signal.welch(x - np.mean(x), fs=fs, nperseg=nperseg, detrend="linear")
    df = f[1] - f[0]
    return [float(np.sqrt(p[(f >= lo) & (f < hi)].sum() * df)) for lo, hi in bands]


def mains_bands(fs, width=2.0):
    nyq = fs / 2.0
    ks = range(1, int(nyq // MAINS) + 1)
    return [(max(MAINS * k - width, 0), min(MAINS * k + width, nyq + 1e-9)) for k in ks]


def noise_rms(x, fs) -> float:
    """RMS of (signal minus a 0.5 s running median): the 'fuzz' on top of the slow trace."""
    size = max(3, int(round(0.5 * fs)) | 1)
    trend = ndimage.median_filter(x, size=size, mode="nearest")
    r = (x - trend)[size:-size] if len(x) > 4 * size else x - trend
    return float(np.sqrt(np.mean(r ** 2)))


def apply_filters(v, fs) -> dict:
    return {name: fn(v, fs) for name, fn in gf.FILTERS.items()}


def quantization_stats(adc) -> dict:
    if adc is None:
        return {}
    codes = adc[np.isfinite(adc)].astype(int)
    d = np.diff(codes)
    sd = float(np.std(codes - ndimage.median_filter(codes, size=251, mode="nearest")))
    return {
        "n_codes": int(np.unique(codes).size),
        "span": int(codes.max() - codes.min()),
        "frac_same": float(np.mean(d == 0)),
        "frac_pm1": float(np.mean(np.abs(d) == 1)),
        "noise_lsb": sd,
    }


# ---------------------------------------------------- fidelity self-checks ---

def _scr(t, rise_10_90, decay=10.0, t0=0.0):
    """Bateman-style GSR response, peak normalised to 1, given 10-90 rise time."""
    tau_r = rise_10_90 / 2.2
    tt = np.clip(t - t0, 0, None)
    y = np.exp(-tt / decay) - np.exp(-tt / tau_r)
    tpk = (np.log(decay) - np.log(tau_r)) * decay * tau_r / (decay - tau_r)
    pk = np.exp(-tpk / decay) - np.exp(-tpk / tau_r)
    return y / pk


def fidelity(fs: float) -> dict:
    """Noiseless distortion tests of every filter at sample rate fs."""
    out = {"step": {}, "scr": {}, "edge": {}}
    # step: 0 -> 1 at t=6 s in a 12 s record
    t = np.arange(0, 12, 1 / fs)
    step = (t >= 6).astype(float)
    t_step = {}
    for name, fn in gf.FILTERS.items():
        y = fn(step, fs)
        ys = y[t >= 6]
        ts = t[t >= 6] - 6
        i10 = np.argmax(ys >= 0.1)
        i90 = np.argmax(ys >= 0.9)
        out_of_band = np.abs(y[t >= 6.15] - 1).max() * 100
        out["step"][name] = {
            "rise_ms": (ts[i90] - ts[i10]) * 1e3,
            "overshoot_pct": max(0.0, (y.max() - 1) * 100),
            "ripple_after_150ms_pct": out_of_band,
        }
        t_step[name] = (t, y)
    out["step_curves"] = t_step
    # GSR responses with 10-90 rise 0.3..5 s
    tt = np.arange(0, 40, 1 / fs)
    for rise in (0.3, 1.0, 2.0, 5.0):
        ref = _scr(tt, rise, t0=10.0)
        res = {}
        for name, fn in gf.FILTERS.items():
            y = fn(ref, fs)
            res[name] = (float(y.max() / ref.max() * 100),
                         float((tt[np.argmax(y)] - tt[np.argmax(ref)]) * 1e3))
        out["scr"][rise] = res
    # edge behaviour: high DC level + slow drift, error vs the input
    te = np.arange(0, 20, 1 / fs)
    slow = 2.0 + 0.4 * np.sin(2 * np.pi * 0.05 * te) + 0.02 * te
    n_edge = int(0.5 * fs)
    for name, fn in gf.FILTERS.items():
        e = fn(slow, fs) - slow
        out["edge"][name] = (float(np.abs(e[:n_edge]).max() * 1e3),
                             float(np.abs(e[-n_edge:]).max() * 1e3),
                             float(np.abs(e[n_edge:-n_edge]).max() * 1e3))
    return out


def freq_responses(fs: float) -> dict:
    rng = np.random.default_rng(1)
    x = rng.standard_normal(1 << 16)
    f, p0 = signal.welch(x, fs=fs, nperseg=2048)
    res = {}
    for name, fn in gf.FILTERS.items():
        _, p = signal.welch(fn(x, fs), fs=fs, nperseg=2048)
        res[name] = 10 * np.log10(np.maximum(p / p0, 1e-16))
    return {"f": f, "H": res}


# ----------------------------------------------------------------- report ---

def analyze(data: dict, truth: np.ndarray | None = None, label="data") -> dict:
    v = np.asarray(data["voltage"], dtype=float)
    adc = data.get("raw_adc")
    lines: list[str] = []
    P = lines.append
    P(f"=== GSR noise report: {label} ({len(v)} samples) ===")

    ts = timing_stats(data["arduino_us"]) if "arduino_us" in data else {}
    fs = ts["fs"] if ts and ts["fs"] > 0 else 500.0
    pc = data.get("pc_time_s")
    if ts:
        P("-- Timing (from arduino_time_us) --")
        P(f"measured fs        : {fs:.3f} Hz (median dt {ts['median_us']:.0f} us; nominal 500 Hz, "
          f"clock error {100 * (fs / 500 - 1):+.3f} %)")
        P(f"dt mean/std/min/max: {ts['mean_us']:.1f} / {ts['std_us']:.1f} / {ts['min_us']:.0f} / {ts['max_us']:.0f} us")
        P(f"late (>1.5x median): {ts['late_n']}; dropped samples (est.): {ts['dropped']}; "
          f"zero/negative dt: {ts['zero_dt']}")
        P(f"99th pct |dt-med|  : {ts['p99_us']:.0f} us  -> timing-jitter noise on a mains tone of "
          f"relative amplitude A: ~{2 * np.pi * MAINS * ts['p99_us'] * 1e-6:.3f} * A (p99)")
    else:
        P("-- No arduino_time_us column: assuming fs = 500 Hz --")
    if pc is not None and len(pc) > 2:
        dpc = np.diff(pc)
        if ts:
            ratio = dpc.std() / max(ts["std_us"] * 1e-6, 1e-9)
            P(f"PC arrival dt std  : {dpc.std() * 1e3:.2f} ms (Arduino dt std {ts['std_us'] / 1e3:.3f} ms, "
              f"x{ratio:.0f}); {100 * np.mean(dpc < 0.0005):.1f} % of PC intervals < 0.5 ms")
            if ratio > 5:
                P("  => time_s is PC arrival time (bursty). Plot/analyse with arduino_time_us instead.")

    if len(v) < 200:
        P("Too few samples for spectral analysis.")
        return {"report": "\n".join(lines), "fs": fs}

    # if samples were dropped, put everything on a uniform grid
    if ts and ts["dropped"] > 0:
        tg, v = gf.resample_uniform(ts["t_s"], v, fs)
        P(f"NOTE: {ts['dropped']} dropped samples -> data resampled to a uniform {fs:.1f} Hz grid")
        truth = None
        t_axis = tg
    else:
        t_axis = ts["t_s"] if ts else np.arange(len(v)) / fs

    q = quantization_stats(adc)
    if q:
        P("-- Quantisation --")
        P(f"1 LSB = {LSB_V * 1e3:.2f} mV; distinct codes {q['n_codes']} (span {q['span']}); "
          f"consecutive-equal {100 * q['frac_same']:.0f} %, +/-1 LSB {100 * q['frac_pm1']:.0f} %")
        P(f"fuzz around the 0.5 s median: {q['noise_lsb']:.2f} LSB rms "
          f"({'quantisation-limited' if q['noise_lsb'] < 0.6 else 'analog noise exceeds 1 LSB'})")

    P("-- Band RMS of raw voltage (mV) --")
    bands = [(0, 5)] + mains_bands(fs)
    edges = np.array([b for b in bands[1:]])
    rms = band_rms(v, fs, bands)
    total = float(np.std(v))
    P(f"0-5 Hz (signal)    : {rms[0] * 1e3:8.2f}")
    mains_sq = 0.0
    for (lo, hi), r in zip(bands[1:], rms[1:]):
        P(f"{(lo + hi) / 2:5.0f} Hz +/-2      : {r * 1e3:8.2f}")
        mains_sq += r ** 2
    other_sq = max(total ** 2 - rms[0] ** 2 - mains_sq, 0)
    P(f"everything else   : {np.sqrt(other_sq) * 1e3:8.2f}   (total std {total * 1e3:.2f})")

    filt = apply_filters(v, fs)
    P("-- Noise RMS (signal minus 0.5 s median), mV --")
    base = noise_rms(v, fs)
    P(f"{'raw':<18}: {base * 1e3:7.3f}")
    nrms = {}
    for name, y in filt.items():
        nrms[name] = noise_rms(y, fs)
        P(f"{name:<18}: {nrms[name] * 1e3:7.3f}   ({100 * (1 - nrms[name] / base):5.1f} % removed)")
    resid = {}
    P("-- Mains-band RMS after filtering (mV; 50/100/150/200 Hz +/-2) --")
    mb = mains_bands(fs)[:4]
    raw_mb = band_rms(v, fs, mb)
    P(f"{'raw':<18}: " + " ".join(f"{r * 1e3:7.3f}" for r in raw_mb))
    for name, y in filt.items():
        b = band_rms(y, fs, mb)
        resid[name] = b
        P(f"{name:<18}: " + " ".join(f"{r * 1e3:7.3f}" for r in b) +
          "   dB: " + " ".join(f"{20 * np.log10(max(r, 1e-12) / max(r0, 1e-12)):6.1f}" for r, r0 in zip(b, raw_mb)))
    # odd/even harmonic hint
    if len(raw_mb) >= 4:
        P("hint: mains pickup shows 50 AND 150 Hz; LED/power ripple aliased from 400 Hz shows "
          "100/200 Hz but little at 50/150 Hz.")
        p50, p100, p150 = raw_mb[0], raw_mb[1], raw_mb[2]
        if p100 > 3 * max(p50, p150):
            P("  -> 100 Hz dominates: suspect sensor LED current pulses / rail ripple, not (only) mains.")
        elif p50 > 3 * p100:
            P("  -> 50 Hz dominates: capacitive/body mains pickup.")

    if truth is not None:
        P("-- Error vs noise-free truth (mV rms) --")
        P(f"{'raw':<18}: {np.sqrt(np.mean((v - truth) ** 2)) * 1e3:7.3f}")
        for name, y in filt.items():
            P(f"{name:<18}: {np.sqrt(np.mean((y - truth) ** 2)) * 1e3:7.3f}")

    fid = fidelity(fs)
    P("-- Signal fidelity (noiseless synthetic tests at this fs) --")
    P("step response (10-90 % rise ms / overshoot % / ripple >150 ms after step %):")
    for name, s in fid["step"].items():
        P(f"  {name:<16}: {s['rise_ms']:7.1f} / {s['overshoot_pct']:5.2f} / {s['ripple_after_150ms_pct']:6.3f}")
    P("GSR response peak retained % (peak shift ms) for 10-90 rise time:")
    P(f"  {'':<16}  " + "  ".join(f"{r:>5g}s      " for r in fid["scr"]))
    for name in gf.FILTERS:
        P(f"  {name:<16}: " + "  ".join(
            f"{fid['scr'][r][name][0]:6.2f}({fid['scr'][r][name][1]:+.0f})" for r in fid["scr"]))
    P("edge error on 2 V + drift (max mV in first 0.5 s / last 0.5 s / interior):")
    for name, e in fid["edge"].items():
        P(f"  {name:<16}: {e[0]:7.3f} / {e[1]:7.3f} / {e[2]:7.3f}")

    P("-- Recommendation --")
    best = min(nrms, key=nrms.get)
    P(f"lowest residual noise: {best} ({nrms[best] * 1e3:.3f} mV rms vs raw {base * 1e3:.3f}).")
    P("Use clean_gsr(values, fs) with fs = estimate_fs(arduino_time_us): it removes every mains "
      "harmonic and everything above 5 Hz while keeping 0.3-5 s GSR responses (see fidelity).")
    if q and q["noise_lsb"] < 0.6:
        P("Residual fuzz is at the 1-LSB level: further gain needs a hardware change (oversampling, "
          "lower reference, gain) rather than more filtering.")
    return {"report": "\n".join(lines), "fs": fs, "v": v, "t": t_axis, "filt": filt,
            "ts": ts, "pc": pc, "adc": adc, "truth": truth, "fid": fid, "nrms": nrms,
            "base": base, "q": q, "resid": resid, "raw_mb": raw_mb}


# ----------------------------------------------------------------- figure ---

def make_figure(res: dict, title: str):
    import matplotlib.pyplot as plt

    v, t, fs, filt = res["v"], res["t"], res["fs"], res["filt"]
    fig = plt.figure(figsize=(19, 21), constrained_layout=True)
    gs = fig.add_gridspec(5, 3)
    fig.suptitle(title, fontsize=15)
    lw = 1.0

    # 1: full time domain (wide)
    ax = fig.add_subplot(gs[0, :2])
    ax.plot(t, v, color=COLORS["raw"], lw=0.6, label="raw")
    for name in ("notch_50 (old)", "clean_gsr"):
        ax.plot(t, filt[name], color=COLORS[name], lw=lw, label=name)
    ax.set(title="Time domain, full record", xlabel="time (s)", ylabel="V")
    ax.legend(loc="upper right", ncol=3, fontsize=9)
    ax.grid(alpha=0.25)

    # 2: zoom 2 s
    ax = fig.add_subplot(gs[0, 2])
    mid = len(v) // 2
    n2 = int(2 * fs)
    sl = slice(mid, min(mid + n2, len(v)))
    ax.plot(t[sl], v[sl], color=COLORS["raw"], lw=0.8, marker=".", ms=2, label="raw")
    for name in FILTER_ORDER:
        ax.plot(t[sl], filt[name][sl], color=COLORS[name], lw=lw, label=name)
    ax.set(title="Zoom: 2 s window", xlabel="time (s)", ylabel="V")
    ax.legend(fontsize=7, ncol=2, loc="upper right")
    ax.grid(alpha=0.25)

    # 3: PSD
    ax = fig.add_subplot(gs[1, 0])
    nper = int(min(len(v), 4096))
    for name, y in [("raw", v)] + list(filt.items()):
        f, p = signal.welch(y - np.mean(y), fs=fs, nperseg=nper, detrend="linear")
        ax.semilogy(f, p, color=COLORS[name], lw=1.0 if name != "raw" else 0.8, label=name)
    for k in range(1, int(fs / 2 // MAINS) + 1):
        ax.axvline(MAINS * k, color="k", ls=":", lw=0.7)
    ax.set(title="Welch PSD (dotted: 50 Hz harmonics)", xlabel="Hz", ylabel="V$^2$/Hz",
           xlim=(0, fs / 2))
    ax.legend(fontsize=7)
    ax.grid(alpha=0.25, which="both")

    # 4: spectrogram
    ax = fig.add_subplot(gs[1, 1])
    f, tt, S = signal.spectrogram(v - np.mean(v), fs=fs, nperseg=min(256, len(v) // 4),
                                  noverlap=None, detrend="linear")
    im = ax.pcolormesh(tt, f, 10 * np.log10(S + 1e-16), shading="auto", cmap="viridis")
    ax.set(title="Spectrogram of raw (dB re 1 V$^2$/Hz)", xlabel="time (s)", ylabel="Hz")
    fig.colorbar(im, ax=ax, pad=0.01)

    # 5: filter frequency responses
    ax = fig.add_subplot(gs[1, 2])
    fr = freq_responses(fs)
    for name in FILTER_ORDER:
        ax.plot(fr["f"], fr["H"][name], color=COLORS[name], lw=1.2, label=name)
    for k in range(1, int(fs / 2 // MAINS) + 1):
        ax.axvline(MAINS * k, color="k", ls=":", lw=0.7)
    ax.set(title="Filter power response (measured on white noise)", xlabel="Hz",
           ylabel="dB", xlim=(0, fs / 2), ylim=(-100, 5))
    ax.legend(fontsize=7)
    ax.grid(alpha=0.25)

    # 6: Arduino dt histogram
    ts = res["ts"]
    ax = fig.add_subplot(gs[2, 0])
    if ts:
        dt = ts["dt_us"]
        lo, hi = ts["median_us"] * 0.5, ts["median_us"] * 1.5
        ax.hist(np.clip(dt, 0, ts["median_us"] * 3), bins=120, color="#1f4e9c")
        ax.set_yscale("log")
        ax.set(title=f"Arduino sample intervals (median {ts['median_us']:.0f} us, "
                     f"fs {ts['fs']:.2f} Hz)", xlabel="dt (us), clipped at 3x median",
               ylabel="count")
        ax.text(0.98, 0.95, f"std {ts['std_us']:.0f} us\nmax {ts['max_us']:.0f} us\n"
                f"late {ts['late_n']}, dropped {ts['dropped']}", transform=ax.transAxes,
                ha="right", va="top", fontsize=9)
        del lo, hi
    else:
        ax.text(0.5, 0.5, "no arduino_time_us column", ha="center", transform=ax.transAxes)
    ax.grid(alpha=0.25)

    # 7: PC arrival histogram
    ax = fig.add_subplot(gs[2, 1])
    pc = res["pc"]
    if pc is not None and len(pc) > 2 and np.isfinite(pc).all():
        d = np.diff(pc) * 1e3
        ax.hist(np.clip(d, 0, 40), bins=120, color="#d95f02")
        ax.set_yscale("log")
        ax.set(title="PC arrival intervals of time_s (bursts => spikes at 0)",
               xlabel="interval (ms), clipped at 40", ylabel="count")
        ax.text(0.98, 0.95, f"std {d.std():.2f} ms\n{100 * np.mean(d < 0.5):.0f} % < 0.5 ms",
                transform=ax.transAxes, ha="right", va="top", fontsize=9)
    else:
        ax.text(0.5, 0.5, "no time_s column", ha="center", transform=ax.transAxes)
    ax.grid(alpha=0.25)

    # 8: ADC code histogram
    ax = fig.add_subplot(gs[2, 2])
    adc = res["adc"]
    if adc is not None:
        codes = adc[np.isfinite(adc)].astype(int)
        c0, c1 = codes.min(), codes.max()
        ax.bar(np.arange(c0, c1 + 1), np.bincount(codes - c0), width=1.0, color="#1b9e77")
        q = res["q"]
        ax.set(title=f"ADC code histogram ({q['n_codes']} codes, 1 LSB = {LSB_V * 1e3:.2f} mV)",
               xlabel="ADC code", ylabel="count")
    else:
        ax.text(0.5, 0.5, "no raw_adc column", ha="center", transform=ax.transAxes)
    ax.grid(alpha=0.25)

    # 9: noise RMS bars
    ax = fig.add_subplot(gs[3, 0])
    names = ["raw"] + FILTER_ORDER
    vals = [res["base"] * 1e3] + [res["nrms"][n] * 1e3 for n in FILTER_ORDER]
    bars = ax.barh(names, vals, color=[COLORS[n] for n in names])
    ax.bar_label(bars, fmt="%.3f", fontsize=8, padding=2)
    ax.set(title="Noise RMS (signal - 0.5 s median), mV", xlabel="mV rms")
    ax.set_xlim(0, max(vals) * 1.2)
    ax.invert_yaxis()
    ax.grid(alpha=0.25, axis="x")

    # 10: mains residual bars (dB)
    ax = fig.add_subplot(gs[3, 1])
    w = 0.14
    labels = ["50", "100", "150", "200"][:len(res["raw_mb"])]
    x = np.arange(len(labels))
    for i, name in enumerate(FILTER_ORDER):
        db = [20 * np.log10(max(r, 1e-12) / max(r0, 1e-12))
              for r, r0 in zip(res["resid"][name], res["raw_mb"])]
        ax.bar(x + (i - 2) * w, np.maximum(db, -80), width=w, color=COLORS[name], label=name)
    ax.set_xticks(x, [l + " Hz" for l in labels])
    ax.set(title="Mains band change vs raw (dB, floor -80)", ylabel="dB")
    ax.legend(fontsize=7, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=3)
    ax.grid(alpha=0.25, axis="y")

    # 11: step response
    ax = fig.add_subplot(gs[3, 2])
    fid = res["fid"]
    for name in FILTER_ORDER:
        tt_, y = fid["step_curves"][name]
        ax.plot(tt_ - 6, y, color=COLORS[name], lw=1.2, label=name)
    ax.set(title="Step response (ideal step at 0)", xlabel="time from step (s)", ylabel="output",
           xlim=(-0.3, 0.6))
    ax.legend(fontsize=7)
    ax.grid(alpha=0.25)

    # 12: GSR-like responses retained
    ax = fig.add_subplot(gs[4, 0])
    fs_ = fs
    tt = np.arange(0, 22, 1 / fs_)
    for rise, ls in ((0.3, ":"), (1.0, "--"), (5.0, "-")):
        ref = _scr(tt, rise, t0=2.0)
        ax.plot(tt, ref, color="k", lw=2.2, alpha=0.25, ls=ls)
        ax.plot(tt, gf.clean_gsr(ref, fs_), color=COLORS["clean_gsr"], lw=1.1, ls=ls,
                label=f"clean_gsr, rise {rise:g} s")
    ax.set(title="GSR responses: ideal (grey) vs clean_gsr (blue)", xlabel="time (s)",
           ylabel="normalised")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.25)

    # 13: edge error
    ax = fig.add_subplot(gs[4, 1])
    te = np.arange(0, 20, 1 / fs_)
    slow = 2.0 + 0.4 * np.sin(2 * np.pi * 0.05 * te) + 0.02 * te
    for name in FILTER_ORDER:
        ax.plot(te, (gf.FILTERS[name](slow, fs_) - slow) * 1e3, color=COLORS[name], lw=1.0,
                label=name)
    ax.axhline(LSB_V * 1e3, color="k", ls=":", lw=0.8)
    ax.axhline(-LSB_V * 1e3, color="k", ls=":", lw=0.8)
    ax.set(title="Edge test: output - input on 2 V + drift (dotted = 1 LSB)",
           xlabel="time (s)", ylabel="mV")
    ax.legend(fontsize=7)
    ax.grid(alpha=0.25)

    # 14: text summary
    ax = fig.add_subplot(gs[4, 2])
    ax.axis("off")
    summary = ["Summary (mV rms noise):", f"  raw: {res['base'] * 1e3:.3f}"]
    summary += [f"  {n}: {res['nrms'][n] * 1e3:.3f}" for n in FILTER_ORDER]
    if ts:
        summary += ["", f"fs measured: {ts['fs']:.3f} Hz",
                    f"dt std: {ts['std_us']:.0f} us, dropped: {ts['dropped']}"]
    ax.text(0.0, 1.0, "\n".join(summary), va="top", family="monospace", fontsize=10)
    return fig


# --------------------------------------------------------------- selftest ---

def synth(seconds=60.0, fs=500.0, seed=3):
    """Synthetic recording as the Arduino would deliver it.

    Returns (data dict, noise-free truth array).  Injected: GSR drift + 4 SCR
    events, mains 50.15 Hz with harmonics 1..10 (9th aliases onto 50 Hz, the
    10th at 501.5 Hz aliases to 1.5 Hz!), 400 Hz rail ripple (aliases to
    100 Hz), white noise, 10-bit quantisation, timestamp jitter, rare 1-3 ms
    stalls, PC-side burst arrival times, and a micros() wrap.
    """
    rng = np.random.default_rng(seed)
    n = int(seconds * fs)
    sched = np.arange(n) * 2000.0
    jitter = np.floor(rng.exponential(18.0, n) / 4) * 4
    stalls = rng.random(n) < 0.002
    jitter[stalls] += rng.uniform(1200, 3000, stalls.sum())
    actual = sched + jitter
    for i in range(1, n):  # a late sample pushes the next one (print+read >= 150 us)
        actual[i] = max(actual[i], actual[i - 1] + 150)
    t = actual / 1e6
    slow = 1.25 + 0.05 * np.sin(2 * np.pi * 0.01 * t) - 0.002 * t
    for t0, amp, rise in ((8, 0.12, 1.0), (20, 0.08, 2.0), (33, 0.15, 3.0), (47, 0.10, 5.0)):
        slow += amp * _scr(t, rise, t0=t0)
    f_mains = 50.15
    amps = {1: 0.040, 2: 0.012, 3: 0.015, 4: 0.006, 5: 0.008, 6: 0.003, 7: 0.004,
            8: 0.002, 9: 0.003, 10: 0.002}
    mains = sum(a * np.sin(2 * np.pi * k * f_mains * t + k) for k, a in amps.items())
    ripple = 0.006 * np.sin(2 * np.pi * 400.0 * t + 0.3)
    white = rng.normal(0, 0.8 * LSB_V, n)
    truth = slow
    adc = np.clip(np.round((slow + mains + ripple + white) / LSB_V), 0, ADC_MAX)
    wrap_start = (1 << 32) - int(18e6)  # wraps 18 s into the record
    arduino_us = ((wrap_start + np.round(actual)).astype(np.int64)) % (1 << 32)
    pc = np.ceil(actual / 16000.0) * 16000.0 / 1e6 + rng.uniform(0, 0.0004, n)
    pc = np.maximum.accumulate(pc)
    data = {"raw_adc": adc, "voltage": adc * LSB_V, "arduino_us": arduino_us, "pc_time_s": pc}
    return data, truth


def selftest(save: str | None, show: bool) -> int:
    ok = True
    # unit checks of the helpers
    t = ((1 << 32) - 4000 + np.arange(0, 20000, 2000)) % (1 << 32)
    fs_chk = gf.estimate_fs(t)
    wrap_ok = abs(fs_chk - 500.0) < 1e-6 and np.all(np.diff(gf.unwrap_micros(t)) == 2000)
    print(f"[check] estimate_fs across a micros() wrap = {fs_chk:.3f} Hz -> {'OK' if wrap_ok else 'FAIL'}")
    ok &= bool(wrap_ok)
    gap = np.cumsum(np.r_[0, [2000] * 10, [12000], [2000] * 10])
    ts_gap = timing_stats(gap)
    print(f"[check] dropped-sample counter on a 5-sample gap = {ts_gap['dropped']} (expect 5)")
    ok &= ts_gap["dropped"] == 5
    short = np.arange(5.0)
    short_ok = all(np.array_equal(fn(short, 500.0), short) for fn in gf.FILTERS.values())
    print(f"[check] short inputs returned unchanged -> {'OK' if short_ok else 'FAIL'}")
    ok &= short_ok

    data, truth = synth()
    res = analyze(data, truth=truth, label="SELFTEST synthetic (50.15 Hz mains + harmonics, 400 Hz ripple)")
    print(res["report"])

    # injected-noise removal table (the numbers the recommendation rests on)
    v, filt, fs = res["v"], res["filt"], res["fs"]
    print("\n=== SELFTEST: injected-noise removal (error = output - noise-free truth) ===")
    raw_err = float(np.sqrt(np.mean((v - truth) ** 2)))
    print(f"{'filter':<18}{'err mV':>9}{'removed %':>11}  {'50Hz':>7}{'100Hz':>7}{'150Hz':>7}{'200Hz':>7}  (dB re raw)")
    summary = {}
    for name, y in filt.items():
        err = float(np.sqrt(np.mean((y - truth) ** 2)))
        db = [20 * np.log10(max(r, 1e-12) / max(r0, 1e-12))
              for r, r0 in zip(res["resid"][name], res["raw_mb"])]
        summary[name] = (err, db)
        print(f"{name:<18}{err * 1e3:9.3f}{100 * (1 - err / raw_err):11.1f}  "
              + "".join(f"{d:7.1f}" for d in db))
    print(f"{'raw':<18}{raw_err * 1e3:9.3f}")
    # floor: the aliased 501.5 Hz (->1.5 Hz) tone + quantisation cannot be removed in software
    alias_rms = 0.002 / np.sqrt(2) * 1e3
    print(f"floor note: the 10th mains harmonic (501.5 Hz) aliases to 1.5 Hz at {alias_rms:.2f} mV rms; "
          "no digital filter can remove it without also removing GSR (needs an analog RC before A0).")

    # pass/fail assertions
    best_old = summary["notch_50 (old)"][0]
    cl = summary["clean_gsr"][0]
    checks = [
        ("clean_gsr error < 0.5 x old notch error", cl < 0.5 * best_old),
        ("clean_gsr removes >= 40 dB at 50/100/150/200 Hz", all(d <= -40 for d in summary["clean_gsr"][1])),
        ("old notch leaves >= 20 dB less removal at 100 Hz than clean_gsr",
         summary["notch_50 (old)"][1][1] - summary["clean_gsr"][1][1] >= 20),
        ("clean_gsr keeps >=98 % of a 1 s-rise GSR peak",
         res["fid"]["scr"][1.0]["clean_gsr"][0] >= 98),
        ("clean_gsr keeps >=99 % of a 2 s-rise GSR peak",
         res["fid"]["scr"][2.0]["clean_gsr"][0] >= 99),
        ("clean_gsr edge error < 1 LSB",
         max(res["fid"]["edge"]["clean_gsr"][:2]) < LSB_V * 1e3),
    ]
    print("\n=== SELFTEST assertions ===")
    for text, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {text}")
        ok &= bool(passed)

    import matplotlib.pyplot as plt
    fig = make_figure(res, "GSR noise diagnostics - SELFTEST (synthetic data)")
    if save:
        fig.savefig(save, dpi=80)
        print(f"\nfigure saved to {save}")
    if show:
        plt.show()
    print("SELFTEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


# ------------------------------------------------------------------- main ---

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", help="voltage CSV written by polygraph_app.py")
    ap.add_argument("--save", metavar="PNG", help="save the figure instead of only showing it")
    ap.add_argument("--show", action="store_true", help="open the window even with --save")
    ap.add_argument("--selftest", action="store_true", help="run on synthetic data and assert results")
    ap.add_argument("--port", help="serial port for a live capture (e.g. COM5)")
    ap.add_argument("--seconds", type=float, default=30.0, help="live capture length")
    args = ap.parse_args(argv)

    import matplotlib
    headless = args.selftest or (args.save and not args.show)
    if headless:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if args.selftest:
        return selftest(args.save, args.show)
    if args.port:
        print(f"capturing {args.seconds:g} s from {args.port} ...")
        data = capture_serial(args.port, args.seconds)
        label = f"live {args.port}"
    elif args.csv:
        data = load_csv(args.csv)
        label = Path(args.csv).name
    else:
        ap.error("give a CSV file, --port, or --selftest")
        return 2

    res = analyze(data, label=label)
    print(res["report"])
    if "filt" not in res:
        return 1
    fig = make_figure(res, f"GSR noise diagnostics - {label}")
    if args.save:
        fig.savefig(args.save, dpi=80)
        print(f"figure saved to {args.save}")
    if not headless or args.show:
        plt.show()
    return 0


if __name__ == "__main__":
    sys.exit(main())
