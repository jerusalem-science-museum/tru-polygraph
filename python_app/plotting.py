"""Plot a saved examination: skin conductance and heart rate with the question marks drawn.

A recording is read once with `load_recording()`; `plot_examination()` builds the figure and
`set_marks()` redraws only the question marks, so switching mark kinds on and off is quick.

Three mark kinds can be drawn, on both panels: the question start mark (dotted), the question
end mark (dashed) and the answer mark (solid). Each question's lines are coloured by its truth
classification: green Truth, red Lie, grey otherwise. Stretches where the heart-rate sensor did
not answer (state NO_SENSOR) are shaded in the heart-rate panel.
"""

from __future__ import annotations

import csv
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from bidi import get_display

from records import QUESTIONS_COLUMNS, SHARED_QUESTIONS_FILENAME

# Matplotlib warns if a redraw happens while the pane is very narrow, e.g. mid-resize. Harmless.
warnings.filterwarnings("ignore", message="Tight layout not applied")

MARK_KINDS = ("start", "end", "answer")
DEFAULT_MARKS = frozenset({"end"})

MARK_LABELS = {"start": "Question start", "end": "Question end", "answer": "Answer"}
MARK_STYLES = {"start": ":", "end": "--", "answer": "-"}
TRUTH_COLORS = {"truth": "#218739", "lie": "#c62828"}
UNCLASSIFIED_COLOR = "#6a6a6a"
LEGEND_LINE_COLOR = "#444444"
MARK_GID = "question-mark"  # Tags every artist set_marks() draws, so it can remove them again.

FIGURE_SIZE = (13, 8)  # Inches; the saved PNG always has this size, whatever the window shows.
PNG_DPI = 150

_STEM_TIMESTAMP = re.compile(r"_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")
_VOLTAGE_SUFFIX = "_voltage"


@dataclass
class QuestionMarks:
    number: int
    truth: str  # answer_truth: Truth / Lie / empty.
    start_s: float | None  # None when the mark was not recorded.
    end_s: float | None
    answer_s: float | None
    text: str = ""  # question_text: the wording at examination time.
    answer: str = ""  # subject_answer: Yes / No / Undefined.
    detected: str = ""  # detected_truth: empty until there is a detection algorithm.

    def time_of(self, kind: str) -> float | None:
        return {"start": self.start_s, "end": self.end_s, "answer": self.answer_s}[kind]


@dataclass
class Recording:
    stem: str  # <subject>_<YYYY-MM-DD>_<HH-MM-SS>
    folder: Path
    times: list[float]  # time_s of each GSR sample.
    conductance: list[float]  # conductance_us of each GSR sample.
    bpm_times: list[float] = field(default_factory=list)
    bpm_values: list[float] = field(default_factory=list)  # NaN where bpm == 0 (not locked).
    bpm_states: list[str] = field(default_factory=list)
    questions: list[QuestionMarks] = field(default_factory=list)
    questions_source: Path | None = None  # File the questions came from; None if none found.
    questions_note: str = ""  # Operator-readable remark about the questions, empty if all is well.


def display_name(stem: str) -> str:
    """The subject part of a stem: strip the trailing _YYYY-MM-DD_HH-MM-SS."""
    return _STEM_TIMESTAMP.sub("", stem) or stem


def _optional_time(text: str | None) -> float | None:
    """A mark time from the CSV; empty, unparseable or 0 means the mark was not recorded."""
    try:
        value = float((text or "").strip())
    except ValueError:
        return None
    return value if value != 0.0 else None


def _read_questions(path: Path, stem: str) -> list[QuestionMarks]:
    """The rows of one questions file whose file_name is stem. ValueError if the header is wrong."""
    by_number: dict[int, QuestionMarks] = {}
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        missing = [column for column in QUESTIONS_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path.name} is missing the columns: {', '.join(missing)}")
        for row in reader:  # Streamed: the shared file grows with every examination.
            if (row.get("file_name") or "").strip() != stem:
                continue
            try:
                number = int((row.get("question_number") or "").strip())
            except ValueError:
                print(f"{path.name}: skipping a row of {stem} without a valid question_number")
                continue
            # A question saved twice (e.g. the file was appended again) keeps its last row.
            by_number[number] = QuestionMarks(
                number=number,
                truth=(row.get("answer_truth") or "").strip(),
                start_s=_optional_time(row.get("question_start_time_s")),
                end_s=_optional_time(row.get("question_end_time_s")),
                answer_s=_optional_time(row.get("answer_time_s")),
                text=(row.get("question_text") or "").strip(),
                answer=(row.get("subject_answer") or "").strip(),
                detected=(row.get("detected_truth") or "").strip(),
            )
    return [by_number[number] for number in sorted(by_number)]


def _load_questions(folder: Path, stem: str) -> tuple[list[QuestionMarks], Path | None, str]:
    """Questions from the shared questions.csv, else from the fallback <stem>_questions.csv."""
    notes: list[str] = []
    for path in (folder / SHARED_QUESTIONS_FILENAME, folder / f"{stem}_questions.csv"):
        if not path.exists():
            continue
        try:
            questions = _read_questions(path, stem)
        except (OSError, ValueError, csv.Error) as error:
            notes.append(f"could not read {path.name}: {error}")
            continue
        if questions:
            return questions, path, "; ".join(notes)
    notes.append("no questions found for this recording")
    return [], None, "; ".join(notes)


def load_recording(voltage_csv: str | Path) -> Recording:
    """Read <stem>_voltage.csv, <stem>_bpm.csv (optional) and the recording's questions.

    Raises ValueError with an operator-readable message if the voltage file is not a valid
    recording; OSError (e.g. file not found) passes through. Missing heart-rate data or
    questions do not fail: the graph is drawn without them and questions_note says why.
    """
    voltage_csv = Path(voltage_csv)
    if not voltage_csv.stem.endswith(_VOLTAGE_SUFFIX):
        raise ValueError(f"{voltage_csv.name} is not a voltage file (expected <name>_voltage.csv)")
    stem = voltage_csv.stem[: -len(_VOLTAGE_SUFFIX)]
    folder = voltage_csv.parent

    times: list[float] = []
    conductance: list[float] = []
    with voltage_csv.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        required = ["time_s", "conductance_us"]
        missing = [column for column in required if column not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{voltage_csv.name} is missing the columns: {', '.join(missing)}")
        for row in reader:
            try:
                times.append(float(row["time_s"]))
                conductance.append(float(row["conductance_us"]))
            except (TypeError, ValueError):
                raise ValueError(
                    f"{voltage_csv.name} has an invalid value on line {reader.line_num}"
                ) from None
    if not times:
        raise ValueError(f"{voltage_csv.name} contains no samples")

    # Heart-rate readings are optional; bpm == 0 (not locked) becomes a gap.
    bpm_times: list[float] = []
    bpm_values: list[float] = []
    bpm_states: list[str] = []
    bpm_csv = folder / f"{stem}_bpm.csv"
    if bpm_csv.exists():
        with bpm_csv.open("r", newline="", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)
            if {"time_s", "bpm"}.issubset(reader.fieldnames or []):
                for row in reader:
                    try:
                        reading_time = float(row["time_s"])
                        bpm = float(row["bpm"])
                    except (TypeError, ValueError):
                        continue
                    bpm_times.append(reading_time)
                    bpm_values.append(bpm if bpm > 0 else float("nan"))
                    bpm_states.append((row.get("state") or "").strip())

    questions, source, note = _load_questions(folder, stem)
    return Recording(
        stem=stem,
        folder=folder,
        times=times,
        conductance=conductance,
        bpm_times=bpm_times,
        bpm_values=bpm_values,
        bpm_states=bpm_states,
        questions=questions,
        questions_source=source,
        questions_note=note,
    )


def _truth_color(truth: str) -> str:
    return TRUTH_COLORS.get(truth.casefold(), UNCLASSIFIED_COLOR)


def _outages(recording: Recording) -> list[tuple[float, float]]:
    """Each run of NO_SENSOR readings, from its first reading to the next reading in another
    state (or the last reading). Connection losses are just missing rows."""
    outages: list[tuple[float, float]] = []
    outage_start: float | None = None
    for reading_time, state in zip(recording.bpm_times, recording.bpm_states):
        if state == "NO_SENSOR":
            if outage_start is None:
                outage_start = reading_time
        elif outage_start is not None:
            outages.append((outage_start, reading_time))
            outage_start = None
    if outage_start is not None:
        outages.append((outage_start, recording.bpm_times[-1]))
    return outages


def plot_examination(recording: Recording, marks=DEFAULT_MARKS, subject_name: str | None = None):
    """Build the conductance and heart-rate figure with the given mark kinds drawn.

    marks is any collection of MARK_KINDS. Returns a matplotlib Figure (not tied to pyplot);
    use set_marks() to change the marks later without rebuilding it.
    """
    from matplotlib.figure import Figure

    # layout="tight" re-applies on every draw, so the graph stays tidy when the window resizes.
    figure = Figure(figsize=FIGURE_SIZE, layout="tight")
    axis, bpm_axis = figure.subplots(2, 1, sharex=True, gridspec_kw={"height_ratios": [3, 2]})

    axis.plot(
        recording.times,
        recording.conductance,
        color="#195a8a",
        linewidth=1.0,
        label="Skin conductance (20 ms mains-cancelling average + 5 Hz low-pass)",
    )

    for index, (start, end) in enumerate(_outages(recording)):
        bpm_axis.axvspan(
            start,
            end,
            color="#9e9e9e",
            alpha=0.18,
            linewidth=0,
            label="sensor disconnected" if index == 0 else None,
        )

    if any(value == value for value in recording.bpm_values):  # any non-NaN value
        bpm_axis.plot(
            recording.bpm_times,
            recording.bpm_values,
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

    title = "Skin conductance and heart rate"
    name = subject_name if subject_name is not None else display_name(recording.stem)
    if name:
        # Matplotlib draws characters in logical order, so a Hebrew name would come out
        # reversed; reorder it for display (Latin letters and digits inside it stay readable).
        title += f" — {get_display(name)}"
    if not recording.questions:
        title += " (no questions found)"
    figure.suptitle(title)
    axis.set_ylabel("Skin conductance (µS)")
    axis.grid(True, alpha=0.25)
    axis.margins(x=0.01, y=0.12)
    bpm_axis.set_xlabel("Time (s)")
    bpm_axis.set_ylabel("Heart rate (BPM)")
    bpm_axis.grid(True, alpha=0.25)
    bpm_axis.margins(y=0.15)
    if bpm_axis.get_legend_handles_labels()[0]:
        bpm_axis.legend(loc="upper right", fontsize=8)

    set_marks(figure, recording, marks)
    return figure


def set_marks(figure, recording: Recording, marks) -> None:
    """Replace the question marks drawn on a figure from plot_examination() with the given kinds.

    Only the mark lines, their labels and the top legend change; call canvas.draw_idle()
    afterwards when the figure is on screen.
    """
    from matplotlib.lines import Line2D

    axis, bpm_axis = figure.axes[:2]
    for ax in (axis, bpm_axis):
        for artist in [*ax.lines, *ax.texts]:
            if artist.get_gid() == MARK_GID:
                artist.remove()

    shown = [kind for kind in MARK_KINDS if kind in marks]
    for question in recording.questions:
        color = _truth_color(question.truth)
        label_time: float | None = None
        for kind in shown:  # In MARK_KINDS order, so the label ends on the latest kind shown.
            mark_time = question.time_of(kind)
            if mark_time is None:
                continue
            label_time = mark_time
            linewidth = 1.5 if kind == "start" else 1.2  # Dotted lines need a little more weight.
            for ax in (axis, bpm_axis):
                ax.axvline(
                    x=mark_time,
                    color=color,
                    linestyle=MARK_STYLES[kind],
                    linewidth=linewidth,
                    alpha=0.9,
                    gid=MARK_GID,
                )
        if label_time is not None:
            label = f"Question {question.number}"
            if question.truth:
                label += f" — {question.truth}"
            axis.text(
                label_time,
                0.97,
                label,
                transform=axis.get_xaxis_transform(),  # x in seconds, y as a fraction of the panel.
                color=color,
                rotation=90,
                ha="right",
                va="top",
                fontsize=9,
                backgroundcolor="white",
                gid=MARK_GID,
            )

    # Legend: the conductance trace plus one grey sample line per mark kind shown.
    handles = [line for line in axis.lines if line.get_gid() != MARK_GID][:1]
    for kind in shown:
        handles.append(
            Line2D(
                [],
                [],
                color=LEGEND_LINE_COLOR,
                linestyle=MARK_STYLES[kind],
                linewidth=1.5 if kind == "start" else 1.2,
                label=f"{MARK_LABELS[kind]} mark",
            )
        )
    # Above the panel, not inside it, so it never hides a mark label. The legend is kept out of
    # the tight layout: when it was wider than the panel, the layout narrowed the panel to make
    # room, the centred legend then stuck out further, and every redraw (each mark toggle)
    # narrowed the panel again until the graph was a sliver. The blank title reserves the
    # legend's strip above the panel instead.
    legend = axis.legend(
        handles=handles,
        loc="lower center",
        bbox_to_anchor=(0.5, 1.0),
        ncol=len(handles),
        fontsize=8,
        frameon=False,
    )
    legend.set_in_layout(False)
    axis.set_title(" ", fontsize=8, pad=10)


def marks_suffix(marks) -> str:
    """start-end-answer style name of the shown kinds, in MARK_KINDS order; no-marks if none."""
    shown = [kind for kind in MARK_KINDS if kind in marks]
    return "-".join(shown) if shown else "no-marks"


def png_path(recording: Recording, marks, default: bool) -> Path:
    """<stem>_plot.png for the automatic default image, else <stem>_plot_<kinds>.png."""
    if default:
        return recording.folder / f"{recording.stem}_plot.png"
    return recording.folder / f"{recording.stem}_plot_{marks_suffix(marks)}.png"


def save_png(recording: Recording, marks, path: str | Path, subject_name: str | None = None) -> Path:
    """Render a fresh FIGURE_SIZE figure with the given marks and save it to path.

    A separate figure keeps the PNG the same size whatever the window size, and leaves the
    on-screen figure untouched. Errors (OSError, e.g. the file is open elsewhere) propagate.
    """
    path = Path(path)
    figure = plot_examination(recording, marks, subject_name)
    figure.savefig(path, dpi=PNG_DPI, bbox_inches="tight")
    return path
