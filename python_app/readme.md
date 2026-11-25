this prototype assumes ideal conditions.

Full documentation (serial protocol, saved CSV columns, GSR and heart-rate filters): [docs/](../docs/README.md).

Install dependencies with `pip install -r python_app/requirements.txt`, then run `python python_app/main.py`. Packaged Windows builds: [docs/releasing.md](../docs/releasing.md).

The questions and all other settings are in `config.toml`. Restart the app after editing it.

An examination is saved only when it is finished (see below), to `measurements/`: two CSVs of its own (voltage with skin conductance, and heart rate), its questions and answers added to the shared `questions.csv`, and an image of the examination graph, `<subject>_<date>_<time>_plot.png`. Details: [docs/csv-files.md](../docs/csv-files.md).

## During an examination

- **Marks**: for each question press Space or Enter (or click the button) when you **start** asking it (question start mark), again when you **finish** asking (question end mark), then the answer key (1, 2, 3, ...) when the subject answers (answer mark). The banner and the status line say which press comes next. Answer keys pressed before the end mark are ignored. All three times are saved.
- **Backspace** undoes an answer: it reopens the last answered question so it can be answered again (its start and end marks stay, its answer mark is cleared). It does not undo a Space press, so press Space carefully.
- **Since last answer**: the counter under the banner shows the time since the last answer mark. Wait until it says "ready" (`min_question_gap_seconds`) before you start the next question, so the skin conductance can recover. It is blank before the first answer and while a question is in progress.
- **Heart rate**: if the heart rate loses its lock, the last locked BPM stays on screen in grey, marked "(holding)", for up to `bpm_display_hold_seconds`. Only the screen holds it; the saved heart-rate data are exactly what the sensor sent. "No data" and a missing sensor show at once. Ask the subject to keep the hand with the heart-rate sensor still.

## Finishing and saving

- The examination finishes when the last question is answered, when the question timer runs out, or with Finish examination (F, asks for confirmation). You then classify each answer as Truth or Lie, and the files are saved.
- After saving, the results screen shows the summary and the graph. **Home (H)** reconnects to the Arduino and goes back to the setup screen for the next examination; **Close (Esc)** quits.
- If no question was answered, nothing is saved and the app returns to the setup screen.
- Closing the window before the examination is finished discards it, after asking "The examination will be discarded. Quit anyway?".
- If `questions.csv` is open in Excel, the app asks you to close it and press Retry. Cancel saves the questions to a separate `<subject>_<date>_<time>_questions.csv` instead.

## Looking at a saved examination

**Open recording…** on the setup screen, and also on the connecting and connection-error screens so it works without an Arduino, opens any saved `*_voltage.csv` and shows it in a separate window laid out like the results screen: the question table (text and answer from the questions file) to the left of the graph, with the same mark check boxes and a Save PNG button. Esc closes that window (the app keeps running).

Letter shortcuts (H, F) go by the physical key, so they also work with a Hebrew keyboard layout active.

## Arduino connection

- The app connects by itself. If the Arduino is not found (at launch, or if the cable comes out before an examination), the error screen stays up and the app keeps looking for it every `reconnect_interval_seconds`. "Try again now" retries at once.
- If the connection is lost during an examination (rest period or questions), a "Connection lost" window appears and the examination is **paused**: the countdown, the "Since last answer" counter and all keys stop, and a question in progress stays where it was. Plug the cable back in: the app reconnects, the window closes and the examination continues where it stopped. The saved data keep real time, so the outage appears as a gap with no samples.
- "End examination" in that window gives up on reconnecting and finishes the examination the same way as Finish examination (F): you go on to classify the answers and the data recorded so far are saved.
