"""Grid locations and metadata for a non-destructive availability preview."""

from .aorc_processing import VARIABLE_LABELS, build_layout, open_aorc_year
from .aoi_selection import gauges_in_geometry
from .era5_processing import Grid, GRID_MARGIN_DEG, PREFIX as ERA5_PREFIX


DATASETS = {
    "aorc": {
        "name": "AORC",
        "first_date": "1979-02-01",
        "coverage": "contiguous United States and contributing areas",
    },
    "era5land": {
        "name": "ERA5-Land",
        "first_date": "1950-01-01",
        "coverage": "global land; some grid cells may have no values",
    },
}


def build_preview(settings, geometry, dataset_opener=None, projection=None):
    """Return the exact station layout used by extraction without reading weather data."""
    dataset = settings.get("dataset", "aorc")
    gauges = settings.get("gauges") or []
    excluded_gauges = 0
    if settings["grid_mode"] == "gauges" and settings.get("subbasin_feature_ids") is not None:
        selected_gauges = gauges_in_geometry(geometry, gauges)
        excluded_gauges = len(gauges) - len(selected_gauges)
        gauges = selected_gauges
        if not gauges:
            raise ValueError("None of the selected rain gauges falls inside the selected sub-basin(s).")
    if dataset == "aorc":
        opener = dataset_opener or open_aorc_year
        archive = opener(int(settings["start_year"]))
        try:
            _, stations, _ = build_layout(
                archive,
                geometry,
                settings["grid_mode"],
                settings.get("block_km"),
                gauges,
                prefix="aorc",
            )
        finally:
            close = getattr(archive, "close", None)
            if close:
                close()
    elif dataset == "era5land":
        if projection is None:
            raise ValueError("An Earth Engine grid projection is required for an ERA5-Land preview.")
        _, transform = projection
        bounds = geometry.bounds
        if settings["grid_mode"] == "gauges" and gauges:
            lons = [gauge["lon"] for gauge in gauges] + [bounds[0], bounds[2]]
            lats = [gauge["lat"] for gauge in gauges] + [bounds[1], bounds[3]]
            bounds = min(lons), min(lats), max(lons), max(lats)
        grid = Grid(transform, bounds, margin=GRID_MARGIN_DEG)
        _, stations, _ = build_layout(
            grid.axes(),
            geometry,
            settings["grid_mode"],
            settings.get("block_km"),
            gauges,
            step=grid.step,
            prefix=ERA5_PREFIX,
        )
    else:
        raise ValueError(f"Unknown gridded dataset: {dataset}")

    return {
        "dataset": dataset,
        "stations": stations,
        "summary": availability_summary(settings, len(stations), excluded_gauges),
    }


def availability_summary(settings, station_count, excluded_gauges=0):
    """Describe the selected period and variables without implying a data download."""
    dataset = DATASETS[settings.get("dataset", "aorc")]
    first_year = int(settings["start_year"])
    last_year = int(settings["end_year"])
    first_date = dataset["first_date"] if first_year == int(dataset["first_date"][:4]) else f"{first_year}-01-01"
    variables = [VARIABLE_LABELS[key] for key in settings["variables"] if key in VARIABLE_LABELS]
    lines = [
        f"{dataset['name']} nominal coverage starts {dataset['first_date']} ({dataset['coverage']}).",
        f"Selected period: {first_date} to {last_year}-12-31.",
        "Selected variables: " + (", ".join(variables) if variables else "none") + ".",
        f"Preview locations: {station_count:,} extraction station(s). No weather data was downloaded.",
    ]
    selected_subbasins = settings.get("subbasin_feature_ids")
    if selected_subbasins is not None:
        lines.append(f"Selected sub-basins: {len(selected_subbasins):,} polygon(s).")
    if excluded_gauges:
        lines.append(
            f"{excluded_gauges:,} selected rain gauge(s) fall outside the selected sub-basin(s) and are omitted."
        )
    return "\n".join(lines)
