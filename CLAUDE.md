# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A science-museum "polygraph" exhibit. The repo is split by language:

- `arduino/`: firmware (C++ sketches). Each sketch must stay in its own folder named after its `.ino`, because Arduino merges every `.ino` in a folder into one program.
  - [arduino/polygraph/polygraph.ino](arduino/polygraph/polygraph.ino): the current combined firmware (GSR at 500 Hz on A0 plus the MAX30102 BPM detector over I2C, one 115200-baud serial stream). Protocol and wiring: [arduino/polygraph/README.md](arduino/polygraph/README.md).
  - `arduino/legacy/`: old standalone sketches and MATLAB checks, kept for reference (`bpm_detector/`, `checking_GSR/`, `checking ECG/`).
- `python_app/`: the Python desktop app that reads the serial stream (`polygraph_app.py`, `arduino.py`, `records.py`, `plotting.py`, `gsr_filters.py`, `gsr_diagnostics.py`; see [python_app/readme.md](python_app/readme.md) and `python_app/NOISE_ANALYSIS.md`). Recordings go to `python_app/measurements/` (gitignored).
- `plan.md`, `todo.md`, `todo_15.10.md`: planning notes.

The BPM detector in `polygraph.ino` is ported from [arduino/legacy/bpm_detector/bpm_detector.ino](arduino/legacy/bpm_detector/bpm_detector.ino), the robust standalone detector: it drains the sensor FIFO at a fixed 100 Hz, timed by sample count rather than `millis()`, then runs a biquad band-pass (0.7–3.5 Hz), an adaptive-threshold peak detector with a refractory period, and validation of each beat interval against the median of recent intervals. The section "How bpm_detector.ino works" below describes that legacy sketch.

## Build / upload

There is no build config, board definition, or test suite in the repo. The sketch README wires the sensor to an Arduino Uno (SDA=A4, SCL=A5), but no FQBN is recorded; verify on hardware via the serial output. Arduino requires the sketch folder name to match the `.ino` name, so keep `arduino/polygraph/polygraph.ino` paired.

Dependency: **SparkFun MAX3010x Pulse and Proximity Sensor Library** (provides `MAX30105.h` and `heartRate.h`; the MAX30105 driver also drives the MAX30102).

With `arduino-cli` (substitute the real FQBN and port):

```
arduino-cli lib install "SparkFun MAX3010x Pulse and Proximity Sensor Library"
arduino-cli compile --fqbn <fqbn> arduino/polygraph
arduino-cli upload  --fqbn <fqbn> -p <port> arduino/polygraph
arduino-cli monitor -p <port> -c baudrate=115200
```

## How bpm_detector.ino works

User-facing docs (wiring, serial output fields, tuning table, troubleshooting) live in [arduino/legacy/bpm_detector/README.md](arduino/legacy/bpm_detector/README.md); keep it in sync when changing constants, output formats, or states.

- **Time base is the sample count, not `millis()`**: `t = sampleCount * SAMPLE_MS` (10 ms). `millis()` is only used for the 1 s status print. The sensor is configured for 400 Hz with 4-sample averaging = 100 Hz effective, and `FS` must match or the biquad coefficients are wrong.
- **`loop()` must keep draining the FIFO** (`sensor.check()` then `while (sensor.available())`); each sample goes through `processSample()`. Don't add `delay()` or slow work (`sps` in the STATUS line should stay ~100). In plot mode a line is printed per sample, so it is much heavier on serial.
- **Signal chain in `processSample()`**: `-lpf.step(hpf.step(raw))`. The sign is inverted because IR drops on each pulse, so a heartbeat is a positive peak. `sig`, `sig1`, `sig2` are the last three samples; a peak is detected one sample late (at `sig1`), so peak time is `t - SAMPLE_MS`.
- **State machine**: `NO_FINGER -> SETTLING (SETTLE_SAMPLES) -> ACQUIRING -> LOCKED`. Losing the finger (`raw < FINGER_THRESHOLD`) resets from any state. On finger placement the filters are primed with `reset()` so the DC step doesn't ring.
- **Beat validation in `handlePeak()`**: reference interval is the median of the last `IBI_HIST` intervals once there are 3, else the previous interval. Interval too short is an extra peak (`lastPeakT` is deliberately *not* advanced); too long is a missed beat (`lastPeakT` is advanced); `MAX_CONSEC_REJECT` rejects or `BEAT_TIMEOUT_MS` without an accepted beat re-acquires. `bpm` is only non-zero once `MIN_BEATS_LOCK` intervals are accepted.
- **Output contract**: `q` mode prints exactly `BPM=<n>` once a second (0 when not locked) and is meant for another program to parse, so don't change that format. `t` and `p` are for human diagnostics.
- The code uses `float` math and the Arduino `PI`; all tunables are `const`s at the top of the file.

## Release build (Python app)

Publishing a GitHub release runs `.github/workflows/release.yml`, which builds a PyInstaller `--onefile` exe (console kept) from `python_app/polygraph_app.py` and attaches it to the release; see [docs/releasing.md](docs/releasing.md). The workflow writes the git-ignored `python_app/_version.py` (shown in the window title). In a frozen build `OUTPUT_FOLDER` is `measurements/` next to the exe (created at startup). `gsr_diagnostics.py` is not packaged.
