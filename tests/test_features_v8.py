"""第八轮：
① 她的口味不用人设置，跟着她每天刷到的、说的、评论的、和别人聊的自己慢慢变；
② 私信和评论区的上下文是通的：回私信时记得评论区刚聊过的，在评论区回时记得私信里聊的（同一个人名字写法不一样也认得）。"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_features_v2 import make_db  # noqa: E402
from tests.test_features_v6 import OWNER_UID, _agent, _inbox, _plans  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _router(payload: dict, cost: float = 0.002):
    r = mock.Mock()
    r.call.return_value = mock.Mock(text=json.dumps(payload, ensure_ascii=False), cost=cost, tokens=50)
    return r


class MyTasteTests(unittest.TestCase):
    def test_videos_push_her_taste_up_and_down(self):
        from app.agent.my_taste import MyTaste
        t = MyTaste(make_db())
        t.on_video("猫", 9, 6, liked=True, commented=True)
        t.on_video("猫咪日常", 8, 6, collected=True)               # 写法不一样也是同一类（“猫咪日常”→“猫咪”）
        t.on_video("美妆", 3, 6)
        rows = {r["topic"]: r for r in t.rows()}
        self.assertGreater(rows["猫"]["score"], 0.9)
        self.assertEqual((rows["猫"]["seen"], rows["猫"]["loved"], rows["猫"]["acted"]), (1, 1, 1))
        self.assertIn("猫咪", rows)
        self.assertLess(rows["美妆"]["score"], 0)
        self.assertEqual(rows["美妆"]["acted"], 0)
        t.on_video("", 9, 6)                                         # 没有分类：不记
        t.on_video("猫", "x", 6)                                     # 分数读不出来：不记
        self.assertEqual(len(t.rows()), 3)

    def test_starts_from_recent_history_once(self):
        from app.agent.my_taste import MyTaste
        db = make_db()
        db.add_video(video_id="a", url="", author="", title="", interest=9, decision="like", liked=1, topic="猫")
        db.add_video(video_id="b", url="", author="", title="", interest=2, decision="skip", topic="美妆")
        db.add_video(video_id="c", url="", author="", title="", interest=8, decision="watch", topic="")
        t = MyTaste(db)
        self.assertEqual(t.seed_from_history(), 2)
        self.assertEqual(t.seed_from_history(), 0)                  # 只补算一次
        r = {x["topic"]: x for x in t.rows()}
        self.assertGreater(r["猫"]["score"], 0)
        self.assertLess(r["美妆"]["score"], 0)

    def test_what_people_talk_about_counts_too(self):
        from app.agent.my_taste import MyTaste
        db = make_db()
        t = MyTaste(db)
        t.on_video("露营", 6, 6)
        t.on_video("猫", 6, 6)
        hit = t.on_talk("周末去露营吗，我最近天天刷露营视频", "好呀！我也想看看")
        self.assertEqual(hit, ["露营"])
        r = {x["topic"]: x for x in t.rows()}
        self.assertEqual(r["露营"]["talked"], 1)
        self.assertAlmostEqual(r["露营"]["score"], 0.15, places=2)
        t.on_talk("你家有猫吗", "有呀，我超爱猫")                      # 她自己也说到了：加得多一点
        self.assertAlmostEqual({x["topic"]: x for x in t.rows()}["猫"]["score"], 0.4, places=2)
        self.assertEqual(t.on_talk("今天好累", "早点休息"), [])
        self.assertEqual(t.on_talk("", ""), [])

    def test_taste_fades_a_little_every_day(self):
        from app.agent.my_taste import MyTaste
        db = make_db()
        t = MyTaste(db)
        t.on_video("猫", 10, 6, liked=True)
        before = t.rows()[0]["score"]
        now = datetime(2026, 11, 20, 10, 0)
        t.decay(now)
        t.decay(now)                                                # 同一天只淡一次
        self.assertAlmostEqual(t.rows()[0]["score"], before * 0.95, places=3)
        t.decay(now + timedelta(days=3))                            # 隔了 3 天：一次补上 3 天
        self.assertAlmostEqual(t.rows()[0]["score"], before * 0.95 ** 4, places=3)

    def test_she_thinks_about_her_taste_and_it_shapes_watching(self):
        from app.agent.memory import Memory
        from app.agent.my_taste import MyTaste
        db = make_db()
        mem = Memory(db)
        t = MyTaste(db, _router({"line": "最近迷上了露营，猫还是一直爱看，美妆有点看腻了",
                                 "loves": ["猫"], "rising": ["露营"], "tired": ["美妆"], "curious": ["陶艺"]}), memory=mem)
        self.assertFalse(t.due())
        self.assertEqual(t.refresh("系统"), 0.0)                   # 没什么新经历：不想
        for _ in range(4):
            t.on_video("猫", 9, 6, liked=True)
            t.on_video("美妆", 4, 6)
        self.assertTrue(t.due())
        cost = t.refresh("系统")
        self.assertGreater(cost, 0)
        prompt = t.router.call.call_args.args[2]
        for w in ("你一开始（人设里）感兴趣的", "猫：分数", "分数是负的", "美妆"):
            self.assertIn(w, prompt)
        p = t.profile()
        self.assertEqual((p["loves"], p["rising"], p["tired"], p["curious"]), (["猫"], ["露营"], ["美妆"], ["陶艺"]))
        self.assertFalse(t.due())                                   # 刚想过
        line = t.line()
        self.assertIn("最近迷上了露营", line)
        self.assertIn("有点看腻的：美妆", line)
        self.assertEqual(t.nudge("美妆"), (-1, "最近有点看腻这一类了"))
        self.assertEqual(t.nudge("陶艺手作")[0], 1)                 # 听人说起想看看的
        self.assertEqual(t.nudge("猫"), (0, ""))
        ev = db.query("SELECT * FROM events WHERE kind='taste'")[0]
        self.assertIn("最近迷上了露营", ev["detail"])
        self.assertTrue(db.query("SELECT 1 FROM memories WHERE kind='self' AND summary LIKE '%迷上了露营%'"))
        # 她的身份里带着最近的口味：刷视频、回私信、回评论都是同一个她
        mem.taste_line = t.line
        self.assertIn("最近迷上了露营", mem.identity_context())
        listing = t.listing()
        self.assertEqual(listing["profile"]["rising"], ["露营"])
        self.assertTrue(any(r["topic"] == "猫" for r in listing["topics"]))
        # 下一次想的时候能看出变化
        t.on_video("猫", 10, 6, liked=True, collected=True)
        self.assertIn("你上次想的自己的口味", t._material())

    def test_triage_prompt_carries_her_taste(self):
        from app.agent.brain import Brain
        b = Brain(make_db())
        b.router = _router({"interest": 7, "topic": "猫", "reason": "可爱"})
        b.triage({"desc": "小猫", "author": "a", "comments": []}, [], taste="你最近的口味：最近迷上的：露营。")
        prompt = b.router.call.call_args.args[2]
        self.assertIn("最近迷上的：露营", prompt)
        self.assertIn("照你现在的口味打分", prompt)
        b.triage({"desc": "小猫", "author": "a", "comments": []}, [])
        self.assertNotIn("照你现在的口味打分", b.router.call.call_args.args[2])

    def test_persona_interests_are_only_the_starting_point(self):
        from app.agent.persona import system_prompt
        s = system_prompt({"name": "雨雨", "interests": ["萌宠", "甜品"]})
        self.assertIn("你一开始就感兴趣的内容：萌宠、甜品", s)
        self.assertIn("慢慢变", s)


class WatchTasteTests(unittest.TestCase):
    def _agent(self):
        from app.agent.runner import Agent
        db = make_db()
        agent = Agent(db)
        agent._want_run = True
        agent._sleep = lambda s: agent._want_run
        agent._watch_sleep = lambda s: agent._want_run
        agent.brain = mock.Mock()
        agent.brain.router.task_can_see.return_value = False
        agent.brain.safety_check.return_value = (True, "", 0.0)
        agent.brain.decide.return_value = {"interest": 9, "like": True, "comment": "", "reason": "", "cost": 0.0}
        agent._listen = mock.Mock(return_value="")
        agent.mood = mock.Mock()                                       # 心情固定：不让“没耐心划走”随机发生
        agent.mood.bias.return_value = {"dwell": 1.0, "threshold": 0}
        agent.mood.current.return_value = {"key": "calm", "label": "平静"}
        agent.likes.rnd = mock.Mock(random=mock.Mock(return_value=0.99), uniform=lambda a, b: a)
        return agent

    def _watch(self, agent, interest=9, vid="v1"):
        agent.brain.triage.return_value = {"interest": interest, "topic": "猫", "reason": "猫", "cost": 0.0}
        with mock.patch("app.agent.runner.Feed") as FeedCls:
            feed = FeedCls.return_value
            feed.read_current.return_value = {"video_id": vid, "url": "u", "author": "a", "desc": "三只小猫", "comments": []}
            feed.capture_frames.return_value = []
            feed.like.return_value = True
            agent._watch_one(mock.Mock(), agent.db.rules())

    def test_watching_moves_her_taste(self):
        agent = self._agent()
        self._watch(agent)
        self.assertIn("taste", agent.brain.triage.call_args.kwargs)
        r = {x["topic"]: x for x in agent.my_taste.rows()}["猫"]
        self.assertEqual((r["seen"], r["loved"], r["acted"]), (1, 1, 1))
        self._watch(agent, interest=2, vid="v2")                       # 不感兴趣划走的也算
        self.assertEqual({x["topic"]: x for x in agent.my_taste.rows()}["猫"]["seen"], 2)

    def test_tired_topic_gets_one_point_less(self):
        agent = self._agent()
        agent.db.set_json("my_taste_profile", {"line": "猫看得有点多了", "tired": ["猫"], "at": "2026-11-20 10:00:00"})
        self._watch(agent, interest=9)
        self.assertEqual(agent.db.query("SELECT interest FROM videos WHERE video_id='v1'")[0]["interest"], 8)
        self._watch(agent, interest=6, vid="v2")                       # 本来刚好想看下去：看腻了就划走
        row = agent.db.query("SELECT interest, decision, reason FROM videos WHERE video_id='v2'")[0]
        self.assertEqual((row["interest"], row["decision"]), (5, "skip"))
        self.assertIn("最近有点看腻这一类了", row["reason"])


class SharedContextTests(unittest.TestCase):
    def test_same_person_with_different_names(self):
        db = make_db()
        db.add_dm("吃白饭的鲸", "them", "在吗")
        db.link_person("小明", "MS4wLjABAAAAming")
        db.link_person("小明同学", "MS4wLjABAAAAming")
        db.save_friend("MS4wLjABAAAAhong", {"nickname": "小红🌸", "mutual": True})
        db.save_dm_user("红红", {"url": "https://www.douyin.com/user/MS4wLjABAAAAhong"})
        self.assertEqual(db.person_aliases("吃白饭的鲸🐳"), ["吃白饭的鲸🐳", "吃白饭的鲸"])
        self.assertEqual(db.person_aliases("小明"), ["小明", "小明同学"])
        self.assertEqual(set(db.person_aliases("小红🌸")), {"小红🌸", "红红"})
        self.assertEqual(db.person_aliases("路人"), ["路人"])
        self.assertEqual(db.person_aliases(""), [])
        db.link_person("x", "name:x")                              # 没读到主页 id 的不记
        db.link_person("", "MS4wLjABAAAAming")
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM person_ids")[0]["n"], 2)

    def test_dm_reply_remembers_the_comments_and_vice_versa(self):
        from app.agent.memory import Memory
        db = make_db()
        m = Memory(db)
        db.add_dm("吃白饭的鲸", "them", "晚上一起看那个灯的视频呀")
        db.add_dm("吃白饭的鲸", "me", "好呀好呀")
        db.add_message(kind="reply", ext_id="r1", user_name="吃白饭的鲸🐳", content="手动开关至少不会自己亮",
                       status="replied", reply="哈哈半斤八两", video_title="酒馆模型")
        db.add_message(kind="reply", ext_id="r2", user_name="吃白饭的鲸🐳", content="没回的不算", status="failed")
        db.add_comment_action("c1", "v1", "吃白饭的鲸", "这灯也太好看了", reply="我也想住进去", video={"desc": "月亮旅店"})
        dm = m.dm_context("吃白饭的鲸", "你说的半斤八两是啥意思")
        self.assertIn("你们最近在评论区的来往", dm)
        self.assertIn("“手动开关至少不会自己亮”，你回：“哈哈半斤八两”", dm)
        self.assertIn("我也想住进去", dm)                         # 她刷视频时回过 TA 的评论也算
        self.assertNotIn("没回的不算", dm)
        cm = m.comment_context("吃白饭的鲸🐳", "哈哈")
        self.assertIn("你们最近的私信", cm)
        self.assertIn("TA：晚上一起看那个灯的视频呀", cm)
        self.assertIn("不要把私信里的私事和细节当众说出来", cm)
        # 很久以前的不算“最近”（那些交给按相关度想起的记忆）
        db.execute("UPDATE dm_history SET ts='2020-01-01 00:00:00'")
        self.assertNotIn("你们最近的私信", m.comment_context("吃白饭的鲸🐳", "哈哈"))
        self.assertEqual(m.other_side("", "dm"), "")

    def test_memories_recalled_across_names(self):
        from app.agent.memory import Memory
        db = make_db()
        m = Memory(db)
        m.remember_dm("吃白饭的鲸", "我下周要去大理", "哇好羡慕！")
        db.execute("UPDATE dm_history SET ts='2020-01-01 00:00:00'")
        ctx = m.comment_context("吃白饭的鲸🐳", "大理好玩吗")
        self.assertIn("我下周要去大理", ctx)                      # 评论区里的名字带 emoji，私信里的不带：还是同一个人
        self.assertEqual(len(m.recall("", person=["吃白饭的鲸🐳", "吃白饭的鲸"], limit=5)), 1)
        self.assertEqual(m.recall("", person="吃白饭的鲸🐳", limit=5), [])

    def test_incoming_messages_link_names_to_profile_ids(self):
        a = _agent()
        a.db.block_user("小明同学")
        a._handle_incoming(_inbox(), 1, {"user": "小明同学", "text": "hi", "peer_uid": "MS4wLjABAAAAming"}, "dm", a.db.rules())
        a.db.block_user("明明")
        a._handle_incoming(_inbox(), 2, {"user": "明明", "text": "@她", "kind": "at",
                                         "user_url": "https://www.douyin.com/user/MS4wLjABAAAAming"}, "at", a.db.rules())
        self.assertEqual(set(a.db.person_aliases("明明")), {"明明", "小明同学", "小明同学🌟"})

    def test_owner_commands_see_both_sides(self):
        a = _agent()
        a.db.add_dm("小廖", "them", "待会儿我在评论区 @ 你，你帮我叫一下小红")
        a.db.add_dm("小廖", "me", "好嘞")
        a.brain.command_step.side_effect = _plans({"tool": "chat", "args": {}})
        it = {"kind": "at", "user": "小廖", "text": "@雨雨 就是这个", "video_url": "https://www.douyin.com/video/7677931347901428325",
              "user_url": f"https://www.douyin.com/user/{OWNER_UID}", "ext_id": "at_y", "cid": "", "mine": ""}
        self.assertIsNone(a.commands.handle_comment(_inbox(), 1, it, a.db.owner("小廖"), {"desc": "猫"}, a.db.rules()))
        prompt = a.brain.command_step.call_args.args[1]
        self.assertIn("你们最近的私信", prompt)
        self.assertIn("待会儿我在评论区 @ 你，你帮我叫一下小红", prompt)
        # 私信里下命令时，也看得到评论区刚聊过的
        a.db.add_message(kind="at", ext_id="at_z", user_name="小廖", content="@雨雨 这个视频好好笑", status="replied",
                         reply="笑死我了", video_title="猫咪打架")
        a.brain.command_step.side_effect = _plans({"tool": "chat", "args": {}})
        mid = a.db.add_message(kind="dm", ext_id="dm_q", user_name="小廖", content="把刚才那个视频发给小红", status="pending")
        a.commands.handle(_inbox(), mid, {"user": "小廖", "text": "把刚才那个视频发给小红", "peer_uid": OWNER_UID,
                                          "shares": []}, a.db.owner("小廖"), [], a.db.rules())
        prompt = a.brain.command_step.call_args.args[1]
        self.assertIn("你们最近在评论区的来往", prompt)
        self.assertIn("这个视频好好笑", prompt)


class UiTests(unittest.TestCase):
    def test_taste_page_and_api(self):
        js = (ROOT / "web" / "js" / "app.js").read_text(encoding="utf-8")
        for w in ('"mytaste", "她的口味"', "taste-think", "她最近的口味", "不用你设置", "她一开始感兴趣的内容"):
            self.assertIn(w, js)
        from app.api import Api
        a = _agent()
        a.my_taste.router = _router({"line": "最近爱看猫", "loves": ["猫"], "rising": [], "tired": [], "curious": []})
        a.my_taste.on_video("猫", 9, 6, liked=True)
        api = Api.__new__(Api)
        api._db, api._agent = a.db, a
        d = api.get_memory("mytaste")["data"]
        self.assertEqual(d["topics"][0]["topic"], "猫")
        self.assertEqual(d["start"], a.db.persona()["interests"])
        r = api.memory_action("taste_think")
        self.assertTrue(r["ok"])
        self.assertIn("cost", r["data"])
        self.assertEqual(api.get_memory("mytaste")["data"]["profile"]["line"], "最近爱看猫")
        self.assertIn("taste", Api.EVENT_GROUPS["watch"])


if __name__ == "__main__":
    unittest.main()
