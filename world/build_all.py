"""Build every world/out/ artifact from the local OSM extract and print a summary.

    PYTHONPATH=. .venv/bin/python -m world.build_all --osm world/cache/map.osm [--no-preview]
"""
from __future__ import annotations

import argparse
import json

from world import build_graph, build_meshes, fetch_osm, pois
from world.fetch_osm import DEFAULT_OSM, out_dir


def preview(dst) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection, PolyCollection

    od = out_dir()
    roads = json.loads((od / "roads.json").read_text())
    blds = json.loads((od / "buildings.json").read_text())
    p = json.loads((od / "pois.json").read_text())
    col = {"residential": "#b0a092", "commercial": "#7092c4", "other": "#a8a8a8"}
    fig, ax = plt.subplots(figsize=(11, 10), dpi=110)
    ax.add_collection(PolyCollection([b["footprint"] for b in blds], facecolors=[col[b["use"]] for b in blds],
                                     edgecolors="none"))
    for kind, c, lw in (("walk", "#9bd18b", 0.5), ("drive", "#333333", 1.2)):
        rs = [r["shape"] for r in roads if r["kind"] == kind]
        ax.add_collection(LineCollection(rs, colors=c, linewidths=lw))
    opc = {"ZipKart": "#ff6b1a", "MinuteMart": "#2f8cff", "DashKirana": "#22c55e"}
    for s in p["dark_stores"]:
        ax.plot(s["x"], s["y"], "s", ms=12, color=opc[s["operator"]], mec="k")
        ax.annotate(s["id"], (s["x"], s["y"]), xytext=(6, 6), textcoords="offset points", fontsize=9, weight="bold")
    for h in p["homes"]:
        ax.plot(h["x"], h["y"], "o", ms=5, color="#d11", mec="k", mew=0.4)
        ax.plot(*zip((h["x"], h["y"]), h["entrance_xy"]), "-", color="#d11", lw=0.6)
        ax.annotate(h["id"][2:], (h["x"], h["y"]), xytext=(3, 3), textcoords="offset points", fontsize=6)
    ax.set_aspect("equal")
    ax.autoscale()
    ax.set_title("Hazard Commons world: Bandra West (north up, local metres)  © OpenStreetMap contributors")
    fig.tight_layout()
    fig.savefig(dst)
    plt.close(fig)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--osm", default=DEFAULT_OSM)
    ap.add_argument("--no-preview", action="store_true")
    a = ap.parse_args(argv)
    osm_args = ["--osm", a.osm]
    fetch_osm.main(osm_args)
    g = build_graph.main(osm_args)
    blds = build_meshes.main(osm_args)
    pp = pois.main(osm_args, buildings=blds)
    od = out_dir()
    if not a.no_preview:
        preview(od / "preview.png")
    xs = [n["x"] for n in g["nodes"]]
    ys = [n["y"] for n in g["nodes"]]
    print("\n=== world/out summary ===")
    print(f"graph: {len(g['nodes'])} nodes, {len(g['edges'])} drive edges, "
          f"{sum(e['length'] for e in g['edges']) / 1000:.1f} km; extent {max(xs) - min(xs):.0f} x {max(ys) - min(ys):.0f} m")
    print(f"buildings: {len(blds)}; dark stores: {[s['id'] for s in pp['dark_stores']]}; homes: {len(pp['homes'])}")
    for f in sorted(od.rglob("*")):
        if f.is_file() and f.name != ".gitkeep":
            print(f"  {f.relative_to(od)}: {f.stat().st_size / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
