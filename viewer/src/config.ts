// Runtime configuration from URL params + Vite env.
//
//   ?replay=<url to stream.jsonl[.gz]>   replay mode (static, no backend)
//   ?world=<base url>                    world files (meta.json, roads.json, pois.json, buildings.glb|json)
//   ?ws=<ws url>  ?api=<http base>       live mode endpoints (default: same origin, proxied in dev)
//   ?cinematic=1                         start in cinematic camera mode (for video recording)
//   ?sat=0                               start with the satellite imagery off (default on)
//   ?top=1                               start with a top-down camera (alignment checks)
//   ?cam=x,y,dist,azimuthDeg,elevDeg      initial camera (local metres)
//   ?t=<sim seconds>                     replay: start at this time
//   ?verify=<0G Storage root hash>       open the "Verify on 0G Storage" dialog for this root
//   ?indexer=<url>                       0G Storage indexer for downloads (default: Galileo turbo)
//
// Env (build time): VITE_WORLD_BASE, VITE_DEFAULT_REPLAY (hosted demo: replay this when no params).

const q = new URLSearchParams(location.search);
const env = import.meta.env;

function defaultWorldBase(): string {
  if (env.VITE_WORLD_BASE) return env.VITE_WORLD_BASE as string;
  return env.DEV ? "/world" : "world";
}

const replayParam = q.get("replay") ?? (q.has("live") ? null : (env.VITE_DEFAULT_REPLAY as string | undefined) ?? null);

export const CONFIG = {
  replayUrl: replayParam,
  mode: (replayParam ? "replay" : "live") as "live" | "replay",
  worldBase: (q.get("world") ?? defaultWorldBase()).replace(/\/$/, ""),
  devFixtureWorld: "/dev/fixtures/world",
  wsUrl: q.get("ws") ?? `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`,
  apiBase: (q.get("api") ?? "").replace(/\/$/, ""),
  cinematic: q.get("cinematic") === "1",
  satellite: q.get("sat") !== "0",
  topDown: q.get("top") === "1",
  cam: q.get("cam")?.split(",").map(Number) ?? null,
  startT: q.has("t") ? Number(q.get("t")) : null,
  explicitWorld: q.has("world"),
  verifyRoot: q.get("verify"),
  zgIndexer: (q.get("indexer") ?? "https://indexer-storage-testnet-turbo.0g.ai").replace(/\/$/, ""),
};

/** 0G Storage download URL for a root hash (indexer returns the exact uploaded bytes, CORS *) */
export const zgDownloadUrl = (root: string) => `${CONFIG.zgIndexer}/file?root=${encodeURIComponent(root)}`;

export const OPERATORS: Record<string, { color: string; short: string }> = {
  ZipKart: { color: "#ff6b1a", short: "ZK" },
  MinuteMart: { color: "#2f8cff", short: "MM" },
  DashKirana: { color: "#22c55e", short: "DK" },
};

export function opColor(op: string | null | undefined): string {
  return (op && OPERATORS[op]?.color) || "#9ca3af";
}

export const CHECKPOINT_LABEL: Record<string, string> = {
  "CP-H1": "Hazard encounter",
  "CP-H2": "Route-ahead decision",
  "CP-V": "Verification",
  "CP-D": "Delivery arrival",
  "CP-Q": "Bystander inquiry",
};

export const HAZARD_LABEL: Record<string, string> = {
  accident: "Accident",
  road_works: "Road works",
  waterlogging: "Waterlogging",
  none: "No hazard",
};

// Fictional catalogue for the customer order page.
export const CATALOGUE = [
  { id: "milk-1l", name: "Toned milk 1 L", emoji: "🥛", price: 68 },
  { id: "bread", name: "Pav (6 pcs)", emoji: "🍞", price: 40 },
  { id: "eggs-6", name: "Eggs ×6", emoji: "🥚", price: 54 },
  { id: "bananas", name: "Bananas 1 dozen", emoji: "🍌", price: 60 },
  { id: "tomatoes", name: "Tomatoes 500 g", emoji: "🍅", price: 35 },
  { id: "onions", name: "Onions 1 kg", emoji: "🧅", price: 45 },
  { id: "chai", name: "Masala chai 250 g", emoji: "🍵", price: 140 },
  { id: "maggi", name: "Instant noodles ×4", emoji: "🍜", price: 56 },
  { id: "curd", name: "Dahi 400 g", emoji: "🥣", price: 50 },
  { id: "chips", name: "Banana chips", emoji: "🥔", price: 30 },
  { id: "soap", name: "Soap bar ×3", emoji: "🧼", price: 99 },
  { id: "water", name: "Water 5 L", emoji: "💧", price: 75 },
];
