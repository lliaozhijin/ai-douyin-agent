"""从粘贴的文字里找出视频地址，认出是哪个网站。

抖音 App 里“分享 → 复制链接”复制出来的是一大段话（“7.43 复制打开抖音，看看【xx的作品】…… https://v.douyin.com/xxxx/ ……”），
YouTube / B 站 / 小红书也类似：这里把里面的网址一个个挑出来，一行一个也行、混在话里也行。
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

_URL = re.compile(r"https?://[^\s<>\"'，。！？、；【】（）《》「」]+", re.I)
_TRAIL = ".,;:!?)]}>'\"…"

PLATFORMS = (
    ("douyin", ("douyin.com", "iesdouyin.com", "douyinvod.com", "amemv.com")),
    ("tiktok", ("tiktok.com",)),
    ("youtube", ("youtube.com", "youtu.be", "youtube-nocookie.com")),
    ("bilibili", ("bilibili.com", "b23.tv", "bili2233.cn")),
    ("kuaishou", ("kuaishou.com", "chenzhongtech.com", "gifshow.com")),
    ("xiaohongshu", ("xiaohongshu.com", "xhslink.com")),
    ("weibo", ("weibo.com", "weibo.cn")),
    ("x", ("twitter.com", "x.com")),
    ("instagram", ("instagram.com",)),
)
NAMES = {"douyin": "抖音", "tiktok": "TikTok", "youtube": "YouTube", "bilibili": "B 站", "kuaishou": "快手",
         "xiaohongshu": "小红书", "weibo": "微博", "x": "X（推特）", "instagram": "Instagram", "other": "其他网站"}


def extract_urls(text: str) -> list[str]:
    """粘贴的文字里所有的网址（去掉末尾粘上的标点，去重，保持顺序）。"""
    out = []
    for m in _URL.finditer(text or ""):
        u = m.group(0).rstrip(_TRAIL)
        if u and u not in out:
            out.append(u)
    return out


def platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for name, hosts in PLATFORMS:
        if any(host == h or host.endswith("." + h) for h in hosts):
            return name
    return "other"


_AWEME = re.compile(r"/(?:video|note|slides|share/video|share/note|share/slides)/(\d{15,21})")


def douyin_id(url: str) -> str:
    """抖音视频 / 图文的 id：/video/7311…、/note/7311…、?modal_id=7311…；短链接（v.douyin.com）要先跳转才知道。"""
    m = _AWEME.search(url or "")
    if m:
        return m.group(1)
    q = parse_qs(urlparse(url or "").query)
    for k in ("modal_id", "aweme_id", "vid", "item_id"):
        v = (q.get(k) or [""])[0]
        if re.fullmatch(r"\d{15,21}", v):
            return v
    return ""


def is_short(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ("v.douyin.com", "b23.tv", "xhslink.com", "youtu.be", "vm.tiktok.com", "vt.tiktok.com")


def safe_name(text: str, limit: int = 60) -> str:
    """能当文件名用的一段字：去掉 Windows 不允许的字符、换行、话题标签，太长截断。"""
    t = re.sub(r"#\S+", " ", text or "")
    t = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", t)
    t = re.sub(r"\s+", " ", t).strip(" .")
    return (t[:limit].rstrip(" .") or "视频")
