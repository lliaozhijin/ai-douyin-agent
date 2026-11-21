"""每日额度：看视频、点赞、评论、回复 @、回复私信、花费。"""
from __future__ import annotations

from datetime import datetime, time as dtime

from ..db import Database

KIND_LABELS = {
    "videos": "看视频",
    "likes": "点赞",
    "collects": "收藏视频",
    "comments": "发评论",
    "replies_at": "回复 @",
    "replies_dm": "回复私信",
    "comment_likes": "赞别人的评论",
    "comment_replies": "回复别人的评论",
    "thread_replies": "接着聊评论",
    "at_friends": "@ 好友看视频",
    "publishes": "发布作品",
    "cost": "模型花费",
}


# 回复找她的人（评论区 @ 她、回复她的评论、评论她的作品、私信）不设上限：有人找她就回。这几项只统计今天回了多少
UNLIMITED = ("replies_at", "replies_dm", "thread_replies")


class Budget:
    def __init__(self, db: Database):
        self.db = db

    def remaining(self, kind: str) -> float:
        limit = float(self.db.rules()["limits"].get(kind) or 0)
        used = float(self.db.usage().get(kind) or 0)
        return max(0.0, limit - used)

    def can(self, kind: str) -> bool:
        if kind != "cost" and self.remaining("cost") <= 0:
            return False
        return kind in UNLIMITED or self.remaining(kind) > 0

    def use(self, kind: str, n: float = 1) -> None:
        self.db.add_usage(kind, n)

    def summary(self) -> list[dict]:
        rules = self.db.rules()["limits"]
        usage = self.db.usage()
        out = []
        for k, label in KIND_LABELS.items():
            limit = 0.0 if k in UNLIMITED else float(rules.get(k) or 0)
            used = float(usage.get(k) or 0)
            out.append({
                "key": k, "label": label, "used": round(used, 2), "limit": limit, "unlimited": k in UNLIMITED,
                "pct": 0 if limit <= 0 else min(100, round(used / limit * 100)),
            })
        return out


def parse_hhmm(s: str, fallback: dtime) -> dtime:
    try:
        h, m = str(s).strip().split(":")
        return dtime(int(h), int(m))
    except (ValueError, AttributeError):
        return fallback


def in_active_hours(rules: dict, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    start = parse_hhmm(rules.get("active_start", "09:00"), dtime(9, 0))
    end = parse_hhmm(rules.get("active_end", "23:00"), dtime(23, 0))
    t = now.time()
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end  # 跨午夜，例如 20:00-02:00
