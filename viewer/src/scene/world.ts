// Static world: buildings (glb or extruded json), road ribbons, ground, POIs.
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { mergeGeometries } from "three/addons/utils/BufferGeometryUtils.js";
import { CONFIG } from "../config";

export interface Road { id: string; kind: "drive" | "walk" | string; width: number; shape: [number, number][]; name?: string; }
export interface DarkStore { id: string; operator: string; building_id: string; name: string; x: number; y: number; entrance_node?: string; entrance_xy?: [number, number]; }
export interface Home { id: string; building_id: string; address: string; customer_name: string; x: number; y: number; entrance_node?: string; entrance_xy?: [number, number]; height?: number; }
export interface Pois { dark_stores: DarkStore[]; homes: Home[]; }
export interface BuildingRec { id: string; use: string; height: number; centroid: [number, number]; footprint: [number, number][]; name?: string | null; address?: string | null; }

export interface WorldData {
  base: string;
  meta: Record<string, unknown> | null;
  roads: Road[];
  roadById: Map<string, Road>;
  pois: Pois;
  buildingHeight: Map<string, number>;
  bounds: { minX: number; maxX: number; minY: number; maxY: number };
}

/** local (x east, y north, z up) -> three (x, z, -y) */
export const toThree = (x: number, y: number, z = 0, v = new THREE.Vector3()) => v.set(x, z, -y);

async function getJson<T>(url: string): Promise<T | null> {
  try {
    const r = await fetch(url);
    if (!r.ok) return null;
    const ct = r.headers.get("content-type") ?? "";
    if (ct.includes("text/html")) return null;   // SPA fallback page, not our file
    return (await r.json()) as T;
  } catch { return null; }
}

async function exists(url: string): Promise<boolean> {
  try {
    const r = await fetch(url, { method: "HEAD" });
    const ct = r.headers.get("content-type") ?? "";
    return r.ok && !ct.includes("text/html");
  } catch { return false; }
}

export async function resolveWorldBase(): Promise<string> {
  const base = CONFIG.worldBase;
  if (await exists(`${base}/roads.json`)) return base;
  if (import.meta.env.DEV && !CONFIG.explicitWorld && (await exists(`${CONFIG.devFixtureWorld}/roads.json`))) {
    console.warn(`[world] ${base}/roads.json missing, using DEV fixture world ${CONFIG.devFixtureWorld}`);
    return CONFIG.devFixtureWorld;
  }
  return base;
}

export async function loadWorldData(onStatus: (s: string) => void): Promise<WorldData> {
  const base = await resolveWorldBase();
  onStatus(`Loading world from ${base} …`);
  const [meta, roads, pois] = await Promise.all([
    getJson<Record<string, unknown>>(`${base}/meta.json`),
    getJson<Road[]>(`${base}/roads.json`),
    getJson<Pois>(`${base}/pois.json`),
  ]);
  const rs = roads ?? [];
  const bounds = { minX: Infinity, maxX: -Infinity, minY: Infinity, maxY: -Infinity };
  for (const r of rs) for (const [x, y] of r.shape) {
    if (x < bounds.minX) bounds.minX = x; if (x > bounds.maxX) bounds.maxX = x;
    if (y < bounds.minY) bounds.minY = y; if (y > bounds.maxY) bounds.maxY = y;
  }
  if (!isFinite(bounds.minX)) Object.assign(bounds, { minX: -700, maxX: 700, minY: -700, maxY: 700 });
  return {
    base, meta, roads: rs, roadById: new Map(rs.map((r) => [r.id, r])),
    pois: pois ?? { dark_stores: [], homes: [] }, buildingHeight: new Map(), bounds,
  };
}

// ---------------------------------------------------------------- buildings
const USE_COLORS: Record<string, string[]> = {
  residential: ["#ebe7e0", "#e2ddd5", "#f1eee8", "#d9d4cc", "#e8e2d6"],
  commercial: ["#c9d3e2", "#b8c5d8", "#d3dbe7", "#aebcd2"],
  other: ["#d9dce1", "#cfd3d9", "#e2e4e8"],
};

function hash(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
}

/** fake ambient occlusion: darken towards the ground */
function shadeByHeight(geom: THREE.BufferGeometry): void {
  const pos = geom.getAttribute("position");
  const col = geom.getAttribute("color");
  if (!col) return;
  for (let i = 0; i < pos.count; i++) {
    const y = pos.getY(i);
    const k = 0.62 + 0.38 * Math.min(1, Math.max(0, y / 14));
    col.setXYZ(i, col.getX(i) * k, col.getY(i) * k, col.getZ(i) * k);
  }
  col.needsUpdate = true;
}

export function buildingsFromJson(recs: BuildingRec[], heights: Map<string, number>): THREE.BufferGeometry {
  const pos: number[] = [], nor: number[] = [], col: number[] = [];
  const c = new THREE.Color();
  for (const b of recs) {
    let fp = b.footprint;
    if (!fp || fp.length < 3) continue;
    if (fp[0][0] === fp[fp.length - 1][0] && fp[0][1] === fp[fp.length - 1][1]) fp = fp.slice(0, -1);
    if (fp.length < 3) continue;
    // CCW in (x,y)
    let area = 0;
    for (let i = 0; i < fp.length; i++) { const [x1, y1] = fp[i], [x2, y2] = fp[(i + 1) % fp.length]; area += x1 * y2 - x2 * y1; }
    if (area < 0) fp = fp.slice().reverse();
    const h = Math.max(3, b.height || 9);
    heights.set(b.id, h);
    const pal = USE_COLORS[b.use] ?? USE_COLORS.other;
    c.set(pal[hash(b.id) % pal.length]).convertSRGBToLinear();
    // walls
    for (let i = 0; i < fp.length; i++) {
      const [x1, y1] = fp[i], [x2, y2] = fp[(i + 1) % fp.length];
      const ex = x2 - x1, ey = y2 - y1, len = Math.hypot(ex, ey) || 1;
      const nx = ey / len, ny = -ex / len;   // outward for CCW
      // three coords: (x, z, -y)
      const a = [x1, 0, -y1], bb = [x2, 0, -y2], cc = [x2, h, -y2], d = [x1, h, -y1];
      for (const v of [a, bb, cc, a, cc, d]) { pos.push(v[0], v[1], v[2]); nor.push(nx, 0, -ny); col.push(c.r, c.g, c.b); }
    }
    // roof
    const contour = fp.map(([x, y]) => new THREE.Vector2(x, y));
    const tris = THREE.ShapeUtils.triangulateShape(contour, []);
    const rc = c.clone().multiplyScalar(1.08);
    for (const t of tris) {
      for (const k of t) { const [x, y] = fp[k]; pos.push(x, h, -y); nor.push(0, 1, 0); col.push(rc.r, rc.g, rc.b); }
    }
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  g.setAttribute("normal", new THREE.Float32BufferAttribute(nor, 3));
  g.setAttribute("color", new THREE.Float32BufferAttribute(col, 3));
  shadeByHeight(g);
  g.computeBoundingSphere();
  return g;
}

async function buildingsFromGlb(url: string, heights: Map<string, number>): Promise<THREE.BufferGeometry | null> {
  try {
    if (!(await exists(url))) return null;
    const gltf = await new GLTFLoader().loadAsync(url);
    gltf.scene.updateMatrixWorld(true);
    const geoms: THREE.BufferGeometry[] = [];
    const c = new THREE.Color();
    gltf.scene.traverse((o) => {
      const m = o as THREE.Mesh;
      if (!m.isMesh) return;
      let g = m.geometry.clone().applyMatrix4(m.matrixWorld);
      if (g.index) g = g.toNonIndexed();
      if (!g.getAttribute("normal")) g.computeVertexNormals();
      for (const k of Object.keys(g.attributes)) if (!["position", "normal", "color"].includes(k)) g.deleteAttribute(k);
      const n = g.getAttribute("position").count;
      const ca = g.getAttribute("color");
      if (!ca || ca.itemSize !== 3) {
        // derive colour from material or from use palette
        const mat = (Array.isArray(m.material) ? m.material[0] : m.material) as THREE.MeshStandardMaterial;
        if (mat?.color && !(mat.color.r > 0.99 && mat.color.g > 0.99 && mat.color.b > 0.99)) c.copy(mat.color);
        else c.set(USE_COLORS.residential[hash(m.name) % 5]).convertSRGBToLinear();
        const arr = new Float32Array(n * 3);
        for (let i = 0; i < n; i++) { arr[i * 3] = c.r; arr[i * 3 + 1] = c.g; arr[i * 3 + 2] = c.b; }
        g.setAttribute("color", new THREE.BufferAttribute(arr, 3));
      } else if (!(ca.array instanceof Float32Array)) {
        // normalized ints -> float
        const arr = new Float32Array(n * 3);
        for (let i = 0; i < n; i++) { arr[i * 3] = ca.getX(i); arr[i * 3 + 1] = ca.getY(i); arr[i * 3 + 2] = ca.getZ(i); }
        g.setAttribute("color", new THREE.BufferAttribute(arr, 3));
      }
      g.computeBoundingBox();
      if (m.name) heights.set(m.name, g.boundingBox!.max.y);
      geoms.push(g);
    });
    if (!geoms.length) return null;
    const merged = mergeGeometries(geoms, false);
    if (!merged) return null;
    shadeByHeight(merged);
    merged.computeBoundingSphere();
    return merged;
  } catch (e) {
    console.warn("[world] buildings.glb failed, falling back to json", e);
    return null;
  }
}

export async function loadBuildings(w: WorldData, onStatus: (s: string) => void): Promise<THREE.Mesh | null> {
  onStatus("Loading buildings …");
  let geom = await buildingsFromGlb(`${w.base}/buildings.glb`, w.buildingHeight);
  if (!geom) {
    const recs = await getJson<BuildingRec[]>(`${w.base}/buildings.json`);
    if (recs) geom = buildingsFromJson(recs, w.buildingHeight);
  }
  if (!geom) return null;
  const mat = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.85, metalness: 0.05 });
  const mesh = new THREE.Mesh(geom, mat);
  mesh.name = "buildings";
  mesh.matrixAutoUpdate = false;
  return mesh;
}

// ---------------------------------------------------------------- roads
/** Flat ribbon along a polyline (mitred joints), in three coords at height y. */
export function ribbon(shape: [number, number][], width: number, y: number, pos: number[], idx: number[]): void {
  const n = shape.length;
  if (n < 2) return;
  const half = width / 2;
  const base = pos.length / 3;
  for (let i = 0; i < n; i++) {
    const [x, yy] = shape[i];
    const p = shape[Math.max(0, i - 1)], q = shape[Math.min(n - 1, i + 1)];
    let tx = q[0] - p[0], ty = q[1] - p[1];
    const tl = Math.hypot(tx, ty) || 1; tx /= tl; ty /= tl;
    let nx = -ty, ny = tx;
    // miter scale
    let scale = 1;
    if (i > 0 && i < n - 1) {
      const ax = x - p[0], ay = yy - p[1], al = Math.hypot(ax, ay) || 1;
      const anx = -ay / al, any = ax / al;
      const dot = anx * nx + any * ny;
      scale = Math.min(2.5, 1 / Math.max(0.4, dot));
    }
    nx *= half * scale; ny *= half * scale;
    pos.push(x + nx, y, -(yy + ny), x - nx, y, -(yy - ny));
  }
  for (let i = 0; i < n - 1; i++) {
    const a = base + i * 2;
    idx.push(a, a + 1, a + 2, a + 1, a + 3, a + 2);
  }
}

export function buildRoads(w: WorldData): THREE.Group {
  const g = new THREE.Group();
  g.name = "roads";
  const layers: { filter: (r: Road) => boolean; color: string; y: number; widen: number }[] = [
    { filter: (r) => r.kind !== "drive", color: "#344055", y: 0.04, widen: 0 },
    { filter: (r) => r.kind === "drive", color: "#4a556b", y: 0.06, widen: 2.2 },  // kerb / sidewalk band
    { filter: (r) => r.kind === "drive", color: "#2a3242", y: 0.08, widen: 0 },
  ];
  for (const L of layers) {
    const pos: number[] = [], idx: number[] = [];
    for (const r of w.roads) if (L.filter(r)) ribbon(r.shape, Math.max(2, r.width || 6) + L.widen, L.y, pos, idx);
    if (!pos.length) continue;
    const geom = new THREE.BufferGeometry();
    geom.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    geom.setIndex(idx);
    geom.computeVertexNormals();
    const m = new THREE.Mesh(geom, new THREE.MeshStandardMaterial({ color: L.color, roughness: 0.95, metalness: 0 }));
    m.matrixAutoUpdate = false;
    m.receiveShadow = false;
    g.add(m);
  }
  // centre lines on wide roads
  const pts: number[] = [];
  for (const r of w.roads) {
    if (r.kind !== "drive" || (r.width ?? 0) < 9) continue;
    for (let i = 0; i < r.shape.length - 1; i++) {
      const [x1, y1] = r.shape[i], [x2, y2] = r.shape[i + 1];
      pts.push(x1, 0.1, -y1, x2, 0.1, -y2);
    }
  }
  if (pts.length) {
    const lg = new THREE.BufferGeometry();
    lg.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
    const lines = new THREE.LineSegments(lg, new THREE.LineBasicMaterial({ color: "#6b7385", transparent: true, opacity: 0.55 }));
    lines.matrixAutoUpdate = false;
    g.add(lines);
  }
  return g;
}

export function buildGround(w: WorldData): THREE.Group {
  const g = new THREE.Group();
  const cx = (w.bounds.minX + w.bounds.maxX) / 2, cy = (w.bounds.minY + w.bounds.maxY) / 2;
  const plane = new THREE.Mesh(
    new THREE.PlaneGeometry(9000, 9000),
    new THREE.MeshStandardMaterial({ color: "#161d29", roughness: 1 }),
  );
  plane.rotation.x = -Math.PI / 2;
  plane.position.set(cx, -0.02, -cy);
  g.add(plane);
  // district pad (slightly lighter) under the map
  const sx = w.bounds.maxX - w.bounds.minX + 120, sy = w.bounds.maxY - w.bounds.minY + 120;
  const pad = new THREE.Mesh(new THREE.PlaneGeometry(sx, sy), new THREE.MeshStandardMaterial({ color: "#1b2331", roughness: 1 }));
  pad.rotation.x = -Math.PI / 2;
  pad.position.set(cx, 0.0, -cy);
  g.add(pad);
  const grid = new THREE.GridHelper(Math.max(sx, sy) * 1.6, 60, "#243045", "#1e2838");
  grid.position.set(cx, 0.01, -cy);
  (grid.material as THREE.Material).transparent = true;
  (grid.material as THREE.Material).opacity = 0.35;
  g.add(grid);
  return g;
}

/** points along an edge chain, in local coords */
export function polylineForEdges(w: WorldData, edgeIds: string[]): [number, number][] {
  const out: [number, number][] = [];
  for (const id of edgeIds) {
    const r = w.roadById.get(id) ?? w.roadById.get(id.replace(/^-/, ""));
    if (!r) continue;
    let s = r.shape;
    // orient consecutive edges so they chain
    if (out.length && s.length) {
      const [lx, ly] = out[out.length - 1];
      const d0 = Math.hypot(s[0][0] - lx, s[0][1] - ly), d1 = Math.hypot(s[s.length - 1][0] - lx, s[s.length - 1][1] - ly);
      if (d1 < d0) s = s.slice().reverse();
    }
    for (const p of s) out.push(p);
  }
  return out;
}

export function edgeMidpoint(w: WorldData, edgeId: string): [number, number] | null {
  const r = w.roadById.get(edgeId);
  if (!r || !r.shape.length) return null;
  let total = 0;
  for (let i = 0; i < r.shape.length - 1; i++) total += Math.hypot(r.shape[i + 1][0] - r.shape[i][0], r.shape[i + 1][1] - r.shape[i][1]);
  let acc = 0;
  for (let i = 0; i < r.shape.length - 1; i++) {
    const [x1, y1] = r.shape[i], [x2, y2] = r.shape[i + 1];
    const l = Math.hypot(x2 - x1, y2 - y1);
    if (acc + l >= total / 2) { const t = (total / 2 - acc) / (l || 1); return [x1 + (x2 - x1) * t, y1 + (y2 - y1) * t]; }
    acc += l;
  }
  return r.shape[0];
}
