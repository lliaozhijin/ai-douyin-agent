"""她每天自己学东西：刷到的视频、评论区里大家说的、和别人私信聊的，她会自己消化，记成“学到的东西”。

做法参考 mem0（https://github.com/mem0ai/mem0）的“抽取 → 对照 → 合并”：
1. 平时只是随手记“学习素材”（learn_notes）：看完一个视频她觉得学到了什么（看视频时顺便判断，不多花一次调用）、
   评论区和私信里的来回。
2. 攒够一批（或者隔了一天）整理一次：先让模型从素材里挑出值得记住的新东西（梗是什么意思、流行说法、
   生活小知识、某个领域的知识、大家对一件事的看法），每条一句话；
   再拿每条去和她已经学到的东西比（找相近的几条），让模型决定：新增 ADD / 改写合并 UPDATE / 发现以前的是错的
   删掉 DELETE / 已经知道了 NOOP。这样不会越记越乱、越记越重复。
3. 用的时候按“聊的是什么”挑几条相关的带进提示词（看视频、回评论、回私信都用），被用到的记得更牢；
   很久没用、又不太确定的慢慢忘掉。

不记具体某个人的隐私（人的事记在记忆和印象卡里，见 memory.py）。

**先当“候选”，见得多了才确认**（“候选池 → 见够次数才升格”的做法）：刚学到的一条是候选，
用的时候打折扣；之后整理时又在别处见到同样的说法（模型判断为“已经知道了 / 合并”）就记一次，
见到 3 次、而且是在 2 天以上见到的，才算确认（你也可以在“记忆 → 她学到的”里直接确认或删掉）。
候选 30 天都没再见到，就当是一时的说法，忘掉。检索用全文索引（见 search_index.py），学得再多也找得快。
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta

from ..db import Database, now_str, today_str
from .search_index import TextIndex

log = logging.getLogger(__name__)

BATCH_MIN = 12             # 攒够这么多条素材就整理一次
BATCH_MAX = 40             # 一次最多整理多少条
DAILY_HOURS = 20           # 素材不够多，隔这么久也整理一次
MAX_FACTS = 800            # 最多记多少条，多了先忘最不常用、最不确定的
FADE_DAYS = 90             # 这么久没用过、又不太确定的，忘掉
RECALL_MIN = 0.22         # 相关度够这么高才想起来（相邻两个字一组算重合）
PROMOTE_HITS, PROMOTE_DAYS = 3, 2   # 候选见到这么多次、在这么多天里见到，才确认
CANDIDATE_DAYS = 30       # 候选这么多天都没再见到，忘掉
CANDIDATE_WEIGHT = 0.6    # 用的时候候选打几折
SOURCES = {"video": "刷到的视频", "comment": "评论区", "dm": "私信"}


def _grams(text: str) -> set:
    t = re.sub(r"[\s\W_]+", "", text or "", flags=re.U).lower()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _sim(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(4, min(len(a), len(b)))


class Learner:
    def __init__(self, db: Database, router=None):
        self.db = db
        self.router = router
        self._last_try = 0.0
        self.index = TextIndex(db, "know_fts")
        try:
            self.index.sync("SELECT id, fact || ' ' || topic AS t FROM knowledge")
        except Exception:  # noqa: BLE001
            log.warning("同步学到的东西的索引失败", exc_info=True)

    # ---------- 记素材 ----------
    def note(self, source: str, text: str, ref: str = "") -> None:
        text = (text or "").strip()
        if len(text) < 6:
            return
        try:
            if self.db.query("SELECT 1 FROM learn_notes WHERE text=? AND done=0 LIMIT 1", (text[:600],)):
                return
            self.db.execute("INSERT INTO learn_notes(source, ref, text, created_at) VALUES(?,?,?,?)",
                            (source if source in SOURCES else "video", ref[:80], text[:600], now_str()))
        except Exception:  # noqa: BLE001 —— 记不下来不影响她干活
            log.warning("学习素材没记下来", exc_info=True)

    # ---------- 整理 ----------
    def due(self, now: datetime | None = None) -> bool:
        n = self.db.query("SELECT COUNT(*) AS n FROM learn_notes WHERE done=0")[0]["n"]
        if n >= BATCH_MIN:
            return True
        if not n:
            return False
        last = self.db.get_json("learn_last", "") or ""
        try:
            return (now or datetime.now()) - datetime.strptime(last, "%Y-%m-%d %H:%M:%S") >= timedelta(hours=DAILY_HOURS)
        except ValueError:
            return True

    def consolidate(self, system: str, force: bool = False) -> float:
        """整理一批学习素材，合并进“学到的东西”。返回花的钱。"""
        if self.router is None or (not force and not self.due()):
            return 0.0
        if not force and time.time() - self._last_try < 600:
            return 0.0               # 刚失败过：过一会儿再试
        self._last_try = time.time()
        notes = self.db.query("SELECT * FROM learn_notes WHERE done=0 ORDER BY id LIMIT ?", (BATCH_MAX,))
        if not notes:
            return 0.0
        from .brain import parse_json

        listed = "\n".join(f"{i}. [{SOURCES.get(n['source'], n['source'])}{'·' + n['ref'] if n['ref'] else ''}] "
                           f"{n['text'][:300]}" for i, n in enumerate(notes, 1))
        prompt = (
            "下面是你最近刷抖音时的见闻（看的视频、评论区里大家说的、私信里和别人聊的）。像一个爱学习的人一样，"
            "从里面挑出你学到的、以后用得上的新东西：网络梗和流行说法是什么意思、生活小常识、某个领域的知识、"
            "大家对一件事的普遍看法、某类视频的门道……\n"
            "要求：每条一句话（40 字以内），写成你自己理解后的说法；只写素材里真的有的，不要编；"
            "不要记具体某个人的事和隐私（名字、住址、联系方式、谁说了什么）；没什么可学的就少写或不写。\n\n"
            f"{listed}\n\n"
            '只输出 JSON：{"items": [{"topic": "属于哪方面（2～4 个字）", "fact": "学到的东西", "from": 素材编号}]}'
        )
        cost = 0.0
        try:
            r = self.router.call("write", system, prompt, max_tokens=900, temperature=0.3)
            cost += float(getattr(r, "cost", 0.0) or 0.0)
            items = [x for x in (parse_json(r.text).get("items") or []) if isinstance(x, dict) and x.get("fact")]
        except Exception:  # noqa: BLE001
            log.warning("整理学到的东西失败（第一步）", exc_info=True)
            return cost
        items = items[:20]
        ops = []
        if items:
            cands = []
            for i, it in enumerate(items, 1):
                near = self.similar(str(it.get("fact")), str(it.get("topic") or ""), limit=3)
                old = "；".join(f"#{k['id']}「{k['fact']}」" for k in near) or "（没有相近的）"
                cands.append(f"{i}. 新学的：「{str(it['fact'])[:80]}」（{str(it.get('topic') or '')[:6]}）\n   已经知道的相近的：{old}")
            prompt2 = (
                "你在整理自己学到的东西。下面每一条新学的，都附上了你以前学到的、相近的几条。逐条决定怎么处理：\n"
                "ADD：是新东西，新增；UPDATE：和以前某条说的是一回事，把两条合成一句更完整、更准确的（给出那条的 id 和合并后的话）；"
                "DELETE：新学的说明以前某条是错的、过时了（给出那条的 id，新学的会另外新增）；NOOP：已经知道了，不用记。\n\n"
                + "\n".join(cands)
                + '\n\n只输出 JSON：{"ops": [{"i": 新学的编号, "op": "ADD/UPDATE/DELETE/NOOP", "id": 以前那条的 id（ADD/NOOP 不用填）, '
                  '"fact": "UPDATE 时合并后的话"}]}'
            )
            try:
                r2 = self.router.call("write", system, prompt2, max_tokens=900, temperature=0.2)
                cost += float(getattr(r2, "cost", 0.0) or 0.0)
                ops = [x for x in (parse_json(r2.text).get("ops") or []) if isinstance(x, dict)]
            except Exception:  # noqa: BLE001
                log.warning("整理学到的东西失败（第二步），新学的直接记下", exc_info=True)
                ops = [{"i": i, "op": "ADD"} for i in range(1, len(items) + 1)]
        added = self._apply(items, ops, notes)
        ids = [n["id"] for n in notes]
        self.db.execute("UPDATE learn_notes SET done=1 WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)
        self.db.execute("DELETE FROM learn_notes WHERE done=1 AND created_at < ?",
                        ((datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),))
        self.db.set_json("learn_last", now_str())
        log.info("整理学到的东西：%d 条素材 → 新学 %d 条，花了 %.3f 元", len(notes), added, cost)
        if added:
            self.db.add_event("info", f"今天学到了 {added} 样新东西", "可以在“记忆 → 她学到的”里看")
        self.fade()
        return cost

    def _apply(self, items: list[dict], ops: list[dict], notes: list[dict]) -> int:
        """按模型的决定改“学到的东西”。没给出决定的新条目当作 ADD。返回新增 + 合并了几条。"""
        decided = {}
        for o in ops:
            try:
                decided[int(o.get("i"))] = o
            except (TypeError, ValueError):
                continue
        n = 0
        now = now_str()
        for i, it in enumerate(items, 1):
            o = decided.get(i, {"op": "ADD"})
            op = str(o.get("op") or "ADD").upper()
            fact = str(it.get("fact") or "").strip()[:120]
            topic = re.sub(r"\s+", "", str(it.get("topic") or ""))[:8]
            try:
                src = notes[int(it.get("from")) - 1]
            except (TypeError, ValueError, IndexError):
                src = notes[0] if notes else {"source": "video", "ref": ""}
            try:
                kid = int(o.get("id") or 0)
            except (TypeError, ValueError):
                kid = 0
            exists = bool(kid) and bool(self.db.query("SELECT 1 FROM knowledge WHERE id=?", (kid,)))
            if op == "NOOP":
                if exists:
                    self.reinforce(kid, 0.1)
                continue
            if op == "UPDATE" and exists:
                merged = str(o.get("fact") or fact).strip()[:120]
                self.db.execute("UPDATE knowledge SET fact=?, topic=CASE WHEN ?!='' THEN ? ELSE topic END, "
                                "updated_at=? WHERE id=?", (merged, topic, topic, now, kid))
                self.reinforce(kid, 0.15)
                n += 1
                continue
            if op == "DELETE" and exists:
                self.db.execute("DELETE FROM knowledge WHERE id=?", (kid,))
                self.index.drop(kid)
            if fact and not self.db.query("SELECT 1 FROM knowledge WHERE fact=?", (fact,)):
                kid2 = self.db.execute(
                    "INSERT INTO knowledge(topic, fact, source, ref, confidence, created_at, updated_at, status, hits, "
                    "days) VALUES(?,?,?,?,?,?,?,'candidate',1,?)",
                    (topic, fact, src["source"], src["ref"], 0.4, now, now, today_str())).lastrowid
                self.index.put(kid2, f"{fact} {topic}")
                n += 1
        return n

    def reinforce(self, kid: int, bump: float = 0.1) -> None:
        """又在别处见到了同样的说法：记一次；见够次数、够天数就从候选升为确认。"""
        rows = self.db.query("SELECT * FROM knowledge WHERE id=?", (kid,))
        if not rows:
            return
        r = rows[0]
        days = [d for d in (r["days"] or "").split(",") if d]
        if today_str() not in days:
            days.append(today_str())
        days = days[-5:]
        hits = int(r["hits"] or 1) + 1
        status = r["status"]
        conf = min(1.0, float(r["confidence"]) + bump)
        if status == "candidate" and hits >= PROMOTE_HITS and len(days) >= PROMOTE_DAYS:
            status, conf = "stable", max(conf, 0.8)
            log.info("学到的东西确认了：%s", r["fact"][:40])
        self.db.execute("UPDATE knowledge SET hits=?, days=?, status=?, confidence=?, updated_at=? WHERE id=?",
                        (hits, ",".join(days), status, conf, now_str(), kid))

    def confirm(self, kid: int) -> None:
        """你在界面上点了“确认”：直接算确认过的。"""
        self.db.execute("UPDATE knowledge SET status='stable', confidence=MAX(confidence, 0.8), updated_at=? WHERE id=?",
                        (now_str(), kid))

    def _candidates_rows(self, text: str, limit: int) -> list[dict]:
        """全文索引先挑出一批相关的；索引用不了时退回读最近的 1500 条。"""
        self.index.sync("SELECT id, fact || ' ' || topic AS t FROM knowledge", table="knowledge")
        hits = self.index.search(text, limit)
        if hits:
            ids = [i for i, _ in hits]
            return self.db.query(f"SELECT * FROM knowledge WHERE id IN ({','.join('?' * len(ids))})", ids)
        if self.index.ok:
            return []
        return self.db.query("SELECT * FROM knowledge ORDER BY updated_at DESC LIMIT 1500")

    # ---------- 用 ----------
    def similar(self, text: str, topic: str = "", limit: int = 3) -> list[dict]:
        q = _grams(text)
        rows = self._candidates_rows(f"{text} {topic}", 40)
        scored = []
        for r in rows:
            s = _sim(q, _grams(r["fact"])) + (0.3 if topic and r["topic"] == topic else 0.0)
            if s >= 0.25:
                scored.append((s, r))
        scored.sort(key=lambda x: -x[0])
        return [r for _, r in scored[:limit]]

    def recall(self, query: str, topic: str = "", limit: int = 3, touch: bool = True) -> list[dict]:
        """和现在聊的事相关的几条（相关度够高才算），被用到的记得更牢。"""
        q = _grams(query)
        if not q and not topic:
            return []
        rows = self._candidates_rows(f"{query} {topic}", 60)
        scored = []
        for r in rows:
            s = _sim(q, _grams(r["fact"] + r["topic"])) + (0.35 if topic and r["topic"] and r["topic"] in topic else 0.0)
            if r["status"] == "candidate":
                s *= CANDIDATE_WEIGHT            # 还没确认的：要更相关才用
            if s >= RECALL_MIN:
                scored.append((s * (0.6 + float(r["confidence"])), r))
        scored.sort(key=lambda x: -x[0])
        picked = [r for _, r in scored[:limit]]
        if touch and picked:
            self.db.execute("UPDATE knowledge SET uses=uses+1, used_at=?, confidence=MIN(1.0, confidence+0.02) "
                            "WHERE id IN (%s)" % ",".join("?" * len(picked)), (now_str(), *[r["id"] for r in picked]))
        return picked

    def context(self, query: str, topic: str = "", limit: int = 3) -> str:
        try:
            picked = self.recall(query, topic, limit)
        except Exception:  # noqa: BLE001
            return ""
        if not picked:
            return ""
        return ("你自己平时学到的、可能用得上的（自然地用，不要硬塞，也不用说“我学到”）：\n"
                + "\n".join(f"- {r['fact']}" + ("（还不太确定）" if r["status"] == "candidate" else "") for r in picked))

    def fade(self) -> int:
        """忘掉：很久没用、又不太确定的；候选 30 天没再见到的；太多了先忘最不常用、最不确定的。"""
        now = datetime.now()
        cutoff = (now - timedelta(days=FADE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        cand = (now - timedelta(days=CANDIDATE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        gone = [r["id"] for r in self.db.query(
            "SELECT id FROM knowledge WHERE (confidence < 0.7 AND COALESCE(NULLIF(used_at,''), updated_at) < ?) "
            "OR (status='candidate' AND updated_at < ?)", (cutoff, cand))]
        total = self.db.query("SELECT COUNT(*) AS n FROM knowledge")[0]["n"] - len(gone)
        if total > MAX_FACTS:
            gone += [r["id"] for r in self.db.query(
                "SELECT id FROM knowledge WHERE id NOT IN (%s) ORDER BY (status='stable') ASC, confidence + uses * 0.05 ASC, "
                "updated_at ASC LIMIT ?" % (",".join("?" * len(gone)) or "0"), (*gone, total - MAX_FACTS))]
        for kid in gone:
            self.db.execute("DELETE FROM knowledge WHERE id=?", (kid,))
            self.index.drop(kid)
        return len(gone)

    # ---------- 界面 ----------
    def listing(self, q: str = "", limit: int = 300) -> dict:
        where, params = "1=1", []
        if q:
            where, params = "(fact LIKE ? OR topic LIKE ?)", [f"%{q}%", f"%{q}%"]
        rows = self.db.query(f"SELECT * FROM knowledge WHERE {where} ORDER BY updated_at DESC LIMIT ?", (*params, limit))
        waiting = self.db.query("SELECT COUNT(*) AS n FROM learn_notes WHERE done=0")[0]["n"]
        total = self.db.query("SELECT COUNT(*) AS n FROM knowledge")[0]["n"]
        cands = self.db.query("SELECT COUNT(*) AS n FROM knowledge WHERE status='candidate'")[0]["n"]
        return {"knowledge": rows, "waiting": waiting, "total": total, "candidates": cands}

    def delete(self, kid: int) -> None:
        self.db.execute("DELETE FROM knowledge WHERE id=?", (kid,))
        self.index.drop(kid)
