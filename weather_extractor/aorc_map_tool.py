from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand


class AORCPolygonTool(QgsMapTool):
    """Digitize a polygon vertex by vertex, like the QGIS Add Polygon tool.

    Left-click adds a vertex, right-click or Enter finishes, Backspace removes
    the last vertex, and Esc cancels.
    """

    def __init__(self, iface, finished_callback):
        self.iface = iface
        self.finished_callback = finished_callback
        self.map_canvas = iface.mapCanvas()
        self.previous_tool = self.map_canvas.mapTool()
        super().__init__(self.map_canvas)
        self.setCursor(Qt.CrossCursor)
        self.points = []
        self._done = False
        self.polygon_band = QgsRubberBand(self.map_canvas, QgsWkbTypes.PolygonGeometry)
        self.polygon_band.setColor(QColor(190, 73, 48, 70))
        self.polygon_band.setStrokeColor(QColor(190, 73, 48))
        self.polygon_band.setWidth(2)
        self.vertex_band = QgsRubberBand(self.map_canvas, QgsWkbTypes.PointGeometry)
        self.vertex_band.setColor(QColor(190, 73, 48))
        self.vertex_band.setIcon(QgsRubberBand.ICON_CIRCLE)
        self.vertex_band.setIconSize(7)

    def canvasMoveEvent(self, event):
        if self.points:
            self._redraw(self._map_point(event.pos()))

    def canvasReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.points.append(self._map_point(event.pos()))
            self.vertex_band.addPoint(self.points[-1])
            self._redraw()
        elif event.button() == Qt.RightButton:
            self._complete()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._finish(None)
        elif event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._complete()
        elif event.key() in (Qt.Key_Backspace, Qt.Key_Delete) and self.points:
            self.points.pop()
            self.vertex_band.reset(QgsWkbTypes.PointGeometry)
            for point in self.points:
                self.vertex_band.addPoint(point)
            self._redraw()

    def _redraw(self, cursor=None):
        points = self.points + ([cursor] if cursor is not None else [])
        self.polygon_band.setToGeometry(
            QgsGeometry.fromPolygonXY([points]) if len(points) > 2
            else QgsGeometry.fromPolylineXY(points),
            None,
        )

    def _complete(self):
        if len(self.points) < 3:
            self.iface.messageBar().pushWarning(
                "Weather Extractor",
                "A polygon needs at least three vertices. Drawing canceled.",
            )
            self._finish(None)
            return

        canvas_crs = self.map_canvas.mapSettings().destinationCrs()
        geometry = QgsGeometry.fromPolygonXY([self.points + [self.points[0]]])
        geometry.transform(
            QgsCoordinateTransform(
                canvas_crs, QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance()
            )
        )
        layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "AORC drawn AOI", "memory")
        feature = QgsFeature(layer.fields())
        feature.setGeometry(geometry)
        layer.dataProvider().addFeatures([feature])
        layer.updateExtents()
        QgsProject.instance().addMapLayer(layer)
        self.iface.setActiveLayer(layer)
        self._finish(layer)

    def _map_point(self, pixel):
        return QgsPointXY(
            self.map_canvas.getCoordinateTransform().toMapCoordinates(pixel)
        )

    def deactivate(self):
        # Another map tool was chosen while drawing: cancel and reopen the dialog.
        if not self._done:
            self._done = True
            self.polygon_band.reset(QgsWkbTypes.PolygonGeometry)
            self.vertex_band.reset(QgsWkbTypes.PointGeometry)
            self.finished_callback(None)
        super().deactivate()

    def _finish(self, layer):
        if self._done:
            return
        self._done = True
        self.polygon_band.reset(QgsWkbTypes.PolygonGeometry)
        self.vertex_band.reset(QgsWkbTypes.PointGeometry)
        if self.previous_tool and self.previous_tool is not self:
            self.map_canvas.setMapTool(self.previous_tool)
        else:
            self.map_canvas.unsetMapTool(self)
        self.finished_callback(layer)
