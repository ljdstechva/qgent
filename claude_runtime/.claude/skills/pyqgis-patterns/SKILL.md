---
name: pyqgis-patterns
description: >
  Canonical PyQGIS snippets for the execute_pyqgis tool — loading vector/raster,
  CRS transforms (PH systems), memory layers, iterating/editing features,
  symbology basics, and Qt5/Qt6 API gotchas. Read this before writing PyQGIS.
---

# PyQGIS patterns

All names below are pre-injected into `execute_pyqgis`: `iface`, `QgsProject`,
`processing`, and every `Qgs*` class. `print()` anything you want returned as
evidence. Always `print` the name + `featureCount()` + `crs().authid()` of layers
you create.

## Loading data
```python
# Vector
vl = QgsVectorLayer("/path/roads.shp", "roads", "ogr")
if not vl.isValid():
    print("INVALID vector"); 
QgsProject.instance().addMapLayer(vl)

# Raster (DEM)
rl = QgsRasterLayer("/path/dem.tif", "dem")
QgsProject.instance().addMapLayer(rl)

# GeoPackage layer
gpkg = QgsVectorLayer("/path/data.gpkg|layername=parcels", "parcels", "ogr")
```

## Finding layers already in the project
```python
proj = QgsProject.instance()
lyr = proj.mapLayersByName("roads")[0]        # by name
active = iface.activeLayer()
for l in proj.mapLayers().values():
    print(l.name(), l.crs().authid(), type(l).__name__)
```

## CRS and reprojection
Common PH CRSs:
- `EPSG:4326` WGS84 (GPS/lat-lon)
- `EPSG:4683` PRS92 (national geodetic datum)
- `EPSG:32651` WGS84 / UTM zone 51N (Luzon/most of PH, metres — use for area/length)
- `EPSG:3123`–`3125` Luzon 1911 PTM zones (older cadastral)

```python
src = QgsCoordinateReferenceSystem("EPSG:4326")
dst = QgsCoordinateReferenceSystem("EPSG:32651")
tr = QgsCoordinateTransform(src, dst, QgsProject.instance())
pt = tr.transform(QgsPointXY(121.05, 14.60))
print(pt)

# Reproject a whole layer -> use processing (keeps attributes):
res = processing.run("native:reprojectlayer",
    {"INPUT": lyr, "TARGET_CRS": dst, "OUTPUT": "TEMPORARY_OUTPUT"})
out = res["OUTPUT"]; QgsProject.instance().addMapLayer(out)
print(out.name(), out.featureCount(), out.crs().authid())
```
Never assume two layers share a CRS. Measure length/area only in a projected
CRS (metres), not in EPSG:4326.

## Memory (scratch) layers
```python
mem = QgsVectorLayer("Point?crs=EPSG:32651&field=id:integer&field=name:string",
                     "sites", "memory")
dp = mem.dataProvider()
f = QgsFeature(mem.fields())
f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(300000, 1600000)))
f.setAttributes([1, "A"])
dp.addFeature(f)
mem.updateExtents()
QgsProject.instance().addMapLayer(mem)
print(mem.featureCount())
```

## Editing features safely (undoable)
```python
lyr.startEditing()
lyr.beginEditCommand("bulk update")     # groups into one Undo step
for feat in lyr.getFeatures():
    lyr.changeAttributeValue(feat.id(), lyr.fields().indexOf("status"), "ok")
lyr.endEditCommand()
lyr.commitChanges()                     # destructive-ish: writes to source
```
Prefer creating new layers over editing sources. `commitChanges()` and
`dataProvider().deleteFeatures()` will trigger the user approval gate.

## Field calculator via expression
```python
from qgis.core import QgsField
from qgis.PyQt.QtCore import QVariant
lyr.startEditing()
lyr.addAttribute(QgsField("area_ha", QVariant.Double))
lyr.updateFields()
idx = lyr.fields().indexOf("area_ha")
for f in lyr.getFeatures():
    lyr.changeAttributeValue(f.id(), idx, f.geometry().area() / 10000.0)
lyr.commitChanges()
```

## Zoom / canvas
```python
iface.setActiveLayer(lyr)
iface.zoomToActiveLayer()
canvas = iface.mapCanvas()
canvas.setExtent(lyr.extent()); canvas.refresh()
```

## Qt5 / Qt6 gotchas (QGIS 3.x = Qt5, QGIS 4.x = Qt6)
- Import Qt through `qgis.PyQt` (e.g. `from qgis.PyQt.QtCore import QVariant`),
  never `PyQt5`/`PyQt6` directly — it shims both.
- Use **fully-qualified enums**: `Qgis.GeometryType.Polygon`,
  `QgsWkbTypes.PointGeometry`, `Qgis.LayerType.Vector`. Bare int enums break on
  Qt6.
- `QgsProject.instance().addMapLayer(layer)` adds to layer tree; pass
  `addToLegend=False` to add without showing.
- `QVariant` still comes via `qgis.PyQt.QtCore` in both.
