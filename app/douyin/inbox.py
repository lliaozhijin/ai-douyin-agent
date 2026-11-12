"""通知里的 @ 和私信：读取、回复。

抖音网页版的通知和私信面板结构经常变化，这里的定位全部来自 selectors.json，
第一次真机运行时如果读不到，用“设置 → 保存当前页面结构”把截图发给开发者校准。
"""
from __future__ import annotations

import hashlib
import logging
import re
import time

from .. import cancel

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


# 一条评论所在的那一串：楼主 + 楼里到这条评论为止的回复，每条 {user 名字, text 正文}。在那条评论的元素上执行。
THREAD_ROWS_JS = r"""
(el, itemSel) => {
  const top = (el.parentElement && el.parentElement.closest(itemSel)) || el;
  const rows = [top, ...top.querySelectorAll(itemSel)];
  const own = (row, sel) => [...row.querySelectorAll(sel)].find((x) => x.closest(itemSel) === row);
  const textOf = (row) => {
    let box = own(row, '[data-e2e="comment-content"], .comment-item-content');
    if (!box) {
      const info = own(row, '.comment-item-info-wrap');
      const nx = info && info.nextElementSibling;
      if (nx && !nx.matches('.comment-item-stats-container') && !nx.querySelector('.comment-item-stats-container, button')) box = nx;
    }
    if (!box) return '';
    let out = '';
    const walk = (n) => {
      if (n.nodeType === 3) { out += n.textContent; return; }
      if (n.nodeType !== 1) return;
      if (n.tagName === 'IMG') { out += n.getAttribute('alt') || ''; return; }
      for (const c of n.childNodes) walk(c);
    };
    walk(box);
    return out.replace(/\s+/g, ' ').trim().slice(0, 200);
  };
  const userOf = (row) => {
    const a = [...row.querySelectorAll('a[href*="/user/"]')].find((x) => x.closest(itemSel) === row && (x.textContent || '').trim());
    return a ? (a.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 30) : '';
  };
  const out = [];
  for (const row of rows) {
    out.push({ user: userOf(row), text: textOf(row) });
    if (row === el && row !== top) break;      // 到对方这条为止（后面的是更晚的回复）
  }
  return out;
}
"""

# 评论区里还没看过、下面有“展开 N 条回复”的评论：给它们编上号（data-aiw-thread），返回 [{key 编号, label 按钮上的字}]。
# 看过的评论记上 data-aiw-seen，下次不再看。参数：评论的选择器、展开按钮的选择器（只要普通 CSS 的）。
THREADS_JS = r"""
([itemSel, expandSels]) => {
  const out = [];
  window.__aiwThread = window.__aiwThread || 0;
  for (const row of document.querySelectorAll(itemSel)) {
    if (row.hasAttribute('data-aiw-seen')) continue;
    row.setAttribute('data-aiw-seen', '1');
    let btn = null;
    for (const s of expandSels) {
      try { btn = row.querySelector(s); } catch (e) { btn = null; }
      if (btn) break;
    }
    if (!btn) {
      btn = [...row.querySelectorAll('button, [role="button"]')]
        .find((b) => /展开\s*\d*\s*条回复|展开更多/.test(b.textContent || '')) || null;
    }
    if (!btn) continue;
    const label = (btn.textContent || '').replace(/\s+/g, '').slice(0, 30);
    if (label.includes('收起')) continue;
    const key = String(++window.__aiwThread);
    row.setAttribute('data-aiw-thread', key);
    out.push({ key, label });
  }
  return out;
}
"""

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
        self.last_notice: dict = {}  # 上次看“互动消息”的情况（给日志用）
        self.last_reply_note = ""    # 上次回复 @ / 评论的补充说明（没找到那条评论、改成 @ 回去了……）
        self._notice_blank = False   # 面板里明确写着“没有消息”

    # ---------- @ 我的（右上角“通知” → “互动消息”面板） ----------
    def open_notice_panel(self) -> bool:
        if self.b.find("notice_panel", timeout=800) is not None:
            return True
        entry = self.b.find("notice_entry", timeout=4000)
        if entry is None:
            log.warning("没找到“通知”入口")
            return False
        ok = self.b.click_open(entry, "通知", lambda t: self.b.find("notice_panel", timeout=t) is not None)
        self.b.check_captcha()
        return ok

    def _mouse_away(self) -> None:
        """把鼠标挪到页面左边的空白处。“通知”是鼠标停在上面就弹出来的面板：鼠标不挪开它会一直开着，
        盖住旁边的私信面板（私信的“返回”“关闭”就点不到了）。"""
        try:
            self.b.page.mouse.move(40, 420)
        except Exception:  # noqa: BLE001
            pass

    def _notice_out_of_the_way(self) -> None:
        """要点私信面板之前：通知面板还弹着就先让它收起来（只挪鼠标，不按 Esc，免得把私信面板也关了）。"""
        if self.b.find("notice_panel", timeout=300) is None:
            return
        self._mouse_away()
        for _ in range(4):
            cancel.sleep(0.4)
            if self.b.find("notice_panel", timeout=200) is None:
                return
        log.info("通知面板挪开鼠标后还开着，点一下空白处")
        try:
            self.b.page.mouse.click(40, 420)
        except Exception:  # noqa: BLE001
            pass
        cancel.sleep(0.5)

    def _notice_rows(self, wait_s: float = 6.0) -> list:
        """面板刚打开时通知还在加载：等通知出来再读（原来一打开就读，经常读到 0 条）。
        面板里明确写着没有消息，或者等够了时间，才算没有。"""
        end = time.time() + wait_s
        while True:
            rows = self.b.find_all("notice_item")[:40]
            if rows:
                return rows
            panel_text = self.b.text_of(self.b.find("notice_panel", timeout=300), 300)
            self._notice_blank = any(w in panel_text for w in
                                     as_list(self.b.sel.get("notice_empty_text") or ["暂无消息", "暂无通知"]))
            if self._notice_blank or time.time() >= end:
                return []
            cancel.sleep(0.5)

    def close_notice_panel(self) -> None:
        self._mouse_away()
        self.b.pause(0.3, 0.6)
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
        self.last_notice = {"opened": False}
        if not self.open_notice_panel():
            return []
        items, seen = [], set()
        self._notice_blank = False
        rows = self._notice_rows()
        if not rows and not self._notice_blank:
            self._dump_once("notice_rows_empty", "通知面板打开了，但等了几秒也没读到一条通知")
        counts = {"at": 0, "reply": 0}
        for row in rows:
            it = self._parse_notice(row, mine)
            if it:
                counts[it["kind"]] = counts.get(it["kind"], 0) + 1
            if it and it["kind"] in kinds and it["ext_id"] not in seen:
                seen.add(it["ext_id"])
                items.append(it)
        # 给日志用：面板打开了没有、读到几条通知、其中几条是 @ / 回复她的
        self.last_notice = {"opened": True, "rows": len(rows), **counts}
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
        """在视频评论区找到对方那条评论（优先按评论 id，其次按内容 + 名字），点“回复”后发送。

        找的顺序（每一步都记日志）：① 已经加载出来的评论里直接找；② 知道楼主是哪条评论（互动消息里带的，
        或者她当初回复的就是那条）：找到楼主、把那一串回复展开了找；③ 像人一样从“互动消息”里点这条通知进去
        （抖音自己会打开那个视频，通常把这条评论放到评论区最前面——评论几千上万条的视频，新评论排得很后，
        靠往下翻是翻不到的）；④ 她那条评论在顶层：展开它下面的回复；⑤ 一边往下翻一边找；
        ⑥ 一边往下翻、一边把折叠着的回复一串串展开找（回复她的至少在 2 条回复以上的串里）。

        都找不到：是 @ 她的，就在评论区发一条 @ 对方的评论把话带到（对方一样会收到提醒）；
        是回复她评论的，不发，原因记在 last_reply_note。"""
        self.last_reply_note = ""
        if not item.get("video_url"):
            return False  # 不知道是哪个视频，不能乱发
        if not self.feed.open_comments():
            self.last_reply_note = "那个视频的评论区没打开"
            return False
        who = item.get("user") or "对方"
        is_reply = item.get("kind") == "reply"
        mine = item.get("mine") or ""
        target = self._find_comment(item, deep=False)
        step = "已加载的评论里"
        if target is None and (item.get("root_cid") or item.get("root_text")):
            step = "楼主那串回复里"
            target = self._find_in_thread(item, item.get("root_cid") or "", item.get("root_text") or "",
                                          item.get("root_user") or "")
        if target is None:
            res = self._reply_via_notice(item, text)
            if res is not None:
                return res                      # 从通知点进去找到了，已经回复过（发出去了 / 没发出去 / 不确定）
            if not self.feed.open_comments():   # 点通知的时候评论区可能被收起来了
                self.last_reply_note = "那个视频的评论区没打开"
                return False
        if target is None and is_reply and mine and not item.get("root_text"):
            # 她那条评论是顶层评论：找到它、展开下面折叠的回复
            step = "她那条评论下面"
            if self._find_comment({"text": mine, "user": ""}) is not None:
                self._expand_replies(mine)
                target = self._find_comment(item, deep=False)
        if target is None and not is_reply:
            step = "往下翻评论区"
            target = self._find_comment(item)
        if target is None:
            # 不知道楼主是谁：一边往下翻、一边把折叠着的回复一串串展开找（有上限，不会没完没了）
            step = "一串串展开折叠的回复"
            target = self._search_threads(item, min_replies=2 if is_reply else 1)
        if target is None:
            loaded = len(self.b.find_all("comment_item"))
            known = len(getattr(self.b, "notice_comments", None) or [])
            log.warning("找 %s 的评论：都没找到（评论 id：%s，楼主 id：%s，认出她的评论：%s；评论区加载了 %d 条%s；"
                        "互动消息接口里记到 %d 条评论）", who, item.get("cid") or "不知道",
                        item.get("root_cid") or "不知道", "是" if mine else "否", loaded,
                        "，已经到底" if self._comments_end() else "，还没到底", known)
            # 两种情况分开保存：评论太多翻不到的 / 评论区里根本没几条的
            self._dump_once("mention_comment_missing" if loaded >= 60 else "mention_comment_missing_short",
                            f"在视频评论区里没找到 {who} 那条评论")
            self.feed.close_comments()
            if not is_reply and item.get("user"):
                return self._at_back(item, text)
            self.last_reply_note = "在评论区里没找到对方那条回复（可能排得太靠后、被折叠了，或者已经删除）"
            return False
        log.info("找 %s 的评论：在%s找到了", who, step)
        ok = self.feed.reply_on_item(target, text, (item.get("text") or "")[:12], item.get("user") or "")
        self.feed.close_comments()
        return ok

    def _at_back(self, item: dict, text: str):
        """对方 @ 了她，但在评论区里找不到那条评论（评论太多）：发一条 @ 对方的评论把话带到。
        @ 不上（候选里没有这个人）就不发——不带 @ 的话对方收不到提醒，等于自说自话。"""
        who = item["user"]
        log.info("找不到 %s @ 她的那条评论，改成在评论区发一条 @%s 的评论回复", who, who)
        res = self.feed.comment(text, mention=who, require_mention=True)
        if res:
            self.last_reply_note = f"没找到对方那条评论，改成在评论区 @{who} 回复的"
        elif not getattr(res, "uncertain", False):
            self.last_reply_note = (f"在评论区里没找到 {who} @ 她的那条评论（这个视频评论太多，新评论排得很后），"
                                    f"想改成 @{who} 回复也没成功")
        return res

    def _notice_row(self, item: dict):
        """互动消息面板里的这一条通知（按名字 + 内容认）；面板打不开 / 找不到返回 None。"""
        if not self.open_notice_panel():
            return None
        mine = [item["mine"]] if item.get("mine") else None
        for row in self._notice_rows():
            again = self._parse_notice(row, mine)
            if again and again["user"] == item.get("user") and again["text"] == item.get("text"):
                return row
        return None

    def _reply_via_notice(self, item: dict, text: str):
        """像人一样回复：点“互动消息”里的这条通知，抖音自己打开那个视频，并把这条评论放在评论区最前面，
        在那儿点“回复”。

        返回发送结果；这条路走不通（通知找不到了、点开以后评论区最前面没有这条评论）返回 None，
        由后面的办法接着找。点开的如果是新标签页，回复完关掉、回到原来的页面。"""
        who = item.get("user") or "对方"
        if not item.get("user") or not item.get("text"):
            return None
        page = self.b.page
        before_url = page.url
        row = self._notice_row(item)
        if row is None:
            log.info("从通知点进去找 %s 的评论：互动消息里没找到这条通知", who)
            self.close_notice_panel()
            return None
        # 点通知右边的视频封面（点名字 / 头像会去对方主页，点评论里的 @ 会去被 @ 的人的主页）
        spot = row
        try:
            thumb = row.locator("img").last
            if thumb.count() > 0:
                spot = thumb
        except Exception:  # noqa: BLE001
            spot = row
        tab = None
        try:
            with page.context.expect_page(timeout=5000) as info:
                spot.click(timeout=3000)
            tab = info.value
        except Exception:  # noqa: BLE001 —— 没有新开标签页：可能是在当前页面里打开的
            tab = None
        if tab is not None:
            try:
                tab.wait_for_load_state("domcontentloaded", timeout=15000)
                tab.set_default_timeout(15000)
            except Exception:  # noqa: BLE001
                pass
            self.b.page = tab
            if getattr(self.b, "_emu", None):
                self.b._ensure_layout(tab)
        try:
            if tab is None:
                self.b.pause(1.5, 2.5)
                self.close_notice_panel()          # 还在当前页面：通知面板弹着会盖住评论区
            how = "开了新标签页" if tab is not None else ("页面换了" if self.b.page.url != before_url else "页面没变")
            if tab is not None and not video_url(str(self.b.page.url)):
                log.info("从通知点进去找 %s 的评论：打开的不是视频（%s），不用这个办法", who, str(self.b.page.url)[:90])
                return None
            target = None
            if self.feed.open_comments():
                self.b.pause(0.8, 1.5)
                target = self._find_comment(item, deep=False)
            log.info("从通知点进去找 %s 的评论：%s（%s），%s", who, how, str(self.b.page.url)[:90],
                     "找到了" if target is not None else "评论区最前面没有这条评论")
            if target is None:
                self._dump_once("notice_open_no_comment", f"从通知点进去以后没看到 {who} 那条评论")
                return None
            res = self.feed.reply_on_item(target, text, (item.get("text") or "")[:12], item.get("user") or "")
            self.feed.close_comments()
            return res
        finally:
            if tab is not None:
                self.b.page = page
                emu = getattr(self.b, "_emu", None)
                cdp = emu.pop(tab, None) if isinstance(emu, dict) else None
                try:
                    if cdp is not None:
                        cdp.detach()
                    tab.close()
                except Exception:  # noqa: BLE001
                    pass
                self.b._activate_tab(page)
                self.close_notice_panel()
            elif page.url != before_url:
                try:
                    page.keyboard.press("Escape")       # 关掉弹出来的视频
                    if page.url != before_url:
                        page.go_back(wait_until="commit", timeout=10000)
                except Exception:  # noqa: BLE001
                    pass
                self.b.pause(0.8, 1.4)

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

    def _expand_once(self, root) -> bool:
        """点一次这条评论下面的“展开 N 条回复 / 展开更多”。没有可点的了返回 False。"""
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
            return False
        self.b.pause(0.9, 1.6)
        return True

    def _expand_all(self, root, max_clicks: int = 12) -> None:
        """把这条评论下面的回复全部展开（一直点，直到没有或点够次数）。"""
        for _ in range(max_clicks):
            if not self._expand_once(root):
                return

    def _expand_until(self, root, item: dict, max_clicks: int):
        """一边展开这一串回复一边找，找到就停（回复多的串不用全展开）。"""
        hit = self._match_in(root, item)
        for _ in range(max_clicks):
            if hit is not None or not self._expand_once(root):
                break
            hit = self._match_in(root, item)
        return hit

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

    def _find_in_thread(self, item: dict, root_cid: str, root_text: str = "", root_user: str = ""):
        """楼主那条评论已知（按 id，或按内容 + 作者）：找到它，展开它下面的回复找。"""
        root = self._root_by_cid(root_cid) if root_cid else None
        if root is None and root_text:
            root = self._find_comment({"text": root_text, "user": root_user})
        if root is None:
            log.info("找 %s 的评论：楼主那条评论没找到", item.get("user") or "对方")
            return None
        return self._expand_until(root, item, max_clicks=30)

    @staticmethod
    def _reply_count(text: str) -> int:
        """“展开 3 条回复”里的 3；读不出来返回 0。"""
        m = re.search(r"(\d+)\s*条回复", text or "")
        return int(m.group(1)) if m else 0

    def _thread_candidates(self) -> list:
        """评论区里还没看过、下面有折叠回复的评论：[(这条评论, “展开 N 条回复”几个字)]。

        用网页脚本一次读出来（以前是一条条评论去问“你下面有没有展开按钮”，评论上千条时光问就要一两分钟）；
        看过的评论做上记号，往下翻出新评论以后再读，只会读到新的。"""
        item_sel = as_list(self.b.sel.get("comment_item"))[0]
        css = [x for x in as_list(self.b.sel.get("comment_expand")) if ":has-text" not in x and ":text" not in x]
        try:
            rows = self.b.page.evaluate(THREADS_JS, [item_sel, css])
        except Exception:  # noqa: BLE001
            rows = None
        if not isinstance(rows, list):
            return []
        return [(self.b.page.locator(f"[data-aiw-thread='{r['key']}']").first, str(r.get("label") or ""))
                for r in rows if isinstance(r, dict) and r.get("key")]

    def _search_threads(self, item: dict, max_threads: int = 40, max_seconds: float = 240.0, min_replies: int = 1):
        """一串串展开折叠的回复去找（从上往下，最多展开 max_threads 串、最多找 max_seconds 秒）。

        min_replies：这串至少要有几条回复才值得展开。回复她评论的，一定在她的回复后面，
        所以那一串至少有 2 条回复——只有 1 条回复的串（评论区里大多数是这种）直接跳过，省下大半时间。

        已经加载出来的都看完了还没找到，就往下翻、加载下一批评论接着找，直到评论区到底、翻够了、或者到时间
        （以前只看刚打开评论区时加载的那一二十条：回复她的那一串排在后面就永远找不到）。"""
        start = time.time()
        done = skipped = 0
        last_n, stale = -1, 0

        def over() -> bool:
            return done >= max_threads or time.time() - start > max_seconds

        for _ in range(int(self.b.sel.get("comment_search_rounds") or 40)):
            for c, label in self._thread_candidates():
                if over():
                    break
                n = self._reply_count(label)
                if n and n < min_replies:
                    skipped += 1
                    continue
                done += 1
                try:
                    c.scroll_into_view_if_needed(timeout=2000)
                except Exception:  # noqa: BLE001
                    pass
                hit = self._expand_until(c, item, max_clicks=6)
                if hit is not None:
                    return hit
                self.b.check_captcha()
            if over() or self._comments_end():
                break
            items = self.b.find_all("comment_item")
            stale = stale + 1 if len(items) == last_n else 0
            if stale >= 3:
                break  # 连续几次都没有加载出新评论：到底了
            last_n = len(items)
            self._load_more_comments(items)
            self.b.check_captcha()
            if item.get("cid"):
                # 新加载出来的评论里直接就有这条（按评论 id 认，很快）
                try:
                    loc = self.b.page.locator(self.feed.cid_selector(item["cid"])).last
                    if loc.count() > 0:
                        loc.scroll_into_view_if_needed(timeout=2000)
                        return loc
                except Exception:  # noqa: BLE001
                    pass
        log.info("找 %s 的评论：展开了 %d 串回复（跳过 %d 串只有 1 条回复的），评论区翻到 %d 条，用了 %d 秒，没找到",
                 item.get("user") or "对方", done, skipped, max(last_n, 0), int(time.time() - start))
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
        self._notice_out_of_the_way()
        if self._dm_panel_open():
            return True
        entry = self.b.find("dm_entry", timeout=4000)
        if entry is None:
            log.warning("没找到“私信”入口")
            return False
        ok = self.b.click_open(entry, "私信入口", lambda t: self.b.find("dm_panel", timeout=t) is not None)
        self.b.check_captcha()
        return ok

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
        """处理完私信后关掉面板，免得盖住刷视频的页面。

        2026-11 实测：聊天开着的时候页面上有两个“关闭”（会话列表一个、聊天一个，叠在同一个位置），
        排在前面的那个被聊天那一层盖着，点它会一直等到超时；能点到的是最上面（排在最后）的那个。
        所以从后往前挨个试，普通点不到再换“不等可点直接点”和“用网页脚本点”，都不行按 Esc；全都不行才记一条警告。"""
        if not self._dm_panel_open():
            return
        self._notice_out_of_the_way()      # 通知面板弹着会盖住关闭按钮
        btns = []
        for sel in as_list(self.b.sel.get("dm_close")):
            try:
                btns = list(self.b.page.locator(sel).all())
            except Exception:  # noqa: BLE001
                btns = []
            if btns:
                break
        ways = (("普通点击", lambda b: b.click(timeout=1500)),
                ("强制点击", lambda b: b.click(timeout=1500, force=True)),
                ("脚本点击", lambda b: b.evaluate(
                    "el => el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}))")))
        reason = ""
        for name, fn in ways:
            for btn in reversed(btns):
                cancel.checkpoint()
                try:
                    fn(btn)
                except Exception as e:  # noqa: BLE001
                    reason = f"{name}：{str(e).splitlines()[0][:80]}"
                    continue
                self.b.pause(0.5, 1.0)
                if not self._dm_panel_open():
                    return
        try:
            self.b.page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass
        self.b.pause(0.6, 1.2)
        if self._dm_panel_open():
            log.warning("私信面板没关掉，可能会挡住后面的操作（找到 %d 个关闭按钮%s）", len(btns),
                        f"，最后一次：{reason}" if reason else "")

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
        self._notice_out_of_the_way()      # 通知面板弹着会盖住“返回”
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

    def fetch_unread_dms(self, limit: int = 10, need_profile=None, only=None, skip=None) -> list[dict]:
        """need_profile(user) 返回 True 时，顺便去对方主页读资料（昵称、抖音号、头像、有没有关注她）。

        only：只看这些人的未读（主人；别人的会话不点开，未读留着）。skip：这些人的未读不点开。"""
        if not self.open_dm_panel() or not self._back_to_list():
            return []
        users, avatars = [], {}
        for conv in self.b.find_all("dm_conversation_item")[:20]:
            if self._has_unread(conv):
                u = self._conv_user(conv)
                if (only is not None and u not in only) or (skip and u in skip):
                    continue
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
            # 这批消息的“身份证”里带上她上一句说的话：对方隔一会儿又说了一遍同样的话（“在吗”“哈哈”），
            # 中间她回过话了，就是新的一批——以前只按“谁 + 说了什么 + 哪天”认，同一天说第二遍会被当成处理过、不回
            prev = next((m["text"] for m in reversed(msgs) if m["from_me"]), "")
            out.append({"ext_id": "dm_" + _hid(user, prev[:60], text, today_str()), "user": user, "text": text,
                        "lines": lines[-12:],
                        "shares": shares[:1], "need_share": need_share, "profile": profile,
                        "recent": [("我：" if m["from_me"] else "对方：") + (m["text"] or f"[{m['kind']}]") for m in msgs]})
        self._back_to_list()
        return out

    def reply_dm(self, item: dict, text: str) -> bool:
        self.last_reply_note = ""
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

    def dm_missed(self, user: str, answered: list, reply: str) -> list[str]:
        """她刚回完 user 的私信：看看有没有哪几句对方的话她还没回应到。返回这些话（按时间顺序）。

        两种情况会漏：① 她读完消息、想回复、打字的这段时间里，对方又发了新的——这些话排在她的回复前面，
        下次再看时“最后一条是她发的”，就被当成回过了；② 她回完以后聊天窗口还开着，对方马上又发了一条——
        窗口开着，这条直接算“已读”，会话上没有未读红点，下一轮也不会去看它。
        做法：聊天还开着时，把“她上一次说话之后对方说的所有话”数一遍，去掉这次已经回应的，剩下的就是漏掉的。"""
        try:
            if self._chat_title() != user and not self._open_conv(user):
                return []
            msgs = self._read_chat(15)
        except Exception:  # noqa: BLE001
            return []
        mine = [i for i, m in enumerate(msgs) if m["from_me"]]
        if not mine:
            return []
        key = _squash(reply)[:10]
        sent = next((i for i in reversed(mine) if key and key in _squash(msgs[i]["text"])), None)
        if sent is None:
            return []                          # 没认出她刚发的那句：不乱猜
        start = sent
        while start > 0 and not msgs[start - 1]["from_me"]:
            start -= 1                         # 她的回复前面、连着的对方的话
        before = [m["text"] for m in msgs[start:sent] if m["kind"] == "text" and m["text"]]
        left = [_squash(a) for a in (answered or [])]
        missed = []
        for t in before:
            if _squash(t) in left:
                left.remove(_squash(t))            # 这句这次回应过了
            else:
                missed.append(t)
        after = [m["text"] for m in msgs[sent + 1:] if not m["from_me"] and m["kind"] == "text" and m["text"]]
        return (missed + after)[-8:]

    def thread_context(self, item: dict, limit: int = 10) -> list[dict]:
        """对方那条评论所在的那一串评论（楼主 + 楼里的回复，页面上的顺序）：[{"user": 名字, "text": 内容}]。
        回复之前读给模型，让她接得上这一串的上下文。评论区里一时找不到那条评论就返回空（不影响回复）。"""
        try:
            if not self.feed.open_comments():
                return []
            target = self._find_comment(item, deep=False)
            if target is None and (item.get("root_cid") or item.get("root_text")):
                target = self._find_in_thread(item, item.get("root_cid") or "", item.get("root_text") or "",
                                              item.get("root_user") or "")
            if target is None:
                return []
            rows = target.evaluate(THREAD_ROWS_JS, as_list(self.b.sel.get("comment_item"))[0])
        except Exception:  # noqa: BLE001
            return []
        out = [{"user": str(r.get("user") or "")[:30], "text": str(r.get("text") or "")[:160]}
               for r in (rows or []) if isinstance(r, dict) and r.get("text")]
        return out[:1] + out[1:][-(limit - 1):] if len(out) > limit else out

    def send_dm_file(self, user: str, path) -> bool:
        """在和 user 的私信里发一张图 / 一个 GIF（她自己的表情包）。聊天没开着就先打开。

        抖音私信的“选择文件”是一个隐藏的上传框：把文件交给它，有的版本直接发出去，有的会弹一个确认框要点“发送”。
        发完看最新一条是不是她发的、而且不是文字，确认发出去了。"""
        if self._chat_title() != user and not self._open_conv(user):
            return False
        before_n = len(self.b.find_all("dm_message_item"))
        before = self._dm_newest()
        inp = None
        for s in as_list(self.b.sel.get("dm_file_input")):
            try:
                loc = self.b.page.locator(s).first
                if loc.count() > 0:
                    inp = loc
                    break
            except Exception:  # noqa: BLE001
                continue
        if inp is None:
            log.warning("发表情包：没找到私信里上传文件的地方")
            self._dump_once("dm_file_input_missing", "私信里没找到上传图片的入口")
            return False
        try:
            inp.set_input_files(str(path))
        except Exception as e:  # noqa: BLE001
            log.warning("发表情包：文件没能交给上传框：%s", str(e).splitlines()[0][:120])
            return False
        for _ in range(24):                       # 最多等 12 秒：上传 + 可能的确认框
            cancel.sleep(0.5)
            btn = self.b.find("dm_file_confirm", timeout=200)
            if btn is not None:
                self.b.safe_click(btn, "发送图片")
            if self._dm_file_sent(before, before_n):
                self.b.pause(0.6, 1.2)
                self.b.check_captcha()
                return True
        log.warning("发表情包：图片交上去了，但没看到它出现在聊天里")
        self._dump_once("dm_file_send_fail", "私信里发图片后没看到它出现在聊天里")
        return False

    def _dm_newest(self, n: int = 3) -> list:
        """聊天里最新的 n 条消息各自的样子：是不是她发的、是不是图片、一小段能区分的内容（发图前后对比用）。"""
        try:
            return self.b.page.evaluate(DM_NEWEST_JS, [
                as_list(self.b.sel.get("dm_message_item"))[0],
                (as_list(self.b.sel.get("dm_from_me")) or ["[class*='isFromMe']"])[0],
                as_list(self.b.sel.get("dm_image_bubble")) or ["[class*='MessageItemImage']"],
                bool(self.b.sel.get("dm_newest_first", True)), n]) or []
        except Exception:  # noqa: BLE001
            return []

    def _dm_file_sent(self, before: list, before_n: int) -> bool:
        """图片发出去了没有。

        2026-11 实测：聊天窗口只保留最近十来条消息，新的一条进来、最旧的一条就被挤掉，所以“消息条数变多了”
        这个办法不灵（发出去了条数还是 12，以前因此误报“没发出去”）。改成看最新的一条：是她发的、
        是一张图片（或者没有字），而且和发之前最新的那几条不一样了。条数真的变多了也算。"""
        now = self._dm_newest()
        if now and isinstance(now[0], dict) and now != before:
            top = now[0]
            if top.get("me") and (top.get("image") or not top.get("text")):
                return True
        return len(self.b.find_all("dm_message_item")) > before_n


# 读聊天里最新几条消息的样子（参数：消息的选择器、“她发的”标记、图片气泡的选择器、是不是最新的排在最前、要几条）
DM_NEWEST_JS = r"""
([itemSel, meSel, imgSels, newestFirst, n]) => {
  const items = [...document.querySelectorAll(itemSel)];
  const pick = newestFirst ? items.slice(0, n) : items.slice(-n).reverse();
  return pick.map((el) => {
    let box = null;
    for (const s of imgSels) { try { box = el.querySelector(s); } catch (e) { box = null; } if (box) break; }
    const pic = box ? (box.tagName === 'IMG' ? box : box.querySelector('img')) : null;
    const src = pic ? (pic.currentSrc || pic.src || '') : '';
    const text = (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 40);
    return { me: !!el.closest(meSel), image: !!box, text: text,
             sig: text + '|' + (box ? 'img:' + src.length + ':' + src.slice(-24) : '') };
  });
}
"""


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
