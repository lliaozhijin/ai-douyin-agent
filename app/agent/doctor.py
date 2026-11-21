"""她的自检：用固定的规则（不调用模型、不花钱）发现“反复出问题”“卡住了”这类情况，
给你一张带证据的提醒卡，告诉你可能是什么原因、可以怎么办（参考 WhaleMaid 的“主动性”：规则发现问题 → 带证据的建议卡 → 你的反馈）。

- 每条规则看最近 24 小时的记录；同一条规则一天最多提醒一次，你点了“知道了”就不再追着提醒。
- 每张卡你可以点“有用 / 没用”，面板上显示最近 7 天 / 30 天提醒了几次、有用的占多少，方便看哪些规则真的帮上了忙。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ..db import Database, now_str, today_str

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS doctor_cards (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rule        TEXT NOT NULL,
    day         TEXT NOT NULL,
    title       TEXT NOT NULL,
    evidence    TEXT NOT NULL DEFAULT '',   -- 证据：最近几条相关记录（换行分开）
    suggestion  TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'open', -- open 待你看 / helpful 有用 / useless 没用 / dismissed 知道了
    created_at  TEXT NOT NULL,
    resolved_at TEXT NOT NULL DEFAULT '',
    UNIQUE(rule, day)
);
"""


def _since(hours: float = 24) -> str:
    return (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


class Doctor:
    def __init__(self, db: Database):
        self.db = db
        with db._lock:
            db.conn.executescript(SCHEMA)
            db.conn.commit()

    # ---------- 规则 ----------
    def _events(self, kind: str, title_like: str, hours: float = 24, limit: int = 5) -> list[dict]:
        return self.db.query("SELECT ts, title, detail FROM events WHERE kind=? AND title LIKE ? AND ts >= ? "
                             "ORDER BY id DESC LIMIT ?", (kind, title_like, _since(hours), limit))

    def _count_events(self, kind: str, title_like: str, hours: float = 24) -> int:
        return int(self.db.query("SELECT COUNT(*) AS n FROM events WHERE kind=? AND title LIKE ? AND ts >= ?",
                                 (kind, title_like, _since(hours)))[0]["n"])

    @staticmethod
    def _ev_lines(rows: list[dict]) -> str:
        return "\n".join(f"{r['ts'][5:16]} {r['title']}" + (f"：{r['detail'][:60]}" if r.get("detail") else "")
                         for r in rows)

    def rules(self, ctx: dict) -> list[dict]:
        """逐条规则检查，返回这次该提醒的（还没去重）。ctx：running 在运行 / active 在活跃时段 / sizes 磁盘占用。"""
        out = []
        fails = self.db.query("SELECT user_name, reason, handled_at FROM messages WHERE status='failed' AND handled_at >= ? "
                              "ORDER BY id DESC LIMIT 5", (_since(),))
        if len(fails) >= 3:
            out.append({"rule": "send_fail", "title": f"最近 24 小时有 {len(fails)} 条回复没发出去",
                        "evidence": "\n".join(f"{(f['handled_at'] or '')[5:16]} 回 {f['user_name']}：{(f['reason'] or '')[:60]}"
                                              for f in fails),
                        "suggestion": "可能是抖音网页改版了、找不到输入框或那条评论。到“设置”里点“保存当前页面结构”，"
                                      "把 data\\debug 里的文件发给开发者；这些消息在消息中心里可以手动回复。"})
        if self._count_events("error", "通知面板打不开") >= 2:
            out.append({"rule": "notice_panel", "title": "去看 @ 和评论回复时，通知面板总是打不开",
                        "evidence": self._ev_lines(self._events("error", "通知面板打不开")),
                        "suggestion": "评论区找她的消息暂时回不了。先在浏览器窗口里看看右上角“通知”能不能点开；"
                                      "能点开说明是定位过时了，把 data\\debug 里的文件发给开发者。"})
        if self._count_events("error", "评论没发出去%") >= 3:
            out.append({"rule": "comment_fail", "title": "她写好的评论好几次没发出去",
                        "evidence": self._ev_lines(self._events("error", "评论没发出去%")),
                        "suggestion": "可能是账号被限制评论、或者评论框定位过时了。用手机看看账号有没有收到提醒；"
                                      "暂时可以在“规则与预算”里把每天发评论的上限调成 0。"})
        n_llm = self._count_events("error", "模型调用失败")
        if n_llm >= 3:
            out.append({"rule": "llm_fail", "title": f"最近 24 小时模型调用失败了 {n_llm} 次",
                        "evidence": self._ev_lines(self._events("error", "模型调用失败")),
                        "suggestion": "检查“模型与 API Key”里的 Key 和余额、网络或代理；可以给“备用”任务分配另一家的模型，"
                                      "主模型出错时自动换过去。"})
        usage = self.db.usage()
        limit = float((self.db.rules().get("limits") or {}).get("cost") or 0)
        if limit > 0 and float(usage.get("cost") or 0) >= 0.8 * limit and datetime.now().hour < 15:
            out.append({"rule": "cost_early", "title": f"还没到下午，今天的模型花费已经用了 {usage.get('cost', 0):.2f} / {limit:g} 元",
                        "evidence": f"看了 {usage.get('videos', 0)} 个视频，回复 {int(usage.get('replies_at') or 0) + int(usage.get('replies_dm') or 0)} 条",
                        "suggestion": "花到上限以后她当天就不说话了。可以把“看视频”换成便宜一点的模型、少截几帧，"
                                      "或者把每天的花费上限调高一点。"})
        if ctx.get("running") and ctx.get("active"):
            last = self.db.query("SELECT MAX(created_at) AS t FROM videos")[0]["t"]
            busy = self.db.query("SELECT MAX(ts) AS t FROM events WHERE kind IN ('reply_dm','reply_at','thread_reply')")[0]["t"]
            newest = max(t for t in (last, busy, "") if t is not None)
            try:
                idle_min = (datetime.now() - datetime.strptime(newest, "%Y-%m-%d %H:%M:%S")).total_seconds() / 60
            except ValueError:
                idle_min = 0
            if newest and idle_min >= 45 and not ctx.get("resting"):
                out.append({"rule": "stall", "title": f"她已经 {int(idle_min)} 分钟没看视频、也没回消息了",
                            "evidence": f"最后一次看视频：{(last or '没有')[5:16]}；最后一次回消息：{(busy or '没有')[5:16]}",
                            "suggestion": "可能卡在某个页面上了。点“让她休息一下”再“开始刷视频”试试；"
                                          "还不行就点“显示浏览器”看看页面上是什么。"})
        pending = int(self.db.query("SELECT COUNT(*) AS n FROM messages WHERE status='pending'")[0]["n"])
        if pending >= 10:
            out.append({"rule": "pending_pile", "title": f"有 {pending} 条消息在“待你处理”里",
                        "evidence": "\n".join(f"{r['user_name']}：{(r['reason'] or '')[:50]}" for r in self.db.query(
                            "SELECT user_name, reason FROM messages WHERE status='pending' ORDER BY id DESC LIMIT 5")),
                        "suggestion": "这些是她没把握、或者碰到敏感话题没回的。去消息中心看看，能手动回的回一下。"})
        sizes = ctx.get("sizes") or {}
        if sizes.get("db", 0) >= 500 * 1048576 or sizes.get("browser_cache", 0) >= 1024 * 1048576:
            out.append({"rule": "disk", "title": "她的数据占的空间有点大了",
                        "evidence": f"数据库 {sizes.get('db', 0) / 1048576:.0f} MB，浏览器缓存 "
                                    f"{sizes.get('browser_cache', 0) / 1048576:.0f} MB",
                        "suggestion": "到“设置 → 存储空间”里点“现在整理”：旧记录会压缩归档，浏览器缓存会清掉（不影响登录）。"})
        return out

    # ---------- 记录和反馈 ----------
    def check(self, ctx: dict | None = None) -> list[dict]:
        """跑一遍规则：新发现的问题记成提醒卡（同一条规则一天一张）。返回新加的卡。"""
        new = []
        day = today_str()
        for c in self.rules(ctx or {}):
            cur = self.db.execute("INSERT OR IGNORE INTO doctor_cards(rule, day, title, evidence, suggestion, created_at) "
                                  "VALUES(?,?,?,?,?,?)", (c["rule"], day, c["title"], c["evidence"], c["suggestion"],
                                                          now_str()))
            if cur.rowcount:
                new.append(c)
                log.info("自检发现：%s", c["title"])
        return new

    def cards(self, include_closed: bool = False, limit: int = 20) -> list[dict]:
        where = "" if include_closed else "WHERE status='open'"
        return self.db.query(f"SELECT * FROM doctor_cards {where} ORDER BY id DESC LIMIT ?", (limit,))

    def feedback(self, cid: int, status: str) -> None:
        if status not in ("helpful", "useless", "dismissed"):
            raise ValueError("不认识的反馈")
        self.db.execute("UPDATE doctor_cards SET status=?, resolved_at=? WHERE id=?", (status, now_str(), cid))

    def effect(self) -> dict:
        """最近 7 天 / 30 天：提醒了几次、你说有用的几次。"""
        out = {}
        for days in (7, 30):
            since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            r = self.db.query("SELECT COUNT(*) AS n, SUM(status='helpful') AS h, SUM(status='useless') AS u "
                              "FROM doctor_cards WHERE day >= ?", (since,))[0]
            n, h, u = int(r["n"] or 0), int(r["h"] or 0), int(r["u"] or 0)
            out[f"d{days}"] = {"cards": n, "helpful": h, "useless": u,
                               "rate": round(h / (h + u) * 100) if (h + u) else None}
        return out
