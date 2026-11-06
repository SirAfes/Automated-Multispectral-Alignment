"""Standards-based GDAL band metadata for the aligned multispectral TIFF."""

from __future__ import annotations

from xml.etree.ElementTree import Element, SubElement, tostring


DEFAULT_WAVELENGTHS_NM = (
    405, 430, 450, 490, 525, 550, 560,
    570, 630, 650, 685, 710, 735, 850,
)


def gdal_band_metadata_extratags(wavelengths_nm: list[int] | tuple[int, ...]):
    """Return a TIFF GDALMetadata tag containing per-sample wavelength labels.

    This records nominal filter wavelengths, not calibrated reflectance,
    focal length, sensor pixel pitch, or a photogrammetric camera model.
    """
    if not wavelengths_nm or len(set(wavelengths_nm)) != len(wavelengths_nm):
        raise ValueError("Wavelengths must be nonempty and unique.")

    root = Element("GDALMetadata")
    SubElement(root, "Item", name="BAND_ORDER_SOURCE").text = (
        "Automated Multispectral Alignment project configuration"
    )
    SubElement(root, "Item", name="IMAGE_TYPE").text = "Aligned multispectral intensity"
    SubElement(root, "Item", name="RADIOMETRIC_CALIBRATION").text = "not_applied"
    SubElement(root, "Item", name="INTRINSIC_CALIBRATION").text = "not_provided"

    for index, wavelength in enumerate(wavelengths_nm):
        value = str(int(wavelength))
        SubElement(
            root,
            "Item",
            name="DESCRIPTION",
            sample=str(index),
            role="description",
        ).text = f"{value} nm"
        SubElement(root, "Item", name="WAVELENGTH", sample=str(index)).text = value
        SubElement(root, "Item", name="WAVELENGTH_UNITS", sample=str(index)).text = "nm"

    xml = tostring(root, encoding="unicode")
    return [(42112, "s", 0, xml, False)]
