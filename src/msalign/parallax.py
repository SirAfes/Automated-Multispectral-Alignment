"""
parallax.py

Depth-dependent (per-pixel) refinement after the global alignment, and a
local residual check.

Why: one translation per lens is exact only for a flat scene. In close-range
scenes with depth, nearer surfaces have larger parallax between the lenses, so
some regions stay misaligned (15-35 px measured on an Agrowing image).

Model: the lenses form a small camera array on a regular grid, so the
remaining displacement of lens k at pixel p is

    r_k(p) = delta(p) * (dcol_k, drow_k) + c_k

- delta(p): one disparity map shared by ALL lenses (inverse depth relative to
  the depth the global alignment fitted), in pixels per lens step;
- (dcol_k, drow_k): grid offset of lens k from the reference lens;
- c_k: small constant per-lens correction (sub-pixel accuracy of the global
  stage, lens mounting).

Because one map explains every lens, the estimate is far more constrained than
independent optical flow per band (multi-baseline stereo, Okutomi & Kanade 1993).
delta is found by a plane sweep: for each candidate value, all lenses are
shifted accordingly and the local normalised cross-correlation of their
gradient images is summed over adjacent lens pairs (gradients make the cost
work across spectral bands). The best value per pixel is smoothed with a
confidence-weighted filter, so weak or ambiguous areas follow their reliable
neighbours or keep the global alignment.

Safety: the residual misalignment is measured on image tiles before and after
(local_residuals). If the correction does not reduce it, it is discarded and
the global alignment is kept.
"""

from __future__ import annotations

import logging
from itertools import combinations

import cv2
import numpy as np

from .image_io import fast_percentiles


log = logging.getLogger(__name__)

SCALE = 4            # disparity sweep at 1/4 resolution
CHECK_SCALE = 2      # residual check at 1/2 resolution (quarter resolution is too coarse)
NCC_WINDOW = 9       # local NCC window at work resolution (~36 px full resolution)
STEP = 0.5           # sweep step at work resolution (2 px full resolution, refined sub-pixel)
MIN_SCORE = 0.25     # pixels with a lower best NCC do not vote
SMOOTH_SIGMA = 6.0   # confidence-weighted smoothing at work resolution
TILE = 256           # residual check tile at check resolution (512 px full resolution)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _crop(img, x, y, w, h):
    """img[y:y+h, x:x+w] with zero padding outside the image."""
    H, W = img.shape[:2]
    out = np.zeros((h, w) + img.shape[2:], dtype=img.dtype)
    x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + w, W), min(y + h, H)
    if x1 > x0 and y1 > y0:
        out[y0 - y:y1 - y, x0 - x:x1 - x] = img[y0:y1, x0:x1]
    return out


def normalise_crop(plane):
    return _normalise(plane)


def _normalise(plane):
    lo, hi = fast_percentiles(plane, [1, 99])
    lo, hi = float(lo), float(hi)
    return np.clip((plane.astype(np.float32) - np.float32(lo)) / np.float32(max(hi - lo, 1e-6)), 0, 1).astype(np.float32)


def _gradient(gray):
    g = cv2.GaussianBlur(gray, (0, 0), 1.0)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1)
    mag = np.sqrt(gx * gx + gy * gy)
    return (mag - mag.mean()) / (mag.std() + 1e-6)


def _local_ncc(a, b):
    box = lambda x: cv2.boxFilter(x, -1, (NCC_WINDOW, NCC_WINDOW))  # noqa: E731
    ma, mb = box(a), box(b)
    va = box(a * a) - ma * ma
    vb = box(b * b) - mb * mb
    return (box(a * b) - ma * mb) / np.sqrt(np.maximum(va * vb, 1e-12))


def _shifted(img, xx, yy, dx, dy):
    return cv2.remap(img, (xx + dx).astype(np.float32), (yy + dy).astype(np.float32),
                     cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


def _warped(img, xx, yy, delta, step, offset):
    """img sampled at p + delta(p) * step + offset (work resolution)."""
    return cv2.remap(img, (xx + delta * step[0] + offset[0]).astype(np.float32),
                     (yy + delta * step[1] + offset[1]).astype(np.float32),
                     cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


# ---------------------------------------------------------------------------
# local residual check
# ---------------------------------------------------------------------------

def _grads(crops, scale):
    return {
        k: _gradient(cv2.resize(_normalise(c), None, fx=1 / scale, fy=1 / scale, interpolation=cv2.INTER_AREA))
        for k, c in crops.items()
    }


def local_residuals(grads: dict, max_shift: float, scale: int = CHECK_SCALE) -> dict:
    """
    Residual misalignment of each lens against the mean of all other lenses,
    measured by phase correlation on image tiles (work resolution in, full
    resolution px out). Returns {lens: {"median", "p90", "max", "tiles", "offset"}}.
    "offset" is the median residual vector (used for the constant correction).
    """
    lenses = sorted(grads)
    h, w = next(iter(grads.values())).shape
    # ~4 x 4 tiles or more; 512 px tiles on full-size lens images
    t = int(max(24, min(TILE, min(h, w) // 3)))
    t -= t % 2  # cv2.phaseCorrelate reports a spurious (0.5, 0.5) shift for odd sizes
    ny, nx = max(2, h // t + 1), max(2, w // t + 1)
    window = cv2.createHanningWindow((t, t), cv2.CV_32F)
    out = {}
    for k in lenses:
        others = np.mean([grads[j] for j in lenses if j != k], axis=0).astype(np.float32)
        vectors = []
        for ty in range(ny):
            for tx in range(nx):
                y0 = int(ty * (h - t) / (ny - 1))
                x0 = int(tx * (w - t) / (nx - 1))
                (dx, dy), r = cv2.phaseCorrelate(
                    others[y0:y0 + t, x0:x0 + t], grads[k][y0:y0 + t, x0:x0 + t], window
                )
                if r > 0.06 and np.hypot(dx, dy) * scale <= max_shift:
                    vectors.append((dx * scale, dy * scale))
        if len(vectors) >= 3:
            v = np.asarray(vectors)
            mag = np.hypot(v[:, 0], v[:, 1])
            out[k] = {
                "median": round(float(np.median(mag)), 2),
                "p90": round(float(np.percentile(mag, 90)), 2),
                "max": round(float(mag.max()), 2),
                "tiles": len(vectors),
                "offset": [float(np.median(v[:, 0])), float(np.median(v[:, 1]))],
            }
        else:
            out[k] = {"median": None, "p90": None, "max": None, "tiles": len(vectors), "offset": [0.0, 0.0]}
    return out


def summarise(residuals: dict) -> dict:
    p90 = [r["p90"] for r in residuals.values() if r["p90"] is not None]
    med = [r["median"] for r in residuals.values() if r["median"] is not None]
    return {
        "worst_p90_px": max(p90) if p90 else None,
        "median_px": round(float(np.median(med)), 2) if med else None,
        "lenses_measured": len(p90),
    }


# ---------------------------------------------------------------------------
# disparity estimation
# ---------------------------------------------------------------------------

def _sweep(grads, steps, offsets, pairs, max_delta):
    h, w = next(iter(grads.values())).shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    candidates = np.arange(-max_delta, max_delta + 1e-6, STEP, dtype=np.float32)
    cost = np.empty((len(candidates), h, w), np.float32)
    for i, d in enumerate(candidates):
        warped = {
            k: _shifted(g, xx, yy, d * steps[k][0] + offsets[k][0], d * steps[k][1] + offsets[k][1])
            for k, g in grads.items()
        }
        cost[i] = np.mean([_local_ncc(warped[a], warped[b]) for a, b in pairs], axis=0)

    best = np.argmax(cost, axis=0)
    i0 = np.clip(best, 1, len(candidates) - 2)
    c0 = np.take_along_axis(cost, (i0 - 1)[None], 0)[0]
    c1 = np.take_along_axis(cost, i0[None], 0)[0]
    c2 = np.take_along_axis(cost, (i0 + 1)[None], 0)[0]
    den = c0 - 2 * c1 + c2
    sub = np.where(np.abs(den) > 1e-6, 0.5 * (c0 - c2) / np.where(np.abs(den) > 1e-6, den, 1), 0)
    delta = candidates[i0] + np.clip(sub, -0.5, 0.5) * STEP

    score = np.take_along_axis(cost, best[None], 0)[0]
    at_edge = (best == 0) | (best == len(candidates) - 1)
    confidence = np.where((score > MIN_SCORE) & ~at_edge, score - cost.mean(axis=0), 0).clip(0)

    num = cv2.GaussianBlur((delta * confidence).astype(np.float32), (0, 0), SMOOTH_SIGMA)
    den = cv2.GaussianBlur(confidence.astype(np.float32), (0, 0), SMOOTH_SIGMA)
    smooth = np.where(den > 1e-4, num / np.maximum(den, 1e-6), 0).astype(np.float32)
    smooth = cv2.medianBlur(smooth, 5)
    reliable = float((confidence > 0).mean())
    return smooth, reliable


def _warp_check(grads2, delta4, steps, offsets_full):
    """Warp half-resolution gradients with a quarter-resolution disparity map."""
    h, w = next(iter(grads2.values())).shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    ratio = SCALE / CHECK_SCALE
    d = cv2.resize(delta4, (w, h), interpolation=cv2.INTER_LINEAR) * ratio
    return {
        k: _warped(g, xx, yy, d, steps[k], (offsets_full[k][0] / CHECK_SCALE, offsets_full[k][1] / CHECK_SCALE))
        for k, g in grads2.items()
    }


def estimate_parallax(lens_crops: dict, steps: dict, pairs, settings) -> dict:
    """
    lens_crops: {lens: grayscale crop of the lens image in the output window,
                 already placed by the global alignment}
    steps:      {lens: (dcol, drow)} grid offset from the reference lens
    Returns {"delta": full-res map (px per lens step), "offsets": {lens: (cx, cy)} full-res px,
             "before": residuals, "after": residuals, "applied": bool, "reason": str}
    """
    max_px = float(settings["parallax_search_px"])
    grads4 = _grads(lens_crops, SCALE)
    grads2 = _grads(lens_crops, CHECK_SCALE)
    ref = next(k for k, s in steps.items() if s == (0, 0))
    max_check = max_px * 2

    before = local_residuals(grads2, max_check)

    # 1) disparity with the global alignment as it is
    zero = {k: (0.0, 0.0) for k in grads4}
    delta, reliable = _sweep(grads4, steps, zero, pairs, max_px / SCALE)

    # 2) constant per-lens correction from what is left, then sweep again
    mid = local_residuals(_warp_check(grads2, delta, steps, zero), max_check)
    offsets_full = {k: (0.0, 0.0) if k == ref else tuple(mid[k]["offset"]) for k in grads4}
    offsets4 = {k: (v[0] / SCALE, v[1] / SCALE) for k, v in offsets_full.items()}
    delta, reliable = _sweep(grads4, steps, offsets4, pairs, max_px / SCALE)

    after = local_residuals(_warp_check(grads2, delta, steps, offsets_full), max_check)

    sb, sa = summarise(before), summarise(after)
    applied = (
        sa["worst_p90_px"] is not None and sb["worst_p90_px"] is not None
        and sa["worst_p90_px"] < sb["worst_p90_px"] - 0.5
        and sa["median_px"] <= sb["median_px"] + 0.25
    )
    reason = "reduced local residuals" if applied else "no reliable improvement; global alignment kept"

    full_h, full_w = next(iter(lens_crops.values())).shape[:2]
    delta_full = cv2.resize(delta, (full_w, full_h), interpolation=cv2.INTER_CUBIC) * SCALE
    return {
        "delta": delta_full.astype(np.float32),
        "offsets": offsets_full,
        "before": before,
        "after": after,
        "reliable_fraction": round(reliable, 3),
        "applied": bool(applied),
        "reason": reason,
    }


def check_only(lens_crops: dict, settings) -> dict:
    return local_residuals(_grads(lens_crops, CHECK_SCALE), float(settings["parallax_search_px"]) * 2)


def lens_pairs(layout, lenses) -> list:
    """Adjacent lens pairs (short baselines, little occlusion) among the given lenses."""
    pairs = [(a, b) for a, b in layout.grid_edges if a in lenses and b in lenses]
    return pairs or list(combinations(sorted(lenses), 2))
