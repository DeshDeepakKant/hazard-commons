## Checkpoint CP-H2: route-ahead decision

The next road segment on this robot's planned route has an active hazard report on the shared on-chain hazard map. You get the report (type, severity, status, reporter), how many independent verifiers confirmed or disputed it, a summary of the evidence, the reporter's operator, this robot's own operator and body type, and how urgent the current delivery is. Decide what the robot should do.

- `action`: "proceed" (keep the planned route), "reroute" (take the alternative route), "wait" (hold position and re-check later), or "handoff_to_drone" (hand the parcel to a drone of the same operator).
- `trust_report`: whether you believe the report is genuine. Weigh confirmations against disputes from other operators, whether the reporter is a rival of this robot's operator (a rival could gain from blocking this route), whether the evidence is consistent, and whether the hazard affects this body type (`passable_for`).
- A report confirmed by an independent verifier is strong evidence. A disputed or rejected report is weak evidence. An unverified report from a rival is uncertain: prefer a safe choice that costs little time.
- `reason`: one short sentence.

JSON schema (example values):
```json
{"action": "reroute", "reason": "Accident confirmed by an independent drone; road impassable for rovers.", "trust_report": true}
```
