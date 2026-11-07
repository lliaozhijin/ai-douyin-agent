"""把桌宠画出来：部件组装、手机 / 小牌子、主人的手、萌系特效、粉色对话框（移植自原版 Painter.cs）。"""
from __future__ import annotations

import math

from . import gfx
from .engine import (BANG, CHAT, DOTS, HEART, NOTE, PUFF, QUES, SPARK, STAR, SWEAT, TEAR, ZZZ, Pet, Pose, clamp,
                     ease_out_back, smooth)
from .gfx import Canvas, Mat
from .rig import Rig

W, H = 400.0, 480.0     # 窗口大小（逻辑单位）
OX, OY = 200.0, 464.0   # 脚底在窗口里的位置
ARM_FRONT = 92.0        # 手臂举过这个角度时画到脑袋前面

STAR_COLORS = [(255, 222, 90), (255, 150, 190), (140, 205, 255), (190, 160, 255)]


def _part(cv: Canvas, rig: Rig, name: str, m: Mat, alpha: float = 1.0) -> None:
    cv.blit(rig.sprites.get(name), m, alpha)


def _arm(cv: Canvas, rig: Rig, side: str, base: Mat, deg: float, s: float, scale: float):
    """画一条手臂，返回手的位置（逻辑坐标）。"""
    limb = rig.limb_arms.get(side) if hasattr(rig, "limb_arms") else None
    if limb is not None:  # 整条手臂顺着骨头弯着画：关节处不会裂开
        hx, hy = limb.draw(cv, base, deg, s, scale)
        return base.apply(hx, hy)
    a = rig.arms[side]
    sx, sy = a["shoulder"]
    ma = base.translate(sx, sy).rotate(deg).scale(scale, scale).translate(-sx, -sy)
    full = smooth((s - 0.75) / 0.22)
    if full < 0.999:
        mb = ma.about(sx, sy, a["adj"] * s) if a["adj"] else ma
        ex, ey = a["elbow"]
        lo = mb.about(ex, ey, a["straight"] * s)
        _part(cv, rig, f"arm_{side}_cap", lo, clamp((s - 0.15) / 0.5, 0, 1))
        _part(cv, rig, f"arm_{side}_lo", lo)
        _part(cv, rig, f"arm_{side}_up", mb)
    if full > 0.001:
        _part(cv, rig, f"arm_{side}_straight", ma, full)
    return ma.apply(*a["hand"])


def draw_pet(cv: Canvas, rig: Rig, p: Pose, t: float, face=None) -> None:
    res = cv.dev
    fy = rig.fy
    if p.Shadow:
        k = 1 - min(0.5, -p.BodyY / 80)
        cv.blit(gfx.shadow(res), Mat().translate(p.ShadowX, 0).scale(k * p.ShadowW, 1), k)
    m = Mat()
    if p.LieE > 0:  # 朝左边倒下：整只转 -90°，同时往右、往上挪，侧身贴在任务栏上
        m = m.translate(150 * p.LieE * fy, -88 * p.LieE * fy).rotate(-90 * p.LieE)
    if p.Rot:
        m = m.about(p.Pivot[0], p.Pivot[1], p.Rot)
    m = m.translate(p.BodyX, p.BodyY).rotate(p.Tilt).scale(p.SquashX, p.SquashY)

    def leg(name, hip, ang, lift):
        limb = rig.limb_legs.get(name) if hasattr(rig, "limb_legs") else None
        if limb is not None:  # 从胯到脚一整条，上端藏在身体里
            cv.blit(limb.strip.whole, limb.matrix(m, ang, lift, p.LegDY, p.LegScaleY))
            return
        hx, hy = hip
        _part(cv, rig, name, m.translate(0, p.LegDY).translate(hx, hy - lift).rotate(ang)
              .scale(1, p.LegScaleY).translate(-hx, -hy))

    if not p.LegsFront:
        leg("leg_l", rig.hip_l, p.LegL, p.LiftL)
        leg("leg_r", rig.hip_r, -p.LegR, p.LiftR)
    _part(cv, rig, rig.body_name(face.skirt.x) if face else "body", m, p.BodyAlpha)
    if p.LegsFront:
        leg("leg_l_front" if "leg_l_front" in rig.sprites else "leg_l", rig.hip_l, p.LegL, p.LiftL)
        leg("leg_r_front" if "leg_r_front" in rig.sprites else "leg_r", rig.hip_r, -p.LegR, p.LiftR)
    front_l = p.StrL > 0.5 and p.ArmL > ARM_FRONT
    front_r = p.StrR > 0.5 and p.ArmR > ARM_FRONT
    am = m.translate(0, p.ArmDY)
    hands = {}

    def arms(left: bool, right: bool):
        if left:
            hands["l"] = _arm(cv, rig, "l", am, p.ArmL, p.StrL, p.ArmScale)
        if right:
            hands["r"] = _arm(cv, rig, "r", am, -p.ArmR, p.StrR, p.ArmScale)

    arms(not front_l, not front_r)
    if p.Phone and "l" in hands and "r" in hands:  # 手机捧在两只手中间
        hx = (hands["l"][0] + hands["r"][0]) / 2
        hy = (hands["l"][1] + hands["r"][1]) / 2 - 6 * fy
        cv.blit(gfx.phone(res), Mat().translate(hx, hy).rotate(math.sin(t * 0.8) * 3).scale(fy * 1.05))
    nx, ny = rig.neck
    mh = (m.translate(nx + p.HeadDX, ny + p.HeadDY).rotate(p.HeadTilt)
          .scale(1 + (1 - p.HeadSquash) * 0.6, p.HeadSquash).translate(-nx, -ny))
    # 辫子、呆毛画在头后面，绕各自的支点甩动
    for name, (px, py) in rig.sway.items():
        spring = getattr(face, name, None) if face else None
        _part(cv, rig, name, mh.about(px, py, spring.x if spring else 0.0))
    _part(cv, rig, "head", mh)
    if face is not None:  # 表情贴片盖在原来的眼睛 / 嘴巴上
        if face.eyes != "open":
            _part(cv, rig, f"eyes_{face.eyes}", mh)
        if face.mouth != "smile":
            _part(cv, rig, f"mouth_{face.mouth}", mh)
    arms(front_l, front_r)
    if p.Sign:
        hand = hands.get("r") or _arm_hand(rig, "r", am, -p.ArmR, p.ArmScale)
        cv.blit(gfx.sign(res), Mat().translate(hand[0], hand[1] + 6).rotate(10 + math.sin(t * 2.2) * 5).scale(fy * 0.9))
    head_top = ny + p.HeadDY - 160 * p.HeadSquash * fy
    if p.Anger:
        s = 1 + abs(math.sin(t * 8)) * 0.18
        cv.blit(gfx.anger(res), m.translate(72 * rig.fx, head_top + 26 * fy).scale(s))
    if p.PatHandT >= 0:
        _pat_hand(cv, m, p.PatHandT, head_top, rig)
    if p.Dizzy:
        for i in range(3):
            ang = t * 4 + i * 2.094
            x, y = math.cos(ang) * 62 * rig.fx, head_top - 6 + math.sin(ang) * 12
            sz = (7.5 + math.sin(ang) * 1.5) / 10
            cv.blit(gfx.star(res), m.translate(x, y).rotate(math.degrees(t * 3)).scale(sz))


def _arm_hand(rig: Rig, side: str, base: Mat, deg: float, scale: float):
    limb = rig.limb_arms.get(side) if hasattr(rig, "limb_arms") else None
    if limb is not None:
        return base.apply(*limb.hand(deg, 1.0, scale))
    a = rig.arms[side]
    sx, sy = a["shoulder"]
    return base.translate(sx, sy).rotate(deg).scale(scale, scale).translate(-sx, -sy).apply(*a["hand"])


def _pat_hand(cv: Canvas, m: Mat, pt: float, head_top: float, rig: Rig) -> None:
    appear = smooth(pt / 0.22) * smooth((2.2 - pt) / 0.25)
    if appear <= 0.001:
        return
    stroke = math.sin(pt * 7)
    press = abs(math.sin(pt * 7))
    hx = stroke * 28 * rig.fx + 6 + (1 - appear) * 70
    hy = head_top + 30 * rig.fy + press * 5 - (1 - appear) * 70
    cv.blit(gfx.pat_hand(cv.dev), m.translate(hx, hy).rotate(-32 + stroke * 9).scale(1.15 * rig.fy), appear)


def draw_particles(cv: Canvas, parts, t: float) -> None:
    res = cv.dev
    for q in parts:
        lk = q.life / q.max
        al = clamp(min(lk * 3, (1 - lk) * 10), 0, 1)
        pop = ease_out_back(clamp((1 - lk) * 6, 0, 1))
        k = q.kind
        base = Mat().translate(q.x, q.y)
        if k == HEART:
            cv.blit(gfx.heart(res), base.scale(q.size * pop / 10), al)
        elif k == SPARK:
            cv.blit(gfx.star(res, (255, 240, 150), 4, 0.28, False), base.scale(q.size * pop / 10), al)
        elif k == STAR:
            c = STAR_COLORS[int(q.seed * 3.99)]
            cv.blit(gfx.star(res, c), base.rotate(math.degrees(t * 4 * (q.seed - 0.5))).scale(q.size * pop / 10), al)
        elif k in (SWEAT, TEAR):
            cv.blit(gfx.drop(res), base.scale(q.size / 10), al)
        elif k == PUFF:
            s = q.size * (0.6 + (1 - lk) * 0.9)
            cv.blit(gfx.puff(res), base.scale(s / 10), al * 0.8)
        elif k in (BANG, QUES):
            col = (255, 110, 150) if k == BANG else (120, 160, 255)
            cv.blit(gfx.glyph(res, "!" if k == BANG else "?", col), base.rotate(math.sin(t * 6) * 10)
                    .scale(q.size * pop / 26), al)
        elif k == ZZZ:
            cv.blit(gfx.glyph(res, "Z", (130, 140, 220)), base.rotate(-12).scale(q.size * (0.7 + (1 - lk) * 0.5) / 26),
                    al)
        elif k == NOTE:
            col = (255, 120, 170) if q.seed < 0.5 else (110, 170, 255)
            cv.blit(gfx.note(res, col), base.rotate(math.sin(t * 4 + q.seed * 6) * 12).scale(q.size * pop), al)
        elif k == CHAT:
            cv.blit(gfx.chat_icon(res), base.scale(q.size * pop / 10), al)
        elif k == DOTS:
            cv.blit(gfx.glyph(res, "…", (222, 70, 132), 18), base.scale(pop), al)


def draw_bubble(cv: Canvas, pet: Pet, rig: Rig, p: Pose, t: float, min_x: float, max_x: float) -> None:
    text, bt, dur = pet.bubble_text, pet.bubble_t, pet.bubble_dur
    if not text:
        return
    ap = clamp(bt / 0.3, 0, 1)
    sc = 0.4 + 0.6 * ease_out_back(ap)
    out_k = clamp((dur - bt) / 0.3, 0, 1)
    sc *= 0.85 + 0.15 * out_k
    fade = out_k * clamp(bt / 0.08, 0, 1)
    if fade <= 0.01:
        return
    shown = int(bt * 20) + 1
    sp, bw, bh = gfx.bubble(text, min(shown, len(text)), cv.dev, colors=tuple(pet.bubble_colors))
    tip_x = 8 * rig.fx - 70 * p.LieE
    tip_y = rig.head_top - 10.5 * rig.fy + 120 * p.LieE + math.sin(t * 2.4) * 2
    # 尾巴在对话框左边 28 处；对话框超出窗口时整体挪回来（尾巴跟着挪一点，仍指向她）
    bx = tip_x - 28
    shift = 0.0
    if bx + bw + 14 > max_x:
        shift = max_x - (bx + bw + 14)
    if bx + shift < min_x + 2:
        shift = min_x + 2 - bx
    cv.blit(sp, Mat().translate(tip_x, tip_y).scale(sc).translate(shift, 0), fade)


def render(pet: Pet, rig: Rig, dev: float) -> Canvas:
    """画一整帧：宠物 + 特效 + 对话框。返回画布（cv.img 是 RGBA 图）。"""
    rig.prepare(dev)
    cv = Canvas(W, H, OX, OY, dev)
    p = pet.pose()
    draw_pet(cv, rig, p, pet.t, getattr(pet, "face", None) if rig.lively else None)
    draw_particles(cv, pet.parts, pet.t)
    if pet.sink > 0.5:
        cv.clear_below(-pet.sink)
    draw_bubble(cv, pet, rig, p, pet.t, -OX, W - OX)
    return cv


def hit_head(rig: Rig, pet: Pet, lx: float, ly: float) -> bool:
    lx, ly = _unlie(pet, rig, lx, ly)
    hd = pet.last_pose.HeadDY
    dx = (lx - rig.head_cx) / rig.head_rx
    dy = (ly - hd - rig.head_cy) / rig.head_ry
    return dx * dx + dy * dy <= 1 and ly < -pet.sink


def hit_body(rig: Rig, pet: Pet, lx: float, ly: float) -> bool:
    return (pet.last_pose.BodyAlpha > 0.5 and rig.body_top < ly < 4 and abs(lx) < rig.body_half_w
            and ly < -pet.sink)


def _unlie(pet: Pet, rig: Rig, lx: float, ly: float):
    e = pet.last_pose.LieE
    if e <= 0:
        return lx, ly
    x, y = lx - 150 * e * rig.fy, ly + 88 * e * rig.fy
    r = math.radians(90 * e)
    return x * math.cos(r) - y * math.sin(r), x * math.sin(r) + y * math.cos(r)
