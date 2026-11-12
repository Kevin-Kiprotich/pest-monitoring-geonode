"""PestWatch settings. Each value can be overridden with a PESTWATCH_<NAME> Django setting."""
import os

from django.conf import settings

DEFAULTS = {
    "MAX_RASTER_BYTES": 100 * 1024 * 1024,
    # Pixel value -> label for classified rasters. Confirm with the EO team.
    "CLASSES": {
        1: "Very low",
        2: "Low",
        3: "Moderate",
        4: "High",
        5: "Very high",
    },
    "CLASS_COLORS": {
        1: "#1a9641",
        2: "#a6d96a",
        3: "#ffffbf",
        4: "#fdae61",
        5: "#d7191c",
    },
    # Values a classified raster may contain that are not classes. The pest
    # risk pipeline writes 0 for "no host" (outside the county or no crop);
    # these are accepted on upload and turned into nodata when published.
    "NO_DATA_CLASSES": [0],
    # Bins used to summarise hybrid (0-1) rasters.
    "HYBRID_BINS": [0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
    # County check: share of the county the raster must cover ...
    "COUNTY_MIN_COVERAGE": 0.9,
    # ... and share of the raster's extent that must fall within the county's
    # bounding box, grown by COUNTY_BBOX_MARGIN degrees.
    "COUNTY_MIN_INSIDE": 0.9,
    "COUNTY_BBOX_MARGIN": 0.02,
    # CRS rasters are published in. GeoServer cannot read some projections
    # (e.g. EASE-Grid 2.0, EPSG:6933), so uploads are reprojected to this one.
    "PUBLISH_SRS": "EPSG:4326",
    # Where uploads wait while they are published, and where job logs go.
    "STAGING_DIR": os.path.join(settings.MEDIA_ROOT, "pestwatch", "staging"),
    # Python used to run background jobs; defaults to the active virtualenv.
    "PYTHON": None,
    "MANAGE_PY": None,
    "MANAGERS_GROUP": "pestwatch-managers",

    # ---- model pipeline ---------------------------------------------------------
    # Where model inputs, outputs and run logs live (see pipeline/layout.py).
    "DATA_ROOT": os.getenv("PESTWATCH_DATA_ROOT", "/data/pestmodels"),
    # Python of the separate model environment (built by docker/pestmodels/Dockerfile).
    "MODELS_PYTHON": os.getenv("PESTWATCH_MODELS_PYTHON", "/opt/pestmodels/bin/python"),
    # Counties the models run for. None = every county that has pest models uploaded.
    "MODEL_COUNTIES": None,
    # Google Earth Engine service account (CHIRPS rainfall, ERA5-Land wind, satellite composites).
    "GEE_KEY_FILE": os.getenv("PESTWATCH_GEE_KEY_FILE", ""),
    "GEE_PROJECT": os.getenv("PESTWATCH_GEE_PROJECT", ""),
    # Copernicus Climate Data Store (ERA5 temperature and humidity, SEAS5).
    "CDS_URL": os.getenv("CDS_API_URL", "https://cds.climate.copernicus.eu/api"),
    "CDS_KEY": os.getenv("CDS_API_KEY", ""),
    # Observed weather: the archive starts here; each daily run re-checks the last WEATHER_WINDOW_DAYS.
    "WEATHER_HISTORY_START": "2020-01-01",
    "WEATHER_WINDOW_DAYS": 45,
    "ERA5_LAG_DAYS": 6,           # ERA5-Land daily statistics are about 5-6 days behind
    "RH_LAG_DAYS": 8,             # ERA5 single-level daily statistics
    "CHIRPS_REFRESH_DAYS": 0,     # re-download the last N CHIRPS days (they can be revised)
    # SEAS5 archive years used for the bias correction (from this year to the issue year).
    "SEAS5_ARCHIVE_START_YEAR": 2020,
    "FORECAST_MONTHS": 7,
    "FORECAST_MAP_DAY": 15,       # each forecast month is mapped for this day
    "CROP_SEASON": "Long_Rain2023",
    # Monthly satellite composites from Earth Engine (pipeline/composites.py). Each run fetches the
    # month that just ended and re-fetches the REFRESH months before it that Earth Engine made: their
    # gap filling borrows from later months (up to +/-3 months for the TOP10 recipe), which only
    # exist once those months are over. Uploaded files are never replaced.
    "COMPOSITE_REFRESH_MONTHS": 3,
    "COMPOSITE_THREADS": 4,       # parallel tile downloads (Earth Engine high-volume endpoint)
    # Recipe of the NDMI history: "median_qa" (the NDMI "monthly fast" notebook, with a QA band) or
    # "monthly_mean" (the TOP10 engine). Confirm with the modelling team.
    "NDMI_RECIPE": "median_qa",
    # When the system starts each pipeline on its own (UTC).
    "SCHEDULE": {
        "weather": {"hour": 3},                    # every day
        "composites": {"day": 3, "hour": 1},       # every month, for the month that just ended
        "nowcast": {"weekday": 0, "hour": 5},      # every Monday (0 = Monday)
        "forecast": {"day": 6, "hour": 6},         # every month, once SEAS5 is out (released on the 5th)
    },
    "RETRY_DELAY_HOURS": 6,       # a failed automatic run is retried after this long ...
    "MAX_ATTEMPTS": 3,            # ... up to this many attempts per period
    "STEP_TIMEOUT_HOURS": 36,
    "WORKER_POLL_SECONDS": 30,
    # Forecast layers of an older issue are deleted when a newer issue covers the same month.
    "KEEP_OLD_FORECASTS": False,
    "MODEL_USER": "pestwatch-models",
    "LSTM_CLEAN_TILE_CACHE": True,
}


def get(name):
    return getattr(settings, f"PESTWATCH_{name}", DEFAULTS[name])
