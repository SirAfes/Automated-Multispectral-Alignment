"""Desktop interface for the automated multispectral alignment pipeline."""

from __future__ import annotations

from pathlib import Path
import queue
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


def _split_set_is_complete(split_dir: Path) -> bool:
    return all((split_dir / f"{idx}.jpg").is_file() for idx in range(1, 7))


def _format_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "Estimating..."

    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"About {seconds} sec remaining"

    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"About {minutes} min {sec:02d} sec remaining"

    hours, minutes = divmod(minutes, 60)
    return f"About {hours} h {minutes:02d} min remaining"


def _short_error(exc: Exception, limit: int = 150) -> str:
    text = " ".join(str(exc).split()) or exc.__class__.__name__
    return text if len(text) <= limit else text[: limit - 3] + "..."


def process_single_file(
    input_path: Path,
    alignment_priors=None,
    reuse_split: bool = False,
    status_callback=None,
):
    """Process one source image completely and return its alignment metadata."""
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

    export_result = export_multipage_tiff_from_split(
        split_dir=split_dir,
        output_tiff=output_tiff,
        alignment_priors=alignment_priors,
        status_callback=status_callback,
    )

    quality_report = export_result.get("quality_report", {})
    quality_summary = quality_report.get("summary", {})

    print("TIFF export completed.")
    print(
        f"Final TIFF: {output_tiff.name} | "
        f"size={export_result['final_size']['width']}x{export_result['final_size']['height']}"
    )

    return {
        "input": str(input_path),
        "output_dir": str(base_output_dir),
        "tiff": str(output_tiff),
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


def _worker(file_paths, event_queue: queue.Queue):
    """Background processing worker. No Tk calls are made from this thread."""
    success_records = {}
    pending_retry = []
    first_pass_durations = []
    retry_durations = []
    total = len(file_paths)

    for index, fp in enumerate(file_paths):
        input_path = Path(fp)
        previous_indices = sorted(i for i in success_records if i < index)
        previous_alignments = [
            success_records[i]["alignment"] for i in previous_indices[-HISTORY_LENGTH:]
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
            first_pass_durations.append(elapsed)
            success_records[index] = result
            event_queue.put(("success", index, result, elapsed, False))
        except Exception as exc:
            elapsed = time.perf_counter() - started
            first_pass_durations.append(elapsed)
            pending_retry.append({
                "index": index,
                "path": input_path,
                "first_error": _short_error(exc),
                "traceback": traceback.format_exc(),
            })
            event_queue.put(("retry_queued", index, _short_error(exc), elapsed))

        avg = sum(first_pass_durations) / len(first_pass_durations)
        event_queue.put(("phase_progress", "initial", index + 1, total, avg))

    if pending_retry:
        event_queue.put(("retry_begin", len(pending_retry)))

        for retry_number, item in enumerate(pending_retry, start=1):
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
                event_queue.put((
                    "phase_progress",
                    "retry",
                    retry_number,
                    len(pending_retry),
                    (sum(retry_durations) / len(retry_durations)) if retry_durations else 0.0,
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
                retry_durations.append(elapsed)
                success_records[index] = result
                event_queue.put(("success", index, result, elapsed, True))
            except Exception as exc:
                elapsed = time.perf_counter() - started
                retry_durations.append(elapsed)
                combined_error = (
                    f"First pass: {item['first_error']} | "
                    f"History-guided retry: {_short_error(exc)}"
                )
                event_queue.put(("final_error", index, combined_error, elapsed))

            avg_retry = sum(retry_durations) / len(retry_durations)
            event_queue.put((
                "phase_progress",
                "retry",
                retry_number,
                len(pending_retry),
                avg_retry,
            ))

    failed_indices = [i for i in range(total) if i not in success_records]
    event_queue.put(("done", success_records, failed_indices))


class PipelineWindow:
    """File-transfer-style progress window for the desktop executable."""

    def __init__(self, root: tk.Tk, file_paths):
        self.root = root
        self.file_paths = list(file_paths)
        self.events = queue.Queue()
        self.details_visible = False
        self.running = True
        self.phase = "initial"
        self.current_started_at = None
        self.avg_duration = 0.0
        self.phase_processed = 0
        self.phase_total = len(self.file_paths)
        self.success_count = 0
        self.error_count = 0
        self.retry_pending_count = 0
        self.tree_items = {}

        self._build_ui()
        self._populate_files()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        worker = threading.Thread(
            target=_worker,
            args=(self.file_paths, self.events),
            daemon=True,
        )
        worker.start()

        self.root.after(100, self._poll_events)
        self.root.after(500, self._update_eta_tick)

    def _build_ui(self):
        self.root.title("Automated Multispectral Alignment")
        self.root.geometry("860x390")
        self.root.minsize(760, 360)

        outer = ttk.Frame(self.root, padding=22)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text="Processing multispectral images",
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w")

        self.current_label = ttk.Label(
            outer,
            text="Preparing...",
            font=("Segoe UI", 11),
        )
        self.current_label.pack(anchor="w", pady=(14, 2))

        self.stage_label = ttk.Label(outer, text="Starting pipeline")
        self.stage_label.pack(anchor="w")

        self.progress = ttk.Progressbar(
            outer,
            mode="determinate",
            maximum=100,
            value=0,
        )
        self.progress.pack(fill="x", pady=(16, 8))

        info = ttk.Frame(outer)
        info.pack(fill="x")

        self.progress_label = ttk.Label(info, text=f"0 of {len(self.file_paths)} processed")
        self.progress_label.pack(side="left")

        self.eta_label = ttk.Label(info, text="Estimating...")
        self.eta_label.pack(side="right")

        counts = ttk.Frame(outer)
        counts.pack(fill="x", pady=(16, 8))

        self.success_label = ttk.Label(counts, text="Success: 0")
        self.success_label.pack(side="left")

        self.pending_label = ttk.Label(counts, text="Pending retry: 0")
        self.pending_label.pack(side="left", padx=(24, 0))

        self.error_label = ttk.Label(counts, text="Errors: 0")
        self.error_label.pack(side="left", padx=(24, 0))

        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(8, 0))

        self.details_button = ttk.Button(
            controls,
            text="Show details",
            command=self._toggle_details,
        )
        self.details_button.pack(side="left")

        self.close_button = ttk.Button(
            controls,
            text="Close",
            command=self._on_close,
            state="disabled",
        )
        self.close_button.pack(side="right")

        self.details_frame = ttk.Frame(outer)
        self.tree = ttk.Treeview(
            self.details_frame,
            columns=("status", "file", "details"),
            show="headings",
            height=10,
        )
        self.tree.heading("status", text="Status")
        self.tree.heading("file", text="Image")
        self.tree.heading("details", text="Details")
        self.tree.column("status", width=110, stretch=False)
        self.tree.column("file", width=220, stretch=False)
        self.tree.column("details", width=450, stretch=True)

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
            self.details_frame.pack(fill="both", expand=True, pady=(16, 0))
            self.details_button.configure(text="Hide details")
            self.root.geometry("860x650")
        else:
            self.details_frame.pack_forget()
            self.details_button.configure(text="Show details")
            self.root.geometry("860x390")

    def _set_tree(self, index, status=None, details=None):
        item = self.tree_items[index]
        values = list(self.tree.item(item, "values"))
        if status is not None:
            values[0] = status
        if details is not None:
            values[2] = details
        self.tree.item(item, values=values)
        self.tree.see(item)

    def _poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass

        if self.running:
            self.root.after(100, self._poll_events)

    def _handle_event(self, event):
        kind = event[0]

        if kind == "file_start":
            _, index, filename, phase = event
            self.phase = phase
            self.current_started_at = time.perf_counter()
            self.current_label.configure(text=filename)
            if phase == "retry":
                self.stage_label.configure(text="Second-pass neighbour-guided retry")
                self._set_tree(index, status="Retrying", details="Using successful neighbouring images")
            else:
                self.stage_label.configure(text="Starting image")
                self._set_tree(index, status="Processing", details="")

        elif kind == "stage":
            _, index, stage, phase = event
            prefix = "Retry: " if phase == "retry" else ""
            self.stage_label.configure(text=prefix + stage)
            self._set_tree(index, details=prefix + stage)

        elif kind == "success":
            _, index, result, elapsed, recovered = event
            quality = str(result.get("quality", "not_available")).replace("_", " ").title()
            history_used = bool(result.get("history_retry_used"))
            note_parts = [f"Quality: {quality}", f"{elapsed:.1f} s"]
            if recovered:
                note_parts.insert(0, "Recovered on second pass")
            elif history_used:
                note_parts.insert(0, "Recovered with historical lens prior")
            self._set_tree(index, status="Success", details=" · ".join(note_parts))
            self.success_count += 1
            if recovered and self.retry_pending_count > 0:
                self.retry_pending_count -= 1
            self._update_counts()

        elif kind == "retry_queued":
            _, index, error, elapsed = event
            self.retry_pending_count += 1
            self._set_tree(
                index,
                status="Retry queued",
                details=f"{error} · first pass {elapsed:.1f} s",
            )
            self._update_counts()

        elif kind == "final_error":
            _, index, error, elapsed = event
            if self.retry_pending_count > 0:
                self.retry_pending_count -= 1
            self.error_count += 1
            suffix = f" · retry {elapsed:.1f} s" if elapsed else ""
            self._set_tree(index, status="Error", details=error + suffix)
            self._update_counts()

        elif kind == "phase_progress":
            _, phase, processed, total, avg = event
            self.phase = phase
            self.phase_processed = processed
            self.phase_total = total
            self.avg_duration = float(avg or 0.0)
            self.current_started_at = None

            if phase == "initial":
                value = 85.0 * processed / max(total, 1)
                self.progress_label.configure(text=f"{processed} of {total} first-pass images checked")
            else:
                value = 85.0 + 15.0 * processed / max(total, 1)
                self.progress_label.configure(text=f"Retry {processed} of {total}")
            self.progress.configure(value=value)

        elif kind == "retry_begin":
            _, total = event
            self.phase = "retry"
            self.phase_processed = 0
            self.phase_total = total
            self.avg_duration = 0.0
            self.current_started_at = None
            self.stage_label.configure(
                text=f"Second pass: retrying {total} unresolved image(s) using neighbouring successes"
            )

        elif kind == "done":
            _, success_records, failed_indices = event
            self.running = False
            self.progress.configure(value=100)
            self.current_label.configure(text="Processing complete")
            self.stage_label.configure(
                text=f"{len(success_records)} successful · {len(failed_indices)} error(s)"
            )
            self.progress_label.configure(text="Finished")
            self.eta_label.configure(text="Complete")
            self.close_button.configure(state="normal")
            self._update_counts()

            if failed_indices and not self.details_visible:
                self._toggle_details()

    def _update_counts(self):
        self.success_label.configure(text=f"Success: {self.success_count}")
        self.pending_label.configure(text=f"Pending retry: {self.retry_pending_count}")
        self.error_label.configure(text=f"Errors: {self.error_count}")

    def _update_eta_tick(self):
        if not self.running:
            return

        eta = None
        if self.avg_duration > 0 and self.phase_total > 0:
            remaining_units = max(0, self.phase_total - self.phase_processed)
            eta = self.avg_duration * remaining_units
            if self.current_started_at is not None and remaining_units > 0:
                eta -= time.perf_counter() - self.current_started_at
            eta = max(0.0, eta)

        self.eta_label.configure(text=_format_seconds(eta))
        self.root.after(500, self._update_eta_tick)

    def _on_close(self):
        if self.running:
            should_close = messagebox.askyesno(
                "Processing is still running",
                "Close the application and stop the current processing session?",
                parent=self.root,
            )
            if not should_close:
                return
        self.root.destroy()


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
