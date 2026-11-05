"""EpisodeLog: per-robot hash-chained decision log, batched uploads to 0G Storage (SPEC §10.2).

Hash chain (per robot, continues across episodes and batches):
    record_sha256 = sha256(canonical_json(record without `record_sha256`))
    canonical_json = json.dumps(model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
                                ensure_ascii=False).encode("utf-8")
    prev_record_sha256 = record_sha256 of the robot's previous record (None for the first).

Batch file `logs/runs/<run_id>/episodes/<robot_id>/batch-<n>.json`:
    {"format": "hazard-commons/episode-batch/v1", "run_id", "robot_id", "batch",
     "prev_batch_last_sha256", "records": [EpisodeRecord...],
     "frames": {"<sha256 of image bytes>": "<base64 image>"}}
Each record's inputs.frame_sha256 = sha256 of the exact image bytes sent to 0G, so a judge can
re-hash the bundled frame. The uploaded file's 0G Storage root hash is the source of truth;
`logs/index.json` is a local mirror: {"version": 1, "batches": [entry, ...]}.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Optional

from common.config import ROOT, settings
from common.interfaces import Storage
from sim.schemas import CheckpointRequest, CheckpointResult, EpisodeRecord, StorageEvent

log = logging.getLogger("hazard.episodes")
FORMAT = "hazard-commons/episode-batch/v1"


# ---------------------------------------------------------------- hashing
def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def record_hash(rec: EpisodeRecord | dict[str, Any]) -> str:
    d = rec.model_dump(mode="json") if isinstance(rec, EpisodeRecord) else dict(rec)
    d.pop("record_sha256", None)
    return hashlib.sha256(canonical_json(d)).hexdigest()


def frame_sha256(image_b64: str) -> str:
    """sha256 of the exact image bytes (decoded from base64) sent to 0G."""
    return hashlib.sha256(base64.b64decode(image_b64)).hexdigest()


def file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def strip_private(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: strip_private(v) for k, v in obj.items()
                if not (isinstance(k, str) and k.startswith("_"))}
    if isinstance(obj, (list, tuple)):
        return [strip_private(v) for v in obj]
    return obj


def record_from_result(req: CheckpointRequest, res: CheckpointResult, action_taken: str,
                       chain_tx: Optional[str] = None) -> EpisodeRecord:
    """Helper for the sim: build the EpisodeRecord for one checkpoint decision (or SAFE_STOP).
    Only the public context (no `_` keys) is logged, i.e. exactly what 0G saw."""
    inputs: dict[str, Any] = {"context": strip_private(dict(req.context))}
    inputs["frame_sha256"] = frame_sha256(req.frame_png_b64) if req.frame_png_b64 else None
    if not res.ok:
        inputs["error"] = res.error
    return EpisodeRecord(
        episode_id=req.episode_id, robot_id=req.robot_id, operator=req.operator,
        sim_time=req.sim_time, checkpoint=req.checkpoint, seq=req.seq, inputs=inputs,
        model=res.model, zg_request_id=res.zg_request_id, client_request_id=res.client_request_id,
        zg_trace=res.zg_trace, tee_verified=res.tee_verified, output=res.output,
        action_taken=action_taken, chain_tx=chain_tx)


# ---------------------------------------------------------------- the log
class EpisodeLog:
    def __init__(self, run_id: str, storage: Optional[Storage] = None,
                 base_dir: Optional[str | Path] = None, flush_every: Optional[int] = None,
                 index_path: Optional[str | Path] = None,
                 on_uploaded: Optional[Callable[[StorageEvent], None]] = None,
                 explorer: Optional[str] = None):
        ss = settings().get("storage", {})
        self.run_id = run_id
        self.storage = storage
        self.base_dir = Path(base_dir) if base_dir else ROOT
        self.flush_every = int(flush_every or ss.get("upload_every_n_records", 8))
        self.index_path = Path(index_path) if index_path else self.base_dir / ss.get(
            "local_mirror", "logs/index.json")
        self.explorer = explorer or ss.get("explorer", "https://storagescan-galileo.0g.ai")
        self.on_uploaded = on_uploaded
        self._lock = threading.RLock()
        self._index_lock = threading.Lock()
        self._last_sha: dict[str, Optional[str]] = {}
        self._pending: dict[str, list[EpisodeRecord]] = {}
        self._frames: dict[str, dict[str, str]] = {}
        self._batch_no: dict[str, int] = {}
        self._batch_prev: dict[str, Optional[str]] = {}
        self.futures: list[Future] = []

    def robot_dir(self, robot_id: str) -> Path:
        return self.base_dir / "logs" / "runs" / self.run_id / "episodes" / robot_id

    # ------------------------------------------------------------------ Protocol
    def append(self, rec: EpisodeRecord, frame_b64: Optional[str] = None) -> EpisodeRecord:
        """Chain the record (fills prev/record sha256). `frame_b64` = the exact image sent to 0G;
        it is bundled into the batch file keyed by its sha256. Auto-flushes every N records."""
        do_flush = False
        with self._lock:
            rid = rec.robot_id
            rec = rec.model_copy(deep=True)
            if frame_b64:
                fsha = frame_sha256(frame_b64)
                rec.inputs = {**rec.inputs, "frame_sha256": fsha}
                self._frames.setdefault(rid, {})[fsha] = frame_b64
            pending = self._pending.setdefault(rid, [])
            if not pending:
                self._batch_prev[rid] = self._last_sha.get(rid)
            rec.prev_record_sha256 = self._last_sha.get(rid)
            rec.record_sha256 = None
            rec.record_sha256 = record_hash(rec)
            self._last_sha[rid] = rec.record_sha256
            pending.append(rec)
            do_flush = len(pending) >= self.flush_every
        if do_flush:
            self.flush(rid)
        return rec

    def flush(self, robot_id: str) -> Optional["Future[dict[str, Any]]"]:
        """Write pending records to a batch file and upload it. Returns the upload Future
        (resolves to the index entry), or None if nothing was pending / no storage configured."""
        with self._lock:
            recs = self._pending.get(robot_id) or []
            if not recs:
                return None
            n = self._batch_no.get(robot_id, 0)
            self._batch_no[robot_id] = n + 1
            frames = self._frames.pop(robot_id, {})
            self._pending[robot_id] = []
            prev = self._batch_prev.get(robot_id)
        path = self.robot_dir(robot_id) / f"batch-{n}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        used = {r.inputs.get("frame_sha256") for r in recs}
        doc = {
            "format": FORMAT, "run_id": self.run_id, "robot_id": robot_id, "batch": n,
            "prev_batch_last_sha256": prev,
            "records": [r.model_dump(mode="json") for r in recs],
            "frames": {k: v for k, v in frames.items() if k in used},
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, sort_keys=True, ensure_ascii=False, indent=1))
        tmp.replace(path)
        entry: dict[str, Any] = {
            "run_id": self.run_id, "robot_id": robot_id, "operator": recs[-1].operator,
            "batch": n, "episode_ids": sorted({r.episode_id for r in recs}),
            "local_path": _rel(path, self.base_dir), "file_sha256": file_sha256(path),
            "record_seqs": [r.seq for r in recs],
            "checkpoints": [r.checkpoint for r in recs],
            "zg_request_ids": [r.zg_request_id for r in recs if r.zg_request_id],
            "record_sha256s": [r.record_sha256 for r in recs],
            "prev_batch_last_sha256": prev,
            "root_hash": None, "tx_hash": None, "status": "pending", "error": None,
            "written_at": time.time(),
        }
        if self.storage is None:
            entry["status"] = "local_only"
            self._write_index(entry)
            return None
        self._write_index(entry)
        out: Future[dict[str, Any]] = Future()
        up = self.storage.upload_file(str(path))
        up.add_done_callback(lambda f: self._on_upload_done(f, entry, recs, out))
        with self._lock:
            self.futures.append(out)
        return out

    # ------------------------------------------------------------------ extras
    def flush_all(self) -> list["Future[dict[str, Any]]"]:
        with self._lock:
            robots = [r for r, p in self._pending.items() if p]
        return [f for f in (self.flush(r) for r in robots) if f is not None]

    def _on_upload_done(self, f: Future, entry: dict[str, Any], recs: list[EpisodeRecord],
                        out: Future) -> None:
        try:
            res = f.result()
        except Exception as e:
            entry.update(status="failed", error=str(e)[:600])
            self._write_index(entry)
            log.error("episode batch %s/%s upload failed: %s", entry["robot_id"], entry["batch"], e)
            out.set_exception(e)
            return
        entry.update(status="uploaded", root_hash=res.get("root_hash"), tx_hash=res.get("tx_hash"),
                     uploaded_at=time.time(),
                     explorer_url=f"{self.explorer.rstrip('/')}/tx/{res.get('tx_hash')}"
                     if res.get("tx_hash") else None)
        self._write_index(entry)
        if self.on_uploaded:
            try:
                self.on_uploaded(StorageEvent(
                    robot_id=entry["robot_id"], episode_id=recs[-1].episode_id,
                    root_hash=entry["root_hash"], tx_hash=entry["tx_hash"],
                    record_seqs=entry["record_seqs"], zg_request_ids=entry["zg_request_ids"],
                    local_path=entry["local_path"]))
            except Exception:
                log.exception("on_uploaded callback failed")
        out.set_result(entry)

    def _write_index(self, entry: dict[str, Any]) -> None:
        with self._index_lock:
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                idx = json.loads(self.index_path.read_text())
                if not isinstance(idx, dict) or "batches" not in idx:
                    idx = {"version": 1, "batches": []}
            except (FileNotFoundError, json.JSONDecodeError):
                idx = {"version": 1, "batches": []}
            key = (entry["run_id"], entry["robot_id"], entry["batch"])
            idx["batches"] = [b for b in idx["batches"]
                              if (b.get("run_id"), b.get("robot_id"), b.get("batch")) != key]
            idx["batches"].append(dict(entry))
            tmp = self.index_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(idx, indent=1, sort_keys=True))
            tmp.replace(self.index_path)


def _rel(p: Path, base: Path) -> str:
    try:
        return str(p.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(p)


# ---------------------------------------------------------------- verification
def verify_chain(path: str | Path) -> dict[str, Any]:
    """Verify a batch file, or a robot directory of batch-*.json files (cross-batch links too).
    -> {"ok": bool, "records": n, "batches": n, "errors": [...]}. Detects edited records,
    reordered/removed records, broken links between batches, and frames not matching their hash."""
    p = Path(path)
    files = sorted(p.glob("batch-*.json"), key=lambda f: int(f.stem.split("-")[1])) \
        if p.is_dir() else [p]
    errors: list[str] = []
    prev: Optional[str] = None
    count = 0
    for i, f in enumerate(files):
        try:
            doc = json.loads(f.read_text())
        except Exception as e:
            errors.append(f"{f.name}: unreadable ({e})")
            continue
        recs = doc.get("records", [])
        if i == 0:
            prev = doc.get("prev_batch_last_sha256")
        elif doc.get("prev_batch_last_sha256") != prev:
            errors.append(f"{f.name}: prev_batch_last_sha256 does not match previous batch")
        for j, r in enumerate(recs):
            count += 1
            where = f"{f.name}#{j} (seq {r.get('seq')})"
            if r.get("prev_record_sha256") != prev:
                errors.append(f"{where}: prev_record_sha256 breaks the chain")
            h = record_hash(r)
            if r.get("record_sha256") != h:
                errors.append(f"{where}: record_sha256 mismatch (record was modified)")
            prev = r.get("record_sha256")
            fsha = (r.get("inputs") or {}).get("frame_sha256")
            frames = doc.get("frames", {})
            if fsha and fsha in frames and frame_sha256(frames[fsha]) != fsha:
                errors.append(f"{where}: bundled frame does not match frame_sha256")
    return {"ok": not errors, "records": count, "batches": len(files), "errors": errors}
