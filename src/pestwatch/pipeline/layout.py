"""Where every model input and output lives under the data root (``/data/pestmodels``).

The ``data/`` folder mirrors the modelling team's ``F:/VI_prediction`` layout, so
the notebooks find their files with only the root folder changed:

    data/HISTORICAL WEATHER/...          observed daily weather (history uploaded, then fetched daily)
    data/SRTM, SoilGrids, JRC_...        static layers (uploaded once by a manager)
    data/<INDEX>/<County>/...            monthly vegetation indices (history uploaded, then Earth Engine)
    data/Sentinel_AllBands_2024/...      monthly Sentinel-2 band stacks (history uploaded, then Earth Engine)
    data/LANDSAT_STACK/<County>/...      monthly Landsat stacks (history uploaded, then Earth Engine)
    data/FORECASTED_WEATHER*/...         SEAS5 forecast, raw / bias-corrected / 30 m
    data/Incidence_data/...              field records (training only)

Outside ``data/``: the SEAS5 archive, county boundaries, trained models, run
outputs and logs. Standard library only (shared by Django and the model environment).
"""
import calendar
import datetime as dt
import os
import re

HIST = "HISTORICAL WEATHER"

# Monthly vegetation indices read by the pest models and the LSTM.
INDICES = ["CVI", "MCARI", "MTVI2", "NDMI", "NDRE", "NDVI", "PRI", "PSRI", "RDVI", "SAVI"]
INDEX_SUBDIR = {"SAVI": "SAVI/tiff", "MTVI2": "MTVI2/tiff"}

# The notebooks' pest codes, in the order they must run (APHIDS uses FAW; MLND uses the four vectors).
PEST_ORDER = ["FAW", "APHIDS", "THRIPS", "STEMBORER", "AAW", "MLND"]

# Folder names of the Sentinel-2 coastal / water vapour stacks for the original counties.
CWV_FOLDERS = {"Kakamega": "C_WV_KACH", "Kilifi": "C_WV_KILIFI", "Nyeri": "C_WV_NYERI"}

INCIDENCE = {
    "FAW": "Fall_Armyworms/{county}_fall_armyworm_incidence.csv",
    "APHIDS": "Aphids/{county}_aphids_incidence.csv",
    "MLND": "MLND/{COUNTY}.csv",
    "AAW": "African_armyworm/{County}_africanarmyworm.csv",
    "STEMBORER": "Stemborer/{County}_Stemborer.csv",
    "THRIPS": "Thrips/{County}_thrips.csv",
}


def tokens(pattern, county="", year=None, month=None):
    out = pattern.replace("{County}", county).replace("{COUNTY}", county.upper()).replace("{county}", county.lower())
    if year is not None:
        out = out.replace("{yyyy}", f"{year:04d}").replace("{mm}", f"{month:02d}")
    return out


def cwv_folder(county):
    return CWV_FOLDERS.get(county, "C_WV_" + county.upper())


class Layout:
    def __init__(self, root):
        self.root = root

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    # --- shared ---------------------------------------------------------------
    @property
    def data(self):
        """The notebooks' v_root / h_root / VI_ROOT (F:/VI_prediction)."""
        return self.p("data")

    @property
    def hist(self):
        return self.p("data", HIST)

    @property
    def boundaries(self):
        return self.p("boundaries", "counties.shp")

    def run_dir(self, run_id):
        return self.p("runs", str(run_id))

    def raw(self, source):
        return self.p("raw", source)

    # --- observed daily weather ---------------------------------------------
    def wx_dir(self, var, county):
        return {
            "tmax": os.path.join(self.hist, "ERA5_LAND_TEMP_fixed", county, "tmax"),
            "tmin": os.path.join(self.hist, "ERA5_LAND_TEMP_fixed", county, "tmin"),
            "precip": os.path.join(self.hist, "CHIRPS_DATA_PY_daily", "filled_daily", county),
            "rh": os.path.join(self.hist, "DAILY_RH", f"RH_{county.upper()}", "daily_tifs"),
            "wind": os.path.join(self.hist, "WIND_DATA_PY_daily", "county_daily_direct", county),
        }[var]

    @staticmethod
    def wx_name(var, county, day):
        return {
            "tmax": f"{county}_tmax_{day:%Y%m%d}.tif",
            "tmin": f"{county}_tmin_{day:%Y%m%d}.tif",
            "precip": f"CHIRPS_{county}_{day:%Y_%m_%d}.tif",
            "rh": f"{county}_RH_{day:%Y-%m-%d}.tif",
            "wind": f"WIND_{county}_{day:%Y_%m_%d}.tif",
        }[var]

    def wx_days(self, var, county):
        """Dates that have a daily file for ``var``."""
        d = self.wx_dir(var, county)
        if not os.path.isdir(d):
            return set()
        return {day for day in (date_in_name(f) for f in os.listdir(d) if f.lower().endswith(".tif")) if day}

    @property
    def chirps_base(self):
        return os.path.join(self.hist, "CHIRPS_DATA_PY_daily")

    @property
    def era5_temp_base(self):
        return os.path.join(self.hist, "ERA5_LAND_TEMP_fixed")

    @property
    def wind_base(self):
        return os.path.join(self.hist, "WIND_DATA_PY_daily")

    # --- SEAS5 forecast weather ------------------------------------------------
    @property
    def fc_raw(self):
        return self.p("data", "FORECASTED_WEATHER")

    @property
    def fc_bc(self):
        return self.p("data", "FORECASTED_WEATHER_BC_V2")

    @property
    def fc_30m(self):
        return self.p("data", "FORECASTED_WEATHER_30M_BC_V2")

    @property
    def seas5_archive(self):
        return self.p("SEAS5_ARCHIVE", "daily")

    def seas5_archive_file(self, year, month):
        """The archived SEAS5 forecast that started on the first of ``month`` in ``year``."""
        return os.path.join(self.seas5_archive, f"SEAS5_daily_{year:04d}-{month:02d}.nc")

    def fc_file(self, base, group, var, stat, county, day):
        return os.path.join(base, group, var, stat, f"{county}_{var}_{stat}_{day:%Y-%m-%d}.tif")

    # --- static and monthly layers ---------------------------------------------
    def dem(self, county):
        return os.path.join(self.data, "SRTM", county, f"{county.lower()}_dem_clipped.tif")

    @property
    def dem_pattern(self):
        """DEM path with the notebooks' format tokens."""
        return os.path.join(self.data, "SRTM", "{county}", "{county_lower}_dem_clipped.tif")

    def soil(self, prop, county):
        return os.path.join(self.data, "SoilGrids", county, f"{prop}_0-30cm_{county}.tif")

    def crop(self, kind, county, season):
        return os.path.join(self.data, "JRC_CropType_CropMask", county, kind, f"{season}_{kind}_{county}.tif")

    def index_file(self, index, county, year, month):
        sub = INDEX_SUBDIR.get(index, index)
        return os.path.join(self.data, *sub.split("/"), county, f"{index}_{county}_{year:04d}_{month:02d}.tif")

    def s2_file(self, county, year, month):
        return os.path.join(self.data, "Sentinel_AllBands_2024", county, f"S2_{county}_{year:04d}_{month:02d}.tif")

    def cwv_file(self, county, year, month):
        return os.path.join(self.data, "coastal_water_vapour", cwv_folder(county),
                            f"S2_CWV_{county}_{year:04d}_{month:02d}.tif")

    def landsat_file(self, county, year, month):
        return os.path.join(self.data, "LANDSAT_STACK", county, f"LANDSAT_STACK_{county}_{year:04d}_{month:02d}.tif")

    @property
    def landsat_template_pattern(self):
        return os.path.join(self.data, "LANDSAT_STACK", "{county}", "LANDSAT_STACK_{county}_*.tif")

    @property
    def s2_template_pattern(self):
        return os.path.join(self.data, "Sentinel_AllBands_2024", "{county}", "S2_{county}_2020_01.tif")

    @property
    def incidence_root(self):
        return self.p("data", "Incidence_data")

    def incidence(self, pest, county):
        return os.path.join(self.incidence_root, *tokens(INCIDENCE[pest], county).split("/"))

    # --- models ------------------------------------------------------------------
    def pest_models(self, county):
        return self.p("models", "pest", county)

    def pest_model(self, pest, county):
        return os.path.join(self.pest_models(county), f"{pest}_model.joblib")

    def pests_with_models(self, county):
        return [p for p in PEST_ORDER if os.path.exists(self.pest_model(p, county))]

    @property
    def lstm_models(self):
        return self.p("models", "lstm")

    @property
    def lstm_model(self):
        """The stored, trained LSTM (Notebook B's final_model.pt). It is used as is, never retrained."""
        return os.path.join(self.lstm_models, "final_model.pt")

    @property
    def lstm_skill(self):
        """Optional: Notebook B's test-skill table, only copied into the forecast summary."""
        return os.path.join(self.lstm_models, "test_skill_observed_weather.csv")

    @property
    def lstm_work(self):
        """Notebooks A, A2 and C's WORK_DIR (dataset and stores, rebuilt from the latest inputs)."""
        return self.p("lstm", "work")

    def lstm_forecast(self, issue):
        return self.p("lstm", "forecast", issue)

    def lstm_geotiff(self, issue):
        """Notebook C's TIFF_DIR: <scenario>/<County>/<County>_<INDEX>_<YYYY-MM>.tif."""
        return os.path.join(self.lstm_forecast(issue), "geotiff")

    # --- pest risk outputs -------------------------------------------------------
    @property
    def pest_nowcast(self):
        return self.p("pest", "nowcast")

    def pest_forecast(self, issue):
        return self.p("pest", "forecast", issue)

    @property
    def pest_training(self):
        return self.p("pest", "training")


_DATE_PATTERNS = [
    re.compile(r"(?<!\d)((?:19|20)\d{2})[_-](\d{2})[_-](\d{2})(?!\d)"),
    re.compile(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})(?!\d)"),
]


def date_in_name(name):
    for pat in _DATE_PATTERNS:
        m = pat.search(os.path.basename(name))
        if m:
            try:
                return dt.date(*map(int, m.groups()))
            except ValueError:
                return None
    return None


def month_in_name(name):
    m = re.search(r"(?<!\d)((?:19|20)\d{2})[_-](\d{2})(?!\d)", os.path.basename(name))
    if m and 1 <= int(m.group(2)) <= 12:
        return int(m.group(1)), int(m.group(2))
    return None


def month_range(start, end):
    """(year, month) pairs from ``start`` to ``end`` inclusive (dates or (y, m) tuples)."""
    y, m = (start.year, start.month) if hasattr(start, "year") else start
    y2, m2 = (end.year, end.month) if hasattr(end, "year") else end
    out = []
    while (y, m) <= (y2, m2):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def add_months(year, month, n):
    k = year * 12 + (month - 1) + n
    return k // 12, k % 12 + 1


def month_end(year, month):
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def issue_months(issue, n):
    """The ``n`` months a forecast issued in ``issue`` ("YYYY-MM") covers, starting with the issue month."""
    y, m = map(int, issue.split("-"))
    return [add_months(y, m, i) for i in range(n)]
