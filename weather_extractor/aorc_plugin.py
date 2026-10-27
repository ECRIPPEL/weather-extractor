import math
import os
from importlib.util import find_spec

from qgis.PyQt.QtCore import QObject, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices, QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox
from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFillSymbol,
    QgsGeometry,
    QgsMarkerSymbol,
    QgsProject,
    QgsRectangle,
    QgsTask,
    QgsVectorLayer,
    QgsWkbTypes,
)

from .aorc_dialog import AORCDialog


# Ask for confirmation above this many stations.
LARGE_STATION_COUNT = 200
CELLS_PER_SQUARE_DEGREE = 120 * 120
KM_PER_DEGREE = 111.195
AORC_MODULES = ["numpy", "pandas", "xarray", "zarr", "fsspec", "aiohttp", "shapely", "tzdata", "osgeo"]
USGS_MODULES = ["numpy", "pandas", "requests", "shapely", "osgeo"]
ET_MODULES = ["numpy", "pandas", "shapely"]
TASK_TITLES = {
    "aorc": "Weather Extractor: AORC weather",
    "usgs_check": "Weather Extractor: check observed data",
    "usgs": "Weather Extractor: observed data",
    "et": "Weather Extractor: evapotranspiration",
}
RESULT_TITLES = {"aorc": "AORC", "usgs": "Observed data", "et": "Evapotranspiration"}


class _TaskProgressBridge(QObject):
    progress = pyqtSignal(float, str)


class _BackgroundTask(QgsTask):
    """Runs ``function(progress=..., is_canceled=...)`` outside the GUI thread."""

    def __init__(self, kind, function, finished_callback, progress_bridge):
        super().__init__(TASK_TITLES[kind], QgsTask.CanCancel)
        self.kind = kind
        self.function = function
        self.finished_callback = finished_callback
        self.progress_bridge = progress_bridge
        self.result = None
        self.error = None

    def run(self):
        try:
            self.result = self.function(
                progress=lambda percent, message: (
                    self.setProgress(percent),
                    self.progress_bridge.progress.emit(percent, message),
                ),
                is_canceled=self.isCanceled,
            )
            return True
        except Exception as exc:
            self.error = exc
            return False

    def finished(self, successful):
        self.finished_callback(self.kind, successful, self.result, self.error)


class AORCWeatherPlugin:
    """QGIS interface for the Weather Extractor tabs."""

    MENU_NAME = "&Weather Extractor"

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.task = None
        self._task_active = False
        self.action = None
        self.dialog = None
        self.progress_bridge = None

    def initGui(self):
        self.action = QAction(
            QIcon(os.path.join(self.plugin_dir, "icon.svg")),
            "Open Weather Extractor",
            self.iface.mainWindow(),
        )
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu(self.MENU_NAME, self.action)

    def unload(self):
        self._cancel_task()
        if self.dialog is not None:
            self.dialog.close()
            self.dialog = None
        if self.action is None:
            return
        self.iface.removePluginMenu(self.MENU_NAME, self.action)
        self.action = None

    def run(self):
        if self.dialog is not None and self.dialog.isVisible():
            self.dialog.raise_()
            self.dialog.activateWindow()
            return
        dialog = AORCDialog(self.iface, self.iface.mainWindow())
        dialog.extractionRequested.connect(self._start_extraction)
        dialog.usgsCheckRequested.connect(self._start_usgs_check)
        dialog.cancelRequested.connect(self._cancel_extraction)
        dialog.finished.connect(self._dialog_closed)
        self.dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _dialog_closed(self, _result):
        self._cancel_task()
        self.dialog = None

    def _cancel_task(self):
        task = self.task
        if task is None or not self._task_active:
            self.task = None
            self._task_active = False
            return
        try:
            task.cancel()
        except RuntimeError:
            self.task = None
            self._task_active = False

    def _cancel_extraction(self):
        self._cancel_task()

    def _missing_modules(self, modules, title):
        missing = [package for package in modules if find_spec(package) is None]
        if missing:
            QMessageBox.critical(
                self.iface.mainWindow(),
                title,
                "The bundled runtime is incomplete or could not be loaded. "
                "Reinstall the Weather Extractor ZIP. Missing modules:\n\n"
                + ", ".join(missing)
                + "\n\nIf the problem persists, include this list when reporting the issue.",
            )
        return missing

    def _launch(self, kind, function, progress_slot):
        if self.action is not None:
            self.action.setEnabled(False)
        self.progress_bridge = _TaskProgressBridge(self.dialog)
        self.progress_bridge.progress.connect(progress_slot)
        self.task = _BackgroundTask(kind, function, self._task_finished, self.progress_bridge)
        self._task_active = True
        try:
            QgsApplication.taskManager().addTask(self.task)
        except Exception:
            self.task = None
            self._task_active = False
            raise

    # -- USGS availability check -------------------------------------------

    def _start_usgs_check(self, settings):
        dialog = self.dialog
        if dialog is None or self._task_active:
            return
        if self._missing_modules(USGS_MODULES, "Observed data dependencies are missing"):
            return
        try:
            geometry = self._get_aoi_geometry(settings, show=False)
        except Exception as exc:
            QMessageBox.critical(self.iface.mainWindow(), "Invalid area of interest", str(exc))
            return
        from .usgs_inventory import check_availability

        dialog.set_checking(True)
        self._launch(
            "usgs_check",
            lambda progress, is_canceled: check_availability(
                settings["usgs"], geometry, progress, is_canceled
            ),
            dialog.usgs_tab.set_check_progress,
        )

    # -- Extractions ---------------------------------------------------------

    def _start_extraction(self, settings):
        dialog = self.dialog
        if dialog is None:
            return
        if settings.get("kind") == "usgs":
            self._start_usgs_extraction(settings)
            return
        if settings.get("kind") == "et":
            self._start_et_extraction(settings)
            return
        if self._missing_modules(AORC_MODULES, "AORC dependencies are missing"):
            dialog.set_running(False)
            dialog.set_progress(0, "Bundled data packages could not be loaded.")
            return
        try:
            geometry = self._get_aoi_geometry(settings)
        except Exception as exc:
            dialog.set_running(False)
            dialog.set_progress(0, "The selected area could not be prepared.")
            QMessageBox.critical(
                self.iface.mainWindow(), "Invalid area of interest", str(exc)
            )
            return

        estimate = self._estimate_station_count(settings, geometry)
        if estimate > LARGE_STATION_COUNT:
            answer = QMessageBox.question(
                self.iface.mainWindow(),
                "Many stations",
                f"This AOI and grid size produce about {estimate:,} stations. "
                "Each station gets its own output files, and the extraction can take a "
                "long time and use much memory.\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                dialog.set_running(False)
                dialog.set_progress(0, "Ready to extract.")
                return

        from .aorc_processing import extract_aorc

        try:
            self._launch(
                "aorc",
                lambda progress, is_canceled: extract_aorc(
                    settings, geometry, progress=progress, is_canceled=is_canceled
                ),
                dialog.set_progress,
            )
        except Exception:
            dialog.set_running(False)
            raise
        self._running_message()

    def _start_usgs_extraction(self, settings):
        dialog = self.dialog
        if self._missing_modules(USGS_MODULES, "Observed data dependencies are missing"):
            dialog.set_running(False)
            return
        from .usgs_export import extract_usgs

        usgs_settings = dict(
            settings["usgs"], output_dir=settings["output_dir"], target_crs=settings["target_crs"],
            timezone=settings["timezone"],
        )
        inventory = dialog.usgs_tab.inventory
        try:
            self._launch(
                "usgs",
                lambda progress, is_canceled: extract_usgs(
                    usgs_settings, inventory, progress, is_canceled
                ),
                dialog.set_progress,
            )
        except Exception:
            dialog.set_running(False)
            raise
        self._running_message()

    def _start_et_extraction(self, settings):
        dialog = self.dialog
        if self._missing_modules(ET_MODULES, "Evapotranspiration dependencies are missing"):
            dialog.set_running(False)
            return
        from .et_processing import extract_et, import_earth_engine
        from .et_tab import earth_engine_library_paths

        try:
            geometry = self._get_aoi_geometry(settings, show=False)
        except Exception as exc:
            dialog.set_running(False)
            QMessageBox.critical(self.iface.mainWindow(), "Invalid area of interest", str(exc))
            return
        # Earth Engine is first touched here; the tab already checked the setup steps.
        try:
            ee = import_earth_engine(earth_engine_library_paths())
        except Exception as exc:
            dialog.set_running(False)
            QMessageBox.critical(self.iface.mainWindow(), "Google Earth Engine unavailable", str(exc))
            return
        et_settings = dict(settings["et"], output_dir=settings["output_dir"])
        try:
            self._launch(
                "et",
                lambda progress, is_canceled: extract_et(
                    et_settings, geometry, progress, is_canceled, ee_module=ee
                ),
                dialog.set_progress,
            )
        except Exception:
            dialog.set_running(False)
            raise
        self._running_message()

    def _running_message(self):
        self.iface.messageBar().pushInfo(
            "Weather Extractor",
            "Extraction is running. Progress and cancellation are available in the Weather Extractor window.",
        )

    @staticmethod
    def _estimate_station_count(settings, geometry):
        mode = settings["grid_mode"]
        if mode == "single":
            return 1
        if mode == "gauges":
            return len(settings["gauges"])
        if mode == "original":
            return int(geometry.area * CELLS_PER_SQUARE_DEGREE)
        cos_lat = max(math.cos(math.radians(geometry.centroid.y)), 0.01)
        area_km2 = geometry.area * KM_PER_DEGREE**2 * cos_lat
        return int(math.ceil(area_km2 / float(settings["block_km"]) ** 2))

    # -- Completion ------------------------------------------------------------

    def _task_finished(self, kind, successful, result, error):
        self.task = None
        self._task_active = False
        self.progress_bridge = None
        if self.action is not None:
            self.action.setEnabled(True)
        dialog = self.dialog
        if kind == "usgs_check":
            self._check_finished(successful, result, error)
            return
        if dialog is not None:
            dialog.set_running(False)
        title = RESULT_TITLES[kind]
        if successful:
            if kind == "aorc":
                self._load_aorc_layers(result)
            elif kind == "et":
                self._et_finished(result)
            else:
                self._load_usgs_layers(result)
        elif error is not None and not isinstance(error, InterruptedError):
            if dialog is not None:
                dialog.set_progress(0, f"Extraction failed: {error}")
            QMessageBox.critical(self.iface.mainWindow(), f"{title} extraction failed", f"{error}")
        else:
            if dialog is not None:
                dialog.set_progress(0, "Extraction canceled. Any completed outputs are retained.")
            QMessageBox.information(
                self.iface.mainWindow(),
                f"{title} extraction canceled",
                "The extraction was canceled. Any outputs already written may be incomplete.",
            )

    def _check_finished(self, successful, inventory, error):
        dialog = self.dialog
        if dialog is None:
            return
        dialog.set_checking(False)
        if successful:
            dialog.usgs_tab.set_inventory(inventory)
        elif error is not None and not isinstance(error, InterruptedError):
            dialog.usgs_tab.check_status.setText(f"Check failed: {error}")
            QMessageBox.critical(self.iface.mainWindow(), "Observed data check failed", f"{error}")
        else:
            dialog.usgs_tab.check_status.setText("Check canceled.")

    def _completion_message(self, title, text, result):
        warnings = ""
        if result.get("warnings_file"):
            count = len(result.get("warnings", []))
            warnings = (
                f"\n\n{count} warning(s). Check the warnings file in the output folder:\n"
                f"{result['warnings_file']}"
            )
        message = QMessageBox(
            QMessageBox.Information, title, text + warnings, QMessageBox.Ok, self.iface.mainWindow()
        )
        open_folder = message.addButton("Open output folder", QMessageBox.ActionRole)
        message.exec_()
        if message.clickedButton() == open_folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(result["output_dir"])))

    def _load_aorc_layers(self, result):
        dialog = self.dialog
        gpkg = str(result["gpkg"])
        label = result["label"]
        areas = QgsVectorLayer(f"{gpkg}|layername=station_areas", f"AORC station areas ({label})", "ogr")
        stations = QgsVectorLayer(f"{gpkg}|layername=stations", f"AORC weather stations ({label})", "ogr")
        if not stations.isValid():
            if dialog is not None:
                dialog.set_progress(100, "Extraction complete, but the station point layer could not be loaded.")
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Station layer unavailable",
                f"Weather data was extracted, but QGIS could not load {gpkg}.",
            )
            return
        project = QgsProject.instance()
        # Keep each run's layers together in a group named after its folder.
        group = project.layerTreeRoot().insertGroup(0, f"AORC {label}")
        project.addMapLayer(stations, False)
        group.addLayer(stations)
        if areas.isValid():
            areas.renderer().setSymbol(
                QgsFillSymbol.createSimple(
                    {"color": "0,0,0,0", "outline_color": "31,120,180", "outline_width": "0.4"}
                )
            )
            project.addMapLayer(areas, False)
            group.addLayer(areas)
        self.iface.setActiveLayer(stations)
        self.iface.zoomToActiveLayer()
        if dialog is not None:
            dialog.set_progress(100, f"Complete: {result['station_count']} station point(s) loaded into QGIS.")
        self._completion_message(
            "AORC extraction complete",
            f"Created weather outputs for {result['station_count']} station(s).\n\n"
            f"Output folder: {result['output_dir']}\n"
            f"Statistics: {result['summary']}\n"
            f"Station layers: {gpkg}",
            result,
        )

    def _et_finished(self, result):
        dialog = self.dialog
        if dialog is not None:
            dialog.set_progress(100, f"Complete: {result['months']} months of evapotranspiration.")
        self._completion_message(
            "Evapotranspiration extraction complete",
            f"Wrote the monthly area-mean evapotranspiration for {result['months']} months.\n\n"
            f"Output folder: {result['output_dir']}\n"
            f"Monthly table: {result['monthly'].name}\n"
            f"Annual totals: {result['annual'].name}",
            result,
        )

    def _load_usgs_layers(self, result):
        dialog = self.dialog
        gpkg = str(result["gpkg"])
        label = result["label"]
        project = QgsProject.instance()
        group = project.layerTreeRoot().insertGroup(0, f"Observed data {label}")
        from .usgs_catalog import LAYER_CHANNEL, QUALITY, QUANTITY
        from .usgs_export import LAYER_NAMES
        from .usgs_tab import LAYER_STYLES

        # Channel geometry first, so its dash is drawn on top of the station triangles.
        for key in (LAYER_CHANNEL, QUANTITY, QUALITY):
            name = LAYER_NAMES[key]
            if name not in result["layers"]:
                continue
            title, style = LAYER_STYLES[key]
            layer = QgsVectorLayer(f"{gpkg}|layername={name}", f"{title} ({label})", "ogr")
            if layer.isValid():
                layer.renderer().setSymbol(QgsMarkerSymbol.createSimple(style))
                project.addMapLayer(layer, False)
                group.addLayer(layer)
        if dialog is not None:
            dialog.set_progress(
                100,
                f"Complete: {result['station_count']} quantity station(s) and "
                f"{result['point_count']} water-quality point(s).",
            )
        self._completion_message(
            "Observed data extraction complete",
            f"Downloaded data for {result['station_count']} quantity station(s) and "
            f"{result['point_count']} water-quality point(s).\n\n"
            f"Output folder: {result['output_dir']}\n"
            f"Station layers: {gpkg}",
            result,
        )

    def _get_aoi_geometry(self, settings, show=True):
        """The AOI in EPSG:4326 as a shapely geometry; ``show`` adds/zooms it on the map."""
        import shapely

        destination = QgsCoordinateReferenceSystem("EPSG:4326")
        if settings["source"] == "bounds":
            bounds = settings["bounds"]
            crs = QgsCoordinateReferenceSystem(settings["bounds_crs"])
            if not crs.isValid():
                raise ValueError("The bounding-coordinate CRS is not valid.")
            rectangle = QgsGeometry.fromRect(
                QgsRectangle(
                    bounds["west"], bounds["south"], bounds["east"], bounds["north"]
                )
            )
            if show:
                layer = QgsVectorLayer("Polygon", "AORC coordinate AOI", "memory")
                layer.setCrs(crs)
                feature = QgsFeature(layer.fields())
                feature.setGeometry(QgsGeometry(rectangle))
                layer.dataProvider().addFeatures([feature])
                layer.updateExtents()
                QgsProject.instance().addMapLayer(layer)
                self.iface.setActiveLayer(layer)
                self.iface.zoomToActiveLayer()
            geographic = rectangle.densifyByCount(50)
            geographic.transform(
                QgsCoordinateTransform(crs, destination, QgsProject.instance())
            )
            return shapely.from_wkb(bytes(geographic.asWkb()))

        if settings["source"] == "selected_layer":
            layer = QgsProject.instance().mapLayer(settings["layer_id"])
            if layer is None or not layer.isValid():
                raise ValueError("The selected polygon layer is no longer available.")
            if layer.geometryType() != QgsWkbTypes.PolygonGeometry:
                raise ValueError("The selected layer must be a polygon layer.")
        else:
            layer = QgsVectorLayer(settings["shapefile"], "AORC AOI", "ogr")
            if not layer.isValid():
                raise ValueError("QGIS could not open the AOI file.")
            if layer.geometryType() != QgsWkbTypes.PolygonGeometry:
                raise ValueError("The AOI file must contain polygons.")

        if not layer.crs().isValid():
            raise ValueError("The AOI layer must have a defined CRS.")
        if settings["source"] == "shapefile" and show:
            QgsProject.instance().addMapLayer(layer)
        transform = QgsCoordinateTransform(
            layer.crs(), destination, QgsProject.instance()
        )
        geometries = []
        for feature in layer.getFeatures():
            qgs_geometry = feature.geometry()
            if qgs_geometry.isNull() or qgs_geometry.isEmpty():
                continue
            qgs_geometry.transform(transform)
            geometries.append(shapely.from_wkb(bytes(qgs_geometry.asWkb())))
        if not geometries:
            raise ValueError("The AOI layer contains no polygon geometry.")
        geometry = shapely.union_all(geometries)

        if geometry.is_empty:
            raise ValueError("The AOI geometry is empty.")
        if not geometry.is_valid:
            geometry = shapely.make_valid(geometry)
        if geometry.geom_type == "GeometryCollection":
            geometry = shapely.union_all(
                [part for part in geometry.geoms if part.geom_type in ("Polygon", "MultiPolygon")]
            )
        if geometry.is_empty or geometry.geom_type not in ("Polygon", "MultiPolygon"):
            raise ValueError("The AOI must contain polygon geometry.")
        if show:
            self.iface.setActiveLayer(layer)
            self.iface.zoomToActiveLayer()
        return geometry
