"""
exports.py

Additional output formats.

ENVI (envi_export)
    <name>.img (band sequential, raw) + <name>.hdr with band names,
    wavelengths and FWHM. Read by ENVI, GDAL/QGIS and hyperspectral
    libraries such as Spectral Python.

Photogrammetry rig (metashape_export)
    For 3D reconstruction the aligned TIFF is not the right input: its bands
    are shifted (and, with parallax correction, warped) to match each other,
    so it is no longer a plain central projection. Photogrammetry software
    should instead see each lens as its own camera of a fixed multi-camera
    rig and solve the geometry in 3D, which also handles depth parallax.

    The *unaligned* lens images are written as single-band TIFFs, one folder
    per band, with the same file name for every band of a capture:

        metashape/01_405nm/DSC05928.tif
        metashape/02_430nm/DSC05928.tif
        ...

    This is the folder layout Agisoft Metashape loads as a multi-camera system
    (Workflow > Add Folder > "Multi-camera system"). Each file carries camera
    make/model, focal length and pixel size (TIFF/EP tags) and the XMP Camera
    tags used for multispectral rigs (BandName, CentralWavelength,
    WavelengthFWHM, RigName, RigCameraIndex, CaptureUUID).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
import tifffile

from . import __version__
from .metadata import _rational_tag, band_records, camera_info


# ---------------------------------------------------------------------------
# ENVI
# ---------------------------------------------------------------------------

ENVI_DTYPES = {np.dtype("uint8"): 1, np.dtype("int16"): 2, np.dtype("float32"): 4,
               np.dtype("float64"): 5, np.dtype("uint16"): 12}


def _envi_list(values, fmt="{}"):
    return "{" + ", ".join(fmt.format(v) for v in values) + "}"


def write_envi(output_tiff: Path, data: np.ndarray, meta: dict) -> tuple[Path, Path]:
    """Band-sequential ENVI copy of the aligned stack (data: bands x lines x samples)."""
    dtype = np.dtype(data.dtype)
    if dtype not in ENVI_DTYPES:
        raise ValueError(f"ENVI export does not support {dtype}")
    img_path = output_tiff.with_suffix(".img")
    hdr_path = output_tiff.with_suffix(".hdr")
    data.astype(dtype.newbyteorder("<"), copy=False).tofile(img_path)

    bands = meta["bands"]
    cam = meta["camera"]
    desc = (
        f"msalign {__version__} aligned multiband image; source {meta.get('source_file')}; "
        f"camera {cam.get('make', '')} {cam.get('model', '')} {cam.get('lens', '')}".strip()
    )
    lines = [
        "ENVI",
        f"description = {{{desc}}}",
        f"samples = {data.shape[2]}",
        f"lines = {data.shape[1]}",
        f"bands = {data.shape[0]}",
        "header offset = 0",
        "file type = ENVI Standard",
        f"data type = {ENVI_DTYPES[dtype]}",
        "interleave = bsq",
        "byte order = 0",
        "band names = " + _envi_list([b["name"] for b in bands]),
    ]
    if all(b["center_wavelength_nm"] is not None for b in bands):
        lines.append("wavelength units = Nanometers")
        lines.append("wavelength = " + _envi_list([b["center_wavelength_nm"] for b in bands], "{:g}"))
        if all(b["fwhm_nm"] is not None for b in bands):
            lines.append("fwhm = " + _envi_list([b["fwhm_nm"] for b in bands], "{:g}"))
        # a natural-colour preview: bands closest to 650, 550 and 450 nm (1-based)
        wl = np.array([b["center_wavelength_nm"] for b in bands])
        lines.append("default bands = " + _envi_list([int(np.argmin(abs(wl - t))) + 1 for t in (650, 550, 450)]))
    if meta["output"].get("padded_pixels"):
        lines.append("data ignore value = 0")
    if "pixel_size_um_output" in cam:
        lines.append(f"pixel size = {{{cam['pixel_size_um_output']:g}, {cam['pixel_size_um_output']:g}, units=Micrometers}}")
    hdr_path.write_text("\n".join(lines) + "\n", encoding="ascii", errors="replace")
    return img_path, hdr_path


# ---------------------------------------------------------------------------
# Photogrammetry rig (Metashape)
# ---------------------------------------------------------------------------

RIG_DIR_NAME = "metashape"


def band_folder(record: dict) -> str:
    wl = record["center_wavelength_nm"]
    label = f"{wl:g}nm" if wl is not None else record["label"]
    return f"{record['band']:02d}_{label}"


def _xmp_packet(record: dict, rig_name: str, capture_uuid: str) -> bytes:
    def opt(tag, value):
        return f"      <Camera:{tag}>{escape(str(value))}</Camera:{tag}>\n" if value is not None else ""

    def seq(tag, value):
        if value is None:
            return ""
        return (f"      <Camera:{tag}><rdf:Seq><rdf:li>{escape(str(value))}</rdf:li></rdf:Seq>"
                f"</Camera:{tag}>\n")

    wl = record["center_wavelength_nm"]
    fwhm = record["fwhm_nm"]
    body = (
        seq("BandName", record["name"])
        + seq("CentralWavelength", f"{wl:g}" if wl is not None else None)
        + seq("WavelengthFWHM", f"{fwhm:g}" if fwhm is not None else None)
        + opt("RigName", rig_name)
        + opt("RigCameraIndex", record["band"] - 1)
        + opt("CaptureUUID", capture_uuid)
    )
    xmp = (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about="" xmlns:Camera="http://pix4d.com/camera/1.0/">\n'
        f"{body}"
        "  </rdf:Description>\n"
        " </rdf:RDF>\n"
        "</x:xmpmeta>\n"
        '<?xpacket end="w"?>'
    )
    return xmp.encode("utf-8")


def write_rig_images(images: dict, band_map: dict, output_order, settings: dict,
                     source_path: Path, name: str, exif: dict) -> Path:
    """
    Write the unaligned lens image of every band for one capture into
    <source folder>/metashape/<NN_wavelength>/<name>.tif. Returns the rig folder.
    """
    source_path = Path(source_path)
    root = source_path.parent / RIG_DIR_NAME
    cam = camera_info(settings, exif)
    rig_name = "msalign " + " ".join(
        str(cam[k]) for k in ("make", "model", "lens") if k in cam
    ).strip()
    capture_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"msalign:{source_path.name}:"
                                                      f"{exif.get('datetime_original', '')}"))

    extratags = []
    if "make" in cam:
        extratags.append((271, "s", 0, str(cam["make"]), True))
    if "model" in cam:
        extratags.append((272, "s", 0, str(cam["model"]), True))
    if "focal_length_mm" in cam:
        extratags.append((37386, "2I", 1, _rational_tag(float(cam["focal_length_mm"])), True))
    if cam.get("pixel_size_um"):
        per_cm = _rational_tag(10000.0 / float(cam["pixel_size_um"]))
        extratags += [(37390, "2I", 1, per_cm, True), (37391, "2I", 1, per_cm, True),
                      (37392, "H", 1, 3, True)]

    acquired = exif.get("datetime_original")
    for record in band_records(settings, band_map, output_order):
        lens, ch = band_map[record["label"]]
        src = images[lens]
        plane = np.ascontiguousarray(src[:, :, ch] if src.ndim == 3 else src)
        folder = root / band_folder(record)
        folder.mkdir(parents=True, exist_ok=True)
        tags = extratags + [(700, "B", 0, _xmp_packet(record, rig_name, capture_uuid), True)]
        kwargs = {"datetime": acquired} if acquired else {}
        tifffile.imwrite(
            str(folder / f"{name}.tif"), plane, photometric="minisblack", metadata=None,
            description=f"msalign {__version__}: unaligned lens {lens} image, band {record['name']}",
            software=f"msalign {__version__}", extratags=tags, **kwargs,
        )
    return root


LOAD_SCRIPT = '''"""
Load this folder into Agisoft Metashape Pro as a multi-camera system.

Run in Metashape Pro: Tools > Run Script... > select this file.
(Written by msalign; not tested inside Metashape by the msalign authors -
the GUI steps in README.txt do the same.)
"""

import os

import Metashape

ROOT = os.path.dirname(os.path.abspath(__file__))
FOCAL_LENGTH_MM = {focal}
PIXEL_SIZE_MM = {pixel}

bands = sorted(d for d in os.listdir(ROOT) if os.path.isdir(os.path.join(ROOT, d)))
names = sorted(os.listdir(os.path.join(ROOT, bands[0])))
captures = [
    [os.path.join(ROOT, band, name) for band in bands]
    for name in names
    if all(os.path.isfile(os.path.join(ROOT, band, name)) for band in bands)
]

doc = Metashape.app.document
chunk = doc.chunk if doc.chunk else doc.addChunk()
chunk.addPhotos(filenames=captures, layout=Metashape.MultiplaneLayout)

for sensor in chunk.sensors:
    if FOCAL_LENGTH_MM:
        sensor.focal_length = FOCAL_LENGTH_MM
    if PIXEL_SIZE_MM:
        sensor.pixel_width = PIXEL_SIZE_MM
        sensor.pixel_height = PIXEL_SIZE_MM

print(f"msalign: added {{len(captures)}} captures x {{len(bands)}} bands.")
print("Next: Tools > Camera Calibration > right-click the {master} band > Set as Master, then Align Photos.")
'''

README = """msalign photogrammetry export
=============================

Each subfolder holds one band; files with the same name are the bands of one
capture. The images are the *unaligned* lens images: the photogrammetry
software solves the lens geometry (including depth parallax) itself.

Agisoft Metashape (GUI)
  1. Workflow > Add Folder... > select this folder > "Multi-camera system".
  2. Tools > Camera Calibration: check focal length ({focal} mm) and pixel size
     ({pixel_um} um) for each band; right-click the {master} band > "Set as Master"
     (a bright, sharp band; the shortest-wavelength bands are dark and noisy).
  3. Align Photos, then continue as usual.

Agisoft Metashape Pro (script)
  Tools > Run Script... > load_in_metashape.py (does step 1 and the focal
  length / pixel size of step 2).

Other software (Pix4D etc.): the files carry the XMP Camera tags BandName,
CentralWavelength, WavelengthFWHM, RigName, RigCameraIndex and CaptureUUID.
"""


def write_rig_helpers(root: Path, settings: dict, master_label: str = "550"):
    cam = settings.get("camera") or {}
    focal = cam.get("focal_length_mm")
    pixel = cam.get("pixel_size_um")
    root.mkdir(parents=True, exist_ok=True)
    (root / "load_in_metashape.py").write_text(
        LOAD_SCRIPT.format(
            focal=repr(float(focal)) if focal else "None",
            pixel=repr(float(pixel) / 1000.0) if pixel else "None",
            master=f"{master_label} nm",
        ),
        encoding="utf-8",
    )
    (root / "README.txt").write_text(
        README.format(focal=focal or "?", pixel_um=pixel or "?", master=f"{master_label} nm"),
        encoding="utf-8",
    )
