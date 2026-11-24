# -*- coding: utf-8 -*-
"""生成应用图标：圆角渐变方块 + 白色 R"""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

OUT = Path(__file__).resolve().parent
S = 1024
A = (59, 108, 246)    # #3b6cf6
B = (106, 76, 245)    # #6a4cf5

# 对角线渐变
grad = Image.new("RGB", (S, S))
d = ImageDraw.Draw(grad)
for i in range(S * 2):
    t = i / (S * 2 - 1)
    c = tuple(int(A[k] + (B[k] - A[k]) * t) for k in range(3))
    d.line([(i, 0), (0, i)], fill=c)

# 圆角遮罩
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.235), fill=255)

icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
icon.paste(grad, (0, 0), mask)

# 顶部高光（模糊后过渡自然，避免出现硬边弧线）
from PIL import ImageFilter
hl = Image.new("L", (S, S), 0)
ImageDraw.Draw(hl).ellipse([-S * 0.25, -S * 0.85, S * 1.25, S * 0.32], fill=46)
hl = hl.filter(ImageFilter.GaussianBlur(S * 0.06))
icon.paste(Image.new("RGBA", (S, S), (255, 255, 255, 255)), (0, 0), Image.composite(hl, Image.new("L", (S, S), 0), mask))

# 字母 R
font = None
for name in ("segoeuib.ttf", "seguisb.ttf", "arialbd.ttf", "calibrib.ttf"):
    try:
        font = ImageFont.truetype(f"C:/Windows/Fonts/{name}", int(S * 0.60))
        break
    except Exception:
        continue
if font is None:
    font = ImageFont.load_default()

layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
dl = ImageDraw.Draw(layer)
x0, y0, x1, y1 = dl.textbbox((0, 0), "R", font=font)
tx = (S - (x1 - x0)) / 2 - x0
ty = (S - (y1 - y0)) / 2 - y0
dl.text((tx, ty + int(S * 0.012)), "R", font=font, fill=(0, 0, 0, 60))
dl.text((tx, ty), "R", font=font, fill=(255, 255, 255, 255))
icon = Image.alpha_composite(icon, layer)

icon.save(OUT / "icon.png")
sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
icon.resize((256, 256), Image.LANCZOS).save(
    OUT / "icon.ico", sizes=[(s, s) for s in sizes])
print("生成:", OUT / "icon.png", OUT / "icon.ico", [s for s in sizes])
