"""“配音图片视频”用的那一张图：标题和正文全部写在同一张图上，图上站着桌宠（每次随机一个动作、表情、姿势）。

和文字卡片的区别：文字卡片是一张封面 + 好几张卡片，配音以后画面一张张换；
这里只有一张图，她读文案的时候画面不动、不翻页。
- 背景：你在“文字卡片的背景”里设了默认背景就用它，没设就用内置的粉色系底图（四种颜色随机一种）；
- 字多的时候自动把字缩小、把桌宠缩小让出地方；实在写不下（很少见）结尾用“……”，配音照样读完整的；
- 形象用桌宠现在的形象（默认形象或你上传的形象），动作从表情包的那 16 种情绪里随机。
"""
from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw

from ..pet.rig import load as load_rig
from . import fonts, stickers
from .cards import PALETTES, _background, _heart, fit_background

W, H = 1080, 1440
SS = 2
MARGIN = 64                 # 白色文字框离画面边缘
PAD = 56                    # 字离白框边缘
SIZES = range(92, 25, -2)   # 正文字号从大到小试
PETS = (470, 360, 250)      # 桌宠的高度：字多了就换小一号


def _lines(title: str, text: str, size: int, width: float) -> tuple[list[str], list[str], int]:
    """按这个字号折行：(标题的行, 正文的行（空字符串 = 段落之间的空隙）, 标题字号)。"""
    tsize = int(size * 1.28)
    t_lines = fonts.wrap(title.strip(), tsize, width) if title.strip() else []
    body: list[str] = []
    for para in [p.strip() for p in (text or "").replace("\r", "").split("\n")]:
        if not para:
            if body and body[-1] != "":
                body.append("")
            continue
        body += fonts.wrap(para, size, width)
    while body and body[-1] == "":
        body.pop()
    return t_lines, body, tsize


def _height(t_lines: list, body: list, size: int, tsize: int) -> float:
    h = len(t_lines) * tsize * 1.35 + (size * 0.7 if t_lines and body else 0)
    return h + sum(size * (0.55 if ln == "" else 1.52) for ln in body)


HEAD_TILT_MAX = 6.0       # 静止的图上，头最多歪这么多度


def _still(pet, rig, scale: float) -> Image.Image:
    """把动作定格成一张静止的画。

    动起来的时候，跳舞、加油这些动作会把头往旁边甩（头相对身体横着挪开二三十个像素），连起来看是自然的；
    定格成一张图，头就像从脖子上错开了、和身体断成两截。所以定格时头不横着挪（留在脖子正上方），
    歪头的角度也收小一点——身体的倾斜、手脚的动作照旧，姿势还是那个姿势。"""
    from ..pet.render import render
    pet.parts = []
    pet.face._blink_in = 99.0
    pose = pet.pose()
    pose.HeadDX = 0.0
    pose.HeadTilt = max(-HEAD_TILT_MAX, min(HEAD_TILT_MAX, pose.HeadTilt))
    pet.last_pose = pose
    pet.pose = lambda: pose              # 画的时候用收过的这个姿势
    pet.face.update(pet, 0.033)
    return render(pet, rig, scale).img


def _pet(rig_name: str, mood: str, height: int) -> Image.Image:
    """桌宠摆好这个情绪的动作，带一圈白边，缩放到指定高度。"""
    rig = load_rig(rig_name or "default")
    act, at = stickers.MOODS[mood][0], stickers.MOODS[mood][1]
    char = _still(stickers._pose(rig, act, at), rig, 3.0)
    box = char.getbbox()
    if box:
        char = char.crop(box)
    char = stickers._outline(char, 7)
    k = height / max(1, char.height)
    return char.resize((max(1, int(char.width * k)), max(1, int(char.height * k))), Image.LANCZOS)


def render(title: str, text: str, name: str, out: Path, rig_name: str = "default", bg_path: Path | None = None,
           mood: str | None = None, seed: int | None = None) -> dict:
    """画这一张图存到 out（JPG）。返回 {"mood": 用了哪个情绪的动作, "cut": 正文有没有写不下被截断}。"""
    rnd = random.Random(seed)
    mood = mood if mood in stickers.MOODS else rnd.choice(list(stickers.MOODS))
    pal = PALETTES[rnd.randrange(len(PALETTES))]
    s = SS
    bg = None
    if bg_path is not None and Path(bg_path).exists():
        try:
            bg = Image.open(bg_path)
            bg.load()
        except OSError:
            bg = None
    img = fit_background(bg, W * s, H * s) if bg is not None else _background(pal, rnd.randrange(10**6))
    inner_w = (W - 2 * MARGIN - 2 * PAD) * s
    top = 96 * s
    fit = None
    for pet_h in PETS:
        bottom = (H - 60 - int(pet_h * 0.62)) * s          # 桌宠上半身压在白框的下沿上
        room = bottom - top - 2 * PAD * s
        for size in SIZES:
            t_lines, body, tsize = _lines(title, text, size * s, inner_w)
            if _height(t_lines, body, size * s, tsize) <= room:
                fit = (pet_h, bottom, size * s, tsize, t_lines, body)
                break
        if fit:
            break
    cut = False
    if not fit:                                             # 最小的字、最小的桌宠还是写不下：截断
        pet_h = PETS[-1]
        bottom = (H - 60 - int(pet_h * 0.62)) * s
        room = bottom - top - 2 * PAD * s
        size = SIZES[-1] * s
        t_lines, body, tsize = _lines(title, text, size, inner_w)
        while body and _height(t_lines, body, size, tsize) > room:
            body.pop()
            cut = True
        if body:
            body[-1] = body[-1][: max(0, len(body[-1]) - 2)] + "……"
        fit = (pet_h, bottom, size, tsize, t_lines, body)
    pet_h, bottom, size, tsize, t_lines, body = fit

    # 白色文字框
    panel = Image.new("RGBA", img.size, (0, 0, 0, 0))
    pd = ImageDraw.Draw(panel)
    box = (MARGIN * s, top, (W - MARGIN) * s, bottom)
    pd.rounded_rectangle((box[0] + 6 * s, box[1] + 10 * s, box[2] + 6 * s, box[3] + 10 * s), radius=56 * s,
                         fill=pal["accent"] + (38,))
    pd.rounded_rectangle(box, radius=56 * s, fill=(255, 255, 255, 236), outline=pal["dot"] + (255,), width=5 * s)
    img.alpha_composite(panel)
    d = ImageDraw.Draw(img)
    _heart(d, box[0] + 70 * s, box[1] + 4 * s, 30 * s, pal["accent"] + (255,))
    _heart(d, box[2] - 96 * s, box[1] - 2 * s, 20 * s, pal["dot"] + (255,))

    # 字：整体在白框里上下居中
    total = _height(t_lines, body, size, tsize)
    y = box[1] + PAD * s + max(0, (bottom - top - 2 * PAD * s - total) / 2)
    x0 = box[0] + PAD * s
    for ln in t_lines:
        w = fonts.text_width(ln, tsize)
        fonts.draw_text(d, (x0 + (inner_w - w) / 2, y), ln, tsize, pal["accent"] + (255,))
        y += tsize * 1.35
    if t_lines and body:
        y += size * 0.7
    center = len([b for b in body if b]) <= 3               # 只有两三行：居中更好看
    for ln in body:
        if ln == "":
            y += size * 0.55
            continue
        w = fonts.text_width(ln, size)
        fonts.draw_text(d, (x0 + (inner_w - w) / 2 if center else x0, y), ln, size, pal["text"] + (255,))
        y += size * 1.52

    # 桌宠：站在右下，随机动作；偶尔左右翻个面
    pet = _pet(rig_name, mood, pet_h * s)
    if rnd.random() < 0.35:
        pet = pet.transpose(Image.FLIP_LEFT_RIGHT)
    px = int(rnd.uniform(0.52, 0.7) * W * s - pet.width / 2)
    px = max(20 * s, min(W * s - pet.width - 20 * s, px))
    img.alpha_composite(pet, (px, H * s - 44 * s - pet.height))
    # 左下角的署名
    sign = fonts.strip_emoji(name or "")[:10]
    if sign:
        fonts.draw_text(d, (MARGIN * s + 12 * s, H * s - 118 * s), f"—— {sign}", 44 * s, pal["accent"] + (255,),
                        stroke_width=5 * s, stroke_fill=(255, 255, 255, 255))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.resize((W, H), Image.LANCZOS).convert("RGB").save(out, quality=92)
    return {"mood": mood, "cut": cut}
