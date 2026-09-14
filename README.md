# Automated Multispectral Alignment

**Automated Multispectral Alignment** is a desktop-oriented image-processing pipeline for multi-lens multispectral imagery. It takes a compound image, separates it into six lens images, automatically registers the views, extracts the configured spectral bands, and exports a spatially aligned 14-band TIFF.

The project is intended for researchers and imaging users who need a practical way to prepare close-range or non-aerial multispectral imagery for later analysis without manually aligning each lens image.

## What it does

```text
Compound multispectral image
          ↓
Split into 6 lens images
          ↓
Automatic inter-lens registration
          ↓
Alignment quality assessment
          ↓
14 spectral bands assembled in wavelength order
          ↓
Aligned multiband TIFF
```

Each selected photograph is processed **from start to finish before the next photograph begins**. This keeps each image set independent and avoids mixing intermediate results between photographs.

## Typical use

1. Start the Windows application or run the Python interface.
2. Select one or more compound multispectral images.
3. The software creates six individual lens images for the current photograph.
4. The lens images are spatially registered using a multi-stage alignment procedure.
5. The aligned spectral information is assembled into a 14-band TIFF.
6. An alignment-quality report is written beside the TIFF.
7. Only after that photograph is complete does processing continue to the next selected image.

No manual point selection is required during normal processing.

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

The TIFF contains the configured bands in the following wavelength order:

`405, 430, 450, 490, 525, 550, 560, 570, 630, 650, 685, 710, 735, 850 nm`

## For users who do not work with Python

The recommended option is the Windows executable. Once a compiled release is available, download `Automated_Multispectral_Alignment.exe`, open it, select the images, and allow the batch to complete.

The executable is designed to include the required Python packages, so end users do not need to install OpenCV or configure a Python environment.

## Running from source

Python users can run the project directly:

```bash
python -m pip install -r requirements.txt
python src/run_pipeline_gui.py
```

Python 3.11-3.13 is recommended. OpenCV is intentionally constrained to version 4.x because the current fallback registration stage uses the `AKAZE_create` API.

## Alignment approach

The software uses a staged registration strategy rather than relying on one alignment method for every image:

1. **Primary registration** estimates global translation using phase correlation and local patch agreement.
2. **Plan B** uses directional patch consensus when the primary solution is not sufficiently reliable.
3. **Plan C** uses feature-based AKAZE matching as a final fallback.

The resulting translations are applied to the lens images and a common valid image area is used for the final multiband product. More technical detail is available in [Technical Notes](docs/TECHNICAL_NOTES.md).

## Alignment quality report

A text and CSV report are generated for each completed image. These reports record the method used, estimated displacement, confidence/score information and support for each registration pair. They are intended to make batch processing easier to review and to identify images that may require visual inspection.

The quality value is an **alignment quality indicator**, not a classification accuracy or ecological confidence score.

## Intended scope

The current configuration is designed for a six-lens compound multispectral image layout producing 14 spectral bands. The project was developed particularly for close-range imagery, where parallax, scene depth and acquisition geometry can make inter-lens registration more difficult than in typical aerial workflows.

Different camera layouts, band arrangements or optical geometries may require changes to the splitting geometry, wavelength mapping or alignment settings.

## Repository structure

```text
src/        Core image processing and user interface
build/      Windows executable build files
docs/       Technical notes
examples/   Guidance for example datasets
.github/    Automated Windows build workflow
```

## Building the Windows executable

On a Windows machine, double-click:

```text
build/build_windows.bat
```

The finished application will be created as:

```text
build-output/Automated_Multispectral_Alignment.exe
```

The repository also includes a GitHub Actions workflow that can build the Windows executable automatically.

## Validation and responsible use

Automatic registration should still be visually checked when imagery contains strong parallax, moving subjects, low texture, severe illumination differences or substantial occlusion between lenses. The generated quality report is a screening aid and should not replace visual validation for scientific datasets.

## Status

This project is under active research development. Interface, calibration and registration parameters may evolve as additional close-range multispectral datasets are evaluated.
