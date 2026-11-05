"""
closure.py

Closure check: an independent test of a finished alignment.

The alignment measures only some lens pairs directly (for example 2->1, 2->3,
2->5, 5->4, 5->6 in the fallback chain); all other relative shifts follow from
those. The check looks at the image again with one common, independent metric
(bounded local NCC on gradient images, see matching.py):

- closure pairs: adjacent lenses whose shift was *not* measured directly
  (1-4 and 3-6 in the example). An error anywhere in the chain shows up here.
- measured pairs: the pairs the alignment itself measured, re-checked with the
  same metric, so every pair's evidence is judged on the same scale.

A pair fails when the best local match is not at the implied shift (residual
above `closure_tolerance_px`, or the best match lies outside the search
window), or when the implied shift has far weaker support than the other pairs
of the same image (NCC below `closure_relative_ncc` x the median NCC of the
measured pairs). Comparing within the image keeps the test fair for spectrally
very different bands. If the whole image has too little shared structure
(median NCC below `closure_min_ncc`), the check is "inconclusive" and does
not reject anything.
"""

from __future__ import annotations

import numpy as np

from .matching import PreparedImages, match_at


def _measure(prepared, a, b, apply_shifts, tol):
    rel_dx = apply_shifts[b]["dx"] - apply_shifts[a]["dx"]
    rel_dy = apply_shifts[b]["dy"] - apply_shifts[a]["dy"]
    row = {
        "pair": f"{a}-{b}",
        "predicted_dx": int(-rel_dx),
        "predicted_dy": int(-rel_dy),
        "residual_dx": 0.0,
        "residual_dy": 0.0,
        "residual": 0.0,
        "ncc": 0.0,
        "at_edge": False,
        "measurable": False,
    }
    try:
        m = match_at(prepared, a, b, rel_dx, rel_dy, tol)
    except ValueError:
        return row
    row.update({
        "residual_dx": round(m["residual_dx"], 2),
        "residual_dy": round(m["residual_dy"], 2),
        "residual": round(m["residual"], 2),
        "ncc": round(m["response"], 4),
        "at_edge": m["at_edge"],
        "measurable": True,
    })
    return row


def check_closure(images, apply_shifts, layout, measured_edges, settings, prepared=None) -> list[dict]:
    """
    Returns one row per checked pair:
        pair, kind ("closure" | "measured"), predicted_dx, predicted_dy (raw shift
        ref -> mov implied by the alignment), residual_dx, residual_dy, residual,
        ncc, status ("pass" | "fail" | "inconclusive"), note
    """
    if settings["closure_check"] == "off":
        return []

    tol = float(settings["closure_tolerance_px"])
    min_ncc = float(settings["closure_min_ncc"])
    ratio = float(settings["closure_relative_ncc"])
    prepared = prepared or PreparedImages(images)

    rows = [dict(_measure(prepared, a, b, apply_shifts, tol), kind="measured") for a, b in measured_edges]
    rows += [
        dict(_measure(prepared, a, b, apply_shifts, tol), kind="closure")
        for a, b in layout.closure_edges(measured_edges)
    ]

    levels = [r["ncc"] for r in rows if r["kind"] == "measured" and r["measurable"]]
    level = float(np.median(levels)) if levels else 0.0
    weak = ratio * level

    for r in rows:
        if not r["measurable"]:
            r["status"], r["note"] = "inconclusive", "shift too large to check"
        elif level < min_ncc:
            r["status"], r["note"] = "inconclusive", "too little shared structure in this image"
        elif r["at_edge"]:
            r["status"], r["note"] = "fail", "best match lies outside the search window"
        elif r["residual"] > tol:
            r["status"], r["note"] = "fail", f"off by {r['residual']:.1f} px"
        elif r["ncc"] < weak:
            r["status"], r["note"] = "fail", f"no match at implied shift (ncc {r['ncc']:.2f} < {weak:.2f})"
        else:
            r["status"], r["note"] = "pass", ""
        r.pop("at_edge")
        r.pop("measurable")
    return rows


def closure_failed(rows) -> bool:
    return any(r["status"] == "fail" for r in rows)


def describe_failures(rows) -> str:
    parts = [f"{r['pair']} {r['note']}" for r in rows if r["status"] == "fail"]
    return "closure check failed: " + "; ".join(parts)
