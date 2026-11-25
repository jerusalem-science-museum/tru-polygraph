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
from tkinter import filedialog, messagebox, ttk

import serial
from serial import SerialException

# The sibling modules sit next to this script; make them importable from any cwd.
_APP_DIR = str(Path(__file__).resolve().parent)
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)
from arduino import ADC_MAX, MicrosUnwrapper, detect_arduino, parse_line  # noqa: E402
from records import (  # noqa: E402
    SHARED_QUESTIONS_FILENAME,
    BpmReading,
    Question,
    QuestionsFileMismatch,
    Sample,
    append_shared_questions,
    question_rows,
    safe_filename,
    save_examination,
    write_fallback_questions,
)
from config import (  # noqa: E402
    ADC_REFERENCE_VOLTS,
    ANSWER_CHOICES,
    BPM_DISPLAY_HOLD_SECONDS,
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
    RECONNECT_INTERVAL_SECONDS,
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
HELD_BPM_COLOR = "#8a8a8a"
# Windows sends repeated presses (no releases) while a key is held, the first after the
# keyboard repeat delay (0.25-1 s). A Space press within this time of the previous one, with
# no release in between, is a repeat and must not set a second mark.
KEY_REPEAT_WINDOW_SECONDS = 1.0


def heart_rate_display(
    latest: tuple[int, str, float] | None,
    last_locked: tuple[int, float] | None,
    now: float,
    stale_seconds: float = BPM_STALE_SECONDS,
    hold_seconds: float = BPM_DISPLAY_HOLD_SECONDS,
) -> tuple[str, bool]:
    """Return (text, held) for the heart-rate label.

    latest is (bpm, state, received_at) of the newest reading, last_locked is (bpm, received_at)
    of the newest LOCKED one. After the lock is lost the last locked BPM stays on screen, marked
    as held, for hold_seconds, so a brief movement doesn't blank it. Stale data and NO_SENSOR
    are faults the operator must see at once, so they are never held. Screen only: the
    recorded readings are always what the firmware sent.
    """
    if latest is None or now - latest[2] > stale_seconds:
        return "Heart rate: no data", False
    bpm, state, _received_at = latest
    if state == "LOCKED" and bpm > 0:
        return f"Heart rate: {bpm} BPM", False
    if state != "NO_SENSOR" and last_locked is not None and now - last_locked[1] <= hold_seconds:
        return f"Heart rate: {last_locked[0]} BPM (holding)", True
    return f"Heart rate: -- ({state})", False


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


def build_question_table(parent: tk.Misc, rows: list[tuple]):
    """Pack a scrollable table of questions into parent; returns the mouse-wheel handler.

    Each row is (number, question text, answer, truth/lie, prediction, certainty). The caller
    binds the returned handler to <MouseWheel> on the table's toplevel window.
    """
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
        ("Truth/Lie", 90, "center"),
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
    for row, values in enumerate(rows):
        text = str(values[1])
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
    return on_mouse_wheel


class PolygraphApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
        self.title(f"Polygraph {VERSION}")
        self.geometry("800x560")
        self.minsize(700, 480)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        # Bound directly (not via bind_keys) so clear_screen() never removes it.
        for sequence in ("<Escape>", "<Control-q>", "<Control-Q>"):
            self.bind(sequence, self.unless_paused(lambda _event: self.on_close()))

        self.connection: serial.Serial | None = None
        self.port_name = ""
        self.reader_thread: threading.Thread | None = None
        self.stop_reader = threading.Event()  # Replaced for each reader (see start_reader).
        self.serial_events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.connect_thread: threading.Thread | None = None
        self.retry_after: str | None = None  # Scheduled automatic connection attempt.
        self.error_details_label: ttk.Label | None = None  # Connection error screen only.
        self.retry_note_label: ttk.Label | None = None

        # Connection loss during an examination: everything on the operator side pauses
        # (countdowns, keys, question gap) while the data keep real time. operator_clock()
        # is PC time minus the paused time and drives the countdowns and the question gap.
        self.connection_lost = False
        self.loss_window: tk.Toplevel | None = None
        self.loss_details_label: ttk.Label | None = None
        self.paused_at: float | None = None
        self.paused_total_s = 0.0
        self.answer_clock_at: dict[int, float] = {}  # Question number -> operator_clock() of its answer.

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
        self.last_saved_paths: tuple[Path, Path] | None = None  # (voltage, bpm) of the last save.
        self.questions_saved_to: Path | None = None  # questions.csv, or the fallback file.

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
        self.last_locked_bpm: tuple[int, float] | None = None  # (bpm, received_at), for the display hold.
        self.heart_label: ttk.Label | None = None
        self.last_gsr_raw: int | None = None
        self.last_gsr_in_range_at: float | None = None  # Last sample with 0 < raw < GSR_SHORT_MIN.
        self.sensor_status_labels: dict[str, tk.Label] = {}  # Setup screen only.
        self.key_bindings: list[tuple[str, str]] = []  # (sequence, funcid) on this window.
        self.answer_var = tk.StringVar(value="")
        self.truth_var = tk.StringVar(value="")
        self.last_answer_saved_at = 0.0
        self.last_mark_at = 0.0  # Time of the latest question start or end mark.
        self.mark_key_down = False  # Space/Enter held (no release seen since its press).
        self.mark_key_pressed_at = 0.0  # Latest Space/Enter press, auto-repeats included.
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
        self.start_connect()

    def unless_paused(self, handler):
        """Wrap a key handler so it does nothing while a connection loss pauses the examination."""
        def wrapped(event: tk.Event):
            if self.connection_lost:
                return "break"
            return handler(event)
        return wrapped

    def bind_keys(self, handlers: dict[str, object]) -> None:
        """Bind keys on the toplevel window (works whatever widget has focus)."""
        for sequence, handler in handlers.items():
            self.key_bindings.append((sequence, self.bind(sequence, self.unless_paused(handler))))

    def bind_letter_keys(self, handlers: dict[str, object]) -> None:
        """Bind letter shortcuts (lowercase letter -> handler) by physical key, so they work
        on any keyboard layout: with a Hebrew layout the H key types a Hebrew letter (only
        Shift+H types "H"), which a <Key-h> binding never sees. At most one call per screen,
        since all letters share the window's single <Key> binding."""
        def on_key(event: tk.Event):
            if event.state & 0x4:  # Control held: leave Ctrl+letter shortcuts alone.
                return None
            for letter, handler in handlers.items():
                # On Windows the keycode is the virtual-key code, which for A-Z is the
                # uppercase letter's code whatever the layout.
                if event.keysym.lower() == letter or (sys.platform == "win32" and event.keycode == ord(letter.upper())):
                    return handler(event)
            return None
        self.bind_keys({"<Key>": on_key})

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
        self.add_open_recording_button()

    def add_open_recording_button(self) -> None:
        """Saved recordings can be viewed without the Arduino, so offer this on every
        screen shown before an examination: connecting, connection error and setup."""
        ttk.Button(
            self.container, text="Open recording…", command=self.open_recording
        ).pack(side="bottom", pady=(0, 4))

    def connection_needed(self) -> bool:
        """True while the app should look for the Arduino: before an examination, or after a
        connection loss during one. The truth and results screens no longer need it."""
        return (
            self.connection is None
            and not self.finished
            and (self.connection_lost or self.recording_started_at is None)
        )

    def start_connect(self) -> None:
        """Run one detect_arduino() attempt in the background; never two at once."""
        self.cancel_retry()
        if not self.connection_needed():
            return
        if self.connect_thread is not None and self.connect_thread.is_alive():
            return  # Its result arrives as a connected/connect_error event.
        self.connect_thread = threading.Thread(target=self.connect_worker, daemon=True)
        self.connect_thread.start()

    def schedule_retry(self) -> None:
        self.cancel_retry()
        self.retry_after = self.after(int(RECONNECT_INTERVAL_SECONDS * 1000), self.start_connect)

    def cancel_retry(self) -> None:
        if self.retry_after is not None:
            self.after_cancel(self.retry_after)
            self.retry_after = None

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
                    connection, port = payload  # type: ignore[misc]
                    if not self.connection_needed():
                        connection.close()  # The examination ended while this attempt ran.
                        continue
                    self.connection, self.port_name = connection, port
                    self.last_valid_sample_at = time.monotonic()
                    self.start_reader()
                    if self.connection_lost:
                        self.resume_after_reconnect()
                    else:
                        self.show_setup_screen()
                elif event == "connect_error":
                    if not self.connection_needed():
                        continue
                    if self.connection_lost:
                        self.update_loss_details(str(payload))
                    else:
                        self.show_connection_error(str(payload))
                    self.schedule_retry()
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
        # Watch for silence while connected: during an examination, and on the setup screen.
        watching = self.connection is not None and not self.connection_lost and (
            self.recording or (self.recording_started_at is None and not self.finished)
        )
        if watching and self.last_valid_sample_at is not None:
            silence = time.monotonic() - self.last_valid_sample_at
            if silence > SERIAL_TIMEOUT_SECONDS:
                self.handle_serial_error(
                    f"No valid data were received for {silence:.1f} seconds. "
                    "The Arduino may have been disconnected."
                )
        self.after(50, self.process_serial_events)

    def start_reader(self) -> None:
        # A fresh event per reader: an old reader that outlived close_serial()'s join must
        # stay stopped (and silent) after a reconnect starts a new one.
        self.stop_reader = threading.Event()
        self.reader_thread = threading.Thread(
            target=self.serial_reader, args=(self.stop_reader,), daemon=True
        )
        self.reader_thread.start()

    def serial_reader(self, stop: threading.Event) -> None:
        # Keep a local reference: close_serial() sets self.connection to None.
        connection = self.connection
        assert connection is not None
        i2c_logged = False  # Once per connection, so a reconnect logs the new boot too.
        while not stop.is_set():
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
                if not stop.is_set():
                    self.serial_events.put(("serial_error", str(exc)))
                return

    def show_connection_error(self, details: str) -> None:
        """Show (or refresh) the error screen; the app keeps retrying on its own."""
        note = f"Retrying automatically... (last attempt {datetime.now():%H:%M:%S})"
        label = self.error_details_label
        if label is not None and label.winfo_exists():
            label.config(text=details)  # Refresh in place, so retries don't flicker the screen.
            if self.retry_note_label is not None:
                self.retry_note_label.config(text=note)
            return
        self.clear_screen()
        self.add_open_recording_button()
        ttk.Button(self.container, text="Try again now", command=self.retry_connection).pack(side="bottom", pady=10)
        ttk.Label(self.container, text="Arduino connection error", style="Title.TLabel").pack(pady=(30, 10))
        self.retry_note_label = ttk.Label(self.container, text=note, foreground="#6a6a6a")
        self.retry_note_label.pack(pady=(0, 4))
        self.error_details_label = ttk.Label(
            self.container,
            text=details,
            foreground="#a40000",
            wraplength=640,
            justify="center",
        )
        self.error_details_label.pack(pady=8)

    def retry_connection(self) -> None:
        if self.retry_note_label is not None and self.retry_note_label.winfo_exists():
            self.retry_note_label.config(text="Retrying now...")
        self.start_connect()

    def show_setup_screen(self) -> None:
        self.clear_screen()
        # Buttons are packed first (bottom) so they can never be clipped.
        self.add_open_recording_button()
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
        ttk.Label(
            self.container, text="Keep the hand with the heart-rate sensor still.", foreground="#6a6a6a"
        ).pack(pady=(0, 6))

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

    def open_recording(self) -> None:
        """Show the graph of a saved examination in its own window. Needs no Arduino."""
        path = filedialog.askopenfilename(
            parent=self,
            title="Open recording",
            initialdir=OUTPUT_FOLDER,
            filetypes=[("Examination recordings", "*_voltage.csv")],
        )
        if not path:
            return
        voltage_path = Path(path)
        stem = voltage_path.name.removesuffix("_voltage.csv")
        window = tk.Toplevel(self)
        window.title(stem)
        window.geometry("1400x750")
        window.minsize(1200, 450)
        # Esc closes only this window, back to the main screen (the main window's Esc,
        # which quits, is bound on the main window and does not reach this one).
        window.bind("<Escape>", lambda _e: window.destroy())
        # Same layout as the results screen: the question table to the left of the graph.
        results = ttk.Frame(window, padding=12)
        results.pack(fill="both", expand=True)
        results.rowconfigure(0, weight=1)
        results.columnconfigure(0, weight=2, minsize=640, uniform="results")
        results.columnconfigure(1, weight=3, uniform="results")
        table_pane = ttk.Frame(results)
        table_pane.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        graph_pane = ttk.Frame(results)
        graph_pane.grid(row=0, column=1, sticky="nsew")
        try:
            from graph_view import GraphView  # Loads matplotlib; imported on first use only.

            view = GraphView(graph_pane, voltage_path, subject_name=None, save_default_png=False)
        except Exception as error:  # ValueError/OSError: unreadable files; anything else is a bug.
            window.destroy()
            messagebox.showerror("Open recording", f"Could not show {voltage_path.name}:\n\n{error}", parent=self)
            return
        graph_pane.pack_propagate(False)  # The pane sets the graph's size, not the figure's.
        view.pack(fill="both", expand=True)

        ttk.Label(table_pane, text="Examination summary", style="Title.TLabel").pack(pady=(4, 8))
        # Prediction is the saved detected_truth; the files keep no certainty.
        rows = [(q.number, q.text, q.answer, q.truth, q.detected, "") for q in view.recording.questions]
        if rows:
            window.bind("<MouseWheel>", build_question_table(table_pane, rows))
        else:
            ttk.Label(table_pane, text=view.recording.questions_note or "No questions found.",
                      foreground="#6a6a6a", wraplength=440).pack(expand=True)

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
        self.answer_clock_at.clear()
        self.anchor_us = None  # Re-anchored on the first sample of this recording.
        self.last_sample_time_s = 0.0
        self.paused_at = None
        self.paused_total_s = 0.0
        self.recording_started_at = time.monotonic()
        self.phase_started_at = self.operator_clock()
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
        # operator_clock() stands still during a connection loss, so the countdown freezes.
        remaining = max(0, self.rest_seconds - (self.operator_clock() - self.phase_started_at))
        self.timer_label.config(text=f"{remaining:04.1f}")
        if remaining <= 0:
            self.show_question_screen()
        else:
            self.after(50, self.update_rest_timer)

    def show_question_screen(self) -> None:
        self.clear_screen()
        self.phase_started_at = self.operator_clock()

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
            text="Start question (Space / Enter)",
            command=self.mark_question,
            takefocus=False,  # Space must only reach the window-level handler.
        )
        self.mark_button.pack(side="left", padx=6)
        ttk.Button(
            action_row, text="Undo answer (Backspace)", command=self.undo_last, takefocus=False
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
            text="Press Space or Enter (or click) when you start asking a question, and again when you "
            f"finish asking. Then press {ANSWER_HINT} when the subject answers. "
            "Backspace = undo the last answer. "
            "Wait for \"Since last answer\" to say ready before starting the next question.",
            style="Q.TLabel",
            wraplength=640,
            justify="center",
        ).pack(pady=2)
        info_row = ttk.Frame(self.container, style="Q.TFrame")
        info_row.pack(pady=2)
        self.status_label = ttk.Label(info_row, text=self.waiting_status(1), style="Q.TLabel")
        self.status_label.pack(side="left", padx=12)
        self.heart_label = ttk.Label(info_row, text="", font=("Segoe UI", 12), style="Q.TLabel")
        self.heart_label.pack(side="left", padx=12)
        self.update_heart_label()
        self.warning_label = tk.Label(self.container, text="", font=("Segoe UI", 12, "bold"), bg=self.q_bg)
        self.warning_label.pack(pady=2)
        self.mark_key_down = False
        handlers: dict[str, object] = {
            "<Return>": self.on_mark_key,
            "<KP_Enter>": self.on_mark_key,
            "<space>": self.on_mark_key,
            "<KeyRelease-Return>": self.on_mark_key_release,
            "<KeyRelease-KP_Enter>": self.on_mark_key_release,
            "<KeyRelease-space>": self.on_mark_key_release,
            "<BackSpace>": lambda _e: self.undo_last(),
        }
        for number, choice in enumerate(ANSWER_CHOICES, start=1):
            handlers[f"<Key-{number}>"] = lambda _e, c=choice: self.answer_by_key(c)
            handlers[f"<KP_{number}>"] = lambda _e, c=choice: self.answer_by_key(c)
        self.bind_keys(handlers)
        self.bind_letter_keys({"f": self.on_finish_key})
        self.update_test_timer()

    def show_question_text(self, label: ttk.Label, number: int) -> None:
        """Show the wording of question `number` on a large label, wrapped for its direction."""
        text = QUESTION_TEXTS[number - 1]
        if is_rtl(text):
            label.config(text=wrap_rtl(text, self.question_font, QUESTION_WRAP_PIXELS), wraplength=0)
        else:
            label.config(text=text, wraplength=QUESTION_WRAP_PIXELS)

    def on_mark_key(self, _event: object | None = None) -> None:
        """Space / Enter / keypad Enter: the question start mark, then the question end mark."""
        now = time.monotonic()
        repeat = self.mark_key_down and now - self.mark_key_pressed_at < KEY_REPEAT_WINDOW_SECONDS
        self.mark_key_down = True
        self.mark_key_pressed_at = now
        if not repeat:  # A held key must not set both marks.
            self.mark_question()

    def on_mark_key_release(self, _event: object | None = None) -> None:
        self.mark_key_down = False

    def on_finish_key(self, _event: object | None = None) -> None:
        # F sits under the answering hand, so confirm (default No) before finishing.
        if self.finished or not self.recording:
            return
        if messagebox.askyesno("Finish examination", "Finish the examination now?", default=messagebox.NO):
            if not self.connection_lost:  # The connection may have dropped while the dialog was open.
                self.finish_measurement()

    def answer_by_key(self, choice: str) -> None:
        question = self.pending_question
        if self.finished or self.connection_lost or question is None:
            return
        if question.end_time_s is None:  # Still asking: the answer key is ignored.
            self.status_label.config(
                text=f"Press Space when you finish asking question {question.number}, then {ANSWER_HINT}"
            )
            return
        self.answer_var.set(choice)
        self.save_answer()

    def update_test_timer(self) -> None:
        if not self.recording or self.finished or self.phase_started_at is None:
            return
        elapsed = self.operator_clock() - self.phase_started_at  # Frozen during a connection loss.
        remaining = max(0, self.test_seconds - elapsed)
        minutes, seconds = divmod(int(remaining), 60)
        self.test_timer_label.config(text=f"{minutes:02d}:{seconds:02d}")
        self.update_gap_label()
        if remaining <= 0:
            self.finish_measurement()
        else:
            self.after(100, self.update_test_timer)

    def update_gap_label(self) -> None:
        """Advisory counter (the question gap): time since the latest saved answer mark."""
        label = getattr(self, "gap_label", None)
        if label is None or not label.winfo_exists() or self.phase_started_at is None:
            return
        # Blank while an answer is pending, and before the first answer (the rest period
        # already covered the first question).
        answered_at = None
        if self.pending_question is None and self.questions:
            answered_at = self.answer_clock_at.get(self.questions[-1].number)
        if answered_at is None:
            label.config(text="")
            return
        # operator_clock() excludes connection-loss pauses, so the counter freezes during one.
        since = max(0.0, self.operator_clock() - answered_at)
        if since >= MIN_QUESTION_GAP_SECONDS:
            label.config(text=f"Since last answer: {since:.1f} s — ready", style="QGapReady.TLabel")
        else:
            label.config(text=f"Since last answer: {since:.1f} s — wait", style="QGapWait.TLabel")

    def operator_clock(self) -> float:
        """PC monotonic time minus connection-loss pauses; stands still during a pause.

        Drives the countdowns and the question gap only. The recorded data keep real time.
        """
        now = time.monotonic()
        paused = self.paused_total_s + (now - self.paused_at if self.paused_at is not None else 0.0)
        return now - paused

    def record_answer_mark(self, question: Question) -> None:
        question.answer_time_s = self.current_time_s()
        self.answer_clock_at[question.number] = self.operator_clock()

    def current_time_s(self) -> float:
        """Arduino-based time since recording start, for the question and answer marks."""
        now = time.monotonic()
        if self.anchor_us is not None:
            # Same Arduino-based clock as the samples: last sample time plus the
            # PC time elapsed since that sample arrived.
            return self.last_sample_time_s + (now - self.last_sample_received_at)
        return now - self.recording_started_at  # No sample yet: fall back to PC time.

    def waiting_status(self, number: int) -> str:
        return f"Waiting for question {number} — press Space when you start asking"

    def mark_question(self) -> None:
        """Set the question start mark, or, while asking, the question end mark."""
        if self.finished or not self.recording or self.recording_started_at is None or self.connection_lost:
            return
        now = time.monotonic()
        question = self.pending_question
        if question is None:
            if now - self.last_answer_saved_at < MARK_DEBOUNCE_SECONDS:
                return  # Key repeat right after an answer must not start the next question.
            number = len(self.questions) + 1
            self.pending_question = Question(number=number, start_time_s=self.current_time_s())
            self.last_mark_at = now
            self.set_question_stage("asking")
            self.show_question_text(self.question_label, number)
            self.status_label.config(text=f"Asking question {number}… press Space when you finish asking")
        elif question.end_time_s is None:
            if now - self.last_mark_at < MARK_DEBOUNCE_SECONDS:
                return  # A double press must not set the start and end marks together.
            question.end_time_s = self.current_time_s()
            self.last_mark_at = now
            self.set_question_stage("awaiting")
            self.status_label.config(text=f"Question {question.number} asked — press {ANSWER_HINT}")
        # Otherwise the question awaits its answer: Space does nothing.
        self.focus_set()

    def save_answer(self) -> None:
        question = self.pending_question
        if question is None or question.end_time_s is None or self.connection_lost:
            return  # Nothing awaiting an answer: a double press or stray click is ignored.
        answer = self.answer_var.get()
        if answer not in ANSWER_CHOICES:
            return
        question.answer = answer
        self.record_answer_mark(question)
        self.questions.append(question)
        self.pending_question = None  # Cleared first, so a repeated key cannot save twice.
        self.last_answer_saved_at = time.monotonic()
        if len(self.questions) >= len(QUESTION_TEXTS):
            self.finish_measurement()
            return
        next_number = len(self.questions) + 1
        self.show_question_text(self.question_label, next_number)
        self.status_label.config(
            text=f"Saved: Question {question.number} = {answer}. {self.waiting_status(next_number)}"
        )
        self.set_question_stage("waiting", saved=True)
        self.focus_set()  # Keep focus on the window, not a widget that reacts to Space.

    def set_question_state(self, state: str) -> None:
        """Colour the whole question screen: 'waiting' (neutral), 'asking' (blue), 'awaiting'
        (amber), 'saved' (green)."""
        if self.q_flash_after is not None:
            self.after_cancel(self.q_flash_after)
            self.q_flash_after = None
        bg, text = {
            "waiting": (self.q_neutral_bg, "READY – Space when you start asking"),
            "asking": ("#90caf9", "ASKING – Space when you finish asking"),
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

    def set_question_stage(self, stage: str, saved: bool = False) -> None:
        """Show a question stage: 'waiting' for the start mark, 'asking' (until the end mark) or
        'awaiting' the answer. The answer buttons are enabled only while awaiting the answer."""
        self.set_question_state("saved" if saved else stage)
        self.answer_var.set("")
        awaiting = stage == "awaiting"
        for button in self.answer_buttons:
            button.config(state="normal" if awaiting else "disabled")
        self.mark_button.config(
            state="disabled" if awaiting else "normal",
            text="Start question (Space / Enter)" if stage == "waiting" else "End question (Space / Enter)",
        )

    def undo_last(self) -> None:
        """Undo the last answer: reopen the last saved question so it can be answered again.

        Only answers can be undone; a question start or end mark can't.
        """
        if self.finished or not self.recording or self.connection_lost:
            return
        now = time.monotonic()
        if now - self.last_undo_at < MARK_DEBOUNCE_SECONDS:
            return  # A held Backspace must undo only one step.
        if self.pending_question is not None or not self.questions:
            return
        question = self.questions.pop()
        question.answer = ""
        # It will be answered again, so its answer mark is cleared; start and end marks stay.
        question.answer_time_s = None
        self.answer_clock_at.pop(question.number, None)
        self.pending_question = question
        self.last_undo_at = now
        self.set_question_stage("awaiting")
        self.show_question_text(self.question_label, question.number)
        self.status_label.config(text=f"Question {question.number} reopened — press {ANSWER_HINT}")
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
                # First sample of the recording, or after a reconnect (the Arduino reset
                # restarted micros()): anchor at the real elapsed time, so an outage shows as
                # a gap and time is never compressed. max() keeps time from going backwards.
                self.anchor_us = unwrapped_us
                self.anchor_offset_s = max(pc_time, self.last_sample_time_s)
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
        if state == "LOCKED" and bpm > 0:
            self.last_locked_bpm = (bpm, received_at)
        elif state == "NO_SENSOR":
            self.last_locked_bpm = None  # A sensor fault must not be followed by a held value.
        self.update_heart_label()
        if self.recording and self.recording_started_at is not None:
            pc_time = received_at - self.recording_started_at
            if self.anchor_us is not None:
                time_s = (unwrapped_us - self.anchor_us) / 1e6 + self.anchor_offset_s
            else:
                time_s = pc_time  # No GSR sample yet: fall back to PC time.
            self.bpm_readings.append(BpmReading(time_s, pc_time, arduino_us, bpm, state))

    def update_heart_label(self) -> None:
        label = self.heart_label
        if label is not None and label.winfo_exists():
            text, held = heart_rate_display(self.latest_bpm, self.last_locked_bpm, time.monotonic())
            # An empty foreground falls back to the label's style colour.
            label.config(text=text, foreground=HELD_BPM_COLOR if held else "")

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
        if self.finished or self.connection_lost:
            return
        self.close_serial()
        if self.recording:
            self.pause_for_connection_loss(details)
        elif self.recording_started_at is None:  # Setup screen: look for the Arduino again.
            self.show_connection_error(f"The Arduino connection was lost.\n\n{details}")
            self.start_connect()
        # Otherwise (truth classification) all data are in; the connection is no longer needed.

    def pause_for_connection_loss(self, details: str) -> None:
        """Pause the examination (countdowns, keys, question gap) and reconnect in the background."""
        self.connection_lost = True
        self.paused_at = time.monotonic()
        self.show_loss_bubble(details)
        self.start_connect()

    def resume_after_reconnect(self) -> None:
        """Called once the reader runs on the new connection: re-anchor time and resume."""
        # Opening the port reset the Arduino, so micros() restarted: unwrap from scratch and
        # let the next sample re-anchor at the real elapsed recording time (handle_sample).
        self.unwrapper = MicrosUnwrapper()
        self.anchor_us = None
        self.zero_started_at = None
        if self.paused_at is not None:
            self.paused_total_s += time.monotonic() - self.paused_at
            self.paused_at = None
        self.connection_lost = False
        self.close_loss_bubble()
        self.focus_set()

    def show_loss_bubble(self, details: str) -> None:
        """A small window on top of the app; its grab blocks clicks on the paused screen."""
        self.close_loss_bubble()
        bubble = tk.Toplevel(self)
        bubble.title("Connection lost")
        bubble.transient(self)
        bubble.resizable(False, False)
        bubble.protocol("WM_DELETE_WINDOW", lambda: None)  # Wait for the cable or End examination.
        frame = ttk.Frame(bubble, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Connection lost", style="Title.TLabel", foreground="#a40000").pack(pady=(0, 8))
        ttk.Label(
            frame,
            text="Reconnect the Arduino cable. Reconnecting...\nThe examination is paused.",
            font=("Segoe UI", 12),
            justify="center",
        ).pack(pady=(0, 8))
        self.loss_details_label = ttk.Label(
            frame, text=details, foreground="#6a6a6a", wraplength=460, justify="center"
        )
        self.loss_details_label.pack(pady=(0, 12))
        ttk.Button(frame, text="End examination", command=self.end_examination_from_bubble).pack()
        self.loss_window = bubble

        # Centre over the main window.
        bubble.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - bubble.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - bubble.winfo_reqheight()) // 3
        bubble.geometry(f"+{max(0, x)}+{max(0, y)}")
        try:
            bubble.grab_set()
        except tk.TclError:
            pass  # Not viewable yet; the key guards still pause the examination.
        bubble.focus_force()

    def update_loss_details(self, details: str) -> None:
        label = self.loss_details_label
        if label is not None and label.winfo_exists():
            label.config(text=f"{details}\n\nLast attempt {datetime.now():%H:%M:%S}")

    def close_loss_bubble(self) -> None:
        if self.loss_window is not None:
            try:
                self.loss_window.grab_release()
                self.loss_window.destroy()
            except tk.TclError:
                pass
        self.loss_window = None
        self.loss_details_label = None

    def end_examination_from_bubble(self) -> None:
        """Stop reconnecting and finish as if the operator had finished early."""
        self.close_loss_bubble()
        # connection_lost stays set while finish_measurement may ask questions, so keys
        # stay ignored and a reconnect in the meantime is still handled.
        self.finish_measurement()
        if self.recording_started_at is None:
            return  # Nothing was answered: back on the setup screen, which reconnects by itself.
        if self.recording:  # The operator cancelled finishing.
            if self.connection_lost:
                self.show_loss_bubble("Still reconnecting...")
            return  # Else the Arduino came back during the dialog and the examination resumed.
        self.connection_lost = False
        self.paused_at = None
        self.cancel_retry()
        self.close_serial()  # In case a reconnect arrived during the dialog.

    def finish_measurement(self) -> None:
        if self.finished:
            return
        if self.pending_question is not None:
            if not messagebox.askyesno(
                "Unfinished question",
                "The current question has no saved answer. Finish without saving this question?",
            ):
                return
            self.pending_question = None

        self.recording = False
        if not self.questions:
            # Before the setup screen exists, so the Enter that closes this can't start an examination.
            messagebox.showinfo("Nothing saved", "No questions were answered, so nothing was saved.")
            self.discard_examination()
            return

        self.show_truth_screen()

    def discard_examination(self) -> None:
        """Drop the examination without saving and go back to the setup screen."""
        self.recording = False
        self.recording_started_at = None
        self.phase_started_at = None
        self.pending_question = None
        self.samples.clear()
        self.questions.clear()
        self.bpm_readings.clear()
        self.answer_clock_at.clear()
        self.zero_started_at = None
        self.set_zero_warning(False)
        self.close_loss_bubble()
        self.connection_lost = False
        self.paused_at = None
        if self.connection is not None:
            self.last_valid_sample_at = time.monotonic()
            self.show_setup_screen()
        else:  # Ended from the "Connection lost" window: look for the Arduino again.
            self.show_connecting_screen()
            self.start_connect()

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
        self.save_files()
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
        results.columnconfigure(0, weight=2, minsize=640, uniform="results")
        results.columnconfigure(1, weight=3, uniform="results")
        table_pane = ttk.Frame(results)
        table_pane.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        graph_pane = ttk.Frame(results)
        graph_pane.grid(row=0, column=1, sticky="nsew")
        self.build_summary_table(table_pane)
        self.after(10, self.build_graph, graph_pane)  # Later, so the table appears without waiting.

    def build_graph(self, parent: ttk.Frame) -> None:
        try:
            from graph_view import GraphView  # Loads matplotlib; imported on first use only.

            assert self.last_saved_paths is not None
            view = GraphView(parent, self.last_saved_paths[0], subject_name=self.subject_name, save_default_png=True)
        except Exception as error:  # The table and saved files must survive a graph failure.
            ttk.Label(
                parent, text=f"The graph could not be drawn:\n{error}", foreground="#a40000", wraplength=500
            ).pack(expand=True)
            return
        parent.pack_propagate(False)  # The pane sets the graph's size, not the figure's.
        view.pack(fill="both", expand=True)

    def build_summary_table(self, parent: ttk.Frame) -> None:
        # Bottom items are packed first so they can never be clipped.
        buttons = ttk.Frame(parent)
        buttons.pack(side="bottom", pady=(8, 4))
        ttk.Button(buttons, text="Home (H)", command=self.go_home).pack(side="left", padx=6)
        ttk.Button(buttons, text="Close (Esc)", command=self.on_close).pack(side="left", padx=6)
        ttk.Label(
            parent,
            text=f"Files saved in: {OUTPUT_FOLDER}\n{self.questions_note()}\n{self.recording_stats()}",
            foreground="#6a6a6a",
            wraplength=540,
            justify="center",
        ).pack(side="bottom", pady=(4, 0))
        # PLACEHOLDER: remove this note once the prediction columns are real.
        ttk.Label(parent, text="Prediction and certainty are random placeholders.", foreground="#6a6a6a").pack(
            side="bottom"
        )
        ttk.Label(parent, text="Examination summary", style="Title.TLabel").pack(pady=(4, 8))

        rows = []
        for q in self.questions:
            # PLACEHOLDER: replace with the classifier's output once it exists.
            prediction = random.choice(("Truth", "Lie"))
            certainty = f"{random.randint(50, 99)}%"
            rows.append((q.number, QUESTION_TEXTS[q.number - 1], q.answer, q.truth, prediction, certainty))
        on_mouse_wheel = build_question_table(parent, rows)
        self.bind_keys({"<MouseWheel>": on_mouse_wheel})
        self.bind_letter_keys({"h": lambda _e: self.go_home()})

    def go_home(self) -> None:
        """Results screen: back to the setup screen for a new examination. The examination
        is already saved and the port closed (finish_truth), so this reconnects first."""
        if not self.finished or self.recording_started_at is None:
            return  # Not on the results screen (or already left it).
        self.finished = False
        self.last_saved_paths = None
        self.questions_saved_to = None
        self.truth_index = 0
        # Sensor readings from the old connection must not show as live on the setup screen.
        self.latest_bpm = None
        self.last_locked_bpm = None
        self.last_gsr_raw = None
        self.last_gsr_in_range_at = None
        # Opening the port resets the Arduino, so micros() restarts.
        self.unwrapper = MicrosUnwrapper()
        self.anchor_us = None
        try:
            self.state("normal")  # Undo the results screen's maximized window.
        except tk.TclError:
            pass
        self.minsize(700, 480)
        self.geometry("800x560")
        self.discard_examination()  # Clears the examination data; with no connection it reconnects.

    def recording_stats(self) -> str:
        return (
            f"Valid samples: {len(self.samples):,}; heart-rate readings: {len(self.bpm_readings):,}; "
            f"malformed serial lines ignored: {self.bad_line_count}"
        )

    def questions_note(self) -> str:
        saved_to = self.questions_saved_to
        if saved_to is None or saved_to.name == SHARED_QUESTIONS_FILENAME:
            return f"Questions added to {SHARED_QUESTIONS_FILENAME}"
        return f"Questions saved to {saved_to.name} ({SHARED_QUESTIONS_FILENAME} could not be written)"

    def save_files(self) -> None:
        """Save a finished examination: its voltage and heart-rate files, and its questions
        appended to the shared questions file (or a fallback file if that can't be written)."""
        self.last_saved_paths = save_examination(OUTPUT_FOLDER, self.file_stem, self.samples, self.bpm_readings)
        rows = question_rows(self.file_stem, self.questions, QUESTION_TEXTS)
        while True:
            try:
                self.questions_saved_to = append_shared_questions(OUTPUT_FOLDER, rows)
                return
            except QuestionsFileMismatch as error:
                message = (
                    f"{error} The questions can't be added to it.\n\n"
                    "Rename or move it (a new one will be created) and press Retry."
                )
            except OSError as error:
                print(f"Could not write {SHARED_QUESTIONS_FILENAME}: {error}")
                message = (
                    f"{SHARED_QUESTIONS_FILENAME} is open in another program (e.g. Excel). "
                    "Close it and press Retry."
                )
            if not messagebox.askretrycancel(
                "Questions not saved",
                f"{message}\n\nCancel saves this examination's questions to a separate file instead.",
                icon=messagebox.WARNING,
            ):
                break
        self.questions_saved_to = write_fallback_questions(OUTPUT_FOLDER, self.file_stem, rows)
        messagebox.showinfo(
            "Questions saved separately",
            f"This examination's questions were saved to\n{self.questions_saved_to}\n\n"
            f"They were not added to {SHARED_QUESTIONS_FILENAME}.",
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
        # Unsaved examination: rest, questions or truth classification (saved only when finished).
        if self.recording_started_at is not None and not self.finished:
            if not messagebox.askyesno(
                "Close examination",
                "The examination will be discarded. Quit anyway?",
                icon=messagebox.WARNING,
                default=messagebox.NO,
            ):
                return
            self.recording = False
        self.finished = True
        self.cancel_retry()
        self.close_serial()
        self.destroy()


if __name__ == "__main__":
    app = PolygraphApp()
    app.mainloop()
