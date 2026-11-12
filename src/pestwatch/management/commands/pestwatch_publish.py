import os
import shutil

from django.contrib.auth import get_user_model
from django.contrib.gis.geos import GEOSGeometry
from django.core.management.base import BaseCommand, CommandError

from pestwatch import services
from pestwatch.models import AdminBoundary, AdminLevel, Pest, PestLayer, PestStyle, ProductType, TemporalType
from pestwatch.raster import RasterError, check_county, inspect_raster, validate_product


class Command(BaseCommand):
    help = "Validate and publish a raster from the command line (same checks as the upload form)."

    def add_arguments(self, parser):
        parser.add_argument("file")
        parser.add_argument("--pest", required=True, help="Pest slug")
        parser.add_argument("--product", required=True, choices=ProductType.values)
        parser.add_argument("--county", required=True, help="County name")
        parser.add_argument("--style", help="PestStyle name (default: first style for the product)")
        parser.add_argument("--temporal", default=TemporalType.BASELINE, choices=TemporalType.values)
        parser.add_argument("--valid-from")
        parser.add_argument("--valid-to")
        parser.add_argument("--title", default="")
        parser.add_argument("--user", default="admin", help="Owner username")
        parser.add_argument("--unpublished", action="store_true", help="Keep it hidden from the dashboard")

    def handle(self, file, **o):
        if not os.path.isfile(file):
            raise CommandError(f"{file} not found")
        try:
            pest = Pest.objects.get(slug=o["pest"])
            county = AdminBoundary.objects.get(level=AdminLevel.COUNTY, name__iexact=o["county"])
            styles = PestStyle.objects.filter(product_type=o["product"])
            style = styles.get(name=o["style"]) if o["style"] else styles.first()
            user = get_user_model().objects.get(username=o["user"])
        except Exception as e:
            raise CommandError(e)
        if style is None:
            raise CommandError(f"No style exists for {o['product']} rasters")

        try:
            info = inspect_raster(file)
            validate_product(info, o["product"])
            check_county(GEOSGeometry(info.footprint_wkt, srid=4326), county,
                         AdminBoundary.objects.filter(level=AdminLevel.COUNTY))
        except RasterError as e:
            raise CommandError(str(e))

        staged = os.path.join(services.staging_dir(), f"cli_{os.getpid()}_{os.path.basename(file)}")
        shutil.copyfile(file, staged)
        layer = PestLayer.objects.create(
            pest=pest, product_type=o["product"], temporal_type=o["temporal"], county=county, style=style,
            title=o["title"], valid_from=o["valid_from"], valid_to=o["valid_to"],
            is_published=not o["unpublished"], uploaded_by=user,
            source_filename=os.path.basename(file), file_size=os.path.getsize(file), staged_path=staged)
        layer = services.process_layer(layer.pk)
        if layer.status != "ready":
            raise CommandError(f"Publishing failed: {layer.status_message}")
        self.stdout.write(self.style.SUCCESS(f"Published layer {layer.pk}: {layer.dataset.alternate}"))
