# Field Viewer (3D parcel map)

React + a hand-written three.js renderer. Shows the EuroCrops parcels around a tube-well on OpenStreetMap tiles,
with the 150 m command zone and the DSS pump / energy result.

## Run

```bash
# 1. Export parcels + DSS results as JSON (project venv; NASA is optional)
.venv\Scripts\python ignore/visualiser/export_data.py                    # verification sites + one dense area
.venv\Scripts\python ignore/visualiser/export_data.py --lat 38.08 --lon -8.12 --name my-site   # add a site

# 2. Start the viewer
cd ignore/visualiser
npm install
npm run dev                                                              # http://localhost:5173
```

Pick a site from the top-right list (it is also kept in the URL hash, e.g. `#tras-os-montes`).

## What you see

| Thing | Meaning |
|---|---|
| Prisms | One per parcel. Height shows crop water need, water used inside the zone, or nothing (flat). |
| Colours | Crop category (legend, bottom-left). Parcels outside the zone are washed out. |
| Cyan cylinder | The 150 m command zone. Its centre pole is the tube-well. |
| Droplets | Water flowing from the well to each parcel in the zone; more droplets = more water used. |
| Right panel | Task 2 and 3 results for the site, plus the month-by-month sunshine. |

Controls: drag to orbit, right-drag or Shift-drag to pan, scroll or pinch to zoom, click a parcel to select it.

## Code map (`src/render/`)

| File | Job |
|---|---|
| `SceneRenderer.js` | The engine: scene, render loop, GPU picking, fog. React talks to this one class. |
| `CameraRig.js` | Own orbit / pan / zoom controls with damping. |
| `mercator.js` | Web Mercator maths and the local metre frame (x east, z south). |
| `TileLayer.js` | OSM tile loader: three zoom levels, a queue, and depth offsets so levels never flicker. |
| `ParcelLayer.js` | Parcels merged into one mesh. A shader sets each prism's height, so mode changes cost nothing. |
| `ZoneLayer.js` | Cylinder, ring, beacon, ripples, and GPU water droplets. |

## Notes

- Tiles come from `tile.openstreetmap.org`; keep use light (the layer limits parallel requests) and keep the attribution.
- The map is flat: there is no terrain model, so prism heights are relative, not real elevations.
- `data/` under `public/` is generated; re-run the export after changing the DSS code.
