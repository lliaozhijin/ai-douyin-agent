"""桌宠绘图的底层工具：2D 仿射变换（和 GDI+ 的 Translate/Rotate/Scale 用法一样）、贴图、预先画好的小特效。

坐标约定（和原来的 C# 桌宠一样）：逻辑坐标以脚底中心为原点，y 向下为正（往上是负数），
角度为正表示在屏幕上顺时针转。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter

from ..publish import fonts


class Mat:
    """2×3 仿射矩阵。translate/rotate/scale 都是“先做这个变换，再做原来的变换”（GDI+ 的 Prepend）。"""
    __slots__ = ("a", "b", "c", "d", "e", "f")

    def __init__(self, a=1.0, b=0.0, c=0.0, d=0.0, e=1.0, f=0.0):
        # x' = a*x + b*y + c ; y' = d*x + e*y + f
        self.a, self.b, self.c, self.d, self.e, self.f = a, b, c, d, e, f

    def mul(self, o: "Mat") -> "Mat":
        """self × o：先用 o 变换，再用 self 变换。"""
        return Mat(self.a * o.a + self.b * o.d, self.a * o.b + self.b * o.e, self.a * o.c + self.b * o.f + self.c,
                   self.d * o.a + self.e * o.d, self.d * o.b + self.e * o.e, self.d * o.c + self.e * o.f + self.f)

    def translate(self, x: float, y: float) -> "Mat":
        return self.mul(Mat(1, 0, x, 0, 1, y))

    def rotate(self, deg: float) -> "Mat":
        r = math.radians(deg)
        c, s = math.cos(r), math.sin(r)
        return self.mul(Mat(c, -s, 0, s, c, 0))

    def scale(self, sx: float, sy: float | None = None) -> "Mat":
        return self.mul(Mat(sx, 0, 0, 0, sx if sy is None else sy, 0))

    def about(self, px: float, py: float, deg: float = 0.0, sx: float = 1.0, sy: float = 1.0) -> "Mat":
        """绕点 (px, py) 旋转 / 缩放。"""
        m = self.translate(px, py)
        if deg:
            m = m.rotate(deg)
        if sx != 1.0 or sy != 1.0:
            m = m.scale(sx, sy)
        return m.translate(-px, -py)

    def apply(self, x: float, y: float) -> tuple[float, float]:
        return self.a * x + self.b * y + self.c, self.d * x + self.e * y + self.f

    def inverse(self) -> "Mat":
        det = self.a * self.e - self.b * self.d
        if abs(det) < 1e-12:
            det = 1e-12
        a, b, d, e = self.e / det, -self.b / det, -self.d / det, self.a / det
        return Mat(a, b, -(a * self.c + b * self.f), d, e, -(d * self.c + e * self.f))


@dataclass
class Sprite:
    """一张贴图：像素 (u, v) 对应逻辑坐标 ((u - ax) / res, (v - ay) / res)。"""
    img: Image.Image
    ax: float
    ay: float
    res: float
    _pm: Image.Image | None = field(default=None, repr=False)

    @property
    def premul(self) -> Image.Image:
        """预乘透明度的版本：旋转缩放时用它插值，边缘不会混进透明像素的黑色（不会有黑边）。"""
        if self._pm is None:
            self._pm = self.img.convert("RGBa")
        return self._pm


class Canvas:
    """一帧画面：设备像素大小 w×h；dev = 每个逻辑单位多少设备像素；(ox, oy) 是脚底在画面里的逻辑位置。"""

    def __init__(self, w_logical: float, h_logical: float, ox: float, oy: float, dev: float):
        self.dev = dev
        self.ox, self.oy = ox, oy
        self.w, self.h = max(1, int(math.ceil(w_logical * dev))), max(1, int(math.ceil(h_logical * dev)))
        self.img = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
        self.base = Mat(dev, 0, ox * dev, 0, dev, oy * dev)

    def blit(self, sp: Sprite | None, m: Mat, alpha: float = 1.0) -> None:
        if sp is None or alpha <= 0.004:
            return
        full = self.base.mul(m).mul(Mat(1 / sp.res, 0, -sp.ax / sp.res, 0, 1 / sp.res, -sp.ay / sp.res))
        w, h = sp.img.size
        pts = [full.apply(x, y) for x, y in ((0, 0), (w, 0), (0, h), (w, h))]
        x0 = max(0, int(math.floor(min(p[0] for p in pts))))
        y0 = max(0, int(math.floor(min(p[1] for p in pts))))
        x1 = min(self.w, int(math.ceil(max(p[0] for p in pts))))
        y1 = min(self.h, int(math.ceil(max(p[1] for p in pts))))
        if x1 <= x0 or y1 <= y0:
            return
        inv = full.inverse()
        data = (inv.a, inv.b, inv.a * x0 + inv.b * y0 + inv.c, inv.d, inv.e, inv.d * x0 + inv.e * y0 + inv.f)
        identity = (abs(inv.a - 1) < 1e-6 and abs(inv.e - 1) < 1e-6 and abs(inv.b) < 1e-6 and abs(inv.d) < 1e-6)
        if identity:
            ix, iy = int(round(data[2])), int(round(data[5]))
            patch = sp.img.crop((ix, iy, ix + x1 - x0, iy + y1 - y0))
        else:
            patch = sp.premul.transform((x1 - x0, y1 - y0), Image.AFFINE, data,
                                        resample=Image.BILINEAR).convert("RGBA")
        if alpha < 0.996:
            a = patch.getchannel("A").point(lambda v: int(v * alpha))
            patch.putalpha(a)
        self.img.alpha_composite(patch, (x0, y0))

    def clear_below(self, y_logical: float) -> None:
        """把逻辑坐标 y 以下的部分清空（躲在任务栏后面时，任务栏以下的身体不画）。"""
        y = int(round((self.oy + y_logical) * self.dev))
        if y < self.h:
            self.img.paste((0, 0, 0, 0), (0, max(0, y), self.w, self.h))


# ---------- 预先画好的小贴图（爱心、星星、音符、手机……） ----------
SS = 4  # 先放大 4 倍画，再缩小，边缘更顺滑


def _canvas(w: float, h: float, res: float) -> tuple[Image.Image, ImageDraw.ImageDraw, float]:
    k = res * SS
    img = Image.new("RGBA", (max(1, int(w * k)), max(1, int(h * k))), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img), k


def _over(img: Image.Image, draw_fn) -> None:
    """画半透明的东西（高光、波点）：先画在一张透明图层上再叠上去。

    直接用 ImageDraw 在透明图上画半透明颜色会“替换”掉下面的像素（变成半透明），
    放到桌面上就成了能看到背后的黑边 / 灰条；叠图层才是真正的混合。"""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(layer))
    img.alpha_composite(layer)


def _done(img: Image.Image, ax: float, ay: float, res: float) -> Sprite:
    w, h = max(1, img.width // SS), max(1, img.height // SS)
    return Sprite(img.resize((w, h), Image.LANCZOS), ax * res, ay * res, res)


def heart_points(cx: float, cy: float, s: float, n: int = 48) -> list[tuple[float, float]]:
    pts = []
    for i in range(n):
        t = math.pi * 2 * i / n
        x = 16 * math.sin(t) ** 3
        y = -(13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t))
        pts.append((cx + x * s / 16, cy + y * s / 16 + s * 0.1))
    return pts


def star_points(cx: float, cy: float, r: float, ir: float, n: int, rot: float = 0.0) -> list[tuple[float, float]]:
    pts = []
    for i in range(n * 2):
        a = math.radians(rot) - math.pi / 2 + i * math.pi / n
        rr = r if i % 2 == 0 else ir
        pts.append((cx + math.cos(a) * rr, cy + math.sin(a) * rr))
    return pts


@lru_cache(maxsize=64)
def heart(res: float, color=(255, 96, 146)) -> Sprite:
    """大小 10（逻辑单位）的爱心，中心为锚点；白色描边 + 高光。"""
    s = 10.0
    img, d, k = _canvas(28, 28, res)
    c = 14 * k
    d.polygon(heart_points(c, c, (s + 1.6) * k), fill=(255, 255, 255, 255))
    d.polygon(heart_points(c, c, s * k), fill=color + (255,))
    _over(img, lambda o: o.ellipse((c - s * 0.62 * k, c - s * 0.5 * k, c - s * 0.2 * k, c - s * 0.18 * k),
                                   fill=(255, 255, 255, 190)))
    return _done(img, 14, 14, res)


@lru_cache(maxsize=64)
def star(res: float, color=(255, 222, 90), n: int = 5, inner: float = 0.5, outline: bool = True) -> Sprite:
    r = 10.0
    img, d, k = _canvas(26, 26, res)
    c = 13 * k
    if outline:
        d.polygon(star_points(c, c, (r + 1.6) * k, (r * inner + 1.4) * k, n), fill=(255, 255, 255, 255))
    d.polygon(star_points(c, c, r * k, r * inner * k, n), fill=color + (255,))
    return _done(img, 13, 13, res)


@lru_cache(maxsize=16)
def drop(res: float) -> Sprite:
    s = 10.0
    img, d, k = _canvas(16, 22, res)
    cx, cy = 8 * k, 12 * k
    pts = [(cx, cy - s * k)]
    for i in range(0, 181, 10):
        a = math.radians(i)
        pts.append((cx + math.cos(a) * 0.6 * s * k, cy + 0.25 * s * k + math.sin(a) * 0.6 * s * k))
    d.polygon(pts, fill=(140, 202, 255, 220), outline=(90, 160, 230, 255))
    _over(img, lambda o: o.ellipse((cx - 0.3 * s * k, cy - 0.1 * s * k, cx - 0.02 * s * k, cy + 0.18 * s * k),
                                   fill=(255, 255, 255, 190)))
    return _done(img, 8, 12, res)


@lru_cache(maxsize=16)
def puff(res: float) -> Sprite:
    s = 10.0
    img, d, k = _canvas(30, 26, res)
    cx, cy = 15 * k, 14 * k
    blobs = ((0, 0, 1), (-0.7, 0.2, 0.75), (0.7, 0.2, 0.7), (0, -0.45, 0.7))
    for bx, by, br in blobs:
        r = s * br * k
        d.ellipse((cx + bx * s * k - r - k, cy + by * s * k - r - k, cx + bx * s * k + r + k, cy + by * s * k + r + k),
                  fill=(225, 215, 230, 110))
    def inner(o):
        for bx, by, br in blobs:
            r = s * br * k
            o.ellipse((cx + bx * s * k - r, cy + by * s * k - r, cx + bx * s * k + r, cy + by * s * k + r),
                      fill=(255, 255, 255, 200))
    _over(img, inner)
    return _done(img, 15, 14, res)


@lru_cache(maxsize=16)
def note(res: float, color=(255, 120, 170)) -> Sprite:
    """音符，大小 1 = 原版的 1 倍（约 16 个逻辑单位高）。"""
    img, d, k = _canvas(20, 26, res)
    ox, oy = 7 * k, 19 * k

    def P(x, y):
        return ox + x * k, oy + y * k
    for col, w in (((255, 255, 255, 255), 4.0), (color + (255,), 1.8)):
        d.ellipse((*P(-5 - (w - 1.8) / 2, -2 - (w - 1.8) / 2), *P(3 + (w - 1.8) / 2, 4 + (w - 1.8) / 2)), fill=col)
        d.line((*P(2.5, 1), *P(2.5, -14)), fill=col, width=int(w * k))
        d.line((*P(2.5, -14), *P(7, -11), *P(7, -5)), fill=col, width=int(w * k), joint="curve")
    return _done(img, 7, 19, res)


@lru_cache(maxsize=32)
def glyph(res: float, ch: str, color=(255, 110, 150), size: float = 26.0) -> Sprite:
    """可爱字体的单个字（“!” “?” “Z”），白色描边，中心为锚点。"""
    img, d, k = _canvas(size * 2, size * 2, res)
    f = fonts.cute(max(4, int(size * k)))
    sw = max(1, int(size * k * 0.12))
    bbox = d.textbbox((0, 0), ch, font=f, stroke_width=sw)
    x = (img.width - (bbox[2] - bbox[0])) / 2 - bbox[0]
    y = (img.height - (bbox[3] - bbox[1])) / 2 - bbox[1]
    d.text((x, y), ch, font=f, fill=color + (255,), stroke_width=sw, stroke_fill=(255, 255, 255, 255))
    return _done(img, size, size, res)


@lru_cache(maxsize=8)
def anger(res: float) -> Sprite:
    """生气的“井”字青筋。"""
    img, d, k = _canvas(28, 28, res)
    c = 14 * k
    for col, w in (((255, 255, 255, 255), 6.0), ((236, 70, 92, 240), 3.2)):
        for i in range(4):
            a = math.radians(90 * i)
            ca, sa = math.cos(a), math.sin(a)

            def R(x, y):
                return c + (x * ca - y * sa) * k, c + (x * sa + y * ca) * k
            d.line((*R(3, 10), *R(3, 6), *R(6, 3), *R(10, 3)), fill=col, width=int(w * k), joint="curve")
    return _done(img, 14, 14, res)


@lru_cache(maxsize=8)
def phone(res: float) -> Sprite:
    """她拿着的手机（我们看到的是手机背面：粉色手机壳 + 摄像头 + 小爱心贴纸）。中心为锚点，约 26×44。"""
    img, d, k = _canvas(34, 52, res)
    x0, y0, x1, y1 = 4 * k, 4 * k, 30 * k, 48 * k
    d.rounded_rectangle((x0 - k, y0 - k, x1 + k, y1 + k), radius=7 * k, fill=(255, 255, 255, 255))
    d.rounded_rectangle((x0, y0, x1, y1), radius=6 * k, fill=(255, 150, 190, 255), outline=(214, 90, 140, 255),
                        width=int(1.2 * k))
    d.rounded_rectangle((x0 + 3 * k, y0 + 3 * k, x0 + 12 * k, y0 + 13 * k), radius=3 * k, fill=(60, 50, 70, 255))
    d.ellipse((x0 + 4.5 * k, y0 + 4.5 * k, x0 + 7.5 * k, y0 + 7.5 * k), fill=(140, 150, 200, 255))
    d.ellipse((x0 + 7.5 * k, y0 + 8.5 * k, x0 + 10.5 * k, y0 + 11.5 * k), fill=(140, 150, 200, 255))
    d.polygon(heart_points(17 * k, 33 * k, 5.5 * k), fill=(255, 255, 255, 235))
    _over(img, lambda o: o.rounded_rectangle((x0 + 2 * k, y0 + 18 * k, x0 + 4 * k, y1 - 6 * k), radius=k,
                                             fill=(255, 255, 255, 90)))
    return _done(img, 17, 26, res)


@lru_cache(maxsize=8)
def chat_icon(res: float) -> Sprite:
    """聊天气泡小图标（回私信时冒出来）。"""
    img, d, k = _canvas(30, 26, res)
    d.rounded_rectangle((2 * k, 2 * k, 28 * k, 19 * k), radius=8 * k, fill=(255, 255, 255, 255))
    d.polygon(((8 * k, 18 * k), (6 * k, 25 * k), (14 * k, 18 * k)), fill=(255, 255, 255, 255))
    d.rounded_rectangle((4 * k, 4 * k, 26 * k, 17 * k), radius=6.5 * k, fill=(120, 170, 255, 255))
    d.polygon(((9 * k, 16 * k), (8 * k, 22 * k), (13 * k, 16 * k)), fill=(120, 170, 255, 255))
    for i in range(3):
        cx = (10 + i * 5) * k
        d.ellipse((cx - 1.4 * k, 10.5 * k - 1.4 * k, cx + 1.4 * k, 10.5 * k + 1.4 * k), fill=(255, 255, 255, 255))
    return _done(img, 15, 13, res)


@lru_cache(maxsize=16)
def sign(res: float, text: str = "想发作品！") -> Sprite:
    """举着的小木牌，锚点在木棍底部（手握的位置）。"""
    f = fonts.cute(int(17 * res * SS))
    tw = f.getlength(text) / (res * SS)
    bw = max(70.0, tw + 22)
    img, d, k = _canvas(bw + 8, 96, res)
    cx = (bw + 8) / 2
    d.rounded_rectangle(((cx - 2.5) * k, 34 * k, (cx + 2.5) * k, 94 * k), radius=2 * k, fill=(196, 140, 92, 255),
                        outline=(150, 100, 60, 255), width=int(k))
    d.rounded_rectangle((4 * k, 4 * k, (bw + 4) * k, 40 * k), radius=9 * k, fill=(255, 255, 255, 255))
    d.rounded_rectangle((6 * k, 6 * k, (bw + 2) * k, 38 * k), radius=8 * k, fill=(255, 214, 230, 255),
                        outline=(240, 110, 160, 255), width=int(2 * k))
    fonts.draw_text(d, ((cx - tw / 2) * k, 10 * k), text, int(17 * k), (214, 51, 108, 255),
                    stroke_width=int(1.5 * k), stroke_fill=(255, 255, 255, 255))
    d.polygon(heart_points((bw - 2) * k, 8 * k, 5 * k), fill=(255, 92, 142, 255))
    return _done(img, cx, 92, res)


@lru_cache(maxsize=8)
def shadow(res: float) -> Sprite:
    """脚下的软阴影：宽 120、高 15 的椭圆（锚点在中心）。"""
    img = Image.new("RGBA", (int(150 * res), int(30 * res)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((15 * res, 7.5 * res, 135 * res, 22.5 * res), fill=(40, 30, 10, 80))
    img = img.filter(ImageFilter.GaussianBlur(4 * res))
    return Sprite(img, 75 * res, 15 * res, res)


@lru_cache(maxsize=4)
def pat_hand(res: float) -> Sprite:
    """主人摸头的手（Q 版的手背 + 粉色针织袖口 + 白色蕾丝），手指朝左，锚点在手心。"""
    img, d, k = _canvas(130, 80, res)
    ox, oy = 50 * k, 40 * k
    skin, skin_sh, line = (250, 216, 196, 255), (226, 170, 146, 255), (190, 124, 104, 255)

    def P(x, y):
        return ox + x * k, oy + y * k
    d.ellipse((*P(-55, -18), *P(65, 40)), fill=(90, 50, 20, 40))
    fingers = ((-2, 16, 10, 27, 9.6), (-6, 5.5, 3, 33, 10.8), (-7, -5.5, -2, 36, 11.3), (-5, -16.5, -8, 32, 10.6))
    for kx, ky, tilt, ln, w in fingers:
        a = math.radians(180 - tilt)
        ex, ey = kx + math.cos(a) * ln, ky + math.sin(a) * ln
        d.line((*P(kx, ky), *P(ex, ey)), fill=line, width=int((w + 1.6) * k))
        d.line((*P(kx, ky), *P(ex, ey)), fill=skin, width=int(w * k))
        r = (w + 1.6) / 2
        d.ellipse((*P(ex - r, ey - r), *P(ex + r, ey + r)), fill=line)
        r = w / 2
        d.ellipse((*P(ex - r, ey - r), *P(ex + r, ey + r)), fill=skin)
        d.ellipse((*P(ex - r * 0.7, ey - r * 0.6), *P(ex + r * 0.1, ey + r * 0.4)), fill=(255, 236, 228, 255))
    d.rounded_rectangle((*P(-14, -26), *P(52, 26)), radius=22 * k, fill=line)
    d.rounded_rectangle((*P(-13, -25), *P(51, 25)), radius=21 * k, fill=skin)
    _over(img, lambda o: o.ellipse((*P(0, -20), *P(40, 0)), fill=(255, 240, 232, 200)))
    d.rounded_rectangle((*P(-2, 10), *P(50, 25)), radius=10 * k, fill=skin_sh)
    d.line((*P(22, 19), *P(40, 30)), fill=line, width=int(13.5 * k))
    d.line((*P(22, 19), *P(40, 30)), fill=skin, width=int(12 * k))
    d.rounded_rectangle((*P(44, -29), *P(74, 29)), radius=9 * k, fill=(240, 150, 188, 255),
                        outline=(214, 110, 150, 255), width=int(1.2 * k))
    _over(img, lambda o: [o.line((*P(x, -27), *P(x, 27)), fill=(255, 255, 255, 90), width=int(1.4 * k))
                          for x in range(49, 74, 5)])
    for i in range(9):
        y = -27 + i * 6.7
        d.ellipse((*P(39.5, y - 4), *P(47.5, y + 4)), fill=(255, 255, 255, 255), outline=(240, 170, 200, 255))
    return _done(img, 50, 40, res)


# ---------- 对话框 ----------
BUBBLE_FONT = 21.0


DEFAULT_BUBBLE = ("#FFE8F2", "#FF8CB9", "#DE4684")  # 底色、边框、文字


def hex_rgb(h: str, fallback=(255, 232, 242)) -> tuple:
    h = (h or "").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return fallback


def _mix(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


@lru_cache(maxsize=96)
def bubble(text: str, shown: int, res: float, max_w: float = 210.0,
           colors: tuple = DEFAULT_BUBBLE) -> tuple[Sprite, float, float]:
    """萌系对话框（文字逐字出现）。colors = (底色, 边框, 文字)，都是 #RRGGBB。
    返回 (贴图, 宽, 高)；贴图锚点在下面的小尾巴尖上。"""
    bg = hex_rgb(colors[0], (255, 232, 242))
    border = hex_rgb(colors[1], (255, 140, 185))
    ink = hex_rgb(colors[2], (222, 70, 132))
    tail_bg = _mix(bg, border, 0.08)
    light = (0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]) > 150  # 浅色底：白色高光、白色描边；深色底就淡一点
    shine = 110 if light else 26
    stroke = _mix(bg, (255, 255, 255), 0.6) if light else bg
    dots = _mix(bg, border, 0.45)
    lines = fonts.wrap(text, int(BUBBLE_FONT * 4), max_w * 4)  # 按 4 倍字号算折行，和画的时候一致
    size = BUBBLE_FONT
    lh = size * 1.3
    tw = max((fonts.text_width(ln, int(size * 4)) / 4 for ln in lines), default=20.0)
    bw, bh, r = tw + 36, len(lines) * lh + 22, 20.0
    pad = 8.0
    W, H = bw + pad * 2, bh + pad * 2 + 18
    img, d, k = _canvas(W, H, res)
    bx, by = pad, pad
    tipx, tipy = pad + 20, by + bh + 14

    def box(dx=0.0, dy=0.0, grow=0.0):
        return ((bx - grow + dx) * k, (by - grow + dy) * k, (bx + bw + grow + dx) * k, (by + bh + grow + dy) * k)

    def tail(grow=0.0, dy=0.0):
        return [((tipx + 12 + grow) * k, (by + bh - 1 + dy) * k), ((tipx - 1) * k, (tipy + grow + dy) * k),
                ((tipx - 8 - grow) * k, (by + bh - 1 + dy) * k)]
    d.rounded_rectangle(box(0, 0, 3.5), radius=(r + 3.5) * k, fill=border + (255,))
    d.polygon(tail(3.5), fill=border + (255,))
    d.rounded_rectangle(box(0, 0, 1.5), radius=(r + 1.5) * k, fill=(255, 255, 255, 255))
    d.polygon(tail(1.5), fill=(255, 255, 255, 255))
    d.rounded_rectangle(box(), radius=r * k, fill=bg + (255,))
    d.polygon(tail(0), fill=tail_bg + (255,))
    # 顶部果冻高光和波点：叠在不透明的粉色底上，整个对话框里面是完全不透明的，放在任何桌面上文字都清楚
    def deco(o):
        o.rounded_rectangle(((bx + 10) * k, (by + 4) * k, (bx + bw - 10) * k, (by + 4 + bh * 0.38) * k),
                            radius=12 * k, fill=(255, 255, 255, shine))
        for yy in range(int(by + bh * 0.55), int(by + bh - 4), 14):
            for xx in range(int(bx + 10 + ((yy // 14) % 2) * 11), int(bx + bw - 6), 22):
                o.ellipse((xx * k, yy * k, (xx + 4) * k, (yy + 4) * k), fill=dots + (90,))
    _over(img, deco)
    left = shown
    ty = by + 11
    fsz = int(size * k)
    for ln in lines:
        if left <= 0:
            break
        part = ln[:left]
        left -= len(ln)
        lw = fonts.text_width(ln, fsz) / k
        fonts.draw_text(d, ((bx + (bw - lw) / 2) * k, ty * k), part, fsz, ink + (255,),
                        stroke_width=int(2 * k), stroke_fill=stroke + (255,))
        ty += lh
    d.polygon(heart_points((bx + bw - 6) * k, (by + 2) * k, 8 * k), fill=(255, 255, 255, 255))
    d.polygon(heart_points((bx + bw - 6) * k, (by + 2) * k, 6.5 * k), fill=_mix(border, ink, 0.5) + (255,))
    d.polygon(star_points((bx + 8) * k, (by + 4) * k, 6.5 * k, 2.6 * k, 4), fill=(255, 214, 90, 255))
    return _done(img, tipx, tipy, res), bw, bh
