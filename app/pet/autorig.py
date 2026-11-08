"""把抠好的全身形象自动拆成可以动的部件（头、身体、左右手臂的上臂 / 前臂、左右腿）。

做法和原来雨雨桌宠的 make_parts.py 一样，只是原来的关节坐标是手填的，这里由你在界面上拖好：
- 脖子以上是头；
- 肩 → 肘 → 手这条线周围一圈是手臂（粗细可以调），离肩近的是上臂、离手近的是前臂；
  一圈以外的“边角料”（比胳膊粗的手 / 蹄子、半透明描边、冒到脖子线以上的一点）也整块归胳膊，身上不留碎片；
- 腿：按实际轮廓找身体和腿的分界（裆在哪、肚子 / 裙摆外沿在哪），圆肚子垂到胯下面也留在身体上；
  左右按中线分开，往上多留一截，画在身体后面，抬腿时不会露缝；
- 剩下的是身体；手臂原来挡住的身体部分用周围的颜色补上（手臂抬起来时不会露出一个洞）；
- 再合成一条“伸直的手臂”，挥手、跳舞时用。

全部在本机完成，只用 Pillow 和 numpy。
"""
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import numpy as np
from PIL import Image

JOINTS = ("neck", "shoulder_l", "elbow_l", "hand_l", "shoulder_r", "elbow_r", "hand_r", "hip_l", "hip_r")
JOINT_LABELS = {
    "neck": "脖子（头和身体的分界）", "shoulder_l": "左肩", "elbow_l": "左肘", "hand_l": "左手",
    "shoulder_r": "右肩", "elbow_r": "右肘", "hand_r": "右手", "hip_l": "左胯（左腿根）", "hip_r": "右胯（右腿根）",
}
ARM_SEG = 2       # 拆手臂的算法版本：2 = 手 / 边角料整块归胳膊（画手臂时不再按颜色去“重复的身体碎片”）
HAND_EXTRA = 0.15  # 手那里的圆比胳膊半径大多少（再大的手 / 蹄子由下面的“边角料归胳膊”接住；圆太大，叉腰时会把胯挖走一块）
CANON = {"l": 150.0, "r": 30.0}  # 伸直的手臂朝哪个方向（和原版雨雨一样：往下、往外张开 60°）


def _norm(a: float) -> float:
    while a > 180:
        a -= 360
    while a <= -180:
        a += 360
    return a


def _ang(p, q) -> float:
    return math.degrees(math.atan2(q[1] - p[1], q[0] - p[0]))


# ---------- 自动猜关节点 ----------
def guess_joints(img: Image.Image) -> dict:
    """根据人物轮廓粗略估一个初始位置（Q 版大头也能大致对上），之后你在界面上拖准。"""
    a = np.asarray(img.getchannel("A"), dtype=np.float32) / 255.0
    m = a > 0.5
    rows = np.where(m.any(axis=1))[0]
    cols = np.where(m.any(axis=0))[0]
    if len(rows) == 0:
        h, w = a.shape
        return _fallback(w, h)
    top, bot = int(rows[0]), int(rows[-1])
    left, right = int(cols[0]), int(cols[-1])
    H = bot - top + 1
    cx = (left + right) / 2
    widths = m.sum(axis=1).astype(np.float32)
    # 脖子：上面 25%～65% 之间最窄的一行（Q 版的头和身体之间通常会收窄）
    lo, hi = top + int(H * 0.25), top + int(H * 0.65)
    seg = widths[lo:hi]
    if len(seg) > 5:
        k = np.convolve(seg, np.ones(9) / 9, mode="same")
        neck_y = lo + int(np.argmin(k[4:-4]) + 4) if len(k) > 8 else lo + int(np.argmin(k))
    else:
        neck_y = top + int(H * 0.45)
    # 胯：从下往上，最后一行能看到“两条腿中间的空隙”的地方
    crotch = None
    for y in range(bot, neck_y, -1):
        xs = np.where(m[y])[0]
        if len(xs) < 2:
            continue
        gaps = np.where(np.diff(xs) > max(3, H * 0.01))[0]
        mid_gap = [g for g in gaps if abs((xs[g] + xs[g + 1]) / 2 - cx) < (right - left) * 0.2]
        if mid_gap:
            crotch = y
        elif crotch is not None:
            break
    hip_y = crotch if crotch is not None else bot - int(H * 0.18)
    body_rows = m[neck_y + int(H * 0.04):hip_y]
    # 肩：脖子下面一点，身体两侧往里收一点
    sy = neck_y + int(H * 0.05)
    xs = np.where(m[min(sy, bot)])[0]
    sl = float(xs[0]) if len(xs) else cx - H * 0.15
    sr = float(xs[-1]) if len(xs) else cx + H * 0.15
    inset = H * 0.04
    shoulder_l, shoulder_r = (sl + inset, sy), (sr - inset, sy)
    # 手臂最外面的点：在躯干上半段的话多半是手肘（叉腰），手在它下面往身体收回去；在下半段的话就是手
    ext_l, ext_r = (sl, (sy + hip_y) / 2), (sr, (sy + hip_y) / 2)
    if body_rows.size:
        ys, xs2 = np.where(body_rows)
        ys = ys + neck_y + int(H * 0.04)
        il, ir = int(np.argmin(xs2)), int(np.argmax(xs2))
        ext_l, ext_r = (float(xs2[il]) + H * 0.02, float(ys[il])), (float(xs2[ir]) - H * 0.02, float(ys[ir]))
    span = max(1.0, hip_y - sy)

    def arm(shoulder, ext, sign):
        if (ext[1] - sy) / span < 0.55:  # 手肘
            elbow = ext
            hand = (ext[0] + (shoulder[0] - ext[0]) * 0.55, min(hip_y - H * 0.02, ext[1] + H * 0.09))
        else:  # 手
            hand = ext
            elbow = ((shoulder[0] + ext[0]) / 2 - sign * H * 0.02, (shoulder[1] + ext[1]) / 2)
        return elbow, hand
    elbow_l, hand_l = arm(shoulder_l, ext_l, 1)
    elbow_r, hand_r = arm(shoulder_r, ext_r, -1)
    xs = np.where(m[min(hip_y + 2, bot)])[0]
    hl = float(xs[0]) if len(xs) else cx - H * 0.08
    hr = float(xs[-1]) if len(xs) else cx + H * 0.08
    hip_l, hip_r = ((hl + cx) / 2, hip_y), ((hr + cx) / 2, hip_y)
    out = {"neck": (cx, neck_y), "shoulder_l": shoulder_l, "elbow_l": elbow_l, "hand_l": hand_l,
           "shoulder_r": shoulder_r, "elbow_r": elbow_r, "hand_r": hand_r, "hip_l": hip_l, "hip_r": hip_r}
    return {k: [round(float(v[0]), 1), round(float(v[1]), 1)] for k, v in out.items()}


def _fallback(w: int, h: int) -> dict:
    return {"neck": [w / 2, h * .45], "shoulder_l": [w * .35, h * .5], "elbow_l": [w * .27, h * .6],
            "hand_l": [w * .25, h * .7], "shoulder_r": [w * .65, h * .5], "elbow_r": [w * .73, h * .6],
            "hand_r": [w * .75, h * .7], "hip_l": [w * .43, h * .8], "hip_r": [w * .57, h * .8]}


def default_arm_width(img: Image.Image, joints: dict) -> float:
    """手臂粗细的初始值：按肩到手的长度估。"""
    ln = 0.0
    for s in ("l", "r"):
        sx, sy = joints[f"shoulder_{s}"]
        ex, ey = joints[f"elbow_{s}"]
        hx, hy = joints[f"hand_{s}"]
        ln += math.hypot(ex - sx, ey - sy) + math.hypot(hx - ex, hy - ey)
    w = max(8.0, ln / 2 * 0.42)
    a = np.asarray(img.getchannel("A")) > 128
    rows = np.where(a.any(axis=1))[0]
    if len(rows):
        w = min(w, (rows[-1] - rows[0]) * 0.08)  # 手臂再粗也不会超过身高的 8%
    return round(float(w), 1)


# ---------- 拆部件 ----------
def _seg_dist(px, py, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy or 1e-6
    t = np.clip(((px - ax) * dx + (py - ay) * dy) / L2, 0, 1)
    return np.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _dilate(m: np.ndarray, k: int) -> np.ndarray:
    """把掩码往外扩 k 像素（十字 / 方形交替，接近圆形）。"""
    out = m.copy()
    for i in range(int(k)):
        n = out.copy()
        n[1:] |= out[:-1]
        n[:-1] |= out[1:]
        n[:, 1:] |= out[:, :-1]
        n[:, :-1] |= out[:, 1:]
        if i % 2:
            n[1:, 1:] |= out[:-1, :-1]
            n[1:, :-1] |= out[:-1, 1:]
            n[:-1, 1:] |= out[1:, :-1]
            n[:-1, :-1] |= out[1:, 1:]
        out = n
    return out


def _erode(m: np.ndarray, k: int) -> np.ndarray:
    k = int(k)
    if k <= 0:
        return m.copy()
    return ~_dilate(np.pad(~m, k, constant_values=True), k)[k:-k, k:-k]


def _components(m: np.ndarray):
    """连通块（8 邻接）：按行找“连续的一段”再合并，只用 numpy + 少量循环，大图也快。返回 (标号图, 块数)。"""
    h, w = m.shape
    parent: list[int] = []

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    runs = []          # (y, x0, x1, id)
    prev: list = []
    for y in range(h):
        row = m[y]
        if not row.any():
            prev = []
            continue
        d = np.diff(np.concatenate(([0], row.view(np.int8), [0])))
        starts, ends = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
        cur = []
        j = 0
        for x0, x1 in zip(starts.tolist(), ends.tolist()):
            rid = len(parent)
            parent.append(rid)
            while j < len(prev) and prev[j][1] < x0:   # 上一行这一段完全在左边（且不斜着挨着）
                j += 1
            k = j
            while k < len(prev) and prev[k][0] <= x1:   # 和上一行的段重叠或斜着挨着
                ra, rb = find(rid), find(prev[k][2])
                if ra != rb:
                    parent[ra] = rb
                k += 1
            cur.append((x0, x1, rid))
            runs.append((y, x0, x1, rid))
        prev = cur
    lab = np.zeros((h, w), dtype=np.int32)
    remap: dict[int, int] = {}
    for y, x0, x1, rid in runs:
        root = find(rid)
        if root not in remap:
            remap[root] = len(remap) + 1
        lab[y, x0:x1] = remap[root]
    return lab, len(remap)


def _inpaint(rgb: np.ndarray, known: np.ndarray, fill: np.ndarray, iters: int) -> np.ndarray:
    """用周围已知的颜色一圈圈往里扩散，把 fill 区域补上。"""
    rgb = rgb.copy()
    k = known.astype(np.float32)
    c = rgb * k[..., None]
    todo = fill & ~known
    for _ in range(iters):
        if not todo.any():
            break
        sc = np.zeros_like(c)
        sk = np.zeros_like(k)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                sc += np.roll(np.roll(c, dy, 0), dx, 1)
                sk += np.roll(np.roll(k, dy, 0), dx, 1)
        reach = todo & (sk > 0)
        if not reach.any():
            break
        newc = sc[reach] / sk[reach][:, None]
        c[reach] = newc
        k[reach] = 1
        rgb[reach] = newc
        todo &= ~reach
    return rgb


def _belly_line(fg: np.ndarray, J: dict, mid_x: float, hip_y: float, ground_y: float) -> np.ndarray:
    """每一列身体和腿的分界 y（见 build 里的说明）。找不到空隙 / 外沿时退回胯的高度。"""
    Hh, Ww = fg.shape
    out = np.full(Ww, hip_y + 3, dtype=np.float32)
    leg_len = ground_y - hip_y
    if leg_len < 8:
        return out
    cx = int(round(min(max(mid_x, 0), Ww - 1)))
    lo_y = int(min(Hh - 1, hip_y + leg_len * 0.6))
    # 裆：中线那一列，从胯往下第一个透明的地方（两腿并拢、没有空隙就当是胯）
    y_c = hip_y + 3
    col = fg[int(max(0, hip_y)):lo_y + 1, max(0, cx - 1):cx + 2].all(axis=1)
    gap = np.nonzero(~col)[0]
    if gap.size:
        y_c = max(hip_y + 3, hip_y + float(gap[0]))
    probe = int(min(Hh - 1, (max(y_c, hip_y) + ground_y) / 2))   # 量腿外沿的那一行（小腿中间）
    for side in ("l", "r"):
        row = np.nonzero(fg[probe])[0]
        row = row[row < cx] if side == "l" else row[row > cx]
        if row.size == 0:
            continue
        xo = float(row.min() if side == "l" else row.max())
        # 从这一行往上找：外轮廓第一次比腿外沿宽出去 3 像素以上的那一行 = 肚子 / 裙摆的下沿
        y_o = hip_y + 3
        for y in range(probe, int(max(0, J["neck"][1])), -1):
            xs_ = np.nonzero(fg[y])[0]
            xs_ = xs_[xs_ < cx] if side == "l" else xs_[xs_ > cx]
            if xs_.size and ((side == "l" and xs_.min() < xo - 3) or (side == "r" and xs_.max() > xo + 3)):
                y_o = float(y)
                break
        y_o = min(max(y_o, hip_y - leg_len * 0.3), y_c)
        span = abs(cx - xo) or 1.0
        xs_all = np.arange(Ww, dtype=np.float32)
        t = np.clip(np.abs(cx - xs_all) / span, 0, 1)
        curve = y_o + (y_c - y_o) * np.sqrt(1 - t * t)   # 四分之一椭圆：外沿往里先快后慢，到裆最低
        sel = (xs_all < cx) if side == "l" else (xs_all >= cx)
        out[sel] = curve[sel]
    return out


def build(img: Image.Image, joints: dict, arm_width: float, out_dir: Path, name: str = "") -> dict:
    """拆部件、写 rig.json。img：抠好的 RGBA 图；joints：关节点（图片像素坐标）；arm_width：手臂粗细（像素）。"""
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    img = img.convert("RGBA")
    arr = np.asarray(img, dtype=np.float32)
    rgb, A = arr[..., :3], arr[..., 3] / 255.0
    Hh, Ww = A.shape
    ys, xs = np.mgrid[0:Hh, 0:Ww].astype(np.float32)
    J = {k: (float(v[0]), float(v[1])) for k, v in joints.items()}
    mask = A > 0.5
    rows = np.where(mask.any(axis=1))[0]
    if len(rows) == 0:
        raise ValueError("图片里没有人物")
    top, ground_y = float(rows[0]), float(rows[-1])
    neck_y = J["neck"][1]
    hip_y = (J["hip_l"][1] + J["hip_r"][1]) / 2
    mid_x = (J["hip_l"][0] + J["hip_r"][0]) / 2
    r = max(4.0, arm_width / 2)

    # 手臂：沿 肩 → 肘 → 手（再往外延伸一个半径），手那里再加一个更大的圆，把比胳膊粗的手 / 蹄子 / 手套整个包进来
    fg = A > 0.01
    # 胳膊顶上冒到脖子线以上的那一点（胳膊画在身体后面、肩膀高时常见）：同一列往上不和头连着的，也算胳膊。
    # 不然这一小条会被分给头，胳膊一动它还留在原地，肩膀上就多一块浅色碎片
    run_top = np.full(A.shape, np.inf, dtype=np.float32)
    prev = np.full(Ww, np.inf, dtype=np.float32)
    for y in range(Hh):
        cur = np.where(fg[y], np.minimum(prev, y), np.inf)
        run_top[y] = cur
        prev = cur
    above_ok = (ys < neck_y - 2) & (ys >= neck_y - r * 1.5) & (run_top >= neck_y - r * 1.2)
    arm_w, upper_w, lower_w, d_eff, d_up, d_lo = {}, {}, {}, {}, {}, {}
    for s in ("l", "r"):
        S, E, Hd = J[f"shoulder_{s}"], J[f"elbow_{s}"], J[f"hand_{s}"]
        ln = math.hypot(Hd[0] - E[0], Hd[1] - E[1]) or 1.0
        # 往手外面只多带 0.6 个半径（指尖再往外的部分由“边角料归胳膊”接住）：叉腰时手搭在身上，带太长会挖到身体
        H2 = (Hd[0] + (Hd[0] - E[0]) / ln * r * 0.6, Hd[1] + (Hd[1] - E[1]) / ln * r * 0.6)
        d1 = _seg_dist(xs, ys, S, E)
        d2 = np.minimum(_seg_dist(xs, ys, E, H2), np.hypot(xs - Hd[0], ys - Hd[1]) - r * HAND_EXTRA)
        d = np.minimum(d1, d2)
        d_eff[s], d_up[s], d_lo[s] = d, d1, d2
        w = np.clip((r - d) / 1.5, 0, 1) * ((ys >= neck_y - 2) | above_ok)
        arm_w[s] = w * A
        geo = w if s == "l" else np.maximum(geo, w)
    arm_any = np.maximum(arm_w["l"], arm_w["r"])
    # 胳膊带子外面的“边角料”：手比胳膊粗露出去的一圈、抗锯齿的半透明描边、蹄子尖……
    # 原来这些会留在身体图上，胳膊一抬就剩下一块孤零零的碎片。现在：把带子以外的部分做一次“开运算”，
    # 细条和离身体主干分开的小块 = 胳膊的边角料，分给离它最近的那条胳膊（上臂 / 前臂）
    d_arm = np.minimum(d_eff["l"], d_eff["r"])
    rest = fg & ~(geo > 0.05)
    k = max(2, int(round(r * 0.3)))
    opened = _dilate(_erode(rest, k), k) & rest
    lab, n = _components(opened)
    main = np.zeros_like(rest)
    if n:
        area = np.bincount(lab.ravel(), minlength=n + 1)
        far = np.bincount(lab.ravel(), weights=(d_arm > r * 2.2).ravel(), minlength=n + 1)
        keep = (far > 0) | (area > fg.sum() * 0.05)
        keep[0] = False
        main = keep[lab]
    core_main = main
    main = rest & _dilate(main, k + 1)
    # 紧贴胳膊带子的那一圈：只有本来就是身体主干的才算身体（胳膊和肚子贴着的地方，胳膊的边不会被“补回”身体）
    main &= core_main | (d_arm > r + 3)
    resid = rest & ~main & (d_arm <= r * 3) & ((ys >= neck_y - 2) | above_ok)
    for s, o in (("l", "r"), ("r", "l")):
        mine = resid & (d_eff[s] <= d_eff[o])
        arm_w[s] = np.maximum(arm_w[s], mine * A)
        upper_w[s] = arm_w[s] * (d_up[s] <= d_lo[s])
        lower_w[s] = arm_w[s] * (d_up[s] > d_lo[s])
    # 从头 / 身体 / 腿里去掉的范围按“位置”算（在胳膊带子里、或是分给胳膊的边角料），不按透明度：
    # 不然胳膊边上几乎透明的那一圈描边会留在身体上，胳膊一动就是几道淡淡的细线
    arm_any = np.maximum(geo, resid.astype(np.float32))
    arm_mask = _dilate(arm_any > 0.05, 2)

    # 头：脖子以上（边缘柔和过渡 3 像素）。
    # 垂到脖子下面的头发（双马尾、长发）：脖子下面一小段里、在两肩外侧、又不是手臂的部分，
    # 而且要从脖子上面（头那边）一路连下来，才算头发，跟着头一起动（胳膊的描边、身体的边不会被误认成头发）
    head_a = A * np.clip((neck_y + 1.5 - ys) / 3, 0, 1) * (arm_any <= 0.05)
    sl_x, sr_x = J["shoulder_l"][0] - r * 0.3, J["shoulder_r"][0] + r * 0.3
    band = (ys >= neck_y - 10) & (ys < neck_y + (ground_y - top) * 0.15)
    outside = (xs < min(sl_x, sr_x)) | (xs > max(sl_x, sr_x))
    zone = fg & outside & ~arm_mask & ((ys < neck_y) | band)
    hair_m = np.zeros_like(zone)
    lab, n = _components(zone)
    if n:
        from_head = np.bincount(lab.ravel(), weights=(ys < neck_y - 2).ravel(), minlength=n + 1)
        below = np.bincount(lab.ravel(), weights=(ys >= neck_y).ravel(), minlength=n + 1)
        ok = (from_head > 0) & (below >= (r * 0.6) ** 2)
        ok[0] = False
        hair_m = ok[lab] & (ys >= neck_y - 10)
    hair = A * hair_m
    head_a = np.maximum(head_a, hair)
    # 身体和腿的分界：圆肚子 / 裙摆常常垂到胯下面，按一条水平线切会把肚子底切成两半分给两条腿，
    # 一抬腿肚子底就跟着走（缺一块、裆部一道“T”字缝）。所以按实际轮廓找：两腿中间的空隙从哪儿开始（裆），
    # 腿外侧从哪儿开始比身体窄（肚子 / 裙摆的外沿），中间用一条圆弧连起来，弧线以上归身体，以下归腿
    y_b = _belly_line(fg, J, mid_x, hip_y, ground_y)[None, :]
    # 腿：分界线以下，往上多留一截（画在身体后面）
    leg_over = max(4.0, (ground_y - hip_y) * 0.18)
    legs = A * np.clip((ys - (y_b - leg_over)) / 2, 0, 1) * (1 - np.clip(arm_any * 2, 0, 1))
    leg_l_a = legs * (xs < mid_x)
    leg_r_a = legs * (xs >= mid_x)
    # 身体：脖子以上留一截被头盖住，胯以下去掉，手臂的部分去掉后补色
    body_a = A * np.clip((ys - (neck_y - 8)) / 2, 0, 1) * np.clip((y_b + 1.5 - ys) / 3, 0, 1) * (hair < 0.5)
    removed = (arm_any > 0.05) & (body_a > 0)
    keep = (body_a > 0.5) & ~removed
    span = np.zeros_like(keep)
    core = keep & (xs >= min(J["shoulder_l"][0], J["shoulder_r"][0]) - r * 2) & \
        (xs <= max(J["shoulder_l"][0], J["shoulder_r"][0]) + r * 2)
    for y in range(int(max(0, neck_y)), int(min(Hh, hip_y + 3))):
        xk = np.where(core[y])[0]
        if len(xk) >= 2:
            span[y, xk[0]:xk[-1] + 1] = True
    # 只补两肩之间（再往外一点点）被手臂挡住的身体；更外面原来就是背景，补了会多出一块“影子”
    lo_x = min(J["shoulder_l"][0], J["shoulder_r"][0]) - r * 0.5
    hi_x = max(J["shoulder_l"][0], J["shoulder_r"][0]) + r * 0.5
    fill = removed & span & (xs >= lo_x) & (xs <= hi_x)
    body_rgb = _inpaint(rgb, keep, fill, iters=int(r * 2.5) + 4)
    body_alpha = np.where(fill, np.maximum(body_a, 1.0 * (ys < hip_y)), body_a * (arm_any <= 0.05))

    def save(name_: str, color: np.ndarray, alpha: np.ndarray):
        al = np.clip(alpha, 0, 1)
        if al.max() <= 0.02:
            return None
        rgba = np.dstack([color, al * 255]).astype(np.uint8)
        im = Image.fromarray(rgba, "RGBA")
        bb = im.getchannel("A").point(lambda v: 255 if v > 2 else 0).getbbox()
        if not bb:
            return None
        im.crop(bb).save(out_dir / f"{name_}.png")
        return [int(bb[0]), int(bb[1])]

    parts = {}
    for nm, col, al in (("head", rgb, head_a), ("body", body_rgb, body_alpha), ("leg_l", rgb, leg_l_a),
                        ("leg_r", rgb, leg_r_a)):
        pos = save(nm, col, al)
        if pos:
            parts[nm] = pos

    arms = {}
    for s in ("l", "r"):
        S, E, Hd = J[f"shoulder_{s}"], J[f"elbow_{s}"], J[f"hand_{s}"]
        for nm, al in ((f"arm_{s}_up", upper_w[s]), (f"arm_{s}_lo", lower_w[s])):
            pos = save(nm, rgb, al)
            if pos:
                parts[nm] = pos
        cap_a = arm_w[s] * (np.hypot(xs - E[0], ys - E[1]) < r * 0.95)
        pos = save(f"arm_{s}_cap", rgb, cap_a)
        if pos:
            parts[f"arm_{s}_cap"] = pos
        th_u, th_l = _ang(S, E), _ang(E, Hd)
        straight = _norm(th_u - th_l)
        adj = _norm(CANON[s] - th_u)
        # 合成伸直的手臂：前臂绕肘转到和上臂一条线，再整条转到标准方向
        big = Image.new("RGBA", (Ww, Hh), (0, 0, 0, 0))
        for nm, rot_e in ((f"arm_{s}_cap", True), (f"arm_{s}_lo", True), (f"arm_{s}_up", False)):
            if nm not in parts:
                continue
            piece = Image.new("RGBA", (Ww, Hh), (0, 0, 0, 0))
            piece.alpha_composite(Image.open(out_dir / f"{nm}.png"), tuple(parts[nm]))
            if rot_e:
                piece = piece.rotate(-straight, resample=Image.BICUBIC, center=E)
            big.alpha_composite(piece)
        big = big.rotate(-adj, resample=Image.BICUBIC, center=S)
        bb = big.getchannel("A").point(lambda v: 255 if v > 2 else 0).getbbox()
        if bb:
            big.crop(bb).save(out_dir / f"arm_{s}_straight.png")
            parts[f"arm_{s}_straight"] = [int(bb[0]), int(bb[1])]
        length = math.hypot(E[0] - S[0], E[1] - S[1]) + math.hypot(Hd[0] - E[0], Hd[1] - E[1])
        hand = (S[0] + math.cos(math.radians(CANON[s])) * length, S[1] + math.sin(math.radians(CANON[s])) * length)
        arms[s] = {"shoulder": list(S), "elbow": list(E), "straight": round(straight, 2), "adj": round(adj, 2),
                   "hand": [round(hand[0], 1), round(hand[1], 1)]}

    K = 300.0 / max(10.0, ground_y - top)
    rig = {"version": 1, "name": name, "K": round(K, 5), "ground": [round(mid_x, 1), ground_y],
           "neck": list(J["neck"]), "hip_l": list(J["hip_l"]), "hip_r": list(J["hip_r"]), "arms": arms,
           "parts": parts, "joints": {k: list(v) for k, v in J.items()}, "arm_width": arm_width,
           "arm_seg": ARM_SEG}
    (out_dir / "rig.json").write_text(json.dumps(rig, ensure_ascii=False, indent=1), encoding="utf-8")
    return rig
