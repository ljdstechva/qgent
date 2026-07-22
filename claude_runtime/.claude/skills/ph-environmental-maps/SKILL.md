---
name: ph-environmental-maps
description: >
  Philippine environmental-compliance mapping conventions and free data sources:
  vicinity maps for EIA/DENR submissions, flood-hazard styling, watershed
  context, and where to get NAMRIA/SRTM/ALOS/OSM/Phil-LIDAR data. Read for any
  DENR/EIA/environmental map task in the Philippines.
---

# Philippine environmental maps

For a professional A4 landscape vicinity-map deliverable, the Supervisor reads
`vicinity-map-template` and includes its applicable recipe in the cartographer
Task; its bundled QPT, Philippines locator asset, credential gate, and
verification checklist take precedence over the generic recipe here. A
QGIS-only cartographer must not call `execute_pyqgis` to read the skill file.

Context: the user is an environmental engineer / PCO in the Philippines
preparing DENR-EMB compliance documents (EIA, ECC, WDP, etc.). Maps must look
official and use standard elements.

## Coordinate systems (pick deliberately)
- Display/GPS: `EPSG:4326` (WGS84).
- National datum: `EPSG:4683` (PRS92).
- **Measurement/area/length: project to UTM** — most of the PH is zone 51N
  `EPSG:32651`; Mindanao east / eastern Visayas may need 51N still, far-east
  areas 52N `EPSG:32652`; far-west Palawan 50N `EPSG:32650`. Choose by longitude.

## Vicinity map (most common EIA requirement)
A locator showing the project site relative to surroundings (barangay, roads,
landmarks), usually on an A4/A3 sheet with title block, legend, scale bar, north
arrow, and a graticule.
- Build a point layer from the site coordinates (often given as lat/lon DMS or
  decimal), in EPSG:4326, then reproject to the appropriate UTM zone for a
  metric scale bar.
- Basemap: OSM (via XYZ tiles `type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png`)
  or a NAMRIA topo where required.
- Typical scales: site vicinity 1:10,000–1:50,000; regional locator inset
  1:250,000+. Add a red site marker/star and a labelled boundary.

Site point from decimal degrees:
```python
site = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string", "project_site", "memory")
dp = site.dataProvider(); f = QgsFeature(site.fields())
f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(121.0437, 14.6760)))  # lon, lat
f.setAttributes(["Project Site"]); dp.addFeature(f); site.updateExtents()
QgsProject.instance().addMapLayer(site)
```

## Flood-hazard styling (Project NOAH / DENR-MGB convention)
Susceptibility classes, low→high, colour-coded:
- Low = light blue/green, Moderate = yellow/orange, High = red.
```python
ramp = {"Low":"166,206,227", "Moderate":"253,191,111", "High":"227,26,28"}
cats = []
for cls, rgb in ramp.items():
    sym = QgsSymbol.defaultSymbol(hazard.geometryType())
    sym.setColor(QColor(*[int(x) for x in rgb.split(",")])); sym.setOpacity(0.6)
    cats.append(QgsRendererCategory(cls, sym, cls))
hazard.setRenderer(QgsCategorizedSymbolRenderer("SuscLevel", cats)); hazard.triggerRepaint()
```
Match the source dataset's actual class field/values — confirm them with
get_layer_features, don't assume field names.

## Standard layout elements for DENR submissions
Title block should carry: map title, project name, location (barangay,
municipality, province), prepared by / for, date, scale (bar + ratio), CRS/datum
note, north arrow, legend, and a data-source citation. Keep a neatline/frame.

## Free Philippine data sources
- **NAMRIA** — official topo maps, admin boundaries, coastline (national mapping
  agency). Base reference for compliance maps.
- **SRTM 30 m** / **ALOS PALSAR 12.5 m** DEMs — elevation, watershed, slope
  (via USGS EarthExplorer / ASF). ALOS is finer for terrain.
- **OpenStreetMap** (Geofabrik “Philippines” extract) — roads, buildings, POIs,
  waterways.
- **Project NOAH / UP Resilience Institute / DENR-MGB** — flood, landslide,
  storm-surge hazard layers.
- **Phil-LIDAR (DREAM/Phil-LiDAR 1)** — high-res DEM and flood models for covered
  river basins.
- **PAGASA** — rainfall/climate; **DENR-BMB** — protected areas / NIPAS.

When you cite a source in a map or to the user, name it plainly; when you need a
file the project doesn't have, tell the user exactly which dataset/agency to
fetch rather than fabricating a path. (data-scout can help validate what's
already loaded.)
