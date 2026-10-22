import { CATEGORY_COLORS } from "../render/ParcelLayer.js";

// Details of one parcel, used by the hover tooltip and the selected-parcel card.
export function ParcelFacts({ feature, doc }) {
  const p = feature.properties;
  const cat = doc.categories[p.cat];
  const zone = p.inZoneHa > 0 ? `${p.inZoneHa.toFixed(2)} ha → ${(p.inZoneHa * p.wd).toFixed(1)} m³/day` : "outside the zone";

  return (
    <dl className="facts">
      <div>
        <dt>Crop</dt>
        <dd>
          <i style={{ background: CATEGORY_COLORS[p.cat] ?? CATEGORY_COLORS.other }} aria-hidden="true" />
          {p.label ?? "Unlabelled"}
        </dd>
      </div>
      <div>
        <dt>Category</dt>
        <dd>
          {cat?.name} · {p.wd} m³/ha/day
        </dd>
      </div>
      <div>
        <dt>HCAT code</dt>
        <dd>{p.hcat ?? "–"}</dd>
      </div>
      <div>
        <dt>Parcel</dt>
        <dd>
          {p.ha.toFixed(2)} ha · id {p.id ?? "–"}
        </dd>
      </div>
      <div>
        <dt>Inside zone</dt>
        <dd>{zone}</dd>
      </div>
    </dl>
  );
}

export function Tooltip({ feature, doc, point }) {
  if (!feature || !point) return null;

  const x = Math.min(point.x + 16, window.innerWidth - 300);
  const y = Math.min(point.y + 16, window.innerHeight - 190);

  return (
    <div className="tooltip" style={{ transform: `translate(${x}px, ${y}px)` }} role="status">
      <ParcelFacts feature={feature} doc={doc} />
    </div>
  );
}

export function SelectedCard({ feature, doc, onClose }) {
  if (!feature) return null;

  return (
    <section className="panel" aria-label="Selected parcel">
      <div className="panel-head">
        <h2>Selected parcel</h2>
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Clear selection">
          ×
        </button>
      </div>
      <ParcelFacts feature={feature} doc={doc} />
    </section>
  );
}
