// Dark stores (operator colour + label) and pickable home markers.
import * as THREE from "three";
import { CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js";
import { opColor } from "../config";
import type { Home, WorldData } from "./world";

export class Pois {
  group = new THREE.Group();
  homesMesh: THREE.InstancedMesh | null = null;
  homes: Home[] = [];
  private homeY: number[] = [];
  private selected = -1;
  private active = new Set<number>();
  private base = new THREE.Color("#e2e8f0");
  private sel = new THREE.Color("#f472b6");
  private act = new THREE.Color("#fbbf24");
  private m = new THREE.Matrix4();

  constructor(world: WorldData) {
    this.group.name = "pois";
    for (const ds of world.pois.dark_stores) {
      const color = opColor(ds.operator);
      const g = new THREE.Group();
      g.position.set(ds.x, 0, -ds.y);
      const pad = new THREE.Mesh(new THREE.CylinderGeometry(9, 9, 0.4, 6), new THREE.MeshStandardMaterial({ color, emissive: color, emissiveIntensity: 0.35, roughness: 0.6 }));
      pad.position.y = 0.2;
      const beam = new THREE.Mesh(
        new THREE.CylinderGeometry(1.6, 1.6, 90, 12, 1, true),
        new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.22, depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide }),
      );
      beam.position.y = 45;
      g.add(pad, beam);
      const el = document.createElement("div");
      el.className = "store-label";
      el.style.setProperty("--op", color);
      el.innerHTML = `<span class="chip"></span>${ds.name || ds.operator + " Dark Store"}`;
      const label = new CSS2DObject(el);
      label.position.y = 62;
      label.center.set(0.5, 1);
      g.add(label);
      this.group.add(g);
    }
    this.homes = world.pois.homes;
    if (this.homes.length) {
      const geom = new THREE.SphereGeometry(2.2, 14, 10);
      const mat = new THREE.MeshStandardMaterial({ color: "#ffffff", emissive: "#94a3b8", emissiveIntensity: 0.35, roughness: 0.3 });
      const mesh = new THREE.InstancedMesh(geom, mat, this.homes.length);
      this.homes.forEach((h, i) => {
        const y = (h.height ?? world.buildingHeight.get(h.building_id) ?? 12) + 5;
        this.homeY.push(y);
        this.m.makeTranslation(h.x, y, -h.y);
        mesh.setMatrixAt(i, this.m);
        mesh.setColorAt(i, this.base);
      });
      mesh.name = "homes";
      this.homesMesh = mesh;
      this.group.add(mesh);
      // stems
      const pts: number[] = [];
      this.homes.forEach((h, i) => pts.push(h.x, this.homeY[i] - 2, -h.y, h.x, this.homeY[i] - 5, -h.y));
      const lg = new THREE.BufferGeometry();
      lg.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
      this.group.add(new THREE.LineSegments(lg, new THREE.LineBasicMaterial({ color: "#cbd5e1", transparent: true, opacity: 0.6 })));
    }
  }

  setSelected(buildingId: string | null): void { this.selected = this.homes.findIndex((h) => h.building_id === buildingId); this.recolor(); }
  setActive(buildingIds: Iterable<string>): void {
    this.active.clear();
    for (const b of buildingIds) { const i = this.homes.findIndex((h) => h.building_id === b); if (i >= 0) this.active.add(i); }
    this.recolor();
  }
  private recolor(): void {
    if (!this.homesMesh) return;
    this.homes.forEach((_, i) => {
      this.homesMesh!.setColorAt(i, i === this.selected ? this.sel : this.active.has(i) ? this.act : this.base);
      const s = i === this.selected ? 1.8 : this.active.has(i) ? 1.35 : 1;
      this.m.makeScale(s, s, s).setPosition(this.homes[i].x, this.homeY[i], -this.homes[i].y);
      this.homesMesh!.setMatrixAt(i, this.m);
    });
    this.homesMesh.instanceColor!.needsUpdate = true;
    this.homesMesh.instanceMatrix.needsUpdate = true;
  }

  homeByBuilding(b: string): Home | undefined { return this.homes.find((h) => h.building_id === b); }
}
