"""Per-checkpoint message building, output parsing and validation (SPEC §10.1).

Context keys the sim is expected to send (all optional, any extra keys are passed through;
keys starting with `_` are sim-internal and are stripped recursively before sending):

CP-H1  task, location {edge_id, road_name, x, y}, detection {source, distance_m}
CP-H2  report {hazard_id, edge_id, type, severity, status, passable_for},
       confirms, disputes, reporter_operator, evidence_summary, urgency, task,
       alternatives {extra_distance_m, extra_time_s}
CP-V   claim {hazard_id, edge_id, type, severity, reporter_operator}, location
CP-D   expected_building {address, height, use, name}
"""
from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from brain.schemas import OUTPUT_SCHEMAS
from sim.schemas import CheckpointRequest

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
PROMPT_FILES = {"CP-H1": "cp_h1.md", "CP-H2": "cp_h2.md", "CP-V": "cp_v.md", "CP-D": "cp_d.md"}

INTRO = {
    "CP-H1": "Hazard encounter on the road ahead. Assess the camera image.",
    "CP-H2": "The next segment of my route has an active hazard report. Decide my action.",
    "CP-V": "Verify this hazard report against the camera image of the reported spot.",
    "CP-D": "Am I at the correct delivery location?",
}


@functools.lru_cache(maxsize=None)
def _read(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8").strip()


def system_prompt(checkpoint: str) -> str:
    return _read("system.md") + "\n\n" + _read(PROMPT_FILES[checkpoint])


def schema_example(checkpoint: str) -> str:
    m = re.search(r"```json\s*(\{.*?\})\s*```", _read(PROMPT_FILES[checkpoint]), re.S)
    return m.group(1) if m else "{}"


def strip_private(obj: Any) -> Any:
    """Drop every dict key starting with `_` (recursively). Sim-internal data never leaves."""
    if isinstance(obj, dict):
        return {k: strip_private(v) for k, v in obj.items()
                if not (isinstance(k, str) and k.startswith("_"))}
    if isinstance(obj, (list, tuple)):
        return [strip_private(v) for v in obj]
    return obj


def image_mime(b64: str) -> str:
    if b64.startswith("/9j/"):
        return "image/jpeg"
    if b64.startswith("UklGR"):
        return "image/webp"
    return "image/png"


def public_context(req: CheckpointRequest) -> dict[str, Any]:
    return strip_private(dict(req.context))


def build_messages(req: CheckpointRequest) -> list[dict[str, Any]]:
    ctx = public_context(req)
    payload = {
        "robot": {"id": req.robot_id, "operator": req.operator, "kind": req.robot_kind},
        "sim_time_s": round(req.sim_time, 2),
        "context": ctx,
    }
    text = (f"{INTRO[req.checkpoint]}\n\nCheckpoint data (JSON):\n"
            f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n\n"
            f"{'The camera image is attached.' if req.frame_png_b64 else 'No camera image for this checkpoint.'}"
            f" Reply with ONE JSON object only.")
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    if req.frame_png_b64:
        content.append({"type": "image_url", "image_url": {
            "url": f"data:{image_mime(req.frame_png_b64)};base64,{req.frame_png_b64}"}})
    return [
        {"role": "system", "content": system_prompt(req.checkpoint)},
        {"role": "user", "content": content},
    ]


def corrective_message(checkpoint: str, problem: str) -> dict[str, Any]:
    return {"role": "user", "content": (
        f"Your previous reply was not valid: {problem[:300]}\n"
        f"Reply again with exactly ONE JSON object, no other text, matching this shape and "
        f"allowed values:\n{schema_example(checkpoint)}")}


_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S)


def extract_json(content: Optional[str]) -> dict[str, Any]:
    """Parse the model's reply as one JSON object. Tolerates <think> blocks and code fences
    (formatting only; the values are the model's). Raises ValueError otherwise."""
    if content is None or not content.strip():
        raise ValueError("empty reply")
    s = _THINK.sub("", content).strip()
    m = _FENCE.match(s)
    if m:
        s = m.group(1).strip()
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        i, j = s.find("{"), s.rfind("}")
        if i < 0 or j <= i:
            raise ValueError(f"not JSON: {s[:120]!r}")
        try:
            obj = json.loads(s[i:j + 1])
        except json.JSONDecodeError as e:
            raise ValueError(f"not JSON ({e.msg}): {s[:120]!r}") from e
    if not isinstance(obj, dict):
        raise ValueError(f"expected a JSON object, got {type(obj).__name__}")
    return obj


def validate_output(checkpoint: str, content: Optional[str]) -> dict[str, Any]:
    """-> validated output dict, or raises ValueError describing what is wrong."""
    obj = extract_json(content)
    try:
        out = OUTPUT_SCHEMAS[checkpoint].model_validate(obj)
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:5])
        raise ValueError(f"schema mismatch: {errs}") from e
    data = out.model_dump()
    if checkpoint == "CP-H1" and data["is_hazard"] != (data["type"] != "none"):
        raise ValueError("is_hazard must be false exactly when type is 'none'")
    return data
