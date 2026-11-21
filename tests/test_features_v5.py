"""第五轮：其他功能做完不留记录（失败才弹窗）、导出文案时用大模型改写成短视频文案、
私信一一回复 + 像网友一样拆成几条短消息发 + 看懂“引用”、配音只读正文。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_features_v2 import make_db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

ORIGINAL = ("大家好啊，那个今天呢跟大家聊一聊，就是说，猫咪为什么每天要睡十几个小时。"
            "第一，猫是夜行动物，它们的祖先在晚上和清晨捕猎，白天就要保存体力。"
            "第二，猫吃的是肉，消化肉要消耗很多能量，所以要多睡来补充。"
            "第三，小猫和老猫睡得更多，小猫一天能睡二十个小时。"
            "所以如果你家猫一直睡，不用太担心，但是如果它突然不吃东西还一直睡，就要带去看医生了。"
            "好了，喜欢的话记得点赞关注我，我们下期再见。")
GOOD = ("你家猫一天睡十几个小时？先别慌\n"
        "原因有三个：\n"
        "一是猫天生是夜行动物，祖先在夜里和清晨出去打猎，白天得省着体力\n"
        "二是猫以肉为主食，消化肉类特别耗能量，只能靠多睡补回来\n"
        "三是幼猫和老年猫睡得更久，小奶猫一天能睡满二十小时\n"
        "所以猫一直睡是正常的；可如果它突然不吃不喝还总在睡，就得赶紧去看兽医\n"
        "你家主子一天睡多久？评论区说说")


def _router(*answers):
    r = mock.Mock()
    r.call.side_effect = [mock.Mock(text=a, cost=0.0, tokens=0) for a in answers]
    return r


# ---------------------------------------------------------------- 改写规则
class RewriteTests(unittest.TestCase):
    def test_rules_cover_what_was_asked(self):
        from app.tools.rewrite import RULES
        for need in ("内容完全一致", "一个都不能少", "70%～95%", "不侵权", "不要连着十几个字照抄", "黄金 3 秒",
                     "评论区", "不用绝对化", "3 个备选标题", "5 个话题标签"):
            self.assertIn(need, RULES)

    def test_check_length_and_copying(self):
        from app.tools import rewrite as rw
        good = rw.check(ORIGINAL, GOOD)
        self.assertTrue(0.7 <= good["ratio"] <= 1.05, good)
        self.assertEqual(good["copied"], [])
        self.assertTrue(good["ok"])
        short = rw.check(ORIGINAL, "猫是夜行动物，所以爱睡觉。")
        self.assertFalse(short["ok"])
        self.assertIn("太短了", short["problems"][0])
        copy = rw.check(ORIGINAL, ORIGINAL.replace("大家好啊，", ""))
        self.assertIn("一字不差", copy["problems"][-1])
        self.assertTrue(any("夜行动物" in r for r in copy["copied"]))

    def test_parse(self):
        from app.tools import rewrite as rw
        d = rw.parse('好的：\n```json\n{"titles": ["猫为啥总在睡"], "body": "第一行\\n第二行", "tags": ["养猫", "#猫咪"]}\n```')
        self.assertEqual(d["body"], "第一行\n第二行")
        self.assertEqual(d["tags"], ["#养猫", "#猫咪"])
        self.assertEqual(rw.parse("不是 JSON 的一段话")["body"], "不是 JSON 的一段话")

    def test_retry_when_too_short_then_accept(self):
        from app.tools import rewrite as rw
        first = json.dumps({"titles": ["a"], "body": "猫爱睡觉。", "tags": ["#猫"]}, ensure_ascii=False)
        second = json.dumps({"titles": ["猫咪为什么这么能睡"], "body": GOOD, "tags": ["#养猫"]}, ensure_ascii=False)
        agent = mock.Mock()
        agent.brain.router = _router(first, second)
        res = rw.rewrite(ORIGINAL, agent, desc="猫咪睡觉", extra="语气活泼一点")
        self.assertEqual(res["body"], GOOD)
        self.assertEqual(res["titles"], ["猫咪为什么这么能睡"])        # 用重改后的那一版
        self.assertTrue(res["check"]["ok"])
        calls = agent.brain.router.call.call_args_list
        self.assertEqual(calls[0].args[0], "write")                  # 用“写作”的那个模型
        self.assertIn("语气活泼一点", calls[0].args[2])
        self.assertIn("太短了", calls[1].args[2])                     # 第二次把问题告诉它

    def test_long_text_is_rewritten_in_parts(self):
        from app.tools import rewrite as rw
        long = "\n".join([ORIGINAL] * 30)
        agent = mock.Mock()
        agent.brain.router = mock.Mock()
        agent.brain.router.call.side_effect = lambda *a, **k: mock.Mock(
            text=json.dumps({"titles": ["t"], "body": GOOD * 6, "tags": ["#x"]}, ensure_ascii=False))
        res = rw.rewrite(long, agent)
        prompts = [c.args[2] for c in agent.brain.router.call.call_args_list]
        self.assertGreater(len(prompts), 1)
        self.assertIn("这一段要有开头钩子", prompts[0])
        self.assertIn("这一段要写结尾", prompts[-1])
        self.assertTrue(res["body"])


# ---------------------------------------------------------------- 导出：原文 + 改写，做完不留记录
class ExportTests(unittest.TestCase):
    def setUp(self):
        from app.tools import jobs as J
        from app.tools import ytdl
        from tests.test_features_v3 import _fake_ytdlp
        self.J = J
        self.db = make_db()
        self.out = Path(tempfile.mkdtemp())
        self.db.set_json("tools", {"dir": str(self.out)})
        self.agent = mock.Mock()
        self.tools = J.ToolJobs(self.db, self.agent)
        self.tools.pool = mock.Mock(submit=lambda fn, job: fn(job))
        segs = [{"tStartMs": i * 1000, "dDurationMs": 1000, "segs": [{"utf8": s}]}
                for i, s in enumerate(ORIGINAL.split("。")) if s]
        fake, _ = _fake_ytdlp({"subtitles": {"zh-Hans": [{"ext": "json3", "url": "u"}]}, "language": "zh"},
                              json.dumps({"events": segs}, ensure_ascii=False))
        ytdl._loaded = fake
        self.addCleanup(lambda: setattr(ytdl, "_loaded", None))

    def test_export_has_original_and_rewrite(self):
        ans = json.dumps({"titles": ["猫咪为什么这么能睡", "猫睡 20 小时正常吗", "猫一直睡要担心吗"],
                          "body": GOOD, "tags": ["#养猫", "#猫咪", "#宠物知识", "#猫咪睡觉", "#新手养猫"]},
                         ensure_ascii=False)
        self.agent.brain.router = _router(ans)
        self.tools.start("text", "https://www.youtube.com/watch?v=abc", {"rewrite": True, "srt": False})
        ev = self.tools.pop_events()[0]
        self.assertEqual((ev["status"], ev["warning"]), ("done", ""))
        txt = next((self.out / "文案").glob("*.txt")).read_text(encoding="utf-8-sig")
        self.assertIn("———— 视频里说的话（原文）————", txt)
        self.assertIn("夜行动物", txt.split("改写后的文案")[0])
        self.assertIn("———— 改写后的文案", txt)
        self.assertIn("1. 猫咪为什么这么能睡", txt)
        self.assertIn(GOOD, txt)
        self.assertIn("话题：#养猫 #猫咪", txt)
        self.assertIn("改写检查：原文", txt)
        self.assertEqual(self.tools.list(), [])

    def test_rewrite_failure_still_exports_the_original_and_warns(self):
        self.agent.brain.router = mock.Mock()
        self.agent.brain.router.call.side_effect = RuntimeError("API Key 不对")
        self.tools.start("text", "https://www.youtube.com/watch?v=abc", {"rewrite": True, "srt": False})
        ev = self.tools.pop_events()[0]
        self.assertEqual(ev["status"], "done")
        self.assertIn("大模型改写没成功", ev["warning"])          # 界面会弹窗说明
        txt = next((self.out / "文案").glob("*.txt")).read_text(encoding="utf-8-sig")
        self.assertIn("夜行动物", txt)
        self.assertNotIn("改写后的文案", txt)

    def test_settings_and_friendly_errors(self):
        s = self.tools.save_settings({"rewrite": 1, "rewrite_extra": "x" * 500})
        self.assertTrue(s["rewrite"])
        self.assertEqual(len(s["rewrite_extra"]), 300)
        msg = "没读到这个抖音作品的数据（作品可能已删除、设为私密或仅粉丝可见）"
        self.assertEqual(self.J.friendly_error(RuntimeError(msg)), msg)       # 自己的原因原样显示，不改成“私密的”
        self.assertIn("私密", self.J.friendly_error(RuntimeError("ERROR: Private video. Sign in")))

    def test_api_and_ui(self):
        from app.api import Api
        api = Api(self.db, mock.Mock())
        info = api.tools_info()["data"]
        self.assertIn("内容完全一致", info["rewrite_rules"])
        r = api.tools_jobs()["data"]
        self.assertEqual(set(r), {"jobs", "events"})
        self.assertIn("tool_events", api.get_state()["data"])
        js = (ROOT / "web" / "js" / "app.js").read_text(encoding="utf-8")
        for need in ("tool-rewrite", "tool-rewrite-extra", '"tool-rules"', "function toolEvents", "没成功`",
                     "toolEvents(S.st.tool_events)"):
            self.assertIn(need, js)
        self.assertNotIn("tool-clear", js)
        self.assertNotIn('"tool-text"', js)


# ---------------------------------------------------------------- 私信：一一回复、拆成短消息、看懂引用
class DmStyleTests(unittest.TestCase):
    def _brain(self, answer):
        from app.agent.brain import Brain
        b = Brain(make_db())
        b.router = _router(answer)
        b.safety_check = mock.Mock(return_value=(True, "", 0.0))
        return b

    def test_casual_chat_is_split_into_short_messages(self):
        b = self._brain("哈哈哈\n吃了吃了\n今天吃的螺蛳粉！")
        d = b.reply_dm("小鹿", [], "你吃饭了吗")
        self.assertEqual(d.parts, ["哈哈哈", "吃了吃了", "今天吃的螺蛳粉！"])
        p = b.router.call.call_args.args[2]
        self.assertIn("像社交平台上的网友聊天", p)
        self.assertIn("拆成几条短消息", p)
        self.assertIn("专业知识", p)                                  # 讲专业知识时才讲完整

    def test_several_messages_answered_one_by_one(self):
        b = self._brain("[1] 吃啦\n[1] 刚吃完火锅hhh\n[2] 下班辛苦啦\n[3] 超喜欢猫！")
        d = b.reply_dm("小鹿", [], "你吃饭了吗\n我刚下班\n对了你喜欢猫吗")
        self.assertEqual(d.parts, ["吃啦", "刚吃完火锅hhh", "下班辛苦啦", "超喜欢猫！"])
        self.assertEqual(d.text, "吃啦\n刚吃完火锅hhh\n下班辛苦啦\n超喜欢猫！")
        self.assertIn("要一一回复", b.router.call.call_args.args[2])

    def test_split_bubbles_and_length_limits(self):
        from app.agent.brain import split_bubbles
        from app.agent.safety import output_problem
        self.assertEqual(split_bubbles("[1-2] 好呀\n\n[3] 嗯"), ["好呀", "嗯"])
        many = split_bubbles("\n".join(f"第{i}句" for i in range(12)))
        self.assertEqual(len(many), 8)
        self.assertIn("第11句", many[-1])
        knowledge = "\n".join("[1] " + "要点" * 25 for _ in range(7))     # 讲知识：7 条、350 字
        self.assertIsNone(output_problem(knowledge))
        self.assertEqual(output_problem("长" * 301), "内容太长")

    def test_quote_is_understood(self):
        from app.douyin.inbox import quote_line, quote_target, split_quote
        self.assertEqual(split_quote("引用 雨雨：我今天去看海啦"), ("雨雨", "我今天去看海啦"))
        earlier = [{"from_me": True, "text": "我今天去看海啦，风超大"}, {"from_me": False, "text": "真好"}]
        self.assertEqual(quote_target("雨雨：我今天去看海啦", earlier), "me")
        self.assertEqual(quote_target("小鹿：真好", earlier), "them")
        self.assertEqual(quote_target("不认识的话", earlier, ("雨雨",)), "")
        self.assertEqual(quote_target("雨雨：很早以前说的", [], ("雨雨",)), "me")
        line = quote_line({"text": "哪个海边呀", "quote": "雨雨：我今天去看海啦", "quote_of": "me"})
        self.assertEqual(line, "[引用了你之前说的「我今天去看海啦」，针对这句回复] 哪个海边呀")
        b = self._brain("在青岛！")
        b.reply_dm("小鹿", [], line)
        self.assertIn("点了“引用”再说的", b.router.call.call_args.args[2])

    def test_read_chat_separates_the_quote_from_the_message(self):
        from app.douyin.inbox import Inbox
        ib = Inbox.__new__(Inbox)
        ib.b = mock.Mock()
        ib.b.sel = {"dm_newest_first": False}
        ib.my_names = ("雨雨",)
        Inbox._dumped_tags.add("dm_quote")
        mine, theirs = mock.Mock(), mock.Mock()
        mine.evaluate.side_effect = lambda js, *a: True if "closest" in js else []
        theirs.evaluate.side_effect = lambda js, *a: False if "closest" in js else ["雨雨：我今天去看海啦"]
        ib.b.find_all.return_value = [mine, theirs]
        ib.b.text_of.side_effect = lambda el, n=300: "我今天去看海啦" if el is mine else "雨雨：我今天去看海啦 哪个海边呀"
        ib._classify = mock.Mock(return_value=("text", ""))
        msgs = ib._read_chat(8)
        self.assertEqual(msgs[1]["text"], "哪个海边呀")
        self.assertEqual((msgs[1]["quote"], msgs[1]["quote_of"]), ("雨雨：我今天去看海啦", "me"))

    def test_quote_in_plain_text_and_calibration_dumps(self):
        from app.douyin.inbox import Inbox
        ib = Inbox.__new__(Inbox)
        ib.b = mock.Mock()
        ib.b.dump.return_value = "data/debug/x.html"
        saved = set(Inbox._dumped_tags)
        Inbox._dumped_tags.discard("dm_quote")
        Inbox._dumped_tags.discard("dm_quote_miss")
        try:
            m = mock.Mock()
            m.evaluate.return_value = []                                 # 页面里没有引用块的样式
            m.inner_text.return_value = "引用 雨雨：我今天去看海啦\n哪个海边呀"
            body, q = ib._quote_of(m, "引用 雨雨：我今天去看海啦 哪个海边呀")
            self.assertEqual((body, q), ("哪个海边呀", "引用 雨雨：我今天去看海啦"))
            self.assertEqual(ib.b.dump.call_args.args[0], "dm_quote")    # 认出来了：存一份页面
            m.inner_text.return_value = "引用了一下"                      # 像引用、但认不出
            self.assertEqual(ib._quote_of(m, "引用了一下"), ("引用了一下", ""))
            self.assertEqual(ib.b.dump.call_args.args[0], "dm_quote_miss")
        finally:
            Inbox._dumped_tags.clear()
            Inbox._dumped_tags.update(saved)

    def test_runner_sends_the_parts_one_by_one(self):
        from app.agent.runner import Agent
        db = make_db()
        a = Agent(db)
        ib = mock.Mock()
        ib.reply_dm.side_effect = [True, True, False]
        with mock.patch("app.agent.runner.cancel.sleep") as sl:
            sent, n = a._send_dm_parts(ib, {"user": "小鹿"}, ["哈哈", "吃了", "螺蛳粉"])
        self.assertEqual((bool(sent), n), (False, 2))
        self.assertEqual([c.args[1] for c in ib.reply_dm.call_args_list], ["哈哈", "吃了", "螺蛳粉"])
        self.assertEqual(sl.call_count, 2)                            # 每两条之间停一下

    def test_missed_check_with_a_split_reply(self):
        from app.douyin.inbox import Inbox
        ib = Inbox.__new__(Inbox)
        ib.b = mock.Mock()
        ib._chat_title = mock.Mock(return_value="小鹿")
        rows = [(True, "早呀"), (False, "你吃饭了吗"), (False, "我刚下班"), (True, "吃啦"),
                (False, "对了你喜欢猫吗"),                                  # 她发第一条和第二条之间对方又说了
                (True, "下班辛苦啦"), (False, "在吗")]
        ib._read_chat = mock.Mock(return_value=[{"from_me": me, "kind": "text", "text": t, "url": "", "el": None}
                                                for me, t in rows])
        self.assertEqual(ib.dm_missed("小鹿", ["你吃饭了吗", "我刚下班"], "吃啦\n下班辛苦啦"), ["对了你喜欢猫吗", "在吗"])
        rows2 = [(False, "你吃饭了吗"), (True, "吃啦"), (True, "刚吃完")]
        ib._read_chat.return_value = [{"from_me": me, "kind": "text", "text": t, "url": "", "el": None} for me, t in rows2]
        self.assertEqual(ib.dm_missed("小鹿", ["你吃饭了吗"], "吃啦\n刚吃完"), [])

    def test_owner_hello_is_split_too(self):
        b = self._brain("在干嘛呀\n我今天刷到一只超胖的橘猫")
        d = b.owner_hello({"name": "小廖", "call": "哥哥"}, [], "", [])
        self.assertEqual(d.parts, ["在干嘛呀", "我今天刷到一只超胖的橘猫"])
        self.assertIn("拆成 1～3 条短消息", b.router.call.call_args.args[2])


# ---------------------------------------------------------------- 配音只读正文
class VoiceBodyOnlyTests(unittest.TestCase):
    def test_title_is_not_read(self):
        from app.publish import voice
        self.assertEqual(voice.speech_text("周末的小确幸", "今天去了海边"), "今天去了海边")
        self.assertEqual(voice.speech_text("只有标题", ""), "")


if __name__ == "__main__":
    unittest.main()
