"""Plot a saved examination: GSR voltage and heart rate with every question marked."""

from __future__ import annotations

import csv
from pathlib import Path

import gsr_filters
from arduino import SAMPLE_RATE_HZ


def plot_examination(
    voltage_csv: str | Path,
    questions_csv: str | Path,
    bpm_csv: str | Path | None = None,
    subject_name: str = "",
) -> None:
    """Plot GSR voltage and heart rate over time and mark every question."""
    import matplotlib.pyplot as plt

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
        for row in reader:
            questions.append(
                (
                    float(row["question_time_s"]),
                    int(row["question_number"]),
                    row["answer_type"].strip() or "Unclassified",
                )
            )

    # Heart-rate readings are optional; bpm == 0 (not locked) becomes a gap.
    bpm_times: list[float] = []
    bpm_values: list[float] = []
    if bpm_csv is not None and Path(bpm_csv).exists():
        with Path(bpm_csv).open("r", newline="", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)
            if {"time_s", "bpm"}.issubset(reader.fieldnames or []):
                for row in reader:
                    bpm_times.append(float(row["time_s"]))
                    bpm = float(row["bpm"])
                    bpm_values.append(bpm if bpm > 0 else float("nan"))
    has_bpm = any(value == value for value in bpm_values)  # any non-NaN value

    figure, (axis, bpm_axis) = plt.subplots(
        2,
        1,
        sharex=True,
        figsize=(13, 8),
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

    for question_time, question_number, answer_type in questions:
        normalized_type = answer_type.casefold()
        if normalized_type == "truth":
            color = "#218739"
        elif normalized_type == "lie":
            color = "#c62828"
        else:
            color = "#6a6a6a"

        axis.axvline(
            x=question_time,
            color=color,
            linestyle="--",
            linewidth=1.2,
            alpha=0.9,
        )
        axis.text(
            question_time,
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
            x=question_time,
            color=color,
            linestyle="--",
            linewidth=1.2,
            alpha=0.9,
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
    figure.tight_layout()
    plt.show()
