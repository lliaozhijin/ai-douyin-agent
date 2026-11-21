"""在抖音上搜视频（主人让她“搜个 xx 的视频发给谁”时用）。

做法和网上的抖音搜索工具一样（Apify 的 douyin-search-scraper、MediaCrawler 的关键词搜索）：
在新标签页打开 www.douyin.com/search/<关键词>?type=video，抖音网页自己会发带签名的搜索请求，
浏览器顺手读它返回的 JSON（见 browser._collect_search），不自己拼接口、不算签名、不另外发请求。
往下滚一两屏让网页多加载一些；接口的数据读不到时，退一步从页面上的视频链接读。
"""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import quote

from .. import cancel
from .browser import BrowserError, DouyinBrowser

log = logging.getLogger(__name__)

# 抖音网页搜索的排序：0 综合（默认）/ 1 最多点赞 / 2 最新发布
SORTS = {"综合": "", "最多点赞": "1", "最热": "1", "点赞最多": "1", "最新": "2", "最新发布": "2"}

LINK_JS = """() => {
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/video/"], a[href*="/note/"]')) {
    const m = (a.getAttribute('href') || '').match(/\\/(video|note)\\/(\\d{15,})/);
    if (!m || seen.has(m[2])) continue;
    const r = a.getBoundingClientRect();
    if (r.width < 40 || r.height < 40) continue;          // 只要搜索结果里的卡片，不要角落里的小链接
    seen.add(m[2]);
    out.push({kind: m[1], id: m[2], text: (a.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 160)});
    if (out.length >= 30) break;
  }
  return out;
}"""


def search_url(keyword: str, sort: str = "") -> str:
    url = f"https://www.douyin.com/search/{quote(keyword.strip())}?type=video"
    code = SORTS.get((sort or "").strip(), "")
    return url + (f"&sort_type={code}" if code else "")


def _from_links(links: list[dict]) -> list[dict]:
    """接口数据读不到时：从页面上的视频卡片读（只有 id 和卡片上的文字）。"""
    out = []
    for x in links or []:
        vid = str(x.get("id") or "")
        if not vid.isdigit():
            continue
        text = str(x.get("text") or "")
        likes = 0
        m = re.search(r"(\d+(?:\.\d+)?)\s*(万|w)?", text)
        if m:
            likes = int(float(m.group(1)) * (10000 if m.group(2) else 1))
        out.append({"aweme_id": vid, "url": f"https://www.douyin.com/{x.get('kind') or 'video'}/{vid}",
                    "desc": text[:120], "author": "", "author_uid": "", "likes": likes, "comments": 0,
                    "duration_s": 0})
    return out


class SearchNeedsVerify(BrowserError):
    """搜索页弹了抖音的安全验证（整页换成“验证码中间页”），没人去点，这次没搜成。"""


def _verify_up(b: DouyinBrowser) -> bool:
    """搜索页现在是不是要验证（“验证码中间页”，或者弹了滑块 / 点选验证）。"""
    try:
        return bool(b.captcha_visible())
    except Exception:  # noqa: BLE001
        return False


def search_videos(b: DouyinBrowser, keyword: str, sort: str = "", limit: int = 8,
                  wait_s: float = 15.0, on_verify=None) -> list[dict]:
    """搜视频，返回最多 limit 个：[{aweme_id, url, desc, author, author_uid, likes, comments, duration_s}]。
    按抖音给的顺序（就是网页上看到的顺序）。

    搜索页弹了验证（2026-11-20 实测：搜索页整页变成“验证码中间页”）：一发现就停下来，不再干等 15 秒；
    on_verify() 会弹窗请你在浏览器里点一下、就在这个标签页里等你验证完（返回 True 表示验证完了），
    验证完页面会自己跳到搜索结果，接着读。没人验证（或者没给 on_verify）就抛 SearchNeedsVerify，
    由调用的地方如实告诉主人——以前这里抛的是全局的“验证码”，标签页一关验证框也没了，
    软件以为你已经验证完，主人那边什么回复也收不到。"""
    keyword = (keyword or "").strip()[:40]
    if not keyword:
        return []
    b.search_items = []
    links: list[dict] = []
    with b.in_new_tab(search_url(keyword, sort)):
        deadline = time.time() + wait_s
        scrolled = 0
        verified = False
        while time.time() < deadline:
            cancel.sleep(1.0)
            if len(b.search_items) >= limit:
                break
            if not b.search_items and _verify_up(b):
                log.info("搜“%s”：搜索页弹了抖音的安全验证", keyword)
                if verified or on_verify is None or not on_verify():
                    raise SearchNeedsVerify("抖音的搜索页弹了安全验证，要人在浏览器里手动完成")
                verified = True                   # 验证完了：页面会自己跳到搜索结果，再等一会儿
                log.info("搜“%s”：验证完了，接着等搜索结果", keyword)
                deadline = time.time() + wait_s
                continue
            if b.search_items and scrolled < 2:      # 有结果了但不够：往下滚一屏让网页再加载一些
                try:
                    b.page.mouse.wheel(0, 1600)
                except Exception:  # noqa: BLE001
                    pass
                scrolled += 1
        if not b.search_items:
            if _verify_up(b):
                raise SearchNeedsVerify("抖音的搜索页弹了安全验证，要人在浏览器里手动完成")
            try:
                links = b.page.evaluate(LINK_JS) or []
            except Exception:  # noqa: BLE001
                links = []
            if not links:
                path = b.dump("search_empty")
                log.warning("搜“%s”没读到结果，页面已保存到 %s", keyword, path)
    items = list(b.search_items) or _from_links(links)
    return [x for x in items if x.get("aweme_id")][:limit]


def brief(items: list[dict]) -> str:
    """给大模型看的搜索结果：一行一个，带编号。"""
    lines = []
    for i, x in enumerate(items, 1):
        likes = x.get("likes") or 0
        like = f"{likes / 10000:.1f}万赞" if likes >= 10000 else (f"{likes}赞" if likes else "")
        dur = x.get("duration_s") or 0
        lines.append(f"{i}. 《{x.get('desc') or '（没有文案）'}》"
                     + (f" @{x['author']}" if x.get("author") else "")
                     + (f" {like}" if like else "") + (f" {dur // 60}:{dur % 60:02d}" if dur else ""))
    return "\n".join(lines)
