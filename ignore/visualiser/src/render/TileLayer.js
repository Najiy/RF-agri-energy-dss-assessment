// OpenStreetMap raster tiles draped on the ground plane, with three levels of detail.

import * as THREE from "three";

const TILE_URL = (z, x, y) => `https://tile.openstreetmap.org/${z}/${x}/${y}.png`;
const MAX_ZOOM = 19; // OSM's deepest level
const HALF_SPAN = 3; // tiles on each side of the centre per level -> 7 x 7 per level
const LEVEL_STEPS = [0, 2, 4]; // fine level, then 2 and 4 zoom levels coarser for the far field
const MAX_PARALLEL = 6; // be polite to the tile server

export class TileLayer {
  constructor(frame, { anisotropy = 4, onStatus = () => {} } = {}) {
    this.frame = frame;
    this.anisotropy = anisotropy;
    this.onStatus = onStatus;

    this.group = new THREE.Group();
    this.tiles = new Map(); // "z/x/y" -> { mesh, texture, loading, dead, z }
    this.queue = [];
    this.active = 0;
    this.failed = 0;
    this.dim = 1;
    this.fineZoom = -1;
    this.coverage = 1000;

    this.geometry = new THREE.PlaneGeometry(1, 1).rotateX(-Math.PI / 2); // flat, image top = north (-z)
  }

  /** Pick the zoom whose texels match screen pixels at the camera target, then (re)select tiles. */
  update(target, distance, fov, viewportHeight) {
    const metresPerPixel = (2 * distance * Math.tan((fov * Math.PI) / 360)) / viewportHeight;
    const idealZoom = Math.log2((this.frame.tileMetres(0) / 256) / metresPerPixel);
    const fine = Math.max(2, Math.min(MAX_ZOOM, Math.floor(idealZoom + 0.6))); // +0.6: favour sharper tiles

    const wanted = new Map();
    LEVEL_STEPS.forEach((step, level) => {
      const z = fine - step;
      if (z < 0) return;

      const [cx, cy] = this.frame.tileAt(z, target.x, target.z);
      const last = 2 ** z - 1;

      for (let dy = -HALF_SPAN; dy <= HALF_SPAN; dy++) {
        for (let dx = -HALF_SPAN; dx <= HALF_SPAN; dx++) {
          const x = cx + dx;
          const y = cy + dy;
          if (x < 0 || y < 0 || x > last || y > last) continue;

          const key = `${z}/${x}/${y}`;
          if (!wanted.has(key)) wanted.set(key, { z, x, y, level, d: Math.hypot(dx, dy) });
        }
      }
    });

    for (const [key, spec] of wanted) {
      const tile = this.tiles.get(key);
      if (tile) this._setLevel(tile, spec.level);
      else this._request(key, spec);
    }

    this.queue.sort((a, b) => a.spec.level - b.spec.level || a.spec.d - b.spec.d); // fine and close first
    this.fineZoom = fine;
    this.coverage = (HALF_SPAN + 0.5) * this.frame.tileMetres(Math.max(0, fine - LEVEL_STEPS.at(-1))); // metres to the map edge
    this._wanted = wanted;
    this._prune();
    this._pump();
  }

  setDim(value) {
    this.dim = value;
    for (const tile of this.tiles.values()) tile.mesh?.material.color.setScalar(value);
  }

  setVisible(on) {
    this.group.visible = on;
  }

  dispose() {
    for (const tile of this.tiles.values()) this._drop(tile);
    this.tiles.clear();
    this.queue.length = 0;
    this.geometry.dispose();
  }

  // Old tiles stay until nothing is loading, so zooming never flashes holes in the map.
  _prune() {
    if (this.active > 0 || this.queue.length > 0) return;

    for (const [key, tile] of this.tiles) {
      if (!this._wanted.has(key)) {
        this._drop(tile);
        this.tiles.delete(key);
      }
    }
  }

  _request(key, spec) {
    const tile = { spec, key, mesh: null, texture: null, loading: false, dead: false, z: spec.z };
    this.tiles.set(key, tile);
    this.queue.push(tile);
  }

  _pump() {
    while (this.active < MAX_PARALLEL && this.queue.length > 0) {
      const tile = this.queue.shift();
      if (!this._wanted.has(tile.key)) {
        this.tiles.delete(tile.key); // the camera moved on before we started
        continue;
      }
      this._load(tile);
    }
    this._report();
  }

  _load(tile) {
    const { z, x, y } = tile.spec;
    const image = new Image();
    image.crossOrigin = "anonymous"; // OSM sends CORS headers, which WebGL needs
    image.decoding = "async";
    tile.loading = true;
    this.active++;

    image.onload = () => {
      this.active--;
      tile.loading = false;

      if (tile.dead) return this._pump();

      const texture = new THREE.Texture(image);
      texture.colorSpace = THREE.SRGBColorSpace;
      texture.anisotropy = this.anisotropy;
      texture.needsUpdate = true;

      const material = new THREE.MeshBasicMaterial({ map: texture, fog: true });
      material.color.setScalar(this.dim);

      const { x0, z0, size } = this.frame.tileBounds(z, x, y);
      const mesh = new THREE.Mesh(this.geometry, material);
      mesh.scale.set(size, 1, size);
      mesh.position.set(x0 + size / 2, 0, z0 + size / 2);

      tile.mesh = mesh;
      tile.texture = texture;
      this._setLevel(tile, tile.spec.level);
      this.group.add(mesh);
      this._pump();
    };

    image.onerror = () => {
      this.active--;
      tile.loading = false;
      this.failed++;
      this.tiles.delete(tile.key);
      this._pump();
    };

    image.src = TILE_URL(z, x, y);
  }

  // Coarser levels are pushed back in depth so they never fight with the finer tiles above them.
  _setLevel(tile, level) {
    tile.spec.level = level;
    if (!tile.mesh) return;

    const m = tile.mesh.material;
    m.polygonOffset = true;
    m.polygonOffsetFactor = level * 2;
    m.polygonOffsetUnits = level * 8;
    tile.mesh.position.y = -level * 0.05;
  }

  _drop(tile) {
    tile.dead = true;
    if (tile.mesh) {
      this.group.remove(tile.mesh);
      tile.mesh.material.dispose();
    }
    tile.texture?.dispose();
  }

  _report() {
    this.onStatus({ loading: this.active + this.queue.length, failed: this.failed, zoom: this.fineZoom });
  }
}
