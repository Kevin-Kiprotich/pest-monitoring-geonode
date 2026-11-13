"""Model inputs that managers upload.

History versus new data (``conf.AUTO_FETCH_FROM``): everything before that day is the
modelling team's history and is uploaded here; from that day on, whatever Earth Engine
or the Copernicus CDS can provide is downloaded by the pipelines.

- static layers per county: DEM, soil, crop mask and crop type (once, upload only);
- monthly satellite composites per county: vegetation indices, Sentinel-2 and
  Landsat stacks. History: uploaded. From AUTO_FETCH_FROM: made in Earth Engine
  every month by the "Satellite composites" pipeline (``pipeline/composites.py``).
  A month can still be uploaded (e.g. one Earth Engine could not make), and an
  uploaded file is never replaced by the automatic fetch;
- observed daily weather per county (rainfall, max / min temperature, humidity,
  wind). History: uploaded. From AUTO_FETCH_FROM: the Weather download pipeline;
- the SEAS5 archive (past forecasts, for the bias correction; one file per year and
  start month, for the whole area). History: uploaded. From AUTO_FETCH_FROM: each
  forecast run adds its own start month from the CDS;
- trained pest models (.joblib) per county (upload only);
- the trained LSTM vegetation forecast (final_model.pt, one for all its counties; upload
  only, the forecast never retrains it);
- pest incidence records (.csv), only used to retrain the pest models (upload only).

Each file is checked and written to the exact path the notebooks read
(``pipeline/layout.py``). Monthly, daily and archive datasets also accept a .zip of
many files, each named with its month or day; a single daily file is saved for the
day the manager enters.
"""
import csv
import datetime as dt
import glob
import functools
import io
import os
import pickle
import re
import uuid
import zipfile
from dataclasses import dataclass, field

from osgeo import gdal

from . import conf
from .pipeline.composites import EE_SOURCE, PRODUCT_KEYS, SOURCE_TAG, UPLOAD_SOURCE
from .pipeline.layout import INDICES, PEST_ORDER, Layout, date_in_name, month_in_name, month_range

gdal.UseExceptions()

PEST_NAMES = {"FAW": "Fall armyworm", "APHIDS": "Aphids", "THRIPS": "Thrips", "STEMBORER": "Stem borer",
              "AAW": "African armyworm", "MLND": "MLND"}


class InputError(ValueError):
    """The file cannot be used. The message is shown to the manager."""


@dataclass
class InputDataset:
    key: str
    label: str
    group: str            # static | monthly | weather | seas5 | model | lstm | incidence
    ext: tuple
    used_by: str
    needed_for: str       # nowcast, forecast, training
    bands: tuple = (1, 1)  # allowed band counts (min, max) for rasters
    pest: str = ""
    index: str = ""
    var: str = ""         # weather variable (Layout.wx_dir)
    notes: str = ""
    example: str = field(default="")

    @property
    def monthly(self):
        return self.group == "monthly"

    @property
    def daily(self):
        return self.group == "weather"

    @property
    def regional(self):
        """One file set for the whole area, not per county (the SEAS5 archive)."""
        return self.group == "seas5"

    @property
    def shared(self):
        """Not per county: the SEAS5 archive and the LSTM, which covers all its counties in one file."""
        return self.group in ("seas5", "lstm")

    @property
    def by_month(self):
        """Files keyed by year and month (composites, SEAS5 archive start months)."""
        return self.group in ("monthly", "seas5")

    @property
    def automatic(self):
        """Fetched from Earth Engine by the Satellite composites pipeline (months from AUTO_FETCH_FROM)."""
        return self.key in PRODUCT_KEYS

    @property
    def downloaded(self):
        """History uploaded; new data downloaded from Earth Engine or the CDS."""
        return self.group in ("monthly", "weather", "seas5")

    @property
    def arrives(self):
        """How the dataset gets onto the server, in words."""
        start = conf.get("AUTO_FETCH_FROM")
        if self.group == "monthly":
            return f"History before {start[:7]}: upload. From {start[:7]}: Earth Engine, every month."
        if self.group == "weather":
            src = "Earth Engine" if self.var in ("precip", "wind") else "Copernicus CDS"
            return f"History before {start}: upload. From {start}: {src}, every day."
        if self.group == "seas5":
            return f"History (start months before {start[:7]}): upload. Later: Copernicus CDS, with each forecast."
        return "Upload."

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
        if k == "lstm_model":
            return L.lstm_model
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
        if k.startswith("wx_"):
            day = year                       # daily datasets: path(L, county, date)
            return os.path.join(L.wx_dir(self.var, county), L.wx_name(self.var, county, day))
        if k == "seas5_archive":
            return L.seas5_archive_file(year, month)
        raise KeyError(k)


WEATHER = [("precip", "Rainfall", "CHIRPS, mm"), ("tmax", "Maximum temperature", "ERA5-Land, deg C"),
           ("tmin", "Minimum temperature", "ERA5-Land, deg C"), ("rh", "Relative humidity", "ERA5, %"),
           ("wind", "Wind", "ERA5-Land")]


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
    for var, label, src in WEATHER:
        # Wind is one 4-band file per day (speed, u, v, direction): the pest models use all four;
        # the bias correction and the LSTM read band 1 (speed). The other variables have one band.
        wind = var == "wind"
        bands = ("4 bands: 1 = wind speed (m/s), 2 = u (east-west) component, 3 = v (north-south) component, "
                 "4 = direction (degrees, 0-360). " if wind else "1 band. ")
        d.append(InputDataset(f"wx_{var}", f"{label} (daily history)", "weather", (".tif", ".zip"),
                              "Pest models (observed weather), SEAS5 bias correction, the LSTM", "nowcast, forecast",
                              bands=(4, 4) if wind else (1, 1),
                              var=var, notes=f"One GeoTIFF per day ({src}), {bands}For a single file, enter its day when "
                                             "uploading. A .zip of many days is easiest: each file name in it must "
                                             "contain its day, e.g. "
                                             f"{Layout.wx_name(var, 'Kakamega', dt.date(2023, 3, 4))}."))
    d.append(InputDataset("seas5_archive", "SEAS5 archive of past forecasts", "seas5", (".nc", ".zip"),
                          "Bias correction of the SEAS5 forecast", "forecast",
                          notes="One NetCDF per year and start month, SEAS5_daily_<YYYY-MM>.nc, with temp_c, rh_pct "
                                "and wind_speed per ensemble member (dims number, date, latitude, longitude), "
                                "covering every county. The years from "
                                f"{conf.get('SEAS5_ARCHIVE_START_YEAR')} are used."))
    for p in PEST_ORDER:
        d.append(InputDataset(f"model_{p}", f"{PEST_NAMES[p]} model ({p}_model.joblib)", "model", (".joblib",),
                              "Pest risk prediction", "nowcast, forecast", pest=p,
                              notes="The trained risk model of this pest for this county, a .joblib file named <PEST>_model.joblib. "
                                    "Each county has its own models."
                                    + (" MLND needs the APHIDS, FAW, THRIPS and STEMBORER models of the same county." if p == "MLND" else "")))
    d.append(InputDataset(
        "lstm_model", "LSTM vegetation forecast model (final_model.pt)", "lstm", (".pt",),
        "Vegetation index forecast, which feeds the pest forecast", "forecast",
        notes="The trained neural network that predicts the satellite vegetation indices and bands of the next "
              "7 months from the past months and the weather forecast. A PyTorch file holding the model's settings "
              "(counties, training months, sizes), its normalisation values and its trained weights. One model covers "
              "all the counties it was trained on; it is used as is and never retrained. Uploading a new one "
              "replaces the current model from the next forecast run."))
    for p in PEST_ORDER:
        d.append(InputDataset(f"incidence_{p}", f"{PEST_NAMES[p]} incidence records", "incidence", (".csv",),
                              "Retraining the pest models only", "training", pest=p,
                              notes="CSV with longitude, latitude and a date column (detection_date, survey_date, ...). "
                                    "At least 15 records."))
    return {x.key: x for x in d}


DATASETS = _catalogue()
GROUPS = [("static", "Static layers (upload once)"),
          ("monthly", "Monthly satellite composites (history uploaded; new months from Earth Engine)"),
          ("weather", "Observed daily weather (history uploaded; new days from Earth Engine and the CDS)"),
          ("seas5", "SEAS5 archive, all counties (history uploaded; new start months from the CDS)"),
          ("model", "Trained pest models"),
          ("lstm", "Trained LSTM vegetation forecast (all counties, one model)"), ("incidence", "Pest incidence records (training only)")]


def fetch_from():
    """The first day the pipelines download; earlier data is the uploaded history."""
    return dt.date.fromisoformat(conf.get("AUTO_FETCH_FROM"))


def seas5_history_needed(start_year, first_fetch_day, months=range(1, 13)):
    """(year, month) archive files that are history: start months before ``first_fetch_day``."""
    return sorted((y, m) for m in months for y in range(start_year, first_fetch_day.year + 1)
                  if dt.date(y, m, 1) < first_fetch_day)


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


def seas5_archive_present(L):
    out = set()
    for f in glob.glob(os.path.join(L.seas5_archive, "SEAS5_daily_*.nc")):
        ym = month_in_name(f)
        if ym:
            out.add(ym)
    return sorted(out)


def _monthly_row(row, ds, L, county, last_complete):
    months = _months_present(ds, L, county)
    row["months"] = months
    if not months:
        row["detail"] = "none"
        return
    gaps = [ym for ym in month_range(months[0], last_complete) if ym not in set(months)]
    row["ok"] = months[-1] >= last_complete
    row["detail"] = (f"{len(months)} months, {months[0][0]}-{months[0][1]:02d} to {months[-1][0]}-{months[-1][1]:02d}"
                     + (f"; {len(gaps)} missing" if gaps else ""))
    row["latest"] = f"{months[-1][0]}-{months[-1][1]:02d}"
    row["gaps"] = [f"{y}-{m:02d}" for y, m in gaps[-12:]]
    row["latest_source"] = source_label(file_source(ds.path(L, county, *months[-1])))


def _daily_row(row, ds, L, county, today, first_fetch):
    """Weather: the uploaded history must run without gaps up to the day before AUTO_FETCH_FROM."""
    days = sorted(L.wx_days(ds.var, county))
    if not days:
        row["detail"] = "none"
        return
    history_end = min(first_fetch, today) - dt.timedelta(days=1)
    have = set(days)
    gaps = [days[0] + dt.timedelta(days=i) for i in range((history_end - days[0]).days + 1)]
    gaps = [d for d in gaps if d not in have]
    row["ok"] = days[-1] >= history_end and not gaps
    row["detail"] = f"{len(days)} days, {days[0]} to {days[-1]}" + (f"; {len(gaps)} history days missing" if gaps else "")
    if days[-1] < history_end:
        row["detail"] += f"; history should reach {history_end}"
    row["latest"] = str(days[-1])
    row["gaps"] = [str(d) for d in gaps[:12]]


def _seas5_row(row, L, first_fetch):
    present = seas5_archive_present(L)
    needed = seas5_history_needed(conf.get("SEAS5_ARCHIVE_START_YEAR"), first_fetch)
    missing = [ym for ym in needed if ym not in set(present)]
    row["ok"] = not missing
    row["detail"] = (f"{len(present)} start months" if present else "none") + \
        (f"; {len(missing)} history files missing" if missing else "")
    if present:
        row["latest"] = f"{present[-1][0]}-{present[-1][1]:02d}"
    row["gaps"] = [f"{y}-{m:02d}" for y, m in missing[:12]]


def _lstm_row(row, L):
    p = L.lstm_model
    if not os.path.exists(p):
        row["detail"] = "missing"
        return
    row["mtime"] = os.path.getmtime(p)
    try:
        info = lstm_summary(p)
    except InputError as e:
        row["detail"] = f"{os.path.getsize(p) / 1e6:.1f} MB; unreadable: {e}"
        return
    row["ok"] = True
    row["detail"] = f"{os.path.getsize(p) / 1e6:.1f} MB; {describe_lstm(info)}"


def status(county, today):
    """One row per dataset for ``county``: what is there and what is missing."""
    L = layout()
    last_complete = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    first_fetch = fetch_from()
    rows = []
    for ds in DATASETS.values():
        row = dict(ds=ds, ok=False, detail="")
        if ds.monthly:
            _monthly_row(row, ds, L, county, last_complete)
        elif ds.daily:
            _daily_row(row, ds, L, county, today, first_fetch)
        elif ds.regional:
            _seas5_row(row, L, first_fetch)
        elif ds.key == "lstm_model":
            _lstm_row(row, L)
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


# --- the LSTM model ----------------------------------------------------------------
# final_model.pt is a torch.save() of dict(cfg=..., norm=..., states=[state_dict per seed]).
# GeoNode's Python has no torch, so the file is read with a restricted unpickler: only plain
# Python and numpy types are rebuilt, every other object (torch tensors, storages) becomes a
# stub and no other code can run. That is enough to check the settings the forecast reads.

LSTM_CFG_KEYS = ("L", "H", "K", "VIS", "WXN", "STN", "COUNTIES", "MONTHS", "PRIMARY", "MIN_ENC_USABLE", "HIDDEN",
                 "LAYERS", "STATIC_EMB", "DROPOUT", "n_static", "K_SHRINK", "ELEV_BINS", "SMOOTH_CLIM", "Z_CLIP",
                 "QA_SOURCE", "clim_end", "epochs", "trained")
_TORCH_LEGACY_MAGIC = 0x1950a86a20f9469cfc6c
_SAFE_GLOBALS = {("builtins", n) for n in ("dict", "list", "tuple", "set", "frozenset", "int", "float", "complex",
                                             "str", "bytes", "bytearray", "bool", "slice", "range")} | \
    {("collections", "OrderedDict"), ("_codecs", "encode")}
# numpy arrays and scalars in cfg / norm (numpy.core.multiarray in numpy 1, numpy._core.multiarray in numpy 2)
_NUMPY_GLOBALS = {("multiarray", "_reconstruct"), ("multiarray", "scalar"), ("numpy", "dtype"), ("numpy", "ndarray")}


class _Stub:
    """Stands in for any object that is not plain Python or numpy (torch tensors, storages, dtypes)."""
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return _Stub()

    def __setstate__(self, state):
        pass

    def __setitem__(self, key, value):
        pass


class _SettingsUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if (module, name) in _SAFE_GLOBALS:
            return super().find_class(module, name)
        if module.split(".")[0] == "numpy" and (module.rsplit(".", 1)[-1], name) in _NUMPY_GLOBALS:
            import numpy
            if module.startswith("numpy._core") and not hasattr(numpy, "_core"):
                module = "numpy.core" + module[len("numpy._core"):]      # a numpy 2 pickle read by numpy 1
            return super().find_class(module, name)
        return _Stub

    def persistent_load(self, pid):
        return _Stub()


def _unpickle_settings(f):
    try:
        return _SettingsUnpickler(f).load()
    except Exception as e:
        raise InputError(f"The model file could not be read ({type(e).__name__}: {e}).")


def _load_lstm_artefact(path):
    with open(path, "rb") as f:
        head = f.read(4)
    if head[:2] == b"PK":                                   # torch >= 1.6: a zip holding <name>/data.pkl
        try:
            with zipfile.ZipFile(path) as z:
                pkl = [n for n in z.namelist() if n == "data.pkl" or n.endswith("/data.pkl")]
                if not pkl:
                    raise InputError("This zip is not a PyTorch file (no data.pkl inside).")
                with z.open(min(pkl, key=len)) as f:
                    return _unpickle_settings(f)
        except zipfile.BadZipFile:
            raise InputError("The model file is a damaged zip.")
    if head[:1] == b"\x80":                                 # legacy torch.save: magic, protocol, sys info, object
        with open(path, "rb") as f:
            if _unpickle_settings(f) != _TORCH_LEGACY_MAGIC:
                raise InputError("This pickle is not a PyTorch file.")
            _unpickle_settings(f)
            _unpickle_settings(f)
            return _unpickle_settings(f)
    raise InputError("This does not look like a PyTorch model file (torch.save).")


def check_lstm(path):
    """The settings of a stored LSTM, checked against what the forecast notebook reads."""
    art = _load_lstm_artefact(path)
    if not isinstance(art, dict) or not all(k in art for k in ("cfg", "norm", "states")):
        raise InputError("This is not a trained LSTM vegetation forecast model: it should hold the model's settings "
                         "(cfg), normalisation values (norm) and trained weights (states).")
    cfg = art["cfg"]
    missing = [k for k in LSTM_CFG_KEYS if not isinstance(cfg, dict) or k not in cfg]
    if missing:
        raise InputError(f"The model's settings (cfg) lack {', '.join(missing)}.")
    try:
        counties, months = [str(c) for c in cfg["COUNTIES"]], [str(m) for m in cfg["MONTHS"]]
        indices, seeds = [str(v) for v in cfg["VIS"]], len(art["states"])
    except TypeError:
        raise InputError("The model's COUNTIES, MONTHS, VIS or states are not lists.")
    if not counties or not months or not seeds:
        raise InputError("The model has no counties, no training months or no trained weights.")
    return dict(counties=counties, months=(months[0], months[-1]), clim_end=str(cfg["clim_end"]),
                trained=str(cfg["trained"]), seeds=seeds, indices=indices)


@functools.lru_cache(maxsize=4)
def _lstm_summary_cached(path, mtime, size):
    return check_lstm(path)


def lstm_summary(path):
    """check_lstm(), cached while the file is unchanged (the Input data page shows it on every view)."""
    st = os.stat(path)
    return _lstm_summary_cached(path, st.st_mtime, st.st_size)


def describe_lstm(info):
    return (f"trained {info['trained']} on {info['months'][0]} to {info['months'][1]}, "
            f"counties {', '.join(info['counties'])}, {info['seeds']} seed{'s' if info['seeds'] != 1 else ''}")


SEAS5_VARIABLES = ("temp_c", "rh_pct", "wind_speed")


def _check_netcdf(path):
    """A SEAS5 archive file: NetCDF (classic or NetCDF-4) holding the variables the bias correction reads."""
    with open(path, "rb") as f:
        head = f.read(8)
    if not (head[:3] == b"CDF" or head == b"\x89HDF\r\n\x1a\n"):
        raise InputError("This is not a NetCDF file.")
    try:
        r = gdal.Open(path)
    except RuntimeError:
        return                     # GDAL without the netCDF driver: the format check above has to do
    if r is None:
        return
    names = {n.rsplit(":", 1)[-1] for n, _ in (r.GetSubDatasets() or [])}
    if names:
        missing = [v for v in SEAS5_VARIABLES if v not in names]
        if missing:
            raise InputError(f"The archive file lacks {', '.join(missing)} (it has {', '.join(sorted(names))}).")


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
    if ds.key == "lstm_model":
        check_lstm(path)
    elif path.lower().endswith(".tif"):
        _check_raster(ds, path)
    elif path.lower().endswith(".csv"):
        _check_csv(path)
    elif path.lower().endswith(".joblib"):
        _check_joblib(path)
    elif path.lower().endswith(".nc"):
        _check_netcdf(path)


def _key_in_name(ds, name):
    """The date (daily datasets) or (year, month) a file name carries, or None."""
    return date_in_name(name) if ds.daily else month_in_name(name)


def _target(ds, L, county, key):
    return ds.path(L, county, key) if ds.daily else ds.path(L, county, *key)


NAME_HINT = {"monthly": "YYYY_MM (e.g. NDVI_Kakamega_2026_10.tif)", "seas5": "YYYY-MM (e.g. SEAS5_daily_2021-11.nc)",
             "weather": "the day, YYYY_MM_DD, YYYY-MM-DD or YYYYMMDD (e.g. CHIRPS_Kakamega_2023_03_04.tif)"}


def save_upload(ds, county, when, uploaded):
    """Check ``uploaded`` and write it where the models read it. Returns (paths written, files skipped).

    ``when`` is the (year, month) of a monthly or archive file, or the date of a daily file; when
    it is None the month or day is read from the file name. A .zip always uses the names.
    ``county`` is ignored for datasets that are not per county (the SEAS5 archive, the LSTM)."""
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
        if ds.by_month or ds.daily:
            key = when or _key_in_name(ds, name)
            if not key:
                raise InputError(("Choose the month, or put it" if ds.by_month else "Enter the day, or put it") +
                                 f" in the file name: {NAME_HINT[ds.group]}.")
            target = _target(ds, L, county, key)
        else:
            target = ds.path(L, county)
        _validate(ds, tmp)
        _write(tmp, target, ds)
        return [target], []
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _save_zip(ds, county, zpath, tmp_dir, L):
    """Every file in the zip with the dataset's extension and a month (or day) in its name is written as that."""
    try:
        z = zipfile.ZipFile(zpath)
    except zipfile.BadZipFile:
        raise InputError("The zip file could not be opened.")
    exts = tuple(e for e in ds.ext if e != ".zip")
    written, errors = [], []
    with z:
        members = [m for m in z.infolist() if not m.is_dir() and m.filename.lower().endswith(exts)]
        if not members:
            raise InputError(f"The zip contains no {' or '.join(exts)} files.")
        for m in members:
            key = _key_in_name(ds, m.filename)
            if not key:
                errors.append(f"{os.path.basename(m.filename)}: no {NAME_HINT[ds.group].split(' (')[0]} in the name")
                continue
            ext = os.path.splitext(m.filename)[1].lower()
            tmp = os.path.join(tmp_dir, f"{uuid.uuid4().hex}{ext}")
            with z.open(m) as src, open(tmp, "wb") as dst:       # never extract by member name (zip slip)
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    dst.write(chunk)
            try:
                _validate(ds, tmp)
                target = _target(ds, L, county, key)
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
