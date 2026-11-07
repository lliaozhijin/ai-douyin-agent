"""推荐页：读取当前视频、截帧、点赞、评论、下一个。"""
from __future__ import annotations

import hashlib
import io
import re
import logging
import time

from .. import cancel
from .browser import BrowserError, DouyinBrowser
from .selectors import as_list

log = logging.getLogger(__name__)


class SendResult(str):
    """发评论 / 回复的结果：sent 确认发出去了；failed 确认没发出去（可以放心让你手动发）；
    uncertain 点了发送但没法确认（可能已经发出去了：不自动重发，免得发两遍）。"""

    def __bool__(self) -> bool:
        return self == "sent"

    @property
    def uncertain(self) -> bool:
        return self == "uncertain"


SENT, FAILED, UNCERTAIN = SendResult("sent"), SendResult("failed"), SendResult("uncertain")

# 一次读出评论区所有评论：评论 id（三个地方都看，对不上就不认）、正文（表情图用它的文字代替）、
# 是不是楼中楼、是不是她自己发的
COMMENT_ROWS_JS = r"""
(meUid) => {
  const rows = [...document.querySelectorAll('[data-e2e="comment-item"]')];
  const idOf = (row) => {
    const ids = new Set();
    const a = row.getAttribute('data-comment-id'); if (a && /^\d+$/.test(a)) ids.add(a);
    const m = /^comment_(\d+)$/.exec(row.id || ''); if (m) ids.add(m[1]);
    // 只看属于这一行自己的 tooltip（不要套在里面的楼中楼的）
    for (const t of row.querySelectorAll('[id^="tooltip_"]')) {
      if (t.closest('[data-e2e="comment-item"]') !== row) continue;
      const v = t.id.slice(8); if (/^\d+$/.test(v)) ids.add(v);
    }
    return ids.size === 1 ? [...ids][0] : '';
  };
  const textOf = (row) => {
    const own = (sel) => [...row.querySelectorAll(sel)].find((el) => el.closest('[data-e2e="comment-item"]') === row);
    let box = own('[data-e2e="comment-content"], .comment-item-content');
    if (!box) {
      // 2026-11 的网页：正文是“作者那一行”后面紧挨着的那一块（再后面是时间、点赞那一行）
      const info = own('.comment-item-info-wrap');
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
    return out.replace(/\s+/g, ' ').trim().slice(0, 512);
  };
  const selfOf = (row) => {
    if (row.getAttribute('data-is-self') === 'true' || row.getAttribute('data-author-is-self') === 'true') return true;
    if (row.querySelector('[data-e2e="comment-self"]')) return true;
    if (!meUid) return null;
    const a = row.querySelector('a[href*="/user/"]');
    return a ? (a.getAttribute('href') || '').includes(meUid) : null;
  };
  return rows.map((row) => ({
    id: idOf(row), text: textOf(row),
    nested: !!(row.parentElement && row.parentElement.closest('[data-e2e="comment-item"]')),
    self: selfOf(row),
  }));
}
"""



def _sq(t: str) -> str:
    return re.sub(r"\s+", "", t or "")


class FeedReadError(BrowserError):
    """当前页面上读不到正在播放的视频信息。"""


class Feed:
    def __init__(self, b: DouyinBrowser):
        self.b = b

    def open(self) -> None:
        """打开“推荐”页，并确认上面真的有正在播放的视频。打不开就报错，不会对着别的页面瞎刷。"""
        b = self.b
        url = b.page.url or ""
        on_detail = re.search(r"/(?:video|note)/\d+", url) or b.find("detail_container", timeout=500) is not None
        if not on_detail and b.find("feed_active", timeout=1500) is not None:
            b.dismiss_tips()
            return  # 已经在推荐页上了
        # 停在单个视频 / 图文的详情页上（比如刚去回复了 @）：那上面也有“正在播放”的视频，但不是推荐页，要离开
        b.goto(b.sel["home_url"])
        # 抖音经常把推荐页跳成“精选”（一排排视频格子）：一发现就点左侧“推荐”，不再干等
        for _ in range(15):
            if b.find("feed_active", timeout=1000) is not None:
                b.pause(1.0, 2.0)
                b.dismiss_tips()  # 第一次进推荐页会弹“滚动鼠标查看更多视频 / 我知道了”的引导
                return
            if "jingxuan" in (b.page.url or ""):
                break
        log.info("没进到推荐页（当前：%s），点左侧“推荐”", b.page.url)
        link = b.find("nav_recommend", timeout=5000)
        if link is not None and b.safe_click(link, "左侧“推荐”"):
            b.pause(2, 4)
            b.check_captcha()
            if b.find("feed_active", timeout=20000) is not None:
                b.pause(1.0, 2.0)
                b.dismiss_tips()
                return
        # 已经在推荐页、只是视频一直转“加载中”（网络慢）：再等一会儿，中途刷新一次
        if "recommend" in (b.page.url or "") or b.loading_visible():
            log.info("推荐页的视频还在加载（%s），再等一会儿", b.page.url)
            if b.wait_any("feed_active", timeout=40) is not None:
                b.pause(1.0, 2.0)
                b.dismiss_tips()
                return
        path = b.dump("feed_open_fail")
        raise BrowserError(f"没能打开“推荐”页（网络慢的话视频会一直“加载中”），当前停在 {b.page.url}"
                          f"（页面已保存到 {path}）。她会自动再试")

    def _active(self):
        return self.b.find("feed_active", timeout=6000)

    def read_current(self, with_comments: bool = True) -> dict:
        active = self._active()
        if active is None:
            raise FeedReadError("当前页面上没有正在播放的推荐视频")
        vid = ""
        for attr in ("data-e2e-vid", "data-vid", "data-id"):
            try:
                vid = active.get_attribute(attr, timeout=1000) or ""
            except Exception:  # noqa: BLE001
                vid = ""
            if vid:
                break
        desc = self.b.text_of(self.b.find("video_desc", timeout=2500, root=active)
                              or self.b.find("video_desc", timeout=1000), 300)
        author = self.b.text_of(self.b.find("video_author", timeout=2500, root=active)
                                or self.b.find("video_author", timeout=1000), 60).lstrip("@")
        if not desc and not author:
            path = self.b.dump("feed_read_fail")
            raise FeedReadError(f"读不到这个视频的文案和作者，页面已保存到 {path}")
        if not vid:
            vid = "h" + hashlib.md5(f"{author}|{desc}".encode("utf-8")).hexdigest()[:16]
        url = f"https://www.douyin.com/video/{vid}" if vid.isdigit() else self.b.page.url
        items = self.read_comments() if with_comments else []
        comments = [f"{c['user']}：{c['text']}" if c["user"] else c["text"] for c in items]
        return {"video_id": vid, "url": url, "author": author, "desc": desc, "comments": comments,
                "comment_items": items}

    def read_detail(self, with_comments: bool = True) -> dict:
        """读单个视频的详情页（www.douyin.com/video/<id>），和推荐页的结构不一样。"""
        b = self.b
        if b.find("detail_container", timeout=20000) is None:
            path = b.dump("detail_read_fail")
            raise FeedReadError(f"打开的不是视频详情页（{b.page.url}），页面已保存到 {path}")
        b.pause(1.0, 2.0)
        b.dismiss_tips()
        desc = b.text_of(b.find("detail_desc", timeout=5000), 300)
        # 图文作品：文案那一块里还带着“发布时间：……”和“展开”按钮，去掉
        desc = re.sub(r"发布时间[:：].*$", "", desc, flags=re.S).replace("展开", " ").strip()
        author = ""
        for a in b.find_all("detail_author")[:4]:
            author = b.text_of(a, 60).strip().lstrip("@")
            if author:
                break
        m = re.search(r"/(?:video|note)/(\d+)", b.page.url or "")
        vid = m.group(1) if m else "h" + hashlib.md5(f"{author}|{desc}".encode("utf-8")).hexdigest()[:16]
        items = self.read_comments() if with_comments else []
        comments = [f"{c['user']}：{c['text']}" if c["user"] else c["text"] for c in items]
        return {"video_id": vid, "url": b.page.url, "author": author, "desc": desc, "comments": comments,
                "comment_items": items}

    def open_comments(self) -> bool:
        if self.b.find("comment_list", timeout=800) is not None:
            return True
        self.b.dismiss_tips()
        # 详情页（尤其图文）右边是“相关推荐 / 评论(N)”两个页签：先点“评论”页签
        if self.b.find("detail_container", timeout=300) is not None:
            tab = self.b.find("detail_comment_tab", timeout=1500)
            if tab is not None and self.b.safe_click(tab, "“评论”页签"):
                self.b.pause(1.0, 1.8)
                if self.b.find("comment_list", timeout=8000) is not None or self.b.find("comment_item", timeout=2000):
                    return True
        btn = self.b.find("comment_button", timeout=3000)
        if btn is None or not self.b.safe_click(btn, "评论按钮"):
            try:  # 点不到按钮时用抖音网页的快捷键 X 打开评论区
                self.b.page.keyboard.press(self.b.sel.get("comment_key", "x"))
            except Exception:  # noqa: BLE001
                return False
        self.b.pause(1.2, 2.2)
        self.b.check_captcha()
        # 评论区打开后常常先转一会儿“加载中”：多等一会儿评论列表出来
        if self.b.find("comment_list", timeout=12000) is not None:
            return True
        if self.b.find("comment_input", timeout=1000) is not None:
            return True  # 评论区开了，只是还没加载出评论
        # 评论区已经打开（顶上有“全部评论(N)”、底下有还没点开的输入框），只是评论一直在“加载中”（网络慢）：再多等一会儿
        opened = False
        for probe in (".comment-input-inner-container", "text=/全部评论\\s*\\(/"):
            try:
                if self.b.page.locator(probe).first.is_visible(timeout=800):
                    opened = True
                    break
            except Exception:  # noqa: BLE001
                continue
        if opened:
            log.info("评论区开了但评论还在加载，再等一会儿")
            self.b.wait_any("comment_list", timeout=30, reload_once=False)
            return True
        return False

    def close_comments(self) -> None:
        close = self.b.find("comment_close", timeout=800)
        try:
            if close is None or not self.b.safe_click(close, "关闭评论区"):
                self.b.page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass
        self.b.pause(0.5, 1.0)

    _comments_dumped = False  # 评论读不到时只自动保存一次页面，方便校准，不刷屏

    def read_comments(self, n: int = 8) -> list[dict]:
        """读前 n 条热评：[{cid 评论 id, user 作者, text 内容, likes 点赞数文字}]。"""
        if not self.open_comments():
            self._dump_comments_once("comments_open_fail", "打不开评论区")
            return []
        # 评论区刚打开时常常还在“加载中”：等第一条评论出现（最多约 8 秒）
        if self.b.find("comment_item", timeout=8000) is None:
            self.b.find("comment_item", timeout=3000)
        out = []
        for item in self.b.find_all("comment_item")[:n]:
            c = self._parse_comment(item)
            if c:
                out.append(c)
        if not out:
            self._dump_comments_once("comments_empty", "评论区打开了，但没读到评论")
        self.close_comments()
        return out

    def _first_attr(self, root, key: str, attr: str) -> str:
        """取元素属性（不要求元素可见，例如鼠标移上去才显示的“...”按钮）。"""
        for s in as_list(self.b.sel.get(key)):
            try:
                v = root.locator(s).first.get_attribute(attr, timeout=300)
            except Exception:  # noqa: BLE001
                v = None
            if v:
                return v
        return ""

    def _parse_comment(self, item) -> dict | None:
        b = self.b
        user = b.text_of(b.find("comment_user", timeout=300, root=item), 40)
        text = b.text_of(b.find("comment_text", timeout=300, root=item), 200)
        if not text:
            text = b.text_of(item, 120)  # 定位失效时退回整条读取
        if not text:
            return None
        likes = b.text_of(b.find("comment_like", timeout=300, root=item), 12)
        cid = self._first_attr(item, "comment_id", "id").replace("tooltip_", "")
        if not cid:
            cid = "h" + hashlib.md5(f"{user}|{text}".encode("utf-8")).hexdigest()[:16]
        return {"cid": cid, "user": user, "text": text, "likes": likes}

    # ---------- 给别人的评论点赞 / 回复 ----------
    def _locate_comment(self, c: dict):
        """在打开的评论区里重新找到这条评论（优先按评论 id，没有 id 按内容）。"""
        item_sel = as_list(self.b.sel.get("comment_item"))[0]
        page = self.b.page
        if c["cid"].isdigit():
            loc = page.locator(self.cid_selector(c["cid"])).last
        else:
            loc = page.locator(item_sel, has_text=c["text"][:20]).first
        try:
            loc.wait_for(state="visible", timeout=4000)
            loc.scroll_into_view_if_needed(timeout=2000)
            return loc
        except Exception:  # noqa: BLE001
            return None

    def interact_comments(self, likes: list[dict], replies: list[tuple[dict, str]]) -> dict:
        """给 likes 里的评论点赞，对 replies 里的 (评论, 回复内容) 逐条回复。返回实际做成的。"""
        done = {"liked": [], "replied": []}
        if not likes and not replies:
            return done
        if not self.open_comments():
            return done
        try:
            for c in likes:
                item = self._locate_comment(c)
                btn = None if item is None else self.b.find("comment_like", timeout=1500, root=item)
                if btn is None:
                    self._dump_comments_once("comment_like_fail", f"找不到这条评论的点赞按钮（{c['text'][:12]}）")
                    continue
                self.b.pause(0.6, 1.4)
                if not self.b.safe_click(btn, "评论的点赞按钮"):
                    continue
                self.b.pause(0.8, 1.6)
                self.b.check_captcha()
                done["liked"].append(c)
            for c, text in replies:
                if self._reply_comment(c, text):
                    done["replied"].append((c, text))
        finally:
            self.close_comments()
        return done

    def _reply_comment(self, c: dict, text: str) -> bool:
        return self.reply_on_item(self._locate_comment(c), text, c["text"][:12], c.get("user") or "")

    def comment_rows(self) -> list[dict]:
        """评论区里每条评论的 id、正文（含表情文字）、是不是楼中楼、是不是她自己发的。"""
        try:
            return self.b.page.evaluate(COMMENT_ROWS_JS, getattr(self.b, "me_uid", "") or "") or []
        except Exception:  # noqa: BLE001
            return []

    def comment_text(self, item) -> str:
        """一条评论的正文：只取正文那一块，表情图换成它的文字（比如 [捂脸]），不混进名字、时间、点赞数。"""
        try:
            t = item.evaluate(r"""(row) => {
                const own = (sel) => [...row.querySelectorAll(sel)].find((el) => el.closest('[data-e2e="comment-item"]') === row);
                let box = own('[data-e2e="comment-content"], .comment-item-content');
                if (!box) {
                  const info = own('.comment-item-info-wrap');
                  const nx = info && info.nextElementSibling;
                  if (nx && !nx.matches('.comment-item-stats-container') && !nx.querySelector('.comment-item-stats-container, button')) box = nx;
                }
                if (!box) return null;
                let out = '';
                const walk = (n) => { if (n.nodeType === 3) { out += n.textContent; return; }
                  if (n.nodeType !== 1) return; if (n.tagName === 'IMG') { out += n.getAttribute('alt') || ''; return; }
                  for (const c of n.childNodes) walk(c); };
                walk(box); return out.replace(/\s+/g, ' ').trim();
            }""")
        except Exception:  # noqa: BLE001
            t = None
        return t if isinstance(t, str) else ""

    def comment_author(self, item) -> str:
        """这条评论自己的作者名（不含楼中楼里别人的名字）。"""
        try:
            t = item.evaluate(r"""(row) => {
                for (const a of row.querySelectorAll('a[href*="/user/"]')) {
                  if (a.closest('[data-e2e="comment-item"]') !== row) continue;
                  const s = (a.textContent || '').trim(); if (s) return s;
                }
                return '';
            }""")
        except Exception:  # noqa: BLE001
            t = ""
        return t if isinstance(t, str) else ""

    @staticmethod
    def cid_selector(cid: str) -> str:
        """按评论 id 找评论：三种标法都认（data-comment-id、id=comment_数字、里面的 tooltip_数字）。"""
        base = as_list(Feed._item_sel)[0]
        return (f"{base}[data-comment-id='{cid}'], {base}#comment_{cid}, "
                f"{base}:has([id='tooltip_{cid}'])")

    _item_sel = "[data-e2e='comment-item']"

    def _reply_target_label(self, box) -> str:
        """回复框上写着回复谁（“回复 @某某”）：看提示文字、无障碍标签、回复对象提示条。"""
        try:
            return box.evaluate(r"""(el) => {
                const parts = [el.getAttribute('aria-label'), el.getAttribute('placeholder'),
                               el.getAttribute('data-placeholder')];
                let p = el;
                for (let i = 0; i < 6 && p; i++, p = p.parentElement) {
                  const t = p.querySelector('[data-e2e="comment-reply-target"], .PPsJmqBy, [class*="placeholder"]');
                  if (t) { parts.push(t.textContent); break; }
                }
                return parts.filter(Boolean).join(' ');
            }""") or ""
        except Exception:  # noqa: BLE001
            return ""

    def _box_text(self, box) -> str:
        try:
            return box.inner_text(timeout=800) or ""
        except Exception:  # noqa: BLE001
            return ""

    def reply_on_item(self, item, text: str, what: str = "", who: str = "") -> SendResult:
        """在评论区的某一条评论上点“回复”，在弹出的回复框里输入并发送，然后核实到底发没发出去。

        - 发之前：确认回复框写的是“回复 @who”，不是别人（认不出提示就照常发）；
        - 发之后：先记下原来已有的评论 id，发完反复查，出现一条新的、内容一样、是她自己发的评论，
          而且连着两次都查到、输入框也清空了，才算发出去；查到好几条或者查不清，算“不确定”，不自动重发。"""
        c = {"text": what}
        baseline = {r["id"] for r in self.comment_rows() if r.get("id")}
        btn = None if item is None else self.b.find("comment_reply_button", timeout=1500, root=item)
        if btn is None:
            self._dump_comments_once("comment_reply_fail", f"找不到这条评论的“回复”按钮（{c['text'][:12]}）")
            return False
        if not self.b.safe_click(btn, "“回复”"):
            return False
        self.b.pause(0.8, 1.5)
        self.b.check_captcha()
        # 回复框通常出现在这条评论下面；也可能是底部输入框切到了“回复 @某人”并获得焦点
        box = self.b.find("comment_reply_input", timeout=2000, root=item.locator("xpath=.."))
        if box is None:
            box = self.b.find_css("[contenteditable='true']:focus", timeout=2000)
        if box is None:
            self._dump_comments_once("comment_reply_box_fail", "点了“回复”，但没找到回复输入框")
            return False
        if not self.b.safe_click(box, "回复输入框"):
            return FAILED
        label = self._reply_target_label(box)
        if who and label and ("回复" in label or "@" in label) and _sq(who) not in _sq(label):
            log.warning("回复框写的是“%s”，不是要回复的 %s，不发", label[:40], who)
            self._dump_comments_once("comment_reply_wrong_target", f"回复框对象不对（{label[:20]}）")
            return FAILED
        self.b.pause(0.4, 0.9)
        self.b.type_slowly(text)
        self.b.pause(0.6, 1.2)
        self.b.page.keyboard.press("Enter")
        self.b.pause(1.2, 2.0)
        self.b.check_captcha()
        return self._confirm_sent(baseline, text, box)

    def _confirm_sent(self, baseline: set, text: str, box) -> SendResult:
        want = _sq(text)
        last_hit = None
        for _ in range(12):
            rows = self.comment_rows()
            hits = [r for r in rows if r.get("id") and r["id"] not in baseline and r.get("self") is not False
                    and _sq(r.get("text", "")) and (_sq(r["text"]) == want or want.startswith(_sq(r["text"]))
                                                   or _sq(r["text"]).endswith(want))]
            ids = {r["id"] for r in hits}
            if len(ids) > 1:
                log.warning("发完查到 %d 条一样的新评论，没法确认是哪条", len(ids))
                return UNCERTAIN
            if len(ids) == 1:
                hid = next(iter(ids))
                if hid == last_hit and not _sq(self._box_text(box)).endswith(want[-6:] if want else "\0"):
                    return SENT
                last_hit = hid
            cancel.sleep(0.8)
        if want and want[-8:] in _sq(self._box_text(box)):
            log.warning("按了发送，但字还在输入框里，没发出去")
            return FAILED
        return SENT if last_hit else UNCERTAIN

    def _dump_comments_once(self, tag: str, why: str) -> None:
        if Feed._comments_dumped:
            return
        Feed._comments_dumped = True
        path = self.b.dump(tag)
        log.warning("%s，页面已保存到 %s（之后同样的情况不再重复保存）", why, path)

    _frames_warned = False

    def capture_frames(self, n: int, dwell_s: float, sleep=None) -> list[bytes]:
        """在停留时间内平均截 n 帧视频画面（JPEG，宽度缩到 640 以省 token）。

        sleep：等待用的函数。后台调度会传入可被“暂停”打断的等待，它返回 False 时立刻停止截图。
        """
        wait = sleep or cancel.sleep
        video = self.b.find("video_element", timeout=4000)
        frames: list[bytes] = []
        if video is None or n <= 0:
            if video is None and not Feed._frames_warned:
                Feed._frames_warned = True
                log.warning("没找到视频画面元素，这次不截图（页面已保存到 %s）", self.b.dump("video_not_found"))
            wait(dwell_s)
            return frames
        gap = max(1.0, dwell_s / n)
        for _ in range(n):
            if wait(gap) is False:
                break  # 被叫停了
            try:
                png = video.screenshot(timeout=4000)
                frames.append(_to_small_jpeg(png))
            except Exception as e:  # noqa: BLE001
                if not Feed._frames_warned:
                    Feed._frames_warned = True
                    log.warning("截取视频画面失败：%s", e)
        return frames

    def watch_through(self, n_frames: int, max_s: float, sleep=None, progress=None) -> tuple[list[bytes], float, float]:
        """把当前页面上的视频从头看到尾（最多看 max_s 秒），在整段视频里均匀截 n_frames 帧。

        视频比 max_s 长时，看完 max_s 秒后把后面没看到的部分快进着截图，保证画面覆盖整个视频。
        sleep：可被“暂停”打断的等待（返回 False 就停）；progress(已看秒数, 总秒数) 用来更新界面。
        返回 (画面, 实际看了多少秒, 视频总长)。
        """
        wait = sleep or (lambda s: cancel.sleep(s) or True)
        video = self.b.find("video_element", timeout=8000)
        if video is None:
            log.warning("没找到视频画面元素，没法看完整个视频（页面：%s）", self.b.page.url)
            return [], 0.0, 0.0
        try:
            video.evaluate("el => { el.muted = true; el.loop = false; el.currentTime = 0;"
                           " if (el.paused) el.play().catch(() => {}); }")
        except Exception:  # noqa: BLE001
            pass
        duration = 0.0
        for _ in range(10):  # 视频信息加载出来才知道多长
            try:
                duration = float(video.evaluate("el => isFinite(el.duration) ? el.duration : 0") or 0)
            except Exception:  # noqa: BLE001
                duration = 0.0
            if duration > 0:
                break
            if wait(0.5) is False:
                return [], 0.0, 0.0
        if duration <= 0:
            duration = 30.0  # 读不到时长（比如直播回放），按 30 秒处理
        points = [duration * (i + 0.5) / n_frames for i in range(n_frames)] if n_frames > 0 else []
        frames: list[tuple[float, bytes]] = []
        limit = min(duration, max_s)
        started = time.time()
        cur = 0.0
        while True:
            if wait(1.0) is False:
                break  # 被叫停了
            try:
                cur, ended = video.evaluate("el => [el.currentTime, el.ended]")
                cur = float(cur or 0)
            except Exception:  # noqa: BLE001
                cur, ended = time.time() - started, False
            while points and points[0] <= cur:
                frames.append((points.pop(0), self._shot(video)))
            if progress:
                progress(cur, duration)
            if ended or cur >= limit - 0.3 or time.time() - started > limit + 15:
                break
        watched = min(cur, duration)
        for t in points:  # 没来得及看到的部分：快进过去截图，保证画面覆盖整段视频
            try:
                video.evaluate("(el, t) => { el.currentTime = t; }", t)
            except Exception:  # noqa: BLE001
                break
            if wait(1.2) is False:
                break
            frames.append((t, self._shot(video)))
        try:
            video.evaluate("el => el.pause()")
        except Exception:  # noqa: BLE001
            pass
        return [f for _, f in sorted(frames, key=lambda x: x[0]) if f], watched, duration

    @staticmethod
    def _shot(video) -> bytes:
        try:
            return _to_small_jpeg(video.screenshot(timeout=5000))
        except Exception as e:  # noqa: BLE001
            log.warning("截取视频画面失败：%s", str(e).splitlines()[0][:120])
            return b""

    def like(self) -> bool:
        btn = self.b.find("like_button", timeout=2500)
        try:
            if btn is None or not self.b.safe_click(btn, "点赞按钮"):
                self.b.page.keyboard.press(self.b.sel.get("like_key", "z"))
            self.b.pause(0.8, 1.5)
            self.b.check_captcha()
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("点赞失败：%s", e)
            return False

    def _collect_toast(self) -> bool:
        for w in _collect_toast_words():
            try:
                if self.b.page.get_by_text(w, exact=False).first.is_visible(timeout=150):
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def collect(self) -> bool:
        """收藏当前视频。已经收藏过的不再点（再点一下会变成取消收藏）。确实收藏上了返回 True。

        按钮上的 data-e2e-state 会告诉我们状态：没收藏时是 video-player-no-collect。"""
        btn = self.b.find("collect_button", timeout=2500)
        if btn is None:
            log.info("没找到收藏按钮")
            return False
        state = _collect_state(btn)
        if state is True:
            log.info("这个视频之前已经收藏过了，不再点")
            return False
        if not self.b.safe_click(btn, "收藏按钮"):
            return False
        self.b.check_captcha()
        # 按钮状态有时要过一两秒才变；网页也会弹“收藏成功”之类的小提示。两样有一样就算收藏上了
        after = None
        for _ in range(8):
            self.b.pause(0.4, 0.6)
            after = _collect_state(btn)
            if after is True or self._collect_toast():
                after = True
                break
        if after is not True:
            log.warning("点了收藏，但按钮状态没变、也没看到“收藏成功”的提示（可能没收藏上）")
            return False
        self.b.dismiss_tips()  # 收藏后偶尔会弹“添加到收藏夹”之类的小提示，关掉免得挡住视频
        return True

    last_mention_ok = False  # 上一次评论里的 @ 有没有成功（选中了候选列表里的好友）

    def comment(self, text: str, mention: str = "") -> bool:
        """发一条评论。mention 是要 @ 的好友昵称：先输入 @ 和名字，在弹出的候选里点选（这样对方才会收到提醒）。

        @ 没成功（候选里找不到这个人）就去掉 @，只发评论内容；结果记在 last_mention_ok。"""
        self.last_mention_ok = False
        if not self.open_comments():
            return False
        box = self.b.find("comment_input", timeout=4000)
        if box is None or not self.b.safe_click(box, "评论输入框"):
            self.close_comments()
            return False
        self.b.pause(0.5, 1.0)
        if mention:
            self.last_mention_ok = self._insert_mention(mention)
        self.b.type_slowly(text)
        self.b.pause(0.6, 1.2)
        self.b.page.keyboard.press("Enter")
        self.b.pause(1.5, 2.5)
        self.b.check_captcha()
        self.close_comments()
        return True

    def _insert_mention(self, name: str) -> bool:
        page = self.b.page
        key = name.strip()[:12]
        page.keyboard.type("@")
        self.b.pause(0.8, 1.3)
        self.b.type_slowly(key)
        self.b.pause(1.2, 2.0)
        cand = None
        for s in as_list(self.b.sel.get("mention_item")):
            try:
                loc = page.locator(s).filter(has_text=key).first
                loc.wait_for(state="visible", timeout=1500)
                cand = loc
                break
            except Exception:  # noqa: BLE001
                continue
        if cand is not None and self.b.safe_click(cand, f"候选里的 {name}"):
            self.b.pause(0.5, 1.0)
            return True
        # 候选里没有这个人：删掉刚才输入的 @ 和名字，只发评论内容
        for _ in range(len(key) + 1):
            page.keyboard.press("Backspace")
            cancel.sleep(0.04)
        self._dump_comments_once("mention_fail", f"输入 @{key} 后没在候选列表里找到这个人")
        return False

    def next(self) -> None:
        self.b.page.keyboard.press(self.b.sel.get("next_video_key", "ArrowDown"))
        self.b.pause(1.5, 3.0)
        self.b.check_captcha()


def _to_small_jpeg(png: bytes, width: int = 640) -> bytes:
    try:
        from PIL import Image
    except ImportError:
        return png
    img = Image.open(io.BytesIO(png)).convert("RGB")
    if img.width > width:
        img = img.resize((width, int(img.height * width / img.width)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def thumb_data_url(jpeg: bytes, width: int = 220) -> str:
    """把截到的画面缩成小图，给仪表盘“她正在看”显示用。"""
    import base64

    try:
        from PIL import Image

        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        if img.width > width:
            img = img.resize((width, int(img.height * width / img.width)))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=72)
        jpeg = buf.getvalue()
    except Exception:  # noqa: BLE001 —— 缩图失败就用原图
        pass
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")


def _collect_toast_words():
    return ("收藏成功", "已收藏", "已添加到收藏", "已加入收藏")


def _collect_state(btn):
    """收藏按钮的状态：True 已收藏，False 没收藏，None 认不出来。"""
    try:
        st = (btn.get_attribute("data-e2e-state", timeout=1000) or "").lower()
    except Exception:  # noqa: BLE001
        return None
    if not st:
        return None
    if "no-collect" in st or "not-collect" in st or "uncollect" in st:
        return False
    return "collect" in st
