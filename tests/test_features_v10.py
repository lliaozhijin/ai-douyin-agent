"""第十轮：按 2026-11-21 的日志修的两个问题。
① 发视频：“等视频上传完成”这一步从来没真正等过（等的那几个字写成了选择器的样子），
   “作品管理”四个字也被当成发布成功——视频还在传就点了发布、马上关了标签页，作品没发出去，软件却说成功了；
② 私信：聊天窗口开着时对方发来的、在手机上被看过的消息不出未读红点，以前只认红点，对方显示“已读”却等不到回音；
   回私信也不写日志，没回看不出为什么。"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_features_v2 import make_db  # noqa: E402


def _launch():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    try:
        return pw, pw.chromium.launch()
    except Exception:  # noqa: BLE001
        try:
            return pw, pw.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
        except Exception:  # noqa: BLE001
            pw.stop()
            raise


class UploadWaitTests(unittest.TestCase):
    def test_words_strip_old_selector_style(self):
        from app.douyin import publish as pub
        b = mock.Mock(sel={"creator_uploading": ["text=上传中", "正在上传", "text='取消上传'"]})
        self.assertEqual(pub._words(b, "creator_uploading"), ["上传中", "正在上传", "取消上传"])
        from app.douyin.selectors import DEFAULT_SELECTORS
        for key in ("creator_uploading", "creator_upload_done", "creator_upload_failed", "creator_done_words"):
            self.assertFalse(any(w.startswith("text=") for w in DEFAULT_SELECTORS[key]), key)
        self.assertNotIn("作品管理", DEFAULT_SELECTORS["creator_done_words"])

    def _real(self, html: str):
        try:
            pw, br = _launch()
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"没有可用的浏览器：{e}")
        from app.douyin.browser import DouyinBrowser
        from app.douyin.selectors import DEFAULT_SELECTORS
        ctx = br.new_context()
        page = ctx.new_page()
        page.set_content(html)
        b = DouyinBrowser.__new__(DouyinBrowser)
        b.sel, b.ctx, b._page = dict(DEFAULT_SELECTORS), ctx, page
        b.dump = mock.Mock(return_value="debug/x")
        return pw, br, b

    def test_waits_until_video_really_uploaded(self):
        from app.douyin import publish as pub
        html = ("<div id=s>上传中 35%</div><button>取消上传</button>"
                "<script>setTimeout(() => { document.body.innerHTML = '<div>重新上传</div><div>预览视频</div>'; }, 3000)"
                "</script>")
        pw, br, b = self._real(html)
        try:
            t = time.time()
            with mock.patch.object(pub.cancel, "sleep", side_effect=lambda s: time.sleep(min(s, 0.5))):
                pub.wait_upload(b, is_video=True)
            self.assertGreaterEqual(time.time() - t, 2.5)                  # 以前这里 0.05 秒就过去了
            self.assertTrue(pub._visible_text(b, ["重新上传"]))
        finally:
            br.close()
            pw.stop()

    def test_upload_failure_and_unknown_state(self):
        from app.douyin import publish as pub
        pw, br, b = self._real("<div>上传失败，点击重试</div>")
        try:
            with self.assertRaises(pub.PublishError) as cm:
                pub.wait_upload(b, is_video=True)
            self.assertIn("上传失败", str(cm.exception))
            b.page.set_content("<div>作品描述</div>")                        # 认不出上传状态
            with mock.patch.object(pub, "UPLOAD_UNKNOWN_S", 0), mock.patch.object(pub.cancel, "sleep"):
                pub.wait_upload(b, is_video=True)
            b.dump.assert_called_with("creator_upload_unknown")
            b.page.set_content("<div>已添加 3 张图片</div>")                  # 图片：没有“上传中”就算传完
            pub.wait_upload(b, is_video=False)
        finally:
            br.close()
            pw.stop()

    def test_menu_text_is_not_success(self):
        from app.douyin import publish as pub
        pw, br, b = self._real("<nav><a>首页</a><a>作品管理</a></nav><button>发布</button>")
        try:
            b.sel["creator_done_words"] = ["发布成功", "作品管理", "content/manage"]   # 你那边旧的 selectors.json 也不怕
            self.assertFalse(pub._published(b))
            b.page.set_content("<div class=toast>发布成功</div>")
            self.assertTrue(pub._published(b))
        finally:
            br.close()
            pw.stop()

    def test_publish_flow_waits_for_upload_before_clicking(self):
        from app.douyin import publish as pub
        b = mock.MagicMock()
        b.sel = {"creator_upload_video": "u", "creator_login_words": []}
        b.page.url = "https://creator.douyin.com/creator-micro/content/upload"
        order = []
        with mock.patch.object(pub, "_visible_text", return_value=""), \
                mock.patch.object(pub, "declare_ai", return_value=True), \
                mock.patch.object(pub, "wait_upload", side_effect=lambda *a: order.append("wait")), \
                mock.patch.object(pub, "_handle_verify", return_value=False), \
                mock.patch.object(pub, "_published", return_value=True), \
                mock.patch.object(pub, "_settle", side_effect=lambda *a: order.append("settle")):
            b.safe_click.side_effect = lambda el, what: order.append(what) or True
            self.assertEqual(pub.publish(b, {"kind": "video", "title": "t", "text": "x"}, [Path("v.mp4")]), "发布成功")
        self.assertLess(order.index("wait"), order.index("发布"))
        self.assertEqual(order[-1], "settle")                              # 发布成功后不马上关标签页


class ConfirmPostTests(unittest.TestCase):
    def test_confirm_on_profile_after_publishing(self):
        from app.agent.runner import Agent
        db = make_db()
        a = Agent(db)
        a.browser = mock.Mock()
        did = db.add_draft(status="published", title="冬天的猫", text="猫猫缩成一团好可爱", source="auto")
        db.update_draft(did, published_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        now = time.time()
        db.set_json("confirm_posts", {str(did): now - 200})
        with mock.patch("app.douyin.works.fetch_my_works", return_value=[]) as f:
            a._confirm_posts()
            self.assertEqual(f.call_count, 1)
            a._confirm_posts()                                             # 刚看过：等几分钟再看
            self.assertEqual(f.call_count, 1)
        self.assertIn(str(did), db.get_json("confirm_posts"))
        db.set_json("confirm_posts_at", 0)
        works = [{"aweme_id": "7500000000000000009", "desc": "冬天的猫 猫猫缩成一团好可爱", "likes": 0,
                  "create_time": int(now)}]
        with mock.patch("app.douyin.works.fetch_my_works", return_value=works):
            a._confirm_posts()
        self.assertEqual(db.get_json("confirm_posts"), {})
        self.assertTrue(db.query("SELECT 1 FROM events WHERE title LIKE '%已经在她主页上了%'"))

    def test_warns_when_not_on_profile_after_30_minutes(self):
        from app.agent.runner import Agent
        db = make_db()
        a = Agent(db)
        a.browser = mock.Mock()
        did = db.add_draft(status="published", title="", text="发了个视频", source="manual")
        db.set_json("confirm_posts", {str(did): time.time() - 1900})
        with mock.patch("app.douyin.works.fetch_my_works", return_value=[]):
            a._confirm_posts()
        self.assertEqual(db.get_json("confirm_posts"), {})
        self.assertIn("她主页上还没有这个作品", db.draft(did)["error"])
        self.assertIn("发了个视频", a.alert)
        a.browser = None
        db.set_json("confirm_posts", {"1": 0})
        a._confirm_posts()                                                 # 没开浏览器：不去看


class DmWatchTests(unittest.TestCase):
    def _inbox(self, convs, chats, unread=()):
        """假私信列表：convs 是会话列表 [(名字, 预览)]，chats[名字] 是点开后读到的消息。"""
        from app.douyin.inbox import Inbox
        ib = Inbox.__new__(Inbox)
        ib.b = mock.Mock()
        ib.sec_uids, ib.dup_names, ib.last_dm_check = {}, set(), {}
        ib.b.known_user.return_value = None
        rows = [mock.Mock(name=n, preview=p) for n, p in convs]
        for r, (n, p) in zip(rows, convs):
            r.user, r.pv = n, p
        ib.b.find_all.return_value = rows
        ib.b.text_of.side_effect = lambda el, n=200: getattr(el, "pv", "")
        ib.open_dm_panel = mock.Mock(return_value=True)
        ib._back_to_list = mock.Mock(return_value=True)
        ib.conv_names = mock.Mock(return_value=[n for n, _ in convs])
        ib._has_unread = lambda c: c.user in unread
        ib._conv_user = lambda c: c.user
        ib._conv_avatar = lambda c: ""
        opened = []

        def open_conv(u):
            opened.append(u)
            ib._cur = u
            return True

        ib._open_conv = open_conv
        ib.peer_uid = lambda u="": "MS4wLjABAAAAown"
        ib._read_chat = lambda n=24: chats.get(ib._cur, [])
        ib.opened = opened
        return ib

    def test_owner_chat_without_red_dot_is_still_checked(self):
        me = {"from_me": True, "kind": "text", "text": "好呀", "url": "", "el": None}
        them = {"from_me": False, "kind": "text", "text": "为什么不回信息", "url": "", "el": None}
        ib = self._inbox([("雨.", "为什么不回信息"), ("路人", "在吗")], {"雨.": [me, them], "路人": [them]})
        with mock.patch("app.douyin.inbox.react_values", return_value=[]):
            got = ib.fetch_unread_dms(watch={"雨."}, is_done=lambda u, p: False)
        self.assertEqual([x["user"] for x in got], ["雨."])                 # 没红点的路人不点开
        self.assertIn("为什么不回信息", got[0]["text"])
        self.assertEqual(ib.last_dm_check["watched"], ["雨."])
        ib = self._inbox([("雨.", "好呀")], {"雨.": [them, me]})
        with mock.patch("app.douyin.inbox.react_values", return_value=[]):
            self.assertEqual(ib.fetch_unread_dms(watch={"雨."}, is_done=lambda u, p: "好呀" in p), [])
        self.assertEqual(ib.opened, [])                                     # 列表上最后一句就是她回的：不用点开

    def test_red_dot_but_last_looks_mine_is_saved_for_calibration(self):
        from app.douyin.inbox import Inbox
        me = {"from_me": True, "kind": "text", "text": "好呀", "url": "", "el": None}
        ib = self._inbox([("雨.", "[引用]好呀 你看这个")], {"雨.": [me]}, unread={"雨."})
        Inbox._dumped_tags.discard("dm_unread_but_mine")
        ib.b.dump.return_value = "debug/x"
        with mock.patch("app.douyin.inbox.react_values", return_value=[]):
            self.assertEqual(ib.fetch_unread_dms(), [])
        ib.b.dump.assert_called_with("dm_unread_but_mine")
        self.assertIn("最后一条是她自己发的", ib.last_dm_check["skipped"][0][1])

    def test_runner_watch_list_and_check_log(self):
        from app.agent.runner import Agent
        db = make_db()
        db.save_friend("MS4wLjABAAAAyu", {"nickname": "雨.", "mutual": True})   # 主人得是互关好友
        db.save_owners([{"name": "雨.", "call": "爸爸", "enabled": True}])
        db.add_dm("小红", "them", "在吗")
        db.add_dm("老朋友", "them", "很久以前")
        db.execute("UPDATE dm_history SET ts='2020-01-01 00:00:00' WHERE user_name='老朋友'")
        a = Agent(db)
        self.assertEqual(a._dm_watch(db.rules()), {"雨.", "小红"})
        ib = mock.Mock(last_dm_check={"opened": True, "convs": 12, "unread": ["小红"], "watched": ["雨."],
                                      "skipped": [("小红", "最后一条是她自己发的（“好的”）")]})
        with self.assertLogs("app.agent.runner", "INFO") as cm:
            a._log_dm_check(ib, [("雨.", "回了")])
        line = cm.output[0]
        for w in ("会话 12 个", "有未读：小红", "没红点但最后一句不是她回的：雨.", "雨. 回了", "最后一条是她自己发的"):
            self.assertIn(w, line)

    def test_dm_done_with_sticker_as_last(self):
        from app.agent.runner import Agent
        db = make_db()
        a = Agent(db)
        db.add_dm("雨.", "me", "[发了一张表情包：开心]")
        self.assertTrue(a._dm_done("雨.", "[表情包]"))
        self.assertFalse(a._dm_done("雨.", "为什么不回信息"))


if __name__ == "__main__":
    unittest.main()
