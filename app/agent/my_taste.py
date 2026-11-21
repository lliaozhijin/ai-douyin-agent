"""她自己的口味：不用你设置，跟着她每天的经历自己慢慢变。

人设里的“感兴趣的内容”只是她一开始的样子；之后爱看什么、看腻了什么，全由她自己的经历推着走：
- 刷到的视频：她给的兴趣分（高过“想看下去”的门槛越多越加分，低于门槛扣分）、点赞 / 收藏 / 评论了没有；
- 聊天里聊到的：私信里、评论区里对方说起的、她自己回的话里提到的那一类，都加一点（朋友安利的东西她会更留意）；
- 时间：每一类的分数每天自然回落 5%，好久没刷到、没聊到的慢慢就淡了；同一类刷得太多，她自己也会说“看腻了”。

每天（或者攒够一批经历）让她自己回头看一眼：每一类的分数和变化、这几天自己写的评论、和别人聊的话，
写一小段“我最近的口味”——一直喜欢的、最近迷上的、有点看腻的、听人说起想去看看的。
这一段会带进她的身份（刷视频、回私信、回评论、找主人聊天、写作品都是同一个她），刷视频时：
看腻的那一类兴趣分 -1，听人说起想看看的那一类 +1（就像人会多留意朋友提过的东西）。
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime

from ..db import Database, now_str, today_str
from .dedupe import canon_topic

log = logging.getLogger(__name__)

DECAY_PER_DAY = 0.95       # 每天自然回落多少（乘这个数）
SCORE_MIN, SCORE_MAX = -6.0, 10.0
PER_POINT = 0.12           # 兴趣分比门槛每高 1 分加多少（低 1 分扣多少）
LIKE_GAIN, COLLECT_GAIN, COMMENT_GAIN = 0.3, 0.5, 0.4
TALK_THEM, TALK_ME = 0.15, 0.25   # 对方聊到 / 她自己说到这一类
TALK_MAX_TOPICS = 3
REFRESH_HOURS = 20         # 隔这么久重新想一想自己最近的口味
REFRESH_MIN_SIGNALS = 8    # 至少有这么多新经历才值得想
REFRESH_BURST = 40         # 新经历攒到这么多，隔 3 小时就可以再想一次
PROFILE_KEY = "my_taste_profile"
SIGNALS_KEY = "my_taste_signals"
DECAY_KEY = "my_taste_decay_day"
LISTS = (("loves", "一直喜欢的"), ("rising", "最近迷上的"), ("tired", "有点看腻的"), ("curious", "听人说起、想去看看的"))


def _hit(topic: str, words: list[str]) -> str:
    """topic 是不是 words 里的某一类（一边包含另一边就算：“猫”和“橘猫”）。"""
    t = canon_topic(topic)
    if not t:
        return ""
    for w in words or []:
        c = canon_topic(w)
        if not c:
            continue
        short, long_ = sorted((c, t), key=len)
        # 两个字以上互相包含就算（“猫咪”和“橘猫咪”）；一个字的只认很短的词（“猫”和“猫咪”，不认“猫和狗的日常”）
        if c == t or (short in long_ and (len(short) >= 2 or len(long_) <= 2)):
            return w
    return ""


class MyTaste:
    def __init__(self, db: Database, router=None, memory=None):
        self.db = db
        self.router = router
        self.memory = memory       # 想好的口味顺便记进她的记忆（聊天时能说起“我最近迷上了……”）

    def seed_from_history(self, threshold: int = 6) -> int:
        """第一次用这个功能时：把最近两周刷过的视频补算进口味（越久以前的算得越轻），不用从零开始。只做一次。"""
        if self.db.get_json("my_taste_seeded", False):
            return 0
        self.db.set_json("my_taste_seeded", True)
        rows = self.db.query("SELECT topic, interest, liked, collected, comment, created_at FROM videos WHERE topic != '' "
                             "AND interest IS NOT NULL AND created_at >= date('now', '-14 day') ORDER BY id LIMIT 3000")
        now = datetime.now()
        n = 0
        for r in rows:
            try:
                age = max(0.0, (now - datetime.strptime(r["created_at"][:19], "%Y-%m-%d %H:%M:%S")).total_seconds() / 86400)
                interest = int(r["interest"])
            except (TypeError, ValueError):
                continue
            liked, collected, commented = bool(r["liked"]), bool(r["collected"]), bool(r["comment"])
            delta = ((interest - threshold) * PER_POINT + (LIKE_GAIN if liked else 0) + (COLLECT_GAIN if collected else 0)
                     + (COMMENT_GAIN if commented else 0)) * math.pow(DECAY_PER_DAY, age)
            self._bump(r["topic"], delta, seen=1, loved=int(interest >= 8), acted=int(liked or collected or commented))
            n += 1
        if n:
            log.info("她的口味：按最近两周刷过的 %d 个视频补算了一遍", n)
        return n

    # ---------- 记经历 ----------
    def _bump(self, topic: str, score: float = 0.0, seen: int = 0, loved: int = 0, acted: int = 0,
              talked: int = 0) -> None:
        topic = canon_topic(topic)
        if not topic:
            return
        now = now_str()
        self.db.execute(
            "INSERT INTO my_taste(topic, score, seen, loved, acted, talked, first_at, last_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(topic) DO UPDATE SET score = MAX(?, MIN(?, score + ?)), seen = seen + ?, loved = loved + ?, "
            "acted = acted + ?, talked = talked + ?, last_at = ?",
            (topic, max(SCORE_MIN, min(SCORE_MAX, score)), seen, loved, acted, talked, now, now,
             SCORE_MIN, SCORE_MAX, score, seen, loved, acted, talked, now))
        self.db.set_json(SIGNALS_KEY, int(self.db.get_json(SIGNALS_KEY, 0) or 0) + 1)

    def on_video(self, topic: str, interest: int, threshold: int, liked: bool = False, collected: bool = False,
                 commented: bool = False) -> None:
        """刷到一个视频：她给了几分、有没有点赞 / 收藏 / 评论。"""
        try:
            interest, threshold = int(interest or 0), int(threshold or 6)
        except (TypeError, ValueError):
            return
        delta = (interest - threshold) * PER_POINT + (LIKE_GAIN if liked else 0) + (COLLECT_GAIN if collected else 0) \
            + (COMMENT_GAIN if commented else 0)
        self._bump(topic, delta, seen=1, loved=int(interest >= 8), acted=int(bool(liked or collected or commented)))

    def on_talk(self, their: str, mine: str = "") -> list[str]:
        """私信、评论区里聊了一次：对方说的、她回的话里提到了哪几类（她认得的分类词），每类加一点。"""
        their, mine = (their or "").lower(), (mine or "").lower()
        if not their and not mine:
            return []
        hit = []
        for t in self.topics():
            if len(hit) >= TALK_MAX_TOPICS:
                break
            in_them, in_me = t in their, t in mine
            if in_them or in_me:
                self._bump(t, (TALK_THEM if in_them else 0) + (TALK_ME if in_me else 0), talked=1)
                hit.append(t)
        return hit

    def topics(self, limit: int = 60) -> list[str]:
        """她认得的分类词：自己口味表里的 + 最近刷视频用过的（长的在前，免得“猫”抢了“猫咪”）。"""
        words = [r["topic"] for r in self.db.query("SELECT topic FROM my_taste ORDER BY last_at DESC LIMIT ?", (limit,))]
        words += [r["topic"] for r in self.db.query(
            "SELECT topic, COUNT(*) AS n FROM videos WHERE topic != '' AND created_at >= date('now', '-30 day') "
            "GROUP BY topic ORDER BY n DESC LIMIT ?", (limit,))]
        out = list(dict.fromkeys(canon_topic(w) for w in words if canon_topic(w)))
        return sorted(out, key=len, reverse=True)

    # ---------- 淡 ----------
    def decay(self, now: datetime | None = None) -> None:
        """一天一次：每一类都自然回落一点（好久没刷到、没聊到的慢慢就淡了）。"""
        now = now or datetime.now()
        day = now.strftime("%Y-%m-%d")
        last = self.db.get_json(DECAY_KEY, "") or ""
        if last == day:
            return
        days = 1
        if last:
            try:
                days = max(1, min(60, (now.date() - datetime.strptime(last, "%Y-%m-%d").date()).days))
            except ValueError:
                days = 1
        self.db.set_json(DECAY_KEY, day)
        self.db.execute("UPDATE my_taste SET score = score * ?", (math.pow(DECAY_PER_DAY, days),))
        self.db.execute("DELETE FROM my_taste WHERE ABS(score) < 0.05 AND last_at < date('now', '-60 day')")

    # ---------- 读 ----------
    def rows(self, limit: int = 40) -> list[dict]:
        return self.db.query("SELECT * FROM my_taste ORDER BY score DESC LIMIT ?", (limit,))

    def profile(self) -> dict:
        p = self.db.get_json(PROFILE_KEY, {}) or {}
        return p if isinstance(p, dict) else {}

    def line(self) -> str:
        """带进她身份里的一小段：她最近的口味（没想过就返回空，用人设里一开始的兴趣）。"""
        p = self.profile()
        if not p.get("line") and not any(p.get(k) for k, _ in LISTS):
            return ""
        bits = [f"{label}：{'、'.join(p[k][:4])}" for k, label in LISTS if p.get(k)]
        return ("你最近的口味（是你这些天刷视频、和人聊天慢慢变成这样的，还会一直变）："
                + (str(p.get("line") or "").strip() + " " if p.get("line") else "") + "；".join(bits) + "。")

    def nudge(self, topic: str) -> tuple[int, str]:
        """刷到这一类时，口味带来的一点偏向：看腻了 -1，听人说起想看看 +1。返回（加减几分, 原因）。"""
        p = self.profile()
        if _hit(topic, p.get("tired") or []):
            return -1, "最近有点看腻这一类了"
        if _hit(topic, p.get("curious") or []):
            return 1, "最近听人聊起这一类，想多看看"
        return 0, ""

    # ---------- 自己回头想一想 ----------
    def due(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        n = int(self.db.get_json(SIGNALS_KEY, 0) or 0)
        at = self.profile().get("at") or ""
        try:
            hours = (now - datetime.strptime(at[:19], "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600 if at else 1e9
        except ValueError:
            hours = 1e9
        return (n >= REFRESH_MIN_SIGNALS and hours >= REFRESH_HOURS) or (n >= REFRESH_BURST and hours >= 3)

    def _material(self) -> str:
        persona = self.db.persona() or {}
        prev = self.profile()
        snap = prev.get("scores") or {}
        top = self.rows(12)
        low = [r for r in self.db.query("SELECT * FROM my_taste WHERE score < 0 ORDER BY score LIMIT 6")]

        def row_line(r: dict) -> str:
            was = snap.get(r["topic"])
            trend = "" if was is None else ("，比上次想的时候更喜欢了" if r["score"] - was >= 0.6
                                             else "，比上次想的时候淡了" if was - r["score"] >= 0.6 else "")
            return (f"- {r['topic']}：分数 {r['score']:.1f}，一共刷到 {r['seen']} 个（8 分以上 {r['loved']} 个），"
                    f"点赞 / 收藏 / 评论过 {r['acted']} 个，聊天里聊到 {r['talked']} 次{trend}")

        comments = [r["comment"] for r in self.db.query(
            "SELECT comment FROM videos WHERE comment != '' AND comment IS NOT NULL AND created_at >= date('now', '-3 day') "
            "ORDER BY id DESC LIMIT 8")]
        talks = [r["summary"] for r in self.db.query(
            "SELECT summary FROM memories WHERE kind IN ('dm','thread','at') AND day >= date('now', '-3 day') "
            "ORDER BY id DESC LIMIT 12")]
        today = self.db.query("SELECT topic, COUNT(*) AS n FROM videos WHERE topic != '' AND created_at >= ? "
                              "GROUP BY topic ORDER BY n DESC LIMIT 6", (today_str(),))
        parts = [f"你一开始（人设里）感兴趣的：{'、'.join(persona.get('interests') or []) or '（没写）'}"
                 + (f"；不喜欢的：{persona['avoid']}" if persona.get("avoid") else "")]
        if prev.get("line") or any(prev.get(k) for k, _ in LISTS):
            parts.append("你上次想的自己的口味：" + str(prev.get("line") or "") + " "
                         + "；".join(f"{label}：{'、'.join(prev[k])}" for k, label in LISTS if prev.get(k)))
        if top:
            parts.append("你对每一类视频现在的感觉（分数越高越喜欢，每天会自然淡一点）：\n" + "\n".join(row_line(r) for r in top))
        if low:
            parts.append("分数是负的（刷到了也提不起兴趣）：" + "、".join(f"{r['topic']}（{r['score']:.1f}）" for r in low))
        if today:
            parts.append("今天刷得最多的：" + "、".join(f"{r['topic']} {r['n']} 个" for r in today))
        if comments:
            parts.append("这几天你自己写的评论：\n" + "\n".join(f"- {c[:60]}" for c in comments))
        if talks:
            parts.append("这几天和别人聊的（私信、评论区）：\n" + "\n".join(f"- {t[:80]}" for t in talks))
        return "\n\n".join(parts)

    def refresh(self, system: str, force: bool = False) -> float:
        """回头看看这些天的自己，写一小段“我最近的口味”。返回花的钱。"""
        if self.router is None or (not force and not self.due()):
            return 0.0
        if not self.rows(1) and not force:
            return 0.0
        prompt = (
            "回头看看你这几天在抖音上的经历，想一想你自己最近的口味变成什么样了。\n\n"
            + self._material()
            + "\n\n要求：只根据上面的真实经历来想，不要编；口味可以慢慢变（迷上新东西、看腻了老东西、因为朋友聊起开始好奇），"
            "但不会一天就完全变样，你一开始喜欢的东西如果最近还常看、还喜欢就还在。类别用 2～4 个字的通用叫法（和上面的写法一致）。\n"
            '只输出 JSON：{"line": "用第一人称说说你最近的口味和变化（40 字以内，像日记里的一句话）", '
            '"loves": ["一直喜欢的，最多 4 个"], "rising": ["最近越来越爱看的，最多 3 个"], '
            '"tired": ["最近看腻了、有点不想再刷的，最多 3 个，没有就空"], '
            '"curious": ["别人聊起、你还没怎么刷到但想去看看的，最多 3 个，没有就空"]}'
        )
        try:
            from .brain import parse_json
            r = self.router.call("write", system, prompt, max_tokens=300, temperature=0.5)
            d = parse_json(r.text)
        except Exception:  # noqa: BLE001 —— 模型出错交给外面（下次再想，不影响刷视频）
            log.warning("想自己最近的口味失败", exc_info=True)
            raise
        old = self.profile()
        new = {"line": re.sub(r"\s+", " ", str(d.get("line") or "")).strip()[:80], "at": now_str(),
               "scores": {r["topic"]: round(float(r["score"]), 2) for r in self.rows(40)}}
        for k, _ in LISTS:
            vals = d.get(k) if isinstance(d.get(k), list) else []
            new[k] = list(dict.fromkeys(canon_topic(str(x)) for x in vals if canon_topic(str(x))))[:4]
        if not new["line"] and not any(new[k] for k, _ in LISTS):
            return float(getattr(r, "cost", 0.0) or 0.0)
        self.db.set_json(PROFILE_KEY, new)
        self.db.set_json(SIGNALS_KEY, 0)
        changed = [f"{label}：{'、'.join(new[k])}" for k, label in LISTS
                   if new[k] and set(new[k]) != set(old.get(k) or [])]
        self.db.add_event("taste", "她想了想自己最近的口味", (new["line"] + ("\n" if changed else "") + "；".join(changed)).strip())
        if new["line"] and self.memory is not None:
            try:
                self.memory.remember("self", f"我最近的口味：{new['line']}", tags=new.get("rising") or [],
                                     importance=3, dedupe=f"taste:{today_str()}")
            except Exception:  # noqa: BLE001
                log.debug("口味没记进记忆", exc_info=True)
        log.info("她想了想自己最近的口味：%s %s", new["line"], json.dumps({k: new[k] for k, _ in LISTS}, ensure_ascii=False))
        return float(getattr(r, "cost", 0.0) or 0.0)

    def listing(self) -> dict:
        """给“记忆 → 她的口味”页面看的。"""
        p = self.profile()
        snap = p.get("scores") or {}
        rows = []
        for r in self.rows(40):
            was = snap.get(r["topic"])
            rows.append({**r, "score": round(float(r["score"]), 2),
                         "trend": 0 if was is None else round(float(r["score"]) - float(was), 2)})
        return {"profile": {k: p.get(k) for k in ("line", "at", "loves", "rising", "tired", "curious")},
                "topics": rows, "waiting": int(self.db.get_json(SIGNALS_KEY, 0) or 0),
                "start": list((self.db.persona() or {}).get("interests") or [])}
