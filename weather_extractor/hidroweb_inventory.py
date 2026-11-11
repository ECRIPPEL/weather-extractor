"""'Check available data' of the HidroWeb Data tab: ANA stations in the AOI, from the inventory only."""

from datetime import datetime
import math
import threading

import pandas as pd
import shapely

from .hidroweb_catalog import DAILY, LAYERS, ROWS, SITE_TYPES, STATION_TYPES
from .hidroweb_client import HidrowebClient, run_with_retries
from .usgs_inventory import _overlaps, search_area


# ANA stations lie in Brazil (a few on its borders).
BRAZIL = shapely.box(-74.5, -34.5, -28.5, 5.8)
# Rain gauge codes are LLOOnnn, from the whole degrees of latitude (S) and longitude (W)
# of the gauge, or 8LLOOnnn north of the equator. Gauges near a degree line may carry the
# neighbouring degree, so the searched cells reach this far beyond the area.
CELL_MARGIN_DEG = 0.25
NORTH_MARKER = 8_000_000
# Codes of one degree of latitude are contiguous across longitudes, so one request covers
# up to this many degrees of longitude (about 600 gauges in 3 s in southern Brazil).
MAX_ROW_DEGREES = 10
# Streamflow codes are SSnnnnnn, SS being the sub-basin; neighbouring sub-basins are asked
# together, up to this many per request (about 1,000 stations in 4 s).
SUB_BASIN_CODES = 1_000_000
MAX_SUB_BASINS = 10
# ServiceANA answers code ranges in seconds, while sub-basin and state filters often hit its
# database timeout. A sub-basin range it cannot answer is asked once more in this many pieces.
SPLIT_PIECES = 10
SMALLEST_RANGE = 1_000
WORKERS = 4


def rain_code_ranges(area):
    """(first, last) rain gauge codes around ``area``: one range per degree of latitude."""
    west, south, east, north = area.bounds
    min_lon, min_lat, max_lon, max_lat = BRAZIL.bounds
    west, south = max(west - CELL_MARGIN_DEG, min_lon), max(south - CELL_MARGIN_DEG, min_lat)
    east, north = min(east + CELL_MARGIN_DEG, max_lon), min(north + CELL_MARGIN_DEG, max_lat)
    # Cell [lon, lon + 1) west of Greenwich holds longitudes of -lon - 1 whole degrees.
    degrees = sorted(-lon - 1 for lon in range(math.floor(west), min(math.floor(east), -1) + 1))
    ranges = set()
    for lat in range(math.floor(south), math.floor(north) + 1):
        row = (lat if lat >= 0 else -lat - 1) * 100_000
        for first in range(0, len(degrees), MAX_ROW_DEGREES):
            group = degrees[first:first + MAX_ROW_DEGREES]
            low, high = row + group[0] * 1_000, row + group[-1] * 1_000 + 999
            ranges.add((low, high))
            if lat >= 0:
                ranges.add((NORTH_MARKER + low, NORTH_MARKER + high))
    return sorted(ranges)


def sub_basin_ranges(sub_basins):
    """(first, last) streamflow codes of the sub-basins, neighbours merged into one range."""
    ranges, run = [], []
    for code in sorted(set(sub_basins)):
        if run and (code != run[-1] + 1 or len(run) == MAX_SUB_BASINS):
            ranges.append((run[0] * SUB_BASIN_CODES, (run[-1] + 1) * SUB_BASIN_CODES - 1))
            run = []
        run.append(code)
    if run:
        ranges.append((run[0] * SUB_BASIN_CODES, (run[-1] + 1) * SUB_BASIN_CODES - 1))
    return ranges


def stations_in_range(client, first, last, kind):
    """Stations of one type with codes first..last.

    A wide range gets one try; if it fails, its pieces are asked (with retries) instead.
    """
    wide = last - first + 1 > SMALLEST_RANGE
    try:
        return client.inventory(codEstDE=first, codEstATE=last, tpEst=kind, attempts=1 if wide else 3)
    except InterruptedError:
        raise
    except Exception:
        if not wide:
            raise
    size = (last - first + 1) // SPLIT_PIECES
    records = []
    for start in range(first, last + 1, size):
        records += client.inventory(codEstDE=start, codEstATE=min(start + size - 1, last), tpEst=kind, attempts=3)
    return records


def _day(value):
    return (value or "")[:10] if value not in (None, "None") else ""


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _period(record, names):
    """First and last day of a variable from the inventory; '' as last day means still running."""
    pairs = [(_day(record.get(f"Periodo{name}Inicio")), _day(record.get(f"Periodo{name}Fim"))) for name in names]
    pairs = [(begin, end) for begin, end in pairs if begin]
    if not pairs:
        return None
    end = "" if any(not end for _, end in pairs) else max(end for _, end in pairs)
    return min(begin for begin, _ in pairs), end


class Inventory:
    """ANA stations and daily datasets available for one AOI, period, and search margin."""

    def __init__(self, start, end, margin_km, sites, entries, warnings, failed_queries=0):
        self.start = start
        self.end = end
        self.margin_km = margin_km
        self.sites = sites
        self.entries = entries
        self.warnings = warnings
        # Inventory queries ANA still refused after the retries: some stations may be missing.
        self.failed_queries = failed_queries
        self.checked_at = datetime.now().strftime("%H:%M")

    def count(self, row_key, column=DAILY):
        return len({entry["site_id"] for entry in self.entries if entry["row"] == row_key})

    def selected_entries(self, selection, site_ids=None):
        rows = {row for row, _column in selection}
        return [
            entry for entry in self.entries
            if entry["row"] in rows and (site_ids is None or entry["site_id"] in site_ids)
        ]

    def visible_sites(self, selection):
        visible = {layer: set() for layer in LAYERS}
        for entry in self.selected_entries(selection):
            visible[self.sites[entry["site_id"]]["layer"]].add(entry["site_id"])
        return visible

    def layer_sites(self, layer):
        return sorted(site_id for site_id, site in self.sites.items() if site["layer"] == layer)

    def describe(self, site_id, layer=None, entries=None):
        """'Discharge, Water level' for one site."""
        keys = {e["row"] for e in (entries if entries is not None else self.entries) if e["site_id"] == site_id}
        return ", ".join(row.label.split(" (")[0] for row in ROWS if row.key in keys)

    def period(self, site_id, entries=None):
        chosen = [e for e in (entries if entries is not None else self.entries) if e["site_id"] == site_id]
        begins = [e["begin"] for e in chosen if e["begin"]]
        ends = [e["end"] or self.end for e in chosen]
        return (min(begins) if begins else ""), (max(ends) if ends else "")

    def keep(self, keys):
        """Keep only the datasets in ``keys`` ((site_id, row, column)) and their sites."""
        self.entries = [e for e in self.entries if (e["site_id"], e["row"], e["column"]) in keys]
        used = {e["site_id"] for e in self.entries}
        self.sites = {site_id: site for site_id, site in self.sites.items() if site_id in used}

    def to_frame(self, selection=None):
        rows = {row for row, _column in selection} if selection is not None else set()
        return pd.DataFrame(
            [
                {
                    "site_id": entry["site_id"],
                    "code": self.sites[entry["site_id"]]["code"],
                    "site_name": self.sites[entry["site_id"]]["name"],
                    "station_type": self.sites[entry["site_id"]]["type"],
                    "variable": entry["row"],
                    "first_date": entry["begin"],
                    "last_date": entry["end"],
                    "selected": entry["row"] in rows,
                }
                for entry in self.entries
            ],
            columns=["site_id", "code", "site_name", "station_type", "variable", "first_date",
                     "last_date", "selected"],
        )


def _site(record, layer):
    code = record["Codigo"]
    return {
        "code": code,
        "name": record.get("Nome") or "",
        "type": SITE_TYPES[layer],
        "layer": layer,
        "org": record.get("OperadoraSigla") or "",
        "source_label": record.get("ResponsavelSigla") or "ANA",
        "responsible": record.get("ResponsavelSigla") or "",
        "river": record.get("RioNome") or "",
        "municipality": record.get("nmMunicipio") or "",
        "state": record.get("nmEstado") or "",
        "sub_basin": record.get("SubBaciaCodigo") or "",
        "drainage_km2": _number(record.get("AreaDrenagem")),
        "altitude_m": _number(record.get("Altitude")),
        "operating": "yes" if record.get("Operando") == "1" else "no",
        "lon": float(record["Longitude"]),
        "lat": float(record["Latitude"]),
    }


def check_availability(settings, geometry, progress=None, is_canceled=None, client=None):
    start, end = settings["start_date"], settings["end_date"]
    if start > end:
        raise ValueError("The start date must be on or before the end date.")
    margin_km = float(settings.get("margin_km", 0.0) or 0.0)
    client = client or HidrowebClient(is_canceled=is_canceled)
    area = search_area(geometry, margin_km)
    warnings = []
    if not area.intersects(BRAZIL):
        warnings.append("HidroWeb covers Brazil; the area of interest is outside it.")
        return Inventory(start, end, margin_km, {}, [], warnings)

    def report(percentage, message):
        if progress:
            progress(percentage, message)

    workers = max(1, int(settings.get("parallel_workers") or WORKERS))
    failed = []

    def fetch(ranges, kind, first, share, label):
        """Stations of each code range, in parallel; refused ranges are asked again later."""
        lock, done = threading.Lock(), [0]

        def run(job):
            records = stations_in_range(client, job[0], job[1], kind)
            with lock:
                done[0] += 1
                count = done[0]
            report(first + share * count / max(len(ranges), 1), f"Checking ANA {label}: {count} of {len(ranges)} queries")
            return records

        def on_retry(count, pause):
            report(first + share * done[0] / max(len(ranges), 1),
                   f"ANA did not answer {count} {label} queries; asking again in {pause} s")

        results, still_failed, errors = run_with_retries(ranges, run, workers, client.wait, on_retry)
        for a, b in still_failed:
            warnings.append(f"ANA inventory of {label} with codes {a} to {b} failed: {errors[(a, b)]}")
        failed.extend(still_failed)
        return [record for records in results for record in records]

    ranges = rain_code_ranges(area)
    rain = fetch(ranges, 2, 0.0, 60.0, "rain gauges")
    # Streamflow codes do not follow coordinates: search the sub-basins of the nearby rain gauges.
    sub_basins = sorted({int(r["SubBaciaCodigo"]) for r in rain if (r.get("SubBaciaCodigo") or "").isdigit()})
    streamflow = fetch(sub_basin_ranges(sub_basins), 1, 60.0, 40.0, "streamflow stations")
    if ranges and len(failed) >= len(ranges):
        raise RuntimeError("The ANA inventory could not be read. " + warnings[0])

    sites, entries = {}, []
    for record in rain + streamflow:
        layer = STATION_TYPES.get(record.get("TipoEstacao"))
        try:
            lon, lat = float(record["Longitude"]), float(record["Latitude"])
        except (KeyError, TypeError, ValueError):
            continue
        site_id = f"ANA-{record.get('Codigo')}"
        if layer is None or not record.get("Codigo") or site_id in sites or not shapely.intersects_xy(area, lon, lat):
            continue
        found = []
        for row in ROWS:
            period = _period(record, row.periods)
            if period and _overlaps(period[0], period[1], start, end):
                found.append({"site_id": site_id, "row": row.key, "column": DAILY,
                              "begin": period[0], "end": period[1]})
        if found:
            sites[site_id] = _site(record, layer)
            entries += found
    return Inventory(start, end, margin_km, sites, entries, warnings, len(failed))
