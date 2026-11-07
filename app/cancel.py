"""“让她休息一下”的急停开关。

界面一点，STOP 立刻置位；后台线程在下一个检查点（找元素、点击、打字、等待、翻页……）
就抛出 OperationCancelled，手上的事马上放下，不等这条消息处理完、这个视频看完。

OperationCancelled 继承 BaseException：各处为了“单次失败不影响整体”写的 `except Exception`
不会把它吞掉，能一路冒到最外层统一收尾。
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

STOP = threading.Event()
_local = threading.local()
_agent_thread: threading.Thread | None = None  # 只对后台干活的线程生效，界面线程的操作不受影响


class OperationCancelled(BaseException):
    """你点了“让她休息一下”，当前操作被中止。"""


def bind_current_thread() -> None:
    """后台干活的线程启动时调用：急停只打断这个线程。"""
    global _agent_thread
    _agent_thread = threading.current_thread()


def _active() -> bool:
    """当前线程受急停控制吗（是后台干活线程，且不在 shield() 里）。"""
    if _agent_thread is not None and threading.current_thread() is not _agent_thread:
        return False
    return getattr(_local, "shield", 0) == 0


def _shielded() -> bool:
    return not _active()


def requested() -> bool:
    return STOP.is_set() and _active()


def checkpoint() -> None:
    """检查点：已经叫停就抛 OperationCancelled。"""
    if requested():
        raise OperationCancelled()


def sleep(seconds: float) -> None:
    """可被急停打断的 sleep：叫停后马上醒来并抛 OperationCancelled。"""
    checkpoint()
    if seconds <= 0:
        return
    if _shielded():
        time.sleep(seconds)
        return
    if STOP.wait(seconds):
        checkpoint()


def call(fn, *args, **kwargs):
    """调用一个可能要等很久、本身没法中断的函数（比如请求大模型）。

    叫停时不再等它返回，马上抛 OperationCancelled（那次请求在后台自己结束，结果丢掉）。"""
    checkpoint()
    if not _active():
        return fn(*args, **kwargs)
    box: dict = {}
    done = threading.Event()

    def work():
        try:
            box["v"] = fn(*args, **kwargs)
        except BaseException as e:  # noqa: BLE001 —— 原样交回调用方
            box["e"] = e
        finally:
            done.set()

    threading.Thread(target=work, daemon=True, name="cancellable-call").start()
    while not done.wait(0.2):
        checkpoint()
    if "e" in box:
        raise box["e"]
    return box["v"]


@contextmanager
def shield():
    """这一段不能半路停（比如已经点了“发布”，正在等发布结果）：期间不响应急停。"""
    _local.shield = getattr(_local, "shield", 0) + 1
    try:
        yield
    finally:
        _local.shield -= 1


def reset() -> None:
    STOP.clear()
