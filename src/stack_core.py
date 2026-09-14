"""
stack_core.py

Primary:
- Direct reference alignment: 1 -> 2,3,4,5,6 using estimate_xy_v4

Plan B:
- If primary fails, switch completely to 2/5-centered layout:
    2 -> 1
    2 -> 3
    2 -> 5
    5 -> 4
    5 -> 6

Plan B does NOT use 1 -> 3.
"""

from pathlib import Path
import csv
import cv2
import tifffile

from alignment_core import estimate_xy_v4, DEFAULT_CONFIG
from alignment_plan_b import estimate_xy_plan_b


FILE_BANDS = {
    1: ("0",   "0",   "850"),
    2: ("0",   "525", "630"),
    3: ("405", "570", "710"),
    4: ("430", "550", "650"),
    5: ("450", "560", "685"),
    6: ("490", "0",   "735"),
}

OUTPUT_ORDER = [
    "405", "430", "450", "490",
    "525", "550", "560", "570",
    "630", "650", "685", "710",
    "735", "850",
]


def load_split_images(split_dir: Path):
    images = {}
    for idx in range(1, 7):
        path = split_dir / f"{idx}.jpg"
        img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if img is None:
            raise FileNotFoundError(f"Could not load split image: {path}")
        images[idx] = img
    return images


def estimate_pair_primary(ref_img, mov_img, config=None):
    result = estimate_xy_v4(ref_img, mov_img, config=config)

    if result is None or not result["accepted"]:
        reason = "none" if result is None else result["reason"]
        raise RuntimeError(reason)

    raw_dx = int(result["dx"])
    raw_dy = int(result["dy"])

    return {
        "raw_dx": raw_dx,
        "raw_dy": raw_dy,
        "apply_dx": -raw_dx,
        "apply_dy": -raw_dy,
        "confidence": float(result["confidence"]),
        "support": int(result["support"]),
        "num_votes": int(result["num_votes"]),
        "reason": str(result["reason"]),
        "method": "primary",
        "algorithm": "phase_patch_consensus",
    }


def estimate_pair_plan_b(ref_img, mov_img, pair_hint):
    result = estimate_xy_plan_b(
        ref_img,
        mov_img,
        pair_hint=pair_hint,
        config=None,
    )

    if result is None or not result["accepted"]:
        reason = "none" if result is None else f"{result['method']} rejected"
        raise RuntimeError(reason)

    raw_dx = int(result["dx"])
    raw_dy = int(result["dy"])

    num_votes = int(
        result["details"].get(
            "num_votes",
            result["details"].get("matches", result["support"]),
        )
    )

    return {
        "raw_dx": raw_dx,
        "raw_dy": raw_dy,
        "apply_dx": -raw_dx,
        "apply_dy": -raw_dy,
        "confidence": float(result["score"]),
        "support": int(result["support"]),
        "num_votes": num_votes,
        "reason": f"plan_b:{result['method']}",
        "method": "plan_b",
        "algorithm": str(result["method"]),
    }


def compute_primary_alignment(images, config=None):
    """
    Old direct-reference method:
        1 -> 2
        1 -> 3
        1 -> 4
        1 -> 5
        1 -> 6
    """
    ref_img = images[1]

    pairs = {}
    apply_shifts = {
        1: {"dx": 0, "dy": 0},
    }
    raw_shifts = {
        1: {"dx": 0, "dy": 0},
    }

    for idx in range(2, 7):
        pair_key = f"1-{idx}"
        pair = estimate_pair_primary(ref_img, images[idx], config=config)

        pairs[pair_key] = pair
        raw_shifts[idx] = {
            "dx": pair["raw_dx"],
            "dy": pair["raw_dy"],
        }
        apply_shifts[idx] = {
            "dx": pair["apply_dx"],
            "dy": pair["apply_dy"],
        }

    return {
        "reference_id": 1,
        "mode": "primary_direct_1_reference",
        "pairs": pairs,
        "raw_shifts": raw_shifts,
        "apply_shifts": apply_shifts,
    }


def compute_plan_b_alignment(images):
    """
    Plan B layout:
        2 -> 1
        2 -> 3
        2 -> 5
        5 -> 4
        5 -> 6

    Global reference:
        image 2 = (0, 0)
    """
    pairs = {}

    pairs["2-1"] = estimate_pair_plan_b(images[2], images[1], pair_hint=(2, 1))
    pairs["2-3"] = estimate_pair_plan_b(images[2], images[3], pair_hint=(2, 3))
    pairs["2-5"] = estimate_pair_plan_b(images[2], images[5], pair_hint=(2, 5))
    pairs["5-4"] = estimate_pair_plan_b(images[5], images[4], pair_hint=(5, 4))
    pairs["5-6"] = estimate_pair_plan_b(images[5], images[6], pair_hint=(5, 6))

    apply_shifts = {
        2: {"dx": 0, "dy": 0},
    }

    apply_shifts[1] = {
        "dx": pairs["2-1"]["apply_dx"],
        "dy": pairs["2-1"]["apply_dy"],
    }

    apply_shifts[3] = {
        "dx": pairs["2-3"]["apply_dx"],
        "dy": pairs["2-3"]["apply_dy"],
    }

    apply_shifts[5] = {
        "dx": pairs["2-5"]["apply_dx"],
        "dy": pairs["2-5"]["apply_dy"],
    }

    apply_shifts[4] = {
        "dx": apply_shifts[5]["dx"] + pairs["5-4"]["apply_dx"],
        "dy": apply_shifts[5]["dy"] + pairs["5-4"]["apply_dy"],
    }

    apply_shifts[6] = {
        "dx": apply_shifts[5]["dx"] + pairs["5-6"]["apply_dx"],
        "dy": apply_shifts[5]["dy"] + pairs["5-6"]["apply_dy"],
    }

    raw_shifts = {}

    return {
        "reference_id": 2,
        "mode": "plan_b_2_5_centered_layout",
        "pairs": pairs,
        "raw_shifts": raw_shifts,
        "apply_shifts": apply_shifts,
    }


def estimate_alignment_from_split(split_dir: Path, config=None):
    """
    First try the old primary alignment.
    If any pair fails, switch completely to Plan B layout.
    """
    if config is None:
        config = DEFAULT_CONFIG.copy()

    images = load_split_images(split_dir)

    try:
        print("Trying primary alignment...")
        return compute_primary_alignment(images, config=config)

    except Exception as e:
        print(f"Primary alignment failed: {e}")
        print("Switching to Plan B 2/5-centered alignment...")
        return compute_plan_b_alignment(images)


def build_band_map():
    band_map = {}
    rgb_to_bgr = {0: 2, 1: 1, 2: 0}

    for file_id, labels in FILE_BANDS.items():
        for rgb_pos, band_name in enumerate(labels):
            if band_name == "0":
                continue
            band_map[band_name] = (file_id, rgb_to_bgr[rgb_pos])

    return band_map



def classify_alignment_quality(pair_result):
    """Assign a conservative quality label to an accepted alignment pair."""
    confidence = float(pair_result.get("confidence", 0.0))
    support = int(pair_result.get("support", 0))
    algorithm = pair_result.get("algorithm", pair_result.get("method", ""))

    if algorithm == "feature_ransac_translation":
        if support >= 20:
            return "high"
        if support >= 6:
            return "medium"
        return "low"

    if confidence >= 0.35 and support >= 4:
        return "high"
    if confidence >= 0.20 and support >= 2:
        return "medium"
    return "low"


def summarize_alignment_quality(alignment_result):
    rows = []

    for pair_name, pair in alignment_result.get("pairs", {}).items():
        rows.append({
            "pair": pair_name,
            "method": pair.get("method", ""),
            "algorithm": pair.get("algorithm", ""),
            "quality": classify_alignment_quality(pair),
            "confidence": float(pair.get("confidence", 0.0)),
            "support": int(pair.get("support", 0)),
            "num_votes": int(pair.get("num_votes", 0)),
            "raw_dx": int(pair.get("raw_dx", 0)),
            "raw_dy": int(pair.get("raw_dy", 0)),
            "apply_dx": int(pair.get("apply_dx", 0)),
            "apply_dy": int(pair.get("apply_dy", 0)),
            "reason": pair.get("reason", ""),
        })

    if not rows:
        return {"overall_quality": "not_available", "mean_confidence": 0.0, "min_support": 0, "rows": rows}

    quality_rank = {"low": 1, "medium": 2, "high": 3}
    worst_rank = min(quality_rank[row["quality"]] for row in rows)

    return {
        "overall_quality": {v: k for k, v in quality_rank.items()}[worst_rank],
        "mean_confidence": sum(row["confidence"] for row in rows) / len(rows),
        "min_support": min(row["support"] for row in rows),
        "rows": rows,
    }


def write_alignment_quality_report(output_tiff: Path, export_result):
    """Write text and CSV alignment quality reports next to the exported TIFF."""
    alignment = export_result["alignment"]
    summary = summarize_alignment_quality(alignment)

    txt_path = output_tiff.with_name(output_tiff.stem + "_alignment_quality.txt")
    csv_path = output_tiff.with_name(output_tiff.stem + "_alignment_quality.csv")

    lines = [
        "Alignment quality report",
        "========================",
        f"Output TIFF: {output_tiff.name}",
        f"Alignment mode: {alignment.get('mode', '')}",
        f"Reference image ID: {alignment.get('reference_id', '')}",
        f"Overall quality: {summary['overall_quality']}",
        f"Mean confidence: {summary['mean_confidence']:.4f}",
        f"Minimum support: {summary['min_support']}",
        "",
        "Per-pair results:",
    ]

    for row in summary["rows"]:
        lines.append(
            f"- {row['pair']}: quality={row['quality']}, "
            f"method={row['method']}, algorithm={row['algorithm']}, "
            f"confidence={row['confidence']:.4f}, support={row['support']}/{row['num_votes']}, "
            f"raw_shift=({row['raw_dx']}, {row['raw_dy']}), "
            f"applied_shift=({row['apply_dx']}, {row['apply_dy']}), "
            f"reason={row['reason']}"
        )

    txt_path.write_text("\n".join(lines), encoding="utf-8")

    fieldnames = [
        "pair", "method", "algorithm", "quality", "confidence",
        "support", "num_votes", "raw_dx", "raw_dy", "apply_dx", "apply_dy", "reason",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(summary["rows"])

    return {"summary": summary, "txt": str(txt_path), "csv": str(csv_path)}


def export_multipage_tiff_from_split(
    split_dir: Path,
    output_tiff: Path,
    config=None,
):
    alignment_result = estimate_alignment_from_split(split_dir, config=config)
    apply_shifts = alignment_result["apply_shifts"]

    print(f"Alignment mode: {alignment_result['mode']}")
    print("Apply shifts:")
    for idx in sorted(apply_shifts.keys()):
        print(f"  {idx}: dx={apply_shifts[idx]['dx']}, dy={apply_shifts[idx]['dy']}")

    images = load_split_images(split_dir)

    ref_h, ref_w = images[1].shape[:2]
    for idx, img in images.items():
        h, w = img.shape[:2]
        if (h, w) != (ref_h, ref_w):
            raise ValueError(f"Image size mismatch in split set: {idx}.jpg")

    warped = {}
    masks = {}

    for idx, img in images.items():
        dx = int(apply_shifts[idx]["dx"])
        dy = int(apply_shifts[idx]["dy"])

        M = cv2.getRotationMatrix2D((0, 0), 0, 1.0)
        M[0, 2] = dx
        M[1, 2] = dy

        warped_img = cv2.warpAffine(
            img,
            M,
            (ref_w, ref_h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )

        mask = 255 * (cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) >= 0).astype("uint8")
        warped_mask = cv2.warpAffine(
            mask,
            M,
            (ref_w, ref_h),
            flags=cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )

        warped[idx] = warped_img
        masks[idx] = warped_mask

    common = None
    for idx in range(1, 7):
        if common is None:
            common = masks[idx].copy()
        else:
            common = cv2.bitwise_and(common, masks[idx])

    ys, xs = (common > 0).nonzero()
    if len(xs) == 0 or len(ys) == 0:
        raise RuntimeError("No common valid area found after alignment.")

    x0, x1 = int(xs.min()), int(xs.max()) + 1
    y0, y1 = int(ys.min()), int(ys.max()) + 1

    band_map = build_band_map()
    output_tiff.parent.mkdir(parents=True, exist_ok=True)

    with tifffile.TiffWriter(str(output_tiff)) as tif:
        for band_name in OUTPUT_ORDER:
            file_id, ch_idx = band_map[band_name]
            band = warped[file_id][y0:y1, x0:x1, ch_idx]

            tif.write(
                band,
                photometric="minisblack",
                contiguous=False,
                metadata={"band_name": band_name},
            )

    export_result = {
        "alignment": alignment_result,
        "crop_box": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
        "final_size": {"width": x1 - x0, "height": y1 - y0},
        "output_tiff": str(output_tiff),
    }

    export_result["quality_report"] = write_alignment_quality_report(
        output_tiff=output_tiff,
        export_result=export_result,
    )

    return export_result