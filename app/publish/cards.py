"""把她写的文字卡片排成图文作品的图片（竖版 3:4，1080 × 1440）。

粉色 Q 版风格：渐变背景 + 波点 + 白色圆角卡片 + 站酷快乐体，底部署名是你上传的头像（没有就用默认头像）+ 你设置的名字。
也可以换成你自己上传的背景图：文字写在半透明的白色卡片上（默认），或者直接写在背景图上（自动按背景深浅选文字颜色并加描边）；
用自定义背景时底部不加署名和声明小字（发布时勾选抖音的“内容由AI生成”声明）。
"""
from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageStat

from . import fonts

W, H = 1080, 1440
SS = 2  # 先按 2 倍画再缩小，边缘更顺滑

PALETTES = [
    {"bg": ((255, 228, 238), (255, 246, 250)), "accent": (214, 51, 108), "text": (92, 52, 70), "dot": (255, 182, 208)},
    {"bg": ((228, 240, 255), (246, 250, 255)), "accent": (64, 112, 220), "text": (52, 64, 96), "dot": (176, 204, 255)},
    {"bg": ((255, 240, 222), (255, 250, 242)), "accent": (226, 112, 52), "text": (96, 64, 44), "dot": (255, 204, 160)},
    {"bg": ((236, 228, 255), (249, 246, 255)), "accent": (132, 88, 220), "text": (72, 56, 104), "dot": (208, 190, 255)},
]


def _heart(draw: ImageDraw.ImageDraw, cx: float, cy: float, s: float, fill) -> None:
    pts = []
    for i in range(60):
        t = math.pi * 2 * i / 60
        x = 16 * math.sin(t) ** 3
        y = -(13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t))
        pts.append((cx + x * s / 16, cy + y * s / 16))
    draw.polygon(pts, fill=fill)


def _star(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, fill, n: int = 5) -> None:
    pts = []
    for i in range(n * 2):
        a = -math.pi / 2 + i * math.pi / n
        rr = r if i % 2 == 0 else r * 0.45
        pts.append((cx + math.cos(a) * rr, cy + math.sin(a) * rr))
    draw.polygon(pts, fill=fill)


def _background(pal: dict, seed: int) -> Image.Image:
    w, h = W * SS, H * SS
    top, bot = pal["bg"]
    grad = Image.new("RGB", (1, 256))
    for y in range(256):
        k = y / 255
        grad.putpixel((0, y), tuple(int(top[i] + (bot[i] - top[i]) * k) for i in range(3)))
    img = grad.resize((w, h)).convert("RGBA")
    d = ImageDraw.Draw(img)
    rnd = random.Random(seed)
    for _ in range(46):
        x, y, r = rnd.uniform(0, w), rnd.uniform(0, h), rnd.uniform(6, 16) * SS
        d.ellipse((x - r, y - r, x + r, y + r), fill=pal["dot"] + (120,))
    for _ in range(6):
        _heart(d, rnd.uniform(0, w), rnd.uniform(0, h), rnd.uniform(18, 34) * SS, pal["dot"] + (170,))
    return img


def _avatar_disc(avatar: Image.Image | None, size: int, pal: dict) -> Image.Image:
    disc = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    if avatar is not None:
        a = avatar.convert("RGBA")
        side = min(a.size)
        a = a.crop(((a.width - side) // 2, (a.height - side) // 2, (a.width + side) // 2, (a.height + side) // 2))
        a = a.resize((size, size), Image.LANCZOS)
        base = Image.new("RGBA", (size, size), (255, 255, 255, 255))  # 透明头像（比如默认头像）垫白底
        base.alpha_composite(a)
        disc.paste(base, (0, 0), mask)
    else:
        ImageDraw.Draw(disc).ellipse((0, 0, size - 1, size - 1), fill=pal["dot"] + (255,))
    ring = ImageDraw.Draw(disc)
    ring.ellipse((0, 0, size - 1, size - 1), outline=(255, 255, 255, 255), width=max(2, size // 14))
    return disc


def fit_background(bg: Image.Image, w: int = W, h: int = H) -> Image.Image:
    """把你的背景图铺满 3:4 画面（等比放大后居中裁掉多出来的部分，不会变形）。"""
    bg = ImageOps.exif_transpose(bg)
    return ImageOps.fit(bg.convert("RGB"), (w, h), Image.LANCZOS, centering=(0.5, 0.5)).convert("RGBA")


def _is_dark(img: Image.Image, box) -> bool:
    """这块区域整体偏暗吗（决定直接写在背景上时用白字还是深色字）。"""
    region = img.crop(tuple(int(v) for v in box)).convert("L")
    return ImageStat.Stat(region).mean[0] < 140


def render_card(card: dict, idx: int, total: int, name: str, avatar: Image.Image | None = None,
                cover_text: str = "", palette: int = 0, bg: Image.Image | None = None,
                panel_on: bool = True) -> Image.Image:
    """画一张卡片。card = {title, lines}；cover_text 不为空时画成封面（大标题 + 一句话）。

    bg：你上传的背景图（None 用内置的粉色波点背景）；panel_on=False 时文字直接写在背景图上。"""
    pal = dict(PALETTES[palette % len(PALETTES)])
    s = SS
    img = fit_background(bg, W * s, H * s) if bg is not None else _background(pal, seed=idx * 31 + palette)
    d = ImageDraw.Draw(img)
    m = 70 * s
    panel = (m, 150 * s, W * s - m, H * s - 170 * s)
    stroke, stroke_fill = 0, None
    if bg is None or panel_on:
        shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle((panel[0], panel[1] + 14 * s, panel[2], panel[3] + 14 * s),
                                                 radius=56 * s, fill=pal["accent"] + (50,))
        img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14 * s)))
        # 自定义背景时卡片稍微透一点，能看到底下的背景
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).rounded_rectangle(panel, radius=56 * s, fill=(255, 255, 255, 240 if bg is None else 214),
                                                outline=pal["dot"] + (255,), width=6 * s)
        img.alpha_composite(layer)
    else:
        # 文字直接写在背景图上：按背景深浅换字的颜色，再加一圈描边，保证看得清
        if _is_dark(img, panel):
            pal["text"], pal["accent"] = (255, 255, 255), (255, 226, 238)
            stroke, stroke_fill = 4 * s, (40, 24, 32, 230)
        else:
            pal["text"] = (52, 36, 44)
            stroke, stroke_fill = 5 * s, (255, 255, 255, 235)

    # 顶部小标签
    tag = f"{name}的小分享 · {idx + 1}/{total}"
    tw = fonts.text_width(tag, 40 * s)
    tx0 = (W * s - tw) / 2 - 34 * s
    tag_fill = PALETTES[palette % len(PALETTES)]["accent"]  # 顶部小标签不跟着换色：白字在深色底上永远看得清
    d.rounded_rectangle((tx0, 70 * s, tx0 + tw + 68 * s, 132 * s), radius=31 * s, fill=tag_fill + (255,))
    fonts.draw_text(d, (tx0 + 34 * s, 78 * s), tag, 40 * s, (255, 255, 255, 255))
    _star(d, panel[0] + 46 * s, panel[1] + 48 * s, 22 * s, (255, 214, 90, 255))
    _heart(d, panel[2] - 52 * s, panel[1] + 52 * s, 26 * s, pal["accent"] + (255,))

    inner_w = panel[2] - panel[0] - 120 * s
    ops = []  # (类型, 内容, 字号, 行高)：先排版算总高度，再在卡片里上下居中
    if cover_text:
        tsize, bsize = 96 * s, 56 * s
        for ln in fonts.wrap(card.get("title") or "", tsize, inner_w)[:4]:
            ops.append(("title_c", ln, tsize, int(tsize * 1.3)))
        ops.append(("rule", "", 0, 100 * s))
        for ln in fonts.wrap(cover_text, bsize, inner_w)[:7]:
            ops.append(("body_c", ln, bsize, int(bsize * 1.55)))
    else:
        tsize, bsize = 80 * s, 62 * s
        for ln in fonts.wrap(card.get("title") or "", tsize, inner_w)[:3]:
            ops.append(("title", ln, tsize, int(tsize * 1.3)))
        ops.append(("gap", "", 0, 40 * s))
        for line in (card.get("lines") or [])[:6]:
            for j, ln in enumerate(fonts.wrap(line, bsize, inner_w - 70 * s)[:3]):
                ops.append(("bullet" if j == 0 else "body", ln, bsize, int(bsize * 1.5)))
            ops.append(("gap", "", 0, 20 * s))
    total = sum(o[3] for o in ops)
    y = panel[1] + max(100 * s, (panel[3] - panel[1] - total) / 2 - 10 * s)
    for kind, ln, size, lh in ops:
        if kind == "title_c" or kind == "body_c":
            lw = fonts.text_width(ln, size)
            color = pal["accent"] + (255,) if kind == "title_c" else pal["text"] + (255,)
            fonts.draw_text(d, ((W * s - lw) / 2, y), ln, size, color,
                            stroke_width=stroke or (4 * s if kind == "title_c" else 0),
                            stroke_fill=stroke_fill or (255, 255, 255, 255))
        elif kind == "rule":
            d.line((panel[0] + 160 * s, y + lh / 2 - 10 * s, panel[2] - 160 * s, y + lh / 2 - 10 * s),
                   fill=pal["dot"] + (255,), width=5 * s)
        elif kind == "title":
            fonts.draw_text(d, (panel[0] + 60 * s, y), ln, size, pal["accent"] + (255,),
                            stroke_width=stroke, stroke_fill=stroke_fill)
        elif kind in ("bullet", "body"):
            if kind == "bullet":
                _heart(d, panel[0] + 82 * s, y + size * 0.62, 20 * s, pal["accent"] + (220,))
            fonts.draw_text(d, (panel[0] + 130 * s, y), ln, size, pal["text"] + (255,),
                            stroke_width=stroke, stroke_fill=stroke_fill)
        y += lh

    # 底部署名：头像 + 你设置的名字
    if bg is None:
        # 内置粉色样式才画署名；用你上传的背景时不画（AI声明靠发布时勾选抖音的“内容由AI生成”）
        av = _avatar_disc(avatar, 96 * s, pal)
        by = H * s - 140 * s
        img.alpha_composite(av, (int(m + 10 * s), int(by)))
        fonts.draw_text(d, (m + 126 * s, by + 22 * s), name, 46 * s, pal["accent"] + (255,))
    return img.resize((W, H), Image.LANCZOS).convert("RGB")


def render_post(title: str, text: str, cards: list[dict], name: str, out_dir: Path,
                avatar: Image.Image | None = None, palette: int | None = None, prefix: str = "card",
                bg_path: Path | None = None, panel_on: bool = True) -> list[str]:
    """封面 + 每张文字卡片各一张 JPG，保存到 out_dir，返回文件名列表。bg_path：自定义背景图。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    bg = None
    if bg_path is not None and Path(bg_path).exists():
        try:
            bg = Image.open(bg_path)
            bg.load()
        except OSError:
            bg = None
    pal = random.randrange(len(PALETTES)) if palette is None else palette
    first = (text or "").replace("\n", " ").strip()
    cut = max(first.find("。"), first.find("！"), first.find("～"))
    cover_line = first[: cut + 1] if 8 <= cut <= 60 else first[:48]
    pages = [({"title": title or "今日分享"}, cover_line)] + [(c, "") for c in (cards or [])[:4]]
    names = []
    for i, (c, cover) in enumerate(pages):
        img = render_card(c, i, len(pages), name, avatar, cover_text=cover or (" " if i == 0 else ""), palette=pal,
                          bg=bg, panel_on=panel_on)
        fn = f"{prefix}_{i + 1}.jpg"
        img.save(out_dir / fn, quality=92)
        names.append(fn)
    return names
