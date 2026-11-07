"""手臂和腿画成“一整条”：不再把手臂切成上臂、下臂两块拼起来，而是把整条手臂顺着骨头弯过去画。

为什么：切成几块再拼，关节处转一下就会裂开一条缝、露出剪下来的身体碎片，看着像断了。
做法（默认形象和你上传的形象都一样，加载时自动处理，只用 numpy + Pillow）：
1. 从“伸直的手臂”图里，沿着肩膀 → 手的方向，把整条手臂拉成一条横着的长条（含手）；
   肩膀那头被身体挡住的部分（和身体图重复的碎片）去掉，用手臂自己的截面往身体里多延伸一截，
   这样手臂根部永远藏在身体后面，不会和身体分开；
2. 画的时候把长条切成十几小段，沿着“上臂 + 下臂”两根骨头摆放，手肘处做成圆滑的弯；
   相邻小段首尾相接、还互相叠一点点，所以弯成什么角度都是连着的；
3. 腿同理：从胯（藏在裙子 / 身体里面）到脚是一整条，裙边那块碎片去掉，腿的上端往身体里延伸。
"""
from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw

from .gfx import Mat, Sprite

SLICES = 18


def _unit(dx: float, dy: float):
    n = math.hypot(dx, dy) or 1.0
    return dx / n, dy / n


def _lerp_angle(a: float, b: float, t: float) -> float:
    d = (b - a + math.pi) % (2 * math.pi) - math.pi
    return a + d * t


def _body_hit(body: Image.Image | None, bpos, pts_x: np.ndarray, pts_y: np.ndarray, colors: np.ndarray) -> np.ndarray:
    """这些原图坐标上的肢体像素，是不是和身体图同一位置、颜色也一样（= 剪下来的身体碎片）。"""
    if body is None:
        return np.zeros(pts_x.shape, dtype=bool)
    b = np.asarray(body)
    bx = np.round(pts_x - bpos[0]).astype(int)
    by = np.round(pts_y - bpos[1]).astype(int)
    inside = (bx >= 0) & (by >= 0) & (bx < b.shape[1]) & (by < b.shape[0])
    out = np.zeros(pts_x.shape, dtype=bool)
    if inside.any():
        bb = b[by[inside], bx[inside]]
        diff = np.abs(bb[:, :3].astype(np.int16) - colors[inside][:, :3].astype(np.int16)).max(axis=1)
        out[inside] = (bb[:, 3] > 100) & (diff < 48)
    return out


class Strip:
    """一条拉直的肢体：横着放，x = 沿骨头方向（根部往身体里多延伸 e 像素），y = 截面方向（中间是骨头）。"""

    def __init__(self, img: Image.Image, e: float, half: float, length: float):
        self.img = img          # 原图像素大小
        self.e = e              # 根部往身体里延伸了多少（像素）
        self.half = half        # 长条半高
        self.length = length    # 从根（关节）到末端的长度（像素，不含延伸部分）
        self.sprites: list[tuple[Sprite, float, float]] = []   # 预缩放的小段：(贴图, 起点 u, 终点 u)
        self.whole: Sprite | None = None
        self._dev = -1.0

    def prepare(self, K: float, dev: float) -> None:
        if abs(dev - self._dev) < 1e-3:
            return
        self._dev = dev
        k = K * dev
        w = max(2, round(self.img.width * k))
        h = max(2, round(self.img.height * k))
        big = self.img.resize((w, h), Image.LANCZOS)
        sx = w / self.img.width
        sy = h / self.img.height
        # 整条（腿只有一根骨头，直接整条画）：像素 (x, y) ↔ 逻辑 ((x/sx - e)·K, (y/sy - half)·K)
        self.whole = Sprite(big, self.e * sx, self.half * sy, sx / K)
        self.sprites = []
        total = self.e + self.length + (self.img.width - self.e - self.length)
        edges = np.linspace(0, total, SLICES + 1)
        ov = 1.5  # 每段两头多带 1.5 像素，和相邻的段叠在一起，弯的时候外侧也不露缝
        for a, b in zip(edges[:-1], edges[1:]):
            x0 = max(0, int(math.floor((a - ov) * sx)))
            x1 = min(w, int(math.ceil((b + ov) * sx)))
            piece = big.crop((x0, 0, x1, h))
            # 小段自己的坐标：逻辑 x = 从这一段的起点 a 算起
            sp = Sprite(piece, a * sx - x0, self.half * sy, sx / K)
            self.sprites.append((sp, a - self.e, b - self.e))


def _composite(src: dict, pos: dict, names: list[str]):
    """把几块部件按原图位置拼回去（得到原图里完整的那条手臂 / 腿）。返回 (预乘的 float 数组, 左上角原图坐标)。"""
    items = [(src[n], pos[n]) for n in names if n in src]
    if not items:
        return None, None
    x0 = min(p[0] for _, p in items)
    y0 = min(p[1] for _, p in items)
    x1 = max(p[0] + im.width for im, p in items)
    y1 = max(p[1] + im.height for im, p in items)
    canvas = Image.new("RGBA", (int(math.ceil(x1 - x0)) + 1, int(math.ceil(y1 - y0)) + 1), (0, 0, 0, 0))
    for im, p in items:
        canvas.alpha_composite(im, (int(round(p[0] - x0)), int(round(p[1] - y0))))
    arr = np.asarray(canvas).astype(np.float32) / 255.0
    arr[..., :3] *= arr[..., 3:4]
    return arr, (x0, y0)


def _sample(arr: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """双线性取样（预乘颜色），超出范围是透明。"""
    h, w = arr.shape[:2]
    x0 = np.floor(xs).astype(int)
    y0 = np.floor(ys).astype(int)
    fx = (xs - x0)[..., None]
    fy = (ys - y0)[..., None]
    out = np.zeros(xs.shape + (4,), dtype=np.float32)
    for dx, dy, wgt in ((0, 0, (1 - fx) * (1 - fy)), (1, 0, fx * (1 - fy)), (0, 1, (1 - fx) * fy), (1, 1, fx * fy)):
        xx, yy = x0 + dx, y0 + dy
        ok = (xx >= 0) & (yy >= 0) & (xx < w) & (yy < h)
        v = np.zeros(xs.shape + (4,), dtype=np.float32)
        v[ok] = arr[yy[ok], xx[ok]]
        out += v * wgt
    return out


def _straighten(arr, origin, path, u_end: float, half: float, floor_u: float, body, bpos, end_margin: float,
                tube: bool = False):
    """沿 path（u -> 中心点、法线，原图坐标）把肢体拉成横条，并把根部整理干净、往身体里延伸。"""
    us = np.arange(0.0, u_end + end_margin, 1.0)
    vs = np.arange(-half, half + 1, 1.0)
    cx, cy, nx, ny = (np.array(t) for t in zip(*[path(u) for u in us]))
    X = cx[None, :] + vs[:, None] * nx[None, :]
    Y = cy[None, :] + vs[:, None] * ny[None, :]
    pm = _sample(arr, X - origin[0], Y - origin[1])
    alpha = pm[..., 3]
    # 和身体图重复的像素（剪下来的身体碎片）去掉
    rgb = np.where(alpha[..., None] > 1e-4, pm[..., :3] / np.maximum(alpha[..., None], 1e-4), 0)
    colors = np.concatenate([rgb * 255, alpha[..., None] * 255], axis=-1).reshape(-1, 4).astype(np.uint8)
    dup = _body_hit(body, bpos, X.ravel(), Y.ravel(), colors).reshape(alpha.shape)
    pm[dup] = 0
    alpha = pm[..., 3]
    # 关节点标得稍微偏一点没关系：按肢体中段笔画的实际位置把它挪回中心线上
    midcols = (us > u_end * 0.3) & (us < u_end * 0.7)
    sub = alpha[:, midcols] > 0.4
    if sub.any():
        off = int(round(float((vs[:, None] * sub).sum() / sub.sum())))
        if off:
            pm = np.roll(pm, -off, axis=0)
            if off > 0:
                pm[-off:] = 0
            else:
                pm[:-off] = 0
            alpha = pm[..., 3]
    # 每一列（沿骨头的每个位置）有多宽
    solid = alpha > 0.4
    c = len(vs) // 2

    def core_half(col: np.ndarray) -> float:
        """这一列里，压在骨头（中心线）上的那一段连续笔画有多宽（只量连着中心线的那一段，旁边的碎片不算）。"""
        start = next((c + k for k in (0, 1, -1, 2, -2, 3, -3) if 0 <= c + k < len(col) and col[c + k]), None)
        if start is None:
            return 0.0
        lo = hi = start
        while lo - 1 >= 0 and col[lo - 1]:
            lo -= 1
        while hi + 1 < len(col) and col[hi + 1]:
            hi += 1
        return float(max(abs(vs[lo]), abs(vs[hi])))

    width = np.array([core_half(solid[:, i]) for i in range(len(us))])
    mid = width[(us > u_end * 0.25) & (us < u_end * 0.75) & (width > 0)]
    # 线条半宽：取中段偏细的那一档（中段偶尔夹着没抠掉的白底，会把宽度撑大）
    stroke = float(np.percentile(mid, 25)) if mid.size else max(3.0, half * 0.3)
    # 肢体本身从哪儿开始：越过身体下沿 / 轮廓后，第一段宽度正常（不比线条粗太多）的地方
    u0 = None
    for i, u in enumerate(us):
        if u < floor_u or width[i] == 0:
            continue
        seg = width[i:i + 4]
        if seg.size and (seg > 0).all() and (seg <= stroke * 1.5 + 1).all():
            u0 = i
            break
    if u0 is None:
        return None
    pm[:, :u0] = 0
    if tube:
        # 手臂是一根“管子”：从根到手腕，管子里有缺口（剪部件留下的 V 形缺口）就用旁边完整的截面补上；
        # 管子外面多出来的东西（剪歪的碎片、三角形空白）去掉。手那一团不动
        # 手的半径：在手心（u = u_end）那里量
        at_hand = width[np.abs(us - u_end) <= 4]
        hr = float(at_hand.max()) if at_hand.size and at_hand.max() > 0 else stroke * 2
        hand_from = u_end - hr * 1.15
        band = np.abs(vs) <= stroke + 1
        cover = (pm[band][:, :, 3] > 0.4).mean(axis=0) if band.any() else np.zeros(len(us))
        good = [i for i in range(u0, len(us)) if us[i] < hand_from and cover[i] >= 0.85]
        for i in range(u0, len(us)):
            if us[i] >= hand_from:
                break
            if cover[i] < 0.85 and good:
                j = min(good, key=lambda g: abs(g - i))
                pm[band, i] = pm[band, j]
            pm[~band, i] = 0
    # 线条边上残留的白纸底（抠图时没抠干净，贴着身体时像一道白缝）：去掉。手 / 鞋那一团不动
    a_now = pm[..., 3:4]
    rgb_now = np.where(a_now > 1e-4, pm[..., :3] / np.maximum(a_now, 1e-4), 0)
    near_white = (rgb_now > 0.78).all(axis=-1) & (pm[..., 3] > 0.02)
    at_edge = np.abs(vs)[:, None] > stroke * 0.5
    keep_end = u_end - (float(width[np.abs(us - u_end) <= 4].max()) * 1.15 if (np.abs(us - u_end) <= 4).any() else 0)
    region = (us < (keep_end if tube else u_end * 0.6))[None, :]
    pm[near_white & at_edge & region] = 0
    # 根部这一小截附近，比线条宽出去的东西也是碎片
    near = slice(u0, min(len(us), u0 + int(stroke * 2) + 1))
    wide = np.abs(vs)[:, None] > stroke * 1.4 + 1
    pm[:, near][np.broadcast_to(wide, pm[:, near].shape[:2])] = 0
    # 往身体里延伸：用 u0 处的截面（只要线条本身那么宽）往回铺，末端圆头
    e = max(6.0, stroke * 1.6)
    ne = int(math.ceil(e + us[u0]))
    # 延伸用的截面：取根部往外一小段的“典型”截面（逐像素取中位数），比只取一列稳
    cross = np.median(pm[:, u0:min(len(us), u0 + max(3, int(stroke * 2)))], axis=1).astype(np.float32)
    cross[np.abs(vs) > stroke] = 0
    cross[cross[:, 3] < 0.5] = 0   # 边上半透明的浅色像素不要，延伸出去的那截干干净净
    ext = np.repeat(cross[:, None, :], ne, axis=1)
    r = stroke + 0.5
    ex = np.arange(ne)[None, :]
    cap = (ex < r) & (((ex - r) ** 2 + vs[:, None] ** 2) > r * r)
    ext[cap] = 0
    body_part = pm[:, u0:]
    full = np.concatenate([ext, body_part], axis=1)
    # 只留连成一整条的那一块（零星的小碎点去掉）
    m = full[..., 3] > 0.08
    if m.any():
        cy = full.shape[0] // 2
        seed = np.zeros_like(m)
        seed[max(0, cy - 2):cy + 3, :] = m[max(0, cy - 2):cy + 3, :]
        comp = seed
        for _ in range(full.shape[0] + full.shape[1]):
            g = comp.copy()
            g[1:] |= comp[:-1]
            g[:-1] |= comp[1:]
            g[:, 1:] |= comp[:, :-1]
            g[:, :-1] |= comp[:, 1:]
            g &= m
            if (g == comp).all():
                break
            comp = g
        full[~comp] = 0
    # 去掉末尾空白
    cols = np.nonzero(full[..., 3].max(axis=0) > 0.02)[0]
    full = full[:, :cols.max() + 2] if cols.size else full
    rows = np.nonzero(full[..., 3].max(axis=1) > 0.02)[0]
    if rows.size == 0:
        return None
    hh = int(max(abs(vs[rows.min()]), abs(vs[rows.max()]))) + 2
    full = full[max(0, c - hh):c + hh + 1]
    a = full[..., 3:4]
    rgb = np.where(a > 1e-4, full[..., :3] / np.maximum(a, 1e-4), 0)
    img = Image.fromarray((np.clip(np.concatenate([rgb, a], axis=-1), 0, 1) * 255).astype(np.uint8), "RGBA")
    # 长条坐标：x = 0 是延伸部分的最左端，关节（u = 0）在 x = ne - us[u0] = e
    return Strip(img, ne - float(us[u0]), float(hh), float(u_end))


def _bent_path(S, E, H, b: float):
    """肩 → 肘 → 手 的中心线（手肘处圆滑过渡），返回 path(u) -> (x, y, 法线x, 法线y)，以及总长。"""
    Lu = math.dist(S, E)
    Ll = math.dist(E, H)
    d1 = _unit(E[0] - S[0], E[1] - S[1])
    d2 = _unit(H[0] - E[0], H[1] - E[1])
    b = min(b, Lu * 0.45, Ll * 0.45)

    def pt(u):
        if u <= Lu:
            return S[0] + d1[0] * u, S[1] + d1[1] * u
        return E[0] + d2[0] * (u - Lu), E[1] + d2[1] * (u - Lu)

    def at(u):
        if b > 0 and Lu - b < u < Lu + b:
            p0, p2 = pt(Lu - b), pt(Lu + b)
            t = (u - (Lu - b)) / (2 * b)
            return ((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * E[0] + t * t * p2[0],
                    (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * E[1] + t * t * p2[1])
        return pt(u)

    def path(u):
        x, y = at(u)
        x1, y1 = at(u + 0.5)
        x0, y0 = at(u - 0.5)
        tx, ty = _unit(x1 - x0, y1 - y0)
        return x, y, -ty, tx

    return path, Lu + Ll


class ArmRig:
    """一条手臂：一整条长条 + 两根骨头（上臂、下臂）。"""

    def __init__(self, strip: Strip, S, E, H0, Hs, K, L):
        self.strip = strip
        self.K = K
        self.S = L(*S)                                # 肩膀（逻辑坐标）
        self.Lu = math.dist(S, E) * K                 # 上臂长
        self.Ls = (math.dist(S, E) + math.dist(E, H0)) * K   # 整条手臂长（肩膀到手心，按原图）
        self.a_up0 = math.atan2(E[1] - S[1], E[0] - S[0])     # 原图里上臂的方向
        self.a_lo0 = math.atan2(H0[1] - E[1], H0[0] - E[0])   # 原图里下臂的方向
        self.a_str = math.atan2(Hs[1] - S[1], Hs[0] - S[0])   # 伸直时的方向

    def chain(self, deg: float, s: float, scale: float):
        """按转角 deg（度）和伸直程度 s（0 = 原图的弯法，1 = 伸直）算出骨头：返回 (肩, 肘, 上臂方向, 下臂方向)。"""
        r = math.radians(deg)
        up = _lerp_angle(self.a_up0, self.a_str, s) + r
        lo = _lerp_angle(self.a_lo0, self.a_str, s) + r
        sx, sy = self.S
        ex, ey = sx + math.cos(up) * self.Lu * scale, sy + math.sin(up) * self.Lu * scale
        return (sx, sy), (ex, ey), up, lo

    def point(self, u: float, deg: float, s: float, scale: float):
        """长条上第 u（逻辑长度，从肩膀算）处在身体坐标里的位置；手肘附近做成圆滑的弯。"""
        (sx, sy), (ex, ey), up, lo = self.chain(deg, s, scale)
        Lu = self.Lu * scale
        u = u * scale
        b = min(Lu * 0.45, max(4.0, self.strip.half * self.K * 1.4 * scale))   # 手肘圆弯的范围

        def straight(t):
            if t <= Lu:
                return sx + math.cos(up) * t, sy + math.sin(up) * t
            return ex + math.cos(lo) * (t - Lu), ey + math.sin(lo) * (t - Lu)

        if Lu - b < u < Lu + b:
            p0, p2 = straight(Lu - b), straight(Lu + b)
            t = (u - (Lu - b)) / (2 * b)
            return ((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * ex + t * t * p2[0],
                    (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * ey + t * t * p2[1])
        return straight(u)

    def hand(self, deg: float, s: float, scale: float):
        return self.point(self.Ls, deg, s, scale)

    def draw(self, cv, base: Mat, deg: float, s: float, scale: float, alpha: float = 1.0):
        half = self.strip.half * self.K * scale
        for sp, a, b in self.strip.sprites:
            ua, ub = a * self.K, b * self.K
            pa = self.point(ua, deg, s, scale)
            pb = self.point(ub, deg, s, scale)
            dx, dy = pb[0] - pa[0], pb[1] - pa[1]
            seg = (ub - ua) or 1e-6
            tx, ty = dx / seg, dy / seg                      # 这一段的方向（含拉伸）
            nx, ny = _unit(-dy, dx)
            m = Mat(tx, nx * scale, pa[0], ty, ny * scale, pa[1])
            cv.blit(sp, base.mul(m), alpha)
        _ = half
        return self.hand(deg, s, scale)


class LegRig:
    """一条腿：从胯到脚的一整条，绕胯转。"""

    def __init__(self, strip: Strip, hip, foot, K, L):
        self.strip = strip
        self.hip = L(*hip)
        self.angle = math.degrees(math.atan2(foot[1] - hip[1], foot[0] - hip[0]))

    def matrix(self, m: Mat, ang: float, lift: float, dy: float, scale_y: float) -> Mat:
        hx, hy = self.hip
        return (m.translate(0, dy).translate(hx, hy - lift).rotate(ang).scale(1, scale_y).rotate(self.angle))


def _grow(m: np.ndarray, r: int) -> np.ndarray:
    out = m.copy()
    for _ in range(r):
        n = out.copy()
        n[1:] |= out[:-1]
        n[:-1] |= out[1:]
        n[:, 1:] |= out[:, :-1]
        n[:, :-1] |= out[:, 1:]
        out = n
    return out


def _shrink(m: np.ndarray, r: int) -> np.ndarray:
    return ~_grow(~m, r)


def _extreme_points(mask: np.ndarray) -> list:
    """每一行最左、最右的点（凸包只需要这些）。"""
    out = []
    for y in range(mask.shape[0]):
        xs = np.nonzero(mask[y])[0]
        if xs.size:
            out += [(int(xs[0]), y), (int(xs[-1]), y)]
    return out


def _convex_hull(pts: list) -> list:
    pts = sorted(set(pts))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def repair_body(rig) -> None:
    """把身体图两侧被剪出来的“缺口 / 凹陷”补上，轮廓线断了的地方接上（默认形象和上传的形象都适用）。

    剪手臂时身体图在肩膀那里常被挖掉一个半圆，手一抬就露出缺口，看着像手和身体分开了。
    做法：只在两个肩膀附近，把身体的外形“闭合”一下（补平缺口），补出来的像素用旁边最近的颜色；
    如果这幅画是有黑色轮廓线的，再把肩膀附近缺了轮廓线的边描回去。"""
    body = rig.src.get("body")
    arms = (rig.data.get("arms") or {})
    if body is None or not arms:
        return
    a = np.array(body).astype(np.float32)
    mask = a[..., 3] > 100
    bx, by = rig.pos["body"]
    arm_w = float(rig.data.get("arm_width") or 40)
    rc = int(max(8, min(64, arm_w * 0.95)))
    # 肩膀那个角被剪成了一个凹进去的弧：在肩膀上下一段里，用身体外形的“凸包”（像用橡皮筋箍住）把角补回来。
    # 只用这一段的轮廓算，凸包在这一段的上下两头正好贴着原来的外形，补完不会有台阶
    yy, xx = np.mgrid[0:mask.shape[0], 0:mask.shape[1]]
    closed = np.zeros_like(mask)
    near = np.zeros_like(mask)
    cols = np.nonzero(mask.any(axis=0))[0]
    mid_x = (cols.min() + cols.max()) / 2 if cols.size else mask.shape[1] / 2
    joints = rig.data.get("joints") or {}
    for side in ("l", "r"):
        if side not in arms:
            continue
        sx, sy = arms[side]["shoulder"]
        # 手臂在原图里贴着身体走过的那一整段（肩膀到手）都可能被剪出凹陷：这一段都补
        ys_arm = [sy, arms[side]["elbow"][1], (joints.get(f"hand_{side}") or arms[side]["hand"])[1]]
        y0 = int(max(0, min(ys_arm) - by - rc * 1.6))
        # 往下一直补到身体最下沿（裙摆 / 衣服下摆两侧也不能有凹进去的地方）
        rows_any = np.nonzero(mask.any(axis=1))[0]
        y1 = int(rows_any.max()) + 1 if rows_any.size else int(min(mask.shape[0], max(ys_arm) - by + rc * 1.6))
        if y1 - y0 < 4:
            continue
        sub = np.zeros_like(mask)
        sub[y0:y1] = mask[y0:y1]
        hull = _convex_hull(_extreme_points(sub))
        if len(hull) < 3:
            continue
        hm = Image.new("L", (mask.shape[1], mask.shape[0]), 0)
        ImageDraw.Draw(hm).polygon(hull, fill=255)
        h = np.asarray(hm) > 0
        half = (xx < mid_x) if side == "l" else (xx >= mid_x)
        rows = (yy >= y0) & (yy < y1)
        closed |= h & half & rows
        near |= half & rows
    # 只补离原来外形不远（不超过 rc）的凹陷：形象本身就有的大弯（比如腰身）不动
    add = closed & ~mask & near & _grow(np.pad(mask, rc), rc)[rc:-rc, rc:-rc]
    lum0 = a[..., :3].mean(axis=-1)
    inner0 = _shrink(np.pad(mask, 1), 1)[1:-1, 1:-1]
    edge0 = mask & ~inner0
    outlined = edge0.sum() > 50 and np.median(lum0[edge0]) < 90
    dark0 = a[edge0 & (lum0 < 90)][:, :3]
    ink = np.median(dark0, axis=0) if dark0.size else np.array([25, 20, 22], dtype=np.float32)
    if add.any() and outlined:
        # 有轮廓线的画：补出来的这一角就在边上，直接用轮廓线的颜色
        a[add, :3] = ink
        a[add, 3] = 255
        mask = mask | add
    elif add.any():
        # 补出来的像素：一圈一圈往里取旁边已有像素的平均颜色
        filled = mask.copy()
        todo = add.copy()
        for _ in range(rc * 3):
            if not todo.any():
                break
            acc = np.zeros(a.shape, dtype=np.float32)
            cnt = np.zeros(mask.shape, dtype=np.float32)
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                src_ok = np.roll(filled, (dy, dx), axis=(0, 1))
                acc += np.roll(a, (dy, dx), axis=(0, 1)) * src_ok[..., None]
                cnt += src_ok
            ready = todo & (cnt > 0)
            a[ready] = acc[ready] / cnt[ready][:, None]
            a[ready, 3] = 255
            filled |= ready
            todo &= ~ready
        mask = filled
    # 有黑色轮廓线的画：肩膀附近（往下多一截）的外边缘如果不是深色，描回轮廓线
    if outlined:
        lum = a[..., :3].mean(axis=-1)
        wide = near
        t = 1
        prev = mask
        for k in range(1, 21):
            cur = _shrink(np.pad(mask, k), k)[k:-k, k:-k]
            ring = prev & ~cur & ~wide        # 用别处（没被剪过）的边量轮廓线粗细
            prev = cur
            if ring.sum() == 0 or (lum[ring] < 90).mean() < 0.6:
                break
            t = k
        inner_t = _shrink(np.pad(mask, t), t)[t:-t, t:-t]
        band = mask & ~inner_t & wide
        fix = band & (lum >= 90)
        a[fix, :3] = ink
    rig.src["body"] = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGBA")


def build_limbs(rig) -> tuple[dict, dict]:
    """给一个形象建好手臂和腿（建不出来的返回空，画的时候退回原来的分段画法）。"""
    d = rig.data
    try:
        repair_body(rig)
    except Exception:  # noqa: BLE001 —— 补不了就用原图
        pass
    body = rig.src.get("body")
    bpos = rig.pos.get("body", (0, 0))
    arms, legs = {}, {}
    joints = d.get("joints") or {}
    arm_w = float(d.get("arm_width") or 0)
    for side in ("l", "r"):
        a = (d.get("arms") or {}).get(side)
        if not a:
            continue
        arr, origin = _composite(rig.src, rig.pos, [f"arm_{side}_up", f"arm_{side}_lo", f"arm_{side}_cap"])
        if arr is None:
            continue
        S, E = tuple(a["shoulder"]), tuple(a["elbow"])
        Hs = tuple(a.get("hand") or joints.get(f"hand_{side}"))
        H0 = tuple(joints.get(f"hand_{side}") or Hs)
        half = max(30.0, arm_w * 0.9 if arm_w else 40.0)
        path, L = _bent_path(S, E, H0, b=max(6.0, (arm_w or 30) * 0.4))
        st = _straighten(arr, origin, path, L, half, 0.0, body, bpos, end_margin=half * 1.5, tube=True)
        if st is not None:
            arms[side] = ArmRig(st, S, E, H0, Hs, rig.K, rig.L)
    for side in ("l", "r"):
        hip = d.get(f"hip_{side}")
        for name in (f"leg_{side}", f"leg_{side}_front"):
            if not hip or name not in rig.src:
                continue
            src = rig.src[name]
            m = np.asarray(src)[..., 3] > 60
            ys, xs = np.nonzero(m)
            if ys.size == 0:
                continue
            low = ys >= ys.max() - max(6, int((ys.max() - ys.min()) * 0.15))
            foot = (float(xs[low].mean()) + rig.pos[name][0], float(ys[low].mean()) + rig.pos[name][1])
            hip_t = tuple(hip)
            axis = _unit(foot[0] - hip_t[0], foot[1] - hip_t[1])
            # 身体（裙子 / 上衣）在胯这一列的最下沿：腿从这里往下才是腿本身
            floor_u = 0.0
            if body is not None:
                ba = np.asarray(body)[..., 3] > 100
                cx = int(round(hip_t[0] - bpos[0]))
                cols = ba[:, max(0, cx - 3):cx + 4]
                rows = np.nonzero(cols.any(axis=1))[0] if cols.size else np.array([])
                if rows.size:
                    floor_u = max(0.0, (rows.max() + bpos[1] - hip_t[1]) / max(0.3, axis[1]))
            arr, origin = _composite(rig.src, rig.pos, [name])
            L = math.dist(hip_t, foot)

            def path(u, h=hip_t, ax=axis):
                return h[0] + ax[0] * u, h[1] + ax[1] * u, -ax[1], ax[0]

            half = max(30.0, float(np.abs((xs + rig.pos[name][0] - hip_t[0]) * -axis[1]
                                          + (ys + rig.pos[name][1] - hip_t[1]) * axis[0]).max()) * 0.0 + 60.0)
            st = _straighten(arr, origin, path, L, half, floor_u, body, bpos, end_margin=40.0)
            if st is not None:
                legs[name] = LegRig(st, hip_t, foot, rig.K, rig.L)
    return arms, legs
