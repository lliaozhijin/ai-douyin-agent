"""整段视频转文字（导出文案用）：没有字幕的视频，用本机的 faster-whisper 把整段声音转成带时间的文字。

和刷视频时“听一听”（app/agent/listen.py，只听开头 90 秒）共用模型文件，但导出文案时要听完整段、要准一些
（beam_size=5、按停顿切段），所以单独开一个模型实例，导完就放掉，不占着内存。
"""
from __future__ import annotations

import logging
import time

from ..agent.listen import Listener, decode_audio

log = logging.getLogger(__name__)

ZH_PROMPT = "以下是普通话的句子，使用简体中文和标点符号。"   # 让 Whisper 出简体字、带标点


def transcribe(path: str, size: str = "base", language: str = "auto", progress=None,
               cancelled=lambda: False) -> tuple[list[dict], str]:
    """返回（[{start, end, text}], 认出来的语言）。用不了时抛 RuntimeError（写清楚原因）。"""
    lis = Listener()
    model = lis._load(size)
    if model is None:
        raise RuntimeError(lis._broken or "语音模型没准备好")
    audio = decode_audio(path, seconds=6 * 3600)
    lang = None if language in ("", "auto") else language
    t0 = time.time()
    # ffmpeg 没解出声音时，不马上说“没有声音”：换语音组件自己的解码器再试一次
    src = audio if audio is not None and len(audio) > 0 else path
    try:
        segments, info = model.transcribe(src, language=lang, beam_size=5, vad_filter=True,
                                          initial_prompt=ZH_PROMPT if lang == "zh" else None)
    except Exception as e:  # noqa: BLE001
        if src is path and audio is not None:
            raise RuntimeError(f"这个文件里的声音解不出来（{type(e).__name__}: {str(e)[:80]}）") from e
        raise
    if lang is None and getattr(info, "language", "") == "zh":
        # 自动认出是中文：带上“简体中文、加标点”的提示重新开始（上一次还没开始转，只花了认语言的那几秒）
        segments, info = model.transcribe(src, language="zh", beam_size=5, vad_filter=True, initial_prompt=ZH_PROMPT)
    total = float(getattr(info, "duration", 0) or 0)
    out = []
    for seg in segments:
        if cancelled():
            raise RuntimeError("取消了")
        t = (seg.text or "").strip()
        if t:
            out.append({"start": float(seg.start), "end": float(seg.end), "text": t})
        if progress and total:
            progress(min(1.0, float(seg.end) / total))
    lang_out = getattr(info, "language", "") or (lang or "")
    log.info("整段转文字：%d 段，%.0f 秒的视频用了 %.0f 秒（语言 %s）", len(out), total, time.time() - t0, lang_out)
    return out, lang_out
