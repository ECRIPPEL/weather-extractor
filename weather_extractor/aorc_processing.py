"""AORC download, spatial and temporal aggregation, and output writers."""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import math
from pathlib import Path
import re
from threading import Event, Lock

import numpy as np
import pandas as pd
import shapely

from .usgs_catalog import site_stem


AORC_BUCKET = "noaa-nws-aorc-v1-1-1km"
# The bucket is public, so plain HTTPS reads it (no AWS libraries needed).
AORC_URL = f"https://{AORC_BUCKET}.s3.amazonaws.com"
GRID_STEP_DEG = 1.0 / 120.0
KM_PER_DEGREE = 111.195
TIMEZONE = "Etc/GMT+6"
MISSING = -99.0
# Zarr chunk shape (time, latitude, longitude) of the AORC v1.1 archive.
ARCHIVE_CHUNKS = (144, 128, 256)
# Values held per AORC variable in one read before more time chunks are added.
MAX_VALUES_PER_READ = 8_000_000
# At most this many time chunks (6 x 144 h, about 36 days) per read, so the
# progress bar advances several times per year.
MAX_TIME_CHUNKS_PER_READ = 6
# Edge blocks with less than this fraction of a full block join a neighbor.
SMALL_BLOCK_FRACTION = 0.25

VARIABLE_ORDER = ["pcp", "tmp", "slr", "hmd", "wnd", "eto"]
VARIABLE_LABELS = {
    "pcp": "precipitation",
    "tmp": "temperature",
    "slr": "solar radiation",
    "hmd": "relative humidity",
    "wnd": "wind speed",
    "eto": "reference evapotranspiration (FAO-56)",
}
VARIABLE_SOURCES = {
    "pcp": ["APCP_surface"],
    "tmp": ["TMP_2maboveground"],
    "slr": ["DSWRF_surface"],
    "hmd": ["SPFH_2maboveground", "PRES_surface", "TMP_2maboveground"],
    "wnd": ["UGRD_10maboveground", "VGRD_10maboveground"],
    "eto": [
        "TMP_2maboveground", "SPFH_2maboveground", "PRES_surface", "DSWRF_surface",
        "UGRD_10maboveground", "VGRD_10maboveground",
    ],
}
HOURLY_COLUMNS = {
    "pcp": ["pcp_mm"],
    "tmp": ["temp_c"],
    "slr": ["slr_mj_m2"],
    "hmd": ["rh_fraction"],
    "wnd": ["wind_ms"],
    "eto": ["eto_mm"],
}
DAILY_COLUMNS = {
    "pcp": ["pcp_mm"],
    "tmp": ["tmax_c", "tmin_c"],
    "slr": ["slr_mj_m2_day"],
    "hmd": ["rh_fraction"],
    "wnd": ["wind_ms"],
    "eto": ["eto_mm"],
}
STEP_COLUMNS = {"daily": DAILY_COLUMNS, "hourly": HOURLY_COLUMNS}
TIMESTEPS = ["daily", "hourly"]
# SWAT+ file extension per variable (the SWAT+ name of reference ET is "pet").
SWAT_EXTENSIONS = {"pcp": "pcp", "tmp": "tmp", "slr": "slr", "hmd": "hmd", "wnd": "wnd", "eto": "pet"}
# Hourly fields needed to compute reference ET.
ETO_FIELDS = ["temp_c", "slr_wm2", "ea_kpa", "pres_kpa", "wind2_ms"]
# FAO-56 Eq. 47: wind speed at 2 m from wind speed at 10 m.
WIND_10M_TO_2M = 4.87 / math.log(67.8 * 10 - 5.42)


def open_aorc_year(year):
    import xarray as xr

    return xr.open_zarr(f"{AORC_URL}/{year}.zarr", consolidated=True, chunks=None)


def archive_variables(variables):
    names = []
    for variable in variables:
        for name in VARIABLE_SOURCES[variable]:
            if name not in names:
                names.append(name)
    return names


def output_timesteps(settings):
    """CSV time steps of a run (daily, hourly, or both) and its SWAT+ precipitation step."""
    steps = settings.get("timesteps", ["daily"])
    return [step for step in TIMESTEPS if step in steps], settings.get("swat_pcp_step", "daily")


def has_fixed_offset(timezone, years):
    """Return True when the timezone never changes its UTC offset in ``years``."""
    offsets = set()
    for year in years:
        for month in (1, 4, 7, 10):
            offsets.add(pd.Timestamp(year, month, 15).tz_localize(timezone).utcoffset())
    return len(offsets) == 1


# ---------------------------------------------------------------------------
# Grid and station layout
# ---------------------------------------------------------------------------


class GridWindow:
    """AORC cells used by the stations, as archive row/column indices."""

    def __init__(self, lat_indices, lon_indices, lons, lats):
        self.lat_indices = np.asarray(lat_indices)
        self.lon_indices = np.asarray(lon_indices)
        self.lons = np.asarray(lons)
        self.lats = np.asarray(lats)


OUTSIDE_GRID_MESSAGE = (
    "The AOI is outside the AORC grid (contiguous United States and contributing "
    "areas). Check the AOI location and coordinate reference system."
)


def grid_window(dataset, geometry):
    """Return every AORC cell whose center is inside ``geometry``."""
    latitudes = dataset.latitude.values
    longitudes = dataset.longitude.values
    min_lon, min_lat, max_lon, max_lat = geometry.bounds
    lat_indices = np.flatnonzero((latitudes >= min_lat) & (latitudes <= max_lat))
    lon_indices = np.flatnonzero((longitudes >= min_lon) & (longitudes <= max_lon))
    if len(lat_indices) and len(lon_indices):
        lon_grid, lat_grid = np.meshgrid(longitudes[lon_indices], latitudes[lat_indices])
        inside = shapely.contains_xy(geometry, lon_grid, lat_grid)
        if np.any(inside):
            rows, cols = np.nonzero(inside)
            return GridWindow(
                lat_indices[rows], lon_indices[cols], lon_grid[inside], lat_grid[inside]
            )

    if (
        max_lat < latitudes.min()
        or min_lat > latitudes.max()
        or max_lon < longitudes.min()
        or min_lon > longitudes.max()
    ):
        raise ValueError(OUTSIDE_GRID_MESSAGE)
    raise ValueError(
        "No AORC grid-cell centers fall inside the AOI. Enlarge the AOI, choose "
        "'Single station (AOI centroid)', or check its coordinate reference system."
    )


def centroid_window(dataset, geometry):
    """Return the AORC cell nearest to the AOI centroid."""
    latitudes = dataset.latitude.values
    longitudes = dataset.longitude.values
    centroid = geometry.centroid
    half = GRID_STEP_DEG / 2.0
    if not (
        latitudes.min() - half <= centroid.y <= latitudes.max() + half
        and longitudes.min() - half <= centroid.x <= longitudes.max() + half
    ):
        raise ValueError(OUTSIDE_GRID_MESSAGE)
    lat_index = int(np.argmin(np.abs(latitudes - centroid.y)))
    lon_index = int(np.argmin(np.abs(longitudes - centroid.x)))
    return GridWindow(
        [lat_index], [lon_index], [longitudes[lon_index]], [latitudes[lat_index]]
    )


def gauge_window(dataset, gauges):
    """The AORC cell nearest to each gauge; gauges off the grid are left out.

    Gauges in the same cell share it, so a cell can appear more than once.
    """
    latitudes = dataset.latitude.values
    longitudes = dataset.longitude.values
    half = GRID_STEP_DEG / 2.0
    kept, lat_indices, lon_indices = [], [], []
    for gauge in gauges:
        lon, lat = float(gauge["lon"]), float(gauge["lat"])
        if not (
            latitudes.min() - half <= lat <= latitudes.max() + half
            and longitudes.min() - half <= lon <= longitudes.max() + half
        ):
            continue
        kept.append(gauge)
        lat_indices.append(int(np.argmin(np.abs(latitudes - lat))))
        lon_indices.append(int(np.argmin(np.abs(longitudes - lon))))
    if not kept:
        raise ValueError(
            "None of the rain gauges selected in the Observed Data tab lies on the AORC grid."
        )
    return GridWindow(lat_indices, lon_indices, longitudes[lon_indices], latitudes[lat_indices]), kept


def gauge_station_name(site_id):
    return "aorc_" + site_stem(site_id)


def _location_in(geometry):
    centroid = geometry.centroid
    if geometry.contains(centroid):
        return centroid
    return geometry.point_on_surface()


def _station_name(prefix, index, count):
    return f"{prefix}{index:0{max(3, len(str(count)))}d}"


def build_layout(dataset, geometry, grid_mode, block_km=None, gauges=None):
    """Group AOI cells into stations.

    Returns the AORC cells used, the station records, and one station index per
    cell (``labels``). The "gauges" mode makes one station per rain gauge
    (``gauges``: site_id, lon, lat), on the AORC cell nearest to it.
    """
    half = GRID_STEP_DEG / 2.0
    if grid_mode == "gauges":
        window, kept = gauge_window(dataset, gauges or [])
        stations = [
            {
                "station": gauge_station_name(gauge["site_id"]),
                "site_id": gauge["site_id"],
                "lon": float(lon),
                "lat": float(lat),
                "cells": 1,
                "area": shapely.box(lon - half, lat - half, lon + half, lat + half),
            }
            for gauge, lon, lat in zip(kept, window.lons, window.lats)
        ]
        return window, stations, np.arange(len(stations))
    if grid_mode == "single":
        window = centroid_window(dataset, geometry)
        lon, lat = float(window.lons[0]), float(window.lats[0])
        stations = [
            {
                "station": "aorc_centroid",
                "lon": lon,
                "lat": lat,
                "cells": 1,
                "area": shapely.box(lon - half, lat - half, lon + half, lat + half),
            }
        ]
        return window, stations, np.zeros(1, dtype=int)

    window = grid_window(dataset, geometry)
    cell_count = len(window.lons)

    if grid_mode == "original":
        labels = np.arange(cell_count)
        stations = [
            {
                "station": _station_name("aorc_", index + 1, cell_count),
                "lon": float(lon),
                "lat": float(lat),
                "cells": 1,
                "area": shapely.box(lon - half, lat - half, lon + half, lat + half),
            }
            for index, (lon, lat) in enumerate(zip(window.lons, window.lats))
        ]
        return window, stations, labels

    if grid_mode != "block":
        raise ValueError(f"Unknown grid mode: {grid_mode}")
    block_km = float(block_km)
    if block_km <= 0:
        raise ValueError("The grid cell size must be greater than zero.")
    min_lon, min_lat, _, _ = geometry.bounds
    block_lat = block_km / KM_PER_DEGREE
    block_lon = block_lat / max(math.cos(math.radians(geometry.centroid.y)), 0.01)
    block_rows = np.floor((window.lats - min_lat) / block_lat).astype(int)
    block_cols = np.floor((window.lons - min_lon) / block_lon).astype(int)
    keys, labels = np.unique(
        np.column_stack((-block_rows, block_cols)), axis=0, return_inverse=True
    )
    labels = np.asarray(labels).reshape(-1)
    full_block_cells = (block_lat / GRID_STEP_DEG) * (block_lon / GRID_STEP_DEG)
    groups = _merge_small_blocks(keys, labels, SMALL_BLOCK_FRACTION * full_block_cells)
    size_text = f"{block_km:g}".replace(".", "p")
    stations = []
    for index, members_of_group in enumerate(groups):
        boxes = [
            shapely.box(
                min_lon + column * block_lon,
                min_lat + -negative_row * block_lat,
                min_lon + (column + 1) * block_lon,
                min_lat + (-negative_row + 1) * block_lat,
            )
            for negative_row, column in keys[members_of_group]
        ]
        block = shapely.union_all(boxes)
        area = block.intersection(geometry)
        members = np.isin(labels, members_of_group)
        if area.is_empty or area.area == 0:
            area = block
            location = shapely.Point(
                window.lons[members].mean(), window.lats[members].mean()
            )
        else:
            location = _location_in(area)
        stations.append(
            {
                "station": _station_name(f"aorc_{size_text}km_", index + 1, len(groups)),
                "lon": float(location.x),
                "lat": float(location.y),
                "cells": int(members.sum()),
                "area": area,
            }
        )
    group_of_block = np.empty(len(keys), dtype=int)
    for index, members_of_group in enumerate(groups):
        group_of_block[members_of_group] = index
    return window, stations, group_of_block[labels]


def _merge_small_blocks(keys, labels, min_cells):
    """Merge blocks with fewer than ``min_cells`` AOI cells into a neighbor.

    ``keys`` holds (-row, column) per block. Returns lists of block indices,
    one per resulting station, in north-to-south, west-to-east order.
    """
    position = {(int(-row), int(column)): index for index, (row, column) in enumerate(keys)}
    parent = list(range(len(keys)))
    size = np.bincount(labels, minlength=len(keys)).astype(float)

    def root(index):
        while parent[index] != index:
            index = parent[index]
        return index

    for index in np.argsort(size, kind="stable"):
        if root(index) != index or size[index] >= min_cells:
            continue
        row, column = int(-keys[index][0]), int(keys[index][1])
        neighbors = {
            root(position[cell])
            for cell in ((row + 1, column), (row - 1, column), (row, column + 1), (row, column - 1))
            if cell in position
        } - {index}
        if neighbors:
            target = max(neighbors, key=lambda neighbor: size[neighbor])
            parent[index] = target
            size[target] += size[index]

    groups = {}
    for index in range(len(keys)):
        groups.setdefault(root(index), []).append(index)
    return [np.array(groups[group]) for group in sorted(groups)]


# ---------------------------------------------------------------------------
# Hourly extraction
# ---------------------------------------------------------------------------


def _chunk_shape(dataset, name):
    chunks = dataset[name].encoding.get("chunks")
    if chunks and len(chunks) == 3:
        return tuple(int(size) for size in chunks)
    return ARCHIVE_CHUNKS


def _read_variable(dataset, name, time_slice, lat_slice, lon_slice):
    values = dataset[name].isel(
        time=time_slice, latitude=lat_slice, longitude=lon_slice
    ).values
    return np.asarray(values, dtype="float64")


def _hourly_fields(raw, variables):
    fields = {}
    if "pcp" in variables:
        fields["pcp_mm"] = raw["APCP_surface"]
    if "tmp" in variables or "hmd" in variables:
        temperature_c = raw["TMP_2maboveground"] - 273.15
        if "tmp" in variables:
            fields["temp_c"] = temperature_c
    if "slr" in variables:
        fields["slr_wm2"] = raw["DSWRF_surface"]
    if "hmd" in variables:
        humidity = raw["SPFH_2maboveground"]
        vapor_pressure = humidity * raw["PRES_surface"] / (0.622 + 0.378 * humidity)
        saturation_pressure = 611.2 * np.exp(
            17.67 * temperature_c / (temperature_c + 243.5)
        )
        fields["rh_fraction"] = np.clip(vapor_pressure / saturation_pressure, 0.0, 1.0)
    if "wnd" in variables:
        fields["wind_ms"] = np.hypot(
            raw["UGRD_10maboveground"], raw["VGRD_10maboveground"]
        )
    if "eto" in variables:
        # Station-mean inputs of the FAO-56 Penman-Monteith reference ET.
        humidity = raw["SPFH_2maboveground"]
        pressure_kpa = raw["PRES_surface"] / 1000.0
        fields["temp_c"] = raw["TMP_2maboveground"] - 273.15
        fields["slr_wm2"] = raw["DSWRF_surface"]
        fields["ea_kpa"] = humidity * pressure_kpa / (0.622 + 0.378 * humidity)
        fields["pres_kpa"] = pressure_kpa
        fields["wind2_ms"] = WIND_10M_TO_2M * np.hypot(
            raw["UGRD_10maboveground"], raw["VGRD_10maboveground"]
        )
    return fields


class _Tile:
    """AOI cells that fall in one spatial chunk of the archive."""

    def __init__(self, lat_indices, lon_indices, labels):
        self.lat_slice = slice(int(lat_indices.min()), int(lat_indices.max()) + 1)
        self.lon_slice = slice(int(lon_indices.min()), int(lon_indices.max()) + 1)
        width = self.lon_slice.stop - self.lon_slice.start
        flat = (lat_indices - self.lat_slice.start) * width + (
            lon_indices - self.lon_slice.start
        )
        order = np.argsort(labels, kind="stable")
        sorted_labels = labels[order]
        self.cell_order = flat[order]
        self.starts = np.flatnonzero(np.r_[True, np.diff(sorted_labels) != 0])
        self.stations = sorted_labels[self.starts]
        self.size = (self.lat_slice.stop - self.lat_slice.start) * width


class _Layout:
    """Stations plus a read plan aligned with the archive chunks.

    Each read covers whole time chunks and at most one spatial chunk, so every
    archive chunk is downloaded once per year.
    """

    def __init__(self, window, stations, labels, chunks=None):
        time_chunk, lat_chunk, lon_chunk = chunks or ARCHIVE_CHUNKS
        self.stations = stations
        keys = np.column_stack(
            (window.lat_indices // lat_chunk, window.lon_indices // lon_chunk)
        )
        _, tile_of_cell = np.unique(keys, axis=0, return_inverse=True)
        tile_of_cell = np.asarray(tile_of_cell).reshape(-1)
        self.tiles = []
        for tile in range(tile_of_cell.max() + 1):
            members = tile_of_cell == tile
            self.tiles.append(
                _Tile(
                    window.lat_indices[members],
                    window.lon_indices[members],
                    np.asarray(labels)[members],
                )
            )
        largest = max(max(tile.size for tile in self.tiles), len(stations))
        self.read_hours = time_chunk * max(
            1,
            min(MAX_TIME_CHUNKS_PER_READ, MAX_VALUES_PER_READ // (time_chunk * largest)),
        )


def extract_local_year(
    year,
    layout,
    variables,
    timezone,
    dataset_opener,
    should_stop=lambda: False,
    report=None,
):
    """Return hourly station series for one local calendar year.

    Rows are labeled by the beginning of each hour in UTC. Precipitation, an
    accumulation ending at the AORC timestamp, is assigned to the preceding
    hour; the other variables are instantaneous at the timestamp.
    """
    start_utc = pd.Timestamp(year, 1, 1).tz_localize(timezone).tz_convert("UTC")
    end_utc = pd.Timestamp(year + 1, 1, 1).tz_localize(timezone).tz_convert("UTC")
    hours = pd.date_range(start_utc, end_utc, freq="h", inclusive="left")
    station_count = len(layout.stations)
    names = archive_variables(variables)
    series = {
        field: np.full((len(hours), station_count), np.nan, dtype="float32")
        for field in _hourly_fields_for(variables)
    }
    warnings = []
    first_ns = start_utc.tz_localize(None).value

    with ThreadPoolExecutor(max_workers=len(names)) as readers:
        for archive_year in range(start_utc.year, end_utc.year + 1):
            if should_stop():
                raise InterruptedError("AORC extraction was canceled.")
            try:
                dataset = dataset_opener(archive_year)
            except Exception as exc:
                if archive_year == year:
                    raise RuntimeError(
                        f"The AORC archive for {archive_year} could not be opened: {exc}"
                    ) from exc
                warnings.append(_boundary_warning(year, archive_year, start_utc, end_utc, timezone))
                continue
            try:
                times = pd.DatetimeIndex(dataset.time.values)
                if archive_year == year and (
                    times[0] > pd.Timestamp(year, 1, 1)
                    or times[-1] < pd.Timestamp(year, 12, 31, 23)
                ):
                    # For example, the 1979 archive starts on 1979-02-01.
                    warnings.append(
                        f"The {year} AORC archive only covers {times[0]:%Y-%m-%d} to "
                        f"{times[-1]:%Y-%m-%d}; days outside that range are missing."
                    )
                first = times.searchsorted(start_utc.tz_localize(None), side="left")
                last = times.searchsorted(end_utc.tz_localize(None), side="right")
                position = first
                while position < last:
                    stop = min(last, (position // layout.read_hours + 1) * layout.read_hours)
                    block = _read_block(
                        dataset, names, variables, layout, slice(position, stop),
                        readers, should_stop,
                    )
                    offsets = (
                        (times[position:stop].asi8 - first_ns) // 3_600_000_000_000
                    ).astype(int)
                    for field, values in block.items():
                        rows = offsets - 1 if field == "pcp_mm" else offsets
                        keep = (rows >= 0) & (rows < len(hours))
                        series[field][rows[keep]] = values[keep]
                    if report:
                        report(year, archive_year, (stop - first) / max(last - first, 1))
                    position = stop
            finally:
                close = getattr(dataset, "close", None)
                if close:
                    close()
    return hours, series, warnings


def _read_block(dataset, names, variables, layout, time_slice, readers, should_stop):
    """Station means for one time block, reading each spatial chunk once."""
    hour_count = time_slice.stop - time_slice.start
    station_count = len(layout.stations)
    sums = {}
    counts = {}
    for tile in layout.tiles:
        if should_stop():
            raise InterruptedError("AORC extraction was canceled.")

        def read(name, tile=tile):
            values = _read_variable(
                dataset, name, time_slice, tile.lat_slice, tile.lon_slice
            )
            return name, values.reshape(values.shape[0], -1)[:, tile.cell_order]

        raw = dict(readers.map(read, names))
        for field, values in _hourly_fields(raw, variables).items():
            valid = np.isfinite(values)
            if field not in sums:
                sums[field] = np.zeros((hour_count, station_count))
                counts[field] = np.zeros((hour_count, station_count), dtype=np.int32)
            sums[field][:, tile.stations] += np.add.reduceat(
                np.where(valid, values, 0.0), tile.starts, axis=1
            )
            counts[field][:, tile.stations] += np.add.reduceat(
                valid.astype(np.int32), tile.starts, axis=1
            )
    with np.errstate(invalid="ignore", divide="ignore"):
        return {
            field: np.where(counts[field] > 0, sums[field] / np.maximum(counts[field], 1), np.nan)
            for field in sums
        }


def _boundary_warning(year, archive_year, start_utc, end_utc, timezone):
    if archive_year > year:
        archive_start = pd.Timestamp(archive_year, 1, 1, tz="UTC")
        missing = max(1, int((end_utc - archive_start) / pd.Timedelta(hours=1)))
        return (
            f"The {archive_year} AORC archive is not available yet, so the last "
            f"{missing} hour(s) of {year}-12-31 in {timezone} time are missing. "
            f"Only that day is incomplete."
        )
    archive_end = pd.Timestamp(archive_year + 1, 1, 1, tz="UTC")
    missing = max(1, int((archive_end - start_utc) / pd.Timedelta(hours=1)))
    return (
        f"The {archive_year} AORC archive could not be opened, so the first "
        f"{missing} hour(s) of {year}-01-01 in {timezone} time are missing. "
        f"Only that day is incomplete."
    )


def _hourly_fields_for(variables):
    names = {"pcp": ["pcp_mm"], "tmp": ["temp_c"], "slr": ["slr_wm2"], "hmd": ["rh_fraction"],
             "wnd": ["wind_ms"], "eto": ETO_FIELDS}
    fields = []
    for variable in variables:
        fields += [name for name in names[variable] if name not in fields]
    return fields


# ---------------------------------------------------------------------------
# Reference evapotranspiration (FAO-56 Penman-Monteith, grass reference)
# Allen, R.G., Pereira, L.S., Raes, D., Smith, M. (1998). Crop evapotranspiration.
# FAO Irrigation and Drainage Paper 56.
# ---------------------------------------------------------------------------

SOLAR_CONSTANT = 0.0820  # MJ m-2 min-1
ALBEDO = 0.23


def _saturation_vapor_pressure(temp_c):
    return 0.6108 * np.exp(17.27 * temp_c / (temp_c + 237.3))  # kPa, Eq. 11


def _elevation_from_pressure(pressure_kpa):
    return 293.0 / 0.0065 * (1.0 - (pressure_kpa / 101.3) ** (1.0 / 5.26))  # inverse of Eq. 7


def _solar_geometry(day_of_year, lat_deg):
    phi = np.radians(lat_deg)
    angle = 2.0 * np.pi * day_of_year / 365.0
    inverse_distance = 1.0 + 0.033 * np.cos(angle)  # Eq. 23
    declination = 0.409 * np.sin(angle - 1.39)  # Eq. 24
    sunset = np.arccos(np.clip(-np.tan(phi) * np.tan(declination), -1.0, 1.0))  # Eq. 25
    return phi, inverse_distance, declination, sunset


def _net_radiation(rs, rso, ea_kpa, sigma_t4):
    ratio = np.clip(rs / rso, 0.3, 1.0)
    net_longwave = sigma_t4 * (0.34 - 0.14 * np.sqrt(np.maximum(ea_kpa, 0.0))) * (1.35 * ratio - 0.35)
    return (1.0 - ALBEDO) * rs - net_longwave  # Eqs. 38-40


def reference_et_daily(day_of_year, tmax, tmin, ea_kpa, pressure_kpa, rs_mj, u2, lat_deg):
    """Daily grass reference ET (mm/day), FAO-56 Eq. 6 with G = 0."""
    with np.errstate(invalid="ignore", divide="ignore"):
        tmean = (tmax + tmin) / 2.0
        es = (_saturation_vapor_pressure(tmax) + _saturation_vapor_pressure(tmin)) / 2.0
        delta = 4098.0 * _saturation_vapor_pressure(tmean) / (tmean + 237.3) ** 2  # Eq. 13
        gamma = 0.000665 * pressure_kpa  # Eq. 8
        phi, dr, declination, sunset = _solar_geometry(day_of_year, lat_deg)
        ra = 24.0 * 60.0 / np.pi * SOLAR_CONSTANT * dr * (
            sunset * np.sin(phi) * np.sin(declination)
            + np.cos(phi) * np.cos(declination) * np.sin(sunset)
        )  # Eq. 21
        rso = (0.75 + 2e-5 * _elevation_from_pressure(pressure_kpa)) * ra  # Eq. 37
        sigma_t4 = 4.903e-9 * ((tmax + 273.16) ** 4 + (tmin + 273.16) ** 4) / 2.0
        rn = _net_radiation(rs_mj, rso, ea_kpa, sigma_t4)
        eto = (0.408 * delta * rn + gamma * 900.0 / (tmean + 273.0) * u2 * (es - ea_kpa)) / (
            delta + gamma * (1.0 + 0.34 * u2)
        )
    return np.maximum(eto, 0.0)


def reference_et_hourly(hours_utc, temp_c, ea_kpa, pressure_kpa, rs_mj, u2, lat_deg, lon_deg):
    """Hourly grass reference ET (mm/hour), FAO-56 Eq. 53.

    ``hours_utc`` labels the beginning of each hour; arrays are hours x stations.
    At night, Rs/Rso keeps the value of the last sunny hour (FAO-56 recommends
    the ratio of 2-3 hours before sunset).
    """
    middle = hours_utc + pd.Timedelta(minutes=30)
    day = np.asarray(middle.dayofyear, dtype=float)[:, None]
    hour = np.asarray(middle.hour + middle.minute / 60.0, dtype=float)[:, None]
    lat = np.asarray(lat_deg, dtype=float)[None, :]
    lon = np.asarray(lon_deg, dtype=float)[None, :]
    with np.errstate(invalid="ignore", divide="ignore"):
        phi, dr, declination, sunset = _solar_geometry(day, lat)
        b = 2.0 * np.pi * (day - 81.0) / 364.0
        season = 0.1645 * np.sin(2 * b) - 0.1255 * np.cos(b) - 0.025 * np.sin(b)  # Eq. 32
        omega = np.pi / 12.0 * ((hour + lon / 15.0 + season) - 12.0)  # Eq. 31, UTC + longitude
        omega1 = np.clip(omega - np.pi / 24.0, -sunset, sunset)  # Eq. 29
        omega2 = np.clip(omega + np.pi / 24.0, -sunset, sunset)  # Eq. 30
        ra = 12.0 * 60.0 / np.pi * SOLAR_CONSTANT * dr * (
            (omega2 - omega1) * np.sin(phi) * np.sin(declination)
            + np.cos(phi) * np.cos(declination) * (np.sin(omega2) - np.sin(omega1))
        )  # Eq. 28
        ra = np.maximum(ra, 0.0)
        rso = (0.75 + 2e-5 * _elevation_from_pressure(pressure_kpa)) * ra
        # Sunny hours use their own Rs/Rso; the others keep the last sunny hour's ratio.
        ratio = np.where(rso > 0.1, rs_mj / rso, np.nan)
        ratio = np.clip(pd.DataFrame(ratio).ffill().bfill().fillna(0.8).to_numpy(), 0.3, 1.0)
        sigma_t4 = 2.043e-10 * (temp_c + 273.16) ** 4
        net_longwave = sigma_t4 * (0.34 - 0.14 * np.sqrt(np.maximum(ea_kpa, 0.0))) * (1.35 * ratio - 0.35)
        rn = (1.0 - ALBEDO) * rs_mj - net_longwave  # Eqs. 38-40
        soil_heat = np.where(ra > 0, 0.1 * rn, 0.5 * rn)  # Eqs. 45-46
        delta = 4098.0 * _saturation_vapor_pressure(temp_c) / (temp_c + 237.3) ** 2
        gamma = 0.000665 * pressure_kpa
        es = _saturation_vapor_pressure(temp_c)
        eto = (0.408 * delta * (rn - soil_heat) + gamma * 37.0 / (temp_c + 273.0) * u2 * (es - ea_kpa)) / (
            delta + gamma * (1.0 + 0.34 * u2)
        )
    return np.maximum(eto, 0.0)


# ---------------------------------------------------------------------------
# Temporal aggregation
# ---------------------------------------------------------------------------


def _group_starts(keys):
    return np.flatnonzero(np.r_[True, keys[1:] != keys[:-1]])


def _grouped_mean(values, starts):
    valid = np.isfinite(values)
    sums = np.add.reduceat(np.where(valid, values, 0.0), starts, axis=0)
    counts = np.add.reduceat(valid.astype(np.int32), starts, axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(counts > 0, sums / np.maximum(counts, 1), np.nan)


def _grouped_sum(values, starts):
    valid = np.isfinite(values)
    sums = np.add.reduceat(np.where(valid, values, 0.0), starts, axis=0)
    counts = np.add.reduceat(valid.astype(np.int32), starts, axis=0)
    return np.where(counts > 0, sums, np.nan)


def daily_from_hourly(hours, series, timezone, latitudes=None):
    """Aggregate hour-beginning UTC series to local civil days.

    ``latitudes`` (one per station) is needed for reference ET.
    """
    local = hours.tz_convert(timezone)
    day_keys = local.year * 1000 + local.dayofyear
    starts = _group_starts(np.asarray(day_keys))
    dates = pd.DatetimeIndex(local[starts].tz_localize(None).normalize())
    daily = {}
    if "pcp_mm" in series:
        daily["pcp_mm"] = _grouped_sum(series["pcp_mm"].astype("float64"), starts)
    if "temp_c" in series:
        temperature = series["temp_c"].astype("float64")
        with np.errstate(invalid="ignore"):
            daily["tmax_c"] = np.fmax.reduceat(temperature, starts, axis=0)
            daily["tmin_c"] = np.fmin.reduceat(temperature, starts, axis=0)
    if "slr_wm2" in series:
        daily["slr_mj_m2_day"] = _grouped_sum(
            series["slr_wm2"].astype("float64") * 0.0036, starts
        )
    if "rh_fraction" in series:
        daily["rh_fraction"] = _grouped_mean(series["rh_fraction"].astype("float64"), starts)
    if "wind_ms" in series:
        daily["wind_ms"] = _grouped_mean(series["wind_ms"].astype("float64"), starts)
    if "ea_kpa" in series and latitudes is not None:
        daily["eto_mm"] = reference_et_daily(
            np.asarray(dates.dayofyear, dtype=float)[:, None],
            daily["tmax_c"],
            daily["tmin_c"],
            _grouped_mean(series["ea_kpa"].astype("float64"), starts),
            _grouped_mean(series["pres_kpa"].astype("float64"), starts),
            daily["slr_mj_m2_day"],
            _grouped_mean(series["wind2_ms"].astype("float64"), starts),
            np.asarray(latitudes, dtype=float)[None, :],
        )
    return dates, daily


def hourly_output(series, hours=None, latitudes=None, longitudes=None):
    output = {}
    for field, values in series.items():
        if field == "slr_wm2":
            output["slr_mj_m2"] = values.astype("float64") * 0.0036
        else:
            output[field] = values.astype("float64")
    if "ea_kpa" in series and latitudes is not None:
        output["eto_mm"] = reference_et_hourly(
            hours,
            output["temp_c"],
            output["ea_kpa"],
            output["pres_kpa"],
            output["slr_mj_m2"],
            output["wind2_ms"],
            latitudes,
            longitudes,
        )
    return output


# ---------------------------------------------------------------------------
# Elevation
# ---------------------------------------------------------------------------


def sample_elevations(stations, dem_path):
    """Return one elevation per station and the names of uncovered stations."""
    if not dem_path:
        return [MISSING] * len(stations), []

    from osgeo import gdal, osr

    raster = gdal.Open(str(dem_path), gdal.GA_ReadOnly)
    if raster is None:
        raise ValueError("The elevation raster could not be opened by GDAL.")
    projection = raster.GetProjection()
    if not projection:
        raise ValueError("The selected elevation raster has no CRS defined.")
    source_crs = osr.SpatialReference()
    source_crs.ImportFromEPSG(4326)
    target_crs = osr.SpatialReference()
    target_crs.ImportFromWkt(projection)
    source_crs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    target_crs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    transform = osr.CoordinateTransformation(source_crs, target_crs)
    inverse = gdal.InvGeoTransform(raster.GetGeoTransform())
    if inverse is None:
        raise ValueError("The elevation raster has an invalid geotransform.")
    band = raster.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    scale = band.GetScale() or 1.0
    offset = band.GetOffset() or 0.0

    elevations = []
    uncovered = []
    for station in stations:
        x, y, _ = transform.TransformPoint(station["lon"], station["lat"])
        column, row = gdal.ApplyGeoTransform(inverse, x, y)
        column, row = int(np.floor(column)), int(np.floor(row))
        value = None
        if 0 <= column < raster.RasterXSize and 0 <= row < raster.RasterYSize:
            value = band.ReadAsArray(column, row, 1, 1)[0, 0]
        if (
            value is None
            or not np.isfinite(value)
            or (nodata is not None and value == nodata)
        ):
            elevations.append(MISSING)
            uncovered.append(station["station"])
        else:
            elevations.append(float(value * scale + offset))
    raster = None
    return elevations, uncovered


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------


class _Statistics:
    def __init__(self, stations, columns, time_step):
        shape = (len(columns), len(stations))
        self.stations = stations
        self.columns = columns
        self.time_step = time_step
        self.count = np.zeros(shape)
        self.missing = np.zeros(shape)
        self.total = np.zeros(shape)
        self.squares = np.zeros(shape)
        self.minimum = np.full(shape, np.inf)
        self.maximum = np.full(shape, -np.inf)

    def update(self, table):
        for index, column in enumerate(self.columns):
            values = table[column]
            valid = np.isfinite(values)
            filled = np.where(valid, values, 0.0)
            self.count[index] += valid.sum(axis=0)
            self.missing[index] += (~valid).sum(axis=0)
            self.total[index] += filled.sum(axis=0)
            self.squares[index] += (filled**2).sum(axis=0)
            self.minimum[index] = np.fmin(
                self.minimum[index], np.where(valid, values, np.inf).min(axis=0)
            )
            self.maximum[index] = np.fmax(
                self.maximum[index], np.where(valid, values, -np.inf).max(axis=0)
            )

    def frame(self):
        rows = []
        for index, column in enumerate(self.columns):
            for station_index, station in enumerate(self.stations):
                count = self.count[index, station_index]
                missing = self.missing[index, station_index]
                total = count + missing
                mean = self.total[index, station_index] / count if count else np.nan
                if count > 1:
                    variance = (
                        self.squares[index, station_index]
                        - count * mean * mean
                    ) / (count - 1)
                    std = math.sqrt(max(variance, 0.0))
                else:
                    std = np.nan
                rows.append(
                    {
                        "station": station["station"],
                        "time_step": self.time_step,
                        "variable": column,
                        "mean": mean,
                        "std": std,
                        "min": self.minimum[index, station_index] if count else np.nan,
                        "max": self.maximum[index, station_index] if count else np.nan,
                        "missing": int(missing),
                        "missing_pct": float(missing * 100.0 / total) if total else np.nan,
                    }
                )
        return pd.DataFrame(rows)


class OutputWriter:
    """Create output files once, then append one local year at a time."""

    def __init__(self, stations, output_dir, settings, variables, timezone):
        self.stations = stations
        self.output_dir = Path(output_dir)
        self.variables = variables
        self.timezone = timezone
        self.write_csv = settings["write_csv"]
        self.write_swat = settings["write_swat"]
        timesteps, self.swat_pcp_step = output_timesteps(settings)
        self.timesteps = timesteps if self.write_csv else []
        self.swat_dir = self.output_dir / "swatplus_weather"
        self.year_count = int(settings["end_year"]) - int(settings["start_year"]) + 1
        # Statistics describe the CSV tables, or the daily values of SWAT+-only runs.
        self.csv_columns = {
            step: [column for variable in variables for column in STEP_COLUMNS[step][variable]]
            for step in (self.timesteps or ["daily"])
        }
        self.statistics = {
            step: _Statistics(stations, columns, step) for step, columns in self.csv_columns.items()
        }
        self.station_columns = ["station", "lat", "lon", "elev_m"] + (
            ["site_id"] if any("site_id" in station for station in stations) else []
        )
        self.latitudes = np.array([station["lat"] for station in stations], dtype=float)
        self.longitudes = np.array([station["lon"] for station in stations], dtype=float)

    def _csv_path(self, station, step):
        return self.output_dir / f"{station['station']}_{step}.csv"

    def _swat_path(self, station, variable):
        return self.swat_dir / f"{station['station']}.{SWAT_EXTENSIONS[variable]}"

    def start(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for step in self.timesteps:
            prefix = (
                ["datetime_local", "datetime_utc", "year", "jday", "hour"]
                if step == "hourly"
                else ["date", "year", "jday"]
            )
            header = ",".join(prefix + self.csv_columns[step] + self.station_columns)
            for station in self.stations:
                self._csv_path(station, step).write_text(header + "\n", encoding="utf-8")
        if self.write_swat:
            self.swat_dir.mkdir(parents=True, exist_ok=True)
            for station in self.stations:
                for variable in self.variables:
                    step = 24 if variable == "pcp" and self.swat_pcp_step == "hourly" else 0
                    with self._swat_path(station, variable).open(
                        "w", encoding="utf-8", newline="\n"
                    ) as file_obj:
                        file_obj.write(
                            f"{station['station']}.{SWAT_EXTENSIONS[variable]}: NOAA/NWS AORC v1.1, "
                            f"{VARIABLE_LABELS[variable]}\n"
                        )
                        file_obj.write("nbyr     tstep       lat       lon      elev\n")
                        file_obj.write(
                            f"{self.year_count:4d}{step:10d}{station['lat']:10.5f}"
                            f"{station['lon']:11.5f}{station['elev_m']:10.2f}\n"
                        )
            for variable in self.variables:
                extension = SWAT_EXTENSIONS[variable]
                with (self.swat_dir / f"{extension}.cli").open(
                    "w", encoding="utf-8", newline="\n"
                ) as file_obj:
                    file_obj.write(f"{extension}.cli: NOAA/NWS AORC v1.1 station files\n")
                    file_obj.write("filename\n")
                    for station in self.stations:
                        file_obj.write(f"{station['station']}.{extension}\n")

    def write_year(self, hours, series):
        swat_hourly = self.write_swat and self.swat_pcp_step == "hourly" and "pcp" in self.variables
        dates, daily = (
            daily_from_hourly(hours, series, self.timezone, self.latitudes)
            if "daily" in self.csv_columns or self.write_swat
            else (None, None)
        )
        hourly = (
            hourly_output(series, hours, self.latitudes, self.longitudes)
            if "hourly" in self.csv_columns or swat_hourly
            else None
        )
        local = hours.tz_convert(self.timezone)

        tables = {}
        if "daily" in self.csv_columns:
            tables["daily"] = daily, {
                "date": dates.strftime("%Y-%m-%d"),
                "year": dates.year,
                "jday": dates.dayofyear,
            }
        if "hourly" in self.csv_columns:
            tables["hourly"] = hourly, {
                "datetime_local": local.strftime("%Y-%m-%dT%H:%M%z"),
                "datetime_utc": hours.strftime("%Y-%m-%dT%H:%MZ"),
                "year": local.year,
                "jday": local.dayofyear,
                "hour": local.hour,
            }
        for step, (table, _) in tables.items():
            self.statistics[step].update(table)

        for index, station in enumerate(self.stations):
            for step in self.timesteps:
                table, time_columns = tables[step]
                frame = pd.DataFrame(time_columns)
                for column in self.csv_columns[step]:
                    frame[column] = np.round(table[column][:, index], 5)
                for column in self.station_columns:
                    frame[column] = station[column]
                frame.to_csv(self._csv_path(station, step), mode="a", header=False, index=False)
            if self.write_swat:
                self._write_swat_year(station, index, dates, daily, local, hourly)

    def _write_swat_year(self, station, index, dates, daily, local, hourly):
        years = dates.year
        jdays = dates.dayofyear

        def values(column):
            data = daily[column][:, index]
            return np.where(np.isfinite(data), data, MISSING)

        lines = {}
        if "pcp" in self.variables:
            if self.swat_pcp_step == "hourly":
                pcp = hourly["pcp_mm"][:, index]
                pcp = np.where(np.isfinite(pcp), pcp, MISSING)
                lines["pcp"] = [
                    f"{year:4d}{jday:5d}{month:4d}{day:4d}{hour:4d}{value:10.3f}\n"
                    for year, jday, month, day, hour, value in zip(
                        local.year, local.dayofyear, local.month, local.day, local.hour, pcp
                    )
                ]
            else:
                lines["pcp"] = [
                    f"{year:4d}{jday:5d}{value:10.3f}\n"
                    for year, jday, value in zip(years, jdays, values("pcp_mm"))
                ]
        if "tmp" in self.variables:
            lines["tmp"] = [
                f"{year:4d}{jday:5d}{tmax:10.3f}{tmin:10.3f}\n"
                for year, jday, tmax, tmin in zip(
                    years, jdays, values("tmax_c"), values("tmin_c")
                )
            ]
        for variable, column, width in (
            ("slr", "slr_mj_m2_day", "10.3f"),
            ("hmd", "rh_fraction", "10.4f"),
            ("wnd", "wind_ms", "10.3f"),
            ("eto", "eto_mm", "10.3f"),
        ):
            if variable in self.variables:
                lines[variable] = [
                    f"{year:4d}{jday:5d}{value:{width}}\n"
                    for year, jday, value in zip(years, jdays, values(column))
                ]
        for variable, rows in lines.items():
            with self._swat_path(station, variable).open(
                "a", encoding="utf-8", newline="\n"
            ) as file_obj:
                file_obj.writelines(rows)

    def finish(self):
        summary_path = self.output_dir / "weather_statistics.csv"
        frame = pd.concat([statistics.frame() for statistics in self.statistics.values()], ignore_index=True)
        order = {station["station"]: index for index, station in enumerate(self.stations)}
        frame.sort_values("station", key=lambda names: names.map(order), kind="stable").to_csv(
            summary_path, index=False
        )
        return summary_path


def _spatial_reference(definition):
    from osgeo import osr

    reference = osr.SpatialReference()
    if reference.SetFromUserInput(definition or "EPSG:4326") != 0:
        raise ValueError("The target coordinate reference system is not recognized.")
    reference.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return reference


def run_label(output_dir):
    """Short name of a run, taken from its output folder."""
    label = re.sub(r"[^\w-]+", "_", Path(output_dir).resolve().name).strip("_")
    return label or "output"


def write_station_locations(stations, output_dir, target_crs=None):
    """Write station_locations.csv and a GeoPackage in the target CRS."""
    from osgeo import ogr, osr

    output_dir = Path(output_dir)
    source = osr.SpatialReference()
    source.ImportFromEPSG(4326)
    source.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    target = _spatial_reference(target_crs)
    transform = osr.CoordinateTransformation(source, target)

    points = []
    for station in stations:
        point = ogr.CreateGeometryFromWkt(f"POINT ({station['lon']} {station['lat']})")
        point.Transform(transform)
        points.append(point)

    gauges = any("site_id" in station for station in stations)
    locations = pd.DataFrame(
        {
            "station": [station["station"] for station in stations],
            "lat": [station["lat"] for station in stations],
            "lon": [station["lon"] for station in stations],
            "x_target_crs": [point.GetX() for point in points],
            "y_target_crs": [point.GetY() for point in points],
            "elev_m": [station["elev_m"] for station in stations],
            "aorc_cells": [station["cells"] for station in stations],
        }
    )
    if gauges:
        # The rain gauge of each station, as in the Observed Data tables.
        locations["site_id"] = [station.get("site_id", "") for station in stations]
    locations.to_csv(output_dir / "station_locations.csv", index=False)

    driver = ogr.GetDriverByName("GPKG")
    stem = f"aorc_{run_label(output_dir)}"
    gpkg_path = output_dir / f"{stem}.gpkg"
    number = 1
    while gpkg_path.exists():
        driver.DeleteDataSource(str(gpkg_path))
        if not gpkg_path.exists():
            break
        # Still open in QGIS from an earlier run (Windows cannot delete it).
        number += 1
        gpkg_path = output_dir / f"{stem}_{number}.gpkg"
    datasource = driver.CreateDataSource(str(gpkg_path))
    if datasource is None:
        raise OSError(f"Could not create {gpkg_path}.")

    station_layer = datasource.CreateLayer("stations", target, ogr.wkbPoint)
    area_layer = datasource.CreateLayer("station_areas", target, ogr.wkbMultiPolygon)
    for layer in (station_layer, area_layer):
        layer.CreateField(ogr.FieldDefn("station", ogr.OFTString))
        for name in ("latitude", "longitude", "elev_m"):
            layer.CreateField(ogr.FieldDefn(name, ogr.OFTReal))
        layer.CreateField(ogr.FieldDefn("aorc_cells", ogr.OFTInteger))
        if gauges:
            layer.CreateField(ogr.FieldDefn("site_id", ogr.OFTString))

    for station, point in zip(stations, points):
        area = ogr.CreateGeometryFromWkb(shapely.to_wkb(station["area"]))
        area.Segmentize(GRID_STEP_DEG)
        area.Transform(transform)
        area = ogr.ForceToMultiPolygon(area)
        for layer, geometry in ((station_layer, point), (area_layer, area)):
            feature = ogr.Feature(layer.GetLayerDefn())
            feature.SetField("station", station["station"])
            feature.SetField("latitude", station["lat"])
            feature.SetField("longitude", station["lon"])
            feature.SetField("elev_m", station["elev_m"])
            feature.SetField("aorc_cells", int(station["cells"]))
            if gauges:
                feature.SetField("site_id", station.get("site_id", ""))
            feature.SetGeometry(geometry)
            layer.CreateFeature(feature)
            feature = None
    datasource = None
    return output_dir / "station_locations.csv", gpkg_path


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


GRID_DESCRIPTIONS = {
    "original": "original AORC grid, one station per 30 arc-second (~800 m) cell",
    "single": "single station, the AORC cell nearest to the AOI centroid",
}


def describe_grid(settings):
    if settings["grid_mode"] == "gauges":
        return (
            f"rain gauges, the AORC cell nearest to each of the {len(settings.get('gauges') or [])} "
            "gauges selected in the Observed Data tab"
        )
    if settings["grid_mode"] == "block":
        return (
            f"{float(settings['block_km']):g} km blocks, each station is the mean "
            "of the AORC cells inside its block"
        )
    return GRID_DESCRIPTIONS[settings["grid_mode"]]


def extract_aorc(
    settings,
    aoi_geometry,
    progress=None,
    is_canceled=None,
    dataset_opener=None,
):
    start_year = int(settings["start_year"])
    end_year = int(settings["end_year"])
    output_dir = Path(settings["output_dir"])
    dataset_opener = dataset_opener or open_aorc_year
    is_canceled = is_canceled or (lambda: False)
    timezone = settings.get("timezone", TIMEZONE)
    timesteps, swat_pcp_step = output_timesteps(settings)
    variables = [
        variable for variable in VARIABLE_ORDER if variable in settings["variables"]
    ]
    if not variables:
        raise ValueError("Select at least one climate variable.")
    if settings["write_csv"] and not timesteps:
        raise ValueError("Choose the daily or the hourly time step, or both.")
    years = list(range(start_year, end_year + 1))
    if (
        swat_pcp_step == "hourly"
        and settings["write_swat"]
        and "pcp" in variables
        and not has_fixed_offset(timezone, years)
    ):
        raise ValueError(
            "Hourly SWAT+ precipitation needs exactly 24 values per day. Choose a "
            "fixed-offset timezone (GMT-x, no daylight saving time)."
        )
    workers = max(1, int(settings.get("parallel_workers", 2)))
    stop_event = Event()

    def should_stop():
        return stop_event.is_set() or is_canceled()

    if should_stop():
        raise InterruptedError("AORC extraction was canceled.")
    if progress:
        progress(0.0, "Connecting to the AORC archive on AWS and building stations...")

    first_dataset = dataset_opener(start_year)
    try:
        window, stations, labels = build_layout(
            first_dataset,
            aoi_geometry,
            settings["grid_mode"],
            settings.get("block_km"),
            settings.get("gauges"),
        )
        chunks = _chunk_shape(first_dataset, archive_variables(variables)[0])
    finally:
        close = getattr(first_dataset, "close", None)
        if close:
            close()
    layout = _Layout(window, stations, labels, chunks)

    elevations, uncovered = sample_elevations(stations, settings.get("dem_path", ""))
    for station, elevation in zip(stations, elevations):
        station["elev_m"] = elevation
    warnings = []
    skipped = len(settings.get("gauges") or []) - len(stations)
    if settings["grid_mode"] == "gauges" and skipped:
        warnings.append(f"{skipped} rain gauge(s) lie outside the AORC grid and were left out.")
    if uncovered:
        warnings.append(
            f"The elevation raster has no value at {len(uncovered)} station(s); "
            "their elevation was written as -99."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    locations_path, gpkg_path = write_station_locations(
        stations, output_dir, settings.get("target_crs")
    )
    writer = OutputWriter(stations, output_dir, settings, variables, timezone)
    writer.start()

    fractions = {year: 0.0 for year in years}
    downloading = set()
    progress_lock = Lock()

    def status(prefix=""):
        # Called with progress_lock held.
        percentage = 100.0 * sum(fractions.values()) / len(years)
        active = ", ".join(str(year) for year in sorted(downloading))
        message = f"Downloading AORC data from AWS: {active}" if active else "Writing output files..."
        return percentage, prefix + message

    def report(year, archive_year, fraction):
        with progress_lock:
            if archive_year == year:
                fractions[year] = max(fractions[year], 0.95 * fraction)
            percentage, message = status()
        if progress:
            progress(percentage, message)

    def process(year):
        with progress_lock:
            downloading.add(year)
            percentage, message = status()
        if progress:
            progress(percentage, message)
        try:
            return extract_local_year(
                year, layout, variables, timezone, dataset_opener, should_stop, report
            )
        finally:
            with progress_lock:
                downloading.discard(year)

    pending = deque()
    next_year = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        try:
            while next_year < len(years) and len(pending) < workers:
                pending.append((years[next_year], executor.submit(process, years[next_year])))
                next_year += 1
            while pending:
                year, future = pending.popleft()
                hours, series, year_warnings = future.result()
                if next_year < len(years):
                    pending.append(
                        (years[next_year], executor.submit(process, years[next_year]))
                    )
                    next_year += 1
                if should_stop():
                    raise InterruptedError("AORC extraction was canceled.")
                writer.write_year(hours, series)
                warnings.extend(year_warnings)
                with progress_lock:
                    fractions[year] = 1.0
                    percentage, message = status(f"Saved year {year}. ")
                if progress:
                    progress(percentage, message)
        except BaseException:
            stop_event.set()
            for _, future in pending:
                future.cancel()
            raise

    if progress:
        progress(100.0, "Writing statistics and README...")
    summary_path = writer.finish()
    manifest = output_dir / "README.txt"
    selected = ", ".join(VARIABLE_LABELS[variable] for variable in variables)
    lines = [
        "NOAA/NWS Analysis of Record for Calibration (AORC) version 1.1",
        f"Archive: {AORC_URL} (s3://{AORC_BUCKET}, https://registry.opendata.aws/noaa-nws-aorc/)",
        f"Data accessed: {date.today().isoformat()}",
        f"Requested local dates: {start_year}-01-01 through {end_year}-12-31",
        f"Timezone: {timezone}",
        f"CSV time steps: {', '.join(timesteps) if settings['write_csv'] else 'none (no CSV files)'}",
        f"Grid: {describe_grid(settings)}",
        f"Stations: {len(stations)}",
        f"Variables: {selected}",
    ]
    if settings["write_swat"]:
        lines.append(f"SWAT+ precipitation time step: {swat_pcp_step}")
    lines += [
        "",
        "Conventions:",
        "- AORC precipitation is the accumulation ending at the archive timestamp; it is "
        "assigned to the preceding hour.",
        "- Temperature, humidity, pressure, radiation and wind are taken at the archive "
        "timestamp.",
        "- Hourly rows are labeled by the beginning of the hour in local time.",
        "- Daily values cover local civil days: precipitation and solar radiation are sums, "
        "maximum/minimum temperature come from hourly values, humidity and wind are means.",
        "- Block values are the mean of the AORC cells they contain; the single station "
        "uses the AORC cell nearest to the AOI centroid; rain-gauge stations use the AORC cell "
        "nearest to each gauge, named in the site_id column as in the Observed Data files.",
        "- Daily and hourly CSV files of a station come from the same hourly values, so they "
        "can be compared directly.",
        "- Relative humidity is derived from specific humidity, surface pressure and "
        "temperature and is bounded to [0, 1].",
        "- Solar radiation is converted from W/m2 to MJ/m2 (per hour or per day).",
        "- Wind speed is derived from the 10 m U/V components.",
        "- Reference ET (eto_mm) is the FAO-56 Penman-Monteith grass reference ET (Allen et al., "
        "1998): daily values use Eq. 6 with daily Tmax/Tmin, mean vapor pressure from specific "
        "humidity, summed solar radiation, and 2 m wind (FAO-56 Eq. 47 from 10 m); hourly values "
        "use Eq. 53. Block and single stations use the mean inputs of their cells. In SWAT+, "
        "files *.pet and pet.cli hold daily ETo (set the PET method to read in values).",
        "- Missing SWAT+ values and elevations use -99.",
        "- SWAT+ hourly precipitation files use tstep 24 and rows of year, day of year, "
        "month, day, hour, value; the other SWAT+ variables stay daily.",
        f"- Parallel workers: {workers}",
    ]
    if warnings:
        lines += ["", f"Warnings: {len(warnings)}, listed in warnings.txt."]
    lines += [
        "",
        "Cite as: NOAA. NOAA Analysis of Record for Calibration (AORC) Dataset. "
        f"Accessed {date.today().isoformat()} from https://registry.opendata.aws/noaa-nws-aorc/.",
        "These are derived values, not original unaltered NOAA data.",
    ]
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    warnings_file = write_warnings(output_dir, warnings)
    if progress:
        progress(100.0, "Extraction complete")
    return {
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


def write_warnings(folder, warnings):
    """Write warnings.txt (one warning per line); remove a stale one when there are none."""
    path = Path(folder) / "warnings.txt"
    if not warnings:
        path.unlink(missing_ok=True)
        return None
    header = f"{len(warnings)} warning(s), {date.today().isoformat()}\n\n"
    path.write_text(header + "\n".join(f"- {warning}" for warning in warnings) + "\n", encoding="utf-8")
    return path
