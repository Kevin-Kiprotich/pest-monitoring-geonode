"""Decide when the system starts a pipeline on its own.

Each automatic pipeline has a period (a day for the weather download, an ISO
week for the nowcast, a month for the composites and the forecast) and a time in that period
from which it is due (``conf.SCHEDULE``, UTC). Once due:

- nothing happens if a run for the period is queued, running or succeeded;
- a failed automatic run is retried after RETRY_DELAY_HOURS, up to MAX_ATTEMPTS;
- a run a manager cancelled is not restarted for that period;
- otherwise a new run is queued, unless inputs are missing: then the schedule
  shows what it is waiting for, and checks again on the next tick.
"""
import datetime as dt
import logging

from django.utils import timezone

from . import conf, orchestrator
from .models import Pipeline, PipelineRun, PipelineSchedule, RunStatus, RunTrigger

logger = logging.getLogger(__name__)

AUTOMATIC = (Pipeline.WEATHER, Pipeline.COMPOSITES, Pipeline.NOWCAST, Pipeline.FORECAST)


def period_key(pipeline, now):
    if pipeline == Pipeline.WEATHER:
        return f"{now:%Y-%m-%d}"
    if pipeline == Pipeline.NOWCAST:
        y, w, _ = now.isocalendar()
        return f"{y}-W{w:02d}"
    return f"{now:%Y-%m}"


def due_at(pipeline, now):
    """The moment the pipeline becomes due in the period that contains ``now``."""
    s = conf.get("SCHEDULE").get(pipeline, {})
    hour = int(s.get("hour", 0))
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if pipeline == Pipeline.WEATHER:
        return midnight.replace(hour=hour)
    if pipeline == Pipeline.NOWCAST:
        monday = midnight - dt.timedelta(days=now.weekday())
        return monday + dt.timedelta(days=int(s.get("weekday", 0)), hours=hour)
    return midnight.replace(day=int(s.get("day", 1)), hour=hour)


def next_due(pipeline, now=None):
    """When the next automatic run of ``pipeline`` is due (for display)."""
    now = now or timezone.now()
    at = due_at(pipeline, now)
    if at > now:
        return at
    if pipeline == Pipeline.WEATHER:
        return due_at(pipeline, now + dt.timedelta(days=1))
    if pipeline == Pipeline.NOWCAST:
        return due_at(pipeline, now + dt.timedelta(days=7))
    y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return due_at(pipeline, now.replace(year=y, month=m, day=1))


def schedules():
    for p in AUTOMATIC:
        PipelineSchedule.objects.get_or_create(pipeline=p)
    return PipelineSchedule.objects.filter(pipeline__in=AUTOMATIC)


def decide(pipeline, now, runs):
    """What to do for one pipeline: ("wait", why) / ("retry", run) / ("start", None) / ("idle", why).

    ``runs`` are the automatic runs of the current period, newest first.
    """
    if now < due_at(pipeline, now):
        return "idle", ""
    if any(r.status in (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.SUCCEEDED) for r in runs):
        return "idle", ""
    last = runs[0] if runs else None
    if last is not None:
        if last.status == RunStatus.CANCELLED:
            return "idle", "Cancelled by a manager for this period."
        if last.attempts >= conf.get("MAX_ATTEMPTS"):
            return "wait", (f"Gave up after {last.attempts} attempts this period. Last error: "
                            f"{last.message[:300]} Fix the cause, then retry run #{last.pk}.")
        if last.finished_at and now - last.finished_at < dt.timedelta(hours=conf.get("RETRY_DELAY_HOURS")):
            return "wait", f"Run #{last.pk} failed; it is retried automatically after a {conf.get('RETRY_DELAY_HOURS')} h pause."
        return "retry", last
    return "start", None


def tick(now=None):
    now = now or timezone.now()
    started = []
    for sched in schedules():
        if not sched.enabled:
            continue
        key = period_key(sched.pipeline, now)
        runs = list(PipelineRun.objects.filter(pipeline=sched.pipeline, trigger=RunTrigger.SCHEDULE,
                                               period_key=key).order_by("-created_at"))
        action, info = decide(sched.pipeline, now, runs)
        message = ""
        if action == "retry":
            orchestrator.requeue(info)
            started.append(info)
        elif action == "start":
            params = orchestrator.default_params(sched.pipeline, now.date())
            problems = orchestrator.check_ready(sched.pipeline, params)
            if problems:
                message = "Waiting for: " + " ".join(problems)
            else:
                started.append(orchestrator.create_run(sched.pipeline, params, RunTrigger.SCHEDULE, period_key=key))
        elif action == "wait":
            message = info
        PipelineSchedule.objects.filter(pk=sched.pk).update(waiting_message=message, last_checked=now)
    return started
