## Checkpoint CP-V: verification of another operator's hazard report

This robot (often a drone over the spot) was assigned to independently verify a hazard report posted by ANOTHER operator. You get the claim (type, severity, reporter) and the current camera image of the reported location. Judge ONLY from what is visible in the image.

- `verdict`: "confirm" if the image clearly shows the claimed hazard type, otherwise "dispute". If the road looks clear and normal, dispute: a report with no visible hazard may be false.
- `observed_type`: what you actually see: "accident", "road_works", "waterlogging", or "none".
- `confidence` (0-1): your confidence in the verdict.
- `reason`: one short sentence describing what you see.

JSON schema (example values):
```json
{"verdict": "dispute", "observed_type": "none", "confidence": 0.85, "reason": "The road is clear with normal traffic; no road works are visible."}
```
