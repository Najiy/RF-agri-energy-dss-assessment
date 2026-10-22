// The 150 m command zone: a glowing cylinder wall, ground ring, the tube-well beacon with expanding
// ripples, and GPU-animated water droplets flowing from the well to every parcel inside the circle.

import * as THREE from "three";
import { HEIGHT_GLSL } from "./ParcelLayer.js";

const WALL_HEIGHT = 70;
const CYAN = new THREE.Color("#4fd8f0");

const WALL_VERT = /* glsl */ `
  varying vec2 vUv;
  #include <fog_pars_vertex>
  void main() {
    vUv = uv;
    vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
    gl_Position = projectionMatrix * mvPosition;
    #include <fog_vertex>
  }
`;

// Fades toward the top; a bright band sweeps upward so the zone reads as "active".
const WALL_FRAG = /* glsl */ `
  uniform float uTime;
  uniform vec3 uColor;
  varying vec2 vUv;
  #include <fog_pars_fragment>
  void main() {
    float fade = pow(1.0 - vUv.y, 1.6) * 0.3;
    float band = exp(-pow((vUv.y - fract(uTime * 0.18)) * 12.0, 2.0)) * 0.45;
    gl_FragColor = vec4(uColor, fade + band * (1.0 - vUv.y));
    #include <colorspace_fragment>
    #include <fog_fragment>
  }
`;

// Each droplet rides a quadratic curve from the well top to its parcel; all maths runs in the vertex shader.
const FLOW_VERT = /* glsl */ `
  ${HEIGHT_GLSL}
  uniform float uTime;
  uniform float uWellTop;
  uniform float uViewScale; // pixels per metre at distance 1
  attribute vec3 aSeed; // x: phase, y: speed, z: sideways wobble
  varying float vAlpha;
  varying float vMix;

  void main() {
    float t = fract(aSeed.x + uTime * aSeed.y);
    vec3 start = vec3(0.0, uWellTop, 0.0);
    vec3 end = vec3(position.x, parcelHeight(aMetrics) + 1.0, position.z);
    float span = length(end.xz);
    vec3 ctrl = (start + end) * 0.5 + vec3(0.0, 30.0 + span * 0.25, 0.0);

    vec3 p = mix(mix(start, ctrl, t), mix(ctrl, end, t), t);
    vec2 side = normalize(vec2(-end.z, end.x) + 0.0001);
    p.xz += side * aSeed.z * sin(t * 3.14159) * 4.0;

    vec4 mvPosition = modelViewMatrix * vec4(p, 1.0);
    gl_Position = projectionMatrix * mvPosition;
    gl_PointSize = clamp(5.0 * uViewScale / -mvPosition.z, 3.0, 40.0); // droplets are about 5 m wide
    vAlpha = smoothstep(0.0, 0.08, t) * (1.0 - smoothstep(0.88, 1.0, t));
    vMix = t;
  }
`;

const FLOW_FRAG = /* glsl */ `
  varying float vAlpha;
  varying float vMix;
  void main() {
    float d = length(gl_PointCoord - 0.5);
    float soft = smoothstep(0.5, 0.05, d);
    vec3 col = mix(vec3(0.75, 0.97, 1.0), vec3(0.2, 0.6, 1.0), vMix);
    gl_FragColor = vec4(col, soft * vAlpha);
  }
`;

function fogged(extra) {
  return THREE.UniformsUtils.merge([THREE.UniformsLib.fog, extra]);
}

export class ZoneLayer {
  constructor(radiusM, parcelLayer) {
    this.radius = radiusM;
    this.parcels = parcelLayer;
    this.reducedMotion = false;
    this.group = new THREE.Group();
    this.zoneGroup = new THREE.Group();
    this.flowGroup = new THREE.Group();

    this.wallUniforms = fogged({ uTime: { value: 0 }, uColor: { value: CYAN.clone() } });
    const wall = new THREE.Mesh(
      new THREE.CylinderGeometry(radiusM, radiusM, WALL_HEIGHT, 160, 1, true).translate(0, WALL_HEIGHT / 2, 0),
      new THREE.ShaderMaterial({
        uniforms: this.wallUniforms,
        vertexShader: WALL_VERT,
        fragmentShader: WALL_FRAG,
        fog: true,
        transparent: true,
        depthWrite: false,
        side: THREE.DoubleSide,
      })
    );

    const disc = new THREE.Mesh(
      new THREE.CircleGeometry(radiusM, 96).rotateX(-Math.PI / 2),
      new THREE.MeshBasicMaterial({ color: CYAN, transparent: true, opacity: 0.1, depthWrite: false })
    );
    disc.position.y = 0.4;

    // depthTest off: the ring stays visible on top of the parcels it crosses.
    const ring = new THREE.Mesh(
      new THREE.RingGeometry(radiusM - 0.9, radiusM + 0.9, 192).rotateX(-Math.PI / 2),
      new THREE.MeshBasicMaterial({ color: CYAN, transparent: true, opacity: 0.95, depthTest: false })
    );
    ring.position.y = 0.8;
    ring.renderOrder = 5;

    this.zoneGroup.add(wall, disc, ring);

    // Tube-well beacon: pole, glowing head, base plate. Its height follows the tallest parcel.
    this.pole = new THREE.Mesh(
      new THREE.CylinderGeometry(0.9, 0.9, 1, 12).translate(0, 0.5, 0),
      new THREE.MeshBasicMaterial({ color: "#d9f6ff" })
    );
    this.head = new THREE.Mesh(new THREE.SphereGeometry(4, 24, 16), new THREE.MeshBasicMaterial({ color: "#7be3ff" }));
    const base = new THREE.Mesh(
      new THREE.CircleGeometry(7, 40).rotateX(-Math.PI / 2),
      new THREE.MeshBasicMaterial({ color: "#d9f6ff", transparent: true, opacity: 0.9, depthTest: false })
    );
    base.position.y = 1.4;
    base.renderOrder = 6;
    this.zoneGroup.add(this.pole, this.head, base);

    this.ripples = [0, 1, 2].map(() => {
      const ripple = new THREE.Mesh(
        new THREE.RingGeometry(0.97, 1, 96).rotateX(-Math.PI / 2),
        new THREE.MeshBasicMaterial({ color: CYAN, transparent: true, depthTest: false, depthWrite: false })
      );
      ripple.position.y = 0.9;
      ripple.renderOrder = 4;
      this.zoneGroup.add(ripple);
      return ripple;
    });

    this.flowUniforms = {
      uTime: { value: 0 },
      uWellTop: { value: 60 },
      uViewScale: { value: 800 },
      uWeights: parcelLayer.uniforms.uWeights,
      uMaxHeight: parcelLayer.uniforms.uMaxHeight,
    };
    this.flow = this._buildFlow(parcelLayer.anchors);
    this.flowGroup.add(this.flow);

    this.group.add(this.zoneGroup, this.flowGroup);
    this.tick(0, 0);
  }

  setZoneVisible(on) {
    this.zoneGroup.visible = on;
  }

  setFlowVisible(on) {
    this.flowGroup.visible = on;
  }

  /** Keeps droplets a constant size in the world: pixels per metre at distance 1. */
  setViewScale(value) {
    this.flowUniforms.uViewScale.value = value;
  }

  setReducedMotion(on) {
    this.reducedMotion = on;
  }

  tick(dt, time) {
    const t = this.reducedMotion ? 0 : time;
    this.wallUniforms.uTime.value = t;
    this.flowUniforms.uTime.value = t;

    // Beacon rises with the tallest parcel so it never disappears inside one.
    const top = Math.max(50, this.parcels.uniforms.uMaxHeight.value * 1.3 + 25);
    this.pole.scale.y = top;
    this.head.position.y = top;
    this.flowUniforms.uWellTop.value = top;

    this.ripples.forEach((ripple, i) => {
      const phase = this.reducedMotion ? 0.5 + i * 0.15 : (time / 3.2 + i / this.ripples.length) % 1;
      ripple.scale.setScalar(Math.max(0.5, phase * this.radius));
      ripple.material.opacity = (1 - phase) * 0.55;
    });
  }

  dispose() {
    this.group.traverse((o) => {
      o.geometry?.dispose();
      o.material?.dispose();
    });
  }

  _buildFlow(anchors) {
    const end = [];
    const metrics = [];
    const seed = [];

    for (const a of anchors) {
      const count = 10 + Math.round(50 * a.metrics[1]); // more droplets where more water is used
      for (let i = 0; i < count; i++) {
        end.push(a.x, 0, a.z);
        metrics.push(...a.metrics);
        seed.push(Math.random(), 0.12 + Math.random() * 0.1, Math.random() * 2 - 1);
      }
    }

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.Float32BufferAttribute(end, 3));
    geometry.setAttribute("aMetrics", new THREE.Float32BufferAttribute(metrics, 3));
    geometry.setAttribute("aSeed", new THREE.Float32BufferAttribute(seed, 3));

    const points = new THREE.Points(
      geometry,
      new THREE.ShaderMaterial({
        uniforms: this.flowUniforms,
        vertexShader: FLOW_VERT,
        fragmentShader: FLOW_FRAG,
        transparent: true,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      })
    );
    points.frustumCulled = false;
    return points;
  }
}
