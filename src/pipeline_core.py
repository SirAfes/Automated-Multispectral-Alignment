"""
pipeline_core.py

Batch processing logic without any GUI code, so it can be tested and reused.

The GUI calls run_pipeline() in a background thread and receives progress as
event tuples through `emit`.
"""

from __future__ import annotations

import os
import re
import shutil
import time
import traceback
from pathlib import Path

from batch_core import (
    calibration_indices,
    check_verification,
    compute_consensus,
    consensus_alignment_result,
    response_thresholds,
    verify_shifts,
)
from image_io import (
    STANDARD_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    find_split_set,
    is_raw_file,
    read_image,
    split_ext_for_input,
)
from settings import file_bands_int, load_settings
from split_core import save_split_cells, split_compound_array
from stack_core import (
    build_alignment_priors,
    export_aligned_tiff,
    export_multipage_tiff_from_split,
    load_split_images,
)


# ---------------------------------------------------------------------------
# Input files
# ---------------------------------------------------------------------------

def natural_key(path) -> list:
    """Sort key that orders DSC2 before DSC10."""
    p = Path(path)
    parts = re.split(r"(\d+)", p.name.lower())
    return [str(p.parent).lower()] + [int(t) if t.isdigit() else t for t in parts]


def sort_paths(paths) -> list[Path]:
    return sorted((Path(p) for p in paths), key=natural_key)


def collect_folder_images(folder, prefer_raw: bool = True) -> list[Path]:
    """
    Supported images directly inside `folder` (not in subfolders, so earlier
    outputs are never picked up). If a shot exists as RAW and JPG, only the RAW
    file is used when prefer_raw is True.
    """
    folder = Path(folder)
    files = [
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    ]

    if prefer_raw:
        raw_stems = {p.stem.lower() for p in files if is_raw_file(p)}
        files = [
            p for p in files
            if not (p.suffix.lower() in STANDARD_EXTENSIONS and p.stem.lower() in raw_stems)
        ]

    return sort_paths(files)


# ---------------------------------------------------------------------------
# Single image
# ---------------------------------------------------------------------------

def output_name(input_path: Path) -> str:
    """
    Output base name. RAW files get their extension appended (DSC06664_ARW) so a
    RAW+JPG pair from the same shot never overwrites each other's results.
    """
    if is_raw_file(input_path):
        return f"{input_path.stem}_{input_path.suffix.lstrip('.').upper()}"
    return input_path.stem


def output_paths(input_path: Path):
    name = output_name(input_path)
    base_output_dir = input_path.parent / name
    return {
        "base": base_output_dir,
        "split_dir": base_output_dir / "split_lens_images",
        "tiff": base_output_dir / f"{name}_aligned_multiband.tif",
    }


def short_error(exc: Exception, limit: int = 180) -> str:
    text = " ".join(str(exc).split()) or exc.__class__.__name__
    return text if len(text) <= limit else text[: limit - 3] + "..."


def add_to_collection(output_tiff: Path, input_path: Path, mode: str = "hardlink"):
    """
    Put the final TIFF also into <source-folder>/tiff.

    "hardlink" costs no extra disk space (same file under two names);
    it falls back to a real copy where links are not supported.
    Returns (collection_path or None, error or None).
    """
    if mode == "off":
        return None, None

    collection_dir = input_path.parent / "tiff"
    collection_path = collection_dir / output_tiff.name

    try:
        collection_dir.mkdir(parents=True, exist_ok=True)
        if collection_path.exists() or collection_path.is_symlink():
            collection_path.unlink()

        linked = False
        if mode == "hardlink":
            try:
                os.link(output_tiff, collection_path)
                linked = True
            except OSError:
                linked = False
        if not linked:
            shutil.copy2(output_tiff, collection_path)

        if not collection_path.is_file():
            raise OSError("destination file was not created")
        if collection_path.stat().st_size != output_tiff.stat().st_size:
            raise OSError("collection TIFF size does not match the original")

        return collection_path, None
    except Exception as exc:
        return collection_path, short_error(exc)


def _split_kwargs(settings):
    return {
        "grid_cols": settings["grid_cols"],
        "grid_rows": settings["grid_rows"],
        "cell_size": settings["cell_size"],
        "center_crop_frac": settings["center_crop_frac"],
    }


def _finish(input_path, paths, export_result, settings, status_callback):
    if status_callback and settings["collection_mode"] != "off":
        status_callback("Adding TIFF to shared tiff folder")

    collection_path, collection_error = add_to_collection(
        paths["tiff"], input_path, settings["collection_mode"]
    )

    quality_report = export_result.get("quality_report", {})
    quality_summary = quality_report.get("summary", {})

    print("TIFF export completed.")
    print(f"Primary TIFF: {paths['tiff']}")
    if collection_error:
        print(f"WARNING: shared TIFF copy failed: {collection_error}")
    elif collection_path:
        print(f"Shared TIFF: {collection_path}")

    return {
        "input": str(input_path),
        "output_dir": str(paths["base"]),
        "tiff": str(paths["tiff"]),
        "tiff_collection_copy": str(collection_path or ""),
        "tiff_collection_copy_ok": collection_error is None,
        "tiff_collection_copy_error": collection_error or "",
        "quality_report": quality_report.get("txt", ""),
        "quality": quality_summary.get("overall_quality", "not_available"),
        "alignment": export_result["alignment"],
        "split_info": export_result.get("split_info", {}),
        "history_retry_used": bool(export_result["alignment"].get("history_retry_used", False)),
        "batch_consensus_used": export_result["alignment"].get("mode") == "batch_consensus",
    }


def process_single_file(
    input_path: Path,
    alignment_priors=None,
    reuse_split: bool = False,
    status_callback=None,
    settings=None,
):
    """Full per-image processing: split, search alignment, export, report."""
    settings = settings or load_settings()
    input_path = Path(input_path)
    paths = output_paths(input_path)
    split_ext = split_ext_for_input(input_path)

    print(f"\n=== Processing: {input_path.name} ===")
    print(f"Output folder: {paths['base']}")

    images = None
    split_info = {}
    if reuse_split and find_split_set(paths["split_dir"], split_ext):
        if status_callback:
            status_callback("Using existing six lens images")
        print("Using existing split lens images.")
    else:
        if status_callback:
            status_callback("Splitting compound image into six lens images")
        images, split_info = split_compound_array(read_image(input_path), **_split_kwargs(settings))
        save_split_cells(images, paths["split_dir"], split_ext, settings["jpeg_quality"])
        print("Split completed.")

    if images is None:
        images = load_split_images(paths["split_dir"], split_ext)

    export_result = export_multipage_tiff_from_split(
        split_dir=paths["split_dir"],
        output_tiff=paths["tiff"],
        alignment_priors=alignment_priors,
        status_callback=status_callback,
        images=images,
        file_bands=file_bands_int(settings),
        output_order=settings["output_order"],
        split_info=split_info,
    )
    return _finish(input_path, paths, export_result, settings, status_callback)


class ConsensusRejected(Exception):
    """The batch consensus shift could not be confirmed on this image."""


def process_with_consensus(
    input_path: Path,
    consensus: dict,
    thresholds: dict,
    status_callback=None,
    settings=None,
):
    """
    Fast path for batch mode: split in memory, verify the consensus shift on
    this image and export with it. Raises ConsensusRejected if not confirmed.
    """
    settings = settings or load_settings()
    input_path = Path(input_path)
    paths = output_paths(input_path)

    print(f"\n=== Processing (batch consensus): {input_path.name} ===")

    if status_callback:
        status_callback("Splitting compound image into six lens images")
    images, split_info = split_compound_array(read_image(input_path), **_split_kwargs(settings))

    if status_callback:
        status_callback("Verifying batch lens geometry on this image")
    try:
        verification = verify_shifts(
            images, consensus["shifts"], settings["batch_verify_max_residual_px"]
        )
    except ValueError as exc:
        raise ConsensusRejected(str(exc)) from exc
    ok, reason = check_verification(verification, thresholds, settings)
    if not ok:
        raise ConsensusRejected(reason)

    if settings["batch_keep_split_images"]:
        save_split_cells(
            images, paths["split_dir"], split_ext_for_input(input_path), settings["jpeg_quality"]
        )

    export_result = export_aligned_tiff(
        images,
        consensus_alignment_result(consensus, verification),
        paths["tiff"],
        status_callback=status_callback,
        file_bands=file_bands_int(settings),
        output_order=settings["output_order"],
        split_info=split_info,
    )
    return _finish(input_path, paths, export_result, settings, status_callback)


# ---------------------------------------------------------------------------
# Batch run
# ---------------------------------------------------------------------------

def nearest_success_alignments(success_records, index, limit):
    """Successful neighbours on both sides, nearest first."""
    ordered = sorted(success_records, key=lambda i: (abs(i - index), i))[:limit]
    return [success_records[i]["alignment"] for i in ordered]


class _Run:
    """
    Shared state of one batch run.

    Events emitted (tuples):
        ("mode", text)                         processing mode chosen
        ("phase", text)                        a new phase begins
        ("file_start", index, name, phase)     phase: "initial" | "calibration" | "consensus" | "retry"
        ("stage", index, text, phase)
        ("success", index, result, elapsed, recovered)
        ("retry_queued", index, error, elapsed)
        ("final_error", index, error, elapsed)
        ("attempt_complete", elapsed)
        ("retry_begin", count)
        ("done" | "cancelled", success_records, failed_indices)
    """

    def __init__(self, file_paths, emit, stop_event, settings):
        self.paths = [Path(p) for p in file_paths]
        self.emit = emit
        self.stop_event = stop_event
        self.settings = settings
        self.success = {}
        self.pending_retry = []
        self.history = int(settings["history_length"])

    def stopped(self):
        return self.stop_event is not None and self.stop_event.is_set()

    def _status(self, index, phase):
        return lambda stage: self.emit(("stage", index, stage, phase))

    def standard(self, index, priors, phase="initial"):
        """Full alignment for one image; failures go to the retry queue."""
        path = self.paths[index]
        self.emit(("file_start", index, path.name, phase))
        started = time.perf_counter()
        try:
            result = process_single_file(
                path,
                alignment_priors=priors,
                status_callback=self._status(index, phase),
                settings=self.settings,
            )
            elapsed = time.perf_counter() - started
            self.success[index] = result
            self.emit(("success", index, result, elapsed, False))
        except Exception as exc:
            elapsed = time.perf_counter() - started
            self.pending_retry.append({
                "index": index,
                "path": path,
                "first_error": short_error(exc),
                "traceback": traceback.format_exc(),
            })
            self.emit(("retry_queued", index, short_error(exc), elapsed))
        self.emit(("attempt_complete", elapsed))

    def previous_priors(self, index):
        previous = sorted(i for i in self.success if i < index)[-self.history:]
        return build_alignment_priors(
            [self.success[i]["alignment"] for i in previous],
            max_samples=self.history,
        )

    def run_standard(self, indices):
        for index in indices:
            if self.stopped():
                return
            self.standard(index, self.previous_priors(index))

    def run_batch(self):
        s = self.settings
        total = len(self.paths)
        calib = calibration_indices(total, int(s["batch_calibration_samples"]))

        self.emit(("phase", f"Calibration: fully aligning {len(calib)} sample images"))
        for index in calib:
            if self.stopped():
                return
            self.standard(index, self.previous_priors(index), phase="calibration")

        calib_ok = [i for i in calib if i in self.success]
        consensus = compute_consensus([self.success[i]["alignment"] for i in calib_ok], s)
        rest = [i for i in range(total) if i not in set(calib)]

        if not consensus["consistent"]:
            self.emit(("phase", f"No common geometry ({consensus['reason']}); aligning each image"))
            self.run_standard(rest)
            return

        # Data-driven verification thresholds from calibration images with known-good shifts.
        agreeing = [i for i, ok in zip(calib_ok, consensus["agreeing_mask"]) if ok]
        verifications = []
        for i in agreeing:
            src = self.paths[i]
            try:
                images = load_split_images(output_paths(src)["split_dir"], split_ext_for_input(src))
            except Exception:
                continue
            own = {
                k: {
                    "dx": self.success[i]["alignment"]["apply_shifts"][k]["dx"]
                    - self.success[i]["alignment"]["apply_shifts"][1]["dx"],
                    "dy": self.success[i]["alignment"]["apply_shifts"][k]["dy"]
                    - self.success[i]["alignment"]["apply_shifts"][1]["dy"],
                }
                for k in range(1, 7)
            }
            try:
                verifications.append(verify_shifts(images, own, s["batch_verify_max_residual_px"]))
            except ValueError:
                continue
        thresholds = response_thresholds(verifications, s)
        priors = build_alignment_priors(
            [self.success[i]["alignment"] for i in agreeing], max_samples=len(agreeing)
        )

        self.emit(("phase", f"Applying batch geometry ({consensus['reason']})"))
        print(f"Batch consensus shifts: {consensus['shifts']}")
        print(f"Verification response thresholds: {thresholds}")

        for index in rest:
            if self.stopped():
                return
            path = self.paths[index]
            self.emit(("file_start", index, path.name, "consensus"))
            started = time.perf_counter()
            try:
                result = process_with_consensus(
                    path,
                    consensus,
                    thresholds,
                    status_callback=self._status(index, "consensus"),
                    settings=s,
                )
                elapsed = time.perf_counter() - started
                self.success[index] = result
                self.emit(("success", index, result, elapsed, False))
                self.emit(("attempt_complete", elapsed))
            except ConsensusRejected as exc:
                print(f"{path.name}: {exc} -> full alignment")
                self.emit(("stage", index, "Geometry differs - running full alignment", "consensus"))
                self.standard(index, priors, phase="initial")
            except Exception as exc:
                elapsed = time.perf_counter() - started
                self.pending_retry.append({
                    "index": index,
                    "path": path,
                    "first_error": short_error(exc),
                    "traceback": traceback.format_exc(),
                })
                self.emit(("retry_queued", index, short_error(exc), elapsed))
                self.emit(("attempt_complete", elapsed))

    def run_retry(self):
        if not self.pending_retry or self.stopped():
            return
        self.emit(("retry_begin", len(self.pending_retry)))

        for item in self.pending_retry:
            if self.stopped():
                return
            index = item["index"]
            priors = build_alignment_priors(
                nearest_success_alignments(self.success, index, self.history),
                max_samples=self.history,
            )
            if not priors:
                self.emit((
                    "final_error",
                    index,
                    "No successful neighbouring alignment was available for a history-guided retry.",
                    0.0,
                ))
                continue

            self.emit(("file_start", index, item["path"].name, "retry"))
            started = time.perf_counter()
            try:
                result = process_single_file(
                    item["path"],
                    alignment_priors=priors,
                    reuse_split=True,
                    status_callback=self._status(index, "retry"),
                    settings=self.settings,
                )
                elapsed = time.perf_counter() - started
                self.success[index] = result
                self.emit(("success", index, result, elapsed, True))
            except Exception as exc:
                elapsed = time.perf_counter() - started
                self.emit((
                    "final_error",
                    index,
                    f"First pass: {item['first_error']} | History-guided retry: {short_error(exc)}",
                    elapsed,
                ))
            self.emit(("attempt_complete", elapsed))


def use_batch_mode(count: int, settings: dict) -> bool:
    return count >= int(settings["batch_threshold"])


def run_pipeline(file_paths, emit, stop_event=None, settings=None):
    """Process all files (in the given order). Returns (success_records, failed_indices)."""
    settings = settings or load_settings()
    run = _Run(file_paths, emit, stop_event, settings)
    total = len(run.paths)

    if use_batch_mode(total, settings):
        emit(("mode", "Batch consensus mode"))
        run.run_batch()
    else:
        emit(("mode", "Per-image alignment"))
        run.run_standard(range(total))

    run.run_retry()

    failed = [i for i in range(total) if i not in run.success]
    emit(("cancelled" if run.stopped() else "done", run.success, failed))
    return run.success, failed
