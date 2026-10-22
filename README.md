# Agri-Energy DSS pipeline: Tasks 1 to 3

Single-cluster engine for the Agri-Energy Decision Support System assessment.

- **Task 1**: spatial ingestion and clustering (EuroCrops Portugal, 150 m tube-well command zone).
- **Task 2**: water demand, live NASA POWER climatology at the cluster centroid, hydraulic pump power.
- **Task 3**: IDCOL pump-tier matching and daily PV surplus energy, driven by Task 2's outputs.

Task 3 runs on Task 2's outputs: the tier is matched on Task 2's hydraulic power and Q_day, and E_PV uses Task 2's PSH_avg. The same balance is also shown month by month. PUE economics (Task 4) are not implemented.

Design notes live in the `dss_pipeline.py` module docstring: CRS handling, spatial indexing, units, assumptions A1 to A9, and the Monte Carlo extension.

## Run

```bash
python -m venv .venv
.venv\Scripts\activate                  # Windows; macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

python run_pipeline.py --download       # fetches data/PT.zip (37 MB) once, then runs the verification suite
python run_pipeline.py --lat 38.0829 --lon -8.1252                  # any site
python run_pipeline.py --area-basis parcel                          # literal whole-parcel reading (A2)
python -m pytest                        # 181 tests, offline except one live NASA POWER check
```

The runner needs internet access for NASA POWER. Exit codes: 0 ok, 1 dataset error, 2 invalid or empty custom site, 3 NASA POWER unavailable after retries.

| File | Role |
|---|---|
| `dss_pipeline.py` | Engine: ETL, clustering, demand, NASA client, hydraulic power, tier matching, energy balance, `evaluate_site` |
| `run_pipeline.py` | CLI runner; the default run is the spec verification suite |
| `tests/` | Unit tests on synthetic geometry with analytic expectations; NASA client tests replaying recorded responses (`tests/fixtures/`) with scripted failures; real-data integration tests (`-m integration`) and one live API test (`-m network`), each skipped when its resource is unavailable |

Requires Python 3.10 or newer.

## Code map

`dss_pipeline.py` is split by concern, so each stage can be reused or replaced on its own:

| Stage | Main functions and classes |
|---|---|
| ETL | `ensure_dataset` (atomic download: writes `.part`, then renames), `load_parcels` (reads `PT.zip`, repairs schema and CRS, returns EPSG:3763) |
| Task 1 | `command_zone` (project, then buffer in metres), `extract_cluster` (STRtree query, exact clip, hectare areas), `classify_parcels` and `hcat_category` (Table 1 mapping), `nearest_parcel` (re-siting hint), `EmptyClusterError` |
| Task 2 | `daily_water_demand_m3`, `hydraulic_power_kw`, `NasaPowerClient` and `parse_power_climatology` (resilient API access, unit normalisation), `SolarResource` |
| Task 3 | `select_pump` (Table 2 as a DataFrame, vectorised tier filter), `pv_energy_kwh_day`, `pump_energy_kwh_day`, `surplus_energy_kwh_day`, `model_energy_balance`, `monthly_energy_profile` |
| Orchestration | `evaluate_site` returns a `SiteAssessment` holding the cluster, Task 2 outputs and Task 3 balance |

`run_pipeline.py` only parses flags, calls `evaluate_site` and prints. Its helpers (`_report`, `_site`, `main`) each carry a docstring describing what they print and which exit code they return.

## Extending to a Monte Carlo policy study

`evaluate_site` is deterministic and takes its tunable inputs explicitly: `DesignParameters` (head, efficiency, pumping hours, derate, area basis) and a `SolarClient`. A regional run is an outer loop with a seeded random generator. It draws sites from parcel points, draws parameters with `dataclasses.replace`, supplies a solar client that samples NASA years, and tallies `EmptyClusterError`. Policy levers such as CAPEX subsidies are applied to `total_capex_usd` and `e_excess_kwh_day`. Draws are independent, so they parallelise. Details are in the module docstring.

## Findings in the data and the API, and how the engine handles them

| # | Finding | Handling |
|---|---|---|
| A1 | All 100,000 `EC_hcat_c` codes start with `33`, the HCAT root. The spec's "first two digits" rule would send every parcel to the fallback. | Longest-prefix mapping on HCAT level-1/2 groups (cereals, permanent crops, grassland, horticulture). The spec's two-digit keys are kept. |
| A2 | Large Alentejo parcels clip the 7.07 ha zone. Mixed site: 46.9 ha whole-parcel vs 5.7 ha in-zone → 7 × Tier 4 vs 1 × Tier 3. | Demand uses in-zone area by default; `--area-basis parcel` switches to the literal reading. Both areas are always reported. |
| A7 | The spec's NASA URL (`community=AG`) returns MJ/m²/day, not kWh: annual 17.65 at Alentejo, i.e. 4.90 kWh/m²/day. | The parser converts by the units the payload declares and rejects unknown units. PSH > 10 is refused as a unit error. |
| A8 | The spec Alentejo point (38.015, -7.865) has no parcel within 1.33 km. PT.zip holds exactly 100,000 parcels, sparse in Alentejo. | `EmptyClusterError` as specified. The runner reports the nearest parcel and re-sites there. |
| A9 | NASA POWER can be slow, rate-limited or down. | 10 s timeout per attempt; 3 attempts with 1 s / 2 s backoff on timeouts, connection errors and HTTP 429/5xx. HTTP 4xx and malformed payloads fail fast with NASA's own message. Results are cached in memory per ~1 km point; failures are not cached. |
| (none) | The catalog pairs about 1.35 kWp per motor kW. With a 0.75 derate and 6 h/day pumping, a surplus needs PSH > ~5.9. Portugal's annual mean (~4.9–5.0) gives E_excess = 0. | The zero-surplus case is explicit: the deficit and break-even PSH are reported. The monthly view shows the surplus from May to August. |

Spec verification: central Lisbon raises `EmptyClusterError` with the exact spec message. The spec Alentejo point raises it too, because of the data gap above. A re-sited Alentejo site takes about 0.3 s including the NASA round trip, or a few ms when cached.

## Sample output (site [2])

```
[2] Alentejo olive/fallow cluster  (37.97883, -7.73698)
  Task 1 | 4 parcel(s) | farmland in zone 5.65 of 7.07 ha | whole parcels 46.89 ha
         |   cat            category  WD m3/ha/day  n  in_zone_ha  parcel_ha  Q m3/day (zone)
         |    20   Orchards & Fruits         45.00  2        1.95      42.60            87.75
         | other Fallback Field Crop         30.00  2        3.70       4.29           111.14
  Task 2 | Q_day = 198.9 m3/day
         | NASA POWER at centroid (37.98, -7.74): PSH_avg 5.01 kWh/m2/day | design month DEC 2.26 kWh/m2/day
         | source: NASA POWER SYN1DEG climatology (MJ/m^2/day), normalised to kWh/m2/day
         | P_hyd = rho.g.Q.H / (3.6e6.t.eta) = 6.57 kW  (H 40 m, eta 0.55, t_pump 6 h/day)
  Task 3 | Tier 3 x 1 for P_hyd 6.57 kW | motor 7.5 kW | PV 10 kWp | CAPEX $14,000 | motor load 88%
         | E_PV     = 10 kWp x 5.01 h/day x 0.75     =   37.60 kWh/day
         | E_pump   = 7.5 kW x 6 h/day               =   45.00 kWh/day
         | E_excess = max(0, E_PV - E_pump)          =    0.00 kWh/day
         | no surplus at PSH_avg: deficit 7.40 kWh/day; needs PSH > 6.00
         | by month (same fleet), surplus in: MAY, JUN, JUL, AUG
         | month             JAN  FEB  MAR  APR  MAY   JUN   JUL  AUG  SEP  OCT  NOV  DEC
         | PSH kWh/m2/day   2.52 3.47 4.57 5.70 6.86  7.69  7.95 7.04 5.41 3.84 2.77 2.26
         | E_excess kWh/day 0.00 0.00 0.00 0.00 6.46 12.65 14.60 7.77 0.00 0.00 0.00 0.00
```
