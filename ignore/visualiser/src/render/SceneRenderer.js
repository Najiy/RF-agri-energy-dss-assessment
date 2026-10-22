// The 3D engine. React only hands it a canvas, a site document and a few options; everything else
// (scene, camera rig, tiles, parcels, zone, picking, render loop) lives here.

import * as THREE from "three";
import { CameraRig } from "./CameraRig.js";
import { LocalFrame } from "./mercator.js";
import { ParcelLayer } from "./ParcelLayer.js";
import { TileLayer } from "./TileLayer.js";
import { ZoneLayer } from "./ZoneLayer.js";

const SKY = new THREE.Color("#0e1823"); // background and fog share a colour, so the map melts into the horizon
const FOV = 42;

export const DEFAULT_OPTIONS = {
  heightMode: "demand",
  exaggeration: 1,
  opacity: 0.85,
  mapDim: 0.9,
  showTiles: true,
  showZone: true,
  showFlow: true,
  showEdges: true,
  autoOrbit: false,
};

export class SceneRenderer {
  /**
   * @param {HTMLCanvasElement} canvas
   * @param {{onHover: Function, onSelect: Function, onTiles: Function}} handlers
   */
  constructor(canvas, handlers) {
    this.canvas = canvas;
    this.handlers = handlers;
    this.options = { ...DEFAULT_OPTIONS };

    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;

    this.scene = new THREE.Scene();
    this.scene.background = SKY;
    this.scene.fog = new THREE.Fog(SKY, 1000, 8000);

    this.camera = new THREE.PerspectiveCamera(FOV, 1, 1, 100000);
    this.rig = new CameraRig(this.camera, canvas);

    this.pickTarget = new THREE.WebGLRenderTarget(1, 1);
    this.pickScene = new THREE.Scene();
    this.pickBuffer = new Uint8Array(4);

    this.site = null;
    this.frame = null;
    this.tiles = null;
    this.parcels = null;
    this.zone = null;
    this.ground = this._makeGround();
    this.scene.add(this.ground);

    this.hover = -1;
    this.selected = -1;
    this.mouse = null; // last pointer position, set while hovering
    this.mouseDirty = false;
    this.lastTileStatus = "";
    this.labelEl = null;
    this.lastFrame = performance.now();
    this.time = 0;
    this.running = true;

    this.reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    if (this.reducedMotion) this.rig.damping = 40;

    this._bindPointer();
    this._observeSize();
    this._loop = this._loop.bind(this);
    this.renderer.setAnimationLoop(this._loop);
  }

  /** Replace everything in the scene with a new site document (see export_data.py). */
  setSite(doc) {
    this._clearSite();
    this.site = doc;
    this.hover = -1;
    this.selected = -1;

    this.frame = new LocalFrame(doc.lat, doc.lon);
    this.tiles = new TileLayer(this.frame, {
      anisotropy: Math.min(8, this.renderer.capabilities.getMaxAnisotropy()),
      onStatus: (status) => this._tileStatus(status),
    });
    this.parcels = new ParcelLayer(this.frame, doc.parcels, { radiusM: doc.radiusM });
    this.zone = new ZoneLayer(doc.radiusM, this.parcels);
    this.zone.setReducedMotion(this.reducedMotion);

    this.scene.add(this.tiles.group, this.parcels.group, this.zone.group);
    this.pickScene.add(this.parcels.pickMesh);

    this.setOptions(this.options);
    this.resetView(true);
  }

  setOptions(patch) {
    const o = Object.assign(this.options, patch);

    this.rig.autoOrbit = o.autoOrbit && !this.reducedMotion;
    if (!this.parcels) return;

    this.parcels.setMode(o.heightMode);
    this.parcels.setExaggeration(o.exaggeration);
    this.parcels.setOpacity(o.opacity);
    this.parcels.setEdgesVisible(o.showEdges);
    this.tiles.setDim(o.mapDim);
    this.tiles.setVisible(o.showTiles);
    this.ground.visible = !o.showTiles || this.lastTilesFailed;
    this.zone.setZoneVisible(o.showZone);
    this.zone.setFlowVisible(o.showFlow);
  }

  /** Select a parcel from outside (for example from a list). */
  setSelected(index) {
    this.selected = index;
    this.parcels?.setSelected(index);
  }

  /** Fly to the opening view. `instant` starts from a high swoop instead of the current view. */
  resetView(instant = false) {
    if (instant) this.rig.fly({ target: new THREE.Vector3(), azimuth: 0.4, polar: 0.25, distance: 2600 }, true);
    this.rig.fly({ target: new THREE.Vector3(), azimuth: 0.9, polar: 1.0, distance: 720 });
  }

  /** DOM element that follows the tube-well beacon on screen. */
  setWellLabel(element) {
    this.labelEl = element;
  }

  dispose() {
    this.running = false;
    this.renderer.setAnimationLoop(null);
    this.resizeObserver?.disconnect();
    this.rig.dispose();
    this._clearSite();
    this.pickTarget.dispose();
    this.renderer.dispose();
    this.canvas.removeEventListener("pointermove", this._onMove);
    this.canvas.removeEventListener("pointerleave", this._onLeave);
    this.canvas.removeEventListener("pointerup", this._onUp);
  }

  // --- internals -----------------------------------------------------------------------------

  _clearSite() {
    for (const layer of [this.tiles, this.parcels, this.zone]) {
      if (!layer) continue;
      this.scene.remove(layer.group);
      layer.dispose();
    }
    if (this.parcels) this.pickScene.remove(this.parcels.pickMesh);
    this.tiles = this.parcels = this.zone = null;
    this.lastTilesFailed = false;
  }

  // Flat dark plane with a 100 m grid: what you see under the parcels when tiles are off or offline.
  _makeGround() {
    const size = 128;
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = size;
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#15212d";
    ctx.fillRect(0, 0, size, size);
    ctx.strokeStyle = "#22384b";
    ctx.lineWidth = 2;
    ctx.strokeRect(0, 0, size, size);

    const texture = new THREE.CanvasTexture(canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
    texture.anisotropy = 8;
    texture.repeat.set(1500, 1500);

    const ground = new THREE.Mesh(
      new THREE.PlaneGeometry(150000, 150000).rotateX(-Math.PI / 2),
      new THREE.MeshBasicMaterial({ map: texture, fog: true })
    );
    ground.position.y = -3;
    ground.visible = false;
    return ground;
  }

  _tileStatus(status) {
    const key = `${status.loading}/${status.failed}/${status.zoom}`;
    if (key === this.lastTileStatus) return;
    this.lastTileStatus = key;

    // If every request so far failed (offline), show the grid ground instead of a black void.
    this.lastTilesFailed = status.failed > 0 && status.loading === 0 && this.tiles.group.children.length === 0;
    this.ground.visible = !this.options.showTiles || this.lastTilesFailed;
    this.handlers.onTiles?.(status);
  }

  _observeSize() {
    const fit = () => {
      const { clientWidth: w, clientHeight: h } = this.canvas.parentElement;
      if (!w || !h) return;

      this.renderer.setSize(w, h, false);
      this.camera.aspect = w / h;
      this.camera.updateProjectionMatrix();
    };

    this.resizeObserver = new ResizeObserver(fit);
    this.resizeObserver.observe(this.canvas.parentElement);
    fit();
  }

  _bindPointer() {
    this._onMove = (e) => {
      this.mouse = { x: e.clientX, y: e.clientY };
      this.mouseDirty = true;
    };
    this._onLeave = () => {
      this.mouse = null;
      this._setHover(-1, null);
    };
    this._onUp = (e) => {
      if (e.button !== 0 || this.rig.dragMoved > 5 || !this.parcels) return; // a drag is not a click

      const index = this._pick(e.clientX, e.clientY);
      this.selected = index;
      this.parcels.setSelected(index);
      this.handlers.onSelect?.(index);
    };

    this.canvas.addEventListener("pointermove", this._onMove);
    this.canvas.addEventListener("pointerleave", this._onLeave);
    this.canvas.addEventListener("pointerup", this._onUp);
  }

  // GPU picking: draw the parcels with ids as colours, look at the single pixel under the cursor.
  _pick(clientX, clientY) {
    if (!this.parcels || this.parcels.mesh.geometry.getIndex().count === 0) return -1;

    const rect = this.canvas.getBoundingClientRect();
    const ratio = this.renderer.getPixelRatio();
    const px = Math.floor((clientX - rect.left) * ratio);
    const py = Math.floor((clientY - rect.top) * ratio);
    const width = Math.floor(rect.width * ratio);
    const height = Math.floor(rect.height * ratio);

    this.camera.setViewOffset(width, height, px, py, 1, 1); // render only that pixel

    const previous = this.renderer.getRenderTarget();
    const clearColor = this.renderer.getClearColor(new THREE.Color());
    const clearAlpha = this.renderer.getClearAlpha();

    this.renderer.setRenderTarget(this.pickTarget);
    this.renderer.setClearColor(0x000000, 0);
    this.renderer.clear();
    this.renderer.render(this.pickScene, this.camera);
    this.renderer.readRenderTargetPixels(this.pickTarget, 0, 0, 1, 1, this.pickBuffer);

    this.renderer.setRenderTarget(previous);
    this.renderer.setClearColor(clearColor, clearAlpha);
    this.camera.clearViewOffset();

    const [r, g, b] = this.pickBuffer;
    return r + g * 256 + b * 65536 - 1; // -1 = nothing under the cursor
  }

  _setHover(index, point) {
    if (index !== this.hover) {
      this.hover = index;
      this.parcels?.setHover(index);
      this.canvas.style.cursor = index >= 0 ? "pointer" : "grab";
    }
    this.handlers.onHover?.(index, point);
  }

  _updateFog() {
    const d = this.rig.state.distance;
    const near = d * 1.3;
    const far = Math.max(near * 2.2, Math.min(this.tiles?.coverage ?? 5000, d * 40 + 4000) * 0.95);

    this.scene.fog.near = near;
    this.scene.fog.far = far;
  }

  _updateLabel() {
    if (!this.labelEl || !this.zone) return;

    const top = this.zone.pole.scale.y + 14;
    const v = new THREE.Vector3(0, top, 0).project(this.camera);
    const visible = v.z < 1 && Math.abs(v.x) < 1.1 && Math.abs(v.y) < 1.1 && this.options.showZone;
    const x = ((v.x + 1) / 2) * this.canvas.clientWidth;
    const y = ((1 - v.y) / 2) * this.canvas.clientHeight;

    this.labelEl.style.transform = `translate(${x}px, ${y}px) translate(-50%, -100%)`;
    this.labelEl.style.opacity = visible ? "1" : "0";
  }

  _loop() {
    if (!this.running) return;

    const now = performance.now();
    const dt = Math.min((now - this.lastFrame) / 1000, 0.1);
    this.lastFrame = now;
    this.time += dt;

    this.rig.update(dt, now);
    if (this.tiles) {
      this.tiles.update(this.rig.state.target, this.rig.state.distance, FOV, this.canvas.clientHeight || 1);
      this._updateFog();
    }
    this.parcels?.tick(dt);
    this.zone?.tick(dt, this.time);
    this.zone?.setViewScale(this.canvas.height / (2 * Math.tan((FOV * Math.PI) / 360)));

    // Hover: pick only when the pointer moved, and not while dragging (a pick stalls the GPU briefly).
    if (this.mouse && this.mouseDirty && this.rig.pointers.size === 0) {
      this.mouseDirty = false;
      this._setHover(this._pick(this.mouse.x, this.mouse.y), this.mouse);
    }

    this.renderer.render(this.scene, this.camera);
    this._updateLabel();
  }
}
