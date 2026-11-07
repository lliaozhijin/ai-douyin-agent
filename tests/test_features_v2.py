"""0.2.0 新功能的测试：心情、评论接着聊、@ 互关好友、发作品、桌宠。不需要联网、不需要浏览器。"""
from __future__ import annotations

import json
import random
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# “让她休息一下”的急停开关是全局的：每个测试前后都复位，免得一个测试按下的开关影响后面的测试。
# 直接包住 TestCase.run，用 unittest（打包脚本）和 pytest 跑都生效。
from app import cancel as _cancel  # noqa: E402


def _reset_cancel():
    _cancel.STOP.clear()
    _cancel._agent_thread = None


if not getattr(unittest.TestCase, "_cancel_reset", False):
    _orig_run = unittest.TestCase.run

    def _run_with_reset(self, result=None):
        _reset_cancel()
        try:
            return _orig_run(self, result)
        finally:
            _reset_cancel()

    unittest.TestCase.run = _run_with_reset
    unittest.TestCase._cancel_reset = True

from PIL import Image  # noqa: E402

from app.agent.brain import Brain  # noqa: E402
from app.agent.mood import MOODS, MoodEngine  # noqa: E402
from app.agent.persona import system_prompt  # noqa: E402
from app.db import Database  # noqa: E402
from app.llm.client import LLMResult  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RIG = ROOT / "assets" / "pet" / "default"


def make_db() -> Database:
    return Database(Path(tempfile.mkdtemp()) / "t.db")


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self):
        return self.t


class MoodTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.clock = Clock(datetime(2026, 11, 6, 15, 0).timestamp())
        self.m = MoodEngine(self.db, clock=self.clock, rnd=random.Random(1))

    def test_starts_with_a_mood_and_prompt(self):
        cur = self.m.current()
        self.assertIn(cur["key"], MOODS)
        self.assertIn("此刻的心情", self.m.prompt())
        self.assertIn("不对任何人发火", self.m.prompt())

    def test_drag_makes_grumpy_when_temper_high(self):
        p = self.db.persona()
        p["traits"]["temper"] = 90
        self.db.set_json("persona", p)
        for _ in range(3):
            cur = self.m.event("drag")
        self.assertEqual(cur["key"], "grumpy")
        self.assertIn("拎来拎去", cur["reason"])

    def test_hostile_makes_sulky_when_gentle(self):
        p = self.db.persona()
        p["traits"]["temper"] = 10
        self.db.set_json("persona", p)
        for _ in range(3):
            cur = self.m.event("hostile")
        self.assertEqual(cur["key"], "sulky")

    def test_pat_too_much_turns_grumpy(self):
        cur = self.m.event("pat")
        self.assertEqual(cur["key"], "clingy")
        for _ in range(4):
            cur = self.m.event("pat")
        self.assertEqual(cur["key"], "grumpy")
        self.assertIn("一直摸头", cur["reason"])

    def test_mood_decays_back_over_time(self):
        for _ in range(3):
            self.m.event("drag")
        self.clock.t += 6 * 3600
        self.assertNotEqual(self.m.current()["key"], "grumpy")

    def test_night_is_sleepy(self):
        clock = Clock(datetime(2026, 11, 7, 3, 0).timestamp())
        m = MoodEngine(make_db(), clock=clock, rnd=random.Random(1))
        self.assertEqual(m.current()["key"], "sleepy")

    def test_bored_after_many_meh_videos(self):
        for _ in range(7):
            cur = self.m.event("video_meh")
        self.assertEqual(cur["key"], "bored")

    def test_bias_and_brain_system_prompt(self):
        for _ in range(3):
            self.m.event("publish_ok")
        b = self.m.bias()
        self.assertGreater(b["publish"], 0.5)
        brain = Brain(self.db)
        brain.mood = self.m
        self.assertIn("此刻的心情", brain.system)
        self.assertIn("脾气", system_prompt(self.db.persona()))


class ThreadAndFriendDbTests(unittest.TestCase):
    def test_thread_counts_and_lines(self):
        db = make_db()
        th = db.ensure_thread("t1", "https://www.douyin.com/video/1", "这只猫好可爱", "小明")
        self.assertEqual(th["count"], 0)
        db.thread_add("t1", "them", "是吧")
        db.thread_add("t1", "me", "超级可爱")
        self.assertEqual(db.thread("t1")["count"], 1)
        self.assertEqual([x["role"] for x in db.thread_lines("t1")], ["them", "me"])
        db.close_thread("t1")
        self.assertEqual(db.thread("t1")["closed"], 1)

    def test_friends_for_at_respects_choice_and_daily_cap(self):
        db = make_db()
        db.save_friend("u1", {"nickname": "小明", "mutual": True})
        db.save_friend("u2", {"nickname": "小红", "mutual": False})
        db.set_friend_choice("u1", True, "猫")
        db.set_friend_choice("u2", True, "")
        self.assertEqual([f["nickname"] for f in db.friends_for_at(1)], ["小明"])  # 没互关的不算
        db.mark_friend_at("u1")
        self.assertEqual(db.friends_for_at(1), [])
        self.assertEqual(len(db.friends_for_at(2)), 1)

    def test_match_mine(self):
        from app.douyin.inbox import match_mine
        self.assertEqual(match_mine("回复了你的评论 这只猫好可爱呀我也想养", ["这只猫好可爱呀我也想养一只", "别的"]),
                         "这只猫好可爱呀我也想养一只")
        self.assertEqual(match_mine("回复了你的评论 别的东西", ["这只猫好可爱"]), "")


def fake_router(text: str):
    r = mock.Mock()
    r.call.return_value = LLMResult(text=text, cost=0.001)
    r.task_can_see.return_value = False
    return r


class BrainV2Tests(unittest.TestCase):
    def test_decide_picks_friend_and_strips_at(self):
        brain = Brain(make_db())
        brain.router = fake_router('{"interest": 9, "like": true, "comment": "@小明 你看这只猫和你家的好像", '
                                   '"at_friend": 1, "reason": "像"}')
        friends = [{"uid": "u1", "nickname": "小明", "tags": "猫"}]
        d = brain.decide({"desc": "猫", "author": "a", "comments": []}, [], True, True, friends=friends)
        self.assertEqual(d["at_friend"]["uid"], "u1")
        self.assertEqual(d["comment"], "你看这只猫和你家的好像")
        prompt = brain.router.call.call_args.args[2]
        self.assertIn("小明（喜欢：猫）", prompt)

    def test_decide_without_friends_has_no_at(self):
        brain = Brain(make_db())
        brain.router = fake_router('{"interest": 9, "like": true, "comment": "好看", "at_friend": 1}')
        d = brain.decide({"desc": "猫", "author": "a", "comments": []}, [], True, True)
        self.assertIsNone(d["at_friend"])

    def test_reply_thread_last_closes(self):
        brain = Brain(make_db())
        brain.router = fake_router("聊得好开心～")
        brain.safety_check = mock.Mock(return_value=(True, "", 0.0))
        d = brain.reply_thread({"desc": "猫"}, "小明", "哈哈", [{"role": "me", "content": "好可爱"}], last=True)
        self.assertTrue(d.ok)
        self.assertIn("最后一次回复", brain.router.call.call_args.args[2])

    def test_draft_post_parses(self):
        brain = Brain(make_db())
        brain.router = fake_router(json.dumps({
            "want": True, "why": "今天好开心想分享", "title": "今天的小确幸", "text": "刷到好多小猫！",
            "tags": ["#萌宠", "日常"], "cards": [{"title": "猫猫", "lines": ["圆滚滚", "好可爱"]}],
            "image_prompt": "一只橘猫"}, ensure_ascii=False))
        d = brain.draft_post(["猫猫视频"], "开心")
        self.assertTrue(d["want"])
        self.assertEqual(d["tags"], ["萌宠", "日常"])
        self.assertEqual(d["cards"][0]["lines"], ["圆滚滚", "好可爱"])


class PublishTests(unittest.TestCase):
    def test_drafts_crud_and_today(self):
        db = make_db()
        did = db.add_draft(status="wish", title="t", text="x", tags=["a"], media=[{"name": "c.jpg", "kind": "image"}],
                           source="auto")
        d = db.draft(did)
        self.assertEqual(d["tags"], ["a"])
        self.assertEqual(db.drafts_today(), 1)
        db.update_draft(did, status="discarded")
        self.assertEqual(db.drafts_today(), 0)

    def test_publish_slots_inside_window(self):
        from app.agent.runner import publish_slots
        now = datetime(2026, 11, 6, 8, 0).timestamp()
        slots = publish_slots({"publish_start": "11:00", "publish_end": "21:00"}, 2, now)
        self.assertEqual(len(slots), 2)
        for t in slots:
            self.assertTrue(11 <= datetime.fromtimestamp(t).hour < 21)
        late = datetime(2026, 11, 6, 22, 0).timestamp()
        self.assertEqual(publish_slots({"publish_start": "11:00", "publish_end": "21:00"}, 1, late), [])

    def test_cards_render(self):
        from app.publish.cards import render_post
        out = Path(tempfile.mkdtemp())
        names = render_post("今天的小确幸", "刷到好多小猫！(｡･ω･｡)ﾉ♡", [{"title": "猫猫", "lines": ["圆滚滚"]}],
                            "雨雨", out)
        self.assertEqual(len(names), 2)
        im = Image.open(out / names[0])
        self.assertEqual(im.size, (1080, 1440))

    def test_parse_cards(self):
        from app.api import _parse_cards
        cards = _parse_cards("猫猫\n圆滚滚\n好可爱\n\n第二张\n一句话")
        self.assertEqual(cards, [{"title": "猫猫", "lines": ["圆滚滚", "好可爱"]},
                                 {"title": "第二张", "lines": ["一句话"]}])

    def test_publish_refuses_without_ai_declaration(self):
        from app.douyin import publish as pub

        b = mock.Mock()
        b.sel = {"creator_upload_image": "u", "creator_login_words": [], "creator_file_input": ["input"]}
        b.page.url = "https://creator.douyin.com/x"
        b.page.get_by_text.return_value.first.is_visible.return_value = False
        b.in_new_tab.return_value.__enter__ = mock.Mock(return_value=None)
        b.in_new_tab.return_value.__exit__ = mock.Mock(return_value=False)
        b.find.return_value = mock.Mock()
        b.page.locator.return_value.first.is_visible.return_value = False  # 页面上没有“内容由AI生成”
        b.dump.return_value = "x"
        with self.assertRaises(pub.PublishError) as ctx:
            pub.publish(b, {"kind": "image", "title": "t", "text": "x", "tags": []}, [Path("a.jpg")])
        self.assertIn("内容由AI生成", str(ctx.exception))
        b.find.assert_any_call("creator_title", timeout=60000)


class RunnerV2Tests(unittest.TestCase):
    def make_agent(self):
        from app.agent.runner import Agent
        agent = Agent(make_db())
        agent.brain = mock.Mock()
        agent.mood = mock.Mock()
        agent.mood.bias.return_value = {"threshold": 0, "dwell": 1.0, "publish": 1.0}
        return agent

    def test_thread_stops_at_cap(self):
        agent = self.make_agent()
        db = agent.db
        rules = db.rules()
        rules["thread_max_replies"] = 2
        it = {"kind": "reply", "user": "小明", "text": "哈哈", "video_url": "https://www.douyin.com/video/1",
              "mine": "好可爱", "ext_id": "rp_1"}
        inbox = mock.Mock()
        inbox.open_mention_video.return_value = {"desc": "猫"}
        inbox.reply_mention.return_value = True
        from app.agent.brain import Draft
        agent.brain.reply_thread.return_value = Draft(text="是呀", ok=True)
        for i in range(3):
            mid = db.add_message(kind="reply", ext_id=f"rp_{i}", user_name="小明", content="哈哈", status="pending")
            agent._handle_thread(inbox, mid, dict(it, ext_id=f"rp_{i}"), rules)
        self.assertEqual(inbox.reply_mention.call_count, 2)
        last = agent.brain.reply_thread.call_args_list[-1]
        self.assertTrue(last.args[4])  # 第 2 次是最后一次，要收尾
        statuses = [m["status"] for m in db.messages("all")]
        self.assertIn("skipped_limit", statuses)

    def test_publish_command_runs_after_ui_marks_publishing(self):
        agent = self.make_agent()
        agent.brain.check_post.return_value = (True, "", 0.0)
        agent.logged_in = True
        folder = Path(tempfile.mkdtemp())
        (folder / "1").mkdir()
        Image.new("RGB", (30, 40)).save(folder / "1" / "c.jpg")
        did = agent.db.add_draft(status="wish", title="标题", text="正文", media=[{"name": "c.jpg", "kind": "image"}])
        agent.db.update_draft(did, status="publishing")  # 界面点了“确认发布”
        with mock.patch("app.agent.runner.DRAFTS_DIR", folder), \
                mock.patch("app.agent.runner.publish", return_value="发布成功") as pub, \
                mock.patch.object(agent, "_ensure_browser", return_value=mock.Mock()):
            agent._cmd_publish(did=did)
        pub.assert_called_once()
        self.assertEqual(agent.db.draft(did)["status"], "published")
        self.assertEqual(agent.db.usage()["publishes"], 1)

    def test_stuck_publishing_is_reset_on_start(self):
        from app.agent.runner import Agent
        db = make_db()
        did = db.add_draft(status="publishing", title="t")
        Agent(db)
        self.assertEqual(db.draft(did)["status"], "failed")

    def test_make_draft_creates_wish_with_cards(self):
        agent = self.make_agent()
        agent.mood.current.return_value = {"key": "happy", "label": "开心", "reason": "x"}
        agent.brain.draft_post.return_value = {"want": True, "why": "想分享", "title": "标题", "text": "正文",
                                               "tags": ["a"], "cards": [{"title": "c", "lines": ["l"]}],
                                               "image_prompt": "", "cost": 0}
        with mock.patch("app.agent.runner.DRAFTS_DIR", Path(tempfile.mkdtemp())):
            did = agent._make_draft(force=False)
            d = agent.db.draft(did)
        self.assertEqual(d["status"], "wish")
        self.assertEqual(len(d["media"]), 2)  # 封面 + 1 张卡片
        self.assertTrue(any(e["kind"] == "publish_wish" for e in agent.db.events()))


class WatchAtFriendTests(unittest.TestCase):
    def test_at_friend_comment_posted_with_mention(self):
        from app.agent.runner import Agent
        db = make_db()
        db.save_friend("u1", {"nickname": "小明", "mutual": True})
        db.set_friend_choice("u1", True, "猫")
        agent = Agent(db)
        agent._want_run = True
        agent._sleep = lambda s: agent._want_run
        agent.brain = mock.Mock()
        agent.brain.router.task_can_see.return_value = False
        agent.brain.triage.return_value = {"interest": 9, "reason": "猫", "cost": 0.0}
        agent.brain.safety_check.return_value = (True, "", 0.0)
        agent.brain.decide.return_value = {"interest": 9, "like": False, "comment": "你家咪咪也这样吧",
                                           "at_friend": {"uid": "u1", "nickname": "小明"}, "reason": "", "cost": 0.0}
        with mock.patch("app.agent.runner.Feed") as FeedCls:
            feed = FeedCls.return_value
            feed.read_current.return_value = {"video_id": "v9", "url": "u", "author": "a", "desc": "三只小猫",
                                              "comments": []}
            feed.capture_frames.return_value = []
            feed.comment.return_value = True
            feed.last_mention_ok = True
            agent._watch_one(mock.Mock(), db.rules())
        feed.comment.assert_called_once_with("你家咪咪也这样吧", mention="小明")
        self.assertEqual(db.usage()["at_friends"], 1)
        self.assertEqual(db.friends_for_at(1), [])  # 今天已经 @ 过了
        self.assertEqual(agent.brain.decide.call_args.kwargs["friends"][0]["nickname"], "小明")
        row = db.query("SELECT comment FROM videos WHERE video_id='v9'")[0]
        self.assertEqual(row["comment"], "@小明 你家咪咪也这样吧")


class PetTests(unittest.TestCase):
    def test_mat_inverse(self):
        from app.pet.gfx import Mat
        m = Mat().translate(10, 5).rotate(33).scale(1.5, 0.7)
        x, y = m.apply(3, -4)
        bx, by = m.inverse().apply(x, y)
        self.assertAlmostEqual(bx, 3, places=6)
        self.assertAlmostEqual(by, -4, places=6)

    def test_default_rig_is_the_girl_and_loads(self):
        from app.pet.rig import Rig, load
        r = Rig(DEFAULT_RIG)
        for name in ("head", "body", "leg_l", "leg_r", "arm_l_straight", "arm_r_straight"):
            self.assertIn(name, r.src)
        self.assertEqual(load("duck").folder.name, "default")  # 小黄鸭已经去掉：旧设置自动用默认形象
        self.assertEqual(load("custom").folder.name, "default")  # 没上传过时退回默认

    def test_bubble_colors(self):
        from app.pet import gfx
        sp, _, _ = gfx.bubble("你好呀", 3, 1.0, colors=("#202040", "#FFFFFF", "#FFDD00"))
        px = sp.img.convert("RGBA").getpixel((int(sp.ax) + 60, int(sp.ay) - 30))
        self.assertEqual(px[3], 255)  # 对话框里面完全不透明
        self.assertLess(sum(px[:3]), 250)  # 用了深色底

    def test_render_frames_not_empty(self):
        from app.pet import engine as E
        from app.pet.render import render
        from app.pet.rig import Rig
        r = Rig(DEFAULT_RIG)
        pet = E.Pet(rnd=random.Random(3))
        for act in (E.IDLE, E.DANCE2, E.PHONE, E.SIGN, E.LIE, E.SULK):
            pet.set_act(act, 5)
            for _ in range(20):
                pet.update(0.033, 0)
            cv = render(pet, r, 1.0)
            self.assertIsNotNone(cv.img.getbbox(), act)

    def test_engine_syncs_with_activity(self):
        from app.pet import engine as E
        pet = E.Pet(rnd=random.Random(1))
        pet.set_mode("watch")
        self.assertEqual(pet.act, E.PHONE)
        pet.on_event({"kind": "like"})
        self.assertEqual(pet.act, E.PHONE_LIKE)
        pet.on_event({"kind": "reply_dm", "text": "小明"})
        self.assertEqual(pet.act, E.TYPE)
        self.assertIn("小明", pet.bubble_text)
        pet.on_event({"kind": "publish_wish", "text": "标题"})
        self.assertEqual(pet.act, E.SIGN)
        self.assertTrue(pet.on_click_sign())
        pet.set_mode("")
        pet.set_act(E.PHONE, 5)
        pet.set_mode("rest")
        self.assertNotIn(pet.act, E.PHONE_ACTS)

    def test_grumpy_idle_sulks(self):
        from app.pet import engine as E
        pet = E.Pet(rnd=random.Random(2), mood_key=lambda: "grumpy")
        pet.hide_enabled = False
        acts = set()
        for _ in range(30):
            pet._next_idle()
            acts.add(pet.act)
        self.assertIn(E.SULK, acts)

    def test_autorig_from_cutout(self):
        from app.pet import autorig
        from app.pet.rig import Rig
        from app.pet.render import render
        from app.pet import engine as E
        img = Image.new("RGBA", (300, 600), (0, 0, 0, 0))
        from PIL import ImageDraw
        d = ImageDraw.Draw(img)
        d.ellipse((60, 20, 240, 200), fill=(250, 220, 90, 255))          # 头
        d.rectangle((100, 200, 200, 420), fill=(240, 200, 80, 255))      # 身体
        d.line((100, 230, 50, 330, 70, 400), fill=(230, 190, 70, 255), width=30)   # 左臂
        d.line((200, 230, 250, 330, 230, 400), fill=(230, 190, 70, 255), width=30)  # 右臂
        d.rectangle((105, 420, 145, 580), fill=(220, 150, 60, 255))      # 左腿
        d.rectangle((155, 420, 195, 580), fill=(220, 150, 60, 255))      # 右腿
        j = autorig.guess_joints(img)
        self.assertEqual(set(j), set(autorig.JOINTS))
        self.assertTrue(150 < j["neck"][1] < 260)
        joints = {"neck": [150, 200], "shoulder_l": [100, 230], "elbow_l": [50, 330], "hand_l": [70, 400],
                  "shoulder_r": [200, 230], "elbow_r": [250, 330], "hand_r": [230, 400],
                  "hip_l": [125, 420], "hip_r": [175, 420]}
        out = Path(tempfile.mkdtemp()) / "rig"
        rig = autorig.build(img, joints, 34, out)
        for name in ("head", "body", "leg_l", "leg_r", "arm_l_up", "arm_l_lo", "arm_l_straight", "arm_r_straight"):
            self.assertIn(name, rig["parts"], name)
        r = Rig(out)
        pet = E.Pet(fx=r.fx, fy=r.fy, peek_depth=r.peek_depth, rnd=random.Random(1))
        pet.start_dance(0)
        for _ in range(30):
            pet.update(0.033, 0)
        self.assertIsNotNone(render(pet, r, 0.8).img.getbbox())
        # 手臂原来挡住的身体被补上了：身体图在原来左臂经过的地方（身体范围内）不透明
        body = Image.open(out / "body.png")
        bx, by = rig["parts"]["body"]
        self.assertGreater(body.getpixel((110 - bx, 300 - by))[3], 200)

    def test_flood_matting_plain_background(self):
        from app.pet import matting
        img = Image.new("RGB", (200, 300), (250, 250, 250))
        from PIL import ImageDraw
        ImageDraw.Draw(img).ellipse((50, 50, 150, 250), fill=(240, 120, 160))
        cut, how = matting.cutout(img, use_model=False)
        self.assertEqual(how, "去掉纯色背景")
        self.assertLess(cut.getpixel((2, 2))[3], 10)
        self.assertGreater(cut.getpixel((cut.width // 2, cut.height // 2))[3], 240)

    def test_bus(self):
        from app.pet import bus
        bus.drain()
        bus.emit("like")
        bus.emit("comment", "你好")
        evs = bus.drain()
        self.assertEqual([e["kind"] for e in evs], ["like", "comment"])
        self.assertEqual(bus.drain(), [])

    def test_host_reloads_rig_from_settings(self):
        from app.pet import engine as E
        from app.pet.host import PetHost
        db = make_db()
        h = PetHost(db, mock.Mock())
        h.settings = {"rig": "default"}
        h.pet = E.Pet()
        h.win = mock.Mock()
        db.set_json("pet", {"rig": "custom"})  # 界面换了形象
        with mock.patch("app.pet.host.load_rig") as lr:
            lr.return_value = mock.Mock(fx=1.2, fy=1.1, peek_depth=100)
            h.send("rig")
            h._commands()
        lr.assert_called_once_with("custom")
        self.assertEqual(h.pet.fy, 1.1)

    def test_host_not_windows(self):
        from app.pet.host import PetHost
        h = PetHost(make_db(), mock.Mock())
        with mock.patch("app.pet.window.IS_WINDOWS", False):
            h.start()
        self.assertIn("Windows", h.error)
        self.assertFalse(h.status()["running"])


if __name__ == "__main__":
    unittest.main()


class PublishQueueTests(unittest.TestCase):
    """点了“确认发布”之后，不会被回 @ / 私信这种长任务一直压着。"""

    def test_run_urgent_dispatches_publish_and_keeps_others(self):
        import queue
        from app.agent.runner import Agent

        a = Agent.__new__(Agent)
        a.q = queue.Queue()
        done = []
        a._dispatch = lambda cmd, kw: done.append(cmd)
        for c in (("check_inbox", {}), ("publish", {"did": 1}), ("sync_bio", {}), ("pause", {})):
            a.q.put(c)
        a._run_urgent()
        self.assertEqual(done, ["publish", "pause"])
        left = []
        while not a.q.empty():
            left.append(a.q.get_nowait()[0])
        self.assertEqual(left, ["check_inbox", "sync_bio"])


class PublishPageLoadTests(unittest.TestCase):
    def _b(self):
        from unittest import mock
        from app.douyin.browser import DouyinBrowser

        b = DouyinBrowser.__new__(DouyinBrowser)
        b.sel = {"creator_file_input": ["input[type='file']"]}
        b.page = mock.Mock(url="https://creator.douyin.com/x")
        b.check_captcha = lambda: None
        return b

    def test_wait_any_reloads_once_then_finds(self):
        from unittest import mock

        b = self._b()
        loc = mock.Mock()
        state = {"reloaded": False}
        loc.count.side_effect = lambda: 1 if state["reloaded"] else 0
        b.page.locator.return_value.first = loc
        b.page.reload.side_effect = lambda **k: state.update(reloaded=True)
        b.page.get_by_text.return_value.first.is_visible.return_value = True
        with mock.patch("app.douyin.browser.cancel.sleep"):
            got = b.wait_any("creator_file_input", timeout=0.6, state="attached")
        self.assertTrue(b.page.reload.called)
        self.assertIs(got, loc)

    def test_wait_any_gives_up(self):
        from unittest import mock

        b = self._b()
        loc = mock.Mock()
        loc.count.return_value = 0
        b.page.locator.return_value.first = loc
        b.page.get_by_text.return_value.first.is_visible.return_value = False
        with mock.patch("app.douyin.browser.cancel.sleep"):
            self.assertIsNone(b.wait_any("creator_file_input", timeout=0.02, state="attached", reload_once=False))
        self.assertFalse(b.page.reload.called)


class CollectTests(unittest.TestCase):
    """收藏：她自己决定收不收；已经收藏过的不会被点成取消收藏。"""

    def _btn(self, states):
        from unittest import mock

        btn = mock.Mock()
        btn.get_attribute.side_effect = lambda *a, **k: states.pop(0) if len(states) > 1 else states[0]
        return btn

    def _feed(self, btn):
        from unittest import mock
        from app.douyin.feed import Feed

        b = mock.Mock()
        b.find.return_value = btn
        b.safe_click.return_value = True
        b.page.get_by_text.return_value.first.is_visible.return_value = False  # 没有“收藏成功”提示
        return Feed(b), b

    def test_collects_when_not_collected(self):
        f, b = self._feed(self._btn(["video-player-no-collect", "video-player-is-collect"]))
        self.assertTrue(f.collect())
        b.safe_click.assert_called_once()

    def test_never_uncollects(self):
        f, b = self._feed(self._btn(["video-player-is-collect"]))
        self.assertFalse(f.collect())
        b.safe_click.assert_not_called()

    def test_click_without_state_change_is_failure(self):
        f, _ = self._feed(self._btn(["video-player-no-collect", "video-player-no-collect"]))
        self.assertFalse(f.collect())

    def test_toast_counts_as_collected(self):
        f, b = self._feed(self._btn(["video-player-no-collect", "video-player-no-collect"]))
        b.page.get_by_text.return_value.first.is_visible.return_value = True  # 弹了“收藏成功”
        self.assertTrue(f.collect())

    def test_usage_and_rules(self):
        import tempfile
        from pathlib import Path
        from app.db import Database

        db = Database(Path(tempfile.mkdtemp()) / "t.db")
        self.assertIn("collects", db.rules()["limits"])
        self.assertTrue(db.rules()["collect_enabled"])
        db.add_usage("collects", 1)
        self.assertEqual(db.usage()["collects"], 1)
        db.add_video(video_id="1", url="u", author="a", title="菜谱", interest=9, decision="collect",
                     liked=0, collected=1, comment="", reason="想以后照着做", cost=0)
        self.assertEqual(len(db.activity("collects")), 1)

    def test_brain_parses_collect(self):
        from app.agent.brain import _yes

        self.assertTrue(_yes(True))
        self.assertTrue(_yes("true"))
        self.assertFalse(_yes("false"))
        self.assertFalse(_yes(None))


class PetFaceTests(unittest.TestCase):
    """默认形象的眨眼、说话嘴型、辫子 / 呆毛 / 裙摆摆动。"""

    def _pet(self, mood="calm"):
        import random
        from app.pet import engine as E
        from app.pet.rig import load

        rig = load("default")
        pet = E.Pet(fx=rig.fx, fy=rig.fy, rnd=random.Random(3))
        pet.mood_key = lambda: mood
        pet.chatter = False
        pet.hide_enabled = False
        return E, rig, pet

    def test_default_rig_has_face_and_sway_parts(self):
        from app.pet.render import render

        E, rig, pet = self._pet()
        for n in ("tail_l", "tail_r", "ahoge", "eyes_closed", "mouth_talk"):
            self.assertIn(n, rig.src, n)
        # 表情只在原图上变化：没有另外画的“开心眼 / 撇嘴”之类
        for n in ("eyes_happy", "eyes_x", "mouth_frown", "mouth_open"):
            self.assertNotIn(n, rig.src, n)
        self.assertEqual(set(rig.sway), {"tail_l", "tail_r", "ahoge"})
        render(pet, rig, 1.0)
        self.assertIn("body~0", rig.sprites)
        self.assertIn("body~3", rig.sprites)

    def test_blinks_now_and_then(self):
        E, rig, pet = self._pet()
        seen = set()
        for _ in range(int(12 / 0.016)):
            pet.update(0.016, 0)
            pet.pose()
            seen.add(pet.face.eyes)
        self.assertIn("closed", seen)
        self.assertIn("open", seen)

    def test_mouth_moves_while_talking(self):
        E, rig, pet = self._pet()
        pet.say("今天刷到好多可爱的小猫咪呀", 5)
        mouths = set()
        for _ in range(40):
            pet.update(0.016, 0)
            pet.pose()
            mouths.add(pet.face.mouth)
        self.assertIn("talk", mouths)
        self.assertIn("smile", mouths)

    def test_face_stays_original_except_blink_and_talk(self):
        E, rig, pet = self._pet("sulky")
        pet.update(0.016, 0)
        self.assertEqual(pet.face.mouth, "smile")   # 心情不好也还是原图的笑嘴
        E, rig, pet = self._pet()
        pet.set_act(E.SLEEP, 30)
        pet.update(0.016, 0)
        self.assertEqual(pet.face.eyes, "closed")
        pet.set_act(E.PAT, 2.6)
        pet.face._blink_in = 99
        pet.update(0.016, 0)
        self.assertEqual(pet.face.eyes, "open")

    def test_hair_swings_when_she_moves(self):
        E, rig, pet = self._pet()
        pet.pose()
        x, v = 0.0, 0.0
        peak = 0.0
        for i in range(120):
            v = v + 30 if i < 25 else 0.0
            x += v * 0.016
            pet.world = (x, 0.0)
            pet.update(0.016, 0)
            pet.pose()
            peak = max(peak, abs(pet.face.tail_l.x))
        self.assertGreater(peak, 4)
        self.assertLess(abs(pet.face.tail_l.x), peak)  # 停下来以后慢慢回去

    def test_custom_rig_without_extra_parts_still_renders(self):
        import json
        import shutil
        import tempfile
        from pathlib import Path
        from app.pet.render import render
        from app.pet.rig import DEFAULT_DIR, Rig

        d = Path(tempfile.mkdtemp())
        rig_json = json.loads((DEFAULT_DIR / "rig.json").read_text(encoding="utf-8"))
        for k in ("sway", "skirt", "face"):
            rig_json.pop(k, None)
        rig_json["parts"] = {k: v for k, v in rig_json["parts"].items()
                             if not k.startswith(("eyes_", "mouth_", "tail_", "ahoge"))}
        for name in rig_json["parts"]:
            shutil.copy(DEFAULT_DIR / f"{name}.png", d / f"{name}.png")
        (d / "rig.json").write_text(json.dumps(rig_json), encoding="utf-8")
        E, _rig, pet = self._pet()
        rig = Rig(d)
        pet.update(0.016, 0)
        self.assertFalse(rig.lively)  # 上传的形象：不加眨眼、辫子这些，保持原来的画法
        render(pet, rig, 1.0)
        self.assertEqual(rig.body_name(0.8), "body")


class HistoryTests(unittest.TestCase):
    """仪表盘一页 10 条、最多 10 页；历史记录按日期 / 类型分页看全部。"""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from app.db import Database

        self.db = Database(Path(tempfile.mkdtemp()) / "t.db")
        for i in range(130):
            self.db.add_event("like" if i % 2 else "skip", f"第 {i} 条", "")
        # 两条旧日期的记录
        self.db.execute("INSERT INTO events(ts, kind, title) VALUES('2026-10-01 09:00:00', 'reply_dm', '旧私信')")
        self.db.execute("INSERT INTO events(ts, kind, title) VALUES('2026-10-01 10:00:00', 'like', '旧点赞')")

    def test_dashboard_pages_capped(self):
        p = self.db.events_page(1, 10, max_pages=10)
        self.assertEqual(len(p["items"]), 10)
        self.assertEqual(p["pages"], 10)
        self.assertEqual(p["items"][0]["title"], "第 129 条")  # 最新的在前
        last = self.db.events_page(99, 10, max_pages=10)
        self.assertEqual(last["page"], 10)
        self.assertEqual(last["items"][-1]["title"], "第 30 条")
        self.assertEqual(p["total"], 132)

    def test_history_by_day_and_group(self):
        from unittest import mock
        from app.api import Api

        api = Api(self.db, mock.Mock())
        r = api.get_history("2026-10-01", "", 1)["data"]
        self.assertEqual(r["total"], 2)
        self.assertEqual([e["title"] for e in r["items"]], ["旧点赞", "旧私信"])
        r = api.get_history("2026-10-01", "reply", 1)["data"]
        self.assertEqual([e["title"] for e in r["items"]], ["旧私信"])
        r = api.get_history("", "interact", 2)["data"]
        self.assertEqual(r["total"], 66)
        self.assertEqual(r["pages"], 4)
        self.assertEqual(r["page"], 2)
        self.assertEqual(r["days"][-1]["day"], "2026-10-01")
        r = api.get_history("bad'; drop", "", 1)["data"]  # 日期格式不对就当“全部日期”
        self.assertEqual(r["day"], "")


class CardBackgroundTests(unittest.TestCase):
    """文字作品可以换成你上传的背景图。"""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from PIL import Image
        from app.db import Database
        import app.agent.runner as runner
        import app.api as apimod

        self.tmp = Path(tempfile.mkdtemp())
        self.p1 = mock.patch.object(runner, "DRAFTS_DIR", self.tmp / "drafts")
        self.p2 = mock.patch.object(apimod, "DRAFTS_DIR", self.tmp / "drafts")
        self.p1.start()
        self.p2.start()
        self.db = Database(self.tmp / "t.db")
        self.api = apimod.Api(self.db, mock.Mock())
        self.bgdir = runner.backgrounds_dir()
        self.bgdir.mkdir(parents=True)
        Image.new("RGB", (900, 600), (20, 30, 80)).save(self.bgdir / "bg_1.jpg")
        self.did = self.db.add_draft(status="wish", kind="image", title="风很温柔", text="今天也想分享给你。", tags=[],
                                     media=[], cards=[{"title": "小确幸", "lines": ["风很温柔"]}], reason="", source="manual")

    def tearDown(self):
        self.p1.stop()
        self.p2.stop()

    def _cards(self):
        from PIL import Image
        from app.agent.runner import media_path

        d = self.db.draft(self.did)
        return [Image.open(media_path(self.did, m)).convert("RGB") for m in d["media"] if m["source"] == "card"]

    def test_set_background_redraws_cards_on_it(self):
        r = self.api.draft_set_bg(self.did, "bg_1.jpg", False)
        self.assertTrue(r["ok"], r)
        imgs = self._cards()
        self.assertEqual(len(imgs), 2)
        self.assertEqual(imgs[0].size, (1080, 1440))
        px = imgs[0].getpixel((540, 1000))  # 卡片中间偏下：是深蓝背景，不是内置的粉色
        self.assertLess(px[0], 90)
        d = self.api.get_draft(self.did)["data"]
        self.assertEqual(d["card_bg"], "bg_1.jpg")
        self.assertEqual(d["card_panel"], 0)
        self.assertEqual([b["name"] for b in d["backgrounds"]], ["bg_1.jpg"])

    def test_default_background_and_delete(self):
        self.api.card_bg_set_default("bg_1.jpg", True)
        from app.agent.runner import card_background
        bg, panel = card_background(self.db, {"card_bg": ""})
        self.assertEqual(bg.name, "bg_1.jpg")
        self.assertTrue(panel)
        self.api.card_bg_delete("bg_1.jpg")
        self.assertEqual(self.db.rules()["card_bg_default"], "")
        self.assertEqual(card_background(self.db, {"card_bg": ""}), (None, True))
        r = self.api.draft_set_bg(self.did, "bg_1.jpg", True)
        self.assertFalse(r["ok"])  # 图删了就不能再选

    def test_builtin_style_still_default(self):
        from app.agent.runner import card_background
        self.assertEqual(card_background(self.db, {"card_bg": ""}), (None, True))
        self.assertTrue(self.api.draft_set_bg(self.did, "none", None)["ok"])
        px = self._cards()[0].getpixel((540, 40))
        self.assertGreater(px[0], 200)  # 内置粉色背景


class LimbTests(unittest.TestCase):
    """手臂、腿各是一整条，转到什么角度都和身体连着，不会裂开。"""

    def _alpha_path_connected(self, img, a, b):
        """从 a 到 b 的直线上每个点（放宽 3 像素）都有不透明像素。"""
        import numpy as np

        al = np.asarray(img)[..., 3]
        n = int(max(abs(b[0] - a[0]), abs(b[1] - a[1]))) + 1
        for i in range(n + 1):
            x = int(round(a[0] + (b[0] - a[0]) * i / n))
            y = int(round(a[1] + (b[1] - a[1]) * i / n))
            if al[max(0, y - 3):y + 4, max(0, x - 3):x + 4].max() < 100:
                return False
        return True

    def test_default_rig_builds_whole_limbs(self):
        from app.pet.rig import load

        rig = load("default")
        self.assertEqual(set(rig.limb_arms), {"l", "r"})
        self.assertEqual(set(rig.limb_legs), {"leg_l", "leg_r"})

    def test_arm_attached_at_any_angle(self):
        from app.pet import engine as E
        from app.pet.gfx import Canvas
        from app.pet.render import H, OX, OY, W, draw_pet
        from app.pet.rig import load

        rig = load("default")
        dev = 2.0
        rig.prepare(dev)
        for ang in (-40, 0, 45, 90, 135, 160):
            for st in (0.0, 0.5, 1.0):
                p = E.Pose()
                p.ArmL, p.StrL = ang, st
                cv = Canvas(W, H, OX, OY, dev)
                draw_pet(cv, rig, p, 0.0, None)
                limb = rig.limb_arms["l"]
                sx, sy = limb.S
                hx, hy = limb.hand(ang, st, 1.0)
                to_px = lambda x, y: ((x + OX) * dev, (y + OY) * dev)
                self.assertTrue(self._alpha_path_connected(cv.img, to_px(sx, sy), to_px(hx, hy)), (ang, st))

    def test_legs_have_no_skirt_piece(self):
        import numpy as np
        from app.pet.rig import load

        rig = load("default")
        for name, leg in rig.limb_legs.items():
            a = np.asarray(leg.strip.img)
            solid = a[..., 3] > 100
            # 腿条里不应该有裙子的粉色
            pink = solid & (a[..., 0] > 200) & (a[..., 1] > 150) & (a[..., 2] > 150)
            self.assertLess(pink.sum(), 30, name)

    def test_body_notch_repaired(self):
        import numpy as np
        from PIL import Image
        from app.pet.rig import DEFAULT_DIR, load

        raw = np.asarray(Image.open(DEFAULT_DIR / "body.png"))[..., 3] > 100
        fixed = np.asarray(load("default").src["body"])[..., 3] > 100
        self.assertGreater(fixed.sum(), raw.sum())   # 肩膀的缺口补上了


class GoneVideoTests(unittest.TestCase):
    """对方回复她的那条视频已经被删除 / 不可见：直接跳过，说明原因，不放进“待你处理”。"""

    def _row(self, imgs, svgs):
        from unittest import mock

        row = mock.Mock()
        row.locator.side_effect = lambda css: mock.Mock(count=mock.Mock(return_value=imgs if css == "img" else svgs))
        return row

    def test_detect_gone_thumbnail(self):
        from unittest import mock
        from app.douyin.inbox import Inbox

        ib = Inbox.__new__(Inbox)
        self.assertTrue(ib._video_gone(self._row(1, 4), "小鲸一 浪漫是本鱼的 回复了你的评论 昨天"))
        self.assertFalse(ib._video_gone(self._row(2, 3), "半岛铁饭盒 回复了你的评论"))
        self.assertTrue(ib._video_gone(self._row(2, 0), "回复了你的评论 · 作品已删除"))
        _ = mock

    def test_runner_skips_gone_video_reply(self):
        from unittest import mock
        from app.agent.runner import Agent

        agent = Agent(make_db())
        it = {"kind": "reply", "user": "小鲸一", "text": "浪漫是本鱼的", "ext_id": "rp_x", "video_url": "",
              "video_gone": True, "mine": "蜡烛", "cid": "", "user_url": ""}
        mid = agent.db.add_message(kind="reply", ext_id="rp_x", user_name="小鲸一", content="浪漫是本鱼的",
                                   status="pending", reason="处理中")
        agent._skip_gone(mid, "小鲸一")
        m = agent.db.query("SELECT status, reason FROM messages WHERE id=?", (mid,))[0]
        self.assertEqual(m["status"], "skipped_gone")
        self.assertIn("删除", m["reason"])
        _ = (it, mock)


class NoticeThreadTests(unittest.TestCase):
    """@ 她的评论是回复在别人评论下面的（楼中楼）：从互动消息接口里认出楼主，展开那一串再回复。"""

    def test_collect_notice_comments(self):
        from app.douyin.browser import DouyinBrowser

        b = DouyinBrowser.__new__(DouyinBrowser)
        b.notice_comments = []
        data = {"notice_list_v2": [
            {"type": 45, "comment": {"comment": {"cid": "7300000000000000002", "text": "@Yu 快点来吃大肥鱼啦",
                                                 "reply_id": "7300000000000000001", "reply_to_reply_id": "0",
                                                 "user": {"nickname": "雨."}},
                                     "aweme": {"aweme_id": "7299999999999999999"}}},
            {"type": 31, "comment": {"comment": {"cid": "7300000000000000003", "text": "顶层评论", "reply_id": "0"}}},
        ]}
        b._collect_notices(data)
        c = b.notice_comment("@Yu 快点来吃大肥鱼啦", "雨.")
        self.assertEqual(c["cid"], "7300000000000000002")
        self.assertEqual(c["reply_id"], "7300000000000000001")
        self.assertEqual(c["aweme_id"], "7299999999999999999")
        self.assertIsNone(b.notice_comment("@Yu 快点来吃大肥鱼啦", "别人"))
        self.assertEqual(b.notice_comment("顶层评论")["reply_id"], "0")


class InboxEpochTests(unittest.TestCase):
    """只回这次启动、活跃时段开始之后收到的 @ 和私信；之前的全部跳过，不提醒。"""

    def _agent(self):
        from unittest import mock
        from app.agent.runner import Agent

        agent = Agent(make_db())
        r = agent.db.rules()
        r["auto_reply_at"] = True
        r["auto_reply_dm"] = True
        agent.db.set_json("rules", r)
        agent._ensure_browser = mock.Mock(return_value=mock.Mock())
        return agent

    def test_epoch_key_uses_last_active_start(self):
        from datetime import datetime

        agent = self._agent()
        rules = {"active_start": "09:00"}
        self.assertEqual(agent._epoch_key(rules, datetime(2026, 11, 7, 10, 0)), "2026-11-07 09:00")
        self.assertEqual(agent._epoch_key(rules, datetime(2026, 11, 7, 8, 0)), "2026-11-06 09:00")

    def test_first_check_skips_everything_already_there(self):
        from unittest import mock

        agent = self._agent()
        old_pending = agent.db.add_message(kind="at", ext_id="at_old", user_name="旧人", content="旧消息",
                                           status="pending", reason="待你处理")
        mention = {"kind": "at", "ext_id": "at_1", "user": "雨.", "text": "@Yu 给我买奶茶", "video_url": "u"}
        dm = {"ext_id": "dm_1", "user": "小明", "text": "在吗"}
        with mock.patch("app.agent.runner.Inbox") as InboxCls:
            ib = InboxCls.return_value
            ib.fetch_mentions.return_value = [mention]
            ib.fetch_unread_dms.return_value = [dm]
            agent._handle_incoming = mock.Mock()
            agent._process_inbox(stop_on_pause=False)
            agent._handle_incoming.assert_not_called()            # 之前的一条都不回
            self.assertFalse(ib.fetch_mentions.call_args.kwargs["resolve_video"])
            rows = {r["ext_id"]: r["status"] for r in agent.db.query("SELECT ext_id, status FROM messages")}
            self.assertEqual(rows, {"at_old": "skipped_old", "at_1": "skipped_old", "dm_1": "skipped_old"})
            # 之后新来的才回
            ib.fetch_mentions.return_value = [mention, {**mention, "ext_id": "at_2", "text": "新的 @"}]
            ib.fetch_unread_dms.return_value = []
            agent._process_inbox(stop_on_pause=False)
            self.assertEqual(agent._handle_incoming.call_count, 1)
            self.assertEqual(agent._handle_incoming.call_args.args[2]["ext_id"], "at_2")
        _ = old_pending

    def test_failed_handling_never_stays_processing(self):
        from unittest import mock

        agent = self._agent()
        agent._inbox_epoch = agent._epoch_key(agent.db.rules())
        with mock.patch("app.agent.runner.Inbox") as InboxCls, mock.patch("app.agent.runner.Feed"):
            ib = InboxCls.return_value
            ib.fetch_mentions.return_value = [{"kind": "at", "ext_id": "at_9", "user": "雨.", "text": "@Yu 嗨",
                                               "video_url": "u"}]
            ib.fetch_unread_dms.return_value = []
            agent._handle_incoming = mock.Mock(side_effect=RuntimeError("读不到这个视频的文案和作者"))
            agent._process_inbox(stop_on_pause=False)
        m = agent.db.query("SELECT status, reason FROM messages WHERE ext_id='at_9'")[0]
        self.assertEqual(m["status"], "failed")
        self.assertIn("读不到", m["reason"])


class EmergencyStopTests(unittest.TestCase):
    """点“让她休息一下”：当前所有操作立刻结束。"""

    def test_sleep_wakes_up_immediately_when_stopped(self):
        import threading
        import time as _t
        from app import cancel

        cancel.bind_current_thread()
        threading.Timer(0.1, cancel.STOP.set).start()
        t0 = _t.time()
        with self.assertRaises(cancel.OperationCancelled):
            cancel.sleep(10)
        self.assertLess(_t.time() - t0, 1.0)

    def test_cancelled_is_not_swallowed_by_except_exception(self):
        from app import cancel

        cancel.STOP.set()
        with self.assertRaises(cancel.OperationCancelled):
            try:
                cancel.checkpoint()
            except Exception:  # noqa: BLE001 —— 各处的“单次失败不影响整体”不能把急停吞掉
                self.fail("急停被 except Exception 吞掉了")

    def test_shield_and_other_threads_are_not_interrupted(self):
        import threading
        from app import cancel

        cancel.bind_current_thread()
        cancel.STOP.set()
        with cancel.shield():
            cancel.checkpoint()  # 已经点了“发布”在等结果：不打断
        box = {}
        t = threading.Thread(target=lambda: box.update(ok=not cancel.requested()))
        t.start()
        t.join()
        self.assertTrue(box["ok"])  # 界面线程的操作不受影响

    def test_call_returns_early_without_waiting_for_slow_request(self):
        import threading
        import time as _t
        from app import cancel

        cancel.bind_current_thread()
        threading.Timer(0.1, cancel.STOP.set).start()
        t0 = _t.time()
        with self.assertRaises(cancel.OperationCancelled):
            cancel.call(_t.sleep, 5)
        self.assertLess(_t.time() - t0, 1.0)
        cancel.STOP.clear()
        self.assertEqual(cancel.call(lambda x: x * 2, 21), 42)

    def test_browser_actions_stop_at_next_step(self):
        from app import cancel
        from app.douyin.browser import DouyinBrowser

        b = DouyinBrowser.__new__(DouyinBrowser)
        b.sel = {"comment_input": ["x"]}
        b._page = mock.Mock()
        cancel.STOP.set()
        for fn in (lambda: b.find("comment_input"), lambda: b.click(mock.Mock()),
                   lambda: b.type_slowly("你好"), lambda: DouyinBrowser.pause(5, 6)):
            with self.assertRaises(cancel.OperationCancelled):
                fn()

    def _agent(self):
        from app.agent.runner import Agent

        agent = Agent(make_db())
        agent._ensure_browser = mock.Mock(return_value=mock.Mock())
        return agent

    def test_request_pause_is_immediate_and_deduplicated(self):
        from app import cancel

        agent = self._agent()
        agent._want_run = True
        agent.request_pause()
        agent.request_pause()
        self.assertTrue(cancel.STOP.is_set())
        self.assertFalse(agent.status()["running"])
        self.assertEqual(agent.state, "paused")
        self.assertEqual(agent.q.qsize(), 1)
        agent._dispatch(*agent.q.get())
        self.assertEqual(agent._pause_queued, 0)

    def test_cancelled_message_goes_back_to_pending_and_stop_propagates(self):
        from app import cancel

        agent = self._agent()
        mid = agent.db.add_message(kind="at", ext_id="at_x", user_name="小明", content="@她", status="pending",
                                   reason="处理中")
        agent._handle_incoming = mock.Mock(side_effect=cancel.OperationCancelled())
        with self.assertRaises(cancel.OperationCancelled):
            agent._safe_handle(mock.Mock(), mid, {"user": "小明"}, "at", agent.db.rules())
        row = agent.db.query("SELECT status, reason FROM messages WHERE id=?", (mid,))[0]
        self.assertEqual(row["status"], "pending")
        self.assertIn("休息", row["reason"])

    def test_top_level_command_cancel_does_not_kill_thread(self):
        from app import cancel

        agent = self._agent()
        cancel.STOP.set()
        agent._cmd_check_inbox = mock.Mock(side_effect=cancel.OperationCancelled())
        agent._dispatch("check_inbox", {})  # 不能往外抛，否则后台线程会退出
        self.assertEqual(agent.state, "paused")

    def test_publish_interrupted_is_marked_failed(self):
        from app import cancel

        agent = self._agent()
        did = agent.db.add_draft(status="publishing", kind="image", title="小作品", text="今天好开心",
                                 tags=[], cards=[], media=[{"name": "a.jpg", "kind": "image"}])
        agent.brain.check_post = mock.Mock(return_value=(True, "", None))
        with mock.patch("app.agent.runner.media_path") as mp, \
                mock.patch("app.agent.runner.publish", side_effect=cancel.OperationCancelled()):
            mp.return_value.exists.return_value = True
            agent._dispatch("publish", {"did": did})
        d = agent.db.draft(did)
        self.assertEqual(d["status"], "failed")
        self.assertIn("没有发出去", d["error"])


class CardSignatureTests(unittest.TestCase):
    def test_default_avatar_is_software_mascot(self):
        from app.agent.pet_avatar import DEFAULT_AVATAR, persona_avatar

        self.assertTrue(DEFAULT_AVATAR.exists())
        av = persona_avatar({})
        self.assertIsNotNone(av)
        self.assertEqual(av.size, Image.open(DEFAULT_AVATAR).size)

    def test_uploaded_avatar_wins(self):
        from app.agent import pet_avatar

        d = Path(tempfile.mkdtemp())
        Image.new("RGB", (64, 64), (10, 200, 30)).save(d / "me.png")
        with mock.patch.object(pet_avatar, "AVATAR_DIR", d):
            av = pet_avatar.persona_avatar({"avatar": "me.png"})
        self.assertEqual(av.getpixel((32, 32))[:3], (10, 200, 30))

    def test_signature_shows_only_name(self):
        from app.publish import cards

        drawn = []
        real = cards.fonts.draw_text

        def spy(d, xy, text, *a, **k):
            drawn.append(text)
            return real(d, xy, text, *a, **k)

        with mock.patch.object(cards.fonts, "draw_text", side_effect=spy):
            cards.render_card({"title": "标题", "lines": ["一行"]}, 1, 2, "小雨", None, palette=0)
        self.assertIn("小雨", drawn)
        self.assertFalse(any("AI虚拟人" in t for t in drawn))


class PersonaCustomTests(unittest.TestCase):
    """人设不分类型、没有“给人的感觉”：性格全由滑块自己调。"""

    def test_prompt_uses_sliders_not_vibe(self):
        p = {"name": "小雨", "vibe": "旧版本留下的感觉", "archetype": "yujie",
             "traits": {"energy": 10, "sharp": 90, "talkative": 50, "cute": 0, "temper": 35, "volatility": 50}}
        sp = system_prompt(p)
        self.assertIn("AI 虚拟女孩「小雨」", sp)
        self.assertNotIn("旧版本留下的感觉", sp)
        self.assertIn("非常安静", sp)
        self.assertIn("非常犀利直接", sp)
        self.assertIn("成年人", sp)

    def test_save_drops_old_type_fields(self):
        from app.api import Api

        api = Api.__new__(Api)
        api._db = make_db()
        api._pet = None
        api._db.set_json("persona", {**api._db.persona(), "vibe": "元气少女", "archetype": "yujie", "custom": {}})
        Api.save_persona(api, {"name": "小雨", "vibe": "乱写", "traits": {"energy": 12}})
        p = api._db.get_json("persona")
        for k in ("vibe", "archetype", "custom"):
            self.assertNotIn(k, p)
        self.assertEqual(p["traits"]["energy"], 12)
