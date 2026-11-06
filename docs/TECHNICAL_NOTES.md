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

The exported product currently uses global translational displacement (`dx`, `dy`) for each lens image. The pipeline does not apply dense optical flow, local mesh deformation or non-rigid warping.

This keeps the transformation interpretable and avoids inventing local geometric corrections without strong evidence. Close-range scenes with substantial depth variation can still contain local parallax after a global translation.

## Band configuration (reference camera)

Set with `file_bands` and `output_order` in the settings file.

| Lens image | Bands (nm) |
|---|---|
| 1 | 850 |
| 2 | 525, 630 |
| 3 | 405, 570, 710 |
| 4 | 430, 550, 650 |
| 5 | 450, 560, 685 |
| 6 | 490, 735 |

Final order:

`405, 430, 450, 490, 525, 550, 560, 570, 630, 650, 685, 710, 735, 850 nm`

## Quality reports

The quality report records pair-level method, algorithm, raw and applied translation, support and confidence/score values.

History-guided pairs additionally report the number of prior samples and the measured residual correction. Confidence values from different algorithms are evidence scores from different procedures and should not be interpreted as directly comparable statistical probabilities.

Each report also lists every pair of the closure check (kind, status, implied shift, residual, correlation) and the source image size and the split mode. `proportional` means the source size differed from the expected grid (for example a RAW decoder adding border pixels); cells are then cut from the centre at native resolution. `proportional_resized` means the cells were smaller than expected and had to be resized — check such images.

For scientific use, inspect low/medium-quality results and a representative subset of high-quality results visually before downstream quantitative analysis.
