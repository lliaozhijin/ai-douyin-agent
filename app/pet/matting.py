"""把你上传的形象图抠出来（去掉背景）。

按顺序尝试：
1. 图片本身就是透明背景的 PNG：直接用；
2. 本机抠图模型（skytnt/anime-seg 的 isnetis.onnx，Apache-2.0，约 176MB，第一次用时下载到 data\\models）；
3. 背景是纯色 / 接近纯色：从四周往里把背景色去掉。
都在本机完成，不上传图片，不需要配置任何 AI。
"""
from __future__ import annotations

import logging
import os


import requests
from PIL import Image, ImageDraw, ImageFilter, ImageOps

from ..paths import MODELS_DIR

log = logging.getLogger(__name__)

MODEL_FILE = MODELS_DIR / "isnetis.onnx"
MODEL_URLS = [
    "https://huggingface.co/skytnt/anime-seg/resolve/main/isnetis.onnx",
    "https://hf-mirror.com/skytnt/anime-seg/resolve/main/isnetis.onnx",  # 国内镜像
]
MODEL_MIN_BYTES = 100 * 1024 * 1024


class MattingError(Exception):
    pass


def onnx_available() -> bool:
    try:
        import onnxruntime  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def model_ready() -> bool:
    return MODEL_FILE.exists() and MODEL_FILE.stat().st_size > MODEL_MIN_BYTES


def download_model(progress=None, proxy: str = "") -> None:
    """下载抠图模型。progress(已下载字节, 总字节)。先试官方地址，不行换国内镜像。"""
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MODEL_FILE.with_suffix(".part")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    last = None
    for url in MODEL_URLS:
        try:
            with requests.get(url, stream=True, timeout=30, proxies=proxies) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length") or 0)
                done = 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1024 * 256):
                        f.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
            if tmp.stat().st_size < MODEL_MIN_BYTES:
                raise MattingError("下载的文件不完整")
            os.replace(tmp, MODEL_FILE)
            return
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("从 %s 下载抠图模型失败：%s", url, e)
    raise MattingError(f"抠图模型下载失败：{last}")


def has_alpha(img: Image.Image) -> bool:
    """图片自带透明背景（至少 5% 的像素是透明的）。"""
    if img.mode not in ("RGBA", "LA", "PA") and not (img.mode == "P" and "transparency" in img.info):
        return False
    a = img.convert("RGBA").getchannel("A")
    hist = a.histogram()
    return sum(hist[:16]) > 0.05 * a.width * a.height


def prepare(img: Image.Image, max_side: int = 1600) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    if max(img.size) > max_side:
        k = max_side / max(img.size)
        img = img.resize((int(img.width * k), int(img.height * k)), Image.LANCZOS)
    return img


def cutout(img: Image.Image, use_model: bool = True) -> tuple[Image.Image, str]:
    """返回 (抠好的 RGBA 图，裁到人物边缘并留一点边；用的方法)。"""
    img = prepare(img)
    if has_alpha(img):
        out, how = img.convert("RGBA"), "原图就是透明背景"
    elif use_model and onnx_available() and model_ready():
        out, how = _model_cut(img.convert("RGB")), "本机抠图模型"
    else:
        out, how = _flood_cut(img.convert("RGB")), "去掉纯色背景"
    a = out.getchannel("A")
    bbox = a.point(lambda v: 255 if v > 40 else 0).getbbox()
    if not bbox:
        raise MattingError("没能从图片里抠出人物：换一张背景简单一点的图，或者用透明背景的 PNG")
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if w * h < 0.03 * out.width * out.height:
        raise MattingError("抠出来的人物太小了，可能没认出来：换一张人物占画面大部分的图试试")
    m = 12
    box = (max(0, bbox[0] - m), max(0, bbox[1] - m), min(out.width, bbox[2] + m), min(out.height, bbox[3] + m))
    return out.crop(box), how


def _model_cut(img: Image.Image) -> Image.Image:
    import numpy as np
    import onnxruntime as ort

    sess = ort.InferenceSession(str(MODEL_FILE), providers=["CPUExecutionProvider"])
    s = 1024
    w, h = img.size
    if h > w:
        nh, nw = s, max(1, int(s * w / h))
    else:
        nh, nw = max(1, int(s * h / w)), s
    ph, pw = s - nh, s - nw
    arr = np.asarray(img.resize((nw, nh), Image.BILINEAR), dtype=np.float32) / 255.0
    inp = np.zeros((s, s, 3), dtype=np.float32)
    inp[ph // 2:ph // 2 + nh, pw // 2:pw // 2 + nw] = arr
    inp = inp.transpose(2, 0, 1)[None]
    mask = sess.run(None, {sess.get_inputs()[0].name: inp})[0][0]
    mask = np.squeeze(mask)[ph // 2:ph // 2 + nh, pw // 2:pw // 2 + nw]
    mask = np.clip((mask - 0.08) / 0.84, 0, 1)
    alpha = Image.fromarray((mask * 255).astype(np.uint8)).resize((w, h), Image.BILINEAR)
    out = img.convert("RGBA")
    out.putalpha(alpha)
    return out


def _flood_cut(img: Image.Image, thresh: int = 38) -> Image.Image:
    """从四周的边往里，把和背景颜色接近的连成一片的区域去掉。"""
    work = img.copy()
    marker = (1, 254, 3)
    w, h = work.size
    seeds = [(x, y) for x in range(0, w, max(1, w // 12)) for y in (0, h - 1)] + \
            [(x, y) for y in range(0, h, max(1, h // 12)) for x in (0, w - 1)]
    for sx, sy in seeds:
        if work.getpixel((sx, sy)) != marker:
            ImageDraw.floodfill(work, (sx, sy), marker, thresh=thresh)
    r, g, b = work.split()
    bg = Image.merge("RGB", (r.point(lambda v: 255 if v == 1 else 0), g.point(lambda v: 255 if v == 254 else 0),
                             b.point(lambda v: 255 if v == 3 else 0))).convert("L").point(lambda v: 255 if v == 255 else 0)
    alpha = ImageOps.invert(bg).filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(1.2))
    out = img.convert("RGBA")
    out.putalpha(alpha)
    return out



