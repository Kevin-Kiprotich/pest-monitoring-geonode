"""Managers' view of the model pipelines (Processes) and of the data they need (Input data)."""
import logging

from django.contrib import messages
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .. import conf, inputs, orchestrator, scheduler
from ..forms import InputUploadForm, StartRunForm
from ..models import AdminBoundary, AdminLevel, InputUpload, PipelineRun, PipelineSchedule, RunStatus
from .manage import perm
from .pages import portal_context

logger = logging.getLogger(__name__)


def _section(request, template, tab, **context):
    return render(request, template, {**portal_context(request, tab), "tab": tab, **context})


# --- Processes -------------------------------------------------------------------------

@perm("view_pipelinerun")
def process_list(request):
    runs = PipelineRun.objects.select_related("created_by").prefetch_related("steps")
    pipeline = request.GET.get("pipeline")
    if pipeline:
        runs = runs.filter(pipeline=pipeline)
    now = timezone.now()
    schedules = []
    for s in scheduler.schedules():
        last = PipelineRun.objects.filter(pipeline=s.pipeline).exclude(status=RunStatus.QUEUED).first()
        last_ok = PipelineRun.objects.filter(pipeline=s.pipeline, status=RunStatus.SUCCEEDED).first()
        schedules.append(dict(schedule=s, next=scheduler.next_due(s.pipeline, now), last=last, last_ok=last_ok,
                              when=_schedule_text(s.pipeline)))
    credentials = orchestrator.worker_status()
    worker_seen = credentials["checked_at"]
    return _section(request, "pestwatch/processes/list.html", "processes",
                    runs=runs[:100], schedules=schedules, credentials=credentials, pipeline=pipeline,
                    worker_seen=worker_seen, worker_stale=not worker_seen or (now - worker_seen).total_seconds() > 600,
                    model_counties=orchestrator.counties_with_models(),
                    active_ids=[r.pk for r in runs[:100] if r.is_active])


def _schedule_text(pipeline):
    s = conf.get("SCHEDULE").get(pipeline, {})
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    if pipeline == "weather":
        return f"Every day at {s.get('hour', 0):02d}:00 UTC"
    if pipeline == "nowcast":
        return f"Every {days[s.get('weekday', 0)]} at {s.get('hour', 0):02d}:00 UTC"
    return f"Every month on day {s.get('day', 1)} at {s.get('hour', 0):02d}:00 UTC"


@perm("view_pipelinerun")
def process_detail(request, pk):
    run = get_object_or_404(PipelineRun.objects.select_related("created_by"), pk=pk)
    log = orchestrator.tail(run.log_path(conf.get("DATA_ROOT")))
    layers = run.layers.select_related("pest", "county").order_by("county__name", "pest__name", "valid_from")
    return _section(request, "pestwatch/processes/detail.html", "processes", run=run,
                    steps=orchestrator.summary_for(run), log=log, layers=layers[:200])


@require_GET
@perm("view_pipelinerun")
def process_status(request, pk):
    run = get_object_or_404(PipelineRun, pk=pk)
    return JsonResponse({
        "status": run.status, "label": run.get_status_display(), "message": run.message,
        "steps": [{"id": s.pk, "status": s.status, "label": s.get_status_display(), "message": s.message}
                  for s in run.steps.order_by("position")],
        "log": orchestrator.tail(run.log_path(conf.get("DATA_ROOT")), 30000),
    })


@perm("add_pipelinerun")
def process_start(request):
    form = StartRunForm(request.POST or None, initial={"pipeline": request.GET.get("pipeline", "nowcast"),
                                                       "issue": f"{timezone.now():%Y-%m}",
                                                       "products": request.GET.getlist("product"),
                                                       "counties": request.GET.getlist("county"),
                                                       "month_from": request.GET.get("month", ""),
                                                       "month_to": request.GET.get("month", "")})
    problems = []
    if request.method == "POST" and form.is_valid():
        params = form.params()
        problems = orchestrator.check_ready(form.cleaned_data["pipeline"], params)
        if problems and not request.POST.get("confirm"):
            messages.warning(request, "This run will probably fail. Start it anyway, or fix these first.")
        else:
            run = orchestrator.create_run(form.cleaned_data["pipeline"], params, user=request.user)
            messages.success(request, f"{run} is queued. It starts when the worker is free.")
            return redirect("pestwatch:process_detail", pk=run.pk)
    return _section(request, "pestwatch/processes/start.html", "processes", form=form, problems=problems)


@require_POST
@perm("change_pipelinerun")
def process_retry(request, pk):
    run = get_object_or_404(PipelineRun, pk=pk)
    if run.is_active:
        messages.info(request, f"{run} is already {run.get_status_display().lower()}.")
    else:
        orchestrator.requeue(run)
        messages.success(request, f"{run} is queued again. Finished steps are not repeated.")
    return redirect("pestwatch:process_detail", pk=run.pk)


@require_POST
@perm("change_pipelinerun")
def process_cancel(request, pk):
    run = get_object_or_404(PipelineRun, pk=pk)
    if not run.is_active:
        messages.info(request, f"{run} is not running.")
    else:
        orchestrator.cancel(run)
        messages.success(request, f"{run} is being stopped." if run.status == RunStatus.RUNNING else f"{run} was cancelled.")
    return redirect("pestwatch:process_detail", pk=run.pk)


@require_POST
@perm("change_pipelinerun")
def schedule_toggle(request, pipeline):
    s = get_object_or_404(PipelineSchedule, pipeline=pipeline)
    s.enabled = not s.enabled
    s.save(update_fields=["enabled"])
    messages.success(request, f"Automatic {s.get_pipeline_display().lower()} runs are {'on' if s.enabled else 'off'}.")
    return redirect("pestwatch:process_list")


# --- Input data ------------------------------------------------------------------------

def _counties():
    return AdminBoundary.objects.filter(level=AdminLevel.COUNTY).order_by("name")


@perm("view_inputupload")
def input_list(request):
    counties = list(_counties())
    with_models = set(orchestrator.counties_with_models())
    chosen = request.GET.get("county")
    county = next((c for c in counties if str(c.pk) == chosen), None) or \
        next((c for c in counties if c.name in with_models), None) or (counties[0] if counties else None)
    groups = []
    if county:
        rows = inputs.status(county.name, timezone.now().date())
        for key, title in inputs.GROUPS:
            groups.append(dict(key=key, title=title, rows=[r for r in rows if r["ds"].group == key]))
    uploads = InputUpload.objects.select_related("county", "uploaded_by")[:30]
    return _section(request, "pestwatch/inputs/list.html", "inputs", counties=counties, county=county, groups=groups,
                    uploads=uploads, with_models=with_models, data_root=conf.get("DATA_ROOT"),
                    composites_when=_schedule_text("composites").replace("Every month on ", ""))


@perm("add_inputupload")
def input_upload(request, key):
    ds = inputs.DATASETS.get(key)
    if ds is None:
        raise Http404("Unknown dataset")
    initial = {}
    if request.GET.get("county"):
        initial["county"] = request.GET["county"]
    form = InputUploadForm(ds, request.POST or None, request.FILES or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        county, f = form.cleaned_data["county"], form.cleaned_data["file"]
        try:
            written, skipped = inputs.save_upload(ds, county.name, form.cleaned_data.get("month"), f)
        except inputs.InputError as e:
            form.add_error("file", str(e))
        except Exception as e:
            logger.exception("Saving %s failed", key)
            form.add_error("file", f"The file could not be saved: {e}")
        else:
            month = form.cleaned_data.get("month")
            InputUpload.objects.create(dataset=key, county=county, month=f"{month[0]}-{month[1]:02d}" if month else "",
                                       filename=f.name, size=f.size, files_written=len(written), uploaded_by=request.user)
            messages.success(request, f"Saved {len(written)} file{'s' if len(written) != 1 else ''} for {ds.label}, {county.name}.")
            if skipped:
                messages.warning(request, "Skipped: " + "; ".join(skipped[:10]))
            return redirect(f"{reverse('pestwatch:input_list')}?county={county.pk}")
    return _section(request, "pestwatch/inputs/upload.html", "inputs", form=form, ds=ds)
