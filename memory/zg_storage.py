"""ZGStorage: Storage Protocol (common/interfaces.py) over the official TS SDK sidecar.

`node memory/zg-storage/upload.mjs <file>` prints ONE JSON line {"root_hash","tx_hash"} and exits 0,
or exits !=0 with the error on stderr. Uploads wait for finality (can take minutes), so they run
in a worker pool and never block the sim. One retry on failure, then the Future raises.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from common.config import ROOT, settings

log = logging.getLogger("hazard.storage")


class StorageError(RuntimeError):
    pass


class ZGStorage:
    def __init__(self, sidecar_dir: Optional[str | Path] = None, timeout_s: float = 600.0,
                 retries: int = 1, max_workers: int = 2, node: Optional[str] = None):
        ss = settings().get("storage", {})
        self.sidecar_dir = Path(sidecar_dir or ROOT / ss.get("sidecar_dir", "memory/zg-storage"))
        self.timeout_s = timeout_s
        self.retries = retries
        self.node = node or shutil.which("node") or "node"
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="zg-storage")

    # ------------------------------------------------------------------ Storage Protocol
    def upload_file(self, local_path: str) -> "Future[dict[str, Any]]":
        return self._pool.submit(self._upload_with_retry, str(Path(local_path).resolve()))

    # ------------------------------------------------------------------ extras
    def upload_file_sync(self, local_path: str) -> dict[str, Any]:
        return self._upload_with_retry(str(Path(local_path).resolve()))

    def download(self, root_hash: str, out_path: str, retries: int = 6) -> "Future[dict[str, Any]]":
        def _go() -> dict[str, Any]:
            env = {"ZG_DOWNLOAD_RETRIES": str(retries)}
            return self._run(["download.mjs", root_hash, str(Path(out_path).resolve())], env)
        return self._pool.submit(_go)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ internals
    def _upload_with_retry(self, path: str) -> dict[str, Any]:
        last: Optional[Exception] = None
        for attempt in range(1 + self.retries):
            try:
                t0 = time.perf_counter()
                res = self._run(["upload.mjs", path],
                                {"ZG_UPLOAD_TIMEOUT_S": str(max(10, int(self.timeout_s) - 5))})
                if not res.get("root_hash"):
                    raise StorageError(f"sidecar returned no root_hash: {res}")
                res.setdefault("tx_hash", None)
                res["upload_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                res["attempts"] = attempt + 1
                return res
            except Exception as e:
                last = e
                log.warning("0G Storage upload attempt %d failed: %s", attempt + 1, e)
        raise StorageError(f"0G Storage upload failed after {1 + self.retries} attempts: {last}")

    def _run(self, args: list[str], extra_env: dict[str, str]) -> dict[str, Any]:
        env = dict(os.environ)
        env.update(extra_env)
        try:
            p = subprocess.run([self.node, *args], cwd=self.sidecar_dir, env=env,
                               capture_output=True, text=True, timeout=self.timeout_s)
        except subprocess.TimeoutExpired as e:
            raise StorageError(f"sidecar timeout after {self.timeout_s:.0f}s") from e
        except FileNotFoundError as e:
            raise StorageError(f"node not found: {e}") from e
        if p.returncode != 0:
            raise StorageError(f"sidecar exit {p.returncode}: {_last_error(p.stderr)}")
        lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
        if not lines:
            raise StorageError("sidecar printed nothing on stdout")
        try:
            return json.loads(lines[-1])
        except json.JSONDecodeError as e:
            raise StorageError(f"sidecar stdout is not JSON: {lines[-1][:200]!r}") from e


def _last_error(stderr: str) -> str:
    """The SDK is chatty on stderr; keep the sidecar's own 'upload failed:' line if present."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    for ln in reversed(lines):
        if ln.startswith(("upload failed", "download failed")):
            return ln[:600]
    return (lines[-1] if lines else "(no stderr)")[:600]
