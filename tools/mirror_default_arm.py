"""默认形象的右手臂：原图里右手臂贴着衣服口袋画，剪下来时连带了衣服轮廓和白纸底，拼起来很粗、还夹着白色三角。
左手臂是干净的一根线，而且两边的关节位置正好左右对称，所以右手臂直接用左手臂镜像过来（开发时运行一次）：
    python tools/mirror_default_arm.py
"""
import json
from pathlib import Path

from PIL import Image, ImageOps

D = Path(__file__).resolve().parent.parent / "assets" / "pet" / "default"
rig = json.loads((D / "rig.json").read_text(encoding="utf-8"))
a_l, a_r = rig["arms"]["l"], rig["arms"]["r"]
cx = (a_l["shoulder"][0] + a_r["shoulder"][0]) / 2          # 左右对称的中线
dy = a_r["shoulder"][1] - a_l["shoulder"][1]
for part in ("up", "lo", "cap", "straight"):
    src = D / f"arm_l_{part}.png"
    if not src.exists():
        continue
    im = Image.open(src).convert("RGBA")
    x, y = rig["parts"][f"arm_l_{part}"]
    ImageOps.mirror(im).save(D / f"arm_r_{part}.png")
    rig["parts"][f"arm_r_{part}"] = [int(round(2 * cx - (x + im.width))), int(round(y + dy))]


def mx(p):
    return [round(2 * cx - p[0], 1), round(p[1] + dy, 1)]


a_r["shoulder"], a_r["elbow"], a_r["hand"] = mx(a_l["shoulder"]), mx(a_l["elbow"]), mx(a_l["hand"])
a_r["straight"], a_r["adj"] = -a_l["straight"], -a_l["adj"]
j = rig.get("joints") or {}
for k in ("shoulder", "elbow", "hand"):
    if f"{k}_l" in j:
        j[f"{k}_r"] = mx(j[f"{k}_l"])
(D / "rig.json").write_text(json.dumps(rig, ensure_ascii=False, indent=1), encoding="utf-8")
print("右手臂已按左手臂镜像：", {k: rig["parts"][k] for k in rig["parts"] if k.startswith("arm_r")})
