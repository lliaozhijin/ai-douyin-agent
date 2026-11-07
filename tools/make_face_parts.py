"""给默认形象（粉裙子小女孩）拆出“会动的小部件”：两条小辫子、头顶的呆毛、各种眼睛和嘴巴。

开发时运行一次就行（结果已经放在 assets/pet/default 里，打包时直接带上）：
    python tools/make_face_parts.py

做了什么：
- 从 head_full.png（第一次运行时把原来的 head.png 备份成它）里把左右小辫子、呆毛剪出来，
  存成 tail_l.png / tail_r.png / ahoge.png，head.png 里去掉它们（脸的轮廓和粉色发绳留在头上）；
- 表情只在原图上做变化：待机就是原图的眼睛和嘴巴；另外用原图的笔画做两张贴片——
  “眨眼”（把原来的眼睛压扁成一短横）和“说话”（原来的笑嘴张开一点），先用脸的白色盖住原来的笔画再贴上；
- 把这些部件的位置、转动的支点写进 rig.json。
需要 numpy、scipy、Pillow（只在开发时用，软件运行时不需要 scipy）。
"""
from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from scipy import ndimage as nd

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "assets" / "pet" / "default"
INK = (22, 18, 20, 255)
FACE = (254, 255, 254, 255)
MOUTH_IN = (196, 58, 74, 255)
TONGUE = (247, 128, 146, 255)
STROKE = 13            # 原图线条大约这么粗（像素）
rnd = random.Random(7)


def comps(mask, min_size=80):
    lab, n = nd.label(mask)
    out = []
    for i, sl in enumerate(nd.find_objects(lab), 1):
        m = lab == i
        if m.sum() >= min_size:
            out.append((m, sl))
    return out


def disk(r):
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return x * x + y * y <= r * r


def crayon_line(draw: ImageDraw.ImageDraw, pts, width=STROKE, color=INK):
    """蜡笔味的线：一串圆头短线，粗细稍微抖一抖。"""
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        w = width + rnd.uniform(-1.2, 1.2)
        draw.line([(x0, y0), (x1, y1)], fill=color, width=int(round(w)))
        r = w / 2
        draw.ellipse([x1 - r, y1 - r, x1 + r, y1 + r], fill=color)
    x, y = pts[0]
    r = width / 2
    draw.ellipse([x - r, y - r, x + r, y + r], fill=color)


def arc_pts(cx, cy, rx, ry, a0, a1, n=14):
    return [(cx + rx * math.cos(math.radians(a)), cy + ry * math.sin(math.radians(a)))
            for a in np.linspace(a0, a1, n)]


def texture(img: Image.Image) -> Image.Image:
    """加一点点蜡笔颗粒，和原图的笔触接近。"""
    a = np.array(img).astype(np.int16)
    ink = (a[..., 3] > 0) & (a[..., 0] < 80)
    noise = np.random.default_rng(3).integers(-14, 15, a.shape[:2])
    for c in range(3):
        a[..., c] = np.where(ink, np.clip(a[..., c] + noise, 0, 255), a[..., c])
    return Image.fromarray(a.astype(np.uint8), "RGBA")


def main() -> None:
    full = D / "head_full.png"
    if not full.exists():
        Image.open(D / "head.png").save(full)
    head = Image.open(full).convert("RGBA")
    a = np.array(head).astype(int)
    r, g, b, al = a[..., 0], a[..., 1], a[..., 2], a[..., 3]
    solid = al > 20
    dark = (al > 128) & (r < 90) & (g < 90) & (b < 90)
    white = (al > 128) & (r > 225) & (g > 225) & (b > 225)
    brown = (al > 128) & (r > 80) & (r < 170) & (g < 90) & (b < 80)
    pink = (al > 128) & (r > 200) & (g < 170) & (b > 120)

    rig = json.loads((D / "rig.json").read_text(encoding="utf-8"))
    hx, hy = rig["parts"]["head"]          # head.png 左上角在原图里的位置

    face = max(comps(white, 1000), key=lambda c: c[0].sum())[0]
    browns = sorted(comps(brown, 300), key=lambda c: -c[0].sum())
    cap = browns[0][0]
    tails = sorted(browns[1:3], key=lambda c: c[1][1].start)           # 左、右
    pinks = sorted(comps(pink, 100), key=lambda c: c[1][1].start)
    bobbles = [m for m, sl in pinks if sl[0].start > 200]                # 发绳（不是呆毛顶上的粉点）

    # 每个有颜色的像素归给离它最近的“种子”：头本体（脸 + 头发 + 发绳）、左辫子、右辫子、呆毛。
    # 这样再粗的蜡笔轮廓线也能分干净（轮廓线跟着它包住的那块颜色走）
    core = nd.binary_fill_holes(face | cap)
    top = int(np.nonzero(cap)[0].min())
    ahoge_seed = solid & ~nd.binary_dilation(core, disk(STROKE + 10))
    ahoge_seed[top - 4:, :] = False
    keep = np.zeros_like(ahoge_seed)
    for m, sl in comps(ahoge_seed, 30):
        if sl[0].start < top - 25:      # 只要明显翘在头发上面的笔画，不要头发轮廓鼓起来的那一点
            keep |= m
    ahoge_seed = keep
    # 发绳跟着辫子一起动（它本来就扎在辫子上）
    seeds = {"core": core, "tail_l": tails[0][0] | bobbles[0], "tail_r": tails[1][0] | bobbles[-1],
             "ahoge": ahoge_seed}
    dist = {k: nd.distance_transform_edt(~m) for k, m in seeds.items()}
    names = list(seeds)
    owner = np.argmin(np.stack([dist[k] for k in names]), axis=0)

    src = np.array(head)
    out = src.copy()
    parts = {}
    pivots = {}
    for k in ("tail_l", "tail_r", "ahoge"):
        reg = solid & (owner == names.index(k))
        if k != "ahoge":
            # 辫子根部往头里多带一圈（画在头后面被盖住），转动时根部不会露出缝
            reg |= solid & nd.binary_dilation(reg, disk(10)) & (owner == names.index("core"))
        ys, xs = np.nonzero(reg)
        x0, y0, x1, y1 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
        img = np.zeros_like(src)
        img[reg] = src[reg]
        Image.fromarray(img[y0:y1, x0:x1], "RGBA").save(D / f"{k}.png")
        out[solid & (owner == names.index(k))] = 0
        parts[k] = [int(hx + x0), int(hy + y0)]
        if k == "ahoge":
            base = reg & (np.arange(reg.shape[0])[:, None] >= y1 - 14)
            bys, bxs = np.nonzero(base)
            pivots[k] = [round(float(hx + bxs.mean()), 1), round(float(hy + bys.max()), 1)]
        else:
            bob = bobbles[0] if k == "tail_l" else bobbles[-1]
            by, bx = nd.center_of_mass(bob)
            pivots[k] = [round(float(hx + bx), 1), round(float(hy + by), 1)]   # 绕发绳转

    Image.fromarray(out, "RGBA").save(D / "head.png")

    # ---- 眼睛和嘴巴：先找到原来的笔画 ----
    inner = nd.binary_erosion(nd.binary_fill_holes(face), disk(4))
    strokes = [(m, sl) for m, sl in comps(dark & inner, 300)]
    eyes = sorted([c for c in strokes if (c[1][0].stop - c[1][0].start) > (c[1][1].stop - c[1][1].start)],
                  key=lambda c: c[1][1].start)[:2]
    eye_ids = {id(c) for c in eyes}
    mouth = max([c for c in strokes if id(c) not in eye_ids], key=lambda c: c[1][1].stop - c[1][1].start)

    def box(sl):
        return sl[1].start, sl[0].start, sl[1].stop, sl[0].stop

    eye_boxes = [box(sl) for _, sl in eyes]
    mx0, my0, mx1, my1 = box(mouth[1])
    eye_cover = nd.binary_dilation(np.any([m for m, _ in eyes], axis=0), disk(5))
    mouth_cover = nd.binary_dilation(mouth[0], disk(5))

    src_full = np.array(head)

    def stroke_img(m: np.ndarray, sl) -> Image.Image:
        """把原图里的一笔（眼睛 / 嘴巴）连同蜡笔质感原样抠出来。"""
        cut = src_full[sl].copy()
        cut[..., 3] = np.where(m[sl], cut[..., 3], 0)
        return Image.fromarray(cut, "RGBA")

    def patch(name: str, cover: np.ndarray, draw_fn, pad=40):
        cy, cx = np.nonzero(cover)
        x0, y0 = int(cx.min()) - pad, int(cy.min()) - pad
        x1, y1 = int(cx.max()) + pad, int(cy.max()) + pad
        cov = Image.fromarray((cover[y0:y1, x0:x1] * 255).astype(np.uint8), "L").filter(ImageFilter.GaussianBlur(1.2))
        # 先用脸的白色盖住原来的笔画（整块都是白色，只用透明度控制范围，边缘不会发灰）
        img = Image.new("RGBA", (x1 - x0, y1 - y0), FACE[:3] + (0,))
        img.putalpha(cov)
        draw_fn(img, -x0, -y0)
        bb = img.getbbox()
        img = img.crop(bb)
        img.save(D / f"{name}.png")
        parts[name] = [int(hx + x0 + bb[0]), int(hy + y0 + bb[1])]

    # 只在原图的表情上做变化：待机就是原图的眼睛和嘴巴，这里只做“眨眼”和“说话张嘴”两张
    def eyes_closed(img, ox, oy):
        """眨眼：把原图的眼睛那一笔压扁成一短横（还是原来的笔触），放在眼睛偏下的位置，像眼皮合上。"""
        for m, sl in eyes:
            st = stroke_img(m, sl)
            w, h = st.size
            flat = st.resize((int(w * 1.45), max(STROKE - 1, int(h * 0.24))), Image.LANCZOS)
            cx = (sl[1].start + sl[1].stop) / 2 + ox
            cy = sl[0].start + h * 0.62 + oy
            img.alpha_composite(flat, (int(cx - flat.width / 2), int(cy - flat.height / 2)))

    def mouth_talk(img, ox, oy):
        """说话：原图的笑嘴不变，在嘴角之间补一道同样笔触的上唇线，中间填上嘴里的颜色，像笑着张开了嘴。"""
        m, sl = mouth
        st = stroke_img(m, sl)
        cols = np.nonzero(m[sl].any(axis=0))[0]
        centers = [(sl[1].start + c, sl[0].start + float(np.nonzero(m[sl][:, c])[0].mean())) for c in cols]
        top = min(centers[0][1], centers[-1][1]) + 2
        mid = sum(y for _, y in centers) / len(centers)
        bottom = [(x, max(y, top) + (y - top) * 0.35) for x, y in centers]      # 嘴张开一点：下嘴唇往下挪
        poly = [(x + ox, y + oy) for x, y in bottom] + [(centers[-1][0] + ox, top + oy), (centers[0][0] + ox, top + oy)]
        ImageDraw.Draw(img).polygon(poly, fill=MOUTH_IN)
        # 下嘴唇：原来那一笔，竖着拉长一点贴着张开的嘴
        lip = st.resize((st.width, int(st.height * 1.35)), Image.LANCZOS)
        img.alpha_composite(lip, (int(sl[1].start + ox), int(sl[0].start + oy)))
        # 上嘴唇：把原来那一笔压扁拉直成一横，还是原来的笔触
        upper = st.resize((int(centers[-1][0] - centers[0][0]) + STROKE, max(STROKE - 2, int(st.height * 0.26))),
                          Image.LANCZOS)
        img.alpha_composite(upper, (int(centers[0][0] - STROKE / 2 + ox), int(top - upper.height / 2 + oy)))
        _ = mid

    patch("eyes_closed", eye_cover, eyes_closed)
    patch("mouth_talk", mouth_cover, mouth_talk)
    for old in ("eyes_happy", "eyes_half", "eyes_x", "mouth_open", "mouth_flat", "mouth_frown", "mouth_o"):
        rig["parts"].pop(old, None)
        (D / f"{old}.png").unlink(missing_ok=True)

    rig["parts"].update(parts)
    rig["sway"] = {
        "tail_l": {"pivot": pivots["tail_l"], "kind": "hang"},
        "tail_r": {"pivot": pivots["tail_r"], "kind": "hang"},
        "ahoge": {"pivot": pivots["ahoge"], "kind": "spring"},
    }
    bx, by = rig["parts"]["body"]
    # 身体图边上有几道抠图留下的细线和小点：裙摆一摆就会显出来，去掉（只留最大的那一整块）
    body = Image.open(D / "body.png").convert("RGBA")
    ba = np.array(body)
    lab, n = nd.label(ba[..., 3] > 10)
    if n > 1:
        sizes = nd.sum(np.ones_like(lab), lab, range(1, n + 1))
        keep = nd.binary_dilation(lab == (int(np.argmax(sizes)) + 1), disk(2))
        ba[~keep] = 0
        body = Image.fromarray(ba, "RGBA")
        body.save(D / "body.png")
    rig["skirt"] = {"top": round(by + body.height * 0.5, 1), "hem": round(by + body.height, 1), "shift": 20}
    rig["face"] = {"eyes": ["closed"], "mouth": ["talk"]}
    (D / "rig.json").write_text(json.dumps(rig, ensure_ascii=False, indent=1, default=lambda o: o.item()), encoding="utf-8")
    print("parts:", parts)
    print("pivots:", pivots)


if __name__ == "__main__":
    sys.exit(main())
