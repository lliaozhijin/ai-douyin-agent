"""她自己的表情包：用桌宠的形象（默认形象或你上传的形象）摆出动作，加上小装饰和配字，画成一张可爱的表情图。

- 情绪（MOODS）决定她摆什么动作、周围画什么装饰（爱心、星星、眼泪、Zzz、问号……）、底色是什么；
- 配字是模型按当时聊的内容写的（2～6 个字），写在图的下方；
- 私信里发会动的 GIF（抖音私信支持 gif），评论里发不会动的 PNG（评论只支持图片）。
形象、动作都是软件自己的，不用别人的表情包。
"""
from __future__ import annotations

import math
import random
import re
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from ..pet import engine as E
from ..pet.render import render
from ..pet.rig import Rig, load as load_rig
from . import fonts

SIZE = 512          # PNG 边长
GIF_SIZE = 320      # GIF 边长（小一点，文件不会太大）
GIF_FRAMES = 10
GIF_STEP = 0.09     # 每帧相隔多少秒

# 情绪 → (动作, 从这个动作的第几秒开始, 装饰, 底色, 默认配字)
MOODS = {
    "开心": (E.CHEER, 0.5, "sparkle", (255, 236, 179), "好耶"),
    "比心": (E.WAVE, 0.6, "heart", (255, 214, 228), "比心"),
    "喜欢": (E.PHONE_LIKE, 0.6, "heart", (255, 205, 222), "喜欢"),
    "害羞": (E.IDLE, 1.0, "blush", (255, 224, 230), "害羞"),
    "谢谢": (E.WAVE, 1.0, "flower", (255, 232, 205), "谢谢你"),
    "你好": (E.WAVE, 0.4, "sparkle", (214, 236, 255), "嗨"),
    "加油": (E.DANCE2, 1.2, "star", (255, 226, 190), "加油"),
    "跳舞": (E.DANCE1, 1.0, "note", (226, 218, 255), "开心到跳舞"),
    "惊讶": (E.ALERT, 0.4, "bang", (255, 240, 200), "哇"),
    "疑惑": (E.LOOK, 0.8, "question", (220, 240, 232), "嗯？"),
    "生气": (E.SULK, 1.5, "anger", (255, 214, 205), "哼"),
    "委屈": (E.SIT, 2.5, "tear", (214, 228, 250), "委屈"),
    "困": (E.YAWN, 0.9, "zzz", (222, 222, 246), "困了"),
    "晚安": (E.SLEEP, 2.0, "zzz", (206, 212, 244), "晚安"),
    "摸鱼": (E.LIE, 2.5, "note", (216, 240, 224), "摸鱼中"),
    "在看": (E.PHONE, 1.5, "sparkle", (226, 240, 255), "在看在看"),
}
ALIASES = {"高兴": "开心", "兴奋": "开心", "哈哈": "开心", "笑": "开心", "爱心": "比心", "爱你": "比心", "点赞": "喜欢",
           "赞": "喜欢", "脸红": "害羞", "感谢": "谢谢", "嗨": "你好", "打招呼": "你好", "早安": "你好", "鼓励": "加油",
           "震惊": "惊讶", "哇": "惊讶", "问号": "疑惑", "好奇": "疑惑", "哼": "生气", "小脾气": "生气", "难过": "委屈",
           "哭": "委屈", "想哭": "委屈", "犯困": "困", "睡觉": "晚安", "无聊": "摸鱼", "躺平": "摸鱼", "吃瓜": "在看"}
TAG_RE = re.compile(r"[〔\[【（(]\s*表情包\s*[:：]\s*([^｜|〕\]】）)\s]{1,6})\s*(?:[｜|]\s*([^〕\]】）)]{0,12}))?\s*[〕\]】）)]")


def mood_names() -> list[str]:
    return list(MOODS)


def norm_mood(name: str) -> str:
    name = (name or "").strip()
    if name in MOODS:
        return name
    if name in ALIASES:
        return ALIASES[name]
    for k in list(MOODS) + list(ALIASES):
        if k and k in name:
            return ALIASES.get(k, k)
    return ""


def split_tag(text: str) -> tuple[str, dict | None]:
    """从她写的回复里取出“〔表情包：情绪｜配字〕”这个标记：返回 (去掉标记的回复, {mood, text} 或 None)。"""
    m = TAG_RE.search(text or "")
    if not m:
        return text, None
    clean = (text[:m.start()] + text[m.end():]).strip()
    mood = norm_mood(m.group(1))
    if not mood:
        return clean, None
    cap = re.sub(r"\s+", "", m.group(2) or "")[:8]
    return clean, {"mood": mood, "text": cap}


# ---------- 画 ----------
def _pose(rig: Rig, act: int, at: float) -> E.Pet:
    pet = E.Pet(fx=rig.fx, fy=rig.fy, peek_depth=rig.peek_depth, rnd=random.Random(7))
    pet.chatter = False
    pet.set_act(act, 1e9)
    for _ in range(int(at / 0.033)):
        _tick(pet, 0.033)
    return pet


def _tick(pet: E.Pet, dt: float) -> None:
    pet.act_t += dt
    pet.t += dt
    pet._smooth_arms(dt)


def _char(pet: E.Pet, rig: Rig, scale: float, blink: bool = False) -> Image.Image:
    pet.parts = []
    pet.face._blink_in = 99.0
    pet.last_pose = pet.pose()
    pet.face.update(pet, 0.033)
    if blink:
        pet.face.eyes = "closed"
    return render(pet, rig, scale).img


def _outline(char: Image.Image, width: int) -> Image.Image:
    """给人物加一圈白边（贴纸的感觉）和一点淡淡的影子。"""
    pad = width * 3
    big = Image.new("RGBA", (char.width + pad * 2, char.height + pad * 2), (0, 0, 0, 0))
    big.alpha_composite(char, (pad, pad))
    a = big.getchannel("A")
    edge = a.filter(ImageFilter.MaxFilter(width * 2 + 1)).filter(ImageFilter.GaussianBlur(0.8))
    out = Image.new("RGBA", big.size, (0, 0, 0, 0))
    shadow = Image.new("RGBA", big.size, (120, 70, 90, 0))
    shadow.putalpha(edge.filter(ImageFilter.GaussianBlur(width)).point(lambda v: int(v * 0.28)))
    out.alpha_composite(shadow, (0, max(2, width // 2)))
    white = Image.new("RGBA", big.size, (255, 255, 255, 0))
    white.putalpha(edge)
    out.alpha_composite(white)
    out.alpha_composite(big)
    return out


def _background(size: int, color: tuple, seed: int) -> Image.Image:
    """柔和的底色：中间亮一点，撒几颗小圆点。"""
    bg = Image.new("RGB", (size, size), color)
    glow = Image.new("L", (size, size), 0)
    ImageDraw.Draw(glow).ellipse((size * 0.08, size * 0.05, size * 0.92, size * 0.9), fill=150)
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.12))
    bg.paste(Image.new("RGB", (size, size), (255, 255, 255)), (0, 0), glow)
    d = ImageDraw.Draw(bg, "RGBA")
    rnd = random.Random(seed)
    for _ in range(14):
        x, y, r = rnd.uniform(0, size), rnd.uniform(0, size), rnd.uniform(size * 0.008, size * 0.022)
        d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, 150))
    return bg.convert("RGBA")


def _heart(d, cx, cy, s, fill):
    d.ellipse((cx - s, cy - s * 0.9, cx, cy + s * 0.1), fill=fill)
    d.ellipse((cx, cy - s * 0.9, cx + s, cy + s * 0.1), fill=fill)
    d.polygon([(cx - s * 0.96, cy - s * 0.2), (cx + s * 0.96, cy - s * 0.2), (cx, cy + s)], fill=fill)


def _spark(d, cx, cy, r, fill):
    """四角星（闪一下）。"""
    k = r * 0.28
    d.polygon([(cx, cy - r), (cx + k, cy - k), (cx + r, cy), (cx + k, cy + k), (cx, cy + r), (cx - k, cy + k),
               (cx - r, cy), (cx - k, cy - k)], fill=fill)


def _star(d, cx, cy, r, fill):
    pts = []
    for i in range(10):
        a = -math.pi / 2 + i * math.pi / 5
        rr = r if i % 2 == 0 else r * 0.45
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    d.polygon(pts, fill=fill)


def _drop(d, cx, cy, s, fill):
    d.polygon([(cx, cy - s * 1.3), (cx - s * 0.7, cy), (cx + s * 0.7, cy)], fill=fill)
    d.ellipse((cx - s * 0.75, cy - s * 0.55, cx + s * 0.75, cy + s * 0.85), fill=fill)


def _anger(d, cx, cy, s, fill, w):
    """生气的“井”字青筋：四段朝里弯的小弧。"""
    r = s * 0.36
    for (sx, sy), (st, en) in {(-1, -1): (0, 90), (1, -1): (90, 180), (1, 1): (180, 270), (-1, 1): (270, 360)}.items():
        ox, oy = cx + sx * s * 0.5, cy + sy * s * 0.5
        d.arc((ox - r, oy - r, ox + r, oy + r), start=st, end=en, fill=fill, width=w)


def _note(d, cx, cy, s, fill, double: bool = False):
    """音符（字体里没有 ♪，自己画）。"""
    w = max(2, int(s * 0.16))
    heads = [(cx - s * 0.25, cy + s * 0.45)] + ([(cx + s * 0.4, cy + s * 0.3)] if double else [])
    for hx, hy in heads:
        d.ellipse((hx - s * 0.26, hy - s * 0.19, hx + s * 0.26, hy + s * 0.19), fill=fill)
        d.line((hx + s * 0.22, hy, hx + s * 0.22, hy - s * 0.95), fill=fill, width=w)
    x0, y0 = heads[0][0] + s * 0.22, heads[0][1] - s * 0.95
    if double:
        d.line((x0, y0, heads[1][0] + s * 0.22, heads[1][1] - s * 0.95), fill=fill, width=int(w * 1.6))
    else:
        d.line((x0, y0, x0 + s * 0.35, y0 + s * 0.3), fill=fill, width=int(w * 1.3))


def _text(img: Image.Image, xy, text: str, size: int, fill, stroke: int, angle: float = 0.0) -> None:
    """带白边的字（可以歪一点）。"""
    w = int(fonts.text_width(text, size)) + stroke * 4 + 8
    h = int(size * 1.5) + stroke * 4
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    fonts.draw_text(ImageDraw.Draw(layer), (stroke * 2 + 2, stroke * 2), text, size, fill, stroke_width=stroke,
                    stroke_fill=(255, 255, 255, 255))
    if angle:
        layer = layer.rotate(angle, expand=True, resample=Image.BICUBIC)
    img.alpha_composite(layer, (int(xy[0] - layer.width / 2), int(xy[1] - layer.height / 2)))


def _decor(img: Image.Image, kind: str, box: tuple, k: float) -> None:
    """在人物头的两边画装饰。box 是人物在图里的位置；k 是 0～1 的动画进度（GIF 里装饰会轻轻晃）。"""
    S = img.width
    d = ImageDraw.Draw(img, "RGBA")
    x0, y0, x1, y1 = box
    hx0, hx1 = max(S * 0.1, x0 - S * 0.02), min(S * 0.9, x1 + S * 0.02)
    hy = y0 + (y1 - y0) * 0.2
    bob = math.sin(k * math.pi * 2) * S * 0.012
    pink, red, gold, blue, purple = (255, 105, 150, 255), (240, 70, 70, 255), (255, 190, 40, 255), \
        (90, 170, 245, 255), (150, 120, 235, 255)
    if kind == "heart":
        _heart(d, hx1, hy - S * 0.05 + bob, S * 0.055, pink)
        _heart(d, hx1 + S * 0.05, hy + S * 0.07 - bob, S * 0.032, (255, 150, 180, 255))
        _heart(d, hx0 - S * 0.01, hy + S * 0.02 - bob, S * 0.038, (255, 130, 165, 255))
    elif kind == "sparkle":
        _spark(d, hx0, hy - S * 0.04 + bob, S * 0.055, gold)
        _spark(d, hx1, hy - S * 0.07 - bob, S * 0.07, gold)
        _spark(d, hx1 + S * 0.03, hy + S * 0.1 + bob, S * 0.035, (255, 215, 110, 255))
    elif kind == "star":
        _star(d, hx0, hy - S * 0.03 - bob, S * 0.055, gold)
        _star(d, hx1, hy - S * 0.06 + bob, S * 0.065, gold)
    elif kind == "flower":
        for cx, cy, r in ((hx0, hy + bob, S * 0.03), (hx1, hy - S * 0.05 - bob, S * 0.036)):
            for i in range(5):
                a = i * math.pi * 2 / 5 + k * 0.6
                d.ellipse((cx + math.cos(a) * r - r * 0.7, cy + math.sin(a) * r - r * 0.7,
                           cx + math.cos(a) * r + r * 0.7, cy + math.sin(a) * r + r * 0.7), fill=(255, 150, 185, 255))
            d.ellipse((cx - r * 0.5, cy - r * 0.5, cx + r * 0.5, cy + r * 0.5), fill=(255, 220, 90, 255))
    elif kind == "tear":
        _drop(d, hx0 + S * 0.02, hy + S * 0.1 + abs(bob) * 2, S * 0.03, blue)
        _drop(d, hx1 - S * 0.02, hy + S * 0.13 + abs(bob) * 3, S * 0.026, blue)
    elif kind == "anger":
        _anger(d, hx1, hy - S * 0.06 + bob, S * 0.1, red, max(4, int(S * 0.018)))
    elif kind == "blush":
        for i in range(3):
            xx = hx1 - S * 0.02 + i * S * 0.028
            d.line((xx, hy + S * 0.06, xx - S * 0.02, hy + S * 0.105), fill=(255, 120, 150, 255), width=max(3, int(S * 0.01)))
        _heart(d, hx0, hy - S * 0.02 + bob, S * 0.035, (255, 150, 180, 255))
    elif kind == "zzz":
        for i, (dx, dy, sz) in enumerate(((0.0, 0.0, 0.085), (0.06, -0.075, 0.065), (0.105, -0.135, 0.05))):
            _text(img, (hx1 + S * dx - S * 0.02, hy + S * dy - S * 0.02 + bob * (i + 1) * 0.6), "Z", int(S * sz), purple,
                  max(2, int(S * 0.008)), angle=-12)
    elif kind == "question":
        _text(img, (hx1, hy - S * 0.06 + bob), "?", int(S * 0.15), (70, 170, 140, 255), max(3, int(S * 0.01)), angle=-14)
    elif kind == "bang":
        _text(img, (hx1, hy - S * 0.07 + bob), "!!", int(S * 0.14), (245, 130, 40, 255), max(3, int(S * 0.01)), angle=-10)
    elif kind == "note":
        _note(d, hx0, hy - S * 0.04 - bob, S * 0.075, purple)
        _note(d, hx1, hy - S * 0.08 + bob, S * 0.085, (235, 110, 170, 255), double=True)


def _caption(img: Image.Image, text: str) -> None:
    if not text:
        return
    S = img.width
    size = int(S * 0.15)
    while size > S * 0.07 and fonts.text_width(text, size) > S * 0.86:
        size -= 2
    _text(img, (S / 2, S * 0.885), text, size, (214, 51, 108, 255), max(3, int(size * 0.14)))


def _frame(char: Image.Image, size: int, mood: str, caption: str, k: float, box_hint=None) -> Image.Image:
    _act, _at, decor, color, _d = MOODS[mood]
    img = _background(size, color, seed=sum(map(ord, mood)))
    area_h = size * (0.77 if caption else 0.9)
    bb = box_hint or char.getbbox() or (0, 0, char.width, char.height)
    cw, ch = bb[2] - bb[0], bb[3] - bb[1]
    s = min(size * 0.8 / cw, area_h / ch)
    piece = char.crop(bb)
    piece = piece.resize((max(1, int(cw * s)), max(1, int(ch * s))), Image.LANCZOS)
    piece = _outline(piece, max(3, int(size * 0.012)))
    x = int((size - piece.width) / 2)
    y = int(size * 0.02 + (area_h - (piece.height - 6 * max(3, int(size * 0.012)))) / 2) - 3 * max(3, int(size * 0.012))
    img.alpha_composite(piece, (x, max(0, y)))
    pad = 3 * max(3, int(size * 0.012))
    _decor(img, decor, (x + pad, max(0, y) + pad, x + piece.width - pad, max(0, y) + piece.height - pad), k)
    _caption(img, caption)
    return img


def render_png(rig: Rig, mood: str, caption: str = "", size: int = SIZE) -> Image.Image:
    mood = norm_mood(mood) or "开心"
    act, at = MOODS[mood][0], MOODS[mood][1]
    pet = _pose(rig, act, at)
    char = _char(pet, rig, 1.6)
    return _frame(char, size, mood, caption, 0.15).convert("RGB")


def render_gif(rig: Rig, mood: str, caption: str = "", size: int = GIF_SIZE) -> list[Image.Image]:
    mood = norm_mood(mood) or "开心"
    act, at = MOODS[mood][0], MOODS[mood][1]
    pet = _pose(rig, act, at)
    chars = []
    for i in range(GIF_FRAMES):
        chars.append(_char(pet, rig, 1.0, blink=(i == GIF_FRAMES - 2 and act not in (E.SLEEP, E.YAWN))))
        for _ in range(3):
            _tick(pet, GIF_STEP / 3)
    # 所有帧用同一个取景框，人物才不会忽大忽小
    boxes = [c.getbbox() for c in chars if c.getbbox()]
    box = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
    return [_frame(c, size, mood, caption, i / GIF_FRAMES, box_hint=box).convert("RGB") for i, c in enumerate(chars)]


def make(out_dir: Path, mood: str, caption: str = "", animated: bool = True, rig_name: str = "") -> Path:
    """画一张表情包存到 out_dir，返回文件路径。animated=True 存成 GIF（私信用），否则 PNG（评论用）。"""
    rig = load_rig(rig_name or "default")
    mood = norm_mood(mood) or "开心"
    caption = fonts.strip_emoji(caption or "")[:8] if caption is not None else ""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{int(time.time() * 1000) % 10**10}"
    if animated:
        frames = render_gif(rig, mood, caption)
        path = out_dir / f"sticker_{stamp}.gif"
        pal = [f.quantize(colors=128, method=Image.MEDIANCUT, dither=Image.NONE) for f in frames]
        pal[0].save(path, save_all=True, append_images=pal[1:], duration=int(GIF_STEP * 1000), loop=0, optimize=False,
                    disposal=1)
    else:
        path = out_dir / f"sticker_{stamp}.png"
        render_png(rig, mood, caption).save(path, format="PNG", optimize=True)
    return path


def sheet(rig: Rig | None = None, cols: int = 4, size: int = 256) -> Image.Image:
    """把所有情绪画成一张预览图（界面上“看看她的表情包”用）。"""
    rig = rig or load_rig("default")
    names = list(MOODS)
    rows = (len(names) + cols - 1) // cols
    out = Image.new("RGB", (cols * size, rows * size), (255, 245, 250))
    for i, n in enumerate(names):
        out.paste(render_png(rig, n, MOODS[n][4], size), ((i % cols) * size, (i // cols) * size))
    return out
