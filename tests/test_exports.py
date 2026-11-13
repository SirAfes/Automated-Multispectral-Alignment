import py_compile
import re
from pathlib import Path

import numpy as np
import tifffile

from conftest import CELL, make_compound, write_jpg
from msalign.pipeline import process_single_file, run_pipeline
from msalign.settings import load_settings
from msalign.split import split_compound_array

CAMERA = {"lens": "Agrowing Sextuple", "focal_length_mm": 21.8, "pixel_size_um": 3.76, "band_fwhm_nm": 30}


def settings_for(tmp_path, **overrides):
    return load_settings(tmp_path / "none.json", {
        "cell_size": CELL, "camera": CAMERA, "output_size_px": [400, 400], "workers": 1, **overrides,
    })


def parse_hdr(path):
    text = Path(path).read_text()
    fields = {}
    for m in re.finditer(r"^([a-z ]+?) = (\{[^}]*\}|[^\n]*)$", text, re.M):
        fields[m.group(1).strip()] = m.group(2).strip()
    return fields


def test_envi_export(tmp_path):
    settings = settings_for(tmp_path, envi_export=True)
    result = process_single_file(write_jpg(tmp_path / "E.JPG", make_compound(1)), settings=settings)
    tif = Path(result["tiff"])
    hdr = parse_hdr(tif.with_suffix(".hdr"))
    assert (hdr["samples"], hdr["lines"], hdr["bands"]) == ("400", "400", "14")
    assert hdr["interleave"] == "bsq" and hdr["data type"] == "1" and hdr["byte order"] == "0"
    assert hdr["wavelength"].startswith("{405, 430, 450") and hdr["wavelength"].endswith("850}")
    assert hdr["fwhm"].startswith("{30, 30")
    assert hdr["band names"].startswith("{405 nm, 430 nm")
    assert hdr["wavelength units"] == "Nanometers"
    img = np.fromfile(tif.with_suffix(".img"), dtype=np.uint8).reshape(14, 400, 400)
    assert np.array_equal(img, tifffile.imread(tif))


def test_metashape_rig_export(tmp_path):
    settings = settings_for(tmp_path, metashape_export=True)
    imgs = [make_compound(1), make_compound(2)]
    paths = [write_jpg(tmp_path / f"M{i}.JPG", img) for i, img in enumerate(imgs)]
    success, failed = run_pipeline(paths, settings=settings, run_files=False)
    assert failed == []

    root = tmp_path / "metashape"
    folders = sorted(d.name for d in root.iterdir() if d.is_dir())
    assert len(folders) == 14 and folders[0] == "01_405nm" and folders[-1] == "14_850nm"
    for f in folders:
        assert sorted(p.name for p in (root / f).iterdir()) == ["M0.tif", "M1.tif"]

    # unaligned lens data: 710 nm is the R channel of lens 3 (BGR index 2)
    cells = split_compound_array((imgs[0] * 255).astype(np.uint8), cell_size=CELL)[0]
    expected = split_compound_array(
        __import__("cv2").imread(str(paths[0]), __import__("cv2").IMREAD_UNCHANGED), cell_size=CELL
    )[0][3][:, :, 2]
    with tifffile.TiffFile(root / "12_710nm" / "M0.tif") as t:
        page = t.pages[0]
        assert np.array_equal(page.asarray(), expected)
        assert page.shape == cells[3].shape[:2]
        tags = {tag.code: tag.value for tag in page.tags.values()}
    xmp = tags[700].decode() if isinstance(tags[700], bytes) else tags[700]
    assert "<rdf:li>710 nm</rdf:li>" in xmp and "<rdf:li>710</rdf:li>" in xmp
    assert "<Camera:RigCameraIndex>11</Camera:RigCameraIndex>" in xmp
    assert tags[37386] == (109, 5)

    def capture_uuid(path):
        with tifffile.TiffFile(path) as t:
            x = t.pages[0].tags[700].value
            x = x.decode() if isinstance(x, bytes) else x
        return re.search(r"<Camera:CaptureUUID>([^<]+)<", x).group(1)

    assert capture_uuid(root / "01_405nm" / "M0.tif") == capture_uuid(root / "14_850nm" / "M0.tif")
    assert capture_uuid(root / "01_405nm" / "M0.tif") != capture_uuid(root / "01_405nm" / "M1.tif")

    readme = (root / "README.txt").read_text()
    assert "Multi-camera system" in readme and "550 nm" in readme and "21.8" in readme
    script = root / "load_in_metashape.py"
    py_compile.compile(str(script), doraise=True)
    assert "FOCAL_LENGTH_MM = 21.8" in script.read_text()
    assert "PIXEL_SIZE_MM = 0.00376" in script.read_text()


def test_exports_off_by_default(tmp_path):
    settings = settings_for(tmp_path)
    result = process_single_file(write_jpg(tmp_path / "N.JPG", make_compound(1)), settings=settings)
    assert not Path(result["tiff"]).with_suffix(".hdr").exists()
    assert not (tmp_path / "metashape").exists()
