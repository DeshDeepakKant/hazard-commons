// Copy repo-level attribution into the static build (public/photo-credits.md) if present.
import { copyFileSync, existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = resolve(here, "../../assets/hazard_photos/ATTRIBUTION.md");
const dst = resolve(here, "../public/photo-credits.md");
if (existsSync(src)) {
  copyFileSync(src, dst);
  console.log(`[prebuild] copied ${src} -> public/photo-credits.md`);
} else {
  console.log("[prebuild] no assets/hazard_photos/ATTRIBUTION.md yet (footer shows a pointer instead)");
}
