"""Windows-style desktop interface for the multispectral alignment pipeline."""

from __future__ import annotations

from collections import deque
from pathlib import Path
import queue
import shutil
import statistics
import threading
import time
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from split_core import split_compound_image
from stack_core import build_alignment_priors, export_multipage_tiff_from_split


CENTER_CROP_FRAC = 0.80
JPEG_QUALITY = 100
SAVE_EXT = ".jpg"
HISTORY_LENGTH = 5
RATE_HISTORY_LENGTH = 72


def _split_set_is_complete(split_dir: Path) -> bool:
    return all((split_dir / f"{idx}.jpg").is_file() for idx in range(1, 7))


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


def _short_error(exc: Exception, limit: int = 180) -> str:
    text = " ".join(str(exc).split()) or exc.__class__.__name__
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _copy_tiff_to_collection(output_tiff: Path, input_path: Path) -> tuple[Path, str | None]:
    """Copy the final TIFF into <source-folder>/tiff without removing the original."""
    collection_dir = input_path.parent / "tiff"
    collection_path = collection_dir / output_tiff.name

    try:
        collection_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(output_tiff, collection_path)

        if not collection_path.is_file():
            raise OSError("destination file was not created")
        if collection_path.stat().st_size != output_tiff.stat().st_size:
            raise OSError("copied TIFF size does not match the original")

        return collection_path, None
    except Exception as exc:
        return collection_path, _short_error(exc)


def process_single_file(
    input_path: Path,
    alignment_priors=None,
    reuse_split: bool = False,
    status_callback=None,
):
    """Process one source image completely and return its output/alignment metadata."""
    base_output_dir = input_path.parent / input_path.stem
    split_dir = base_output_dir / "split_lens_images"
    output_tiff = base_output_dir / f"{input_path.stem}_aligned_multiband.tif"

    print(f"\n=== Processing: {input_path.name} ===")
    print(f"Output folder: {base_output_dir}")

    if reuse_split and _split_set_is_complete(split_dir):
        if status_callback:
            status_callback("Using existing six lens images")
        print("Using existing split lens images.")
    else:
        if status_callback:
            status_callback("Splitting compound image into six lens images")
        split_compound_image(
            input_image=input_path,
            output_dir=split_dir,
            center_crop_frac=CENTER_CROP_FRAC,
            save_ext=SAVE_EXT,
            jpeg_quality=JPEG_QUALITY,
        )
        print("Split completed.")

    if status_callback:
        status_callback("Aligning lens images and preparing 14-band TIFF")

    export_result = export_multipage_tiff_from_split(
        split_dir=split_dir,
        output_tiff=output_tiff,
        alignment_priors=alignment_priors,
        status_callback=status_callback,
    )

    if status_callback:
        status_callback("Copying final TIFF to shared tiff folder")

    collection_path, collection_copy_error = _copy_tiff_to_collection(
        output_tiff=output_tiff,
        input_path=input_path,
    )

    quality_report = export_result.get("quality_report", {})
    quality_summary = quality_report.get("summary", {})

    print("TIFF export completed.")
    print(f"Primary TIFF: {output_tiff}")
    if collection_copy_error:
        print(f"WARNING: shared TIFF copy failed: {collection_copy_error}")
    else:
        print(f"Shared TIFF copy: {collection_path}")

    return {
        "input": str(input_path),
        "output_dir": str(base_output_dir),
        "tiff": str(output_tiff),
        "tiff_collection_copy": str(collection_path),
        "tiff_collection_copy_ok": collection_copy_error is None,
        "tiff_collection_copy_error": collection_copy_error or "",
        "quality_report": quality_report.get("txt", ""),
        "quality": quality_summary.get("overall_quality", "not_available"),
        "alignment": export_result["alignment"],
        "history_retry_used": bool(
            export_result["alignment"].get("history_retry_used", False)
        ),
    }


def _nearest_success_alignments(success_records, failed_index, limit=HISTORY_LENGTH):
    """Return successful neighbours on both sides, nearest first."""
    ordered_indices = sorted(
        success_records,
        key=lambda idx: (abs(idx - failed_index), idx),
    )[:limit]
    return [success_records[idx]["alignment"] for idx in ordered_indices]


def _worker(file_paths, event_queue: queue.Queue, stop_event: threading.Event):
    """Background processing worker. No Tk calls are made from this thread."""
    success_records = {}
    pending_retry = []
    total = len(file_paths)

    for index, fp in enumerate(file_paths):
        if stop_event.is_set():
            break

        input_path = Path(fp)
        previous_indices = sorted(i for i in success_records if i < index)
        previous_alignments = [
            success_records[i]["alignment"]
            for i in previous_indices[-HISTORY_LENGTH:]
        ]
        priors = build_alignment_priors(
            previous_alignments,
            max_samples=HISTORY_LENGTH,
        )

        event_queue.put(("file_start", index, input_path.name, "initial"))
        started = time.perf_counter()

        def status(stage):
            event_queue.put(("stage", index, stage, "initial"))

        try:
            result = process_single_file(
                input_path,
                alignment_priors=priors,
                reuse_split=False,
                status_callback=status,
            )
            elapsed = time.perf_counter() - started
            success_records[index] = result
            event_queue.put(("success", index, result, elapsed, False))
        except Exception as exc:
            elapsed = time.perf_counter() - started
            pending_retry.append({
                "index": index,
                "path": input_path,
                "first_error": _short_error(exc),
                "traceback": traceback.format_exc(),
            })
            event_queue.put(("retry_queued", index, _short_error(exc), elapsed))

        event_queue.put(("attempt_complete", elapsed))

    if pending_retry and not stop_event.is_set():
        event_queue.put(("retry_begin", len(pending_retry)))

        for item in pending_retry:
            if stop_event.is_set():
                break

            index = item["index"]
            input_path = item["path"]
            neighbour_alignments = _nearest_success_alignments(
                success_records,
                failed_index=index,
                limit=HISTORY_LENGTH,
            )
            priors = build_alignment_priors(
                neighbour_alignments,
                max_samples=HISTORY_LENGTH,
            )

            if not priors:
                event_queue.put((
                    "final_error",
                    index,
                    "No successful neighbouring alignment was available for a history-guided retry.",
                    0.0,
                ))
                continue

            event_queue.put(("file_start", index, input_path.name, "retry"))
            started = time.perf_counter()

            def retry_status(stage):
                event_queue.put(("stage", index, stage, "retry"))

            try:
                result = process_single_file(
                    input_path,
                    alignment_priors=priors,
                    reuse_split=True,
                    status_callback=retry_status,
                )
                elapsed = time.perf_counter() - started
                success_records[index] = result
                event_queue.put(("success", index, result, elapsed, True))
            except Exception as exc:
                elapsed = time.perf_counter() - started
                combined_error = (
                    f"First pass: {item['first_error']} | "
                    f"History-guided retry: {_short_error(exc)}"
                )
                event_queue.put(("final_error", index, combined_error, elapsed))

            event_queue.put(("attempt_complete", elapsed))

    resolved = set(success_records)
    failed_indices = [i for i in range(total) if i not in resolved]

    if stop_event.is_set():
        event_queue.put(("cancelled", success_records, failed_indices))
    else:
        event_queue.put(("done", success_records, failed_indices))


class PipelineWindow:
    """Windows file-transfer-inspired progress window."""

    BG = "#ffffff"
    BORDER = "#d7d7d7"
    GRID = "#d7ead7"
    GREEN = "#11b425"
    GREEN_FILL = "#a7e49a"
    TEXT = "#111111"
    MUTED = "#555555"

    def __init__(self, root: tk.Tk, file_paths):
        self.root = root
        self.file_paths = list(file_paths)
        self.total = len(self.file_paths)

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

        self._configure_style()
        self._build_ui()
        self._populate_files()
        self.root.protocol("WM_DELETE_WINDOW", self._request_cancel)

        worker = threading.Thread(
            target=_worker,
            args=(self.file_paths, self.events, self.stop_event),
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
            text=f"Processing {self.total} multispectral image{'s' if self.total != 1 else ''}",
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
            else:
                self.current_stage = "Starting image"
                self._set_tree(index, status="Processing", details="")

            self.stage_label.configure(text=self.current_stage)

        elif kind == "stage":
            _, index, stage, phase = event
            prefix = "Retry: " if phase == "retry" else ""
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
            elif history_used:
                note_parts.insert(0, "Recovered with historical lens prior")

            if copy_ok:
                note_parts.append("TIFF saved in both locations")
                status_text = "Success"
            else:
                note_parts.append(
                    "Shared TIFF copy warning: "
                    + result.get("tiff_collection_copy_error", "unknown error")
                )
                status_text = "Success / warning"

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
                self.stage_label.configure(
                    text=f"{len(success_records)} successful · {len(failed_indices)} error(s)"
                )
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


def main():
    root = tk.Tk()
    root.withdraw()

    file_paths = filedialog.askopenfilenames(
        parent=root,
        title="Select one or more compound multispectral images",
        filetypes=[
            ("Image files", "*.jpg *.jpeg *.png *.tif *.tiff"),
            ("All files", "*.*"),
        ],
    )

    if not file_paths:
        root.destroy()
        return

    root.deiconify()
    PipelineWindow(root, file_paths)
    root.mainloop()


if __name__ == "__main__":
    main()
