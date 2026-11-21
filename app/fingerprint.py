"""内容指纹：认出“同样的内容”——搬运、二次剪辑、换了标题的同一个梗。

- 文案：去掉 @人、链接、话题标签、标点和空白后，按相邻 2～3 个字切片算 64 位 SimHash（Charikar 2002）。
  两段文案的指纹只差几位（海明距离 ≤ TEXT_DIST），就是同一段话（多了几个字、换了标点、加了话题也认得出）。
- 画面：第一眼截图缩成 9×8 灰度图，比较左右相邻像素算 64 位 dHash（差异哈希）。画面几乎一片纯色的不算
  （黑屏、纯色背景会让所有视频都“长得一样”）。

只用标准库 + Pillow（已经是依赖），不用联网。
"""
from __future__ import annotations

import hashlib
import io
import re

BITS = 64
TEXT_DIST = 3          # 文案指纹差几位以内算同一段话
IMG_DIST = 5           # 画面指纹差几位以内算同一个画面
LOOSE_TEXT_DIST = 14   # 画面一样时，文案差得不太远也算同一个内容（换了标题的搬运）
MIN_TEXT = 6           # 文案（清理后）少于这么多字，不拿文案比（“哈哈哈”“#日常”大家都一样）

_MENTION = re.compile(r"@[^\s@#]{1,30}")
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_NOISE = re.compile(r"[\s\W_]+", re.U)
# 话题标签里这些太常见，几乎每个视频都有，不算内容
_COMMON_TAGS = ("抖音", "dou上热门", "上热门", "热门", "推荐", "抖音小助手", "热门视频", "fyp", "日常", "vlog")


_TAG = re.compile(r"#[^\s#@]{1,30}")


def clean_text(desc: str) -> str:
    """去掉 @人、链接、话题标签、标点和空白，只留文案正文（搬运的人常常只改话题）。
    正文太短（整条文案几乎只有话题）时，才把话题也算进来（去掉“上热门”这种大家都带的）。"""
    t = _MENTION.sub(" ", _URL.sub(" ", desc or ""))
    body = _NOISE.sub("", _TAG.sub(" ", t)).lower()
    if len(body) >= MIN_TEXT:
        return body
    for tag in _COMMON_TAGS:
        t = re.sub("#" + re.escape(tag) + r"(?=[\s#]|$)", " ", t, flags=re.I)
    return _NOISE.sub("", t).lower()


def _h64(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")


def simhash(text: str) -> int | None:
    """文案的 64 位 SimHash；清理后太短返回 None。"""
    t = clean_text(text)
    if len(t) < MIN_TEXT:
        return None
    feats: dict[str, int] = {}
    for n in (2, 3):
        for i in range(len(t) - n + 1):
            g = t[i:i + n]
            feats[g] = feats.get(g, 0) + 1
    v = [0] * BITS
    for g, w in feats.items():
        h = _h64(g)
        for b in range(BITS):
            v[b] += w if (h >> b) & 1 else -w
    out = 0
    for b in range(BITS):
        if v[b] > 0:
            out |= 1 << b
    return out


def dhash(image: bytes | None) -> int | None:
    """一张截图（JPEG / PNG 字节）的 64 位 dHash；打不开、或者画面几乎一片纯色返回 None。"""
    if not image:
        return None
    try:
        from PIL import Image, ImageStat

        im = Image.open(io.BytesIO(image)).convert("L")
        if ImageStat.Stat(im).stddev[0] < 12:
            return None
        small = im.resize((9, 8), Image.Resampling.LANCZOS)
        px = list(small.tobytes())          # L 模式：每个像素一个字节
    except Exception:  # noqa: BLE001 —— 认不出画面不影响别的
        return None
    out = 0
    for row in range(8):
        for col in range(8):
            if px[row * 9 + col] > px[row * 9 + col + 1]:
                out |= 1 << (row * 8 + col)
    return out


def hamming(a: int | None, b: int | None) -> int:
    if a is None or b is None:
        return BITS
    return bin((a ^ b) & ((1 << BITS) - 1)).count("1")


def to_hex(h: int | None) -> str:
    return "" if h is None else f"{h:016x}"


def from_hex(s: str | None) -> int | None:
    try:
        return int(s, 16) if s else None
    except ValueError:
        return None


JACCARD_SAME = 0.7     # 短文案：相邻两个字一组，重合七成以上也算同一段话（短文案改一两个字，SimHash 会差很多位）


def bigram_jaccard(a: str, b: str) -> float:
    ga = {a[i:i + 2] for i in range(len(a) - 1)}
    gb = {b[i:i + 2] for i in range(len(b) - 1)}
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def fingerprint(desc: str, image: bytes | None = None) -> dict:
    """一个视频的指纹：{"th": 文案 SimHash, "ih": 画面 dHash, "txt": 清理后的文案（最多 80 字）}。"""
    txt = clean_text(desc)[:80]
    return {"th": simhash(desc), "ih": dhash(image), "txt": txt if len(txt) >= MIN_TEXT else ""}


def same_text(a: dict, b: dict) -> bool:
    if a.get("th") is not None and b.get("th") is not None and hamming(a["th"], b["th"]) <= TEXT_DIST:
        return True
    return bool(a.get("txt") and b.get("txt")) and bigram_jaccard(a["txt"], b["txt"]) >= JACCARD_SAME


def same_content(a: dict, b: dict) -> bool:
    """两个视频是不是同一个内容：文案几乎一样；或者画面几乎一样、文案也不算差得远（或者没有能比的文案）。"""
    if same_text(a, b):
        return True
    if a.get("ih") is not None and b.get("ih") is not None and hamming(a["ih"], b["ih"]) <= IMG_DIST:
        if a.get("th") is None or b.get("th") is None:
            return True
        return hamming(a["th"], b["th"]) <= LOOSE_TEXT_DIST or bigram_jaccard(a.get("txt", ""), b.get("txt", "")) >= 0.4
    return False


def bands(h: int | None, n: int = 4) -> tuple:
    """把 64 位指纹切成 n 段（每段 64/n 位）。两个指纹只差 k 位（k < n）时，至少有一段完全相同——
    按段建索引，就不用和每一条都比（Manku 等人在 Google 网页去重里用的分段查找法）。没有指纹返回全 None。"""
    if h is None:
        return (None,) * n
    w = 64 // n
    return tuple((h >> (i * w)) & ((1 << w) - 1) for i in range(n))
