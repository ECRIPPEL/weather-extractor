"""'Check available data': find USGS, partner, and NOAA data in the AOI from metadata only."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import math

import pandas as pd
import shapely

from .usgs_catalog import (
    CHANNEL,
    CONTINUOUS,
    DAILY,
    DAILY_STATISTICS,
    FIELD,
    FIELD_CODES,
    GHCN_PRECIPITATION,
    LAYERS,
    PEAKS,
    RAIN_GAUGE_ROWS,
    ROWS,
    ROWS_BY_CODE,
    ROWS_BY_KEY,
    SAMPLES,
    WQP_USGS_FROZEN,
    layer_of,
)
from .usgs_client import UsgsClient


KM_PER_DEGREE = 111.195
SQ_MI_TO_KM2 = 2.589988110336
USGS, OTHER, NOAA = "usgs", "other", "noaa"
# Value of the "source" field of the station layers.
SITE_SOURCES = {USGS: "USGS", NOAA: "NOAA"}
USGS_ORG_NAME = "U.S. Geological Survey"
NOAA_ORG_NAME = "NOAA National Centers for Environmental Information (GHCN)"
GHCN_DAILY_DATASET = "daily-summaries"
GHCNH_DATASET = "global-historical-climatology-network-hourly"
# GHCN station id prefix -> (network, full name). GHCN-Daily and GHCNh share the ids.
GHCN_NETWORKS = {
    "US1": ("CoCoRaHS", "Community Collaborative Rain, Hail and Snow Network, volunteers"),
    "USC": ("COOP", "NWS Cooperative Observer Program"),
    "USW": ("First-order", "NWS/FAA first-order stations: ASOS, airports, USCRN"),
    "USR": ("RAWS", "Remote Automated Weather Stations"),
    "USS": ("SNOTEL", "NRCS SNOTEL and SCAN stations"),
}


def ghcn_network(station_id):
    return GHCN_NETWORKS.get(station_id[:3], ("Other", "Other GHCN stations"))


def ghcn_site_id(station_id):
    return f"GHCN-{station_id}"


class Inventory:
    """Sites and datasets available for one AOI, period, and search margin."""

    def __init__(self, start, end, margin_km, bbox, sites, entries, warnings):
        self.start = start
        self.end = end
        self.margin_km = margin_km
        self.bbox = bbox
        self.sites = sites
        self.entries = entries
        self.warnings = warnings
        self.checked_at = datetime.now().strftime("%H:%M")

    def _usable(self, entry, sources):
        return entry["column"] != SAMPLES or entry["source"] in sources

    def count(self, row_key, column, sources=(USGS, OTHER)):
        return len(
            {
                entry["site_id"]
                for entry in self.entries
                if entry["row"] == row_key and entry["column"] == column and self._usable(entry, sources)
            }
        )

    def selected_entries(self, selection, sources=(USGS, OTHER), site_ids=None):
        return [
            entry
            for entry in self.entries
            if (entry["row"], entry["column"]) in selection
            and self._usable(entry, sources)
            and (site_ids is None or entry["site_id"] in site_ids)
        ]

    def visible_sites(self, selection, sources=(USGS, OTHER)):
        """Site ids of each map layer that hold at least one selected dataset."""
        visible = {layer: set() for layer in LAYERS}
        for entry in self.selected_entries(selection, sources):
            visible[layer_of(entry["row"])].add(entry["site_id"])
        return visible

    def rain_gauges(self, selection, sources=(USGS, OTHER), site_ids=None):
        """Stations with a selected precipitation dataset: site_id, name, lon, lat."""
        chosen = {
            entry["site_id"]
            for entry in self.selected_entries(selection, sources, site_ids)
            if entry["row"] in RAIN_GAUGE_ROWS
        }
        return [
            {"site_id": site_id, "name": self.sites[site_id].get("name", ""),
             "lon": self.sites[site_id]["lon"], "lat": self.sites[site_id]["lat"]}
            for site_id in sorted(chosen)
        ]

    def layer_sites(self, layer):
        return sorted({e["site_id"] for e in self.entries if layer_of(e["row"]) == layer})

    def describe(self, site_id, layer, entries=None):
        """'Streamflow: daily, field; Gage height: daily' for one site."""
        by_row = {}
        for entry in entries if entries is not None else self.entries:
            row = ROWS_BY_KEY[entry["row"]]
            if entry["site_id"] == site_id and layer_of(row.key) == layer:
                by_row.setdefault(row.label.split(" (")[0], set()).add(entry["column"])
        return "; ".join(f"{name}: {', '.join(sorted(columns))}" for name, columns in sorted(by_row.items()))

    def period(self, site_id, layer, entries=None):
        dates = [
            (entry["begin"], entry["end"])
            for entry in (entries if entries is not None else self.entries)
            if entry["site_id"] == site_id and layer_of(entry["row"]) == layer
        ]
        begins = [b for b, _ in dates if b]
        ends = [e for _, e in dates if e]
        return (min(begins) if begins else ""), (max(ends) if ends else "")

    def to_frame(self, selection=None, sources=(USGS, OTHER)):
        rows = []
        for entry in self.entries:
            site = self.sites.get(entry["site_id"], {})
            row = ROWS_BY_KEY[entry["row"]]
            rows.append(
                {
                    "site_id": entry["site_id"],
                    "site_name": site.get("name", ""),
                    "layer": layer_of(row.key),
                    "variable": row.key,
                    "format": entry["column"],
                    "parameter_code": entry.get("parameter_code", ""),
                    "statistic": entry.get("statistic", ""),
                    "first_date": entry["begin"],
                    "last_date": entry["end"],
                    "source": entry["source"],
                    "selected": selection is not None
                    and (entry["row"], entry["column"]) in selection
                    and self._usable(entry, sources),
                }
            )
        return pd.DataFrame(rows)


def search_area(geometry, margin_km):
    if not margin_km:
        return geometry
    # Buffer in an equirectangular frame so the margin is about equal in every direction.
    scale = max(math.cos(math.radians(geometry.centroid.y)), 0.01)
    stretched = shapely.affinity.scale(geometry, xfact=scale, yfact=1.0, origin=(0, 0))
    buffered = stretched.buffer(float(margin_km) / KM_PER_DEGREE)
    return shapely.affinity.scale(buffered, xfact=1.0 / scale, yfact=1.0, origin=(0, 0))


def _overlaps(begin, end, start, finish):
    return not ((begin and begin > finish) or (end and end < start))


def _date(value):
    return (value or "")[:10]


def wqp_date(iso_date):
    """Water Quality Portal date: 2020-05-01 -> 05-01-2020."""
    year, month, day = iso_date.split("-")
    return f"{month}-{day}-{year}"


def _km2(square_miles):
    try:
        return round(float(square_miles) * SQ_MI_TO_KM2, 2)
    except (TypeError, ValueError):
        return None


def _series_entries(client, bbox, area, start, end):
    """Daily, continuous, and peak series from time-series metadata."""
    best = {}
    for feature in client.features("time-series-metadata", {"bbox": bbox}):
        properties = feature.get("properties", {})
        row = ROWS_BY_CODE.get(properties.get("parameter_code"))
        coordinates = (feature.get("geometry") or {}).get("coordinates")
        if row is None or not coordinates:
            continue
        lon, lat = float(coordinates[0]), float(coordinates[1])
        if not shapely.intersects_xy(area, lon, lat):
            continue
        begin, finish = _date(properties.get("begin")), _date(properties.get("end"))
        if not _overlaps(begin, finish, start, end):
            continue
        period = properties.get("computation_period_identifier")
        statistic = properties.get("statistic_id") or ""
        if period == "Daily" and statistic in DAILY_STATISTICS:
            column, rank = DAILY, DAILY_STATISTICS.index(statistic)
        elif period == "Points":
            column, rank = CONTINUOUS, 0
        elif period == "Water Year" and PEAKS in row.columns:
            column, rank = PEAKS, 0
        else:
            continue
        if column not in row.columns:
            continue
        rank = rank * 2 + (0 if properties.get("primary") == "Primary" else 1)
        key = (properties["monitoring_location_id"], row.key, column)
        entry = {
            "site_id": properties["monitoring_location_id"],
            "row": row.key,
            "column": column,
            "parameter_code": row.code,
            "series_id": properties.get("id") or feature.get("id"),
            "statistic": statistic,
            "begin": begin,
            "end": finish,
            "source": USGS,
            "lon": lon,
            "lat": lat,
            "_rank": rank,
        }
        if key not in best or rank < best[key]["_rank"]:
            best[key] = entry
    return list(best.values())


def _field_entries(client, bbox, area, start, end):
    entries = {}
    for code in FIELD_CODES:
        row = ROWS_BY_CODE[code]
        features = client.features(
            "field-measurements-metadata",
            {"bbox": bbox, "parameter_code": code, "properties": "monitoring_location_id,begin,end"},
        )
        for feature in features:
            properties = feature.get("properties", {})
            coordinates = (feature.get("geometry") or {}).get("coordinates")
            if not coordinates:
                continue
            lon, lat = float(coordinates[0]), float(coordinates[1])
            begin, finish = _date(properties.get("begin")), _date(properties.get("end"))
            if not shapely.intersects_xy(area, lon, lat) or not _overlaps(begin, finish, start, end):
                continue
            key = (properties["monitoring_location_id"], row.key)
            entry = entries.setdefault(
                key,
                {
                    "site_id": properties["monitoring_location_id"],
                    "row": row.key,
                    "column": FIELD,
                    "parameter_code": code,
                    "begin": begin,
                    "end": finish,
                    "source": USGS,
                    "lon": lon,
                    "lat": lat,
                },
            )
            entry["begin"] = min(filter(None, [entry["begin"], begin]), default="")
            entry["end"] = max(filter(None, [entry["end"], finish]), default="")
    return list(entries.values())


def _channel_entries(client, bbox, area, start, end):
    """Sites with channel measurements (width, area, velocity) in the period."""
    entries = {}
    features = client.features(
        "channel-measurements",
        {"bbox": bbox, "time": f"{start}/{end}", "properties": "monitoring_location_id,time"},
    )
    for feature in features:
        properties = feature.get("properties", {})
        coordinates = (feature.get("geometry") or {}).get("coordinates")
        if not coordinates:
            continue
        lon, lat = float(coordinates[0]), float(coordinates[1])
        if not shapely.intersects_xy(area, lon, lat):
            continue
        day = _date(properties.get("time"))
        entry = entries.setdefault(
            properties["monitoring_location_id"],
            {
                "site_id": properties["monitoring_location_id"],
                "row": CHANNEL,
                "column": FIELD,
                "parameter_code": "",
                "begin": day,
                "end": day,
                "source": USGS,
                "lon": lon,
                "lat": lat,
            },
        )
        entry["begin"] = min(entry["begin"], day)
        entry["end"] = max(entry["end"], day)
    return list(entries.values())


def _sample_filters(row):
    return (
        ("characteristicType", row.sample_groups)
        if row.sample_groups
        else ("characteristicName", row.sample_characteristics)
    )


def _wqp_sites(client, row, bbox, area, start, end):
    name, values = _sample_filters(row)
    frame = client.wqp(
        "Station",
        {
            "bBox": bbox,
            name: ";".join(values),
            "startDateLo": wqp_date(start),
            "startDateHi": wqp_date(end),
        },
    )
    sites = []
    for record in frame.to_dict("records"):
        try:
            lon, lat = float(record["LongitudeMeasure"]), float(record["LatitudeMeasure"])
        except (KeyError, ValueError):
            continue
        if not shapely.intersects_xy(area, lon, lat):
            continue
        site_id = record["MonitoringLocationIdentifier"]
        sites.append(
            {
                "site_id": site_id,
                "name": record.get("MonitoringLocationName", ""),
                "type": record.get("MonitoringLocationTypeName", ""),
                "org": record.get("OrganizationIdentifier", ""),
                "org_name": record.get("OrganizationFormalName", ""),
                "lon": lon,
                "lat": lat,
                "drainage_km2": None,
                "source": USGS if site_id.startswith("USGS-") else OTHER,
            }
        )
    return sites


def _ncei_params(dataset, area, start, end):
    west, south, east, north = area.bounds
    return {
        "dataset": dataset,
        "bbox": f"{north:.6f},{west:.6f},{south:.6f},{east:.6f}",  # NCEI order: N,W,S,E
        "startDate": f"{start}T00:00:00",
        "endDate": f"{end}T23:59:59",
    }


def _ghcn_results(results, area, data_type, usable=lambda data: True):
    """(station, coordinates, first day, last day) of the search results inside the area."""
    for result in results:
        station = (result.get("stations") or [{}])[0]
        coordinates = (result.get("location") or {}).get("coordinates") or result.get("centroid")
        data = next((t for t in station.get("dataTypes", []) if t.get("id") == data_type), None)
        if not station.get("id") or not coordinates or data is None or not usable(data):
            continue
        if not shapely.intersects_xy(area, float(coordinates[0]), float(coordinates[1])):
            continue
        dates = data.get("dateRange") or {}
        yield (station, coordinates, _date(dates.get("start") or data.get("startDate")),
               _date(dates.get("end") or data.get("endDate")))


def _ghcn_entry(station, coordinates, column, data_type, begin, finish):
    network, network_name = ghcn_network(station["id"])
    return {
        "site_id": ghcn_site_id(station["id"]),
        "row": GHCN_PRECIPITATION,
        "column": column,
        "parameter_code": data_type,
        "station": station["id"],
        "begin": begin,
        "end": finish,
        "source": NOAA,
        "lon": float(coordinates[0]),
        "lat": float(coordinates[1]),
        "name": station.get("name", ""),
        "network": network,
        "network_name": network_name,
    }


def _ghcn_entries(client, area, start, end):
    """NOAA GHCN-Daily stations with precipitation (PRCP) in the area and period."""
    results = client.ncei_search(dict(_ncei_params(GHCN_DAILY_DATASET, area, start, end), dataTypes="PRCP"))
    return [
        _ghcn_entry(station, coordinates, DAILY, "PRCP", begin, finish)
        for station, coordinates, begin, finish in _ghcn_results(results, area, "PRCP")
        if _overlaps(begin, finish, start, end)
    ]


def _ghcnh_entries(client, area, start, end):
    """NOAA GHCNh stations with hourly precipitation in the area and period.

    The search returns one result per station and year. Every result lists all GHCNh
    variables, so only years with precipitation coverage count (tide gauges have none).
    """
    results = client.ncei_search(_ncei_params(GHCNH_DATASET, area, start, end))
    found = {}
    reported = lambda data: float(data.get("coverage") or 0) > 0
    for station, coordinates, begin, finish in _ghcn_results(results, area, "precipitation", reported):
        if not _overlaps(begin, finish, start, end):
            continue
        entry = found.get(station["id"])
        if entry is None:
            found[station["id"]] = _ghcn_entry(station, coordinates, CONTINUOUS, "precipitation", begin, finish)
        else:
            entry["begin"] = min(filter(None, [entry["begin"], begin]), default="")
            entry["end"] = max(filter(None, [entry["end"], finish]), default="")
    return list(found.values())


def _site_details(client, bbox):
    details = {}
    features = client.features(
        "monitoring-locations",
        {
            "bbox": bbox,
            "properties": "monitoring_location_name,site_type,agency_code,drainage_area",
        },
    )
    for feature in features:
        properties = feature.get("properties", {})
        details[feature.get("id") or properties.get("id")] = properties
    return details


def check_availability(settings, geometry, progress=None, is_canceled=None, client=None):
    start, end = settings["start_date"], settings["end_date"]
    if start > end:
        raise ValueError("The start date must be on or before the end date.")
    margin_km = float(settings.get("margin_km", 0.0) or 0.0)
    client = client or UsgsClient(settings.get("api_key", ""), is_canceled)
    area = search_area(geometry, margin_km)
    bbox = ",".join(f"{value:.6f}" for value in area.bounds)
    warnings = []

    def report(percentage, message):
        if progress:
            progress(percentage, message)

    sample_rows = [row for row in ROWS if SAMPLES in row.columns]
    jobs = {}
    # Sampling sites come from the Water Quality Portal, which also lists USGS sites
    # sampled before 2024-03-11. Searching USGS Samples by area takes minutes or fails,
    # so USGS sites first sampled after that date are not discovered here.
    if end >= WQP_USGS_FROZEN:
        warnings.append(
            f"USGS sites sampled for the first time after {WQP_USGS_FROZEN} are not listed "
            "(the USGS Samples area search is too slow); their recent samples are still "
            "downloaded for the listed USGS sites."
        )
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs[pool.submit(_series_entries, client, bbox, area, start, end)] = "series"
        jobs[pool.submit(_field_entries, client, bbox, area, start, end)] = "field"
        jobs[pool.submit(_channel_entries, client, bbox, area, start, end)] = "channel"
        jobs[pool.submit(_site_details, client, bbox)] = "details"
        jobs[pool.submit(_ghcn_entries, client, area, start, end)] = "ghcn"
        jobs[pool.submit(_ghcnh_entries, client, area, start, end)] = "ghcnh"
        for row in sample_rows:
            jobs[pool.submit(_wqp_sites, client, row, bbox, area, start, end)] = ("wqp", row)
        results = {}
        done = 0
        for future in as_completed(jobs):
            label = jobs[future]
            done += 1
            try:
                results[label] = future.result()
            except InterruptedError:
                raise
            except Exception as exc:
                if isinstance(label, tuple):
                    warnings.append(f"Water Quality Portal check failed for {label[1].label}: {exc}")
                elif label == "details":
                    warnings.append(f"Station names could not be read: {exc}")
                elif label == "ghcn":
                    warnings.append(f"NOAA GHCN-Daily check failed: {exc}")
                elif label == "ghcnh":
                    warnings.append(f"NOAA GHCNh (hourly) check failed: {exc}")
                else:
                    raise
            report(100.0 * done / len(jobs), f"Checking available data: {done} of {len(jobs)} queries")

    details = results.get("details", {})
    sites = {}
    entries = []
    for entry in results.get("series", []) + results.get("field", []) + results.get("channel", []):
        info = details.get(entry["site_id"], {})
        org = info.get("agency_code") or "USGS"
        sites.setdefault(
            entry["site_id"],
            {
                "name": info.get("monitoring_location_name", ""),
                "type": info.get("site_type", ""),
                "org": org,
                "org_name": USGS_ORG_NAME if org == "USGS" else "",
                "lon": entry["lon"],
                "lat": entry["lat"],
                "drainage_km2": _km2(info.get("drainage_area")),
                "source": USGS,
            },
        )
        entries.append({k: v for k, v in entry.items() if k not in ("lon", "lat", "_rank")})

    site_fields = ("lon", "lat", "name", "network", "network_name")
    for entry in results.get("ghcn", []) + results.get("ghcnh", []):
        sites.setdefault(
            entry["site_id"],
            {
                "name": entry["name"],
                "type": entry["network"],
                "org": "NOAA",
                "org_name": NOAA_ORG_NAME,
                "network": entry["network"],
                "network_name": entry["network_name"],
                "lon": entry["lon"],
                "lat": entry["lat"],
                "drainage_km2": None,
                "source": NOAA,
            },
        )
        entries.append({k: v for k, v in entry.items() if k not in site_fields})

    sample_keys = set()
    for label, found in results.items():
        if not (isinstance(label, tuple) and label[0] == "wqp"):
            continue
        row = label[1]
        for site in found:
            sites.setdefault(site["site_id"], {k: v for k, v in site.items() if k != "site_id"})
            key = (site["site_id"], row.key)
            if key in sample_keys:
                continue
            sample_keys.add(key)
            entries.append(
                {
                    "site_id": site["site_id"],
                    "row": row.key,
                    "column": SAMPLES,
                    "parameter_code": "",
                    "begin": "",
                    "end": "",
                    "source": site["source"],
                }
            )
    return Inventory(start, end, margin_km, bbox, sites, entries, warnings)
