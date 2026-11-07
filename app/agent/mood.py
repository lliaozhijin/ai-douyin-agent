"""情绪系统：她有一个随时间、随经历变化的心情。

- 性格（人设里的滑块）是长期不变的：撒娇程度、小脾气、情绪起伏……
- 心情是此刻的状态：开心、兴奋、想撒娇、有点小脾气、委屈、犯困、无聊、平静。
  用两个数表示：愉快度 v（-100 ~ 100）和精力 e（0 ~ 100），再加上“最近一件事”带来的倾向。
- 每件事（刷到喜欢的视频、有人回她的评论、主人摸头、被拖来拖去……）会推动这两个数；
  没有新的事时，心情慢慢回到当天的基调（每天早上随机定一次）。
- 心情会写进给模型的提示词（决定说话语气），也会影响行为（划视频快慢、想不想发作品）和桌宠的动作。

心情只影响语气，不会让她违反任何硬性规则：公开评论区里，小脾气和委屈都只是轻微、可爱的。
"""
from __future__ import annotations

import random
import threading
import time
from datetime import datetime

MOODS: dict[str, dict] = {
    "happy": {"label": "开心", "emoji": "😊", "color": "#F06292",
              "style": "语气轻快明亮，爱夸人，偶尔“嘿嘿”“嘻嘻”"},
    "excited": {"label": "兴奋", "emoji": "🤩", "color": "#FF8A3D",
                "style": "语气兴奋，爱用感叹号，“哇”“太好了吧”，好奇心爆棚、爱追问"},
    "clingy": {"label": "想撒娇", "emoji": "🥺", "color": "#E879A6",
               "style": "软软地撒娇，爱用“嘛～”“人家”“好不好嘛”，可爱但不暧昧"},
    "grumpy": {"label": "有点小脾气", "emoji": "😤", "color": "#E2574C",
               "style": "有点小傲娇，偶尔“哼”“才没有呢”，话变短一点；只是可爱地嘟囔，不对任何人发火，不阴阳怪气"},
    "sulky": {"label": "有点委屈", "emoji": "🥲", "color": "#7C8CF8",
              "style": "委屈巴巴的，“呜”“好吧……”，但不卖惨、不让别人内疚"},
    "sleepy": {"label": "犯困", "emoji": "😪", "color": "#9AA5B8",
               "style": "迷迷糊糊、懒洋洋的，偶尔“哈欠～”，话少一点"},
    "bored": {"label": "无聊", "emoji": "😑", "color": "#A79F8E",
              "style": "有点提不起劲，“唔……”，但依然友好，看到有趣的会马上来精神"},
    "calm": {"label": "平静", "emoji": "🙂", "color": "#5FB49C",
             "style": "自然平常，温和放松"},
}

# 她心里的小声音：显示在仪表盘的心情卡片上，桌宠闲聊时也会说
INNER_LINES = {
    "happy": ["今天心情超好的～", "嘿嘿，刷到好多好玩的！", "开心到想转圈圈～", "{name}今天是快乐小鸭！"],
    "excited": ["哇哇哇，好想马上分享给大家！", "今天的视频也太精彩了吧！", "{name}的小宇宙要爆发啦！"],
    "clingy": ["主人～陪我一下下嘛～", "想被主人摸摸头～", "人家今天也很努力哦，夸夸我嘛～"],
    "grumpy": ["哼，才没有生气呢！", "不想说话，除非给我小蛋糕。", "哼唧……让{name}静静。"],
    "sulky": ["呜……有点委屈。", "算了，{name}自己消化一下……", "需要一点点安慰……"],
    "sleepy": ["哈～欠……眼皮在打架……", "再刷一个就睡……大概……", "好困好困……"],
    "bored": ["唔……今天的视频有点无聊。", "好想刷到点新鲜的……", "发呆中……"],
    "calm": ["今天也平平稳稳的～", "慢慢刷，慢慢看～", "一切都刚刚好。"],
}

# 事件：愉快度变化、精力变化、带来的心情倾向、倾向强度、原因
EVENTS: dict[str, tuple[float, float, str, float, str]] = {
    "video_love": (5, 3, "excited", 0.3, "刷到了超喜欢的视频{title}"),
    "video_ok": (1, -0.3, "", 0, ""),
    "video_meh": (-0.5, -0.4, "", 0, ""),
    "liked": (1, 0, "", 0, ""),
    "collected": (3, 1, "happy", 0.2, "收藏了一个想以后再看的视频{title}"),
    "commented": (2, 0, "happy", 0.15, "在{title}下面留了评论"),
    "at_friend": (3, 1, "happy", 0.3, "把好看的视频分享给了好朋友 {user}"),
    "thread_reply": (6, 2, "happy", 0.4, "{user} 回复了她的评论"),
    "dm": (3, 1, "happy", 0.2, "{user} 来找她聊天"),
    "dm_share": (4, 2, "excited", 0.3, "{user} 分享了视频给她"),
    "hostile": (-8, 0, "@grumpy", 0.45, "有人说了不太友好的话"),
    "pat": (6, 1, "clingy", 0.5, "主人摸了摸她的头"),
    "pat_much": (-6, 0, "grumpy", 0.6, "被主人一直摸头，有点小脾气了"),
    "tickle": (4, 2, "happy", 0.4, "被主人挠痒痒"),
    "drag": (-5, 0, "grumpy", 0.5, "被主人拎来拎去"),
    "dizzy": (-4, -2, "sulky", 0.4, "摔得头晕晕的"),
    "user_back": (5, 2, "clingy", 0.5, "主人回来啦"),
    "ignored": (-3, -1, "sulky", 0.3, "好久没人理她"),
    "publish_ok": (10, 5, "excited", 0.6, "作品发出去啦"),
    "publish_no": (-6, 0, "sulky", 0.5, "主人没让她发这个作品"),
    "captcha": (-3, 0, "@grumpy", 0.3, "刷着刷着被验证码打断了"),
    "rest": (2, 15, "", 0, ""),
}

HALF_LIFE_MIN = 40.0     # 心情回到当天基调的半衰期（分钟）
NUDGE_HALF_LIFE = 25.0   # “最近一件事”的影响的半衰期（分钟）
_LOCK = threading.RLock()


def _clamp(v: float, a: float, b: float) -> float:
    return a if v < a else b if v > b else v


class MoodEngine:
    def __init__(self, db, clock=time.time, rnd: random.Random | None = None):
        self.db = db
        self.clock = clock
        self.rnd = rnd or random.Random()

    # ---------- 内部状态 ----------
    def _traits(self) -> dict:
        t = (self.db.persona().get("traits") or {})
        return {k: float(t.get(k, d)) for k, d in
                (("energy", 60), ("cute", 60), ("temper", 35), ("volatility", 50), ("sharp", 25))}

    def _load(self) -> dict:
        now = self.clock()
        st = self.db.get_json("mood", {}) or {}
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if st.get("day") != day:
            tr = self._traits()
            base_v = self.rnd.uniform(-10, 25) + (tr["energy"] - 50) * 0.1
            base_e = _clamp(self.rnd.uniform(55, 80) + (tr["energy"] - 50) * 0.3, 20, 95)
            st = {"day": day, "v": base_v, "e": base_e, "base_v": base_v, "base_e": base_e,
                  "_init": True,
                  "nudge": None, "boredom": 0, "pats": [], "history": st.get("history", [])[-12:],
                  "reason": "新的一天开始啦", "updated": now}
        if st.pop("_init", False):
            st["e"] = self._energy_target(st, now)
        self._decay(st, now)
        return st

    def _energy_target(self, st: dict, now: float) -> float:
        h = datetime.fromtimestamp(now).hour
        adj = -60 if h < 6 else -10 if 13 <= h < 15 else -25 if h >= 23 else -10 if h >= 22 else 0
        return _clamp(st["base_e"] + adj, 5, 100)

    def _decay(self, st: dict, now: float) -> None:
        dt_min = max(0.0, (now - float(st.get("updated") or now)) / 60.0)
        if dt_min <= 0:
            return
        k = 0.5 ** (dt_min / HALF_LIFE_MIN)
        st["v"] = st["base_v"] + (st["v"] - st["base_v"]) * k
        st["e"] = self._energy_target(st, now) + (st["e"] - self._energy_target(st, now)) * k
        n = st.get("nudge")
        if n:
            n["w"] *= 0.5 ** (dt_min / NUDGE_HALF_LIFE)
            if n["w"] < 0.05:
                st["nudge"] = None
        st["updated"] = now

    def _pick(self, st: dict) -> tuple[str, int]:
        tr = self._traits()
        v, e = st["v"], st["e"]
        cute, temper = tr["cute"] / 50.0, tr["temper"] / 100.0
        scores = {
            "calm": 22.0,
            "happy": max(0.0, v) * 0.9,
            "excited": max(0.0, v - 15) * 0.6 + max(0.0, e - 65) * 0.8,
            "clingy": max(0.0, v + 10) * 0.18 * cute,
            "grumpy": max(0.0, -v) * 0.9 * (0.35 + temper),
            "sulky": max(0.0, -v) * 0.9 * (1.25 - temper),
            "sleepy": max(0.0, 40 - e) * 2.5,
            "bored": float(st.get("boredom") or 0) * 5.0,
        }
        n = st.get("nudge")
        if n and n.get("mood") in scores:
            scores[n["mood"]] += 70.0 * n["w"]
        best = max(scores, key=scores.get)
        return best, int(_clamp(scores[best], 15, 100))

    def _save(self, st: dict) -> None:
        self.db.set_json("mood", st)

    # ---------- 对外 ----------
    def event(self, kind: str, **kw) -> dict:
        """记一件影响心情的事。kw 里的 title / user 用来填原因。返回最新心情。"""
        spec = EVENTS.get(kind)
        if not spec:
            return self.current()
        with _LOCK:
            st = self._load()
            now = self.clock()
            tr = self._traits()
            dv, de, mood, w, why = spec
            if kind == "pat":  # 一分钟内摸了 3 次以上：开心变成小脾气
                st["pats"] = [t for t in st.get("pats", []) if now - t < 60] + [now]
                if len(st["pats"]) >= 4:
                    dv, de, mood, w, why = EVENTS["pat_much"]
            if mood.startswith("@"):  # 不开心的事：脾气大的闹小脾气，脾气好的觉得委屈
                mood = "grumpy" if tr["temper"] >= 50 else "sulky"
            k = 0.5 + tr["volatility"] / 100.0
            st["v"] = _clamp(st["v"] + dv * k, -100, 100)
            st["e"] = _clamp(st["e"] + de, 0, 100)
            if kind in ("video_meh",):
                st["boredom"] = min(10, int(st.get("boredom") or 0) + 1)
            elif kind in ("video_love", "video_ok", "dm", "dm_share", "thread_reply", "pat", "user_back"):
                st["boredom"] = 0
            if st["boredom"] >= 6 and kind == "video_meh" and not (st.get("nudge") or {}).get("mood") == "bored":
                mood, w, why = "bored", 0.4, "连刷了好几个没意思的视频"
            if mood:
                st["nudge"] = {"mood": mood, "w": _clamp(w * k, 0, 1), "at": now}
            if why:
                title = kw.get("title") or ""
                st["reason"] = why.format(title=f"《{title[:16]}》" if title else "", user=kw.get("user") or "有人")
            before = st.get("mood")
            st["mood"], st["intensity"] = self._pick(st)
            if st["mood"] != before:
                st.setdefault("history", []).append({"t": datetime.fromtimestamp(now).strftime("%H:%M"),
                                                     "mood": st["mood"], "reason": st.get("reason", "")})
                st["history"] = st["history"][-12:]
            self._save(st)
            return self._public(st)

    def current(self) -> dict:
        with _LOCK:
            st = self._load()
            st["mood"], st["intensity"] = self._pick(st)
            self._save(st)
            return self._public(st)

    def _public(self, st: dict) -> dict:
        m = MOODS[st["mood"]]
        return {"key": st["mood"], "label": m["label"], "emoji": m["emoji"], "color": m["color"],
                "intensity": st["intensity"], "reason": st.get("reason", ""),
                "valence": round(st["v"]), "energy": round(st["e"]),
                "history": list(reversed(st.get("history", [])[-6:]))}

    def inner_line(self, key: str | None = None) -> str:
        key = key or self.current()["key"]
        name = self.db.persona().get("name") or "雨雨"
        return self.rnd.choice(INNER_LINES.get(key) or INNER_LINES["calm"]).format(name=name)

    def prompt(self) -> str:
        """写进系统提示词的一段：她现在的心情和说话方式。"""
        m = self.current()
        style = MOODS[m["key"]]["style"]
        level = "一点点" if m["intensity"] < 40 else "比较" if m["intensity"] < 70 else "非常"
        return (f"【你此刻的心情】{level}{m['label']}（原因：{(m['reason'] or '没什么特别的').replace('她', '你')}）。"
                f"说话时自然地带出这种情绪：{style}。情绪只影响语气，不改变下面必须遵守的规则；"
                "在公开的评论区里，小脾气、委屈都只是轻微的、可爱的，不对任何人发火，不阴阳怪气，不让别人内疚。")

    def bias(self) -> dict:
        """心情对行为的影响：感兴趣门槛加减、每个视频停留时间倍数、今天想发作品的概率。"""
        k = self.current()["key"]
        threshold = {"excited": -1, "happy": -1, "bored": 1, "sleepy": 1, "grumpy": 1}.get(k, 0)
        dwell = {"excited": 1.2, "happy": 1.1, "bored": 0.6, "sleepy": 0.8, "grumpy": 0.85}.get(k, 1.0)
        publish = {"excited": 0.85, "happy": 0.75, "clingy": 0.6, "calm": 0.5, "bored": 0.45,
                   "grumpy": 0.3, "sulky": 0.25, "sleepy": 0.2}.get(k, 0.5)
        return {"threshold": threshold, "dwell": dwell, "publish": publish}


def mood_label(key: str) -> str:
    return MOODS.get(key, MOODS["calm"])["label"]


def intensity_word(n: float) -> str:
    return "一点点" if n < 40 else "比较" if n < 70 else "非常"

