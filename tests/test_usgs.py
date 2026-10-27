"""Offline checks of the USGS tab logic with a fake web service."""

from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

_VENDOR = Path(__file__).resolve().parents[1] / "weather_extractor" / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.append(str(_VENDOR))

import pandas as pd
import shapely

from weather_extractor import usgs_client
from weather_extractor.usgs_export import estimate_requests, extract_usgs, ghcnh_hourly
from weather_extractor.usgs_inventory import GHCNH_DATASET, OTHER, USGS, check_availability


AOI = shapely.box(-88.5, 30.5, -87.5, 31.5)
INSIDE = {"A": (-88.0, 31.0), "C": (-88.1, 30.9), "D": (-87.9, 31.1), "E": (-87.8, 31.2), "G": (-88.2, 30.8)}


def point(site, lon=None, lat=None):
    lon, lat = (lon, lat) if lon is not None else INSIDE[site]
    return {"type": "Point", "coordinates": [lon, lat]}


def series(site, code, period, statistic="", begin="2000-01-01", end="2026-01-01", primary="Primary", sid=None, geometry=None):
    return {
        "id": sid or f"{site}-{code}-{period}-{statistic}-{primary}",
        "geometry": geometry or point(site),
        "properties": {
            "id": sid or f"{site}-{code}-{period}-{statistic}-{primary}",
            "monitoring_location_id": f"USGS-{site}",
            "parameter_code": code,
            "statistic_id": statistic,
            "computation_period_identifier": period,
            "begin": begin,
            "end": end,
            "primary": primary,
        },
    }


TIME_SERIES = [
    series("A", "00060", "Daily", "00003", sid="A-q-daily"),
    series("A", "00060", "Daily", "00003", primary="", sid="A-q-daily-secondary"),
    series("A", "00065", "Points", sid="A-gh-cont"),
    series("A", "00060", "Water Year", sid="A-q-peaks"),
    series("B", "00060", "Daily", "00003", geometry=point("B", -89.5, 31.0)),  # outside the AOI
    series("C", "00060", "Daily", "00003", begin="1950-01-01", end="1990-01-01"),  # outside the period
    series("D", "72019", "Daily", "00002", sid="D-gw-min"),
    series("E", "00095", "Daily", "00003", sid="E-sc-daily"),
    series("A", "99999", "Daily", "00003"),  # parameter not offered
]


def frame(rows):
    return pd.DataFrame(rows, dtype=str) if rows else pd.DataFrame()


ORG_NAMES = {"21AWIC": "ALABAMA DEPT. OF ENVIRONMENTAL MANAGEMENT - WATER QUALITY DATA", "USGS": "USGS-AL"}


class FakeClient:
    def __init__(self):
        self.calls = []

    def features(self, collection, params):
        self.calls.append((collection, dict(params)))
        if collection == "time-series-metadata":
            return TIME_SERIES
        if collection == "field-measurements-metadata":
            code = params["parameter_code"]
            if code == "00060":
                return [{"geometry": point("A"), "properties": {"monitoring_location_id": "USGS-A", "begin": "2010-01-01", "end": "2025-06-01"}}]
            if code == "72019":
                return [{"geometry": point("G"), "properties": {"monitoring_location_id": "USGS-G", "begin": "2015-01-01", "end": "2016-01-01"}}]
            return []
        if collection == "channel-measurements":
            return [
                {"geometry": point("A"), "properties": {"monitoring_location_id": "USGS-A", "time": "2020-05-01T15:00:00+00:00"}},
                {"geometry": point("A"), "properties": {"monitoring_location_id": "USGS-A", "time": "2020-09-01T15:00:00+00:00"}},
                {"geometry": point("B", -89.5, 31.0), "properties": {"monitoring_location_id": "USGS-B", "time": "2020-05-01T15:00:00+00:00"}},
            ]
        if collection == "monitoring-locations":
            return [
                {"id": f"USGS-{site}", "properties": {"monitoring_location_name": f"Site {site}", "site_type": "Stream", "agency_code": "USGS", "drainage_area": "10"}}
                for site in "ADEG"
            ]
        return []

    def csv(self, collection, params, sortby=None):
        self.calls.append((collection, dict(params)))
        if collection == "daily":
            start, _end = params["time"].split("/")
            dates = pd.date_range(start, periods=3, freq="D").strftime("%Y-%m-%d")
            return frame([{"time": d, "value": str(10 + i), "approval_status": "Provisional" if i else "Approved", "qualifier": ""} for i, d in enumerate(dates)])
        if collection == "continuous":
            start = params["time"].split("/")[0]
            if not start.startswith("2020-01-01"):
                return pd.DataFrame()
            return frame([
                {"time": "2020-01-01 00:00:00+00:00", "value": "1.5", "unit_of_measure": "ft", "approval_status": "Approved", "qualifier": ""},
                {"time": "2020-01-01 00:15:00+00:00", "value": "1.6", "unit_of_measure": "ft", "approval_status": "Approved", "qualifier": ""},
                # Real USGS data sometimes carries fractional seconds.
                {"time": "2020-01-01 00:18:04.702000+00:00", "value": "1.7", "unit_of_measure": "ft", "approval_status": "Approved", "qualifier": ""},
            ])
        if collection == "field-measurements":
            return frame([
                {"monitoring_location_id": "USGS-A", "time": "2020-03-01", "value": "100", "unit_of_measure": "ft^3/s", "approval_status": "Approved", "qualifier": ""},
                {"monitoring_location_id": "USGS-Z", "time": "2020-03-02", "value": "5", "unit_of_measure": "ft^3/s", "approval_status": "Approved", "qualifier": ""},
            ])
        if collection == "channel-measurements":
            common = {"channel_flow_unit": "ft^3/s", "channel_width_unit": "ft", "channel_area_unit": "ft^2",
                      "channel_velocity_unit": "ft/s", "channel_material": "Sand", "measurement_type": "Wading"}
            return frame([
                dict(common, monitoring_location_id="USGS-A", time="2020-05-01T15:00:00+00:00",
                     channel_flow="25.9", channel_width="35", channel_area="24", channel_velocity="1.08"),
                dict(common, monitoring_location_id="USGS-A", time="2020-09-01T15:00:00+00:00",
                     channel_flow="30", channel_width="", channel_width_unit="", channel_area="20", channel_velocity="1.5"),
                dict(common, monitoring_location_id="USGS-Z", time="2020-05-02T15:00:00+00:00",
                     channel_flow="1", channel_width="2", channel_area="1", channel_velocity="1"),
            ])
        if collection == "peaks":
            return frame([
                {"monitoring_location_id": "USGS-A", "water_year": "2020", "time": "2020-02-10", "value": "5000", "unit_of_measure": "ft^3/s", "qualifier": ""},
                {"monitoring_location_id": "USGS-A", "water_year": "1999", "time": "1999-02-10", "value": "7000", "unit_of_measure": "ft^3/s", "qualifier": ""},
            ])
        return pd.DataFrame()

    def wqp(self, endpoint, params):
        self.calls.append(("wqp/" + endpoint, dict(params)))
        if endpoint == "Station":
            stations = []
            if params.get("characteristicType") == "Nutrient":
                stations = [("21AWIC-1", -88.3, 30.7), ("USGS-0247", -88.25, 30.75), ("21AWIC-9", -90.0, 30.7)]
            if params.get("characteristicName") == "Temperature, water":
                stations = [("21AWIC-1", -88.3, 30.7)]
            return frame([
                {"MonitoringLocationIdentifier": s, "MonitoringLocationName": f"Point {s}", "MonitoringLocationTypeName": "River/Stream",
                 "OrganizationIdentifier": s.split("-")[0], "OrganizationFormalName": ORG_NAMES.get(s.split("-")[0], ""),
                 "LongitudeMeasure": str(lon), "LatitudeMeasure": str(lat)}
                for s, lon, lat in stations
            ])
        if endpoint == "Project":
            return frame([
                {"OrganizationIdentifier": "21AWIC", "ProjectIdentifier": "1097",
                 "ProjectName": "CY2020_COASTAL_WATERS_MONITORING_PROGRAM_MOBILE_BAY"},
                {"OrganizationIdentifier": "21AWIC", "ProjectIdentifier": "9", "ProjectName": "Unrelated project"},
            ])
        rows = []
        if params.get("characteristicType") == "Nutrient":
            rows.append({"MonitoringLocationIdentifier": "21AWIC-1", "CharacteristicName": "Phosphorus", "ResultMeasureValue": "",
                         "ResultDetectionConditionText": "Not Detected", "DetectionQuantitationLimitMeasure/MeasureValue": "0.01",
                         "ActivityStartDate": "2020-05-01", "OrganizationIdentifier": "21AWIC",
                         "OrganizationFormalName": ORG_NAMES["21AWIC"], "ProjectIdentifier": "1097"})
        if "Temperature, water" in params.get("characteristicName", ""):
            for name in ("Temperature, water", "pH"):
                rows.append({"MonitoringLocationIdentifier": "21AWIC-1", "CharacteristicName": name, "ResultMeasureValue": "20",
                             "ActivityStartDate": "2020-05-01", "OrganizationIdentifier": "21AWIC"})
        return frame(rows)

    def ncei_search(self, params):
        self.calls.append(("ncei/search", dict(params)))

        def station(station_id, lon, lat, start, end, data_type="PRCP", coverage=None):
            data = {"id": data_type, "dateRange": {"start": f"{start}T00:00:00", "end": f"{end}T23:59:59"}}
            if coverage is not None:
                data["coverage"] = coverage
            return {"location": {"coordinates": [lon, lat], "type": "point"},
                    "stations": [{"id": station_id, "name": f"Gauge {station_id}", "dataTypes": [data]}]}

        if params["dataset"] == GHCNH_DATASET:
            # One result per station and year; every result lists precipitation.
            def hourly(*args, coverage=100.0):
                return station(*args, data_type="precipitation", coverage=coverage)

            return [
                hourly("USW00013894", -88.24, 30.68, "2019-01-01", "2019-12-31"),
                hourly("USW00013894", -88.24, 30.68, "2020-01-01", "2020-12-31"),
                hourly("USC00010583", -88.0, 31.0, "2020-01-01", "2020-12-31"),
                hourly("USL000PTOA1", -88.03, 30.67, "2020-01-01", "2020-12-31", coverage=0.0),  # tide gauge
                hourly("USW00099999", -89.5, 31.0, "2020-01-01", "2020-12-31"),  # outside the AOI
            ]
        return [
            station("USC00010583", -88.0, 31.0, "1913-11-01", "2026-10-19"),
            station("US1ALBW0079", -89.5, 31.0, "1998-06-17", "2026-10-21"),  # outside the AOI
            station("USC00099999", -88.1, 30.9, "1950-01-01", "1960-12-31"),  # outside the period
        ]

    def ncei_data(self, params):
        self.calls.append(("ncei/data", dict(params)))
        stations = params["stations"].split(",")
        if params["dataset"] == GHCNH_DATASET:
            reports = [
                # Airport: routine reports at minute 56, specials with running totals.
                ("USW00013894", "2020-01-01T05:56:00", "0.5", "FM15"),
                ("USW00013894", "2020-01-01T06:15:00", "1.0", "FM16"),
                ("USW00013894", "2020-01-01T06:35:00", "2.0", "FM16"),
                ("USW00013894", "2020-01-01T06:56:00", "2.5", "FM15"),
                ("USW00013894", "2020-01-01T07:10:00", "", ""),
                ("USW00013894", "2020-01-01T07:56:00", "0.0", "FM15"),
                # Recording gauge: the total of the hour ending at minute 0.
                ("USC00010583", "2020-01-01T07:00:00", "1.2", "H-derived-HPD-C-high-res"),
                ("USC00010583", "2020-01-01T07:15:00", "", ""),
            ]
            return frame([
                {"": "", "DATE": d, "STATION": s, "precipitation": v, "precipitation_Measurement_Code": "",
                 "precipitation_Quality_Code": "5" if r.startswith("FM") else "",
                 "precipitation_Report_Type": r, "precipitation_Source_Code": "343"}
                for s, d, v, r in reports
                if s in stations and params["startDate"] <= d <= params["endDate"]
            ])
        rows = [
            ("USC00010583", "2020-01-01", "5.6", ",,7,0700"),
            ("USC00010583", "2020-01-02", "0.0", "T,,7,0700"),
            ("USC00010583", "2020-01-05", "12.3", ",X,7,0700"),
        ]
        return frame([
            {"STATION": s, "DATE": d, "PRCP": v, "PRCP_ATTRIBUTES": a}
            for s, d, v, a in rows
            if s in stations and params["startDate"] <= d <= params["endDate"]
        ])

    def samples(self, endpoint, params, **_kwargs):
        self.calls.append(("samples/" + endpoint, list(params)))
        sites = [value for key, value in params if key == "monitoringLocationIdentifier"]
        return frame([
            {"Location_Identifier": site, "Location_Name": "USGS point", "Result_Characteristic": "Nitrate",
             "Result_CharacteristicGroup": "Nutrient", "Result_Measure": "0.5", "Result_MeasureUnit": "mg/L",
             "Activity_StartDate": "2020-06-01", "Org_Identifier": "USGS-AL",
             "Org_FormalName": "USGS Alabama Water Science Center", "Project_Identifier": ""}
            for site in sites
        ])


SETTINGS = {"start_date": "2020-01-01", "end_date": "2020-12-31", "margin_km": 0.0}


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.inventory = check_availability(SETTINGS, AOI, client=self.client)

    def test_series_are_classified_and_filtered(self):
        inventory = self.inventory
        self.assertEqual(inventory.count("streamflow", "daily"), 1)
        self.assertEqual(inventory.count("gage_height", "continuous"), 1)
        self.assertEqual(inventory.count("streamflow", "peaks"), 1)
        self.assertEqual(inventory.count("streamflow", "field"), 1)
        self.assertEqual(inventory.count("groundwater_depth", "field"), 0, "2015-2016 is outside the period")
        self.assertEqual(inventory.count("groundwater_depth", "daily"), 1)
        self.assertEqual(inventory.count("specific_conductance", "daily"), 1)
        self.assertEqual(inventory.count("channel_geometry", "field"), 1, "site B is outside the AOI")
        channel = next(e for e in inventory.entries if e["row"] == "channel_geometry")
        self.assertEqual((channel["begin"], channel["end"]), ("2020-05-01", "2020-09-01"))
        daily = next(e for e in inventory.entries if e["row"] == "streamflow" and e["column"] == "daily")
        self.assertEqual(daily["series_id"], "A-q-daily", "the primary series is preferred")
        self.assertNotIn("USGS-B", inventory.sites, "outside the AOI")
        self.assertNotIn("USGS-C", inventory.sites, "outside the period")
        self.assertEqual(inventory.sites["USGS-A"]["name"], "Site A")

    def test_samples_sites_and_sources(self):
        inventory = self.inventory
        self.assertEqual(inventory.count("nutrients", "samples"), 2)
        self.assertEqual(inventory.count("nutrients", "samples", (OTHER,)), 1)
        self.assertEqual(inventory.count("nutrients", "samples", (USGS,)), 1)
        self.assertEqual(inventory.count("water_temperature", "samples"), 1)
        self.assertNotIn("21AWIC-9", inventory.sites)
        self.assertEqual(inventory.sites["USGS-0247"]["source"], USGS)

    def test_margin_includes_nearby_station(self):
        outlet = series("H", "00060", "Daily", "00003", geometry=point("H", -88.505, 31.0))
        with patch.object(sys.modules[__name__], "TIME_SERIES", TIME_SERIES + [outlet]):
            without = check_availability(SETTINGS, AOI, client=FakeClient())
            with_margin = check_availability(dict(SETTINGS, margin_km=1.0), AOI, client=FakeClient())
        self.assertNotIn("USGS-H", without.sites)
        self.assertIn("USGS-H", with_margin.sites)

    def test_visible_sites_follow_selection(self):
        inventory = self.inventory
        visible = inventory.visible_sites({("streamflow", "daily"), ("nutrients", "samples")})
        self.assertEqual(visible["quantity"], {"USGS-A"})
        self.assertEqual(visible["quality"], {"21AWIC-1", "USGS-0247"})
        visible = inventory.visible_sites({("nutrients", "samples")}, (OTHER,))
        self.assertEqual(visible["quality"], {"21AWIC-1"})
        self.assertEqual(visible["quantity"], set())
        # Channel geometry has its own map layer (drawn as a dash).
        visible = inventory.visible_sites({("channel_geometry", "field")})
        self.assertEqual(visible["channel"], {"USGS-A"})
        self.assertEqual(visible["quantity"], set())

    def test_ghcn_rain_gauges(self):
        inventory = self.inventory
        self.assertEqual(inventory.count("precipitation_ghcn", "daily"), 1, "one gauge in the AOI and period")
        site = inventory.sites["GHCN-USC00010583"]
        self.assertEqual((site["org"], site["type"], site["source"]), ("NOAA", "COOP", "noaa"))
        search = next(params for call, params in self.client.calls if call == "ncei/search")
        self.assertEqual(search["bbox"].split(",")[0], "31.500000", "NCEI bbox order is N,W,S,E")
        self.assertIn("GHCN-USC00010583", inventory.visible_sites({("precipitation_ghcn", "daily")})["quantity"])

    def test_ghcnh_hourly_gauges(self):
        inventory = self.inventory
        # The airport and the COOP gauge; the tide gauge reports no precipitation.
        self.assertEqual(inventory.count("precipitation_ghcn", "continuous"), 2)
        self.assertNotIn("GHCN-USL000PTOA1", inventory.sites)
        airport = next(e for e in inventory.entries if e["site_id"] == "GHCN-USW00013894")
        self.assertEqual((airport["begin"], airport["end"]), ("2020-01-01", "2020-12-31"), "2019 is outside the period")
        two_years = check_availability(dict(SETTINGS, start_date="2019-01-01"), AOI, client=FakeClient())
        airport = next(e for e in two_years.entries if e["site_id"] == "GHCN-USW00013894")
        self.assertEqual((airport["begin"], airport["end"]), ("2019-01-01", "2020-12-31"), "years are merged")
        self.assertEqual(inventory.sites["GHCN-USW00013894"]["type"], "First-order")
        # The COOP gauge has daily and hourly data under one site id.
        coop = {e["column"] for e in inventory.entries if e["site_id"] == "GHCN-USC00010583"}
        self.assertEqual(coop, {"daily", "continuous"})

    def test_rain_gauges_for_the_aorc_tab(self):
        gauges = self.inventory.rain_gauges({("precipitation_ghcn", "daily"), ("streamflow", "daily")})
        self.assertEqual([(g["site_id"], g["lon"], g["lat"]) for g in gauges], [("GHCN-USC00010583", -88.0, 31.0)])
        everything = self.inventory.rain_gauges({("precipitation_ghcn", "daily"), ("precipitation_ghcn", "continuous")})
        self.assertEqual([g["site_id"] for g in everything], ["GHCN-USC00010583", "GHCN-USW00013894"])
        self.assertEqual(self.inventory.rain_gauges({("precipitation_ghcn", "daily")}, site_ids=set()), [])

    def test_recent_period_warns_about_usgs_samples(self):
        inventory = check_availability(dict(SETTINGS, end_date="2025-01-01"), AOI, client=FakeClient())
        self.assertTrue(any("2024-03-11" in warning for warning in inventory.warnings))
        self.assertFalse(self.inventory.warnings)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name) / "run 1"
        self.output.mkdir()
        self.client = FakeClient()
        self.inventory = check_availability(SETTINGS, AOI, client=self.client)
        self.selection = [
            ("streamflow", "daily"), ("gage_height", "continuous"), ("streamflow", "peaks"),
            ("streamflow", "field"), ("groundwater_depth", "daily"), ("specific_conductance", "daily"),
            ("nutrients", "samples"), ("water_temperature", "samples"), ("channel_geometry", "field"),
        ]

    def tearDown(self):
        self.temp.cleanup()

    def run_export(self, **overrides):
        settings = {"output_dir": str(self.output), "target_crs": "EPSG:4326", "selection": self.selection,
                    "sample_sources": (USGS, OTHER), "site_ids": None, "write_shapefile": True}
        settings.update(overrides)
        return extract_usgs(settings, self.inventory, client=self.client)

    def test_all_outputs(self):
        result = self.run_export()
        base = self.output / "usgs_water_data"
        daily = pd.read_csv(base / "quantity" / "daily" / "usgs_a_daily.csv", dtype=str)
        self.assertEqual(len(daily), 366)
        for column in ("streamflow_cfs", "streamflow_m3s", "streamflow_status", "streamflow_qualifier"):
            self.assertIn(column, daily.columns)
        self.assertAlmostEqual(float(daily["streamflow_m3s"].iloc[0]), 10 * 0.028316846592, places=4)
        groundwater = pd.read_csv(base / "quantity" / "daily" / "usgs_d_daily.csv")
        self.assertIn("groundwater_depth_min_ft", groundwater.columns)
        self.assertIn("groundwater_depth_min_m", groundwater.columns)
        sensor = pd.read_csv(base / "quality" / "daily" / "usgs_e_daily.csv")
        self.assertIn("specific_conductance_uScm", sensor.columns)
        continuous = pd.read_csv(base / "quantity" / "continuous" / "usgs_a_gage_height.csv")
        self.assertEqual(
            continuous["datetime_utc"].tolist(),
            ["2020-01-01T00:00:00Z", "2020-01-01T00:15:00Z", "2020-01-01T00:18:04Z"],
        )
        peaks = pd.read_csv(base / "quantity" / "annual_peaks.csv")
        self.assertEqual(peaks["water_year"].tolist(), [2020], "peaks outside the period are dropped")
        field = pd.read_csv(base / "quantity" / "field_measurements.csv")
        self.assertEqual(field["site_id"].tolist(), ["USGS-A"], "unselected sites are dropped")
        channel = pd.read_csv(base / "quantity" / "channel_measurements.csv")
        self.assertEqual(channel["site_id"].tolist(), ["USGS-A", "USGS-A"])
        first = channel.iloc[0]
        self.assertAlmostEqual(first["width_m"], 35 * 0.3048, places=4)
        self.assertAlmostEqual(first["area_m2"], 24 * 0.09290304, places=4)
        self.assertAlmostEqual(first["mean_depth_m"], (24 * 0.09290304) / (35 * 0.3048), places=3)
        self.assertEqual(first["material"], "Sand")
        self.assertTrue(pd.isna(channel.iloc[1]["mean_depth_m"]), "no width, no depth")
        samples = pd.read_csv(base / "quality" / "samples.csv", dtype=str, keep_default_na=False)
        self.assertEqual(sorted(samples["source"].unique()), ["USGS Samples", "Water Quality Portal"])
        self.assertNotIn("pH", samples["characteristic"].tolist(), "only the selected characteristic is kept")
        nondetect = samples[samples["characteristic"] == "Phosphorus"].iloc[0]
        self.assertEqual((nondetect["detection_condition"], nondetect["detection_limit"]), ("Not Detected", "0.01"))
        # Quantity: A (streamflow, gage height) and D (groundwater); quality: sensor site E
        # and the sampling points 21AWIC-1 and USGS-0247.
        self.assertEqual(result["station_count"], 2)
        self.assertEqual(result["point_count"], 3)
        self.assertTrue(result["gpkg"].name.startswith("usgs_run_1"))
        self.assertEqual(
            result["layers"], ["quantity_stations", "channel_geometry_stations", "quality_points"]
        )
        from osgeo import ogr

        datasource = ogr.Open(str(result["gpkg"]))
        channel_layer = datasource.GetLayerByName("channel_geometry_stations")
        self.assertEqual(channel_layer.GetFeatureCount(), 1)
        self.assertEqual(channel_layer.GetNextFeature().GetField("datasets"), "Channel geometry: field")
        datasource = None
        for shapefile in result["shapefiles"]:
            self.assertTrue(shapefile.exists())
        available = pd.read_csv(base / "available_data.csv")
        usgs_and_wqp = available[available["variable"] != "precipitation_ghcn"]
        self.assertTrue(usgs_and_wqp["selected"].all(), "every available USGS and WQP dataset was selected")
        self.assertIn("provisional", " ".join(result["warnings"]))
        self.assertEqual(result["warnings_file"], base / "warnings.txt")
        self.assertIn("provisional", (base / "warnings.txt").read_text())
        self.assertIn("listed in warnings.txt", (base / "README.txt").read_text())

    def test_readme_lists_included_data(self):
        self.run_export()
        base = self.output / "usgs_water_data"
        readme = (base / "README.txt").read_text(encoding="utf-8")
        lines = readme.splitlines()

        def line_with(*words):
            return next((line for line in lines if all(word in line for word in words)), None)

        self.assertIn("1. USGS Water Data API\n   https://api.waterdata.usgs.gov/ogcapi/v1\n", readme)
        self.assertIn("2. USGS Samples\n   https://api.waterdata.usgs.gov/samples-data\n", readme)
        self.assertIn("3. Water Quality Portal (EPA)\n   https://www.waterqualitydata.us/data", readme)
        daily = line_with("Streamflow", "daily", "USGS (U.S. Geological Survey)")
        self.assertIsNotNone(daily)
        cells = re.split(r"\s{2,}", daily.strip())
        self.assertEqual(cells[2:4], ["1", "3"], "1 station, 3 daily values with data")
        self.assertIsNotNone(line_with("Channel geometry", "field", "2020-05-01", "2020-09-01"))
        self.assertIsNotNone(line_with("Nutrients", "samples", "USGS-AL (USGS Alabama Water Science Center)"))
        wqp = line_with("Nutrients", "samples", "21AWIC (ALABAMA DEPT. OF ENVIRONMENTAL MANAGEMENT")
        self.assertIsNotNone(wqp)
        self.assertIn("2020-05-01", wqp)
        project = line_with("21AWIC", "1097", "CY2020_COASTAL_WATERS_MONITORING_PROGRAM_MOBILE_BAY")
        self.assertIsNotNone(project, "project names come from one Project request")
        self.assertIsNotNone(line_with("21AWIC", "(none)"), "results without a project are counted too")
        self.assertIn("Water-quality points by organization:", readme)
        self.assertIn("Not included:", readme)
        project_calls = [params for call, params in self.client.calls if call == "wqp/Project"]
        self.assertEqual(project_calls, [{"organization": "21AWIC"}])
        samples = pd.read_csv(base / "quality" / "samples.csv")
        self.assertNotIn("project", samples.columns, "samples.csv keeps its columns")
        self.assertNotIn("organization_name", samples.columns)

    def test_ghcn_precipitation_and_missing_days(self):
        result = self.run_export(selection=[("precipitation_ghcn", "daily"), ("streamflow", "daily")],
                                 write_shapefile=False)
        base = self.output / "usgs_water_data"
        gauge = pd.read_csv(base / "quantity" / "daily" / "ghcnd_usc00010583_daily.csv", dtype=str,
                            keep_default_na=False).set_index("date")
        self.assertEqual(len(gauge), 366, "every day of the period")
        self.assertEqual(gauge.loc["2020-01-01", "precipitation_mm"], "5.6")
        self.assertEqual(gauge.loc["2020-01-01", "observation_time"], "0700")
        self.assertEqual(
            (gauge.loc["2020-01-02", "precipitation_mm"], gauge.loc["2020-01-02", "measurement_flag"]), ("0", "T")
        )
        self.assertEqual(gauge.loc["2020-01-05", "quality_flag"], "X")
        missing = gauge.loc["2020-01-03"]
        self.assertEqual(missing["precipitation_mm"], "-99")
        self.assertEqual((missing["measurement_flag"], missing["observation_time"]), ("", ""))
        # USGS daily series use -99 too, in the original and the metric column.
        usgs = pd.read_csv(base / "quantity" / "daily" / "usgs_a_daily.csv", dtype=str,
                           keep_default_na=False).set_index("date")
        self.assertEqual(usgs.loc["2020-01-04", ["streamflow_cfs", "streamflow_m3s"]].tolist(), ["-99", "-99"])
        self.assertEqual(usgs.loc["2020-01-04", "streamflow_status"], "")
        self.assertEqual(usgs.loc["2020-01-01", "streamflow_cfs"], "10")
        data_calls = [params for call, params in self.client.calls if call == "ncei/data"]
        self.assertEqual([(p["dataset"], p["stations"], p["startDate"], p["endDate"]) for p in data_calls],
                         [("daily-summaries", "USC00010583", "2020-01-01", "2020-12-31")])
        readme = (base / "README.txt").read_text(encoding="utf-8")
        self.assertIn("4. NOAA GHCN-Daily (NCEI)", readme)
        line = next(l for l in readme.splitlines() if l.startswith("   Precipitation (NOAA GHCN)"))
        cells = re.split(r"\s{2,}", line.strip())
        self.assertEqual(cells[2:4], ["1", "3"], "1 gauge, 3 days with values")
        self.assertIn("COOP (NWS Cooperative Observer Program)", line)
        self.assertIn("Quantity stations by source: NOAA 1, USGS 1", readme)
        self.assertIn("-99 marks a day without a value", readme)
        from osgeo import ogr

        datasource = ogr.Open(str(result["gpkg"]))
        layer = datasource.GetLayerByName("quantity_stations")
        gauges = [f for f in layer if f.GetField("site_id") == "GHCN-USC00010583"]
        self.assertEqual([(f.GetField("org"), f.GetField("source"), f.GetField("site_type")) for f in gauges],
                         [("NOAA", "NOAA", "COOP")])
        datasource = None

    def test_ghcnh_hourly_precipitation(self):
        result = self.run_export(selection=[("precipitation_ghcn", "continuous")], write_shapefile=False,
                                 timezone="Etc/GMT+6")
        base = self.output / "usgs_water_data"
        airport = pd.read_csv(base / "quantity" / "hourly" / "ghcnh_usw00013894_hourly.csv", dtype=str,
                              keep_default_na=False)
        self.assertEqual(len(airport), 366 * 24, "every hour of the local days")
        first = airport.iloc[0]
        self.assertEqual((first["datetime_local"], first["datetime_utc"]), ("2020-01-01T00:00-0600", "2020-01-01T06:00Z"))
        # The last report of the hour: the routine total, not the specials added up.
        self.assertEqual((first["precipitation_mm"], first["report_type"], first["report_time_utc"]),
                         ("2.5", "FM15", "2020-01-01T06:56Z"))
        self.assertEqual(airport.iloc[1]["precipitation_mm"], "0")
        self.assertEqual((airport.iloc[2]["precipitation_mm"], airport.iloc[2]["report_type"]), ("-99", ""))
        self.assertEqual(airport["site_id"].unique().tolist(), ["GHCN-USW00013894"])
        # A report at minute 0 closes the hour before it.
        coop = pd.read_csv(base / "quantity" / "hourly" / "ghcnh_usc00010583_hourly.csv", dtype=str)
        self.assertEqual((coop.iloc[0]["datetime_utc"], coop.iloc[0]["precipitation_mm"]), ("2020-01-01T06:00Z", "1.2"))
        calls = [params for call, params in self.client.calls if call == "ncei/data"]
        self.assertTrue(all(p["dataset"] == GHCNH_DATASET for p in calls))
        self.assertEqual(calls[0]["stations"], "USC00010583,USW00013894")
        self.assertEqual(calls[0]["startDate"], "2020-01-01T05:00:00", "one hour before the first local hour")
        self.assertEqual(calls[-1]["endDate"], "2021-01-01T06:59:59", "one hour after the last local hour")
        settings = {"selection": [("precipitation_ghcn", "continuous")], "sample_sources": (USGS, OTHER),
                    "site_ids": None, "timezone": "Etc/GMT+6"}
        self.assertEqual(estimate_requests(self.inventory, settings), len(calls))
        readme = (base / "README.txt").read_text(encoding="utf-8")
        self.assertIn("5. NOAA GHCNh, hourly (NCEI)", readme)
        line = next(l for l in readme.splitlines() if l.startswith("   Precipitation (NOAA GHCN)") and "First-order" in l)
        self.assertEqual(re.split(r"\s{2,}", line.strip())[1:4], ["hourly", "1", "2"], "1 airport, 2 hours with totals")
        self.assertIn("Timezone of the hourly GHCNh files: Etc/GMT+6", readme)
        self.assertEqual(result["station_count"], 2)

    def test_ghcnh_hour_of_each_report(self):
        rows = frame([
            {"DATE": d, "precipitation": v, "precipitation_Report_Type": r}
            for d, v, r in [
                ("2020-03-01T12:56:00", "1.0", "FM15"), ("2020-03-01T13:15:00", "0.4", "FM16"),
                ("2020-03-01T13:56:00", "0.9", "FM15"),
                # A special just after the routine report starts the next hour.
                ("2020-03-01T13:58:00", "0.3", "FM16"), ("2020-03-01T14:56:00", "0.5", "FM15"),
                ("2020-03-01T15:56:00", "-9999", "FM15"),
            ]
        ])
        hourly = ghcnh_hourly(rows)
        self.assertEqual(hourly.index.strftime("%H").tolist(), ["12", "13", "14"])
        self.assertEqual(hourly["value"].tolist(), [1.0, 0.9, 0.5])

    def test_results_with_several_projects(self):
        from weather_extractor.usgs_export import _project_name

        names = {("21AWIC", "1097"): "CWMP 2023"}
        self.assertEqual(_project_name(names, "21AWIC", "1097"), "CWMP 2023")
        self.assertEqual(_project_name(names, "21AWIC", "1097;EPACONTINUOUS"), "1097: CWMP 2023; EPACONTINUOUS")
        self.assertEqual(_project_name(names, "21AWIC", ""), "")

    def test_only_selected_sites(self):
        result = self.run_export(site_ids=["USGS-A"], write_shapefile=False)
        base = self.output / "usgs_water_data"
        self.assertEqual(result["station_count"], 1)
        self.assertEqual(result["point_count"], 0)
        self.assertFalse((base / "quality" / "samples.csv").exists())
        self.assertEqual(result["shapefiles"], [])

    def test_sample_source_filter(self):
        self.run_export(sample_sources=(OTHER,))
        base = self.output / "usgs_water_data"
        samples = pd.read_csv(base / "quality" / "samples.csv")
        self.assertEqual(samples["source"].unique().tolist(), ["Water Quality Portal"])
        available = pd.read_csv(base / "available_data.csv")
        usgs_samples = available[(available["format"] == "samples") & (available["source"] == USGS)]
        self.assertFalse(usgs_samples["selected"].any())
        self.assertTrue(available["selected"].any())

    def test_request_estimate(self):
        settings = {"selection": self.selection, "sample_sources": (USGS, OTHER), "site_ids": None}
        # 3 daily series, 2 one-year continuous chunks for the 366 days of 2020,
        # field + peaks + channel tables, and 3 sample batches (USGS nutrients,
        # WQP nutrients, WQP water temperature).
        self.assertEqual(estimate_requests(self.inventory, settings), 3 + 2 + 3 + 3)
        settings["site_ids"] = ["USGS-A"]
        self.assertEqual(estimate_requests(self.inventory, settings), 1 + 2 + 3)
        # One GHCN-Daily request: one batch of stations, one ten-year window.
        settings = {"selection": [("precipitation_ghcn", "daily")], "sample_sources": (USGS, OTHER), "site_ids": None}
        self.assertEqual(estimate_requests(self.inventory, settings), 1)

    def test_nothing_selected_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "Nothing is selected"):
            self.run_export(selection=[("velocity", "field")])


class FakeResponse:
    def __init__(self, status, text="", headers=None, payload=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        return self.responses.pop(0)


class ClientTests(unittest.TestCase):
    def client(self, responses, api_key=""):
        client = usgs_client.UsgsClient(api_key, sleep=lambda seconds: None)
        session = FakeSession(responses)
        client._session = lambda: session
        return client, session

    def test_retries_after_rate_limit(self):
        client, session = self.client([FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(200, "ok")], "KEY")
        self.assertEqual(client.request(usgs_client.WATERDATA_URL).text, "ok")
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[0][2], {"X-Api-Key": "KEY"})

    def test_rate_limit_message_suggests_key(self):
        client, _ = self.client([FakeResponse(429, headers={"Retry-After": "0"})] * usgs_client.MAX_ATTEMPTS)
        with self.assertRaisesRegex(RuntimeError, "API key"):
            client.request(usgs_client.WATERDATA_URL)

    def test_client_error_is_reported(self):
        client, _ = self.client([FakeResponse(400, payload={"description": "Limit of 50000 exceeded"})])
        with self.assertRaisesRegex(RuntimeError, "Limit of 50000"):
            client.request(usgs_client.WATERDATA_URL)

    def test_csv_paging_with_offset(self):
        pages = ["a,b\n1,2\n3,4\n", "a,b\n5,6\n7,8\n", "a,b\n9,10\n"]
        client, session = self.client([FakeResponse(200, text) for text in pages])
        with patch.object(usgs_client, "CSV_LIMIT", 2):
            table = client.csv("daily", {"x": "1"}, sortby="time")
        self.assertEqual(table["a"].tolist(), ["1", "3", "5", "7", "9"])
        self.assertEqual([call[1]["offset"] for call in session.calls], [0, 2, 4])
        self.assertEqual(session.calls[0][1]["sortby"], "time")

    def test_feature_paging_follows_next_links(self):
        pages = [
            FakeResponse(200, payload={"features": [1, 2], "links": [{"rel": "next", "href": "https://next"}]}),
            FakeResponse(200, payload={"features": [3], "links": []}),
        ]
        client, session = self.client(pages)
        self.assertEqual(client.features("daily", {"bbox": "x"}), [1, 2, 3])
        self.assertEqual(session.calls[1][0], "https://next")

    def test_cancel_stops_requests(self):
        client = usgs_client.UsgsClient(is_canceled=lambda: True)
        with self.assertRaises(InterruptedError):
            client.request(usgs_client.WATERDATA_URL)


if __name__ == "__main__":
    unittest.main()
