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

For scientific use, inspect low/medium-quality results and a representative subset of high-quality results visually before downstream quantitative analysis.
