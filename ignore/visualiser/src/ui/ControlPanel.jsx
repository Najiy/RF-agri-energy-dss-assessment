import { HEIGHT_MODES } from "../render/ParcelLayer.js";

const TOGGLES = [
  ["showTiles", "OpenStreetMap tiles"],
  ["showZone", "Command zone"],
  ["showFlow", "Water flow"],
  ["showEdges", "Parcel outlines"],
  ["autoOrbit", "Auto-orbit"],
];

export default function ControlPanel({ options, onChange, onReset }) {
  const set = (key) => (e) => onChange({ [key]: e.target.type === "checkbox" ? e.target.checked : +e.target.value });

  return (
    <section className="panel" aria-label="View controls">
      <h2>View</h2>

      <fieldset>
        <legend>Parcel height shows</legend>
        <div className="segmented">
          {Object.entries(HEIGHT_MODES).map(([mode, { label }]) => (
            <label key={mode} className={options.heightMode === mode ? "on" : ""}>
              <input
                type="radio"
                name="height-mode"
                checked={options.heightMode === mode}
                onChange={() => onChange({ heightMode: mode })}
              />
              {label}
            </label>
          ))}
        </div>
      </fieldset>

      <Slider
        label="Height exaggeration"
        value={options.exaggeration}
        min={0.3}
        max={2.5}
        step={0.1}
        fmt={(v) => `${v.toFixed(1)}×`}
        onChange={set("exaggeration")}
      />
      <Slider
        label="Parcel opacity"
        value={options.opacity}
        min={0.3}
        max={1}
        step={0.05}
        fmt={(v) => `${Math.round(v * 100)}%`}
        onChange={set("opacity")}
      />
      <Slider
        label="Map brightness"
        value={options.mapDim}
        min={0.2}
        max={1.2}
        step={0.05}
        fmt={(v) => `${Math.round(v * 100)}%`}
        onChange={set("mapDim")}
      />

      <div className="toggles">
        {TOGGLES.map(([key, label]) => (
          <label key={key} className="toggle">
            <input type="checkbox" checked={options[key]} onChange={set(key)} />
            <span>{label}</span>
          </label>
        ))}
      </div>

      <button type="button" className="btn" onClick={onReset}>
        Reset view
      </button>
      <p className="hint">Drag to orbit · right-drag or Shift-drag to pan · scroll to zoom · click a parcel</p>
    </section>
  );
}

function Slider({ label, value, min, max, step, fmt, onChange }) {
  return (
    <label className="slider">
      <span>
        {label} <output>{fmt(value)}</output>
      </span>
      <input type="range" min={min} max={max} step={step} value={value} onChange={onChange} />
    </label>
  );
}
