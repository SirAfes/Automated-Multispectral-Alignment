import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from settings import load_settings  # noqa: E402

CELL = 640
# Lens geometry used for the synthetic camera: where each lens sees the scene.
SHIFTS = [(0, 0), (12, -7), (-9, 5), (20, 3), (-4, -15), (7, 11)]
# Per-lens channel mixing, so the lenses look "spectrally" different.
MIX = [
    (1.0, 0.2, 0.1), (0.3, 1.0, 0.2), (0.1, 0.4, 1.0),
    (0.8, 0.8, 0.1), (0.2, 0.9, 0.7), (1.0, 0.1, 0.6),
]


def make_scene(seed, size):
    rng = np.random.default_rng(seed)
    base = cv2.GaussianBlur(rng.random((size, size, 3)).astype(np.float32), (0, 0), 2.5)
    for _ in range(220):
        x, y = rng.integers(0, size, 2)
        color = tuple(float(v) for v in rng.random(3))
        if rng.random() < 0.5:
            cv2.circle(base, (int(x), int(y)), int(rng.integers(4, 30)), color, -1)
        else:
            w, h = rng.integers(6, 50, 2)
            cv2.rectangle(base, (int(x), int(y)), (int(x + w), int(y + h)), color, -1)
    return base


def make_compound(seed, shifts=SHIFTS, cell=CELL, jitter=0):
    """Compound 3x2 image as float in [0, 1]; lens k sees the scene shifted by shifts[k]."""
    rng = np.random.default_rng(seed + 1000)
    pad = max(60, max(max(abs(dx), abs(dy)) for dx, dy in shifts) + jitter + 10)
    scene = make_scene(seed, cell + 2 * pad)
    img = np.zeros((2 * cell, 3 * cell, 3), np.float32)
    for i, (dx, dy) in enumerate(shifts):
        if jitter:
            dx += int(rng.integers(-jitter, jitter + 1))
            dy += int(rng.integers(-jitter, jitter + 1))
        r, c = divmod(i, 3)
        view = scene[pad + dy: pad + dy + cell, pad + dx: pad + dx + cell]
        mixed = view * np.asarray(MIX[i], np.float32)
        img[r * cell:(r + 1) * cell, c * cell:(c + 1) * cell] = mixed
    return img / img.max()


def write_jpg(path, img):
    # imencode + tofile: cv2.imwrite cannot write non-ASCII paths on Windows.
    ok, buf = cv2.imencode(".jpg", (img * 255).astype(np.uint8))
    assert ok
    buf.tofile(str(path))
    return path


@pytest.fixture
def settings():
    return load_settings(
        path=Path("does-not-exist.json"),
        overrides={
            "cell_size": CELL,
            "batch_threshold": 8,
            "batch_calibration_samples": 5,
            "batch_min_successful_samples": 4,
        },
    )


@pytest.fixture
def fake_rawpy(monkeypatch):
    """rawpy stand-in: an .ARW file 'contains' the path of a .npy array."""
    import types

    module = types.ModuleType("rawpy")

    class ColorSpace:
        raw = "raw"

    class _Raw:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def postprocess(self, **kw):
            assert kw["output_bps"] == 16
            assert kw["gamma"] == (1, 1)
            assert kw["output_color"] == ColorSpace.raw
            return np.load(Path(self.path).read_text())

    module.ColorSpace = ColorSpace
    module.imread = _Raw
    monkeypatch.setitem(sys.modules, "rawpy", module)
    return module


def write_fake_raw(path, img):
    """Store a compound image as a fake RAW file (16-bit RGB) for fake_rawpy."""
    npy = path.with_suffix(".npy")
    rgb16 = (img[:, :, ::-1] * 60000).astype(np.uint16)
    np.save(npy, rgb16)
    path.write_text(str(npy))
    return path
