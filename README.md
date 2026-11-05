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

The application asks whether to process **selected images** or a **whole folder**, then processing begins automatically. When a folder contains the RAW and the JPG of the same shot, only the RAW file is used. Subfolders (for example earlier outputs) are not scanned.

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

Images are processed in natural file-name order (`DSC2` before `DSC10`), so "neighbouring" really means neighbouring shots.

## Large batches: batch consensus mode

When 50 or more images are selected, the pipeline switches to **batch consensus mode**. It is designed for large series taken at the same distance, where every image has (nearly) the same lens geometry:

```text
Calibration:  ~16 images spread over the batch are fully aligned
                   ↓
Consensus:    common lens geometry, only if the calibration images agree
                   ↓
Every other image:
    verify the geometry on this image (small local search)
    ├─ confirmed → apply it, plus the small measured per-image correction
    └─ not confirmed → full per-image alignment, as in normal mode
```

Each image is still checked against its own content, so a frame taken at a different distance or a bad frame is not silently forced onto the batch geometry. If the calibration images themselves disagree (for example the batch mixes distances), all images are aligned individually.

In a synthetic test at full resolution (9504 × 6336), the full alignment took about 70 s per image and a consensus image about 1 s, with identical shifts. A 600-image batch then needs roughly 16 full alignments plus 584 fast images instead of 600 full alignments. Actual timings depend on the computer and on RAW decoding.

Details: [Technical Notes](docs/TECHNICAL_NOTES.md#batch-consensus-mode).

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

## Supported input formats

| Type | Extensions | Split lens images | Final TIFF |
|---|---|---|---|
| Standard | `.jpg`, `.jpeg`, `.png`, `.tif`, `.tiff` | `1.jpg … 6.jpg` | 8-bit |
| Camera RAW | `.arw`, `.raw` (also `.dng`, `.nef`, `.cr2`, `.cr3`, `.orf`, `.rw2`, `.raf`, `.pef`, `.srw`) | `1.tif … 6.tif` (16-bit, lossless) | 16-bit |

RAW files are decoded with [rawpy](https://github.com/letmaik/rawpy) (LibRaw) as close to the sensor data as possible: no white balance, no camera colour matrix, linear response and no automatic brightening. Each sensor channel therefore stays a separate spectral measurement, and the same band mapping is used as for JPG input.

The final TIFF is also placed in a shared `tiff/` folder beside the source images. By default this is a hard link — the same file under a second name, so it uses no extra disk space (relevant for 16-bit RAW output of roughly 180 MB per image). Set `"collection_mode": "copy"` for an independent copy or `"off"` to disable it.

RAW outputs get the RAW extension in their name, so a RAW+JPG pair of the same shot does not overwrite each other — for example `DSC06664.ARW` produces `DSC06664_ARW/` and `DSC06664_ARW_aligned_multiband.tif`.

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

## Settings file

Camera layout, band mapping and batch behaviour can be changed without editing code. Copy `pipeline_config.example.json` to `pipeline_config.json` next to the `.exe` (or into the repository root when running from source) and keep only the settings you want to change. All settings and their defaults are documented in `src/settings.py`. Unknown or inconsistent settings are rejected with an error message at start-up.

## Running from Python

Users who want to inspect or modify the source can run it directly.

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python src\run_pipeline_gui.py
```

Run the tests with:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest tests
```

The tests use synthetic compound images with known lens shifts and run on every push (GitHub Actions, Windows and Linux).

Python 3.11–3.13 is recommended. OpenCV is constrained to a 4.x build because the feature fallback uses the `AKAZE_create` API.

## Repository structure

```text
.github/    GitHub Actions Windows build workflow
build/      PyInstaller / Windows build files
docs/       Technical implementation notes
examples/   Example-data guidance
src/        Alignment, stacking, batch logic and desktop interface
tests/      Automated tests with synthetic images
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
