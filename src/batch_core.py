"""
batch_core.py

Batch consensus mode for large sets of images taken with the same camera at the
same distance.

Idea:
    The lens-to-lens displacement is fixed physical geometry plus a parallax
    term that depends on the subject distance. If a set of images was taken at
    (nearly) the same distance, the correct shifts are (nearly) the same for
    every image. So instead of running the full search on every image:

    1. Calibration: fully align an evenly spaced sample of the batch.
    2. Consensus: take the per-lens median shift of the calibration images and
       check that the calibration images agree with it.
    3. Apply: for every other image, verify the consensus shift on that image
       (fast residual check) and use it. Images that fail the check get the
       normal full alignment, so a changed distance or a bad frame is never
       forced onto the consensus.
"""

from __future__ import annotations

import cv2
import numpy as np

from alignment_core import gradient_mag, to_gray_float


LENSES = range(1, 7)


def calibration_indices(total: int, samples: int) -> list[int]:
    """Evenly spaced indices covering the whole batch, first and last included."""
    if total <= samples:
        return list(range(total))
    return sorted({int(round(i * (total - 1) / (samples - 1))) for i in range(samples)})


def _relative_shifts(alignment) -> dict[int, tuple[int, int]]:
    """Apply shifts expressed relative to lens 1 (both primary and Plan B results)."""
    s = alignment["apply_shifts"]
    return {
        k: (int(s[k]["dx"]) - int(s[1]["dx"]), int(s[k]["dy"]) - int(s[1]["dy"]))
        for k in LENSES
    }


def compute_consensus(alignments, settings) -> dict:
    """
    Robust per-lens consensus from successful calibration alignments.

    Returns a dict with:
        consistent:  bool, whether the consensus may be used for the batch
        shifts:      {lens: {"dx", "dy"}} relative to lens 1
        successful, agreeing, agreeing_mask, tolerance_px, spread_px, reason
    """
    tol = float(settings["batch_consensus_tolerance_px"])
    rel = [_relative_shifts(a) for a in alignments]
    n = len(rel)

    result = {
        "consistent": False,
        "shifts": None,
        "successful": n,
        "agreeing": 0,
        "agreeing_mask": [],
        "tolerance_px": tol,
        "spread_px": {},
        "reason": "",
    }

    if n < int(settings["batch_min_successful_samples"]):
        result["reason"] = (
            f"only {n} calibration image(s) aligned successfully "
            f"(need {settings['batch_min_successful_samples']})"
        )
        return result

    def medians(rows):
        return {
            k: (
                float(np.median([r[k][0] for r in rows])),
                float(np.median([r[k][1] for r in rows])),
            )
            for k in LENSES
        }

    med = medians(rel)
    mask = [
        all(abs(r[k][0] - med[k][0]) <= tol and abs(r[k][1] - med[k][1]) <= tol for k in LENSES)
        for r in rel
    ]
    agreeing = [r for r, ok in zip(rel, mask) if ok]
    result["agreeing"] = len(agreeing)
    result["agreeing_mask"] = mask

    if not agreeing:
        result["reason"] = (
            "calibration images do not agree on a common lens geometry "
            "- the batch probably contains different distances"
        )
        return result

    med = medians(agreeing)
    result["shifts"] = {k: {"dx": int(round(med[k][0])), "dy": int(round(med[k][1]))} for k in LENSES}
    result["spread_px"] = {
        k: float(max(np.hypot(r[k][0] - med[k][0], r[k][1] - med[k][1]) for r in agreeing))
        for k in LENSES
    }

    agreement = len(agreeing) / n
    if agreement < float(settings["batch_min_agreement"]):
        result["reason"] = (
            f"only {len(agreeing)}/{n} calibration images agree within {tol:g} px "
            "- the batch probably contains different distances"
        )
        return result

    result["consistent"] = True
    result["reason"] = f"{len(agreeing)}/{n} calibration images agree within {tol:g} px"
    return result


# ---------------------------------------------------------------------------
# Per-image verification
# ---------------------------------------------------------------------------

VERIFY_DOWNSCALE = 2
# Extra search radius beyond the accepted residual (full-resolution px), so a
# peak at the edge of the search window (= true shift outside it) can be
# recognised and rejected.
VERIFY_SEARCH_MARGIN_PX = 4


def _prep_for_verify(img):
    h, w = img.shape[:2]
    small = cv2.resize(
        img,
        (w // VERIFY_DOWNSCALE, h // VERIFY_DOWNSCALE),
        interpolation=cv2.INTER_AREA,
    )
    return gradient_mag(to_gray_float(small))


def _subpixel(values, i):
    """Parabolic peak refinement along one axis."""
    if 0 < i < len(values) - 1:
        a, b, c = values[i - 1], values[i], values[i + 1]
        denom = a - 2 * b + c
        if abs(denom) > 1e-9:
            return i + 0.5 * (a - c) / denom
    return float(i)


def verify_shifts(images, shifts, max_residual_px: float = 5.0) -> dict[int, dict]:
    """
    Check given shifts (relative to lens 1) on one image set.

    For each lens, the centre of lens 1 (gradient image) is searched for in the
    lens image within a small window around the expected position (normalised
    cross-correlation). A correct shift gives a peak at the expected position,
    i.e. a residual near 0 px. Searching only locally avoids picking up a
    strong but unrelated match elsewhere in the image.

    Returns {lens: {"residual_dx", "residual_dy", "residual", "response",
    "at_edge"}} for lens 2..6 (residuals in full-resolution pixels).
    """
    s = VERIFY_DOWNSCALE
    r = int(np.ceil((max_residual_px + VERIFY_SEARCH_MARGIN_PX) / s))
    ref = _prep_for_verify(images[1])
    h, w = ref.shape

    max_shift = max(max(abs(v["dx"]), abs(v["dy"])) for v in shifts.values()) / s
    margin = int(np.ceil(max_shift)) + r + int(0.12 * min(h, w))
    if 2 * margin >= min(h, w) - 32:
        raise ValueError("shifts too large for verification")
    template = np.ascontiguousarray(ref[margin:h - margin, margin:w - margin])

    out = {}
    for k in LENSES:
        if k == 1:
            continue
        mov = _prep_for_verify(images[k])
        # Lens k pixel (x, y) shows what lens 1 shows at (x + dx, y + dy);
        # the template therefore sits at (margin - dx, margin - dy) in lens k.
        ex = margin - shifts[k]["dx"] / s
        ey = margin - shifts[k]["dy"] / s
        x0, y0 = int(round(ex)) - r, int(round(ey)) - r
        region = mov[y0:y0 + template.shape[0] + 2 * r, x0:x0 + template.shape[1] + 2 * r]
        scores = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)

        _, peak, _, (px, py) = cv2.minMaxLoc(scores)
        sx = _subpixel(scores[py, :], px)
        sy = _subpixel(scores[:, px], py)
        found_x, found_y = x0 + sx, y0 + sy
        # residual = correction that would have to be added to the apply shift
        rx = (ex - found_x) * s
        ry = (ey - found_y) * s
        out[k] = {
            "residual_dx": float(rx),
            "residual_dy": float(ry),
            "residual": float(np.hypot(rx, ry)),
            "response": float(peak),
            "at_edge": bool(px in (0, 2 * r) or py in (0, 2 * r)),
        }
    return out


def response_thresholds(verifications, settings) -> dict[int, float]:
    """
    Data-driven minimum verification response per lens: a fraction of the
    median response measured on calibration images whose shifts are known good.
    """
    ratio = float(settings["batch_verify_min_response_ratio"])
    thresholds = {}
    for k in LENSES:
        if k == 1:
            continue
        values = [v[k]["response"] for v in verifications if k in v]
        thresholds[k] = ratio * float(np.median(values)) if values else 0.0
    return thresholds


def check_verification(verification, thresholds, settings) -> tuple[bool, str]:
    max_res = float(settings["batch_verify_max_residual_px"])
    problems = []
    for k, v in verification.items():
        if v.get("at_edge"):
            problems.append(f"lens {k} best match outside the search window")
        elif v["residual"] > max_res:
            problems.append(f"lens {k} residual {v['residual']:.1f} px")
        elif v["response"] < thresholds.get(k, 0.0):
            problems.append(f"lens {k} weak match ({v['response']:.3f} < {thresholds[k]:.3f})")
    if problems:
        return False, "consensus shift not confirmed: " + "; ".join(problems)
    return True, "ok"


def consensus_alignment_result(consensus, verification) -> dict:
    """
    Alignment result in the same structure stack_core uses for its reports.
    Final shift per lens = consensus shift + verified residual (rounded).
    """
    # The verified residual is measured precisely, so it is added as a small
    # per-image refinement: consensus = anchor, residual = local correction.
    shifts = {1: {"dx": 0, "dy": 0}}
    for k in LENSES:
        if k == 1:
            continue
        v = verification[k]
        shifts[k] = {
            "dx": consensus["shifts"][k]["dx"] + int(round(v["residual_dx"])),
            "dy": consensus["shifts"][k]["dy"] + int(round(v["residual_dy"])),
        }

    pairs = {}
    for k in LENSES:
        if k == 1:
            continue
        v = verification[k]
        pairs[f"1-{k}"] = {
            "raw_dx": -shifts[k]["dx"],
            "raw_dy": -shifts[k]["dy"],
            "apply_dx": shifts[k]["dx"],
            "apply_dy": shifts[k]["dy"],
            "confidence": v["response"],
            "support": consensus["agreeing"],
            "num_votes": consensus["successful"],
            "reason": (
                f"consensus=({consensus['shifts'][k]['dx']}, {consensus['shifts'][k]['dy']}), "
                f"residual=({v['residual_dx']:.2f}, {v['residual_dy']:.2f})px"
            ),
            "method": "batch_consensus",
            "algorithm": "batch_consensus_verified",
            "history_prior_used": False,
            "details": {
                "residual_dx": int(round(v["residual_dx"])),
                "residual_dy": int(round(v["residual_dy"])),
                "prior_samples": consensus["agreeing"],
            },
        }

    return {
        "reference_id": 1,
        "mode": "batch_consensus",
        "pairs": pairs,
        "raw_shifts": {},
        "apply_shifts": shifts,
        "history_priors_available": 0,
        "history_retry_used": False,
        "batch_consensus": {
            "agreeing": consensus["agreeing"],
            "successful": consensus["successful"],
            "tolerance_px": consensus["tolerance_px"],
        },
    }
