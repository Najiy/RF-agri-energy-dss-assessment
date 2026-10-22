import { CATEGORY_COLORS } from "../render/ParcelLayer.js";

// One row per crop category: colour, name, water need, parcel count and hectares inside the zone.
export default function Legend({ doc }) {
  const rows = Object.entries(doc.categories).map(([key, cat]) => {
    const parcels = doc.parcels.features.filter((f) => f.properties.cat === key);

    return {
      key,
      cat,
      count: parcels.length,
      zoneHa: parcels.reduce((sum, f) => sum + f.properties.inZoneHa, 0),
    };
  });

  return (
    <section className="panel legend" aria-label="Crop categories">
      <h2>Crop categories</h2>
      <table>
        <thead>
          <tr>
            <th scope="col">Category</th>
            <th scope="col" title="Daily water need per hectare">
              m³/ha/day
            </th>
            <th scope="col" title="Parcels in the 1.5 km window">
              Parcels
            </th>
            <th scope="col" title="Hectares inside the 150 m zone">
              Zone ha
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ key, cat, count, zoneHa }) => (
            <tr key={key}>
              <th scope="row">
                <i style={{ background: CATEGORY_COLORS[key] ?? CATEGORY_COLORS.other }} aria-hidden="true" />
                {cat.name}
              </th>
              <td>{cat.wd}</td>
              <td>{count}</td>
              <td>{zoneHa > 0 ? zoneHa.toFixed(2) : "–"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
