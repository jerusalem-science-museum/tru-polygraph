# Known issues

Problems found during testing that are not worth fixing yet. Each entry says what was seen, when it matters, and a likely fix.

## Repeated GSR lines right after the Arduino restarts

- **Seen:** within about 10 s of an Arduino restart, the serial stream sometimes repeats a block of 3-4 GSR lines (about 64 bytes, the size of the Arduino's serial transmit buffer) several times, with a broken fragment such as `4,85` in between. The repeated samples land in `*_voltage.csv` as rows whose `arduino_time_us` goes backwards.
- **When it matters:** only when the Arduino restarts during an examination, for example after a connection loss when the cable is replugged. One test recording (2026-11-15) had 25 such rows around 111 s. Recordings without a restart are not affected; no older recording has any.
- **Cause:** unknown. The firmware prints a fresh `micros()` per line, so it cannot repeat itself in normal operation; candidates are the USB-serial chip or driver, or a firmware crash and restart. A raw-stream check also showed the board restarting by itself once (uptime 33 s and 143 s), possibly a late reset from opening the port.
- **Likely fix:** in the app, drop any GSR or BPM sample whose unwrapped Arduino time is not later than the previous one.

## Heart-rate sensor unplugged in the middle of an I2C transfer

- **Seen (not tested):** if the sensor is pulled out mid-read, it can be left holding the SDA line low. The firmware only clears a stuck bus at boot, so the sensor may stay "lost" until the Arduino is reset.
- **Why not cleared at runtime:** the bus clear reads A4 through the ADC, which can spoil the next GSR sample.

## Heart-rate sensor briefly lost after a long stall

- **Seen (not tested):** if polling the sensor stalls for more than 320 ms, its 32-sample FIFO fills and its read and write pointers match, so it looks silent. After 1 s the firmware treats it as lost and reconfigures it. This recovers on its own but shows as a short `NO_SENSOR` stretch.

## Diagnostics

- `rl` on the 10 s `# late=` line counts both outlier-long beat intervals and pauses longer than `MAX_IBI`; split it if the difference ever matters.
- `late` rises by about 4 per 10 s, compared with about 2 in v1.2 (longer diagnostic line plus the repeated `# i2c` line). `resync` stays 0, so no GSR samples are lost.

## Upgrading the exe keeps the old config.toml

A `config.toml` written by an older exe lacks settings added later (`bpm_display_hold_seconds` and `reconnect_interval_seconds` in v1.3), and the app stops at startup naming the missing setting. Add the missing lines from the new version's `python_app/config.toml`, or delete `config.toml` next to the exe so the new defaults are written (this also resets the questions).
