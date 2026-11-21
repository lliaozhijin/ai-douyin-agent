"""检查“听视频声音”的组件在这台电脑上能不能用：一步一步、每一步都在单独的小进程里试，哪一步崩了一看就知道。

用法（build.bat 会自动运行；也可以自己运行）：
    .venv\\Scripts\\python.exe tools\\check_listen.py
结果打印出来，同时写进 data\\logs\\listen_check.txt。不管结果如何都不会让打包失败。
"""
from __future__ import annotations

import subprocess
import sys
import time
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL = ROOT / "assets" / "models" / "whisper" / "faster-whisper-base"
OUT = ROOT / "data" / "logs" / "listen_check.txt"
PKGS = ("faster-whisper", "ctranslate2", "av", "onnxruntime", "numpy", "tokenizers", "huggingface_hub")

TONE = ("import numpy as np; t=np.arange(32000)/16000; a=(0.2*np.sin(2*np.pi*440*t)).astype(np.float32); "
        "s,_=m.transcribe(a, language='zh', beam_size=1, vad_filter=True, condition_on_previous_text=False); "
        "print('segments', len(list(s)))")


def load(compute: str, extra: str = "") -> str:
    return (f"from faster_whisper import WhisperModel; "
            f"m=WhisperModel(r'{MODEL}', device='cpu', compute_type='{compute}', local_files_only=True{extra}); "
            f"print('loaded {compute}')")


STEPS = [
    ("导入 numpy", "import numpy; print(numpy.__version__)"),
    ("导入 av（PyAV）", "import av; print(av.__version__)"),
    ("导入 ctranslate2", "import ctranslate2 as c; print(c.__version__, sorted(c.get_supported_compute_types('cpu')))"),
    ("导入 onnxruntime", "import onnxruntime as o; print(o.__version__)"),
    ("导入 faster_whisper", "import faster_whisper; print(faster_whisper.__version__)"),
    ("加载模型（int8）", load("int8")),
    ("加载模型（float32）", load("float32")),
    ("加载模型（int8，单线程）", load("int8", ", cpu_threads=1")),
    ("int8 转一段声音（带 VAD）", load("int8") + "; " + TONE),
    ("float32 转一段声音（带 VAD）", load("float32") + "; " + TONE),
    ("先导入 onnxruntime 再加载模型", "import onnxruntime; " + load("int8")),
    ("软件里的试加载（和软件运行时一样）", None),
]


def file_version(path: str) -> str:
    """Windows 文件的版本号（只读文件信息，不加载它）。"""
    try:
        import ctypes
        from ctypes import wintypes
        ver = ctypes.windll.version
        size = ver.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(str(path), 0, size, buf):
            return ""
        ptr, n = ctypes.c_void_p(), wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(n)):
            return ""
        info = (ctypes.c_uint32 * 4).from_address(ptr.value)      # VS_FIXEDFILEINFO 前 4 个字段
        ms, ls = info[2], info[3]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:  # noqa: BLE001
        return ""


LOADED_RUNTIME = ("import ctypes; h=ctypes.WinDLL('msvcp140.dll')._handle; b=ctypes.create_unicode_buffer(400); "
                  "ctypes.windll.kernel32.GetModuleFileNameW(ctypes.c_void_p(h), b, 400); print(b.value)")


def runtime_lines() -> list[str]:
    """Microsoft VC++ 运行库（msvcp140.dll）：低于 14.40 时，新版 onnxruntime / ctranslate2 一启动就崩溃。"""
    if sys.platform != "win32":
        return []
    import os
    sys_dll = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "msvcp140.dll"
    out = [f"VC++ 运行库：System32\\msvcp140.dll {file_version(str(sys_dll)) or '没有'}（需要 14.40 以上）"]
    try:
        r = subprocess.run([sys.executable, "-c", LOADED_RUNTIME], capture_output=True, timeout=60)
        used = (r.stdout or b"").decode("utf-8", "replace").strip()
        if used:
            out.append(f"  Python 实际加载的是：{used} {file_version(used)}")
    except Exception:  # noqa: BLE001
        pass
    return out


def run(code: str | None) -> tuple[str, str, float]:
    t0 = time.time()
    if code is None:
        out = ROOT / "data" / "logs" / "listen_check_probe.json"
        cmd = [sys.executable, str(ROOT / "run.py"), "--probe-listen", "base", "int8", str(out)]
    else:
        cmd = [sys.executable, "-X", "faulthandler", "-c", code]
    try:
        r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, timeout=600)
    except subprocess.TimeoutExpired:
        return "超时", "", time.time() - t0
    text = (r.stdout or b"").decode("utf-8", "replace").strip()
    err = (r.stderr or b"").decode("utf-8", "replace").strip()
    if code is None:
        try:
            text = out.read_text(encoding="utf-8")
        except OSError:
            pass
        trace = out.with_suffix(".trace")
        if trace.exists():
            err = (err + "\n" + trace.read_text(encoding="utf-8", errors="replace")).strip()
    if r.returncode == 0:
        return "通过", text[-300:], time.time() - t0
    kind = "崩溃" if r.returncode < 0 or r.returncode > 255 else "出错"
    return f"{kind}（退出码 {r.returncode}）", (text + "\n" + err)[-1500:].strip(), time.time() - t0


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"语音组件检查  {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"Python {sys.version.split()[0]}  {sys.executable}"]
    for p in PKGS:
        try:
            lines.append(f"  {p} {metadata.version(p)}")
        except Exception:  # noqa: BLE001
            lines.append(f"  {p} 没装")
    lines.append(f"模型：{MODEL}（{'有' if (MODEL / 'model.bin').is_file() else '没有'}）")
    lines += runtime_lines()
    lines.append("")
    print("\n".join(lines), flush=True)
    for name, code in STEPS:
        if code and "WhisperModel" in code and not (MODEL / "model.bin").is_file():
            res, detail, sec = "跳过（没有模型）", "", 0.0
        else:
            res, detail, sec = run(code)
        line = f"[{res}] {name}（{sec:.1f} 秒）"
        print(line, flush=True)
        lines.append(line)
        if detail and not res.startswith("通过"):
            block = "\n".join("    " + x for x in detail.splitlines()[-25:])
            print(block, flush=True)
            lines.append(block)
        elif detail:
            lines.append("    " + detail.splitlines()[-1][:200])
    try:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n结果已保存到 {OUT}")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
