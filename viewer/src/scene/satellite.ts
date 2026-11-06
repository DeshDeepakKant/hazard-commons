// Satellite imagery layer: fetches Esri World Imagery tiles at runtime (nothing is bundled or committed),
// stitches them into one canvas texture and exposes per-vertex UVs from local metres.
// Imagery © Esri, Maxar, Earthstar Geographics.
import * as THREE from "three";
import { LocalFrame, lonLatToTile } from "../geo";

const TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
const TILE = 256;

export interface SatBounds { minX: number; maxX: number; minY: number; maxY: number }

export class Satellite {
  /** shared uniform objects, patched into the building material and read live every frame */
  readonly uMap = { value: Satellite.placeholder() };
  readonly uMix = { value: 0 };
  texture: THREE.CanvasTexture | null = null;
  ready = false;
  enabled = true;
  zoom = 17;
  private tx0 = 0; private ty0 = 0; private wPx = 1; private hPx = 1;
  onChange: () => void = () => {};

  constructor(private frame: LocalFrame) {}

  private static placeholder(): THREE.Texture {
    const t = new THREE.DataTexture(new Uint8Array([40, 48, 60, 255]), 1, 1);
    t.needsUpdate = true;
    return t;
  }

  /** local metres -> UV in the stitched texture (exact projection, no affine approximation) */
  uvAt(x: number, y: number, out: [number, number] = [0, 0]): [number, number] {
    const [lon, lat] = this.frame.toLonLat(x, y);
    const [tx, ty] = lonLatToTile(lon, lat, this.zoom);
    out[0] = ((tx - this.tx0) * TILE) / this.wPx;
    out[1] = 1 - ((ty - this.ty0) * TILE) / this.hPx;
    return out;
  }

  private grid = { x0: 0, y0: 0, nx: 1, ny: 1 };

  /** Fix the tile layout up-front (synchronously) so UVs are valid before any tile arrives. */
  plan(b: SatBounds, margin: number): void {
    const corners = [[b.minX - margin, b.minY - margin], [b.maxX + margin, b.minY - margin], [b.maxX + margin, b.maxY + margin], [b.minX - margin, b.maxY + margin]]
      .map(([x, y]) => { const [lo, la] = this.frame.toLonLat(x, y); return lonLatToTile(lo, la, this.zoom); });
    const x0 = Math.floor(Math.min(...corners.map((c) => c[0]))), x1 = Math.floor(Math.max(...corners.map((c) => c[0])));
    const y0 = Math.floor(Math.min(...corners.map((c) => c[1]))), y1 = Math.floor(Math.max(...corners.map((c) => c[1])));
    this.grid = { x0, y0, nx: x1 - x0 + 1, ny: y1 - y0 + 1 };
    this.tx0 = x0; this.ty0 = y0; this.wPx = this.grid.nx * TILE; this.hPx = this.grid.ny * TILE;
  }

  /** Fetch + stitch the planned tiles. Resolves false when imagery is unavailable. */
  async load(renderer: THREE.WebGLRenderer): Promise<boolean> {
    try {
      const { x0, y0, nx, ny } = this.grid, x1 = x0 + nx - 1, y1 = y0 + ny - 1;
      if (nx * TILE > 4096 || ny * TILE > 4096 || nx * ny > 80) throw new Error(`tile grid ${nx}x${ny} too large`);

      const canvas = document.createElement("canvas");
      canvas.width = nx * TILE; canvas.height = ny * TILE;
      const ctx = canvas.getContext("2d");
      if (!ctx) throw new Error("no 2d canvas");
      ctx.fillStyle = "#28303c";
      ctx.fillRect(0, 0, canvas.width, canvas.height);

      let ok = 0;
      const jobs: Promise<void>[] = [];
      const deadline = new Promise<"timeout">((r) => setTimeout(() => r("timeout"), 20000));
      for (let ty = y0; ty <= y1; ty++) for (let tx = x0; tx <= x1; tx++) {
        const url = TILE_URL.replace("{z}", String(this.zoom)).replace("{y}", String(ty)).replace("{x}", String(tx));
        jobs.push(new Promise<void>((res) => {
          const img = new Image();
          img.crossOrigin = "anonymous";
          img.onload = () => { ctx.drawImage(img, (tx - x0) * TILE, (ty - y0) * TILE, TILE, TILE); ok++; res(); };
          img.onerror = () => res();
          img.src = url;
        }));
      }
      await Promise.race([Promise.all(jobs), deadline]);
      if (ok < (nx * ny) * 0.6) throw new Error(`only ${ok}/${nx * ny} tiles loaded`);

      const tex = new THREE.CanvasTexture(canvas);
      tex.colorSpace = THREE.SRGBColorSpace;
      tex.generateMipmaps = true;
      tex.minFilter = THREE.LinearMipmapLinearFilter;
      tex.magFilter = THREE.LinearFilter;
      tex.anisotropy = Math.min(4, renderer.capabilities.getMaxAnisotropy());
      tex.wrapS = tex.wrapT = THREE.ClampToEdgeWrapping;
      tex.needsUpdate = true;
      this.texture = tex;
      this.uMap.value = tex;
      this.ready = true;
      console.info(`[satellite] ${ok}/${nx * ny} tiles at z${this.zoom}, ${canvas.width}x${canvas.height}px`);
      this.apply();
      return true;
    } catch (e) {
      console.warn("[satellite] imagery unavailable, using plain look:", (e as Error).message);
      this.ready = false;
      this.apply();
      return false;
    }
  }

  /** is the textured look currently on screen */
  get active(): boolean { return this.ready && this.enabled; }

  setEnabled(on: boolean): void { this.enabled = on; this.apply(); }

  private apply(): void {
    this.uMix.value = this.active ? 1 : 0;
    this.onChange();
  }

  /** Ground plane (subdivided) in three coords with UVs from the exact projection. Covers `b` + margin. */
  buildGround(b: SatBounds, margin: number, seg = 64): THREE.Mesh {
    const w = b.maxX - b.minX + 2 * margin, h = b.maxY - b.minY + 2 * margin;
    const cx = (b.minX + b.maxX) / 2, cy = (b.minY + b.maxY) / 2;
    const geom = new THREE.PlaneGeometry(w, h, seg, seg);
    geom.rotateX(-Math.PI / 2);                     // plane y -> -z, i.e. local north
    const pos = geom.getAttribute("position");
    const uv = geom.getAttribute("uv");
    const t: [number, number] = [0, 0];
    for (let i = 0; i < pos.count; i++) {
      const x = pos.getX(i) + cx, y = -pos.getZ(i) + cy;   // local metres
      pos.setXYZ(i, x, 0, -y);
      this.uvAt(x, y, t);
      uv.setXY(i, t[0], t[1]);
    }
    geom.computeBoundingSphere();
    const mat = new THREE.MeshStandardMaterial({ color: "#b0b8c4", roughness: 1, metalness: 0, map: null });
    const mesh = new THREE.Mesh(geom, mat);
    mesh.position.y = -0.12;   // below road ribbons (0.04+), well above the far plane (-3): no z-fighting at distance
    mesh.name = "satellite-ground";
    return mesh;
  }

  /** Refresh the ground material after load/toggle. */
  syncGround(mesh: THREE.Mesh): void {
    const mat = mesh.material as THREE.MeshStandardMaterial;
    mat.map = this.texture;
    mat.needsUpdate = true;
    mesh.visible = this.active;
  }

  /** Add a per-vertex `satUv` attribute to a (de-indexed, three-space) geometry from its x/z positions. */
  addUvAttribute(geom: THREE.BufferGeometry): void {
    const pos = geom.getAttribute("position");
    const arr = new Float32Array(pos.count * 2);
    const t: [number, number] = [0, 0];
    for (let i = 0; i < pos.count; i++) { this.uvAt(pos.getX(i), -pos.getZ(i), t); arr[i * 2] = t[0]; arr[i * 2 + 1] = t[1]; }
    geom.setAttribute("satUv", new THREE.BufferAttribute(arr, 2));
  }

  /** Patch a MeshStandardMaterial so up-facing triangles show the imagery (viewed from above). */
  patchBuildingMaterial(mat: THREE.MeshStandardMaterial): void {
    mat.onBeforeCompile = (sh) => {
      sh.uniforms.uSatMap = this.uMap;
      sh.uniforms.uSatMix = this.uMix;
      sh.vertexShader = sh.vertexShader
        .replace("#include <common>", "#include <common>\nattribute vec2 satUv;\nvarying vec2 vSatUv;\nvarying float vRoof;")
        .replace("#include <begin_vertex>", `#include <begin_vertex>
          vSatUv = satUv;
          vRoof = smoothstep(0.7, 0.85, normal.y);`);
      sh.fragmentShader = sh.fragmentShader
        .replace("#include <common>", "#include <common>\nuniform sampler2D uSatMap;\nuniform float uSatMix;\nvarying vec2 vSatUv;\nvarying float vRoof;")
        .replace("#include <color_fragment>", `#include <color_fragment>
          {
            vec3 sat = texture2D(uSatMap, vSatUv).rgb;
            float l = dot(sat, vec3(0.2126, 0.7152, 0.0722));
            sat = mix(vec3(l), sat, 0.85) * 0.78;
            diffuseColor.rgb = mix(diffuseColor.rgb, sat, vRoof * uSatMix);
          }`);
    };
    mat.customProgramCacheKey = () => "sat-roof-v1";
  }
}
