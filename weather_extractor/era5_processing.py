"""ERA5-Land hourly reanalysis (ECMWF, Copernicus) at stations, through Google Earth Engine.

Earth Engine only samples the ERA5-Land cells of the stations, hour by hour. The values are
then converted and written like the AORC ones: daily and hourly CSV tables, SWAT+ climate
files, and FAO-56 reference ET, on the same station layouts (grid, blocks, AOI centroid,
or the cells of rain gauges).
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
import math
from pathlib import Path
import threading
from types import SimpleNamespace

import numpy as np
import pandas as pd

from .aorc_processing import (
    EXTRA_VARIABLES,
    SOIL_COLUMNS,
    VARIABLE_LABELS,
    VARIABLE_ORDER,
    WIND_10M_TO_2M,
    OutputWriter,
    build_layout,
    has_fixed_offset,
    output_timesteps,
    run_label,
    sample_elevations,
    write_station_locations,
    write_warnings,
)
from .et_processing import _with_retries, connect


COLLECTION = "ECMWF/ERA5_LAND/HOURLY"
FIRST_YEAR = 1950
PREFIX = "era5"
SOURCE = "ECMWF ERA5-Land"
VARIABLES = VARIABLE_ORDER + EXTRA_VARIABLES
# Earth Engine bands read for each variable.
BANDS = {
    "pcp": ["total_precipitation_hourly"],
    "tmp": ["temperature_2m"],
    "slr": ["surface_solar_radiation_downwards_hourly"],
    "hmd": ["temperature_2m", "dewpoint_temperature_2m"],
    "wnd": ["u_component_of_wind_10m", "v_component_of_wind_10m"],
    "eto": [
        "temperature_2m", "dewpoint_temperature_2m", "surface_solar_radiation_downwards_hourly",
        "u_component_of_wind_10m", "v_component_of_wind_10m", "surface_pressure",
    ],
    "evap": ["total_evaporation_hourly"],
    "runoff": ["runoff_hourly"],
    "soil": [f"volumetric_soil_water_layer_{layer}" for layer in range(1, 5)],
}
# Totals over the hour that ends at the image time; the other bands are values at that time.
ACCUMULATED = {
    "total_precipitation_hourly", "surface_solar_radiation_downwards_hourly",
    "total_evaporation_hourly", "runoff_hourly",
}
# Earth Engine getRegion returns at most 1,048,576 values per request.
MAX_VALUES = 800_000
HEADER_COLUMNS = 4  # id, longitude, latitude, time
GRID_MARGIN_DEG = 0.2
YEAR_WORKERS = 4
HOUR = pd.Timedelta(hours=1)
GRID_DESCRIPTIONS = {
    "original": "original ERA5-Land grid, one station per 0.1 degree (~9-11 km) cell",
    "single": "single station, the ERA5-Land cell nearest to the AOI centroid",
}


# -- Grid ----------------------------------------------------------------------


class Grid:
    """The ERA5-Land cells of Earth Engine around an area, from the image transform.

    ``transform`` is [x scale, x shear, x origin, y shear, y scale, y origin] in degrees.
    """

    def __init__(self, transform, bounds, margin=GRID_MARGIN_DEG):
        self.transform = transform
        x_scale, _, x_origin, _, y_scale, y_origin = transform[:6]
        west, south, east, north = bounds
        self.cols = np.arange(math.floor((west - margin - x_origin) / x_scale),
                              math.floor((east + margin - x_origin) / x_scale) + 1)
        # y_scale is negative: rows grow southwards.
        self.rows = np.arange(math.floor((north + margin - y_origin) / y_scale),
                              math.floor((south - margin - y_origin) / y_scale) + 1)
        self.longitude = x_origin + (self.cols + 0.5) * x_scale
        self.latitude = y_origin + (self.rows + 0.5) * y_scale
        self.step = abs(x_scale)

    def axes(self):
        """The latitude/longitude axes that build_layout reads."""
        return SimpleNamespace(latitude=SimpleNamespace(values=self.latitude),
                               longitude=SimpleNamespace(values=self.longitude))

    def cells(self, lons, lats):
        """(row, column) of the cells holding each point."""
        x_scale, _, x_origin, _, y_scale, y_origin = self.transform[:6]
        rows = np.floor((np.asarray(lats, dtype=float) - y_origin) / y_scale).astype(int)
        cols = np.floor((np.asarray(lons, dtype=float) - x_origin) / x_scale).astype(int)
        return rows, cols


# -- Earth Engine requests -------------------------------------------------------


def grid_projection(ee):
    """(crs, transform) of the ERA5-Land images."""
    projection = ee.ImageCollection(COLLECTION).first().select("temperature_2m").projection().getInfo()
    return projection["crs"], projection["transform"]


def _ms(timestamp):
    """Milliseconds since 1970 (UTC), the time format of Earth Engine."""
    return int(timestamp.value // 1_000_000)


def region_table(ee, crs, transform, points, start, end, bands):
    """getRegion table (header row first) of the hourly images in [start, end) at ``points``."""
    collection = ee.ImageCollection(COLLECTION).filterDate(_ms(start), _ms(end)).select(bands)
    geometry = ee.Geometry.MultiPoint([[float(lon), float(lat)] for lon, lat in points])
    return collection.getRegion(geometry, None, crs, transform).getInfo()


def request_plan(cell_count, band_count, start, end):
    """(first cell, last cell, start, end) requests that each stay under MAX_VALUES values."""
    columns = HEADER_COLUMNS + band_count
    cell_hours = MAX_VALUES // columns
    hours = int((end - start) / HOUR)
    # Many cells: groups of cells, at least about a month of hours per request.
    group = max(1, min(cell_count, cell_hours // (24 * 31)))
    step = max(24, min(hours, cell_hours // group))
    plan = []
    for first in range(0, cell_count, group):
        last = min(first + group, cell_count)
        window = start
        while window < end:
            stop = min(window + step * HOUR, end)
            plan.append((first, last, window, stop))
            window = stop
    return plan


# -- Hourly values -----------------------------------------------------------------


def place(table, grid, cell_index, year_start, hours, bands):
    """Band arrays (hours, cells) from a getRegion table; totals go to the hour they end."""
    header, rows = table[0], table[1:]
    values = {band: np.full((hours, len(cell_index)), np.nan) for band in bands}
    if not rows:
        return values
    frame = pd.DataFrame(rows, columns=header)
    grid_rows, grid_cols = grid.cells(frame["longitude"], frame["latitude"])
    cells = np.array([cell_index.get(key, -1) for key in zip(grid_rows, grid_cols)])
    first_ms = _ms(year_start)
    hour = ((pd.to_numeric(frame["time"]).to_numpy(dtype="int64") - first_ms) // 3_600_000).astype(int)
    for band in bands:
        index = hour - 1 if band in ACCUMULATED else hour
        data = pd.to_numeric(frame[band], errors="coerce").to_numpy(dtype=float)
        keep = (cells >= 0) & (index >= 0) & (index < hours) & np.isfinite(data)
        values[band][index[keep], cells[keep]] = data[keep]
    return values


def _vapor_pressure(temp_c):
    return 0.6108 * np.exp(17.27 * temp_c / (temp_c + 237.3))  # FAO-56 Eq. 11 and 14


def hourly_fields(raw, variables):
    """Hourly fields of the AORC pipeline (pcp_mm, temp_c, slr_wm2, ...) from ERA5-Land bands."""
    fields = {}
    temp_c = raw["temperature_2m"] - 273.15 if "temperature_2m" in raw else None
    if "dewpoint_temperature_2m" in raw:
        ea_kpa = _vapor_pressure(raw["dewpoint_temperature_2m"] - 273.15)
    if "pcp" in variables:
        fields["pcp_mm"] = np.clip(raw["total_precipitation_hourly"] * 1000.0, 0.0, None)
    if "tmp" in variables:
        fields["temp_c"] = temp_c
    if "slr" in variables or "eto" in variables:
        fields["slr_wm2"] = np.clip(raw["surface_solar_radiation_downwards_hourly"] / 3600.0, 0.0, None)
    if "hmd" in variables:
        fields["rh_fraction"] = np.clip(ea_kpa / _vapor_pressure(temp_c), 0.0, 1.0)
    if "wnd" in variables:
        fields["wind_ms"] = np.hypot(raw["u_component_of_wind_10m"], raw["v_component_of_wind_10m"])
    if "eto" in variables:
        fields["temp_c"] = temp_c
        fields["ea_kpa"] = ea_kpa
        fields["pres_kpa"] = raw["surface_pressure"] / 1000.0
        fields["wind2_ms"] = WIND_10M_TO_2M * np.hypot(raw["u_component_of_wind_10m"], raw["v_component_of_wind_10m"])
    if "evap" in variables:
        # ECMWF counts upward water fluxes as negative; evaporation is written as a positive depth.
        fields["evap_mm"] = -raw["total_evaporation_hourly"] * 1000.0
    if "runoff" in variables:
        fields["runoff_mm"] = np.clip(raw["runoff_hourly"] * 1000.0, 0.0, None)
    if "soil" in variables:
        for layer, column in enumerate(SOIL_COLUMNS, start=1):
            fields[column] = raw[f"volumetric_soil_water_layer_{layer}"]
    return fields


def station_means(values, cell_of_window, labels, station_count):
    """Mean of the cells of each station: (hours, cells) -> (hours, stations)."""
    data = values[:, cell_of_window]
    order = np.argsort(labels, kind="stable")
    sorted_labels = np.asarray(labels)[order]
    starts = np.flatnonzero(np.r_[True, np.diff(sorted_labels) != 0])
    data = data[:, order]
    valid = np.isfinite(data)
    sums = np.add.reduceat(np.where(valid, data, 0.0), starts, axis=1)
    counts = np.add.reduceat(valid.astype(np.int32), starts, axis=1)
    means = np.full((values.shape[0], station_count), np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        means[:, sorted_labels[starts]] = np.where(counts > 0, sums / np.maximum(counts, 1), np.nan)
    return means


# -- Extraction ------------------------------------------------------------------------


def describe_grid(settings):
    mode = settings["grid_mode"]
    if mode == "gauges":
        return (f"rain gauges, the ERA5-Land cell nearest to each of the {len(settings.get('gauges') or [])} "
                "gauges selected in the USGS and HidroWeb Data tabs")
    if mode == "block":
        return f"{float(settings['block_km']):g} km blocks, each station is the mean of the ERA5-Land cells inside its block"
    return GRID_DESCRIPTIONS[mode]


def extract_era5(settings, aoi_geometry, progress=None, is_canceled=None, ee_module=None,
                 fetcher=None, projection=None):
    """Write ERA5-Land station series. ``fetcher(points, start, end, bands)`` returns a
    getRegion table and ``projection`` is (crs, transform); both replace Earth Engine in tests."""
    start_year, end_year = int(settings["start_year"]), int(settings["end_year"])
    if start_year > end_year:
        raise ValueError("The start year must be less than or equal to the end year.")
    output_dir = Path(settings["output_dir"])
    is_canceled = is_canceled or (lambda: False)
    timezone = settings.get("timezone", "Etc/GMT+6")
    timesteps, swat_pcp_step = output_timesteps(settings)
    variables = [variable for variable in VARIABLES if variable in settings["variables"]]
    if not variables:
        raise ValueError("Select at least one climate variable.")
    if settings["write_csv"] and not timesteps:
        raise ValueError("Choose the daily or the hourly time step, or both.")
    years = list(range(start_year, end_year + 1))
    if swat_pcp_step == "hourly" and settings["write_swat"] and "pcp" in variables and not has_fixed_offset(timezone, years):
        raise ValueError(
            "Hourly SWAT+ precipitation needs exactly 24 values per day. Choose a "
            "fixed-offset timezone (GMT-x, no daylight saving time)."
        )

    def report(percentage, message):
        if progress:
            progress(percentage, message)

    if fetcher is None:
        ee = ee_module
        connect(ee, settings["project"], report)
        report(4.0, "Reading the ERA5-Land grid from Earth Engine...")
        crs, transform = _with_retries(lambda: grid_projection(ee), is_canceled)

        def fetcher(points, start, end, bands):
            return region_table(ee, crs, transform, points, start, end, bands)
    else:
        crs, transform = projection

    gauges = settings.get("gauges") or []
    bounds = aoi_geometry.bounds
    if settings["grid_mode"] == "gauges" and gauges:
        lons = [g["lon"] for g in gauges] + [bounds[0], bounds[2]]
        lats = [g["lat"] for g in gauges] + [bounds[1], bounds[3]]
        bounds = (min(lons), min(lats), max(lons), max(lats))
    grid = Grid(transform, bounds)
    window, stations, labels = build_layout(
        grid.axes(), aoi_geometry, settings["grid_mode"], settings.get("block_km"), gauges,
        step=grid.step, prefix=PREFIX,
    )
    labels = np.asarray(labels)
    # Cells to sample: each one once, even when gauges share it.
    keys = list(dict.fromkeys(zip(grid.rows[window.lat_indices], grid.cols[window.lon_indices])))
    cell_index = {key: index for index, key in enumerate(keys)}
    cell_of_window = np.array([cell_index[key] for key in zip(grid.rows[window.lat_indices],
                                                               grid.cols[window.lon_indices])])
    lookup = {(row, col): (lon, lat) for row, lat in zip(grid.rows, grid.latitude)
              for col, lon in zip(grid.cols, grid.longitude) if (row, col) in cell_index}
    points = [lookup[key] for key in keys]
    bands = list(dict.fromkeys(band for variable in variables for band in BANDS[variable]))

    elevations, uncovered = sample_elevations(stations, settings.get("dem_path", ""))
    for station, elevation in zip(stations, elevations):
        station["elev_m"] = elevation
    warnings = []
    skipped = len(gauges) - len(stations)
    if settings["grid_mode"] == "gauges" and skipped:
        warnings.append(f"{skipped} rain gauge(s) lie outside the ERA5-Land grid and were left out.")
    if uncovered:
        warnings.append(f"The elevation raster has no value at {len(uncovered)} station(s); their elevation was written as -99.")

    output_dir.mkdir(parents=True, exist_ok=True)
    locations_path, gpkg_path = write_station_locations(
        stations, output_dir, settings.get("target_crs"), prefix=PREFIX, step=grid.step
    )
    writer = OutputWriter(stations, output_dir, settings, variables, timezone, source=SOURCE)
    writer.start()

    # Each local year runs from its first to its last local hour, in UTC.
    spans = {}
    for year in years:
        start = pd.Timestamp(year, 1, 1).tz_localize(timezone).tz_convert("UTC")
        end = pd.Timestamp(year + 1, 1, 1).tz_localize(timezone).tz_convert("UTC")
        spans[year] = (start, end)
    # Images from the first hour to one hour after the last: the last total ends then.
    jobs = [(year, first, last, window_start, window_end)
            for year in years
            for first, last, window_start, window_end in request_plan(
                len(points), len(bands), spans[year][0], spans[year][1] + HOUR)]
    lock, done = threading.Lock(), [0]
    message = "Reading ERA5-Land from Earth Engine: {} of {} requests"
    report(5.0, message.format(0, len(jobs)))

    def run(job):
        year, first, last, window_start, window_end = job
        table = _with_retries(lambda: fetcher(points[first:last], window_start, window_end, bands), is_canceled)
        with lock:
            done[0] += 1
            count = done[0]
        report(5.0 + 90.0 * count / len(jobs), message.format(count, len(jobs)))
        return job, table

    missing_hours = 0
    workers = max(1, int(settings.get("parallel_workers") or YEAR_WORKERS))
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        results = pool.map(run, jobs)
        current, raw = None, None
        for (year, _, _, _, _), table in list(results) + [((None,) * 5, None)]:
            if year != current and current is not None:
                missing_hours += _write_year(writer, current, spans[current], raw, variables, cell_of_window, labels,
                                             len(stations))
            if year is None:
                break
            if year != current:
                current = year
                hours = int((spans[year][1] - spans[year][0]) / HOUR)
                raw = {band: np.full((hours, len(points)), np.nan) for band in bands}
            placed = place(table, grid, cell_index, spans[year][0], raw[bands[0]].shape[0], bands)
            for band in bands:
                filled = np.isfinite(placed[band])
                raw[band][filled] = placed[band][filled]
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    if missing_hours:
        warnings.append(
            f"{missing_hours} station-hour(s) have no ERA5-Land value (cells over water, or hours not "
            "published yet); they are blank in the CSV files and -99 in the SWAT+ files."
        )

    report(97.0, "Writing statistics and README...")
    summary_path = writer.finish()
    manifest = output_dir / "README.txt"
    _write_readme(manifest, settings, variables, timesteps, swat_pcp_step, timezone, stations, warnings, grid)
    warnings_file = write_warnings(output_dir, warnings)
    report(100.0, "Extraction complete")
    return {
        "dataset": "era5land",
        "station_count": len(stations),
        "summary": summary_path,
        "locations": locations_path,
        "gpkg": gpkg_path,
        "label": run_label(output_dir),
        "manifest": manifest,
        "output_dir": output_dir,
        "warnings": warnings,
        "warnings_file": warnings_file,
    }


def _write_year(writer, year, span, raw, variables, cell_of_window, labels, station_count):
    """Convert one local year of cell values to station series and append them; returns the
    number of station-hours without a value."""
    start, end = span
    hours = pd.date_range(start, end, freq="h", inclusive="left")
    fields = hourly_fields(raw, variables)
    series = {name: station_means(values, cell_of_window, labels, station_count) for name, values in fields.items()}
    writer.write_year(hours, series)
    first = next(iter(series.values()))
    return int(np.isnan(first).sum())


def _write_readme(path, settings, variables, timesteps, swat_pcp_step, timezone, stations, warnings, grid):
    today = date.today().isoformat()
    lines = [
        "ECMWF ERA5-Land hourly reanalysis (Copernicus Climate Change Service), retrieved with Weather Extractor",
        f"Source: Google Earth Engine collection {COLLECTION}",
        f"Data accessed: {today}",
        f"Requested local dates: {settings['start_year']}-01-01 through {settings['end_year']}-12-31",
        f"Timezone: {timezone}",
        f"CSV time steps: {', '.join(timesteps) if settings['write_csv'] else 'none (no CSV files)'}",
        f"Grid: {describe_grid(settings)} (cell size {grid.step:g} degree)",
        f"Stations: {len(stations)}",
        "Variables: " + ", ".join(VARIABLE_LABELS[variable] for variable in variables),
    ]
    if settings["write_swat"]:
        lines.append(f"SWAT+ precipitation time step: {swat_pcp_step}")
    lines += [
        "",
        "Conventions:",
        "- Precipitation, solar radiation, evaporation, and runoff are ERA5-Land totals over the hour "
        "ending at the image time; they are assigned to that hour. Temperature, dewpoint, pressure, "
        "wind, and soil moisture are values at the image time.",
        "- Hourly rows are labeled by the beginning of the hour in local time; daily values cover local "
        "civil days (sums for precipitation, radiation, evaporation, and runoff; maximum and minimum "
        "temperature from hourly values; means for humidity, wind, and soil moisture).",
        "- Units: temperature K -> degC; precipitation, evaporation, and runoff m -> mm; solar radiation "
        "J/m2 -> MJ/m2; relative humidity from the 2 m dewpoint and temperature (FAO-56 Eq. 11 and 14), "
        "bounded to [0, 1]; wind speed from the 10 m U/V components.",
        "- evap_mm is ERA5-Land total evaporation, positive for water leaving the surface (ECMWF stores it "
        "negative); runoff_mm is total runoff (surface and subsurface); soil_water_l1 to l4 are volumetric "
        "soil water (m3/m3) of the layers 0-7, 7-28, 28-100, and 100-289 cm.",
        "- Reference ET (eto_mm) is the FAO-56 Penman-Monteith grass reference ET (Allen et al., 1998), "
        "computed as for AORC: daily Eq. 6, hourly Eq. 53, wind at 2 m from 10 m (Eq. 47).",
        "- Block and single stations use the mean of the values of their cells. ERA5-Land covers land "
        "only: cells over the sea have no values.",
        "- Missing SWAT+ values and elevations use -99. SWAT+ files hold precipitation, temperature, "
        "radiation, humidity, wind, and ETo; evaporation, runoff, and soil moisture are in the CSV files only.",
    ]
    if warnings:
        lines += ["", f"Warnings: {len(warnings)}, listed in warnings.txt."]
    lines += [
        "",
        "Cite as: Muñoz Sabater, J. (2019). ERA5-Land hourly data from 1950 to present. Copernicus Climate "
        "Change Service (C3S) Climate Data Store (CDS). https://doi.org/10.24381/cds.e2161bac; "
        "Muñoz-Sabater, J., et al. (2021). ERA5-Land: a state-of-the-art global reanalysis dataset for land "
        "applications. Earth System Science Data 13, 4349-4383. https://doi.org/10.5194/essd-13-4349-2021",
        f"Generated using Copernicus Climate Change Service information {date.today().year}. Neither the "
        "European Commission nor ECMWF is responsible for any use of this information.",
        "These are derived values, not original ERA5-Land data.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
