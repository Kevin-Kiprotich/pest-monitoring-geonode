import logging
import signal
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from pestwatch import conf, orchestrator, scheduler
from pestwatch.models import PipelineRun, RunStatus

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = ("Run the PestWatch model pipelines: start them on schedule and execute queued runs one at a time. "
            "Runs in the pestmodels container.")

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Check the schedule, run what is queued, then exit")
        parser.add_argument("--no-schedule", action="store_true", help="Only execute queued runs")

    def handle(self, once=False, no_schedule=False, **options):
        self.stopping = False
        signal.signal(signal.SIGTERM, self._stop)
        orchestrator.recover_interrupted()
        self.stdout.write("PestWatch worker started")
        while not self.stopping:
            close_old_connections()
            try:
                orchestrator.write_worker_status()
                if not no_schedule:
                    for run in scheduler.tick():
                        self.stdout.write(f"Scheduled {run}")
                run = PipelineRun.objects.filter(status=RunStatus.QUEUED).order_by("created_at").first()
                if run is not None:
                    self.stdout.write(f"Running {run}")
                    run = orchestrator.execute(run)
                    self.stdout.write(f"{run}: {run.get_status_display()} {run.message}")
                    continue                      # look for the next queued run straight away
            except Exception:
                logger.exception("Worker loop error")
            if once:
                break
            for _ in range(conf.get("WORKER_POLL_SECONDS")):
                if self.stopping:
                    break
                time.sleep(1)
        self.stdout.write("PestWatch worker stopped")

    def _stop(self, *args):
        # Finish the current step's bookkeeping; a running run is marked interrupted on the next start.
        self.stopping = True
