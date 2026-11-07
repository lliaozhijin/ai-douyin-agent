"""桌宠形象（部件 + 关节）。

一个形象是一个文件夹：rig.json（各部件在原图中的位置、关节点）+ 部件图片（head.png、body.png……）。
- 默认形象：assets/pet/default（粉裙子小女孩，由 autorig.py 拆好）。
- 你上传的形象：data/pet/custom（由 autorig.py 根据你拖好的关节点自动拆出来）。

逻辑坐标 = (原图坐标 - 脚底中心) × K；K 让整只宠物大约 300 个逻辑单位高。
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path

from PIL import Image

from ..paths import ASSETS_DIR, PET_DIR
from .gfx import Sprite

DEFAULT_DIR = ASSETS_DIR / "pet" / "default"
CUSTOM_DIR = PET_DIR / "custom"
RIG_NAMES = {"default": "默认形象（粉裙子小女孩）", "custom": "你上传的形象"}

PART_NAMES = ("head", "body", "leg_l", "leg_r", "leg_l_front", "leg_r_front",
              "arm_l_up", "arm_l_lo", "arm_l_cap", "arm_l_straight",
              "arm_r_up", "arm_r_lo", "arm_r_cap", "arm_r_straight",
              # 会甩动的头发（画在头后面，绕 rig.json 里 sway 的支点转）
              "tail_l", "tail_r", "ahoge",
              # 表情贴片：盖在脸上原来的眼睛 / 嘴巴上
              "eyes_closed", "eyes_happy", "eyes_half", "eyes_x",
              "mouth_talk", "mouth_open", "mouth_flat", "mouth_frown", "mouth_o")
SKIRT_STEPS = 3  # 裙摆左右各预先做 3 档摆动


def rig_dir(which: str) -> Path:
    if which == "custom" and (CUSTOM_DIR / "rig.json").exists():
        return CUSTOM_DIR
    return DEFAULT_DIR


log = logging.getLogger(__name__)


class Rig:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.data = json.loads((self.folder / "rig.json").read_text(encoding="utf-8"))
        d = self.data
        self.K = float(d["K"])
        self.gx, self.gy = d["ground"]
        self.src: dict[str, Image.Image] = {}
        for name, pos in d["parts"].items():
            f = self.folder / f"{name}.png"
            if f.exists():
                self.src[name] = Image.open(f).convert("RGBA")
        self.pos = {k: tuple(v) for k, v in d["parts"].items()}
        # 手臂、腿各画成一整条（顺着骨头弯），和身体之间不会裂开（默认形象和上传的形象都这样）
        from .limbs import build_limbs
        try:
            self.limb_arms, self.limb_legs = build_limbs(self)
        except Exception:  # noqa: BLE001 —— 建不出来就按原来的分段画法，不影响桌宠出来
            log.exception("手臂 / 腿整条化失败，按原来的画法")
            self.limb_arms, self.limb_legs = {}, {}
        self.neck = self.L(*d["neck"])
        self.hip_l, self.hip_r = self.L(*d["hip_l"]), self.L(*d["hip_r"])
        self.arms = {}
        for side in ("l", "r"):
            a = d["arms"][side]
            self.arms[side] = {
                "shoulder": self.L(*a["shoulder"]), "elbow": self.L(*a["elbow"]),
                "straight": float(a.get("straight", 0)), "adj": float(a.get("adj", 0)),
                "hand": self.L(*a["hand"]),
            }
        # 会甩动的部件：名字 -> 支点（逻辑坐标）
        self.sway = {k: self.L(*v["pivot"]) for k, v in (d.get("sway") or {}).items() if k in self.src}
        sk = d.get("skirt")
        self.skirt = dict(sk) if sk and "body" in self.src else None
        # 只有默认形象带这些“会动的小部件”（眨眼、嘴型、辫子、裙摆）；你上传的形象保持原来的画法
        self.lively = bool(self.sway or self.skirt or any(n.startswith(("eyes_", "mouth_")) for n in self.src))
        self._metrics()
        self._prepared_for = -1.0
        self.sprites: dict[str, Sprite] = {}

    def L(self, ix: float, iy: float) -> tuple[float, float]:
        return (ix - self.gx) * self.K, (iy - self.gy) * self.K

    def _metrics(self) -> None:
        """头的位置大小、身体范围：点击判断、特效位置、躲猫猫的深度都按这些算（默认雨雨和原版完全一致）。"""
        head = self.src.get("head")
        hx, hy = self.pos.get("head", (self.gx, self.gy - 600))
        bb = head.getchannel("A").point(lambda v: 255 if v > 40 else 0).getbbox() if head else None
        if bb:
            x0, y0 = self.L(hx + bb[0], hy + bb[1])
            x1, y1 = self.L(hx + bb[2], hy + bb[3])
        else:
            x0, y0, x1, y1 = -77, -301, 77, -141
        self.head_top = y0
        self.head_cx, self.head_cy = (x0 + x1) / 2, (y0 + y1) / 2
        self.head_rx, self.head_ry = max(20.0, (x1 - x0) / 2), max(20.0, (y1 - y0) / 2)
        # 特效位置原本按头顶 -301.5、头半宽 77 的形象调的；其他形象按比例换算
        self.fy = self.head_top / -301.5
        self.fx = self.head_rx / 77.0
        self.peek_depth = max(40.0, -self.neck[1] - 10.0)
        body = self.src.get("body")
        bx, by = self.pos.get("body", (self.gx - 100, self.gy - 300))
        bb = body.getchannel("A").getbbox() if body else None
        if bb:
            self.body_half_w = max(abs(self.L(bx + bb[0], 0)[0]), abs(self.L(bx + bb[2], 0)[0]))
            self.body_top = self.L(0, by + bb[1])[1]
        else:
            self.body_half_w, self.body_top = 82.0, -150.0

    def prepare(self, dev: float) -> None:
        """按当前屏幕缩放把部件预先缩放好（画的时候几乎不再缩放，清晰又省 CPU）。"""
        if abs(dev - self._prepared_for) < 1e-3:
            return
        self._prepared_for = dev
        self.sprites = {}
        k = self.K * dev
        for name, img in self.src.items():
            w, h = max(1, round(img.width * k)), max(1, round(img.height * k))
            ox, oy = self.L(*self.pos[name])
            small = img.resize((w, h), Image.LANCZOS)
            self.sprites[name] = Sprite(small, -ox * (w / img.width) / self.K, -oy * (h / img.height) / self.K, dev)
        if self.skirt:
            self._prepare_skirt(dev)
        for limb in list(self.limb_arms.values()) + list(self.limb_legs.values()):
            limb.strip.prepare(self.K, dev)

    def _prepare_skirt(self, dev: float) -> None:
        """裙摆摆动：把身体图从腰往下按行左右错开（越往下错得越多），左右各做几档，画的时候按摆动挑一档。"""
        import numpy as np

        img = self.src["body"]
        bx, by = self.pos["body"]
        top, hem = float(self.skirt["top"]), float(self.skirt["hem"])
        shift = float(self.skirt.get("shift", 20))
        pad = int(math.ceil(shift)) + 2
        a = np.asarray(img)
        h, w = a.shape[:2]
        ys = np.arange(h) + by
        weight = np.clip((ys - top) / max(1.0, hem - top), 0, 1) ** 1.5
        k = self.K * dev
        for i in range(-SKIRT_STEPS, SKIRT_STEPS + 1):
            out = np.zeros((h, w + 2 * pad, 4), dtype=a.dtype)
            for y in range(h):
                s = int(round(i / SKIRT_STEPS * shift * weight[y]))
                out[y, pad + s: pad + s + w] = a[y]
            big = Image.fromarray(out, "RGBA")
            sw, sh = max(1, round(big.width * k)), max(1, round(big.height * k))
            ox, oy = self.L(bx - pad, by)
            small = big.resize((sw, sh), Image.LANCZOS)
            self.sprites[f"body~{i}"] = Sprite(small, -ox * (sw / big.width) / self.K,
                                               -oy * (sh / big.height) / self.K, dev)

    def body_name(self, skirt: float) -> str:
        if not self.skirt:
            return "body"
        i = int(round(max(-1.0, min(1.0, skirt)) * SKIRT_STEPS))
        return f"body~{i}"

    def full_head(self) -> Image.Image | None:
        """头 + 辫子 + 呆毛拼回一张（头像、图标用）。"""
        head = self.src.get("head")
        if head is None:
            return None
        extra = [n for n in ("tail_l", "tail_r", "ahoge") if n in self.src]
        if not extra:
            return head
        hx, hy = self.pos["head"]
        boxes = [(hx, hy, hx + head.width, hy + head.height)]
        for n in extra:
            x, y = self.pos[n]
            boxes.append((x, y, x + self.src[n].width, y + self.src[n].height))
        x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
        x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
        canvas = Image.new("RGBA", (int(x1 - x0), int(y1 - y0)), (0, 0, 0, 0))
        for n in extra:
            x, y = self.pos[n]
            canvas.alpha_composite(self.src[n], (int(x - x0), int(y - y0)))
        canvas.alpha_composite(head, (int(hx - x0), int(hy - y0)))
        return canvas

    def icon(self, size: int = 64) -> Image.Image:
        head = self.full_head()
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        if head is None:
            return img
        bb = head.getbbox() or (0, 0, head.width, head.height)
        h = head.crop(bb)
        s = size / max(h.width, h.height)
        h = h.resize((max(1, int(h.width * s)), max(1, int(h.height * s))), Image.LANCZOS)
        img.alpha_composite(h, ((size - h.width) // 2, (size - h.height) // 2))
        return img


def load(which: str = "default") -> Rig:
    try:
        return Rig(rig_dir(which))
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return Rig(DEFAULT_DIR)


def angle_deg(dx: float, dy: float) -> float:
    return math.degrees(math.atan2(dy, dx))
