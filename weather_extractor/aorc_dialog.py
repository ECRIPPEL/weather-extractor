from datetime import date
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsMapLayer,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsProjectionSelectionWidget

from .aorc_map_tool import AORCPolygonTool
from .et_tab import EtTab
from .usgs_tab import UsgsTab


POLYGON_FILE_FILTER = (
    "Polygon files (*.shp *.gpkg *.geojson *.json);;Shapefiles (*.shp);;"
    "GeoPackage (*.gpkg);;GeoJSON (*.geojson *.json)"
)
POLYGON_FILE_SUFFIXES = (".shp", ".gpkg", ".geojson", ".json")


TAB_AREA, TAB_AORC, TAB_USGS, TAB_ET = range(4)
TAB_TITLES = ["1. Area of Interest", "2. Gridded Data", "3. Observed Data", "4. Remote Sensing Data"]
# Shown above the tabs; short enough for two lines, so the window keeps its height.
TAB_DESCRIPTIONS = [
    "Define the area of interest, output folder, and timezone first, then choose the data in "
    "the other tabs. The elevation raster is optional.",
    "NOAA AORC gridded weather: hourly, ~800 m, from 1979; daily and hourly series, FAO-56 "
    "reference ET, and SWAT+ files.",
    "Observed data: USGS water quantity and quality, EPA Water Quality Portal samples, and "
    "NOAA GHCN daily and hourly rain gauges.",
    "Remote sensing evapotranspiration: monthly OpenET and MODIS means over the area of interest.",
]
DEFAULT_TIMEZONE = "Etc/GMT+6"
US_STANDARD_TIMES = {
    -5: "US Eastern Standard Time (GMT-5)",
    -6: "US Central Standard Time (GMT-6)",
    -7: "US Mountain Standard Time (GMT-7)",
    -8: "US Pacific Standard Time (GMT-8)",
}


def _timezone_choices():
    """Fixed UTC offsets only: local standard time, never daylight saving time.

    IANA "Etc/GMT+6" means UTC-06:00, so the sign is reversed for display.
    """
    choices = [(f"Etc/GMT{-hours:+d}", label) for hours, label in US_STANDARD_TIMES.items()]
    choices.append(("UTC", "UTC (GMT+0)"))
    for hours in range(-12, 15):
        if hours != 0 and hours not in US_STANDARD_TIMES:
            choices.append((f"Etc/GMT{-hours:+d}", f"GMT{hours:+d}"))
    return choices


def _format_duration(seconds):
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min {seconds:02d} s"
    return f"{seconds} s"


def crs_definition(crs):
    """Return a CRS string that both QGIS and GDAL understand."""
    authid = crs.authid()
    if authid.upper().startswith("EPSG:"):
        return authid
    try:
        from qgis.core import Qgis

        return crs.toWkt(Qgis.CrsWktVariant.PreferredGdal)
    except AttributeError:
        return crs.toWkt(QgsCoordinateReferenceSystem.WKT_PREFERRED_GDAL)


class AORCDialog(QDialog):
    extractionRequested = pyqtSignal(object)
    usgsCheckRequested = pyqtSignal(object)
    cancelRequested = pyqtSignal()

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle("Weather Extractor")
        self.setMinimumWidth(540)

        layout = QVBoxLayout(self)
        # Describes the data of the selected tab.
        self.introduction = QLabel(TAB_DESCRIPTIONS[TAB_AREA])
        self.introduction.setWordWrap(True)
        # Two lines, fixed: Qt otherwise sizes the window for an estimated three lines.
        self.introduction.setFixedHeight(2 * self.introduction.fontMetrics().lineSpacing() + 2)
        layout.addWidget(self.introduction)

        aoi_group = QGroupBox("Area of interest (required)")
        aoi_form = QFormLayout(aoi_group)

        self.aoi_source = QComboBox()
        self.aoi_source.addItem("Load a polygon file from disk", "shapefile")
        self.aoi_source.addItem("Choose a polygon layer from the project", "selected_layer")
        self.aoi_source.addItem("Draw a polygon on the map", "draw")
        self.aoi_source.addItem("Enter bounding coordinates", "bounds")
        self.aoi_source.setToolTip(
            "The area of interest"
        )

        self.layer_combo = QComboBox()
        self.layer_combo.addItem("Select a polygon layer", None)
        for layer in QgsProject.instance().mapLayers().values():
            if (
                layer.isValid()
                and layer.type() == QgsMapLayer.VectorLayer
                and layer.geometryType() == QgsWkbTypes.PolygonGeometry
            ):
                self.layer_combo.addItem(layer.name(), layer.id())
        self.layer_combo.setToolTip(
            "It can be a mask layer that defines the area of interest"
        )

        self.shapefile_edit = QLineEdit()
        self.shapefile_edit.setPlaceholderText("Select a polygon file (.shp, .gpkg, .geojson)")
        self.shapefile_button = QPushButton("Browse...")
        self.shapefile_button.clicked.connect(self._browse_shapefile)
        self._set_tooltip(
            "Polygon file (shapefile, GeoPackage, or GeoJSON)",
            self.shapefile_edit,
            self.shapefile_button,
        )

        self.draw_button = QPushButton("Draw polygon on map...")
        self.draw_button.clicked.connect(self._start_drawing)
        self.draw_button.setToolTip(
            "Click to add vertex; Right-click or press Enter to finish; "
            "Backspace removes the last vertex; Esc cancels."
        )

        self.bounds_widget = QWidget()
        bounds_grid = QGridLayout(self.bounds_widget)
        bounds_grid.setContentsMargins(0, 0, 0, 0)
        self.west_lon = self._coordinate_input()
        self.south_lat = self._coordinate_input()
        self.east_lon = self._coordinate_input()
        self.north_lat = self._coordinate_input()
        bounds_grid.addWidget(QLabel("West"), 0, 0)
        bounds_grid.addWidget(self.west_lon, 0, 1)
        bounds_grid.addWidget(QLabel("South"), 0, 2)
        bounds_grid.addWidget(self.south_lat, 0, 3)
        bounds_grid.addWidget(QLabel("East"), 1, 0)
        bounds_grid.addWidget(self.east_lon, 1, 1)
        bounds_grid.addWidget(QLabel("North"), 1, 2)
        bounds_grid.addWidget(self.north_lat, 1, 3)
        self._set_tooltip(
            "Box edges, in the units of the selected CRS",
            self.west_lon,
            self.south_lat,
            self.east_lon,
            self.north_lat,
        )
        self.bounds_crs = QgsProjectionSelectionWidget()
        self.bounds_crs.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        self.bounds_crs.setToolTip("CRS of the box coordinates")
        self.bounds_crs.crsChanged.connect(self._update_bounds_ranges)
        bounds_grid.addWidget(QLabel("CRS"), 2, 0)
        bounds_grid.addWidget(self.bounds_crs, 2, 1, 1, 3)
        self._update_bounds_ranges()
        self._draw_tool = None
        self._loaded_file = None

        # One form row whose label and field follow the selected input type.
        self.source_pages = {
            "shapefile": (
                "Polygon file (required)",
                self._row_widget(self.shapefile_edit, self.shapefile_button),
            ),
            "selected_layer": ("Polygon layer (required)", self.layer_combo),
            "draw": ("Map drawing", self.draw_button),
            "bounds": ("Bounding coordinates", self.bounds_widget),
        }
        self.source_label = QLabel()
        self.source_stack = QStackedWidget()
        for _, widget in self.source_pages.values():
            self.source_stack.addWidget(widget)

        self.dem_path = QLineEdit()
        self.dem_path.setPlaceholderText("Optional: raster used to sample station elevation")
        self.dem_button = QPushButton("Browse...")
        self.dem_button.clicked.connect(self._browse_dem)
        self._set_tooltip(
            "Optional DEM for station elevation (-99 if empty)",
            self.dem_path,
            self.dem_button,
        )

        aoi_form.addRow("Input layer", self.aoi_source)
        aoi_form.addRow(self.source_label, self.source_stack)
        aoi_form.addRow(
            "Elevation raster (optional)", self._row_widget(self.dem_path, self.dem_button)
        )

        period_group = QGroupBox("Period, time step, and grid")
        period_form = QFormLayout(period_group)

        latest_complete_year = max(1979, date.today().year - 1)
        self.start_year = QSpinBox()
        self.start_year.setRange(1979, latest_complete_year)
        self.start_year.setValue(max(1979, latest_complete_year - 1))
        self.end_year = QSpinBox()
        self.end_year.setRange(1979, latest_complete_year)
        self.end_year.setValue(latest_complete_year)
        self.start_year.setToolTip("From 1979 (data starts on 1979-02-01)")
        self.end_year.setToolTip(
            "Up to the latest complete year"
        )

        # CSV time steps; both can be written in one run (same hourly values).
        self.daily_step = QCheckBox("Daily")
        self.daily_step.setChecked(True)
        self.daily_step.setToolTip("Summarized per local day")
        self.hourly_step = QCheckBox("Hourly")
        self.hourly_step.setToolTip("Original AORC values")
        self.timestep_widget = self._row_widget(self.daily_step, self.hourly_step)
        self.timestep_widget.layout().addStretch(1)

        self.timezone = QComboBox()
        for value, label in _timezone_choices():
            self.timezone.addItem(label, value)
        self.timezone.setCurrentIndex(self.timezone.findData(DEFAULT_TIMEZONE))
        self.timezone.setToolTip(
            "Local standard time of the AORC and NOAA hourly data (no daylight saving time)"
        )

        self.grid_mode = QComboBox()
        self.grid_mode.addItem("Original AORC grid (~800 m)", ("original", None))
        self.grid_mode.addItem("5 km grid (mean of AORC cells)", ("block", 5.0))
        self.grid_mode.addItem("10 km grid (mean of AORC cells)", ("block", 10.0))
        self.grid_mode.addItem("50 km grid (mean of AORC cells)", ("block", 50.0))
        self.grid_mode.addItem("Custom grid size (mean of AORC cells)", ("block", None))
        self.grid_mode.addItem("Rain gauges of tab 3 (nearest AORC cell)", ("gauges", None))
        self.grid_mode.addItem("Single station (AOI centroid)", ("single", None))
        self.grid_mode.setCurrentIndex(self.grid_mode.count() - 1)
        self.grid_mode.setToolTip(
            "Original: one station per AORC cell (~800 m); km grids: mean of the cells in "
            "each block; Rain gauges: the cell of each precipitation station checked in tab 3; "
            "Single station: the cell at the AOI centroid (fastest)"
        )
        self.block_km = QDoubleSpinBox()
        self.block_km.setRange(1.0, 500.0)
        self.block_km.setDecimals(1)
        self.block_km.setValue(25.0)
        self.block_km.setSuffix(" km")
        self.block_km.setToolTip(
            "Block width (approximate)"
        )
        self.grid_widget = self._row_widget(self.grid_mode, self.block_km)

        self.parallel_workers = QSpinBox()
        self.parallel_workers.setRange(1, max(1, os.cpu_count() or 1))
        self.parallel_workers.setValue(min(2, self.parallel_workers.maximum()))
        self.parallel_workers.setToolTip(
            "Number of years downloaded simultaneously; higher is faster but uses more memory"
        )

        period_form.addRow("First year", self.start_year)
        period_form.addRow("Last year", self.end_year)
        period_form.addRow("Time step", self.timestep_widget)
        period_form.addRow("Grid size", self.grid_widget)
        period_form.addRow("Parallel processing", self.parallel_workers)

        output_group = QGroupBox("Output (required)")
        output_form = QFormLayout(output_group)
        aorc_output_group = QGroupBox("Variables and export formats")
        aorc_output_form = QFormLayout(aorc_output_group)

        self.output_dir = QLineEdit()
        self.output_dir.setPlaceholderText("Choose where results will be saved")
        self.output_button = QPushButton("Browse...")
        self.output_button.clicked.connect(self._browse_output)
        self._set_tooltip(
            "Where results are saved (existing files are overwritten)",
            self.output_dir,
            self.output_button,
        )

        self.target_crs = QgsProjectionSelectionWidget()
        self.target_crs.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        self.target_crs.setToolTip(
            "CRS of the output station layers"
        )

        self.variable_checks = {}
        variables_widget = QWidget()
        variables_grid = QGridLayout(variables_widget)
        variables_grid.setContentsMargins(0, 0, 0, 0)
        for index, (key, label, tooltip) in enumerate(
            [
                ("pcp", "Precipitation", "mm; hourly values or daily sums"),
                ("tmp", "Temperature", "°C at 2 m; hourly values or daily maximum and minimum"),
                ("slr", "Solar radiation", "MJ/m²; hourly or daily sums"),
                (
                    "hmd",
                    "Humidity",
                    "Relative humidity (0 to 1); hourly values or daily means",
                ),
                (
                    "wnd",
                    "Wind",
                    "m/s at 10 m; hourly values or daily means",
                ),
                (
                    "eto",
                    "Reference ET",
                    "FAO-56 Penman-Monteith grass reference ET (mm), calculated from AORC "
                    "temperature, humidity, pressure, solar radiation, and wind",
                ),
            ]
        ):
            check = QCheckBox(label)
            check.setChecked(key != "eto")
            check.setToolTip(tooltip)
            self.variable_checks[key] = check
            variables_grid.addWidget(check, index // 3, index % 3)
        variables_label = QLabel("Climate variables")
        variables_label.setToolTip(
            "Fewer variables, faster download"
        )

        self.write_csv = QCheckBox("CSV files")
        self.write_csv.setChecked(True)
        self.write_csv.setToolTip(
            "One table per station"
        )
        self.write_swat = QCheckBox("SWAT+ climate files")
        self.write_swat.setChecked(False)
        self.write_swat.setToolTip(
            "Optional, for SWAT+ users: .pcp, .tmp, .slr, .hmd, .wnd and .cli files"
        )
        self.swat_pcp_step = QComboBox()
        self.swat_pcp_step.addItem("Daily", "daily")
        self.swat_pcp_step.addItem("Hourly", "hourly")
        self.swat_pcp_step.setToolTip("Time step of the .pcp files (hourly: tstep 24); the others stay daily")
        self.write_csv.toggled.connect(self.timestep_widget.setEnabled)
        self.write_swat.toggled.connect(self.swat_pcp_step.setEnabled)
        self.swat_pcp_step.setEnabled(False)

        output_form.addRow(
            "Output folder (required)", self._row_widget(self.output_dir, self.output_button)
        )
        output_form.addRow("Target CRS", self.target_crs)
        output_form.addRow("Timezone", self.timezone)
        aorc_output_form.addRow(variables_label, variables_widget)
        aorc_output_form.addRow(
            "Export formats", self._row_widget(self.write_csv, self.write_swat)
        )
        aorc_output_form.addRow("SWAT+ precipitation", self.swat_pcp_step)

        self.usgs_tab = UsgsTab()
        self.usgs_tab.checkRequested.connect(self._usgs_check_clicked)
        self.usgs_tab.selectionChanged.connect(self._update_run_buttons)
        self.usgs_tab.run_button.clicked.connect(lambda: self._run_clicked("usgs"))

        # Each data tab has its own Run button; it turns into Cancel while that tab runs.
        self.aorc_run = QPushButton("Run")
        self.aorc_run.setToolTip("Extract AORC weather for the area of tab 1")
        self.aorc_run.clicked.connect(lambda: self._run_clicked("aorc"))
        self.et_tab = EtTab()
        self.et_tab.run_button.clicked.connect(lambda: self._run_clicked("et"))
        self.run_buttons = {
            "aorc": self.aorc_run,
            "usgs": self.usgs_tab.run_button,
            "et": self.et_tab.run_button,
        }

        # Tab 1 is shared by every data source; the other tabs hold source options.
        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab_page(aoi_group, output_group), TAB_TITLES[TAB_AREA])
        self.tabs.addTab(
            self._tab_page(period_group, aorc_output_group, footer=self.aorc_run), TAB_TITLES[TAB_AORC]
        )
        self.tabs.addTab(self.usgs_tab, TAB_TITLES[TAB_USGS])
        self.tabs.addTab(self.et_tab, TAB_TITLES[TAB_ET])
        # Widen the window rather than hide or clip tab titles.
        self.tabs.setUsesScrollButtons(False)
        bar = self.tabs.tabBar()
        titles = sum(bar.tabSizeHint(i).width() for i in range(bar.count()))
        margins = layout.contentsMargins()
        self.setMinimumWidth(max(540, titles + margins.left() + margins.right() + 4))
        self.tabs.currentChanged.connect(lambda index: self.introduction.setText(TAB_DESCRIPTIONS[index]))

        self.buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.buttons.rejected.connect(self.reject)

        self.progress_label = QLabel("Ready to extract.")
        self.progress_label.setWordWrap(True)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)

        layout.addWidget(self.tabs)
        layout.addWidget(self.progress_label)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.buttons)
        self._input_groups = (aoi_group, output_group, period_group, aorc_output_group)
        self._running = False
        self._running_kind = None
        self._started = time.monotonic()
        self.aoi_source.currentIndexChanged.connect(self._update_source_widgets)
        self.grid_mode.currentIndexChanged.connect(self._update_grid_widgets)
        # A new area makes the last USGS availability check obsolete.
        self.aoi_source.currentIndexChanged.connect(self.usgs_tab.invalidate)
        self.layer_combo.currentIndexChanged.connect(self.usgs_tab.invalidate)
        self.shapefile_edit.textChanged.connect(self.usgs_tab.invalidate)
        self.bounds_crs.crsChanged.connect(self.usgs_tab.invalidate)
        for field in (self.west_lon, self.south_lat, self.east_lon, self.north_lat):
            field.valueChanged.connect(self.usgs_tab.invalidate)
        self._update_source_widgets()
        self._update_grid_widgets()
        self._update_run_buttons()

    @staticmethod
    def _tab_page(*widgets, footer=None):
        page = QWidget()
        column = QVBoxLayout(page)
        for widget in widgets:
            column.addWidget(widget)
        column.addStretch(1)
        if footer is not None:
            row = QHBoxLayout()
            row.addStretch(1)
            row.addWidget(footer)
            column.addLayout(row)
        return page

    @staticmethod
    def _set_tooltip(text, *widgets):
        for widget in widgets:
            widget.setToolTip(text)

    @staticmethod
    def _row_widget(*widgets):
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        for widget in widgets:
            row.addWidget(widget)
        return container

    def _update_run_buttons(self):
        idle = not self._running and not self.usgs_tab._checking
        ready = {"aorc": idle, "usgs": idle and self.usgs_tab.can_run(), "et": idle}
        for kind, button in self.run_buttons.items():
            running_here = self._running_kind == kind
            button.setText("Cancel" if running_here else "Run")
            button.setEnabled(running_here or ready[kind])

    def _usgs_check_clicked(self):
        if self.usgs_tab._checking:
            self.cancelRequested.emit()
            return
        if not self.validate_area():
            return
        settings = self.get_settings()
        if settings["usgs"]["start_date"] > settings["usgs"]["end_date"]:
            self._invalid(TAB_USGS, "Invalid period", "The start date must be on or before the end date.")
            return
        self.usgsCheckRequested.emit(settings)

    def _run_clicked(self, kind):
        if self._running:
            if self._running_kind == kind:
                self.cancelRequested.emit()
                self.progress_label.setText(
                    "Cancellation requested. Waiting for the active download to stop..."
                )
            return
        if kind == "usgs":
            valid = self.validate_area() and self.validate_usgs() and self.usgs_tab.confirm_run()
        elif kind == "et":
            valid = self.validate_area() and self.validate_et()
        else:
            valid = self.validate()
        if not valid:
            return
        self.set_running(True, kind)
        self.extractionRequested.emit(dict(self.get_settings(), kind=kind))

    def set_running(self, running, kind=None):
        self._running = running
        self._running_kind = kind if running else None
        for group in self._input_groups:
            group.setEnabled(not running)
        self.usgs_tab.set_running(running)
        self.et_tab.set_running(running)
        self._update_run_buttons()
        if running:
            self._started = time.monotonic()
            self.progress_bar.setValue(0)
            self.progress_label.setText("Preparing extraction...")

    def set_checking(self, checking):
        """USGS availability check running in the background."""
        self.usgs_tab.set_checking(checking)
        for group in self._input_groups[:2]:
            group.setEnabled(not checking)
        self._update_run_buttons()

    def set_progress(self, percentage, message):
        self.progress_bar.setValue(max(0, min(100, round(percentage))))
        if self._running:
            elapsed = time.monotonic() - self._started
            message += f" | Elapsed {_format_duration(elapsed)}"
            # Early estimates are unreliable, so wait for a few percent.
            if 3 <= percentage < 100:
                remaining = elapsed * (100 - percentage) / percentage
                message += f", about {_format_duration(remaining)} left"
        self.progress_label.setText(message)

    @staticmethod
    def _coordinate_input():
        field = QDoubleSpinBox()
        field.setSingleStep(0.1)
        field.setKeyboardTracking(False)
        return field

    def _update_bounds_ranges(self, *_):
        crs = self.bounds_crs.crs()
        geographic = not crs.isValid() or crs.isGeographic()
        for field, limit in (
            (self.west_lon, 180.0),
            (self.east_lon, 180.0),
            (self.south_lat, 90.0),
            (self.north_lat, 90.0),
        ):
            if geographic:
                field.setDecimals(6)
                field.setRange(-limit, limit)
                field.setSingleStep(0.1)
            else:
                field.setDecimals(2)
                field.setRange(-1e9, 1e9)
                field.setSingleStep(1000.0)

    def _browse_shapefile(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select AOI polygon file", "", POLYGON_FILE_FILTER
        )
        if path:
            self.shapefile_edit.setText(path)
            self._load_shapefile(path)

    def _load_shapefile(self, path):
        layer = QgsVectorLayer(path, Path(path).stem, "ogr")
        if not layer.isValid() or layer.geometryType() != QgsWkbTypes.PolygonGeometry:
            QMessageBox.warning(self, "Invalid AOI", "Choose a readable polygon file.")
            return
        if not layer.crs().isValid():
            QMessageBox.warning(self, "Missing CRS", "The AOI file must have a defined CRS.")
            return
        QgsProject.instance().addMapLayer(layer)
        # Reuse this project layer at run time so the file is not added twice.
        self._loaded_file = (path, layer.id())
        self._select_polygon_layer(layer, switch_source=False)
        self.iface.setActiveLayer(layer)
        self.iface.zoomToActiveLayer()

    def _select_polygon_layer(self, layer, switch_source=True):
        index = self.layer_combo.findData(layer.id())
        if index < 0:
            self.layer_combo.addItem(layer.name(), layer.id())
            index = self.layer_combo.count() - 1
        self.layer_combo.setCurrentIndex(index)
        if switch_source:
            self.aoi_source.setCurrentIndex(self.aoi_source.findData("selected_layer"))

    def _start_drawing(self):
        self.hide()
        self.iface.messageBar().pushInfo(
            "Weather Extractor",
            "Click to add each vertex of the AOI. Right-click or Enter finishes, "
            "Backspace removes the last vertex, Esc cancels.",
        )
        self._draw_tool = AORCPolygonTool(self.iface, self._drawing_finished)
        self.iface.mapCanvas().setMapTool(self._draw_tool)

    def _drawing_finished(self, layer):
        if layer is not None:
            self._select_polygon_layer(layer)
        self.show()
        self.raise_()
        self.activateWindow()

    def _browse_output(self):
        path = QFileDialog.getExistingDirectory(self, "Select output folder")
        if path:
            self.output_dir.setText(path)

    def _browse_dem(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select elevation raster", "", "Raster datasets (*.tif *.tiff *.img);;All files (*)"
        )
        if path:
            self.dem_path.setText(path)

    def _update_source_widgets(self):
        label, current = self.source_pages[self.aoi_source.currentData()]
        self.source_label.setText(label)
        for _, widget in self.source_pages.values():
            # Size the stack to the visible page only.
            policy = QSizePolicy.Preferred if widget is current else QSizePolicy.Ignored
            widget.setSizePolicy(policy, policy)
        self.source_stack.setCurrentWidget(current)
        self.source_stack.adjustSize()

    def _update_grid_widgets(self):
        mode, size = self.grid_mode.currentData()
        self.block_km.setVisible(mode == "block" and size is None)


    def get_settings(self):
        mode, size = self.grid_mode.currentData()
        source = self.aoi_source.currentData()
        layer_id = self.layer_combo.currentData()
        path = self.shapefile_edit.text().strip()
        if (
            source == "shapefile"
            and self._loaded_file is not None
            and self._loaded_file[0] == path
            and QgsProject.instance().mapLayer(self._loaded_file[1]) is not None
        ):
            # The browsed file is already a project layer; use it directly.
            source, layer_id = "selected_layer", self._loaded_file[1]
        return {
            "source": source,
            "layer_id": layer_id,
            "shapefile": path,
            "bounds": {
                "west": self.west_lon.value(),
                "south": self.south_lat.value(),
                "east": self.east_lon.value(),
                "north": self.north_lat.value(),
            },
            "bounds_crs": crs_definition(self.bounds_crs.crs()),
            "dem_path": self.dem_path.text().strip(),
            "start_year": self.start_year.value(),
            "end_year": self.end_year.value(),
            "timesteps": [
                step for step, check in (("daily", self.daily_step), ("hourly", self.hourly_step))
                if check.isChecked()
            ],
            "swat_pcp_step": self.swat_pcp_step.currentData(),
            "timezone": self.timezone.currentData(),
            "grid_mode": mode,
            "gauges": self.usgs_tab.rain_gauges() if mode == "gauges" else [],
            "block_km": size if size is not None else self.block_km.value(),
            "parallel_workers": self.parallel_workers.value(),
            "output_dir": self.output_dir.text().strip(),
            "target_crs": crs_definition(self.target_crs.crs()),
            "variables": [key for key, check in self.variable_checks.items() if check.isChecked()],
            "write_csv": self.write_csv.isChecked(),
            "write_swat": self.write_swat.isChecked(),
            "usgs": self.usgs_tab.get_settings(),
            "et": self.et_tab.get_settings(),
        }

    def _invalid(self, tab, title, text):
        """Show the tab holding the problem, then explain it."""
        self.tabs.setCurrentIndex(tab)
        QMessageBox.warning(self, title, text)
        return False

    def validate(self):
        return self.validate_area() and self.validate_aorc()

    def validate_area(self):
        """Checks for tab 1, which every data source needs."""
        settings = self.get_settings()
        if settings["source"] == "selected_layer":
            if settings["layer_id"] is None:
                return self._invalid(TAB_AREA, "No layer selected", "Please choose a polygon layer in the project.")
        elif settings["source"] == "shapefile":
            if not settings["shapefile"]:
                return self._invalid(TAB_AREA, "No file selected", "Please choose a polygon file for the area of interest.")
            if not Path(settings["shapefile"]).exists():
                return self._invalid(TAB_AREA, "File not found", "The selected polygon file does not exist.")
            if Path(settings["shapefile"]).suffix.lower() not in POLYGON_FILE_SUFFIXES:
                return self._invalid(TAB_AREA, "Invalid file", "Choose a shapefile, GeoPackage, or GeoJSON file.")
        elif settings["source"] == "draw":
            return self._invalid(TAB_AREA, "Draw the area first", "Press 'Draw polygon on map...' and draw the area before continuing.")
        elif settings["source"] == "bounds":
            if not self.bounds_crs.crs().isValid():
                return self._invalid(TAB_AREA, "Invalid CRS", "Choose the coordinate reference system of the bounding coordinates.")
            bounds = settings["bounds"]
            if bounds["west"] >= bounds["east"] or bounds["south"] >= bounds["north"]:
                return self._invalid(TAB_AREA, "Invalid bounds", "Minimum X/Y must be less than maximum X/Y.")
        if settings["dem_path"] and not Path(settings["dem_path"]).is_file():
            return self._invalid(TAB_AREA, "DEM not found", "The selected elevation raster does not exist.")
        if not settings["output_dir"]:
            return self._invalid(TAB_AREA, "No output folder", "Please choose an output folder.")
        output_path = Path(settings["output_dir"])
        if output_path.exists() and not output_path.is_dir():
            return self._invalid(TAB_AREA, "Invalid output folder", "The output path exists and is not a folder.")
        if not output_path.exists() and not output_path.parent.is_dir():
            return self._invalid(TAB_AREA, "Invalid output folder", "Choose a folder whose parent directory exists.")
        if not self.target_crs.crs().isValid():
            return self._invalid(TAB_AREA, "Invalid CRS", "Choose a target coordinate reference system.")
        return True

    def validate_usgs(self):
        """Checks for tab 3 (USGS data)."""
        usgs = self.usgs_tab
        if usgs.inventory is None:
            return self._invalid(TAB_USGS, "Check available data first", "Press 'Check available data' before downloading.")
        if not usgs.selection():
            return self._invalid(TAB_USGS, "Nothing selected", "Check at least one available box.")
        settings = usgs.get_settings()
        if settings["site_ids"] is not None and not settings["site_ids"]:
            return self._invalid(
                TAB_USGS,
                "No stations selected",
                "Select preview points on the map, or clear 'Only stations selected on the map'.",
            )
        return True

    def validate_et(self):
        """Checks for tab 4 (evapotranspiration), then the Google Earth Engine setup steps."""
        settings = self.et_tab.get_settings()
        if settings["start_year"] > settings["end_year"]:
            return self._invalid(TAB_ET, "Invalid year range", "The first year must be on or before the last year.")
        if not settings["products"]:
            return self._invalid(TAB_ET, "No product selected", "Check OpenET, MODIS, or both.")
        return self.et_tab.confirm_setup()

    def validate_aorc(self):
        """Checks for tab 2 (AORC weather)."""
        settings = self.get_settings()
        if settings["start_year"] > settings["end_year"]:
            return self._invalid(TAB_AORC, "Invalid year range", "The start year must be less than or equal to the end year.")
        try:
            ZoneInfo(settings["timezone"])
        except Exception:
            return self._invalid(TAB_AREA, "Timezone data unavailable", "The timezone database could not be loaded. Reinstall the plugin ZIP.")
        if not settings["variables"]:
            return self._invalid(TAB_AORC, "No climate variables", "Select at least one climate variable.")
        if not settings["write_csv"] and not settings["write_swat"]:
            return self._invalid(TAB_AORC, "No output selected", "Choose CSV, SWAT+ climate files, or both.")
        if settings["write_csv"] and not settings["timesteps"]:
            return self._invalid(TAB_AORC, "No time step", "Check Daily, Hourly, or both.")
        if settings["grid_mode"] == "gauges":
            if self.usgs_tab.inventory is None:
                return self._invalid(
                    TAB_USGS, "Check available data first",
                    "The rain gauges come from this tab: press 'Check available data', then run tab 2 again.",
                )
            if not settings["gauges"]:
                return self._invalid(
                    TAB_USGS, "No rain gauges selected",
                    "Keep a precipitation row checked (USGS or NOAA GHCN), with stations in the map selection if "
                    "'Only stations selected on the map' is on.",
                )
        return True
