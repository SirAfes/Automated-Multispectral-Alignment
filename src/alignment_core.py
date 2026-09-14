"""
alignment_core.py

Core image alignment utilities for estimating x/y translation between two images.

This module contains only the alignment logic and does not perform any file I/O
or directory management.
"""

import cv2
import numpy as np


# =========================================================
# DEFAULT CONFIGURATION
# =========================================================
DEFAULT_CONFIG = {
    # Refinement search range around the coarse shift
    "REFINE_DX": 40,
    "REFINE_DY": 40,

    # Cropping / preprocessing
    "GLOBAL_CENTER_CROP": 0.80,
    "BORDER_CROP_FRAC": 0.08,
    "CENTER_CROSS_BAND_FRAC": 0.08,

    # Patch selection parameters
    "PATCH_SIZE_FRAC": 0.18,
    "NUM_TOP_PATCHES": 9,
    "GRID_ROWS": 4,
    "GRID_COLS": 4,
    "MIN_PATCH_STD": 0.02,

    # Decision thresholds
    "MIN_PATCH_NCC": 0.20,
    "MIN_SUPPORT": 3,
    "MIN_CONFIDENCE": 0.28,

    # Phase correlation thresholds
    "MIN_PHASE_RESPONSE": 0.05,
    "PHASE_FALLBACK_RESPONSE": 0.10,
    "PHASE_STRONG_RESPONSE": 0.16,

    # Distance penalty for local patch votes away from the coarse anchor
    "DIST_PENALTY_PER_PIXEL": 0.003,

    # Debug output
    "PRINT_DEBUG": True,
}


# =========================================================
# CONFIG HELPER
# =========================================================
def _cfg(config, key):
    """
    Return the configured value for a given key, falling back to DEFAULT_CONFIG.
    """
    if config is None:
        return DEFAULT_CONFIG[key]
    return config.get(key, DEFAULT_CONFIG[key])


# =========================================================
# PREPROCESSING
# =========================================================
def to_gray_float(img):
    """
    Convert an input image to normalized grayscale float32 in the range [0, 1].
    """
    if img is None:
        raise FileNotFoundError("Image could not be loaded.")

    if img.ndim == 3:
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        g = img.copy()

    g = g.astype(np.float32)

    p1, p99 = np.percentile(g, 1), np.percentile(g, 99)
    g = (g - p1) / (p99 - p1 + 1e-6)
    g = np.clip(g, 0, 1)

    g = cv2.GaussianBlur(g, (0, 0), 1.0)
    return g.astype(np.float32)


def center_crop(img, frac):
    """
    Apply a centered crop using a fractional size relative to the input image.
    """
    h, w = img.shape[:2]
    ch = int(h * frac)
    cw = int(w * frac)
    y0 = (h - ch) // 2
    x0 = (w - cw) // 2
    return img[y0:y0 + ch, x0:x0 + cw].copy()


def border_crop(img, frac):
    """
    Remove a border region from all sides of the image using a fractional margin.
    """
    h, w = img.shape[:2]
    bx = int(w * frac)
    by = int(h * frac)
    x0, x1 = bx, w - bx
    y0, y1 = by, h - by

    if x1 <= x0 or y1 <= y0:
        return img.copy()

    return img[y0:y1, x0:x1].copy()


def gradient_mag(img_f):
    """
    Compute normalized gradient magnitude from a float32 grayscale image.
    """
    gx = cv2.Sobel(img_f, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(img_f, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    mag = cv2.GaussianBlur(mag, (0, 0), 1.0)

    p1, p99 = np.percentile(mag, 1), np.percentile(mag, 99)
    mag = (mag - p1) / (p99 - p1 + 1e-6)
    mag = np.clip(mag, 0, 1)

    return mag.astype(np.float32)


def suppress_center_cross(img_f, band_frac=0.08):
    """
    Suppress the central horizontal and vertical bands to reduce dominance of
    strong center-line structures during alignment.
    """
    h, w = img_f.shape[:2]
    out = img_f.copy()

    band = int(min(h, w) * band_frac / 2)
    cy = h // 2
    cx = w // 2

    if band > 0:
        out[max(0, cy - band):min(h, cy + band), :] *= 0.15
        out[:, max(0, cx - band):min(w, cx + band)] *= 0.15

    return out


def preprocess(img, config=None):
    """
    Full preprocessing pipeline used before alignment estimation.
    """
    g = to_gray_float(img)
    g = border_crop(g, _cfg(config, "BORDER_CROP_FRAC"))
    g = center_crop(g, _cfg(config, "GLOBAL_CENTER_CROP"))
    g = gradient_mag(g)
    g = suppress_center_cross(g, _cfg(config, "CENTER_CROSS_BAND_FRAC"))
    return g


# =========================================================
# SIMILARITY SCORE
# =========================================================
def ncc_score(a, b):
    """
    Compute normalized cross-correlation between two same-sized patches.
    """
    a = a.astype(np.float32)
    b = b.astype(np.float32)

    a = a - a.mean()
    b = b - b.mean()

    denom = np.linalg.norm(a) * np.linalg.norm(b) + 1e-12
    return float((a * b).sum() / denom)


# =========================================================
# GLOBAL COARSE SHIFT
# =========================================================
def phase_correlate_shift(ref_g, mov_g):
    """
    Estimate a coarse global translation using phase correlation.
    """
    h = min(ref_g.shape[0], mov_g.shape[0])
    w = min(ref_g.shape[1], mov_g.shape[1])

    ref = ref_g[:h, :w].copy()
    mov = mov_g[:h, :w].copy()

    win = cv2.createHanningWindow((w, h), cv2.CV_32F)
    (sx, sy), response = cv2.phaseCorrelate(ref * win, mov * win)

    return float(sx), float(sy), float(response)


# =========================================================
# PATCH SELECTION
# =========================================================
def choose_candidate_patches(ref_g, config=None):
    """
    Select the strongest candidate patches from the reference image based on
    texture and spatial preference.
    """
    h, w = ref_g.shape[:2]
    ps = int(min(h, w) * _cfg(config, "PATCH_SIZE_FRAC"))
    ps = max(64, ps)

    patches = []
    margin = int(ps * 0.6)

    grid_rows = _cfg(config, "GRID_ROWS")
    grid_cols = _cfg(config, "GRID_COLS")
    min_patch_std = _cfg(config, "MIN_PATCH_STD")

    for r in range(grid_rows):
        for c in range(grid_cols):
            cy = int((r + 0.5) * h / grid_rows)
            cx = int((c + 0.5) * w / grid_cols)

            if cx < margin or cx > (w - margin):
                continue
            if cy < margin or cy > (h - margin):
                continue

            y0 = cy - ps // 2
            x0 = cx - ps // 2
            y1 = y0 + ps
            x1 = x0 + ps

            patch = ref_g[y0:y1, x0:x1]
            if patch.shape[0] != ps or patch.shape[1] != ps:
                continue

            stdv = float(patch.std())
            if stdv < min_patch_std:
                continue

            texture = float(cv2.Laplacian(patch, cv2.CV_32F).var())

            dcx = abs(cx - w / 2) / (w / 2)
            dcy = abs(cy - h / 2) / (h / 2)
            center_penalty = 1.0 - 0.25 * (dcx + dcy)

            quality = texture * center_penalty

            patches.append({
                "rect": (x0, y0, ps, ps),
                "texture": texture,
                "std": stdv,
                "quality": quality,
            })

    patches.sort(key=lambda d: d["quality"], reverse=True)
    return patches[:_cfg(config, "NUM_TOP_PATCHES")]


# =========================================================
# LOCAL SHIFT SEARCH
# =========================================================
def local_search_ncc(ref_patch, mov_img, x0, y0, coarse_dx, coarse_dy, dx_max, dy_max, config=None):
    """
    Perform a local NCC-based search around the coarse translation estimate for
    a single reference patch.
    """
    ph, pw = ref_patch.shape[:2]
    h, w = mov_img.shape[:2]

    best = {
        "dx": None,
        "dy": None,
        "score": -1e9,
        "raw_ncc": -1e9,
        "dist_from_coarse": None,
    }

    base_x = x0 + coarse_dx
    base_y = y0 + coarse_dy
    dist_penalty = _cfg(config, "DIST_PENALTY_PER_PIXEL")

    for dy in range(-dy_max, dy_max + 1):
        yy0 = int(round(base_y + dy))
        yy1 = yy0 + ph
        if yy0 < 0 or yy1 > h:
            continue

        for dx in range(-dx_max, dx_max + 1):
            xx0 = int(round(base_x + dx))
            xx1 = xx0 + pw
            if xx0 < 0 or xx1 > w:
                continue

            mov_patch = mov_img[yy0:yy1, xx0:xx1]
            raw_ncc = ncc_score(ref_patch, mov_patch)

            cand_dx = int(round(coarse_dx + dx))
            cand_dy = int(round(coarse_dy + dy))

            dist = float(np.hypot(cand_dx - coarse_dx, cand_dy - coarse_dy))
            score = raw_ncc - dist_penalty * dist

            if score > best["score"]:
                best["dx"] = cand_dx
                best["dy"] = cand_dy
                best["score"] = score
                best["raw_ncc"] = raw_ncc
                best["dist_from_coarse"] = dist

    return best


# =========================================================
# CONSENSUS
# =========================================================
def get_inlier_tolerance(coarse_dx, coarse_dy):
    """
    Define the inlier tolerance based on the magnitude of the coarse shift.
    """
    shift_mag = float(np.hypot(coarse_dx, coarse_dy))

    if shift_mag < 120:
        return 8
    if shift_mag < 220:
        return 12
    return 18


def anchored_consensus(votes, coarse_dx, coarse_dy, phase_response):
    """
    Fuse patch-level votes around the coarse phase-correlation anchor.
    """
    if not votes:
        return None

    tol = get_inlier_tolerance(coarse_dx, coarse_dy)

    inliers = []
    for v in votes:
        if abs(v["dx"] - coarse_dx) <= tol and abs(v["dy"] - coarse_dy) <= tol:
            inliers.append(v)

    if not inliers:
        return {
            "dx": int(round(coarse_dx)),
            "dy": int(round(coarse_dy)),
            "confidence": float(phase_response),
            "support": 0,
            "num_votes": len(votes),
            "used_phase_fallback": True,
            "reason": "no_inliers_around_coarse",
        }

    weights = np.array([max(v["raw_ncc"], 0.001) for v in inliers], dtype=np.float32)
    dx_final = int(round(np.average([v["dx"] for v in inliers], weights=weights)))
    dy_final = int(round(np.average([v["dy"] for v in inliers], weights=weights)))

    patch_conf = float(np.mean([v["raw_ncc"] for v in inliers]))
    fused_conf = 0.65 * patch_conf + 0.35 * min(1.0, phase_response * 3.0)

    return {
        "dx": dx_final,
        "dy": dy_final,
        "confidence": fused_conf,
        "patch_confidence": patch_conf,
        "support": len(inliers),
        "num_votes": len(votes),
        "used_phase_fallback": False,
        "reason": "ok",
    }


# =========================================================
# MAIN ESTIMATOR
# =========================================================
def estimate_xy_v4(ref_img, mov_img, config=None):
    """
    Estimate x/y translation between a reference image and a moving image.

    Returns:
        dict: Alignment result with shift, confidence, support, and acceptance flag.
    """
    ref_g = preprocess(ref_img, config=config)
    mov_g = preprocess(mov_img, config=config)

    h = min(ref_g.shape[0], mov_g.shape[0])
    w = min(ref_g.shape[1], mov_g.shape[1])
    ref_g = ref_g[:h, :w]
    mov_g = mov_g[:h, :w]

    coarse_dx_f, coarse_dy_f, phase_response = phase_correlate_shift(ref_g, mov_g)
    coarse_dx = int(round(coarse_dx_f))
    coarse_dy = int(round(coarse_dy_f))

    if _cfg(config, "PRINT_DEBUG"):
        print(
            f"    coarse phase shift -> "
            f"dx={coarse_dx_f:.2f}, dy={coarse_dy_f:.2f}, response={phase_response:.4f}"
        )

    if phase_response < _cfg(config, "MIN_PHASE_RESPONSE"):
        return {
            "dx": coarse_dx,
            "dy": coarse_dy,
            "confidence": float(phase_response),
            "support": 0,
            "num_votes": 0,
            "accepted": False,
            "reason": f"low_phase_response({phase_response:.4f})",
        }

    patches = choose_candidate_patches(ref_g, config=config)
    if not patches:
        return None

    votes = []
    for p in patches:
        x0, y0, pw, ph = p["rect"]
        ref_patch = ref_g[y0:y0 + ph, x0:x0 + pw]

        best = local_search_ncc(
            ref_patch=ref_patch,
            mov_img=mov_g,
            x0=x0,
            y0=y0,
            coarse_dx=coarse_dx,
            coarse_dy=coarse_dy,
            dx_max=_cfg(config, "REFINE_DX"),
            dy_max=_cfg(config, "REFINE_DY"),
            config=config,
        )

        if best["dx"] is None:
            continue

        if _cfg(config, "PRINT_DEBUG"):
            print(
                f"    patch @ ({x0},{y0},{pw},{ph}) -> "
                f"dx={best['dx']}, dy={best['dy']}, "
                f"score={best['raw_ncc']:.3f}, dist={best['dist_from_coarse']:.1f}"
            )

        if best["raw_ncc"] >= _cfg(config, "MIN_PATCH_NCC"):
            votes.append(best)

    result = anchored_consensus(
        votes=votes,
        coarse_dx=coarse_dx,
        coarse_dy=coarse_dy,
        phase_response=phase_response,
    )

    if result is None:
        return {
            "dx": coarse_dx,
            "dy": coarse_dy,
            "confidence": float(phase_response),
            "support": 0,
            "num_votes": 0,
            "accepted": False,
            "reason": "no_votes",
        }

    if (
        result["support"] >= _cfg(config, "MIN_SUPPORT")
        and result["confidence"] >= _cfg(config, "MIN_CONFIDENCE")
    ):
        return {
            **result,
            "accepted": True,
            "reason": "ok",
        }

    if result["support"] >= 2 and phase_response >= _cfg(config, "PHASE_FALLBACK_RESPONSE"):
        return {
            **result,
            "accepted": True,
            "reason": "accepted_with_phase_anchor",
        }

    if result["support"] == 0 and phase_response >= _cfg(config, "PHASE_STRONG_RESPONSE"):
        return {
            "dx": coarse_dx,
            "dy": coarse_dy,
            "confidence": float(phase_response),
            "support": 0,
            "num_votes": len(votes),
            "accepted": True,
            "reason": "accepted_with_strong_phase_only",
        }

    if result["support"] < _cfg(config, "MIN_SUPPORT") and phase_response >= _cfg(config, "PHASE_STRONG_RESPONSE"):
        return {
            "dx": coarse_dx,
            "dy": coarse_dy,
            "confidence": float(phase_response),
            "support": result["support"],
            "num_votes": result["num_votes"],
            "accepted": True,
            "reason": "accepted_with_strong_phase_fallback",
        }

    return {
        **result,
        "accepted": False,
        "reason": f"low_support({result['support']}) / phase={phase_response:.4f}",
    }