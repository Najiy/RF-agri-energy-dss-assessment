// Web Mercator (the projection of OpenStreetMap tiles) and the local metre frame the 3D scene uses.

export const WORLD = 40075016.68557849; // Earth's circumference at the equator, metres
const R = WORLD / (2 * Math.PI);
const RAD = Math.PI / 180;

export function toMercator(lon, lat) {
  const clamped = Math.max(-85.0511, Math.min(85.0511, lat)); // the projection blows up at the poles
  return [R * lon * RAD, R * Math.log(Math.tan(Math.PI / 4 + (clamped * RAD) / 2))];
}

export function fromMercator(mx, my) {
  return [mx / R / RAD, (2 * Math.atan(Math.exp(my / R)) - Math.PI / 2) / RAD];
}

/**
 * Flat metre frame centred on the site: x = east, z = south (so north is -z and "up" stays +y).
 * Mercator stretches distances by 1/cos(lat); multiplying by cos(lat) gives true ground metres.
 */
export class LocalFrame {
  constructor(lat, lon) {
    [this.mx, this.my] = toMercator(lon, lat);
    this.k = Math.cos(lat * RAD);
  }

  toLocal(lon, lat) {
    const [mx, my] = toMercator(lon, lat);
    return [(mx - this.mx) * this.k, -(my - this.my) * this.k];
  }

  fromLocal(x, z) {
    return fromMercator(this.mx + x / this.k, this.my - z / this.k);
  }

  /** Ground size of one tile at zoom z, in metres. */
  tileMetres(z) {
    return (WORLD / 2 ** z) * this.k;
  }

  /** Tile (x, y) that contains the local point. */
  tileAt(z, x, zl) {
    const size = WORLD / 2 ** z;
    const mx = this.mx + x / this.k;
    const my = this.my - zl / this.k;

    return [Math.floor((mx + WORLD / 2) / size), Math.floor((WORLD / 2 - my) / size)];
  }

  /** North-west corner and side length of a tile in local metres. */
  tileBounds(z, tx, ty) {
    const size = WORLD / 2 ** z;
    const [x0, z0] = this.toLocalMercator(-WORLD / 2 + tx * size, WORLD / 2 - ty * size);

    return { x0, z0, size: size * this.k };
  }

  toLocalMercator(mx, my) {
    return [(mx - this.mx) * this.k, -(my - this.my) * this.k];
  }
}
