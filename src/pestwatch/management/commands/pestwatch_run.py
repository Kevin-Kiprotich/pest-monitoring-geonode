from django.core.management.base import BaseCommand, CommandError

from pestwatch import orchestrator
from pestwatch.models import Pipeline


class Command(BaseCommand):
    help = "Queue a model run (the worker executes it). Same options as Processes > Start a run."

    def add_arguments(self, parser):
        parser.add_argument("pipeline", choices=Pipeline.values)
        parser.add_argument("--county", action="append", help="County name (repeat for several; default: all with models)")
        parser.add_argument("--start", help="Weather: first day (YYYY-MM-DD)")
        parser.add_argument("--end", help="Weather: last day (YYYY-MM-DD)")
        parser.add_argument("--date", action="append", help="Nowcast: map date (repeat for several; default: latest)")
        parser.add_argument("--issue", help="Forecast: issue month (YYYY-MM; default: this month)")
        parser.add_argument("--now", action="store_true", help="Run it here and now instead of queueing it")

    def handle(self, pipeline, **o):
        params = orchestrator.default_params(pipeline)
        if o["county"]:
            params["counties"] = o["county"]
        for key in ("start", "end", "issue"):
            if o[key]:
                params[key] = o[key]
        if o["date"]:
            params["map_dates"] = sorted(o["date"])
        for problem in orchestrator.check_ready(pipeline, params):
            self.stderr.write(f"Warning: {problem}")
        run = orchestrator.create_run(pipeline, params)
        self.stdout.write(f"Queued {run}: {params}")
        if o["now"]:
            run = orchestrator.execute(run)
            if run.status != "succeeded":
                raise CommandError(f"{run} {run.status}: {run.message}")
            self.stdout.write(self.style.SUCCESS(f"{run} succeeded"))
