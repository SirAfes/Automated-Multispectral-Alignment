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
import sys
from pathlib import Path


CONFIG_FILENAME = "pipeline_config.json"


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
    "file_bands": {
        "1": ["0", "0", "850"],
        "2": ["0", "525", "630"],
        "3": ["405", "570", "710"],
        "4": ["430", "550", "650"],
        "5": ["450", "560", "685"],
        "6": ["490", "0", "735"],
    },
    # Band order in the output TIFF.
    "output_order": [
        "405", "430", "450", "490",
        "525", "550", "560", "570",
        "630", "650", "685", "710",
        "735", "850",
    ],

    # --- Outputs ------------------------------------------------------------
    # Second copy of every final TIFF in <source folder>/tiff.
    #   "hardlink": same file, no extra disk space (falls back to copy if the
    #               file system does not support it)
    #   "copy":     independent copy
    #   "off":      no second copy
    "collection_mode": "hardlink",

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


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if key.startswith("_"):
            continue  # allows "_comment" keys in the JSON file
        if key not in out:
            raise ValueError(f"Unknown setting in {CONFIG_FILENAME}: '{key}'")
        out[key] = value
    return out


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
    for lens, labels in bands.items():
        if len(labels) != 3:
            raise ValueError(f"file_bands['{lens}'] must have exactly 3 entries (R, G, B)")
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

    if settings["collection_mode"] not in {"hardlink", "copy", "off"}:
        raise ValueError("collection_mode must be 'hardlink', 'copy' or 'off'")

    if not 0 < settings["center_crop_frac"] <= 1:
        raise ValueError("center_crop_frac must be in (0, 1]")

    return settings


def load_settings(path: Path | None = None, overrides: dict | None = None) -> dict:
    """Defaults, then pipeline_config.json (if present), then explicit overrides."""
    settings = copy.deepcopy(DEFAULT_SETTINGS)

    config_path = path or (_base_dir() / CONFIG_FILENAME)
    if config_path.is_file():
        with config_path.open("r", encoding="utf-8") as f:
            settings = _merge(settings, json.load(f))
        settings["_source"] = str(config_path)
    else:
        settings["_source"] = "built-in defaults"

    if overrides:
        settings = _merge(settings, overrides)

    return validate_settings(settings)


def file_bands_int(settings: dict) -> dict[int, tuple[str, str, str]]:
    return {int(k): tuple(v) for k, v in settings["file_bands"].items()}
