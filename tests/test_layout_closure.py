import csv
from pathlib import Path

import numpy as np
import pytest

from conftest import CELL, SHIFTS, make_compound, physical_shifts, write_jpg
from msalign.cli import main as cli_main
from msalign.layout import LensLayout
from msalign.pipeline import process_single_file, run_pipeline
from msalign.settings import load_settings
from msalign.split import split_compound_array
from msalign.stack import compute_plan_b_alignment, estimate_alignment_from_images, run_closure_check


# --- layout -----------------------------------------------------------------

def test_default_layout_reproduces_original_chain():
    layout = LensLayout(rows=2, cols=3)
    assert layout.fallback_reference == 2
    assert set(layout.fallback_tree) == {(2, 1), (2, 3), (2, 5), (5, 4), (5, 6)}
    assert layout.closure_edges(list(layout.fallback_tree)) == [(1, 4), (3, 6)]


@pytest.mark.parametrize("rows,cols", [(1, 4), (2, 2), (3, 3), (4, 4), (3, 5)])
def test_fallback_tree_spans_any_grid(rows, cols):
    layout = LensLayout(rows=rows, cols=cols)
    reached = {layout.fallback_reference}
    for parent, child in layout.fallback_tree:
        assert parent in reached, "parent must be aligned before its child"
        assert (min(parent, child), max(parent, child)) in layout.grid_edges, "only adjacent steps"
        reached.add(child)
    assert reached == set(layout.lenses)
    assert len(layout.fallback_tree) == layout.count - 1


def test_search_window_follows_baseline_direction():
    layout = LensLayout(rows=2, cols=3)
    left = layout.search_window(2, 1, [20, 300], 50)
    assert left["dx_max"] == -20 and left["dx_min"] == -300 and left["dy_min"] == -50
    down = layout.search_window(2, 5, [20, 300], 50)
    assert down["dy_min"] == 20 and down["dx_max"] == 50


def test_invalid_reference_lens_rejected(tmp_path):
    with pytest.raises(ValueError, match="fallback_reference_lens"):
        load_settings(tmp_path / "none.json", {"fallback_reference_lens": 9})


# --- closure check ----------------------------------------------------------

@pytest.fixture
def cells():
    img = (make_compound(7) * 255).astype(np.uint8)
    return split_compound_array(img, cell_size=CELL)[0]


def test_closure_passes_for_correct_alignment(cells, settings):
    result = estimate_alignment_from_images(cells, settings=settings)
    statuses = {r["pair"]: r["status"] for r in result["closure"]}
    assert statuses and all(s == "pass" for s in statuses.values()), result["closure"]


def test_closure_catches_a_wrong_link_in_the_chain(cells, settings):
    layout = LensLayout(rows=2, cols=3)
    result = compute_plan_b_alignment(cells, layout=layout, settings=settings)
    # Corrupt the 5->4 step by 8 px, as a wrongly accepted pair would.
    result["apply_shifts"][4]["dx"] += 8
    rows = run_closure_check(cells, result, layout, settings)
    by_pair = {r["pair"]: r for r in rows}
    assert by_pair["1-4"]["status"] == "fail"
    assert by_pair["3-6"]["status"] == "pass"


def test_closure_reject_vs_warn(tmp_path, settings, monkeypatch):
    import msalign.stack as stack

    real = stack.compute_plan_b_alignment

    def corrupted(*args, **kwargs):
        result = real(*args, **kwargs)
        result["apply_shifts"][4]["dx"] += 8
        return result

    def failing_primary(*args, **kwargs):
        raise RuntimeError("primary disabled for this test")

    monkeypatch.setattr(stack, "compute_primary_alignment", failing_primary)
    monkeypatch.setattr(stack, "compute_plan_b_alignment", corrupted)

    src = write_jpg(tmp_path / "A.JPG", make_compound(7))
    with pytest.raises(RuntimeError, match="closure check failed:.*1-4"):
        process_single_file(src, settings=settings)

    warn = dict(settings, closure_check="warn")
    result = process_single_file(src, settings=warn)
    assert result["closure_status"] == "fail" and result["quality"] == "low"
    report = Path(result["quality_report"]).read_text()
    assert "Closure check: fail" in report and "1-4 (closure): fail" in report


# --- other grids ------------------------------------------------------------

def test_two_by_two_camera(tmp_path):
    settings = load_settings(tmp_path / "none.json", {
        "grid_cols": 2, "grid_rows": 2, "cell_size": CELL,
        "file_bands": {"1": ["450", "0", "0"], "2": ["0", "550", "0"],
                       "3": ["0", "0", "650"], "4": ["0", "0", "850"]},
        "output_order": ["450", "550", "650", "850"],
    })
    shifts = physical_shifts(offsets=[(0, 0), (2, -1), (-1, 2), (1, 1)], cols=2)
    src = write_jpg(tmp_path / "quad.jpg", make_compound(3, shifts, cols=2))
    result = process_single_file(src, settings=settings)
    got = result["alignment"]["apply_shifts"]
    ref = got[1]
    for k, (dx, dy) in enumerate(shifts, start=1):
        assert abs(got[k]["dx"] - ref["dx"] - dx) <= 1 and abs(got[k]["dy"] - ref["dy"] - dy) <= 1
    assert result["closure_status"] == "pass"


# --- run files and CLI ------------------------------------------------------

def test_run_summary_csv(tmp_path, settings):
    paths = [write_jpg(tmp_path / f"S{i}.JPG", make_compound(i)) for i in range(2)]
    events = []
    run_pipeline(paths, events.append, settings=settings)
    (_, log_path, summary_path) = next(e for e in events if e[0] == "run_files")
    rows = list(csv.DictReader(open(summary_path, encoding="utf-8")))
    assert [r["file"] for r in rows] == ["S0.JPG", "S1.JPG"]
    assert all(r["status"] == "success" and r["closure"] == "pass" for r in rows)
    assert "Finished: 2 successful" in Path(log_path).read_text()


def test_cli_on_folder(tmp_path, capsys):
    for i in range(2):
        write_jpg(tmp_path / f"C{i}.JPG", make_compound(i))
    code = cli_main([str(tmp_path), "--set", f"cell_size={CELL}", "--no-batch"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "Done: 2 successful, 0 failed." in out
    assert (tmp_path / "C0" / "C0_aligned_multiband.tif").is_file()


def test_cli_bad_setting(tmp_path, capsys):
    code = cli_main([str(tmp_path), "--set", "closure_check=maybe"])
    assert code == 2
    assert "closure_check" in capsys.readouterr().err


def test_reference_band_table_follows_sensor_physics():
    """Bayer R pixels carry the longest band of a lens, B pixels the shortest."""
    from msalign.settings import DEFAULT_SETTINGS, file_bands_int

    table = file_bands_int(DEFAULT_SETTINGS)
    assert table[3] == ("710", "570", "405")
    assert table[5] == ("735", "0", "490") and table[6] == ("685", "560", "450")
    for lens, (r, g, b) in table.items():
        used = [float(x) for x in (r, g, b) if x != "0"]
        assert used == sorted(used, reverse=True), f"lens {lens}: {r, g, b}"


def test_band_table_dict_and_list_forms_agree(tmp_path):
    from msalign.settings import file_bands_int

    as_dict = load_settings(tmp_path / "none.json", {"file_bands": {
        "1": {"R": "850"}, "2": {"R": "630", "G": "525"}, "3": {"R": "710", "G": "570", "B": "405"},
        "4": {"R": "650", "G": "550", "B": "430"}, "5": {"R": "735", "B": "490"},
        "6": {"R": "685", "G": "560", "B": "450"}}})
    as_list = load_settings(tmp_path / "none.json", {"file_bands": {
        "1": ["850", "0", "0"], "2": ["630", "525", "0"], "3": ["710", "570", "405"],
        "4": ["650", "550", "430"], "5": ["735", "0", "490"], "6": ["685", "560", "450"]}})
    assert file_bands_int(as_dict) == file_bands_int(as_list)
    with pytest.raises(ValueError, match="unknown channel"):
        load_settings(tmp_path / "none.json", {"file_bands": {**as_dict["file_bands"], "1": {"NIR": "850"}}})
