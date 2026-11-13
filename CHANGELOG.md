# Changelog

## Unreleased

### Added

- Desktop app: check boxes for depth parallax correction, the Metashape export and the ENVI export, with an option to remember them in `pipeline_config.json`.

- Photogrammetry export (`metashape_export`): unaligned single-band lens images, one folder per band, in the multi-camera layout Agisoft Metashape loads with Add Folder; focal length, pixel size and XMP Camera band/rig tags in every file; README and a Metashape Pro loading script in the folder.
- ENVI export (`envi_export`): band-sequential `.img` + `.hdr` with band names, wavelengths and FWHM.

- Parallel processing (`workers`, default `auto`): images are processed in separate processes, sized to CPU cores and free memory. First-pass results do not depend on processing order; if a worker process dies, the run continues one image at a time. 1.75x faster with two workers on two cores.

### Fixed

- Windows executable failed at start with "No module named 'msalign'" (since 1.0); the build now runs the executable's self-test.

## 1.2.0

### Added

- Local residual check for every image: remaining misalignment per lens on image tiles, in the quality report and metadata, with a hint when depth parallax is large.
- Optional depth parallax correction (`parallax_correction`): one disparity map shared by all lenses (multi-baseline stereo on gradient images), a constant sub-pixel correction per lens, applied only if it measurably improves alignment; disparity map saved as `_parallax.tif`. On a close-range Agrowing image: worst lens 90th-percentile residual 25.7 -> 4.8 px, median 3.6 -> 1.3 px.

### Changed

- RAW files are demosaiced bilinearly from same-colour neighbours (`raw_demosaic: linear`) instead of LibRaw's correlation-based AHD, which mixes information between spectral bands; `ahd` remains available.
- LibRaw's per-image white-level adjustment is disabled, so all images of a series get the same scaling.

### Performance

- Primary alignment about 25x faster (one image: ~60 s -> ~2.5 s without RAW decoding): the local NCC search uses one `cv2.matchTemplate` per patch instead of one NumPy call per candidate offset, with identical results.

## 1.1.0

### Added

- Camera, band and processing metadata in every TIFF: make / model from the source EXIF; lens, effective focal length, pixel size and band width from the `camera` settings (defaults: Agrowing Sextuple, 21.8 mm, 3.76 µm, 30 nm); band names, centre wavelengths and FWHM as GDAL band metadata; alignment mode, lens shifts and crop origin as JSON in ImageDescription and in a `_metadata.json` sidecar.
- Fixed output size for every image (`output_size_px`, default 2048 x 2048 for the reference camera), placed at the centre of the shared lens area or at a fixed position (`output_anchor`). The crop origin is recorded in reference-lens and source-image pixels in the metadata, quality report and run summary.
- Optional lossless deflate compression (`tiff_compression`).

### Fixed

- **Band assignment of the reference camera.** The band table used up to 1.0 had the R and B labels of every lens exchanged and lenses 5 and 6 exchanged, so 10 of the 14 bands carried another band's data and two (630, 560 nm) came from the wrong sensor channel. Verified against Agrowing's own output and Bayer sensor physics (see Technical Notes). Outputs made with earlier versions should be reprocessed.
- `file_bands` can name the sensor channel explicitly (`{"R": .., "G": .., "B": ..}`); lists are still read as [R, G, B].

### Changed

- The TIFF is one multiband image instead of one page per band, so GDAL / QGIS / rasterio / ENVI read all bands with their wavelengths. `tiff_layout: multipage` restores the previous layout.
- Bands are cut directly from the lens images (exact for whole-pixel shifts) instead of shifting full copies of every lens image.

## 1.0.0 — first public release

### Added

- Closure check: every shift of a finished alignment is tested independently on the image (bounded local NCC) - adjacent pairs not measured directly and the measured pairs themselves. `closure_check`: reject (default), warn or off. Results in the quality report, CSV and run summary.
- Any rectangular lens array (`grid_cols` x `grid_rows`): fallback chain, search windows, history and closure pairs are derived from the grid instead of being written for the 3 x 2 camera.
- Fallback search windows follow the lens baseline direction with a configurable `parallax_range_px`; the previous fixed windows started at 80 px and could miss shifts at longer working distances.
- Command line interface `msalign` (files and folders, `--config`, `--set`, `--batch` / `--no-batch`, `--list-settings`).
- Installable Python package (`pip install`), with `msalign` and `msalign-gui` commands.
- Run log and per-image summary CSV in `msalign_runs/`.
- CONTRIBUTING.md, CITATION.cff.

### Changed

- Source reorganised into the `msalign` package (`src/msalign/`); diagnostics use `logging` instead of `print`.
- Alignment modes are named `primary_direct_<ref>_reference` and `fallback_chain_<ref>_reference`.

## Earlier changes (pre-1.0)

### Added

- Batch consensus mode for 50+ images: a calibration sample learns the common lens geometry, which is verified (bounded local NCC search) and refined on every other image; unconfirmed images get the full alignment.
- "Select a whole folder" option; RAW is preferred when a folder holds RAW and JPG of the same shot.
- `pipeline_config.json` settings file (camera layout, band mapping, batch and output options) with validation.
- Automated tests with synthetic images and a GitHub Actions test workflow (Windows and Linux).

- Camera RAW input (`.arw`, `.raw` and other LibRaw formats) via rawpy, decoded linearly without white balance or colour matrix; RAW inputs keep 16-bit data through 16-bit TIFF split images and a 16-bit final TIFF.
- File dialog filters for all supported, RAW-only and standard image types.

- Graphical Windows processing window with progress, current stage and estimated remaining time.
- Expandable live details table for per-image success, retry and error states.
- Pair-specific alignment history learned from successful images.
- Robust median/MAD historical priors using up to five successful neighbouring frames.
- History-guided pre-shift followed by a fresh Primary → Plan B → Plan C residual retry.
- Second-pass retry for unresolved images using nearest successful images on either side of the sequence.
- History/prior information in alignment quality reports.

### Improved

- Images are processed in natural file-name order, so neighbour-based history uses true neighbours.
- Shared `tiff/` collection uses hard links by default (no extra disk space).
- Sources slightly larger than the expected grid are cut at native resolution instead of being resampled; the split mode is recorded in the quality report.
- Valid-area mask after shifting is computed explicitly (the previous `>= 0` test was always true).
- AKAZE is found on OpenCV 5 builds as well.
- Processing logic moved from the GUI into `pipeline_core.py`.

- Non-ASCII file paths (e.g. Turkish characters) are now read and written correctly on Windows.
- AKAZE fallback scales 16-bit images to 8-bit instead of truncating them.

### Fixed

- The Windows executable is built from `src/run_pipeline_gui.py`; the newer GUI (cancel button, safe TIFF collection copy) that had been placed at the repository root was moved there so it is actually included in the build.

- Directional patch search now uses optimized OpenCV normalized template matching.
- Patch-consensus displacement uses a robust median to reduce influence from isolated high-NCC outliers.
- Plan B and Plan C failures are both reported instead of hiding the Plan C diagnostic behind the Plan B result.
- Windows PyInstaller build now runs in windowed mode without a separate command-prompt window.

### Existing pipeline

- Six-lens compound-image splitting.
- Multi-stage translation registration.
- 14-band wavelength-ordered TIFF export.
- Per-image alignment quality TXT and CSV reports.
- GitHub Actions Windows executable build workflow.
