"""Bookkeeping for the automatic model runs and the input data managers upload."""
import os

from django.conf import settings
from django.db import models

from .common import TimeStampedModel


class Pipeline(models.TextChoices):
    WEATHER = "weather", "Weather download"
    NOWCAST = "nowcast", "Nowcast"
    FORECAST = "forecast", "Forecast"
    TRAINING = "training", "Pest model training"


class RunStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class RunTrigger(models.TextChoices):
    SCHEDULE = "schedule", "Automatic"
    MANUAL = "manual", "Manual"


class StepStatus(models.TextChoices):
    PENDING = "pending", "Waiting"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Done"
    SKIPPED = "skipped", "Nothing to do"
    FAILED = "failed", "Failed"


class PipelineRun(TimeStampedModel):
    pipeline = models.CharField(max_length=20, choices=Pipeline.choices, db_index=True)
    status = models.CharField(max_length=20, choices=RunStatus.choices, default=RunStatus.QUEUED, db_index=True)
    trigger = models.CharField(max_length=20, choices=RunTrigger.choices, default=RunTrigger.MANUAL)
    # The scheduled period this run covers, e.g. "2026-11-10" (weather), "2026-W46" (nowcast), "2026-11" (forecast).
    period_key = models.CharField(max_length=20, blank=True, db_index=True)
    # counties, start, end, map_dates, issue: see orchestrator.create_run.
    params = models.JSONField(default=dict, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    cancel_requested = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    message = models.TextField(blank=True)
    pid = models.IntegerField(null=True, blank=True)
    layers_published = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_pipeline_display()} #{self.pk}"

    @property
    def period_label(self):
        p = self.params or {}
        if p.get("issue"):
            return f"issued {p['issue']}"
        if p.get("map_dates"):
            d = p["map_dates"]
            return d[0] if len(d) == 1 else f"{d[0]} to {d[-1]} ({len(d)} dates)"
        if p.get("start") and p.get("end"):
            return f"{p['start']} to {p['end']}"
        return "latest data"

    @property
    def duration(self):
        if self.started_at and self.finished_at:
            return self.finished_at - self.started_at
        return None

    @property
    def is_active(self):
        return self.status in (RunStatus.QUEUED, RunStatus.RUNNING)

    def log_path(self, root):
        return os.path.join(root, "runs", str(self.pk), "run.log")


class PipelineStep(models.Model):
    run = models.ForeignKey(PipelineRun, on_delete=models.CASCADE, related_name="steps")
    name = models.CharField(max_length=40)
    position = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=StepStatus.choices, default=StepStatus.PENDING)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    message = models.TextField(blank=True)
    result = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["run", "position"]
        constraints = [models.UniqueConstraint(fields=["run", "position"], name="pw_step_unique_position")]

    def __str__(self):
        return f"{self.run} / {self.name}"

    @property
    def duration(self):
        if self.started_at and self.finished_at:
            return self.finished_at - self.started_at
        return None


class PipelineSchedule(models.Model):
    """Whether the system starts a pipeline on its own, and why it is waiting if it is."""
    pipeline = models.CharField(max_length=20, choices=Pipeline.choices, unique=True)
    enabled = models.BooleanField(default=True)
    waiting_message = models.TextField(blank=True)
    last_checked = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["pipeline"]

    def __str__(self):
        return f"{self.get_pipeline_display()} schedule"


class InputUpload(TimeStampedModel):
    """A file a manager uploaded into the model data store (static layer, monthly composite, model...)."""
    dataset = models.CharField(max_length=40, db_index=True)
    county = models.ForeignKey("pestwatch.AdminBoundary", on_delete=models.SET_NULL, null=True, blank=True)
    month = models.CharField(max_length=7, blank=True)
    filename = models.CharField(max_length=255)
    size = models.BigIntegerField(default=0)
    files_written = models.PositiveIntegerField(default=1)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.dataset} {self.filename}"
