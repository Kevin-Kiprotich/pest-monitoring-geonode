"""The pipeline steps, run one at a time by ``stepper.py`` in the model environment.

Each step receives the spec written by the orchestrator (Django side) and
returns a dict that is saved as the step's result. Steps never touch the
database or GeoServer; publishing happens on the Django side.

Most steps run cells of the modelling team's notebooks (``notebooks/``) with
the folders, counties and dates replaced (see ``nbrun``). The cell lists below
skip the cells that only plot or check. When a notebook is updated, check that
these cell numbers still point at the same cells.
"""
import datetime as dt
import glob
import json
import os
import shutil

import composites
import nbrun
import seas5
from layout import Layout, PEST_ORDER, add_months, cwv_folder, issue_months, month_end, month_range

HERE = os.path.dirname(os.path.abspath(__file__))
NOTEBOOKS = os.path.join(HERE, "notebooks")
STEPS = {}


class StepSkipped(Exception):
    """Nothing to do (e.g. no new data). The step counts as done."""


def step(name):
    def register(fn):
        STEPS[name] = fn
        return fn
    return register


def nb(name):
    return os.path.join(NOTEBOOKS, name + ".ipynb")


def log(*a):
    print(*a, flush=True)


def day(s):
    return dt.date.fromisoformat(s) if isinstance(s, str) else s


def new_period(spec):
    """The run's (start, end), with the start moved to AUTO_FETCH_FROM: earlier days are the uploaded history."""
    start, end = day(spec["start"]), day(spec["end"])
    if spec.get("auto_fetch_from"):
        first = day(spec["auto_fetch_from"])
        if start < first:
            log(f"Days before {first} are the uploaded history: downloading from {first}")
            start = first
    if end < start:
        raise StepSkipped(f"Nothing to download: days before {start} are the uploaded history.")
    return start, end


def run(spec):
    fn = STEPS.get(spec["step"])
    if fn is None:
        raise ValueError(f"Unknown step {spec['step']!r}")
    return fn(spec, Layout(spec["root"]))


# --- credentials ----------------------------------------------------------------

def gee_init(spec):
    """Initialise Earth Engine with the service account, then make the notebooks' own
    ee.Authenticate / ee.Initialize calls no-ops (they expect an interactive login)."""
    import ee
    key = spec.get("gee_key_file")
    if not key or not os.path.exists(key):
        raise RuntimeError("No Google Earth Engine key. Set PESTWATCH_GEE_KEY_FILE to the service-account JSON "
                           "and PESTWATCH_GEE_PROJECT to its Cloud project.")
    with open(key) as f:
        email = json.load(f)["client_email"]
    ee.Initialize(ee.ServiceAccountCredentials(email, key), project=spec.get("gee_project") or None)
    ee.Authenticate = lambda *a, **k: True
    ee.Initialize = lambda *a, **k: None
    log(f"Earth Engine initialised as {email}")


def cds_check():
    if not os.environ.get("CDSAPI_KEY"):
        raise RuntimeError("No CDS key. Set CDS_API_KEY (and CDS_API_URL) in .env.")


def cds_trim_days(cutoff, first=None):
    """Drop requested days after ``cutoff`` (and before ``first``) from CDS requests: the ERA5
    notebook asks for days 1-31 of every month, which fails for the current month and would
    re-download uploaded history days in the first month."""
    import cdsapi
    original = cdsapi.Client.retrieve

    def retrieve(self, name, request, *args, **kwargs):
        if isinstance(request, dict) and {"year", "month", "day"} <= set(request):
            y, m = int(str(request["year"]).strip("[]'\" ")), int(str(request["month"]).strip("[]'\" "))
            days = [d for d in request["day"] if _valid_day(y, m, int(d)) and dt.date(y, m, int(d)) <= cutoff
                    and (first is None or dt.date(y, m, int(d)) >= first)]
            if not days:
                raise RuntimeError(f"no days of {y}-{m:02d} are available yet (cut-off {cutoff})")
            request = dict(request, day=days)
        return original(self, name, request, *args, **kwargs)
    cdsapi.Client.retrieve = retrieve


def _valid_day(y, m, d):
    try:
        dt.date(y, m, d)
        return True
    except ValueError:
        return False


def check_days(layout, var, counties, start, end, label):
    """Raise if no county got any file for the period; report gaps otherwise."""
    report, total = {}, 0
    for c in counties:
        have = layout.wx_days(var, c)
        want = {start + dt.timedelta(days=i) for i in range((end - start).days + 1)}
        missing = sorted(want - have)
        total += len(want) - len(missing)
        report[c] = dict(last=str(max(have)) if have else None, missing=len(missing),
                         first_missing=str(missing[0]) if missing else None)
    log(f"{label}: {json.dumps(report)}")
    if total == 0 and end >= start:
        raise RuntimeError(f"{label}: no daily files were written for {start} to {end}. See the log above.")
    return report


# --- observed weather -----------------------------------------------------------

@step("fetch_chirps")
def fetch_chirps(spec, L):
    start, end = new_period(spec)
    counties = spec["counties"]
    for i in range(int(spec.get("refresh_days") or 0)):          # re-download days CHIRPS may have revised
        d = end - dt.timedelta(days=i)
        if d < start:                                            # never the uploaded history
            break
        for sub in ("county_daily_direct", "clipped_daily", "filled_daily"):
            for c in counties:
                f = os.path.join(L.chirps_base, sub, c, f"CHIRPS_{c}_{d:%Y_%m_%d}.tif")
                if os.path.exists(f):
                    os.remove(f)
    gee_init(spec)
    nbrun.run(nb("chirps"), cells=[3], params=dict(
        EE_PROJECT_ID=spec.get("gee_project") or "", SHAPEFILE_PATH=L.boundaries, NAME_COLUMN="adm1_name",
        COUNTIES=counties, OUTPUT_DIR=L.chirps_base, START_YEAR=start.year, END_YEAR=end.year,
        # The notebook processes from 1 January of START_YEAR; start on the run's first day instead.
        DATE_RANGE=nbrun.Expr(f"pd.date_range('{start}', _end, freq='D')"),
        PLOT_SAMPLE_AFTER_RUN=False, RUN_LEGACY_NODATA_REPAIR=False, FORCE_REBUILD_COUNTY_OUTPUTS=False,
        MAX_WORKERS=4))
    return dict(coverage=check_days(L, "precip", counties, start, min(end, _last_any(L, "precip", counties, end)),
                                    "CHIRPS rainfall"))


def _last_any(L, var, counties, default):
    last = [max(L.wx_days(var, c)) for c in counties if L.wx_days(var, c)]
    return max(last) if last else default


@step("fetch_era5_temp")
def fetch_era5_temp(spec, L):
    cds_check()
    start, end = new_period(spec)
    cutoff = min(end, dt.date.today() - dt.timedelta(days=int(spec.get("era5_lag_days", 6))))
    if cutoff < start:
        raise StepSkipped(f"ERA5-Land is not available after {cutoff} yet")
    cds_trim_days(cutoff, first=start)
    work = os.path.join(spec["run_dir"], "era5_temp")
    os.makedirs(work, exist_ok=True)
    # Write straight into the folder the models read. The models' "ERA5_LAND_TEMP_fixed" step
    # is not in the zips; the files written here are already deg C on the ERA5-Land grid.
    ns = nbrun.run(nb("era5_temp"), cells=[1, 3], params=dict(
        output_base=L.era5_temp_base, shp_path=L.boundaries, target_counties=spec["counties"],
        years=[], months=[], csv_path=os.path.join(work, "daily_tmax_tmin.csv"),
        log_path=os.path.join(work, "extraction_log.txt")))
    for y, m in month_range(start, cutoff):
        ns["years"], ns["months"] = [y], [m]
        nbrun.run(nb("era5_temp"), cells=[5], ns=ns)
    return dict(tmax=check_days(L, "tmax", spec["counties"], start, cutoff, "ERA5-Land tmax"),
                tmin=check_days(L, "tmin", spec["counties"], start, cutoff, "ERA5-Land tmin"))


@step("fetch_era5_rh")
def fetch_era5_rh(spec, L):
    cds_check()
    start, end = new_period(spec)
    cutoff = min(end, dt.date.today() - dt.timedelta(days=int(spec.get("rh_lag_days", 8))))
    if cutoff < start:
        raise StepSkipped(f"ERA5 is not available after {cutoff} yet")
    work = L.raw("era5_rh")
    counties = spec["counties"]
    for c in counties:                     # the notebook skips existing files: clear the period so it is rebuilt
        for f in glob.glob(os.path.join(work, c, "daily_tifs", f"{c}_RH_*.tif")):
            d = _date(f)
            if d and start <= d <= cutoff:
                os.remove(f)
    nbrun.run(nb("era5_rh"), cells=[4, 6, 8, 10, 12, 14, 16, 18], params=dict(
        SHAPEFILE_PATH=L.boundaries, NAME_COLUMN="adm1_name", COUNTIES=counties,
        START_DATE=str(start), END_DATE=str(cutoff), RH_METHOD="daily_mean", MAX_PARALLEL_REQUESTS=2,
        OUT_DIR=work, SAVE_NETCDF=False, COMPARE_ROOT=os.path.join(work, "_no_comparison")))
    # The notebook writes <County>/daily_tifs; the models read DAILY_RH/RH_<COUNTY>/daily_tifs
    # (same file names, with hyphens: the notebook's rename-to-underscores cell is NOT run).
    for c in counties:
        dst = L.wx_dir("rh", c)
        os.makedirs(dst, exist_ok=True)
        for f in glob.glob(os.path.join(work, c, "daily_tifs", f"{c}_RH_*.tif")):
            d = _date(f)
            if d and start <= d <= cutoff:
                shutil.copy2(f, os.path.join(dst, os.path.basename(f)))
    return dict(rh=check_days(L, "rh", counties, start, cutoff, "ERA5 RH"))


def _date(path):
    from layout import date_in_name
    return date_in_name(path)


@step("fetch_era5_wind")
def fetch_era5_wind(spec, L):
    start, end = new_period(spec)
    gee_init(spec)
    nbrun.run(nb("era5_wind"), cells=[2], params=dict(
        EE_PROJECT=spec.get("gee_project") or "", SHAPEFILE_PATH=L.boundaries, NAME_COLUMN="adm1_name",
        COUNTIES=spec["counties"], OUTPUT_DIR=L.wind_base, START_DATE=str(start), END_DATE=str(end),
        MAX_WORKERS=4, FORCE_REBUILD_COUNTY_OUTPUTS=False, PLOT_SAMPLE_AFTER_RUN=False))
    last = _last_any(L, "wind", spec["counties"], end)
    return dict(wind=check_days(L, "wind", spec["counties"], start, min(end, last), "ERA5-Land wind"))


# --- monthly satellite composites (Earth Engine) -----------------------------------------

def _composite_counties(spec, L):
    """The run's counties plus the stored LSTM's: it needs the composites of all its counties."""
    counties = list(spec["counties"])
    if os.path.exists(L.lstm_model):
        counties += [c for c in _lstm_cfg(L)["COUNTIES"] if c not in counties]
    return counties


def _fetch_composites(spec, L, step_name):
    keys = [k for k in composites.STEP_PRODUCTS[step_name] if not spec.get("products") or k in spec["products"]]
    if not keys:
        raise StepSkipped("Not selected for this run.")
    report = composites.run(
        L, keys, _composite_counties(spec, L), spec.get("months") or [], spec.get("refresh") or [],
        force=bool(spec.get("force")), key_file=spec.get("gee_key_file"), project=spec.get("gee_project"),
        threads=int(spec.get("composite_threads") or 4), ndmi_recipe=spec.get("ndmi_recipe") or "median_qa",
        done_file=os.path.join(spec["run_dir"], f"{step_name}.done.json"),
        fetch_from=spec.get("auto_fetch_from") or "", log=log)
    counts = {k: len(v) for k, v in report.items() if v}
    log(f"{step_name}: {json.dumps(counts)}")
    for k in ("low_coverage", "no_scenes", "no_boundary", "failed"):
        for item in report[k][:50]:
            log(f"  {k}: {item}")
    if report["failed"]:
        raise RuntimeError(f"{len(report['failed'])} composite(s) could not be downloaded (finished ones are kept; "
                           f"retry the run): {report['failed'][0]}")
    if not report["written"]:
        raise StepSkipped("Nothing new: " + (", ".join(f"{n} {k.replace('_', ' ')}" for k, n in counts.items())
                                             or "no months to fetch"))
    return dict(counts=counts, written=report["written"], low_coverage=report["low_coverage"],
                no_boundary=report["no_boundary"])


@step("fetch_s2_bands")
def fetch_s2_bands(spec, L):
    return _fetch_composites(spec, L, "fetch_s2_bands")


@step("fetch_s2_cwv")
def fetch_s2_cwv(spec, L):
    return _fetch_composites(spec, L, "fetch_s2_cwv")


@step("fetch_landsat")
def fetch_landsat(spec, L):
    return _fetch_composites(spec, L, "fetch_landsat")


@step("fetch_indices")
def fetch_indices(spec, L):
    return _fetch_composites(spec, L, "fetch_indices")


# --- forecast weather -------------------------------------------------------------

def _issue_dates(spec):
    y, m = map(int, spec["issue"].split("-"))
    first = dt.date(y, m, 1)
    return first, first + dt.timedelta(days=seas5.LEAD_DAYS - 1)


@step("fetch_seas5")
def fetch_seas5(spec, L):
    cds_check()
    y, _ = map(int, spec["issue"].split("-"))
    years = list(range(int(spec.get("archive_start_year", 2020)), y + 1))
    return seas5.fetch(L, spec["counties"], L.boundaries, spec["issue"], years,
                       history_before=day(spec["auto_fetch_from"]) if spec.get("auto_fetch_from") else None, log=log)


@step("bias_correct")
def bias_correct(spec, L):
    first, last = _issue_dates(spec)
    y, m = map(int, spec["issue"].split("-"))
    nbrun.run(nb("seas5_bias_correction"), cells=[1, 3, 5, 7, 13], params=dict(
        COUNTIES=spec["counties"], STATS=["mean", "p10", "p90"], IN_DIR=L.fc_raw, OUT_DIR=L.fc_bc,
        ARCHIVE_DIR=L.seas5_archive, DATE_START=str(first), DATE_END=str(last), START_MONTHS=[m],
        YEARS=nbrun.Expr(f"range({int(spec.get('archive_start_year', 2020))}, {y + 1})"),
        HIST_ROOT=L.hist, DEM_PATH=L.dem_pattern, SKIP_EXISTING=False))
    return dict(first_day=str(first), last_day=str(last))


@step("downscale_30m")
def downscale_30m(spec, L):
    first, last = _issue_dates(spec)
    nbrun.run(nb("forecast_downscale_30m"), cells=[1, 3, 5, 6], params=dict(
        COUNTIES=spec["counties"], STATS=["mean"], IN_DIR=L.fc_bc, OUT_DIR=L.fc_30m, DEM_PATH=L.dem_pattern,
        S2_PATH=L.s2_template_pattern, LANDSAT_PATH=L.landsat_template_pattern, GRID_FROM="landsat",
        DATE_START=str(first), DATE_END=str(last), SKIP_EXISTING=True))
    return {}


# --- LSTM vegetation index forecast ----------------------------------------------

def _lstm_end_month(spec):
    """Last observed month = the month before the forecast starts."""
    y, m = map(int, spec["issue"].split("-"))
    return add_months(y, m, -1)


def _cwv_dirs(counties):
    return {c: cwv_folder(c) for c in counties}


# The LSTM is not retrained: the stored final_model.pt (L.lstm_model) is fed the latest inputs.
# Notebooks A and A2 rebuild the dataset and the stores up to the month before the forecast;
# Notebook C runs the stored model on them.

def _lstm_cfg(L):
    """The stored model's settings (counties, months, ...), read from final_model.pt."""
    import torch
    if not os.path.exists(L.lstm_model):
        raise RuntimeError(f"No LSTM model at {L.lstm_model}: put the trained final_model.pt there.")
    return torch.load(L.lstm_model, map_location="cpu", weights_only=False)["cfg"]


def _extend_model_months(art, data_npz):
    """Let the stored model run on months after the ones it was trained on.

    Notebook C requires the dataset and the stores to cover exactly the model's months
    (cfg["MONTHS"]), which only holds when the model is retrained every cycle. The model's
    climatology and normalisation only use months up to cfg["clim_end"], so newer months
    change nothing it learned: they are only the encoder's recent history. The model's
    months are extended with the dataset's, provided the dataset starts with exactly the
    months the model was trained on.
    """
    import numpy as np
    trained = [str(m) for m in art["cfg"]["MONTHS"]]
    with np.load(data_npz, allow_pickle=False) as D:
        months = [str(m) for m in D["months"]]
    if months[:len(trained)] != trained:
        raise RuntimeError(f"The LSTM dataset covers {months[0]}..{months[-1]}, but the stored model needs it to start "
                           f"with its training months {trained[0]}..{trained[-1]} (clim_end {art['cfg']['clim_end']}).")
    log(f"Stored LSTM (trained {art['cfg']['trained']} on {trained[0]}..{trained[-1]}) "
        f"fed {len(months) - len(trained)} newer month(s), up to {months[-1]}")
    art["cfg"]["MONTHS"] = months
    return art


@step("lstm_dataset")
def lstm_dataset(spec, L):
    cfg = _lstm_cfg(L)
    ey, em = _lstm_end_month(spec)
    end = f"{ey:04d}-{em:02d}"
    if end < cfg["MONTHS"][-1]:
        raise RuntimeError(f"The forecast issued {spec['issue']} starts before the end of the stored model's "
                           f"training months ({cfg['MONTHS'][-1]}).")
    os.makedirs(L.lstm_work, exist_ok=True)
    # The model's counties, in its order: its county one-hot and climatology groups depend on it.
    counties = list(cfg["COUNTIES"])
    nbrun.run(nb("lstm_a_dataset"), cells=[1, 3, 5, 7, 9, 12, 14, 18], params=dict(
        COUNTIES=counties, VI_ROOT=L.data, CWV_DIR=_cwv_dirs(counties), HIST_ROOT=L.hist,
        IN_DIR=L.fc_bc, FC30_DIR=L.fc_30m, DEM_PATH=L.dem_pattern, WORK_DIR=L.lstm_work,
        START_MONTH=cfg["MONTHS"][0], END_MONTH=end))
    return dict(end_month=end, counties=counties)


@step("lstm_stores")
def lstm_stores(spec, L):
    counties = list(_lstm_cfg(L)["COUNTIES"])
    nbrun.run(nb("lstm_a2_stores"), cells=[1, 3, 5, 7, 8, 10, 13, 15], params=dict(
        WORK_DIR=L.lstm_work, VI_ROOT=L.data, CWV_DIR=_cwv_dirs(counties), HIST_ROOT=L.hist,
        IN_DIR=L.fc_bc, SCENARIOS=["mean"], REBUILD_COARSE=True))
    return {}


@step("lstm_train")
def lstm_train(spec, L):
    # No longer part of the forecast pipeline; kept so runs queued before the change still finish.
    raise StepSkipped(f"The stored LSTM model ({L.lstm_model}) is used; it is not retrained.")


@step("lstm_forecast")
def lstm_forecast(spec, L):
    model_counties = list(_lstm_cfg(L)["COUNTIES"])
    counties = [c for c in spec["counties"] if c in model_counties]
    if not counties:
        raise RuntimeError(f"The stored LSTM covers {', '.join(model_counties)}; none of {', '.join(spec['counties'])}.")
    out = L.lstm_forecast(spec["issue"])
    os.makedirs(out, exist_ok=True)
    ns = nbrun.new_namespace(nb("lstm_c_forecast"))
    ns["_extend_model_months"] = _extend_model_months
    nbrun.run(nb("lstm_c_forecast"), cells=[1, 3, 5, 7, 9, 11, 13, 15, 20, 22], ns=ns, params=dict(
        WORK_DIR=L.lstm_work, MODEL_PATH=L.lstm_model, SKILL_CSV=L.lstm_skill,
        art=nbrun.Expr("_extend_model_months(art, DATA_NPZ)"),
        OUT_DIR=out, TILE_CACHE=os.path.join(out, "tile_cache"), FULL_SCENARIOS=["mean"],
        COUNTIES_TO_RUN=counties, FORCE_RECOMPUTE=False, DELETE_TILE_CACHE=False))
    tiffs = glob.glob(os.path.join(L.lstm_geotiff(spec["issue"]), "mean", "*", "*.tif"))
    if not tiffs:
        raise RuntimeError(f"Notebook C wrote no GeoTIFFs to {L.lstm_geotiff(spec['issue'])}")
    if spec.get("clean_tile_cache", True):
        shutil.rmtree(os.path.join(out, "tile_cache"), ignore_errors=True)
    return dict(geotiffs=len(tiffs), folder=L.lstm_geotiff(spec["issue"]))


# --- pest risk ---------------------------------------------------------------------

PEST_CELLS = [4, 6, 8, 10, 12, 14, 16, 18, 20, 21, 23, 25, 27, 29, 33, 35, 37, 39, 41, 43, 45, 47, 59]


def load_pest_pipeline(spec, L, out_root):
    """Run the definition cells of the pest notebook; return its namespace (cfg, functions)."""
    os.environ.pop("PEST_CFG_OVERRIDE", None)
    p = {f"cfg.{k}": v for k, v in dict(
        root=L.root, h_root=L.data, v_root=L.data, inc_root=L.incidence_root, boundary_dir=os.path.dirname(L.boundaries),
        county_shp=L.boundaries, county_name_col="adm1_name", out_root=out_root, counties=spec["counties"],
        fc_root=L.fc_bc, crop_season=spec.get("crop_season", "Long_Rain2023"), cores=spec.get("cores"),
        plot_products=[], export_products=["hybrid", "risk_class"], use_gpu=bool(spec.get("use_gpu", False)),
        uncovered_mode="hybrid").items()}
    p["CWV_FOLDER"] = _cwv_dirs(spec["counties"])
    # cell 59 is a forecast run cell; only its memory-friendly wx_to_grid is taken.
    return nbrun.run(nb("pest_risk"), cells=PEST_CELLS, params=p, only={59: ["wx_to_grid"]})


def _clear_wx_cache(ns, county, tag):
    for f in glob.glob(os.path.join(ns["out_dir"](county, "wx"), f"wx_{tag}*.tif")):
        os.remove(f)


def _collect(maps, county, kind, valid_from, valid_to, map_date):
    out = []
    for pest, files in maps.items():
        if "hybrid" in files and "risk_class" in files:
            out.append(dict(county=county, pest=pest, kind=kind, map_date=map_date, valid_from=valid_from,
                            valid_to=valid_to, hybrid=files["hybrid"], classified=files["risk_class"]))
    return out


@step("pest_nowcast")
def pest_nowcast(spec, L):
    ns = load_pest_pipeline(spec, L, L.pest_nowcast)
    cfg = ns["cfg"]
    cfg.vi_fc_months, cfg.fc_only_vars, cfg.fc_scenario = [], ["precip"], "mean"
    outputs, notes = [], []
    for county in spec["counties"]:
        pests = L.pests_with_models(county)
        if not pests:
            notes.append(f"{county}: no pest models uploaded, skipped")
            continue
        cfg.pests = pests
        cfg.use_forecast = False
        ends = {}
        for v in ("tmax", "tmin", "rh", "wind"):
            x = ns["read_wx_var"](county, v)
            ends[v] = x["dates"].max().date() if x is not None else None
        if None in ends.values():
            notes.append(f"{county}: no observed {', '.join(k for k, v in ends.items() if v is None)}, skipped")
            continue
        latest = min(ends.values())
        dates = [day(d) for d in spec.get("map_dates") or []] or [latest]
        for d in dates:
            tag = str(d)
            if d > latest:
                notes.append(f"{county} {tag}: observed weather ends {latest}, skipped")
                continue
            if tag in (spec.get("skip") or {}).get(county, []):
                notes.append(f"{county} {tag}: already published, skipped")
                continue
            ns["_MONTHLY_CACHE"].clear(); ns["_WARNED_YM"].clear()
            _clear_wx_cache(ns, county, tag)
            maps = ns["predict_dates_only"](county, [tag], forecast=True, fc_scenario="mean",
                                            models_dir=L.pest_models(county), outputs=False)
            outputs += _collect(maps[tag], county, "nowcast", tag, tag, tag)
    for n in notes:
        log(n)
    if not outputs:
        raise StepSkipped("; ".join(notes) or "nothing to map")
    return dict(outputs=outputs, notes=notes)


@step("pest_forecast")
def pest_forecast(spec, L):
    issue = spec["issue"]
    first, _ = _issue_dates(spec)
    months = issue_months(issue, int(spec.get("forecast_months", 7)))
    out_root = L.pest_forecast(issue)
    ns = load_pest_pipeline(spec, L, out_root)
    cfg = ns["cfg"]
    cfg.vi_fc_dir, cfg.vi_fc_scenario = L.lstm_geotiff(issue), "mean"
    cfg.vi_fc_months = [f"{y:04d}-{m:02d}" for y, m in months]
    cfg.fc_only_vars, cfg.fc_scenario = None, "mean"
    cfg.weather_end = str(first - dt.timedelta(days=1))      # observed weather up to the day before the forecast
    map_day = int(spec.get("map_day", 15))
    outputs, notes = [], []
    for county in spec["counties"]:
        pests = L.pests_with_models(county)
        if not pests:
            notes.append(f"{county}: no pest models uploaded, skipped")
            continue
        cfg.pests = pests
        for y, m in months:
            d = dt.date(y, m, map_day)
            ns["_MONTHLY_CACHE"].clear(); ns["_WARNED_YM"].clear()
            maps = ns["predict_dates_only"](county, [str(d)], forecast=True, fc_scenario="mean",
                                            models_dir=L.pest_models(county), outputs=False)
            outputs += _collect(maps[str(d)], county, "forecast", str(dt.date(y, m, 1)), str(month_end(y, m)), str(d))
    for n in notes:
        log(n)
    if not outputs:
        raise RuntimeError("; ".join(notes) or "no forecast maps were produced")
    return dict(outputs=outputs, notes=notes, issue=issue)


@step("pest_train")
def pest_train(spec, L):
    ns = load_pest_pipeline(spec, L, L.pest_training)
    ns["cfg"].pests = list(PEST_ORDER)
    trained = {}
    for county in spec["counties"]:
        ns["run_pipeline"](counties=[county], predict_dates=[], train=True)
        src = os.path.join(L.pest_training, county, "models")
        new = sorted(glob.glob(os.path.join(src, "*_model.joblib")))
        if not new:
            log(f"{county}: no models were trained (see the log for the reason, e.g. too few records)")
            continue
        dst = L.pest_models(county)
        if os.path.isdir(dst) and os.listdir(dst):
            backup = os.path.join(os.path.dirname(dst), "_backup", f"{county}_{dt.datetime.now():%Y%m%d_%H%M%S}")
            shutil.copytree(dst, backup)
            log(f"{county}: previous models backed up to {backup}")
        os.makedirs(dst, exist_ok=True)
        for f in new:
            shutil.copy2(f, os.path.join(dst, os.path.basename(f)))
        for extra in ("session_info.txt", "config_used.json", "mechanistic_thresholds_and_sources.csv"):
            if os.path.exists(os.path.join(src, extra)):
                shutil.copy2(os.path.join(src, extra), os.path.join(dst, extra))
        trained[county] = [os.path.basename(f).split("_")[0] for f in new]
    if not trained:
        raise RuntimeError("No models were trained")
    return dict(trained=trained)
