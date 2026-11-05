"""
batch.py

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

import numpy as np

from .matching import PreparedImages, match_at


def calibration_indices(total: int, samples: int) -> list[int]:
    """Evenly spaced indices covering the whole batch, first and last included."""
    if total <= samples:
        return list(range(total))
    return sorted({int(round(i * (total - 1) / (samples - 1))) for i in range(samples)})


def _relative_shifts(alignment, reference) -> dict[int, tuple[int, int]]:
    """Apply shifts relative to the reference lens (comparable across alignment modes)."""
    s = alignment["apply_shifts"]
    r = s[reference]
    return {
        k: (int(v["dx"]) - int(r["dx"]), int(v["dy"]) - int(r["dy"]))
        for k, v in s.items()
    }


def compute_consensus(alignments, settings, reference: int = 1) -> dict:
    """
    Robust per-lens consensus from successful calibration alignments.

    Returns a dict with:
        consistent:  bool, whether the consensus may be used for the batch
        shifts:      {lens: {"dx", "dy"}} relative to lens 1
        successful, agreeing, agreeing_mask, tolerance_px, spread_px, reason
    """
    tol = float(settings["batch_consensus_tolerance_px"])
    rel = [_relative_shifts(a, reference) for a in alignments]
    n = len(rel)
    lenses = sorted(rel[0]) if rel else []

    result = {
        "consistent": False,
        "shifts": None,
        "successful": n,
        "agreeing": 0,
        "agreeing_mask": [],
        "tolerance_px": tol,
        "spread_px": {},
        "reason": "",
        "reference": reference,
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
            for k in lenses
        }

    med = medians(rel)
    mask = [
        all(abs(r[k][0] - med[k][0]) <= tol and abs(r[k][1] - med[k][1]) <= tol for k in lenses)
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
    result["shifts"] = {k: {"dx": int(round(med[k][0])), "dy": int(round(med[k][1]))} for k in lenses}
    result["spread_px"] = {
        k: float(max(np.hypot(r[k][0] - med[k][0], r[k][1] - med[k][1]) for r in agreeing))
        for k in lenses
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

def verify_shifts(images, shifts, max_residual_px: float = 5.0, reference: int = 1,
                  prepared: PreparedImages | None = None) -> dict[int, dict]:
    """
    Check given shifts (apply shifts relative to `reference`) on one image set
    with a bounded local match per lens (see matching.py).

    Returns {lens: {"residual_dx", "residual_dy", "residual", "response",
    "at_edge"}} for every lens except the reference (full-resolution px).
    """
    prepared = prepared or PreparedImages(images)
    out = {}
    for k in images:
        if k == reference:
            continue
        rel_dx = shifts[k]["dx"] - shifts[reference]["dx"]
        rel_dy = shifts[k]["dy"] - shifts[reference]["dy"]
        out[k] = match_at(prepared, reference, k, rel_dx, rel_dy, max_residual_px)
    return out


def response_thresholds(verifications, settings) -> dict[int, float]:
    """
    Data-driven minimum verification response per lens: a fraction of the
    median response measured on calibration images whose shifts are known good.
    """
    ratio = float(settings["batch_verify_min_response_ratio"])
    thresholds = {}
    lenses = sorted({k for v in verifications for k in v})
    for k in lenses:
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
    ref = consensus["reference"]
    shifts = {ref: {"dx": 0, "dy": 0}}
    for k, v in verification.items():
        shifts[k] = {
            "dx": consensus["shifts"][k]["dx"] + int(round(v["residual_dx"])),
            "dy": consensus["shifts"][k]["dy"] + int(round(v["residual_dy"])),
        }

    pairs = {}
    for k, v in verification.items():
        pairs[f"{ref}-{k}"] = {
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
        "reference_id": ref,
        "mode": "batch_consensus",
        "pairs": pairs,
        "measured_edges": [(ref, k) for k in verification],
        "apply_shifts": shifts,
        "history_priors_available": 0,
        "history_retry_used": False,
        "batch_consensus": {
            "agreeing": consensus["agreeing"],
            "successful": consensus["successful"],
            "tolerance_px": consensus["tolerance_px"],
        },
    }
