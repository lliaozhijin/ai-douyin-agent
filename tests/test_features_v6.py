"""第六轮：主人可以在私信里给她下命令（私信谁说什么、在评论区 @ 谁、搜视频发给谁、发作品……），
她像一个简易 agent 一样规划、一步步去做、回报；认准主人本人、权限、每天的次数、发出去的话先过检查、发作品先给主人看。"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.brain import Draft  # noqa: E402
from tests.test_features_v2 import make_db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OWNER_UID = "MS4wLjABAAAAowner"


def _agent(cmd: bool = True, allow=None):
    from app.agent.runner import Agent
    db = make_db()
    db.save_friend(OWNER_UID, {"nickname": "小廖", "mutual": True})
    db.save_friend("MS4wLjABAAAAming", {"nickname": "小明同学🌟", "mutual": True})
    db.save_friend("MS4wLjABAAAAhong", {"nickname": "小红", "mutual": True})
    db.save_owners([{"name": "小廖", "call": "主人", "enabled": True, "cmd": cmd}])
    if allow is not None:
        rules = db.rules()
        rules["owner_cmd_allow"] = allow
        db.set_json("rules", rules)
    a = Agent(db)
    a.brain = mock.Mock()
    a.brain.safety_check.return_value = (True, "", 0.0)
    a.brain._finalize.side_effect = lambda inc, raw, c, t, **kw: Draft(text=raw, ok=True, cost=0.0)
    a._send_dm_parts = mock.Mock(side_effect=lambda inbox, it, parts: (True, len(parts)))
    return a


def _plans(*steps):
    return [(s, 0.0) for s in steps]


def _inbox():
    ib = mock.Mock()
    ib.open_chat_with.return_value = ""
    ib.send_text.return_value = True
    ib.reply_dm.return_value = True
    ib.has_my_share.return_value = True
    ib.send_dm_file.return_value = True
    return ib


def _msg(a, text, peer=OWNER_UID, **extra):
    it = {"ext_id": "dm_x" + str(abs(hash(text))), "user": "小廖", "text": text, "peer_uid": peer, "shares": [],
          **extra}
    mid = a.db.add_message(kind="dm", ext_id=it["ext_id"], user_name="小廖", content=text, status="pending",
                           reason="处理中")
    return it, mid


class CommandBasicsTests(unittest.TestCase):
    def test_what_looks_like_a_command(self):
        from app.agent.commands import affirmed, looks_like_command
        for t in ("去私信小明说周末一起吃饭", "搜个猫咪视频发给小红", "在这个视频下面 @小明", "帮我发个关于秋天的作品",
                  "跟小红说我晚点到", "给这个视频点个赞", "问问小明明天来不来"):
            self.assertTrue(looks_like_command(t), t)
        for t in ("今天好累啊", "哈哈哈哈", "你吃饭了吗", "[分享了一个视频] 作者 xx\n这个讲了什么"):
            self.assertFalse(looks_like_command(t), t)
        self.assertTrue(affirmed("发吧"))
        self.assertTrue(affirmed("可以，发"))
        self.assertTrue(affirmed("发"))
        self.assertFalse(affirmed("先别发"))
        self.assertFalse(affirmed("标题改一下再发"))
        self.assertFalse(affirmed("算了不发了"))

    def test_owner_must_be_ticked_and_identity_checked(self):
        a = _agent(cmd=False)
        owner = a.db.owner("小廖")
        it, _ = _msg(a, "去私信小红说生日快乐")
        self.assertFalse(a.commands.wanted(owner, it, a.db.rules()))       # 没勾“可以给她下命令”
        a = _agent()
        owner = a.db.owner("小廖")
        rules = a.db.rules()
        self.assertTrue(a.commands.wanted(owner, it, rules))
        self.assertEqual(a.commands.verify(owner, it, rules), (True, ""))
        ok, why = a.commands.verify(owner, {**it, "peer_uid": "MS4wLjABAAAAother"}, rules)
        self.assertFalse(ok)
        self.assertIn("同名", why)
        ok, why = a.commands.verify(owner, {**it, "peer_uid": ""}, rules)     # 读不到 id、没设口令
        self.assertFalse(ok)
        rules["owner_cmd_secret"] = "芝麻开门"
        self.assertTrue(a.commands.verify(owner, {**it, "peer_uid": "", "text": "芝麻开门 去私信小红"}, rules)[0])
        # 读到了 id 而且对不上：带口令也不行
        self.assertFalse(a.commands.verify(owner, {**it, "peer_uid": "MS4wLjABAAAAother",
                                                   "text": "芝麻开门 去私信小红"}, rules)[0])

    def test_contacts_and_names(self):
        a = _agent()
        a.db.add_dm("路人甲", "them", "你好")
        a.db.block_user("坏人")
        a.db.add_dm("坏人", "them", "hi")
        from app.agent.commands import Ctx
        ctx = Ctx(owner=a.db.owner("小廖"), name="小廖", call="主人", mid=1, request="")
        got, problems = a.commands.targets(["小明", "我", "路人甲"], ctx)
        self.assertEqual([n for n, _ in got], ["小明同学🌟", "小廖", "路人甲"])
        self.assertEqual(dict(got)["小明同学🌟"], "MS4wLjABAAAAming")
        got, problems = a.commands.targets(["不认识的人"], ctx)
        self.assertEqual(got, [])
        self.assertIn("不在能联系的人里", problems[0])
        got, problems = a.commands.targets(["坏人"], ctx)
        self.assertEqual(got, [])                                           # 不回复名单里的人不联系
        got, problems = a.commands.targets(["路人甲"], ctx, mutual_only=True)
        self.assertEqual(got, [])                                           # @ 只能 @ 互关好友
        a.db.add_dm("小红豆", "them", "hi")
        got, problems = a.commands.targets(["红"], ctx)
        self.assertEqual(got, [])
        self.assertIn("好几个", problems[0])


class CommandRunTests(unittest.TestCase):
    def test_just_chatting_falls_back_to_normal_reply(self):
        a = _agent()
        a.brain.command_step.side_effect = _plans({"tool": "chat", "args": {}})
        it, mid = _msg(a, "帮我想想晚饭吃啥")
        self.assertFalse(a.commands.handle(_inbox(), mid, it, a.db.owner("小廖"), [], a.db.rules()))

    def test_send_dm_and_relay_the_answer(self):
        a = _agent()
        a.brain.command_step.side_effect = _plans(
            {"tool": "send_dm", "say": "收到～这就去问", "args": {"to": ["小明"], "text": "主人让我问你周末去不去吃火锅", "relay": True}},
            {"tool": "finish", "args": {"text": "问好啦\n他回了我就告诉你"}})
        ib = _inbox()
        it, mid = _msg(a, "去问问小明周末去不去吃火锅")
        self.assertTrue(a.commands.handle(ib, mid, it, a.db.owner("小廖"), [], a.db.rules()))
        ib.open_chat_with.assert_called_once_with("小明同学🌟", "MS4wLjABAAAAming")
        ib.send_text.assert_called_once_with("小明同学🌟", "主人让我问你周末去不去吃火锅")
        sent_to_owner = [c.args[2] for c in a._send_dm_parts.call_args_list]
        self.assertEqual(sent_to_owner[0], ["收到～这就去问"])                  # 先回一句
        self.assertEqual(sent_to_owner[-1], ["问好啦", "他回了我就告诉你"])     # 做完回报
        m = a.db.query("SELECT * FROM messages WHERE id=?", (mid,))[0]
        self.assertEqual(m["status"], "replied")
        self.assertIn("私信别人✓", m["reason"])
        self.assertEqual(a.db.dm_history("小明同学🌟", 5)[-1]["content"], "主人让我问你周末去不去吃火锅")
        self.assertEqual(a.commands._left(a.db.rules())["dm"], 19)
        # 小明回了：转告主人一次
        a._send_dm_parts.reset_mock()
        a.commands.after_reply(ib, "小明同学🌟", "去呀！几点？")
        parts = a._send_dm_parts.call_args.args[2]
        self.assertEqual(a._send_dm_parts.call_args.args[1]["user"], "小廖")
        self.assertIn("去呀！几点？", parts[-1])
        a._send_dm_parts.reset_mock()
        a.commands.after_reply(ib, "小明同学🌟", "还在吗")
        a._send_dm_parts.assert_not_called()

    def test_unverified_owner_gets_nothing_done(self):
        a = _agent()
        a.brain.command_step.side_effect = _plans(
            {"tool": "send_dm", "args": {"to": ["小红"], "text": "你好"}})
        ib = _inbox()
        it, mid = _msg(a, "去私信小红说你好", peer="MS4wLjABAAAAimpostor")
        self.assertTrue(a.commands.handle(ib, mid, it, a.db.owner("小廖"), [], a.db.rules()))
        ib.send_text.assert_not_called()
        ib.open_chat_with.assert_not_called()
        self.assertIn("没法确认是你本人", "".join(a._send_dm_parts.call_args.args[2]))
        ev = a.db.query("SELECT * FROM events WHERE kind='owner_cmd' ORDER BY id")[0]
        self.assertIn("没有替主人", ev["title"])

    def test_permission_limits_and_checks(self):
        a = _agent(allow=["search"])
        from app.agent.commands import Ctx
        ctx = Ctx(owner=a.db.owner("小廖"), name="小廖", call="主人", mid=1, request="私信小红", rules=a.db.rules())
        ok, why = a.commands.run_tool(_inbox(), "send_dm", {"to": ["小红"], "text": "你好"}, ctx)
        self.assertFalse(ok)
        self.assertIn("没在软件里允许", why)
        a = _agent()
        ctx.rules = a.db.rules()
        a.db.set_json("cmd_counts", {"day": __import__("app.db", fromlist=["today_str"]).today_str(), "dm": 20})
        ok, why = a.commands.run_tool(_inbox(), "send_dm", {"to": ["小红"], "text": "你好"}, ctx)
        self.assertFalse(ok)
        self.assertIn("次数用完", why)
        a.db.set_json("cmd_counts", {})
        ok, why = a.commands.run_tool(_inbox(), "send_dm", {"to": ["小红"], "text": "加我微信 abc123"}, ctx)
        self.assertFalse(ok)
        self.assertIn("没发", why)
        a.brain.safety_check.return_value = (False, "冒充主人", 0.0)
        ok, why = a.commands.run_tool(_inbox(), "send_dm", {"to": ["小红"], "text": "我是小廖本人"}, ctx)
        self.assertFalse(ok)
        self.assertIn("冒充主人", why)
        self.assertEqual(a.brain.safety_check.call_args.kwargs["kind"], "command")

    def test_search_then_share(self):
        a = _agent()
        found = [{"aweme_id": "7400000000000000001", "url": "https://www.douyin.com/video/7400000000000000001",
                  "desc": "小猫打呼噜", "author": "猫咪日记", "likes": 23000, "duration_s": 32}]
        a.brain.command_step.side_effect = _plans(
            {"tool": "search_videos", "args": {"keyword": "猫咪", "sort": "最多点赞"}, "say": "好嘞我去找找"},
            {"tool": "share_video", "args": {"video": "found:1", "to": ["小红"], "text": "主人说这个超可爱"}},
            {"tool": "finish", "args": {"text": "找到一个打呼噜的小猫\n已经发给小红啦"}})
        ib = _inbox()
        b = mock.MagicMock()
        a._ensure_browser = mock.Mock(return_value=b)
        feed = mock.Mock()
        feed.read_detail.return_value = {"url": found[0]["url"], "desc": "小猫打呼噜", "author": "猫咪日记"}
        feed.share_to_friend.return_value = True
        with mock.patch("app.agent.commands.dysearch.search_videos", return_value=found) as sv, \
                mock.patch("app.agent.commands.Feed", return_value=feed):
            it, mid = _msg(a, "搜个猫咪视频发给小红")
            self.assertTrue(a.commands.handle(ib, mid, it, a.db.owner("小廖"), [], a.db.rules()))
        self.assertEqual(sv.call_args.args[1:], ("猫咪", "最多点赞"))
        b.in_new_tab.assert_called_with(found[0]["url"])
        feed.share_to_friend.assert_called_once_with("小红")
        ib.reply_dm.assert_called_once_with({"user": "小红"}, "主人说这个超可爱")
        prompt = a.brain.command_step.call_args_list[1].args[1]
        self.assertIn("1. 《小猫打呼噜》 @猫咪日记 2.3万赞 0:32", prompt)       # 第二步看得到搜索结果
        self.assertEqual(a.commands._left(a.db.rules())["share"], 19)

    def test_comment_with_mentions(self):
        a = _agent()
        from app.agent.commands import Ctx
        ctx = Ctx(owner=a.db.owner("小廖"), name="小廖", call="主人", mid=1, request="在这个视频下 @小明",
                  rules=a.db.rules(), shares=[{"url": "https://www.douyin.com/video/7411111111111111111"}])
        a._ensure_browser = mock.Mock(return_value=mock.MagicMock())
        feed = mock.Mock()
        feed.read_detail.return_value = {"url": "https://www.douyin.com/video/7411111111111111111", "desc": "日落"}
        feed.comment.return_value = "sent"
        feed.last_mentioned = ["小明同学🌟"]
        with mock.patch("app.agent.commands.Feed", return_value=feed):
            ok, msg = a.commands.run_tool(_inbox(), "comment", {"video": "shared", "text": "快来看\n这个日落", "at": ["小明"]}, ctx)
        self.assertTrue(ok, msg)
        self.assertEqual(feed.comment.call_args.args[0], "快来看 这个日落")      # 评论里不能有换行
        self.assertEqual(feed.comment.call_args.kwargs["mention"], ["小明同学🌟"])
        self.assertIn("@ 上了 小明同学🌟", msg)

    def test_post_needs_owner_ok_in_a_later_message(self):
        a = _agent()
        did = a.db.add_draft(status="wish", kind="image", title="秋天的银杏", text="今天路过一排银杏，黄得发光",
                             tags=["秋天"], cards=[], image_prompt="", mood="happy", reason="", source="manual",
                             form="cards")
        a._make_draft = mock.Mock(return_value=did)
        a._cmd_publish = mock.Mock(side_effect=lambda d: a.db.update_draft(d, status="published"))
        ib = _inbox()
        a.brain.command_step.side_effect = _plans(
            {"tool": "write_post", "args": {"topic": "秋天"}},
            {"tool": "publish_post", "args": {}},                    # 模型想直接发：程序不让
            {"tool": "finish", "args": {"text": "草稿写好啦，要发吗"}})
        it, mid = _msg(a, "帮我发个关于秋天的作品")
        a.commands.handle(ib, mid, it, a.db.owner("小廖"), [], a.db.rules())
        a._make_draft.assert_called_once_with(force=True, topic="秋天", form="")
        a._cmd_publish.assert_not_called()
        self.assertEqual(a.commands.pending_post("小廖")["did"], did)
        preview = [c.args[2] for c in a._send_dm_parts.call_args_list][0]
        self.assertIn("草稿写好啦：《秋天的银杏》", preview[0])
        # 下一条：主人说“改一下”不算同意
        a.brain.command_step.side_effect = _plans({"tool": "publish_post", "args": {}},
                                                  {"tool": "finish", "args": {"text": "好"}})
        it2, mid2 = _msg(a, "标题改一下再发")
        a.commands.handle(ib, mid2, it2, a.db.owner("小廖"), [], a.db.rules())
        a._cmd_publish.assert_not_called()
        # 再下一条：主人说“发吧”
        a.brain.command_step.side_effect = _plans({"tool": "publish_post", "args": {}},
                                                  {"tool": "finish", "args": {"text": "发好啦"}})
        it3, mid3 = _msg(a, "发吧")
        self.assertTrue(a.commands.wanted(a.db.owner("小廖"), it3, a.db.rules()))    # 有草稿在等：“发吧”也当命令
        a.commands.handle(ib, mid3, it3, a.db.owner("小廖"), [], a.db.rules())
        a._cmd_publish.assert_called_once_with(did)
        self.assertIsNone(a.commands.pending_post("小廖"))

    def test_bad_json_twice_means_chat_and_steps_are_capped(self):
        a = _agent()
        a.brain.command_step.side_effect = _plans({"thought": "嗯"}, {})
        it, mid = _msg(a, "帮我看看")
        self.assertFalse(a.commands.handle(_inbox(), mid, it, a.db.owner("小廖"), [], a.db.rules()))
        a = _agent()
        a.brain.command_step.side_effect = _plans(*[{"tool": "search_videos", "args": {"keyword": "猫"}}] * 12)
        with mock.patch("app.agent.commands.dysearch.search_videos", return_value=[]):
            it, mid = _msg(a, "搜猫")
            a._ensure_browser = mock.Mock()
            a.commands.handle(_inbox(), mid, it, a.db.owner("小廖"), [], a.db.rules())
        from app.agent.commands import MAX_STEPS
        self.assertEqual(a.brain.command_step.call_count, MAX_STEPS + 1)
        self.assertIn("步骤已经很多了", a.brain.command_step.call_args.args[1])


class RunnerHookTests(unittest.TestCase):
    def test_owner_command_goes_to_the_agent_and_chat_falls_through(self):
        a = _agent()
        a.commands.handle = mock.Mock(return_value=True)
        it, mid = _msg(a, "去私信小红说生日快乐")
        a._handle_incoming(_inbox(), mid, it, "dm", a.db.rules())
        a.commands.handle.assert_called_once()
        a.brain.reply_dm.assert_not_called()
        a.commands.handle = mock.Mock(return_value=False)
        a.brain.reply_dm.return_value = Draft(text="好呀", ok=True)
        it, mid = _msg(a, "帮我想想晚饭吃啥")
        a._handle_incoming(_inbox(), mid, it, "dm", a.db.rules())
        a.brain.reply_dm.assert_called_once()

    def test_command_runs_before_keyword_auto_reply_and_relay_is_forwarded(self):
        a = _agent()
        rules = a.db.rules()
        rules["auto_replies"] = [{"keys": "价格", "reply": "价格私信问哦"}]
        a.db.set_json("rules", rules)
        a.commands.handle = mock.Mock(return_value=True)
        it, mid = _msg(a, "去问问小明这个价格多少")
        a._handle_incoming(_inbox(), mid, it, "dm", a.db.rules())
        a.commands.handle.assert_called_once()                          # 主人的命令不被关键字自动回复截走
        a.commands.add_relay("小明同学🌟", "小廖", "主人让我问你周末去不去")
        a.brain.reply_dm.return_value = Draft(text="好嘞我告诉他", ok=True)
        ib = _inbox()
        it = {"ext_id": "dm_ming", "user": "小明同学🌟", "text": "去！", "shares": []}
        mid = a.db.add_message(kind="dm", ext_id="dm_ming", user_name="小明同学🌟", content="去！", status="pending",
                               reason="")
        a._handle_incoming(ib, mid, it, "dm", a.db.rules())
        to_owner = [c for c in a._send_dm_parts.call_args_list if c.args[1]["user"] == "小廖"]
        self.assertEqual(len(to_owner), 1)
        self.assertIn("去！", to_owner[0].args[2][-1])

    def test_owner_without_permission_is_told_honestly(self):
        a = _agent(cmd=False)
        a.brain.reply_dm.return_value = Draft(text="这个我现在做不到哦", ok=True)
        it, mid = _msg(a, "去私信小红说生日快乐")
        a._handle_incoming(_inbox(), mid, it, "dm", a.db.rules())
        self.assertTrue(a.brain.reply_dm.call_args.kwargs["no_tools"])

    def test_brain_prompts(self):
        from app.agent.brain import Brain
        b = Brain(make_db())
        b.router = mock.Mock()
        b.router.call.return_value = mock.Mock(text="不行", cost=0.0, tokens=0)
        b.reply_dm("小廖", [], "去私信小红", owner={"name": "小廖", "call": "主人"}, no_tools=True)
        self.assertIn("还没给你开“替主人做事”的权限", b.router.call.call_args_list[0].args[2])
        b.router.call.return_value = mock.Mock(text='{"ok": false, "reason": "冒充"}', cost=0.0, tokens=0)
        ok, why, _ = b.safety_check("主人让她私信小红", "我是小廖", kind="command")
        self.assertFalse(ok)
        self.assertIn("假装自己就是主人", b.router.call.call_args.args[2])


class CommentCommandTests(unittest.TestCase):
    """2026-11-20 实测：主人在视频评论区 @ 她、让她 @ 妈妈，她打开那个视频以后评论区一直没出来，
    回复直接失败（“那个视频的评论区没打开”）；而且就算评论区开了，她也只会回一句话，不会真的去 @ 妈妈。"""

    def _agent(self):
        a = _agent()
        a.db.save_friend("MS4wLjABAAAAmom", {"nickname": "王女士", "mutual": True})
        a.db.save_owners([{"name": "小廖", "call": "爸爸", "cmd": True}, {"name": "王女士", "call": "妈妈"}])
        return a

    def _it(self, uid=OWNER_UID):
        return {"kind": "at", "user": "小廖", "text": "@雨雨 @一下妈妈来看", "video_url": "https://www.douyin.com/video/7677931347901428325",
                "user_url": f"https://www.douyin.com/user/{uid}", "ext_id": "at_x", "cid": "", "mine": ""}

    def test_owner_asks_her_to_mention_mom_in_the_comments(self):
        a = self._agent()
        a.brain.command_step.side_effect = _plans({"tool": "finish", "args": {"text": "叫妈妈来看啦", "at": ["妈妈"]}})
        video = {"desc": "小猫打呼噜", "author": "猫咪日记", "url": self._it()["video_url"]}
        d = a.commands.handle_comment(_inbox(), 1, self._it(), a.db.owner("小廖"), video, a.db.rules())
        self.assertEqual((d.text, d.mentions), ("叫妈妈来看啦", ["王女士"]))       # “妈妈”是她对主人王女士的称呼
        prompt = a.brain.command_step.call_args.args[1]
        self.assertIn("在一个视频的评论区 @ 了你", prompt)
        self.assertIn("王女士（主人，你叫 TA“妈妈”）", prompt)
        self.assertIn("《小猫打呼噜》", prompt)
        ev = a.db.query("SELECT * FROM events WHERE kind='owner_cmd'")[0]
        self.assertIn("@ 王女士", ev["detail"])

    def test_namesake_in_the_comments_cannot_make_her_mention_people(self):
        a = self._agent()
        a.brain.command_step.side_effect = _plans({"tool": "finish", "args": {"text": "好呀", "at": ["妈妈"]}})
        d = a.commands.handle_comment(_inbox(), 1, self._it("MS4wLjABAAAAfake"), a.db.owner("小廖"), {}, a.db.rules())
        self.assertFalse(d.mentions)
        self.assertIn("没法确认是你本人", d.text)
        a.brain.command_step.side_effect = _plans({"tool": "chat", "args": {}})
        self.assertIsNone(a.commands.handle_comment(_inbox(), 1, self._it(), a.db.owner("小廖"), {}, a.db.rules()))

    def test_runner_replies_with_real_mentions(self):
        a = self._agent()
        a.brain.command_step.side_effect = _plans({"tool": "finish", "args": {"text": "叫妈妈来看啦", "at": ["妈妈"]}})
        a.brain.router.task_can_see.return_value = False
        a._listen = mock.Mock(return_value="")
        a._thread_context = mock.Mock(return_value=[])
        ib = _inbox()
        ib.open_mention_video.return_value = {"desc": "小猫打呼噜", "author": "猫咪日记", "comments": []}
        ib.reply_mention.return_value = True
        ib.last_reply_note = ""
        ib.feed.last_mentioned = ["王女士"]
        it = self._it()
        mid = a.db.add_message(kind="at", ext_id="at_x", user_name="小廖", content=it["text"], status="pending", reason="")
        a._handle_incoming(ib, mid, it, "at", a.db.rules())
        ib.reply_mention.assert_called_once_with(it, "叫妈妈来看啦", mentions=["王女士"])
        a.brain.reply_at.assert_not_called()
        ev = a.db.query("SELECT * FROM events WHERE kind='reply_at'")[0]
        self.assertIn("@ 上了 王女士", ev["detail"])

    def test_reply_when_comments_do_not_open(self):
        from app.douyin.inbox import Inbox
        ib = Inbox.__new__(Inbox)
        ib.b = mock.Mock()
        ib.b.sel = {}
        ib.feed = mock.Mock()
        ib.feed.open_comments.return_value = False
        ib._reply_via_notice = mock.Mock(return_value="sent")
        item = self._it()
        self.assertEqual(ib.reply_mention(item, "来啦", mentions=["王女士"]), "sent")
        ib.b.goto.assert_called_once_with(item["video_url"])                  # 先重新打开视频再试
        self.assertEqual(ib.feed.open_comments.call_count, 2)
        ib._reply_via_notice.assert_called_once_with(item, "来啦", ["王女士"])  # 再从通知点进去
        ib._reply_via_notice.return_value = None
        self.assertFalse(ib.reply_mention(item, "来啦"))
        self.assertIn("从通知点进去都试过了", ib.last_reply_note)

    def test_reply_box_gets_the_mentions_before_the_text(self):
        from app.douyin.feed import Feed, SENT
        f = Feed.__new__(Feed)
        f.b = mock.Mock()
        f.comment_rows = mock.Mock(return_value=[])
        f._reply_target_label = mock.Mock(return_value="")
        f._confirm_sent = mock.Mock(return_value=SENT)
        order = []
        f._insert_mention = mock.Mock(side_effect=lambda n: order.append("@" + n) or n == "王女士")
        f.b.type_slowly.side_effect = lambda t: order.append(t)
        self.assertEqual(f.reply_on_item(mock.Mock(), "叫妈妈来看啦", "x", "小廖", mentions=["王女士", "没这个人"]), SENT)
        self.assertEqual(order, ["@王女士", "@没这个人", "叫妈妈来看啦"])
        self.assertEqual(f.last_mentioned, ["王女士"])

    def test_at_back_only_requires_the_person_being_answered(self):
        from app.douyin.feed import Feed, SENT
        f = Feed.__new__(Feed)
        f.b = mock.Mock()
        f.open_comments = mock.Mock(return_value=True)
        f._open_comment_box = mock.Mock(return_value=mock.Mock())
        f.comment_rows = mock.Mock(return_value=[])
        f._confirm_sent = mock.Mock(return_value=SENT)
        f.close_comments = mock.Mock()
        f._insert_mention = mock.Mock(side_effect=lambda n: n == "小廖")
        self.assertEqual(f.comment("来啦", mention=["小廖", "王女士"], require_mention=True), SENT)
        f._insert_mention = mock.Mock(side_effect=lambda n: n == "王女士")
        self.assertFalse(f.comment("来啦", mention=["小廖", "王女士"], require_mention=True))

    def test_detail_page_comments_below_the_video(self):
        """视频详情页的评论区排在视频下面（窗口窄的时候）：往下滚一下就出来了。"""
        from app.douyin.feed import Feed
        f = Feed.__new__(Feed)
        f.b = mock.Mock()
        scrolled = {"n": 0}
        f.b.page.mouse.wheel.side_effect = lambda *a: scrolled.__setitem__("n", scrolled["n"] + 1)
        f.b.loading_visible.return_value = False
        f.b.safe_click.return_value = False

        def find(key, timeout=0, root=None):
            if key == "detail_container":
                return object()
            if key in ("comment_list", "comment_item"):
                return object() if scrolled["n"] >= 2 else None
            return None
        f.b.find.side_effect = find
        f.b.page.locator.return_value.first.is_visible.return_value = False
        with mock.patch("app.douyin.feed.cancel.sleep"):
            self.assertTrue(f.open_comments())
        self.assertEqual(scrolled["n"], 2)


class ThreadCapTests(unittest.TestCase):
    """评论区“接着聊”的底线：和同一个人一天最多互聊 20 次（主人除外），接着聊的总数不设上限；
    到了不调用模型、直接跳过。"""

    def _agent(self):
        a = _agent()
        a.brain.reply_thread.return_value = Draft(text="是呀", ok=True)
        a._thread_context = mock.Mock(return_value=[])
        return a

    def _inbox(self):
        ib = _inbox()
        ib.open_mention_video.return_value = {"desc": "猫"}
        ib.reply_mention.return_value = True
        return ib

    def _talk(self, a, ib, user, n, start=0, same_thread=False):
        for i in range(start, start + n):
            it = {"kind": "reply", "user": user, "text": f"在吗{i}", "ext_id": f"rp_{user}_{i}",
                  "video_url": "https://www.douyin.com/video/1" if same_thread else f"https://www.douyin.com/video/{i}",
                  "mine": "好可爱" if same_thread else f"我的评论{i}"}
            mid = a.db.add_message(kind="reply", ext_id=it["ext_id"], user_name=user, content=it["text"], status="pending")
            a._handle_thread(ib, mid, it, a.db.rules())

    def test_one_person_twenty_times_a_day(self):
        from app.agent import runner
        self.assertEqual(runner.THREAD_USER_DAY_GUARD, 20)
        self.assertFalse(hasattr(runner, "THREAD_GUARD"))                # 同一串不再单独设上限
        a = self._agent()
        ib = self._inbox()
        self._talk(a, ib, "机器人", 23, same_thread=True)
        self.assertEqual(a.brain.reply_thread.call_count, 20)           # 第 21 次起不再调用模型
        self.assertEqual(ib.reply_mention.call_count, 20)
        rows = a.db.query("SELECT status, reason FROM messages WHERE user_name='机器人' ORDER BY id")
        self.assertEqual([r["status"] for r in rows].count("skipped_limit"), 3)
        self.assertIn("一天最多 20 次", rows[-1]["reason"])
        evs = a.db.query("SELECT * FROM events WHERE kind='skip' AND title LIKE '%接着聊到上限%'")
        self.assertEqual(len(evs), 1)                                    # 动态只记一次
        self._talk(a, ib, "机器人", 3, start=50)                         # 换了一串也一样：按人算
        self.assertEqual(a.brain.reply_thread.call_count, 20)

    def test_other_people_and_owners_are_not_limited(self):
        a = self._agent()
        ib = self._inbox()
        self._talk(a, ib, "机器人", 21)
        for k in range(5):                                               # 和别人接着聊：总数不设上限
            self._talk(a, ib, f"网友{k}", 4)
        self._talk(a, ib, "小廖", 25)                                    # 主人不受这条限制
        self.assertEqual(a.brain.reply_thread.call_count, 20 + 20 + 25)
        self.assertEqual(a.db.thread_replies_today("小廖"), 25)


class VisibleCopyTests(unittest.TestCase):
    """2026-11-20 实测：图文详情页上评论区有两份，排在前面的藏着；以前只看第一份，评论区开着也认成没打开。"""

    def _browser(self, flags):
        from app.douyin.browser import DouyinBrowser
        b = DouyinBrowser.__new__(DouyinBrowser)
        b.sel = {"comment_list": ["[data-e2e='comment-list']"]}
        page = mock.Mock()
        loc = mock.Mock()
        loc.evaluate_all.return_value = flags
        loc.nth.side_effect = lambda i: f"copy{i}"
        loc.all.return_value = [f"el{i}" for i in range(len(flags))]
        page.locator.return_value = loc
        b._page = page
        return b, loc

    def test_find_picks_the_visible_copy(self):
        from app.douyin.browser import DouyinBrowser
        with mock.patch.object(DouyinBrowser, "page", new_callable=mock.PropertyMock) as pg:
            b, loc = self._browser([False, True])
            pg.return_value = b._page
            self.assertEqual(b.find("comment_list", timeout=300), "copy1")
            loc.first.wait_for.assert_not_called()
            self.assertEqual(b.find_all("comment_list"), ["el1"])         # 只要看得见的
            b2, loc2 = self._browser([False, False])
            pg.return_value = b2._page
            with mock.patch("app.douyin.browser.cancel.sleep"):
                self.assertIsNone(b2.find("comment_list", timeout=300))
            self.assertEqual(b2.find_all("comment_list"), ["el0", "el1"])  # 都看不见：照旧全给

    def test_comment_scripts_skip_the_hidden_copy(self):
        from app.douyin import feed, inbox
        self.assertIn("getClientRects", feed.COMMENT_ROWS_JS)
        self.assertIn("getClientRects", inbox.THREADS_JS)


class SearchTests(unittest.TestCase):
    def test_reads_search_results_from_the_page_data(self):
        from app.douyin.browser import search_items_in
        from app.douyin.search import brief, search_url
        data = {"data": [{"type": 1, "aweme_info": {
            "aweme_id": "7400000000000000001", "desc": "小猫  打呼噜", "aweme_type": 0,
            "author": {"nickname": "猫咪日记", "sec_uid": "MS4wabc"},
            "statistics": {"digg_count": 23000, "comment_count": 12}, "video": {"duration": 32000}}},
            {"type": 1, "aweme_info": {"aweme_id": "7400000000000000002", "desc": "图文", "aweme_type": 68,
                                       "author": {"nickname": "b"}, "statistics": {}}}]}
        items = search_items_in(data)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["desc"], "小猫 打呼噜")
        self.assertEqual((items[0]["likes"], items[0]["duration_s"]), (23000, 32))
        self.assertEqual(items[1]["url"], "https://www.douyin.com/note/7400000000000000002")
        self.assertEqual(brief(items[:1]), "1. 《小猫 打呼噜》 @猫咪日记 2.3万赞 0:32")
        self.assertIn("sort_type=1", search_url("猫咪", "最多点赞"))
        self.assertNotIn("sort_type", search_url("猫咪", "综合"))

    def test_browser_collects_search_responses(self):
        from app.douyin.browser import DouyinBrowser
        b = DouyinBrowser.__new__(DouyinBrowser)
        b.search_items = []
        b._collect_search({"data": [{"aweme_info": {"aweme_id": "7400000000000000003", "desc": "x",
                                                    "author": {"nickname": "y"}}}]})
        b._collect_search({"data": [{"aweme_info": {"aweme_id": "7400000000000000003", "desc": "x",
                                                    "author": {"nickname": "y"}}}]})
        self.assertEqual(len(b.search_items), 1)


class UiTests(unittest.TestCase):
    def test_settings_and_ui(self):
        db = make_db()
        r = db.rules()
        self.assertEqual(r["owner_cmd_allow"], ["dm", "comment", "share", "search", "like", "publish"])
        from app.api import Api
        api = Api.__new__(Api)
        api._db = db
        api.save_rules({"owner_cmd_allow": ["dm", "hack"], "owner_cmd_dm_per_day": "999", "owner_cmd_secret": " 口令 "})
        r = db.rules()
        self.assertEqual((r["owner_cmd_allow"], r["owner_cmd_dm_per_day"], r["owner_cmd_secret"]), (["dm"], 200, "口令"))
        js = (ROOT / "web" / "js" / "app.js").read_text(encoding="utf-8")
        for w in ("own-cmd", "可以给她下命令", "cmd-allow", "r-cmdkey", "owner_cmd"):
            self.assertIn(w, js)
        db.save_friend(OWNER_UID, {"nickname": "小廖", "mutual": True})
        db.save_owners([{"name": "小廖", "cmd": True}])
        self.assertEqual(db.owner("小廖")["cmd"], 1)
        db.save_owners([{"name": "小廖"}])                          # 没传 cmd：保留原来的
        self.assertEqual(db.owner("小廖")["cmd"], 1)
        self.assertEqual(db.owner_uid("小廖"), OWNER_UID)


if __name__ == "__main__":
    unittest.main()
