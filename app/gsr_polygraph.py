"""GSR polygraph acquisition GUI for an Arduino Nano.

Expected Arduino serial format (one sample per line):
    arduino_time_us,raw_adc

The supplied checking_GSR.ino uses 115200 baud and produces 500 samples/s.
"""

from __future__ import annotations

import csv
import queue
import re
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

import serial
from serial import SerialException
from serial.tools import list_ports


# ---------------------------- User settings ----------------------------
BAUD_RATE = 115200
ADC_REFERENCE_VOLTS = 5.0  # Classic Nano default ADC reference is AVcc (~5 V).
ADC_MAX = 1023
SAMPLE_RATE_HZ = 500.0
MAINS_FREQUENCY_HZ = 50.0
NOTCH_QUALITY_FACTOR = 30.0
DEFAULT_REST_SECONDS = 30
DEFAULT_TEST_SECONDS = 300
SERIAL_TIMEOUT_SECONDS = 2.0
ZERO_WARNING_AFTER_SECONDS = 0.10
OUTPUT_FOLDER = Path(__file__).resolve().parent / "measurements"
# -----------------------------------------------------------------------


@dataclass
class Sample:
    elapsed_s: float
    arduino_time_us: int
    raw_adc: int
    voltage_v: float


@dataclass
class Question:
    number: int
    time_s: float
    answer: str = ""
    truth: str = ""


def safe_filename(name: str) -> str:
    """Keep Hebrew/Unicode letters while removing invalid Windows filename chars."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name.strip())
    cleaned = cleaned.rstrip(". ")
    return cleaned or "subject"


def parse_sample(line: str) -> tuple[int, int]:
    fields = line.strip().split(",")
    if len(fields) != 2:
        raise ValueError("Expected two comma-separated values")
    arduino_time_us = int(fields[0])
    raw_adc = int(fields[1])
    if arduino_time_us < 0 or not 0 <= raw_adc <= ADC_MAX:
        raise ValueError("Serial values are outside the expected range")
    return arduino_time_us, raw_adc


def candidate_ports() -> list[str]:
    ports = list(list_ports.comports())
    preferred_words = ("arduino", "ch340", "usb serial", "nano")
    ports.sort(
        key=lambda p: not any(
            word in f"{p.description} {p.manufacturer or ''}".lower()
            for word in preferred_words
        )
    )
    return [p.device for p in ports]


def detect_arduino() -> tuple[serial.Serial, str]:
    """Open ports one by one and accept the first with valid GSR sample lines."""
    ports = candidate_ports()
    if not ports:
        raise RuntimeError("No serial ports were found. Connect the Arduino and try again.")

    errors: list[str] = []
    for port in ports:
        connection: serial.Serial | None = None
        try:
            connection = serial.Serial(port, BAUD_RATE, timeout=0.25)
            # Opening a Nano can reset it. Allow time for the bootloader/sketch.
            time.sleep(2.0)
            connection.reset_input_buffer()
            deadline = time.monotonic() + 2.5
            valid_lines = 0
            while time.monotonic() < deadline:
                data = connection.readline()
                if not data:
                    continue
                try:
                    parse_sample(data.decode("ascii", errors="strict"))
                    valid_lines += 1
                    if valid_lines >= 3:
                        connection.reset_input_buffer()
                        return connection, port
                except (UnicodeDecodeError, ValueError):
                    valid_lines = 0
            errors.append(f"{port}: no valid GSR data")
        except (OSError, SerialException) as exc:
            errors.append(f"{port}: {exc}")
        if connection is not None and connection.is_open:
            connection.close()

    details = "\n".join(errors)
    raise RuntimeError(
        "Could not find an Arduino sending 'time_us,raw_adc' at 115200 baud.\n\n"
        + details
    )


def apply_50hz_notch_filter(voltage_values: list[float]) -> list[float]:
    """Remove 50 Hz mains interference without changing the signal timing."""
    import numpy as np
    from scipy.signal import filtfilt, iirnotch

    if not voltage_values:
        return []

    values = np.asarray(voltage_values, dtype=float)
    numerator, denominator = iirnotch(
        w0=MAINS_FREQUENCY_HZ,
        Q=NOTCH_QUALITY_FACTOR,
        fs=SAMPLE_RATE_HZ,
    )

    # filtfilt needs several samples at both ends. Very short recordings are
    # returned unchanged instead of raising an exception.
    minimum_length = 3 * max(len(numerator), len(denominator))
    if len(values) <= minimum_length:
        return values.tolist()

    filtered = filtfilt(numerator, denominator, values)
    return filtered.tolist()


def plot_examination(
    voltage_csv: str | Path,
    questions_csv: str | Path,
    subject_name: str = "",
) -> None:
    """Plot voltage over time and mark the time and type of every question."""
    import matplotlib.pyplot as plt

    voltage_csv = Path(voltage_csv)
    questions_csv = Path(questions_csv)

    times: list[float] = []
    raw_voltages: list[float] = []
    filtered_voltages: list[float] = []
    with voltage_csv.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        required = {"time_s", "voltage_v"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"{voltage_csv.name} is missing required columns: {required}")
        for row in reader:
            times.append(float(row["time_s"]))
            raw_voltages.append(float(row["voltage_v"]))
            if "filtered_voltage_v" in row and row["filtered_voltage_v"].strip():
                filtered_voltages.append(float(row["filtered_voltage_v"]))

    if not times:
        raise ValueError(f"{voltage_csv.name} contains no voltage samples")

    # Older voltage files may not yet contain the filtered column. Filter them
    # here so the graph always displays the 50 Hz filtered signal.
    if len(filtered_voltages) != len(raw_voltages):
        filtered_voltages = apply_50hz_notch_filter(raw_voltages)

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

    figure, axis = plt.subplots(figsize=(13, 6.5))
    axis.plot(
        times,
        filtered_voltages,
        color="#195a8a",
        linewidth=1.0,
        label="Filtered GSR voltage",
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

    title = "GSR Voltage vs. Time — 50 Hz Notch Filtered"
    if subject_name:
        title += f" — {subject_name}"
    axis.set_title(title)
    axis.set_xlabel("Time (s)")
    axis.set_ylabel("Voltage (V)")
    axis.grid(True, alpha=0.25)
    axis.margins(x=0.01, y=0.12)
    figure.tight_layout()
    plt.show()


class PolygraphApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("GSR Polygraph")
        self.geometry("820x560")
        self.minsize(720, 500)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.connection: serial.Serial | None = None
        self.port_name = ""
        self.reader_thread: threading.Thread | None = None
        self.stop_reader = threading.Event()
        self.serial_events: queue.Queue[tuple[str, object]] = queue.Queue()

        self.subject_name = ""
        self.file_stem = ""
        self.samples: list[Sample] = []
        self.questions: list[Question] = []
        self.pending_question: Question | None = None
        self.recording_started_at: float | None = None
        self.phase_started_at: float | None = None
        self.recording = False
        self.finished = False
        self.zero_started_at: float | None = None
        self.zero_warning_active = False
        self.last_valid_sample_at: float | None = None
        self.bad_line_count = 0
        self.rest_seconds = DEFAULT_REST_SECONDS
        self.test_seconds = DEFAULT_TEST_SECONDS
        self.truth_index = 0
        self.last_saved_paths: tuple[Path, Path] | None = None

        self.style = ttk.Style(self)
        self.style.configure("Title.TLabel", font=("Segoe UI", 22, "bold"))
        self.style.configure("Timer.TLabel", font=("Consolas", 38, "bold"))
        self.style.configure("Question.TLabel", font=("Segoe UI", 26, "bold"))
        self.container = ttk.Frame(self, padding=24)
        self.container.pack(fill="both", expand=True)

        self.after(50, self.process_serial_events)
        self.show_connecting_screen()
        threading.Thread(target=self.connect_worker, daemon=True).start()

    def clear_screen(self) -> None:
        for child in self.container.winfo_children():
            child.destroy()

    def show_connecting_screen(self) -> None:
        self.clear_screen()
        ttk.Label(self.container, text="Connecting to Arduino", style="Title.TLabel").pack(pady=(100, 18))
        ttk.Label(self.container, text="Checking serial ports and validating GSR data...").pack()
        ttk.Progressbar(self.container, mode="indeterminate", length=320).pack(pady=25)
        progress = self.container.winfo_children()[-1]
        progress.start(12)

    def connect_worker(self) -> None:
        try:
            connection, port = detect_arduino()
            self.serial_events.put(("connected", (connection, port)))
        except Exception as exc:
            self.serial_events.put(("connect_error", str(exc)))

    def process_serial_events(self) -> None:
        try:
            while True:
                event, payload = self.serial_events.get_nowait()
                if event == "connected":
                    self.connection, self.port_name = payload  # type: ignore[misc]
                    self.last_valid_sample_at = time.monotonic()
                    self.start_reader()
                    self.show_setup_screen()
                elif event == "connect_error":
                    self.show_connection_error(str(payload))
                elif event == "sample":
                    arduino_us, raw_adc, received_at = payload  # type: ignore[misc]
                    self.handle_sample(arduino_us, raw_adc, received_at)
                elif event == "bad_line":
                    self.bad_line_count += 1
                elif event == "serial_error":
                    self.handle_serial_error(str(payload))
        except queue.Empty:
            pass

        if self.recording and self.last_valid_sample_at is not None:
            silence = time.monotonic() - self.last_valid_sample_at
            if silence > SERIAL_TIMEOUT_SECONDS:
                self.handle_serial_error(
                    f"No valid data were received for {silence:.1f} seconds. "
                    "The Arduino may have been disconnected."
                )
        self.after(50, self.process_serial_events)

    def start_reader(self) -> None:
        self.stop_reader.clear()
        self.reader_thread = threading.Thread(target=self.serial_reader, daemon=True)
        self.reader_thread.start()

    def serial_reader(self) -> None:
        assert self.connection is not None
        while not self.stop_reader.is_set():
            try:
                data = self.connection.readline()
                if not data:
                    continue
                try:
                    arduino_us, raw_adc = parse_sample(data.decode("ascii", errors="strict"))
                    self.serial_events.put(
                        ("sample", (arduino_us, raw_adc, time.monotonic()))
                    )
                except (UnicodeDecodeError, ValueError):
                    self.serial_events.put(("bad_line", None))
            except (OSError, SerialException) as exc:
                self.serial_events.put(("serial_error", str(exc)))
                return

    def show_connection_error(self, details: str) -> None:
        self.clear_screen()
        ttk.Label(self.container, text="Arduino connection error", style="Title.TLabel").pack(pady=(65, 18))
        ttk.Label(
            self.container,
            text=details,
            foreground="#a40000",
            wraplength=700,
            justify="center",
        ).pack(pady=12)
        ttk.Button(self.container, text="Try again", command=self.retry_connection).pack(pady=18)

    def retry_connection(self) -> None:
        self.close_serial()
        self.show_connecting_screen()
        threading.Thread(target=self.connect_worker, daemon=True).start()

    def show_setup_screen(self) -> None:
        self.clear_screen()
        ttk.Label(self.container, text="New GSR examination", style="Title.TLabel").pack(pady=(25, 25))
        ttk.Label(self.container, text=f"Arduino connected on {self.port_name}", foreground="#167a35").pack(pady=(0, 25))

        form = ttk.Frame(self.container)
        form.pack()
        ttk.Label(form, text="Subject name:").grid(row=0, column=0, sticky="e", padx=8, pady=8)
        self.name_entry = ttk.Entry(form, width=35)
        self.name_entry.grid(row=0, column=1, padx=8, pady=8)
        self.name_entry.focus_set()

        ttk.Label(form, text="Rest duration (seconds):").grid(row=1, column=0, sticky="e", padx=8, pady=8)
        self.rest_entry = ttk.Entry(form, width=12)
        self.rest_entry.insert(0, str(DEFAULT_REST_SECONDS))
        self.rest_entry.grid(row=1, column=1, sticky="w", padx=8, pady=8)

        ttk.Label(form, text="Question phase duration (seconds):").grid(row=2, column=0, sticky="e", padx=8, pady=8)
        self.test_entry = ttk.Entry(form, width=12)
        self.test_entry.insert(0, str(DEFAULT_TEST_SECONDS))
        self.test_entry.grid(row=2, column=1, sticky="w", padx=8, pady=8)

        ttk.Button(self.container, text="Start examination", command=self.begin_examination).pack(pady=28)
        self.name_entry.bind("<Return>", lambda _event: self.begin_examination())

    def begin_examination(self) -> None:
        name = self.name_entry.get().strip()
        if not name:
            messagebox.showwarning("Missing name", "Enter the subject's name.")
            return
        try:
            rest = int(self.rest_entry.get())
            test = int(self.test_entry.get())
            if rest < 1 or test < 1:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Invalid duration", "Durations must be positive whole seconds.")
            return

        self.subject_name = name
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.file_stem = f"{safe_filename(name)}_{timestamp}"
        self.rest_seconds = rest
        self.test_seconds = test
        self.samples.clear()
        self.questions.clear()
        self.pending_question = None
        self.bad_line_count = 0
        self.recording_started_at = time.monotonic()
        self.phase_started_at = self.recording_started_at
        self.last_valid_sample_at = self.recording_started_at
        self.recording = True
        self.show_rest_screen()

    def show_rest_screen(self) -> None:
        self.clear_screen()
        ttk.Label(self.container, text="Rest period", style="Title.TLabel").pack(pady=(80, 15))
        ttk.Label(self.container, text="The subject should remain still and quiet.").pack(pady=8)
        self.timer_label = ttk.Label(self.container, text="", style="Timer.TLabel")
        self.timer_label.pack(pady=25)
        self.warning_label = tk.Label(self.container, text="", font=("Segoe UI", 14, "bold"))
        self.warning_label.pack(pady=8)
        self.update_rest_timer()

    def update_rest_timer(self) -> None:
        if not self.recording or self.finished or self.phase_started_at is None:
            return
        remaining = max(0, self.rest_seconds - (time.monotonic() - self.phase_started_at))
        self.timer_label.config(text=f"{remaining:04.1f}")
        if remaining <= 0:
            self.show_question_screen()
        else:
            self.after(50, self.update_rest_timer)

    def show_question_screen(self) -> None:
        self.clear_screen()
        self.phase_started_at = time.monotonic()

        top = ttk.Frame(self.container)
        top.pack(fill="x")
        ttk.Label(top, text="Question phase", style="Title.TLabel").pack(side="left")
        self.test_timer_label = ttk.Label(top, text="", font=("Consolas", 22, "bold"))
        self.test_timer_label.pack(side="right")

        self.question_label = ttk.Label(self.container, text="Question 1", style="Question.TLabel")
        self.question_label.pack(pady=(55, 20))
        self.mark_button = ttk.Button(
            self.container,
            text="Mark question time",
            command=self.mark_question,
        )
        self.mark_button.pack(pady=8)

        answer_frame = ttk.Frame(self.container)
        answer_frame.pack(pady=25)
        ttk.Label(answer_frame, text="Subject's answer:").pack(side="left", padx=8)
        self.answer_entry = ttk.Entry(answer_frame, width=42, state="disabled")
        self.answer_entry.pack(side="left", padx=8)
        self.answer_entry.bind("<Return>", self.save_answer)

        ttk.Label(
            self.container,
            text="Click when the question is asked. Type the answer, then press Enter.",
        ).pack(pady=4)
        self.status_label = ttk.Label(self.container, text="Waiting for question 1")
        self.status_label.pack(pady=6)
        self.warning_label = tk.Label(self.container, text="", font=("Segoe UI", 14, "bold"))
        self.warning_label.pack(pady=8)
        ttk.Button(self.container, text="Finish examination", command=self.finish_measurement).pack(side="bottom", pady=12)
        self.update_test_timer()

    def update_test_timer(self) -> None:
        if not self.recording or self.finished or self.phase_started_at is None:
            return
        elapsed = time.monotonic() - self.phase_started_at
        remaining = max(0, self.test_seconds - elapsed)
        minutes, seconds = divmod(int(remaining), 60)
        self.test_timer_label.config(text=f"{minutes:02d}:{seconds:02d}")
        if remaining <= 0:
            self.finish_measurement()
        else:
            self.after(100, self.update_test_timer)

    def mark_question(self) -> None:
        if self.pending_question is not None or self.recording_started_at is None:
            return
        number = len(self.questions) + 1
        question_time = time.monotonic() - self.recording_started_at
        self.pending_question = Question(number=number, time_s=question_time)
        self.mark_button.config(state="disabled")
        self.answer_entry.config(state="normal")
        self.answer_entry.delete(0, "end")
        self.answer_entry.focus_set()
        self.status_label.config(text=f"Question {number} marked at {question_time:.3f} s — enter the answer")

    def save_answer(self, _event: object | None = None) -> None:
        if self.pending_question is None:
            return
        answer = self.answer_entry.get().strip()
        if not answer:
            messagebox.showwarning("Missing answer", "Enter the subject's answer before continuing.")
            return
        self.pending_question.answer = answer
        self.questions.append(self.pending_question)
        self.pending_question = None
        next_number = len(self.questions) + 1
        self.question_label.config(text=f"Question {next_number}")
        self.status_label.config(text=f"Answer saved. Waiting for question {next_number}")
        self.answer_entry.delete(0, "end")
        self.answer_entry.config(state="disabled")
        self.mark_button.config(state="normal")
        self.mark_button.focus_set()

    def handle_sample(self, arduino_us: int, raw_adc: int, received_at: float) -> None:
        self.last_valid_sample_at = received_at
        if self.recording and self.recording_started_at is not None:
            elapsed = received_at - self.recording_started_at
            voltage = raw_adc * ADC_REFERENCE_VOLTS / ADC_MAX
            self.samples.append(Sample(elapsed, arduino_us, raw_adc, voltage))

            if raw_adc == 0:
                if self.zero_started_at is None:
                    self.zero_started_at = received_at
                if received_at - self.zero_started_at >= ZERO_WARNING_AFTER_SECONDS:
                    self.set_zero_warning(True)
            else:
                self.zero_started_at = None
                self.set_zero_warning(False)

    def set_zero_warning(self, active: bool) -> None:
        if active == self.zero_warning_active:
            return
        self.zero_warning_active = active
        label = getattr(self, "warning_label", None)
        if label is not None and label.winfo_exists():
            if active:
                label.config(
                    text="WARNING: zero voltage — one finger may be disconnected",
                    fg="white",
                    bg="#c62828",
                    padx=12,
                    pady=8,
                )
            else:
                label.config(text="", bg=self.cget("bg"), padx=0, pady=0)

    def handle_serial_error(self, details: str) -> None:
        if self.finished:
            return
        self.recording = False
        self.finished = True
        saved_message = self.save_files(partial=True)
        messagebox.showerror(
            "Arduino connection lost",
            f"The serial connection failed:\n\n{details}\n\n{saved_message}",
        )
        self.close_serial()
        self.show_connection_error("The Arduino connection was lost. Reconnect it to start a new examination.")

    def finish_measurement(self) -> None:
        if self.finished:
            return
        if self.pending_question is not None:
            answer = self.answer_entry.get().strip()
            if answer:
                self.pending_question.answer = answer
                self.questions.append(self.pending_question)
            elif not messagebox.askyesno(
                "Unfinished question",
                "The current question has no saved answer. Finish without saving this question?",
            ):
                return
            self.pending_question = None

        self.recording = False
        if not self.questions:
            if messagebox.askyesno("No questions", "No questions were saved. Save the voltage recording anyway?"):
                self.finished = True
                saved_message = self.save_files(partial=False)
                messagebox.showinfo("Saved", saved_message)
                saved_paths = self.last_saved_paths
                self.close_serial()
                self.destroy()
                if saved_paths is not None:
                    plot_examination(*saved_paths, subject_name=self.subject_name)
            return

        self.show_truth_screen()

    def show_truth_screen(self) -> None:
        self.clear_screen()
        self.truth_index = 0
        ttk.Label(self.container, text="Classify each answer", style="Title.TLabel").pack(pady=(45, 25))
        self.truth_question_label = ttk.Label(self.container, text="", style="Question.TLabel")
        self.truth_question_label.pack(pady=15)
        self.truth_answer_label = ttk.Label(self.container, text="", font=("Segoe UI", 16), wraplength=700)
        self.truth_answer_label.pack(pady=12)
        self.truth_var = tk.StringVar(value="")
        choices = ttk.Frame(self.container)
        choices.pack(pady=22)
        ttk.Radiobutton(choices, text="Truth", variable=self.truth_var, value="Truth").pack(side="left", padx=30)
        ttk.Radiobutton(choices, text="Lie", variable=self.truth_var, value="Lie").pack(side="left", padx=30)
        ttk.Button(self.container, text="Save and continue", command=self.save_truth).pack(pady=18)
        self.render_truth_question()

    def render_truth_question(self) -> None:
        question = self.questions[self.truth_index]
        self.truth_question_label.config(text=f"Question {question.number}")
        self.truth_answer_label.config(text=f"Subject's answer: {question.answer}")
        self.truth_var.set("")

    def save_truth(self) -> None:
        truth = self.truth_var.get()
        if not truth:
            messagebox.showwarning("Choose a type", "Choose Truth or Lie.")
            return
        self.questions[self.truth_index].truth = truth
        self.truth_index += 1
        if self.truth_index < len(self.questions):
            self.render_truth_question()
            return

        self.finished = True
        saved_message = self.save_files(partial=False)
        messagebox.showinfo("Examination saved", saved_message)
        saved_paths = self.last_saved_paths
        self.close_serial()
        self.destroy()
        if saved_paths is not None:
            plot_examination(*saved_paths, subject_name=self.subject_name)

    def save_files(self, partial: bool) -> str:
        OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
        suffix = "_PARTIAL" if partial else ""
        voltage_path = OUTPUT_FOLDER / f"{self.file_stem}_voltage{suffix}.csv"
        questions_path = OUTPUT_FOLDER / f"{self.file_stem}_questions{suffix}.csv"
        self.last_saved_paths = (voltage_path, questions_path)

        filtered_voltages = apply_50hz_notch_filter(
            [sample.voltage_v for sample in self.samples]
        )

        with voltage_path.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.writer(file)
            writer.writerow(
                [
                    "time_s",
                    "arduino_time_us",
                    "raw_adc",
                    "voltage_v",
                    "filtered_voltage_v",
                ]
            )
            for sample, filtered_voltage in zip(self.samples, filtered_voltages):
                writer.writerow(
                    [
                        f"{sample.elapsed_s:.6f}",
                        sample.arduino_time_us,
                        sample.raw_adc,
                        f"{sample.voltage_v:.6f}",
                        f"{filtered_voltage:.6f}",
                    ]
                )

        with questions_path.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.writer(file)
            writer.writerow(["question_time_s", "question_number", "subject_answer", "answer_type"])
            for question in self.questions:
                writer.writerow(
                    [f"{question.time_s:.6f}", question.number, question.answer, question.truth]
                )

        kind = "Partial files were saved" if partial else "Files saved"
        return (
            f"{kind} in:\n{OUTPUT_FOLDER}\n\n"
            f"{voltage_path.name}\n{questions_path.name}\n\n"
            f"Valid samples: {len(self.samples):,}; malformed serial lines ignored: {self.bad_line_count}"
        )

    def close_serial(self) -> None:
        self.stop_reader.set()
        if self.connection is not None and self.connection.is_open:
            try:
                self.connection.close()
            except SerialException:
                pass
        self.connection = None

    def on_close(self) -> None:
        if self.recording and not messagebox.askyesno(
            "Close examination",
            "An examination is running. Close it and save partial data?",
        ):
            return
        if self.recording:
            self.recording = False
            self.save_files(partial=True)
        self.finished = True
        self.close_serial()
        self.destroy()


if __name__ == "__main__":
    app = PolygraphApp()
    app.mainloop()
