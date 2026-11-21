"""yt-dlp（https://github.com/yt-dlp/yt-dlp）：YouTube、B 站、TikTok、快手、微博…… 一千多个网站的下载都靠它。

- 安装包里自带 yt-dlp（pip 装的 yt-dlp[default]，含 yt-dlp-ejs：解 YouTube 签名要用的脚本）。
  网站一改版 yt-dlp 就要更新：界面上点“更新下载组件”会把官方发布的最新版（zipimport 格式的 yt-dlp 文件）
  下载到 data/tools/yt-dlp.pyz，下次启动优先用它（比自带的新才用）。
- 2025.11 起下载 YouTube 需要一个 JavaScript 运行环境（官方说明：https://github.com/yt-dlp/yt-dlp/wiki/EJS）：
  优先用 deno（yt-dlp 默认就认它），也认 node（22 以上）、bun、quickjs。电脑上都没有时，界面上点一下
  “安装 deno”，会把 deno 官方发布的单文件版下载到 data/tools/deno/ 里（不改系统设置）。
- 合并音视频、转格式用软件自带的 ffmpeg（imageio-ffmpeg）。
- 选项用 yt_dlp.parse_options 从命令行参数转换（官方推荐的做法，见 devscripts/cli_to_api.py），
  不手写内部参数的格式，yt-dlp 改了内部写法也不受影响。
"""
from __future__ import annotations

import logging
import os
import platform as _pf
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from ..paths import DATA_DIR

log = logging.getLogger(__name__)

TOOLS_DIR = DATA_DIR / "tools"
PYZ = TOOLS_DIR / "yt-dlp.pyz"
DENO_DIR = TOOLS_DIR / "deno"
YTDLP_LATEST = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
DENO_LATEST = "https://github.com/denoland/deno/releases/latest/download/{asset}"
_loaded = None


def _pyz_version(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            src = z.read("yt_dlp/version.py").decode("utf-8", "ignore")
        m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", src)
        return m.group(1) if m else ""
    except Exception:  # noqa: BLE001
        return ""


def _bundled_version() -> str:
    try:
        from importlib.metadata import version
        return version("yt-dlp")
    except Exception:  # noqa: BLE001
        return ""


def _vkey(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4])


def load():
    """导入 yt_dlp：data/tools/yt-dlp.pyz 比自带的新就用它。没有 yt-dlp 返回 None。"""
    global _loaded
    if _loaded is not None:
        return _loaded
    if PYZ.is_file() and "yt_dlp" not in sys.modules:
        newer, bundled = _pyz_version(PYZ), _bundled_version()
        if newer and _vkey(newer) > _vkey(bundled):
            sys.path.insert(0, str(PYZ))
            log.info("下载组件：用 data/tools 里更新过的 yt-dlp %s（自带的是 %s）", newer, bundled or "没有")
    try:
        import yt_dlp
    except Exception as e:  # noqa: BLE001
        log.warning("没有下载组件 yt-dlp：%s", e)
        return None
    _loaded = yt_dlp
    return yt_dlp


def version() -> str:
    m = load()
    try:
        return m.version.__version__ if m else ""
    except Exception:  # noqa: BLE001
        return ""


def has_ejs() -> bool:
    try:
        import yt_dlp_ejs  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return PYZ.is_file() and "yt_dlp" in sys.modules and str(PYZ) in sys.path


# ---------------------------------------------------------------- JavaScript 运行环境
def _exe(name: str) -> str:
    return name + (".exe" if os.name == "nt" else "")


def _node_ok(path: str) -> bool:
    try:
        flags = 0x08000000 if sys.platform == "win32" else 0
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10, creationflags=flags).stdout
        return _vkey(out)[:1] >= (22,)
    except Exception:  # noqa: BLE001
        return False


def js_runtime() -> tuple[str, str]:
    """电脑上能给 yt-dlp 用的 JavaScript 运行环境：(名字, 路径)；都没有返回 ("", "")。"""
    mine = DENO_DIR / _exe("deno")
    if mine.is_file():
        return "deno", str(mine)
    p = shutil.which("deno")
    if p:
        return "deno", p
    p = shutil.which("node")
    if p and _node_ok(p):
        return "node", p
    for name in ("bun", "qjs"):
        p = shutil.which(name)
        if p:
            return ("quickjs" if name == "qjs" else name), p
    return "", ""


def deno_asset() -> str:
    mach = _pf.machine().lower()
    arm = mach in ("arm64", "aarch64")
    if sys.platform == "win32":
        return "deno-x86_64-pc-windows-msvc.zip"
    if sys.platform == "darwin":
        return "deno-aarch64-apple-darwin.zip" if arm else "deno-x86_64-apple-darwin.zip"
    return "deno-aarch64-unknown-linux-gnu.zip" if arm else "deno-x86_64-unknown-linux-gnu.zip"


def _fetch(url: str, dest: Path, progress=None, proxy: str = "", cancelled=lambda: False) -> None:
    import requests

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    with requests.get(url, stream=True, timeout=30, proxies=proxies, allow_redirects=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        got = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(256 * 1024):
                if cancelled():
                    raise RuntimeError("取消了")
                f.write(chunk)
                got += len(chunk)
                if progress and total:
                    progress(got / total)
    tmp.replace(dest)


def install_deno(progress=None, proxy: str = "", cancelled=lambda: False) -> str:
    """把 deno 官方发布的单文件版下载到 data/tools/deno/（只下载这一个程序，不改系统设置）。返回路径。"""
    z = TOOLS_DIR / "deno.zip"
    _fetch(DENO_LATEST.format(asset=deno_asset()), z, progress, proxy, cancelled)
    DENO_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(z) as zf:
        name = next(n for n in zf.namelist() if Path(n).name.lower() in ("deno", "deno.exe"))
        target = DENO_DIR / _exe("deno")
        with zf.open(name) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
    z.unlink(missing_ok=True)
    if os.name != "nt":
        target.chmod(0o755)
    return str(target)


def update_ytdlp(progress=None, proxy: str = "", cancelled=lambda: False) -> str:
    """下载官方发布的最新 yt-dlp（zipimport 格式，一个文件就是整个程序）到 data/tools/yt-dlp.pyz。返回版本号。"""
    tmp = TOOLS_DIR / "yt-dlp.new.pyz"
    _fetch(YTDLP_LATEST, tmp, progress, proxy, cancelled)
    v = _pyz_version(tmp)
    if not v:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("下载下来的文件不是 yt-dlp")
    tmp.replace(PYZ)
    return v


# ---------------------------------------------------------------- 选项
def ffmpeg_path() -> str:
    try:
        from ..publish.narrate import ffmpeg_exe
        return ffmpeg_exe()
    except Exception:  # noqa: BLE001
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:  # noqa: BLE001
            return shutil.which("ffmpeg") or ""


def cli_args(proxy: str = "", cookies: str = "") -> list[str]:
    """通用的命令行参数：JS 运行环境、ffmpeg、代理、cookies。"""
    args = ["--ignore-config", "--no-playlist", "--windows-filenames", "--no-mtime", "--retries", "5",
            "--fragment-retries", "5", "--concurrent-fragments", "4"]
    name, path = js_runtime()
    if name:
        args += ["--js-runtimes", f"{name}:{path}"]
    if not has_ejs():
        args += ["--remote-components", "ejs:github"]   # 没带 yt-dlp-ejs 时，让 yt-dlp 自己从 GitHub 取解签名的脚本
    ff = ffmpeg_path()
    if ff:
        args += ["--ffmpeg-location", ff]
    if proxy:
        args += ["--proxy", proxy]
    if cookies and Path(cookies).is_file():
        args += ["--cookies", cookies]
    return args


def options(args: list[str], extra: dict | None = None) -> dict:
    """命令行参数 → YoutubeDL 的选项。"""
    m = load()
    if m is None:
        raise RuntimeError("没有下载组件 yt-dlp")
    try:
        opts = dict(m.parse_options(args).ydl_opts)
    except Exception as e:  # noqa: BLE001 —— 版本太老 / 某个参数不认：去掉 JS 和远程组件那两项再试
        log.warning("yt-dlp 不认部分参数（%s），去掉 JS 运行环境的设置再试", e)
        keep, skip = [], False
        for a in args:
            if skip:
                skip = False
                continue
            if a in ("--js-runtimes", "--remote-components"):
                skip = True
                continue
            keep.append(a)
        opts = dict(m.parse_options(keep).ydl_opts)
    opts["logger"] = _Log()
    opts.update(extra or {})
    return opts


class _Log:
    """yt-dlp 的输出写进软件自己的日志（软件没有命令行窗口）。"""

    def debug(self, msg):
        if not str(msg).startswith("[debug]"):
            log.debug("yt-dlp：%s", msg)

    def info(self, msg):
        log.debug("yt-dlp：%s", msg)

    def warning(self, msg):
        log.info("yt-dlp 提醒：%s", msg)

    def error(self, msg):
        log.warning("yt-dlp 出错：%s", msg)


def status() -> dict:
    name, path = js_runtime()
    return {"ytdlp": version(), "ejs": has_ejs(), "js": name, "js_path": path, "ffmpeg": bool(ffmpeg_path()),
            "updated": PYZ.is_file()}
