# Automated Multispectral Alignment

**Automated Multispectral Alignment** is a desktop image-processing pipeline for multi-lens multispectral imagery. It takes a compound multispectral photograph, separates it into six lens images, automatically aligns the lens views, extracts the configured spectral bands, and exports a spatially aligned 14-band TIFF.

The software is intended for researchers and imaging users, including users who do not normally work with Python or software development.

## What it does

```text
Compound multispectral image
            ↓
    Split into 6 lens images
            ↓
 Automatic inter-lens alignment
            ↓
   Alignment quality assessment
            ↓
   Extract 14 spectral bands
            ↓
  Determine common valid region
            ↓
    Aligned 14-band TIFF
```

No manual control-point selection is required during normal processing.

## Windows — no Python required

The easiest way to use the project on Windows is the standalone executable built by GitHub Actions.

1. Open the **Actions** tab at the top of this repository.
2. Select **Build Windows executable**.
3. Open the latest successful run marked with a green check.
4. Scroll to **Artifacts**.
5. Download **Automated-Multispectral-Alignment-Windows**.
6. Extract the downloaded ZIP file.
7. Run `Automated_Multispectral_Alignment.exe`.

The application opens a standard file-selection window. Select one or more compound multispectral images and processing begins automatically.

> Windows may display a security warning for an unsigned executable downloaded from the internet. The Actions artifact is built directly from the source code in this repository.

## Desktop processing window

The Windows application uses a graphical progress window rather than a command prompt. During processing it shows:

- the image currently being processed;
- the current processing stage;
- overall progress;
- an estimated remaining time based on completed images;
- live counts for successful, pending-retry and failed images;
- an expandable **Details** section showing the status of every selected image.

## How a batch is processed

The first pass processes the selected images sequentially. Each image is split, aligned and exported before the next image begins.

If an image cannot be aligned reliably, it is **not immediately accepted with a looser threshold**. It is marked for a second-pass retry. Successful neighbouring images are then used to estimate the stable physical displacement between lens pairs, and the unresolved image is tried again around that expected geometry.

This gives the pipeline two useful sources of evidence:

```text
Image evidence from the current photograph
                  +
Lens geometry learned from successful neighbouring photographs
```

If the second pass is still not sufficiently supported, the image remains an error for manual inspection rather than being forced into the final dataset.

## Output

For an input such as `DSC06664.JPG`, the software creates a dedicated output folder:

```text
DSC06664/
├── split_lens_images/
│   ├── 1.jpg
│   ├── 2.jpg
│   ├── 3.jpg
│   ├── 4.jpg
│   ├── 5.jpg
│   └── 6.jpg
├── DSC06664_aligned_multiband.tif
├── DSC06664_aligned_multiband_alignment_quality.txt
└── DSC06664_aligned_multiband_alignment_quality.csv
```

The final TIFF contains 14 bands in wavelength order:

`405, 430, 450, 490, 525, 550, 560, 570, 630, 650, 685, 710, 735, 850 nm`

## Current spectral configuration

| Band | Wavelength |
|---:|---:|
| 1 | 405 nm |
| 2 | 430 nm |
| 3 | 450 nm |
| 4 | 490 nm |
| 5 | 525 nm |
| 6 | 550 nm |
| 7 | 560 nm |
| 8 | 570 nm |
| 9 | 630 nm |
| 10 | 650 nm |
| 11 | 685 nm |
| 12 | 710 nm |
| 13 | 735 nm |
| 14 | 850 nm |

The current reference implementation assumes a six-lens compound image arranged in a 3 × 2 layout. Other camera layouts or wavelength configurations may require changes to the split geometry and band mapping.

## Alignment strategy

The software uses a staged registration strategy:

```text
Primary registration
        ↓ if rejected
Plan B — directional patch consensus
        ↓ if rejected
Plan C — feature-based translation
        ↓ if still rejected and history exists
History-guided pre-shift
        ↓
Primary → Plan B → Plan C again on the small residual
```

Historical information is used as a **starting estimate**, not as an automatic answer. The current image must still provide supporting registration evidence before the result is accepted.

The current geometric model is global translation (`dx`, `dy`). It does not perform dense optical-flow or non-rigid pixel-by-pixel deformation. More implementation detail is available in [Technical Notes](docs/TECHNICAL_NOTES.md).

## Alignment quality report

Every completed TIFF receives text and CSV quality reports containing the registration method, estimated displacement, support and confidence information for each lens pair.

When historical geometry was used, the report also records:

- whether a historical prior was used;
- how many successful images supported that prior;
- the small residual correction found after the pre-shift.

The reported quality is an **alignment quality indicator**, not a biological classification accuracy or an ecological confidence score.

## Running from Python

Users who want to inspect or modify the source can run it directly.

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python src\run_pipeline_gui.py
```

Python 3.11–3.13 is recommended. OpenCV is constrained to a 4.x build because the feature fallback uses the `AKAZE_create` API.

## Repository structure

```text
.github/    GitHub Actions Windows build workflow
build/      PyInstaller / Windows build files
docs/       Technical implementation notes
examples/   Example-data guidance
src/        Alignment, stacking and desktop interface
```

## Building the Windows executable locally

On Windows, run:

```text
build\build_windows.bat
```

The executable is built as:

```text
build-output\Automated_Multispectral_Alignment.exe
```

The PyInstaller configuration uses windowed mode, so the packaged application opens the graphical processing interface without a separate black command-prompt window.

## Intended applications

Potential uses include close-range multispectral imaging, underwater and marine imaging, laboratory imaging, environmental monitoring, biological imagery, material analysis, and preparation of registered multispectral datasets for machine-learning or deep-learning workflows.

## Validation and responsible use

Automatic registration should still be visually checked for scientifically important datasets, particularly when scenes contain strong depth-dependent parallax, moving subjects, low texture, major occlusion or severe spectral/illumination differences.

A failed alignment is intentionally preferable to silently accepting a geometrically unsupported result.

## Status

This project is under active research development. Registration criteria, camera configuration and interface features may evolve as additional close-range multispectral datasets are evaluated.

## License

See the repository `LICENSE` file for the current license terms.
