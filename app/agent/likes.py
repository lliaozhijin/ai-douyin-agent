"""点赞策略：推荐页越刷越准、满屏都是她喜欢的视频时，也不会见一个赞一个。

真人刷抖音：大多数视频看看就划走，“还不错”的看完就够了，只有真正打动自己的才点赞。
所以这里不用固定分数线，而是三条一起判断：

1. 相对门槛：和她最近看过的约 30 个视频比，分数排进前 25%（“点赞倾向”可调：挑剔 15% / 适中 25% / 大方 40%）
   才考虑点赞，而且不低于 7 分（挑剔 8 分）。推荐页全是喜欢的内容时门槛自动变高，只赞其中最喜欢的；
   分数一样的视频按比例随机挑，不会因为全是 8 分就全赞。
2. 一天的赞匀着发：每天的点赞上限按活跃时段平均摊开，赞得比进度快就先忍住，只有 10 分的才破例。
3. 同类内容越赞越腻：今天已经赞过这个作者、或者赞过 3 个以上同一类（比如都是猫），再遇到就不太想赞了。

心情只让比例小幅浮动（开心时大方一点、烦的时候挑剔一点），不会越刷越松。
"""
from __future__ import annotations

import random
from datetime import datetime, time as dtime, timedelta

from ..db import Database
from .budget import parse_hhmm

TENDENCY = {"picky": 0.15, "normal": 0.25, "generous": 0.40}
TENDENCY_LABELS = {"picky": "挑剔", "normal": "适中", "generous": "大方"}
MIN_SCORE = {"picky": 8, "normal": 7, "generous": 7}
WINDOW = 30            # 和最近多少个视频比
COLD_START = 8         # 看过的视频少于这个数时还没法比，只按最低分 + 一个默认概率
MOOD_SHIFT = {"excited": 0.05, "happy": 0.03, "clingy": 0.02, "bored": -0.05, "grumpy": -0.06,
              "sulky": -0.06, "sleepy": -0.03}
BURST = 3              # 允许比匀速进度多赞几个
TOPIC_SOFT = 3         # 同一类今天赞过几个以后开始腻


class LikePolicy:
    def __init__(self, db: Database, rnd: random.Random | None = None):
        self.db = db
        self.rnd = rnd or random.Random()

    # ---------- 数据 ----------
    def _recent_scores(self) -> list[int]:
        rows = self.db.query("SELECT interest FROM videos WHERE interest IS NOT NULL ORDER BY id DESC LIMIT ?",
                             (WINDOW - 1,))
        return [int(r["interest"]) for r in rows]

    def _liked_today(self) -> list[dict]:
        day = datetime.now().strftime("%Y-%m-%d")
        return self.db.query("SELECT author, topic FROM videos WHERE liked=1 AND created_at >= ?", (day,))

    # ---------- 规则 ----------
    @staticmethod
    def rank_chance(score: int, window: list[int], q: float) -> float:
        """score 在 window（含它自己）里能不能排进前 q：能就 1，不能就 0，正好卡在一串同分里就按比例给概率。"""
        scores = list(window) + [score]
        k = q * len(scores)
        above = sum(1 for s in scores if s > score)
        equal = sum(1 for s in scores if s == score)
        if above >= k:
            return 0.0
        if above + equal <= k:
            return 1.0
        return max(0.0, min(1.0, (k - above) / equal))

    @staticmethod
    def pace_allowance(limit: float, used: float, rules: dict, now: datetime | None = None) -> float:
        """按活跃时段的进度，现在最多该赞到第几个（含 BURST 的富余）。"""
        now = now or datetime.now()
        start_t = parse_hhmm(rules.get("active_start", "09:00"), dtime(9, 0))
        end_t = parse_hhmm(rules.get("active_end", "23:00"), dtime(23, 0))
        start = datetime.combine(now.date(), start_t)
        end = datetime.combine(now.date(), end_t)
        if end <= start:                      # 跨午夜，例如 20:00–02:00
            if now < end:
                start -= timedelta(days=1)
            else:
                end += timedelta(days=1)
        span = max(60.0, (end - start).total_seconds())
        frac = max(0.0, min(1.0, (now - start).total_seconds() / span))
        return limit * frac + BURST

    def judge(self, interest: int, author: str, topic: str, rules: dict, mood_key: str = "calm",
              likes_used: float = 0, now: datetime | None = None) -> dict:
        """这个视频该不该点赞：返回 {p: 点赞概率, why: 原因}（还没掷骰子）。"""
        tendency = rules.get("like_tendency") or "normal"
        if tendency not in TENDENCY:
            tendency = "normal"
        q = max(0.05, min(0.6, TENDENCY[tendency] + MOOD_SHIFT.get(mood_key, 0.0)))
        if interest < MIN_SCORE[tendency]:
            return {"p": 0.0, "why": f"{interest} 分，还不到想点赞的程度"}
        window = self._recent_scores()
        if len(window) < COLD_START:
            p = min(1.0, q * 2.4) if interest < 9 else 1.0      # 刚开始没得比：8 分左右的大约一半会赞
            why = "刚开始刷，还没得比"
        else:
            p = self.rank_chance(interest, window, q)
            why = f"在最近 {len(window) + 1} 个视频里排前 {round(q * 100)}%" if p >= 1 else (
                "最近好看的视频太多了，这个不算最出挑" if p <= 0 else "和最近几个差不多好看，看心情")
        if p <= 0:
            return {"p": 0.0, "why": why}
        limit = float((rules.get("limits") or {}).get("likes") or 0)
        if limit > 0 and likes_used >= self.pace_allowance(limit, likes_used, rules, now) and interest < 10:
            return {"p": 0.0, "why": "今天赞得有点快了，先忍住（10 分的才破例）"}
        liked = self._liked_today()
        if author and any((r["author"] or "") == author for r in liked):
            p *= 0.4
            why += "；今天已经赞过这个作者"
        if topic:
            same = sum(1 for r in liked if (r["topic"] or "") == topic)
            if same >= TOPIC_SOFT:
                p *= 0.55 ** (same - TOPIC_SOFT + 1)
                why += f"；今天已经赞了 {same} 个「{topic}」，有点腻"
        return {"p": round(p, 3), "why": why}

    def roll(self, verdict: dict) -> bool:
        return verdict["p"] > 0 and self.rnd.random() < verdict["p"]


def impatient_skip(interest: int, mood_key: str, rnd: random.Random) -> bool:
    """真人偶尔再好看的也直接划走：无聊、困的时候更没耐心。9 分以上的不会。"""
    if interest >= 9:
        return False
    chance = {"bored": 0.15, "sleepy": 0.12, "grumpy": 0.08}.get(mood_key, 0.03)
    return rnd.random() < chance


def tendency_label(key: str) -> str:
    return TENDENCY_LABELS.get(key, TENDENCY_LABELS["normal"])

