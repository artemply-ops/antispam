"""Аватар бота из картинки канала: градиентная карта в холодной палитре + подпись.
python assets/make_avatar.py путь_к_картинке_канала"""
import os
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

SRC = sys.argv[1]
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "avatar.png")
SIZE = 640
FONTS = r"C:\Windows\Fonts"

# Тёмное → светлое: ночной синий, фиолетовый, бирюза, почти белый
STOPS = [(0, (8, 12, 38)), (70, (40, 30, 120)), (125, (92, 60, 230)), (185, (30, 215, 200)), (255, (235, 255, 250))]


def lut():
    out = []
    for v in range(256):
        for (a, ca), (b, cb) in zip(STOPS, STOPS[1:]):
            if a <= v <= b:
                t = (v - a) / (b - a)
                out.append(tuple(round(ca[i] + (cb[i] - ca[i]) * t) for i in range(3)))
                break
    return out


img = Image.open(SRC).convert("RGB").resize((SIZE, SIZE), Image.LANCZOS)
gray = ImageOps.autocontrast(img.convert("L"), cutoff=1)
table = lut()
toned = Image.merge("RGB", [gray.point([c[i] for c in table]) for i in range(3)])

# Нижняя плашка с подписью, в пределах круга (Telegram обрезает аватар кругом)
overlay = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(overlay)
fade = Image.linear_gradient("L").resize((SIZE, 260))
dark = Image.new("RGBA", (SIZE, 260), (6, 8, 30, 255))
dark.putalpha(fade.point(lambda v: int(v * 0.92)))
overlay.paste(dark, (0, SIZE - 260), dark)

big = ImageFont.truetype(os.path.join(FONTS, "impact.ttf"), 92)
small = ImageFont.truetype(os.path.join(FONTS, "segoeuib.ttf"), 40)


def centered(text, font, y, fill, glow=None):
    w = d.textlength(text, font=font)
    x = (SIZE - w) / 2
    if glow:
        g = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
        ImageDraw.Draw(g).text((x, y), text, font=font, fill=glow)
        overlay.alpha_composite(g.filter(ImageFilter.GaussianBlur(8)))
    d.text((x, y), text, font=font, fill=fill)


centered("ANTISPAM", big, 440, (235, 255, 250), glow=(30, 215, 200, 255))
d.rounded_rectangle((215, 552, 425, 556), radius=2, fill=(30, 215, 200))
centered("За гранью", small, 558, (190, 175, 255))

result = Image.alpha_composite(toned.convert("RGBA"), overlay).convert("RGB")
result.save(OUT, optimize=True)

# Превью, как увидят в Telegram (круг)
mask = Image.new("L", (SIZE, SIZE), 0)
ImageDraw.Draw(mask).ellipse((0, 0, SIZE, SIZE), fill=255)
preview = Image.new("RGB", (SIZE, SIZE), (255, 255, 255))
preview.paste(result, (0, 0), mask)
preview.save(OUT.replace(".png", "_circle_preview.png"))
print(OUT)
