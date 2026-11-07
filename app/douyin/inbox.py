"""通知里的 @ 和私信：读取、回复。

抖音网页版的通知和私信面板结构经常变化，这里的定位全部来自 selectors.json，
第一次真机运行时如果读不到，用“设置 → 保存当前页面结构”把截图发给开发者校准。
"""
from __future__ import annotations

import hashlib
import logging
import re

from ..db import today_str
from .browser import DouyinBrowser
from .selectors import as_list
from .feed import Feed, FeedReadError

log = logging.getLogger(__name__)


def video_url(href: str) -> str:
    """把分享链接 / 弹窗地址统一成 https://www.douyin.com/video/<id>（图文是 /note/<id>）。认不出返回空。"""
    href = href or ""
    m = re.search(r"(?:/video/|modal_id=)(\d{8,})", href)
    if m:
        return f"https://www.douyin.com/video/{m.group(1)}"
    m = re.search(r"/note/(\d{8,})", href)
    if m:
        return f"https://www.douyin.com/note/{m.group(1)}"
    return ""


# 对方在问某个视频的内容（“这个视频讲了什么”“帮我分析一下”“提炼一下重点”之类）
VIDEO_ASK = re.compile(r"视频|作品|分析|提炼|总结|概括|讲了|说了什么|讲的|讲什么|重点|关键信息|帮我看")


# 从某个元素往上找能滚动的容器（侧边评论区），往下滚一屏；找不到就滚整个页面（视频详情页的评论在页面下方）
SCROLL_MORE_JS = r"""
(el) => {
  el.scrollIntoView({block: "end"});
  let n = el;
  while (n && n !== document.body) {
    const s = getComputedStyle(n);
    if ((s.overflowY === "auto" || s.overflowY === "scroll") && n.scrollHeight > n.clientHeight + 10) {
      n.scrollTop += Math.max(800, n.clientHeight); return "box";
    }
    n = n.parentElement;
  }
  window.scrollBy(0, 1500);
  return "page";
}
"""


def _squash(t: str) -> str:
    return re.sub(r"\s+", "", t or "")


def match_mine(text: str, mine: list[str]) -> str:
    """通知里引用的“你的评论”是她哪一条：返回她那条评论的原文，认不出返回空。"""
    t = (text or "").replace(" ", "")
    best = ""
    for m in mine or []:
        k = (m or "").replace(" ", "")[:8]
        if len(k) >= 4 and k in t and len(m) > len(best):
            best = m
    return best


def _same_name(a: str, b: str) -> bool:
    a, b = (a or "").strip().lstrip("@"), (b or "").strip().lstrip("@")
    return bool(a) and bool(b) and (a == b or a.startswith(b) or b.startswith(a))


def user_url(href: str) -> str:
    """把各种主页链接统一成 https://www.douyin.com/user/<id>；自己的主页、认不出的返回空。"""
    m = re.search(r"/user/([A-Za-z0-9_\-]{10,})", href or "")
    return f"https://www.douyin.com/user/{m.group(1)}" if m else ""


# 在网页元素背后的 React 数据里找指定字段（只读页面自己已经有的数据，不发请求）。
# 从元素本身往上找几层，每层看它的 props 和组件链上的 memoizedProps；离元素最近的一层找到就停。
REACT_FIND_JS = r"""
(el, opts) => {
  const want = new Set(opts.keys);
  const re = opts.pattern ? new RegExp(opts.pattern) : null;
  const found = [];
  const seen = new WeakSet();
  let budget = 6000;
  const skip = new Set(["children", "_owner", "return", "stateNode", "child", "sibling", "alternate", "_debugOwner"]);
  const visit = (obj, depth, path) => {
    if (!obj || typeof obj !== "object" || depth > opts.depth || budget-- <= 0) return;
    if (seen.has(obj)) return;
    seen.add(obj);
    if (typeof Node !== "undefined" && obj instanceof Node) return;
    let keys;
    try { keys = Object.keys(obj); } catch (e) { return; }
    for (const k of keys) {
      if (skip.has(k) || found.length > 30) continue;
      let v;
      try { v = obj[k]; } catch (e) { continue; }
      if (want.has(k) && (typeof v === "string" || typeof v === "number")) {
        const sv = String(v);
        if (!re || re.test(sv)) found.push({ key: k, value: sv, path: path + "." + k });
      } else if (v && typeof v === "object") {
        visit(v, depth + 1, path + "." + k);
      }
    }
  };
  let node = el;
  for (let up = 0; node && up <= opts.up; up++, node = node.parentElement) {
    for (const k of Object.keys(node)) {
      if (k.startsWith("__reactProps$")) visit(node[k], 0, "props" + up);
      else if (k.startsWith("__reactFiber$")) {
        let f = node[k];
        for (let i = 0; f && i < 6; i++, f = f.return) visit(f.memoizedProps, 0, "fiber" + up + "." + i);
      }
    }
    if (found.length) break;
  }
  return found;
}
"""

SEC_UID_RE = r"^MS4wLjABAAAA[\w-]{10,}$"


def react_values(el, keys: list[str], pattern: str = "", up: int = 4, depth: int = 5) -> list[dict]:
    try:
        found = el.evaluate(REACT_FIND_JS, {"keys": keys, "pattern": pattern, "up": up, "depth": depth})
    except Exception:  # noqa: BLE001
        return []
    return [f for f in found if isinstance(f, dict) and "value" in f] if isinstance(found, list) else []


def pick_unique(found: list[dict], prefer: str = "") -> str:
    """从找到的值里取一个：优先路径里带 prefer 的；剩下的值如果互相不一致，就当没找到（宁可不认，不能认错）。"""
    if prefer:
        preferred = {f["value"] for f in found if prefer in f.get("path", "")}
        if len(preferred) == 1:
            return preferred.pop()
        if len(preferred) > 1:
            return ""
    values = {f["value"] for f in found}
    return values.pop() if len(values) == 1 else ""


def _hid(*parts: str) -> str:
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()[:20]


class Inbox:
    def __init__(self, b: DouyinBrowser):
        self.b = b
        self.sec_uids: dict[str, str] = {}  # 私信列表里的名字 -> 主页 id
        self.feed = Feed(b)

    # ---------- @ 我的（右上角“通知” → “互动消息”面板） ----------
    def open_notice_panel(self) -> bool:
        if self.b.find("notice_panel", timeout=800) is not None:
            return True
        entry = self.b.find("notice_entry", timeout=4000)
        if entry is None:
            log.warning("没找到“通知”入口")
            return False
        if not self.b.safe_click(entry, "通知"):
            return False
        self.b.pause(1.5, 2.5)
        self.b.check_captcha()
        return self.b.find("notice_panel", timeout=5000) is not None

    def close_notice_panel(self) -> None:
        if self.b.find("notice_panel", timeout=500) is None:
            return
        try:
            self.b.page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass
        self.b.pause(0.5, 1.0)
        if self.b.find("notice_panel", timeout=500) is not None:
            entry = self.b.find("notice_entry", timeout=1000)
            if entry is not None:
                self.b.safe_click(entry, "关闭通知")
                self.b.pause(0.5, 1.0)

    def _parse_notice(self, row, mine: list[str] | None = None) -> dict | None:
        """读一条通知。只要两种：“提到了你 / @了你”（kind=at）和“回复了你的评论”（kind=reply）。

        读出名字、评论内容、对方主页、视频 id（网页自己的数据里有就直接用）；
        回复她的评论时，再认一认回复的是她哪条评论（mine：她发过的评论）。"""
        text = self.b.text_of(row, 400)
        reply_kw = self.b.sel.get("notice_reply_keywords") or ["回复了你的评论", "回复了你"]
        at_kw = self.b.sel.get("notice_at_keywords") or ["@了你", "提到了你"]
        if any(k in text for k in reply_kw):
            kind = "reply"
        elif any(k in text for k in at_kw):
            kind = "at"
        else:
            return None
        user = self.b.text_of(self.b.find("notice_user", timeout=500, root=row), 40).strip()
        content = self.b.text_of(self.b.find("notice_content", timeout=500, root=row), 300).strip()
        if not user or not content:
            return None
        href = ""
        try:
            href = row.locator("a[href*='/user/']").first.get_attribute("href", timeout=500) or ""
        except Exception:  # noqa: BLE001
            href = ""
        vid = pick_unique(react_values(row, ["aweme_id", "awemeId", "itemId", "item_id", "group_id"],
                                       r"^\d{15,20}$", up=1, depth=6), prefer="aweme")
        cid = pick_unique(react_values(row, ["cid", "comment_id", "commentId"], r"^\d{10,20}$", up=1, depth=6))
        it = {"kind": kind, "ext_id": ("rp_" if kind == "reply" else "at_") + _hid(user, content), "user": user,
              "text": content, "user_url": user_url(href),
              "video_url": f"https://www.douyin.com/video/{vid}" if vid else "", "cid": cid, "mine": "",
              "video_gone": False}
        # 互动消息接口里有这条评论的完整信息：补上评论 id、视频 id，记下它是不是回复在别人评论下面（楼中楼）
        info = self.b.notice_comment(content, user) if hasattr(self.b, "notice_comment") else None
        it["root_cid"] = ""
        if isinstance(info, dict):
            it["cid"] = it["cid"] or info["cid"]
            if not vid and info.get("aweme_id"):
                vid = info["aweme_id"]
                it["video_url"] = f"https://www.douyin.com/video/{vid}"
            if info.get("reply_id") not in ("", "0", None) and info["reply_id"] != info["cid"]:
                it["root_cid"] = info["reply_id"]
        if not vid:
            it["video_gone"] = self._video_gone(row, text)
        if kind == "reply":
            it["mine"] = match_mine(text.replace(content, " "), mine or [])
        return it

    GONE_WORDS = ("作品已删除", "作品不存在", "视频已删除", "视频不存在", "作品不可见", "已失效", "无法查看")

    def _video_gone(self, row, text: str) -> bool:
        """通知右边本该是视频封面：原视频被删除 / 设为不可见时，抖音只放一个“视频 + 禁止”的灰色图标，没有封面图。"""
        if any(w in text for w in self.GONE_WORDS):
            return True
        try:
            imgs = int(row.locator("img").count())      # 头像 1 张 + 视频封面 1 张
            svgs = int(row.locator("svg").count())
        except Exception:  # noqa: BLE001
            return False
        return imgs <= 1 and svgs >= 2

    def fetch_mentions(self, limit: int = 10, is_known=None, kinds: tuple = ("at",), mine=None,
                       resolve_video: bool = True) -> list[dict]:
        """读“互动消息”里 @ 她的评论（kind=at）、回复她评论的（kind=reply）。

        is_known(ext_id) 为 True 的（已经处理过的）跳过；mine 是她发过的评论（认出别人回复的是哪条）。"""
        if not self.open_notice_panel():
            return []
        items, seen = [], set()
        for row in self.b.find_all("notice_item")[:40]:
            it = self._parse_notice(row, mine)
            if it and it["kind"] in kinds and it["ext_id"] not in seen:
                seen.add(it["ext_id"])
                items.append(it)
        out = []
        for it in items:
            if len(out) >= limit:
                break
            if is_known and is_known(it["ext_id"]):
                continue
            if resolve_video and not it["video_url"] and not it.get("video_gone"):
                it["video_url"] = self._notice_video_by_click(it)
            if resolve_video and not it["video_url"] and not it.get("video_gone"):
                self._dump_once("notice_video_unknown", f"认不出 {it['user']} 是在哪个视频下找她的")
            out.append(it)
        self.close_notice_panel()
        return out

    def _notice_video_by_click(self, it: dict) -> str:
        """通知里没有视频链接：点一下这条通知的视频缩略图，看打开的是哪个视频。"""
        if not self.open_notice_panel():
            return ""
        for row in self.b.find_all("notice_item")[:40]:
            again = self._parse_notice(row, [it.get("mine")] if it.get("mine") else None)
            if again and again["ext_id"] == it["ext_id"]:
                try:
                    thumb = row.locator("img").last
                    target = thumb if thumb.count() > 0 else row
                except Exception:  # noqa: BLE001
                    target = row
                return self._video_url_by_click(target, "notice_click")
        return ""

    def open_mention_video(self, item: dict, with_comments: bool = True) -> dict:
        """打开对方 @ 她 / 回复她的那个视频。网络慢时多等一会儿（中途刷新一次）；还是打不开就说清楚原因。"""
        if item.get("video_url"):
            self.b.goto(item["video_url"])
            if self.b.wait_any("detail_container", timeout=45) is not None:
                return self.feed.read_detail(with_comments=with_comments)  # 单个视频的详情页
            raise FeedReadError("那个视频的页面一直没打开（可能是网络慢，或者视频已经删除 / 设为不可见），这次没法回复")
        if self.b.find("detail_container", timeout=8000) is not None:
            return self.feed.read_detail(with_comments=with_comments)
        raise FeedReadError("不知道对方是在哪个视频下找她的（没有视频链接），这次没法回复")

    def reply_mention(self, item: dict, text: str) -> bool:
        """在视频评论区找到对方那条评论（优先按评论 id，其次按内容 + 名字），点“回复”后发送。找不到就不发。

        回复她评论的（kind=reply）通常折叠在她那条评论下面：先在顶层找，找不到就展开她那条评论的回复再找。"""
        if not item.get("video_url"):
            return False  # 不知道是哪个视频，不能乱发
        if not self.feed.open_comments():
            return False
        is_reply = item.get("kind") == "reply"
        target = self._find_comment(item, deep=not is_reply)
        if target is None and is_reply:
            # 回复她评论的，通常折叠在她那条评论下面：先找到并展开她的评论，再找
            mine = item.get("mine") or ""
            if mine and self._find_comment({"text": mine, "user": ""}) is not None:
                self._expand_replies(mine)
            target = self._find_comment(item, deep=False) or self._find_comment(item)
        if target is None and item.get("root_cid"):
            # 这条评论是回复在别人评论下面的（楼中楼）：先找到楼主那条评论，展开它的回复再找
            target = self._find_in_thread(item, item["root_cid"])
        if target is None:
            # 不知道楼主是谁：把评论区里折叠着的回复一串串展开找（有上限，不会没完没了）
            target = self._search_threads(item)
        if target is None:
            self._dump_once("mention_comment_missing", f"在视频评论区里没找到 {item.get('user')} 那条评论")
            self.feed.close_comments()
            return False
        ok = self.feed.reply_on_item(target, text, (item.get("text") or "")[:12], item.get("user") or "")
        self.feed.close_comments()
        return ok

    def _find_comment(self, item: dict, deep: bool = True):
        """在打开的评论区里找到对方那条评论。

        先按评论 id、再按内容 + 名字在已经加载的评论里找；找不到时（评论区默认按热度排，刚发的 @ 往往在很后面）
        一边往下滚一边找，必要时点“加载更多”，最多翻几十屏。"""
        if item.get("cid"):
            t = self.feed._locate_comment({"cid": item["cid"], "text": item.get("text", "")})
            if t is not None:
                return t
        last_n, stale, checked = -1, 0, 0
        rounds = int(self.b.sel.get("comment_search_rounds") or 40) if deep else 1
        for i in range(rounds):
            items = self.b.find_all("comment_item")
            # 只看新加载出来的那些（前面看过的不再一条条读一遍，评论多的时候能快很多）
            hit = self._match_comment(items[checked:], item)
            if hit is not None:
                return hit
            checked = max(0, len(items) - 3)
            if not deep or i == rounds - 1:
                break
            if self._comments_end():
                break  # 评论区已经显示“暂时没有更多评论”：到底了
            stale = stale + 1 if len(items) == last_n else 0
            if stale >= 4:
                break  # 连续几次都没有加载出新评论：到底了
            last_n = len(items)
            self._load_more_comments(items)
            self.b.check_captcha()
        return None

    def _root_by_cid(self, cid: str):
        """评论区里 id 是 cid 的那条（顶层）评论；没加载出来就往下翻着找。"""
        item_sel = as_list(self.b.sel.get("comment_item"))[0]
        last_n, stale = -1, 0
        for _ in range(int(self.b.sel.get("comment_search_rounds") or 40)):
            loc = self.b.page.locator(self.feed.cid_selector(cid)).first
            try:
                if loc.count() > 0:
                    loc.scroll_into_view_if_needed(timeout=2000)
                    return loc
            except Exception:  # noqa: BLE001
                pass
            items = self.b.find_all("comment_item")
            if self._comments_end():
                break
            stale = stale + 1 if len(items) == last_n else 0
            if stale >= 4:
                break
            last_n = len(items)
            self._load_more_comments(items)
            self.b.check_captcha()
        return None

    def _expand_all(self, root, max_clicks: int = 12) -> None:
        """把这条评论下面的回复全部展开（“展开 N 条回复”“展开更多”一直点，直到没有或点够次数）。"""
        for _ in range(max_clicks):
            btn = None
            for s in as_list(self.b.sel.get("comment_expand")):
                try:
                    for cand in root.locator(s).all()[:4]:
                        if not cand.is_visible(timeout=300):
                            continue
                        if "收起" in (cand.inner_text(timeout=500) or ""):
                            continue  # 已经展开了：这个按钮现在是“收起”，别点回去
                        btn = cand
                        break
                except Exception:  # noqa: BLE001
                    continue
                if btn is not None:
                    break
            if btn is None or not self.b.safe_click(btn, "展开回复"):
                return
            self.b.pause(0.9, 1.6)

    def _match_in(self, root, item: dict):
        """在一串回复里找对方那条（按评论 id，或按内容 + 名字）。"""
        if item.get("cid"):
            try:
                loc = root.locator(self.feed.cid_selector(item["cid"])).last
                if loc.count() > 0:
                    loc.scroll_into_view_if_needed(timeout=2000)
                    return loc
            except Exception:  # noqa: BLE001
                pass
        try:
            subs = root.locator(as_list(self.b.sel.get("comment_item"))[0]).all()
        except Exception:  # noqa: BLE001
            subs = []
        return self._match_comment(subs, item)

    def _find_in_thread(self, item: dict, root_cid: str):
        root = self._root_by_cid(root_cid)
        if root is None:
            return None
        self._expand_all(root)
        return self._match_in(root, item)

    def _search_threads(self, item: dict, max_threads: int = 40, max_seconds: float = 150.0):
        """一串串展开折叠的回复去找（从上往下，最多展开 max_threads 串、最多找 max_seconds 秒）。"""
        import time as _t

        start = _t.time()
        done = 0
        for c in self.b.find_all("comment_item"):
            if done >= max_threads or _t.time() - start > max_seconds:
                break
            has_btn = False
            for s in as_list(self.b.sel.get("comment_expand"))[:3]:
                try:
                    if c.locator(s).count() > 0:
                        has_btn = True
                        break
                except Exception:  # noqa: BLE001
                    continue
            if not has_btn:
                continue
            done += 1
            try:
                c.scroll_into_view_if_needed(timeout=2000)
            except Exception:  # noqa: BLE001
                pass
            self._expand_all(c, max_clicks=6)
            hit = self._match_in(c, item)
            if hit is not None:
                return hit
            self.b.check_captcha()
        return None

    def _comments_end(self) -> bool:
        for w in ("暂时没有更多评论", "没有更多评论"):
            try:
                if self.b.page.get_by_text(w, exact=False).first.is_visible(timeout=200):
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _match_comment(self, items: list, item: dict):
        text = item.get("text") or ""
        core = _squash(re.sub(r"@\S+", " ", text))[:10] or _squash(text)[:10]
        user = _squash(item.get("user") or "")
        if not core:
            return None
        for c in items:
            # 只看这条评论自己的正文和作者（表情换成文字；不把展开的楼中楼里别人的话算进来）
            own = self.feed.comment_text(c) if hasattr(self.feed, "comment_text") else ""
            if not isinstance(own, str) or not own:
                t = _squash(self.b.text_of(c, 400))
                if core in t and (not user or user in t):
                    return c
                continue
            if core not in _squash(re.sub(r"@\S+", " ", own)) and core not in _squash(own):
                continue
            author = self.feed.comment_author(c) if hasattr(self.feed, "comment_author") else ""
            if user and isinstance(author, str) and author and user not in _squash(author):
                continue
            return c
        return None

    def _load_more_comments(self, items: list) -> None:
        """往下滚评论区，让网页加载下一批评论；有“加载更多 / 展开更多”按钮也点一下。"""
        target = items[-1] if items else self.b.find("comment_list", timeout=1000)
        if target is not None:
            try:
                target.evaluate(SCROLL_MORE_JS)
            except Exception:  # noqa: BLE001
                pass
        root = self.b.find("comment_list", timeout=500)
        more = self.b.find("comment_more", timeout=300, root=root) if root is not None else None
        if more is not None:
            self.b.safe_click(more, "加载更多评论")
        self.b.pause(1.0, 1.8)

    def _expand_replies(self, mine: str) -> bool:
        """展开她那条评论下面折叠的回复（“展开 N 条回复”）。没认出是哪条时，把能看到的都展开一遍。"""
        snippet = (mine or "")[:10]
        clicked = False
        roots = self.b.find_all("comment_item")[:40]
        for c in roots:
            if snippet and snippet not in self.b.text_of(c, 300):
                continue
            for s in as_list(self.b.sel.get("comment_expand")):
                try:
                    btns = c.locator(s).all()
                except Exception:  # noqa: BLE001
                    continue
                for btn in btns[:3]:
                    if self.b.safe_click(btn, "展开回复"):
                        clicked = True
                        self.b.pause(1.0, 1.8)
            if snippet and clicked:
                break
        return clicked

    # ---------- 私信 ----------
    # 2026-11 实测：私信是页面右侧的一个面板，左边会话列表、右边聊天；打开某个聊天后会盖住会话列表，
    # 要先点左上角的返回箭头回到列表。聊天消息是倒着排的（最新的一条在最前面）。
    def _dm_panel_open(self) -> bool:
        return self.b.find("dm_panel", timeout=800) is not None

    def open_dm_panel(self) -> bool:
        if self._dm_panel_open():
            return True
        entry = self.b.find("dm_entry", timeout=4000)
        if entry is None:
            log.warning("没找到“私信”入口")
            return False
        if not self.b.safe_click(entry, "私信入口"):
            return False
        self.b.pause(1.5, 2.5)
        self.b.check_captcha()
        return self.b.find("dm_panel", timeout=5000) is not None

    def dm_badge(self) -> bool:
        """不打开私信面板，只看右上角“消息”旁有没有未读数字（秒回模式每看完一个视频看一眼）。"""
        entry = self.b.find("dm_entry", timeout=500)
        if entry is None:
            return False
        try:
            text = entry.inner_text(timeout=800)
        except Exception:  # noqa: BLE001
            return False
        return bool(re.search(r"\d", text or ""))

    def close_dm_panel(self) -> None:
        """处理完私信后关掉面板，免得盖住刷视频的页面。"""
        if not self._dm_panel_open():
            return
        btn = self.b.find("dm_close", timeout=1000)
        if btn is None or not self.b.safe_click(btn, "关闭私信面板"):
            try:
                self.b.page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass
        self.b.pause(0.6, 1.2)

    def _chat_title(self) -> str:
        return self.b.text_of(self.b.find("dm_chat_title", timeout=800), 40).strip()

    def _conv_user(self, conv) -> str:
        name = self.b.text_of(self.b.find("dm_conv_title", timeout=300, root=conv), 40).strip()
        if not name:
            preview = self.b.text_of(conv, 120)
            name = preview.split()[0] if preview else ""
        return name[:30]

    def _back_to_list(self) -> bool:
        """回到会话列表。打开着的聊天会盖住列表（列表在页面上仍算“可见”，但点不到），所以先点返回。"""
        if self._chat_title():
            back = self.b.find("dm_back", timeout=1500)
            if back is not None and self.b.safe_click(back, "私信返回"):
                self.b.pause(0.8, 1.4)
        return self.b.find("dm_conversation_item", timeout=3000) is not None

    def _open_conv(self, user: str) -> bool:
        """打开和 user 的聊天。已经开着就直接用；否则回到会话列表按名字找。"""
        if not self.open_dm_panel():
            return False
        if user and self._chat_title() == user:
            return True
        for attempt in range(2):
            if not self._back_to_list():
                log.warning("回不到私信会话列表")
                return False
            conv = next((c for c in self.b.find_all("dm_conversation_item")[:30] if self._conv_user(c) == user), None)
            if conv is None:
                log.warning("私信列表里没找到 %s", user)
                return False
            if self.b.safe_click(conv, f"和 {user} 的会话"):
                self.b.pause(1.0, 1.8)
                return self.b.find("dm_input", timeout=4000) is not None
        return False

    def _read_chat(self, n: int = 8) -> list[dict]:
        """当前聊天里最近 n 条消息，按时间从旧到新。

        每条：{"from_me": bool, "kind": "text" / "share" / "unsupported", "text": str, "url": 分享的视频地址, "el": 元素}
        """
        items = self.b.find_all("dm_message_item")
        items = items[:n][::-1] if self.b.sel.get("dm_newest_first", True) else items[-n:]
        mark = (as_list(self.b.sel.get("dm_from_me")) or ["[class*='isFromMe']"])[0]
        out = []
        for m in items:
            text = self.b.text_of(m, 300).strip()
            try:
                from_me = bool(m.evaluate("(el, sel) => !!el.closest(sel)", mark))
            except Exception:  # noqa: BLE001
                from_me = False
            kind, url = self._classify(m, text)
            if kind == "text" and not text:
                continue
            out.append({"from_me": from_me, "kind": kind, "text": text if kind != "unsupported" else "",
                        "url": url, "el": m})
        return out

    def _classify(self, m, text: str) -> tuple[str, str]:
        """判断一条消息是文字、分享的视频，还是网页版显示不了的内容。"""
        if self.b.sel.get("dm_unsupported_text", "暂不支持该消息类型") in text:
            return "unsupported", ""
        url = self._share_link(m)
        if url:
            return "share", url
        for s in as_list(self.b.sel.get("dm_share_card")):
            try:
                card = m.locator(s).first
                if card.count() > 0:
                    # 视频分享卡片（封面 + 作者），网页上没有链接：先从网页自己的数据里读视频 id
                    vid = pick_unique(react_values(card, ["itemId", "item_id", "aweme_id", "awemeId"], r"^\d{15,20}$"),
                                      prefer="_parsedContent")
                    return "share", (f"https://www.douyin.com/video/{vid}" if vid else "")
            except Exception:  # noqa: BLE001
                continue
        for s in as_list(self.b.sel.get("dm_text_bubble")):
            try:
                if m.locator(s).count() > 0:
                    return "text", ""
            except Exception:  # noqa: BLE001
                continue
        return "share", ""  # 不是文字气泡：多半是分享卡片（视频封面 + 标题），地址等需要时点开再拿

    def _share_link(self, m) -> str:
        for s in as_list(self.b.sel.get("dm_share_link")):
            try:
                loc = m.locator(s).first
                if loc.count() > 0:
                    url = video_url(loc.get_attribute("href", timeout=500) or "")
                    if url:
                        return url
            except Exception:  # noqa: BLE001
                continue
        return ""

    def _aweme_ids_on_page(self) -> set:
        try:
            return set(self.b.page.eval_on_selector_all(
                "[data-e2e-aweme-id]", "els => els.map(e => e.getAttribute('data-e2e-aweme-id'))") or [])
        except Exception:  # noqa: BLE001
            return set()

    def _share_link_by_click(self, m) -> str:
        """分享卡片上没有链接、网页数据里也没读到：点一下卡片，看它打开了哪个视频。"""
        target = m
        for sel in as_list(self.b.sel.get("dm_share_card")):
            try:
                loc = m.locator(sel).first
                if loc.count() > 0:
                    target = loc
                    break
            except Exception:  # noqa: BLE001
                continue
        return self._video_url_by_click(target, "dm_share_click")

    def _video_url_by_click(self, target, tag: str) -> str:
        """点一下 target，看打开了哪个视频——新开的标签页、当前页地址（/video/ 或 modal_id），
        或者页面上新出现的视频 id。拿到后关掉弹出的视频，回到原来的页面。"""
        page = self.b.page
        before_url = page.url
        before_ids = self._aweme_ids_on_page()
        url = ""
        try:
            with page.context.expect_page(timeout=6000) as info:
                target.click(timeout=3000)
            tab = info.value
            try:
                tab.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:  # noqa: BLE001
                pass
            url = video_url(tab.url)
            tab.close()
            self.b._activate_tab(page)
            if url:
                return url
        except Exception:  # noqa: BLE001
            pass
        self.b.pause(1.5, 2.5)
        if page.url != before_url:
            url = video_url(page.url)
        if not url:
            new_ids = [i for i in self._aweme_ids_on_page() - before_ids if i and i.isdigit()]
            if len(new_ids) == 1:
                url = f"https://www.douyin.com/video/{new_ids[0]}"
        if not url:
            self._dump_once(tag, "点开后没认出是哪个视频")
        try:
            page.keyboard.press("Escape")  # 关掉弹出来的视频
            if page.url != before_url:
                page.go_back(wait_until="commit", timeout=10000)
        except Exception:  # noqa: BLE001
            pass
        self.b.pause(0.8, 1.4)
        return url

    _unknown_dumped = False
    _dumped_tags: set = set()

    def _dump_unknown_once(self, why: str) -> None:
        if Inbox._unknown_dumped:
            return
        Inbox._unknown_dumped = True
        path = self.b.dump("dm_share_unreadable")
        log.warning("%s，页面已保存到 %s（发给开发者就能校准）", why, path)

    def _dump_once(self, tag: str, why: str) -> None:
        if tag in Inbox._dumped_tags:
            return
        Inbox._dumped_tags.add(tag)
        path = self.b.dump(tag)
        log.warning("%s，页面已保存到 %s（发给开发者就能校准）", why, path)

    def watch_shared(self, url: str, n_frames: int, can_see: bool, max_s: float = 180,
                     sleep=None, progress=None, on_open=None) -> tuple[dict, list[bytes]]:
        """在新标签页里把对方分享的视频完整看一遍：读文案、作者、热评，从头播到尾（最多 max_s 秒），
        能识图的话在整段视频里均匀截 n_frames 帧。"""
        with self.b.in_new_tab(url):
            video = self.feed.read_detail(with_comments=True)
            if on_open:
                on_open(video)
            frames, watched, duration = self.feed.watch_through(n_frames if can_see else 0, max_s, sleep, progress)
        video.update(watched_s=round(watched), duration_s=round(duration))
        return video, frames

    # ---------- 私信对象的资料：头像、昵称、抖音号、有没有关注她 ----------
    def _conv_avatar(self, conv) -> str:
        for sel in as_list(self.b.sel.get("dm_conv_avatar")):
            try:
                src = conv.locator(sel).first.get_attribute("src", timeout=300) or ""
            except Exception:  # noqa: BLE001
                src = ""
            if src.startswith("http"):
                return src
        return ""

    def _profile_link_in_im(self) -> str:
        for sel in as_list(self.b.sel.get("dm_profile_link")):
            try:
                for a in self.b.page.locator(sel).all()[:10]:
                    url = user_url(a.get_attribute("href", timeout=300) or "")
                    if url:
                        return url
            except Exception:  # noqa: BLE001
                continue
        return ""

    def profile_url_from_chat(self, user: str = "") -> str:
        """当前打开的聊天对象的主页地址。

        1. 抖音网页打开私信时自己加载过这个人的资料（有主页 id）就直接用；
        2. 私信面板 / 弹出的资料卡片里有主页链接就用（只在私信面板里找，不会拿到后面视频作者的链接）；
        3. 点聊天顶部的头像，看它打开了哪个主页。
        """
        info = self.b.known_user(user) if user else None
        if info and info.get("sec_uid"):
            return f"https://www.douyin.com/user/{info['sec_uid']}"
        sec = self.sec_uids.get(user) or self._sec_uid_from_header()
        if sec:
            return f"https://www.douyin.com/user/{sec}"
        url = self._profile_link_in_im()
        if url:
            return url
        avatar = self.b.find("dm_chat_avatar", timeout=1500)
        if avatar is None:
            return ""
        page = self.b.page
        before = page.url
        try:
            with page.context.expect_page(timeout=5000) as info:
                avatar.click(timeout=3000)
            tab = info.value
            try:
                tab.wait_for_load_state("commit", timeout=10000)
            except Exception:  # noqa: BLE001
                pass
            url = user_url(tab.url)
            tab.close()
            self.b._activate_tab(page)
            return url
        except Exception:  # noqa: BLE001
            pass
        if page.url != before:
            url = user_url(page.url)
            try:
                page.go_back(wait_until="commit", timeout=15000)
            except Exception:  # noqa: BLE001
                pass
            return url
        self.b.pause(0.8, 1.4)
        url = self._profile_link_in_im()  # 可能弹出了资料卡片
        try:
            page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass
        return url

    def _sec_uid_from_header(self) -> str:
        """聊天窗口顶部（对方头像和名字）背后的数据里的主页 id。"""
        head = self.b.find("dm_chat_header", timeout=800)
        return pick_unique(react_values(head, ["secUid", "sec_uid"], SEC_UID_RE)) if head is not None else ""

    def read_profile(self, url: str, expect_name: str = "") -> dict:
        """在新标签页打开对方主页，读昵称、抖音号、头像，以及关注按钮上的字（回关 / 互相关注 = 对方关注了她）。"""
        with self.b.in_new_tab(url):
            info = self.b.find("profile_info", timeout=15000)
            if info is None:
                self.b.dump("profile_read_fail")
                return {}
            text = self.b.text_of(info, 600)
            m = re.search(r"抖音号[:：]\s*([^\s]+)", text)
            avatar = ""
            for sel in as_list(self.b.sel.get("profile_avatar")):
                try:
                    avatar = self.b.page.locator(sel).first.get_attribute("src", timeout=1000) or ""
                except Exception:  # noqa: BLE001
                    avatar = ""
                if avatar.startswith("http"):
                    break
            name = self.b.text_of(self.b.find("profile_name", timeout=1500), 60).strip()
            btn = self.b.text_of(self.b.find("profile_follow_btn", timeout=3000), 20).strip()
            words = as_list(self.b.sel.get("profile_follows_me_words")) or ["回关", "互相关注"]
        if expect_name and not _same_name(name, expect_name):
            log.warning("打开的主页是「%s」，不是私信里的「%s」，不保存这份资料", name, expect_name)
            return {}
        return {
            "nickname": name,
            "douyin_id": m.group(1) if m else "",
            "avatar": avatar if avatar.startswith("http") else "",
            "url": url,
            "follow_btn": btn,
            "follows_me": (any(w in btn for w in words)) if btn else None,
        }

    def _has_unread(self, conv) -> bool:
        for s in as_list(self.b.sel.get("dm_unread_badge")):
            try:
                if conv.locator(s).count() > 0:
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def fetch_unread_dms(self, limit: int = 10, need_profile=None) -> list[dict]:
        """need_profile(user) 返回 True 时，顺便去对方主页读资料（昵称、抖音号、头像、有没有关注她）。"""
        if not self.open_dm_panel() or not self._back_to_list():
            return []
        users, avatars = [], {}
        for conv in self.b.find_all("dm_conversation_item")[:20]:
            if self._has_unread(conv):
                u = self._conv_user(conv)
                if u and u not in users:
                    users.append(u)
                    avatars[u] = self._conv_avatar(conv)
                    # 会话这一行背后的数据里有对方的主页 id；同一行里出现两个不同的 id（比如群聊）就不认
                    sec = pick_unique(react_values(conv, ["secUid", "sec_uid"], SEC_UID_RE, up=2))
                    if sec:
                        self.sec_uids[u] = sec
        out = []
        for user in users[:limit]:
            if not self._open_conv(user):
                continue
            profile = {"avatar": avatars.get(user, "")}  # 私信列表里的头像，一定是这个人的
            known = self.b.known_user(user)
            if known:
                profile.update({k: known[k] for k in ("nickname", "douyin_id") if known.get(k)})
                profile["url"] = f"https://www.douyin.com/user/{known['sec_uid']}"
            elif self.sec_uids.get(user):
                profile["url"] = f"https://www.douyin.com/user/{self.sec_uids[user]}"
            if need_profile and need_profile(user):
                url = self.profile_url_from_chat(user)
                if url:
                    try:
                        got = self.read_profile(url, expect_name=user)
                        profile.update({k: v for k, v in got.items() if v not in ("", None) and k != "avatar"})
                    except Exception as e:  # noqa: BLE001
                        log.warning("读 %s 的主页失败：%s", user, str(e).splitlines()[0][:120])
                else:
                    self._dump_once("dm_profile_unknown", f"找不到 {user} 的主页入口")
            msgs = self._read_chat(15)
            if not msgs or msgs[-1]["from_me"]:
                continue  # 最后一条是她自己发的，不用再回
            theirs = []  # 对方在她上次说话之后连着发的几条
            for m in reversed(msgs):
                if m["from_me"]:
                    break
                theirs.insert(0, m)
            shares, lines = [], []
            asked = " ".join(m["text"] for m in theirs if m["kind"] == "text")
            # 只看对方这次新发来的消息里的视频卡片，多张时只用最新的一张；以前发过的卡片不再翻出来
            newest = next((m for m in reversed(theirs) if m["kind"] == "share"), None)
            need_share = newest is None and bool(VIDEO_ASK.search(asked))
            for m in theirs:
                if m["kind"] == "share" and m is not newest:
                    lines.append("[还分享了另一个视频，这次只看最新的那个]")
                    continue
                if m["kind"] == "text":
                    lines.append(m["text"])
                elif m["kind"] == "share":
                    url = m["url"] or self._share_link_by_click(m["el"])
                    label = "分享了一个视频"
                    if url:
                        shares.append({"url": url, "title": m["text"][:80]})
                        lines.append(f"[{label}]{(' 作者 ' + m['text'][:40]) if m['text'] else ''}")
                    else:
                        self._dump_unknown_once("对方发来的分享卡片读不到视频地址")
                        lines.append("[分享了一条内容，但网页版打不开]")
                else:
                    self._dump_unknown_once("对方发了一条网页版显示不了的消息")
                    lines.append("[发了一条网页版显示不了的内容，可能是表情包、图片或直播分享]")
            if not lines:
                continue
            text = "\n".join(lines)[-400:]
            out.append({"ext_id": "dm_" + _hid(user, text, today_str()), "user": user, "text": text,
                        "shares": shares[:1], "need_share": need_share, "profile": profile,
                        "recent": [("我：" if m["from_me"] else "对方：") + (m["text"] or f"[{m['kind']}]") for m in msgs]})
        self._back_to_list()
        return out

    def reply_dm(self, item: dict, text: str) -> bool:
        user = item.get("user", "")
        if not self._open_conv(user):
            return False
        box = self.b.find("dm_input", timeout=4000)
        if box is None or not self.b.safe_click(box, "私信输入框"):
            log.warning("没找到私信输入框")
            return False
        self.b.pause(0.4, 0.9)
        self.b.type_slowly(text)
        self.b.pause(0.5, 1.0)
        self.b.page.keyboard.press("Enter")
        self.b.pause(1.2, 2.0)
        self.b.check_captcha()
        # 确认真的发出去了：最新一条应该是她自己发的这句
        msgs = self._read_chat(3)
        if msgs and not (msgs[-1]["from_me"] and text[:8] in msgs[-1]["text"]):
            log.warning("私信好像没发出去（最新一条：%s）", msgs[-1]["text"][:30])
            return False
        return True


def update_bio(b: DouyinBrowser, bio: str) -> tuple[bool, str]:
    """把个性签名同步到抖音主页简介。返回 (是否成功, 说明)。

    流程（2026-11 实测页面）：我的主页 → 点名字旁边的铅笔图标 → 弹出“编辑资料” →
    清空“简介”富文本框并输入 → 点“保存” → 确认弹窗关闭。
    """
    def fail(step: str) -> tuple[bool, str]:
        log.warning("同步简介失败：%s", step)
        b.dump("bio_fail")
        return False, step

    b.goto(b.sel["profile_url"])
    btn = b.find("edit_profile_button", timeout=8000)
    if btn is None:
        return fail("没找到编辑资料的入口（名字旁边的铅笔图标）")
    b.click(btn)
    b.pause(1.5, 2.5)
    editor = b.find("bio_editor", timeout=6000)
    if editor is None:
        return fail("打开了编辑资料，但没找到简介输入框")

    try:
        tag = (editor.evaluate("e => e.tagName") or "").lower()
    except Exception:  # noqa: BLE001
        tag = ""
    if tag == "textarea":
        editor.fill(bio)
    else:
        # 富文本编辑器：点进去、全选、删除，再一个字一个字输入
        b.click(editor)
        b.pause(0.3, 0.6)
        b.page.keyboard.press("Control+A")
        b.page.keyboard.press("Delete")
        b.pause(0.3, 0.6)
        b.type_slowly(bio)
    b.pause(0.8, 1.4)

    save = b.find("save_profile_button", timeout=4000)
    if save is None:
        return fail("没找到“保存”按钮")
    try:
        disabled = save.is_disabled(timeout=1000)
    except Exception:  # noqa: BLE001
        disabled = False
    if disabled:
        # 内容和原来一模一样时，抖音不让点保存
        try:
            b.page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass
        log.info("简介没有变化，不需要保存")
        return True, "简介和抖音上的一样，不需要更新"
    b.click(save)
    b.pause(2.0, 3.0)
    b.check_captcha()
    if b.find("bio_editor", timeout=1500) is not None:
        return fail("点了保存，但编辑框没有关闭，可能是简介内容不符合抖音要求")
    log.info("同步简介成功：%s", bio)
    return True, ""
