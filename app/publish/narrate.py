"""把“她读文案的声音 + 文字卡片”合成一个竖屏视频（MP4，H.264 + AAC），当作视频作品发布。

用 imageio-ffmpeg 自带的 ffmpeg（打包进安装包，不需要用户另外安装）；电脑上本来就有 ffmpeg 也可以用。
几张卡片按顺序平均分配时长，声音读完后多停一小会儿。
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

W, H = 1080, 1440
TAIL = 0.8          # 读完以后画面再停多久（秒）
MIN_EACH = 1.5      # 每张卡片至少停多久


class NarrateError(Exception):
    pass


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        exe = shutil.which("ffmpeg")
        if exe:
            return exe
    raise NarrateError("没有找到合成视频用的组件 ffmpeg（重新运行一次 build.bat 会自动装上 imageio-ffmpeg）")


def _run(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    flags = 0x08000000 if sys.platform == "win32" else 0      # 不弹黑色的命令行窗口
    return subprocess.run(args, capture_output=True, timeout=timeout, creationflags=flags)


def audio_seconds(path: Path) -> float:
    """这段声音有多长（秒）。"""
    r = _run([ffmpeg_exe(), "-hide_banner", "-i", str(path), "-f", "null", "-"], timeout=120)
    text = (r.stderr or b"").decode("utf-8", "ignore")
    times = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if times:
        h, m, s = times[-1]
        return int(h) * 3600 + int(m) * 60 + float(s)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    raise NarrateError("读不出这段声音的长度")


def make_video(images: list[Path], audio: Path, out: Path) -> float:
    """图片（一张或几张）+ 声音 → MP4。返回视频时长（秒）。"""
    images = [Path(p) for p in images if Path(p).exists()]
    if not images:
        raise NarrateError("没有可以当画面的图片（先生成文字卡片或加一张图）")
    dur = audio_seconds(audio)
    if dur <= 0.3:
        raise NarrateError("声音太短了")
    n = len(images)
    while n > 1 and dur / n < MIN_EACH:        # 声音很短、卡片很多：只用前面几张
        n -= 1
    images = images[:n]
    each = dur / n
    args = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error"]
    for i, img in enumerate(images):
        args += ["-loop", "1", "-t", f"{each + (TAIL if i == n - 1 else 0):.3f}", "-i", str(img)]
    args += ["-i", str(audio)]
    parts = []
    for i in range(n):
        parts.append(f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                     f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=white,setsar=1,fps=25,format=yuv420p[v{i}]")
    parts.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]")
    parts.append(f"[{n}:a]apad=pad_dur={TAIL}[a]")
    args += ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-tune", "stillimage", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-movflags", "+faststart", "-shortest", str(out)]
    r = _run(args)
    if r.returncode != 0 or not out.exists() or out.stat().st_size < 2000:
        err = (r.stderr or b"").decode("utf-8", "ignore").strip().splitlines()
        raise NarrateError("合成视频失败：" + (err[-1][:160] if err else "ffmpeg 没有输出"))
    return dur + TAIL
