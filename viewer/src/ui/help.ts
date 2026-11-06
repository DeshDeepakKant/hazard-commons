// Keyboard shortcuts overlay ('?').
import { el } from "./dom";

const KEYS: [string, string][] = [
  ["?", "this help"], ["o", "orders drawer"], ["c", "cinematic camera"], ["r", "reset camera"],
  ["d", "cycle drones"], ["Esc", "close / back to orbit"],
  ["click home marker", "pick delivery address"], ["click operator card", "filter feed & orders"],
  ["click robot / order row", "follow its robot"], ["click hazard row", "fly to hazard"],
];

export function initHelp(host: HTMLElement): void {
  const root = el("div", "help-overlay", `<div class="help-box"><h3>Keyboard shortcuts</h3><dl>${
    KEYS.map(([k, d]) => `<dt><kbd>${k}</kbd></dt><dd>${d}</dd>`).join("")}</dl><small>press ? or Esc to close</small></div>`);
  root.hidden = true;
  root.addEventListener("click", () => { root.hidden = true; });
  host.appendChild(root);
  window.addEventListener("keydown", (e) => {
    const tag = (e.target as HTMLElement)?.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if (e.key === "?") root.hidden = !root.hidden;
    else if (e.key === "Escape") root.hidden = true;
  });
}
