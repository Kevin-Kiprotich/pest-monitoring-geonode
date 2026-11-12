"""Monthly satellite composites from Google Earth Engine, made the way the modelling team made them.

The stored models were trained on composites from four recipes (the team's notebooks are
in the repository's ``models/`` folder). Each product below follows its recipe exactly:

``s2_stack``   Sentinel-2 band stack (sentinel2_monthly_*.ipynb) and the coastal / water
               vapour stack (sent2_C_WV_*.ipynb): scenes with CLOUDY_PIXEL_PERCENTAGE <= 60,
               Cloud Score+ mask (cs_cdf >= 0.60), monthly median, gaps filled from +/-1 then
               +/-2 months, ``fill_flag`` band (0 / 1 / 2 / 255). int16, UTM.
``median_qa``  MCARI and NDMI (*_monthly_*_fast.ipynb): SCL + s2cloudless mask (probability
               > 40, dilated 40 m), index per scene averaged to 30 m, monthly median, gaps
               filled from +/-30 days, QA band (0 / 2 / 255). float32, nodata -9999, UTM.
``monthly_mean`` CVI, MTVI2, NDRE, NDVI, PRI, PSRI, RDVI, SAVI and the Landsat stack (the
               *TOP10.ipynb notebooks): SCL mask (Landsat: QA_PIXEL), index per scene,
               monthly mean, empty pixels filled from the nearest of up to 6 months within
               +/-3 months, then grown in from neighbouring pixels. float32, NaN, EPSG:4326.

Only new months are made here: months before ``conf.AUTO_FETCH_FROM`` are the modelling
team's history, which is uploaded (Input data) or copied onto the server, never fetched.

Files are written where the models read them (``layout.Layout``) and tagged
``PESTWATCH_SOURCE=earthengine``. A file without that tag (a manager's upload, or the
team's own history copied onto the server) is never replaced.

Only the standard library is imported at module level, so Django can import the product
list; Earth Engine, numpy, rasterio and shapely are imported where they are used.
"""
import datetime as dt
import io
import json
import math
import os
import random
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

SOURCE_TAG = "PESTWATCH_SOURCE"
EE_SOURCE = "earthengine"
UPLOAD_SOURCE = "upload"

HIGH_VOLUME_URL = "https://earthengine-highvolume.googleapis.com"
METRES_PER_DEGREE = 111319.49079327357        # what Earth Engine uses for a metric scale in EPSG:4326

S2_STACK_BANDS = ["B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B11", "B12"]
CWV_BANDS = ["B1", "B9"]
MEAN_S2_BANDS = ["B2", "B3", "B4", "B5", "B6", "B8", "B8A", "B11"]
LANDSAT_BANDS = ["COASTAL", "BLUE", "LST_K"]

# recipe settings, as in the notebooks
S2_STACK = dict(max_cloud=60, cs_threshold=0.60, fill_windows=(1, 2))
MEDIAN_QA = dict(max_cloud=60, cloud_prob_max=40, cloud_buffer_m=40, fill_days=30, scale=30, nodata=-9999.0,
                 min_valid_fraction=0.05)
MONTHLY_MEAN = dict(s2_cloud_max=60, landsat_cloud_max=60, scale=30, window_months=3, max_neighbours=6,
                    spatial_iter=10, spatial_radius_px=2, coverage_scale=300, min_valid_fraction=0.05)


@dataclass
class Product:
    key: str                 # the Input data key (inputs.DATASETS)
    recipe: str              # s2_stack | median_qa | monthly_mean
    bands: list              # band names written to the file
    scale: int = 30
    tile_px: int = 512
    sensor: str = "S2"
    index: str = ""          # vegetation index name, for index products

    def path(self, L, county, year, month):
        if self.key == "s2_bands":
            return L.s2_file(county, year, month)
        if self.key == "s2_cwv":
            return L.cwv_file(county, year, month)
        if self.key == "landsat":
            return L.landsat_file(county, year, month)
        return L.index_file(self.index, county, year, month)


MEAN_INDICES = ["CVI", "MTVI2", "NDRE", "NDVI", "PRI", "PSRI", "RDVI", "SAVI"]
MEDIAN_INDICES = ["MCARI", "NDMI"]


def products(ndmi_recipe="median_qa"):
    """Every product, keyed like the Input data datasets. ``ndmi_recipe`` picks NDMI's recipe."""
    out = [
        Product("s2_bands", "s2_stack", S2_STACK_BANDS + ["fill_flag"], scale=10, tile_px=1024),
        Product("s2_cwv", "s2_stack", CWV_BANDS + ["fill_flag"], scale=60, tile_px=512),
        Product("landsat", "monthly_mean", LANDSAT_BANDS, sensor="LANDSAT"),
    ]
    for ix in MEDIAN_INDICES:
        recipe = ndmi_recipe if ix == "NDMI" else "median_qa"
        bands = [ix, "QA"] if recipe == "median_qa" else [ix]
        out.append(Product(f"index_{ix}", recipe, bands, index=ix))
    for ix in MEAN_INDICES:
        out.append(Product(f"index_{ix}", "monthly_mean", [ix], index=ix))
    return {p.key: p for p in out}


PRODUCT_KEYS = list(products())
STEP_PRODUCTS = {
    "fetch_s2_bands": ["s2_bands"],
    "fetch_s2_cwv": ["s2_cwv"],
    "fetch_landsat": ["landsat"],
    "fetch_indices": [f"index_{ix}" for ix in MEDIAN_INDICES + MEAN_INDICES],
}


# --- months, grids and the overwrite rule (pure, tested) ----------------------------------

def month_add(ym, n):
    y, m = map(int, ym.split("-"))
    k = y * 12 + (m - 1) + n
    return f"{k // 12:04d}-{k % 12 + 1:02d}"


def month_gap(a, b):
    ya, ma = map(int, a.split("-"))
    yb, mb = map(int, b.split("-"))
    return abs((ya - yb) * 12 + (ma - mb))


def month_span(first, last):
    out, ym = [], first
    while ym <= last:
        out.append(ym)
        ym = month_add(ym, 1)
    return out


def donor_months(month, months_with_scenes, window=3, max_n=6):
    """The TOP10 notebooks' donors: other months with scenes within +/-window, nearest first (ties: earlier)."""
    cands = [o for o in months_with_scenes if o != month and month_gap(o, month) <= window]
    return sorted(cands, key=lambda o: (month_gap(o, month), o))[:max_n]


def utm_epsg(lon, lat):
    return (32600 if lat >= 0 else 32700) + int((lon + 180) // 6) + 1


def grid(bounds, step):
    """Pixel grid covering ``bounds`` (minx, miny, maxx, maxy) with its corner on a multiple of ``step``."""
    minx, miny, maxx, maxy = bounds
    x0 = math.floor(minx / step) * step
    y0 = math.ceil(maxy / step) * step
    return x0, y0, math.ceil((maxx - x0) / step - 1e-9), math.ceil((y0 - miny) / step - 1e-9)


def windows(width, height, tile_px):
    return [(r, c, min(tile_px, height - r), min(tile_px, width - c))
            for r in range(0, height, tile_px) for c in range(0, width, tile_px)]


def split_history(months, fetch_from):
    """(months to fetch, history months) for ``fetch_from`` ("YYYY-MM-DD" or "YYYY-MM"): history is uploaded."""
    first = (fetch_from or "")[:7]
    return [m for m in months if m >= first], [m for m in months if m < first]


def decide(exists, source, refresh, force):
    """Write a product file? Files not made by this module (uploads, the team's history) are kept."""
    if not exists:
        return True
    return source == EE_SOURCE and (refresh or force)


def file_source(path):
    try:
        import rasterio
        with rasterio.open(path) as src:
            return src.tags().get(SOURCE_TAG, "")
    except Exception:
        return ""


# --- Earth Engine images (one builder per recipe) -----------------------------------------

def ee_init(key_file, project):
    import ee
    if not key_file or not os.path.exists(key_file):
        raise RuntimeError("No Google Earth Engine key. Set PESTWATCH_GEE_KEY_FILE to the service-account JSON "
                           "and PESTWATCH_GEE_PROJECT to its Cloud project.")
    with open(key_file) as f:
        email = json.load(f)["client_email"]
    ee.Initialize(ee.ServiceAccountCredentials(email, key_file), project=project or None, opt_url=HIGH_VOLUME_URL)
    return email


def _first_day(ym):
    import ee
    y, m = map(int, ym.split("-"))
    return ee.Date.fromYMD(y, m, 1)


def s2_stack_image(bands, aoi, ym):
    """sentinel2_monthly_*.ipynb / sent2_C_WV_*.ipynb: build_month() then county_image()."""
    import ee
    s = S2_STACK
    s2 = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED").filterBounds(aoi)
          .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", s["max_cloud"])))
    s2 = s2.linkCollection(ee.ImageCollection("GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED"), ["cs_cdf"])
    s2 = s2.map(lambda img: img.updateMask(img.select("cs_cdf").gte(s["cs_threshold"])).select(bands))
    empty = ee.Image.constant([0] * len(bands)).rename(bands).toUint16().updateMask(0)

    def composite(start, end):
        col = s2.filterDate(start, end)
        return ee.Image(ee.Algorithms.If(col.size().gt(0), col.median(), empty))

    start = _first_day(ym)
    base = composite(start, start.advance(1, "month"))
    filled = base
    flag = ee.Image(255).where(base.select(bands[0]).mask(), 0)
    for w in s["fill_windows"]:
        win = composite(start.advance(-w, "month"), start.advance(w + 1, "month"))
        flag = flag.where(flag.eq(255).And(win.select(bands[0]).mask()), w)
        filled = filled.unmask(win)
    img = filled.unmask(0).toInt16().addBands(flag.rename("fill_flag").toInt16())
    clipped = img.clip(aoi)
    return (clipped.select(bands).unmask(0, False)
            .addBands(clipped.select("fill_flag").unmask(255, False))).toInt16()


def _median_qa_index(ix, img):
    r = img.divide(10000)
    if ix == "MCARI":
        g, red, re1 = r.select("B3"), r.select("B4"), r.select("B5")
        idx = re1.subtract(red).subtract(re1.subtract(g).multiply(0.2)).multiply(re1.divide(red))
        return idx, g.gt(0).And(red.gt(0)).And(re1.gt(0)), "B4"
    if ix == "NDMI":
        nir, swir = r.select("B8"), r.select("B11")
        return nir.subtract(swir).divide(nir.add(swir)), nir.gt(0).And(swir.gt(0)), "B8"
    raise ValueError(f"No median_qa formula for {ix}")


def median_qa_image(ix, roi, epsg, ym):
    """*_monthly_*_fast.ipynb: compute_indices(), prepare_collection(), build_month_image()."""
    import ee
    s = MEDIAN_QA
    m0 = _first_day(ym)
    m1 = m0.advance(1, "month")
    start, end = m0.advance(-s["fill_days"], "day"), m1.advance(s["fill_days"], "day")
    s2 = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED").filterBounds(roi).filterDate(start, end)
          .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", s["max_cloud"])))
    prob = ee.ImageCollection("COPERNICUS/S2_CLOUD_PROBABILITY").filterBounds(roi).filterDate(start, end)
    joined = ee.Join.saveFirst("cloud_prob").apply(
        primary=s2, secondary=prob, condition=ee.Filter.equals(leftField="system:index", rightField="system:index"))

    def per_scene(im):
        im = ee.Image(im)
        scl = im.select("SCL")
        bad = (scl.eq(0).Or(scl.eq(1)).Or(scl.eq(3)).Or(scl.eq(8)).Or(scl.eq(9)).Or(scl.eq(10)).Or(scl.eq(11)))
        cp = ee.Image(ee.Algorithms.If(im.get("cloud_prob"), im.get("cloud_prob"),
                                       ee.Image.constant(0).rename("probability"))).select("probability")
        bad = bad.Or(cp.gt(s["cloud_prob_max"])).focal_max(radius=s["cloud_buffer_m"], kernelType="circle",
                                                             units="meters")
        idx, valid, proj_band = _median_qa_index(ix, im)
        out = idx.rename(ix).toFloat().updateMask(bad.Not().And(valid))
        out = (out.setDefaultProjection(im.select(proj_band).projection())
               .reduceResolution(reducer=ee.Reducer.mean(), bestEffort=False, maxPixels=1024)
               .reproject(crs=f"EPSG:{epsg}", scale=s["scale"]))
        return out.set("system:time_start", im.get("system:time_start"))

    col = ee.ImageCollection(joined).map(per_scene)
    blank = ee.Image.constant(0).rename(ix).toFloat().updateMask(ee.Image.constant(0))
    safe = lambda c: ee.ImageCollection(c.select(ix).merge(ee.ImageCollection([blank])))
    comp = safe(col.filterDate(m0, m1)).median()
    ext = safe(col.filterDate(start, end)).median()
    filled = ee.ImageCollection([ext, comp]).mosaic().rename(ix)
    qa = ee.Image.constant(255).where(ext.mask(), 2).where(comp.mask(), 0).rename("QA")
    return ee.Image.cat([filled, qa]).toFloat().clip(roi).unmask(s["nodata"], False)


def _mean_index(ix, img):
    import ee
    b = img.select
    if ix == "NDVI":
        return img.normalizedDifference(["B8", "B4"]).rename(ix)
    if ix == "NDRE":
        return img.normalizedDifference(["B8A", "B5"]).rename(ix)
    if ix == "PRI":
        return img.normalizedDifference(["B2", "B3"]).rename(ix)
    if ix == "NDMI":
        return img.normalizedDifference(["B8", "B11"]).rename(ix)
    if ix == "CVI":
        green = b("B3")
        return b("B8").multiply(b("B4")).divide(green.pow(2)).updateMask(green.gt(0)).rename(ix)
    if ix == "PSRI":
        return img.expression("(RED - BLUE) / RE2", {"RED": b("B4"), "BLUE": b("B2"), "RE2": b("B6")}).rename(ix)
    if ix == "RDVI":
        return img.expression("(NIR - RED) / sqrt(NIR + RED)", {"NIR": b("B8"), "RED": b("B4")}).rename(ix)
    if ix == "SAVI":
        return img.expression("(1 + L) * (NIR - RED) / (NIR + RED + L)",
                              {"NIR": b("B8"), "RED": b("B4"), "L": 0.5}).rename(ix)
    if ix == "MTVI2":
        return img.expression("1.5 * (1.2 * (NIR - GREEN) - 2.5 * (RED - GREEN)) / "
                              "sqrt((2 * NIR + 1) ** 2 - (6 * NIR - 5 * sqrt(RED)) - 0.5)",
                              {"NIR": b("B8"), "RED": b("B4"), "GREEN": b("B3")}).rename(ix)
    raise ValueError(f"No monthly_mean formula for {ix}")


def monthly_mean_images(product, aoi, ym):
    """The TOP10 engine (gee_common.py): returns (filled image, the month's own image, donor months)."""
    import ee
    s = MONTHLY_MEAN
    start, end = _first_day(month_add(ym, -s["window_months"])), _first_day(month_add(ym, s["window_months"] + 1))
    if product.sensor == "S2":
        raw = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED").filterBounds(aoi).filterDate(start, end)
               .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", s["s2_cloud_max"])))

        def prep(img):
            scl = img.select("SCL")
            good = scl.eq(4).Or(scl.eq(5)).Or(scl.eq(6)).Or(scl.eq(7)).Or(scl.eq(11))
            return (img.select(MEAN_S2_BANDS).updateMask(good).divide(10000)
                    .copyProperties(img, ["system:time_start"]))
    else:
        def one(cid):
            return (ee.ImageCollection(cid).filterBounds(aoi).filterDate(start, end)
                    .filter(ee.Filter.lt("CLOUD_COVER", s["landsat_cloud_max"])))
        raw = one("LANDSAT/LC08/C02/T1_L2").merge(one("LANDSAT/LC09/C02/T1_L2"))

        def prep(img):
            clear = img.select("QA_PIXEL").bitwiseAnd(0b11110).eq(0)
            coastal = img.select("SR_B1").multiply(0.0000275).add(-0.2).rename("COASTAL")
            blue = img.select("SR_B2").multiply(0.0000275).add(-0.2).rename("BLUE")
            lst = img.select("ST_B10").multiply(0.00341802).add(149.0).rename("LST_K")
            return coastal.addBands(blue).addBands(lst).updateMask(clear).copyProperties(img, ["system:time_start"])

    stamps = raw.aggregate_array("system:time_start").getInfo()
    have = sorted({dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).strftime("%Y-%m") for t in stamps})
    if ym not in have:
        return None, None, []

    def month_image(m):
        d0 = _first_day(m)

        def per_scene(img):
            out = ee.Image(prep(img))
            if product.index:
                out = ee.Image(_mean_index(product.index, out))
            return out.toFloat()
        return raw.filterDate(d0, d0.advance(1, "month")).map(per_scene).mean().toFloat()

    donors = donor_months(ym, have, s["window_months"], s["max_neighbours"])
    own = month_image(ym)
    stack = [month_image(o) for o in reversed(donors)] + [own]
    filled = ee.ImageCollection.fromImages(stack).mosaic()
    for _ in range(s["spatial_iter"]):
        filled = filled.unmask(filled.focal_mean(radius=s["spatial_radius_px"], units="pixels"))
    return filled.clip(aoi).toFloat(), own.clip(aoi), donors


def mean_coverage(own, filled, aoi):
    """(share of the county really observed, share valid after filling), as the TOP10 notebooks measure it."""
    import ee
    both = ee.Image.cat(own.select(0).mask().rename("raw"), filled.select(0).mask().rename("final"))
    d = both.reduceRegion(ee.Reducer.mean(), aoi, MONTHLY_MEAN["coverage_scale"], maxPixels=1e9,
                          bestEffort=True).getInfo()
    return float(d.get("raw") or 0.0), float(d.get("final") or 0.0)


# --- downloading ------------------------------------------------------------------------

class TooBig(RuntimeError):
    pass


QUOTA_HINTS = ("too many requests", "429", "503", "quota", "rate limit", "concurrency", "resource exhausted")
SPLIT_HINTS = ("must be less than or equal to", "total request size", "computation timed out",
               "user memory limit exceeded", "memory capacity")


def _get_tile(image, crs, x0, y0, step, h, w, bands, tries=6, log=print):
    """One window of ``image`` on the grid (x0, y0, step): returns an array [bands, h, w]."""
    nbands = len(bands)
    import ee
    import numpy as np
    import requests
    import rasterio
    from rasterio.io import MemoryFile
    # The grid is fixed by crs_transform; the region is inset by 1 % of a pixel so that rounding
    # (degrees in EPSG:4326) cannot pull in an extra edge row or column.
    e = step * 0.01
    region = ee.Geometry.Rectangle([x0 + e, y0 - h * step + e, x0 + w * step - e, y0 - e], proj=crs, geodesic=False)
    params = {"region": region, "crs": crs, "crs_transform": [step, 0, x0, 0, -step, y0], "format": "GEO_TIFF"}
    last = None
    for i in range(tries):
        try:
            try:
                url = image.getDownloadURL(params)
            except ee.EEException as e:
                if any(k in str(e).lower() for k in SPLIT_HINTS):
                    raise TooBig(str(e))
                raise
            r = requests.get(url, timeout=600)
            if r.status_code != 200:
                msg = f"HTTP {r.status_code}: {r.text[:300]}"
                if any(k in msg.lower() for k in SPLIT_HINTS):
                    raise TooBig(msg)
                raise RuntimeError(msg)
            content = r.content
            if content[:2] == b"PK":                                    # one GeoTIFF per band
                with zipfile.ZipFile(io.BytesIO(content)) as zf:
                    arrs = []
                    for b in bands:                                     # in band order, not name order
                        name = next(n for n in zf.namelist() if n.endswith(f".{b}.tif"))
                        with MemoryFile(zf.read(name)) as mf, mf.open() as src:
                            arrs.append(src.read(1))
                arr = np.stack(arrs)
            else:
                with MemoryFile(content) as mf, mf.open() as src:
                    arr = src.read()
            if arr.shape[0] != nbands or arr.shape[1] < h or arr.shape[2] < w or \
                    arr.shape[1] > h + 1 or arr.shape[2] > w + 1:
                raise RuntimeError(f"tile has shape {arr.shape}, expected {(nbands, h, w)}")
            return arr[:, :h, :w]       # the grid starts at the top-left corner: an extra edge pixel is at the end
        except TooBig:
            raise
        except Exception as e:
            last = e
            quota = any(k in str(e).lower() for k in QUOTA_HINTS)
            wait = min(120 if quota else 60, (5 if quota else 2) * 2 ** i) * random.uniform(0.7, 1.3)
            log(f"  tile retry {i + 1}/{tries} in {wait:.0f}s: {str(e)[:160]}")
            time.sleep(wait)
    raise RuntimeError(f"tile failed after {tries} tries: {str(last)[:300]}")


def download(image, crs, bounds, step, tile_px, bands, dtype, fill, out, inside=None, threads=4, log=print,
             tags=None, compress="deflate", predictor=None, nodata=None):
    """Download ``image`` over ``bounds`` on a grid of ``step`` into the GeoTIFF ``out``.

    Windows that do not touch ``inside`` (a shapely geometry in ``crs``) are not requested and
    keep the ``fill`` values. A window Earth Engine finds too heavy is split in four.
    """
    import numpy as np
    import rasterio
    from rasterio.transform import Affine
    from rasterio.windows import Window
    from shapely.geometry import box
    x0, y0, width, height = grid(bounds, step)
    jobs = []
    for r, c, h, w in windows(width, height, tile_px):
        tb = box(x0 + c * step, y0 - (r + h) * step, x0 + (c + w) * step, y0 - r * step)
        if inside is None or tb.intersects(inside):
            jobs.append((r, c, h, w))
    profile = dict(driver="GTiff", height=height, width=width, count=len(bands), dtype=dtype, crs=crs,
                   transform=Affine(step, 0, x0, 0, -step, y0), compress=compress, tiled=True,
                   blockxsize=256, blockysize=256, BIGTIFF="IF_SAFER")
    if predictor:
        profile["predictor"] = predictor
    if nodata is not None:
        profile["nodata"] = nodata
    part = f"{out}.{uuid.uuid4().hex[:8]}.part"
    os.makedirs(os.path.dirname(out), exist_ok=True)

    def fetch(r, c, h, w, depth=0):
        try:
            return [(r, c, _get_tile(image, crs, x0 + c * step, y0 - r * step, step, h, w, bands, log=log))]
        except TooBig:
            if min(h, w) < 128 or depth > 4:
                raise
            hh, ww = (h + 1) // 2, (w + 1) // 2
            parts = []
            for rr, cc, h2, w2 in ((r, c, hh, ww), (r, c + ww, hh, w - ww), (r + hh, c, h - hh, ww),
                                   (r + hh, c + ww, h - hh, w - ww)):
                if h2 > 0 and w2 > 0:
                    parts += fetch(rr, cc, h2, w2, depth + 1)
            return parts

    try:
        with rasterio.open(part, "w", **profile) as dst:
            for i, name in enumerate(bands, 1):
                dst.set_band_description(i, name)
            for r, c, h, w in windows(width, height, 1024):
                block = np.empty((len(bands), h, w), dtype=dtype)
                for b, v in enumerate(fill):
                    block[b] = v
                dst.write(block, window=Window(c, r, w, h))
            done = 0
            with ThreadPoolExecutor(max_workers=threads) as ex:
                futs = [ex.submit(fetch, *j) for j in jobs]
                try:
                    for fut in as_completed(futs):
                        for r, c, arr in fut.result():
                            dst.write(arr.astype(dtype), window=Window(c, r, arr.shape[2], arr.shape[1]))
                        done += 1
                        if done % 20 == 0 or done == len(jobs):
                            log(f"  {done}/{len(jobs)} tiles")
                except BaseException:
                    ex.shutdown(wait=False, cancel_futures=True)
                    raise
            dst.update_tags(**{SOURCE_TAG: EE_SOURCE, "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(
                timespec="seconds"), **(tags or {})})
        return part
    except BaseException:
        if os.path.exists(part):
            os.remove(part)
        raise


# --- one product, county and month ----------------------------------------------------------

@dataclass
class County:
    name: str
    geom_ll: object          # shapely, EPSG:4326
    epsg: int
    geom_utm: object         # shapely, simplified, in EPSG:epsg
    aoi: object = None       # ee.Geometry

    @classmethod
    def from_shapefile(cls, path, name, name_col="adm1_name"):
        import geopandas as gpd
        from shapely.geometry import mapping
        gdf = gpd.read_file(path).to_crs(4326)
        sub = gdf[gdf[name_col].astype(str).str.strip().str.lower() == name.lower()]
        if sub.empty:
            return None
        geom = sub.geometry.union_all() if hasattr(sub.geometry, "union_all") else sub.geometry.unary_union
        c = geom.centroid
        epsg = utm_epsg(c.x, c.y)
        # simplified by 20 m, as the MCARI notebook does, to keep Earth Engine requests small
        utm = gpd.GeoSeries([geom], crs=4326).to_crs(epsg).iloc[0].simplify(20, preserve_topology=True)
        ll = gpd.GeoSeries([utm], crs=epsg).to_crs(4326).iloc[0]
        import ee
        return cls(name, ll, epsg, utm, ee.Geometry(mapping(ll), None, False))


def make(product, county, ym, out, threads=4, log=print):
    """Build and download one file. Returns (status, info): written / low_coverage / no_scenes."""
    import numpy as np
    import rasterio
    crs_utm = f"EPSG:{county.epsg}"
    if product.recipe == "s2_stack":
        img = s2_stack_image(product.bands[:-1], county.aoi, ym)
        part = download(img, crs_utm, county.geom_utm.bounds, product.scale, product.tile_px, product.bands, "int16",
                        [0] * (len(product.bands) - 1) + [255], out, county.geom_utm, threads, log, predictor=2,
                        tags=dict(recipe="s2_stack", month=ym, composite="monthly median, Cloud Score+ >= 0.60",
                                  fill_flag="0=month;1=+/-1 month;2=+/-2 months;255=no data"))
        os.replace(part, out)
        return "written", {}
    if product.recipe == "median_qa":
        s = MEDIAN_QA
        img = median_qa_image(product.index, county.aoi, county.epsg, ym)
        part = download(img, crs_utm, county.geom_utm.bounds, s["scale"], product.tile_px, product.bands, "float32",
                        [s["nodata"], s["nodata"]], out, county.geom_utm, threads, log, compress="lzw",
                        nodata=s["nodata"],
                        tags=dict(recipe="median_qa", month=ym, composite="monthly median of clear observations",
                                  fill_window_days=s["fill_days"], qa_legend="0=clear in month;2=filled from "
                                  "+/-window days;255=unfilled"))
        with rasterio.open(part) as src:
            qa = src.read(2)
        inside = int((qa >= 0).sum())
        frac = float((qa == 0).sum() / inside) if inside else 0.0
        if frac < s["min_valid_fraction"]:
            os.remove(part)
            return "low_coverage", dict(observed=round(frac, 3))
        with rasterio.open(part, "r+") as dst:
            dst.update_tags(observed_valid_fraction=f"{frac:.3f}")
        os.replace(part, out)
        return "written", dict(observed=round(frac, 3))
    # monthly_mean
    s = MONTHLY_MEAN
    filled, own, donors = monthly_mean_images(product, county.aoi, ym)
    if filled is None:
        return "no_scenes", {}
    raw_frac, final_frac = mean_coverage(own, filled, county.aoi)
    if raw_frac < s["min_valid_fraction"]:
        return "low_coverage", dict(observed=round(raw_frac, 3))
    # The notebooks unmask with 0/0 to get NaN, but Earth Engine masks a division by zero and masked
    # pixels download as 0. A -9999 placeholder is downloaded instead and turned into NaN here, which
    # is what the team's files hold outside the county and where nothing could be filled.
    sentinel = -9999.0
    step = s["scale"] / METRES_PER_DEGREE
    part = download(filled.unmask(sentinel, sameFootprint=False), "EPSG:4326", county.geom_ll.bounds, step,
                    product.tile_px, product.bands, "float32", [np.nan] * len(product.bands), out,
                    county.geom_ll, threads, log, nodata=float("nan"),
                    tags=dict(recipe="monthly_mean", month=ym, donors=",".join(donors),
                              valid_fraction=f"{raw_frac:.3f}", final_valid_fraction=f"{final_frac:.3f}"))
    with rasterio.open(part, "r+") as dst:
        for _, win in dst.block_windows(1):
            a = dst.read(window=win)
            a[a == sentinel] = np.nan
            dst.write(a, window=win)
    os.replace(part, out)
    return "written", dict(observed=round(raw_frac, 3), after_fill=round(final_frac, 3))


def run(L, keys, counties, months, refresh=(), force=False, key_file=None, project=None, threads=4,
        ndmi_recipe="median_qa", done_file=None, fetch_from="", log=print):
    """Fetch ``keys`` x ``counties`` x ``months`` (and re-fetch ``refresh`` months this module made).

    Months before ``fetch_from`` are history (uploaded by the team) and are never fetched."""
    prods = products(ndmi_recipe)
    months, old = split_history(list(months), fetch_from)
    refresh, old_refresh = split_history(list(refresh), fetch_from)
    if old or old_refresh:
        log(f"Not fetched, history before {fetch_from[:7]} (uploaded by the modelling team): "
            f"{', '.join(sorted(set(old + old_refresh)))}")
    log(f"Earth Engine as {ee_init(key_file, project)}")
    done = set()
    if done_file and os.path.exists(done_file):
        with open(done_file) as f:
            done = set(json.load(f))
    report = dict(written=[], kept=[], low_coverage=[], no_scenes=[], history=[], failed=[], no_boundary=[])
    report["history"] = [f"{k}/{c}/{m}" for k in keys for c in counties for m in old]
    geoms = {}
    for name in counties:
        geoms[name] = County.from_shapefile(L.boundaries, name)
        if geoms[name] is None:
            report["no_boundary"].append(name)
            log(f"{name}: not in the county boundaries, skipped (import it under Layers > Boundaries)")
    todo = [(m, False) for m in months] + [(m, True) for m in refresh if m not in months]
    for key in keys:
        p = prods[key]
        for name, county in geoms.items():
            if county is None:
                continue
            for ym, is_refresh in todo:
                item = f"{key}/{name}/{ym}"
                y, m = map(int, ym.split("-"))
                out = p.path(L, name, y, m)
                if item in done:
                    continue
                exists = os.path.exists(out)
                if not decide(exists, file_source(out) if exists else "", is_refresh, force):
                    report["kept"].append(item)   # an upload, the team's history, or already fetched
                    continue
                log(f"{key} {name} {ym}: building ({p.recipe})")
                t0 = time.time()
                try:
                    status, info = make(p, county, ym, out, threads, log)
                except Exception as e:
                    report["failed"].append(f"{item}: {str(e)[:300]}")
                    log(f"{key} {name} {ym}: FAILED {str(e)[:300]}")
                    continue
                report[status].append(item + (f" {info}" if info else ""))
                log(f"{key} {name} {ym}: {status} {info or ''} in {time.time() - t0:.0f}s")
                done.add(item)
                if done_file:
                    with open(done_file + ".part", "w") as f:
                        json.dump(sorted(done), f)
                    os.replace(done_file + ".part", done_file)
    return report
