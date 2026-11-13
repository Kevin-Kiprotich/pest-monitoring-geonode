"""Create, run and publish the model pipelines.

A run is a list of steps. Every model step runs ``pipeline/stepper.py`` in the
separate model environment (``conf.MODELS_PYTHON``), one at a time, from the
``pestwatch_worker`` process. The last step, ``publish``, runs here: it turns
the hybrid (0-1) and risk class (1-5) GeoTIFFs into PestLayers through the same
code as an upload (``services.process_layer``), so they appear on the
dashboard with their zonal statistics.
"""
import datetime as dt
import json
import logging
import os
import shutil
import signal
import subprocess
import time
import uuid

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from . import conf, services
from .models import (AdminBoundary, AdminLevel, LayerStatus, Pest, PestLayer, PestStyle, Pipeline, PipelineRun,
                     PipelineStep, ProductType, RunStatus, RunTrigger, StepStatus, TemporalType)
from .pipeline import composites
from .pipeline.layout import Layout, PEST_ORDER, add_months, issue_months

logger = logging.getLogger(__name__)

PIPELINES = {
    Pipeline.WEATHER: ["fetch_chirps", "fetch_era5_temp", "fetch_era5_rh", "fetch_era5_wind"],
    Pipeline.COMPOSITES: ["fetch_s2_bands", "fetch_s2_cwv", "fetch_landsat", "fetch_indices"],
    Pipeline.NOWCAST: ["pest_nowcast", "publish"],
    # The stored LSTM (models/lstm/final_model.pt) is used as is: no training step.
    Pipeline.FORECAST: ["fetch_seas5", "bias_correct", "downscale_30m", "lstm_dataset", "lstm_stores",
                        "lstm_forecast", "pest_forecast", "publish"],
    Pipeline.TRAINING: ["pest_train"],
}

STEP_LABELS = {
    "fetch_chirps": "Download CHIRPS rainfall (Earth Engine)",
    "fetch_era5_temp": "Download ERA5-Land max/min temperature (CDS)",
    "fetch_era5_rh": "Download ERA5 relative humidity (CDS)",
    "fetch_era5_wind": "Download ERA5-Land wind (Earth Engine)",
    "fetch_s2_bands": "Sentinel-2 band stack (Earth Engine)",
    "fetch_s2_cwv": "Sentinel-2 coastal and water vapour (Earth Engine)",
    "fetch_landsat": "Landsat stack (Earth Engine)",
    "fetch_indices": "Vegetation index composites (Earth Engine)",
    "fetch_seas5": "Download the SEAS5 seasonal forecast (CDS)",
    "bias_correct": "Bias-correct SEAS5 against observed weather",
    "downscale_30m": "Downscale the forecast to 30 m with the DEM",
    "lstm_dataset": "LSTM: gather the latest months of satellite data and the forecast weather",
    "lstm_stores": "LSTM: prepare the inputs for the whole area",
    "lstm_train": "LSTM training: skipped (the stored model is used)",
    "lstm_forecast": "LSTM: forecast vegetation indices with the stored model (7 months)",
    "pest_nowcast": "Pest models: map current risk",
    "pest_forecast": "Pest models: map forecast risk",
    "pest_train": "Pest models: retrain from incidence records",
    "publish": "Publish hybrid and classified layers",
}

# Steps whose result lists the maps that "publish" turns into layers.
PRODUCERS = ("pest_nowcast", "pest_forecast")


class PipelineError(RuntimeError):
    pass


def layout():
    return Layout(conf.get("DATA_ROOT"))


# --- what to run ------------------------------------------------------------------

def counties_with_models():
    L = layout()
    names = conf.get("MODEL_COUNTIES")
    qs = AdminBoundary.objects.filter(level=AdminLevel.COUNTY).order_by("name")
    if names:
        return [c.name for c in qs if c.name.lower() in {n.lower() for n in names}]
    return [c.name for c in qs if L.pests_with_models(c.name)]


def default_params(pipeline, today=None):
    today = today or timezone.now().date()
    counties = counties_with_models()
    if pipeline == Pipeline.WEATHER:
        # The last WEATHER_WINDOW_DAYS, never before AUTO_FETCH_FROM: earlier days are the uploaded history.
        start = max(today - dt.timedelta(days=conf.get("WEATHER_WINDOW_DAYS")), fetch_from())
        return dict(counties=counties, start=str(start), end=str(today))
    if pipeline == Pipeline.NOWCAST:
        return dict(counties=counties, map_dates=[])
    if pipeline == Pipeline.COMPOSITES:
        # The month that just ended, and the months before it whose gap filling can now use later months.
        last = composites.month_add(f"{today:%Y-%m}", -1)
        refresh = [composites.month_add(last, -i) for i in range(conf.get("COMPOSITE_REFRESH_MONTHS"), 0, -1)]
        # Months before AUTO_FETCH_FROM are the uploaded history, never fetched.
        first = conf.get("AUTO_FETCH_FROM")
        months, _ = composites.split_history([last], first)
        refresh, _ = composites.split_history(refresh, first)
        return dict(counties=counties, months=months, refresh=refresh)
    if pipeline == Pipeline.FORECAST:
        return dict(counties=counties, issue=f"{today:%Y-%m}")
    return dict(counties=counties)


def fetch_from():
    """The first day the pipelines download from Earth Engine / the CDS; earlier data is uploaded history."""
    return dt.date.fromisoformat(conf.get("AUTO_FETCH_FROM"))


def _status_file():
    return os.path.join(conf.get("DATA_ROOT"), "worker_status.json")


def local_status():
    key = conf.get("GEE_KEY_FILE")
    return dict(checked_at=timezone.now().isoformat(), gee=bool(key) and os.path.exists(key),
                cds=bool(conf.get("CDS_KEY")), models_env=os.path.exists(conf.get("MODELS_PYTHON")))


def write_worker_status():
    """Called by the worker: what it can see (the key file is only mounted in its container)."""
    path = _status_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".part", "w") as f:
        json.dump(local_status(), f)
    os.replace(path + ".part", path)


def worker_status():
    """The worker's view if it has reported, else this process's own."""
    try:
        with open(_status_file()) as f:
            s = json.load(f)
        s["checked_at"] = dt.datetime.fromisoformat(s["checked_at"])
        s["reported"] = True
        return s
    except (OSError, ValueError, KeyError):
        s = local_status()
        s.update(checked_at=None, reported=False)
        return s


def check_ready(pipeline, params):
    """Problems that would make the run fail, in words a manager can act on."""
    L = layout()
    counties = params.get("counties") or []
    problems = []
    if not counties:
        return ["No county has pest models yet: upload them under Input data."]
    creds = worker_status()
    needs_cds = pipeline in (Pipeline.WEATHER, Pipeline.FORECAST)
    if needs_cds and not creds["cds"]:
        problems.append("The Copernicus CDS key is not set (CDS_API_KEY in .env).")
    if pipeline in (Pipeline.WEATHER, Pipeline.COMPOSITES) and not creds["gee"]:
        problems.append("The Earth Engine service-account key is missing (PESTWATCH_GEE_KEY_FILE in .env, "
                        "key file in ./secrets).")
    first = fetch_from()
    if pipeline == Pipeline.WEATHER and params.get("end") and dt.date.fromisoformat(params["end"]) < first:
        problems.append(f"Weather is downloaded from {first} on; earlier days are history: upload them under "
                        f"Input data.")
    if pipeline == Pipeline.COMPOSITES:
        months, old = composites.split_history(params.get("months") or [], str(first))
        refresh, _ = composites.split_history(params.get("refresh") or [], str(first))
        if not months and not refresh:
            problems.append(f"Earth Engine composites start with {first:%Y-%m}; " +
                            ("earlier months are history: upload them under Input data." if old
                             else "no months to fetch yet."))
    if pipeline == Pipeline.NOWCAST:
        no_wx = [c for c in counties if not L.wx_days("tmax", c)]
        if no_wx:
            problems.append(f"No observed weather yet for {', '.join(no_wx)}: upload the weather history under Input "
                            f"data, and the weather download adds the days from {first}.")
    if pipeline in (Pipeline.NOWCAST, Pipeline.FORECAST):
        no_dem = [c for c in counties if not os.path.exists(L.dem(c))]
        if no_dem:
            problems.append(f"Elevation (DEM) missing for {', '.join(no_dem)}.")
    if pipeline == Pipeline.FORECAST and not os.path.exists(L.lstm_model):
        problems.append("The trained LSTM model is missing: upload final_model.pt under Input data > LSTM model.")
    if pipeline == Pipeline.FORECAST and params.get("issue"):
        y, m = map(int, params["issue"].split("-"))
        if dt.date(y, m, 1) < first:
            problems.append(f"Forecasts are downloaded for issues from {first:%Y-%m} on; {params['issue']} is history.")
        else:
            years = range(conf.get("SEAS5_ARCHIVE_START_YEAR"), y + 1)
            missing = [yy for yy in years if dt.date(yy, m, 1) < first and not os.path.exists(L.seas5_archive_file(yy, m))]
            if missing:
                problems.append(f"SEAS5 archive for start month {m:02d} missing for {', '.join(map(str, missing))} "
                                f"(history: upload under Input data > SEAS5 archive).")
        py, pm = add_months(y, m, -1)
        for c in counties:
            missing = [i for i in ("NDVI", "NDMI", "SAVI") if not os.path.exists(L.index_file(i, c, py, pm))]
            if not os.path.exists(L.s2_file(c, py, pm)):
                missing.append("Sentinel-2 bands")
            if not os.path.exists(L.landsat_file(c, py, pm)):
                missing.append("Landsat stack")
            if missing:
                how = ("history: upload them under Input data" if dt.date(py, pm, 1) < first.replace(day=1)
                       else "the Satellite composites run makes them")
                problems.append(f"{c}: monthly composites for {py}-{pm:02d} missing ({', '.join(missing)}; {how}).")
    if pipeline == Pipeline.TRAINING:
        for c in counties:
            if not any(os.path.exists(L.incidence(p, c)) for p in PEST_ORDER):
                problems.append(f"{c}: no incidence records uploaded.")
    return problems


def create_run(pipeline, params, trigger=RunTrigger.MANUAL, user=None, period_key=""):
    with transaction.atomic():
        run = PipelineRun.objects.create(pipeline=pipeline, params=params, trigger=trigger, created_by=user,
                                         period_key=period_key)
        PipelineStep.objects.bulk_create(
            PipelineStep(run=run, name=name, position=i) for i, name in enumerate(PIPELINES[pipeline]))
    logger.info("Queued %s with %s", run, params)
    return run


def requeue(run):
    """Run a failed or cancelled run again, starting at the step that did not finish."""
    with transaction.atomic():
        run.steps.exclude(status__in=[StepStatus.SUCCEEDED, StepStatus.SKIPPED]).update(
            status=StepStatus.PENDING, message="", started_at=None, finished_at=None)
        PipelineRun.objects.filter(pk=run.pk).update(status=RunStatus.QUEUED, message="", cancel_requested=False,
                                                     finished_at=None, pid=None)
    run.refresh_from_db()
    return run


def cancel(run):
    if run.status == RunStatus.QUEUED:
        PipelineRun.objects.filter(pk=run.pk).update(status=RunStatus.CANCELLED, finished_at=timezone.now(),
                                                     message="Cancelled before it started.")
    elif run.status == RunStatus.RUNNING:
        PipelineRun.objects.filter(pk=run.pk).update(cancel_requested=True)
    run.refresh_from_db()
    return run


def recover_interrupted():
    """Runs left 'running' by a worker that stopped (restart, crash) are marked failed, so they can be retried."""
    now = timezone.now()
    for run in PipelineRun.objects.filter(status=RunStatus.RUNNING):
        run.steps.filter(status=StepStatus.RUNNING).update(status=StepStatus.FAILED, finished_at=now,
                                                           message="Interrupted: the worker stopped.")
        PipelineRun.objects.filter(pk=run.pk).update(status=RunStatus.FAILED, finished_at=now, pid=None,
                                                     message="Interrupted: the worker stopped while this ran.")


# --- running ------------------------------------------------------------------------

def _log(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(f"[{timezone.now():%Y-%m-%d %H:%M:%S}] {text}\n")


def execute(run):
    """Run every unfinished step of ``run`` in order. Blocks until the run ends."""
    L = layout()
    log_file = run.log_path(L.root)
    PipelineRun.objects.filter(pk=run.pk).update(status=RunStatus.RUNNING, started_at=timezone.now(),
                                                 finished_at=None, message="", attempts=run.attempts + 1)
    run.refresh_from_db()
    _log(log_file, f"===== {run} (attempt {run.attempts}) {json.dumps(run.params)} =====")
    try:
        export_boundaries(L)
    except Exception as e:
        logger.exception("Boundary export failed")
        return _finish(run, RunStatus.FAILED, f"Could not export the county boundaries: {e}")

    for st in run.steps.order_by("position"):
        if st.status in (StepStatus.SUCCEEDED, StepStatus.SKIPPED):
            continue
        run.refresh_from_db()
        if run.cancel_requested:
            return _finish(run, RunStatus.CANCELLED, "Cancelled by a manager.")
        PipelineStep.objects.filter(pk=st.pk).update(status=StepStatus.RUNNING, started_at=timezone.now(),
                                                     finished_at=None, message="")
        _log(log_file, f"----- step {st.position + 1}: {STEP_LABELS.get(st.name, st.name)} -----")
        try:
            if st.name == "publish":
                result = publish(run)
            else:
                result = _run_model_step(run, st, L, log_file)
        except Exception as e:
            logger.exception("Step %s of %s failed", st.name, run)
            _log(log_file, f"FAILED: {e}")
            PipelineStep.objects.filter(pk=st.pk).update(status=StepStatus.FAILED, finished_at=timezone.now(),
                                                         message=str(e)[:4000])
            run.refresh_from_db()
            status = RunStatus.CANCELLED if run.cancel_requested else RunStatus.FAILED
            return _finish(run, status, f"{STEP_LABELS.get(st.name, st.name)}: {e}"[:4000])
        skipped = result.pop("skipped", None)
        PipelineStep.objects.filter(pk=st.pk).update(
            status=StepStatus.SKIPPED if skipped else StepStatus.SUCCEEDED, finished_at=timezone.now(),
            message=(skipped or _summary(st.name, result))[:4000], result=result)
        _log(log_file, f"step {st.name}: {'skipped: ' + skipped if skipped else 'done'}")
        if skipped and st.name in PRODUCERS:
            # Nothing new to map: the run is complete.
            run.steps.filter(position__gt=st.position).update(status=StepStatus.SKIPPED, message="Nothing to publish.")
            return _finish(run, RunStatus.SUCCEEDED, skipped)
    return _finish(run, RunStatus.SUCCEEDED, "")


def _finish(run, status, message):
    PipelineRun.objects.filter(pk=run.pk).update(status=status, message=message, finished_at=timezone.now(), pid=None)
    run.refresh_from_db()
    _log(run.log_path(conf.get("DATA_ROOT")), f"===== {run}: {run.get_status_display()} {message} =====")
    return run


def _summary(name, result):
    if "outputs" in result:
        return f"{len(result['outputs'])} maps produced"
    if name == "publish":
        return f"{result.get('published', 0)} layers published"
    if "counts" in result:
        return ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in result["counts"].items())
    return ""


def spec_for(run, st, L):
    p = run.params or {}
    run_dir = L.run_dir(run.pk)
    spec = dict(
        step=st.name, root=L.root, run_dir=run_dir, counties=p.get("counties") or [],
        start=p.get("start"), end=p.get("end"), issue=p.get("issue"), map_dates=p.get("map_dates") or [],
        gee_key_file=conf.get("GEE_KEY_FILE"), gee_project=conf.get("GEE_PROJECT"),
        era5_lag_days=conf.get("ERA5_LAG_DAYS"), rh_lag_days=conf.get("RH_LAG_DAYS"),
        refresh_days=conf.get("CHIRPS_REFRESH_DAYS"), archive_start_year=conf.get("SEAS5_ARCHIVE_START_YEAR"),
        forecast_months=conf.get("FORECAST_MONTHS"), map_day=conf.get("FORECAST_MAP_DAY"),
        crop_season=conf.get("CROP_SEASON"), clean_tile_cache=conf.get("LSTM_CLEAN_TILE_CACHE"),
        months=p.get("months") or [], refresh=p.get("refresh") or [], products=p.get("products") or [],
        force=bool(p.get("force")), composite_threads=conf.get("COMPOSITE_THREADS"),
        auto_fetch_from=conf.get("AUTO_FETCH_FROM"),
        ndmi_recipe=conf.get("NDMI_RECIPE"),
        result_path=os.path.join(run_dir, f"{st.position:02d}_{st.name}.result.json"),
    )
    if st.name == "pest_nowcast" and not spec["map_dates"]:
        spec["skip"] = published_nowcast_dates(spec["counties"])
    return spec


def published_nowcast_dates(counties):
    out = {}
    for layer in PestLayer.objects.filter(source="model", temporal_type=TemporalType.NOWCAST,
                                          county__name__in=counties).select_related("county"):
        if layer.valid_from:
            out.setdefault(layer.county.name, set()).add(str(layer.valid_from))
    return {k: sorted(v) for k, v in out.items()}


def _run_model_step(run, st, L, log_file):
    python = conf.get("MODELS_PYTHON")
    if not os.path.exists(python):
        raise PipelineError(f"The model environment is not installed ({python}). The worker must run in the "
                            "pestmodels container.")
    spec = spec_for(run, st, L)
    os.makedirs(spec["run_dir"], exist_ok=True)
    spec_path = os.path.join(spec["run_dir"], f"{st.position:02d}_{st.name}.json")
    with open(spec_path, "w") as f:
        json.dump(spec, f, indent=2)
    if os.path.exists(spec["result_path"]):
        os.remove(spec["result_path"])
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV", "DJANGO_SETTINGS_MODULE")}
    env.update(CDSAPI_URL=conf.get("CDS_URL"), CDSAPI_KEY=conf.get("CDS_KEY") or "", PYTHONNOUSERSITE="1",
               PYTHONUNBUFFERED="1")
    stepper = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline", "stepper.py")
    timeout = conf.get("STEP_TIMEOUT_HOURS") * 3600
    t0 = time.time()
    with open(log_file, "ab") as log:
        proc = subprocess.Popen([python, "-I", "-u", stepper, spec_path], cwd=spec["run_dir"], env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    PipelineRun.objects.filter(pk=run.pk).update(pid=proc.pid)
    while proc.poll() is None:
        time.sleep(5)
        cancelled = PipelineRun.objects.filter(pk=run.pk, cancel_requested=True).exists()
        if cancelled or time.time() - t0 > timeout:
            _kill(proc)
            raise PipelineError("cancelled by a manager" if cancelled else
                                f"did not finish within {conf.get('STEP_TIMEOUT_HOURS')} hours and was stopped")
    if not os.path.exists(spec["result_path"]):
        raise PipelineError(f"the step stopped without a result (exit code {proc.returncode}); see the log")
    with open(spec["result_path"]) as f:
        result = json.load(f)
    if not result.pop("ok", False):
        raise PipelineError(result.get("error") or "failed; see the log")
    result.pop("seconds", None)
    return result


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(30)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass


# --- county boundaries for the notebooks ---------------------------------------------

def export_boundaries(L):
    """Write the counties from PostGIS as the shapefile the notebooks read (name field ``adm1_name``)."""
    from osgeo import ogr, osr
    counties = list(AdminBoundary.objects.filter(level=AdminLevel.COUNTY).order_by("name"))
    if not counties:
        raise PipelineError("No counties: import the boundaries first (Layers > Boundaries).")
    out_dir = os.path.dirname(L.boundaries)
    tmp = os.path.join(os.path.dirname(out_dir), f".boundaries_{uuid.uuid4().hex[:8]}")
    os.makedirs(tmp)
    try:
        path = os.path.join(tmp, os.path.basename(L.boundaries))
        ds = ogr.GetDriverByName("ESRI Shapefile").CreateDataSource(path)
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        layer = ds.CreateLayer("counties", srs, ogr.wkbMultiPolygon)
        for name, width in (("adm1_name", 80), ("adm1_code", 40)):
            fd = ogr.FieldDefn(name, ogr.OFTString)
            fd.SetWidth(width)
            layer.CreateField(fd)
        for c in counties:
            f = ogr.Feature(layer.GetLayerDefn())
            f.SetField("adm1_name", c.name)
            f.SetField("adm1_code", c.code or "")
            f.SetGeometry(ogr.CreateGeometryFromWkb(bytes(c.geom.wkb)))
            layer.CreateFeature(f)
        ds = None
        shutil.rmtree(out_dir, ignore_errors=True)
        os.replace(tmp, out_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- publishing -------------------------------------------------------------------------

def model_user():
    User = get_user_model()
    user, created = User.objects.get_or_create(
        username=conf.get("MODEL_USER"), defaults={"first_name": "PestWatch", "last_name": "models"})
    if created:
        user.set_unusable_password()
        user.save()
    return user


def _outputs(run):
    for st in run.steps.filter(name__in=PRODUCERS):
        if st.result.get("outputs"):
            return st.result["outputs"]
    return []


def publish(run):
    outputs = _outputs(run)
    if not outputs:
        return {"skipped": "No maps to publish."}
    user = model_user()
    styles = {p: PestStyle.objects.filter(product_type=p).order_by("pk").first() for p in ProductType.values}
    if None in styles.values():
        raise PipelineError("A style for hybrid and one for classified rasters are needed (Layers > Styles).")
    pests = {p.model_code.upper(): p for p in Pest.objects.exclude(model_code="")}
    counties = {c.name.lower(): c for c in AdminBoundary.objects.filter(level=AdminLevel.COUNTY)}
    issued = None
    if run.params.get("issue"):
        y, m = map(int, run.params["issue"].split("-"))
        issued = dt.date(y, m, 1)
    published, failed, notes, replaced = 0, [], [], []
    for o in outputs:
        pest, county = pests.get(o["pest"].upper()), counties.get(o["county"].lower())
        if pest is None:
            notes.append(f"{o['pest']}: no pest has this model code (set it under Django admin > Pests)")
            continue
        if county is None:
            notes.append(f"{o['county']}: not a county in PestWatch")
            continue
        for product, path in ((ProductType.HYBRID, o["hybrid"]), (ProductType.CLASSIFIED, o["classified"])):
            same = PestLayer.objects.filter(source="model", pest=pest, county=county, product_type=product,
                                            temporal_type=o["kind"], valid_from=o["valid_from"])
            if o["kind"] == TemporalType.FORECAST and not conf.get("KEEP_OLD_FORECASTS"):
                old = list(same)                          # every earlier issue for this month is replaced
            else:
                old = list(same.filter(issued_on=issued))  # the same nowcast date / issue, re-run
            layer = _publish_one(run, pest, county, product, o, path, styles[product], user, issued)
            if layer.status == LayerStatus.READY:
                published += 1
                replaced += [x for x in old if x.pk != layer.pk]
            else:
                failed.append(f"{pest.name} {county.name} {product} {o['map_date']}: {layer.status_message[:300]}")
    for layer in replaced:
        try:
            services.delete_layer(layer)
        except Exception:
            logger.exception("Could not remove the replaced layer %s", layer.pk)
    PipelineRun.objects.filter(pk=run.pk).update(layers_published=published)
    if failed and not published:
        raise PipelineError("No layer could be published: " + " | ".join(failed[:5]))
    return dict(published=published, replaced=len(replaced), failed=failed, notes=notes)


def _publish_one(run, pest, county, product, o, path, style, user, issued):
    if not os.path.exists(path):
        raise PipelineError(f"Model output not found: {path}")
    staged = os.path.join(services.staging_dir(), f"model_{uuid.uuid4().hex}.tif")
    shutil.copyfile(path, staged)
    kind = TemporalType(o["kind"])
    when = dt.date.fromisoformat(o["valid_from"])
    if kind == TemporalType.FORECAST:
        period = f"forecast for {when:%b %Y}, issued {issued:%b %Y}"
        about = (f"Forecast risk of {pest.name} in {county.name} for {when:%B %Y} (mapped for {o['map_date']}), "
                 f"from the SEAS5 forecast issued {issued:%B %Y} and the LSTM vegetation index forecast.")
    else:
        period = f"nowcast {when:%d %b %Y}"
        about = (f"Current risk of {pest.name} in {county.name} on {when:%d %B %Y}, from observed weather "
                 "(ERA5-Land, ERA5, CHIRPS) and the latest satellite composites.")
    label = "hybrid risk (0-1)" if product == ProductType.HYBRID else "risk class (1-5)"
    layer = PestLayer.objects.create(
        pest=pest, county=county, product_type=product, temporal_type=kind, style=style,
        title=f"{pest.name} – {label} – {county.name} ({period})", description=about + f" Model run #{run.pk}.",
        is_published=True, uploaded_by=user, valid_from=o["valid_from"], valid_to=o["valid_to"],
        source="model", run=run, issued_on=issued, source_filename=os.path.basename(path),
        file_size=os.path.getsize(path), staged_path=staged)
    return services.process_layer(layer.pk)


def summary_for(run):
    """Steps with labels, for the templates."""
    return [dict(step=s, label=STEP_LABELS.get(s.name, s.name)) for s in run.steps.order_by("position")]


def tail(path, max_bytes=60000):
    if not os.path.exists(path):
        return ""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read().decode("utf-8", "replace")
    return data if size <= max_bytes else "…\n" + data.split("\n", 1)[-1]


def months_label(issue, n):
    return [f"{y:04d}-{m:02d}" for y, m in issue_months(issue, n)]
