"""主程序（刷抖音的后台线程、界面）和桌宠之间传话：发生了什么事，桌宠就做对应的动作。

只在本进程内传递，不经过网络。桌宠没开的时候，消息会被丢掉（只保留最近 30 条）。
"""
from __future__ import annotations

import threading
import time
from collections import deque

_LOCK = threading.Lock()
_Q: deque = deque(maxlen=30)


def emit(kind: str, text: str = "", **kw) -> None:
    """kind：like 点赞 / comment 评论 / at_friend @好友 / comment_like 赞评论 / comment_reply 回复评论 /
    reply_at 回复@ / reply_dm 回私信 / thread_reply 接着聊 / skip 划走 / love 刷到超喜欢的 /
    publish_wish 想发作品 / published 发布成功 / attention 需要你帮忙 / mood 心情变了"""
    with _LOCK:
        _Q.append({"kind": kind, "text": text, "t": time.time(), **kw})


def drain() -> list[dict]:
    with _LOCK:
        out = list(_Q)
        _Q.clear()
    return out
