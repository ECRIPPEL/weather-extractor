"""Offline checks of the ERA5-Land extraction with a fake Earth Engine."""

from pathlib import Path
import sys
import tempfile
import unittest

_VENDOR = Path(__file__).resolve().parents[1] / "weather_extractor" / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.append(str(_VENDOR))

import numpy as np
import pandas as pd
import shapely

from weather_extractor import era5_processing as era5


# ERA5-Land in Earth Engine: 0.1 degree cells centred on multiples of 0.1 degree.
PROJECTION = ("EPSG:4326", [0.1, 0, -180.05, 0, -0.1, 90.05])
AOI = shapely.box(-53.84, -29.74, -53.76, -29.66)  # inside the cell centred on (-53.8, -29.7)
SEA_WEST = -53.55  # cells east of this are "sea" (no values)


class FakeEarthEngine:
    """getRegion tables with known hourly values."""

    def __init__(self, precipitation=None):
        self.requests = []
        self.precipitation = precipitation  # {image time: mm}, else 1 mm every hour

    def __call__(self, points, start, end, bands):
        self.requests.append((len(points), start, end, list(bands)))
        rows = [["id", "longitude", "latitude", "time"] + list(bands)]
        for time in pd.date_range(start, end, freq="h", inclusive="left"):
            for lon, lat in points:
                values = [self.value(band, time, lon) for band in bands]
                rows.append([time.strftime("%Y%m%dT%H"), lon, lat, int(time.value // 1_000_000)] + values)
        return rows

    def value(self, band, time, lon):
        if lon > SEA_WEST:
            return None
        if band == "total_precipitation_hourly":
            if self.precipitation is None:
                return 0.001
            return self.precipitation.get(time, 0.0) / 1000.0
        temperature = 273.15 + time.hour
        return {
            "temperature_2m": temperature,
            "dewpoint_temperature_2m": temperature - 5.0,
            "surface_solar_radiation_downwards_hourly": 360000.0,
            "u_component_of_wind_10m": 3.0,
            "v_component_of_wind_10m": 4.0,
            "surface_pressure": 100000.0,
            "total_evaporation_hourly": -0.0001,
            "runoff_hourly": 0.0002,
        }.get(band, 0.1 * int(band[-1]) if band.startswith("volumetric_soil_water") else None)


def settings(output_dir, **overrides):
    values = {
        "start_year": 2021, "end_year": 2021, "output_dir": str(output_dir), "timezone": "UTC",
        "timesteps": ["daily"], "swat_pcp_step": "daily", "grid_mode": "single", "block_km": None,
        "variables": ["pcp", "tmp", "slr", "hmd", "wnd", "evap", "runoff", "soil"],
        "parallel_workers": 3, "write_csv": True, "write_swat": True, "dem_path": "", "target_crs": "EPSG:4326",
    }
    values.update(overrides)
    return values


class Era5Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_era5(self, area=AOI, fetcher=None, **overrides):
        fetcher = fetcher or FakeEarthEngine()
        result = era5.extract_era5(settings(self.output, **overrides), area, fetcher=fetcher, projection=PROJECTION)
        return result, fetcher

    def test_units_daily_values_and_files(self):
        result, fetcher = self.run_era5()
        self.assertEqual(result["station_count"], 1)
        self.assertEqual(len(fetcher.requests), 1, "one station, one request for the year")
        daily = pd.read_csv(self.output / "era5_centroid_daily.csv")
        self.assertEqual(len(daily), 365)
        self.assertAlmostEqual((daily["lon"].iloc[0], daily["lat"].iloc[0])[0], -53.8, places=6)
        self.assertAlmostEqual(daily["lat"].iloc[0], -29.7, places=6)
        day = daily.iloc[10]
        self.assertAlmostEqual(day["pcp_mm"], 24.0, places=4)
        self.assertAlmostEqual((day["tmax_c"], day["tmin_c"])[0], 23.0, places=4)
        self.assertAlmostEqual(day["tmin_c"], 0.0, places=4)
        self.assertAlmostEqual(day["slr_mj_m2_day"], 8.64, places=4)
        self.assertAlmostEqual(day["wind_ms"], 5.0, places=4)
        self.assertTrue(0.6 < day["rh_fraction"] < 0.8, "dewpoint 5 degrees below the air")
        self.assertAlmostEqual(day["evap_mm"], 2.4, places=4)
        self.assertAlmostEqual(day["runoff_mm"], 4.8, places=4)
        self.assertEqual([round(day[f"soil_water_l{n}"], 4) for n in range(1, 5)], [0.1, 0.2, 0.3, 0.4])
        swat = self.output / "swatplus_weather"
        self.assertEqual(sorted(p.name for p in swat.glob("era5_centroid.*")),
                         ["era5_centroid.hmd", "era5_centroid.pcp", "era5_centroid.slr", "era5_centroid.tmp",
                          "era5_centroid.wnd"], "evaporation, runoff, and soil moisture are CSV only")
        self.assertIn("ECMWF ERA5-Land", (swat / "pcp.cli").read_text())
        self.assertTrue(result["gpkg"].name.startswith("era5_"))
        readme = (self.output / "README.txt").read_text(encoding="utf-8")
        self.assertIn("Copernicus Climate Change Service information", readme)
        self.assertIn("ECMWF/ERA5_LAND/HOURLY", readme)

    def test_hourly_totals_belong_to_the_hour_they_end(self):
        fake = FakeEarthEngine(precipitation={pd.Timestamp("2021-01-02 00:00", tz="UTC"): 7.0})
        self.run_era5(fetcher=fake, variables=["pcp"], timesteps=["daily", "hourly"])
        daily = pd.read_csv(self.output / "era5_centroid_daily.csv")
        self.assertEqual(daily["pcp_mm"].iloc[:2].tolist(), [7.0, 0.0], "the total ending at 00:00 is the day before")
        hourly = pd.read_csv(self.output / "era5_centroid_hourly.csv")
        self.assertEqual(hourly.loc[hourly["pcp_mm"] > 0, "datetime_utc"].tolist(), ["2021-01-01T23:00Z"])
        self.assertEqual(len(hourly), 8760)

    def test_local_days_and_hourly_reference_et(self):
        self.run_era5(variables=["tmp", "eto"], timesteps=["daily", "hourly"], timezone="Etc/GMT+3")
        hourly = pd.read_csv(self.output / "era5_centroid_hourly.csv")
        self.assertEqual(hourly["datetime_local"].iloc[0], "2021-01-01T00:00-0300")
        self.assertEqual(hourly["datetime_utc"].iloc[0], "2021-01-01T03:00Z")
        self.assertAlmostEqual(hourly["temp_c"].iloc[0], 3.0, places=4, msg="the 03 UTC value")
        self.assertTrue((hourly["eto_mm"] >= 0).all())
        daily = pd.read_csv(self.output / "era5_centroid_daily.csv")
        self.assertTrue(daily["eto_mm"].between(0.5, 10).all())

    def test_original_grid_blocks_and_requests(self):
        area = shapely.box(-53.96, -29.86, -53.54, -29.44)  # 4 x 4 cell centres inside
        result, fetcher = self.run_era5(area=area, grid_mode="original", variables=["pcp"])
        self.assertEqual(result["station_count"], 16)
        locations = pd.read_csv(self.output / "station_locations.csv")
        self.assertEqual(locations["era5_cells"].sum(), 16)
        self.assertEqual(sum(count for count, *_ in fetcher.requests), 16, "every cell asked once")
        result, _ = self.run_era5(area=area, grid_mode="block", block_km=50.0, variables=["pcp"])
        self.assertEqual(result["station_count"], 1)
        daily = pd.read_csv(self.output / "era5_50km_001_daily.csv")
        self.assertAlmostEqual(daily["pcp_mm"].iloc[3], 24.0, places=4)

    def test_rain_gauges_share_cells(self):
        gauges = [
            {"site_id": "ANA-2953001", "lon": -53.81, "lat": -29.71},
            {"site_id": "ANA-2953002", "lon": -53.79, "lat": -29.69},  # same cell
            {"site_id": "GHCN-USC1", "lon": -53.31, "lat": -29.21},  # outside the AOI box
        ]
        result, fetcher = self.run_era5(grid_mode="gauges", gauges=gauges, variables=["pcp"], write_swat=False)
        self.assertEqual(result["station_count"], 3)
        self.assertEqual(fetcher.requests[0][0], 2, "two cells for three gauges")
        locations = pd.read_csv(self.output / "station_locations.csv")
        self.assertEqual(locations["site_id"].tolist(), ["ANA-2953001", "ANA-2953002", "GHCN-USC1"])
        first = pd.read_csv(self.output / "era5_ana_2953001_daily.csv")
        self.assertEqual(first["site_id"].unique().tolist(), ["ANA-2953001"])

    def test_sea_cells_are_blank(self):
        sea = shapely.box(-53.46, -29.74, -53.36, -29.66)
        result, _ = self.run_era5(area=sea, variables=["pcp"])
        self.assertIn("no ERA5-Land value", result["warnings"][0])
        daily = pd.read_csv(self.output / "era5_centroid_daily.csv")
        self.assertTrue(daily["pcp_mm"].isna().all())
        rows = (self.output / "swatplus_weather" / "era5_centroid.pcp").read_text().splitlines()[3:]
        self.assertTrue(all(row.split()[-1] == "-99.000" for row in rows))

    def test_request_plan_stays_under_the_limit(self):
        start = pd.Timestamp("2021-01-01", tz="UTC")
        end = pd.Timestamp("2022-01-01 01:00", tz="UTC")
        self.assertEqual(len(era5.request_plan(1, 12, start, end)), 1)
        plan = era5.request_plan(500, 12, start, end)
        columns = era5.HEADER_COLUMNS + 12
        for first, last, window_start, window_end in plan:
            hours = int((window_end - window_start) / era5.HOUR)
            self.assertLessEqual((last - first) * hours * columns, era5.MAX_VALUES)
        covered = {}
        for first, last, window_start, window_end in plan:
            covered[(first, last)] = covered.get((first, last), 0) + int((window_end - window_start) / era5.HOUR)
        self.assertTrue(all(hours == 8761 for hours in covered.values()), "every hour of every cell group")
        self.assertEqual(sum(last - first for first, last in covered), 500)

    def test_grid_matches_cell_centres(self):
        grid = era5.Grid(PROJECTION[1], (-53.84, -29.74, -53.76, -29.66), margin=0.0)
        self.assertTrue(np.allclose(grid.longitude, [-53.8]))
        self.assertTrue(np.allclose(grid.latitude, [-29.7]))
        rows, cols = grid.cells([-53.8, -53.849], [-29.7, -29.651])
        self.assertEqual((list(rows), list(cols)), ([grid.rows[0]] * 2, [grid.cols[0]] * 2))


if __name__ == "__main__":
    unittest.main()
