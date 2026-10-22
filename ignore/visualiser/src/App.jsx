import { useCallback, useEffect, useRef, useState } from "react";
import { DEFAULT_OPTIONS } from "./render/SceneRenderer.js";
import AssessmentCard from "./ui/AssessmentCard.jsx";
import ControlPanel from "./ui/ControlPanel.jsx";
import Legend from "./ui/Legend.jsx";
import { SelectedCard, Tooltip } from "./ui/ParcelInfo.jsx";
import Viewport from "./ui/Viewport.jsx";

const base = import.meta.env.BASE_URL;

export default function App() {
  const [sites, setSites] = useState([]);
  const [name, setName] = useState(() => location.hash.slice(1));
  const [doc, setDoc] = useState(null);
  const [error, setError] = useState(null);
  const [options, setOptions] = useState(DEFAULT_OPTIONS);
  const [hover, setHover] = useState({ index: -1, point: null });
  const [selected, setSelected] = useState(-1);
  const [tiles, setTiles] = useState({ loading: 0, failed: 0, zoom: 0 });
  const viewport = useRef(null);

  // The site list comes from index.json, written by export_data.py.
  useEffect(() => {
    fetch(`${base}data/index.json`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`index.json: HTTP ${r.status}`))))
      .then((list) => {
        setSites(list);
        setName((current) => (list.some((s) => s.name === current) ? current : list[0]?.name));
      })
      .catch((e) => setError(`${e.message}. Run export_data.py first.`));
  }, []);

  useEffect(() => {
    const site = sites.find((s) => s.name === name);
    if (!site) return;

    let stale = false; // ignore a slow response if the user already picked another site
    setSelected(-1);
    setHover({ index: -1, point: null });
    location.hash = name;

    fetch(`${base}${site.file}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${site.file}: HTTP ${r.status}`))))
      .then((d) => {
        if (stale) return;
        setDoc(d);
        setError(null);
      })
      .catch((e) => !stale && setError(e.message));

    return () => {
      stale = true;
    };
  }, [name, sites]);

  const patchOptions = useCallback((patch) => setOptions((o) => ({ ...o, ...patch })), []);
  const onHover = useCallback(
    (index, point) => setHover((h) => (h.index === index && h.point?.x === point?.x && h.point?.y === point?.y ? h : { index, point })),
    []
  );

  const features = doc?.parcels.features ?? [];

  return (
    <div className="app">
      <Viewport
        ref={viewport}
        doc={doc}
        options={options}
        selectedIndex={selected}
        onHover={onHover}
        onSelect={setSelected}
        onTiles={setTiles}
      />

      <header className="topbar">
        <div className="brand">
          <span className="logo" aria-hidden="true" />
          <div>
            <h1>Field Viewer</h1>
            <p>EuroCrops Portugal · tube-well command zone</p>
          </div>
        </div>
        <label className="site-select">
          <span>Site</span>
          <select value={name ?? ""} onChange={(e) => setName(e.target.value)}>
            {sites.map((s) => (
              <option key={s.name} value={s.name}>
                {s.label}
              </option>
            ))}
          </select>
        </label>
      </header>

      {doc && (
        <div className="dock">
          <aside className="side left">
            <ControlPanel options={options} onChange={patchOptions} onReset={() => viewport.current?.resetView()} />
            <Legend doc={doc} />
          </aside>
          <aside className="side right">
            <AssessmentCard doc={doc} />
            <SelectedCard feature={features[selected]} doc={doc} onClose={() => setSelected(-1)} />
          </aside>
        </div>
      )}

      {doc && <Tooltip feature={features[hover.index]} doc={doc} point={hover.point} />}

      {error && (
        <div className="toast" role="alert">
          {error}
        </div>
      )}
      {!doc && !error && (
        <div className="toast" role="status">
          Loading site…
        </div>
      )}

      <footer className="attribution">
        {tiles.failed > 0 && tiles.loading === 0 && <span className="warn">Map tiles unreachable (offline?)</span>}
        {tiles.loading > 0 && <span>Loading {tiles.loading} tiles…</span>}
        <span>
          ©{" "}
          <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">
            OpenStreetMap
          </a>{" "}
          contributors · parcels: EuroCrops
        </span>
      </footer>
    </div>
  );
}
