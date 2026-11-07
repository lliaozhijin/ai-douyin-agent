"""核心逻辑的单元测试（不需要联网、不需要浏览器）。运行：python -m unittest discover -s tests"""
from __future__ import annotations

import base64
import io
import sys
import tempfile
import time
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

from app import crypto  # noqa: E402
from app.agent import safety  # noqa: E402
from app.agent.brain import Brain, Draft, parse_json  # noqa: E402
from app.agent.budget import Budget, in_active_hours  # noqa: E402
from app.agent.persona import AI_LABEL, douyin_bio, system_prompt  # noqa: E402
from app.db import Database  # noqa: E402
from app.llm import client  # noqa: E402
from app.llm.client import LLMError, LLMResult  # noqa: E402
from app.llm.router import BudgetExceeded, Router  # noqa: E402


def make_db() -> Database:
    tmp = tempfile.mkdtemp()
    return Database(Path(tmp) / "test.db")


def fake_response(status: int, data: dict):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = data
    r.text = str(data)
    return r


class ProviderMigrationTests(unittest.TestCase):
    def test_old_deepseek_row_gets_thinking_disabled(self):
        path = Path(tempfile.mkdtemp()) / "old.db"
        db = Database(path)
        db.execute("UPDATE providers SET extra='{}', models='[\"deepseek-chat\"]' WHERE id='deepseek'")  # 模拟老版本的数据
        db.close()
        db2 = Database(path)
        p = db2.provider("deepseek")
        self.assertEqual(p["extra"]["request_extra"], {"thinking": {"type": "disabled"}})
        self.assertEqual(p["models"], ["deepseek-chat"])  # 用户填的模型不被覆盖
        new = make_db().provider("deepseek")
        self.assertEqual(new["models"], ["deepseek-flash"])
        self.assertEqual(new["extra"]["vision_models"], ["deepseek-flash"])


class DbTests(unittest.TestCase):
    def test_seed_and_json(self):
        db = make_db()
        self.assertEqual(db.persona()["name"], "雨雨")
        self.assertEqual(db.ui()["theme"], "pink")
        self.assertEqual(len(db.providers()), 8)
        db.set_json("ui", {"theme": "blue"})
        self.assertEqual(db.ui()["theme"], "blue")

    def test_usage_and_messages(self):
        db = make_db()
        db.add_usage("videos")
        db.add_usage("cost", 0.25)
        u = db.usage()
        self.assertEqual(u["videos"], 1)
        self.assertAlmostEqual(u["cost"], 0.25)
        mid = db.add_message(kind="dm", ext_id="x1", user_name="小鹿", content="你好", status="pending")
        db.add_message(kind="dm", ext_id="x1", user_name="小鹿", content="重复", status="pending")  # 去重
        self.assertEqual(db.message_counts()["all_count"], 1)
        db.update_message(mid, status="replied", handled_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self.assertEqual(db.replies_to_user_today("小鹿"), 1)
        db.block_user("坏人")
        self.assertTrue(db.is_blocked("坏人"))


class CryptoTests(unittest.TestCase):
    def test_roundtrip_and_mask(self):
        enc = crypto.encrypt("sk-ant-api03-abcdefghijklmn")
        self.assertNotIn("abcdefghijklmn", enc)
        self.assertEqual(crypto.decrypt(enc), "sk-ant-api03-abcdefghijklmn")
        self.assertEqual(crypto.mask("sk-ant-api03-abcdefghijklmn"), "sk-ant••••••klmn")
        self.assertEqual(crypto.encrypt(""), "")


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.rules = make_db().rules()

    def test_keyword_hit(self):
        self.assertEqual(safety.keyword_hit("你觉得现在买哪只基金好？", self.rules), "投资理财")
        self.assertEqual(safety.keyword_hit("加个微信聊吧", self.rules), "索要电话 / 地址 / 微信")
        self.assertIn("稳赚", safety.keyword_hit("这个项目稳赚不赔", self.rules))
        self.assertIsNone(safety.keyword_hit("这件外套哪里买？", self.rules))
        rules = dict(self.rules, topics=[])
        self.assertIsNone(safety.keyword_hit("买基金", rules))

    def test_output_problem(self):
        self.assertIsNone(safety.output_problem("这件大衣好好看呀～"))
        self.assertEqual(safety.output_problem("看这里 https://x.com"), "包含链接")
        self.assertEqual(safety.output_problem("打我电话13812345678"), "包含手机号")
        self.assertIsNotNone(safety.output_problem("加我微信呀"))
        self.assertEqual(safety.output_problem("  "), "内容为空")

    def test_clean_reply(self):
        self.assertEqual(safety.clean_reply("回复：“好呀～**真好看**”"), "好呀～真好看")


class BudgetTests(unittest.TestCase):
    def test_limits(self):
        db = make_db()
        b = Budget(db)
        rules = db.rules()
        rules["limits"]["likes"] = 1
        db.set_json("rules", rules)
        self.assertTrue(b.can("likes"))
        b.use("likes")
        self.assertFalse(b.can("likes"))
        rules["limits"]["cost"] = 0.1
        db.set_json("rules", rules)
        db.add_usage("cost", 0.2)
        self.assertFalse(b.can("videos"))  # 花费到上限时其他动作也停

    def test_active_hours(self):
        rules = {"active_start": "09:00", "active_end": "23:00"}
        self.assertTrue(in_active_hours(rules, datetime(2026, 1, 1, 12, 0)))
        self.assertFalse(in_active_hours(rules, datetime(2026, 1, 1, 3, 0)))
        night = {"active_start": "20:00", "active_end": "02:00"}
        self.assertTrue(in_active_hours(night, datetime(2026, 1, 1, 1, 0)))
        self.assertFalse(in_active_hours(night, datetime(2026, 1, 1, 12, 0)))


class PersonaTests(unittest.TestCase):
    def test_prompt_and_bio(self):
        p = make_db().persona()
        sp = system_prompt(p)
        self.assertIn("雨雨", sp)
        self.assertIn("如实说自己是 AI", sp)
        self.assertIn("不发任何链接", sp)
        bio = douyin_bio(p)
        self.assertIn(AI_LABEL, bio)
        self.assertIn(p["signature"], bio)


class ParseJsonTests(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(parse_json('{"a": 1}'), {"a": 1})
        self.assertEqual(parse_json('好的：\n```json\n{"like": true, "comment": "好看"}\n```'),
                         {"like": True, "comment": "好看"})
        self.assertEqual(parse_json("没有 JSON"), {})
        self.assertEqual(parse_json('{"a": 1} 多余的 }'), {"a": 1})


class LlmClientTests(unittest.TestCase):
    def provider(self, kind="anthropic", vision=True):
        return {"id": "p", "name": "测试", "kind": kind, "base_url": "https://example.com/v1" if kind != "anthropic" else "",
                "api_key_enc": crypto.encrypt("k-123"), "proxy": "", "vision": vision,
                "models": ["claude-haiku-5-5", "m"], "price_in": 1.0, "price_out": 2.0, "extra": {}}

    def test_thinking_disabled_and_empty_retry(self):
        p = self.provider("openai")
        p["extra"] = {"request_extra": {"thinking": {"type": "disabled"}}}
        empty = {"choices": [{"message": {"content": "", "reasoning_content": "想了很多……"}, "finish_reason": "length"}],
                 "usage": {"prompt_tokens": 100, "completion_tokens": 120}}
        good = {"choices": [{"message": {"content": "好呀"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 300}}
        with mock.patch("app.llm.client.requests.post",
                        side_effect=[fake_response(200, empty), fake_response(200, good)]) as post:
            r = client.chat(p, "m", "sys", "hi", max_tokens=120)
        first, second = post.call_args_list[0].kwargs["json"], post.call_args_list[1].kwargs["json"]
        self.assertEqual(first["thinking"], {"type": "disabled"})
        self.assertEqual(first["max_tokens"], 120)
        self.assertGreaterEqual(second["max_tokens"], 2000)
        self.assertEqual(r.text, "好呀")
        self.assertEqual(r.output_tokens, 420)  # 两次的用量都要算钱
        with mock.patch("app.llm.client.requests.post",
                        side_effect=[fake_response(200, empty), fake_response(200, empty)]):
            with self.assertRaises(LLMError):
                client.chat(p, "m", "sys", "hi")

    def test_per_model_vision(self):
        p = self.provider("openai")
        p["models"] = ["qwen-vl-plus", "qwen-plus"]
        p["extra"] = {"vision_models": ["qwen-vl-plus"]}
        self.assertTrue(client.model_can_see(p, "qwen-vl-plus"))
        self.assertFalse(client.model_can_see(p, "qwen-plus"))
        with self.assertRaises(LLMError):
            client.chat(p, "qwen-plus", "s", "u", images=[b"x"])
        old = self.provider("openai", vision=True)  # 老数据：没有逐个模型的设置，按整个服务商算
        self.assertTrue(client.model_can_see(old, "m"))

    def test_anthropic(self):
        data = {"content": [{"type": "text", "text": " 你好 "}], "usage": {"input_tokens": 1000, "output_tokens": 500}}
        with mock.patch("app.llm.client.requests.post", return_value=fake_response(200, data)) as post:
            r = client.chat(self.provider(), "claude-haiku-5-5", "sys", "hi", images=[b"\x89PNG\r\n\x1a\nxxxx"])
        self.assertEqual(r.text, "你好")
        self.assertAlmostEqual(r.cost, 1000 / 1e6 * 1 + 500 / 1e6 * 2)
        url = post.call_args.args[0]
        payload = post.call_args.kwargs["json"]
        headers = post.call_args.kwargs["headers"]
        self.assertTrue(url.endswith("/v1/messages"))
        self.assertEqual(headers["x-api-key"], "k-123")
        self.assertEqual(payload["system"], "sys")
        self.assertEqual(payload["messages"][0]["content"][0]["source"]["media_type"], "image/png")

    def test_openai(self):
        data = {"choices": [{"message": {"content": "好呀"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        with mock.patch("app.llm.client.requests.post", return_value=fake_response(200, data)) as post:
            r = client.chat(self.provider("openai"), "m", "sys", "hi")
        self.assertEqual(r.text, "好呀")
        self.assertTrue(post.call_args.args[0].endswith("/chat/completions"))
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer k-123")

    def test_errors(self):
        p = self.provider()
        p["api_key_enc"] = ""
        with self.assertRaises(LLMError):
            client.chat(p, "m", "s", "u")
        with self.assertRaises(LLMError):
            client.chat(self.provider(vision=False), "m", "s", "u", images=[b"x"])
        with mock.patch("app.llm.client.requests.post", return_value=fake_response(401, {"error": "bad key"})):
            with self.assertRaises(LLMError):
                client.chat(self.provider(), "m", "s", "u")


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.db.set_provider_key("anthropic", crypto.encrypt("k1"))
        self.db.set_provider_key("deepseek", crypto.encrypt("k2"))
        self.db.set_json("tasks", {"write": {"provider": "anthropic", "model": "claude-sonnet-5-5"},
                                   "fallback": {"provider": "deepseek", "model": "deepseek-chat"}})

    def test_fallback_and_accounting(self):
        calls = []

        def fake_chat(p, model, *a, **k):
            calls.append(p["id"])
            if p["id"] == "anthropic":
                raise LLMError("连不上")
            return LLMResult(text="ok", input_tokens=10, output_tokens=10, cost=0.01, provider=p["id"], model=model)

        with mock.patch("app.llm.router.chat", side_effect=fake_chat):
            r = Router(self.db).call("safety", "s", "u")  # safety 未配置 → 借用 write → 失败 → 备用
        self.assertEqual(calls, ["anthropic", "deepseek"])
        self.assertEqual(r.text, "ok")
        self.assertAlmostEqual(self.db.usage()["cost"], 0.01)

    def test_blind_model_drops_images(self):
        self.db.set_json("tasks", {"vision": {"provider": "deepseek", "model": "deepseek-chat"}})
        r = Router(self.db)
        self.assertFalse(r.task_can_see("vision"))  # deepseek-chat 没在“能识图”名单里
        seen = {}

        def fake_chat(p, model, system, text, images, *a, **k):
            seen["images"] = images
            return LLMResult(text="ok")

        with mock.patch("app.llm.router.chat", side_effect=fake_chat):
            r.call("vision", "s", "u", images=[b"img"])
        self.assertIsNone(seen["images"])

    def test_budget_exceeded(self):
        self.db.add_usage("cost", 100)
        with self.assertRaises(BudgetExceeded):
            Router(self.db).call("write", "s", "u")


class BrainTests(unittest.TestCase):
    def brain_with(self, *texts):
        b = Brain(make_db())
        results = [LLMResult(text=t, cost=0.001) for t in texts]
        b.router = mock.Mock()
        b.router.call.side_effect = results
        return b

    def test_reply_ok(self):
        b = self.brain_with("我是 AI 虚拟女孩雨雨哦～", '{"ok": true, "reason": ""}')
        d = b.reply_dm("小鹿", [], "你是真人吗")
        self.assertTrue(d.ok)
        self.assertAlmostEqual(d.cost, 0.002)

    def test_reply_blocked_by_rules(self):
        b = self.brain_with("加我微信聊呀")
        d = b.reply_dm("小鹿", [], "你好")
        self.assertFalse(d.ok)
        self.assertIn("联系方式", d.reason)

    def test_reply_blocked_by_model(self):
        b = self.brain_with("我也好想你呀", '{"ok": false, "reason": "暧昧"}')
        d = b.reply_dm("小鹿", [], "想你了")
        self.assertFalse(d.ok)
        self.assertIn("暧昧", d.reason)

    def test_triage_and_decide(self):
        b = self.brain_with('{"interest": 8, "reason": "美食"}', '{"interest": 8, "like": true, "comment": "看饿了", "reason": "好吃"}')
        t = b.triage({"author": "a", "desc": "番茄牛腩", "comments": []})
        self.assertEqual(t["interest"], 8)
        d = b.decide({"author": "a", "desc": "番茄牛腩"}, [b"img"], can_like=True, can_comment=False)
        self.assertTrue(d["like"])
        self.assertEqual(d["comment"], "")  # 评论额度用完时不评论

    def test_decide_collect_only_when_allowed(self):
        b = self.brain_with('{"interest": 9, "like": true, "collect": "true", "comment": "", "reason": "菜谱"}')
        d = b.decide({"author": "a", "desc": "番茄牛腩"}, [], True, False, can_collect=True)
        self.assertTrue(d["collect"])
        b = self.brain_with('{"interest": 9, "like": true, "collect": true, "comment": "", "reason": "菜谱"}')
        d = b.decide({"author": "a", "desc": "番茄牛腩"}, [], True, False)
        self.assertFalse(d["collect"])  # 收藏关掉 / 额度用完时不收藏

    def test_decide_picks_comments_to_like_and_reply(self):
        v = {"author": "a", "desc": "秋景", "comments": ["小林：好美", "阿树：像油画", "路人：一般"]}
        b = self.brain_with('{"interest": 8, "like": true, "comment": "", "comment_likes": [2, 9, "1", 2],'
                            ' "comment_reply": {"to": 2, "text": "“真的像油画！”"}, "reason": "好看"}')
        d = b.decide(v, [], True, True, True, True)
        self.assertEqual(d["comment_likes"], [1, 0])  # 编号从 1 开始；越界的、重复的去掉
        self.assertEqual(d["comment_reply"], {"to": 1, "text": "真的像油画！"})
        b = self.brain_with('{"interest": 8, "comment_likes": [1], "comment_reply": {"to": 1, "text": "嗯"}}')
        d = b.decide(v, [], True, True, False, False)  # 额度用完 / 关掉了：不给评论点赞也不回复
        self.assertEqual((d["comment_likes"], d["comment_reply"]), ([], None))
        self.assertNotIn("comment_likes", b.router.call.call_args.args[2])


class ApiTests(unittest.TestCase):
    def setUp(self):
        from app.api import Api

        self.db = make_db()
        self.agent = mock.Mock()
        self.agent.status.return_value = {"state": "idle", "label": "待命中", "logged_in": False, "running": False,
                                          "detail": "", "current": {}, "next_inbox_s": None, "alert": ""}
        self.tmp = Path(tempfile.mkdtemp())
        self.api = Api(self.db, self.agent)

    def test_state_theme_persona(self):
        r = self.api.get_state()
        self.assertTrue(r["ok"], r)
        self.assertFalse(r["data"]["models_ready"])
        self.assertFalse(self.api.set_theme("green")["ok"])
        self.assertTrue(self.api.set_theme("blue")["ok"])
        self.assertEqual(self.db.ui()["theme"], "blue")
        self.assertTrue(self.api.save_persona({"name": "雨雨酱", "signature": "嗨", "hacker": 1})["ok"])
        p = self.db.persona()
        self.assertEqual(p["name"], "雨雨酱")
        self.assertNotIn("hacker", p)

    def test_avatar_upload(self):
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (400, 300), (255, 200, 220)).save(buf, format="JPEG")
        data_url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        with mock.patch("app.api.AVATAR_DIR", self.tmp):
            r = self.api.upload_avatar(data_url)
            self.assertTrue(r["ok"], r)
            self.assertTrue(r["data"].startswith("data:image/png;base64,"))
            name = self.db.persona()["avatar"]
            with Image.open(self.tmp / name) as img:
                self.assertEqual(img.size, (320, 320))

    def test_provider_and_tasks(self):
        r = self.api.save_provider({"id": "deepseek", "api_key": "sk-test-1234567890", "base_url": "https://api.deepseek.com/v1",
                                    "models": "deepseek-chat, deepseek-reasoner", "price_in": "2", "price_out": "8"})
        self.assertTrue(r["ok"], r)
        provs = {p["id"]: p for p in self.api.get_providers()["data"]["providers"]}
        self.assertTrue(provs["deepseek"]["has_key"])
        self.assertEqual(provs["deepseek"]["models"], ["deepseek-chat", "deepseek-reasoner"])
        self.assertNotIn("sk-test-1234567890", str(provs))
        self.api.save_tasks({"write": {"provider": "deepseek", "model": "deepseek-chat"}})
        self.assertTrue(self.api.get_state()["data"]["models_ready"])
        self.assertTrue(self.api.start_agent()["ok"])
        self.agent.send.assert_called_with("start")

    def test_vision_models_and_request_extra(self):
        r = self.api.save_provider({"id": "deepseek", "models": "deepseek-flash, deepseek-v4-pro",
                                    "vision_models": ["deepseek-flash", "不存在的模型"],
                                    "request_extra": '{"thinking": {"type": "disabled"}}'})
        self.assertTrue(r["ok"], r)
        p = {x["id"]: x for x in self.api.get_providers()["data"]["providers"]}["deepseek"]
        self.assertEqual(p["vision_models"], ["deepseek-flash"])
        self.assertIn("disabled", p["request_extra"])
        bad = self.api.save_provider({"id": "deepseek", "models": "deepseek-flash", "request_extra": "{thinking: off"})
        self.assertFalse(bad["ok"])
        self.assertIn("JSON", bad["error"])

    def test_test_provider_reports_each_model(self):
        self.api.save_provider({"id": "deepseek", "api_key": "sk-x-1234567890", "models": "deepseek-flash, bad-model",
                                "vision_models": ["deepseek-flash"]})

        def fake_chat(p, model, *a, images=None, **k):
            if model == "bad-model":
                raise LLMError("Model Not Exist")
            return LLMResult(text="你好")

        with mock.patch("app.api.chat", side_effect=fake_chat):
            res = self.api.test_provider("deepseek")["data"]["results"]
        self.assertEqual([x["model"] for x in res], ["deepseek-flash", "bad-model"])
        self.assertTrue(res[0]["ok"] and res[0]["vision_ok"])
        self.assertFalse(res[1]["ok"])
        self.assertIn("Model Not Exist", res[1]["error"])


class FakeBrowser:
    """假的浏览器：found 里列出“页面上找得到”的元素及其文字。"""

    def __init__(self, found: dict):
        self.found = dict(found)
        self.sel = {"home_url": "https://www.douyin.com/?recommend=1"}
        self.page = mock.Mock(url="https://www.douyin.com/jingxuan")
        self.dumps, self.clicked = [], []

    def goto(self, url):
        pass

    def find(self, key, timeout=0, root=None):
        if key not in self.found:
            return None
        el = mock.Mock(name=key)
        el.text = self.found[key]
        el.get_attribute.return_value = ""
        el.click.side_effect = lambda: self.on_click(key)
        return el

    def on_click(self, key):
        self.clicked.append(key)
        if key == "nav_recommend":
            self.found["feed_active"] = ""

    @staticmethod
    def text_of(loc, limit=500):
        return "" if loc is None else loc.text

    def dump(self, tag):
        self.dumps.append(tag)
        return f"debug/{tag}"

    def pause(self, *a):
        pass

    def check_captcha(self):
        pass

    def dismiss_tips(self):
        return False

    loading = False
    late = {}  # 刷新 / 多等一会儿之后才出现的元素

    def loading_visible(self):
        return self.loading

    def wait_any(self, key, timeout=60, state="visible", reload_once=True):
        self.found.update(self.late)
        self.loading = False
        return self.find(key)

    def click(self, loc, timeout=0):
        loc.click()

    def safe_click(self, loc, what=""):
        loc.click()
        return True


class FeedTests(unittest.TestCase):
    def test_open_clicks_recommend_when_redirected_to_jingxuan(self):
        from app.douyin.feed import Feed

        b = FakeBrowser({"nav_recommend": "推荐"})
        Feed(b).open()
        self.assertEqual(b.clicked, ["nav_recommend"])

    def test_open_fails_loudly_instead_of_browsing_wrong_page(self):
        from app.douyin.browser import BrowserError
        from app.douyin.feed import Feed

        b = FakeBrowser({})
        with self.assertRaises(BrowserError):
            Feed(b).open()
        self.assertEqual(b.dumps, ["feed_open_fail"])

    def test_open_waits_when_recommend_page_is_still_loading(self):
        from app.douyin.feed import Feed

        b = FakeBrowser({})
        b.page.url = "https://www.douyin.com/?recommend=1"
        b.loading = True
        b.late = {"feed_active": ""}  # 网络慢：多等一会儿视频才出来
        Feed(b).open()
        self.assertEqual(b.dumps, [])

    def test_read_current_refuses_empty_video(self):
        from app.douyin.feed import Feed, FeedReadError

        with self.assertRaises(FeedReadError):
            Feed(FakeBrowser({})).read_current(with_comments=False)  # 没有正在播放的视频
        b = FakeBrowser({"feed_active": ""})
        with self.assertRaises(FeedReadError):
            Feed(b).read_current(with_comments=False)  # 有视频但读不到文案和作者
        self.assertEqual(b.dumps, ["feed_read_fail"])
        ok = Feed(FakeBrowser({"feed_active": "", "video_desc": "番茄牛腩", "video_author": "@厨房小林"}))
        v = ok.read_current(with_comments=False)
        self.assertEqual((v["desc"], v["author"]), ("番茄牛腩", "厨房小林"))


class WatchTests(unittest.TestCase):
    def jpeg(self):
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (640, 1136), (30, 30, 60)).save(buf, format="JPEG")
        return buf.getvalue()

    def run_watch(self, interest, can_see=True, pause_during=None):
        from app.agent.runner import Agent

        agent = Agent(make_db())
        agent._want_run = True
        agent._sleep = lambda s: agent._want_run
        agent.brain = mock.Mock()
        agent.brain.router.task_can_see.return_value = can_see
        agent.brain.triage.return_value = {"interest": interest, "reason": "测试", "cost": 0.001}
        agent.brain.decide.return_value = {"interest": interest, "like": False, "comment": "", "reason": "看完了", "cost": 0.002}
        img = self.jpeg()
        with mock.patch("app.agent.runner.Feed") as FeedCls:
            feed = FeedCls.return_value
            feed.read_current.return_value = {"video_id": "v1", "url": "u", "author": "怀旧音乐", "desc": "前奏一响", "comments": []}
            feed.capture_frames.side_effect = lambda n, d, sleep=None: [img] * n
            if pause_during == "decide":
                def decide(*a, **kw):
                    agent.request_pause()  # 她还在想的时候，用户点了一次“让她休息一下”
                    return {"interest": interest, "like": True, "comment": "好听", "reason": "", "cost": 0.0}
                agent.brain.decide.side_effect = decide
            agent._watch_one(mock.Mock(), agent.db.rules())
        self.feed = feed
        return agent, img

    def test_one_click_pause_stops_before_like_or_comment(self):
        agent, _ = self.run_watch(interest=9, pause_during="decide")
        self.feed.like.assert_not_called()
        self.feed.comment.assert_not_called()
        self.assertEqual(agent.state, "paused")
        self.assertFalse(agent.status()["running"])  # 按钮马上变回“开始刷视频”

    def test_collects_and_logs_it(self):
        from app.agent.runner import Agent

        agent = Agent(make_db())
        agent._want_run = True
        agent._sleep = lambda s: agent._want_run
        agent.brain = mock.Mock()
        agent.brain.router.task_can_see.return_value = True
        agent.brain.triage.return_value = {"interest": 9, "reason": "菜谱", "cost": 0}
        agent.brain.decide.return_value = {"interest": 9, "like": True, "collect": True, "comment": "",
                                           "reason": "想以后照着做", "cost": 0}
        with mock.patch("app.agent.runner.Feed") as FeedCls:
            feed = FeedCls.return_value
            feed.read_current.return_value = {"video_id": "v1", "url": "u", "author": "厨房小林", "desc": "番茄牛腩",
                                              "comments": []}
            feed.capture_frames.side_effect = lambda n, d, sleep=None: [self.jpeg()] * n
            feed.like.return_value = True
            feed.collect.return_value = True
            agent._watch_one(mock.Mock(), agent.db.rules())
        self.assertTrue(agent.brain.decide.call_args.kwargs["can_collect"])
        feed.collect.assert_called_once()
        self.assertEqual(agent.db.usage()["collects"], 1)
        self.assertEqual(len(agent.db.activity("collects")), 1)
        ev = agent.db.query("SELECT kind, title FROM events WHERE kind='collect'")
        self.assertTrue(ev and ev[0]["title"].startswith("点赞 + 收藏 @厨房小林"))

    def test_collect_switch_off(self):
        from app.agent.runner import Agent

        db = make_db()
        r = db.rules()
        r["collect_enabled"] = False
        db.set_json("rules", r)
        agent = Agent(db)
        agent._want_run = True
        agent._sleep = lambda s: agent._want_run
        agent.brain = mock.Mock()
        agent.brain.router.task_can_see.return_value = True
        agent.brain.triage.return_value = {"interest": 9, "reason": "", "cost": 0}
        agent.brain.decide.return_value = {"interest": 9, "like": False, "comment": "", "reason": "", "cost": 0}
        with mock.patch("app.agent.runner.Feed") as FeedCls:
            feed = FeedCls.return_value
            feed.read_current.return_value = {"video_id": "v1", "url": "u", "author": "a", "desc": "d", "comments": []}
            feed.capture_frames.side_effect = lambda n, d, sleep=None: [self.jpeg()] * n
            agent._watch_one(mock.Mock(), agent.db.rules())
        self.assertFalse(agent.brain.decide.call_args.kwargs["can_collect"])
        feed.collect.assert_not_called()

    def test_low_interest_still_looks_at_one_frame(self):
        agent, img = self.run_watch(interest=3)
        self.assertEqual(agent.brain.triage.call_args.args[1], [img])  # 判断时带上了 1 张画面
        self.assertEqual(len(agent.current["frames"]), 1)
        self.assertTrue(agent.current["frames"][0].startswith("data:image/jpeg;base64,"))
        agent.brain.decide.assert_not_called()

    def test_high_interest_shows_four_frames(self):
        agent, img = self.run_watch(interest=8)
        self.assertEqual(len(agent.brain.decide.call_args.args[1]), 4)
        self.assertEqual(len(agent.current["frames"]), 4)

    def test_blind_model_gets_no_images_but_ui_still_shows_frame(self):
        agent, img = self.run_watch(interest=3, can_see=False)
        self.assertEqual(agent.brain.triage.call_args.args[1], [])
        self.assertEqual(len(agent.current["frames"]), 1)


class CommentEngageTests(unittest.TestCase):
    def setUp(self):
        from app.agent.runner import Agent

        self.agent = Agent(make_db())
        self.agent.brain = mock.Mock()
        self.agent.brain.safety_check.return_value = (True, "", 0.001)
        self.feed = mock.Mock()
        self.feed.interact_comments.side_effect = lambda likes, replies: {"liked": likes, "replied": replies}
        self.items = [
            {"cid": "701", "user": "小林", "text": "这光影绝了", "likes": "5"},
            {"cid": "702", "user": "阿树", "text": "加微信带你赚钱", "likes": "1"},
            {"cid": "703", "user": "雨雨", "text": "我自己的评论", "likes": "0"},
        ]
        self.v = {"video_id": "v9", "desc": "秋景", "comment_items": self.items}

    def run_engage(self, likes, reply=None):
        dec = {"comment_likes": likes, "comment_reply": reply}
        return self.agent._engage_comments(self.feed, self.v, dec, self.agent.db.rules())

    def test_likes_and_replies_are_recorded_and_counted(self):
        self.run_engage([0], {"to": 0, "text": "确实，光打得好温柔"})
        likes, replies = self.feed.interact_comments.call_args.args
        self.assertEqual([c["cid"] for c in likes], ["701"])
        self.assertEqual(replies[0][1], "确实，光打得好温柔")
        usage = self.agent.db.usage()
        self.assertEqual((usage["comment_likes"], usage["comment_replies"]), (1, 1))
        self.assertTrue(self.agent.db.comment_done("701"))
        kinds = [e["kind"] for e in self.agent.db.events()]
        self.assertIn("comment_like", kinds)
        self.assertIn("comment_reply", kinds)
        # 同一条评论下次不再重复互动
        self.feed.interact_comments.reset_mock()
        self.run_engage([0])
        self.feed.interact_comments.assert_not_called()

    def test_skips_sensitive_own_and_unsafe(self):
        self.run_engage([1, 2])  # 引流评论、自己的评论都不碰
        self.feed.interact_comments.assert_not_called()
        self.agent.brain.safety_check.return_value = (False, "抬杠", 0.001)
        self.run_engage([], {"to": 0, "text": "你懂什么"})
        self.feed.interact_comments.assert_not_called()

    def test_respects_daily_limit_and_switch(self):
        rules = self.agent.db.rules()
        rules["limits"]["comment_likes"] = 0
        rules["limits"]["comment_replies"] = 0
        self.agent.db.set_json("rules", rules)
        self.run_engage([0], {"to": 0, "text": "好看"})
        self.feed.interact_comments.assert_not_called()
        rules["engage_comments"] = False
        rules["limits"]["comment_likes"] = 30
        self.agent.db.set_json("rules", rules)
        self.run_engage([0])
        self.feed.interact_comments.assert_not_called()


class CommentParseTests(unittest.TestCase):
    def test_parse_comment_reads_user_text_and_id(self):
        from app.douyin.feed import Feed

        b = FakeBrowser({"comment_user": "莯紫翊", "comment_text": "心底安宁，风都是美好的", "comment_like": "5"})
        b.sel["comment_id"] = ["[id^='tooltip_']"]
        item = mock.Mock()
        item.locator.return_value.first.get_attribute.return_value = "tooltip_7703018464164774693"
        c = Feed(b)._parse_comment(item)
        self.assertEqual(c, {"cid": "7703018464164774693", "user": "莯紫翊", "text": "心底安宁，风都是美好的", "likes": "5"})

    def test_old_usage_table_gets_new_columns(self):
        import sqlite3

        path = Path(tempfile.mkdtemp()) / "old.db"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE daily_usage (day TEXT PRIMARY KEY, videos INTEGER NOT NULL DEFAULT 0, "
                    "likes INTEGER NOT NULL DEFAULT 0, comments INTEGER NOT NULL DEFAULT 0, "
                    "replies_at INTEGER NOT NULL DEFAULT 0, replies_dm INTEGER NOT NULL DEFAULT 0, "
                    "tokens INTEGER NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0)")
        con.execute("INSERT INTO daily_usage(day, videos) VALUES('2026-11-06', 12)")
        con.commit()
        con.close()
        db = Database(path)
        db.add_usage("comment_likes", 1, day="2026-11-06")
        u = db.usage("2026-11-06")
        self.assertEqual((u["videos"], u["comment_likes"]), (12, 1))


class TipLayerTests(unittest.TestCase):
    def make_browser(self, tip_visible):
        from app.douyin.browser import DouyinBrowser

        b = DouyinBrowser.__new__(DouyinBrowser)
        b.sel = {"tip_close": ["[data-e2e='recommend-guide-mask'] button"]}
        b.page = mock.Mock()
        tip = b.page.locator.return_value.first
        tip.is_visible.return_value = tip_visible
        return b, tip

    def test_click_blocked_by_tip_closes_it_and_retries(self):
        b, tip = self.make_browser(tip_visible=True)
        target = mock.Mock()
        target.click.side_effect = [Exception("recommend-guide-mask intercepts pointer events"), None]
        with mock.patch("app.douyin.browser.cancel.sleep"):
            self.assertTrue(b.safe_click(target, "评论按钮"))
        tip.click.assert_called_once()  # 点了“我知道了”
        self.assertEqual(target.click.call_count, 2)

    def test_click_failure_without_tip_does_not_crash(self):
        b, tip = self.make_browser(tip_visible=False)
        target = mock.Mock()
        target.click.side_effect = Exception("Timeout 6000ms exceeded")
        self.assertFalse(b.safe_click(target, "评论按钮"))
        with self.assertRaises(Exception):
            b.click(target)


class DmTests(unittest.TestCase):
    def make_inbox(self):
        from app.douyin.inbox import Inbox

        b = mock.Mock()
        b.sel = {"dm_newest_first": True, "dm_from_me": "[class*='isFromMe']", "dm_unread_badge": ["[class*='unread']"],
                 "dm_text_bubble": ["[class*='MessageItemText']"], "dm_share_link": ["a[href*='/video/']"],
                 "dm_unsupported_text": "暂不支持该消息类型"}
        b.text_of.side_effect = lambda loc, limit=500: "" if loc is None else loc.text
        b.dump.return_value = "debug/x"
        b.known_user.return_value = None
        b.sel["dm_share_card"] = ["[class*='MessageItemShareAweme']"]
        Inbox._unknown_dumped = False
        Inbox._dumped_tags = set()
        return Inbox(b), b

    @staticmethod
    def msg(text, from_me, kind="text", href=""):
        m = mock.Mock(text=text)
        m.evaluate.return_value = from_me

        def locator(sel):
            loc = mock.Mock()
            hit = (kind == "text" and "MessageItemText" in sel) or (kind == "share" and href and "href" in sel)
            loc.count.return_value = 1 if hit else 0
            loc.first.count.return_value = 1 if hit else 0
            loc.first.get_attribute.return_value = href
            return loc
        m.locator.side_effect = locator
        return m

    def test_read_chat_reverses_newest_first_list_and_classifies(self):
        inbox, b = self.make_inbox()
        # 页面上最新的一条排在最前面
        b.find_all.return_value = [
            self.msg("帮我看看这个", False),
            self.msg("秋天的大兴安岭 @三月大的猫", False, kind="share", href="//www.douyin.com/video/7701287939262696714"),
            self.msg("暂不支持该消息类型展示 请到抖音手机端查看", True, kind="other"),
            self.msg("早呀", True)]
        msgs = inbox._read_chat()
        self.assertEqual([(m["kind"], m["from_me"]) for m in msgs],
                         [("text", True), ("unsupported", True), ("share", False), ("text", False)])
        self.assertEqual(msgs[2]["url"], "https://www.douyin.com/video/7701287939262696714")

    def run_fetch(self, inbox, chat):
        conv = mock.Mock()
        conv.locator.return_value.count.return_value = 1  # 有未读
        inbox.b.find_all.return_value = [conv]
        with mock.patch.object(inbox, "open_dm_panel", return_value=True), \
                mock.patch.object(inbox, "_back_to_list", return_value=True), \
                mock.patch.object(inbox, "_conv_user", return_value="雨."), \
                mock.patch.object(inbox, "_open_conv", return_value=True), \
                mock.patch.object(inbox, "_read_chat", return_value=chat):
            return inbox.fetch_unread_dms()

    def test_fetch_takes_their_messages_after_my_last_reply(self):
        inbox, b = self.make_inbox()
        items = self.run_fetch(inbox, [
            {"from_me": True, "kind": "text", "text": "早呀", "url": "", "el": None},
            {"from_me": False, "kind": "text", "text": "你好", "url": "", "el": None},
            {"from_me": False, "kind": "text", "text": "在吗", "url": "", "el": None}])
        self.assertEqual((items[0]["user"], items[0]["text"], items[0]["shares"]), ("雨.", "你好\n在吗", []))

    def test_fetch_picks_up_shared_video(self):
        inbox, b = self.make_inbox()
        items = self.run_fetch(inbox, [
            {"from_me": False, "kind": "share", "text": "秋天的大兴安岭", "url": "https://www.douyin.com/video/77", "el": None},
            {"from_me": False, "kind": "text", "text": "能帮我分析一下这个视频吗", "url": "", "el": None}])
        self.assertEqual(items[0]["shares"], [{"url": "https://www.douyin.com/video/77", "title": "秋天的大兴安岭"}])
        self.assertIn("[分享了一个视频] 作者 秋天的大兴安岭", items[0]["text"])

    def test_unreadable_share_is_saved_for_calibration(self):
        inbox, b = self.make_inbox()
        with mock.patch.object(inbox, "_share_link_by_click", return_value=""):
            items = self.run_fetch(inbox, [
                {"from_me": False, "kind": "share", "text": "", "url": "", "el": mock.Mock()},
                {"from_me": False, "kind": "unsupported", "text": "", "url": "", "el": None}])
        self.assertEqual(items[0]["shares"], [])
        self.assertIn("网页版打不开", items[0]["text"])
        b.dump.assert_called_once_with("dm_share_unreadable")  # 只保存一次

    def test_share_card_without_link_is_recognised(self):
        inbox, b = self.make_inbox()
        card = mock.Mock(text="Nuooo糯糯")
        card.evaluate.return_value = False
        card.locator.side_effect = lambda sel: mock.Mock(**{"count.return_value": 1 if "ShareAweme" in sel else 0,
                                                            "first.count.return_value": 0})
        b.find_all.return_value = [card]
        msgs = inbox._read_chat()
        self.assertEqual((msgs[0]["kind"], msgs[0]["text"], msgs[0]["url"]), ("share", "Nuooo糯糯", ""))

    def test_asks_to_resend_instead_of_looking_back(self):
        inbox, b = self.make_inbox()
        with mock.patch.object(inbox, "_share_link_by_click", return_value="https://www.douyin.com/video/77") as click:
            items = self.run_fetch(inbox, [
                {"from_me": False, "kind": "share", "text": "Nuooo糯糯", "url": "", "el": mock.Mock()},
                {"from_me": True, "kind": "text", "text": "看完啦～", "url": "", "el": None},
                {"from_me": False, "kind": "text", "text": "这视频里讲了什么内容帮我提炼一下关键信息", "url": "", "el": None}])
        click.assert_not_called()  # 以前的卡片不再翻出来
        self.assertEqual((items[0]["shares"], items[0]["need_share"]), ([], True))

    def test_only_newest_card_is_used(self):
        inbox, b = self.make_inbox()
        items = self.run_fetch(inbox, [
            {"from_me": False, "kind": "share", "text": "旧作者", "url": "https://www.douyin.com/video/1", "el": None},
            {"from_me": False, "kind": "share", "text": "新作者", "url": "https://www.douyin.com/video/2", "el": None},
            {"from_me": False, "kind": "text", "text": "帮我分析一下", "url": "", "el": None}])
        self.assertEqual([x["url"] for x in items[0]["shares"]], ["https://www.douyin.com/video/2"])
        self.assertFalse(items[0]["need_share"])

    def test_plain_chat_is_not_a_video_question(self):
        inbox, b = self.make_inbox()
        items = self.run_fetch(inbox, [{"from_me": False, "kind": "text", "text": "晚安呀", "url": "", "el": None}])
        self.assertFalse(items[0]["need_share"])

    def test_profile_comes_from_page_data_not_feed_links(self):
        inbox, b = self.make_inbox()
        b.known_user.return_value = {"nickname": "雨.", "sec_uid": "MS4wLjABAAAArain", "douyin_id": "rain_0612",
                                     "avatar": "https://p3.douyinpic.com/a.webp", "from_im": True}
        self.assertEqual(inbox.profile_url_from_chat("雨."), "https://www.douyin.com/user/MS4wLjABAAAArain")

    def test_same_name_check(self):
        from app.douyin.inbox import _same_name

        self.assertTrue(_same_name("雨.", "雨."))
        self.assertFalse(_same_name("人间注释", "雨."))
        self.assertFalse(_same_name("", "雨."))

    def test_video_url_normalizes_links(self):
        from app.douyin.inbox import video_url

        self.assertEqual(video_url("//www.douyin.com/video/7701287939262696714?previous_page=im"),
                         "https://www.douyin.com/video/7701287939262696714")
        self.assertEqual(video_url("https://www.douyin.com/jingxuan?modal_id=7701287939262696714"),
                         "https://www.douyin.com/video/7701287939262696714")
        self.assertEqual(video_url("https://www.douyin.com/user/self"), "")

    def test_reply_checks_message_really_sent(self):
        inbox, b = self.make_inbox()
        b.safe_click.return_value = True
        with mock.patch.object(inbox, "_open_conv", return_value=True), \
                mock.patch.object(inbox, "_read_chat", return_value=[{"from_me": True, "text": "你好呀～叫我雨雨就好啦"}]):
            self.assertTrue(inbox.reply_dm({"user": "雨."}, "你好呀～叫我雨雨就好啦"))
        with mock.patch.object(inbox, "_open_conv", return_value=True), \
                mock.patch.object(inbox, "_read_chat", return_value=[{"from_me": False, "text": "你好"}]):
            self.assertFalse(inbox.reply_dm({"user": "雨."}, "你好呀～"))
        with mock.patch.object(inbox, "_open_conv", return_value=False):
            self.assertFalse(inbox.reply_dm({"user": "不存在"}, "你好"))


class HiddenBrowserTests(unittest.TestCase):
    def test_focus_is_given_back_when_hidden(self):
        from app.douyin.browser import DouyinBrowser

        b = DouyinBrowser.__new__(DouyinBrowser)
        b.hidden = True
        with mock.patch("app.douyin.browser.winfocus") as wf:
            wf.foreground.return_value = 111
            with b.keep_focus():
                pass
            wf.give_back.assert_called_once_with(111, mock.ANY)
            b.hidden = False  # 扫码 / 验证时窗口在前面，不去抢焦点
            wf.give_back.reset_mock()
            with b.keep_focus():
                pass
            wf.give_back.assert_not_called()

    def _browser(self):
        from app.douyin.browser import DouyinBrowser

        b = DouyinBrowser.__new__(DouyinBrowser)
        b.hidden = True
        b.ctx = mock.Mock()
        b._page = mock.Mock()
        b._page.is_closed.return_value = False
        b._page.evaluate.return_value = [1280, 820]
        b._emu = {}
        b._hide_checked = 0.0
        cdp = b.ctx.new_cdp_session.return_value
        cdp.send.side_effect = lambda m, p=None: {"windowId": 7} if m == "Browser.getWindowForTarget" else {}
        return b, cdp

    def test_show_and_hide_move_the_window(self):
        from app.douyin.browser import ONSCREEN

        b, cdp = self._browser()
        with mock.patch("app.douyin.browser.winfocus") as wf:
            wf.work_area.return_value = None
            wf.virtual_screen.return_value = (0, 0, 1920, 1080)
            wf.system_scale.return_value = 1.0
            b.show_window()
            self.assertFalse(b.hidden)
            self.assertEqual(cdp.send.call_args.args[1]["bounds"], ONSCREEN)
            b.hide_window()
            self.assertTrue(b.hidden)
            off = [c.args[1]["bounds"] for c in cdp.send.call_args_list if c.args[0] == "Browser.setWindowBounds"][-1]
        self.assertLess(off["left"] + off["width"], 0)  # 整个窗口都在屏幕外

    def test_offscreen_works_with_monitor_on_the_left_and_scaling(self):
        from app.douyin import browser

        with mock.patch.object(browser, "winfocus") as wf:
            # 副屏（4K）摆在主屏左边：所有显示器的范围从 x=-3840 开始；系统缩放 150%
            wf.virtual_screen.return_value = (-3840, -500, 1920, 1080)
            wf.system_scale.return_value = 1.5
            off = browser.offscreen_bounds()
        # 换算回物理像素后，整个窗口都在最左边那块屏幕的左边外面
        self.assertLess((off["left"] + off["width"]) * 1.5, -3840)

    def test_onscreen_fits_small_scaled_screen(self):
        from app.douyin import browser

        with mock.patch.object(browser, "winfocus") as wf:
            wf.work_area.return_value = (0, 0, 1366, 728)  # 1366x768 笔记本，任务栏 40
            wf.system_scale.return_value = 1.25
            on = browser.onscreen_bounds("x")
        self.assertLessEqual(on["left"] + on["width"], 1366 / 1.25 + 1)
        self.assertLessEqual(on["top"] + on["height"], 728 / 1.25 + 1)
        self.assertGreaterEqual(on["left"], 0)

    def test_small_window_gets_fixed_layout_and_show_clears_it(self):
        b, cdp = self._browser()
        b._page.evaluate.return_value = [1093, 560]  # 小屏幕上窗口被系统缩小了
        with mock.patch("app.douyin.browser.winfocus") as wf:
            wf.work_area.return_value = None
            wf.virtual_screen.return_value = (0, 0, 1366, 768)
            wf.system_scale.return_value = 1.25
            b.hide_window()
            sent = [c.args[0] for c in cdp.send.call_args_list]
            self.assertIn("Emulation.setDeviceMetricsOverride", sent)
            self.assertIn(b._page, b._emu)
            b.show_window()
            self.assertEqual(b._emu, {})
            self.assertIn("Emulation.clearDeviceMetricsOverride", [c.args[0] for c in cdp.send.call_args_list])

    def test_big_enough_window_keeps_natural_layout(self):
        b, cdp = self._browser()
        with mock.patch("app.douyin.browser.winfocus") as wf:
            wf.virtual_screen.return_value = (0, 0, 1920, 1080)
            wf.system_scale.return_value = 1.0
            b.hide_window()
        self.assertNotIn("Emulation.setDeviceMetricsOverride", [c.args[0] for c in cdp.send.call_args_list])

    def test_ensure_hidden_rehides_window_that_came_back(self):
        b, cdp = self._browser()

        def send(m, p=None):
            if m == "Browser.getWindowForTarget":
                return {"windowId": 7}
            if m == "Browser.getWindowBounds":
                return {"bounds": {"left": 100, "top": 100, "width": 1280, "height": 860}}
            return {}
        cdp.send.side_effect = send
        with mock.patch("app.douyin.browser.winfocus") as wf:
            wf.system_scale.return_value = 1.0
            wf.virtual_screen.return_value = (0, 0, 1920, 1080)
            wf.rect_on_any_monitor.return_value = True
            b.ensure_hidden()
        moved = [c.args[1]["bounds"] for c in cdp.send.call_args_list
                 if c.args[0] == "Browser.setWindowBounds" and "left" in c.args[1]["bounds"]]
        self.assertTrue(moved and moved[-1]["left"] < 0)

    def test_winfocus_is_harmless_off_windows(self):
        from app import winfocus

        if sys.platform != "win32":
            self.assertEqual(winfocus.foreground(), 0)
            winfocus.give_back(0, "x")  # 不报错


class VerifyPopupTests(unittest.TestCase):
    def make(self):
        from app.agent.runner import Agent

        agent = Agent(make_db())
        b = mock.Mock()
        b.is_logged_in.return_value = False
        agent.browser = b
        return agent, b

    def test_sms_during_login_raises_popup_and_clears(self):
        agent, b = self.make()
        states = iter([False, False, True])
        b.is_logged_in.side_effect = lambda: next(states, True)
        b.sms_visible.return_value = True
        seen = []
        with mock.patch.object(agent, "_ensure_browser", return_value=b), \
                mock.patch("app.agent.runner.winfocus") as wf, mock.patch("app.agent.runner.time.sleep",
                                                                             side_effect=lambda s: seen.append(agent.attention)):
            agent._cmd_login()
        self.assertEqual(seen[0]["kind"], "sms")  # 等扫码时弹出了“需要填写手机验证码”
        self.assertIn("手机验证码", seen[0]["title"])
        wf.call_attention.assert_called()  # 软件窗口弹到前面
        b.show_window.assert_called()
        self.assertIsNone(agent.attention)  # 登录成功后弹窗自动关掉
        b.hide_window.assert_called()
        self.assertTrue(agent.logged_in)

    def test_captcha_while_running_uses_popup(self):
        agent, b = self.make()
        b.verify_kind.return_value = "captcha"
        visible = iter([True, False])
        b.captcha_visible.side_effect = lambda: next(visible, False)
        b.sms_visible.return_value = False
        agent._want_run = True
        with mock.patch("app.agent.runner.winfocus"):
            agent._on_captcha()
        self.assertIsNone(agent.attention)
        kinds = [e["title"] for e in agent.db.events()]
        self.assertIn("需要你完成一个验证", kinds)
        self.assertIn("验证完成，继续运行", kinds)
        self.assertIn("attention", agent.status())


class ReactDataTests(unittest.TestCase):
    def test_pick_unique(self):
        from app.douyin.inbox import pick_unique

        same = [{"value": "7701", "path": "props0.a"}, {"value": "7701", "path": "fiber0.1.b"}]
        self.assertEqual(pick_unique(same), "7701")
        conflict = [{"value": "7701", "path": "props0.a"}, {"value": "9999", "path": "props0.b"}]
        self.assertEqual(pick_unique(conflict), "")  # 两个不同的值：宁可不认
        prefer = conflict + [{"value": "9999", "path": "props0.message._parsedContent.itemId"}]
        self.assertEqual(pick_unique(prefer, prefer="_parsedContent"), "9999")
        self.assertEqual(pick_unique([]), "")

    def test_share_card_id_from_page_data(self):
        from app.douyin.inbox import Inbox

        b = mock.Mock()
        b.sel = {"dm_unsupported_text": "暂不支持该消息类型", "dm_share_link": ["a[href*='/video/']"],
                 "dm_share_card": ["[class*='MessageItemShareAweme']"], "dm_text_bubble": ["[class*='MessageItemText']"]}
        inbox = Inbox(b)
        card = mock.Mock()
        card.count.return_value = 1
        card.evaluate.return_value = [{"key": "itemId", "value": "7701287939262696714",
                                       "path": "fiber0.2.message._parsedContent.itemId"}]
        m = mock.Mock()
        m.locator.side_effect = lambda sel: mock.Mock(first=card if "ShareAweme" in sel else
                                                      mock.Mock(**{"count.return_value": 0}))
        self.assertEqual(inbox._classify(m, "Nuooo糯糯"), ("share", "https://www.douyin.com/video/7701287939262696714"))

    def test_partner_sec_uid_from_conversation_row(self):
        from app.douyin.inbox import Inbox

        b = mock.Mock()
        b.sel = {"dm_unread_badge": ["[class*='unread']"]}
        b.known_user.return_value = None
        inbox = Inbox(b)
        conv = mock.Mock()
        conv.locator.return_value.count.return_value = 1
        conv.evaluate.return_value = [{"key": "secUid", "value": "MS4wLjABAAAArainrainrain", "path": "props0.conv.secUid"}]
        b.find_all.return_value = [conv]
        with mock.patch.object(inbox, "open_dm_panel", return_value=True), \
                mock.patch.object(inbox, "_back_to_list", return_value=True), \
                mock.patch.object(inbox, "_conv_user", return_value="雨."), \
                mock.patch.object(inbox, "_conv_avatar", return_value=""), \
                mock.patch.object(inbox, "_open_conv", return_value=False):
            inbox.fetch_unread_dms()
        self.assertEqual(inbox.sec_uids, {"雨.": "MS4wLjABAAAArainrainrain"})
        self.assertEqual(inbox.profile_url_from_chat("雨."), "https://www.douyin.com/user/MS4wLjABAAAArainrainrain")


class DmInterruptTests(unittest.TestCase):
    def test_new_dm_cuts_video_short_in_instant_mode(self):
        from app.agent.runner import Agent

        agent = Agent(make_db())
        agent._want_run = True
        agent._cur_rules = agent.db.rules()  # 默认：自动回私信 + 秒回
        agent.browser = mock.Mock()
        agent.browser.dm_signal.side_effect = [False, True]
        with mock.patch.object(agent, "_sleep", return_value=True):
            self.assertFalse(agent._watch_sleep(30))  # 第二秒来了新私信，提前结束
        self.assertTrue(agent._dm_flag)
        agent._cur_rules = dict(agent._cur_rules, dm_reply_mode="5min")
        agent._dm_flag = False
        agent.browser.dm_signal.side_effect = None
        agent.browser.dm_signal.return_value = True
        with mock.patch.object(agent, "_sleep", return_value=True):
            self.assertTrue(agent._watch_sleep(2))  # 每 5 分钟回一次的模式不打断看视频


class MentionTests(unittest.TestCase):
    def make_inbox(self):
        from app.douyin.inbox import Inbox

        b = mock.Mock()
        b.sel = {"notice_at_keywords": ["@了你", "提到了你"]}
        b.text_of.side_effect = lambda loc, limit=500: "" if loc is None else loc.text
        b.dump.return_value = "debug/x"
        Inbox._dumped_tags = set()
        return Inbox(b), b

    @staticmethod
    def row(full, user, content, react=None):
        r = mock.Mock(text=full)
        r.evaluate.return_value = react or []
        r.locator.return_value.first.get_attribute.return_value = "//www.douyin.com/user/MS4wLjABAAAATdw4j8WDjAGx"
        r._user, r._content = mock.Mock(text=user), mock.Mock(text=content)
        return r

    def finder(self, b):
        def find(key, timeout=0, root=None):
            if root is None:
                return mock.Mock()  # 面板、入口都在
            return {"notice_user": root._user, "notice_content": root._content}.get(key)
        b.find.side_effect = find

    def test_reads_each_notification_row(self):
        inbox, b = self.make_inbox()
        self.finder(b)
        at = self.row("雨. 朋友 @Yu 快点来吃大肥鱼啦 在评论中提到了你 刚刚", "雨.", "@Yu 快点来吃大肥鱼啦",
                      react=[{"key": "aweme_id", "value": "7701287939262696714", "path": "props0.notice.aweme.aweme_id"}])
        like = self.row("半岛铁饭盒 赞了你的评论 10:24", "半岛铁饭盒", "")
        b.find_all.return_value = [at, like, like]
        items = inbox.fetch_mentions()
        self.assertEqual(len(items), 1)  # “赞了你的评论”不算 @
        self.assertEqual((items[0]["user"], items[0]["text"]), ("雨.", "@Yu 快点来吃大肥鱼啦"))
        self.assertEqual(items[0]["video_url"], "https://www.douyin.com/video/7701287939262696714")
        self.assertTrue(items[0]["user_url"].startswith("https://www.douyin.com/user/MS4wLjABAAAA"))

    def test_known_mentions_are_skipped_and_unknown_video_clicked(self):
        inbox, b = self.make_inbox()
        self.finder(b)
        a = self.row("小林 @Yu 你看这个 在评论中提到了你", "小林", "@Yu 你看这个")
        b.find_all.return_value = [a]
        with mock.patch.object(inbox, "_notice_video_by_click", return_value="https://www.douyin.com/video/88") as click:
            items = inbox.fetch_mentions()
        click.assert_called_once()
        self.assertEqual(items[0]["video_url"], "https://www.douyin.com/video/88")
        self.assertEqual(inbox.fetch_mentions(is_known=lambda ext: True), [])

    def test_reply_needs_video(self):
        inbox, b = self.make_inbox()
        self.assertFalse(inbox.reply_mention({"user": "雨.", "text": "@Yu 你好"}, "你好呀"))

    def test_runner_holds_mention_without_video(self):
        from app.agent.runner import Agent

        agent = Agent(make_db())
        agent.brain = mock.Mock()
        inbox = mock.Mock()
        mid = agent.db.add_message(kind="at", ext_id="at_1", user_name="雨.", content="@Yu 你看", status="pending", reason="")
        agent._handle_incoming(inbox, mid, {"user": "雨.", "text": "@Yu 你看", "video_url": ""}, "at", agent.db.rules())
        inbox.open_mention_video.assert_not_called()  # 不会拿推荐页上正在放的视频来回答
        agent.brain.reply_at.assert_not_called()
        self.assertEqual(agent.db.query("SELECT status FROM messages WHERE id=?", (mid,))[0]["status"], "pending")

    def test_bad_old_mentions_cleaned_once(self):
        import sqlite3

        path = Path(tempfile.mkdtemp()) / "x.db"
        db = Database(path)
        db.add_message(kind="at", ext_id="bad", user_name="搜索", content="刚刚 半岛铁饭盒 赞了你的评论 暂时没有更多了 消息",
                       status="failed", reason="")
        db.add_message(kind="at", ext_id="ok", user_name="小林", content="@Yu 你好", status="replied", reason="")
        db.close()
        con = sqlite3.connect(path)
        con.execute("DELETE FROM kv WHERE key='bad_at_cleanup_v1'")
        con.commit()
        con.close()
        db = Database(path)
        self.assertEqual([m["ext_id"] for m in db.messages("all")], ["ok"])


class PageDataTests(unittest.TestCase):
    def test_collects_users_from_douyin_json(self):
        from app.douyin.browser import DouyinBrowser

        b = DouyinBrowser.__new__(DouyinBrowser)
        b.users_seen = {}
        resp = mock.Mock(url="https://www.douyin.com/aweme/v1/web/im/user/info/?a=1",
                         headers={"content-type": "application/json"})
        resp.json.return_value = {"data": [{"uid": "1", "nickname": "雨.", "sec_uid": "MS4wLjABAAAArain",
                                            "unique_id": "rain_0612", "avatar_thumb": {"url_list": ["https://p3/a.webp"]}}]}
        b._on_response(resp)
        feed = mock.Mock(url="https://www.douyin.com/aweme/v1/web/tab/feed/?x", headers={"content-type": "application/json"})
        feed.json.return_value = {"aweme_list": [{"author": {"nickname": "雨.", "sec_uid": "OTHER", "unique_id": "x"}}]}
        b._on_response(feed)  # 同名的视频作者不会覆盖私信接口里的资料
        self.assertEqual(b.known_user("雨."), {"nickname": "雨.", "sec_uid": "MS4wLjABAAAArain", "douyin_id": "rain_0612",
                                               "avatar": "https://p3/a.webp", "from_im": True})

    def test_wrong_old_profiles_are_cleared_once(self):
        path = Path(tempfile.mkdtemp()) / "x.db"
        db = Database(path)
        db.save_dm_user("雨.", {"nickname": "人间注释", "douyin_id": "91028348669"}, checked=True)
        db.close()
        import sqlite3
        con = sqlite3.connect(path)
        con.execute("DELETE FROM kv WHERE key='dm_users_reset_v2'")  # 模拟从旧版本升级上来
        con.commit()
        con.close()
        db = Database(path)
        self.assertIsNone(db.dm_user("雨."))
        db.save_dm_user("雨.", {"nickname": "雨."}, checked=True)
        db.close()
        self.assertIsNotNone(Database(path).dm_user("雨."))  # 只清一次


class SharedVideoReplyTests(unittest.TestCase):
    def setUp(self):
        from app.agent.runner import Agent

        self.agent = Agent(make_db())
        self.agent.brain = mock.Mock()
        self.agent.brain.router.task_can_see.return_value = True
        self.agent.brain.reply_dm.return_value = Draft(text="看完啦，晨雾那段好美～", ok=True, reason="", cost=0.01, tokens=900)
        self.inbox = mock.Mock()
        self.inbox.reply_dm.return_value = True
        self.it = {"ext_id": "dm_1", "user": "雨.", "text": "[分享了一个视频] 秋天\n能帮我分析一下吗",
                   "shares": [{"url": "https://www.douyin.com/video/77", "title": "秋天"}]}
        self.mid = self.agent.db.add_message(kind="dm", ext_id="dm_1", user_name="雨.", content=self.it["text"],
                                             status="pending", reason="处理中")

    def test_watches_shared_video_before_replying(self):
        video = {"desc": "秋天的大兴安岭", "author": "三月大的猫", "comments": []}
        self.inbox.watch_shared.return_value = (video, [b"jpg"])
        self.agent._handle_incoming(self.inbox, self.mid, self.it, "dm", self.agent.db.rules())
        self.inbox.watch_shared.assert_called_once()
        kw = self.agent.brain.reply_dm.call_args.kwargs
        self.assertEqual((kw["video"], kw["frames"]), (video, [b"jpg"]))
        m = self.agent.db.query("SELECT * FROM messages WHERE id=?", (self.mid,))[0]
        self.assertEqual((m["status"], m["video_title"], m["video_url"]), ("replied", "秋天的大兴安岭", "https://www.douyin.com/video/77"))

    def test_unopenable_share_tells_model_honestly(self):
        self.inbox.watch_shared.side_effect = Exception("打开的不是视频详情页")
        self.agent._handle_incoming(self.inbox, self.mid, self.it, "dm", self.agent.db.rules())
        self.assertTrue(self.agent.brain.reply_dm.call_args.kwargs["video"]["unreadable"])

    def test_text_only_video_question_asks_for_card(self):
        it = {"ext_id": "dm_2", "user": "雨.", "text": "这个视频讲了什么", "shares": [], "need_share": True}
        mid = self.agent.db.add_message(kind="dm", ext_id="dm_2", user_name="雨.", content=it["text"],
                                        status="pending", reason="")
        self.agent._handle_incoming(self.inbox, mid, it, "dm", self.agent.db.rules())
        self.inbox.watch_shared.assert_not_called()
        self.assertEqual(self.agent.brain.reply_dm.call_args.kwargs["video"], {"need_share": True})

    def test_brain_need_share_prompt(self):
        b = Brain(make_db())
        b.router = mock.Mock()
        b.router.call.side_effect = [LLMResult(text="可以把视频重新分享给我一次吗～", cost=0.0), LLMResult(text='{"ok": true}', cost=0.0)]
        b.reply_dm("雨.", [], "这个视频讲了什么", video={"need_share": True})
        task, _, prompt = b.router.call.call_args_list[0].args[:3]
        self.assertEqual(task, "write")
        self.assertIn("重新分享", prompt)

    def test_brain_prompt_includes_video_and_uses_vision(self):
        b = Brain(make_db())
        b.router = mock.Mock()
        b.router.call.side_effect = [LLMResult(text="看完啦～晨雾好温柔", cost=0.01), LLMResult(text='{"ok": true}', cost=0.0)]
        d = b.reply_dm("雨.", [], "能帮我分析一下吗", video={"desc": "秋天的大兴安岭", "author": "猫", "comments": ["a：好美"]},
                       frames=[b"jpg"])
        self.assertTrue(d.ok)
        task, _, prompt = b.router.call.call_args_list[0].args[:3]
        self.assertEqual(task, "vision")
        self.assertIn("秋天的大兴安岭", prompt)


class DetailTests(unittest.TestCase):
    def test_comment_reply_detail_has_both_sides(self):
        db = make_db()
        v = {"desc": "秋天的大兴安岭", "author": "三月大的猫", "url": "https://www.douyin.com/video/1"}
        db.add_comment_action("701", "1", "小林", "这光影绝了", reply="真的！晨雾好温柔", video=v)
        db.add_event("comment_reply", "回复了 小林 的评论", "", ref="cmt:701")
        ev = db.events()[0]
        d = db.detail(ev["ref"])
        self.assertEqual(d["video"]["title"], "秋天的大兴安岭")
        self.assertEqual([(p["me"], p["text"]) for p in d["parts"]], [(False, "这光影绝了"), (True, "真的！晨雾好温柔")])
        self.assertEqual(len(db.activity("comment_replies")), 1)
        self.assertEqual(db.activity("comment_likes"), [])

    def test_own_comment_detail(self):
        db = make_db()
        vid = db.add_video(video_id="9", url="u", author="捉会鱼", title="风", interest=8, decision="comment",
                           liked=0, comment="这束光好温柔", reason="喜欢光影")
        d = db.detail(f"video:{vid}")
        self.assertIn({"who": "她的评论", "me": True, "text": "这束光好温柔"}, d["parts"])
        self.assertEqual(len(db.activity("comments")), 1)
        self.assertIsNone(db.detail("video:999"))

    def test_api_open_video_only_allows_douyin(self):
        from app.api import Api

        api = Api(make_db(), mock.Mock())
        with mock.patch("webbrowser.open") as op:
            self.assertTrue(api.open_video("https://www.douyin.com/video/1")["ok"])
            self.assertFalse(api.open_video("https://evil.example.com")["ok"])
        op.assert_called_once()


class InboxScheduleTests(unittest.TestCase):
    def make_agent(self, **rules):
        from app.agent.runner import Agent

        agent = Agent(make_db())
        r = agent.db.rules()
        r.update(rules)
        agent.db.set_json("rules", r)
        return agent

    def test_instant_mode_checks_badge_between_full_checks(self):
        agent = self.make_agent(dm_reply_mode="instant")
        agent._next_at = agent._next_dm = time.time() + 600
        b = mock.Mock()
        b.dm_signal.return_value = False
        with mock.patch("app.agent.runner.Inbox") as InboxCls:
            InboxCls.return_value.dm_badge.return_value = True
            self.assertEqual(agent._inbox_due(b, agent.db.rules()), (False, True))
            InboxCls.return_value.dm_badge.return_value = False
            self.assertEqual(agent._inbox_due(b, agent.db.rules()), (False, False))
            b.dm_signal.return_value = True  # 页面里的脚本发现未读数变多了
            self.assertEqual(agent._inbox_due(b, agent.db.rules()), (False, True))

    def test_five_minute_mode_ignores_badge(self):
        from app.agent.runner import dm_gap_seconds

        agent = self.make_agent(dm_reply_mode="5min")
        agent._next_at = agent._next_dm = time.time() + 600
        with mock.patch("app.agent.runner.Inbox") as InboxCls:
            InboxCls.return_value.dm_badge.return_value = True
            self.assertEqual(agent._inbox_due(mock.Mock(), agent.db.rules()), (False, False))
        self.assertEqual(dm_gap_seconds(agent.db.rules()), 300)
        self.assertEqual(dm_gap_seconds({"dm_reply_mode": "instant"}), 60)

    def test_daily_cap_stops_checking(self):
        agent = self.make_agent()
        r = agent.db.rules()
        r["limits"]["replies_at"] = 0
        r["limits"]["replies_dm"] = 0
        agent.db.set_json("rules", r)
        agent._next_at = agent._next_dm = 0
        with mock.patch("app.agent.runner.Inbox") as InboxCls:
            InboxCls.return_value.dm_badge.return_value = True
            self.assertEqual(agent._inbox_due(mock.Mock(), agent.db.rules()), (False, False))


class CuteStyleTests(unittest.TestCase):
    def test_kaomoji_only_when_cute_is_high(self):
        from app.agent.persona import cute_style

        self.assertIn("颜文字", cute_style({"cute": 85}))
        self.assertIn("每条回复", cute_style({"cute": 85}))
        self.assertIn("偶尔", cute_style({"cute": 60}))
        self.assertEqual(cute_style({"cute": 20}), "")
        self.assertIn("颜文字", system_prompt({"name": "雨雨", "traits": {"cute": 90}}))

    def test_clean_reply_keeps_kaomoji(self):
        self.assertEqual(safety.clean_reply("**好呀** ヾ(≧▽≦*)o"), "好呀 ヾ(≧▽≦*)o")
        self.assertEqual(safety.clean_reply("(*/ω＼*) 害羞啦"), "(*/ω＼*) 害羞啦")


class CaptchaPropagationTests(unittest.TestCase):
    def test_captcha_is_not_swallowed_by_like(self):
        from app.douyin.browser import CaptchaDetected
        from app.douyin.feed import Feed

        b = FakeBrowser({"like_button": ""})

        def boom():
            raise CaptchaDetected("text=验证码")
        b.check_captcha = boom
        with self.assertRaises(CaptchaDetected):
            Feed(b).like()  # 以前会被当成“点赞失败”吞掉，现在会一路传上去让她暂停


class SessionTests(unittest.TestCase):
    def make_browser(self, cookies):
        from app.douyin.browser import DouyinBrowser

        b = DouyinBrowser.__new__(DouyinBrowser)
        b.sel = {"login_cookie_names": ["sessionid"], "login_url": "https://www.douyin.com/",
                 "login_cookie_words": ["session", "sid", "passport"]}
        b.ctx = mock.Mock()
        b.ctx.cookies.side_effect = lambda *a: list(cookies)
        b.ctx.clear_cookies.side_effect = cookies.clear
        b.ctx.add_cookies.side_effect = cookies.extend
        b._page = mock.Mock()
        b._page.is_closed.return_value = False
        return b

    def test_save_restore_and_logout(self):
        import app.douyin.browser as br

        tmp = Path(tempfile.mkdtemp()) / "douyin_session.bin"
        jar = [{"name": "sessionid", "value": "abc", "domain": ".douyin.com", "path": "/", "expires": -1},
               {"name": "ttwid", "value": "dev", "domain": ".douyin.com", "path": "/", "expires": -1}]
        with mock.patch.object(br, "SESSION_FILE", tmp):
            b = self.make_browser(jar)
            self.assertTrue(b.save_session())
            self.assertNotIn('"sessionid"', tmp.read_text(encoding="utf-8"))  # 文件是加密 / 编码过的，不是明文
            jar.clear()  # 模拟浏览器被强制结束，登录丢了
            self.assertFalse(b.is_logged_in())
            self.assertTrue(b.restore_session())
            self.assertTrue(b.is_logged_in())
            with mock.patch.object(b, "goto"):
                b.logout()
            self.assertEqual([c["name"] for c in jar], ["ttwid"])  # 登录凭证清掉，设备 cookie 留着
            self.assertFalse(tmp.exists())

    def test_page_recovers_when_tab_closed(self):
        b = self.make_browser([])
        closed = b._page
        closed.is_closed.return_value = True
        other = mock.Mock()
        other.is_closed.return_value = False
        b.ctx.pages = [closed, other]
        self.assertIs(b.page, other)


class LoginMemoryTests(unittest.TestCase):
    def test_remembered_login_and_logout_without_browser(self):
        import app.paths as paths
        from app.agent.runner import Agent

        db = make_db()
        db.set_json("douyin_session", {"logged_in": True})
        agent = Agent(db)
        self.assertTrue(agent.logged_in)  # 上次登录过：直接显示已登录
        tmp = Path(tempfile.mkdtemp()) / "douyin_session.bin"
        tmp.write_text("x", encoding="utf-8")
        with mock.patch.object(paths, "SESSION_FILE", tmp):
            agent._cmd_logout()
        self.assertFalse(agent.logged_in)
        self.assertFalse(tmp.exists())
        self.assertEqual(db.get_json("douyin_session")["logout_pending"], True)
        self.assertFalse(Agent(db).logged_in)


class DmScopeTests(unittest.TestCase):
    def run_dm(self, follows):
        from app.agent.runner import Agent

        agent = Agent(make_db())
        r = agent.db.rules()
        r["dm_scope"] = "followers"
        agent.db.set_json("rules", r)
        if follows is not None:
            agent.db.save_dm_user("雨.", {"nickname": "雨", "douyin_id": "rain123", "follows_me": follows}, checked=True)
        agent.brain = mock.Mock()
        agent.brain.reply_dm.return_value = Draft(text="你好呀～", ok=True, reason="", cost=0.0, tokens=10)
        inbox = mock.Mock()
        inbox.reply_dm.return_value = True
        mid = agent.db.add_message(kind="dm", ext_id="d", user_name="雨.", content="你好", status="pending", reason="")
        agent._handle_incoming(inbox, mid, {"user": "雨.", "text": "你好"}, "dm", agent.db.rules())
        return agent.db.query("SELECT status FROM messages WHERE id=?", (mid,))[0]["status"]

    def test_only_followers_get_replies(self):
        self.assertEqual(self.run_dm(True), "replied")
        self.assertEqual(self.run_dm(False), "skipped_scope")
        self.assertEqual(self.run_dm(None), "pending")  # 确认不了就放进“待你处理”

    def test_messages_api_includes_profile(self):
        from app.api import Api

        db = make_db()
        db.save_dm_user("雨.", {"nickname": "雨", "douyin_id": "rain123", "avatar": "https://p3.douyinpic.com/a.webp",
                                "url": "https://www.douyin.com/user/MS4wLjABAAAA", "follows_me": True}, checked=True)
        db.add_message(kind="dm", ext_id="d", user_name="雨.", content="你好", status="replied", reason="")
        item = Api(db, mock.Mock()).get_messages("all")["data"]["items"][0]
        self.assertEqual((item["user"]["douyin_id"], item["user"]["follows_me"]), ("rain123", 1))
        self.assertTrue(db.dm_user("雨.")["checked_at"])

    def test_user_url(self):
        from app.douyin.inbox import user_url

        self.assertEqual(user_url("//www.douyin.com/user/MS4wLjABAAAAJSwQiIzI1tef?from=im"),
                         "https://www.douyin.com/user/MS4wLjABAAAAJSwQiIzI1tef")
        self.assertEqual(user_url("https://www.douyin.com/user/self"), "")


class WatchThroughTests(unittest.TestCase):
    def test_watches_to_the_end_and_spreads_frames(self):
        from app.douyin.feed import Feed

        b = FakeBrowser({"video_element": ""})
        video = mock.Mock()
        clock = {"t": 0.0}

        def evaluate(js, *args):
            if "isFinite" in js:
                return 20.0
            if "[el.currentTime, el.ended]" in js:
                return [clock["t"], clock["t"] >= 20]
            if "currentTime = t" in js:
                clock["t"] = args[0]
            return None
        video.evaluate.side_effect = evaluate
        b.find = lambda key, timeout=0, root=None: video if key == "video_element" else None
        progress = []

        def sleep(sec):
            clock["t"] += 4  # 每等一次，视频往前播 4 秒
            return True
        with mock.patch("app.douyin.feed._to_small_jpeg", side_effect=lambda png: b"jpg"):
            frames, watched, duration = Feed(b).watch_through(4, 180, sleep=sleep,
                                                              progress=lambda c, t: progress.append(c))
        self.assertEqual((len(frames), duration), (4, 20.0))
        self.assertGreaterEqual(watched, 19)
        self.assertTrue(progress)

    def test_long_video_fast_forwards_the_rest(self):
        from app.douyin.feed import Feed

        b = FakeBrowser({})
        video = mock.Mock()
        clock = {"t": 0.0, "seeks": []}

        def evaluate(js, *args):
            if "isFinite" in js:
                return 600.0
            if "[el.currentTime, el.ended]" in js:
                return [clock["t"], False]
            if "currentTime = t" in js:
                clock["seeks"].append(args[0])
            return None
        video.evaluate.side_effect = evaluate
        b.find = lambda key, timeout=0, root=None: video if key == "video_element" else None

        def sleep(sec):
            clock["t"] += 10
            return True
        with mock.patch("app.douyin.feed._to_small_jpeg", side_effect=lambda png: b"jpg"):
            frames, watched, duration = Feed(b).watch_through(6, 60, sleep=sleep)
        self.assertEqual(len(frames), 6)  # 前 60 秒看到 1 帧，后面 5 帧快进截
        self.assertTrue(all(t > 60 for t in clock["seeks"]))


class PauseTests(unittest.TestCase):
    def test_request_pause_is_immediate_and_idempotent(self):
        from app.agent.runner import Agent

        agent = Agent(make_db())
        agent._want_run = True
        agent.request_pause()
        self.assertFalse(agent._want_run)
        self.assertEqual(agent.state, "paused")
        agent.request_pause()  # 连点几次也只排一个暂停命令
        agent.request_pause()
        self.assertEqual(agent.q.qsize(), 1)
        agent._dispatch(*agent.q.get())
        agent._cmd_pause()
        self.assertEqual(sum(1 for e in agent.db.events() if e["kind"] == "pause"), 1)

    def test_capture_frames_stops_when_sleep_says_stop(self):
        from app.douyin.feed import Feed

        b = FakeBrowser({"video_element": ""})
        calls = []
        frames = Feed(b).capture_frames(4, 8, sleep=lambda s: calls.append(s) or False)
        self.assertEqual((frames, len(calls)), ([], 1))


class StartTests(unittest.TestCase):
    def test_start_does_not_run_when_feed_cannot_open(self):
        from app.agent.runner import Agent
        from app.douyin.browser import BrowserError

        agent = Agent(make_db())
        fake_b = mock.Mock()
        fake_b.is_logged_in.return_value = True
        with mock.patch.object(agent, "_ensure_browser", return_value=fake_b), \
                mock.patch("app.agent.runner.Feed") as FeedCls:
            FeedCls.return_value.open.side_effect = BrowserError("没能打开“推荐”页")
            agent._cmd_start()
        self.assertFalse(agent._want_run)
        self.assertIn("推荐", agent.alert)


class SelectorFileTests(unittest.TestCase):
    def test_old_file_is_upgraded_and_backed_up(self):
        import json
        from app.douyin import selectors

        tmp = Path(tempfile.mkdtemp()) / "selectors.json"
        tmp.write_text(json.dumps({"edit_profile_button": ["text=编辑资料"]}), encoding="utf-8")  # 第 1 版，没有 _version
        with mock.patch.object(selectors, "SELECTORS_FILE", tmp):
            data = selectors.load_selectors()
            self.assertEqual(data["edit_profile_button"], selectors.DEFAULT_SELECTORS["edit_profile_button"])
            self.assertTrue(tmp.with_suffix(".json.bak").exists())
            saved = json.loads(tmp.read_text(encoding="utf-8"))
            self.assertEqual(saved["_version"], selectors.SELECTORS_VERSION)
            # 同版本时，用户自己的修改要保留
            saved["bio_editor"] = ["#my-editor"]
            tmp.write_text(json.dumps(saved), encoding="utf-8")
            self.assertEqual(selectors.load_selectors()["bio_editor"], ["#my-editor"])


class RunnerIncomingTests(unittest.TestCase):
    def setUp(self):
        from app.agent.runner import Agent

        self.db = make_db()
        self.agent = Agent(self.db)
        self.agent.brain = mock.Mock()
        self.inbox = mock.Mock()
        self.inbox.reply_dm.return_value = True

    def add(self, text, user="小鹿"):
        return self.db.add_message(kind="dm", ext_id=f"dm_{text}", user_name=user, content=text, status="pending")

    def status(self, mid):
        return self.db.query("SELECT * FROM messages WHERE id=?", (mid,))[0]

    def test_sensitive_goes_to_pending(self):
        mid = self.add("买哪只股票好")
        self.agent._handle_incoming(self.inbox, mid, {"user": "小鹿", "text": "买哪只股票好"}, "dm", self.db.rules())
        m = self.status(mid)
        self.assertEqual(m["status"], "pending")
        self.assertIn("投资理财", m["reason"])
        self.inbox.reply_dm.assert_not_called()

    def test_normal_reply(self):
        self.agent.brain.reply_dm.return_value = Draft(text="嗨嗨～", ok=True, cost=0.002, tokens=50)
        mid = self.add("你好呀")
        self.agent._handle_incoming(self.inbox, mid, {"user": "小鹿", "text": "你好呀"}, "dm", self.db.rules())
        m = self.status(mid)
        self.assertEqual(m["status"], "replied")
        self.assertEqual(m["reply"], "嗨嗨～")
        self.assertEqual(self.db.usage()["replies_dm"], 1)
        self.assertEqual(len(self.db.dm_history("小鹿")), 2)

    def test_blocked_and_per_user_limit(self):
        self.db.block_user("坏人")
        mid = self.add("hi", user="坏人")
        self.agent._handle_incoming(self.inbox, mid, {"user": "坏人", "text": "hi"}, "dm", self.db.rules())
        self.assertEqual(self.status(mid)["status"], "blocked")
        rules = self.db.rules()
        rules["per_user_daily"] = 0
        mid2 = self.add("再来", user="小鹿")
        self.agent._handle_incoming(self.inbox, mid2, {"user": "小鹿", "text": "再来"}, "dm", rules)
        self.assertEqual(self.status(mid2)["status"], "pending")


if __name__ == "__main__":
    unittest.main()
