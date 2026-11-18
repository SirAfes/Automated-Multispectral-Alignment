"""
One-file-per-band captures (MicaSense-style): band files carry per-band lens
calibration in XMP; msalign maps them into the reference camera, then aligns.
"""

from pathlib import Path

import cv2
import numpy as np
import pytest
import tifffile

from conftest import make_scene
from msalign.pipeline import collect_folder_images, process_single_file, run_pipeline
from msalign.rig_input import _rotation, capture_files, is_band_file, load_capture
from msalign.settings import load_settings

W, H = 640, 480
PIXEL_UM = 3.75
BANDS = {  # band number: (name, wavelength, fwhm, focal px, principal point px, distortion, rig rotation deg)
    1: ("Blue", 475, 32, 721.0, (329, 239), (-0.10, 0.22, -0.20, 0.0007, -0.0005), (-0.42, 0.09, -0.10)),
    2: ("Green", 560, 27, 717.0, (321, 237), (-0.11, 0.23, -0.22, -0.0001, -0.0010), (0.0, 0.0, 0.0)),
    3: ("Red", 668, 14, 722.0, (307, 253), (-0.11, 0.21, -0.17, 0.0004, -0.0007), (-0.54, -0.02, -0.06)),
    4: ("NIR", 842, 57, 723.0, (317, 240), (-0.11, 0.22, -0.18, 0.0008, -0.0009), (-0.53, 0.24, 0.08)),
    5: ("Red edge", 717, 12, 726.0, (318, 241), (-0.12, 0.23, -0.20, 0.0007, -0.0005), (-0.27, 0.17, -0.02)),
}
REF = 2


def K_of(f, pp):
    return np.array([[f, 0, pp[0]], [0, f, pp[1]], [0, 0, 1]], dtype=np.float64)


def xmp(band):
    name, wl, fwhm, f, pp, dist, rr = BANDS[band]
    mm = PIXEL_UM / 1000.0
    seq = lambda tag, vals: (  # noqa: E731
        f"<Camera:{tag}><rdf:Seq>" + "".join(f"<rdf:li>{v}</rdf:li>" for v in vals) + f"</rdf:Seq></Camera:{tag}>"
    )
    return (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:Camera="http://pix4d.com/camera/1.0/"'
        f' Camera:BandName="{name}" Camera:CentralWavelength="{wl}" Camera:WavelengthFWHM="{fwhm}"'
        f' Camera:RigCameraIndex="{band - 1}" Camera:RigRelativesReferenceRigCameraIndex="{REF - 1}"'
        f' Camera:RigRelatives="{rr[0]}, {rr[1]}, {rr[2]}" Camera:PerspectiveFocalLengthUnits="mm"'
        f' Camera:PrincipalPoint="{pp[0] * mm},{pp[1] * mm}">'
        + seq("PerspectiveFocalLength", [f * mm]) + seq("PerspectiveDistortion", dist)
        + "</rdf:Description></rdf:RDF></x:xmpmeta>"
    ).encode()


def write_capture(folder: Path, name: str, seed: int, shift=(0, 0)):
    """
    Each band camera sees the reference camera's ideal image through its own
    intrinsics, distortion and rig rotation (plus an optional translation).
    """
    rng = np.random.default_rng(seed)
    scene = make_scene(seed, 900).mean(axis=2)
    ideal = scene[130:130 + H, 130:130 + W]
    K_ref = K_of(BANDS[REF][3], BANDS[REF][4])
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    pts = np.stack([xx.ravel(), yy.ravel()], axis=1).reshape(-1, 1, 2)
    per_cm = (int(10000 / PIXEL_UM * 1000), 1000)
    for b, (_, _, _, f, pp, dist, rr) in BANDS.items():
        k1, k2, k3, p1, p2 = dist
        d = np.array([k1, k2, p1, p2, k3])
        # band pixel -> ray -> rotate into reference camera -> reference pixel
        ref_pts = cv2.undistortPoints(pts, K_of(f, pp), d, R=_rotation(rr), P=K_ref).reshape(H, W, 2)
        sx, sy = (shift if b != REF else (0, 0))
        img = cv2.remap(ideal.astype(np.float32), ref_pts[..., 0] + sx, ref_pts[..., 1] + sy,
                        cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        img = img * (0.6 + 0.1 * b) * 40000 + rng.normal(0, 60, img.shape)
        tifffile.imwrite(
            str(folder / f"{name}_{b}.tif"), np.clip(img, 0, 65535).astype(np.uint16),
            photometric="minisblack", metadata=None,
            extratags=[(271, "s", 0, "TestCam", True), (272, "s", 0, "Rig-5", True),
                       (37390, "2I", 1, per_cm, True), (37392, "H", 1, 3, True),
                       (700, "B", 0, xmp(b), True)],
        )
    return folder / f"{name}_1.tif"


@pytest.fixture
def base(tmp_path):
    return load_settings(tmp_path / "none.json", {"workers": 1})


def test_detects_and_groups_band_files(tmp_path, base):
    for i in range(2):
        write_capture(tmp_path, f"IMG_000{i}", i)
    (tmp_path / "notes_1.txt").write_text("x")
    assert is_band_file(tmp_path / "IMG_0000_3.tif", base)
    assert len(capture_files(tmp_path / "IMG_0000_3.tif")) == 5
    assert [p.name for p in collect_folder_images(tmp_path, settings=base)] == ["IMG_0000_1.tif", "IMG_0001_1.tif"]
    assert not is_band_file(tmp_path / "IMG_0000_3.tif", dict(base, input_mode="compound"))


def test_normalisation_removes_lens_differences(tmp_path, base):
    src = write_capture(tmp_path, "IMG_0005", 5)
    images, eff, info = load_capture(src, base)
    assert "rig rotations" in info["normalisation"]
    assert eff["output_order"] == ["475", "560", "668", "717", "842"]
    assert eff["primary_reference_lens"] == 2

    def misfit(imgs):
        g = {k: cv2.GaussianBlur(v.astype(np.float32), (0, 0), 1.5) for k, v in imgs.items()}
        ref = g[2][60:-60, 60:-60]
        errs = []
        for k in (1, 3, 4, 5):
            a = g[k][60:-60, 60:-60]
            a = (a - a.mean()) / a.std()
            r = (ref - ref.mean()) / ref.std()
            errs.append(float(np.mean((a - r) ** 2)))
        return max(errs)

    raw = {k: tifffile.imread(str(f)) for k, f in capture_files(src).items()}
    assert misfit(images) < 0.1 * misfit(raw)

    no_rot, _, info2 = load_capture(src, dict(base, rig_rotation=False))
    assert "rig rotations" not in info2["normalisation"]
    assert misfit(images) < misfit(no_rot)


def test_end_to_end_band_files(tmp_path, base):
    src = write_capture(tmp_path, "IMG_0007", 7, shift=(3, -2))
    result = process_single_file(src, settings=base)
    assert Path(result["tiff"]).name == "IMG_0007_aligned_multiband.tif"
    assert result["quality"] in {"high", "medium"} and result["closure_status"] == "pass"
    with tifffile.TiffFile(result["tiff"]) as t:
        page = t.pages[0]
        data = np.stack([p.asarray() for p in t.pages])
        tags = {tag.code: tag.value for tag in page.tags.values()}
    assert len(t.pages) == 5 and data.shape[0] == 5 and data.dtype == np.uint16
    assert tags[271] == "TestCam" and tags[272] == "Rig-5"
    assert "<Item name=\"DESCRIPTION\" sample=\"4\" role=\"description\">842 nm</Item>" in tags[42112]
    assert "FWHM_UM" in tags[42112]
    local = result["output"]["local_alignment"]
    assert local["worst_p90_px"] is not None and local["worst_p90_px"] < 1.5


def test_band_files_in_a_run_and_parallax_option(tmp_path, base):
    for i in range(3):
        write_capture(tmp_path, f"IMG_01{i:02d}", 10 + i)
    paths = collect_folder_images(tmp_path, settings=base)
    success, failed = run_pipeline(paths, settings=dict(base, parallax_correction=True), run_files=False)
    assert failed == [] and len(success) == 3
    local = success[0]["output"]["local_alignment"]
    assert local["parallax_applied"] is False and "lens positions unknown" in local["parallax_reason"]


def test_band_file_settings_validation(tmp_path):
    with pytest.raises(ValueError, match="input_mode"):
        load_settings(tmp_path / "none.json", {"input_mode": "files"})
    with pytest.raises(ValueError, match="band_file_pattern"):
        load_settings(tmp_path / "none.json", {"band_file_pattern": r"(?P<capture>.+)"})
