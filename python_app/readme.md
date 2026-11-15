this prototype assumes ideal conditions.

Full documentation (serial protocol, saved CSV columns, GSR and heart-rate filters): [docs/](../docs/README.md).

Install dependencies with `pip install -r python_app/requirements.txt`, then run `python python_app/main.py`. Packaged Windows builds: [docs/releasing.md](../docs/releasing.md).

The questions and all other settings are in `config.toml`. Restart the app after editing it.

Each finished examination is saved to `measurements/` as three CSVs (voltage, questions, heart rate) plus an image of the examination graph, `<subject>_<date>_<time>_plot.png`.

## During an examination

- **Question and answer marks**: press Space or Enter when you finish asking a question (question mark), then the answer key (1, 2, 3, ...) when the subject answers (answer mark). Both times are saved. Backspace reopens the last question and clears its answer mark, so the new answer gets a new one.
- **Since last answer**: the counter under the banner shows the time since the last answer mark. Wait until it says "ready" (`min_question_gap_seconds`) before asking the next question, so the skin conductance can recover. It is blank before the first answer and while an answer is pending.
- **Heart rate**: if the heart rate loses its lock, the last locked BPM stays on screen in grey, marked "(holding)", for up to `bpm_display_hold_seconds`. Only the screen holds it; the saved heart-rate data are exactly what the sensor sent. "No data" and a missing sensor show at once. Ask the subject to keep the hand with the heart-rate sensor still.

## Arduino connection

- The app connects by itself. If the Arduino is not found (at launch, or if the cable comes out before an examination), the error screen stays up and the app keeps looking for it every `reconnect_interval_seconds`. "Try again now" retries at once.
- If the connection is lost during an examination (rest period or questions), a "Connection lost" window appears and the examination is **paused**: the countdown, the "Since last answer" counter and all keys stop, and a question waiting for its answer stays waiting. Plug the cable back in: the app reconnects, the window closes and the examination continues where it stopped. The saved data keep real time, so the outage appears as a gap with no samples.
- "End examination" in that window gives up on reconnecting and finishes the examination the same way as Finish examination (F): the data recorded so far are saved and you go on to classify the answers.
