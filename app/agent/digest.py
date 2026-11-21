"""她的刷视频日报：每天刷了什么、最喜欢哪几个、学到了什么、和谁聊了天（参考 dsh-bili-scroll 的“每日简报”）。

- 纯本地统计、按模板写成 Markdown，不调用模型、不花钱。保存在 data/reports/<日期>.md，“历史记录”页里能看。
- 每个推荐都写清楚：链接、作者、她为什么喜欢（她当时自己说的原因）、她做了什么；
  没听到声音 / 没看到画面的视频，不替它编内容——只写她当时真的看到、听到的。
- 每天第一次找主人聊天时，会把前一天日报里最值得说的一两件事带给她，让她顺口跟主人说说（参考 WhaleMaid 的每日进展简报）。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from ..db import Database, today_str
from ..paths import REPORTS_DIR

log = logging.getLogger(__name__)

WEEKDAYS = "一二三四五六日"
MAX_PICKS = 8


def _videos(db: Database, day: str) -> list[dict]:
    rows = db.query("SELECT * FROM videos WHERE created_at LIKE ? ORDER BY id", (day + "%",))
    if not rows and getattr(db, "keeper", None) is not None:
        rows = db.keeper.read("videos", day)        # 旧的日子：从归档里解压出来
    return rows


def _did(r: dict) -> str:
    acts = [w for w, on in (("点了赞", r.get("liked")), ("收藏了", r.get("collected"))) if on]
    if r.get("comment"):
        acts.append(f"评论了“{str(r['comment'])[:40]}”")
    return "、".join(acts)


def build(db: Database, day: str) -> dict:
    """统计某一天：返回 {"day", "md", "stats", "picks", "brief"}。没刷过视频也没聊过天的日子 md 为空。"""
    usage = db.usage(day)
    vids = _videos(db, day)
    watched = [v for v in vids if v.get("decision") not in ("skip",) or v.get("interest")]
    picks = sorted((v for v in vids if v.get("liked") or v.get("collected") or v.get("comment")
                    or int(v.get("interest") or 0) >= 8),
                   key=lambda v: (int(v.get("interest") or 0), bool(v.get("comment")), bool(v.get("collected"))),
                   reverse=True)[:MAX_PICKS]
    topics: dict[str, int] = {}
    for v in vids:
        t = (v.get("topic") or "").strip()
        if t and v.get("decision") != "skip":
            topics[t] = topics.get(t, 0) + 1
    learned = [r["fact"] for r in db.query("SELECT fact FROM knowledge WHERE created_at LIKE ? ORDER BY id", (day + "%",))]
    chats = db.query("SELECT kind, user_name, COUNT(*) AS n FROM messages WHERE status IN ('replied','merged','manual') "
                     "AND COALESCE(handled_at, created_at) LIKE ? GROUP BY kind, user_name ORDER BY n DESC", (day + "%",))
    shares = db.query("SELECT * FROM owner_shares WHERE created_at LIKE ? ORDER BY id", (day + "%",))
    stats = {k: int(usage.get(k) or 0) for k in ("videos", "likes", "collects", "comments", "replies_at", "replies_dm",
                                                  "thread_replies", "comment_likes")}
    stats["cost"] = round(float(usage.get("cost") or 0), 2)
    if not vids and not chats and not stats["videos"]:
        return {"day": day, "md": "", "stats": stats, "picks": [], "brief": ""}
    d = date.fromisoformat(day)
    name = (db.persona().get("name") or "她")
    lines = [f"# {name}的刷视频日报 · {day}（星期{WEEKDAYS[d.weekday()]}）", ""]
    lines.append(f"- 看了 **{stats['videos'] or len(watched)}** 个视频，点赞 {stats['likes']}、收藏 {stats['collects']}、"
                 f"评论 {stats['comments']}、赞别人的评论 {stats['comment_likes']}")
    replies = stats["replies_at"] + stats["replies_dm"] + stats["thread_replies"]
    lines.append(f"- 回复了 {replies} 条找她的消息（评论区 {stats['replies_at'] + stats['thread_replies']}、私信 {stats['replies_dm']}）"
                 + (f"，模型花费 ¥{stats['cost']}" if stats["cost"] else ""))
    if topics:
        top = sorted(topics.items(), key=lambda x: -x[1])[:6]
        lines.append("- 这天刷得最多的：" + "、".join(f"{t}（{n}）" for t, n in top))
    lines.append("")
    if picks:
        lines += ["## 她最喜欢的", ""]
        for i, v in enumerate(picks, 1):
            title = (v.get("title") or "（没有文案）").strip()[:60]
            when = str(v.get("created_at") or "")[11:16]
            lines.append(f"{i}. **{title}** —— @{v.get('author') or '某位作者'}（{v.get('topic') or '日常'}，"
                         f"{v.get('interest') or '?'} 分，{when}）")
            if v.get("reason"):
                lines.append(f"   - 为什么喜欢：{str(v['reason'])[:100]}")
            did = _did(v)
            if did:
                lines.append(f"   - 她做了：{did}")
            if v.get("url"):
                lines.append(f"   - 链接：{v['url']}")
        lines.append("")
    if learned:
        lines += ["## 这天学到的", ""] + [f"- {f}" for f in learned[:12]] + [""]
    if chats:
        kinds = {"dm": "私信", "at": "评论区 @ 她", "reply": "回复她的评论", "work": "评论她的作品"}
        lines += ["## 和谁聊了天", ""]
        for c in chats[:12]:
            lines.append(f"- {c['user_name']}：{kinds.get(c['kind'], c['kind'])} {c['n']} 条")
        lines.append("")
    if shares:
        react = {"sent": "还没回", "liked": "喜欢", "neutral": "一般", "disliked": "不喜欢", "ignored": "没反应"}
        lines += ["## 分享给主人的", ""]
        for s in shares:
            lines.append(f"- {'私信分享' if s['way'] == 'dm' else '评论里 @'}《{(s['title'] or '')[:30]}》（{s['topic']}）"
                         f"：{react.get(s['status'], s['status'])}")
        lines.append("")
    lines.append(f"_这份日报是软件按当天的记录自动整理的（{datetime.now().strftime('%Y-%m-%d %H:%M')}），没有调用模型。_")
    brief_bits = []
    if picks:
        brief_bits.append(f"最喜欢的是 @{picks[0].get('author') or '某位作者'} 的《{(picks[0].get('title') or '')[:24]}》"
                          + (f"（{str(picks[0].get('reason'))[:40]}）" if picks[0].get("reason") else ""))
    if learned:
        brief_bits.append(f"学到了：{learned[0][:40]}")
    if topics:
        brief_bits.append("刷得最多的是" + "、".join(t for t, _ in sorted(topics.items(), key=lambda x: -x[1])[:2]))
    brief = (f"{'昨天' if day != today_str() else '今天'}看了 {stats['videos'] or len(watched)} 个视频；"
             + "；".join(brief_bits)) if (vids or brief_bits) else ""
    return {"day": day, "md": "\n".join(lines), "stats": stats, "picks": [
        {k: v.get(k) for k in ("title", "author", "url", "topic", "interest", "reason")} for v in picks],
        "brief": brief}


def write(db: Database, day: str) -> str:
    """生成并保存某一天的日报，返回文件路径（没内容返回空）。"""
    rep = build(db, day)
    if not rep["md"]:
        db.set_json(f"digest:{day}", {"brief": "", "stats": rep["stats"], "picks": []})   # 记一笔，不用反复再算
        return ""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"{day}.md"
    path.write_text(rep["md"], encoding="utf-8")
    db.set_json(f"digest:{day}", {"brief": rep["brief"], "stats": rep["stats"], "picks": rep["picks"]})
    log.info("写好了 %s 的刷视频日报", day)
    return str(path)


def due(db: Database, now: datetime | None = None) -> list[str]:
    """还没写日报的、已经过完的日子（最近 3 天里有记录的）。"""
    now = now or datetime.now()
    out = []
    for back in (3, 2, 1):
        day = (now - timedelta(days=back)).strftime("%Y-%m-%d")
        if (REPORTS_DIR / f"{day}.md").exists() or db.get_json(f"digest:{day}", None) is not None:
            continue
        if db.query("SELECT 1 FROM videos WHERE created_at LIKE ? LIMIT 1", (day + "%",)) or \
                db.query("SELECT 1 FROM messages WHERE created_at LIKE ? LIMIT 1", (day + "%",)):
            out.append(day)
    return out


def listing(limit: int = 60) -> list[dict]:
    if not REPORTS_DIR.exists():
        return []
    files = sorted(REPORTS_DIR.glob("*.md"), reverse=True)[:limit]
    return [{"day": f.stem, "bytes": f.stat().st_size} for f in files]


def read(day: str) -> str:
    p = REPORTS_DIR / f"{day}.md"
    if not (len(day) == 10 and p.exists()):
        return ""
    return p.read_text(encoding="utf-8")


def brief_for_owner(db: Database, now: datetime | None = None) -> str:
    """每天第一次找主人聊天时可以顺口说的：前一天日报里最值得说的一两件事。说过了就不再说。"""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    if (db.get_json("digest_told", "") or "") == today:
        return ""
    y = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    d = db.get_json(f"digest:{y}", None)
    if d is None:
        try:
            write(db, y)
        except Exception:  # noqa: BLE001
            log.warning("写日报失败", exc_info=True)
        d = db.get_json(f"digest:{y}", None)
    return (d or {}).get("brief", "") if isinstance(d, dict) else ""


def mark_told(db: Database, now: datetime | None = None) -> None:
    db.set_json("digest_told", (now or datetime.now()).strftime("%Y-%m-%d"))

