"""读自己的“关注”列表，找出互相关注的好友（给“@ 好友看视频”的下拉框用）。

做法：在新标签页打开自己的主页 → 点“关注” → 弹出的列表往下滚几屏。
抖音网页加载列表时返回的数据里有关注关系（follow_status / follower_status），浏览器顺手读下来；
同时看列表里每个人旁边的按钮字（“互相关注”）。两种都读不到的人就不算互关。不发任何额外请求。
"""
from __future__ import annotations

import logging

from .browser import DouyinBrowser
from .selectors import as_list

log = logging.getLogger(__name__)


def fetch_mutual_friends(b: DouyinBrowser, max_scroll: int = 15) -> tuple[list[dict], str]:
    """返回 (好友列表, 出错原因)。好友：{uid, nickname, douyin_id, avatar, mutual}。"""
    b.following.clear()
    dom_mutual: set[str] = set()
    dom_names: set[str] = set()
    words = as_list(b.sel.get("mutual_words")) or ["互相关注"]
    with b.in_new_tab(b.sel.get("profile_url", "https://www.douyin.com/user/self")):
        entry = b.find("following_entry", timeout=15000)
        if entry is None or not b.safe_click(entry, "关注"):
            path = b.dump("following_entry_missing")
            return [], f"在自己的主页上没找到“关注”入口（页面已保存到 {path}）"
        b.pause(2.0, 3.0)
        b.check_captcha()
        panel = b.find("following_panel", timeout=8000)
        if panel is None:
            path = b.dump("following_panel_missing")
            return [], f"点了“关注”，但没看到关注列表（页面已保存到 {path}）"
        stable = 0
        last = -1
        for _ in range(max_scroll):
            for item in b.find_all("following_item", root=panel)[:400]:
                t = b.text_of(item, 120)
                if not t:
                    continue
                name = t.split(" ")[0].strip()
                if name:
                    dom_names.add(name)
                    if any(w in t for w in words):
                        dom_mutual.add(name)
            n = len(b.following) + len(dom_names)
            stable = stable + 1 if n == last else 0
            last = n
            if stable >= 2:
                break
            try:
                box = panel.bounding_box()
                if box:
                    b.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                b.page.mouse.wheel(0, 1400)
            except Exception:  # noqa: BLE001
                break
            b.pause(1.2, 2.2)
            b.check_captcha()
    out: dict[str, dict] = {}
    for uid, u in b.following.items():
        if b.me_uid and uid == b.me_uid:
            continue  # 自己不算
        mutual = u["mutual"]
        if mutual is None:
            mutual = u["nickname"] in dom_mutual
        out[uid] = {**u, "mutual": bool(mutual or u["nickname"] in dom_mutual)}
    known = {u["nickname"] for u in out.values()}
    for name in dom_mutual - known:  # 只在页面上看到“互相关注”、数据里没有的人
        out["name:" + name] = {"uid": "name:" + name, "nickname": name, "douyin_id": "", "avatar": "", "mutual": True}
    if not out and not dom_names:
        path = b.dump("following_empty")
        return [], f"关注列表是空的，或者没读出来（页面已保存到 {path}）"
    return list(out.values()), ""
