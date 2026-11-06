import "./style.css";
import * as THREE from "three";
import { CONFIG } from "./config";
import { store, type Card } from "./store";
import type { ChainTxEvent, Hazard, Order } from "./types";
import { SceneView } from "./scene/index";
import { buildGround, buildRoads, loadBuildings, loadWorldData } from "./scene/world";
import { Actors } from "./scene/actors";
import { Robots } from "./scene/robots";
import { Hazards } from "./scene/hazards";
import { Pois } from "./scene/pois";
import { DecisionPanel } from "./ui/panel";
import { Topbar } from "./ui/topbar";
import { Dashboards } from "./ui/dashboards";
import { OrderPage } from "./ui/order";
import { Timeline } from "./ui/timeline";
import { el, esc } from "./ui/dom";
import { LiveSource, fetchMeta, setBrainUnreachable } from "./live";
import { ReplayPlayer, loadJsonl } from "./replay";
import type { SnapshotBuffer } from "./interp";

const app = document.getElementById("app")!;
app.innerHTML = `
  <div class="top-host"></div>
  <div class="mock-banner" hidden>DEV MOCK BRAIN — these decisions are NOT from 0G</div>
  <main>
    <div class="left-host"></div>
    <div class="stage"><div class="outage-veil" hidden><div><b>0G unreachable</b><span>Every robot pulled over · nothing guessed · waiting for the brain</span></div></div><div class="toasts"></div></div>
    <div class="right-host"></div>
  </main>
  <div class="bottom-host"></div>
  <footer>
    <span>© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap contributors</a></span>
    <a href="#" class="credits-link">Photo credits</a>
    <span>ZipKart, MinuteMart and DashKirana are fictional brands.</span>
    <span class="grow"></span>
    <span class="powered">Decisions: 0G Compute · Memory: 0G Storage · Shared map: 0G Chain (Galileo)</span>
  </footer>
  <div class="loading"><div class="spinner"></div><div class="msg">Loading…</div></div>`;

const loadingMsg = app.querySelector<HTMLElement>(".loading .msg")!;
const setStatus = (s: string) => { loadingMsg.textContent = s; };
const stage = app.querySelector<HTMLElement>(".stage")!;
const toasts = app.querySelector<HTMLElement>(".toasts")!;

function toast(html: string, cls = ""): void {
  const t = el("div", `toast ${cls}`, html);
  toasts.prepend(t);
  while (toasts.children.length > 4) toasts.lastElementChild!.remove();
  setTimeout(() => t.classList.add("out"), 4200);
  setTimeout(() => t.remove(), 4800);
}

async function showCredits(): Promise<void> {
  let text = "Hazard photos: see assets/hazard_photos/ATTRIBUTION.md in the repository.";
  try {
    const r = await fetch("photo-credits.md");
    if (r.ok && !(r.headers.get("content-type") ?? "").includes("html")) text = await r.text();
  } catch { /* keep default */ }
  const m = el("div", "modal", `<div class="modal-box"><header><h2>Photo credits</h2><button class="x">✕</button></header><pre>${esc(text)}</pre>
    <p class="muted small">Map data © OpenStreetMap contributors (ODbL). Operator brands are fictional.</p></div>`);
  m.addEventListener("click", (e) => { if (e.target === m || (e.target as HTMLElement).classList.contains("x")) m.remove(); });
  document.body.appendChild(m);
}
app.querySelector(".credits-link")!.addEventListener("click", (e) => { e.preventDefault(); void showCredits(); });

async function boot(): Promise<void> {
  // ---------------------------------------------------------------- data source
  let live: LiveSource | null = null;
  let replay: ReplayPlayer | null = null;
  if (CONFIG.mode === "replay") {
    setStatus(`Loading recording ${CONFIG.replayUrl} …`);
    const envs = await loadJsonl(CONFIG.replayUrl!, (b, total) =>
      setStatus(`Loading recording … ${(b / 1e6).toFixed(1)}${total ? ` / ${(total / 1e6).toFixed(1)}` : ""} MB`));
    replay = new ReplayPlayer(envs);
    setStatus(`Recording: ${replay.buffer.snaps.length} snapshots, ${replay.events.length} events`);
  }

  // ---------------------------------------------------------------- world
  const world = await loadWorldData(setStatus);
  const view = new SceneView(stage, world);
  view.scene.add(buildGround(world), buildRoads(world));
  const buildings = await loadBuildings(world, setStatus);
  if (buildings) view.scene.add(buildings);
  else console.warn("[world] no buildings found");
  const pois = new Pois(world);
  const actors = new Actors();
  const robots = new Robots(world);
  const hazards = new Hazards(world);
  view.scene.add(pois.group, actors.group, robots.group, robots.routeGroup, hazards.group, hazards.propGroup);
  if (pois.homesMesh) view.pickables.push(pois.homesMesh);
  view.getRobotPos = (id) => robots.position(id);

  // ---------------------------------------------------------------- UI
  const topbar = new Topbar(app.querySelector(".top-host")!, {
    onOutage: (on) => setBrainUnreachable(on).then((ok) => { if (!ok) toast("Could not reach /api/brain/unreachable", "err"); return ok; }),
    onRoutes: (on) => { robots.showRoutes = on; },
    onSatellite: (on) => {
      const sat = view.scene.getObjectByName("satellite-imagery");
      if (sat) sat.visible = on;
    },
    onCamera: (m) => { if (m === "reset") view.resetView(); else view.setMode(view.mode === "cinematic" ? "orbit" : "cinematic"); },
    onFollow: (id) => view.setMode(id ? "follow" : "orbit", id),
    onOrder: () => orderPage.toggle(),
  });
  const leftHost = app.querySelector<HTMLElement>(".left-host")!;
  const dash = new Dashboards(leftHost);
  dash.onRobot = (id) => view.setMode("follow", id);
  const orderPage = new OrderPage(leftHost, world.pois.homes, {
    onSelectHome: (h) => {
      pois.setSelected(h?.building_id ?? null);
      if (h) view.flyTo(new THREE.Vector3(h.x, 0, -h.y), 260);
    },
    onTrack: (o) => trackOrder(o),
    onFollow: (id) => view.setMode("follow", id),
  });
  const panel = new DecisionPanel(app.querySelector(".right-host")!);
  panel.onCardClick = (c) => view.setMode("follow", c.ev.result.robot_id);
  hazards.onClick = (h) => { const p = hazards.position(h.id); if (p) { view.setMode("orbit"); view.flyTo(p, 180); } };
  robots.onClick = (id) => view.setMode("follow", id);
  view.onPick = (hit) => {
    if (hit.object === pois.homesMesh && hit.instanceId !== undefined) {
      const h = pois.homes[hit.instanceId];
      if (CONFIG.mode === "live") orderPage.pickHome(h);
      else toast(`🏠 ${esc(h.customer_name)} · ${esc(h.address)}`);
    }
  };

  let trackedOrder: string | null = null;
  function trackOrder(o: Order | null): void {
    trackedOrder = o?.id ?? null;
    robots.highlighted = new Set(o?.robot_id ? [o.robot_id] : []);
    pois.setSelected(o?.customer_building_id ?? null);
  }

  const mockBanner = app.querySelector<HTMLElement>(".mock-banner")!;
  const veil = app.querySelector<HTMLElement>(".outage-veil")!;
  let activeHomesDirty = true;

  store.on((what, payload) => {
    if (what === "card") {
      const c = payload as Card;
      panel.upsert(c);
      if (c.ev.result.mock) mockBanner.hidden = false;
      if (c.seq === store.cards.length - 1) {
        const p = robots.position(c.ev.result.robot_id);
        if (p && Date.now() - lastFocus > 4000) { lastFocus = Date.now(); view.focus(p); }
      }
    } else if (what === "brain") {
      const on = payload as boolean;
      topbar.setBrain(on); veil.hidden = on;
      toast(on ? "🧠 0G brain back online — robots resume" : "⛔ 0G unreachable — robots enter SAFE STOP", on ? "ok" : "err");
    } else if (what === "order") {
      const o = payload as Order;
      activeHomesDirty = true;
      if (o.id === trackedOrder) robots.highlighted = new Set(o.robot_id ? [o.robot_id] : []);
    } else if (what === "tx") {
      const t = payload as ChainTxEvent;
      if (t.status === "mined") toast(`⛓ <b>${esc(t.kind)}</b> mined on 0G Chain${t.operator ? ` · ${esc(t.operator)}` : ""}`, "tx");
      if (t.status === "failed") toast(`⛓ ${esc(t.kind)} tx failed`, "err");
    } else if (what === "hazards") {
      for (const h of store.hazards.values()) {
        const prev = hzSeen.get(h.id);
        if (prev !== h.status) {
          hzSeen.set(h.id, h.status);
          if (prev !== undefined || h.status === "reported") hazardToast(h);
        }
      }
    }
  });
  const hzSeen = new Map<string, string>();
  let lastFocus = 0;
  function hazardToast(h: Hazard): void {
    const icon = { reported: "⚠️", confirmed: "🟥", rejected: "🚫", cleared: "✅" }[h.status] ?? "⚠️";
    toast(`${icon} ${esc(h.type.replace("_", " "))} <b>${esc(h.status)}</b> · reported by ${esc(h.reporter_operator)}`, `hz-${h.status}`);
  }

  // ---------------------------------------------------------------- start the source
  let buffer: SnapshotBuffer;
  let timeline: Timeline | null = null;
  if (replay) {
    buffer = replay.buffer;
    timeline = new Timeline(app.querySelector(".bottom-host")!, replay);
    replay.onRebuild = () => {
      panel.rebuild();
      hzSeen.clear();
      for (const h of store.hazards.values()) hzSeen.set(h.id, h.status);
      topbar.setBrain(store.brainOnline); veil.hidden = store.brainOnline;
      if (store.cards.some((c) => c.ev.result.mock)) mockBanner.hidden = false;
      dash.update(true);
      activeHomesDirty = true;
      if (orderPage.open) orderPage.render();
    };
    topbar.setConn("replay");
    if (CONFIG.startT != null) replay.seek(CONFIG.startT); else replay.rebuild();
  } else {
    live = new LiveSource();
    buffer = live.buffer;
    live.onConn = (s) => topbar.setConn(s);
    live.start();
    fetchMeta().then((m) => {
      const ops = (m?.operators as { name: string; balance?: number | string }[] | undefined) ?? [];
      for (const o of ops) if (o.balance !== undefined) dash.balances[o.name] = o.balance;
      if (m?.mock) mockBanner.hidden = false;
    });
  }
  if (CONFIG.cinematic) view.setMode("cinematic");

  // keyboard
  window.addEventListener("keydown", (e) => {
    if ((e.target as HTMLElement)?.tagName === "INPUT") return;
    if (e.key === "c") view.setMode(view.mode === "cinematic" ? "orbit" : "cinematic");
    if (e.key === "r") view.resetView();
    if (e.key === "o") orderPage.toggle();
    if (e.key === "Escape") { orderPage.toggle(false); view.setMode("orbit"); }
  });

  app.querySelector(".loading")!.remove();

  // ---------------------------------------------------------------- frame loop
  let slow = 0, prev = performance.now();
  const loop = () => {
    const nowMs = performance.now();
    const dt = Math.min(0.1, (nowMs - prev) / 1000);
    prev = nowMs;
    const now = nowMs / 1000;
    if (replay) { replay.tick(dt); timeline!.update(); }
    const t = replay ? replay.time : live!.renderTime();
    const s = buffer.sample(t);
    if (s) {
      actors.update(s.a.actors ?? [], s.b.actors ?? [], s.alpha);
      robots.update(s.a.robots ?? [], s.b.robots ?? [], s.alpha, now);
      hazards.syncProps(s.b.props ?? []);
    }
    hazards.sync(store.hazards.values(), now);
    topbar.setClock(t);
    slow += dt;
    if (slow > 0.5) {
      slow = 0;
      dash.update();
      orderPage.tick();
      const rs = store.latest?.robots ?? [];
      topbar.setRobots(rs.map((r) => ({ id: r.id, operator: r.operator })).sort((a, b) => a.id.localeCompare(b.id)));
      if (activeHomesDirty) {
        activeHomesDirty = false;
        pois.setActive(Array.from(store.orders.values()).filter((o) => o.status !== "delivered" && o.status !== "failed").map((o) => o.customer_building_id));
        if (trackedOrder) { const o = store.orders.get(trackedOrder); if (o) pois.setSelected(o.customer_building_id); }
      }
    }
    view.update(dt);
    requestAnimationFrame(loop);
  };
  requestAnimationFrame(loop);
  (window as unknown as { hc: unknown }).hc = { store, view, replay, live, actors, robots };
}

boot().catch((e) => {
  console.error(e);
  setStatus(`Failed to start: ${(e as Error).message}`);
  app.querySelector(".loading")?.classList.add("failed");
});
