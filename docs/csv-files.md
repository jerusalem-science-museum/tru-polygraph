# Saved CSV files

Source of truth: [python_app/records.py](../python_app/records.py) (`save_examination()`, `question_rows()`, `append_shared_questions()`, `write_fallback_questions()`), with the time base in [python_app/main.py](../python_app/main.py) (`handle_sample()`, `handle_bpm()`, `current_time_s()`) and the save flow in `PolygraphApp.save_files()`.

**In short:** each examination produces two spreadsheet files of its own with the same name prefix: one holds the skin-conductance signal (500 rows per second) and one holds the heart rate (one row per second). Its questions and answers are added as rows to one shared file, `questions.csv`, which collects the questions of every examination. All times are in seconds since that examination's recording started, on one shared clock, so the files can be lined up. The `file_name` column of `questions.csv` says which examination a row belongs to.

## Where and how files are named

Folder: `python_app/measurements/` (`OUTPUT_FOLDER`; `measurements/` next to the exe in a packaged build). The folder is gitignored except for `.gitkeep`.

```
<subject>_<YYYY-MM-DD>_<HH-MM-SS>_voltage.csv     one per examination
<subject>_<YYYY-MM-DD>_<HH-MM-SS>_bpm.csv         one per examination
questions.csv                                      shared: one row per answered question of every examination
<subject>_<YYYY-MM-DD>_<HH-MM-SS>_questions.csv   only if questions.csv could not be written (see below)
```

- `<subject>_<YYYY-MM-DD>_<HH-MM-SS>` is the examination's **stem**. It is also the `file_name` value in `questions.csv`.
- `<subject>` is the name typed on the setup screen, passed through `safe_filename()`. Hebrew and other Unicode letters are kept. The characters `< > : " / \ | ? *` and control characters become `_`, and trailing dots and spaces are removed. An empty result becomes `subject`.
- The timestamp is the PC's local time when **Start examination** was pressed (`datetime.now().strftime("%Y-%m-%d_%H-%M-%S")`).

## When an examination is saved

An examination is saved **only when the operator finishes it**: the last question is answered, the question-phase timer runs out, **Finish examination (F)** is confirmed, or **End examination** is pressed in the "Connection lost" window, which finishes the same way as F. The operator then classifies every answer as Truth or Lie, and the files are written as soon as the last answer is classified. Older versions saved `_PARTIAL` files after a connection loss or when the window was closed; that no longer happens.

- A question that was started but not answered when the examination is finished is left out, after the app asks for confirmation.
- If no question was answered, nothing is saved: the app says "No questions were answered, so nothing was saved." and returns to the setup screen.
- Closing the window during the rest period, the questions or the Truth/Lie classification asks "The examination will be discarded. Quit anyway?". **Yes** quits without writing anything; **No** returns to the examination.

After saving, the app shows the examination graph beside the summary table (`graph_view.py`, drawn by `plotting.py`): skin conductance (`conductance_us`, µS) in the top panel and heart rate in the bottom panel, on a shared time axis.

- Each question can be marked on both panels at its question start mark (dotted line), question end mark (dashed) and answer mark (solid). Three check boxes above the graph switch each kind on and off; only the question end mark is shown at first.
- Lines are coloured by the truth classification (green Truth, red Lie, grey otherwise). Each question is labelled once, `Question <n> — <answer_truth>`, on the latest mark kind shown.
- Questions come from the rows of `questions.csv` whose `file_name` is the recording's stem, or from `<stem>_questions.csv` if there are none there. With no questions the graph is drawn without marks.
- The default view is saved automatically as `<stem>_plot.png` (150 dpi). **Save PNG** saves the current view as `<stem>_plot_<kinds>.png` (for example `_plot_start-end-answer.png`, or `_plot_no-marks.png`) and never overwrites `<stem>_plot.png`.
- **Open recording…** on the setup screen (and on the connecting and connection-error screens, so no Arduino is needed) shows the same view for any saved `*_voltage.csv`. Only files in the current format can be opened (they need the `conductance_us` column).
- Hebrew subject names in the title are reordered for display with `python-bidi`, because matplotlib draws text in logical order.

### How questions.csv is written

- If `questions.csv` doesn't exist (or is empty), it is created with the header row. Otherwise the examination's rows are appended, after checking that the file's header row is exactly the columns [below](#questionscsv--questions-and-answers-of-every-examination). All rows of one examination go in one write, so a failure never leaves half of them.
- If the file can't be written, typically because it is open in Excel (which locks it), the app shows "questions.csv is open in another program (e.g. Excel). Close it and press Retry." **Retry** tries again. **Cancel** writes this examination's rows, with the same header, to `<stem>_questions.csv` in the same folder and tells the operator where they went. The same Retry/Cancel choice is offered if the existing file has other columns (for example one made by hand); such a file is never changed. The rows of a fallback file can be copied into `questions.csv` by hand later. The graph finds an examination's questions in either file.
- The voltage and heart-rate files are written first, so a problem with `questions.csv` never loses the recording.

`*_voltage.csv`, `*_bpm.csv` and a new `questions.csv` are written with `csv.writer`, encoded as UTF-8 **with BOM** (`utf-8-sig`) so Excel shows Hebrew correctly, with a header row and comma separators. Rows appended to an existing `questions.csv` are plain UTF-8, because the file already starts with its BOM. Floating-point times, voltages and conductances have 6 decimals.

## The three time columns

The voltage and heart-rate files have an Arduino-based time and a PC time, both in seconds since the recording started. The question times in `questions.csv` are on the Arduino-based scale only.

| Column | Clock | How it is computed | Use it for |
|---|---|---|---|
| `time_s` (and `question_start_time_s`, `question_end_time_s`, `answer_time_s` in `questions.csv`) | **Arduino** `micros()`, anchored to the PC at the first sample | `(unwrapped_us − anchor_us) / 1e6 + anchor_offset_s`. `anchor_us` is the unwrapped `micros()` of the first GSR sample processed after Start, and `anchor_offset_s` is that sample's PC arrival time (its `pc_time_s`). | **All analysis and alignment.** Sample spacing is the true 2 ms acquisition spacing, unaffected by USB buffering. |
| `pc_time_s` | PC `time.monotonic()` | arrival time of the line − time Start was pressed | Reference and debugging only |
| `arduino_time_us` | raw Arduino `micros()` | the 32-bit `t_us` exactly as sent, **not unwrapped** | Re-deriving timing, measuring the real sample rate, checking for gaps |

How they relate:

- **The Arduino clock is the master.** USB-serial adapters deliver data in bursts of several ms, so `pc_time_s` repeats or jumps in steps, for example several consecutive samples with the same `pc_time_s`. `time_s` advances by about 0.002 s per row.
- **At the anchor, `time_s == pc_time_s`.** After that they drift apart at the rate of the Arduino's clock error, because its ceramic resonator is typically a few tenths of a percent off. In the recordings in `measurements/` the difference was about 0.3–0.5 % (for example 59.70 s vs 60.02 s at the end of a 60 s recording). Don't mix the two clocks in one analysis.
- **The first rows can have small negative times** (about −0.015 to −0.05 s). Samples that arrived just before Start was pressed were still in the app's queue and are counted as part of the recording, with their arrival time before the start.
- **Question marks use the same Arduino-based scale.** At a key press, `current_time_s()` returns `last_sample_time_s + (now − last_sample_received_at)`, the `time_s` of the latest sample plus the PC time elapsed since it arrived. The difference between the USB latency of that sample and of the anchor sample limits the accuracy to a few ms (up to about 16 ms), which is negligible for GSR responses that last seconds.
- **Fallback.** Before the first GSR sample of a recording has arrived (`anchor_us is None`), question and BPM times use PC time.
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
| `filtered_voltage_v` | Cleaned GSR voltage | V | `gsr_filters.clean_recording(voltage_v, 500, arduino_time_us)` over the **whole recording at save time**: a 20 ms mains-cancelling moving average followed by a zero-phase 5 Hz low-pass, with resampling over gaps. It is aligned row for row with `voltage_v`. Recordings shorter than 30 samples are copied unfiltered. See [filters-gsr.md](filters-gsr.md). |
| `conductance_us` | Skin conductance, the trace plotted by the app | µS (microsiemens) | `gsr_filters.conductance_us(filtered_voltage_v)` = `V / (10^6 − V × 10^5) × 10^6`, with `V` = `filtered_voltage_v`. The formula comes from the sensor circuit. |

Edge cases:

- `raw_adc = 0` means electrode contact was lost. The app shows a red warning after 0.10 s of zeros (`ZERO_WARNING_AFTER_SECONDS`), but the zeros are still saved.
- Gaps (missed samples after a firmware resync) appear as a jump of more than 2000 µs in `arduino_time_us`. No rows are invented. The filter interpolates internally only.
- Because filtering runs once over the full file, `filtered_voltage_v` near the very start and end depends on edge padding (odd reflection, 1 s pad). It is accurate to well under 1 count on slow signals (see [NOISE_ANALYSIS.md](../python_app/NOISE_ANALYSIS.md), "Distortion checks").

## `questions.csv` — questions and answers of every examination

One row per answered question (`Question` dataclass), appended when each examination is saved, in question order. A fallback `<stem>_questions.csv` has the same columns. The columns are `QUESTIONS_COLUMNS` in `records.py`.

Each question has three moments, its **marks** (see [GLOSSARY.md](../GLOSSARY.md)):

- **Question start mark**: the operator starts asking the question and presses Space/Enter (or clicks) the first time.
- **Question end mark**: the operator finishes asking and presses Space/Enter (or clicks) again.
- **Answer mark**: the operator records the subject's answer by pressing an answer key (1/2/3). The subject answers between the end mark and the answer mark, so the answer mark is the latest moment the answer can have been given.

| Column | Meaning | Units / values | How it is computed |
|---|---|---|---|
| `file_name` | The examination this row belongs to | stem `<subject>_<YYYY-MM-DD>_<HH-MM-SS>` | Its recording is `<file_name>_voltage.csv` and `<file_name>_bpm.csv`. |
| `question_number` | Position in the question list | 1, 2, 3, … | `len(questions) + 1` at the start mark. A question reopened with Backspace keeps its number and its start and end marks. |
| `question_text` | The wording that was asked | text | `questions[question_number − 1]` from `config.toml` at save time |
| `subject_answer` | What the visitor answered | `Yes`, `No`, `Undefined` (`ANSWER_CHOICES`, keys 1/2/3) | set when the answer key is pressed |
| `answer_truth` | Operator's truth classification after the examination | `Truth`, `Lie` | set on the classification screen (keys 1/2) |
| `detected_truth` | The polygraph's own detection | empty | Reserved for a detection algorithm; always empty for now. |
| `question_start_time_s` | Question start mark | s, Arduino-based scale | `current_time_s()` at the first Space |
| `question_end_time_s` | Question end mark | s, Arduino-based scale | `current_time_s()` at the second Space |
| `answer_time_s` | Answer mark | s, Arduino-based scale | `current_time_s()` at the answer key |

The times are relative to that row's own examination, so they can only be compared with times in the files of the same `file_name`. GSR responses typically start 1–3 s after the stimulus.

Files from older versions (`<stem>_questions.csv` with `question_time_s`, `pc_time_s`, `answer_type`, ...) are not read by the app.

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

A **sensor outage** is a run of consecutive rows with `state = NO_SENSOR`. The graph shades it from the first `NO_SENSOR` row to the next row in another state (or to the last row), labelled "sensor disconnected". Other not-locked states (`NO_FINGER`, `SETTLING`, `ACQUIRING`) are plain gaps, not shaded. A connection loss to the Arduino is not shaded: it simply has no rows.
