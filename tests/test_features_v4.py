"""第四轮：存储优化（归档压缩、空间回收、全文索引）、记忆分层、学到的东西要多次确认、日报、自检、成长、精力节律、好感度。"""
from __future__ import annotations

import json
import random
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_features_v2 import make_db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WORDS = ["橘猫", "打哈欠", "做饭", "红烧肉", "旅行", "大理", "洱海", "健身", "深蹲", "考研", "英语", "吉他", "弹唱",
         "宝宝", "辅食", "穿搭", "通勤", "数码", "开箱", "露营", "日落", "搞笑", "段子", "舞蹈", "翻跳"]


def _ts(days_ago: float, h: int = 12) -> str:
    return (datetime.now() - timedelta(days=days_ago)).replace(hour=h, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")


def _fill(db, n_days: int = 60, per_day: int = 30, seed: int = 1) -> None:
    """造一些旧记录：每天若干条看视频、操作记录、评论区互动。"""
    rnd = random.Random(seed)
    with db._lock:
        cur = db.conn.cursor()
        for d in range(n_days, 0, -1):
            for i in range(per_day):
                ts = _ts(d, 9 + i % 12)
                desc = "".join(rnd.choice(WORDS) for _ in range(4)) + f" #{rnd.choice(WORDS)}"
                vid = f"v{d}_{i}"
                comment = f"太{rnd.choice(WORDS)}了吧" if i % 5 == 0 else ""
                cur.execute("INSERT INTO videos(video_id, author, title, url, liked, collected, comment, interest, "
                            "decision, reason, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (vid, f"作者{i % 7}", desc, f"https://www.douyin.com/video/{vid}", int(i % 3 == 0), 0,
                             comment, rnd.randint(2, 9), "watch", "看着挺有意思", ts))
                cur.execute("INSERT INTO events(ts, kind, title, detail, ref) VALUES(?,?,?,?,?)",
                            (ts, "like" if i % 3 == 0 else "skip", f"看了：{desc[:20]}", "", f"video:{cur.lastrowid}"))
                if i % 4 == 0:
                    cur.execute("INSERT INTO comment_actions(cid, video_id, user_name, text, liked, reply, video_title, "
                                "video_author, video_url, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (f"c{d}_{i}", vid, "路人", "哈哈哈好可爱", 1, "是吧！" if i % 8 == 0 else "", desc,
                                 "作者", "", ts))
        db.conn.commit()


# ---------------------------------------------------------------- 压缩
class PackTests(unittest.TestCase):
    def test_dictionary_makes_small_records_much_smaller(self):
        from app.storage import pack, train_zdict, unpack
        rows = [json.dumps({"kind": "like", "title": f"看了：{a}{b}", "ts": f"2026-01-{i % 28 + 1:02d} 12:00:00"},
                           ensure_ascii=False).encode() for i, (a, b) in enumerate(zip(WORDS * 8, reversed(WORDS * 8)))]
        zd = train_zdict(rows)
        self.assertLessEqual(len(zd), 32 * 1024)
        one = rows[3]
        c1, plain = pack(one)
        c2, with_dict = pack(one, zd)
        self.assertEqual(c2, "zd")
        self.assertLess(len(with_dict), len(plain))
        self.assertEqual(unpack(c2, with_dict, zd), one)
        self.assertEqual(unpack(c1, plain), one)
        big = b"\n".join(rows) * 20
        codec, data = pack(big)
        self.assertIn(codec, ("z", "xz"))
        self.assertEqual(unpack(codec, data), big)


# ---------------------------------------------------------------- 归档
class KeeperTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        _fill(self.db)

    def test_old_rows_are_archived_compressed_and_still_readable(self):
        db = self.db
        old_day = _ts(55)[:10]
        before = db.events_page(1, 100, day=old_day)
        self.assertEqual(before["total"], 30)
        detail_before = db.detail(before["items"][0]["ref"])
        done = db.keeper.maintain()
        self.assertGreater(done["archived"].get("events", 0), 0)
        self.assertGreater(done["archived"].get("videos", 0), 0)
        # 表里只剩最近 45 天的
        oldest = db.query("SELECT MIN(created_at) AS t FROM videos")[0]["t"]
        self.assertGreaterEqual(oldest[:10], (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d"))
        # 归档的日子照样能翻、能看详情
        after = db.events_page(1, 100, day=old_day)
        self.assertTrue(after.get("archived"))
        self.assertEqual(after["total"], 30)
        self.assertEqual({r["title"] for r in after["items"]}, {r["title"] for r in before["items"]})
        self.assertEqual(db.detail(before["items"][0]["ref"]), detail_before)
        self.assertIn(old_day, [d["day"] for d in db.event_days()])
        self.assertEqual(next(d["n"] for d in db.event_days() if d["day"] == old_day), 30)
        self.assertTrue(db.activity("likes", old_day))
        self.assertTrue(db.activity("comments", old_day))
        # 去重照样认得：看过的视频、评论过的视频、处理过的评论
        self.assertTrue(db.seen_video("v55_0"))
        self.assertTrue(db.commented_video("v55_0"))
        self.assertFalse(db.commented_video("v55_1"))
        self.assertTrue(db.comment_done("c55_0"))
        self.assertIsNotNone(db.detail("cmt:c55_0"))
        # 压缩率
        u = db.keeper.usage()
        self.assertGreater(u["archive"]["ratio"], 4)
        self.assertEqual(u["archive"]["rows"], sum(done["archived"].values()))

    def test_space_is_given_back_to_the_disk(self):
        db = self.db
        db.keeper.maintain(vacuum=False)
        size_before = Path(db.path).stat().st_size + Path(str(db.path) + "-wal").stat().st_size
        out = db.keeper.reclaim()
        self.assertTrue(out.get("vacuumed"))
        self.assertEqual(int(db.conn.execute("PRAGMA auto_vacuum").fetchone()[0]), 2)     # 以后增量回收
        size_after = Path(db.path).stat().st_size + Path(str(db.path) + "-wal").stat().st_size
        self.assertLess(size_after, size_before)
        db.execute("DELETE FROM comment_actions")
        out2 = db.keeper.reclaim()
        self.assertFalse(out2.get("vacuumed"))
        self.assertEqual(int(db.conn.execute("PRAGMA freelist_count").fetchone()[0]), 0)

    def test_growth_counts_survive_archiving(self):
        from app.pet import growth
        db = self.db
        for d in (50, 49):
            db.execute("INSERT INTO events(ts, kind, title, detail, ref) VALUES(?,?,?,?,?)",
                       (_ts(d), "owner_chat", "找主人聊天", "", ""))
        before = growth.status(db)["owner_touches"]
        db.keeper.maintain(vacuum=False)
        self.assertEqual(growth.status(db)["owner_touches"], before)

    def test_dm_history_keeps_the_latest_per_person(self):
        from app import storage
        db = self.db
        with mock.patch.object(storage, "DM_KEEP_PER_USER", 20):
            for i in range(50):
                db.execute("INSERT INTO dm_history(user_name, role, content, ts) VALUES(?,?,?,?)",
                           ("小明", "them" if i % 2 else "me", f"第{i}句", _ts(1)))
            self.assertEqual(db.keeper.trim_dm_history(), 30)
        left = db.query("SELECT content FROM dm_history WHERE user_name='小明' ORDER BY id")
        self.assertEqual(len(left), 20)
        self.assertEqual(left[-1]["content"], "第49句")

    def test_debug_and_browser_cache_cleanup(self):
        from app.storage import Keeper
        tmp = Path(tempfile.mkdtemp())
        dbg = tmp / "debug"
        dbg.mkdir()
        old = dbg / "old.png"
        old.write_bytes(b"x" * 1000)
        new = dbg / "new.png"
        new.write_bytes(b"y" * 1000)
        t = time.time() - 20 * 86400
        import os
        os.utime(old, (t, t))
        self.assertEqual(Keeper.clean_debug(dbg), 1000)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        prof = tmp / "profile"
        (prof / "Default" / "Cache").mkdir(parents=True)
        (prof / "Default" / "Cache" / "f").write_bytes(b"z" * 5000)
        (prof / "Default" / "Cookies").write_bytes(b"login")
        self.assertEqual(Keeper.clean_browser_cache(prof, limit=10_000), 0)          # 没超过上限不清
        self.assertEqual(Keeper.clean_browser_cache(prof, limit=1000), 5000)
        self.assertTrue((prof / "Default" / "Cookies").exists())                       # 登录状态还在


# ---------------------------------------------------------------- 全文索引
class SearchIndexTests(unittest.TestCase):
    def test_chinese_is_split_into_pairs(self):
        from app.agent.search_index import match_query, tokens
        self.assertEqual(tokens("橘猫打哈欠"), ["橘猫", "猫打", "打哈", "哈欠"])
        self.assertIn("vlog", tokens("我的VLOG日常"))
        self.assertIn('"橘猫"', match_query("橘猫"))
        self.assertEqual(match_query("!!"), "")

    def test_search_and_fallback(self):
        from app.agent.search_index import TextIndex
        db = make_db()
        idx = TextIndex(db, "t_fts")
        idx.put(1, "今天做了红烧肉")
        idx.put(2, "去大理看洱海日落")
        idx.put(3, "红烧肉要先炒糖色")
        hits = [i for i, _ in idx.search("想学做红烧肉")]
        self.assertEqual(set(hits), {1, 3})
        idx.drop(1)
        self.assertEqual([i for i, _ in idx.search("红烧肉")], [3])
        self.assertEqual(idx.count(), 2)


# ---------------------------------------------------------------- 记忆
class MemoryTierTests(unittest.TestCase):
    def setUp(self):
        from app.agent.memory import Memory
        self.db = make_db()
        self.mem = Memory(self.db)

    def test_recall_finds_old_memories_through_the_index(self):
        mem = self.mem
        mem.remember("video", "看了一个教做红烧肉的视频，先炒糖色", tags="美食")
        for i in range(300):
            mem.remember("video", f"看了第{i}个{WORDS[i % len(WORDS)]}视频", tags="日常")
        got = mem.recall("红烧肉怎么做")
        self.assertTrue(got and "红烧肉" in got[0]["summary"])

    def test_faded_memories_are_kept_compressed_and_come_back(self):
        mem, db = self.mem, self.db
        mem.remember("dm", "小明说他下个月要去大理洱海旅行", person="小明", scope="dm:小明", dedupe="dm:小明:大理")
        mem.remember("thread", "路人在评论区说哈哈", scope="thread:1")
        db.execute("UPDATE memories SET strength=0.01, touched_at=?", (_ts(400),))
        self.assertEqual(mem.fade(), 2)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM memories")[0]["n"], 0)
        faded = mem.archived_listing()
        self.assertEqual(len(faded), 1)                           # 评论区路人的那条直接删了
        self.assertIn("大理", faded[0]["summary"])
        self.assertEqual(mem.archived_listing("洱海")[0]["id"], faded[0]["id"])
        # 有人提起来就想起来了
        back = mem.revive("我到大理洱海啦", person="小明")
        self.assertEqual(len(back), 1)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM memory_archive")[0]["n"], 0)
        self.assertTrue(mem.recall("大理旅行", person="小明"))

    def test_restore_does_not_lose_a_memory_recorded_again(self):
        mem, db = self.mem, self.db
        mem.remember("dm", "小红说她养了一只橘猫", person="小红", dedupe="dm:小红:猫")
        db.execute("UPDATE memories SET strength=0.01, touched_at=?", (_ts(400),))
        mem.fade()
        rid = mem.archived_listing()[0]["id"]
        mem.remember("dm", "小红说她养了一只橘猫叫大橘", person="小红", dedupe="dm:小红:猫")
        self.assertTrue(mem.restore(rid))
        rows = db.query("SELECT * FROM memories WHERE dedupe='dm:小红:猫'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM memory_archive")[0]["n"], 0)

    def test_context_budget(self):
        from app.agent.memory import budget_join
        out = budget_join(["a" * 50, "b" * 50, "c" * 500], 200)
        self.assertTrue(out.startswith("a" * 50))
        self.assertLessEqual(len(out), 200)
        self.assertEqual(budget_join(["x" * 10, "", "y" * 10], 100), "x" * 10 + "\n" + "y" * 10)

    def test_affection_moves_with_tone_and_drifts_back(self):
        mem, db = self.mem, self.db
        mem.remember_dm("小明", "你好呀，谢谢你昨天的推荐，太喜欢了", "不客气～")
        a1 = db.query("SELECT affection FROM people WHERE name='小明'")[0]["affection"]
        self.assertGreater(a1, 30)
        mem.remember_dm("小刚", "滚，傻X", "……")
        a2 = db.query("SELECT affection FROM people WHERE name='小刚'")[0]["affection"]
        self.assertLess(a2, 30)
        db.execute("UPDATE people SET affection=90, last_at=? WHERE name='小明'", (_ts(10),))
        mem.drift_affection()
        a3 = db.query("SELECT affection FROM people WHERE name='小明'")[0]["affection"]
        self.assertAlmostEqual(a3, 90 - 60 * 0.02, places=3)
        mem.drift_affection()                                        # 一天只一次
        self.assertAlmostEqual(db.query("SELECT affection FROM people WHERE name='小明'")[0]["affection"], a3)
        ctx = mem.person_context("小明", "最近在干嘛")
        self.assertIn("好感", ctx)


# ---------------------------------------------------------------- 学到的东西
class KnowledgeCandidateTests(unittest.TestCase):
    def setUp(self):
        from app.agent.learner import Learner
        self.db = make_db()
        self.learner = Learner(self.db, mock.Mock())

    def test_new_facts_start_as_candidates_and_get_promoted(self):
        L, db = self.learner, self.db
        notes = [{"source": "video", "ref": "1"}]
        L._apply([{"fact": "“绝绝子”是夸人特别好的意思", "topic": "网络梗", "from": 1}], [], notes)
        k = db.query("SELECT * FROM knowledge")[0]
        self.assertEqual(k["status"], "candidate")
        self.assertIn("还不太确定", L.context("绝绝子是什么意思", "网络梗"))
        L.reinforce(k["id"])
        self.assertEqual(db.query("SELECT status FROM knowledge")[0]["status"], "candidate")   # 同一天见到不够
        db.execute("UPDATE knowledge SET days=? WHERE id=?", (_ts(3)[:10], k["id"]))
        L.reinforce(k["id"])
        self.assertEqual(db.query("SELECT status FROM knowledge")[0]["status"], "stable")
        self.assertNotIn("还不太确定", L.context("绝绝子是什么意思", "网络梗"))
        self.assertEqual(L.listing()["candidates"], 0)

    def test_confirm_and_candidate_forgetting(self):
        L, db = self.learner, self.db
        now = _ts(0)
        for f in ("候选一：猫咪一天睡十几个小时", "候选二：洱海在大理"):
            db.execute("INSERT INTO knowledge(topic, fact, source, created_at, updated_at, status) "
                       "VALUES('常识',?,'video',?,?,'candidate')", (f, now, now))
        ids = [r["id"] for r in db.query("SELECT id FROM knowledge ORDER BY id")]
        L.confirm(ids[0])
        db.execute("UPDATE knowledge SET updated_at=? WHERE id=?", (_ts(40), ids[1]))
        L.fade()
        left = db.query("SELECT id, status FROM knowledge")
        self.assertEqual([(r["id"], r["status"]) for r in left], [(ids[0], "stable")])
        self.assertEqual([r["id"] for r in L.similar("猫咪睡觉的时间", "常识")], [ids[0]])


# ---------------------------------------------------------------- 同样的内容：分段指纹
class BandTests(unittest.TestCase):
    def test_bands_find_near_duplicates_among_many(self):
        from app.agent.dedupe import ContentMarks
        from app.fingerprint import bands
        self.assertEqual(len(bands(0x1234_5678_9ABC_DEF0)), 4)
        self.assertEqual(sorted(bands(0x1234_5678_9ABC_DEF0)), sorted([0x1234, 0x5678, 0x9ABC, 0xDEF0]))
        db = make_db()
        marks = ContentMarks(db)
        rnd = random.Random(3)
        for i in range(800):
            v = {"video_id": f"x{i}", "desc": "".join(rnd.choice(WORDS) for _ in range(5))}
            marks.record(v, marks.check(v, ""), {"like": True})
        db.execute("UPDATE content_marks SET day='2000-01-01'")
        target = {"video_id": "orig", "desc": "橘猫打哈欠也太可爱了吧 今天的猫猫日常"}
        marks.record(target, marks.check(target, ""), {"comment": True})
        for i in range(600):                                   # 原视频被挤出“最近 500 条”
            v = {"video_id": f"y{i}", "desc": "".join(rnd.choice(WORDS) for _ in range(5))}
            marks.record(v, marks.check(v, ""), {"like": True})
        chk = marks.check({"video_id": "repost", "desc": "橘猫打哈欠也太可爱了吧！！今天的猫猫日常"}, "")
        self.assertIn("comment", chk["blocked"])


# ---------------------------------------------------------------- 日报
class DigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.p = mock.patch("app.agent.digest.REPORTS_DIR", self.tmp)
        self.p.start()
        self.addCleanup(self.p.stop)

    def test_build_write_and_brief(self):
        from app.agent import digest
        db = make_db()
        y = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        db.execute("INSERT INTO videos(video_id, author, title, url, liked, collected, comment, interest, decision, reason, "
                   "topic, created_at) VALUES('1','小橘','橘猫打哈欠','https://www.douyin.com/video/1',1,1,'太可爱了吧',9,"
                   "'watch','猫猫好可爱','萌宠',?)", (y + " 10:00:00",))
        db.execute("INSERT INTO videos(video_id, author, title, url, liked, interest, decision, created_at) "
                   "VALUES('2','某人','广告','',0,1,'skip',?)", (y + " 10:05:00",))
        db.execute("INSERT INTO daily_usage(day, videos, likes, comments) VALUES(?,2,1,1) ON CONFLICT(day) DO UPDATE SET "
                   "videos=2, likes=1, comments=1", (y,))
        self.assertEqual(digest.due(db), [y])
        rep = digest.build(db, y)
        self.assertIn("橘猫打哈欠", rep["md"])
        self.assertIn("https://www.douyin.com/video/1", rep["md"])
        self.assertIn("猫猫好可爱", rep["md"])
        self.assertNotIn("广告", "\n".join(p["title"] or "" for p in rep["picks"]))
        self.assertTrue(digest.write(db, y))
        self.assertEqual(digest.due(db), [])
        self.assertIn("橘猫", digest.read(y))
        self.assertEqual(digest.read("../../etc"), "")
        b = digest.brief_for_owner(db)
        self.assertIn("2 个视频", b)
        digest.mark_told(db)
        self.assertEqual(digest.brief_for_owner(db), "")
        # 什么都没有的日子不写文件，也不会反复再算
        empty = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        db.execute("INSERT INTO messages(kind, user_name, content, status, created_at) VALUES('dm','a','hi','pending',?)",
                   (empty + " 10:00:00",))
        self.assertIn(empty, digest.due(db))
        self.assertEqual(digest.write(db, empty), "")
        self.assertNotIn(empty, digest.due(db))


# ---------------------------------------------------------------- 自检
class DoctorTests(unittest.TestCase):
    def test_rules_cards_feedback_and_effect(self):
        from app.agent.doctor import Doctor
        db = make_db()
        doc = Doctor(db)
        self.assertEqual(doc.check({}), [])
        for _ in range(3):
            db.add_event("error", "模型调用失败", "401 invalid key")
        for i in range(12):
            db.execute("INSERT INTO messages(kind, user_name, content, status, reason, created_at) "
                       "VALUES('dm',?,?,'pending','敏感话题',?)", (f"u{i}", "hi", _ts(0)))
        db.execute("INSERT INTO videos(video_id, created_at) VALUES('v', ?)", (_ts(0.2),))
        new = doc.check({"running": True, "active": True, "sizes": {"db": 600 * 1048576}})
        rules = {c["rule"] for c in new}
        self.assertTrue({"llm_fail", "pending_pile", "disk"} <= rules)
        self.assertEqual(doc.check({"running": True, "active": True}), [])        # 同一条规则一天只提醒一次
        cards = doc.cards()
        llm = next(c for c in cards if c["rule"] == "llm_fail")
        self.assertIn("401", llm["evidence"])
        doc.feedback(llm["id"], "helpful")
        doc.feedback(next(c for c in cards if c["rule"] == "pending_pile")["id"], "useless")
        with self.assertRaises(ValueError):
            doc.feedback(llm["id"], "whatever")
        eff = doc.effect()
        self.assertEqual(eff["d7"]["helpful"], 1)
        self.assertEqual(eff["d7"]["rate"], 50)
        self.assertEqual(len(doc.cards()), len(cards) - 2)


# ---------------------------------------------------------------- 成长
class GrowthTests(unittest.TestCase):
    def test_xp_levels_stages_and_card(self):
        from app.pet import growth
        db = make_db()
        g = growth.status(db)
        self.assertEqual((g["xp"], g["level"], g["stage_name"]), (0, 1, "萌新小号"))
        for d in range(4):
            day = (datetime.now() - timedelta(days=d)).strftime("%Y-%m-%d")
            db.execute("INSERT INTO daily_usage(day, videos, likes, comments) VALUES(?,?,?,?) ON CONFLICT(day) DO UPDATE "
                       "SET videos=excluded.videos, likes=excluded.likes, comments=excluded.comments", (day, 500, 50, 10))
        g = growth.status(db)
        self.assertEqual(g["xp"], 4 * growth.DAY_CAP)                # 一天最多 300
        self.assertEqual(g["level"], 13)
        self.assertEqual(g["stage_name"], "熟练网友")
        self.assertEqual(g["stage_next"], 1600)
        out = growth.card(db, Path(tempfile.mkdtemp()) / "card.png")
        from PIL import Image
        with Image.open(out) as im:
            self.assertEqual(im.size, (1200, 630))


# ---------------------------------------------------------------- 精力节律 / 勿扰 / 关键词
class RhythmTests(unittest.TestCase):
    def test_energy_follows_the_clock(self):
        from app.agent.mood import MoodEngine
        m = MoodEngine(make_db())
        st = m._load()
        at = lambda h, mi=0: m._energy_target(st, datetime.now().replace(hour=h, minute=mi).timestamp())  # noqa: E731
        self.assertGreater(at(8), at(12))
        self.assertGreater(at(12), at(22))
        self.assertLess(at(14), at(11))                              # 午饭后犯困
        self.assertLess(at(3), at(22))                               # 半夜最困
        self.assertGreater(at(7, 30), at(6))                         # 早上慢慢醒
        self.assertIn("【现在】", m.prompt())

    def test_quiet_hours_and_watch_keywords(self):
        from app.agent.runner import in_quiet_hours, watch_keywords
        r = {"owner_quiet_start": "23:30", "owner_quiet_end": "08:00"}
        day = datetime.now().replace(second=0, microsecond=0)
        self.assertTrue(in_quiet_hours(r, day.replace(hour=23, minute=45)))
        self.assertTrue(in_quiet_hours(r, day.replace(hour=3)))
        self.assertFalse(in_quiet_hours(r, day.replace(hour=8, minute=0)))
        self.assertFalse(in_quiet_hours({"owner_quiet_start": "10:00", "owner_quiet_end": "10:00"}, day.replace(hour=10)))
        self.assertTrue(in_quiet_hours({"owner_quiet_start": "13:00", "owner_quiet_end": "14:00"}, day.replace(hour=13, minute=30)))
        rules = {"watch_exclude": ["赌博"], "watch_prefer": ["猫"]}
        self.assertEqual(watch_keywords({"desc": "线上赌博教程"}, rules), ("赌博", ""))
        self.assertEqual(watch_keywords({"desc": "橘猫", "author": "x"}, rules), ("", "猫"))
        self.assertEqual(watch_keywords({"desc": "风景"}, rules), ("", ""))


# ---------------------------------------------------------------- 听声音的组件：先在小进程里试
class ListenProbeTests(unittest.TestCase):
    """2026-11-19：新装的 ctranslate2 4.9.0 在打包测试里一加载语音模型，整个进程就没了（没有报错，测试总结都没打印）。"""

    def setUp(self):
        from app.agent import listen
        self.listen = listen
        self.tmp = Path(tempfile.mkdtemp())
        for p in (mock.patch.object(listen, "PROBE_FILE", self.tmp / "probe.json"),
                  mock.patch.object(listen, "installed", return_value=True)):
            p.start()
            self.addCleanup(p.stop)
        self.calls = []

    def fake_run(self, outcome):
        """outcome：精度 → "ok" / "crash" / "error"。模拟小进程：写结果文件，或者什么都不写直接“崩溃”。"""
        def run(cmd, **kw):
            compute, out = cmd[-2], Path(cmd[-1])
            self.calls.append(compute)
            self.assertIn("--probe-listen", cmd)
            what = outcome[compute]
            if what == "ok":
                out.write_text(json.dumps({"ok": True, "compute": compute}), encoding="utf-8")
                return mock.Mock(returncode=0, stderr=b"")
            if what == "error":
                out.write_text(json.dumps({"ok": False, "reason": "RuntimeError: bad model"}), encoding="utf-8")
                return mock.Mock(returncode=3, stderr=b"")
            out.with_suffix(".trace").write_text("Windows fatal exception: access violation\n  File ctranslate2",
                                                 encoding="utf-8")
            return mock.Mock(returncode=3221225477, stderr=b"")
        return run

    def test_mock_browser_never_loads_the_real_model(self):
        ls = self.listen.Listener()
        with mock.patch.object(ls, "_load") as load:
            self.assertEqual(ls.listen(mock.Mock(), "v1"), "")          # 假浏览器给的“地址”不是列表
            self.assertEqual(ls.listen(mock.MagicMock(), "v1"), "")
        load.assert_not_called()

    def test_status_does_not_import_the_component(self):
        import sys
        sys.modules.pop("faster_whisper", None)
        with mock.patch.object(self.listen, "installed", return_value=False):
            self.assertFalse(self.listen.Listener().status()["installed"])
        self.assertNotIn("faster_whisper", sys.modules)

    def test_crash_in_the_probe_does_not_crash_the_app_and_is_remembered(self):
        import sys
        sys.modules.pop("faster_whisper", None)
        with mock.patch.object(self.listen.subprocess, "run", side_effect=self.fake_run({"int8": "crash", "float32": "crash"})):
            ls = self.listen.Listener()
            self.assertIsNone(ls._load("base"))
            self.assertIn("一加载就出错", ls.status()["broken"])
            self.assertIn("3221225477", ls.status()["broken"])
            self.assertIn("VC++ 运行库", ls.status()["broken"])          # 访问冲突：告诉用户多半是运行库太旧
            self.assertEqual(self.calls, ["int8", "float32"])
            self.assertIsNone(self.listen.Listener()._load("base"))     # 同样的版本不再试
            self.assertEqual(len(self.calls), 2)
        self.assertNotIn("faster_whisper", sys.modules)                 # 软件本身一直没导入它
        with mock.patch.object(self.listen, "_pkg_versions", return_value="ctranslate2=9.9"), \
                mock.patch.object(self.listen.subprocess, "run", side_effect=self.fake_run({"int8": "crash", "float32": "crash"})):
            self.listen.Listener()._load("base")                         # 组件换了版本：再试一次
        self.assertEqual(len(self.calls), 4)

    def test_falls_back_to_float32_and_loads_with_it(self):
        import sys
        import types
        sys.modules.pop("faster_whisper", None)
        made = []
        fake = types.ModuleType("faster_whisper")
        fake.WhisperModel = lambda *a, **kw: made.append(kw) or "MODEL"
        with mock.patch.object(self.listen.subprocess, "run", side_effect=self.fake_run({"int8": "crash", "float32": "ok"})):
            ls = self.listen.Listener()
            self.assertIsNone(ls._broken or None)
            real_import = __import__

            def imp(name, *a, **kw):
                if name == "faster_whisper":
                    sys.modules["faster_whisper"] = fake
                    return fake
                return real_import(name, *a, **kw)
            with mock.patch("builtins.__import__", side_effect=imp):
                self.assertEqual(ls._load("base"), "MODEL")
        sys.modules.pop("faster_whisper", None)
        self.assertEqual(made[0]["compute_type"], "float32")
        self.assertEqual(self.calls, ["int8", "float32"])
        ls2 = self.listen.Listener()
        self.assertEqual(ls2._probe("base"), "")                       # 记住了：直接用 float32
        self.assertEqual(ls2.compute, "float32")
        self.assertEqual(len(self.calls), 2)

    def test_model_not_ready_is_not_remembered(self):
        def run(cmd, **kw):
            Path(cmd[-1]).write_text(json.dumps({"ok": False, "reason": "下载失败", "transient": True}), encoding="utf-8")
            return mock.Mock(returncode=2, stderr=b"")
        with mock.patch.object(self.listen.subprocess, "run", side_effect=run):
            self.assertEqual(self.listen.Listener()._probe("small"), "下载失败")
        self.assertEqual(self.listen._read_probes(), {})

    def test_probe_main_loads_and_transcribes(self):
        import sys
        import types
        seen = {}

        class Model:
            def transcribe(self, audio, **kw):
                seen["n"] = len(audio)
                seen["vad"] = kw.get("vad_filter")
                return iter([]), None
        fake = types.ModuleType("faster_whisper")
        fake.WhisperModel = lambda *a, **kw: seen.update(compute=kw["compute_type"]) or Model()
        out = self.tmp / "r.json"
        with mock.patch.dict(sys.modules, {"faster_whisper": fake}):
            self.assertEqual(self.listen.probe_main(["base", "float32", str(out)]), 0)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8")), {"ok": True, "compute": "float32"})
        self.assertEqual((seen["compute"], seen["n"], seen["vad"]), ("float32", 32000, True))

    def test_main_runs_the_probe_without_opening_a_window(self):
        from app import main as m
        with mock.patch.object(m.sys, "argv", ["run.py", "--probe-listen", "base", "int8", "x.json"]), \
                mock.patch("app.agent.listen.probe_main", return_value=0) as pm, \
                mock.patch.object(m, "ensure_dirs") as ens:
            with self.assertRaises(SystemExit) as cm:
                m.main()
        self.assertEqual(cm.exception.code, 0)
        pm.assert_called_once_with(["base", "int8", "x.json"])
        ens.assert_not_called()

    def test_build_checks_the_component_and_shows_crashes(self):
        bat = (ROOT / "build.bat").read_text(encoding="utf-8")
        self.assertIn("tools\\check_listen.py", bat)
        self.assertIn("-X faulthandler -m unittest", bat)
        import importlib.util
        spec = importlib.util.spec_from_file_location("check_listen", ROOT / "tools" / "check_listen.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        names = [n for n, _ in mod.STEPS]
        self.assertIn("加载模型（int8）", names)
        self.assertIn("加载模型（float32）", names)


# ---------------------------------------------------------------- C++ 组件：VC++ 运行库太旧时一导入就崩
class NativeProbeTests(unittest.TestCase):
    """2026-11-19：那台电脑上 import onnxruntime 1.32.0 直接访问冲突（退出码 3221225477），
    打包时 PyInstaller 收集 onnxruntime 也死在这里。原因是 msvcp140.dll 低于 14.40。"""

    def setUp(self):
        from app import native_probe
        self.np = native_probe
        self.tmp = Path(tempfile.mkdtemp())
        p = mock.patch.object(native_probe, "PROBE_FILE", self.tmp / "native.json")
        p.start()
        self.addCleanup(p.stop)
        self.calls = 0

    def fake_run(self, ok: bool):
        def run(cmd, **kw):
            self.calls += 1
            self.assertEqual(cmd[-3:-1], ["--probe-import", "fakemod_xyz"])
            out = Path(cmd[-1])
            if ok:
                out.write_text('{"ok": true}', encoding="utf-8")
                return mock.Mock(returncode=0, stderr=b"")
            out.with_suffix(".trace").write_text("Windows fatal exception: access violation", encoding="utf-8")
            return mock.Mock(returncode=3221225477, stderr=b"")
        return run

    def test_crash_is_reported_with_the_fix_and_retried_after_runtime_update(self):
        np_ = self.np
        with mock.patch.object(np_.subprocess, "run", side_effect=self.fake_run(False)):
            reason = np_.import_ok("fakemod_xyz")
            self.assertIn("一加载就崩溃", reason)
            self.assertIn("aka.ms/vs/17/release/vc_redist.x64.exe", reason)
            self.assertEqual(np_.import_ok("fakemod_xyz"), reason)            # 记住了，不再开小进程
            self.assertEqual(self.calls, 1)
        with mock.patch.object(np_, "runtime_stamp", return_value="999-1"), \
                mock.patch.object(np_.subprocess, "run", side_effect=self.fake_run(True)):
            self.assertEqual(np_.import_ok("fakemod_xyz"), "")                # 更新了运行库：再试一次，好了
        self.assertEqual(self.calls, 2)

    def test_probe_child_imports_and_reports(self):
        out = self.tmp / "r.json"
        self.assertEqual(self.np.probe_import_main(["json", str(out)]), 0)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8")), {"ok": True})
        self.assertEqual(self.np.probe_import_main(["no_such_module_abc", str(out)]), 3)
        self.assertIn("ModuleNotFoundError", json.loads(out.read_text(encoding="utf-8"))["reason"])

    def test_probe_runs_for_real_in_a_child_process(self):
        """真的开一个小进程（run.py --probe-import）：确认入口接得上。"""
        mod = "tabnanny"                      # 标准库里一个平时用不到的模块：测试进程里还没导入过
        sys.modules.pop(mod, None)
        self.assertEqual(self.np.import_ok(mod), "")
        self.assertNotIn(mod, sys.modules)    # 是在小进程里导入的，不是这里
        self.assertTrue(list(self.np._read().values())[-1]["ok"])

    def test_in_process_probe_closes_its_trace_file_and_restores_faulthandler(self):
        """打包时的测试输出里一直有 “unclosed file … r.trace” 的提醒：在本进程里调用时要关掉调用栈文件。"""
        import builtins
        import faulthandler
        was = faulthandler.is_enabled()
        opened = []

        def spy(*a, **kw):
            f = builtins.open(*a, **kw)
            opened.append(f)
            return f
        with mock.patch("app.native_probe.open", side_effect=spy, create=True):
            self.np.probe_import_main(["json", str(self.tmp / "a.json")])
        self.assertTrue(opened and all(f.closed for f in opened))
        self.assertEqual(faulthandler.is_enabled(), was)

    def test_build_window_does_not_freeze_when_clicked(self):
        """2026-11-19：打包时窗口被鼠标点了一下，标题变成“选择 …”，PyInstaller 就一直停着（快速编辑模式）。"""
        bat = (ROOT / "build.bat").read_text(encoding="utf-8")
        self.assertIn('tools\\no_quickedit.ps1', bat)
        self.assertLess(bat.index("no_quickedit.ps1"), bat.index("[1/6]"))
        self.assertIn("按 Esc 就能继续", bat)
        ps1 = (ROOT / "tools" / "no_quickedit.ps1").read_bytes()
        self.assertTrue(all(b < 128 for b in ps1))
        self.assertIn(b"SetConsoleMode", ps1)
        self.assertIn(b"0x40", ps1)

    def test_main_dispatches_the_import_probe(self):
        from app import main as m
        with mock.patch.object(m.sys, "argv", ["run.py", "--probe-import", "onnxruntime", "x.json"]), \
                mock.patch("app.native_probe.probe_import_main", return_value=0) as pm, \
                mock.patch.object(m, "ensure_dirs") as ens:
            with self.assertRaises(SystemExit):
                m.main()
        pm.assert_called_once_with(["onnxruntime", "x.json"])
        ens.assert_not_called()

    def test_pet_page_does_not_import_onnxruntime_and_matting_falls_back(self):
        from PIL import Image
        from app.pet import matting
        sys.modules.pop("onnxruntime", None)
        with mock.patch("importlib.util.find_spec", return_value=object()):
            self.assertTrue(matting.onnx_available())
        self.assertNotIn("onnxruntime", sys.modules)                         # 打开桌宠页不会导入它
        img = Image.new("RGB", (200, 300), (255, 255, 255))
        for x in range(60, 140):
            for y in range(50, 260):
                img.putpixel((x, y), (200, 60, 120))
        with mock.patch.object(matting, "onnx_available", return_value=True), \
                mock.patch.object(matting, "model_ready", return_value=True), \
                mock.patch.object(matting, "_onnx_broken", return_value="onnxruntime 在这台电脑上一加载就崩溃"), \
                mock.patch.object(matting, "_model_cut") as model_cut:
            cut, how = matting.cutout(img)
        model_cut.assert_not_called()
        self.assertIn("去掉纯色背景", how)
        self.assertIn("一加载就崩溃", how)

    def test_build_and_installer_bring_a_new_vc_runtime(self):
        bat = (ROOT / "build.bat").read_text(encoding="utf-8")
        self.assertIn('tools\\vcredist.ps1" -Check', bat)
        self.assertIn('tools\\vcredist.ps1" -Install', bat)
        self.assertIn('-Download "installer\\redist\\vc_redist.x64.exe"', bat)
        self.assertLess(bat.index("vcredist.ps1\" -Check"), bat.index("pip install -r requirements.txt"))
        ps1 = (ROOT / "tools" / "vcredist.ps1").read_bytes()
        self.assertTrue(all(b < 128 for b in ps1))                            # PowerShell 5.1 读不带 BOM 的中文会乱
        self.assertIn(b'"14.40.0.0"', ps1)
        iss = (ROOT / "installer" / "aiWannaTikTok.iss").read_text(encoding="utf-8-sig")
        self.assertIn("function VCRedistNeeded", iss)
        self.assertIn("(14 shl 16) or 40", iss)
        self.assertIn("shellexec waituntilterminated; Check: VCRedistNeeded", iss)
        self.assertLess(iss.index("vc_redist.x64.exe\"; Parameters"), iss.index("立即启动"))


# ---------------------------------------------------------------- 接口和界面
class ApiV4Tests(unittest.TestCase):
    def setUp(self):
        from app.agent.runner import Agent
        from app.api import Api
        self.tmp = Path(tempfile.mkdtemp())
        for p in (mock.patch("app.agent.digest.REPORTS_DIR", self.tmp / "reports"),
                  mock.patch("app.paths.REPORTS_DIR", self.tmp / "reports"),
                  mock.patch("app.paths.DEBUG_DIR", self.tmp / "debug"),
                  mock.patch("app.paths.PROFILE_DIR", self.tmp / "profile")):
            p.start()
            self.addCleanup(p.stop)
        self.db = make_db()
        _fill(self.db, n_days=50, per_day=10)
        self.agent = Agent(self.db)
        self.api = Api(self.db, self.agent)

    def test_storage_report_doctor_growth_memory(self):
        api, db = self.api, self.db
        info = api.storage_info()
        self.assertTrue(info["ok"], info)
        self.assertGreater(info["data"]["db"], 0)
        r = api.storage_clean()
        self.assertTrue(r["ok"], r)
        self.assertGreater(r["data"]["usage"]["archive"]["rows"], 0)
        self.assertIn("整理了记忆仓库", [e["title"] for e in db.events_page(1, 5)["items"]])
        day = _ts(48)[:10]
        h = api.get_history(day)["data"]
        self.assertTrue(h["archived"])
        rep = api.report_get(day)["data"]
        self.assertIn("刷视频日报", rep["md"])
        self.assertTrue(api.get_history(day)["data"]["has_report"])
        st = api.get_state()["data"]
        self.assertIn("doctor", st)
        self.assertEqual(st["growth"]["level"], growth_level(db))
        self.assertTrue(api.doctor_check()["ok"])
        self.assertIn("effect", api.doctor_info()["data"])
        card = api.growth_card()["data"]
        self.assertTrue(card["url"].startswith("data:image/png;base64,"))
        self.assertTrue(Path(card["path"]).exists())
        # 淡忘的记忆
        mem = self.agent.memory
        mem.remember("dm", "小明说喜欢吃火锅", person="小明")
        db.execute("UPDATE memories SET strength=0.01, touched_at=?", (_ts(400),))
        mem.fade()
        faded = api.get_memory("faded")["data"]
        self.assertEqual(faded["total"], 1)
        self.assertTrue(api.memory_action("restore_faded", faded["faded"][0]["id"])["ok"])
        self.assertEqual(api.get_memory("faded")["data"]["total"], 0)
        # 候选的东西可以直接确认
        db.execute("INSERT INTO knowledge(topic, fact, source, created_at, updated_at, status) "
                   "VALUES('梗','yyds 是永远的神','video',?,?,'candidate')", (_ts(0), _ts(0)))
        kid = db.query("SELECT id FROM knowledge")[0]["id"]
        api.memory_action("confirm_knowledge", kid)
        self.assertEqual(db.query("SELECT status FROM knowledge")[0]["status"], "stable")

    def test_rules_keep_watch_keywords_and_quiet_hours(self):
        api, db = self.api, self.db
        api.save_rules({"watch_exclude": ["赌博", " ", "x" * 50], "watch_prefer": "猫", "owner_quiet_start": "22:00",
                        "owner_quiet_end": "坏的"})
        r = db.rules()
        self.assertEqual(r["watch_exclude"], ["赌博", "x" * 20])
        self.assertEqual(r["watch_prefer"], [])
        self.assertEqual((r["owner_quiet_start"], r["owner_quiet_end"]), ("22:00", "08:00"))

    def test_ui_has_the_new_parts(self):
        js = (ROOT / "web" / "js" / "app.js").read_text(encoding="utf-8")
        for needle in ('"faded", "淡忘的"', "storage_info", "storage_clean", "report_get", "doctor_feedback",
                       "growth_card", "watch_exclude", "owner_quiet_start", "confirm_knowledge", "restore_faded"):
            self.assertIn(needle, js)
        css = (ROOT / "web" / "css" / "styles.css").read_text(encoding="utf-8")
        for needle in (".doc-card", ".xp-bar", ".stat-grid", ".md-list"):
            self.assertIn(needle, css)


def growth_level(db) -> int:
    from app.pet import growth
    return growth.status(db)["level"]


if __name__ == "__main__":
    unittest.main()
