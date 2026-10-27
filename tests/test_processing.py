"""Offline checks with synthetic AORC archives (no QGIS GUI or AWS access)."""

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
import xarray as xr

from weather_extractor import aorc_processing as aorc


STEP = 1.0 / 120.0


def make_opener(cells=5, variables=None, missing_years=(), pcp_events=None):
    """Return a dataset opener producing deterministic hourly archives."""
    variables = variables or [
        "APCP_surface",
        "TMP_2maboveground",
        "SPFH_2maboveground",
        "PRES_surface",
        "DSWRF_surface",
        "UGRD_10maboveground",
        "VGRD_10maboveground",
    ]
    latitudes = 30.0 + np.arange(cells) * STEP
    longitudes = -97.0 + np.arange(cells) * STEP
    opened = []

    def opener(year):
        if year in missing_years:
            raise FileNotFoundError(f"{year}.zarr")
        opened.append(year)
        times = pd.date_range(f"{year}-01-01", f"{year}-12-31 23:00", freq="h")
        shape = (len(times), cells, cells)
        hour = times.hour.to_numpy()[:, None, None]
        column = np.arange(cells)[None, None, :]
        data = {}
        for name in variables:
            if name == "APCP_surface":
                values = np.zeros(shape)
                for stamp, amount in (pcp_events or {}).items():
                    position = times.get_indexer([pd.Timestamp(stamp)])[0]
                    if position >= 0:
                        values[position] = amount
                if pcp_events is None:
                    values = np.broadcast_to(column * 1.0, shape).copy()
            elif name == "TMP_2maboveground":
                values = np.broadcast_to(273.15 + hour, shape).astype(float)
            elif name == "SPFH_2maboveground":
                values = np.full(shape, 0.005)
            elif name == "PRES_surface":
                values = np.full(shape, 100000.0)
            elif name == "DSWRF_surface":
                values = np.full(shape, 100.0)
            elif name == "UGRD_10maboveground":
                values = np.full(shape, 3.0)
            else:
                values = np.full(shape, 4.0)
            data[name] = (("time", "latitude", "longitude"), values)
        return xr.Dataset(
            data,
            coords={"time": times.values, "latitude": latitudes, "longitude": longitudes},
        )

    opener.opened = opened
    return opener


def aoi(cells=5):
    return shapely.box(
        -97.0 - STEP / 2, 30.0 - STEP / 2, -97.0 + (cells - 0.5) * STEP, 30.0 + (cells - 0.5) * STEP
    )


def settings(output_dir, **overrides):
    values = {
        "start_year": 2021,
        "end_year": 2021,
        "output_dir": str(output_dir),
        "timezone": "UTC",
        "timesteps": ["daily"],
        "grid_mode": "single",
        "block_km": None,
        "variables": ["pcp", "tmp", "slr", "hmd", "wnd"],
        "parallel_workers": 2,
        "write_csv": True,
        "write_swat": True,
        "dem_path": "",
        "target_crs": "EPSG:4326",
    }
    values.update(overrides)
    return values


def swat_rows(path):
    return path.read_text(encoding="utf-8").splitlines()[3:]


class LayoutTests(unittest.TestCase):
    def test_original_grid_has_one_station_per_cell(self):
        window, stations, labels = aorc.build_layout(make_opener()(2021), aoi(), "original")
        self.assertEqual(len(stations), 25)
        self.assertEqual(sorted(labels), list(range(25)))

    def test_single_station_is_the_centroid_cell(self):
        window, stations, labels = aorc.build_layout(make_opener()(2021), aoi(), "single")
        self.assertEqual(len(stations), 1)
        self.assertEqual(stations[0]["cells"], 1)
        self.assertEqual(list(window.lat_indices), [2])
        self.assertEqual(list(window.lon_indices), [2])
        self.assertAlmostEqual(stations[0]["lon"], -97.0 + 2 * STEP)

    def test_single_station_works_for_tiny_aoi(self):
        tiny = shapely.box(-96.9999, 30.0001, -96.9998, 30.0002)
        _, stations, _ = aorc.build_layout(make_opener()(2021), tiny, "single")
        self.assertEqual(stations[0]["cells"], 1)

    def test_blocks_partition_cells(self):
        dataset = make_opener(cells=12, variables=["APCP_surface"])(2021)
        _, stations, labels = aorc.build_layout(dataset, aoi(12), "block", 5.0)
        self.assertGreater(len(stations), 1)
        self.assertEqual(sum(station["cells"] for station in stations), 144)
        self.assertEqual(len(np.unique(labels)), len(stations))

    def test_small_edge_blocks_join_a_neighbor(self):
        # 5 km blocks over 12 x 12 cells leave a one-row strip at the north edge.
        dataset = make_opener(cells=12, variables=["APCP_surface"])(2021)
        _, stations, labels = aorc.build_layout(dataset, aoi(12), "block", 5.0)
        self.assertEqual(len(stations), 4)
        self.assertEqual(sum(station["cells"] for station in stations), 144)
        self.assertGreaterEqual(min(station["cells"] for station in stations), 30)
        self.assertEqual(sorted(np.unique(labels)), list(range(4)))
        for index, station in enumerate(stations):
            self.assertEqual(int((labels == index).sum()), station["cells"])

    def test_rain_gauges_use_their_nearest_cell(self):
        gauges = [
            {"site_id": "GHCN-USC00000001", "lon": -97.0 + 2 * STEP + 0.001, "lat": 30.0 + STEP},
            # Two gauges in one cell share it.
            {"site_id": "USGS-0002", "lon": -97.0, "lat": 30.0},
            {"site_id": "GHCN-US1XX0003", "lon": -97.0 + 0.002, "lat": 30.0 - 0.001},
            {"site_id": "GHCN-USW00000004", "lon": -80.0, "lat": 30.0},  # off the grid
        ]
        window, stations, labels = aorc.build_layout(make_opener()(2021), aoi(), "gauges", gauges=gauges)
        self.assertEqual([s["station"] for s in stations],
                         ["aorc_ghcn_usc00000001", "aorc_usgs_0002", "aorc_ghcn_us1xx0003"])
        self.assertEqual([s["site_id"] for s in stations], ["GHCN-USC00000001", "USGS-0002", "GHCN-US1XX0003"])
        self.assertEqual(list(window.lat_indices), [1, 0, 0])
        self.assertEqual(list(window.lon_indices), [2, 0, 0])
        self.assertEqual(list(labels), [0, 1, 2])
        with self.assertRaisesRegex(ValueError, "rain gauges"):
            aorc.build_layout(make_opener()(2021), aoi(), "gauges", gauges=gauges[3:])

    def test_aoi_outside_grid_is_reported(self):
        with self.assertRaisesRegex(ValueError, "outside the AORC grid"):
            aorc.build_layout(make_opener()(2021), shapely.box(10, 10, 11, 11), "single")


def _pressure_at(elevation_m):
    return 101.3 * ((293.0 - 0.0065 * elevation_m) / 293.0) ** 5.26  # FAO-56 Eq. 7


class ReferenceEtTests(unittest.TestCase):
    """FAO-56 worked examples (Allen et al., 1998)."""

    def test_daily_example_18_brussels(self):
        # 6 July, 50.80 N, 100 m: Tmax 21.5, Tmin 12.3, ea 1.409 kPa, Rs 22.07 MJ/m2/day,
        # u2 2.078 m/s -> ETo = 3.9 mm/day.
        eto = aorc.reference_et_daily(
            np.array([187.0]), np.array([21.5]), np.array([12.3]), np.array([1.409]),
            np.array([_pressure_at(100)]), np.array([22.07]), np.array([2.078]), np.array([50.80]),
        )
        self.assertAlmostEqual(float(eto[0]), 3.9, delta=0.1)

    def test_hourly_example_19_ndiaye(self):
        # 1 October, 16.22 N, 16.25 W, 8 m, local standard time = UTC-1.
        # 14:00-15:00: T 38 C, RH 52 %, u2 3.3, Rs 2.450 MJ/m2/h -> ETo = 0.63 mm/h.
        es38 = 0.6108 * np.exp(17.27 * 38 / (38 + 237.3))
        day = aorc.reference_et_hourly(
            pd.DatetimeIndex(["2025-10-01 15:00"], tz="UTC"), np.array([[38.0]]),
            np.array([[0.52 * es38]]), np.array([[_pressure_at(8)]]), np.array([[2.450]]),
            np.array([[3.3]]), [16.22], [-16.25],
        )
        self.assertAlmostEqual(float(day[0, 0]), 0.63, delta=0.03)
        # 02:00-03:00: T 28 C, RH 90 %, u2 1.9, Rs 0 -> ETo = 0.0 mm/h.
        es28 = 0.6108 * np.exp(17.27 * 28 / (28 + 237.3))
        night = aorc.reference_et_hourly(
            pd.DatetimeIndex(["2025-10-01 03:00"], tz="UTC"), np.array([[28.0]]),
            np.array([[0.90 * es28]]), np.array([[_pressure_at(8)]]), np.array([[0.0]]),
            np.array([[1.9]]), [16.22], [-16.25],
        )
        self.assertAlmostEqual(float(night[0, 0]), 0.0, delta=0.02)


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_daily_single_station_values_and_files(self):
        result = aorc.extract_aorc(settings(self.output), aoi(), dataset_opener=make_opener())
        self.assertEqual(result["station_count"], 1)
        daily = pd.read_csv(self.output / "aorc_centroid_daily.csv")
        self.assertEqual(len(daily), 365)
        # The centroid cell is in column 2, which receives 2 mm per hour.
        self.assertAlmostEqual(daily["pcp_mm"].iloc[10], 48.0, places=4)
        self.assertAlmostEqual(daily["tmax_c"].iloc[10], 23.0, places=4)
        self.assertAlmostEqual(daily["tmin_c"].iloc[10], 0.0, places=4)
        self.assertAlmostEqual(daily["wind_ms"].iloc[10], 5.0, places=4)
        self.assertAlmostEqual(daily["slr_mj_m2_day"].iloc[10], 8.64, places=4)
        self.assertTrue(daily["rh_fraction"].between(0, 1).all())
        swat = self.output / "swatplus_weather"
        for variable in ("pcp", "tmp", "slr", "hmd", "wnd"):
            self.assertTrue((swat / f"aorc_centroid.{variable}").exists())
            self.assertIn("aorc_centroid." + variable, (swat / f"{variable}.cli").read_text())
        header = (swat / "aorc_centroid.pcp").read_text().splitlines()[2].split()
        self.assertEqual(header[:2], ["1", "0"])
        self.assertEqual(len(swat_rows(swat / "aorc_centroid.tmp")[0].split()), 4)
        # Random temp names may end in "_", which run_label strips.
        self.assertEqual(result["gpkg"].name, f"aorc_{aorc.run_label(self.output)}.gpkg")
        self.assertTrue(result["gpkg"].exists())
        statistics = pd.read_csv(self.output / "weather_statistics.csv")
        self.assertEqual(
            set(statistics["variable"]),
            {"pcp_mm", "tmax_c", "tmin_c", "slr_mj_m2_day", "rh_fraction", "wind_ms"},
        )
        manifest = (self.output / "README.txt").read_text()
        self.assertIn("Analysis of Record for Calibration (AORC) version 1.1", manifest)

    def test_precipitation_is_assigned_to_preceding_hour(self):
        opener = make_opener(pcp_events={"2021-01-02 00:00": 7.0, "2021-01-01 00:00": 3.0})
        aorc.extract_aorc(
            settings(self.output, variables=["pcp"]), aoi(), dataset_opener=opener
        )
        daily = pd.read_csv(self.output / "aorc_centroid_daily.csv")
        # The 00:00 Jan 2 accumulation belongs to Jan 1; the 00:00 Jan 1 value belongs to 2020.
        self.assertAlmostEqual(daily["pcp_mm"].iloc[0], 7.0)
        self.assertAlmostEqual(daily["pcp_mm"].iloc[1], 0.0)

    def test_local_days_read_the_next_archive(self):
        opener = make_opener(pcp_events={"2022-01-01 03:00": 5.0})
        aorc.extract_aorc(
            settings(self.output, timezone="Etc/GMT+6", variables=["pcp"]),
            aoi(),
            dataset_opener=opener,
        )
        self.assertIn(2022, opener.opened)
        daily = pd.read_csv(self.output / "aorc_centroid_daily.csv")
        self.assertEqual(daily["date"].iloc[-1], "2021-12-31")
        self.assertAlmostEqual(daily["pcp_mm"].iloc[-1], 5.0)

    def test_missing_neighbor_archive_is_a_warning(self):
        opener = make_opener(missing_years=(2022,))
        result = aorc.extract_aorc(
            settings(self.output, timezone="Etc/GMT+6", variables=["tmp"]),
            aoi(),
            dataset_opener=opener,
        )
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("last 6 hour(s) of 2021-12-31", result["warnings"][0])
        rows = swat_rows(self.output / "swatplus_weather" / "aorc_centroid.tmp")
        self.assertEqual(len(rows), 365)
        self.assertIn("last 6 hour(s)", (self.output / "warnings.txt").read_text())
        # A clean rerun into the same folder removes the stale warnings file.
        result = aorc.extract_aorc(
            settings(self.output, timezone="Etc/GMT+6", variables=["tmp"]),
            aoi(),
            dataset_opener=make_opener(),
        )
        self.assertIsNone(result["warnings_file"])
        self.assertFalse((self.output / "warnings.txt").exists())

    def test_partial_archive_year_is_a_warning(self):
        # The real 1979 archive starts on 1979-02-01.
        full_opener = make_opener(variables=["APCP_surface"])

        def opener(year):
            dataset = full_opener(year)
            return dataset.sel(time=slice(f"{year}-02-01", None)) if year == 2021 else dataset

        result = aorc.extract_aorc(
            settings(self.output, variables=["pcp"]), aoi(), dataset_opener=opener
        )
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("only covers 2021-02-01 to 2021-12-31", result["warnings"][0])
        daily = pd.read_csv(self.output / "aorc_centroid_daily.csv")
        # January 31 keeps one hour: the 00:00 February 1 accumulation belongs to it.
        self.assertEqual(int(daily["pcp_mm"].isna().sum()), 30)

    def test_selected_variables_only(self):
        opener = make_opener(variables=["UGRD_10maboveground", "VGRD_10maboveground"])
        aorc.extract_aorc(
            settings(self.output, variables=["wnd"]), aoi(), dataset_opener=opener
        )
        swat = self.output / "swatplus_weather"
        self.assertTrue((swat / "aorc_centroid.wnd").exists())
        self.assertFalse((swat / "aorc_centroid.pcp").exists())
        self.assertFalse((swat / "pcp.cli").exists())
        daily = pd.read_csv(self.output / "aorc_centroid_daily.csv")
        self.assertNotIn("pcp_mm", daily.columns)

    def test_hourly_swat_precipitation(self):
        aorc.extract_aorc(
            settings(
                self.output,
                timesteps=["hourly"],
                swat_pcp_step="hourly",
                timezone="Etc/GMT+6",
                variables=["pcp", "tmp"],
            ),
            aoi(),
            dataset_opener=make_opener(),
        )
        swat = self.output / "swatplus_weather"
        header = (swat / "aorc_centroid.pcp").read_text().splitlines()[2].split()
        self.assertEqual(header[1], "24")
        rows = swat_rows(swat / "aorc_centroid.pcp")
        self.assertEqual(len(rows), 365 * 24)
        self.assertEqual(rows[0].split()[:5], ["2021", "1", "1", "1", "0"])
        self.assertEqual(rows[23].split()[4], "23")
        self.assertEqual(len(swat_rows(swat / "aorc_centroid.tmp")), 365)
        self.assertEqual((swat / "aorc_centroid.tmp").read_text().splitlines()[2].split()[1], "0")
        hourly = pd.read_csv(self.output / "aorc_centroid_hourly.csv")
        self.assertEqual(len(hourly), 365 * 24)
        self.assertEqual(hourly["datetime_local"].iloc[0], "2021-01-01T00:00-0600")

    def test_hourly_swat_rejects_daylight_saving_timezone(self):
        with self.assertRaisesRegex(ValueError, "fixed-offset"):
            aorc.extract_aorc(
                settings(self.output, swat_pcp_step="hourly", timezone="America/Chicago"),
                aoi(),
                dataset_opener=make_opener(),
            )

    def test_daily_and_hourly_in_one_run(self):
        result = aorc.extract_aorc(
            settings(self.output, timesteps=["daily", "hourly"], swat_pcp_step="daily",
                     timezone="Etc/GMT+6", variables=["pcp", "tmp"]),
            aoi(),
            dataset_opener=make_opener(),
        )
        daily = pd.read_csv(self.output / "aorc_centroid_daily.csv")
        hourly = pd.read_csv(self.output / "aorc_centroid_hourly.csv")
        self.assertEqual((len(daily), len(hourly)), (365, 365 * 24))
        # Both come from the same hourly values.
        sums = hourly.groupby(hourly["datetime_local"].str[:10])["pcp_mm"].sum()
        self.assertTrue(np.allclose(sums.to_numpy(), daily["pcp_mm"].to_numpy()))
        self.assertAlmostEqual(hourly.groupby(hourly["datetime_local"].str[:10])["temp_c"].max().iloc[5],
                               daily["tmax_c"].iloc[5])
        header = (self.output / "swatplus_weather" / "aorc_centroid.pcp").read_text().splitlines()[2].split()
        self.assertEqual(header[1], "0", "SWAT+ precipitation follows its own time step")
        statistics = pd.read_csv(self.output / "weather_statistics.csv")
        self.assertEqual(set(statistics["time_step"]), {"daily", "hourly"})
        self.assertEqual(set(statistics.loc[statistics["time_step"] == "hourly", "variable"]), {"pcp_mm", "temp_c"})
        manifest = (self.output / "README.txt").read_text()
        self.assertIn("CSV time steps: daily, hourly", manifest)
        self.assertIn("SWAT+ precipitation time step: daily", manifest)
        self.assertEqual(result["station_count"], 1)

    def test_hourly_swat_precipitation_with_daily_csv(self):
        aorc.extract_aorc(
            settings(self.output, timesteps=["daily"], swat_pcp_step="hourly", timezone="Etc/GMT+6",
                     variables=["pcp"]),
            aoi(),
            dataset_opener=make_opener(),
        )
        self.assertFalse((self.output / "aorc_centroid_hourly.csv").exists())
        swat = self.output / "swatplus_weather" / "aorc_centroid.pcp"
        self.assertEqual(swat.read_text().splitlines()[2].split()[1], "24")
        self.assertEqual(len(swat_rows(swat)), 365 * 24)

    def test_csv_needs_a_time_step(self):
        with self.assertRaisesRegex(ValueError, "daily or the hourly"):
            aorc.extract_aorc(settings(self.output, timesteps=[]), aoi(), dataset_opener=make_opener())

    def test_rain_gauge_stations(self):
        gauges = [
            {"site_id": "GHCN-USC00000001", "lon": -97.0 + 2 * STEP, "lat": 30.0 + STEP},
            {"site_id": "USGS-0002", "lon": -97.0, "lat": 30.0},
            {"site_id": "GHCN-US1XX0003", "lon": -97.0 + 0.002, "lat": 30.0},
            {"site_id": "GHCN-USW00000004", "lon": -80.0, "lat": 30.0},
        ]
        result = aorc.extract_aorc(
            settings(self.output, grid_mode="gauges", gauges=gauges, timesteps=["hourly"], variables=["pcp"]),
            aoi(),
            dataset_opener=make_opener(),
        )
        self.assertEqual(result["station_count"], 3)
        self.assertIn("1 rain gauge(s) lie outside the AORC grid", result["warnings"][0])
        first = pd.read_csv(self.output / "aorc_ghcn_usc00000001_hourly.csv")
        self.assertEqual(first["site_id"].unique().tolist(), ["GHCN-USC00000001"])
        # Column 2 receives 2 mm per hour; gauges in one cell get the same values.
        self.assertAlmostEqual(first["pcp_mm"].iloc[5], 2.0)
        shared = [pd.read_csv(self.output / f"aorc_{name}_hourly.csv")["pcp_mm"] for name in ("usgs_0002", "ghcn_us1xx0003")]
        pd.testing.assert_series_equal(shared[0], shared[1])
        locations = pd.read_csv(self.output / "station_locations.csv")
        self.assertEqual(locations["site_id"].tolist(), ["GHCN-USC00000001", "USGS-0002", "GHCN-US1XX0003"])
        self.assertIn("rain gauges, the AORC cell nearest to each of the 4 gauges", (self.output / "README.txt").read_text())
        from osgeo import ogr

        datasource = ogr.Open(str(result["gpkg"]))
        layer = datasource.GetLayerByName("stations")
        self.assertEqual(sorted(f.GetField("site_id") for f in layer),
                         ["GHCN-US1XX0003", "GHCN-USC00000001", "USGS-0002"])
        datasource = None

    def test_block_grid_means_and_multiple_years(self):
        opener = make_opener(cells=12, variables=["APCP_surface"])
        result = aorc.extract_aorc(
            settings(
                self.output,
                start_year=2020,
                end_year=2021,
                grid_mode="block",
                block_km=5.0,
                variables=["pcp"],
                parallel_workers=3,
                target_crs="EPSG:32614",
            ),
            aoi(12),
            dataset_opener=opener,
        )
        self.assertGreater(result["station_count"], 1)
        locations = pd.read_csv(self.output / "station_locations.csv")
        self.assertEqual(locations["aorc_cells"].sum(), 144)
        self.assertTrue((locations["x_target_crs"] > 100000).all())
        first = locations["station"].iloc[0]
        rows = swat_rows(self.output / "swatplus_weather" / f"{first}.pcp")
        self.assertEqual(len(rows), 366 + 365)
        self.assertEqual(rows[0].split()[:2], ["2020", "1"])
        self.assertEqual(rows[-1].split()[:2], ["2021", "365"])
        header = (self.output / "swatplus_weather" / f"{first}.pcp").read_text().splitlines()[2]
        self.assertEqual(header.split()[0], "2")

    def test_chunked_reads_match_and_never_repeat(self):
        reads = []
        original_read = aorc._read_variable

        def logged_read(dataset, name, time_slice, lat_slice, lon_slice):
            archive = str(dataset.time.values[0])
            reads.append((archive, name, time_slice.start, lat_slice.start, lon_slice.start))
            return original_read(dataset, name, time_slice, lat_slice, lon_slice)

        options = dict(grid_mode="block", block_km=5.0, variables=["pcp"], write_swat=False)
        opener = make_opener(cells=12, variables=["APCP_surface"])
        aorc.extract_aorc(settings(self.output / "one", **options), aoi(12), dataset_opener=opener)

        original_chunks = aorc.ARCHIVE_CHUNKS
        aorc.ARCHIVE_CHUNKS = (144, 5, 7)
        aorc._read_variable = logged_read
        try:
            aorc.extract_aorc(
                settings(self.output / "tiled", parallel_workers=1, **options),
                aoi(12),
                dataset_opener=opener,
            )
        finally:
            aorc.ARCHIVE_CHUNKS = original_chunks
            aorc._read_variable = original_read

        self.assertGreater(len({read[3:] for read in reads}), 1)
        self.assertEqual(len(reads), len(set(reads)))
        for path in (self.output / "one").glob("*_daily.csv"):
            pd.testing.assert_frame_equal(
                pd.read_csv(path), pd.read_csv(self.output / "tiled" / path.name)
            )

    def test_reference_et_columns_and_swat_files(self):
        aorc.extract_aorc(
            settings(self.output, variables=["eto"]), aoi(), dataset_opener=make_opener()
        )
        daily = pd.read_csv(self.output / "aorc_centroid_daily.csv")
        self.assertIn("eto_mm", daily.columns)
        self.assertNotIn("tmax_c", daily.columns, "only the selected variables are written")
        self.assertTrue(daily["eto_mm"].between(0.5, 10).all())
        swat = self.output / "swatplus_weather"
        rows = swat_rows(swat / "aorc_centroid.pet")
        self.assertEqual(len(rows), 365)
        self.assertEqual(len(rows[0].split()), 3)
        self.assertIn("aorc_centroid.pet", (swat / "pet.cli").read_text())

    def test_reference_et_hourly(self):
        aorc.extract_aorc(
            settings(self.output, variables=["eto"], timesteps=["hourly"], timezone="Etc/GMT+6"),
            aoi(),
            dataset_opener=make_opener(),
        )
        hourly = pd.read_csv(self.output / "aorc_centroid_hourly.csv")
        self.assertEqual(len(hourly), 8760)
        self.assertTrue((hourly["eto_mm"] >= 0).all())
        self.assertGreater(hourly["eto_mm"].max(), 0.0)

    def test_fixed_offset_detection(self):
        self.assertTrue(aorc.has_fixed_offset("Etc/GMT+6", [2021]))
        self.assertTrue(aorc.has_fixed_offset("America/Phoenix", [2021]))
        self.assertFalse(aorc.has_fixed_offset("America/Chicago", [2021]))


if __name__ == "__main__":
    unittest.main()
