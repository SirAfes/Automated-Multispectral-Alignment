"""
image_io.py

Shared image reading helpers.

Standard formats (JPG, PNG, TIFF) are read with OpenCV. Camera RAW files
(.ARW, .RAW and other LibRaw-supported formats) are decoded with rawpy.

RAW decoding is intentionally kept close to the sensor data, because the
pipeline treats each colour channel as a separate spectral band:

- no camera white balance (all channel multipliers = 1),
- no camera colour matrix (output stays in the sensor's own RGB space),
- linear response (no gamma curve) and no automatic brightening,
- 16-bit output.

The returned array uses OpenCV's BGR channel order, so the band mapping in
stack_core.FILE_BANDS applies unchanged to both JPG and RAW inputs.
"""

from pathlib import Path

import cv2
import numpy as np


STANDARD_EXTENSIONS = (".jpg", ".jpeg", ".png", ".tif", ".tiff")

# .arw = Sony, .raw = Panasonic/Leica and generic RAW. The others are common
# LibRaw-supported formats that cost nothing extra to accept.
RAW_EXTENSIONS = (
    ".arw", ".raw", ".dng", ".nef", ".cr2", ".cr3",
    ".orf", ".rw2", ".raf", ".pef", ".srw",
)

SUPPORTED_EXTENSIONS = STANDARD_EXTENSIONS + RAW_EXTENSIONS

# Split lens images: RAW inputs keep their 16-bit data in lossless TIFF,
# standard inputs keep the existing JPG behaviour.
SPLIT_EXT_STANDARD = ".jpg"
SPLIT_EXT_RAW = ".tif"
SPLIT_EXTENSIONS = (SPLIT_EXT_RAW, SPLIT_EXT_STANDARD)


def is_raw_file(path) -> bool:
    return Path(path).suffix.lower() in RAW_EXTENSIONS


def split_ext_for_input(path) -> str:
    return SPLIT_EXT_RAW if is_raw_file(path) else SPLIT_EXT_STANDARD


def file_dialog_types():
    """File-type filters for tkinter dialogs (both cases, for case-sensitive systems)."""

    def patterns(exts):
        return " ".join(f"*{e} *{e.upper()}" for e in exts)

    return [
        ("All supported images", patterns(SUPPORTED_EXTENSIONS)),
        ("Camera RAW", patterns(RAW_EXTENSIONS)),
        ("Standard images", patterns(STANDARD_EXTENSIONS)),
        ("All files", "*.*"),
    ]


def _read_raw(path: Path):
    try:
        import rawpy
    except ImportError as exc:
        raise ImportError(
            "Reading camera RAW files requires the 'rawpy' package "
            "(pip install rawpy)."
        ) from exc

    try:
        with rawpy.imread(str(path)) as raw:
            rgb = raw.postprocess(
                use_camera_wb=False,
                use_auto_wb=False,
                user_wb=[1.0, 1.0, 1.0, 1.0],
                output_color=rawpy.ColorSpace.raw,
                gamma=(1, 1),
                no_auto_bright=True,
                output_bps=16,
            )
    except Exception as exc:
        raise IOError(f"Could not decode RAW file {path.name}: {exc}") from exc

    # rawpy returns RGB; the rest of the pipeline expects OpenCV BGR.
    return np.ascontiguousarray(rgb[:, :, ::-1])


def read_image(path):
    """Read a standard or RAW image. Returns a BGR (or grayscale) numpy array."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Input image not found: {path}")

    if is_raw_file(path):
        return _read_raw(path)

    # np.fromfile + imdecode also handles non-ASCII Windows paths,
    # which cv2.imread cannot open.
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise FileNotFoundError(f"Could not load image: {path}")
    return img


def find_split_set(split_dir: Path, preferred_ext: str | None = None):
    """
    Return the extension of a complete 1..6 split set in split_dir, or None.

    If preferred_ext is given, only that extension is accepted, so a RAW input
    never silently reuses JPG splits from the camera's JPG twin (or vice versa).
    """
    candidates = (preferred_ext,) if preferred_ext else SPLIT_EXTENSIONS
    for ext in candidates:
        if all((split_dir / f"{idx}{ext}").is_file() for idx in range(1, 7)):
            return ext
    return None


def to_uint8(img):
    """Scale any-depth image to uint8 for 8-bit-only OpenCV routines (e.g. AKAZE, CLAHE)."""
    if img.dtype == np.uint8:
        return img
    g = img.astype(np.float32)
    lo, hi = np.percentile(g, 0.5), np.percentile(g, 99.5)
    g = (g - lo) * (255.0 / max(hi - lo, 1e-6))
    return np.clip(g, 0, 255).astype(np.uint8)
