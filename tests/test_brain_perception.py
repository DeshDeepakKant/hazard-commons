"""Perception compositing: real photo + sim render inset, hashes, clear-road for fake reports."""
from __future__ import annotations

import base64
import hashlib
import io

import numpy as np
from PIL import Image

from brain import perception


def render():
    a = np.zeros((240, 320, 4), dtype=np.uint8)
    a[:, :, 1] = 180
    a[:, :, 3] = 255
    return a


def test_photos_index_has_all_types():
    idx = perception.photo_index()
    for k in ("accident", "road_works", "waterlogging", "clear_road"):
        assert idx.get(k), k
        for f in idx[k]:
            assert (perception.PHOTO_DIR / f).exists()


def test_build_observation_hashes_and_thumb():
    obs = perception.build_observation(render(), hazard_type="accident", key="hz-1")
    png = base64.b64decode(obs.png_b64)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert obs.sha256 == hashlib.sha256(png).hexdigest() == perception.image_sha256(obs.png_b64)
    assert (obs.width, obs.height) == (640, 480)
    assert obs.photo.startswith("accident_")
    th = Image.open(io.BytesIO(base64.b64decode(obs.thumb_jpeg_b64)))
    assert th.format == "JPEG" and th.width <= 192
    again = perception.build_observation(render(), hazard_type="accident", key="hz-1")
    assert again.sha256 == obs.sha256          # deterministic


def test_fake_report_uses_clear_road():
    obs = perception.build_observation(render(), hazard_type="road_works", real=False,
                                       photo="road_works_1.jpg", key="fake")
    assert obs.photo.startswith("clear_road_")
    obs = perception.build_observation(render(), hazard_type="none")
    assert obs.photo.startswith("clear_road_")


def test_render_only():
    obs = perception.build_observation(render(), use_photo=False)
    assert obs.photo is None and obs.width == 640
