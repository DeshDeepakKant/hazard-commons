#!/usr/bin/env bash
# Seeded random demand for Bandra West (vehicles per type + pedestrians).
# Usage: traffic/demand.sh [--seed N] [--end SECONDS] [--scale F]
#   --scale multiplies all insertion rates (1.0 ~ 150 vehicles + 120 pedestrians in steady state).
# Env: PY (python with eclipse-sumo; default .venv/bin/python)
# Writes traffic/out/{car,motorcycle,auto,bus,ped}.rou.xml.gz
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
SEED=$(PYTHONPATH=. "$PY" -c 'from common.config import settings;print(settings()["traffic"]["seed"])')
END=7200
SCALE=1.0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --seed) SEED="$2"; shift 2 ;;
    --end) END="$2"; shift 2 ;;
    --scale) SCALE="$2"; shift 2 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
SUMO_HOME=${SUMO_HOME:-$("$PY" -c 'import sumo;print(sumo.SUMO_HOME)')}
export SUMO_HOME
BIN=$(cd "$(dirname "$PY")" && pwd)
export PATH="$BIN:$PATH"
RT="$SUMO_HOME/tools/randomTrips.py"
NET=traffic/out/bandra.net.xml.gz
ADD=traffic/vtypes.add.xml
OUT=traffic/out
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# name  vtype  vclass  base_period(s)  seed_offset
# Periods tuned so steady-state concurrency is ~150 vehicles at scale 1.0
gen() {
  local name=$1 vtype=$2 vclass=$3 period=$4 off=$5
  local p
  p=$("$PY" -c "print($period / $SCALE)")
  "$PY" "$RT" -n "$NET" --seed $((SEED + off)) -b 0 -e "$END" -p "$p" \
    --vclass "$vclass" \
    --trip-attributes "type=\"$vtype\" departLane=\"best\" departSpeed=\"max\"" \
    --additional-files "$ADD" \
    --fringe-factor 10 --min-distance 300 --validate --remove-loops \
    --prefix "${name}_" \
    -o "$TMP/$name.trips.xml" -r "$TMP/$name.rou.xml" >/dev/null
  # randomTrips/duarouter re-emit vType definitions; the bridge loads vtypes.add.xml itself
  "$PY" - "$TMP/$name.rou.xml" "$OUT/$name.rou.xml.gz" <<'PYEOF'
import gzip, re, sys
s = open(sys.argv[1]).read()
s = re.sub(r"\s*<vType\b[^>]*/>", "", s)
s = re.sub(r"\s*<vType\b.*?</vType>", "", s, flags=re.S)
gzip.open(sys.argv[2], "wt").write(s)
print(sys.argv[2], s.count("<vehicle "), "vehicles")
PYEOF
}

gen car        car           passenger  4.0  0
gen motorcycle motorcycle    motorcycle 5.0  1
gen auto       auto_rickshaw passenger  5.5  2
gen bus        bus           bus        30   3

# pedestrians: shorter walks, anywhere (no fringe bias)
PP=$("$PY" -c "print(4.0 / $SCALE)")
"$PY" "$RT" -n "$NET" --seed $((SEED + 4)) -b 0 -e "$END" -p "$PP" \
  --pedestrians --max-distance 900 --min-distance 150 --validate \
  --trip-attributes 'type="pedestrian"' --additional-files "$ADD" \
  --prefix "ped_" -o "$TMP/ped.trips.xml" >/dev/null
"$PY" - "$TMP/ped.trips.xml" "$OUT/ped.rou.xml.gz" <<'PYEOF'
import gzip, re, sys
s = open(sys.argv[1]).read()
s = re.sub(r"\s*<vType\b[^>]*/>", "", s)
gzip.open(sys.argv[2], "wt").write(s)
print(sys.argv[2], s.count("<person "), "persons")
PYEOF
ls -la "$OUT"
