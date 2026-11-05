## Checkpoint CP-D: delivery arrival

The robot believes it has reached the customer's building. Compare the camera image with the expected building description (address, height, use) and decide whether the robot is at the correct location.

- `at_correct_location`: true if the view is consistent with the expected building.
- `confidence` (0-1): your confidence.

JSON schema (example values):
```json
{"at_correct_location": true, "confidence": 0.8}
```
