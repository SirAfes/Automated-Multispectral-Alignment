"""
alignment_plan_b.py

Fallback alignment utilities for difficult multispectral image pairs.

Implemented stages:
- directional patch consensus (Plan B)
- AKAZE feature translation (Plan C)
- sequence-aware history-guided residual retry

Historical geometry is used only after the normal pair estimators reject a result.
"""

import cv2
import numpy as np

from alignment_core import (
    DEFAULT_CONFIG,
    estimate_xy_v4,
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

    # history-guided retry
    "PRIOR_RESIDUAL_WINDOW": 80,
    "PRIOR_MAX_RESIDUAL": 90,
    "PRIOR_PRIMARY_REFINE": 35,
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
    """
    Search a bounded translation window using OpenCV's optimized normalized
    cross-correlation.  This is mathematically equivalent to evaluating the old
    per-pixel NCC loop, but avoids tens of thousands of Python-level patch
    comparisons for every selected patch.
    """
    ph, pw = ref_patch.shape[:2]
    h, w = mov_g.shape[:2]

    valid_dx_min = max(int(dx_min), -int(x0))
    valid_dx_max = min(int(dx_max), int(w - pw - x0))
    valid_dy_min = max(int(dy_min), -int(y0))
    valid_dy_max = min(int(dy_max), int(h - ph - y0))

    if valid_dx_min > valid_dx_max or valid_dy_min > valid_dy_max:
        return {"dx": None, "dy": None, "score": -1e9}

    sx0 = int(x0 + valid_dx_min)
    sy0 = int(y0 + valid_dy_min)
    sx1 = int(x0 + valid_dx_max + pw)
    sy1 = int(y0 + valid_dy_max + ph)

    search_region = mov_g[sy0:sy1, sx0:sx1].astype(np.float32, copy=False)
    template = ref_patch.astype(np.float32, copy=False)

    if search_region.shape[0] < ph or search_region.shape[1] < pw:
        return {"dx": None, "dy": None, "score": -1e9}

    scores = cv2.matchTemplate(search_region, template, cv2.TM_CCOEFF_NORMED)
    _, max_score, _, max_loc = cv2.minMaxLoc(scores)

    return {
        "dx": int(valid_dx_min + max_loc[0]),
        "dy": int(valid_dy_min + max_loc[1]),
        "score": float(max_score),
    }


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

    # Use the median translation of the spatially consistent patches.  A
    # weighted mean can be pulled toward a single high-NCC but geometrically
    # misleading patch (for example a repetitive texture).  The median is much
    # more stable for fixed multi-lens geometry.
    dx_final = int(round(float(np.median([v["dx"] for v in inliers]))))
    dy_final = int(round(float(np.median([v["dy"] for v in inliers]))))
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


def _result_summary(result, label):
    """Create a compact diagnostic message without hiding a later fallback result."""
    if result is None:
        return f"{label}=no_result"

    support = int(result.get("support", 0))
    score = float(result.get("score", result.get("confidence", 0.0)))
    details = result.get("details", {}) or {}
    residual = details.get("mean_residual")

    extra = ""
    if residual is not None:
        extra = f", mean_residual={float(residual):.2f}"

    return (
        f"{label}={'accepted' if result.get('accepted') else 'rejected'}"
        f"(support={support}, score={score:.4f}{extra})"
    )


def _translate_for_retry(img, apply_dx, apply_dy):
    """Translate an image for a prior-guided residual-alignment retry."""
    h, w = img.shape[:2]
    matrix = np.float32([[1, 0, float(apply_dx)], [0, 1, float(apply_dy)]])
    return cv2.warpAffine(
        img,
        matrix,
        (w, h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )


def _residual_is_plausible(result, config=None):
    if result is None or not result.get("accepted"):
        return False

    max_residual = int(_cfg(config, "PRIOR_MAX_RESIDUAL"))
    return (
        abs(int(result.get("dx", 0))) <= max_residual
        and abs(int(result.get("dy", 0))) <= max_residual
    )


def _combine_with_prior(result, prior_shift, method_name):
    """Convert a residual result back into the original pair coordinate system."""
    prior_dx = int(round(float(prior_shift["dx"])))
    prior_dy = int(round(float(prior_shift["dy"])))
    residual_dx = int(result["dx"])
    residual_dy = int(result["dy"])

    details = dict(result.get("details", {}) or {})
    details.update({
        "prior_dx": prior_dx,
        "prior_dy": prior_dy,
        "prior_samples": int(prior_shift.get("samples", 0)),
        "prior_mad_dx": float(prior_shift.get("mad_dx", 0.0)),
        "prior_mad_dy": float(prior_shift.get("mad_dy", 0.0)),
        "residual_dx": residual_dx,
        "residual_dy": residual_dy,
        "source_method": str(result.get("method", "primary")),
    })

    if "num_votes" in result:
        details.setdefault("num_votes", int(result["num_votes"]))

    return {
        "dx": prior_dx + residual_dx,
        "dy": prior_dy + residual_dy,
        "score": float(result.get("score", result.get("confidence", 0.0))),
        "support": int(result.get("support", 0)),
        "accepted": True,
        "method": method_name,
        "details": details,
    }


def prior_guided_retry(ref_img, mov_img, prior_shift, config=None):
    """
    Re-run all three alignment strategies around a historical pair shift.

    The moving image is first translated by the inverse of the historical raw
    shift.  The remaining alignment should therefore be a small residual around
    zero.  We then try:

    A. phase/patch primary estimator
    B. directional patch consensus in a narrow residual window
    C. AKAZE feature translation

    Only a strictly accepted residual result is combined with the historical
    prior.  The original acceptance thresholds are intentionally not relaxed.
    """
    if not prior_shift:
        return None

    prior_dx = int(round(float(prior_shift["dx"])))
    prior_dy = int(round(float(prior_shift["dy"])))

    _debug(
        config,
        f"    history prior -> dx={prior_dx}, dy={prior_dy}, "
        f"samples={int(prior_shift.get('samples', 0))}"
    )

    # raw shift describes moving relative to reference; apply its inverse first.
    shifted_mov = _translate_for_retry(mov_img, -prior_dx, -prior_dy)

    # Plan A again, now around residual ~= (0, 0).
    primary_config = DEFAULT_CONFIG.copy()
    primary_config.update({
        "REFINE_DX": int(_cfg(config, "PRIOR_PRIMARY_REFINE")),
        "REFINE_DY": int(_cfg(config, "PRIOR_PRIMARY_REFINE")),
        "PRINT_DEBUG": bool(_cfg(config, "PRINT_DEBUG")),
    })
    primary_result = estimate_xy_v4(ref_img, shifted_mov, config=primary_config)
    if _residual_is_plausible(primary_result, config=config):
        return _combine_with_prior(
            primary_result,
            prior_shift,
            "history_prior_phase_patch_consensus",
        )

    # Plan B again, but search only a small residual window around zero.
    residual_window = int(_cfg(config, "PRIOR_RESIDUAL_WINDOW"))
    residual_config = dict(config or {})
    residual_config.update({
        "DEFAULT_DX_MIN": -residual_window,
        "DEFAULT_DX_MAX": residual_window,
        "DEFAULT_DY_MIN": -residual_window,
        "DEFAULT_DY_MAX": residual_window,
    })
    patch_result = directional_patch_consensus(
        ref_img,
        shifted_mov,
        pair_hint=None,
        config=residual_config,
    )
    if _residual_is_plausible(patch_result, config=config):
        return _combine_with_prior(
            patch_result,
            prior_shift,
            "history_prior_directional_patch_consensus",
        )

    # Plan C again. Its strict residual-quality threshold is preserved.
    feature_result = feature_ransac_translation(
        ref_img,
        shifted_mov,
        config=config,
    )
    if _residual_is_plausible(feature_result, config=config):
        return _combine_with_prior(
            feature_result,
            prior_shift,
            "history_prior_feature_ransac_translation",
        )

    # A strict historical prior lets us use agreement between independent weak
    # estimators without loosening any individual estimator's normal threshold.
    # This is intentionally enabled only when at least three prior successful
    # frames support the same physical lens geometry.
    prior_samples = int(prior_shift.get("samples", 0))
    prior_spread = max(
        float(prior_shift.get("mad_dx", 0.0)),
        float(prior_shift.get("mad_dy", 0.0)),
    )
    if prior_samples >= 3 and prior_spread <= 10.0:
        candidates = []

        if primary_result is not None:
            phase_response = float(primary_result.get("confidence", 0.0))
            if phase_response >= 0.025:
                candidates.append((
                    "A",
                    int(primary_result.get("dx", 0)),
                    int(primary_result.get("dy", 0)),
                    phase_response,
                ))

        if patch_result is not None:
            if (
                int(patch_result.get("support", 0)) >= 2
                and float(patch_result.get("score", 0.0)) >= 0.25
            ):
                candidates.append((
                    "B",
                    int(patch_result.get("dx", 0)),
                    int(patch_result.get("dy", 0)),
                    float(patch_result.get("score", 0.0)),
                ))

        if feature_result is not None:
            feature_details = feature_result.get("details", {}) or {}
            if (
                int(feature_result.get("support", 0)) >= 10
                and float(feature_details.get("mean_residual", 1e9)) <= 12.0
            ):
                candidates.append((
                    "C",
                    int(feature_result.get("dx", 0)),
                    int(feature_result.get("dy", 0)),
                    float(feature_result.get("score", 0.0)),
                ))

        if len(candidates) >= 2:
            residual_dxs = np.asarray([c[1] for c in candidates], dtype=np.float32)
            residual_dys = np.asarray([c[2] for c in candidates], dtype=np.float32)
            med_dx = int(round(float(np.median(residual_dxs))))
            med_dy = int(round(float(np.median(residual_dys))))

            agreeing = [
                c for c in candidates
                if abs(c[1] - med_dx) <= 6 and abs(c[2] - med_dy) <= 6
            ]

            if len(agreeing) >= 2 and abs(med_dx) <= 20 and abs(med_dy) <= 20:
                agreement_score = float(np.mean([c[3] for c in agreeing]))
                combined = {
                    "dx": med_dx,
                    "dy": med_dy,
                    "score": agreement_score,
                    "support": len(agreeing),
                    "accepted": True,
                    "method": "cross_method_consensus",
                    "details": {
                        "agreeing_methods": [c[0] for c in agreeing],
                        "candidate_residuals": [
                            {"method": c[0], "dx": c[1], "dy": c[2], "score": c[3]}
                            for c in candidates
                        ],
                    },
                }
                return _combine_with_prior(
                    combined,
                    prior_shift,
                    "history_prior_cross_method_consensus",
                )

    if _cfg(config, "PRINT_DEBUG"):
        print("\n[HISTORY PRIOR RETRY RESULT]")
        print({
            "prior": prior_shift,
            "primary_result": primary_result,
            "patch_result": patch_result,
            "feature_result": feature_result,
        })

    return {
        "dx": prior_dx,
        "dy": prior_dy,
        "score": 0.0,
        "support": 0,
        "accepted": False,
        "method": "history_prior_retry_failed",
        "reason": "; ".join([
            _result_summary(primary_result, "A"),
            _result_summary(patch_result, "B"),
            _result_summary(feature_result, "C"),
        ]),
        "details": {
            "prior_dx": prior_dx,
            "prior_dy": prior_dy,
            "prior_samples": int(prior_shift.get("samples", 0)),
            "primary_result": primary_result,
            "patch_result": patch_result,
            "feature_result": feature_result,
        },
    }


def estimate_xy_plan_b(ref_img, mov_img, pair_hint=None, config=None, prior_shift=None):
    """
    Fallback alignment order:

    1. Directional patch consensus (Plan B)
    2. Feature-based AKAZE translation (Plan C)
    3. If both fail and history is available, pre-shift by the robust historical
       pair displacement and re-run Plan A, Plan B and Plan C on the residual.
    """
    patch_result = directional_patch_consensus(
        ref_img,
        mov_img,
        pair_hint=pair_hint,
        config=config,
    )

    if patch_result is not None and patch_result.get("accepted"):
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

    if feature_result is not None and feature_result.get("accepted"):
        return feature_result

    if prior_shift is not None:
        prior_result = prior_guided_retry(
            ref_img,
            mov_img,
            prior_shift=prior_shift,
            config=config,
        )
        if prior_result is not None and prior_result.get("accepted"):
            return prior_result
        if prior_result is not None:
            return prior_result

    # Preserve both failure diagnostics; do not hide Plan C behind Plan B.
    return {
        "dx": int((patch_result or feature_result or {}).get("dx", 0)),
        "dy": int((patch_result or feature_result or {}).get("dy", 0)),
        "score": float((feature_result or patch_result or {}).get("score", 0.0)),
        "support": int((feature_result or patch_result or {}).get("support", 0)),
        "accepted": False,
        "method": "plan_b_and_c_failed",
        "reason": "; ".join([
            _result_summary(patch_result, "B"),
            _result_summary(feature_result, "C"),
        ]),
        "details": {
            "patch_result": patch_result,
            "feature_result": feature_result,
        },
    }
