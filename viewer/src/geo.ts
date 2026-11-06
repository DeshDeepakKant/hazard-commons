// Map projections: WGS84 lon/lat <-> UTM (transverse Mercator, Kruger series) <-> web-mercator tile coords.
// Local frame = UTM minus origin_utm (x east, y north, metres), see docs/INTERFACES.md.

const A = 6378137.0;
const F = 1 / 298.257223563;
const K0 = 0.9996;
const N = F / (2 - F);
const N2 = N * N, N3 = N2 * N, N4 = N3 * N;
const RECT = (A / (1 + N)) * (1 + N2 / 4 + N4 / 64);
const ALPHA = [
  N / 2 - (2 * N2) / 3 + (5 * N3) / 16 + (41 * N4) / 180,
  (13 * N2) / 48 - (3 * N3) / 5 + (557 * N4) / 1440,
  (61 * N3) / 240 - (103 * N4) / 140,
  (49561 * N4) / 161280,
];
const BETA = [
  N / 2 - (2 * N2) / 3 + (37 * N3) / 96 - N4 / 360,
  N2 / 48 + N3 / 15 - (437 * N4) / 1440,
  (17 * N3) / 480 - (37 * N4) / 840,
  (4397 * N4) / 161280,
];
const DELTA = [
  2 * N - (2 * N2) / 3 - 2 * N3 + (116 * N4) / 45,
  (7 * N2) / 3 - (8 * N3) / 5 - (227 * N4) / 45,
  (56 * N3) / 15 - (136 * N4) / 35,
  (4279 * N4) / 630,
];
const RAD = Math.PI / 180;

export const utmCentralMeridian = (zone: number): number => (zone * 6 - 183) * RAD;

/** lon/lat (degrees) -> UTM easting/northing (metres) */
export function lonLatToUtm(lon: number, lat: number, zone: number, north = true): [number, number] {
  const phi = lat * RAD, lam = lon * RAD - utmCentralMeridian(zone);
  const e = (2 * Math.sqrt(N)) / (1 + N);
  const sp = Math.sin(phi);
  const t = Math.sinh(Math.atanh(sp) - e * Math.atanh(e * sp));
  const xi0 = Math.atan2(t, Math.cos(lam));
  const eta0 = Math.atanh(Math.sin(lam) / Math.hypot(1, t));
  let xi = xi0, eta = eta0;
  for (let j = 1; j <= 4; j++) {
    xi += ALPHA[j - 1] * Math.sin(2 * j * xi0) * Math.cosh(2 * j * eta0);
    eta += ALPHA[j - 1] * Math.cos(2 * j * xi0) * Math.sinh(2 * j * eta0);
  }
  return [500000 + K0 * RECT * eta, (north ? 0 : 10000000) + K0 * RECT * xi];
}

/** UTM easting/northing -> lon/lat (degrees) */
export function utmToLonLat(E: number, Nn: number, zone: number, north = true): [number, number] {
  const xi = (Nn - (north ? 0 : 10000000)) / (K0 * RECT), eta = (E - 500000) / (K0 * RECT);
  let xi0 = xi, eta0 = eta;
  for (let j = 1; j <= 4; j++) {
    xi0 -= BETA[j - 1] * Math.sin(2 * j * xi) * Math.cosh(2 * j * eta);
    eta0 -= BETA[j - 1] * Math.cos(2 * j * xi) * Math.sinh(2 * j * eta);
  }
  const chi = Math.asin(Math.sin(xi0) / Math.cosh(eta0));
  let phi = chi;
  for (let j = 1; j <= 4; j++) phi += DELTA[j - 1] * Math.sin(2 * j * chi);
  const lam = utmCentralMeridian(zone) + Math.atan2(Math.sinh(eta0), Math.cos(xi0));
  return [lam / RAD, phi / RAD];
}

/** Fractional web-mercator tile coordinates at zoom z (x east, y south). */
export function lonLatToTile(lon: number, lat: number, z: number): [number, number] {
  const n = 2 ** z, s = Math.sin(lat * RAD);
  return [((lon + 180) / 360) * n, (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * n];
}

/** Local metres (x east, y north) <-> lon/lat, given the world's UTM zone and origin. */
export class LocalFrame {
  constructor(public zone: number, public originUtm: [number, number], public north = true) {}
  /** From meta.json (epsg 326xx / 327xx). */
  static fromMeta(meta: { epsg?: number; origin_utm?: number[] } | null | undefined): LocalFrame {
    const epsg = meta?.epsg ?? 32643;
    const zone = epsg % 100, north = Math.floor(epsg / 100) === 326;
    const o = meta?.origin_utm;
    return new LocalFrame(zone, o && o.length >= 2 ? [o[0], o[1]] : [272066.1955488995, 2108763.0331085664], north);
  }
  toLonLat(x: number, y: number): [number, number] {
    return utmToLonLat(x + this.originUtm[0], y + this.originUtm[1], this.zone, this.north);
  }
  fromLonLat(lon: number, lat: number): [number, number] {
    const [e, n] = lonLatToUtm(lon, lat, this.zone, this.north);
    return [e - this.originUtm[0], n - this.originUtm[1]];
  }
}
