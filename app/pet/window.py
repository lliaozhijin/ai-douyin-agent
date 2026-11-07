"""桌宠的 Windows 透明窗口（分层窗口 + 每像素透明度），只用 ctypes 调系统接口，不需要额外安装任何东西。

窗口不抢焦点（WS_EX_NOACTIVATE）、不在任务栏显示（WS_EX_TOOLWINDOW）、总在最前（WS_EX_TOPMOST）；
透明的地方鼠标会穿过去，只有画了东西的地方能点。
"""
from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

from PIL import Image

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

WM_DESTROY, WM_CLOSE, WM_TIMER = 0x0002, 0x0010, 0x0113
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0200, 0x0201, 0x0202, 0x0203
WM_RBUTTONUP, WM_CAPTURECHANGED, WM_SETCURSOR = 0x0205, 0x0215, 0x0020
WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_EX_NOACTIVATE = 0x80000, 0x80, 0x8, 0x08000000
CS_DBLCLKS = 0x0008
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
ULW_ALPHA = 2
MF_STRING, MF_GRAYED, MF_CHECKED, MF_POPUP, MF_SEPARATOR = 0, 1, 8, 0x10, 0x800
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 2, 0x80, 0x100
IDC_HAND = 32649

if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    LRESULT = wintypes.LPARAM
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
                    ("hIconSm", wintypes.HICON)]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                    ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]

    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    user32.RegisterClassExW.restype = wintypes.ATOM
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                       wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, wintypes.LPVOID]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                           ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
                                           ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
                                           ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
    user32.UpdateLayeredWindow.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.SetCapture.argtypes = [wintypes.HWND]
    user32.SetCapture.restype = wintypes.HWND
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.MonitorFromPoint.restype = wintypes.HANDLE
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
    user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      wintypes.HWND, wintypes.LPVOID]
    user32.TrackPopupMenu.restype = ctypes.c_int
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPVOID]
    user32.LoadCursorW.restype = wintypes.HANDLE
    user32.SetCursor.argtypes = [wintypes.HANDLE]
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    if hasattr(user32, "GetDpiForWindow"):
        user32.GetDpiForWindow.argtypes = [wintypes.HWND]
        user32.GetDpiForWindow.restype = wintypes.UINT
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE


class LayeredWindow:
    """一个透明窗口。handler(msg, x, y) 接收鼠标消息；on_timer() 每 16 毫秒调用一次。必须在同一个线程里创建和运行。"""

    CLASS = "aiWannaTikTokPet"  # 每个窗口再加上自己的编号，重新打开桌宠时不会用到旧的回调

    def __init__(self, title: str, handler, on_timer):
        self.handler = handler
        self.on_timer = on_timer
        self.hwnd = None
        self._proc = WNDPROC(self._wndproc)  # 保存引用，避免被回收
        self._dib = None
        self._memdc = None
        self._old = None
        self._bits = ctypes.c_void_p()
        self._size = (0, 0)
        self._cursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_HAND))
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = CS_DBLCLKS
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.hCursor = self._cursor
        self._cls = f"{self.CLASS}{id(self)}"
        wc.lpszClassName = self._cls
        user32.RegisterClassExW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE,
                                           self._cls, title, WS_POPUP, 0, 0, 10, 10, None, None, hinst, None)
        if not self.hwnd:
            raise OSError(f"创建桌宠窗口失败（错误码 {ctypes.get_last_error()}）")
        user32.SetTimer(self.hwnd, 1, 16, None)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TIMER:
                self.on_timer()
                return 0
            if msg in (WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP, WM_CAPTURECHANGED):
                self.handler(msg)
                return 0
            if msg == WM_SETCURSOR:
                user32.SetCursor(self._cursor)
                return 1
            if msg == WM_CLOSE:
                user32.KillTimer(hwnd, 1)
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                self._free()
                user32.PostQuitMessage(0)
                return 0
        except Exception:  # noqa: BLE001 —— 窗口回调里出错不能让程序崩
            log.exception("桌宠窗口处理消息出错")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def loop(self) -> None:
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def close(self) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def show(self, on: bool) -> None:
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE if on else SW_HIDE)

    def _free(self) -> None:
        if self._memdc:
            if self._old:
                gdi32.SelectObject(self._memdc, self._old)
            gdi32.DeleteDC(self._memdc)
            self._memdc = None
        if self._dib:
            gdi32.DeleteObject(self._dib)
            self._dib = None

    def push(self, img: Image.Image, x: int, y: int) -> None:
        """把一帧 RGBA 图显示到屏幕 (x, y) 处。"""
        w, h = img.size
        screen = user32.GetDC(None)
        try:
            if (w, h) != self._size or not self._dib:
                self._free()
                bmi = BITMAPINFO()
                bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
                bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = w, -h  # 负数：从上到下
                bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
                self._dib = gdi32.CreateDIBSection(screen, ctypes.byref(bmi), 0, ctypes.byref(self._bits), None, 0)
                self._memdc = gdi32.CreateCompatibleDC(screen)
                self._old = gdi32.SelectObject(self._memdc, self._dib)
                self._size = (w, h)
            r, g, b, a = img.convert("RGBa").split()  # 预乘透明度
            data = Image.merge("RGBA", (b, g, r, a)).tobytes()  # 按 BGRA 排
            ctypes.memmove(self._bits, data, len(data))
            pt, src = wintypes.POINT(int(x), int(y)), wintypes.POINT(0, 0)
            size = wintypes.SIZE(w, h)
            blend = BLENDFUNCTION(0, 0, 255, 1)
            user32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(pt), ctypes.byref(size), self._memdc,
                                       ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA)
        finally:
            user32.ReleaseDC(None, screen)

    # ---------- 工具 ----------
    @staticmethod
    def cursor() -> tuple[int, int]:
        p = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(p))
        return p.x, p.y

    def capture(self, on: bool) -> None:
        if on:
            user32.SetCapture(self.hwnd)
        else:
            user32.ReleaseCapture()

    @staticmethod
    def work_area(x: int, y: int) -> tuple[int, int, int, int]:
        """(x, y) 所在屏幕除去任务栏的区域：(左, 上, 右, 下)。"""
        mon = user32.MonitorFromPoint(wintypes.POINT(int(x), int(y)), 2)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if mon and user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            r = mi.rcWork
            return r.left, r.top, r.right, r.bottom
        return 0, 0, 1920, 1040

    def dpi(self) -> int:
        try:
            return int(user32.GetDpiForWindow(self.hwnd)) or 96
        except (AttributeError, OSError):
            return 96

    def menu(self, items: list) -> int:
        """弹出右键菜单。items：[(id, 文字, 勾选, 可用)]、("-",)、或 ("子菜单名", [子项...])。返回选中的 id（0 = 没选）。"""
        def build(lst):
            h = user32.CreatePopupMenu()
            for it in lst:
                if it == ("-",):
                    user32.AppendMenuW(h, MF_SEPARATOR, 0, None)
                elif isinstance(it[1], list):
                    sub = build(it[1])
                    user32.AppendMenuW(h, MF_POPUP, sub, it[0])
                else:
                    iid, text, checked, enabled = (list(it) + [False, True])[:4]
                    flags = MF_STRING | (MF_CHECKED if checked else 0) | (0 if enabled else MF_GRAYED)
                    user32.AppendMenuW(h, flags, iid, text)
            return h
        h = build(items)
        x, y = self.cursor()
        user32.SetForegroundWindow(self.hwnd)  # 不这样做的话，点菜单外面菜单不会消失
        cmd = user32.TrackPopupMenu(h, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY, x, y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, 0, 0, 0)
        user32.DestroyMenu(h)
        return cmd
