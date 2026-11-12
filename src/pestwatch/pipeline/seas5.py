"""Download the ECMWF SEAS5 seasonal forecast from the Copernicus CDS.

The zips received from the modelling team contain no SEAS5 download script, only
the files it produced. This module writes the same two products, in the formats
the bias-correction notebook reads:

1. The forecast of one start month, per county, stat and day:
   ``FORECASTED_WEATHER/<group>/<var>/<stat>/<County>_<var>_<stat>_<YYYY-MM-DD>.tif``
   on a 0.05 degree county grid. var: temp, tmax, tmin (deg C), rain (mm/day),
   rh (%), wind_speed (m/s), wind_dir (deg, direction the wind comes from).
   stat: ensemble mean, p10 and p90 (wind_dir: direction of the ensemble-mean wind).
2. The archive of past forecasts with the same start month, one file per year:
   ``SEAS5_ARCHIVE/daily/SEAS5_daily_<YYYY-MM>.nc`` with temp_c, rh_pct and
   wind_speed per ensemble member (dims number, date, latitude, longitude).

Daily values: mean of the four 6-hourly values ending 06, 12, 18 and 24 UTC;
rain = difference of the accumulated total precipitation at 24 h steps; tmax /
tmin = the 24-hour maximum / minimum fields; RH from the daily-mean temperature
and dewpoint (Magnus 17.625 / 243.04, the observed RH uses the same method).

ASSUMPTIONS to confirm with the modelling team: the aggregation above, the
0.05 degree bilinear regrid, and the CDS "system" codes (SYSTEMS below).
"""
import datetime as dt
import os
import shutil

import numpy as np

LEAD_DAYS = 215                      # SEAS5 runs 7 months (5160 h)
SIX_HOURLY = ["2m_temperature", "2m_dewpoint_temperature", "10m_u_component_of_wind",
              "10m_v_component_of_wind", "total_precipitation"]
DAILY = ["maximum_2m_temperature_in_the_last_24_hours", "minimum_2m_temperature_in_the_last_24_hours"]
SHORT = {"2m_temperature": "t2m", "2m_dewpoint_temperature": "d2m", "10m_u_component_of_wind": "u10",
         "10m_v_component_of_wind": "v10", "total_precipitation": "tp",
         "maximum_2m_temperature_in_the_last_24_hours": "mx2t24",
         "minimum_2m_temperature_in_the_last_24_hours": "mn2t24"}
# CDS "system" of the ECMWF forecast by start month (newest first). Tried in order if one is rejected.
SYSTEMS = [("2022-11", ["51", "5"]), ("0000-00", ["5", "51"])]
GRID_DEG = 0.05
STATS = ("mean", "p10", "p90")
LAYERS = [("temperature", "temp"), ("temperature", "tmax"), ("temperature", "tmin"), ("rainfall", "rain"),
          ("humidity", "rh"), ("wind", "wind_speed"), ("wind", "wind_dir")]


def es(tc):
    return 6.1094 * np.exp(17.625 * tc / (tc + 243.04))


def systems_for(year, month):
    key = f"{year:04d}-{month:02d}"
    for start, codes in SYSTEMS:
        if key >= start:
            return codes
    return SYSTEMS[-1][1]


def request_area(bounds, pad=2.0):
    """CDS area [N, W, S, E] around (minx, miny, maxx, maxy), on whole degrees."""
    minx, miny, maxx, maxy = bounds
    return [float(np.ceil(maxy + pad)), float(np.floor(minx - pad)), float(np.floor(miny - pad)), float(np.ceil(maxx + pad))]


def retrieve(year, month, variables, step_h, area, dest, log=print):
    """One CDS request (all members, all lead times). Skipped if ``dest`` exists."""
    if os.path.exists(dest):
        return dest
    import cdsapi
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    last_error = None
    for system in systems_for(year, month):
        req = {"originating_centre": "ecmwf", "system": system, "variable": variables,
               "year": [f"{year:04d}"], "month": [f"{month:02d}"], "day": ["01"],
               "leadtime_hour": [str(h) for h in range(step_h, LEAD_DAYS * 24 + 1, step_h)],
               "area": area, "data_format": "netcdf"}
        log(f"SEAS5 {year}-{month:02d} system {system}: requesting {', '.join(SHORT[v] for v in variables)} "
            f"every {step_h} h (the CDS queue can take a while)")
        try:
            cdsapi.Client(quiet=True).retrieve("seasonal-original-single-levels", req, dest + ".part")
        except Exception as e:                                   # wrong system code, or not released yet
            last_error = e
            log(f"  rejected: {str(e)[:300]}")
            if os.path.exists(dest + ".part"):
                os.remove(dest + ".part")
            continue
        _unpack(dest + ".part", dest)
        return dest
    raise RuntimeError(f"SEAS5 {year}-{month:02d} could not be downloaded: {last_error}")


def _unpack(part, dest):
    """The CDS sometimes delivers a zip of NetCDF files; keep a single NetCDF."""
    import zipfile
    if zipfile.is_zipfile(part):
        import xarray as xr
        tmp = part + "_unzipped"
        with zipfile.ZipFile(part) as z:
            z.extractall(tmp)
        files = sorted(os.path.join(tmp, f) for f in os.listdir(tmp) if f.endswith(".nc"))
        xr.merge([xr.open_dataset(f) for f in files], compat="override").to_netcdf(dest)
        shutil.rmtree(tmp)
        os.remove(part)
    else:
        os.replace(part, dest)


def open_fields(path, start):
    """(fields {short: array[member, lead, lat, lon]}, lead hours, lat, lon) from a CDS NetCDF."""
    import xarray as xr
    with xr.open_dataset(path) as ds:
        ds = ds.load()
    for d in ("forecast_reference_time", "time"):
        if d in ds.dims and ds.sizes[d] == 1:
            ds = ds.isel({d: 0})
    lead = next((d for d in ("forecast_period", "step", "leadtime", "lead_time") if d in ds.dims), None)
    if lead is None:
        raise RuntimeError(f"{path}: no lead-time dimension in {dict(ds.sizes)}")
    hours = ds[lead].values
    hours = (hours / np.timedelta64(1, "h")).astype(int) if np.issubdtype(hours.dtype, np.timedelta64) else hours.astype(int)
    lat = ds["latitude"].values
    lon = ds["longitude"].values
    out = {}
    for name in ds.data_vars:
        v = ds[name]
        if "number" not in v.dims:
            v = v.expand_dims("number")
        out[name] = v.transpose("number", lead, "latitude", "longitude").values.astype("float32")
    return out, hours, lat, lon


def daily_fields(six, six_h, dly=None, dly_h=None):
    """Daily arrays [member, day, lat, lon] in deg C, %, mm/day and m/s."""
    n_days = int(six_h.max() // 24)
    def by_day(a, hours, reduce):
        days = []
        for k in range(n_days):
            sel = (hours > 24 * k) & (hours <= 24 * (k + 1))
            days.append(reduce(a[:, sel]) if sel.any() else np.full(a[:, 0].shape, np.nan, "float32"))
        return np.stack(days, 1)
    mean = lambda a: np.nanmean(a, 1)
    t = by_day(six["t2m"] - 273.15, six_h, mean)
    td = by_day(six["d2m"] - 273.15, six_h, mean)
    u = by_day(six["u10"], six_h, mean)
    v = by_day(six["v10"], six_h, mean)
    speed = by_day(np.hypot(six["u10"], six["v10"]), six_h, mean)
    out = dict(temp=t, rh=np.clip(100 * es(td) / es(t), 0, 100).astype("float32"), wind_speed=speed, u=u, v=v)
    if "tp" in six:
        at = {int(h): i for i, h in enumerate(six_h)}
        acc = [np.zeros(six["tp"][:, 0].shape, "float32")] + [six["tp"][:, at[24 * k]] if 24 * k in at else
                                                               np.full(six["tp"][:, 0].shape, np.nan, "float32")
                                                               for k in range(1, n_days + 1)]
        acc = np.stack(acc, 1)
        out["rain"] = np.clip(np.diff(acc, axis=1) * 1000.0, 0, None).astype("float32")
    if dly is not None:
        at = {int(h): i for i, h in enumerate(dly_h)}
        for key, name in (("tmax", "mx2t24"), ("tmin", "mn2t24")):
            arr = dly[name]
            out[key] = np.stack([arr[:, at[24 * (k + 1)]] - 273.15 if 24 * (k + 1) in at else
                                 np.full(arr[:, 0].shape, np.nan, "float32") for k in range(n_days)], 1)
    return out


def write_archive(path, start, d, lat, lon):
    import pandas as pd
    import xarray as xr
    dates = pd.date_range(start, periods=d["temp"].shape[1], freq="D")
    coords = dict(number=np.arange(d["temp"].shape[0]), date=dates, latitude=lat, longitude=lon)
    dims = ("number", "date", "latitude", "longitude")
    ds = xr.Dataset({"temp_c": (dims, d["temp"]), "rh_pct": (dims, d["rh"]), "wind_speed": (dims, d["wind_speed"])},
                    coords=coords)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ds.to_netcdf(path + ".part")
    os.replace(path + ".part", path)


class CountyGrid:
    """0.05 degree grid over a county, with the cells outside the county set to NaN."""

    def __init__(self, geom, bounds):
        from rasterio import features
        from rasterio.transform import Affine
        minx, miny, maxx, maxy = bounds
        r = GRID_DEG
        x0, y1 = np.floor(minx / r) * r - r, np.ceil(maxy / r) * r + r
        x1, y0 = np.ceil(maxx / r) * r + r, np.floor(miny / r) * r - r
        self.width, self.height = int(round((x1 - x0) / r)), int(round((y1 - y0) / r))
        self.transform = Affine(r, 0, x0, 0, -r, y1)
        self.inside = features.rasterize([(geom, 1)], out_shape=(self.height, self.width), transform=self.transform,
                                         fill=0, all_touched=True, dtype="uint8").astype(bool)

    def regrid(self, a, lat, lon):
        from rasterio.transform import Affine
        from rasterio.warp import Resampling, reproject
        if lat[0] < lat[-1]:
            a, lat = a[::-1], lat[::-1]
        dx, dy = abs(float(lon[1] - lon[0])), abs(float(lat[1] - lat[0]))
        src_tf = Affine(dx, 0, float(lon.min()) - dx / 2, 0, -dy, float(lat.max()) + dy / 2)
        out = np.full((self.height, self.width), np.nan, "float32")
        reproject(np.ascontiguousarray(a, dtype="float32"), out, src_transform=src_tf, src_crs="EPSG:4326",
                  dst_transform=self.transform, dst_crs="EPSG:4326", resampling=Resampling.bilinear,
                  src_nodata=np.nan, dst_nodata=np.nan)
        return np.where(self.inside, out, np.nan).astype("float32")

    def write(self, path, a, tags):
        import rasterio
        os.makedirs(os.path.dirname(path), exist_ok=True)
        prof = dict(driver="GTiff", height=self.height, width=self.width, count=1, dtype="float32", crs="EPSG:4326",
                    transform=self.transform, nodata=np.nan, compress="deflate")
        with rasterio.open(path + ".part", "w", **prof) as dst:
            dst.write(a.astype("float32"), 1)
            dst.update_tags(**tags)
        os.replace(path + ".part", path)


def fetch(layout, counties, boundaries, issue, archive_years, log=print):
    """Download the forecast that starts on the first day of ``issue`` ("YYYY-MM") and its archive."""
    import geopandas as gpd
    year, month = map(int, issue.split("-"))
    start = dt.date(year, month, 1)
    g = gpd.read_file(boundaries).to_crs("EPSG:4326")
    g["_n"] = g["adm1_name"].astype(str).str.strip().str.lower()
    sel = {c: g[g["_n"] == c.lower()] for c in counties}
    missing = [c for c, s in sel.items() if s.empty]
    if missing:
        raise RuntimeError(f"Counties not in the boundary file: {', '.join(missing)}")
    area = request_area(g[g["_n"].isin([c.lower() for c in counties])].total_bounds)
    raw = layout.raw("seas5")

    # 1. archive: the same start month in earlier years (and this year, as the notebook does)
    archive = lambda y: os.path.join(layout.seas5_archive, f"SEAS5_daily_{y:04d}-{month:02d}.nc")
    for y in archive_years:
        path = archive(y)
        if y == year or os.path.exists(path):
            continue
        nc = retrieve(y, month, SIX_HOURLY[:4], 6, area, os.path.join(raw, f"{y:04d}-{month:02d}_6h_noprecip.nc"), log)
        six, h, lat, lon = open_fields(nc, dt.date(y, month, 1))
        write_archive(path, dt.date(y, month, 1), daily_fields(six, h), lat, lon)
        log(f"archive written: {path}")

    # 2. this month's forecast, per county, stat and day
    nc6 = retrieve(year, month, SIX_HOURLY, 6, area, os.path.join(raw, f"{issue}_6h.nc"), log)
    nc24 = retrieve(year, month, DAILY, 24, area, os.path.join(raw, f"{issue}_24h.nc"), log)
    six, h6, lat, lon = open_fields(nc6, start)
    dly, h24, _, _ = open_fields(nc24, start)
    d = daily_fields(six, h6, dly, h24)
    n_days = d["temp"].shape[1]
    if year in archive_years and not os.path.exists(archive(year)):
        write_archive(archive(year), start, d, lat, lon)
    stats = {}
    for key in ("temp", "tmax", "tmin", "rain", "rh", "wind_speed"):
        a = d[key]
        stats[key] = {"mean": np.nanmean(a, 0), "p10": np.nanpercentile(a, 10, axis=0), "p90": np.nanpercentile(a, 90, axis=0)}
    u, v = np.nanmean(d["u"], 0), np.nanmean(d["v"], 0)
    wdir = ((180.0 + np.degrees(np.arctan2(u, v))) % 360.0).astype("float32")     # meteorological "from", as the wind notebook
    written = 0
    for county in counties:
        geom = sel[county].geometry.union_all() if hasattr(sel[county].geometry, "union_all") else sel[county].geometry.unary_union
        grid = CountyGrid(geom, sel[county].total_bounds)
        for k in range(n_days):
            day = start + dt.timedelta(days=k)
            for group, var in LAYERS:
                for stat in STATS:
                    if var == "wind_dir":
                        # direction is circular: regrid sin and cos, not degrees
                        s = grid.regrid(np.sin(np.radians(wdir[k])), lat, lon)
                        c = grid.regrid(np.cos(np.radians(wdir[k])), lat, lon)
                        a = ((np.degrees(np.arctan2(s, c)) + 360.0) % 360.0).astype("float32")
                    else:
                        a = grid.regrid(stats[var][stat][k], lat, lon)
                    grid.write(layout.fc_file(layout.fc_raw, group, var, stat, county, day), a,
                               dict(variable=var, stat=stat, source=f"ECMWF SEAS5 {issue}", lead_day=str(k)))
                    written += 1
        log(f"{county}: {n_days} forecast days written ({start} to {start + dt.timedelta(days=n_days - 1)})")
    return dict(issue=issue, days=n_days, files=written, first_day=str(start),
                last_day=str(start + dt.timedelta(days=n_days - 1)))
