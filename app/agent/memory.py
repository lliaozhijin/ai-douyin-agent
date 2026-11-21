"""她的记忆：记什么、怎么淡忘、什么时候想起。

四层：
- 身份档案：人设页里的设定（每次说话都带上，见 persona.py）。
- 人物印象卡（people）：只给**私信好友**建卡——对方的说话风格、常聊的话题、告诉过她的事。
  只在评论区聊过的人不建卡：评论完这件事就过去了，只记“这一串评论 + 这个视频”的上下文（scope=thread:…，几天就淡忘）。
- 私信和评论区的记忆是**通的**：每条和人有关的记忆都记着是和谁（person），不管在哪儿聊，回 TA 的时候都能想起
  另一边聊过的事。私信好友、主人在评论区和她的互动，和私信里的事一样记得久，也会写进印象卡。
  除了按相关度想起的旧事，回私信时一定带上最近两周在评论区和 TA 的来往（原话），在评论区回时一定带上最近的私信，
  不会出现“只记得评论区的事、不记得私信里的事”。同一个人在私信列表、评论区、通知里名字写法不一样
  （带不带 emoji、改过昵称），按主页 id 认成一个人。
  （在公开的评论区回复时会提醒模型：可以记得，但不把私信里的私事当众说出来。）
- 具体的事（memories）：看了什么视频、和谁聊了什么、发了什么作品，每条一句话。
- 日记（diary）：每天大约 500 字；按 天 → 周 → 月 逐级合并，越久越概括。

淡忘：每条记忆有强度，按半衰期（默认 30 天）衰减；被想起一次，强度回升、半衰期变长。
强度掉到阈值以下就忘掉——细节删除，只留在当天的日记里。重要的（重要程度 5）和主人有关的永远不忘。

想起：按“和谁、聊什么”挑几条（相关度 × 强度 × 新近度），带进提示词；被挑中的记忆会加强。
"""
from __future__ import annotations

import json
import logging
import math
import random
import re
from datetime import date, datetime, timedelta

from ..db import Database, now_str, today_str
from .posting import covered
from .search_index import TextIndex

log = logging.getLogger(__name__)

HALF_LIFE_DAYS = 30.0      # 记忆半衰期：一个月
HALF_LIFE_MAX = 720.0
THREAD_HALF_LIFE = 3.0     # 评论区的上下文：几天就淡了
FORGET_BELOW = 0.25        # 强度低于它就忘掉
RECALL_BOOST = 0.5         # 被想起一次，强度回升多少
RECALL_STRETCH = 1.5       # 被想起一次，半衰期变成原来的几倍
STRENGTH_MAX = 3.0
KEEP_IMPORTANCE = 5        # 这个重要程度的永远不忘
CARD_EVERY = 4             # 和一个人又聊了几次以后，重新整理一次印象卡
DIARY_WORDS = 500
DAY_KEEP = 35              # 日记留多少天（之后只留周记）
WEEK_KEEP = 200            # 周记留多少天（之后只留月记）

RECALL_POOL = 120          # 全文检索先挑出这么多条候选，再按强度、新近度细排
REVIVE_SIM = 0.5           # 淡忘的记忆：现在聊的话和它重合这么多，就“想起来了”
CONTEXT_BUDGET = 1200      # 每次带进提示词的记忆最多这么多字（印象卡 > 和 TA 的旧事 > 自己的经历，超了从后面截）

HIGHLIGHTS = 10            # 写作品时想起今天的几件事
# 带着强烈情绪的事印象更深（好笑、生气、委屈、感动、尴尬、惊讶……）
FEELING = re.compile(r"笑死|哈哈|好笑|气死|生气|烦|委屈|难过|哭|感动|暖|尴尬|离谱|震惊|惊了|吓|开心|激动|害怕|心疼|无语|崩溃|破防|上头|好喜欢|讨厌")

OTHER_SIDE_DAYS = 14       # 回私信时带上最近多少天在评论区的来往（反过来也一样）
OTHER_SIDE_COMMENTS = 5    # 最多带几次评论区的来往
OTHER_SIDE_DMS = 10        # 最多带几句私信
OTHER_SIDE_BUDGET = 700    # 这一段最多多少字

AFFECTION_BASE = 30.0      # 好感度没什么特别时的水平
KIND_WORDS = ("谢谢", "感谢", "喜欢你", "好可爱", "可爱", "哈哈", "笑死", "爱了", "好棒", "厉害", "辛苦", "晚安", "早安",
              "抱抱", "[比心]", "[爱心]", "❤", "♡", "么么", "开心")
HARSH_WORDS = ("滚", "傻逼", "傻x", "sb", "垃圾", "恶心", "闭嘴", "有病", "烦死", "讨厌你", "去死", "废物", "脑残", "智障")

CLOSENESS_WORDS = ((80, "非常亲密，像最离不开的人"), (50, "很亲近，什么都愿意说"), (20, "熟悉了，说话很放松"),
                   (0, "刚认识不久，有点小心翼翼但很想亲近"))


def _dt(s: str) -> datetime:
    try:
        return datetime.strptime((s or "")[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return datetime.now()


def _grams(text: str) -> set:
    """相邻两个字一组（不用分词）：用来算两段话有多少内容重合。"""
    t = re.sub(r"[\s\W_]+", "", text or "", flags=re.U)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def budget_join(parts: list[str], budget: int) -> str:
    """把几段记忆按顺序拼起来，总长度不超过 budget 个字：前面的优先（重要的放前面），超了的那段截短、后面的不要。"""
    out, used = [], 0
    for p in parts:
        if not p:
            continue
        if used + len(p) <= budget:
            out.append(p)
            used += len(p) + 1
            continue
        room = budget - used
        if room > 60:
            out.append(p[:room].rsplit("\n", 1)[0] if "\n" in p[:room] else p[:room])
        break
    return "\n".join(out)


def affection_word(v: float) -> str:
    if v >= 80:
        return "很喜欢 TA，聊天时很放松、很热情"
    if v >= 60:
        return "对 TA 挺有好感，愿意多聊几句"
    if v <= 15:
        return "TA 之前说过不太客气的话，你会有点保留：礼貌、简短，不讨好也不吵架"
    return "一般"


def closeness_word(v: float) -> str:
    for least, word in CLOSENESS_WORDS:
        if v >= least:
            return word
    return CLOSENESS_WORDS[-1][1]


def week_of(day: str) -> str:
    y, w, _ = date.fromisoformat(day).isocalendar()
    return f"{y}-W{w:02d}"


class Memory:
    def __init__(self, db: Database, router=None):
        self.db = db
        self.router = router
        # 全文索引：记得的事、淡忘了的事各一个（见 search_index.py）
        self.index = TextIndex(db, "mem_fts")
        self.arc_index = TextIndex(db, "marc_fts")
        try:
            self.index.sync("SELECT id, summary || ' ' || tags AS t FROM memories")
        except Exception:  # noqa: BLE001
            log.warning("同步记忆索引失败", exc_info=True)
        self.on_taste = None   # 整理主人的印象卡时，顺便记下主人说过喜欢 / 不喜欢看什么：(主人, 喜欢的, 不喜欢的)
        self.taste_line = None  # 她自己最近的口味（一小段话，见 my_taste.py）：带进她的身份，不管在哪儿说话都是同一个她

    # ---------- 强度 ----------
    @staticmethod
    def strength_now(row: dict, now: datetime | None = None) -> float:
        days = max(0.0, ((now or datetime.now()) - _dt(row["touched_at"])).total_seconds() / 86400)
        return float(row["strength"]) * math.pow(0.5, days / max(0.5, float(row["half_life"])))

    def _kept_forever(self, row: dict, owners: set) -> bool:
        return int(row["importance"]) >= KEEP_IMPORTANCE or (row["person"] and row["person"] in owners)

    # ---------- 写 ----------
    def remember(self, kind: str, summary: str, *, scope: str = "", person: str = "", detail: str = "",
                 tags: list[str] | None = None, importance: int = 3, dedupe: str = "",
                 half_life: float | None = None) -> None:
        """记下一件事。同一件事（dedupe 相同）只留一条：再发生就更新内容、当作又想起了一次。"""
        summary = (summary or "").strip()[:200]
        if not summary:
            return
        importance = max(1, min(KEEP_IMPORTANCE, int(importance)))
        tag_s = "|".join(dict.fromkeys(t.strip()[:20] for t in (tags or []) if t and t.strip()))
        now = now_str()
        hl = float(half_life or HALF_LIFE_DAYS)
        try:
            if dedupe:
                old = self.db.query("SELECT * FROM memories WHERE dedupe=?", (dedupe,))
                if old:
                    o = old[0]
                    self.db.execute(
                        "UPDATE memories SET summary=?, detail=?, tags=?, importance=MAX(importance, ?), strength=?, "
                        "touched_at=? WHERE id=?",
                        (summary, detail[:1000], tag_s or o["tags"], importance,
                         min(STRENGTH_MAX, self.strength_now(o) + RECALL_BOOST), now, o["id"]))
                    self.index.put(o["id"], f"{summary} {tag_s or o['tags']}")
                    return
            mid = self.db.execute(
                "INSERT INTO memories(kind, scope, person, summary, detail, tags, importance, strength, half_life, day, "
                "created_at, touched_at, dedupe) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (kind, scope, person, summary, detail[:1000], tag_s, importance, 0.4 + 0.2 * importance, hl,
                 today_str(), now, now, dedupe or None)).lastrowid
            self.index.put(mid, f"{summary} {tag_s}")
        except Exception:  # noqa: BLE001 —— 记不下来不能影响她干活
            log.warning("记忆没写进去：%s", summary[:40], exc_info=True)

    def remember_video(self, v: dict, interest: int, topic: str, liked: bool, collected: bool, comment: str) -> None:
        """看过的视频：只记她有反应的（点了赞 / 收藏 / 评论）或者特别喜欢的。"""
        if not (liked or collected or comment or interest >= 9):
            return
        did = [w for w, on in (("点了赞", liked), ("收藏了", collected)) if on]
        if comment:
            did.append(f"评论了“{comment[:40]}”")
        title = (v.get("desc") or "").strip()[:40] or "（没有文案）"
        self.remember(
            "video", f"看了 @{v.get('author') or '某位作者'} 的视频《{title}》（{topic or '日常'}，{interest} 分）"
                     + ("，" + "、".join(did) if did else "，很喜欢"),
            tags=[topic, v.get("author") or ""], importance=3 if (comment or collected) else 2,
            dedupe=f"video:{v.get('video_id') or title}")

    def remember_dm(self, user: str, their: str, mine: str, video: dict | None = None, owner: bool = False) -> None:
        """和私信好友的一次来回。只有私信才关联到人、才会建人物印象卡。"""
        about = ""
        if video and (video.get("desc") or video.get("author")):
            about = f"（聊的是 @{video.get('author') or '某位作者'} 的视频《{(video.get('desc') or '')[:30]}》）"
        self.remember("dm", f"{user} 私信说“{their[:60]}”，我回“{mine[:60]}”{about}", scope=f"dm:{user}", person=user,
                      importance=4 if owner else 3)
        self.db.execute(
            "INSERT INTO people(name, chats, pending, last_at) VALUES(?,1,1,?) ON CONFLICT(name) DO UPDATE SET "
            "chats = chats + 1, pending = pending + 1, last_at = excluded.last_at", (user, now_str()))
        self.feel(user, their)

    @staticmethod
    def tone(text: str) -> float:
        """一句话的语气：友善 > 0、难听 < 0（简单的关键词判断，不调用模型）。"""
        t = (text or "").lower()
        harsh = sum(1 for w in HARSH_WORDS if w in t)
        if harsh:
            return -min(1.0, 0.6 * harsh)
        kind = sum(1 for w in KIND_WORDS if w.lower() in t)
        return min(0.5, 0.2 * kind) if kind else 0.05      # 愿意来聊天本身就是一点点好感

    def feel(self, user: str, their: str) -> float:
        """TA 说了一句话：好感度跟着变一点（友善 +，难听 −）。返回新的好感度。"""
        d = self.tone(their)
        self.db.execute("UPDATE people SET affection = MAX(0, MIN(100, affection + ?)) WHERE name=?", (d, user))
        row = self.db.query("SELECT affection FROM people WHERE name=?", (user,))
        return float(row[0]["affection"]) if row else AFFECTION_BASE

    def drift_affection(self, now: datetime | None = None) -> None:
        """一天一次：很久没联系的人，好感度慢慢回到一般（高的往下、低的往上，每天靠近 2%）；
        主人三天以上没联系，亲密度每天回落 0.5（聊起来很快就回来）。"""
        now = now or datetime.now()
        day = now.strftime("%Y-%m-%d")
        if (self.db.get_json("affection_day", "") or "") == day:
            return
        self.db.set_json("affection_day", day)
        quiet = (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        self.db.execute("UPDATE people SET affection = affection + (? - affection) * 0.02 WHERE last_at < ?",
                        (AFFECTION_BASE, quiet))
        for o in self.db.owners():
            last = self.db.query("SELECT MAX(ts) AS t FROM dm_history WHERE user_name=?", (o["name"],))[0]["t"] or ""
            if last and last < quiet and float(o.get("closeness") or 0) > 0:
                self.db.execute("UPDATE owners SET closeness = MAX(0, closeness - 0.5) WHERE name=?", (o["name"],))

    def knows(self, user: str) -> bool:
        """这个人是不是“认识的人”：私信聊过（有印象卡那一行），或者是主人。"""
        if not user:
            return False
        return bool(self.person(user)) or any(o["name"] == user for o in self.db.owners())

    def remember_thread(self, tid: str, user: str, their: str, mine: str, video: dict | None = None) -> None:
        """评论区的一串对话。记着是和谁聊的：以后 TA 来私信、或者在别的视频下找她，都能想起来。
        只在评论区聊过的人不建人物卡、几天就淡忘；私信好友和主人的，和私信里的事记得一样久。"""
        title = ((video or {}).get("desc") or "").strip()[:30]
        known = self.knows(user)
        self.remember("thread", f"在《{title or '一个视频'}》的评论区，{user} 回复我“{their[:50]}”，我回“{mine[:50]}”",
                      scope=f"thread:{tid}", person=user, importance=2 if known else 1,
                      half_life=None if known else THREAD_HALF_LIFE, dedupe=f"thread:{tid}")
        self._touch_person(user)

    def remember_at(self, user: str, their: str, mine: str, video: dict | None = None, work: bool = False) -> None:
        title = ((video or {}).get("desc") or "").strip()[:30]
        known = self.knows(user)
        did = f"在我的作品《{title or '一个作品'}》下评论" if work else f"在《{title or '一个视频'}》的评论区 @ 我"
        self.remember("at", f"{user} {did}说“{their[:50]}”，我回“{mine[:50]}”",
                      person=user, importance=2 if known else 1, half_life=None if known else THREAD_HALF_LIFE)
        self._touch_person(user)

    def _touch_person(self, user: str) -> None:
        """私信好友在评论区又和她聊了：印象卡那一行也记一笔（攒够几次会重新整理印象）。没有卡的人不新建。"""
        if user and self.person(user):
            self.db.execute("UPDATE people SET pending = pending + 1, last_at=? WHERE name=?", (now_str(), user))

    # ---------- 想起 ----------
    def recall(self, query: str = "", *, person="", scope: str = "", kinds: tuple = (), tags: tuple = (),
               limit: int = 4, touch: bool = True) -> list[dict]:
        """挑出最该想起的几条：相关度 × 强度 × 新近度。被挑中的会加强（强度回升、半衰期变长）。
        person 可以是一个名字，也可以是同一个人的几个名字（列表）。"""
        where, params = ["1=1"], []
        names = [n for n in (person if isinstance(person, (list, tuple, set)) else [person]) if n]
        person = names[0] if names else ""
        if names:
            where.append("person IN (%s)" % ",".join("?" * len(names)))
            params += names
        if scope:
            where.append("scope=?")
            params.append(scope)
        if not person and not scope:
            where.append("scope NOT LIKE 'thread:%' AND person=''")   # 别人的私信、评论区的上下文不串到别处
        if kinds:
            where.append("kind IN (%s)" % ",".join("?" * len(kinds)))
            params += list(kinds)
        cond = " AND ".join(where)
        if query:
            self.index.sync("SELECT id, summary || ' ' || tags AS t FROM memories", table="memories")
        hits = self.index.search(query, RECALL_POOL) if query else []
        if hits:
            # 全文索引在全部记忆里挑出最相关的一批（再久以前的事也找得到），加上最近的几十条，一起细排
            ids = [i for i, _ in hits]
            rows = self.db.query(f"SELECT * FROM memories WHERE {cond} AND id IN ({','.join('?' * len(ids))})",
                                 (*params, *ids))
            seen = {r["id"] for r in rows}
            rows += [r for r in self.db.query(f"SELECT * FROM memories WHERE {cond} ORDER BY touched_at DESC LIMIT 80",
                                              tuple(params)) if r["id"] not in seen]
        else:
            rows = self.db.query(f"SELECT * FROM memories WHERE {cond} ORDER BY touched_at DESC LIMIT 400",
                                 tuple(params))
        if not rows:
            return []
        q = _grams(query)
        want_tags = {t for t in tags if t}
        now = datetime.now()
        scored = []
        for r in rows:
            s = self.strength_now(r, now)
            rel = 0.0
            if q:
                g = _grams(r["summary"] + r["tags"])
                rel = len(q & g) / max(6, min(len(q), len(g)) or 1)
            if want_tags and want_tags & set((r["tags"] or "").split("|")):
                rel += 0.6
            if (q or want_tags) and rel <= 0 and not (person or scope):
                continue                      # 问的是具体的事：不相干的不想起
            age = max(0.0, (now - _dt(r["created_at"])).total_seconds() / 86400)
            scored.append(((0.25 + rel) * (0.5 + s) * (1.0 + 1.0 / (1.0 + age)), r))
        scored.sort(key=lambda x: -x[0])
        picked = [r for _, r in scored[:limit]]
        if touch:
            for r in picked:
                self.db.execute(
                    "UPDATE memories SET strength=?, half_life=?, recalls=recalls+1, touched_at=? WHERE id=?",
                    (min(STRENGTH_MAX, self.strength_now(r, now) + RECALL_BOOST),
                     min(HALF_LIFE_MAX, float(r["half_life"]) * RECALL_STRETCH), now_str(), r["id"]))
        return picked

    def person(self, name: str) -> dict | None:
        rows = self.db.query("SELECT * FROM people WHERE name=?", (name,))
        return rows[0] if rows else None

    def aliases(self, user: str) -> list[str]:
        """同一个人在私信、评论区、通知里的几个名字（见 db.person_aliases）。"""
        try:
            return self.db.person_aliases(user) or ([user] if user else [])
        except Exception:  # noqa: BLE001
            return [user] if user else []

    def person_context(self, user: str, their_text: str, public: bool = False) -> str:
        """和一个人说话时带上的记忆：对方的印象卡 + 和 TA 有关的几件旧事（私信里的、评论区的都算）
        + 她自己最近的相关经历。public=True 是在公开的评论区回复：提醒不要把私信里的私事当众说出来。
        同一个人在私信和评论区里名字写法不一样（带不带 emoji、改过昵称）也算同一个人（按主页 id 认）。"""
        parts = []
        names = self.aliases(user)
        p = next((x for x in (self.person(n) for n in names) if x), None)
        if p and (p["card"] or p["style"]):
            bits = [p["card"]] + [f"{k}：{p[f]}" for k, f in (("说话风格", "style"), ("常聊的话题", "topics"),
                                                              ("TA 说过的事 / 你答应过的事", "facts")) if p[f]]
            parts.append(f"你对「{user}」的印象：" + "；".join(b for b in bits if b))
        if p and p.get("affection") is not None:
            a = float(p["affection"])
            if a >= 60 or a <= 15:
                parts.append(f"你对 TA 的好感：{affection_word(a)}。")
        if user and their_text:
            for n in names[:3]:
                self.revive(their_text, person=n)      # TA 提起了一件你已经淡忘的事：想起来
        old = self.recall(their_text, person=names, limit=4) if user else []
        if old:
            parts.append("你记得和 TA 有关的事（私信里聊的、评论区聊的都在这儿）：\n"
                         + "\n".join(f"- {r['summary']}（{r['day']}）" for r in old))
        mine = self.recall(their_text, kinds=("video", "post"), limit=2) if _grams(their_text) else []
        if mine:
            parts.append("你自己最近的相关经历：\n" + "\n".join(f"- {r['summary']}（{r['day']}）" for r in mine))
        if public and parts and (p or any(r["kind"] == "dm" for r in old)):
            parts.insert(0, "注意：现在是在公开的评论区，别人都看得到。可以让 TA 感觉到你记得 TA、接得上之前的话，"
                            "但不要把私信里聊过的私事和细节当众说出来。")
        return budget_join(parts, CONTEXT_BUDGET)

    def other_side(self, user: str, here: str) -> str:
        """和这个人在“另一边”最近聊了什么，原话、按时间顺序（不靠记忆检索挑，最近的一定在）。

        here="dm"：正在回私信 → 带上最近在评论区的来回（TA 找她的、回复她评论的、她回过 TA 的评论）；
        here="comment"：正在评论区回 → 带上最近的私信，并提醒这是私下聊的，公开场合不要说出私事。
        以前只靠记忆检索按相关度挑 4 条：评论区里刚聊过的事，到了私信里常常想不起来，反过来也一样。"""
        if not user:
            return ""
        names = self.aliases(user)
        since = (datetime.now() - timedelta(days=OTHER_SIDE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        try:
            if here == "dm":
                rows = self.db.comment_lines(names, since, limit=OTHER_SIDE_COMMENTS)
                if not rows:
                    return ""
                lines = []
                for r in rows:
                    where = f"《{r['where'][:20]}》下" if r["where"] else "评论区"
                    what = {"reply": "回复你的评论", "work": "评论你的作品", "my_reply": "的评论（你回复过）"}.get(
                        r["kind"], "找你")
                    line = f"- {r['ts'][5:16]} 在{where}，TA {what}：“{r['them'][:60]}”"
                    if r["me"]:
                        line += f"，你回：“{r['me'][:60]}”"
                    lines.append(line)
                return budget_join(["你们最近在评论区的来往（TA 在私信里提到评论区的事要接得上）：\n" + "\n".join(lines)],
                                   OTHER_SIDE_BUDGET)
            rows = self.db.dm_lines(names, since, limit=OTHER_SIDE_DMS)
            if not rows:
                return ""
            lines = [f"- {r['ts'][5:16]} {'TA' if r['role'] == 'them' else '你'}：{r['content'][:70]}" for r in rows]
            return budget_join(["你们最近的私信（只有你们俩看得到；在评论区可以接得上、让 TA 知道你记得，"
                                "但不要把私信里的私事和细节当众说出来）：\n" + "\n".join(lines)], OTHER_SIDE_BUDGET)
        except Exception:  # noqa: BLE001 —— 读不出来不影响回复
            log.warning("读和 %s 在另一边的聊天失败", user, exc_info=True)
            return ""

    def dm_context(self, user: str, their_text: str) -> str:
        """回私信时带上的记忆：印象卡、和 TA 有关的旧事，加上最近在评论区和 TA 的来往。"""
        return "\n\n".join(x for x in (self.person_context(user, their_text), self.other_side(user, "dm")) if x)

    def comment_context(self, user: str, their_text: str) -> str:
        """在评论区回 TA（@ 她的、回复她评论的）时带上的记忆：印象卡、和 TA 有关的旧事，加上最近的私信。"""
        return "\n\n".join(x for x in (self.person_context(user, their_text, public=True),
                                        self.other_side(user, "comment")) if x)

    def video_context(self, v: dict, topic: str) -> str:
        """看视频做决定时带上的记忆：同一个作者 / 同一类内容的旧事，加上她最近点赞评论的习惯。"""
        parts = []
        old = self.recall(v.get("desc") or "", kinds=("video", "post"), tags=(topic, v.get("author") or ""), limit=3)
        if old:
            parts.append("你想起来：\n" + "\n".join(f"- {r['summary']}（{r['day']}）" for r in old))
        habit = self.habit_line(topic)
        if habit:
            parts.append(habit)
        return "\n".join(parts)

    def habit_line(self, topic: str = "") -> str:
        """她最近的习惯，让模型自己掂量（不再由程序按比例替她决定点不点赞）。"""
        day = today_str()
        r = self.db.query("SELECT COUNT(*) AS n, SUM(liked) AS l, SUM(CASE WHEN comment != '' AND comment IS NOT NULL "
                          "THEN 1 ELSE 0 END) AS c FROM (SELECT liked, comment FROM videos WHERE decision NOT IN "
                          "('skip','watch_skip') ORDER BY id DESC LIMIT 30)")[0]
        n, liked, commented = int(r["n"] or 0), int(r["l"] or 0), int(r["c"] or 0)
        if n < 5:
            return ""
        line = f"你最近认真看完的 {n} 个视频里，点赞了 {liked} 个，评论了 {commented} 个。"
        if topic:
            same = self.db.query("SELECT SUM(liked) AS l, SUM(CASE WHEN comment != '' AND comment IS NOT NULL THEN 1 "
                                 "ELSE 0 END) AS c FROM videos WHERE topic=? AND created_at >= ?", (topic, day))[0]
            if int(same["l"] or 0) or int(same["c"] or 0):
                line += f"今天「{topic}」这一类你已经赞了 {int(same['l'] or 0)} 个、评论了 {int(same['c'] or 0)} 个。"
        return line

    def identity_context(self) -> str:
        """每次说话都带上的一小段：最近一篇日记和周记的开头，让她“记得昨天的自己”。"""
        rows = self.db.query("SELECT level, period, content FROM diary WHERE level IN ('day','week') "
                             "ORDER BY created_at DESC LIMIT 6")
        day = next((r for r in rows if r["level"] == "day"), None)
        week = next((r for r in rows if r["level"] == "week"), None)
        parts = []
        if day:
            parts.append(f"你最近一篇日记（{day['period']}）：{day['content'][:160]}")
        if week:
            parts.append(f"上一周的你（{week['period']}）：{week['content'][:120]}")
        if callable(self.taste_line):
            try:
                t = self.taste_line()
            except Exception:  # noqa: BLE001
                t = ""
            if t:
                parts.append(t)
        return "\n".join(parts)

    # ---------- 人物印象卡（只有私信好友） ----------
    def update_card(self, user: str, force: bool = False) -> float:
        """和一个人又聊了几次以后，把“旧印象 + 最近的聊天”合并成新的印象卡。返回花的钱。"""
        p = self.person(user)
        if not p or self.router is None or (not force and int(p["pending"]) < CARD_EVERY):
            return 0.0
        names = self.aliases(user)
        lines = self.db.dm_lines(names, limit=16) or self.db.dm_history(user, limit=16)
        cmts = self.db.query("SELECT summary, day FROM memories WHERE person IN (%s) AND kind IN ('thread','at') "
                             "ORDER BY touched_at DESC LIMIT 6" % ",".join("?" * len(names)), tuple(names))
        if not lines and not cmts:
            return 0.0
        chat = "\n".join(f"{'TA' if h['role'] == 'them' else '我'}：{h['content'][:120]}" for h in lines)
        public = "\n".join(f"- {r['summary']}（{r['day']}）" for r in cmts)
        is_owner = bool(self.db.owner(user)) and self.on_taste is not None
        taste_keys = (', "likes_videos": ["TA 在聊天里明确说过喜欢看的视频类型，2～4 个字一个，没有就留空"], '
                      '"dislikes_videos": ["TA 明确说过不喜欢看的视频类型"]' if is_owner else "")
        prompt = (
            f"你在抖音上和「{user}」聊过天（私信里、评论区里都算）。请根据旧的印象和最近的聊天，更新你对 TA 的印象卡。\n"
            f"旧的印象：{p['card'] or '（还没有）'}\n旧的说话风格：{p['style'] or '（还没有）'}\n"
            f"旧的常聊话题：{p['topics'] or '（还没有）'}\n旧的要记住的事：{p['facts'] or '（还没有）'}\n\n"
            + (f"最近的私信：\n{chat}\n\n" if chat else "")
            + (f"你们在评论区的互动：\n{public}\n\n" if public else "")
            +
            "只记聊天里真的出现过的内容，不要猜；不要记手机号、住址、健康、收入这类隐私。\n"
            '只输出 JSON：{"card": "一两句话的整体印象（60 字以内）", "style": "TA 的说话风格：长短、语气、爱用的词和表情（40 字以内）", '
            '"topics": "常聊的话题，用顿号分开", "facts": "TA 告诉过你的事、你答应过 TA 的事（80 字以内，没有就留空）"'
            + taste_keys + "}"
        )
        try:
            from .brain import parse_json
            r = self.router.call("write", "你在整理自己对一位朋友的印象，客观、简短。", prompt, max_tokens=320,
                                 temperature=0.3)
            d = parse_json(r.text)
        except Exception:  # noqa: BLE001
            log.warning("整理 %s 的印象卡失败", user, exc_info=True)
            return 0.0
        if d.get("card") or d.get("style"):
            self.db.execute(
                "UPDATE people SET card=?, style=?, topics=?, facts=?, pending=0, updated_at=? WHERE name=?",
                (str(d.get("card") or p["card"])[:160], str(d.get("style") or p["style"])[:120],
                 str(d.get("topics") or p["topics"])[:120], str(d.get("facts") or p["facts"])[:240], now_str(), user))
        if is_owner:
            likes = [str(x) for x in (d.get("likes_videos") or []) if isinstance(x, str) and x.strip()]
            dislikes = [str(x) for x in (d.get("dislikes_videos") or []) if isinstance(x, str) and x.strip()]
            if likes or dislikes:
                try:
                    self.on_taste(user, likes, dislikes)
                except Exception:  # noqa: BLE001
                    log.warning("记主人的口味失败", exc_info=True)
        return float(getattr(r, "cost", 0.0) or 0.0)

    def forget_person(self, name: str) -> int:
        """忘掉这个人：印象卡、和 TA 有关的记忆（包括淡忘了的）、私信聊天记录都删掉（只对私信好友）。"""
        for r in self.db.query("SELECT id FROM memories WHERE person=?", (name,)):
            self.index.drop(r["id"])
        for r in self.db.query("SELECT id FROM memory_archive WHERE person=?", (name,)):
            self.arc_index.drop(r["id"])
        n = self.db.execute("DELETE FROM memories WHERE person=?", (name,)).rowcount
        self.db.execute("DELETE FROM memory_archive WHERE person=?", (name,))
        self.db.execute("DELETE FROM people WHERE name=?", (name,))
        self.db.execute("DELETE FROM dm_history WHERE user_name=?", (name,))
        return int(n or 0)

    # ---------- 淡忘 ----------
    def fade(self) -> int:
        """把淡到阈值以下的记忆“淡忘”：不再主动想起，压缩后放进 memory_archive（当天的日记里也有一笔）。
        以后有人提起相关的事，还能想起来（revive）。重要的、和主人有关的不忘。
        评论区路人的上下文（只在那一串评论里有用）直接删掉。"""
        owners = {o["name"] for o in self.db.owners()}
        now = datetime.now()
        gone = [r for r in self.db.query("SELECT * FROM memories")
                if not self._kept_forever(r, owners) and self.strength_now(r, now) < FORGET_BELOW]
        if not gone:
            return 0
        keeper = getattr(self.db, "keeper", None)
        samples = [json.dumps(r, ensure_ascii=False).encode("utf-8")
                   for r in self.db.query("SELECT * FROM memories ORDER BY id DESC LIMIT 300")]
        kept = 0
        for r in gone:
            self.index.drop(r["id"])
            if keeper is not None and not (r["scope"] or "").startswith("thread:"):
                raw = json.dumps(r, ensure_ascii=False).encode("utf-8")
                codec, did, data = keeper.compress_text("memories", raw, samples)
                self.db.execute("INSERT OR REPLACE INTO memory_archive(id, kind, person, day, codec, dict_id, data, "
                                "archived_at) VALUES(?,?,?,?,?,?,?,?)",
                                (r["id"], r["kind"], r["person"], r["day"], codec, did, data, now_str()))
                self.arc_index.put(r["id"], f"{r['summary']} {r['tags']}")
                kept += 1
        ids = [r["id"] for r in gone]
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            self.db.execute("DELETE FROM memories WHERE id IN (%s)" % ",".join("?" * len(chunk)), tuple(chunk))
        log.info("淡忘了 %d 条记忆（%d 条压缩后留着，被提起时还能想起来）", len(gone), kept)
        return len(gone)

    def _archived(self, rid: int) -> dict | None:
        rows = self.db.query("SELECT * FROM memory_archive WHERE id=?", (rid,))
        keeper = getattr(self.db, "keeper", None)
        if not rows or keeper is None:
            return None
        r = rows[0]
        try:
            return json.loads(keeper.decompress(r["codec"], r["dict_id"], r["data"]).decode("utf-8"))
        except Exception:  # noqa: BLE001
            log.warning("淡忘的记忆 %s 解不开", rid, exc_info=True)
            return None

    def revive(self, query: str, person: str = "", limit: int = 2) -> list[dict]:
        """有人提起了一件她已经淡忘的事：和现在聊的话重合得够多，就想起来（放回记忆里，强度不高，还会再淡）。"""
        q = _grams(query)
        if len(q) < 3:
            return []
        back = []
        for rid, _score in self.arc_index.search(query, 12):
            meta = self.db.query("SELECT person FROM memory_archive WHERE id=?", (rid,))
            if not meta or (person and meta[0]["person"] not in ("", person)):
                continue
            m = self._archived(rid)
            if not m:
                continue
            g = _grams(m.get("summary", "") + m.get("tags", ""))
            if len(q & g) / max(1, min(len(q), len(g))) < REVIVE_SIM:
                continue
            self._put_back(m, recalls=int(m.get("recalls") or 0) + 1,
                           half_life=max(HALF_LIFE_DAYS, float(m.get("half_life") or 0)))
            log.info("想起了一件淡忘的事：%s", m["summary"][:40])
            back.append(m)
            if len(back) >= limit:
                break
        return back

    def restore(self, rid: int) -> bool:
        """你在“记忆”页里点了“想起来”：把一条淡忘的记忆放回去。"""
        m = self._archived(rid)
        if not m:
            return False
        return bool(self.revive(m["summary"] + " " + (m.get("tags") or ""), person=m.get("person") or "", limit=1)) or \
            self._force_restore(m)

    def _force_restore(self, m: dict) -> bool:
        self._put_back(m, recalls=int(m.get("recalls") or 0), half_life=HALF_LIFE_DAYS)
        return True

    def _put_back(self, m: dict, recalls: int, half_life: float) -> None:
        """把一条淡忘的记忆放回 memories（还用原来的 id）。同一件事这期间又记了一条新的（dedupe 一样），
        就不再放回旧的，只让新的那条记得更牢一点。"""
        now = now_str()
        self.db.execute(
            "INSERT OR IGNORE INTO memories(id, kind, scope, person, summary, detail, tags, importance, strength, "
            "half_life, recalls, day, created_at, touched_at, dedupe) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (m["id"], m["kind"], m.get("scope") or "", m.get("person") or "", m["summary"], m.get("detail") or "",
             m.get("tags") or "", m.get("importance") or 3, 0.8, half_life, recalls, m.get("day") or today_str(),
             m.get("created_at") or now, now, m.get("dedupe")))
        if self.db.query("SELECT 1 FROM memories WHERE id=?", (m["id"],)):
            self.index.put(m["id"], f"{m['summary']} {m.get('tags') or ''}")
        elif m.get("dedupe"):
            self.db.execute("UPDATE memories SET strength = MIN(1.0, strength + 0.2), touched_at=? WHERE dedupe=?",
                            (now, m["dedupe"]))
        self.db.execute("DELETE FROM memory_archive WHERE id=?", (m["id"],))
        self.arc_index.drop(m["id"])

    def archived_listing(self, q: str = "", limit: int = 200) -> list[dict]:
        """“记忆”页：淡忘了的记忆（解压出来看）。"""
        if q:
            ids = [i for i, _ in self.arc_index.search(q, limit)]
            rows = [r for i in ids for r in self.db.query("SELECT * FROM memory_archive WHERE id=?", (i,))]
        else:
            rows = self.db.query("SELECT * FROM memory_archive ORDER BY archived_at DESC, id DESC LIMIT ?", (limit,))
        out = []
        keeper = getattr(self.db, "keeper", None)
        for r in rows:
            try:
                m = json.loads(keeper.decompress(r["codec"], r["dict_id"], r["data"]).decode("utf-8"))
            except Exception:  # noqa: BLE001
                continue
            out.append({"id": r["id"], "kind": m.get("kind"), "summary": m.get("summary"), "person": m.get("person"),
                        "day": m.get("day"), "archived_at": r["archived_at"], "bytes": len(r["data"])})
        return out

    # ---------- 日记 ----------
    def day_material(self, day: str) -> list[str]:
        """当天的事，写日记用。去重：同一个视频 / 同一个人 / 同一串评论只算一条。"""
        rows = self.db.query("SELECT kind, scope, person, summary, dedupe FROM memories WHERE day=? "
                             "ORDER BY importance DESC, id", (day,))
        seen, per_person, out = set(), {}, []
        for r in rows:
            key = r["dedupe"] or r["summary"][:24]
            if key in seen:
                continue
            if r["person"]:
                per_person[r["person"]] = per_person.get(r["person"], 0) + 1
                if per_person[r["person"]] > 3:      # 和同一个人聊了很多句：日记里挑几句就够了
                    continue
            seen.add(key)
            out.append(r["summary"])
        return out[:60]

    def highlights(self, now: datetime | None = None, avoid: list[str] | None = None, n: int = HIGHLIGHTS,
                   rnd: random.Random | None = None) -> list[str]:
        """写作品时她想得起来的今天的事（今天的事不多就加上昨天的）。

        不再只挑“重要程度最高的 6 件”：参考 Generative Agents 的做法，每件事按“印象有多深”打分——
        重要程度、是不是和人有来往、今天被想起过几次、有没有强烈的情绪（好笑、生气、委屈、感动……）、
        再加一点随机（人每次回想今天，冒出来的事都不完全一样）；已经写进最近作品里的事扣分（不老写同一件）。
        同一类事、同一个人各最多几件，免得全是刷视频或者全是和一个人聊天；最后再捎上两件不起眼的小事——
        真人发的东西常常就是一件很小的事。"""
        now = now or datetime.now()
        rnd = rnd or random.Random(now.strftime("%Y%m%d%H"))
        avoid = [a for a in (avoid or []) if a]
        days = [now.strftime("%Y-%m-%d")]
        rows = self.db.query("SELECT kind, person, summary, importance, recalls, dedupe, day FROM memories WHERE day=?",
                             (days[0],))
        if len(rows) < 6:
            days.append((now - timedelta(days=1)).strftime("%Y-%m-%d"))
            rows += self.db.query("SELECT kind, person, summary, importance, recalls, dedupe, day FROM memories WHERE day=?",
                                  (days[1],))
        seen, scored = set(), []
        for r in rows:
            key = r["dedupe"] or r["summary"][:24]
            if key in seen or r["kind"] in ("self",):
                continue
            seen.add(key)
            text = r["summary"]
            score = float(r["importance"] or 3)
            if r["kind"] in ("dm", "thread", "at", "note", "post"):
                score += 1.0                                     # 和人有来往的事印象更深
            score += 0.5 * min(int(r["recalls"] or 0), 3)        # 今天又想起过
            if FEELING.search(text):
                score += 1.0                                     # 有强烈情绪的事
            if avoid and max(covered(text, a) for a in avoid) >= 0.5:
                score -= 2.5                                     # 已经写进最近的作品里了
            score += rnd.uniform(0, 1.5)
            label = (f"私信里：{text}" if r["kind"] == "dm" else text)   # 别人私下跟她说的，标出来
            label = label if r["day"] == days[0] else f"（昨天）{label}"
            scored.append((score, r["kind"], r["person"] or "", label, int(r["importance"] or 3)))
        scored.sort(key=lambda x: -x[0])
        per_kind: dict[str, int] = {}
        per_person: dict[str, int] = {}
        out, rest = [], []
        for sc, kind, person, label, imp in scored:
            if len(out) >= n - 2 or per_kind.get(kind, 0) >= 3 or (person and per_person.get(person, 0) >= 2):
                rest.append((label, imp))
                continue
            per_kind[kind] = per_kind.get(kind, 0) + 1
            if person:
                per_person[person] = per_person.get(person, 0) + 1
            out.append(label)
        small = [label for label, imp in rest if imp <= 3]
        out += rnd.sample(small, min(2, len(small), max(0, n - len(out))))
        return out[:n]

    def post_material(self, now: datetime | None = None, avoid: list[str] | None = None) -> str:
        """写作品时给她翻的“记忆”：今天（和昨天）印象深的事、最近的一篇日记、几件以前的事（她可以回忆，也可以不用）。
        avoid：她最近发过的作品的文字（写过的事少想起来一点）。"""
        now = now or datetime.now()
        parts = []
        today = self.highlights(now, avoid)
        if today:
            parts.append("今天的事（标着“私信里”的是别人私下跟你说的）：\n" + "\n".join(f"- {t}" for t in today))
        rows = self.db.query("SELECT period, content FROM diary WHERE level='day' ORDER BY period DESC LIMIT 1")
        if rows:
            parts.append(f"你最近的一篇日记（{rows[0]['period']}）：{rows[0]['content'][:260]}")
        cutoff = (now - timedelta(days=3)).strftime("%Y-%m-%d")
        old = self.db.query("SELECT summary, created_at FROM memories WHERE created_at < ? AND scope NOT LIKE 'thread:%' "
                            "ORDER BY importance DESC, strength DESC LIMIT 12", (cutoff,))
        if old:
            pick = random.Random(now.strftime("%Y%m%d%H")).sample(old, min(3, len(old)))
            parts.append("以前的事：\n" + "\n".join(f"- {r['created_at'][:10]} {r['summary'][:60]}" for r in pick))
        older = self.db.query("SELECT level, period, content FROM diary WHERE level IN ('week', 'month') "
                              "ORDER BY period DESC LIMIT 1")
        if older:
            parts.append(f"更早的时候（{older[0]['period']}）：{older[0]['content'][:160]}")
        return "\n".join(parts)

    def diary_due(self, now: datetime | None = None) -> str:
        """该补写哪一天的日记：昨天（或更早、还没写的最近一天）有事可写就写。今天的要等今天过完。"""
        today = (now or datetime.now()).date().isoformat()
        rows = self.db.query("SELECT day, COUNT(*) AS n FROM memories WHERE day < ? AND day >= ? GROUP BY day "
                             "ORDER BY day DESC", (today, (date.fromisoformat(today) - timedelta(days=7)).isoformat()))
        have = {r["period"] for r in self.db.query("SELECT period FROM diary WHERE level='day'")}
        for r in rows:
            if r["day"] not in have:
                return r["day"]
        return ""

    def write_diary(self, day: str, system: str, mood: str = "") -> float:
        """写某一天的日记（大约 500 字）。返回花的钱；没东西可写或没配模型返回 0。"""
        items = self.day_material(day)
        if not items or self.router is None:
            return 0.0
        usage = self.db.usage(day)
        stats = (f"这天看了 {int(usage.get('videos') or 0)} 个视频，点赞 {int(usage.get('likes') or 0)} 个，"
                 f"评论 {int(usage.get('comments') or 0)} 条，回复私信 {int(usage.get('replies_dm') or 0)} 条。")
        prompt = (
            f"今天是 {day}。请用第一人称写今天的日记，大约 {DIARY_WORDS} 字。\n{stats}\n"
            + (f"今天的心情：{mood}\n" if mood else "")
            + "今天发生的事（已经去掉重复的）：\n" + "\n".join(f"- {s}" for s in items) + "\n\n"
            "写法：像写给自己看的日记，挑印象最深的几件事展开写，写出当时的感受和想法；"
            "同一件事只写一次；只写上面真的发生过的事，不要编造；不要列清单，不要写标题。"
        )
        r = self.router.call("write", system, prompt, max_tokens=1100, temperature=0.8)
        text = (r.text or "").strip()
        if not text:
            return float(r.cost or 0)
        self.db.execute("INSERT INTO diary(level, period, content, mood, created_at) VALUES('day',?,?,?,?) "
                        "ON CONFLICT(level, period) DO UPDATE SET content=excluded.content, mood=excluded.mood",
                        (day, text[:1500], mood[:40], now_str()))
        log.info("写了 %s 的日记（%d 字）", day, len(text))
        return float(r.cost or 0)

    def _merge(self, level: str, period: str, sources: list[dict], system: str, words: int) -> float:
        if not sources or self.router is None:
            return 0.0
        what = "周记" if level == "week" else "月记"
        body = "\n\n".join(f"【{s['period']}】{s['content'][:700]}" for s in sources)
        prompt = (f"下面是你这段时间写的日记。请用第一人称把它们合并成一篇{what}（{period}），大约 {words} 字：\n\n{body}\n\n"
                  "留下重要的事、反复出现的人和话题、心情的变化；重复的内容合并成一句；不要编造，不要写标题。")
        r = self.router.call("write", system, prompt, max_tokens=900, temperature=0.6)
        text = (r.text or "").strip()
        if text:
            self.db.execute("INSERT INTO diary(level, period, content, created_at) VALUES(?,?,?,?) "
                            "ON CONFLICT(level, period) DO UPDATE SET content=excluded.content",
                            (level, period, text[:1500], now_str()))
            log.info("合并出 %s 的%s", period, what)
        return float(r.cost or 0)

    def roll_up(self, system: str, now: datetime | None = None) -> float:
        """日记逐级合并：过完的一周合成周记，过完的一个月合成月记；太久以前的日记 / 周记删掉（内容已经在上一级里）。"""
        today = (now or datetime.now()).date()
        cost = 0.0
        days = self.db.query("SELECT period, content FROM diary WHERE level='day' ORDER BY period")
        have = {(r["level"], r["period"]) for r in self.db.query("SELECT level, period FROM diary")}
        this_week, this_month = week_of(today.isoformat()), today.isoformat()[:7]
        by_week: dict[str, list] = {}
        for d in days:
            by_week.setdefault(week_of(d["period"]), []).append(d)
        for wk, items in sorted(by_week.items()):
            if wk < this_week and ("week", wk) not in have:
                cost += self._merge("week", wk, items, system, 350)
        by_month: dict[str, list] = {}
        for d in days:
            by_month.setdefault(d["period"][:7], []).append(d)
        for mo, items in sorted(by_month.items()):
            if mo < this_month and ("month", mo) not in have:
                weeks = [w for w in self.db.query("SELECT period, content FROM diary WHERE level='week' ORDER BY period")
                         if any(week_of(d["period"]) == w["period"] for d in items)]
                cost += self._merge("month", mo, weeks or items, system, 400)
        self.db.execute("DELETE FROM diary WHERE level='day' AND period < ? AND EXISTS "
                        "(SELECT 1 FROM diary w WHERE w.level='week')",
                        ((today - timedelta(days=DAY_KEEP)).isoformat(),))
        old_week = week_of((today - timedelta(days=WEEK_KEEP)).isoformat())
        self.db.execute("DELETE FROM diary WHERE level='week' AND period < ? AND EXISTS "
                        "(SELECT 1 FROM diary m WHERE m.level='month')", (old_week,))
        return cost

    # ---------- 记忆页 ----------
    def listing(self, tab: str = "events", q: str = "", person: str = "", limit: int = 200) -> dict:
        owners = {o["name"] for o in self.db.owners()}
        if tab == "people":
            rows = self.db.query("SELECT p.*, (SELECT COUNT(*) FROM memories m WHERE m.person=p.name) AS n "
                                 "FROM people p ORDER BY last_at DESC LIMIT ?", (limit,))
            for r in rows:
                r["owner"] = r["name"] in owners
            return {"people": rows}
        if tab == "diary":
            return {"diary": self.db.query("SELECT * FROM diary ORDER BY CASE level WHEN 'day' THEN 0 WHEN 'week' "
                                           "THEN 1 ELSE 2 END, period DESC LIMIT ?", (limit,))}
        where, params = ["1=1"], []
        if person:
            where.append("person=?")
            params.append(person)
        if q:
            where.append("(summary LIKE ? OR tags LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        rows = self.db.query(f"SELECT * FROM memories WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT ?",
                             (*params, limit))
        now = datetime.now()
        for r in rows:
            r["now"] = round(self.strength_now(r, now), 2)
            r["kept"] = bool(self._kept_forever(r, owners))
        total = self.db.query("SELECT COUNT(*) AS n FROM memories")[0]["n"]
        return {"events": rows, "total": total}

    def set_importance(self, mid: int, importance: int) -> None:
        self.db.execute("UPDATE memories SET importance=? WHERE id=?", (max(1, min(KEEP_IMPORTANCE, int(importance))), mid))

    def delete(self, mid: int) -> None:
        self.db.execute("DELETE FROM memories WHERE id=?", (mid,))
        self.index.drop(mid)

    def delete_archived(self, mid: int) -> None:
        self.db.execute("DELETE FROM memory_archive WHERE id=?", (mid,))
        self.arc_index.drop(mid)

    def delete_diary(self, did: int) -> None:
        self.db.execute("DELETE FROM diary WHERE id=?", (did,))
