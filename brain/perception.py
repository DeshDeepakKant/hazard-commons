"""Realistic perception (SPEC §10.5).

PyBullet renders look like toy boxes, so at hazard checkpoints we composite a REAL street photo
(CC-licensed, assets/hazard_photos/ATTRIBUTION.md) as the main camera view with the live sim
render as a labelled inset, and send that composite to the vision model. Disclosed in the README.

For locations with no physical hazard (e.g. a fake report posted by a cheating operator) we use
a "clear road" photo, so an honest verifier sees no hazard there.

Usage from the sim:
    obs = build_observation(render_rgb, hazard_type=prop.type, real=prop.real,
                            photo=prop.photo, key=prop.id)
    req = CheckpointRequest(..., frame_png_b64=obs.png_b64)
    DecisionEvent(..., frame_thumb_b64=obs.thumb_jpeg_b64); EpisodeRecord.inputs.frame_sha256 = obs.sha256
"""
from __future__ import annotations

import base64
import functools
import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageFont, ImageOps

from common.config import ROOT

PHOTO_DIR = ROOT / "assets" / "hazard_photos"
OUT_W, OUT_H = 640, 480
INSET_FRAC = 0.36
THUMB_W, THUMB_H = 192, 144


@dataclass
class Observation:
    png_b64: str            # exact image sent to 0G (PNG)
    thumb_jpeg_b64: str     # small JPEG for the viewer decision card
    sha256: str             # sha256 of the PNG bytes == EpisodeRecord.inputs.frame_sha256
    photo: Optional[str]    # photo file used (None = sim render only)
    width: int
    height: int


@functools.lru_cache(maxsize=1)
def photo_index() -> dict[str, list[str]]:
    p = PHOTO_DIR / "index.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def pick_photo(hazard_type: Optional[str], real: bool = True, key: str = "") -> Optional[Path]:
    """Deterministic photo choice. No physical hazard (real=False or type none) -> clear road."""
    cat = hazard_type if (real and hazard_type and hazard_type != "none") else "clear_road"
    files = photo_index().get(cat) or []
    if not files:
        return None
    i = int(hashlib.sha256(f"{cat}:{key}".encode()).hexdigest(), 16) % len(files)
    return PHOTO_DIR / files[i]


def to_image(src: Any) -> Image.Image:
    """Accepts a PIL image, PNG/JPEG bytes, base64 string, or an HxWx3/4 uint8 array
    (pybullet getCameraImage rgb, possibly flat)."""
    if isinstance(src, Image.Image):
        return src.convert("RGB")
    if isinstance(src, (bytes, bytearray)):
        return Image.open(io.BytesIO(src)).convert("RGB")
    if isinstance(src, str):
        return Image.open(io.BytesIO(base64.b64decode(src))).convert("RGB")
    import numpy as np
    a = np.asarray(src, dtype=np.uint8)
    if a.ndim == 3:
        return Image.fromarray(a[:, :, :3], "RGB")
    raise TypeError(f"cannot convert {type(src).__name__} to an image")


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _label(img: Image.Image, xy: tuple[int, int], text: str, size: int = 14) -> None:
    d = ImageDraw.Draw(img)
    f = _font(size)
    x0, y0, x1, y1 = d.textbbox(xy, text, font=f)
    d.rectangle((x0 - 4, y0 - 3, x1 + 4, y1 + 3), fill=(0, 0, 0))
    d.text(xy, text, font=f, fill=(255, 255, 255))


def compose(render: Any = None, photo: Optional[str | Path] = None,
            caption: Optional[str] = None) -> Image.Image:
    """Photo as the main 640x480 view (center-cropped), sim render as a labelled inset
    bottom-right. Without a photo, the sim render alone is upscaled to 640x480."""
    if photo is not None:
        base = ImageOps.fit(Image.open(photo).convert("RGB"), (OUT_W, OUT_H),
                            method=Image.Resampling.LANCZOS)
        _label(base, (8, 6), "robot camera (street photo)")
        if render is not None:
            r = to_image(render)
            iw = int(OUT_W * INSET_FRAC)
            ih = int(iw * r.height / max(1, r.width))
            inset = r.resize((iw, ih), Image.Resampling.BILINEAR)
            inset = ImageOps.expand(inset, border=2, fill=(255, 255, 255))
            x, y = OUT_W - inset.width - 8, OUT_H - inset.height - 8
            base.paste(inset, (x, y))
            _label(base, (x + 4, y + 4), "sim render", size=11)
    elif render is not None:
        base = to_image(render).resize((OUT_W, OUT_H), Image.Resampling.BILINEAR)
        _label(base, (8, 6), "sim render")
    else:
        raise ValueError("compose() needs a render and/or a photo")
    if caption:
        _label(base, (8, OUT_H - 24), caption, size=12)
    return base


def encode(img: Image.Image) -> Observation:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    png = buf.getvalue()
    tb = io.BytesIO()
    th = img.copy()
    th.thumbnail((THUMB_W, THUMB_H))
    th.save(tb, format="JPEG", quality=70)
    return Observation(png_b64=base64.b64encode(png).decode("ascii"),
                       thumb_jpeg_b64=base64.b64encode(tb.getvalue()).decode("ascii"),
                       sha256=hashlib.sha256(png).hexdigest(), photo=None,
                       width=img.width, height=img.height)


def build_observation(render: Any = None, hazard_type: Optional[str] = None, real: bool = True,
                      photo: Optional[str] = None, key: str = "", use_photo: bool = True,
                      caption: Optional[str] = None) -> Observation:
    """The checkpoint image. `photo` (HazardProp.photo, a filename in assets/hazard_photos)
    wins only when the hazard is physically real; otherwise a deterministic photo for the type
    (clear road when real=False / type none). use_photo=False -> sim render only."""
    p: Optional[Path] = None
    if use_photo:
        if photo and real and hazard_type not in (None, "none"):
            p = PHOTO_DIR / photo
        else:
            p = pick_photo(hazard_type, real, key)
        if p is not None and not p.exists():
            raise FileNotFoundError(f"hazard photo missing: {p}")
    obs = encode(compose(render, p, caption))
    obs.photo = p.name if p else None
    return obs


def image_sha256(image_b64: str) -> str:
    return hashlib.sha256(base64.b64decode(image_b64)).hexdigest()
