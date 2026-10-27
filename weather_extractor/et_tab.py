"""Evapotranspiration tab: monthly basin-mean actual ET from OpenET and MODIS."""

from datetime import date
from importlib.util import find_spec
import os

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsApplication, QgsSettings

from .et_processing import FIRST_YEAR, MODIS, OPENET, has_credentials, stored_project


PROJECT_KEY = "weather_extractor/ee_project"
REGISTER_URL = "https://code.earthengine.google.com/register"


def earth_engine_library_paths():
    """Library folders of the Google Earth Engine plugin, enabled or not."""
    folders = [
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        os.path.join(QgsApplication.qgisSettingsDirPath(), "python", "plugins"),
    ]
    return [os.path.join(folder, "ee_plugin", "extlibs") for folder in folders]


def gee_plugin_installed():
    return find_spec("ee") is not None or any(os.path.isdir(path) for path in earth_engine_library_paths())


class EtTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        column = QVBoxLayout(self)
        self.group = QGroupBox("Monthly evapotranspiration (area mean)")
        form = QFormLayout(self.group)

        last_year = max(FIRST_YEAR, date.today().year - 1)
        self.start_year = QSpinBox()
        self.end_year = QSpinBox()
        for spin in (self.start_year, self.end_year):
            spin.setRange(FIRST_YEAR, last_year)
        self.end_year.setValue(max(FIRST_YEAR, last_year - 1))
        self.start_year.setValue(max(FIRST_YEAR, self.end_year.value() - 4))
        self.start_year.setToolTip(f"From {FIRST_YEAR}")
        self.end_year.setToolTip("The latest months may not be published yet")
        period = QWidget()
        period_row = QHBoxLayout(period)
        period_row.setContentsMargins(0, 0, 0, 0)
        period_row.addWidget(self.start_year)
        period_row.addWidget(QLabel("to"))
        period_row.addWidget(self.end_year)
        period_row.addStretch(1)

        self.product_checks = {
            OPENET: QCheckBox("OpenET"),
            MODIS: QCheckBox("MODIS"),
        }
        self.product_checks[OPENET].setToolTip("OpenET ensemble actual ET (6 models, 30 m)")
        self.product_checks[MODIS].setToolTip("MOD16A2GF actual and potential ET (500 m)")
        products = QWidget()
        products_row = QHBoxLayout(products)
        products_row.setContentsMargins(0, 0, 0, 0)
        for check in self.product_checks.values():
            check.setChecked(True)
            products_row.addWidget(check)
        products_row.addStretch(1)

        saved = QgsSettings().value(PROJECT_KEY, "", type=str)
        self.project = QLineEdit(saved or stored_project())
        self.project.setPlaceholderText("Google Cloud project ID")
        self.project.setToolTip("Project set in Plugins > Google Earth Engine > Set Project")

        form.addRow("Period", period)
        form.addRow("Products", products)
        form.addRow("Google Earth Engine project", self.project)

        note = QLabel(
            "One monthly series: the mean over the area of tab 1. Requires the Google Earth "
            "Engine plugin; Run shows the setup steps if something is missing."
        )
        note.setWordWrap(True)
        form.addRow(note)
        column.addWidget(self.group)
        column.addStretch(1)

        self.run_button = QPushButton("Run")
        self.run_button.setToolTip("Download monthly evapotranspiration for the area of tab 1")
        footer = QHBoxLayout()
        footer.addStretch(1)
        footer.addWidget(self.run_button)
        column.addLayout(footer)

    def get_settings(self):
        return {
            "start_year": self.start_year.value(),
            "end_year": self.end_year.value(),
            "products": [key for key, check in self.product_checks.items() if check.isChecked()],
            "project": self.project.text().strip(),
        }

    def save_project(self):
        QgsSettings().setValue(PROJECT_KEY, self.project.text().strip())

    def setup_status(self):
        """(installed, signed in, project entered), checked locally without network."""
        return gee_plugin_installed(), has_credentials(), bool(self.project.text().strip())

    def confirm_setup(self):
        """Explain the Google Earth Engine steps still missing; True when all are done."""
        status = self.setup_status()
        if all(status):
            self.save_project()
            return True
        installed, signed_in, has_project = status
        mark = {True: "✓", False: "✗"}
        message = QMessageBox(
            QMessageBox.Information,
            "Google Earth Engine setup",
            "Evapotranspiration data come from Google Earth Engine. "
            "Complete the steps marked ✗, then press Run again.\n\n"
            f"{mark[installed]}  1. Install and enable the 'Google Earth Engine' plugin\n"
            "      (Plugins > Manage and Install Plugins).\n\n"
            f"{mark[signed_in]}  2. Sign in with your Google account in the plugin\n"
            "      (Plugins > Google Earth Engine > Sign-in).\n\n"
            f"{mark[has_project]}  3. Select your Google Cloud project in the plugin\n"
            "      (Plugins > Google Earth Engine > Set Project).\n"
            "      Then paste its ID in this tab's\n"
            "      'Google Earth Engine project' box.\n\n"
            f"No Earth Engine access yet? Register a project at {REGISTER_URL}",
            QMessageBox.Ok,
            self,
        )
        manager = message.addButton("Open Plugin Manager", QMessageBox.ActionRole) if not installed else None
        known = stored_project()
        use_known = (
            message.addButton(f"Use project '{known}'", QMessageBox.AcceptRole)
            if known and not has_project
            else None
        )
        message.exec_()
        clicked = message.clickedButton()
        if manager is not None and clicked == manager:
            from qgis.utils import iface

            iface.actionManagePlugins().trigger()
            return False
        if use_known is not None and clicked == use_known:
            self.project.setText(known)
            if all(self.setup_status()):
                self.save_project()
                return True
        return False

    def set_running(self, running):
        self.group.setEnabled(not running)
