"""Polygraph examination app: GSR + heart-rate acquisition GUI for an Arduino (Uno/Nano).

The serial protocol is described in arduino.py. GSR samples, heart-rate readings
and question marks all share one Arduino-based clock (see PolygraphApp).
"""

from __future__ import annotations

import queue
import random
import sys
import threading
import time
import tkinter as tk
import unicodedata
from datetime import datetime
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk

import serial
from serial import SerialException

# The sibling modules sit next to this script; make them importable from any cwd.
_APP_DIR = str(Path(__file__).resolve().parent)
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)
from arduino import ADC_MAX, MicrosUnwrapper, detect_arduino, parse_line  # noqa: E402
from records import (  # noqa: E402
    BpmReading,
    Question,
    Sample,
    safe_filename,
    save_examination,
)
from plotting import plot_examination  # noqa: E402
from config import (  # noqa: E402
    ADC_REFERENCE_VOLTS,
    ANSWER_CHOICES,
    BPM_STALE_SECONDS,
    DEFAULT_REST_SECONDS,
    DEFAULT_TEST_SECONDS,
    FLASH_MS,
    GSR_READING_STALE_SECONDS,
    GSR_SHORT_MIN,
    LIE_FLASH_COLOR,
    MARK_DEBOUNCE_SECONDS,
    MIN_QUESTION_GAP_SECONDS,
    OUTPUT_FOLDER,
    QUESTION_TEXTS,
    SCREEN_CHANGE_LOCKOUT_SECONDS,
    SERIAL_TIMEOUT_SECONDS,
    TRUTH_FLASH_COLOR,
    ZERO_WARNING_AFTER_SECONDS,
)

try:  # Written by the release build (.github/workflows/release.yml); absent in source runs.
    from _version import VERSION  # noqa: E402
except ImportError:
    VERSION = "dev"


ANSWER_HINT = ", ".join(f"{number} = {choice}" for number, choice in enumerate(ANSWER_CHOICES, start=1))
QUESTION_WRAP_PIXELS = 640
RLE, PDF = chr(0x202B), chr(0x202C)  # Unicode marks: start and end of right-to-left text.


def log_i2c_boot(text: str) -> None:
    """Keep the firmware's sensor start-up check, so failed boots can be compared later."""
    entry = f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}"
    print(entry)
    try:
        with open(OUTPUT_FOLDER / "i2c_boot.log", "a", encoding="ascii") as log_file:
            log_file.write(entry + "\n")
    except OSError as exc:
        print(f"Could not write i2c_boot.log: {exc}")


def is_rtl(text: str) -> bool:
    """True if the first strongly directional character is right-to-left (Hebrew, Arabic)."""
    for character in text:
        direction = unicodedata.bidirectional(character)
        if direction in ("R", "AL"):
            return True
        if direction == "L":
            return False
    return False


def wrap_rtl(text: str, font: tkfont.Font, width: int) -> str:
    """Break right-to-left text into lines no wider than width, each marked as right-to-left.

    Tk draws every line as a separate left-to-right paragraph, which misplaces punctuation and
    Latin words. The marks must be on each line, so Tk's own wraplength can't be used here.
    """
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if current and font.measure(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    lines.append(current)
    return "\n".join(f"{RLE}{line}{PDF}" for line in lines)


class PolygraphApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
        self.title(f"Polygraph {VERSION}")
        self.geometry("800x560")
        self.minsize(700, 480)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        # Bound directly (not via bind_keys) so clear_screen() never removes it.
        self.bind("<Escape>", lambda _event: self.on_close())
        self.bind("<Control-q>", lambda _event: self.on_close())
        self.bind("<Control-Q>", lambda _event: self.on_close())

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
        self.last_saved_paths: tuple[Path, Path, Path] | None = None

        # Time base: Arduino micros() unwrapped to 64 bit, shared by GSR and B lines.
        # time_s = (unwrapped_us - anchor_us) / 1e6 + anchor_offset_s, where the anchor
        # is the first sample after recording starts. Questions use the same clock.
        self.unwrapper = MicrosUnwrapper()
        self.anchor_us: int | None = None
        self.anchor_offset_s = 0.0
        self.last_sample_time_s = 0.0
        self.last_sample_received_at = 0.0
        self.bpm_readings: list[BpmReading] = []
        self.latest_bpm: tuple[int, str, float] | None = None  # (bpm, state, received_at)
        self.heart_label: ttk.Label | None = None
        self.last_gsr_raw: int | None = None
        self.last_gsr_in_range_at: float | None = None  # Last sample with 0 < raw < GSR_SHORT_MIN.
        self.sensor_status_labels: dict[str, tk.Label] = {}  # Setup screen only.
        self.key_bindings: list[tuple[str, str]] = []  # (sequence, funcid) on this window.
        self.answer_var = tk.StringVar(value="")
        self.truth_var = tk.StringVar(value="")
        self.last_answer_saved_at = 0.0
        self.last_undo_at = 0.0

        self.style = ttk.Style(self)
        self.style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        self.style.configure("Timer.TLabel", font=("Consolas", 30, "bold"))
        self.style.configure("Question.TLabel", font=("Segoe UI", 22, "bold"))
        self.container = ttk.Frame(self, padding=12)
        self.container.pack(fill="both", expand=True)

        # Question-screen state colouring (Q.* styles are only used on that screen).
        self.default_bg = self.cget("bg")
        self.q_neutral_bg = self.style.lookup("TFrame", "background") or self.default_bg
        self.q_bg = self.q_neutral_bg
        self.q_screen_active = False
        self.q_flash_after: str | None = None
        self.truth_flash_after: str | None = None
        self.truth_flash_label: tk.Label | None = None
        self.question_font = tkfont.Font(family="Segoe UI", size=22, weight="bold")
        self.style.configure("Q.TFrame", background=self.q_neutral_bg)
        self.style.configure("Q.TLabel", background=self.q_neutral_bg, foreground="#202020")
        self.style.configure("Q.TRadiobutton", background=self.q_neutral_bg, foreground="#202020")
        self.style.configure("QTitle.TLabel", font=("Segoe UI", 16, "bold"))
        self.style.configure("QQuestion.TLabel", font=("Segoe UI", 22, "bold"))
        self.style.configure("QBanner.TLabel", font=("Segoe UI", 22, "bold"))
        for name in ("QTitle.TLabel", "QQuestion.TLabel", "QBanner.TLabel"):
            self.style.configure(name, background=self.q_neutral_bg, foreground="#202020")
        self.style.configure("QGapWait.TLabel", font=("Segoe UI", 14, "bold"),
                             background=self.q_neutral_bg, foreground="#b71c1c")
        self.style.configure("QGapReady.TLabel", font=("Segoe UI", 14, "bold"),
                             background=self.q_neutral_bg, foreground="#1b5e20")

        self.after(50, self.process_serial_events)
        self.show_connecting_screen()
        threading.Thread(target=self.connect_worker, daemon=True).start()

    def bind_keys(self, handlers: dict[str, object]) -> None:
        """Bind keys on the toplevel window (works whatever widget has focus)."""
        for sequence, handler in handlers.items():
            self.key_bindings.append((sequence, self.bind(sequence, handler)))

    def unbind_keys(self) -> None:
        for sequence, funcid in self.key_bindings:
            self.unbind(sequence, funcid)
        self.key_bindings.clear()

    def clear_screen(self) -> None:
        self.unbind_keys()  # Screen-specific shortcuts never outlive their screen.
        if self.q_flash_after is not None:
            self.after_cancel(self.q_flash_after)
            self.q_flash_after = None
        self.cancel_truth_flash()
        if self.q_screen_active:  # Undo the question screen's whole-window colouring.
            self.q_screen_active = False
            self.container.configure(style="TFrame")
            self.configure(bg=self.default_bg)
        for child in self.container.winfo_children():
            child.destroy()

    def show_connecting_screen(self) -> None:
        self.clear_screen()
        ttk.Label(self.container, text="Connecting to Arduino", style="Title.TLabel").pack(pady=(60, 10))
        ttk.Label(self.container, text="Checking serial ports and validating GSR data...").pack()
        ttk.Progressbar(self.container, mode="indeterminate", length=320).pack(pady=14)
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
                elif event == "bpm":
                    arduino_us, bpm, state, received_at = payload  # type: ignore[misc]
                    self.handle_bpm(arduino_us, bpm, state, received_at)
                elif event == "bad_line":
                    self.bad_line_count += 1
                elif event == "serial_error":
                    self.handle_serial_error(str(payload))
        except queue.Empty:
            pass

        self.update_heart_label()
        self.update_sensor_status()
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
        # Keep a local reference: close_serial() sets self.connection to None.
        connection = self.connection
        assert connection is not None
        i2c_logged = False
        while not self.stop_reader.is_set():
            try:
                data = connection.readline()
                if not data:
                    continue
                try:
                    parsed = parse_line(data.decode("ascii", errors="strict"))
                    received_at = time.monotonic()
                    if parsed[0] == "gsr":
                        self.serial_events.put(("sample", (parsed[1], parsed[2], received_at)))
                    elif parsed[0] == "bpm":
                        self.serial_events.put(
                            ("bpm", (parsed[1], parsed[2], parsed[3], received_at))
                        )
                    elif not i2c_logged and parsed[1].startswith("i2c "):
                        i2c_logged = True
                        log_i2c_boot(parsed[1])
                    # Other comment lines are dropped and are not counted as bad lines.
                except (UnicodeDecodeError, ValueError):
                    self.serial_events.put(("bad_line", None))
            except (OSError, SerialException, TypeError, AttributeError) as exc:
                # pyserial on Windows raises TypeError/AttributeError if the port
                # is closed while readline() is blocked; that is a normal shutdown.
                if not self.stop_reader.is_set():
                    self.serial_events.put(("serial_error", str(exc)))
                return

    def show_connection_error(self, details: str) -> None:
        self.clear_screen()
        ttk.Button(self.container, text="Try again", command=self.retry_connection).pack(side="bottom", pady=10)
        ttk.Label(self.container, text="Arduino connection error", style="Title.TLabel").pack(pady=(30, 10))
        ttk.Label(
            self.container,
            text=details,
            foreground="#a40000",
            wraplength=640,
            justify="center",
        ).pack(pady=8)

    def retry_connection(self) -> None:
        self.close_serial()
        self.show_connecting_screen()
        threading.Thread(target=self.connect_worker, daemon=True).start()

    def show_setup_screen(self) -> None:
        self.clear_screen()
        # Start button is packed first (bottom) so it can never be clipped.
        self.setup_start_button = ttk.Button(
            self.container, text="Start examination", command=self.begin_examination
        )
        self.setup_start_button.pack(side="bottom", pady=(6, 4))
        ttk.Label(self.container, text="New Polygraph examination", style="Title.TLabel").pack(pady=(2, 6))
        ttk.Label(self.container, text=f"Arduino connected on {self.port_name}", foreground="#167a35").pack(pady=(0, 6))

        status = ttk.Frame(self.container)
        status.pack(pady=(0, 8))
        for column, heading in enumerate(("Sensor", "Detected", "Readings")):
            ttk.Label(status, text=heading, font=("Segoe UI", 10, "bold")).grid(
                row=0, column=column, sticky="w", padx=12
            )
        self.sensor_status_labels = {}
        for row, (key, name) in enumerate((("hr", "Heart-rate sensor"), ("gsr", "GSR sensor")), start=1):
            ttk.Label(status, text=name).grid(row=row, column=0, sticky="w", padx=12, pady=2)
            for column, field in enumerate(("detected", "readings"), start=1):
                label = tk.Label(status, text="", anchor="w")
                label.grid(row=row, column=column, sticky="w", padx=12, pady=2)
                self.sensor_status_labels[f"{key}_{field}"] = label
        self.update_sensor_status()

        form = ttk.Frame(self.container)
        form.pack()
        ttk.Label(form, text="Subject name:").grid(row=0, column=0, sticky="e", padx=8, pady=3)
        self.name_entry = ttk.Entry(form, width=35)
        self.name_entry.grid(row=0, column=1, padx=8, pady=3)
        self.name_entry.focus_set()

        ttk.Label(form, text="Rest duration (seconds):").grid(row=1, column=0, sticky="e", padx=8, pady=3)
        self.rest_entry = ttk.Entry(form, width=12)
        self.rest_entry.insert(0, str(DEFAULT_REST_SECONDS))
        self.rest_entry.grid(row=1, column=1, sticky="w", padx=8, pady=3)

        ttk.Label(form, text="Question phase duration (seconds):").grid(row=2, column=0, sticky="e", padx=8, pady=3)
        self.test_entry = ttk.Entry(form, width=12)
        self.test_entry.insert(0, str(DEFAULT_TEST_SECONDS))
        self.test_entry.grid(row=2, column=1, sticky="w", padx=8, pady=3)

        self.setup_start_button.configure(text="Start examination (Enter)")
        self.bind_keys({
            "<Return>": self.on_setup_enter,
            "<KP_Enter>": self.on_setup_enter,
        })

    def on_setup_enter(self, _event=None) -> str:
        # Debounce: ignore Enter right after a previous attempt (e.g. dismissing the
        # validation messagebox, or key auto-repeat) so it can't loop or double-start.
        now = time.monotonic()
        if now - getattr(self, "_last_enter_t", 0.0) >= 0.3:
            self._last_enter_t = now
            self.begin_examination()
            self._last_enter_t = time.monotonic()  # Measure from after any messagebox closed.
        return "break"

    def begin_examination(self) -> None:
        name =self.name_entry.get().strip()
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
        self.bpm_readings.clear()
        self.anchor_us = None  # Re-anchored on the first sample of this recording.
        self.recording_started_at = time.monotonic()
        self.phase_started_at = self.recording_started_at
        self.last_valid_sample_at = self.recording_started_at
        self.recording = True
        self.show_rest_screen()

    def show_rest_screen(self) -> None:
        self.clear_screen()
        ttk.Label(self.container, text="Rest period", style="Title.TLabel").pack(pady=(30, 8))
        ttk.Label(self.container, text="The subject should remain still and quiet.").pack(pady=4)
        self.timer_label = ttk.Label(self.container, text="", style="Timer.TLabel")
        self.timer_label.pack(pady=12)
        self.heart_label = ttk.Label(self.container, text="", font=("Segoe UI", 14))
        self.heart_label.pack(pady=4)
        self.update_heart_label()
        self.warning_label = tk.Label(self.container, text="", font=("Segoe UI", 12, "bold"))
        self.warning_label.pack(pady=4)
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

        self.q_screen_active = True
        self.container.configure(style="Q.TFrame")
        self.state_banner = ttk.Label(self.container, text="", style="QBanner.TLabel")
        self.set_question_state("waiting")

        # Finish button is packed first (bottom) so it can never be clipped.
        ttk.Button(self.container, text="Finish examination (F)", command=self.finish_measurement).pack(side="bottom", pady=(4, 0))

        top = ttk.Frame(self.container, style="Q.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text="Question phase", style="QTitle.TLabel").pack(side="left")
        self.test_timer_label = ttk.Label(top, text="", font=("Consolas", 18, "bold"), style="Q.TLabel")
        self.test_timer_label.pack(side="right")

        self.state_banner.pack(pady=(6, 0))
        self.gap_label = ttk.Label(self.container, text="", style="QGapWait.TLabel")
        self.gap_label.pack(pady=(2, 0))
        self.question_label = ttk.Label(self.container, style="QQuestion.TLabel", justify="center")
        self.question_label.pack(pady=(6, 6))
        self.show_question_text(self.question_label, 1)
        action_row = ttk.Frame(self.container, style="Q.TFrame")
        action_row.pack(pady=4)
        self.mark_button = ttk.Button(
            action_row,
            text="Mark question time (Space / Enter)",
            command=self.mark_question,
            takefocus=False,  # Space must only reach the window-level handler.
        )
        self.mark_button.pack(side="left", padx=6)
        ttk.Button(
            action_row, text="Undo (Backspace)", command=self.undo_last, takefocus=False
        ).pack(side="left", padx=6)

        answer_frame = ttk.Frame(self.container, style="Q.TFrame")
        answer_frame.pack(pady=8)
        ttk.Label(answer_frame, text="Subject's answer:", style="Q.TLabel").pack(side="left", padx=8)
        self.answer_var.set("")
        self.answer_buttons: list[ttk.Radiobutton] = []
        for number, choice in enumerate(ANSWER_CHOICES, start=1):
            button = ttk.Radiobutton(
                answer_frame,
                text=f"{choice} ({number})",
                variable=self.answer_var,
                value=choice,
                style="Q.TRadiobutton",
                command=self.save_answer,  # Clicking saves immediately.
                state="disabled",
                takefocus=False,  # Space must never select an answer.
            )
            button.pack(side="left", padx=14)
            self.answer_buttons.append(button)

        ttk.Label(
            self.container,
            text="Press Space or Enter (or click) when the question is asked. "
            f"Then press {ANSWER_HINT}. Backspace = undo.",
            style="Q.TLabel",
            wraplength=640,
            justify="center",
        ).pack(pady=2)
        info_row = ttk.Frame(self.container, style="Q.TFrame")
        info_row.pack(pady=2)
        self.status_label = ttk.Label(info_row, text="Waiting for question 1", style="Q.TLabel")
        self.status_label.pack(side="left", padx=12)
        self.heart_label = ttk.Label(info_row, text="", font=("Segoe UI", 12), style="Q.TLabel")
        self.heart_label.pack(side="left", padx=12)
        self.update_heart_label()
        self.warning_label = tk.Label(self.container, text="", font=("Segoe UI", 12, "bold"), bg=self.q_bg)
        self.warning_label.pack(pady=2)
        handlers: dict[str, object] = {
            "<Return>": self.on_enter_key,
            "<KP_Enter>": self.on_enter_key,
            "<space>": self.on_enter_key,
            "<BackSpace>": lambda _e: self.undo_last(),
            "<Key-f>": self.on_finish_key,
            "<Key-F>": self.on_finish_key,
        }
        for number, choice in enumerate(ANSWER_CHOICES, start=1):
            handlers[f"<Key-{number}>"] = lambda _e, c=choice: self.answer_by_key(c)
            handlers[f"<KP_{number}>"] = lambda _e, c=choice: self.answer_by_key(c)
        self.bind_keys(handlers)
        self.update_test_timer()

    def show_question_text(self, label: ttk.Label, number: int) -> None:
        """Show the wording of question `number` on a large label, wrapped for its direction."""
        text = QUESTION_TEXTS[number - 1]
        if is_rtl(text):
            label.config(text=wrap_rtl(text, self.question_font, QUESTION_WRAP_PIXELS), wraplength=0)
        else:
            label.config(text=text, wraplength=QUESTION_WRAP_PIXELS)

    def on_enter_key(self, _event: object | None = None) -> None:
        if self.finished or not self.recording or self.pending_question is not None:
            return
        if time.monotonic() - self.last_answer_saved_at < MARK_DEBOUNCE_SECONDS:
            return  # Key repeat right after an answer must not mark the next question.
        self.mark_question()

    def on_finish_key(self, _event: object | None = None) -> None:
        # F sits under the answering hand, so confirm (default No) before finishing.
        if self.finished or not self.recording:
            return
        if messagebox.askyesno("Finish examination", "Finish the examination now?", default=messagebox.NO):
            self.finish_measurement()

    def answer_by_key(self, choice: str) -> None:
        if self.finished or self.pending_question is None:
            return
        self.answer_var.set(choice)
        self.save_answer()

    def update_test_timer(self) -> None:
        if not self.recording or self.finished or self.phase_started_at is None:
            return
        elapsed = time.monotonic() - self.phase_started_at
        remaining = max(0, self.test_seconds - elapsed)
        minutes, seconds = divmod(int(remaining), 60)
        self.test_timer_label.config(text=f"{minutes:02d}:{seconds:02d}")
        self.update_gap_label()
        if remaining <= 0:
            self.finish_measurement()
        else:
            self.after(100, self.update_test_timer)

    def update_gap_label(self) -> None:
        """Advisory counter: time since the latest question mark."""
        label = getattr(self, "gap_label", None)
        if label is None or not label.winfo_exists() or self.phase_started_at is None:
            return
        latest = self.pending_question or (self.questions[-1] if self.questions else None)
        if latest is None or self.recording_started_at is None:
            label.config(text="")  # The rest period already covered the first question.
            return
        since = max(0.0, time.monotonic() - (self.recording_started_at + latest.pc_time_s))
        if since >= MIN_QUESTION_GAP_SECONDS:
            label.config(text=f"Since last question: {since:.1f} s — ready", style="QGapReady.TLabel")
        else:
            label.config(text=f"Since last question: {since:.1f} s — wait", style="QGapWait.TLabel")

    def current_times(self) -> tuple[float, float]:
        """Return (arduino_based_time_s, pc_time_s) since recording start."""
        now = time.monotonic()
        pc_time = now - self.recording_started_at
        if self.anchor_us is not None:
            # Same Arduino-based clock as the samples: last sample time plus the
            # PC time elapsed since that sample arrived.
            return self.last_sample_time_s + (now - self.last_sample_received_at), pc_time
        return pc_time, pc_time  # No sample yet: fall back to PC time.

    def mark_question(self) -> None:
        if self.pending_question is not None or self.recording_started_at is None:
            return
        number = len(self.questions) + 1
        question_time, pc_time = self.current_times()
        self.pending_question = Question(number=number, time_s=question_time, pc_time_s=pc_time)
        self.set_answer_mode(True)
        self.status_label.config(
            text=f"Question {number} marked at {question_time:.3f} s — press {ANSWER_HINT}"
        )

    def save_answer(self) -> None:
        question = self.pending_question
        if question is None:
            return  # Nothing pending: a double press or stray click is ignored.
        answer = self.answer_var.get()
        if answer not in ANSWER_CHOICES:
            return
        question.answer = answer
        self.questions.append(question)
        self.pending_question = None  # Cleared first, so a repeated key cannot save twice.
        self.last_answer_saved_at = time.monotonic()
        if len(self.questions) >= len(QUESTION_TEXTS):
            self.finish_measurement()
            return
        next_number = len(self.questions) + 1
        self.show_question_text(self.question_label, next_number)
        self.status_label.config(
            text=(
                f"Saved: Question {question.number} = {answer}. "
                f"Waiting for question {next_number}"
            )
        )
        self.set_answer_mode(False, saved=True)
        self.focus_set()  # Keep focus on the window, not a widget that reacts to Space.

    def set_question_state(self, state: str) -> None:
        """Colour the whole question screen: 'waiting' (neutral), 'awaiting' (amber), 'saved' (green)."""
        if self.q_flash_after is not None:
            self.after_cancel(self.q_flash_after)
            self.q_flash_after = None
        bg, text = {
            "waiting": (self.q_neutral_bg, "READY – mark next question"),
            "awaiting": ("#ffd54f", "ANSWER?  " + ANSWER_HINT.replace(" = ", " ").replace(", ", " · ")),
            "saved": ("#81c784", "SAVED"),
        }[state]
        self.q_bg = bg
        for name in (
            "Q.TFrame", "Q.TLabel", "Q.TRadiobutton", "QTitle.TLabel", "QQuestion.TLabel", "QBanner.TLabel",
            "QGapWait.TLabel", "QGapReady.TLabel",
        ):
            self.style.configure(name, background=bg)
        self.container.configure(style="Q.TFrame")
        self.configure(bg=bg)  # Window behind the padding (and any tk widgets).
        self.state_banner.config(text=text)
        warning = getattr(self, "warning_label", None)
        if warning is not None and warning.winfo_exists() and not self.zero_warning_active:
            warning.config(bg=bg)
        if state == "saved":
            self.q_flash_after = self.after(400, self.end_saved_flash)

    def end_saved_flash(self) -> None:
        self.q_flash_after = None
        if self.q_screen_active and self.pending_question is None:
            self.set_question_state("waiting")

    def set_answer_mode(self, awaiting_answer: bool, saved: bool = False) -> None:
        """Enable the answer buttons (and disable marking) while a question awaits its answer."""
        self.set_question_state("awaiting" if awaiting_answer else "saved" if saved else "waiting")
        self.answer_var.set("")
        for button in self.answer_buttons:
            button.config(state="normal" if awaiting_answer else "disabled")
        self.mark_button.config(state="disabled" if awaiting_answer else "normal")

    def undo_last(self) -> None:
        """Cancel a pending mark, or reopen the last saved question for a new answer."""
        if self.finished or not self.recording:
            return
        now = time.monotonic()
        if now - self.last_undo_at < MARK_DEBOUNCE_SECONDS:
            return  # A held Backspace must undo only one step.
        if self.pending_question is not None:
            number = self.pending_question.number
            self.pending_question = None
            self.last_undo_at = now
            self.set_answer_mode(False)
            self.status_label.config(text=f"Mark for question {number} cancelled. Waiting for question {number}")
        elif self.questions:
            question = self.questions.pop()
            question.answer = ""
            self.pending_question = question  # Keeps its original mark time.
            self.last_undo_at = now
            self.set_answer_mode(True)
            self.show_question_text(self.question_label, question.number)
            self.status_label.config(
                text=(
                    f"Question {question.number} reopened (marked at {question.time_s:.3f} s)"
                    f" — press {ANSWER_HINT}"
                )
            )
        else:
            return
        self.focus_set()

    def handle_sample(self, arduino_us: int, raw_adc: int, received_at: float) -> None:
        self.last_valid_sample_at = received_at
        self.last_gsr_raw = raw_adc
        if 0 < raw_adc < GSR_SHORT_MIN:
            self.last_gsr_in_range_at = received_at
        unwrapped_us = self.unwrapper.unwrap(arduino_us)  # Keep unwrapping while idle.
        if self.recording and self.recording_started_at is not None:
            pc_time = received_at - self.recording_started_at
            if self.anchor_us is None:
                self.anchor_us = unwrapped_us
                self.anchor_offset_s = pc_time
            elapsed = (unwrapped_us - self.anchor_us) / 1e6 + self.anchor_offset_s
            self.last_sample_time_s = elapsed
            self.last_sample_received_at = received_at
            voltage = raw_adc * ADC_REFERENCE_VOLTS / ADC_MAX
            self.samples.append(Sample(elapsed, pc_time, arduino_us, raw_adc, voltage))

            if raw_adc == 0:
                if self.zero_started_at is None:
                    self.zero_started_at = received_at
                if received_at - self.zero_started_at >= ZERO_WARNING_AFTER_SECONDS:
                    self.set_zero_warning(True)
            else:
                self.zero_started_at = None
                self.set_zero_warning(False)

    def handle_bpm(self, arduino_us: int, bpm: int, state: str, received_at: float) -> None:
        unwrapped_us = self.unwrapper.unwrap(arduino_us)
        self.latest_bpm = (bpm, state, received_at)
        self.update_heart_label()
        if self.recording and self.recording_started_at is not None:
            pc_time = received_at - self.recording_started_at
            if self.anchor_us is not None:
                time_s = (unwrapped_us - self.anchor_us) / 1e6 + self.anchor_offset_s
            else:
                time_s = pc_time  # No GSR sample yet: fall back to PC time.
            self.bpm_readings.append(BpmReading(time_s, pc_time, arduino_us, bpm, state))

    def heart_rate_text(self) -> str:
        if self.latest_bpm is None or time.monotonic() - self.latest_bpm[2] > BPM_STALE_SECONDS:
            return "Heart rate: no data"
        bpm, state, _received_at = self.latest_bpm
        if state == "LOCKED" and bpm > 0:
            return f"Heart rate: {bpm} BPM"
        return f"Heart rate: -- ({state})"

    def update_heart_label(self) -> None:
        label = self.heart_label
        if label is not None and label.winfo_exists():
            label.config(text=self.heart_rate_text())

    def sensor_status(self) -> dict[str, tuple[bool, str]]:
        """(ok, text) for each setup-screen status cell."""
        now = time.monotonic()
        status: dict[str, tuple[bool, str]] = {}

        if self.latest_bpm is None or now - self.latest_bpm[2] > BPM_STALE_SECONDS:
            status["hr_detected"] = (False, "No (no heart-rate data)")
            status["hr_readings"] = (False, "No")
        else:
            bpm, state, _received_at = self.latest_bpm
            if state == "NO_SENSOR":
                status["hr_detected"] = (False, "No (check SDA/SCL wiring)")
                status["hr_readings"] = (False, "No")
            else:
                status["hr_detected"] = (True, "Yes")
                if state == "LOCKED" and bpm > 0:
                    status["hr_readings"] = (True, f"Yes ({bpm} BPM)")
                else:
                    status["hr_readings"] = (False, f"No ({state.replace('_', ' ').lower()})")

        if self.last_valid_sample_at is None or self.last_gsr_raw is None or (
            now - self.last_valid_sample_at > SERIAL_TIMEOUT_SECONDS
        ):
            status["gsr_detected"] = (False, "No (no GSR data)")
            status["gsr_readings"] = (False, "No")
        else:
            status["gsr_detected"] = (True, "Yes")
            if self.last_gsr_in_range_at is not None and now - self.last_gsr_in_range_at <= GSR_READING_STALE_SECONDS:
                volts = self.last_gsr_raw * ADC_REFERENCE_VOLTS / ADC_MAX
                status["gsr_readings"] = (True, f"Yes ({volts:.2f} V)")
            elif self.last_gsr_raw >= GSR_SHORT_MIN:
                status["gsr_readings"] = (False, "No (maxed at 5 V, check wiring)")
            else:
                status["gsr_readings"] = (False, "No (0 V, check electrodes)")
        return status

    def update_sensor_status(self) -> None:
        labels = self.sensor_status_labels
        if not labels or not next(iter(labels.values())).winfo_exists():
            return  # Not on the setup screen.
        for key, (ok, text) in self.sensor_status().items():
            labels[key].config(text=text, fg="#167a35" if ok else "#a40000")

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
                    pady=3,
                )
            else:
                label.config(text="", bg=self.q_bg if self.q_screen_active else self.cget("bg"), padx=0, pady=0)

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
            answer = self.answer_var.get()
            if answer in ANSWER_CHOICES:
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
                self.finish_truth()
            return

        self.show_truth_screen()

    def show_truth_screen(self) -> None:
        self.clear_screen()
        self.truth_index = 0
        # Swallow a held key: 1 and 2 also answered the question that led here.
        self.last_answer_saved_at = time.monotonic() + SCREEN_CHANGE_LOCKOUT_SECONDS
        # Bottom block is packed first so Tk reserves its space before the content.
        self.truth_bottom_frame = ttk.Frame(self.container)
        self.truth_bottom_frame.pack(side="bottom", fill="x")
        ttk.Label(self.container, text="Classify each answer", style="Title.TLabel").pack(pady=(4, 8))
        # Plain tk.Label (ttk takes no bg); stays on screen across question changes so the
        # flash remains visible after the screen advances. Fixed height reserves its space.
        self.truth_flash_label = tk.Label(
            self.container, text="", font=("Segoe UI", 28, "bold"), fg="white",
            bg=self.q_neutral_bg, height=1,
        )
        self.truth_flash_label.pack(fill="x", padx=40, pady=(0, 6))
        self.truth_progress_label = ttk.Label(self.container, text="")
        self.truth_progress_label.pack()
        self.truth_question_label = ttk.Label(self.container, text="", style="Question.TLabel", justify="center")
        self.truth_question_label.pack(pady=6)
        self.truth_answer_label = ttk.Label(self.container, text="", font=("Segoe UI", 14), wraplength=640)
        self.truth_answer_label.pack(pady=4)
        self.truth_var.set("")
        self.truth_choices_frame = ttk.Frame(self.container)
        self.truth_choices_frame.pack(pady=10)
        ttk.Radiobutton(
            self.truth_choices_frame, text="Truth (1)", variable=self.truth_var, value="Truth",
            command=lambda: self.classify_truth("Truth"),
        ).pack(side="left", padx=24)
        ttk.Radiobutton(
            self.truth_choices_frame, text="Lie (2)", variable=self.truth_var, value="Lie",
            command=lambda: self.classify_truth("Lie"),
        ).pack(side="left", padx=24)
        self.truth_back_button = ttk.Button(self.truth_bottom_frame, text="Back", command=self.truth_back)
        self.truth_back_button.pack(pady=(4, 4))
        ttk.Label(
            self.truth_bottom_frame,
            text="Keys: 1 = Truth, 2 = Lie (moves to next question), Backspace = previous question",
            wraplength=640,
            justify="center",
        ).pack()
        self.bind_keys(
            {
                "<Key-1>": lambda _e: self.classify_truth("Truth"),
                "<KP_1>": lambda _e: self.classify_truth("Truth"),
                "<Key-2>": lambda _e: self.classify_truth("Lie"),
                "<KP_2>": lambda _e: self.classify_truth("Lie"),
                "<BackSpace>": self.truth_back,
            }
        )
        self.render_truth_question()

    def render_truth_question(self) -> None:
        question = self.questions[self.truth_index]
        self.truth_progress_label.config(text=f"Question {self.truth_index + 1} of {len(self.questions)}")
        self.show_question_text(self.truth_question_label, question.number)
        self.truth_answer_label.config(text=f"Subject's answer: {question.answer}")
        self.truth_var.set(question.truth)  # Shows the existing classification when going back.
        self.truth_back_button.state(["!disabled"] if self.truth_index > 0 else ["disabled"])

    def truth_debounced(self) -> bool:
        now = time.monotonic()
        if now - self.last_answer_saved_at < MARK_DEBOUNCE_SECONDS:
            return True
        self.last_answer_saved_at = now
        return False

    def classify_truth(self, truth: str) -> None:
        """Save Truth/Lie for the current question and advance immediately."""
        if self.finished or self.truth_index >= len(self.questions):
            return
        if self.truth_debounced():
            self.truth_var.set(self.questions[self.truth_index].truth)  # Undo a radiobutton click.
            return
        self.questions[self.truth_index].truth = truth
        self.truth_index += 1
        if self.truth_index >= len(self.questions):
            self.finish_truth()
            return
        self.render_truth_question()
        self.flash_truth(truth)

    def flash_truth(self, truth: str) -> None:
        """Briefly colour the banner green (Truth) or red (Lie); never blocks."""
        label = self.truth_flash_label
        if label is None:
            return
        self.cancel_truth_flash()  # Rapid presses: drop the pending revert so colour never sticks.
        color = TRUTH_FLASH_COLOR if truth == "Truth" else LIE_FLASH_COLOR
        try:
            label.config(text=truth.upper(), bg=color)
        except tk.TclError:
            return
        self.truth_flash_after = self.after(FLASH_MS, self.end_truth_flash)

    def end_truth_flash(self) -> None:
        self.truth_flash_after = None
        label = self.truth_flash_label
        if label is not None:
            try:
                label.config(text="", bg=self.q_neutral_bg)
            except tk.TclError:
                pass  # Screen already left.

    def cancel_truth_flash(self) -> None:
        if self.truth_flash_after is not None:
            try:
                self.after_cancel(self.truth_flash_after)
            except tk.TclError:
                pass
            self.truth_flash_after = None

    def truth_back(self, _event: object | None = None) -> None:
        if self.finished or self.truth_index <= 0 or self.truth_debounced():
            return
        self.truth_index -= 1
        self.render_truth_question()

    def finish_truth(self) -> None:
        if self.finished:
            return
        self.finished = True
        self.unbind_keys()
        self.save_files(partial=False)
        self.close_serial()
        self.show_results_screen()

    def show_results_screen(self) -> None:
        """Final screen, in one maximized window: the summary table to the left of the graph."""
        self.clear_screen()
        self.minsize(1100, 600)
        try:
            self.state("zoomed")
        except tk.TclError:
            pass  # "zoomed" does not exist on every platform.
        results = ttk.Frame(self.container)
        results.pack(fill="both", expand=True)
        results.rowconfigure(0, weight=1)
        results.columnconfigure(0, weight=2, minsize=560, uniform="results")
        results.columnconfigure(1, weight=3, uniform="results")
        table_pane = ttk.Frame(results)
        table_pane.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        graph_pane = ttk.Frame(results)
        graph_pane.grid(row=0, column=1, sticky="nsew")
        self.build_summary_table(table_pane)
        self.after(10, self.build_graph, graph_pane)  # Later, so the table appears without waiting.

    def build_graph(self, parent: ttk.Frame) -> None:
        try:
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

            figure = plot_examination(*self.last_saved_paths, subject_name=self.subject_name)
            canvas = FigureCanvasTkAgg(figure, master=parent)
        except Exception as error:  # The table and saved files must survive a graph failure.
            ttk.Label(
                parent, text=f"The graph could not be drawn:\n{error}", foreground="#a40000", wraplength=500
            ).pack(expand=True)
            return
        toolbar = NavigationToolbar2Tk(canvas, parent, pack_toolbar=False)
        toolbar.update()
        toolbar.pack(side="bottom", fill="x")
        parent.pack_propagate(False)  # The pane sets the graph's size, not the figure's 13x8 inches.
        canvas.get_tk_widget().pack(fill="both", expand=True)
        canvas.draw()

    def build_summary_table(self, parent: ttk.Frame) -> None:
        # Bottom items are packed first so they can never be clipped.
        ttk.Button(parent, text="Close (Esc)", command=self.on_close).pack(side="bottom", pady=(8, 4))
        ttk.Label(
            parent,
            text=f"Files saved in: {OUTPUT_FOLDER}\n{self.recording_stats()}",
            foreground="#6a6a6a",
            wraplength=540,
            justify="center",
        ).pack(side="bottom", pady=(4, 0))
        # PLACEHOLDER: remove this note once the prediction columns are real.
        ttk.Label(parent, text="Prediction and certainty are random placeholders.", foreground="#6a6a6a").pack(
            side="bottom"
        )
        ttk.Label(parent, text="Examination summary", style="Title.TLabel").pack(pady=(4, 8))

        table_frame = ttk.Frame(parent)
        table_frame.pack(fill="both", expand=True)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(1, weight=1)
        # A grid of labels, not a ttk.Treeview: Treeview rows share one height, so text can't wrap.
        cell_padx, cell_pady = 6, 5
        columns = (  # (heading, fixed width or 0 to take the remaining space, anchor)
            ("#", 40, "center"),
            ("Question", 0, "w"),
            ("Answer", 90, "center"),
            ("Prediction", 110, "center"),
            ("Certainty", 90, "center"),
        )
        fixed_width = sum(width for _heading, width, _anchor in columns)

        header = tk.Frame(table_frame, bg="#e0e0e0")
        header.grid(row=0, column=0, sticky="ew")
        canvas = tk.Canvas(table_frame, bg="white", highlightthickness=0)
        canvas.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scrollbar.set)
        rows_frame = tk.Frame(canvas, bg="white")
        rows_window = canvas.create_window(0, 0, anchor="nw", window=rows_frame)

        for frame in (header, rows_frame):
            for index, (_heading, width, _anchor) in enumerate(columns):
                frame.columnconfigure(index, minsize=width, weight=0 if width else 1)
        for index, (heading, _width, anchor) in enumerate(columns):
            tk.Label(
                header, text=heading, font=("Segoe UI", 11, "bold"), bg="#e0e0e0", anchor=anchor,
                padx=cell_padx, pady=cell_pady,
            ).grid(row=0, column=index, sticky="nsew")

        body_font = tkfont.Font(family="Segoe UI", size=12)
        question_labels: list[tuple[tk.Label, str]] = []
        for row, question in enumerate(self.questions):
            text = QUESTION_TEXTS[question.number - 1]
            # PLACEHOLDER: replace with the classifier's output once it exists.
            prediction = random.choice(("Truth", "Lie"))
            certainty = random.randint(50, 99)
            values = (question.number, text, question.answer, prediction, f"{certainty}%")
            background = "white" if row % 2 == 0 else "#f3f3f3"
            for index, (value, (_heading, _width, anchor)) in enumerate(zip(values, columns)):
                label = tk.Label(
                    rows_frame, text=str(value), font=body_font, bg=background, anchor=anchor,
                    justify="left", padx=cell_padx, pady=cell_pady,
                )
                label.grid(row=row, column=index, sticky="nsew")
                if index == 1:
                    question_labels.append((label, text))
                    if is_rtl(text):
                        label.config(anchor="e", justify="right")

        def on_canvas_resize(event: tk.Event) -> None:
            canvas.itemconfigure(rows_window, width=event.width)
            wrap_width = max(100, event.width - fixed_width - 2 * cell_padx)
            for label, text in question_labels:
                if is_rtl(text):
                    label.config(text=wrap_rtl(text, body_font, wrap_width))
                else:
                    label.config(wraplength=wrap_width)

        def on_mouse_wheel(event: tk.Event) -> None:
            over_table = str(event.widget).startswith(str(canvas))  # Not when pointing at the graph.
            if over_table and rows_frame.winfo_height() > canvas.winfo_height():
                canvas.yview_scroll(-event.delta // 120, "units")

        canvas.bind("<Configure>", on_canvas_resize)
        rows_frame.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        self.bind_keys({"<MouseWheel>": on_mouse_wheel})

    def recording_stats(self) -> str:
        return (
            f"Valid samples: {len(self.samples):,}; heart-rate readings: {len(self.bpm_readings):,}; "
            f"malformed serial lines ignored: {self.bad_line_count}"
        )

    def save_files(self, partial: bool) -> str:
        voltage_path, questions_path, bpm_path = self.last_saved_paths = save_examination(
            OUTPUT_FOLDER, self.file_stem, self.samples, self.questions, self.bpm_readings, partial
        )

        kind = "Partial files were saved" if partial else "Files saved"
        return (
            f"{kind} in:\n{OUTPUT_FOLDER}\n\n"
            f"{voltage_path.name}\n{questions_path.name}\n{bpm_path.name}\n\n"
            f"{self.recording_stats()}"
        )

    def close_serial(self) -> None:
        self.stop_reader.set()
        # Let the reader leave readline() (timeout=0.25 s) before closing the port.
        reader = getattr(self, "reader_thread", None)
        if reader is not None and reader.is_alive() and reader is not threading.current_thread():
            reader.join(timeout=1.0)
        if self.connection is not None and self.connection.is_open:
            try:
                self.connection.close()
            except SerialException:
                pass
        self.connection = None

    def on_close(self) -> None:
        if self.recording:
            choice = messagebox.askyesnocancel(
                "Close examination",
                "An examination is running. Save partial data before closing?\n\n"
                "Yes: save and exit\nNo: exit without saving\nCancel: keep recording",
                default=messagebox.CANCEL,
            )
            if choice is None:
                return
            self.recording = False
            if choice:
                self.save_files(partial=True)
        self.finished = True
        self.close_serial()
        self.destroy()


if __name__ == "__main__":
    app = PolygraphApp()
    app.mainloop()
