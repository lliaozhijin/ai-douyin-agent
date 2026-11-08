"""在抖音创作者中心发布作品（图文 / 视频）。

一定会在“作品声明”里勾选“内容由AI生成”；找不到这个选项就不发布（宁可不发，也不发没有 AI 声明的作品）。
每一步找不到页面元素都会保存页面（data\\debug），方便校准 selectors.json 里 creator_ 开头的定位。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from .. import cancel
from .browser import BrowserError, DouyinBrowser
from .selectors import as_list

log = logging.getLogger(__name__)


class PublishError(BrowserError):
    pass


class CreatorLoginNeeded(PublishError):
    """创作者中心要求登录（和抖音网页版的登录通常是通的，偶尔需要再扫一次码）。"""


def _visible_text(b: DouyinBrowser, words: list[str], timeout: int = 300) -> str:
    for w in words:
        try:
            if b.page.get_by_text(w, exact=False).first.is_visible(timeout=timeout):
                return w
        except Exception:  # noqa: BLE001
            continue
    return ""


def _handle_verify(b: DouyinBrowser, wait_verify) -> bool:
    """页面上弹了验证（创作者中心点“发布”后常见的是“接收短信验证码”）：交给 wait_verify 弹窗等你处理。
    没有验证返回 False；有验证并且你完成了返回 True；有验证但没完成（或没法等）抛 PublishError。"""
    if not b.verification_open():
        return False
    kind = b.verify_kind() or "sms"
    log.info("发作品时抖音要求验证（%s）", kind)
    if wait_verify and wait_verify(kind):
        return True
    raise PublishError(f"发作品时抖音要求验证（{'手机短信验证码' if kind == 'sms' else '安全验证'}），"
                       f"没有在时间内完成，这次没有发出去（页面已保存到 {b.dump('creator_verify')}）。"
                       "可以再点一次“确认发布”，弹出验证时在浏览器里完成即可")


def publish(b: DouyinBrowser, draft: dict, files: list[Path], step=None, wait_login=None, wait_verify=None) -> str:
    """发布草稿。files：按顺序的图片（图文）或 1 个视频。返回成功说明；失败抛 PublishError。

    step(说明)：更新界面上的进度；wait_login()：创作者中心要登录时调用，返回 True 表示登录好了；
    wait_verify(kind)：抖音弹了验证（短信验证码 / 滑块）时调用，弹窗等你处理，返回 True 表示验证完了。"""
    ui_step = step or (lambda s: None)

    def step(text: str) -> None:
        log.info("发作品：%s", text)
        ui_step(text)

    is_video = draft.get("kind") == "video"
    url = b.sel.get("creator_upload_video" if is_video else "creator_upload_image")
    with b.in_new_tab(url):
        step("打开抖音创作者中心")
        if _visible_text(b, as_list(b.sel.get("creator_login_words")), 1500) or "login" in (b.page.url or ""):
            if not wait_login or not wait_login():
                raise CreatorLoginNeeded("创作者中心需要登录：请在她的浏览器里扫码登录创作者中心后再点发布")
            b.goto(url)
        step("等上传页面加载出来")
        # 创作者中心页面很重，网络慢时会一直转“加载中”：最多等 90 秒，中途刷新一次
        inp = b.wait_any("creator_file_input", timeout=90, state="attached")
        if inp is None and not is_video:
            # 页面加载好了但停在别的分页（比如“发布视频”）：点一下“发布图文”再找
            tab = b.find("creator_tab_image", timeout=3000)
            if tab is not None and b.safe_click(tab, "发布图文"):
                inp = b.wait_any("creator_file_input", timeout=20, state="attached", reload_once=False)
        if inp is None:
            path = b.dump("creator_no_input")
            if b.loading_visible():
                raise PublishError("抖音创作者中心的上传页面等了 90 秒（中途还刷新了一次）还是一直在“加载中”，"
                                   "可能是网络比较慢或者创作者中心暂时打不开。等一会儿再点“确认发布”试试"
                                   f"（页面已保存到 {path}）")
            raise PublishError(f"上传页面上没找到选择文件的按钮（页面已保存到 {path}）")
        step("上传视频" if is_video else f"上传 {len(files)} 张图片")
        inp.set_input_files([str(f) for f in files])
        title_box = b.find("creator_title", timeout=60000 if not is_video else 120000)
        if title_box is None:
            raise PublishError(f"上传后没进入填写作品信息的页面（页面已保存到 {b.dump('creator_no_form')}）")
        b.pause(1.5, 2.5)
        b.check_captcha()

        step("填写标题和简介")
        title = (draft.get("title") or "")[:30]
        if title:
            b.safe_click(title_box, "标题输入框")
            b.page.keyboard.press("Control+A")
            b.type_slowly(title)
        desc = b.find("creator_desc", timeout=5000)
        if desc is not None and b.safe_click(desc, "简介输入框"):
            b.page.keyboard.press("Control+A")
            b.page.keyboard.press("Backspace")
            b.type_slowly((draft.get("text") or "")[:900])
            for t in (draft.get("tags") or [])[:5]:
                b.type_slowly(f" #{t}")
                b.pause(0.8, 1.4)
                b.page.keyboard.press("Space")  # 话题输入完按空格，关掉话题候选
        else:
            log.warning("没找到简介输入框，只填了标题")

        step("勾选“内容由AI生成”声明")
        if not declare_ai(b):
            raise PublishError("没找到“内容由AI生成”的声明选项，为了合规这次没有发布"
                               f"（页面已保存到 {b.dump('creator_no_ai_declare')}）")

        if is_video:
            step("等视频上传完成")
            end = time.time() + 900
            while time.time() < end and _visible_text(b, as_list(b.sel.get("creator_uploading")), 300):
                cancel.sleep(3)
        _handle_verify(b, wait_verify)   # 发布前就弹了验证：先处理掉
        step("点“发布”")
        btn = b.find("creator_publish", timeout=10000)
        if btn is None or not b.safe_click(btn, "发布"):
            raise PublishError(f"没找到“发布”按钮（页面已保存到 {b.dump('creator_no_publish')}）")
        with cancel.shield():  # 已经点了“发布”：这时叫停也要等出结果，免得发出去了却显示没发
            end = time.time() + 90
            reclicked = False
            while time.time() < end:
                # 点了“发布”后抖音常要求二次验证（“接收短信验证码”）：弹窗等你在浏览器里填完，再接着等发布结果
                if _handle_verify(b, wait_verify):
                    step("验证完了，等发布结果")
                    end = time.time() + 90
                    b.pause(2.5, 3.5)
                    if _published(b):
                        return "发布成功"
                    # 验证完还停在填写页、“发布”按钮还在：说明这次点击被验证拦下了，再点一次（只补点一次）
                    again = None if reclicked else b.find("creator_publish", timeout=3000)
                    if again is not None and b.safe_click(again, "发布"):
                        reclicked = True
                        log.info("验证完成后重新点了一次“发布”")
                    continue
                b.check_captcha()
                if _published(b):
                    return "发布成功"
                cancel.sleep(2)
        raise PublishError(f"点了发布，但 90 秒内没看到发布成功的提示（页面已保存到 {b.dump('creator_publish_unknown')}）。"
                           "可以去抖音 App 的作品列表里确认一下")


def _published(b: DouyinBrowser) -> bool:
    if any(w in (b.page.url or "") for w in ("content/manage", "content/publish/success")):
        return True
    return bool(_visible_text(b, [w for w in as_list(b.sel.get("creator_done_words")) if "/" not in w], 500))


DECLARE_DIALOG = ("[role=dialog]:has-text('请选择声明类型')", ".semi-modal:has-text('请选择声明类型')",
                  ".semi-modal-content:has-text('内容由AI生成')")


def _declare_dialog(b: DouyinBrowser, timeout: float = 0.8):
    for css in DECLARE_DIALOG:
        try:
            loc = b.page.locator(css).last
            if loc.is_visible(timeout=int(timeout * 1000)):
                return loc
        except Exception:  # noqa: BLE001
            continue
    return None


def _pick_ai_option(b: DouyinBrowser, root) -> bool:
    """在声明弹窗里选上“内容由AI生成”这个单选项，选上了返回 True。

    弹窗刚弹出来时还在做动画，普通点击偶尔会超时；依次换几种点法（点整行 → 点单选圆圈），
    每点一次都确认单选框真的被选中了。"""
    for s in as_list(b.sel.get("creator_declare_ai")):
        try:
            opt = root.locator(s).first
            if not opt.is_visible(timeout=800):
                continue
        except Exception:  # noqa: BLE001
            continue
        tries = (
            lambda: opt.click(timeout=4000),
            lambda: opt.click(timeout=3000, force=True),
            lambda: opt.locator("xpath=ancestor-or-self::label[1]").locator("input[type=radio]").first
                       .check(timeout=3000, force=True),
            lambda: opt.evaluate("el => { const box = el.closest('label') || el;"
                                 " const i = box.querySelector('input[type=radio],input[type=checkbox]');"
                                 " (i || box).click(); }"),
        )
        for n, t in enumerate(tries, 1):
            try:
                t()
            except Exception as e:  # noqa: BLE001
                log.info("选“内容由AI生成”第 %d 种点法没成功：%s", n, " | ".join(str(e).splitlines()[:3])[:300])
            b.pause(0.4, 0.7)
            if _checked(opt) is True:
                return True
    return False


def declare_ai(b: DouyinBrowser) -> bool:
    """在作品声明里选“内容由AI生成”并点确定。确认页面上已经带上这条声明才返回 True。"""
    dialog = _declare_dialog(b)
    if dialog is None:
        entry = b.find("creator_declare_entry", timeout=3000)
        if entry is None:  # 声明藏在“高级设置”里：先展开
            adv = b.find("creator_advanced", timeout=3000)
            if adv is not None:
                b.safe_click(adv, "高级设置")
                b.pause(0.8, 1.4)
            entry = b.find("creator_declare_entry", timeout=3000)
        if entry is not None:
            try:
                entry.scroll_into_view_if_needed(timeout=2000)
            except Exception:  # noqa: BLE001
                pass
            b.safe_click(entry, "作品声明")
        dialog = _declare_dialog(b, timeout=4)
    if dialog is None:
        log.warning("没找到作品声明的弹窗")
        return False
    b.pause(0.8, 1.2)  # 等弹窗动画放完再点
    if not _pick_ai_option(b, dialog):
        log.warning("声明弹窗里没能选上“内容由AI生成”")
        return False
    # 选上之后“确定”才会亮起来
    ok_btn = None
    for _ in range(10):
        for s in as_list(b.sel.get("creator_declare_ok")):
            try:
                loc = dialog.locator(s).last
                if loc.is_visible(timeout=300) and loc.is_enabled(timeout=300):
                    ok_btn = loc
                    break
            except Exception:  # noqa: BLE001
                continue
        if ok_btn is not None:
            break
        cancel.sleep(0.5)
    if ok_btn is None:
        log.warning("声明弹窗的“确定”一直是灰的")
        return False
    if not b.safe_click(ok_btn, "确定"):
        try:
            ok_btn.click(timeout=3000, force=True)
        except Exception:  # noqa: BLE001
            return False
    for _ in range(10):  # 等弹窗关掉
        if _declare_dialog(b, timeout=0.3) is None:
            break
        cancel.sleep(0.5)
    else:
        log.warning("点了确定，声明弹窗还没关")
        return False
    # 确认发布页上已经显示了这条声明
    return bool(_visible_text(b, ["内容由AI生成", "AI生成"], 2000))


def _checked(loc):
    """选项旁边的单选 / 复选框有没有勾上；认不出返回 None。"""
    try:
        return loc.evaluate(
            "el => { const box = el.closest('label') || el.parentElement || el;"
            " if (box.classList && [...box.classList].some(c => /radio-checked|checkbox-checked/.test(c))) return true;"
            " const i = box.querySelector('input[type=radio],input[type=checkbox]');"
            " if (i) return i.checked;"
            " const c = box.querySelector('[aria-checked]'); return c ? c.getAttribute('aria-checked') === 'true' : null; }")
    except Exception:  # noqa: BLE001
        return None
