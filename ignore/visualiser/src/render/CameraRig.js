// Hand-written orbit / pan / zoom controls with damping (no OrbitControls).
//
//   drag            orbit around the target
//   right-drag      pan along the ground  (also: shift + drag, middle button)
//   wheel / pinch   zoom toward the cursor
//   two fingers     pinch to zoom, drag to pan
//
// The rig keeps a "goal" state and eases the visible state toward it, so every move glides.

import * as THREE from "three";

const MIN_DISTANCE = 60;
const MIN_HEIGHT = 12; // camera never drops below this above the ground, so it stays out of tall parcels
const MAX_DISTANCE = 60000;
const MIN_POLAR = 0.08; // radians from straight down
const MAX_POLAR = 1.5; // just above the horizon
const MAX_PAN = 25000; // metres from the site

const ZOOM_SPEED = 0.0016;
const ORBIT_SPEED = 0.0055;

const ground = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);

export class CameraRig {
  constructor(camera, element) {
    this.camera = camera;
    this.element = element;

    this.state = { target: new THREE.Vector3(), azimuth: 0.6, polar: 0.35, distance: 1800 };
    this.goal = { target: new THREE.Vector3(), azimuth: 0.6, polar: 1.0, distance: 650 };

    this.damping = 11; // higher = snappier
    this.autoOrbit = false;
    this.idleSince = 0;

    this.pointers = new Map();
    this.dragMoved = 0;
    this.lastPinch = 0;

    this._raycaster = new THREE.Raycaster();
    this._ndc = new THREE.Vector2();

    this._bind();
    this.apply();
  }

  /** Move the goal; the camera glides there. Pass `instant` to jump. */
  fly({ target, azimuth, polar, distance }, instant = false) {
    if (target) this.goal.target.copy(target);
    if (azimuth !== undefined) this.goal.azimuth = azimuth;
    if (polar !== undefined) this.goal.polar = THREE.MathUtils.clamp(polar, MIN_POLAR, MAX_POLAR);
    if (distance !== undefined) this.goal.distance = THREE.MathUtils.clamp(distance, MIN_DISTANCE, MAX_DISTANCE);

    if (instant) {
      this.state.target.copy(this.goal.target);
      Object.assign(this.state, { azimuth: this.goal.azimuth, polar: this.goal.polar, distance: this.goal.distance });
    }
  }

  /** Ease toward the goal and place the camera; call once per frame. */
  update(dt, now) {
    const k = 1 - Math.exp(-dt * this.damping);
    const { state, goal } = this;

    if (this.autoOrbit && now - this.idleSince > 2500) goal.azimuth += dt * 0.07;

    goal.polar = Math.min(goal.polar, Math.acos(Math.min(1, MIN_HEIGHT / goal.distance)));

    state.target.lerp(goal.target, k);
    state.azimuth += (goal.azimuth - state.azimuth) * k;
    state.polar += (goal.polar - state.polar) * k;
    state.distance *= Math.pow(goal.distance / state.distance, k); // zoom is multiplicative

    this.apply();
  }

  apply() {
    const { target, azimuth, polar, distance } = this.state;
    const sinP = Math.sin(polar);

    this.camera.position.set(
      target.x + distance * sinP * Math.sin(azimuth),
      target.y + distance * Math.cos(polar),
      target.z + distance * sinP * Math.cos(azimuth)
    );
    this.camera.lookAt(target);

    // Tight clip planes near the ground keep depth precision high when zoomed in.
    this.camera.near = Math.max(0.5, distance * 0.02);
    this.camera.far = distance * 80 + 6000;
    this.camera.updateProjectionMatrix();
  }

  dispose() {
    const el = this.element;
    el.removeEventListener("pointerdown", this._down);
    el.removeEventListener("pointermove", this._move);
    el.removeEventListener("pointerup", this._up);
    el.removeEventListener("pointercancel", this._up);
    el.removeEventListener("wheel", this._wheel);
    el.removeEventListener("contextmenu", this._noMenu);
  }

  _bind() {
    const el = this.element;

    this._noMenu = (e) => e.preventDefault();

    this._down = (e) => {
      el.setPointerCapture(e.pointerId);
      this.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY, button: e.button, shift: e.shiftKey });
      this.dragMoved = 0;
      this.idleSince = performance.now();
      if (this.pointers.size === 2) this.lastPinch = this._pinchDistance();
    };

    this._move = (e) => {
      const p = this.pointers.get(e.pointerId);
      if (!p) return;

      const dx = e.clientX - p.x;
      const dy = e.clientY - p.y;
      this.dragMoved += Math.abs(dx) + Math.abs(dy);
      this.idleSince = performance.now();

      if (this.pointers.size === 1) {
        const panning = p.button === 2 || p.button === 1 || p.shift;
        if (panning) this._pan(dx, dy);
        else this._orbit(dx, dy);
      } else if (this.pointers.size === 2) {
        this._pinch(e.pointerId, dx, dy);
      }

      p.x = e.clientX;
      p.y = e.clientY;
    };

    this._up = (e) => {
      this.pointers.delete(e.pointerId);
      if (el.hasPointerCapture(e.pointerId)) el.releasePointerCapture(e.pointerId);
    };

    this._wheel = (e) => {
      e.preventDefault();
      const delta = e.deltaMode === 1 ? e.deltaY * 33 : e.deltaY; // line mode -> pixels
      this._zoomAt(Math.exp(delta * ZOOM_SPEED), e.clientX, e.clientY);
      this.idleSince = performance.now();
    };

    el.addEventListener("pointerdown", this._down);
    el.addEventListener("pointermove", this._move);
    el.addEventListener("pointerup", this._up);
    el.addEventListener("pointercancel", this._up);
    el.addEventListener("wheel", this._wheel, { passive: false });
    el.addEventListener("contextmenu", this._noMenu);
  }

  _orbit(dx, dy) {
    this.goal.azimuth -= dx * ORBIT_SPEED;
    this.goal.polar = THREE.MathUtils.clamp(this.goal.polar - dy * ORBIT_SPEED, MIN_POLAR, MAX_POLAR);
  }

  // Drag the ground with the cursor: sideways moves along the camera's right, vertical along its forward.
  _pan(dx, dy) {
    const { azimuth, polar, distance } = this.goal;
    const metresPerPixel = (2 * distance * Math.tan((this.camera.fov * Math.PI) / 360)) / this.element.clientHeight;
    const foreshortening = 1 / Math.max(0.2, Math.cos(polar)); // a tilted ground stretches vertical pixels

    const right = new THREE.Vector3(Math.cos(azimuth), 0, -Math.sin(azimuth));
    const forward = new THREE.Vector3(-Math.sin(azimuth), 0, -Math.cos(azimuth));

    this.goal.target
      .addScaledVector(right, -dx * metresPerPixel)
      .addScaledVector(forward, dy * metresPerPixel * foreshortening);
    this.goal.target.x = THREE.MathUtils.clamp(this.goal.target.x, -MAX_PAN, MAX_PAN);
    this.goal.target.z = THREE.MathUtils.clamp(this.goal.target.z, -MAX_PAN, MAX_PAN);
  }

  _pinchDistance() {
    const [a, b] = [...this.pointers.values()];
    return Math.hypot(a.x - b.x, a.y - b.y);
  }

  // Two fingers: spread/pinch zooms, the shared movement pans.
  _pinch(id, dx, dy) {
    const now = this._pinchDistance();
    if (this.lastPinch > 0 && now > 0) {
      const [a, b] = [...this.pointers.values()];
      this._zoomAt(this.lastPinch / now, (a.x + b.x) / 2, (a.y + b.y) / 2);
    }
    this.lastPinch = now;
    this._pan(dx / 2, dy / 2);
  }

  // Zoom by `factor` (<1 in) and slide the target toward the point under the cursor, so that point stays put.
  _zoomAt(factor, clientX, clientY) {
    const before = this.goal.distance;
    const after = THREE.MathUtils.clamp(before * factor, MIN_DISTANCE, MAX_DISTANCE);
    const hit = this._groundPoint(clientX, clientY);

    this.goal.distance = after;

    if (hit) {
      const pull = 1 - after / before;
      this.goal.target.x += (hit.x - this.goal.target.x) * pull;
      this.goal.target.z += (hit.z - this.goal.target.z) * pull;
    }
  }

  _groundPoint(clientX, clientY) {
    const rect = this.element.getBoundingClientRect();
    this._ndc.set(((clientX - rect.left) / rect.width) * 2 - 1, -((clientY - rect.top) / rect.height) * 2 + 1);
    this._raycaster.setFromCamera(this._ndc, this.camera);

    return this._raycaster.ray.intersectPlane(ground, new THREE.Vector3());
  }
}
