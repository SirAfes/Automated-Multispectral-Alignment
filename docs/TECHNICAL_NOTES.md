# Technical Notes

This document provides additional implementation detail for users who want to understand or modify the registration pipeline. The main README is intentionally kept accessible to users who primarily work with imagery rather than software development.

## Processing sequence

For each selected input image, processing is strictly sequential:

1. Create an output directory for the image.
2. Split the compound image into six lens images.
3. Estimate inter-lens translations.
4. Apply the accepted alignment solution.
5. Determine the common valid image area.
6. Extract and order the configured spectral bands.
7. Write the 14-band TIFF.
8. Write the alignment-quality TXT and CSV reports.
9. Continue to the next input image.

A failed image does not produce a completed TIFF. The batch interface records the failure and can continue with the next selected image.

## Registration strategy

### Primary registration

`alignment_core.py` performs the primary translation estimation. It combines phase-correlation-based coarse registration with local normalized cross-correlation measurements and consensus checks.

### Plan B: patch consensus

If the primary result is rejected, `directional_patch_consensus` in `alignment_plan_b.py` estimates translation from multiple local image regions and accepts a solution only when sufficient spatial support is present.

### Plan C: feature registration

If Plan B is also rejected, `feature_ransac_translation` uses AKAZE keypoints and descriptor matching to estimate a robust translation from feature correspondences.

The fallback order is therefore:

```text
Primary → Plan B → Plan C
```

Plan C is only used when the earlier stages do not provide an accepted result.

## Geometric model

The current pipeline estimates translational displacement (`dx`, `dy`) between lens images. It does not perform dense per-pixel optical-flow warping or a general non-rigid deformation.

This is deliberate: the current workflow aims to provide a controlled and interpretable global registration for a fixed multi-lens imaging system. Scenes with strong depth-dependent parallax can still contain local residual misregistration after a global translation has been applied.

## Band configuration

The six split images are currently mapped to 14 wavelengths:

| Lens image | Bands (nm) |
|---|---|
| 1 | 850 |
| 2 | 525, 630 |
| 3 | 405, 570, 710 |
| 4 | 430, 550, 650 |
| 5 | 450, 560, 685 |
| 6 | 490, 735 |

Zero-valued placeholders in the internal mapping represent channels that are not exported as spectral bands.

Final output order:

`405, 430, 450, 490, 525, 550, 560, 570, 630, 650, 685, 710, 735, 850 nm`

## Quality indicators

The report summarizes pair-level registration evidence using method-dependent confidence/score and support values. Because the registration stages use different evidence, their raw confidence values should not be interpreted as identical statistical probabilities.

For scientific use, inspect low-quality results and representative high-quality results visually before downstream classification or quantitative spectral analysis.
