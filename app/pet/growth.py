"""桌宠的成长：她在抖音上真的做了事，才会长经验、升级、进阶（参考 dsh-pet-evolve：形态是“挣来的”，不是贴上去的）。

- 经验只从真实记录里算（每天的用量、学到并确认的东西、和主人的互动、活跃了几天），不另外存“经验值”，
  所以不会算重、也改不了；同一天最多算 300 经验，刷得再多也不会一天升好几级。
- 每 100 经验升 1 级；累计 300 / 800 / 1600 / 3000 经验进阶（和 dsh-pet-evolve 一样的门槛），一共 5 个阶段。
- 可以生成一张 1200×630 的成长卡（PNG），卡上写明她是 AI 虚拟人。
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

WEIGHTS = {"videos": 1, "likes": 1, "collects": 2, "comments": 3, "comment_likes": 1, "comment_replies": 2,
           "replies_at": 2, "replies_dm": 2, "thread_replies": 2, "at_friends": 2, "publishes": 30}
DAY_CAP = 300               # 一天最多算这么多经验
ACTIVE_DAY = 10             # 每个活跃的日子 +10
KNOWLEDGE = 5               # 每条确认过的“学到的东西” +5
OWNER_TOUCH = 3             # 主动找主人聊天、给主人分享视频，每次 +3
LEVEL_XP = 100
STAGES = ((0, "萌新小号", "刚来抖音，什么都觉得新鲜"), (300, "见习刷手", "慢慢知道自己喜欢看什么了"),
          (800, "熟练网友", "评论区里能接得上梗"), (1600, "冲浪达人", "刷到什么都能聊上几句"),
          (3000, "传说级冲浪选手", "她的日报比热搜还准"))


def _day_xp(row: dict) -> int:
    xp = sum(int(row.get(k) or 0) * w for k, w in WEIGHTS.items())
    if any(int(row.get(k) or 0) for k in WEIGHTS):
        xp += ACTIVE_DAY
    return min(DAY_CAP, xp)


def status(db) -> dict:
    """现在的等级、阶段、经验，和各项累计。"""
    days = db.query("SELECT * FROM daily_usage")
    xp = sum(_day_xp(r) for r in days)
    active = sum(1 for r in days if any(int(r.get(k) or 0) for k in WEIGHTS))
    try:
        known = int(db.query("SELECT COUNT(*) AS n FROM knowledge WHERE status='stable'")[0]["n"])
    except Exception:  # noqa: BLE001
        known = 0
    touches = int(db.query("SELECT COUNT(*) AS n FROM events WHERE kind IN ('owner_chat','owner_share')")[0]["n"])
    keeper = getattr(db, "keeper", None)
    if keeper is not None:      # 旧记录归档以后也要算上
        touches += keeper.archived_count("events", ("owner_chat", "owner_share"))
    xp += known * KNOWLEDGE + touches * OWNER_TOUCH
    totals = {k: sum(int(r.get(k) or 0) for r in days) for k in WEIGHTS}
    stage_i = max(i for i, (need, _n, _d) in enumerate(STAGES) if xp >= need)
    need, name, desc = STAGES[stage_i]
    nxt = STAGES[stage_i + 1][0] if stage_i + 1 < len(STAGES) else None
    level = xp // LEVEL_XP + 1
    return {"xp": xp, "level": level, "level_xp": xp % LEVEL_XP, "next_xp": level * LEVEL_XP,
            "stage": stage_i, "stage_name": name, "stage_desc": desc, "stage_from": need, "stage_next": nxt,
            "stages": [{"need": n, "name": s} for n, s, _d in STAGES], "active_days": active, "knowledge": known,
            "owner_touches": touches, "totals": totals}


def card(db, out: Path, avatar: Path | None = None) -> Path:
    """画一张 1200×630 的成长卡，存到 out。"""
    from PIL import Image, ImageDraw

    from ..publish.fonts import cute, plain

    g = status(db)
    persona = db.persona()
    name = persona.get("name") or "雨雨"
    W, H = 1200, 630
    im = Image.new("RGB", (W, H), (255, 240, 246))
    d = ImageDraw.Draw(im)
    for y in range(H):                                   # 竖向渐变底色
        k = y / H
        d.line([(0, y), (W, y)], fill=(255, int(240 - 26 * k), int(246 - 16 * k)))
    d.rounded_rectangle([40, 40, W - 40, H - 40], radius=40, fill=(255, 255, 255), outline=(255, 140, 185), width=6)
    # 头像
    if avatar and avatar.exists():
        try:
            src = Image.open(avatar).convert("RGBA").resize((260, 260))
            av = Image.new("RGBA", (260, 260), (255, 240, 246, 255))      # 透明的地方垫上浅粉色
            av.alpha_composite(src)
            mask = Image.new("L", (260, 260), 0)
            ImageDraw.Draw(mask).ellipse([0, 0, 259, 259], fill=255)
            im.paste(av, (90, 120), mask)
        except Exception:  # noqa: BLE001
            log.warning("成长卡的头像画不上", exc_info=True)
    d.ellipse([84, 114, 356, 386], outline=(255, 140, 185), width=8)
    d.text((220, 420), f"Lv.{g['level']}", font=cute(64), fill=(222, 70, 132), anchor="mm")
    d.text((220, 490), g["stage_name"], font=cute(40), fill=(107, 53, 80), anchor="mm")
    # 右边：名字、进度、累计
    x0 = 430
    d.text((x0, 90), f"{name}的成长卡", font=cute(56), fill=(74, 31, 53))
    d.text((x0, 168), g["stage_desc"], font=plain(28), fill=(138, 74, 107))
    nxt = g["stage_next"]
    frac = 1.0 if nxt is None else (g["xp"] - g["stage_from"]) / max(1, nxt - g["stage_from"])
    d.rounded_rectangle([x0, 230, W - 90, 262], radius=16, fill=(253, 225, 235))
    d.rounded_rectangle([x0, 230, x0 + int((W - 90 - x0) * max(0.03, min(1.0, frac))), 262], radius=16,
                        fill=(240, 98, 146))
    d.text((x0, 274), f"经验 {g['xp']}" + (f"，离下一阶还差 {nxt - g['xp']}" if nxt else "，已经满阶啦"),
           font=plain(26), fill=(107, 53, 80))
    t = g["totals"]
    stats = [("活跃", f"{g['active_days']} 天"), ("看视频", f"{t['videos']} 个"), ("点赞", f"{t['likes']} 次"),
             ("评论", f"{t['comments']} 条"), ("回复", f"{t['replies_at'] + t['replies_dm'] + t['thread_replies']} 条"),
             ("学到", f"{g['knowledge']} 样")]
    for i, (k, v) in enumerate(stats):
        cx, cy = x0 + (i % 3) * 230, 340 + (i // 3) * 110
        d.rounded_rectangle([cx, cy, cx + 210, cy + 92], radius=22, fill=(255, 240, 245), outline=(251, 211, 225), width=3)
        d.text((cx + 105, cy + 30), k, font=plain(24), fill=(138, 74, 107), anchor="mm")
        d.text((cx + 105, cy + 64), v, font=cute(34), fill=(222, 70, 132), anchor="mm")
    d.text((W // 2, H - 70), f"她是 AI 虚拟人 · AI也要刷抖音 · {datetime.now().strftime('%Y-%m-%d')}", font=plain(22),
           fill=(160, 110, 135), anchor="mm")
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, "PNG")
    return out
