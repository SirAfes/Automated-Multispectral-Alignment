"""
run_pipeline_gui.py
"""

from pathlib import Path
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox

from split_core import split_compound_image
from stack_core import export_multipage_tiff_from_split


CENTER_CROP_FRAC = 0.80
JPEG_QUALITY = 100
SAVE_EXT = ".jpg"


def process_single_file(input_path: Path):
    base_output_dir = input_path.parent / input_path.stem
    split_dir = base_output_dir / "split_lens_images"
    output_tiff = base_output_dir / f"{input_path.stem}_aligned_multiband.tif"

    print(f"\n=== Processing: {input_path.name} ===")
    print(f"Output folder: {base_output_dir}")

    split_compound_image(
        input_image=input_path,
        output_dir=split_dir,
        center_crop_frac=CENTER_CROP_FRAC,
        save_ext=SAVE_EXT,
        jpeg_quality=JPEG_QUALITY,
    )
    print("Split completed.")

    export_result = export_multipage_tiff_from_split(
        split_dir=split_dir,
        output_tiff=output_tiff,
    )
    print("Hybrid TIFF export completed.")
    print(
        f"Final TIFF: {output_tiff.name} | "
        f"size={export_result['final_size']['width']}x{export_result['final_size']['height']}"
    )

    quality_report = export_result.get("quality_report", {})
    if quality_report:
        print(
            f"Alignment quality: {quality_report['summary']['overall_quality']} | "
            f"mean confidence={quality_report['summary']['mean_confidence']:.4f}"
        )
        print(f"Quality report: {quality_report['txt']}")

    return {
        "input": str(input_path),
        "output_dir": str(base_output_dir),
        "tiff": str(output_tiff),
        "quality_report": quality_report.get("txt", ""),
    }


def main():
    root = tk.Tk()
    root.withdraw()

    file_paths = filedialog.askopenfilenames(
        title="Select one or more compound multispectral images",
        filetypes=[
            ("Image files", "*.jpg *.jpeg *.png *.tif *.tiff"),
            ("All files", "*.*"),
        ],
    )

    if not file_paths:
        print("No files selected.")
        return

    successes = []
    failures = []

    for fp in file_paths:
        input_path = Path(fp)
        try:
            result = process_single_file(input_path)
            successes.append(result)
        except Exception as e:
            failures.append((str(input_path), str(e)))
            print(f"\nERROR while processing {input_path.name}")
            traceback.print_exc()

    summary_lines = []
    summary_lines.append(f"Processed successfully: {len(successes)}")
    summary_lines.append(f"Failed: {len(failures)}")

    if successes:
        summary_lines.append("")
        summary_lines.append("Successful files:")
        for item in successes:
            summary_lines.append(f"- {Path(item['input']).name}")

    if failures:
        summary_lines.append("")
        summary_lines.append("Failed files:")
        for file_name, err in failures:
            summary_lines.append(f"- {Path(file_name).name}: {err}")

    summary_text = "\n".join(summary_lines)
    print("\n" + summary_text)

    messagebox.showinfo("Pipeline summary", summary_text)


if __name__ == "__main__":
    main()