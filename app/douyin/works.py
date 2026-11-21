"""看看自己发过的作品现在怎么样了：点赞、评论、收藏、转发各多少。

做法和读关注列表一样：在新标签页打开自己的主页，抖音网页会自己加载“作品”列表（JSON），浏览器顺手读下来。
不发任何额外请求，不点任何东西。读不到（网页改版、没登录、要验证）就返回空，不影响别的。
"""
from __future__ import annotations

import logging
import time

from .. import cancel
from .browser import DouyinBrowser

log = logging.getLogger(__name__)


def fetch_my_works(b: DouyinBrowser, limit: int = 12, wait_s: float = 12.0) -> list[dict]:
    """返回最近的作品：[{aweme_id, desc, create_time, likes, comments, collects, shares, plays}]（新的在前）。"""
    b.my_works = []
    b.my_works_on = True
    try:
        with b.in_new_tab(b.sel.get("profile_url", "https://www.douyin.com/user/self")):
            deadline = time.time() + wait_s
            got_at = 0.0
            while time.time() < deadline:
                cancel.sleep(1.0)
                if b.my_works and not got_at:
                    got_at = time.time()
                if got_at and time.time() - got_at >= 1.5:       # 读到了：再等一下，有时分两次加载
                    break
            if not b.my_works:
                b.check_captcha()
                log.info("没读到自己的作品列表（可能还没发过作品，或者网页改版了）")
    finally:
        b.my_works_on = False
    works = sorted(b.my_works, key=lambda w: -w.get("create_time", 0))
    return works[:limit]
