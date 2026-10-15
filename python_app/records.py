"""Examination records and the three CSV files an examination is saved to."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

import gsr_filters
from arduino import SAMPLE_RATE_HZ


@dataclass
class Sample:
    elapsed_s: float  # Arduino-based time since recording start.
    pc_time_s: float  # PC receive time since recording start (reference only).
    arduino_time_us: int  # Raw 32-bit micros() value as sent by the Arduino.
    raw_adc: int
    voltage_v: float


@dataclass
class BpmReading:
    time_s: float  # Arduino-based time since recording start.
    pc_time_s: float  # PC receive time since recording start.
    arduino_time_us: int  # Raw 32-bit micros() value as sent by the Arduino.
    bpm: int  # 0 when not locked.
    state: str


@dataclass
class Question:
    number: int
    time_s: float  # Arduino-based time since recording start.
    pc_time_s: float = 0.0  # PC clock time since recording start (reference only).
    answer: str = ""
    truth: str = ""


def safe_filename(name: str) -> str:
    """Keep Hebrew/Unicode letters while removing invalid Windows filename chars."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name.strip())
    cleaned = cleaned.rstrip(". ")
    return cleaned or "subject"


def save_examination(
    folder: Path,
    file_stem: str,
    samples: list[Sample],
    questions: list[Question],
    bpm_readings: list[BpmReading],
    partial: bool,
) -> tuple[Path, Path, Path]:
    """Write the voltage, questions and heart-rate CSVs; returns their paths in that order."""
    folder.mkdir(parents=True, exist_ok=True)
    suffix = "_PARTIAL" if partial else ""
    voltage_path = folder / f"{file_stem}_voltage{suffix}.csv"
    questions_path = folder / f"{file_stem}_questions{suffix}.csv"
    bpm_path = folder / f"{file_stem}_bpm{suffix}.csv"

    filtered_voltages = gsr_filters.clean_recording(
        [sample.voltage_v for sample in samples],
        SAMPLE_RATE_HZ,
        [sample.arduino_time_us for sample in samples],
    )

    with voltage_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "time_s",
                "pc_time_s",
                "arduino_time_us",
                "raw_adc",
                "voltage_v",
                "filtered_voltage_v",
            ]
        )
        for sample, filtered_voltage in zip(samples, filtered_voltages):
            writer.writerow(
                [
                    f"{sample.elapsed_s:.6f}",
                    f"{sample.pc_time_s:.6f}",
                    sample.arduino_time_us,
                    sample.raw_adc,
                    f"{sample.voltage_v:.6f}",
                    f"{filtered_voltage:.6f}",
                ]
            )

    with questions_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(
            ["question_time_s", "pc_time_s", "question_number", "subject_answer", "answer_type"]
        )
        for question in questions:
            writer.writerow(
                [
                    f"{question.time_s:.6f}",
                    f"{question.pc_time_s:.6f}",
                    question.number,
                    question.answer,
                    question.truth,
                ]
            )

    with bpm_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(["time_s", "pc_time_s", "arduino_time_us", "bpm", "state"])
        for reading in bpm_readings:
            writer.writerow(
                [
                    f"{reading.time_s:.6f}",
                    f"{reading.pc_time_s:.6f}",
                    reading.arduino_time_us,
                    reading.bpm,
                    reading.state,
                ]
            )

    return voltage_path, questions_path, bpm_path
