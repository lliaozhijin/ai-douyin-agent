"""比较抖音昵称用的小工具（私信、评论区、通知、接口里读到的同一个人的名字，写法常常不完全一样）。"""
from __future__ import annotations

import re


def name_key(name: str) -> str:
    """比名字用的写法：去掉空白、emoji、标点。

    抖音网页把名字里的 emoji 画成图片（<img alt="🐳">），读出来的文字里没有它；通知、接口里的名字却带着。
    2026-11-20 实测：“吃白饭的鲸🐳”在评论区读出来是“吃白饭的鲸”，以前直接比字，永远对不上。"""
    return re.sub(r"[\W_]+", "", (name or "").lstrip("@"))


def same_person(a: str, b: str) -> bool:
    """两个名字是不是同一个人（忽略 emoji、标点、空白；一边被截短了也算）。"""
    ka, kb = name_key(a), name_key(b)
    if ka and kb:
        short, long_ = sorted((ka, kb), key=len)
        return short == long_ or (len(short) >= 2 and short in long_)
    sa, sb = re.sub(r"\s+", "", a or "").lstrip("@"), re.sub(r"\s+", "", b or "").lstrip("@")
    return bool(sa) and bool(sb) and (sa in sb or sb in sa)


def uid_of(url: str) -> str:
    """主页链接里的 sec_uid（https://www.douyin.com/user/MS4wLjAB…）；没有返回空。"""
    m = re.search(r"/user/([A-Za-z0-9_\-]{10,})", url or "")
    return m.group(1) if m else ""
