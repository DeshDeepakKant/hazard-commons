"""FROZEN service interfaces between workstreams. Implementations live in their own packages.

Threading model: the sim runs a synchronous fixed-step loop. Anything slow (0G Compute,
0G Storage, chain txs) is submitted to a worker pool and returns a Future that the sim
polls each tick (`fut.done()`). The sim never blocks on the network.
"""
from __future__ import annotations

from concurrent.futures import Future
from typing import Any, Optional, Protocol

from sim.schemas import CheckpointRequest, CheckpointResult, EpisodeRecord, Hazard


class Brain(Protocol):
    """brain/client.py: 0G Router. Never fabricates a decision.

    submit() returns a Future that ALWAYS resolves to a CheckpointResult (never raises):
    ok=False with `error` on timeout / unreachable / invalid JSON after one retry.
    The caller enters SAFE_STOP when ok=False.
    """
    def submit(self, req: CheckpointRequest) -> "Future[CheckpointResult]": ...
    def online(self) -> bool: ...            # last call succeeded / endpoint reachable
    def set_unreachable(self, on: bool) -> None: ...   # demo toggle: 0G unreachable


class Storage(Protocol):
    """memory/zg_storage.py: wraps the TS sidecar (official 0G Storage SDK)."""
    def upload_file(self, local_path: str) -> "Future[dict[str, Any]]": ...
    # resolves to {"root_hash": str, "tx_hash": str|None} or raises


class EpisodeLog(Protocol):
    """memory/episode_log.py: hash-chained per-robot log, batched uploads to 0G Storage."""
    def append(self, rec: EpisodeRecord) -> EpisodeRecord: ...   # fills prev/record sha256
    def flush(self, robot_id: str) -> Optional["Future[dict[str, Any]]"]: ...


class Chain(Protocol):
    """chain/bindings.py: HazardRegistry on 0G Galileo. All writes are async."""
    def report_hazard(self, operator: str, robot_id: str, edge_id: str, hazard_type: str,
                      severity: float, evidence_root: str, zg_request_id: str
                      ) -> "Future[dict[str, Any]]": ...     # {"tx_hash", "hazard_id"}
    def verify_hazard(self, operator: str, robot_id: str, hazard_id: str, confirm: bool,
                      evidence_root: str, zg_request_id: str) -> "Future[dict[str, Any]]": ...
    def clear_hazard(self, operator: str, hazard_id: str) -> "Future[dict[str, Any]]": ...
    def poll_events(self) -> list[dict[str, Any]]: ...       # new decoded events since last poll
    def hazards(self) -> list[Hazard]: ...                   # current shared map (from events)
