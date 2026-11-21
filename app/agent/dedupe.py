"""同样的内容不重复互动。

你定的规矩（2026-11）：
- 同一个内容（同一个梗、同一段素材、换了标题的搬运）——点赞、评论、收藏、分享（分享给主人 / 在评论里 @ 人来看）
  每一样**永远只做一次**。认“同一个内容”用 app/fingerprint.py 的文案指纹 + 画面指纹。
- 同一类视频（她看的时候自己判断的分类，比如“猫”“火锅”）——这几样**每天各只做一次**。
  分类词由模型给出；为了“同一类用同一个词”，看视频时会把她最近用过的分类词告诉模型，让它尽量沿用。

程序只负责“这一样还能不能做”，做不做还是她自己判断。
"""
from __future__ import annotations

import logging
import re

from ..db import Database, now_str, today_str
from ..fingerprint import bands, fingerprint, from_hex, same_content, to_hex

log = logging.getLogger(__name__)

ACTIONS = ("like", "comment", "collect", "share")
WORDS = {"like": "点赞", "comment": "评论", "collect": "收藏", "share": "分享 / @ 人来看"}
COLUMNS = {"like": "liked", "comment": "commented", "collect": "collected", "share": "shared"}
RECENT = 500         # 最近互动过的这么多条逐条比（短文案的相似要逐条算）；更早的按指纹分段索引查
_SUFFIX = re.compile(r"(视频|日常|类|相关|合集|系列)$")


def canon_topic(topic: str) -> str:
    """分类词统一一下写法：去掉空白和“视频 / 日常 / 类”这种尾巴（“猫咪日常”→“猫咪”）。"""
    t = re.sub(r"[\s#·、,，。!！?？]+", "", str(topic or "")).lower()
    t2 = _SUFFIX.sub("", t)
    return (t2 or t)[:8]


class ContentMarks:
    def __init__(self, db: Database):
        self.db = db

    def known_topics(self, limit: int = 30) -> list[str]:
        """她最近 30 天用过的分类词（用得多的在前）：看视频时告诉模型，同一类尽量用同一个词。"""
        rows = self.db.query(
            "SELECT topic, COUNT(*) AS n FROM videos WHERE topic != '' AND created_at >= date('now', '-30 day') "
            "GROUP BY topic ORDER BY n DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            t = canon_topic(r["topic"])
            if t and t not in out:
                out.append(t)
        return out

    def check(self, v: dict, topic: str, image: bytes | None = None) -> dict:
        """这个视频哪几样互动不能再做了。返回 {"fp": 指纹, "topic": 统一后的分类, "blocked": {动作: 原因}}。"""
        fp = fingerprint(v.get("desc") or "", image)
        topic = canon_topic(topic)
        blocked: dict[str, str] = {}
        today = today_str()
        rows = self._candidates(v, fp, today)
        for r in rows:
            same = (v.get("video_id") and r["video_id"] == v.get("video_id")) or same_content(
                fp, {"th": from_hex(r["text_hash"]), "ih": from_hex(r["img_hash"]), "txt": r["txt"]})
            if same:
                for a in ACTIONS:
                    if r[COLUMNS[a]] and a not in blocked:
                        blocked[a] = f"同样的内容以前{WORDS[a]}过了（《{(r['title'] or '')[:20]}》，{r['day']}）"
            if topic and r["day"] == today and canon_topic(r["topic"]) == topic:
                for a in ACTIONS:
                    if r[COLUMNS[a]] and a not in blocked:
                        blocked[a] = f"今天已经{WORDS[a]}过一个「{topic}」类的视频了，同一类每天只{WORDS[a]}一次"
        return {"fp": fp, "topic": topic, "blocked": blocked}

    def _candidates(self, v: dict, fp: dict, today: str) -> list[dict]:
        """要比的那些：最近的几百条 + 今天的 + 同一个视频 + 指纹有一段完全相同的（再久以前的也找得到）。"""
        rows = {r["id"]: r for r in self.db.query(f"SELECT * FROM content_marks ORDER BY id DESC LIMIT {RECENT}")}
        conds, args = ["day=?"], [today]
        if v.get("video_id"):
            conds.append("video_id=?")
            args.append(v["video_id"])
        for prefix, h in (("b", fp.get("th")), ("i", fp.get("ih"))):
            for i, part in enumerate(bands(h)):
                if part is not None:
                    conds.append(f"{prefix}{i}=?")
                    args.append(part)
        for r in self.db.query(f"SELECT * FROM content_marks WHERE {' OR '.join(conds)} LIMIT 2000", tuple(args)):
            rows.setdefault(r["id"], r)
        return sorted(rows.values(), key=lambda r: -r["id"])

    def record(self, v: dict, chk: dict, done: dict) -> None:
        """她这次对这个视频做了哪几样：记下来，以后同样的内容 / 今天同一类的就不再做。什么都没做不记。"""
        if not any(done.get(a) for a in ACTIONS):
            return
        fp = chk.get("fp") or fingerprint(v.get("desc") or "")
        self.db.execute(
            "INSERT INTO content_marks(video_id, text_hash, img_hash, txt, topic, title, liked, commented, collected, "
            "shared, day, created_at, b0, b1, b2, b3, i0, i1, i2, i3) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (v.get("video_id") or "", to_hex(fp.get("th")), to_hex(fp.get("ih")), fp.get("txt") or "",
             chk.get("topic") or "", (v.get("desc") or "")[:60], int(bool(done.get("like"))),
             int(bool(done.get("comment"))), int(bool(done.get("collect"))), int(bool(done.get("share"))),
             today_str(), now_str(), *bands(fp.get("th")), *bands(fp.get("ih"))))
