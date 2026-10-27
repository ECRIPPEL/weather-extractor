"""Monthly basin-mean actual evapotranspiration from OpenET and MODIS via Google Earth Engine.

Earth Engine only averages each image over the area; the monthly calendar work
(spreading MODIS 8-day composites over months) is done locally, which keeps
every Earth Engine request small. Sign-in and the Cloud project are handled by
the Google Earth Engine QGIS plugin; nothing here contacts Earth Engine until
an extraction runs.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
import json
import math
import os
from pathlib import Path
import site
import sys
import threading
import time

import numpy as np
import pandas as pd
import shapely

from .aorc_processing import write_warnings


OPENET = "openet"
MODIS = "modis"
PRODUCTS = {
    OPENET: {
        "label": "OpenET",
        "description": "OpenET Ensemble v2.1 (v2.0 before 2016), monthly, 30 m",
        "bands": ["et_ensemble_mad", "et_ensemble_mad_min", "et_ensemble_mad_max", "et_ensemble_mad_count"],
        "columns": ["openet_et_mm", "openet_et_min_mm", "openet_et_max_mm", "openet_models"],
        "valid": "openet_valid_fraction",
    },
    MODIS: {
        "label": "MODIS",
        "description": "MODIS MOD16A2GF, 8-day, 500 m",
        "collection": "MODIS/061/MOD16A2GF",
        "bands": ["ET", "PET"],
        "columns": ["modis_et_mm", "modis_pet_mm"],
        "valid": "modis_valid_fraction",
    },
}
FIRST_YEAR = 2000
# OpenET v2.1 covers the whole CONUS from October 2015; v2.0 starts in 1999 but
# covers only UTM zones 10-15 (west of about 90 W). Newest first: (first year, version, asset).
OPENET_COLLECTIONS = [
    (2016, "2.1", "projects/openet/assets/ensemble/conus/gridmet/monthly/v2_1"),
    (FIRST_YEAR, "2.0", "projects/openet/assets/ensemble/conus/gridmet/monthly/v2_0"),
]
MODIS_FILL = 32700  # MOD16 codes above this are water, urban, barren, or no data
MODIS_SCALE = 0.1  # kg/m2 per composite = mm
MODIS_COMPOSITE_DAYS = 8
# OpenET is averaged at a coarser scale on large areas (Earth Engine then reads
# its pre-averaged pyramid levels), keeping about this many pixels per image.
TARGET_PIXELS = 20000
OPENET_NATIVE_M = 30
MAX_VERTICES = 2000
YEAR_WORKERS = 4
MAX_ATTEMPTS = 4
LOW_COVERAGE = 0.5
KM_PER_DEGREE = 111.195
CREDENTIALS_PATH = os.path.expanduser("~/.config/earthengine/credentials")
RETRY_WORDS = ("timed out", "timeout", "too many", "concurrent", "quota", "unavailable", "503", "429")
SIGN_IN_WORDS = ("authorize", "authenticate", "credentials", "invalid_grant", "reauth", "refresh")


# -- Earth Engine connection ---------------------------------------------------


def _read_credentials():
    try:
        with open(CREDENTIALS_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def has_credentials():
    """True when an Earth Engine sign-in is saved on this computer (no network)."""
    return bool(_read_credentials().get("refresh_token"))


def stored_project():
    """Cloud project saved by the Earth Engine sign-in or plugin, if any."""
    return _read_credentials().get("project") or ""


def import_earth_engine(library_paths=()):
    """Import the Earth Engine API, also from a disabled Google Earth Engine plugin."""
    try:
        import ee

        return ee
    except ImportError:
        pass
    for path in library_paths:
        if os.path.isdir(path) and path not in sys.path:
            site.addsitedir(path)
    try:
        import ee
    except ImportError as exc:
        raise RuntimeError(
            "The Earth Engine library was not found. Install the 'Google Earth Engine' plugin "
            "(Plugins > Manage and Install Plugins); it provides the library and may stay disabled."
        ) from exc
    return ee


def connect(ee, project, report):
    if ee.data.is_initialized() and getattr(ee.data, "_cloud_api_user_project", None) == project:
        return
    report(3.0, "Connecting to Google Earth Engine...")
    try:
        ee.Initialize(project=project)
    except Exception as exc:
        text = str(exc)
        if any(word in text.lower() for word in SIGN_IN_WORDS):
            raise RuntimeError(
                "The Google Earth Engine sign-in is missing or expired. Sign in again with "
                f"Plugins > Google Earth Engine > Sign-in, then press Run.\n\nDetails: {text}"
            ) from exc
        raise RuntimeError(
            f"Google Earth Engine could not start with the Cloud project '{project}'. Check that "
            f"the project ID is right and registered for Earth Engine.\n\nDetails: {text}"
        ) from exc


# -- Earth Engine requests -----------------------------------------------------


def _area_km2(geometry):
    scale = math.cos(math.radians(geometry.centroid.y))
    return geometry.area * KM_PER_DEGREE**2 * scale


def openet_scale(area_km2):
    """Pixel size (m) for the OpenET area mean."""
    return int(max(OPENET_NATIVE_M, math.sqrt(area_km2 * 1e6 / TARGET_PIXELS)))


def simplified(geometry):
    """Fewer vertices for a small request; the area mean barely changes."""
    tolerance = 0.0005  # degrees, about 50 m
    result = geometry
    while shapely.get_num_coordinates(result) > MAX_VERTICES:
        candidate = geometry.simplify(tolerance, preserve_topology=True)
        if candidate.is_empty:
            break
        result = candidate
        tolerance *= 2
    return result


def openet_collection(year):
    """(version, asset) of the OpenET collection used for ``year``."""
    return next((version, asset) for first, version, asset in OPENET_COLLECTIONS if year >= first)


def fetch_year(ee, product, region, year, scale):
    """Area means of every image of one product and year (one request)."""
    spec = PRODUCTS[product]
    asset = openet_collection(year)[1] if product == OPENET else spec["collection"]
    collection = (
        ee.ImageCollection(asset)
        .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
        .filterBounds(region)
        .select(spec["bands"])
    )

    def area_mean(image, day, grid):
        coverage = image.select(spec["bands"][0]).mask().rename("valid_fraction")
        stats = image.addBands(coverage).reduceRegion(
            reducer=ee.Reducer.mean(), geometry=region, maxPixels=1e10, tileScale=4, **grid
        )
        return ee.Feature(None, stats).set("date", day)

    if product == MODIS:

        def modis_mean(image):
            # Native 500 m grid: on coarser levels fill codes would mix with real values.
            values = image.updateMask(image.lt(MODIS_FILL)).multiply(MODIS_SCALE)
            grid = {"crs": image.select("ET").projection()}
            return area_mean(values, image.date().format("YYYY-MM-dd"), grid)

        features = collection.map(modis_mean)
    else:
        # OpenET has one image per MGRS grid zone and month: mosaic the zones of each month.
        months = (
            collection.aggregate_array("system:time_start")
            .map(lambda time: ee.Date(time).format("YYYY-MM-01"))
            .distinct()
        )

        def openet_mean(month):
            start = ee.Date(month)
            image = collection.filterDate(start, start.advance(1, "month")).mosaic()
            return area_mean(image, month, {"scale": scale})

        features = ee.FeatureCollection(months.map(openet_mean))
    return [dict(feature["properties"]) for feature in features.getInfo()["features"]]


def _with_retries(function, is_canceled, sleep=time.sleep):
    last = None
    for attempt in range(MAX_ATTEMPTS):
        if is_canceled():
            raise InterruptedError("Evapotranspiration extraction was canceled.")
        try:
            return function()
        except Exception as exc:  # ee.EEException and HTTP errors
            last = exc
            if not any(word in str(exc).lower() for word in RETRY_WORDS):
                raise
            sleep(min(2 ** (attempt + 1), 30))
    raise RuntimeError(f"Google Earth Engine did not answer after {MAX_ATTEMPTS} attempts: {last}")


# -- Monthly tables ------------------------------------------------------------


def _number(value, default=np.nan):
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def openet_to_monthly(records):
    spec = PRODUCTS[OPENET]
    rows = {}
    for record in records:
        month = pd.Timestamp(record["date"]).to_period("M").to_timestamp()
        row = {column: _number(record.get(band)) for band, column in zip(spec["bands"], spec["columns"])}
        row[spec["valid"]] = _number(record.get("valid_fraction"), default=0.0)
        row["openet_version"] = record.get("version", "")
        rows[month] = row
    numeric = spec["columns"] + [spec["valid"]]
    table = pd.DataFrame.from_dict(rows, orient="index", columns=numeric + ["openet_version"])
    return table.astype(dict.fromkeys(numeric, float))


def modis_to_monthly(records):
    """Spread 8-day MODIS totals evenly over their days and sum by month.

    A month stays empty unless every one of its days has a value. The last
    composite of each year is shorter (5 or 6 days) and ends on 31 December.
    """
    spec = PRODUCTS[MODIS]
    columns = spec["columns"] + [spec["valid"]]
    days = {}
    for record in records:
        start = pd.Timestamp(record["date"])
        end = min(start + pd.Timedelta(days=MODIS_COMPOSITE_DAYS), pd.Timestamp(start.year + 1, 1, 1))
        length = (end - start).days
        values = (
            _number(record.get("ET")) / length,
            _number(record.get("PET")) / length,
            _number(record.get("valid_fraction"), default=0.0),
        )
        for offset in range(length):
            days[start + pd.Timedelta(days=offset)] = values
    if not days:
        return pd.DataFrame(columns=columns, dtype=float)
    daily = pd.DataFrame.from_dict(days, orient="index", columns=["et", "pet", "valid"]).sort_index()
    grouped = daily.groupby(daily.index.to_period("M"))
    monthly = pd.DataFrame({"et": grouped["et"].sum(), "pet": grouped["pet"].sum(), "valid": grouped["valid"].mean()})
    days_in_month = monthly.index.days_in_month
    for column in ("et", "pet"):
        monthly.loc[grouped[column].count() < days_in_month, column] = np.nan
    monthly.columns = columns
    monthly.index = monthly.index.to_timestamp()
    return monthly


# -- Extraction ----------------------------------------------------------------


def extract_et(settings, aoi_geometry, progress=None, is_canceled=None, ee_module=None, fetcher=None):
    """Write the monthly basin-mean ET table. ``fetcher(product, year)`` replaces Earth Engine in tests."""
    start_year, end_year = int(settings["start_year"]), int(settings["end_year"])
    products = [p for p in (OPENET, MODIS) if p in settings["products"]]
    if not products:
        raise ValueError("Select OpenET, MODIS, or both.")
    if start_year > end_year:
        raise ValueError("The first year must be on or before the last year.")
    is_canceled = is_canceled or (lambda: False)
    output_dir = Path(settings["output_dir"]) / "evapotranspiration"
    area_km2 = _area_km2(aoi_geometry)

    def report(percentage, message):
        if progress:
            progress(percentage, message)

    if fetcher is None:
        ee = ee_module
        connect(ee, settings["project"], report)
        region = ee.Geometry(shapely.geometry.mapping(simplified(aoi_geometry)), None, False)
        scale = openet_scale(area_km2)

        def fetcher(product, year):
            return fetch_year(ee, product, region, year, scale)

    jobs = [(product, year) for product in products for year in range(start_year, end_year + 1)]
    lock = threading.Lock()
    done = [0]
    message = "Reading monthly evapotranspiration from Earth Engine: {} of {} product-years"
    report(5.0, message.format(0, len(jobs)))

    def run(job):
        product, year = job
        result = _with_retries(lambda: fetcher(product, year), is_canceled)
        with lock:
            done[0] += 1
            count = done[0]
        report(5.0 + 90.0 * count / len(jobs), message.format(count, len(jobs)))
        return result

    pool = ThreadPoolExecutor(max_workers=YEAR_WORKERS)
    try:
        results = list(pool.map(run, jobs))
    finally:
        # After a failure or Cancel, skip the requests that have not started.
        pool.shutdown(wait=True, cancel_futures=True)

    records = {product: [] for product in products}
    warnings = []
    for (product, year), result in zip(jobs, results):
        if not result:
            warnings.append(f"{PRODUCTS[product]['label']}: no images for {year}.")
        if product == OPENET:
            for record in result:
                record["version"] = openet_collection(year)[0]
        records[product].extend(result)
    newest_first_year = OPENET_COLLECTIONS[0][0]
    if OPENET in products and start_year < newest_first_year:
        warnings.append(
            f"OpenET: years before {newest_first_year} come from Collection v2.0, which covers only "
            "the western US (UTM zones 10-15, west of about 90° W); see openet_version."
        )

    months = pd.date_range(f"{start_year}-01-01", f"{end_year}-12-01", freq="MS")
    table = pd.DataFrame(index=months)
    if OPENET in products:
        table = table.join(openet_to_monthly(records[OPENET]))
    if MODIS in products:
        table = table.join(modis_to_monthly(records[MODIS]))
    for product in products:
        spec = PRODUCTS[product]
        values = table[spec["columns"][0]]
        missing = int(values.isna().sum())
        if missing:
            warnings.append(f"{spec['label']}: {missing} month(s) without a value (blank cells in et_monthly.csv).")
        low = int(((table[spec["valid"]] < LOW_COVERAGE) & values.notna()).sum())
        if low:
            warnings.append(
                f"{spec['label']}: in {low} month(s), valid pixels cover less than half of the area "
                f"(see {spec['valid']})."
            )

    output_dir.mkdir(parents=True, exist_ok=True)
    table.insert(0, "month", table.index.month)
    table.insert(0, "year", table.index.year)
    table.insert(0, "date", table.index.strftime("%Y-%m-%d"))
    monthly_path = output_dir / "et_monthly.csv"
    table.round(4).to_csv(monthly_path, index=False)

    totals = [c for c in ("openet_et_mm", "modis_et_mm", "modis_pet_mm") if c in table]
    annual = table.groupby("year")[totals].sum(min_count=12)
    annual_path = output_dir / "et_annual.csv"
    annual.round(1).to_csv(annual_path)

    warnings_file = write_warnings(output_dir, warnings)
    _write_readme(output_dir / "README.txt", settings, products, area_km2, warnings)
    report(100.0, "Evapotranspiration extraction complete")
    return {
        "kind": "et",
        "output_dir": output_dir,
        "monthly": monthly_path,
        "annual": annual_path,
        "months": len(table),
        "warnings": warnings,
        "warnings_file": warnings_file,
    }


def _write_readme(path, settings, products, area_km2, warnings):
    lines = [
        "Monthly basin-mean evapotranspiration retrieved with Weather Extractor",
        f"Data accessed: {date.today().isoformat()} through Google Earth Engine",
        f"Period: {settings['start_year']}-01 through {settings['end_year']}-12",
        f"Area of interest: {area_km2:,.1f} km2 (one series: mean over the whole area)",
        "Products: " + "; ".join(PRODUCTS[p]["description"] for p in products),
        "",
        "Files:",
        "- et_monthly.csv: one row per month (date = first day of the month).",
        "- et_annual.csv: annual totals, only for years with 12 months of data.",
        "",
        "Columns (mm per month):",
    ]
    if OPENET in products:
        lines += [
            "- openet_et_mm: OpenET ensemble actual ET (mean of the models kept after outlier removal).",
            "- openet_et_min_mm, openet_et_max_mm: lowest and highest model kept in the ensemble.",
            "- openet_models: number of models in the ensemble (area mean).",
            "- openet_version: OpenET Collection used. v2.1 covers the whole CONUS from 2016;",
            "  v2.0 (before 2016) covers only the western US (UTM zones 10-15, west of about 90 W).",
            "  Each month is the mosaic of the OpenET tiles (MGRS grid zones) over the area.",
        ]
    if MODIS in products:
        lines += [
            "- modis_et_mm, modis_pet_mm: MOD16A2GF actual and potential ET. 8-day totals are spread",
            "  evenly over their days and summed by month. Water, urban, and barren pixels (fill codes",
            "  above 32700) are excluded.",
        ]
    lines += [
        "- *_valid_fraction: fraction of the area with valid pixels; low values mean the mean",
        "  represents only part of the area.",
        "",
        "Sources and citation:",
    ]
    if OPENET in products:
        lines += [
            "- Melton, F.S., et al. (2022). OpenET: Filling a critical data gap in water management for",
            "  the western United States. JAWRA 58(6), 971-994. https://doi.org/10.1111/1752-1688.12956",
            "  OpenET data license: CC-BY-4.0 (https://etdata.org).",
        ]
    if MODIS in products:
        lines += [
            "- Running, S., Mu, Q., Zhao, M., Moreno, A. (2021). MODIS/Terra Net Evapotranspiration",
            "  Gap-Filled 8-Day L4 Global 500m SIN Grid V061. NASA LP DAAC.",
            "  https://doi.org/10.5067/MODIS/MOD16A2GF.061",
        ]
    if warnings:
        lines += ["", f"Warnings: {len(warnings)}, listed in warnings.txt."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
