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
from .exports import RIG_DIR_NAME, write_rig_helpers, write_rig_images
from .metadata import read_source_exif
from .split import load_split_info, save_split_cells, split_compound_array
from .stack import (
    build_band_map,
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

    exif = read_source_exif(input_path)
    images = None
    split_info = {}
    if reuse_split and find_split_set(paths["split_dir"], split_ext, layout.count):
        if status_callback:
            status_callback("Using existing lens images")
        split_info = load_split_info(paths["split_dir"])
    else:
        if status_callback:
            status_callback(f"Splitting compound image into {layout.count} lens images")
        images, split_info = split_compound_array(read_image(input_path, settings["raw_demosaic"]), **_split_kwargs(settings))
        save_split_cells(images, paths["split_dir"], split_ext, settings["jpeg_quality"], info=split_info)
        _export_rig(images, input_path, settings, exif, status_callback)

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
        exif=exif,
    )
    return _finish(input_path, paths, export_result, settings, status_callback)


def _export_rig(images, input_path, settings, exif, status_callback=None):
    """Unaligned single-band lens images for photogrammetry (metashape_export)."""
    if not settings["metashape_export"]:
        return
    if status_callback:
        status_callback("Writing lens images for photogrammetry")
    write_rig_images(
        images, build_band_map(file_bands_int(settings)), settings["output_order"], settings,
        input_path, output_name(input_path), exif,
    )


def nearest_band(output_order, target=550.0):
    from .metadata import wavelength_nm

    labelled = [(abs(wavelength_nm(b) - target), b) for b in output_order if wavelength_nm(b) is not None]
    return min(labelled)[1] if labelled else output_order[0]


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
    images, split_info = split_compound_array(read_image(input_path, settings["raw_demosaic"]), **_split_kwargs(settings))
    exif = read_source_exif(input_path)
    _export_rig(images, input_path, settings, exif, status_callback)

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
        exif=exif,
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

    def __init__(self, file_paths, emit, stop_event, settings, run_files=None, workers=1, verbose=False):
        self.paths = [Path(p) for p in file_paths]
        self.workers = workers
        self.verbose = verbose
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

    # --- task execution -----------------------------------------------------
    #
    # Every image of a phase is a task. With one worker, tasks run here in
    # order, exactly as before (history priors from the previous images).
    # With several workers, tasks run in separate processes; the first pass
    # then uses no history priors, so results do not depend on which image
    # happens to finish first. History is used in the second pass, which
    # starts only after the first pass is complete.

    def parallel(self):
        return self.workers > 1

    def _run_tasks(self, tasks):
        """
        tasks: list of dicts with index, phase, kind ("standard" | "consensus" |
        "retry"), and the arguments of _worker. "priors" may be a callable
        (evaluated just before the task runs, sequential mode only).
        """
        if not tasks:
            return
        if not self.parallel():
            for task in tasks:
                if self.stopped():
                    return
                if callable(task.get("priors")):
                    task["priors"] = task["priors"]()
                self.emit(("file_start", task["index"], self.paths[task["index"]].name, task["phase"]))
                outcome = _worker(task, status_callback=self._status(task["index"], task["phase"]))
                self._handle(task, outcome)
            return

        import concurrent.futures as cf
        import multiprocessing
        from concurrent.futures.process import BrokenProcessPool

        context = multiprocessing.get_context("spawn")
        queue = list(tasks)
        running = {}
        broken = False
        pool = cf.ProcessPoolExecutor(max_workers=self.workers, mp_context=context)
        try:
            def submit():
                while queue and len(running) < self.workers and not self.stopped():
                    task = queue[0]
                    if callable(task.get("priors")):
                        task["priors"] = None
                    task["capture_log"] = True
                    task["verbose"] = self.verbose
                    future = pool.submit(_worker, task)  # may raise BrokenProcessPool
                    queue.pop(0)
                    self.emit(("file_start", task["index"], self.paths[task["index"]].name, task["phase"]))
                    running[future] = task

            submit()
            while running:
                done, _ = cf.wait(running, return_when=cf.FIRST_COMPLETED)
                for future in done:
                    try:
                        outcome = future.result()
                    except BrokenProcessPool:
                        raise  # the task stays in `running` and is redone below
                    except Exception as exc:
                        outcome = {"ok": False, "error": f"worker failed: {short_error(exc)}",
                                   "elapsed": 0.0, "log": ""}
                    task = running.pop(future)
                    self._handle(task, outcome)
                if self.stopped():
                    queue.clear()
                submit()
        except BrokenProcessPool:
            # A worker process died (for example out of memory). Finish the
            # remaining images one after the other in this process.
            broken = True
            unfinished = sorted(running.values(), key=lambda t: t["index"]) + queue
            log.warning(
                "A worker process stopped unexpectedly; processing the remaining %d image(s) "
                "one at a time.", len(unfinished),
            )
            self.emit(("phase", "A worker process stopped; continuing one image at a time"))
        finally:
            pool.shutdown(wait=not broken, cancel_futures=True)

        if broken:
            for task in unfinished:
                task.pop("capture_log", None)
            workers, self.workers = self.workers, 1
            try:
                self._run_tasks(unfinished)
            finally:
                self.workers = workers

    def _handle(self, task, outcome):
        index, phase = task["index"], task["phase"]
        path = self.paths[index]
        if outcome.get("log"):
            for line in outcome["log"].rstrip().splitlines():
                log.info("[%s] %s", path.name, line)
        if outcome.get("consensus_rejected"):
            log.info("%s: %s -> full alignment", path.name, outcome["consensus_rejected"])
            self.emit(("stage", index, "Geometry differs - ran full alignment", phase))
        elapsed = outcome["elapsed"]

        if outcome["ok"]:
            self.success[index] = outcome["result"]
            self.emit(("success", index, outcome["result"], elapsed, phase == "retry"))
        elif phase == "retry":
            self.emit((
                "final_error", index,
                f"First pass: {task['first_error']} | History-guided retry: {outcome['error']}",
                elapsed,
            ))
        else:
            log.info("%s: first pass failed: %s", path.name, outcome["error"])
            self.pending_retry.append({"index": index, "path": path, "first_error": outcome["error"]})
            self.emit(("retry_queued", index, outcome["error"], elapsed))
        self.emit(("attempt_complete", elapsed))

    def _standard_task(self, index, phase, priors):
        return {
            "index": index, "phase": phase, "kind": "standard",
            "path": str(self.paths[index]), "priors": priors, "settings": self.settings,
        }

    # --- phases -------------------------------------------------------------

    def previous_priors(self, index):
        previous = sorted(i for i in self.success if i < index)[-self.history:]
        return self.priors([self.success[i]["alignment"] for i in previous])

    def run_standard(self, indices, phase="initial"):
        self._run_tasks([
            self._standard_task(i, phase, lambda i=i: self.previous_priors(i)) for i in indices
        ])

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
        self.run_standard(calib, phase="calibration")
        if self.stopped():
            return

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

        self._run_tasks([
            {
                "index": i, "phase": "consensus", "kind": "consensus",
                "path": str(self.paths[i]), "settings": s,
                "consensus": consensus, "thresholds": thresholds, "priors": priors,
            }
            for i in rest
        ])

    def run_retry(self):
        if not self.pending_retry or self.stopped():
            return
        self.emit(("retry_begin", len(self.pending_retry)))

        tasks = []
        for item in self.pending_retry:
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
            task = self._standard_task(index, "retry", priors)
            task.update({"kind": "retry", "first_error": item["first_error"]})
            tasks.append(task)
        self._run_tasks(tasks)


def _worker(task, status_callback=None):
    """
    Process one image. Runs in the main process (sequential mode) or in a
    worker process; returns a picklable outcome dict and never raises.
    """
    import io

    started = time.perf_counter()
    capture = None
    if task.get("capture_log"):
        capture = io.StringIO()
        handler = logging.StreamHandler(capture)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        level = logging.DEBUG if task.get("verbose") else logging.INFO
        handler.setLevel(level)
        root = logging.getLogger("msalign")
        root.addHandler(handler)
        root.setLevel(level)

    outcome = {"ok": False}
    try:
        path = Path(task["path"])
        settings = task["settings"]
        if task["kind"] == "consensus":
            try:
                result = process_with_consensus(
                    path, task["consensus"], task["thresholds"],
                    status_callback=status_callback, settings=settings,
                )
            except ConsensusRejected as exc:
                outcome["consensus_rejected"] = str(exc)
                if status_callback:
                    status_callback("Geometry differs - running full alignment")
                result = process_single_file(
                    path, alignment_priors=task["priors"],
                    status_callback=status_callback, settings=settings,
                )
        else:
            result = process_single_file(
                path,
                alignment_priors=task.get("priors"),
                reuse_split=task["kind"] == "retry",
                status_callback=status_callback,
                settings=settings,
            )
        outcome.update(ok=True, result=result)
    except Exception as exc:
        log.debug(traceback.format_exc())
        outcome.update(ok=False, error=short_error(exc))
    finally:
        outcome["elapsed"] = time.perf_counter() - started
        if capture is not None:
            logging.getLogger("msalign").removeHandler(handler)
            outcome["log"] = capture.getvalue()
    return outcome


def resolve_workers(setting, count: int, raw_input: bool = True) -> int:
    """
    Number of worker processes. "auto": one per CPU core (one core left free),
    limited by available memory (about 1.5 GB per RAW image, 0.6 GB per JPG/TIFF)
    and by the number of images.
    """
    if count < 2:
        return 1
    if setting != "auto":
        return max(1, min(int(setting), count))
    cores = max(1, (os.cpu_count() or 2) - 1)
    per_image = 1.5 if raw_input else 0.6
    memory = available_memory_gb()
    by_memory = max(1, int(memory / per_image)) if memory else cores
    return max(1, min(cores, by_memory, 8, count))


def available_memory_gb():
    """Available physical memory in GB, or None if it cannot be determined."""
    try:
        if os.name == "nt":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return stat.ullAvailPhys / 1024 ** 3
        meminfo = Path("/proc/meminfo")
        if meminfo.exists():
            for line in meminfo.read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024 ** 2
        pages, size = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
        return pages * size / 1024 ** 3 * 0.5
    except (OSError, ValueError, AttributeError):
        return None


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
    workers = resolve_workers(settings["workers"], len(paths), any(is_raw_file(p) for p in paths))
    run = _Run(paths, emit, stop_event, settings, files, workers=workers, verbose=verbose)
    total = len(paths)

    try:
        if files is not None:
            emit(("run_files", str(files.log_path), str(files.summary_path)))
        log.info("msalign run: %d image(s), %d worker process(es), settings from %s",
                 total, workers, settings.get("_source", "?"))
        emit(("workers", workers))
        if settings["metashape_export"] and paths:
            write_rig_helpers(paths[0].parent / RIG_DIR_NAME, settings,
                              master_label=nearest_band(settings["output_order"]))
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
