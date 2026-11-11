"""Offline checks of the HidroWeb tab logic with a fake ANA service."""

from pathlib import Path
import sys
import tempfile
import unittest

_VENDOR = Path(__file__).resolve().parents[1] / "weather_extractor" / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.append(str(_VENDOR))

import pandas as pd
import shapely

from weather_extractor import hidroweb_client
from weather_extractor.hidroweb_catalog import ROWS_BY_KEY
from weather_extractor.hidroweb_export import daily_values, estimate_requests, extract_hidroweb
from weather_extractor.hidroweb_inventory import check_availability, rain_code_ranges, sub_basin_ranges


AOI = shapely.box(-53.9, -29.9, -53.6, -29.6)
SETTINGS = {"start_date": "2020-01-01", "end_date": "2020-03-31", "margin_km": 0.0}


def station(code, kind, lon, lat, sub_basin="85", **periods):
    record = {"Codigo": code, "TipoEstacao": kind, "Nome": f"Station {code}", "Longitude": str(lon),
              "Latitude": str(lat), "SubBaciaCodigo": sub_basin, "OperadoraSigla": "SGB-CPRM",
              "ResponsavelSigla": "ANA", "RioNome": "RIO IBICUÍ", "nmEstado": "RIO GRANDE DO SUL",
              "nmMunicipio": "SANTA MARIA", "AreaDrenagem": "1000", "Altitude": "95", "Operando": "1"}
    for name, (begin, end) in periods.items():
        record[f"Periodo{name}Inicio"] = f"{begin} 00:00:00"
        record[f"Periodo{name}Fim"] = f"{end} 00:00:00" if end else None
    return record


RAIN = [
    station("2953001", "2", -53.8, -29.7, Pluviometro=("1990-01-01", None)),
    station("2953002", "2", -53.95, -29.95, Pluviometro=("1990-01-01", None)),  # outside the AOI
    station("2953003", "2", -53.7, -29.8, Pluviometro=("1970-01-01", "2010-12-01")),  # outside the period
    station("2953005", "2", -53.75, -29.75, Pluviometro=("1990-01-01", None)),  # no values in the period
]
STREAMFLOW = [
    station("85400000", "1", -53.7, -29.65, Escala=("1980-01-01", None), DescLiquida=("1980-01-01", None)),
    station("85600000", "1", -52.0, -29.0, Escala=("1980-01-01", None)),  # outside the AOI
]


def month(first, prefix, values, consistency, mean=None):
    record = {"DataHora": f"{first} 00:00:00", "NivelConsistencia": str(consistency)}
    if mean is not None:
        record["MediaDiaria"] = str(mean)
    for day, value in values.items():
        record[f"{prefix}{day:02d}"] = value
    return record


SERIES = {
    ("85400000", 3): [
        month("2020-01-01", "Vazao", {1: "12.5", 2: "13.0"}, 2, 1),
        month("2020-01-01", "Vazao", {1: "99", 2: "99", 3: "14.0"}, 1, 1),  # raw: only day 3 is new
        month("2020-02-01", "Vazao", {1: "20.0", 29: "21.0", 30: "22.0"}, 1, 1),  # 2020-02-30 does not exist
    ],
    ("85400000", 1): [
        month("2020-01-01", "Cota", {1: "150"}, 1, 0),  # single reading
        month("2020-01-01", "Cota", {1: "148"}, 1, 1),  # daily mean
    ],
    ("2953001", 2): [month("2020-01-01", "Chuva", {1: "1,5", 2: "0"}, 2)],
}


class FakeAna:
    uses_api = False

    def __init__(self):
        self.calls = []

    def inventory(self, attempts=5, timeout=None, **filters):
        self.calls.append(dict(filters))
        stations = RAIN if filters["tpEst"] == 2 else STREAMFLOW
        return [r for r in stations if filters["codEstDE"] <= int(r["Codigo"]) <= filters["codEstATE"]]

    def series(self, code, data_type, start, end):
        self.calls.append(("series", code, data_type, start, end))
        return SERIES.get((code, data_type), [])

    def wait(self, seconds):
        self.calls.append(("wait", seconds))


class BusyAna(FakeAna):
    """Fails on streamflow code ranges wider than 100,000 codes, like an overloaded ServiceANA."""

    def inventory(self, attempts=5, timeout=None, **filters):
        if filters["tpEst"] == 1 and filters["codEstATE"] - filters["codEstDE"] >= 100_000:
            self.calls.append(("failed", filters["codEstDE"], filters["codEstATE"]))
            raise RuntimeError("HTTP 500")
        return super().inventory(attempts, timeout, **filters)


class RefusingAna(FakeAna):
    """Refuses (HTTP 429) the first ``refusals`` requests for one rain cell and one series."""

    def __init__(self, refusals=1):
        super().__init__()
        self.refusals = {"cell": refusals, "series": refusals}

    def _refuse(self, key):
        if self.refusals[key] > 0:
            self.refusals[key] -= 1
            raise RuntimeError("The ANA web service (ServiceANA) did not respond (HTTP 429).")

    def inventory(self, attempts=5, timeout=None, **filters):
        if filters["codEstDE"] == 2953000:
            self._refuse("cell")
        return super().inventory(attempts, timeout, **filters)

    def series(self, code, data_type, start, end):
        if code == "85400000":
            self._refuse("series")
        return super().series(code, data_type, start, end)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeAna()
        self.inventory = check_availability(SETTINGS, AOI, client=self.client)

    def test_rain_gauge_codes_one_range_per_latitude(self):
        self.assertEqual(rain_code_ranges(AOI), [(2953000, 2954999), (3053000, 3054999)])
        north = rain_code_ranges(shapely.box(-60.8, 2.7, -60.6, 2.9))
        self.assertIn((260000, 261999), north)
        self.assertIn((8260000, 8261999), north, "gauges north of the equator start with 8")
        wide = rain_code_ranges(shapely.box(-60.0, -29.5, -40.0, -29.2))
        self.assertEqual(len(wide), 6, "2 degrees of latitude x 3 groups of up to 10 degrees of longitude")

    def test_neighbouring_sub_basins_share_a_request(self):
        self.assertEqual(sub_basin_ranges([85, 70, 71, 72, 74, 86]),
                         [(70000000, 72999999), (74000000, 74999999), (85000000, 86999999)])
        self.assertEqual(len(sub_basin_ranges(range(10, 31))), 3, "at most 10 sub-basins per request")

    def test_stations_in_the_area_and_period(self):
        inventory = self.inventory
        self.assertEqual(sorted(inventory.sites), ["ANA-2953001", "ANA-2953005", "ANA-85400000"])
        self.assertEqual({row: inventory.count(row) for row in ROWS_BY_KEY},
                         {"discharge": 1, "stage": 1, "precipitation": 2})
        self.assertIn({"codEstDE": 85000000, "codEstATE": 85999999, "tpEst": 1}, self.client.calls,
                      "the code range of the sub-basin of the nearby rain gauges")
        self.assertEqual(inventory.layer_sites("streamflow"), ["ANA-85400000"])
        self.assertEqual(inventory.describe("ANA-85400000"), "Discharge, Water level")
        site = inventory.sites["ANA-85400000"]
        self.assertEqual((site["lon"], site["lat"], site["drainage_km2"], site["org"]), (-53.7, -29.65, 1000.0, "SGB-CPRM"))

    def test_failed_code_ranges_are_split(self):
        client = BusyAna()
        inventory = check_availability(SETTINGS, AOI, client=client)
        self.assertIn("ANA-85400000", inventory.sites)
        self.assertEqual(inventory.warnings, [])
        self.assertIn(("failed", 85000000, 85999999), client.calls)
        self.assertIn({"codEstDE": 85400000, "codEstATE": 85499999, "tpEst": 1}, client.calls)

    def test_refused_queries_are_asked_again(self):
        # Each round asks the latitude range, then its pieces: 4 refusals take two rounds.
        client = RefusingAna(refusals=4)
        inventory = check_availability(SETTINGS, AOI, client=client)
        self.assertEqual(sorted(inventory.sites), ["ANA-2953001", "ANA-2953005", "ANA-85400000"])
        self.assertEqual((inventory.failed_queries, inventory.warnings), (0, []))
        self.assertEqual([c for c in client.calls if isinstance(c, tuple) and c[0] == "wait"], [("wait", 15), ("wait", 30)])

    def test_check_reports_queries_still_refused(self):
        inventory = check_availability(SETTINGS, AOI, client=RefusingAna(refusals=10))
        self.assertEqual(inventory.failed_queries, 1)
        self.assertNotIn("ANA-2953001", inventory.sites, "its cell was never answered")
        self.assertIn("HTTP 429", inventory.warnings[0])

    def test_area_outside_brazil(self):
        inventory = check_availability(SETTINGS, shapely.box(-88.3, 30.4, -87.8, 31.0), client=FakeAna())
        self.assertEqual(inventory.sites, {})
        self.assertIn("covers Brazil", inventory.warnings[0])


class SeriesTests(unittest.TestCase):
    def test_one_value_per_day(self):
        discharge = daily_values(SERIES[("85400000", 3)], ROWS_BY_KEY["discharge"])
        self.assertEqual(discharge.loc["2020-01-01"], 12.5, "consisted over raw")
        self.assertEqual(discharge.loc["2020-01-03"], 14.0, "raw where nothing is consisted")
        self.assertNotIn(pd.Timestamp("2020-03-01"), discharge.index, "2020-02-30 is not March 1")
        stage = daily_values(SERIES[("85400000", 1)], ROWS_BY_KEY["stage"])
        self.assertEqual(stage.loc["2020-01-01"], 148.0, "daily mean over a single reading")
        rain = daily_values(SERIES[("2953001", 2)], ROWS_BY_KEY["precipitation"])
        self.assertEqual(rain.tolist(), [1.5, 0.0], "decimal comma")

    def test_hidrowebservice_field_names(self):
        record = {"Data_Hora_Dado": "2020-01-01 00:00:00.0", "Nivel_Consistencia": "2", "Vazao_01": "7.5",
                  "Vazao_01_Status": "1"}
        values = daily_values([record], ROWS_BY_KEY["discharge"])
        self.assertEqual(values.to_dict(), {pd.Timestamp("2020-01-01"): 7.5})


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name) / "run 1"
        self.output.mkdir()
        self.client = FakeAna()
        self.inventory = check_availability(SETTINGS, AOI, client=self.client)

    def tearDown(self):
        self.temp.cleanup()

    def run_export(self, **overrides):
        settings = {"output_dir": str(self.output), "target_crs": "EPSG:4326", "site_ids": None,
                    "selection": [("discharge", "daily"), ("stage", "daily"), ("precipitation", "daily")],
                    "write_shapefile": True}
        settings.update(overrides)
        return extract_hidroweb(settings, self.inventory, client=self.client)

    def test_series_index_layers_and_readme(self):
        result = self.run_export()
        base = self.output / "hidroweb_data"
        discharge = pd.read_csv(base / "daily" / "ana_85400000_discharge.csv", dtype=str)
        self.assertEqual(discharge.columns.tolist(), ["date", "discharge_m3s"], "only the date and the value")
        self.assertEqual(len(discharge), 91, "every day of the period")
        values = discharge.set_index("date")["discharge_m3s"]
        self.assertEqual((values["2020-01-01"], values["2020-01-03"], values["2020-02-29"]), ("12.5", "14", "21"))
        self.assertEqual(values["2020-03-15"], "-99")
        rain = pd.read_csv(base / "daily" / "ana_2953001_precipitation.csv")
        self.assertEqual(rain.columns.tolist(), ["date", "precipitation_mm"])
        index = pd.read_csv(base / "hidroweb_index.csv").set_index("site_id")
        self.assertEqual(index.loc["ANA-85400000", ["latitude", "longitude"]].tolist(), [-29.65, -53.7])
        self.assertEqual(index.loc["ANA-85400000", ["discharge_days", "stage_days", "precipitation_days"]].tolist(),
                         [5, 1, 0])
        self.assertEqual(index.loc["ANA-2953001", "station_type"], "Rain gauge")
        self.assertEqual((result["station_count"], result["gauge_count"]), (1, 1))
        self.assertEqual(result["layers"], ["streamflow_stations", "rain_gauges"])
        for shapefile in result["shapefiles"]:
            self.assertTrue(shapefile.exists())
        from osgeo import ogr

        datasource = ogr.Open(str(result["gpkg"]))
        layer = datasource.GetLayerByName("streamflow_stations")
        feature = layer.GetNextFeature()
        self.assertEqual((feature.GetField("site_id"), feature.GetField("datasets")),
                         ("ANA-85400000", "Discharge, Water level"))
        datasource = None
        readme = (base / "README.txt").read_text(encoding="utf-8")
        self.assertIn("ServiceANA (public)", readme)
        self.assertIn("Discharge (vazão)", readme)
        self.assertIn("Totals: 1 rain gauge(s); 1 streamflow station(s).", readme)
        self.assertIn("ANA-2953005  Precipitation (chuva)", readme, "listed as returning no values")
        self.assertNotIn("ANA-2953005", index.index)
        self.assertEqual(result["excluded"], 1, "one selected station had no data")
        # After the run the tab keeps only what was downloaded.
        self.inventory.keep(set(result["downloaded"]))
        self.assertEqual(sorted(self.inventory.sites), ["ANA-2953001", "ANA-85400000"])
        self.assertEqual(self.inventory.count("precipitation"), 1)
        available = pd.read_csv(base / "available_data.csv")
        self.assertTrue(available["selected"].all())

    def test_refused_series_are_asked_again(self):
        self.client = RefusingAna(refusals=1)
        result = self.run_export()
        self.assertTrue((self.output / "hidroweb_data" / "daily" / "ana_85400000_discharge.csv").exists())
        self.assertEqual(result["warnings"], [])

    def test_only_selected_sites(self):
        result = self.run_export(site_ids=["ANA-2953001"], write_shapefile=False)
        self.assertEqual((result["station_count"], result["gauge_count"]), (0, 1))
        self.assertFalse((self.output / "hidroweb_data" / "daily" / "ana_85400000_discharge.csv").exists())

    def test_nothing_selected_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "Nothing is selected"):
            self.run_export(selection=[])

    def test_request_estimate(self):
        settings = {"selection": [("discharge", "daily"), ("precipitation", "daily")], "site_ids": None}
        self.assertEqual(estimate_requests(self.inventory, settings), 3, "one request per series")
        settings.update(user="12345678900", password="secret")
        self.assertEqual(estimate_requests(self.inventory, settings), 4, "sign-in plus one 366-day chunk each")


class FakeResponse:
    def __init__(self, status, payload=None, content=b""):
        self.status_code = status
        self._payload = payload
        self.content = content
        self.text = content.decode("utf-8")
        self.headers = {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        return self.responses.pop(0)


NO_DATA = (b'<?xml version="1.0" encoding="utf-8"?><DataTable xmlns="http://MRCS/"><diffgr:diffgram '
           b'xmlns:diffgr="urn:schemas-microsoft-com:xml-diffgram-v1"><DocumentElement xmlns=""><ErrorTable>'
           b'<Error>Sem dados</Error></ErrorTable></DocumentElement></diffgr:diffgram></DataTable>')
ONE_MONTH = (b'<?xml version="1.0" encoding="utf-8"?><DataTable xmlns="http://MRCS/"><diffgr:diffgram '
             b'xmlns:diffgr="urn:schemas-microsoft-com:xml-diffgram-v1"><DocumentElement xmlns="">'
             b'<SerieHistorica><DataHora>2020-01-01 00:00:00</DataHora><NivelConsistencia>2</NivelConsistencia>'
             b'<Vazao01>5.5</Vazao01></SerieHistorica></DocumentElement></diffgr:diffgram></DataTable>')


class PacerTests(unittest.TestCase):
    def test_burst_then_one_per_second(self):
        pacer = hidroweb_client.RatePacer(1.0, 8)
        waits = [pacer.delay(now=100.0) for _ in range(11)]
        self.assertEqual(waits[:8], [0.0] * 8)
        self.assertEqual(waits[8:], [1.0, 2.0, 3.0])
        self.assertEqual(pacer.delay(now=120.0), 0.0, "the burst refills while idle")


class ClientTests(unittest.TestCase):
    def client(self, responses, user="", password=""):
        client = hidroweb_client.HidrowebClient(user, password, sleep=lambda seconds: None)
        session = FakeSession(responses)
        client._session = lambda: session
        return client, session

    def test_public_service(self):
        client, session = self.client([FakeResponse(200, content=ONE_MONTH), FakeResponse(200, content=NO_DATA)])
        records = client.series("85400000", 3, "1990-01-01", "2020-12-31")
        self.assertEqual([r["Vazao01"] for r in records], ["5.5"], "'Sem dados' replies add nothing")
        url, params, _ = session.calls[0]
        self.assertEqual(url, f"{hidroweb_client.LEGACY_URL}/HidroSerieHistorica")
        self.assertEqual((params["dataInicio"], params["tipoDados"]), ("01/01/1990", 3))
        self.assertEqual(len(session.calls), 2, "20-year chunks")

    def test_hidrowebservice_login_and_chunks(self):
        token = FakeResponse(200, payload={"items": {"tokenautenticacao": "TOKEN"}})
        day = {"Data_Hora_Dado": "2020-01-01 00:00:00.0", "Vazao_01": "1.0"}
        client, session = self.client([token, FakeResponse(200, payload={"items": [day]}), FakeResponse(404)],
                                      user="12345678900", password="secret")
        records = client.series("85400000", 3, "2020-01-01", "2021-03-01")
        self.assertEqual(records, [day])
        self.assertEqual(session.calls[0][2], {"Identificador": "12345678900", "Senha": "secret"})
        self.assertEqual(session.calls[1][2], {"Authorization": "Bearer TOKEN"})
        self.assertEqual(session.calls[1][1]["Data Final (yyyy-MM-dd)"], "2020-12-31", "366 days per request")
        self.assertEqual(len(session.calls), 3, "one sign-in for both requests")

    def test_wrong_login(self):
        client, _ = self.client([FakeResponse(200, payload={"items": {"sucesso": "false"}})],
                                user="12345678900", password="wrong")
        with self.assertRaisesRegex(RuntimeError, "sign-in failed"):
            client.token()


if __name__ == "__main__":
    unittest.main()
