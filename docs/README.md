# Polygraph exhibit documentation

This folder documents the science-museum "polygraph" exhibit. The first half of this page is a plain-language overview for museum staff and educators. The second half is an index of the technical documents for the development team.

## Overview
### What the exhibit does

A visitor rests two fingers on two small electrodes and one fingertip on a small light sensor. While an operator asks questions, the exhibit records two body signals that change when a person is stressed or excited. After the session it draws a graph with every question marked on it, so visitors can see how their body reacted.

### The two signals

**Skin conductance (GSR, "galvanic skin response").** The skin always carries a tiny amount of sweat, even when you do not feel sweaty. When the body becomes alert or stressed, the sweat glands in the fingers become slightly more active within a second or two. Sweat contains salt and conducts electricity, so the skin conducts a very small current a little more easily. The two finger electrodes pass a harmless, tiny current through the skin and measure how easily it flows. The reaction is slow: it rises over one to a few seconds and then fades over several seconds. That is why the operator waits about 10 seconds between questions.

**Heart rate (BPM, beats per minute).** The light sensor shines invisible infrared light into the fingertip. Each heartbeat pushes a small wave of blood into the finger, and the blood absorbs a little more light, so the amount of light coming back dips once per beat. The exhibit counts these dips and reports the heart rate. The heart tends to speed up a little under stress or excitement.

### Why a polygraph measures these

Both signals are controlled by the part of the nervous system that we do not steer consciously (the "fight or flight" system). A surprising, embarrassing or stressful question can cause a small sweat response and a change in heart rate, even if the person stays calm on the outside. A polygraph records these reactions next to the moment each question was asked.

Note for educators: a reaction shows arousal (surprise, worry, embarrassment, excitement), not lying. A truthful person can react strongly to an uncomfortable question, and a calm liar may not react at all. This is a good point to discuss with visitors: the graph shows the body's reaction, and people interpret it.

### How the data flows

```
 finger electrodes ──> GSR sensor ──┐
                                     ├──> Arduino ──USB cable──> laptop app ──> files (CSV) + graph
 fingertip ──> heart-rate sensor ───┘
```

1. **Sensors.** The GSR sensor turns skin conductance into a voltage. The heart-rate sensor measures reflected infrared light.
2. **Arduino** (a small microcontroller board). It reads the GSR voltage 500 times per second. It reads the light sensor 100 times per second, finds the heartbeats itself, and works out the heart rate once per second.
3. **USB cable.** The Arduino sends all of this to the laptop as lines of text.
4. **Laptop app.** The operator types the visitor's name, waits through a short rest period, then presses a key each time a question is asked and another key for the visitor's answer (Yes / No / Undefined). At the end the operator marks each answer as Truth or Lie.
5. **Files and graph.** The app saves three spreadsheet files (CSV) and shows a graph. The top graph is the skin-conductance signal after electrical interference is cleaned out, and the bottom graph is the heart rate. Each question is a dashed line: green for Truth, red for Lie, grey if it was not classified.

### Why the signal needs "cleaning"

Electrical wiring in the building hums at 50 times per second (50 Hz), and the wires and the visitor's body pick some of it up, much like a radio picks up static. Skin-conductance reactions are slow (they take seconds), while the hum and other interference are fast. The app removes the fast parts and keeps the slow ones. The heart-rate sensor does something similar: it keeps only changes at heartbeat speed and ignores the steady light level and slow drifts from breathing or finger pressure.

## Technical documentation

| Document | Contents |
|---|---|
| [serial-protocol.md](serial-protocol.md) | Every line the firmware sends over USB serial (GSR samples, `B,...` heart-rate lines, `#` comments, test-mode output), with each field's meaning, units, range, rate and edge cases |
| [csv-files.md](csv-files.md) | The files the app saves (`*_voltage.csv`, `*_questions.csv`, `*_bpm.csv`, and `_PARTIAL` variants): filename rules, every column, and how the three time columns relate |
| [filters-gsr.md](filters-gsr.md) | The GSR cleaning chain in `gsr_filters.py` (20 ms mains-cancelling average plus a 5 Hz zero-phase low-pass), with parameters and the reasons for them |
| [filters-heart-rate.md](filters-heart-rate.md) | The firmware heart-rate chain: finger detection, 0.7–3.5 Hz band-pass, inversion, adaptive-threshold peak detection, interval validation, and the lock / re-acquire state machine, plus what the app does with BPM |
| [releasing.md](releasing.md) | How to publish a release so GitHub builds the Windows exe (and optionally Linux), where to find it, first-run notes, and where measurements are saved |

Related documents elsewhere in the repo:

- [arduino/polygraph/README.md](../arduino/polygraph/README.md): firmware build, wiring, test mode, timing and hardware notes.
- [python_app/NOISE_ANALYSIS.md](../python_app/NOISE_ANALYSIS.md): the investigation of GSR noise that led to the current filters. It is a historical report and quotes older file names and line numbers.
- [arduino/legacy/bpm_detector/README.md](../arduino/legacy/bpm_detector/README.md): the standalone heart-rate sketch that the firmware's detector was ported from.

### Code map

| Part | File |
|---|---|
| Firmware (GSR at 500 Hz plus heart-rate detector) | [arduino/polygraph/polygraph.ino](../arduino/polygraph/polygraph.ino) |
| Serial parsing, `micros()` unwrapping, port detection | [python_app/arduino.py](../python_app/arduino.py) |
| GUI, time base, question marking | [python_app/main.py](../python_app/main.py) |
| CSV writing | [python_app/records.py](../python_app/records.py) |
| GSR filters | [python_app/gsr_filters.py](../python_app/gsr_filters.py) |
| End-of-session graph | [python_app/plotting.py](../python_app/plotting.py) |
| Offline and live noise diagnostics (not used by the app) | [python_app/gsr_diagnostics.py](../python_app/gsr_diagnostics.py) |
