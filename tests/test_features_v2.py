"""0.2.0 新功能的测试：心情、评论接着聊、@ 互关好友、发作品、桌宠。不需要联网、不需要浏览器。"""
from __future__ import annotations

import json
import math
import random
import sys
import tempfile
import unittest
import time
from datetime import datetime, timedelta
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

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from app.agent.brain import Brain  # noqa: E402
from app.agent.mood import MOODS, MoodEngine  # noqa: E402
from app.agent.persona import system_prompt  # noqa: E402
from app.db import Database  # noqa: E402
from app.llm.client import LLMResult  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RIG = ROOT / "assets" / "pet" / "default"


def be_friends(db, *names):
    """把这些人记成“从抖音关注列表里核实过的互关好友”（只有互关好友才能设为主人）。"""
    for n in names:
        db.save_friend(f"uid_{n}", {"nickname": n, "mutual": True})


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
        agent.db.set_json("rules", dict(agent.db.rules(), post_form="cards"))     # 图文卡片这种形式
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
        feed.comment.assert_called_once_with("你家咪咪也这样吧", mention="小明", image=None)
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
        names = [b["name"] for b in d["backgrounds"]]
        self.assertEqual(names[0], "bg_1.jpg")                                 # 你上传的排最前面，后面是内置的可爱背景
        self.assertTrue(all(n.startswith("bg_cute_") for n in names[1:]))

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
        self.assertIn("虚拟女孩「小雨」", sp)
        self.assertIn("必须如实说自己是 AI", sp)
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
        for k in ("vibe", "custom"):
            self.assertNotIn(k, p)
        self.assertEqual(p["archetype"], "")                 # 旧版本留下的、现在认不出的性格类型：清空
        self.assertEqual(p["traits"]["energy"], 12)


def _chubby_pet():
    """和用户反馈的小猪类似：粗短胳膊画在身体后面、蹄子比胳膊粗、圆肚子垂到胯下面、没有轮廓线。"""
    from PIL import ImageDraw
    img = Image.new("RGBA", (600, 820), (0, 0, 0, 0))
    hoof = Image.new("L", img.size, 0)
    d = ImageDraw.Draw(img)
    dh = ImageDraw.Draw(hoof)
    pink = (250, 170, 190, 255)
    for (sx, sy), (ex, ey), (hx, hy) in (((215, 470), (165, 535), (140, 590)), ((385, 470), (435, 535), (460, 590))):
        d.line((sx, sy, ex, ey, hx, hy), fill=pink, width=50, joint="curve")
        d.ellipse((hx - 32, hy - 30, hx + 32, hy + 34), fill=(245, 160, 180, 255))
        dh.ellipse((hx - 32, hy - 30, hx + 32, hy + 34), fill=255)
    for x in (255, 345):
        d.rounded_rectangle((x - 36, 640, x + 36, 780), radius=34, fill=(240, 160, 180, 255))
    d.ellipse((185, 410, 415, 700), fill=(252, 178, 198, 255))      # 圆肚子（底到 700，胯在 680）
    d.ellipse((150, 160, 450, 470), fill=(250, 175, 195, 255))      # 头套
    d.ellipse((210, 260, 390, 440), fill=(245, 215, 200, 255))      # 脸
    joints = {"neck": [300, 452], "shoulder_l": [215, 470], "elbow_l": [165, 535], "hand_l": [140, 592],
              "shoulder_r": [385, 470], "elbow_r": [435, 535], "hand_r": [460, 592],
              "hip_l": [255, 680], "hip_r": [345, 680]}
    return img, joints, np.asarray(hoof) > 128


def _layer(out: Path, rig: dict, name: str, size) -> np.ndarray:
    """某个部件放回原图位置后的透明度（0～255）。"""
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    if name in rig["parts"]:
        canvas.alpha_composite(Image.open(out / f"{name}.png"), tuple(rig["parts"][name]))
    return np.asarray(canvas)[..., 3].astype(int)


class CutoutSeamTests(unittest.TestCase):
    """自定义形象拆部件：手 / 蹄子整块归胳膊、不留碎片；胳膊描边不被当成头发；肚子底不被切给腿；同色胳膊不被削成细棍。"""

    @classmethod
    def setUpClass(cls):
        from app.pet import autorig
        cls.img, cls.joints, cls.hoof = _chubby_pet()
        cls.out = Path(tempfile.mkdtemp()) / "rig"
        cls.rig = autorig.build(cls.img, cls.joints, 50, cls.out)

    def test_components(self):
        from app.pet.autorig import _components
        m = np.zeros((20, 20), dtype=bool)
        m[2:5, 2:5] = True
        m[10:15, 10:12] = True
        m[5, 5] = True                      # 斜着挨着 → 和第一块是一块
        lab, n = _components(m)
        self.assertEqual(n, 2)
        self.assertEqual(lab[2, 2], lab[5, 5])
        self.assertNotEqual(lab[2, 2], lab[12, 10])

    def test_whole_hoof_goes_with_arm(self):
        size = self.img.size
        arm = sum(_layer(self.out, self.rig, n, size) for n in ("arm_l_up", "arm_l_lo", "arm_r_up", "arm_r_lo"))
        self.assertGreater((arm[self.hoof] > 128).mean(), 0.97)       # 比胳膊粗的蹄子整块在胳膊里
        for name in ("head", "body", "leg_l", "leg_r"):
            a = _layer(self.out, self.rig, name, size)
            self.assertLess(int((a[self.hoof] > 12).sum()), 15, name)  # 头 / 身体 / 腿上不留蹄子碎片

    def test_no_arm_edges_left_on_head_or_body(self):
        size = self.img.size
        A = np.asarray(self.img)[..., 3]
        torso = np.zeros(A.shape, dtype=bool)
        from PIL import ImageDraw
        t = Image.new("L", size, 0)
        ImageDraw.Draw(t).ellipse((183, 408, 417, 702), fill=255)
        ImageDraw.Draw(t).ellipse((148, 158, 452, 472), fill=255)
        torso = np.asarray(t) > 0
        outside = (A > 0) & ~torso & (np.arange(size[1])[:, None] < 640)   # 头 / 肚子以外、腿以上 = 只有胳膊
        for name in ("head", "body"):
            a = _layer(self.out, self.rig, name, size)
            self.assertEqual(int((a[outside] > 3).sum()), 0, name)

    def test_belly_bottom_stays_on_body(self):
        size = self.img.size
        body = _layer(self.out, self.rig, "body", size)
        self.assertGreater(body[692, 300], 200)                       # 胯（680）下面的肚子底还在身体上
        legs = _layer(self.out, self.rig, "leg_l", size) + _layer(self.out, self.rig, "leg_r", size)
        self.assertGreater(legs[760, 255], 200)

    def test_same_colour_arm_not_thinned(self):
        from app.pet.rig import Rig
        self.assertEqual(self.rig.get("arm_seg"), 2)
        r = Rig(self.out)
        self.assertIn("l", r.limb_arms)
        st = r.limb_arms["l"].strip
        a = np.asarray(st.img)[..., 3]
        mid = int(st.e + st.length * 0.45)                             # 上臂和前臂之间那一段
        self.assertGreaterEqual(int((a[:, mid] > 128).sum()), 40)     # 胳膊画的是 50 粗：不能被当成身体碎片削细

    def test_default_rig_unchanged_path(self):
        """默认形象（旧版拆的，没有 arm_seg）照旧按原来的方式处理。"""
        from app.pet.rig import Rig, rig_dir
        r = Rig(rig_dir("default"))
        self.assertNotIn("arm_seg", r.data)
        self.assertEqual(set(r.limb_arms), {"l", "r"})


class PhoneHoldTests(unittest.TestCase):
    """刷视频捧手机：手肘自然弯着、双手在手机两边，不是两条胳膊直直地交叉伸过去。"""

    def _pose(self, act, secs=2.0):
        from app.pet import engine as E
        pet = E.Pet(rnd=random.Random(3))
        pet.set_act(act, 1e9)
        for _ in range(int(secs / 0.033)):
            pet.act_t += 0.033
            pet.t += 0.033
            pet._smooth_arms(0.033)
        return pet.pose()

    def test_engine_uses_hold_not_straight_arms(self):
        from app.pet import engine as E
        p = self._pose(E.PHONE)
        self.assertGreater(p.HoldL, 0.95)
        self.assertGreater(p.HoldR, 0.95)
        self.assertLess(p.StrL, 0.05)
        self.assertLess(abs(p.ArmL), 2.0)
        free = self._pose(E.WAVE, 1.0)
        self.assertLess(free.HoldL, 0.05)                 # 别的动作不受影响

    def test_two_bone_bends_elbow_outward(self):
        from app.pet.limbs import two_bone
        up, lo = two_bone((0.0, 0.0), 50.0, 50.0, (40.0, 60.0), out=-1.0)   # 左手伸向身前偏右下
        ex = math.cos(up) * 50
        self.assertLess(ex, 0)                            # 手肘在肩膀外侧（左边）
        bend = abs((lo - up + math.pi) % (2 * math.pi) - math.pi)
        self.assertGreater(math.degrees(bend), 45)        # 真的弯了
        hx, hy = ex + math.cos(lo) * 50, math.sin(up) * 50 + math.sin(lo) * 50
        self.assertAlmostEqual(hx, 40.0, delta=0.5)
        self.assertAlmostEqual(hy, 60.0, delta=0.5)

    def test_rig_hands_meet_at_phone_without_crossing(self):
        from app.pet import engine as E
        from app.pet.render import hold_target
        from app.pet.rig import Rig, rig_dir
        r = Rig(rig_dir("default"))
        p = self._pose(E.PHONE)
        hands = {}
        for side, deg, h, d in (("l", p.ArmL, p.HoldL, p.HoldDL), ("r", -p.ArmR, p.HoldR, p.HoldDR)):
            limb = r.limb_arms[side]
            hold = (h, hold_target(r, side, d), -1.0 if side == "l" else 1.0)
            hands[side] = limb.hand(deg, p.StrL, 1.0, hold)
            (sx, sy), (ex, ey), up, lo = limb.chain(deg, p.StrL, 1.0, hold)
            out = -1 if side == "l" else 1
            hx, hy = hands[side]
            x_line = sx + (hx - sx) * (ey - sy) / ((hy - sy) or 1e-6)          # 肩→手连线在手肘这一高度的 x
            self.assertGreater((ex - x_line) * out, 0, side)                     # 手肘往外弯，不往身体里拐
            bend = abs((lo - up + math.pi) % (2 * math.pi) - math.pi)
            self.assertGreater(math.degrees(bend), 30, side)                     # 手肘弯着
        self.assertLess(hands["l"][0], hands["r"][0])                            # 左手在左、右手在右，不交叉
        self.assertLess(hands["r"][0] - hands["l"][0], 80 * r.fy)               # 两只手凑在手机两边



class CommentLikePolicyTests(unittest.TestCase):
    """赞别人的评论：只在喜欢的视频上、只赞特别出彩的、一般 1 条、一天匀着赞，跟着“点赞倾向”走。"""

    RULES = {"like_tendency": "normal", "limits": {"comment_likes": 30}, "active_start": "00:00", "active_end": "23:59"}
    NOON = datetime(2026, 11, 7, 12, 0)

    def _pol(self, r=0.0):
        from app.agent.likes import LikePolicy
        return LikePolicy(make_db(), mock.Mock(random=mock.Mock(return_value=r)))

    def test_gate(self):
        pol = self._pol(0.9)
        self.assertTrue(pol.comment_gate(self.RULES, 0, self.NOON)["ok"])               # 看了的视频都看评论区
        early = datetime(2026, 11, 7, 1, 0)
        self.assertFalse(pol.comment_gate(self.RULES, 10, early)["ok"])                 # 凌晨就赞了 10 条：太快
        self.assertFalse(pol.comment_gate(self.RULES, 30, self.NOON)["ok"])             # 额度用完

    def test_brain_parses_scores(self):
        from app.agent.brain import Brain
        b = Brain(make_db())
        fake = mock.Mock(text='{"like": true, "comment": "", "comment_likes": [[2, 9], {"no": 1, "score": 6}, 3, [9, 10]]}',
                         cost=0.0)
        with mock.patch.object(b.router, "call", return_value=fake):
            d = b.decide({"author": "a", "desc": "d", "comments": ["一", "二", "三"]}, [], True, False, True, False)
        self.assertEqual(d["comment_scores"], [(1, 9), (0, 6), (2, 8)])                # 越界的去掉；只写编号算 8 分

class CollectPolicyTests(unittest.TestCase):
    """收藏：一天匀着收；同一类收藏多了提高标准、再多就不收。"""

    RULES = {"limits": {"collects": 10}, "active_start": "09:00", "active_end": "23:00"}

    def _db(self, collected_topics=(), when="2026-11-07 10:00:00"):
        db = make_db()
        for i, t in enumerate(collected_topics):
            db.add_video(video_id=f"c{i}", author="x", interest=9, decision="collect", collected=1, topic=t,
                         created_at=when)
        return db

    def test_pacing(self):
        from app.agent.likes import LikePolicy
        pol = LikePolicy(self._db())
        early = datetime(2026, 11, 7, 10, 0)                                          # 一天刚过 1/14
        self.assertTrue(pol.collect_gate(8, "", self.RULES, 1, early)["ok"])
        self.assertFalse(pol.collect_gate(8, "", self.RULES, 3, early)["ok"])         # 上午就收了 3 个：太快
        self.assertTrue(pol.collect_gate(10, "", self.RULES, 3, early)["ok"])         # 10 分的破例
        self.assertFalse(pol.collect_gate(10, "", self.RULES, 10, datetime(2026, 11, 7, 22, 0))["ok"])  # 额度用完
        self.assertTrue(pol.collect_gate(8, "", self.RULES, 6, datetime(2026, 11, 7, 20, 0))["ok"])     # 晚上进度跟得上

    def test_same_topic(self):
        from app.agent.likes import LikePolicy
        night = datetime(2026, 11, 7, 22, 0)
        pol = LikePolicy(self._db(["菜谱", "菜谱"]))
        self.assertFalse(pol.collect_gate(8, "菜谱", self.RULES, 2, night)["ok"])     # 已经收藏 2 个菜谱：8 分不够
        g = pol.collect_gate(9, "菜谱", self.RULES, 2, night)
        self.assertTrue(g["ok"])
        self.assertIn("2 个「菜谱」", g["note"])                                      # 提醒她标准要更高
        self.assertTrue(pol.collect_gate(8, "风景", self.RULES, 2, night)["ok"])      # 别的类不受影响
        pol3 = LikePolicy(self._db(["菜谱"] * 3))
        self.assertFalse(pol3.collect_gate(10, "菜谱", self.RULES, 3, night)["ok"])   # 3 个以上就不再收
        old = LikePolicy(self._db(["菜谱"] * 3, when="2026-11-06 20:00:00"))
        self.assertTrue(old.collect_gate(8, "菜谱", self.RULES, 0, night)["ok"])      # 昨天收的不算

    def test_note_goes_into_prompt(self):
        from app.agent.brain import Brain
        b = Brain(make_db())
        fake = mock.Mock(text='{"collect": false}', cost=0.0)
        with mock.patch.object(b.router, "call", return_value=fake) as call:
            b.decide({"author": "a", "desc": "d", "comments": []}, [], False, False, can_collect=True,
                     collect_note="你今天已经收藏了 2 个「菜谱」类的视频")
        self.assertIn("2 个「菜谱」", call.call_args.args[2])

    def test_runner_does_not_offer_collect_when_paced(self):
        from app.agent.runner import Agent
        db = make_db()
        db.add_usage("collects", 10)                                                  # 今天收满了
        agent = Agent(db)
        agent._want_run = True
        agent._sleep = lambda s: agent._want_run
        agent.likes.rnd = mock.Mock(random=mock.Mock(return_value=0.99))
        agent.brain = mock.Mock()
        agent.brain.router.task_can_see.return_value = True
        agent.brain.triage.return_value = {"interest": 9, "topic": "菜谱", "reason": "", "cost": 0}
        agent.brain.decide.return_value = {"interest": 9, "like": False, "collect": True, "comment": "", "reason": "",
                                           "cost": 0}
        with mock.patch("app.agent.runner.Feed") as FeedCls:
            feed = FeedCls.return_value
            feed.read_current.return_value = {"video_id": "n", "url": "u", "author": "b", "desc": "番茄炒蛋", "comments": []}
            feed.capture_frames.side_effect = lambda n, d, sleep=None: [b"x"] * n
            agent._watch_one(mock.Mock(), agent.db.rules())
        self.assertFalse(agent.brain.decide.call_args.kwargs["can_collect"])
        feed.collect.assert_not_called()                                              # 模型说要收也不收


class PublishVerifyTests(unittest.TestCase):
    """创作者中心点“发布”后弹“接收短信验证码”：要认出来、弹窗等你验证，验证完接着等发布结果。"""

    FIXTURE = Path(__file__).parent / "fixtures" / "creator_sms_verify.html"

    def test_real_dialog_is_recognised(self):
        """用用户发来的真实页面里的验证弹窗：verification_open() 要认出来，verify_kind() 是短信验证。"""
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"没有 Playwright：{e}")
        try:
            try:
                br = pw.chromium.launch()
            except Exception:  # noqa: BLE001
                try:
                    br = pw.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
                except Exception as e:  # noqa: BLE001
                    self.skipTest(f"没有可用的浏览器：{e}")
            from app.douyin.browser import DouyinBrowser
            from app.douyin.selectors import DEFAULT_SELECTORS
            ctx = br.new_context()
            page = ctx.new_page()
            page.goto(self.FIXTURE.as_uri())
            b = DouyinBrowser.__new__(DouyinBrowser)
            b.sel, b.ctx, b._page = DEFAULT_SELECTORS, ctx, page
            self.assertTrue(b.verification_open())
            self.assertEqual(b.verify_kind(), "sms")
            page.set_content("<p>评论：收到验证码了吗 哈哈</p>")
            self.assertFalse(b.verification_open())                                  # 只有“验证码”几个字不算
            br.close()
        finally:
            pw.stop()

    def _browser(self, verify_seq, published_after):
        """假浏览器：verify_seq 依次是每次查“有没有验证框”的结果；第 published_after 次查结果时显示发布成功。"""
        b = mock.Mock()
        b.sel = {"creator_done_words": ["发布成功"]}
        seq = iter(verify_seq)
        b.verification_open.side_effect = lambda: next(seq, False)
        b.verify_kind.return_value = "sms"
        n = {"checks": 0}

        def visible(*a, **k):
            n["checks"] += 1
            return n["checks"] >= published_after
        b.page.url = "https://creator.douyin.com/creator-micro/content/upload"
        b.page.get_by_text.return_value.first.is_visible.side_effect = visible
        b.dump.return_value = "x"
        return b

    def test_handle_verify(self):
        from app.douyin import publish as pub
        b = self._browser([False], 99)
        self.assertFalse(pub._handle_verify(b, mock.Mock()))                         # 没有验证：不打扰
        b = self._browser([True], 99)
        wait = mock.Mock(return_value=True)
        self.assertTrue(pub._handle_verify(b, wait))
        wait.assert_called_once_with("sms")
        b = self._browser([True], 99)
        with self.assertRaises(pub.PublishError) as ctx:
            pub._handle_verify(b, mock.Mock(return_value=False))                     # 没验证完：明确告诉你原因
        self.assertIn("手机短信验证码", str(ctx.exception))

    def test_after_click_waits_for_sms_then_finishes(self):
        """点了“发布”→ 弹短信验证 → 你验证完 → 自动补点一次“发布” → 看到发布成功。"""
        from app.douyin import publish as pub
        b = self._browser([False, True, False, False], published_after=3)
        b.find.return_value = mock.Mock()
        b.safe_click.return_value = True
        waited = []
        with mock.patch.object(pub, "declare_ai", return_value=True), \
                mock.patch.object(pub.cancel, "sleep"), mock.patch.object(pub, "_visible_text", return_value=""), \
                mock.patch.object(pub, "_published", side_effect=[False, True]):
            b.in_new_tab.return_value.__enter__ = mock.Mock(return_value=None)
            b.in_new_tab.return_value.__exit__ = mock.Mock(return_value=False)
            msg = pub.publish(b, {"kind": "image", "title": "t", "text": "x", "tags": []}, [Path("a.jpg")],
                              wait_verify=lambda k: waited.append(k) or True)
        self.assertEqual(msg, "发布成功")
        self.assertEqual(waited, ["sms"])
        publish_clicks = [c for c in b.safe_click.call_args_list if c.args[1:] == ("发布",)]
        self.assertEqual(len(publish_clicks), 2)                                     # 验证完补点了一次

    def test_runner_wait_publish_verify_pops_up_and_hides(self):
        from app.agent.runner import Agent
        agent = Agent(make_db())
        agent.browser = mock.Mock()
        seq = iter([True, False, False])
        agent.browser.verification_open.side_effect = lambda: next(seq, False)
        seen = []
        with mock.patch("app.agent.runner.winfocus"), \
                mock.patch("app.agent.runner.time.sleep", side_effect=lambda s: seen.append(agent.attention)):
            ok = agent._wait_publish_verify("sms")
        self.assertTrue(ok)
        self.assertEqual(seen[0]["kind"], "sms")
        self.assertIn("发作品时", seen[0]["body"])
        self.assertIn("接着把作品发出去", seen[0]["body"])
        agent.browser.show_window.assert_called()
        agent.browser.hide_window.assert_called_once()
        self.assertIsNone(agent.attention)


class StubbornClickTests(unittest.TestCase):
    """点“通知 / 私信”入口被浮层挡住、视频画面一直在动：换点法 / 换截图方法，在真浏览器里验证。"""

    PAGE = """<!doctype html><meta charset="utf-8"><body style="margin:0">
    <button data-e2e="im-entry" style="position:absolute;left:20px;top:20px;width:80px;height:40px"
      onclick="document.getElementById('p').style.display='block'">消息</button>
    <div id="mask" style="position:absolute;left:0;top:0;width:300px;height:100px;background:transparent"></div>
    <div id="p" data-e2e="im-panel" style="display:none;position:absolute;left:20px;top:120px;width:200px;height:200px;background:#fcc">私信面板</div>
    <video id="v" width="160" height="90" style="position:absolute;left:20px;top:340px;background:#36c;
      animation:wob .2s infinite alternate"></video>
    <style>@keyframes wob{from{transform:translateX(0)}to{transform:translateX(30px)}}</style></body>"""

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
            cls.pw = sync_playwright().start()
        except Exception as e:  # noqa: BLE001
            raise unittest.SkipTest(f"没有 Playwright：{e}")
        try:
            try:
                cls.br = cls.pw.chromium.launch()
            except Exception:  # noqa: BLE001
                cls.br = cls.pw.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
        except Exception as e:  # noqa: BLE001
            cls.pw.stop()
            raise unittest.SkipTest(f"没有可用的浏览器：{e}")

    @classmethod
    def tearDownClass(cls):
        cls.br.close()
        cls.pw.stop()

    def _browser(self):
        from app.douyin.browser import DouyinBrowser
        ctx = self.br.new_context()
        page = ctx.new_page()
        page.set_content(self.PAGE)
        b = DouyinBrowser.__new__(DouyinBrowser)
        b.ctx, b._page = ctx, page
        b.sel = {"tip_close": [], "dm_entry": ["[data-e2e='im-entry']"], "dm_panel": ["[data-e2e='im-panel']"],
                 "captcha": [], "video_element": ["video"]}
        b.pause = lambda *a, **k: None
        return b, page

    def test_covered_entry_still_opens_panel(self):
        b, page = self._browser()
        entry = page.locator("[data-e2e='im-entry']").first
        with self.assertLogs("app.douyin.browser", level="INFO") as logs:
            ok = b.click_open(entry, "私信入口", lambda t: b.find("dm_panel", timeout=t) is not None)
        self.assertTrue(ok)
        self.assertTrue(page.locator("#p").is_visible())
        self.assertTrue(any("intercepts pointer events" in m for m in logs.output))   # 日志里写清了被什么挡住

    def test_why_picks_useful_line(self):
        from app.douyin.browser import why
        e = Exception("Timeout 6000ms exceeded.\nCall log:\n  - waiting for locator\n"
                      "  - <div class=\"mask\"></div> intercepts pointer events\n  - retrying click action")
        self.assertIn("intercepts pointer events", why(e))
        self.assertTrue(why(e).startswith("Timeout 6000ms exceeded."))

    def test_moving_video_still_captured(self):
        from app.douyin.feed import Feed
        b, page = self._browser()
        feed = Feed.__new__(Feed)
        feed.b = b
        video = page.locator("video").first
        png = feed._grab(video, timeout=1500)
        self.assertTrue(png.startswith(b"\x89PNG"))


class AdAndProductCardTests(unittest.TestCase):
    """广告：认出来、默认直接划走；商品卡挡住评论：先关掉商品卡。结构照用户发来的真实页面（2026-11）。"""

    PAGE = """<!doctype html><meta charset="utf-8"><body>
    <div data-e2e="feed-active-video"><div data-e2e="video-info"><div class="account">
      <div data-e2e="feed-video-nickname">@MINISO名创优品拖鞋旗舰店</div>
      <div class="video-create-time"><span class="time">· 9月9日</span></div>
      <svg width="30" height="16" viewBox="0 0 30 16"><rect width="30" height="16"></rect></svg></div></div>
      <div class="xgTips" style="display:none">广告视频不支持倍速功能</div></div>
    <div id="videoSideBar"><svg id="svg_icon_circle_close" width="38" height="38"
        onclick="document.getElementById('videoSideBar').style.display='none';
                 document.getElementById('videoSideCard').style.display='block'"><rect width="38" height="38"/></svg>
      <div>¥39.9</div><div>立即购买</div></div>
    <div id="videoSideCard" style="display:none"><div data-e2e="comment-list"><div data-e2e="comment-item">好可爱</div></div></div>
    </body>"""

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
            cls.pw = sync_playwright().start()
        except Exception as e:  # noqa: BLE001
            raise unittest.SkipTest(f"没有 Playwright：{e}")
        try:
            try:
                cls.br = cls.pw.chromium.launch()
            except Exception:  # noqa: BLE001
                cls.br = cls.pw.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
        except Exception as e:  # noqa: BLE001
            cls.pw.stop()
            raise unittest.SkipTest(f"没有可用的浏览器：{e}")

    @classmethod
    def tearDownClass(cls):
        cls.br.close()
        cls.pw.stop()

    def _feed(self, html):
        from app.douyin.browser import DouyinBrowser
        from app.douyin.feed import Feed
        from app.douyin.selectors import DEFAULT_SELECTORS
        ctx = self.br.new_context()
        page = ctx.new_page()
        page.set_content(html)
        b = DouyinBrowser.__new__(DouyinBrowser)
        b.ctx, b._page, b.sel = ctx, page, dict(DEFAULT_SELECTORS, tip_close=[])
        b.pause = lambda *a, **k: None
        f = Feed.__new__(Feed)
        f.b = b
        return f, page

    def test_ad_is_recognised(self):
        f, _ = self._feed(self.PAGE)
        self.assertTrue(f.is_ad())
        plain = self.PAGE.replace('<svg width="30" height="16"', '<span').replace("广告视频不支持倍速功能", "倍速")
        f2, _ = self._feed(plain)
        self.assertFalse(f2.is_ad())

    def test_product_card_closed_to_show_comments(self):
        f, page = self._feed(self.PAGE)
        self.assertFalse(page.locator("[data-e2e='comment-item']").first.is_visible())
        self.assertTrue(f._close_product_card())
        self.assertTrue(page.locator("[data-e2e='comment-item']").first.is_visible())
        self.assertFalse(f._close_product_card())                                    # 没有商品卡了：什么都不做

    def test_runner_skips_ads_by_default(self):
        from app.agent.runner import Agent
        agent = Agent(make_db())
        agent._want_run = True
        agent._sleep = lambda s: agent._want_run
        agent.brain = mock.Mock()
        with mock.patch("app.agent.runner.Feed") as FeedCls:
            feed = FeedCls.return_value
            feed.read_current.return_value = {"video_id": "ad1", "url": "u", "author": "MINISO", "desc": "猫爪拖鞋",
                                              "comments": [], "comment_items": [], "ad": True}
            agent._watch_one(mock.Mock(), agent.db.rules())
        self.assertTrue(feed.read_current.call_args.kwargs["skip_ad"])               # 广告不去翻评论区
        agent.brain.triage.assert_not_called()                                       # 不花模型的钱
        feed.like.assert_not_called()
        feed.next.assert_called_once()
        row = agent.db.query("SELECT decision, reason FROM videos WHERE video_id='ad1'")[0]
        self.assertEqual((row["decision"], row["reason"]), ("skip", "广告"))
        self.assertEqual(agent.db.usage().get("videos") or 0, 0)                     # 不算进“看视频”的额度

    def test_ads_watched_when_switch_off(self):
        from app.agent.runner import Agent
        db = make_db()
        r = db.rules()
        r["skip_ads"] = False
        db.set_json("rules", r)
        agent = Agent(db)
        agent._want_run = True
        agent._sleep = lambda s: agent._want_run
        agent.brain = mock.Mock()
        agent.brain.router.task_can_see.return_value = False
        agent.brain.triage.return_value = {"interest": 2, "topic": "", "reason": "广告", "cost": 0}
        with mock.patch("app.agent.runner.Feed") as FeedCls:
            feed = FeedCls.return_value
            feed.read_current.return_value = {"video_id": "ad2", "url": "u", "author": "x", "desc": "d",
                                              "comments": [], "comment_items": [], "ad": True}
            feed.capture_frames.return_value = []
            agent._watch_one(mock.Mock(), agent.db.rules())
        agent.brain.triage.assert_called_once()                                      # 关掉开关：广告也照常看



class CommentSendConfirmTests(unittest.TestCase):
    """发评论按了回车后要核实：评论区里出现了她发的这条才算发出去。"""

    def _feed(self, rows_seq, box_text=""):
        from app.douyin.feed import Feed
        f = Feed.__new__(Feed)
        f.b = mock.Mock()
        f.b.sel = {}
        f.b.find.return_value = mock.Mock()
        f.b.safe_click.return_value = True
        f.open_comments = mock.Mock(return_value=True)
        f.close_comments = mock.Mock()
        f._dump_comments_once = mock.Mock()
        seq = iter(rows_seq)
        last = {"v": []}

        def rows():
            last["v"] = next(seq, last["v"])
            return last["v"]
        f.comment_rows = rows
        f._box_text = lambda box: box_text
        return f

    def test_sent_when_comment_appears(self):
        with mock.patch("app.douyin.feed.cancel.sleep"):
            f = self._feed([[{"id": "1", "text": "旧的"}],
                            [{"id": "1", "text": "旧的"}, {"id": "2", "text": "这段太好笑了", "self": True}]])
            self.assertEqual(f.comment("这段太好笑了"), "sent")

    def test_failed_when_text_stuck_in_box(self):
        with mock.patch("app.douyin.feed.cancel.sleep"):
            f = self._feed([[{"id": "1", "text": "旧的"}]], box_text="这段太好笑了")
            r = f.comment("这段太好笑了")
        self.assertEqual(r, "failed")
        self.assertFalse(r)
        f._dump_comments_once.assert_called_once()


class CommentSendRetryAndMentionTests(unittest.TestCase):
    """2026-11-12 日志里的两个问题：按了回车字还留在输入框里；@ 好友时候选里找不到名字带标点的人（“雨.”）。"""

    def _feed(self):
        from app.douyin.feed import Feed
        f = Feed.__new__(Feed)
        f.b = mock.Mock()
        f.b.sel = {}
        f.b.safe_click.return_value = True
        f.open_comments = mock.Mock(return_value=True)
        f.close_comments = mock.Mock()
        f._dump_comments_once = mock.Mock()
        f._open_comment_box = mock.Mock(return_value=mock.Mock())
        f.comment_rows = lambda: []
        return f

    def test_enter_did_not_send_then_send_button_does(self):
        from app.douyin.feed import FAILED, SENT
        f = self._feed()
        f._confirm_sent = mock.Mock(side_effect=[FAILED, SENT])
        btn = mock.Mock(name="send")
        f.b.find.side_effect = lambda key, timeout=0, root=None: btn if key == "comment_send" else None
        with mock.patch("app.douyin.feed.cancel.sleep"):
            self.assertEqual(f.comment("这吃相笑死我了"), "sent")
        f.b.safe_click.assert_any_call(btn, "发送按钮")
        self.assertEqual(f._confirm_sent.call_count, 2)
        f._dump_comments_once.assert_not_called()

    def test_uncertain_is_never_resent(self):
        from app.douyin.feed import UNCERTAIN
        f = self._feed()
        f._confirm_sent = mock.Mock(return_value=UNCERTAIN)
        f.b.find.return_value = mock.Mock()
        with mock.patch("app.douyin.feed.cancel.sleep"):
            r = f.comment("这吃相笑死我了")
        self.assertTrue(r.uncertain)
        self.assertEqual(f._confirm_sent.call_count, 1)                  # 可能已经发出去了：不点发送按钮、不重发

    def test_send_button_selector_matches_real_page(self):
        from app.douyin.selectors import DEFAULT_SELECTORS, SELECTORS_VERSION
        self.assertIn("#FE2C55", DEFAULT_SELECTORS["comment_send"][0])   # 实测：红色箭头，在 commentInput-right-ct 里
        self.assertGreaterEqual(SELECTORS_VERSION, 23)

    def test_dump_is_once_per_kind_not_once_per_run(self):
        """以前一次运行只保存一次页面：01:02 先因为 @ 不上保存了，01:06 评论没发出去就没保存，查不了。"""
        from app.douyin.feed import Feed
        f = Feed.__new__(Feed)
        f.b = mock.Mock()
        with mock.patch.object(Feed, "_comments_dumped", set()):
            f._dump_comments_once("mention_fail", "a")
            f._dump_comments_once("comment_send_fail", "b")
            f._dump_comments_once("mention_fail", "c")
        self.assertEqual([c.args[0] for c in f.b.dump.call_args_list], ["mention_fail", "comment_send_fail"])

    def test_plain_name(self):
        from app.douyin.feed import plain_name
        self.assertEqual(plain_name("雨."), "雨")
        self.assertEqual(plain_name("布之道🇨🇳"), "布之道")
        self.assertEqual(plain_name("小 廖~"), "小廖")

    def test_mention_retries_with_plain_name_and_picks_exact(self):
        f = self._feed()
        typed = []
        f.b.type_slowly.side_effect = typed.append
        cand = mock.Mock(name="cand")
        f._mention_candidate = mock.Mock(side_effect=[None, cand])       # “雨.” 搜不到 → 只搜“雨”，候选里按整名认到了
        with mock.patch("app.douyin.feed.cancel.sleep"):
            self.assertTrue(f._insert_mention("雨."))
        self.assertEqual(typed, ["雨.", "雨"])
        self.assertEqual([c.args[0] for c in f._mention_candidate.call_args_list], ["雨.", "雨."])
        self.assertEqual(f.b.page.keyboard.press.call_count, 2)          # 只删掉“雨.”两个字，@ 留着
        f._dump_comments_once.assert_not_called()

    def test_mention_fail_saves_page_before_deleting(self):
        """以前先删掉输入的 @ 再保存页面：候选列表已经关了，保存下来的页面什么都看不出来。"""
        f = self._feed()
        order = []
        f._mention_candidate = mock.Mock(return_value=None)
        f._dump_comments_once.side_effect = lambda *a: order.append("dump")
        f.b.page.keyboard.press.side_effect = lambda k: order.append(k)
        with mock.patch("app.douyin.feed.cancel.sleep"):
            self.assertFalse(f._insert_mention("小明"))
        self.assertEqual(order[0], "dump")
        self.assertEqual(order.count("Backspace"), 3)                     # @ + 两个字

    def test_mention_candidate_prefers_exact_name(self):
        f = self._feed()
        f.b.sel = {"mention_item": ["[role='option']:visible"]}
        f.b.page.evaluate_handle.return_value.as_element.return_value = None     # 页面脚本没认出来：按定位找
        other, exact = mock.Mock(name="other"), mock.Mock(name="exact")
        other.inner_text.return_value = "清风细雨.\n抖音号：a1"
        exact.inner_text.return_value = "雨.\n抖音号：b2"
        f.b.page.locator.return_value.filter.return_value.all.return_value = [other, exact]
        self.assertIs(f._mention_candidate("雨."), exact)
        f.b.page.locator.return_value.filter.return_value.all.return_value = [other]
        self.assertIs(f._mention_candidate("雨."), other)                         # 没有一字不差的：退而求其次

    def test_required_mention_failing_means_no_comment(self):
        f = self._feed()
        f._insert_mention = mock.Mock(return_value=False)
        with mock.patch("app.douyin.feed.cancel.sleep"):
            r = f.comment("来啦～", mention="雨.", require_mention=True)
        self.assertEqual(r, "failed")
        f.b.type_slowly.assert_not_called()                               # 话没打出去
        f.b.page.keyboard.press.assert_not_called()                       # 也没按发送
        f.close_comments.assert_called_once()

    def test_runner_reports_why_reply_failed(self):
        from app.agent.runner import _fail_reason
        ib = mock.Mock()
        ib.last_reply_note = "在评论区里没找到对方那条回复"
        self.assertEqual(_fail_reason(ib), "在评论区里没找到对方那条回复")
        ib.last_reply_note = ""
        self.assertIn("发送失败", _fail_reason(ib))
        self.assertIn("发送失败", _fail_reason(mock.Mock()))              # 说不清的还是老说法


class ThreadCheckIndependentTests(unittest.TestCase):
    """“接着聊评论”不再挂在“自动回复 @”下面。"""

    def test_thread_only(self):
        from app.agent.runner import Agent
        db = make_db()
        r = db.rules()
        r["auto_reply_at"] = False
        r["auto_reply_thread"] = True
        r["auto_reply_dm"] = False
        db.set_json("rules", r)
        agent = Agent(db)
        agent._next_at = 0
        self.assertEqual(agent._inbox_due(mock.Mock(), db.rules()), (True, False))
        agent._inbox_epoch = agent._epoch_key(db.rules())
        with mock.patch("app.agent.runner.Inbox") as InboxCls:
            inbox = InboxCls.return_value
            inbox.fetch_mentions.return_value = []
            inbox.last_notice = {"opened": True, "rows": 3, "at": 0, "reply": 0}
            agent._ensure_browser = mock.Mock()
            agent._process_inbox(stop_on_pause=False, at=True, dm=False)
        self.assertEqual(inbox.fetch_mentions.call_args.kwargs["kinds"], ("reply",))
        self.assertEqual(agent.notice_checked["new_reply"], 0)

    def test_panel_fail_warns_once_an_hour(self):
        from app.agent.runner import Agent
        agent = Agent(make_db())
        inbox = mock.Mock(last_notice={"opened": False})
        agent._log_notice_check(inbox, ("at", "reply"), [])
        agent._log_notice_check(inbox, ("at", "reply"), [])
        n = agent.db.query("SELECT COUNT(*) AS n FROM events WHERE title='通知面板打不开'")[0]["n"]
        self.assertEqual(n, 1)


class CommentBoxAndVerifyTests(unittest.TestCase):
    """2026-11-09 实测：评论输入框没点开时只是占位文字；二次验证的遮罩挡着时要按“弹了验证”处理；审核不误伤。"""

    def test_placeholder_is_clicked_first(self):
        from app.douyin.feed import Feed
        f = Feed.__new__(Feed)
        f.b = mock.Mock()
        editor, opener = mock.Mock(name="editor"), mock.Mock(name="opener")
        state = {"opened": False}

        def find(key, timeout=0, root=None):
            if key == "comment_input":
                return editor if state["opened"] else None
            return opener if key == "comment_input_open" else None

        def click(loc, what=""):
            state["opened"] = state["opened"] or loc is opener
            return True
        f.b.find.side_effect = find
        f.b.safe_click.side_effect = click
        self.assertIs(f._open_comment_box(), editor)
        f.b.safe_click.assert_called_once()

    def test_selectors_match_real_page(self):
        from app.douyin.selectors import DEFAULT_SELECTORS as S
        self.assertIn(".comment-input-inner-container", S["comment_input_open"])
        self.assertIn("#uc-second-verify .second-verify-mask", S["second_verify"])

    def test_click_blocked_by_verify_mask_raises(self):
        from app.douyin.browser import CaptchaDetected, DouyinBrowser
        b = DouyinBrowser.__new__(DouyinBrowser)
        b.check_captcha = mock.Mock(side_effect=CaptchaDetected("x"))
        b.click = mock.Mock(side_effect=Exception(
            'Timeout 6000ms exceeded.\\n  - <div class="second-verify-mask">…</div> from <div id="uc-second-verify">…</div> '
            'subtree intercepts pointer events'))
        with self.assertRaises(CaptchaDetected):
            b.safe_click(mock.Mock(), "点赞按钮")
        b.click = mock.Mock(side_effect=Exception("Timeout 6000ms exceeded."))
        b.check_captcha.reset_mock()
        self.assertFalse(b.safe_click(mock.Mock(), "点赞按钮"))
        b.check_captcha.assert_not_called()

    def test_contact_words_not_overblocking(self):
        from app.agent import safety
        self.assertIsNone(safety.output_problem("蹲桌子吃庆功宴，加我一个行不行呀！"))
        self.assertIsNotNone(safety.output_problem("加我微信聊"))
        self.assertIsNotNone(safety.output_problem("加我好友吧"))

    def test_comment_review_prompt(self):
        from app.agent.brain import Brain
        b = Brain(make_db())
        fake = mock.Mock(text='{"ok": true, "reason": ""}', cost=0.0)
        with mock.patch.object(b.router, "call", return_value=fake) as call:
            self.assertTrue(b.safety_check("雪地里的猫", "像撒了糖霜的煤球，太治愈啦", kind="comment")[0])
        prompt = call.call_args.args[2]
        self.assertIn("不是谁对她说的话", prompt)
        self.assertIn("拿不准时判可以发送", prompt)
        self.assertIn("表白、调情", prompt)


class NoticeAndThreadSearchTests(unittest.TestCase):
    """2026-11-10 实测：通知面板一打开就读会读到 0 条；通知面板弹着会盖住私信的按钮；
    回复她的评论折叠在别人评论下面的一串回复里，要直接去那一串找。"""

    def _inbox(self):
        from app.douyin.inbox import Inbox
        ib = Inbox.__new__(Inbox)
        ib.b = mock.Mock()
        ib.b.sel = {}
        ib.feed = mock.Mock()
        ib.last_notice = {}
        ib._notice_blank = False
        return ib

    def test_notice_rows_wait_for_loading(self):
        ib = self._inbox()
        ib.b.find_all.side_effect = [[], [], ["r1", "r2"]]
        ib.b.text_of.return_value = ""
        with mock.patch("app.douyin.inbox.cancel.sleep"):
            self.assertEqual(ib._notice_rows(), ["r1", "r2"])
        self.assertEqual(ib.b.find_all.call_count, 3)

    def test_notice_rows_known_empty_returns_at_once(self):
        ib = self._inbox()
        ib.b.find_all.return_value = []
        ib.b.text_of.return_value = "互动消息 暂无消息"
        with mock.patch("app.douyin.inbox.cancel.sleep") as sl:
            self.assertEqual(ib._notice_rows(), [])
        sl.assert_not_called()
        self.assertTrue(ib._notice_blank)

    def test_dm_close_moves_mouse_off_notice_first(self):
        ib = self._inbox()
        state = {"notice": True, "dm": True}
        ib.b.find.side_effect = lambda key, timeout=0, root=None: (
            mock.Mock() if (key == "notice_panel" and state["notice"]) or (key == "dm_panel" and state["dm"]) else None)
        ib.b.page.mouse.move.side_effect = lambda x, y: state.update(notice=False)
        ib.b.sel = {"dm_close": ["svg[class*='closeImPage']:visible"]}
        btn = mock.Mock()
        btn.click.side_effect = lambda **kw: state.update(dm=False)
        ib.b.page.locator.return_value.all.return_value = [btn]
        with mock.patch("app.douyin.inbox.cancel.sleep"):
            ib.close_dm_panel()
        ib.b.page.mouse.move.assert_called()
        self.assertFalse(state["dm"])

    def test_dm_close_clicks_the_top_button_quietly(self):
        """2026-11-12 实测：聊天开着时有两个“关闭”叠在一起，前面那个被盖着（点它等 6 秒超时还记一条警告）。
        现在先点最上面（排在最后）的那个，点得到就不再碰前面的，也不记警告。"""
        ib = self._inbox()
        state = {"dm": True}
        ib.b.find.side_effect = lambda key, timeout=0, root=None: (
            mock.Mock() if key == "dm_panel" and state["dm"] else None)
        ib.b.sel = {"dm_close": ["svg[class*='closeImPage']:visible"]}
        covered, top = mock.Mock(name="covered"), mock.Mock(name="top")
        covered.click.side_effect = Exception("Timeout 6000ms exceeded. <path> intercepts pointer events")
        top.click.side_effect = lambda **kw: state.update(dm=False)
        ib.b.page.locator.return_value.all.return_value = [covered, top]
        with mock.patch("app.douyin.inbox.cancel.sleep"), mock.patch("app.douyin.inbox.log") as lg:
            ib.close_dm_panel()
        lg.warning.assert_not_called()
        top.click.assert_called_once()
        covered.click.assert_not_called()
        ib.b.safe_click.assert_not_called()

    def test_dm_close_falls_back_to_force_script_then_esc(self):
        ib = self._inbox()
        state = {"dm": True}
        ib.b.find.side_effect = lambda key, timeout=0, root=None: (
            mock.Mock() if key == "dm_panel" and state["dm"] else None)
        ib.b.sel = {"dm_close": ["svg[class*='closeImPage']:visible"]}
        btn = mock.Mock()
        btn.click.side_effect = Exception("intercepts pointer events")
        btn.evaluate.side_effect = lambda js: state.update(dm=False)       # 普通、强制都点不到，脚本点到了
        ib.b.page.locator.return_value.all.return_value = [btn]
        with mock.patch("app.douyin.inbox.cancel.sleep"):
            ib.close_dm_panel()
        self.assertEqual(btn.click.call_count, 2)
        self.assertFalse(state["dm"])
        ib.b.page.keyboard.press.assert_not_called()

    def test_reply_count(self):
        from app.douyin.inbox import Inbox
        self.assertEqual(Inbox._reply_count("展开13条回复"), 13)
        self.assertEqual(Inbox._reply_count("展开更多"), 0)

    def test_search_threads_skips_single_reply_threads(self):
        ib = self._inbox()
        one, many = mock.Mock(name="one"), mock.Mock(name="many")
        ib._thread_candidates = mock.Mock(return_value=[(one, "展开1条回复"), (many, "展开3条回复")])
        ib._expand_until = mock.Mock(return_value="HIT")
        self.assertEqual(ib._search_threads({"user": "u", "text": "t"}, min_replies=2), "HIT")
        ib._expand_until.assert_called_once()
        self.assertIs(ib._expand_until.call_args.args[0], many)

    def test_search_threads_scrolls_for_more_comments(self):
        """2026-11-12 实测：“展开了 0 串回复，用了 0 秒，没找到”——以前只看刚打开评论区时加载的那一二十条，
        回复她的那一串排在后面就永远找不到。现在看完了就往下翻、加载下一批接着找。"""
        ib = self._inbox()
        late = mock.Mock(name="late")
        batches = iter([[], [], [(late, "展开2条回复")]])
        ib._thread_candidates = mock.Mock(side_effect=lambda: next(batches, []))
        ib._expand_until = mock.Mock(return_value="HIT")
        ib._comments_end = mock.Mock(return_value=False)
        ib._load_more_comments = mock.Mock()
        sizes = iter([20, 40, 60, 80])
        ib.b.find_all.side_effect = lambda key, root=None: [0] * next(sizes)
        self.assertEqual(ib._search_threads({"user": "u", "text": "t"}, min_replies=2), "HIT")
        self.assertEqual(ib._load_more_comments.call_count, 2)

    def test_search_threads_stops_at_the_end(self):
        ib = self._inbox()
        ib._thread_candidates = mock.Mock(return_value=[])
        ib._comments_end = mock.Mock(return_value=True)
        ib._load_more_comments = mock.Mock()
        self.assertIsNone(ib._search_threads({"user": "u", "text": "t"}))
        ib._load_more_comments.assert_not_called()

    def test_search_threads_spots_comment_by_id_after_scrolling(self):
        ib = self._inbox()
        ib._thread_candidates = mock.Mock(return_value=[])
        ib._comments_end = mock.Mock(return_value=False)
        ib._load_more_comments = mock.Mock()
        ib.b.find_all.return_value = [0] * 20
        ib.feed.cid_selector.return_value = "#comment_77"
        loc = ib.b.page.locator.return_value.last
        loc.count.return_value = 1
        self.assertIs(ib._search_threads({"user": "u", "text": "t", "cid": "77"}), loc)

    def test_thread_candidates_come_from_one_page_script(self):
        """以前一条条评论去问“下面有没有展开按钮”，上千条评论光问就要一两分钟；现在一次读出来。"""
        from app.douyin import inbox as inbox_mod
        ib = self._inbox()
        ib.b.sel = {"comment_item": ["[data-e2e='comment-item']"],
                    "comment_expand": ["button.comment-reply-expand-btn", "button:has-text('条回复')"]}
        ib.b.page.evaluate.return_value = [{"key": "3", "label": "展开2条回复"}, {"bad": 1}]
        got = ib._thread_candidates()
        self.assertEqual([lbl for _, lbl in got], ["展开2条回复"])
        js, args = ib.b.page.evaluate.call_args.args
        self.assertIs(js, inbox_mod.THREADS_JS)
        self.assertEqual(args, ["[data-e2e='comment-item']", ["button.comment-reply-expand-btn"]])   # 只给脚本普通 CSS
        ib.b.page.locator.assert_called_with("[data-aiw-thread='3']")
        ib.b.page.evaluate.return_value = None
        self.assertEqual(ib._thread_candidates(), [])

    # ---------- 从通知点进去 / @ 回去 ----------
    def _item(self, kind="at"):
        return {"kind": kind, "video_url": "https://www.douyin.com/video/1", "user": "雨.", "text": "@大肥鱼 快来看",
                "mine": "", "root_cid": "", "cid": ""}

    def test_reply_tries_the_notice_itself_before_scrolling(self):
        """2026-11-12 实测：有人在评论上万条的视频下 @ 她，往下翻了 1200 多条、展开了 25 串都没找到。
        现在先像人一样从互动消息里点这条通知进去，在那儿找到就直接回复，不再往下翻。"""
        ib = self._inbox()
        ib.feed.open_comments.return_value = True
        ib._find_comment = mock.Mock(return_value=None)
        ib._reply_via_notice = mock.Mock(return_value="sent-result")
        ib._search_threads = mock.Mock()
        self.assertEqual(ib.reply_mention(self._item(), "来啦"), "sent-result")
        ib._search_threads.assert_not_called()
        self.assertEqual(ib._find_comment.call_count, 1)

    def test_reply_falls_through_when_notice_route_fails(self):
        ib = self._inbox()
        ib.feed.open_comments.return_value = True
        ib.feed.reply_on_item.return_value = True
        ib._reply_via_notice = mock.Mock(return_value=None)
        ib._find_comment = mock.Mock(side_effect=[None, "T"])        # 已加载的里没有 → 往下翻找到了
        self.assertTrue(ib.reply_mention(self._item(), "来啦"))
        ib.feed.reply_on_item.assert_called_once()
        self.assertEqual(ib.feed.reply_on_item.call_args.args[0], "T")

    def test_at_falls_back_to_mentioning_them(self):
        """@ 她的那条评论怎么都找不到：发一条 @ 对方的评论把话带到；@ 不上就不发。"""
        from app.douyin.feed import FAILED, SENT
        ib = self._inbox()
        ib.feed.open_comments.return_value = True
        ib._reply_via_notice = mock.Mock(return_value=None)
        ib._find_comment = mock.Mock(return_value=None)
        ib._search_threads = mock.Mock(return_value=None)
        ib._comments_end = mock.Mock(return_value=False)
        ib._dump_once = mock.Mock()
        ib.b.find_all.return_value = [0] * 1234
        ib.b.notice_comments = []
        ib.feed.comment.return_value = SENT
        self.assertTrue(ib.reply_mention(self._item(), "来啦"))
        ib.feed.comment.assert_called_once_with("来啦", mention="雨.", require_mention=True)
        self.assertIn("@雨.", ib.last_reply_note)
        self.assertEqual(ib._dump_once.call_args.args[0], "mention_comment_missing")
        ib.feed.comment.return_value = FAILED
        self.assertFalse(ib.reply_mention(self._item(), "来啦"))
        self.assertIn("没找到", ib.last_reply_note)

    def test_reply_kind_does_not_post_a_new_comment(self):
        ib = self._inbox()
        ib.feed.open_comments.return_value = True
        ib._reply_via_notice = mock.Mock(return_value=None)
        ib._find_comment = mock.Mock(return_value=None)
        ib._search_threads = mock.Mock(return_value=None)
        ib._comments_end = mock.Mock(return_value=True)
        ib._dump_once = mock.Mock()
        ib.b.find_all.return_value = [0] * 8
        ib.b.notice_comments = []
        self.assertFalse(ib.reply_mention(self._item("reply"), "嗯嗯"))
        ib.feed.comment.assert_not_called()
        self.assertIn("没找到对方那条回复", ib.last_reply_note)
        self.assertEqual(ib._dump_once.call_args.args[0], "mention_comment_missing_short")   # 评论没几条的另存一份

    def _via_notice_inbox(self, new_tab: bool):
        ib = self._inbox()
        row = mock.Mock(name="row")
        ib._notice_row = mock.Mock(return_value=row)
        ib.close_notice_panel = mock.Mock()
        ib._dump_once = mock.Mock()
        ib.b.find.return_value = None                      # 通知里没单独的“内容”元素：点整条通知
        ib.b._emu = {}
        page = ib.b.page = mock.MagicMock(name="page")
        page.url = "https://www.douyin.com/video/7696133565038729289"
        tab = mock.MagicMock(name="tab")
        tab.url = "https://www.douyin.com/video/7696133565038729289?from=notice"
        if new_tab:
            page.context.expect_page.return_value.__enter__.return_value.value = tab
        else:
            page.context.expect_page.return_value.__exit__.side_effect = Exception("没有新标签页")
        ib.feed.open_comments.return_value = True
        return ib, page, tab, row

    def test_via_notice_replies_in_the_tab_it_opens_then_comes_back(self):
        from app.douyin.feed import SENT
        ib, page, tab, row = self._via_notice_inbox(new_tab=True)
        seen_on = []
        ib._find_comment = mock.Mock(side_effect=lambda it, deep=True: seen_on.append(ib.b.page) or "T")
        ib.feed.reply_on_item.return_value = SENT
        self.assertEqual(ib._reply_via_notice(self._item(), "来啦"), SENT)
        row.click.assert_called_once()
        self.assertIs(seen_on[0], tab)                     # 在抖音自己打开的那个标签页里找、在那儿回复
        tab.close.assert_called_once()
        self.assertIs(ib.b.page, page)                     # 回到原来的页面
        ib.b._activate_tab.assert_called_once_with(page)

    def test_via_notice_gives_up_quietly_when_comment_is_not_there(self):
        ib, page, tab, row = self._via_notice_inbox(new_tab=True)
        ib._find_comment = mock.Mock(return_value=None)
        self.assertIsNone(ib._reply_via_notice(self._item(), "来啦"))
        ib.feed.reply_on_item.assert_not_called()
        tab.close.assert_called_once()
        self.assertIs(ib.b.page, page)
        self.assertEqual(ib._dump_once.call_args.args[0], "notice_open_no_comment")

    def test_via_notice_same_page(self):
        from app.douyin.feed import SENT
        ib, page, tab, row = self._via_notice_inbox(new_tab=False)
        ib._find_comment = mock.Mock(return_value="T")
        ib.feed.reply_on_item.return_value = SENT
        self.assertEqual(ib._reply_via_notice(self._item(), "来啦"), SENT)
        ib.close_notice_panel.assert_called()               # 通知面板还弹着会盖住评论区：先收起来
        tab.close.assert_not_called()

    def test_via_notice_ignores_a_tab_that_is_not_a_video(self):
        ib, page, tab, row = self._via_notice_inbox(new_tab=True)
        tab.url = "https://www.douyin.com/user/MS4wLjABAAAA"          # 点开的是对方主页
        ib._find_comment = mock.Mock()
        self.assertIsNone(ib._reply_via_notice(self._item(), "来啦"))
        ib.feed.open_comments.assert_not_called()
        tab.close.assert_called_once()
        self.assertIs(ib.b.page, page)

    def test_via_notice_clicks_the_video_cover(self):
        ib, page, tab, row = self._via_notice_inbox(new_tab=True)
        cover = row.locator.return_value.last
        cover.count.return_value = 1
        ib._find_comment = mock.Mock(return_value=None)
        ib._reply_via_notice(self._item(), "来啦")
        row.locator.assert_called_with("img")
        cover.click.assert_called_once()
        row.click.assert_not_called()

    def test_via_notice_needs_the_notice_row(self):
        ib, page, tab, row = self._via_notice_inbox(new_tab=True)
        ib._notice_row = mock.Mock(return_value=None)
        self.assertIsNone(ib._reply_via_notice(self._item(), "来啦"))
        ib.feed.open_comments.assert_not_called()

    def test_notice_row_matches_by_user_and_text(self):
        ib = self._inbox()
        ib.open_notice_panel = mock.Mock(return_value=True)
        ib._notice_rows = mock.Mock(return_value=["r1", "r2"])
        ib._parse_notice = mock.Mock(side_effect=lambda row, mine=None: (
            {"user": "别人", "text": "@大肥鱼 快来看"} if row == "r1" else {"user": "雨.", "text": "@大肥鱼 快来看"}))
        self.assertEqual(ib._notice_row(self._item()), "r2")

    def test_reply_goes_to_known_root_thread_first(self):
        ib = self._inbox()
        ib.feed.open_comments.return_value = True
        ib.feed.reply_on_item.return_value = True
        ib._find_comment = mock.Mock(return_value=None)
        ib._find_in_thread = mock.Mock(return_value="T")
        ib._search_threads = mock.Mock()
        it = {"kind": "reply", "video_url": "u", "user": "小鱼", "text": "下得去手", "mine": "真到删的时候",
              "root_cid": "", "root_text": "别的鱼全在抄那句", "root_user": "鱼崽崽"}
        self.assertTrue(ib.reply_mention(it, "嗯嗯"))
        ib._find_in_thread.assert_called_once_with(it, "", "别的鱼全在抄那句", "鱼崽崽")
        ib._search_threads.assert_not_called()

    def test_db_reply_parent(self):
        db = make_db()
        db.add_comment_action("7701", "v1", "鱼崽崽", "别的鱼全在抄那句", reply="真到删的时候下得去手吗")
        p = db.reply_parent("真到删的时候下得去手吗")
        self.assertEqual((p["cid"], p["user_name"]), ("7701", "鱼崽崽"))
        self.assertIsNone(db.reply_parent("没发过的话"))


# ====================== 记忆、主人、身份、规则（2026-11 第三轮） ======================

class MemoryTests(unittest.TestCase):
    """记忆：半衰期 30 天，用到就加强；淡到阈值以下忘掉；重要的、主人的不忘；评论区只记这一串。"""

    def setUp(self):
        from app.agent.memory import Memory
        self.db = make_db()
        self.mem = Memory(self.db, router=mock.Mock())

    def _age(self, days, where="1=1"):
        ts = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        self.db.execute(f"UPDATE memories SET touched_at=?, created_at=?, day=? WHERE {where}", (ts, ts, ts[:10]))

    def test_half_life_is_one_month(self):
        self.mem.remember("video", "看了一个橘猫视频", importance=3)
        row = self.db.query("SELECT * FROM memories")[0]
        self.assertEqual(row["half_life"], 30)
        s0 = self.mem.strength_now(row)
        self._age(30)
        row = self.db.query("SELECT * FROM memories")[0]
        self.assertAlmostEqual(self.mem.strength_now(row), s0 / 2, places=2)

    def test_recall_strengthens_and_stretches(self):
        self.mem.remember("video", "看了 @阿橘 的视频《橘猫打哈欠》（猫，9 分），点了赞", tags=["猫", "阿橘"])
        self._age(20)
        before = self.db.query("SELECT * FROM memories")[0]
        hit = self.mem.recall("橘猫", kinds=("video",))
        self.assertEqual(len(hit), 1)
        after = self.db.query("SELECT * FROM memories")[0]
        self.assertGreater(after["strength"], self.mem.strength_now(before))      # 强度回升
        self.assertAlmostEqual(after["half_life"], 45)                             # 半衰期变长
        self.assertEqual(after["recalls"], 1)

    def test_unrelated_is_not_recalled(self):
        self.mem.remember("video", "看了一个滑雪视频", tags=["滑雪"])
        self.assertEqual(self.mem.recall("今天吃什么火锅", kinds=("video",)), [])

    def test_fade_forgets_weak_keeps_important_and_owner(self):
        be_friends(self.db, '小廖')
        self.db.save_owners([{"name": "小廖", "call": "主人"}])
        self.mem.remember("video", "小事一桩", importance=1)
        self.mem.remember("note", "主人喜欢橘猫", importance=5)
        self.mem.remember_dm("小廖", "在吗", "在呀", owner=True)
        self.mem.remember_dm("路人甲", "你好", "你好呀")
        self._age(200)
        self.assertEqual(self.mem.fade(), 2)                                       # 小事和路人的私信忘了
        left = {r["summary"][:4] for r in self.db.query("SELECT summary FROM memories")}
        self.assertEqual(len(left), 2)
        self.assertTrue(any("主人喜欢" in x for x in left))

    def test_only_dm_friends_get_cards(self):
        self.mem.remember_dm("小鹿", "你喜欢猫吗", "超喜欢")
        self.mem.remember_thread("t1", "评论区路人", "说得对", "嘿嘿", {"desc": "猫"})
        self.mem.remember_at("另一个路人", "来看", "来啦", {"desc": "猫"})
        self.assertEqual([p["name"] for p in self.db.query("SELECT name FROM people")], ["小鹿"])
        th = self.db.query("SELECT * FROM memories WHERE kind='thread'")[0]
        self.assertEqual(th["person"], "评论区路人")                                # 记着是和谁聊的（以后 TA 来私信也想得起来）
        self.assertLess(th["half_life"], 7)                                        # 只在评论区聊过的人：很快淡忘

    def test_memory_is_shared_between_dm_and_comments(self):
        """私信和评论区的记忆是通的：不管在哪儿和她聊，都能想起另一边聊过的事。"""
        self.mem.remember_dm("小鹿", "我养了一只橘猫叫团子", "好可爱")
        ctx = self.mem.comment_context("小鹿", "你还记得我的猫吗")                 # 在评论区回小鹿
        self.assertIn("团子", ctx)
        self.assertIn("不要把私信里聊过的私事", ctx)                               # 公开场合：记得，但不当众说私事
        self.mem.remember_at("小鹿", "快来看这只柯基", "腿好短哈哈", {"desc": "柯基散步"})
        self.mem.remember_thread("t9", "小鹿", "柯基的屁股像面包", "真的像", {"desc": "柯基散步"})
        dm = self.mem.dm_context("小鹿", "上次那只柯基你还记得吗")                  # 回小鹿的私信
        self.assertIn("柯基", dm)
        self.assertNotIn("不要把私信里聊过的私事", dm)
        self.assertNotIn("柯基", self.mem.dm_context("别人", "上次那只柯基你还记得吗"))   # 别人的事不串
        self.assertEqual(self.mem.comment_context("没聊过的路人", "你好呀"), "")

    def test_friends_comment_memories_last_like_dm_ones(self):
        from app.agent.memory import HALF_LIFE_DAYS, THREAD_HALF_LIFE
        self.mem.remember_dm("小鹿", "在吗", "在呀")
        self.mem.remember_thread("t1", "小鹿", "说得对", "嘿嘿", {"desc": "猫"})
        self.mem.remember_thread("t2", "路人", "说得对", "嘿嘿", {"desc": "猫"})
        self.mem.remember_at("主人小廖", "来看", "来啦", {"desc": "猫"})
        be_friends(self.db, "主人小廖")
        self.db.save_owners([{"name": "主人小廖", "call": "主人"}])
        self.mem.remember_at("主人小廖", "再来看", "又来啦", {"desc": "狗"})
        hl = {(r["person"], r["scope"] or r["summary"][-4:]): r["half_life"]
              for r in self.db.query("SELECT * FROM memories WHERE kind IN ('thread','at')")}
        self.assertEqual(hl[("小鹿", "thread:t1")], HALF_LIFE_DAYS)                 # 私信好友：和私信里的事记得一样久
        self.assertEqual(hl[("路人", "thread:t2")], THREAD_HALF_LIFE)
        self.assertEqual(sorted(v for (p, _), v in hl.items() if p == "主人小廖"), [THREAD_HALF_LIFE, HALF_LIFE_DAYS])
        self.assertEqual(self.db.query("SELECT pending FROM people WHERE name='小鹿'")[0]["pending"], 2)   # 印象卡也记一笔
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM people")[0]["n"], 1)                     # 路人不建卡

    def test_card_update_reads_comment_interactions_too(self):
        self.mem.remember_dm("小鹿", "在吗", "在呀")
        self.mem.remember_at("小鹿", "快来看这只柯基", "腿好短哈哈", {"desc": "柯基散步"})
        self.mem.router = mock.Mock()
        self.mem.router.call.return_value = mock.Mock(text='{"card": "爱看狗", "style": "", "topics": "", "facts": ""}', cost=0.0)
        self.mem.update_card("小鹿", force=True)
        self.assertIn("你们在评论区的互动", self.mem.router.call.call_args.args[2])
        self.assertIn("柯基", self.mem.router.call.call_args.args[2])

    def test_thread_and_other_peoples_dms_do_not_leak(self):
        self.mem.remember_dm("小鹿", "我养了一只橘猫叫团子", "好可爱")
        self.mem.remember_thread("t1", "路人", "橘猫最胖", "哈哈", {"desc": "橘猫"})
        self.assertEqual(self.mem.recall("橘猫", kinds=()), [])                    # 不指定人：别人的私信、评论区都不带出来
        self.assertEqual(len(self.mem.recall("橘猫", person="小鹿")), 1)
        self.assertIn("团子", self.mem.dm_context("小鹿", "你还记得我的猫吗"))
        self.assertNotIn("团子", self.mem.dm_context("别人", "你还记得我的猫吗"))

    def test_same_video_is_one_memory(self):
        v = {"video_id": "v1", "author": "阿橘", "desc": "橘猫打哈欠"}
        self.mem.remember_video(v, 9, "猫", True, False, "")
        self.mem.remember_video(v, 9, "猫", True, False, "这哈欠也太大了")
        rows = self.db.query("SELECT * FROM memories")
        self.assertEqual(len(rows), 1)
        self.assertIn("这哈欠也太大了", rows[0]["summary"])
        self.mem.remember_video({"video_id": "v2", "author": "x", "desc": "一般"}, 7, "风景", False, False, "")
        self.assertEqual(len(self.db.query("SELECT * FROM memories")), 1)          # 没反应的普通视频不记

    def test_forget_person(self):
        self.db.add_dm("小鹿", "them", "你好")
        self.mem.remember_dm("小鹿", "你好", "你好呀")
        self.mem.forget_person("小鹿")
        self.assertEqual(self.db.query("SELECT * FROM people"), [])
        self.assertEqual(self.db.query("SELECT * FROM memories"), [])
        self.assertEqual(self.db.dm_history("小鹿"), [])

    def test_card_updates_after_a_few_chats(self):
        self.mem.router.call.return_value = mock.Mock(
            text='{"card": "爱猫的夜猫子", "style": "短句，爱用哈哈", "topics": "猫、游戏", "facts": "养了橘猫团子"}', cost=0.01)
        for i in range(3):
            self.db.add_dm("小鹿", "them", f"第{i}句")
            self.mem.remember_dm("小鹿", f"第{i}句", "嗯嗯")
        self.assertEqual(self.mem.update_card("小鹿"), 0.0)                        # 才聊 3 次：先不整理
        self.mem.remember_dm("小鹿", "第4句", "嗯嗯")
        self.assertEqual(self.mem.update_card("小鹿"), 0.01)
        p = self.mem.person("小鹿")
        self.assertEqual((p["style"], p["pending"]), ("短句，爱用哈哈", 0))
        self.assertIn("短句，爱用哈哈", self.mem.dm_context("小鹿", "在吗"))

    def test_diary_dedupes_and_asks_for_500_words(self):
        day = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        v = {"video_id": "v1", "author": "阿橘", "desc": "橘猫打哈欠"}
        self.mem.remember_video(v, 9, "猫", True, False, "")
        self.mem.remember_video(v, 9, "猫", True, False, "又刷到了")
        for i in range(6):
            self.mem.remember_dm("小鹿", f"第{i}句", "嗯")
        self._age(1)
        self.assertEqual(self.mem.diary_due(), day)
        items = self.mem.day_material(day)
        self.assertEqual(sum("橘猫打哈欠" in x for x in items), 1)                 # 同一个视频只写一次
        self.assertEqual(sum("小鹿" in x for x in items), 3)                       # 同一个人挑几句就够
        self.mem.router.call.return_value = mock.Mock(text="今天刷到一只橘猫……", cost=0.02)
        self.assertEqual(self.mem.write_diary(day, "系统提示", "开心"), 0.02)
        self.assertIn("500 字", self.mem.router.call.call_args.args[2])
        self.assertEqual(self.mem.diary_due(), "")
        self.assertIn("橘猫", self.mem.identity_context())                         # 下次说话时记得昨天的自己

    def test_roll_up_week_and_month(self):
        self.mem.router.call.return_value = mock.Mock(text="合并后的内容", cost=0.01)
        for d in ("2026-10-05", "2026-10-06", "2026-10-13"):
            self.db.execute("INSERT INTO diary(level, period, content, created_at) VALUES('day',?,?,?)", (d, "日记" + d, d))
        self.mem.roll_up("系统提示", now=datetime(2026, 11, 20))
        got = {(r["level"], r["period"]) for r in self.db.query("SELECT level, period FROM diary")}
        self.assertIn(("week", "2026-W41"), got)
        self.assertIn(("week", "2026-W42"), got)
        self.assertIn(("month", "2026-10"), got)
        self.assertNotIn(("day", "2026-10-05"), got)                               # 一个多月前的日记只留周记


class OwnerTests(unittest.TestCase):
    """主人：自定义称呼和关系；回复不设上限；主动聊天、分享视频；身份里对主人更亲近但仍然承认是 AI。"""

    def setUp(self):
        from app.agent.runner import Agent
        self.db = make_db()
        be_friends(self.db, '小廖')
        self.db.save_owners([{"name": "小廖", "call": "哥哥", "relation": "把我带到这个世界的人"}])
        self.agent = Agent(self.db)
        self.agent.brain = mock.Mock()
        self.agent.brain.reply_dm.return_value = mock.Mock(ok=True, text="在呀哥哥", cost=0.0, tokens=0, reason="")
        self.inbox = mock.Mock()
        self.inbox.reply_dm.return_value = True

    def _dm(self, user, text):
        mid = self.db.add_message(kind="dm", ext_id=f"dm_{user}_{text}", user_name=user, content=text, status="pending")
        self.agent._handle_incoming(self.inbox, mid, {"user": user, "text": text}, "dm", self.db.rules())
        return self.db.query("SELECT * FROM messages WHERE id=?", (mid,))[0]

    def test_prompt_for_owner(self):
        from app.agent.persona import system_prompt
        o = self.db.owner("小廖")
        sp = system_prompt(self.db.persona(), owner=o)
        self.assertIn("你叫 TA「哥哥」", sp)
        self.assertIn("把我带到这个世界的人", sp)
        self.assertIn("必须如实说自己是 AI", sp)
        self.assertNotIn("不和任何人发展恋爱", sp)
        self.assertIn("不和任何人发展恋爱", system_prompt(self.db.persona()))       # 对别人还是保持距离

    def test_no_reply_cap_for_owner(self):
        r = self.db.rules()
        r["per_user_daily"] = 1
        r["limits"]["replies_dm"] = 1
        self.db.set_json("rules", r)
        for i in range(4):
            self.assertEqual(self._dm("小廖", f"第{i}句")["status"], "replied")
        self.assertEqual(self.agent.brain.reply_dm.call_args.kwargs["owner"]["name"], "小廖")
        self.assertEqual(self.agent.budget.remaining("replies_dm"), 1)             # 和主人聊天不占别人的私信额度
        self.assertEqual(self._dm("路人", "你好")["status"], "replied")
        self.assertNotEqual(self._dm("路人", "再问一句")["status"], "replied")      # 别人照常受上限限制
        self.assertGreater(self.db.owner("小廖")["closeness"], 0)                   # 聊得越多越亲近

    def test_owner_skips_sensitive_guard(self):
        m = self._dm("小廖", "想你了")                                               # 对别人会命中“情感依赖”
        self.assertEqual(m["reply"], "在呀哥哥")
        self.assertIn("不方便聊", self._dm("路人乙", "想你了")["reply"])

    def _wait(self, minutes: float, frac: float = 0.5):
        """把“上次和主人说话”拨到 minutes 分钟以前。"""
        self.agent._owner_next["小廖"] = (time.time() - minutes * 60, frac)

    def test_proactive_schedule_and_send(self):
        a = self.agent
        a.logged_in = True
        rules = self.db.rules()
        a.mood.current = mock.Mock(return_value={"key": "calm", "label": "平静"})
        self.assertIsNone(a._owner_due(rules))                                       # 刚启动：过一会儿再找
        self._wait(60)
        self.assertIsNone(a._owner_due(rules))                                       # 平静的时候 1 小时还不找
        self._wait(181)
        self.assertEqual(a._owner_due(rules)["name"], "小廖")
        a.brain.owner_hello.return_value = mock.Mock(ok=True, text="哥哥，我今天刷到一只超胖的橘猫", cost=0, tokens=0,
                                                     sticker=None)
        with mock.patch("app.agent.runner.Inbox") as InboxCls:
            InboxCls.return_value.reply_dm.return_value = True
            a._owner_reach_out(mock.Mock(), self.db.owner("小廖"), rules)
        self.assertLess(time.time() - a._owner_next["小廖"][0], 5)                   # 找过了：重新计时
        self.assertIsNone(a._owner_due(rules))
        self.assertEqual(a._today_events("owner_chat"), 1)
        self.assertEqual(self.db.dm_history("小廖")[-1]["content"], "哥哥，我今天刷到一只超胖的橘猫")

    def test_no_daily_count_for_reaching_out(self):
        """不设“每天主动找主人聊几次”：今天已经找过很多次了，到时候照样可以找（以前到次数就不找了）。"""
        a = self.agent
        a.logged_in = True
        self.assertNotIn("owner_chat_per_day", self.db.rules())
        a.mood.current = mock.Mock(return_value={"key": "happy", "label": "开心"})
        for i in range(8):
            self.db.add_event("owner_chat", "主动找主人聊天", f"第 {i} 次")
        self.db.add_dm("小廖", "them", "刚忙完")
        self._wait(120)
        self.assertEqual(a._owner_due(dict(self.db.rules(), owner_chat_per_day=1))["name"], "小廖")

    def test_mood_decides_how_often(self):
        from app.agent.runner import OWNER_GAP_MIN
        a = self.agent
        a.logged_in = True
        rules = self.db.rules()
        self._wait(70)
        a.mood.current = mock.Mock(return_value={"key": "clingy", "label": "想撒娇"})
        self.assertIsNotNone(a._owner_due(rules))                                    # 想撒娇：一个小时就忍不住了
        a.mood.current = mock.Mock(return_value={"key": "sleepy", "label": "犯困"})
        self.assertIsNone(a._owner_due(rules))                                       # 犯困：同样的时间还不找
        self.assertLess(OWNER_GAP_MIN["clingy"][1], OWNER_GAP_MIN["calm"][0])
        self.assertLess(OWNER_GAP_MIN["calm"][1], OWNER_GAP_MIN["sleepy"][1])
        # 越亲近隔得越短
        o = self.db.owner("小廖")
        self.assertLess(a._owner_gap(dict(o, closeness=90), 0.5), a._owner_gap(dict(o, closeness=0), 0.5))

    def test_backs_off_when_owner_does_not_answer(self):
        a = self.agent
        a.logged_in = True
        rules = self.db.rules()
        a.mood.current = mock.Mock(return_value={"key": "clingy", "label": "想撒娇"})
        self.db.add_dm("小廖", "them", "我去开会了")
        for t in ("好的哥哥", "哥哥开完会了吗", "[发了一张表情包：委屈｜理理我]", "我刷到一只橘猫"):
            self.db.add_dm("小廖", "me", t)
        self.assertEqual(self.db.owner_unanswered("小廖")[0], 3)                     # 表情包不算一句
        self._wait(300)
        self.assertIsNone(a._owner_due(rules))                                       # 连着三句没回：先不打扰
        self.db.execute("UPDATE dm_history SET ts='2020-01-01 08:00:00' WHERE role='me'")
        self.assertIsNotNone(a._owner_due(rules))                                    # 隔了大半天：可以再问候一次
        self.db.add_dm("小廖", "them", "开完啦")
        self.assertEqual(self.db.owner_unanswered("小廖")[0], 0)

    def test_replying_to_owner_restarts_the_clock(self):
        self._wait(500)
        self._dm("小廖", "在吗")
        self.assertLess(time.time() - self.agent._owner_next["小廖"][0], 5)

    def test_only_mutual_friends_can_become_owner(self):
        refused = self.db.save_owners([{"name": "小廖", "call": "哥哥"}, {"name": "路人甲"}, {"name": "手动加的"}])
        self.assertEqual(refused, ["路人甲", "手动加的"])
        self.assertEqual([o["name"] for o in self.db.owners()], ["小廖"])
        self.db.save_friend("name:手动加的", {"nickname": "手动加的", "mutual": True, "manual": True})
        self.assertEqual(self.db.save_owners([{"name": "小廖"}, {"name": "手动加的"}]), ["手动加的"])   # 手动添加的没核实过：不行
        self.db.save_friend("uid_x", {"nickname": "不互关", "mutual": False})
        self.assertEqual(self.db.mutual_names(), ["小廖"])
        # 以前设好的主人不受影响：名单里暂时没有 TA（比如还没刷新）也照旧保留
        self.db.execute("DELETE FROM friends")
        self.assertEqual(self.db.save_owners([{"name": "小廖", "call": "主人"}]), [])
        self.assertEqual(self.db.owner("小廖")["call"], "主人")

    def test_owner_dm_is_watched_even_when_dm_budget_is_gone(self):
        a = self.agent
        r = self.db.rules()
        r["limits"]["replies_dm"] = 0
        self.db.set_json("rules", r)
        self.assertFalse(a._dm_for_all(r))
        self.assertTrue(a._dm_watch(r))                                              # 额度用完了：还是去看（只回主人）
        self.assertTrue(a._dm_watch(dict(r, auto_reply_dm=False)))                   # 关了自动回复私信：主人的照样回
        self.assertFalse(a._dm_watch(dict(r, owner_enabled=False)))
        self.db.save_owners([])
        self.assertFalse(a._dm_watch(r))

    def test_only_owner_conversations_are_opened_when_budget_is_gone(self):
        a = self.agent
        r = self.db.rules()
        r["limits"]["replies_dm"] = 0
        self.db.set_json("rules", r)
        a._inbox_epoch = a._epoch_key(r)
        a._ensure_browser = mock.Mock()
        a._want_run = True
        with mock.patch("app.agent.runner.Inbox") as InboxCls:
            ib = InboxCls.return_value
            ib.fetch_mentions.return_value = []
            ib.fetch_unread_dms.return_value = []
            a._process_inbox(at=False, dm=True)
            self.assertEqual(ib.fetch_unread_dms.call_args.kwargs["only"], {"小廖"})
            r["limits"]["replies_dm"] = 30
            self.db.set_json("rules", r)
            a._process_inbox(at=False, dm=True)
            self.assertIsNone(ib.fetch_unread_dms.call_args.kwargs["only"])          # 平时：所有人的都看
            a._process_inbox(at=False, dm=True, owners_only=True)                    # 活跃时段以外
            self.assertEqual(ib.fetch_unread_dms.call_args.kwargs["only"], {"小廖"})

    def test_owner_is_answered_outside_active_hours(self):
        a = self.agent
        a._want_run = True
        a.logged_in = True
        a._ensure_browser = mock.Mock()
        a._ensure_browser.return_value.is_logged_in.return_value = True
        a._process_inbox = mock.Mock()
        a._sleep = mock.Mock()
        a._next_dm = 0
        with mock.patch("app.agent.runner.in_active_hours", return_value=False), \
                mock.patch.object(a, "_run_commands", create=True), mock.patch.object(a, "_memory_upkeep"):
            a._tick()
        a._process_inbox.assert_called_once_with(at=False, dm=True, owners_only=True)
        self.assertEqual(a.state, "outside")
        a._process_inbox.reset_mock()
        self.db.save_owners([])
        a._next_dm = 0
        with mock.patch("app.agent.runner.in_active_hours", return_value=False), mock.patch.object(a, "_memory_upkeep"):
            a._tick()
        a._process_inbox.assert_not_called()                                         # 没设主人：夜里不看私信

    def test_baseline_leaves_owner_dms_alone(self):
        a = self.agent
        ib = mock.Mock()
        ib.fetch_mentions.return_value = []
        ib.fetch_unread_dms.return_value = []
        mid = self.db.add_message(kind="dm", ext_id="p1", user_name="小廖", content="在吗", status="pending")
        mid2 = self.db.add_message(kind="dm", ext_id="p2", user_name="路人", content="在吗", status="pending")
        a._baseline_inbox(ib, self.db.rules(), ())
        self.assertEqual(ib.fetch_unread_dms.call_args.kwargs["skip"], {"小廖"})     # 主人的未读不点开、不算旧消息
        st = {r["id"]: r["status"] for r in self.db.query("SELECT id, status FROM messages")}
        self.assertEqual((st[mid], st[mid2]), ("pending", "skipped_old"))

    def test_unread_filter_in_inbox(self):
        from app.douyin.inbox import Inbox
        ib = Inbox.__new__(Inbox)
        ib.b = mock.Mock()
        ib.sec_uids = {}
        ib.open_dm_panel = mock.Mock(return_value=True)
        ib._back_to_list = mock.Mock(return_value=True)
        ib._has_unread = mock.Mock(return_value=True)
        ib.b.find_all.return_value = ["c1", "c2"]
        ib._conv_user = mock.Mock(side_effect=lambda c: {"c1": "路人", "c2": "小廖"}[c])
        ib._conv_avatar = mock.Mock(return_value="")
        opened = []
        ib._open_conv = mock.Mock(side_effect=lambda u: opened.append(u) or False)
        with mock.patch("app.douyin.inbox.react_values", return_value=[]):
            ib.fetch_unread_dms(only={"小廖"})
            self.assertEqual(opened, ["小廖"])                                       # 别人的会话不点开，未读留着
            opened.clear()
            ib.fetch_unread_dms(skip={"小廖"})
            self.assertEqual(opened, ["路人"])

    def test_share_video(self):
        a = self.agent
        with mock.patch("app.agent.runner.Inbox") as InboxCls:
            InboxCls.return_value.reply_dm.return_value = True
            a._owner_share(mock.Mock(), self.db.owner("小廖"), "这只猫好像你",
                           {"url": "https://www.douyin.com/video/1", "desc": "橘猫", "author": "阿橘"})
            sent = InboxCls.return_value.reply_dm.call_args.args[1]
        self.assertIn("这只猫好像你", sent)
        self.assertIn("douyin.com/video/1", sent)
        self.assertEqual(len(a._owners_for_share(dict(self.db.rules(), owner_share_per_day=1))), 0)

    def test_save_owners_keeps_closeness(self):
        self.db.owner_touch("小廖", closer=7)
        be_friends(self.db, '小廖', '阿宁')
        self.db.save_owners([{"name": "小廖", "call": "主人", "relation": ""}, {"name": "阿宁", "call": "姐姐"}])
        self.assertEqual(self.db.owner("小廖")["closeness"], 7)
        self.assertEqual(len(self.db.owners()), 2)
        be_friends(self.db, '阿宁')
        self.db.save_owners([{"name": "阿宁"}])
        self.assertIsNone(self.db.owner("小廖"))


class RulesRound3Tests(unittest.TestCase):
    """取消点赞倾向；同一视频只评论一次；关键字自动回复；敏感话题开关；回复里不带“AI虚拟人”。"""

    def _agent(self):
        from app.agent.runner import Agent
        db = make_db()
        r = db.rules()
        r["interest_threshold"] = 4          # 心情（跟几点钟有关）怎么变都会看下去
        db.set_json("rules", r)
        agent = Agent(db)
        agent._want_run = True
        agent._sleep = lambda s: agent._want_run
        agent.likes.rnd = mock.Mock(random=mock.Mock(return_value=0.99))   # 不会“没耐心直接划走”
        agent.brain = mock.Mock()
        agent.brain.router.task_can_see.return_value = True
        agent.brain.safety_check.return_value = (True, "", 0.0)
        agent.brain.triage.return_value = {"interest": 8, "topic": "猫", "reason": "", "cost": 0}
        agent.listener = mock.Mock(_broken="")
        agent.listener.listen.return_value = "今天带大家看看我家的猫"
        return agent

    def _watch(self, agent, dec, vid="v1"):
        from app.douyin.feed import SENT
        agent.brain.decide.return_value = {"interest": 8, "like": False, "comment": "", "reason": "", "cost": 0, **dec}
        with mock.patch("app.agent.runner.Feed") as FeedCls:
            feed = FeedCls.return_value
            feed.read_current.return_value = {"video_id": vid, "url": "u", "author": "阿橘", "desc": "橘猫", "comments": []}
            feed.capture_frames.side_effect = lambda n, d, sleep=None: [b"x"] * n
            feed.comment.return_value = SENT
            feed.like.return_value = True
            agent._watch_one(mock.Mock(), agent.db.rules())
        return feed

    def test_model_decides_like_and_gets_memory_and_speech(self):
        agent = self._agent()
        for i in range(8):
            agent.db.add_video(video_id=f"o{i}", author="x", interest=8, decision="like", liked=1, topic="猫")
        feed = self._watch(agent, {"like": True})
        args, kw = agent.brain.decide.call_args.args, agent.brain.decide.call_args.kwargs
        self.assertTrue(args[2])                                                    # 让不让赞只看额度，喜不喜欢她自己定
        self.assertIn("点赞了 8 个", kw["memory"])                                  # 记忆里有她最近的习惯
        self.assertEqual(args[0]["speech"], "今天带大家看看我家的猫")               # 听到的话也给了模型
        feed.like.assert_called_once()
        self.assertEqual(len(agent.db.query("SELECT * FROM memories WHERE kind='video'")), 1)

    def test_like_respects_model_no_and_daily_pace(self):
        agent = self._agent()
        self._watch(agent, {"like": False}).like.assert_not_called()
        agent.db.add_usage("likes", 40)
        self._watch(agent, {"like": True}, vid="v2").like.assert_not_called()       # 今天的额度用完了
        self.assertFalse(agent.brain.decide.call_args.args[2])

    def test_same_video_commented_only_once_forever(self):
        agent = self._agent()
        feed = self._watch(agent, {"comment": "这猫也太会了"})
        feed.comment.assert_called_once()
        self.assertTrue(agent.db.commented_video("v1"))
        agent.db.execute("UPDATE videos SET created_at='2025-01-01 00:00:00'")       # 过了很久
        feed2 = self._watch(agent, {"comment": "又来啦"})
        self.assertEqual(agent.brain.decide.call_count, 1)                           # 推荐页再刷到：直接划过去
        feed2.comment.assert_not_called()
        # 从别的入口（比如别人分享）又看到它：可以点赞，但不再让她发新评论
        agent.db.seen_video = lambda vid: False
        feed3 = self._watch(agent, {"comment": "再评一次"})
        self.assertFalse(agent.brain.decide.call_args.args[3])
        feed3.comment.assert_not_called()

    def test_no_tendency_setting_left(self):
        from app.defaults import DEFAULT_RULES
        import app.agent.likes as likes
        self.assertNotIn("like_tendency", DEFAULT_RULES)
        self.assertFalse(hasattr(likes, "TENDENCY"))
        pol = likes.LikePolicy(make_db())
        rules = {"limits": {"likes": 40}, "active_start": "09:00", "active_end": "23:00"}
        self.assertTrue(pol.judge(6, "a", "猫", rules, used=0, now=datetime(2026, 11, 8, 12, 0))["ok"])
        self.assertFalse(pol.judge(8, "a", "猫", rules, used=20, now=datetime(2026, 11, 8, 10, 0))["ok"])   # 赞太快
        self.assertTrue(pol.judge(10, "a", "猫", rules, used=20, now=datetime(2026, 11, 8, 10, 0))["ok"])

    def test_keyword_auto_reply_skips_model(self):
        from app.agent import safety
        rules = {"auto_replies": [{"keys": "价格，多少钱", "reply": "这个不卖哦～"}, {"keys": "在吗", "reply": "在呀"}]}
        self.assertEqual(safety.auto_reply("这个多少钱呀", rules), "这个不卖哦～")
        self.assertEqual(safety.auto_reply("你好", rules), "")
        agent = self._agent()
        r = agent.db.rules()
        r["auto_replies"] = rules["auto_replies"]
        agent.db.set_json("rules", r)
        inbox = mock.Mock()
        inbox.reply_dm.return_value = True
        mid = agent.db.add_message(kind="dm", ext_id="k1", user_name="路人", content="在吗", status="pending")
        agent._handle_incoming(inbox, mid, {"user": "路人", "text": "在吗"}, "dm", agent.db.rules())
        m = agent.db.query("SELECT * FROM messages WHERE id=?", (mid,))[0]
        self.assertEqual((m["status"], m["reply"]), ("replied", "在呀"))
        agent.brain.reply_dm.assert_not_called()

    def test_sensitive_guard_off_replies_normally(self):
        agent = self._agent()
        r = agent.db.rules()
        r["sensitive_guard"] = False
        agent.db.set_json("rules", r)
        agent.brain.reply_dm.return_value = mock.Mock(ok=True, text="这个我不太懂呀", cost=0.0, tokens=0, reason="")
        inbox = mock.Mock()
        inbox.reply_dm.return_value = True
        mid = agent.db.add_message(kind="dm", ext_id="s1", user_name="路人", content="买哪只股票好", status="pending")
        agent._handle_incoming(inbox, mid, {"user": "路人", "text": "买哪只股票好"}, "dm", agent.db.rules())
        self.assertEqual(agent.db.query("SELECT reply FROM messages WHERE id=?", (mid,))[0]["reply"], "这个我不太懂呀")

    def test_replies_do_not_say_ai_label(self):
        from app.agent.persona import douyin_bio, system_prompt
        sp = system_prompt(make_db().persona())
        self.assertNotIn("你是抖音上的 AI 虚拟", sp)
        self.assertIn("不要用“AI虚拟人”", sp)
        self.assertIn("必须如实说自己是 AI", sp)                                    # 被问到还是如实说
        self.assertIn("【AI虚拟人】", douyin_bio({"name": "雨雨", "signature": "嗨"}))   # 主页简介的标识不变


class PersonaRound3Tests(unittest.TestCase):
    def test_richer_persona_in_prompt(self):
        from app.agent.persona import system_prompt
        p = {"name": "雨雨", "archetype": "tsundere", "self_name": "本小姐", "emote": "none", "mbti": "ENFP",
             "catchphrases": "哼", "catch_freq": "often", "extra": "看到猫就走不动道",
             "traits": {"humor": 90, "clingy": 10}}
        sp = system_prompt(p, memory="你最近一篇日记（2026-11-09）：今天刷到一只橘猫")
        for want in ("用“本小姐”自称", "傲娇", "非常爱玩梗", "非常独立", "ENFP", "经常用", "看到猫就走不动道",
                     "不用颜文字和 emoji", "今天刷到一只橘猫"):
            self.assertIn(want, sp)
        self.assertIn("很少用", system_prompt({**p, "catch_freq": "rare"}))

    def test_api_saves_new_fields(self):
        from app.api import Api
        api = Api.__new__(Api)
        api._db = make_db()
        api._pet = None
        Api.save_persona(api, {"archetype": "cool", "catch_freq": "rare", "emote": "emoji", "self_name": " 人家 ",
                               "mbti": "infp", "extra": "x" * 3000})
        p = api._db.persona()
        self.assertEqual((p["archetype"], p["catch_freq"], p["emote"], p["self_name"], p["mbti"]),
                         ("cool", "rare", "emoji", "人家", "INFP"))
        self.assertEqual(len(p["extra"]), 1500)
        Api.save_persona(api, {"archetype": "乱写", "catch_freq": "x"})
        self.assertEqual((api._db.persona()["archetype"], api._db.persona()["catch_freq"]), ("", "sometimes"))

    def test_brain_uses_one_identity_everywhere(self):
        from app.agent.brain import Brain
        db = make_db()
        b = Brain(db)
        db.execute("INSERT INTO diary(level, period, content, created_at) VALUES('day','2026-11-09','今天刷到一只橘猫','x')")
        self.assertIn("今天刷到一只橘猫", b.system)
        be_friends(db, '小廖')
        db.save_owners([{"name": "小廖", "call": "哥哥"}])
        self.assertIn("哥哥", b.system_for(db.owner("小廖")))


class ListenTests(unittest.TestCase):
    def test_media_urls_collected_from_feed_json(self):
        from app.douyin.browser import DouyinBrowser
        b = DouyinBrowser.__new__(DouyinBrowser)
        b.media = {}
        b._collect_media({"aweme_list": [{"aweme_id": "77", "video": {"play_addr": {"url_list": ["https://v.example/a.mp4"]}}},
                                         {"aweme_id": "78", "video": {}}]})
        self.assertEqual(b.media_urls("77"), ["https://v.example/a.mp4"])
        self.assertEqual(b.media_urls("78"), [])

    def test_listen_is_quiet_when_unavailable(self):
        import sys
        from app.agent.listen import Listener
        ls = Listener()
        b = mock.Mock()
        b.media_urls.return_value = []
        self.assertEqual(ls.listen(b, "77"), "")                                     # 拿不到视频地址：当没听到
        b.media_urls.return_value = ["https://v.example/a.mp4"]
        # 假装这台电脑没装听声音的组件（打包的电脑上是装了的，所以这里要明确“假装没装”）
        with mock.patch.dict(sys.modules, {"faster_whisper": None}):
            self.assertEqual(ls.listen(b, "77"), "")                                 # 没装组件：不报错
        b.ctx.request.get.assert_not_called()

    def test_bundled_model_is_used_without_download(self):
        import sys
        import types
        from app.agent import listen
        d = Path(tempfile.mkdtemp())
        m = d / "faster-whisper-base"
        m.mkdir()
        (m / "model.bin").write_bytes(b"x")
        (m / "config.json").write_text("{}", encoding="utf-8")
        made = []
        fake = types.ModuleType("faster_whisper")
        fake.WhisperModel = lambda *a, **kw: made.append((a, kw)) or "MODEL"
        with mock.patch.object(listen, "BUNDLED_DIR", d), mock.patch.dict(sys.modules, {"faster_whisper": fake}):
            ls = listen.Listener()
            self.assertEqual(ls._load("base"), "MODEL")
            self.assertEqual(made[0][0][0], str(m))                                  # 用的是安装包里自带的文件夹
            self.assertTrue(made[0][1]["local_files_only"])                           # 不联网
            st = ls.status()
            self.assertEqual((st["bundled"], st["models"]), (["base"], ["base"]))
            ls2 = listen.Listener()
            ls2._load("small")                                                       # 没自带的大小：还是第一次用时下载
            self.assertEqual(made[1][0][0], "small")
            self.assertIn("download_root", made[1][1])

    def _mp4(self, cut: bool = False) -> str:
        import subprocess
        from app.publish.narrate import ffmpeg_exe
        d = Path(tempfile.mkdtemp())
        out = d / "a.mp4"
        subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                        "-f", "lavfi", "-i", "color=c=blue:s=160x120:d=4", "-c:v", "libx264", "-c:a", "aac",
                        "-shortest", "-movflags", "faststart", str(out)], check=True)
        if cut:                                   # 和真的一样：只下载了开头一段，文件结尾是断的
            data = out.read_bytes()
            out.write_bytes(data[: len(data) * 2 // 3])
        return str(out)

    def test_audio_is_decoded_by_bundled_ffmpeg(self):
        """2026-11-12 日志：每个视频都报 open() got an unexpected keyword argument 'metadata_errors'（159 次）——
        faster-whisper 自己解声音要用 PyAV，PyAV 新版本去掉了这个参数。改成用自带的 ffmpeg 解，不走那条路。"""
        from app.agent import listen
        a = listen.decode_audio(self._mp4())
        self.assertEqual(str(a.dtype), "float32")
        self.assertAlmostEqual(len(a) / listen.SAMPLE_RATE, 4.0, delta=0.3)
        cut = listen.decode_audio(self._mp4(cut=True))                   # 断尾的文件：解出来多少用多少
        self.assertGreater(len(cut), listen.SAMPLE_RATE)
        self.assertEqual(len(listen.decode_audio(str(Path(tempfile.mkdtemp()) / "none.mp4"))), 0)

    def test_transcribe_gets_samples_not_the_file(self):
        from app.agent import listen

        class Model:
            def __init__(self):
                self.got = None

            def transcribe(self, audio, **kw):
                self.got = audio
                return iter([types.SimpleNamespace(text=" 大家好 "), types.SimpleNamespace(text="")]), None

        import types
        ls = listen.Listener()
        m = Model()
        ls._load = lambda size: m
        self.assertEqual(ls.transcribe_file(self._mp4()), "大家好")
        self.assertEqual(type(m.got).__name__, "ndarray")                 # 给模型的是解好的声音，不是文件路径
        m.got = None
        with mock.patch.object(listen, "decode_audio", return_value=listen.decode_audio("/nope/x.mp4")):
            self.assertEqual(ls.transcribe_file("x.mp4"), "")            # 没解出声音：当没听到，不去麻烦模型
        self.assertIsNone(m.got)
        with mock.patch.object(listen, "decode_audio", return_value=None):
            ls.transcribe_file("x.mp4")                                  # ffmpeg 用不了：才把文件交给 faster-whisper
        self.assertEqual(m.got, "x.mp4")

    def test_same_component_error_stops_after_three_videos(self):
        from app.agent import listen
        ls = listen.Listener()
        ls._load = lambda size: object()
        b = mock.Mock()
        b.media_urls.return_value = ["https://v.example/a.mp4"]
        resp = b.ctx.request.get.return_value
        resp.status, resp.body.return_value = 206, b"x" * 30000
        boom = TypeError("open() got an unexpected keyword argument 'metadata_errors'")
        with mock.patch.object(ls, "transcribe_file", side_effect=boom), mock.patch.object(listen.log, "warning") as warn:
            for _ in range(5):
                self.assertEqual(ls.listen(b, "77"), "")
        self.assertEqual(b.ctx.request.get.call_count, listen.SAME_ERROR_LIMIT)   # 第 4 个视频起不再下载、不再试
        warn.assert_called_once()
        self.assertIn("metadata_errors", ls.status()["broken"])
        # 个别视频自己的问题（解不出来）不算：不会因此停掉
        ls2 = listen.Listener()
        ls2._load = lambda size: object()
        with mock.patch.object(ls2, "transcribe_file", side_effect=RuntimeError("Invalid data found")):
            for _ in range(5):
                ls2.listen(b, "77")
        self.assertEqual(ls2.status()["broken"], "")

    def test_spec_and_build_bundle_the_model(self):
        root = Path(__file__).resolve().parent.parent
        self.assertIn("assets", (root / "aiWannaTikTok.spec").read_text(encoding="utf-8").split("语音模型")[1][:400])
        self.assertIn("tools\\fetch_whisper.py base", (root / "build.bat").read_text(encoding="utf-8"))
        sys_path = str(root / "tools")
        import importlib.util
        spec = importlib.util.spec_from_file_location("fetch_whisper", root / "tools" / "fetch_whisper.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        d = Path(tempfile.mkdtemp())
        self.assertFalse(mod.complete(d))
        for n in mod.NEEDED:
            (d / n).write_bytes(b"x")
        self.assertTrue(mod.complete(d))

    def test_browser_is_muted(self):
        import inspect
        from app.douyin import browser
        self.assertIn("--mute-audio", inspect.getsource(browser))

    def test_speech_goes_into_video_block(self):
        from app.agent.brain import _video_block
        self.assertIn("今天带大家看看", _video_block({"author": "a", "desc": "d", "speech": "今天带大家看看我家的猫"}))


class MessageCenterKeepTests(unittest.TestCase):
    """消息中心：私信记录一直留着；评论区的记录，和这个人 5 天没有新一轮对话就整组删掉；
    删掉以后同一条通知也不会被当成新的再回一遍。同一个人只有一个框。"""

    def setUp(self):
        from datetime import datetime, timedelta
        self.db = make_db()
        self.now = datetime(2026, 11, 12, 12, 0, 0)
        self.ago = lambda d, h=0: (self.now - timedelta(days=d, hours=h)).strftime("%Y-%m-%d %H:%M:%S")

    def _msg(self, kind, ext, user, days, status="replied", reply="好呀", handled=None):
        return self.db.add_message(kind=kind, ext_id=ext, user_name=user, content=f"{user} 说的话", status=status,
                                   reply=reply, created_at=self.ago(days),
                                   handled_at=self.ago(handled) if handled is not None else self.ago(days))

    def test_dm_stays_comments_go_after_five_days(self):
        self._msg("dm", "d1", "小鹿", 300)
        self._msg("at", "a1", "路人甲", 6)
        self._msg("reply", "r1", "路人甲", 5.1, status="skipped_old", reply="")
        self._msg("at", "a2", "路人乙", 4)
        n = self.db.purge_comment_messages(now=self.now)
        self.assertEqual(n, 2)
        left = {m["ext_id"] for m in self.db.query("SELECT ext_id FROM messages")}
        self.assertEqual(left, {"d1", "a2"})                                         # 私信一直在；还没到 5 天的也在

    def test_a_new_round_keeps_the_whole_conversation(self):
        self._msg("at", "a1", "路人甲", 9)
        self._msg("reply", "r1", "路人甲", 2)                                        # 两天前又聊起来了
        self._msg("at", "a3", "路人丙", 9, handled=1)                                # 9 天前的，她昨天才回
        self.assertEqual(self.db.purge_comment_messages(now=self.now), 0)
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM messages")[0]["n"], 3)

    def test_same_person_dm_is_untouched_when_comments_expire(self):
        self._msg("dm", "d1", "小鹿", 30)
        self._msg("at", "a1", "小鹿", 30)
        self.assertEqual(self.db.purge_comment_messages(now=self.now), 1)
        self.assertEqual([m["kind"] for m in self.db.query("SELECT kind FROM messages")], ["dm"])

    def test_deleted_notice_is_not_answered_again(self):
        self._msg("at", "a1", "路人甲", 6, reply="哈哈这只猫也太胖了")
        self._msg("reply", "r1", "路人甲", 6, status="skipped_old", reply="")
        self.db.purge_comment_messages(now=self.now)
        self.assertTrue(self.db.message_exists("a1"))                                # 记录没了，回执还在
        self.assertTrue(self.db.message_exists("r1"))
        self.assertFalse(self.db.message_exists("别的"))
        self.assertIn("哈哈这只猫也太胖了", self.db.my_comment_texts())               # 以后有人回这句，还认得是她说的
        gone = {g["ext_id"]: g["reply"] for g in self.db.query("SELECT * FROM gone_messages")}
        self.assertEqual(gone["r1"], "")
        # 回执也不会永远留着
        from datetime import timedelta
        self.db.purge_comment_messages(now=self.now + timedelta(days=self.db.GONE_KEEP_DAYS + 1))
        self.assertFalse(self.db.message_exists("a1"))

    def test_runner_and_api_both_tidy_up(self):
        from app.api import Api
        self._msg("at", "a1", "路人甲", 400)
        self._msg("dm", "d1", "路人甲", 400)
        api = Api(self.db, mock.Mock())
        out = api.get_messages("all")["data"]
        self.assertEqual([m["ext_id"] for m in out["items"]], ["d1"])
        self.assertEqual(out["comment_keep_days"], 5)
        self._msg("at", "a2", "路人甲", 400)
        api.get_messages("all")                                                      # 一小时内不重复整理
        self.assertTrue(self.db.query("SELECT 1 FROM messages WHERE ext_id='a2'"))
        src = (ROOT / "app" / "agent" / "runner.py").read_text(encoding="utf-8")
        self.assertIn("self.db.purge_comment_messages()", src)

    def test_comment_items_carry_the_dm_profile(self):
        """同一个人的私信和评论区消息合在一个框里：评论区那几条也带着 TA 的头像、昵称。"""
        from app.api import Api
        self.db.save_dm_user("小鹿", {"nickname": "小鹿🦌", "avatar": "https://a/1.jpg"})
        self._msg("dm", "d1", "小鹿", 1)
        self._msg("at", "a1", "小鹿", 0)
        self._msg("at", "a2", "路人", 0)
        items = {m["ext_id"]: m for m in Api(self.db, mock.Mock()).get_messages("all")["data"]["items"]}
        self.assertEqual(items["a1"]["user"]["nickname"], "小鹿🦌")
        self.assertIsNone(items["a2"]["user"])

    def test_ui_groups_one_box_per_person(self):
        js = (ROOT / "web" / "js" / "app.js").read_text(encoding="utf-8")
        fn = js[js.index("function msgGroups()"):js.index("function renderInbox()")]
        self.assertIn('const key = m.user_name || "";', fn)                          # 按人分组，不再分“私信 / 评论区”两个框
        self.assertNotIn('"d:"', fn)
        self.assertIn("私信 + 评论区", fn)
        self.assertIn("天没有新一轮对话就自动删除", js)


class DraftDeleteAndPreviewTests(unittest.TestCase):
    """作品：点了“不发了”的可以删除；图片放大看、视频和配音可以播放（本机小服务）。"""

    def setUp(self):
        from app.api import Api
        self.db = make_db()
        self.tmp = Path(tempfile.mkdtemp())
        self.patch = mock.patch("app.api.DRAFTS_DIR", self.tmp)
        self.patch2 = mock.patch("app.agent.runner.DRAFTS_DIR", self.tmp)
        self.patch.start()
        self.patch2.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.patch2.stop)
        self.api = Api(self.db, mock.Mock())

    def _draft(self, status="wish"):
        from PIL import Image
        outside = self.tmp / "my_upload.jpg"
        Image.new("RGB", (30, 40), "red").save(outside)
        did = self.db.add_draft(status=status, kind="image", title="今天的小事", text="t", tags=[], cards=[], media=[
            {"name": "card_1.jpg", "kind": "image", "source": "card"},
            {"name": "voice.mp4", "kind": "video", "source": "voice", "audio": "voice.mp3"},
            {"name": "my_upload.jpg", "kind": "image", "source": "upload", "path": str(outside)}],
            reason="", source="auto")
        d = self.tmp / str(did)
        d.mkdir(parents=True)
        Image.new("RGB", (108, 144), "pink").save(d / "card_1.jpg")
        (d / "voice.mp4").write_bytes(b"0123456789" * 100)
        (d / "voice.mp3").write_bytes(b"mp3-bytes")
        (d / "secret.txt").write_text("not a draft file")
        return did, outside

    def test_only_discarded_can_be_deleted(self):
        did, outside = self._draft("wish")
        r = self.api.delete_draft(did)
        self.assertFalse(r["ok"])
        self.assertIn("不发了", r["error"])
        self.assertIsNotNone(self.db.draft(did))
        self.api.discard_draft(did)
        self.assertTrue(self.api.delete_draft(did)["ok"])
        self.assertIsNone(self.db.draft(did))
        self.assertFalse((self.tmp / str(did)).exists())                             # 卡片、配音一起删
        self.assertTrue(outside.exists())                                            # 你电脑上的原文件不动
        self.assertEqual(self.api.list_drafts()["data"]["drafts"], [])

    def test_preview_urls_and_local_server(self):
        import urllib.error
        import urllib.request
        did, outside = self._draft()
        view = {m["name"]: m for m in self.api.get_draft(did)["data"]["media_view"]}
        self.assertTrue(view["card_1.jpg"]["url"].startswith("http://127.0.0.1:"))   # 只在本机
        self.assertTrue(view["voice.mp4"]["audio_url"].endswith("/voice.mp3"))
        self.assertEqual(view["card_1.jpg"]["audio_url"], "")
        # 不走系统代理：电脑上开着代理软件（Clash 之类）时，Python 会把发往 127.0.0.1 的请求也交给代理，
        # 代理连不到就回 502——那是测试自己的请求方式不对，不是预览服务的问题（软件界面访问本机地址不经过代理）
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        get = lambda u, h=None: direct.open(urllib.request.Request(u, headers=h or {}), timeout=5)
        with get(view["card_1.jpg"]["url"]) as r:
            self.assertEqual(r.headers["Content-Type"], "image/jpeg")
            self.assertEqual(r.read(), (self.tmp / str(did) / "card_1.jpg").read_bytes())
        with get(view["voice.mp4"]["url"], {"Range": "bytes=10-19"}) as r:           # 视频能拖进度条
            self.assertEqual((r.status, r.read()), (206, b"0123456789"))
            self.assertEqual(r.headers["Content-Range"], "bytes 10-19/1000")
            self.assertEqual(r.headers["Content-Type"], "video/mp4")
        with get(view["voice.mp4"]["audio_url"]) as r:
            self.assertEqual((r.headers["Content-Type"], r.read()), ("audio/mpeg", b"mp3-bytes"))
        with get(view["my_upload.jpg"]["url"]) as r:                                 # 你上传的（在草稿文件夹外面）也能看
            self.assertEqual(r.read(), outside.read_bytes())
        base = view["card_1.jpg"]["url"].rsplit("/", 1)[0]
        for bad in (base + "/secret.txt",                                            # 草稿里没登记的文件
                    base + "/..%2F..%2Ft.db",
                    base.replace(f"/draft/{did}", "/draft/99999") + "/card_1.jpg",
                    view["card_1.jpg"]["url"].replace(base.split("/")[3], "wrong-token")):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                get(bad)
            self.assertEqual(cm.exception.code, 404)

    def test_range_parsing(self):
        from app.media_server import parse_range
        self.assertEqual(parse_range("bytes=0-", 100), (0, 99))
        self.assertEqual(parse_range("bytes=10-19", 100), (10, 19))
        self.assertEqual(parse_range("bytes=90-500", 100), (90, 99))
        self.assertEqual(parse_range("bytes=-10", 100), (90, 99))
        for bad in ("bytes=100-", "bytes=5-2", "items=0-1", "bytes=0-1,5-6", "bytes=a-b", ""):
            self.assertIsNone(parse_range(bad, 100))

    def test_open_with_system_player_only_for_draft_files(self):
        did, _ = self._draft()
        with mock.patch("app.api._open_folder") as op:
            self.assertTrue(self.api.open_draft_media(did, "voice.mp4")["ok"])
            op.assert_called_once()
            self.assertFalse(self.api.open_draft_media(did, "secret.txt")["ok"])
            op.assert_called_once()

    def test_ui_has_preview_and_delete(self):
        js = (ROOT / "web" / "js" / "app.js").read_text(encoding="utf-8")
        for want in ('data-act="d-view"', "function lightbox(", "<video controls autoplay", "<audio controls autoplay",
                     'data-act="d-remove"', '"d-remove": async', 'call("delete_draft"'):
            self.assertIn(want, js)
        self.assertIn('d.status === "discarded" ?', js)                              # 只有“不发了”的才有删除按钮
        self.assertNotIn("draft_voice_audio", js)                                    # 配音不再整段塞进网页里
        css = (ROOT / "web" / "css" / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".lightbox", css)


class OwnerUiTests(unittest.TestCase):
    def test_owner_is_picked_from_mutual_friends(self):
        js = (ROOT / "web" / "js" / "app.js").read_text(encoding="utf-8")
        fn = js[js.index("function ownersSection(r)"):js.index("/* @ 互关好友：下拉框只显示互关的好友")]
        self.assertIn('id="own-pick"', fn)                                           # 从互关好友里选，不能手填名字
        self.assertNotIn("own-name", js)
        self.assertIn('data-act="fr-fetch"', fn)                                     # 刷新好友列表
        self.assertIn("刷新好友列表", fn)
        self.assertNotIn("r-ownchat", js)                                            # 不再设“每天主动找主人聊天几次”
        self.assertNotIn("owner_chat_per_day", js)
        self.assertIn("!f.manual", js)                                               # 手动添加的好友不能设为主人

    def test_api_lists_choices_and_refuses_strangers(self):
        from app.api import Api
        db = make_db()
        agent = mock.Mock()
        agent.listener.status.return_value = {}
        api = Api(db, agent)
        be_friends(db, "小廖", "阿宁")
        api.add_friend("手动加的")
        r = api.get_rules()["data"]
        self.assertEqual(r["owner_choices"], ["小廖", "阿宁"])
        res = api.save_rules({"owners": [{"name": "小廖", "call": "哥哥"}, {"name": "手动加的"}, {"name": "陌生人"}]})["data"]
        self.assertEqual(res["owners_refused"], ["手动加的", "陌生人"])
        owners = api.get_rules()["data"]["owners"]
        self.assertEqual([(o["name"], o["mutual"]) for o in owners], [("小廖", True)])
        self.assertNotIn("owner_chat_per_day", r)


class MemoryApiTests(unittest.TestCase):
    def setUp(self):
        from app.agent.memory import Memory
        from app.api import Api
        self.db = make_db()
        agent = mock.Mock()
        agent.memory = Memory(self.db, router=mock.Mock())
        agent.listener.status.return_value = {"installed": False, "models": []}
        self.mem = agent.memory
        self.api = Api(self.db, agent)

    def test_list_set_importance_delete(self):
        self.mem.remember("video", "看了一个橘猫视频", importance=2)
        rows = self.api.get_memory("events")["data"]["events"]
        self.assertFalse(rows[0]["kept"])
        self.api.memory_action("importance", rows[0]["id"], 5)
        self.assertTrue(self.api.get_memory("events")["data"]["events"][0]["kept"])  # 设成重要：不会忘
        self.api.memory_action("delete", rows[0]["id"])
        self.assertEqual(self.api.get_memory("events")["data"]["events"], [])

    def test_people_and_forget(self):
        self.mem.remember_dm("小鹿", "你好", "你好呀")
        people = self.api.get_memory("people")["data"]["people"]
        self.assertEqual((people[0]["name"], people[0]["n"]), ("小鹿", 1))
        self.api.memory_action("forget_person", "小鹿")
        self.assertEqual(self.api.get_memory("people")["data"]["people"], [])

    def test_rules_roundtrip_owners_and_auto_replies(self):
        be_friends(self.db, "小廖")
        self.api.save_rules({"owners": [{"name": "小廖", "call": "哥哥", "relation": "r"}],
                             "auto_replies": [{"keys": "在吗", "reply": "在呀"}, {"keys": "", "reply": "空的不存"}],
                             "sensitive_guard": False, "listen_model": "tiny"})
        r = self.api.get_rules()["data"]
        self.assertEqual(r["owners"][0]["call"], "哥哥")
        self.assertEqual(r["auto_replies"], [{"keys": "在吗", "reply": "在呀"}])
        self.assertEqual((r["sensitive_guard"], r["listen_model"]), (False, "tiny"))

    def test_about_lists_open_source(self):
        names = [o["name"] for o in self.api.about()["data"]["oss"]]
        for want in ("pywebview", "Playwright for Python", "faster-whisper", "ONNX Runtime", "SQLite"):
            self.assertIn(want, names)

    def test_messages_mark_owner(self):
        be_friends(self.db, '小廖')
        self.db.save_owners([{"name": "小廖", "call": "哥哥"}])
        self.db.add_message(kind="dm", ext_id="a", user_name="小廖", content="在吗", status="replied")
        self.db.add_message(kind="dm", ext_id="b", user_name="路人", content="hi", status="replied")
        items = {m["user_name"]: m["owner"] for m in self.api.get_messages("all")["data"]["items"]}
        self.assertEqual(items, {"小廖": "哥哥", "路人": None})


class StickerAndEmojiTests(unittest.TestCase):
    """抖音自带的小表情；她自己的表情包（用桌宠形象画的），私信里发 GIF、评论里配 PNG。"""

    def test_tag_is_parsed_and_stripped(self):
        from app.publish import stickers
        self.assertEqual(stickers.split_tag("好呀好呀～\n〔表情包：开心｜冲鸭〕"), ("好呀好呀～", {"mood": "开心", "text": "冲鸭"}))
        self.assertEqual(stickers.split_tag("嗯嗯 [表情包:哭]"), ("嗯嗯", {"mood": "委屈", "text": ""}))
        self.assertEqual(stickers.split_tag("没有标记"), ("没有标记", None))
        self.assertEqual(stickers.split_tag("嗯〔表情包：乱写的〕"), ("嗯", None))        # 认不出的情绪：只去掉标记

    def test_files_are_made(self):
        from PIL import Image
        from app.publish import stickers
        d = Path(tempfile.mkdtemp())
        png = stickers.make(d, "比心", "爱你哟", animated=False)
        gif = stickers.make(d, "晚安", "睡啦", animated=True)
        im = Image.open(png)
        self.assertEqual((im.format, im.size), ("PNG", (stickers.SIZE, stickers.SIZE)))
        g = Image.open(gif)
        self.assertEqual((g.format, g.n_frames, g.size), ("GIF", stickers.GIF_FRAMES, (stickers.GIF_SIZE, stickers.GIF_SIZE)))
        self.assertLess(gif.stat().st_size, 900 * 1024)
        self.assertEqual(len(stickers.mood_names()), len(set(stickers.mood_names())))
        for alias, mood in stickers.ALIASES.items():
            self.assertIn(mood, stickers.MOODS, alias)

    def test_brain_strips_tag_and_offers_stickers_only_when_allowed(self):
        from app.agent.brain import Brain
        b = Brain(make_db())
        b.safety_check = mock.Mock(return_value=(True, "", 0.0))
        fake = mock.Mock(text="晚安呀，做个好梦\n〔表情包：晚安｜盖好被子〕", cost=0.0, tokens=5)
        with mock.patch.object(b.router, "call", return_value=fake) as call:
            d = b.reply_dm("小廖", [], "我去睡啦", sticker=True)
            self.assertIn("〔表情包：情绪｜配字〕", call.call_args.args[2])
            d2 = b.reply_dm("小廖", [], "我去睡啦")
            self.assertNotIn("表情包", call.call_args.args[2])
        self.assertEqual((d.text, d.sticker), ("晚安呀，做个好梦", {"mood": "晚安", "text": "盖好被子"}))
        self.assertEqual(d2.text, "晚安呀，做个好梦")                                 # 没让配也会把标记去掉，不会发出去

    def test_decide_returns_sticker_for_comment(self):
        from app.agent.brain import Brain
        b = Brain(make_db())
        fake = mock.Mock(text='{"comment": "这猫会后空翻", "sticker": {"mood": "震惊", "text": "还有这种操作"}}', cost=0.0)
        with mock.patch.object(b.router, "call", return_value=fake) as call:
            d = b.decide({"author": "a", "desc": "d", "comments": []}, [], False, True, can_sticker=True)
            self.assertIn('"sticker"', call.call_args.args[2])
            d2 = b.decide({"author": "a", "desc": "d", "comments": []}, [], False, True)
        self.assertEqual(d["sticker"], {"mood": "惊讶", "text": "还有这种操作"})
        self.assertIsNone(d2["sticker"])

    def test_douyin_emoji_line_follows_switch(self):
        from app.agent.brain import Brain
        from app.agent.persona import DOUYIN_EMOJI
        db = make_db()
        self.assertIn("[比心]", Brain(db).system)
        r = db.rules()
        r["douyin_emoji"] = False
        db.set_json("rules", r)
        self.assertNotIn("[比心]", Brain(db).system)
        self.assertEqual(len(DOUYIN_EMOJI), len(set(DOUYIN_EMOJI)))

    def test_runner_sends_sticker_after_dm_and_respects_daily_cap(self):
        from app.agent.runner import Agent
        agent = Agent(make_db())
        rules = agent.db.rules()
        inbox = mock.Mock()
        inbox.send_dm_file.return_value = True
        agent._dm_sticker(inbox, "小廖", {"mood": "开心", "text": "好耶"}, rules)
        path = inbox.send_dm_file.call_args.args[1]
        self.assertTrue(str(path).endswith(".gif") and Path(path).exists())
        self.assertEqual(agent._today_events("sticker"), 1)
        self.assertIn("表情包", agent.db.dm_history("小廖")[-1]["content"])
        rules["sticker_per_day"] = 1
        self.assertFalse(agent._sticker_ok(rules, "dm"))                             # 今天的张数用完了
        agent._dm_sticker(inbox, "小廖", {"mood": "开心", "text": "好耶"}, rules)
        self.assertEqual(inbox.send_dm_file.call_count, 1)
        rules["sticker_per_day"] = 10
        self.assertFalse(agent._sticker_ok(rules, "comment"))                        # 评论里默认不配

    def _dm_inbox(self):
        from app.douyin.inbox import Inbox
        ib = Inbox.__new__(Inbox)
        ib.b = mock.Mock()
        ib.b.sel = {"dm_file_input": ["[data-e2e='msg-input'] input[type='file']"]}
        ib._chat_title = mock.Mock(return_value="小廖")
        ib._dump_once = mock.Mock()
        ib.b.find.return_value = None
        loc = ib.b.page.locator.return_value.first
        loc.count.return_value = 1
        return ib, loc

    def test_dm_file_goes_to_hidden_upload_input(self):
        ib, loc = self._dm_inbox()
        ib._dm_newest = mock.Mock(return_value=[])
        counts = iter([3, 3, 4])
        ib.b.find_all.side_effect = lambda key, root=None: [0] * next(counts)
        with mock.patch("app.douyin.inbox.cancel.sleep"):
            self.assertTrue(ib.send_dm_file("小廖", "/tmp/a.gif"))
        loc.set_input_files.assert_called_once_with("/tmp/a.gif")

    def test_dm_file_sent_is_seen_even_when_count_stays_the_same(self):
        """2026-11-12 实测：聊天窗口只留最近 12 条，表情包发出去了条数还是 12，被误报成“没看到它出现在聊天里”。
        现在看最新一条：是她发的、是图片、和发之前不一样，就算发出去了。"""
        ib, loc = self._dm_inbox()
        text = {"me": True, "image": False, "text": "不是啥意思呀", "sig": "不是啥意思呀|"}
        pic = {"me": True, "image": True, "text": "", "sig": "|img:372254:AAOw=="}
        states = iter([[text], [text], [pic, text]])
        ib._dm_newest = mock.Mock(side_effect=lambda n=3: next(states))
        ib.b.find_all.return_value = [0] * 12                                         # 条数一直是 12
        with mock.patch("app.douyin.inbox.cancel.sleep"):
            self.assertTrue(ib.send_dm_file("小廖", "/tmp/a.gif"))
        ib._dump_once.assert_not_called()

    def test_dm_file_not_sent_when_nothing_changes(self):
        ib, loc = self._dm_inbox()
        pic = {"me": True, "image": True, "text": "", "sig": "|img:1:x"}             # 发之前最新一条就是张图
        ib._dm_newest = mock.Mock(return_value=[pic])
        ib.b.find_all.return_value = [0] * 12
        with mock.patch("app.douyin.inbox.cancel.sleep"):
            self.assertFalse(ib.send_dm_file("小廖", "/tmp/a.gif"))
        ib._dump_once.assert_called_once()

    def test_dm_file_their_new_message_is_not_mistaken_for_ours(self):
        ib, loc = self._dm_inbox()
        mine = {"me": True, "image": False, "text": "好呀", "sig": "好呀|"}
        theirs = {"me": False, "image": True, "text": "", "sig": "|img:9:y"}         # 这时对方正好发来一张图
        states = iter([[mine]])
        ib._dm_newest = mock.Mock(side_effect=lambda n=3: next(states, [theirs, mine]))
        ib.b.find_all.return_value = [0] * 12
        with mock.patch("app.douyin.inbox.cancel.sleep"):
            self.assertFalse(ib.send_dm_file("小廖", "/tmp/a.gif"))

    def test_comment_image_is_attached_before_typing(self):
        from app.douyin.feed import Feed
        f = Feed.__new__(Feed)
        f.b = mock.Mock()
        f.b.sel = {"comment_file_input": [".comment-input-container input[type='file']"]}
        loc = f.b.page.locator.return_value.first
        loc.count.return_value = 1
        f.b.find.return_value = mock.Mock()
        self.assertTrue(f._attach_image("/tmp/a.png"))
        loc.set_input_files.assert_called_once_with("/tmp/a.png")
        f.b.find.return_value = None                                                 # 没看到缩略图
        f._dump_comments_once = mock.Mock()
        self.assertFalse(f._attach_image("/tmp/a.png"))


class VoiceTests(unittest.TestCase):
    """发作品时用她的声音（BreezeBlue 的 Breeze TTS 2）读文案，和文字卡片合成视频。"""

    def _db(self, **rules):
        from app.publish import voice
        db = make_db()
        voice.save_key(db, "bz-test-key")
        r = db.rules()
        r.update(voice_name="voc_abc123", **rules)
        db.set_json("rules", r)
        return db

    def test_speech_text_is_cleaned(self):
        from app.publish import voice
        t = voice.speech_text("周末的小确幸", "今天去了海边🌊，风好大呀～～(｡･ω･｡)ﾉ♡ 看到一只柯基[比心]！！ #海边 #柯基 https://x.com/a")
        self.assertEqual(t, "周末的小确幸。\n今天去了海边，风好大呀～ 看到一只柯基！")
        self.assertLessEqual(len(voice.speech_text("", "长" * 3000)), voice.MAX_CHARS)

    def test_key_is_encrypted_and_never_in_rules(self):
        from app.publish import voice
        db = self._db()
        raw = db.get_json(voice.KEY_NAME, "")
        self.assertTrue(raw and "bz-test-key" not in raw)
        self.assertNotIn("bz-test-key", json.dumps(db.rules(), ensure_ascii=False))
        self.assertTrue(voice.available(db, db.rules()))
        voice.save_key(db, "")
        self.assertFalse(voice.available(db, db.rules()))

    def test_request_shape(self):
        from app.publish import voice
        db = self._db(voice_instruction="用轻快的语气读", voice_rate=1.2, voice_volume=120)
        resp = mock.Mock(status_code=200, content=b"x" * 2000, headers={"Content-Type": "audio/mpeg"})
        with mock.patch("app.publish.voice.requests.post", return_value=resp) as rp:
            audio, ext, cost = voice.synthesize(db, db.rules(), "你好呀" * 10)
        url, kw = rp.call_args.args[0], rp.call_args.kwargs
        self.assertEqual(url, "https://api.breeze.blue/v1/text-to-speech/voc_abc123?output_format=mp3_24000_128")
        self.assertEqual(kw["headers"]["xi-api-key"], "bz-test-key")
        body = kw["json"]
        self.assertEqual((body["text"], body["language_code"], body["instructions"]), ("你好呀" * 10, "zh", "用轻快的语气读"))
        self.assertEqual(body["voice_settings"], {"speed": 1.2, "volume": 1.2, "guidance_scale": 3.0})
        self.assertNotIn("model_id", body)
        self.assertEqual((len(audio), ext), (2000, "mp3"))
        self.assertAlmostEqual(cost, 30 * 2 / 20000 * 5.0, places=4)          # 汉字一个算两个单位

    def test_no_instruction_no_guidance_and_old_cosyvoice_settings_ignored(self):
        from app.publish import voice
        db = self._db(voice_model="cosyvoice-v3-flash")
        resp = mock.Mock(status_code=200, content=b"x" * 2000, headers={"Content-Type": "audio/mpeg"})
        with mock.patch("app.publish.voice.requests.post", return_value=resp) as rp:
            voice.synthesize(db, db.rules(), "hello")
        body = rp.call_args.kwargs["json"]
        self.assertEqual(body["voice_settings"], {"speed": 1.0, "volume": 1.0})
        self.assertNotIn("model_id", body)
        self.assertNotIn("language_code", body)
        r = db.rules()
        r["voice_name"] = "longanhuan_v3"                                     # 以前 CosyVoice 的音色：当成没填
        db.set_json("rules", r)
        self.assertFalse(voice.available(db, db.rules()))

    def test_errors_are_readable_and_busy_is_retried_once(self):
        from app.publish import voice
        db = self._db()
        bad = mock.Mock(status_code=404, headers={})
        bad.json.return_value = {"ok": False, "code": "VOICE_NOT_FOUND", "detail": "voice not found", "error": "not_found"}
        with mock.patch("app.publish.voice.requests.post", return_value=bad):
            with self.assertRaises(voice.VoiceError) as cm:
                voice.synthesize(db, db.rules(), "你好")
        self.assertIn("音色 ID 不存在", str(cm.exception))
        self.assertIn("VOICE_NOT_FOUND", str(cm.exception))
        busy = mock.Mock(status_code=503, headers={"Retry-After": "2"})
        ok = mock.Mock(status_code=200, content=b"x" * 2000, headers={"Content-Type": "audio/mpeg"})
        with mock.patch("app.publish.voice.requests.post", side_effect=[busy, ok]) as rp, \
                mock.patch("app.publish.voice.cancel.sleep") as sl:
            voice.synthesize(db, db.rules(), "你好")
        self.assertEqual(rp.call_count, 2)
        sl.assert_called_once_with(2)
        with self.assertRaises(voice.VoiceError):
            voice.synthesize(make_db(), make_db().rules(), "你好")                 # 没填 Key

    def test_list_voices(self):
        from app.publish import voice
        db = self._db()
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"voices": [
            {"voice_id": "voc_1", "name": "小甜", "origin": "designed", "tone": ["bright", "playful"], "age": "young"},
            {"voice_id": "voc_2", "name": "童声", "origin": "designed", "tone": [], "age": "child"}], "has_more": False}
        with mock.patch("app.publish.voice.requests.get", return_value=resp) as rg:
            out = voice.list_voices(db, db.rules(), "personal")
        self.assertEqual(rg.call_args.args[0], "https://api.breeze.blue/v1/voices")
        self.assertEqual(rg.call_args.kwargs["params"]["voice_type"], "personal")
        self.assertEqual([v["id"] for v in out], ["voc_1"])                    # 童声不列

    def _ffmpeg(self):
        from app.publish import narrate
        try:
            return narrate.ffmpeg_exe()
        except narrate.NarrateError:
            self.skipTest("这台机器没有 ffmpeg")

    def test_narrate_draft_makes_a_video_and_publish_uses_only_it(self):
        import subprocess
        from PIL import Image
        from app.agent import runner
        from app.agent.runner import Agent, narrate_draft
        exe = self._ffmpeg()
        db = self._db()
        tmp = Path(tempfile.mkdtemp())
        mp3 = tmp / "v.mp3"
        subprocess.run([exe, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3.2",
                        "-b:a", "64k", str(mp3)], check=True)
        with mock.patch.object(runner, "DRAFTS_DIR", tmp):
            did = db.add_draft(status="wish", kind="image", title="周末", text="今天去了海边，风好大呀", tags=[],
                               media=[{"name": "c1.png", "kind": "image", "source": "card"},
                                      {"name": "c2.png", "kind": "image", "source": "card"}], cards=[], reason="", source="manual")
            (tmp / str(did)).mkdir()
            for n in ("c1.png", "c2.png"):
                Image.new("RGB", (1080, 1440), (255, 220, 230)).save(tmp / str(did) / n)
            with mock.patch("app.publish.voice.synthesize", return_value=(mp3.read_bytes(), "mp3", 0.002)) as syn:
                cost = narrate_draft(db, did)
            self.assertEqual(syn.call_args.args[2], "周末。\n今天去了海边，风好大呀")
            self.assertEqual(cost, 0.002)
            d = db.draft(did)
            v = d["media"][-1]
            self.assertEqual((v["source"], v["kind"]), ("voice", "video"))
            self.assertGreater((tmp / str(did) / v["name"]).stat().st_size, 5000)
            self.assertAlmostEqual(v["seconds"], 4.0, delta=0.4)
            # 发布：只发配音视频
            agent = Agent(db)
            agent.brain = mock.Mock()
            agent.brain.check_post.return_value = (True, "", 0.0)
            agent._ensure_browser = mock.Mock()
            with mock.patch.object(runner, "publish", return_value="ok") as pub:
                agent._cmd_publish(did)
            self.assertEqual([f.name for f in pub.call_args.args[2]], [v["name"]])
            self.assertEqual(pub.call_args.args[1]["kind"], "video")
            # 改了正文：旧配音去掉
            from app.api import Api
            api = Api(db, agent)
            with mock.patch("app.agent.runner.DRAFTS_DIR", tmp):
                db.update_draft(did, status="wish")
                api.save_draft(did, {"text": "改过的正文"})
            self.assertFalse(any(m.get("source") == "voice" for m in db.draft(did)["media"]))
            self.assertFalse((tmp / str(did) / v["name"]).exists())


class ReplyEveryMessageTests(unittest.TestCase):
    """私信：对方说的每一条都要回应到，不跳着回、不忽视对方的话；评论区回复要结合那一串评论的上下文。"""

    def _inbox(self, msgs):
        from app.douyin.inbox import Inbox
        ib = Inbox.__new__(Inbox)
        ib.b = mock.Mock()
        ib._chat_title = mock.Mock(return_value="小鹿")
        ib._read_chat = mock.Mock(return_value=[{"from_me": me, "kind": "text", "text": t, "url": "", "el": None}
                                                for me, t in msgs])
        return ib

    def test_messages_sent_while_she_was_typing_are_caught(self):
        ib = self._inbox([(True, "早呀"), (False, "你吃饭了吗"), (False, "我刚下班"),
                          (False, "对了你喜欢猫吗"),                    # 她打字的时候新发来的
                          (True, "吃啦～下班辛苦了"), (False, "在吗")])  # 她回完以后马上又发的
        self.assertEqual(ib.dm_missed("小鹿", ["你吃饭了吗", "我刚下班"], "吃啦～下班辛苦了"), ["对了你喜欢猫吗", "在吗"])

    def test_nothing_missed(self):
        ib = self._inbox([(True, "早呀"), (False, "你吃饭了吗"), (True, "吃啦～")])
        self.assertEqual(ib.dm_missed("小鹿", ["你吃饭了吗"], "吃啦～"), [])
        ib = self._inbox([(False, "你吃饭了吗"), (True, "别的话")])
        self.assertEqual(ib.dm_missed("小鹿", ["你吃饭了吗"], "吃啦～"), [])       # 没认出她刚发的那句：不乱猜

    def test_same_words_twice_counts_twice(self):
        ib = self._inbox([(True, "早"), (False, "哈哈"), (False, "哈哈"), (True, "笑什么呀")])
        self.assertEqual(ib.dm_missed("小鹿", ["哈哈"], "笑什么呀"), ["哈哈"])

    def test_same_text_later_the_same_day_is_a_new_message(self):
        """以前同一个人同一天说两遍同样的话（“在吗”），第二遍被当成处理过了、直接不回。"""
        from app.douyin.inbox import Inbox
        ib = Inbox.__new__(Inbox)
        ib.b = mock.Mock()
        ib.sec_uids = {}
        ib.b.known_user.return_value = None
        ib.open_dm_panel = mock.Mock(return_value=True)
        ib._back_to_list = mock.Mock(return_value=True)
        ib._has_unread = mock.Mock(return_value=True)
        ib.b.find_all.return_value = ["c1"]
        ib._conv_user = mock.Mock(return_value="小鹿")
        ib._conv_avatar = mock.Mock(return_value="")
        ib._open_conv = mock.Mock(return_value=True)
        chat = lambda *pairs: [{"from_me": me, "kind": "text", "text": t, "url": "", "el": None} for me, t in pairs]
        with mock.patch("app.douyin.inbox.react_values", return_value=[]):
            ib._read_chat = mock.Mock(return_value=chat((True, "早呀"), (False, "在吗"), (False, "想问你个事")))
            a = ib.fetch_unread_dms()[0]
            ib._read_chat = mock.Mock(return_value=chat((True, "在的在的"), (False, "在吗"), (False, "想问你个事")))
            b = ib.fetch_unread_dms()[0]
        self.assertEqual(a["text"], b["text"])
        self.assertNotEqual(a["ext_id"], b["ext_id"])
        self.assertEqual(a["lines"], ["在吗", "想问你个事"])

    def test_runner_keeps_replying_until_nothing_is_left(self):
        from app.agent.runner import Agent
        db = make_db()
        a = Agent(db)
        ib = mock.Mock()
        ib.dm_missed.side_effect = [["对了你喜欢猫吗"], []]
        mid = db.add_message(kind="dm", ext_id="x1", user_name="小鹿", content="你吃饭了吗", status="replied", reply="吃啦")
        handled = []

        def handle(inbox, m, it, kind, rules):
            handled.append(it)
            db.update_message(m, status="replied", reply="喜欢呀")
        a._safe_handle = handle
        a._dm_catch_up(ib, mid, {"user": "小鹿", "text": "你吃饭了吗", "lines": ["你吃饭了吗"]}, db.rules())
        self.assertEqual([h["text"] for h in handled], ["对了你喜欢猫吗"])
        self.assertEqual(ib.dm_missed.call_args_list[0].args, ("小鹿", ["你吃饭了吗"], "吃啦"))
        self.assertEqual(ib.dm_missed.call_args_list[1].args, ("小鹿", ["对了你喜欢猫吗"], "喜欢呀"))
        # 这一批没回出去（待你处理）：不接着往下
        ib.dm_missed.reset_mock()
        db.update_message(mid, status="pending")
        a._dm_catch_up(ib, mid, {"user": "小鹿", "text": "x", "lines": ["x"]}, db.rules())
        ib.dm_missed.assert_not_called()
        # 最多接 3 轮
        db.update_message(mid, status="replied")
        n = iter(range(100))
        ib.dm_missed.side_effect = lambda *a_: [f"又一句{next(n)}"]
        handled.clear()
        a._dm_catch_up(ib, mid, {"user": "小鹿", "text": "x", "lines": ["x"]}, db.rules())
        self.assertEqual(len(handled), 3)

    def test_prompt_demands_an_answer_to_every_line(self):
        from app.agent.brain import Brain
        b = Brain(make_db())
        b.router = mock.Mock()
        b.router.call.return_value = mock.Mock(text="好呀", cost=0.0, tokens=0)
        b.router.call.reset_mock()
        b.reply_dm("小鹿", [], "你吃饭了吗\n我刚下班\n对了你喜欢猫吗")
        p = b.router.call.call_args_list[0].args[2]
        self.assertIn("对方连着发了 3 条消息", p)
        self.assertIn("3. 对了你喜欢猫吗", p)
        self.assertIn("每一条都要回应到", p)
        self.assertIn("120 字以内", p)
        b.router.call.reset_mock()
        b.reply_dm("小鹿", [], "你喜欢猫吗")
        p = b.router.call.call_args_list[0].args[2]
        self.assertIn("先回应对方这句话本身", p)
        self.assertIn("不要无视 TA 的话", p)

    def test_comment_replies_get_the_thread_context(self):
        from app.agent.brain import Brain
        b = Brain(make_db())
        b.router = mock.Mock()
        b.router.call.return_value = mock.Mock(text="哈哈对", cost=0.0, tokens=0)
        ctx = [{"user": "楼主阿宁", "text": "这只柯基的屁股像面包", "me": False},
               {"user": "雨雨", "text": "真的像！想捏", "me": True}]
        b.router.call.reset_mock()
        b.reply_at({"desc": "柯基", "author": "a"}, "小鹿", "@雨雨 你也来看", [], context=ctx)
        p = b.router.call.call_args_list[0].args[2]
        self.assertIn("楼主阿宁：这只柯基的屁股像面包", p)
        self.assertIn("你：真的像！想捏", p)
        self.assertIn("要接得上这一串在聊的事", p)
        b.router.call.reset_mock()
        b.reply_thread({"desc": "柯基", "author": "a"}, "小鹿", "捏了会被咬", [], False, context=ctx)
        p = b.router.call.call_args_list[0].args[2]
        self.assertIn("楼主阿宁：这只柯基的屁股像面包", p)
        self.assertIn("不要撇开上下文另起话题", p)
        b.router.call.reset_mock()
        b.reply_at({"desc": "柯基", "author": "a"}, "小鹿", "@雨雨 你也来看", [])
        self.assertNotIn("这条评论所在的那一串评论", b.router.call.call_args_list[0].args[2])

    def test_runner_marks_her_own_lines_in_the_thread(self):
        from app.agent.runner import Agent
        db = make_db()
        a = Agent(db)
        db.add_message(kind="at", ext_id="e1", user_name="阿宁", content="x", status="replied", reply="真的像！想捏")
        ib = mock.Mock()
        ib.thread_context.return_value = [{"user": "楼主阿宁", "text": "这只柯基的屁股像面包"},
                                          {"user": "雨雨", "text": "真的像！ 想捏"},
                                          {"user": "小鹿", "text": "捏了会被咬"}]
        out = a._thread_context(ib, {"user": "小鹿", "text": "捏了会被咬"})
        self.assertEqual([(r["user"], r["me"]) for r in out], [("楼主阿宁", False), ("雨雨", True)])   # 对方这条本身不重复列
        ib.thread_context.side_effect = Exception("页面变了")
        self.assertEqual(a._thread_context(ib, {"user": "小鹿", "text": "x"}), [])

    def test_thread_rows_script_on_a_real_shaped_page(self):
        from app.douyin import inbox as inbox_mod
        js = inbox_mod.THREAD_ROWS_JS
        self.assertIn("row === el && row !== top", js)                               # 读到对方这条为止
        ib = inbox_mod.Inbox.__new__(inbox_mod.Inbox)
        ib.b = mock.Mock()
        ib.b.sel = {"comment_item": ["[data-e2e='comment-item']"]}
        ib.feed = mock.Mock()
        ib.feed.open_comments.return_value = True
        target = mock.Mock()
        target.evaluate.return_value = [{"user": "楼主", "text": "第一条"}] + [{"user": f"u{i}", "text": f"回复{i}"} for i in range(14)]
        ib._find_comment = mock.Mock(return_value=target)
        rows = ib.thread_context({"user": "u13", "text": "回复13"})
        self.assertEqual(len(rows), 10)
        self.assertEqual((rows[0]["text"], rows[-1]["text"]), ("第一条", "回复13"))    # 楼主 + 最近的几条
        ib._find_comment = mock.Mock(return_value=None)
        self.assertEqual(ib.thread_context({"user": "u", "text": "t"}), [])

    def test_still_picture_keeps_the_head_on_the_neck(self):
        """预览里歪头 / 跳舞的动作，头和身体像断成两截：定格成图时头不横着挪开，歪头角度收小。"""
        from app.pet.rig import load
        from app.publish import stickers, voicecard
        rig = load("default")
        seen = []
        import app.pet.render as render_mod
        real = render_mod.render
        with mock.patch.object(render_mod, "render", side_effect=lambda pet, r, s: seen.append(pet.pose()) or real(pet, r, s)):
            for mood in ("跳舞", "加油", "摸鱼", "晚安"):
                act, at = stickers.MOODS[mood][0], stickers.MOODS[mood][1]
                raw = stickers._pose(rig, act, at).pose()
                voicecard._still(stickers._pose(rig, act, at), rig, 1.0)
                p = seen[-1]
                self.assertEqual(p.HeadDX, 0.0)
                self.assertLessEqual(abs(p.HeadTilt), voicecard.HEAD_TILT_MAX)
                self.assertEqual((p.Tilt, p.LieE, p.ArmL), (raw.Tilt, raw.LieE, raw.ArmL))   # 身体、手的动作照旧
        raw = stickers._pose(rig, stickers.MOODS["跳舞"][0], stickers.MOODS["跳舞"][1]).pose()
        self.assertGreater(abs(raw.HeadDX), 15)                                      # 原来的动作确实把头甩开了


class PostFormTests(unittest.TestCase):
    """作品的三种形式：配音图片视频（默认：文案全写在一张带桌宠的图上 + 自动配音，画面不翻页）、
    图文卡片（保持原来的功能，可以手动配音）、只用你上传的图片 / 视频。
    “重新生成文字卡片”按正文输入框里现在的全部内容重新做。"""

    def setUp(self):
        import subprocess
        from app.agent import runner
        from app.api import Api
        from app.publish import narrate
        self.db = make_db()
        self.tmp = Path(tempfile.mkdtemp())
        for target in ("app.api.DRAFTS_DIR", "app.agent.runner.DRAFTS_DIR"):
            p = mock.patch(target, self.tmp)
            p.start()
            self.addCleanup(p.stop)
        self.runner = runner
        self.agent = runner.Agent(self.db)
        self.agent.brain = mock.Mock()
        self.agent.mood = mock.Mock()
        self.agent.mood.current.return_value = {"key": "happy", "label": "开心", "reason": "x"}
        self.agent.memory = mock.Mock()
        self.agent.memory.post_material.return_value = ""
        self.api = Api(self.db, self.agent)
        try:
            exe = narrate.ffmpeg_exe()
        except narrate.NarrateError:
            self.skipTest("这台机器没有 ffmpeg")
        mp3 = self.tmp / "v.mp3"
        subprocess.run([exe, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2.5",
                        "-b:a", "64k", str(mp3)], check=True)
        self.mp3 = mp3.read_bytes()
        self.post = {"want": True, "why": "想分享", "title": "被猫无视的下午", "tags": ["日常"], "image_prompt": "一只猫",
                     "text": "今天下班路上看到一只橘猫。\n它打了个哈欠就不理我了！原来被一只猫无视，也能开心一整天～",
                     "cards": [{"title": "c", "lines": ["她自己写的卡片"]}], "cost": 0}

    def _voice(self, on=True):
        return mock.patch.multiple("app.publish.voice", available=mock.Mock(return_value=on),
                                   synthesize=mock.Mock(return_value=(self.mp3, "mp3", 0.002)))

    def _make(self, **rules):
        self.db.set_json("rules", dict(self.db.rules(), **rules))
        self.agent.brain.draft_post.return_value = dict(self.post)
        return self.db.draft(self.agent._make_draft(force=True))

    def test_default_is_one_picture_with_voice(self):
        from PIL import Image
        self.assertEqual(self.db.rules()["post_form"], "voice")                       # 默认设置
        with self._voice(), mock.patch("app.publish.imagegen.available", return_value=True), \
                mock.patch("app.publish.imagegen.generate") as gen:
            d = self._make()
        self.assertEqual(d["form"], "voice")
        self.assertEqual([m["source"] for m in d["media"]], ["voicecard", "voice"])   # 一张图 + 配好音的视频，没有文字卡片
        gen.assert_not_called()                                                       # 这种形式不另外生成 AI 配图
        pic = self.tmp / str(d["id"]) / d["media"][0]["name"]
        self.assertEqual(Image.open(pic).size, (1080, 1440))
        self.assertIn(d["media"][0]["pose"], __import__("app.publish.stickers", fromlist=["MOODS"]).MOODS)
        v = d["media"][1]
        self.assertEqual(v["kind"], "video")
        self.assertGreater((self.tmp / str(d["id"]) / v["name"]).stat().st_size, 5000)
        self.assertAlmostEqual(v["seconds"], 3.3, delta=0.4)
        self.assertEqual(d["cards"], self.post["cards"])                              # 她写的卡片内容留着，换形式时用

    def test_video_is_one_still_picture(self):
        """配音图片视频：整个视频只有那一张图（不翻页、不滚动）——合成时只给了一张图。"""
        with self._voice(), mock.patch("app.publish.narrate.make_video", return_value=3.0) as mk:
            d = self._make()
        images = mk.call_args.args[0]
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0].name.startswith("voicecard_"))
        self.assertEqual(d["media"][0]["name"], images[0].name)

    def test_without_her_voice_set_up_only_the_picture(self):
        with self._voice(False):
            d = self._make()
        self.assertEqual([m["source"] for m in d["media"]], ["voicecard"])
        self.assertTrue(any("还没设置她的声音" in e["detail"] for e in self.db.query("SELECT detail FROM events")))

    def test_cards_form_keeps_old_behaviour(self):
        with self._voice() as _:
            d = self._make(post_form="cards", image_gen_auto=False)
        self.assertEqual(d["form"], "cards")
        self.assertEqual([m["source"] for m in d["media"]], ["card", "card"])         # 封面 + 她写的那张；不自动配音
        with self._voice():
            d = self._make(post_form="cards", image_gen_auto=False, voice_auto=True)
        self.assertEqual([m["source"] for m in d["media"]], ["card", "card", "voice"])
        self.assertEqual(self.api.get_draft(d["id"])["data"]["form"], "cards")

    def test_old_drafts_count_as_cards(self):
        from app.agent.runner import draft_form
        did = self.db.add_draft(status="wish", title="t", text="x", media=[], cards=[], tags=[])
        self.assertEqual(self.db.draft(did)["form"], "")
        self.assertEqual(self.api.get_draft(did)["data"]["form"], "cards")
        self.assertEqual([draft_form(v) for v in ("voice", "upload", None, "乱写")], ["voice", "upload", "cards", "cards"])

    def test_picture_shows_all_text_and_the_pet(self):
        from PIL import Image, ImageChops
        from app.publish import voicecard
        out = self.tmp / "a.jpg"
        info = voicecard.render("标题", "第一句。\n第二句～", "雨雨", out, seed=1)
        blank = self.tmp / "b.jpg"
        voicecard.render("", "", "", blank, seed=1)
        a, b = Image.open(out), Image.open(blank)
        diff = ImageChops.difference(a, b).convert("L")
        self.assertIsNotNone(diff.crop((120, 150, 960, 700)).getbbox())               # 白框里写了字
        self.assertFalse(info["cut"])
        # 桌宠在图上：下半部分有大块非背景色（深色的头发 / 描线）
        dark = a.crop((0, 900, 1080, 1440)).convert("L").point(lambda v: 255 if v < 90 else 0)
        self.assertGreater(sum(dark.histogram()[255:]), 2500)
        # 随机动作：不同的种子摆出不同的姿势
        poses = {voicecard.render("t", "x", "雨雨", self.tmp / f"p{i}.jpg", seed=i)["mood"] for i in range(12)}
        self.assertGreater(len(poses), 3)
        # 字很多：缩小字、缩小桌宠，全写上；多到写不下才截断
        self.assertFalse(voicecard.render("长", "这一句有二十个字左右用来测试排版。" * 40, "雨雨", self.tmp / "l.jpg")["cut"])
        self.assertTrue(voicecard.render("长", "这一句有二十个字左右用来测试排版。" * 220, "雨雨", self.tmp / "x.jpg")["cut"])

    def test_cards_from_body_text_keep_every_sentence(self):
        from app.publish.cards import MAX_CARDS, MAX_LINES, cards_from_text
        text = "今天下班路上看到一只橘猫。它趴在便利店门口晒太阳！\n\n谁路过都要被它瞄一眼～我蹲下来看了它好久，它打了个哈欠就不理我了……原来被无视也能开心一整天"
        cards = cards_from_text(text)
        joined = "".join("".join(c["lines"]) for c in cards)
        self.assertEqual(joined, text.replace("\n", ""))                              # 一个字都不丢
        self.assertTrue(all(not c["title"] and 1 <= len(c["lines"]) <= MAX_LINES for c in cards))
        long = "".join(f"第{i}句话说点什么好呢。" for i in range(70))
        cards = cards_from_text(long)
        self.assertLessEqual(len(cards), MAX_CARDS)
        self.assertEqual("".join("".join(c["lines"]) for c in cards), long)
        one = cards_from_text("没有标点的一长句" * 12)                                  # 很长又没标点：切开，不丢
        self.assertEqual("".join("".join(c["lines"]) for c in one), "没有标点的一长句" * 12)
        self.assertTrue(all(len(x) <= 60 for c in one for x in c["lines"]))
        self.assertEqual(cards_from_text("  \n "), [])

    def test_regenerate_cards_uses_current_body_text(self):
        with self._voice():
            d = self._make(post_form="cards", image_gen_auto=False)
        did = d["id"]
        self.api.save_draft(did, {"text": "改成了新的正文第一句。新的第二句！新的第三句～新的第四句。新的第五句。"})
        self.assertTrue(self.api.draft_cards(did, True)["ok"])
        d = self.db.draft(did)
        self.assertEqual("".join("".join(c["lines"]) for c in d["cards"]),
                         "改成了新的正文第一句。新的第二句！新的第三句～新的第四句。新的第五句。")   # 不再是她原来写的那张卡片
        self.assertEqual([m["source"] for m in d["media"]], ["card", "card", "card"])  # 封面 + 2 张
        # “按上面写的重画”：用卡片编辑框里的内容
        self.api.save_draft(did, {"cards": "只有这一句"})
        self.api.draft_cards(did, False)
        d = self.db.draft(did)
        self.assertEqual(d["cards"], [{"title": "", "lines": ["只有这一句"]}])
        self.assertEqual(len(d["media"]), 2)
        self.db.update_draft(did, text="")
        self.assertIn("正文还是空的", self.api.draft_cards(did, True)["error"])

    def test_switching_forms(self):
        from PIL import Image
        with self._voice():
            d = self._make()
            did = d["id"]
            up = self.tmp / "mine.jpg"
            Image.new("RGB", (40, 40), "red").save(up)
            self.db.update_draft(did, media=d["media"] + [{"name": "mine.jpg", "kind": "image", "source": "upload", "path": str(up)}])
            old = [self.tmp / str(did) / m["name"] for m in d["media"]]
            self.assertEqual(self.api.draft_set_form(did, "cards")["data"], {"form": "cards", "voiced": False})
            d = self.db.draft(did)
            self.assertEqual([m["source"] for m in d["media"]], ["card", "card", "upload"])
            self.assertFalse(any(f.exists() for f in old))                            # 上一种形式的图和配音删了
            self.assertTrue(up.exists())                                              # 你上传的不动
            self.assertEqual(self.api.draft_set_form(did, "upload")["data"]["form"], "upload")
            self.assertEqual([m["source"] for m in self.db.draft(did)["media"]], ["upload"])
            self.assertEqual(self.api.draft_set_form(did, "voice")["data"], {"form": "voice", "voiced": True})
            d = self.db.draft(did)
            self.assertEqual([m["source"] for m in d["media"]], ["voicecard", "upload", "voice"])
        with self._voice(False):
            self.api.draft_set_form(did, "cards")
            self.assertEqual(self.api.draft_set_form(did, "voice")["data"]["voiced"], False)
            self.assertEqual([m["source"] for m in self.db.draft(did)["media"]], ["voicecard", "upload"])
        self.assertFalse(self.api.draft_set_form(did, "别的")["ok"])
        self.db.update_draft(did, status="published")
        self.assertFalse(self.api.draft_set_form(did, "cards")["ok"])

    def test_editing_text_redraws_the_picture_and_drops_the_voice(self):
        with self._voice():
            d = self._make()
            did = d["id"]
            pic, vid = d["media"][0]["name"], d["media"][1]["name"]
            self.api.save_draft(did, {"text": "改过的正文"})
            d = self.db.draft(did)
            self.assertEqual([m["source"] for m in d["media"]], ["voicecard"])
            self.assertNotEqual(d["media"][0]["name"], pic)                           # 图按新文案重画了
            self.assertFalse((self.tmp / str(did) / pic).exists())
            self.assertFalse((self.tmp / str(did) / vid).exists())
            # “重新生成图片并配音”
            self.assertTrue(self.api.draft_voice(did)["ok"])
            d = self.db.draft(did)
            self.assertEqual([m["source"] for m in d["media"]], ["voicecard", "voice"])
            self.assertEqual(len(list((self.tmp / str(did)).glob("voicecard_*.jpg"))), 1)   # 旧图不堆着

    def test_publish_sends_only_the_voice_video(self):
        with self._voice():
            d = self._make()
        self.agent.brain.check_post.return_value = (True, "", 0.0)
        self.agent._ensure_browser = mock.Mock()
        with mock.patch.object(self.runner, "publish", return_value="ok") as pub:
            self.agent._cmd_publish(d["id"])
        self.assertEqual([f.name for f in pub.call_args.args[2]], [d["media"][1]["name"]])
        self.assertEqual(pub.call_args.args[1]["kind"], "video")

    def test_new_blank_draft_uses_default_form(self):
        did = self.api.new_draft()["data"]
        self.assertEqual(self.db.draft(did)["form"], "voice")
        self.db.set_json("rules", dict(self.db.rules(), post_form="cards"))
        self.assertEqual(self.db.draft(self.api.new_draft()["data"])["form"], "cards")

    def test_ui(self):
        js = (ROOT / "web" / "js" / "app.js").read_text(encoding="utf-8")
        for want in ('data-act="d-form"', '"d-form": async', 'call("draft_set_form"', 'call("draft_cards", S.selDraft, true)',
                     'data-act="d-cards-custom"', "配音图片视频（默认）", 'id="v-form"', "post_form: g(\"#v-form\").value",
                     "重新生成图片并配音", 'voicecard: "配音图片"'):
            self.assertIn(want, js)


class FreeFormPostTests(unittest.TestCase):
    """作品文案没有固定格式：卡片几张、每张几句都由她定；内置一批可爱背景。"""

    def test_prompt_is_free_form_and_uses_memory(self):
        from app.agent.brain import Brain
        b = Brain(make_db())
        cards = [{"title": "", "lines": ["今天的风有点像小时候"]}] + \
                [{"title": f"第{i}件", "lines": [f"句{j}" for j in range(i + 1)]} for i in range(1, 7)] + ["只有一句的卡片"]
        fake = mock.Mock(text=json.dumps({"want": True, "why": "想说话", "title": "碎碎念", "text": "正文", "tags": ["日常"],
                                          "cards": cards}, ensure_ascii=False), cost=0.0)
        with mock.patch.object(b.router, "call", return_value=fake) as call:
            d = b.draft_post(["猫猫视频"], "有点想家", memory="以前的事：\n- 2026-10-01 和小廖聊过海边")
        prompt = call.call_args.args[2]
        for word in ("没有固定的格式", "回忆", "憧憬", "吐槽", "和小廖聊过海边", "张数你自己定"):
            self.assertIn(word, prompt)
        self.assertNotIn("1～4 张", prompt)
        self.assertEqual(len(d["cards"]), 8)                                   # 不再砍成 4 张
        self.assertEqual(d["cards"][0], {"title": "", "lines": ["今天的风有点像小时候"]})
        self.assertEqual(len(d["cards"][6]["lines"]), 7)                       # 一张可以超过 4 句
        self.assertEqual(d["cards"][7], {"title": "", "lines": ["只有一句的卡片"]})

    def test_cards_render_any_count_and_fit(self):
        from PIL import Image
        from app.publish.cards import render_post
        out = Path(tempfile.mkdtemp())
        cards = [{"title": "", "lines": ["就一句话"]}, {"title": "", "lines": ["没有小标题", "但是有三句", "也可以的"]},
                 {"title": "很多很多句", "lines": [f"这是第 {i} 句，写得稍微长一点点看看会不会放不下" for i in range(8)]}]
        names = render_post("碎碎念", "正文第一句。后面的。", cards, "雨雨", out)
        self.assertEqual(len(names), 4)                                        # 封面 + 3 张
        for n in names:
            self.assertEqual(Image.open(out / n).size, (1080, 1440))
        self.assertEqual(len(render_post("只有封面", "一句话。", [], "雨雨", out, prefix="x")), 1)

    def test_card_editor_text_roundtrip(self):
        from app.api import _parse_cards
        cards = _parse_cards("就一句话\n\n-\n没有小标题\n但是有两句\n\n小标题\n第一句\n第二句")
        self.assertEqual(cards, [{"title": "", "lines": ["就一句话"]}, {"title": "", "lines": ["没有小标题", "但是有两句"]},
                                 {"title": "小标题", "lines": ["第一句", "第二句"]}])
        self.assertEqual(len(_parse_cards("\n\n".join(f"卡{i}" for i in range(20)))), 9)

    def test_post_material_has_today_diary_and_old_things(self):
        from app.agent.memory import Memory
        db = make_db()
        mem = Memory(db)
        mem.remember("note", "今天看了一只打滚的橘猫")
        db.execute("INSERT INTO diary(level, period, content, mood, created_at) VALUES('day','2026-11-10','昨天下雨了，窝着看了一天视频','平静','2026-11-10 23:00:00')")
        db.execute("UPDATE memories SET created_at='2026-01-01 10:00:00' WHERE summary LIKE '%橘猫%'")
        text = mem.post_material()
        self.assertIn("昨天下雨了", text)
        self.assertIn("橘猫", text)

    def test_builtin_backgrounds(self):
        from PIL import Image
        from app.agent import runner
        from app.agent.runner import Agent
        from app.api import Api
        from app.publish import backgrounds
        tmp = Path(tempfile.mkdtemp())
        db = make_db()
        api = Api(db, Agent(db))
        with mock.patch.object(runner, "DRAFTS_DIR", tmp):
            items = api._backgrounds()
            self.assertEqual(len(items), len(backgrounds.BUILTIN))
            self.assertGreaterEqual(len(items), 12)
            self.assertIn("小熊饼干", [i["label"] for i in items])
            first = tmp / runner.BG_DIR_NAME / items[0]["name"]
            self.assertEqual(Image.open(first).size, (1080, 1440))
            api.card_bg_delete(items[0]["name"])                               # 删掉的内置背景不会自己回来
            again = api._backgrounds()
            self.assertEqual(len(again), len(backgrounds.BUILTIN) - 1)
            self.assertNotIn(items[0]["name"], [i["name"] for i in again])
