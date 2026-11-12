"""Database tests of the run bookkeeping and the scheduler.

They need a test database with PostGIS, which the deployment's database user cannot create:
    python manage.py test pestwatch.tests_pipeline_db   (with a superuser database role)
"""
import datetime as dt
import os
import shutil
import tempfile

from django.test import TestCase, override_settings

from pestwatch.pipeline import layout as lay


@override_settings(PESTWATCH_MODEL_COUNTIES=["Alpha"])
class RunBookkeepingTests(TestCase):
    def setUp(self):
        from django.contrib.gis.geos import MultiPolygon, Polygon
        from pestwatch.models import AdminBoundary, BoundarySource
        src = BoundarySource.objects.create(dataset_name="test", level="county", name_field="name")
        AdminBoundary.objects.create(level="county", name="Alpha", source=src,
                                     geom=MultiPolygon(Polygon.from_bbox((36, -1, 37, 0)), srid=4326))
        self.root = tempfile.mkdtemp()
        self.override = override_settings(PESTWATCH_DATA_ROOT=self.root)
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.root)

    def test_create_requeue_cancel(self):
        from pestwatch import orchestrator
        from pestwatch.models import StepStatus
        run = orchestrator.create_run("forecast", {"counties": ["Alpha"], "issue": "2026-11"})
        self.assertEqual([s.name for s in run.steps.all()], orchestrator.PIPELINES["forecast"])
        self.assertEqual(run.period_label, "issued 2026-11")
        run.steps.filter(position__lt=2).update(status=StepStatus.SUCCEEDED)
        run.steps.filter(position=2).update(status=StepStatus.FAILED, message="x")
        run.status = "failed"
        run.save()
        run = orchestrator.requeue(run)
        self.assertEqual(run.status, "queued")
        self.assertEqual(list(run.steps.values_list("status", flat=True)[:3]), ["succeeded", "succeeded", "pending"])
        self.assertEqual(orchestrator.cancel(run).status, "cancelled")

    def test_check_ready_names_what_is_missing(self):
        from pestwatch import orchestrator
        problems = " ".join(orchestrator.check_ready("forecast", {"counties": ["Alpha"], "issue": "2026-11"}))
        self.assertIn("Elevation (DEM) missing for Alpha", problems)
        self.assertIn("2026-10 missing", problems)
        self.assertEqual(orchestrator.check_ready("nowcast", {"counties": []}),
                         ["No county has pest models yet: upload them under Input data."])

    def test_tick_waits_for_inputs_then_starts(self):
        from pestwatch import scheduler
        from pestwatch.models import PipelineRun, PipelineSchedule
        now = dt.datetime(2026, 11, 10, 8, tzinfo=dt.timezone.utc)
        PipelineSchedule.objects.update_or_create(pipeline="weather", defaults={"enabled": False})
        PipelineSchedule.objects.update_or_create(pipeline="forecast", defaults={"enabled": False})
        self.assertEqual(scheduler.tick(now), [])
        self.assertIn("No observed weather", PipelineSchedule.objects.get(pipeline="nowcast").waiting_message)
        L = lay.Layout(self.root)
        for var in ("tmax",):
            os.makedirs(L.wx_dir(var, "Alpha"))
            open(os.path.join(L.wx_dir(var, "Alpha"), "Alpha_tmax_20261101.tif"), "w").close()
        os.makedirs(os.path.dirname(L.dem("Alpha")))
        open(L.dem("Alpha"), "w").close()
        started = scheduler.tick(now)
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0].period_key, "2026-W46")
        self.assertEqual(scheduler.tick(now), [])                 # already queued for this week
        self.assertEqual(PipelineRun.objects.count(), 1)

    def test_export_boundaries(self):
        from osgeo import ogr
        from pestwatch import orchestrator
        L = lay.Layout(self.root)
        orchestrator.export_boundaries(L)
        ds = ogr.Open(L.boundaries)
        from pestwatch.models import AdminBoundary
        layer = ds.GetLayer()
        self.assertEqual(layer.GetFeatureCount(), AdminBoundary.objects.filter(level="county").count())
        self.assertIn("Alpha", [f.GetField("adm1_name") for f in layer])
