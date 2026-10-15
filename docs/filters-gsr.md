# GSR filtering

Source of truth: [python_app/gsr_filters.py](../python_app/gsr_filters.py). The investigation behind these choices, with spectra, a synthetic self-test, and a comparison of all candidate filters, is in [python_app/NOISE_ANALYSIS.md](../python_app/NOISE_ANALYSIS.md) and is not repeated here.

**In short:** a skin-conductance reaction is slow, taking seconds. The interference on the wire is fast: mains hum at 50 Hz and its multiples, LED flicker from the heart-rate sensor, and random noise. The app keeps everything slower than about 5 Hz and removes everything faster, without shifting the reactions in time.

## Where filtering happens

| Place | What | When |
|---|---|---|
| Firmware | **None.** `raw_adc` is sent as read. | — |
| `records.py` `save_examination()` | `gsr_filters.clean_recording(voltages, 500.0, arduino_time_us)` → `filtered_voltage_v` column | Once, at save time, over the whole recording |
| `plotting.py` `plot_examination()` | Same call, only if an older file lacks `filtered_voltage_v` | When plotting |
| `gsr_diagnostics.py` | All filters in the `FILTERS` registry, for comparison | Manual analysis tool, not used by the app |

The app has no live GSR plot, so nothing is filtered in real time. The raw `voltage_v` is always saved next to the filtered column.

## The chain used by the app

```
voltage_v ──> [measure fs, resample if gaps] ──> moving average, 1 mains period (20 ms)
          ──> Butterworth low-pass 5 Hz, order 2, forward + backward ──> filtered_voltage_v
```

`clean_recording()` → `clean_gsr()` → `moving_average_mains()` then `lowpass(cutoff_hz=5.0, order=2)`.

### Gaps and sample rate

`clean_recording(voltage_values, nominal_fs, arduino_time_us)`

| Step | Detail | Constant |
|---|---|---|
| Measure fs | `estimate_fs()` = 1e6 / median(Δ`arduino_time_us`) after unwrapping. The median ignores stalls and gaps. | |
| Accept measured fs | Only if within 1 % of the nominal 500 Hz, otherwise use 500.0 | `FS_TOLERANCE = 0.01` |
| Detect gaps | Any interval longer than 1.5 sample periods means samples were dropped (firmware resync) | `GAP_FACTOR = 1.5` |
| Resample | If there are gaps (and all steps are positive), linearly interpolate onto a uniform grid (`resample_uniform()`), filter, then interpolate back to the original sample times | |
| Too short | Fewer than 30 samples, or fs ≤ 0: returned unchanged | `_MIN_LEN = 30` |

*Why:* the filters assume equally spaced samples. The moving-average length and the low-pass coefficients are both set in samples, so a 2 ms tick that really took 12 ms would distort them. Using the measured rate keeps the mains nulls on the real mains frequency even though the Arduino's resonator is a few tenths of a percent off. Interpolating back keeps exactly one output per input row, so `filtered_voltage_v` lines up with `voltage_v` in the CSV.

### Stage 1: mains-cancelling moving average (`moving_average_mains`)

| Parameter | Value |
|---|---|
| Window length | `fs / f0` = 500 / 50 = **10 samples = 20 ms**, one mains period (`MAINS_HZ = 50.0`) |
| Kernel | Centered and symmetric. With fs = 500 Hz: 9 taps of weight 1 plus 0.5 at each end, normalised. A non-integer `fs/f0` (measured fs) gives fractional end weights, so the window is always exactly one period long. |
| Edges | Odd reflection padding (`np.pad(..., mode="reflect", reflect_type="odd")`), so slow levels and slopes at the start and end are not pulled toward zero |
| Phase | Zero (symmetric kernel) |

*Intuition:* averaging over exactly one hum cycle adds up one full up-swing and one full down-swing, which cancel. The same happens for 2, 3, 4 … cycles, so the average also cancels 100, 150, 200 and 250 Hz. It also cancels harmonics above 250 Hz that alias back onto multiples of 50 Hz at a 500 Hz sample rate.

*Technical:* a boxcar of length N = fs/f0 has a frequency response `sin(πfN/fs) / (N·sin(πf/fs))` with exact nulls at every multiple of f0. Measured on the synthetic self-test it removes about 50 dB at 50/100/150/200 Hz. At 5 Hz the loss is only about 0.14 dB, so it doesn't touch the GSR band. It replaces the original single 50 Hz notch (`notch_50`, Q = 30, about 1.7 Hz wide), which removed only 50 Hz itself and missed the tone whenever mains or the Arduino clock drifted.

On its own it leaves the noise *between* the nulls, which is why stage 2 follows. It is kept because it costs nothing, has no overshoot, and still protects against mains if the low-pass cutoff is ever raised.

### Stage 2: zero-phase Butterworth low-pass (`lowpass`)

| Parameter | Value |
|---|---|
| Type | Butterworth, second-order sections (`signal.butter(order, cutoff_hz, btype="low", fs=fs, output="sos")`) |
| Cutoff | **5 Hz** (`cutoff_hz=5.0` in `clean_gsr`) |
| Order | **2** per pass. Applied forward and backward (`sosfiltfilt`), so the effective order is 4 (−24 dB/octave). |
| Attenuation | 5 Hz: −6 dB (each pass is −3 dB there, and the two passes add). 50 Hz: about −80 dB. |
| Edge padding | `padlen = min(n − 1, fs)`, i.e. 1 s, odd extension (scipy default), so slow GSR levels don't create start/end transients |
| Phase | Zero |

*Intuition:* it acts as a smoothing filter that ignores anything faster than about five wiggles per second. A skin-conductance response takes 1–3 s to rise and several seconds to recover, which is far slower than 5 Hz, so its shape passes through intact. The fuzz on top of it is removed.

*Why 5 Hz:* the physiologically meaningful content of skin conductance is below about 1–2 Hz. Phasic responses (SCRs) have rise times of roughly 0.5–3 s, and the tonic level changes over tens of seconds. A 5 Hz cutoff leaves a wide safety margin: the self-test shows 100 % of the peak kept, with 0 ms peak shift, for rise times of 0.3, 1, 2 and 5 s. Everything above 5 Hz is noise for this measurement: mains and its harmonics, the 100 Hz alias of the MAX30102's 400 Hz LED pulses, and white and quantisation noise. In the self-test the 5 Hz low-pass removes about 98 % of the noise RMS (34.7 mV down to 0.78 mV). It also averages about 50 samples, which reduces quantisation steps (4.89 mV per count) on noisy data.

*Why order 2 and not 4:* at the same cutoff, order 2 halves the step overshoot (3.4 % vs 6.8 %). Overshoot appears as a small ringing artefact on sharp electrode-contact jumps, which order 2 keeps small. Mains rejection is still about −80 dB.

*Why zero-phase (forward-backward):* a normal causal filter delays its output. A 5 Hz low-pass would shift GSR onsets later by tens of ms, more for slower content, by different amounts at different frequencies. Running the filter forward and then backward cancels the delay exactly, so a response stays at the same position relative to the question markers in `*_questions.csv`. The cost is that the filter can't run in real time, because it needs the whole recording. That is fine here, since filtering only happens at save time.

## Trade-offs and limits

- **Step response.** An ideal step rises in about 92 ms (10–90 %, measured on `clean_gsr` at 500 Hz) with 3.3 % overshoot. NOISE_ANALYSIS.md says 46 ms, which is probably the count in samples (46 samples × 2 ms = 92 ms). Only non-physical steps, such as electrode contact being made or broken, show this. Real GSR changes are far slower.
- **Zero-phase smears slightly backwards in time.** The impulse response is symmetric and spans about ±0.2 s (to the 1 % level), so a sharp artefact also shows up a little *before* it happens. That is negligible for GSR, but keep it in mind when timing very sharp events.
- **Cannot remove** components that alias into the 0–5 Hz band (for example the 10th mains harmonic at 501.5 Hz aliasing to 1.5 Hz, because there is no analog anti-alias filter in front of `A0`), electrode motion artefacts, or steps. Those need the hardware fixes listed in [NOISE_ANALYSIS.md](../python_app/NOISE_ANALYSIS.md) ("Hardware mitigations"): an RC low-pass before `A0`, star grounding, decoupling, running on battery, and lower LED current.
- **50 Hz mains is assumed** (`MAINS_HZ = 50.0`, Israel and Europe). In a 60 Hz country, change `MAINS_HZ`. The moving average would then be 8.33 samples long, which the fractional-weight kernel supports.

## Filters kept for comparison only

These are in `gsr_filters.FILTERS` for `gsr_diagnostics.py`. The app doesn't use them.

| Filter | What it is | Why it's not used |
|---|---|---|
| `notch_50` | Single IIR notch at 50 Hz, Q = 30, `filtfilt`. This was the original app filter. | Leaves 100/150/200 Hz untouched. It is only about 1.7 Hz wide, so mains drift defeats it. Removes about 51 % of the noise. |
| `comb_notch_mains` | Cascade of Q = 30 notches at every multiple of 50 Hz below 0.98 × Nyquist, `sosfiltfilt` | About −25 dB at each harmonic but leaves noise between them (7.4 mV in the self-test), and 4 % overshoot |
| `moving_average_mains` alone | Stage 1 above | Nothing between the nulls (1.55 mV left) |
| `lowpass` with order 4 | Stage 2 with order 4 | Same noise removal as order 2 but twice the overshoot (6.8 %) |

See the selftest table in [NOISE_ANALYSIS.md](../python_app/NOISE_ANALYSIS.md#selftest-results-all-filters-60-s-synthetic-record) for the numbers.
