"""画图用的字体：中文用站酷快乐体（可爱），它没有的字（颜文字里的符号等）换成系统字体。"""
from __future__ import annotations


from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

from ..paths import FONT_FILE

_FALLBACKS = [
    r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\seguisym.ttf", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]


@lru_cache(maxsize=32)
def cute(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(FONT_FILE), size)
    except OSError:
        return plain(size)


@lru_cache(maxsize=32)
def plain(size: int) -> ImageFont.FreeTypeFont:
    for p in _FALLBACKS:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size)
    except TypeError:  # 老版本 Pillow
        return ImageFont.load_default()


def is_emoji(ch: str) -> bool:
    o = ord(ch)
    return o >= 0x1F000 or 0x2600 <= o <= 0x27BF or o in (0xFE0F, 0x200D)


def cute_covers(ch: str) -> bool:
    """站酷快乐体大概率有的字：常用汉字、ASCII、中文标点、全角字符。"""
    o = ord(ch)
    return (o < 0x80 or 0x4E00 <= o <= 0x9FFF or 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF
            or o in (0x2026, 0x2014, 0x201C, 0x201D, 0x2018, 0x2019, 0x00B7))


def strip_emoji(text: str) -> str:
    return "".join(c for c in text if not is_emoji(c))


def runs(text: str, size: int):
    """把一行字按字体切成几段：[(字, 字体)]，相邻同字体的字合在一起。"""
    out: list[tuple[str, ImageFont.FreeTypeFont]] = []
    for ch in strip_emoji(text):
        f = cute(size) if cute_covers(ch) else plain(size)
        if out and out[-1][1] is f:
            out[-1] = (out[-1][0] + ch, f)
        else:
            out.append((ch, f))
    return out


def text_width(text: str, size: int) -> float:
    return sum(f.getlength(t) for t, f in runs(text, size))


def draw_text(draw, xy, text: str, size: int, fill, stroke_width: int = 0, stroke_fill=None) -> float:
    """按字体分段画一行字，返回画完后的 x。"""
    x, y = xy
    for t, f in runs(text, size):
        draw.text((x, y), t, font=f, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill)
        x += f.getlength(t)
    return x


def wrap(text: str, size: int, max_w: float) -> list[str]:
    """按宽度折行；标点不放在行首。"""
    lines, cur = [], ""
    for ch in strip_emoji(text).replace("\r", ""):
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        nxt = cur + ch
        if cur and text_width(nxt, size) > max_w:
            if ch in "，。！？～…、）》」,.!?~":
                lines.append(nxt)
                cur = ""
                continue
            lines.append(cur)
            cur = ch
        else:
            cur = nxt
    if cur:
        lines.append(cur)
    return lines



