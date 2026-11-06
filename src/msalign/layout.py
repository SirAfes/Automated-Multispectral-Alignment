"""
layout.py

Geometry of the lens array: which lenses are neighbours, how the fallback
alignment chains them together, which pairs are left over for the closure
check, and where to search for each pair's shift.

Lenses are numbered row by row starting at 1. For the default 3 x 2 camera:

    1  2  3
    4  5  6
"""

from __future__ import annotations

from dataclasses import dataclass, field


Edge = tuple[int, int]


@dataclass(frozen=True)
class LensLayout:
    rows: int
    cols: int
    primary_reference: int = 1
    fallback_reference: int | None = None
    fallback_tree: tuple[Edge, ...] = field(init=False)

    def __post_init__(self):
        if self.rows < 1 or self.cols < 1 or self.rows * self.cols < 2:
            raise ValueError("A lens array needs at least 2 lenses")
        for name in ("primary_reference", "fallback_reference"):
            value = getattr(self, name)
            if value is not None and not 1 <= value <= self.count:
                raise ValueError(f"{name} must be between 1 and {self.count}")
        if self.fallback_reference is None:
            center = ((self.rows - 1) // 2) * self.cols + (self.cols - 1) // 2 + 1
            object.__setattr__(self, "fallback_reference", center)
        object.__setattr__(self, "fallback_tree", self._build_tree())

    # --- basic geometry -------------------------------------------------------

    @property
    def count(self) -> int:
        return self.rows * self.cols

    @property
    def lenses(self) -> list[int]:
        return list(range(1, self.count + 1))

    def position(self, lens: int) -> tuple[int, int]:
        return divmod(lens - 1, self.cols)

    def lens_at(self, row: int, col: int) -> int:
        return row * self.cols + col + 1

    @property
    def grid_edges(self) -> list[Edge]:
        """All horizontally or vertically adjacent lens pairs (a < b)."""
        edges = []
        for lens in self.lenses:
            row, col = self.position(lens)
            if col + 1 < self.cols:
                edges.append((lens, self.lens_at(row, col + 1)))
            if row + 1 < self.rows:
                edges.append((lens, self.lens_at(row + 1, col)))
        return edges

    # --- fallback chain -------------------------------------------------------

    def _build_tree(self) -> tuple[Edge, ...]:
        """
        Spanning tree used by the fallback alignment: a vertical spine through
        the reference lens, and horizontal branches from the spine outwards.
        Every lens is reached through short, adjacent-lens steps; parents always
        come before their children.

        Default 3 x 2 camera (reference 2): 2->5, 2->1, 2->3, 5->4, 5->6.
        """
        r0, c0 = self.position(self.fallback_reference)
        edges = []

        spine = [r0]
        for step in range(1, self.rows):
            for row in (r0 + step, r0 - step):
                if 0 <= row < self.rows:
                    parent_row = row - 1 if row > r0 else row + 1
                    edges.append((self.lens_at(parent_row, c0), self.lens_at(row, c0)))
                    spine.append(row)

        for row in spine:
            for col in range(c0 - 1, -1, -1):
                edges.append((self.lens_at(row, col + 1), self.lens_at(row, col)))
            for col in range(c0 + 1, self.cols):
                edges.append((self.lens_at(row, col - 1), self.lens_at(row, col)))

        return tuple(edges)

    # --- closure ---------------------------------------------------------------

    def closure_edges(self, measured: list[Edge]) -> list[Edge]:
        """Adjacent pairs whose shift was not measured directly - these are checked."""
        done = {tuple(sorted(e)) for e in measured}
        return [e for e in self.grid_edges if e not in done]

    # --- search windows --------------------------------------------------------

    def search_window(self, ref: int, mov: int, parallax_range, cross: float) -> dict:
        """
        Where the raw shift (ref -> mov) may lie. The shift points in the
        direction of the baseline between the two lenses: along the baseline it
        is between parallax_range[0] and [1] per lens step; across it, within
        +/- cross pixels.
        """
        (r1, c1), (r2, c2) = self.position(ref), self.position(mov)
        lo, hi = float(parallax_range[0]), float(parallax_range[1])

        def axis(steps):
            if steps == 0:
                return -cross, cross
            a, b = steps * lo, steps * hi
            return min(a, b), max(a, b)

        dx_min, dx_max = axis(c2 - c1)
        dy_min, dy_max = axis(r2 - r1)
        return {
            "dx_min": int(dx_min), "dx_max": int(dx_max),
            "dy_min": int(dy_min), "dy_max": int(dy_max),
        }


def layout_from_settings(settings: dict) -> LensLayout:
    return LensLayout(
        rows=int(settings["grid_rows"]),
        cols=int(settings["grid_cols"]),
        primary_reference=int(settings["primary_reference_lens"]),
        fallback_reference=settings.get("fallback_reference_lens"),
    )
