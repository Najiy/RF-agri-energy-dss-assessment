const fmt = (v, d = 1) => v.toLocaleString("en", { minimumFractionDigits: d, maximumFractionDigits: d });

export default function AssessmentCard({ doc }) {
  const a = doc.assessment;
  const inZone = doc.parcels.features.filter((f) => f.properties.inZoneHa > 0).length;

  if (!a) {
    return (
      <section className="panel" aria-label="Pump and energy result">
        <h2>Pump &amp; energy</h2>
        <p className="notice">
          {inZone === 0
            ? `No parcel inside the ${doc.radiusM} m zone: the pipeline raises EmptyClusterError here.`
            : "Sunshine data could not be fetched, so there is no energy result for this site."}
        </p>
      </section>
    );
  }

  const { pump, energy, monthly } = a;
  const surplusMonths = monthly.months.filter((_, i) => monthly.excess[i] > 0);
  const top = Math.max(energy.pv, energy.pump);

  return (
    <section className="panel" aria-label="Pump and energy result">
      <h2>Pump &amp; energy</h2>

      <dl className="metrics">
        <Metric label="Water need" value={fmt(a.qDayM3)} unit="m³/day" />
        <Metric label="Hydraulic power" value={fmt(a.hydraulicKw, 2)} unit="kW" />
        <Metric
          label="Pump"
          value={`Tier ${pump.tier}${pump.units > 1 ? ` × ${pump.units}` : ""}`}
          unit={`${fmt(pump.motorKw, 1)} kW motor`}
        />
        <Metric label="Solar array" value={fmt(pump.pvKwp, 1)} unit="kWp" />
        <Metric label="Sunshine (avg)" value={fmt(a.solar.pshAvg, 2)} unit="kWh/m²/day" />
        <Metric label="CAPEX" value={`$${fmt(pump.capexUsd, 0)}`} unit="" />
      </dl>

      <div className="balance">
        <Bar label="PV makes" value={energy.pv} max={top} tone="sun" />
        <Bar label="Pump uses" value={energy.pump} max={top} tone="load" />
        <p className="balance-note">
          Surplus <strong>{fmt(energy.excess, 2)} kWh/day</strong> on the yearly average
          {energy.excess === 0 ? ` (short by ${fmt(energy.pump - energy.pv, 2)})` : ""}
        </p>
      </div>

      <MonthlyChart monthly={monthly} />
      <p className="hint">
        {surplusMonths.length > 0 ? `Surplus months: ${surplusMonths.join(", ")}` : "No month has a surplus."}
      </p>
    </section>
  );
}

function Metric({ label, value, unit }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>
        {value} <small>{unit}</small>
      </dd>
    </div>
  );
}

function Bar({ label, value, max, tone }) {
  return (
    <div className="bar-row">
      <span>{label}</span>
      <div className="bar-track">
        <div className={`bar-fill ${tone}`} style={{ width: `${max > 0 ? (value / max) * 100 : 0}%` }} />
      </div>
      <b>{fmt(value)} kWh</b>
    </div>
  );
}

// Monthly sunshine as bars; months where the array out-produces the pump are highlighted.
function MonthlyChart({ monthly }) {
  const W = 260;
  const H = 86;
  const max = Math.max(...monthly.psh, 1);
  const step = W / 12;
  const summary = monthly.months.map((m, i) => `${m} ${monthly.psh[i].toFixed(1)}`).join(", ");

  return (
    <svg viewBox={`0 0 ${W} ${H + 16}`} className="chart" role="img" aria-label={`Peak sun hours by month: ${summary}`}>
      {monthly.psh.map((v, i) => {
        const h = (v / max) * H;
        const surplus = monthly.excess[i] > 0;

        return (
          <g key={monthly.months[i]}>
            <rect x={i * step + 3} y={H - h} width={step - 6} height={h} rx="2" className={surplus ? "m-surplus" : "m-normal"}>
              <title>{`${monthly.months[i]}: ${v.toFixed(2)} kWh/m²/day, surplus ${monthly.excess[i].toFixed(1)} kWh/day`}</title>
            </rect>
            <text x={i * step + step / 2} y={H + 12} textAnchor="middle">
              {monthly.months[i][0]}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
