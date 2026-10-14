# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A heart-rate component for a science-museum "polygraph" exhibit, made of Arduino sketches that read a MAX30102 pulse-oximeter over I2C:

- [bpm_detector/bpm_detector.ino](bpm_detector/bpm_detector.ino): the robust detector. It drains the sensor FIFO at a fixed 100 Hz, timed by sample count rather than `millis()`, then runs a biquad band-pass (0.7–3.5 Hz), an adaptive-threshold peak detector with a refractory period, and validation of each beat interval against the median of recent intervals; BPM is the median interval converted to BPM. It has three serial diagnostic modes (`t` text, `p` Serial Plotter, `q` quiet `BPM=<n>`).
- [max30102/max30102.ino](max30102/max30102.ino): the original sketch, which uses SparkFun's `checkForBeat()` and a moving average (described below).

Each sketch must stay in its own folder, because Arduino merges every `.ino` in a folder into one program.

## Build / upload

There is no build config, board definition, or test suite in the repo; the target board is not recorded. Arduino requires the sketch folder name to match the `.ino` name, so keep `max30102/max30102.ino` paired.

Dependency: **SparkFun MAX3010x Pulse and Proximity Sensor Library** (provides `MAX30105.h` and `heartRate.h`; the MAX30105 driver also drives the MAX30102).

With `arduino-cli` (substitute the real FQBN and port):

```
arduino-cli lib install "SparkFun MAX3010x Pulse and Proximity Sensor Library"
arduino-cli compile --fqbn <fqbn> max30102
arduino-cli upload  --fqbn <fqbn> -p <port> max30102
arduino-cli monitor -p <port> -c baudrate=115200
```

## How max30102.ino works

- `loop()` polls `getIR()` every iteration; `checkForBeat()` (SparkFun's PBA algorithm) must be fed samples continuously, so avoid adding `delay()` or blocking work in `loop()` — it will cause missed beats.
- IR < 50000 means "no finger"; averaging state resets only after 1 s without contact.
- A beat interval becomes a BPM sample only if it is within 40–150 BPM and, once ≥2 samples exist, within ±25% of the running average (`RATE_SIZE` = 4). An interval that is too short is treated as an extra peak and does not move `lastDetectedBeatMs`. 3 consecutive rejects reset the measurement.
- Output is one CSV row per sample (header printed in `setup()`): `time_ms,ir_value,contact_ok,beat_status,detected_bpm,average_bpm,consecutive_rejected`. `beat_status` marks events (1 = first beat, 2 = accepted, 3 = rejected) only on the row where the beat happens. `detected_bpm` and `average_bpm` are held between beats so they plot as continuous lines.
