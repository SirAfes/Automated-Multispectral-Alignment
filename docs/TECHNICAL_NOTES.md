# Technical Notes

This document describes the registration logic in more detail. The main README is intentionally written for users who primarily work with imagery rather than software development.

## Processing sequence

The first pass processes selected source images sequentially:

1. Create the image output directory.
2. Split the 3 × 2 compound image into six lens images.
3. Attempt registration.
4. Apply accepted lens translations.
5. Determine the common valid image region.
6. Extract and order the 14 configured bands.
7. Write the multipage TIFF.
8. Write TXT and CSV quality reports.
9. Continue with the next selected image.

If an image cannot be registered reliably, its split images are retained and the image is queued for the second pass. The second pass reuses these split images instead of splitting the source again.

## Normal registration strategy

### Primary registration

`alignment_core.py` performs the first translation estimate. It combines phase-correlation-based coarse registration with local normalized cross-correlation measurements and anchored consensus checks.

### Plan B — directional patch consensus

If the primary route fails, `directional_patch_consensus` searches multiple textured image regions within pair-specific geometric windows.

The patch search uses OpenCV normalized template matching rather than a Python per-pixel search loop. Accepted patch translations are fused using a robust median. This reduces the effect of a single high-correlation but geometrically misleading repetitive region.

### Plan C — feature translation

If Plan B is rejected, `feature_ransac_translation` uses AKAZE keypoints and descriptor matches to estimate translation. Its original residual threshold remains strict; the history mechanism does not simply increase this threshold.

Normal order:

```text
Primary → Plan B → Plan C
```

## Sequence-aware historical geometry

The camera is a fixed multi-lens system. Although scene content changes between photographs, the physical relationship between lens views should normally remain similar. The pipeline therefore learns pair-specific displacement history from successfully processed images.

History is maintained independently for these fallback pairs:

```text
2 → 1
2 → 3
2 → 5
5 → 4
5 → 6
```

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

## Batch consensus mode

For selections of `batch_threshold` (default 50) or more images, the pipeline assumes the images were taken with the same camera at roughly the same distance. The lens-to-lens displacement is then fixed physical geometry plus a distance-dependent parallax term that is nearly constant across the batch.

1. **Calibration.** `batch_calibration_samples` (default 16) images, evenly spaced over the whole batch (first and last included), are aligned with the full per-image method described above.
2. **Consensus.** All shifts are expressed relative to lens 1 (so primary and Plan B results are comparable). The per-lens median is computed, and a calibration image *agrees* if every lens lies within `batch_consensus_tolerance_px` (default 3 px) of it. The consensus is the median of the agreeing images. It is used only if at least `batch_min_successful_samples` images succeeded and at least `batch_min_agreement` (default 75 %) of them agree. A wrongly accepted single result is therefore excluded rather than averaged in. If the batch does not agree — typically because it mixes distances — every remaining image is aligned individually, exactly as in normal mode.
3. **Per-image verification and refinement.** For every other image, the gradient-magnitude centre of lens 1 is searched for in each lens image by normalised cross-correlation, but only within `batch_verify_max_residual_px` + 4 px of the consensus position (at half resolution, with parabolic sub-pixel peak refinement). The bounded search cannot lock onto an unrelated match elsewhere in the image, and a peak on the edge of the window means the true shift lies outside it.
   - The image is accepted when every lens has its peak inside the window, a residual ≤ `batch_verify_max_residual_px` (default 5 px) and a correlation of at least `batch_verify_min_response_ratio` × the median correlation measured on the calibration images at their own known-good shifts. This threshold is learned from the data, so it adapts to how similar the spectral bands of each lens pair are.
   - The final shift is the consensus shift plus the measured residual (rounded). The consensus is the anchor; the residual follows small frame-to-frame differences.
   - If the check fails, the image gets the full per-image alignment, with the consensus calibration images as history priors. It is never forced onto the consensus.

Quality reports of consensus images record the consensus, the residual and the number of agreeing calibration images. Their quality is `high` for a residual ≤ 1.5 px and `medium` otherwise.

The cost per consensus image is dominated by reading the file and writing the TIFF; the alignment search itself is skipped. Split lens images are not written for consensus images unless `batch_keep_split_images` is enabled.

## Geometric model

The exported product currently uses global translational displacement (`dx`, `dy`) for each lens image. The pipeline does not apply dense optical flow, local mesh deformation or non-rigid warping.

This keeps the transformation interpretable and avoids inventing local geometric corrections without strong evidence. Close-range scenes with substantial depth variation can still contain local parallax after a global translation.

## Band configuration

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

Each report also records the source image size and the split mode. `proportional` means the source size differed from the expected grid (for example a RAW decoder adding border pixels); cells are then cut from the centre at native resolution. `proportional_resized` means the cells were smaller than expected and had to be resized — check such images.

For scientific use, inspect low/medium-quality results and a representative subset of high-quality results visually before downstream quantitative analysis.
