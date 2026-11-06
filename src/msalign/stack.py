"""
stack.py

Aligns the lens images of one compound image and writes the multiband TIFF.

Alignment strategy:

Primary
    Every lens is aligned directly to the primary reference lens (lens 1 by
    default) with phase correlation + patch consensus.

Fallback (Plan B / Plan C)
    If any primary pair fails, the whole image switches to a chain of short,
    adjacent-lens steps through the fallback reference lens (see layout.py),
    e.g. for the 3 x 2 camera: 2->1, 2->3, 2->5, 5->4, 5->6. Each step tries
    directional patch consensus, then AKAZE feature translation, then (if
    history is available) a history-guided residual retry.

Closure check
    After either mode, adjacent lens pairs that were not measured directly are
    checked on the image (closure.py). Depending on `closure_check`, a failed
    check rejects the result ("reject") or is only reported ("warn").
"""

from pathlib import Path
import csv
import logging

import cv2
import numpy as np
import tifffile

from .alignment import estimate_xy_v4, DEFAULT_CONFIG
from .closure import check_closure, closure_failed, describe_failures
from .fallback import estimate_xy_plan_b
from .image_io import find_split_set
from .layout import LensLayout, layout_from_settings
from .matching import PreparedImages
from .metadata import build_metadata, tiff_tags, write_sidecar
from . import parallax
from .parallax import _crop as parallax_crop
from .settings import DEFAULT_SETTINGS, file_bands_int


log = logging.getLogger(__name__)

# Defaults; the pipeline passes the values from settings (pipeline_config.json).
FILE_BANDS = file_bands_int(DEFAULT_SETTINGS)
OUTPUT_ORDER = list(DEFAULT_SETTINGS["output_order"])


def _layout(settings=None) -> LensLayout:
    return layout_from_settings(settings or DEFAULT_SETTINGS)


def load_split_images(split_dir: Path, split_ext: str | None = None, count: int = 6):
    ext = find_split_set(split_dir, split_ext, count)
    if ext is None:
        wanted = split_ext or ".tif / .jpg"
        raise FileNotFoundError(f"No complete split set (1..{count}{wanted}) in: {split_dir}")

    images = {}
    for idx in range(1, count + 1):
        path = split_dir / f"{idx}{ext}"
        data = np.fromfile(str(path), dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
        if img is None:
            raise FileNotFoundError(f"Could not load split image: {path}")
        images[idx] = img
    return images


def _pair_record(result, raw_dx, raw_dy, method, algorithm, reason, history=False, details=None):
    return {
        "raw_dx": int(raw_dx),
        "raw_dy": int(raw_dy),
        "apply_dx": -int(raw_dx),
        "apply_dy": -int(raw_dy),
        "confidence": float(result.get("confidence", result.get("score", 0.0))),
        "support": int(result.get("support", 0)),
        "num_votes": int(result.get("num_votes", 0)),
        "reason": reason,
        "method": method,
        "algorithm": algorithm,
        "history_prior_used": history,
        "details": details or {},
    }


def estimate_pair_primary(ref_img, mov_img, config=None):
    result = estimate_xy_v4(ref_img, mov_img, config=config)

    if result is None or not result["accepted"]:
        reason = "none" if result is None else result["reason"]
        raise RuntimeError(reason)

    return _pair_record(
        result, result["dx"], result["dy"],
        method="primary", algorithm="phase_patch_consensus", reason=str(result["reason"]),
    )


def estimate_pair_fallback(ref_img, mov_img, pair, search_window, prior_shift=None):
    result = estimate_xy_plan_b(
        ref_img,
        mov_img,
        search_window=search_window,
        config=None,
        prior_shift=prior_shift,
    )

    if result is None or not result.get("accepted"):
        if result is None:
            reason = "no alignment result"
        else:
            reason = result.get("reason", f"{result.get('method', 'unknown')} rejected")
        raise RuntimeError(f"{pair[0]}->{pair[1]}: {reason}")

    details = result.get("details", {}) or {}
    algorithm = str(result.get("method", "plan_b"))
    history = algorithm.startswith("history_prior_")
    record = _pair_record(
        result, result["dx"], result["dy"],
        method="history_retry" if history else "plan_b",
        algorithm=algorithm,
        reason=f"fallback:{algorithm}",
        history=history,
        details=details,
    )
    record["num_votes"] = int(details.get("num_votes", details.get("matches", result.get("support", 0))))
    return record


def compute_primary_alignment(images, config=None, layout: LensLayout | None = None):
    """Every lens aligned directly to the primary reference lens."""
    layout = layout or _layout()
    ref = layout.primary_reference

    pairs = {}
    apply_shifts = {ref: {"dx": 0, "dy": 0}}
    for idx in layout.lenses:
        if idx == ref:
            continue
        pair = estimate_pair_primary(images[ref], images[idx], config=config)
        pairs[f"{ref}-{idx}"] = pair
        apply_shifts[idx] = {"dx": pair["apply_dx"], "dy": pair["apply_dy"]}

    return {
        "reference_id": ref,
        "mode": f"primary_direct_{ref}_reference",
        "pairs": pairs,
        "measured_edges": [(ref, idx) for idx in layout.lenses if idx != ref],
        "apply_shifts": apply_shifts,
    }


def compute_plan_b_alignment(images, alignment_priors=None, layout: LensLayout | None = None,
                             settings=None):
    """Chain of adjacent-lens steps through the fallback reference lens."""
    layout = layout or _layout(settings)
    settings = settings or DEFAULT_SETTINGS
    alignment_priors = alignment_priors or {}
    ref = layout.fallback_reference

    pairs = {}
    apply_shifts = {ref: {"dx": 0, "dy": 0}}
    for parent, child in layout.fallback_tree:
        window = layout.search_window(
            parent, child, settings["parallax_range_px"], settings["parallax_cross_px"]
        )
        pair = estimate_pair_fallback(
            images[parent], images[child], (parent, child), window,
            prior_shift=alignment_priors.get((parent, child)),
        )
        pairs[f"{parent}-{child}"] = pair
        apply_shifts[child] = {
            "dx": apply_shifts[parent]["dx"] + pair["apply_dx"],
            "dy": apply_shifts[parent]["dy"] + pair["apply_dy"],
        }

    return {
        "reference_id": ref,
        "mode": f"fallback_chain_{ref}_reference",
        "pairs": pairs,
        "measured_edges": list(layout.fallback_tree),
        "apply_shifts": apply_shifts,
        "history_priors_available": len(alignment_priors),
        "history_retry_used": any(p.get("history_prior_used", False) for p in pairs.values()),
    }


def derive_history_pair_shifts(alignment_result, edges):
    """
    Raw relative shifts for the fallback chain's lens pairs, derived from any
    successful alignment mode, so primary-mode successes also teach the chain.
    raw(ref->mov) = apply_ref - apply_mov
    """
    apply_shifts = alignment_result.get("apply_shifts", {})
    pair_shifts = {}
    for ref_id, mov_id in edges:
        if ref_id in apply_shifts and mov_id in apply_shifts:
            pair_shifts[(ref_id, mov_id)] = {
                "dx": int(apply_shifts[ref_id]["dx"] - apply_shifts[mov_id]["dx"]),
                "dy": int(apply_shifts[ref_id]["dy"] - apply_shifts[mov_id]["dy"]),
            }
    return pair_shifts


def _robust_prior(values):
    """Return a robust median/MAD prior while rejecting obvious history outliers."""
    if not values:
        return None

    dxs = np.asarray([v["dx"] for v in values], dtype=np.float32)
    dys = np.asarray([v["dy"] for v in values], dtype=np.float32)

    med_dx = float(np.median(dxs))
    med_dy = float(np.median(dys))
    mad_dx = float(np.median(np.abs(dxs - med_dx)))
    mad_dy = float(np.median(np.abs(dys - med_dy)))

    # At least 12 px is allowed because close-range parallax can alter the best
    # translation slightly even though the physical lens geometry is fixed.
    tol_dx = max(12.0, 4.0 * 1.4826 * mad_dx)
    tol_dy = max(12.0, 4.0 * 1.4826 * mad_dy)
    keep = (np.abs(dxs - med_dx) <= tol_dx) & (np.abs(dys - med_dy) <= tol_dy)

    filtered_dx = dxs[keep] if np.any(keep) else dxs
    filtered_dy = dys[keep] if np.any(keep) else dys

    final_dx = float(np.median(filtered_dx))
    final_dy = float(np.median(filtered_dy))

    return {
        "dx": int(round(final_dx)),
        "dy": int(round(final_dy)),
        "samples": int(len(filtered_dx)),
        "source_samples": int(len(values)),
        "mad_dx": float(np.median(np.abs(filtered_dx - final_dx))),
        "mad_dy": float(np.median(np.abs(filtered_dy - final_dy))),
    }


def build_alignment_priors(alignment_results, max_samples=5, layout: LensLayout | None = None):
    """
    Pair-specific priors from the given successful alignments (the caller
    chooses which neighbours); robust aggregation only.
    """
    if not alignment_results:
        return {}

    edges = (layout or _layout()).fallback_tree
    buckets = {pair: [] for pair in edges}
    for alignment in list(alignment_results)[-max_samples:]:
        for pair, shift in derive_history_pair_shifts(alignment, edges).items():
            buckets[pair].append(shift)

    priors = {}
    for pair, values in buckets.items():
        prior = _robust_prior(values)
        if prior is not None:
            priors[pair] = prior
    return priors


def run_closure_check(images, result, layout, settings, prepared=None):
    rows = check_closure(
        images, result["apply_shifts"], layout, result["measured_edges"], settings, prepared
    )
    result["closure"] = rows
    if rows:
        log.info(
            "Closure check: %s",
            ", ".join(f"{r['pair']}={r['status']}({r['residual']:.1f}px)" for r in rows),
        )
    return rows


def estimate_alignment_from_split(split_dir: Path, config=None, alignment_priors=None,
                                  split_ext=None, settings=None):
    settings = settings or DEFAULT_SETTINGS
    images = load_split_images(split_dir, split_ext, _layout(settings).count)
    return estimate_alignment_from_images(images, config, alignment_priors, settings)


def estimate_alignment_from_images(images, config=None, alignment_priors=None, settings=None):
    """
    Primary first; if any pair fails (or, with closure_check="reject", the
    closure check fails), switch completely to the fallback chain. Normal
    fallback methods are always attempted before historical priors can
    influence a pair.
    """
    settings = settings or DEFAULT_SETTINGS
    layout = _layout(settings)
    config = config or DEFAULT_CONFIG.copy()
    reject = settings["closure_check"] == "reject"
    prepared = PreparedImages(images)

    try:
        log.info("Trying primary alignment...")
        result = compute_primary_alignment(images, config=config, layout=layout)
        rows = run_closure_check(images, result, layout, settings, prepared)
        if reject and closure_failed(rows):
            raise RuntimeError(describe_failures(rows))
        result["history_priors_available"] = len(alignment_priors or {})
        result["history_retry_used"] = False
        return result
    except Exception as e:
        log.info("Primary alignment not used: %s", e)
        log.info("Switching to fallback chain via lens %s...", layout.fallback_reference)
        if alignment_priors:
            log.info("Historical lens geometry available for %d pair(s).", len(alignment_priors))

    result = compute_plan_b_alignment(images, alignment_priors, layout, settings)
    rows = run_closure_check(images, result, layout, settings, prepared)
    if reject and closure_failed(rows):
        raise RuntimeError(describe_failures(rows))
    return result


def build_band_map(file_bands=None):
    band_map = {}
    rgb_to_bgr = {0: 2, 1: 1, 2: 0}

    for file_id, labels in (file_bands or FILE_BANDS).items():
        for rgb_pos, band_name in enumerate(labels):
            if band_name == "0":
                continue
            band_map[band_name] = (file_id, rgb_to_bgr[rgb_pos])

    return band_map



def classify_alignment_quality(pair_result):
    """Assign a conservative quality label to an accepted alignment pair."""
    confidence = float(pair_result.get("confidence", 0.0))
    support = int(pair_result.get("support", 0))
    algorithm = pair_result.get("algorithm", pair_result.get("method", ""))

    if algorithm in {
        "feature_ransac_translation",
        "history_prior_feature_ransac_translation",
    }:
        if support >= 20:
            return "high"
        if support >= 6:
            return "medium"
        return "low"

    if algorithm == "batch_consensus_verified":
        details = pair_result.get("details", {}) or {}
        residual_mag = float(np.hypot(
            details.get("residual_dx", 0),
            details.get("residual_dy", 0),
        ))
        if residual_mag <= 1.5:
            return "high"
        return "medium"

    if algorithm == "history_prior_cross_method_consensus":
        details = pair_result.get("details", {}) or {}
        agreeing = details.get("agreeing_methods", [])
        prior_samples = int(details.get("prior_samples", 0))
        residual_mag = float(np.hypot(
            details.get("residual_dx", 0),
            details.get("residual_dy", 0),
        ))
        if len(agreeing) >= 3 and prior_samples >= 3 and residual_mag <= 5.0:
            return "high"
        if len(agreeing) >= 2 and prior_samples >= 3 and residual_mag <= 10.0:
            return "medium"
        return "low"

    if confidence >= 0.35 and support >= 4:
        return "high"
    if confidence >= 0.20 and support >= 2:
        return "medium"
    return "low"


def summarize_alignment_quality(alignment_result):
    rows = []

    for pair_name, pair in alignment_result.get("pairs", {}).items():
        details = pair.get("details", {}) or {}
        rows.append({
            "pair": pair_name,
            "method": pair.get("method", ""),
            "algorithm": pair.get("algorithm", ""),
            "quality": classify_alignment_quality(pair),
            "confidence": float(pair.get("confidence", 0.0)),
            "support": int(pair.get("support", 0)),
            "num_votes": int(pair.get("num_votes", 0)),
            "raw_dx": int(pair.get("raw_dx", 0)),
            "raw_dy": int(pair.get("raw_dy", 0)),
            "apply_dx": int(pair.get("apply_dx", 0)),
            "apply_dy": int(pair.get("apply_dy", 0)),
            "history_prior_used": bool(pair.get("history_prior_used", False)),
            "prior_samples": int(details.get("prior_samples", 0)),
            "residual_dx": int(details.get("residual_dx", 0)),
            "residual_dy": int(details.get("residual_dy", 0)),
            "reason": pair.get("reason", ""),
        })

    if not rows:
        return {"overall_quality": "not_available", "mean_confidence": 0.0, "min_support": 0, "rows": rows}

    quality_rank = {"low": 1, "medium": 2, "high": 3}
    worst_rank = min(quality_rank[row["quality"]] for row in rows)

    closure = alignment_result.get("closure", [])
    # A failed closure check that was only reported ("warn") caps quality at low.
    if closure_failed(closure):
        worst_rank = 1

    return {
        "overall_quality": {v: k for k, v in quality_rank.items()}[worst_rank],
        "mean_confidence": sum(row["confidence"] for row in rows) / len(rows),
        "min_support": min(row["support"] for row in rows),
        "rows": rows,
        "closure": closure,
        "closure_max_residual": max((c["residual"] for c in closure if c["status"] != "inconclusive"), default=None),
        "closure_status": (
            "fail" if closure_failed(closure)
            else "pass" if any(c["status"] == "pass" for c in closure)
            else "inconclusive" if closure else "not_checked"
        ),
    }


def write_alignment_quality_report(output_tiff: Path, export_result):
    """Write text and CSV alignment quality reports next to the exported TIFF."""
    alignment = export_result["alignment"]
    summary = summarize_alignment_quality(alignment)

    txt_path = output_tiff.with_name(output_tiff.stem + "_alignment_quality.txt")
    csv_path = output_tiff.with_name(output_tiff.stem + "_alignment_quality.csv")

    lines = [
        "Alignment quality report",
        "========================",
        f"Output TIFF: {output_tiff.name}",
        f"Alignment mode: {alignment.get('mode', '')}",
        f"Reference image ID: {alignment.get('reference_id', '')}",
        f"Overall quality: {summary['overall_quality']}",
        f"Mean confidence: {summary['mean_confidence']:.4f}",
        f"Minimum support: {summary['min_support']}",
    ]

    out = export_result.get("output") or {}
    if out:
        pad = out.get("padded_pixels", 0)
        lines.append(
            f"Output: {out['width']}x{out['height']} px, {out['bands']} bands, pixel grid of lens "
            f"{out['frame_lens']}, window {out['anchor']}, origin {tuple(out['origin_in_reference_lens_px'])} "
            f"in lens {out['frame_lens']}"
            + (f" / {tuple(out['origin_in_source_px'])} in the source image" if out.get("origin_in_source_px") else "")
            + (f", {pad} px padded with 0 (nodata)" if pad else "")
        )
        ca = out["common_area_in_reference_lens_px"]
        lines.append(f"Shared area of all lenses: {ca['width']}x{ca['height']} px at ({ca['x']}, {ca['y']})")

    local = (export_result.get("output") or {}).get("local_alignment") or {}
    if local:
        if "before" in local:
            b, a = local["before"], local["after"]
            lines.append(
                f"Parallax correction: {'applied' if local['parallax_applied'] else 'not applied'} "
                f"({local['parallax_reason']}); local residual worst p90 {b['worst_p90_px']} -> "
                f"{a['worst_p90_px']} px, median {b['median_px']} -> {a['median_px']} px"
            )
        else:
            lines.append(
                f"Local residual (tile check): median {local['median_px']} px, worst lens p90 "
                f"{local['worst_p90_px']} px"
                + ("  -> depth parallax likely; consider parallax_correction"
                   if (local.get("worst_p90_px") or 0) > 6 else "")
            )

    cam = export_result.get("camera") or {}
    if cam:
        lines.append(
            "Camera: " + ", ".join(
                f"{k}={cam[k]}" for k in ("make", "model", "lens", "focal_length_mm", "pixel_size_um_output")
                if k in cam
            )
        )

    split_info = export_result.get("split_info") or {}
    if split_info:
        lines.append(
            f"Source size: {split_info.get('source_width')}x{split_info.get('source_height')} "
            f"(expected {split_info.get('expected_width')}x{split_info.get('expected_height')}), "
            f"split mode: {split_info.get('split_mode')}"
        )

    batch = alignment.get("batch_consensus")
    if batch:
        lines.append(
            f"Batch consensus: learned from {batch.get('agreeing')}/{batch.get('successful')} "
            f"calibration images (tolerance {batch.get('tolerance_px')} px); "
            "shift verified on this image before use"
        )

    lines.append(f"Closure check: {summary['closure_status']}")

    lines += ["", "Per-pair results:"]

    for row in summary["rows"]:
        history_note = ""
        if row["history_prior_used"]:
            history_note = (
                f", history_prior=yes(samples={row['prior_samples']}, "
                f"residual=({row['residual_dx']}, {row['residual_dy']}))"
            )
        lines.append(
            f"- {row['pair']}: quality={row['quality']}, "
            f"method={row['method']}, algorithm={row['algorithm']}, "
            f"confidence={row['confidence']:.4f}, support={row['support']}/{row['num_votes']}, "
            f"raw_shift=({row['raw_dx']}, {row['raw_dy']}), "
            f"applied_shift=({row['apply_dx']}, {row['apply_dy']})"
            f"{history_note}, reason={row['reason']}"
        )

    if summary["closure"]:
        lines += [
            "",
            "Closure check (independent bounded-NCC test of every shift; 'closure' = adjacent",
            "pair not measured by the alignment, 'measured' = pair the alignment measured):",
        ]
        for c in summary["closure"]:
            note = f", {c['note']}" if c.get("note") else ""
            lines.append(
                f"- {c['pair']} ({c['kind']}): {c['status']}, "
                f"implied_shift=({c['predicted_dx']}, {c['predicted_dy']}), "
                f"residual=({c['residual_dx']:.2f}, {c['residual_dy']:.2f}) = {c['residual']:.2f} px, "
                f"ncc={c['ncc']:.3f}{note}"
            )

    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    fieldnames = [
        "pair", "method", "algorithm", "quality", "confidence",
        "support", "num_votes", "raw_dx", "raw_dy", "apply_dx", "apply_dy",
        "history_prior_used", "prior_samples", "residual_dx", "residual_dy", "reason",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        # "2-1" would be turned into a date (2 January) by Excel; "2->1" is not.
        writer.writerows(
            {**row, "pair": row["pair"].replace("-", "->")} for row in summary["rows"]
        )
        for c in summary["closure"]:
            writer.writerow({
                "pair": c["pair"].replace("-", "->"),
                "method": f"{c['kind']}_check",
                "algorithm": "bounded_ncc",
                "quality": c["status"],
                "confidence": c["ncc"],
                "raw_dx": c["predicted_dx"],
                "raw_dy": c["predicted_dy"],
                "residual_dx": c["residual_dx"],
                "residual_dy": c["residual_dy"],
                "reason": c.get("note") or f"residual={c['residual']:.2f}px",
            })

    return {"summary": summary, "txt": str(txt_path), "csv": str(csv_path)}


def export_multipage_tiff_from_split(
    split_dir: Path,
    output_tiff: Path,
    config=None,
    alignment_priors=None,
    status_callback=None,
    split_ext=None,
    images=None,
    file_bands=None,
    output_order=None,
    split_info=None,
    settings=None,
    source_path=None,
    exif=None,
):
    """Align the lens images and export the multiband TIFF.

    `images` may be passed to skip reading the split images from split_dir.
    """
    settings = settings or DEFAULT_SETTINGS
    if images is None:
        images = load_split_images(split_dir, split_ext, _layout(settings).count)

    if status_callback:
        status_callback("Aligning lens images")

    alignment_result = estimate_alignment_from_images(
        images,
        config=config,
        alignment_priors=alignment_priors,
        settings=settings,
    )

    return export_aligned_tiff(
        images,
        alignment_result,
        output_tiff,
        status_callback=status_callback,
        file_bands=file_bands,
        output_order=output_order,
        split_info=split_info,
        settings=settings,
        source_path=source_path,
        exif=exif,
    )


def _local_alignment(images, shifts, band_map, output_order, layout, win, settings, status_callback):
    """Local residual check and, if enabled, depth-dependent parallax correction."""
    if not (settings["parallax_correction"] or settings["local_residual_check"]):
        return {}

    used = {}
    for label in output_order:
        lens, ch = band_map[label]
        used.setdefault(lens, []).append(ch)
    ref = layout.primary_reference
    r0, c0 = layout.position(ref)
    steps = {k: (layout.position(k)[1] - c0, layout.position(k)[0] - r0) for k in used}

    crops = {}
    for k, channels in used.items():
        src = images[k]
        planes = [src[:, :, ch] if src.ndim == 3 else src for ch in channels]
        x = win["x"] - shifts[k]["dx"]
        y = win["y"] - shifts[k]["dy"]
        crops[k] = np.mean(
            [parallax.normalise_crop(parallax_crop(pl, x, y, win["width"], win["height"])) for pl in planes],
            axis=0,
        ).astype(np.float32)

    if not settings["parallax_correction"]:
        residuals = parallax.check_only(crops, settings)
        summary = parallax.summarise(residuals)
        return {"mode": "check", "residuals": _clean_residuals(residuals), **summary}

    if status_callback:
        status_callback("Correcting depth parallax")
    res = parallax.estimate_parallax(crops, steps, parallax.lens_pairs(layout, crops), settings)
    out = {
        "mode": "parallax_correction" if res["applied"] else "check",
        "parallax_applied": res["applied"],
        "parallax_reason": res["reason"],
        "reliable_fraction": res["reliable_fraction"],
        "residuals_before": _clean_residuals(res["before"]),
        "residuals_after": _clean_residuals(res["after"]),
        "before": parallax.summarise(res["before"]),
        "after": parallax.summarise(res["after"]),
    }
    log.info(
        "Parallax correction %s: worst local residual p90 %s -> %s px",
        "applied" if res["applied"] else "not applied",
        out["before"]["worst_p90_px"], out["after"]["worst_p90_px"],
    )
    if res["applied"]:
        out["lens_offset_corrections_px"] = {str(k): [round(v[0], 2), round(v[1], 2)] for k, v in res["offsets"].items()}
        d = res["delta"]
        out["parallax_range_px_per_lens_step"] = [round(float(np.percentile(d, 2)), 2), round(float(np.percentile(d, 98)), 2)]
        out["_correction"] = {"delta": d, "offsets": res["offsets"], "steps": steps}
    return out


def _clean_residuals(residuals):
    return {str(k): {kk: vv for kk, vv in v.items() if kk != "offset"} for k, v in residuals.items()}


def _quality_brief(alignment_result):
    try:
        summary = summarize_alignment_quality(alignment_result)
    except Exception:
        return None
    return {"overall": summary["overall_quality"], "closure_check": summary["closure_status"]}


def write_tiff(path: Path, data, meta, output_order, settings):
    path.parent.mkdir(parents=True, exist_ok=True)
    tags = tiff_tags(meta)
    compression = "zlib" if settings["tiff_compression"] == "deflate" else None
    common = {
        "software": tags["software"],
        "extratags": tags["extratags"],
        "compression": compression,
    }
    if tags["datetime"]:
        common["datetime"] = tags["datetime"]

    if settings["tiff_layout"] == "multiband":
        tifffile.imwrite(
            str(path), data,
            photometric="minisblack", planarconfig="separate",
            description=tags["description"], metadata=None,
            **common,
        )
        return

    # legacy: one page per band; camera tags and band information on page 1
    with tifffile.TiffWriter(str(path)) as tif:
        for i, label in enumerate(output_order):
            extra = common if i == 0 else {"compression": compression}
            tif.write(
                data[i], photometric="minisblack", contiguous=False,
                metadata={"band_name": label}, **extra,
            )


def output_window(shifts, lens_w, lens_h, settings):
    """
    Output window in the pixel grid of the reference lens.

    `shifts` are apply shifts relative to the reference lens. Lens k covers
    [dx_k, lens_w + dx_k) x [dy_k, lens_h + dy_k) of that grid; the common area
    is the intersection over all lenses.
    """
    dxs = [v["dx"] for v in shifts.values()]
    dys = [v["dy"] for v in shifts.values()]
    cx0, cx1 = max(0, max(dxs)), min(lens_w, lens_w + min(dxs))
    cy0, cy1 = max(0, max(dys)), min(lens_h, lens_h + min(dys))
    if cx1 <= cx0 or cy1 <= cy0:
        raise RuntimeError("No common valid area found after alignment.")

    size = settings["output_size_px"]
    if size is None:
        ox, oy, ww, wh = cx0, cy0, cx1 - cx0, cy1 - cy0
        anchor = "common_area"
    else:
        ww, wh = int(size[0]), int(size[1])
        anchor = settings["output_anchor"]
        if anchor == "fixed":
            center = settings["output_fixed_center_px"] or (lens_w / 2, lens_h / 2)
            ox = int(round(center[0] - ww / 2))
            oy = int(round(center[1] - wh / 2))
        else:
            ox = (cx0 + cx1 - ww) // 2
            oy = (cy0 + cy1 - wh) // 2

    padding = {
        "left": max(0, cx0 - ox),
        "right": max(0, ox + ww - cx1),
        "top": max(0, cy0 - oy),
        "bottom": max(0, oy + wh - cy1),
    }
    ix0, ix1 = max(ox, cx0), min(ox + ww, cx1)
    iy0, iy1 = max(oy, cy0), min(oy + wh, cy1)
    covered = max(0, ix1 - ix0) * max(0, iy1 - iy0)

    return {
        "x": ox, "y": oy, "width": ww, "height": wh, "anchor": anchor,
        "common_area": {"x": cx0, "y": cy0, "width": cx1 - cx0, "height": cy1 - cy0},
        "inner": (ix0, ix1, iy0, iy1),
        "padding_px": padding,
        "padded_pixels": int(ww * wh - covered),
    }


def export_aligned_tiff(
    images,
    alignment_result,
    output_tiff: Path,
    status_callback=None,
    file_bands=None,
    output_order=None,
    split_info=None,
    settings=None,
    source_path=None,
    exif=None,
):
    """
    Cut every band from its lens image at the aligned position, inside one
    output window in the reference-lens pixel grid, and write the TIFF with
    camera / band / processing metadata.
    """
    settings = settings or DEFAULT_SETTINGS
    split_info = split_info or {}
    layout = _layout(settings)
    ref = layout.primary_reference
    output_order = output_order or OUTPUT_ORDER

    raw = alignment_result["apply_shifts"]
    # Express all shifts in the pixel grid of the primary reference lens, so the
    # output grid is the same whichever alignment mode was used.
    shifts = {
        k: {"dx": int(v["dx"]) - int(raw[ref]["dx"]), "dy": int(v["dy"]) - int(raw[ref]["dy"])}
        for k, v in raw.items()
    }

    log.info(
        "Alignment mode: %s | shifts (lens %s grid): %s",
        alignment_result["mode"], ref,
        ", ".join(f"{k}=({v['dx']},{v['dy']})" for k, v in sorted(shifts.items())),
    )

    lens_h, lens_w = images[ref].shape[:2]
    for idx, img in images.items():
        if img.shape[:2] != (lens_h, lens_w):
            raise ValueError(f"Image size mismatch in split set: image {idx}")

    win = output_window(shifts, lens_w, lens_h, settings)
    max_pad = max(win["padding_px"].values())
    if max_pad > int(settings["output_max_padding_px"]):
        raise RuntimeError(
            f"shared area of the lenses ({win['common_area']['width']}x{win['common_area']['height']} px) "
            f"does not contain the {win['width']}x{win['height']} px output window "
            f"(short by up to {max_pad} px); reduce output_size_px or allow output_max_padding_px"
        )

    band_map = build_band_map(file_bands)
    ix0, ix1, iy0, iy1 = win["inner"]
    ox, oy = win["x"], win["y"]
    dtype = images[ref].dtype

    local = _local_alignment(images, shifts, band_map, output_order, layout, win, settings, status_callback)
    correction = local.pop("_correction", None)

    data = np.zeros((len(output_order), win["height"], win["width"]), dtype=dtype)
    if correction is not None:
        yy, xx = np.mgrid[0:win["height"], 0:win["width"]].astype(np.float32)
    for b, label in enumerate(output_order):
        lens, ch = band_map[label]
        dx, dy = shifts[lens]["dx"], shifts[lens]["dy"]
        src = images[lens]
        plane = src[:, :, ch] if src.ndim == 3 else src
        if correction is not None and lens != ref:
            # aligned(p) = lens(p + origin - s_k + delta(p) * grid_step_k + c_k)
            step = correction["steps"][lens]
            cx, cy = correction["offsets"][lens]
            delta = correction["delta"]
            data[b] = cv2.remap(
                plane,
                (xx + (ox - dx + cx) + delta * step[0]).astype(np.float32),
                (yy + (oy - dy + cy) + delta * step[1]).astype(np.float32),
                cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0,
            )
        else:
            # aligned(x, y) = lens(x - dx, y - dy)
            data[b, iy0 - oy:iy1 - oy, ix0 - ox:ix1 - ox] = plane[iy0 - dy:iy1 - dy, ix0 - dx:ix1 - dx]

    origins = split_info.get("lens_origins") or {}
    scale = float(split_info.get("lens_scale", 1.0))
    output = {
        "width": win["width"],
        "height": win["height"],
        "bands": len(output_order),
        "frame_lens": ref,
        "anchor": win["anchor"],
        # top-left output pixel in the reference lens image ...
        "origin_in_reference_lens_px": [ox, oy],
        "common_area_in_reference_lens_px": win["common_area"],
        "padding_px": win["padding_px"],
        "padded_pixels": win["padded_pixels"],
        "shifts": shifts,
    }
    if local:
        output["local_alignment"] = local
    if str(ref) in origins:
        # ... and in the full source (sensor) image
        sx, sy = origins[str(ref)]
        if scale == 1.0:
            output["origin_in_source_px"] = [int(sx + ox), int(sy + oy)]
        else:
            output["origin_in_source_px"] = [round(sx + ox * scale, 2), round(sy + oy * scale, 2)]

    if status_callback:
        status_callback("Writing aligned multiband TIFF")

    meta = build_metadata(
        settings=settings,
        source_path=source_path,
        exif=exif or {},
        band_map=band_map,
        output_order=output_order,
        dtype=dtype,
        output={k: v for k, v in output.items()},
        alignment=alignment_result,
        split_info=split_info,
        quality=_quality_brief(alignment_result),
    )
    write_tiff(output_tiff, data, meta, output_order, settings)
    if correction is not None and settings["write_parallax_map"]:
        parallax_path = output_tiff.with_name(output_tiff.stem + "_parallax.tif")
        tifffile.imwrite(
            str(parallax_path), correction["delta"], photometric="minisblack", metadata=None,
            description=(
                "msalign parallax map: residual disparity in pixels per lens step "
                "(relative to the depth fitted by the global alignment; larger = nearer)"
            ),
        )
        output["local_alignment"]["parallax_map"] = parallax_path.name
    metadata_json = write_sidecar(output_tiff, meta) if settings["write_metadata_json"] else None

    output.pop("shifts")
    export_result = {
        "alignment": alignment_result,
        "output": output,
        "final_size": {"width": win["width"], "height": win["height"]},
        "output_tiff": str(output_tiff),
        "metadata_json": str(metadata_json) if metadata_json else "",
        "camera": meta["camera"],
        "split_info": split_info,
    }

    if status_callback:
        status_callback("Writing alignment quality report")

    export_result["quality_report"] = write_alignment_quality_report(
        output_tiff=output_tiff,
        export_result=export_result,
    )

    return export_result