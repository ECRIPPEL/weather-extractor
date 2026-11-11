"""HidroWeb Data tab (ANA, Brazil): availability check, selection table, and map preview."""

from datetime import date, timedelta

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
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
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsSettings

from .hidroweb_catalog import COLUMN_LABELS, COLUMN_TOOLTIPS, COLUMNS, RAIN, ROWS, STREAMFLOW
from .usgs_tab import ROW_HEIGHT, PreviewLayers, UsgsTab


USER_KEY = "weather_extractor/hidroweb_user"
PREVIEW_GROUP = "HidroWeb data (preview)"
LAYER_STYLES = {
    STREAMFLOW: ("HidroWeb streamflow stations", {"name": "triangle", "color": "31,120,180", "size": "3.2"}),
    RAIN: ("HidroWeb rain gauges", {"name": "circle", "color": "106,61,154", "size": "2.4"}),
}
# Ask before downloads that need more requests than this.
LARGE_REQUEST_COUNT = 500


class HidrowebTab(QWidget):
    checkRequested = pyqtSignal()
    selectionChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.inventory = None
        self.preview = PreviewLayers(
            PREVIEW_GROUP, [(key, *LAYER_STYLES[key]) for key in (STREAMFLOW, RAIN)]
        )
        self.preview.on_selection = self._update_counter
        self._updating = False
        self._checking = False

        column = QVBoxLayout(self)
        form = QFormLayout()
        yesterday = date.today() - timedelta(days=1)
        self.start_date = UsgsTab._date_edit(date(yesterday.year - 2, 1, 1))
        self.end_date = UsgsTab._date_edit(yesterday)
        self.start_date.setToolTip("First day to retrieve")
        self.end_date.setToolTip("Last day to retrieve")
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
        # Optional HidroWebService login; without it the public ANA service is used.
        self.user = QLineEdit(QgsSettings().value(USER_KEY, "", type=str))
        self.user.setPlaceholderText("CPF/CNPJ")
        self.user.setToolTip("Optional ANA HidroWebService login (hidro@ana.gov.br)")
        self.user.editingFinished.connect(lambda: QgsSettings().setValue(USER_KEY, self.user.text().strip()))
        self.password = QLineEdit()
        self.password.setPlaceholderText("Password")
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setToolTip("Password sent by ANA (not saved)")
        for field in (self.user, self.password):
            field.setMaximumWidth(120)
        margin_row = QWidget()
        margin_layout = QHBoxLayout(margin_row)
        margin_layout.setContentsMargins(0, 0, 0, 0)
        margin_layout.addWidget(self.margin_km)
        margin_layout.addStretch(1)
        margin_layout.addWidget(QLabel("ANA login"))
        margin_layout.addWidget(self.user)
        margin_layout.addWidget(self.password)

        form.addRow("Period", period)
        form.addRow("Search margin", margin_row)
        column.addLayout(form)

        self.check_button = QPushButton("Check available data")
        self.check_button.setToolTip("Find the ANA stations with data in the area and period, without downloading")
        self.check_button.clicked.connect(self.checkRequested.emit)
        self.check_status = QLabel("Press 'Check available data' to see what exists in the area.")
        self.check_status.setWordWrap(True)
        column.addWidget(self.check_button)
        column.addWidget(self.check_status)

        self.table = self._make_table()
        column.addWidget(self.table)
        column.addStretch(1)

        self.only_selected = QCheckBox("Only stations selected on the map")
        self.only_selected.setToolTip("Download only the preview points selected with the QGIS selection tools")
        self.only_selected.toggled.connect(lambda _: self._update_counter())
        self.write_shapefile = QCheckBox("Also save station layers as shapefiles")
        self.write_shapefile.setToolTip("The GeoPackage is always written")
        self.counter = QLabel("")
        options = QHBoxLayout()
        options.addWidget(self.only_selected)
        options.addWidget(self.write_shapefile)
        column.addLayout(options)
        self.run_button = QPushButton("Run")
        self.run_button.setToolTip("Download the checked daily series")
        footer = QHBoxLayout()
        footer.addWidget(self.counter, 1)
        footer.addWidget(self.run_button)
        column.addLayout(footer)

        for widget in (self.start_date, self.end_date):
            widget.dateChanged.connect(lambda _: self.invalidate())
        self.margin_km.valueChanged.connect(lambda _: self.invalidate())
        self._set_results_enabled(False)

    # -- widgets ----------------------------------------------------------

    def _make_table(self):
        table = QTableWidget(len(ROWS), len(COLUMNS) + 1)
        table.setHorizontalHeaderLabels(["Variable"] + [COLUMN_LABELS[c] for c in COLUMNS])
        for c, name in enumerate(COLUMNS, start=1):
            table.horizontalHeaderItem(c).setToolTip(COLUMN_TOOLTIPS[name])
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
        height = table.horizontalHeader().sizeHint().height() + len(ROWS) * ROW_HEIGHT
        table.setFixedHeight(height + 2 * table.frameWidth() + 2)
        for r, row in enumerate(ROWS):
            name = QTableWidgetItem(row.label)
            name.setFlags(Qt.ItemIsEnabled)
            table.setItem(r, 0, name)
            for c, column in enumerate(COLUMNS, start=1):
                item = QTableWidgetItem("")
                item.setTextAlignment(Qt.AlignCenter)
                item.setData(Qt.UserRole, (row.key, column))
                item.setFlags(Qt.NoItemFlags)
                table.setItem(r, c, item)
        table.itemChanged.connect(self._item_changed)
        return table

    def _cells(self):
        for r in range(self.table.rowCount()):
            for c in range(1, self.table.columnCount()):
                yield self.table.item(r, c)

    def _set_results_enabled(self, enabled):
        for widget in (self.table, self.only_selected, self.write_shapefile):
            widget.setEnabled(enabled)

    # -- state ------------------------------------------------------------

    def set_checking(self, checking):
        self._checking = checking
        self.check_button.setText("Cancel check" if checking else "Check available data")
        for widget in (self.start_date, self.end_date, self.margin_km, self.user, self.password):
            widget.setEnabled(not checking)
        if checking:
            self.check_status.setText("Checking available data...")

    def set_check_progress(self, percentage, message):
        self.check_status.setText(f"{message} ({round(percentage)}%)")

    def invalidate(self, *_args):
        """Forget the last check; the table turns grey until a new check."""
        if self.inventory is None and not self.preview.layers:
            return
        self.inventory = None
        self.preview.remove()
        self._updating = True
        for item in self._cells():
            item.setFlags(Qt.NoItemFlags)
            item.setData(Qt.CheckStateRole, None)
            item.setData(Qt.ForegroundRole, None)
            item.setText("")
        self._updating = False
        self._set_results_enabled(False)
        self.counter.setText("")
        self.check_status.setToolTip("")
        self.check_status.setStyleSheet("")
        self.check_status.setText("The area, period, or margin changed. Check available data again.")
        self.selectionChanged.emit()

    def set_inventory(self, inventory):
        self.inventory = inventory
        self._updating = True
        for item in self._cells():
            row_key, column = item.data(Qt.UserRole)
            count = inventory.count(row_key, column)
            item.setText(f"{count:,}")
            if count:
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked)
            else:
                item.setFlags(Qt.NoItemFlags)
                item.setForeground(QColor(150, 150, 150))
        self._updating = False
        self._set_results_enabled(True)
        message = (
            f"Found {len(inventory.layer_sites(STREAMFLOW)):,} streamflow station(s) and "
            f"{len(inventory.layer_sites(RAIN)):,} rain gauge(s) (checked {inventory.checked_at})."
        )
        if inventory.warnings:
            message += f" {len(inventory.warnings)} note(s): hover for details."
        if inventory.failed_queries:
            message = (
                f"Incomplete: ANA refused {inventory.failed_queries} inventory queries, so some stations may "
                "be missing. Check available data again in a few minutes. " + message
            )
        self.check_status.setStyleSheet("color: rgb(190, 0, 0)" if inventory.failed_queries else "")
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

    def _item_changed(self, _item):
        if not self._updating:
            self._refresh()

    def _refresh(self):
        if self.inventory is None:
            return
        self.preview.update(self.inventory.visible_sites(set(self.selection())))
        self._update_counter()

    def _update_counter(self):
        """Stations the next Run downloads: only the map selection when that box is checked."""
        if self.inventory is None:
            return
        visible = self.inventory.visible_sites(set(self.selection()))
        if self.only_selected.isChecked():
            chosen = self.preview.selected_site_ids()
            visible = {layer: sites & chosen for layer, sites in visible.items()}
        self.counter.setText(
            f"Selected: {len(visible[STREAMFLOW]):,} streamflow station(s), {len(visible[RAIN]):,} rain gauge(s)"
        )
        self.selectionChanged.emit()

    def keep_downloaded(self, keys):
        """After a run, the table and the map keep only the datasets that returned data."""
        if self.inventory is None:
            return
        self.inventory.keep(set(keys))
        # The map then shows exactly the downloaded stations, so the map selection is not needed.
        self.only_selected.setChecked(False)
        self.set_inventory(self.inventory)
        self.check_status.setText(
            "Showing the stations with data from the last run. Press 'Check available data' to see all again."
        )

    def can_run(self):
        return self.inventory is not None and not self._checking and bool(self.selection())

    def get_settings(self):
        return {
            "start_date": self.start_date.date().toString("yyyy-MM-dd"),
            "end_date": self.end_date.date().toString("yyyy-MM-dd"),
            "margin_km": self.margin_km.value(),
            "user": self.user.text().strip(),
            "password": self.password.text(),
            "selection": self.selection(),
            "site_ids": sorted(self.preview.selected_site_ids()) if self.only_selected.isChecked() else None,
            "write_shapefile": self.write_shapefile.isChecked(),
        }

    def confirm_run(self):
        """Before a very large download, say how many requests it needs."""
        from .hidroweb_export import estimate_requests

        requests = estimate_requests(self.inventory, self.get_settings())
        if requests <= LARGE_REQUEST_COUNT:
            return True
        answer = QMessageBox.question(
            self,
            "HidroWeb download",
            f"This request needs about {requests:,} downloads from ANA and may take a while.\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        return answer == QMessageBox.Yes

    def set_running(self, running):
        for widget in (self.start_date, self.end_date, self.margin_km, self.user, self.password, self.check_button):
            widget.setEnabled(not running)
        self._set_results_enabled(not running and self.inventory is not None)
