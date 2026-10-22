# SUMMARY: what this repo does and how the pieces fit

## 1. The problem in plain words

Small farms in hot regions often pump irrigation water with **diesel pumps**. Diesel is expensive and dirty.
The alternative is a pump powered by **solar panels**. Before anyone buys one, they need answers to:

1. Which fields would this pump water, and how much water do those fields need each day?
2. How big must the pump be?
3. How much sunshine is there, so how much electricity do the panels make?
4. Do the panels make **more** electricity than the pump uses? Leftover electricity could run something
   useful (a cold room, a grain mill). That leftover is called the **surplus**.

This repo answers questions 1 to 4 for **one farming cluster** at a time. You give it a point on the map
(latitude and longitude). It looks at the fields within 150 metres of that point and prints the answers.

This was written for a job assessment that had four tasks. **Tasks 1, 2 and 3 are done. Task 4** (picking the
most profitable use for the surplus) is **not done**, on purpose.

| Task | Question it answers | Main code |
|---|---|---|
| 1 | Which fields are near this point? | `extract_cluster` |
| 2 | How much water per day? How much sun? How powerful a pump? | `daily_water_demand_m3`, `fetch_solar_resource`, `hydraulic_power_kw` |
| 3 | Which pump from the catalog? Is there surplus electricity? | `select_pump`, `model_energy_balance` |

---

## 2. Words you will meet

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

## 3. The big picture

```
   INPUTS                                STEP                                       RESULT
   ------                                ----                                       ------

   your point (lat, lon)  --+
   PT.zip (100,000 fields) -+--->  TASK 1   find the fields near the point  --->  parcels: hectares + crop type
                                                    |
   Table 1 (water per crop) ---->  TASK 2a  water needed per day  ------------>  Q_day    (m3/day)
                                                    |
   NASA POWER (internet)  ------>  TASK 2b  sunshine at the cluster centre  --->  PSH      (kWh/m2/day)
                                                    |
   design numbers (40 m, 6 h...) > TASK 2c  pump power needed  --------------->  P        (kW)
                                                    |
   Table 2 (pump catalog)  ----->  TASK 3a  pick a pump tier  ---------------->  tier, motor kW, panels kWp
                                                    |
                                   TASK 3b  energy made vs energy used  ------->  E_PV, E_pump, E_excess (kWh/day)
```

The arrows show the order the steps run in. Which result feeds which equation is spelled out in
[section 7](#7-equation-dependency) (for example the sunshine from step 2b is only needed in step 3b).

One function, `evaluate_site`, runs this whole chain. `run_pipeline.py` calls it and prints the results.

---

## 4. What is fetched, and what it is used for

| Source | What it is | How we get it | Used for |
|---|---|---|---|
| **EuroCrops Portugal** `PT.zip` | A map file (a "shapefile", zipped) of 100,000 farm parcels in Portugal, 2021, with a crop code on each. 37 MB. | Downloaded once from `zenodo.org` into `data/PT.zip` by `ensure_dataset` (`--download`). Read by `load_parcels`. | Task 1: where fields are, how big, what crop. |
| **NASA POWER** | A free NASA web service with satellite-based sunshine averages for any point on Earth. | One internet request per cluster, by `fetch_solar_resource`. No API key needed. Answers are remembered (cached) in memory. | Task 2: sunshine (PSH) for Task 3's solar energy. |
| **Table 1** | Water need per day for each crop bucket (for example orchards: 45 m³/ha/day). | Typed into the code as `CROP_ARCHETYPES`. Given by the assessment. | Task 2: water demand. |
| **Table 2** | Pump catalog: 4 sizes with motor kW, panel kWp, price, maximum daily water delivery. | Typed into the code as `PUMP_CATALOG`. Given by the assessment. | Task 3: choosing the pump. |
| **Design numbers** | Head 40 m, efficiency 0.55, 6 pumping hours, derate 0.75, radius 150 m. | `DesignParameters` in the code. Given by the assessment. | Tasks 1 to 3. |

Columns used from `PT.zip`: only `OSA_ID` (parcel id), `EC_hcat_c` (crop code) and `EC_hcat_n` (crop name),
plus the outline of each parcel. The other columns are ignored.

---

## 5. Step by step

### Task 1: find the fields near a point

```
 (lat, lon in degrees)
        |
        v
 1. convert the point from degrees to metres (EPSG:4326 -> EPSG:3763)
        |
        v
 2. draw a circle of radius 150 m around it                 <- the "buffer"
        |
        v
 3. ask the spatial index: "which parcels touch this circle?"
        |
        v
 4. repair broken outlines, cut each parcel to the circle
        |    -> area_ha            (whole parcel)
        |    -> area_in_zone_ha    (only the part inside the circle)
        v
 5. look up the crop category of each parcel (olives -> "Orchards", wheat -> "Cereals", ...)
        |
        +--> no parcel inside?  stop with EmptyClusterError
        v
 Cluster: a table with one row per parcel
```

**Why convert to metres before drawing the circle?** In degrees, "150" means 150 degrees (over 15,000 km).
We convert the point to metres first, and only then draw a circle with a radius of 150.

**What if nothing is there?** A point in a city, in the sea or in another country has no parcels in its circle.
The code stops with `EmptyClusterError` and this exact message, as the assessment requires:
`Location is out of agricultural domain or contains no parcels within the 150m radius.`

### Task 2: water, sunshine, pump power

```
 Cluster ---> Q_day = add up (area_in_zone_ha x water need of its crop category)   [m3/day]
    |
    +------> centre of the farmland in the circle ---> ask NASA POWER ---> 12 monthly PSH + yearly average
    |
 Q_day ---> P = power a pump needs to lift Q_day by 40 m in 6 hours              [kW]
```

NASA is asked about the **centre of the farmland** (not the exact point you typed), rounded to 0.01 degree
(about 1 km). NASA's sunshine map has squares of about 100 km, so this loses nothing.

If the internet is down, slow (more than 10 seconds) or NASA sends something unusable, the code stops with
`SolarDataError` and a clear message instead of guessing.

### Task 3: pump tier and surplus energy

```
 P (kW) and Q_day (m3/day) ---> smallest tier whose motor AND water delivery are big enough
        |
        v
 that tier gives: motor kW, panel kWp, price
        |
        +--> E_PV   = panel kWp x PSH_average x 0.75      energy the panels make per day
        +--> E_pump = motor kW x 6 hours                  energy the pump uses per day
        v
 E_excess = the larger of (0, E_PV - E_pump)             surplus per day
```

The output also shows the same calculation for each of the 12 months, because a yearly average can hide the
fact that summer months have a surplus while winter months do not.

---

## 6. What needed preprocessing, and why

Raw data was never usable as-is. Everything below happens automatically in the code.

| # | Problem in the raw data | What we do | Where |
|---|---|---|---|
| 1 | Parcel outlines are in **degrees**. Areas and distances in degrees are meaningless. | Convert the whole file once to metres (EPSG:3763). The user's point is converted the same way before the circle is drawn. | `load_parcels`, `command_zone` |
| 2 | The file has text columns with broken accents (for example `Superf�cie`). | Read only the 3 columns we need. | `load_parcels` |
| 3 | Parcel ids are stored as decimals (`506762.0`). | Convert to whole numbers. | `load_parcels` |
| 4 | **78 parcel outlines are invalid**, for example a line that crosses itself in a "bow-tie" shape. Area and cutting give wrong numbers on such shapes. | Repair them (`make_valid`). Healthy outlines are unchanged. | `extract_cluster` |
| 5 | Searching 100,000 outlines one by one is slow. | Use the spatial index. A lookup takes well under a second. | `extract_cluster` |
| 6 | A big parcel can stick out of the circle. | Cut each parcel to the circle and keep **both** areas (whole parcel and inside-circle). Water demand uses the inside part. | `extract_cluster` |
| 7 | **All 100,000 crop codes start with `33`.** The assessment says to use the first two digits, which would put every parcel in the "other" bucket. | Match each code to the longest known prefix instead. For example `330308...` means shrubland, otherwise `3303...` means permanent crops. | `HCAT_PREFIX_RULES`, `hcat_category` |
| 8 | **NASA gives MJ/m²/day** for the URL in the assessment, but we need kWh/m²/day. | Read the units NASA states in its reply and convert: divide MJ by 3.6. Stop if the units are unknown. | `parse_power_climatology` |
| 9 | NASA writes `-999` where data is missing. | Treat any negative value as a gap and stop with an error, instead of using it. | `parse_power_climatology` |
| 10 | Asking NASA again for the same place wastes time. | Remember answers in memory, keyed by the point rounded to 0.01 degree. | `fetch_solar_resource` |
| 11 | Parcel areas come out in m². | Divide by 10,000 to get hectares. | `extract_cluster` |

---

## 7. Equation dependency

Each box needs everything that points into it. Read top to bottom.

```
  parcel area inside circle (ha)      water need of the crop (m3/ha/day)
        |   [Task 1]                        |   [Table 1, via crop category]
        +------------------+----------------+
                           v
        (1)  Q_day = sum( area x water need )                        [m3/day]
                           |
                           |        head H = 40 m,  pumping time t = 6 h,  efficiency = 0.55
                           |                          |    [design numbers]
                           v                          v
        (2)  P = ( 1000 x 9.81 x Q_day x H )  /  ( 3,600,000 x t x efficiency )     [kW]
                           |
                           +-----------------+
                           v                 v
        (3) pick the smallest tier where   motor kW >= P   AND   delivery >= Q_day      [Table 2]
                           |
              +------------+-------------+
              v                          v
         panel kWp                   motor kW
              |                          |
              |   PSH average (NASA)     |   pumping time t = 6 h
              |   derate = 0.75          |
              v                          v
        (4) E_PV = kWp x PSH x 0.75   (5) E_pump = motor kW x t              [kWh/day]
              |                          |
              +------------+-------------+
                           v
        (6)  E_excess = max( 0 , E_PV - E_pump )                              [kWh/day]
```

### The equations in words

1. **Water per day.** Each crop needs a known number of cubic metres per hectare per day. Multiply by the
   hectares of that crop and add everything up.
2. **Pump power.** Lifting water takes energy: weight of water x height. In symbols, density (1000 kg/m³) x
   gravity (9.81) x volume x height gives joules per day. Dividing by 3,600,000 turns joules into kWh.
   Dividing by the pumping hours gives kW (the pump works only 6 hours a day, so it must be strong enough to
   do the day's work in that time). Dividing by the efficiency 0.55 gives the electric power the motor must
   take in, because 45 % is lost.
3. **Choose a pump.** Go down the catalog from the smallest tier. Take the first one whose motor is at least
   P kW **and** whose daily delivery is at least Q_day. If even Tier 4 is too small, use several Tier 4
   pumps, as many as needed.
4. **Solar energy made.** Panel rating x sun hours x 0.75 losses.
5. **Pump energy used.** Motor rating x hours. We assume the motor runs at full rating all 6 hours, which
   slightly overestimates its use (the safe direction).
6. **Surplus.** What the panels make beyond what the pump uses. It cannot be below zero, so the lower limit is 0.

### Worked example (real numbers from the program)

Site `[1b]`: one wheat field, 1.94 ha inside the circle, at (38.0157, -7.8809) in Alentejo.

| Step | Calculation | Result |
|---|---|---|
| Water | 1.94 ha x 35 m³/ha/day (cereals) | **67.8 m³/day** |
| Pump power | 1000 x 9.81 x 67.8 x 40 / (3,600,000 x 6 x 0.55) | **2.24 kW** |
| Pick tier | Tier 1: motor 3.7 kW >= 2.24 and 250 m³/day >= 67.8 | **Tier 1** (3.7 kW, 5 kWp, $7,500) |
| Sunshine | NASA yearly average 17.65 MJ/m²/day / 3.6 | **4.90 PSH** |
| Energy made | 5 kWp x 4.90 x 0.75 | **18.39 kWh/day** |
| Energy used | 3.7 kW x 6 h | **22.20 kWh/day** |
| Surplus | max(0, 18.39 - 22.20) | **0 kWh/day** |

The surplus is zero on a yearly average: the panels make 3.8 kWh/day **less** than the pump uses. But by
month, May to August have a surplus (up to 7.4 kWh/day in July). The program prints both views.

---

## 8. Judgement calls we made (and why)

The assessment allows reasonable assumptions if they are written down. These are ours.

| # | Choice | Reason |
|---|---|---|
| A1 | Map crop codes by their longest known prefix, not by their first two digits. | The first two digits are always `33` in this file (see section 6, item 7). |
| A2 | Water demand uses the parcel area **inside** the circle. | One 20 ha olive grove that only clips the edge would otherwise size a pump for 20 ha. In the real data this made a site look 8x bigger (46.9 ha vs 5.7 ha), which meant 7 big pumps instead of 1 medium one. Both areas are still printed. |
| A3 | Fallow land, shrub, forest and unknown parcels use the "other" water need (30 m³/ha/day). | Table 1 says so for unknown codes. It is the cautious choice: it assumes the land is farmed once water is available. |
| A4 | If one Tier 4 pump is not enough, repeat Tier 4. | The assessment only says "multiple pumps required". This is the simplest rule that gives a number. |
| A5 | The pump's energy use is its full motor rating x pumping hours. | The assessment's formula. It is a worst case, so the surplus is never overstated. |
| A6 | Convert NASA's MJ to kWh. | See section 6, item 8. |
| A7 | "Design month" = the month with the least sunshine (December here). | The assessment gives no irrigation calendar. |
| A8 | Ask NASA about the farmland centre, rounded to 0.01 degree; that rounded point is also the cache key. | See section 5, Task 2. |
| A9 | Treat the assessment's Alentejo example point as an expected `EmptyClusterError`, then jump to the nearest parcel. | See section 9. |

The assumptions are also listed at the top of `dss_pipeline.py`.

---

## 9. Things that surprised us

1. **The assessment's "valid" example location has no fields.** At latitude 38.015, longitude -7.865 the
   nearest parcel is **1.33 km away**. `PT.zip` holds exactly 100,000 parcels, which looks like a sample of
   the full national data, and it is thin in Alentejo. So both of the assessment's test points
   (Alentejo and Lisbon) raise `EmptyClusterError`. The runner then finds the nearest real parcel and runs the
   full calculation there (case `[1b]`).
2. **Crop codes all start with `33`.** See section 6, item 7.
3. **NASA's units differ from what the assessment says.** See section 6, item 8.
4. **Surplus is usually zero on a yearly average.** The catalog pairs about 1.35 kWp of panels with each kW of
   motor. With 0.75 losses and 6 pumping hours, the panels only beat the pump when the sun gives more than
   about 5.9 PSH. Portugal's yearly average is about 4.9 to 5.0, so the answer is zero most of the year and
   positive in summer. This is a finding, not a bug. It also means Task 4 (use the surplus) would find
   little to work with outside summer.

---

## 10. How the files link together

```
 repo
 |-- dss_pipeline.py          ALL the logic: loading, clustering, equations, NASA, pump choice
 |-- run_pipeline.py          Command-line program: loads data, calls evaluate_site, prints results
 |-- requirements.txt         Python packages to install
 |-- pytest.ini               Test settings
 |-- README.md                Short how-to-run page
 |-- SUMMARY.md               This page
 |-- CLAUDE.md                House rules for the AI assistant that helped write this (comment style)
 |-- data/PT.zip              Parcel map (downloaded; not stored in git)
 |-- ignore/                  The assessment PDFs (not stored in git)
 '-- tests/
      |-- conftest.py            Shared test helpers: made-up parcels, saved NASA replies, stand-ins
      |-- test_task1_spatial.py  Task 1: circle, parcels, crop categories, EmptyClusterError
      |-- test_etl.py            Loading and downloading the parcel file
      |-- test_task2_solar.py    NASA reply parsing, units, errors, cache, Task 2 -> Task 3 link
      |-- test_task3_energy.py   Water and pump formulas, tier choice, energy and surplus
      |-- test_runner.py         The command-line program and its exit codes
      |-- test_integration_real_data.py   Checks against the real PT.zip
      '-- fixtures/              Two real NASA replies saved to disk so tests work offline
```

### Which function calls which

```
 run_pipeline.main
  |-- ensure_dataset            download PT.zip (only with --download)
  |-- load_parcels              read PT.zip, convert to metres
  '-- _site  -->  evaluate_site
                   |-- extract_cluster                     [Task 1]
                   |     |-- command_zone                  draw the circle
                   |     |-- (spatial index, repair, cut)
                   |     '-- classify_parcels -- hcat_category
                   |-- daily_water_demand_m3               [Task 2: Q_day]
                   |-- fetch_solar_resource                [Task 2: sunshine]
                   |     '-- parse_power_climatology       check and convert NASA's reply
                   |-- hydraulic_power_kw                  [Task 2: pump kW]
                   '-- model_energy_balance                [Task 3]
                         |-- select_pump                   choose the tier
                         |-- pv_energy_kwh_day             E_PV
                         |-- pump_energy_kwh_day           E_pump
                         '-- surplus_energy_kwh_day        E_excess
```

`evaluate_site` takes an optional `get_solar` argument. Tests pass a fake sunshine source so they run without
internet, and the same hook would let a larger study feed in different sunshine years.

---

## 11. Running it and reading the output

```bash
pip install -r requirements.txt
python run_pipeline.py --download        # first run: fetch data, then run the 3 check locations
python run_pipeline.py --lat 37.97883 --lon -7.73698   # your own point
python -m pytest                         # run all tests
```

A site's output has three blocks, in task order:

```
Task 1 | which parcels, how many hectares in the circle
Task 2 | water per day, NASA sunshine, pump power
Task 3 | tier, E_PV / E_pump / E_excess, and the month-by-month surplus
```

### Errors you can meet

| Error | Means | What to do |
|---|---|---|
| `EmptyClusterError` | No parcel in the 150 m circle (city, sea, other country, or a gap in the data). | Pick a point on farmland. The program suggests the nearest parcel. |
| `DatasetError` | `PT.zip` is missing, damaged, or lacks a column. | Run with `--download`. |
| `SolarDataError` | NASA could not be reached within 10 seconds, or sent unusable data. | Check the internet connection and try again. |
| `ValueError` | An input is out of range (for example latitude 95, or efficiency 1.5). | Fix the number. |

The program exits with code `0` when everything worked and `1` on any failure, so scripts can check it.

---

## 12. What was left out on purpose

- **Task 4** (choose the best use for the surplus and compute payback). Not requested.
- **Retrying NASA requests.** The program tries once, with a 10-second wait, and reports a clear error.
  Adding retries is possible but was judged more than this prototype needs.
- **Switching between "whole parcel" and "inside circle" area.** Only the inside-circle area is used
  (assumption A2). The whole-parcel area is still printed so a reader can compare.
- **Fancy unit checks and validation layers.** The code checks that numbers are in sensible ranges and stops
  with a clear message. It does not try to guess what a wrong input meant.

## 13. Where a bigger study would plug in (Monte Carlo)

"Monte Carlo" means running the same calculation thousands of times with randomly varied inputs to see the
spread of outcomes. `evaluate_site` is built for it: give it a random point, random settings (for example a
different `head_m` from a water-depth guess) and a random sunshine year, and keep the result. Count how often
`EmptyClusterError` appears (that is "not farmland"). A policy test, such as a subsidy on pump cost, simply
changes the cost figure afterwards. Each run is independent, so thousands can run side by side. The
details are in the docstring at the top of `dss_pipeline.py`.
