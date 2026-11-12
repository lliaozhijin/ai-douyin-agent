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
from ..llm.router import BudgetExceeded
from . import safety
from .brain import Brain
from .budget import Budget, in_active_hours, parse_hhmm
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
        # 只回“这次启动、登录之后，并且活跃时段开始之后”才收到的 @ 和私信：
        # 每次启动后第一次去看消息、每天活跃时段开始后第一次去看消息，先把当时已经在的全部记下、不回
        self._inbox_epoch = None
        self._pause_requested = False
        self._pause_queued = 0  # 已经点了“让她休息一下”、还没处理到的暂停命令数（急停开关等它们处理完才复位）
        self._shutdown = False
        self._fails = 0
        self._since_rest = 0
        self._rest_until = 0.0
        self._next_at = 0.0   # 下次去看 @ 的时间
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
        self._next_at = self._next_dm = time.time() + 5
        self._set("running", "开始刷视频")
        self.db.add_event("info", "开始刷视频", "")

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
        self._process_inbox(stop_on_pause=was_running)
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
        if m["kind"] in ("at", "reply"):
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
        if not in_active_hours(rules):
            # 活跃时段以外她不刷视频、不回别人；但主人随时可以找她聊：来了主人的私信照样回
            if self._owner_on(rules) and self._owner_dm_waiting(b, rules):
                self._dm_flag = False
                self._process_inbox(at=False, dm=True, owners_only=True)
                self._next_dm = time.time() + max(300, dm_gap_seconds(rules))   # 夜里 5 分钟看一次，来了新私信马上看
            self._set("outside", f"活跃时段是 {rules['active_start']}–{rules['active_end']}（主人的私信照样回）"
                      if self._owner_on(rules) else f"活跃时段是 {rules['active_start']}–{rules['active_end']}")
            self._sleep(60)
            return
        due_at, due_dm = self._inbox_due(b, rules)
        if due_at or due_dm:
            self._process_inbox(at=due_at, dm=due_dm)
            now = time.time()
            if due_at:
                self._next_at = now + max(1, _int(rules.get("at_interval"), 10)) * 60
            if due_dm:
                self._next_dm = now + dm_gap_seconds(rules)
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
            self._set("budget_done", "今天看视频的额度用完啦，继续回复 @ 和私信")
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
            tri = self.brain.triage(v, first if can_see else [])
        except LLMError as e:
            self.db.add_event("error", "模型调用失败", str(e)[:200])
            self._sleep(dwell)
            feed.next()
            raise
        cost += tri["cost"]
        topic = tri.get("topic") or ""
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
            if not self._sleep(min(dwell, random.uniform(2, 6))) or self._stopped():
                return
            feed.next()
            return
        if self._stopped():
            return

        # 点不点赞由她自己（身份卡 + 记忆）判断；程序只看今天的额度和进度（app/agent/likes.py）
        lk = self.likes.judge(tri["interest"], v["author"], topic, rules, mood_key,
                              used=float(self.db.usage().get("likes") or 0))
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
        elif self.budget.can("comments"):
            post_gate = self.likes.post_gate(tri["interest"], topic, rules,
                                             float(self.db.usage().get("comments") or 0))
        share_owners = self._owners_for_share(rules)
        try:
            mem_ctx = self.memory.video_context(v, topic)
        except Exception:  # noqa: BLE001 —— 记忆读不出来不影响刷视频
            log.warning("读记忆失败", exc_info=True)
            mem_ctx = ""
        friends = []
        if (rules.get("at_friends_enabled", True) and self.budget.can("at_friends") and post_gate["ok"]):
            friends = self.db.friends_for_at(max(1, _int(rules.get("at_friend_per_day"), 1)))[:8]
        # 发评论、赞别人的评论、回复别人的评论：看了的视频都要判断，和视频本身赞没赞无关；
        # 赞评论只受今天的额度和进度限制（app/agent/likes.py）
        cl_used = float(self.db.usage().get("comment_likes") or 0)
        cl_gate = (self.likes.comment_gate(rules, cl_used)
                   if engage and self.budget.can("comment_likes") else {"ok": False, "why": ""})
        # 收藏：一天匀着收、同一类收多了提高标准（app/agent/likes.py）
        col_gate = {"ok": False, "why": "", "note": ""}
        if rules.get("collect_enabled", True) and self.budget.can("collects"):
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
                if friend and feed.last_mention_ok:
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
        if dec.get("share_owner") and not self._stopped():
            self._owner_share(b, dec["share_owner"]["owner"], dec["share_owner"]["text"], v)
        feed.next()

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
            n = self.db.purge_comment_messages()      # 评论区聊完的记录过期就删（私信一直留着）
            if n:
                log.info("消息中心：删掉了 %d 条评论区的旧记录", n)
        except (LLMError, BudgetExceeded) as e:
            log.info("这次没写成日记：%s", str(e)[:100])
        except Exception:  # noqa: BLE001
            log.warning("整理记忆出错", exc_info=True)

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
            sent = inbox.reply_dm({"user": owner["name"]}, text)
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
        try:
            draft = self.brain.owner_hello(owner, self.db.dm_history(name), self.memory.dm_context(name, ""),
                                           self.memory.day_material(today_str()),
                                           sticker=self._sticker_ok(rules, "dm"))
        except LLMError as e:
            self.db.add_event("error", "模型调用失败", str(e)[:200])
            return
        if not draft.ok:
            log.info("想对主人说的话没发：%s（%s）", draft.text[:30], draft.reason)
            return
        bus.emit("reply_dm", name)
        if self._owner_send(b, owner, draft.text, draft.sticker):
            self.db.add_dm(name, "me", draft.text)
            self.db.owner_touch(name, closer=0.3, proactive=True)
            self.memory.remember("dm", f"我主动找 {owner.get('call') or name} 聊天，说“{draft.text[:60]}”",
                                 scope=f"dm:{name}", person=name, importance=4)
            self.mood.event("dm", user=name)
            self.db.add_event("owner_chat", f"主动找主人 {owner.get('call') or name} 聊天", f"“{draft.text[:60]}”")

    def _owner_share(self, b, owner: dict, text: str, v: dict) -> None:
        """刷到想让主人也看看的视频：私信告诉 TA（她想说的话 + 视频地址）。"""
        if safety.output_problem(text):
            return
        name = owner["name"]
        url = v.get("url") or ""
        msg = f"{text} 《{(v.get('desc') or '')[:20]}》@{v.get('author') or ''} {url}".strip()
        self.current["step"] = f"把这个视频分享给 {owner.get('call') or name}"
        if self._owner_send(b, owner, msg):
            self.db.add_dm(name, "me", msg)
            self.db.owner_touch(name, closer=0.3, proactive=True)
            self.memory.remember("dm", f"我把 @{v.get('author')} 的视频《{(v.get('desc') or '')[:30]}》分享给了"
                                       f" {owner.get('call') or name}，说“{text[:50]}”",
                                 scope=f"dm:{name}", person=name, importance=4)
            self.db.add_event("owner_share", f"给主人 {owner.get('call') or name} 分享了视频",
                              f"“{text[:50]}” 《{(v.get('desc') or '')[:24]}》")

    # ---------- @ 和私信 ----------
    def _need_profile(self, user: str, rules: dict) -> bool:
        """要不要去对方主页看资料：没看过的要看；“只回复关注她的人”时每 12 小时重新确认一次关注关系。"""
        u = self.db.dm_user(user)
        if not u or not u.get("checked_at"):
            return True
        try:
            age_h = (datetime.now() - datetime.strptime(u["checked_at"], "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600
        except ValueError:
            return True
        return age_h >= (12 if rules.get("dm_scope") == "followers" else 24 * 7)

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
        r = self._cur_rules
        if not (r.get("dm_reply_mode", "instant") == "instant" and self.browser):
            return False
        if not self._dm_watch(r):
            return False
        if self.browser.dm_signal():
            self._dm_flag = True
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
        """现在该不该去看 @ / 私信。额度用完的就不去看（省时间也省 token）。"""
        now = time.time()
        # “互动消息”里的 @ 她、回复她评论的（接着聊）一起看；两个开关各管各的，额度也各算各的
        due_at = bool(self._notice_kinds(rules)) and now >= self._next_at
        due_dm = False
        if self._dm_watch(rules):
            due_dm = now >= self._next_dm
            if not due_dm and rules.get("dm_reply_mode", "instant") == "instant":
                # 秒回：页面里的小脚本一直盯着“消息”未读数；看视频途中来了新私信会提前结束这个视频，马上去回
                # 只回主人的时候不看“有没有未读”这个红点：别人的未读一直留着，红点一直在，会让她不停地去看
                due_dm = self._dm_flag or b.dm_signal() or (self._dm_for_all(rules) and Inbox(b).dm_badge())
        if due_dm:
            self._dm_flag = False
        return due_at, due_dm

    def _dm_for_all(self, rules: dict) -> bool:
        """现在回不回所有人的私信：开了自动回复，而且今天的私信额度还没用完。"""
        return bool(rules.get("auto_reply_dm")) and self.budget.can("replies_dm")

    def _dm_watch(self, rules: dict) -> bool:
        """要不要去看私信：回所有人的时候要看；额度用完了 / 关了自动回复，只要设了主人也照样看（只回主人）。"""
        return self._dm_for_all(rules) or self._owner_on(rules)

    def _notice_kinds(self, rules: dict, with_budget: bool = True) -> tuple:
        """要去“互动消息”里看哪几种：at（@ 她的）、reply（回复她评论的，接着聊）。
        “接着聊”不再挂在“自动回复 @”下面：只关了 @、或者 @ 的额度用完了，照样会去看有没有人回复她。"""
        out = []
        if rules.get("auto_reply_at") and (not with_budget or self.budget.can("replies_at")):
            out.append("at")
        if rules.get("auto_reply_thread", True) and (not with_budget or self.budget.can("thread_replies")):
            out.append("reply")
        return tuple(out)

    def _epoch_key(self, rules: dict, now: datetime | None = None) -> str:
        """最近一次活跃时段开始的时刻（今天的开始时间还没到，就是昨天的）。"""
        from datetime import time as dtime

        now = now or datetime.now()
        start = datetime.combine(now.date(), parse_hhmm(rules.get("active_start", "09:00"), dtime(9, 0)))
        if start > now:
            start -= timedelta(days=1)
        return start.strftime("%Y-%m-%d %H:%M")

    def _baseline_inbox(self, inbox: Inbox, rules: dict, kinds: tuple) -> None:
        """把现在已经在的 @、评论回复、未读私信都记成“之前收到的”，不回复、不提醒。
        之前留在“待你处理”里还没回的，也一起取消。"""
        n = 0
        self.current = {**self.current, "step": "记下之前收到的消息（这些不回）"}
        if kinds:
            for it in inbox.fetch_mentions(limit=40, is_known=self.db.message_exists, kinds=kinds,
                                           mine=self.db.my_comment_texts(), resolve_video=False):
                if self.db.message_exists(it["ext_id"]):
                    continue
                self.db.add_message(kind=it["kind"], ext_id=it["ext_id"], user_name=it["user"], content=it["text"],
                                    video_url=it.get("video_url", ""), status="skipped_old", reason=OLD_REASON,
                                    handled_at=now_str())
                n += 1
        if rules.get("auto_reply_dm"):
            owners = self._owner_names(rules)
            try:
                # 主人的未读私信不算“之前收到的”：不点开，留着等下一轮照常回
                for it in inbox.fetch_unread_dms(limit=20, need_profile=lambda u: False, skip=owners):
                    if self.db.message_exists(it["ext_id"]):
                        continue
                    self.db.add_message(kind="dm", ext_id=it["ext_id"], user_name=it["user"], content=it["text"],
                                        status="skipped_old", reason=OLD_REASON, handled_at=now_str())
                    n += 1
            finally:
                with cancel.shield():
                    try:
                        inbox.close_dm_panel()
                    except Exception:  # noqa: BLE001
                        pass
        keep = sorted(self._owner_names(rules))            # 主人留在“待你处理”里的不取消
        old = self.db.execute(
            "UPDATE messages SET status='skipped_old', reason=?, handled_at=? WHERE status='pending'"
            + (" AND NOT (kind='dm' AND user_name IN (%s))" % ",".join("?" * len(keep)) if keep else ""),
            (OLD_REASON, now_str(), *keep)).rowcount or 0
        if n or old:
            self.db.add_event("info", "从现在起才回复新消息",
                              f"之前收到的 {n + old} 条 @ / 私信不再回复（只回这次启动、活跃时段开始之后收到的）")

    def _owner_names(self, rules: dict) -> set:
        return {o["name"] for o in self.db.owners(enabled_only=True)} if rules.get("owner_enabled", True) else set()

    def _process_inbox(self, stop_on_pause: bool = True, at: bool = True, dm: bool = True,
                       owners_only: bool = False) -> None:
        """检查 @ 和私信。运行中被叫停时（stop_on_pause），处理完当前这一条就不再继续。

        owners_only：只看主人的私信（活跃时段以外用）。私信额度用完了、或者关了“自动回复私信”时，
        也只点开主人的会话——别人的未读留着不动。"""
        rules = self.db.rules()
        b = self._ensure_browser()
        inbox = Inbox(b)
        halted = (lambda: stop_on_pause and not self._want_run)
        self._set("running" if self._want_run else "idle", "去看看主人有没有找她" if owners_only else "去看看有没有人找她")
        kinds = self._notice_kinds(rules)
        key = self._epoch_key(rules)
        if self._inbox_epoch != key and not owners_only:
            # 这次启动后第一次看消息，或者新的一天活跃时段刚开始：之前的都不回
            self.activity = "inbox"
            self._baseline_inbox(inbox, rules, self._notice_kinds(rules, with_budget=False))
            self._inbox_epoch = key
            return
        if at and kinds and not halted():
            self.activity = "inbox"
            found = inbox.fetch_mentions(is_known=self.db.message_exists, kinds=kinds,
                                         mine=self.db.my_comment_texts())
            self._log_notice_check(inbox, kinds, found)
            for it in found:
                if halted():
                    return
                if self.db.message_exists(it["ext_id"]):
                    continue
                mid = self.db.add_message(kind=it["kind"], ext_id=it["ext_id"], user_name=it["user"],
                                          content=it["text"], video_url=it.get("video_url", ""),
                                          status="pending", reason="处理中")
                self._safe_handle(inbox, mid, it, it["kind"], rules)
                self._run_urgent()
        only = None if (self._dm_for_all(rules) and not owners_only) else self._owner_names(rules)
        if dm and (only is None or only) and not halted():
            self.activity = "inbox"
            try:
                for it in inbox.fetch_unread_dms(need_profile=lambda u: self._need_profile(u, rules), only=only):
                    if it.get("profile"):
                        self.db.save_dm_user(it["user"], it["profile"], checked="follow_btn" in it["profile"])
                    if halted():
                        return
                    if self.db.message_exists(it["ext_id"]):
                        continue
                    mid = self.db.add_message(kind="dm", ext_id=it["ext_id"], user_name=it["user"],
                                              content=it["text"], status="pending", reason="处理中")
                    self._safe_handle(inbox, mid, it, "dm", rules)
                    self._dm_catch_up(inbox, mid, it, rules, halted)
                    self._run_urgent()
            finally:
                with cancel.shield():  # 叫停了也先把私信面板关掉，免得盖住页面
                    try:
                        inbox.close_dm_panel()
                    except Exception:  # noqa: BLE001
                        pass
                self.activity = "watch" if self._want_run else ""

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
            it = {"ext_id": ext, "user": it["user"], "text": text, "lines": missed, "shares": [], "need_share": False,
                  "profile": {}}
            answered = list(missed)
            mid = self.db.add_message(kind="dm", ext_id=ext, user_name=it["user"], content=text, status="pending",
                                      reason="处理中")
            self._safe_handle(inbox, mid, it, "dm", rules)

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
        for r in rows:
            sq = re.sub(r"\s+", "", r["text"])
            if their and sq == their and r["user"] == it.get("user"):
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
        names = {"at": "@ 她", "reply": "回复她评论"}
        log.info("看了互动消息（%s）：通知 %s 条，其中 @ 她 %s 条、回复她评论 %s 条；新的 @ %d 条、新的回复 %d 条",
                 " + ".join(names[k] for k in kinds), stats.get("rows", "?"), stats.get("at", "?"),
                 stats.get("reply", "?"), n_at, n_rp)
        self.notice_checked = {"at": now_str(), "new_at": n_at, "new_reply": n_rp}

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

    def _handle_incoming(self, inbox: Inbox, mid: int, it: dict, kind: str, rules: dict) -> None:
        user, text = it["user"], it["text"]
        if self.db.is_blocked(user):
            self.db.update_message(mid, status="blocked", reason="在不回复名单里", handled_at=now_str())
            return
        if kind == "dm":
            self.db.add_dm(user, "them", text)
            if rules.get("dm_scope", "all") == "followers" and not self.db.owner(user):
                u = self.db.dm_user(user) or {}
                follows = u.get("follows_me", -1)
                if follows == 0:
                    self.db.update_message(mid, status="skipped_scope", reason="设置了只回复关注她的人，对方没有关注她",
                                           handled_at=now_str())
                    return
                if follows != 1:
                    self._hold(mid, "没能确认对方有没有关注她，先放着等你看", status="pending")
                    return
        owner = self.db.owner(user) if kind == "dm" and rules.get("owner_enabled", True) else None
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
        budget_key = "replies_at" if kind == "at" else "replies_dm"
        if not owner:                              # 和主人聊天不设上限
            if self.db.replies_to_user_today(user) >= _int(rules.get("per_user_daily"), 3):
                self._hold(mid, "今天对这个人的回复已到上限", status="pending")
                return
            if not self.budget.can(budget_key):
                self.db.update_message(mid, status="skipped_limit", reason="今日回复额度已用完", handled_at=now_str())
                return

        if kind == "at":
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
            draft = self.brain.reply_at(video, user, text, frames, memory=self.memory.comment_context(user, text),
                                        context=self._thread_context(inbox, it))
        else:
            history = self.db.dm_history(user)
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
            draft = self.brain.reply_dm(user, history, text, video=video, frames=frames, owner=owner, memory=mem_ctx,
                                        sticker=self._sticker_ok(rules, "dm"))
        self.db.update_message(mid, cost=draft.cost, tokens=draft.tokens)
        if not draft.ok:
            self._hold(mid, draft.reason, reply=draft.text, rules=rules)
            return
        bus.emit("reply_at" if kind == "at" else "reply_dm", user)
        sent = inbox.reply_mention(it, draft.text) if kind == "at" else inbox.reply_dm(it, draft.text)
        if sent:
            if owner:
                self._owner_mark(user)             # 刚和主人聊过：主动找 TA 的时间重新算；也不占别人的私信额度
            else:
                self.budget.use(budget_key)
            self.db.update_message(mid, status="replied", reply=draft.text, reason="", handled_at=now_str())
            where = "评论区的 @" if kind == "at" else "私信"
            note = getattr(inbox, "last_reply_note", "") if kind == "at" else ""
            self.db.add_event("reply_at" if kind == "at" else "reply_dm", f"回复了 @{user} 的{where}",
                              f"“{text[:30]}” → “{draft.text[:40]}”" + (f"（{note}）" if isinstance(note, str) and note else ""),
                              ref=f"msg:{mid}")
            if kind == "dm":
                self.db.add_dm(user, "me", draft.text)
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
                self.memory.remember_at(user, text, draft.text, video)
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

    def _quick_reply(self, inbox: Inbox, mid: int, it: dict, kind: str, text: str, why: str) -> None:
        """不走模型，直接发一句现成的话（关键字自动回复、敏感话题说明原因）。"""
        user = it["user"]
        key = {"at": "replies_at", "dm": "replies_dm", "reply": "thread_replies"}[kind]
        free = kind == "dm" and user in self._owner_names(self.db.rules())      # 和主人聊天不设上限、不占额度
        if not free and not self.budget.can(key):
            self.db.update_message(mid, status="skipped_limit", reason="今日回复额度已用完", handled_at=now_str())
            return
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
            if free:
                self._owner_mark(user)
            else:
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
        cap = max(1, _int(rules.get("thread_max_replies"), 3))
        if th["closed"] or th["count"] >= cap:
            self.db.close_thread(tid)
            self.db.update_message(mid, status="skipped_limit", handled_at=now_str(),
                                   reason=f"这串评论对话她已经回了 {th['count']} 次（上限 {cap} 次），不再继续")
            return
        if self.db.replies_to_user_today(user) >= _int(rules.get("per_user_daily"), 3):
            self._hold(mid, "今天对这个人的回复已到上限", status="pending")
            return
        if not self.budget.can("thread_replies"):
            self.db.update_message(mid, status="skipped_limit", reason="今日“接着聊评论”的额度已用完",
                                   handled_at=now_str())
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
        last = th["count"] + 1 >= cap
        parent = self.db.reply_parent(mine)            # 她当初回复的那条评论（楼主）：这一串的开头
        draft = self.brain.reply_thread(video, user, text, history + [], last, parent=parent,
                                        memory=self.memory.comment_context(user, text),
                                        context=self._thread_context(inbox, it))
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
            self.db.thread_add(tid, "them", text)
            self.db.thread_add(tid, "me", draft.text)
            if last:
                self.db.close_thread(tid)
            self.db.update_message(mid, status="replied", reply=draft.text, reason="", handled_at=now_str())
            self.db.add_event("thread_reply", f"接着和 @{user} 聊（这串第 {th['count'] + 1}/{cap} 次）",
                              f"“{text[:30]}” → “{draft.text[:40]}”", ref=f"msg:{mid}")
            self.memory.remember_thread(tid, user, text, draft.text, video)
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

    def _make_draft(self, force: bool) -> int | None:
        rows = self.db.query("SELECT title FROM videos WHERE interest >= 7 AND title != '' ORDER BY id DESC LIMIT 12")
        mood = self.mood.current()
        self.current = {**self.current, "step": "在构思想发的作品"}
        try:
            mem = self.memory.post_material()
        except Exception:  # noqa: BLE001 —— 记忆读不到不影响写作品
            mem = ""
        d = self.brain.draft_post([r["title"] for r in rows], f"{mood['label']}（{mood['reason']}）", force=force,
                                  memory=mem)
        if not d["want"]:
            self.db.add_event("info", "她想了想，今天不发作品", d["why"] or f"心情：{mood['label']}")
            return None
        rules = self.db.rules()
        form = draft_form(rules.get("post_form"))
        if form == "upload":
            form = "voice"                      # 她自己写的没有“你上传的”这种形式
        did = self.db.add_draft(status="wish", kind="image", title=d["title"], text=d["text"], tags=d["tags"],
                                cards=d["cards"], image_prompt=d["image_prompt"], mood=mood["key"],
                                reason=d["why"], source="manual" if force else "auto", form=form)
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
        self.db.add_event("publish_wish", "您的小可爱想发作品", f"《{d['title']}》{d['why']}", ref=f"draft:{did}")
        bus.emit("publish_wish", d["title"])
        return did

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
            self.db.add_event("pause", "发布中途停下", f"《{d['title'][:20]}》没有发出去", ref=f"draft:{did}")
            raise
        except (PublishError, BrowserError) as e:
            self.db.update_draft(did, status="failed", error=str(e)[:300])
            self.db.add_event("error", "作品发布失败", str(e)[:200], ref=f"draft:{did}")
            self.alert = f"作品《{d['title'][:12]}》没有发布成功：{str(e)[:120]}"
            return
        finally:
            self.activity = prev
        self.budget.use("publishes")
        self.db.update_draft(did, status="published", published_at=now_str(), error="")
        self.db.add_event("published", f"发布了作品《{d['title'][:20]}》", msg + "（已声明：内容由AI生成）",
                          ref=f"draft:{did}")
        self.mood.event("publish_ok")
        self.memory.remember("post", f"我发了一个作品《{d['title'][:30]}》", importance=4, dedupe=f"post:{did}")
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
    def _raise_attention(self, kind: str, during_login: bool = False, during_publish: bool = False) -> None:
        """需要你亲自处理：把浏览器拿到屏幕上，软件窗口弹到前面并弹窗说明（不只是一行小字）。"""
        self._attention_seq += 1
        if kind == "creator_login":
            title = "需要登录抖音创作者中心"
            body = ("发作品要用抖音创作者中心，它要求再登录一次。请在她的浏览器窗口里用抖音 App 扫码登录，"
                    "登录好后她会自动接着发布。")
        elif kind == "sms":
            title = "需要填写手机验证码"
            body = (("发作品时抖音要求验证你的手机号。" if during_publish else "抖音要求验证你的手机号。")
                    + "请到弹到最前面的浏览器窗口里点“获取验证码”，把手机收到的短信验证码填进去，再点“验证”。"
                    + ("填完就登录成功了。" if during_login else
                       "验证完成后浏览器会自动藏回后台，她接着把作品发出去。" if during_publish else
                       "验证完成后浏览器会自动藏回后台，她继续刷视频。") + "（软件不会、也不能替你填写验证码。）")
        else:
            title = "需要你完成一个验证"
            body = ("抖音弹出了安全验证（比如拖动滑块、点选图片）。请到弹到最前面的浏览器窗口里手动完成，"
                    + ("完成后浏览器会自动藏回后台，她接着把作品发出去。" if during_publish else
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


OLD_REASON = "这次启动 / 今天活跃时段开始之前就收到的消息，按规则不再回复"
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


def dm_gap_seconds(rules: dict) -> int:
    """私信多久全面检查一次：秒回 60 秒（中间还会看红点）；否则 5 分钟。"""
    return 60 if rules.get("dm_reply_mode", "instant") == "instant" else 300


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
