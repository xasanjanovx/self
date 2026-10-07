"""Логотип @ishdasiz без жёлтой плашки (07.10): из bot/assets/ishdasiz_logo.png (непрозрачный, жёлтый фон) делает две прозрачные версии.

  ishdasiz_logo_light.png — для тёмных фото: круг и «ISHDASIZ» фирменным жёлтым, слоган белым (самолётик — «вырез», сквозь него видно фото);
  ishdasiz_logo_navy.png  — для светлых: всё тёмно-синее, как в оригинале.

Маска: насколько пиксель «синий, а не жёлтый» (красный канал: синий ≈ 10–40, жёлтый ≈ 245). Картинку сначала увеличиваем вчетверо, чуть
размываем и пропускаем через S-кривую — края получаются гладкими, как у векторного (в исходнике шум JPEG).
Запуск:  python scripts/make_logo.py   (нужны Pillow и numpy). Исходник — bot/assets/ishdasiz_logo_original.png.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

ASSETS = Path(__file__).resolve().parent.parent / "bot" / "assets"
SCALE = 4
YELLOW = (255, 212, 0)
WHITE = (244, 244, 244)
NAVY = (13, 27, 66)


def _ink_mask(rgb: Image.Image) -> np.ndarray:
    big = rgb.resize((rgb.width * SCALE, rgb.height * SCALE), Image.LANCZOS)
    red = np.asarray(big, dtype=np.float32)[..., 0]
    ink = np.clip((245.0 - red) / (245.0 - 45.0), 0.0, 1.0)
    blurred = np.asarray(Image.fromarray((ink * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(SCALE * 0.45)), dtype=np.float32) / 255.0
    t = np.clip((blurred - 0.30) / (0.70 - 0.30), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def main() -> None:
    source = ASSETS / "ishdasiz_logo_original.png"
    if not source.exists():                                   # первый запуск: сохраняем оригинал рядом
        (ASSETS / "ishdasiz_logo.png").replace(source)
    rgb = Image.open(source).convert("RGB")
    alpha = _ink_mask(rgb)
    h, w = alpha.shape
    yy, xx = np.mgrid[0:h, 0:w]
    circle = xx < 0.225 * w                                   # круг с самолётиком слева
    tagline = (~circle) & (yy > 0.60 * h)                     # нижняя строка — слоган
    for name, paint in (("light", {"circle": YELLOW, "word": YELLOW, "tag": WHITE}), ("navy", {"circle": NAVY, "word": NAVY, "tag": NAVY})):
        out = np.zeros((h, w, 4), dtype=np.uint8)
        for region, key in ((circle, "circle"), (tagline, "tag"), ((~circle) & (~tagline), "word")):
            out[region, :3] = paint[key]
        out[..., 3] = (alpha * 255).astype(np.uint8)
        Image.fromarray(out, "RGBA").save(ASSETS / f"ishdasiz_logo_{name}.png", optimize=True)
        print("saved", ASSETS / f"ishdasiz_logo_{name}.png", out.shape)


if __name__ == "__main__":
    main()
