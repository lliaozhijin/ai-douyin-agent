"""第九轮：发作品。
① 知道自己最近发过什么（不老写同一件事）；② 看得到大家对她作品的反应（点赞、评论、收藏，别人说了什么）；
③ 今天的事按“印象有多深”来挑，不只是重要程度最高的 6 件；
④ 写法更随性：不给她定规矩，先想几个再挑一个不那么典型的，写完自己读一遍（AI 味重就改）。"""
from __future__ import annotations

import json
import random
import sys
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_features_v2 import make_db  # noqa: E402

AI_TEXT = ("生活不是等待风暴过去，而是学会在雨中跳舞。时光温柔，岁月静好，美好总会如期而至。"
           "我们要热爱生活，热爱每一个平凡的日子。愿你被这个世界温柔以待。")
HUMAN_TEXT = "今天下楼拿快递 结果门禁卡忘带了 在楼下站了二十分钟 冻死 后来是隔壁阿姨开的门哈哈哈"


def _resp(obj, cost=0.001):
    return mock.Mock(text=json.dumps(obj, ensure_ascii=False), cost=cost, tokens=10)


class PostingHelpersTests(unittest.TestCase):
    def test_ai_tells(self):
        from app.agent.posting import ai_tells
        tells = ai_tells(AI_TEXT)
        self.assertIn("“不是……而是……”", tells)
        self.assertIn("结尾在总结升华", tells)
        self.assertIn("抽象的漂亮话很多", tells)
        self.assertEqual(ai_tells(HUMAN_TEXT), [])
        self.assertEqual(ai_tells("好困"), [])
        self.assertIn("一二三地分条讲", ai_tells("首先我要说今天天气不错。其次我吃了火锅。最后我睡了一觉，挺好的。"))
        self.assertIn("排比", ai_tells("有的人爱吃甜，有的人爱吃辣，有的人什么都吃，我是最后那种"))
        self.assertIn("心灵鸡汤的腔调", ai_tells("今天有点累，但是没关系，要学会和自己和解，慢慢来就好，晚安晚安"))

    def test_pick_idea_skips_repeats_and_prefers_less_typical(self):
        from app.agent.posting import pick_idea
        ideas = [{"idea": "今天刷到一只橘猫在打滚", "p": 0.6}, {"idea": "楼下奶茶店换了新店员", "p": 0.1},
                 {"idea": "想吃火锅", "p": 0.3}, {"idea": "", "p": 0.9}, "随手的一句话", 42]
        avoid = ["刷到一只橘猫在打滚 太可爱了"]
        rnd = mock.Mock(random=mock.Mock(return_value=0.1), choice=lambda xs: xs[0])
        self.assertEqual(pick_idea(ideas, avoid, rnd)["idea"], "楼下奶茶店换了新店员")   # 不典型的那个
        rnd = mock.Mock(random=mock.Mock(return_value=0.9), uniform=lambda a, b: b)
        self.assertNotEqual(pick_idea(ideas, avoid, rnd)["idea"], "今天刷到一只橘猫在打滚")  # 写过的不挑
        self.assertIsNone(pick_idea([{"idea": "今天刷到一只橘猫在打滚", "p": 0.5}], avoid, random.Random(1)))
        self.assertIsNone(pick_idea(None, [], random.Random(1)))
        got = {pick_idea(ideas, [], random.Random(i))["idea"] for i in range(40)}
        self.assertGreaterEqual(len(got), 3)                                           # 每次挑的不一样

    def test_posts_block(self):
        from app.agent.posting import posts_block
        txt = posts_block([{"day": "2026-11-18", "title": "冬天的猫", "text": "猫猫缩成一团", "likes": 42, "comments": 5,
                            "collects": 3, "said": [{"user": "小明", "text": "好可爱！", "reply": "对吧"}]},
                           {"day": "2026-11-15", "title": "", "text": "一句话", "likes": None, "said": []}],
                          [{"created_at": "2026-11-19 10:00:00", "title": "没发的", "text": "写了没发"}])
        self.assertIn("11-18《冬天的猫》“猫猫缩成一团” —— 42 个赞、5 条评论、3 个收藏；小明说“好可爱！”（你回“对吧”）", txt)
        self.assertIn("《无标题》“一句话” —— 还没看到数据", txt)
        self.assertIn("写了、但没发出去的", txt)
        self.assertEqual(posts_block([]), "你最近还没发过作品。")


class DraftPostTests(unittest.TestCase):
    def _brain(self, *responses):
        from app.agent.brain import Brain
        b = Brain(make_db())
        b.router = mock.Mock()
        b.router.call.side_effect = list(responses)
        return b

    def test_she_may_not_want_to_post(self):
        b = self._brain(_resp({"want": False, "why": "今天好累，不想发", "ideas": []}))
        d = b.draft_post([], "困")
        self.assertFalse(d["want"])
        self.assertEqual(d["why"], "今天好累，不想发")
        self.assertEqual(b.router.call.call_count, 1)

    def test_think_then_write_with_posts_and_reactions(self):
        b = self._brain(_resp({"want": True, "why": "想说说今天", "ideas": [{"idea": "门禁卡忘带了", "p": 0.15}]}),
                        _resp({"title": "", "text": HUMAN_TEXT, "tags": ["日常"], "cards": [{"title": "", "lines": ["x"]}],
                               "image_prompt": "画一张图"}))
        posts = [{"day": "2026-11-18", "title": "冬天的猫", "text": "猫猫缩成一团", "likes": 42, "comments": 5, "collects": 3,
                  "said": [{"user": "小明", "text": "好可爱！", "reply": ""}]}]
        d = b.draft_post(["猫猫视频"], "开心", memory="今天的事：\n- 下楼拿快递", posts=posts, form="voice",
                         rnd=random.Random(1))
        self.assertTrue(d["want"])
        self.assertEqual((d["text"], d["cards"], d["image_prompt"], d["idea"]), (HUMAN_TEXT, [], "", "门禁卡忘带了"))
        think = b.router.call.call_args_list[0].args[2]
        for w in ("42 个赞", "小明说“好可爱！”", "下楼拿快递", "可能性", "不想发也完全可以"):
            self.assertIn(w, think)
        write = b.router.call.call_args_list[1].args[2]
        self.assertIn("你想发的是：门禁卡忘带了", write)
        self.assertIn("像你平时随手发抖音那样", write)
        self.assertNotIn("cards", write)                                   # 配音图片视频：不用分卡片
        self.assertEqual(b.router.call.call_args_list[0].kwargs["temperature"], 1.0)

    def test_owner_topic_skips_thinking(self):
        b = self._brain(_resp({"title": "海边", "text": "想去海边吹风了"}))
        d = b.draft_post([], "平静", topic="写写海边")
        self.assertTrue(d["want"])
        self.assertIn("主人让我写：写写海边", d["why"])
        self.assertEqual(b.router.call.call_count, 1)
        self.assertIn("主人让你写的：「写写海边」", b.router.call.call_args.args[2])

    def test_rereads_when_it_sounds_like_ai(self):
        b = self._brain(_resp({"want": True, "ideas": [{"idea": "随便说说", "p": 0.5}]}),
                        _resp({"title": "生活", "text": AI_TEXT}),
                        _resp({"title": "", "text": HUMAN_TEXT}))
        d = b.draft_post([], "开心", rnd=random.Random(1))
        self.assertEqual(d["text"], HUMAN_TEXT)
        note = b.router.call.call_args_list[2].args[2]
        self.assertIn("像在写作文", note)
        self.assertIn("你刚写好的是", note)
        self.assertEqual(b.router.call.call_count, 3)                      # 最多改一次

    def test_rereads_when_too_similar_or_cannot_be_posted(self):
        posts = [{"day": "2026-11-18", "title": "门禁卡", "text": HUMAN_TEXT, "likes": 1, "comments": 0, "collects": 0}]
        b = self._brain(_resp({"want": True, "ideas": [{"idea": "又一件小事", "p": 0.5}]}),
                        _resp({"title": "", "text": HUMAN_TEXT}),
                        _resp({"title": "", "text": "今天发现楼下新开了一家面馆，牛肉面巨大一碗"}))
        d = b.draft_post([], "开心", posts=posts, rnd=random.Random(1))
        self.assertIn("面馆", d["text"])
        self.assertIn("差不多", b.router.call.call_args_list[2].args[2])
        b = self._brain(_resp({"want": True, "ideas": [{"idea": "推荐个网站", "p": 0.5}]}),
                        _resp({"title": "", "text": "看这个 https://x.com 超好玩"}),
                        _resp({"title": "", "text": "今天发现一个超好玩的小游戏"}))
        d = b.draft_post([], "开心", rnd=random.Random(1))
        self.assertIn("包含链接", b.router.call.call_args_list[2].args[2])
        self.assertNotIn("http", d["text"])

    def test_all_ideas_already_posted(self):
        posts = [{"day": "2026-11-18", "title": "门禁卡", "text": HUMAN_TEXT}]
        b = self._brain(_resp({"want": True, "ideas": [{"idea": "门禁卡忘带了在楼下站了二十分钟", "p": 0.5}]}))
        d = b.draft_post([], "开心", posts=posts)
        self.assertFalse(d["want"])
        b = self._brain(_resp({"want": True, "ideas": [{"idea": "门禁卡忘带了在楼下站了二十分钟", "p": 0.5}]}),
                        _resp({"title": "随便", "text": "今天也是普通的一天，晚饭吃了面"}))
        self.assertTrue(b.draft_post([], "开心", posts=posts, force=True)["want"])     # 你让她写：一定写

    def test_revise_prompt_has_no_rule_list(self):
        b = self._brain(_resp({"title": "新标题", "text": "改好了"}))
        b.revise_post({"title": "旧", "text": "旧的"}, "标题改可爱点")
        self.assertNotIn("不涉及政治", b.router.call.call_args.args[2])


class HighlightsTests(unittest.TestCase):
    def test_salient_diverse_and_small_things(self):
        from app.agent.memory import Memory
        db = make_db()
        m = Memory(db)
        for i in range(8):
            m.remember("video", f"刷到第 {i} 个猫视频", importance=4, dedupe=f"v{i}")
        m.remember("dm", "小明说他今天被老板骂了，气死了", person="小明", scope="dm:小明", importance=3, dedupe="d1")
        m.remember("dm", "小明后来又说想辞职", person="小明", scope="dm:小明", importance=3, dedupe="d2")
        m.remember("dm", "小明问我晚饭吃什么", person="小明", scope="dm:小明", importance=3, dedupe="d3")
        m.remember("note", "楼下的猫今天没出来", importance=2, dedupe="n1")
        m.remember("note", "门禁卡忘带了在楼下站了二十分钟", importance=3, dedupe="n2")
        got = m.highlights(rnd=random.Random(3))
        self.assertLessEqual(len(got), 10)
        self.assertLessEqual(sum("猫视频" in g for g in got), 5)              # 同一类最多 3 件（加上两件小事）
        self.assertLessEqual(sum("小明" in g for g in got), 2 + 2)
        self.assertTrue(any("气死了" in g for g in got))                    # 有情绪的事印象深
        avoided = m.highlights(avoid=["门禁卡忘带了在楼下站了二十分钟，冻死"], rnd=random.Random(3), n=4)
        self.assertFalse(any("门禁卡" in g for g in avoided[:2]))            # 写过的事往后排

    def test_yesterday_when_today_is_quiet(self):
        from app.agent.memory import Memory
        db = make_db()
        m = Memory(db)
        m.remember("note", "今天只做了一件事")
        m.remember("note", "昨天和朋友吃了火锅", dedupe="y")
        y = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        db.execute("UPDATE memories SET day=? WHERE dedupe='y'", (y,))
        got = m.highlights()
        self.assertIn("今天只做了一件事", got)
        self.assertIn("（昨天）昨天和朋友吃了火锅", got)
        self.assertIn("今天的事（标着“私信里”的是别人私下跟你说的）：", m.post_material())
        m.remember("dm", "小红说她分手了", person="小红", scope="dm:小红", dedupe="p")
        self.assertIn("私信里：小红说她分手了", m.highlights())


class WorksTests(unittest.TestCase):
    def _db_with_posts(self):
        db = make_db()
        now = datetime.now()
        a = db.add_draft(status="published", title="冬天的猫", text="猫猫缩成一团好可爱", source="auto")
        b = db.add_draft(status="published", title="", text="今天的晚霞是粉色的", source="auto")
        c = db.add_draft(status="discarded", title="没发的", text="写了没发", source="auto")
        db.update_draft(a, published_at=(now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"))
        db.update_draft(b, published_at=(now - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S"))
        return db, a, b, c, now

    def test_save_works_match_drafts_and_recent_posts(self):
        db, a, b, c, now = self._db_with_posts()
        n = db.save_my_works([
            {"aweme_id": "7500000000000000001", "desc": "冬天的猫 猫猫缩成一团好可爱 #萌宠", "likes": 42, "comments": 5,
             "collects": 3, "shares": 1, "plays": 0, "create_time": int((now - timedelta(days=2)).timestamp())},
            {"aweme_id": "7500000000000000002", "desc": "（文案被抖音改了）", "likes": 7, "comments": 0, "collects": 0,
             "shares": 0, "plays": 0, "create_time": int((now - timedelta(hours=5, minutes=3)).timestamp())},
            {"aweme_id": "7500000000000000003", "desc": "很久以前的", "likes": 1, "create_time": 1000}])
        self.assertEqual(n, 2)                                                 # 一个按文案认出，一个按发布时间认出
        db.add_message(kind="work", ext_id="w1", user_name="小明", content="好可爱！", status="replied", reply="对吧",
                       video_url="https://www.douyin.com/note/7500000000000000001")
        posts = db.recent_posts()
        self.assertEqual([p["id"] for p in posts], [b, a])
        self.assertEqual((posts[1]["likes"], posts[1]["comments"], posts[1]["collects"]), (42, 5, 3))
        self.assertEqual(posts[1]["said"], [{"user": "小明", "text": "好可爱！", "reply": "对吧"}])
        self.assertEqual(posts[0]["likes"], 7)
        db.save_my_works([{"aweme_id": "7500000000000000001", "desc": "冬天的猫 猫猫缩成一团好可爱", "likes": 50}])
        self.assertEqual(db.recent_posts()[1]["likes"], 50)                    # 数据会更新
        self.assertEqual([d["id"] for d in db.unposted_drafts()], [c])

    def test_browser_collects_only_on_own_page(self):
        from app.douyin.browser import DouyinBrowser
        br = DouyinBrowser.__new__(DouyinBrowser)
        br.my_works, br.my_works_on = [], False
        data = {"aweme_list": [{"aweme_id": "7500000000000000001", "desc": "x", "create_time": 1,
                                "statistics": {"digg_count": 3, "comment_count": 1, "collect_count": 2}}]}
        resp = mock.Mock(url="https://www.douyin.com/aweme/v1/web/aweme/post/?sec_user_id=x",
                         headers={"content-type": "application/json"})
        resp.json.return_value = data
        br.users_seen, br.notice_comments, br.search_items, br.aweme_details = {}, [], [], {}
        br.following = {}
        br.media = {}
        with mock.patch.object(DouyinBrowser, "_collect_media"):
            br._on_response(resp)
            self.assertEqual(br.my_works, [])                                   # 别人的主页：不收
            br.my_works_on = True
            br._on_response(resp)
            br._on_response(resp)
        self.assertEqual(len(br.my_works), 1)
        self.assertEqual((br.my_works[0]["likes"], br.my_works[0]["collects"]), (3, 2))

    def test_fetch_my_works(self):
        from app.douyin import works
        b = mock.Mock()
        b.sel = {}
        b.in_new_tab.return_value = mock.MagicMock()
        state = {"n": 0}

        def sleep(_s):
            state["n"] += 1
            if state["n"] == 2:
                b.my_works = [{"aweme_id": "1", "create_time": 5}, {"aweme_id": "2", "create_time": 9}]

        with mock.patch.object(works.cancel, "sleep", side_effect=sleep), \
                mock.patch.object(works.time, "time", side_effect=[0, 0, 1, 2, 2, 3, 5, 5, 6, 7, 20, 30]):
            got = works.fetch_my_works(b)
        self.assertEqual([w["aweme_id"] for w in got], ["2", "1"])
        self.assertFalse(b.my_works_on)


class RunnerDraftTests(unittest.TestCase):
    def test_make_draft_passes_posts_and_splits_cards(self):
        import tempfile
        from app.agent import runner
        from app.agent.runner import Agent
        db = make_db()
        db.set_json("rules", dict(db.rules(), post_form="cards", image_gen_auto=False))
        a = Agent(db)
        a.brain = mock.Mock()
        a.mood = mock.Mock()
        a.mood.current.return_value = {"key": "happy", "label": "开心", "reason": "x"}
        did0 = db.add_draft(status="published", title="冬天的猫", text="猫猫缩成一团", source="auto")
        db.update_draft(did0, published_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        a.brain.draft_post.return_value = {"want": True, "why": "想说", "title": "", "text": "第一句。第二句。第三句。",
                                           "tags": [], "cards": [], "image_prompt": "", "idea": "x", "cost": 0}
        with mock.patch.object(runner, "DRAFTS_DIR", Path(tempfile.mkdtemp())):
            did = a._make_draft(force=True)
        kw = a.brain.draft_post.call_args.kwargs
        self.assertEqual(kw["posts"][0]["title"], "冬天的猫")
        self.assertEqual((kw["form"], kw["want_image"]), ("cards", False))
        self.assertTrue(db.draft(did)["cards"])                                # 她没分卡片：按正文自动分

    def test_refresh_works_is_throttled(self):
        from app.agent.runner import Agent
        db = make_db()
        a = Agent(db)
        a.browser = mock.Mock()
        with mock.patch("app.douyin.works.fetch_my_works", return_value=[]) as f:
            self.assertEqual(a._refresh_my_works(), 0)                         # 最近没发过作品：不去看
            f.assert_not_called()
            did = db.add_draft(status="published", title="t", text="x", source="auto")
            db.update_draft(did, published_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            a._refresh_my_works()
            a._refresh_my_works()                                              # 刚看过：不再去
            self.assertEqual(f.call_count, 1)
            db.set_json("my_works_at", time.time() - 7 * 3600)
            a._refresh_my_works()
            self.assertEqual(f.call_count, 2)
            f.side_effect = RuntimeError("网页改版")
            self.assertEqual(a._refresh_my_works(force=True), 0)               # 读不到不影响写作品
        a.browser = None
        self.assertEqual(a._refresh_my_works(force=True), 0)


if __name__ == "__main__":
    unittest.main()
