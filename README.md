# Weather Extractor for QGIS

Weather Extractor is a QGIS plugin for downloading hydroclimatic data for teaching and research. Version 2.1.0 includes:

- **Gridded data (NOAA AORC):** hourly gridded weather (precipitation, temperature, solar radiation, humidity, wind) and FAO-56 reference evapotranspiration for the contiguous United States, from February 1979 to the latest complete year, as daily and/or hourly series.
- **Observed data (USGS, EPA, and NOAA):** USGS water quantity (streamflow, gage height, groundwater, precipitation) and water quality (sensor series and samples), other agencies' samples from the Water Quality Portal, and NOAA rain gauges (daily GHCN-Daily and hourly GHCNh).
- **Remote sensing data:** monthly actual ET from OpenET and MODIS, averaged over the area of interest, through Google Earth Engine (e.g., to close a water balance).

Outputs are CSV tables, GeoPackage station layers, and optional SWAT+ climate files.

## Install

1. In QGIS 3.40 or later, open **Plugins > Manage and Install Plugins... > Install from ZIP**.
2. Select `Weather_Extractor-2.1.0-win.zip` and choose **Install Plugin**.
3. Open **Plugins > Weather Extractor > Open Weather Extractor**.

The Windows ZIP bundles its Python libraries; no pip installation is needed. Internet access is required.

The Remote Sensing Data tab also needs the **Google Earth Engine** QGIS plugin and a Google Cloud project [registered for Earth Engine](https://code.earthengine.google.com/register).

## Workflow

**Tab 1. Area of Interest (required).** Load a polygon file (shapefile, GeoPackage, GeoJSON), choose a project layer, draw a polygon on the map, or enter bounding coordinates in any CRS. Set the output folder, target CRS, and timezone (local standard time, no daylight saving time; used for the hours and days of tabs 2 and 3). An elevation raster (DEM) is optional and gives AORC station elevations.

**Tab 2. Gridded Data (NOAA AORC).** Choose:

- Period, and the CSV time step: daily, hourly, or both (both come from the same hourly values, e.g. to compare them)
- Grid: original AORC grid (~800 m), 5/10/50 km or custom blocks (mean of AORC cells), the AORC cell of each rain gauge checked in tab 3, or a single station at the AOI centroid
- Climate variables, including reference ET (ETo), CSV and/or SWAT+ files, and the SWAT+ precipitation time step (daily or hourly)

Press **Run**. Several years are downloaded in parallel.

For the rain gauge grid, first press **Check available data** in tab 3 and keep a precipitation row checked (USGS or NOAA GHCN). Each AORC station is named after its gauge (`aorc_ghcn_usw00013894` for `GHCN-USW00013894`) and its files carry the gauge `site_id`, so AORC and observed series can be joined directly.

**Tab 3. Observed Data (USGS, EPA, and NOAA).**

1. Set the period, an optional search margin (km around the area, e.g. to include a basin outlet), and an optional free API key ([sign up](https://api.waterdata.usgs.gov/signup/)) for faster downloads.
2. Press **Check available data**. Only metadata are read; the tables then show how many stations have each dataset, and the stations appear on the map.
3. Uncheck what you do not need (the box next to a variable switches its whole row); the map updates. Optionally, select stations on the map and check **Only stations selected on the map**.
4. Press **Run**.

| Water quantity | Daily | Sub-daily | Field | Peaks |
|---|:-:|:-:|:-:|:-:|
| Streamflow | x | x | x | x |
| Streamflow, tide-filtered | x | x | | |
| Gage height | x | x | x | x |
| Estuary / ocean level | x | x | | |
| Groundwater depth below land surface | x | x | x | |
| Precipitation (USGS) | x | x | | |
| Precipitation (NOAA GHCN: daily from COOP, CoCoRaHS, airports; hourly GHCNh from airports, USCRN, COOP recording gauges) | x | x | | |
| Water velocity | x | x | x | |
| Channel geometry (width, area, mean velocity, bed material) | | | x | |

| Water quality | Sensor daily | Sensor sub-daily | Samples |
|---|:-:|:-:|:-:|
| Temperature, conductance, salinity, dissolved oxygen, pH, turbidity | x | x | x |
| Nutrients, sediment, microbiological, major ions, metals, pesticides, other organics | | | x |

Sub-daily means the USGS sensor values at their original interval (usually 15 min, sometimes 5 to 60 min), and hourly totals for NOAA GHCNh.

Not every dataset exists for every variable or place; the check shows what is available.

**Tab 4. Remote Sensing Data (OpenET and MODIS).** Choose the years and OpenET and/or MODIS, then press **Run**. The result is one monthly series: the mean over the whole area of interest.

| Product | Variables | Resolution | Period |
|---|---|---|---|
| OpenET Ensemble v2.1 | actual ET, model range | monthly, 30 m | from 2016 (CONUS); before 2016 v2.0, western US only |
| MODIS MOD16A2GF v061 | actual and potential ET | 8-day, 500 m | from 2000 (global) |

If the Google Earth Engine setup is incomplete, **Run** lists the missing steps:

1. Install and enable the Google Earth Engine plugin (**Plugins > Manage and Install Plugins**).
2. Sign in with your Google account (**Plugins > Google Earth Engine > Sign-in**).
3. Select your Cloud project (**Plugins > Google Earth Engine > Set Project**) and paste its ID in the **Google Earth Engine project** box.

## Outputs

**AORC** (output folder): one CSV per station and time step (`<station>_daily.csv`, `<station>_hourly.csv`), `swatplus_weather/` (optional), `weather_statistics.csv`, `station_locations.csv`, and `aorc_<folder>.gpkg` with station points and areas.

**Observed data** (`usgs_water_data/`): `quantity/` and `quality/` folders with daily series (`daily/`) and USGS sub-daily sensor series (`continuous/`) per station (NOAA rain gauges as `quantity/daily/ghcnd_<station>_daily.csv` and `quantity/hourly/ghcnh_<station>_hourly.csv`), `field_measurements.csv`, `annual_peaks.csv`, `channel_measurements.csv` (with mean depth = area / width), and `samples.csv`; `usgs_<folder>.gpkg` with quantity stations (triangles), channel geometry stations (dashes), and quality points (circles), with optional shapefiles; and `available_data.csv`. The `site_id` column links layers and tables.

**Evapotranspiration** (`evapotranspiration/`): `et_monthly.csv` (mm/month, with the fraction of the area with valid pixels) and `et_annual.csv` (totals for complete years).

Each run also writes a `README.txt` with its settings and conventions, and a `warnings.txt` when there are warnings. Layers are loaded into QGIS in a group named after the output folder.

## Notes

- AORC precipitation is assigned to the hour before its timestamp; daily values cover local days. Hourly SWAT+ precipitation uses `tstep` 24; other SWAT+ variables stay daily. Hourly rows of AORC and GHCNh files are labeled by the beginning of the hour, in UTC and local standard time.
- Reference ET is calculated with the FAO-56 Penman-Monteith equation for grass (Allen et al., 1998) from AORC temperature, humidity, pressure, solar radiation, and wind, on the same grid as the other variables (daily Eq. 6; hourly Eq. 53). SWAT+ files `*.pet` and `pet.cli` hold daily ETo; set the SWAT+ PET method to read values from file.
- AORC data are read in archive chunks of about 1° × 2°; large areas on the original grid take long. Start with one year and a small area.
- Daily CSV files of the Observed Data tab list every day of the period, and hourly GHCNh files every hour; -99 marks a day or hour without a value.
- USGS values include their approval status; provisional values may be revised.
- GHCN-Daily precipitation is the 24-hour total ending at the observation time of each gauge; COOP and CoCoRaHS gauges are usually read in the morning, so their day differs from a calendar day. NOAA measurement, quality, and source flags are kept.
- GHCNh hourly precipitation uses the last report of each hour, so airport special reports (METAR SPECI, running totals) are not counted twice. NOAA rain gauge site ids are `GHCN-<station>` for both daily and hourly data (2.0.0 used `GHCND-<station>`).
- Sampling points are found through the Water Quality Portal, which stopped receiving USGS samples on 2024-03-11; USGS sites first sampled after that date are not listed.
- Without an API key, USGS limits requests per hour; large downloads may be slow or partly fail (see `warnings.txt`).
- Earth Engine computes only the area mean of each image (one request per product and year); MODIS 8-day totals are split evenly over their days and summed by month on your computer. MODIS water, urban, and barren pixels have no ET and are excluded. The latest months may not be published yet.

## Data sources and citation

- NOAA. NOAA Analysis of Record for Calibration (AORC) Dataset. Accessed YYYY-MM-DD from https://registry.opendata.aws/noaa-nws-aorc/.
- Allen, R.G., Pereira, L.S., Raes, D., & Smith, M. (1998). *Crop evapotranspiration: Guidelines for computing crop water requirements.* FAO Irrigation and Drainage Paper 56. FAO, Rome.
- Fall, G., et al. (2023). The Office of Water Prediction's Analysis of Record for Calibration, version 1.1: Dataset description and precipitation evaluation. *JAWRA, 59*(6), 1246–1272. https://doi.org/10.1111/1752-1688.13143
- U.S. Geological Survey, Water Data for the Nation, accessed YYYY-MM-DD, at https://waterdata.usgs.gov/.
- Water Quality Portal, National Water Quality Monitoring Council. https://doi.org/10.5066/P9QRKUVJ
- Menne, M.J., Durre, I., Vose, R.S., Gleason, B.E., & Houston, T.G. (2012). An overview of the Global Historical Climatology Network-Daily database. *Journal of Atmospheric and Oceanic Technology, 29*, 897–910. https://doi.org/10.1175/JTECH-D-11-00103.1 (data: GHCN-Daily, Version 3, https://doi.org/10.7289/V5D21VHZ)
- Menne, M.J., Noone, S., Casey, N.W., Dunn, R.H., McNeill, S., Kantor, D., Thorne, P.W., Orcutt, K., Cunningham, S., & Risavi, N. (2023). Global Historical Climatology Network-Hourly (GHCNh). NOAA National Centers for Environmental Information. https://doi.org/10.25921/jp3d-3v19
- Melton, F.S., et al. (2022). OpenET: Filling a critical data gap in water management for the western United States. *JAWRA, 58*(6), 971–994. https://doi.org/10.1111/1752-1688.12956 (OpenET data: CC-BY-4.0)
- Running, S., Mu, Q., Zhao, M., & Moreno, A. (2021). MODIS/Terra Net Evapotranspiration Gap-Filled 8-Day L4 Global 500m SIN Grid V061. NASA LP DAAC. https://doi.org/10.5067/MODIS/MOD16A2GF.061

Outputs are derived data, not original NOAA, USGS, OpenET, or NASA data. This plugin is an independent tool, not affiliated with or endorsed by these agencies or by Google.

## License

MIT (see `LICENSE`).

## Build and tests

`weather_extractor/vendor/`, with the Python libraries that QGIS for Windows lacks, is not kept in git. From the repository root, this command downloads them with pip (versions pinned in `weather_extractor/vendor-requirements.txt`) and writes `dist/Weather_Extractor-<version>-win.zip`:

```powershell
python scripts/build_zip.py --vendor
```

Offline tests are in `tests/` and need `vendor/`. From the repository root:

```powershell
& 'C:\Program Files\QGIS 3.40.x\bin\python-qgis-ltr.bat' -m unittest discover -s tests -t . -v
```
