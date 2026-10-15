# polygraph (combined GSR + heart-rate firmware)

One Arduino Uno/Nano sketch (`polygraph.ino`) that streams a GSR sensor at 500 Hz and runs the MAX30102 BPM detector from `legacy/bpm_detector/bpm_detector.ino` (same algorithm and constants), over one USB serial link at **115200 baud**.

Library: **SparkFun MAX3010x Pulse and Proximity Sensor Library**.

Full field-by-field documentation of the serial output, the saved CSVs and both filter chains: [docs/](../../docs/README.md).

```
arduino-cli compile --fqbn arduino:avr:uno polygraph
arduino-cli upload  --fqbn arduino:avr:uno -p <port> polygraph
```

## Wiring

| Sensor pin | Arduino |
|---|---|
| GSR signal | `A0` |
| GSR VCC / GND | 5 V / GND |
| MAX30102 SDA | `A4` |
| MAX30102 SCL | `A5` |
| MAX30102 VIN / GND | 5 V (or 3.3 V, per your breakout) / GND |

Share one GND between both sensors (star point at the Arduino).

## Serial protocol

ASCII, one record per line, ending in `\r\n`.

| Line | Rate | Meaning |
|---|---|---|
| `<t_us>,<raw_adc>` | 500 Hz | GSR sample. `t_us` = `micros()` just before `analogRead(A0)`; `raw_adc` = 0..1023. Same format as `legacy/checking_GSR/.../checking_GSR.ino`. |
| `B,<t_us>,<bpm>,<state>` | 1 Hz | Heart rate. `t_us` = `micros()` at emission. `bpm` is an integer, `0` when not locked. `state` is `NO_FINGER`, `SETTLING`, `ACQUIRING`, `LOCKED` or `NO_SENSOR`. |
| `# <text>` | rare | Comments: `# polygraph ready` at startup, `# MAX3010x not found`, and `# late=<n> resync=<n> hr=<n>` every 10 s (`hr` = MAX30102 samples read in those 10 s, expect ~1000; near 0 means the sensor is being starved). Parsers should ignore these. |

`t_us` is the raw 32-bit `micros()` and wraps every ~71.6 min; the PC side must unwrap it.

If the MAX30102 is not found, the sketch keeps streaming GSR and sends `B,<t_us>,0,NO_SENSOR` once a second.

## Test mode (checking the sensors by hand)

Open the Serial Monitor at 115200 baud and send `t`. The 500 Hz stream stops, and every second you get one line per sensor, each ending in a verdict:

```
GSR  raw=412 [405..418] 2.01 V  sps=500  -> OK
HR   ir=118532 pulse=405 LOCKED bpm=73 sps=100 ok=12 rej=1  -> OK
  beat  ibi=812 ms (73 bpm)
```

| Verdict | Meaning |
|---|---|
| GSR `NO CONTACT` | A0 reads 0 V: electrodes open or fingers off |
| GSR `MAXED at 5 V` | A0 stuck at the top: short circuit or wiring fault |
| GSR `UNSTEADY` | Readings swung more than 50 counts (~0.25 V) within the second: movement or loose contact |
| HR `NOT FOUND` | MAX30102 didn't answer on I2C at startup: check SDA=A4, SCL=A5, power, then reset |
| HR `no finger` / `settling` / `looking for steady beats` | Normal progression after placing a finger; `OK` once locked |
| HR `SATURATED` | IR maxed out: press lighter or lower `LED_BRIGHTNESS` |

A `beat` line is printed for every accepted heartbeat. `sps` should be about 500 (GSR) and 100 (HR). Send `s` to go back to stream mode. Test mode isn't remembered: the app opening the port resets the board into stream mode.

## Timing

- GSR ticks use a fixed 2000 us schedule. If the loop falls more than 10 ms behind, the schedule resyncs to "now" (no catch-up burst) and `resync` is incremented. `late` counts ticks more than 500 us late.
- Each loop iteration does one thing: a due GSR sample, else one MAX30102 sample, else a periodic FIFO poll (every 4 ms, skipped within 1.5 ms of the next GSR tick), else the 1 Hz / 10 s text lines. There is no `delay()`.
- I2C runs at 400 kHz. A FIFO poll still costs a few hundred microseconds, so occasional small `late` counts are expected. `Serial.print` blocks when the 64-byte TX buffer is full, and the GSR stream uses about 74% of the link.

## Hardware notes

- Keep a common GND. The ADC reference is the 5 V rail (USB), so rail noise appears in the GSR reading.
- The MAX30102 LED current pulses (up to 50 mA, here `LED_BRIGHTNESS` 60 at 400 Hz) can ripple the 5 V rail and couple into GSR. A 400 Hz ripple sampled at 500 Hz aliases to 100 Hz. Mitigations: 100 nF + 10 uF decoupling at the MAX30102, an RC low-pass before `A0`, a lower `LED_BRIGHTNESS`, powering the MAX30102 from a separate 3.3 V supply, running the laptop on battery. See the noise analysis in `python_app/NOISE_ANALYSIS.md`.
