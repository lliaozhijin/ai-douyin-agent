"""程序入口。"""
from __future__ import annotations

import logging
import socket
import sys
from logging.handlers import RotatingFileHandler

from . import APP_NAME
from .paths import DATA_DIR, LOG_DIR, WEB_DIR, ensure_dirs

_SINGLE_INSTANCE_PORT = 49281


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    fh = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    if sys.stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


class _Mutex:
    def __init__(self, handle):
        self.handle = handle

    def close(self) -> None:
        try:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self.handle))
        except Exception:  # noqa: BLE001
            pass


def _single_instance():
    """防止同时开两个，抢同一个浏览器配置文件。

    Windows 上用系统的“命名互斥量”判断（不占用网络端口：别的软件正好用了某个端口也不会误判成“已经在运行”）；
    其他系统用本机端口。"""
    if sys.platform == "win32":
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        h = k32.CreateMutexW(None, False, "Local\\aiWannaTikTok_single_instance")
        if h and ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
            return _Mutex(h)
        if h:
            k32.CloseHandle(ctypes.c_void_p(h))
        return None
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", _SINGLE_INSTANCE_PORT))
    except OSError:
        return None
    return s


def _webview2_installed() -> bool:
    """主界面要用到微软的 WebView2 运行库（Win11 自带，Win10 绝大多数也装了）。"""
    if sys.platform != "win32":
        return True
    import winreg

    guid = r"{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    keys = [(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}"),
            (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"),
            (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}")]
    for root, path in keys:
        try:
            with winreg.OpenKey(root, path) as k:
                v, _ = winreg.QueryValueEx(k, "pv")
                if v and v != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def _ask_install_webview2() -> None:
    import ctypes
    import webbrowser

    msg = (f"{APP_NAME}的界面需要微软的“WebView2 运行库”，这台电脑上还没有装。\n\n"
           "点“确定”打开微软官网下载页，装好后再打开本软件。")
    if ctypes.windll.user32.MessageBoxW(None, msg, APP_NAME, 0x1 | 0x40) == 1:  # MB_OKCANCEL | MB_ICONINFORMATION
        webbrowser.open("https://developer.microsoft.com/microsoft-edge/webview2/")


def _bring_existing_to_front() -> bool:
    from . import winfocus

    hwnd = winfocus.find_window(APP_NAME)
    if hwnd:
        winfocus.call_attention(APP_NAME)
        return True
    return False


def _window_size() -> tuple[tuple[int, int], tuple[int, int]]:
    """主窗口大小按这台电脑的屏幕来：大屏幕 1360×900，小屏幕 / 缩放比例大时缩小到放得下。"""
    from . import winfocus

    area = winfocus.work_area()
    if not area:
        return (1360, 900), (1000, 680)
    s = winfocus.system_scale()
    w, h = (area[2] - area[0]) / s, (area[3] - area[1]) / s
    size = (int(min(1360, w * 0.94)), int(min(900, h * 0.94)))
    min_size = (int(min(1000, size[0])), int(min(680, size[1])))
    return size, min_size


def main() -> None:
    ensure_dirs()
    _setup_logging()
    log = logging.getLogger("main")

    if not _webview2_installed():
        log.error("没有找到 WebView2 运行库")
        _ask_install_webview2()
        return

    import webview  # pywebview

    lock = _single_instance()
    if lock is None:
        if _bring_existing_to_front():  # 已经开着：把开着的那个窗口拿到前面
            return
        webview.create_window(APP_NAME, html=f"<h2 style='font-family:sans-serif;padding:24px'>{APP_NAME}已经在运行啦～</h2>",
                              width=420, height=160)
        webview.start()
        return

    from .agent.runner import Agent
    from .api import Api
    from .db import Database

    db = Database()
    agent = Agent(db)
    agent.start()
    api = Api(db, agent)
    bg = "#FFF5F8" if db.ui().get("theme") == "pink" else "#F4F5F7"

    from .pet.host import PetHost, pet_settings

    def _call(fn):
        return lambda: fn()

    pet = PetHost(db, agent, {"open": api.request_page, "start": _call(api.start_agent), "pause": _call(api.pause_agent)})
    api._pet = pet

    (win_w, win_h), min_size = _window_size()
    window = webview.create_window(
        APP_NAME,
        url=str(WEB_DIR / "index.html"),
        js_api=api,
        width=win_w,
        height=win_h,
        min_size=min_size,
        background_color=bg,
    )

    api._window = window
    if pet_settings(db).get("enabled", True):
        pet.start()  # 桌宠在自己的线程里开一个透明窗口，和主界面是同一个程序

    def on_closing():
        log.info("窗口关闭，正在退出")
        pet.stop()
        agent.request_shutdown()
        agent.join(timeout=25)  # 等它把浏览器正常关掉（这样登录状态才会存好）
        db.close()

    window.events.closed += on_closing
    log.info("%s 启动（数据目录：%s）", APP_NAME, DATA_DIR)
    webview.start(debug=False)
    lock.close()


if __name__ == "__main__":
    main()
