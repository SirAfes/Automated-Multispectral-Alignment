"""
alignment_plan_b.py

Fallback alignment utilities for difficult multispectral image pairs.

Currently implemented:
- directional_patch_consensus

This module is intended to be used only when the primary alignment method fails.
"""

import cv2
import numpy as np

from alignment_core import (
    to_gray_float,
    center_crop,
    border_crop,
    gradient_mag,
    suppress_center_cross,
    ncc_score,
)


# =========================================================
# CONFIG
# =========================================================
DEFAULT_PLAN_B_CONFIG = {
    "PRINT_DEBUG": True,

    # preprocessing
    "CENTER_CROP_FRAC": 0.90,
    "BORDER_CROP_FRAC": 0.04,
    "CENTER_CROSS_BAND_FRAC": 0.05,

    # patch selection
    "PATCH_SIZE_FRAC": 0.18,
    "GRID_ROWS": 5,
    "GRID_COLS": 5,
    "MIN_PATCH_STD": 0.012,

    # staged testing
    "PATCH_COUNTS": [3, 6],

    # inlier filtering
    "INLIER_TOL_DX": 25,
    "INLIER_TOL_DY": 25,

    # acceptance
    "MIN_SUPPORT": 3,
    "MIN_SCORE_PATCH": 0.25,

    # fallback search window if no pair prior exists
    "DEFAULT_DX_MIN": -250,
    "DEFAULT_DX_MAX": 250,
    "DEFAULT_DY_MIN": -250,
    "DEFAULT_DY_MAX": 250,
}


# =========================================================
# PAIR GEOMETRY PRIORS
# =========================================================
PAIR_PRIORS = {
    (1, 2): {"dx_min": 80,  "dx_max": 300, "dy_min": -20, "dy_max": 80},
    (1, 3): {"dx_min": 160, "dx_max": 320, "dy_min": -20, "dy_max": 100},
    (1, 4): {"dx_min": -100, "dx_max": 60, "dy_min": 80,  "dy_max": 300},
    (1, 5): {"dx_min": 40,  "dx_max": 300, "dy_min": 100, "dy_max": 320},
    (1, 6): {"dx_min": 160, "dx_max": 320, "dy_min": 120, "dy_max": 320},


    # Optional 2/5-centered pairs for manual testing
    (2, 1): {"dx_min": -320, "dx_max": -80, "dy_min": -80, "dy_max": 20},
    (2, 3): {"dx_min": 80,   "dx_max": 320, "dy_min": -20, "dy_max": 80},
    (2, 5): {"dx_min": -80,  "dx_max": 80,  "dy_min": 100, "dy_max": 360},
    (5, 4): {"dx_min": -320, "dx_max": -80, "dy_min": -80, "dy_max": 20},
    (5, 6): {"dx_min": 80,   "dx_max": 320, "dy_min": -20, "dy_max": 80},
}


# =========================================================
# HELPERS
# =========================================================
def _cfg(config, key):
    if config is None:
        return DEFAULT_PLAN_B_CONFIG[key]
    return config.get(key, DEFAULT_PLAN_B_CONFIG[key])


def _debug(config, msg):
    if _cfg(config, "PRINT_DEBUG"):
        print(msg)


def get_search_window(pair_hint=None, config=None):
    if pair_hint is not None and pair_hint in PAIR_PRIORS:
        return PAIR_PRIORS[pair_hint]

    return {
        "dx_min": _cfg(config, "DEFAULT_DX_MIN"),
        "dx_max": _cfg(config, "DEFAULT_DX_MAX"),
        "dy_min": _cfg(config, "DEFAULT_DY_MIN"),
        "dy_max": _cfg(config, "DEFAULT_DY_MAX"),
    }


def preprocess_for_plan_b(img, config=None):
    g = to_gray_float(img)
    g = border_crop(g, _cfg(config, "BORDER_CROP_FRAC"))
    g = center_crop(g, _cfg(config, "CENTER_CROP_FRAC"))
    g = gradient_mag(g)
    g = suppress_center_cross(g, _cfg(config, "CENTER_CROSS_BAND_FRAC"))
    return g


def choose_reference_patches(ref_g, config=None, max_patches=9):
    h, w = ref_g.shape[:2]
    ps = int(min(h, w) * _cfg(config, "PATCH_SIZE_FRAC"))
    ps = max(96, ps)

    candidates = []
    margin = int(ps * 0.6)

    grid_rows = _cfg(config, "GRID_ROWS")
    grid_cols = _cfg(config, "GRID_COLS")
    min_std = _cfg(config, "MIN_PATCH_STD")

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
            if stdv < min_std:
                continue

            texture = float(cv2.Laplacian(patch, cv2.CV_32F).var())
            score = texture * (
                1.0
                - 0.15 * abs(cx - w / 2) / (w / 2)
                - 0.15 * abs(cy - h / 2) / (h / 2)
            )

            candidates.append({
                "rect": (x0, y0, ps, ps),
                "score": score,
            })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:max_patches]


def constrained_patch_search(ref_patch, mov_g, x0, y0, dx_min, dx_max, dy_min, dy_max):
    ph, pw = ref_patch.shape[:2]
    h, w = mov_g.shape[:2]

    best = {"dx": None, "dy": None, "score": -1e9}

    for dy in range(dy_min, dy_max + 1):
        yy0 = y0 + dy
        yy1 = yy0 + ph
        if yy0 < 0 or yy1 > h:
            continue

        for dx in range(dx_min, dx_max + 1):
            xx0 = x0 + dx
            xx1 = xx0 + pw
            if xx0 < 0 or xx1 > w:
                continue

            mov_patch = mov_g[yy0:yy1, xx0:xx1]
            score = ncc_score(ref_patch, mov_patch)

            if score > best["score"]:
                best["dx"] = dx
                best["dy"] = dy
                best["score"] = score

    return best


def robust_vote_fusion(votes, config=None):
    if not votes:
        return None

    dxs = np.array([v["dx"] for v in votes], dtype=np.float32)
    dys = np.array([v["dy"] for v in votes], dtype=np.float32)

    dx_med = float(np.median(dxs))
    dy_med = float(np.median(dys))

    tol_dx = _cfg(config, "INLIER_TOL_DX")
    tol_dy = _cfg(config, "INLIER_TOL_DY")

    inliers = []
    for v in votes:
        if abs(v["dx"] - dx_med) <= tol_dx and abs(v["dy"] - dy_med) <= tol_dy:
            inliers.append(v)

    if not inliers:
        return None

    weights = np.array([max(v["score"], 0.001) for v in inliers], dtype=np.float32)
    dx_final = int(round(np.average([v["dx"] for v in inliers], weights=weights)))
    dy_final = int(round(np.average([v["dy"] for v in inliers], weights=weights)))
    score_final = float(np.mean([v["score"] for v in inliers]))

    return {
        "dx": dx_final,
        "dy": dy_final,
        "score": score_final,
        "support": len(inliers),
        "num_votes": len(votes),
        "median_dx": dx_med,
        "median_dy": dy_med,
    }


# =========================================================
# FEATURE-BASED FALLBACK TRANSLATION
# =========================================================
def preprocess_for_features(img):
    """
    Prepare image for feature detection.
    Uses grayscale normalization + CLAHE to improve local contrast.
    """
    if img.ndim == 3:
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        g = img.copy()

    g = g.astype(np.uint8) if g.dtype != np.uint8 else g

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    g = clahe.apply(g)

    return g


def feature_ransac_translation(ref_img, mov_img, config=None):
    """
    Estimate translation using AKAZE feature matching + RANSAC-like filtering.

    Returns:
        dict with dx, dy, score, support, accepted, method, details
    """
    ref_g = preprocess_for_features(ref_img)
    mov_g = preprocess_for_features(mov_img)

    detector = cv2.AKAZE_create()
    kp1, des1 = detector.detectAndCompute(ref_g, None)
    kp2, des2 = detector.detectAndCompute(mov_g, None)

    if des1 is None or des2 is None:
        return None

    if len(kp1) < 8 or len(kp2) < 8:
        return None

    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
    raw_matches = matcher.knnMatch(des1, des2, k=2)

    good = []
    for pair in raw_matches:
        if len(pair) < 2:
            continue
        m, n = pair
        if m.distance < 0.75 * n.distance:
            good.append(m)

    if len(good) < 6:
        return None

    shifts = []
    for m in good:
        p1 = kp1[m.queryIdx].pt
        p2 = kp2[m.trainIdx].pt

        dx = p2[0] - p1[0]
        dy = p2[1] - p1[1]
        shifts.append((dx, dy, m.distance))

    dxs = np.array([s[0] for s in shifts], dtype=np.float32)
    dys = np.array([s[1] for s in shifts], dtype=np.float32)

    med_dx = float(np.median(dxs))
    med_dy = float(np.median(dys))

    inliers = []
    for dx, dy, dist in shifts:
        if abs(dx - med_dx) <= 20 and abs(dy - med_dy) <= 20:
            inliers.append((dx, dy, dist))

    if len(inliers) < 5:
        return None

    weights = np.array([1.0 / (d + 1.0) for _, _, d in inliers], dtype=np.float32)

    dx_final = int(round(np.average([v[0] for v in inliers], weights=weights)))
    dy_final = int(round(np.average([v[1] for v in inliers], weights=weights)))

    residuals = [
        np.hypot(dx - dx_final, dy - dy_final)
        for dx, dy, _ in inliers
    ]
    mean_residual = float(np.mean(residuals))

    score = float(len(inliers) / max(len(good), 1)) / (1.0 + mean_residual)

    accepted = len(inliers) >= 6 and mean_residual <= 8.0

    return {
        "dx": dx_final,
        "dy": dy_final,
        "score": score,
        "support": len(inliers),
        "accepted": accepted,
        "method": "feature_ransac_translation",
        "details": {
            "keypoints_ref": len(kp1),
            "keypoints_mov": len(kp2),
            "matches": len(good),
            "inliers": len(inliers),
            "median_dx": med_dx,
            "median_dy": med_dy,
            "mean_residual": mean_residual,
        },
    }


# =========================================================
# DIRECTIONAL PATCH CONSENSUS
# =========================================================
def directional_patch_consensus(ref_img, mov_img, pair_hint=None, config=None):
    """
    Directional patch-based fallback estimator.
    """
    search = get_search_window(pair_hint=pair_hint, config=config)

    ref_g = preprocess_for_plan_b(ref_img, config=config)
    mov_g = preprocess_for_plan_b(mov_img, config=config)

    h = min(ref_g.shape[0], mov_g.shape[0])
    w = min(ref_g.shape[1], mov_g.shape[1])
    ref_g = ref_g[:h, :w]
    mov_g = mov_g[:h, :w]

    patch_counts = _cfg(config, "PATCH_COUNTS")
    patches = choose_reference_patches(ref_g, config=config, max_patches=max(patch_counts))

    if not patches:
        return None

    last_result = None

    for patch_count in patch_counts:
        votes = []

        for p in patches[:patch_count]:
            x0, y0, pw, ph = p["rect"]
            ref_patch = ref_g[y0:y0 + ph, x0:x0 + pw]

            best = constrained_patch_search(
                ref_patch=ref_patch,
                mov_g=mov_g,
                x0=x0,
                y0=y0,
                dx_min=search["dx_min"],
                dx_max=search["dx_max"],
                dy_min=search["dy_min"],
                dy_max=search["dy_max"],
            )

            if best["dx"] is None:
                continue

            _debug(
                config,
                f"    planB patch @ ({x0},{y0},{pw},{ph}) -> "
                f"dx={best['dx']}, dy={best['dy']}, score={best['score']:.3f}"
            )

            if best["score"] >= 0.10:
                votes.append(best)

        fused = robust_vote_fusion(votes, config=config)
        if fused is None:
            continue

        _debug(
            config,
            f"    planB median center -> dx={fused['median_dx']:.1f}, "
            f"dy={fused['median_dy']:.1f} | "
            f"inliers={fused['support']}/{fused['num_votes']}"
        )

        accepted = False

        if fused["support"] >= _cfg(config, "MIN_SUPPORT") and fused["score"] >= _cfg(config, "MIN_SCORE_PATCH"):
            accepted = True

        # fallback: strong consensus but slightly lower score
        elif fused["support"] >= 4 and fused["score"] >= 0.18:
            accepted = True

        last_result = {
            "dx": fused["dx"],
            "dy": fused["dy"],
            "score": fused["score"],
            "support": fused["support"],
            "accepted": accepted,
            "method": "directional_patch_consensus",
            "details": {
                "num_votes": fused["num_votes"],
                "patch_count": patch_count,
                "median_dx": fused["median_dx"],
                "median_dy": fused["median_dy"],
            },
        }

        if last_result["accepted"]:
            return last_result

    return last_result


def estimate_xy_plan_b(ref_img, mov_img, pair_hint=None, config=None):
    """
    Fallback alignment order:

    1. Directional patch consensus (Plan B)
    2. Feature-based AKAZE/RANSAC translation (Plan C)
    """

    patch_result = directional_patch_consensus(
        ref_img,
        mov_img,
        pair_hint=pair_hint,
        config=config,
    )

    if patch_result is not None and patch_result["accepted"]:
        if _cfg(config, "PRINT_DEBUG"):
            print("\n[PLAN B RESULT]")
            print(patch_result)
        return patch_result

    feature_result = feature_ransac_translation(
        ref_img,
        mov_img,
        config=config,
    )

    if _cfg(config, "PRINT_DEBUG"):
        print("\n[PLAN B / PLAN C RESULT]")
        print({
            "patch_result": patch_result,
            "feature_result": feature_result,
        })

    if feature_result is not None and feature_result["accepted"]:
        return feature_result

    return patch_result
