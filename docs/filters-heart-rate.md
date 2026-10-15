# Heart-rate detection and filtering

Source of truth: [arduino/polygraph/polygraph.ino](../arduino/polygraph/polygraph.ino) (`processSample()`, `handlePeak()`, `Biquad`, constants at the top of the file). The algorithm and every constant are ported unchanged from [arduino/legacy/bpm_detector/bpm_detector.ino](../arduino/legacy/bpm_detector/bpm_detector.ino). Its README has a user-oriented description and a troubleshooting table.

**In short:** the fingertip sensor sees a steady glow with a tiny dip on every heartbeat. The Arduino removes the steady part and the slow drifts, keeps only wiggles at heartbeat speed, and counts a beat at each peak. It then checks that the gaps between beats are believable and reports the typical gap as beats per minute. Until it has seen several consistent beats it reports 0 rather than guess.

All heart-rate processing happens **on the Arduino**. The app saves and shows the result but does no filtering of its own (see [What the app does with BPM](#what-the-app-does-with-bpm)).

## Pipeline

```
MAX30102 IR (100 Hz, 18-bit)
  -> finger detection (raw IR < 50000 -> NO_FINGER)
  -> high-pass biquad 0.7 Hz -> low-pass biquad 3.5 Hz        (band-pass)
  -> invert (sig = -filtered)                                   (pulse becomes a peak)
  -> adaptive threshold = 0.5 x peak envelope
  -> local-maximum peak detector + 300 ms refractory period
  -> beat-interval (IBI) validation vs median of recent IBIs
  -> BPM = 60000 / median(last <= 8 IBIs), reported only when LOCKED
```

## 1. Sensor sampling and time base

| Setting | Value | Constant |
|---|---|---|
| LED mode | Red + IR (only IR is used; red kept dim with `setPulseAmplitudeRed(0x0A)`) | `LED_MODE = 2` |
| LED brightness | 60 of 255 (255 = 50 mA, so about 12 mA) | `LED_BRIGHTNESS` |
| Internal rate | 400 samples/s, averaged 4 at a time → **100 Hz** effective | `SAMPLE_RATE = 400`, `SAMPLE_AVERAGE = 4`, `FS = 100.0` |
| Resolution | 18-bit (pulse width 411 µs), ADC range 16384 | `PULSE_WIDTH`, `ADC_RANGE` |
| Time step | 10 ms per sample | `SAMPLE_MS = 10` |

**Time is counted in samples, not read from a clock:** `t = sampleCount × SAMPLE_MS`. The sensor's own oscillator paces the samples, so intervals are exact multiples of 10 ms, even if the Arduino reads the FIFO late because it was busy with GSR. Reading `millis()` at read time would add the loop's jitter to every beat interval. Consequences: beat intervals have 10 ms resolution, and `FS` must match the sensor configuration, otherwise the filter cutoffs are wrong.

The loop takes at most one sample from the sensor per iteration (`sensor.available()` → `getFIFOIR()` → `nextSample()`). It polls the FIFO over I2C every 4 ms (`SENSOR_POLL_US`), but only when the next GSR tick is more than 700 µs away (`I2C_GUARD_US`). The sensor's 32-sample FIFO absorbs the delay, so no samples are lost as long as the `# … hr=` diagnostic stays near 1000 per 10 s.

## 2. Finger detection

| Rule | Constant |
|---|---|
| Raw IR < 50000 → `NO_FINGER`. Clears the beat history, `lastPeakT`, the envelope and the signal history. | `FINGER_THRESHOLD = 50000` |
| First sample ≥ 50000 → `SETTLING`; filters are primed | |

*Intuition:* with no finger, little infrared light is reflected back, so the reading is low. A finger reflects a lot.

*Priming:* on placement, `hpf.reset(raw, 0)` makes the high-pass behave as if the input had always been at this level, and `lpf.reset(0, 0)` starts the low-pass at zero. Without this, the jump from about 0 to about 100 000 counts would hit the high-pass as a huge step and ring for seconds, producing fake peaks.

## 3. Band-pass filter (0.7–3.5 Hz)

Two second-order IIR biquads (RBJ Audio-EQ-Cookbook formulas, Direct Form I, `float`), designed in `setup()`:

| Filter | Cutoff | Q | Removes |
|---|---|---|---|
| High-pass `hpf.design(true, 0.7, 0.7071)` | 0.7 Hz = 42 BPM | 0.7071 (Butterworth) | The large steady light level (DC), plus slow drift from breathing (about 0.2–0.4 Hz), finger pressure changes and temperature |
| Low-pass `lpf.design(false, 3.5, 0.7071)` | 3.5 Hz = 210 BPM | 0.7071 (Butterworth) | Sensor noise, small hand tremor, and the sharp detail of the pulse shape (the dicrotic notch, a second small bump in each pulse) |

*Why this band:* a heart rate from 42 to 210 BPM is 0.7 to 3.5 Hz (BPM = Hz × 60). This covers every resting or excited visitor, children included, with margin. Energy outside the band can't be a heartbeat. Measured response of the cascade at 100 Hz: −3 dB at 0.7 and 3.5 Hz, within about 1 dB from 1.0 to 3.0 Hz, −7 dB at 0.5 Hz and at 5 Hz. Each side rolls off at 12 dB per octave.

*Intuition:* the high-pass ignores anything that changes more slowly than about 40 beats per minute. The low-pass ignores anything that changes faster than about 210 beats per minute. What's left wiggles once per heartbeat.

*Why causal filtering is fine here:* unlike the GSR filter, these filters run in real time and delay the signal (by about 120–260 ms across most of the band). Only the time *between* peaks matters, and every peak is delayed by about the same amount, so BPM is unaffected.

## 4. Inversion

`sig = -lpf.step(hpf.step(raw))`. Blood absorbs infrared, so each pulse makes the reflected IR **dip**. Flipping the sign turns each heartbeat into an upward peak, so the detector only has to look for maxima.

## 5. Adaptive threshold and peak detection

| Element | Rule | Constant |
|---|---|---|
| Envelope decay | `env *= 0.995` every sample (halves in about 1.4 s) | `ENV_DECAY = 0.995` |
| Envelope attack | If `sig > env`: `env += 0.3 × (sig − env)` | `ENV_ATTACK = 0.3` |
| Threshold | `thr = 0.5 × env` | `THRESH_FRAC = 0.5` |
| Peak | The **previous** sample `sig1` is a local maximum (`sig1 > sig2 && sig1 >= sig`), above `thr`, and above 0 | |
| Refractory | At least 300 ms since the last kept peak (`tPrev − lastPeakT >= REFRACTORY_MS`) | `REFRACTORY_MS = 300` |
| Peak time | `t − SAMPLE_MS`, because the peak is only recognised one sample later | |

*Intuition:* the envelope remembers roughly how tall recent pulses were. A bump counts as a beat only if it reaches at least half that height, so the threshold follows strong and weak pulses automatically, such as a cold finger or light pressure. After a beat, the detector ignores everything for 300 ms, because no heart beats that fast at rest.

*Why these values:* the partial attack (0.3) stops a 1–2 sample spike from inflating the envelope and hiding the next real beats. The slow decay (1.4 s half-life) keeps the threshold steady across a few beats but lets it adapt within seconds when the signal weakens. The 300 ms refractory period (200 BPM) mainly rejects the dicrotic notch, which follows the main peak within a few hundred ms and could otherwise double the count.

## 6. Beat-interval validation (`handlePeak()`)

Each peak after the first gives an interval `ibi = tPeak − lastPeakT` (ms). It is compared to a **reference**: the median of the stored intervals once there are at least 3, otherwise the last accepted interval (0 = none yet).

| Case | Condition | Action |
|---|---|---|
| First peak | `lastPeakT == 0` | Remember it; nothing to measure yet |
| Too slow / pause | `ibi > 1500` ms (`MAX_IBI`, 40 BPM) | Restart the chain from this peak (`lastPeakT = tPeak`). Counted in `rejectedTotal`, but not in `consecReject` |
| Extra peak (too early) | `ibi < 333` ms (`MIN_IBI`, 180 BPM), or `ibi < 0.7 × ref` | Reject. **`lastPeakT` is not moved**, so the next real beat is still measured from the last real beat |
| Missed beat (too late) | `ibi > 1.3 × ref` | Reject, but move `lastPeakT` to this peak |
| Accepted | Otherwise | Push into the 8-slot history, reset `consecReject`. Once 4 intervals are stored → `LOCKED`, `bpm = round(60000 / median)` |

Constants: `OUTLIER_FRAC = 0.30`, `IBI_HIST = 8`, `MIN_BEATS_LOCK = 4`, `MAX_CONSEC_REJECT = 4`.

After a rejection:

- **While acquiring** (fewer than 3 stored intervals) and `ibi ≥ 333`: the history is thrown away and reseeded with this interval. Early on there is no trustworthy reference, so the newest plausible interval is as good as the old one.
- **Otherwise**, after 4 rejections in a row: clear the history and go back to `ACQUIRING`. The detector has probably locked onto the wrong rhythm.

*Intuition:* a real heart doesn't suddenly beat 30 % faster or slower from one beat to the next at rest. An interval that is far off the recent typical value is almost always a noise peak or a missed beat, not the heart. Using the **median** of the last 8 intervals for both the reference and the reported BPM means a single bad interval can't move the result.

*Consequences:* the reported BPM changes smoothly and lags real changes by a few beats. A genuine sudden jump of more than 30 % is first rejected 4 times and then re-acquired, which takes several seconds.

## State machine

```
           finger placed            150 samples (1.5 s)        4 accepted intervals
NO_FINGER ──────────────> SETTLING ───────────────────> ACQUIRING ─────────────────> LOCKED
    ^                                                        ^                          |
    |                                                        └── 4 consecutive rejects ─┤
    |                                                        └── 3 s without accepted ──┘
    └──────────────── raw IR < 50000, from any state ──────────────────────────────────┘
```

| Transition | Rule | Constant |
|---|---|---|
| → `SETTLING` | IR rises above `FINGER_THRESHOLD`; filters primed | |
| `SETTLING` → `ACQUIRING` | After 150 samples the filters have settled. No peaks are detected and the envelope isn't updated while settling. | `SETTLE_SAMPLES = 150` |
| `ACQUIRING` → `LOCKED` | 4 accepted intervals (so at least 5 peaks) | `MIN_BEATS_LOCK = 4` |
| → `ACQUIRING` | 4 consecutive rejects (`MAX_CONSEC_REJECT`), or more than 3000 ms since the last accepted beat while any interval is stored (`BEAT_TIMEOUT_MS`). History cleared, `bpm = 0`. | |
| → `NO_FINGER` | IR below threshold, from any state | |
| `NO_SENSOR` | Not a detector state: reported in the `B` line when `sensor.begin()` failed at boot | |

Typical time from placing a finger to the first non-zero BPM at about 70 BPM: 1.5 s settling + up to one beat for the first peak + 4 intervals (about 3.4 s), so about 5–6 s, plus up to 1 s until the next `B` line.

The `B` line reports `bpm` only when `state == LOCKED`, otherwise 0 (`printBpmLine()`).

## What the app does with BPM

No filtering, smoothing or recalculation. The firmware already reports a median-based value.

- `arduino.py` `parse_line()` checks the range: `0 ≤ bpm ≤ 300` (`BPM_MAX`) and `state` in `BPM_STATES`.
- `polygraph_app.py` `heart_rate_text()`: shows `Heart rate: <bpm> BPM` only when `state == "LOCKED"` and `bpm > 0`, `Heart rate: -- (<state>)` otherwise, and `Heart rate: no data` if no `B` line arrived for 3 s (`BPM_STALE_SECONDS`). The setup screen shows "check SDA/SCL wiring" for `NO_SENSOR`.
- `records.py` saves every `B` line received during the recording unchanged in `*_bpm.csv` (see [csv-files.md](csv-files.md)).
- `plotting.py` turns `bpm = 0` into gaps (NaN) and draws the rest as a step plot with markers.

## Tuning reference

| Symptom | Likely cause | Adjust |
|---|---|---|
| `SATURATED` in test mode, or no pulse with IR near 262143 | LED too bright or finger pressed too hard | Lower `LED_BRIGHTNESS`; press lighter |
| Stays `NO_FINGER` with a finger on | IR below 50000 (thin or cold finger, dim LED) | Raise `LED_BRIGHTNESS` or lower `FINGER_THRESHOLD` |
| BPM about double the real value | Dicrotic notch counted as a beat | Raise `REFRACTORY_MS` or `THRESH_FRAC` |
| Never locks, many `rej` | Movement, or the rhythm really varies more than 30 % | Keep the hand still; loosen `OUTLIER_FRAC` carefully |
| `# … hr=` well below 1000 | FIFO starved by the GSR loop | See the firmware timing notes in [arduino/polygraph/README.md](../arduino/polygraph/README.md) |

Changing `SAMPLE_RATE` or `SAMPLE_AVERAGE` requires changing `FS` and `SAMPLE_MS` to match. A higher `LED_BRIGHTNESS` also increases the 5 V rail ripple that couples into GSR (see [NOISE_ANALYSIS.md](../python_app/NOISE_ANALYSIS.md), finding 5).
