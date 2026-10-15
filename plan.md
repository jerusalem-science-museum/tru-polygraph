# Implementation plan for todo.md

Hardware assumption: **one Arduino (Uno/Nano, ATmega328P, 16 MHz, 2 KB RAM)** with both sensors attached:
GSR on `A0` (analog), MAX30102 on I2C (`SDA=A4`, `SCL=A5`). One USB serial link to the PC at **115200 baud**.

Work is split into four tasks. Tasks A, B and D run in parallel; task C runs after B, because B and C edit the same file.

| Task | todo item | Files owned | Depends on |
|---|---|---|---|
| A: combined firmware | (prereq for 1) | `arduino/polygraph/polygraph.ino`, `arduino/polygraph/README.md` | protocol below |
| B: BPM in the app | 1, 2 | `python_app/gsr_polygraph.py` | protocol below |
| C: answer UI + filter integration | 3 (+ wire in D) | `python_app/gsr_polygraph.py` | B and D finished |
| D: noise diagnostics | 4 | `python_app/gsr_filters.py`, `python_app/gsr_diagnostics.py`, `python_app/NOISE_ANALYSIS.md` | none |

Only edit the files you own. Don't touch `CLAUDE.md`, `todo.md`, `plan.md` or the other sketches.

---

## Shared serial protocol (the contract between A and B)

ASCII, one record per line, terminated by `\r\n` (`Serial.println`). Three line kinds:

| Line | Rate | Meaning |
|---|---|---|
| `<t_us>,<raw_adc>` | 500 Hz | GSR sample. **Unchanged** from `checking_GSR.ino`, so older tools still work. `t_us` = `micros()` taken immediately before `analogRead(A0)`; `raw_adc` = 0..1023. |
| `B,<t_us>,<bpm>,<state>` | 1 Hz | Heart rate. `t_us` = `micros()` when the line is emitted. `bpm` = integer, `0` when not locked. `state` = one of `NO_FINGER`, `SETTLING`, `ACQUIRING`, `LOCKED`, `NO_SENSOR`. |
| `# <text>` | rare | Info/comment (startup banner, "MAX3010x not found"). Parsers ignore these and must not count them as malformed. |

Rules:
- `t_us` is the raw 32-bit `micros()` value, so it **wraps every ~71.6 min**. The PC side must unwrap it. The board may have been powered long before the app connects, so a wrap can happen at any time during a recording.
- If the MAX30102 is missing, the firmware must keep streaming GSR and emit `B,<t_us>,0,NO_SENSOR` once a second. It must never halt.
- Bandwidth budget: a GSR line is at most 17 bytes, and 500/s × 17 = 8.5 kB/s against 11.52 kB/s capacity at 115200 8N1 (~74%). B lines add ~30 B/s. No other per-sample output is allowed.

---

## Task A: combined firmware `arduino/polygraph/polygraph.ino`

Goal: one sketch that streams GSR at exactly 500 Hz and runs the existing BPM detector, emitting the protocol above.

1. Create `arduino/polygraph/polygraph.ino`. The folder name must match the file name, and it must be its own folder.
2. Port the BPM detector from `arduino/legacy/bpm_detector/bpm_detector.ino` **unchanged in behaviour**: same constants, biquads, state machine, `handlePeak()`, and the sample-count time base (`t = sampleCount * SAMPLE_MS`). Remove the `t`/`p`/`q` modes and all `BEAT`/`STATUS`/plot printing. The only BPM output is the 1 Hz `B,...` line, using `state == LOCKED ? bpm : 0`.
3. GSR sampling: keep the `previousMicros += 2000` scheduler from `arduino/legacy/checking_GSR/קוד/checking_GSR/checking_GSR.ino`, but take `t_us = micros()` right before `analogRead`. If the loop falls more than ~10 ms behind (e.g. a long stall), resync `previousMicros = now` instead of bursting catch-up samples, and count that event.
4. Loop structure, in priority order: (a) GSR sample when due; (b) `sensor.check()` plus drain the FIFO **one sample per loop iteration**, so a GSR tick is never delayed by draining a big batch; (c) the 1 Hz B line. No `delay()` anywhere.
5. Use `sensor.begin(Wire, I2C_SPEED_FAST)` (400 kHz). If it fails, print `# MAX3010x not found` and continue in GSR-only mode (`NO_SENSOR`).
6. `setup()` prints `# polygraph ready` once.
7. Timing self-check (optional but wanted): keep a counter of late GSR ticks (> 500 µs late) and of resyncs. Expose them only via a `#` line every 10 s, e.g. `# late=<n> resync=<n>`.
8. RAM: the Uno has 2 KB. Use `F()` for all string literals and don't add buffers.
9. Compile check: find `arduino-cli` (not on PATH; look under `%LOCALAPPDATA%\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe` or similar) and compile with `--fqbn arduino:avr:uno`. Report flash/RAM usage. If the SparkFun library isn't installed, install it with `arduino-cli lib install "SparkFun MAX3010x Pulse and Proximity Sensor Library"`. If no CLI exists, say so; don't fake a compile.
10. Write `arduino/polygraph/README.md`: wiring for both sensors, the protocol table, and hardware notes (shared GND; MAX30102 LED current pulses on the 5 V rail can couple into the GSR reading, see task D).

Done when: it compiles for `arduino:avr:uno`, the code matches the protocol exactly, and the README exists.

---

## Task B: BPM in the app (todo 1 + 2), file `python_app/gsr_polygraph.py`

1. **Parsing.** Replace `parse_sample` with `parse_line(line) -> ("gsr", t_us, raw) | ("bpm", t_us, bpm, state) | ("comment", text)`. Raise `ValueError` for anything else. `detect_arduino()` must accept a port once it has seen ≥3 valid GSR lines; B and `#` lines must not reset the counter. Update the error text and module docstring to describe the protocol. Keep 115200 baud.
2. **Reader thread.** Push `("sample", ...)`, `("bpm", (t_us, bpm, state, received_at))` events. Comments are dropped and are not counted as bad lines.
3. **Time base (important, also fixes a noise source from task D).** The current code timestamps samples with the PC arrival time (`time.monotonic()` when the line is read), but serial data arrives in bursts, so many samples get the same time. Change this:
   - Unwrap `t_us` (32-bit wrap) into a monotonically increasing 64-bit Arduino time. Share one unwrapper between GSR and B lines, because both use the same `micros()`.
   - `time_s` of a sample = `(t_us_unwrapped - anchor_us) / 1e6 + anchor_offset_s`, where the anchor is the first sample received after recording starts, and `anchor_offset_s = received_at_of_that_sample - recording_started_at`.
   - Question times must be on the **same Arduino-based clock** (the Arduino resonator can drift ~0.5% against the PC clock, which is ~1.5 s over 5 min). When marking a question: `time_s = last_sample_time_s + (time.monotonic() - last_sample_received_at)`. Also keep `pc_time_s = time.monotonic() - recording_started_at` for reference.
   - Before the anchor exists (no sample yet), fall back to PC time.
4. **BPM storage.** New dataclass `BpmReading(time_s, pc_time_s, arduino_time_us, bpm, state)`. Record it only while `self.recording`. `time_s` uses the same Arduino clock; `pc_time_s` is the PC receive time relative to `recording_started_at`. This is the "mark the time it was received" in todo 1.
5. **Live display.** On the rest and question screens, show a small label `Heart rate: 72 BPM` (or `--` plus the state when not locked, e.g. `Heart rate: -- (NO_FINGER)`). Update it from the latest reading. If no B line has arrived for >3 s, show `Heart rate: no data`.
6. **CSV.** Save a third file `<stem>_bpm[_PARTIAL].csv` with columns `time_s,pc_time_s,arduino_time_us,bpm,state`. Add `pc_time_s` to the voltage CSV (keep all existing columns, put the new one after `time_s`), and add `pc_time_s` to the questions CSV. `last_saved_paths` becomes a 3-tuple and the saved-message text lists all three files. Keep the header-order changes backward compatible for `plot_examination` (it reads by column name).
7. **Plot (todo 2).** `plot_examination(voltage_csv, questions_csv, bpm_csv=None, subject_name="")`: a 2-row figure with a shared x-axis. Top = filtered GSR (as now, with question lines and labels). Bottom = BPM vs time as a step/line plot, with readings where `bpm == 0` drawn as gaps (NaN), and the same question lines (no labels). If `bpm_csv` is missing, empty, or has no locked readings, still show the bottom axis with a "no heart-rate data" text. Update both call sites.
8. **Verification without hardware.** Write a throwaway script in your scratchpad (not the repo) that feeds synthetic lines (including a `micros()` wrap, B lines, `#` lines and garbage) through `parse_line` and the unwrapper, and writes fake CSVs then calls `plot_examination` with a non-interactive backend (`matplotlib.use("Agg")`, `savefig`). Run `python -m py_compile app/gsr_polygraph.py`. If numpy/scipy/matplotlib/pyserial are missing, install them with `python -m pip install --user numpy scipy matplotlib pyserial` (the app already needs them).

Don't touch the question-answer UI (`show_question_screen`, `mark_question` widgets, `save_answer`). Task C will rewrite it. Only change `mark_question`'s time computation as described in step 3.

---

## Task C: answer UI (todo 3) + integrate D's filter, file `python_app/gsr_polygraph.py`

Runs after B and D. Read the file fresh.

1. Replace the answer `ttk.Entry` with three `ttk.Radiobutton`s bound to a `tk.StringVar`: **Yes**, **No**, **Undefined**. They are disabled until a question is marked.
2. Keyboard, bound on the toplevel window (`self.bind`) so it works regardless of focus, and unbound/ignored outside the question screen:
   - `Return` / `KP_Enter`: mark the question time (same as the button). Ignored while a question is pending.
   - `1` / `2` / `3` (and keypad `KP_1..3`): while a question is pending, immediately save Yes / No / Undefined and move to the next question. No Enter needed. Ignored when nothing is pending.
   - Clicking a radio button also saves immediately, the same as pressing a number.
   - Bindings must be removed when leaving the screen (finish/truth screen), so that Enter on the truth screen doesn't mark questions.
3. Update the instruction text: `Press Enter (or click) when the question is asked. Then press 1 = Yes, 2 = No, 3 = Undefined.` Show the last saved answer in the status label.
4. `finish_measurement()`: a pending question with no answer is handled as now (ask whether to discard it).
5. The truth screen shows `Subject's answer: Yes/No/Undefined`. Also add keyboard shortcuts there: `1` = Truth, `2` = Lie, `Enter` = save and continue.
6. The CSV `subject_answer` column now holds `Yes`/`No`/`Undefined`.
7. **Integrate task D**: replace `apply_50hz_notch_filter` usage with the recommended function from `python_app/gsr_filters.py` (read `python_app/NOISE_ANALYSIS.md` for which one and its signature). Keep the CSV column name `filtered_voltage_v` and update the plot title to describe the new filter. Use the Arduino-measured sample rate if D's function takes `fs`.
8. Verify: `python -m py_compile`, plus a scratchpad script that instantiates nothing GUI-heavy but checks the filter integration on synthetic data. If you can, construct `PolygraphApp` logic paths without a serial connection; at minimum, review the bindings carefully.

---

## Task D: noise diagnostics (todo 4), files `python_app/gsr_filters.py`, `python_app/gsr_diagnostics.py`, `python_app/NOISE_ANALYSIS.md`

Don't edit `python_app/gsr_polygraph.py` (B and C own it). Leads to investigate, verify, and explain in the analysis:

- **Only 50 Hz is notched, but mains noise has harmonics.** At fs = 500 Hz, harmonics 100/150/200/250 Hz are in-band, and higher ones (350, 450, 550 ...) alias onto multiples of 50 Hz. A single notch leaves all of them.
- **GSR is a very slow signal (< ~5 Hz).** A low-pass (e.g. 4th-order Butterworth at ~5 Hz, `sosfiltfilt`) or a 20 ms moving average (exactly one 50 Hz period, which nulls 50 Hz and *all* its harmonics) is much more effective than a notch. Compare these options quantitatively.
- **Time base**: the app currently timestamps samples by PC arrival time, so samples come in bursts with duplicate or uneven times, and the plotted line looks jagged even when the signal isn't. (B fixes this; document it.) The Arduino timestamps (`arduino_time_us`) are the true sample times. Measure the real fs and jitter from them.
- **Quantization**: 10-bit ADC at 5 V means 4.9 mV per LSB. Show the histogram of codes; ±1-2 LSB toggling looks like noise. Averaging/oversampling helps.
- **Reference/supply noise**: the ADC reference is AVcc = USB 5 V (noisy, laptop charger ground loops). With the MAX30102 on the same board, its LED current pulses (up to 50 mA at 400 Hz) can ripple the rail; 400 Hz sampled at 500 Hz aliases to 100 Hz. List hardware mitigations (common GND star point, 100 nF + 10 µF decoupling at the sensor, an RC low-pass before A0, laptop on battery, a lower LED_BRIGHTNESS, a separate 3.3 V supply for the MAX30102).
- **Motion/electrode artifacts and the analog front end**: describe these briefly as non-mains causes. Check `arduino/legacy/checking_GSR/קוד/gsr_checking.m` for what has already been tried.
- Read `python_app/gsr_polygraph.py` and the GSR sketch for anything else that could cause noise, and report it.

Deliverables:
1. `python_app/gsr_filters.py`: pure functions on numpy arrays, no GUI. At least `notch_50(values, fs)`, `comb_notch_mains(values, fs, f0=50, harmonics=...)`, `lowpass(values, fs, cutoff_hz=5, order=4)` (zero-phase), `moving_average_mains(values, fs, f0=50)`, and **`clean_gsr(values, fs) -> np.ndarray`** (the recommended default, documented in its docstring). Also `estimate_fs(arduino_time_us)` (handles 32-bit wrap; returns the median-based fs). All functions return input unchanged (as an array) for too-short inputs, like the current `apply_50hz_notch_filter`.
2. `python_app/gsr_diagnostics.py`: a CLI. `python app/gsr_diagnostics.py <voltage.csv>` reads the app's voltage CSV (columns `time_s`, `arduino_time_us`, `raw_adc`, `voltage_v`; tolerate missing optional columns). It produces one multi-panel figure, shown or saved with `--save out.png`:
   - raw vs each filter option (time domain, plus a zoomed 2 s window)
   - Welch PSD on a log scale, 0-250 Hz, with 50 Hz harmonics marked
   - spectrogram
   - histogram of sample intervals from `arduino_time_us` (jitter, dropped samples), and of PC arrival intervals if `time_s` came from the PC
   - ADC code histogram
   
   It also prints a text report: measured fs, interval stats, dropped-sample count, the RMS in the bands 0-5 Hz / each 50·k Hz ±2 Hz / everything else, and the noise RMS (signal minus a 0.5 s median) before and after each filter, with a recommendation.
   
   `--selftest` generates synthetic data (a slow GSR drift, 50 Hz plus harmonics, white noise, quantization to 10 bits, timestamp jitter, and one 32-bit `micros()` wrap) and runs the whole report on it with the Agg backend. Optional `--port COMx --seconds N` live-captures N seconds from the Arduino (GSR lines only; ignore `B,` and `#` lines) and analyses it.
3. `python_app/NOISE_ANALYSIS.md`: findings, ranked by likely impact, each with evidence (cite code lines), how to confirm with the diagnostics tool, and the fix (software and hardware). End with the recommended `clean_gsr` choice and its exact signature, for task C.

Verify: run `--selftest` with `--save` into your scratchpad and look at the PNG (use the Read tool on it). Install deps with `python -m pip install --user numpy scipy matplotlib pyserial` if missing.
