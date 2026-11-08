"""用 Playwright 控制电脑自带的 Edge（找不到就用 Chrome）打开抖音网页版。

说明：
- 浏览器以普通有界面的方式运行，登录状态保存在 data/browser_profile，不用每次扫码。
- 不做任何绕过平台风控的处理：检测到验证码就暂停，交给你手动完成。
- 必须在同一个线程里使用（Playwright 同步接口的要求），由 agent/runner.py 的后台线程独占。
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from contextlib import contextmanager
from datetime import datetime

from .. import APP_NAME, cancel, crypto, winfocus
from ..cancel import OperationCancelled  # noqa: F401 —— 方便别处 from browser import
from ..paths import DEBUG_DIR, PROFILE_DIR, SESSION_FILE
from .selectors import as_list, load_selectors

log = logging.getLogger(__name__)


class BrowserError(Exception):
    pass


def why(e: Exception) -> str:
    """Playwright 报错的第一行只有“Timeout 6000ms exceeded”，真正原因在后面的过程记录里
    （比如“<div class=...> intercepts pointer events”被别的东西挡住、“element is not stable”还在动）。
    挑出最有用的那一行一起记下来。"""
    lines = [ln.strip(" -") for ln in str(e).splitlines() if ln.strip()]
    if not lines:
        return str(e)[:200]
    keys = ("intercepts pointer events", "not stable", "not visible", "outside of the viewport", "not enabled",
            "detached", "obscured")
    detail = next((ln for ln in reversed(lines[1:]) if any(k in ln for k in keys)), "")
    return (lines[0][:150] + (f"（{detail[:200]}）" if detail else ""))


class CaptchaDetected(BaseException):
    """页面弹出了验证码。

    故意不继承 Exception：代码里各处“某一步失败就跳过”的 except Exception 不会把它吞掉，
    它会一路传到后台调度那里，暂停并提醒你手动完成验证（软件不会尝试自动过验证码）。
    """


# 平时把浏览器窗口放在所有显示器外面：照常播放、截图，但你看不到它，也不会挡住你用电脑。
# 位置按这台电脑的显示器摆放和缩放比例现算（多显示器、副屏在左边 / 上面、125% / 150% 缩放都适用）
BROWSER_W, BROWSER_H = 1280, 860
# 页面至少要有这么大，抖音网页才是“推荐页 + 右侧评论区”的标准布局（小屏幕笔记本上窗口会被系统缩小）
MIN_LAYOUT = (1200, 720)
LAYOUT_SIZE = (1280, 820)
ONSCREEN = {"left": 80, "top": 40, "width": 1280, "height": 860}


def offscreen_bounds() -> dict:
    """藏起来时的窗口位置（浏览器用的是“缩放后”的坐标）：放在所有显示器拼起来的范围的左边外侧。"""
    left, top, _r, _b = winfocus.virtual_screen()
    s = winfocus.system_scale()
    return {"left": int(left / s) - BROWSER_W - 1000, "top": int(top / s), "width": BROWSER_W, "height": BROWSER_H}


def onscreen_bounds(near_title: str = "") -> dict:
    """拿到屏幕上时的位置：软件主窗口所在的那块显示器，按可用区域和缩放比例算，不会超出屏幕。"""
    area = winfocus.work_area(near_title)
    if not area:
        return dict(ONSCREEN)
    s = winfocus.system_scale()
    left, top, right, bottom = (v / s for v in area)
    w, h = right - left, bottom - top
    width = int(max(min(1000, w * 0.96), w * 0.88))
    height = int(max(min(700, h * 0.95), h * 0.9))
    return {"left": int(left + (w - width) / 2), "top": int(top + (h - height) / 2), "width": width, "height": height}


def _browser_exes() -> list[str]:
    """Edge / Chrome 没装在默认位置时，从注册表和常见目录里找可执行文件。"""
    import os

    found: list[str] = []
    try:
        import winreg  # type: ignore

        for exe in ("msedge.exe", "chrome.exe"):
            for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(root, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                        v = winreg.QueryValue(k, None)
                        if v and v not in found:
                            found.append(v)
                except OSError:
                    continue
    except ImportError:
        pass
    for base in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"), os.environ.get("LOCALAPPDATA")):
        if not base:
            continue
        for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
            p = os.path.join(base, rel)
            if os.path.isfile(p) and p not in found:
                found.append(p)
    return [p.strip('"') for p in found if os.path.isfile(p.strip('"'))]


class DouyinBrowser:
    def __init__(self):
        self.hidden = True
        self._hwnd = 0           # 浏览器窗口的 Windows 句柄（拿到最前面时用）
        self.sel = load_selectors()
        self._pw = None
        self.ctx = None
        self._page = None
        self.users_seen: dict[str, dict] = {}  # 昵称 -> 抖音网页自己加载的用户资料（私信对象的抖音号、主页 id、头像）
        # 打开“互动消息”时网页自己加载的通知内容：评论 id、所在视频、是不是楼中楼（回复在别人评论下面）
        self.notice_comments: list[dict] = []
        self.following: dict[str, dict] = {}   # 打开自己的“关注”列表时读到的人：主页 id -> 资料（含是否互关）
        self.me_uid = ""                         # 她自己账号的主页 id（读互关好友时排除自己）
        self._emu: dict = {}                     # 被系统缩小窗口时，给页面固定大小用的调试会话（标签页 -> 会话）
        self._hide_checked = 0.0

    @property
    def page(self):
        """当前操作的标签页。它被关掉了（用户手动关、浏览器自己换了标签页）就换一个还开着的，没有就新开一个。"""
        p = getattr(self, "_page", None)
        if p is None or getattr(self, "ctx", None) is None:
            return p
        try:
            closed = p.is_closed()
        except Exception:  # noqa: BLE001
            closed = True
        if not closed:
            return p
        try:
            alive = [x for x in self.ctx.pages if not x.is_closed()]
            self._page = alive[-1] if alive else self.ctx.new_page()
            self._page.set_default_timeout(15000)
            log.info("原来的标签页被关掉了，换到另一个标签页继续")
        except Exception:  # noqa: BLE001 —— 整个浏览器都关了，交给上层重启浏览器
            self._page = None
        return self._page

    @page.setter
    def page(self, value) -> None:
        self._page = value

    # ---------- 生命周期 ----------
    @property
    def running(self) -> bool:
        return self.page is not None

    def start(self) -> None:
        if self.running:
            return
        from playwright.sync_api import sync_playwright  # 延迟导入，方便没装 Playwright 时也能跑测试

        prev_focus = winfocus.foreground()
        self._pw = sync_playwright().start()
        last_err = None
        off = offscreen_bounds()
        args = ["--mute-audio", "--hide-crash-restore-bubble", "--no-first-run",
                "--no-default-browser-check",
                f"--window-position={off['left']},{off['top']}",
                f"--window-size={off['width']},{off['height']}",
                "--disable-features=CalculateNativeWinOcclusion",
                "--disable-backgrounding-occluded-windows",
                # 发作品时在新标签页里干活，原来的刷视频页面会退到后台：后台页面也不降速、不冻结
                "--disable-renderer-backgrounding",
                "--disable-background-timer-throttling"]
        # 先按名字找 Edge / Chrome；没装在默认位置的，再去注册表和常见目录里找
        tries = [{"channel": "msedge"}, {"channel": "chrome"}] + [{"executable_path": p} for p in _browser_exes()]
        for opt in tries:
            try:
                self.ctx = self._pw.chromium.launch_persistent_context(
                    str(PROFILE_DIR),
                    headless=False,
                    no_viewport=True,  # 页面大小跟着窗口走：窗口多大就显示多大，能正常滚动看全
                    locale="zh-CN",
                    # 普通的启动选项（不做任何伪装）：静音、不弹“恢复页面”气泡和默认浏览器提示；
                    # 窗口直接开在屏幕外面，并且窗口被挡住 / 在屏幕外时照常播放视频
                    args=args,
                    **opt,
                )
                log.info("已启动浏览器：%s", opt.get("channel") or opt.get("executable_path"))
                break
            except Exception as e:  # noqa: BLE001 —— 找不到这个浏览器就换下一个
                last_err = e
                self.ctx = None
        if self.ctx is None:
            self._pw.stop()
            self._pw = None
            raise BrowserError("没能启动 Edge 或 Chrome（这台电脑上需要装有其中一个）："
                               f"{str(last_err).splitlines()[0][:200] if last_err else ''}")
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.set_default_timeout(15000)
        try:
            self.ctx.on("response", self._on_response)
            self.ctx.add_init_script(DM_WATCH_JS)
        except Exception:  # noqa: BLE001
            pass
        self.hide_window()
        winfocus.give_back(prev_focus, APP_NAME)  # 浏览器刚启动时会抢焦点：还给你原来在用的窗口
        if not self.is_logged_in():
            self.restore_session()

    def stop(self) -> None:
        """正常关闭浏览器：先备份登录状态，再让浏览器把 cookie 写回磁盘（直接结束进程会丢登录）。"""
        try:
            if self.ctx:
                self.save_session()
                self.ctx.close()
        except Exception as e:  # noqa: BLE001
            log.warning("关闭浏览器时出错：%s", str(e).splitlines()[0][:120])
        finally:
            try:
                if self._pw:
                    self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = self.ctx = self.page = None
            self._emu = {}

    # ---------- 窗口：平时藏在屏幕外，扫码 / 验证时才拿出来 ----------
    def _set_window(self, bounds: dict) -> bool:
        try:
            cdp = self.ctx.new_cdp_session(self.page)
            wid = cdp.send("Browser.getWindowForTarget")["windowId"]
            cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
            cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": dict(bounds)})
            cdp.detach()
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("调整浏览器窗口失败：%s", str(e).splitlines()[0][:120])
            return False

    def _activate_tab(self, page) -> None:
        """切回某个标签页（藏着的时候切完把焦点还回去，不打扰你）。"""
        with self.keep_focus():
            try:
                page.bring_to_front()
            except Exception:  # noqa: BLE001
                pass

    def hide_window(self) -> None:
        """把浏览器窗口挪到所有显示器外面（照常运行），并把焦点还给你原来的窗口。"""
        prev = winfocus.foreground()
        if self.ctx and self._set_window(offscreen_bounds()):
            self.hidden = True
            self._ensure_layout()
        winfocus.give_back(prev, APP_NAME)

    def ensure_hidden(self) -> None:
        """藏着的浏览器被系统挪回了屏幕上（插拔显示器、改分辨率 / 缩放后 Windows 会这么做）：再藏回去。

        顺便检查页面大小够不够标准布局。半分钟最多查一次。"""
        if not self.ctx or not self.hidden or time.time() - self._hide_checked < 30:
            return
        self._hide_checked = time.time()
        b = self._window_bounds()
        if b:
            s = winfocus.system_scale()
            rect = (b["left"] * s, b["top"] * s, (b["left"] + b["width"]) * s, (b["top"] + b["height"]) * s)
            if winfocus.rect_on_any_monitor(rect):
                log.info("藏起来的浏览器窗口跑回了屏幕上（可能是显示器设置变了），重新藏好")
                self.hide_window()
                return
        self._ensure_layout()

    def _window_bounds(self) -> dict | None:
        try:
            cdp = self.ctx.new_cdp_session(self.page)
            try:
                wid = cdp.send("Browser.getWindowForTarget")["windowId"]
                return cdp.send("Browser.getWindowBounds", {"windowId": wid}).get("bounds")
            finally:
                cdp.detach()
        except Exception:  # noqa: BLE001
            return None

    # ---------- 页面大小：保证每台电脑上都是同一种网页布局 ----------
    def _layout_size(self, page=None):
        try:
            w, h = (page or self.page).evaluate("() => [window.innerWidth, window.innerHeight]")
            return int(w), int(h)
        except Exception:  # noqa: BLE001
            return None

    def _ensure_layout(self, page=None) -> None:
        """藏着时，如果系统把窗口缩得比标准布局小（小屏幕笔记本、缩放比例大时会这样），
        就让页面按固定的 1280×820 排版。元素位置都由浏览器自己算，换哪台电脑都一样。"""
        page = page or self.page
        if not self.ctx or not self.hidden or page is None or page in self._emu:
            return
        size = self._layout_size(page)
        if not size or (size[0] >= MIN_LAYOUT[0] and size[1] >= MIN_LAYOUT[1]):
            return
        try:
            cdp = self.ctx.new_cdp_session(page)
            cdp.send("Emulation.setDeviceMetricsOverride", {"width": LAYOUT_SIZE[0], "height": LAYOUT_SIZE[1],
                                                             "deviceScaleFactor": 0, "mobile": False})
            self._emu[page] = cdp
            log.info("浏览器窗口被系统缩小到 %dx%d，页面改按固定的 %dx%d 排版", size[0], size[1], *LAYOUT_SIZE)
        except Exception as e:  # noqa: BLE001
            log.warning("固定页面大小失败：%s", str(e).splitlines()[0][:120])

    def _clear_layout(self) -> None:
        """拿到屏幕上给你看时，页面跟着真实窗口大小走（不然可能有一部分看不到）。"""
        for cdp in list(self._emu.values()):
            try:
                cdp.send("Emulation.clearDeviceMetricsOverride")
                cdp.detach()
            except Exception:  # noqa: BLE001
                pass
        self._emu.clear()

    def show_window(self) -> None:
        """把浏览器窗口拿到屏幕上（扫码登录、手动完成验证、你想看看她在干嘛的时候），
        放在软件主窗口所在的那块显示器上，大小按那块屏幕和缩放比例来，不会超出屏幕；
        并且真正放到所有窗口的最前面（不然会被软件主窗口挡住，看着像还藏着）。"""
        bounds = onscreen_bounds(APP_NAME)
        log.info("把浏览器拿到屏幕上：位置 %s，系统缩放 %.2f，所有屏幕范围 %s", bounds, winfocus.system_scale(),
                 winfocus.virtual_screen())
        if not self.ctx:
            log.warning("浏览器还没启动，没法拿到屏幕上")
            return
        if self._set_window(bounds):
            self.hidden = False
            self._clear_layout()
            try:
                self.page.bring_to_front()
            except Exception:  # noqa: BLE001
                pass
            hwnd = self._os_window()
            ok = bool(hwnd) and winfocus.bring_to_front(hwnd)
            fg = winfocus.foreground()
            log.info("浏览器窗口句柄 %s，放到最前面 %s；现在最前面的窗口 %s（%s），浏览器窗口实际位置 %s",
                     hwnd, "成功" if ok else "失败", fg, "是浏览器" if fg and fg == hwnd else "不是浏览器",
                     winfocus.window_rect(hwnd) if hwnd else None)
        else:
            log.warning("把浏览器拿到屏幕上失败（调整窗口位置出错，见上一条）")

    def _os_window(self) -> int:
        """浏览器窗口在 Windows 里的句柄：临时把网页标题改成一个暗号，按标题找到窗口后再改回去。找到后记住。"""
        known = getattr(self, "_hwnd", 0)
        if known and winfocus.is_window(known):
            return known
        if not self.page:
            return 0
        token = f"aiw-{random.randrange(16 ** 8):08x}"
        try:
            old = self.page.evaluate("t => { const o = document.title; document.title = t; return o; }", token)
        except Exception:  # noqa: BLE001
            return 0
        hwnd = 0
        try:
            for _ in range(15):
                hwnd = winfocus.find_window_containing(token)
                if hwnd:
                    break
                time.sleep(0.1)
        finally:
            try:
                self.page.evaluate("t => { document.title = t; }", old)
            except Exception:  # noqa: BLE001
                pass
        self._hwnd = hwnd
        return hwnd

    @contextmanager
    def keep_focus(self):
        """包住一段浏览器操作：如果操作过程中浏览器（藏在屏幕外）抢走了键盘焦点，结束时还回去。"""
        prev = winfocus.foreground()
        try:
            yield
        finally:
            if self.hidden:
                winfocus.give_back(prev, APP_NAME)

    # ---------- 新私信提醒：页面里的脚本一直盯着右上角“消息”的未读数 ----------
    _dm_seen_seq = 0

    def dm_signal(self) -> bool:
        """自上次查看以来，“消息”的未读数有没有增加（不打开私信面板，也不刷新页面）。"""
        try:
            seq = int(self.page.evaluate("() => (window.__aiwDm && window.__aiwDm.seq) || 0") or 0)
        except Exception:  # noqa: BLE001
            return False
        if seq > self._dm_seen_seq:
            self._dm_seen_seq = seq
            return True
        return False

    # ---------- 记下抖音网页自己加载的用户资料 ----------
    def _on_response(self, resp) -> None:
        """抖音网页在打开私信、主页时会加载用户资料（JSON）。这里只是顺手读一下这些返回内容，
        记下“昵称 -> 抖音号 / 主页 id / 头像”，用来准确找到私信对象的主页。不发任何额外请求。"""
        try:
            url = resp.url
            if "douyin.com" not in url or not any(k in url for k in ("/im/", "/user/", "/aweme/v1/web/")):
                return
            if "json" not in (resp.headers.get("content-type") or ""):
                return
            data = resp.json()
        except Exception:  # noqa: BLE001
            return
        if "/user/profile/self" in url:
            try:
                self.me_uid = str((data.get("user") or {}).get("sec_uid") or self.me_uid)
            except AttributeError:
                pass
        if "/following/list" in url or "/follower/list" in url:
            self._collect_following(data)
        if "/notice" in url:
            self._collect_notices(data)
        for u in _iter_users(data):
            name = (u.get("nickname") or "").strip()
            if not name or not u.get("sec_uid"):
                continue
            info = {
                "nickname": name,
                "sec_uid": u["sec_uid"],
                "douyin_id": str(u.get("unique_id") or u.get("short_id") or "").strip(),
                "avatar": _first_url(u.get("avatar_thumb") or u.get("avatar_medium") or u.get("avatar_larger")),
                "from_im": "/im/" in url,
            }
            old = self.users_seen.get(name)
            if old and old.get("from_im") and not info["from_im"]:
                continue  # 私信接口里来的更可信，不被同名的其他人覆盖
            self.users_seen[name] = info

    def _collect_notices(self, data) -> None:
        """互动消息的返回内容里，每条通知带着那条评论：评论 id、内容、作者、所在视频 id，
        以及 reply_id（不是 0 就说明这条评论是回复在别人评论下面的“楼中楼”，reply_id 是那条楼主评论）。"""
        found: list[dict] = []

        def aweme_of(o: dict) -> str:
            for k in ("aweme_id", "item_id", "group_id"):
                v = o.get(k)
                if v and str(v).isdigit() and len(str(v)) >= 15:
                    return str(v)
            for k in ("aweme", "aweme_info", "item"):
                v = o.get(k)
                if isinstance(v, dict) and v.get("aweme_id"):
                    return str(v["aweme_id"])
            return ""

        def walk(o, aweme: str = "", depth: int = 0) -> None:
            if depth > 12:
                return
            if isinstance(o, dict):
                aw = aweme_of(o) or aweme
                if "cid" in o and "text" in o and str(o.get("cid") or "").isdigit():
                    u = o.get("user") or {}
                    found.append({"cid": str(o["cid"]), "text": str(o.get("text") or ""),
                                  "reply_id": str(o.get("reply_id") or "0"),
                                  "reply_to_reply_id": str(o.get("reply_to_reply_id") or "0"),
                                  "aweme_id": str(o.get("aweme_id") or aw or ""),
                                  "user": str(u.get("nickname") or "") if isinstance(u, dict) else ""})
                for v in o.values():
                    walk(v, aw, depth + 1)
            elif isinstance(o, list):
                for v in o:
                    walk(v, aweme, depth + 1)

        walk(data)
        if found:
            seen = {c["cid"] for c in found}
            self.notice_comments = (found + [c for c in self.notice_comments if c["cid"] not in seen])[:400]

    def notice_comment(self, text: str, user: str = "") -> dict | None:
        """按评论内容（和作者昵称）找到互动消息里的那条评论数据。"""
        def sq(t: str) -> str:
            return re.sub(r"\s+", "", t or "")

        t = sq(text)
        if not t:
            return None
        best = None
        for c in self.notice_comments:
            ct = sq(c["text"])
            if not ct or not (ct == t or (len(t) >= 6 and (t in ct or ct in t))):
                continue
            if user and c.get("user") and sq(c["user"]) != sq(user):
                continue
            if best is None or (ct == t and sq(best["text"]) != t):
                best = c
        return best

    def _collect_following(self, data) -> None:
        """“关注”列表的返回内容：follow_status=2 或 follower_status=1 表示互相关注。"""
        for u in _iter_users(data):
            if not u.get("sec_uid") or not (u.get("nickname") or "").strip():
                continue
            fs, fr = u.get("follow_status"), u.get("follower_status")
            mutual = None
            if fs is not None or fr is not None:
                mutual = str(fs) == "2" or str(fr) == "1"
            self.following[u["sec_uid"]] = {
                "uid": u["sec_uid"], "nickname": u["nickname"].strip(),
                "douyin_id": str(u.get("unique_id") or u.get("short_id") or "").strip(),
                "avatar": _first_url(u.get("avatar_thumb") or u.get("avatar_medium") or u.get("avatar_larger")),
                "mutual": mutual,
            }

    def known_user(self, name: str) -> dict | None:
        return self.users_seen.get((name or "").strip())

    # ---------- 登录状态备份：登录一次，下次直接用 ----------
    def save_session(self) -> bool:
        """把抖音的 cookie 加密备份到 data/douyin_session.bin（Windows 账户加密，换电脑 / 换用户解不开）。"""
        if not self.ctx or not self.is_logged_in():
            return False
        try:
            cookies = [c for c in self.ctx.cookies() if "douyin" in c.get("domain", "") or
                       "bytedance" in c.get("domain", "") or "amemv" in c.get("domain", "")]
            SESSION_FILE.write_text(crypto.encrypt(json.dumps(cookies)), encoding="utf-8")
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("备份登录状态失败：%s", str(e).splitlines()[0][:120])
            return False

    def restore_session(self) -> bool:
        """浏览器里的登录丢了（比如上次被强制结束），用备份恢复，免得又要扫码、验证手机。"""
        if not self.ctx or not SESSION_FILE.exists():
            return False
        try:
            cookies = json.loads(crypto.decrypt(SESSION_FILE.read_text(encoding="utf-8")))
            now = time.time()
            cookies = [c for c in cookies if c.get("expires", -1) in (-1, None) or c["expires"] > now]
            if cookies:
                self.ctx.add_cookies(cookies)
            ok = self.is_logged_in()
            log.info("用备份恢复登录状态：%s", "成功" if ok else "备份已失效")
            return ok
        except Exception as e:  # noqa: BLE001
            log.warning("恢复登录状态失败：%s", str(e).splitlines()[0][:120])
            return False

    def logout(self) -> None:
        """退出抖音登录：清掉登录相关的 cookie 和备份。设备相关的 cookie 保留，下次登录不会被当成陌生设备。"""
        if SESSION_FILE.exists():
            try:
                SESSION_FILE.unlink()
            except OSError:
                pass
        if not self.ctx:
            return
        try:
            words = [w.lower() for w in as_list(self.sel.get("login_cookie_words"))]
            keep = [c for c in self.ctx.cookies() if not any(w in c["name"].lower() for w in words)]
            self.ctx.clear_cookies()
            if keep:
                self.ctx.add_cookies(keep)
            self.goto(self.sel["login_url"])
        except Exception as e:  # noqa: BLE001
            log.warning("退出登录时出错：%s", str(e).splitlines()[0][:120])

    def alive(self) -> bool:
        if not self.page:
            return False
        try:
            self.page.title()
            return True
        except Exception:  # noqa: BLE001 —— 用户手动关掉了浏览器窗口
            return False

    # ---------- 登录 ----------
    def is_logged_in(self) -> bool:
        if not self.ctx:
            return False
        names = set(self.sel.get("login_cookie_names") or [])
        try:
            for c in self.ctx.cookies("https://www.douyin.com"):
                if c.get("name") in names and c.get("value"):
                    return True
        except Exception:  # noqa: BLE001
            return False
        return False

    def open_login(self) -> None:
        self.show_window()  # 扫码需要你看得到浏览器
        self.goto(self.sel["login_url"])

    def wait_login(self, timeout_s: int = 300, should_stop=lambda: False) -> bool:
        end = time.time() + timeout_s
        while time.time() < end and not should_stop():
            if self.is_logged_in():
                return True
            time.sleep(2)
        return self.is_logged_in()

    @contextmanager
    def in_new_tab(self, url: str):
        """在新标签页里打开 url 做事，做完关掉回到原来的页面（不打断刷视频和私信面板）。"""
        if not self.ctx:
            raise BrowserError("浏览器还没启动")
        old = self.page
        tab = self.ctx.new_page()
        tab.set_default_timeout(15000)
        self.page = tab
        if self._emu:  # 原来的页面是固定大小排版的：新标签页也一样
            self._ensure_layout(tab)
        try:
            with self.keep_focus():
                try:
                    tab.bring_to_front()  # 后台标签页会被浏览器降速，切到前面加载得快
                except Exception:  # noqa: BLE001
                    pass
                self.goto(url)
            yield tab
        finally:
            self.page = old
            cdp = self._emu.pop(tab, None)
            if cdp is not None:
                try:
                    cdp.detach()
                except Exception:  # noqa: BLE001
                    pass
            try:
                tab.close()
            except Exception:  # noqa: BLE001
                pass
            self._activate_tab(old)

    # ---------- 通用工具 ----------
    def goto(self, url: str) -> None:
        """打开网址。抖音网页很重、走代理时更慢，这里不死等整页加载完，超时也继续往下走。"""
        cancel.checkpoint()
        try:
            self.page.goto(url, wait_until="commit", timeout=45000)
        except Exception as e:  # noqa: BLE001
            if "Timeout" not in type(e).__name__:
                raise
            log.warning("打开 %s 超过 45 秒还没响应，先继续等待页面", url)
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:  # noqa: BLE001 —— 页面一直在加载也没关系，后面会按元素判断
            log.info("页面 %s 还在加载，继续", url)
        self.pause(2, 4)
        self.check_captcha()

    @staticmethod
    def pause(a: float = 0.6, b: float = 1.6) -> None:
        cancel.sleep(random.uniform(a, b))  # 点了“让她休息一下”会立刻醒来并中止

    @staticmethod
    def sleep(seconds: float) -> None:
        """可被“让她休息一下”打断的等待。"""
        cancel.sleep(seconds)

    LOADING_WORDS = ("加载中", "正在加载")

    def loading_visible(self) -> bool:
        """页面上是不是还转着“加载中”。"""
        for w in self.LOADING_WORDS:
            try:
                if self.page.get_by_text(w, exact=False).first.is_visible(timeout=200):
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def wait_any(self, key: str, timeout: float = 60, state: str = "visible", reload_once: bool = True):
        """等 selectors 里 key 的任意一个候选出现（每秒把所有候选都看一遍，不是一个个死等）。

        网络慢、页面一直“加载中”时：等到一半还没出来就刷新一次页面再等。找不到返回 None。"""
        cands = as_list(self.sel.get(key))
        end = time.time() + timeout
        reload_at = time.time() + timeout / 2 if reload_once else None
        while time.time() < end:
            cancel.checkpoint()
            for s in cands:
                try:
                    loc = self.page.locator(s).first
                    ok = loc.count() > 0 if state == "attached" else loc.is_visible()
                    if ok:
                        return loc
                except Exception:  # noqa: BLE001
                    continue
            if reload_at and time.time() >= reload_at:
                reload_at = None
                log.info("页面 %s 等了很久还没加载好（%s），刷新一次", self.page.url,
                         "一直在转“加载中”" if self.loading_visible() else "没找到要的元素")
                try:
                    self.page.reload(wait_until="commit", timeout=45000)
                    self.page.wait_for_load_state("domcontentloaded", timeout=30000)
                except Exception:  # noqa: BLE001
                    pass
                self.check_captcha()
                continue
            cancel.sleep(1)
        return None

    def find(self, key: str, timeout: int = 4000, root=None):
        """按 selectors.json 里的候选依次查找，返回第一个可见的元素；找不到返回 None。"""
        base = root or self.page
        for s in as_list(self.sel.get(key)):
            cancel.checkpoint()
            try:
                loc = base.locator(s).first
                loc.wait_for(state="visible", timeout=timeout)
                return loc
            except Exception:  # noqa: BLE001
                continue
        return None

    def dismiss_tips(self) -> bool:
        """关掉抖音盖在页面上的新手引导 / 提示层（例如“我知道了”）。关掉了返回 True。

        只处理普通的操作提示；验证码不在这里处理，遇到验证码会暂停交给你。
        """
        if not self.page:
            return False
        closed = False
        for s in as_list(self.sel.get("tip_close")):
            try:
                loc = self.page.locator(s).first
                if loc.is_visible():
                    loc.click(timeout=3000)
                    closed = True
                    log.info("关掉了页面上的提示层（%s）", s)
                    cancel.sleep(random.uniform(0.6, 1.2))
            except Exception:  # noqa: BLE001
                continue
        return closed

    def click(self, loc, timeout: int = 6000) -> None:
        """点击；被提示层挡住时先关掉提示层再点一次。还是不行就抛出原来的错误。"""
        cancel.checkpoint()
        try:
            loc.click(timeout=timeout)
            return
        except Exception:  # noqa: BLE001
            if not self.dismiss_tips():
                raise
        loc.click(timeout=timeout)

    def safe_click(self, loc, what: str = "") -> bool:
        """点击，失败时只记日志、返回 False，不让整个刷视频流程报错。"""
        try:
            self.click(loc)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("点击%s失败：%s", what, why(e))
            return False

    def click_open(self, loc, what: str, opened) -> bool:
        """点一个“打开面板”的入口（通知、私信），并确认面板真的打开了。

        普通点击有时会一直等到超时：按钮上盖着别的浮层，或者按钮还在做动画、一直不“稳定”。
        这种入口点开只是打开一个面板，点完都会确认面板开没开，所以可以放心换别的点法：
        普通点击 → 强制点击（不等按钮“可点”）→ 用网页脚本直接点。opened(timeout) 返回面板开没开。"""
        tries = (("普通点击", lambda: self.click(loc, timeout=4000)),
                 ("强制点击", lambda: loc.click(timeout=3000, force=True)),
                 ("脚本点击", lambda: loc.evaluate("el => el.click()")))
        reasons = []
        for i, (name, fn) in enumerate(tries):
            cancel.checkpoint()
            if i and opened(800):          # 上一种点法其实已经打开了（比如鼠标移上去就弹出来了）
                return True
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                reasons.append(f"{name}：{why(e)}")
            if opened(3000 if i == 0 else 2500):
                if i:
                    log.info("点%s：%s没成功，换成%s打开了（%s）", what, tries[0][0], name, "；".join(reasons)[:300])
                return True
        log.warning("点击%s失败，几种点法都没打开面板：%s", what, "；".join(reasons)[:400] or "点了但面板没出来")
        return False

    def find_css(self, css: str, timeout: int = 2000, root=None):
        """直接按一个 CSS 选择器找可见元素（不经过 selectors.json）。"""
        cancel.checkpoint()
        try:
            loc = (root or self.page).locator(css).first
            loc.wait_for(state="visible", timeout=timeout)
            return loc
        except Exception:  # noqa: BLE001
            return None

    def find_all(self, key: str, root=None) -> list:
        cancel.checkpoint()
        base = root or self.page
        for s in as_list(self.sel.get(key)):
            try:
                items = base.locator(s).all()
                if items:
                    return items
            except Exception:  # noqa: BLE001
                continue
        return []

    @staticmethod
    def text_of(loc, limit: int = 500) -> str:
        if loc is None:
            return ""
        try:
            return " ".join((loc.inner_text(timeout=2000) or "").split())[:limit]
        except Exception:  # noqa: BLE001
            return ""

    def type_slowly(self, text: str) -> None:
        for ch in text:
            cancel.checkpoint()
            self.page.keyboard.type(ch)
            cancel.sleep(random.uniform(0.05, 0.18))

    def check_captcha(self) -> None:
        cancel.checkpoint()
        if not self.page:
            return
        for s in as_list(self.sel.get("captcha")):
            try:
                if self.page.locator(s).first.is_visible(timeout=300):
                    raise CaptchaDetected(s)
            except CaptchaDetected:
                raise
            except Exception:  # noqa: BLE001
                continue

    def sms_visible(self) -> bool:
        """页面上是不是在要求填手机短信验证码。"""
        if not self.page:
            return False
        for sel in as_list(self.sel.get("sms_verify")):
            try:
                if self.page.locator(sel).first.is_visible(timeout=300):
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def verification_open(self) -> bool:
        """还有没有验证框开着（判断“验证完了没”用）。

        只认验证框本身：滑块 / 图形验证的容器和 iframe、短信验证码输入框和“获取 / 发送验证码”按钮；
        不认页面上随便哪里出现的“验证”“验证码”几个字（评论、文案、登录面板里都可能有，
        认了它们就会一直以为你还没验证完，浏览器藏不回去）。所有标签页都看。"""
        if not self.ctx:
            return False
        exact = ("text=请完成下列验证", "text=获取验证码", "text=发送验证码", "text=接收短信验证码")
        strong = [s for s in as_list(self.sel.get("captcha")) + as_list(self.sel.get("sms_verify"))
                  if not s.startswith("text=") or s in exact]
        try:
            pages = [pg for pg in self.ctx.pages if not pg.is_closed()]
        except Exception:  # noqa: BLE001
            pages = [self.page] if self.page else []
        for pg in pages:
            for sel in strong:
                try:
                    if pg.locator(sel).first.is_visible(timeout=300):
                        return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    def verify_kind(self) -> str:
        """现在页面上要你处理的验证：sms 手机验证码 / captcha 滑块或图形验证 / 空字符串 没有。"""
        if self.sms_visible():
            return "sms"
        return "captcha" if self.captcha_visible() else ""

    def captcha_visible(self) -> bool:
        try:
            self.check_captcha()
            return False
        except CaptchaDetected:
            return True

    def dump(self, tag: str) -> str:
        """保存当前页面截图和 HTML 到 data/debug，用来校准页面定位。"""
        if not self.page:
            return ""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = DEBUG_DIR / f"{stamp}_{tag}"
        try:
            self.page.screenshot(path=str(base.with_suffix(".png")), full_page=False)
            base.with_suffix(".html").write_text(self.page.content(), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            log.warning("保存调试信息失败：%s", e)
        return str(base)


# 注入到抖音页面里的小脚本：每秒、以及“消息”图标变化时，读一下未读数；变多了就记一笔（seq + 1）。
# 只读页面上已经显示的数字，不发任何请求。
DM_WATCH_JS = r"""
(() => {
  try {
    if (window.top !== window || !/douyin\.com$/.test(location.hostname)) return;
    if (window.__aiwDm) return;
    const state = window.__aiwDm = { seq: 0, last: 0 };
    const count = () => {
      const e = document.querySelector("[data-e2e='im-entry']");
      if (!e) return 0;
      const m = (e.innerText || "").match(/\d+/);
      if (m) return parseInt(m[0], 10);
      return e.querySelector("[class*='unread'],[class*='Unread'],[data-e2e='unread-count'],[class*='badge'],[class*='Badge']") ? 1 : 0;
    };
    const check = () => {
      const n = count();
      if (n > state.last && n > 0) state.seq += 1;
      state.last = n;
    };
    let observed = null;
    const hook = () => {
      const e = document.querySelector("[data-e2e='im-entry']");
      if (e && e !== observed) {
        observed = e;
        new MutationObserver(() => { clearTimeout(state.t); state.t = setTimeout(check, 200); })
          .observe(e, { subtree: true, childList: true, characterData: true, attributes: true });
      }
      check();
    };
    setInterval(hook, 1000);
  } catch (e) { /* 出错不影响页面 */ }
})();
"""


def _iter_users(obj, depth: int = 0):
    """在接口返回的 JSON 里找出所有“像用户资料”的对象（有昵称和主页 id）。"""
    if depth > 8:
        return
    if isinstance(obj, dict):
        if "nickname" in obj and "sec_uid" in obj:
            yield obj
        for v in obj.values():
            if isinstance(v, (dict, list)):
                yield from _iter_users(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:200]:
            yield from _iter_users(v, depth + 1)


def _first_url(img) -> str:
    try:
        lst = (img or {}).get("url_list") or []
        return lst[0] if lst else ""
    except AttributeError:
        return ""
