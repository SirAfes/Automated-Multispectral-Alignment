"""
gui.py

Desktop interface: choose images or a folder, then a progress window in the
style of the Windows file-copy dialog. Processing runs in a background thread
(pipeline.run_pipeline); this module only displays its events.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
import queue
import statistics
import threading
import time
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .image_io import file_dialog_types
from .pipeline import collect_folder_images, run_pipeline, sort_paths, use_batch_mode
from .settings import load_settings


RATE_HISTORY_LENGTH = 72


def _format_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "Calculating..."

    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"About {seconds} sec"

    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"About {minutes} min {sec:02d} sec"

    hours, minutes = divmod(minutes, 60)
    return f"About {hours} h {minutes:02d} min"


def _worker(file_paths, event_queue: queue.Queue, stop_event: threading.Event, settings):
    """Background processing worker. No Tk calls are made from this thread."""
    try:
        run_pipeline(file_paths, event_queue.put, stop_event, settings)
    except Exception:
        event_queue.put(("crash", traceback.format_exc()))


class PipelineWindow:
    """Windows file-transfer-inspired progress window."""

    BG = "#ffffff"
    BORDER = "#d7d7d7"
    GRID = "#d7ead7"
    GREEN = "#11b425"
    GREEN_FILL = "#a7e49a"
    TEXT = "#111111"
    MUTED = "#555555"

    def __init__(self, root: tk.Tk, file_paths, settings):
        self.root = root
        self.settings = settings
        self.file_paths = sort_paths(file_paths)
        self.total = len(self.file_paths)
        self.batch_mode = use_batch_mode(self.total, settings)

        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.running = True
        self.details_visible = False

        self.success_count = 0
        self.error_count = 0
        self.retry_pending_count = 0
        self.resolved_count = 0

        self.current_index = None
        self.current_started_at = None
        self.current_filename = "Preparing..."
        self.current_stage = "Starting pipeline"

        self.attempt_durations = deque(maxlen=10)
        self.rate_history = deque([0.0] * RATE_HISTORY_LENGTH, maxlen=RATE_HISTORY_LENGTH)
        self.last_rate = 0.0
        self.tree_items = {}
        self.summary_path = None

        self._configure_style()
        self._build_ui()
        self._populate_files()
        self.root.protocol("WM_DELETE_WINDOW", self._request_cancel)

        worker = threading.Thread(
            target=_worker,
            args=(self.file_paths, self.events, self.stop_event, self.settings),
            daemon=True,
        )
        worker.start()

        self.root.after(100, self._poll_events)
        self.root.after(500, self._tick)

    def _configure_style(self):
        self.root.configure(bg=self.BG)
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Main.TFrame", background=self.BG)
        style.configure("Main.TLabel", background=self.BG, foreground=self.TEXT)
        style.configure("Muted.TLabel", background=self.BG, foreground=self.MUTED)
        style.configure("Green.Horizontal.TProgressbar", troughcolor="#efefef")

    def _build_ui(self):
        self.root.title("Automated Multispectral Alignment")
        self.root.geometry("690x520")
        self.root.minsize(650, 500)
        self.root.resizable(True, True)

        outer = ttk.Frame(self.root, padding=(38, 24, 38, 24), style="Main.TFrame")
        outer.pack(fill="both", expand=True)
        self.outer = outer

        self.operation_label = ttk.Label(
            outer,
            text=(
                f"Processing {self.total} multispectral image{'s' if self.total != 1 else ''}"
                + (" · batch consensus mode" if self.batch_mode else "")
            ),
            font=("Segoe UI", 11),
            style="Main.TLabel",
        )
        self.operation_label.pack(anchor="w")

        top_row = ttk.Frame(outer, style="Main.TFrame")
        top_row.pack(fill="x", pady=(4, 6))

        self.percent_label = ttk.Label(
            top_row,
            text="0% complete",
            font=("Segoe UI", 22),
            style="Main.TLabel",
        )
        self.percent_label.pack(side="left", anchor="w")

        self.cancel_button = ttk.Button(
            top_row,
            text="Cancel",
            command=self._request_cancel,
            width=9,
        )
        self.cancel_button.pack(side="right", anchor="e", pady=(6, 0))

        self.graph = tk.Canvas(
            outer,
            height=126,
            bg="#ffffff",
            highlightthickness=1,
            highlightbackground=self.BORDER,
        )
        self.graph.pack(fill="x", pady=(6, 18))
        self.graph.bind("<Configure>", lambda _event: self._draw_graph())

        info = ttk.Frame(outer, style="Main.TFrame")
        info.pack(fill="x")
        info.columnconfigure(1, weight=1)

        ttk.Label(info, text="Name:", font=("Segoe UI", 10), style="Main.TLabel").grid(
            row=0, column=0, sticky="nw", pady=2
        )
        self.name_label = ttk.Label(
            info, text="Preparing...", font=("Segoe UI", 10), style="Main.TLabel"
        )
        self.name_label.grid(row=0, column=1, sticky="nw", padx=(8, 0), pady=2)

        ttk.Label(info, text="Stage:", font=("Segoe UI", 10), style="Main.TLabel").grid(
            row=1, column=0, sticky="nw", pady=2
        )
        self.stage_label = ttk.Label(
            info,
            text="Starting pipeline",
            font=("Segoe UI", 10),
            style="Main.TLabel",
            wraplength=500,
        )
        self.stage_label.grid(row=1, column=1, sticky="nw", padx=(8, 0), pady=2)

        ttk.Label(info, text="Time remaining:", font=("Segoe UI", 10), style="Main.TLabel").grid(
            row=2, column=0, sticky="nw", pady=2
        )
        self.eta_label = ttk.Label(
            info, text="Calculating...", font=("Segoe UI", 10), style="Main.TLabel"
        )
        self.eta_label.grid(row=2, column=1, sticky="nw", padx=(8, 0), pady=2)

        ttk.Label(info, text="Items remaining:", font=("Segoe UI", 10), style="Main.TLabel").grid(
            row=3, column=0, sticky="nw", pady=2
        )
        self.remaining_label = ttk.Label(
            info,
            text=f"{self.total} ({self.retry_pending_count} pending retry)",
            font=("Segoe UI", 10),
            style="Main.TLabel",
        )
        self.remaining_label.grid(row=3, column=1, sticky="nw", padx=(8, 0), pady=2)

        self.progress = ttk.Progressbar(
            outer,
            mode="determinate",
            maximum=100,
            value=0,
            style="Green.Horizontal.TProgressbar",
        )
        self.progress.pack(fill="x", pady=(16, 14))

        separator = ttk.Separator(outer, orient="horizontal")
        separator.pack(fill="x", pady=(0, 8))

        controls = ttk.Frame(outer, style="Main.TFrame")
        controls.pack(fill="x")

        self.details_button = ttk.Button(
            controls,
            text="⌄  More details",
            command=self._toggle_details,
        )
        self.details_button.pack(side="left")

        self.counts_label = ttk.Label(
            controls,
            text="Success: 0   Pending retry: 0   Errors: 0",
            font=("Segoe UI", 9),
            style="Muted.TLabel",
        )
        self.counts_label.pack(side="right")

        self.details_frame = ttk.Frame(outer, style="Main.TFrame")
        self.tree = ttk.Treeview(
            self.details_frame,
            columns=("status", "file", "details"),
            show="headings",
            height=10,
        )
        self.tree.heading("status", text="Status")
        self.tree.heading("file", text="Image")
        self.tree.heading("details", text="Details")
        self.tree.column("status", width=120, stretch=False)
        self.tree.column("file", width=200, stretch=False)
        self.tree.column("details", width=400, stretch=True)

        scrollbar = ttk.Scrollbar(
            self.details_frame,
            orient="vertical",
            command=self.tree.yview,
        )
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _populate_files(self):
        for index, fp in enumerate(self.file_paths):
            item = self.tree.insert(
                "",
                "end",
                values=("Waiting", Path(fp).name, ""),
            )
            self.tree_items[index] = item

    def _toggle_details(self):
        self.details_visible = not self.details_visible
        if self.details_visible:
            self.details_frame.pack(fill="both", expand=True, pady=(12, 0))
            self.details_button.configure(text="⌃  Fewer details")
            self.root.geometry("860x760")
        else:
            self.details_frame.pack_forget()
            self.details_button.configure(text="⌄  More details")
            self.root.geometry("690x520")

    def _set_tree(self, index, status=None, details=None):
        item = self.tree_items.get(index)
        if item is None:
            return
        values = list(self.tree.item(item, "values"))
        if status is not None:
            values[0] = status
        if details is not None:
            values[2] = details
        self.tree.item(item, values=values)
        self.tree.see(item)

    def _record_attempt_duration(self, elapsed: float):
        if elapsed <= 0:
            return
        self.attempt_durations.append(float(elapsed))
        rate = 60.0 / max(float(elapsed), 0.001)
        # Cap only the graph display so one unusually quick retry does not flatten everything else.
        self.last_rate = min(rate, 20.0)
        self.rate_history.append(self.last_rate)
        self._draw_graph()

    def _draw_graph(self):
        canvas = self.graph
        canvas.delete("all")

        width = max(canvas.winfo_width(), 100)
        height = max(canvas.winfo_height(), 60)

        for i in range(1, 10):
            x = width * i / 10
            canvas.create_line(x, 0, x, height, fill=self.GRID)
        for i in range(1, 4):
            y = height * i / 4
            canvas.create_line(0, y, width, y, fill=self.GRID)

        values = list(self.rate_history)
        if not values:
            values = [0.0]

        max_value = max(max(values), 1.0)
        points = []
        count = max(len(values) - 1, 1)
        for i, value in enumerate(values):
            x = width * i / count
            y = height - (value / max_value) * (height * 0.78)
            points.extend([x, y])

        polygon = [0, height] + points + [width, height]
        if len(points) >= 4:
            canvas.create_polygon(polygon, fill=self.GREEN_FILL, outline="")
            canvas.create_line(points, fill=self.GREEN, width=2)

        canvas.create_line(0, height - 1, width, height - 1, fill="#222222")
        canvas.create_text(
            width - 10,
            10,
            anchor="ne",
            text=f"Rate: {self.last_rate:.2f} images/min",
            fill=self.TEXT,
            font=("Segoe UI", 10),
        )

    def _refresh_summary(self):
        self.resolved_count = min(self.total, self.success_count + self.error_count)
        percentage = int(round(100 * self.resolved_count / max(self.total, 1)))
        remaining = max(0, self.total - self.resolved_count)

        self.percent_label.configure(text=f"{percentage}% complete")
        self.progress.configure(value=percentage)
        self.remaining_label.configure(
            text=f"{remaining} ({self.retry_pending_count} pending retry)"
        )
        self.counts_label.configure(
            text=(
                f"Success: {self.success_count}   "
                f"Pending retry: {self.retry_pending_count}   "
                f"Errors: {self.error_count}"
            )
        )

    def _estimated_remaining_seconds(self) -> float | None:
        if not self.attempt_durations:
            return None

        typical = statistics.median(self.attempt_durations)
        unresolved = max(0, self.total - self.success_count - self.error_count)
        eta = typical * unresolved

        if self.current_started_at is not None and unresolved > 0:
            eta -= time.perf_counter() - self.current_started_at

        return max(0.0, eta)

    def _poll_events(self):
        try:
            while True:
                self._handle_event(self.events.get_nowait())
        except queue.Empty:
            pass

        if self.running:
            self.root.after(100, self._poll_events)

    def _handle_event(self, event):
        kind = event[0]

        if kind == "file_start":
            _, index, filename, phase = event
            self.current_index = index
            self.current_filename = filename
            self.current_started_at = time.perf_counter()
            self.name_label.configure(text=filename)

            if phase == "retry":
                self.current_stage = "Second-pass neighbour-guided retry"
                self._set_tree(
                    index,
                    status="Retrying",
                    details="Using successful neighbouring images",
                )
            elif phase == "calibration":
                self.current_stage = "Calibration: full alignment"
                self._set_tree(index, status="Calibrating", details="")
            else:
                self.current_stage = "Starting image"
                self._set_tree(index, status="Processing", details="")

            self.stage_label.configure(text=self.current_stage)

        elif kind == "stage":
            _, index, stage, phase = event
            prefix = {"retry": "Retry: ", "calibration": "Calibration: "}.get(phase, "")
            self.current_stage = prefix + stage
            self.stage_label.configure(text=self.current_stage)
            self._set_tree(index, details=self.current_stage)

        elif kind == "success":
            _, index, result, elapsed, recovered = event
            quality = str(result.get("quality", "not_available")).replace("_", " ").title()
            history_used = bool(result.get("history_retry_used"))
            copy_ok = bool(result.get("tiff_collection_copy_ok", False))

            note_parts = [f"Quality: {quality}", f"{elapsed:.1f} s"]
            if recovered:
                note_parts.insert(0, "Recovered on second pass")
            elif result.get("batch_consensus_used"):
                note_parts.insert(0, "Batch geometry (verified)")
            elif history_used:
                note_parts.insert(0, "Recovered with historical lens prior")

            if copy_ok:
                note_parts.append(
                    "TIFF saved in both locations" if result.get("tiff_collection_copy") else "TIFF saved"
                )
                status_text = "Success"
            else:
                note_parts.append(
                    "Shared TIFF copy warning: "
                    + result.get("tiff_collection_copy_error", "unknown error")
                )
                status_text = "Success / warning"

            closure = result.get("closure_status")
            if closure == "fail":
                note_parts.insert(1, "Closure check FAILED - inspect")
                status_text = "Success / check"
            elif closure == "pass":
                note_parts.insert(1, "Closure check passed")

            self._set_tree(index, status=status_text, details=" · ".join(note_parts))
            self.success_count += 1
            if recovered and self.retry_pending_count > 0:
                self.retry_pending_count -= 1
            self.current_started_at = None
            self._refresh_summary()

        elif kind == "retry_queued":
            _, index, error, elapsed = event
            self.retry_pending_count += 1
            self.current_started_at = None
            self._set_tree(
                index,
                status="Retry queued",
                details=f"{error} · first pass {elapsed:.1f} s",
            )
            self._refresh_summary()

        elif kind == "final_error":
            _, index, error, elapsed = event
            if self.retry_pending_count > 0:
                self.retry_pending_count -= 1
            self.error_count += 1
            self.current_started_at = None
            suffix = f" · retry {elapsed:.1f} s" if elapsed else ""
            self._set_tree(index, status="Error", details=error + suffix)
            self._refresh_summary()

        elif kind == "attempt_complete":
            _, elapsed = event
            self._record_attempt_duration(float(elapsed))

        elif kind == "run_files":
            _, _log_path, summary_path = event
            self.summary_path = summary_path

        elif kind == "workers":
            _, count = event
            if count > 1:
                self.operation_label.configure(
                    text=self.operation_label.cget("text") + f" · {count} at a time"
                )

        elif kind in {"mode", "phase"}:
            _, text = event
            self.current_stage = text
            self.stage_label.configure(text=text)

        elif kind == "crash":
            _, tb = event
            self.running = False
            messagebox.showerror("Processing stopped", tb[-1500:], parent=self.root)
            self.cancel_button.configure(text="Close", command=self.root.destroy, state="normal")

        elif kind == "retry_begin":
            _, total = event
            self.current_stage = f"Second pass: retrying {total} unresolved image(s)"
            self.stage_label.configure(text=self.current_stage)

        elif kind in {"done", "cancelled"}:
            _, success_records, failed_indices = event
            self.running = False
            self.current_started_at = None

            if kind == "cancelled":
                self.name_label.configure(text="Processing stopped")
                self.stage_label.configure(text="Cancelled after the current image")
                self.percent_label.configure(text=f"{int(self.progress['value'])}% complete")
                self.eta_label.configure(text="Stopped")
                for index in failed_indices:
                    values = self.tree.item(self.tree_items[index], "values")
                    if values and values[0] in {"Waiting", "Retry queued"}:
                        self._set_tree(index, status="Cancelled", details="Not processed")
            else:
                self.name_label.configure(text="Processing complete")
                summary = f"{len(success_records)} successful · {len(failed_indices)} error(s)"
                if self.summary_path:
                    summary += f"\nSummary: {self.summary_path}"
                self.stage_label.configure(text=summary)
                self.success_count = len(success_records)
                self.error_count = len(failed_indices)
                self.retry_pending_count = 0
                self._refresh_summary()
                self.percent_label.configure(text="100% complete")
                self.progress.configure(value=100)
                self.eta_label.configure(text="Complete")

            self.cancel_button.configure(text="Close", command=self.root.destroy)
            if failed_indices and not self.details_visible:
                self._toggle_details()

    def _tick(self):
        if not self.running:
            return

        # Extend the visual rate trace between completed images so it behaves
        # more like the Windows file-copy graph rather than a static bar chart.
        self.rate_history.append(self.last_rate)
        self._draw_graph()
        self.eta_label.configure(text=_format_seconds(self._estimated_remaining_seconds()))
        self.root.after(500, self._tick)

    def _request_cancel(self):
        if not self.running:
            self.root.destroy()
            return

        should_cancel = messagebox.askyesno(
            "Stop processing?",
            "The current image will be allowed to finish safely, then the remaining images will be skipped. Continue?",
            parent=self.root,
        )
        if not should_cancel:
            return

        self.stop_event.set()
        self.cancel_button.configure(state="disabled")
        self.stage_label.configure(text="Stopping after the current image...")


def _choose_inputs(root: tk.Tk):
    """Small launcher: choose individual images or a whole folder."""
    choice = {"value": None}

    dialog = tk.Toplevel(root)
    dialog.title("Automated Multispectral Alignment")
    dialog.resizable(False, False)
    frame = ttk.Frame(dialog, padding=24)
    frame.pack(fill="both", expand=True)

    ttk.Label(frame, text="What would you like to process?", font=("Segoe UI", 11)).pack(
        anchor="w", pady=(0, 12)
    )

    def pick(value):
        choice["value"] = value
        dialog.destroy()

    ttk.Button(frame, text="Select images...", width=28, command=lambda: pick("files")).pack(
        fill="x", pady=3
    )
    ttk.Button(frame, text="Select a whole folder...", width=28, command=lambda: pick("folder")).pack(
        fill="x", pady=3
    )
    ttk.Label(
        frame,
        text="In a folder with both RAW and JPG of the same shot, only the RAW is used.",
        font=("Segoe UI", 8),
        wraplength=260,
    ).pack(anchor="w", pady=(10, 0))

    dialog.protocol("WM_DELETE_WINDOW", lambda: pick(None))
    dialog.update_idletasks()
    x = (dialog.winfo_screenwidth() - dialog.winfo_width()) // 2
    y = (dialog.winfo_screenheight() - dialog.winfo_height()) // 3
    dialog.geometry(f"+{x}+{y}")
    dialog.grab_set()
    root.wait_window(dialog)

    if choice["value"] == "files":
        return list(filedialog.askopenfilenames(
            parent=root,
            title="Select one or more compound multispectral images",
            filetypes=file_dialog_types(),
        ))

    if choice["value"] == "folder":
        folder = filedialog.askdirectory(parent=root, title="Select a folder of compound images")
        if not folder:
            return []
        files = collect_folder_images(folder)
        if not files:
            messagebox.showinfo(
                "No images found",
                "The selected folder contains no supported images.",
                parent=root,
            )
        return files

    return []


def main():
    import multiprocessing

    multiprocessing.freeze_support()
    root = tk.Tk()
    root.withdraw()

    try:
        settings = load_settings()
    except Exception as exc:
        messagebox.showerror("Configuration error", str(exc), parent=root)
        root.destroy()
        return

    file_paths = _choose_inputs(root)
    if not file_paths:
        root.destroy()
        return

    root.deiconify()
    PipelineWindow(root, file_paths, settings)
    root.mainloop()


if __name__ == "__main__":
    main()
