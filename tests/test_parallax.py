import json
from pathlib import Path

import numpy as np
import tifffile

from conftest import make_compound, make_compound_depth, write_jpg

CELL = 1280  # realistic proportions for tile statistics
from msalign.pipeline import process_single_file
from msalign.settings import load_settings


def settings_for(tmp_path, **overrides):
    return load_settings(tmp_path / "none.json", {"cell_size": CELL, **overrides})


def _peak_offset(a, b, r=12):
    a = (a - a.mean()) / (a.std() + 1e-9)
    best, arg = -9, None
    h, w = a.shape
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            bb = b[r + dy:h - r + dy, r + dx:w - r + dx]
            bb = (bb - bb.mean()) / (bb.std() + 1e-9)
            c = float((a[r:h - r, r:w - r] * bb).mean())
            if c > best:
                best, arg = c, (dx, dy)
    return arg


def test_check_reports_depth_parallax(tmp_path):
    src = write_jpg(tmp_path / "D.JPG", make_compound_depth(3, extra=8, cell=CELL))
    result = process_single_file(src, settings=settings_for(tmp_path))
    local = result["output"]["local_alignment"]
    assert local["mode"] == "check"
    assert local["worst_p90_px"] > 6
    assert "consider parallax_correction" in Path(result["quality_report"]).read_text()


def test_parallax_correction_aligns_the_nearer_block(tmp_path):
    extra = 8
    img = make_compound_depth(3, extra=extra, cell=CELL)
    off = process_single_file(write_jpg(tmp_path / "OFF.JPG", img),
                              settings=settings_for(tmp_path, parallax_correction=False))
    on = process_single_file(write_jpg(tmp_path / "ON.JPG", img),
                             settings=settings_for(tmp_path, parallax_correction=True))

    local = on["output"]["local_alignment"]
    assert local["parallax_applied"]
    assert local["after"]["worst_p90_px"] < local["before"]["worst_p90_px"]
    assert local["after"]["worst_p90_px"] < 3

    # 850 nm (lens 1, R) vs 630 nm (lens 2, R): same scene channel, one lens step apart.
    order = load_settings(tmp_path / "none.json")["output_order"]
    def offsets(result):
        data = tifffile.imread(result["tiff"]).astype(np.float32)
        h, w = data.shape[1:]
        out = []
        # one region inside the nearer block, one in the background
        for fy, fx in ((0.38, 0.33), (0.02, 0.40)):
            y0, x0 = int(h * fy), int(w * fx)
            a = data[order.index("850"), y0:y0 + 120, x0:x0 + 120]
            b = data[order.index("630"), y0:y0 + 120, x0:x0 + 120]
            out.append(_peak_offset(a, b))
        return out

    worst = lambda offs: max(max(abs(o[0]), abs(o[1])) for o in offs)  # noqa: E731
    assert worst(offsets(off)) >= 5      # one depth layer is misaligned by ~one lens step of parallax
    assert worst(offsets(on)) <= 1       # both aligned after the correction

    pmap = tifffile.imread(Path(on["output_dir"]) / (Path(on["tiff"]).stem + "_parallax.tif"))
    h, w = pmap.shape
    inside = np.median(pmap[int(h * 0.35):int(h * 0.5), int(w * 0.30):int(w * 0.5)])
    outside = np.median(pmap[int(h * 0.8):, int(w * 0.8):])
    assert abs((inside - outside) - extra) < 2.5  # larger values = nearer surfaces

    meta = json.loads(Path(on["metadata_json"]).read_text())
    assert meta["output"]["local_alignment"]["parallax_applied"] is True


def test_flat_scene_is_left_alone(tmp_path):
    src = write_jpg(tmp_path / "F.JPG", make_compound(4, cell=CELL))
    result = process_single_file(src, settings=settings_for(tmp_path, parallax_correction=True))
    local = result["output"]["local_alignment"]
    assert local["before"]["worst_p90_px"] < 1.5  # global alignment is already exact
    if local["parallax_applied"]:
        pmap = tifffile.imread(Path(result["output_dir"]) / (Path(result["tiff"]).stem + "_parallax.tif"))
        assert np.percentile(np.abs(pmap), 95) < 1.0
