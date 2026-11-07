"""把抠好的全身形象自动拆成可以动的部件（头、身体、左右手臂的上臂 / 前臂、左右腿）。

做法和原来雨雨桌宠的 make_parts.py 一样，只是原来的关节坐标是手填的，这里由你在界面上拖好：
- 脖子以上是头；
- 肩 → 肘 → 手这条线周围一圈是手臂（粗细可以调），离肩近的是上臂、离手近的是前臂；
- 胯以下是腿（左右按中线分开，往上多留一截，画在身体后面，抬腿时不会露缝）；
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

    # 手臂：沿 肩 → 肘 → 手（再往外延伸一个半径，把手掌包进来）
    arm_w, upper_w, lower_w = {}, {}, {}
    for s in ("l", "r"):
        S, E, Hd = J[f"shoulder_{s}"], J[f"elbow_{s}"], J[f"hand_{s}"]
        ln = math.hypot(Hd[0] - E[0], Hd[1] - E[1]) or 1.0
        H2 = (Hd[0] + (Hd[0] - E[0]) / ln * r, Hd[1] + (Hd[1] - E[1]) / ln * r)
        d1 = _seg_dist(xs, ys, S, E)
        d2 = _seg_dist(xs, ys, E, H2)
        d = np.minimum(d1, d2)
        w = np.clip((r - d) / 1.5, 0, 1) * (ys >= neck_y - 2)
        arm_w[s] = w * A
        upper_w[s] = arm_w[s] * (d1 <= d2)
        lower_w[s] = arm_w[s] * (d1 > d2)
    arm_any = np.maximum(arm_w["l"], arm_w["r"])

    # 头：脖子以上（边缘柔和过渡 3 像素）。
    # 垂到脖子下面的头发（双马尾、长发）：脖子下面一小段里、在两肩外侧、又不是手臂的部分，也算头，跟着头一起动
    head_a = A * np.clip((neck_y + 1.5 - ys) / 3, 0, 1)
    sl_x, sr_x = J["shoulder_l"][0] - r * 0.3, J["shoulder_r"][0] + r * 0.3
    band = (ys >= neck_y - 10) & (ys < neck_y + (ground_y - top) * 0.15)
    outside = (xs < min(sl_x, sr_x)) | (xs > max(sl_x, sr_x))
    hair = A * band * outside * (arm_any < 0.3)
    head_a = np.maximum(head_a, hair)
    # 腿：胯以下，往上多留一截（画在身体后面）
    leg_over = max(4.0, (ground_y - hip_y) * 0.18)
    legs = A * np.clip((ys - (hip_y - leg_over)) / 2, 0, 1) * (1 - np.clip(arm_any * 2, 0, 1))
    leg_l_a = legs * (xs < mid_x)
    leg_r_a = legs * (xs >= mid_x)
    # 身体：脖子以上留一截被头盖住，胯以下去掉，手臂的部分去掉后补色
    body_a = A * np.clip((ys - (neck_y - 8)) / 2, 0, 1) * np.clip((hip_y + 3 - ys) / 3, 0, 1) * (hair < 0.5)
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
           "parts": parts, "joints": {k: list(v) for k, v in J.items()}, "arm_width": arm_width}
    (out_dir / "rig.json").write_text(json.dumps(rig, ensure_ascii=False, indent=1), encoding="utf-8")
    return rig
