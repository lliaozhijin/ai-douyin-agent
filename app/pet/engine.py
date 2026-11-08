"""桌宠的“大脑”：状态机、每个动作的姿态、台词、特效粒子。

大部分逻辑移植自原来的雨雨桌宠（C# 版 Pet.cs）：待机三支舞、坐下、趴下、睡觉、摸头、挠痒、拎起来、
摔下来、躲到任务栏后面探头、爬出来、整点报时、主人回来打招呼……
新增：和她在抖音上做的事同步（刷视频时捧着手机认真看、点赞冒爱心、写评论 / 回私信时打字、
想发作品时举小牌子、需要你帮忙时着急挥手），以及按心情挑待机动作和台词。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime

from .dances import DANCES


# ---------- 数学小工具（和原版 M 类一样） ----------
def clamp(v, a, b):
    return a if v < a else b if v > b else v


def lerp(a, b, t):
    return a + (b - a) * t


def smooth(t):
    t = clamp(t, 0.0, 1.0)
    return t * t * (3 - 2 * t)


def env(a, dur, fade):
    """动作的淡入淡出：开头 fade 秒从 0 升到 1，结尾 fade 秒降回 0。"""
    return smooth(min(a / fade, (dur - a) / fade))


def ease_out_back(x):
    c1 = 1.70158
    c3 = c1 + 1
    u = x - 1
    return 1 + c3 * u * u * u + c1 * u * u


def ease_in_out(x):
    x = clamp(x, 0.0, 1.0)
    return 2 * x * x if x < 0.5 else 1 - (-2 * x + 2) ** 2 / 2


def catmull(p0, p1, p2, p3, u):
    return 0.5 * (2 * p1 + (-p0 + p2) * u + (2 * p0 - 5 * p1 + 4 * p2 - p3) * u * u
                  + (-p0 + 3 * p1 - 3 * p2 + p3) * u * u * u)


S = math.sin
C = math.cos
PI = math.pi

# 动作
IDLE, LOOK, WALK, WAVE, HOP, DANCE, DANCE1, DANCE2, DANCE3, YAWN, SLEEP, SIT, LIE, PAT, TICKLE, DRAG, FALL, LAND, \
    HIDE, PEEK, CLIMB, PHONE, TYPE, PHONE_LIKE, CHEER, SIGN, ALERT, SULK = range(28)
DANCE_ACTS = (DANCE1, DANCE2, DANCE3)
FREE_ACTS = {IDLE, LOOK, WALK, WAVE, HOP, DANCE, DANCE1, DANCE2, DANCE3, YAWN, SIT, LIE, SULK, SLEEP}
PHONE_ACTS = {PHONE, TYPE, PHONE_LIKE}

# 粒子
HEART, SPARK, SWEAT, TEAR, ZZZ, NOTE, PUFF, BANG, QUES, STAR, CHAT, DOTS = range(12)


@dataclass
class Pose:
    BodyX: float = 0.0
    BodyY: float = 0.0
    Tilt: float = 0.0
    SquashX: float = 1.0
    SquashY: float = 1.0
    HeadTilt: float = 0.0
    HeadSquash: float = 1.0
    HeadDY: float = 0.0
    HeadDX: float = 0.0
    ArmL: float = 0.0
    ArmR: float = 0.0
    StrL: float = 0.0
    StrR: float = 0.0
    ArmDY: float = 0.0
    LegDY: float = 0.0
    LegL: float = 0.0
    LegR: float = 0.0
    LiftL: float = 0.0
    LiftR: float = 0.0
    BodyAlpha: float = 1.0
    ShadowW: float = 1.0
    ShadowX: float = 0.0
    ArmScale: float = 1.0
    LieE: float = 0.0
    LegsFront: bool = False
    LegScaleY: float = 1.0
    Shadow: bool = True
    Anger: bool = False
    Dizzy: bool = False
    PatHandT: float = -1.0
    Rot: float = 0.0
    Pivot: tuple = (0.0, -230.0)
    Phone: bool = False   # 捧着手机
    Typing: bool = False  # 在手机上打字
    Sign: bool = False    # 举着“想发作品”的牌子
    HoldL: float = 0.0    # 双手捧着东西的程度（0～1）：手放到手机两边，手肘自然弯着（不是直直地伸过去）
    HoldR: float = 0.0
    HoldDL: tuple = (0.0, 0.0)   # 捧着时手的小动作（打字、往上划），逻辑单位
    HoldDR: tuple = (0.0, 0.0)


class Spring:
    """一个带阻尼的小弹簧：辫子、呆毛、裙摆的摆动都用它。x 是偏离量（角度或比例）。"""

    def __init__(self, k: float, c: float, lim: float):
        self.k, self.c, self.lim = k, c, lim
        self.x = 0.0
        self.v = 0.0

    def step(self, dt: float, target: float, force: float) -> float:
        acc = -self.k * (self.x - target) - self.c * self.v + force
        self.v += acc * dt
        self.x += self.v * dt
        if abs(self.x) > self.lim:  # 甩得太狠就停在边上，像碰到了什么
            self.x = math.copysign(self.lim, self.x)
            self.v *= -0.25
        return self.x


class Face:
    """表情和“软”的部件：眨眼、说话时嘴巴一张一合、跟着动作和心情换眼睛嘴巴，
    还有两条小辫子、头顶呆毛、裙摆跟着她的动作甩来甩去（默认形象有这些部件；形象里没有就不画）。

    表情只在原图上变化：eyes 是 open（原图的眼睛）/ closed（眨眼）；mouth 是 smile（原图的笑嘴）/ talk（说话张嘴）。"""

    def __init__(self, rnd: random.Random):
        self.rnd = rnd
        self.eyes = "open"
        self.mouth = "smile"
        self._blink_in = rnd.uniform(1.5, 4.0)
        self._blink_left = 0.0
        self._double = False
        self._talk_flip = 0.0
        self._talk_open = False
        self._happy_flash = 0.0
        self.tail_l = Spring(26.0, 3.0, 38.0)
        self.tail_r = Spring(24.0, 2.8, 38.0)
        self.ahoge = Spring(110.0, 7.0, 32.0)
        self.skirt = Spring(40.0, 3.6, 1.0)
        self._prev = None
        self._vel = (0.0, 0.0)
        self._acc = (0.0, 0.0)

    # ---------- 眼睛 / 嘴巴 ----------
    def _talking(self, pet) -> bool:
        text = pet.bubble_text
        if not text:
            return False
        shown = pet.bubble_t * 20 + 1
        return shown < len(text) + 4  # 字一个个冒出来的时候在说话（最后多动两下）

    def _pick_eyes(self, pet, p, mood: str, dt: float) -> str:
        """一直是原图的眼睛，只是会眨眼；睡觉、打哈欠时闭着。"""
        a, t = pet.act, pet.act_t
        if a == SLEEP:
            return "closed"
        if a == YAWN:
            k = t / max(pet.act_dur, 0.1)
            if 0.2 < k < 0.75:
                return "closed"
        # 眨眼：每 2～6 秒眨一下，偶尔连眨两下（困的时候眨得勤一点）
        self._blink_in -= dt
        if self._blink_left > 0:
            self._blink_left -= dt
            if self._blink_left <= 0 and self._double:
                self._double = False
                self._blink_in = 0.12
            return "closed"
        if self._blink_in <= 0:
            self._blink_left = 0.11
            self._double = self.rnd.random() < 0.18
            self._blink_in = self.rnd.uniform(2.0, 6.0) * (0.6 if mood == "sleepy" else 1.0)
            return "closed"
        return "open"

    def _pick_mouth(self, pet, p, mood: str, dt: float) -> str:
        """一直是原图的笑嘴；说话（对话框里字一个个冒出来）时一张一合，打哈欠时张开。"""
        if pet.act == YAWN:
            k = pet.act_t / max(pet.act_dur, 0.1)
            if 0.15 < k < 0.8:
                return "talk"
        if self._talking(pet):
            self._talk_flip -= dt
            if self._talk_flip <= 0:
                self._talk_open = not self._talk_open
                self._talk_flip = self.rnd.uniform(0.07, 0.15)
            return "talk" if self._talk_open else "smile"
        self._talk_open = False
        return "smile"

    def flash_happy(self, sec: float = 1.2) -> None:
        """点赞、收藏、作品发出去……开心地眯一下眼。"""
        self._happy_flash = max(self._happy_flash, sec)

    # ---------- 摆动 ----------
    def _motion(self, pet, p, dt: float):
        wx, wy = getattr(pet, "world", (0.0, 0.0))
        ang = math.radians(p.Tilt + p.Rot)
        hx = wx + p.BodyX + p.HeadDX + math.sin(ang) * 230 * pet.fy
        hy = wy + p.BodyY + p.HeadDY - math.cos(ang) * 230 * pet.fy
        if self._prev is None or dt <= 0:
            self._prev = (hx, hy)
            return 0.0, 0.0
        vx, vy = (hx - self._prev[0]) / dt, (hy - self._prev[1]) / dt
        self._prev = (hx, hy)
        ax, ay = (vx - self._vel[0]) / dt, (vy - self._vel[1]) / dt
        self._vel = (vx, vy)
        k = min(1.0, dt * 18)  # 低通一下，免得某一帧卡顿造成猛甩
        self._acc = (self._acc[0] + (clamp(ax, -6000, 6000) - self._acc[0]) * k,
                     self._acc[1] + (clamp(ay, -6000, 6000) - self._acc[1]) * k)
        return self._acc

    def update(self, pet, dt: float) -> None:
        dt = clamp(dt, 1 / 240, 1 / 15)
        p = pet.last_pose
        mood = pet._mood()
        self._happy_flash = max(0.0, self._happy_flash - dt)
        self.eyes = self._pick_eyes(pet, p, mood, dt)
        self.mouth = self._pick_mouth(pet, p, mood, dt)
        ax, ay = self._motion(pet, p, dt)
        tilt = p.Tilt + p.HeadTilt + p.Rot
        breeze = math.sin(pet.t * 1.3) * 1.6
        # 辫子往下垂：身体歪了它还想竖直往下（反着转一点）；往右加速时甩向左（顺时针）；往下加速时往外飘
        self.tail_l.step(dt, -tilt * 0.7 + breeze, ax * 0.12 + ay * 0.09)
        self.tail_r.step(dt, -tilt * 0.7 + breeze * 0.8, ax * 0.12 - ay * 0.09)
        # 呆毛朝上翘：跟着头转，加速时往反方向弯一下
        self.ahoge.step(dt, math.sin(pet.t * 2.1) * 2.0, -ax * 0.5)
        # 裙摆：往右加速时下摆落在后面（往左）；身体歪了下摆还是往下垂
        self.skirt.step(dt, -p.Tilt * 0.02, -ax * 0.012)


@dataclass
class Particle:
    kind: int
    x: float
    y: float
    vx: float
    vy: float
    life: float
    max: float
    size: float
    seed: float


@dataclass
class Pet:
    fx: float = 1.0          # 形象的比例（默认雨雨 = 1）：特效位置跟着头的大小换算
    fy: float = 1.0
    peek_depth: float = 132.0
    name: str = "雨雨"
    mood_key: object = None      # () -> 当前心情 key
    mood_line: object = None     # () -> 她心里的一句话
    on_mood: object = None       # (事件) -> None：摸头、拎起来……会影响心情

    act: int = IDLE
    act_t: float = 0.0
    act_dur: float = 3.0
    t: float = 0.0
    walk_dir: int = 1
    look_x: float = 0.0
    swing: float = 0.0
    swing_v: float = 0.0
    drag_vx: float = 0.0
    pivot: tuple = (0.0, -230.0)
    land_dizzy: bool = False
    sink: float = 0.0
    bubble_text: str | None = None
    bubble_t: float = 0.0
    bubble_dur: float = 0.0
    parts: list = field(default_factory=list)
    idle_since: float = 0.0
    chatter_timer: float = 20.0
    chatter: bool = True
    hide_enabled: bool = True
    hide_after: float = 45.0
    last_pose: Pose = field(default_factory=Pose)
    mode: str = ""               # watch / inbox / publish：在抖音上忙；rest：休息；空：自由活动
    bubble_colors: tuple = ("#FFE8F2", "#FF8CB9", "#DE4684")  # 对话框：底色、边框、文字
    wish: bool = False           # 有想发的作品在等你确认
    flick: float = 0.0           # 刷视频时手指往上一划
    rnd: random.Random = field(default_factory=random.Random)

    def __post_init__(self):
        self._arm = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]  # armL, armR, strL, strR, holdL, holdR（平滑过渡用）
        self._peek_mode = 0
        self._peek_mode_t = 0.0
        self._peek_total = 0.0
        self._hop_prev = 0.0
        self._next_time_line = False
        self._sign_cool = 0.0
        self._dance = 0
        self.face = Face(self.rnd)
        self.world = (0.0, 0.0)  # 宠物在屏幕上的位置（逻辑单位），辫子甩动按它算加速度

    # ---------- 小工具 ----------
    def R(self, a, b):
        return a + self.rnd.random() * (b - a)

    def chance(self, dt, per_second):
        return self.rnd.random() < dt * per_second

    def pick(self, lst):
        return self.rnd.choice(lst)

    def side(self):
        return -1 if self.rnd.random() < 0.5 else 1

    def set_act(self, a, dur):
        self.act, self.act_t, self.act_dur = a, 0.0, dur

    def say(self, text: str, dur: float | None = None):
        if dur is None:
            dur = 2.2 + len(text) * 0.14
        if self.bubble_text == text and self.bubble_t < self.bubble_dur:
            self.bubble_dur = max(self.bubble_dur, self.bubble_t + dur)
            return
        self.bubble_text, self.bubble_t, self.bubble_dur = text, 0.0, dur

    def _mood(self) -> str:
        try:
            return self.mood_key() if self.mood_key else "calm"
        except Exception:  # noqa: BLE001
            return "calm"

    def _feel(self, kind: str) -> None:
        if self.on_mood:
            try:
                self.on_mood(kind)
            except Exception:  # noqa: BLE001
                pass

    # ---------- 台词 ----------
    def idle_lines(self):
        n = self.name
        return [f"主人主人～看看{n}嘛！", f"{n}今天也超可爱的对吧？", "要不要休息一下眼睛呀？", f"嘿嘿～{n}在这里哦！",
                "主人最棒啦！", "摸摸头可以……但只能一下下哦～", "肚子咕咕叫了……", "记得多喝水哦～", "主人加油鸭！",
                f"{n}想出去玩～", "坐久了要起来伸个懒腰哦～", f"{n}会一直陪着主人的！", "今天也要开开心心哒～",
                f"主人在忙什么呀？{n}乖乖等你～"]

    TICKLE_LINES = ["嘻嘻，好痒～", "不要戳我啦～", "诶？干嘛呀～"]

    def peek_lines(self):
        return ["偷偷看主人在干嘛～", f"{self.name}躲好啦，来找我呀～", "嘿嘿，被发现了吗？", "主人看不见我～看不见我～"]

    def time_line(self, hour: int) -> str:
        if 5 <= hour < 10:
            lst = ["早上好呀主人～今天也要元气满满哦！", "早餐吃了吗？要好好吃饭哦～"]
        elif 10 <= hour < 14:
            lst = ["中午啦～记得吃饭饭！", "吃饱饱才有力气工作哦～"]
        elif 14 <= hour < 18:
            lst = ["下午容易犯困，喝杯水吧～", f"下午茶时间到～{self.name}想吃小蛋糕！"]
        elif 18 <= hour < 23:
            lst = ["晚上好～今天辛苦啦！", f"晚饭吃什么呀？{self.name}也想吃～"]
        else:
            lst = [f"这么晚了还不睡吗……{self.name}陪着你～", "熬夜会变成熊猫眼哦，早点睡吧～"]
        return self.pick(lst)

    # ---------- 互动入口 ----------
    def greet(self):
        self.say(f"主人好～{self.name}来陪你啦！", 3.0)
        self.chatter_timer = 6.0
        self._next_time_line = True

    def start_pat(self):
        self.set_act(PAT, 2.6)
        self.sink = 0.0
        mood = self._mood()
        self.say("哼，不要一直摸啦！" if mood == "grumpy" else "嘿嘿～再摸一下下" if mood == "clingy"
                 else f"主人，不要摸{self.name}的头！", 2.8)
        self.idle_since = 0.0
        for _ in range(4):
            self.add_heart()
        self._feel("pat")

    def start_tickle(self):
        self.set_act(TICKLE, 1.3)
        self.say(self.pick(self.TICKLE_LINES), 2.0)
        self.idle_since = 0.0
        for _ in range(4):
            self.add(STAR, self.R(-70, 70) * self.fx, self.R(-140, -60) * self.fy, self.R(-40, 40), self.R(-90, -50),
                     0.9, self.R(5, 8))
        self._feel("tickle")

    def start_drag(self, pivot):
        self.set_act(DRAG, 1e9)
        self.sink = 0.0
        self.pivot, self.swing_v, self.drag_vx = pivot, 0.0, 0.0
        self.say(f"主人，快放{self.name}下来！", 1e9)
        self.idle_since = 0.0
        self.add(BANG, 70 * self.fx, -300 * self.fy, 10, -40, 0.9, 26)
        self._feel("drag")

    def start_fall(self, high: bool):
        self.set_act(FALL, 1e9)
        self.sink = 0.0
        if high:
            self.say("哇啊——", 1e9)
        else:
            self.bubble_text = None

    def land(self, dizzy: bool):
        self.land_dizzy = dizzy
        self.swing = self.swing_v = 0.0
        self.set_act(LAND, 2.2 if dizzy else 0.8)
        self.say("呜……头好晕～" if dizzy else "呼～终于下来了", 2.2)
        self.puff_burst(6 if dizzy else 4)
        if dizzy:
            self._feel("dizzy")

    def start_sleep(self):
        self.set_act(SLEEP, self.R(25, 50))
        self.say(f"{self.name}困了……", 2.0)

    def start_dance(self, k: int | None = None):
        k = self.rnd.randrange(3) if k is None else k
        self._dance = k
        self.set_act(DANCE_ACTS[k], len(DANCES[k]) * 0.1 * 2)

    def start_sit(self):
        self.set_act(SIT, self.R(8, 14))
        if self.chatter and self.rnd.random() < 0.5:
            self.say("坐一会儿～")

    def start_lie(self):
        self.set_act(LIE, self.R(8, 13))
        if self.chatter and self.rnd.random() < 0.5:
            self.say("趴一下下好舒服～")

    def start_hide(self):
        self.set_act(HIDE, 1.0)

    def _start_peek(self):
        self.set_act(PEEK, 1e9)
        self.sink = self.peek_depth
        self._peek_mode = 0
        self._peek_mode_t = self._peek_total = 0.0
        if self.chatter:
            self.say(self.pick(self.peek_lines()))

    def start_climb(self):
        self.set_act(CLIMB, 1.7)
        self.say("嘿咻……嘿咻……", 1.6)
        self.idle_since = 0.0
        self.add(BANG, 60 * self.fx, -330 * self.fy, 8, -40, 0.8, 24)

    @property
    def is_hiding(self):
        return self.act in (PEEK, HIDE)

    @property
    def is_held(self):
        return self.act in (DRAG, FALL)

    def on_hover(self):
        if self.act == PEEK:
            self.start_climb()

    def on_user_back(self):
        self.say(f"主人回来啦！{self.name}好想你～")
        self.add_heart()
        self.add_heart()
        self._feel("user_back")

    def on_hour(self, hour: int):
        self.say(f"整点啦～现在是{hour}点哦！")

    # ---------- 和她在抖音上做的事同步 ----------
    @property
    def busy(self) -> bool:
        return self.mode in ("watch", "inbox", "publish")

    def set_mode(self, mode: str) -> None:
        if mode == self.mode:
            return
        self.mode = mode
        if self.busy and self.act in FREE_ACTS | {PEEK, HIDE}:
            if self.act in (PEEK, HIDE):
                self.start_climb()
            else:
                self.start_phone()
        elif not self.busy and self.act in PHONE_ACTS:
            self.set_act(IDLE, self.R(2, 4))
            if mode == "rest" and self.chatter:
                self.say("休息一下下～")

    def start_phone(self):
        self.set_act(PHONE, self.R(6, 12))

    def on_event(self, ev: dict) -> None:
        kind, text = ev.get("kind"), (ev.get("text") or "").strip()
        if kind == "publish_wish":
            self.wish = True
        if kind in ("published",):
            self.wish = False
        if self.is_held or self.act in (CLIMB,):
            return
        if self.is_hiding and kind in ("publish_wish", "attention", "love", "published"):
            self.start_climb()
            return
        if self.is_hiding:
            return
        short = (text[:14] + "…") if len(text) > 14 else text
        if kind in ("like", "collect", "love", "published", "comment_like"):
            self.face.flash_happy()
        if kind == "like":
            self.set_act(PHONE_LIKE, 1.6)
            for _ in range(5):
                self.add(HEART, self.R(-30, 30) * self.fx, self.R(-110, -80) * self.fy, self.R(-30, 30),
                         self.R(-120, -70), 1.4, self.R(6, 10))
            if self.rnd.random() < 0.4:
                self.say(self.pick(["点赞！♡", "这个必须赞～", "喜欢喜欢！"]))
        elif kind == "collect":
            self.set_act(PHONE_LIKE, 1.6)
            for _ in range(4):
                self.add(STAR, self.R(-40, 40) * self.fx, self.R(-200, -120) * self.fy, self.R(-40, 40),
                         self.R(-100, -60), 1.2, self.R(5, 8))
            self.say(self.pick(["收藏起来，以后还要看！", "这个先存着～", "马住马住！"]))
        elif kind in ("typing", "comment", "comment_reply", "reply_dm", "reply_at", "thread_reply", "publishing"):
            self.set_act(TYPE, clamp(1.5 + len(text) * 0.08, 2.0, 5.0))
            lines = {
                "typing": f"在评论：{short}" if text else "在想怎么评论～",
                "comment": f"评论好啦：{short}" if text else "评论好啦～",
                "comment_reply": "回复一下这条评论～",
                "reply_dm": f"在回 {short} 的私信～" if text else "在回私信～",
                "reply_at": f"在回复 @{short}～" if text else "有人@我，去看看～",
                "thread_reply": f"{short} 回我啦，接着聊～" if text else "有人回我评论啦～",
                "publishing": "在发作品啦，有点紧张…",
            }
            self.say(lines[kind])
            if kind == "reply_dm":
                for _ in range(2):
                    self.add(CHAT, self.R(-40, 40) * self.fx, self.R(-150, -120) * self.fy, self.R(-15, 15), -40,
                             1.6, 10)
        elif kind == "comment_like":
            for _ in range(3):
                self.add_heart()
            if self.rnd.random() < 0.5:
                self.say("这条评论说得好！")
        elif kind == "skip":
            self.flick = 0.45
        elif kind in ("love", "published"):
            self.set_act(CHEER, 1.8)
            self.say("哇！好喜欢这个！" if kind == "love" else "作品发出去啦！好开心～")
            for _ in range(6):
                self.add(STAR, self.R(-90, 90) * self.fx, self.R(-260, -120) * self.fy, self.R(-60, 60),
                         self.R(-110, -60), 1.1, self.R(5, 9))
        elif kind == "publish_wish":
            self.start_sign()
        elif kind == "attention":
            self.set_act(ALERT, 8.0)
            self.say(text or "需要主人帮忙一下！", 6.0)
            self.add(BANG, 70 * self.fx, -320 * self.fy, 6, -30, 1.0, 26)
        elif kind == "at_friend":
            self.say(f"快来看～{short}" if text else "分享给好朋友～")

    def start_sign(self):
        self.set_act(SIGN, 9.0)
        self._sign_cool = 70.0
        self.say("主人～我想发作品！点我看看嘛", 5.0)

    def on_click_sign(self) -> bool:
        """点了举着牌子的她：返回 True 表示应该打开“作品”页面。"""
        if self.act == SIGN:
            self.say("耶！主人快看看～", 2.0)
            self.set_act(IDLE, 2.0)
            return True
        return False

    # ---------- 待机 ----------
    def _next_idle(self):
        if self.busy:
            self.start_phone()
            return
        if self.hide_enabled and self.idle_since > self.hide_after and self.mode != "rest":
            self._feel("ignored")
            self.start_hide()
            return
        mood = self._mood()
        if self.mode == "rest" and mood not in ("excited",):
            mood = "sleepy" if self.rnd.random() < 0.4 else mood
        if self.idle_since > 150 and self.rnd.random() < 0.33:
            self.start_sleep()
            return
        r = self.rnd.randrange(100)
        if mood in ("happy", "excited"):
            if r < 15:
                self.set_act(IDLE, self.R(3, 5))
            elif r < 80:
                self.start_dance()
            elif r < 92:
                self.set_act(WAVE, 2.2)
            else:
                self.set_act(HOP, 2.4)
        elif mood == "clingy":
            if r < 30:
                self.set_act(WAVE, 2.4)
                self.add_heart()
            elif r < 55:
                self.set_act(IDLE, self.R(3, 5))
            elif r < 75:
                self.start_sit()
            else:
                self.start_dance()
        elif mood == "grumpy":
            if r < 50:
                self.set_act(SULK, self.R(5, 8))
                if self.chatter and self.rnd.random() < 0.6:
                    self.say(self.pick(["哼！", "哼唧……", "不理你了！（才怪）"]))
            elif r < 80:
                self.set_act(IDLE, self.R(3, 5))
            else:
                self.start_lie()
        elif mood == "sulky":
            if r < 45:
                self.start_sit()
                self.add(TEAR, 52 * self.fx, -176 * self.fy, 40, -60, 0.8, 4.5)
            elif r < 70:
                self.start_lie()
            else:
                self.set_act(IDLE, self.R(3, 6))
        elif mood == "sleepy":
            if r < 30:
                self.set_act(YAWN, 2.8)
            elif r < 55 and self.idle_since > 30:
                self.start_sleep()
            elif r < 80:
                self.start_sit()
            else:
                self.start_lie()
        elif mood == "bored":
            if r < 35:
                self.set_act(LOOK, self.R(3, 5))
            elif r < 60:
                self.start_sit()
            elif r < 80:
                self.start_lie()
            else:
                self.start_dance()
        else:  # 平静：和原版一样，三支舞中间穿插站姿休息，偶尔趴下
            if r < 25:
                self.set_act(IDLE, self.R(3, 6))
            elif r < 50:
                self.start_dance(0)
            elif r < 75:
                self.start_dance(1)
            elif r < 92:
                self.start_dance(2)
            else:
                self.start_lie()

    @property
    def can_idle_chat(self):
        return self.act in (IDLE, LOOK, SIT, LIE, PEEK, PHONE, SULK)

    # ---------- 每帧更新 ----------
    def update(self, dt: float, cursor_lx: float, now: datetime | None = None, watching: str = "") -> None:
        self.t += dt
        self.act_t += dt
        self.idle_since += dt if not self.busy else 0.0
        self.flick = max(0.0, self.flick - dt)
        self._sign_cool -= dt
        tx = clamp(cursor_lx / 260.0, -1, 1)
        self.look_x += (tx - self.look_x) * min(1.0, dt * 4)
        if self.bubble_text is not None:
            self.bubble_t += dt
            if self.bubble_t >= self.bubble_dur:
                self.bubble_text = None
        self.face.update(self, dt)
        if self.chatter and self.can_idle_chat and self.bubble_text is None:
            self.chatter_timer -= dt
            if self.chatter_timer <= 0:
                self.say(self._chat_line(now or datetime.now(), watching))
                self.chatter_timer = self.R(30, 70) if not self.busy else self.R(25, 50)
        if self.wish and self._sign_cool <= 0 and self.act in FREE_ACTS | PHONE_ACTS and not self.busy:
            self.start_sign()

        a = self.act
        if a == IDLE:
            self.sink = 0.0
            if self.act_t >= self.act_dur:
                self._next_idle()
        elif a in PHONE_ACTS:
            self.sink = 0.0
            if a == TYPE and self.chance(dt, 3):
                self.add(DOTS, self.R(-10, 10) * self.fx, -112 * self.fy, 0, -30, 0.8, 1.0)
            if self.act_t >= self.act_dur:
                if self.busy:
                    self.start_phone()
                else:
                    self.set_act(IDLE, self.R(2, 4))
        elif a == SLEEP:
            if self.chance(dt, 0.8):
                self.add(ZZZ, 46 * self.fx, -292 * self.fy, 14, -22, 2.6, self.R(13, 19))
            if self.act_t >= self.act_dur:
                self.idle_since = 0.0
                self.set_act(YAWN, 2.8)
        elif a == DRAG:
            target = clamp(self.drag_vx * 0.045, -24, 24)
            self.swing_v += ((target - self.swing) * 80 - self.swing_v * 8) * dt
            self.swing = clamp(self.swing + self.swing_v * dt, -26, 26)
            if self.chance(dt, 3):
                s = self.side()
                self.add(SWEAT, s * self.R(70, 84) * self.fx, self.R(-280, -240) * self.fy, s * self.R(50, 90),
                         self.R(-90, -50), 0.9, self.R(6, 8))
            if self.chance(dt, 12):
                s = self.side()
                self.add(TEAR, s * 52 * self.fx, -176 * self.fy, s * self.R(90, 150), self.R(-130, -60), 0.8,
                         self.R(3.5, 5))
        elif a == FALL:
            self.swing_v += ((0 - self.swing) * 60 - self.swing_v * 6) * dt
            self.swing += self.swing_v * dt
        elif a == HIDE:
            if self.act_t < 0.3:
                self.sink = 0.0
            else:
                if self.sink == 0.0:
                    self.puff_burst(3)
                self.sink = self.peek_depth * ease_out_back(clamp((self.act_t - 0.3) / 0.6, 0, 1))
            if self.act_t >= self.act_dur:
                self._start_peek()
        elif a == PEEK:
            self._update_peek(dt)
        elif a == CLIMB:
            t = self.act_t
            pd = self.peek_depth
            if t < 0.3:
                self.sink = pd + 14 * smooth(t / 0.3)
            elif t < 1.15:
                self.sink = lerp(pd + 14, 0, ease_in_out((t - 0.3) / 0.85))
            else:
                self.sink = 0.0
            if 0.3 < t < 1.15 and self.chance(dt, 4):
                s = self.side()
                self.add(SWEAT, s * self.R(70, 84) * self.fx, self.R(-290, -250) * self.fy, s * self.R(40, 70), -60,
                         0.8, self.R(6, 8))
            if self.act_t >= self.act_dur:
                self.sink = 0.0
                self.idle_since = 0.0
                if self.busy:
                    self.start_phone()
                else:
                    self.set_act(IDLE, self.R(2.5, 4))
                    self.say(f"当当～{self.name}爬上来啦！")
                for _ in range(6):
                    self.add(STAR, self.R(-90, 90) * self.fx, self.R(-260, -120) * self.fy, self.R(-60, 60),
                             self.R(-110, -60), 1.1, self.R(5, 9))
                self.add_heart()
                self.add_heart()
        else:
            self._update_effects(dt)
            if self.act_t >= self.act_dur:
                if self.busy:
                    self.start_phone()
                else:
                    self.set_act(IDLE, self.R(2.5, 5))

        self._smooth_arms(dt)
        keep = []
        for q in self.parts:
            q.life -= dt
            if q.life <= 0:
                continue
            q.x += q.vx * dt
            q.y += q.vy * dt
            if q.kind in (SWEAT, TEAR):
                q.vy += 620 * dt
            elif q.kind == PUFF:
                q.vx *= 1 - dt * 3
                q.vy *= 1 - dt * 3
            elif q.kind == STAR:
                q.vy += 160 * dt
            else:
                q.x += S(self.t * 3 + q.seed * 6.28) * 12 * dt
            keep.append(q)
        self.parts = keep

    def _chat_line(self, now: datetime, watching: str) -> str:
        if self.busy:
            if watching and self.rnd.random() < 0.5:
                w = watching[:12]
                return self.pick([f"在看：{w}…", f"这个《{w}》有点意思～", "认真看ing……"])
            return self.pick(["认真刷视频中～", "这个好好笑～", "嗯嗯……原来是这样！", "下一个下一个～",
                              (self.mood_line() if self.mood_line else "今天也要元气满满～")])
        if self._next_time_line or self.rnd.random() < 0.25:
            self._next_time_line = False
            return self.time_line(now.hour)
        if self.act == PEEK:
            return self.pick(self.peek_lines())
        if self.mood_line and self.rnd.random() < 0.5:
            try:
                return self.mood_line()
            except Exception:  # noqa: BLE001
                pass
        return self.pick(self.idle_lines())

    def _update_peek(self, dt):
        self._peek_mode_t += dt
        self._peek_total += dt
        dur = 1.6 if self._peek_mode == 1 else 4.5
        if self._peek_mode_t > dur:
            self._peek_mode_t = 0.0
            self._peek_mode = 4 if self._peek_total > 90 and self.rnd.random() < 0.5 else self.rnd.randrange(4)
            if self._peek_mode == 0 and self.rnd.random() < 0.5:
                self.add(QUES, 62 * self.fx, -320 * self.fy, 6, -20, 1.4, 26)
        a = self._peek_mode_t
        self.sink = self.peek_depth
        if self._peek_mode == 1:  # 缩下去再“噗”地冒出来
            if a < 0.5:
                self.sink += 75 * smooth(a / 0.5)
            elif a < 0.9:
                self.sink += 75
            else:
                k = clamp((a - 0.9) / 0.45, 0, 1)
                self.sink += 75 * (1 - ease_out_back(k))
                if a - dt < 0.9:
                    self.add(BANG, 64 * self.fx, -320 * self.fy, 6, -30, 0.9, 24)
                    self.puff_burst(2)
        if self._peek_mode == 4 and self.chance(dt, 0.7):
            self.add(ZZZ, 46 * self.fx, -300 * self.fy, 14, -22, 2.4, self.R(12, 17))
        if self.chance(dt, 0.12):
            self.add_heart()

    def _update_effects(self, dt):
        a = self.act
        fx, fy = self.fx, self.fy
        if a == PAT:
            if self.act_t < 2 and self.chance(dt, 5):
                self.add_heart()
            if self.chance(dt, 2.5):
                self.add(SPARK, self.R(-85, 85) * fx, self.R(-320, -250) * fy, 0, -30, 0.7, self.R(5, 8))
        elif a == TICKLE:
            if self.chance(dt, 6):
                self.add(SPARK, self.R(-75, 75) * fx, self.R(-140, -40) * fy, 0, -40, 0.6, self.R(4, 7))
        elif a in DANCE_ACTS or a in (DANCE, CHEER):
            if self.chance(dt, 2.4):
                self.add(NOTE, self.R(-95, 95) * fx, self.R(-280, -230) * fy, 0, -45, 1.8, self.R(0.9, 1.3))
            if self.chance(dt, 1.5):
                self.add(STAR, self.R(-90, 90) * fx, self.R(-200, -100) * fy, self.R(-30, 30), -70, 1.0, self.R(4, 7))
        elif a == WALK:
            if self.chance(dt, 1.6):
                self.add(PUFF, -self.walk_dir * self.R(10, 30), -3, -self.walk_dir * 20, -6, 0.6, self.R(5, 7))
        elif a == HOP:
            ph = (self.act_t % 0.8) / 0.8
            if ph < self._hop_prev:
                self.puff_burst(2)
            self._hop_prev = ph
        elif a == SIT:
            if self.chance(dt, 0.35):
                self.add(NOTE, self.R(-80, 80) * fx, self.R(-260, -220) * fy, 0, -40, 1.8, self.R(0.9, 1.2))
            if self.chance(dt, 0.2) and self._mood() != "sulky":
                self.add_heart()
        elif a == LIE:
            if self.act_t > 0.8 and self.chance(dt, 0.8):
                self.add(NOTE, self.R(110, 160) * fx, self.R(-140, -110) * fy, 0, -40, 1.6, self.R(0.8, 1.1))
            if self.chance(dt, 0.25):
                self.add_heart()
        elif a == WAVE:
            if self.chance(dt, 3):
                self.add(SPARK, self.R(80, 110) * fx, self.R(-200, -150) * fy, 0, -30, 0.6, self.R(4, 7))
        elif a == ALERT:
            if self.chance(dt, 1.2):
                self.add(BANG, self.R(-80, 80) * fx, -330 * fy, 6, -30, 0.9, 24)
        elif a == SIGN:
            if self.chance(dt, 0.8):
                self.add_heart()

    def add_heart(self):
        self.add(HEART, self.R(-85, 85) * self.fx, self.R(-310, -260) * self.fy, self.R(-15, 15), self.R(-70, -45),
                 1.4, self.R(6, 10))

    def puff_burst(self, n):
        for i in range(n):
            s = -1 if i % 2 == 0 else 1
            self.add(PUFF, s * self.R(20, 45), -3, s * self.R(30, 70), self.R(-25, -8), 0.7, self.R(6, 9))

    def add(self, kind, x, y, vx, vy, life, size):
        if len(self.parts) > 100:
            return
        self.parts.append(Particle(kind, x, y, vx, vy, life, life, size, self.rnd.random()))

    # ---------- 姿态 ----------
    def build_pose(self) -> Pose:
        p = Pose()
        a, t = self.act_t, self.t
        fy = self.fy
        br = S(t * 2.4)
        p.SquashY = 1 + br * 0.01
        p.SquashX = 1 - br * 0.006
        p.HeadTilt = self.look_x * 4 + S(t * 0.9) * 1.2
        act = self.act
        if act == LOOK:
            s = S(a * 1.9)
            p.HeadTilt, p.Tilt = s * 8, s * 1.5
        elif act == WALK:
            s = S(a * 9)
            p.LiftL, p.LiftR = max(0, s) * 8, max(0, -s) * 8
            p.BodyY, p.Tilt = -abs(s) * 3, s * 2.5
            p.ArmL, p.ArmR = -48 + s * 14, -48 - s * 14
            p.StrL = p.StrR = 1
            p.HeadTilt = self.walk_dir * 4 + s * 1.5
        elif act == WAVE:
            e = env(a, self.act_dur, 0.3)
            p.ArmR = 75 + S(a * 13) * 20
            p.StrR = 1 if e > 0.05 else 0
            p.HeadTilt = -5 * e + self.look_x * 4
        elif act in (HOP, CHEER):
            ph = (a % 0.8) / 0.8
            h = S(ph * PI)
            p.BodyY = -h * 30
            p.SquashY, p.SquashX = 1 + (h - 0.35) * 0.12, 1 - (h - 0.35) * 0.08
            p.ArmL = p.ArmR = (-30 + h * 100) if act == HOP else (95 + S(a * 9) * 15)
            p.StrL = p.StrR = 1
            p.LiftL = p.LiftR = h * 3
        elif act in DANCE_ACTS:
            self._apply_dance(p, DANCES[DANCE_ACTS.index(act)], a, self.act_dur)
        elif act == DANCE:
            s = S(a * 6.5)
            p.Tilt, p.BodyY = s * 8, -abs(C(a * 6.5)) * 7
            p.ArmL, p.ArmR = 35 + s * 45, 35 - s * 45
            p.StrL = p.StrR = 1
            p.LiftL, p.LiftR = max(0, s) * 8, max(0, -s) * 8
            p.HeadTilt = -s * 6
        elif act == YAWN:
            e = S(clamp(a / self.act_dur, 0, 1) * PI)
            p.ArmL = p.ArmR = -40 + e * 112
            p.StrL = p.StrR = 1 if a < self.act_dur - 0.25 else 0
            p.SquashY += e * 0.05
            p.SquashX -= e * 0.03
            p.HeadTilt = -4 * e
        elif act == SLEEP:
            p.HeadTilt, p.HeadDY = 8 + S(t * 1.3) * 2.5, 3
            p.SquashY, p.SquashX = 1 + S(t * 1.3) * 0.018, 1
            p.ArmL = p.ArmR = -52
            p.StrL = p.StrR = 1
        elif act == SIT:
            e = env(a, self.act_dur, 0.45)
            p.BodyY = 26 * e * fy
            p.LegsFront = e > 0.4
            p.LegScaleY = lerp(1, 0.45, e)
            p.LegL = e * (14 + S(t * 3.2) * 10)
            p.LegR = e * (14 + S(t * 3.2 + 1.7) * 10)
            p.ArmL = p.ArmR = -46
            p.StrL = p.StrR = 1 if a < self.act_dur - 0.35 else 0
            p.HeadTilt = S(t * 0.8) * 5 + self.look_x * 3
            p.Tilt = S(t * 0.8) * 1.5 * e
            p.ShadowW = 1 + 0.35 * e
        elif act == LIE:
            e = env(a, self.act_dur, 0.6)
            p.LieE = e
            p.HeadTilt = e * (12 + S(t * 1.2) * 4)
            p.LegR = e * (18 + S(t * 4) * 24)
            p.LegL = e * (8 + S(t * 4 + PI) * 14)
            p.ArmL, p.ArmR = -50, -45 + max(0, S(t * 0.9)) * 80
            p.StrL = p.StrR = 1 if a < self.act_dur - 0.4 else 0
            p.SquashY, p.SquashX = 1 + S(t * 1.6) * 0.012, 1
            p.ShadowW, p.ShadowX = 1 + 1.4 * e, -8 * e
        elif act == PAT:
            press = abs(S(a * 7)) if a < 2 else 0
            p.HeadSquash = 1 - 0.07 * press
            p.ArmL, p.ArmR = 5 + S(a * 15) * 25, 5 - S(a * 15) * 25
            p.StrL = p.StrR = 1 if a < self.act_dur - 0.25 else 0
            st = S(a * 9)
            p.LiftL, p.LiftR = max(0, st) * 5, max(0, -st) * 5
            p.HeadTilt = S(a * 3.5) * 5
            p.Anger = self._mood() != "clingy"
            p.PatHandT = a if a < 2.2 else -1
        elif act == TICKLE:
            h = S(a / 0.5 * PI) if a < 0.5 else 0
            p.BodyY = -h * 18
            p.Tilt = S(a * 20) * 5 * (1 - a / self.act_dur)
            p.ArmL = p.ArmR = -20 + h * 60
            p.StrL = p.StrR = 1 if a < self.act_dur - 0.25 else 0
        elif act == DRAG:
            p.Shadow = False
            p.Rot = self.swing + S(t * 7) * 4
            p.Pivot = self.pivot
            p.SquashY, p.SquashX = 1.03, 0.98
            p.ArmL, p.ArmR = 45 + S(t * 21) * 45, 45 + S(t * 21 + 2.2) * 45
            p.StrL = p.StrR = 1
            kick = S(t * 17)
            p.LegL, p.LegR = 6 + kick * 18, 6 - kick * 18
            p.LiftL, p.LiftR = max(0, kick) * 8, max(0, -kick) * 8
            p.HeadTilt = S(t * 13) * 6
            p.BodyX = S(t * 31) * 1.5
            p.Anger = True
        elif act == FALL:
            p.Shadow = False
            p.Rot, p.Pivot = self.swing, self.pivot
            p.ArmL, p.ArmR = 85 + S(t * 25) * 18, 85 + S(t * 25 + 1.5) * 18
            p.StrL = p.StrR = 1
            p.LegL = p.LegR = 14 + S(t * 20) * 5
        elif act == LAND:
            k, c = math.exp(-a * 7), C(a * 22)
            p.SquashY, p.SquashX = 1 - 0.22 * k * c, 1 + 0.16 * k * c
            if self.land_dizzy:
                p.HeadTilt, p.Tilt, p.Dizzy = S(t * 3.5) * 9, S(t * 3.5) * 3, True
                p.ArmL = p.ArmR = -50 + S(t * 3.5) * 8
                p.StrL = p.StrR = 1 if a < self.act_dur - 0.35 else 0
        elif act == HIDE:
            h = S(a / 0.3 * PI) if a < 0.3 else 0
            p.BodyY = -h * 18
            p.ArmL = p.ArmR = lerp(0, 45, smooth(a / 0.4))
            p.StrL = p.StrR = 1
            p.Shadow = a < 0.3
        elif act == PEEK:
            pa = self._peek_mode_t
            p.Shadow = False
            p.ArmL = p.ArmR = 45
            p.StrL = p.StrR = 1
            m = self._peek_mode
            if m == 0:
                p.HeadTilt = S(pa * 1.3) * 10
            elif m == 1:
                p.HeadTilt = S(pa * 6) * 3
            elif m == 2:
                p.ArmR, p.HeadTilt = 72 + S(pa * 12) * 18, -5
            elif m == 3:
                p.Tilt, p.HeadTilt = S(pa * 2.2) * 4, S(pa * 2.2) * 7
                p.ArmL, p.ArmR = 45 + S(pa * 2.2) * 4, 45 - S(pa * 2.2) * 4
            elif m == 4:
                p.HeadTilt = 10 + S(t * 1.2) * 3
                p.ArmL = p.ArmR = 40
        elif act == CLIMB:
            p.Shadow = self.sink < 6
            p.StrL = p.StrR = 1
            if a < 0.3:
                p.ArmL = p.ArmR = lerp(45, 54, a / 0.3)
                p.SquashY = 1 - 0.04 * smooth(a / 0.3)
            elif a < 1.15:
                u = ease_in_out((a - 0.3) / 0.85)
                p.ArmL = p.ArmR = lerp(54, -5, u)
                p.Tilt = S(a * 16) * 4
                p.LegL, p.LegR = S(a * 14) * 22, -S(a * 14) * 22
                p.LiftL, p.LiftR = max(0, S(a * 14)) * 8, max(0, -S(a * 14)) * 8
            else:
                k = clamp((a - 1.15) / 0.55, 0, 1)
                h = S(k * PI)
                p.BodyY = -h * 26
                p.SquashY, p.SquashX = 1 + (h - 0.3) * 0.1, 1 - (h - 0.3) * 0.07
                p.ArmL = p.ArmR = -5 + h * 50
                p.StrL = p.StrR = 1 if k < 0.7 else 0
        elif act in PHONE_ACTS:  # 双手捧着手机，低头认真看
            e = smooth(a / 0.35)
            p.Phone = e > 0.5
            # 手肘自然弯着、双手捧在胸前（不是两条胳膊直直地交叉伸过去）
            p.ArmL = p.ArmR = 0.0
            p.StrL = p.StrR = 0.0
            p.HoldL = p.HoldR = 1.0
            p.HeadDY = 4 * e
            p.HeadTilt = S(t * 0.7) * 2 + self.look_x * 1.5
            if act == TYPE:
                p.Typing = True
                p.HoldDL = (0.0, S(t * 31) * 2.2)
                p.HoldDR = (0.0, S(t * 27 + 1) * 2.2)
            elif act == PHONE_LIKE:
                bob = abs(S(a * 9)) * (1 - a / self.act_dur)
                p.BodyY = -bob * 10
                p.HeadTilt = S(a * 7) * 5
            if self.flick > 0:   # 右手拇指往上一划
                k = S((0.45 - self.flick) / 0.45 * PI)
                p.HoldDR = (p.HoldDR[0] - 2.0 * k, p.HoldDR[1] - 7.0 * k)
        elif act == SIGN:  # 右手举着“想发作品”的小牌子，左手挥一挥
            e = env(a, self.act_dur, 0.4)
            p.Sign = e > 0.3
            p.ArmR = lerp(-40, 58, e) + S(t * 3) * 4 * e
            p.ArmL = -40 + S(t * 6) * 18 * e
            p.StrL = p.StrR = 1
            p.HeadTilt = -6 * e + S(t * 2) * 2
            p.BodyY = -abs(S(t * 4)) * 4 * e
        elif act == ALERT:  # 着急：两只手举高挥舞
            p.ArmL = 100 + S(t * 12) * 22
            p.ArmR = 100 + S(t * 12 + 1.6) * 22
            p.StrL = p.StrR = 1
            p.BodyY = -abs(S(t * 8)) * 8
            p.HeadTilt = S(t * 8) * 5
        elif act == SULK:  # 小脾气：叉腰、扭头、冒青筋
            e = env(a, self.act_dur, 0.4)
            p.StrL = p.StrR = 0
            p.HeadTilt = -9 * e + S(t * 1.1) * 1.5
            p.Tilt = -2 * e
            p.Anger = e > 0.3
            p.SquashY = 1 + abs(S(t * 3)) * 0.01
        return p

    @staticmethod
    def _apply_dance(p: Pose, tab, a: float, dur: float):
        """按 10 帧/秒取舞蹈关键帧，相邻帧之间平滑插值（和原版完全一样）。"""
        n = len(tab)
        f = (a * 10) % n
        i1 = int(f)
        i0, i2, i3 = (i1 - 1 + n) % n, (i1 + 1) % n, (i1 + 2) % n
        u = f - i1
        v = [lerp(tab[i1][c], tab[i2][c], u) if c in (10, 12) else catmull(tab[i0][c], tab[i1][c], tab[i2][c],
                                                                            tab[i3][c], u)
             for c in range(len(tab[0]))]
        e = env(a, dur, 0.25)
        p.BodyX = v[0] * 0.7 * e
        bob = v[1] * e
        if bob < 0:
            p.BodyY = bob
        else:
            p.SquashY, p.SquashX = 1 - bob / 300, 1 + bob / 700
        p.HeadDX = (v[2] + v[4] * 6) * e
        p.Tilt = v[3] * 0.8 * e
        p.HeadTilt = v[3] * 0.5 * e
        p.SquashX *= 1 - 0.16 * abs(v[4]) * e
        p.LegL, p.LegR = v[5] * e, v[6] * e
        p.LiftL, p.LiftR = max(0, v[7]) * e, max(0, v[8]) * e
        p.ArmL, p.StrL = v[9], (v[10] if e > 0.1 else 0)
        p.ArmR, p.StrR = v[11], (v[12] if e > 0.1 else 0)
        hi = smooth((max(v[9] * v[10], v[11] * v[12]) - 80) / 25) * e
        p.ArmDY = -30 * hi
        p.HeadDY = 14 * hi
        p.ArmScale = 1 + 0.3 * hi

    def _smooth_arms(self, dt):
        """手臂角度和伸直程度平滑过渡：先把手肘伸直，再抬手。"""
        tp = self.build_pose()
        dance = self.act in DANCE_ACTS
        ka = min(1.0, dt * (40 if dance else 22))
        ks = min(1.0, dt * (20 if dance else 14))
        s = self._arm
        s[0] += (tp.ArmL - s[0]) * ka
        s[1] += (tp.ArmR - s[1]) * ka
        s[2] += (tp.StrL - s[2]) * ks
        s[3] += (tp.StrR - s[3]) * ks
        kh = min(1.0, dt * 10)
        s[4] += (tp.HoldL - s[4]) * kh
        s[5] += (tp.HoldR - s[5]) * kh

    def pose(self) -> Pose:
        """这一帧要画的姿态（手臂用平滑后的值）。"""
        p = self.build_pose()
        armL, armR, strL, strR, holdL, holdR = self._arm
        p.StrL, p.StrR = strL, strR
        p.HoldL, p.HoldR = holdL, holdR
        p.ArmL = armL * smooth(strL * 1.4)
        p.ArmR = armR * smooth(strR * 1.4)
        self.last_pose = p
        return p
