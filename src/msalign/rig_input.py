"""
rig_input.py

Input from multi-camera rigs that write one file per band per capture, such as
MicaSense RedEdge / Altum (IMG_0001_1.tif ... IMG_0001_5.tif).

Unlike a single-sensor multi-lens camera (one compound image), every band has
its own lens and sensor: slightly different focal length, principal point and
distortion, and a small rotation relative to the reference band. Cameras like
MicaSense record these per band in XMP (PerspectiveFocalLength,
PrincipalPoint, PerspectiveDistortion, RigRelatives). Before the usual
alignment, every band is therefore mapped into one common ideal camera (the
reference band's, without distortion), so only the translation and the depth
parallax are left for the alignment to find.

Measured on two MicaSense RedEdge-MX sample captures (local residual check,
median / worst 90th percentile):

    IMG_0001:  as recorded 1.50 / 3.61 px -> undistorted 0.86 / 2.52 -> + rig rotation 1.09 / 1.86
    IMG_0020:  as recorded 1.46 / 4.85 px -> undistorted 0.61 / 1.63 -> + rig rotation 0.38 / 1.06

RigRelatives are rotations in degrees about the camera x, y, z axes; applying
them with this sign improved both captures, the opposite sign made them worse.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np


log = logging.getLogger(__name__)

DEFAULT_PATTERN = r"^(?P<capture>.+)_(?P<band>\d+)$"
BAND_SUFFIXES = (".tif", ".tiff")


# ---------------------------------------------------------------------------
# Finding captures
# ---------------------------------------------------------------------------

def split_name(path, pattern=DEFAULT_PATTERN):
    """(capture name, band number) or None."""
    m = re.match(pattern, Path(path).stem)
    if not m:
        return None
    return m.group("capture"), int(m.group("band"))


def capture_files(path, pattern=DEFAULT_PATTERN) -> dict[int, Path]:
    """All band files of the capture `path` belongs to: {band number: path}."""
    path = Path(path)
    parts = split_name(path, pattern)
    if parts is None:
        return {}
    capture, _ = parts
    files = {}
    for candidate in path.parent.iterdir():
        if candidate.suffix.lower() != path.suffix.lower():
            continue
        p = split_name(candidate, pattern)
        if p and p[0] == capture:
            files[p[1]] = candidate
    return dict(sorted(files.items()))


@lru_cache(maxsize=4096)
def _single_channel_tiff(path_str: str) -> bool:
    import tifffile

    try:
        with tifffile.TiffFile(path_str) as tif:
            page = tif.pages[0]
            return page.samplesperpixel == 1 and len(page.shape) == 2
    except Exception:
        return False


def is_band_file(path, settings) -> bool:
    """True if `path` is one band of a one-file-per-band capture."""
    mode = settings.get("input_mode", "auto")
    if mode == "compound":
        return False
    path = Path(path)
    if path.suffix.lower() not in BAND_SUFFIXES:
        return False
    pattern = settings.get("band_file_pattern", DEFAULT_PATTERN)
    if split_name(path, pattern) is None:
        return False
    if mode == "band_files":
        return True
    files = capture_files(path, pattern)
    return len(files) >= 3 and _single_channel_tiff(str(path))


def group_captures(paths, settings) -> list[Path]:
    """
    Replace the band files of each capture by one representative file (its
    lowest band number); other files pass through unchanged.
    """
    pattern = settings.get("band_file_pattern", DEFAULT_PATTERN)
    out, seen = [], set()
    for p in map(Path, paths):
        if is_band_file(p, settings):
            capture = (p.parent, split_name(p, pattern)[0])
            if capture in seen:
                continue
            seen.add(capture)
            out.append(next(iter(capture_files(p, pattern).values())))
        else:
            out.append(p)
    return out


def capture_name(path, settings) -> str:
    parts = split_name(path, settings.get("band_file_pattern", DEFAULT_PATTERN))
    return parts[0] if parts else Path(path).stem


# ---------------------------------------------------------------------------
# Per-band metadata
# ---------------------------------------------------------------------------

def _xmp_values(xmp: str, tag: str):
    """Values of an XMP property written as rdf:Seq, element or attribute."""
    m = re.search(r"<(?:[\w-]+:)?" + tag + r">\s*<rdf:(?:Seq|Bag|Alt)>(.*?)</rdf:(?:Seq|Bag|Alt)>", xmp, re.S)
    if m:
        return [v.strip() for v in re.findall(r"<rdf:li[^>]*>([^<]*)</rdf:li>", m.group(1))]
    m = re.search(r"<(?:[\w-]+:)?" + tag + r">([^<]*)</", xmp) or \
        re.search(r"\s(?:[\w-]+:)?" + tag + r"=\"([^\"]*)\"", xmp)
    return [v.strip() for v in m.group(1).split(",")] if m else None


def _floats(values):
    try:
        return [float(v) for v in values] if values else None
    except ValueError:
        return None


def read_band_meta(path) -> dict:
    """Band and lens calibration of one band file (whatever is present)."""
    import tifffile

    from .metadata import read_source_exif

    meta = {"path": Path(path)}
    tifffile_log = logging.getLogger("tifffile")
    level = tifffile_log.level
    tifffile_log.setLevel(logging.ERROR)
    try:
        with tifffile.TiffFile(str(path)) as tif:
            page = tif.pages[0]
            meta["shape"] = page.shape
            tag = page.tags.get(700)
            xmp = tag.value if tag else b""
    finally:
        tifffile_log.setLevel(level)
    xmp = xmp.decode("utf-8", "replace") if isinstance(xmp, (bytes, bytearray)) else str(xmp or "")

    exif = read_source_exif(path)
    meta["exif"] = exif

    name = _xmp_values(xmp, "BandName")
    wl = _floats(_xmp_values(xmp, "CentralWavelength"))
    fwhm = _floats(_xmp_values(xmp, "WavelengthFWHM"))
    meta["band_name"] = name[0] if name else None
    meta["wavelength_nm"] = wl[0] if wl else None
    meta["fwhm_nm"] = fwhm[0] if fwhm else None

    focal = _floats(_xmp_values(xmp, "PerspectiveFocalLength"))
    units = _xmp_values(xmp, "PerspectiveFocalLengthUnits")
    pp = _floats(_xmp_values(xmp, "PrincipalPoint"))
    dist = _floats(_xmp_values(xmp, "PerspectiveDistortion"))
    pixel_um = exif.get("pixel_size_um")
    if focal and pp and len(pp) == 2 and pixel_um:
        mm_to_px = 1000.0 / pixel_um
        in_px = bool(units) and units[0].lower() in ("px", "pixel", "pixels")
        meta["focal_px"] = focal[0] if in_px else focal[0] * mm_to_px
        meta["principal_point_px"] = (pp[0] * mm_to_px, pp[1] * mm_to_px)
    if dist and len(dist) >= 5:
        meta["distortion"] = dist[:5]  # MicaSense order: k1, k2, k3, p1, p2

    rel = _floats(_xmp_values(xmp, "RigRelatives"))
    if rel and len(rel) == 3:
        meta["rig_relatives_deg"] = rel
    idx = _floats(_xmp_values(xmp, "RigCameraIndex"))
    ref = _floats(_xmp_values(xmp, "RigRelativesReferenceRigCameraIndex"))
    meta["rig_index"] = int(idx[0]) if idx else None
    meta["rig_reference_index"] = int(ref[0]) if ref else None
    return meta


def band_label(meta, band_no) -> str:
    wl = meta.get("wavelength_nm")
    return f"{wl:g}" if wl else f"band{band_no}"


# ---------------------------------------------------------------------------
# Effective settings for a capture
# ---------------------------------------------------------------------------

def capture_settings(path, settings) -> tuple[dict, dict]:
    """
    Settings for processing this camera: one "lens" per band file, free
    layout, band labels and FWHM from the files. Returns (settings, info).
    """
    pattern = settings.get("band_file_pattern", DEFAULT_PATTERN)
    files = capture_files(path, pattern)
    metas = {b: read_band_meta(f) for b, f in files.items()}
    bands = list(files)

    labels = {b: band_label(m, b) for b, m in metas.items()}
    if len(set(labels.values())) != len(labels):
        labels = {b: f"band{b}" for b in bands}

    # reference: the band the rig rotations are relative to, else band 1 / the first
    ref_band = bands[0]
    rel_ref = {m.get("rig_reference_index") for m in metas.values()} - {None}
    if len(rel_ref) == 1:
        for b, m in metas.items():
            if m.get("rig_index") == next(iter(rel_ref)):
                ref_band = b

    lens_of = {b: i + 1 for i, b in enumerate(bands)}  # lens numbers 1..N in band order
    order = sorted(bands, key=lambda b: (metas[b].get("wavelength_nm") is None,
                                         metas[b].get("wavelength_nm") or 0, b))
    eff = dict(settings)
    eff.update({
        "_band_capture": True,
        "rig_layout": "free",
        "grid_rows": 1,
        "grid_cols": len(bands),
        "primary_reference_lens": lens_of[ref_band],
        "fallback_reference_lens": lens_of[ref_band],
        "file_bands": {str(lens_of[b]): {"R": labels[b]} for b in bands},
        "output_order": [labels[b] for b in order],
        "camera": {
            "make": None, "model": None, "lens": None, "serial_number": None,
            "focal_length_mm": None, "pixel_size_um": None,
            "band_fwhm_nm": {labels[b]: m["fwhm_nm"] for b, m in metas.items() if m.get("fwhm_nm")} or None,
        },
    })
    h, w = metas[bands[0]]["shape"][:2]
    size = eff.get("output_size_px")
    if size and (size[0] > w or size[1] > h):
        eff["output_size_px"] = None
    info = {"files": files, "metas": metas, "labels": labels, "lens_of": lens_of, "reference_band": ref_band}
    return eff, info


# ---------------------------------------------------------------------------
# Reading and normalising a capture
# ---------------------------------------------------------------------------

def _K(meta):
    f = meta["focal_px"]
    cx, cy = meta["principal_point_px"]
    return np.array([[f, 0, cx], [0, f, cy], [0, 0, 1]], dtype=np.float64)


def _rotation(degrees):
    a = np.deg2rad(np.asarray(degrees, dtype=np.float64))
    rx = cv2.Rodrigues(np.array([a[0], 0.0, 0.0]))[0]
    ry = cv2.Rodrigues(np.array([0.0, a[1], 0.0]))[0]
    rz = cv2.Rodrigues(np.array([0.0, 0.0, a[2]]))[0]
    return rz @ ry @ rx


def load_capture(path, settings):
    """
    Read all bands of the capture and map them into the reference band's
    ideal camera when the files carry lens calibration.
    Returns (images {lens: 2-D array}, eff_settings, info).
    """
    import tifffile

    eff, info = capture_settings(path, settings)
    metas, ref_band = info["metas"], info["reference_band"]

    raw = {}
    for b, f in info["files"].items():
        img = tifffile.imread(str(f))
        if img.ndim != 2:
            raise ValueError(f"{f.name}: expected a single-band image, got shape {img.shape}")
        raw[b] = img
    shapes = {img.shape for img in raw.values()}
    if len(shapes) != 1:
        raise ValueError(f"band images differ in size {sorted(shapes)}; bands of different "
                         "resolution (e.g. thermal) are not supported")

    calibrated = all("focal_px" in m and "distortion" in m for m in metas.values())
    use_rotation = settings.get("rig_rotation", True) and all(
        "rig_relatives_deg" in m for m in metas.values()
    )
    images = {}
    if calibrated:
        K_ref = _K(metas[ref_band])
        for b, img in raw.items():
            m = metas[b]
            k1, k2, k3, p1, p2 = m["distortion"]
            dist = np.array([k1, k2, p1, p2, k3])
            R = _rotation(m["rig_relatives_deg"]) if use_rotation else np.eye(3)
            mx, my = cv2.initUndistortRectifyMap(_K(m), dist, R, K_ref,
                                                 (img.shape[1], img.shape[0]), cv2.CV_32FC1)
            out = cv2.remap(img.astype(np.float32), mx, my, cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_CONSTANT, borderValue=0)
            if np.issubdtype(img.dtype, np.integer):
                info_ = np.iinfo(img.dtype)
                out = np.clip(np.rint(out), info_.min, info_.max)
            images[info["lens_of"][b]] = out.astype(img.dtype)
    else:
        images = {info["lens_of"][b]: img for b, img in raw.items()}

    info["normalisation"] = (
        "undistorted to the reference band's camera" + (" with rig rotations" if use_rotation else "")
        if calibrated else "none (no lens calibration in the files)"
    )
    log.info("%s: %d bands, normalisation: %s", capture_name(path, settings), len(images), info["normalisation"])
    return images, eff, info
