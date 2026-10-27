"""Observed Data tab (USGS, Water Quality Portal, NOAA GHCN): availability check, selection tables, and map preview."""

from datetime import date, timedelta

from qgis.PyQt.QtCore import QDate, Qt, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QColor, QDesktopServices
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDateEdit,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    QgsFeature,
    QgsGeometry,
    QgsMarkerSymbol,
    QgsPointXY,
    QgsProject,
    QgsSettings,
    QgsVectorLayer,
)

from .usgs_catalog import (
    COLUMN_LABELS,
    COLUMN_TOOLTIPS,
    LAYER_CHANNEL,
    QUALITY,
    QUANTITY,
    columns_of,
    rows_of,
)
from .usgs_client import SIGNUP_URL
from .usgs_inventory import OTHER, SITE_SOURCES, USGS


SETTINGS_KEY = "weather_extractor/usgs_api_key"
SKIP_KEY_NOTICE = "weather_extractor/skip_api_key_notice"
ROW_HEIGHT = 19
ROW_TOGGLE = "row"
PREVIEW_GROUP = "Observed data (preview)"
# Map symbols shared by the preview and the final layers. Channel geometry is a
# horizontal dash drawn over the station triangle, at the same size.
LAYER_STYLES = {
    QUANTITY: ("Quantity stations", {"name": "triangle", "color": "31,120,180", "size": "3.2"}),
    QUALITY: ("Water quality points", {"name": "circle", "color": "51,160,44", "size": "2.6"}),
    LAYER_CHANNEL: (
        "USGS channel geometry",
        {"name": "line", "angle": "90", "size": "3.2", "color": "140,81,10",
         "outline_color": "140,81,10", "outline_width": "0.7"},
    ),
}
PREVIEW_STYLE = {
    layer: (f"{title} (preview)", style) for layer, (title, style) in LAYER_STYLES.items()
}


class PreviewLayers:
    """Temporary memory layers that show the available sites on the map."""

    def __init__(self):
        self.layers = {}

    def build(self, inventory):
        self.remove()
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        group = root.findGroup(PREVIEW_GROUP)
        if group is not None:
            # Previews left by an earlier window are replaced.
            for node in group.findLayers():
                project.removeMapLayer(node.layerId())
        else:
            group = root.insertGroup(0, PREVIEW_GROUP)
        # Channel first, so its dash is drawn on top of the station triangles.
        for layer_group in (LAYER_CHANNEL, QUANTITY, QUALITY):
            name, style = PREVIEW_STYLE[layer_group]
            layer = QgsVectorLayer(
                "Point?crs=EPSG:4326&field=site_id:string&field=name:string&field=site_type:string"
                "&field=org:string&field=source:string&field=datasets:string(254)&field=visible:integer",
                name,
                "memory",
            )
            features = []
            for site_id in inventory.layer_sites(layer_group):
                site = inventory.sites.get(site_id, {})
                feature = QgsFeature(layer.fields())
                feature.setAttributes(
                    [
                        site_id,
                        site.get("name", ""),
                        site.get("type", ""),
                        site.get("org", ""),
                        SITE_SOURCES.get(site.get("source"), "Other agency"),
                        inventory.describe(site_id, layer_group)[:254],
                        1,
                    ]
                )
                feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(site["lon"], site["lat"])))
                features.append(feature)
            layer.dataProvider().addFeatures(features)
            layer.updateExtents()
            layer.renderer().setSymbol(QgsMarkerSymbol.createSimple(style))
            project.addMapLayer(layer, False)
            group.addLayer(layer)
            self.layers[layer_group] = layer

    def update(self, visible):
        """Show only the sites in ``visible`` (per layer group)."""
        for layer_group, layer in list(self.layers.items()):
            if not _alive(layer):
                del self.layers[layer_group]
                continue
            index = layer.fields().indexOf("visible")
            layer.setSubsetString("")
            changes = {
                feature.id(): {index: int(feature["site_id"] in visible[layer_group])}
                for feature in layer.getFeatures()
            }
            layer.dataProvider().changeAttributeValues(changes)
            layer.setSubsetString('"visible" = 1')
            layer.triggerRepaint()

    def selected_site_ids(self):
        site_ids, any_layer = set(), False
        for layer in self.layers.values():
            if _alive(layer):
                any_layer = True
                site_ids.update(feature["site_id"] for feature in layer.selectedFeatures())
        return site_ids if any_layer else set()

    def remove(self):
        project = QgsProject.instance()
        for layer in self.layers.values():
            if _alive(layer) and project.mapLayer(layer.id()):
                project.removeMapLayer(layer.id())
        self.layers = {}
        root = project.layerTreeRoot()
        group = root.findGroup(PREVIEW_GROUP)
        if group is not None and not group.children():
            root.removeChildNode(group)


def _alive(layer):
    try:
        layer.id()
        return True
    except RuntimeError:
        return False


class UsgsTab(QWidget):
    checkRequested = pyqtSignal()
    selectionChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.inventory = None
        self.preview = PreviewLayers()
        self._updating = False
        self._checking = False

        column = QVBoxLayout(self)
        form = QFormLayout()
        yesterday = date.today() - timedelta(days=1)
        self.start_date = self._date_edit(date(yesterday.year - 2, 1, 1))
        self.end_date = self._date_edit(yesterday)
        self.start_date.setToolTip("First day to retrieve")
        self.end_date.setToolTip("Last day to retrieve (USGS data run up to yesterday)")
        period = QWidget()
        period_row = QHBoxLayout(period)
        period_row.setContentsMargins(0, 0, 0, 0)
        period_row.addWidget(self.start_date)
        period_row.addWidget(QLabel("to"))
        period_row.addWidget(self.end_date)

        self.margin_km = QDoubleSpinBox()
        self.margin_km.setRange(0.0, 50.0)
        self.margin_km.setDecimals(1)
        self.margin_km.setSuffix(" km")
        self.margin_km.setToolTip("Include stations up to this distance outside the area (e.g., the basin outlet)")
        self.api_key = QLineEdit(QgsSettings().value(SETTINGS_KEY, "", type=str))
        self.api_key.setPlaceholderText("Optional")
        self.api_key.setToolTip("Free USGS API key for higher request limits (api.waterdata.usgs.gov/signup)")
        self.api_key.editingFinished.connect(
            lambda: QgsSettings().setValue(SETTINGS_KEY, self.api_key.text().strip())
        )
        margin_row = QWidget()
        margin_layout = QHBoxLayout(margin_row)
        margin_layout.setContentsMargins(0, 0, 0, 0)
        margin_layout.addWidget(self.margin_km)
        margin_layout.addWidget(QLabel("API key"))
        margin_layout.addWidget(self.api_key)

        form.addRow("Period", period)
        form.addRow("Search margin", margin_row)
        column.addLayout(form)

        self.check_button = QPushButton("Check available data")
        self.check_button.setToolTip("Find the data available in the area and period, without downloading it")
        self.check_button.clicked.connect(self._check_clicked)
        self.check_status = QLabel("Press 'Check available data' to see what exists in the area.")
        self.check_status.setWordWrap(True)
        column.addWidget(self.check_button)
        column.addWidget(self.check_status)

        self.tables = {}
        self.inner_tabs = QTabWidget()
        for group, title in ((QUANTITY, "Water quantity"), (QUALITY, "Water quality")):
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(4, 4, 4, 4)
            table = self._make_table(group)
            self.tables[group] = table
            page_layout.addWidget(table)
            if group == QUALITY:
                self.usgs_samples = QCheckBox("USGS Samples")
                self.other_samples = QCheckBox("Water Quality Portal (other agencies)")
                self.usgs_samples.setToolTip("USGS water-quality samples")
                self.other_samples.setToolTip("Samples from EPA, states, and other organizations")
                sources = QHBoxLayout()
                sources.addWidget(QLabel("Sample sources:"))
                sources.addWidget(self.usgs_samples)
                sources.addWidget(self.other_samples)
                sources.addStretch(1)
                page_layout.addLayout(sources)
                for check in (self.usgs_samples, self.other_samples):
                    check.setChecked(True)
                    check.toggled.connect(self._sources_changed)
            page_layout.addStretch(1)
            self.inner_tabs.addTab(page, title)
        # Reserve the height of the tallest page so no table is clipped.
        tallest = max(
            self.inner_tabs.widget(i).minimumSizeHint().height() for i in range(self.inner_tabs.count())
        )
        frame = self.inner_tabs.sizeHint().height() - max(
            self.inner_tabs.widget(i).sizeHint().height() for i in range(self.inner_tabs.count())
        )
        self.inner_tabs.setMinimumHeight(tallest + max(frame, self.inner_tabs.tabBar().sizeHint().height()) + 12)
        column.addWidget(self.inner_tabs, 1)

        self.only_selected = QCheckBox("Only stations selected on the map")
        self.only_selected.setToolTip("Download only the preview points selected with the QGIS selection tools")
        self.only_selected.toggled.connect(lambda _: self.selectionChanged.emit())
        self.write_shapefile = QCheckBox("Also save station layers as shapefiles")
        self.write_shapefile.setToolTip("The GeoPackage is always written")
        self.counter = QLabel("")
        options = QHBoxLayout()
        options.addWidget(self.only_selected)
        options.addWidget(self.write_shapefile)
        column.addLayout(options)
        self.run_button = QPushButton("Run")
        self.run_button.setToolTip("Download the checked datasets")
        footer = QHBoxLayout()
        footer.addWidget(self.counter, 1)
        footer.addWidget(self.run_button)
        column.addLayout(footer)

        for widget in (self.start_date, self.end_date):
            widget.dateChanged.connect(lambda _: self.invalidate())
        self.margin_km.valueChanged.connect(lambda _: self.invalidate())
        self._set_results_enabled(False)

    # -- widgets ----------------------------------------------------------

    @staticmethod
    def _date_edit(value):
        edit = QDateEdit(QDate(value.year, value.month, value.day))
        edit.setCalendarPopup(True)
        edit.setDisplayFormat("yyyy-MM-dd")
        edit.setMinimumDate(QDate(1800, 1, 1))
        edit.setMaximumDate(QDate.currentDate())
        return edit

    def _make_table(self, group):
        rows, columns = rows_of(group), columns_of(group)
        # Column 0 holds the variable name with a box that switches its whole row.
        table = QTableWidget(len(rows), len(columns) + 1)
        table.setHorizontalHeaderLabels(["Variable"] + [COLUMN_LABELS[group][c] for c in columns])
        table.horizontalHeaderItem(0).setToolTip("Check or uncheck every format of the variable")
        for c, column in enumerate(columns, start=1):
            table.horizontalHeaderItem(c).setToolTip(COLUMN_TOOLTIPS[column])
        table.verticalHeader().hide()
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.NoSelection)
        table.verticalHeader().setMinimumSectionSize(ROW_HEIGHT)
        table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        table.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        table.setWordWrap(False)
        # Show every row: the boxes must all be visible to choose from.
        height = table.horizontalHeader().sizeHint().height() + len(rows) * ROW_HEIGHT
        table.setFixedHeight(height + 2 * table.frameWidth() + 2)
        for r, row in enumerate(rows):
            name = QTableWidgetItem(row.label)
            name.setData(Qt.UserRole, (ROW_TOGGLE, row.key))
            name.setFlags(Qt.NoItemFlags)
            table.setItem(r, 0, name)
            for c, column in enumerate(columns, start=1):
                item = QTableWidgetItem("" if column in row.columns else "n/a")
                item.setTextAlignment(Qt.AlignCenter)
                item.setData(Qt.UserRole, (row.key, column))
                item.setFlags(Qt.NoItemFlags)
                table.setItem(r, c, item)
        table.itemChanged.connect(self._item_changed)
        return table

    def _cells(self):
        """Dataset cells (every column except the row toggle)."""
        for table in self.tables.values():
            for r in range(table.rowCount()):
                for c in range(1, table.columnCount()):
                    yield table.item(r, c)

    def _row_toggles(self):
        for table in self.tables.values():
            for r in range(table.rowCount()):
                yield table, r, table.item(r, 0)

    def _update_row_toggles(self):
        """Row box: checked when all its boxes are, partial when some are."""
        self._updating = True
        for table, r, toggle in self._row_toggles():
            cells = [table.item(r, c) for c in range(1, table.columnCount())]
            usable = [cell for cell in cells if cell.flags() & Qt.ItemIsUserCheckable]
            if not usable:
                toggle.setFlags(Qt.NoItemFlags)
                toggle.setData(Qt.CheckStateRole, None)
                continue
            toggle.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            checked = sum(cell.checkState() == Qt.Checked for cell in usable)
            toggle.setCheckState(
                Qt.Checked if checked == len(usable) else Qt.Unchecked if not checked else Qt.PartiallyChecked
            )
        self._updating = False

    def _set_results_enabled(self, enabled):
        for widget in (
            self.tables[QUANTITY], self.tables[QUALITY], self.usgs_samples,
            self.other_samples, self.only_selected, self.write_shapefile,
        ):
            widget.setEnabled(enabled)

    # -- state ------------------------------------------------------------

    def _check_clicked(self):
        self.checkRequested.emit()

    def set_checking(self, checking):
        self._checking = checking
        self.check_button.setText("Cancel check" if checking else "Check available data")
        for widget in (self.start_date, self.end_date, self.margin_km, self.api_key):
            widget.setEnabled(not checking)
        if checking:
            self.check_status.setText("Checking available data...")

    def set_check_progress(self, percentage, message):
        self.check_status.setText(f"{message} ({round(percentage)}%)")

    def invalidate(self, *_args):
        """Forget the last check; the tables turn grey until a new check."""
        if self.inventory is None and not self.preview.layers:
            return
        self.inventory = None
        self.preview.remove()
        self._updating = True
        for item in self._cells():
            available = item.text() != "n/a"
            item.setFlags(Qt.NoItemFlags)
            item.setData(Qt.CheckStateRole, None)
            item.setData(Qt.ForegroundRole, None)
            item.setText("" if available else "n/a")
        for _table, _row, toggle in self._row_toggles():
            toggle.setFlags(Qt.NoItemFlags)
            toggle.setData(Qt.CheckStateRole, None)
        self._updating = False
        self._set_results_enabled(False)
        self.counter.setText("")
        self.check_status.setToolTip("")
        self.check_status.setText("The area, period, or margin changed. Check available data again.")
        self.selectionChanged.emit()

    def set_inventory(self, inventory):
        self.inventory = inventory
        self._updating = True
        for item in self._cells():
            row_key, column = item.data(Qt.UserRole)
            if item.text() == "n/a":
                continue
            count = inventory.count(row_key, column)
            item.setText(f"{count:,}")
            if count:
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked)
            else:
                item.setFlags(Qt.NoItemFlags)
                item.setForeground(QColor(150, 150, 150))
        self._updating = False
        self._update_row_toggles()
        self._set_results_enabled(True)
        quantity = len(set(inventory.layer_sites(QUANTITY)) | set(inventory.layer_sites(LAYER_CHANNEL)))
        quality = len(inventory.layer_sites(QUALITY))
        message = (
            f"Found {quantity:,} quantity station(s) and {quality:,} water-quality point(s) "
            f"(checked {inventory.checked_at})."
        )
        if inventory.warnings:
            message += f" {len(inventory.warnings)} note(s): hover for details."
        self.check_status.setText(message)
        self.check_status.setToolTip("\n".join(inventory.warnings))
        self.preview.build(inventory)
        self._refresh()

    def selection(self):
        return [
            item.data(Qt.UserRole)
            for item in self._cells()
            if item.flags() & Qt.ItemIsUserCheckable and item.checkState() == Qt.Checked
        ]

    def sample_sources(self):
        return tuple(
            source for source, check in ((USGS, self.usgs_samples), (OTHER, self.other_samples))
            if check.isChecked()
        )

    def _item_changed(self, item):
        if self._updating:
            return
        if item.column() == 0:
            # Row box: apply its state to every available box of the row.
            state = Qt.Unchecked if item.checkState() == Qt.Unchecked else Qt.Checked
            table = item.tableWidget()
            self._updating = True
            for c in range(1, table.columnCount()):
                cell = table.item(item.row(), c)
                if cell.flags() & Qt.ItemIsUserCheckable:
                    cell.setCheckState(state)
            self._updating = False
        self._update_row_toggles()
        self._refresh()

    def _sources_changed(self, _checked):
        if self.inventory is None:
            return
        self._updating = True
        for item in self._cells():
            row_key, column = item.data(Qt.UserRole)
            if column == "samples" and item.text() != "n/a":
                item.setText(f"{self.inventory.count(row_key, column, self.sample_sources()):,}")
        self._updating = False
        self._refresh()

    def _refresh(self):
        if self.inventory is None:
            return
        visible = self.inventory.visible_sites(set(self.selection()), self.sample_sources())
        self.preview.update(visible)
        quantity = len(visible[QUANTITY] | visible[LAYER_CHANNEL])
        self.counter.setText(
            f"Selected: {quantity:,} quantity station(s), "
            f"{len(visible[QUALITY]):,} water-quality point(s)"
        )
        self.selectionChanged.emit()

    def rain_gauges(self):
        """Precipitation stations the current selection would download (for the AORC tab)."""
        if self.inventory is None:
            return []
        site_ids = self.get_settings()["site_ids"]
        return self.inventory.rain_gauges(
            set(self.selection()), self.sample_sources(), set(site_ids) if site_ids is not None else None
        )

    def can_run(self):
        return self.inventory is not None and not self._checking and bool(self.selection())

    def get_settings(self):
        return {
            "start_date": self.start_date.date().toString("yyyy-MM-dd"),
            "end_date": self.end_date.date().toString("yyyy-MM-dd"),
            "margin_km": self.margin_km.value(),
            "api_key": self.api_key.text().strip(),
            "selection": self.selection(),
            "sample_sources": self.sample_sources(),
            "site_ids": sorted(self.preview.selected_site_ids()) if self.only_selected.isChecked() else None,
            "write_shapefile": self.write_shapefile.isChecked(),
        }

    def confirm_run(self):
        """Before downloading without an API key, explain that it may take a while."""
        settings = self.get_settings()
        if settings["api_key"] or QgsSettings().value(SKIP_KEY_NOTICE, False, type=bool):
            return True
        from .usgs_export import estimate_requests

        requests = estimate_requests(self.inventory, settings)
        box = QMessageBox(
            QMessageBox.Information,
            "Observed data download",
            f"This request needs about {requests:,} download(s) from USGS, EPA, and NOAA services\n"
            "and may take a while to finish.\n\n"
            "For a faster run, use a free USGS Water Data API key:\n"
            f"{SIGNUP_URL}\n\n"
            "Paste it in the 'API key' field of this tab.",
            parent=self,
        )
        run = box.addButton("Run anyway", QMessageBox.AcceptRole)
        get_key = box.addButton("Get a free key", QMessageBox.HelpRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(run)
        skip = QCheckBox("Don't show this again")
        box.setCheckBox(skip)
        box.exec_()
        if skip.isChecked():
            QgsSettings().setValue(SKIP_KEY_NOTICE, True)
        if box.clickedButton() == get_key:
            QDesktopServices.openUrl(QUrl(SIGNUP_URL))
            self.api_key.setFocus()
            return False
        return box.clickedButton() == run

    def set_running(self, running):
        for widget in (self.start_date, self.end_date, self.margin_km, self.api_key, self.check_button):
            widget.setEnabled(not running)
        self._set_results_enabled(not running and self.inventory is not None)
