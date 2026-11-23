"""Sub-basin feature selection keeps whole-AOI behavior as the default."""

import unittest

import shapely

from weather_extractor.aoi_selection import gauges_in_geometry, union_polygon_features


class AOISelectionTests(unittest.TestCase):
    def setUp(self):
        self.features = [
            (10, shapely.box(-100, 30, -99, 31)),
            (20, shapely.box(-98, 30, -97, 31)),
        ]

    def test_default_union_keeps_every_polygon_feature(self):
        geometry = union_polygon_features(self.features)
        self.assertEqual(geometry.geom_type, "MultiPolygon")
        self.assertEqual(geometry.bounds, (-100.0, 30.0, -97.0, 31.0))

    def test_selection_includes_only_chosen_subbasins(self):
        geometry = union_polygon_features(self.features, {20})
        self.assertEqual(geometry.geom_type, "Polygon")
        self.assertEqual(geometry.bounds, (-98.0, 30.0, -97.0, 31.0))

    def test_stale_selection_is_reported(self):
        with self.assertRaisesRegex(ValueError, "selected polygon features"):
            union_polygon_features(self.features, {99})

    def test_explicit_aoi_selection_filters_grid_gauges(self):
        area = union_polygon_features(self.features, {10})
        gauges = [
            {"site_id": "inside", "lon": -99.5, "lat": 30.5},
            {"site_id": "outside", "lon": -97.5, "lat": 30.5},
        ]
        self.assertEqual(
            [gauge["site_id"] for gauge in gauges_in_geometry(area, gauges)],
            ["inside"],
        )


if __name__ == "__main__":
    unittest.main()
