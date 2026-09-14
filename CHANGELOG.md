# Changelog

## Unreleased

### Added

- Graphical Windows processing window with progress, current stage and estimated remaining time.
- Expandable live details table for per-image success, retry and error states.
- Pair-specific alignment history learned from successful images.
- Robust median/MAD historical priors using up to five successful neighbouring frames.
- History-guided pre-shift followed by a fresh Primary → Plan B → Plan C residual retry.
- Second-pass retry for unresolved images using nearest successful images on either side of the sequence.
- History/prior information in alignment quality reports.

### Improved

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
