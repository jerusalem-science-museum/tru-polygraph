# Saved CSV files

Source of truth: [python_app/records.py](../python_app/records.py) `save_examination()`, with the time base in [python_app/polygraph_app.py](../python_app/polygraph_app.py) (`handle_sample()`, `handle_bpm()`, `current_times()`).

**In short:** each examination produces three spreadsheet files with the same name prefix. One holds the skin-conductance signal (500 rows per second), one holds the questions and answers, and one holds the heart rate (one row per second). They share a time column, `time_s`, so the files can be lined up.

## Where and how files are named

Folder: `python_app/measurements/` (`OUTPUT_FOLDER`). The folder is gitignored except for `.gitkeep`.

```
<subject>_<YYYY-MM-DD>_<HH-MM-SS>_voltage[_PARTIAL].csv
<subject>_<YYYY-MM-DD>_<HH-MM-SS>_questions[_PARTIAL].csv
<subject>_<YYYY-MM-DD>_<HH-MM-SS>_bpm[_PARTIAL].csv
```

- `<subject>` is the name typed on the setup screen, passed through `safe_filename()`. Hebrew and other Unicode letters are kept. The characters `< > : " / \ | ? *` and control characters become `_`, and trailing dots and spaces are removed. An empty result becomes `subject`.
- The timestamp is the PC's local time when **Start examination** was pressed (`datetime.now().strftime("%Y-%m-%d_%H-%M-%S")`).
- **`_PARTIAL`** is added when the recording did not end normally:
  - the Arduino connection was lost (serial error, or no valid GSR line for 2.0 s while recording, see `handle_serial_error()`), or
  - the window was closed during the rest or question phase and the operator chose **Yes** (save) in the dialog (`on_close()`).

  A partial save contains everything recorded up to that point. A question that was marked but not yet answered is left out. `answer_type` is empty because the Truth/Lie classification only happens after a normal finish.
- A normal finish (timer runs out, or **Finish examination** followed by Truth/Lie classification and **Save examination**) writes the files without a suffix. If no question was saved, the app asks whether to save the recording anyway. If the operator agrees, the questions file has only its header row.

All three files are written with `csv.writer`, encoded as UTF-8 **with BOM** (`utf-8-sig`) so Excel shows Hebrew names correctly, with a header row and comma separators. Floating-point times and voltages have 6 decimals.

## The three time columns

Every file has an Arduino-based time and a PC time. Both are in seconds since the recording started.

| Column | Clock | How it is computed | Use it for |
|---|---|---|---|
| `time_s` (`question_time_s`, `answer_time_s` in the questions file) | **Arduino** `micros()`, anchored to the PC at the first sample | `(unwrapped_us − anchor_us) / 1e6 + anchor_offset_s`. `anchor_us` is the unwrapped `micros()` of the first GSR sample processed after Start, and `anchor_offset_s` is that sample's PC arrival time (its `pc_time_s`). | **All analysis and alignment.** Sample spacing is the true 2 ms acquisition spacing, unaffected by USB buffering. |
| `pc_time_s` (`answer_pc_time_s`) | PC `time.monotonic()` | arrival time of the line (or key-press time for questions) − time Start was pressed | Reference and debugging only |
| `arduino_time_us` | raw Arduino `micros()` | the 32-bit `t_us` exactly as sent, **not unwrapped** | Re-deriving timing, measuring the real sample rate, checking for gaps |

How they relate:

- **The Arduino clock is the master.** USB-serial adapters deliver data in bursts of several ms, so `pc_time_s` repeats or jumps in steps, for example several consecutive samples with the same `pc_time_s`. `time_s` advances by about 0.002 s per row.
- **At the anchor, `time_s == pc_time_s`.** After that they drift apart at the rate of the Arduino's clock error, because its ceramic resonator is typically a few tenths of a percent off. In the recordings in `measurements/` the difference was about 0.3–0.5 % (for example 59.70 s vs 60.02 s at the end of a 60 s recording). Don't mix the two clocks in one analysis.
- **The first rows can have small negative times** (about −0.015 to −0.05 s). Samples that arrived just before Start was pressed were still in the app's queue and are counted as part of the recording, with their arrival time before the start.
- **Questions use the same Arduino-based scale.** At a key press, `current_times()` returns `last_sample_time_s + (now − last_sample_received_at)`, the `time_s` of the latest sample plus the PC time elapsed since it arrived. The difference between the USB latency of that sample and of the anchor sample limits the accuracy to a few ms (up to about 16 ms), which is negligible for GSR responses that last seconds.
- **Fallback.** Before the first GSR sample of a recording has arrived (`anchor_us is None`), question and BPM times use PC time for both columns.
- **Unwrapping `arduino_time_us` yourself.** It wraps every 2^32 µs ≈ 71.6 min. Use `gsr_filters.unwrap_micros()`, which accumulates forward differences modulo 2^32.

## `*_voltage.csv` — GSR samples

One row per GSR sample received while recording, about 500 rows per second (`Sample` dataclass).

| Column | Meaning | Units | How it is computed |
|---|---|---|---|
| `time_s` | Arduino-based sample time since recording start | s | see [time columns](#the-three-time-columns). Uses the `t_us` taken just before `analogRead`. |
| `pc_time_s` | PC arrival time since recording start | s | `received_at − recording_started_at` |
| `arduino_time_us` | Raw `micros()` from the GSR line | µs (wraps at 2^32) | copied from the serial line |
| `raw_adc` | ADC reading of `A0` | counts, 0–1023 | copied from the serial line |
| `voltage_v` | Sensor output voltage | V, 0–5 | `raw_adc × ADC_REFERENCE_VOLTS / ADC_MAX` = `raw_adc × 5.0 / 1023`. Resolution is about 4.89 mV per count. The 5.0 V reference is nominal: the real reference is the USB 5 V rail, so absolute volts are only as accurate as that rail. |
| `filtered_voltage_v` | Cleaned GSR voltage, the trace plotted by the app | V | `gsr_filters.clean_recording(voltage_v, 500, arduino_time_us)` over the **whole recording at save time**: a 20 ms mains-cancelling moving average followed by a zero-phase 5 Hz low-pass, with resampling over gaps. It is aligned row for row with `voltage_v`. Recordings shorter than 30 samples are copied unfiltered. See [filters-gsr.md](filters-gsr.md). |

Edge cases:

- `raw_adc = 0` means electrode contact was lost. The app shows a red warning after 0.10 s of zeros (`ZERO_WARNING_AFTER_SECONDS`), but the zeros are still saved.
- Gaps (missed samples after a firmware resync) appear as a jump of more than 2000 µs in `arduino_time_us`. No rows are invented. The filter interpolates internally only.
- Because filtering runs once over the full file, `filtered_voltage_v` near the very start and end depends on edge padding (odd reflection, 1 s pad). It is accurate to well under 1 count on slow signals (see [NOISE_ANALYSIS.md](../python_app/NOISE_ANALYSIS.md), "Distortion checks").
- Older files without `filtered_voltage_v` are filtered on the fly by `plotting.py` when plotted.

## `*_questions.csv` — question marks and answers

One row per saved question (`Question` dataclass), in the order they were saved.

| Column | Meaning | Units / values | How it is computed |
|---|---|---|---|
| `question_time_s` | When the operator pressed Space/Enter (or clicked) to mark that the question was asked | s, Arduino-based scale | `current_times()[0]` at the mark |
| `pc_time_s` | Same moment on the PC clock | s | `current_times()[1]` |
| `question_number` | Sequential number | 1, 2, 3, … | `len(questions) + 1` at the mark. A question reopened with Backspace keeps its number and its original mark time. |
| `subject_answer` | What the visitor answered | `Yes`, `No`, `Undefined` (`ANSWER_CHOICES`, keys 1/2/3) | set when the answer key is pressed |
| `answer_type` | Operator's classification after the examination | `Truth`, `Lie`, or empty | set on the classification screen (keys 1/2). It is empty in `_PARTIAL` files. Despite the name, it is the ground truth (truth or lie), not the kind of answer. |
| `answer_time_s` | When the answer key was pressed | s, Arduino-based scale; empty if missing | `current_times()[0]` at the answer. If the operator presses Backspace to reopen the question, it is reset and recorded again at the new answer. |
| `answer_pc_time_s` | Same moment on the PC clock | s; empty if missing | `current_times()[1]` |

`answer_time_s − question_time_s` is how long the operator took to enter the answer, not the visitor's reaction time. Use `question_time_s` as the stimulus time when looking for GSR responses (typically 1–3 s later).

## `*_bpm.csv` — heart rate

One row per `B` line received while recording, about 1 per second (`BpmReading` dataclass). Values are stored exactly as the firmware sent them. The app doesn't filter heart rate.

| Column | Meaning | Units / values | How it is computed |
|---|---|---|---|
| `time_s` | Arduino-based time the `B` line was **sent** | s | same anchor formula as the voltage file, using the `B` line's `t_us` |
| `pc_time_s` | PC arrival time | s | `received_at − recording_started_at` |
| `arduino_time_us` | Raw `micros()` from the `B` line | µs (wraps at 2^32) | copied |
| `bpm` | Heart rate | BPM, integer; `0` = no valid reading | copied. It is non-zero only when `state` is `LOCKED` (40–180 in practice). It is the median of the last ≤ 8 beat intervals, so it reflects roughly the preceding 4–8 beats, not the instant of `time_s`. |
| `state` | Detector state | `NO_FINGER`, `SETTLING`, `ACQUIRING`, `LOCKED`, `NO_SENSOR` | copied, see the state table in [serial-protocol.md](serial-protocol.md) |

In the graph (`plotting.py`), `bpm = 0` is drawn as a gap, values are drawn as steps (`drawstyle="steps-post"`), and "No heart-rate data" is shown if there is no non-zero value.
