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
| `# <text>` | rare | Comments: `# polygraph ready` at startup, `# MAX3010x not found` (boot), `# MAX3010x lost` / `# MAX3010x found` (hot-plug, see below), the 10 s diagnostic line (below), and the `# i2c` boot check (see below). Parsers should ignore these. |

`t_us` is the raw 32-bit `micros()` and wraps every ~71.6 min; the PC side must unwrap it.

### 10 s diagnostic line

```
# late=3 resync=0 hr=1001 rs=2 rl=1 rq=0 to=0
```

| Field | Meaning |
|---|---|
| `late` | GSR ticks more than 500 us late, cumulative since boot |
| `resync` | GSR schedule resyncs (gaps), cumulative since boot |
| `hr` | MAX30102 samples processed in the last ~10 s; expect ~1000, near 0 = sensor starved or missing |
| `rs` | Beat intervals rejected as **too short** (extra peak: noise, dicrotic notch, movement) in the last ~10 s |
| `rl` | Beat intervals rejected as **too long** (missed beat, or a pause over `MAX_IBI`) in the last ~10 s |
| `rq` | Re-acquires caused by `MAX_CONSEC_REJECT` rejects in a row, last ~10 s |
| `to` | Re-acquires caused by `BEAT_TIMEOUT_MS` without an accepted beat, last ~10 s |

`hr`, `rs`, `rl`, `rq` and `to` are cleared after each line. A lock drop shows up as `rq` or `to` (or a `NO_FINGER`); lots of `rs` with a finger on the sensor usually means movement. The line is kept short because it shares the 64-byte TX buffer with the 500 Hz stream.

## Sensor outage (hot-plug)

GSR keeps streaming at 500 Hz whether or not the MAX30102 answers. While the sensor is missing, the `B` line is `B,<t_us>,0,NO_SENSOR` once a second.

- **Loss detection.** The sensor delivers 100 samples/s even without a finger, so if the FIFO gives no sample for 1 s (`SENSOR_LOST_US`) the sensor is marked lost: unplugged (FIFO pointers read back as 0) or power-cycled (it forgot its configuration). An I2C timeout during a FIFO poll (stuck bus) marks it lost at once. The sketch prints `# MAX3010x lost`.
- **Re-probe.** A missing sensor, whether missing since boot or lost later, is probed every 2 s (`PROBE_INTERVAL_US`), and only when the next GSR tick is not imminent. A probe is one address ping, which fails within ~0.1 ms if nothing answers. If the sensor answers, the sketch checks the part ID, runs the same `sensor.setup(...)` as at boot (a few ms, so a few GSR ticks come late but none are lost), resets the detector to `NO_FINGER` and prints `# MAX3010x found`. There is no bus clear or retry at runtime: those are part of the boot check only.
- Losing the sensor resets the detector, so after it returns the heart rate goes through `NO_FINGER -> SETTLING -> ACQUIRING -> LOCKED` again.

## I2C boot check

At startup the sketch tries the sensor twice, then clears the bus (nine SCL clocks and a STOP) and tries twice more. It prints the result once at boot and repeats it every 10 s in stream mode, because the app discards anything sent during the reset. The app appends the first copy of each connection to `measurements/i2c_boot.log`. The line always describes the **boot** check, even after a later sensor outage or reconnect (those show in the `B` line state and the `# MAX3010x lost` / `found` comments).

```
# i2c f=3290,3291 p=3480,3481 t=1 e=0 id=15 c=- OK
```

| Field | Meaning |
|---|---|
| `f=<sda>,<scl>` | Idle bus in mV before Wire starts, with only the module's pull-ups: this is the module's pull-up voltage. Near 0 with `p` near 5000 means nothing is pulling the lines up (sensor absent or unpowered). |
| `p=<sda>,<scl>` | Idle bus in mV with the AVR pull-ups added. SDA near 0 here means something is holding the bus low at boot. |
| `t=<n>` | Attempts made (1 to 4). 1 or 2 worked without a bus clear. |
| `e=<n>` | `Wire.endTransmission()` code of the last attempt: 0 = answered, 2 = no answer to its address, 4 = bus error, 5 = timeout. |
| `id=<hex>` | Part ID read on the last attempt, expect `15`. |
| `c=` | `-` = bus clear not needed, a number = SCL clocks until SDA was released (0 = it was already high), `x` = SDA stayed low through all nine clocks. |
| `OK` / `FAIL` | Whether the sensor started at boot. |

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
| HR `NOT FOUND` | MAX30102 isn't answering on I2C (at startup or lost since): check SDA=A4, SCL=A5, power. It is re-probed every 2 s, so no reset is needed |
| HR `no finger` / `settling` / `looking for steady beats` | Normal progression after placing a finger; `OK` once locked |
| HR `SATURATED` | IR maxed out: press lighter or lower `LED_BRIGHTNESS` |

A `beat` line is printed for every accepted heartbeat. `sps` should be about 500 (GSR) and 100 (HR). Send `s` to go back to stream mode. Test mode isn't remembered: the app opening the port resets the board into stream mode.

## Timing

- GSR ticks use a fixed 2000 us schedule. If the loop falls more than 10 ms behind, the schedule resyncs to "now" (no catch-up burst) and `resync` is incremented. `late` counts ticks more than 500 us late.
- Each loop iteration does one thing: a due GSR sample, else one MAX30102 sample, else a periodic FIFO poll (every 4 ms, skipped within 0.7 ms of the next GSR tick) or, while the sensor is missing, a re-probe every 2 s (same guard), else the 1 Hz / 10 s text lines. There is no `delay()` in the loop.
- I2C transactions time out after 3 ms (`Wire.setWireTimeout`). On a stuck bus one FIFO poll can block for up to ~12 ms (four transactions), which can cost one `resync`; the sensor is then marked lost and only one ping per 2 s (at most 3 ms) is tried until it answers.
- I2C runs at 400 kHz. A FIFO poll still costs a few hundred microseconds, so occasional small `late` counts are expected. `Serial.print` blocks when the 64-byte TX buffer is full, and the GSR stream uses about 74% of the link.

## Hardware notes

- Keep a common GND. The ADC reference is the 5 V rail (USB), so rail noise appears in the GSR reading.
- The MAX30102 LED current pulses (up to 50 mA, here `LED_BRIGHTNESS` 60 at 400 Hz) can ripple the 5 V rail and couple into GSR. A 400 Hz ripple sampled at 500 Hz aliases to 100 Hz. Mitigations: 100 nF + 10 uF decoupling at the MAX30102, an RC low-pass before `A0`, a lower `LED_BRIGHTNESS`, powering the MAX30102 from a separate 3.3 V supply, running the laptop on battery. See the noise analysis in `python_app/NOISE_ANALYSIS.md`.
