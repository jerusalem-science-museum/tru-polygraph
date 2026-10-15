# BPM Detector (MAX30102)

An Arduino sketch that measures heart rate from a finger on a MAX30102 (or MAX30105) pulse sensor. It reports a steady BPM that doesn't jump around, and it can print diagnostics showing why each beat was accepted or rejected.

## Hardware

| MAX30102 | Arduino Uno |
|---|---|
| VIN | 5V (or 3.3V, depending on the breakout board) |
| GND | GND |
| SDA | A4 |
| SCL | A5 |

## Setup

1. Install the **SparkFun MAX3010x Pulse and Proximity Sensor Library**. In the Arduino IDE, use Library Manager; or from the command line:
   ```
   arduino-cli lib install "SparkFun MAX3010x Pulse and Proximity Sensor Library"
   ```
2. Open `bpm_detector.ino` and upload it.
3. Open the Serial Monitor at **115200 baud**.

## Using it

Rest a fingertip lightly on the sensor and keep still. Pressing hard squeezes out the blood and weakens the pulse signal.

- **About 1.5 s** after the finger is placed, the filters have settled and detection starts.
- **After 4 consistent beats** (about 3–4 s at a normal heart rate), a BPM is reported.
- **When the finger is lifted**, everything resets.

## Serial commands

Send a single letter from the Serial Monitor to switch the output mode:

| Key | Mode | Output |
|---|---|---|
| `t` | Text (default) | A `BEAT` line for every detected peak, and a `STATUS` line every second |
| `p` | Plot | 100 lines per second for the Arduino **Serial Plotter** |
| `q` | Quiet | Only `BPM=<n>` once per second; use this when another program reads the output |

### Text mode: why there are two kinds of line

Text mode prints two kinds of line, because they answer different questions:

- **`BEAT` lines answer "is the detector judging each heartbeat correctly?"** One appears each time the detector finds a peak, accepted or rejected. It shows the measured interval, what it was compared against, and the verdict, so you can see exactly why a beat counted or was thrown out. When no peaks are found, for example with no finger on the sensor or a signal too weak to cross the threshold, no `BEAT` lines appear at all.
- **`STATUS` lines answer "is the setup healthy?"** One appears every second no matter what. It shows the current state, the signal levels, the sample rate and running totals. It's how you notice problems that stop beats being detected in the first place: no finger detected, a saturated sensor, a weak pulse, or lost samples.

Use them together. If the BPM is wrong, the `BEAT` lines show which beats were miscounted. If the BPM never appears, the `STATUS` line shows whether the detector is getting a usable signal at all. The `STATUS` line is also the only output while nothing is being detected, so it confirms the sketch is still running.

### `BEAT` line

Printed once per detected peak. The example below shows the format; the values are made up for illustration:

```
BEAT t=15230 ibi=812 inst=73.9 ref=820 peak=412 thr=198 -> ok | bpm=73 n=6
```

| Field | Meaning |
|---|---|
| `t` | Time of the peak, in ms since startup |
| `ibi` | Time since the previous beat, in ms (0 for the first beat) |
| `inst` | BPM calculated from this one interval |
| `ref` | Interval this beat was checked against: the median of recent intervals, or the previous interval during startup |
| `peak` / `thr` | Height of the peak and the threshold it had to clear |
| verdict | `ok`, `first`, `REJECT early`, `REJECT late` or `REJECT too slow` |
| `bpm` | Reported BPM after this beat (0 until locked) |
| `n` | Number of intervals currently stored (up to 8) |

### `STATUS` line

Printed once per second. As above, the example shows the format; the values are made up for illustration:

```
STATUS LOCKED bpm=73 raw=118532 dc=118210 ac=405.3 pi=0.343% sps=100 ok=42 rej=3
```

| Field | Meaning |
|---|---|
| state | `NO_FINGER`, `SETTLING`, `ACQUIRING` or `LOCKED` |
| `bpm` | Reported BPM |
| `raw` | Latest raw IR reading |
| `dc` | Slow average of the raw reading; the steady light level, which shifts with finger position and pressure |
| `ac` | Pulse strength |
| `pi` | Perfusion index: pulse strength as a percentage of the light level |
| `sps` | Samples processed in the last second; should be about 100 |
| `ok` / `rej` | Total accepted and rejected beats since startup |
| `SATURATED` | Shown when the sensor is maxed out; lower `LED_BRIGHTNESS` |

### Plot mode

Shows three lines in the Serial Plotter:

- `ac`: the filtered pulse wave;
- `thr`: the detection threshold;
- `beat`: a spike wherever a peak was detected.

Each heartbeat should produce one clear hump above `thr`, with a single `beat` spike on it.

## How it works

1. **Sampling.** The sensor delivers 100 IR readings per second. Every reading is processed, and time is measured by counting samples rather than with the Arduino clock.
2. **Filtering.** A band-pass filter (0.7–3.5 Hz, roughly 40–210 BPM) removes the steady light level, slow drift from breathing, and high-frequency noise. The signal is flipped so each heartbeat appears as a peak.
3. **Peak detection.** A peak counts if it rises above a threshold set at half the recent peak height, so it adapts to strong and weak signals. Two peaks can't be closer than 300 ms, which stops the small second bump in each pulse from counting as a beat.
4. **Beat validation.** The time between beats must be 333–1500 ms (40–180 BPM). It must also be within 30% of the median of recent intervals:
   - An early extra peak is ignored.
   - A late one (a missed beat) is rejected.
   - After 4 rejects in a row, or 3 s with no good beat, the detector starts over.
5. **Output.** BPM = 60000 ÷ the median of the last 8 intervals. A single bad beat can't move a median, which is why the reading stays steady.

```
NO_FINGER ──finger placed──> SETTLING ──1.5 s──> ACQUIRING ──4 good intervals──> LOCKED
    ^                                                ^                              │
    └──────── finger removed (from any state) ───────┼──────────────────────────────┘
                                                     └── 4 rejects in a row, or 3 s with no good beat
```

## Tuning

All settings are constants at the top of the sketch.

| Constant | Default | Effect |
|---|---|---|
| `LED_BRIGHTNESS` | 60 | IR LED power (0–255). Lower it if `SATURATED` appears; raise it if `pi` is very small |
| `FINGER_THRESHOLD` | 50000 | Raw reading below which no finger is assumed |
| `SETTLE_SAMPLES` | 150 | Samples (1.5 s) to wait for the filters after the finger is placed |
| `REFRACTORY_MS` | 300 | Minimum gap between two peaks |
| `THRESH_FRAC` | 0.5 | Threshold as a fraction of recent peak height |
| `OUTLIER_FRAC` | 0.30 | How far a beat may differ from the median and still be accepted |
| `IBI_HIST` | 8 | Intervals in the median; more is steadier but slower to react |
| `MIN_BEATS_LOCK` | 4 | Good intervals required before a BPM is reported |
| `BEAT_TIMEOUT_MS` | 3000 | Time with no good beat before restarting |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `MAX3010x not found` | Check wiring and power, and that SDA/SCL are on the right pins |
| Stuck at `NO_FINGER` with a finger on the sensor | `raw` is below 50000: raise `LED_BRIGHTNESS` or lower `FINGER_THRESHOLD` |
| `SATURATED` | LED too bright, or the sensor is exposed to strong ambient light |
| Stuck at `ACQUIRING` with many `REJECT` lines | Movement or too much pressure. Check plot mode for a clean, regular wave |
| BPM about double the real value | Two peaks per heartbeat. Check plot mode, then raise `REFRACTORY_MS` or `THRESH_FRAC` |
| `sps` well below 100 | Samples are being lost; avoid adding `delay()` or slow code to `loop()` |
