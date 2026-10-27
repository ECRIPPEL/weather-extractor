"""Download the selected observed datasets and write CSV tables and station layers."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
import threading

import numpy as np
import pandas as pd

from .aorc_processing import TIMEZONE, _spatial_reference, run_label, write_warnings
from .usgs_catalog import (
    CHANNEL,
    CONTINUOUS,
    DAILY,
    FIELD,
    LAYER_CHANNEL,
    MISSING_VALUE,
    PEAKS,
    QUALITY,
    QUANTITY,
    ROWS_BY_KEY,
    SAMPLES,
    STATISTIC_NAMES,
    layer_of,
    site_stem,
)
from .usgs_client import NCEI_DATA_URL, SAMPLES_URL, WATERDATA_URL, WQP_URL, UsgsClient
from .usgs_inventory import (
    GHCN_DAILY_DATASET, GHCNH_DATASET, NOAA, OTHER, SITE_SOURCES, USGS, wqp_date,
)

# Map layer -> GeoPackage layer name.
LAYER_NAMES = {
    QUANTITY: "quantity_stations",
    LAYER_CHANNEL: "channel_geometry_stations",
    QUALITY: "quality_points",
}


DAILY_CHUNK_YEARS = 100  # stays below the 50,000-row API limit
# One year per request: each request costs ~2.5 s of server time, so fewer, larger
# requests are faster and use less of the hourly quota. 15-minute data (~35,000
# values a year) fits one page; denser series are paged automatically.
CONTINUOUS_CHUNK_DAYS = 365
USGS_SAMPLE_BATCH = 10
WQP_SAMPLE_BATCH = 50
GHCN_STATION_BATCH = 20
GHCN_CHUNK_YEARS = 10
# GHCNh rows come every 5-60 min (about 100,000 a year at 5-min stations), so requests
# stay small: a few stations, one year.
GHCNH_STATION_BATCH = 5
GHCNH_CHUNK_YEARS = 1
GHCNH_FIELDS = [
    "precipitation_Measurement_Code", "precipitation_Quality_Code",
    "precipitation_Report_Type", "precipitation_Source_Code",
]
# Daily CSVs: up to 10 significant digits, so missing days read -99 (not -99.0).
FLOAT_FORMAT = "%.10g"
SERIES_WORKERS = 4
SAMPLE_WORKERS = 2

SAMPLE_COLUMNS = [
    "source", "organization", "site_id", "site_name", "date", "time", "time_zone",
    "depth", "depth_unit", "variable", "group", "characteristic", "fraction", "value", "unit",
    "detection_condition", "detection_limit", "detection_limit_unit", "qualifier",
    "status", "usgs_pcode", "method",
]
# Kept with the samples for the README summary; not written to samples.csv.
AUDIT_COLUMNS = ["organization_name", "project"]
WATER_DATA = "USGS Water Data API"
USGS_SAMPLES = "USGS Samples"
WQP = "Water Quality Portal"
GHCN = "NOAA GHCN-Daily"
GHCNH = "NOAA GHCNh"
SERVICES = [
    (WATER_DATA, "USGS Water Data API", WATERDATA_URL.rsplit("/collections", 1)[0]),
    (USGS_SAMPLES, "USGS Samples", SAMPLES_URL),
    (WQP, "Water Quality Portal (EPA)", f"{WQP_URL}, legacy WQX 2.2 profiles"),
    (GHCN, "NOAA GHCN-Daily (NCEI)", f"{NCEI_DATA_URL}, dataset {GHCN_DAILY_DATASET}"),
    (GHCNH, "NOAA GHCNh, hourly (NCEI)", f"{NCEI_DATA_URL}, dataset {GHCNH_DATASET}"),
]
HOURLY = "hourly"
DATASET_ORDER = {DAILY: 0, CONTINUOUS: 1, HOURLY: 1, FIELD: 2, PEAKS: 3, SAMPLES: 4}
MAX_EMPTY_LISTED = 30
WQP_MAP = {
    "organization": "OrganizationIdentifier",
    "organization_name": "OrganizationFormalName",
    "project": "ProjectIdentifier",
    "site_id": "MonitoringLocationIdentifier",
    "date": "ActivityStartDate",
    "time": "ActivityStartTime/Time",
    "time_zone": "ActivityStartTime/TimeZoneCode",
    "depth": "ActivityDepthHeightMeasure/MeasureValue",
    "depth_unit": "ActivityDepthHeightMeasure/MeasureUnitCode",
    "characteristic": "CharacteristicName",
    "fraction": "ResultSampleFractionText",
    "value": "ResultMeasureValue",
    "unit": "ResultMeasure/MeasureUnitCode",
    "detection_condition": "ResultDetectionConditionText",
    "detection_limit": "DetectionQuantitationLimitMeasure/MeasureValue",
    "detection_limit_unit": "DetectionQuantitationLimitMeasure/MeasureUnitCode",
    "qualifier": "MeasureQualifierCode",
    "status": "ResultStatusIdentifier",
    "usgs_pcode": "USGSPCode",
    "method": "ResultAnalyticalMethod/MethodName",
}
SAMPLES_MAP = {
    "organization": "Org_Identifier",
    "organization_name": "Org_FormalName",
    "project": "Project_Identifier",
    "site_id": "Location_Identifier",
    "site_name": "Location_Name",
    "date": "Activity_StartDate",
    "time": "Activity_StartTime",
    "time_zone": "Activity_StartTimeZone",
    "depth": "Activity_DepthHeightMeasure",
    "depth_unit": "Activity_DepthHeightMeasureUnit",
    "group": "Result_CharacteristicGroup",
    "characteristic": "Result_Characteristic",
    "fraction": "Result_SampleFraction",
    "value": "Result_Measure",
    "unit": "Result_MeasureUnit",
    "detection_condition": "Result_ResultDetectionCondition",
    "detection_limit": "DetectionLimit_MeasureA",
    "detection_limit_unit": "DetectionLimit_MeasureUnitA",
    "qualifier": "Result_MeasureQualifierCode",
    "status": "Result_MeasureStatusIdentifier",
    "usgs_pcode": "USGSpcode",
    "method": "ResultAnalyticalMethod_Name",
}


def _numeric(series):
    return pd.to_numeric(series, errors="coerce")


def _column_base(row, entry):
    statistic = STATISTIC_NAMES.get(entry.get("statistic", ""), "")
    return row.key if statistic in ("", "mean", "sum") else f"{row.key}_{statistic}"


# ---------------------------------------------------------------------------
# Series (daily and continuous)
# ---------------------------------------------------------------------------


def download_daily(client, entry, start, end):
    frames = []
    for year in range(int(start[:4]), int(end[:4]) + 1, DAILY_CHUNK_YEARS):
        chunk_start = max(start, f"{year}-01-01")
        chunk_end = min(end, f"{year + DAILY_CHUNK_YEARS - 1}-12-31")
        frame = client.csv(
            "daily",
            {
                "time_series_id": entry["series_id"],
                "time": f"{chunk_start}/{chunk_end}",
                "properties": "time,value,approval_status,qualifier",
            },
        )
        if not frame.empty:
            frames.append(frame)
    return _series_frame(frames, date_only=True)


def download_continuous(client, entry, start, end):
    frames = []
    chunk_start = pd.Timestamp(start)
    last = pd.Timestamp(end) + pd.Timedelta(days=1)
    while chunk_start < last:
        chunk_end = min(chunk_start + pd.Timedelta(days=CONTINUOUS_CHUNK_DAYS), last)
        frame = client.csv(
            "continuous",
            {
                "time_series_id": entry["series_id"],
                "time": f"{chunk_start:%Y-%m-%dT%H:%M:%SZ}/{chunk_end:%Y-%m-%dT%H:%M:%SZ}",
                "properties": "time,value,unit_of_measure,approval_status,qualifier",
            },
            sortby="time",
        )
        if not frame.empty:
            frames.append(frame)
        chunk_start = chunk_end
    data = _series_frame(frames, date_only=False)
    # Windows include their end instant; drop midnight after the last requested day.
    return data[data.index < last.tz_localize("UTC")] if len(data) else data


def _series_frame(frames, date_only):
    if not frames:
        return pd.DataFrame(columns=["value", "approval_status", "qualifier"])
    data = pd.concat(frames, ignore_index=True)
    if date_only:
        data["key"] = pd.to_datetime(data["time"].str[:10])
    else:
        # Some sensor times carry fractional seconds (e.g. 00:18:04.702000+00:00).
        data["key"] = pd.to_datetime(data["time"], utc=True, format="ISO8601")
    data["value"] = _numeric(data["value"])
    for column in ("approval_status", "qualifier"):
        if column not in data:
            data[column] = ""
    return (
        data.drop_duplicates("key", keep="last")
        .set_index("key")
        .sort_index()[["value", "approval_status", "qualifier"]]
    )


def write_daily_tables(folder, results, start, end):
    """One wide CSV per site: date plus value, status, and qualifier per variable."""
    folder.mkdir(parents=True, exist_ok=True)
    index = pd.date_range(start, end, freq="D")
    by_site = {}
    for entry, data in results:
        by_site.setdefault(entry["site_id"], []).append((entry, data))
    counts = {}
    for site_id, items in by_site.items():
        table = pd.DataFrame({"date": index.strftime("%Y-%m-%d")})
        for entry, data in sorted(items, key=lambda item: item[0]["row"]):
            row = ROWS_BY_KEY[entry["row"]]
            base = _column_base(row, entry)
            values = data.reindex(index)
            raw = values["value"].to_numpy(dtype=float)
            known = np.isfinite(raw)
            table[f"{base}_{row.unit}"] = np.where(known, raw, MISSING_VALUE)
            if row.factor:
                table[f"{base}_{row.metric_unit}"] = np.where(known, np.round(raw * row.factor, 5), MISSING_VALUE)
            table[f"{base}_status"] = values["approval_status"].fillna("").to_numpy()
            table[f"{base}_qualifier"] = values["qualifier"].fillna("").to_numpy()
            counts[(site_id, entry["row"], entry["column"])] = int(known.sum())
        table["site_id"] = site_id
        table.to_csv(folder / f"{site_stem(site_id)}_daily.csv", index=False, float_format=FLOAT_FORMAT)
    return counts


def write_continuous_tables(folder, results):
    folder.mkdir(parents=True, exist_ok=True)
    counts = {}
    for entry, data in results:
        row = ROWS_BY_KEY[entry["row"]]
        table = pd.DataFrame(
            {"datetime_utc": data.index.strftime("%Y-%m-%dT%H:%M:%SZ"), "value": data["value"].to_numpy()}
        )
        table["unit"] = row.unit
        if row.factor:
            table["value_metric"] = np.round(table["value"] * row.factor, 5)
            table["unit_metric"] = row.metric_unit
        table["status"] = data["approval_status"].to_numpy()
        table["qualifier"] = data["qualifier"].to_numpy()
        table.insert(0, "site_id", entry["site_id"])
        table.to_csv(folder / f"{site_stem(entry['site_id'])}_{row.key}.csv", index=False)
        counts[(entry["site_id"], entry["row"], entry["column"])] = len(table)
    return counts


# ---------------------------------------------------------------------------
# Field measurements and annual peaks (one long table each)
# ---------------------------------------------------------------------------


def download_point_table(client, collection, entries, bbox, start, end):
    """Field measurements or peaks of the selected sites, as one long table."""
    frames = []
    by_code = {}
    for entry in entries:
        by_code.setdefault(entry["parameter_code"], set()).add(entry["site_id"])
    for code, site_ids in sorted(by_code.items()):
        row = next(r for r in map(ROWS_BY_KEY.get, {e["row"] for e in entries}) if r.code == code)
        properties = (
            "monitoring_location_id,time,value,unit_of_measure,approval_status,qualifier"
            if collection == "field-measurements"
            else "monitoring_location_id,water_year,time,value,unit_of_measure,qualifier"
        )
        params = {"bbox": bbox, "parameter_code": code, "properties": properties}
        if collection == "field-measurements":
            params["time"] = f"{start}/{end}"
        frame = client.csv(collection, params, sortby="monitoring_location_id,time")
        if frame.empty:
            continue
        frame = frame[frame["monitoring_location_id"].isin(site_ids)].copy()
        frame = frame[(frame["time"].str[:10] >= start) & (frame["time"].str[:10] <= end)]
        if frame.empty:
            continue
        table = pd.DataFrame(
            {
                "site_id": frame["monitoring_location_id"],
                "variable": row.key,
                "parameter_code": code,
            }
        )
        if "water_year" in frame:
            table["water_year"] = frame["water_year"]
        table["time"] = frame["time"]
        table["value"] = _numeric(frame["value"])
        table["unit"] = frame["unit_of_measure"]
        if row.factor:
            table["value_metric"] = np.round(table["value"] * row.factor, 5)
            table["unit_metric"] = row.metric_unit
        table["status"] = frame.get("approval_status", "")
        table["qualifier"] = frame.get("qualifier", "")
        frames.append(table)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values(["site_id", "variable", "time"])


CHANNEL_PROPERTIES = (
    "monitoring_location_id,time,channel_name,channel_flow,channel_flow_unit,channel_width,"
    "channel_width_unit,channel_area,channel_area_unit,channel_velocity,channel_velocity_unit,"
    "channel_material,channel_stability,channel_evenness,measurement_type,"
    "channel_location_distance,channel_location_distance_unit,channel_location_direction"
)
# Original unit -> factor to the metric unit.
CHANNEL_UNITS = {
    "width": {"ft": 0.3048, "m": 1.0},
    "area": {"ft^2": 0.09290304, "m^2": 1.0},
    "velocity": {"ft/s": 0.3048, "m/s": 1.0},
    "flow": {"ft^3/s": 0.028316846592, "m^3/s": 1.0},
}
CHANNEL_METRIC = {"width": "m", "area": "m2", "velocity": "ms", "flow": "m3s"}


def download_channel_table(client, entries, bbox, start, end):
    """Channel width, area, velocity, and flow of each discharge measurement."""
    frame = client.csv(
        "channel-measurements",
        {"bbox": bbox, "time": f"{start}/{end}", "properties": CHANNEL_PROPERTIES},
        sortby="monitoring_location_id,time",
    )
    site_ids = {entry["site_id"] for entry in entries}
    if frame.empty:
        return pd.DataFrame()
    frame = frame[frame["monitoring_location_id"].isin(site_ids)]
    if frame.empty:
        return pd.DataFrame()
    table = pd.DataFrame({"site_id": frame["monitoring_location_id"], "time": frame["time"]})
    for name, units in CHANNEL_UNITS.items():
        value = _numeric(frame[f"channel_{name}"])
        unit = frame[f"channel_{name}_unit"].fillna("")
        table[name] = value
        table[f"{name}_unit"] = unit
        table[f"{name}_{CHANNEL_METRIC[name]}"] = np.round(value * unit.map(units).astype(float), 5)
    # Mean depth = cross-section area / width.
    with np.errstate(divide="ignore", invalid="ignore"):
        depth = table["area_m2"] / table["width_m"].where(table["width_m"] > 0)
    table["mean_depth_m"] = np.round(depth, 4)
    for column in ("channel_material", "channel_stability", "channel_evenness", "measurement_type",
                   "channel_name", "channel_location_distance", "channel_location_distance_unit",
                   "channel_location_direction"):
        table[column.replace("channel_", "")] = frame.get(column, "")
    return table.sort_values(["site_id", "time"])


# ---------------------------------------------------------------------------
# NOAA GHCN precipitation: daily (GHCN-Daily) and hourly (GHCNh)
# ---------------------------------------------------------------------------


def _year_chunks(start, end, years):
    for year in range(int(start[:4]), int(end[:4]) + 1, years):
        yield max(start, f"{year}-01-01"), min(end, f"{year + years - 1}-12-31")


def ghcn_jobs(entries, start, end):
    """Request batches: groups of stations by periods of up to GHCN_CHUNK_YEARS years."""
    stations = sorted({entry["station"] for entry in entries})
    return [
        (batch, first, last)
        for batch in _batches(stations, GHCN_STATION_BATCH)
        for first, last in _year_chunks(start, end, GHCN_CHUNK_YEARS)
    ]


def download_ghcn(client, entries, start, end, report=None):
    """Daily precipitation (mm) of GHCN-Daily stations as one long table, plus warnings."""
    jobs = ghcn_jobs(entries, start, end)
    frames, warnings = [], []
    for done, (batch, first, last) in enumerate(jobs, start=1):
        try:
            frame = client.ncei_data(
                {
                    "dataset": GHCN_DAILY_DATASET,
                    "stations": ",".join(batch),
                    "startDate": first,
                    "endDate": last,
                    "dataTypes": "PRCP",
                    "units": "metric",
                    "includeAttributes": "true",
                }
            )
        except InterruptedError:
            raise
        except Exception as exc:
            warnings.append(f"GHCN-Daily precipitation of {len(batch)} station(s), {first} to {last}: {exc}")
            frame = pd.DataFrame()
        if not frame.empty and "PRCP" in frame:
            frames.append(frame)
        if report:
            report(done, len(jobs))
    if not frames:
        return pd.DataFrame(columns=["STATION", "DATE", "PRCP", "PRCP_ATTRIBUTES"]), warnings
    table = pd.concat(frames, ignore_index=True)
    if "PRCP_ATTRIBUTES" not in table:
        table["PRCP_ATTRIBUTES"] = ""
    return table, warnings


def write_ghcn_tables(folder, table, entries, start, end):
    """One CSV per station: every day of the period, -99 where no value was reported.

    Returns counts and (first, last) dates of the days with values, per entry key.
    """
    index = pd.date_range(start, end, freq="D")
    counts, periods = {}, {}
    for entry in entries:
        rows = table[table["STATION"] == entry["station"]].drop_duplicates("DATE", keep="last")
        if rows.empty:
            continue
        rows = rows.set_index(pd.to_datetime(rows["DATE"])).reindex(index)
        values = _numeric(rows["PRCP"])
        if not values.notna().any():
            continue
        # PRCP_ATTRIBUTES = "measurement flag,quality flag,source flag,observation time".
        flags = rows["PRCP_ATTRIBUTES"].fillna("").str.split(",", expand=True)
        flags = flags.reindex(columns=range(4)).fillna("")
        output = pd.DataFrame({"date": index.strftime("%Y-%m-%d")})
        output["precipitation_mm"] = values.fillna(MISSING_VALUE).to_numpy()
        output["measurement_flag"] = flags[0].to_numpy()
        output["quality_flag"] = flags[1].to_numpy()
        output["source_flag"] = flags[2].to_numpy()
        output["observation_time"] = flags[3].to_numpy()
        output["site_id"] = entry["site_id"]
        folder.mkdir(parents=True, exist_ok=True)
        output.to_csv(folder / f"ghcnd_{entry['station'].lower()}_daily.csv", index=False,
                      float_format=FLOAT_FORMAT)
        key = (entry["site_id"], entry["row"], entry["column"])
        counts[key] = int(values.notna().sum())
        days = values.dropna().index
        periods[key] = (days.min(), days.max())
    return counts, periods


def local_hours(start, end, timezone):
    """Beginning of every hour (UTC) of the local days start..end."""
    first = pd.Timestamp(start).tz_localize(timezone).tz_convert("UTC")
    last = (pd.Timestamp(end) + pd.Timedelta(days=1)).tz_localize(timezone).tz_convert("UTC")
    return pd.date_range(first, last, freq="h", inclusive="left")


def _hour_chunks(start, end, timezone):
    """UTC request windows of up to GHCNH_CHUNK_YEARS years.

    The windows reach one hour beyond each end of the period: the special reports of
    its first hour come before it, and minute-0 reports close its last hour after it.
    """
    hours = local_hours(start, end, timezone)
    first = hours[0] - pd.Timedelta(hours=1)
    last = hours[-1] + pd.Timedelta(hours=2)
    while first < last:
        stop = min(first + pd.DateOffset(years=GHCNH_CHUNK_YEARS), last)
        yield f"{first:%Y-%m-%dT%H:%M:%S}", f"{stop - pd.Timedelta(seconds=1):%Y-%m-%dT%H:%M:%S}"
        first = stop


def ghcnh_jobs(entries, start, end, timezone=TIMEZONE):
    """Request batches: groups of stations by periods of up to GHCNH_CHUNK_YEARS years."""
    stations = sorted({entry["station"] for entry in entries})
    return [
        (batch, first, last)
        for batch in _batches(stations, GHCNH_STATION_BATCH)
        for first, last in _hour_chunks(start, end, timezone)
    ]


def download_ghcnh(client, entries, start, end, timezone=TIMEZONE, report=None):
    """GHCNh precipitation reports (mm, UTC) of the stations as one long table, plus warnings."""
    jobs = ghcnh_jobs(entries, start, end, timezone)
    columns = ["STATION", "DATE", "precipitation"] + GHCNH_FIELDS
    frames, warnings = [], []
    for done, (batch, first, last) in enumerate(jobs, start=1):
        try:
            frame = client.ncei_data(
                {
                    "dataset": GHCNH_DATASET,
                    "stations": ",".join(batch),
                    "startDate": first,
                    "endDate": last,
                    "dataTypes": ",".join(columns),
                }
            )
        except InterruptedError:
            raise
        except Exception as exc:
            warnings.append(f"GHCNh precipitation of {len(batch)} station(s), {first} to {last}: {exc}")
            frame = pd.DataFrame()
        if not frame.empty and "precipitation" in frame:
            # Most rows of 5- and 15-minute stations carry no precipitation total.
            frames.append(frame[frame["precipitation"] != ""].reindex(columns=columns, fill_value=""))
        if report:
            report(done, len(jobs))
    if not frames:
        return pd.DataFrame(columns=columns), warnings
    return pd.concat(frames, ignore_index=True), warnings


def ghcnh_hourly(rows):
    """Hourly totals of one GHCNh station, indexed by the beginning of the hour (UTC).

    A report holds the precipitation since the station's routine report of the hour
    before. Airports also send special reports (METAR SPECI) with running totals within
    the hour, so, as the GHCNh documentation advises, the last report of each hour is
    kept. A report belongs to the hour holding most of the 60 minutes that end at the
    next routine report (the most common minute of the station in that year): the
    routine report at 13:56 and the specials at 13:15 and 13:35 give the 13:00 hour,
    and a report at 14:00 (USCRN and COOP gauges) gives the 13:00 hour too.
    """
    values = _numeric(rows["precipitation"])
    rows = rows[values.notna() & (values >= 0)].assign(value=values)
    if rows.empty:
        return pd.DataFrame(columns=list(rows.columns) + ["time"])
    rows = rows.assign(time=pd.to_datetime(rows["DATE"], utc=True).dt.floor("min")).sort_values("time")
    parts = []
    for _, part in rows.groupby(rows["time"].dt.year, sort=True):
        minute = int(part["time"].dt.minute.mode().iloc[0])
        ends = part["time"] + pd.to_timedelta((minute - part["time"].dt.minute) % 60, unit="min")
        parts.append(part.assign(hour=(ends - pd.Timedelta(minutes=30)).dt.floor("h")))
    return pd.concat(parts).drop_duplicates("hour", keep="last").set_index("hour")


def write_ghcnh_tables(folder, table, entries, start, end, timezone=TIMEZONE):
    """One CSV per station: every hour of the local days of the period, -99 where no
    total was reported. Returns counts and (first, last) hours with values per entry key.
    """
    hours = local_hours(start, end, timezone)
    local = hours.tz_convert(timezone)
    counts, periods = {}, {}
    for entry in entries:
        hourly = ghcnh_hourly(table[table["STATION"] == entry["station"]])
        if hourly.empty:
            continue
        hourly = hourly.reindex(hours)
        known = hourly["value"].notna().to_numpy()
        if not known.any():
            continue
        output = pd.DataFrame({
            "datetime_local": local.strftime("%Y-%m-%dT%H:%M%z"),
            "datetime_utc": hours.strftime("%Y-%m-%dT%H:%MZ"),
            "precipitation_mm": hourly["value"].fillna(MISSING_VALUE).to_numpy(),
        })
        for column, field in zip(("measurement_code", "quality_code", "report_type", "source_code"), GHCNH_FIELDS):
            output[column] = hourly[field].fillna("").to_numpy()
        output["report_time_utc"] = hourly["time"].dt.strftime("%Y-%m-%dT%H:%MZ").fillna("").to_numpy()
        output["site_id"] = entry["site_id"]
        folder.mkdir(parents=True, exist_ok=True)
        output.to_csv(folder / f"ghcnh_{entry['station'].lower()}_hourly.csv", index=False,
                      float_format=FLOAT_FORMAT)
        key = (entry["site_id"], entry["row"], entry["column"])
        counts[key] = int(known.sum())
        periods[key] = (local[known].min(), local[known].max())
    return counts, periods


# ---------------------------------------------------------------------------
# Water-quality samples (USGS Samples and Water Quality Portal)
# ---------------------------------------------------------------------------


def _batches(items, size):
    items = sorted(items)
    return [items[first:first + size] for first in range(0, len(items), size)]


def _standard_samples(frame, mapping, source, row, group=None):
    if frame.empty:
        return pd.DataFrame(columns=SAMPLE_COLUMNS + AUDIT_COLUMNS)
    table = pd.DataFrame(index=frame.index)
    for column in SAMPLE_COLUMNS + AUDIT_COLUMNS:
        source_column = mapping.get(column)
        table[column] = frame[source_column] if source_column in frame else ""
    table["source"] = source
    table["variable"] = row.key
    if group:
        table["group"] = group
    if row.sample_characteristics:
        table = table[table["characteristic"].isin(row.sample_characteristics)]
    return table


def download_samples(client, entries, start, end, sites, report=None):
    """Samples of the selected sites; USGS sites from USGS Samples, others from WQP."""
    jobs = []
    rows = {}
    for entry in entries:
        rows.setdefault(entry["row"], {USGS: set(), OTHER: set()})[entry["source"]].add(entry["site_id"])
    for row_key, by_source in rows.items():
        row = ROWS_BY_KEY[row_key]
        for batch in _batches(by_source[USGS], USGS_SAMPLE_BATCH):
            jobs.append(("usgs", row, batch, None))
        for batch in _batches(by_source[OTHER], WQP_SAMPLE_BATCH):
            for group in row.sample_groups or [None]:
                jobs.append(("wqp", row, batch, group))

    def run(job):
        kind, row, batch, group = job
        if kind == "usgs":
            name = "characteristicGroup" if row.sample_groups else "characteristic"
            values = row.sample_groups or row.sample_characteristics
            params = [("monitoringLocationIdentifier", site) for site in batch]
            params += [(name, value) for value in values]
            params += [("activityStartDateLower", start), ("activityStartDateUpper", end)]
            frame = client.samples("results/narrow", params)
            return _standard_samples(frame, SAMPLES_MAP, USGS_SAMPLES, row)
        params = {
            "siteid": ";".join(batch),
            "startDateLo": wqp_date(start),
            "startDateHi": wqp_date(end),
        }
        if group:
            params["characteristicType"] = group
        else:
            params["characteristicName"] = ";".join(row.sample_characteristics)
        frame = client.wqp("Result", params)
        return _standard_samples(frame, WQP_MAP, WQP, row, group)

    tables, warnings = [], []
    lock = threading.Lock()
    finished = [0]

    def guarded(job):
        try:
            table = run(job)
        except InterruptedError:
            raise
        except Exception as exc:
            table = None
            with lock:
                warnings.append(f"Samples of {len(job[2])} site(s) for {job[1].label} failed: {exc}")
        with lock:
            finished[0] += 1
            if report:
                report(finished[0], len(jobs))
        return table

    with ThreadPoolExecutor(max_workers=SAMPLE_WORKERS) as pool:
        for table in pool.map(guarded, jobs):
            if table is not None and not table.empty:
                tables.append(table)
    if not tables:
        return pd.DataFrame(columns=SAMPLE_COLUMNS + AUDIT_COLUMNS), warnings
    samples = pd.concat(tables, ignore_index=True)
    for column, key in (("site_name", "name"), ("organization_name", "org_name")):
        known = samples["site_id"].map({site_id: info.get(key, "") for site_id, info in sites.items()})
        samples[column] = samples[column].fillna("").where(samples[column].fillna("") != "", known.fillna(""))
    samples = samples.drop_duplicates()
    columns = SAMPLE_COLUMNS + AUDIT_COLUMNS
    return samples.sort_values(["site_id", "date", "time", "characteristic"])[columns], warnings


def _project_names(client, samples, warnings):
    """Names of the Water Quality Portal projects in the samples, in one request."""
    projects = samples[(samples["source"] == WQP) & (samples["project"].fillna("") != "")]
    organizations = sorted(set(projects["organization"].fillna("")) - {""})
    names = {}

    def read(batch):
        frame = client.wqp("Project", {"organization": ";".join(batch)})
        for record in frame.to_dict("records"):
            key = (record.get("OrganizationIdentifier", ""), record.get("ProjectIdentifier", ""))
            names[key] = record.get("ProjectName", "")

    if not organizations:
        return names
    try:
        read(organizations)
    except InterruptedError:
        raise
    except Exception:
        # One unknown organization fails the whole request; retry them one by one.
        failed = []
        for organization in organizations:
            try:
                read([organization])
            except InterruptedError:
                raise
            except Exception:
                failed.append(organization)
        if failed:
            warnings.append(f"Water Quality Portal project names could not be read for: {', '.join(failed)}.")
    return names


# ---------------------------------------------------------------------------
# Station layers
# ---------------------------------------------------------------------------


LAYER_FIELDS = [
    ("site_id", "string"), ("name", "string"), ("site_type", "string"), ("org", "string"),
    ("source", "string"), ("drain_km2", "real"), ("datasets", "string"),
    ("first_date", "string"), ("last_date", "string"), ("n_records", "integer"),
]


def write_layers(layers, path, target_crs, shapefile_dirs=None):
    """Write point layers to one GeoPackage (and optional shapefiles)."""
    from osgeo import ogr, osr

    source = osr.SpatialReference()
    source.ImportFromEPSG(4326)
    source.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    target = _spatial_reference(target_crs)
    transform = osr.CoordinateTransformation(source, target)
    types = {"string": ogr.OFTString, "real": ogr.OFTReal, "integer": ogr.OFTInteger}

    def write(datasource, name, records):
        layer = datasource.CreateLayer(name, target, ogr.wkbPoint)
        for field, kind in LAYER_FIELDS:
            definition = ogr.FieldDefn(field, types[kind])
            if field == "datasets":
                definition.SetWidth(254)
            layer.CreateField(definition)
        for record in records:
            feature = ogr.Feature(layer.GetLayerDefn())
            for field, kind in LAYER_FIELDS:
                value = record.get(field)
                if value is None or (isinstance(value, float) and np.isnan(value)):
                    continue
                feature.SetField(field, str(value)[:254] if kind == "string" else value)
            point = ogr.CreateGeometryFromWkt(f"POINT ({record['lon']} {record['lat']})")
            point.Transform(transform)
            feature.SetGeometry(point)
            layer.CreateFeature(feature)
            feature = None

    gpkg_driver = ogr.GetDriverByName("GPKG")
    stem, number = path.with_suffix(""), 1
    while path.exists():
        gpkg_driver.DeleteDataSource(str(path))
        if not path.exists():
            break
        number += 1  # still open in QGIS from an earlier run
        path = Path(f"{stem}_{number}.gpkg")
    datasource = gpkg_driver.CreateDataSource(str(path))
    if datasource is None:
        raise OSError(f"Could not create {path}.")
    for name, records in layers.items():
        if records:
            write(datasource, name, records)
    datasource = None

    shapefiles = []
    if shapefile_dirs:
        shp_driver = ogr.GetDriverByName("ESRI Shapefile")
        for name, records in layers.items():
            if not records:
                continue
            shp_path = shapefile_dirs[name] / f"{name}.shp"
            if shp_path.exists():
                shp_driver.DeleteDataSource(str(shp_path))
            datasource = shp_driver.CreateDataSource(str(shp_path))
            write(datasource, name, records)
            datasource = None
            shapefiles.append(shp_path)
    return path, shapefiles


# ---------------------------------------------------------------------------
# Audit of what was downloaded (README summary)
# ---------------------------------------------------------------------------


def _day(value):
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return ""
    return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value)[:10]


def _audit(service, site_id, variable, dataset, records, first, last, sites,
           organization=None, organization_name=None, project=""):
    """One README row: what a service returned for one site, variable, and dataset."""
    site = sites.get(site_id, {})
    return {
        "service": service,
        "organization": site.get("org", "") if organization is None else organization,
        "organization_name": site.get("org_name", "") if organization_name is None else organization_name,
        "project": project,
        "site_id": site_id,
        "variable": variable,
        "dataset": dataset,
        "records": int(records),
        "first": _day(first),
        "last": _day(last),
    }


def _audit_table(table, dataset, sites, variable=None):
    """Audit rows of a long USGS table with site_id, time, and (unless given) variable."""
    keys = ["site_id"] if variable else ["site_id", "variable"]
    rows = []
    for key, group in table.groupby(keys):
        key = key if isinstance(key, tuple) else (key,)
        days = group["time"].astype(str).str[:10]
        rows.append(_audit(WATER_DATA, key[0], variable or key[1], dataset, len(group),
                           days.min(), days.max(), sites))
    return rows


def _audit_samples(samples, sites):
    rows = []
    table = samples.fillna("")
    keys = ["source", "organization", "organization_name", "project", "variable", "site_id"]
    for (source, organization, name, project, variable, site_id), group in table.groupby(keys):
        dates = group["date"][group["date"] != ""]
        rows.append(_audit(source, site_id, variable, SAMPLES, len(group), dates.min() if len(dates) else "",
                           dates.max() if len(dates) else "", sites, organization, name, project))
    return rows


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def estimate_requests(inventory, settings):
    """Approximate number of web requests a download will make."""
    selection = {tuple(item) for item in settings["selection"]}
    sources = tuple(settings.get("sample_sources", (USGS, OTHER)))
    site_ids = settings.get("site_ids")
    entries = inventory.selected_entries(selection, sources, set(site_ids) if site_ids else None)
    days = (pd.Timestamp(inventory.end) - pd.Timestamp(inventory.start)).days + 1
    years = int(inventory.end[:4]) - int(inventory.start[:4]) + 1
    noaa = [e for e in entries if e["source"] == NOAA]
    total = len(ghcn_jobs([e for e in noaa if e["column"] == DAILY], inventory.start, inventory.end))
    total += len(ghcnh_jobs([e for e in noaa if e["column"] == CONTINUOUS], inventory.start, inventory.end,
                            settings.get("timezone", TIMEZONE)))
    point_codes = set()
    samples = {}
    for entry in entries:
        if entry["source"] == NOAA:
            continue
        if entry["column"] == DAILY:
            total += -(-years // DAILY_CHUNK_YEARS)
        elif entry["column"] == CONTINUOUS:
            total += -(-days // CONTINUOUS_CHUNK_DAYS)
        elif entry["column"] in (FIELD, PEAKS):
            point_codes.add((entry["column"], entry["parameter_code"] or entry["row"]))
        else:
            samples.setdefault(entry["row"], {USGS: 0, OTHER: 0})[entry["source"]] += 1
    total += len(point_codes)
    for row_key, counts in samples.items():
        groups = max(len(ROWS_BY_KEY[row_key].sample_groups), 1)
        total += -(-counts[USGS] // USGS_SAMPLE_BATCH)
        total += -(-counts[OTHER] // WQP_SAMPLE_BATCH) * groups
    return total


def extract_usgs(settings, inventory, progress=None, is_canceled=None, client=None):
    selection = {tuple(item) for item in settings["selection"]}
    sources = tuple(settings.get("sample_sources", (USGS, OTHER)))
    site_ids = settings.get("site_ids")
    entries = inventory.selected_entries(selection, sources, set(site_ids) if site_ids else None)
    if not entries:
        raise ValueError("Nothing is selected for download. Check at least one available box.")
    client = client or UsgsClient(settings.get("api_key", ""), is_canceled)
    start, end = inventory.start, inventory.end
    timezone = settings.get("timezone", TIMEZONE)
    base = Path(settings["output_dir"]) / "usgs_water_data"
    folders = {QUANTITY: base / "quantity", QUALITY: base / "quality"}
    warnings = list(inventory.warnings)
    counts = {}

    def report(percentage, message):
        if progress:
            progress(percentage, message)

    series = [e for e in entries if e["column"] in (DAILY, CONTINUOUS) and e["source"] != NOAA]
    lock = threading.Lock()
    finished = [0]

    def fetch(entry):
        try:
            if entry["column"] == DAILY:
                data = download_daily(client, entry, start, end)
            else:
                data = download_continuous(client, entry, start, end)
        except InterruptedError:
            raise
        except Exception as exc:
            data = None
            with lock:
                warnings.append(f"{entry['site_id']} {entry['row']} {entry['column']}: {exc}")
        with lock:
            finished[0] += 1
            count = finished[0]
        report(60.0 * count / max(len(series), 1),
               f"Downloading USGS series: {count} of {len(series)}")
        return entry, data

    results = {(group, column): [] for group in (QUANTITY, QUALITY) for column in (DAILY, CONTINUOUS)}
    report(0.0, f"Downloading USGS series: 0 of {len(series)}")
    with ThreadPoolExecutor(max_workers=SERIES_WORKERS) as pool:
        for entry, data in pool.map(fetch, series):
            if data is not None and data["value"].notna().any():
                group = ROWS_BY_KEY[entry["row"]].group
                results[(group, entry["column"])].append((entry, data))
    for (group, column), items in results.items():
        if not items:
            continue
        if column == DAILY:
            counts.update(write_daily_tables(folders[group] / "daily", items, start, end))
        else:
            counts.update(write_continuous_tables(folders[group] / "continuous", items))
    audit = []
    for (_group, column), items in results.items():
        for entry, data in items:
            records = counts.get((entry["site_id"], entry["row"], column), 0)
            if records:
                valid = data["value"].dropna().index
                audit.append(_audit(WATER_DATA, entry["site_id"], entry["row"], column, records,
                                    valid.min(), valid.max(), inventory.sites))

    for column, collection, filename in (
        (FIELD, "field-measurements", "field_measurements.csv"),
        (PEAKS, "peaks", "annual_peaks.csv"),
    ):
        selected = [e for e in entries if e["column"] == column and e["row"] != CHANNEL]
        if not selected:
            continue
        report(62.0, f"Downloading USGS {column.replace('field', 'field measurements')}...")
        try:
            table = download_point_table(client, collection, selected, inventory.bbox, start, end)
        except InterruptedError:
            raise
        except Exception as exc:
            warnings.append(f"{filename}: {exc}")
            continue
        if not table.empty:
            folders[QUANTITY].mkdir(parents=True, exist_ok=True)
            table.to_csv(folders[QUANTITY] / filename, index=False)
            for (site_id, variable), size in table.groupby(["site_id", "variable"]).size().items():
                counts[(site_id, variable, column)] = int(size)
            audit += _audit_table(table, column, inventory.sites)

    channel = [e for e in entries if e["row"] == CHANNEL]
    if channel:
        report(66.0, "Downloading USGS channel geometry...")
        try:
            table = download_channel_table(client, channel, inventory.bbox, start, end)
        except InterruptedError:
            raise
        except Exception as exc:
            warnings.append(f"channel_measurements.csv: {exc}")
            table = pd.DataFrame()
        if not table.empty:
            folders[QUANTITY].mkdir(parents=True, exist_ok=True)
            table.to_csv(folders[QUANTITY] / "channel_measurements.csv", index=False)
            for site_id, size in table.groupby("site_id").size().items():
                counts[(site_id, CHANNEL, FIELD)] = int(size)
            audit += _audit_table(table, FIELD, inventory.sites, variable=CHANNEL)

    for column, service, title, share in ((DAILY, GHCN, "GHCN-Daily", 67.0), (CONTINUOUS, GHCNH, "GHCNh hourly", 68.5)):
        ghcn = [e for e in entries if e["source"] == NOAA and e["column"] == column]
        if not ghcn:
            continue
        message = f"Downloading NOAA {title} precipitation"
        report(share, f"{message}...")

        def step(done, total, share=share, message=message):
            report(share + 1.5 * done / total, f"{message}: {done} of {total} requests")

        if column == DAILY:
            table, ghcn_warnings = download_ghcn(client, ghcn, start, end, step)
            ghcn_counts, periods = write_ghcn_tables(folders[QUANTITY] / "daily", table, ghcn, start, end)
        else:
            table, ghcn_warnings = download_ghcnh(client, ghcn, start, end, timezone, step)
            ghcn_counts, periods = write_ghcnh_tables(
                folders[QUANTITY] / "hourly", table, ghcn, start, end, timezone
            )
        warnings.extend(ghcn_warnings)
        counts.update(ghcn_counts)
        for (site_id, row_key, entry_column), records in ghcn_counts.items():
            site = inventory.sites.get(site_id, {})
            first, last = periods[(site_id, row_key, entry_column)]
            dataset = DAILY if column == DAILY else HOURLY
            audit.append(_audit(service, site_id, row_key, dataset, records, first, last, inventory.sites,
                                site.get("network", ""), site.get("network_name", "")))

    project_names = {}
    sample_entries = [e for e in entries if e["column"] == SAMPLES]
    if sample_entries:
        samples, sample_warnings = download_samples(
            client, sample_entries, start, end, inventory.sites,
            lambda done, total: report(70.0 + 25.0 * done / total, f"Downloading water-quality samples: {done} of {total} requests"),
        )
        warnings.extend(sample_warnings)
        if not samples.empty:
            folders[QUALITY].mkdir(parents=True, exist_ok=True)
            samples[SAMPLE_COLUMNS].to_csv(folders[QUALITY] / "samples.csv", index=False)
            for (site_id, variable), size in samples.groupby(["site_id", "variable"]).size().items():
                counts[(site_id, variable, SAMPLES)] = int(size)
            audit += _audit_samples(samples, inventory.sites)
            report(95.0, "Reading Water Quality Portal project names...")
            project_names = _project_names(client, samples, warnings)

    report(96.0, "Writing station layers...")
    downloaded = [e for e in entries if counts.get((e["site_id"], e["row"], e["column"]))]
    empty = [e for e in entries if not counts.get((e["site_id"], e["row"], e["column"]))]
    if not downloaded:
        failures = warnings[len(inventory.warnings):]
        raise RuntimeError(
            f"The selected stations returned no values between {start} and {end}."
            + (" Download problems: " + " ".join(failures[:3]) if failures else "")
        )
    layers = {name: [] for name in LAYER_NAMES.values()}
    for group, layer_name in LAYER_NAMES.items():
        group_entries = [e for e in downloaded if layer_of(e["row"]) == group]
        for site_id in sorted({e["site_id"] for e in group_entries}):
            site = inventory.sites.get(site_id, {})
            first, last = inventory.period(site_id, group, group_entries)
            layers[layer_name].append(
                {
                    "site_id": site_id,
                    "name": site.get("name", ""),
                    "site_type": site.get("type", ""),
                    "org": site.get("org", ""),
                    "source": SITE_SOURCES.get(site.get("source"), "Other agency"),
                    "drain_km2": site.get("drainage_km2"),
                    "datasets": inventory.describe(site_id, group, group_entries),
                    "first_date": first,
                    "last_date": last,
                    "n_records": sum(
                        counts.get((site_id, e["row"], e["column"]), 0)
                        for e in group_entries if e["site_id"] == site_id
                    ),
                    "lon": site.get("lon"),
                    "lat": site.get("lat"),
                }
            )
    label = run_label(settings["output_dir"])
    shapefile_dirs = None
    if settings.get("write_shapefile"):
        shapefile_dirs = {
            "quantity_stations": folders[QUANTITY],
            "channel_geometry_stations": folders[QUANTITY],
            "quality_points": folders[QUALITY],
        }
        for folder in shapefile_dirs.values():
            folder.mkdir(parents=True, exist_ok=True)
    base.mkdir(parents=True, exist_ok=True)
    gpkg_path, shapefiles = write_layers(
        layers, base / f"usgs_{label}.gpkg", settings.get("target_crs"), shapefile_dirs
    )
    inventory.to_frame(selection, sources).to_csv(base / "available_data.csv", index=False)

    provisional = 0
    for items in results.values():
        provisional += sum(int((data["approval_status"] == "Provisional").sum()) for _, data in items)
    if provisional:
        warnings.append(
            f"{provisional} USGS value(s) are provisional and may be revised (see the status columns)."
        )
    warnings_file = write_warnings(base, warnings)
    _write_readme(
        base / "README.txt", inventory, selection, sources, layers, warnings,
        audit, project_names, skipped=len(inventory.entries) - len(entries), empty=empty, timezone=timezone,
    )
    report(100.0, "USGS extraction complete")
    return {
        "kind": "usgs",
        "station_count": len(
            {r["site_id"] for r in layers["quantity_stations"] + layers["channel_geometry_stations"]}
        ),
        "point_count": len(layers["quality_points"]),
        "gpkg": gpkg_path,
        "layers": [name for name, records in layers.items() if records],
        "shapefiles": shapefiles,
        "label": label,
        "output_dir": base,
        "summary": base / "available_data.csv",
        "warnings": warnings,
        "warnings_file": warnings_file,
    }


def _text_table(header, rows, right=()):
    """Plain-text table, indented; columns listed in ``right`` are right-aligned."""
    widths = [max(len(str(value)) for value in column) for column in zip(header, *rows)]
    lines = []
    for values in [header] + rows:
        cells = [
            str(value).rjust(width) if index in right else str(value).ljust(width)
            for index, (value, width) in enumerate(zip(values, widths))
        ]
        lines.append("   " + "  ".join(cells).rstrip())
    return lines


def _organization_label(code, name):
    return f"{code} ({name})" if code and name else (code or name or "unknown")


def _project_name(names, organization, project):
    """Project name; results can list several projects separated by ';'."""
    parts = [part for part in project.split(";") if part]
    if len(parts) <= 1:
        return names.get((organization, project), "")
    return "; ".join(
        f"{part}: {names[(organization, part)]}" if names.get((organization, part)) else part for part in parts
    )


def _included_lines(audit, project_names):
    """Per-service tables of what was downloaded, by variable, dataset, and organization."""
    frame = pd.DataFrame(audit, columns=[
        "service", "organization", "organization_name", "project", "site_id",
        "variable", "dataset", "records", "first", "last",
    ]).fillna("")
    lines = []
    for number, (service, title, url) in enumerate(SERVICES, 1):
        lines += ["", f"{number}. {title}", f"   {url}"]
        part = frame[frame["service"] == service]
        if part.empty:
            lines.append("   No values downloaded.")
            continue
        grouped = (
            part.groupby(["variable", "dataset", "organization", "organization_name"])
            .agg(stations=("site_id", "nunique"), records=("records", "sum"),
                 first=("first", lambda s: min((v for v in s if v), default="")),
                 last=("last", "max"))
            .reset_index()
        )
        grouped["label"] = grouped["variable"].map(lambda key: ROWS_BY_KEY[key].label)
        grouped["order"] = grouped["dataset"].map(DATASET_ORDER)
        grouped = grouped.sort_values(["label", "order", "organization"])
        noun = "Results" if service in (USGS_SAMPLES, WQP) else "Records"
        lines += _text_table(
            ["Variable", "Dataset", "Stations", noun, "First", "Last", "Organization"],
            [
                [r.label, r.dataset, r.stations, f"{r.records:,}", r.first, r.last,
                 _organization_label(r.organization, r.organization_name)]
                for r in grouped.itertuples()
            ],
            right=(2, 3),
        )
        if service != WATER_DATA and (part["project"] != "").any():
            projects = (
                part.groupby(["organization", "project"])
                .agg(stations=("site_id", "nunique"), records=("records", "sum"),
                     first=("first", lambda s: min((v for v in s if v), default="")),
                     last=("last", "max"))
                .reset_index()
                .sort_values(["organization", "project"])
            )
            lines += ["", "   Projects (ProjectIdentifier reported with each result)"]
            lines += _text_table(
                ["Organization", "Project", "Stations", "Results", "First", "Last", "Name"],
                [
                    [r.organization, r.project or "(none)", r.stations, f"{r.records:,}", r.first, r.last,
                     _project_name(project_names, r.organization, r.project)]
                    for r in projects.itertuples()
                ],
                right=(2, 3),
            )
    return lines


def _write_readme(path, inventory, selection, sources, layers, warnings,
                  audit=(), project_names=None, skipped=0, empty=(), timezone=TIMEZONE):
    today = date.today().isoformat()
    chosen = sorted(f"{ROWS_BY_KEY[row].label}: {column}" for row, column in selection)
    lines = [
        "Observed data (USGS, Water Quality Portal, NOAA GHCN) retrieved with Weather Extractor",
        f"Data accessed: {today}",
        f"Period: {inventory.start} through {inventory.end}",
        f"Search margin around the area of interest: {inventory.margin_km:g} km",
        f"Timezone of the hourly GHCNh files: {timezone} (local standard time)",
        "Selected datasets: " + "; ".join(chosen),
        "Sample sources: " + ", ".join(
            {USGS: "USGS Samples", OTHER: "Water Quality Portal (EPA; non-USGS agencies)"}[s] for s in sources
        ),
        "",
        "=" * 79,
        "DATA INCLUDED IN THIS DOWNLOAD",
        "Stations with at least one value in the period; counts match the CSV files.",
        "=" * 79,
    ]
    lines += _included_lines(list(audit), project_names or {})
    points = Counter(record.get("org") or "unknown" for record in layers["quality_points"])
    stations = Counter(record.get("source") or "unknown" for record in layers["quantity_stations"])
    lines += [
        "",
        f"Totals: {len(layers['quantity_stations'])} quantity stations; "
        f"{len(layers['channel_geometry_stations'])} channel geometry stations; "
        f"{len(layers['quality_points'])} water-quality points.",
    ]
    if stations:
        lines.append(
            "Quantity stations by source: "
            + ", ".join(f"{name} {count}" for name, count in sorted(stations.items(), key=lambda item: (-item[1], item[0])))
        )
    if points:
        lines.append(
            "Water-quality points by organization: "
            + ", ".join(f"{org} {count}" for org, count in sorted(points.items(), key=lambda item: (-item[1], item[0])))
        )
    lines += [
        "",
        "Not included:",
        f"- {skipped} dataset(s) found by 'Check available data' but not downloaded (unchecked, "
        "sample source off, or outside the map selection); see available_data.csv.",
    ]
    if empty:
        lines.append(f"- {len(empty)} selected dataset(s) returned no values in the period:")
        for entry in empty[:MAX_EMPTY_LISTED]:
            lines.append(f"    {entry['site_id']}  {ROWS_BY_KEY[entry['row']].label}  {entry['column']}")
        if len(empty) > MAX_EMPTY_LISTED:
            lines.append(f"    ... and {len(empty) - MAX_EMPTY_LISTED} more")
    else:
        lines.append("- Every selected dataset returned values.")
    lines += [
        "",
        "Files:",
        "- available_data.csv: every dataset found by 'Check available data' (selected = downloaded).",
        "- quantity/daily/<site>_daily.csv: one column set per variable (original unit, metric unit, "
        "status, qualifier).",
        "- quantity/daily/ghcnd_<station>_daily.csv: NOAA GHCN-Daily precipitation_mm with the NOAA "
        "measurement, quality, and source flags and the observation time.",
        "- quantity/hourly/ghcnh_<station>_hourly.csv: NOAA GHCNh hourly precipitation_mm with the "
        "GHCNh measurement, quality, report type, and source codes and the time of the report used.",
        "- quantity/continuous/<site>_<variable>.csv: USGS sensor values (Sub-daily column of the tab) at "
        "their original interval, usually 15 min, in UTC.",
        "- quantity/field_measurements.csv and annual_peaks.csv: long tables for all sites.",
        "- quantity/channel_measurements.csv: width, cross-section area, mean velocity, and flow of "
        "each discharge measurement (original and metric units), mean_depth_m = area / width, "
        "and bed material.",
        "- quality/daily and quality/continuous: water-quality sensor series.",
        "- quality/samples.csv: one row per sample result (USGS Samples and Water Quality Portal).",
        "- usgs_<folder>.gpkg: quantity_stations, channel_geometry_stations, and quality_points layers.",
        "",
        "Conventions:",
        "- site_id links the station layers and every CSV table (NOAA gauges: GHCN-<station>, the same "
        "for their daily and hourly files).",
        f"- Daily CSV files list every day of the period; {MISSING_VALUE} marks a day without a value "
        "(in both the original and the metric column); its status and flags are empty.",
        f"- Hourly GHCNh files list every hour of the local days of the period; {MISSING_VALUE} marks an "
        "hour without a total. Rows are labeled by the beginning of the hour, in UTC and in local "
        "standard time, as the AORC hourly files.",
        "- Daily values are the USGS daily statistic of each site's local day (mean unless the "
        "column name says min, max).",
        "- GHCN-Daily precipitation is the total of the 24 hours ending at observation_time (HHMM, "
        "station local time; 2400 = midnight). Volunteer and COOP gauges are usually read in the "
        "morning, so their day differs from a calendar day. measurement_flag T = trace (value 0); "
        "a non-empty quality_flag means the value failed a NOAA quality check (kept as reported).",
        "- GHCNh hourly precipitation: each report holds the rain since the station's routine report "
        "of the hour before (airports report near minute 53-56, USCRN and COOP recording gauges at "
        "minute 0). The last report of each hour is used, so METAR special reports (FM-16) with "
        "running totals are not added twice; a report belongs to the hour holding most of its 60 "
        "minutes (13:56 and 14:00 reports give the 13:00 hour). report_type FM-16 alone means the "
        "routine report of that hour is missing and the total may be partial. Values are kept as "
        "reported; see the GHCNh documentation for the quality codes.",
        "- status is the USGS approval status; Provisional data may be revised by USGS.",
        "- Sample values keep the unit reported by the laboratory; non-detects are flagged in "
        "detection_condition with their detection_limit.",
        f"- USGS samples come from {SAMPLES_URL}; other agencies from the Water Quality Portal, "
        f"{WQP_URL} (legacy WQX 2.2), which serves the data those agencies submit to EPA's WQX.",
        f"- Series, field measurements, and peaks come from {WATERDATA_URL}.",
        f"- GHCN-Daily precipitation comes from {NCEI_DATA_URL} (dataset {GHCN_DAILY_DATASET}); "
        f"GHCNh from the same service (dataset {GHCNH_DATASET}).",
    ]
    if warnings:
        lines += ["", f"Warnings: {len(warnings)}, listed in warnings.txt."]
    lines += [
        "",
        f"Cite as: U.S. Geological Survey, Water Data for the Nation, accessed {today}, at "
        "https://waterdata.usgs.gov/; Water Quality Portal, National Water Quality Monitoring "
        "Council, https://doi.org/10.5066/P9QRKUVJ; Menne, M.J., et al. (2012), Global Historical "
        "Climatology Network - Daily (GHCN-Daily), Version 3, NOAA National Climatic Data Center, "
        f"https://doi.org/10.7289/V5D21VHZ, accessed {today}; Menne, M.J., et al. (2023), Global "
        "Historical Climatology Network-Hourly (GHCNh), NOAA National Centers for Environmental "
        f"Information, https://doi.org/10.25921/jp3d-3v19, accessed {today}.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
