"""Variables offered in the Observed Data tab (USGS, Water Quality Portal, NOAA GHCN)."""

import re

QUANTITY = "quantity"
QUALITY = "quality"

DAILY = "daily"
CONTINUOUS = "continuous"
FIELD = "field"
PEAKS = "peaks"
SAMPLES = "samples"

QUANTITY_COLUMNS = [DAILY, CONTINUOUS, FIELD, PEAKS]
QUALITY_COLUMNS = [DAILY, CONTINUOUS, SAMPLES]
COLUMN_LABELS = {
    QUANTITY: {DAILY: "Daily", CONTINUOUS: "Sub-daily", FIELD: "Field", PEAKS: "Peaks"},
    QUALITY: {DAILY: "Sensor daily", CONTINUOUS: "Sensor sub-daily", SAMPLES: "Samples"},
}
COLUMN_TOOLTIPS = {
    DAILY: "Daily series (mean, or sum for precipitation); -99 marks missing days",
    CONTINUOUS: "USGS: sensor values every 5-60 min, usually 15 (since about 2007); NOAA GHCNh: hourly totals",
    FIELD: "Field measurements made by USGS staff (point in time); for channel geometry: "
    "width, cross-section area, mean velocity, and bed material of each discharge measurement",
    PEAKS: "Annual peak flow or stage",
    SAMPLES: "Laboratory or field samples (point in time)",
}

# Daily statistics in order of preference: mean, sum, maximum, minimum.
DAILY_STATISTICS = ["00003", "00006", "00001", "00002"]
STATISTIC_NAMES = {"00003": "mean", "00006": "sum", "00001": "max", "00002": "min"}

# The legacy Water Quality Portal stopped receiving USGS samples on this date.
WQP_USGS_FROZEN = "2024-03-11"


class Row:
    def __init__(
        self,
        key,
        label,
        group,
        columns,
        code=None,
        unit="",
        metric_unit="",
        factor=None,
        sample_groups=(),
        sample_characteristics=(),
        collection=None,
    ):
        self.key = key
        self.label = label
        self.group = group
        self.columns = columns
        self.code = code
        self.unit = unit
        self.metric_unit = metric_unit
        self.factor = factor
        self.sample_groups = tuple(sample_groups)
        self.sample_characteristics = tuple(sample_characteristics)
        # Rows without a parameter code come from their own API collection.
        self.collection = collection


ROWS = [
    Row("streamflow", "Streamflow (00060)", QUANTITY, {DAILY, CONTINUOUS, FIELD, PEAKS},
        "00060", "cfs", "m3s", 0.028316846592),
    Row("streamflow_tidal", "Streamflow, tide-filtered (72137)", QUANTITY, {DAILY, CONTINUOUS},
        "72137", "cfs", "m3s", 0.028316846592),
    Row("gage_height", "Gage height (00065)", QUANTITY, {DAILY, CONTINUOUS, FIELD, PEAKS},
        "00065", "ft", "m", 0.3048),
    Row("estuary_level", "Estuary / ocean level (62620)", QUANTITY, {DAILY, CONTINUOUS},
        "62620", "ft", "m", 0.3048),
    Row("groundwater_depth", "Groundwater depth (72019)", QUANTITY, {DAILY, CONTINUOUS, FIELD},
        "72019", "ft", "m", 0.3048),
    Row("precipitation", "Precipitation (USGS 00045)", QUANTITY, {DAILY, CONTINUOUS},
        "00045", "in", "mm", 25.4),
    # NOAA rain gauges, found and read through NCEI services: daily from GHCN-Daily (COOP,
    # CoCoRaHS, airports), hourly from GHCNh (airports, USCRN, COOP recording gauges).
    Row("precipitation_ghcn", "Precipitation (NOAA GHCN)", QUANTITY, {DAILY, CONTINUOUS}, unit="mm"),
    Row("velocity", "Water velocity (72255)", QUANTITY, {DAILY, CONTINUOUS, FIELD},
        "72255", "fts", "ms", 0.3048),
    Row("channel_geometry", "Channel geometry", QUANTITY, {FIELD},
        collection="channel-measurements"),
    Row("water_temperature", "Water temperature", QUALITY, {DAILY, CONTINUOUS, SAMPLES},
        "00010", "degC", sample_characteristics=["Temperature, water"]),
    Row("specific_conductance", "Specific conductance", QUALITY, {DAILY, CONTINUOUS, SAMPLES},
        "00095", "uScm", sample_characteristics=["Specific conductance"]),
    Row("salinity", "Salinity", QUALITY, {DAILY, CONTINUOUS, SAMPLES},
        "00480", "ppt", sample_characteristics=["Salinity"]),
    Row("dissolved_oxygen", "Dissolved oxygen", QUALITY, {DAILY, CONTINUOUS, SAMPLES},
        "00300", "mgL", sample_characteristics=["Dissolved oxygen (DO)"]),
    Row("ph", "pH", QUALITY, {DAILY, CONTINUOUS, SAMPLES},
        "00400", "pH", sample_characteristics=["pH"]),
    Row("turbidity", "Turbidity", QUALITY, {DAILY, CONTINUOUS, SAMPLES},
        "63680", "FNU", sample_characteristics=["Turbidity"]),
    Row("nutrients", "Nutrients (N, P)", QUALITY, {SAMPLES}, sample_groups=["Nutrient"]),
    Row("sediment", "Sediment", QUALITY, {SAMPLES}, sample_groups=["Sediment"]),
    Row("microbiological", "Microbiological", QUALITY, {SAMPLES},
        sample_groups=["Microbiological"]),
    Row("major_ions", "Major ions", QUALITY, {SAMPLES},
        sample_groups=["Inorganics, Major, Non-metals", "Inorganics, Major, Metals"]),
    Row("metals", "Metals (minor)", QUALITY, {SAMPLES}, sample_groups=["Inorganics, Minor, Metals"]),
    Row("pesticides", "Pesticides", QUALITY, {SAMPLES}, sample_groups=["Organics, Pesticide"]),
    Row("other_organics", "Other organics", QUALITY, {SAMPLES}, sample_groups=["Organics, Other"]),
]
ROWS_BY_KEY = {row.key: row for row in ROWS}
ROWS_BY_CODE = {row.code: row for row in ROWS if row.code}
FIELD_CODES = [row.code for row in ROWS if FIELD in row.columns and row.code]
CHANNEL = "channel_geometry"
GHCN_PRECIPITATION = "precipitation_ghcn"
# Rows whose stations are rain gauges (the AORC tab can extract their cells).
RAIN_GAUGE_ROWS = ("precipitation", GHCN_PRECIPITATION)
# Missing day in every daily CSV, and missing hour in the hourly GHCNh files.
MISSING_VALUE = -99

# Map layers: channel geometry sites get their own layer (drawn as a dash).
LAYER_CHANNEL = "channel"
LAYERS = (QUANTITY, QUALITY, LAYER_CHANNEL)


def layer_of(row_key):
    return LAYER_CHANNEL if row_key == CHANNEL else ROWS_BY_KEY[row_key].group


def site_stem(site_id):
    """File-name stem of a site id: 'USGS-02378500' -> 'usgs_02378500'."""
    return re.sub(r"[^0-9A-Za-z]+", "_", site_id).strip("_").lower()


def rows_of(group):
    return [row for row in ROWS if row.group == group]


def columns_of(group):
    return QUANTITY_COLUMNS if group == QUANTITY else QUALITY_COLUMNS
