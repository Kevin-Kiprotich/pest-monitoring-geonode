from django.core.management.base import BaseCommand, CommandError

from pestwatch import services
from pestwatch.models import PestLayer


class Command(BaseCommand):
    help = "Run a PestWatch background job (started by the web app)."

    def add_arguments(self, parser):
        parser.add_argument("job", choices=["process_layer", "recompute_layer", "recompute_counties"])
        parser.add_argument("ids", nargs="+", type=int)

    def handle(self, job, ids, **options):
        if job == "process_layer":
            layer = services.process_layer(ids[0])
            self.stdout.write(f"Layer {layer.pk}: {layer.status} {layer.status_message}")
        elif job == "recompute_layer":
            try:
                layer = PestLayer.objects.select_related("county", "dataset").get(pk=ids[0])
            except PestLayer.DoesNotExist:
                raise CommandError(f"No layer {ids[0]}")
            self.stdout.write(f"Layer {layer.pk}: {services.compute_layer_stats(layer)} zones")
        else:
            services.recompute_county_stats(ids)
            self.stdout.write(f"Recomputed statistics for counties {ids}")
