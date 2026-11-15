"""Plot a saved examination: GSR voltage and heart rate with every answer mark drawn.

Each question is drawn as a dashed line at its answer mark (the operator pressed the answer
key), coloured by the truth classification. Question marks are not drawn. Older questions
files without the `answer_time_s` column fall back to the question mark. Stretches where the
heart-rate sensor did not answer (state NO_SENSOR) are shaded in the heart-rate panel.
"""

from __future__ import annotations

import csv
import warnings
from pathlib import Path

import gsr_filters
from arduino import SAMPLE_RATE_HZ

# Matplotlib warns if a redraw happens while the pane is very narrow, e.g. mid-resize. Harmless.
warnings.filterwarnings("ignore", message="Tight layout not applied")


def plot_examination(
    voltage_csv: str | Path,
    questions_csv: str | Path,
    bpm_csv: str | Path | None = None,
    subject_name: str = "",
):
    """Build the GSR and heart-rate figure with every answer mark drawn, and save it as a PNG.

    Answer marks are drawn on both panels and labelled with the question number and its
    Truth/Lie classification (falling back to the question mark for older files without
    `answer_time_s`). Sensor outages (bpm `state` == NO_SENSOR) are shaded in the heart-rate
    panel; not-locked readings are drawn as gaps.

    Returns a matplotlib Figure for the caller to display. The PNG is written here, before
    the caller embeds the figure and resizes it to fit the window.
    """
    from matplotlib.figure import Figure

    voltage_csv = Path(voltage_csv)
    questions_csv = Path(questions_csv)

    times: list[float] = []
    raw_voltages: list[float] = []
    filtered_voltages: list[float] = []
    arduino_us: list[int] = []
    with voltage_csv.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        required = {"time_s", "voltage_v"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"{voltage_csv.name} is missing required columns: {required}")
        for row in reader:
            times.append(float(row["time_s"]))
            raw_voltages.append(float(row["voltage_v"]))
            if row.get("arduino_time_us", "").strip():
                arduino_us.append(int(row["arduino_time_us"]))
            if "filtered_voltage_v" in row and row["filtered_voltage_v"].strip():
                filtered_voltages.append(float(row["filtered_voltage_v"]))

    if not times:
        raise ValueError(f"{voltage_csv.name} contains no voltage samples")

    # Older voltage files may not yet contain the filtered column. Filter them
    # here so the graph always displays the cleaned signal.
    if len(filtered_voltages) != len(raw_voltages):
        filtered_voltages = gsr_filters.clean_recording(
            raw_voltages, SAMPLE_RATE_HZ, arduino_us or None
        ).tolist()

    questions: list[tuple[float, int, str]] = []
    with questions_csv.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        required = {"question_time_s", "question_number", "answer_type"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"{questions_csv.name} is missing required columns: {required}")
        has_answer_time = "answer_time_s" in (reader.fieldnames or [])
        for row in reader:
            # Draw at the answer mark; older files (or an unset 0) fall back to the question mark.
            mark_time = float(row["question_time_s"])
            if has_answer_time and (row.get("answer_time_s") or "").strip():
                answer_time = float(row["answer_time_s"])
                if answer_time != 0.0:
                    mark_time = answer_time
            questions.append(
                (
                    mark_time,
                    int(row["question_number"]),
                    row["answer_type"].strip() or "Unclassified",
                )
            )

    # Heart-rate readings are optional; bpm == 0 (not locked) becomes a gap.
    bpm_times: list[float] = []
    bpm_values: list[float] = []
    bpm_states: list[str] = []
    if bpm_csv is not None and Path(bpm_csv).exists():
        with Path(bpm_csv).open("r", newline="", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)
            if {"time_s", "bpm"}.issubset(reader.fieldnames or []):
                for row in reader:
                    bpm_times.append(float(row["time_s"]))
                    bpm = float(row["bpm"])
                    bpm_values.append(bpm if bpm > 0 else float("nan"))
                    bpm_states.append((row.get("state") or "").strip())
    has_bpm = any(value == value for value in bpm_values)  # any non-NaN value

    # layout="tight" re-applies on every draw, so the graph stays tidy when the window resizes.
    figure = Figure(figsize=(13, 8), layout="tight")
    axis, bpm_axis = figure.subplots(
        2,
        1,
        sharex=True,
        gridspec_kw={"height_ratios": [3, 2]},
    )
    axis.plot(
        times,
        filtered_voltages,
        color="#195a8a",
        linewidth=1.0,
        label="GSR voltage (20 ms mains-cancelling average + 5 Hz low-pass)",
    )

    voltage_min = min(filtered_voltages)
    voltage_max = max(filtered_voltages)
    voltage_range = voltage_max - voltage_min
    if voltage_range == 0:
        voltage_range = max(abs(voltage_max), 1.0)
    label_y = voltage_max + 0.03 * voltage_range

    for mark_time, question_number, answer_type in questions:
        normalized_type = answer_type.casefold()
        if normalized_type == "truth":
            color = "#218739"
        elif normalized_type == "lie":
            color = "#c62828"
        else:
            color = "#6a6a6a"

        axis.axvline(
            x=mark_time,
            color=color,
            linestyle="--",
            linewidth=1.2,
            alpha=0.9,
        )
        axis.text(
            mark_time,
            label_y,
            f"Question {question_number} — {answer_type}",
            color=color,
            rotation=90,
            ha="right",
            va="top",
            fontsize=9,
            backgroundcolor="white",
        )
        bpm_axis.axvline(
            x=mark_time,
            color=color,
            linestyle="--",
            linewidth=1.2,
            alpha=0.9,
        )

    # Sensor outages: each run of NO_SENSOR readings, from its first reading to the next
    # reading in another state (or the last reading). Connection losses are just missing rows.
    outages: list[tuple[float, float]] = []
    outage_start: float | None = None
    for reading_time, state in zip(bpm_times, bpm_states):
        if state == "NO_SENSOR":
            if outage_start is None:
                outage_start = reading_time
        elif outage_start is not None:
            outages.append((outage_start, reading_time))
            outage_start = None
    if outage_start is not None:
        outages.append((outage_start, bpm_times[-1]))
    for index, (start, end) in enumerate(outages):
        bpm_axis.axvspan(
            start,
            end,
            color="#9e9e9e",
            alpha=0.18,
            linewidth=0,
            label="sensor disconnected" if index == 0 else None,
        )

    if has_bpm:
        bpm_axis.plot(
            bpm_times,
            bpm_values,
            color="#b3261e",
            linewidth=1.2,
            drawstyle="steps-post",
            marker="o",
            markersize=3,
        )
    else:
        bpm_axis.text(
            0.5,
            0.5,
            "No heart-rate data",
            transform=bpm_axis.transAxes,
            ha="center",
            va="center",
            color="#6a6a6a",
            fontsize=12,
        )

    title = "GSR Voltage vs. Time — mains-cancelling 20 ms average + 5 Hz low-pass"
    if subject_name:
        title += f" — {subject_name}"
    axis.set_title(title)
    axis.set_ylabel("Voltage (V)")
    axis.grid(True, alpha=0.25)
    axis.legend(loc="upper right", fontsize=8)
    axis.margins(x=0.01, y=0.12)
    bpm_axis.set_xlabel("Time (s)")
    bpm_axis.set_ylabel("Heart rate (BPM)")
    bpm_axis.grid(True, alpha=0.25)
    bpm_axis.margins(y=0.15)
    if outages:
        bpm_axis.legend(loc="upper right", fontsize=8)

    # Save the graph next to the CSVs: <stem>_voltage.csv -> <stem>_plot.png.
    stem = voltage_csv.stem
    if stem.endswith("_voltage"):
        stem = stem[: -len("_voltage")]
    png_path = voltage_csv.with_name(f"{stem}_plot.png")
    try:
        figure.savefig(png_path, dpi=150, bbox_inches="tight")
    except Exception as error:  # A failed save must not stop the plot from showing.
        print(f"Could not save the graph image {png_path}: {error}")

    return figure
