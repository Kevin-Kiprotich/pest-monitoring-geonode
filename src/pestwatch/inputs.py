"""Model inputs that managers upload.

- static layers per county: DEM, soil, crop mask and crop type (once);
- monthly satellite composites per county: vegetation indices, Sentinel-2 and
  Landsat stacks. These are also fetched from Earth Engine every month by the
  "Satellite composites" pipeline (``pipeline/composites.py``); an upload is
  still possible, e.g. for the modelling team's history or a month Earth Engine
  could not make, and an uploaded file is never replaced by the automatic fetch;
- trained pest models (.joblib) per county;
- pest incidence records (.csv), only used to retrain the pest models.

Each file is checked and written to the exact path the notebooks read
(``pipeline/layout.py``). Monthly datasets also accept a .zip of many months.
"""
import csv
import io
import os
import re
import uuid
import zipfile
from dataclasses import dataclass, field

from osgeo import gdal

from . import conf
from .pipeline.composites import EE_SOURCE, PRODUCT_KEYS, SOURCE_TAG, UPLOAD_SOURCE
from .pipeline.layout import INDICES, PEST_ORDER, Layout, month_in_name, month_range

gdal.UseExceptions()

PEST_NAMES = {"FAW": "Fall armyworm", "APHIDS": "Aphids", "THRIPS": "Thrips", "STEMBORER": "Stem borer",
              "AAW": "African armyworm", "MLND": "MLND"}


class InputError(ValueError):
    """The file cannot be used. The message is shown to the manager."""


@dataclass
class InputDataset:
    key: str
    label: str
    group: str            # static | monthly | model | incidence
    ext: tuple
    used_by: str
    needed_for: str       # nowcast, forecast, training
    bands: tuple = (1, 1)  # allowed band counts (min, max) for rasters
    pest: str = ""
    index: str = ""
    notes: str = ""
    example: str = field(default="")

    @property
    def monthly(self):
        return self.group == "monthly"

    @property
    def automatic(self):
        """Also fetched from Earth Engine by the Satellite composites pipeline."""
        return self.key in PRODUCT_KEYS

    def path(self, L, county, year=None, month=None):
        k = self.key
        if k == "dem":
            return L.dem(county)
        if k.startswith("soil_"):
            return L.soil(k[5:], county)
        if k == "crop_mask":
            return L.crop("CropMask", county, conf.get("CROP_SEASON"))
        if k == "crop_type":
            return L.crop("CropType", county, conf.get("CROP_SEASON"))
        if k.startswith("model_"):
            return L.pest_model(self.pest, county)
        if k.startswith("incidence_"):
            return L.incidence(self.pest, county)
        if k.startswith("index_"):
            return L.index_file(self.index, county, year, month)
        if k == "s2_bands":
            return L.s2_file(county, year, month)
        if k == "s2_cwv":
            return L.cwv_file(county, year, month)
        if k == "landsat":
            return L.landsat_file(county, year, month)
        raise KeyError(k)


def _catalogue():
    d = [
        InputDataset("dem", "Elevation (DEM, 30 m)", "static", (".tif",), "Terrain predictors, temperature lapse-rate "
                     "correction, forecast downscaling, LSTM static inputs", "nowcast, forecast",
                     notes="SRTM or ALOS, clipped to the county, in metres."),
        InputDataset("soil_clay", "Soil clay 0-30 cm", "static", (".tif",), "Pest models (APHIDS, STEMBORER, THRIPS, MLND)",
                     "nowcast, forecast", notes="ISRIC SoilGrids."),
        InputDataset("soil_sand", "Soil sand 0-30 cm", "static", (".tif",), "Pest models (FAW, AAW, STEMBORER, THRIPS, MLND)",
                     "nowcast, forecast", notes="ISRIC SoilGrids."),
        InputDataset("soil_soc", "Soil organic carbon 0-30 cm", "static", (".tif",), "All pest models", "nowcast, forecast",
                     notes="ISRIC SoilGrids."),
        InputDataset("soil_phh2o", "Soil pH 0-30 cm", "static", (".tif",), "Registered by the pest models; not in the "
                     "current predictor lists", "optional", notes="ISRIC SoilGrids."),
        InputDataset("crop_mask", "Crop mask (JRC)", "static", (".tif",), "Host layer of the pest models", "nowcast, forecast",
                     notes=f"1 = cropland, 2 = other. Season {conf.get('CROP_SEASON')}."),
        InputDataset("crop_type", "Crop type (JRC)", "static", (".tif",), "Pest models (crop.type predictor)",
                     "nowcast, forecast", notes=f"Crop type codes. Season {conf.get('CROP_SEASON')}."),
    ]
    for ix in INDICES:
        d.append(InputDataset(f"index_{ix}", f"{ix} monthly composite", "monthly", (".tif", ".zip"),
                              "Pest models (observed month) and the LSTM (history)", "nowcast, forecast", bands=(1, 2),
                              index=ix, notes="Band 1 = index value, optional band 2 = QA (0 clear, 2 gap-filled, 255 missing). "
                                              "Nodata -9999."))
    d += [
        InputDataset("s2_bands", "Sentinel-2 monthly band stack", "monthly", (".tif", ".zip"),
                     "Pest models (s.red, s.nir, s.red.edge) and the LSTM (S_RED, S_NIR, S_RE3)", "nowcast, forecast",
                     bands=(11, 13), notes="11 bands: 3 = B4 red, 6 = B7 red edge 3, 7 = B8 NIR, 11 = fill flag."),
        InputDataset("s2_cwv", "Sentinel-2 coastal and water vapour", "monthly", (".tif", ".zip"),
                     "Pest models (s.coastal, s.vapour) and the LSTM (S_COASTAL, S_WV)", "nowcast, forecast",
                     bands=(3, 3), notes="Band 1 = B1 coastal, 2 = B9 water vapour, 3 = fill flag."),
        InputDataset("landsat", "Landsat monthly stack", "monthly", (".tif", ".zip"),
                     "Pest models (l.coastal, l.blue, l.lst), the LSTM, and the 30 m grid of the forecast", "nowcast, forecast",
                     bands=(3, 8), notes="Bands COASTAL, BLUE, LST_K (Kelvin), 30 m."),
    ]
    for p in PEST_ORDER:
        d.append(InputDataset(f"model_{p}", f"{PEST_NAMES[p]} model ({p}_model.joblib)", "model", (".joblib",),
                              "Pest risk prediction", "nowcast, forecast", pest=p,
                              notes="Saved by the pest notebook (models/<PEST>_model.joblib). Models are county-specific."
                                    + (" MLND needs the APHIDS, FAW, THRIPS and STEMBORER models of the same county." if p == "MLND" else "")))
    for p in PEST_ORDER:
        d.append(InputDataset(f"incidence_{p}", f"{PEST_NAMES[p]} incidence records", "incidence", (".csv",),
                              "Retraining the pest models only", "training", pest=p,
                              notes="CSV with longitude, latitude and a date column (detection_date, survey_date, ...). "
                                    "At least 15 records."))
    return {x.key: x for x in d}


DATASETS = _catalogue()
GROUPS = [("static", "Static layers (upload once)"),
          ("monthly", "Monthly satellite composites (fetched from Earth Engine every month, or uploaded)"),
          ("model", "Trained pest models"), ("incidence", "Pest incidence records (training only)")]


def layout():
    return Layout(conf.get("DATA_ROOT"))


# --- status ---------------------------------------------------------------------

def _months_present(ds, L, county):
    probe = ds.path(L, county, 2000, 1)
    folder = os.path.dirname(probe)
    if not os.path.isdir(folder):
        return []
    prefix = re.escape(os.path.basename(probe).split("2000_01")[0])
    pat = re.compile(rf"^{prefix}((?:19|20)\d\d)_(\d\d)(?:_\d\d)?\.tif$", re.I)
    out = set()
    for f in os.listdir(folder):
        m = pat.match(f)
        if m:
            out.add((int(m.group(1)), int(m.group(2))))
    return sorted(out)


def status(county, today):
    """One row per dataset for ``county``: what is there and what is missing."""
    L = layout()
    last_complete = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    rows = []
    for ds in DATASETS.values():
        row = dict(ds=ds, ok=False, detail="")
        if ds.monthly:
            months = _months_present(ds, L, county)
            row["months"] = months
            if months:
                start = months[0]
                gaps = [ym for ym in month_range(start, last_complete) if ym not in set(months)]
                row["ok"] = months[-1] >= last_complete
                row["detail"] = (f"{len(months)} months, {months[0][0]}-{months[0][1]:02d} to {months[-1][0]}-{months[-1][1]:02d}"
                                 + (f"; {len(gaps)} missing" if gaps else ""))
                row["latest"] = f"{months[-1][0]}-{months[-1][1]:02d}"
                row["gaps"] = [f"{y}-{m:02d}" for y, m in gaps[-12:]]
                row["latest_source"] = source_label(file_source(ds.path(L, county, *months[-1])))
            else:
                row["detail"] = "none"
        else:
            p = ds.path(L, county)
            if os.path.exists(p):
                row["ok"] = True
                row["detail"] = f"{os.path.getsize(p) / 1e6:.1f} MB"
                row["mtime"] = os.path.getmtime(p)
            else:
                row["detail"] = "missing"
        rows.append(row)
    return rows


# --- upload ------------------------------------------------------------------------

def _check_raster(ds, path):
    try:
        r = gdal.Open(path)
    except RuntimeError:
        r = None
    if r is None:
        raise InputError("The file could not be read as a raster.")
    if r.GetDriver().ShortName != "GTiff":
        raise InputError(f"Expected a GeoTIFF, got {r.GetDriver().ShortName}.")
    if r.GetSpatialRef() is None or r.GetGeoTransform(can_return_null=True) is None:
        raise InputError("The raster has no coordinate reference system or georeferencing.")
    lo, hi = ds.bands
    if not lo <= r.RasterCount <= hi:
        want = f"{lo}" if lo == hi else f"{lo} to {hi}"
        raise InputError(f"{ds.label} should have {want} band(s); this file has {r.RasterCount}.")


def _check_csv(path):
    with open(path, "rb") as f:
        head = f.read(65536).decode("utf-8-sig", "replace")
    try:
        cols = [c.strip().lower() for c in next(csv.reader(io.StringIO(head)))]
    except StopIteration:
        raise InputError("The CSV is empty.")
    missing = [c for c in ("longitude", "latitude") if c not in cols]
    if missing:
        raise InputError(f"The CSV needs the columns longitude and latitude; it has {', '.join(cols[:15])}.")


def _check_joblib(path):
    with open(path, "rb") as f:
        head = f.read(4)
    # joblib files are pickles, optionally compressed (zlib, gzip, bz2, xz, lz4)
    if not head or not (head[:1] == b"\x80" or head[:2] in (b"x\x9c", b"\x1f\x8b", b"BZ") or head[:4] in
                        (b"\xfd7zX", b"\x04\x22\x4d\x18") or head[:2] == b"ZF"):
        raise InputError("This does not look like a joblib model file.")


def file_source(path):
    """Who made a monthly file: "earthengine", "upload", or "" (copied onto the server, e.g. the team's history)."""
    try:
        r = gdal.Open(path)
        return (r.GetMetadataItem(SOURCE_TAG) or "") if r is not None else ""
    except RuntimeError:
        return ""


def source_label(source):
    return {EE_SOURCE: "Earth Engine", UPLOAD_SOURCE: "uploaded"}.get(source, "copied to the server")


def _mark_uploaded(path):
    """Tag an uploaded GeoTIFF, so the automatic Earth Engine fetch never replaces it."""
    r = gdal.Open(path, gdal.GA_Update)
    r.SetMetadataItem(SOURCE_TAG, UPLOAD_SOURCE)
    r.FlushCache()
    r = None


def _write(tmp, target, ds=None):
    if ds is not None and ds.monthly and tmp.lower().endswith(".tif"):
        try:
            _mark_uploaded(tmp)
        except Exception:      # only a label: untagged files are never replaced by the fetch either
            pass
    os.makedirs(os.path.dirname(target), exist_ok=True)
    os.replace(tmp, target)


def _validate(ds, path):
    if path.lower().endswith(".tif"):
        _check_raster(ds, path)
    elif path.lower().endswith(".csv"):
        _check_csv(path)
    elif path.lower().endswith(".joblib"):
        _check_joblib(path)


def save_upload(ds, county, month, uploaded):
    """Check ``uploaded`` and write it where the models read it. Returns (paths written, files skipped)."""
    L = layout()
    name = uploaded.name
    ext = os.path.splitext(name)[1].lower()
    if ext not in ds.ext:
        raise InputError(f"{ds.label} must be a {' or '.join(ds.ext)} file, not {ext or 'a file without extension'}.")
    tmp_dir = os.path.join(L.root, "uploads_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    tmp = os.path.join(tmp_dir, f"{uuid.uuid4().hex}{ext}")
    with open(tmp, "wb") as out:
        for chunk in uploaded.chunks():
            out.write(chunk)
    try:
        if ext == ".zip":
            return _save_zip(ds, county, tmp, tmp_dir, L)
        if ds.monthly:
            ym = month or month_in_name(name)
            if not ym:
                raise InputError("Choose the month, or put it in the file name (e.g. NDVI_Kakamega_2026_10.tif).")
            target = ds.path(L, county, *ym)
        else:
            target = ds.path(L, county)
        _validate(ds, tmp)
        _write(tmp, target, ds)
        return [target], []
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _save_zip(ds, county, zpath, tmp_dir, L):
    """Every .tif in the zip whose name carries a year and month (YYYY_MM) is written as that month."""
    try:
        z = zipfile.ZipFile(zpath)
    except zipfile.BadZipFile:
        raise InputError("The zip file could not be opened.")
    written, errors = [], []
    with z:
        members = [m for m in z.infolist() if not m.is_dir() and m.filename.lower().endswith(".tif")]
        if not members:
            raise InputError("The zip contains no .tif files.")
        for m in members:
            ym = month_in_name(m.filename)
            if not ym:
                errors.append(f"{os.path.basename(m.filename)}: no YYYY_MM in the name")
                continue
            tmp = os.path.join(tmp_dir, f"{uuid.uuid4().hex}.tif")
            with z.open(m) as src, open(tmp, "wb") as dst:       # never extract by member name (zip slip)
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    dst.write(chunk)
            try:
                _validate(ds, tmp)
                target = ds.path(L, county, *ym)
                _write(tmp, target, ds)
                written.append(target)
            except InputError as e:
                errors.append(f"{os.path.basename(m.filename)}: {e}")
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
    if not written:
        raise InputError("Nothing was saved. " + "; ".join(errors[:5]))
    return written, errors
