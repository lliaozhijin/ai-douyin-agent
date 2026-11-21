"""第三轮：先回完消息再刷视频、自主学习、了解主人口味、同样的内容不重复互动、其他功能（下载 / 导出文案）。"""
from __future__ import annotations

import io
import json
import random
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_features_v2 import be_friends, make_db  # noqa: E402  （也带上了“急停开关每个测试前后复位”）

from PIL import Image  # noqa: E402

from app.agent.brain import Brain, Draft  # noqa: E402
from app.llm.client import LLMResult  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _img(seed: int, flat: bool = False) -> bytes:
    rnd = random.Random(seed)
    im = Image.new("L", (64, 48), 128)
    if not flat:
        px = im.load()
        for x in range(64):
            for y in range(48):
                px[x, y] = rnd.randint(0, 255) if (x // 8 + y // 8 + seed) % 3 else (x * 4) % 256
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue()


# ---------------------------------------------------------------- 指纹
class FingerprintTests(unittest.TestCase):
    def test_reposts_with_other_tags_and_punctuation_are_the_same(self):
        from app.fingerprint import fingerprint, same_content
        a = fingerprint("橘猫打哈欠也太可爱了吧 #萌宠 #猫咪 @小明")
        self.assertTrue(same_content(a, fingerprint("橘猫打哈欠也太可爱了吧！！#萌宠 #抖音")))
        self.assertTrue(same_content(a, fingerprint("橘猫打哈欠也太可爱了吧哈哈")))          # 多了两个字
        self.assertFalse(same_content(a, fingerprint("今天去爬山看到超美的日出 #户外")))
        self.assertFalse(same_content(a, fingerprint("橘猫打哈欠真的太可爱了")))            # 意思像、话不一样：算同一类，不算同一个内容

    def test_short_captions_are_not_compared_by_text(self):
        from app.fingerprint import fingerprint, same_content
        self.assertIsNone(fingerprint("哈哈哈")["th"])
        self.assertFalse(same_content(fingerprint("哈哈哈"), fingerprint("哈哈哈")))

    def test_same_picture_with_a_new_title_is_the_same(self):
        from app.fingerprint import dhash, fingerprint, hamming, same_content
        pic = _img(1)
        self.assertLessEqual(hamming(dhash(pic), dhash(_img(1))), 2)
        self.assertGreater(hamming(dhash(pic), dhash(_img(2))), 10)
        self.assertIsNone(dhash(_img(3, flat=True)))                                   # 一片纯色的画面不拿来比
        self.assertTrue(same_content(fingerprint("", pic), fingerprint("#搞笑", pic)))
        self.assertFalse(same_content(fingerprint("", pic), fingerprint("", _img(2))))


# ---------------------------------------------------------------- 同样的内容不重复互动
class ContentMarksTests(unittest.TestCase):
    def setUp(self):
        from app.agent.dedupe import ContentMarks
        self.db = make_db()
        self.marks = ContentMarks(self.db)

    def test_same_content_forever_same_topic_once_a_day(self):
        v1 = {"video_id": "1", "desc": "橘猫打哈欠也太可爱了吧 #萌宠"}
        chk = self.marks.check(v1, "猫咪日常")
        self.assertEqual(chk["topic"], "猫咪")
        self.assertEqual(chk["blocked"], {})
        self.marks.record(v1, chk, {"like": True, "comment": True})
        repost = self.marks.check({"video_id": "2", "desc": "橘猫打哈欠也太可爱了吧！！"}, "搞笑")
        self.assertEqual(set(repost["blocked"]), {"like", "comment"})
        self.assertIn("同样的内容以前点赞过了", repost["blocked"]["like"])
        other_cat = self.marks.check({"video_id": "3", "desc": "我家猫学会了开门"}, "猫咪")
        self.assertEqual(set(other_cat["blocked"]), {"like", "comment"})
        self.assertIn("同一类每天只点赞一次", other_cat["blocked"]["like"])
        self.assertEqual(self.marks.check({"video_id": "4", "desc": "重庆火锅探店"}, "火锅")["blocked"], {})
        # 第二天：同一类可以再互动；同样的内容还是不行
        self.db.execute("UPDATE content_marks SET day='2000-01-01'")
        self.assertEqual(self.marks.check({"video_id": "3", "desc": "我家猫学会了开门"}, "猫咪")["blocked"], {})
        self.assertIn("like", self.marks.check({"video_id": "2", "desc": "橘猫打哈欠也太可爱了吧"}, "猫")["blocked"])

    def test_nothing_done_is_not_recorded(self):
        v = {"video_id": "1", "desc": "橘猫打哈欠也太可爱了吧"}
        self.marks.record(v, self.marks.check(v, "猫"), {"like": False})
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM content_marks")[0]["n"], 0)

    def test_upgrade_backfills_old_interactions(self):
        from app.db import Database
        import tempfile
        path = Path(tempfile.mkdtemp()) / "t.db"
        db = Database(path)
        db.add_video(video_id="9", title="橘猫打哈欠也太可爱了吧", liked=1, topic="猫")
        db.execute("DELETE FROM kv WHERE key='content_marks_v1'")
        db.close()
        db = Database(path)
        rows = db.query("SELECT * FROM content_marks")
        self.assertEqual((len(rows), rows[0]["liked"], rows[0]["topic"]), (1, 1, "猫"))

    def test_known_topics_for_the_model(self):
        for t in ("猫", "猫", "火锅", "猫咪视频"):
            self.db.add_video(video_id=t, title=t, topic=t)
        topics = self.marks.known_topics()
        self.assertEqual(topics[0], "猫")                                              # 用得多的在前
        self.assertEqual(set(topics), {"猫", "火锅", "猫咪"})                            # “猫咪视频”统一成“猫咪”


class WatchDedupeTests(unittest.TestCase):
    """刷视频时：同样的内容、今天同一类的，不再点赞 / 评论 / 收藏 / 分享。"""

    def _agent(self):
        from app.agent.runner import Agent
        db = make_db()
        r = db.rules()
        r["interest_threshold"] = 4
        db.set_json("rules", r)
        agent = Agent(db)
        agent._want_run = True
        agent._sleep = lambda s: agent._want_run
        agent.likes.rnd = mock.Mock(random=mock.Mock(return_value=0.0))
        agent.brain = mock.Mock()
        agent.brain.router.task_can_see.return_value = True
        agent.brain.safety_check.return_value = (True, "", 0.0)
        agent.brain.triage.return_value = {"interest": 9, "topic": "猫", "reason": "", "cost": 0}
        agent.listener = mock.Mock(_broken="")
        agent.listener.listen.return_value = ""
        agent.learner.context = mock.Mock(return_value="")
        return agent

    def _watch(self, agent, vid, desc, dec):
        from app.douyin.feed import SENT
        agent.brain.decide.return_value = {"interest": 9, "like": True, "comment": "好可爱", "reason": "", "cost": 0,
                                           "collect": True, **dec}
        with mock.patch("app.agent.runner.Feed") as FeedCls:
            feed = FeedCls.return_value
            feed.read_current.return_value = {"video_id": vid, "url": "u" + vid, "author": "阿橘", "desc": desc,
                                               "comments": []}
            feed.capture_frames.side_effect = lambda n, d, sleep=None: [_img(int(vid))] * n
            feed.comment.return_value = SENT
            feed.like.return_value = True
            feed.collect.return_value = True
            feed.last_mention_ok = False
            agent._watch_one(mock.Mock(), agent.db.rules())
        return feed

    def test_second_time_same_content_and_same_topic(self):
        agent = self._agent()
        first = self._watch(agent, "1", "橘猫打哈欠也太可爱了吧 #萌宠", {"learned": "猫打哈欠是放松的表现"})
        first.like.assert_called_once()
        first.comment.assert_called_once()
        self.assertEqual(agent.brain.triage.call_args.kwargs["known_topics"], [])
        notes = agent.db.query("SELECT * FROM learn_notes")
        self.assertIn("猫打哈欠是放松的表现", notes[0]["text"])                       # 学到的东西记成素材
        again = self._watch(agent, "2", "橘猫打哈欠也太可爱了吧！！", {})
        again.like.assert_not_called()
        again.comment.assert_not_called()
        args, kw = agent.brain.decide.call_args.args, agent.brain.decide.call_args.kwargs
        self.assertFalse(args[2])                                                     # 不让点赞
        self.assertFalse(args[3])                                                     # 不让评论
        self.assertFalse(kw["can_collect"])
        self.assertEqual(agent.brain.triage.call_args.kwargs["known_topics"], ["猫"])
        third = self._watch(agent, "3", "我家猫学会了开门，太聪明了", {})               # 不同的猫视频：今天也不再互动
        third.like.assert_not_called()
        agent.brain.triage.return_value = {"interest": 9, "topic": "火锅", "reason": "", "cost": 0}
        self._watch(agent, "4", "重庆火锅探店，毛肚太好吃了", {}).like.assert_called_once()


# ---------------------------------------------------------------- 主人的口味
class OwnerTasteTests(unittest.TestCase):
    def setUp(self):
        from app.agent.owner_taste import OwnerTaste
        self.db = make_db()
        self.taste = OwnerTaste(self.db, rnd=random.Random(7))

    def test_unknown_topics_are_rarely_shared_and_disliked_never(self):
        tries = [self.taste.allow("小廖", "狗", 9)[0] for _ in range(400)]
        self.assertTrue(0.1 < sum(tries) / 400 < 0.32)                                # 没把握的：偶尔试一次
        self.assertFalse(self.taste.allow("小廖", "狗", 6)[0])                         # 她自己都不太喜欢的不分享
        self.taste.learn_said("小廖", [], ["恐怖"])
        self.assertEqual(self.taste.allow("小廖", "恐怖", 10), (False, "主人不太爱看「恐怖」"))

    def test_feedback_makes_her_share_what_the_owner_likes(self):
        for _ in range(4):
            sid = self.taste.shared("小廖", "dm", {"video_id": "1", "desc": "橘猫"}, "猫", "看这只猫")
            self.taste.react(sid, "liked", "哈哈哈好可爱")
        rate = sum(self.taste.allow("小廖", "猫", 9)[0] for _ in range(300)) / 300
        self.assertGreater(rate, 0.75)
        self.assertIn("爱看：猫（很确定）", self.taste.profile("小廖"))
        for _ in range(3):
            sid = self.taste.shared("小廖", "at", {"video_id": "2", "desc": "跳舞"}, "舞蹈", "")
            self.taste.react(sid, "disliked", "别发这种")
        self.assertIn("不爱看：舞蹈", self.taste.profile("小廖"))

    def test_said_once_counts_once_and_silence_expires(self):
        self.taste.learn_said("小廖", ["猫"], [])
        self.taste.learn_said("小廖", ["猫"], [])                                     # 整理印象卡时又读到同一句：不重复加
        self.assertEqual(self.taste.overview("小廖")[0]["evidence"], 1.5)
        sid = self.taste.shared("小廖", "dm", {"video_id": "1"}, "猫", "")
        self.db.execute("UPDATE owner_shares SET created_at='2000-01-01 00:00:00' WHERE id=?", (sid,))
        self.assertEqual(self.taste.expire(), 1)
        self.assertEqual(self.db.query("SELECT status FROM owner_shares")[0]["status"], "ignored")
        self.taste.reset("小廖")
        self.assertEqual(self.taste.overview("小廖"), [])

    def test_owner_reply_after_share_teaches_her(self):
        from app.agent.runner import Agent
        db = self.db
        be_friends(db, "小廖")
        db.save_owners([{"name": "小廖", "call": "哥哥"}])
        agent = Agent(db)
        agent.brain = mock.Mock()
        agent.brain.reply_dm.return_value = Draft(text="嘿嘿就知道你喜欢", ok=True)
        agent.brain.owner_reaction.return_value = {"share": 1, "reaction": "liked", "cost": 0.0}
        agent.learner.context = mock.Mock(return_value="")
        agent.taste.shared("小廖", "dm", {"video_id": "1", "desc": "橘猫"}, "猫", "看这只猫")
        inbox = mock.Mock()
        inbox.reply_dm.return_value = True
        mid = db.add_message(kind="dm", ext_id="d1", user_name="小廖", content="哈哈哈好可爱", status="pending")
        agent._handle_incoming(inbox, mid, {"user": "小廖", "text": "哈哈哈好可爱"}, "dm", db.rules())
        self.assertEqual(db.query("SELECT status FROM owner_shares")[0]["status"], "liked")
        self.assertEqual(agent.brain.owner_reaction.call_args.args[2], "哈哈哈好可爱")

    def test_taste_pick_filters_and_describes(self):
        from app.agent.runner import Agent
        agent = Agent(self.db)
        agent.taste = self.taste
        self.taste.learn_said("小廖", ["猫"], ["恐怖"])
        for _ in range(5):
            self.taste.react(self.taste.shared("小廖", "dm", {}, "猫", ""), "liked")
        picked = agent._taste_pick([{"name": "小廖", "call": "哥哥"}], "猫", 9, "name")
        self.assertEqual(picked[0]["name"], "小廖")
        self.assertIn("爱看：猫", picked[0]["taste"])
        self.assertEqual(agent._taste_pick([{"name": "小廖"}], "恐怖", 10, "name"), [])

    def test_owner_card_update_records_taste(self):
        from app.agent.memory import Memory
        be_friends(self.db, "小廖")
        self.db.save_owners([{"name": "小廖", "call": "哥哥"}])
        mem = Memory(self.db, router=mock.Mock())
        mem.on_taste = self.taste.learn_said
        self.db.add_dm("小廖", "them", "我最爱看猫，别给我发恐怖片")
        mem.remember_dm("小廖", "我最爱看猫，别给我发恐怖片", "记住啦", owner=True)
        mem.router.call.return_value = mock.Mock(cost=0.0, text=json.dumps(
            {"card": "爱猫", "style": "", "topics": "猫", "facts": "", "likes_videos": ["猫"], "dislikes_videos": ["恐怖"]},
            ensure_ascii=False))
        mem.update_card("小廖", force=True)
        self.assertIn("likes_videos", mem.router.call.call_args.args[2])
        self.assertEqual({t["topic"]: t["said"] for t in self.taste.overview("小廖")}, {"猫": "喜欢", "恐怖": "不喜欢"})


# ---------------------------------------------------------------- 自主学习
class LearnerTests(unittest.TestCase):
    def setUp(self):
        from app.agent.learner import Learner
        self.db = make_db()
        self.router = mock.Mock()
        self.learner = Learner(self.db, self.router)

    def _r(self, obj):
        return LLMResult(text=json.dumps(obj, ensure_ascii=False), cost=0.01, output_tokens=10)

    def test_extract_then_merge_like_mem0(self):
        self.db.execute("INSERT INTO knowledge(topic, fact, source, ref, confidence, created_at, updated_at) "
                        "VALUES('梗', '“city不city”是问洋气不洋气', 'video', '', 0.6, '2026-11-01', '2026-11-01')")
        self.db.execute("INSERT INTO knowledge(topic, fact, source, ref, confidence, created_at, updated_at) "
                        "VALUES('健康', '喝冰水会伤胃是绝对的', 'dm', '', 0.6, '2026-11-01', '2026-11-01')")
        for i in range(12):
            self.learner.note("video", f"第{i}个视频：评论区都在说 city不city，意思是时髦不时髦 {i}", ref=f"v{i}")
        self.assertTrue(self.learner.due())
        self.router.call.side_effect = [
            self._r({"items": [{"topic": "梗", "fact": "“city不city”就是问时髦不时髦、洋气不洋气", "from": 1},
                               {"topic": "做饭", "fact": "炒青菜先放盐容易出水", "from": 2},
                               {"topic": "健康", "fact": "喝冰水对多数健康人没什么伤害", "from": 3},
                               {"topic": "梗", "fact": "city不city是在问洋气不洋气", "from": 4}]}),
            self._r({"ops": [{"i": 1, "op": "UPDATE", "id": 1, "fact": "“city不city”是问时髦不时髦、洋不洋气"},
                             {"i": 2, "op": "ADD"}, {"i": 3, "op": "DELETE", "id": 2}, {"i": 4, "op": "NOOP", "id": 1}]}),
        ]
        cost = self.learner.consolidate("你是雨雨")
        self.assertAlmostEqual(cost, 0.02)
        facts = {r["fact"] for r in self.db.query("SELECT fact FROM knowledge")}
        self.assertEqual(facts, {"“city不city”是问时髦不时髦、洋不洋气", "炒青菜先放盐容易出水", "喝冰水对多数健康人没什么伤害"})
        second_prompt = self.router.call.call_args_list[1].args[2]
        self.assertIn("#1「", second_prompt)                                              # 把相近的旧知识给模型对照
        self.assertFalse(self.learner.due())
        self.assertIn("不要记具体某个人的事和隐私", self.router.call.call_args_list[0].args[2])

    def test_recall_and_context(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.db.execute("INSERT INTO knowledge(topic, fact, source, confidence, created_at, updated_at) "
                        "VALUES('猫', '猫打哈欠多半是放松，不一定是困', 'video', 0.7, ?, ?)", (now, now))
        ctx = self.learner.context("我家猫老打哈欠是不是困了")
        self.assertIn("猫打哈欠多半是放松", ctx)
        self.assertEqual(self.db.query("SELECT uses FROM knowledge")[0]["uses"], 1)
        self.assertEqual(self.learner.context("今天天气怎么样"), "")

    def test_fade_and_note_dedupe(self):
        self.learner.note("dm", "私信里，对方说：在吗\n我回：在呀")
        self.learner.note("dm", "私信里，对方说：在吗\n我回：在呀")
        self.learner.note("dm", "短")
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM learn_notes")[0]["n"], 1)
        self.db.execute("INSERT INTO knowledge(topic, fact, source, confidence, created_at, updated_at) "
                        "VALUES('x', '很久没用的东西', 'video', 0.5, '2020-01-01 00:00:00', '2020-01-01 00:00:00')")
        self.assertEqual(self.learner.fade(), 1)

    def test_first_step_failure_keeps_notes(self):
        for i in range(12):
            self.learner.note("video", f"素材 {i} 有点内容")
        self.router.call.side_effect = RuntimeError("网络断了")
        self.learner.consolidate("你是雨雨")
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM learn_notes WHERE done=0")[0]["n"], 12)

    def test_replies_bring_knowledge_and_leave_notes(self):
        from app.agent.runner import Agent
        db = self.db
        agent = Agent(db)
        agent.brain = mock.Mock()
        agent.brain.reply_dm.return_value = Draft(text="猫打哈欠多半是放松哦", ok=True)
        agent.learner.context = mock.Mock(return_value="你自己平时学到的：\n- 猫打哈欠多半是放松")
        inbox = mock.Mock()
        inbox.reply_dm.return_value = True
        mid = db.add_message(kind="dm", ext_id="d1", user_name="路人", content="猫为什么老打哈欠", status="pending")
        agent._handle_incoming(inbox, mid, {"user": "路人", "text": "猫为什么老打哈欠"}, "dm", db.rules())
        self.assertIn("猫打哈欠多半是放松", agent.brain.reply_dm.call_args.kwargs["memory"])
        self.assertIn("对方说：猫为什么老打哈欠", db.query("SELECT text FROM learn_notes")[0]["text"])


# ---------------------------------------------------------------- 先回完消息：收件箱
class InboxDeepReadTests(unittest.TestCase):
    def _inbox(self):
        from app.douyin.inbox import Inbox
        ib = Inbox.__new__(Inbox)
        ib.b = mock.Mock()
        ib.b.sel = {}
        ib.b.text_of.side_effect = lambda el, n=0: getattr(el, "txt", "")
        ib.sec_uids = {}
        ib.last_notice = {}
        ib._notice_blank = False
        return ib

    def test_notice_panel_is_scrolled_until_known_ones(self):
        ib = self._inbox()
        rows = [mock.Mock(txt=f"row{i}") for i in range(30)]
        pages = [rows[:10], rows[:20], rows[10:30]]
        ib.open_notice_panel = mock.Mock(return_value=True)
        ib._notice_rows = mock.Mock(return_value=pages[0])
        ib.close_notice_panel = mock.Mock()
        ib._scroll_notice = mock.Mock(return_value=True)
        ib.b.find_all.side_effect = pages[1:] + [rows[10:30]] * 5
        ib._parse_notice = lambda row, mine=None: {"kind": "at", "ext_id": row.txt, "user": "u", "text": row.txt,
                                                   "video_url": "v", "video_gone": False}
        known = {f"row{i}" for i in range(12, 30)}
        out = ib.fetch_mentions(limit=200, is_known=lambda e: e in known, kinds=("at",), deep=True)
        self.assertEqual([it["ext_id"] for it in out], [f"row{i}" for i in range(12)])
        self.assertEqual(ib._scroll_notice.call_count, 1)                             # 连着 8 条都处理过：不再往下翻
        shallow = self._inbox()
        shallow.open_notice_panel, shallow.close_notice_panel = mock.Mock(return_value=True), mock.Mock()
        shallow._notice_rows = mock.Mock(return_value=pages[0])
        shallow._scroll_notice = mock.Mock()
        shallow._parse_notice = ib._parse_notice
        shallow.fetch_mentions(kinds=("at",))
        shallow._scroll_notice.assert_not_called()

    def test_work_comments_are_read(self):
        ib = self._inbox()
        row = mock.Mock(txt="阿花 评论了你的作品 好可爱 11-18")
        ib.b.sel = {"notice_work_keywords": ["评论了你的作品"]}
        ib.b.find.side_effect = lambda key, timeout=0, root=None: mock.Mock(
            txt={"notice_user": "阿花", "notice_content": "好可爱"}.get(key, ""))
        row.locator.return_value.first.get_attribute.return_value = ""
        with mock.patch("app.douyin.inbox.react_values", return_value=[]), \
                mock.patch("app.douyin.inbox.pick_unique", return_value="7311111111111111111"):
            ib.b.notice_comment = mock.Mock(return_value=None)
            it = ib._parse_notice(row)
        self.assertEqual((it["kind"], it["ext_id"][:3]), ("work", "wk_"))

    def test_scan_all_opens_read_but_unanswered_conversations(self):
        from app.douyin.inbox import Inbox
        ib = self._inbox()
        convs = [mock.Mock(txt="小明 在吗 10:24"), mock.Mock(txt="阿花 好的拜拜 昨天"),
                 mock.Mock(txt="老王 你好 2025-01-02"), mock.Mock(txt="小美 [图片] 11-18")]
        ib.open_dm_panel = mock.Mock(return_value=True)
        ib._back_to_list = mock.Mock(return_value=True)
        ib.b.find_all.return_value = convs
        ib._has_unread = mock.Mock(side_effect=lambda c: c is convs[3])
        ib._conv_user = lambda c: c.txt.split()[0]
        ib._conv_avatar = mock.Mock(return_value="")
        opened = []
        ib._open_conv = mock.Mock(side_effect=lambda u: opened.append(u) or False)
        done = lambda u, preview: u == "阿花"                                          # noqa: E731  记录里她已经回过最后一句
        with mock.patch("app.douyin.inbox.react_values", return_value=[]):
            Inbox.fetch_unread_dms(ib, scan_all=True, is_done=done)
        self.assertEqual(opened, ["小明", "小美"])                                     # 老王是一年前的旧会话
        opened.clear()
        with mock.patch("app.douyin.inbox.react_values", return_value=[]):
            Inbox.fetch_unread_dms(ib)
        self.assertEqual(opened, ["小美"])                                             # 平时只看有红点的

    def test_dm_items_carry_the_window_before_their_messages(self):
        from app.douyin.inbox import Inbox
        ib = self._inbox()
        ib.open_dm_panel = mock.Mock(return_value=True)
        ib._back_to_list = mock.Mock(return_value=True)
        ib.b.find_all.return_value = [mock.Mock(txt="小明 吃了吗")]
        ib._has_unread = mock.Mock(return_value=True)
        ib._conv_user = lambda c: "小明"
        ib._conv_avatar = mock.Mock(return_value="")
        ib._open_conv = mock.Mock(return_value=True)
        ib.b.known_user.return_value = None
        msgs = [{"from_me": False, "kind": "text", "text": "周末爬山吗", "url": "", "el": None},
                {"from_me": True, "kind": "text", "text": "好呀", "url": "", "el": None},
                {"from_me": False, "kind": "text", "text": "吃了吗", "url": "", "el": None},
                {"from_me": False, "kind": "text", "text": "人呢", "url": "", "el": None}]
        ib._read_chat = mock.Mock(return_value=msgs)
        with mock.patch("app.douyin.inbox.react_values", return_value=[]):
            out = Inbox.fetch_unread_dms(ib)
        self.assertEqual(out[0]["lines"], ["吃了吗", "人呢"])
        self.assertEqual(out[0]["before"], [{"role": "them", "content": "周末爬山吗"}, {"role": "me", "content": "好呀"}])
        self.assertEqual(ib._read_chat.call_args.args[0], 24)

    def test_conversation_age(self):
        from datetime import date
        from app.douyin.inbox import conv_age_days
        today = date(2026, 11, 19)
        self.assertEqual(conv_age_days("小明 在吗 10:24", today), 0)
        self.assertEqual(conv_age_days("小明 在吗 昨天", today), 1)
        self.assertEqual(conv_age_days("小明 在吗 11-02", today), 17)
        self.assertEqual(conv_age_days("小明 在吗 12-25", today), 329)                  # 去年的
        self.assertEqual(conv_age_days("小明 在吗 2025-12-30", today), 324)


# ---------------------------------------------------------------- 回复要看上下文
class ContextPromptTests(unittest.TestCase):
    def _brain(self):
        brain = Brain(make_db())
        brain.router = mock.Mock()
        brain.router.call.return_value = LLMResult(text="好呀", cost=0.0, output_tokens=1)
        brain.safety_check = mock.Mock(return_value=(True, "", 0.0))
        return brain

    def test_comment_reply_sees_earlier_lines_and_past(self):
        brain = self._brain()
        brain.reply_at({"desc": "我发的猫"}, "阿花", "第三句", [], kind="work",
                       earlier=["第一句", "第二句"], past=["11-10 在《狗狗》下，TA 说：“哈哈”，你回：“嘿嘿”"])
        prompt = brain.router.call.call_args.args[2]
        self.assertIn("在你发的作品下面评论", prompt)
        self.assertIn("1. 第一句", prompt)
        self.assertIn("不要只回最后一句", prompt)
        self.assertIn("以前在评论区找过你", prompt)
        self.assertIn("这是你自己发的作品", prompt)

    def test_dm_reply_sees_long_history(self):
        brain = self._brain()
        hist = [{"role": "them" if i % 2 else "me", "content": f"第{i}句"} for i in range(30)]
        brain.reply_dm("小明", hist, "那就这么定了")
        prompt = brain.router.call.call_args.args[2]
        self.assertIn("第6句", prompt)
        self.assertNotIn("第5句", prompt)
        self.assertIn("回复要和前面聊的连起来", prompt)

    def test_triage_reuses_her_topic_words_and_decide_asks_what_she_learned(self):
        brain = self._brain()
        brain.router.call.return_value = LLMResult(text='{"interest": 7, "topic": "猫", "reason": ""}', cost=0, output_tokens=1)
        brain.triage({"desc": "橘猫"}, [], known_topics=["猫", "火锅"])
        self.assertIn("猫、火锅", brain.router.call.call_args.args[2])
        brain.router.call.return_value = LLMResult(text='{"like": true, "learned": "橘猫大多是公猫"}', cost=0, output_tokens=1)
        dec = brain.decide({"desc": "橘猫"}, [], True, False, owners=[{"name": "小廖", "call": "哥哥", "taste": "爱看：猫"}])
        self.assertEqual(dec["learned"], "橘猫大多是公猫")
        prompt = brain.router.call.call_args.args[2]
        self.assertIn("你了解到的 TA 的口味：爱看：猫", prompt)
        self.assertIn("不是什么视频都分享给主人", prompt)


# ---------------------------------------------------------------- 规则页
class RulesPageTests(unittest.TestCase):
    def test_reply_switches_and_limits_are_gone(self):
        js = (ROOT / "web" / "js" / "app.js").read_text(encoding="utf-8")
        for gone in ('id="r-at"', 'id="r-dm"', 'id="r-th"', 'id="r-per"', 'id="r-scope"', 'id="r-dmmode"',
                     'id="r-atint"', 'id="r-atmax"', 'id="r-dmmax"', 'id="r-thmax"', 'id="r-thday"'):
            self.assertNotIn(gone, js)
        self.assertIn("有人找她的时候", js)
        from app.defaults import DEFAULT_RULES
        for k in ("auto_reply_at", "auto_reply_dm", "auto_reply_thread", "dm_scope", "per_user_daily",
                  "thread_max_replies", "at_interval", "dm_reply_mode"):
            self.assertNotIn(k, DEFAULT_RULES)
        for k in ("replies_at", "replies_dm", "thread_replies"):
            self.assertNotIn(k, DEFAULT_RULES["limits"])

    def test_dashboard_shows_unlimited_replies(self):
        from app.agent.budget import Budget
        rows = {r["key"]: r for r in Budget(make_db()).summary()}
        self.assertTrue(rows["replies_dm"]["unlimited"])
        self.assertFalse(rows["likes"]["unlimited"])

    def test_memory_api_tabs(self):
        from app.api import Api
        from app.agent.runner import Agent
        db = make_db()
        be_friends(db, "小廖")
        db.save_owners([{"name": "小廖", "call": "哥哥"}])
        agent = Agent(db)
        api = Api(db, agent)
        agent.taste.learn_said("小廖", ["猫"], [])
        data = api.get_memory("taste")["data"]
        self.assertEqual(data["owners"][0]["taste"][0]["topic"], "猫")
        self.assertIn("爱看：猫", data["owners"][0]["profile"])
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.execute("INSERT INTO knowledge(topic, fact, source, created_at, updated_at) VALUES('梗','绝绝子是夸','video',?,?)",
                   (now, now))
        k = api.get_memory("learned")["data"]["knowledge"]
        self.assertEqual(k[0]["fact"], "绝绝子是夸")
        api.memory_action("delete_knowledge", k[0]["id"])
        self.assertEqual(api.get_memory("learned")["data"]["total"], 0)
        api.memory_action("reset_taste", "小廖")
        self.assertEqual(api.get_memory("taste")["data"]["owners"][0]["taste"], [])



# ---------------------------------------------------------------- 其他功能：下载 / 导出文案
SHARE_TEXT = ("7.43 复制打开抖音，看看【小橘的作品】橘猫打哈欠 # 萌宠 https://v.douyin.com/iAbCdEf/ Kjc:/ 06/19 "
              "还有这个 https://www.youtube.com/watch?v=dQw4w9WgXcQ。")


def _detail(**kw):
    d = {"aweme_id": "7311111111111111111", "desc": "橘猫打哈欠 #萌宠", "author": {"nickname": "小橘"},
         "video": {"duration": 15300, "width": 1080, "height": 1920,
                   "bit_rate": [
                       {"gear_name": "normal_720_0", "bit_rate": 900000, "is_h265": 0,
                        "play_addr": {"url_list": ["https://v.cdn/720"], "width": 720, "height": 1280, "data_size": 1000}},
                       {"gear_name": "adapt_lowest_1080_1", "bit_rate": 1500000, "is_h265": 1,
                        "play_addr": {"url_list": ["https://v.cdn/1080h265"], "width": 1080, "height": 1920, "data_size": 3000}},
                       {"gear_name": "normal_1080_0", "bit_rate": 2100000, "is_h265": 0,
                        "play_addr": {"url_list": ["https://v.cdn/1080h264", "https://v.cdn/1080h264b"], "width": 1080,
                                      "height": 1920, "data_size": 5000}}],
                   "download_addr": {"url_list": ["https://v.cdn/watermark"]},
                   "play_addr": {"uri": "v0300fg10000abc", "url_list": ["https://aweme.snssdk.com/aweme/v1/playwm/?video_id=v0300fg10000abc"]}}}
    d.update(kw)
    return d


class LinkAndDouyinDataTests(unittest.TestCase):
    def test_urls_from_share_text(self):
        from app.tools.links import douyin_id, extract_urls, is_short, platform, safe_name
        urls = extract_urls(SHARE_TEXT)
        self.assertEqual(urls, ["https://v.douyin.com/iAbCdEf/", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"])
        self.assertEqual([platform(u) for u in urls], ["douyin", "youtube"])
        self.assertTrue(is_short(urls[0]))
        self.assertEqual(platform("https://b23.tv/xyz"), "bilibili")
        self.assertEqual(douyin_id("https://www.douyin.com/video/7311111111111111111?previous_page=app"), "7311111111111111111")
        self.assertEqual(douyin_id("https://www.douyin.com/jingxuan?modal_id=7322222222222222222"), "7322222222222222222")
        self.assertEqual(douyin_id("https://www.iesdouyin.com/share/note/7333333333333333333/"), "7333333333333333333")
        self.assertEqual(safe_name('小橘 - 橘猫:打哈欠? #萌宠 #猫'), "小橘 - 橘猫 打哈欠")

    def test_best_quality_without_watermark(self):
        from app.tools import douyin as dy
        d = _detail()
        best = dy.best_video(d)
        self.assertEqual(best["urls"], ["https://v.cdn/1080h264", "https://v.cdn/1080h264b"])     # 同分辨率码率更高的
        self.assertTrue(all("watermark" not in u and "playwm" not in u for c in dy.video_choices(d) for u in c["urls"]))
        d["video"]["bit_rate"][2]["play_addr"]["width"] = 720                                      # 1080 只剩 H.265
        d["video"]["bit_rate"][2]["play_addr"]["height"] = 1280
        self.assertEqual(dy.best_video(d)["codec"], "h265")
        d["video"]["bit_rate"].append({"gear_name": "normal_1080_0b", "bit_rate": 100, "is_h265": 0,
                                       "play_addr": {"url_list": ["https://v.cdn/1080lo"], "width": 1080, "height": 1920}})
        self.assertEqual(dy.best_video(d, prefer_h264=True)["urls"], ["https://v.cdn/1080lo"])
        self.assertEqual(dy.smallest_video(_detail())["urls"], ["https://v.cdn/720"])
        meta = dy.info(_detail())
        self.assertEqual((meta["author"], meta["duration"]), ("小橘", 15))
        self.assertEqual(dy.nowm_from_uri(_detail())[0],
                         "https://aweme.snssdk.com/aweme/v1/play/?video_id=v0300fg10000abc&ratio=1080p&line=0")

    def test_image_posts_and_share_page(self):
        from app.tools import douyin as dy
        d = _detail(images=[{"url_list": ["https://p.cdn/1.webp?x=1"]},
                            {"url_list": ["https://p.cdn/2.jpg"], "video": {"play_addr": {"url_list": ["https://v.cdn/live2"]}}}])
        pics = dy.images(d)
        self.assertEqual([p["urls"][0] for p in pics], ["https://p.cdn/1.webp?x=1", "https://p.cdn/2.jpg"])
        self.assertEqual(pics[1]["live"], ["https://v.cdn/live2"])
        html = ('<script>window._ROUTER_DATA = {"loaderData": {"video_(id)/page": {"videoInfoRes": {"item_list": ['
                + json.dumps(_detail(), ensure_ascii=False) + ']}}}}</script>')
        self.assertEqual(dy.detail_from_share_page(html)["aweme_id"], "7311111111111111111")
        self.assertIsNone(dy.detail_from_share_page("<html></html>"))

    def test_render_data_in_the_page(self):
        from urllib.parse import quote
        from app.tools import douyin as dy
        raw = quote(json.dumps({"app": {"videoDetail": {
            "awemeId": "7311111111111111111", "desc": "橘猫", "authorInfo": {"nickname": "小橘"},
            "video": {"width": 1080, "height": 1920, "duration": 15300, "playAddr": [{"src": "//v.cdn/play"}],
                      "bitRateList": [{"gearName": "normal_1080_0", "bitRate": 2000000, "isH265": 0, "width": 1080,
                                       "height": 1920, "playAddr": [{"src": "//v.cdn/1080"}]}]}}}}, ensure_ascii=False))
        d = dy.from_render_text(raw, "7311111111111111111")
        self.assertEqual(dy.best_video(d)["urls"], ["https://v.cdn/1080"])
        self.assertEqual(dy.info(d)["author"], "小橘")
        self.assertIsNone(dy.from_render_text(raw, "7399999999999999999"))

    def test_guest_browser_reads_the_page_like_an_extension(self):
        """不用她的浏览器：后台单独开一个看不见、不登录的浏览器，读网页自己请求的作品数据（和下载插件一样）。"""
        from app.tools import guest_browser as gb
        handlers = {}

        class Page:
            url = "https://www.douyin.com/video/7311111111111111111"

            def on(self, ev, fn):
                handlers[ev] = fn

            def goto(self, url, **kw):
                api = mock.Mock(url="https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=7311111111111111111",
                                status=200, headers={"content-type": "application/json"})
                api.json.return_value = {"aweme_detail": _detail()}
                handlers["response"](api)
                handlers["response"](mock.Mock(url="https://v26.douyinvod.com/abc/video/tos/x.mp4", status=206,
                                               headers={"content-type": "video/mp4"}))

            def evaluate(self, js):
                return ""

            def wait_for_timeout(self, ms):
                pass

        ctx = mock.Mock()
        ctx.new_page.return_value = Page()
        ctx.cookies.return_value = [{"name": "ttwid", "value": "x", "domain": ".douyin.com"},
                                    {"name": "o", "value": "y", "domain": ".example.com"}]
        browser = mock.Mock()
        browser.new_context.return_value = ctx
        pw = mock.Mock()
        pw.chromium.launch.return_value = browser
        sp = mock.Mock(return_value=mock.Mock(start=mock.Mock(return_value=pw)))
        res = gb.read_detail("https://www.douyin.com/video/7311111111111111111", "7311111111111111111",
                             proxy="http://127.0.0.1:7890", sync_playwright=sp)
        self.assertEqual(res["detail"]["desc"], "橘猫打哈欠 #萌宠")
        self.assertEqual(res["cookies"], {"ttwid": "x"})
        self.assertEqual(res["sniffed"], ["https://v26.douyinvod.com/abc/video/tos/x.mp4"])
        kw = pw.chromium.launch.call_args.kwargs
        self.assertTrue(kw["headless"])                                   # 看不见的
        self.assertEqual(kw["proxy"], {"server": "http://127.0.0.1:7890"})
        self.assertNotIn("user_data_dir", kw)                             # 不是她那个带登录的配置文件夹
        browser.close.assert_called_once()
        pw.stop.assert_called_once()

    def test_guest_browser_for_real_on_a_local_page(self):
        """真的在后台开一个看不见的浏览器，打开一个带 RENDER_DATA 的本地网页，读出作品数据。"""
        import tempfile
        from urllib.parse import quote
        from app.tools import guest_browser as gb
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("没有 Playwright")
        raw = quote(json.dumps({"app": {"videoDetail": {
            "awemeId": "7311111111111111111", "desc": "橘猫", "authorInfo": {"nickname": "小橘"},
            "video": {"width": 1080, "height": 1920, "playAddr": [{"src": "//v.cdn/play"}],
                      "bitRateList": [{"gearName": "normal_1080_0", "bitRate": 1, "width": 1080, "height": 1920,
                                       "playAddr": [{"src": "//v.cdn/1080"}]}]}}}}))
        f = Path(tempfile.mkdtemp()) / "video.html"
        f.write_text(f'<html><body><script id="RENDER_DATA" type="application/json">{raw}</script></body></html>',
                     encoding="utf-8")
        try:
            res = gb.read_detail(f.as_uri(), "7311111111111111111", wait_s=8)
        except gb.GuestBrowserError as e:
            self.skipTest(f"这台机器开不了浏览器：{e}")
        self.assertEqual(res["detail"]["desc"], "橘猫")
        self.assertEqual(res["detail"]["video"]["bit_rate"][0]["play_addr"]["url_list"], ["https://v.cdn/1080"])

    def test_extract_audio_with_bundled_ffmpeg(self):
        import subprocess
        import tempfile
        from app.tools import jobs as J
        from app.tools import ytdl
        exe = ytdl.ffmpeg_path()
        if not exe:
            self.skipTest("这台机器没有 ffmpeg")
        d = Path(tempfile.mkdtemp())
        src = d / "a.mp4"
        subprocess.run([exe, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                        "-f", "lavfi", "-i", "color=c=blue:s=160x120:d=2", "-c:v", "libx264", "-c:a", "aac",
                        "-shortest", str(src)], check=True)
        for fmt in ("mp3", "m4a"):
            out = J.extract_audio(src, d / f"a.{fmt}", fmt)
            self.assertGreater(out.stat().st_size, 1000)
        self.assertFalse(list(d.glob("*.part*")))

    def test_pick_video_by_quality(self):
        from app.tools import douyin as dy
        d = _detail()
        self.assertEqual(dy.pick_video(d, 720)["urls"], ["https://v.cdn/720"])
        self.assertEqual(dy.pick_video(d, 1080)["urls"][0], "https://v.cdn/1080h264")
        self.assertEqual(dy.pick_video(d, 480)["urls"], ["https://v.cdn/720"])    # 没有更小的：退回最接近的
        self.assertEqual(dy.pick_video(d, 0, smallest=True)["urls"], ["https://v.cdn/720"])
        self.assertIn("ratio=720p", dy.nowm_from_uri(d, ratio="720p")[0])


class SubtitleTests(unittest.TestCase):
    def test_vtt_rolling_captions_are_deduped(self):
        from app.tools import subtitles as st
        vtt = ("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n大家好\n\n00:00:02.000 --> 00:00:04.000\n大家好今天<c>我们</c>聊猫\n\n"
               "00:00:04.000 --> 00:00:06.000\n大家好今天我们聊猫\n\n00:00:09.000 --> 00:00:11.000\n下一段 &amp; 结尾\n")
        segs = st.parse_any(vtt, "vtt")
        self.assertEqual([s["text"] for s in segs], ["大家好", "今天我们聊猫", "下一段 & 结尾"])
        self.assertEqual(st.to_paragraphs(segs), "大家好，今天我们聊猫。\n\n下一段 & 结尾。")
        srt = st.to_srt(segs)
        self.assertIn("1\n00:00:00,000 --> 00:00:02,000\n大家好", srt)
        self.assertEqual(st.parse_any(srt)[1]["text"], "今天我们聊猫")

    def test_json3_and_bilibili(self):
        from app.tools import subtitles as st
        j3 = json.dumps({"events": [{"tStartMs": 0, "dDurationMs": 1500, "segs": [{"utf8": "Hello "}, {"utf8": "world"}]},
                                    {"tStartMs": 1600, "dDurationMs": 900, "segs": [{"utf8": "again"}]}]})
        self.assertEqual(st.to_paragraphs(st.parse_any(j3, "json3")), "Hello world again")
        bili = json.dumps({"body": [{"from": 0.5, "to": 2.0, "content": "哈喽"}, {"from": 2.0, "to": 3.0, "content": "大家好"}]})
        self.assertEqual([s["text"] for s in st.parse_any(bili, "json")], ["哈喽", "大家好"])

    def test_choose_subtitle(self):
        from app.tools.jobs import choose_subtitle
        info = {"language": "en", "subtitles": {"en": [{"ext": "vtt", "url": "u-en-vtt"}, {"ext": "json3", "url": "u-en-j3"}]},
                "automatic_captions": {"zh-Hans": [{"ext": "json3", "url": "u?tlang=zh-Hans"}]}}
        self.assertEqual(choose_subtitle(info)["url"], "u-en-j3")
        self.assertIsNone(choose_subtitle({"language": "en", "automatic_captions": info["automatic_captions"]}, "zh"))
        bili = {"subtitles": {"ai-zh": [{"ext": "json", "url": "b-ai"}], "danmaku": []}}
        self.assertEqual(choose_subtitle(bili)["lang"], "ai-zh")


class FakeResp:
    def __init__(self, body=b"x" * 50000, status=200, ctype="video/mp4", text=""):
        self.body, self.status_code, self.text = body, status, text
        self.headers = {"content-type": ctype, "content-length": str(len(body))}
        self.url = "https://www.douyin.com/video/7311111111111111111"

    def iter_content(self, n):
        for i in range(0, len(self.body), n):
            yield self.body[i:i + n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


def _fake_ytdlp(info_extra=None, sub_text=""):
    import types

    class DownloadCancelled(Exception):
        pass

    class FakeYDL:
        made = []

        def __init__(self, opts):
            self.opts = opts
            FakeYDL.made.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def _out(self):
            args = self.opts["args"]
            return args[args.index("-o") + 1] if "-o" in args else ""

        def extract_info(self, url, download=True):
            info = {"id": "dQw4w9WgXcQ", "title": "A song", "uploader": "Rick", "duration": 213, "ext": "mp4",
                    "width": 1920, "height": 1080, "language": "en", "description": "desc", **(info_extra or {})}
            if download:
                path = Path(self._out().replace("%(ext)s", "webm").replace(
                    "%(uploader,channel|未知作者).30B - %(title).70B [%(id)s]", "Rick - A song [dQw4w9WgXcQ]"))
                path.parent.mkdir(parents=True, exist_ok=True)
                for h in self.opts.get("progress_hooks") or []:
                    h({"status": "downloading", "filename": str(path), "downloaded_bytes": 50, "total_bytes": 100})
                    h({"status": "finished", "filename": str(path)})
                path.write_bytes(b"v" * 2000)
                info["requested_downloads"] = [{"filepath": str(path)}]
            return info

        def urlopen(self, url):
            return io.BytesIO(sub_text.encode("utf-8"))

        def prepare_filename(self, info):
            return ""

    return types.SimpleNamespace(
        YoutubeDL=FakeYDL, parse_options=lambda args: types.SimpleNamespace(ydl_opts={"args": list(args)}),
        utils=types.SimpleNamespace(DownloadCancelled=DownloadCancelled),
        version=types.SimpleNamespace(__version__="2025.11.12")), FakeYDL


class ToolJobsTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from app.tools import jobs as J
        self.J = J
        self.db = make_db()
        self.out = Path(tempfile.mkdtemp())
        self.db.set_json("tools", {"dir": str(self.out)})
        self.agent = mock.Mock()
        self.tools = J.ToolJobs(self.db, self.agent)
        self.tools.pool = mock.Mock(submit=lambda fn, job: fn(job))       # 测试里直接在当前线程做
        # 做完的任务不留在列表里了：测试里记下每个任务结束时的样子
        self.done = []
        finish = self.tools._finish
        self.tools._finish = lambda job: (self.done.append(job.view()), finish(job))

    def done_by(self, jid):
        return next(x for x in self.done if x["id"] == jid)

    def tearDown(self):
        from app.tools import ytdl
        ytdl._loaded = None

    def test_douyin_video_without_her(self):
        """不用她去操作：原画质优先时用后台单独的浏览器读作品数据，从头到尾不找她。"""
        guest = {"detail": _detail(), "cookies": {"ttwid": "x"}, "ua": "UA-EDGE"}
        calls = []

        def get(url, **kw):
            calls.append((url, kw.get("headers", {}).get("Referer"), kw.get("cookies")))
            return FakeResp(status=403) if url.endswith("1080h264") else FakeResp()
        with mock.patch("requests.get", side_effect=get), \
                mock.patch("app.tools.guest_browser.read_detail", return_value=guest) as rd:
            job = self.tools.start("video", SHARE_TEXT.split("还有")[0])[0]
        j = self.done[-1]
        self.assertEqual(j["status"], "done", j["error"])
        self.assertEqual(Path(j["files"][0]).name, "小橘 - 橘猫打哈欠 [7311111111111111111].mp4")
        self.assertEqual(Path(j["files"][0]).parent.name, "视频")
        self.assertEqual([c[0] for c in calls[-2:]], ["https://v.cdn/1080h264", "https://v.cdn/1080h264b"])  # 第一个地址不行换下一个
        self.assertEqual(calls[-1][1:], ("https://www.douyin.com/", {"ttwid": "x"}))
        self.assertIn("1080×1920", j["note"])
        self.agent.ask.assert_not_called()                                 # 没找她
        self.assertEqual(rd.call_args.args[0], "https://www.douyin.com/video/7311111111111111111")
        self.assertEqual(job["site_name"], "抖音")
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM tool_jobs")[0]["n"], 0)          # 不留记录

    def test_douyin_image_post_and_share_page_fallback(self):
        boom = mock.patch("app.tools.guest_browser.read_detail", side_effect=RuntimeError("没装 Edge"))
        boom.start()
        self.addCleanup(boom.stop)
        page = ('<script>window._ROUTER_DATA = {"loaderData": {"note_(id)/page": {"videoInfoRes": {"item_list": ['
                + json.dumps(_detail(images=[{"url_list": ["https://p.cdn/1.webp"]}, {"url_list": ["https://p.cdn/2.jpg"]}]),
                             ensure_ascii=False) + ']}}}}</script>')

        def get(url, **kw):
            if "iesdouyin.com/share" in url:
                return FakeResp(ctype="text/html", text=page)
            return FakeResp(ctype="image/jpeg")
        with mock.patch("requests.get", side_effect=get):
            self.tools.start("video", "https://www.douyin.com/note/7311111111111111111")
        j = self.done[-1]
        self.assertEqual(j["status"], "done", j["error"])
        folder = Path(j["files"][0])
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["01.webp", "02.jpg"])
        self.assertIn("2 张原图", j["note"])

    def test_douyin_formats_like_loader_sites(self):
        """和 en.loader.to 一样能选格式：720p 只走分享页（纯网络，不开浏览器）；只要声音时存成 MP3。"""
        page = ('<script>window._ROUTER_DATA = {"loaderData": {"video_(id)/page": {"videoInfoRes": {"item_list": ['
                + json.dumps(_detail(), ensure_ascii=False) + ']}}}}</script>')
        got = []

        def get(url, **kw):
            got.append(url)
            return FakeResp(ctype="text/html", text=page) if "iesdouyin.com/share" in url else FakeResp()
        with mock.patch("requests.get", side_effect=get), mock.patch("app.tools.guest_browser.read_detail") as rd:
            self.tools.start("video", "https://www.douyin.com/video/7311111111111111111", {"format": "720"})
        j = self.done[-1]
        self.assertEqual(j["status"], "done", j["error"])
        self.assertEqual(got[-1], "https://v.cdn/720")
        self.assertIn("720×1280", j["note"])
        rd.assert_not_called()

        def fake_extract(src, dest, fmt):
            Path(dest).write_bytes(b"mp3" * 1000)
            return dest
        with mock.patch("requests.get", side_effect=get), mock.patch("app.tools.guest_browser.read_detail") as rd, \
                mock.patch.object(self.J, "extract_audio", side_effect=fake_extract) as ex:
            jid = self.tools.start("video", "https://www.douyin.com/video/7311111111111111111", {"format": "mp3"})[0]["id"]
        j = self.done_by(jid)
        self.assertEqual(j["status"], "done", j["error"])
        self.assertTrue(j["files"][0].endswith("[7311111111111111111].mp3"))
        self.assertEqual(ex.call_args.args[2], "mp3")
        rd.assert_not_called()

    def test_douyin_sniffed_video_when_page_data_is_missing(self):
        guest = {"detail": None, "cookies": {}, "ua": "UA", "aweme_id": "7311111111111111111",
                 "sniffed": ["https://v26.douyinvod.com/x/video/tos/a.mp4"]}
        with mock.patch("app.tools.guest_browser.read_detail", return_value=guest), \
                mock.patch("requests.get", return_value=FakeResp()) as get:
            self.tools.start("video", "https://www.douyin.com/video/7311111111111111111")
        j = self.done[-1]
        self.assertEqual(j["status"], "done", j["error"])
        self.assertEqual(get.call_args.args[0], "https://v26.douyinvod.com/x/video/tos/a.mp4")

    def test_ytdlp_format_args(self):
        J = self.J
        self.assertEqual(J.ytdlp_format_args("best")[:2], ["-f", "bv*+ba/b"])
        self.assertIn("bv*[height<=720]+ba/b[height<=720]/bv*+ba/b", J.ytdlp_format_args("720"))
        mp3 = J.ytdlp_format_args("mp3")
        self.assertEqual(mp3[mp3.index("--audio-format") + 1], "mp3")
        self.assertIn("-x", J.ytdlp_format_args("m4a"))
        self.assertEqual(self.tools.save_settings({"format": "8k"})["format"], "best")
        self.assertEqual(self.tools.save_settings({"format": "mp3", "douyin_best": 0})["douyin_best"], False)

    def test_finished_jobs_leave_no_history_only_a_one_time_notice(self):
        """做完的任务不留记录：列表里只剩正在做的；成功 / 失败各留一条一次性的通知（失败的界面弹窗说原因）。"""
        from app.tools import ytdl
        sub = json.dumps({"events": [{"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "Never gonna"}]}]})
        fake, _ = _fake_ytdlp({"subtitles": {"en": [{"ext": "json3", "url": "sub-url"}]}}, sub)
        ytdl._loaded = fake
        self.tools.start("text", "https://www.youtube.com/watch?v=dQw4w9WgXcQ", {"srt": False})
        with mock.patch("app.tools.guest_browser.read_detail", side_effect=RuntimeError("打不开")), \
                mock.patch("requests.get", return_value=FakeResp(ctype="text/html", text="<html></html>")):
            self.tools.start("video", "https://www.douyin.com/video/7311111111111111111")
        self.assertEqual(self.tools.list(), [])                                   # 进度条都消失了
        evs = self.tools.pop_events()
        self.assertEqual([e["status"] for e in evs], ["done", "failed"])
        self.assertTrue(evs[0]["files"][0].endswith(".txt"))
        self.assertIn("没读到这个抖音作品的数据", evs[1]["error"])
        self.assertEqual(self.tools.pop_events(), [])                             # 只通知一次
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM tool_jobs")[0]["n"], 0)   # 不存历史记录
        from app.tools.jobs import Job
        job = Job("video", "https://x.com/v", {})
        self.tools.jobs[job.id] = job
        self.assertEqual([j["id"] for j in self.tools.list()], [job.id])         # 排队中的照样显示
        self.assertTrue(self.tools.cancel(job.id))
        self.assertEqual((self.tools.list(), self.tools.pop_events()), ([], []))  # 取消的：不提示

    def test_other_sites_with_ytdlp(self):
        from app.tools import ytdl
        fake, FakeYDL = _fake_ytdlp()
        ytdl._loaded = fake
        with mock.patch.object(ytdl, "js_runtime", return_value=("deno", "/x/deno")), \
                mock.patch.object(ytdl, "has_ejs", return_value=True), mock.patch.object(ytdl, "ffmpeg_path", return_value="/x/ffmpeg"):
            self.tools.start("video", "https://www.youtube.com/watch?v=dQw4w9WgXcQ", {"proxy": "http://127.0.0.1:7890"})
        j = self.done[-1]
        self.assertEqual(j["status"], "done", j["error"])
        self.assertTrue(j["files"][0].endswith("Rick - A song [dQw4w9WgXcQ].webm"))
        args = FakeYDL.made[0].opts["args"]
        for a in ("--js-runtimes", "deno:/x/deno", "--ffmpeg-location", "/x/ffmpeg", "--proxy", "http://127.0.0.1:7890",
                  "bv*+ba/b", "--merge-output-format", "mp4/mkv", "--no-playlist"):
            self.assertIn(a, args)
        self.assertNotIn("--remote-components", args)
        self.assertEqual(j["note"], "1920×1080")

    def test_transcript_from_platform_subtitles(self):
        from app.tools import ytdl
        sub = json.dumps({"events": [{"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "Never gonna"}]},
                                     {"tStartMs": 1000, "dDurationMs": 1000, "segs": [{"utf8": "give you up"}]}]})
        fake, _ = _fake_ytdlp({"subtitles": {"en": [{"ext": "json3", "url": "sub-url"}]}}, sub)
        ytdl._loaded = fake
        self.tools.start("text", "https://www.youtube.com/watch?v=dQw4w9WgXcQ", {"srt": True})
        j = self.done[-1]
        self.assertEqual(j["status"], "done", j["error"])
        txt = Path(j["files"][0]).read_text(encoding="utf-8-sig")
        self.assertIn("标题：A song", txt)
        self.assertIn("文字来源：平台字幕（en）", txt)
        self.assertIn("Never gonna give you up", txt)
        self.assertTrue(j["files"][1].endswith(".srt"))
        self.assertEqual(Path(j["files"][0]).parent.name, "文案")

    def test_douyin_transcript_falls_back_to_local_asr(self):
        page = ('<script>window._ROUTER_DATA = {"loaderData": {"video_(id)/page": {"videoInfoRes": {"item_list": ['
                + json.dumps(_detail(), ensure_ascii=False) + ']}}}}</script>')
        segs = [{"start": 0.0, "end": 2.0, "text": "大家好"}, {"start": 2.1, "end": 4.0, "text": "今天聊聊橘猫"}]

        def get(url, **kw):
            return FakeResp(ctype="text/html", text=page) if "iesdouyin.com/share" in url else FakeResp()
        with mock.patch("requests.get", side_effect=get) as get, \
                mock.patch("app.tools.guest_browser.read_detail") as rd, \
                mock.patch("app.agent.listen.has_audio", return_value=True), \
                mock.patch("app.tools.asr.transcribe", return_value=(segs, "zh")) as asr:
            self.tools.start("text", "https://www.douyin.com/video/7311111111111111111", {"asr_model": "small", "srt": False})
        j = self.done[-1]
        self.assertEqual(j["status"], "done", j["error"])
        self.assertEqual(get.call_args.args[0], "https://v.cdn/720")                     # 转文字只下最小的那档
        rd.assert_not_called()                                                            # 分享页就够了，不开浏览器
        self.agent.ask.assert_not_called()
        self.assertEqual(asr.call_args.args[1], "small")
        txt = Path(j["files"][0]).read_text(encoding="utf-8-sig")
        self.assertIn("大家好，今天聊聊橘猫。", txt)
        self.assertIn("———— 视频简介 / 文案 ————\n橘猫打哈欠 #萌宠", txt)
        self.assertEqual(len(j["files"]), 1)

    def test_other_site_transcript_retries_when_the_audio_file_has_no_sound(self):
        from app.tools import ytdl
        fake, FakeYDL = _fake_ytdlp()
        FakeYDL.made.clear()
        ytdl._loaded = fake
        segs = [{"start": 0.0, "end": 1.0, "text": "hello"}]
        answers = iter([False, True])
        with mock.patch("app.agent.listen.has_audio", side_effect=lambda p: next(answers, True)), \
                mock.patch("app.tools.asr.transcribe", return_value=(segs, "en")) as asr:
            jid = self.tools.start("text", "https://www.youtube.com/watch?v=dQw4w9WgXcQ", {"srt": False})[0]["id"]
        j = self.done_by(jid)
        self.assertEqual(j["status"], "done", j["error"])
        fmts = [m.opts["args"][m.opts["args"].index("-f") + 1] for m in FakeYDL.made if "-f" in m.opts["args"]]
        self.assertEqual(fmts, ["ba/b", "b"])                       # “最好的声音”没声音 → 换成整个视频
        self.assertTrue(Path(asr.call_args.args[0]).name.startswith("b0."))

    def test_douyin_transcript_skips_files_without_sound(self):
        """2026-11-19：导出文案提示“这个视频里没有声音”，其实视频有声音——挑到的那一档只有画面。
        现在下载后先看有没有声音，没有就换下一个地址（单独的声音、播放地址、带水印的地址、作品的原声……）。"""
        d = _detail()
        d["video"]["bit_rate_audio"] = [{"audio_meta": {"url_list": {"main_url": "https://a.cdn/audio.m4a",
                                                                     "backup_url": "https://a.cdn/audio_b.m4a"}}}]
        guest = {"detail": d, "cookies": {}, "ua": "UA"}
        page = '<html>no data</html>'
        fetched = []

        def get(url, **kw):
            fetched.append(url)
            return FakeResp(ctype="text/html", text=page) if "iesdouyin.com/share" in url else FakeResp()
        with_sound = {"https://a.cdn/audio.m4a"}
        listened = []

        def has_audio(path):
            url = fetched[-1]
            listened.append(url)
            return url in with_sound
        segs = [{"start": 0.0, "end": 2.0, "text": "大家好"}]
        with mock.patch("requests.get", side_effect=get), \
                mock.patch("app.tools.guest_browser.read_detail", return_value=guest), \
                mock.patch("app.agent.listen.has_audio", side_effect=has_audio), \
                mock.patch("app.tools.asr.transcribe", return_value=(segs, "zh")) as asr:
            jid = self.tools.start("text", "https://www.douyin.com/video/7311111111111111111", {"srt": False})[0]["id"]
        j = self.done_by(jid)
        self.assertEqual(j["status"], "done", j["error"])
        self.assertEqual(listened[:2], ["https://v.cdn/720", "https://v.cdn/1080h265"])      # 先试视频档位：都只有画面
        self.assertEqual(listened[-1], "https://a.cdn/audio.m4a")                           # 换成单独的声音
        self.assertTrue(asr.call_args.args[0].endswith(".media"))

    def test_audio_candidates_include_original_sound_and_watermarked(self):
        from app.tools import douyin as dy
        d = _detail(music={"title": "@小橘创作的原声", "author": "小橘", "play_url": {"url_list": ["https://m.cdn/orig.mp3"]}})
        labels = [label for label, _ in dy.audio_candidates(d, share_page=True)]
        self.assertTrue(labels[0].startswith("视频档位"))
        self.assertIn("download_addr（可能带水印，只用来听）", labels)
        self.assertEqual(labels[-1], "作品的原声")
        urls = [u for _, us in dy.audio_candidates(d) for u in us]
        self.assertEqual(len(urls), len(set(urls)))                                          # 不重复试

    def test_has_audio_with_real_ffmpeg(self):
        import subprocess
        import tempfile
        from app.agent.listen import has_audio
        from app.tools import ytdl
        exe = ytdl.ffmpeg_path()
        if not exe:
            self.skipTest("这台机器没有 ffmpeg")
        dd = Path(tempfile.mkdtemp())
        silent_video = dd / "v.mp4"
        subprocess.run([exe, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64:d=1",
                        "-c:v", "libx264", str(silent_video)], check=True)
        both = dd / "a.mp4"
        subprocess.run([exe, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=1", "-f", "lavfi",
                        "-i", "color=c=red:s=64x64:d=1", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(both)], check=True)
        self.assertFalse(has_audio(str(silent_video)))
        self.assertTrue(has_audio(str(both)))

    def test_polish_with_the_model_and_failures_are_friendly(self):
        agent = mock.Mock()
        agent.brain.router.call.return_value = LLMResult(text="大家好。今天聊聊橘猫。", cost=0.0)
        self.assertEqual(self.J.polish("大家好今天聊聊橘猫", agent), "大家好。今天聊聊橘猫。")
        self.assertIn("cookies", self.J.friendly_error(RuntimeError("ERROR: Sign in to confirm you’re not a bot")))
        self.assertIn("deno", self.J.friendly_error(RuntimeError("No supported JavaScript runtime could be found")))
        with self.assertRaises(ValueError):
            self.tools.start("video", "没有地址")

    def test_old_history_is_forgotten(self):
        self.db.execute("INSERT INTO tool_jobs(id, kind, url, status, created_at) VALUES('old','video','u','done','2026-01-01')")
        again = self.J.ToolJobs(self.db, None)
        self.assertEqual(again.list(), [])
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM tool_jobs")[0]["n"], 0)

    def test_settings(self):
        s = self.tools.save_settings({"lang": "xx", "asr_model": "huge", "polish": 1, "proxy": "http://p"})
        self.assertEqual((s["lang"], s["asr_model"], s["polish"], s["proxy"]), ("auto", "base", True, "http://p"))
        self.assertEqual(s["dir_effective"], str(self.out))


class YtdlpSetupTests(unittest.TestCase):
    def test_js_runtime_detection_order(self):
        from app.tools import ytdl
        with mock.patch.object(ytdl, "DENO_DIR", Path("/nonexistent")), \
                mock.patch("shutil.which", side_effect=lambda n: {"node": "/usr/bin/node"}.get(n)), \
                mock.patch.object(ytdl, "_node_ok", return_value=True):
            self.assertEqual(ytdl.js_runtime(), ("node", "/usr/bin/node"))
        with mock.patch.object(ytdl, "DENO_DIR", Path("/nonexistent")), mock.patch("shutil.which", return_value=None):
            self.assertEqual(ytdl.js_runtime(), ("", ""))
            with mock.patch.object(ytdl, "has_ejs", return_value=False), mock.patch.object(ytdl, "ffmpeg_path", return_value=""):
                args = ytdl.cli_args()
        self.assertIn("ejs:github", args)
        self.assertNotIn("--js-runtimes", args)

    def test_update_downloads_the_official_zipapp(self):
        import tempfile
        import zipfile
        from app.tools import ytdl
        d = Path(tempfile.mkdtemp())

        def fake_fetch(url, dest, progress=None, proxy="", cancelled=None):
            with zipfile.ZipFile(dest, "w") as z:
                z.writestr("yt_dlp/version.py", "__version__ = '2099.01.01'\n")
        with mock.patch.object(ytdl, "TOOLS_DIR", d), mock.patch.object(ytdl, "PYZ", d / "yt-dlp.pyz"), \
                mock.patch.object(ytdl, "_fetch", side_effect=fake_fetch):
            self.assertEqual(ytdl.update_ytdlp(), "2099.01.01")
            self.assertEqual(ytdl._pyz_version(d / "yt-dlp.pyz"), "2099.01.01")
        self.assertGreater(ytdl._vkey("2099.01.01"), ytdl._vkey("2025.11.12"))
        self.assertIn("windows-msvc", ytdl.deno_asset()) if sys.platform == "win32" else self.assertIn("deno-", ytdl.deno_asset())


class ToolsWiringTests(unittest.TestCase):
    def test_tools_never_ask_her_browser(self):
        from app.agent.runner import Agent
        self.assertNotIn("tool_douyin", Agent.URGENT)
        self.assertFalse(hasattr(Agent, "_cmd_tool_douyin"))
        from app.tools import douyin as dy
        self.assertFalse(hasattr(dy, "read_in_browser"))

    def test_browser_keeps_detail_data(self):
        from app.douyin.browser import DouyinBrowser
        b = DouyinBrowser.__new__(DouyinBrowser)
        b.aweme_details, b.media, b.users_seen = {}, {}, {}
        resp = mock.Mock(url="https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=7311111111111111111",
                         headers={"content-type": "application/json"})
        resp.json.return_value = {"aweme_detail": _detail()}
        b._on_response(resp)
        self.assertIn("7311111111111111111", b.aweme_details)

    def test_api_only_opens_files_it_made(self):
        import tempfile
        from app.api import Api
        db = make_db()
        out = Path(tempfile.mkdtemp())
        db.set_json("tools", {"dir": str(out)})
        api = Api(db, mock.Mock())
        f = out / "视频" / "a.mp4"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"x")
        api._tool_jobs()
        with mock.patch("app.api._open_folder") as of, mock.patch("app.api.subprocess.Popen"):
            self.assertTrue(api.tools_open(str(f), False)["ok"])
            self.assertFalse(api.tools_open("C:/Windows/System32/cmd.exe")["ok"])
            api.tools_open("")
        self.assertEqual(of.call_count, 2)

    def test_tools_page_is_in_the_menu(self):
        js = (ROOT / "web" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn('["tools", "其他功能"]', js)
        for act in ('"tool-start"', '"tool-cancel"', '"tool-open"', '"tool-setup"', "function renderTools()"):
            self.assertIn(act, js)
        req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("yt-dlp[default]", req)
        self.assertIn('"yt_dlp_ejs"', (ROOT / "aiWannaTikTok.spec").read_text(encoding="utf-8"))


def time_sleep(s):
    import time
    time.sleep(s)


if __name__ == "__main__":
    unittest.main()
