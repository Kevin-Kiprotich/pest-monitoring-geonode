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

import numpy as np
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings

from pestwatch.pipeline import layout as lay
from pestwatch.pipeline import nbrun, seas5
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

    def test_csv_and_extension_checks(self):
        from pestwatch import inputs
        with self.assertRaisesRegex(inputs.InputError, "longitude and latitude"):
            inputs.save_upload(inputs.DATASETS["incidence_FAW"], "Kakamega", None,
                               SimpleUploadedFile("faw.csv", b"lon,lat,date\n34,0.2,2024-01-01\n"))
        with self.assertRaisesRegex(inputs.InputError, ".joblib"):
            inputs.save_upload(inputs.DATASETS["model_FAW"], "Kakamega", None, SimpleUploadedFile("faw.pkl", b"x"))


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
