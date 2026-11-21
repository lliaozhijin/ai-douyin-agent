"""第七轮：按 2026-11-20 的日志和页面校准——
① 名字里带 emoji 的人（网页把 emoji 画成图片，读出来的名字少了它）回复她，点通知进去明明看到了也认不出；
② 只发了一张图 / 表情包的回复（没有字）永远找不到；
③ 推荐页一直加载时刷新，抖音跳到“精选”，等满时间就报错；
④ 替主人搜视频时搜索页变成“验证码中间页”，标签页一关验证框就没了，主人那边一直没回音。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_features_v6 import _agent, _inbox, _msg, _plans  # noqa: E402

ME = "MS4wLjABAAAAme"


class NameAndMediaTests(unittest.TestCase):
    def test_names_ignore_emoji_and_punctuation(self):
        from app.douyin.browser import media_only, name_key, same_person
        self.assertEqual(name_key("吃白饭的鲸🐳"), "吃白饭的鲸")
        self.assertTrue(same_person("吃白饭的鲸🐳", "吃白饭的鲸"))
        self.assertTrue(same_person("漾屿.", "漾屿"))
        self.assertTrue(same_person("@小明同学🌟", "小明同学"))
        self.assertFalse(same_person("小明", "小红"))
        self.assertFalse(same_person("吃白饭的鲸🐳", "偷吃大肥鱼的白米饭"))
        self.assertTrue(same_person("🐳", "🐳"))                     # 名字只有 emoji：按原样比
        for t in ("[图片]", "[表情]", " [表情包] ", "@小明 [图片]", "[图片][图片]"):
            self.assertTrue(media_only(t), t)
        for t in ("[捂脸]", "哈哈[图片]", "", "图片", "[比心]好的"):
            self.assertFalse(media_only(t), t)

    def test_notice_api_finds_media_only_comment_by_author(self):
        from app.douyin.browser import DouyinBrowser
        b = DouyinBrowser.__new__(DouyinBrowser)
        b.notice_comments = []
        b._collect_notices({"comments": [
            {"cid": "7710207576773051173", "text": "", "sticker": {"static_url": {}}, "aweme_id": "7702013415473195883",
             "user": {"nickname": "漾屿."}, "reply_id": "7702332687415542587"},
            {"cid": "7710128864072991545", "text": "手动开关至少不会自己亮，不过半夜摸黑找按钮那下也挺阴间的",
             "user": {"nickname": "吃白饭的鲸🐳"}, "aweme_id": "7701545759291788578"}]})
        self.assertTrue(b.notice_comments[0]["media"])
        self.assertFalse(b.notice_comments[1]["media"])
        hit = b.notice_comment("[图片]", "漾屿.")
        self.assertEqual(hit["cid"], "7710207576773051173")
        self.assertIsNone(b.notice_comment("[图片]", "别人"))
        self.assertIsNone(b.notice_comment("[图片]", ""))
        # 通知里的名字带 emoji、接口里没带（或者反过来）也对得上
        self.assertEqual(b.notice_comment("手动开关至少不会自己亮，不过半夜摸黑找按钮那下也挺阴间的", "吃白饭的鲸")["cid"],
                         "7710128864072991545")


class _Row:
    def __init__(self, text, people, inner=""):
        self.text, self.people, self.inner = text, people, inner


class _Feed:
    def comment_text(self, c):
        return c.text

    def comment_people(self, c):
        return c.people

    def comment_author(self, c):
        return c.people[0]["name"] if c.people else ""


def _inbox_for_match():
    from app.douyin.inbox import Inbox
    ib = Inbox.__new__(Inbox)
    ib.b = mock.Mock(me_uid=ME)
    ib.b.text_of.side_effect = lambda c, n=400: c.inner
    ib.feed = _Feed()
    return ib


class MatchCommentTests(unittest.TestCase):
    def test_emoji_in_name_still_matches(self):
        ib = _inbox_for_match()
        other = _Row("灯亮着那感觉就是有人等你回家", [{"name": "吃白饭的鲸🐳", "href": "/user/whale"}])
        target = _Row("手动开关至少不会自己亮，不过半夜摸黑找按钮那下也挺阴间的，半斤八两。",
                      [{"name": "吃白饭的鲸🐳", "href": "/user/whale"}, {"name": "偷吃大肥鱼的白米饭", "href": "/user/" + ME}])
        hit = ib._match_comment([other, target], {"user": "吃白饭的鲸🐳", "text": "手动开关至少不会自己亮，不过半夜摸黑"})
        self.assertIs(hit, target)
        # 旧的读法：名字里的 emoji 读不出来
        target.people = [{"name": "吃白饭的鲸", "href": "/user/whale"}]
        self.assertIs(ib._match_comment([other, target], {"user": "吃白饭的鲸🐳", "text": "手动开关至少不会自己亮"}), target)
        # 正文读不出来、只能看整条文字时也一样
        target.text, target.inner = "", "吃白饭的鲸 偷吃大肥鱼的白米饭 ... 手动开关至少不会自己亮，不过 4分钟前·云南"
        self.assertIs(ib._match_comment([target], {"user": "吃白饭的鲸🐳", "text": "手动开关至少不会自己亮"}), target)
        # 别人说了一样的话：不认
        same_words = _Row("手动开关至少不会自己亮", [{"name": "邪恶大肥鱼", "href": "/user/x"}])
        self.assertIsNone(ib._match_comment([same_words], {"user": "吃白饭的鲸🐳", "text": "手动开关至少不会自己亮"}))

    def test_media_only_reply_found_by_author_and_reply_to_her(self):
        ib = _inbox_for_match()
        top = _Row("松开它！你捏疼它了，让我来[不失礼貌的微笑]", [{"name": "漾屿.", "href": "/user/yang"}])
        old_pic = _Row("[图片]", [{"name": "漾屿.", "href": "/user/yang"}])
        mine = _Row("笑死，让我来排队！这Q弹谁能忍住", [{"name": "偷吃大肥鱼的白米饭", "href": "/user/" + ME}])
        sticker = _Row("[图片]", [{"name": "漾屿.", "href": "/user/yang"}, {"name": "偷吃大肥鱼的白米饭", "href": "/user/" + ME}])
        hit = ib._match_comment([top, old_pic, mine, sticker], {"user": "漾屿.", "text": "[图片]"})
        self.assertIs(hit, sticker)                                   # 好几张图时挑回复她的那张
        self.assertIs(ib._match_comment([top, old_pic], {"user": "漾屿.", "text": "[表情]"}), old_pic)
        self.assertIsNone(ib._match_comment([top, mine], {"user": "漾屿.", "text": "[图片]"}))
        other = _Row("[图片]", [{"name": "别人", "href": "/user/o"}])
        self.assertIsNone(ib._match_comment([other], {"user": "漾屿.", "text": "[图片]"}))

    def test_comment_reading_scripts(self):
        from app.douyin.feed import Feed
        f = Feed(mock.Mock())
        src = Path(sys.modules[Feed.__module__].__file__).read_text(encoding="utf-8")
        self.assertIn("n.getAttribute('alt') || '[图片]'", src)          # 只有一张图的评论读成“[图片]”
        js = Feed.COMMENT_PEOPLE_JS
        for w in ("alt", "头像", "comment-item-avatar", "href"):
            self.assertIn(w, js)
        item = mock.Mock()
        item.evaluate.return_value = [{"name": "吃白饭的鲸🐳", "href": "//www.douyin.com/user/whale"},
                                      {"name": "偷吃大肥鱼的白米饭", "href": "//www.douyin.com/user/me"}]
        self.assertEqual(f.comment_author(item), "吃白饭的鲸🐳")
        item.evaluate.side_effect = RuntimeError("detached")
        self.assertEqual(f.comment_author(item), "")
        self.assertEqual(f.comment_people(item), [])

    def test_notice_path_expands_the_pinned_thread(self):
        from app.douyin.inbox import Inbox
        ib = Inbox.__new__(Inbox)
        ib.b = mock.Mock()
        page = mock.Mock(url="https://www.douyin.com/?recommend=1")
        ib.b.page = page
        page.context.expect_page.side_effect = RuntimeError("no new tab")
        ib.feed = mock.Mock()
        ib.feed.open_comments.return_value = True
        ib.feed.reply_on_item.return_value = True
        row = mock.Mock()
        ib._notice_row = mock.Mock(return_value=row)
        ib.close_notice_panel = mock.Mock()
        ib._find_comment = mock.Mock(return_value=None)
        top, reply = object(), object()
        ib.b.find.return_value = top
        ib._expand_until = mock.Mock(return_value=reply)
        res = ib._reply_via_notice({"user": "吃白饭的鲸🐳", "text": "手动开关"}, "哈哈是的")
        self.assertTrue(res)
        ib._expand_until.assert_called_once_with(top, mock.ANY, max_clicks=4)
        self.assertIs(ib.feed.reply_on_item.call_args.args[0], reply)


class FeedOpenTests(unittest.TestCase):
    def _browser(self):
        from tests.test_core import FakeBrowser
        b = FakeBrowser({"nav_recommend": "推荐"})
        clicks = []

        def on_click(key):
            clicks.append(key)
            b.clicked.append(key)
            b.page.url = "https://www.douyin.com/?recommend=1"
            if len(clicks) >= 2:
                b.found["feed_active"] = ""      # 第二次点“推荐”视频才出来

        b.on_click = on_click
        waits = []

        def wait_any(key, timeout=60, state="visible", reload_once=True):
            waits.append(reload_once)
            b.page.url = "https://www.douyin.com/jingxuan"    # 一直加载，最后跳到了精选
            return None

        b.wait_any = wait_any
        return b, waits

    def test_recommend_page_redirected_to_jingxuan_is_clicked_again(self):
        from app.douyin.feed import Feed
        b, waits = self._browser()
        Feed(b).open()
        self.assertEqual(b.clicked, ["nav_recommend", "nav_recommend"])
        self.assertEqual(waits, [False])                     # 等的时候不刷新（刷新 ?recommend=1 会跳到精选）
        self.assertEqual(b.dumps, [])

    def test_gives_up_after_a_few_tries(self):
        from app.douyin.browser import BrowserError
        from app.douyin.feed import Feed
        b, waits = self._browser()
        b.on_click = lambda key: b.clicked.append(key)        # 怎么点都出不来
        with self.assertRaises(BrowserError):
            Feed(b).open()
        self.assertEqual(len(b.clicked), Feed.OPEN_TRIES)
        self.assertEqual(b.dumps, ["feed_open_fail"])


class VerifyTests(unittest.TestCase):
    def _browser(self, title="验证码中间页"):
        from app.douyin.browser import DouyinBrowser
        b = DouyinBrowser.__new__(DouyinBrowser)
        b.sel = {"captcha": ["#captcha_container"], "second_verify": [], "sms_verify": []}
        page = mock.Mock()
        page.title.return_value = title
        page.locator.return_value.first.is_visible.return_value = False
        page.is_closed.return_value = False
        b.page = page
        b.ctx = mock.Mock(pages=[page])
        return b

    def test_middle_page_counts_as_verification(self):
        from app.douyin.browser import CaptchaDetected
        b = self._browser()
        with self.assertRaises(CaptchaDetected):
            b.check_captcha()
        self.assertTrue(b.verification_open())
        self.assertTrue(b.captcha_visible())
        ok = self._browser("猫咪 - 抖音搜索")
        ok.check_captcha()
        self.assertFalse(ok.verification_open())

    def test_new_tab_waits_for_verification_before_closing(self):
        from app.douyin.browser import CaptchaDetected
        b = self._browser()
        tab = mock.Mock()
        tab.is_closed.return_value = False
        b.ctx.new_page.return_value = tab
        b._emu = {}
        b.keep_focus = mock.MagicMock()
        b.goto = mock.Mock()
        b._activate_tab = mock.Mock()
        seen = []
        b.on_tab_verify = lambda: seen.append(b.page) or True
        with self.assertRaises(CaptchaDetected) as cm:
            with b.in_new_tab("https://www.douyin.com/search/x"):
                raise CaptchaDetected("middle_page")
        self.assertEqual(seen, [tab])                          # 在那个标签页还开着的时候等的
        self.assertTrue(cm.exception.solved)
        tab.close.assert_called_once()

    def _search_browser(self, verify_states, results_after_verify=True):
        b = mock.Mock()
        b.search_items = []
        states = list(verify_states)

        def visible():
            v = states.pop(0) if states else False
            return v

        b.captcha_visible.side_effect = visible
        b.in_new_tab.return_value = mock.MagicMock()
        b.page.evaluate.return_value = []
        b.dump.return_value = "debug/x"
        return b

    def test_search_stops_waiting_and_asks_for_verification(self):
        from app.douyin import search
        b = self._search_browser([True, True, True])
        with mock.patch.object(search.cancel, "sleep"):
            with self.assertRaises(search.SearchNeedsVerify):
                search.search_videos(b, "Lolita小裙子")                    # 没人能验证
            b = self._search_browser([True, True])
            asked = []
            with self.assertRaises(search.SearchNeedsVerify):
                search.search_videos(b, "Lolita小裙子", on_verify=lambda: asked.append(1) or False)
            self.assertEqual(asked, [1])
            b.dump.assert_not_called()                                        # 不是“没搜到”，不存页面

    def test_search_continues_after_verification(self):
        from app.douyin import search
        b = self._search_browser([True])

        def verify():
            b.search_items = [{"aweme_id": "7400000000000000001", "desc": "小裙子"}]
            return True

        with mock.patch.object(search.cancel, "sleep"):
            items = search.search_videos(b, "Lolita小裙子", on_verify=verify)
        self.assertEqual([x["aweme_id"] for x in items], ["7400000000000000001"])


class CommandVerifyTests(unittest.TestCase):
    def test_search_needing_verification_is_reported_to_owner(self):
        from app.douyin import search
        a = _agent()
        a._ensure_browser = mock.Mock(return_value=mock.Mock())
        a.wait_verify = mock.Mock(return_value=False)
        a.brain.command_step.side_effect = _plans(
            {"tool": "search_videos", "say": "收到～我去搜", "args": {"keyword": "Lolita小裙子"}},
            {"tool": "finish", "args": {"text": "抖音让我先过个验证\n等你帮我点一下再搜哦"}})
        it, mid = _msg(a, "搜个Lolita小裙子的视频发给我")

        def fake_search(b, kw, sort="", on_verify=None, **kw2):
            self.assertTrue(callable(on_verify))
            on_verify()
            raise search.SearchNeedsVerify("x")

        with mock.patch.object(search, "search_videos", side_effect=fake_search):
            self.assertTrue(a.commands.handle(_inbox(), mid, it, a.db.owner("小廖"), [], a.db.rules()))
        a.wait_verify.assert_called_once_with("替主人搜视频")
        prompt = a.brain.command_step.call_args_list[-1].args[1]
        self.assertIn("安全验证", prompt)
        self.assertEqual(a._send_dm_parts.call_args.args[2], ["抖音让我先过个验证", "等你帮我点一下再搜哦"])

    def test_captcha_midway_is_turned_into_a_step_result(self):
        from app.douyin.browser import CaptchaDetected
        a = _agent()
        a.wait_verify = mock.Mock(return_value=True)
        a.brain.command_step.side_effect = _plans(
            {"tool": "send_dm", "args": {"to": ["小红"], "text": "主人让我跟你说生日快乐"}},
            {"tool": "finish", "args": {"text": "发的时候抖音弹了验证\n不知道发出去没有"}})
        ib = _inbox()
        ib.open_chat_with.side_effect = CaptchaDetected("#captcha_container")
        it, mid = _msg(a, "去私信小红说生日快乐")
        self.assertTrue(a.commands.handle(ib, mid, it, a.db.owner("小廖"), [], a.db.rules()))
        a.wait_verify.assert_called_once_with("替主人私信别人")
        prompt = a.brain.command_step.call_args_list[-1].args[1]
        self.assertIn("不要重复去做", prompt)
        # 已经在新标签页里等过验证的，不再等第二次
        a.wait_verify.reset_mock()
        self.assertIn("没人处理", a.commands._verify_midway("watch_video", solved=False))
        self.assertIn("可以再试一次", a.commands._verify_midway("watch_video", solved=True))
        a.wait_verify.assert_not_called()

    def test_runner_wait_verify_and_tab_hook(self):
        from app.agent.runner import Agent
        from tests.test_features_v2 import make_db
        a = Agent(make_db())
        a.browser = mock.Mock()
        a.browser.verify_kind.return_value = "captcha"
        a.browser.verification_open.side_effect = [True, True, False, False]
        with mock.patch("app.agent.runner.winfocus"), mock.patch("app.agent.runner.cancel.sleep"):
            self.assertTrue(a.wait_verify("替主人搜视频"))
        a.browser.hide_window.assert_called()
        self.assertIsNone(a.attention)
        ev = a.db.query("SELECT * FROM events ORDER BY id DESC")[0]
        self.assertIn("替主人搜视频时的验证已完成", ev["title"])
        # 超时
        a.browser.verification_open.side_effect = None
        a.browser.verification_open.return_value = True
        with mock.patch("app.agent.runner.winfocus"), mock.patch("app.agent.runner.cancel.sleep"), \
                mock.patch("app.agent.runner.time.time", side_effect=[0, 0, 400, 400, 400]):
            self.assertFalse(a.wait_verify("替主人搜视频", max_s=300))
        # 弹窗里写着她在做什么
        with mock.patch("app.agent.runner.winfocus"):
            a._raise_attention("captcha", doing="替主人搜视频")
        self.assertIn("她在替主人搜视频", a.attention["body"])
        self.assertIn("她接着替主人搜视频", a.attention["body"])


if __name__ == "__main__":
    unittest.main()
