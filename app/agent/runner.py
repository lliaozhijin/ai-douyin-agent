"""后台运行调度：一个独立线程独占浏览器，按规则刷视频、检查 @ 和私信。

界面通过 send() 发命令，通过 status() 读状态；两边不共享浏览器对象。
"""
from __future__ import annotations

import re
import logging
import os
from contextlib import nullcontext
import queue
import random
import threading
import time
from datetime import datetime, timedelta

from .. import APP_NAME, cancel, winfocus
from ..cancel import OperationCancelled
from ..db import Database, now_str, today_str
from ..douyin.browser import BrowserError, CaptchaDetected, DouyinBrowser
from ..douyin.feed import Feed, FeedReadError, thumb_data_url
from ..douyin.friends import fetch_mutual_friends
from ..douyin.inbox import Inbox, _hid, update_bio
from ..douyin.publish import PublishError, publish
from ..paths import DRAFTS_DIR, STICKER_DIR
from ..pet import bus
from ..llm.client import LLMError
from ..names import uid_of
from ..llm.router import BudgetExceeded
from . import safety
from .brain import Brain
from .commands import OwnerCommands, looks_like_command
from .my_taste import MyTaste
from .budget import Budget, in_active_hours, parse_hhmm
from . import digest
from .dedupe import WORDS as ACT_WORDS, ContentMarks
from .doctor import Doctor
from .learner import Learner
from .owner_taste import OwnerTaste
from .likes import LikePolicy, impatient_skip
from .listen import Listener
from .persona import douyin_bio

log = logging.getLogger(__name__)

STATE_LABELS = {
    "stopped": "未启动",
    "idle": "待命中",
    "login": "等待扫码登录",
    "running": "元气运行中",
    "resting": "休息一下",
    "outside": "不在活跃时段",
    "budget_done": "今天的额度用完啦",
    "paused": "已暂停",
    "captcha": "遇到验证码，等你处理",
    "error": "出错了",
}


class Agent(threading.Thread):
    def __init__(self, db: Database):
        super().__init__(daemon=True, name="aiWannaTikTok-agent")
        self.db = db
        self.brain = Brain(db)
        self.budget = Budget(db)
        self.likes = LikePolicy(db)
        self.memory = self.brain.memory          # 她的记忆（事件、私信好友的印象卡、日记）
        self.marks = ContentMarks(db)            # 同样的内容只互动一次、同一类每天只互动一次
        self.taste = OwnerTaste(db)              # 她慢慢了解到的主人口味：只分享 / @ 主人真想看的
        # 她自己的口味：跟着每天刷到的、聊到的自己慢慢变（不用设置），带进她的身份
        self.my_taste = MyTaste(db, self.brain.router, memory=self.memory)
        self.memory.taste_line = self.my_taste.line
        try:
            self.my_taste.seed_from_history(_int(db.rules().get("interest_threshold"), 6))
        except Exception:  # noqa: BLE001
            log.warning("补算口味失败", exc_info=True)
        self.commands = OwnerCommands(self)      # 主人在私信里让她去做事（私信谁、@ 谁、搜视频发给谁、发作品……）
        self._thread_capped: set = set()         # 今天已经记过“接着聊到上限”的人（动态只记一次）
        self.memory.on_taste = self.taste.learn_said
        self.learner = Learner(db, self.brain.router)   # 她每天自己学到的东西
        self.doctor = Doctor(db)                 # 自检：规则发现反复出错、卡住了，给你带证据的提醒卡
        self.listener = Listener()               # 听视频里的声音（Whisper，本机）
        self._listen_warned = False
        # 每位主人：(上次联系 / 开始计时的时刻, 一个 0～1 的随机数)。隔多久再主动找 TA 由“她现在的心情”决定，
        # 随机数只决定落在这个心情对应的时间范围里的哪一点（心情变了，等的时间跟着变）
        self._owner_next: dict[str, tuple] = {}
        self._owner_tip_day = ""                 # 哪天已经提醒过“私信列表里找不到主人”
        self._upkeep_at = 0.0                    # 上次整理记忆（写日记、淡忘）的时间
        self.q: queue.Queue = queue.Queue()
        self.browser: DouyinBrowser | None = None
        self.state = "stopped"
        self.detail = ""
        self.current: dict = {}
        sess = db.get_json("douyin_session", {}) or {}
        # 上次登录过、没点“退出登录”：界面直接显示已登录，点“开始刷视频”就直接开始，不用再扫码
        self.logged_in = bool(sess.get("logged_in"))
        self._logout_pending = bool(sess.get("logout_pending"))
        self._session_saved_at = 0.0
        self._dm_flag = False  # 看视频途中发现了新私信（秒回模式）
        self._cur_rules: dict = {}
        self.attention: dict | None = None  # 需要你亲自处理的事（界面上会弹窗）：手机验证码 / 滑块验证
        self._attention_seq = 0
        self.alert = ""
        self._want_run = False
        # 每次点“开始刷视频”、每天活跃时段开始的时候：先把攒下的评论（@ 她、回复她的评论、评论她的作品）
        # 和私信全部回完，再去刷视频。以前收到、还没回的也回，不丢下
        self._drain_pending = False
        self._active_epoch = None     # 现在这一段活跃时段是从什么时候开始的（换了就是新的一段）
        self._notice_flag = False     # 看视频途中“通知”上来了新的红点
        self._pause_requested = False
        self._pause_queued = 0  # 已经点了“让她休息一下”、还没处理到的暂停命令数（急停开关等它们处理完才复位）
        self._shutdown = False
        self._fails = 0
        self._since_rest = 0
        self._rest_until = 0.0
        self._next_at = 0.0   # 下次去“互动消息”看 @、评论回复的时间
        self._next_dm = 0.0   # 下次去看私信的时间
        self._notice_fail_warned = 0.0   # 上次在动态里提醒“通知面板打不开”的时间
        self.notice_checked: dict = {}   # 上次看“互动消息”的时间和结果
        self.activity = ""    # 桌宠用：watch 刷视频 / inbox 回消息 / publish 发作品 / 空 什么都没做
        # 上次软件关闭（或出错）时还在“发布中”的作品：标记为没发出去，让你可以再点一次发布
        db.execute("UPDATE drafts SET status='failed', error='上次发布没有完成（软件关闭或中断了），可以再点一次“确认发布”' "
                   "WHERE status='publishing'")
        self.mood = self.brain.mood

    # ---------- 给界面用 ----------
    def send(self, cmd: str, **kw) -> None:
        self.q.put((cmd, kw))

    def ask(self, cmd: str, timeout: float = 120.0, **kw):
        """别的线程（“其他功能”的下载任务）请后台线程用浏览器办一件事，等它办完拿结果。
        浏览器（Playwright）只能在后台这个线程里用，所以不能直接拿过去用。"""
        box = {"done": threading.Event(), "result": None, "error": None}
        self.send(cmd, _box=box, **kw)
        if not box["done"].wait(timeout):
            raise TimeoutError("后台一直在忙，没腾出手来打开这个作品")
        if box["error"] is not None:
            raise box["error"]
        return box["result"]

    def request_shutdown(self) -> None:
        """关软件：马上让手上的事收尾，然后正常关闭浏览器。"""
        self._shutdown = True
        self._want_run = False
        self.send("shutdown")

    def request_pause(self) -> None:
        """界面点“让她休息一下”：立刻结束当前所有操作。

        直接改运行标记、按下急停开关（app/cancel.py）：后台线程在下一个检查点（找元素、点击、
        打字、等待、请求大模型……）就把手上的事放下，不等这条消息回完、这个视频看完、这次搜索结束。
        还在排队、没开始的操作也一并取消。多点几次也只算一次。
        """
        cancel.STOP.set()
        if self._pause_requested:
            return  # 已经在停了：重复点击不重复处理
        self._pause_requested = True
        self._pause_was_running = self._want_run or self.state not in ("paused", "idle")
        self._want_run = False
        self._set("paused", "正在停下：马上放下手上的事")
        self.current = {**(self.current or {}), "step": "正在停下"}
        self._pause_queued += 1
        self.send("pause")

    def _stopped(self) -> bool:
        """刷视频过程中的检查点：已经要求暂停（或软件要关了）就立刻收手。"""
        if self._want_run and not self._shutdown:
            return False
        self.current["step"] = "已暂停"
        return True

    def status(self) -> dict:
        next_at = max(0, int(self._next_at - time.time())) if self._want_run else None
        next_dm = max(0, int(self._next_dm - time.time())) if self._want_run else None
        return {
            "state": self.state,
            "label": STATE_LABELS.get(self.state, self.state),
            "detail": self.detail,
            "current": self.current,
            "logged_in": self.logged_in,
            "attention": self.attention,
            "browser_open": bool(self.browser),
            "browser_hidden": bool(self.browser.hidden) if self.browser else True,
            "running": self._want_run,
            "next_at_s": next_at,
            "next_dm_s": next_dm,
            "notice_checked": self.notice_checked,
            "draining": bool(self._drain_pending and self._want_run),
            "alert": self.alert,
            "activity": self.activity if self._want_run or self.activity == "publish" else "",
        }

    # ---------- 线程主循环 ----------
    def run(self) -> None:
        cancel.bind_current_thread()  # 急停只打断这个后台线程，界面那边的操作不受影响
        while not self._shutdown:
            if cancel.STOP.is_set() and self._pause_queued <= 0:
                cancel.reset()  # 叫停之前排着的命令都已取消、暂停也处理完了：复位，之后的操作照常做
            try:
                cmd = self.q.get(timeout=0.5 if self._want_run else 1.0)
                self._dispatch(*cmd)
            except queue.Empty:
                pass
            if self._want_run and not self._shutdown:
                try:
                    with self._focus_guard():
                        self._tick()
                    self._fails = 0
                except OperationCancelled:
                    self._on_cancelled()
                except CaptchaDetected:
                    self._on_captcha()
                except BudgetExceeded:
                    self._set("budget_done", "今日模型花费已到上限，明天自动继续")
                    self._sleep(60)
                except Exception as e:  # noqa: BLE001 —— 后台线程不能因为单次失败退出
                    self._fails += 1
                    log.exception("运行出错")
                    bus.emit("oops", "呜……刚才出错了，我再试试")
                    self.db.add_event("error", "运行出错", str(e)[:200])
                    if self.browser:
                        self.browser.dump("error")
                    if self._fails >= 3:
                        self._want_run = False
                        self._set("paused", "连续 3 次操作失败，已自动暂停")
                        self.alert = (f"连续 3 次操作失败，已自动暂停。最后一次的原因：{str(e)[:120]}。"
                                      "出错时的页面已保存在 data\\debug")
                        self.db.add_event("pause", "自动暂停", f"连续 3 次操作失败：{str(e)[:150]}")
                    else:
                        self._sleep(10)
        if self.browser:
            log.info("正在关闭浏览器（保存登录状态）")
            self.browser.stop()

    def _set(self, state: str, detail: str = "") -> None:
        self.state, self.detail = state, detail

    def _focus_guard(self):
        """浏览器藏在屏幕外干活时，任何一步抢走了你的键盘焦点都会马上还回去。"""
        return self.browser.keep_focus() if self.browser else nullcontext()

    def _dispatch(self, cmd: str, kw: dict) -> None:
        self._depth = getattr(self, "_depth", 0) + 1
        try:
            with self._focus_guard():
                getattr(self, f"_cmd_{cmd}")(**kw)
        except OperationCancelled:
            log.info("命令 %s 被“让她休息一下”中止", cmd)
            self._on_cancelled()
            if self._depth > 1 and cancel.requested():
                raise  # 嵌在别的操作里（比如回消息途中处理命令）：外面那件事也要停
        except CaptchaDetected:
            self._on_captcha()
        except Exception as e:  # noqa: BLE001
            log.exception("命令 %s 失败", cmd)
            self.db.add_event("error", f"操作失败：{cmd}", str(e)[:200])
            self.alert = f"操作失败：{e}"
        finally:
            self._depth -= 1

    URGENT = ("publish", "pause", "shutdown")

    def _run_urgent(self) -> None:
        """回 @ / 私信这种一做好几分钟的事，中途每处理完一条就看一眼：
        你点了“确认发布”或“让她休息一下”就马上去办，不用等全部处理完。其它命令照旧排队。"""
        keep = []
        while True:
            try:
                cmd = self.q.get_nowait()
            except queue.Empty:
                break
            if cmd[0] in self.URGENT:
                self._dispatch(*cmd)
            else:
                keep.append(cmd)
        for c in keep:
            self.q.put(c)

    def _sleep(self, seconds: float) -> bool:
        """可被命令打断的等待。返回 False 表示等待中被暂停了。"""
        end = time.time() + seconds
        while time.time() < end and not self._shutdown:
            try:
                cmd = self.q.get(timeout=min(1.0, max(0.05, end - time.time())))
                self._dispatch(*cmd)
            except queue.Empty:
                pass
            if not self._want_run or self._shutdown or cancel.requested():
                return False
        return not self._shutdown

    def _ensure_browser(self) -> DouyinBrowser:
        if self.browser is None or not self.browser.alive():
            if self.browser:
                self.browser.stop()
            self.browser = DouyinBrowser()
            self.browser.on_tab_verify = lambda: self.wait_verify("办刚才那件事")
            self.browser.start()
            if self._logout_pending:  # 浏览器没开的时候点了“退出登录”：现在补上
                self.browser.logout()
                self._remember_login(False)
        was = self.logged_in
        self.logged_in = self.browser.is_logged_in()
        if self.logged_in and time.time() - self._session_saved_at > 600:
            self._remember_login(True)
        elif was and not self.logged_in:
            self.db.set_json("douyin_session", {"logged_in": False, "since": now_str()})
            self.alert = "抖音的登录已经失效（可能在别处退出了，或者太久没用），请重新扫码登录"
        return self.browser

    def _remember_login(self, logged_in: bool) -> None:
        """记住登录状态：登录着就备份 cookie；退出了就清掉记录。"""
        if logged_in and self.browser:
            self.browser.save_session()
        self._session_saved_at = time.time()
        self._logout_pending = False
        self.db.set_json("douyin_session", {"logged_in": logged_in, "since": now_str()})

    # ---------- 命令 ----------
    def _cmd_start(self) -> None:
        b = self._ensure_browser()
        if not b.is_logged_in():
            self.alert = "还没登录抖音，请先点“扫码登录”"
            self._set("idle", "需要先扫码登录抖音")
            return
        self.alert = ""
        self._set("running", "正在打开推荐页")
        try:
            Feed(b).open()
        except BrowserError as e:
            # 推荐页打不开就不进入运行状态，免得对着别的页面瞎刷
            self._want_run = False
            self._set("idle", "没能打开推荐页")
            self.alert = f"{e}。可以检查一下网络或代理，再点“开始刷视频”重试"
            self.db.add_event("error", "没能打开推荐页", str(e)[:200])
            return
        self._want_run = True
        self._next_at = self._next_dm = time.time()
        self._drain_pending = True            # 先把攒下的评论和私信回完，再刷视频
        self._set("running", "先看看有没有人找她")
        self.db.add_event("info", "开始刷视频", "先把攒下的评论和私信回完，再开始刷视频")

    def _cmd_pause(self) -> None:
        self._want_run = False
        self._pause_queued = max(0, self._pause_queued - 1)
        self.activity = ""
        self.current = {**(self.current or {}), "step": "休息中"}
        self._set("paused", "你让她休息一下")
        if self._pause_requested:
            self._pause_requested = False
            if getattr(self, "_pause_was_running", True):
                self.db.add_event("pause", "已暂停", "手动暂停：当前所有操作已立刻结束")

    def _on_cancelled(self) -> None:
        """手上的事被“让她休息一下”打断后的收尾：进入休息状态，浏览器藏回后台。"""
        self._want_run = False
        self.activity = ""
        self.current = {**(self.current or {}), "step": "已停下"}
        self._set("paused", "你让她休息一下")
        if self.browser:
            with cancel.shield():
                try:
                    self.browser.ensure_hidden()
                except Exception:  # noqa: BLE001
                    pass

    def _cmd_login(self) -> None:
        b = self._ensure_browser()
        if b.is_logged_in():
            self.logged_in = True
            self.alert = ""
            self._remember_login(True)
            self._set("idle", "抖音已经登录着，不用再扫码")
            return
        b.open_login()  # 会把浏览器窗口拿到屏幕上，方便扫码
        self._set("login", "请在弹出的浏览器窗口里用抖音 App 扫码")
        end = time.time() + 300
        ok = False
        while time.time() < end and not self._shutdown:
            if b.is_logged_in():
                ok = True
                break
            if self.attention is None and b.sms_visible():
                self._raise_attention("sms", during_login=True)
                end = max(end, time.time() + 600)  # 等你收短信、填验证码，多给 10 分钟
            if cancel.requested():
                break  # 点了“让她休息一下”：不等扫码了
            time.sleep(2)
        self._clear_attention()
        b.hide_window()  # 扫完（或超时）就藏回后台
        self.logged_in = ok
        if ok:
            self.alert = ""
            self._remember_login(True)
            self.db.add_event("info", "抖音登录成功", "以后打开软件会自动保持登录，除非你点“退出登录”")
            self._set("idle", "抖音已登录")
        else:
            self._set("idle", "扫码超时，可以再试一次")

    def _cmd_check_inbox(self) -> None:
        self._ensure_browser()
        was_running = self._want_run
        self._process_inbox(stop_on_pause=was_running, deep=True)    # 你点了“现在去看”：攒下的都翻出来
        if self._want_run and self.browser:
            Feed(self.browser).open()
        elif not was_running:
            self._set("idle" if self.state != "paused" else "paused", "检查完了")

    def _cmd_sync_bio(self) -> None:
        b = self._ensure_browser()
        bio = douyin_bio(self.db.persona())
        ok, reason = update_bio(b, bio)
        detail = bio if ok and not reason else f"{reason}｜{bio}"
        self.db.add_event("info" if ok else "error", "同步抖音简介" + ("成功" if ok else "失败"), detail)
        self.alert = "" if ok else f"同步简介失败：{reason}。已自动保存页面结构到 data\\debug，可以发给开发者"
        if self._want_run:
            Feed(b).open()

    def _cmd_dump(self) -> None:
        b = self._ensure_browser()
        path = b.dump("manual")
        self.db.add_event("info", "已保存页面结构", path)

    def _cmd_open_url(self, url: str) -> None:
        b = self._ensure_browser()
        b.goto(url)

    def _cmd_manual_reply(self, mid: int, text: str) -> None:
        try:
            self._manual_reply(mid, text)
        except OperationCancelled:
            self.db.update_message(mid, reply=text, status="pending",
                                   reason="你让她休息了，这条手动回复没发出去（可以再点一次发送）")
            raise

    def _manual_reply(self, mid: int, text: str) -> None:
        rows = self.db.query("SELECT * FROM messages WHERE id=?", (mid,))
        if not rows:
            return
        m = rows[0]
        b = self._ensure_browser()
        inbox = Inbox(b)
        if m["kind"] in NOTICE_KINDS:
            if not m["video_url"]:
                self.db.update_message(mid, reply=text, status="failed", handled_at=now_str(),
                                       reason="不知道对方是在哪个视频下找她的，没法自动发；请点“在抖音中打开”自己回复")
                return
            try:
                inbox.open_mention_video({"video_url": m["video_url"]}, with_comments=False)
            except FeedReadError as e:
                self.db.update_message(mid, reply=text, status="failed", reason=f"手动回复没发出去：{e}",
                                       handled_at=now_str())
                return
            ok = inbox.reply_mention({"user": m["user_name"], "text": m["content"], "video_url": m["video_url"] or "",
                                      "kind": m["kind"]}, text)
        else:
            ok = inbox.reply_dm({"user": m["user_name"]}, text)
            inbox.close_dm_panel()
        if getattr(ok, "uncertain", False):
            self.db.update_message(mid, reply=text, status="uncertain", reason=UNCERTAIN_REASON, handled_at=now_str())
            return
        self.db.update_message(mid, reply=text, status="manual" if ok else "failed",
                               reason="" if ok else ("手动回复发送失败" + (
                                   f"：{inbox.last_reply_note}" if isinstance(getattr(inbox, "last_reply_note", None), str)
                                   and inbox.last_reply_note else "")), handled_at=now_str())
        if ok and m["kind"] == "dm":
            self.db.add_dm(m["user_name"], "me", text)

    def _cmd_show_browser(self) -> None:
        self._ensure_browser().show_window()

    def _cmd_hide_browser(self) -> None:
        if self.browser and self.browser.alive():
            self.browser.hide_window()

    def _cmd_logout(self) -> None:
        """退出抖音登录。下次要重新扫码。"""
        self._want_run = False
        if self.browser and self.browser.alive():
            self.browser.logout()
            self._remember_login(False)
        else:
            # 浏览器没开：先删掉登录备份，等下次打开浏览器时再清掉里面的登录 cookie
            from ..paths import SESSION_FILE
            try:
                SESSION_FILE.unlink()
            except OSError:
                pass
            self._logout_pending = True
            self.db.set_json("douyin_session", {"logged_in": False, "logout_pending": True, "since": now_str()})
        self.logged_in = False
        self.alert = ""
        self.current = {}
        self._set("idle", "已退出抖音登录，下次需要重新扫码")
        self.db.add_event("info", "已退出抖音登录", "下次开始前需要重新扫码登录")

    def _cmd_shutdown(self) -> None:
        self._shutdown = True
        self._want_run = False

    # ---------- 运行一步 ----------
    def _tick(self) -> None:
        b = self._ensure_browser()
        b.ensure_hidden()  # 插拔显示器 / 改缩放后窗口可能被系统挪回屏幕上：藏回去
        if not self.logged_in:
            self._want_run = False
            self.alert = "抖音登录失效了，请重新扫码登录"
            self._set("idle", "需要重新扫码登录")
            return
        rules = self.db.rules()
        self._cur_rules = rules
        self._memory_upkeep(rules)
        self._confirm_posts()
        if not in_active_hours(rules):
            # 活跃时段以外她不刷视频、不回别人；但主人随时可以找她聊：来了主人的私信照样回
            if self._owner_on(rules) and self._owner_dm_waiting(b, rules):
                self._dm_flag = False
                self._process_inbox(at=False, dm=True, owners_only=True)
                self._next_dm = time.time() + DM_GAP_NIGHT   # 夜里 5 分钟看一次，来了新私信马上看
            self._set("outside", f"活跃时段是 {rules['active_start']}–{rules['active_end']}（主人的私信照样回）"
                      if self._owner_on(rules) else f"活跃时段是 {rules['active_start']}–{rules['active_end']}")
            self._sleep(60)
            return
        key = self._epoch_key(rules)
        if self._active_epoch != key:
            if self._active_epoch is not None:
                self._drain_pending = True    # 新的一段活跃时段开始了（第二天）：先回消息
            self._active_epoch = key
        if self._drain_pending:
            if self._drain_inbox():
                self._drain_pending = False
            if self._want_run:
                Feed(b).open()
            return
        due_at, due_dm = self._inbox_due(b, rules)
        if due_at or due_dm:
            self._process_inbox(at=due_at, dm=due_dm)
            now = time.time()
            if due_at:
                self._next_at = now + NOTICE_GAP
            if due_dm:
                self._next_dm = now + DM_GAP
            if self._want_run:
                Feed(b).open()
            return
        if time.time() < self._rest_until:
            self._set("resting", f"休息到 {datetime.fromtimestamp(self._rest_until).strftime('%H:%M')}")
            self._sleep(min(30, self._rest_until - time.time()))
            return
        self._maybe_publish_wish(rules)
        owner = self._owner_due(rules)
        if owner is not None:
            self._owner_reach_out(b, owner, rules)
            if self._want_run:
                Feed(b).open()
            return
        if not self.budget.can("videos"):
            self._set("budget_done", "今天看视频的额度用完啦，有人找她照样会回")
            self.activity = ""
            self._sleep(60)
            return
        self._set("running", "正在刷视频")
        self.activity = "watch"
        self._watch_one(b, rules)
        self._since_rest += 1
        if self._since_rest >= int(rules.get("rest_every") or 15):
            self._since_rest = 0
            minutes = random.uniform(float(rules.get("rest_min") or 10), float(rules.get("rest_max") or 30))
            self._rest_until = time.time() + minutes * 60
            self.activity = ""
            self.mood.event("rest")
            self.db.add_event("info", "休息一下", f"刷了一会儿，休息 {int(minutes)} 分钟")

    def _engage_comments(self, feed: Feed, v: dict, dec: dict, rules: dict) -> float:
        """给评论区里她喜欢的评论点赞、回复她想接话的那一条。返回额外花的模型费用。"""
        items = v.get("comment_items") or []
        if not rules.get("engage_comments", True) or not items:
            return 0.0
        me = str(self.db.persona().get("name") or "").strip()

        def ok_target(c: dict) -> bool:
            if self.db.comment_done(c["cid"]) or (c["user"] and self.db.is_blocked(c["user"])):
                return False
            if me and c["user"].strip() == me:
                return False  # 不给自己的评论点赞 / 回复
            return safety.keyword_hit(c["text"], rules) is None and safety.output_problem(c["text"]) is None

        cost = 0.0
        left = int(self.budget.remaining("comment_likes"))
        likes = [items[i] for i in dec.get("comment_likes") or [] if i < len(items) and ok_target(items[i])][:min(3, left)]
        replies: list[tuple[dict, str]] = []
        want = dec.get("comment_reply")
        if want and self.budget.can("comment_replies") and want["to"] < len(items) and ok_target(items[want["to"]]):
            target, text = items[want["to"]], want["text"]
            problem = safety.output_problem(text) or (
                f"命中{safety.keyword_hit(text, rules)}" if safety.keyword_hit(text, rules) else None)
            if problem is None:
                try:
                    ok, reason, c2 = self.brain.safety_check(
                        f"视频文案：{v['desc'][:80]}\n{target['user'] or '一位用户'}的评论：{target['text']}", text)
                    cost += c2
                    problem = None if ok else f"安全检查未通过：{reason}"
                except LLMError as e:
                    problem = f"安全检查失败：{e}"
            if problem is None:
                replies.append((target, text))
            else:
                log.info("回复评论没发：%s（%s）", text, problem)
        if not likes and not replies:
            return cost
        self.current["step"] = "给喜欢的评论点赞 / 回复"
        done = feed.interact_comments(likes, replies)
        title = f"《{v['desc'][:16] or '无文案'}》"
        if done["liked"]:
            bus.emit("comment_like", done["liked"][0]["text"][:30])
        for c in done["liked"]:
            self.budget.use("comment_likes")
            self.db.add_comment_action(c["cid"], v["video_id"], c["user"], c["text"], liked=True, video=v)
            self.db.add_event("comment_like", f"赞了 {c['user'] or '一位用户'} 的评论 {title}", f"“{c['text'][:60]}”",
                              ref=f"cmt:{c['cid']}")
        for c, text in done["replied"]:
            bus.emit("comment_reply", text)
            self.budget.use("comment_replies")
            self.db.add_comment_action(c["cid"], v["video_id"], c["user"], c["text"], reply=text, video=v)
            self.db.add_event("comment_reply", f"回复了 {c['user'] or '一位用户'} 的评论 {title}",
                              f"对方：“{c['text'][:40]}”\n她：“{text}”", ref=f"cmt:{c['cid']}")
        return cost

    def _show_frames(self, frames: list[bytes]) -> None:
        """把截到的画面缩成小图，放进状态里给仪表盘“她正在看”显示。"""
        try:
            self.current["frames"] = [thumb_data_url(f) for f in frames[:4]]
        except Exception:  # noqa: BLE001 —— 显示失败不影响刷视频
            self.current["frames"] = []

    def _watch_one(self, b: DouyinBrowser, rules: dict) -> None:
        feed = Feed(b)
        skip_ads = bool(rules.get("skip_ads", True))
        try:
            v = feed.read_current(with_comments=True, skip_ad=skip_ads)
        except FeedReadError as first:
            # 可能被跳到了别的页面：重新打开推荐页再读一次；还读不到就交给主循环（连续 3 次会自动暂停）
            log.warning("%s，重新打开推荐页再试", first)
            self.current = {"title": "", "author": "", "step": "读不到视频信息，重新打开推荐页"}
            feed.open()
            v = feed.read_current(with_comments=True, skip_ad=skip_ads)
        self.current = {"title": v["desc"][:40] or "（无文案）", "author": v["author"], "step": "读文案和热评",
                        "frames": []}
        if self.db.seen_video(v["video_id"]):
            feed.next()
            return
        if self._stopped():
            return
        if v.get("ad") and skip_ads:
            # 刷到广告：真人大多直接划走；也不让她给广告点赞、评论，还省一次模型调用
            bus.emit("skip")
            self.current["step"] = "是广告，划走"
            title = f"《{v['desc'][:24] or '无文案'}》"
            vrow = self.db.add_video(video_id=v["video_id"], url=v["url"], author=v["author"], title=v["desc"][:100],
                                     decision="skip", reason="广告")
            self.db.add_event("skip", f"划走广告 {title}", "刷到广告，直接划走", ref=f"video:{vrow}")
            if not self._sleep(random.uniform(1.5, 4.0)) or self._stopped():
                return
            feed.next()
            return
        bad_kw, good_kw = watch_keywords(v, rules)
        if bad_kw:
            # 你设的“不想看”的内容：直接划走，不打分、不花模型的钱（参考 dsh-bili-scroll 的排除词）
            bus.emit("skip")
            self.current["step"] = f"有“{bad_kw}”，划走"
            title = f"《{v['desc'][:24] or '无文案'}》"
            vrow = self.db.add_video(video_id=v["video_id"], url=v["url"], author=v["author"], title=v["desc"][:100],
                                     decision="skip", reason=f"命中不想看的关键词“{bad_kw}”")
            self.db.add_event("skip", f"划走 {title}", f"命中你设的不想看的关键词“{bad_kw}”", ref=f"video:{vrow}")
            if not self._sleep(random.uniform(1.0, 3.0)) or self._stopped():
                return
            feed.next()
            return
        self.budget.use("videos")
        bias = self.mood.bias()
        dwell = random.uniform(float(rules.get("dwell_min") or 8), float(rules.get("dwell_max") or 40)) * bias["dwell"]
        cost = 0.0
        can_see = self.brain.router.task_can_see("vision")
        # 先截 1 张画面：显示在仪表盘上；模型能识图的话，和文案一起交给她判断感不感兴趣
        self.current["step"] = "先看一眼画面"
        first = feed.capture_frames(1, 1.5, sleep=self._sleep)
        self._show_frames(first)
        if self._stopped():
            return
        try:
            tri = self.brain.triage(v, first if can_see else [], known_topics=self.marks.known_topics(),
                                    taste=self.my_taste.line())
        except LLMError as e:
            self.db.add_event("error", "模型调用失败", str(e)[:200])
            self._sleep(dwell)
            feed.next()
            raise
        cost += tri["cost"]
        if good_kw and tri["interest"] < 10:
            tri["interest"] += 1          # 你设的“想多看”的内容：她会更愿意看下去
            tri["reason"] = (tri.get("reason") or "") + f"（有你想让她多看的“{good_kw}”）"
        topic = tri.get("topic") or ""
        nudge, nudge_why = self.my_taste.nudge(topic)
        if nudge and 0 < tri["interest"] + nudge <= 10:
            tri["interest"] += nudge      # 她自己的口味：看腻了的少看一点，听人说起的多看看
            tri["reason"] = (tri.get("reason") or "") + f"（{nudge_why}）"
        title = f"《{v['desc'][:24] or '无文案'}》"
        threshold = max(0, min(10, _int(rules.get("interest_threshold"), 6) + bias["threshold"]))
        if tri["interest"] >= 8:
            self.mood.event("video_love", title=v["desc"])
            bus.emit("love", v["desc"][:20])
        else:
            self.mood.event("video_ok" if tri["interest"] >= threshold else "video_meh")
        mood_key = self.mood.current().get("key", "calm")
        bored_skip = tri["interest"] >= threshold and impatient_skip(tri["interest"], mood_key, self.likes.rnd)
        if tri["interest"] < threshold or bored_skip:
            bus.emit("skip")
            reason = "有点没耐心，还没看完就划走了" if bored_skip else (tri["reason"] or "不是她喜欢的类型")
            self.current["step"] = "没耐心，划走" if bored_skip else "不太感兴趣，划走"
            vrow = self.db.add_video(video_id=v["video_id"], url=v["url"], author=v["author"], title=v["desc"][:100],
                                     interest=tri["interest"], decision="skip", reason=reason, cost=cost, topic=topic)
            self.db.add_event("skip", f"划走 {title}", reason, ref=f"video:{vrow}")
            self._taste_video(topic, tri["interest"], threshold)
            if not self._sleep(min(dwell, random.uniform(2, 6))) or self._stopped():
                return
            feed.next()
            return
        if self._stopped():
            return

        # 同样的内容以前互动过的、今天同一类已经互动过的：那几样这次不再做（app/agent/dedupe.py）
        chk = self.marks.check(v, topic, first[0] if first else None)
        blocked = chk["blocked"]
        topic = chk["topic"] or topic
        if blocked:
            log.info("不重复互动：%s", "；".join(f"{ACT_WORDS[k]}——{w}" for k, w in blocked.items()))
        # 点不点赞由她自己（身份卡 + 记忆）判断；程序只看今天的额度和进度（app/agent/likes.py）
        lk = self.likes.judge(tri["interest"], v["author"], topic, rules, mood_key,
                              used=float(self.db.usage().get("likes") or 0))
        if "like" in blocked:
            lk = {"ok": False, "why": blocked["like"]}
        can_like = self.budget.can("likes") and lk["ok"]
        liked = False
        if self._stopped():
            return
        # 听一听视频里说了什么 / 唱了什么（本机语音转文字；浏览器是静音播放的，不影响你用电脑）
        if rules.get("listen_enabled", True):
            self.current["step"] = "感兴趣，听听视频里说了什么"
            v["speech"] = self._listen(b, v, rules)
        if self._stopped():
            return
        if can_see:
            self.current["step"] = "感兴趣，多截几张画面细看"
            more = feed.capture_frames(max(0, int(rules.get("frames_per_video") or 4) - len(first)), dwell,
                                       sleep=self._watch_sleep)
            frames = first + more
            self._show_frames(frames)
        else:
            # “看视频”分配的模型不能识图：只根据文案和评论判断
            self.current["step"] = "看文案和评论（模型不识图）"
            self._watch_sleep(dwell)
            frames = []
        if self._stopped():
            return
        self.current["step"] = "想想要不要点赞 / 评论"
        engage = bool(rules.get("engage_comments", True)) and bool(v.get("comment_items"))
        # 自己发评论：说不说由她自己判断；程序只看今天的额度和进度，并且同一个视频永远只评论一次
        post_gate = {"ok": False, "invite": False, "why": "今天评论的额度用完了"}
        if self.db.commented_video(v["video_id"]):
            post_gate["why"] = "这个视频以前评论过了，不再评论（别人回复她的还可以接着聊）"
        elif "comment" in blocked:
            post_gate["why"] = blocked["comment"]
        elif self.budget.can("comments"):
            post_gate = self.likes.post_gate(tri["interest"], topic, rules,
                                             float(self.db.usage().get("comments") or 0))
        # 分享给主人、在评论里 @ 主人：不是每个视频都分享——照她慢慢了解到的主人口味挑（app/agent/owner_taste.py）
        share_owners = [] if "share" in blocked else self._taste_pick(self._owners_for_share(rules), topic,
                                                                     tri["interest"], "name")
        try:
            mem_ctx = self.memory.video_context(v, topic)
        except Exception:  # noqa: BLE001 —— 记忆读不出来不影响刷视频
            log.warning("读记忆失败", exc_info=True)
            mem_ctx = ""
        know = self.learner.context(f"{v.get('desc') or ''} {topic}", topic)
        if know:
            mem_ctx = (mem_ctx + "\n\n" + know).strip()
        friends = []
        if (rules.get("at_friends_enabled", True) and self.budget.can("at_friends") and post_gate["ok"]
                and "share" not in blocked):
            friends = self.db.friends_for_at(max(1, _int(rules.get("at_friend_per_day"), 1)))[:8]
        if post_gate["ok"] and "share" not in blocked:
            # 主人排在最前面：她喜欢这个视频、照她对主人口味的了解主人也会喜欢，就在评论里 @ 主人来看
            at_owners = self._taste_pick(self._owners_for_at(rules), topic, tri["interest"], "nickname")
            names = {o["nickname"] for o in at_owners}
            friends = (at_owners + [f for f in friends if f["nickname"] not in names])[:8]
        # 发评论、赞别人的评论、回复别人的评论：看了的视频都要判断，和视频本身赞没赞无关；
        # 赞评论只受今天的额度和进度限制（app/agent/likes.py）
        cl_used = float(self.db.usage().get("comment_likes") or 0)
        cl_gate = (self.likes.comment_gate(rules, cl_used)
                   if engage and self.budget.can("comment_likes") else {"ok": False, "why": ""})
        # 收藏：一天匀着收、同一类收多了提高标准（app/agent/likes.py）
        col_gate = {"ok": False, "why": "", "note": ""}
        if "collect" in blocked:
            col_gate["why"] = blocked["collect"]
        elif rules.get("collect_enabled", True) and self.budget.can("collects"):
            col_gate = self.likes.collect_gate(tri["interest"], topic, rules,
                                               float(self.db.usage().get("collects") or 0))
        dec = self.brain.decide(v, frames, can_like,
                                post_gate["ok"],
                                cl_gate["ok"],
                                engage and self.budget.can("comment_replies"), friends=friends,
                                can_collect=col_gate["ok"], collect_note=col_gate["note"],
                                memory=mem_ctx, owners=share_owners,
                                can_sticker=post_gate["ok"] and self._sticker_ok(rules, "comment"))
        cost += dec["cost"]
        log.info("她的判断（%s 分）：点赞 %s；评论 %s", tri["interest"],
                 ("要" if dec.get("like") else "不要") if can_like else f"不考虑（{lk['why']}）",
                 (f"写了“{dec['comment'][:30]}”" if dec.get("comment") else "不写") if post_gate["ok"]
                 else f"不考虑（{post_gate['why']}）")
        # 模型给热评打的分 → 真正要赞的：只赞够分的，一般 1 条，特别精彩才多赞，按今天的进度封顶
        dec["comment_likes"] = (self.likes.pick_comments(dec.get("comment_scores") or [], rules, cl_used)
                                if cl_gate["ok"] else [])
        if self._stopped():  # 想完的时候已经被叫停了：不再点赞或评论
            return
        if dec["like"] and can_like and not liked and self.budget.can("likes"):
            liked = feed.like()
            if liked:
                self.budget.use("likes")
                self.mood.event("liked")
                bus.emit("like")
        collected = False
        if dec.get("collect") and col_gate["ok"] and self.budget.can("collects") and not self._stopped():
            collected = feed.collect()
            if collected:
                self.budget.use("collects")
                self.mood.event("collected", title=v["desc"])
                bus.emit("collect", v["desc"][:30])
        comment = dec["comment"] if post_gate["ok"] else ""      # 程序没同意评论的（额度、进度、评论过了），她写了也不发
        posted = ""
        if comment and self.budget.can("comments") and not self._stopped():
            problem = safety.output_problem(comment) or (
                f"命中{safety.keyword_hit(comment, rules)}" if safety.keyword_hit(comment, rules) else None)
            if problem is None:
                ok, reason, c2 = self.brain.safety_check(v["desc"], comment, kind="comment")
                cost += c2
                problem = None if ok else f"安全检查未通过：{reason}"
            friend = dec.get("at_friend")
            if problem is None:
                bus.emit("typing", f"@{friend['nickname']} " + comment if friend else comment)
            pic = self._make_sticker(dec.get("sticker"), animated=False) if problem is None else None
            sent = (feed.comment(comment, mention=friend["nickname"] if friend else "", image=pic)
                    if problem is None else None)
            if pic and sent and getattr(feed, "last_image_ok", False) is True:
                self._sticker_sent("评论里", dec["sticker"])
            if sent is not None and not sent and getattr(sent, "uncertain", False):
                # 点了发送但没在评论区里查到：可能已经发出去了，不重发，按发了算（免得发两遍）
                self.db.add_event("info", f"评论 {title} 可能已经发出去了",
                                  f"“{comment}”——按了发送但没在评论区里确认到，去抖音里看一眼")
            if sent is not None and not sent and not getattr(sent, "uncertain", False):
                self.db.add_event("error", f"评论没发出去 {title}",
                                  f"“{comment}”——按了发送，评论没出现在评论区（可能需要校准页面定位）")
            if sent or getattr(sent, "uncertain", False):
                self.budget.use("comments")
                posted = comment
                if friend and feed.last_mention_ok and friend.get("owner"):
                    posted = f"@{friend['nickname']} {comment}"
                    call = friend.get("call") or friend["nickname"]
                    self.mood.event("at_friend", user=friend["nickname"])
                    self.db.owner_touch(friend["nickname"], closer=0.2)
                    self.db.add_event("owner_at", f"评论里 @ 了主人 {call} 来看 {title}", f"“{posted}”")
                    self.taste.shared(friend["nickname"], "at", v, topic, comment)
                    self.memory.remember("at", f"我在 @{v.get('author')} 的视频《{(v.get('desc') or '')[:30]}》下面评论，"
                                               f"@ 了 {call} 来看，说“{comment[:50]}”", person=friend["nickname"],
                                         importance=3)
                elif friend and feed.last_mention_ok:
                    posted = f"@{friend['nickname']} {comment}"
                    self.budget.use("at_friends")
                    self.db.mark_friend_at(friend["uid"])
                    self.mood.event("at_friend", user=friend["nickname"])
                    self.db.add_event("at_friend", f"@ 了好友 {friend['nickname']} 来看 {title}", f"“{posted}”")
                else:
                    self.mood.event("commented", title=v["desc"])
                bus.emit("comment", posted)
            elif problem:
                log.info("评论没发：%s（%s）", comment, problem)
                self.db.add_event("skip", f"评论被拦下 {title}", f"“{comment}”——{problem}")
        if not self._stopped():
            cost += self._engage_comments(feed, v, dec, rules)
        acts = [k for k, on in (("like", liked), ("collect", collected), ("comment", posted)) if on]
        decision = "+".join(acts) or "watch"
        vrow = self.db.add_video(video_id=v["video_id"], url=v["url"], author=v["author"], title=v["desc"][:100],
                                 interest=tri["interest"], decision=decision, liked=int(liked),
                                 collected=int(collected), comment=posted, reason=dec["reason"], cost=cost,
                                 topic=topic)
        if acts:
            kind = "comment" if posted else "collect" if collected else "like"
            label = " + ".join({"like": "点赞", "collect": "收藏", "comment": "评论"}[a] for a in acts)
            self.db.add_event(kind, f"{label} @{v['author']} {title}", f"评论：“{posted}”" if posted else dec["reason"],
                              ref=f"video:{vrow}")
        else:
            self.db.add_event("skip", f"看完 {title}", dec["reason"] or "看完了，没有互动", ref=f"video:{vrow}")
        self.memory.remember_video(v, tri["interest"], topic, liked, collected, posted)
        self._taste_video(topic, tri["interest"], threshold, liked, collected, bool(posted))
        if dec.get("learned"):
            self.learner.note("video", f"看《{(v.get('desc') or '')[:40]}》（{topic}）学到：{dec['learned']}",
                              ref=(v.get("desc") or "")[:30])
        shared = False
        if dec.get("share_owner") and not self._stopped():
            shared = self._owner_share(b, dec["share_owner"]["owner"], dec["share_owner"]["text"], v, topic)
        self.marks.record(v, chk, {"like": liked, "comment": bool(posted), "collect": collected,
                                   "share": shared or posted.startswith("@")})
        feed.next()

    def _taste_video(self, topic: str, interest, threshold: int, liked: bool = False, collected: bool = False,
                     commented: bool = False) -> None:
        """刷到的这个视频算进她自己的口味（记不下来不影响刷视频）。"""
        try:
            self.my_taste.on_video(topic, interest, threshold, liked, collected, commented)
        except Exception:  # noqa: BLE001
            log.warning("记口味失败", exc_info=True)

    def _taste_talk(self, their: str, mine: str) -> None:
        """私信、评论区里聊到的也算进她的口味（朋友聊起的东西她会更留意）。"""
        try:
            self.my_taste.on_talk(their, mine)
        except Exception:  # noqa: BLE001
            log.warning("记口味失败", exc_info=True)

    def _taste_pick(self, owners: list[dict], topic: str, interest: int, key: str) -> list[dict]:
        """主人里，照她对 TA 口味的了解，这个视频值得考虑分享 / @ 的那几位（带上口味，写给模型看）。"""
        out = []
        for o in owners:
            name = o.get(key) or o.get("name") or ""
            ok, why = self.taste.allow(name, topic, int(interest or 0))
            if not ok:
                log.debug("不分享给主人 %s：%s", name, why)
                continue
            out.append({**o, "taste": self.taste.profile(name)})
        return out

    def _listen(self, b, v: dict, rules: dict) -> str:
        """听这个视频开头的一段，转成文字；听不了（没装组件、拿不到视频地址）就返回空，不影响刷视频。"""
        try:
            text = self.listener.listen(b, v.get("video_id") or "", str(rules.get("listen_model") or "base"))
        except Exception:  # noqa: BLE001
            log.warning("听视频声音出错", exc_info=True)
            return ""
        broken = getattr(self.listener, "_broken", "")
        if broken and not self._listen_warned:
            self._listen_warned = True
            self.db.add_event("info", "这次听不了视频的声音", broken + "。不影响刷视频，她会只按画面和文案判断")
        return text

    # ---------- 记忆：写日记、逐级合并、淡忘 ----------
    def _memory_upkeep(self, rules: dict) -> None:
        """每隔一会儿整理一次记忆：补写过完的那天的日记（约 500 字），周 / 月合并，把淡掉的记忆忘掉。"""
        now = time.time()
        if now - self._upkeep_at < 900:
            return
        self._upkeep_at = now
        try:
            day = self.memory.diary_due()
            late = datetime.now().hour >= 18 and not in_active_hours(rules)   # 今天的活跃时段已经结束：今天的也可以写了
            if not day and late and self.memory.day_material(today_str()) and not self.db.query(
                    "SELECT 1 FROM diary WHERE level='day' AND period=?", (today_str(),)):
                day = today_str()
            if day:
                self.current = {**self.current, "step": f"写 {day} 的日记"}
                mood = self.mood.current()
                cost = self.memory.write_diary(day, self.brain.system, mood.get("label", ""))
                cost += self.memory.roll_up(self.brain.system)
                if cost:
                    self.db.add_event("info", f"写了 {day} 的日记", f"花了 {cost:.3f} 元，可以在“记忆”页里看")
            self.memory.fade()
            self.memory.drift_affection()             # 好感度：很久没联系会慢慢回落
            self.taste.expire()                        # 分享给主人两天都没反应的：算“没兴趣”
            for d in digest.due(self.db):              # 过完的日子写一份刷视频日报（不调用模型）
                digest.write(self.db, d)
            if self.learner.due():
                self.current = {**self.current, "step": "整理今天学到的东西"}
                self.learner.consolidate(self.brain.system)
            self.my_taste.decay()                      # 她的口味：每一类每天自然淡一点
            self._refresh_my_works()                   # 隔几个小时看看自己作品的点赞、评论
            if self.my_taste.due():
                self.current = {**self.current, "step": "想想自己最近爱看什么"}
                self.my_taste.refresh(self.brain.system)
            self._storage_upkeep()
            n = self.db.purge_comment_messages()      # 评论区聊完的记录过期就删（私信一直留着）
            if n:
                log.info("消息中心：删掉了 %d 条评论区的旧记录", n)
            self._check_growth()
            self.doctor.check({"running": self._want_run, "active": in_active_hours(rules),
                               "resting": time.time() < self._rest_until,
                               "sizes": self.db.get_json("storage_sizes", {}) or {}})
        except (LLMError, BudgetExceeded) as e:
            log.info("这次没写成日记：%s", str(e)[:100])
        except Exception:  # noqa: BLE001
            log.warning("整理记忆出错", exc_info=True)

    STORAGE_EVERY = 6 * 3600     # 多久整理一次记忆仓库（旧记录归档压缩、回收空间）

    def _storage_upkeep(self, force: bool = False) -> dict:
        """整理记忆仓库：旧记录打包压缩、私信上下文只留最近的、回收空间；顺便清理太旧的调试快照（见 storage.py）。"""
        last = (self.db.get_json("storage_last", {}) or {}).get("at", "")
        try:
            age = (datetime.now() - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).total_seconds() if last else 1e9
        except ValueError:
            age = 1e9
        if not force and age < self.STORAGE_EVERY:
            return {}
        self.current = {**self.current, "step": "整理记忆仓库"}
        from ..paths import DEBUG_DIR, PROFILE_DIR
        from ..storage import CACHE_DIRS, _dir_size
        done = self.db.keeper.maintain()
        done["debug_freed"] = self.db.keeper.clean_debug(DEBUG_DIR)
        try:
            self.db.set_json("storage_sizes", {
                "db": os.path.getsize(self.db.path),
                "browser_cache": sum(_dir_size(PROFILE_DIR / d) for d in CACHE_DIRS if (PROFILE_DIR / d).exists())})
        except OSError:
            pass
        return done

    def _check_growth(self) -> None:
        """桌宠的成长：经验够了就升级，桌宠欢呼一下（见 pet/growth.py）。"""
        try:
            from ..pet import growth
            g = growth.status(self.db)
        except Exception:  # noqa: BLE001
            log.warning("算成长等级失败", exc_info=True)
            return
        last = int(self.db.get_json("growth_level", 0) or 0)
        if g["level"] > last:
            self.db.set_json("growth_level", g["level"])
            if last:
                bus.emit("level_up", f"Lv.{g['level']} {g['stage_name']}")
                self.db.add_event("info", f"升级啦：Lv.{g['level']}（{g['stage_name']}）",
                                  f"累计经验 {g['xp']}，离下一级还差 {g['next_xp'] - g['xp']}")

    # ---------- 主人：主动聊天、分享视频 ----------
    def _today_events(self, kind: str) -> int:
        return self.db.query("SELECT COUNT(*) AS n FROM events WHERE kind=? AND ts >= ?",
                             (kind, today_str()))[0]["n"]

    def _owners_for_share(self, rules: dict) -> list[dict]:
        if not rules.get("owner_enabled", True):
            return []
        if self._today_events("owner_share") >= max(0, _int(rules.get("owner_share_per_day"), 2)):
            return []
        return [o for o in self.db.owners(enabled_only=True) if o["share"]]

    def _owners_for_at(self, rules: dict) -> list[dict]:
        """今天还可以在评论里 @ 的主人，带上“TA 喜欢什么”（你写的关系 + 她印象卡里记的常聊话题、TA 说过的事），
        让模型照主人的喜好决定这个视频要不要 @ TA 来看。每天最多 @ 几次在规则里设。"""
        if not rules.get("owner_enabled", True):
            return []
        if self._today_events("owner_at") >= max(0, _int(rules.get("owner_at_per_day"), 2)):
            return []
        out = []
        for o in self.db.owners(enabled_only=True):
            if not o.get("at", 1):
                continue
            p = self.memory.person(o["name"]) or {}
            likes = "；".join(x for x in (p.get("topics") and f"常聊：{p['topics'][:50]}",
                                          p.get("facts") and f"TA 说过：{p['facts'][:60]}",
                                          o.get("relation") and f"你们的关系：{o['relation'][:40]}") if x)
            out.append({"nickname": o["name"], "tags": likes, "owner": True, "call": o.get("call") or "", "uid": ""})
        return out

    def _owner_on(self, rules: dict) -> bool:
        """主人功能开着、而且设了主人：主人的私信随时都看、都回。"""
        return bool(rules.get("owner_enabled", True)) and bool(self.db.owners(enabled_only=True))

    def _owner_mark(self, name: str) -> None:
        """刚和这位主人说过话（她主动找的，或者回了 TA 的私信）：从现在起重新计时。"""
        self._owner_next[name] = (time.time(), random.random())

    def _owner_gap(self, owner: dict, frac: float) -> float:
        """隔多久再主动找这位主人（秒）：看她现在的心情——想撒娇、开心、无聊的时候找得勤，犯困、闹小脾气的时候找得少；
        越亲近隔得越短一点。没有“每天几次”的上限。"""
        lo, hi = OWNER_GAP_MIN.get(self.mood.current()["key"], OWNER_GAP_MIN["calm"])
        close = max(0.0, min(100.0, float(owner.get("closeness") or 0)))
        return (lo + (hi - lo) * frac) * 60 * (1.0 - close / 300.0)

    def _owner_due(self, rules: dict) -> dict | None:
        """现在想不想主动找哪位主人聊天。不设每天的次数，什么时候找看心情（见 _owner_gap）。
        只有一条克制：她连着发了几句主人都没回，就先不打扰，隔上大半天再问候一次。"""
        if not rules.get("owner_enabled", True) or not self.logged_in:
            return None
        if in_quiet_hours(rules):
            return None                           # 主人的勿扰时段：不主动打扰（TA 发来的照样回）
        now = time.time()
        for o in self.db.owners(enabled_only=True):
            if not o["chat"]:
                continue
            name = o["name"]
            st = self._owner_next.get(name)
            if st is None:                        # 这次启动后第一次：过一会儿再找，不要一开软件就发
                self._owner_mark(name)
                continue
            since, frac = st
            if now - since < self._owner_gap(o, frac):
                continue
            n, last = self.db.owner_unanswered(name)
            if n >= OWNER_UNANSWERED_MAX:
                try:
                    quiet = (datetime.now() - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).total_seconds()
                except ValueError:
                    quiet = OWNER_QUIET_HOURS * 3600
                if quiet < OWNER_QUIET_HOURS * 3600:
                    continue                      # 连着几句都没回：先不打扰
            return o
        return None

    # ---------- 她自己的表情包 ----------
    def _sticker_ok(self, rules: dict, where: str) -> bool:
        """现在能不能发表情包：where 是 dm（私信）或 comment（评论）；各有开关，合起来每天有上限。"""
        on = rules.get("sticker_dm", True) if where == "dm" else rules.get("sticker_comment", False)
        return bool(on) and self._today_events("sticker") < max(0, _int(rules.get("sticker_per_day"), 10))

    def _make_sticker(self, sticker: dict | None, animated: bool):
        """把她想配的表情包画出来（用桌宠现在的形象）。画不出来返回 None，不影响发文字。"""
        if not sticker:
            return None
        try:
            from ..pet.host import pet_settings
            from ..publish import stickers
            for old in sorted(STICKER_DIR.glob("sticker_*"), key=lambda f: f.stat().st_mtime)[:-60]:
                old.unlink(missing_ok=True)                      # 只留最近 60 张
            return stickers.make(STICKER_DIR, sticker.get("mood") or "", sticker.get("text") or "", animated=animated,
                                 rig_name=str(pet_settings(self.db).get("rig") or "default"))
        except Exception:  # noqa: BLE001
            log.warning("表情包没画出来", exc_info=True)
            return None

    def _sticker_sent(self, where: str, sticker: dict) -> None:
        label = f"{sticker.get('mood')}" + (f" ·“{sticker['text']}”" if sticker.get("text") else "")
        self.db.add_event("sticker", f"{where}发了一张自己的表情包", label)
        log.info("%s发了表情包：%s", where, label)

    def _dm_sticker(self, inbox: Inbox, user: str, sticker: dict | None, rules: dict) -> None:
        """文字发出去以后，再把表情包发过去（发不出去不算失败，只记日志）。"""
        if not sticker or not self._sticker_ok(rules, "dm"):
            return
        pic = self._make_sticker(sticker, animated=True)
        if pic is None:
            return
        try:
            if inbox.send_dm_file(user, pic):
                self._sticker_sent(f"给 @{user} ", sticker)
                self.db.add_dm(user, "me", f"[发了一张表情包：{sticker.get('text') or sticker.get('mood')}]")
        except CaptchaDetected:
            raise
        except Exception:  # noqa: BLE001
            log.warning("表情包没发出去", exc_info=True)

    def _owner_send(self, b, owner: dict, text: str, sticker: dict | None = None) -> bool:
        inbox = Inbox(b)
        try:
            # 像真人发消息：一行一条，几条分开发
            parts = [x.strip() for x in str(text or "").split("\n") if x.strip()] or [text]
            sent, _n = self._send_dm_parts(inbox, {"user": owner["name"]}, parts)
            if sent:
                self._dm_sticker(inbox, owner["name"], sticker, self.db.rules())
        finally:
            with cancel.shield():
                try:
                    inbox.close_dm_panel()
                except Exception:  # noqa: BLE001
                    pass
        if not sent and self._owner_tip_day != today_str():
            self._owner_tip_day = today_str()
            self.db.add_event("info", f"没能主动联系主人 {owner['name']}",
                              "私信列表里没找到 TA，或者消息没发出去。请确认“主人”里填的名字和私信列表里显示的一样，"
                              "并且 TA 先给她发过一条私信")
        return bool(sent)

    def _owner_reach_out(self, b, owner: dict, rules: dict) -> None:
        name = owner["name"]
        self._owner_mark(name)
        self._set("running", f"想找主人 {owner.get('call') or name} 聊聊")
        self.current = {"title": "", "author": "", "step": f"想找 {owner.get('call') or name} 聊聊"}
        brief = digest.brief_for_owner(self.db)       # 每天第一次找主人：顺口说说昨天刷到的、学到的
        try:
            draft = self.brain.owner_hello(owner, self.db.dm_history(name), self.memory.dm_context(name, ""),
                                           self.memory.day_material(today_str()),
                                           sticker=self._sticker_ok(rules, "dm"), brief=brief)
        except LLMError as e:
            self.db.add_event("error", "模型调用失败", str(e)[:200])
            return
        if not draft.ok:
            log.info("想对主人说的话没发：%s（%s）", draft.text[:30], draft.reason)
            return
        bus.emit("reply_dm", name)
        if self._owner_send(b, owner, draft.text, draft.sticker):
            if brief:
                digest.mark_told(self.db)
            for p in [x.strip() for x in draft.text.split("\n") if x.strip()]:
                self.db.add_dm(name, "me", p)
            self.db.owner_touch(name, closer=0.3, proactive=True)
            self.memory.remember("dm", f"我主动找 {owner.get('call') or name} 聊天，说“{draft.text[:60]}”",
                                 scope=f"dm:{name}", person=name, importance=4)
            self.mood.event("dm", user=name)
            self.db.add_event("owner_chat", f"主动找主人 {owner.get('call') or name} 聊天", f"“{draft.text[:60]}”")

    def _owner_share(self, b, owner: dict, text: str, v: dict, topic: str = "") -> bool:
        """刷到想让主人也看看的视频：用抖音自己的“分享给朋友”把整个视频发给 TA（私信里是视频卡片，
        点开直接播放），再跟一句她想说的话。不发视频地址。视频卡片没发出去就什么都不发（光有一句话对方看不懂）。"""
        if safety.output_problem(text):
            return False
        name = owner["name"]
        self.current["step"] = f"把这个视频分享给 {owner.get('call') or name}"
        inbox = Inbox(b)
        carded = False
        try:
            if Feed(b).share_to_friend(name):
                carded = inbox.has_my_share(name)        # 去私信里看一眼：视频卡片真的在那儿了
        except OperationCancelled:
            raise
        except Exception:  # noqa: BLE001
            log.warning("分享视频给主人出错", exc_info=True)
        if not carded:
            with cancel.shield():
                try:
                    inbox.close_dm_panel()
                except Exception:  # noqa: BLE001
                    pass
            log.warning("没能把视频分享给主人 %s（视频卡片没出现在私信里），这次不发", name)
            if self._owner_tip_day != "share:" + today_str():
                self._owner_tip_day = "share:" + today_str()
                self.db.add_event("error", f"没能把视频分享给主人 {owner.get('call') or name}",
                                  "视频卡片没发出去，这次什么都没发（不会改成发一串地址）。"
                                  "data\\debug 里有保存的页面的话，发给开发者校准“分享给朋友”的面板")
            return False
        msg = text.strip()
        self.taste.shared(name, "dm", v, topic, msg)      # 视频卡片已经发出去了：等主人的反应，慢慢了解 TA 的口味
        if self._owner_send(b, owner, msg):
            self.db.add_dm(name, "me", f"[分享了视频《{(v.get('desc') or '')[:20]}》] {msg}")
            self.db.owner_touch(name, closer=0.3, proactive=True)
            self.memory.remember("dm", f"我把 @{v.get('author')} 的视频《{(v.get('desc') or '')[:30]}》分享给了"
                                       f" {owner.get('call') or name}，说“{text[:50]}”",
                                 scope=f"dm:{name}", person=name, importance=4)
            self.db.add_event("owner_share", f"给主人 {owner.get('call') or name} 分享了视频",
                              f"“{text[:50]}” 《{(v.get('desc') or '')[:24]}》")
        return True

    # ---------- @ 和私信 ----------
    def _need_profile(self, user: str, rules: dict) -> bool:
        """要不要去对方主页看资料（头像、昵称、抖音号）：没看过的要看；一周更新一次。"""
        u = self.db.dm_user(user)
        if not u or not u.get("checked_at"):
            return True
        try:
            age_h = (datetime.now() - datetime.strptime(u["checked_at"], "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600
        except ValueError:
            return True
        return age_h >= 24 * 7

    def _watch_sleep(self, seconds: float) -> bool:
        """看视频时的等待：被暂停返回 False；秒回模式下来了新私信也提前结束（返回 False，只是不再多截图）。"""
        end = time.time() + seconds
        while time.time() < end:
            if not self._sleep(min(1.0, max(0.05, end - time.time()))):
                return False
            if self._dm_arrived():
                self.current["step"] = "来了新私信，看完这一眼就去回"
                return False
        return True

    def _dm_arrived(self) -> bool:
        """看视频途中来了新私信：这个视频不再多看，马上去回。“通知”上来了新的红点（有人 @ 她、回复她的评论）
        记一笔，看完这个视频就去看。"""
        if not self.browser:
            return False
        if self.browser.dm_signal():
            self._dm_flag = True
        try:
            if self.browser.notice_signal():
                self._notice_flag = True
        except Exception:  # noqa: BLE001
            pass
        return self._dm_flag

    def _owner_dm_waiting(self, b: DouyinBrowser, rules: dict) -> bool:
        """活跃时段以外：有没有新私信值得去看一眼（到了该看的时间，或者“消息”上有未读）。"""
        if time.time() >= self._next_dm:
            return True
        try:
            return bool(self._dm_flag or b.dm_signal())      # 未读数刚变多了（别人留着的旧未读不算）
        except Exception:  # noqa: BLE001
            return False

    def _inbox_due(self, b: DouyinBrowser, rules: dict) -> tuple[bool, bool]:
        """现在该不该去看评论区的消息（@ 她、回复她的评论、评论她的作品）/ 私信。
        到了该看的时间就去看；“通知”“消息”上的未读数刚变多了，也马上去看。不设开关、不设上限：有人找她就回。"""
        now = time.time()
        due_at = now >= self._next_at or self._notice_flag
        due_dm = now >= self._next_dm or self._dm_flag
        try:
            due_at = due_at or b.notice_signal()
            due_dm = due_dm or b.dm_signal()
        except Exception:  # noqa: BLE001
            pass
        if due_at:
            self._notice_flag = False
        if due_dm:
            self._dm_flag = False
        return due_at, due_dm

    def _epoch_key(self, rules: dict, now: datetime | None = None) -> str:
        """最近一次活跃时段开始的时刻（今天的开始时间还没到，就是昨天的）。"""
        from datetime import time as dtime

        now = now or datetime.now()
        start = datetime.combine(now.date(), parse_hhmm(rules.get("active_start", "09:00"), dtime(9, 0)))
        if start > now:
            start -= timedelta(days=1)
        return start.strftime("%Y-%m-%d %H:%M")

    def _drain_inbox(self) -> bool:
        """刚点了“开始刷视频”、或者活跃时段刚开始：先把评论区里找她的（@ 她、回复她的评论、评论她的作品）
        和私信全部看完、回完，再去刷视频。以前收到、还没回的也回（不再丢下启动之前的消息）。
        回的时候又来了新消息，就再看一轮，直到没有新的为止。返回 False：中途被叫停了，下次接着回。"""
        self.activity = "inbox"
        total, rounds = 0, 0
        for rounds in range(1, DRAIN_ROUNDS + 1):
            if not self._want_run:
                return False
            n = self._process_inbox(deep=True)
            total += n
            if not self._want_run:
                return False
            if n == 0:
                break
        now = time.time()
        self._next_at, self._next_dm = now + NOTICE_GAP, now + DM_GAP
        self._dm_flag = self._notice_flag = False
        left = self.db.query("SELECT COUNT(*) AS n FROM messages WHERE status='pending'")[0]["n"]
        log.info("攒下的消息回完了：%d 轮，处理了 %d 条，待你处理 %d 条", rounds, total, left)
        self.db.add_event("info", "消息都看完了，开始刷视频",
                          (f"这次看了 {total} 条找她的评论和私信" if total else "没有要回的评论和私信")
                          + (f"，有 {left} 条放在“待你处理”里" if left else ""))
        self.activity = "watch"
        return True

    def _owner_names(self, rules: dict) -> set:
        return {o["name"] for o in self.db.owners(enabled_only=True)} if rules.get("owner_enabled", True) else set()

    def _process_inbox(self, stop_on_pause: bool = True, at: bool = True, dm: bool = True,
                       owners_only: bool = False, deep: bool = False) -> int:
        """看评论区的消息（@ 她、回复她的评论、评论她的作品）和私信，一条条看懂、回复。返回这次处理了几条。
        运行中被叫停时（stop_on_pause），处理完当前这一条就不再继续。

        owners_only：只看主人的私信（活跃时段以外用）。
        deep：把攒下的都翻出来（通知往下翻、私信会话列表里“对方说了话、她还没回”的都点开）。"""
        rules = self.db.rules()
        b = self._ensure_browser()
        inbox = Inbox(b)
        inbox.my_names = (str(self.db.persona().get("name") or "").strip(),)   # 认“引用”的是不是她说的话
        halted = (lambda: stop_on_pause and not self._want_run)
        self._set("running" if self._want_run else "idle",
                  "去看看主人有没有找她" if owners_only else ("先把攒下的评论和私信回完" if deep else "去看看有没有人找她"))
        done = 0
        if at and not owners_only and not halted():
            self.activity = "inbox"
            found = inbox.fetch_mentions(limit=200 if deep else 40, is_known=self.db.message_handled,
                                         kinds=NOTICE_KINDS, mine=self.db.my_comment_texts(), deep=deep)
            self._log_notice_check(inbox, NOTICE_KINDS, found)
            for group in group_notices(found):
                if halted():
                    return done
                # 同一个人在同一个视频下连着说的几句：一起看，回在最新那条下面
                claimed = [(o, m) for o, m in ((o, self.db.claim_message(
                    kind=o["kind"], ext_id=o["ext_id"], user_name=o["user"], content=o["text"],
                    video_url=o.get("video_url", ""))) for o in group) if m]
                if not claimed:
                    continue
                (it, mid), rest = claimed[0], claimed[1:]
                others = [m for _, m in rest]
                if rest:
                    it["earlier"] = [o["text"] for o, _ in reversed(rest)]     # 按时间从早到晚
                self._safe_handle(inbox, mid, it, it["kind"], rules)
                self._settle_merged(mid, others)
                done += 1 + len(others)
                self._run_urgent()
        only = self._owner_names(rules) if owners_only else None
        if dm and (only is None or only) and not halted():
            self.activity = "inbox"
            seen: list[tuple[str, str]] = []             # 这一轮点开、读到了的每个人，后来怎么样了
            try:
                for it in inbox.fetch_unread_dms(limit=30 if deep else 10,
                                                 need_profile=lambda u: self._need_profile(u, rules), only=only,
                                                 scan_all=deep, is_done=self._dm_done, watch=self._dm_watch(rules)):
                    if it.get("profile"):
                        self.db.save_dm_user(it["user"], it["profile"], checked="follow_btn" in it["profile"])
                    if halted():
                        return done
                    mid = self.db.claim_message(kind="dm", ext_id=it["ext_id"], user_name=it["user"],
                                                content=it["text"])
                    if not mid:
                        old = self.db.query("SELECT status, reason FROM messages WHERE ext_id=?", (it["ext_id"],))
                        seen.append((it["user"], "这批话以前处理过了（%s），不再回" % (
                            f"{old[0]['status']}：{(old[0]['reason'] or '')[:30]}" if old else "记录里有")))
                        continue
                    self._safe_handle(inbox, mid, it, "dm", rules)
                    st = self._msg_status(mid)
                    seen.append((it["user"], {"replied": "回了", "uncertain": "回了（没法确认发出去没有）"}.get(
                        st, f"没回（{st}：{self._msg_reason(mid)[:40]}）")))
                    if st in ("replied", "uncertain"):
                        self.db.settle_dm_leftovers(it["user"], mid)
                    done += 1
                    self._dm_catch_up(inbox, mid, it, rules, halted)
                    self._run_urgent()
            finally:
                self._log_dm_check(inbox, seen)
                with cancel.shield():  # 叫停了也先把私信面板关掉，免得盖住页面
                    try:
                        inbox.close_dm_panel()
                    except Exception:  # noqa: BLE001
                        pass
                self.activity = "watch" if self._want_run else ""
        return done

    DM_WATCH_HOURS = 1      # 这么久以内私信聊过的人：没有未读红点也看一眼会话列表上 TA 的最后一句

    def _dm_watch(self, rules: dict) -> set:
        """没有未读红点也要看一眼的会话：主人，和最近几个小时私信聊过的人
        （只看会话列表上的最后一句，不是她回的那句才点开，不多花时间）。"""
        since = (datetime.now() - timedelta(hours=self.DM_WATCH_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
        try:
            recent = {r["user_name"] for r in self.db.query(
                "SELECT DISTINCT user_name FROM dm_history WHERE ts >= ?", (since,))}
        except Exception:  # noqa: BLE001
            recent = set()
        return (self._owner_names(rules) | recent) - {""}

    def _log_dm_check(self, inbox: Inbox, seen: list[tuple[str, str]]) -> None:
        """每次看完私信都在日志里记一笔（和“看了互动消息”一样）：看了几个会话、哪些有红点、
        哪些没红点但看了一眼、每个人最后怎么样了——以前回私信不留日志，没回也看不出为什么。"""
        st = getattr(inbox, "last_dm_check", None)
        if not isinstance(st, dict) or not st:
            return
        if st.get("opened") is False:
            log.warning("看私信：私信面板没打开，这次没看成")
            return
        parts = [f"会话 {st.get('convs', 0)} 个"]
        if st.get("unread"):
            parts.append("有未读：" + "、".join(st["unread"][:8]))
        if st.get("watched"):
            parts.append("没红点但最后一句不是她回的：" + "、".join(st["watched"][:8]))
        detail = [f"{u} {why}" for u, why in (st.get("skipped") or [])] + [f"{u} {why}" for u, why in seen]
        if detail:
            parts.append("；".join(detail[:10]))
        log.info("看了私信：%s", "，".join(parts))

    def _msg_reason(self, mid: int) -> str:
        row = self.db.query("SELECT reason FROM messages WHERE id=?", (mid,))
        return (row[0]["reason"] or "") if row else ""

    def _msg_status(self, mid: int) -> str:
        row = self.db.query("SELECT status FROM messages WHERE id=?", (mid,))
        return row[0]["status"] if row else ""

    def _settle_merged(self, mid: int, others: list[int]) -> None:
        """同一个人在同一个视频下连着说的几条，合在一起回在最新那条下面：前面那几条跟着这条的结果走。"""
        if not others:
            return
        row = self.db.query("SELECT status, reason FROM messages WHERE id=?", (mid,))
        if not row:
            return
        st, why = row[0]["status"], row[0]["reason"] or ""
        for o in others:
            if st in ("replied", "uncertain", "manual"):
                self.db.update_message(o, status="merged", reason="TA 在同一个视频下连着说了几句，她看完一起回了",
                                       handled_at=now_str())
            else:
                self.db.update_message(o, status=st, reason=why, handled_at=now_str())

    def _dm_done(self, user: str, preview: str) -> bool:
        """私信会话列表里这一行：记录里最后一句是她说的，而且列表上显示的最后一句就是这句——不用点开。"""
        last = self.db.dm_history(user, 1)
        if not last or last[-1]["role"] != "me":
            return False
        content = last[-1]["content"] or ""
        pv = re.sub(r"\s+", "", preview or "")
        if content.startswith("[发了一张表情包"):            # 她最后发的是表情包：列表上显示成“[表情包]”这类
            return "表情" in pv
        key = re.sub(r"\s+", "", content)[:8]
        return bool(key) and key in pv

    def _dm_history(self, user: str, it: dict) -> list[dict]:
        """回私信时给模型看的聊天记录：记下来的 + 聊天窗口里现在能看到的（她不在的时候 TA 发的、
        你在手机上替她回的，记录里没有）。这次要回的那几句不在里面（单独列给模型）。"""
        db_hist = self.db.dm_history(user, 24)
        page = [h for h in (it.get("before") or []) if h.get("content")]
        if not page:
            return db_hist
        blob = re.sub(r"\s+", "", "".join(h["content"] for h in page))
        older = [h for h in db_hist if re.sub(r"\s+", "", h["content"] or "")[:20] not in blob]
        return (older[-14:] + page)[-24:]

    def _dm_catch_up(self, inbox: Inbox, mid: int, it: dict, rules: dict, halted=lambda: False) -> None:
        """回完一个人的私信，趁聊天还开着，看看 TA 有没有哪几句话还没回应到（她打字时新发来的、
        她回完以后马上又发的），有就接着回。最多接 3 轮，免得两边一直你一句我一句停不下来。"""
        answered = list(it.get("lines") or [it.get("text") or ""])
        for _ in range(3):
            if halted():
                return
            row = self.db.query("SELECT status, reply FROM messages WHERE id=?", (mid,))
            if not row or row[0]["status"] != "replied" or not row[0]["reply"]:
                return                               # 这一批没回出去（待你处理 / 失败 / 到上限）：不接着往下
            try:
                missed = inbox.dm_missed(it["user"], answered, row[0]["reply"])
            except Exception:  # noqa: BLE001
                log.warning("检查有没有漏回的私信出错", exc_info=True)
                return
            if not missed:
                return
            text = "\n".join(missed)[-400:]
            ext = "dm_" + _hid(it["user"], str(row[0]["reply"])[:60], text, today_str())
            if self.db.message_exists(ext):
                return
            log.info("%s 还有 %d 句话她没回应到（她回复的时候新发来的），接着回", it["user"], len(missed))
            pics = getattr(inbox, "missed_images", None)
            try:                                     # 聊天还开着：再读一次对方的主页 id（主人的命令要认准人）
                peer = inbox.peer_uid(it["user"])
            except Exception:  # noqa: BLE001
                peer = ""
            it = {"ext_id": ext, "user": it["user"], "text": text, "lines": missed, "shares": [], "need_share": False,
                  "profile": {}, "images": pics if isinstance(pics, list) and "[发了一张表情包" in text else [],
                  "peer_uid": peer if isinstance(peer, str) else "", "name_dup": bool(it.get("name_dup"))}
            answered = list(missed)
            mid = self.db.add_message(kind="dm", ext_id=ext, user_name=it["user"], content=text, status="pending",
                                      reason="处理中")
            self._safe_handle(inbox, mid, it, "dm", rules)
        # 接了 3 轮还在你一句我一句：先停一下去干别的。TA 之后再发的，聊天窗口开着时抖音直接算已读、不出红点，
        # 下一轮看私信时按“会话列表上的最后一句不是她回的”找回来（见 _dm_watch）
        log.info("%s 聊得很快，接着回了 3 轮先停一下，后面的话下一轮看私信时再回", it["user"])

    def _thread_context(self, inbox: Inbox, it: dict) -> list[dict]:
        """对方那条评论所在的那一串评论，标出哪几句是她自己说的。读不到返回空。"""
        try:
            rows = inbox.thread_context(it)
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(rows, list):
            return []
        mine = {re.sub(r"\s+", "", t) for t in self.db.my_comment_texts()}
        their = re.sub(r"\s+", "", it.get("text") or "")
        out = []
        it["image"] = None
        for r in rows:
            sq = re.sub(r"\s+", "", r["text"])
            if r.get("target") or (their and sq == their and r["user"] == it.get("user")):
                if r.get("img"):                     # 对方这条评论里配了表情包 / 图：下载下来给模型看
                    it["image"] = inbox.fetch_image(r["img"])
                    log.info("%s 的评论里有一张图，%s", it.get("user"), "看到了" if it["image"] else "没能打开")
                continue                             # 对方这条本身不重复列
            out.append({"user": r["user"], "text": r["text"], "me": sq in mine})
        return out

    def _log_notice_check(self, inbox: Inbox, kinds: tuple, found: list[dict]) -> None:
        """每次看完“互动消息”都在日志里记一笔，没有新消息也记；通知面板打不开的，一小时在动态里提醒一次。"""
        n_at = sum(1 for it in found if it.get("kind") == "at")
        n_rp = sum(1 for it in found if it.get("kind") == "reply")
        stats = getattr(inbox, "last_notice", None) or {}
        if stats.get("opened") and not stats.get("rows") and not getattr(inbox, "_notice_blank", False):
            log.warning("看互动消息：通知面板打开了，但没读到一条通知（如果右上角“通知”有红点，说明没读出来）")
        if stats.get("opened") is False:
            log.warning("看互动消息：通知面板没打开，这次没看成（@ 和“接着聊”都会受影响）")
            now = time.time()
            if now - self._notice_fail_warned >= 3600:
                self._notice_fail_warned = now
                self.db.add_event("error", "通知面板打不开",
                                  "去看 @ 和评论回复时没能打开“通知”面板，这段时间回复 @、接着聊评论都没法进行；"
                                  "多次出现请把 data\\debug 里的文件发给开发者")
            return
        n_wk = sum(1 for it in found if it.get("kind") == "work")
        names = {"at": "@ 她", "reply": "回复她评论", "work": "评论她的作品"}
        log.info("看了互动消息（%s）：通知 %s 条，其中 @ 她 %s 条、回复她评论 %s 条、评论她作品 %s 条；"
                 "要回的：@ %d 条、回复 %d 条、作品评论 %d 条",
                 " + ".join(names.get(k, k) for k in kinds), stats.get("rows", "?"), stats.get("at", "?"),
                 stats.get("reply", "?"), stats.get("work", "?"), n_at, n_rp, n_wk)
        self.notice_checked = {"at": now_str(), "new_at": n_at, "new_reply": n_rp, "new_work": n_wk}

    def _with_knowledge(self, memory: str, text: str, video: dict | None = None) -> str:
        """回复时，把她自己平时学到的、和这次聊的事相关的几条也带上。"""
        about = f"{text} {(video or {}).get('desc') or ''}"
        know = self.learner.context(about)
        return (f"{memory}\n\n{know}" if memory else know) if know else memory

    def _owner_feedback(self, owner: dict, text: str, way: str = "", video_url: str = "") -> None:
        """主人说话了：如果是在回应她之前分享的视频，记下 TA 喜不喜欢，慢慢了解 TA 的口味。"""
        waiting = self.taste.waiting(owner["name"], way)
        if video_url:
            waiting = [w for w in waiting if w["video_url"] == video_url] or waiting[:0]
        if not waiting:
            return
        try:
            res = self.brain.owner_reaction(owner, waiting, text)
        except (LLMError, BudgetExceeded):
            return
        if res.get("share") and res.get("reaction"):
            self.taste.react(waiting[res["share"] - 1]["id"], res["reaction"], text[:80])

    def _comment_past(self, user: str, it: dict, mid: int) -> list[str]:
        """这个人以前在评论区找过她的几次（同一个视频下的在前），给模型对照着看，回复要接得上。"""
        out = []
        for r in self.db.comment_history(user, it.get("video_url") or "", exclude=mid):
            where = ("这个视频下" if it.get("video_url") and r["video_url"] == it.get("video_url")
                     else f"在《{(r['video_title'] or '另一个视频')[:20]}》下")
            line = f"{r['created_at'][5:16]} {where}，TA 说：“{(r['content'] or '')[:60]}”"
            if r["reply"]:
                line += f"，你回：“{r['reply'][:60]}”"
            out.append(line)
        return out

    def _skip_gone(self, mid: int, user: str) -> None:
        """原视频已经被删除 / 设为不可见：视频打不开，评论区也没了，没法回复（你手动也回复不了），直接跳过。"""
        reason = "原视频已被删除或设为不可见，打不开视频，没法在评论区回复，已跳过"
        self.db.update_message(mid, status="skipped_gone", reason=reason, handled_at=now_str())
        self.db.add_event("skip", f"没有回复 @{user}", reason)

    def _hold(self, mid: int, reason: str, reply: str = "", status: str | None = None, rules: dict | None = None):
        if status is None:
            status = "pending" if (rules or {}).get("on_hit", "pending") == "pending" else "skipped_sensitive"
        self.db.update_message(mid, status=status, reason=reason, reply=reply, handled_at=now_str())
        self.db.add_event("skip", "没有回复" if status != "pending" else "放进待你处理", reason)

    def _safe_handle(self, inbox: Inbox, mid: int, it: dict, kind: str, rules: dict) -> None:
        """处理一条消息；中途出了错，这条消息标成“失败”并写明原因（不会一直停在“处理中”），
        然后把浏览器带回推荐页，接着干别的。验证码照常往上交给统一处理。"""
        try:
            self._handle_incoming(inbox, mid, it, kind, rules)
        except OperationCancelled:
            self.db.update_message(mid, status="pending", handled_at=now_str(),
                                   reason="你让她休息了，这条还没回（可以手动回复）")
            raise
        except CaptchaDetected:
            self.db.update_message(mid, status="pending", reason="遇到验证，等验证完再处理")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("处理 %s 的消息出错", it.get("user"))
            self.db.update_message(mid, status="failed", reason=f"处理时出错：{str(e).splitlines()[0][:150]}",
                                   handled_at=now_str())
            try:
                Feed(inbox.b).open()
            except Exception:  # noqa: BLE001
                pass

    def _link_person(self, it: dict) -> None:
        """记下这个名字对应的主页 id：同一个人在私信列表、评论区、通知里的名字写法不一样时，靠它认成一个人
        （私信和评论区的上下文才通得起来）。"""
        try:
            uid = it.get("peer_uid") or uid_of(it.get("user_url") or "")
            if uid and it.get("user"):
                self.db.link_person(it["user"], uid)
        except Exception:  # noqa: BLE001
            log.debug("记主页 id 失败", exc_info=True)

    def _handle_incoming(self, inbox: Inbox, mid: int, it: dict, kind: str, rules: dict) -> None:
        user, text = it["user"], it["text"]
        self._link_person(it)
        if self.db.is_blocked(user):
            self.db.update_message(mid, status="blocked", reason="在不回复名单里", handled_at=now_str())
            return
        history: list[dict] = []
        if kind == "dm":
            history = self._dm_history(user, it)          # 先取聊天记录（这次要回的几句单独列给模型）
            self.db.add_dm(user, "them", text)
        owner = self.db.owner(user) if kind == "dm" and rules.get("owner_enabled", True) else None
        if kind == "dm" and not owner:
            try:                                       # TA 回的是她替主人问的话：转告主人（只转一次）
                self.commands.after_reply(inbox, user, text)
            except Exception:  # noqa: BLE001
                log.warning("把回复转告主人时出错", exc_info=True)
        if kind == "dm" and owner and self.commands.wanted(owner, it, rules):
            # 主人让她去做事：自己规划、一步步做完、回报（只是聊天的话 handle 返回 False，照平时那样回）
            if self.commands.handle(inbox, mid, it, owner, history, rules):
                self.mood.event("dm", user=user)
                return
        # 关键字自动回复：命中你设的关键字，直接发你写好的话，不走模型
        canned = safety.auto_reply(text, rules)
        if canned:
            self._quick_reply(inbox, mid, it, kind, canned, "命中关键字，自动回复")
            return
        hit = None if owner else safety.keyword_hit(text, rules)
        if hit and hit.startswith("屏蔽词"):
            self.mood.event("hostile")
            self._hold(mid, f"命中{hit}", rules=rules)
            return
        if hit and rules.get("sensitive_guard", True):
            # 敏感话题开关开着：不聊这个话题，直接告诉对方主要原因
            self._quick_reply(inbox, mid, it, kind, safety.sensitive_reply(hit), f"涉及敏感话题「{hit}」，直接说明了原因")
            return
        if kind == "reply":
            self._handle_thread(inbox, mid, it, rules)
            return
        if kind == "dm":
            self.mood.event("dm_share" if it.get("shares") else "dm", user=user)
        budget_key = "replies_dm" if kind == "dm" else "replies_at"     # 只用来统计今天回了多少，不设上限
        if not owner and self.db.replies_to_user_today(user) >= PER_USER_DAY_GUARD:
            self._hold(mid, GUARD_REASON, status="pending")
            return

        if kind in ("at", "work"):
            if it.get("video_gone"):
                self._skip_gone(mid, user)
                return
            if not it.get("video_url"):
                # 认不出是哪个视频：不能拿推荐页上正在放的视频来回答，放进“待你处理”
                self._hold(mid, "认不出对方是在哪个视频下 @ 的她", status="pending")
                return
            try:
                video = inbox.open_mention_video(it)
            except FeedReadError as e:
                self.db.update_message(mid, status="failed", reason=str(e), handled_at=now_str())
                return
            self.db.update_message(mid, video_title=video.get("desc", "")[:100])
            if rules.get("listen_enabled", True):
                video["speech"] = self._listen(inbox.b, video, rules)     # 回 @ 时也听一听视频里说了什么
            frames = (inbox.feed.capture_frames(int(rules.get("frames_per_video") or 4), 6)
                      if self.brain.router.task_can_see("vision") else [])
            ctx = self._thread_context(inbox, it)
            draft = None
            cmd_owner = self.db.owner(user) if rules.get("owner_enabled", True) else None
            if cmd_owner and self.commands.wanted(cmd_owner, it, rules):
                # 主人在评论区 @ 她让她做事（比如“@一下妈妈”）：规划、去做，回复里真的 @ 上要 @ 的人
                draft = self.commands.handle_comment(inbox, mid, it, cmd_owner, video, rules)
            if draft is None:
                draft = self.brain.reply_at(video, user, text, frames, memory=self._with_knowledge(
                                                self.memory.comment_context(user, text), text, video),
                                            context=ctx, image=it.get("image"), kind=kind,
                                            earlier=it.get("earlier") or [], past=self._comment_past(user, it, mid))
        else:
            video, frames = None, []
            if it.get("shares"):
                # 对方分享了视频：先在新标签页里看一遍（文案、作者、热评，能识图就截几帧），再回复
                url = it["shares"][0]["url"]
                self._set(self.state, f"去看 {user} 分享的视频")
                was_running = self._want_run

                def sleeper(sec: float) -> bool:
                    cancel.sleep(sec)
                    return self._want_run or not was_running  # 运行中被叫停就不看了

                def on_open(v: dict) -> None:
                    self.current = {"title": (v.get("desc") or "")[:40] or "（无文案）", "author": v.get("author", ""),
                                    "step": f"在看 {user} 分享的视频", "frames": []}

                def progress(cur: float, total: float) -> None:
                    self.current["step"] = f"在看 {user} 分享的视频 {_mmss(cur)} / {_mmss(total)}"

                try:
                    video, frames = inbox.watch_shared(
                        url, _int(rules.get("share_frames"), 6), self.brain.router.task_can_see("vision"),
                        max_s=max(0.5, float(rules.get("share_watch_minutes") or 3)) * 60,
                        sleep=sleeper, progress=progress, on_open=on_open)
                    self._show_frames(frames)
                    self.current["step"] = f"看完了 {user} 分享的视频，正在想怎么回"
                    self.db.update_message(mid, video_title=(video.get("desc") or "")[:100], video_url=url)
                except Exception as e:  # noqa: BLE001 —— 打不开就按看不到处理，坦白告诉对方
                    log.warning("没能打开 %s 分享的视频 %s：%s", user, url, str(e).splitlines()[0][:150])
                    self.db.update_message(mid, video_url=url)
                    video, frames = {"unreadable": True, "url": url}, []
            if video is None and it.get("need_share"):
                video = {"need_share": True}  # 光问视频、没发视频卡片：请对方重新发一次
            try:
                mem_ctx = self.memory.dm_context(user, text)
            except Exception:  # noqa: BLE001
                log.warning("读记忆失败", exc_info=True)
                mem_ctx = ""
            mem_ctx = self._with_knowledge(mem_ctx, text, video if isinstance(video, dict) else None)
            if owner:
                self._owner_feedback(owner, text)          # TA 是不是在说她之前分享的视频：喜不喜欢
            pics = [x for x in (inbox.fetch_image(u) for u in (it.get("images") or [])[-2:]) if x]
            if it.get("images"):
                log.info("%s 发来 %d 张表情包 / 图片，看到了 %d 张", user, len(it["images"]), len(pics))
            no_tools = bool(owner) and not self.commands.enabled_for(owner, rules) and looks_like_command(text)
            draft = self.brain.reply_dm(user, history, text, video=video, frames=frames, owner=owner, memory=mem_ctx,
                                        sticker=self._sticker_ok(rules, "dm"), images=pics, no_tools=no_tools)
        self.db.update_message(mid, cost=draft.cost, tokens=draft.tokens)
        if not draft.ok:
            self._hold(mid, draft.reason, reply=draft.text, rules=rules)
            return
        bus.emit("reply_dm" if kind == "dm" else "reply_at", user)
        many = getattr(draft, "parts", None)
        parts = [p for p in (many if isinstance(many, list) and many else [draft.text]) if p] if kind == "dm" \
            else [draft.text]
        if kind == "dm":
            sent, n_sent = self._send_dm_parts(inbox, it, parts)
            if n_sent and n_sent < len(parts):
                # 一一回复发到一半没发出去：发出去的几句算回过了，没发出去的写进原因
                parts = parts[:n_sent]
                draft.text = "\n".join(parts)
                sent = True
                log.warning("回 %s 的私信发到第 %d 句没发出去", user, n_sent + 1)
        else:
            at = getattr(draft, "mentions", None)
            sent = inbox.reply_mention(it, draft.text, mentions=at) if isinstance(at, list) and at \
                else inbox.reply_mention(it, draft.text)
        if sent:
            if owner:
                self._owner_mark(user)             # 刚和主人聊过：主动找 TA 的时间重新算
            self.budget.use(budget_key)
            self.db.update_message(mid, status="replied", reply=draft.text, reason="", handled_at=now_str())
            where = {"at": "评论区的 @", "work": "作品下的评论"}.get(kind, "私信")
            note = getattr(inbox, "last_reply_note", "") if kind != "dm" else ""
            want_at = getattr(draft, "mentions", None) if kind != "dm" else None
            if isinstance(want_at, list) and want_at:      # 主人让她 @ 的人：@ 上了谁、谁没 @ 上
                got = getattr(getattr(inbox, "feed", None), "last_mentioned", None)
                got = got if isinstance(got, list) else []
                miss = [x for x in want_at if x not in got]
                note = "；".join(x for x in ((note if isinstance(note, str) else ""),
                                             f"@ 上了 {'、'.join(got)}" if got else "",
                                             f"没 @ 上 {'、'.join(miss)}（候选里没找到 TA）" if miss else "") if x)
            self.db.add_event("reply_dm" if kind == "dm" else "reply_at", f"回复了 @{user} 的{where}",
                              f"“{text[:30]}” → “{draft.text[:40]}”" + (f"（{note}）" if isinstance(note, str) and note else ""),
                              ref=f"msg:{mid}")
            self._taste_talk(text, draft.text)
            self.learner.note("dm" if kind == "dm" else "comment",
                              (f"在《{((video or {}).get('desc') or '')[:30]}》的评论区，" if kind != "dm" else "私信里，")
                              + f"对方说：{text[:200]}\n我回：{draft.text[:150]}",
                              ref="私信" if kind == "dm" else ((video or {}).get("desc") or "")[:30])
            if kind == "dm":
                for p in parts:                            # 一一回复的几句分开记（聊天记录里也是分开的几条）
                    self.db.add_dm(user, "me", p)
                self._dm_sticker(inbox, user, getattr(draft, "sticker", None), rules)
                self.memory.remember_dm(user, text, draft.text, video if isinstance(video, dict) else None,
                                        owner=bool(owner))
                if owner:
                    self.db.owner_touch(user, closer=0.5)
                try:
                    self.memory.update_card(user)
                except (LLMError, BudgetExceeded):
                    pass
            else:
                self.memory.remember_at(user, "\n".join((it.get("earlier") or []) + [text]), draft.text, video,
                                        work=kind == "work")
        elif getattr(sent, "uncertain", False):
            # 点了发送但没法确认：可能已经发出去了，不自动重发（免得发两遍），请你去抖音里看一眼
            self.budget.use(budget_key)
            self.db.update_message(mid, status="uncertain", reply=draft.text, reason=UNCERTAIN_REASON,
                                   handled_at=now_str())
            self.db.add_event("info", f"回复 @{user} 可能已经发出去了", UNCERTAIN_REASON, ref=f"msg:{mid}")
        else:
            self.db.update_message(mid, status="failed", reply=draft.text, reason=_fail_reason(inbox),
                                   handled_at=now_str())
            inbox.b.dump(f"send_fail_{kind}")

    def _send_dm_parts(self, inbox: Inbox, it: dict, parts: list[str]):
        """一一回复：几句分开发，每发一句停一两秒（像真人打完一句再打下一句）。
        返回（最后一次发送的结果，发出去了几句）。"""
        last, n = False, 0
        for i, p in enumerate(parts):
            if i:
                cancel.sleep(random.uniform(1.0, 2.5))
            last = inbox.reply_dm(it, p)
            if not last:
                break
            n += 1
        return last, n

    def _quick_reply(self, inbox: Inbox, mid: int, it: dict, kind: str, text: str, why: str) -> None:
        """不走模型，直接发一句现成的话（关键字自动回复、敏感话题说明原因）。"""
        user = it["user"]
        key = {"dm": "replies_dm", "reply": "thread_replies"}.get(kind, "replies_at")   # 只统计，不设上限
        owner = kind == "dm" and user in self._owner_names(self.db.rules())
        if kind != "dm":
            if it.get("video_gone"):
                self._skip_gone(mid, user)
                return
            if not it.get("video_url"):
                self._hold(mid, "认不出对方是在哪个视频下找她的", status="pending")
                return
            try:
                inbox.open_mention_video(it, with_comments=False)
            except FeedReadError as e:
                self.db.update_message(mid, status="failed", reason=str(e), handled_at=now_str())
                return
        sent = inbox.reply_dm(it, text) if kind == "dm" else inbox.reply_mention(it, text)
        if sent or getattr(sent, "uncertain", False):
            if owner:
                self._owner_mark(user)
            self.budget.use(key)
            self.db.update_message(mid, status="replied" if sent else "uncertain", reply=text, reason=why,
                                   handled_at=now_str())
            if kind == "dm":
                self.db.add_dm(user, "me", text)
            self.db.add_event("reply_dm" if kind == "dm" else "reply_at", f"回复了 @{user}（{why}）",
                              f"“{it['text'][:30]}” → “{text[:40]}”", ref=f"msg:{mid}")
        else:
            self.db.update_message(mid, status="failed", reply=text, reason=_fail_reason(inbox),
                                   handled_at=now_str())

    # ---------- 有人回复她的评论：接着聊 ----------
    def _handle_thread(self, inbox: Inbox, mid: int, it: dict, rules: dict) -> None:
        user, text = it["user"], it["text"]
        mine = it.get("mine") or ""
        tid = "t_" + _hid(it.get("video_url") or "", mine or user)
        th = self.db.ensure_thread(tid, it.get("video_url") or "", mine, user)
        is_owner = rules.get("owner_enabled", True) and self.db.owner(user) is not None
        if not is_owner and self.db.thread_replies_today(user) >= THREAD_USER_DAY_GUARD:
            # 和这个人今天接着聊到底线了：不看视频、不调用模型，直接跳过（动态只在第一次碰到时记一条，免得刷屏）
            self.db.update_message(mid, status="skipped_limit", handled_at=now_str(),
                                   reason=THREAD_LIMIT_REASON.format(d=THREAD_USER_DAY_GUARD))
            capped = self.__dict__.setdefault("_thread_capped", set())
            if f"{today_str()}|{user}" not in capped:
                capped.add(f"{today_str()}|{user}")
                self.db.add_event("skip", f"和 @{user} 在评论区接着聊到上限了",
                                  f"今天和 TA 在评论区已经互聊了 {THREAD_USER_DAY_GUARD} 次，之后 TA 再回复就不回了（不花模型的钱）",
                                  ref=f"msg:{mid}")
            return
        if self.db.replies_to_user_today(user) >= PER_USER_DAY_GUARD:
            self._hold(mid, GUARD_REASON, status="pending")
            return
        if it.get("video_gone"):
            self._skip_gone(mid, user)
            return
        if not it.get("video_url"):
            self._hold(mid, "认不出对方是在哪个视频下回复她的", status="pending")
            return
        self.mood.event("thread_reply", user=user)
        try:
            video = inbox.open_mention_video(it)
        except FeedReadError as e:
            self.db.update_message(mid, status="failed", reason=str(e), handled_at=now_str())
            return
        self.db.update_message(mid, video_title=video.get("desc", "")[:100])
        history = self.db.thread_lines(tid)
        if not history and mine:
            history = [{"role": "me", "content": mine}]
        parent = self.db.reply_parent(mine)            # 她当初回复的那条评论（楼主）：这一串的开头
        owner = self.db.owner(user) if rules.get("owner_enabled", True) else None
        if owner:
            # 主人回复了她 @ TA 的那条评论：TA 来看了。看看 TA 喜不喜欢这个视频
            self._owner_feedback(owner, text, way="at", video_url=it.get("video_url") or "")
        draft = self.brain.reply_thread(video, user, text, history + [], False, parent=parent,
                                        memory=self._with_knowledge(self.memory.comment_context(user, text), text,
                                                                    video),
                                        context=self._thread_context(inbox, it), image=it.get("image"),
                                        earlier=it.get("earlier") or [], past=self._comment_past(user, it, mid))
        self.db.update_message(mid, cost=draft.cost, tokens=draft.tokens)
        if not draft.ok:
            self._hold(mid, draft.reason, reply=draft.text, rules=rules)
            return
        bus.emit("thread_reply", user)
        if parent:
            # 她那条评论本身是回复在别人评论下面的：对方的回复也在那一串里，直接去那一串找
            it["root_text"], it["root_user"] = parent["text"], parent["user_name"]
            if not it.get("root_cid") and str(parent["cid"]).isdigit():
                it["root_cid"] = str(parent["cid"])
        sent_t = inbox.reply_mention(it, draft.text)
        if sent_t:
            self.budget.use("thread_replies")
            for t in (it.get("earlier") or []):
                self.db.thread_add(tid, "them", t)
            self.db.thread_add(tid, "them", text)
            self.db.thread_add(tid, "me", draft.text)
            self.db.update_message(mid, status="replied", reply=draft.text, reason="", handled_at=now_str())
            self.db.add_event("thread_reply", f"接着和 @{user} 聊（这串第 {th['count'] + 1} 次）",
                              f"“{text[:30]}” → “{draft.text[:40]}”", ref=f"msg:{mid}")
            self.memory.remember_thread(tid, user, text, draft.text, video)
            self._taste_talk(text, draft.text)
            self.learner.note("comment", f"在《{(video.get('desc') or '')[:30]}》的评论区接着聊，对方说：{text[:200]}\n"
                                         f"我回：{draft.text[:150]}", ref=(video.get("desc") or "")[:30])
        elif getattr(sent_t, "uncertain", False):
            self.budget.use("thread_replies")
            self.db.update_message(mid, status="uncertain", reply=draft.text, reason=UNCERTAIN_REASON,
                                   handled_at=now_str())
        else:
            self.db.update_message(mid, status="failed", reply=draft.text, reason=_fail_reason(inbox),
                                   handled_at=now_str())

    # ---------- 互关好友 ----------
    def _cmd_fetch_friends(self) -> None:
        b = self._ensure_browser()
        if not b.is_logged_in():
            self.alert = "还没登录抖音，请先点“扫码登录”"
            return
        self.current = {"title": "", "author": "", "step": "去看看互关好友有哪些"}
        friends, err = fetch_mutual_friends(b)
        for f in friends:
            self.db.save_friend(f["uid"], f)
        n = sum(1 for f in friends if f["mutual"])
        if err:
            self.alert = f"读互关好友失败：{err}"
            self.db.add_event("error", "读互关好友失败", err[:200])
        else:
            self.db.add_event("info", "互关好友名单已更新", f"关注列表里读到 {len(friends)} 人，其中互关 {n} 人")
        self.db.set_json("friends_fetch", {"at": now_str(), "count": n, "error": err})
        if self._want_run:
            Feed(b).open()

    # ---------- 发作品 ----------
    def _maybe_publish_wish(self, rules: dict) -> None:
        """每天在设定的时间段里挑几个时间点，按心情想一想要不要发作品。想发就写好草稿，等你确认。"""
        if not rules.get("publish_enabled", True):
            return
        limit = _int((rules.get("limits") or {}).get("publishes"), 1)
        if limit <= 0:
            return
        today = today_str()
        plan = self.db.get_json("publish_plan", {}) or {}
        if plan.get("day") != today:
            plan = {"day": today, "slots": publish_slots(rules, limit)}
            self.db.set_json("publish_plan", plan)
        now = time.time()
        if not any(t <= now for t in plan.get("slots", [])):
            return
        plan["slots"] = [t for t in plan["slots"] if t > now]
        self.db.set_json("publish_plan", plan)
        if self.db.drafts_today() >= limit:
            return
        if any(d["status"] == "wish" for d in self.db.drafts(include_done=False)):
            return  # 还有一个想发的作品在等你确认：先不写新的
        mood = self.mood.current()
        if random.random() > self.mood.bias()["publish"]:
            self.db.add_event("info", "她想了想，这会儿不想发作品", f"心情：{mood['label']}")
            return
        try:
            self._make_draft(force=False)
        except LLMError as e:
            self.db.add_event("error", "写作品草稿失败", str(e)[:200])

    def _make_draft(self, force: bool, topic: str = "", form: str = "") -> int | None:
        """写一个作品草稿（画图、配音）。topic：主人在私信里让她写的主题；form：主人指定的形式（voice / cards）。"""
        rows = self.db.query("SELECT title FROM videos WHERE interest >= 7 AND title != '' ORDER BY id DESC LIMIT 12")
        mood = self.mood.current()
        rules = self.db.rules()
        form = draft_form(form or rules.get("post_form"))
        if form == "upload":
            form = "voice"                      # 她自己写的没有“你上传的”这种形式
        self.current = {**self.current, "step": "在想想要发点什么"}
        self._refresh_my_works()                # 先看看最近发的作品大家怎么说
        posts, unposted = self.db.recent_posts(), self.db.unposted_drafts()
        try:
            mem = self.memory.post_material(avoid=[f"{p['title']} {p['text']}" for p in posts])
        except Exception:  # noqa: BLE001 —— 记忆读不到不影响写作品
            mem = ""
        want_image = False
        if form == "cards" and rules.get("image_gen_auto", True):
            from ..publish import imagegen
            want_image = bool(imagegen.available(self.db, rules))
        d = self.brain.draft_post([r["title"] for r in rows], f"{mood['label']}（{mood['reason']}）", force=force,
                                  memory=mem, topic=topic, posts=posts, unposted=unposted, form=form, want_image=want_image)
        if not d["want"]:
            self.db.add_event("info", "她想了想，今天不发作品", d["why"] or f"心情：{mood['label']}")
            return None
        if form == "cards" and not d.get("cards") and (d.get("text") or "").strip():
            from ..publish.cards import cards_from_text
            d["cards"] = cards_from_text(d["text"])  # 她没分卡片：按正文自动分
        did = self.db.add_draft(status="wish", kind="image", title=d["title"], text=d["text"], tags=d["tags"],
                                cards=d["cards"], image_prompt=d["image_prompt"], mood=mood["key"],
                                reason=(f"主人让我写：{topic[:60]}" if topic else d["why"]),
                                source="manual" if (force or topic) else "auto", form=form)
        media = []
        try:
            media += render_voice_image(self.db, did, d) if form == "voice" else render_cards_for(self.db, did, d)
        except Exception as e:  # noqa: BLE001 —— 图画不出来也不影响草稿
            log.warning("画作品的图片失败：%s", e, exc_info=True)
        if form == "cards" and d["image_prompt"] and rules.get("image_gen_auto", True):
            from ..publish import imagegen
            if imagegen.available(self.db, rules):
                try:
                    raw, cost = imagegen.generate(self.db, rules, d["image_prompt"])
                    name = f"ai_{int(time.time())}.png"
                    (DRAFTS_DIR / str(did)).mkdir(parents=True, exist_ok=True)
                    (DRAFTS_DIR / str(did) / name).write_bytes(raw)
                    if cost:
                        self.budget.use("cost", cost)
                    media.insert(1 if media else 0, {"name": name, "kind": "image", "source": "ai"})
                except OperationCancelled:
                    self.db.update_draft(did, media=media)  # 生图半路被叫停：草稿和文字卡片留着
                    raise
                except Exception as e:  # noqa: BLE001
                    log.warning("AI 生图失败：%s", e)
                    self.db.add_event("error", "AI 生图失败（草稿里还有文字卡片）", str(e)[:200])
        self.db.update_draft(did, media=media)
        if form == "voice" or rules.get("voice_auto"):     # 配音图片视频：每次都自动配好音；图文卡片：看设置
            from ..publish import voice as tts
            if not tts.available(self.db, rules):
                if form == "voice":
                    self.db.add_event("info", "作品还没配音", "还没设置她的声音（规则与预算 → 她的声音），这次先只做了图片；"
                                      "设置好以后在作品里点“重新生成图片并配音”", ref=f"draft:{did}")
            else:
                try:
                    self.current = {**self.current, "step": "给作品配音"}
                    cost = narrate_draft(self.db, did)
                    if cost:
                        self.budget.use("cost", cost)
                except OperationCancelled:
                    raise
                except Exception as e:  # noqa: BLE001 —— 配音失败不影响草稿（还是图文）
                    log.warning("作品配音失败：%s", e)
                    self.db.add_event("error", "作品配音失败（草稿里图片还在，可以手动再配）", str(e)[:200])
        name = d["title"] or (d.get("text") or "")[:16]       # 她这次没写标题：用正文开头
        self.db.add_event("publish_wish", "您的小可爱想发作品", f"《{name}》{d['why']}", ref=f"draft:{did}")
        bus.emit("publish_wish", name)
        return did

    CONFIRM_AFTER_S = 120   # 发布后多久去她主页确认作品在不在
    CONFIRM_EVERY_S = 300   # 没看到的话隔多久再看
    CONFIRM_GIVE_UP_S = 1800  # 这么久还没看到：告诉你“可能没发出去”

    def _confirm_posts(self) -> None:
        """发布后去她主页确认作品真的在（2026-11-21：创作者中心说发布成功、主页上却没有）。
        看到了在动态里记一笔；30 分钟还没看到就提醒你去抖音 App 里看一眼（可能还在审核，也可能没发出去）。"""
        pend = self.db.get_json("confirm_posts", {}) or {}
        if not pend or not self.browser:
            return
        now = time.time()
        oldest = min(float(t) for t in pend.values())
        if now - oldest < self.CONFIRM_AFTER_S or now - float(self.db.get_json("confirm_posts_at", 0) or 0) < self.CONFIRM_EVERY_S:
            return
        self.db.set_json("confirm_posts_at", now)
        from ..douyin.works import fetch_my_works
        try:
            works = fetch_my_works(self.browser)
        except Exception as e:  # noqa: BLE001 —— 读不到下次再看（要验证、被叫停这些照常往外传）
            log.info("去主页确认作品失败：%s", e)
            works = []
        if works:
            self.db.save_my_works(works)
            self.db.set_json("my_works_at", now)
        left = {}
        for did, at in pend.items():
            d = self.db.draft(int(did))
            if not d:
                continue
            name = d["title"] or (d.get("text") or "")[:16]
            if self.db.query("SELECT 1 FROM my_works WHERE draft_id=? LIMIT 1", (int(did),)):
                self.db.add_event("info", f"作品《{name[:20]}》已经在她主页上了", "", ref=f"draft:{did}")
                log.info("发布后确认：《%s》已经在她主页上了", name[:20])
            elif now - float(at) >= self.CONFIRM_GIVE_UP_S:
                why = ("创作者中心说发布成功，但过了 30 分钟她主页上还没有这个作品：可能还在审核，也可能没发出去。"
                       "去抖音 App 的作品列表看一眼；没有的话再点一次“确认发布”")
                self.db.update_draft(int(did), error=why)
                self.db.add_event("error", f"她主页上还没看到《{name[:20]}》", why, ref=f"draft:{did}")
                self.alert = f"作品《{name[:12]}》发布后她主页上还没看到，去抖音 App 里确认一下"
                log.warning("发布后确认：30 分钟了她主页上还没有《%s》", name[:20])
            else:
                left[did] = at
        self.db.set_json("confirm_posts", left)

    WORKS_EVERY_H = 6       # 隔多久去自己主页看一次作品的点赞、评论

    def _refresh_my_works(self, force: bool = False) -> int:
        """去自己主页看看最近发的作品现在怎么样了（点赞、评论、收藏），写作品时她会参考大家的反应。
        最近一个月没发过作品、浏览器没开着、刚看过，就不去。返回读到几个作品。"""
        if not self.browser or (not force and not self.db.recent_posts(days=30, limit=1)):
            return 0
        last = self.db.get_json("my_works_at", 0) or 0
        if not force and time.time() - float(last) < self.WORKS_EVERY_H * 3600:
            return 0
        self.db.set_json("my_works_at", time.time())
        from ..douyin.works import fetch_my_works
        try:
            works = fetch_my_works(self.browser)
        except Exception as e:  # noqa: BLE001 —— 读不到不影响写作品（要验证、被叫停这些照常往外传）
            log.info("看自己作品的数据失败：%s", e)
            return 0
        if works:
            n = self.db.save_my_works(works)
            log.info("看了自己最近的 %d 个作品（认出 %d 个是她发的）", len(works), n)
        return len(works)

    def _rerender_draft(self, did: int) -> None:
        """草稿的文字改了（主人在私信里让她改）：按现在的文字重画图片，配音图片视频重新配音。"""
        from ..publish import voice as tts
        from ..publish.cards import cards_from_text
        d = self.db.draft(did)
        if not d:
            return
        rules = self.db.rules()
        form = draft_form(d.get("form"))
        media = drop_source(did, d["media"], ("card", "voice", "voicecard"))
        self.db.update_draft(did, media=media)
        d = self.db.draft(did)
        try:
            if form == "cards" and not d.get("cards") and (d.get("text") or "").strip():
                d["cards"] = cards_from_text(d["text"])
                self.db.update_draft(did, cards=d["cards"])
            if form == "voice" or rules.get("voice_auto"):
                if tts.available(self.db, rules) and len(tts.speech_text(d["title"], d["text"])) >= 4:
                    if form != "voice":
                        self.db.update_draft(did, media=render_cards_for(self.db, did, d) + media)
                    cost = narrate_draft(self.db, did)
                    if cost:
                        self.budget.use("cost", cost)
                    return
            new = render_voice_image(self.db, did, d) if form == "voice" else render_cards_for(self.db, did, d)
            self.db.update_draft(did, media=new + media)
        except OperationCancelled:
            raise
        except Exception as e:  # noqa: BLE001 —— 画不出来不影响草稿的文字
            log.warning("重画草稿的图片 / 配音失败：%s", e, exc_info=True)

    def _cmd_draft_now(self) -> None:
        try:
            did = self._make_draft(force=True)
        except LLMError as e:
            self.alert = f"写草稿失败：{e}"
            return
        if did is None:
            self.alert = "她这次没写出草稿，再试一次吧"

    def _cmd_publish(self, did: int) -> None:
        d = self.db.draft(did)
        # 界面点“确认发布”时已经先把状态改成了 publishing（让你马上看到“发布中”），所以这里也要接受它
        if not d or d["status"] not in ("wish", "failed", "publishing"):
            return
        rules = self.db.rules()
        name = d["title"] or (d.get("text") or "")[:16]     # 没写标题的作品：用正文开头称呼它
        if self.db.usage().get("publishes", 0) >= _int(rules["limits"].get("publishes"), 1):
            self.db.update_draft(did, status="failed", error="今天发布作品的数量已经到上限了（在“规则与预算”里可以调）")
            return
        files, kinds = [], set()
        voiced = [m for m in d["media"] if m.get("source") == "voice" and media_path(did, m).exists()]
        for m in (voiced[-1:] or d["media"]):      # 有配音视频：只发这个视频（文字卡片已经在视频里了）
            f = media_path(did, m)
            if f.exists():
                files.append(f)
                kinds.add(m.get("kind"))
        if not files:
            self.db.update_draft(did, status="failed", error="还没有图片或视频，先添加一些再发布")
            return
        if "video" in kinds and (len(files) > 1 or len(kinds) > 1):
            self.db.update_draft(did, status="failed", error="视频作品只能放 1 个视频，不能和图片混在一起")
            return
        d["kind"] = "video" if "video" in kinds else "image"
        ok, reason, _ = self.brain.check_post(d["title"], d["text"])
        if not ok:
            self.db.update_draft(did, status="failed", error=f"文字没通过检查：{reason}，改一改再发吧")
            return
        self.db.update_draft(did, status="publishing", kind=d["kind"], error="")
        prev = self.activity
        self.activity = "publish"
        bus.emit("publishing", d["title"])

        def step(text: str) -> None:
            self.current = {"title": d["title"], "author": "", "step": f"发作品：{text}", "frames": []}

        try:
            b = self._ensure_browser()
            msg = publish(b, d, files, step=step, wait_login=self._wait_creator_login,
                          wait_verify=self._wait_publish_verify)
        except OperationCancelled:
            self.db.update_draft(did, status="failed",
                                 error="你让她休息了，发布中途停下，作品没有发出去（可以再点一次“确认发布”）")
            self.db.add_event("pause", "发布中途停下", f"《{name[:20]}》没有发出去", ref=f"draft:{did}")
            raise
        except (PublishError, BrowserError) as e:
            self.db.update_draft(did, status="failed", error=str(e)[:300])
            self.db.add_event("error", "作品发布失败", str(e)[:200], ref=f"draft:{did}")
            self.alert = f"作品《{name[:12]}》没有发布成功：{str(e)[:120]}"
            return
        finally:
            self.activity = prev
        self.budget.use("publishes")
        self.db.update_draft(did, status="published", published_at=now_str(), error="")
        self.db.add_event("published", f"发布了作品《{name[:20]}》", msg + "（已声明：内容由AI生成）"
                          + "，过几分钟去她主页确认一下", ref=f"draft:{did}")
        pend = self.db.get_json("confirm_posts", {}) or {}
        self.db.set_json("confirm_posts", {**pend, str(did): time.time()})
        self.mood.event("publish_ok")
        self.memory.remember("post", f"我发了一个作品《{name[:30]}》：{(d.get('text') or '')[:50]}", importance=4,
                             dedupe=f"post:{did}")
        bus.emit("published", d["title"])
        if self._want_run and self.browser:
            Feed(self.browser).open()

    def _wait_creator_login(self) -> bool:
        self._raise_attention("creator_login")
        end = time.time() + 300
        ok = False
        while time.time() < end and not self._shutdown and self.browser:
            words = self.browser.sel.get("creator_login_words") or []
            if "login" not in (self.browser.page.url or "") and not any(
                    self.browser.page.get_by_text(w).first.is_visible(timeout=300) for w in words[:2]):
                ok = True
                break
            time.sleep(3)
        self._clear_attention()
        if self.browser:
            self.browser.hide_window()
        return ok

    def _wait_publish_verify(self, kind: str) -> bool:
        """发作品时抖音要求验证（多半是点“发布”后的“接收短信验证码”）：弹窗提醒、浏览器拿到最前面，
        等你在浏览器里验证完（最多 10 分钟）。验证完把浏览器藏回后台，返回 True，她接着等发布结果。"""
        self._raise_attention(kind, during_publish=True)
        started = time.time()
        gone = 0
        ok = False
        try:
            while time.time() - started < 600 and not self._shutdown and self.browser:
                if self.browser.verification_open():
                    gone = 0
                else:
                    gone += 1
                    if gone >= 2:
                        ok = True
                        break
                time.sleep(1.5)
        finally:
            self._clear_attention()
            if self.browser:
                with cancel.shield():
                    try:
                        self.browser.hide_window()
                    except Exception:  # noqa: BLE001
                        log.warning("验证后藏浏览器失败", exc_info=True)
        self.db.add_event("info" if ok else "error", "发作品时的验证已完成" if ok else "发作品时的验证没有完成",
                          f"等了 {int(time.time() - started)} 秒")
        return ok

    # ---------- 验证码 ----------
    def _raise_attention(self, kind: str, during_login: bool = False, during_publish: bool = False,
                         doing: str = "") -> None:
        """需要你亲自处理：把浏览器拿到屏幕上，软件窗口弹到前面并弹窗说明（不只是一行小字）。
        doing：她正在做的事（比如“替主人搜视频”），验证完她接着做这件事。"""
        self._attention_seq += 1
        if kind == "creator_login":
            title = "需要登录抖音创作者中心"
            body = ("发作品要用抖音创作者中心，它要求再登录一次。请在她的浏览器窗口里用抖音 App 扫码登录，"
                    "登录好后她会自动接着发布。")
        elif kind == "sms":
            title = "需要填写手机验证码"
            body = (("发作品时抖音要求验证你的手机号。" if during_publish else
                     f"她在{doing}，抖音要求验证你的手机号。" if doing else "抖音要求验证你的手机号。")
                    + "请到弹到最前面的浏览器窗口里点“获取验证码”，把手机收到的短信验证码填进去，再点“验证”。"
                    + ("填完就登录成功了。" if during_login else
                       "验证完成后浏览器会自动藏回后台，她接着把作品发出去。" if during_publish else
                       f"验证完成后浏览器会自动藏回后台，她接着{doing}。" if doing else
                       "验证完成后浏览器会自动藏回后台，她继续刷视频。") + "（软件不会、也不能替你填写验证码。）")
        else:
            title = "需要你完成一个验证"
            body = ((f"她在{doing}，" if doing else "")
                    + "抖音弹出了安全验证（比如拖动滑块、点选图片）。请到弹到最前面的浏览器窗口里手动完成，"
                    + ("完成后浏览器会自动藏回后台，她接着把作品发出去。" if during_publish else
                       f"完成后浏览器会自动藏回后台，她接着{doing}。" if doing else
                       "完成后浏览器会自动藏回后台，她继续刷视频。") + "（软件不会自动破解验证码。）")
        self.attention = {"id": self._attention_seq, "kind": kind, "title": title, "body": body}
        self.alert = f"{title}：{body}"
        # 先让软件窗口亮出来（恢复最小化、任务栏闪烁、弹窗写着要做什么），再把浏览器放到最前面：
        # 以前顺序反过来，浏览器刚拿出来就被随后弹到前面的软件窗口整个挡住，看着像还藏着
        log.info("需要你处理：%s（%s）", title, kind)
        winfocus.call_attention(APP_NAME)
        if self.browser:
            self.browser.show_window()
        else:
            log.warning("要你处理验证，但浏览器对象不在了")
        bus.emit("attention", title)
        if kind != "creator_login":
            self.mood.event("captcha")
        self.db.add_event("pause", title, "已弹窗提醒你去浏览器里处理")

    def _clear_attention(self) -> None:
        if self.attention:
            self.attention = None
            self.alert = ""

    def wait_verify(self, doing: str, max_s: float = 300) -> bool:
        """做事做到一半抖音弹了验证（比如替主人搜视频时，搜索页整页变成了“验证码中间页”）：
        弹窗提醒你、把浏览器拿到最前面，就在当前页面等你验证完（最多 max_s 秒，不关这个页面——
        以前是抛给后台统一处理，可那时搜索用的标签页已经关了，验证框跟着没了，软件以为你验证完了，
        主人那边什么也收不到）。验证完把浏览器藏回后台，返回 True；超时 / 没有浏览器返回 False。"""
        if not self.browser:
            return False
        kind = self.browser.verify_kind() or "captcha"
        self._raise_attention(kind, doing=doing)
        started = time.time()
        gone = 0
        ok = False
        try:
            while time.time() - started < max_s and not self._shutdown and self.browser:
                if self.browser.verification_open():
                    gone = 0
                else:
                    gone += 1
                    if gone >= 2:
                        ok = True
                        break
                cancel.sleep(1.5)
        finally:
            self._clear_attention()
            if self.browser:
                with cancel.shield():
                    try:
                        self.browser.hide_window()
                    except Exception:  # noqa: BLE001
                        log.warning("验证后藏浏览器失败", exc_info=True)
        self.db.add_event("info" if ok else "error", f"{doing}时的验证已完成" if ok else f"{doing}时的验证没有完成",
                          f"等了 {int(time.time() - started)} 秒")
        return ok

    def _on_captcha(self) -> None:
        """抖音弹了验证：把浏览器拿到屏幕上、弹窗提醒你；你验证完，马上自动把浏览器藏回后台，她接着干活。"""
        kind = (self.browser.verify_kind() if self.browser else "") or "captcha"
        self._raise_attention(kind)
        self._set("captcha", "等你在浏览器里完成验证")
        started = time.time()
        gone = 0
        timed_out = False
        try:
            while not self._shutdown and self.browser:
                # 连着两次（约 3 秒）都看不到验证框才算验证完：抖音有时验证完一个紧接着再弹一个
                if self.browser.verification_open():
                    gone = 0
                else:
                    gone += 1
                    if gone >= 2:
                        break
                try:
                    cmd = self.q.get(timeout=1.5)
                    self._dispatch(*cmd)
                except queue.Empty:
                    pass
                if time.time() - started > 1800:
                    timed_out = True
                    break
        finally:
            self._clear_attention()
            if self.browser:
                with cancel.shield():
                    try:
                        self.browser.hide_window()   # 验证完（或超时、出错）都藏回后台，不留在屏幕上
                    except Exception:  # noqa: BLE001
                        log.warning("验证后藏浏览器失败", exc_info=True)
        if timed_out:
            self._want_run = False
            self.alert = "验证 30 分钟没有完成，她已经暂停了。在浏览器里完成验证后，再点“开始刷视频”继续"
            self._set("paused", "验证 30 分钟没处理，已暂停")
            return
        self.db.add_event("info", "验证完成，浏览器已藏回后台", f"等了 {int(time.time() - started)} 秒")
        if self._want_run:
            self._set("running", "继续刷视频")

def publish_slots(rules: dict, n: int, now: float | None = None) -> list[float]:
    """今天想一想要不要发作品的时间点：在设定的时间段里随机挑 n 个（已经过去的不算）。"""
    from .budget import parse_hhmm
    from datetime import time as dtime

    now = now or time.time()
    day = datetime.fromtimestamp(now).date()
    a = datetime.combine(day, parse_hhmm(rules.get("publish_start", "11:00"), dtime(11, 0))).timestamp()
    b = datetime.combine(day, parse_hhmm(rules.get("publish_end", "21:00"), dtime(21, 0))).timestamp()
    a = max(a, now + 60)
    if b <= a:
        return []
    seg = (b - a) / n
    return sorted(a + seg * i + random.uniform(0, seg) for i in range(n))


def media_path(did: int, m: dict):
    """草稿里的一个图片 / 视频文件在哪：上传的大视频直接用原来的位置，其余都在 data/drafts/<草稿号>/。"""
    from pathlib import Path

    return Path(m["path"]) if m.get("path") else DRAFTS_DIR / str(did) / m["name"]


NOTICE_KINDS = ("at", "reply", "work")   # 互动消息里要看的：@ 她、回复她的评论、评论她的作品
NOTICE_GAP = 180          # 秒：多久去“互动消息”看一次（“通知”上来了新的红点会马上去看）
DM_GAP = 60               # 秒：活跃时段里多久把私信全面看一次（“消息”上来了新的红点会马上去看）
DM_GAP_NIGHT = 300        # 活跃时段以外：只看主人的私信，5 分钟一次
DRAIN_ROUNDS = 6          # 先回消息时最多看几轮（回的时候又来了新的，再看一轮）
# 安全阀（不是给你设的上限）：同一个人（主人除外）一天里来回超过这么多次，先停下放进“待你处理”
# ——免得碰上自动回复的机器人，两边没完没了地刷
PER_USER_DAY_GUARD = 60
GUARD_REASON = "和 TA 今天已经来回很多次了，可能是自动回复的机器人，先停一下，你看看要不要接着聊"
# 评论区“接着聊”的底线（写死，不能在界面上改）：和同一个人（主人除外）一天最多互聊 20 次；
# 接着聊的总数不设上限（和不同的人聊多少都行）。到了就不再回 TA、也不调用模型（不花钱），
# 消息标成“跳过 · 额度用完”——防止和自动回复的机器人互刷浪费 token
THREAD_USER_DAY_GUARD = 20
THREAD_LIMIT_REASON = ("今天在评论区和 TA 已经互聊了 {d} 次（和同一个人一天最多 {d} 次），可能是自动回复的机器人，"
                       "不再回了，省得白花模型的钱；想接着聊可以在这里手动回复")


def group_notices(items: list[dict]) -> list[list[dict]]:
    """互动消息按“同一个人、同一个视频、同一种”分组（每组最新的在前）：TA 连着说了几句，她看完一起回。
    认不出视频的不合并。组的顺序按每组最新那条的先后（从新到旧）。"""
    groups: dict = {}
    order = []
    for it in items:
        key = (it.get("user"), it.get("video_url"), it.get("kind")) if it.get("video_url") else id(it)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(it)
    return [groups[k] for k in order]
UNCERTAIN_REASON = "点了发送，但没能确认有没有发出去（可能已经发出去了）。为免重复，不会自动重发，请到抖音里看一眼"

BG_DIR_NAME = "_backgrounds"  # 文字卡片背景图库：data/drafts/_backgrounds


def backgrounds_dir():
    return DRAFTS_DIR / BG_DIR_NAME


def card_background(db: Database, d: dict):
    """这个草稿的文字卡片用哪张背景：(背景图路径或 None, 文字是否写在白卡片上)。"""
    rules = db.rules()
    name = str(d.get("card_bg") or "")
    if not name:
        name = str(rules.get("card_bg_default") or "")
        panel = bool(rules.get("card_panel_default", True))
    else:
        panel = bool(d.get("card_panel", 1))
    if not name or name == "none":
        return None, True
    p = backgrounds_dir() / os.path.basename(name)
    return (p if p.exists() else None), panel


def render_cards_for(db: Database, did: int, d: dict) -> list[dict]:
    """把草稿的文字卡片画成图片（按选好的背景），返回媒体列表。"""
    from ..publish.cards import render_post
    from .pet_avatar import persona_avatar

    persona = db.persona()
    bg, panel = card_background(db, d)
    names = render_post(d.get("title") or "", d.get("text") or "", d.get("cards") or [],
                        persona.get("name") or "雨雨", DRAFTS_DIR / str(did), avatar=persona_avatar(persona),
                        prefix=f"card{int(time.time()) % 100000}", bg_path=bg, panel_on=panel)
    return [{"name": n, "kind": "image", "source": "card"} for n in names]


FORMS = ("voice", "cards", "upload")


def draft_form(value) -> str:
    """作品的形式。以前的草稿没有记（空）：按图文卡片。"""
    v = str(value or "")
    return v if v in FORMS else "cards"


def drop_source(did: int, media: list, sources: tuple) -> list:
    """去掉草稿里某几种来源的图片 / 视频（文件也删掉；你从电脑上添加的原文件不在草稿文件夹里，不会动）。"""
    keep = []
    for m in media:
        if m.get("source") in sources:
            for n in (m.get("name"), m.get("audio")):
                if n and not m.get("path"):
                    (DRAFTS_DIR / str(did) / os.path.basename(n)).unlink(missing_ok=True)
        else:
            keep.append(m)
    return keep


def render_voice_image(db: Database, did: int, d: dict) -> list[dict]:
    """配音图片视频用的那一张图：标题和正文全写在一张图上，图上有桌宠（随机动作表情姿势）。返回媒体列表（1 项）。"""
    from ..pet.host import pet_settings
    from ..publish import voicecard

    bg, _panel = card_background(db, d)
    name = f"voicecard_{int(time.time() * 1000) % 10**9}.jpg"
    info = voicecard.render(d.get("title") or "", d.get("text") or "", db.persona().get("name") or "雨雨",
                            DRAFTS_DIR / str(did) / name, rig_name=str(pet_settings(db).get("rig") or "default"),
                            bg_path=bg)
    if info.get("cut"):
        log.info("配音图片：正文太长，图上没写完（配音会读完整的）")
    return [{"name": name, "kind": "image", "source": "voicecard", "pose": info.get("mood", "")}]


def drop_voice(did: int, media: list) -> list:
    """去掉草稿里的配音视频（文案或卡片改了，旧的配音就不对了）。"""
    keep = []
    for m in media:
        if m.get("source") == "voice":
            for n in (m.get("name"), m.get("audio")):
                if n:
                    (DRAFTS_DIR / str(did) / n).unlink(missing_ok=True)
        else:
            keep.append(m)
    return keep


def narrate_draft(db: Database, did: int) -> float:
    """用她的声音把草稿的文案读出来，和图片合成一个视频，放进草稿。返回花费（元）。

    配音图片视频（form=voice）：按现在的标题和正文重新画那一张图（桌宠换一个随机动作），整个视频只有这一张图，
    画面不动、不翻页。图文卡片（form=cards）：文字卡片和别的图片按顺序各停一会儿。"""
    from ..publish import narrate
    from ..publish import voice as tts

    d = db.draft(did)
    if not d:
        raise ValueError("找不到这个草稿")
    text = tts.speech_text(d.get("title") or "", d.get("text") or "")
    if len(text) < 4:
        raise ValueError("正文太短了，先写点内容再配音")
    media = drop_voice(did, list(d["media"]))
    if draft_form(d.get("form")) == "voice":
        media = render_voice_image(db, did, d) + drop_source(did, media, ("voicecard", "card"))
        db.update_draft(did, media=media)
        images = [media_path(did, media[0])]
    else:
        images = [media_path(did, m) for m in media if m.get("kind") == "image" and media_path(did, m).exists()]
    if not images:
        raise ValueError("还没有图片，先生成文字卡片或加一张图再配音")
    audio, ext, cost = tts.synthesize(db, db.rules(), text)
    folder = DRAFTS_DIR / str(did)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time()) % 10**8
    a_name, v_name = f"voice_{stamp}.{ext}", f"voice_{stamp}.mp4"
    (folder / a_name).write_bytes(audio)
    seconds = narrate.make_video(images, folder / a_name, folder / v_name)
    media.append({"name": v_name, "kind": "video", "source": "voice", "audio": a_name, "seconds": round(seconds, 1)})
    db.update_draft(did, media=media)
    return cost


def _mmss(sec: float) -> str:
    sec = int(max(0, sec))
    return f"{sec // 60}:{sec % 60:02d}"


# 主动找主人聊天：按心情隔多久（分钟，最短～最长）
OWNER_GAP_MIN = {"clingy": (25, 60), "excited": (35, 80), "bored": (40, 90), "happy": (45, 100), "sulky": (60, 150),
                 "calm": (90, 180), "grumpy": (120, 240), "sleepy": (180, 300)}
OWNER_UNANSWERED_MAX = 3      # 她连着说了这么多句主人都没回（比如回了一句 + 又主动找了两次）：先不打扰
OWNER_QUIET_HOURS = 8         # 不打扰多久以后可以再问候一次


def _fail_reason(inbox) -> str:
    """回复没发出去的原因：收件箱那边说得清就用它说的（比如“评论区里没找到对方那条评论”），说不清再用笼统的。"""
    note = getattr(inbox, "last_reply_note", "")
    return note if isinstance(note, str) and note else "发送失败，可能需要校准页面定位"


def in_quiet_hours(rules: dict, now: datetime | None = None) -> bool:
    """主人的勿扰时段（比如 23:30–08:00）：这段时间她不主动找主人。没设（开始 = 结束）就没有勿扰。"""
    from datetime import time as dtime
    start = parse_hhmm(rules.get("owner_quiet_start", "23:30"), dtime(23, 30))
    end = parse_hhmm(rules.get("owner_quiet_end", "08:00"), dtime(8, 0))
    if start == end:
        return False
    t = (now or datetime.now()).time()
    return (start <= t < end) if start < end else (t >= start or t < end)


def watch_keywords(v: dict, rules: dict) -> tuple[str, str]:
    """你设的“不想看 / 想多看”关键词：返回 (命中的不想看的词, 命中的想多看的词)。看文案、作者和热评。"""
    text = " ".join([v.get("desc") or "", v.get("author") or ""] + [str(c) for c in (v.get("comments") or [])[:3]]).lower()
    def hit(key: str) -> str:
        for w in rules.get(key) or []:
            w = str(w).strip().lower()
            if w and w in text:
                return w
        return ""
    return hit("watch_exclude"), hit("watch_prefer")


def dm_gap_seconds(rules: dict | None = None) -> int:
    """私信多久全面检查一次：60 秒（中间还会看“消息”上的红点，来了新私信马上去回）。"""
    return DM_GAP


def _int(v, default: int) -> int:
    """把设置值转成整数；没填时用默认值（0 是合法设置，不能被当成没填）。"""
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def safe_browser_error(e: Exception) -> str:
    if isinstance(e, BrowserError):
        return str(e)
    return f"{type(e).__name__}: {e}"
