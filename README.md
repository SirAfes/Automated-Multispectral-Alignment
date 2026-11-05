# Automated Multispectral Alignment

[![Tests](https://github.com/SirAfes/Automated-Multispectral-Alignment/actions/workflows/tests.yml/badge.svg)](https://github.com/SirAfes/Automated-Multispectral-Alignment/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**msalign** registers the lens images of **multi-lens (snapshot) multispectral cameras** and exports a spatially aligned multiband TIFF — automatically, without manual control points.

A multi-lens camera records one compound photograph in which every lens, each behind its own filter, sees the scene from a slightly different position. Before the bands can be used together, the lens views must be split and brought onto one pixel grid. msalign does this for single photographs and for series of thousands, and it says how much each result can be trusted.

```text
Compound image (JPG / TIFF / camera RAW)
        ↓  split into lens images
        ↓  register lenses to each other (multi-stage, with fallbacks)
        ↓  closure check: independent test of every shift
        ↓  crop to the area all lenses share
Aligned multiband TIFF  +  quality report (TXT / CSV)
```

The reference configuration is a 3 × 2 six-lens camera with 14 bands from 405 to 850 nm. Any rectangular lens array (2 × 2, 4 × 4, 1 × 5, …) and any band mapping can be set up in a [settings file](#configuration).

## Contents

- [Installation](#installation)
- [Usage](#usage)
- [Outputs](#outputs)
- [Configuration](#configuration)
- [How it works](#how-it-works)
- [Validation and responsible use](#validation-and-responsible-use)
- [Development](#development)
- [Citation](#citation) · [License](#license)

## Installation

### Windows — no Python required

1. Open the **Actions** tab of this repository and choose **Build Windows executable**.
2. Open the latest successful run and download **Automated-Multispectral-Alignment-Windows** under *Artifacts*.
3. Extract the ZIP and run `Automated_Multispectral_Alignment.exe`.

> Windows may warn about an unsigned executable downloaded from the internet. The artifact is built by GitHub Actions directly from the source in this repository.

### Python (Windows, macOS, Linux)

Python 3.10 or newer:

```bash
python -m pip install "git+https://github.com/SirAfes/Automated-Multispectral-Alignment.git"
```

This installs two commands: `msalign` (command line) and `msalign-gui` (desktop app).

## Usage

### Desktop app

Start the `.exe` or `msalign-gui`. Choose **Select images…** or **Select a whole folder…**; processing starts at once. The window shows progress, remaining time and a per-image status list.

When a folder holds both the RAW and the JPG of a shot, only the RAW is used. Subfolders (for example earlier outputs) are not scanned.

### Command line

```bash
msalign D:/survey/day1                         # every image in the folder
msalign IMG_0001.ARW IMG_0002.ARW              # selected files
msalign D:/survey/day1 --set closure_check=warn
msalign D:/survey/day1 --config my_camera.json
msalign --list-settings                        # show all settings and current values
```

`msalign --help` lists all options. The exit code is 0 when every image succeeded and 1 otherwise, so the command fits into scripts.

### Large series

From 50 images on, msalign uses **batch consensus mode** for series taken at the same distance: about 16 images spread over the series are aligned in full, their common lens geometry is learned, and every other image only *verifies and refines* that geometry on its own content. Frames that do not match are aligned in full as usual. In a synthetic test at 9504 × 6336 px, a full alignment took about 70 s and a verified consensus image about 1 s, with identical shifts. Details: [batch consensus mode](docs/TECHNICAL_NOTES.md#batch-consensus-mode).

## Outputs

For `D:/survey/DSC06664.ARW`:

```text
D:/survey/
├── DSC06664.ARW
├── DSC06664_ARW/
│   ├── split_lens_images/1.tif … 6.tif
│   ├── DSC06664_ARW_aligned_multiband.tif                    ← aligned bands
│   ├── DSC06664_ARW_aligned_multiband_alignment_quality.txt  ← quality report
│   └── DSC06664_ARW_aligned_multiband_alignment_quality.csv
├── tiff/
│   └── DSC06664_ARW_aligned_multiband.tif                    ← all TIFFs in one place
└── msalign_runs/
    ├── 20261105_141502.log                                   ← run log
    └── 20261105_141502_summary.csv                           ← one row per image
```

- **Multiband TIFF:** one page per band, in the configured wavelength order, each page tagged with its band name. 8-bit for JPG input, 16-bit for RAW input.
- **`tiff/` folder:** a hard link by default — the same file under a second name, using no extra disk space. Can be switched to a real copy or off.
- **Quality report:** for every lens pair the method, shift, support and confidence, plus the [closure check](#closure-check).
- **Run summary:** status, alignment mode, quality and closure result of every image of a run, written as the run progresses.

RAW outputs carry the RAW extension in their name (`DSC06664_ARW`), so the RAW and JPG of the same shot never overwrite each other. In batch consensus mode the split lens images are not kept unless `batch_keep_split_images` is enabled (about 200 MB per RAW image).

### Supported input formats

| Type | Extensions | Final TIFF |
|---|---|---|
| Standard | `.jpg` `.jpeg` `.png` `.tif` `.tiff` | 8-bit |
| Camera RAW | `.arw` `.raw` `.dng` `.nef` `.cr2` `.cr3` `.orf` `.rw2` `.raf` `.pef` `.srw` | 16-bit |

RAW files are decoded with [rawpy](https://github.com/letmaik/rawpy) / LibRaw as close to the sensor data as possible: no white balance, no camera colour matrix, linear response, no automatic brightening. Each sensor channel stays a separate spectral measurement. For quantitative work, prefer RAW: camera JPGs are 8-bit, lossy, and their colour processing mixes the channels.

## Configuration

All settings have defaults for the reference camera. To change them, copy [`pipeline_config.example.json`](pipeline_config.example.json) to `pipeline_config.json` next to the `.exe` (or pass `--config FILE`) and keep only what you change. Unknown or inconsistent settings are rejected with a clear message at start-up. Every setting is documented in [`src/msalign/settings.py`](src/msalign/settings.py).

| Setting | Default | Meaning |
|---|---|---|
| `grid_cols`, `grid_rows` | 3, 2 | Lens array; lenses are numbered row by row from 1 |
| `cell_size` | 3168 | Expected size of one lens cell in the compound image (px) |
| `center_crop_frac` | 0.80 | Centred part of each cell that is kept |
| `file_bands` | reference camera | Band label in the R, G, B channel of each lens (`"0"` = unused) |
| `output_order` | 405 … 850 | Band order in the output TIFF |
| `parallax_range_px` | [20, 360] | Expected shift between adjacent lenses along their baseline |
| `closure_check` | `reject` | `reject`, `warn` or `off` |
| `batch_threshold` | 50 | Number of images from which batch consensus mode is used |
| `collection_mode` | `hardlink` | Second copy in `tiff/`: `hardlink`, `copy` or `off` |

Example for a 2 × 2 camera with one band per lens:

```json
{
  "grid_cols": 2,
  "grid_rows": 2,
  "cell_size": 2000,
  "file_bands": {"1": ["450", "0", "0"], "2": ["0", "550", "0"],
                 "3": ["0", "0", "650"], "4": ["0", "0", "850"]},
  "output_order": ["450", "550", "650", "850"]
}
```

## How it works

Every lens is related to the others by a translation (`dx`, `dy`): fixed lens geometry plus a parallax that depends on the subject distance. msalign estimates these translations in stages and accepts a result only when the image supports it.

```text
Primary         every lens directly against lens 1
                (phase correlation + patch consensus)
   ↓ any pair rejected, or closure check failed
Fallback chain  short steps between adjacent lenses through the array centre
                (e.g. 2→1, 2→3, 2→5, 5→4, 5→6 for 3 × 2), each step:
                directional patch consensus → AKAZE features → history-guided retry
   ↓
Closure check   independent test of every shift on the image
   ↓ still failing
Second pass     retry with the lens geometry of the nearest successful images
   ↓ still failing
Error           reported for manual inspection — nothing is forced
```

The fallback only searches in the direction of each lens baseline (`parallax_range_px`), which keeps it away from fixed-pattern matches at zero shift.

### Closure check

The alignment measures only some lens pairs directly; all other relative shifts follow from them. The closure check looks at the image again with one independent metric — a small, bounded correlation search around each implied shift:

- **closure pairs** — adjacent lenses that were *not* measured directly (1–4 and 3–6 in the fallback chain): an error anywhere in the chain shows up here;
- **measured pairs** — re-checked with the same metric, so every shift is judged on the same scale.

A pair fails when the best local match is not at the implied shift, or when the implied shift has far weaker support than the other pairs of the same image. With `closure_check: reject` (default) a failed check sends the image to the next stage; with `warn` it is only reported and the quality is set to *low*. The report lists every checked pair with residual and correlation.

More detail: [Technical Notes](docs/TECHNICAL_NOTES.md).

## Validation and responsible use

- Inspect a representative subset visually before quantitative analysis, especially scenes with strong depth differences, moving subjects, little texture, occlusion or very different illumination between bands.
- The geometric model is a global translation per lens. Close-range scenes with large depth variation can keep local parallax after alignment; msalign does not apply non-rigid warping.
- The reported quality is an **alignment** quality indicator, not a statement about the scientific content of the bands.
- A failed alignment is intentionally preferred over silently accepting an unsupported one.

## Development

```bash
git clone https://github.com/SirAfes/Automated-Multispectral-Alignment.git
cd Automated-Multispectral-Alignment
python -m pip install -e ".[dev]"
python -m pytest
```

The tests build synthetic compound images with known lens shifts (JPG, RAW, other grid sizes, batch series with outliers) and run on every push on Windows and Linux.

```text
src/msalign/
├── gui.py, cli.py          desktop app and command line
├── pipeline.py             one image / a whole run, run files
├── batch.py                batch consensus mode
├── stack.py                alignment strategy, TIFF export, reports
├── alignment.py            primary estimator (phase correlation + patches)
├── fallback.py             directional patches, AKAZE, history-guided retry
├── layout.py               lens array geometry
├── closure.py, matching.py closure check, bounded local matching
├── split.py, image_io.py   splitting, reading JPG/TIFF/RAW
└── settings.py             settings and validation
build/                      PyInstaller build of the Windows executable
docs/                       technical notes
tests/                      automated tests
```

To build the Windows executable locally, run `build\build_windows.bat`.

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## Citation

If you use msalign in research, please cite it — see [CITATION.cff](CITATION.cff) (GitHub shows a *Cite this repository* button).

## License

[MIT](LICENSE)
