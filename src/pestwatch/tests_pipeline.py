"""Tests for the model pipeline plumbing: notebook runner, data layout, SEAS5 aggregation,
input uploads, the schedule rules, and the run bookkeeping."""
import ast
import datetime as dt
import io
import json
import os
import shutil
import tempfile
import zipfile
from types import SimpleNamespace
from unittest import mock as unittest_mock

import numpy as np
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings

from pestwatch.pipeline import layout as lay
from pestwatch.pipeline import composites, nbrun, seas5
from pestwatch.tests import write_tif


def notebook(path, *sources):
    cells = [{"cell_type": "markdown", "source": ["# title"]}]
    cells += [{"cell_type": "code", "source": s, "outputs": [], "metadata": {}} for s in sources]
    with open(path, "w") as f:
        json.dump({"cells": cells, "metadata": {}, "nbformat": 4, "nbformat_minor": 5}, f)
    return path


class NotebookRunnerTests(SimpleTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_params_replace_assignments_and_flow_into_later_lines(self):
        nb = notebook(os.path.join(self.dir, "n.ipynb"),
                      "%pip install something\nimport os\nOUT = r'F:\\\\old'\nSUB = os.path.join(OUT, 'x')\n"
                      "A, B = 1, 2\n",
                      "cfg = dict(a=1)\nfrom types import SimpleNamespace\nns = SimpleNamespace(root='F:/', n=1)\n")
        logs = []
        ns = nbrun.run(nb, cells=[1, 2], params={"OUT": "/data", "B": 20, "ns.root": "/data/v", "YEARS": nbrun.Expr("range(2020, 2023)")},
                       log=logs.append)
        self.assertEqual(ns["OUT"], "/data")
        self.assertEqual(ns["SUB"], os.path.join("/data", "x"))
        self.assertEqual((ns["A"], ns["B"]), (1, 20))
        self.assertEqual(ns["ns"].root, "/data/v")
        self.assertEqual(ns["ns"].n, 1)
        self.assertTrue(any("YEARS" in line and "never assigned" in line for line in logs))

    def test_markdown_cell_is_refused(self):
        nb = notebook(os.path.join(self.dir, "n.ipynb"), "x = 1")
        with self.assertRaisesRegex(nbrun.NotebookError, "markdown"):
            nbrun.run(nb, cells=[0], log=lambda *_: None)

    def test_only_definitions(self):
        nb = notebook(os.path.join(self.dir, "n.ipynb"), "import math\ndef keep():\n    return math.pi\nraise SystemExit('run code')\n")
        ns = nbrun.run(nb, cells=[1], only={1: ["keep"]}, log=lambda *_: None)
        self.assertAlmostEqual(ns["keep"](), 3.14159, places=4)

    def test_inject_ignores_nested_assignments(self):
        tree, used = nbrun.inject(ast.parse("def f():\n    X = 1\n"), {"X": 2})
        self.assertEqual(used, set())


class LayoutTests(SimpleTestCase):
    def test_paths_match_the_notebooks(self):
        L = lay.Layout("/r")
        self.assertEqual(L.wx_dir("rh", "Kilifi"), os.path.join("/r", "data", "HISTORICAL WEATHER", "DAILY_RH", "RH_KILIFI", "daily_tifs"))
        self.assertEqual(L.dem("Nyeri"), os.path.join("/r", "data", "SRTM", "Nyeri", "nyeri_dem_clipped.tif"))
        self.assertEqual(L.index_file("SAVI", "Kakamega", 2026, 9),
                         os.path.join("/r", "data", "SAVI", "tiff", "Kakamega", "SAVI_Kakamega_2026_09.tif"))
        self.assertEqual(L.incidence("MLND", "Kakamega"), os.path.join("/r", "data", "Incidence_data", "MLND", "KAKAMEGA.csv"))
        self.assertEqual(lay.cwv_folder("Kakamega"), "C_WV_KACH")
        self.assertEqual(lay.cwv_folder("Bungoma"), "C_WV_BUNGOMA")

    def test_dates_in_file_names(self):
        self.assertEqual(lay.date_in_name("Kilifi_RH_2026-10-03.tif"), dt.date(2026, 10, 3))
        self.assertEqual(lay.date_in_name("Nyeri_tmax_20261003.tif"), dt.date(2026, 10, 3))
        self.assertEqual(lay.date_in_name("CHIRPS_Kilifi_2026_10_03.tif"), dt.date(2026, 10, 3))
        self.assertEqual(lay.month_in_name("NDVI_Kakamega_2026_10.tif"), (2026, 10))
        self.assertIsNone(lay.month_in_name("NDVI_Kakamega.tif"))

    def test_months(self):
        self.assertEqual(lay.issue_months("2026-11", 3), [(2026, 11), (2026, 12), (2027, 1)])
        self.assertEqual(lay.add_months(2026, 1, -1), (2025, 12))
        self.assertEqual(lay.month_range((2025, 11), (2026, 2)), [(2025, 11), (2025, 12), (2026, 1), (2026, 2)])

    def test_wx_days(self):
        root = tempfile.mkdtemp()
        try:
            L = lay.Layout(root)
            d = L.wx_dir("tmax", "Nyeri")
            os.makedirs(d)
            for name in ("Nyeri_tmax_20261001.tif", "Nyeri_tmax_20261002.tif", "notes.txt"):
                open(os.path.join(d, name), "w").close()
            self.assertEqual(L.wx_days("tmax", "Nyeri"), {dt.date(2026, 10, 1), dt.date(2026, 10, 2)})
            self.assertEqual(L.wx_days("rh", "Nyeri"), set())
        finally:
            shutil.rmtree(root)


class Seas5AggregationTests(SimpleTestCase):
    def test_daily_fields(self):
        members, lat, lon = 2, 2, 3
        hours = np.arange(6, 24 * 2 + 1, 6)                  # two days of 6-hourly steps
        shape = (members, len(hours), lat, lon)
        six = {"t2m": np.full(shape, 293.15, "float32"), "d2m": np.full(shape, 293.15, "float32"),
               "u10": np.full(shape, 3.0, "float32"), "v10": np.full(shape, 4.0, "float32"),
               "tp": np.broadcast_to((hours / 24.0 * 0.005)[None, :, None, None], shape).astype("float32")}
        dly_h = np.array([24, 48])
        dly = {"mx2t24": np.full((members, 2, lat, lon), 303.15, "float32"),
               "mn2t24": np.full((members, 2, lat, lon), 283.15, "float32")}
        d = seas5.daily_fields(six, hours, dly, dly_h)
        self.assertEqual(d["temp"].shape, (members, 2, lat, lon))
        np.testing.assert_allclose(d["temp"], 20.0, atol=1e-4)
        np.testing.assert_allclose(d["rh"], 100.0, atol=1e-3)          # dewpoint = temperature
        np.testing.assert_allclose(d["wind_speed"], 5.0, atol=1e-5)
        np.testing.assert_allclose(d["rain"], 5.0, atol=1e-3)          # 5 mm per day
        np.testing.assert_allclose(d["tmax"], 30.0, atol=1e-4)
        np.testing.assert_allclose(d["tmin"], 10.0, atol=1e-4)

    def test_system_and_area(self):
        self.assertEqual(seas5.systems_for(2026, 10)[0], "51")
        self.assertEqual(seas5.systems_for(2021, 10)[0], "5")
        self.assertEqual(seas5.request_area((34.5, -0.6, 35.2, 0.9)), [3.0, 32.0, -3.0, 38.0])


class InputUploadTests(SimpleTestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.override = override_settings(PESTWATCH_DATA_ROOT=self.root)
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.root)

    def tif_bytes(self, bands=1):
        path = os.path.join(self.root, "src.tif")
        write_tif(path, np.ones((10, 10), "float32"), bands=bands)
        with open(path, "rb") as f:
            data = f.read()
        os.remove(path)
        return data

    def test_monthly_index_upload(self):
        from pestwatch import inputs
        ds = inputs.DATASETS["index_NDVI"]
        f = SimpleUploadedFile("anything.tif", self.tif_bytes())
        written, skipped = inputs.save_upload(ds, "Kakamega", (2026, 10), f)
        self.assertEqual(written, [lay.Layout(self.root).index_file("NDVI", "Kakamega", 2026, 10)])
        self.assertTrue(os.path.exists(written[0]))
        rows = {r["ds"].key: r for r in inputs.status("Kakamega", dt.date(2026, 11, 10))}
        self.assertTrue(rows["index_NDVI"]["ok"])
        self.assertFalse(rows["dem"]["ok"])

    def test_month_from_name_and_band_check(self):
        from pestwatch import inputs
        with self.assertRaisesRegex(inputs.InputError, "11 to 13 band"):
            inputs.save_upload(inputs.DATASETS["s2_bands"], "Kakamega", None,
                               SimpleUploadedFile("S2_Kakamega_2026_10.tif", self.tif_bytes()))
        with self.assertRaisesRegex(inputs.InputError, "Choose the month"):
            inputs.save_upload(inputs.DATASETS["index_NDVI"], "Kakamega", None, SimpleUploadedFile("x.tif", self.tif_bytes()))

    def test_zip_writes_each_month_and_ignores_member_paths(self):
        from pestwatch import inputs
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("../../evil/NDVI_Kakamega_2026_08.tif", self.tif_bytes())
            z.writestr("NDVI_Kakamega_2026_09.tif", self.tif_bytes())
            z.writestr("readme.tif", b"no month")
        written, skipped = inputs.save_upload(inputs.DATASETS["index_NDVI"], "Kakamega", None,
                                              SimpleUploadedFile("ndvi.zip", buf.getvalue()))
        L = lay.Layout(self.root)
        self.assertEqual(sorted(written), sorted([L.index_file("NDVI", "Kakamega", 2026, 8),
                                                  L.index_file("NDVI", "Kakamega", 2026, 9)]))
        self.assertEqual(len(skipped), 1)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.root), "evil")))

    def test_uploaded_composite_is_tagged_and_kept_by_the_fetch(self):
        from pestwatch import inputs
        written, _ = inputs.save_upload(inputs.DATASETS["index_NDVI"], "Kakamega", (2026, 10),
                                        SimpleUploadedFile("ndvi.tif", self.tif_bytes()))
        self.assertEqual(inputs.file_source(written[0]), composites.UPLOAD_SOURCE)
        rows = {r["ds"].key: r for r in inputs.status("Kakamega", dt.date(2026, 11, 10))}
        self.assertEqual(rows["index_NDVI"]["latest_source"], "uploaded")
        self.assertTrue(rows["index_NDVI"]["ds"].automatic)
        self.assertFalse(rows["dem"]["ds"].automatic)
        # neither a refresh nor a forced fetch replaces it
        src = inputs.file_source(written[0])
        self.assertFalse(composites.decide(True, src, refresh=True, force=True))

    def test_csv_and_extension_checks(self):
        from pestwatch import inputs
        with self.assertRaisesRegex(inputs.InputError, "longitude and latitude"):
            inputs.save_upload(inputs.DATASETS["incidence_FAW"], "Kakamega", None,
                               SimpleUploadedFile("faw.csv", b"lon,lat,date\n34,0.2,2024-01-01\n"))
        with self.assertRaisesRegex(inputs.InputError, ".joblib"):
            inputs.save_upload(inputs.DATASETS["model_FAW"], "Kakamega", None, SimpleUploadedFile("faw.pkl", b"x"))


class CompositeTests(SimpleTestCase):
    """The parts of the Earth Engine composites that do not need Earth Engine."""

    def test_products_cover_the_monthly_datasets(self):
        from pestwatch import inputs
        monthly = {k for k, d in inputs.DATASETS.items() if d.monthly}
        self.assertEqual(set(composites.PRODUCT_KEYS), monthly)
        self.assertEqual({k for ks in composites.STEP_PRODUCTS.values() for k in ks}, monthly)
        L = lay.Layout("/r")
        for key, p in composites.products().items():
            self.assertEqual(p.path(L, "Kakamega", 2026, 10), inputs.DATASETS[key].path(L, "Kakamega", 2026, 10))

    def test_recipes_follow_the_notebooks(self):
        p = composites.products()
        self.assertEqual(p["s2_bands"].bands, ["B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B11", "B12", "fill_flag"])
        self.assertEqual((p["s2_bands"].scale, p["s2_cwv"].scale), (10, 60))
        self.assertEqual(p["s2_cwv"].bands, ["B1", "B9", "fill_flag"])
        self.assertEqual(p["index_MCARI"].bands, ["MCARI", "QA"])
        self.assertEqual(p["landsat"].bands, ["COASTAL", "BLUE", "LST_K"])
        self.assertEqual({k: v.recipe for k, v in p.items() if v.recipe == "median_qa"},
                         {"index_MCARI": "median_qa", "index_NDMI": "median_qa"})
        self.assertEqual(composites.products("monthly_mean")["index_NDMI"].bands, ["NDMI"])
        # 2024 is not in the Sentinel-2 / TOP10 histories, but is in the coastal / water vapour and MCARI ones
        self.assertIn(2024, p["s2_bands"].skip_years)
        self.assertIn(2024, p["index_NDVI"].skip_years)
        self.assertNotIn(2024, p["s2_cwv"].skip_years)
        self.assertNotIn(2024, p["index_MCARI"].skip_years)

    def test_months_and_donors(self):
        self.assertEqual(composites.month_add("2026-01", -1), "2025-12")
        self.assertEqual(composites.month_add("2025-11", 3), "2026-02")
        self.assertEqual(composites.month_span("2025-11", "2026-02"), ["2025-11", "2025-12", "2026-01", "2026-02"])
        have = ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-10", "2026-12"]
        # nearest first, ties earlier first, at most 6, only within +/-3 months, never the month itself
        self.assertEqual(composites.donor_months("2026-07", have),
                         ["2026-06", "2026-08", "2026-05", "2026-04", "2026-10"])
        self.assertEqual(len(composites.donor_months("2026-07", have, max_n=2)), 2)

    def test_grid_utm_and_windows(self):
        self.assertEqual(composites.utm_epsg(34.8, 0.3), 32636)       # Kakamega
        self.assertEqual(composites.utm_epsg(39.8, -3.5), 32737)      # Kilifi
        self.assertEqual(composites.utm_epsg(36.9, -0.4), 32737)      # Nyeri
        self.assertEqual(composites.grid((101.0, 205.0, 159.0, 290.0), 10), (100, 290, 6, 9))
        self.assertEqual(composites.grid((100.0, 200.0, 160.0, 290.0), 10), (100, 290, 6, 9))
        tiles = composites.windows(5, 3, 2)
        self.assertEqual(tiles, [(0, 0, 2, 2), (0, 2, 2, 2), (0, 4, 2, 1), (2, 0, 1, 2), (2, 2, 1, 2), (2, 4, 1, 1)])

    def test_overwrite_rule(self):
        ee, up = composites.EE_SOURCE, composites.UPLOAD_SOURCE
        self.assertTrue(composites.decide(False, "", False, False))        # missing: fetch
        self.assertFalse(composites.decide(True, ee, False, False))        # fetched before: keep
        self.assertTrue(composites.decide(True, ee, True, False))          # fetched before, refresh month: again
        self.assertTrue(composites.decide(True, ee, False, True))          # forced
        self.assertFalse(composites.decide(True, up, True, True))          # uploaded: never
        self.assertFalse(composites.decide(True, "", True, True))          # copied onto the server: never

    def test_default_months_and_form_params(self):
        from pestwatch import orchestrator
        from pestwatch.forms import StartRunForm
        with override_settings(PESTWATCH_COMPOSITE_REFRESH_MONTHS=3), \
                unittest_mock.patch.object(orchestrator, "counties_with_models", return_value=["Kakamega"]):
            p = orchestrator.default_params("composites", dt.date(2026, 1, 3))
            self.assertEqual(p, dict(counties=["Kakamega"], months=["2025-12"],
                                     refresh=["2025-09", "2025-10", "2025-11"]))
            with unittest_mock.patch("pestwatch.forms._model_county_choices", return_value=[("Kakamega", "Kakamega")]):
                form = StartRunForm(dict(pipeline="composites", month_from="2026-08", month_to="2026-10",
                                         products=["index_NDVI", "landsat"], force="on"))
                self.assertTrue(form.is_valid(), form.errors)
                p = form.params()
        self.assertEqual(p["months"], ["2026-08", "2026-09", "2026-10"])
        self.assertEqual((p["refresh"], p["products"], p["force"]), ([], ["index_NDVI", "landsat"], True))


def fake_run(status, attempts=1, finished=None, pk=1, message="boom"):
    return SimpleNamespace(status=status, attempts=attempts, finished_at=finished, pk=pk, message=message)


class ScheduleRuleTests(SimpleTestCase):
    tz = dt.timezone.utc

    def at(self, *a):
        return dt.datetime(*a, tzinfo=self.tz)

    def test_periods_and_due_times(self):
        from pestwatch import scheduler
        now = self.at(2026, 11, 10, 8)                        # a Tuesday
        self.assertEqual(scheduler.period_key("weather", now), "2026-11-10")
        self.assertEqual(scheduler.period_key("nowcast", now), "2026-W46")
        self.assertEqual(scheduler.period_key("forecast", now), "2026-11")
        self.assertEqual(scheduler.due_at("nowcast", now), self.at(2026, 11, 9, 5))
        self.assertEqual(scheduler.due_at("forecast", now), self.at(2026, 11, 6, 6))
        self.assertEqual(scheduler.next_due("forecast", now), self.at(2026, 12, 6, 6))

    def test_decisions(self):
        from pestwatch import scheduler
        now = self.at(2026, 11, 10, 8)
        self.assertEqual(scheduler.decide("weather", self.at(2026, 11, 10, 1), []), ("idle", ""))
        self.assertEqual(scheduler.decide("weather", now, []), ("start", None))
        self.assertEqual(scheduler.decide("weather", now, [fake_run("succeeded")])[0], "idle")
        recent = fake_run("failed", finished=now - dt.timedelta(hours=1))
        self.assertEqual(scheduler.decide("weather", now, [recent])[0], "wait")
        old = fake_run("failed", finished=now - dt.timedelta(hours=7))
        self.assertEqual(scheduler.decide("weather", now, [old]), ("retry", old))
        self.assertEqual(scheduler.decide("weather", now, [fake_run("failed", attempts=3, finished=old.finished_at)])[0], "wait")
        self.assertEqual(scheduler.decide("weather", now, [fake_run("cancelled")])[0], "idle")
