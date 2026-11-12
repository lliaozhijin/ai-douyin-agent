"""互动的节奏：点不点赞、评不评论由她自己（身份卡 + 记忆）判断，程序只管“一天的额度匀着用”。

原来有“点赞倾向”（只赞最近看过的前百分之几），现在取消了：
- 不好看的视频直接划走；感兴趣的看完以后，由模型按她的性格、兴趣和记忆决定要不要点赞 / 评论 / 收藏。
  记忆里有她最近的习惯（最近看完的 30 个里赞了几个、评论了几个，今天同一类已经赞了几个），让她自己掂量。
- 程序只做三件事：
  1. 每天的额度按活跃时段匀着用——赞 / 评论 / 收藏得比进度快就先歇一歇（10 分的才破例），不会上午就把一天的用完；
  2. 同一个视频只评论一次（永久，见 runner）；
  3. 给别人的评论点赞：模型逐条打分，只赞 8 分以上的，一个视频一般 1 条、最多 2 条。
- 收藏：同一类今天收藏过 2 个以后要 9 分以上才考虑，3 个以上不再收。
"""
from __future__ import annotations

import math
import random
from datetime import datetime, time as dtime, timedelta

from ..db import Database
from .budget import parse_hhmm

BURST = 3              # 允许比匀速进度多赞几个
COLLECT_BURST = 2      # 收藏额度少：只允许比匀速进度多收 2 个
COLLECT_TOPIC_SOFT = 2  # 同一类今天收藏过几个以后提高标准
COLLECT_TOPIC_HARD = 3  # 同一类今天收藏过几个以后不再收
COMMENT_MIN = 8        # 别人的评论至少几分才赞
COMMENT_MAX = 2        # 一个视频最多赞几条评论
POST_BURST = 2         # 评论额度少：只允许比匀速进度多发 2 条


class LikePolicy:
    """点赞 / 评论 / 收藏的节奏。喜不喜欢由模型按身份卡和记忆判断，这里只管额度和进度。"""

    def __init__(self, db: Database, rnd: random.Random | None = None):
        self.db = db
        self.rnd = rnd or random.Random()

    @staticmethod
    def pace_allowance(limit: float, used: float, rules: dict, now: datetime | None = None,
                       burst: float = BURST) -> float:
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
        return limit * frac + burst

    # ---------- 给别人的评论点赞 ----------
    def comment_gate(self, rules: dict, used: float = 0, now: datetime | None = None) -> dict:
        """看了的视频（过了感兴趣门槛的）都会看评论区、判断要不要给评论点赞，不管视频本身赞没赞；
        这里只管今天的额度和进度：返回 {ok, why}。"""
        limit = float((rules.get("limits") or {}).get("comment_likes") or 0)
        if limit <= 0 or used >= limit:
            return {"ok": False, "why": "今天赞评论的额度用完了"}
        if used >= self.pace_allowance(limit, used, rules, now):
            return {"ok": False, "why": "今天赞评论有点快了，先忍住"}
        return {"ok": True, "why": ""}

    # ---------- 收藏 ----------
    def collect_gate(self, interest: int, topic: str, rules: dict, used: float = 0,
                     now: datetime | None = None) -> dict:
        """这个视频要不要让她考虑收藏：返回 {ok, why, note}。note 是给她的提醒（写进提示词），比如同类已经收藏了好几个。"""
        limit = float((rules.get("limits") or {}).get("collects") or 0)
        if limit <= 0 or used >= limit:
            return {"ok": False, "why": "今天收藏的额度用完了", "note": ""}
        if used >= self.pace_allowance(limit, used, rules, now, COLLECT_BURST) and interest < 10:
            return {"ok": False, "why": "今天收藏得有点快了，先不收（10 分的才破例）", "note": ""}
        if topic:
            day = (now or datetime.now()).strftime("%Y-%m-%d")
            same = self.db.query("SELECT COUNT(*) AS n FROM videos WHERE collected=1 AND topic=? AND created_at >= ?",
                                 (topic, day))[0]["n"]
            if same >= COLLECT_TOPIC_HARD:
                return {"ok": False, "why": f"今天已经收藏了 {same} 个「{topic}」，够了", "note": ""}
            if same >= COLLECT_TOPIC_SOFT:
                if interest < 9:
                    return {"ok": False, "why": f"今天已经收藏了 {same} 个「{topic}」，这个不够特别", "note": ""}
                return {"ok": True, "why": "",
                        "note": f"你今天已经收藏了 {same} 个「{topic}」类的视频，除非这个明显比之前的都更有用、更想反复看，否则别再收藏。"}
        return {"ok": True, "why": "", "note": ""}

    # ---------- 视频点赞 ----------
    def judge(self, interest: int, author: str, topic: str, rules: dict, mood_key: str = "",
              used: float = 0, now: datetime | None = None) -> dict:
        """这个视频能不能让她考虑点赞：只看今天的额度和进度，喜不喜欢由她自己判断。返回 {ok, why}。"""
        limit = float((rules.get("limits") or {}).get("likes") or 0)
        if limit <= 0 or used >= limit:
            return {"ok": False, "why": "今天点赞的额度用完了"}
        if used >= self.pace_allowance(limit, used, rules, now) and interest < 10:
            return {"ok": False, "why": "今天赞得有点快了，先歇一歇（10 分的才破例）"}
        return {"ok": True, "why": "由她自己判断"}

    # ---------- 自己发评论 ----------
    def post_gate(self, interest: int, topic: str, rules: dict, used: float = 0,
                  now: datetime | None = None) -> dict:
        """这个视频能不能让她考虑评论：只看今天的额度和进度，说不说由她自己判断。返回 {ok, invite, why}。"""
        limit = float((rules.get("limits") or {}).get("comments") or 0)
        if limit <= 0 or used >= limit:
            return {"ok": False, "invite": False, "why": "今天评论的额度用完了"}
        if used >= self.pace_allowance(limit, used, rules, now, POST_BURST) and interest < 10:
            return {"ok": False, "invite": False, "why": "今天评论得有点快了，先不说（10 分的才破例）"}
        return {"ok": True, "invite": False, "why": "由她自己判断"}

    def pick_comments(self, scored: list, rules: dict, used: float = 0, now: datetime | None = None) -> list[int]:
        """模型给热评打的分 [(编号, 分数)] → 真正要赞的编号：只赞够分的，一般 1 条，特别精彩才 2 条，按进度封顶。"""
        good = sorted(((i, s) for i, s in scored if s >= COMMENT_MIN), key=lambda x: -x[1])
        if not good:
            return []
        great = sum(1 for _, s in good if s >= 9)
        n = min(1 + (great >= 2), COMMENT_MAX, len(good))
        limit = float((rules.get("limits") or {}).get("comment_likes") or 0)
        room = math.ceil(self.pace_allowance(limit, used, rules, now) - used) if limit > 0 else 0
        room = min(room, int(limit - used)) if limit > 0 else 0
        return [i for i, _ in good[:max(0, min(n, room))]]


def impatient_skip(interest: int, mood_key: str, rnd: random.Random) -> bool:
    """真人偶尔再好看的也直接划走：无聊、困的时候更没耐心。9 分以上的不会。"""
    if interest >= 9:
        return False
    chance = {"bored": 0.15, "sleepy": 0.12, "grumpy": 0.08}.get(mood_key, 0.03)
    return rnd.random() < chance
