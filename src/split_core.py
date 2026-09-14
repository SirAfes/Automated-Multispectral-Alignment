"""
split_core.py

Split a 3x2 compound multispectral image into 6 cropped lens images.
"""

from pathlib import Path
import cv2


def center_crop(img, frac: float):
    h, w = img.shape[:2]
    ch = int(h * frac)
    cw = int(w * frac)

    y0 = (h - ch) // 2
    x0 = (w - cw) // 2

    return img[y0:y0 + ch, x0:x0 + cw].copy()


def save_image(path: Path, img, jpeg_quality: int = 100):
    path.parent.mkdir(parents=True, exist_ok=True)

    ext = path.suffix.lower()
    if ext in [".jpg", ".jpeg"]:
        ok = cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
    else:
        ok = cv2.imwrite(str(path), img)

    if not ok:
        raise IOError(f"Could not save file: {path}")


def split_compound_image(
    input_image: Path,
    output_dir: Path,
    grid_cols: int = 3,
    grid_rows: int = 2,
    cell_size: int = 3168,
    center_crop_frac: float = 0.80,
    save_ext: str = ".jpg",
    jpeg_quality: int = 100,
):
    """
    Split a compound image into 6 cells and apply center crop before saving.

    Primary mode:
    - Uses the expected fixed grid size based on cell_size

    Fallback mode:
    - If the input size differs, uses proportional grid boundaries computed
      from the actual image size, while keeping the same 3x2 split logic

    Returns:
        dict[int, Path]: Mapping from image id (1..6) to saved file path
    """
    if not input_image.exists():
        raise FileNotFoundError(f"Input image not found: {input_image}")

    img = cv2.imread(str(input_image), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise FileNotFoundError(f"Could not load image: {input_image}")

    h, w = img.shape[:2]
    expected_w = grid_cols * cell_size
    expected_h = grid_rows * cell_size

    output_dir.mkdir(parents=True, exist_ok=True)

    # Default fixed-grid mode
    if w == expected_w and h == expected_h:
        x_edges = [col * cell_size for col in range(grid_cols + 1)]
        y_edges = [row * cell_size for row in range(grid_rows + 1)]
    else:
        # Proportional fallback mode for alternate image sizes
        x_edges = [round(i * w / grid_cols) for i in range(grid_cols + 1)]
        y_edges = [round(i * h / grid_rows) for i in range(grid_rows + 1)]

    saved = {}
    idx = 1
    for row in range(grid_rows):
        for col in range(grid_cols):
            x0 = x_edges[col]
            x1 = x_edges[col + 1]
            y0 = y_edges[row]
            y1 = y_edges[row + 1]

            cell = img[y0:y1, x0:x1].copy()
            cell = center_crop(cell, center_crop_frac)

            target_size = int(cell_size * center_crop_frac)

            if cell.shape[0] != target_size or cell.shape[1] != target_size:
                cell = cv2.resize(
                    cell,
                    (target_size, target_size),
                    interpolation=cv2.INTER_AREA,
                )

            out_path = output_dir / f"{idx}{save_ext}"
            save_image(out_path, cell, jpeg_quality=jpeg_quality)

            saved[idx] = out_path
            idx += 1

    return saved