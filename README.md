# Agri-Energy DSS pipeline: Tasks 1 to 3

Single-cluster engine for the Agri-Energy Decision Support System. Give it a latitude and longitude;
it finds the farm parcels within 150 m, works out their daily water need, fetches sunshine data from NASA,
sizes a solar pump and reports the surplus electricity.

- **Task 1**: spatial ingestion and clustering (EuroCrops Portugal, 150 m tube-well command zone).
- **Task 2**: water demand, NASA POWER sunshine at the cluster centre, hydraulic pump power.
- **Task 3**: IDCOL pump-tier matching and daily PV surplus energy, driven by Task 2's outputs.

Task 4 (PUE selection and payback) is not implemented.


## Run

```bash
python -m venv .venv
.venv\Scripts\activate                  # Windows; macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

python run_pipeline.py --download       # fetches data/PT.zip (37 MB) once, then runs the executable script
python run_pipeline.py --lat 38.0829 --lon -8.1252      # any site
python -m pytest                        # 148 tests, offline except two groups noted below
```

Requires Python 3.10 or newer. The runner needs internet access for NASA POWER.
Exit code: `0` = everything worked, `1` = dataset, input or NASA error (or a custom site with no parcels).

| File | Role |
|---|---|
| `dss_pipeline.py` | All the logic: loading, clustering, equations, NASA client, pump choice, `evaluate_site` |
| `run_pipeline.py` | Command-line runner; the default run is the DSS's verification executable |
| `tests/` | Unit tests on made-up parcels with hand-calculated answers; NASA tests replay saved replies (`tests/fixtures/`); real-data tests (`-m integration`, need `data/PT.zip`) and one live NASA test (`-m network`), each skipped when its resource is unavailable |

## Code map

| Stage | Main functions |
|---|---|
| Load | `ensure_dataset` (download), `load_parcels` (read `PT.zip`, convert to metres) |
| Task 1 | `command_zone` (project, then buffer in metres), `extract_cluster` (spatial index, clip, areas), `hcat_category` / `classify_parcels` (crop categories), `nearest_parcel` (re-siting hint), `EmptyClusterError` |
| Task 2 | `daily_water_demand_m3`, `hydraulic_power_kw`, `fetch_solar_resource` and `parse_power_climatology` (NASA POWER, cached), `SolarResource` |
| Task 3 | `select_pump` (Table 2 as a DataFrame filter), `pv_energy_kwh_day`, `pump_energy_kwh_day`, `surplus_energy_kwh_day`, `model_energy_balance`, `monthly_energy_profile` |
| All | `evaluate_site` returns a `SiteAssessment` with the cluster, Task 2 outputs and Task 3 balance |

## Dictionary

| Word | Meaning |
|---|---|
| **Parcel** | One field, stored as an outline (a polygon) on a map, plus a label saying what grows there. |
| **Hectare (ha)** | A unit of land area. 1 ha = 10,000 m², about one and a half football pitches. |
| **Cluster** | The group of parcels that touch the 150 m circle around your point. One pump serves one cluster. |
| **Command zone** | The circle itself (radius 150 m, about 7 ha). The area one tube-well can reach. |
| **Crop code (HCAT)** | A 10-digit number saying what grows in a parcel, for example `3303050000` = olives. |
| **Crop category** | Our grouping of crop codes into 5 buckets (cereals, orchards, pasture, vegetables, other). Each bucket has a known water need. |
| **m³ (cubic metre)** | 1,000 litres of water. `m³/ha/day` means cubic metres per hectare per day. |
| **Head (H)** | How high the pump must lift the water, including losses in the pipe. Here 40 m. |
| **kW (kilowatt)** | **Power**: how fast energy is used or made *at one moment*. A 3.7 kW motor draws 3.7 kW while it runs. |
| **kWh (kilowatt-hour)** | **Energy**: power x time. A 3.7 kW motor running 6 hours uses 22.2 kWh. |
| **kWp (kilowatt-peak)** | The panel rating: power it makes in perfect full sun. A "5 kWp" array makes at most 5 kW. |
| **PSH (peak sun hours)** | A way to describe daily sunshine: "the sun was as strong as 4.9 hours of perfect full sun". Same number as kWh per m² per day. |
| **Derate (0.75)** | Real panels lose output to heat, dust and wiring. We assume they deliver 75 % of the ideal figure. |
| **Efficiency (0.55)** | Only 55 % of the electricity the motor takes ends up lifting water. The rest is lost as heat. |
| **Tier** | A pump size from the catalog (Tier 1 smallest, Tier 4 largest). |
| **CRS / EPSG code** | A "coordinate system": the rule for turning places on Earth into numbers. See the next row. |
| **EPSG:4326** | Latitude and longitude, in **degrees**. What GPS and NASA use. Fine for locating a place, **bad for measuring** (one degree is about 100 km, and it varies). |
| **EPSG:3763** | A map system made for Portugal where x and y are in **metres**. Good for measuring distances and areas. |
| **Reproject** | Convert coordinates from one system to another (here degrees to metres). |
| **Buffer** | A shape drawn around a point at a fixed distance. A 150 m buffer is a circle. |
| **Spatial index** | A lookup structure for maps, like a book index. It finds the few parcels near a point without checking all 100,000. |
| **Climatology** | Weather averaged over many years (here 2001 to 2025), not a forecast. |
| **API** | A web address a program can ask for data. NASA POWER is an API. |
| **Cache** | Remembering an answer so we do not ask the internet twice for the same thing. |
| **Exception** | Python's way of stopping with a named error, for example `EmptyClusterError`. |
| **DataFrame** | A table in Python (rows and columns), from the `pandas` library. A **GeoDataFrame** is the same with a map shape in every row. |

---

## Findings in the data and the API

| Finding | Handling |
|---|---|
| All 100,000 `EC_hcat_c` codes start with `33`, so the spec's "first two digits" rule would send every parcel to the fallback. | Longest-prefix match on HCAT groups. The spec's two-digit keys are kept. (A1) |
| Big parcels clip the 7.07 ha zone. At the mixed Alentejo site: 46.9 ha whole-parcel vs 5.7 ha inside the circle. | Demand uses the area inside the circle. Both areas are printed. (A2) |
| The spec's NASA URL (`community=AG`) returns MJ/m²/day, not kWh. Alentejo annual: 17.65 MJ = 4.90 kWh. | Convert by the units NASA states in its reply. (A6) |
| The spec's Alentejo point (38.015, -7.865) has no parcel within 1.33 km. `PT.zip` holds exactly 100,000 parcels, sparse in Alentejo. | `EmptyClusterError` as specified. The runner reports the nearest parcel and re-sites there. (A9) |
| Table 2 pairs about 1.35 kWp per motor kW. With a 0.75 derate and 6 h/day pumping, a surplus needs PSH above about 5.9. Portugal's annual mean is about 4.9 to 5.0. | `E_excess` is 0 on the yearly average. The deficit and break-even PSH are printed, and the monthly view shows the May to August surplus. |

Assumptions A1 to A9 are listed in the `dss_pipeline.py` docstring, which also holds the short Monte Carlo note.

Spec verification: central Lisbon raises `EmptyClusterError` with the exact spec message. The spec Alentejo point
raises it too, because of the data gap above. A re-sited Alentejo site takes about 0.3 to 1 s including the
NASA request, and a few milliseconds when cached.

## Monte Carlo extension (regional policy modelling)

For regional policy modellling, ff "Monte Carlo" means running the same calculation thousands of times with 
randomly varied inputs to see the spread of outcomes. `evaluate_site` is built for it: give it a random point, 
random settings (for example a different `head_m` from a water-depth guess) and a random sunshine year, and keep 
the result. Count how often `EmptyClusterError` appears (that is "not farmland"). A policy test, such as a subsidy 
on pump cost, simply changes the cost figure afterwards. Each run is independent, so thousands can run side by side. 
The details are in the docstring at the top of `dss_pipeline.py`. Additionally, multithreading can be leveraged for
parallel en masse computes with current implementation.


## 3D visualiser (extra, local only)

An attempt at viewing the results on a map, in `ignore/visualiser/`. It is a React app with a custom renderer: 
OpenStreetMap tiles, extruded parcels, the 150 m zone and the pump result. See its own README.

```bash
.venv\Scripts\python ignore/visualiser/export_data.py   # parcels + DSS results -> JSON
cd ignore/visualiser && npm install && npm run dev      # http://localhost:5173
```

## Sample output (site [2])

```
[2] Alentejo mixed-crop cluster  (37.97883, -7.73698)
  Task 1 | 4 parcel(s) | farmland in zone 5.65 of 7.07 ha | whole parcels 46.89 ha
         |   cat            category  WD m3/ha/day  n  in_zone_ha  parcel_ha  Q m3/day
         |    20   Orchards & Fruits         45.00  2        1.95      42.60     87.75
         | other Fallback Field Crop         30.00  2        3.70       4.29    111.14
  Task 2 | Q_day = 198.9 m3/day
         | NASA POWER at (37.98, -7.74): PSH_avg 5.01 kWh/m2/day | dullest month DEC 2.26
         | P_hyd = rho.g.Q.H / (3.6e6.t.eta) = 6.57 kW  (H 40 m, eta 0.55, t_pump 6 h/day)
  Task 3 | Tier 3 x 1 for P_hyd 6.57 kW | motor 7.5 kW | PV 10 kWp | CAPEX $14,000
         | E_PV     = 10 kWp x 5.01 h/day x 0.75     =   37.60 kWh/day
         | E_pump   = 7.5 kW x 6 h/day               =   45.00 kWh/day
         | E_excess = max(0, E_PV - E_pump)          =    0.00 kWh/day
         | no surplus at PSH_avg: short by 7.40 kWh/day; needs PSH > 6.00
         | by month, surplus in: MAY, JUN, JUL, AUG
         | month             JAN  FEB  MAR  APR  MAY   JUN   JUL  AUG  SEP  OCT  NOV  DEC
         | PSH kWh/m2/day   2.52 3.47 4.57 5.70 6.86  7.69  7.95 7.04 5.41 3.84 2.77 2.26
         | E_excess kWh/day 0.00 0.00 0.00 0.00 6.46 12.65 14.60 7.77 0.00 0.00 0.00 0.00
```
