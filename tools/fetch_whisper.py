"""打包前把语音模型下载到 assets/models/whisper/faster-whisper-<大小>，这样安装包里自带模型，用户不用再下载。

用法：python tools/fetch_whisper.py [tiny|base|small]   （默认 base，约 145MB）
先连 huggingface.co，连不上自动换国内镜像 hf-mirror.com。下载失败不影响打包：软件会在第一次用时再下载。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NEEDED = ("model.bin", "config.json", "tokenizer.json")


def model_dir(size: str) -> Path:
    return ROOT / "assets" / "models" / "whisper" / f"faster-whisper-{size}"


def complete(d: Path) -> bool:
    return all((d / n).is_file() and (d / n).stat().st_size > 0 for n in NEEDED)


def fetch(size: str) -> bool:
    out = model_dir(size)
    if complete(out):
        print(f"语音模型 {size} 已经在 {out}")
        return True
    try:
        from faster_whisper import download_model
    except Exception as e:  # noqa: BLE001
        print(f"没装 faster-whisper（{type(e).__name__}），跳过语音模型")
        return False
    endpoints = [os.environ["HF_ENDPOINT"]] if os.environ.get("HF_ENDPOINT") else ["", "https://hf-mirror.com"]
    for ep in endpoints:
        if ep:
            os.environ["HF_ENDPOINT"] = ep
        code = (f"import os; os.environ.update(HF_ENDPOINT={ep!r}) if {ep!r} else None; "
                f"from faster_whisper import download_model; download_model({size!r}, output_dir={str(out)!r})")
        print(f"下载语音模型 {size}（{ep or 'huggingface.co'}）...")
        import subprocess
        # 换地址要在新的进程里才生效（huggingface_hub 在导入时就读了 HF_ENDPOINT）
        if subprocess.call([sys.executable, "-c", code]) == 0 and complete(out):
            shutil.rmtree(out / ".cache", ignore_errors=True)     # 下载时留下的临时文件，不用打包
            print(f"语音模型 {size} 下载完成：{out}")
            return True
    print(f"语音模型 {size} 没下载下来：安装包里不带模型，软件第一次用时会再下载")
    return False


if __name__ == "__main__":
    size = sys.argv[1] if len(sys.argv) > 1 else "base"
    if size not in ("tiny", "base", "small"):
        sys.exit("只支持 tiny / base / small")
    fetch(size)
    sys.exit(0)        # 失败也不让打包中断
