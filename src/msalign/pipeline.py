"""
pipeline.py

Processing of one image and of a whole selection, without any GUI code.

The GUI and the command line call run_pipeline(); progress is reported as event
tuples through `emit` (see _Run for the list).
"""

from __future__ import annotations

import csv
import logging
import os
import re
import shutil
import time
import traceback
from datetime import datetime
from pathlib import Path

from .batch import (
    calibration_indices,
    check_verification,
    compute_consensus,
    consensus_alignment_result,
    response_thresholds,
    verify_shifts,
)
from .closure import closure_failed, describe_failures
from .image_io import (
    STANDARD_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    find_split_set,
    is_raw_file,
    read_image,
    split_ext_for_input,
)
from .layout import layout_from_settings
from .matching import PreparedImages
from .settings import file_bands_int, load_settings
from .metadata import read_source_exif
from .split import load_split_info, save_split_cells, split_compound_array
from .stack import (
    build_alignment_priors,
    export_aligned_tiff,
    export_multipage_tiff_from_split,
    load_split_images,
    run_closure_check,
)


log = logging.getLogger(__name__)

RUN_DIR_NAME = "msalign_runs"


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


def expand_inputs(paths) -> list[Path]:
    """Files are kept, folders are expanded with collect_folder_images; result is sorted."""
    out = []
    for p in map(Path, paths):
        if p.is_dir():
            out.extend(collect_folder_images(p))
        else:
            out.append(p)
    return sort_paths(dict.fromkeys(out))


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
    summary = quality_report.get("summary", {})
    alignment = export_result["alignment"]

    log.info("TIFF written: %s", paths["tiff"])
    if collection_error:
        log.warning("Shared TIFF copy failed: %s", collection_error)

    return {
        "input": str(input_path),
        "output_dir": str(paths["base"]),
        "tiff": str(paths["tiff"]),
        "tiff_collection_copy": str(collection_path or ""),
        "tiff_collection_copy_ok": collection_error is None,
        "tiff_collection_copy_error": collection_error or "",
        "quality_report": quality_report.get("txt", ""),
        "quality": summary.get("overall_quality", "not_available"),
        "closure_status": summary.get("closure_status", "not_checked"),
        "closure_max_residual": summary.get("closure_max_residual"),
        "alignment": alignment,
        "split_info": export_result.get("split_info", {}),
        "output": export_result.get("output", {}),
        "metadata_json": export_result.get("metadata_json", ""),
        "history_retry_used": bool(alignment.get("history_retry_used", False)),
        "batch_consensus_used": alignment.get("mode") == "batch_consensus",
    }


def process_single_file(
    input_path: Path,
    alignment_priors=None,
    reuse_split: bool = False,
    status_callback=None,
    settings=None,
):
    """Full per-image processing: split, search alignment, check, export, report."""
    settings = settings or load_settings()
    layout = layout_from_settings(settings)
    input_path = Path(input_path)
    paths = output_paths(input_path)
    split_ext = split_ext_for_input(input_path)

    log.info("=== Processing: %s ===", input_path.name)

    images = None
    split_info = {}
    if reuse_split and find_split_set(paths["split_dir"], split_ext, layout.count):
        if status_callback:
            status_callback("Using existing lens images")
        split_info = load_split_info(paths["split_dir"])
    else:
        if status_callback:
            status_callback(f"Splitting compound image into {layout.count} lens images")
        images, split_info = split_compound_array(read_image(input_path), **_split_kwargs(settings))
        save_split_cells(images, paths["split_dir"], split_ext, settings["jpeg_quality"], info=split_info)

    if images is None:
        images = load_split_images(paths["split_dir"], split_ext, layout.count)

    export_result = export_multipage_tiff_from_split(
        split_dir=paths["split_dir"],
        output_tiff=paths["tiff"],
        alignment_priors=alignment_priors,
        status_callback=status_callback,
        images=images,
        file_bands=file_bands_int(settings),
        output_order=settings["output_order"],
        split_info=split_info,
        settings=settings,
        source_path=input_path,
        exif=read_source_exif(input_path),
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
    this image (plus the closure check) and export with it.
    Raises ConsensusRejected if not confirmed.
    """
    settings = settings or load_settings()
    layout = layout_from_settings(settings)
    input_path = Path(input_path)
    paths = output_paths(input_path)

    log.info("=== Processing (batch consensus): %s ===", input_path.name)

    if status_callback:
        status_callback(f"Splitting compound image into {layout.count} lens images")
    images, split_info = split_compound_array(read_image(input_path), **_split_kwargs(settings))

    if status_callback:
        status_callback("Verifying batch lens geometry on this image")
    prepared = PreparedImages(images)
    try:
        verification = verify_shifts(
            images,
            consensus["shifts"],
            settings["batch_verify_max_residual_px"],
            reference=consensus["reference"],
            prepared=prepared,
        )
    except ValueError as exc:
        raise ConsensusRejected(str(exc)) from exc
    ok, reason = check_verification(verification, thresholds, settings)
    if not ok:
        raise ConsensusRejected(reason)

    alignment = consensus_alignment_result(consensus, verification)
    rows = run_closure_check(images, alignment, layout, settings, prepared)
    if settings["closure_check"] == "reject" and closure_failed(rows):
        raise ConsensusRejected(describe_failures(rows))

    if settings["batch_keep_split_images"]:
        save_split_cells(
            images, paths["split_dir"], split_ext_for_input(input_path), settings["jpeg_quality"],
            info=split_info,
        )

    export_result = export_aligned_tiff(
        images,
        alignment,
        paths["tiff"],
        status_callback=status_callback,
        file_bands=file_bands_int(settings),
        output_order=settings["output_order"],
        split_info=split_info,
        settings=settings,
        source_path=input_path,
        exif=read_source_exif(input_path),
    )
    return _finish(input_path, paths, export_result, settings, status_callback)


# ---------------------------------------------------------------------------
# Run files: log + summary CSV
# ---------------------------------------------------------------------------

SUMMARY_FIELDS = [
    "index", "file", "status", "mode", "quality", "closure", "closure_max_residual_px",
    "batch_consensus", "history_retry", "second_pass", "seconds",
    "width", "height", "origin_x", "origin_y", "padded_px", "tiff", "error",
]


class RunFiles:
    """
    <first input folder>/msalign_runs/<timestamp>.log and <timestamp>_summary.csv.
    The summary is written row by row, so it is useful even if a run is stopped.
    """

    def __init__(self, folder: Path, verbose: bool = False):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.dir = Path(folder) / RUN_DIR_NAME
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.dir / f"{stamp}.log"
        self.summary_path = self.dir / f"{stamp}_summary.csv"

        self.handler = logging.FileHandler(self.log_path, encoding="utf-8")
        self.handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        self.handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root = logging.getLogger("msalign")
        root.addHandler(self.handler)
        if root.level == logging.NOTSET or root.level > self.handler.level:
            root.setLevel(self.handler.level)

        self._file = self.summary_path.open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=SUMMARY_FIELDS)
        self._writer.writeheader()
        self._file.flush()

    def row(self, **values):
        self._writer.writerow({k: values.get(k, "") for k in SUMMARY_FIELDS})
        self._file.flush()

    def close(self):
        self._file.close()
        logging.getLogger("msalign").removeHandler(self.handler)
        self.handler.close()


# ---------------------------------------------------------------------------
# Batch run
# ---------------------------------------------------------------------------

def nearest_success_alignments(success_records, index, limit):
    """Successful neighbours on both sides, nearest first."""
    ordered = sorted(success_records, key=lambda i: (abs(i - index), i))[:limit]
    return [success_records[i]["alignment"] for i in ordered]


class _Run:
    """
    Shared state of one run.

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
        ("run_files", log_path, summary_path)   emitted by run_pipeline
    """

    def __init__(self, file_paths, emit, stop_event, settings, run_files=None):
        self.paths = [Path(p) for p in file_paths]
        self._emit = emit
        self.stop_event = stop_event
        self.settings = settings
        self.layout = layout_from_settings(settings)
        self.run_files = run_files
        self.success = {}
        self.errored = set()
        self.pending_retry = []
        self.history = int(settings["history_length"])

    # --- events + summary ---------------------------------------------------

    def emit(self, event):
        if self.run_files is not None:
            kind = event[0]
            if kind == "success":
                _, index, result, elapsed, recovered = event
                out = result.get("output") or {}
                self.run_files.row(
                    index=index + 1, file=self.paths[index].name, status="success",
                    mode=result["alignment"].get("mode", ""), quality=result.get("quality", ""),
                    closure=result.get("closure_status", ""),
                    closure_max_residual_px=result.get("closure_max_residual") or "",
                    batch_consensus=result.get("batch_consensus_used", False),
                    history_retry=result.get("history_retry_used", False),
                    second_pass=recovered, seconds=round(elapsed, 1), tiff=result.get("tiff", ""),
                    width=out.get("width", ""), height=out.get("height", ""),
                    origin_x=(out.get("origin_in_reference_lens_px") or ["", ""])[0],
                    origin_y=(out.get("origin_in_reference_lens_px") or ["", ""])[1],
                    padded_px=out.get("padded_pixels", ""),
                )
            elif kind == "final_error":
                _, index, error, elapsed = event
                self.errored.add(index)
                self.run_files.row(
                    index=index + 1, file=self.paths[index].name, status="error",
                    seconds=round(elapsed, 1), error=error,
                )
        self._emit(event)

    def stopped(self):
        return self.stop_event is not None and self.stop_event.is_set()

    def _status(self, index, phase):
        return lambda stage: self.emit(("stage", index, stage, phase))

    def priors(self, alignments, max_samples=None):
        return build_alignment_priors(
            alignments, max_samples=max_samples or self.history, layout=self.layout
        )

    # --- steps --------------------------------------------------------------

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
            log.info("%s: first pass failed: %s", path.name, exc)
            log.debug(traceback.format_exc())
            self.pending_retry.append({
                "index": index,
                "path": path,
                "first_error": short_error(exc),
            })
            self.emit(("retry_queued", index, short_error(exc), elapsed))
        self.emit(("attempt_complete", elapsed))

    def previous_priors(self, index):
        previous = sorted(i for i in self.success if i < index)[-self.history:]
        return self.priors([self.success[i]["alignment"] for i in previous])

    def run_standard(self, indices):
        for index in indices:
            if self.stopped():
                return
            self.standard(index, self.previous_priors(index))

    def _calibration_thresholds(self, agreeing, consensus):
        """Verification correlation on calibration images at their own known-good shifts."""
        s = self.settings
        ref = consensus["reference"]
        verifications = []
        for i in agreeing:
            src = self.paths[i]
            try:
                images = load_split_images(
                    output_paths(src)["split_dir"], split_ext_for_input(src), self.layout.count
                )
                own = self.success[i]["alignment"]["apply_shifts"]
                verifications.append(
                    verify_shifts(images, own, s["batch_verify_max_residual_px"], reference=ref)
                )
            except (OSError, ValueError) as exc:
                log.debug("calibration image %s skipped for thresholds: %s", src.name, exc)
        return response_thresholds(verifications, s)

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
        consensus = compute_consensus(
            [self.success[i]["alignment"] for i in calib_ok], s,
            reference=self.layout.primary_reference,
        )
        calib_set = set(calib)
        rest = [i for i in range(total) if i not in calib_set]

        if not consensus["consistent"]:
            log.info("No batch consensus: %s", consensus["reason"])
            self.emit(("phase", f"No common geometry ({consensus['reason']}); aligning each image"))
            self.run_standard(rest)
            return

        agreeing = [i for i, ok in zip(calib_ok, consensus["agreeing_mask"]) if ok]
        thresholds = self._calibration_thresholds(agreeing, consensus)
        priors = self.priors([self.success[i]["alignment"] for i in agreeing], len(agreeing))

        log.info("Batch consensus: %s; shifts %s", consensus["reason"], consensus["shifts"])
        log.info("Verification correlation thresholds: %s", thresholds)
        self.emit(("phase", f"Applying batch geometry ({consensus['reason']})"))

        for index in rest:
            if self.stopped():
                return
            path = self.paths[index]
            self.emit(("file_start", index, path.name, "consensus"))
            started = time.perf_counter()
            try:
                result = process_with_consensus(
                    path, consensus, thresholds,
                    status_callback=self._status(index, "consensus"),
                    settings=s,
                )
                elapsed = time.perf_counter() - started
                self.success[index] = result
                self.emit(("success", index, result, elapsed, False))
                self.emit(("attempt_complete", elapsed))
            except ConsensusRejected as exc:
                log.info("%s: %s -> full alignment", path.name, exc)
                self.emit(("stage", index, "Geometry differs - running full alignment", "consensus"))
                self.standard(index, priors, phase="initial")
            except Exception as exc:
                elapsed = time.perf_counter() - started
                log.info("%s: failed: %s", path.name, exc)
                log.debug(traceback.format_exc())
                self.pending_retry.append({
                    "index": index,
                    "path": path,
                    "first_error": short_error(exc),
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
            priors = self.priors(nearest_success_alignments(self.success, index, self.history))
            if not priors:
                self.emit((
                    "final_error",
                    index,
                    f"First pass: {item['first_error']} | no successful neighbouring image "
                    "for a history-guided retry",
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


def run_pipeline(file_paths, emit=None, stop_event=None, settings=None,
                 run_files: bool = True, verbose: bool = False):
    """
    Process all files in the given order. Returns (success_records, failed_indices).

    With run_files=True, a log and a summary CSV are written to
    <first input folder>/msalign_runs/.
    """
    settings = settings or load_settings()
    emit = emit or (lambda event: None)
    paths = [Path(p) for p in file_paths]

    files = RunFiles(paths[0].parent, verbose) if (run_files and paths) else None
    run = _Run(paths, emit, stop_event, settings, files)
    total = len(paths)

    try:
        if files is not None:
            emit(("run_files", str(files.log_path), str(files.summary_path)))
        log.info("msalign run: %d image(s), settings from %s", total, settings.get("_source", "?"))
        if use_batch_mode(total, settings):
            emit(("mode", "Batch consensus mode"))
            run.run_batch()
        else:
            emit(("mode", "Per-image alignment"))
            run.run_standard(range(total))

        run.run_retry()

        failed = [i for i in range(total) if i not in run.success]
        if files is not None and run.stopped():
            for i in failed:
                if i not in run.errored:
                    files.row(index=i + 1, file=paths[i].name, status="not processed")
        log.info("Finished: %d successful, %d failed", len(run.success), len(failed))
    finally:
        if files is not None:
            files.close()

    emit(("cancelled" if run.stopped() else "done", run.success, failed))
    return run.success, failed
