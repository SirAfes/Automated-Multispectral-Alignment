# Automated Multispectral Alignment

**Automated Multispectral Alignment** is a desktop-oriented image-processing pipeline for multi-lens multispectral imagery.

The software takes a compound multispectral image, separates it into six individual lens images, automatically registers the views, extracts the configured spectral bands, and exports a spatially aligned 14-band TIFF.

It is intended for researchers and imaging users who need a practical way to prepare close-range or non-aerial multispectral imagery for subsequent analysis without manually aligning individual lens images.

---

## What it does

The complete processing workflow is automated:

```text
Compound multispectral image
            ↓
    Split into 6 lens images
            ↓
 Automatic inter-lens registration
            ↓
   Alignment quality assessment
            ↓
   Extract 14 spectral bands
            ↓
  Determine common valid region
            ↓
    Aligned 14-band TIFF
```

Each selected source image is processed completely before the next image begins.

This means that for every source image, the software performs splitting, alignment, band preparation and TIFF generation as one continuous workflow.

---

## Windows — No Python Required

For Windows users, the software can be used without installing Python or setting up a programming environment.

A standalone Windows executable is automatically built from the source code in this repository using GitHub Actions.

### Download the Windows application

1. Open the **Actions** tab at the top of this repository.
2. Select **Build Windows executable**.
3. Open the latest successful build marked with a green check.
4. Scroll to the **Artifacts** section.
5. Download **Automated-Multispectral-Alignment-Windows**.
6. Extract the downloaded ZIP archive.
7. Run:

```text
Automated_Multispectral_Alignment.exe
```

The application will open a file-selection window. Select one or more compound multispectral images and processing will begin automatically.

> **Windows security notice:** Windows may display a security warning for an unsigned executable downloaded from the internet. The executable provided through the Actions workflow is automatically built from the source code contained in this repository.

---

## Using the application

When the application starts, select one or more source multispectral images.

For each selected image, the software automatically:

1. separates the compound image into six lens images;
2. estimates the spatial displacement between the lens views;
3. applies the calculated alignment;
4. evaluates the alignment result;
5. determines the common valid image region;
6. extracts and orders the configured spectral bands;
7. creates the final 14-band multispectral TIFF.

Multiple images can be selected at once. They are processed sequentially rather than being mixed into a single processing operation.

---

## Output

Each source image produces its own processing output.

The principal result is:

```text
<original_filename>_aligned_multiband.tif
```

This is a spatially aligned **14-band multipage TIFF** intended for subsequent multispectral analysis.

Intermediate split lens images and alignment information are also retained so that the processing result can be inspected when required.

Where enabled, the pipeline also produces an alignment quality report containing information about the registration method and estimated displacement for each lens pair.

---

## Spectral configuration

The current reference configuration produces the following 14 spectral bands:

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

The band-to-lens mapping is defined in the source code and can be adapted for other compatible multispectral configurations.

> **Important:** The current implementation assumes a six-lens compound image arranged in a 3 × 2 layout and a predefined 14-band spectral configuration. Other camera layouts or wavelength configurations may require modification of the splitting and band-mapping settings.

---

## Alignment strategy

The software uses a multi-stage registration strategy rather than relying on a single alignment method.

The normal processing route first attempts the primary image-registration method. If a reliable solution cannot be obtained, alternative registration approaches are automatically evaluated.

At a high level:

```text
Primary registration
        ↓
If unsuccessful
        ↓
Patch-based fallback
        ↓
If required
        ↓
Feature-based fallback
```

This approach is intended to improve robustness when working with imagery containing variations in texture, illumination, scene content or inter-lens displacement.

The current registration process estimates translational displacement between lens images. It should therefore not be interpreted as dense optical-flow or deformable pixel-by-pixel registration.

More information about the alignment algorithms is available in:

```text
docs/TECHNICAL_NOTES.md
```

---

## Why alignment is necessary

Multi-lens multispectral cameras acquire different wavelength bands through physically separated optical paths.

As a result, the same object or feature may not appear at exactly the same image coordinates in every lens image.

This becomes particularly important in close-range imaging, where relatively small differences in camera viewpoint can produce noticeable spatial displacement between spectral bands.

If these images are stacked without registration, a single physical feature may occupy different pixels in different spectral bands.

Automated alignment reduces this spatial mismatch before the spectral bands are combined into the final multispectral image.

---

## Intended applications

The pipeline was developed primarily for close-range multispectral imaging, where inter-lens spatial differences can be more pronounced than in conventional distant or aerial acquisition.

Potential applications include:

- underwater and marine imaging;
- benthic habitat and biological imaging;
- laboratory multispectral imaging;
- close-range environmental monitoring;
- object and material analysis;
- vegetation and biological studies;
- computer-vision and machine-learning dataset preparation;
- other multi-lens multispectral imaging applications.

The software is not restricted to a particular analysis package. The resulting multiband TIFF can be imported into compatible multispectral, remote-sensing or image-analysis software.

---

## Running from Python

Users who wish to inspect, modify or develop the software can run the Python source directly.

### Requirements

- Python 3.13
- NumPy
- OpenCV
- tifffile

Clone or download the repository and open a terminal in the project directory.

Create a virtual environment:

```powershell
python -m venv .venv
```

Activate it on Windows:

```powershell
.venv\Scripts\activate
```

Install the required packages:

```powershell
python -m pip install -r requirements.txt
```

Run the application:

```powershell
python src\run_pipeline_gui.py
```

A file-selection window will open.

---

## Repository structure

```text
Automated-Multispectral-Alignment/
│
├── .github/
│   └── workflows/
│       └── windows-build.yml
│
├── build/
│   ├── Automated_Multispectral_Alignment.spec
│   └── build_windows.bat
│
├── docs/
│   └── TECHNICAL_NOTES.md
│
├── examples/
│   └── README.md
│
├── src/
│   ├── alignment_core.py
│   ├── alignment_plan_b.py
│   ├── run_pipeline_gui.py
│   ├── split_core.py
│   └── stack_core.py
│
├── .gitignore
├── CHANGELOG.md
├── LICENSE
├── README.md
├── requirements-build.txt
└── requirements.txt
```

---

## Main software components

The processing pipeline is separated into several modules:

**`split_core.py`**  
Separates the original compound multispectral image into the six individual lens images.

**`alignment_core.py`**  
Contains the primary image-registration routines.

**`alignment_plan_b.py`**  
Provides alternative registration methods when the primary alignment cannot produce a sufficiently reliable result.

**`stack_core.py`**  
Coordinates registration, applies the calculated translations, determines the common valid region and constructs the final multiband TIFF.

**`run_pipeline_gui.py`**  
Provides the desktop file-selection interface and sequential processing workflow.

---

## Alignment quality

Automatic image registration cannot guarantee a correct result for every possible scene.

Image content with very low texture, strong repetitive patterns, severe illumination differences or insufficient common scene information can reduce registration reliability.

For this reason, alignment information should be reviewed when processing scientifically important datasets.

The generated quality information is intended to assist with identifying results that may require visual inspection. It should not be interpreted as an absolute measurement of geometric registration accuracy.

---

## Building the Windows executable

A Windows executable can also be built locally.

From the repository root, run:

```text
build\build_windows.bat
```

Alternatively, GitHub Actions automatically provides a reproducible Windows build using:

```text
.github/workflows/windows-build.yml
```

The build process verifies that the installed OpenCV version provides the feature-detection functionality required by the fallback alignment method before creating the executable.

---

## Current status

The project is under active research development.

The current version provides:

- automated six-lens image separation;
- automated inter-lens registration;
- multi-stage alignment fallback;
- common-region calculation;
- configured 14-band extraction and ordering;
- multiband TIFF generation;
- sequential multi-image processing;
- alignment quality reporting;
- Windows executable build support.

Future versions may extend camera configuration options, alignment assessment and user-interface functionality.

---

## Citation and research use

If this software contributes to published research, please acknowledge the repository and the corresponding software version used in the analysis.

A formal software citation file and DOI may be added in a future release.

---

## License

This project is distributed under the terms provided in the repository's `LICENSE` file.

---

## Disclaimer

This software is provided for research and image-processing purposes.

Users are responsible for validating registration quality and confirming that the generated multispectral products are appropriate for their scientific or analytical application.
