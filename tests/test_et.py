"""Offline checks of the evapotranspiration tab logic with a fake Earth Engine."""

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

_VENDOR = Path(__file__).resolve().parents[1] / "weather_extractor" / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.append(str(_VENDOR))

import numpy as np
import pandas as pd
import shapely

from weather_extractor import et_processing as et


AOI = shapely.box(-88.2, 30.9, -87.6, 31.5)
EE_PLUGIN_LIBS = Path(os.environ.get("APPDATA", "")) / "QGIS/QGIS3/profiles/default/python/plugins/ee_plugin/extlibs"


def modis_year(year, mm_per_day=1.0, skip=()):
    """8-day composites like MOD16A2GF; the last one is shorter and ends on 31 December."""
    records = []
    start = pd.Timestamp(year, 1, 1)
    while start.year == year:
        length = min(8, (pd.Timestamp(year + 1, 1, 1) - start).days)
        if start not in skip:
            records.append(
                {"date": f"{start:%Y-%m-%d}", "ET": mm_per_day * length, "PET": 2 * mm_per_day * length, "valid_fraction": 0.9}
            )
        start += pd.Timedelta(days=8)
    return records


def openet_year(year, value=50.0):
    return [
        {
            "date": f"{year}-{month:02d}-01",
            "et_ensemble_mad": value,
            "et_ensemble_mad_min": value - 10,
            "et_ensemble_mad_max": value + 10,
            "et_ensemble_mad_count": 5.6,
            "valid_fraction": 1.0,
        }
        for month in range(1, 13)
    ]


class MonthlyTests(unittest.TestCase):
    def test_modis_composites_are_spread_over_months(self):
        monthly = et.modis_to_monthly(modis_year(2024))
        self.assertEqual(len(monthly), 12)
        np.testing.assert_allclose(monthly["modis_et_mm"], monthly.index.days_in_month)
        np.testing.assert_allclose(monthly["modis_pet_mm"], 2 * monthly.index.days_in_month)
        self.assertAlmostEqual(monthly["modis_et_mm"].sum(), 366)
        self.assertAlmostEqual(monthly.loc["2024-12-01", "modis_valid_fraction"], 0.9)

    def test_month_with_a_missing_composite_is_empty(self):
        # The composite starting 25 January covers 25 January to 1 February.
        monthly = et.modis_to_monthly(modis_year(2023, skip=[pd.Timestamp(2023, 1, 25)]))
        self.assertTrue(np.isnan(monthly.loc["2023-01-01", "modis_et_mm"]))
        self.assertTrue(np.isnan(monthly.loc["2023-02-01", "modis_et_mm"]))
        self.assertAlmostEqual(monthly.loc["2023-03-01", "modis_et_mm"], 31)

    def test_masked_composite_leaves_months_empty(self):
        records = modis_year(2023)
        records[0]["ET"] = None
        monthly = et.modis_to_monthly(records)
        self.assertTrue(np.isnan(monthly.loc["2023-01-01", "modis_et_mm"]))
        self.assertAlmostEqual(monthly.loc["2023-01-01", "modis_pet_mm"], 62)

    def test_openet_months(self):
        monthly = et.openet_to_monthly(openet_year(2022))
        self.assertEqual(
            list(monthly.columns), et.PRODUCTS[et.OPENET]["columns"] + ["openet_valid_fraction", "openet_version"]
        )
        self.assertEqual(monthly.loc["2022-07-01", "openet_et_max_mm"], 60)
        self.assertTrue(et.openet_to_monthly([]).empty)


class ExtractTests(unittest.TestCase):
    def run_extract(self, products, fetcher, start=2023, end=2024):
        folder = tempfile.mkdtemp()
        settings = {"start_year": start, "end_year": end, "products": products, "project": "p", "output_dir": folder}
        messages = []
        result = et.extract_et(settings, AOI, progress=lambda p, m: messages.append(p), fetcher=fetcher)
        return result, pd.read_csv(result["monthly"]), messages

    def test_both_products(self):
        def fetcher(product, year):
            return openet_year(year) if product == et.OPENET else modis_year(year)

        result, table, messages = self.run_extract([et.OPENET, et.MODIS], fetcher)
        self.assertEqual(len(table), 24)
        self.assertEqual(
            list(table.columns),
            ["date", "year", "month", "openet_et_mm", "openet_et_min_mm", "openet_et_max_mm", "openet_models",
             "openet_valid_fraction", "openet_version", "modis_et_mm", "modis_pet_mm", "modis_valid_fraction"],
        )
        self.assertEqual(table.loc[0, "date"], "2023-01-01")
        self.assertEqual(set(table["openet_version"].astype(str)), {"2.1"})
        annual = pd.read_csv(result["annual"], index_col="year")
        self.assertAlmostEqual(annual.loc[2023, "openet_et_mm"], 600)
        self.assertAlmostEqual(annual.loc[2024, "modis_et_mm"], 366)
        self.assertEqual(result["warnings"], [])
        self.assertIsNone(result["warnings_file"])
        self.assertEqual(messages[-1], 100.0)
        readme = (result["output_dir"] / "README.txt").read_text(encoding="utf-8")
        self.assertIn("OpenET", readme)
        self.assertIn("MOD16A2GF", readme)

    def test_year_without_images_is_reported(self):
        def fetcher(product, year):
            return openet_year(year) if year < 2024 else []

        result, table, _ = self.run_extract([et.OPENET], fetcher)
        self.assertNotIn("modis_et_mm", table.columns)
        self.assertEqual(table["openet_et_mm"].isna().sum(), 12)
        annual = pd.read_csv(result["annual"], index_col="year")
        self.assertTrue(np.isnan(annual.loc[2024, "openet_et_mm"]))
        self.assertEqual(len(result["warnings"]), 2)
        self.assertIn("no images for 2024", result["warnings"][0])
        self.assertTrue(Path(result["warnings_file"]).is_file())

    def test_openet_version_by_year(self):
        # v2.0 (western US only) before 2016, v2.1 (whole CONUS) afterwards.
        self.assertEqual(et.openet_collection(2015)[0], "2.0")
        self.assertTrue(et.openet_collection(2016)[1].endswith("/v2_1"))
        result, table, _ = self.run_extract([et.OPENET], lambda product, year: openet_year(year), 2015, 2016)
        self.assertEqual(table.groupby("year")["openet_version"].first().astype(str).to_dict(), {2015: "2.0", 2016: "2.1"})
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("Collection v2.0", result["warnings"][0])

    def test_low_coverage_is_reported(self):
        def fetcher(product, year):
            records = modis_year(year)
            for record in records:
                record["valid_fraction"] = 0.2
            return records

        result, _, _ = self.run_extract([et.MODIS], fetcher, 2023, 2023)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("less than half", result["warnings"][0])

    def test_failure_stops_pending_requests(self):
        calls = []

        def fetcher(product, year):
            calls.append(year)
            time.sleep(0.05)
            raise ValueError("Collection not found")

        with self.assertRaises(ValueError):
            self.run_extract([et.OPENET], fetcher, 2000, 2020)
        self.assertLess(len(calls), 21)

    def test_validation(self):
        settings = {"start_year": 2023, "end_year": 2023, "products": [], "output_dir": tempfile.mkdtemp()}
        with self.assertRaises(ValueError):
            et.extract_et(settings, AOI, fetcher=lambda p, y: [])


class EarthEngineTests(unittest.TestCase):
    def test_retries_only_transient_errors(self):
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("Too many concurrent aggregations.")
            return ["ok"]

        self.assertEqual(et._with_retries(flaky, lambda: False, sleep=lambda s: None), ["ok"])
        self.assertEqual(len(attempts), 3)
        with self.assertRaises(KeyError):
            et._with_retries(lambda: {}["x"], lambda: False, sleep=lambda s: None)
        with self.assertRaises(InterruptedError):
            et._with_retries(lambda: [], lambda: True)

    def test_scale_and_simplification(self):
        self.assertEqual(et.openet_scale(10), 30)
        self.assertEqual(et.openet_scale(20000), 1000)
        circle = shapely.Point(-88, 31).buffer(0.5, quad_segs=2000)
        self.assertLessEqual(shapely.get_num_coordinates(et.simplified(circle)), et.MAX_VERTICES)
        self.assertAlmostEqual(et.simplified(circle).area, circle.area, delta=circle.area * 0.001)

    def test_saved_sign_in_is_read_locally(self):
        folder = Path(tempfile.mkdtemp())
        path = folder / "credentials"
        with patch.object(et, "CREDENTIALS_PATH", str(path)):
            self.assertFalse(et.has_credentials())
            self.assertEqual(et.stored_project(), "")
            path.write_text('{"refresh_token": "t", "project": "my-project"}')
            self.assertTrue(et.has_credentials())
            self.assertEqual(et.stored_project(), "my-project")

    def test_connect(self):
        class FakeData:
            initialized = False
            _cloud_api_user_project = None

            def is_initialized(self):
                return self.initialized

        class FakeEE:
            def __init__(self, error=None):
                self.data = FakeData()
                self.error = error
                self.projects = []

            def Initialize(self, project):
                if self.error:
                    raise Exception(self.error)
                self.projects.append(project)
                self.data.initialized = True
                self.data._cloud_api_user_project = project

        report = lambda *a: None
        ee = FakeEE()
        et.connect(ee, "p1", report)
        et.connect(ee, "p1", report)
        et.connect(ee, "p2", report)
        self.assertEqual(ee.projects, ["p1", "p2"])
        # Sign-in problems point to the Google Earth Engine plugin.
        with self.assertRaisesRegex(RuntimeError, "Google Earth Engine > Sign-in"):
            et.connect(FakeEE("Please authorize access to your Earth Engine account"), "p", report)
        with self.assertRaisesRegex(RuntimeError, "Cloud project 'x'"):
            et.connect(FakeEE("Project 'x' not found or deleted."), "x", report)

    @unittest.skipUnless(EE_PLUGIN_LIBS.is_dir(), "Google Earth Engine plugin not installed")
    def test_library_from_gee_plugin(self):
        ee = et.import_earth_engine([str(EE_PLUGIN_LIBS)])
        self.assertTrue(hasattr(ee, "Initialize"))


if __name__ == "__main__":
    unittest.main()
