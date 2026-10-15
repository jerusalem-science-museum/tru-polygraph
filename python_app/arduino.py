"""Arduino serial link: line protocol, micros() unwrapping and port detection.

Expected Arduino serial format (ASCII, 115200 baud, one record per line):
    <t_us>,<raw_adc>             GSR sample, 500 Hz (t_us = raw 32-bit micros())
    B,<t_us>,<bpm>,<state>       heart rate, 1 Hz (bpm 0 = not locked; state is one of
                                 NO_FINGER, SETTLING, ACQUIRING, LOCKED, NO_SENSOR)
    # <text>                     info/comment, ignored

t_us wraps every ~71.6 minutes, so it is unwrapped on the PC (MicrosUnwrapper).
"""

from __future__ import annotations

import time

import serial
from serial import SerialException
from serial.tools import list_ports

BAUD_RATE = 115200
ADC_MAX = 1023
SAMPLE_RATE_HZ = 500.0  # Nominal GSR sample rate of the sketch.
BPM_STATES = ("NO_FINGER", "SETTLING", "ACQUIRING", "LOCKED", "NO_SENSOR")
BPM_MAX = 300
MICROS_WRAP = 1 << 32


class MicrosUnwrapper:
    """Turn wrapping 32-bit micros() values into a monotonic 64-bit count.

    One instance must be shared by GSR and B lines, because both come from the same
    Arduino clock. Small backwards steps (reordering) are tolerated, not treated as a wrap.
    """

    def __init__(self) -> None:
        self.last_raw: int | None = None
        self.unwrapped = 0

    def unwrap(self, raw_us: int) -> int:
        if self.last_raw is None:
            self.unwrapped = raw_us
        else:
            delta = (raw_us - self.last_raw) % MICROS_WRAP
            if delta >= MICROS_WRAP // 2:
                delta -= MICROS_WRAP  # Slightly older than the previous value.
            self.unwrapped += delta
        self.last_raw = raw_us
        return self.unwrapped


def parse_line(line: str) -> tuple:
    """Parse one serial line.

    Returns ("gsr", t_us, raw_adc), ("bpm", t_us, bpm, state) or ("comment", text).
    Raises ValueError for anything else.
    """
    text = line.strip()
    if text.startswith("#"):
        return ("comment", text[1:].strip())

    fields = text.split(",")
    if fields[0] == "B":
        if len(fields) != 4:
            raise ValueError("Expected B,t_us,bpm,state")
        t_us = int(fields[1])
        bpm = int(fields[2])
        state = fields[3].strip()
        if not 0 <= t_us < MICROS_WRAP or not 0 <= bpm <= BPM_MAX or state not in BPM_STATES:
            raise ValueError("Heart-rate values are outside the expected range")
        return ("bpm", t_us, bpm, state)

    if len(fields) != 2:
        raise ValueError("Expected two comma-separated values")
    t_us = int(fields[0])
    raw_adc = int(fields[1])
    if not 0 <= t_us < MICROS_WRAP or not 0 <= raw_adc <= ADC_MAX:
        raise ValueError("Serial values are outside the expected range")
    return ("gsr", t_us, raw_adc)


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
                    parsed = parse_line(data.decode("ascii", errors="strict"))
                    # B and # lines are valid but must not count or reset the counter.
                    if parsed[0] == "gsr":
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
        "Could not find an Arduino sending 'time_us,raw_adc' GSR lines "
        "(plus optional 'B,time_us,bpm,state' heart-rate lines) at 115200 baud.\n\n"
        + details
    )
