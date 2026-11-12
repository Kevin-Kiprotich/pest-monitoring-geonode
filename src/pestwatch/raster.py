"""Inspect and validate uploaded rasters with GDAL."""
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
from osgeo import gdal, ogr, osr

from . import conf

gdal.UseExceptions()
ogr.UseExceptions()
osr.UseExceptions()


class RasterError(ValueError):
    """The raster cannot be used. The message is shown to the uploader."""


@dataclass
class RasterInfo:
    driver: str
    width: int
    height: int
    band_count: int
    data_type: str
    is_integer: bool
    srs_wkt: str
    nodata: Optional[float]
    geotransform: tuple
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    values: List[int] = field(default_factory=list)
    footprint_wkt: str = ""


def srs_4326():
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def _footprint(ds, srs):
    """Raster extent as a polygon in EPSG:4326, densified so it survives reprojection."""
    gt = ds.GetGeoTransform()
    minx, maxy = gt[0], gt[3]
    maxx = minx + gt[1] * ds.RasterXSize
    miny = maxy + gt[5] * ds.RasterYSize
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)):
        ring.AddPoint_2D(x, y)
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    poly.Segmentize(max(abs(maxx - minx), abs(maxy - miny)) / 20.0)
    poly.Transform(osr.CoordinateTransformation(srs, srs_4326()))
    return poly.ExportToWkt()


def _iter_blocks(band, rows=512):
    for y in range(0, band.YSize, rows):
        yield band.ReadAsArray(0, y, band.XSize, min(rows, band.YSize - y))


def _valid(arr, nodata):
    mask = np.isfinite(arr) if arr.dtype.kind == "f" else np.ones(arr.shape, dtype=bool)
    if nodata is not None:
        mask &= arr != nodata
    return arr[mask]


def inspect_raster(path, read_values=True) -> RasterInfo:
    try:
        ds = gdal.Open(path)
    except RuntimeError:
        ds = None
    if ds is None:
        raise RasterError("The file could not be read as a raster.")

    driver = ds.GetDriver().ShortName
    if driver != "GTiff":
        raise RasterError(f"Expected a GeoTIFF, got {driver}.")
    if ds.RasterCount != 1:
        raise RasterError(f"Expected a single-band raster, got {ds.RasterCount} bands.")
    gt = ds.GetGeoTransform(can_return_null=True)
    if gt is None:
        raise RasterError("The raster has no georeferencing.")
    if gt[2] != 0 or gt[4] != 0:
        raise RasterError("Rotated rasters are not supported. Warp it to a north-up grid first.")
    srs = ds.GetSpatialRef()
    if srs is None:
        raise RasterError("The raster has no coordinate reference system.")
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    band = ds.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    if nodata is None:
        raise RasterError("The raster has no nodata value. Set one, e.g. `gdal_edit.py -a_nodata -9999 file.tif`.")

    data_type = gdal.GetDataTypeName(band.DataType)
    info = RasterInfo(
        driver=driver,
        width=ds.RasterXSize,
        height=ds.RasterYSize,
        band_count=ds.RasterCount,
        data_type=data_type,
        is_integer=data_type in {"Byte", "Int8", "UInt16", "Int16", "UInt32", "Int32", "UInt64", "Int64"},
        srs_wkt=srs.ExportToWkt(),
        nodata=nodata,
        geotransform=gt,
        footprint_wkt=_footprint(ds, srs),
    )

    if read_values:
        lo, hi, values = None, None, set()
        for block in _iter_blocks(band):
            valid = _valid(block, nodata)
            if valid.size == 0:
                continue
            lo = float(valid.min()) if lo is None else min(lo, float(valid.min()))
            hi = float(valid.max()) if hi is None else max(hi, float(valid.max()))
            # Only collect distinct values for integer rasters, and stop early
            # if there are clearly too many to be classes.
            if info.is_integer and len(values) <= 256:
                values.update(np.unique(valid).tolist())
        if lo is None:
            raise RasterError("The raster contains only nodata.")
        info.minimum, info.maximum = lo, hi
        info.values = sorted(int(v) for v in values)
    return info


def validate_product(info: RasterInfo, product_type):
    """Check the pixel values make sense for the product type."""
    if product_type == "hybrid":
        tol = 1e-6
        if info.minimum < -tol or info.maximum > 1 + tol:
            raise RasterError(
                f"Hybrid rasters must have values between 0 and 1; this one ranges "
                f"from {info.minimum:g} to {info.maximum:g}.")
    elif product_type == "classified":
        classes = set(conf.get("CLASSES"))
        ignored = set(conf.get("NO_DATA_CLASSES"))
        if not info.is_integer:
            raise RasterError(f"Classified rasters must use an integer data type, not {info.data_type}.")
        extra = sorted(set(info.values) - classes - ignored)
        if extra:
            shown = ", ".join(str(v) for v in extra[:10])
            allowed = f"the class values {sorted(classes)}"
            if ignored:
                allowed += f" (and {', '.join(str(v) for v in sorted(ignored))} for no data)"
            raise RasterError(
                f"Classified rasters may only contain {allowed}; "
                f"found {shown}{'…' if len(extra) > 10 else ''}.")
        if not set(info.values) & classes:
            raise RasterError("The raster contains no class values, only no-data values.")
    else:
        raise RasterError(f"Unknown product type {product_type!r}.")


def values_to_nodata(src, dst, values):
    """Copy ``src`` to ``dst`` (GeoTIFF) with every pixel equal to one of ``values`` set to nodata."""
    gdal.Translate(dst, src, format="GTiff", creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"])
    ds = gdal.Open(dst, gdal.GA_Update)
    band = ds.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    values = [v for v in values if v != nodata]
    rows = 512
    for y in range(0, band.YSize, rows):
        n = min(rows, band.YSize - y)
        block = band.ReadAsArray(0, y, band.XSize, n)
        hit = np.isin(block, values)
        if hit.any():
            block[hit] = nodata
            band.WriteArray(block, 0, y)
    band.FlushCache()
    ds = None
    return dst


def county_fit(footprint, county_geom):
    """Return (coverage, inside) for a raster footprint and a county geometry.

    coverage: share of the county covered by the raster extent.
    inside:   share of the raster extent within the county's (slightly grown) bounding box.
    Both arguments are GEOS geometries in EPSG:4326.
    """
    if county_geom.area == 0 or footprint.area == 0:
        return 0.0, 0.0
    coverage = county_geom.intersection(footprint).area / county_geom.area
    margin = conf.get("COUNTY_BBOX_MARGIN")
    minx, miny, maxx, maxy = county_geom.extent
    from django.contrib.gis.geos import Polygon
    box = Polygon.from_bbox((minx - margin, miny - margin, maxx + margin, maxy + margin))
    box.srid = 4326
    inside = box.intersection(footprint).area / footprint.area
    return coverage, inside


def check_county(footprint, county, other_counties=()):
    """Raise RasterError unless the raster extent matches ``county``.

    ``other_counties`` are checked to suggest the right county in the message.
    """
    coverage, inside = county_fit(footprint, county.geom)
    if coverage >= conf.get("COUNTY_MIN_COVERAGE") and inside >= conf.get("COUNTY_MIN_INSIDE"):
        return
    best = None
    for other in other_counties:
        if other.pk == county.pk:
            continue
        c, i = county_fit(footprint, other.geom)
        if c >= conf.get("COUNTY_MIN_COVERAGE") and i >= conf.get("COUNTY_MIN_INSIDE"):
            best = other
            break
    msg = (f"The raster does not match {county.name}: it covers {coverage:.0%} of the county, "
           f"and {inside:.0%} of the raster lies within the county's extent.")
    if best:
        msg += f" It looks like a raster for {best.name}."
    raise RasterError(msg)
