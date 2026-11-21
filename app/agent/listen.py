"""听视频里的声音：把人说的话 / 唱的词转成文字（Whisper，本机运行），和画面、文案一起给模型判断。

- 浏览器一直是静音在后台播放的（启动参数 --mute-audio），不会吵到正在用电脑的你；
  这里不录电脑的声音，而是直接取抖音网页自己加载的视频地址，下载开头一小段，在本机转成文字。
- 用的是 faster-whisper（CTranslate2 版的 OpenAI Whisper）。安装包里自带 base 模型（约 145MB，打包时下载好），
  装完就能用、不用联网；换成 tiny / small 时才在第一次用时下载到 data/models/whisper。
- 没装 faster-whisper、模型下载不下来、或者这个视频拿不到地址：就当没听到，照常按画面和文案判断，不报错。
- 把视频里的声音解出来这一步用软件自带的 ffmpeg（imageio-ffmpeg），不走 faster-whisper 自己的解码：
  它的解码依赖 PyAV，PyAV 新版本去掉了一个参数（metadata_errors），会让每个视频都报
  “open() got an unexpected keyword argument 'metadata_errors'”。ffmpeg 用不了时才退回它自己的解码。
- **先在另一个进程里试一下**：语音组件（CTranslate2 等）是 C++ 写的，个别电脑、个别版本一加载就直接崩溃，
  连错误都来不及报，整个软件跟着闪退（2026-11-19：新装的 ctranslate2 4.9.0 在打包测试里一加载模型进程就没了）。
  所以第一次用之前，先开一个小进程加载模型、转一小段声音试试：没问题才在软件里加载；int8 崩了就换 float32 再试；
  都不行就这次不听（照常按画面和文案判断），软件不会闪退。试的结果按组件版本记在 data/listen_probe.json，
  同样的版本只试一次。
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

from ..paths import ASSETS_DIR, DATA_DIR, MODELS_DIR, ROOT

log = logging.getLogger(__name__)

WHISPER_DIR = MODELS_DIR / "whisper"                 # 第一次用时下载的模型放这里
BUNDLED_DIR = ASSETS_DIR / "models" / "whisper"      # 安装包里自带的模型（打包时由 tools/fetch_whisper.py 下载）
SIZES = ("tiny", "base", "small")
MAX_BYTES = 5 * 1024 * 1024      # 只下载视频开头这么多（大约前 30～60 秒）
SAMPLE_RATE = 16000              # Whisper 要的采样率
MAX_SECONDS = 90                 # 最多听这么长
SAME_ERROR_LIMIT = 3             # 同一个“程序 / 组件”类的错连着出现这么多次：这次运行不再试（不是视频的问题）
HF_MIRROR = "https://hf-mirror.com"
PROBE = True                     # 第一次加载前先在另一个进程里试（测试里可以关掉）
PROBE_FILE = DATA_DIR / "listen_probe.json"
COMPUTE_TYPES = ("int8", "float32")   # int8 最快；个别电脑 int8 会崩，就退回 float32
PROBE_PKGS = ("faster-whisper", "ctranslate2", "av", "onnxruntime", "numpy", "tokenizers")
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def decode_audio(path: str, seconds: int = MAX_SECONDS):
    """用自带的 ffmpeg 把视频 / 音频文件里的声音解成 16kHz 单声道的一串数（Whisper 直接能用）。

    只下载了视频开头一段，文件结尾是断的：ffmpeg 会报错但已经解出来的部分照样能用，所以不看返回码，只看有没有解出声音。
    ffmpeg 用不了返回 None（调用的地方再退回 faster-whisper 自己的解码）；
    ffmpeg 能用、但这个文件一点声音都解不出来（没有声音，或者开头这一段不完整），返回空的一串数。"""
    import subprocess
    import sys

    try:
        import numpy as np
        from ..publish.narrate import ffmpeg_exe
        exe = ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None
    flags = 0x08000000 if sys.platform == "win32" else 0      # 不弹黑色的命令行窗口
    args = [exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-t", str(int(seconds)), "-i", str(path),
            "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    try:
        # 整段转文字时视频可能很长：按长度放宽时间（以前固定 2 分钟，长视频会超时）
        r = subprocess.run(args, capture_output=True, timeout=max(120, int(seconds) // 15), creationflags=flags)
    except Exception:  # noqa: BLE001
        return None
    raw = r.stdout or b""
    if len(raw) < SAMPLE_RATE:           # 不到半秒：当没有声音
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(raw[: len(raw) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0


def has_audio(path: str) -> bool | None:
    """文件里有没有声音轨道（用 ffmpeg 看一眼文件信息，不解码）。ffmpeg 用不了返回 None（不知道）。"""
    import subprocess
    import sys
    try:
        from ..publish.narrate import ffmpeg_exe
        exe = ffmpeg_exe()
        r = subprocess.run([exe, "-nostdin", "-hide_banner", "-i", str(path)], capture_output=True, timeout=60,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
    except Exception:  # noqa: BLE001
        return None
    info = (r.stderr or b"").decode("utf-8", "replace")
    return "Audio:" in info


def installed() -> bool:
    """装没装 faster-whisper：只看找不找得到，不真的导入（导入时就可能崩）。"""
    import importlib.util
    try:
        return importlib.util.find_spec("faster_whisper") is not None
    except (ValueError, ImportError):      # 已经导入过了（或者测试里放了一个假的）
        return sys.modules.get("faster_whisper") is not None


def _pkg_versions() -> str:
    from importlib import metadata
    out = []
    for p in PROBE_PKGS:
        try:
            out.append(f"{p}={metadata.version(p)}")
        except Exception:  # noqa: BLE001 —— 打包后的程序里不一定读得到
            pass
    return ",".join(out)


def probe_key(size: str) -> str:
    """同样的软件版本 + 组件版本 + 模型大小 + 电脑上的 VC++ 运行库，只试一次（更新了运行库会自动再试）。"""
    from .. import APP_VERSION
    from ..native_probe import exe_stamp, runtime_stamp
    return f"{APP_VERSION}|{size}|{_pkg_versions()}|{runtime_stamp()}|{exe_stamp()}"


def probe_command(size: str, compute: str, out: Path) -> list[str]:
    """试加载用的命令：打包后的程序就是它自己（加 --probe-listen），源码运行时是 run.py。"""
    from ..native_probe import command
    return command(["--probe-listen", size, compute, str(out)])


def probe_main(argv: list[str]) -> int:
    """（在另一个进程里运行）加载模型、转 2 秒的声音试试。结果写进 out（JSON）；崩溃时的调用栈写进 out.trace。"""
    import faulthandler
    size, compute, out = argv[0], argv[1], Path(argv[2])
    was_on = faulthandler.is_enabled()      # 测试里在本进程调用：用完要还原成原来的样子
    trace = open(out.with_suffix(".trace"), "w", encoding="utf-8")   # noqa: SIM115 —— 进程结束前一直开着
    faulthandler.enable(file=trace, all_threads=True)

    def done(d: dict) -> None:
        out.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

    ls = Listener()
    ls.probe = False
    ls.compute = compute
    try:
        model = ls._load(size)
        if model is None:
            done({"ok": False, "reason": ls._broken, "transient": True})
            return 2
        import numpy as np
        t = np.arange(SAMPLE_RATE * 2) / SAMPLE_RATE
        audio = (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        segments, _info = model.transcribe(audio, language="zh", beam_size=1, vad_filter=True,
                                           condition_on_previous_text=False)
        list(segments)
    except Exception as e:  # noqa: BLE001
        done({"ok": False, "reason": f"{type(e).__name__}: {str(e).splitlines()[0][:150] if str(e) else ''}"})
        return 3
    finally:                                  # 没崩：关掉调用栈文件（测试里在本进程调用时也不留着）
        if was_on and sys.stderr is not None:
            faulthandler.enable(file=sys.stderr, all_threads=True)
        else:
            faulthandler.disable()
        trace.close()
    done({"ok": True, "compute": compute})
    return 0


def _read_probes() -> dict:
    try:
        d = json.loads(PROBE_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_probe(key: str, res: dict) -> None:
    d = _read_probes()
    d[key] = {**res, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    d = dict(list(d.items())[-6:])          # 只留最近几次
    try:
        PROBE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROBE_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def bundled_model(size: str) -> Path | None:
    """安装包里自带的这个大小的模型；没有返回 None。"""
    d = BUNDLED_DIR / f"faster-whisper-{size}"
    return d if (d / "model.bin").is_file() and (d / "config.json").is_file() else None


class Listener:
    def __init__(self):
        self._model = None
        self._size = ""
        self._broken = ""        # 这次运行里已经确认用不了的原因（不再反复尝试）
        self.last_error = ""
        self._same_error = ("", 0)   # （上一次的错，连着出现了几次）
        self.probe = PROBE           # 第一次加载前先在另一个进程里试
        self.compute = ""            # 用哪种精度（试出来的；空 = 还没试）

    # ---------- 模型 ----------
    def status(self) -> dict:
        bundled = [s for s in SIZES if bundled_model(s)]
        have = [s for s in SIZES if s in bundled or (WHISPER_DIR.exists() and any(WHISPER_DIR.glob(f"*faster-whisper-{s}*")))]
        return {"installed": installed(), "models": have, "bundled": bundled, "broken": self._broken}

    def _probe(self, size: str) -> str:
        """在另一个进程里试着加载模型。返回空字符串 = 可以放心加载（self.compute 设成试出来的精度），否则返回原因。"""
        key = probe_key(size)
        hit = _read_probes().get(key)
        if hit:
            if hit.get("ok"):
                self.compute = hit.get("compute") or COMPUTE_TYPES[0]
                return ""
            return hit.get("reason") or "语音组件在这台电脑上用不了"
        from ..native_probe import crash_hint
        crashes, hint = [], ""
        for compute in COMPUTE_TYPES:
            out = Path(tempfile.gettempdir()) / f"aiwtt_probe_{os.getpid()}_{size}_{compute}.json"
            trace = out.with_suffix(".trace")
            for f in (out, trace):
                try:
                    f.unlink()
                except OSError:
                    pass
            t0 = time.time()
            try:
                r = subprocess.run(probe_command(size, compute, out), cwd=str(ROOT), capture_output=True,
                                   timeout=300 if bundled_model(size) else 1800, creationflags=NO_WINDOW)
                code = r.returncode
            except subprocess.TimeoutExpired:
                return "试着加载语音模型太久没反应（这次先不听，下次启动再试）"
            except Exception as e:  # noqa: BLE001 —— 连小进程都开不了：不试了，直接在软件里加载（和以前一样）
                log.info("没能开小进程试语音模型（%s），直接加载", e)
                self.compute = COMPUTE_TYPES[0]
                return ""
            res = {}
            try:
                res = json.loads(out.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            if res.get("ok"):
                self.compute = res.get("compute") or compute
                _save_probe(key, {"ok": True, "compute": self.compute})
                log.info("语音模型试过了（%s，%.1f 秒）：可以用", self.compute, time.time() - t0)
                return ""
            if res.get("transient"):           # 模型没下载下来之类：下次再试，不记下来
                return res.get("reason") or "语音模型没准备好"
            tail = ""
            try:
                tail = "\n".join(trace.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-12:])
            except OSError:
                pass
            if not tail:
                tail = (r.stderr or b"").decode("utf-8", errors="replace").strip()[-800:]
            detail = res.get("reason") or f"进程直接退出了（退出码 {code}）"
            crashes.append(f"{compute}：{detail}")
            hint = hint or crash_hint(code)
            log.warning("语音模型用 %s 试加载失败：%s\n%s", compute, detail, tail)
        reason = ("语音组件在这台电脑上一加载就出错（" + "；".join(crashes)[:200] + "）。"
                  + (hint + "。" if hint else "")
                  + "这次先不听视频的声音，软件照常用；更新软件、组件或 VC++ 运行库后会自动再试")
        _save_probe(key, {"ok": False, "reason": reason})
        return reason

    def _load(self, size: str):
        size = size if size in SIZES else "base"
        if self._model is not None and self._size == size:
            return self._model
        if self._broken:
            return None
        if self.probe and "faster_whisper" not in sys.modules and installed():
            reason = self._probe(size)
            if reason:
                self._broken = reason
                log.warning("听不了视频的声音：%s", reason)
                return None
        compute = self.compute or COMPUTE_TYPES[0]
        try:
            # 国内直连 huggingface.co 经常连不上：没指定地址时用镜像
            os.environ.setdefault("HF_ENDPOINT", HF_MIRROR)
            from faster_whisper import WhisperModel
        except Exception as e:  # noqa: BLE001
            self._broken = f"没装听声音的组件 faster-whisper（{type(e).__name__}）"
            log.warning("听不了视频的声音：%s。用 pip install faster-whisper 安装后重启软件", self._broken)
            return None
        try:
            WHISPER_DIR.mkdir(parents=True, exist_ok=True)
            t0 = time.time()
            local = bundled_model(size)
            if local is not None:
                # 安装包里自带：直接用，不联网
                self._model = WhisperModel(str(local), device="cpu", compute_type=compute, local_files_only=True)
            else:
                self._model = WhisperModel(size, device="cpu", compute_type=compute, download_root=str(WHISPER_DIR))
            self._size = size
            log.info("语音模型 %s 准备好了（%s，%.1f 秒）", size, "软件自带" if local is not None else "已下载",
                     time.time() - t0)
        except Exception as e:  # noqa: BLE001
            self._broken = f"语音模型 {size} 没准备好：{str(e).splitlines()[0][:120]}"
            log.warning("听不了视频的声音：%s（模型要从网上下载，检查网络或代理后重启软件再试）", self._broken)
            return None
        return self._model

    # ---------- 听 ----------
    def transcribe_file(self, path: str, size: str = "base", max_chars: int = 400) -> str:
        model = self._load(size)
        if model is None:
            return ""
        audio = decode_audio(path)       # 自带的 ffmpeg 解出来的声音；ffmpeg 用不了才把文件交给 faster-whisper 自己解
        if audio is not None and len(audio) == 0:
            return ""                    # 这个视频没有声音，或者下载的这一段解不出来：当没听到
        segments, _info = model.transcribe(audio if audio is not None else path, language="zh", beam_size=1,
                                           vad_filter=True, condition_on_previous_text=False)
        out, n = [], 0
        for seg in segments:
            t = (seg.text or "").strip()
            if not t:
                continue
            out.append(t)
            n += len(t)
            if n >= max_chars:
                break
        return " ".join(out)[:max_chars]

    def _count_error(self, e: Exception, reason: str) -> None:
        """同一个“程序 / 组件”类的错（参数不对、缺东西）连着出现几次：不是哪个视频的问题，
        这次运行不再一个个视频试下去（以前会每个视频报一遍，日志里刷出一百多行）。"""
        if not isinstance(e, (TypeError, AttributeError, ImportError, NameError)):
            self._same_error = ("", 0)
            return
        last, n = self._same_error
        n = n + 1 if last == reason else 1
        self._same_error = (reason, n)
        if n >= SAME_ERROR_LIMIT:
            self._broken = f"听声音的组件出错了：{reason}"
            log.warning("听不了视频的声音：%s。连着 %d 个视频都是这个错，这次运行先不听了（照常按画面和文案判断）；"
                        "把这行日志发给开发者", self._broken, n)

    def listen(self, browser, video_id: str, size: str = "base") -> str:
        """听这个视频开头的一段，返回转出来的文字；听不了返回空字符串。"""
        self.last_error = ""
        if self._broken:
            return ""
        urls = browser.media_urls(video_id) if hasattr(browser, "media_urls") else []
        # 只认真的地址列表（测试里的假浏览器会返回别的东西：那样不能去加载真的语音模型）
        urls = [u for u in urls if isinstance(u, str) and u] if isinstance(urls, (list, tuple)) else []
        if not urls:
            self.last_error = "没拿到这个视频的地址"
            return ""
        if self._load(size) is None:
            return ""
        data = b""
        for url in urls[:3]:
            try:
                resp = browser.ctx.request.get(url, headers={"Referer": "https://www.douyin.com/",
                                                             "Range": f"bytes=0-{MAX_BYTES - 1}"}, timeout=20000)
                if resp.status in (200, 206):
                    data = resp.body()[:MAX_BYTES]
                    if len(data) > 20000:
                        break
            except Exception as e:  # noqa: BLE001
                self.last_error = f"视频下载失败：{str(e).splitlines()[0][:100]}"
        if len(data) <= 20000:
            self.last_error = self.last_error or "视频没下载下来"
            return ""
        tmp = Path(tempfile.gettempdir()) / f"aiwtt_listen_{os.getpid()}.mp4"
        try:
            tmp.write_bytes(data)
            t0 = time.time()
            text = self.transcribe_file(str(tmp), size)
            log.info("听了视频的声音（%.1f 秒）：%s", time.time() - t0, text[:40] or "（没有人说话）")
            self._same_error = ("", 0)
            return text
        except Exception as e:  # noqa: BLE001 —— 只下载了开头一段，个别视频解不出来：当没听到
            reason = str(e).splitlines()[0][:100] if str(e) else type(e).__name__
            self.last_error = f"声音没转出来：{reason}"
            log.info("这个视频的声音没转出来：%s", reason)
            self._count_error(e, reason)
            return ""
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
