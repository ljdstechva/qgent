---
name: cartography-print-layout
description: >
  Programmatic symbology and QgsPrintLayout recipes — categorized/graduated
  renderers, labeling, and a full print layout with map frame, legend, scale
  bar, north arrow, grid, title, and PDF/PNG export. Read before styling or
  building a layout.
---

# Symbology & print layouts

## Single-symbol style
```python
from qgis.PyQt.QtGui import QColor
sym = QgsFillSymbol.createSimple({"color": "0,120,200,120",
                                  "outline_color": "0,60,120", "outline_width": "0.4"})
lyr.renderer().setSymbol(sym)
lyr.triggerRepaint(); iface.layerTreeView().refreshLayerSymbology(lyr.id())
```

## Categorized renderer
```python
from qgis.PyQt.QtGui import QColor
import random
field = "landuse"
cats = []
for v in sorted({f[field] for f in lyr.getFeatures()}):
    sym = QgsSymbol.defaultSymbol(lyr.geometryType())
    sym.setColor(QColor(random.randint(60,230), random.randint(60,230), random.randint(60,230)))
    cats.append(QgsRendererCategory(v, sym, str(v)))
lyr.setRenderer(QgsCategorizedSymbolRenderer(field, cats))
lyr.triggerRepaint()
```

## Graduated (choropleth)
```python
r = QgsGraduatedSymbolRenderer.createRenderer(
    lyr, "pop_density", 5,
    QgsGraduatedSymbolRenderer.Jenks,
    QgsSymbol.defaultSymbol(lyr.geometryType()),
    QgsGradientColorRamp(QColor(255,255,204), QColor(178,24,43)))
lyr.setRenderer(r); lyr.triggerRepaint()
```

## Labels
```python
s = QgsPalLayerSettings()
s.fieldName = "name"; s.enabled = True
s.placement = QgsPalLayerSettings.OverPoint  # or .Line / .AroundPoint
txt = QgsTextFormat(); txt.setSize(9)
s.setFormat(txt)
lyr.setLabeling(QgsVectorLayerSimpleLabeling(s))
lyr.setLabelsEnabled(True); lyr.triggerRepaint()
```

## Full print layout (map + legend + scale bar + north arrow + grid + title)
```python
project = QgsProject.instance()
manager = project.layoutManager()
name = "Vicinity Map A3"
old = manager.layoutByName(name)
if old: manager.removeLayout(old)          # replace, don't stack duplicates
layout = QgsPrintLayout(project); layout.initializeDefaults()
layout.setName(name)
# A3 landscape
from qgis.core import QgsLayoutSize, QgsUnitTypes
pc = layout.pageCollection(); pc.pages()[0].setPageSize("A3", QgsLayoutItemPage.Landscape)
manager.addLayout(layout)

# Map item
mp = QgsLayoutItemMap(layout)
mp.setRect(20, 20, 200, 180)
mp.setExtent(iface.mapCanvas().extent())    # or a specific QgsRectangle
mp.setCrs(project.crs())
layout.addLayoutItem(mp)
mp.attemptMove(QgsLayoutPoint(10, 20, QgsUnitTypes.LayoutMillimeters))
mp.attemptResize(QgsLayoutSize(240, 170, QgsUnitTypes.LayoutMillimeters))

# Grid (graticule)
grid = mp.grid(); grid.setEnabled(True); grid.setIntervalX(1000); grid.setIntervalY(1000)

# Legend
legend = QgsLayoutItemLegend(layout); legend.setTitle("Legend")
legend.setLinkedMap(mp)
layout.addLayoutItem(legend)
legend.attemptMove(QgsLayoutPoint(255, 25, QgsUnitTypes.LayoutMillimeters))

# Scale bar
sb = QgsLayoutItemScaleBar(layout); sb.setStyle("Single Box"); sb.setLinkedMap(mp)
sb.applyDefaultSize(); layout.addLayoutItem(sb)
sb.attemptMove(QgsLayoutPoint(255, 150, QgsUnitTypes.LayoutMillimeters))

# North arrow (picture from QGIS SVG search paths)
na = QgsLayoutItemPicture(layout)
na.setPicturePath("/usr/share/qgis/svg/arrows/NorthArrow_02.svg")  # path varies by OS
layout.addLayoutItem(na)
na.attemptResize(QgsLayoutSize(18, 18, QgsUnitTypes.LayoutMillimeters))
na.attemptMove(QgsLayoutPoint(255, 100, QgsUnitTypes.LayoutMillimeters))

# Title
title = QgsLayoutItemLabel(layout); title.setText("VICINITY MAP")
title.setFont(QFont("Arial", 18)); title.adjustSizeToText()
layout.addLayoutItem(title)
title.attemptMove(QgsLayoutPoint(10, 5, QgsUnitTypes.LayoutMillimeters))
```
(`from qgis.PyQt.QtGui import QFont`.) If a north-arrow SVG path isn't found,
locate one via `QgsApplication.svgPaths()` and search for `arrows/`.

## Export
```python
exporter = QgsLayoutExporter(layout)
pdf = "/tmp/vicinity.pdf"
res = exporter.exportToPdf(pdf, QgsLayoutExporter.PdfExportSettings())
print("PDF export result:", res, "->", pdf)   # res == QgsLayoutExporter.Success (0)
# PNG
img = QgsLayoutExporter.ImageExportSettings(); img.dpi = 300
exporter.exportToImage("/tmp/vicinity.png", img)
```
Always confirm the export succeeded and the file exists before reporting done.
