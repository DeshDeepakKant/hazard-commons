"""Small helpers for the sim package."""
from __future__ import annotations

import contextlib
import os
import sys
from typing import Iterator


@contextlib.contextmanager
def quiet_stdout() -> Iterator[None]:
    """Silence C-level stdout (pybullet URDF importer warnings) for the duration of the block."""
    try:
        fd = sys.stdout.fileno()
    except (AttributeError, OSError, ValueError):   # pytest capture etc.
        yield
        return
    sys.stdout.flush()
    saved = os.dup(fd)
    try:
        with open(os.devnull, "w") as dn:
            os.dup2(dn.fileno(), fd)
            yield
    finally:
        os.dup2(saved, fd)
        os.close(saved)
