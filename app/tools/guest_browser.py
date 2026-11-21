"""“其他功能”专用的浏览器：和她刷抖音用的浏览器完全分开，不用她去操作，也不用等她手上的事。

和 Chrome 上的“抖音视频下载器”这类插件是一个道理：插件跑在网页里，读网页自己加载的作品数据
（/aweme/v1/web/aweme/detail/ 接口的返回、或者直接写在网页里的 RENDER_DATA），里面每一档清晰度的
play_addr 都是不带水印的原画质地址；读不到时再像“猫抓”一类嗅探插件那样，看网页实际在播放的视频文件地址。

这里就是在后台单独开一个看不见的浏览器（不登录、用一个临时的配置文件夹、用完就关），打开作品页，读同样的数据。
抖音网页版给没登录的访客也会加载作品数据，所以不需要登录。只有“作品分享页”那条纯网络的路走不通、
或者你要原画质（2K / 4K 那几档只在网页数据里有）时才会用到它。
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import time

from . import douyin as dy

log = logging.getLogger(__name__)

_LOCK = threading.Lock()          # 同一时间只开一个
DETAIL_API = "/aweme/v1/web/aweme/detail"
UA_PC = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 "
         "Safari/537.36 Edg/131.0.0.0")


class GuestBrowserError(RuntimeError):
    pass


def _launch(pw, proxy: str = ""):
    """按顺序试 Edge、Chrome、注册表里找到的浏览器；都不行时试 Playwright 自带的 Chromium。"""
    try:
        from ..douyin.browser import _browser_exes
        exes = _browser_exes()
    except Exception:  # noqa: BLE001
        exes = []
    tries = [{"channel": "msedge"}, {"channel": "chrome"}] + [{"executable_path": p} for p in exes] + [{}]
    kw = {"headless": True, "args": ["--mute-audio", "--no-first-run", "--no-default-browser-check"]}
    if proxy:
        kw["proxy"] = {"server": proxy}
    last = None
    for opt in tries:
        try:
            return pw.chromium.launch(**kw, **opt)
        except Exception as e:  # noqa: BLE001
            last = e
    raise GuestBrowserError("没能在后台打开浏览器（这台电脑上需要装有 Edge 或 Chrome）："
                            f"{str(last).splitlines()[0][:150] if last else ''}")


def read_detail(url: str, aweme_id: str = "", proxy: str = "", wait_s: float = 25.0, stop=lambda: False,
                sync_playwright=None) -> dict:
    """打开作品页，读作品数据。返回 {detail, cookies, ua, final_url, aweme_id, sniffed}。
    sniffed：网页里实际播放的视频地址（作品数据读不到时的后备，和嗅探插件一样）。"""
    if sync_playwright is None:
        from playwright.sync_api import sync_playwright
    from .links import douyin_id

    found: dict = {}
    sniffed: list[str] = []

    def on_response(resp):
        try:
            u = resp.url
            if DETAIL_API in u and resp.status == 200:
                d = (resp.json() or {}).get("aweme_detail")
                if isinstance(d, dict):
                    found[str(d.get("aweme_id") or "")] = d
            elif ("douyinvod.com" in u or "/aweme/v1/play" in u or "video/tos" in u) and resp.status in (200, 206):
                ct = (resp.headers or {}).get("content-type", "")
                if ("video" in ct or "octet-stream" in ct) and u not in sniffed:
                    sniffed.append(u)
        except Exception:  # noqa: BLE001 —— 个别返回读不了没关系
            pass

    with _LOCK:
        profile = tempfile.mkdtemp(prefix="aiwtt_guest_")
        pw = sync_playwright().start()
        browser = ctx = None
        try:
            browser = _launch(pw, proxy)
            ctx = browser.new_context(user_agent=UA_PC, locale="zh-CN", viewport={"width": 1280, "height": 800})
            page = ctx.new_page()
            page.on("response", on_response)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001 —— 加载慢也先看看拿到了什么
                log.info("后台浏览器打开作品页没等到加载完：%s", e)
            aid = aweme_id
            detail = None
            end = time.time() + wait_s
            while time.time() < end:
                if stop():
                    raise GuestBrowserError("取消了")
                aid = aid or douyin_id(str(page.url))
                detail = found.get(aid) if aid else (next(iter(found.values())) if found else None)
                if detail is None:
                    try:
                        detail = dy.from_render_text(page.evaluate(dy.RENDER_JS) or "", aid)
                    except Exception:  # noqa: BLE001
                        detail = None
                    if detail is not None and not (dy.video_choices(detail) or dy.images(detail)):
                        detail = None
                if detail is not None:
                    break
                page.wait_for_timeout(700)
            cookies = {}
            try:
                for c in ctx.cookies():
                    if "douyin" in (c.get("domain") or ""):
                        cookies[c["name"]] = c["value"]
            except Exception:  # noqa: BLE001
                pass
            return {"detail": detail, "cookies": cookies, "ua": UA_PC, "final_url": str(page.url),
                    "aweme_id": aid, "sniffed": list(sniffed)}
        finally:
            for closer in (ctx, browser):
                try:
                    if closer is not None:
                        closer.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                pw.stop()
            except Exception:  # noqa: BLE001
                pass
            shutil.rmtree(profile, ignore_errors=True)

