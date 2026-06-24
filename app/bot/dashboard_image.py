"""Рендер дашборда КБЖУ картинкой (PNG) — пиксель-перфектные полоски на любой платформе.

Текстовые бары в <code> на Android разъезжаются (кириллица не моноширинна), поэтому для
прогресс-баров рисуем картинку. Подписи кириллицей (DejaVu из fonts-dejavu-core), бары — цветные.
"""
from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from app.domain.goals import Remaining

_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# цвет на макрос (под смысл эмодзи): ккал-оранжевый, белок-красный, жир-золотой, угл-коричневый
_COLORS = ((240, 138, 0), (214, 69, 65), (224, 168, 0), (150, 111, 51))


def render_dashboard(rem: Remaining) -> bytes:
    """Картинка 4 баров КБЖУ: подпись · потреблено/цель · полоска · %. PNG-байты."""
    c, g = rem.consumed, rem.goal
    rows = [
        ("Ккал", c.kcal, g.daily_kcal),
        ("Белок", c.protein, g.protein_g),
        ("Жир", c.fat, g.fat_g),
        ("Углеводы", c.carb, g.carb_g),
    ]
    width, row_h, pad = 720, 60, 22
    height = pad * 2 + row_h * len(rows)
    img = Image.new("RGB", (width, height), (255, 255, 255))
    d = ImageDraw.Draw(img)
    label_f = ImageFont.truetype(_FONT_BOLD, 27)
    num_f = ImageFont.truetype(_FONT, 23)
    bar_x, bar_w, bh = 360, 250, 22

    for i, (label, cons, goal) in enumerate(rows):
        cy = pad + i * row_h + row_h // 2
        pct = round(100 * cons / goal) if goal else 0
        nums = f"{round(cons)}/{round(goal)}"
        d.text((pad, cy), label, font=label_f, fill=(30, 30, 30), anchor="lm")
        d.text((190, cy), nums, font=num_f, fill=(95, 95, 95), anchor="lm")
        by = cy - bh // 2
        d.rounded_rectangle([bar_x, by, bar_x + bar_w, by + bh], radius=11, fill=(233, 233, 233))
        ratio = max(0.0, min(1.0, cons / goal)) if goal else 0.0
        if ratio > 0:
            fw = max(bh, round(bar_w * ratio))  # минимум — кружок, чтобы было видно
            d.rounded_rectangle([bar_x, by, bar_x + fw, by + bh], radius=11, fill=_COLORS[i])
        d.text((bar_x + bar_w + 14, cy), f"{pct}%", font=num_f, fill=(30, 30, 30), anchor="lm")

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
