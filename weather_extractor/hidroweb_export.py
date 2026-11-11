"""Download the selected HidroWeb (ANA) daily series and write CSV tables and station layers."""

from collections import Counter
from datetime import date
from pathlib import Path
import re
import threading

import pandas as pd

from .aorc_processing import run_label, write_warnings
from .hidroweb_catalog import LAYER_NAMES, MISSING_VALUE, RAIN, ROWS, ROWS_BY_KEY, STREAMFLOW
from .hidroweb_client import API_URL, LEGACY_URL, HidrowebClient, api_chunks, legacy_chunks, run_with_retries
from .usgs_export import FLOAT_FORMAT, _text_table, write_layers


FOLDER = "hidroweb_data"
SERIES_WORKERS = 4
# Monthly record field holding the first day of the month, by service.
DATE_FIELDS = ("datahora", "datahoradado", "dataleitura", "data")
INDEX_COLUMNS = (
    ["site_id", "code", "name", "station_type", "latitude", "longitude", "altitude_m", "drainage_km2",
     "river", "municipality", "state", "sub_basin", "operator", "responsible", "operating"]
    + [f"{row.key}_days" for row in ROWS]
    + ["first_date", "last_date"]
)


def _fields(record):
    """Field names without case, spaces, or underscores: Vazao01 and Vazao_01 both read vazao01."""
    return {re.sub(r"[^a-z0-9]", "", str(key).lower()): value for key, value in record.items()}


def _float(value):
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _rank(value, default=0):
    number = _float(value)
    return int(number) if number is not None else default


def daily_values(records, row):
    """Daily values of one variable from ANA monthly records (one field per day of the month).

    One value per day: consisted data (NivelConsistencia 2) over raw data (1), and daily
    means (MediaDiaria 1) over single readings.
    """
    prefix = row.prefix.lower()
    best = {}
    for record in records:
        fields = _fields(record)
        month = next((fields[name] for name in DATE_FIELDS if fields.get(name)), None)
        try:
            first = pd.Timestamp(str(month)[:10]) if month else None
        except ValueError:
            first = None
        if first is None:
            continue
        rank = (_rank(fields.get("nivelconsistencia")), _rank(fields.get("mediadiaria"), 1))
        for day in range(1, 32):
            value = _float(fields.get(f"{prefix}{day:02d}"))
            when = first + pd.Timedelta(days=day - 1)
            if value is None or when.month != first.month:
                continue
            if when not in best or rank > best[when][0]:
                best[when] = (rank, value)
    days = sorted(best)
    return pd.Series([best[day][1] for day in days], index=pd.DatetimeIndex(days), dtype=float)


def series_path(folder, site, row):
    return folder / f"ana_{site['code']}_{row.key}.csv"


def write_series(folder, site, row, values, start, end):
    """date and the daily value, every day of the period; -99 where no value was measured."""
    index = pd.date_range(start, end, freq="D")
    table = pd.DataFrame({
        "date": index.strftime("%Y-%m-%d"),
        row.column: values.reindex(index).fillna(MISSING_VALUE).to_numpy(),
    })
    folder.mkdir(parents=True, exist_ok=True)
    table.to_csv(series_path(folder, site, row), index=False, float_format=FLOAT_FORMAT)


def estimate_requests(inventory, settings):
    """Approximate number of web requests a download will make."""
    selection = {tuple(item) for item in settings["selection"]}
    site_ids = settings.get("site_ids")
    entries = inventory.selected_entries(selection, set(site_ids) if site_ids is not None else None)
    if settings.get("user") and settings.get("password"):
        return 1 + len(entries) * len(list(api_chunks(inventory.start, inventory.end)))
    return len(entries) * len(list(legacy_chunks(inventory.start, inventory.end)))


def extract_hidroweb(settings, inventory, progress=None, is_canceled=None, client=None):
    selection = {tuple(item) for item in settings["selection"]}
    site_ids = settings.get("site_ids")
    entries = inventory.selected_entries(selection, set(site_ids) if site_ids is not None else None)
    if not entries:
        raise ValueError("Nothing is selected for download. Check at least one available box.")
    client = client or HidrowebClient(settings.get("user", ""), settings.get("password", ""), is_canceled)
    start, end = inventory.start, inventory.end
    base = Path(settings["output_dir"]) / FOLDER
    warnings = list(inventory.warnings)

    def report(percentage, message):
        if progress:
            progress(percentage, message)

    if client.uses_api:
        report(0.0, "Signing in to the ANA HidroWebService...")
        client.token()  # a wrong login stops here, before any download
    lock = threading.Lock()
    finished = [0]

    def fetch(index):
        entry = entries[index]
        site, row = inventory.sites[entry["site_id"]], ROWS_BY_KEY[entry["row"]]
        values = daily_values(client.series(site["code"], row.data_type, start, end), row)
        values = values[(values.index >= pd.Timestamp(start)) & (values.index <= pd.Timestamp(end))]
        with lock:
            finished[0] += 1
            done = finished[0]
        report(90.0 * done / len(entries), f"Downloading HidroWeb series: {done} of {len(entries)}")
        return entry, values

    def on_retry(count, pause):
        report(90.0 * finished[0] / len(entries),
               f"ANA did not answer {count} series; asking again in {pause} s")

    report(0.0, f"Downloading HidroWeb series: 0 of {len(entries)}")
    workers = max(1, int(settings.get("parallel_workers") or SERIES_WORKERS))
    results, failed, errors = run_with_retries(range(len(entries)), fetch, workers, client.wait, on_retry)
    for index in failed:
        warnings.append(f"{entries[index]['site_id']} {entries[index]['row']}: {errors[index]}")
    counts, periods = {}, {}
    for entry, values in results:
        if values.empty:
            continue
        site, row = inventory.sites[entry["site_id"]], ROWS_BY_KEY[entry["row"]]
        write_series(base / "daily", site, row, values, start, end)
        key = (entry["site_id"], entry["row"])
        counts[key] = len(values)
        periods[key] = (values.index.min(), values.index.max())
    downloaded = [e for e in entries if counts.get((e["site_id"], e["row"]))]
    empty = [e for e in entries if not counts.get((e["site_id"], e["row"]))]
    if not downloaded:
        failures = warnings[len(inventory.warnings):]
        raise RuntimeError(
            f"The selected stations returned no values between {start} and {end}."
            + (" Download problems: " + " ".join(failures[:3]) if failures else "")
        )

    report(92.0, "Writing the station index and layers...")
    index_rows = []
    layers = {name: [] for name in LAYER_NAMES.values()}
    for site_id in sorted({e["site_id"] for e in downloaded}):
        site = inventory.sites[site_id]
        keys = [(site_id, e["row"]) for e in downloaded if e["site_id"] == site_id]
        first = min(periods[key][0] for key in keys).strftime("%Y-%m-%d")
        last = max(periods[key][1] for key in keys).strftime("%Y-%m-%d")
        index_rows.append(dict(
            {
                "site_id": site_id, "code": site["code"], "name": site["name"], "station_type": site["type"],
                "latitude": site["lat"], "longitude": site["lon"], "altitude_m": site["altitude_m"],
                "drainage_km2": site["drainage_km2"], "river": site["river"],
                "municipality": site["municipality"], "state": site["state"], "sub_basin": site["sub_basin"],
                "operator": site["org"], "responsible": site["responsible"], "operating": site["operating"],
                "first_date": first, "last_date": last,
            },
            **{f"{row.key}_days": counts.get((site_id, row.key), 0) for row in ROWS},
        ))
        layers[LAYER_NAMES[site["layer"]]].append({
            "site_id": site_id, "name": site["name"], "site_type": site["type"], "org": site["org"],
            "source": site["responsible"], "drain_km2": site["drainage_km2"],
            "datasets": inventory.describe(site_id, entries=downloaded), "first_date": first,
            "last_date": last, "n_records": sum(counts[key] for key in keys),
            "lon": site["lon"], "lat": site["lat"],
        })
    base.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(index_rows, columns=INDEX_COLUMNS).to_csv(base / "hidroweb_index.csv", index=False)
    label = run_label(settings["output_dir"])
    shapefile_dirs = {name: base for name in layers} if settings.get("write_shapefile") else None
    gpkg_path, shapefiles = write_layers(layers, base / f"hidroweb_{label}.gpkg", settings.get("target_crs"),
                                         shapefile_dirs)
    inventory.to_frame(selection).to_csv(base / "available_data.csv", index=False)
    warnings_file = write_warnings(base, warnings)
    _write_readme(base / "README.txt", inventory, selection, client, index_rows, counts, periods, warnings,
                  skipped=len(inventory.entries) - len(entries), empty=empty)
    report(100.0, "HidroWeb extraction complete")
    return {
        "kind": "hidroweb",
        "station_count": len(layers[LAYER_NAMES[STREAMFLOW]]),
        "gauge_count": len(layers[LAYER_NAMES[RAIN]]),
        "downloaded": [(e["site_id"], e["row"], e["column"]) for e in downloaded],
        "excluded": len({e["site_id"] for e in entries} - {e["site_id"] for e in downloaded}),
        "gpkg": gpkg_path,
        "layers": [name for name, records in layers.items() if records],
        "shapefiles": shapefiles,
        "label": label,
        "output_dir": base,
        "summary": base / "hidroweb_index.csv",
        "warnings": warnings,
        "warnings_file": warnings_file,
    }


def _write_readme(path, inventory, selection, client, index_rows, counts, periods, warnings, skipped=0, empty=()):
    today = date.today().isoformat()
    service = (
        f"HidroWebService (ANA login), {API_URL}" if client.uses_api else f"ServiceANA (public), {LEGACY_URL}"
    )
    chosen = [row.label for row in ROWS if (row.key, "daily") in selection]
    lines = [
        "HidroWeb data (ANA, Brazilian national hydrometeorological network) retrieved with Weather Extractor",
        f"Data accessed: {today}",
        f"Service: {service}",
        f"Period: {inventory.start} through {inventory.end}",
        f"Search margin around the area of interest: {inventory.margin_km:g} km",
        "Selected variables: " + "; ".join(chosen),
        "",
        "=" * 79,
        "DATA INCLUDED IN THIS DOWNLOAD",
        "Stations with at least one value in the period; counts match the CSV files.",
        "=" * 79,
        "",
    ]
    table = []
    for row in ROWS:
        keys = [key for key in counts if key[1] == row.key]
        by_operator = Counter(inventory.sites[site_id]["org"] or "unknown" for site_id, _ in keys)
        for operator in sorted(by_operator):
            mine = [key for key in keys if (inventory.sites[key[0]]["org"] or "unknown") == operator]
            table.append([
                row.label, by_operator[operator], f"{sum(counts[key] for key in mine):,}",
                min(periods[key][0] for key in mine).strftime("%Y-%m-%d"),
                max(periods[key][1] for key in mine).strftime("%Y-%m-%d"), operator,
            ])
    lines += _text_table(["Variable", "Stations", "Days", "First", "Last", "Operator"], table, right=(1, 2))
    types = Counter(row["station_type"] for row in index_rows)
    lines += ["", "Totals: " + "; ".join(f"{count} {name.lower()}(s)" for name, count in sorted(types.items())) + "."]
    lines += [
        "",
        "Not included:",
        f"- {skipped} dataset(s) found by 'Check available data' but not downloaded (unchecked or "
        "outside the map selection); see available_data.csv.",
    ]
    if empty:
        lines.append(f"- {len(empty)} selected dataset(s) returned no values in the period:")
        for entry in empty[:30]:
            lines.append(f"    {entry['site_id']}  {ROWS_BY_KEY[entry['row']].label}")
        if len(empty) > 30:
            lines.append(f"    ... and {len(empty) - 30} more")
    else:
        lines.append("- Every selected dataset returned values.")
    lines += [
        "",
        "Files:",
        "- hidroweb_index.csv: one row per station: code, name, type, latitude and longitude (WGS 84), "
        "altitude, drainage area, river, municipality, state, sub-basin, operator, days with values per "
        "variable, first and last day.",
        "- daily/ana_<code>_<variable>.csv: date and daily value (discharge_m3s, stage_cm, precipitation_mm).",
        "- available_data.csv: every dataset found by 'Check available data' (selected = downloaded).",
        "- hidroweb_<folder>.gpkg: streamflow_stations and rain_gauges layers.",
        "",
        "Conventions:",
        f"- Daily files list every day of the period; {MISSING_VALUE} marks a day without a value.",
        "- One value per day: consisted data where ANA has them, raw data otherwise; daily means are "
        "used over single readings.",
        "- Units as reported by ANA: discharge m3/s, water level cm, precipitation mm.",
        "- Stations come from the ANA inventory: rain gauges by their code (whole degrees of latitude and "
        "longitude), streamflow stations by the code ranges of the sub-basins of the nearby rain gauges. "
        "Only stations whose "
        "inventory period overlaps the requested period are listed; that period is not always current "
        "for stations of other agencies, which then return no values (see Not included).",
    ]
    if warnings:
        lines += ["", f"Warnings: {len(warnings)}, listed in warnings.txt."]
    lines += [
        "",
        "Cite as: Agência Nacional de Águas e Saneamento Básico (ANA). HidroWeb, Sistema de Informações "
        f"Hidrológicas. https://www.snirh.gov.br/hidroweb/, accessed {today}.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
