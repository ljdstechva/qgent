---
name: processing-recipes
description: >
  Most-used QGIS Processing algorithm ids with their parameter schemas and how
  to chain them — buffer, clip, dissolve, reproject, intersection, GDAL merge,
  raster fill sinks / flow / watershed (SAGA, GRASS, WhiteboxTools). Read before
  calling run_processing or scripting processing.run.
---

# Processing recipes

Run with the `run_processing` tool or inside `execute_pyqgis` via
`processing.run(alg_id, params)`. `OUTPUT: "TEMPORARY_OUTPUT"` yields a scratch
layer; give a file path (`.gpkg`, `.shp`, `.tif`) to persist. Always add the
result to the project and print its feature count.

Discover algorithms:
```python
for a in QgsApplication.processingRegistry().algorithms():
    if "buffer" in a.id(): print(a.id(), "-", a.displayName())
```

## Vector — core
| Task | alg id | key params |
|---|---|---|
| Buffer | `native:buffer` | INPUT, DISTANCE (map units), SEGMENTS, DISSOLVE(bool) |
| Clip | `native:clip` | INPUT, OVERLAY |
| Intersection | `native:intersection` | INPUT, OVERLAY |
| Dissolve | `native:dissolve` | INPUT, FIELD([]) |
| Reproject | `native:reprojectlayer` | INPUT, TARGET_CRS |
| Centroids | `native:centroids` | INPUT |
| Fix geometries | `native:fixgeometries` | INPUT |
| Field calc | `native:fieldcalculator` | INPUT, FIELD_NAME, FORMULA, FIELD_TYPE |
| Join by location | `native:joinattributesbylocation` | INPUT, JOIN, PREDICATE, JOIN_FIELDS |
| Join by attribute | `native:joinattributestable` | INPUT, FIELD, INPUT_2, FIELD_2 |
| Merge layers | `native:mergevectorlayers` | LAYERS([]), CRS |
| Extract by expr | `native:extractbyexpression` | INPUT, EXPRESSION |

Buffer example (metres → INPUT must be in a projected CRS):
```python
res = processing.run("native:buffer", {
    "INPUT": lyr, "DISTANCE": 100, "SEGMENTS": 8,
    "DISSOLVE": False, "OUTPUT": "TEMPORARY_OUTPUT"})
buf = res["OUTPUT"]; QgsProject.instance().addMapLayer(buf)
print("buffer", buf.featureCount(), buf.crs().authid())
```
If the layer is in EPSG:4326, reproject to UTM (e.g. 32651) first, or the
distance is in degrees.

## Raster — hydrology / DEM
GDAL:
| Task | alg id |
|---|---|
| Merge rasters | `gdal:merge` |
| Clip raster by mask | `gdal:cliprasterbymasklayer` |
| Slope | `gdal:slope` |
| Hillshade | `gdal:hillshade` |
| Contours | `gdal:contour` (INTERVAL) |

Watershed / flow (provider must be installed):
- **SAGA**: `sagang:fillsinkswangliu` (INPUT→FILLED), then
  `sagang:catchmentarea` / `sagang:channelnetworkanddrainagebasins`.
- **GRASS**: `grass7:r.watershed` (elevation, threshold → drainage, basin,
  accumulation, stream).
- **WhiteboxTools** (plugin): `wbt:BreachDepressions`, `wbt:D8Pointer`,
  `wbt:D8FlowAccumulation`, `wbt:Watershed` (pour points).

Typical watershed chain (WhiteboxTools):
```python
fill = processing.run("wbt:BreachDepressions", {"dem": dem, "output":"TEMPORARY_OUTPUT"})["output"]
ptr  = processing.run("wbt:D8Pointer", {"dem": fill, "output":"TEMPORARY_OUTPUT"})["output"]
acc  = processing.run("wbt:D8FlowAccumulation", {"input": fill, "out_type":"cells", "output":"TEMPORARY_OUTPUT"})["output"]
ws   = processing.run("wbt:Watershed", {"d8_pntr": ptr, "pour_pts": pour_pts_layer, "output":"TEMPORARY_OUTPUT"})["output"]
```
Check the provider is active first:
```python
print("wbt" in [p.id() for p in QgsApplication.processingRegistry().providers()])
```
If a provider is missing, tell the user which plugin to enable rather than
inventing an algorithm id.

## Chaining tip
Feed one algorithm's `res["OUTPUT"]` straight into the next as `INPUT`; only add
the final result to the project unless the user wants intermediates. After each
step, sanity-check `featureCount()` / band stats — a 0-feature or all-nodata
intermediate means stop and diagnose.
