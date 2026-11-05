## Checkpoint CP-H1: hazard encounter

Something was detected on the road segment directly ahead of the robot. Look at the camera image and decide whether there is a real road hazard, of which type, how severe it is, and which robot body types can still pass safely.

- `type`: "accident" (crashed or collided vehicles, collision debris), "road_works" (construction, barricades, dug-up road), "waterlogging" (standing or flood water on the road), or "none".
- `is_hazard` must be false exactly when `type` is "none".
- `severity` (0-1): 0 = no impact, 1 = road fully blocked or dangerous.
- `passable_for`: the body types that can still pass safely, a subset of ["rover", "drone"]. A ground rover cannot drive through deep water, a blocked carriageway or a crash scene; a drone flies over at altitude and is usually unaffected. With no hazard, both can pass.
- `confidence` (0-1): your confidence in this assessment.
- `description`: one short sentence about what you see.

JSON schema (example values):
```json
{"is_hazard": true, "type": "accident", "severity": 0.8, "passable_for": ["drone"], "confidence": 0.9, "description": "Two cars collided and block the left lane."}
```
