"""预览图：把桌宠的各种动作画成一张网格图（界面上“预览动作”、换形象后检查效果、测试都用它）。"""
from __future__ import annotations

import random

from PIL import Image, ImageDraw

from ..publish import fonts
from . import engine as E
from .render import H, W, render
from .rig import Rig

SHOTS = [
    ("站着", E.IDLE, 1.0, {}), ("跳舞", E.DANCE1, 1.3, {}), ("跳舞", E.DANCE2, 2.2, {}), ("挥手", E.WAVE, 1.0, {}),
    ("坐下", E.SIT, 3.0, {}), ("趴下", E.LIE, 3.0, {}), ("摸头", E.PAT, 0.6, {}), ("拎起来", E.DRAG, 1.0, {}),
    ("刷视频", E.PHONE, 2.0, {}), ("评论", E.TYPE, 1.0, {"say": "在评论：这只猫好可爱"}),
    ("想发作品", E.SIGN, 3.0, {}), ("小脾气", E.SULK, 2.0, {}),
]


def pose_grid(rig: Rig, scale: float = 0.6, cols: int = 4, shots=None) -> Image.Image:
    shots = shots or SHOTS
    cw, ch = int(W * scale), int(H * scale)
    rows = (len(shots) + cols - 1) // cols
    sheet = Image.new("RGBA", (cw * cols, (ch + 26) * rows), (255, 245, 250, 255))
    d = ImageDraw.Draw(sheet)
    for i, (label, act, at, opt) in enumerate(shots):
        pet = E.Pet(fx=rig.fx, fy=rig.fy, peek_depth=rig.peek_depth, rnd=random.Random(7))
        pet.chatter = False
        pet.set_act(act, 1e9 if act in (E.DRAG, E.PHONE, E.TYPE) else max(at + 2.0, 4.0))
        steps = int(at / 0.033)
        for _ in range(steps):  # 推进到这个动作的第 at 秒（手臂平滑过渡也跟着走完）
            pet.act_t += 0.033
            pet.t += 0.033
            pet._smooth_arms(0.033)
        pet.parts = []
        pet.face._blink_in = 99.0  # 预览图里不要正好在眨眼
        pet.last_pose = pet.pose()
        pet.face.update(pet, 0.033)  # 按这个动作换上对应的眼睛和嘴巴
        if opt.get("say"):
            pet.say(opt["say"], 10)
            pet.bubble_t = 2.0
        cv = render(pet, rig, scale)
        x, y = (i % cols) * cw, (i // cols) * (ch + 26)
        sheet.alpha_composite(cv.img, (x, y + 26))
        fonts.draw_text(d, (x + 8, y + 4), label, 18, (214, 51, 108, 255))
    return sheet
