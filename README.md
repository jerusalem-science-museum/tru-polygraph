# Polygraph exhibit

A science-museum "polygraph" exhibit. A visitor rests their fingers on two GSR electrodes and a MAX30102 heart-rate sensor. An Arduino streams skin conductance and heart rate over USB to a desktop app. The operator marks each question and answer, and the app saves the session as CSVs plus a graph.

## Repository layout

- [arduino/polygraph/](arduino/polygraph/): the exhibit firmware (GSR at 500 Hz plus the heart-rate detector). [arduino/legacy/](arduino/legacy/) holds older standalone sketches, kept for reference.
- [python_app/](python_app/): the operator's desktop app.
- [docs/](docs/): the full documentation.

## Quick start

1. Flash the firmware: see [arduino/polygraph/README.md](arduino/polygraph/README.md) for wiring, the required library and upload steps.
2. Run the app:

   ```
   pip install -r python_app/requirements.txt
   python python_app/main.py
   ```

   Or download the Windows exe from the GitHub releases page ([docs/releasing.md](docs/releasing.md)).

Recordings are saved to `python_app/measurements/` (or `measurements/` next to the exe).

## Documentation

Start at [docs/README.md](docs/README.md). It has a plain-language overview of the exhibit for museum staff and an index of the technical documents: the serial protocol, the saved CSV files, the GSR and heart-rate filters, and how to publish a release.
