"""Settings loaded from config.toml, plus the folders that depend on how the app is run.

A source run reads python_app/config.toml. A packaged exe reads config.toml next to the exe,
so settings can be edited without rebuilding; the first launch writes it from the bundled copy.
"""

from __future__ import annotations

import shutil
import sys
import tkinter as tk
import tomllib
from pathlib import Path
from tkinter import messagebox

# Frozen (PyInstaller) builds use the exe's folder, not the temp extraction folder.
if getattr(sys, "frozen", False):
    APP_FOLDER = Path(sys.executable).resolve().parent
else:
    APP_FOLDER = Path(__file__).resolve().parent
CONFIG_PATH = APP_FOLDER / "config.toml"
OUTPUT_FOLDER = APP_FOLDER / "measurements"

_NUMBER = (int, float)
_REQUIRED_TYPES = {
    "questions": list,
    "answer_choices": list,
    "default_rest_seconds": int,
    "default_test_seconds": int,
    "min_question_gap_seconds": _NUMBER,
    "adc_reference_volts": _NUMBER,
    "gsr_short_min": int,
    "gsr_reading_stale_seconds": _NUMBER,
    "bpm_stale_seconds": _NUMBER,
    "bpm_display_hold_seconds": _NUMBER,
    "serial_timeout_seconds": _NUMBER,
    "reconnect_interval_seconds": _NUMBER,
    "zero_warning_after_seconds": _NUMBER,
    "mark_debounce_seconds": _NUMBER,
    "screen_change_lockout_seconds": _NUMBER,
    "truth_flash_color": str,
    "lie_flash_color": str,
    "flash_ms": int,
}


def _load() -> dict:
    if not CONFIG_PATH.exists() and getattr(sys, "frozen", False):
        shutil.copyfile(Path(sys._MEIPASS) / "config.toml", CONFIG_PATH)  # type: ignore[attr-defined]
    # utf-8-sig: Windows editors may save a byte-order mark, which TOML rejects.
    settings = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))

    for key, expected in _REQUIRED_TYPES.items():
        if key not in settings:
            raise ValueError(f'The setting "{key}" is missing.')
        if not isinstance(settings[key], expected) or isinstance(settings[key], bool):
            raise ValueError(f'The setting "{key}" has the wrong type of value.')
    for key, maximum in (("questions", None), ("answer_choices", 9)):
        items = settings[key]
        if not items or not all(isinstance(item, str) and item.strip() for item in items):
            raise ValueError(f'"{key}" must be a non-empty list of non-empty text items.')
        if maximum is not None and len(items) > maximum:
            raise ValueError(f'"{key}" can have at most {maximum} items.')
    if settings["bpm_display_hold_seconds"] < 0:
        raise ValueError('"bpm_display_hold_seconds" must not be negative.')
    if settings["reconnect_interval_seconds"] <= 0:  # 0 would retry in a busy loop.
        raise ValueError('"reconnect_interval_seconds" must be greater than 0.')
    return settings


try:
    _settings = _load()
except (OSError, ValueError) as error:  # TOML syntax and encoding errors are ValueErrors.
    _root = tk.Tk()
    _root.withdraw()
    messagebox.showerror("Configuration error", f"Could not load {CONFIG_PATH}\n\n{error}")
    sys.exit(1)

QUESTION_TEXTS: tuple[str, ...] = tuple(_settings["questions"])
ANSWER_CHOICES: tuple[str, ...] = tuple(_settings["answer_choices"])
DEFAULT_REST_SECONDS: int = _settings["default_rest_seconds"]
DEFAULT_TEST_SECONDS: int = _settings["default_test_seconds"]
MIN_QUESTION_GAP_SECONDS: float = _settings["min_question_gap_seconds"]
ADC_REFERENCE_VOLTS: float = _settings["adc_reference_volts"]
GSR_SHORT_MIN: int = _settings["gsr_short_min"]
GSR_READING_STALE_SECONDS: float = _settings["gsr_reading_stale_seconds"]
BPM_STALE_SECONDS: float = _settings["bpm_stale_seconds"]
BPM_DISPLAY_HOLD_SECONDS: float = _settings["bpm_display_hold_seconds"]
SERIAL_TIMEOUT_SECONDS: float = _settings["serial_timeout_seconds"]
RECONNECT_INTERVAL_SECONDS: float = _settings["reconnect_interval_seconds"]
ZERO_WARNING_AFTER_SECONDS: float = _settings["zero_warning_after_seconds"]
MARK_DEBOUNCE_SECONDS: float = _settings["mark_debounce_seconds"]
SCREEN_CHANGE_LOCKOUT_SECONDS: float = _settings["screen_change_lockout_seconds"]
TRUTH_FLASH_COLOR: str = _settings["truth_flash_color"]
LIE_FLASH_COLOR: str = _settings["lie_flash_color"]
FLASH_MS: int = _settings["flash_ms"]
