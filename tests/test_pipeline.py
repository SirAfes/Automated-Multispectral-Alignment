import json
from pathlib import Path

import numpy as np
import pytest
import tifffile

from conftest import CELL, SHIFTS, make_compound, write_fake_raw, write_jpg
from batch_core import calibration_indices, compute_consensus
from image_io import file_dialog_types
from pipeline_core import (
    collect_folder_images,
    output_paths,
    process_single_file,
    run_pipeline,
    sort_paths,
)
from settings import load_settings
from split_core import split_compound_array


def relative_shifts(alignment):
    s = alignment["apply_shifts"]
    return [(s[k]["dx"] - s[1]["dx"], s[k]["dy"] - s[1]["dy"]) for k in range(1, 7)]


def read_bands(path):
    with tifffile.TiffFile(path) as tif:
        return np.stack([page.asarray() for page in tif.pages])


def assert_shifts(alignment, expected=SHIFTS, tol=1):
    got = relative_shifts(alignment)
    for k, (g, e) in enumerate(zip(got, expected), start=1):
        assert abs(g[0] - e[0]) <= tol and abs(g[1] - e[1]) <= tol, f"lens {k}: got {g}, expected {e}"


# --- splitting --------------------------------------------------------------

def test_split_fixed_grid():
    img = np.zeros((2 * CELL, 3 * CELL, 3), np.uint8)
    cells, info = split_compound_array(img, cell_size=CELL)
    assert info["split_mode"] == "fixed_grid"
    assert all(c.shape[:2] == (int(CELL * 0.8),) * 2 for c in cells.values())


def test_split_larger_source_is_not_resampled():
    # A RAW decoder delivering a few extra pixels must not change the pixel scale.
    img = np.random.default_rng(0).integers(0, 65535, (2 * CELL + 10, 3 * CELL + 18, 3)).astype(np.uint16)
    cells, info = split_compound_array(img, cell_size=CELL)
    assert info["split_mode"] == "proportional"
    target = int(CELL * 0.8)
    assert cells[1].shape[:2] == (target, target)
    assert cells[1].dtype == np.uint16
    # exact centre crop of the first cell, no interpolation
    h, w = round(img.shape[0] / 2), round(img.shape[1] / 3)
    y0, x0 = (h - target) // 2, (w - target) // 2
    assert np.array_equal(cells[1], img[y0:y0 + target, x0:x0 + target])


def test_split_smaller_source_is_resized():
    img = np.zeros((2 * 300, 3 * 300, 3), np.uint8)
    cells, info = split_compound_array(img, cell_size=CELL)
    assert info["split_mode"] == "proportional_resized"
    assert cells[1].shape[:2] == (int(CELL * 0.8),) * 2


# --- single image -----------------------------------------------------------

def test_jpg_recovers_known_shifts(tmp_path, settings):
    src = write_jpg(tmp_path / "DSC0001.JPG", make_compound(1))
    result = process_single_file(src, settings=settings)
    assert_shifts(result["alignment"])
    tiff = read_bands(result["tiff"])
    assert tiff.shape[0] == 14 and tiff.dtype == np.uint8
    assert (tmp_path / "tiff" / Path(result["tiff"]).name).is_file()


def test_raw_is_16bit_and_matches(tmp_path, settings, fake_rawpy):
    src = write_fake_raw(tmp_path / "DSC0001.ARW", make_compound(1))
    result = process_single_file(src, settings=settings)
    assert_shifts(result["alignment"])
    assert Path(result["output_dir"]).name == "DSC0001_ARW"
    assert sorted(p.name for p in output_paths(src)["split_dir"].iterdir())[0] == "1.tif"
    bands = read_bands(result["tiff"])
    assert bands.shape[0] == 14 and bands.dtype == np.uint16


def test_raw_without_rawpy_gives_clear_error(tmp_path, settings, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "rawpy", None)
    src = tmp_path / "DSC0001.ARW"
    src.write_bytes(b"x")
    with pytest.raises(ImportError, match="rawpy"):
        process_single_file(src, settings=settings)


def test_non_ascii_path(tmp_path, settings):
    folder = tmp_path / "çalışma_ğörüntü"
    folder.mkdir()
    src = write_jpg(folder / "DSC0001.JPG", make_compound(1))
    # cv2.imwrite cannot handle such paths on Windows; write via imencode instead
    result = process_single_file(src, settings=settings)
    assert Path(result["tiff"]).is_file()


def test_collection_hardlink(tmp_path, settings):
    src = write_jpg(tmp_path / "DSC0001.JPG", make_compound(1))
    result = process_single_file(src, settings=settings)
    a, b = Path(result["tiff"]), Path(result["tiff_collection_copy"])
    assert a.stat().st_ino == b.stat().st_ino  # same file, no extra disk space


# --- batch ------------------------------------------------------------------

def test_calibration_indices_cover_batch():
    idx = calibration_indices(600, 24)
    assert idx[0] == 0 and idx[-1] == 599 and len(idx) == 24


def test_consensus_detects_mixed_distances(settings):
    def alignment(shifts):
        return {"apply_shifts": {k + 1: {"dx": dx, "dy": dy} for k, (dx, dy) in enumerate(shifts)}}

    near = [(0, 0), (40, -20), (-30, 15), (60, 9), (-12, -45), (21, 33)]
    same = [alignment(SHIFTS)] * 8
    assert compute_consensus(same, settings)["consistent"]

    mixed = [alignment(SHIFTS)] * 4 + [alignment(near)] * 4
    result = compute_consensus(mixed, settings)
    assert not result["consistent"]
    assert "different distances" in result["reason"]


def test_batch_mode_applies_consensus_and_catches_outlier(tmp_path, settings):
    paths = []
    near = [(0, 0), (16, -10), (-12, 8), (26, 5), (-6, -19), (10, 14)]
    for i in range(12):
        shifts = SHIFTS
        if i == 5:  # one frame shot at another distance
            shifts = near
        # jitter: small per-image differences that the verified refinement must follow
        paths.append(write_jpg(tmp_path / f"DSC{i + 1:04d}.JPG", make_compound(i, shifts)))

    events = []
    success, failed = run_pipeline(paths, events.append, settings=settings)

    assert failed == []
    assert ("mode", "Batch consensus mode") in events
    calib = set(calibration_indices(12, settings["batch_calibration_samples"]))
    assert 5 not in calib

    for i, record in success.items():
        if i == 5:
            # must NOT get the consensus; full alignment finds its own geometry
            assert record["alignment"]["mode"] != "batch_consensus"
            assert_shifts(record["alignment"], near)
        elif i in calib:
            assert record["alignment"]["mode"] != "batch_consensus"
        else:
            assert record["alignment"]["mode"] == "batch_consensus"
            assert record["quality"] in {"high", "medium"}
            assert_shifts(record["alignment"])
            # split images are not kept for consensus images by default
            assert not output_paths(Path(record["input"]))["split_dir"].exists()

    report = Path(success[1]["quality_report"]).read_text()
    assert "Batch consensus" in report


def test_small_selection_uses_per_image_mode(tmp_path, settings):
    paths = [write_jpg(tmp_path / f"DSC{i}.JPG", make_compound(i)) for i in range(2)]
    events = []
    run_pipeline(paths, events.append, settings=settings)
    assert ("mode", "Per-image alignment") in events


# --- inputs and settings ----------------------------------------------------

def test_natural_sort():
    names = ["DSC10.JPG", "DSC9.JPG", "DSC100.JPG", "DSC2.JPG"]
    assert [p.name for p in sort_paths(names)] == ["DSC2.JPG", "DSC9.JPG", "DSC10.JPG", "DSC100.JPG"]


def test_folder_scan_prefers_raw_and_skips_subfolders(tmp_path):
    for name in ["A1.JPG", "A1.ARW", "A2.jpg", "notes.txt", "B3.raw"]:
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "A1_ARW").mkdir()
    (tmp_path / "A1_ARW" / "1.jpg").write_bytes(b"x")
    assert [p.name for p in collect_folder_images(tmp_path)] == ["A1.ARW", "A2.jpg", "B3.raw"]


def test_file_dialog_lists_raw():
    patterns = file_dialog_types()[0][1]
    assert "*.arw" in patterns and "*.ARW" in patterns and "*.raw" in patterns


def test_settings_file_and_validation(tmp_path):
    cfg = tmp_path / "pipeline_config.json"
    cfg.write_text(json.dumps({"_comment": "x", "batch_threshold": 100}))
    assert load_settings(cfg)["batch_threshold"] == 100

    cfg.write_text(json.dumps({"batch_treshold": 100}))
    with pytest.raises(ValueError, match="Unknown setting"):
        load_settings(cfg)

    cfg.write_text(json.dumps({"output_order": ["405", "999"]}))
    with pytest.raises(ValueError, match="999"):
        load_settings(cfg)


def test_example_config_is_valid():
    example = Path(__file__).resolve().parent.parent / "pipeline_config.example.json"
    load_settings(example)


def test_batch_consensus_follows_small_per_image_differences(tmp_path, settings):
    # Same distance, but every frame differs by up to 2 px (hand-held, slight
    # depth changes). The verified refinement must give each frame its own shift.
    rng = np.random.default_rng(42)
    truth = {}
    paths = []
    for i in range(10):
        shifts = [(0, 0)] + [
            (dx + int(rng.integers(-2, 3)), dy + int(rng.integers(-2, 3))) for dx, dy in SHIFTS[1:]
        ]
        truth[i] = shifts
        paths.append(write_jpg(tmp_path / f"IMG{i:03d}.JPG", make_compound(100 + i, shifts)))

    success, failed = run_pipeline(paths, lambda e: None, settings=settings)
    assert failed == []
    consensus_used = [i for i, r in success.items() if r["alignment"]["mode"] == "batch_consensus"]
    assert len(consensus_used) >= 4
    errors = {}
    for i, record in success.items():
        got = relative_shifts(record["alignment"])
        errors[i] = (record["alignment"]["mode"], max(
            max(abs(g[0] - t[0]), abs(g[1] - t[1])) for g, t in zip(got, truth[i])
        ))
    print(errors)
    assert all(err <= 1 for _, err in errors.values()), errors
    assert all(err == 0 for mode, err in errors.values() if mode == "batch_consensus"), errors


def test_consensus_ignores_single_bad_calibration_result(settings):
    def alignment(shifts):
        return {"apply_shifts": {k + 1: {"dx": dx, "dy": dy} for k, (dx, dy) in enumerate(shifts)}}

    wrong = list(SHIFTS)
    wrong[5] = (-118, -17)  # one lens pair wrongly accepted by the full search
    result = compute_consensus([alignment(SHIFTS)] * 7 + [alignment(wrong)], settings)
    assert result["consistent"] and result["agreeing"] == 7
    assert result["shifts"][6] == {"dx": SHIFTS[5][0], "dy": SHIFTS[5][1]}
