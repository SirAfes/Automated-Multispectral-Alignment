from pathlib import Path

import pytest

from conftest import make_compound, physical_shifts, write_jpg
from msalign.pipeline import resolve_workers, run_pipeline
from msalign.settings import load_settings


def shifts_of(success):
    return {
        i: {k: (v["dx"], v["dy"]) for k, v in r["alignment"]["apply_shifts"].items()}
        for i, r in success.items()
    }


def test_parallel_gives_the_same_result_as_sequential(tmp_path, settings):
    seq_dir, par_dir = tmp_path / "seq", tmp_path / "par"
    seq_dir.mkdir(), par_dir.mkdir()
    imgs = [make_compound(i) for i in range(4)]
    seq = [write_jpg(seq_dir / f"P{i}.JPG", img) for i, img in enumerate(imgs)]
    par = [write_jpg(par_dir / f"P{i}.JPG", img) for i, img in enumerate(imgs)]

    s_seq, f_seq = run_pipeline(seq, settings=dict(settings, workers=1))
    events = []
    s_par, f_par = run_pipeline(par, events.append, settings=dict(settings, workers=2))

    assert f_seq == f_par == []
    assert shifts_of(s_seq) == shifts_of(s_par)
    assert ("workers", 2) in events
    kinds = [e[0] for e in events]
    assert kinds.count("file_start") == 4 and kinds.count("success") == 4
    # worker logs end up in the run log, labelled with the image
    log_path = next(e for e in events if e[0] == "run_files")[1]
    assert "[P0.JPG]" in Path(log_path).read_text(encoding="utf-8")


def test_parallel_batch_mode_with_outlier(tmp_path, settings):
    near = physical_shifts(parallax=33)
    paths = []
    for i in range(10):
        shifts = near if i == 5 else physical_shifts()  # 5 is not a calibration image
        paths.append(write_jpg(tmp_path / f"B{i:02d}.JPG", make_compound(i, shifts)))
    events = []
    success, failed = run_pipeline(paths, events.append, settings=dict(settings, workers=3))
    assert failed == []
    assert success[5]["alignment"]["mode"] != "batch_consensus"
    assert sum(r["alignment"]["mode"] == "batch_consensus" for r in success.values()) >= 3
    assert any(e[0] == "stage" and "Geometry differs" in e[2] for e in events)


def test_cancel_stops_submitting(tmp_path, settings):
    import threading

    paths = [write_jpg(tmp_path / f"C{i}.JPG", make_compound(i)) for i in range(6)]
    stop = threading.Event()

    def emit(event):
        if event[0] == "success":
            stop.set()

    success, failed = run_pipeline(paths, emit, stop_event=stop, settings=dict(settings, workers=2))
    assert 1 <= len(success) < 6  # images already running finish, nothing new starts


def test_resolve_workers(monkeypatch):
    import msalign.pipeline as p

    monkeypatch.setattr(p.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(p, "available_memory_gb", lambda: 6.0)
    assert resolve_workers("auto", 100, raw_input=True) == 4     # memory: 6 GB / 1.5 GB
    assert resolve_workers("auto", 100, raw_input=False) == 7    # cores - 1
    assert resolve_workers("auto", 3, raw_input=False) == 3      # not more than images
    assert resolve_workers("auto", 1) == 1
    assert resolve_workers(2, 100) == 2
    monkeypatch.setattr(p, "available_memory_gb", lambda: None)
    assert resolve_workers("auto", 100) == 7


def test_workers_setting_validation(tmp_path):
    with pytest.raises(ValueError, match="workers"):
        load_settings(tmp_path / "none.json", {"workers": 0})
    assert load_settings(tmp_path / "none.json", {"workers": 3})["workers"] == 3


def _crash_in_worker(task, status_callback=None):
    """Stand-in for _worker that kills its worker process (like an out-of-memory kill)."""
    import os

    if task.get("capture_log"):  # only inside a worker process
        os._exit(1)
    from msalign.pipeline import _worker_impl
    return _worker_impl(task, status_callback)


def test_broken_worker_pool_falls_back_to_sequential(tmp_path, settings, monkeypatch):
    import msalign.pipeline as p

    paths = [write_jpg(tmp_path / f"K{i}.JPG", make_compound(i)) for i in range(4)]
    monkeypatch.setattr(p, "_worker_impl", p._worker, raising=False)
    monkeypatch.setattr(p, "_worker", _crash_in_worker)
    events = []
    success, failed = run_pipeline(paths, events.append, settings=dict(settings, workers=2))
    assert failed == [] and len(success) == 4
    assert any(e[0] == "phase" and "one image at a time" in e[1] for e in events)
