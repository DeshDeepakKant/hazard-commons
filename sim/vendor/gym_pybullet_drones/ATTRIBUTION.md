# Vendored: gym-pybullet-drones (MIT)

- Source: https://github.com/utiasDSL/gym-pybullet-drones
- Commit: `bc5000cb52b0770c381b09db34fdc08c27858be8` (main, fetched 2026-11-06)
- License: MIT, Copyright (c) 2020 Jacopo Panerati — see `LICENSE` in this directory.

## Files copied
| Vendored path | Upstream path |
|---|---|
| `control/BaseControl.py` | `gym_pybullet_drones/control/BaseControl.py` |
| `control/DSLPIDControl.py` | `gym_pybullet_drones/control/DSLPIDControl.py` |
| `utils/enums.py` | `gym_pybullet_drones/utils/enums.py` |
| `assets/cf2x.urdf`, `assets/cf2.dae` | `gym_pybullet_drones/assets/` |

## Changes
- Imports rewritten from `gym_pybullet_drones.*` to `sim.vendor.gym_pybullet_drones.*`
  (the upstream package needs Python >= 3.12 + torch/gymnasium; we only need the controller).
- `BaseControl._getURDFParameter`: URDF path resolved from the vendored `assets/` dir
  (`ASSETS_DIR`) instead of `importlib.resources.files('gym_pybullet_drones')`.
- No algorithmic changes. Motor force/torque application (BaseAviary `_physics`) is
  re-implemented in `sim/drone.py` (per-motor `applyExternalForce` in LINK_FRAME on links 0..3,
  z-torque via `applyExternalTorque` on link 4 = `center_of_mass_link`).
