# GSR noise analysis (todo item 4)

Question: the code "filters 50 Hz" but the data still looks noisy. Why, and what do we do?

Line numbers refer to the files as of commit `6ee6879` (`app/gsr_polygraph.py`, `checking_GSR/קוד/checking_GSR/checking_GSR.ino`); the app is being edited, so search by the quoted symbol if lines have moved.

Tools: `app/gsr_filters.py` (filters), `app/gsr_diagnostics.py` (analysis).
All numbers below come from `python app/gsr_diagnostics.py --selftest` (60 s of synthetic data, 500 Hz, 10-bit, 50.15 Hz mains with harmonics 1-10 of 40/12/15/6/8/3/4/2/3/2 mV, 6 mV 400 Hz rail ripple, 0.8 LSB white noise, timestamp jitter plus rare 1-3 ms stalls, one `micros()` wrap). They are model results, not measurements of your hardware; confirm each finding on a real recording with the "Confirm" step.

## Findings, ranked by likely impact

### 1. Only 50 Hz is notched; mains has harmonics, and they are most of the noise
Evidence: `gsr_polygraph.py:129-151` (`apply_50hz_notch_filter`: a single `iirnotch(w0=50, Q=30)`, constants at lines 32-33). At fs = 500 Hz the 100/150/200/250 Hz harmonics are in band, and 350/450/550... alias onto 150/50/50... Hz. The notch is also only ~1.7 Hz wide (50/Q), and mains wanders about +/-0.2 Hz, and the Arduino's resonator is typically 0.1-0.5 % off, so the tone can sit outside even that.
Selftest (mains-band RMS change vs raw): old notch -24 dB at 50 Hz, **0 dB at 100/150/200 Hz**; noise RMS only 51 % removed (34.7 -> 17.0 mV).
Confirm: PSD panel and the "Mains-band RMS" table (peaks at 100/150/200 Hz survive the `notch_50 (old)` trace).
Fix (software): replace the notch with `clean_gsr` (below).

### 2. No low-pass: GSR lives below ~5 Hz but the app keeps 0-250 Hz of noise
Evidence: nothing in the app limits bandwidth; `save_files` (line 668) and `plot_examination` (line 185) apply only the notch. The signal's useful content is < 5 Hz (the MATLAB script itself ends up using a 1-2 s moving average, `gsr_checking.m:230-240, 287-292`, and only looks for FFT peaks below 100 Hz, lines 108-112). In the selftest the 0-5 Hz band holds 18 mV rms of signal while everything above 5 Hz holds ~59 mV rms of noise, i.e. >90 % of the visible fuzz is removable.
Selftest: low-pass 5 Hz leaves 0.78 mV noise (97.7 % removed) vs 16.98 mV for the old notch.
Confirm: "Noise RMS" bar chart and "everything else" row of the band report.
Fix: `lowpass` / `clean_gsr`.

### 3. Time base: the app timestamps samples with PC arrival time (bursty), so the trace looks jagged
Evidence: `gsr_polygraph.py:365` (`time.monotonic()` at read time) and `:546` (`elapsed = received_at - recording_started_at`) are used as `time_s` while the true time `arduino_time_us` is stored but ignored for plotting. USB-serial chips and the Windows driver deliver data in chunks (CH340/FTDI latency timer, typically 1-16 ms), so ~8 samples share almost the same x. The selftest model shows 87 % of PC intervals < 0.5 ms and a second cluster at 16 ms (std 5.2 ms vs 0.13 ms from the Arduino). A plot against this x draws a sawtooth-looking line even for a clean signal, and any x-based analysis (FFT with `mean(diff(time))`, as in `gsr_checking.m:94, 174`) is wrong. Task B fixes `time_s`; this is documented here for completeness.
Confirm: "PC arrival intervals" histogram (should be one tight spike at 2 ms, not 0 ms + 16 ms) and the report line `PC arrival dt std`.
Fix: use `arduino_time_us` (unwrap the 32-bit wrap, `gsr_filters.unwrap_micros`) and `estimate_fs`.

### 4. Sampling jitter and stalls, especially with the MAX30102 on the same Arduino
Evidence: firmware `checking_GSR.ino:12-21`: the timestamp is `micros()` read at loop entry, before `analogRead` (~112 us conversion at the default 125 kHz ADC clock), then `Serial.print` x3 (17 bytes = 1.5 ms of the 2 ms slot at 115200). The UART TX buffer is only 64 bytes, so `Serial.println` blocks whenever it is full and any other work (I2C FIFO reads of the MAX30102 at 400 kHz, ~1 ms each) pushes samples late. `previousMicros += sampleInterval` (line 15) keeps the average rate exact but turns a stall into a burst of back-to-back samples afterwards. Timing error converts to amplitude error on any fast component: `error = 2*pi*f*dt*A`; for the 50 Hz tone, 100 us of jitter is 3 % of its amplitude (99th percentile in the selftest), and a 2 ms stall is a 60 % error on that sample. Every filter in `gsr_filters.py` assumes a uniform grid.
Confirm: "Arduino sample intervals" histogram: tight peak at 2000 us, tail below ~500 us / above 3000 us means stalls and catch-up; report line `late` and `dropped`.
Fix: firmware (task A): resync after long stalls, drain the MAX30102 one sample per loop; software: `resample_uniform` is applied automatically by the diagnostics if samples were dropped.

### 5. Supply / reference noise and MAX30102 LED current pulses coupling into the ADC
Evidence: `gsr_polygraph.py:29` (`ADC_REFERENCE_VOLTS = 5.0`, i.e. AVcc = USB 5 V) and the sketch uses the default reference, so USB-supply noise is both in the reference and (if the GSR module is powered from 5 V) in the sensor. The MAX30102 draws LED current pulses (up to 50 mA) at its sample rate; at 400 Hz this is sampled at 500 Hz and aliases to |400-500| = **100 Hz** (and 800 -> 200 Hz). That is exactly the mains 2nd/4th harmonic bins, so a tall 100 Hz line does not prove it is mains.
Telling them apart: mains pickup shows 50 and 150 Hz; LED/rail ripple shows 100/200 Hz with little at 50/150. The report prints a hint (`100 Hz dominates` vs `50 Hz dominates`). Definitive test: capture once with the MAX30102 unplugged (or LED current set to 0) and once with it running.
Selftest: with a 6 mV 400 Hz ripple the injected energy is removed by the same filters (software can remove it; hardware should still reduce it).
Fix (hardware): see list below.

### 6. Mains harmonics alias into the GSR band and cannot be filtered in software
Evidence: no analog anti-alias filter before A0 (the Uno ADC samples with a bare sample-and-hold). A component at k*500 Hz + d aliases to d Hz. The 10th harmonic of mains (501.5 Hz at 50.15 Hz mains) lands at 1.5 Hz, in the middle of the GSR band; the same happens to switching-charger noise. In the selftest a 2 mV 10th harmonic leaves 1.41 mV rms after **every** filter (it is the whole floor of the "removed" column), see the `floor note`.
Confirm: a narrow line in the PSD / spectrogram below 5 Hz that moves with mains drift (e.g. 1.5 Hz now, 0.5 Hz later) and disappears on battery power.
Fix (hardware only): RC low-pass before A0 (below).

### 7. Quantisation: 10 bits, 4.89 mV per LSB
Evidence: `gsr_polygraph.py:29-30` (`ADC_MAX = 1023`, 5 V -> 4.89 mV/LSB); `checking_GSR.ino:17` one `analogRead` per sample, no averaging. The MATLAB data uses ylim 0-2 V (`gsr_checking.m:63`), so the signal uses less than half of the range, ~400 codes, and GSR responses of 10-100 mV are only 2-20 codes. If the analog noise is below ~0.5 LSB the trace toggles between two codes, which looks like noise but is deterministic; averaging does not help in that case (no dither), but it does when noise >= 1 LSB (random noise averages down by sqrt(N)).
Confirm: ADC code histogram (few distinct codes / gaps) and the report line `fuzz around the 0.5 s median: ... LSB rms`; < 0.6 LSB means quantisation-limited.
Fix: the 5 Hz low-pass already averages ~50 samples (it effectively adds ~2.5 bits for noise-like data); in hardware use a gain stage or `analogReference(INTERNAL)` (1.1 V, 1.07 mV/LSB) if the signal is kept below 1.1 V (a divider can bring it there), and/or 4x oversample inside each 2 ms slot (4 x 112 us).

### 8. Analog front end: source impedance, electrode/motion artefacts
Not mains, but they look like noise. Skin/electrode contact is a megohm-range source and the Uno ADC wants < 10 kOhm; unbuffered, the ADC input sample-and-hold droop and cable pick-up (the body is an antenna for 50 Hz) both rise. Electrode pressure and movement cause slow steps and spikes (visible in `gsr_checking.m:34-39`: the "little pressure" and "no sleeve" recordings were already compared; the 2 s/1 s moving averages at lines 230-240 and 287-292 and the FFT/amplitude table at lines 98-148 are what has been tried so far: they show the energy is mostly at low frequency, but the FFT view stops at 100 Hz, so the 150/200/250 Hz lines and the 100 Hz-vs-50 Hz question were never examined, and no mains-specific filter was tried).
Confirm: spikes/steps that scale with touching the cable or the electrodes, and are not periodic.
Fix: shielded/twisted cable, buffered front end (op-amp follower or the module's own buffer), firm electrodes; no filter should be asked to remove step artefacts (a median/Hampel despike is the right tool, and a step cannot be distinguished from a real GSR onset by frequency).

### 9. Other things found in the code
- `gsr_polygraph.py:138-142` hard-codes `SAMPLE_RATE_HZ = 500` (line 31); the notch is tuned for the nominal rate, not the measured one (use `estimate_fs`).
- `gsr_polygraph.py:146-148` / `filtfilt` default `padlen` is only ~9 samples, so the first/last fraction of a second of a recording is poorly conditioned for a 1.7 Hz-wide filter (ring time Q/(pi*f0) ~ 0.19 s). The selftest step response shows the old notch rings ~0.2 s after any sharp edge (ripple 0.75 % after 150 ms); this is why contact steps show 50 Hz "ringing".
- `gsr_polygraph.py:171-185` `plot_examination` re-filters old files without the column, but plots against PC-time `time_s` (finding 3).
- `checking_GSR.ino:19` sends `now` (loop time) not the actual conversion time; the 4-100 us difference is within finding 4.
- `checking_GSR.ino:3,15`: a stalled loop never resyncs; after a long stall the loop emits samples back-to-back (dt much less than 2000 us in the histogram) until it has caught up. Each timestamp is still the real `micros()`, so the data are correct per sample but not uniformly spaced.

## How to confirm everything on real data

```
python app/gsr_diagnostics.py app/measurements/<name>_voltage.csv --save out.png
python app/gsr_diagnostics.py --port COM5 --seconds 30 --save live.png
```
Do four captures of 30 s each and compare the report: (a) electrodes on the hand, MAX30102 running; (b) same with MAX30102 unplugged; (c) electrodes shorted together with a resistor (e.g. 100 kOhm, hand away from the cable); (d) laptop on battery. The difference (a)-(b) is the LED/rail coupling; (c) is the electronic floor; (d) is the ground-loop part.

## Hardware mitigations
- Star-point ground: one GND return from the GSR module, the MAX30102 and the Arduino meeting at the Arduino GND pin; keep the GSR analog wires away from the I2C wires.
- 100 nF + 10 uF decoupling at each sensor's supply pins, and 100 nF from AREF to GND (AVcc remains the reference unless you set an external one).
- **RC low-pass in front of A0** (e.g. 1 kOhm + 1 uF, ~160 Hz corner, or 10 kOhm + 1 uF if the GSR module output is buffered and you can afford ~16 Hz). This is the only fix for aliased components (finding 6). Add a 100 nF directly at the pin.
- Run the laptop on battery (or an isolated USB hub) while measuring; mains-powered laptop chargers inject common-mode 50 Hz and ground loops.
- Lower `LED_BRIGHTNESS` of the MAX30102 as far as the BPM lock allows (the LED current is the pulse load); power the MAX30102 from its own 3.3 V regulator, not the 5 V USB rail.
- Shielded/twisted GSR cable, shield tied to GND at one end.
- Optional firmware: oversample 4x per slot, `analogRead` twice and keep the second (settling), `analogReference(INTERNAL)` if the signal fits.

## Selftest results (all filters, 60 s synthetic record)

| filter | noise RMS (mV) | error vs truth (mV) | 50 Hz | 100 Hz | 150 Hz | 200 Hz | step overshoot | peak kept, 1 s / 2 s rise |
|---|---|---|---|---|---|---|---|---|
| raw | 34.7 | 33.0 | | | | | | |
| notch_50 (old) | 17.0 | 17.0 (48 % removed) | -24 dB | 0 | 0 | 0 | 1.6 % | 100 / 100 % |
| comb_notch | 7.4 | 7.5 (77 %) | -24 | -24 | -26 | -27 | 4.0 % | 100 / 100 % |
| moving_avg_20ms | 1.55 | 2.07 (94 %) | -49 | -51 | -53 | -57 | 0 % | 100 / 100 % |
| lowpass_5Hz (order 4) | 0.78 | 1.96 (94 %) | -153 | -162 | -174 | -177 | 6.8 % | 100 / 100 % |
| **clean_gsr** | **0.75** | **1.91 (94 %)** | **-130** | **-158** | **-174** | **-177** | **3.3 %** | **100 / 100 %** |

"Error vs truth" has a 1.4 mV floor that no filter can remove (the aliased 10th harmonic, finding 6, plus quantisation); that is why 94 % and not 100 %. dB columns are band RMS (+/-2 Hz) after filtering relative to raw.

Distortion checks (noiseless, synthetic): GSR responses with 10-90 % rise times of 0.3, 1, 2 and 5 s keep 100.0 % of their peak amplitude with 0 ms peak shift in `clean_gsr`; edge error on a 2 V level with slow drift is < 0.01 mV in the first and last 0.5 s (1 LSB = 4.9 mV) thanks to odd-reflection padding in the moving average and a 1 s `padlen` in the zero-phase low-pass; the ideal step rise is 46 ms (10-90 %) with 3.3 % overshoot, which is only visible on non-physical steps (electrode contact jumps).

## Recommendation for the integrating agent (task C)

Use **`clean_gsr`**: moving average exactly one mains period long (nulls 50 Hz and every harmonic, tolerant of clock error because the window length is `fs/50` with fractional end weights) followed by a zero-phase 2nd-order Butterworth low-pass at 5 Hz (effective 4th order). Both stages are zero-phase, so GSR onsets are not shifted relative to question markers. Why this over the alternatives:
- The old notch removes only 24 dB at 50 Hz and nothing at the harmonics (finding 1).
- The comb notch removes ~25 dB everywhere but leaves 7.4 mV noise and rings (4 % overshoot).
- The 20 ms moving average alone gives -50 dB at every harmonic but nothing for noise between the nulls (1.55 mV).
- The low-pass alone reaches -150 dB at the mains lines and is the main noise remover; the moving average stage costs nothing and protects against a low-pass cutoff being raised later and against mains frequency drift.
- Fidelity is unchanged for 0.3-5 s GSR responses (table above).

```python
from gsr_filters import clean_gsr, estimate_fs

def clean_gsr(values, fs: float) -> np.ndarray   # values: 1-D array-like, fs in Hz
def estimate_fs(arduino_time_us) -> float         # handles the 32-bit micros() wrap, median based
```
Integration notes:
- `values` is the voltage (or ADC) series in sample order; the result has the same length, dtype float64, same units (DC level preserved). Inputs shorter than 30 samples are returned unchanged.
- Pass `fs = estimate_fs(arduino_time_us)` if it is non-zero and within 1 % of 500, else 500.0.
- If samples were dropped (gaps in `arduino_time_us`), first `t, v = resample_uniform(time_s_from_arduino, values, fs)`, since the filters assume a uniform grid.
- Keep the CSV column `filtered_voltage_v`; call `clean_gsr(np.asarray(voltages), fs)` instead of `apply_50hz_notch_filter(...)` at `save_files` and in `plot_examination`'s fallback. The app imports `gsr_filters` from the same folder (`app/`).
- Software cannot fix finding 6 (aliased components) or electrode artefacts; those need the hardware changes above.
