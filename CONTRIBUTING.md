# Contributing

Thank you for helping improve msalign. Bug reports, test images, documentation and code are all welcome.

## Reporting a problem

Open an issue with:

- what you ran (desktop app or `msalign …` command) and the msalign version (`msalign --version`);
- the camera / lens layout and, if you changed them, your `pipeline_config.json` settings;
- the run log and summary from the `msalign_runs/` folder, and the `_alignment_quality.txt` of an affected image.

Sample images help most. If you cannot share full images, a downscaled or cropped compound image that shows the problem is often enough. Only share images you are allowed to redistribute.

## Supporting another camera

Many cameras only need a settings file: `grid_cols`, `grid_rows`, `cell_size`, `center_crop_frac`, `file_bands` and `output_order`, and possibly `parallax_range_px` for the working distance. If your camera works with a settings file, consider contributing it (with a short description of the camera) to the documentation.

## Code changes

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

- Keep changes focused, and add or extend a test in `tests/` for behaviour you change. The tests use synthetic compound images with known shifts (`tests/conftest.py`), so no real data is needed.
- New settings go into `DEFAULT_SETTINGS` in `src/msalign/settings.py` with a comment, plus validation where a wrong value would give confusing results.
- Alignment changes should keep the project's principle: a result is accepted only when the image supports it. Prefer adding an independent check over loosening a threshold, and report what was done in the quality report.
- Use `logging` (`log = logging.getLogger(__name__)`), not `print`.

CI runs the tests on Windows and Linux for every push and pull request.

## Releasing

1. Update `__version__` in `src/msalign/__init__.py`, `version` in `CITATION.cff`, and add a `## <version>` section to `CHANGELOG.md`.
2. Merge to `main`, then create the tag `v<version>` (on the Releases page, or `git tag v<version> && git push origin v<version>`).
3. The **Release** workflow tests the tagged code, builds the Windows ZIP, the wheel and the source archive, and publishes them as a GitHub Release with the changelog section as notes.

