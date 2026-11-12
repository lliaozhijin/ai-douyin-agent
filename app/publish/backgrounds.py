"""文字卡片的内置可爱背景：全部是程序画出来的（不用别人的图片），第一次用到时画好放进背景图库。

每张 1080×1440。图案集中在四周、颜色偏浅，中间留给文字（写在半透明白卡片上，或者直接写在图上都看得清）。
放进背景图库以后和你上传的背景一样用：选中、设为默认、删除（删掉的内置背景不会自己再出现）。
"""
from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

W, H = 1080, 1440
SS = 2
PREFIX = "bg_cute_"       # 内置背景的文件名前缀（背景图库里的文件都以 bg_ 开头）


def _canvas(top: tuple, bottom: tuple) -> Image.Image:
    """上下渐变的底。"""
    w, h = W * SS, H * SS
    col = Image.new("RGB", (1, h))
    px = col.load()
    for y in range(h):
        t = y / (h - 1)
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return col.resize((w, h)).convert("RGBA")


def _done(img: Image.Image) -> Image.Image:
    return img.resize((W, H), Image.LANCZOS).convert("RGB")


def _edge(rnd: random.Random, n: int, margin: float = 0.2):
    """在四周（不在正中间）撒 n 个点：返回 (x, y)，单位是 2 倍画布的像素。"""
    w, h = W * SS, H * SS
    out = []
    tries = 0
    while len(out) < n and tries < n * 40:
        tries += 1
        x, y = rnd.uniform(0, w), rnd.uniform(0, h)
        inside = margin * w < x < (1 - margin) * w and margin * 0.9 * h < y < (1 - margin * 0.9) * h
        if inside and rnd.random() < 0.82:
            continue
        if all((x - a) ** 2 + (y - b) ** 2 > (w * 0.085) ** 2 for a, b in out):
            out.append((x, y))
    return out


def _heart(d, cx, cy, s, fill):
    d.ellipse((cx - s, cy - s * 0.9, cx, cy + s * 0.1), fill=fill)
    d.ellipse((cx, cy - s * 0.9, cx + s, cy + s * 0.1), fill=fill)
    d.polygon([(cx - s * 0.96, cy - s * 0.2), (cx + s * 0.96, cy - s * 0.2), (cx, cy + s)], fill=fill)


def _star(d, cx, cy, r, fill, rot=0.0):
    pts = []
    for i in range(10):
        a = -math.pi / 2 + rot + i * math.pi / 5
        rr = r if i % 2 == 0 else r * 0.46
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    d.polygon(pts, fill=fill)


def _spark(d, cx, cy, r, fill):
    k = r * 0.26
    d.polygon([(cx, cy - r), (cx + k, cy - k), (cx + r, cy), (cx + k, cy + k), (cx, cy + r), (cx - k, cy + k),
               (cx - r, cy), (cx - k, cy - k)], fill=fill)


def _cloud(d, cx, cy, s, fill):
    for dx, dy, r in ((-1.0, 0.15, 0.62), (-0.35, -0.3, 0.8), (0.45, -0.15, 0.7), (1.05, 0.2, 0.55), (0.1, 0.3, 0.7)):
        d.ellipse((cx + dx * s - r * s, cy + dy * s - r * s, cx + dx * s + r * s, cy + dy * s + r * s), fill=fill)
    d.rounded_rectangle((cx - 1.5 * s, cy + 0.2 * s, cx + 1.55 * s, cy + 0.9 * s), radius=0.35 * s, fill=fill)


def _flower(d, cx, cy, r, petal, core, rot=0.0, n=5):
    for i in range(n):
        a = rot + i * math.pi * 2 / n
        x, y = cx + math.cos(a) * r, cy + math.sin(a) * r
        d.ellipse((x - r * 0.72, y - r * 0.72, x + r * 0.72, y + r * 0.72), fill=petal)
    d.ellipse((cx - r * 0.5, cy - r * 0.5, cx + r * 0.5, cy + r * 0.5), fill=core)


def _layer(img):
    return Image.new("RGBA", img.size, (0, 0, 0, 0))


def _rot_paste(img: Image.Image, draw_fn, cx: float, cy: float, size: float, angle: float) -> None:
    """在一小块透明图上画好，转个角度再贴上去（给草莓、小熊、花瓣这些歪一点的图案用）。"""
    n = int(size * 2.6)
    tile = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(tile), n / 2, n / 2, size)
    tile = tile.rotate(angle, resample=Image.BICUBIC)
    img.alpha_composite(tile, (int(cx - n / 2), int(cy - n / 2)))


# ---------- 各种风格 ----------
def polka(seed=1, top=(255, 247, 222), bottom=(255, 236, 200), dot=(255, 255, 255, 190)):
    img = _canvas(top, bottom)
    d = ImageDraw.Draw(img, "RGBA")
    step, r = 120 * SS, 26 * SS
    for j, y in enumerate(range(-step, H * SS + step, step)):
        for x in range(-step, W * SS + step, step):
            xx = x + (step // 2 if j % 2 else 0)
            d.ellipse((xx - r, y - r, xx + r, y + r), fill=dot)
    return _done(img)


def gingham(base=(255, 250, 250), line=(255, 182, 203)):
    img = Image.new("RGBA", (W * SS, H * SS), base + (255,))
    lay = _layer(img)
    d = ImageDraw.Draw(lay)
    step = 90 * SS
    for x in range(0, W * SS, step * 2):
        d.rectangle((x, 0, x + step, H * SS), fill=line + (120,))
    img.alpha_composite(lay)
    lay = _layer(img)
    d = ImageDraw.Draw(lay)
    for y in range(0, H * SS, step * 2):
        d.rectangle((0, y, W * SS, y + step), fill=line + (120,))
    img.alpha_composite(lay)
    return _done(img)


def sky(seed=3):
    img = _canvas((176, 222, 255), (236, 247, 255))
    d = ImageDraw.Draw(img, "RGBA")
    rnd = random.Random(seed)
    for x, y in _edge(rnd, 9, 0.24):
        _cloud(d, x, y, rnd.uniform(44, 78) * SS, (255, 255, 255, 235))
    for x, y in _edge(rnd, 12, 0.2):
        _spark(d, x, y, rnd.uniform(10, 20) * SS, (255, 255, 255, 220))
    return _done(img)


def night(seed=4):
    img = _canvas((205, 196, 245), (240, 222, 250))
    d = ImageDraw.Draw(img, "RGBA")
    rnd = random.Random(seed)
    mx, my, mr = W * SS * 0.8, H * SS * 0.1, 86 * SS
    d.ellipse((mx - mr, my - mr, mx + mr, my + mr), fill=(255, 244, 190, 255))
    d.ellipse((mx - mr * 0.45, my - mr * 1.1, mx + mr * 1.35, my + mr * 0.7), fill=(209, 200, 246, 255))
    for x, y in _edge(rnd, 26, 0.2):
        if (x - mx) ** 2 + (y - my) ** 2 < (mr * 1.8) ** 2:
            continue
        (_star if rnd.random() < 0.55 else _spark)(d, x, y, rnd.uniform(14, 34) * SS, (255, 246, 196, 240))
    for x, y in _edge(rnd, 5, 0.26):
        _cloud(d, x, y, rnd.uniform(40, 60) * SS, (255, 255, 255, 120))
    return _done(img)


def hearts(seed=5):
    img = _canvas((255, 226, 236), (255, 240, 244))
    d = ImageDraw.Draw(img, "RGBA")
    rnd = random.Random(seed)
    cols = [(255, 143, 177, 220), (255, 183, 205, 230), (255, 255, 255, 235), (255, 110, 155, 200)]
    for x, y in _edge(rnd, 34, 0.2):
        _heart(d, x, y, rnd.uniform(20, 50) * SS, rnd.choice(cols))
    return _done(img)


def garden(seed=6):
    img = _canvas((232, 247, 226), (250, 252, 232))
    d = ImageDraw.Draw(img, "RGBA")
    rnd = random.Random(seed)
    petals = [(255, 170, 196, 245), (255, 255, 255, 250), (255, 214, 120, 245), (190, 170, 245, 240)]
    for x, y in _edge(rnd, 30, 0.2):
        p = rnd.choice(petals)
        _flower(d, x, y, rnd.uniform(18, 36) * SS, p, (255, 226, 110, 255) if p[1] != 214 else (255, 255, 255, 255),
                rot=rnd.uniform(0, 1.2))
    for x, y in _edge(rnd, 16, 0.18):
        r = rnd.uniform(8, 14) * SS
        d.ellipse((x - r, y - r * 0.55, x + r, y + r * 0.55), fill=(150, 205, 140, 200))
    return _done(img)


def rainbow():
    img = _canvas((255, 250, 236), (255, 243, 226))
    d = ImageDraw.Draw(img, "RGBA")
    cols = [(255, 160, 170), (255, 200, 150), (255, 232, 150), (176, 226, 176), (160, 206, 246), (200, 180, 240)]
    for (cx, cy, r0) in ((W * SS * 0.12, H * SS * 0.07, 330 * SS), (W * SS * 0.9, H * SS * 0.99, 380 * SS)):
        for i, c in enumerate(cols):
            r = r0 - i * 34 * SS
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=c + (255,))
        r = r0 - len(cols) * 34 * SS
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 248, 232, 255))
    rnd = random.Random(7)
    _cloud(d, W * SS * 0.31, H * SS * 0.1, 46 * SS, (255, 255, 255, 250))
    _cloud(d, W * SS * 0.66, H * SS * 0.95, 52 * SS, (255, 255, 255, 250))
    for x, y in _edge(rnd, 14, 0.2):
        _spark(d, x, y, rnd.uniform(12, 22) * SS, (255, 214, 120, 230))
    return _done(img)


def soda(seed=8):
    img = _canvas((255, 214, 196), (255, 240, 214))
    lay = _layer(img)
    d = ImageDraw.Draw(lay)
    rnd = random.Random(seed)
    for x, y in _edge(rnd, 40, 0.2):
        r = rnd.uniform(14, 62) * SS
        d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, 70), outline=(255, 255, 255, 210), width=3 * SS)
        d.ellipse((x - r * 0.55, y - r * 0.6, x - r * 0.25, y - r * 0.3), fill=(255, 255, 255, 220))
    img.alpha_composite(lay)
    return _done(img)


def sakura(seed=9):
    img = _canvas((255, 232, 240), (255, 246, 248))
    rnd = random.Random(seed)

    def petal(d, cx, cy, s):
        d.ellipse((cx - s * 0.55, cy - s, cx + s * 0.55, cy + s), fill=(255, 176, 200, 235))
        d.polygon([(cx - s * 0.2, cy - s * 1.02), (cx, cy - s * 0.7), (cx + s * 0.2, cy - s * 1.02)], fill=(255, 236, 242, 255))

    for x, y in _edge(rnd, 46, 0.2):
        _rot_paste(img, petal, x, y, rnd.uniform(16, 34) * SS, rnd.uniform(0, 360))
    d = ImageDraw.Draw(img, "RGBA")
    for x, y in _edge(rnd, 6, 0.24):
        _flower(d, x, y, rnd.uniform(26, 40) * SS, (255, 190, 210, 245), (255, 236, 150, 255), rot=rnd.uniform(0, 1))
    return _done(img)


def bears(seed=10):
    img = _canvas((250, 236, 214), (255, 245, 230))
    rnd = random.Random(seed)

    def bear(d, cx, cy, s):
        fur, inner, dark = (214, 164, 112, 255), (245, 214, 176, 255), (96, 66, 48, 255)
        for sx in (-1, 1):
            d.ellipse((cx + sx * s * 0.72 - s * 0.36, cy - s * 0.92, cx + sx * s * 0.72 + s * 0.36, cy - s * 0.2), fill=fur)
            d.ellipse((cx + sx * s * 0.72 - s * 0.19, cy - s * 0.76, cx + sx * s * 0.72 + s * 0.19, cy - s * 0.38), fill=inner)
        d.ellipse((cx - s, cy - s * 0.8, cx + s, cy + s * 0.9), fill=fur)
        d.ellipse((cx - s * 0.42, cy + s * 0.05, cx + s * 0.42, cy + s * 0.62), fill=inner)
        for sx in (-1, 1):
            d.ellipse((cx + sx * s * 0.42 - s * 0.1, cy - s * 0.2, cx + sx * s * 0.42 + s * 0.1, cy), fill=dark)
            d.ellipse((cx + sx * s * 0.66 - s * 0.16, cy + s * 0.12, cx + sx * s * 0.66 + s * 0.16, cy + s * 0.32),
                      fill=(255, 170, 170, 170))
        d.ellipse((cx - s * 0.13, cy + s * 0.14, cx + s * 0.13, cy + s * 0.32), fill=dark)

    for x, y in _edge(rnd, 13, 0.25):
        _rot_paste(img, bear, x, y, rnd.uniform(46, 70) * SS, rnd.uniform(-18, 18))
    d = ImageDraw.Draw(img, "RGBA")
    for x, y in _edge(rnd, 18, 0.2):
        _heart(d, x, y, rnd.uniform(10, 18) * SS, (255, 170, 170, 200))
    return _done(img)


def strawberry(seed=11):
    img = _canvas((255, 240, 240), (255, 250, 244))
    rnd = random.Random(seed)

    def berry(d, cx, cy, s):
        red = (255, 105, 125, 255)
        d.ellipse((cx - s * 0.85, cy - s * 0.7, cx + s * 0.85, cy + s * 0.5), fill=red)
        d.polygon([(cx - s * 0.8, cy + s * 0.05), (cx + s * 0.8, cy + s * 0.05), (cx, cy + s * 1.1)], fill=red)
        for ax in (-0.55, -0.2, 0.2, 0.55, 0.0):
            d.polygon([(cx + ax * s - s * 0.2, cy - s * 0.62), (cx + ax * s + s * 0.2, cy - s * 0.62),
                       (cx + ax * s * 1.5, cy - s * (1.02 if ax else 1.12))], fill=(120, 196, 120, 255))
        for sx, sy in ((-0.4, -0.2), (0.35, -0.25), (0.0, 0.1), (-0.3, 0.35), (0.35, 0.3), (0.0, 0.6)):
            d.ellipse((cx + sx * s - s * 0.06, cy + sy * s - s * 0.09, cx + sx * s + s * 0.06, cy + sy * s + s * 0.09),
                      fill=(255, 236, 190, 255))

    for x, y in _edge(rnd, 22, 0.22):
        _rot_paste(img, berry, x, y, rnd.uniform(34, 56) * SS, rnd.uniform(-28, 28))
    d = ImageDraw.Draw(img, "RGBA")
    for x, y in _edge(rnd, 22, 0.18):
        r = rnd.uniform(5, 10) * SS
        d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 190, 200, 200))
    return _done(img)


def candy():
    """斜条纹（海盐奶油）。"""
    w, h = W * SS, H * SS
    img = Image.new("RGBA", (w, h), (236, 247, 255, 255))
    d = ImageDraw.Draw(img, "RGBA")
    step = 150 * SS
    for i in range(-h, w + h, step):
        d.polygon([(i, 0), (i + step * 0.5, 0), (i + step * 0.5 - h, h), (i - h, h)], fill=(198, 228, 250, 255))
    rnd = random.Random(12)
    for x, y in _edge(rnd, 20, 0.2):
        _spark(d, x, y, rnd.uniform(12, 24) * SS, (255, 255, 255, 235))
    return _done(img)


def notebook():
    """方格便签纸 + 四角的胶带。"""
    w, h = W * SS, H * SS
    img = Image.new("RGBA", (w, h), (255, 252, 240, 255))
    d = ImageDraw.Draw(img, "RGBA")
    step = 60 * SS
    for x in range(0, w, step):
        d.line((x, 0, x, h), fill=(236, 224, 205, 255), width=2 * SS)
    for y in range(0, h, step):
        d.line((0, y, w, y), fill=(236, 224, 205, 255), width=2 * SS)

    def tape(d2, cx, cy, s):
        d2.rounded_rectangle((cx - s * 1.2, cy - s * 0.3, cx + s * 1.2, cy + s * 0.3), radius=s * 0.06, fill=(255, 190, 205, 215))
        for k in range(-4, 5):
            d2.line((cx + k * s * 0.26, cy - s * 0.3, cx + k * s * 0.26 + s * 0.14, cy + s * 0.3), fill=(255, 255, 255, 120),
                    width=max(2, int(s * 0.05)))

    for (x, y, a, c) in ((0.13, 0.07, 38, 0), (0.87, 0.07, -38, 1), (0.13, 0.93, -38, 1), (0.87, 0.93, 38, 0)):
        _rot_paste(img, tape, w * x, h * y, 130 * SS, a)
    d = ImageDraw.Draw(img, "RGBA")
    rnd = random.Random(13)
    for x, y in _edge(rnd, 10, 0.2):
        (_heart if rnd.random() < 0.5 else _star)(d, x, y, rnd.uniform(14, 24) * SS, rnd.choice(
            [(255, 160, 185, 230), (255, 208, 110, 235), (160, 205, 245, 230)]))
    return _done(img)


def sunset(seed=14):
    img = _canvas((255, 196, 190), (255, 232, 196))
    w, h = W * SS, H * SS
    glow = Image.new("L", (w, h), 0)
    ImageDraw.Draw(glow).ellipse((w * 0.5 - 300 * SS, h * 0.86 - 300 * SS, w * 0.5 + 300 * SS, h * 0.86 + 300 * SS), fill=210)
    img.paste(Image.new("RGBA", (w, h), (255, 246, 216, 255)), (0, 0), glow.filter(ImageFilter.GaussianBlur(40 * SS)))
    d = ImageDraw.Draw(img, "RGBA")
    rnd = random.Random(seed)
    for x, y in _edge(rnd, 7, 0.26):
        _cloud(d, x, y, rnd.uniform(46, 76) * SS, (255, 255, 255, 170))
    for x, y in _edge(rnd, 12, 0.2):
        _spark(d, x, y, rnd.uniform(10, 18) * SS, (255, 255, 255, 220))
    return _done(img)


BUILTIN = [
    ("polka", "奶油波点", lambda: polka()),
    ("gingham_pink", "草莓牛奶格子", lambda: gingham()),
    ("gingham_mint", "薄荷格子", lambda: gingham((248, 255, 251), (160, 224, 196))),
    ("sky", "云朵天空", sky),
    ("night", "星星月亮", night),
    ("hearts", "满屏爱心", hearts),
    ("garden", "小花园", garden),
    ("rainbow", "彩虹", rainbow),
    ("soda", "蜜桃汽水", soda),
    ("sakura", "樱花飘落", sakura),
    ("bears", "小熊饼干", bears),
    ("strawberry", "草莓", strawberry),
    ("candy", "海盐条纹", candy),
    ("notebook", "手帐便签", notebook),
    ("sunset", "落日橘子海", sunset),
    ("polka_lilac", "香芋波点", lambda: polka(2, (238, 230, 255), (226, 216, 250))),
]
LABELS = {PREFIX + k + ".jpg": label for k, label, _f in BUILTIN}


def is_builtin(name: str) -> bool:
    return str(name).startswith(PREFIX)


def ensure(folder: Path, removed: list[str] | None = None) -> int:
    """把还没有的内置背景画出来放进背景图库（你删掉过的不再放）。返回这次新画了几张。"""
    folder.mkdir(parents=True, exist_ok=True)
    removed = set(removed or [])
    made = 0
    for key, _label, fn in BUILTIN:
        name = PREFIX + key + ".jpg"
        if name in removed or (folder / name).exists():
            continue
        fn().save(folder / name, quality=90)
        made += 1
    return made
