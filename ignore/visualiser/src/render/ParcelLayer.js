// Farm parcels as extruded prisms: one merged mesh, one draw call. A custom shader lifts every prism
// to its height on the GPU, so changing the height mode only changes a few uniforms (no rebuild).

import * as THREE from "three";

export const CATEGORY_COLORS = {
  10: "#e5b94a", // cereals
  20: "#e8744f", // orchards
  30: "#74c27a", // pastures
  50: "#35bfc7", // vegetables
  other: "#a99bd6", // fallback
};

const MIN_HEIGHT = 1.2; // metres; keeps flat parcels from z-fighting with the map
const BASE_HEIGHT_M = 38; // metres at exaggeration 1 for the tallest parcel

export const HEIGHT_MODES = {
  demand: { weights: [1, 0, 0], label: "Crop water need" },
  volume: { weights: [0, 1, 0], label: "Water used in zone" },
  flat: { weights: [0, 0, 0.12], label: "Flat" },
};

// Shared by every parcel shader: where a vertex sits vertically.
// position.y is only a flag (0 = on the ground, 1 = on top); the real height comes from the metrics.
export const HEIGHT_GLSL = /* glsl */ `
  uniform vec3 uWeights;
  uniform float uMaxHeight;
  attribute vec3 aMetrics;
  float parcelHeight(vec3 m) { return ${MIN_HEIGHT.toFixed(1)} + dot(m, uWeights) * uMaxHeight; }
`;

const FACE_VERT = /* glsl */ `
  ${HEIGHT_GLSL}
  attribute vec3 aColor;
  attribute float aZone;
  attribute float aFeature;
  varying vec3 vColor;
  varying vec3 vNormal;
  varying float vTop;
  varying float vZone;
  varying float vFeature;
  #include <fog_pars_vertex>

  void main() {
    vec3 p = vec3(position.x, position.y * parcelHeight(aMetrics), position.z);
    vec4 mvPosition = modelViewMatrix * vec4(p, 1.0);
    gl_Position = projectionMatrix * mvPosition;

    vColor = aColor;
    vNormal = normal;
    vTop = position.y;
    vZone = aZone;
    vFeature = aFeature;
    #include <fog_vertex>
  }
`;

const FACE_FRAG = /* glsl */ `
  uniform float uOpacity;
  uniform float uHover;
  uniform float uSelected;
  uniform vec3 uLight;
  varying vec3 vColor;
  varying vec3 vNormal;
  varying float vTop;
  varying float vZone;
  varying float vFeature;
  #include <fog_pars_fragment>

  void main() {
    float inZone = step(0.0001, vZone);

    // Parcels outside the command zone are washed out so the 150 m cluster stands out.
    vec3 grey = vec3(dot(vColor, vec3(0.33))) * 0.6;
    vec3 base = mix(mix(grey, vColor, 0.35), vColor * 1.08, inZone);

    float light = 0.5 + 0.6 * max(dot(normalize(vNormal), normalize(uLight)), 0.0);
    light *= mix(0.7, 1.0, vTop); // walls get darker toward the ground

    float hover = 1.0 - step(0.5, abs(vFeature - uHover));
    float selected = 1.0 - step(0.5, abs(vFeature - uSelected));
    vec3 col = mix(base * light, vec3(1.0), hover * 0.28 + selected * 0.4);

    gl_FragColor = vec4(col, uOpacity * mix(0.62, 1.0, inZone));
    #include <colorspace_fragment>
    #include <fog_fragment>
  }
`;

// Picking pass: each prism paints its index as a colour, then we read the one pixel under the cursor.
const PICK_VERT = /* glsl */ `
  ${HEIGHT_GLSL}
  attribute float aFeature;
  varying float vFeature;

  void main() {
    vec3 p = vec3(position.x, position.y * parcelHeight(aMetrics), position.z);
    gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
    vFeature = aFeature;
  }
`;

const PICK_FRAG = /* glsl */ `
  varying float vFeature;

  void main() {
    float id = floor(vFeature + 1.5); // 0 is reserved for "nothing"
    gl_FragColor = vec4(mod(id, 256.0), mod(floor(id / 256.0), 256.0), floor(id / 65536.0), 255.0) / 255.0;
  }
`;

const EDGE_VERT = /* glsl */ `
  ${HEIGHT_GLSL}
  attribute float aZone;
  attribute float aFeature;
  varying float vZone;
  varying float vFeature;
  #include <fog_pars_vertex>

  void main() {
    vec3 p = vec3(position.x, position.y * parcelHeight(aMetrics), position.z);
    vec4 mvPosition = modelViewMatrix * vec4(p, 1.0);
    gl_Position = projectionMatrix * mvPosition;
    vZone = aZone;
    vFeature = aFeature;
    #include <fog_vertex>
  }
`;

const EDGE_FRAG = /* glsl */ `
  uniform float uHover;
  uniform float uSelected;
  varying float vZone;
  varying float vFeature;
  #include <fog_pars_fragment>

  void main() {
    float inZone = step(0.0001, vZone);
    float lit = max(1.0 - step(0.5, abs(vFeature - uHover)), 1.0 - step(0.5, abs(vFeature - uSelected)));
    float alpha = mix(0.28, 0.9, inZone);
    gl_FragColor = vec4(mix(vec3(0.85, 0.9, 0.95), vec3(1.0), lit), max(alpha, lit));
    #include <colorspace_fragment>
    #include <fog_fragment>
  }
`;

function ringPoints(ring, frame) {
  const pts = ring.map(([lon, lat]) => frame.toLocal(lon, lat));
  const [first, last] = [pts[0], pts[pts.length - 1]];
  if (first[0] === last[0] && first[1] === last[1]) pts.pop(); // GeoJSON repeats the first point
  return pts;
}

// Shoelace formula. The sign tells the winding direction, which decides which way walls face.
function signedArea(pts) {
  let sum = 0;
  for (let i = 0; i < pts.length; i++) {
    const [x1, z1] = pts[i];
    const [x2, z2] = pts[(i + 1) % pts.length];
    sum += x1 * z2 - x2 * z1;
  }
  return sum / 2;
}

function oriented(pts, positive) {
  return signedArea(pts) > 0 === positive ? pts : [...pts].reverse();
}

export class ParcelLayer {
  constructor(frame, collection, { radiusM }) {
    this.features = collection.features;
    this.group = new THREE.Group();

    const metrics = this._normalisedMetrics();
    const built = this._build(frame, metrics, radiusM);

    this.anchors = built.anchors;

    // Mode weights and max height ease toward their targets in tick(), so mode changes animate.
    this.uniforms = {
      uWeights: { value: new THREE.Vector3(...HEIGHT_MODES.demand.weights) },
      uMaxHeight: { value: BASE_HEIGHT_M },
    };
    this.weightsTarget = new THREE.Vector3(...HEIGHT_MODES.demand.weights);
    this.maxHeightTarget = BASE_HEIGHT_M;

    const faceUniforms = THREE.UniformsUtils.merge([
      THREE.UniformsLib.fog,
      {
        uOpacity: { value: 0.85 },
        uHover: { value: -1 },
        uSelected: { value: -1 },
        uLight: { value: new THREE.Vector3(-0.4, 0.8, 0.45) },
      },
    ]);
    Object.assign(faceUniforms, this.uniforms);
    this.faceUniforms = faceUniforms;

    this.material = new THREE.ShaderMaterial({
      uniforms: faceUniforms,
      vertexShader: FACE_VERT,
      fragmentShader: FACE_FRAG,
      fog: true,
      transparent: true,
      side: THREE.DoubleSide,
    });
    this.mesh = new THREE.Mesh(built.faces, this.material);
    this.mesh.frustumCulled = false; // the shader moves vertices, so the CPU bounding box is wrong

    // merge() copies values, so hover/selected are re-linked afterwards to share one uniform with the faces.
    const edgeUniforms = THREE.UniformsUtils.merge([THREE.UniformsLib.fog]);
    Object.assign(edgeUniforms, this.uniforms, { uHover: faceUniforms.uHover, uSelected: faceUniforms.uSelected });
    this.edges = new THREE.LineSegments(
      built.edges,
      new THREE.ShaderMaterial({
        uniforms: edgeUniforms,
        vertexShader: EDGE_VERT,
        fragmentShader: EDGE_FRAG,
        fog: true,
        transparent: true,
        depthWrite: false,
      })
    );
    this.edges.frustumCulled = false;

    // The same geometry again, painted with ids; it lives in its own scene and never reaches the screen.
    this.pickMesh = new THREE.Mesh(
      built.faces,
      new THREE.ShaderMaterial({
        uniforms: this.uniforms,
        vertexShader: PICK_VERT,
        fragmentShader: PICK_FRAG,
        side: THREE.DoubleSide,
      })
    );
    this.pickMesh.frustumCulled = false;

    this.group.add(this.mesh, this.edges);
    this.exaggeration = 1;
    this.mode = "demand";
  }

  setMode(mode) {
    this.mode = mode;
    this._applyTargets();
  }

  setExaggeration(value) {
    this.exaggeration = value;
    this._applyTargets();
  }

  setOpacity(value) {
    this.faceUniforms.uOpacity.value = value;
  }

  setEdgesVisible(on) {
    this.edges.visible = on;
  }

  setHover(index) {
    this.faceUniforms.uHover.value = index;
  }

  setSelected(index) {
    this.faceUniforms.uSelected.value = index;
  }

  /** Ease toward the target height; call once per frame. */
  tick(dt) {
    const k = 1 - Math.exp(-dt * 7);
    this.uniforms.uWeights.value.lerp(this.weightsTarget, k);
    this.uniforms.uMaxHeight.value += (this.maxHeightTarget - this.uniforms.uMaxHeight.value) * k;
  }

  /** Mirror of the shader's height formula, for placing things on top of a parcel. */
  heightOf(metrics) {
    const w = this.uniforms.uWeights.value;
    return MIN_HEIGHT + (metrics[0] * w.x + metrics[1] * w.y + metrics[2] * w.z) * this.uniforms.uMaxHeight.value;
  }

  dispose() {
    this.mesh.geometry.dispose();
    this.edges.geometry.dispose();
    this.material.dispose();
    this.edges.material.dispose();
    this.pickMesh.material.dispose();
  }

  _applyTargets() {
    this.weightsTarget.set(...HEIGHT_MODES[this.mode].weights);
    this.maxHeightTarget = BASE_HEIGHT_M * this.exaggeration;
  }

  // Heights come from three numbers per parcel, each scaled to 0..1 across the site:
  // crop water need, water used inside the zone, and a constant 1 for the flat mode.
  _normalisedMetrics() {
    const demand = this.features.map((f) => f.properties.wd);
    const volume = this.features.map((f) => f.properties.wd * f.properties.inZoneHa);
    const maxDemand = Math.max(...demand, 1);
    const maxVolume = Math.max(...volume, 1);

    return this.features.map((_, i) => [demand[i] / maxDemand, volume[i] / maxVolume, 1]);
  }

  _build(frame, metrics, radiusM) {
    const pos = [];
    const nor = [];
    const col = [];
    const met = [];
    const zone = [];
    const feat = [];
    const idx = [];

    const edgePos = [];
    const edgeMet = [];
    const edgeZone = [];
    const edgeFeat = [];

    const anchors = [];
    const color = new THREE.Color();

    const vertex = (x, flag, z, normal, c, m, zoneShare, f) => {
      pos.push(x, flag, z);
      nor.push(...normal);
      col.push(c.r, c.g, c.b);
      met.push(...m);
      zone.push(zoneShare);
      feat.push(f);
      return pos.length / 3 - 1;
    };

    this.features.forEach((feature, f) => {
      const { cat, inZoneHa, ha } = feature.properties;
      color.set(CATEGORY_COLORS[cat] ?? CATEGORY_COLORS.other);

      const share = ha > 0 ? inZoneHa / ha : 0;
      const m = metrics[f];
      const geometry = feature.geometry;
      const polygons = geometry.type === "Polygon" ? [geometry.coordinates] : geometry.coordinates;

      let largest = null;

      for (const rings of polygons) {
        const outer = oriented(ringPoints(rings[0], frame), true);
        if (outer.length < 3) continue;

        const holes = rings.slice(1).map((r) => oriented(ringPoints(r, frame), false)).filter((h) => h.length >= 3);
        const all = [outer, ...holes];

        // Top cap: earcut triangulation (handles holes), every vertex flagged "top".
        const flat = all.flat();
        const first = pos.length / 3;
        for (const [x, z] of flat) vertex(x, 1, z, [0, 1, 0], color, m, share, f);

        const triangles = THREE.ShapeUtils.triangulateShape(
          outer.map(([x, z]) => new THREE.Vector2(x, z)),
          holes.map((h) => h.map(([x, z]) => new THREE.Vector2(x, z)))
        );
        for (const [a, b, c] of triangles) idx.push(first + a, first + b, first + c);

        // Walls: one quad per ring edge, from the ground (flag 0) up to the top (flag 1).
        for (const ring of all) {
          for (let i = 0; i < ring.length; i++) {
            const [x1, z1] = ring[i];
            const [x2, z2] = ring[(i + 1) % ring.length];
            const dx = x2 - x1;
            const dz = z2 - z1;
            const len = Math.hypot(dx, dz) || 1;
            const n = [dz / len, 0, -dx / len]; // outward for positive-area rings, into the hole for holes

            const a = vertex(x1, 0, z1, n, color, m, share, f);
            const b = vertex(x2, 0, z2, n, color, m, share, f);
            const c = vertex(x2, 1, z2, n, color, m, share, f);
            const d = vertex(x1, 1, z1, n, color, m, share, f);
            idx.push(a, b, c, a, c, d);

            for (const flag of [0, 1]) {
              edgePos.push(x1, flag, z1, x2, flag, z2);
              edgeMet.push(...m, ...m);
              edgeZone.push(share, share);
              edgeFeat.push(f, f);
            }
          }
        }

        if (!largest || outer.length > largest.length) largest = outer;
      }

      if (share > 0 && largest) anchors.push(this._anchor(largest, radiusM, m, share));
    });

    const faces = new THREE.BufferGeometry();
    faces.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    faces.setAttribute("normal", new THREE.Float32BufferAttribute(nor, 3));
    faces.setAttribute("aColor", new THREE.Float32BufferAttribute(col, 3));
    faces.setAttribute("aMetrics", new THREE.Float32BufferAttribute(met, 3));
    faces.setAttribute("aZone", new THREE.Float32BufferAttribute(zone, 1));
    faces.setAttribute("aFeature", new THREE.Float32BufferAttribute(feat, 1));
    faces.setIndex(new THREE.BufferAttribute(new Uint32Array(idx), 1));

    const edges = new THREE.BufferGeometry();
    edges.setAttribute("position", new THREE.Float32BufferAttribute(edgePos, 3));
    edges.setAttribute("aMetrics", new THREE.Float32BufferAttribute(edgeMet, 3));
    edges.setAttribute("aZone", new THREE.Float32BufferAttribute(edgeZone, 1));
    edges.setAttribute("aFeature", new THREE.Float32BufferAttribute(edgeFeat, 1));

    return { faces, edges, anchors };
  }

  // Where water arrives on a parcel: the average of its outline points that fall inside the circle.
  // If the circle only clips an edge and no point is inside, use the nearest point on the circle itself.
  _anchor(outline, radiusM, metrics, share) {
    const inside = outline.filter(([x, z]) => Math.hypot(x, z) < radiusM * 0.97);
    let x;
    let z;

    if (inside.length > 0) {
      x = inside.reduce((s, p) => s + p[0], 0) / inside.length;
      z = inside.reduce((s, p) => s + p[1], 0) / inside.length;
    } else {
      const cx = outline.reduce((s, p) => s + p[0], 0) / outline.length;
      const cz = outline.reduce((s, p) => s + p[1], 0) / outline.length;
      const d = Math.hypot(cx, cz) || 1;
      x = (cx / d) * radiusM * 0.9;
      z = (cz / d) * radiusM * 0.9;
    }

    return { x, z, metrics, share };
  }
}
