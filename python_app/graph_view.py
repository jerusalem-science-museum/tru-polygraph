"""The examination graph as a Tk widget: mark-kind switches, Save PNG, and the matplotlib canvas."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk

import plotting
from plotting import DEFAULT_MARKS, MARK_KINDS

CHECKBOX_LABELS = {"start": "Question start", "end": "Question end", "answer": "Answer"}
ERROR_COLOR = "#a40000"
STATUS_COLOR = "#6a6a6a"


class GraphView(ttk.Frame):
    """One saved examination's graph. The caller packs or grids the frame.

    Loading errors (ValueError for an invalid recording, OSError) propagate from __init__ so
    the caller can show them. With save_default_png=True the automatic <stem>_plot.png (with
    DEFAULT_MARKS) is written right after building; a failure there is only reported.
    """

    def __init__(
        self,
        parent,
        voltage_csv: Path,
        subject_name: str | None = None,
        save_default_png: bool = False,
    ):
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

        # Load before creating any widget, so a bad file leaves nothing behind in the parent.
        self.recording = plotting.load_recording(voltage_csv)
        super().__init__(parent)
        self.subject_name = subject_name

        controls = ttk.Frame(self)
        controls.pack(side="top", fill="x", pady=(0, 4))
        ttk.Label(controls, text="Show:").pack(side="left", padx=(0, 6))
        self.mark_vars: dict[str, tk.BooleanVar] = {}
        for kind in MARK_KINDS:
            var = tk.BooleanVar(self, value=kind in DEFAULT_MARKS)
            self.mark_vars[kind] = var
            ttk.Checkbutton(controls, text=CHECKBOX_LABELS[kind], variable=var, command=self.redraw_marks).pack(
                side="left", padx=(0, 10)
            )
        ttk.Button(controls, text="Save PNG", command=self.save_png).pack(side="left", padx=(6, 10))
        self.status = ttk.Label(controls, text="", foreground=STATUS_COLOR, anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        if self.recording.questions_note:
            self.set_status(f"Note: {self.recording.questions_note}.")

        self.figure = plotting.plot_examination(self.recording, self.shown_marks(), subject_name)
        self.canvas = FigureCanvasTkAgg(self.figure, master=self)
        toolbar = NavigationToolbar2Tk(self.canvas, self, pack_toolbar=False)
        toolbar.update()
        toolbar.pack(side="bottom", fill="x")
        widget = self.canvas.get_tk_widget()
        # Request a small size so the surrounding pane, not the figure's 13x8 inches, sets the size.
        widget.configure(width=400, height=300)
        widget.pack(side="top", fill="both", expand=True)
        self.canvas.draw()

        if save_default_png:
            path = plotting.png_path(self.recording, DEFAULT_MARKS, default=True)
            try:
                plotting.save_png(self.recording, DEFAULT_MARKS, path, subject_name)
            except Exception as error:  # The graph on screen must survive a failed save.
                print(f"Could not save the graph image {path}: {error}")
                self.set_status(f"Could not save {path.name}: {error}", error=True)

    def shown_marks(self) -> frozenset[str]:
        return frozenset(kind for kind, var in self.mark_vars.items() if var.get())

    def redraw_marks(self) -> None:
        plotting.set_marks(self.figure, self.recording, self.shown_marks())
        self.canvas.draw_idle()

    def save_png(self) -> Path | None:
        """Save the marks currently shown as <stem>_plot_<kinds>.png; never the default name."""
        marks = self.shown_marks()
        path = plotting.png_path(self.recording, marks, default=False)
        try:
            plotting.save_png(self.recording, marks, path, self.subject_name)
        except Exception as error:
            print(f"Could not save the graph image {path}: {error}")
            self.set_status(f"Could not save {path.name}: {error}", error=True)
            return None
        self.set_status(f"Saved {path}")
        return path

    def set_status(self, text: str, error: bool = False) -> None:
        self.status.configure(text=text, foreground=ERROR_COLOR if error else STATUS_COLOR)
