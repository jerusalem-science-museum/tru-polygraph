"""Examination records and the CSV files an examination is saved to.

Each finished examination writes two files of its own, <stem>_voltage.csv and <stem>_bpm.csv,
and appends its questions to the shared questions.csv (or, if that file can't be written,
to a fallback <stem>_questions.csv). This module has no GUI: main.py decides what to do when
the shared file can't be written.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import gsr_filters
from arduino import SAMPLE_RATE_HZ

# The shared questions file: every finished examination appends its questions here.
SHARED_QUESTIONS_FILENAME = "questions.csv"

# Columns of the shared questions file (and of a per-examination fallback file
# <stem>_questions.csv, written when the shared file is locked). Times are on the
# Arduino-based scale, seconds since that examination's recording start.
QUESTIONS_COLUMNS = [
    "file_name",  # Examination stem <subject>_<YYYY-MM-DD>_<HH-MM-SS>; its files are <stem>_voltage.csv etc.
    "question_number",
    "question_text",  # Wording at examination time, from config.toml.
    "subject_answer",  # Yes / No / Undefined (ANSWER_CHOICES).
    "answer_truth",  # Operator's truth classification: Truth / Lie.
    "detected_truth",  # Polygraph's detection: Truth / Lie; empty until there is an algorithm.
    "question_start_time_s",
    "question_end_time_s",
    "answer_time_s",
]


class QuestionsFileMismatch(ValueError):
    """The existing shared questions file has other columns, so rows can't be appended to it."""


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
    """One question and its three marks, in Arduino-based seconds since recording start."""

    number: int
    start_time_s: float  # Question start mark: first Space, the operator starts asking.
    end_time_s: float | None = None  # Question end mark: second Space, the operator finished asking.
    answer_time_s: float | None = None  # Answer mark: the answer key was pressed.
    answer: str = ""  # Subject's answer (ANSWER_CHOICES).
    truth: str = ""  # Operator's truth classification: Truth / Lie.


def safe_filename(name: str) -> str:
    """Keep Hebrew/Unicode letters while removing invalid Windows filename chars."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name.strip())
    cleaned = cleaned.rstrip(". ")
    return cleaned or "subject"


def save_examination(
    folder: Path,
    file_stem: str,
    samples: list[Sample],
    bpm_readings: list[BpmReading],
) -> tuple[Path, Path]:
    """Write <stem>_voltage.csv and <stem>_bpm.csv; returns their paths in that order."""
    folder.mkdir(parents=True, exist_ok=True)
    voltage_path = folder / f"{file_stem}_voltage.csv"
    bpm_path = folder / f"{file_stem}_bpm.csv"

    filtered_voltages = gsr_filters.clean_recording(
        [sample.voltage_v for sample in samples],
        SAMPLE_RATE_HZ,
        [sample.arduino_time_us for sample in samples],
    )
    conductances = gsr_filters.conductance_us(filtered_voltages)

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
                "conductance_us",
            ]
        )
        for sample, filtered_voltage, conductance in zip(samples, filtered_voltages, conductances):
            writer.writerow(
                [
                    f"{sample.elapsed_s:.6f}",
                    f"{sample.pc_time_s:.6f}",
                    sample.arduino_time_us,
                    sample.raw_adc,
                    f"{sample.voltage_v:.6f}",
                    f"{filtered_voltage:.6f}",
                    f"{conductance:.6f}",
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

    return voltage_path, bpm_path


def _time(value: float | None) -> str:
    return "" if value is None else f"{value:.6f}"


def question_rows(file_stem: str, questions: list[Question], question_texts: Sequence[str]) -> list[list[str]]:
    """One examination's questions as rows in QUESTIONS_COLUMNS order."""
    return [
        [
            file_stem,
            str(question.number),
            question_texts[question.number - 1],
            question.answer,
            question.truth,
            "",  # detected_truth: no detection algorithm yet.
            _time(question.start_time_s),
            _time(question.end_time_s),
            _time(question.answer_time_s),
        ]
        for question in questions
    ]


def _csv_text(rows: list[list[str]]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)  # "\r\n" line endings, like the other files.
    return buffer.getvalue()


def append_shared_questions(folder: Path, rows: list[list[str]]) -> Path:
    """Append rows to the shared questions.csv in one write, creating it with a header if missing.

    Raises OSError (PermissionError while the file is open in Excel) if it can't be written, and
    QuestionsFileMismatch if the existing file's header isn't QUESTIONS_COLUMNS. In both cases
    the file is left unchanged.
    """
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / SHARED_QUESTIONS_FILENAME
    if path.exists() and path.stat().st_size > 0:
        try:
            with path.open("r", newline="", encoding="utf-8-sig") as file:
                header = next(csv.reader(file), None)
        except UnicodeDecodeError as error:
            raise QuestionsFileMismatch(f"{path.name} is not a UTF-8 text file.") from error
        if header != QUESTIONS_COLUMNS:
            raise QuestionsFileMismatch(f"{path.name} has different columns than this app writes.")
        with path.open("rb") as file:
            file.seek(-1, 2)
            prefix = "" if file.read(1) in (b"\n", b"\r") else "\r\n"  # Edited without a final newline.
        # utf-8, not utf-8-sig: the file already starts with its BOM.
        with path.open("a", newline="", encoding="utf-8") as file:
            file.write(prefix + _csv_text(rows))
    else:
        with path.open("w", newline="", encoding="utf-8-sig") as file:
            file.write(_csv_text([QUESTIONS_COLUMNS, *rows]))
    return path


def write_fallback_questions(folder: Path, file_stem: str, rows: list[list[str]]) -> Path:
    """Write one examination's questions to <stem>_questions.csv, with a header."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{file_stem}_questions.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        file.write(_csv_text([QUESTIONS_COLUMNS, *rows]))
    return path
