"""Tests guarding 14-layer Metashape import and lossless centered TIFF cropping."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
import warnings
from unittest.mock import patch

import numpy as np
import rasterio
from rasterio.errors import NotGeoreferencedWarning
import tifffile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prepare_metashape_tiffs import main
from stack_core import export_multipage_tiff_from_split
from tiff_metadata import DEFAULT_WAVELENGTHS_NM, gdal_band_metadata_extratags


def sample(height, width):
    return np.stack([
        (np.arange(height * width, dtype=np.uint16).reshape(height, width)
         + 1000 * index)
        for index in range(14)
    ], axis=0)


def legacy(path, data):
    with tifffile.TiffWriter(path) as writer:
        for index, band in enumerate(data):
            writer.write(
                band,
                photometric="minisblack",
                contiguous=False,
                metadata={"band_name": str(DEFAULT_WAVELENGTHS_NM[index])},
            )


def multichannel(path, data):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        with rasterio.open(
            path, "w", driver="GTiff", height=data.shape[1],
            width=data.shape[2], count=14, dtype="uint16"
        ) as dst:
            dst.write(data)


def cli(*args):
    with contextlib.redirect_stdout(io.StringIO()):
        return main(list(map(str, args)))


class TIFFLayoutTests(unittest.TestCase):
    def test_legacy_crop_preserves_fourteen_layers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "input", root / "output"
            source.mkdir()
            big = sample(13, 11)
            small = sample(12, 10)
            legacy(source / "big.tif", big)
            legacy(source / "small.tif", small)
            self.assertEqual(cli("--input", source, "--output", output), 0)
            with tifffile.TiffFile(output / "big.tif") as image:
                self.assertEqual(len(image.pages), 14)
                for index, page in enumerate(image.pages):
                    self.assertEqual(page.shape, (12, 10))
                    np.testing.assert_array_equal(page.asarray(), big[index, :12, :10])
            self.assertTrue((output / "crop_report.csv").is_file())
            self.assertTrue((source / "big.tif").is_file())

    def test_multichannel_conversion_restores_fourteen_layers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "input", root / "output"
            source.mkdir()
            original = sample(10, 12)
            multichannel(source / "stack.tif", original)
            self.assertEqual(
                cli("--input", source, "--output", output, "--layout", "multilayer"), 0
            )
            with tifffile.TiffFile(output / "stack.tif") as image:
                self.assertEqual(len(image.pages), 14)
                for index, page in enumerate(image.pages):
                    np.testing.assert_array_equal(page.asarray(), original[index])
                    self.assertIn(str(DEFAULT_WAVELENGTHS_NM[index]), page.description)

    def test_multichannel_default_stays_multichannel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "input", root / "output"
            source.mkdir()
            original = sample(10, 12)
            multichannel(source / "stack.tif", original)
            self.assertEqual(cli("--input", source, "--output", output), 0)
            with tifffile.TiffFile(output / "stack.tif") as image:
                self.assertEqual(len(image.pages), 1)
            with rasterio.open(output / "stack.tif") as image:
                self.assertEqual(image.count, 14)
                self.assertEqual(image.descriptions[0], "405 nm")
                np.testing.assert_array_equal(image.read(), original)

    def test_exporter_keeps_original_multilayer_format(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            aligned = root / "final.tif"
            images = {
                index: np.full((8, 9, 3), index, dtype=np.uint16)
                for index in range(1, 7)
            }
            alignment = {
                "apply_shifts": {
                    index: {"dx": 0, "dy": 0} for index in range(1, 7)
                },
                "mode": "test",
                "reference_id": 1,
                "pairs": {},
            }
            with patch("stack_core.load_split_images", return_value=images), patch(
                "stack_core.estimate_alignment_from_split", return_value=alignment
            ):
                export_multipage_tiff_from_split(root, aligned)
            with tifffile.TiffFile(aligned) as image:
                self.assertEqual(len(image.pages), 14)
                self.assertTrue(all(page.shape == (8, 9) for page in image.pages))

    def test_metadata_helper_single_page(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "single_page.tif"
            values = sample(8, 9)
            with tifffile.TiffWriter(image_path) as writer:
                writer.write(
                    (values[index] for index in range(14)),
                    shape=values.shape,
                    dtype=values.dtype,
                    photometric="minisblack",
                    planarconfig="separate",
                    metadata=None,
                    extratags=gdal_band_metadata_extratags(DEFAULT_WAVELENGTHS_NM),
                )
            with rasterio.open(image_path) as image:
                self.assertEqual(image.count, 14)
                self.assertEqual(image.descriptions[0], "405 nm")
                np.testing.assert_array_equal(image.read(), values)


if __name__ == "__main__":
    unittest.main()
