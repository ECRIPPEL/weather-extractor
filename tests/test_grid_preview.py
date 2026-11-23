"""Grid availability preview uses extraction station locations without reading weather data."""

from types import SimpleNamespace
import unittest

import numpy as np
import shapely

from weather_extractor.grid_preview import availability_summary, build_preview


STEP = 1.0 / 120.0
PROJECTION = ("EPSG:4326", [0.1, 0, -180.05, 0, -0.1, 90.05])


class FakeArchive:
    def __init__(self):
        self.latitude = SimpleNamespace(values=np.array([30.0, 30.0 + STEP, 30.0 + 2 * STEP]))
        self.longitude = SimpleNamespace(values=np.array([-97.0, -97.0 + STEP, -97.0 + 2 * STEP]))
        self.closed = False

    def close(self):
        self.closed = True


def settings(**overrides):
    values = {
        "dataset": "aorc",
        "start_year": 1979,
        "end_year": 2025,
        "grid_mode": "original",
        "block_km": 10,
        "gauges": [],
        "variables": ["pcp", "tmp"],
    }
    values.update(overrides)
    return values


class GridPreviewTests(unittest.TestCase):
    def test_aorc_multipart_area_previews_exact_grid_cells_and_closes_metadata(self):
        archive = FakeArchive()
        area = shapely.MultiPolygon(
            [
                shapely.box(-97.001, 29.999, -96.999, 30.001),
                shapely.box(-97.0 + 2 * STEP - 0.001, 30.0 + 2 * STEP - 0.001,
                            -97.0 + 2 * STEP + 0.001, 30.0 + 2 * STEP + 0.001),
            ]
        )
        result = build_preview(settings(), area, dataset_opener=lambda _year: archive)
        locations = [(station["lon"], station["lat"]) for station in result["stations"]]
        self.assertEqual(locations, [(-97.0, 30.0), (-97.0 + 2 * STEP, 30.0 + 2 * STEP)])
        self.assertTrue(archive.closed)

    def test_aorc_block_and_gauge_modes_use_the_extraction_layout(self):
        archive = FakeArchive()
        area = shapely.box(-97.001, 29.999, -96.98, 30.02)
        block = build_preview(
            settings(grid_mode="block", block_km=10),
            area,
            dataset_opener=lambda _year: archive,
        )
        self.assertTrue(block["stations"])
        self.assertTrue(all(station["cells"] > 0 for station in block["stations"]))

        gauge = {"site_id": "GHCN-001", "lon": -97.0, "lat": 30.0}
        result = build_preview(
            settings(grid_mode="gauges", gauges=[gauge]),
            area,
            dataset_opener=lambda _year: archive,
        )
        self.assertEqual(len(result["stations"]), 1)
        self.assertEqual(result["stations"][0]["station"], "aorc_ghcn_001")

    def test_era5_single_station_uses_the_selected_grid_cell(self):
        result = build_preview(
            settings(dataset="era5land", start_year=1950, grid_mode="single"),
            shapely.box(-53.84, -29.74, -53.76, -29.66),
            projection=PROJECTION,
        )
        station = result["stations"][0]
        self.assertAlmostEqual(station["lon"], -53.8)
        self.assertAlmostEqual(station["lat"], -29.7)
        self.assertIn("1950-01-01", result["summary"])

    def test_selected_subbasin_filters_grid_gauge_preview(self):
        area = shapely.box(-97.001, 29.999, -96.999, 30.001)
        gauges = [
            {"site_id": "inside", "lon": -97.0, "lat": 30.0},
            {"site_id": "outside", "lon": -97.0 + 2 * STEP, "lat": 30.0 + 2 * STEP},
        ]
        result = build_preview(
            settings(grid_mode="gauges", gauges=gauges, subbasin_feature_ids=[10]),
            area,
            dataset_opener=lambda _year: FakeArchive(),
        )
        self.assertEqual(len(result["stations"]), 1)
        self.assertIn("1 selected rain gauge(s)", result["summary"])
        self.assertIn("Selected sub-basins: 1 polygon(s)", result["summary"])

    def test_summary_discloses_source_coverage_selection_and_non_download(self):
        summary = availability_summary(
            settings(dataset="aorc", start_year=1979, variables=["pcp", "unknown"]), 12
        )
        self.assertIn("1979-02-01", summary)
        self.assertIn("Selected period: 1979-02-01 to 2025-12-31", summary)
        self.assertIn("Selected variables: precipitation.", summary)
        self.assertIn("12 extraction station(s)", summary)
        self.assertIn("No weather data was downloaded", summary)


if __name__ == "__main__":
    unittest.main()
