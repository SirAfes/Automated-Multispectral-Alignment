"""
settings.py

Pipeline settings: camera layout, band mapping and batch behaviour.

Defaults live in DEFAULT_SETTINGS. To change them without editing code, place a
`pipeline_config.json` next to the executable (or in the repository root when
running from source). Only the keys you want to change need to be present; see
`pipeline_config.example.json`.
"""

from __future__ import annotations

import copy
import json
import logging
import sys
from pathlib import Path


CONFIG_FILENAME = "pipeline_config.json"

log = logging.getLogger(__name__)


DEFAULT_SETTINGS = {
    # --- Compound image layout -------------------------------------------
    # Lenses are numbered row by row from 1 (3 x 2: top row 1 2 3, bottom 4 5 6).
    "grid_cols": 3,
    "grid_rows": 2,
    # Expected cell size in pixels (3 x 3168 = 9504, 2 x 3168 = 6336).
    "cell_size": 3168,
    # Fraction of each lens cell kept (centred) after splitting.
    "center_crop_frac": 0.80,
    "jpeg_quality": 100,

    # --- Camera RAW decoding ----------------------------------------------
    # "linear": bilinear demosaicing from same-colour neighbours only (keeps
    # spectral bands separate); "ahd": LibRaw's default, correlation-based
    # (sharper, but mixes information between bands). See image_io.py.
    "raw_demosaic": "linear",

    # --- Alignment geometry -----------------------------------------------
    # Primary alignment: every lens directly against this lens.
    "primary_reference_lens": 1,
    # Fallback chain: adjacent-lens steps through this lens. null = grid centre
    # (lens 2 for 3 x 2).
    "fallback_reference_lens": None,
    # Expected parallax between two adjacent lenses, in pixels along their
    # baseline: the fallback only searches this range in the baseline
    # direction (keeps it away from fixed-pattern matches at 0 px). Depends on
    # the camera and the working distance - widen it if results sit at an edge.
    "parallax_range_px": [20, 360],
    # Allowed shift across the baseline direction (px).
    "parallax_cross_px": 80,

    # --- Local (depth-dependent) alignment -------------------------------------
    # Measure the remaining misalignment on image tiles after the global
    # alignment (reported per image; ~0.5 s).
    "local_residual_check": True,
    # Correct depth parallax with one disparity map shared by all lenses
    # (close-range scenes with depth). Bands are then resampled (bilinear).
    # Kept only if it measurably reduces the local residuals.
    "parallax_correction": False,
    # Largest extra parallax searched, in full-resolution px per lens step.
    "parallax_search_px": 48,
    # Write <name>_parallax.tif (disparity map) when the correction is applied.
    "write_parallax_map": True,

    # --- Closure check --------------------------------------------------------
    # Adjacent lens pairs not measured directly are checked on the image.
    #   "reject": a failed check rejects the alignment (primary -> fallback,
    #             fallback -> retry / error); "warn": only reported; "off".
    "closure_check": "reject",
    "closure_tolerance_px": 4.0,
    # A pair also fails when its correlation at the implied shift is below this
    # fraction of the median correlation of the directly measured pairs of the
    # same image (= the implied shift is not supported by the image).
    "closure_relative_ncc": 0.4,
    # If even the measured pairs correlate below this, the image has too little
    # shared structure: the check is "inconclusive" and rejects nothing.
    "closure_min_ncc": 0.10,

    # --- Band mapping -------------------------------------------------------
    # Lens image number -> wavelength label stored in its (R, G, B) channels.
    # "0" means the channel is not used.
    # Written per sensor channel, e.g. {"R": "710", "G": "570", "B": "405"};
    # channels without a band are left out. (A list is read as [R, G, B],
    # with "0" for an unused channel.)
    # Verified on Agrowing Sextuple data: Bayer red pixels carry the longest
    # band of each lens and blue pixels the shortest, matching Agrowing's own
    # software. Lens 1: the NIR band is read from the red channel.
    "file_bands": {
        "1": {"R": "850"},
        "2": {"R": "630", "G": "525"},
        "3": {"R": "710", "G": "570", "B": "405"},
        "4": {"R": "650", "G": "550", "B": "430"},
        "5": {"R": "735", "B": "490"},
        "6": {"R": "685", "G": "560", "B": "450"},
    },
    # Band order in the output TIFF.
    "output_order": [
        "405", "430", "450", "490",
        "525", "550", "560", "570",
        "630", "650", "685", "710",
        "735", "850",
    ],

    # --- Camera metadata ----------------------------------------------------
    # Written into every TIFF. Values given here win over the source EXIF;
    # null fields are taken from EXIF or left out (nothing is guessed).
    # Defaults describe the reference camera: Agrowing Sextuple lens on a
    # Sony 61 MP full-frame body (make / model come from the body's EXIF).
    # Effective focal length and 30 nm band width: Agrowing specifications.
    # Pixel size: 35.7 mm sensor width / 9504 px.
    "camera": {
        "make": None,
        "model": None,
        "lens": "Agrowing Sextuple",
        "focal_length_mm": 21.8,
        "pixel_size_um": 3.76,
        # one value for all bands, or {"405": 30, ...}
        "band_fwhm_nm": 30,
        "serial_number": None,
    },

    # --- Output image -----------------------------------------------------------
    # The output pixel grid is the grid of `primary_reference_lens`.
    # Fixed output size [width, height] for every image, or null for the
    # largest area all lenses share (size then varies slightly per image).
    # 2048 fits the reference camera over its full focus range (0.5 m - inf).
    "output_size_px": [2048, 2048],
    # Where the fixed-size window is placed:
    #   "common_area_center": centred on the area all lenses share (default);
    #       the crop origin can differ by a few px between images and is
    #       recorded in the metadata.
    #   "fixed": same origin in reference-lens pixels for every image, centred
    #       on `output_fixed_center_px` ([x, y] in the reference lens image,
    #       null = its centre). For pixel-to-pixel comparison of series.
    "output_anchor": "common_area_center",
    "output_fixed_center_px": None,
    # Pixels of the window outside the shared area are set to 0 (nodata).
    # An image whose shared area is smaller than the window by more than this
    # many pixels on any side fails instead of being written.
    "output_max_padding_px": 0,
    # "multiband": one image with N bands (GDAL / QGIS / ENVI read all bands);
    # "multipage": one page per band (format of msalign < 1.1).
    "tiff_layout": "multiband",
    # "none" or "deflate" (lossless, smaller files).
    "tiff_compression": "none",
    # Also write <name>_metadata.json next to the TIFF.
    "write_metadata_json": True,

    # --- Outputs ------------------------------------------------------------
    # Second copy of every final TIFF in <source folder>/tiff.
    #   "hardlink": same file, no extra disk space (falls back to copy if the
    #               file system does not support it)
    #   "copy":     independent copy
    #   "off":      no second copy
    "collection_mode": "hardlink",

    # --- Speed --------------------------------------------------------------
    # Images processed at the same time in separate processes: "auto" (one
    # per CPU core minus one, limited by free memory: ~1.5 GB per RAW image)
    # or a number. 1 = one after the other, as in versions before 1.3.
    "workers": "auto",

    # --- Per-image history (standard mode) ----------------------------------
    "history_length": 5,

    # --- Batch consensus mode -----------------------------------------------
    # Selections with at least this many images use batch consensus mode.
    "batch_threshold": 50,
    # Number of evenly spaced images fully aligned to learn the lens geometry.
    "batch_calibration_samples": 16,
    # Minimum successful calibration images required to trust the consensus.
    "batch_min_successful_samples": 8,
    # A calibration image agrees with the consensus if every lens is within
    # this many pixels of the median shift.
    "batch_consensus_tolerance_px": 3.0,
    # Fraction of successful calibration images that must agree.
    "batch_min_agreement": 0.75,
    # Per-image check: the consensus shift is verified on every image and the
    # measured residual is applied as a small correction. Frames deviating by
    # more than this (px) get the full alignment search instead.
    "batch_verify_max_residual_px": 5.0,
    # Required verification response, relative to the median response seen on
    # the calibration images (data-driven threshold).
    "batch_verify_min_response_ratio": 0.35,
    # Keep the six split lens images for images processed with the consensus.
    # Off by default: for RAW input they are ~200 MB per image.
    "batch_keep_split_images": False,
}


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


# Settings that describe a different camera than the reference camera. If a
# configuration changes any of them, reference-camera defaults that would then
# be wrong (lens metadata, output size) are dropped unless also configured.
CAMERA_GEOMETRY_KEYS = {"grid_cols", "grid_rows", "cell_size", "center_crop_frac", "file_bands"}
REFERENCE_CAMERA_KEYS = {"camera", "output_size_px"}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if key.startswith("_"):
            continue  # allows "_comment" keys in the JSON file
        if key not in out:
            raise ValueError(f"Unknown setting in {CONFIG_FILENAME}: '{key}'")
        if key == "camera" and isinstance(value, dict):
            merged = dict(out["camera"])
            merged.update(value)
            value = merged
        out[key] = value
    return out


def _drop_reference_camera_defaults(settings: dict, configured: set) -> dict:
    if not (configured & CAMERA_GEOMETRY_KEYS):
        return settings
    dropped = []
    if "camera" not in configured:
        settings["camera"] = {k: None for k in DEFAULT_SETTINGS["camera"]}
        dropped.append("camera")
    if "output_size_px" not in configured:
        settings["output_size_px"] = None
        dropped.append("output_size_px")
    if dropped:
        log.warning(
            "Camera geometry differs from the reference camera; reference defaults for %s "
            "are not used. Set them in the configuration if needed.", ", ".join(dropped),
        )
    return settings


def validate_settings(settings: dict) -> dict:
    bands = settings["file_bands"]
    n_lenses = settings["grid_cols"] * settings["grid_rows"]

    if n_lenses < 2:
        raise ValueError("grid_rows x grid_cols must give at least 2 lenses")
    if sorted(int(k) for k in bands) != list(range(1, n_lenses + 1)):
        raise ValueError(
            f"file_bands must define lens images 1..{n_lenses} "
            f"(grid {settings['grid_cols']} x {settings['grid_rows']})"
        )

    available = set()
    for lens in bands:
        labels = channel_labels(bands[lens], lens)
        for label in labels:
            if label != "0":
                if label in available:
                    raise ValueError(f"Band '{label}' is assigned to more than one channel")
                available.add(label)

    missing = [b for b in settings["output_order"] if b not in available]
    if missing:
        raise ValueError(f"output_order contains bands not in file_bands: {missing}")

    for key in ("primary_reference_lens", "fallback_reference_lens"):
        value = settings[key]
        if value is not None and not 1 <= int(value) <= n_lenses:
            raise ValueError(f"{key} must be between 1 and {n_lenses}")

    lo, hi = settings["parallax_range_px"]
    if not 0 <= lo < hi:
        raise ValueError("parallax_range_px must be [min, max] with 0 <= min < max")

    if settings["closure_check"] not in {"reject", "warn", "off"}:
        raise ValueError("closure_check must be 'reject', 'warn' or 'off'")

    size = settings["output_size_px"]
    if size is not None:
        if not (isinstance(size, (list, tuple)) and len(size) == 2 and all(int(v) > 0 for v in size)):
            raise ValueError("output_size_px must be [width, height] or null")
        lens = int(settings["cell_size"] * settings["center_crop_frac"])
        if max(size) > lens:
            raise ValueError(f"output_size_px {list(size)} is larger than a lens image ({lens} px)")

    w = settings["workers"]
    if not (w == "auto" or (isinstance(w, int) and not isinstance(w, bool) and w >= 1)):
        raise ValueError("workers must be \"auto\" or a whole number >= 1")

    if settings["raw_demosaic"] not in {"linear", "ahd"}:
        raise ValueError("raw_demosaic must be 'linear' or 'ahd'")

    if settings["output_anchor"] not in {"common_area_center", "fixed"}:
        raise ValueError("output_anchor must be 'common_area_center' or 'fixed'")
    if settings["tiff_layout"] not in {"multiband", "multipage"}:
        raise ValueError("tiff_layout must be 'multiband' or 'multipage'")
    if settings["tiff_compression"] not in {"none", "deflate"}:
        raise ValueError("tiff_compression must be 'none' or 'deflate'")

    unknown = set(settings["camera"]) - set(DEFAULT_SETTINGS["camera"])
    if unknown:
        raise ValueError(f"Unknown camera setting(s): {sorted(unknown)}")

    if settings["collection_mode"] not in {"hardlink", "copy", "off"}:
        raise ValueError("collection_mode must be 'hardlink', 'copy' or 'off'")

    if not 0 < settings["center_crop_frac"] <= 1:
        raise ValueError("center_crop_frac must be in (0, 1]")

    return settings


def load_settings(path: Path | None = None, overrides: dict | None = None) -> dict:
    """Defaults, then pipeline_config.json (if present), then explicit overrides."""
    settings = copy.deepcopy(DEFAULT_SETTINGS)

    configured = set()
    config_path = path or (_base_dir() / CONFIG_FILENAME)
    if config_path.is_file():
        with config_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        settings = _merge(settings, data)
        configured |= {k for k in data if not k.startswith("_")}
        settings["_source"] = str(config_path)
    else:
        settings["_source"] = "built-in defaults"

    if overrides:
        settings = _merge(settings, overrides)
        configured |= set(overrides)

    settings = _drop_reference_camera_defaults(settings, configured)
    return validate_settings(settings)


CHANNELS = ("R", "G", "B")


def channel_labels(entry, lens="?") -> tuple[str, str, str]:
    """
    Band labels of one lens as (R, G, B), "0" for unused channels.
    Accepts {"R": "710", "G": "570", "B": "405"} or ["710", "570", "405"].
    """
    if isinstance(entry, dict):
        unknown = set(entry) - set(CHANNELS)
        if unknown:
            raise ValueError(f"file_bands['{lens}']: unknown channel(s) {sorted(unknown)}; use R, G, B")
        return tuple(str(entry.get(ch) or "0") for ch in CHANNELS)
    if isinstance(entry, (list, tuple)) and len(entry) == 3:
        return tuple(str(v) for v in entry)
    raise ValueError(
        f"file_bands['{lens}'] must be {{\"R\": .., \"G\": .., \"B\": ..}} or a list [R, G, B]"
    )


def file_bands_int(settings: dict) -> dict[int, tuple[str, str, str]]:
    """{lens: (R label, G label, B label)}."""
    return {int(k): channel_labels(v, k) for k, v in settings["file_bands"].items()}
