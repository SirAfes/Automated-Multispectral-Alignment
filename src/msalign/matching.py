"""
matching.py

Bounded local matching between two lens images around an expected shift.

Used to *check* a shift rather than to find one: the centre of the reference
lens (gradient image) is searched for in the other lens only within a small
window around the expected position. A correct shift gives the peak at the
expected position (residual near 0 px). Because the search is local, it cannot
lock onto a strong but unrelated match elsewhere in the image, and a peak on the
window edge shows that the true shift lies outside it.

Shift convention: `rel_dx, rel_dy` is the apply shift of `mov` relative to `ref`
(apply[mov] - apply[ref]), i.e. moving `mov` by it puts it onto `ref`.
"""

from __future__ import annotations

import cv2
import numpy as np

from .alignment import gradient_mag, to_gray_float


DOWNSCALE = 2
# Extra search radius beyond the accepted residual (full-resolution px).
SEARCH_MARGIN_PX = 4


class PreparedImages:
    """Lazily prepared (downscaled gradient) lens images, shared by several checks."""

    def __init__(self, images):
        self.images = images
        self._cache = {}

    def __getitem__(self, lens):
        if lens not in self._cache:
            img = self.images[lens]
            h, w = img.shape[:2]
            small = cv2.resize(img, (w // DOWNSCALE, h // DOWNSCALE), interpolation=cv2.INTER_AREA)
            self._cache[lens] = gradient_mag(to_gray_float(small))
        return self._cache[lens]


def _subpixel(values, i):
    """Parabolic peak refinement along one axis."""
    if 0 < i < len(values) - 1:
        a, b, c = values[i - 1], values[i], values[i + 1]
        denom = a - 2 * b + c
        if abs(denom) > 1e-9:
            return i + 0.5 * (a - c) / denom
    return float(i)


def match_at(prepared: PreparedImages, ref: int, mov: int, rel_dx: float, rel_dy: float,
             max_residual_px: float) -> dict:
    """
    Returns {"residual_dx", "residual_dy", "residual", "response", "at_edge"}.
    The residual is the correction to add to (rel_dx, rel_dy), in full-resolution px.
    """
    s = DOWNSCALE
    r = int(np.ceil((max_residual_px + SEARCH_MARGIN_PX) / s))
    ref_g = prepared[ref]
    mov_g = prepared[mov]
    h, w = ref_g.shape

    margin = int(np.ceil(max(abs(rel_dx), abs(rel_dy)) / s)) + r + int(0.12 * min(h, w))
    if 2 * margin >= min(h, w) - 32:
        raise ValueError(f"shift between lens {ref} and {mov} too large to check")
    template = np.ascontiguousarray(ref_g[margin:h - margin, margin:w - margin])

    # mov pixel (x, y) shows what ref shows at (x + rel_dx, y + rel_dy),
    # so the template sits at (margin - rel_dx, margin - rel_dy) in mov.
    ex = margin - rel_dx / s
    ey = margin - rel_dy / s
    x0, y0 = int(round(ex)) - r, int(round(ey)) - r
    region = mov_g[y0:y0 + template.shape[0] + 2 * r, x0:x0 + template.shape[1] + 2 * r]
    scores = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)

    _, peak, _, (px, py) = cv2.minMaxLoc(scores)
    sx = _subpixel(scores[py, :], px)
    sy = _subpixel(scores[:, px], py)
    rx = (ex - (x0 + sx)) * s
    ry = (ey - (y0 + sy)) * s
    return {
        "residual_dx": float(rx),
        "residual_dy": float(ry),
        "residual": float(np.hypot(rx, ry)),
        "response": float(peak),
        "at_edge": bool(px in (0, 2 * r) or py in (0, 2 * r)),
    }
