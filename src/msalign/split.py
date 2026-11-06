"""
split.py

Split a compound multi-lens image into cropped lens images (grid_cols x grid_rows).
"""

import json
import logging
from pathlib import Path

import cv2
import numpy as np

from .image_io import read_image

log = logging.getLogger(__name__)


def _center_box(h, w, ch, cw):
    y0 = (h - ch) // 2
    x0 = (w - cw) // 2
    return x0, y0, cw, ch


def center_crop(img, frac: float):
    h, w = img.shape[:2]
    x0, y0, cw, ch = _center_box(h, w, int(h * frac), int(w * frac))
    return img[y0:y0 + ch, x0:x0 + cw].copy()


def center_crop_size(img, size: int):
    h, w = img.shape[:2]
    x0, y0, _, _ = _center_box(h, w, size, size)
    return img[y0:y0 + size, x0:x0 + size].copy()


def save_image(path: Path, img, jpeg_quality: int = 100):
    path.parent.mkdir(parents=True, exist_ok=True)

    ext = path.suffix.lower()
    if ext in [".jpg", ".jpeg"]:
        if img.dtype != np.uint8:
            raise ValueError(f"JPEG cannot store {img.dtype} data; use .tif for {path.name}")
        params = [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality]
    else:
        params = []

    # imencode + tofile also works for non-ASCII Windows paths, unlike cv2.imwrite.
    ok, buf = cv2.imencode(ext, img, params)
    if not ok:
        raise IOError(f"Could not save file: {path}")
    buf.tofile(str(path))


def split_compound_array(
    img,
    grid_cols: int = 3,
    grid_rows: int = 2,
    cell_size: int = 3168,
    center_crop_frac: float = 0.80,
):
    """
    Split an in-memory compound image into lens cells.

    Fixed-grid mode:
        The image is exactly grid * cell_size; cells are cut on that grid.

    Proportional mode (other image sizes, e.g. a RAW decoder adding or
    trimming a few border pixels):
        Grid boundaries are computed proportionally. If a cell is large
        enough, the target-size square is cut from its centre directly, so the
        pixel scale stays native and shifts are comparable with fixed-grid
        images. Only cells that are too small are resized.

    Returns:
        (cells, info): cells is dict[int, ndarray] for lens 1..N,
        info describes the source size and the split mode used.
    """
    h, w = img.shape[:2]
    expected_w = grid_cols * cell_size
    expected_h = grid_rows * cell_size
    target_size = int(cell_size * center_crop_frac)

    if w == expected_w and h == expected_h:
        mode = "fixed_grid"
        x_edges = [col * cell_size for col in range(grid_cols + 1)]
        y_edges = [row * cell_size for row in range(grid_rows + 1)]
    else:
        mode = "proportional"
        x_edges = [round(i * w / grid_cols) for i in range(grid_cols + 1)]
        y_edges = [round(i * h / grid_rows) for i in range(grid_rows + 1)]

    cells = {}
    origins = {}
    resized = False
    scale = 1.0
    idx = 1
    for row in range(grid_rows):
        for col in range(grid_cols):
            cell = img[y_edges[row]:y_edges[row + 1], x_edges[col]:x_edges[col + 1]]
            h_c, w_c = cell.shape[:2]

            if mode == "fixed_grid":
                bx, by, bw, bh = _center_box(h_c, w_c, int(h_c * center_crop_frac), int(w_c * center_crop_frac))
                cell = cell[by:by + bh, bx:bx + bw]
            elif min(h_c, w_c) >= target_size:
                bx, by, bw, bh = _center_box(h_c, w_c, target_size, target_size)
                cell = cell[by:by + bh, bx:bx + bw]
            else:
                bx, by, bw, bh = _center_box(h_c, w_c, int(h_c * center_crop_frac), int(w_c * center_crop_frac))
                cell = cv2.resize(cell[by:by + bh, bx:bx + bw], (target_size, target_size),
                                  interpolation=cv2.INTER_AREA)
                resized = True
                scale = max(bw, bh) / target_size

            # top-left of this lens image in the source (sensor) image
            origins[idx] = [int(x_edges[col] + bx), int(y_edges[row] + by)]
            cells[idx] = np.ascontiguousarray(cell)
            idx += 1

    info = {
        "source_width": int(w),
        "source_height": int(h),
        "expected_width": int(expected_w),
        "expected_height": int(expected_h),
        "split_mode": mode + ("_resized" if resized else ""),
        "lens_size": int(target_size),
        # source pixels per lens-image pixel (1.0 = native resolution)
        "lens_scale": round(float(scale), 6),
        "lens_origins": {str(k): v for k, v in origins.items()},
    }
    if mode != "fixed_grid":
        log.warning(
            f"source is {w}x{h}, expected {expected_w}x{expected_h}; "
            f"using {info['split_mode']} split."
        )
    return cells, info


SPLIT_INFO_NAME = "split_info.json"


def save_split_cells(cells, output_dir: Path, save_ext: str = ".jpg", jpeg_quality: int = 100,
                     info: dict | None = None):
    output_dir.mkdir(parents=True, exist_ok=True)
    saved = {}
    for idx, cell in cells.items():
        out_path = output_dir / f"{idx}{save_ext}"
        save_image(out_path, cell, jpeg_quality=jpeg_quality)
        saved[idx] = out_path
    if info:
        (output_dir / SPLIT_INFO_NAME).write_text(json.dumps(info, indent=2), encoding="utf-8")
    return saved


def load_split_info(split_dir: Path) -> dict:
    path = Path(split_dir) / SPLIT_INFO_NAME
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def split_compound_image(
    input_image: Path,
    output_dir: Path,
    grid_cols: int = 3,
    grid_rows: int = 2,
    cell_size: int = 3168,
    center_crop_frac: float = 0.80,
    save_ext: str = ".jpg",
    jpeg_quality: int = 100,
    info: dict | None = None,
):
    """
    Split a compound image file into 6 cropped lens images and save them.

    Accepts standard images and camera RAW files (see image_io). RAW inputs are
    16-bit and should be saved with save_ext=".tif"; JPG cannot hold 16-bit data.

    If `info` is a dict, it is filled with the split information.

    Returns:
        dict[int, Path]: Mapping from image id (1..6) to saved file path
    """
    img = read_image(input_image)
    cells, split_info = split_compound_array(
        img,
        grid_cols=grid_cols,
        grid_rows=grid_rows,
        cell_size=cell_size,
        center_crop_frac=center_crop_frac,
    )
    if info is not None:
        info.update(split_info)
    return save_split_cells(cells, output_dir, save_ext=save_ext, jpeg_quality=jpeg_quality,
                            info=split_info)
