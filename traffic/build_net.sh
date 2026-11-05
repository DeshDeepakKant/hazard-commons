#!/usr/bin/env bash
# Build the SUMO network for Bandra West from the shared OSM extract.
# Usage: traffic/build_net.sh [--osm PATH]   (default world/cache/map.osm)
# Env: PY (python with eclipse-sumo; default .venv/bin/python)
# Writes traffic/out/bandra.net.xml.gz and traffic/out/net_meta.json.
set -euo pipefail
cd "$(dirname "$0")/.."
OSM=world/cache/map.osm
while [[ $# -gt 0 ]]; do
  case "$1" in
    --osm) OSM="$2"; shift 2 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
[[ -f "$OSM" ]] || { echo "OSM file not found: $OSM (pass --osm PATH)" >&2; exit 1; }

PY=${PY:-.venv/bin/python}
BIN=$(dirname "$PY")
NETCONVERT=${NETCONVERT:-$BIN/netconvert}
SUMO_HOME=${SUMO_HOME:-$("$PY" -c 'import sumo;print(sumo.SUMO_HOME)')}
export SUMO_HOME
OUT=traffic/out
mkdir -p "$OUT"

"$NETCONVERT" --osm-files "$OSM" \
  --type-files "$SUMO_HOME/data/typemap/osmNetconvert.typ.xml,$SUMO_HOME/data/typemap/osmNetconvertPedestrians.typ.xml" \
  --lefthand \
  --sidewalks.guess --crossings.guess --walkingareas \
  --output.street-names true \
  --geometry.remove --roundabouts.guess --ramps.guess \
  --junctions.join --tls.guess --tls.join \
  --keep-edges.in-geo-boundary 72.826,19.052,72.842,19.066 \
  --remove-edges.isolated --keep-edges.components 1 \
  --keep-edges.by-vclass passenger,motorcycle,bus,pedestrian,delivery \
  --no-turnarounds.except-deadend true \
  --ignore-errors.edge-type true \
  --no-warnings true \
  --output-file "$OUT/raw.net.xml.gz"

# pass 2: drop edges overhanging the bbox, keep the largest connected component
PYTHONPATH=. "$PY" traffic/clip_edges.py "$OUT/raw.net.xml.gz" "$OUT/keep_edges.txt" 120
"$NETCONVERT" --sumo-net-file "$OUT/raw.net.xml.gz" --lefthand \
  --keep-edges.input-file "$OUT/keep_edges.txt" \
  --remove-edges.isolated --keep-edges.components 1 \
  --no-warnings true \
  --output-file "$OUT/bandra.net.xml.gz"
rm -f "$OUT/raw.net.xml.gz" "$OUT/keep_edges.txt"

"$PY" traffic/net_meta.py "$OUT/bandra.net.xml.gz" "$OUT/net_meta.json"
