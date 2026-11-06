import time

import numpy as np
import pytest

from conftest import CELL, make_compound
from msalign.alignment import gradient_mag, local_search_ncc, local_search_ncc_reference, to_gray_float
from msalign.split import split_compound_array


@pytest.fixture(scope="module")
def gradients():
    img = (make_compound(5) * 255).astype(np.uint8)
    cells = split_compound_array(img, cell_size=CELL)[0]
    return {k: gradient_mag(to_gray_float(v)) for k, v in cells.items()}


def test_fast_local_search_matches_reference(gradients):
    rng = np.random.default_rng(1)
    ref, mov = gradients[1], gradients[5]
    for _ in range(40):
        ps = int(rng.integers(40, 90))
        x0, y0 = (int(v) for v in rng.integers(0, ref.shape[0] - ps, 2))
        patch = ref[y0:y0 + ps, x0:x0 + ps]
        cdx, cdy = (float(v) for v in rng.uniform(-40, 40, 2))
        if abs(cdx % 1 - 0.5) < 1e-6 or abs(cdy % 1 - 0.5) < 1e-6:
            continue  # exact .5 rounding ties are ambiguous in the reference loop
        dmax = int(rng.integers(3, 15))
        fast = local_search_ncc(patch, mov, x0, y0, cdx, cdy, dmax, dmax)
        slow = local_search_ncc_reference(patch, mov, x0, y0, cdx, cdy, dmax, dmax)
        assert (fast["dx"], fast["dy"]) == (slow["dx"], slow["dy"])
        if slow["dx"] is not None:
            assert fast["raw_ncc"] == pytest.approx(slow["raw_ncc"], abs=1e-3)
            assert fast["score"] == pytest.approx(slow["score"], abs=1e-3)


def test_fast_local_search_is_much_faster(gradients):
    ref, mov = gradients[1], gradients[4]
    patch = ref[100:180, 100:180]
    t = time.perf_counter()
    local_search_ncc_reference(patch, mov, 100, 100, 3.2, -2.7, 20, 20)
    slow = time.perf_counter() - t
    t = time.perf_counter()
    local_search_ncc(patch, mov, 100, 100, 3.2, -2.7, 20, 20)
    fast = time.perf_counter() - t
    assert fast * 10 < slow
