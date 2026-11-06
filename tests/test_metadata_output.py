import json
import re
import struct
from pathlib import Path

import numpy as np
import pytest
import tifffile

from conftest import CELL, make_compound, physical_shifts, write_fake_raw, write_jpg
from msalign.metadata import read_source_exif
from msalign.pipeline import process_single_file
from msalign.settings import load_settings

CAMERA = {
    "lens": "Agrowing Sextuple",
    "focal_length_mm": 21.8,
    "pixel_size_um": 3.76,
    "band_fwhm_nm": 30,
}
SIZE = [400, 400]


def make_settings(tmp_path, **overrides):
    base = {"cell_size": CELL, "camera": CAMERA, "output_size_px": SIZE}
    base.update(overrides)
    return load_settings(tmp_path / "none.json", base)


def add_exif(path: Path, make="SONY", model="ILCE-7RM4", focal=None):
    """Insert an EXIF block (written with Pillow) into an existing JPEG."""
    pil = pytest.importorskip("PIL.Image")
    exif = pil.Exif()
    exif[271], exif[272] = make, model
    sub = exif.get_ifd(0x8769)
    sub[36867] = "2026:11:01 10:30:00"  # DateTimeOriginal
    sub[33434] = (1, 250)               # ExposureTime
    sub[34855] = 200                    # ISO
    if focal:
        sub[37386] = focal
    payload = exif.tobytes()
    if not payload.startswith(b"Exif\x00\x00"):
        payload = b"Exif\x00\x00" + payload
    data = path.read_bytes()
    segment = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    path.write_bytes(data[:2] + segment + data[2:])
    return path


def tags_of(path):
    with tifffile.TiffFile(path) as tif:
        page = tif.pages[0]
        return {t.code: t.value for t in page.tags.values()}, len(tif.pages), page.asarray()


def gdal_items(xml):
    return {
        (m.group(1), m.group(2)): m.group(3)
        for m in re.finditer(r'<Item name="([^"]+)"(?: sample="(\d+)")?[^>]*>([^<]*)</Item>', xml)
    }


# --- metadata -----------------------------------------------------------------

def test_exif_reader(tmp_path):
    src = add_exif(write_jpg(tmp_path / "e.jpg", make_compound(0)), focal=(218, 10))
    exif = read_source_exif(src)
    assert exif["make"] == "SONY" and exif["model"] == "ILCE-7RM4"
    assert exif["focal_length_mm"] == pytest.approx(21.8)
    assert exif["datetime_original"] == "2026:11:01 10:30:00"
    assert exif["iso"] == 200

    (tmp_path / "x.ARW").write_text("not a raw file")
    assert read_source_exif(tmp_path / "x.ARW") == {}
    assert read_source_exif(write_jpg(tmp_path / "plain.jpg", make_compound(0))) == {}


def test_tiff_has_camera_band_and_processing_metadata(tmp_path):
    settings = make_settings(tmp_path)
    src = add_exif(write_jpg(tmp_path / "DSC0001.JPG", make_compound(1)))
    result = process_single_file(src, settings=settings)

    tags, pages, data = tags_of(result["tiff"])
    # one image with all bands, not one page per band
    assert pages == 1 and data.shape == (14, *SIZE[::-1])

    assert tags[271] == "SONY" and tags[272] == "ILCE-7RM4"           # from EXIF
    assert tags[37386] == (109, 5)                                     # 21.8 mm from settings
    num, den = tags[37390]
    assert 10000 / (num / den) == pytest.approx(3.76, abs=1e-3)       # pixel size, per cm
    assert tags[37392] == 3
    assert tags[305].startswith("msalign ")
    assert tags[306] == "2026:11:01 10:30:00"

    items = gdal_items(tags[42112])
    assert items[("DESCRIPTION", "0")] == "405 nm"
    assert items[("DESCRIPTION", "13")] == "850 nm"
    assert items[("CENTRAL_WAVELENGTH_UM", "0")] == "0.4050"
    assert items[("FWHM_UM", "6")] == "0.0300"
    assert items[("LENS", "13")] == "1"
    assert items[("CAMERA_MODEL", None)] == "ILCE-7RM4"
    assert items[("LENS", None)] == "Agrowing Sextuple"

    meta = json.loads(tags[270])
    assert meta["camera"]["sources"]["model"] == "exif"
    assert meta["camera"]["sources"]["focal_length_mm"] == "settings"
    assert [b["center_wavelength_nm"] for b in meta["bands"]][:3] == [405, 430, 450]

    sidecar = json.loads(Path(result["metadata_json"]).read_text(encoding="utf-8"))
    assert sidecar["output"]["width"] == 400 and sidecar["source_file"] == "DSC0001.JPG"


def test_missing_fields_are_left_out_not_guessed(tmp_path):
    settings = make_settings(tmp_path, camera={"lens": None, "focal_length_mm": None,
                                               "pixel_size_um": None, "band_fwhm_nm": None})
    src = write_jpg(tmp_path / "noexif.JPG", make_compound(1))
    tags, _, _ = tags_of(process_single_file(src, settings=settings)["tiff"])
    assert 271 not in tags and 272 not in tags and 37386 not in tags and 37390 not in tags
    items = gdal_items(tags[42112])
    assert ("FWHM_UM", "0") not in items and items[("DESCRIPTION", "0")] == "405 nm"


def test_raw_output_is_16bit_with_metadata(tmp_path, fake_rawpy):
    settings = make_settings(tmp_path)
    src = write_fake_raw(tmp_path / "DSC0001.ARW", make_compound(1))
    tags, _, data = tags_of(process_single_file(src, settings=settings)["tiff"])
    assert data.dtype == np.uint16 and data.shape[0] == 14
    meta = json.loads(tags[270])
    assert meta["data_type"] == "uint16"
    assert meta["raw_decoding"]["demosaic"] == "linear"
    assert fake_rawpy.calls[-1]["demosaic_algorithm"] == "LINEAR"

    process_single_file(src, settings=make_settings(tmp_path, raw_demosaic="ahd"))
    assert fake_rawpy.calls[-1]["demosaic_algorithm"] == "AHD"


def test_legacy_multipage_layout(tmp_path):
    settings = make_settings(tmp_path, tiff_layout="multipage")
    src = add_exif(write_jpg(tmp_path / "L.JPG", make_compound(1)))
    tags, pages, _ = tags_of(process_single_file(src, settings=settings)["tiff"])
    assert pages == 14 and tags[272] == "ILCE-7RM4" and 42112 in tags


def test_deflate_compression_roundtrip(tmp_path):
    plain = process_single_file(write_jpg(tmp_path / "A.JPG", make_compound(1)),
                                settings=make_settings(tmp_path))
    packed = process_single_file(write_jpg(tmp_path / "B.JPG", make_compound(1)),
                                 settings=make_settings(tmp_path, tiff_compression="deflate"))
    assert np.array_equal(tifffile.imread(plain["tiff"]), tifffile.imread(packed["tiff"]))
    assert Path(packed["tiff"]).stat().st_size < Path(plain["tiff"]).stat().st_size


# --- standard output size -------------------------------------------------------

def _band_correlation(a, b):
    a = a.astype(np.float64) - a.mean()
    b = b.astype(np.float64) - b.mean()
    return float((a * b).sum() / np.sqrt((a * a).sum() * (b * b).sum()))


def test_fixed_size_keeps_band_alignment(tmp_path):
    settings = make_settings(tmp_path)
    near = physical_shifts(parallax=33)
    outputs = []
    for i, shifts in enumerate([physical_shifts(), near]):
        src = write_jpg(tmp_path / f"S{i}.JPG", make_compound(i, shifts))
        result = process_single_file(src, settings=settings)
        outputs.append(result)
        data = tifffile.imread(result["tiff"])
        assert data.shape == (14, 400, 400)

        # 850 nm (lens 1, R) and 630 nm (lens 2, R) see the same scene channel:
        # after alignment and cropping they must coincide at zero offset.
        order = settings["output_order"]
        b850 = data[order.index("850")][10:-10, 10:-10]
        b630 = data[order.index("630")]
        scores = {
            (dx, dy): _band_correlation(b850, b630[10 + dy:390 + dy, 10 + dx:390 + dx])
            for dx in range(-3, 4) for dy in range(-3, 4)
        }
        assert max(scores, key=scores.get) == (0, 0)
        assert scores[(0, 0)] > 0.85

    # different parallax -> different shared area -> crop origin differs, and is recorded
    o0, o1 = (r["output"]["origin_in_reference_lens_px"] for r in outputs)
    assert o0 != o1
    assert all(r["output"]["padded_pixels"] == 0 for r in outputs)


def test_origin_recorded_in_source_coordinates(tmp_path):
    settings = make_settings(tmp_path)
    result = process_single_file(write_jpg(tmp_path / "O.JPG", make_compound(1)), settings=settings)
    ox, oy = result["output"]["origin_in_reference_lens_px"]
    margin = (CELL - int(CELL * 0.8)) // 2  # lens 1 image starts here in the source
    assert result["output"]["origin_in_source_px"] == [margin + ox, margin + oy]
    split_info = json.loads((Path(result["output_dir"]) / "split_lens_images" / "split_info.json").read_text())
    assert split_info["lens_origins"]["1"] == [margin, margin]


def test_fixed_anchor_same_origin_for_every_image(tmp_path):
    settings = make_settings(tmp_path, output_anchor="fixed", output_size_px=[360, 360],
                             output_fixed_center_px=[230, 236])
    origins = []
    for i, p in enumerate([24, 30]):
        result = process_single_file(write_jpg(tmp_path / f"F{i}.JPG", make_compound(i, physical_shifts(parallax=p))),
                                     settings=settings)
        origins.append(result["output"]["origin_in_reference_lens_px"])
    assert origins[0] == origins[1] == [50, 56]


def test_window_larger_than_shared_area(tmp_path):
    src = write_jpg(tmp_path / "P.JPG", make_compound(1))
    with pytest.raises(RuntimeError, match="does not contain"):
        process_single_file(src, settings=make_settings(tmp_path, output_size_px=[500, 500]))

    result = process_single_file(src, settings=make_settings(
        tmp_path, output_size_px=[500, 500], output_max_padding_px=40))
    assert result["output"]["padded_pixels"] > 0
    tags, _, data = tags_of(result["tiff"])
    assert tags[42113] == "0"  # GDAL_NODATA
    assert data[:, 0, 0].max() == 0 or data[:, -1, -1].max() == 0


def test_variable_size_when_disabled(tmp_path):
    result = process_single_file(write_jpg(tmp_path / "V.JPG", make_compound(1)),
                                 settings=make_settings(tmp_path, output_size_px=None))
    ca = result["output"]["common_area_in_reference_lens_px"]
    assert (result["output"]["width"], result["output"]["height"]) == (ca["width"], ca["height"])


# --- settings -----------------------------------------------------------------------

def test_other_camera_does_not_inherit_reference_metadata(tmp_path, caplog):
    settings = load_settings(tmp_path / "none.json", {"cell_size": 1000})
    assert settings["camera"]["lens"] is None and settings["camera"]["focal_length_mm"] is None
    assert settings["output_size_px"] is None
    assert "reference defaults" in caplog.text


def test_camera_settings_merge_keywise(tmp_path):
    settings = load_settings(tmp_path / "none.json", {"camera": {"model": "ILX-LR1"}})
    assert settings["camera"]["model"] == "ILX-LR1"
    assert settings["camera"]["focal_length_mm"] == 21.8
    assert settings["output_size_px"] == [2048, 2048]


def test_invalid_output_settings(tmp_path):
    with pytest.raises(ValueError, match="larger than a lens image"):
        load_settings(tmp_path / "none.json", {"output_size_px": [3000, 3000]})
    with pytest.raises(ValueError, match="Unknown camera setting"):
        load_settings(tmp_path / "none.json", {"camera": {"focal_mm": 20}})
