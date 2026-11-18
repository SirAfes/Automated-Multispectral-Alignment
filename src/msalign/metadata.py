"""
metadata.py

Camera, band and processing metadata for the output TIFF.

Sources, in order of precedence:
1. `camera` settings (values the user states explicitly, e.g. the effective
   focal length of a multi-lens add-on that the camera body cannot report);
2. EXIF of the source image (camera make / model, lens, focal length, date,
   exposure), read with tifffile, so no extra dependency is needed;
3. otherwise the field is left out - nothing is guessed.

The metadata is written in several places so that common tools find it:
- baseline TIFF tags: Make, Model, Software, DateTime, ImageDescription (JSON);
- TIFF/EP tags in IFD0: FocalLength, FocalPlaneX/YResolution (pixel size);
- GDAL_METADATA: band descriptions ("405 nm"), central wavelength and FWHM per
  band (GDAL / QGIS / rasterio show these as band information);
- a JSON sidecar file with everything, including the crop and alignment record.
"""

from __future__ import annotations

import io
import json
import logging
import re
import struct
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from xml.sax.saxutils import escape

from . import __version__


log = logging.getLogger(__name__)

TIFF_BASED_EXTENSIONS = {".tif", ".tiff", ".arw", ".dng", ".nef", ".cr2", ".pef", ".srw"}
JPEG_EXTENSIONS = {".jpg", ".jpeg"}

# TIFF/EP focal plane resolution unit: 3 = centimetre (also understood by EXIF readers).
UNIT_CM = 3


# ---------------------------------------------------------------------------
# Reading EXIF from the source image
# ---------------------------------------------------------------------------

def _rational(value):
    if isinstance(value, tuple) and len(value) == 2:
        num, den = value
        return float(num) / float(den) if den else None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value):
    if isinstance(value, bytes):
        value = value.split(b"\x00", 1)[0].decode("utf-8", "replace")
    if value is None:
        return None
    text = str(value).strip().strip("\x00").strip()
    return text or None


def _exif_from_tiff_stream(stream) -> dict:
    import tifffile

    tifffile_log = logging.getLogger("tifffile")
    previous = tifffile_log.level
    tifffile_log.setLevel(logging.ERROR)  # EXIF blocks have no image data; that is fine
    try:
        with tifffile.TiffFile(stream) as tif:
            tags = tif.pages[0].tags
            ifd0 = {t.name: t.value for t in tags.values()}
    finally:
        tifffile_log.setLevel(previous)

    exif = ifd0.get("ExifTag") or {}
    if not isinstance(exif, dict):
        exif = {}

    def pick(*names, source=None):
        for name in names:
            for src in ([source] if source is not None else [exif, ifd0]):
                if name in src and src[name] not in (None, "", 0):
                    return src[name]
        return None

    focal = _rational(pick("FocalLength"))
    out = {
        "make": _text(ifd0.get("Make")),
        "model": _text(ifd0.get("Model")),
        "lens": _text(pick("LensModel")),
        "focal_length_mm": focal if focal else None,
        "datetime_original": _text(pick("DateTimeOriginal", "DateTime")),
        "exposure_time_s": _rational(pick("ExposureTime")),
        "f_number": _rational(pick("FNumber")),
        "iso": pick("ISOSpeedRatings", "PhotographicSensitivity"),
        "serial_number": _text(pick("BodySerialNumber", "CameraSerialNumber")),
    }

    # Pixel size from focal plane resolution (pixels per unit).
    fpx = _rational(pick("FocalPlaneXResolution"))
    unit = pick("FocalPlaneResolutionUnit")
    unit_mm = {2: 25.4, 3: 10.0, 4: 1.0, 5: 0.001}.get(int(unit) if unit else 0)
    if fpx and unit_mm:
        out["pixel_size_um"] = round(unit_mm * 1000.0 / fpx, 4)

    if isinstance(out["iso"], (tuple, list)):
        out["iso"] = out["iso"][0] if out["iso"] else None
    return {k: v for k, v in out.items() if v is not None}


def _jpeg_exif_payload(path: Path) -> bytes | None:
    with path.open("rb") as f:
        head = f.read(256 * 1024)
    if not head.startswith(b"\xff\xd8"):
        return None
    pos = 2
    while pos + 4 <= len(head):
        if head[pos] != 0xFF:
            return None
        marker = head[pos + 1]
        if marker in (0xD9, 0xDA):  # end of image / start of scan
            return None
        length = struct.unpack(">H", head[pos + 2:pos + 4])[0]
        segment = head[pos + 4:pos + 2 + length]
        if marker == 0xE1 and segment.startswith(b"Exif\x00\x00"):
            return segment[6:]
        pos += 2 + length
    return None


def read_source_exif(path) -> dict:
    """Camera EXIF of a source image; {} if there is none or it cannot be read."""
    path = Path(path)
    suffix = path.suffix.lower()
    try:
        if suffix in JPEG_EXTENSIONS:
            payload = _jpeg_exif_payload(path)
            return _exif_from_tiff_stream(io.BytesIO(payload)) if payload else {}
        if suffix in TIFF_BASED_EXTENSIONS:
            with path.open("rb") as f:
                return _exif_from_tiff_stream(f)
    except Exception as exc:  # metadata must never stop processing
        log.debug("No EXIF read from %s: %s", path.name, exc)
    return {}


# ---------------------------------------------------------------------------
# Building the metadata record
# ---------------------------------------------------------------------------

def wavelength_nm(label: str):
    """Numeric centre wavelength from a band label such as '405' or '405nm'."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:nm)?\s*", str(label))
    return float(match.group(1)) if match else None


def camera_info(settings: dict, exif: dict) -> dict:
    """Settings values win; EXIF fills the rest; unknown fields are left out."""
    configured = settings.get("camera") or {}
    info = {}
    for key in ("make", "model", "lens", "focal_length_mm", "pixel_size_um", "serial_number"):
        value = configured.get(key)
        if value in (None, ""):
            value = exif.get(key)
        if value not in (None, ""):
            info[key] = value
    info["sources"] = {
        k: ("settings" if configured.get(k) not in (None, "") else "exif")
        for k in info
    }
    return info


def band_records(settings: dict, band_map: dict, output_order) -> list[dict]:
    fwhm_setting = (settings.get("camera") or {}).get("band_fwhm_nm")
    channel_names = {2: "R", 1: "G", 0: "B"}  # band_map holds OpenCV BGR indices
    records = []
    for i, label in enumerate(output_order, start=1):
        lens, ch = band_map[label]
        wl = wavelength_nm(label)
        if isinstance(fwhm_setting, dict):
            fwhm = fwhm_setting.get(str(label))
        else:
            fwhm = fwhm_setting
        records.append({
            "band": i,
            "name": f"{label} nm" if wl is not None else str(label),
            "label": str(label),
            "center_wavelength_nm": wl,
            "fwhm_nm": float(fwhm) if fwhm not in (None, "") else None,
            "lens": int(lens),
            "sensor_channel": channel_names[ch],
        })
    return records


def build_metadata(*, settings, source_path, exif, band_map, output_order, dtype,
                   output, alignment, split_info, quality=None) -> dict:
    camera = camera_info(settings, exif)

    scale = float(split_info.get("lens_scale", 1.0)) if split_info else 1.0
    if "pixel_size_um" in camera:
        camera["pixel_size_um_output"] = round(float(camera["pixel_size_um"]) * scale, 4)

    acquisition = {
        k: exif[k]
        for k in ("datetime_original", "exposure_time_s", "f_number", "iso")
        if k in exif
    }

    return {
        "msalign_version": __version__,
        "processed": datetime.now().isoformat(timespec="seconds"),
        "source_file": Path(source_path).name if source_path else None,
        "camera": camera,
        "acquisition": acquisition,
        "bands": band_records(settings, band_map, output_order),
        "data_type": str(dtype),
        "output": output,
        "alignment": {
            "mode": alignment.get("mode"),
            "reference_lens": output.get("frame_lens"),
            "lens_shifts_px": {
                str(k): [int(v["dx"]), int(v["dy"])]
                for k, v in sorted(output.get("shifts", {}).items())
            },
            "quality": quality,
        },
        "split": split_info or {},
        "settings_source": settings.get("_source"),
        "raw_decoding": _raw_decoding(source_path, settings),
    }


def _raw_decoding(source_path, settings):
    from .image_io import is_raw_file, raw_decode_params

    if not source_path or not is_raw_file(source_path):
        return None
    params = raw_decode_params(settings.get("raw_demosaic", "linear"))
    params["gamma"] = list(params["gamma"])
    params["decoder"] = "LibRaw via rawpy"
    return params


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def gdal_metadata_xml(meta: dict) -> str:
    items = []

    def item(name, value, sample=None, role=None, domain=None):
        attrs = f' name="{escape(name)}"'
        if sample is not None:
            attrs += f' sample="{sample}"'
        if role:
            attrs += f' role="{role}"'
        if domain:
            attrs += f' domain="{domain}"'
        items.append(f"  <Item{attrs}>{escape(str(value))}</Item>")

    cam = meta["camera"]
    for key, name in (("make", "CAMERA_MAKE"), ("model", "CAMERA_MODEL"), ("lens", "LENS"),
                      ("focal_length_mm", "FOCAL_LENGTH_MM"), ("pixel_size_um", "PIXEL_SIZE_UM"),
                      ("pixel_size_um_output", "PIXEL_SIZE_UM_OUTPUT")):
        if key in cam:
            item(name, cam[key])
    if meta["acquisition"].get("datetime_original"):
        item("ACQUISITION_DATETIME", meta["acquisition"]["datetime_original"])
    item("wavelength_units", "nm")
    item("SOFTWARE", f"msalign {meta['msalign_version']}")

    for band in meta["bands"]:
        s = band["band"] - 1
        item("DESCRIPTION", band["name"], sample=s, role="description")
        item("LENS", band["lens"], sample=s)
        item("SENSOR_CHANNEL", band["sensor_channel"], sample=s)
        if band["center_wavelength_nm"] is not None:
            wl = band["center_wavelength_nm"]
            item("wavelength", f"{wl:g}", sample=s)
            item("CENTRAL_WAVELENGTH_UM", f"{wl / 1000:.4f}", sample=s, domain="IMAGERY")
        if band["fwhm_nm"] is not None:
            item("fwhm", f"{band['fwhm_nm']:g}", sample=s)
            item("FWHM_UM", f"{band['fwhm_nm'] / 1000:.4f}", sample=s, domain="IMAGERY")

    return "<GDALMetadata>\n" + "\n".join(items) + "\n</GDALMetadata>"


def _rational_tag(value: float):
    frac = Fraction(value).limit_denominator(100000)
    return (frac.numerator, frac.denominator)


def tiff_tags(meta: dict) -> dict:
    """
    Arguments for tifffile: description, software, datetime and extratags.
    """
    cam = meta["camera"]
    extratags = []
    if "make" in cam:
        extratags.append((271, "s", 0, str(cam["make"]), True))
    if "model" in cam:
        extratags.append((272, "s", 0, str(cam["model"]), True))
    if "focal_length_mm" in cam:
        extratags.append((37386, "2I", 1, _rational_tag(float(cam["focal_length_mm"])), True))
    pixel = cam.get("pixel_size_um_output")
    if pixel:
        per_cm = _rational_tag(10000.0 / float(pixel))
        extratags += [
            (37390, "2I", 1, per_cm, True),  # FocalPlaneXResolution
            (37391, "2I", 1, per_cm, True),  # FocalPlaneYResolution
            (37392, "H", 1, UNIT_CM, True),  # FocalPlaneResolutionUnit
        ]
    extratags.append((42112, "s", 0, gdal_metadata_xml(meta), True))
    if meta["output"].get("padded_pixels"):
        extratags.append((42113, "s", 0, "0", True))  # GDAL_NODATA

    acquired = meta["acquisition"].get("datetime_original")
    return {
        "description": json.dumps(meta, ensure_ascii=False, separators=(",", ":")),
        "software": f"msalign {meta['msalign_version']}",
        "datetime": acquired if acquired and re.fullmatch(r"\d{4}:\d\d:\d\d \d\d:\d\d:\d\d", acquired) else None,
        "extratags": extratags,
    }


def write_sidecar(output_tiff: Path, meta: dict) -> Path:
    path = output_tiff.with_name(output_tiff.stem + "_metadata.json")
    path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# EXIF sub-IFD
# ---------------------------------------------------------------------------
#
# Photogrammetry software (e.g. Agisoft Metashape) reads focal length, pixel
# size and exposure from the standard EXIF block (a sub-IFD referenced by tag
# 34665), as cameras and Agrowing's own software write it - not from the same
# values placed directly in the main image directory. tifffile cannot write an
# EXIF sub-IFD (it refuses tag 34665), so the TIFF is written with a
# placeholder tag 34664 (unassigned, sorts directly before 34665); the EXIF
# directory is then appended to the file and the placeholder entry is turned
# into a 34665 pointer to it.

_EXIF_PLACEHOLDER_CODE = 34664
EXIF_PLACEHOLDER = (_EXIF_PLACEHOLDER_CODE, "I", 1, 0, True)

_ASCII, _SHORT, _LONG, _RATIONAL, _UNDEFINED = 2, 3, 4, 5, 7


def exif_entries(meta: dict) -> list:
    """[(tag, type, count, value)] for the EXIF directory, sorted by tag."""
    cam, acq = meta.get("camera", {}), meta.get("acquisition", {})
    entries = [(36864, _UNDEFINED, 4, b"0232")]  # ExifVersion
    if acq.get("exposure_time_s"):
        entries.append((33434, _RATIONAL, 1, _rational_tag(float(acq["exposure_time_s"]))))
    if acq.get("f_number"):
        entries.append((33437, _RATIONAL, 1, _rational_tag(float(acq["f_number"]))))
    if acq.get("iso"):
        entries.append((34855, _SHORT, 1, int(acq["iso"])))
    dto = acq.get("datetime_original")
    if dto and re.fullmatch(r"\d{4}:\d\d:\d\d \d\d:\d\d:\d\d", str(dto)):
        entries.append((36867, _ASCII, 20, str(dto).encode() + b"\x00"))
    if cam.get("focal_length_mm"):
        entries.append((37386, _RATIONAL, 1, _rational_tag(float(cam["focal_length_mm"]))))
    pixel = cam.get("pixel_size_um_output") or cam.get("pixel_size_um")
    if pixel:
        per_cm = _rational_tag(10000.0 / float(pixel))
        entries += [(41486, _RATIONAL, 1, per_cm), (41487, _RATIONAL, 1, per_cm), (41488, _SHORT, 1, 3)]
    if cam.get("lens"):
        text = str(cam["lens"]).encode("ascii", "replace") + b"\x00"
        entries.append((42036, _ASCII, len(text), text))
    return sorted(entries)


def append_exif_ifd(path, meta: dict) -> bool:
    """
    Append an EXIF directory to the TIFF at `path` and turn the placeholder
    tag on page 1 into an ExifIFD (34665) pointer to it. Returns False if
    there is no placeholder.
    """
    import tifffile

    path = Path(path)
    with tifffile.TiffFile(str(path)) as tif:
        if tif.is_bigtiff:
            raise ValueError("EXIF directory: BigTIFF is not supported")
        tag = tif.pages[0].tags.get(_EXIF_PLACEHOLDER_CODE)
        if tag is None or 34665 in tif.pages[0].tags:
            return False
        entry_offset, value_offset = tag.offset, tag.valueoffset
        bo = "<" if tif.byteorder == "<" else ">"

    entries = exif_entries(meta)
    with path.open("r+b") as f:
        f.seek(0, 2)
        start = f.tell()
        start += start % 2  # word aligned
        n = len(entries)
        data_at = start + 2 + 12 * n + 4
        ifd = bytearray(struct.pack(bo + "H", n))
        extra = bytearray()
        for code, typ, count, value in entries:
            if typ == _RATIONAL:
                raw = struct.pack(bo + "II", int(value[0]), int(value[1]))
            elif typ == _SHORT:
                raw = struct.pack(bo + "H", int(value)) + b"\x00\x00"
            elif typ == _LONG:
                raw = struct.pack(bo + "I", int(value))
            else:  # ASCII / UNDEFINED
                raw = bytes(value)
            if len(raw) <= 4:
                ifd += struct.pack(bo + "HHI", code, typ, count) + raw.ljust(4, b"\x00")
            else:
                ifd += struct.pack(bo + "HHII", code, typ, count, data_at + len(extra))
                extra += raw + (b"\x00" if len(raw) % 2 else b"")
        ifd += struct.pack(bo + "I", 0)
        f.seek(0, 2)
        if f.tell() % 2:
            f.write(b"\x00")
        f.write(bytes(ifd) + bytes(extra))
        f.seek(entry_offset)
        f.write(struct.pack(bo + "H", 34665))
        f.seek(value_offset)
        f.write(struct.pack(bo + "I", start))
    return True
