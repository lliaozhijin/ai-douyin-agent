"""桌宠的总控：在自己的线程里开透明窗口，处理鼠标互动、右键菜单、站在任务栏上、摔下来的物理效果，
并且每隔一会儿看看她在抖音上正在做什么（刷视频、回私信、发作品……），让桌宠做对应的动作。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime

from .. import defaults
from ..agent.mood import MoodEngine
from . import bus
from . import engine as E
from .render import OX, OY, hit_body, hit_head, render
from .rig import load as load_rig

log = logging.getLogger(__name__)

SIZES = [(0.7, "小"), (1.0, "中"), (1.3, "大"), (1.6, "特大")]


def pet_settings(db) -> dict:
    s = dict(defaults.DEFAULT_PET)
    s.update(db.get_json("pet", {}) or {})
    return s


def bubble_colors(s: dict) -> tuple:
    d = defaults.DEFAULT_PET
    return tuple(str(s.get(k) or d[k]) for k in ("bubble_bg", "bubble_border", "bubble_text"))


class PetHost:
    """actions：界面那边提供的动作 —— open(page) 打开主界面某一页；start() / pause() 开始 / 暂停刷视频。"""

    def __init__(self, db, agent, actions: dict | None = None):
        self.db = db
        self.agent = agent
        self.actions = actions or {}
        self.cmds: queue.Queue = queue.Queue()
        self.win = None
        self.thread: threading.Thread | None = None
        self.error = ""
        self.running = False
        self.mood = MoodEngine(db)
        self._mood_key = "calm"
        self._mood_at = 0.0
        self._visible = False

    # ---------- 给界面用（任何线程都可以调用） ----------
    def start(self) -> None:
        from .window import IS_WINDOWS

        if not IS_WINDOWS:
            self.error = "桌宠只能在 Windows 上显示"
            return
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="pet", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.win:
            self.win.close()

    def send(self, cmd: str, **kw) -> None:
        self.cmds.put((cmd, kw))

    def status(self) -> dict:
        return {"running": self.running, "error": self.error, "visible": bool(self.running and self._visible)}

    # ---------- 线程 ----------
    def _run(self) -> None:
        from .window import LayeredWindow

        try:
            self.settings = pet_settings(self.db)
            self.rig = load_rig(self.settings.get("rig") or "default")
            name = self.db.persona().get("name") or "雨雨"
            self.pet = E.Pet(fx=self.rig.fx, fy=self.rig.fy, peek_depth=self.rig.peek_depth, name=name,
                             mood_key=self._get_mood, mood_line=lambda: self.mood.inner_line(self._mood_key),
                             on_mood=self._feel)
            self.pet.chatter = bool(self.settings.get("chatter", True))
            self.pet.hide_enabled = bool(self.settings.get("hide", True))
            self.pet.bubble_colors = bubble_colors(self.settings)
            self.win = LayeredWindow(name, self._on_mouse, self._tick)
            self._init_state()
            self.running = True
            self.error = ""
            self._visible = bool(self.settings.get("enabled", True))
            self.win.show(self._visible)
            if self._visible:
                self.pet.greet()
            self.win.loop()
        except Exception as e:  # noqa: BLE001
            log.exception("桌宠启动失败")
            self.error = f"桌宠没能启动：{e}"
        finally:
            self.running = False
            self.win = None

    def _init_state(self) -> None:
        self.user_scale = float(self.settings.get("size") or 1.0)
        self.scale = self.win.dpi() / 96.0 * self.user_scale
        self.last = time.perf_counter()
        self.pressed = self.dragging = False
        self.down_cursor = (0, 0)
        self.down_pos = (0.0, 0.0)
        self.last_cursor_x = 0
        self.last_mouse = self.win.cursor()
        self.last_mouse_move = 0.0
        self.last_hour = datetime.now().hour
        self.vx = self.vy = 0.0
        self.fall_start = 0.0
        self.poll_at = 0.0
        self.wish_at = 0.0
        self.watching = ""
        wl, wt, wr, wb = self.win.work_area(*self.last_mouse)
        x = self.settings.get("x")
        if x is None or not isinstance(x, (int, float)):
            x = wr - (OX + 160) * self.scale
        else:
            # 上次的位置是在别的显示器摆法下记的（拔了副屏、换了电脑、改了分辨率）：放回离它最近的那块屏幕里
            wl, wt, wr, wb = self.win.work_area(int(x + OX * self.scale), int((wt + wb) / 2))
            x = min(max(float(x), wl - (OX - 60) * self.scale), wr - (OX + 60) * self.scale)
        self.pos = [float(x), self._ground((wl, wt, wr, wb))]

    def _ground(self, wa) -> float:
        return wa[3] - (OY + 2) * self.scale

    def _wa(self):
        return self.win.work_area(int(self.pos[0] + OX * self.scale), int(self.pos[1] + (OY - 100 - self.pet.sink) * self.scale))

    def _local(self, sx, sy):
        return (sx - self.pos[0]) / self.scale - OX, (sy - self.pos[1]) / self.scale - OY

    def _get_mood(self) -> str:
        now = time.time()
        if now - self._mood_at > 5:
            self._mood_at = now
            try:
                self._mood_key = self.mood.current()["key"]
            except Exception:  # noqa: BLE001
                pass
        return self._mood_key

    def _feel(self, kind: str) -> None:
        try:
            self._mood_key = self.mood.event(kind)["key"]
        except Exception:  # noqa: BLE001
            pass

    def _save(self, **kw) -> None:
        s = pet_settings(self.db)
        s.update(kw)
        self.db.set_json("pet", s)
        self.settings = s

    # ---------- 每帧 ----------
    def _tick(self) -> None:
        self._commands()
        now = time.perf_counter()
        dt = min(0.05, now - self.last)
        self.last = now
        if not self._visible:
            return
        pet = self.pet
        cur = self.win.cursor()
        if cur != self.last_mouse:
            if self.last_mouse_move > 0 and now - self.last_mouse_move > 180:
                pet.on_user_back()  # 主人离开 3 分钟后回来了
            self.last_mouse = cur
            self.last_mouse_move = now
        hour = datetime.now().hour
        if hour != self.last_hour:
            self.last_hour = hour
            if datetime.now().minute == 0:
                pet.on_hour(hour)
        if self.dragging:
            self.pos[0] = self.down_pos[0] + (cur[0] - self.down_cursor[0])
            self.pos[1] = self.down_pos[1] + (cur[1] - self.down_cursor[1])
            vx_now = (cur[0] - self.last_cursor_x) / max(dt, 0.001) / self.scale
            pet.drag_vx = pet.drag_vx * 0.75 + vx_now * 0.25
        self.last_cursor_x = cur[0]
        if now >= self.poll_at:
            self.poll_at = now + 0.25
            self._poll()
        pet.world = (self.pos[0] / self.scale, self.pos[1] / self.scale)
        pet.update(dt, self._local(*cur)[0], watching=self.watching)
        wa = self._wa()
        ground = self._ground(wa)
        if pet.act == E.FALL:
            self.vy += 2400.0 * self.scale * dt
            self.pos[1] += self.vy * dt
            self.pos[0] += self.vx * dt
            self.vx *= 0.15 ** dt
            self._clamp_x(wa)
            if self.pos[1] >= ground:
                self.pos[1] = ground
                pet.land(ground - self.fall_start > 180 * self.scale)
                self.vx = self.vy = 0.0
        elif not self.dragging:
            if pet.sink <= 0.01 and self.pos[1] < ground - 1:
                self.fall_start, self.vx, self.vy = self.pos[1], 0.0, 0.0
                pet.start_fall(False)
            else:
                self.pos[1] = ground + pet.sink * self.scale
        cv = render(pet, self.rig, self.scale)
        self.win.push(cv.img, round(self.pos[0]), round(self.pos[1]))

    def _clamp_x(self, wa) -> bool:
        lo, hi = wa[0] - (OX - 90) * self.scale, wa[2] - (OX + 90) * self.scale
        if self.pos[0] < lo:
            self.pos[0] = lo
            return True
        if self.pos[0] > hi:
            self.pos[0] = hi
            return True
        return False

    def _poll(self) -> None:
        """看看她在抖音上正在做什么，桌宠跟着做动作。"""
        pet = self.pet
        try:
            st = self.agent.status()
        except Exception:  # noqa: BLE001
            st = {}
        mode = ""
        if self.settings.get("sync", True):
            act, state = st.get("activity") or "", st.get("state") or ""
            if act == "publish" or (act in ("watch", "inbox") and state == "running"):
                mode = act
            elif state in ("resting", "outside", "budget_done"):
                mode = "rest"
            self.watching = (st.get("current") or {}).get("title") or ""
        pet.set_mode(mode)
        for ev in bus.drain():
            if self.settings.get("sync", True) or ev["kind"] in ("publish_wish", "attention"):
                pet.on_event(ev)
        now = time.time()
        if now >= self.wish_at:
            self.wish_at = now + 10
            try:
                pet.wish = any(d["status"] == "wish" for d in self.db.drafts(include_done=False, limit=10))
            except Exception:  # noqa: BLE001
                pass

    def _commands(self) -> None:
        while True:
            try:
                cmd, kw = self.cmds.get_nowait()
            except queue.Empty:
                return
            if cmd == "settings":
                old = self.settings
                self.settings = pet_settings(self.db)
                self.pet.chatter = bool(self.settings.get("chatter", True))
                self.pet.hide_enabled = bool(self.settings.get("hide", True))
                self.pet.bubble_colors = bubble_colors(self.settings)
                if float(self.settings.get("size") or 1) != float(old.get("size") or 1):
                    self._set_size(float(self.settings.get("size") or 1))
                vis = bool(self.settings.get("enabled", True))
                if vis != self._visible:
                    self._visible = vis
                    self.win.show(vis)
                    if vis:
                        self.pet.greet()
            elif cmd == "rig":
                self.settings = pet_settings(self.db)  # 界面刚把“用哪个形象”存进数据库：先重新读一遍设置
                self.rig = load_rig(self.settings.get("rig") or "default")
                self.pet.fx, self.pet.fy, self.pet.peek_depth = self.rig.fx, self.rig.fy, self.rig.peek_depth
                self.pet.say("换好新形象啦～好看吗？", 3)
            elif cmd == "say":
                self.pet.say(kw.get("text", ""), kw.get("dur"))
            elif cmd == "act":
                self._menu_action(kw.get("id", 0))
            elif cmd == "name":
                self.pet.name = self.db.persona().get("name") or "雨雨"

    def _set_size(self, v: float) -> None:
        fx, fy = self.pos[0] + OX * self.scale, self.pos[1] + OY * self.scale
        self.user_scale = v
        self.scale = self.win.dpi() / 96.0 * v
        self.pos = [fx - OX * self.scale, fy - OY * self.scale]

    # ---------- 鼠标 ----------
    def _on_mouse(self, msg: int) -> None:
        from .window import WM_CAPTURECHANGED, WM_LBUTTONDBLCLK, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEMOVE, WM_RBUTTONUP

        pet = self.pet
        cur = self.win.cursor()
        if msg == WM_LBUTTONDOWN:
            self.pressed, self.dragging = True, False
            self.down_cursor, self.down_pos = cur, (self.pos[0], self.pos[1])
            self.win.capture(True)
        elif msg == WM_MOUSEMOVE:
            if not self.pressed:
                pet.on_hover()
                return
            if self.dragging:
                return
            if abs(cur[0] - self.down_cursor[0]) + abs(cur[1] - self.down_cursor[1]) > 6 * self.scale:
                self.dragging = True
                lx, ly = self._local(*self.down_cursor)
                pet.start_drag((E.clamp(lx, -80, 80), E.clamp(ly, -300 * self.rig.fy, -20)))
                self.last_cursor_x = cur[0]
        elif msg == WM_LBUTTONUP:
            if self.dragging:
                self._end_drag()
            elif self.pressed:
                lx, ly = self._local(*cur)
                if pet.is_hiding:
                    pet.start_climb()
                elif pet.on_click_sign():
                    self._open("drafts")
                elif hit_head(self.rig, pet, lx, ly):
                    pet.start_pat()
                elif hit_body(self.rig, pet, lx, ly):
                    pet.start_tickle()
            self.pressed = False
            self.win.capture(False)
        elif msg == WM_LBUTTONDBLCLK:
            self._open("dashboard")
        elif msg == WM_RBUTTONUP and not self.dragging:
            self._menu()
        elif msg == WM_CAPTURECHANGED and self.dragging:
            self._end_drag()

    def _end_drag(self) -> None:
        self.dragging = self.pressed = False
        self.vx = E.clamp(self.pet.drag_vx * 0.8, -1800, 1800) * self.scale
        self.vy, self.fall_start = 0.0, self.pos[1]
        self.pet.start_fall(self._ground(self._wa()) - self.pos[1] > 180 * self.scale)
        self._save(x=round(self.pos[0]))

    @property
    def free(self) -> bool:
        return not self.dragging and not self.pet.is_held

    def _open(self, page: str) -> None:
        fn = self.actions.get("open")
        if fn:
            try:
                fn(page)
            except Exception:  # noqa: BLE001
                log.exception("打开主界面失败")

    def _menu(self) -> None:
        st = {}
        try:
            st = self.agent.status()
        except Exception:  # noqa: BLE001
            pass
        name = self.pet.name
        s = self.settings
        items = [
            (1, f"摸摸{name}的头"),
            ("跳个舞吧", [(11, "舞蹈一：浪子踢球"), (12, "舞蹈二：狗熊哆嗦毛"), (13, "舞蹈三：闪身步")]),
            (2, "坐下来歇歇"), (3, "趴一会儿"), (4, "去睡觉吧"), (5, "躲起来"),
            ("-",),
            (20, "打开主界面"),
            (21, "让她休息一下" if st.get("running") else "开始刷视频"),
            (22, "看看她想发的作品", False, bool(self.pet.wish)),
            ("-",),
            ("大小", [(30 + i, label, abs(v - float(s.get("size") or 1)) < 0.01) for i, (v, label) in enumerate(SIZES)]),
            (40, "和主人聊天", bool(s.get("chatter", True))),
            (41, "没人理时躲到任务栏后面", bool(s.get("hide", True))),
            (42, "同步她在抖音上做的事", bool(s.get("sync", True))),
            ("-",),
            (50, "隐藏桌宠（可在主界面“桌宠”里重新打开）"),
        ]
        cmd = self.win.menu(items)
        if cmd:
            self._menu_action(cmd)

    def _menu_action(self, cmd: int) -> None:
        pet = self.pet
        if cmd == 1 and self.free:
            pet.start_pat()
        elif cmd in (11, 12, 13) and self.free:
            pet.start_dance(cmd - 11)
        elif cmd == 2 and self.free:
            pet.start_sit()
        elif cmd == 3 and self.free:
            pet.start_lie()
        elif cmd == 4 and self.free:
            pet.start_sleep()
        elif cmd == 5 and self.free and not pet.is_hiding:
            pet.start_hide()
        elif cmd == 20:
            self._open("dashboard")
        elif cmd == 21:
            running = bool((self.agent.status() or {}).get("running"))
            fn = self.actions.get("pause" if running else "start")
            r = fn() if fn else None
            if isinstance(r, dict) and not r.get("ok"):
                pet.say(str(r.get("error") or "没能开始")[:40], 4)
            else:
                pet.say("好哒，休息一下～" if running else "开始刷视频咯！")
        elif cmd == 22:
            self._open("drafts")
        elif 30 <= cmd < 30 + len(SIZES):
            v = SIZES[cmd - 30][0]
            self._save(size=v)
            self._set_size(v)
        elif cmd == 40:
            self._save(chatter=not self.settings.get("chatter", True))
            pet.chatter = bool(self.settings["chatter"])
        elif cmd == 41:
            self._save(hide=not self.settings.get("hide", True))
            pet.hide_enabled = bool(self.settings["hide"])
        elif cmd == 42:
            self._save(sync=not self.settings.get("sync", True))
        elif cmd == 50:
            self._save(enabled=False)
            self._visible = False
            self.win.show(False)
