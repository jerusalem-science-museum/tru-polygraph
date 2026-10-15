# Serial protocol

Source of truth: [arduino/polygraph/polygraph.ino](../arduino/polygraph/polygraph.ino) (sender) and [python_app/arduino.py](../python_app/arduino.py) `parse_line()` (receiver).

**In short:** the Arduino sends text lines over USB. Most lines are GSR samples (500 per second), one line per second carries the heart rate, and lines starting with `#` are notes for humans that the app ignores.

## Link settings

| Setting | Value | Where |
|---|---|---|
| Baud rate | 115200, 8N1 | `Serial.begin(115200)` in `setup()`; `BAUD_RATE` in `arduino.py` |
| Encoding | ASCII | the app decodes with `decode("ascii", errors="strict")` |
| Line ending | `\r\n` (`Serial.println`) | the app calls `line.strip()`, so `\n` alone would also work |
| Board reset | Opening the port resets an Uno/Nano | the app waits 2.0 s after opening (`detect_arduino()`) |

The firmware has two modes. **Stream mode** is the default after every reset, and it is what the app reads. **Test mode** is for checking the sensors by hand in the Serial Monitor. Send `t` to enter test mode and `s` to return to stream mode (`handleSerial()`). Any other character is ignored. The mode isn't stored, so opening the port, which resets the board, always starts in stream mode.

## Stream mode lines

### 1. GSR sample: `<t_us>,<raw_adc>`

Example: `8496608,199`

| Field | Meaning | Units | Range | Notes |
|---|---|---|---|---|
| `t_us` | `micros()` read just before `analogRead(A0)` (`uint32_t t = micros();` in `loop()`) | µs since board reset | 0 … 4 294 967 295 | Raw 32-bit value, so it **wraps to 0 every 2^32 µs ≈ 71.6 min**. Resolution is 4 µs on a 16 MHz AVR. The app unwraps it (see below). |
| `raw_adc` | 10-bit ADC reading of the GSR sensor output on `A0` | ADC counts | 0 … 1023 | Reference is AVcc (the USB 5 V rail), so volts = `raw_adc × 5.0 / 1023` in the app (`ADC_REFERENCE_VOLTS`, `ADC_MAX`). `0` means the electrodes are open or a finger is off. `≥ 1020` (`GSR_SHORT_MIN`) means the input is maxed at 5 V, probably a short or a wiring fault. |

**Rate:** nominally 500 Hz (`GSR_INTERVAL_US = 2000`). The schedule is phase-locked: each tick is due 2000 µs after the previous *scheduled* tick, so the average rate stays exact even when individual ticks are late.

Timing edge cases (all in `loop()` part (a)):

- **Jitter.** A tick can start late if the loop was busy with an I2C read or a blocked `Serial.print`. The timestamp is the real time of that sample, so spacing is not perfectly uniform. Ticks more than 500 µs late (`LATE_US`) are counted in `lateTicks`.
- **Catch-up.** A tick that is late by up to 10 ms is followed by its successors as fast as possible until the schedule is caught up, so a few samples may be closer together than 2000 µs.
- **Resync (gap).** If the loop falls more than 10 ms behind (`RESYNC_US = 10000`), the schedule jumps to "now" without producing catch-up samples. The missed samples are simply absent, which shows up as a gap of more than 2000 µs in `t_us`. The event is counted in `resyncs`. The app's filter resamples over such gaps (see [filters-gsr.md](filters-gsr.md#gaps-and-sample-rate)).
- GSR sampling has priority over everything else. The loop does at most one task per iteration and never calls `delay()`.

The same `<t_us>,<raw_adc>` format was used by the older `arduino/legacy/checking_GSR/.../checking_GSR.ino`. The difference is that the old sketch sent the loop-entry time rather than the time just before `analogRead`.

### 2. Heart rate: `B,<t_us>,<bpm>,<state>`

Examples: `B,9001276,67,LOCKED`, `B,3000348,0,NO_SENSOR`, `B,123456789,0,ACQUIRING`

Emitted by `printBpmLine()`.

| Field | Meaning | Units | Range | Notes |
|---|---|---|---|---|
| `B` | Record tag | | literal `B` | Distinguishes the line from a GSR sample |
| `t_us` | `micros()` **at the moment the line is printed** | µs since board reset | 0 … 2^32−1 | Same clock and same 71.6 min wrap as the GSR `t_us`, so one unwrapper must be shared by both line types (`MicrosUnwrapper`). This is **not** the time of the last heartbeat. |
| `bpm` | Reported heart rate | beats per minute, integer | `0`, or about 40 … 180 | `0` unless `state` is `LOCKED` (`state == LOCKED ? bpm : 0`). When locked, it is `round(60000 / median(last ≤ 8 accepted beat intervals))`. Intervals are limited to 333–1500 ms, so a locked value is 40–180. The app accepts 0–300 (`BPM_MAX`). |
| `state` | Detector state | | `NO_FINGER`, `SETTLING`, `ACQUIRING`, `LOCKED`, `NO_SENSOR` | See the table below and [filters-heart-rate.md](filters-heart-rate.md#state-machine) |

**Rate:** about 1 Hz, timed by `millis()` (`ms - lastB >= 1000`). It is only sent when the loop has no GSR tick or sensor work pending, so the spacing is about 1 s with a few ms of jitter. The heart-rate algorithm itself runs on its own 100 Hz sample-count clock. `t_us` only says when the report was sent.

| `state` | Meaning | `bpm` |
|---|---|---|
| `NO_FINGER` | IR reading below `FINGER_THRESHOLD` (50000): no finger on the sensor | 0 |
| `SETTLING` | Finger just detected; filters settle for `SETTLE_SAMPLES` = 150 samples (1.5 s) | 0 |
| `ACQUIRING` | Looking for consistent beats (fewer than `MIN_BEATS_LOCK` = 4 accepted intervals), or re-acquiring after too many rejects or a timeout | 0 |
| `LOCKED` | At least 4 consistent intervals; `bpm` is valid | 40–180 |
| `NO_SENSOR` | The MAX30102 did not answer on I2C at startup (`sensor.begin()` failed). GSR keeps streaming. It is detected only at boot, so after fixing the wiring the board must be reset. | 0 |

### 3. Comment lines: `# <text>`

Every line starting with `#` is informational. The app's `parse_line()` returns `("comment", ...)`, and `serial_reader()` drops it without counting it as a bad line.

| Line | When | Fields |
|---|---|---|
| `# polygraph ready (send t = test mode, s = stream mode)` | Once at startup (`setup()`) | |
| `# MAX3010x not found` | Once at startup if the heart-rate sensor is missing | Followed by `B,…,0,NO_SENSOR` lines every second |
| `# late=<n> resync=<n> hr=<n>` | Every 10 s in stream mode (`ms - lastDiag >= 10000`). The first one comes about 10 s after boot. It is sent on a different loop iteration from the `B` line. | `late`: GSR ticks more than 500 µs late, **cumulative since boot**. `resync`: schedule resyncs (gaps), cumulative since boot. `hr`: MAX30102 samples processed **since the previous diagnostic line**; expect about 1000 (100 Hz × 10 s). Near 0 means the sensor is starved or missing (always 0 with `NO_SENSOR`). |
| `# TEST MODE: one GSR and one HR line per second. Send s to go back to stream mode.` | After `t` is received | Preceded by one empty line |
| `# stream mode` | After `s` is received in test mode | |

## Test mode lines (human use only)

In test mode the 500 Hz GSR lines, `B` lines and `# late=` lines all stop. Instead, each second produces a GSR report and an HR report (`printGsrReport()`, `printHrReport()`), plus a beat line for each accepted beat. These lines have no `#` prefix, so **the app can't use a board in test mode**. Port detection wouldn't find GSR lines, and during a recording the app would count the lines as malformed and stop with a "no valid data" error after 2 s. In practice this doesn't happen, because opening the port resets the board into stream mode.

```
GSR  raw=412 [405..418] 2.01 V  sps=500  -> OK
HR   ir=118532 pulse=405 LOCKED bpm=73 sps=100 ok=12 rej=1  -> OK
  beat  ibi=812 ms (73 bpm)
```

| Field | Meaning |
|---|---|
| `GSR raw=` | Mean ADC reading over the last second (integer, 0–1023) |
| `[min..max]` | Lowest and highest ADC reading in that second |
| `… V` | Mean voltage, `avg × 5.0 / 1023`, 2 decimals |
| `GSR sps=` | GSR samples taken in that second (expect about 500) |
| GSR verdict | First match wins: `NO SAMPLES` (count 0), `NO CONTACT` (max ≤ 0, `GSR_OPEN_MAX`), `MAXED at 5 V` (min ≥ 1020, `GSR_SHORT_MIN`), `UNSTEADY` (max − min > 50 counts ≈ 0.24 V, `GSR_UNSTEADY`), else `OK` |
| `HR ir=` | Latest raw IR reading, 18-bit ADC counts (0–262143) |
| `pulse=` | Current peak envelope `env` of the filtered pulse signal (arbitrary units, pulse amplitude) |
| state | Detector state name (see above) |
| `HR bpm=` | Same rule as the `B` line: 0 unless `LOCKED` |
| `HR sps=` | MAX30102 samples processed in that second (expect about 100) |
| `ok=` / `rej=` | Accepted / rejected beat intervals since boot (`acceptedTotal`, `rejectedTotal`) |
| HR verdict | `NOT FOUND` (no sensor; whole line is `HR   -> NOT FOUND: …`), `NO SAMPLES`, `SATURATED` (ir > 250000, `IR_SATURATED`), `no finger`, `finger detected, settling...`, `looking for steady beats, keep still`, else `OK` |
| `beat ibi=` | Interval of an accepted beat in ms, followed by `60000 / ibi` in BPM. The division is an integer division, so it truncates, unlike the rounded `bpm` value. |

## How the app reads the stream

`python_app/arduino.py`:

- **Port detection (`detect_arduino()`).** Ports whose description mentions `arduino`, `ch340`, `usb serial` or `nano` are tried first. For each port the app opens it, waits 2.0 s for the reset, discards buffered input, and accepts the port once it has received 3 valid GSR lines within 2.5 s. `B` and `#` lines are valid but don't count toward the 3. A malformed line resets the count.
- **Validation (`parse_line()`).** GSR: exactly 2 integer fields, `0 ≤ t_us < 2^32`, `0 ≤ raw_adc ≤ 1023`. `B`: exactly 4 fields, `0 ≤ bpm ≤ 300`, and `state` must be one of the five names. Anything else raises `ValueError` and is counted as a malformed line. The count is reported in the save dialog and the line isn't saved.
- **Unwrapping (`MicrosUnwrapper`).** Turns the wrapping 32-bit `t_us` into a monotonic 64-bit count. The forward distance from the previous value is taken modulo 2^32. A distance of 2^31 µs (about 35.8 min) or more is treated as a small step backwards rather than a wrap. One instance is shared by GSR and `B` lines because they come from the same clock.
- **Disconnect detection.** While recording, if no valid GSR line arrives for 2.0 s (`SERIAL_TIMEOUT_SECONDS`), or the port raises an error, the app saves `_PARTIAL` files and stops.
