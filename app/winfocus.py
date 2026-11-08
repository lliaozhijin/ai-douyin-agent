"""Windows 窗口焦点小工具：她的浏览器藏在屏幕外面干活，不能抢走你正在用的窗口的键盘焦点。

只在 Windows 上生效；其他系统上全部是空操作。
"""
from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

_WIN = sys.platform == "win32"
if _WIN:  # pragma: no cover - 只在 Windows 上执行
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32")  # 自己的一份：设置函数参数类型时不影响别的库
    _kernel32 = ctypes.windll.kernel32
    # 显示器句柄在 64 位系统上是指针大小：声明好类型，免得被截断
    _user32.MonitorFromRect.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD]
    _user32.MonitorFromRect.restype = ctypes.c_void_p
    _user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    _user32.MonitorFromWindow.restype = ctypes.c_void_p
    _user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]


def foreground() -> int:
    """当前在最前面、有键盘焦点的窗口。"""
    if not _WIN:
        return 0
    try:
        return int(_user32.GetForegroundWindow() or 0)
    except Exception:  # noqa: BLE001
        return 0


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    if not _WIN or not hwnd:
        return None
    try:
        r = wintypes.RECT()
        if not _user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r)):
            return None
        return r.left, r.top, r.right, r.bottom
    except Exception:  # noqa: BLE001
        return None


def rect_on_any_monitor(rect: tuple[int, int, int, int]) -> bool:
    """这块区域和任何一块显示器有没有重叠（多显示器、显示器摆在主屏左边 / 上面都算对）。"""
    if not _WIN:
        return True
    try:
        r = wintypes.RECT(*[int(v) for v in rect])
        return bool(_user32.MonitorFromRect(ctypes.byref(r), 0))  # MONITOR_DEFAULTTONULL：不在任何屏幕上返回 0
    except Exception:  # noqa: BLE001
        l, t, rr, b = rect
        return not (rr <= 0 or b <= 0)


def is_offscreen(hwnd: int) -> bool:
    """窗口是不是整个在所有显示器外面（她的浏览器平时就放在那里）。最小化的窗口不算。"""
    if not _WIN or not hwnd:
        return False
    try:
        if _user32.IsIconic(wintypes.HWND(hwnd)):
            return False
    except Exception:  # noqa: BLE001
        pass
    rect = window_rect(hwnd)
    return bool(rect) and not rect_on_any_monitor(rect)


def virtual_screen() -> tuple[int, int, int, int]:
    """所有显示器拼在一起的范围（物理像素）：(left, top, right, bottom)。副屏在主屏左边 / 上面时 left / top 是负数。"""
    if not _WIN:
        return 0, 0, 1920, 1080
    try:
        x, y = _user32.GetSystemMetrics(76), _user32.GetSystemMetrics(77)    # SM_XVIRTUALSCREEN / SM_YVIRTUALSCREEN
        w, h = _user32.GetSystemMetrics(78), _user32.GetSystemMetrics(79)    # SM_CXVIRTUALSCREEN / SM_CYVIRTUALSCREEN
        if w > 0 and h > 0:
            return int(x), int(y), int(x + w), int(y + h)
    except Exception:  # noqa: BLE001
        pass
    return 0, 0, 1920, 1080


def system_scale() -> float:
    """Windows 的“缩放与布局”比例（100% → 1.0，150% → 1.5）。浏览器的窗口坐标按这个比例换算。"""
    if not _WIN:
        return 1.0
    try:
        if hasattr(_user32, "GetDpiForSystem"):
            dpi = int(_user32.GetDpiForSystem())
        else:
            dc = _user32.GetDC(None)
            try:
                dpi = int(ctypes.windll.gdi32.GetDeviceCaps(dc, 88))  # LOGPIXELSX
            finally:
                _user32.ReleaseDC(None, dc)
        return max(1.0, dpi / 96.0) if dpi else 1.0
    except Exception:  # noqa: BLE001
        return 1.0


def find_window(title: str) -> int:
    if not _WIN or not title:
        return 0
    try:
        return int(_user32.FindWindowW(None, title) or 0)
    except Exception:  # noqa: BLE001
        return 0


def focus(hwnd: int) -> bool:
    """把焦点还给 hwnd 这个窗口。"""
    if not _WIN or not hwnd:
        return False
    try:
        h = wintypes.HWND(hwnd)
        if not _user32.IsWindow(h):
            return False
        cur = _user32.GetForegroundWindow()
        cur_thread = _user32.GetWindowThreadProcessId(cur, None) if cur else 0
        me = _kernel32.GetCurrentThreadId()
        attached = bool(cur_thread and cur_thread != me and _user32.AttachThreadInput(me, cur_thread, True))
        try:
            ok = bool(_user32.SetForegroundWindow(h))
        finally:
            if attached:
                _user32.AttachThreadInput(me, cur_thread, False)
        return ok
    except Exception as e:  # noqa: BLE001
        log.debug("还焦点失败：%s", e)
        return False


def is_window(hwnd: int) -> bool:
    if not _WIN or not hwnd:
        return False
    try:
        return bool(_user32.IsWindow(wintypes.HWND(hwnd)))
    except Exception:  # noqa: BLE001
        return False


def find_window_containing(text: str) -> int:
    """找标题里含有 text 的可见顶层窗口（浏览器窗口标题后面会自动加上“ - Microsoft Edge”之类，没法按全名找）。"""
    if not _WIN or not text:
        return 0
    found: list[int] = []
    try:
        proc_t = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _lparam):
            try:
                if not _user32.IsWindowVisible(hwnd):
                    return True
                n = _user32.GetWindowTextLengthW(hwnd)
                if n <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(n + 1)
                _user32.GetWindowTextW(hwnd, buf, n + 1)
                if text in buf.value:
                    found.append(int(hwnd or 0))
                    return False
            except Exception:  # noqa: BLE001
                pass
            return True

        _user32.EnumWindows(proc_t(cb), 0)
    except Exception as e:  # noqa: BLE001
        log.debug("找窗口失败：%s", e)
    return found[0] if found else 0


def bring_to_front(hwnd: int) -> bool:
    """把窗口真正放到所有窗口最前面并给它焦点（浏览器自己“切到前台”在 Windows 上常常不生效，会被别的窗口挡住）。

    做法：最小化了先恢复；临时设成“置顶”再取消（这样不需要前台权限也能盖到最上面）；最后再把键盘焦点给它。"""
    if not _WIN or not hwnd:
        return False
    try:
        h = wintypes.HWND(hwnd)
        if not _user32.IsWindow(h):
            return False
        if _user32.IsIconic(h):
            _user32.ShowWindow(h, 9)                       # SW_RESTORE
        flags = 0x0001 | 0x0002 | 0x0040                   # SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
        _user32.SetWindowPos(h, wintypes.HWND(-1), 0, 0, 0, 0, flags)   # HWND_TOPMOST
        _user32.SetWindowPos(h, wintypes.HWND(-2), 0, 0, 0, 0, flags)   # HWND_NOTOPMOST：盖在最上面，但不一直置顶
        focus(hwnd)
        return True
    except Exception as e:  # noqa: BLE001
        log.debug("把窗口拿到最前面失败：%s", e)
        return False


def give_back(prev: int, fallback_title: str = "") -> None:
    """如果焦点被屏幕外的浏览器抢走了，还给原来的窗口（原来的窗口不行就还给软件自己的窗口）。"""
    cur = foreground()
    if not cur or not is_offscreen(cur):
        return  # 焦点不在藏起来的浏览器上：你自己切换了窗口，不去打扰
    target = prev if prev and prev != cur and not is_offscreen(prev) else find_window(fallback_title)
    if target and target != cur:
        focus(target)


def flash(hwnd: int, times: int = 5) -> None:
    """让任务栏上的图标闪几下，提醒你看过来。"""
    if not _WIN or not hwnd:
        return
    try:
        class FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND), ("dwFlags", wintypes.DWORD),
                        ("uCount", wintypes.UINT), ("dwTimeout", wintypes.DWORD)]
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), wintypes.HWND(hwnd), 0x3 | 0xC, times, 0)  # 标题栏+任务栏，直到切过来
        _user32.FlashWindowEx(ctypes.byref(info))
    except Exception:  # noqa: BLE001
        pass


def call_attention(title: str) -> None:
    """有需要你亲自处理的事（填手机验证码等）：把软件窗口拿到前面，任务栏图标闪烁。"""
    hwnd = find_window(title)
    if hwnd:
        try:
            _user32.ShowWindow(wintypes.HWND(hwnd), 9)  # SW_RESTORE：最小化了也恢复出来
        except Exception:  # noqa: BLE001
            pass
        focus(hwnd)
        flash(hwnd)


def work_area(near_title: str = "") -> tuple[int, int, int, int] | None:
    """可用区域（去掉任务栏）：(left, top, right, bottom)，物理像素。

    给了 near_title 时取这个窗口（软件主窗口）所在的那块显示器，不然取主屏。"""
    if not _WIN:
        return None
    try:
        hwnd = find_window(near_title) if near_title else 0
        if hwnd:
            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                            ("dwFlags", wintypes.DWORD)]
            mon = _user32.MonitorFromWindow(wintypes.HWND(hwnd), 2)  # MONITOR_DEFAULTTONEAREST
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if mon and _user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                r = mi.rcWork
                return r.left, r.top, r.right, r.bottom
        r = wintypes.RECT()
        if _user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):  # SPI_GETWORKAREA
            return r.left, r.top, r.right, r.bottom
    except Exception:  # noqa: BLE001
        pass
    return None
