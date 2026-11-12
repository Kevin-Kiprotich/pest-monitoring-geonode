"""Tests for raster validation, the county check and zonal statistics.

These use only GDAL and GEOS (SimpleTestCase), so they need no database.
"""
import os
import shutil
import tempfile

import numpy as np
from django.contrib.gis.geos import GEOSGeometry, Polygon
from django.test import SimpleTestCase
from osgeo import gdal, osr

from pestwatch.raster import (RasterError, check_county, county_fit, inspect_raster, validate_product,
                              values_to_nodata)
from pestwatch.zonal import distribution_keys, zonal_stats

gdal.UseExceptions()


def write_tif(path, array, bbox=(36.0, -1.0, 37.0, 0.0), epsg=4326, nodata=-9999, bands=1):
    """Write ``array`` as a GeoTIFF covering ``bbox`` (minx, miny, maxx, maxy)."""
    rows, cols = array.shape
    dtype = gdal.GDT_Float32 if array.dtype.kind == "f" else gdal.GDT_Byte if array.dtype == np.uint8 else gdal.GDT_Int16
    ds = gdal.GetDriverByName("GTiff").Create(path, cols, rows, bands, dtype)
    minx, miny, maxx, maxy = bbox
    ds.SetGeoTransform((minx, (maxx - minx) / cols, 0, maxy, 0, -(maxy - miny) / rows))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    for b in range(1, bands + 1):
        band = ds.GetRasterBand(b)
        if nodata is not None:
            band.SetNoDataValue(nodata)
        band.WriteArray(array)
    ds.FlushCache()
    ds = None
    return path


def box(minx, miny, maxx, maxy):
    p = Polygon.from_bbox((minx, miny, maxx, maxy))
    p.srid = 4326
    return p


class FakeCounty:
    def __init__(self, pk, name, geom):
        self.pk, self.name, self.geom = pk, name, geom


class RasterTestCase(SimpleTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def path(self, name):
        return os.path.join(self.dir, name)


class InspectRasterTests(RasterTestCase):
    def test_valid_hybrid(self):
        arr = np.linspace(0, 1, 100, dtype="float32").reshape(10, 10)
        arr[0, 0] = -9999
        info = inspect_raster(write_tif(self.path("h.tif"), arr))
        validate_product(info, "hybrid")
        self.assertAlmostEqual(info.maximum, 1.0, places=5)
        self.assertGreaterEqual(info.minimum, 0.0)
        self.assertEqual(GEOSGeometry(info.footprint_wkt).extent, (36.0, -1.0, 37.0, 0.0))

    def test_hybrid_out_of_range(self):
        arr = np.full((10, 10), 1.5, dtype="float32")
        info = inspect_raster(write_tif(self.path("h.tif"), arr))
        with self.assertRaisesRegex(RasterError, "between 0 and 1"):
            validate_product(info, "hybrid")

    def test_valid_classified(self):
        arr = np.tile(np.arange(1, 6, dtype="int16"), (5, 2))
        info = inspect_raster(write_tif(self.path("c.tif"), arr))
        validate_product(info, "classified")
        self.assertEqual(info.values, [1, 2, 3, 4, 5])

    def test_classified_unknown_value(self):
        arr = np.full((10, 10), 7, dtype="int16")
        info = inspect_raster(write_tif(self.path("c.tif"), arr))
        with self.assertRaisesRegex(RasterError, "found 7"):
            validate_product(info, "classified")

    def test_classified_accepts_no_host_zero(self):
        arr = np.tile(np.arange(0, 6, dtype="uint8"), (5, 2))
        arr[0, 0] = 255
        info = inspect_raster(write_tif(self.path("c.tif"), arr, nodata=255))
        validate_product(info, "classified")
        self.assertEqual(info.values, [0, 1, 2, 3, 4, 5])

    def test_classified_only_no_host(self):
        arr = np.zeros((10, 10), dtype="uint8")
        info = inspect_raster(write_tif(self.path("c.tif"), arr, nodata=255))
        with self.assertRaisesRegex(RasterError, "no class values"):
            validate_product(info, "classified")

    def test_values_to_nodata(self):
        arr = np.tile(np.arange(0, 6, dtype="uint8"), (5, 2))
        src = write_tif(self.path("c.tif"), arr, nodata=255)
        out = values_to_nodata(src, self.path("m.tif"), [0])
        info = inspect_raster(out)
        self.assertEqual(info.values, [1, 2, 3, 4, 5])
        self.assertEqual(info.nodata, 255)

    def test_classified_must_be_integer(self):
        arr = np.full((10, 10), 2.0, dtype="float32")
        info = inspect_raster(write_tif(self.path("c.tif"), arr))
        with self.assertRaisesRegex(RasterError, "integer"):
            validate_product(info, "classified")

    def test_requires_nodata(self):
        arr = np.zeros((10, 10), dtype="float32")
        with self.assertRaisesRegex(RasterError, "nodata"):
            inspect_raster(write_tif(self.path("n.tif"), arr, nodata=None))

    def test_single_band_only(self):
        arr = np.zeros((10, 10), dtype="float32")
        with self.assertRaisesRegex(RasterError, "single-band"):
            inspect_raster(write_tif(self.path("b.tif"), arr, bands=2))

    def test_all_nodata(self):
        arr = np.full((10, 10), -9999, dtype="float32")
        with self.assertRaisesRegex(RasterError, "only nodata"):
            inspect_raster(write_tif(self.path("e.tif"), arr))

    def test_not_a_raster(self):
        path = self.path("x.tif")
        with open(path, "w") as f:
            f.write("not a tiff")
        with self.assertRaisesRegex(RasterError, "could not be read"):
            inspect_raster(path)

    def test_projected_footprint_is_in_4326(self):
        # UTM 37S around Nairobi.
        arr = np.full((10, 10), 0.5, dtype="float32")
        info = inspect_raster(write_tif(self.path("u.tif"), arr, bbox=(250000, 9850000, 260000, 9860000), epsg=32737))
        minx, miny, maxx, maxy = GEOSGeometry(info.footprint_wkt).extent
        self.assertTrue(36 < minx < maxx < 37.5 and -2 < miny < maxy < 0, (minx, miny, maxx, maxy))


class CountyCheckTests(SimpleTestCase):
    county = FakeCounty(1, "Alpha", box(36.0, -1.0, 37.0, 0.0))
    other = FakeCounty(2, "Beta", box(38.0, -1.0, 39.0, 0.0))

    def test_matching_extent(self):
        self.assertEqual(county_fit(box(35.99, -1.01, 37.01, 0.01), self.county.geom)[0], 1.0)
        check_county(box(35.99, -1.01, 37.01, 0.01), self.county)

    def test_raster_for_another_county(self):
        with self.assertRaisesRegex(RasterError, "looks like a raster for Beta"):
            check_county(box(38.0, -1.0, 39.0, 0.0), self.county, [self.county, self.other])

    def test_raster_covering_too_little(self):
        with self.assertRaisesRegex(RasterError, "covers 50%"):
            check_county(box(36.0, -1.0, 36.5, 0.0), self.county)

    def test_raster_much_larger_than_county(self):
        with self.assertRaises(RasterError):
            check_county(box(34.0, -3.0, 39.0, 2.0), self.county)


class ZonalStatsTests(RasterTestCase):
    def test_classified_halves(self):
        arr = np.ones((10, 10), dtype="int16")
        arr[:, 5:] = 5
        path = write_tif(self.path("c.tif"), arr)
        zones = [("left", box(36.0, -1.0, 36.5, 0.0).wkb), ("all", box(36.0, -1.0, 37.0, 0.0).wkb),
                 ("outside", box(40.0, -1.0, 41.0, 0.0).wkb)]
        out = zonal_stats(path, zones, "classified")
        self.assertEqual(out["left"]["distribution"]["1"], 50)
        self.assertEqual(out["left"]["distribution"]["5"], 0)
        self.assertEqual(out["left"]["mean"], 1.0)
        self.assertEqual(out["all"]["mean"], 3.0)
        self.assertEqual(out["all"]["valid_count"], 100)
        self.assertEqual(out["outside"]["valid_count"], 0)
        self.assertIsNone(out["outside"]["mean"])

    def test_hybrid_bins_and_nodata(self):
        arr = np.full((10, 10), 0.9, dtype="float32")
        arr[:5, :] = 0.1
        arr[0, :] = -9999
        out = zonal_stats(write_tif(self.path("h.tif"), arr), [("all", box(36.0, -1.0, 37.0, 0.0).wkb)], "hybrid")["all"]
        self.assertEqual(out["pixel_count"], 100)
        self.assertEqual(out["valid_count"], 90)
        self.assertEqual(out["distribution"]["0.0-0.2"], 40)
        self.assertEqual(out["distribution"]["0.8-1.0"], 50)
        self.assertEqual(list(out["distribution"]), distribution_keys("hybrid"))

    def test_zone_smaller_than_a_pixel(self):
        arr = np.full((10, 10), 3, dtype="int16")
        out = zonal_stats(write_tif(self.path("c.tif"), arr), [("tiny", box(36.01, -0.99, 36.02, -0.98).wkb)],
                          "classified")["tiny"]
        self.assertGreaterEqual(out["valid_count"], 1)
        self.assertEqual(out["mean"], 3.0)

    def test_projected_raster(self):
        arr = np.full((100, 100), 4, dtype="int16")
        path = write_tif(self.path("u.tif"), arr, bbox=(250000, 9850000, 260000, 9860000), epsg=32737)
        footprint = GEOSGeometry(inspect_raster(path).footprint_wkt, srid=4326)
        out = zonal_stats(path, [("z", footprint.buffer(-0.01).wkb)], "classified")["z"]
        self.assertGreater(out["valid_count"], 5000)
        self.assertEqual(out["distribution"]["4"], out["valid_count"])
