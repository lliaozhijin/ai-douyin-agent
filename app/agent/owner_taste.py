"""她对主人口味的了解：慢慢学会只把主人真想看的分享给 TA、只在这种视频下 @ TA。

不是每个视频都分享、都 @ 主人。她对“主人喜不喜欢某一类视频”的把握，用 Beta 分布记着（每位主人 × 每一类各一个）：
- 一开始什么都不知道，先验偏保守（α=1, β=2，平均 0.33）：没把握的不分享。
- 主人自己在聊天里说过喜欢 / 不喜欢看什么（整理主人印象卡时顺便记下）：喜欢 α+1.5，不喜欢 β+2。
- 分享 / @ 之后看主人的反应：说喜欢、接着聊这个视频 α+1；一般般 α、β 各 +0.2；说不喜欢 β+1.5；
  两天都没反应 β+0.5（可能是没兴趣，也可能只是忙，所以扣得轻）。
- 要不要分享：她自己特别喜欢（8 分以上）的视频才考虑；从这一类的 Beta 分布里抽一个数，过了线才分享
  （汤普森采样）：越有把握主人喜欢，越常分享；没试过的类别偶尔也会试一次，这样才学得到新东西；
  明确不喜欢的类别不再分享。最后分享不分享、说什么，还是她自己判断（主人的口味会写给模型看）。
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta

from ..db import Database, now_str
from .dedupe import canon_topic

log = logging.getLogger(__name__)

PRIOR_A, PRIOR_B = 1.0, 2.0
SHARE_BAR = 0.55           # 抽到的数过了这条线才分享
MIN_INTEREST = 8           # 她自己至少这么喜欢才考虑分享给主人
WAIT_HOURS = 48            # 分享以后等主人反应多久
GAIN = {"liked": (1.0, 0.0), "neutral": (0.2, 0.2), "disliked": (0.0, 1.5), "ignored": (0.0, 0.5)}
SAID_LIKE, SAID_DISLIKE = 1.5, 2.0
REACTION_WORDS = {"liked": "喜欢", "neutral": "一般", "disliked": "不喜欢", "ignored": "没反应"}


class OwnerTaste:
    def __init__(self, db: Database, rnd: random.Random | None = None):
        self.db = db
        self.rnd = rnd or random.Random()

    # ---------- 读 ----------
    def _row(self, owner: str, topic: str) -> dict:
        rows = self.db.query("SELECT * FROM owner_taste WHERE owner=? AND topic=?", (owner, topic))
        return rows[0] if rows else {"owner": owner, "topic": topic, "alpha": PRIOR_A, "beta": PRIOR_B, "said": ""}

    def _add(self, owner: str, topic: str, da: float, db_: float, said: str | None = None) -> None:
        topic = canon_topic(topic)
        if not owner or not topic:
            return
        self.db.execute(
            "INSERT INTO owner_taste(owner, topic, alpha, beta, said, updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(owner, topic) DO UPDATE SET alpha = alpha + ?, beta = beta + ?, "
            "said = CASE WHEN ? != '' THEN ? ELSE said END, updated_at = excluded.updated_at",
            (owner, topic, PRIOR_A + da, PRIOR_B + db_, said or "", now_str(), da, db_, said or "", said or ""))

    @staticmethod
    def mean(row: dict) -> float:
        return float(row["alpha"]) / max(1e-6, float(row["alpha"]) + float(row["beta"]))

    @staticmethod
    def evidence(row: dict) -> float:
        """除了先验以外，攒了多少证据（分享了几次、主人说过几次）。"""
        return max(0.0, float(row["alpha"]) + float(row["beta"]) - PRIOR_A - PRIOR_B)

    def allow(self, owner: str, topic: str, interest: int) -> tuple[bool, str]:
        """这个视频要不要考虑分享给 / @ 这位主人。返回 (要不要, 原因)。"""
        if interest < MIN_INTEREST:
            return False, f"她自己只有 {interest} 分喜欢，不值得打扰主人"
        topic = canon_topic(topic)
        if not topic:
            return False, "说不清这是哪一类视频"
        row = self._row(owner, topic)
        m, n = self.mean(row), self.evidence(row)
        if n >= 2 and m < 0.25:
            return False, f"主人不太爱看「{topic}」"
        draw = self.rnd.betavariate(float(row["alpha"]), float(row["beta"]))
        if draw >= SHARE_BAR:
            return True, (f"觉得主人会喜欢「{topic}」" if n >= 2 else f"不太确定主人爱不爱看「{topic}」，试一次")
        return False, f"没把握主人想看「{topic}」"

    def profile(self, owner: str, limit: int = 8) -> str:
        """写给模型看的一句话：她了解到的主人的口味。还没了解就返回空。"""
        rows = self.db.query("SELECT * FROM owner_taste WHERE owner=?", (owner,))
        rows = [r for r in rows if self.evidence(r) >= 1]
        if not rows:
            return ""
        rows.sort(key=lambda r: -self.evidence(r))
        likes = [r for r in rows if self.mean(r) >= 0.55][:limit]
        dislikes = [r for r in rows if self.mean(r) < 0.3][:limit]

        def word(r):
            n = self.evidence(r)
            sure = "很确定" if n >= 4 else ("比较确定" if n >= 2 else "有一点感觉")
            return f"{r['topic']}（{sure}{'，TA 自己说过' if r['said'] else ''}）"
        parts = []
        if likes:
            parts.append("爱看：" + "、".join(word(r) for r in likes))
        if dislikes:
            parts.append("不爱看：" + "、".join(word(r) for r in dislikes))
        return "；".join(parts)

    def overview(self, owner: str) -> list[dict]:
        """界面用：每一类的把握程度。"""
        out = []
        for r in self.db.query("SELECT * FROM owner_taste WHERE owner=? ORDER BY updated_at DESC", (owner,)):
            out.append({"topic": r["topic"], "like": round(self.mean(r), 2), "evidence": round(self.evidence(r), 1),
                        "said": r["said"]})
        out.sort(key=lambda x: (-x["evidence"], -x["like"]))
        return out

    # ---------- 写 ----------
    def shared(self, owner: str, way: str, v: dict, topic: str, said: str = "") -> int:
        """她刚把一个视频分享给主人（way=dm）/ 在评论里 @ 了主人（way=at）：记下来，等主人的反应。"""
        return self.db.execute(
            "INSERT INTO owner_shares(owner, way, video_id, video_url, topic, title, said, status, created_at) "
            "VALUES(?,?,?,?,?,?,?,'sent',?)",
            (owner, way, v.get("video_id") or "", v.get("url") or "", canon_topic(topic), (v.get("desc") or "")[:60],
             said[:120], now_str())).lastrowid

    def waiting(self, owner: str, way: str = "") -> list[dict]:
        cutoff = (datetime.now() - timedelta(hours=WAIT_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
        return self.db.query("SELECT * FROM owner_shares WHERE owner=? AND status='sent' AND created_at >= ?"
                             + (" AND way=?" if way else "") + " ORDER BY id DESC LIMIT 5",
                             (owner, cutoff, way) if way else (owner, cutoff))

    def react(self, share_id: int, reaction: str, note: str = "") -> None:
        """主人对某次分享的反应：liked / neutral / disliked / ignored。"""
        if reaction not in GAIN:
            return
        rows = self.db.query("SELECT * FROM owner_shares WHERE id=? AND status='sent'", (share_id,))
        if not rows:
            return
        r = rows[0]
        da, db_ = GAIN[reaction]
        self._add(r["owner"], r["topic"], da, db_)
        self.db.execute("UPDATE owner_shares SET status=?, feedback=?, resolved_at=? WHERE id=?",
                        (reaction, note[:120], now_str(), share_id))
        log.info("主人 %s 对「%s」的反应：%s", r["owner"], r["topic"], REACTION_WORDS[reaction])

    def expire(self) -> int:
        """分享出去两天都没反应的：算“没兴趣”（扣得轻）。"""
        cutoff = (datetime.now() - timedelta(hours=WAIT_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
        rows = self.db.query("SELECT id FROM owner_shares WHERE status='sent' AND created_at < ?", (cutoff,))
        for r in rows:
            self.react(r["id"], "ignored")
        return len(rows)

    def learn_said(self, owner: str, likes: list, dislikes: list) -> None:
        """主人在聊天里自己说过喜欢 / 不喜欢看哪类视频。同一句话（同一类、同样的态度）只算一次：
        整理印象卡时每次都会再读到以前说过的话，不能一直加。态度变了（以前说喜欢、现在说不喜欢）才再算。"""
        for items, word, da, db_ in ((likes, "喜欢", SAID_LIKE, 0.0), (dislikes, "不喜欢", 0.0, SAID_DISLIKE)):
            for t in (items or [])[:6]:
                topic = canon_topic(str(t))
                if topic and self._row(owner, topic).get("said") != word:
                    self._add(owner, topic, da, db_, said=word)

    def reset(self, owner: str) -> None:
        self.db.execute("DELETE FROM owner_taste WHERE owner=?", (owner,))
        self.db.execute("UPDATE owner_shares SET status='neutral', resolved_at=? WHERE owner=? AND status='sent'",
                        (now_str(), owner))
