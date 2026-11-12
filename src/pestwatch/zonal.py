"""Zonal statistics of a raster over polygons, with GDAL and numpy."""
import math

import numpy as np
from osgeo import gdal, ogr, osr

from . import conf
from .raster import srs_4326

gdal.UseExceptions()


def bin_label(lo, hi):
    return f"{lo:.1f}-{hi:.1f}"


def distribution_keys(product_type):
    """Keys of ``ZonalStatistic.distribution`` in display order (jsonb does not keep order)."""
    if product_type == "classified":
        return [str(k) for k in sorted(conf.get("CLASSES"))]
    edges = conf.get("HYBRID_BINS")
    return [bin_label(lo, hi) for lo, hi in zip(edges[:-1], edges[1:])]


def _empty(pixel_count=0):
    return {"pixel_count": pixel_count, "valid_count": 0, "mean": None, "minimum": None,
            "maximum": None, "std": None, "distribution": {}}


def _summarise(values, product_type, pixel_count):
    if values.size == 0:
        return _empty(pixel_count)
    values = values.astype("float64")
    if product_type == "classified":
        dist = {str(k): 0 for k in conf.get("CLASSES")}
        uniq, counts = np.unique(values.astype("int64"), return_counts=True)
        for u, c in zip(uniq.tolist(), counts.tolist()):
            if str(u) in dist:
                dist[str(u)] = int(c)
    else:
        edges = conf.get("HYBRID_BINS")
        counts, _ = np.histogram(np.clip(values, edges[0], edges[-1]), bins=edges)
        dist = {bin_label(lo, hi): int(c) for lo, hi, c in zip(edges[:-1], edges[1:], counts.tolist())}
    return {
        "pixel_count": pixel_count,
        "valid_count": int(values.size),
        "mean": float(values.mean()),
        "minimum": float(values.min()),
        "maximum": float(values.max()),
        "std": float(values.std()),
        "distribution": dist,
    }


def _rasterize(geom, srs, window_gt, w, h, all_touched):
    mem = gdal.GetDriverByName("MEM").Create("", w, h, 1, gdal.GDT_Byte)
    mem.SetGeoTransform(window_gt)
    mem.SetSpatialRef(srs)
    vds = ogr.GetDriverByName("Memory").CreateDataSource("")
    lyr = vds.CreateLayer("zone", srs=srs, geom_type=ogr.wkbMultiPolygon)
    feat = ogr.Feature(lyr.GetLayerDefn())
    feat.SetGeometry(geom)
    lyr.CreateFeature(feat)
    opts = ["ALL_TOUCHED=TRUE"] if all_touched else []
    gdal.RasterizeLayer(mem, [1], lyr, burn_values=[1], options=opts)
    return mem.GetRasterBand(1).ReadAsArray().astype(bool)


def zonal_stats(raster_path, zones, product_type):
    """Compute statistics for each zone.

    ``zones`` is an iterable of (key, wkb) with geometries in EPSG:4326.
    Returns {key: stats_dict}.
    """
    ds = gdal.Open(raster_path)
    band = ds.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    gt = ds.GetGeoTransform()
    srs = ds.GetSpatialRef()
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    to_raster = osr.CoordinateTransformation(srs_4326(), srs)

    results = {}
    for key, wkb in zones:
        geom = ogr.CreateGeometryFromWkb(bytes(wkb))
        geom.Transform(to_raster)
        minx, maxx, miny, maxy = geom.GetEnvelope()

        x0 = max(0, int(math.floor((minx - gt[0]) / gt[1])))
        x1 = min(ds.RasterXSize, int(math.ceil((maxx - gt[0]) / gt[1])))
        y0 = max(0, int(math.floor((maxy - gt[3]) / gt[5])))
        y1 = min(ds.RasterYSize, int(math.ceil((miny - gt[3]) / gt[5])))
        if x1 <= x0 or y1 <= y0:
            results[key] = _empty()
            continue
        w, h = x1 - x0, y1 - y0
        window_gt = (gt[0] + x0 * gt[1], gt[1], 0.0, gt[3] + y0 * gt[5], 0.0, gt[5])

        mask = _rasterize(geom, srs, window_gt, w, h, all_touched=False)
        if not mask.any():
            # Zone smaller than a pixel: take the pixels it touches.
            mask = _rasterize(geom, srs, window_gt, w, h, all_touched=True)

        arr = band.ReadAsArray(x0, y0, w, h)
        valid = mask.copy()
        if arr.dtype.kind == "f":
            valid &= np.isfinite(arr)
        if nodata is not None:
            valid &= arr != nodata
        results[key] = _summarise(arr[valid], product_type, int(mask.sum()))
    return results
