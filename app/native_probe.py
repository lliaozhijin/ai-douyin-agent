"""C++ 组件“先在小进程里试一下”。

onnxruntime（桌宠抠图）、ctranslate2（听声音）这些组件是 C++ 写的。电脑上的 Microsoft VC++ 运行库（msvcp140.dll）
低于 14.40 时，用 VS 2022 17.10 以后的编译器编出来的它们一启动就“访问冲突”崩溃——连 Python 的报错都来不及，
整个软件直接闪退（2026-11-19：onnxruntime 1.32.0、ctranslate2 4.9.0 在一台运行库旧的电脑上就是这样）。

所以第一次用之前，先开一个看不见的小进程导入一下：没问题才在软件里用；崩了就记下来、给出原因和办法，软件照常运行。
结果按“软件版本 + 组件版本 + 电脑上 msvcp140.dll 的版本”记在 data/native_probe.json：同样的情况只试一次；
你更新了 VC++ 运行库或者组件，会自动再试。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .paths import DATA_DIR, ROOT

log = logging.getLogger(__name__)

PROBE_FILE = DATA_DIR / "native_probe.json"
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
VC_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
VC_HINT = f"多半是这台电脑上的 Microsoft VC++ 运行库太旧（需要 14.40 以上）：安装 {VC_URL} 后重启软件就好"
# Windows 上“程序崩了”的退出码：访问冲突 0xC0000005、栈缓冲区溢出 / 快速失败 0xC0000409（有的地方是负数）
CRASH_CODES = {0xC0000005, 0xC0000409, 0xC0000005 - (1 << 32), 0xC0000409 - (1 << 32)}


def runtime_stamp() -> str:
    """电脑上 msvcp140.dll 的大小和修改时间：更新了 VC++ 运行库就会变（不加载它，只看文件）。"""
    if sys.platform != "win32":
        return ""
    f = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "msvcp140.dll"
    try:
        st = f.stat()
        return f"{st.st_size}-{int(st.st_mtime)}"
    except OSError:
        return "none"


def exe_stamp() -> str:
    """打包后的程序：程序文件的修改时间（重新安装 / 升级就会变）。"""
    if not getattr(sys, "frozen", False):
        return ""
    try:
        return str(int(Path(sys.executable).stat().st_mtime))
    except OSError:
        return ""


def crash_hint(code: int) -> str:
    return VC_HINT if code in CRASH_CODES else ""


def command(args: list[str]) -> list[str]:
    """开小进程用的命令：打包后的程序就是它自己，源码运行时是 run.py。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, str(ROOT / "run.py"), *args]


def pkg_version(pkg: str) -> str:
    from importlib import metadata
    try:
        return metadata.version(pkg)
    except Exception:  # noqa: BLE001 —— 打包后的程序里不一定读得到
        return "?"


def _read() -> dict:
    try:
        d = json.loads(PROBE_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(key: str, res: dict) -> None:
    d = _read()
    d[key] = {**res, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    d = dict(list(d.items())[-10:])
    try:
        PROBE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROBE_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def import_ok(module: str, pkg: str = "") -> str:
    """在小进程里导入 module 试试。返回空字符串 = 可以放心在软件里导入，否则返回原因（给用户看的话）。"""
    if module in sys.modules:                 # 已经导入过了（没崩）
        return ""
    from . import APP_VERSION
    key = f"{APP_VERSION}|{module}|{pkg_version(pkg or module)}|{runtime_stamp()}|{exe_stamp()}"
    hit = _read().get(key)
    if hit:
        return "" if hit.get("ok") else (hit.get("reason") or f"{module} 在这台电脑上用不了")
    out = Path(tempfile.gettempdir()) / f"aiwtt_import_{os.getpid()}_{module}.json"
    trace = out.with_suffix(".trace")
    for f in (out, trace):
        try:
            f.unlink()
        except OSError:
            pass
    try:
        r = subprocess.run(command(["--probe-import", module, str(out)]), cwd=str(ROOT), capture_output=True,
                           timeout=180, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return f"试着加载 {module} 太久没反应（这次先不用，下次启动再试）"
    except Exception as e:  # noqa: BLE001 —— 连小进程都开不了：不试了（和以前一样直接用）
        log.info("没能开小进程试 %s（%s），直接用", module, e)
        return ""
    try:
        res = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        res = {}
    if res.get("ok"):
        _save(key, {"ok": True})
        return ""
    tail = ""
    try:
        tail = "\n".join(trace.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-12:])
    except OSError:
        pass
    if res.get("reason"):
        reason = f"{module} 用不了：{res['reason']}"
    else:
        reason = f"{module} 在这台电脑上一加载就崩溃（退出码 {r.returncode}）"
        hint = crash_hint(r.returncode)
        if hint:
            reason += "。" + hint
    log.warning("%s\n%s", reason, tail or (r.stderr or b"").decode("utf-8", "replace")[-800:])
    _save(key, {"ok": False, "reason": reason})
    return reason


def probe_import_main(argv: list[str]) -> int:
    """（在小进程里运行）导入 argv[0]，结果写进 argv[1]（JSON）；崩溃时的调用栈写进同名 .trace。"""
    import faulthandler
    import importlib
    module, out = argv[0], Path(argv[1])
    was_on = faulthandler.is_enabled()      # 测试里在本进程调用：用完要还原成原来的样子
    trace = open(out.with_suffix(".trace"), "w", encoding="utf-8")   # noqa: SIM115 —— 进程结束前一直开着
    faulthandler.enable(file=trace, all_threads=True)
    try:
        importlib.import_module(module)
    except Exception as e:  # noqa: BLE001
        out.write_text(json.dumps({"ok": False, "reason": f"{type(e).__name__}: {str(e)[:150]}"}, ensure_ascii=False),
                       encoding="utf-8")
        return 3
    finally:                                  # 没崩：关掉调用栈文件（测试里在本进程调用时也不留着）
        if was_on and sys.stderr is not None:
            faulthandler.enable(file=sys.stderr, all_threads=True)
        else:
            faulthandler.disable()
        trace.close()
    out.write_text(json.dumps({"ok": True}), encoding="utf-8")
    return 0
