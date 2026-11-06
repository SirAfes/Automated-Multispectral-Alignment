#!/usr/bin/env python3
"""Center-crop 14-band multispectral TIFF batches and write Metashape-ready TIFFs.

Accepts legacy 14-page TIFF files and modern single-image, 14-sample TIFF
files. Keeps pixel values and nominal spectral order unchanged. No resampling.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import json
import os
from pathlib import Path
from uuid import uuid4
import warnings

import numpy as np
import rasterio
from rasterio.enums import MaskFlags
from rasterio.errors import NotGeoreferencedWarning
from rasterio.windows import Window
import tifffile

from tiff_metadata import DEFAULT_WAVELENGTHS_NM


WAVELENGTHS_NM = DEFAULT_WAVELENGTHS_NM
TIFF_EXTENSIONS = frozenset({".tif", ".tiff"})
REPORT_COLUMNS = (
    "source", "destination", "layout", "original_width", "original_height",
    "target_width", "target_height", "crop_left", "crop_right",
    "crop_top", "crop_bottom", "band_count", "status",
)


@dataclass(frozen=True)
class ImageInfo:
    path: Path
    width: int
    height: int
    dtype: str
    layout: str


def arguments(argv=None):
    parser = argparse.ArgumentParser(
        description="Convert legacy/multiband TIFFs to uniform, metadata-labelled 14-band TIFFs."
    )
    parser.add_argument("--input", type=Path, required=True, help="Input TIFF directory")
    parser.add_argument("--output", type=Path, required=True, help="New output directory")
    parser.add_argument("--recursive", action="store_true", help="Scan subdirectories")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--overwrite", action="store_true", help="Replace output files")
    parser.add_argument("--chunk-rows", type=int, default=128, help="Streaming rows per block")
    parser.add_argument(
        "--wavelengths-nm", type=str, default=",".join(map(str, WAVELENGTHS_NM)),
        help="Comma-separated band wavelengths in file order (default: project band map)",
    )
    parser.add_argument("--camera-model", type=str, help="Verified camera model (optional)")
    parser.add_argument("--focal-length-mm", type=float, help="Verified lens focal length (optional)")
    parser.add_argument("--pixel-size-um", type=float, help="Verified effective pixel pitch (optional)")
    return parser.parse_args(argv)


def parse_wavelengths(value: str) -> tuple[int, ...]:
    try:
        numbers = tuple(int(v.strip()) for v in value.split(","))
    except ValueError as exc:
        raise ValueError("--wavelengths-nm requires comma-separated integers.") from exc
    if len(numbers) != 14 or any(v <= 0 for v in numbers) or len(set(numbers)) != 14:
        raise ValueError("Exactly 14 distinct positive wavelengths are required.")
    return numbers


def discover_images(input_dir: Path, recursive: bool) -> list[Path]:
    candidates = input_dir.rglob("*") if recursive else input_dir.iterdir()
    return sorted(
        (p for p in candidates if p.is_file() and p.suffix.lower() in TIFF_EXTENSIONS),
        key=lambda p: str(p).lower(),
    )


def inspect(path: Path, wavelengths: tuple[int, ...]) -> ImageInfo:
    with tifffile.TiffFile(path) as tif:
        pages = tif.pages
        if len(pages) == 14:
            shape = pages[0].shape
            dtype = pages[0].dtype
            if len(shape) != 2 or any(
                len(page.shape) != 2 or page.shape != shape or page.dtype != dtype
                for page in pages
            ):
                raise ValueError(f"Unsupported or inconsistent 14-page TIFF: {path}")
            # Old exporter stored each band's nominal wavelength in its JSON description.
            for index, page in enumerate(pages):
                try:
                    info = json.loads(page.description or "{}")
                except (TypeError, ValueError):
                    continue
                if isinstance(info, dict) and "band_name" in info:
                    name = str(info["band_name"]).strip()
                    if name != str(wavelengths[index]):
                        raise ValueError(
                            f"Band {index + 1} is labelled {name} nm in {path.name}, "
                            f"but --wavelengths-nm specifies {wavelengths[index]} nm."
                        )
            height, width = shape
            layout = "legacy_14_page"
        elif len(pages) == 1:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", NotGeoreferencedWarning)
                with rasterio.open(path) as src:
                    if src.count != 14:
                        raise ValueError(
                            f"Expected 14 bands; GDAL reports {src.count} in {path.name}."
                        )
                    height, width = src.height, src.width
                    dtype = np.dtype(src.dtypes[0])
                    if len(set(src.dtypes)) != 1:
                        raise ValueError(f"Mixed TIFF sample types: {path.name}")
                    for index in src.indexes:
                        existing_nm = src.tags(index).get("WAVELENGTH")
                        if existing_nm is not None:
                            try:
                                matches = float(existing_nm) == wavelengths[index - 1]
                            except ValueError:
                                matches = False
                            if not matches:
                                raise ValueError(
                                    f"Band {index} wavelength conflicts with stored "
                                    f"metadata in {path.name}: {existing_nm}"
                                )
            layout = "multisample_14_band"
        else:
            raise ValueError(
                f"Expected 14 grayscale pages or one 14-band image; "
                f"found {len(pages)} pages in {path.name}."
            )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        with rasterio.open(path) as src:
            gcps, _ = src.gcps
            if gcps or src.rpcs is not None:
                raise ValueError(
                    f"GCP/RPC referencing requires special handling: {path.name}"
                )
    return ImageInfo(path, width, height, str(dtype), layout)


def crop_box(info: ImageInfo, target_width: int, target_height: int) -> dict[str, int]:
    left = (info.width - target_width) // 2
    top = (info.height - target_height) // 2
    right = info.width - target_width - left
    bottom = info.height - target_height - top
    if min(left, right, top, bottom) < 0:
        raise ValueError(f"Target dimensions exceed source: {info.path}")
    return {
        "original_width": info.width,
        "original_height": info.height,
        "target_width": target_width,
        "target_height": target_height,
        "crop_left": left,
        "crop_right": right,
        "crop_top": top,
        "crop_bottom": bottom,
        "band_count": 14,
    }


def write_one(
    info: ImageInfo,
    destination: Path,
    crop: dict[str, int],
    wavelengths: tuple[int, ...],
    args,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.{uuid4().hex}.tmp.tif")
    left, top = crop["crop_left"], crop["crop_top"]
    width, height = crop["target_width"], crop["target_height"]

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", NotGeoreferencedWarning)
            with rasterio.open(info.path) as src:
                profile = {
                    "driver": "GTiff",
                    "width": width,
                    "height": height,
                    "count": 14,
                    "dtype": info.dtype,
                    "interleave": "band",
                    "compress": "deflate",
                    "BIGTIFF": "IF_SAFER",
                }
                if src.crs is not None:
                    profile["crs"] = src.crs
                if src.crs is not None or not src.transform.is_identity:
                    profile["transform"] = src.window_transform(
                        Window(left, top, width, height)
                    )
                if src.nodata is not None:
                    profile["nodata"] = src.nodata

                with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
                    with rasterio.open(temporary, "w", **profile) as dst:
                        dataset_tags = {
                            key: value for key, value in src.tags().items()
                            if not key.startswith("TIFFTAG_")
                            and key not in {"BAND_ORDER_SOURCE", "IMAGE_TYPE", "RADIOMETRIC_CALIBRATION"}
                        }
                        if dataset_tags:
                            dst.update_tags(**dataset_tags)
                        dst.update_tags(
                            BAND_ORDER_SOURCE="Project wavelength map (verify against camera)",
                            IMAGE_TYPE="Aligned multispectral intensity",
                            RADIOMETRIC_CALIBRATION="not_applied",
                            INTRINSIC_CALIBRATION="not_provided",
                        )
                        if args.camera_model:
                            dst.update_tags(CAMERA_MODEL=args.camera_model)
                        if args.focal_length_mm is not None:
                            dst.update_tags(FOCAL_LENGTH_MM=str(args.focal_length_mm))
                        if args.pixel_size_um is not None:
                            dst.update_tags(PIXEL_SIZE_UM=str(args.pixel_size_um))
                        for index, wavelength in enumerate(wavelengths, start=1):
                            if info.layout == "multisample_14_band":
                                old_tags = src.tags(index)
                                if old_tags:
                                    dst.update_tags(index, **old_tags)
                            dst.set_band_description(index, f"{wavelength} nm")
                            dst.update_tags(
                                index,
                                WAVELENGTH=str(wavelength),
                                WAVELENGTH_UNITS="nm",
                            )

                        if info.layout == "legacy_14_page":
                            with tifffile.TiffFile(info.path) as tif:
                                for index, page in enumerate(tif.pages, start=1):
                                    band = page.asarray()
                                    dst.write(band[top:top + height, left:left + width], index)
                        else:
                            for row in range(0, height, args.chunk_rows):
                                rows = min(args.chunk_rows, height - row)
                                src_window = Window(left, top + row, width, rows)
                                dst_window = Window(0, row, width, rows)
                                dst.write(src.read(window=src_window), window=dst_window)

                            if any(
                                MaskFlags.per_dataset in flags
                                for flags in src.mask_flag_enums
                            ):
                                for row in range(0, height, args.chunk_rows):
                                    rows = min(args.chunk_rows, height - row)
                                    dst.write_mask(
                                        src.dataset_mask(
                                            window=Window(left, top + row, width, rows)
                                        ),
                                        window=Window(0, row, width, rows),
                                    )

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", NotGeoreferencedWarning)
            with rasterio.open(temporary) as check:
                if (
                    check.count != 14
                    or check.width != width
                    or check.height != height
                    or check.descriptions != tuple(f"{nm} nm" for nm in wavelengths)
                ):
                    raise IOError(f"Written TIFF failed validation: {temporary}")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv=None) -> int:
    args = arguments(argv)
    print("Metashape TIFF preparation started", flush=True)
    input_dir = args.input.expanduser().resolve()
    output_dir = args.output.expanduser().resolve()
    if not input_dir.is_dir():
        raise ValueError(f"Input directory does not exist: {input_dir}")
    if input_dir == output_dir or input_dir in output_dir.parents:
        raise ValueError("Output must be outside the input directory.")
    if args.chunk_rows < 1:
        raise ValueError("--chunk-rows must be positive.")
    for key in ("focal_length_mm", "pixel_size_um"):
        value = getattr(args, key)
        if value is not None and (not np.isfinite(value) or value <= 0):
            raise ValueError(f"--{key.replace('_', '-')} must be positive and finite.")

    wavelengths = parse_wavelengths(args.wavelengths_nm)
    paths = discover_images(input_dir, args.recursive)
    if not paths:
        raise ValueError(f"No TIFF files found in {input_dir}")

    print(f"Scanning {len(paths)} TIFF files...")
    images = [inspect(path, wavelengths) for path in paths]
    target_width = min(item.width for item in images)
    target_height = min(item.height for item in images)
    print(
        f"Target: {target_width} x {target_height}; "
        f"14 bands; wavelengths: {', '.join(map(str, wavelengths))} nm"
    )

    report = []
    for index, info in enumerate(images, start=1):
        relative = info.path.relative_to(input_dir)
        dest = output_dir / relative
        box = crop_box(info, target_width, target_height)
        if args.dry_run:
            status = "dry-run"
        elif dest.exists() and not args.overwrite:
            status = "skipped-existing"
        else:
            write_one(info, dest, box, wavelengths, args)
            status = "written"
        report.append({
            "source": str(relative),
            "destination": str(dest),
            "layout": info.layout,
            **box,
            "status": status,
        })
        print(
            f"[{index}/{len(images)}] {status}: {relative} "
            f"({info.width}x{info.height} -> {target_width}x{target_height})",
            flush=True,
        )

    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / "crop_report.csv"
        with report_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=REPORT_COLUMNS)
            writer.writeheader()
            writer.writerows(report)
        print(f"Report: {report_path}")
    print("Finished. Original TIFF files were not modified.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
