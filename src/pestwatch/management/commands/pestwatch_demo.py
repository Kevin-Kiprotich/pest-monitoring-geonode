"""Create demo boundaries and synthetic rasters so the dashboard has something to show.

For the test instance only (settings_pwtest): it publishes real layers to GeoServer.
"""
import os

import numpy as np
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.core.management.base import BaseCommand, CommandError
from osgeo import gdal, osr

from pestwatch import services
from pestwatch.models import AdminBoundary, AdminLevel, BoundarySource, Pest, PestLayer, PestStyle

DEMO_SOURCE = "pestwatch-demo"
# (name, minx, miny) — 0.6° x 0.6° squares in central Kenya; names are clearly fake.
COUNTIES = [("Demo North", 37.0, 0.1), ("Demo Central", 37.0, -0.5), ("Demo South", 37.6, -0.5)]
SIZE = 0.6


def rect(minx, miny, maxx, maxy):
    return MultiPolygon(Polygon.from_bbox((minx, miny, maxx, maxy)), srid=4326)


def risk_field(seed, shape=(240, 240)):
    """A smooth 0-1 surface made of a few Gaussian hot spots."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:1:shape[0] * 1j, 0:1:shape[1] * 1j]
    field = np.zeros(shape)
    for _ in range(4):
        cx, cy, r = rng.uniform(0.1, 0.9), rng.uniform(0.1, 0.9), rng.uniform(0.12, 0.3)
        field += rng.uniform(0.5, 1.0) * np.exp(-((x - cx) ** 2 + (y - cy) ** 2) / (2 * r ** 2))
    field = (field - field.min()) / (field.max() - field.min())
    return np.clip(field * 0.9 + rng.uniform(0, 0.1, shape), 0, 1)


def write_tif(path, array, bbox, nodata):
    rows, cols = array.shape
    dtype = gdal.GDT_Float32 if array.dtype.kind == "f" else gdal.GDT_Int16
    ds = gdal.GetDriverByName("GTiff").Create(path, cols, rows, 1, dtype)
    minx, miny, maxx, maxy = bbox
    ds.SetGeoTransform((minx, (maxx - minx) / cols, 0, maxy, 0, -(maxy - miny) / rows))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(nodata)
    band.WriteArray(array)
    ds = None


class Command(BaseCommand):
    help = "Create demo counties, constituencies, wards and synthetic rasters (test instance only)."

    def add_arguments(self, parser):
        parser.add_argument("--quick", action="store_true", help="One county and two pests only")
        parser.add_argument("--remove", action="store_true", help="Delete the demo layers and boundaries")

    def handle(self, quick, remove, **options):
        if settings.DEFAULT_WORKSPACE == "geonode":
            raise CommandError("Refusing to run against the live workspace. Use my_geonode.settings_pwtest.")
        if remove:
            return self.remove()

        user = get_user_model().objects.filter(is_superuser=True).order_by("pk").first()
        counties = self.boundaries()
        pests = list(Pest.objects.all()[:2] if quick else Pest.objects.all())
        styles = {s.product_type: s for s in PestStyle.objects.filter(name__in=["pw-hybrid", "pw-classified"])}

        jobs = []
        for ci, county in enumerate(counties[:1] if quick else counties):
            for pi, pest in enumerate(pests):
                jobs.append((county, pest, ci * 10 + pi, None))
        # A second period for one combination, to show the period picker.
        jobs.append((counties[0], pests[0], 99, "2026-12-01"))

        for county, pest, seed, valid_from in jobs:
            hybrid = risk_field(seed).astype("float32")
            classified = (np.digitize(hybrid, [0.2, 0.4, 0.6, 0.8]) + 1).astype("int16")
            for product, array, nodata in (("hybrid", hybrid, -9999.0), ("classified", classified, 0)):
                path = os.path.join(services.staging_dir(), f"demo_{county.pk}_{pest.slug}_{product}_{seed}.tif")
                write_tif(path, array, county.geom.extent, nodata)
                layer = PestLayer.objects.create(
                    pest=pest, product_type=product, county=county, style=styles[product],
                    valid_from=valid_from or "2026-11-01", is_published=True, uploaded_by=user,
                    description="Synthetic demo layer.", source_filename=os.path.basename(path),
                    file_size=os.path.getsize(path), staged_path=path)
                layer = services.process_layer(layer.pk)
                msg = f"{layer}: {layer.status}"
                self.stdout.write(self.style.SUCCESS(msg) if layer.status == "ready"
                                  else self.style.ERROR(f"{msg} {layer.status_message}"))

    def boundaries(self):
        source, _ = BoundarySource.objects.get_or_create(
            dataset_name=DEMO_SOURCE, level=AdminLevel.COUNTY, defaults={"name_field": "name"})
        counties = []
        for name, minx, miny in COUNTIES:
            county, _ = AdminBoundary.objects.update_or_create(
                level=AdminLevel.COUNTY, name=name, parent=None,
                defaults={"source": source, "geom": rect(minx, miny, minx + SIZE, miny + SIZE)})
            counties.append(county)
            step = SIZE / 3
            for i in range(3):  # three constituencies as vertical strips
                cx = minx + i * step
                cons, _ = AdminBoundary.objects.update_or_create(
                    level=AdminLevel.CONSTITUENCY, name=f"{name} {i + 1}", parent=county,
                    defaults={"source": source, "geom": rect(cx, miny, cx + step, miny + SIZE)})
                half_x, half_y = step / 2, SIZE / 2
                for j in range(4):  # four wards per constituency (2 x 2)
                    wx, wy = cx + (j % 2) * half_x, miny + (j // 2) * half_y
                    AdminBoundary.objects.update_or_create(
                        level=AdminLevel.WARD, name=f"{name} {i + 1}-{'ABCD'[j]}", parent=cons,
                        defaults={"source": source, "geom": rect(wx, wy, wx + half_x, wy + half_y)})
        source.feature_count = AdminBoundary.objects.filter(source=source).count()
        source.save()
        self.stdout.write(f"Demo boundaries: {len(counties)} counties, 9 constituencies, 36 wards")
        return counties

    def remove(self):
        demo = AdminBoundary.objects.filter(source__dataset_name=DEMO_SOURCE, level=AdminLevel.COUNTY)
        for layer in PestLayer.objects.filter(county__in=demo):
            services.delete_layer(layer)
            self.stdout.write(f"Deleted {layer}")
        BoundarySource.objects.filter(dataset_name=DEMO_SOURCE).delete()
        self.stdout.write(self.style.SUCCESS("Demo data removed"))
