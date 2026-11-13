# Technical Notes

This document describes the registration logic in detail. Module names refer to `src/msalign/`. Examples use the reference 3 × 2 camera (lenses `1 2 3` on top, `4 5 6` below); everything generalises to any rectangular lens array (see [Lens array geometry](#lens-array-geometry)).

## Processing sequence

The first pass processes selected source images sequentially:

1. Create the image output directory.
2. Split the compound image into lens images (`split.py`).
3. Attempt registration (`stack.py`), including the closure check.
4. Apply accepted lens translations.
5. Determine the common valid image region.
6. Extract and order the configured bands.
7. Write the multipage TIFF.
8. Write TXT and CSV quality reports, and a row in the run summary.
9. Continue with the next selected image.

If an image cannot be registered reliably, its split images are retained and the image is queued for the second pass. The second pass reuses these split images instead of splitting the source again.

## Normal registration strategy

### Primary registration

`alignment.py` aligns every lens directly to the primary reference lens (lens 1 by default). It combines phase-correlation-based coarse registration with local normalized cross-correlation measurements and anchored consensus checks.

### Plan B — directional patch consensus

If any primary pair fails, the whole image switches to the **fallback chain**: short steps between adjacent lenses through the fallback reference lens (`2→5, 2→1, 2→3, 5→4, 5→6` for 3 × 2). Each step first tries `directional_patch_consensus` (`fallback.py`), which searches multiple textured image regions within a geometric window: along the baseline between the two lenses the shift is searched between `parallax_range_px[0]` and `[1]` per lens step, across the baseline within ± `parallax_cross_px`. The lower bound keeps the search away from fixed-pattern matches at zero shift.

The patch search uses OpenCV normalized template matching rather than a Python per-pixel search loop. Accepted patch translations are fused using a robust median. This reduces the effect of a single high-correlation but geometrically misleading repetitive region.

### Plan C — feature translation

If Plan B is rejected, `feature_ransac_translation` uses AKAZE keypoints and descriptor matches to estimate translation. Its original residual threshold remains strict; the history mechanism does not simply increase this threshold.

Normal order:

```text
Primary → Plan B → Plan C
```

## Sequence-aware historical geometry

The camera is a fixed multi-lens system. Although scene content changes between photographs, the physical relationship between lens views should normally remain similar. The pipeline therefore learns pair-specific displacement history from successfully processed images.

History is maintained independently for each step of the fallback chain (`2→1, 2→3, 2→5, 5→4, 5→6` for 3 × 2).

A successful primary alignment can also contribute to this history. Pair shifts are derived from the global applied translations, so the history is not restricted to images that previously used fallback mode.

### Robust prior

Up to five selected successful alignments are used to form each prior. The expected `dx` and `dy` are calculated with the median rather than the arithmetic mean.

Median absolute deviation (MAD) is used to identify obvious historical outliers before the final prior is calculated. This makes one anomalous successful frame less likely to move the expected geometry for later images.

## Prior-guided retry

Historical geometry is used only after the normal fallback methods reject a pair.

For a failed pair:

1. Take the robust historical raw shift for that physical lens pair.
2. Apply the inverse of that shift to the moving image as a temporary pre-shift.
3. The two images should now be approximately aligned.
4. Re-run the primary estimator on the residual.
5. If rejected, re-run Plan B in a narrow residual search window.
6. If rejected, re-run Plan C.
7. Add an accepted residual correction back to the historical prior.

Conceptually:

```text
final shift = historical prior + measured residual correction
```

A residual is bounded so that a historical retry cannot wander far away from the geometry that triggered the retry.

## Cross-method consensus during a historical retry

Sometimes no individual residual estimator crosses its normal acceptance threshold even though several methods independently locate almost the same residual displacement.

A conservative cross-method consensus is permitted only when:

- at least three successful historical samples support the prior;
- the historical MAD is small;
- at least two independently qualified residual estimators agree within a small pixel tolerance;
- the resulting residual remains close to zero.

This is intentionally different from simply increasing Plan C's residual threshold. The pair is accepted because **historical geometry and independent current-image estimators agree**, not because an individual quality limit was relaxed.

## First-pass and second-pass history

During the first pass, a difficult image can use up to the previous five successful images as history.

Images that still fail are retried after the first pass. For this second pass, the pipeline chooses up to five nearest successful images by acquisition order. This allows both earlier and later successful photographs to support an image that originally failed.

If no successful neighbours exist, or if the historical retry is still unsupported, the image remains failed.

## Lens array geometry

`layout.py` describes any `grid_cols × grid_rows` array; lenses are numbered row by row from 1.

- **Primary reference:** `primary_reference_lens` (default 1).
- **Fallback chain:** a spanning tree through `fallback_reference_lens` (default: the grid centre, lens 2 for 3 × 2) — a vertical spine through the reference column and horizontal branches outwards. Every step joins two adjacent lenses, so each estimated shift is short and points along a known baseline; parents are always aligned before their children.
- **Search windows** follow from the grid direction of each pair (see Plan B above), replacing the earlier hand-written per-pair windows for the 3 × 2 camera.
- **Closure pairs** are all adjacent pairs that a given alignment did not measure directly.

## Closure check

`closure.py` tests a finished alignment independently of the estimator that produced it. For each pair it runs the bounded local match from `matching.py`: the centre of one lens (gradient magnitude, half resolution) is searched for in the other lens only within `closure_tolerance_px` + 4 px of the position implied by the alignment, using normalised cross-correlation with parabolic sub-pixel peak refinement.

Checked pairs:

- **closure pairs** — adjacent pairs not measured directly (1–4 and 3–6 for the fallback chain; 2–3, 2–5, 3–6, 4–5, 5–6 for the primary star). An error in any link of the chain shows up as a residual here.
- **measured pairs** — re-checked with the same metric.

A pair **fails** when

- the correlation peak lies on the edge of the search window (the true shift is outside it),
- the residual exceeds `closure_tolerance_px` (default 4 px), or
- its correlation at the implied shift is below `closure_relative_ncc` (default 0.4) × the median correlation of the measured pairs of the same image.

The last criterion compares within one image, so spectrally very different bands are not penalised by an absolute threshold, while a shift that the image does not support at all (correlation near noise level while the other pairs correlate strongly) is caught. In testing, this rejected a primary result that had accepted a wrong lens pair on phase response alone (support 1), after which the fallback chain found the correct shift.

If even the measured pairs correlate below `closure_min_ncc` (default 0.10), the image lacks shared structure and the check is **inconclusive**; it rejects nothing.

With `closure_check: reject` (default) a failed check after the primary stage switches to the fallback chain, and after the fallback chain makes the image fail its first pass (it then gets the history-guided second pass). With `warn`, failures are only reported and the overall quality is set to *low*. Batch consensus images that fail the check get the full alignment.

## Batch consensus mode

For selections of `batch_threshold` (default 50) or more images, the pipeline assumes the images were taken with the same camera at roughly the same distance. The lens-to-lens displacement is then fixed physical geometry plus a distance-dependent parallax term that is nearly constant across the batch.

1. **Calibration.** `batch_calibration_samples` (default 16) images, evenly spaced over the whole batch (first and last included), are aligned with the full per-image method described above.
2. **Consensus.** All shifts are expressed relative to the primary reference lens (so primary and fallback results are comparable). The per-lens median is computed, and a calibration image *agrees* if every lens lies within `batch_consensus_tolerance_px` (default 3 px) of it. The consensus is the median of the agreeing images. It is used only if at least `batch_min_successful_samples` images succeeded and at least `batch_min_agreement` (default 75 %) of them agree. A wrongly accepted single result is therefore excluded rather than averaged in. If the batch does not agree — typically because it mixes distances — every remaining image is aligned individually, exactly as in normal mode.
3. **Per-image verification and refinement.** For every other image, the gradient-magnitude centre of the primary reference lens is searched for in each lens image (the bounded match described under [Closure check](#closure-check)) by normalised cross-correlation, but only within `batch_verify_max_residual_px` + 4 px of the consensus position (at half resolution, with parabolic sub-pixel peak refinement). The bounded search cannot lock onto an unrelated match elsewhere in the image, and a peak on the edge of the window means the true shift lies outside it.
   - The image is accepted when every lens has its peak inside the window, a residual ≤ `batch_verify_max_residual_px` (default 5 px) and a correlation of at least `batch_verify_min_response_ratio` × the median correlation measured on the calibration images at their own known-good shifts. This threshold is learned from the data, so it adapts to how similar the spectral bands of each lens pair are.
   - The final shift is the consensus shift plus the measured residual (rounded). The consensus is the anchor; the residual follows small frame-to-frame differences.
   - The closure check then runs on the refined shifts.
   - If the verification or the closure check fails, the image gets the full per-image alignment, with the consensus calibration images as history priors. It is never forced onto the consensus.

Quality reports of consensus images record the consensus, the residual and the number of agreeing calibration images. Their quality is `high` for a residual ≤ 1.5 px and `medium` otherwise.

The cost per consensus image is dominated by reading the file and writing the TIFF; the alignment search itself is skipped. Split lens images are not written for consensus images unless `batch_keep_split_images` is enabled.

## Output window and metadata

**Pixel grid.** All lens shifts are expressed relative to the primary reference lens, so the output pixel grid is that lens's grid whichever alignment mode was used. Lens *k* covers `[dx_k, W + dx_k) × [dy_k, H + dy_k)` of that grid; the shared area is the intersection over all lenses.

**Cutting.** Each band is taken directly from its lens image at `lens(x - dx, y - dy)`. Shifts are whole pixels, so this is an exact copy without interpolation, and it avoids building a shifted copy of every lens image.

**Window.** With `output_size_px` set, a window of that size is placed either at the centre of the shared area (`common_area_center`) or at a fixed position in the reference lens (`fixed`). The window is common to all bands of the image: it decides *which part of the scene* is kept, not how the bands line up. Its origin is recorded as `origin_in_reference_lens_px` and `origin_in_source_px` (reference-lens origin from the split step plus window origin, times the lens scale if cells had to be resized). The split step stores the lens origins in `split_lens_images/split_info.json`, so a second-pass retry reports the same coordinates. If the window is not fully inside the shared area by more than `output_max_padding_px`, the image fails.

For the reference camera, the lens baseline (≈ 11.9 mm, one cell of 3168 px × 3.76 µm) and the effective focal length of 21.8 mm give a parallax of roughly 140 px per lens step at the 0.5 m minimum focus distance and close to 0 px at infinity. The shared area is then at least about 2534 − 2 × 140 ≈ 2250 px wide, so the default 2048 × 2048 window fits over the whole focus range with some margin for lens mounting offsets.

**TIFF layout.** One image with `SamplesPerPixel` = number of bands, planar configuration *separate*, photometric *minisblack*. The earlier layout (one page per band) is available as `tiff_layout: multipage`; GDAL-based tools see only the first band of such a file.

**Tags.** `Make`, `Model`, `Software`, `DateTime` (acquisition time from EXIF), `ImageDescription` (the full metadata record as JSON), TIFF/EP `FocalLength` (37386) and `FocalPlaneXResolution` / `FocalPlaneYResolution` / `FocalPlaneResolutionUnit` (37390–37392, pixels per cm), `GDAL_METADATA` (42112) with dataset items (`CAMERA_MODEL`, `LENS`, `FOCAL_LENGTH_MM`, `PIXEL_SIZE_UM`, …) and per-band items (`DESCRIPTION` with role *description*, `CENTRAL_WAVELENGTH_UM` and `FWHM_UM` in the `IMAGERY` domain, `wavelength`, `fwhm`, `LENS`, `SENSOR_CHANNEL`), and `GDAL_NODATA` (42113) only when padding occurred.

**EXIF.** Read with tifffile from TIFF-based files (TIFF, ARW, DNG, NEF, CR2, PEF, SRW) and from the APP1 block of JPEGs. Formats with another container (CR3, RAF, ORF, RW2) are processed normally but give no EXIF; configure the camera fields for them.

## Geometric model

The global stage estimates one translation (`dx`, `dy`) per lens, which is exact for a flat scene. For scenes with depth, an optional second stage (`parallax.py`) adds a depth-dependent part.

### Local residual check

After the global alignment, each lens is compared with the mean of all other lenses (gradient magnitude, half resolution) on a grid of about 512 px tiles. Phase correlation per tile gives the remaining shift; the report states the median and the 90th percentile per lens. Tile sizes are kept even: `cv2.phaseCorrelate` reports a spurious (0.5, 0.5) px shift for odd sizes.

### Depth parallax correction

The lenses lie on a regular grid with equal spacing, so a scene point at depth *Z* is displaced between lenses in proportion to their grid offset and to 1/*Z*. After the global alignment (which fits one depth), the remaining displacement of lens *k* at pixel *p* is

`r_k(p) = δ(p) · (Δcol_k, Δrow_k) + c_k`

with one disparity map δ shared by all lenses and a small constant c_k per lens. This was checked on DSC05928 before implementing it: at the same tile, lens 5 (one column from lens 4) showed (−15.6, 2.0) px, lens 2 (one column, one row) (−18.5, 12.8) px and lens 3 (two columns, one row) (−33.0, 11.7) px — one value δ ≈ 15 px per lens step explains all three.

Estimation (multi-baseline stereo; Okutomi & Kanade, 1993):

1. Gradient-magnitude images of each lens (mean of its used channels) at 1/4 resolution.
2. Plane sweep over δ in ± `parallax_search_px` (step 2 px full resolution): every lens is shifted by δ·(Δcol, Δrow), and the local NCC (≈ 36 px window) is averaged over adjacent lens pairs. Gradients make the cost usable across spectral bands; short baselines limit occlusion.
3. Best δ per pixel with parabolic sub-step refinement; pixels with weak or edge-of-range peaks get no weight. Confidence-weighted Gaussian smoothing and a median filter fill and regularise the map.
4. c_k from the median tile residual after step 3, then a second sweep with c_k.
5. Bands are resampled from the lens images (bilinear, no overshoot) with `lens(p + origin − s_k + δ(p)·(Δcol_k, Δrow_k) + c_k)`.

The correction is applied only if the worst lens's 90th-percentile residual drops by more than 0.5 px and the median does not increase. Otherwise the global alignment is kept and the report says so.

Validation on DSC05928 (2350 × 2451 px output, all six lenses): median residual 3.6 → 1.3 px, worst lens 90th percentile 25.7 → 4.8 px; an independent check against the 550 nm band agreed (90th percentile 2.1–4.4 px after). Runtime about 5 s per image. Limits: areas occluded in some lenses cannot be aligned; very low-texture areas take the disparity of their surroundings.

Related approaches: Tommaselli et al. (ISPRS Archives XLVIII-G-2025) register close-range multi-lens images pixel-wise via a DSM and orthoimages per band, reaching about 1.5 px, but require a rigorous camera calibration. MicaSense's band alignment uses one homography per band and notes it fails when depth parallax is significant.

## Photogrammetry export

The aligned TIFF suits band analysis (indices, classification). For 3D reconstruction it is the wrong input: each band has been shifted — and with `parallax_correction` warped — to match the others, so the stack is no longer a central projection of one camera, and the crop origin can vary per image. Structure-from-motion software needs geometrically untouched images.

`metashape_export` therefore writes, for every capture, each band's *unaligned* lens image (the same centred lens crop for every capture, so each lens keeps a constant principal point) into `metashape/<NN>_<wavelength>/<capture>.tif`. Agisoft's documented multi-camera loading uses exactly this layout: one folder per band, equal file counts, added with Add Folder as a multi-camera system. Each lens band then becomes a sensor of a fixed rig; the bundle adjustment solves the relative lens poses once and models depth parallax through the 3D geometry, which per-image 2D alignment cannot do.

Metadata per file: Make/Model (EXIF of the source), TIFF/EP FocalLength and FocalPlaneX/YResolution (pixel size), and an XMP packet with the `Camera` namespace tags BandName, CentralWavelength, WavelengthFWHM, RigName, RigCameraIndex and a CaptureUUID shared by the bands of one capture (namespace and formats as specified by Pix4D for rig cameras). Whether a given program reads the focal length and pixel size from these tags varies; the folder's README lists the values to enter in Metashape's Camera Calibration if needed, and recommends a bright band (nearest 550 nm) as master.

## RAW decoding

RAW files are decoded with LibRaw (rawpy) with white balance off, no colour matrix, linear response, no auto-brightening and 16-bit output. Two further choices:

- **Demosaicing:** `raw_demosaic: linear` (default) interpolates each sensor channel only from neighbours of the same colour. Correlation-based methods (AHD, LibRaw's default and the setting before 1.2) use the other channels to interpolate; for narrow-band data this carries noise and structure between spectral bands (Eskelinen & Hämäläinen, WHISPERS 2018: higher absolute error than bilinear on hyperspectral data).
- **White level:** `adjust_maximum_thr = 0`. LibRaw otherwise lowers the white level per image from its content, which changes the digital scaling between images of a series.

The decoding parameters are recorded in the metadata (`raw_decoding`).

## Performance

Measured on a 9504 × 6336 px synthetic compound image (one CPU core, without RAW decoding):

| Stage | Before 1.2 | 1.2 |
|---|---|---|
| Primary alignment (5 pairs) | ~59 s | ~2 s |
| Whole image | ~60 s | ~2.5 s |

### Parallel processing

With `workers` > 1 (default `auto`: CPU cores − 1, at most 8, limited to about 1.5 GB of free memory per RAW image and 0.6 GB per JPG/TIFF), images are processed in separate processes (spawn start method, also used by the Windows executable via `multiprocessing.freeze_support()`). Two 61 MP synthetic images per pair of cores: 3.8 s → 2.2 s per image with two workers on a two-core machine.

- **Same results, independent of timing:** in the first pass, images get no history priors, so the result of an image never depends on which other image happened to finish first. History-guided retries run in the second pass, after the first pass is complete. With `workers: 1` everything runs in order as before, including history priors from the previous images.
- **Logs:** each worker's log is returned with its result and written to the run log, prefixed with the image name.
- **Robustness:** if a worker process dies (for example when memory runs out), the images still in progress or waiting are processed one after the other in the main process; the run continues.
- **Cancel:** no new images are started; images already running finish.
- The executable's `--self-test` starts a worker process, so a build in which parallel processing cannot start is never published.

The primary method's local search evaluated its NCC with one NumPy call per candidate offset (≈ 230 000 calls per image). It now uses one `cv2.matchTemplate` (`TM_CCOEFF_NORMED`, the same zero-mean NCC) per patch; a test checks that both give identical shifts. Intensity percentiles for normalisation are taken from a regular pixel subsample.

## Band configuration (reference camera)

Set with `file_bands` and `output_order` in the settings file. Each lens of the Agrowing Sextuple has a multi-bandpass filter; every band reaches the sensor channel that is most sensitive to it.

| Lens image | R channel | G channel | B channel |
|---|---|---|---|
| 1 | 850 | – | – |
| 2 | 630 | 525 | – |
| 3 | 710 | 570 | 405 |
| 4 | 650 | 550 | 430 |
| 5 | 735 | – | 490 |
| 6 | 685 | 560 | 450 |

Final order:

`405, 430, 450, 490, 525, 550, 560, 570, 630, 650, 685, 710, 735, 850 nm`

**How the table was verified.** Versions before 1.1 used a table in which every lens had its R and B labels exchanged and lenses 5 and 6 were exchanged. This was found by comparing an msalign output with Agrowing's own software for the same RAW image (DSC05928):

- *Sensor physics.* Bayer red pixels hardly respond at 430–490 nm and blue pixels hardly respond at 650–735 nm. In the old output, blue channels labelled 650/685/735 nm were bright and red channels labelled 430/450/490 nm were dark. That is only possible if the labels are reversed.
- *Content match.* After whole-pixel registration, each band of the old output matched the Agrowing band predicted by the corrected table (phase-correlation responses 0.31–0.85, compared with < 0.1 for the other bands). Lens 1 (850 nm, single filter) could not be cross-checked this way; its band is read from the red channel.
- *Two-band lens.* The G channel of lens 5 was almost fully explained by its own R and B channels (R² 0.98, compared with 0.95 for three-band lenses), as expected for the lens without a green band.

The regression test `test_reference_band_table_follows_sensor_physics` keeps the rule *R = longest, B = shortest band of each lens*.

## Quality reports

The quality report records pair-level method, algorithm, raw and applied translation, support and confidence/score values.

History-guided pairs additionally report the number of prior samples and the measured residual correction. Confidence values from different algorithms are evidence scores from different procedures and should not be interpreted as directly comparable statistical probabilities.

Each report also lists every pair of the closure check (kind, status, implied shift, residual, correlation) and the source image size and the split mode. `proportional` means the source size differed from the expected grid (for example a RAW decoder adding border pixels); cells are then cut from the centre at native resolution. `proportional_resized` means the cells were smaller than expected and had to be resized — check such images.

For scientific use, inspect low/medium-quality results and a representative subset of high-quality results visually before downstream quantitative analysis.
